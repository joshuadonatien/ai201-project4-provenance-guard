"""API tests for the platform layer. Detection is monkeypatched: no network, deterministic scores."""

import os
import tempfile

# Keep the module-level `app = create_app()` from creating provenance.db in the repo during tests.
os.environ.setdefault("PROVENANCE_DB", os.path.join(tempfile.mkdtemp(), "import.db"))

import pytest  # noqa: E402

import app as app_module  # noqa: E402

HUMAN_TEXT = ("honestly i wrote this on the bus this morning, kinda tired. my sister laughed at the first line "
              "and then cried at the last one, which is the whole point i guess.")
AI_TEXT = ("It is important to note that technology plays a crucial role in today's fast-paced world. "
           "Furthermore, stakeholders must delve into the rich tapestry of innovation.")


def make_result(ai_score, adjustments=None):
    attribution = "likely_ai" if ai_score >= 0.80 else ("likely_human" if ai_score <= 0.35 else "uncertain")
    return {
        "attribution": attribution,
        "ai_score": ai_score,
        "confidence": max(ai_score, 1 - ai_score),
        "signals": {
            "llm": {"score": ai_score, "available": True, "weight": 0.5,
                    "details": {"reasoning": "Fake reasoning sentence."}},
            "stylometric": {"score": ai_score, "available": True, "weight": 0.3,
                            "details": {"sentence_length_cv": 0.2}},
            "lexical": {"score": None, "available": False, "weight": 0.2,
                        "details": {"ai_phrases": ["it is important to note"]}},
        },
        "adjustments": adjustments or [],
        "word_count": 30,
    }


class FakeDetector:
    """Controllable stand-in for detection.analyze_text / analyze_image."""

    def __init__(self):
        self.score = 0.2
        self.adjustments = []
        self.image_calls = []

    def text(self, text):
        return make_result(self.score, self.adjustments)

    def image(self, metadata, description=None):
        self.image_calls.append((metadata, description))
        res = make_result(self.score, self.adjustments)
        res["signals"] = {
            "metadata": {"score": self.score, "available": True, "weight": 0.65, "details": {"matched": "x"}},
            "llm": {"score": self.score, "available": True, "weight": 0.35, "details": {"reasoning": "r"}},
        }
        return res


@pytest.fixture
def fake(monkeypatch):
    f = FakeDetector()
    monkeypatch.setattr(app_module, "analyze_text", f.text)
    monkeypatch.setattr(app_module, "analyze_image", f.image)
    monkeypatch.setattr(app_module, "llm_available", lambda: True)
    return f


@pytest.fixture
def client(tmp_path, fake):
    application = app_module.create_app(str(tmp_path / "test.db"))
    application.config["TESTING"] = True
    return application.test_client()


def submit(client, text=HUMAN_TEXT, creator="user-1", **kw):
    return client.post("/submit", json={"text": text, "creator_id": creator, **kw})


def appeal(client, cid, creator="user-1", reasoning="I wrote this myself over two weeks; I have my notebook drafts."):
    return client.post("/appeal", json={"content_id": cid, "creator_id": creator, "creator_reasoning": reasoning})


# ---------------------------------------------------------------- submit

def test_submit_returns_201_with_full_shape(client, fake):
    fake.score = 0.9
    r = submit(client, title="Poem")
    assert r.status_code == 201
    body = r.get_json()
    for key in ("content_id", "creator_id", "content_type", "attribution", "ai_score", "confidence", "status",
                "signals", "adjustments", "label", "timestamp"):
        assert key in body, key
    assert body["status"] == "classified"
    assert body["content_type"] == "text"
    assert body["creator_id"] == "user-1"
    assert set(body["label"]) == {"variant", "headline", "body", "confidence_text"}
    assert body["signals"]["llm"]["weight"] == 0.5
    assert body["timestamp"].endswith("Z") and len(body["timestamp"]) == 24


@pytest.mark.parametrize("score,attribution,variant,headline", [
    (0.95, "likely_ai", "high_confidence_ai", "Likely AI-generated"),
    (0.51, "uncertain", "uncertain", "Origin unclear"),
    (0.10, "likely_human", "high_confidence_human", "Likely human-written"),
])
def test_all_three_label_variants_reachable(client, fake, score, attribution, variant, headline):
    fake.score = score
    body = submit(client).get_json()
    assert body["attribution"] == attribution
    assert body["label"]["variant"] == variant
    assert body["label"]["headline"] == headline


