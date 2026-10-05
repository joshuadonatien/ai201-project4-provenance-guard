# Provenance Guard: Learning Guide

A study guide so you can explain and defend every part of this project, on video and to a grader. Every number
here comes from `docs/calibration.md`, `docs/evidence/*` or the code. None are invented. If someone asks
"where does that number come from?", the answer is in the file named next to it.

---

## 1. The big picture in 5 sentences

1. Provenance Guard is a Flask backend that a creative-writing platform calls to estimate whether a submitted piece
   (text, or an image described by its metadata) was AI-generated.
2. It runs **three independent signals** on text: a Groq LLM judge, stylometric statistics and lexical stock-phrase
   markers. It combines them into one `ai_score` with a weighted mean, then pulls that score toward 0.5 whenever the
   evidence is weak (signals disagree, text is short, or only one signal ran).
3. **Asymmetric thresholds** turn the score into `likely_ai` (≥ 0.80), `uncertain`, or `likely_human` (≤ 0.35), because
   wrongly calling a human's work AI is worse than missing an AI piece.
4. Each attribution maps to a fixed, plain-language **transparency label** for readers. Creators can **appeal**, which
   flips the piece to "Label under review" and puts it in a human reviewer queue.
5. Every decision, appeal, resolution and certificate is written to an **append-only SQLite audit log**, and
   **rate limits** (10/min and 100/day on `/submit`) protect the shared Groq quota and stop people from probing the
   classifier.

### Request lifecycle: `POST /submit` (text)

| Step | File : function | What happens |
|---|---|---|
| 1 | `app.py : submit` (decorated with `@limiter.limit(SUBMIT_LIMIT)`) | Flask-Limiter checks the caller's IP quota first. Over the limit → the `rate_limited` handler returns a 429 JSON body. |
| 2 | `app.py : _json_body` | Parses the JSON. A missing or non-object body → 400 `invalid_json`. |
| 3 | `app.py : submit` | Validates `creator_id`, `title` (≤ 200 chars), `content_type` ∈ {text, image}, and text length 20–10,000 chars (`MIN_TEXT_CHARS` / `MAX_TEXT_CHARS`). |
| 4 | `detection/__init__.py : analyze_text` | Counts words, then runs each signal inside `_safe(...)` so a crashing signal can never take the request down. |
| 4a | `detection/llm.py : llm_signal → _judge → _call` | Groq `openai/gpt-oss-120b` at temperature 0 (falls back to `openai/gpt-oss-20b`). JSON mode first, then without it if Groq returns 400. `extract_json` parses `{"ai_likelihood", "reasoning"}`, and the code divides by 100 and clamps. Any failure → `available: False`. |
| 4b | `detection/stylometric.py : stylometric_signal` | `split_sentences` (prose or verse mode), then CV of sentence lengths, average word length, informal punctuation per sentence → three `ramp`s → weighted sub-score. |
| 4c | `detection/lexical.py : lexical_signal` | Counts AI stock phrases and human casual markers per 100 words → `0.5 + 0.18·ai_rate − 0.12·human_rate`, clamped. |
| 5 | `detection/scoring.py : combine_signals` | Renormalised weighted mean → `shrink` for disagreement → `short_text_shrink` → single-signal shrink → `attribution_for` → `confidence = max(s, 1−s)`. |
| 6 | `labels.py : build_label → label_variant_for, confidence_text` | Attribution → variant → fixed headline and body, plus "Our confidence: high/moderate/low". |
| 7 | `storage.py : save_content` | Inserts the current-state row into the `contents` table. |
| 8 | `storage.py : append_audit("submission", ...)` | Appends an immutable event to `audit_log` (scores, weights, adjustments). |
| 9 | `app.py : _public_record` → `jsonify(...)`, **201** | Response: `content_id, attribution, ai_score, confidence, status, signals{score, available, weight, details}, adjustments, label, timestamp`. |

Images take the same route, except step 4 is `detection/__init__.py : analyze_image` →
`detection/image.py : metadata_signal` + `detection/llm.py : llm_image_signal`, with weights 0.65 / 0.35 and no
short-text rule.

### Request lifecycle: `POST /appeal`

