"""Run the detection pipeline (real Groq call) on the 8 calibration texts and write docs/calibration.md.

Usage:  .venv/bin/python scripts/calibrate.py [--out docs/calibration.md]
"""

import argparse
import datetime as dt
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from detection import AI_THRESHOLD, HUMAN_THRESHOLD, analyze_text  # noqa: E402
from detection import scoring  # noqa: E402

# (id, description, expected band, text). Bands: "ai" (>= 0.80), "human" (<= 0.35), "uncertain" (0.35-0.80).
CASES = [
    ("H1", "Handout: clear AI", "ai",
     "Artificial intelligence represents a transformative paradigm shift in modern society. It is important to "
     "note that while the benefits of AI are numerous, it is equally essential to consider the ethical "
     "implications. Furthermore, stakeholders across various sectors must collaborate to ensure responsible "
     "deployment."),
    ("H2", "Handout: clear human", "human",
     "ok so i finally tried that new ramen place downtown and honestly? underwhelming. the broth was fine but "
     "they put WAY too much sodium in it and i was thirsty for like three hours after. my friend got the spicy "
     "version and said it was better. probably won't go back unless someone drags me there"),
    ("H3", "Handout: borderline formal human", "uncertain",
     "The relationship between monetary policy and asset price inflation has been extensively studied in the "
     "literature. Central banks face a fundamental tension between their mandate for price stability and the "
     "unintended consequences of prolonged low interest rates on equity and real estate valuations."),
    ("H4", "Handout: borderline edited AI", "uncertain",
     "I've been thinking a lot about remote work lately. There are genuine tradeoffs — flexibility and no "
     "commute on one side, isolation and blurred work-life boundaries on the other. Studies show productivity "
     "varies widely by individual and role type."),
    ("C1", "Own: short repetitive poem (human)", "uncertain",
     "the rain, the rain, it falls again\n"
     "on the roof and on the lane\n"
     "the rain, the rain, it falls again\n"
     "and I am waiting by the pane\n"
     "\n"
     "the cat is sleeping, so is Jane\n"
     "the rain, the rain, it falls again"),
    ("C2", "Own: ESL-style formal-but-human paragraph", "uncertain",
     "In my country the education system is very different from here. Furthermore, it is important to consider "
     "that students must respect the teacher in all situations. When I was student in Hanoi, we did not ask "
     "questions during the class, because this is considered impolite. Moreover, the exams decide everything "
     "about your future. In conclusion, I think both systems have advantages and disadvantages, but I prefer "
     "that here the students can discuss with the professor."),
    ("C3", "Own: AI text with injected slang", "uncertain",
     "honestly lol remote work is kinda a double-edged sword. On one hand, it offers unparalleled flexibility "
     "and eliminates the daily commute, allowing employees to achieve a healthier work-life balance. On the "
     "other hand, it can foster feelings of isolation and make collaboration more challenging. Ultimately, the "
     "effectiveness of remote work depends on individual preferences, organizational culture, and the "
     "availability of robust communication tools. tbh it's all about finding the right balance."),
    ("C4", "Own: personal diary entry (~100 words)", "human",
     "Tuesday. Woke up late again because the stupid alarm didn't go off (or I slept through it, who knows). "
     "Missed the 8:15 bus so I walked, which actually wasn't bad? The leaves on Maple St are doing that orange "
     "thing. Work was a blur. Priya brought in her mom's samosas and I ate three, no regrets. Called Dad on the "
     "way home - his knee is still bugging him but he won't see anyone about it, classic. Tonight: leftover "
     "pasta, two episodes of that baking show, bed by 11. Ha. We'll see."),
]


def in_band(score, band):
    if band == "ai":
        return score >= AI_THRESHOLD
    if band == "human":
        return score <= HUMAN_THRESHOLD
    return HUMAN_THRESHOLD < score < AI_THRESHOLD


BAND_LABEL = {"ai": f"likely_ai (>= {AI_THRESHOLD})", "human": f"likely_human (<= {HUMAN_THRESHOLD})",
              "uncertain": f"uncertain ({HUMAN_THRESHOLD}-{AI_THRESHOLD})"}


def fmt(x):
    return "n/a" if x is None else f"{x:.3f}"


