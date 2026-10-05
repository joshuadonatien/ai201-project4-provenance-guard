"""Provenance certificate: "Verified human: creator shared their drafts" (planning.md §9.2)."""

import difflib
import re
import secrets

from storage import now_iso

MIN_DRAFT_WORDS = 40
MIN_NOTES_CHARS = 40
MIN_SIMILARITY = 0.30
MAX_SIMILARITY = 0.95


def new_certificate_id():
    return "PG-" + secrets.token_hex(4).upper()


def _words(text):
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def draft_similarity(draft_text, final_text):
    """SequenceMatcher ratio over lowercase word tokens.

    Word-level, not character-level: on characters, two unrelated English paragraphs already score ~0.30
    (shared letters and spaces), which sits right on the lower bound and lets unrelated text pass.
    """
    return difflib.SequenceMatcher(None, _words(draft_text), _words(final_text), autojunk=False).ratio()


def evaluate_certificate(content_row, creator_id, draft_text, process_notes):
    """Run every §9.2 check (no short-circuit, so the creator sees all failures).

    Returns (ok, checks, certificate_or_None); each check is {"check", "passed", "detail"}.
    """
    draft_text = draft_text or ""
    process_notes = process_notes or ""
    checks = []

    def add(name, passed, detail):
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    add("creator_matches", creator_id == content_row.get("creator_id"),
        "Caller is the creator of this piece." if creator_id == content_row.get("creator_id")
        else "Only the creator of this piece can request a certificate.")

    attribution = content_row.get("attribution")
    add("not_flagged_ai", attribution != "likely_ai",
        f"Current attribution is '{attribution}'." if attribution != "likely_ai"
        else "This piece is currently labelled likely AI-generated; file an appeal first.")

    words = len(draft_text.split())
    add("draft_length", words >= MIN_DRAFT_WORDS,
        f"Draft has {words} words (minimum {MIN_DRAFT_WORDS}).")

    notes_len = len(process_notes.strip())
    add("process_notes_length", notes_len >= MIN_NOTES_CHARS,
        f"Process notes have {notes_len} characters (minimum {MIN_NOTES_CHARS}).")

    sim = round(draft_similarity(draft_text, content_row.get("text") or ""), 3)
    in_range = MIN_SIMILARITY <= sim <= MAX_SIMILARITY
    if sim > MAX_SIMILARITY:
        why = "too similar to the final piece to show real revision"
    elif sim < MIN_SIMILARITY:
        why = "too different from the final piece to be an earlier draft of it"
    else:
        why = "consistent with an earlier draft of this piece"
    add("draft_similarity", in_range,
        f"Similarity {sim} ({why}); must be between {MIN_SIMILARITY} and {MAX_SIMILARITY}.")

    ok = all(c["passed"] for c in checks)
    cert = None
    if ok:
        cert = {
            "certificate_id": new_certificate_id(),
            "content_id": content_row.get("content_id"),
            "creator_id": creator_id,
            "issued_at": now_iso(),
            "method": "draft_history",
            "draft_similarity": sim,
        }
    return ok, checks, cert
