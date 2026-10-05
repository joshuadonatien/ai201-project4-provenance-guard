from labels import LABEL_VARIANTS, build_label, confidence_text


def test_attribution_variants_verbatim():
    ai = build_label("likely_ai", 0.95)
    assert ai["variant"] == "high_confidence_ai"
    assert ai["headline"] == "Likely AI-generated"
    assert ai["body"] == ("Our automated checks found strong signs that this piece was mostly or entirely generated "
                          "by an AI tool. Automated checks can be wrong, and the creator can appeal this label.")
    unc = build_label("uncertain", 0.6)
    assert unc["headline"] == "Origin unclear"
    assert unc["body"].endswith("It is not an accusation.")
    hum = build_label("likely_human", 0.8)
    assert hum["headline"] == "Likely human-written"


def test_only_ai_label_mentions_appeal():
    assert "appeal" in build_label("likely_ai", 0.9)["body"]
    assert "appeal" not in build_label("uncertain", 0.6)["body"]
    assert "appeal" not in build_label("likely_human", 0.9)["body"]


def test_precedence():
    cert = {"certificate_id": "PG-ABCDEF12", "issued_at": "2026-10-05T14:32:10.123Z"}
    assert build_label("likely_ai", 0.9, "under_review")["variant"] == "under_review"
    assert build_label("likely_ai", 0.9, "resolved_overturned")["variant"] == "high_confidence_human"
    assert build_label("likely_ai", 0.9, "resolved_upheld")["variant"] == "high_confidence_ai"
    v = build_label("uncertain", 0.6, "under_review", cert)
    assert v["variant"] == "verified_human"
    assert v["headline"] == "Verified human: creator shared their drafts"
    assert v["body"].endswith("Certificate PG-ABCDEF12, issued 2026-10-05.")


def test_confidence_text_boundaries():
    assert confidence_text(0.90) == "Our confidence: high"
    assert confidence_text(0.8999) == "Our confidence: moderate"
    assert confidence_text(0.75) == "Our confidence: moderate"
    assert confidence_text(0.7499) == "Our confidence: low"
    assert confidence_text(0.5) == "Our confidence: low"


def test_label_variants_exported():
    assert set(LABEL_VARIANTS) == {"high_confidence_ai", "uncertain", "high_confidence_human",
                                   "under_review", "verified_human"}
