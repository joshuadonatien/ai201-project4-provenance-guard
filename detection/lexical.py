"""Signal 3 — lexical markers (planning.md §3, Signal 3). Pure Python, no network.

score = clamp(0.5 + 0.18 * ai_hits_per_100w - 0.12 * human_hits_per_100w, 0, 1)
"""

import re

from .scoring import clamp

NAME = "lexical"
AI_COEF = 0.18
HUMAN_COEF = 0.12

# Stock phrases over-represented in instruction-tuned LLM output. Matched case-insensitively on word boundaries;
# longer phrases are matched first and their spans can't be re-counted by shorter ones.
AI_PHRASES = [
    "it is important to note", "it's important to note", "it is worth noting", "it's worth noting",
    "it is essential to", "it is equally essential", "it is crucial to", "it is imperative",
    "plays a crucial role", "plays a vital role", "plays a pivotal role", "a pivotal role",
    "in today's fast-paced world", "in today's digital age", "in today's world", "in the modern era",
    "in conclusion", "in summary", "to summarize", "overall,", "ultimately,",
    "furthermore", "moreover", "additionally", "consequently", "nevertheless",
    "delve", "delves", "delving", "tapestry", "testament to", "a rich tapestry",
    "paradigm shift", "transformative", "stakeholders", "multifaceted", "nuanced",
    "navigate the complexities", "navigating the complexities", "the complexities of",
    "ever-evolving", "ever-changing landscape", "landscape of", "realm of", "in the realm",
    "foster", "fostering", "leverage", "leveraging", "harness the power", "unlock the potential",
    "seamless", "seamlessly", "robust", "holistic", "streamline", "synergy",
    "ethical implications", "ethical considerations", "responsible deployment",
    "across various sectors", "various sectors", "a wide range of", "a myriad of", "myriad",
    "numerous benefits", "are numerous", "underscores", "underscore the importance",
    "embark on a journey", "a journey of", "serves as a", "stands as a",
    "not only", "but also", "on the other hand", "it can be argued",
    "studies show", "research suggests", "in essence", "crucial", "vital", "pivotal",
    "ensure that", "to ensure", "significant impact", "valuable insights", "key takeaways",
]

# Informal / personal-voice markers typical of human casual writing.
HUMAN_MARKERS = [
    "lol", "lmao", "lmfao", "rofl", "omg", "tbh", "idk", "imo", "imho", "ngl", "smh", "btw", "fwiw",
    "kinda", "sorta", "gonna", "wanna", "gotta", "dunno", "y'all", "ya", "yeah", "yep", "nope", "nah",
    "ugh", "meh", "huh", "hmm", "oops", "whoa", "wow", "dude", "lowkey", "highkey",
    "honestly", "literally", "basically", "totally", "super", "pretty much",
    "ok so", "okay so", "so yeah", "anyway", "anyways", "i mean", "you know",
    "my friend", "my mom", "my dad", "my sister", "my brother", "my boss", "my roommate",
    "yesterday", "last night", "this morning", "today i",
]

# Contractions are a weaker human marker (assistant prose avoids them, but edited AI uses them);
# each one counts as half a hit.
CONTRACTION_RE = re.compile(
    r"\b[A-Za-z]+(?:n['’]t|['’](?:re|ve|ll|d|m))\b", re.IGNORECASE)
CONTRACTION_WEIGHT = 0.5
# Lower-case first-person "i" (as in "i finally tried") is a strong casual marker.
LOWER_I_RE = re.compile(r"(?<![A-Za-z'’])i(?![A-Za-z'’])")
WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)*")


def _phrase_regex(phrase):
    esc = re.escape(phrase).replace("'", "['’]")
    left = r"(?<![A-Za-z0-9])"
    right = r"" if not phrase[-1].isalnum() else r"(?![A-Za-z0-9])"
    return re.compile(left + esc + right, re.IGNORECASE)


_AI_PATTERNS = [(p, _phrase_regex(p)) for p in sorted(set(AI_PHRASES), key=len, reverse=True)]
_HUMAN_PATTERNS = [(p, _phrase_regex(p)) for p in sorted(set(HUMAN_MARKERS), key=len, reverse=True)]


def _match(patterns, text):
    taken = []  # spans already counted
    hits = []
    for phrase, rx in patterns:
        for m in rx.finditer(text):
            s, e = m.span()
            if any(s < te and ts < e for ts, te in taken):
                continue
            taken.append((s, e))
            hits.append(phrase)
    return hits


def lexical_signal(text):
    text = text or ""
    n_words = len(WORD_RE.findall(text))
    if n_words == 0:
        return {"name": NAME, "score": None, "available": False, "details": {"reason": "no words"}}

    ai_hits = _match(_AI_PATTERNS, text)
    human_hits = _match(_HUMAN_PATTERNS, text)
    contractions = [m.group(0) for m in CONTRACTION_RE.finditer(text)]
    lower_i = len(LOWER_I_RE.findall(text))

    ai_count = len(ai_hits)
    human_count = len(human_hits) + lower_i + CONTRACTION_WEIGHT * len(contractions)
    ai_rate = 100.0 * ai_count / n_words
    human_rate = 100.0 * human_count / n_words
    score = clamp(0.5 + AI_COEF * ai_rate - HUMAN_COEF * human_rate)

    return {
        "name": NAME,
        "score": round(score, 3),
        "available": True,
        "details": {
            "word_count": n_words,
            "ai_phrases": ai_hits,
            "human_markers": human_hits + (["lowercase 'i'"] * lower_i),
            "contractions": contractions,
            "ai_hits": ai_count,
            "human_hits": round(human_count, 2),
            "ai_hits_per_100w": round(ai_rate, 2),
            "human_hits_per_100w": round(human_rate, 2),
        },
    }
