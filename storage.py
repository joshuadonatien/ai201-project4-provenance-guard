"""SQLite storage for Provenance Guard (planning.md §8).

Two tables:
- ``contents``: current state of each piece, one row per content_id (updated in place).
- ``audit_log``: append-only event log. Rows are only ever INSERTed, never UPDATEd or DELETEd.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

DEFAULT_DB = "provenance.db"

# Columns stored as JSON text in `contents`.
_CONTENT_JSON = ("metadata", "signals", "adjustments", "certificate")
# Columns stored as JSON text in `audit_log`.
_AUDIT_JSON = ("signals_used", "details")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contents (
    content_id           TEXT PRIMARY KEY,
    text                 TEXT,
    title                TEXT,
    content_type         TEXT NOT NULL DEFAULT 'text',
    metadata             TEXT,
    creator_id           TEXT NOT NULL,
    attribution          TEXT NOT NULL,
    original_attribution TEXT NOT NULL,
    ai_score             REAL NOT NULL,
    confidence           REAL NOT NULL,
    signals              TEXT,
    adjustments          TEXT,
    label_variant        TEXT,
    status               TEXT NOT NULL DEFAULT 'classified',
    appeal_id            TEXT,
    appeal_reasoning     TEXT,
    appeal_filed_at      TEXT,
    reviewer_decision    TEXT,
    reviewer_note        TEXT,
    resolved_at          TEXT,
    certificate          TEXT,
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp         TEXT NOT NULL,
    event_type        TEXT NOT NULL,
    content_id        TEXT,
    creator_id        TEXT,
    content_type      TEXT,
    attribution       TEXT,
    ai_score          REAL,
    confidence        REAL,
    llm_score         REAL,
    stylometric_score REAL,
    lexical_score     REAL,
    signals_used      TEXT,
    status            TEXT,
    label_variant     TEXT,
    appeal_reasoning  TEXT,
    details           TEXT
);
"""


def now_iso():
    """ISO-8601 UTC timestamp with milliseconds and a trailing Z, e.g. 2026-10-05T14:32:10.123Z.

    Reads the clock once so the seconds and milliseconds parts always come from the same instant.
    """
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


_timestamp = now_iso


def resolve_db_path(db_path=None):
    return db_path or os.environ.get("PROVENANCE_DB") or DEFAULT_DB


def _connect(db_path):
    conn = sqlite3.connect(resolve_db_path(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path=None):
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)
    return resolve_db_path(db_path)


def _decode(row, json_cols):
    if row is None:
        return None
    d = dict(row)
    for col in json_cols:
        if d.get(col) is not None:
            try:
                d[col] = json.loads(d[col])
            except (TypeError, ValueError):
                pass
    return d


def _encode(values, json_cols):
    out = dict(values)
    for col in json_cols:
        if col in out and out[col] is not None and not isinstance(out[col], str):
            out[col] = json.dumps(out[col])
    return out


# ---------------------------------------------------------------- contents

def save_content(record, db_path=None):
    """Insert a new contents row. `record` is a dict keyed by column name."""
    rec = _encode(record, _CONTENT_JSON)
    rec.setdefault("created_at", _timestamp())
    rec.setdefault("original_attribution", rec.get("attribution"))
    cols = ", ".join(rec.keys())
    marks = ", ".join("?" for _ in rec)
    with _connect(db_path) as conn:
        conn.execute(f"INSERT INTO contents ({cols}) VALUES ({marks})", list(rec.values()))
    return get_content(record["content_id"], db_path)


def get_content(content_id, db_path=None):
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM contents WHERE content_id = ?", (content_id,)).fetchone()
    return _decode(row, _CONTENT_JSON)


def update_content(content_id, db_path=None, **fields):
    """Update current-state columns for one piece and return the new row."""
    if fields:
        enc = _encode(fields, _CONTENT_JSON)
        sets = ", ".join(f"{k} = ?" for k in enc)
        with _connect(db_path) as conn:
            conn.execute(f"UPDATE contents SET {sets} WHERE content_id = ?",
                         list(enc.values()) + [content_id])
    return get_content(content_id, db_path)


def list_appeals(status="open", db_path=None):
    """Appeals oldest-first (FIFO). status: open | resolved | all."""
    if status == "open":
        where = "status = 'under_review'"
    elif status == "resolved":
        where = "status IN ('resolved_upheld', 'resolved_overturned')"
    else:
        where = "appeal_filed_at IS NOT NULL"
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM contents WHERE appeal_filed_at IS NOT NULL AND {where} "
            "ORDER BY appeal_filed_at ASC").fetchall()
    return [_decode(r, _CONTENT_JSON) for r in rows]


