import re

from certificate import evaluate_certificate, new_certificate_id

FINAL = ("The lighthouse keeper counted ships the way other people count sheep. Every night he wrote their names "
         "in a ledger his grandfather had started, and every morning he crossed out the ones that never came back. "
         "His daughter thought it was morbid. He thought it was the only honest record anyone kept.")
DRAFT = ("The old lighthouse keeper counted ships like people count sheep. Each night he wrote names in a ledger "
         "that his father started, and in the morning he crossed out the ones that did not return. His kid "
         "found it grim. To him it was the one honest record on the whole coast, and he kept it anyway.")
UNRELATED = ("Quarterly revenue grew across all segments, driven by strong subscription renewals and lower churn. "
             "Operating margin expanded two points year over year while headcount stayed flat. Guidance for the "
             "next fiscal year assumes moderate growth in international markets and stable pricing overall today.")
NOTES = "Wrote the first draft in March in a notebook, then revised it twice after my workshop group read it."


def row(attribution="uncertain", creator="writer"):
    return {"content_id": "c1", "creator_id": creator, "attribution": attribution, "text": FINAL}


def failed(checks):
    return {c["check"] for c in checks if not c["passed"]}


def test_certificate_id_format():
    assert re.fullmatch(r"PG-[0-9A-F]{8}", new_certificate_id())


def test_valid_draft_passes():
    ok, checks, cert = evaluate_certificate(row(), "writer", DRAFT, NOTES)
    assert ok, checks
    assert cert["method"] == "draft_history"
    assert 0.30 <= cert["draft_similarity"] <= 0.95
    assert cert["content_id"] == "c1" and cert["creator_id"] == "writer"
    assert cert["issued_at"].endswith("Z")


def test_identical_draft_fails_similarity():
    ok, checks, cert = evaluate_certificate(row(), "writer", FINAL, NOTES)
    assert not ok and cert is None
    assert failed(checks) == {"draft_similarity"}


def test_unrelated_draft_fails_similarity():
    ok, checks, _ = evaluate_certificate(row(), "writer", UNRELATED, NOTES)
    assert failed(checks) == {"draft_similarity"}


def test_all_other_checks():
    ok, checks, _ = evaluate_certificate(row(attribution="likely_ai"), "intruder", "short draft", "brief")
    assert failed(checks) == {"creator_matches", "not_flagged_ai", "draft_length", "process_notes_length",
                              "draft_similarity"}
    assert all(c["detail"] for c in checks)


def test_human_attribution_allowed():
    ok, _, _ = evaluate_certificate(row(attribution="likely_human"), "writer", DRAFT, NOTES)
    assert ok