| Step | File : function | What happens |
|---|---|---|
| 1 | `app.py : appeal` (`@limiter.limit("5 per hour")`) | Rate limit check. |
| 2 | `app.py : _json_body`, field checks | `content_id` and `creator_id` required → 400. |
| 3 | `storage.py : get_content` | Unknown id → **404**. |
| 4 | `app.py : appeal` | `creator_id` ≠ the submitter → **403** (stands in for real authentication). Reasoning must be 20–2,000 chars → **400**. Already `under_review` → **409**. |
| 5 | `app.py : _decision_snapshot` | Freezes the original attribution, ai_score, confidence, label variant and every signal score *before* anything changes. |
| 6 | `storage.py : update_content` | `status = under_review`, `label_variant = under_review`, stores `appeal_id` (`AP-…`), reasoning and `appeal_filed_at`. |
| 7 | `storage.py : append_audit("appeal_filed", ...)` | The audit event holds the reasoning **and** `details.original_decision`, so the appeal sits next to the decision it contests. |
| 8 | `app.py : _label_for → labels.build_label` | Returns the "Label under review" label. Response **201**. |
| Later | `app.py : appeals_queue` (`GET /appeals`), `app.py : resolve_appeal` (`POST /appeals/<id>/resolve`) | Reviewer sees the queue oldest-first and resolves it as `upheld` or `overturned`. Overturned → `likely_human`. Audit event `appeal_resolved` with a `before` snapshot. |

---

## 2. Concepts, explained with this project's real numbers

### 2.1 Multi-signal detection, and why the signals must be independent

One detector has one set of blind spots. Three detectors that measure **different things** have blind spots that
mostly don't overlap, so one detector being fooled doesn't automatically fool the system.

- **LLM judge**: meaning and register ("assistant voice", hedged balance, no lived specifics).
- **Stylometric**: shape (sentence-length rhythm, word length, punctuation texture). It ignores meaning.
- **Lexical**: specific surface tokens ("furthermore", "paradigm shift", "lol", contractions).

If all three measured the same thing (say, three LLM prompts), they would fail together and their agreement would be
fake confidence. Case **C3** in `docs/calibration.md` (AI text with injected slang) shows why independence matters.
The slang fooled the LLM (0.200), but stylometrics (0.872) and lexical (0.829) still saw AI structure. The spread was
0.672, which is more than 0.45, so the disagreement rule fired and the result was **uncertain (0.518)**, not a
confident wrong answer.

### 2.2 How each signal measures, with worked examples

**`ramp(x, zero_at, one_at)`** (`detection/scoring.py`) is linear interpolation clamped to [0, 1]. `ramp(x, 0.70, 0.20)`
returns 0 at x = 0.70 (reads human), 1 at x = 0.20 (reads AI), and is linear in between.

#### Stylometric, by hand for H1 (handout clear-AI text)

From `docs/calibration.md`: 3 sentences of lengths [10, 22, 11], CV 0.379, avg word length 6.233, informal punctuation
per sentence 0.0.

1. **Burstiness**: `ramp(0.379, 0.70 → 0, 0.20 → 1) = (0.379 − 0.70) / (0.20 − 0.70) = −0.321 / −0.5 ≈ 0.641`
   (0.642 with the rounded CV. The code uses the unrounded CV, which gives 0.641).
2. **Word length**: `ramp(6.233, 4.2 → 0, 5.6 → 1)`. 6.233 is past 5.6, so it **clamps to 1.0**.
3. **Punctuation**: `ramp(0.0, 0.8 → 0, 0.0 → 1)`. Zero informal marks, so **1.0**.
4. **Weighted 0.45 / 0.35 / 0.20**: `0.45·0.641 + 0.35·1.0 + 0.20·1.0 = 0.2885 + 0.35 + 0.20 = 0.8385 ≈ 0.839` ✔
   (this matches the table).

Compare **C4** (diary): CV 0.714 → burstiness 0.0, avg word length 3.967 → 0.0, punctuation 0.3/sentence → 0.625, so
stylometric = `0.20·0.625 = 0.125` ✔.

**Verse mode:** for a poem, line length is set by the form, not the writer, so burstiness is **excluded** and the weights
renormalise over word length and punctuation (0.35 + 0.20 = 0.55). For C1 (rain poem):
`(0.35·0.0 + 0.20·1.0) / 0.55 = 0.364` ✔. Without this, a perfectly regular poem (CV 0.0) would get burstiness 1.0
and look "metronomic AI".

