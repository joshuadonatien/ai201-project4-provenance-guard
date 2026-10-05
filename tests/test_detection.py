"""Detection unit tests — no network (the LLM signal is monkeypatched)."""

import pytest

import detection
from detection import llm as llm_mod
from detection.image import metadata_signal
from detection.lexical import lexical_signal
from detection.scoring import (AI_THRESHOLD, HUMAN_THRESHOLD, TEXT_WEIGHTS, attribution_for,
                               combine_signals, ramp, shrink)
from detection.stylometric import stylometric_signal

AI_TEXT = ("Artificial intelligence represents a transformative paradigm shift in modern society. It is important "
           "to note that while the benefits of AI are numerous, it is equally essential to consider the ethical "
           "implications. Furthermore, stakeholders across various sectors must collaborate to ensure responsible "
           "deployment.")
HUMAN_TEXT = ("ok so i finally tried that new ramen place downtown and honestly? underwhelming. the broth was fine "
              "but they put WAY too much sodium in it and i was thirsty for like three hours after. my friend got "
              "the spicy version and said it was better. probably won't go back unless someone drags me there")


def sig(score, available=True):
    return {"score": score, "available": available, "details": {}}


# --- thresholds (planning.md §4) -------------------------------------------------------------------------------

@pytest.mark.parametrize("score,expected", [
    (0.80, "likely_ai"), (0.7999, "uncertain"), (0.51, "uncertain"),
    (0.3501, "uncertain"), (0.35, "likely_human"), (0.0, "likely_human"), (1.0, "likely_ai"),
])
def test_threshold_boundaries(score, expected):
    assert attribution_for(score) == expected


def test_thresholds_are_spec_values():
    assert AI_THRESHOLD == 0.80 and HUMAN_THRESHOLD == 0.35
    assert TEXT_WEIGHTS == {"llm": 0.50, "stylometric": 0.30, "lexical": 0.20}


def test_ramp_and_shrink():
    assert ramp(0.70, 0.70, 0.20) == 0.0
    assert ramp(0.20, 0.70, 0.20) == 1.0
    assert ramp(0.45, 0.70, 0.20) == pytest.approx(0.5)
    assert shrink(1.0, 0.35) == pytest.approx(0.825)


# --- combination + adjustments ----------------------------------------------------------------------------------

def test_weighted_mean_no_adjustments():
    r = combine_signals({"llm": sig(0.9), "stylometric": sig(0.8), "lexical": sig(0.7)}, TEXT_WEIGHTS, word_count=200)
    assert r["raw_score"] == pytest.approx(0.5 * 0.9 + 0.3 * 0.8 + 0.2 * 0.7)
    assert r["adjustments"] == []
    assert r["attribution"] == "likely_ai"
    assert r["confidence"] == pytest.approx(max(r["ai_score"], 1 - r["ai_score"]), abs=1e-3)


def test_one_loud_signal_cannot_decide_alone():
    r = combine_signals({"llm": sig(0.95), "stylometric": sig(0.5), "lexical": sig(0.5)}, TEXT_WEIGHTS, word_count=200)
    assert r["attribution"] == "uncertain"


def test_disagreement_shrinks_toward_half():
    r = combine_signals({"llm": sig(0.95), "stylometric": sig(0.3), "lexical": sig(0.9)}, TEXT_WEIGHTS, word_count=200)
    assert "signal_disagreement" in r["adjustments"]
    assert abs(r["ai_score"] - 0.5) < abs(r["raw_score"] - 0.5)


def test_short_text_shrink_graded():
    base = {"llm": sig(1.0), "stylometric": sig(1.0), "lexical": sig(1.0)}
    tiny = combine_signals(base, TEXT_WEIGHTS, word_count=10)
    short = combine_signals(base, TEXT_WEIGHTS, word_count=45)
    long_ = combine_signals(base, TEXT_WEIGHTS, word_count=100)
    assert "short_text" in tiny["adjustments"] and "short_text" in short["adjustments"]
    assert tiny["ai_score"] == pytest.approx(0.80)        # 40% shrink caps at the AI threshold
    assert short["ai_score"] == pytest.approx(0.925)      # 15% shrink
    assert long_["ai_score"] == 1.0 and long_["adjustments"] == []


def test_unavailable_signal_renormalises_weights():
    r = combine_signals({"llm": sig(None, False), "stylometric": sig(0.8), "lexical": sig(0.6)},
                        TEXT_WEIGHTS, word_count=200)
    assert r["signals"]["llm"]["weight"] == 0.0
    assert r["signals"]["stylometric"]["weight"] == pytest.approx(0.6)
    assert r["raw_score"] == pytest.approx(0.6 * 0.8 + 0.4 * 0.6)


def test_single_signal_shrink_and_no_signals():
    one = combine_signals({"llm": sig(1.0), "stylometric": sig(None, False), "lexical": sig(None, False)},
                          TEXT_WEIGHTS, word_count=200)
    assert "single_signal" in one["adjustments"] and one["ai_score"] == pytest.approx(0.75)
    none = combine_signals({"llm": sig(None, False)}, TEXT_WEIGHTS, word_count=200)
    assert none["attribution"] == "uncertain" and "no_signals" in none["adjustments"]


# --- individual signals ------------------------------------------------------------------------------------------

def test_stylometric_unavailable_on_short_text():
    assert stylometric_signal("Too short. Really.")["available"] is False


def test_stylometric_separates_uniform_from_bursty():
    ai = stylometric_signal(AI_TEXT)
    human = stylometric_signal(HUMAN_TEXT)
    assert ai["available"] and human["available"]
    assert ai["score"] > human["score"]


def test_lexical_catches_stock_phrases_and_casual_markers():
    ai = lexical_signal(AI_TEXT)
    human = lexical_signal(HUMAN_TEXT)
    assert "it is important to note" in ai["details"]["ai_phrases"]
    assert ai["score"] > 0.9
    assert human["score"] < 0.2


def test_metadata_signal_cases():
    assert metadata_signal({"software": "Midjourney v6"})["score"] >= 0.95
    assert metadata_signal({"digital_source_type": "trainedAlgorithmicMedia"})["score"] == pytest.approx(0.97)
    cam = metadata_signal({"make": "Canon", "model": "EOS R6", "exposure_time": "1/250", "iso": 400})
    assert cam["score"] == pytest.approx(0.15)
    assert metadata_signal({})["score"] == 0.5


# --- full pipeline with the LLM faked ------------------------------------------------------------------------------

def test_analyze_text_end_to_end_with_fake_llm(monkeypatch):
    monkeypatch.setattr(llm_mod, "llm_signal", lambda text: {"name": "llm", "score": 0.85, "available": True,
                                                              "details": {"reasoning": "fake"}})
    ai = detection.analyze_text(AI_TEXT)
    monkeypatch.setattr(llm_mod, "llm_signal", lambda text: {"name": "llm", "score": 0.1, "available": True,
                                                              "details": {"reasoning": "fake"}})
    human = detection.analyze_text(HUMAN_TEXT)
    assert ai["attribution"] == "likely_ai"
    assert human["attribution"] == "likely_human"
    assert set(ai["signals"]) == {"llm", "stylometric", "lexical"}


def test_analyze_text_survives_llm_outage(monkeypatch):
    monkeypatch.setattr(llm_mod, "llm_signal", lambda text: {"name": "llm", "score": None, "available": False,
                                                              "details": {"error": "down"}})
    r = detection.analyze_text(AI_TEXT)
    assert r["signals"]["llm"]["available"] is False
    assert r["attribution"] in {"likely_ai", "uncertain", "likely_human"}