@pytest.mark.parametrize("payload,code", [
    ({"creator_id": "u"}, "missing_field"),
    ({"text": HUMAN_TEXT}, "missing_field"),
    ({"text": "too short", "creator_id": "u"}, "text_too_short"),
    ({"text": "x" * 10_001, "creator_id": "u"}, "text_too_long"),
    ({"text": HUMAN_TEXT, "creator_id": "u", "content_type": "video"}, "invalid_field"),
    ({"creator_id": "u", "content_type": "image"}, "missing_field"),
])
def test_submit_validation_errors(client, payload, code):
    r = client.post("/submit", json=payload)
    assert r.status_code == 400
    assert r.get_json()["error"] == code
    assert r.get_json()["message"]


def test_submit_bad_json(client):
    r = client.post("/submit", data="{not json", content_type="application/json")
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_json"


def test_text_at_limits_accepted(client):
    assert submit(client, text="x" * 20).status_code == 201
    assert submit(client, text="x" * 10_000).status_code == 201


def test_image_submission_path(client, fake):
    fake.score = 0.97
    meta = {"software": "Midjourney v6", "width": 1024, "height": 1024}
    r = client.post("/submit", json={"creator_id": "artist", "content_type": "image", "metadata": meta,
                                     "description": "A neon city at dusk"})
    assert r.status_code == 201
    body = r.get_json()
    assert body["content_type"] == "image"
    assert body["attribution"] == "likely_ai"
    assert body["metadata"] == meta
    assert fake.image_calls == [(meta, "A neon city at dusk")]
    assert client.get("/stats").get_json()["by_content_type"] == {"image": 1}


def test_get_content_and_404(client):
    cid = submit(client).get_json()["content_id"]
    r = client.get(f"/content/{cid}")
    assert r.status_code == 200 and r.get_json()["content_id"] == cid
    r = client.get("/content/does-not-exist")
    assert r.status_code == 404 and r.get_json()["error"] == "not_found"


def test_unknown_route_and_wrong_method_are_json(client):
    r = client.get("/nope")
    assert r.status_code == 404 and r.get_json()["error"] == "not_found"
    r = client.get("/submit")
    assert r.status_code == 405 and r.get_json()["error"] == "method_not_allowed"


# ---------------------------------------------------------------- appeals

def test_appeal_flow_switches_label_and_logs(client, fake):
    fake.score = 0.92
    cid = submit(client).get_json()["content_id"]
    reasoning = "English is my second language and I was taught to write formally; this is my own essay."
    r = appeal(client, cid, reasoning=reasoning)
    assert r.status_code == 201
    body = r.get_json()
    assert body["status"] == "under_review"
    assert body["original_decision"]["attribution"] == "likely_ai"
    assert body["appeal_id"] and body["message"]

    content = client.get(f"/content/{cid}").get_json()
    assert content["status"] == "under_review"
    assert content["label"]["variant"] == "under_review"
    assert content["label"]["headline"] == "Label under review"

    entry = client.get("/log").get_json()["entries"][0]
    assert entry["event_type"] == "appeal_filed"
    assert entry["appeal_reasoning"] == reasoning
    assert entry["status"] == "under_review"
    assert entry["details"]["original_decision"]["attribution"] == "likely_ai"
    assert entry["details"]["original_decision"]["ai_score"] == 0.92
    assert entry["details"]["original_decision"]["signal_scores"]["llm"] == 0.92

    queue = client.get("/appeals").get_json()
    assert queue["count"] == 1
    item = queue["appeals"][0]
    assert item["content_id"] == cid
    assert item["creator_reasoning"] == reasoning
    assert item["llm_reasoning"] == "Fake reasoning sentence."
    assert item["stylometric_metrics"] == {"sentence_length_cv": 0.2}
    assert item["lexical_matches"]["ai_phrases"] == ["it is important to note"]
    assert len(item["excerpt"]) <= 303


def test_appeal_wrong_creator_403(client):
    cid = submit(client).get_json()["content_id"]
    r = appeal(client, cid, creator="someone-else")
    assert r.status_code == 403 and r.get_json()["error"] == "forbidden"