**Availability rule:** stylometrics needs ≥ 3 sentences **and** ≥ 40 words. H3 had 2 sentences (43 words) and H4 had
39 words (3 sentences), so stylometrics was `available: false` for both.

#### Lexical, worked example (H4, borderline edited AI)

AI phrases: "studies show" → 2.56 per 100 words. Human markers: the contraction "I've" counts as half a hit → 1.28 per
100 words.
`0.5 + 0.18·2.56 − 0.12·1.28 = 0.5 + 0.461 − 0.154 ≈ 0.808` ✔.
For H1 there are 11 AI phrases in 43 words = 25.58/100w → `0.5 + 0.18·25.58` is far above 1, so it clamps to **1.0**.

#### LLM judge

The prompt asks for `{"ai_likelihood": 0–100, "reasoning": "<one sentence>"}` at temperature 0. For H1 the reply was
85 → **0.850**, with the reasoning "generic, balanced phrasing, hedged statements, and lacks personal specifics". The
prompt explicitly says "Formal or non-native English alone is NOT evidence of AI", which is a mitigation for edge case 2.

### 2.3 Weighted mean and renormalisation

Weights: **LLM 0.50, stylometric 0.30, lexical 0.20** (`TEXT_WEIGHTS`).

`raw = Σ wᵢ·scoreᵢ / Σ wᵢ`, summed **only over available signals**.

- **H1 (all three available):** `0.5·0.850 + 0.3·0.839 + 0.2·1.000 = 0.425 + 0.252 + 0.200 = 0.877` ✔.
- **H3 (stylometric unavailable):** the remaining weights 0.5 and 0.2 add up to 0.7, so they renormalise to
  `0.5/0.7 = 0.714` and `0.2/0.7 = 0.286`. Then `raw = 0.714·0.700 + 0.286·0.500 = 0.500 + 0.143 = 0.643` ✔.
  The response shows these effective weights in each signal's `weight` field (stylometric gets weight 0).

Why renormalise instead of treating a missing signal as 0.5? Plugging in 0.5 would silently drag every result toward
uncertain and *pretend* we measured something. Renormalising is honest: "we only have two signals". The uncertainty
from having less evidence is handled separately and visibly, by the single-signal shrink.

### 2.4 Shrinking toward 0.5

`shrink(s, f) = 0.5 + (s − 0.5)·(1 − f)`. It keeps `(1 − f)` of the distance from 0.5. Adjustments apply in this
order, and each one is recorded in `adjustments[]`:

| Rule | Condition | Shrink |
|---|---|---|
| `signal_disagreement` | max − min of available scores > 0.45 | 35% |
| `short_text` | < 25 words / 25–59 words | 40% / 15% |
| `single_signal` | only one signal available | 50% |

- **H1:** raw 0.877, 43 words → 15%: `0.5 + 0.377·0.85 = 0.820` → **likely_ai** ✔.
- **H2:** raw 0.108, 55 words → 15%: `0.5 − 0.392·0.85 = 0.167` → **likely_human** ✔.
- **H4:** raw 0.481. Spread 0.808 − 0.350 = 0.458 > 0.45 → `0.5 − 0.019·0.65 = 0.488`. Then 39 words → 15% →
  **0.489** → uncertain ✔.
- **C2 (ESL paragraph):** raw 0.574. Spread 1.0 − 0.3 = 0.7 → `0.5 + 0.074·0.65 = 0.548` → **uncertain** ✔.

**The short-text deviation story (good for the video).** The spec said "< 60 words → shrink 40%". With a 40% shrink
the highest score any short text can reach is `0.5 + 0.5·0.6 = 0.80`, right on the threshold. The handout's 43-word
clear-AI sample (raw 0.877) then scored `0.5 + 0.377·0.6 = 0.726`, which is uncertain, so the clearest AI example in
the handout could never be flagged. I changed this to a graded rule: < 25 words still uses 40% (haiku and captions
stay capped at 0.80), and 25–59 words uses 15%. To reach 0.80 at 15%, raw must be ≥ 0.853, so **all signals must
agree strongly**. One loud LLM (0.95 with two neutral 0.5s → raw 0.725) still can't get there. The comment block in
`detection/scoring.py` (`SHORT_TEXT_RULES`) documents this.

