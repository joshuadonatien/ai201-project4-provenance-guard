"""Signal combination, uncertainty adjustments and thresholds (planning.md §3 "How signals combine", §4, §9.1)."""

AI_THRESHOLD = 0.80      # ai_score >= this -> likely_ai
HUMAN_THRESHOLD = 0.35   # ai_score <= this -> likely_human

TEXT_WEIGHTS = {"llm": 0.50, "stylometric": 0.30, "lexical": 0.20}
IMAGE_WEIGHTS = {"metadata": 0.65, "llm": 0.35}

DISAGREEMENT_SPREAD = 0.45   # max - min > this -> shrink
DISAGREEMENT_SHRINK = 0.35   # pull 35% toward 0.5

# Short-text rule. DEVIATION from spec §4 ("< 60 words -> shrink 40%"), see docs/calibration.md:
# with a flat 40% shrink the best possible score under 60 words is 0.5 + 0.5*0.6 = 0.80, so the handout's
# 43-word clear-AI sample (raw 0.877) could never be labelled likely_ai (it scored 0.726). Graded instead:
#   < 25 words (captions, haiku): 40%  -> still mathematically capped at 0.80, keeps §7 edge case 5 "uncertain"
#   25-59 words:                  15%  -> needs raw >= 0.853 to reach 0.80, i.e. all signals must agree strongly;
#                                         one loud signal (LLM 0.95 + two neutral 0.5 -> raw 0.725) still can't.
SHORT_TEXT_RULES = [         # (word_count strictly below, shrink); first match wins
    (25, 0.40),
    (60, 0.15),
]
SINGLE_SIGNAL_SHRINK = 0.50


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def ramp(x, zero_at, one_at):
    """Linear interpolation: x == zero_at -> 0.0, x == one_at -> 1.0, clamped to [0,1].

    Works in either direction (zero_at may be larger than one_at)."""
    if zero_at == one_at:
        return 1.0 if x >= one_at else 0.0
    return clamp((x - zero_at) / (one_at - zero_at))


def shrink(score, fraction):
    """Pull a score toward 0.5 by `fraction` (0.35 -> keep 65% of the distance from 0.5)."""
    return 0.5 + (score - 0.5) * (1.0 - fraction)


def attribution_for(score):
    if score >= AI_THRESHOLD:
        return "likely_ai"
    if score <= HUMAN_THRESHOLD:
        return "likely_human"
    return "uncertain"


def short_text_shrink(word_count):
    if word_count is None:
        return 0.0
    for limit, amount in SHORT_TEXT_RULES:
        if word_count < limit:
            return amount
    return 0.0


def combine_signals(signals, weights, word_count=None):
    """Combine signal dicts {name: {"score", "available", "details", ...}} into a decision.

    Weights are renormalised over available signals. Adjustments (in order): signal_disagreement,
    short_text, single_signal. Each signal in the output gets "weight" = its effective weight (0 if unavailable).
    """
    out_signals = {}
    available = {}
    for name, sig in signals.items():
        sig = dict(sig)
        ok = bool(sig.get("available")) and sig.get("score") is not None and weights.get(name, 0) > 0
        sig["available"] = ok
        if not ok:
            sig["score"] = None
        out_signals[name] = sig
        if ok:
            available[name] = clamp(float(sig["score"]))

    total_w = sum(weights[n] for n in available)
    adjustments = []

    if not available or total_w <= 0:
        for sig in out_signals.values():
            sig["weight"] = 0.0
        adjustments.append("no_signals")
        score = 0.5
        return {
            "attribution": attribution_for(score),
            "ai_score": score,
            "confidence": 0.5,
            "signals": out_signals,
            "adjustments": adjustments,
            "raw_score": None,
        }

    for name, sig in out_signals.items():
        sig["weight"] = round(weights[name] / total_w, 3) if name in available else 0.0
        if sig["score"] is not None:
            sig["score"] = round(sig["score"], 3)

    raw = sum(weights[n] * s for n, s in available.items()) / total_w
    score = raw

    if len(available) >= 2:
        spread = max(available.values()) - min(available.values())
        if spread > DISAGREEMENT_SPREAD:
            score = shrink(score, DISAGREEMENT_SHRINK)
            adjustments.append("signal_disagreement")

    st = short_text_shrink(word_count)
    if st > 0:
        score = shrink(score, st)
        adjustments.append("short_text")

    if len(available) == 1:
        score = shrink(score, SINGLE_SIGNAL_SHRINK)
        adjustments.append("single_signal")

    score = round(clamp(score), 3)
    return {
        "attribution": attribution_for(score),
        "ai_score": score,
        "confidence": round(max(score, 1 - score), 3),
        "signals": out_signals,
        "adjustments": adjustments,
        "raw_score": round(raw, 3),
    }