def test_appeal_double_409(client):
    cid = submit(client).get_json()["content_id"]
    assert appeal(client, cid).status_code == 201
    r = appeal(client, cid)
    assert r.status_code == 409 and r.get_json()["error"] == "conflict"


def test_appeal_unknown_content_404(client):
    assert appeal(client, "nope").status_code == 404


def test_appeal_short_reasoning_400(client):
    cid = submit(client).get_json()["content_id"]
    r = appeal(client, cid, reasoning="it's mine")
    assert r.status_code == 400 and r.get_json()["error"] == "reasoning_too_short"


def test_resolve_overturned_switches_to_human(client, fake):
    fake.score = 0.9
    cid = submit(client).get_json()["content_id"]
    appeal(client, cid)
    r = client.post(f"/appeals/{cid}/resolve", json={"decision": "overturned", "reviewer_note": "Drafts check out."})
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "resolved_overturned"
    assert body["attribution"] == "likely_human"
    assert body["original_attribution"] == "likely_ai"
    assert body["label"]["variant"] == "high_confidence_human"
    assert client.get("/appeals").get_json()["count"] == 0
    assert client.get("/appeals?status=resolved").get_json()["count"] == 1
    entry = client.get("/log").get_json()["entries"][0]
    assert entry["event_type"] == "appeal_resolved" and entry["details"]["decision"] == "overturned"
    # Resolving again: no open appeal.
    assert client.post(f"/appeals/{cid}/resolve", json={"decision": "upheld"}).status_code == 409


def test_resolve_upheld_restores_automated_label(client, fake):
    fake.score = 0.9
    cid = submit(client).get_json()["content_id"]
    appeal(client, cid)
    body = client.post(f"/appeals/{cid}/resolve", json={"decision": "upheld", "reviewer_note": "n"}).get_json()
    assert body["status"] == "resolved_upheld"
    assert body["label"]["variant"] == "high_confidence_ai"


def test_resolve_bad_decision_400(client):
    cid = submit(client).get_json()["content_id"]
    appeal(client, cid)
    assert client.post(f"/appeals/{cid}/resolve", json={"decision": "maybe"}).status_code == 400


# ---------------------------------------------------------------- rate limiting

def test_submit_rate_limit_11th_request_429(client):
    codes = [submit(client).status_code for _ in range(11)]
    assert codes[:10] == [201] * 10
    assert codes[10] == 429
    r = submit(client)
    body = r.get_json()
    assert body["error"] == "rate_limited"
    assert body["message"]
    assert body["limit"] == "10 per 1 minute"


def test_rate_limit_is_per_app(tmp_path, fake):
    """A fresh app starts with fresh counters (limiter is created inside the factory)."""
    a = app_module.create_app(str(tmp_path / "a.db")).test_client()
    for _ in range(10):
        submit(a)
    assert submit(a).status_code == 429
    b = app_module.create_app(str(tmp_path / "b.db")).test_client()
    assert submit(b).status_code == 201


# ---------------------------------------------------------------- log

def test_log_newest_first_with_decoded_json(client):
    ids = [submit(client).get_json()["content_id"] for _ in range(3)]
    appeal(client, ids[0])
    entries = client.get("/log?limit=20").get_json()["entries"]
    assert len(entries) >= 3
    assert entries[0]["event_type"] == "appeal_filed"
    assert [e["content_id"] for e in entries[1:4]] == ids[::-1]
    sub = entries[1]
    assert sub["event_type"] == "submission"
    assert sub["signals_used"] == ["llm", "stylometric"]
    assert sub["lexical_score"] is None and sub["llm_score"] == 0.2
    assert isinstance(sub["details"], dict)
    ids_desc = [e["id"] for e in entries]
    assert ids_desc == sorted(ids_desc, reverse=True)
    assert len(client.get("/log?limit=2").get_json()["entries"]) == 2
    assert client.get("/log?limit=abc").status_code == 400


# ---------------------------------------------------------------- certificate

FINAL = ("The lighthouse keeper counted ships the way other people count sheep. Every night he wrote their names "
         "in a ledger his grandfather had started, and every morning he crossed out the ones that never came back. "
         "His daughter thought it was morbid. He thought it was the only honest record anyone kept.")
DRAFT = ("The old lighthouse keeper counted ships like people count sheep. Each night he wrote names in a ledger "
         "that his father started, and in the morning he crossed out the ones that did not return. His kid "
         "found it grim. To him it was the one honest record on the whole coast, and he kept it anyway.")