### 2.5 `ai_score` vs `confidence`

- `ai_score` ∈ [0, 1] answers *which direction*: 0 = reads fully human, 1 = reads fully AI.
- `confidence = max(ai_score, 1 − ai_score)` ∈ [0.5, 1] answers *how sure we are of the label shown*.
- H2: ai_score 0.167 → confidence **0.833** (fairly sure it's human). H4: ai_score 0.489 → confidence **0.511**
  (a coin flip).
- **A confidence of 0.6** means "slightly better than a coin flip, we would not act on this". It always falls in the
  uncertain band, because 0.6 confidence means ai_score is 0.6 or 0.4, both of which are between 0.35 and 0.80.
- Readers never see decimals. `labels.confidence_text` maps ≥ 0.90 → "high", 0.75–0.90 → "moderate", < 0.75 → "low".

### 2.6 Asymmetric thresholds and the cost of a false positive

| ai_score | attribution |
|---|---|
| ≥ 0.80 | likely_ai |
| 0.35–0.80 | uncertain |
| ≤ 0.35 | likely_human |

The AI bar is 0.30 away from 0.5, but the human bar is only 0.15 away. A false "AI" label damages a real person's
reputation, while a missed AI piece costs readers some context. So the system needs much more evidence before it
accuses anyone. The wide middle band is an honest "we don't know", and the uncertain label says "It is not an
accusation." Example: the ESL writer (C2) got uncertain at 0.548 even though two signals (stylometric 0.747,
lexical 1.0) leaned AI.

### 2.7 Calibration vs thresholds: "tune ramps, not thresholds"

- **Thresholds** (0.80 / 0.35) are **product/ethics decisions**: how much evidence before we label someone. They
  shouldn't move just because a test case fails.
- **Ramps** (CV 0.70 → 0.20, word length 4.2 → 5.6, punctuation 0.8 → 0.0) and coefficients (0.18 / 0.12) are
  **measurement**: how a raw statistic maps to AI-likelihood. These are what you tune when calibration misses.
- `scripts/calibrate.py` runs 8 texts (4 from the handout, 4 of mine) with real Groq calls and writes
  `docs/calibration.md`. Result: **7/8 PASS**. The miss is **C1** (rain poem): expected uncertain, got
  likely_human at 0.338. That miss is in the **safe direction**: a human poem was called human, just more
  confidently than planned. I left it visible instead of moving a threshold to hide it.

---

## 3. Flask and production essentials used here

### Flask
- **App factory**: `create_app(db_path=None)` in `app.py` builds a fresh app with its own DB path and its own limiter.
  Tests call `create_app(tmp_path)`, so each test gets a clean database and fresh rate-limit counters. A module-level
  `app = create_app()` exists for `python app.py`.
- **Routes**: `@app.post("/submit")`, `@app.get("/log")` and so on. URL variables look like `/content/<content_id>`.
- **`jsonify`**: turns a dict into a JSON response with the right `Content-Type`. Returning `(jsonify(body), 201)`
  sets the status code. `app.json.sort_keys = False` keeps fields in a readable order.
- **`request.get_json(silent=True)`**: returns `None` instead of raising on bad JSON, so the code can return a clean
  400 `invalid_json`.
- **Error handlers**: `@app.errorhandler(429)`, `@app.errorhandler(HTTPException)` and `@app.errorhandler(Exception)`
  turn *every* error (404, 405, 413, 500 …) into `{"error": "<code>", "message": "<sentence>"}`.
