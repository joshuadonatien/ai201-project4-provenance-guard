"""Detection package: three-signal text ensemble and two-signal image ensemble (planning.md §3, §4, §9.1, §9.4).

analyze_text(text) / analyze_image(metadata, description) both return:
{
  "attribution": "likely_ai" | "uncertain" | "likely_human",
  "ai_score": float,          # 0..1, AI-likelihood after adjustments
  "confidence": float,        # max(ai_score, 1-ai_score)
  "signals": {name: {"score": float|None, "available": bool, "weight": float, "details": dict}},
  "adjustments": [str],       # e.g. ["signal_disagreement", "short_text"]
  "word_count": int,
}
(plus "raw_score": the weighted mean before adjustments.)

Text signal names: "llm", "stylometric", "lexical". Image signal names: "metadata", "llm".
"""

from . import llm as _llm
from .image import metadata_signal
from .lexical import lexical_signal
from .scoring import (AI_THRESHOLD, HUMAN_THRESHOLD, IMAGE_WEIGHTS, TEXT_WEIGHTS, attribution_for,
                      combine_signals, ramp)
from .stylometric import stylometric_signal, words

__all__ = ["analyze_text", "analyze_image", "llm_available", "AI_THRESHOLD", "HUMAN_THRESHOLD",
           "TEXT_WEIGHTS", "IMAGE_WEIGHTS", "attribution_for", "combine_signals", "ramp"]


def _safe(fn, name, *args):
    """A signal must never take the request down."""
    try:
        return fn(*args)
    except Exception as exc:  # defensive: signals are written not to raise
        return {"name": name, "score": None, "available": False,
                "details": {"error": f"{type(exc).__name__}: {exc}"}}


def analyze_text(text):
    text = text or ""
    word_count = len(words(text))
    signals = {
        "llm": _safe(_llm.llm_signal, "llm", text),
        "stylometric": _safe(stylometric_signal, "stylometric", text),
        "lexical": _safe(lexical_signal, "lexical", text),
    }
    result = combine_signals(signals, TEXT_WEIGHTS, word_count=word_count)
    result["word_count"] = word_count
    return result


def analyze_image(metadata, description=None):
    signals = {
        "metadata": _safe(metadata_signal, "metadata", metadata),
        "llm": _safe(_llm.llm_image_signal, "llm", metadata, description),
    }
    # The short-text rule is about prose length; it doesn't apply to image evidence, so word_count isn't passed.
    result = combine_signals(signals, IMAGE_WEIGHTS, word_count=None)
    result["word_count"] = len(words(description or ""))
    return result


def llm_available():
    return _llm.llm_available()