NOTES = "Wrote the first draft in March in a notebook, then revised it twice after my workshop group read it."


def test_certificate_success(client, fake):
    fake.score = 0.5
    cid = submit(client, text=FINAL, creator="writer").get_json()["content_id"]
    r = client.post("/certificate", json={"content_id": cid, "creator_id": "writer",
                                          "draft_text": DRAFT, "process_notes": NOTES})
    assert r.status_code == 201, r.get_json()
    cert = r.get_json()["certificate"]
    assert cert["certificate_id"].startswith("PG-") and len(cert["certificate_id"]) == 11
    assert cert["method"] == "draft_history"
    content = client.get(f"/content/{cid}").get_json()
    assert content["label"]["variant"] == "verified_human"
    assert cert["certificate_id"] in content["label"]["body"]
    assert client.get("/log").get_json()["entries"][0]["event_type"] == "certificate_issued"
    assert client.get("/stats").get_json()["certificates"]["issued"] == 1
    # Second request: already certified.
    r = client.post("/certificate", json={"content_id": cid, "creator_id": "writer",
                                          "draft_text": DRAFT, "process_notes": NOTES})
    assert r.status_code == 409


def test_certificate_failure_identical_draft_and_ai_flag(client, fake):
    fake.score = 0.9
    cid = submit(client, text=FINAL, creator="writer").get_json()["content_id"]
    r = client.post("/certificate", json={"content_id": cid, "creator_id": "writer",
                                          "draft_text": FINAL, "process_notes": NOTES})
    assert r.status_code == 422
    body = r.get_json()
    assert body["error"] == "certificate_denied"
    failed = {c["check"] for c in body["checks"] if not c["passed"]}
    assert failed == {"not_flagged_ai", "draft_similarity"}
    entry = client.get("/log").get_json()["entries"][0]
    assert entry["event_type"] == "certificate_denied"
    assert client.get(f"/content/{cid}").get_json()["label"]["variant"] == "high_confidence_ai"


def test_certificate_missing_fields_and_404(client):
    assert client.post("/certificate", json={"content_id": "x"}).status_code == 400
    r = client.post("/certificate", json={"content_id": "nope", "creator_id": "w",
                                          "draft_text": DRAFT, "process_notes": NOTES})
    assert r.status_code == 404


# ---------------------------------------------------------------- stats / dashboard / health

def test_stats_numbers(client, fake):
    fake.score = 0.9
    a = submit(client).get_json()["content_id"]
    fake.score = 0.5
    fake.adjustments = ["signal_disagreement"]
    submit(client)
    fake.score = 0.1
    fake.adjustments = []
    submit(client)
    submit(client)
    appeal(client, a)
    client.post(f"/appeals/{a}/resolve", json={"decision": "overturned", "reviewer_note": "ok"})

    s = client.get("/stats").get_json()
    assert s["total_submissions"] == 4
    p = s["detection_pattern"]
    # Overturn doesn't rewrite history: the automated pattern still counts the AI call.
    assert (p["likely_ai"]["count"], p["uncertain"]["count"], p["likely_human"]["count"]) == (1, 1, 2)
    assert p["likely_human"]["percent"] == 50.0
    assert s["appeals"]["filed"] == 1 and s["appeals"]["appeal_rate_percent"] == 25.0
    assert s["appeals"]["overturn_rate_percent"] == 100.0
    assert s["signal_disagreement"] == {"count": 1, "rate_percent": 25.0}
    assert s["average_confidence"] == pytest.approx((0.9 + 0.5 + 0.9 + 0.9) / 4, abs=1e-3)
    assert s["by_content_type"] == {"text": 4}


def test_stats_empty(client):
    s = client.get("/stats").get_json()
    assert s["total_submissions"] == 0 and s["average_confidence"] is None


def test_dashboard_html(client, fake):
    submit(client, title="My Poem")
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert r.mimetype == "text/html"
    html = r.get_data(as_text=True)
    assert "Provenance Guard" in html and "Signal disagreement" in html and "My Poem" in html


def test_dashboard_html_empty(client):
    assert client.get("/dashboard").status_code == 200


def test_health_and_index(client):
    assert client.get("/health").get_json() == {"status": "ok", "llm_available": True}
    assert "POST /submit" in client.get("/").get_json()["endpoints"]
