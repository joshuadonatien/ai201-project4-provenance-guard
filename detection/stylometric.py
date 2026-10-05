"""Signal 2 — stylometric heuristics (planning.md §3, Signal 2). Pure Python, no network."""

import re
import statistics

from .scoring import clamp, ramp

NAME = "stylometric"

# Ramp endpoints (spec §3): ramp(x, value_that_reads_human -> 0.0, value_that_reads_ai -> 1.0)
CV_HUMAN, CV_AI = 0.70, 0.20
WORDLEN_HUMAN, WORDLEN_AI = 4.2, 5.6
PUNCT_HUMAN, PUNCT_AI = 0.8, 0.0
SUB_WEIGHTS = {"burstiness": 0.45, "word_length": 0.35, "punctuation": 0.20}

MIN_SENTENCES = 3
MIN_WORDS = 40
MATTR_WINDOW = 50

WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)*")
# Sentence end: one or more terminal marks (incl. the single-char ellipsis), optionally followed by closing
# quotes/brackets, then whitespace or end of text.
SENT_SPLIT_RE = re.compile(r"(?<=[.?!…])[\"'”’)\]]*\s+")
TERMINAL_RE = re.compile(r"[.?!…][\"'”’)\]]*$")
ALLCAPS_RE = re.compile(r"\b[A-Z]{3,}\b")


def words(text):
    return WORD_RE.findall(text)


def _looks_like_verse(lines):
    """A block of >=3 short lines, most of which don't end in terminal punctuation, is treated as verse."""
    if len(lines) < 3:
        return False
    short = sum(1 for ln in lines if len(words(ln)) <= 12)
    unterminated = sum(1 for ln in lines if not TERMINAL_RE.search(ln.strip()))
    return short >= 0.7 * len(lines) and unterminated >= 0.5 * len(lines)


def split_sentences(text):
    """Return (sentences, mode).

    - Blank lines always end a sentence (paragraph / stanza break).
    - Inside a block, if it looks like verse (short lines, mostly no terminal punctuation) every line is a unit,
      otherwise single newlines are treated as spaces (hard-wrapped prose).
    - Then split on . ? ! … (runs like "?!" or "..." count once), keeping closing quotes with the sentence.
    """
    text = text.replace("\r\n", "\n").strip()
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    block_lines = [[ln.strip() for ln in b.split("\n") if ln.strip()] for b in blocks]
    # Decide "verse" for the whole document, not per block: a 2-line closing stanza is still verse.
    whole_doc_verse = _looks_like_verse([ln for lines in block_lines for ln in lines])
    units, verse_blocks = [], 0
    for lines in block_lines:
        if whole_doc_verse or _looks_like_verse(lines):
            verse_blocks += 1
            units.extend(lines)
        else:
            units.append(" ".join(lines))
    sentences = []
    for unit in units:
        for s in SENT_SPLIT_RE.split(unit):
            if words(s):
                sentences.append(s.strip())
    mode = "verse_lines" if verse_blocks else "prose"
    return sentences, mode


def mattr(tokens, window=MATTR_WINDOW):
    """Moving-average type-token ratio. Falls back to plain TTR for texts shorter than the window."""
    if not tokens:
        return 0.0
    tokens = [t.lower() for t in tokens]
    if len(tokens) <= window:
        return len(set(tokens)) / len(tokens)
    ratios = [len(set(tokens[i:i + window])) / window for i in range(len(tokens) - window + 1)]
    return sum(ratios) / len(ratios)


def informal_punctuation_count(text):
    """Count 'human' punctuation marks: ? ! … — – ( quotes, ellipses, ALL-CAPS words, spaced hyphen dashes."""
    t = text
    ellipses = len(re.findall(r"\.{3,}|…", t))
    t_no_ell = re.sub(r"\.{3,}|…", " ", t)
    counts = {
        "question": len(re.findall(r"\?+", t_no_ell)),
        "exclamation": len(re.findall(r"!+", t_no_ell)),
        "ellipsis": ellipses,
        "dash": len(re.findall(r"[—–]|\s-{1,2}\s", t_no_ell)),
        "parenthesis": t_no_ell.count("("),
        "quote_pairs": (t_no_ell.count('"') + t_no_ell.count("“") + t_no_ell.count("”")) // 2,
        "allcaps_words": len(ALLCAPS_RE.findall(t_no_ell)),
    }
    return sum(counts.values()), counts


def stylometric_signal(text):
    text = text or ""
    tokens = words(text)
    sentences, mode = split_sentences(text)
    n_words, n_sent = len(tokens), len(sentences)
    base_details = {"word_count": n_words, "sentence_count": n_sent, "segmentation": mode}

    if n_sent < MIN_SENTENCES or n_words < MIN_WORDS:
        base_details["reason"] = (
            f"needs >= {MIN_SENTENCES} sentences and >= {MIN_WORDS} words "
            f"(got {n_sent} sentences, {n_words} words)"
        )
        return {"name": NAME, "score": None, "available": False, "details": base_details}

    lengths = [len(words(s)) for s in sentences]
    mean_len = statistics.mean(lengths)
    cv = statistics.pstdev(lengths) / mean_len if mean_len else 0.0
    avg_word_len = sum(len(re.sub(r"['’]", "", w)) for w in tokens) / n_words
    punct_total, punct_breakdown = informal_punctuation_count(text)
    punct_per_sentence = punct_total / n_sent

    sub = {
        "burstiness": ramp(cv, CV_HUMAN, CV_AI),
        "word_length": ramp(avg_word_len, WORDLEN_HUMAN, WORDLEN_AI),
        "punctuation": ramp(punct_per_sentence, PUNCT_HUMAN, PUNCT_AI),
    }
    # In verse, line length is set by metre/form, not by the writer's habits, so a perfectly regular poem would
    # read as "metronomic AI". Burstiness is excluded for verse and the other two sub-weights renormalised.
    used = {k: w for k, w in SUB_WEIGHTS.items() if not (mode == "verse_lines" and k == "burstiness")}
    score = clamp(sum(w * sub[k] for k, w in used.items()) / sum(used.values()))

    details = dict(base_details)
    details.update({
        "sentence_lengths": lengths,
        "mean_sentence_length": round(mean_len, 2),
        "sentence_length_cv": round(cv, 3),
        "avg_word_length": round(avg_word_len, 3),
        "mattr": round(mattr(tokens), 3),
        "informal_punct_per_sentence": round(punct_per_sentence, 3),
        "informal_punct_breakdown": punct_breakdown,
        "sub_scores": {k: round(v, 3) for k, v in sub.items()},
        "sub_weights": {k: round(w / sum(used.values()), 3) for k, w in used.items()},
        "burstiness_excluded": mode == "verse_lines",
    })
    return {"name": NAME, "score": round(score, 3), "available": True, "details": details}