- **Why JSON errors?** The clients are programs (the platform's backend), not browsers. A machine needs a stable
  `error` code to branch on, and a human-readable `message` to show. Flask's default HTML error pages would break
  any client that calls `response.json()`.
- **`MAX_CONTENT_LENGTH` = 1 MB**: oversized bodies get a 413 before any processing.
- **`render_template("dashboard.html", ...)`**: Jinja renders the analytics page on the server. No JavaScript
  framework is needed.

### Flask-Limiter
- `Limiter(get_remote_address, app=app, storage_uri="memory://")`. The **key** is the client IP, and counters live in
  **process memory**.
- `/submit`: `"10 per minute;100 per day"`. `/appeal` and `/certificate`: `"5 per hour"`.
- Over the limit → Flask-Limiter raises a 429. The custom handler returns
  `{"error": "rate_limited", "message": "Too many requests. Please wait before trying again.", "limit": "10 per 1 minute"}`
  (the real output is in `docs/evidence/rate_limit_run.txt`: requests 1–10 → 201, requests 11–12 → 429).
- **Consequences of memory storage**: counters reset when the server restarts, and they are not shared across
  multiple worker processes. In production you'd use Redis (`storage_uri="redis://..."`). Behind a proxy or NAT, many
  users share one IP, so you'd key on the authenticated user id instead.
- **Why these numbers**: a real writer posts a few pieces per session. 10/min still allows uploading a small
  collection, and 100/day is roughly 5–20× a prolific poster. Each submit makes one Groq call, and Groq's free tier is
  about 30 requests/min, so one client can't exhaust the shared quota. The daily cap also stops someone from probing
  the classifier thousands of times to learn how to evade it. Appeals go to humans, so 5/hour stops reviewer spam.

### SQLite: append-only audit log vs current-state table
- **`contents`**: one row per piece, **updated in place** (status, label_variant, appeal fields, certificate). It
  answers "what is true *now*?"
- **`audit_log`**: one row per **event** (`submission`, `appeal_filed`, `appeal_resolved`, `certificate_issued`,
  `certificate_denied`), **never updated or deleted**. It answers "what happened, when, and what did we know at the
  time?" Each row has a timestamp (ISO-8601 UTC, ms), attribution, ai_score, confidence, every signal score,
  `signals_used`, status, label variant, appeal reasoning and a JSON `details` blob.
- Why both: if you only kept current state, an appeal would overwrite the evidence of the original decision. The
  `appeal_filed` event stores `details.original_decision`, so a reviewer or auditor can always see the decision being
  contested. `GET /log?limit=N` returns entries newest-first. The sample in `docs/evidence/audit_log_sample.json` has
  6 entries: 4 submissions, 1 appeal_filed, 1 certificate_issued.

---

## 4. How the stretch features work

1. **Ensemble (≥ 3 signals)**: LLM, stylometric and lexical, weighted 0.50 / 0.30 / 0.20. Conflict resolution:
   (a) the weighted mean means no signal decides alone, and the LLM is capped at 50%; (b) spread > 0.45 → 35% shrink;
   (c) missing signals are dropped and weights renormalised, and a lone survivor gets a 50% shrink. Individual scores,
   effective weights and details are shown in every response and in the audit log.
2. **Provenance certificate** (`certificate.py`, `POST /certificate`): the creator submits an **earlier draft** and a
   process note. Checks: the caller is the creator, the piece is not currently `likely_ai`, the draft has ≥ 40 words,
   the notes have ≥ 40 chars, and **word-level** `difflib.SequenceMatcher` similarity is between 0.30 and 0.95 (close
   enough to be the same piece, different enough to show revision). All checks run with no short-circuit, so the
   creator sees every failure. On success the label becomes "Verified human: creator shared their drafts", with an id
   like `PG-EBFC1FE1` (demo similarity **0.694**). On failure → 422 plus a `certificate_denied` audit event.
   *Deviation:* character-level similarity gave **0.303** for two unrelated texts, which would pass the 0.30 floor,
   so I switched to word tokens.
3. **Analytics dashboard** (`GET /stats` JSON, `GET /dashboard` HTML, `storage.stats`): detection pattern (count and %
   for each attribution, using the *original* automated decision), appeal rate and overturn rate, **signal-disagreement
   rate** (my metric: a rising rate warns that signals are drifting apart, e.g. a new model the phrase list doesn't
   know), average confidence, submissions by content type, and certificates issued/denied. Demo run: appeal rate
   25.0%, disagreement rate 25.0%. A screenshot is in `docs/evidence/dashboard.png`.
4. **Multi-modal images** (`detection/image.py`): `content_type: "image"` with a `metadata` object and an optional
   `description`. The metadata signal scores: IPTC `trainedAlgorithmicMedia` → 0.97, a named generator (Midjourney,
   DALL·E, Stable Diffusion, …) → 0.95, generation params (`prompt`, `seed`, `cfg_scale` …) → 0.90, camera make+model
   with exposure data → 0.15, camera without exposure data → **0.30** (*deviation:* the spec didn't cover this case,
   and make/model alone is easy to type, so it leans human less), nothing → 0.5. A square generator size (e.g.
   1024×1024) adds +0.10. Combined with an LLM judge of description and metadata at weights 0.65 / 0.35. Demo:
   Midjourney + 1024×1024 + prompt → metadata 1.0, LLM 1.0 → **likely_ai 1.0**. Blind spot: stripped metadata
   (screenshots, social re-encodes) → 0.5 → uncertain.

---

## 5. Likely grader / interviewer questions, with model answers

1. **Why not threshold at 0.5?**
   At 0.5, a score of 0.51 would accuse someone based on a coin flip. The errors cost different amounts: a false AI
   label hurts a real person, a miss costs readers some context. So the AI bar is 0.80, the human bar is 0.35, and
   everything between is labelled "Origin unclear, not an accusation".

2. **What does a confidence of 0.6 mean?**
   It means we're slightly better than a coin flip and wouldn't act on it. Confidence is `max(score, 1−score)`, so 0.6
   means ai_score is 0.6 or 0.4. Both fall in the uncertain band, and the label says "Our confidence: low".

3. **Why is the LLM capped at 50% weight?**
   So it can never decide alone. LLMs are over-confident and badly calibrated, and they tend to call formal or
   non-native English "AI". With the LLM at 0.95 and two neutral heuristics at 0.5, the result is
   `0.475 + 0.15 + 0.10 = 0.725`, which is uncertain. It needs at least one other signal to agree.

4. **Why must the signals be independent?**
   Correlated signals fail together, so their agreement adds no information. Mine measure meaning (LLM), shape
   (stylometric) and surface tokens (lexical). In C3 the slang fooled the LLM (0.20), but the other two (0.872, 0.829)
   didn't, and the disagreement rule produced uncertain instead of a wrong "human".

5. **How did you validate the scores?**
   `scripts/calibrate.py` ran 8 texts with real Groq calls and recorded every signal separately in
   `docs/calibration.md`: 7/8 pass. Clear AI 0.820, clear human 0.167, the two borderlines 0.621 and 0.489. The one
   miss is a human poem scored 0.338 (human) where I expected uncertain. That's a miss in the safe direction.
   There are also 66 offline pytest tests, including threshold boundaries at 0.80 / 0.7999 / 0.3501 / 0.35.

6. **What happens if Groq is down?**
   The LLM signal returns `available: false` with the error in `details`. The other weights renormalise to
   0.6 / 0.4, and the response still comes back. If only one signal survives, a 50% shrink pushes it toward
   uncertain. A dependency outage never becomes a 500 or a confident wrong label.

7. **How would an adversary beat it?**
   They'd ask the AI to vary its sentence length and use contractions and informal punctuation (beats stylometrics),
   find-and-replace the stock phrases (beats lexical), and add personal anecdotes and typos (often fools the LLM, as
   C3 shows). They could also probe the API to learn the boundary, which the 100/day limit slows down. For images,
   they'd just strip the metadata. The design means a successful evasion usually lands in *uncertain*, not *human*.
   This is a deterrent and a context tool, not proof.

8. **What would you change for production?**
   Redis-backed rate limits keyed on the authenticated user rather than IP; real authentication for appeals instead
   of `creator_id` in the body; Postgres with a write-only role for the audit log; a labelled evaluation set of
   hundreds of texts to calibrate ramps and measure false-positive rate per group (ESL writers especially); async
   Groq calls with a timeout budget; versioned model, prompt and phrase lists recorded in each audit row; reviewer
   authentication on `/appeals/.../resolve`.

9. **Why an append-only log *and* a contents table?**
   The table is the current truth for the UI. The log is the history for accountability. An appeal changes the
   current label, but the log keeps the original decision (`details.original_decision`) next to the appeal reasoning.

10. **Why does the appeal not re-run the detector?**
    Re-running gives the same signals and the same answer. The point of the appeal is human judgement on context the
    detector can't see, like "I'm a non-native speaker". The reviewer sees the excerpt, every signal score, the LLM's
    reasoning, the matched phrases and the creator's reasoning, then upholds or overturns.

11. **Where did you deviate from your spec, and why?**
    (a) The short-text shrink was graded (< 25 words 40%, 25–59 words 15%) because the flat 40% capped the 43-word
    clear-AI sample at 0.726. (b) Certificate similarity is word-level because character-level gave 0.303 for
    unrelated text. (c) Verse mode excludes burstiness, because a regular poem would otherwise look "metronomic AI".
    (d) Camera without exposure data scores 0.30, a case the spec didn't cover. (e) The model is `openai/gpt-oss-120b`
    because the handout's llama-4-scout is no longer served by Groq.

12. **Why 15% for 25–59 words, not some other number?**
    It has to let a short text that all signals agree on reach 0.80 (raw ≥ 0.853), but not a text with one loud
    signal (raw 0.725 → 0.691). 15% separates those two cases. Under 25 words the 40% shrink caps the score at exactly
    0.80, so a haiku or caption reaches likely_ai only if every available signal gives a perfect 1.0.

13. **Why does the "Likely human" label say "no strong signs" instead of "human-written"?**
    We can't prove absence. The AI label is the only one that mentions the appeal, because it's the only one that can
    harm someone.

14. **Why does the ESL paragraph (C2) not get flagged even though two signals said AI?**
    The LLM said 0.30, so the spread (0.7) triggered the disagreement shrink: raw 0.574 → 0.548, which is uncertain.
    This is exactly edge case 2 in `planning.md`, and the demo appeals this piece.

15. **Why rate-limit at all? It's your own API.**
    Every submit spends shared Groq quota (about 30 requests/min on the free tier). An unlimited detector is also a
    free oracle for tuning evasions. The 429 is JSON with the limit string, so a client can back off.

---

## 6. Self-check: every rubric line → evidence → status

> README.md was written by a separate agent in parallel. Skim it once and confirm the README sections named below
> exist and say what you'd say. Section names may differ slightly.

### Required (25 pts)

| # | Rubric line | Pts | Evidence (file → section) | Status |
|---|---|---|---|---|
| 1 | Submission endpoint returns structured JSON | 1 | `app.py : submit` / `_public_record`; `docs/evidence/demo_run.txt` steps 1–3 | ✅ |
| 2 | Response includes attribution + confidence | 1 | Same, e.g. `"attribution": "likely_ai", "confidence": 0.82` | ✅ |
| 3 | Response includes label text | 1 | `label {variant, headline, body, confidence_text}` in demo_run steps 1–3 | ✅ |
| 4 | README names ≥ 2 signals, what each measures and misses | 1 | README → signals section; `planning.md` §3 | ✅ (confirm in README) |
| 5 | Demo/source shows individual signal scores next to combined | 1 | `signals.{llm,stylometric,lexical}.score` + `ai_score` in demo_run; `docs/calibration.md` table | ✅ (show on video) |
| 6 | Two submissions with noticeably different confidence | 1 | demo_run: 0.82 (AI), 0.833 (human), **0.548** (ESL); calibration H4 0.511 | ✅ (show on video) |
| 7 | README explains combination + validation | 1 | README → confidence/scoring section; `planning.md` §3–4; `docs/calibration.md` | ✅ (confirm in README) |
| 8 | README has the actual text of the label variants | 1 | README → labels section; `labels.py`; `planning.md` §5 | ✅ (confirm in README) |
| 9 | Labels are plain language | 1 | `labels.py`: no decimals, confidence in words | ✅ (+ do the reader test, §7) |
| 10 | Labels visibly differ between high/low confidence | 1 | "Likely AI-generated" vs "Origin unclear … not an accusation" vs "Likely human-written" | ✅ (show on video) |
| 11 | Appeal submitted with reasoning | 1 | demo_run step 5 (`creator_reasoning`) | ✅ (show on video) |
| 12 | Status under review + appeal visible in audit log | 1 | demo_run steps 5–6 (`under_review`), step 9 `appeal_filed`; `audit_log_sample.json` | ✅ (show on video) |
| 13 | Demo shows a 429 | 1 | `docs/evidence/rate_limit_run.txt` (requests 11–12 → 429) | ⚠️ must appear in your **video** |
| 14 | README documents limits + reasoning | 1 | README → rate limiting section; `planning.md` §8 | ✅ (confirm in README) |
| 15 | ≥ 3 log entries, each with attribution, confidence, timestamp | 1 | `docs/evidence/audit_log_sample.json` (6 entries) | ✅ |
| 16 | Log is structured | 1 | JSON with fixed columns (`storage.py` `audit_log` schema) | ✅ |
| 17 | ≥ 1 appeal visible next to the original | 1 | `appeal_filed` entry with `details.original_decision` + `appeal_reasoning` | ✅ |
| 18 | planning.md: signals + combination | 1 | `planning.md` §3 | ✅ |
| 19 | planning.md: thresholds | 1 | `planning.md` §4 (thresholds table) | ✅ |
| 20 | planning.md: label variants | 1 | `planning.md` §5 | ✅ |
| 21 | planning.md: appeals + ≥ 2 edge cases + AI Tool Plan | 1 | `planning.md` §6, §7 (6 edge cases), "AI Tool Plan" | ✅ |
| 22 | README known limitations are specific | 1 | README → limitations (verse miss C1, ESL, metadata stripping, slang evasion…) | ✅ (confirm in README) |
| 23 | README spec reflection with a real divergence | 1 | README → spec reflection (short-text shrink, word-level difflib, model swap) | ✅ (confirm in README) |
| 24 | AI usage: ≥ 2 specific instances | 1 | README → AI usage section | ⚠️ review that it matches your experience, in your voice |
| 25 | AI usage: each says what was revised/overridden | 1 | Same | ⚠️ review |

### Stretch (+4)

| Feature | Evidence | Status |
|---|---|---|
| Ensemble (≥ 3 signals, weighting, conflict resolution, individual scores shown) | `detection/scoring.py`, `planning.md` §9.1, signals in every response | ✅ |
| Provenance certificate | `certificate.py`, `POST /certificate`, demo_run step 8 (`PG-EBFC1FE1`, similarity 0.694) | ✅ |
| Analytics dashboard (≥ 3 metrics) | `/stats`, `/dashboard`, `templates/dashboard.html`, `docs/evidence/dashboard.png` | ✅ |
| Multi-modal | `detection/image.py`, demo_run step 4 (Midjourney image → likely_ai 1.0) | ✅ |

---

## 7. Things only YOU (the human) must do

Due **Monday Oct 5 2026, 11:59 PM EDT (today)**.

- [ ] **Record the walkthrough video** (follow `docs/VIDEO_SCRIPT.md`), upload it (YouTube unlisted or Loom), and
      **paste the link into README.md in the TODO spot**.
- [ ] **Do the reader test of the label with a real person.** Show someone the three label variants (no
      explanation) and ask what each means to them and whether the uncertain one reads as an accusation. **Fill in
      the README TODO honestly** with what they actually said, including any confusion.
- [ ] **Review the README AI-usage section.** Make sure it's accurate to your experience and in your voice. Edit
      anything you don't recognise or wouldn't say.
- [ ] **Submit the repo link + video link on the Course Portal before 11:59 PM EDT.**
- [ ] **Keep `.env` out of git.** It's already in `.gitignore`. Before pushing, check that `git status` doesn't
      list `.env`.
- [ ] *(Optional)* **Rotate the Groq key** at console.groq.com if it was ever pasted into a chat, screenshot or commit.

---

## 8. How to re-run everything

```bash
cd ~/ai201-project4-provenance-guard
source .venv/bin/activate

# Offline tests (no network, no Groq): expect "66 passed"
python -m pytest -q

# Start the server (port 5000 on macOS is taken by AirPlay Receiver, so use 5001)
rm -f demo.db && PORT=5001 PROVENANCE_DB=demo.db python app.py

# In a second terminal: full end-to-end demo (health, 3 texts, image, appeal, queue, certificate, log, stats)
bash scripts/demo.sh

# Rate-limit demo: 12 rapid submits → 10×201 then 429.
# Needs a fresh per-minute quota: restart the server or wait 60 s after the main demo.
bash scripts/demo.sh ratelimit

# Dashboard
open http://localhost:5001/dashboard

# Re-run calibration (makes 8 REAL Groq calls and OVERWRITES docs/calibration.md; only if you mean to)
python scripts/calibrate.py
```

Port already in use? Run `lsof -nP -iTCP:5001 -sTCP:LISTEN`, then `kill <PID>`.