def recent_contents(limit=10, db_path=None):
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM contents ORDER BY created_at DESC, rowid DESC LIMIT ?",
                            (limit,)).fetchall()
    return [_decode(r, _CONTENT_JSON) for r in rows]


# ---------------------------------------------------------------- audit log

def _signal_score(signals, name):
    sig = (signals or {}).get(name) or {}
    return sig.get("score") if sig.get("available") else None


def append_audit(event_type, content_row, db_path=None, **extra):
    """Append one event to the audit log. Fields default from `content_row`; `extra` overrides them."""
    row = content_row or {}
    signals = row.get("signals") or {}
    if isinstance(signals, str):
        signals = json.loads(signals)
    entry = {
        "timestamp": _timestamp(),
        "event_type": event_type,
        "content_id": row.get("content_id"),
        "creator_id": row.get("creator_id"),
        "content_type": row.get("content_type"),
        "attribution": row.get("attribution"),
        "ai_score": row.get("ai_score"),
        "confidence": row.get("confidence"),
        "llm_score": _signal_score(signals, "llm"),
        "stylometric_score": _signal_score(signals, "stylometric"),
        "lexical_score": _signal_score(signals, "lexical"),
        "signals_used": [n for n, s in signals.items() if (s or {}).get("available")],
        "status": row.get("status"),
        "label_variant": row.get("label_variant"),
        "appeal_reasoning": row.get("appeal_reasoning"),
        "details": {},
    }
    entry.update(extra)
    enc = _encode(entry, _AUDIT_JSON)
    cols = ", ".join(enc.keys())
    marks = ", ".join("?" for _ in enc)
    with _connect(db_path) as conn:
        cur = conn.execute(f"INSERT INTO audit_log ({cols}) VALUES ({marks})", list(enc.values()))
        entry["id"] = cur.lastrowid
    return entry


def get_log(limit=20, db_path=None):
    """Newest-first audit entries with JSON columns decoded."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_decode(r, _AUDIT_JSON) for r in rows]


# ---------------------------------------------------------------- analytics (§9.3)

def _pct(num, den):
    return round(100.0 * num / den, 1) if den else 0.0


def stats(db_path=None):
    with _connect(db_path) as conn:
        rows = [_decode(r, _CONTENT_JSON) for r in conn.execute("SELECT * FROM contents").fetchall()]
        appeals_filed = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE event_type = 'appeal_filed'").fetchone()[0]
        certs_issued = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE event_type = 'certificate_issued'").fetchone()[0]
        certs_denied = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE event_type = 'certificate_denied'").fetchone()[0]
        resolved = conn.execute(
            "SELECT details FROM audit_log WHERE event_type = 'appeal_resolved'").fetchall()

    total = len(rows)
    # Detection pattern counts the *automated* decision, not post-appeal state.
    pattern = {}
    for a in ("likely_ai", "uncertain", "likely_human"):
        n = sum(1 for r in rows if r["original_attribution"] == a)
        pattern[a] = {"count": n, "percent": _pct(n, total)}

    decisions = [json.loads(r[0] or "{}").get("decision") for r in resolved]
    n_resolved = len(decisions)
    n_overturned = sum(1 for d in decisions if d == "overturned")
    n_upheld = sum(1 for d in decisions if d == "upheld")
    open_appeals = sum(1 for r in rows if r["status"] == "under_review")

    disagree = sum(1 for r in rows if "signal_disagreement" in (r.get("adjustments") or []))

    by_type = {}
    for r in rows:
        by_type[r["content_type"]] = by_type.get(r["content_type"], 0) + 1

    avg_conf = round(sum(r["confidence"] for r in rows) / total, 3) if total else None

    return {
        "total_submissions": total,
        "detection_pattern": pattern,
        "appeals": {
            "filed": appeals_filed,
            "open": open_appeals,
            "resolved": n_resolved,
            "upheld": n_upheld,
            "overturned": n_overturned,
            "appeal_rate_percent": _pct(appeals_filed, total),
            "overturn_rate_percent": _pct(n_overturned, n_resolved),
        },
        "signal_disagreement": {"count": disagree, "rate_percent": _pct(disagree, total)},
        "average_confidence": avg_conf,
        "by_content_type": by_type,
        "certificates": {"issued": certs_issued, "denied": certs_denied},
    }