def run():
    rows = []
    for cid, desc, band, text in CASES:
        r = analyze_text(text)
        rows.append((cid, desc, band, text, r))
        s = r["signals"]
        print(f"{cid} {desc}: llm={fmt(s['llm']['score'])} stylo={fmt(s['stylometric']['score'])} "
              f"lex={fmt(s['lexical']['score'])} raw={fmt(r['raw_score'])} final={fmt(r['ai_score'])} "
              f"-> {r['attribution']} adj={r['adjustments']} words={r['word_count']} "
              f"{'PASS' if in_band(r['ai_score'], band) else 'MISS'}")
    return rows


def write_markdown(rows, path):
    passes = sum(1 for _, _, band, _, r in rows if in_band(r["ai_score"], band))
    lines = [
        "# Calibration results",
        "",
        f"Generated by `scripts/calibrate.py` on {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        f" (real Groq call). Result: **{passes}/{len(rows)} PASS**.",
        "",
        f"Settings: weights {scoring.TEXT_WEIGHTS}; thresholds AI >= {AI_THRESHOLD}, human <= {HUMAN_THRESHOLD}; "
        f"disagreement spread > {scoring.DISAGREEMENT_SPREAD} -> shrink {scoring.DISAGREEMENT_SHRINK:.0%}; "
        f"short text {scoring.SHORT_TEXT_RULES} (words below, shrink); single signal -> shrink "
        f"{scoring.SINGLE_SIGNAL_SHRINK:.0%}.",
        "",
        "| # | Input | Words | LLM | Stylometric | Lexical | Raw | Final ai_score | Confidence | Attribution "
        "| Adjustments | Expected | Result |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for cid, desc, band, _, r in rows:
        s = r["signals"]
        lines.append(
            f"| {cid} | {desc} | {r['word_count']} | {fmt(s['llm']['score'])} | {fmt(s['stylometric']['score'])} "
            f"| {fmt(s['lexical']['score'])} | {fmt(r['raw_score'])} | **{fmt(r['ai_score'])}** "
            f"| {fmt(r['confidence'])} | {r['attribution']} | {', '.join(r['adjustments']) or '-'} "
            f"| {BAND_LABEL[band]} | {'PASS' if in_band(r['ai_score'], band) else 'MISS'} |")
    lines += ["", "## Per-case signal details", ""]
    for cid, desc, band, text, r in rows:
        s = r["signals"]
        st = s["stylometric"]["details"]
        lx = s["lexical"]["details"]
        lines += [
            f"### {cid}: {desc}",
            "",
            "> " + text.replace("\n", "  \n> "),
            "",
            f"- **LLM** ({fmt(s['llm']['score'])}): {s['llm']['details'].get('reasoning') or s['llm']['details'].get('error')}",
        ]
        if s["stylometric"]["available"]:
            lines.append(
                f"- **Stylometric** ({fmt(s['stylometric']['score'])}): {st['sentence_count']} sentences "
                f"({st['segmentation']}), lengths {st['sentence_lengths']}, CV {st['sentence_length_cv']}, "
                f"avg word length {st['avg_word_length']}, informal punct/sentence "
                f"{st['informal_punct_per_sentence']}, MATTR {st['mattr']}; sub-scores {st['sub_scores']}")
        else:
            lines.append(f"- **Stylometric**: unavailable ({st.get('reason')})")
        lines.append(
            f"- **Lexical** ({fmt(s['lexical']['score'])}): AI phrases {lx.get('ai_phrases')} "
            f"({lx.get('ai_hits_per_100w')}/100w); human markers {lx.get('human_markers')} + contractions "
            f"{lx.get('contractions')} ({lx.get('human_hits_per_100w')}/100w)")
        lines.append(f"- Effective weights: { {k: v['weight'] for k, v in s.items()} }; adjustments: "
                     f"{r['adjustments'] or 'none'}")
        lines.append("")
    tuning = os.path.join(ROOT, "docs", "calibration_tuning_log.md.part")
    if os.path.exists(tuning):
        with open(tuning) as f:
            lines += ["", f.read().rstrip(), ""]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote {path} ({passes}/{len(rows)} PASS)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "calibration.md"))
    ap.add_argument("--json", action="store_true", help="also dump full results as JSON to stdout")
    args = ap.parse_args()
    rows = run()
    if args.json:
        print(json.dumps([{"id": c, "result": r} for c, _, _, _, r in rows], indent=2))
    write_markdown(rows, args.out)
