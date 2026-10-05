"""Provenance Guard: Flask API (planning.md §2, §5, §6, §8, §9.2-9.4)."""

import os
import uuid

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, jsonify, render_template, request  # noqa: E402
from flask_limiter import Limiter  # noqa: E402
from flask_limiter.util import get_remote_address  # noqa: E402
from werkzeug.exceptions import HTTPException  # noqa: E402

import storage  # noqa: E402
from certificate import evaluate_certificate  # noqa: E402
from detection import analyze_image, analyze_text, llm_available  # noqa: E402
from labels import build_label  # noqa: E402

MIN_TEXT_CHARS = 20
MAX_TEXT_CHARS = 10_000
MIN_REASONING_CHARS = 20
MAX_REASONING_CHARS = 2_000
MAX_TITLE_CHARS = 200
EXCERPT_CHARS = 300

SUBMIT_LIMIT = "10 per minute;100 per day"
APPEAL_LIMIT = "5 per hour"
CERTIFICATE_LIMIT = "5 per hour"


def error(code, message, status, **extra):
    body = {"error": code, "message": message}
    body.update(extra)
    return jsonify(body), status


def _json_body():
    """Return the request's JSON object, or None if the body is missing / not a JSON object."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _label_for(row):
    return build_label(row["attribution"], row["confidence"], row["status"], row.get("certificate"))


def _signal_scores(signals):
    return {name: (s or {}).get("score") for name, s in (signals or {}).items()}


def _decision_snapshot(row):
    return {
        "attribution": row["attribution"],
        "ai_score": row["ai_score"],
        "confidence": row["confidence"],
        "label_variant": row["label_variant"],
        "status": row["status"],
        "signal_scores": _signal_scores(row.get("signals")),
        "adjustments": row.get("adjustments") or [],
    }


def _public_record(row):
    rec = {
        "content_id": row["content_id"],
        "creator_id": row["creator_id"],
        "title": row.get("title"),
        "content_type": row["content_type"],
        "attribution": row["attribution"],
        "ai_score": row["ai_score"],
        "confidence": row["confidence"],
        "status": row["status"],
        "signals": row.get("signals") or {},
        "adjustments": row.get("adjustments") or [],
        "label": _label_for(row),
        "timestamp": row["created_at"],
    }
    if row["original_attribution"] != row["attribution"]:
        rec["original_attribution"] = row["original_attribution"]
    if row["content_type"] == "image":
        rec["metadata"] = row.get("metadata")
    if row.get("appeal_filed_at"):
        rec["appeal"] = {
            "appeal_id": row.get("appeal_id"),
            "filed_at": row["appeal_filed_at"],
            "creator_reasoning": row.get("appeal_reasoning"),
            "reviewer_decision": row.get("reviewer_decision"),
            "reviewer_note": row.get("reviewer_note"),
            "resolved_at": row.get("resolved_at"),
        }
    if row.get("certificate"):
        rec["certificate"] = row["certificate"]
    return rec


def _queue_item(row):
    signals = row.get("signals") or {}
    llm = (signals.get("llm") or {}).get("details") or {}
    lex = (signals.get("lexical") or {}).get("details") or {}
    sty = (signals.get("stylometric") or {}).get("details") or {}
    text = row.get("text") or ""
    return {
        "content_id": row["content_id"],
        "appeal_id": row.get("appeal_id"),
        "creator_id": row["creator_id"],
        "title": row.get("title"),
        "content_type": row["content_type"],
        "status": row["status"],
        "filed_at": row["appeal_filed_at"],
        "creator_reasoning": row.get("appeal_reasoning"),
        "excerpt": text[:EXCERPT_CHARS] + ("..." if len(text) > EXCERPT_CHARS else ""),
        "original_attribution": row["original_attribution"],
        "ai_score": row["ai_score"],
        "confidence": row["confidence"],
        "adjustments": row.get("adjustments") or [],
        "signal_scores": _signal_scores(signals),
        "llm_reasoning": llm.get("reasoning"),
        "lexical_matches": lex,
        "stylometric_metrics": sty,
        "metadata": row.get("metadata") if row["content_type"] == "image" else None,
        "reviewer_decision": row.get("reviewer_decision"),
        "reviewer_note": row.get("reviewer_note"),
        "resolved_at": row.get("resolved_at"),
    }


def create_app(db_path=None):
    app = Flask(__name__)
    app.config["DB_PATH"] = storage.resolve_db_path(db_path)
    app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024  # 1 MB request body cap -> 413
    app.json.sort_keys = False
    storage.init_db(app.config["DB_PATH"])
    db = app.config["DB_PATH"]

    # One limiter per app so each test app gets fresh in-memory counters.
    limiter = Limiter(get_remote_address, app=app, storage_uri="memory://", default_limits=[])
    app.extensions["pg_limiter"] = limiter

    # ------------------------------------------------------------ error handlers (always JSON)

    @app.errorhandler(429)
    def rate_limited(e):
        return error("rate_limited",
                     "Too many requests. Please wait before trying again.",
                     429, limit=str(e.description))

    @app.errorhandler(HTTPException)
    def http_error(e):
        codes = {400: "bad_request", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
                 409: "conflict", 413: "payload_too_large", 415: "unsupported_media_type"}
        return error(codes.get(e.code, "http_error"), e.description or e.name, e.code)

    @app.errorhandler(Exception)
    def internal_error(e):
        app.logger.exception("Unhandled error")
        return error("internal_error", "Something went wrong on our side.", 500)

    # ------------------------------------------------------------ routes

    @app.get("/")
    def index():
        return jsonify({
            "service": "Provenance Guard",
            "endpoints": {
                "POST /submit": "Classify a text or image submission",
                "GET /content/<content_id>": "Current record for a piece",
                "POST /appeal": "Creator appeals a label",
                "GET /appeals?status=open|resolved|all": "Reviewer queue",
                "POST /appeals/<content_id>/resolve": "Reviewer decision: upheld | overturned",
                "POST /certificate": "Draft-history provenance certificate",
                "GET /log?limit=20": "Audit log, newest first",
                "GET /stats": "Analytics JSON",
                "GET /dashboard": "Analytics HTML",
                "GET /health": "Service health",
            },
        })

    @app.get("/health")
    def health():
        try:
            llm = bool(llm_available())
        except Exception:
            llm = False
        return jsonify({"status": "ok", "llm_available": llm})

    @app.post("/submit")
    @limiter.limit(SUBMIT_LIMIT)
    def submit():
        data = _json_body()
        if data is None:
            return error("invalid_json", "Request body must be a JSON object.", 400)

        creator_id = data.get("creator_id")
        if not isinstance(creator_id, str) or not creator_id.strip():
            return error("missing_field", "creator_id is required.", 400)
        creator_id = creator_id.strip()

        title = data.get("title")
        if title is not None and (not isinstance(title, str) or len(title) > MAX_TITLE_CHARS):
            return error("invalid_field", f"title must be a string of at most {MAX_TITLE_CHARS} characters.", 400)

        content_type = data.get("content_type", "text") or "text"
        if content_type not in ("text", "image"):
            return error("invalid_field", "content_type must be 'text' or 'image'.", 400)

        metadata = None
        if content_type == "text":
            text = data.get("text")
            if not isinstance(text, str) or not text.strip():
                return error("missing_field", "text is required.", 400)
            n = len(text.strip())
            if n < MIN_TEXT_CHARS:
                return error("text_too_short", f"text must be at least {MIN_TEXT_CHARS} characters (got {n}).", 400)
            if n > MAX_TEXT_CHARS:
                return error("text_too_long", f"text must be at most {MAX_TEXT_CHARS:,} characters (got {n:,}).", 400)
            text = text.strip()
            result = analyze_text(text)
        else:
            metadata = data.get("metadata")
            if not isinstance(metadata, dict):
                return error("missing_field", "Image submissions require a 'metadata' object.", 400)
            description = data.get("description")
            if description is not None and not isinstance(description, str):
                return error("invalid_field", "description must be a string.", 400)
            if description and len(description) > MAX_TEXT_CHARS:
                return error("text_too_long", f"description must be at most {MAX_TEXT_CHARS:,} characters.", 400)
            text = (description or "").strip()
            result = analyze_image(metadata, text or None)

        status = "classified"
        record = {
            "content_id": str(uuid.uuid4()),
            "text": text,
            "title": title,
            "content_type": content_type,
            "metadata": metadata,
            "creator_id": creator_id,
            "attribution": result["attribution"],
            "original_attribution": result["attribution"],
            "ai_score": result["ai_score"],
            "confidence": result["confidence"],
            "signals": result.get("signals") or {},
            "adjustments": result.get("adjustments") or [],
            "label_variant": build_label(result["attribution"], result["confidence"], status)["variant"],
            "status": status,
            "created_at": storage.now_iso(),
        }
        row = storage.save_content(record, db)
        storage.append_audit("submission", row, db_path=db, details={
            "title": title,
            "word_count": result.get("word_count"),
            "adjustments": row["adjustments"],
            "signal_weights": {k: (v or {}).get("weight") for k, v in row["signals"].items()},
            "signal_scores": _signal_scores(row["signals"]),
        })
        return jsonify(_public_record(row)), 201

    @app.get("/content/<content_id>")
    def get_content(content_id):
        row = storage.get_content(content_id, db)
        if row is None:
            return error("not_found", f"No content with id '{content_id}'.", 404)
        return jsonify(_public_record(row))

    @app.post("/appeal")
    @limiter.limit(APPEAL_LIMIT)
    def appeal():
        data = _json_body()
        if data is None:
            return error("invalid_json", "Request body must be a JSON object.", 400)
        content_id, creator_id = data.get("content_id"), data.get("creator_id")
        if not isinstance(content_id, str) or not content_id:
            return error("missing_field", "content_id is required.", 400)
        if not isinstance(creator_id, str) or not creator_id:
            return error("missing_field", "creator_id is required.", 400)

        row = storage.get_content(content_id, db)
        if row is None:
            return error("not_found", f"No content with id '{content_id}'.", 404)
        if creator_id.strip() != row["creator_id"]:
            return error("forbidden", "Only the creator of this piece can appeal its label.", 403)

        reasoning = data.get("creator_reasoning")
        if not isinstance(reasoning, str) or not reasoning.strip():
            return error("missing_field", "creator_reasoning is required.", 400)
        reasoning = reasoning.strip()
        if len(reasoning) < MIN_REASONING_CHARS:
            return error("reasoning_too_short",
                         f"creator_reasoning must be at least {MIN_REASONING_CHARS} characters.", 400)
        if len(reasoning) > MAX_REASONING_CHARS:
            return error("reasoning_too_long",
                         f"creator_reasoning must be at most {MAX_REASONING_CHARS:,} characters.", 400)
        evidence_url = data.get("evidence_url")
        if evidence_url is not None and not isinstance(evidence_url, str):
            return error("invalid_field", "evidence_url must be a string.", 400)

        if row["status"] == "under_review":
            return error("conflict", "An appeal for this piece is already open.", 409)

        original = _decision_snapshot(row)
        appeal_id = "AP-" + uuid.uuid4().hex[:12]
        filed_at = storage.now_iso()
        row = storage.update_content(
            content_id, db,
            status="under_review", label_variant="under_review",
            appeal_id=appeal_id, appeal_reasoning=reasoning, appeal_filed_at=filed_at,
            reviewer_decision=None, reviewer_note=None, resolved_at=None,
        )
        storage.append_audit("appeal_filed", row, db_path=db, details={
            "appeal_id": appeal_id,
            "original_decision": original,
            "evidence_url": evidence_url,
        })
        return jsonify({
            "appeal_id": appeal_id,
            "content_id": content_id,
            "status": "under_review",
            "filed_at": filed_at,
            "original_decision": original,
            "label": _label_for(row),
            "message": "Your appeal has been filed. A person will review this label; until then readers see "
                       "a 'Label under review' notice.",
        }), 201

    @app.get("/appeals")
    def appeals_queue():
        status = request.args.get("status", "open")
        if status not in ("open", "resolved", "all"):
            return error("invalid_field", "status must be one of: open, resolved, all.", 400)
        items = [_queue_item(r) for r in storage.list_appeals(status, db)]
        return jsonify({"status": status, "count": len(items), "appeals": items})

    @app.post("/appeals/<content_id>/resolve")
    def resolve_appeal(content_id):
        data = _json_body()
        if data is None:
            return error("invalid_json", "Request body must be a JSON object.", 400)
        decision = data.get("decision")
        if decision not in ("upheld", "overturned"):
            return error("invalid_field", "decision must be 'upheld' or 'overturned'.", 400)
        note = data.get("reviewer_note") or ""
        if not isinstance(note, str):
            return error("invalid_field", "reviewer_note must be a string.", 400)

        row = storage.get_content(content_id, db)
        if row is None:
            return error("not_found", f"No content with id '{content_id}'.", 404)
        if row["status"] != "under_review":
            return error("conflict", "This piece has no open appeal to resolve.", 409)

        before = _decision_snapshot(row)
        fields = {
            "status": f"resolved_{decision}",
            "reviewer_decision": decision,
            "reviewer_note": note.strip(),
            "resolved_at": storage.now_iso(),
        }
        if decision == "overturned":
            fields["attribution"] = "likely_human"
        new_status = fields["status"]
        attribution = fields.get("attribution", row["attribution"])
        fields["label_variant"] = build_label(attribution, row["confidence"], new_status,
                                              row.get("certificate"))["variant"]
        row = storage.update_content(content_id, db, **fields)
        storage.append_audit("appeal_resolved", row, db_path=db, details={
            "appeal_id": row.get("appeal_id"),
            "decision": decision,
            "reviewer_note": row["reviewer_note"],
            "before": before,
        })
        return jsonify(_public_record(row))

    @app.post("/certificate")
    @limiter.limit(CERTIFICATE_LIMIT)
    def certificate():
        data = _json_body()
        if data is None:
            return error("invalid_json", "Request body must be a JSON object.", 400)
        missing = [f for f in ("content_id", "creator_id", "draft_text", "process_notes")
                   if not isinstance(data.get(f), str) or not data.get(f).strip()]
        if missing:
            return error("missing_field", f"Missing required field(s): {', '.join(missing)}.", 400)

        row = storage.get_content(data["content_id"], db)
        if row is None:
            return error("not_found", f"No content with id '{data['content_id']}'.", 404)
        if row.get("certificate"):
            return error("conflict", "This piece already has a certificate.", 409,
                         certificate=row["certificate"])

        ok, checks, cert = evaluate_certificate(row, data["creator_id"].strip(),
                                                data["draft_text"], data["process_notes"])
        if not ok:
            storage.append_audit("certificate_denied", row, db_path=db, creator_id=data["creator_id"].strip(),
                                 details={"checks": checks})
            return error("certificate_denied", "The verification checks did not all pass.", 422,
                         checks=checks)

        label = build_label(row["attribution"], row["confidence"], row["status"], cert)
        row = storage.update_content(row["content_id"], db, certificate=cert, label_variant=label["variant"])
        storage.append_audit("certificate_issued", row, db_path=db, details={
            "certificate": cert, "checks": checks, "process_notes": data["process_notes"].strip(),
        })
        return jsonify({"certificate": cert, "checks": checks, "label": label}), 201

    @app.get("/log")
    def log():
        raw = request.args.get("limit", "20")
        try:
            limit = int(raw)
        except ValueError:
            return error("invalid_field", "limit must be an integer.", 400)
        if not 1 <= limit <= 500:
            return error("invalid_field", "limit must be between 1 and 500.", 400)
        return jsonify({"entries": storage.get_log(limit, db)})

    @app.get("/stats")
    def stats():
        return jsonify(storage.stats(db))

    @app.get("/dashboard")
    def dashboard():
        recent = []
        for r in storage.recent_contents(15, db):
            recent.append({**r, "label": _label_for(r)})
        return render_template("dashboard.html", s=storage.stats(db), recent=recent,
                               generated_at=storage.now_iso())

    return app


# Detection functions are looked up as module globals at call time, so tests can monkeypatch
# `app.analyze_text` / `app.analyze_image` / `app.llm_available` on this module.
app = create_app()

if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 5000)), debug=False)
