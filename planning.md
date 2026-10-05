# Provenance Guard — Planning & Spec

> Written before implementation (Milestones 1–2). This document is the contract the code implements and the
> context I hand to AI tools in Milestones 3–5. Stretch-feature sections (§9) were added before any stretch work began.

---

## 1. Problem framing

Provenance Guard is a backend a creative-writing platform plugs into. A creator posts a piece; we estimate whether it was
AI-generated, express **how sure we are**, show readers a plain-language **transparency label**, and give creators an
**appeal** path. Every decision is written to an **audit log**.

Guiding principle: **a false positive (calling a human's work AI) is worse than a false negative.** A wrong "AI" label
damages a real person's reputation; a missed AI piece costs readers some context. So:

- The bar for an "AI" label is high (score ≥ 0.80); the bar for "human" is lower (score ≤ 0.35).
- Anything in between is labelled **uncertain**, and the uncertain label explicitly says "this is not an accusation."
- When signals disagree, or the text is too short to judge, the score is pulled toward 0.5 (uncertain) instead of
  letting one loud signal decide.

---

## Architecture

### Narrative

**Submission flow.** A client sends `POST /submit` with `text` and `creator_id`. The rate limiter checks the caller's
quota, then input validation checks length. The text goes to three independent signals: the Groq LLM judge (semantic),
the stylometric analyzer (structural statistics) and the lexical-marker analyzer (stock-phrase vs. informal markers). Each
returns an AI-likelihood between 0 and 1. The scoring module combines them by weight, applies uncertainty adjustments
(disagreement and short text), and maps the final score to one of three attributions. The label module turns that
attribution into reader-facing text. The record is saved to the `contents` table, an audit event is appended to
`audit_log`, and the JSON response (content_id, attribution, confidence, every signal score, label) goes back to the client.

**Appeal flow.** The creator sends `POST /appeal` with `content_id`, `creator_id` and `creator_reasoning`. We check that the
content exists, that the caller is the creator, that the reasoning is substantive, and that no appeal is already open. The
content's status becomes `under_review`, its label switches to the "under review" variant, an `appeal_filed` event holding
**a snapshot of the original decision plus the reasoning** goes into the audit log, and a confirmation comes back. A reviewer
sees the appeal in `GET /appeals` and can resolve it with `POST /appeals/<content_id>/resolve`.

### Diagram

```
 SUBMISSION FLOW
 ───────────────
  client ──(JSON: text, creator_id, content_type)──▶ POST /submit
                                                     │
                                     [Flask-Limiter: 10/min, 100/day per IP] ──over──▶ 429 JSON error
                                                     │ ok
                                     [validate: 20–10,000 chars, creator_id present] ──bad──▶ 400
                                                     │ raw text
                 ┌───────────────────────────────────┼────────────────────────────────────┐
                 ▼ raw text                          ▼ raw text                           ▼ raw text
     Signal 1: LLM judge (Groq)        Signal 2: Stylometrics (pure Py)     Signal 3: Lexical markers (pure Py)
     → llm_score ∈[0,1] + reasoning    → stylometric_score ∈[0,1] + metrics → lexical_score ∈[0,1] + hits
                 └───────────────────────────────────┼────────────────────────────────────┘
                                                     ▼ three signal scores (+ word count)
                              Scoring: weighted mean (0.50 / 0.30 / 0.20)
                                       → disagreement shrink → short-text shrink
                                       → ai_score ∈[0,1], confidence ∈[0.5,1], attribution
                                                     ▼ attribution + status
                              Label builder → label {variant, headline, body}
                                                     ▼ full record
                              Storage: contents table (current status) + audit_log (append-only event)
                                                     ▼
  client ◀──(JSON: content_id, attribution, ai_score, confidence, signals{}, label, status)──┘


 APPEAL FLOW
 ───────────
  creator ──(JSON: content_id, creator_id, creator_reasoning)──▶ POST /appeal   [limit 5/hour per IP]
                                                     │
                     [content exists? 404] [creator matches? 403] [reasoning ≥ 20 chars? 400] [already open? 409]
                                                     │ ok
                              contents.status: classified ──▶ under_review ; label ──▶ "under review" variant
                                                     ▼ original decision snapshot + reasoning
                              audit_log: event "appeal_filed"
                                                     ▼
  creator ◀──(JSON: appeal_id, content_id, status "under_review", original decision, message)──┘

  reviewer ──▶ GET /appeals (queue: excerpt, scores, signals, reasoning, filed_at)
  reviewer ──(decision: upheld | overturned, note)──▶ POST /appeals/<content_id>/resolve
                              → status "resolved_upheld" | "resolved_overturned" → audit_log "appeal_resolved"
```

---

## 2. API contract

| Method | Path | Body / query | Returns |
|---|---|---|---|
| POST | `/submit` | `{text, creator_id, title?, content_type?="text"}` or image form (§9.4) | 201 + classification record |
| GET | `/content/<content_id>` | — | current record (status, label, scores) |
| POST | `/appeal` | `{content_id, creator_id, creator_reasoning}` | 201 + appeal confirmation |
| GET | `/appeals` | `?status=open` (default) | reviewer queue |
| POST | `/appeals/<content_id>/resolve` | `{decision: "upheld"\|"overturned", reviewer_note}` | updated record |
| POST | `/certificate` | `{content_id, creator_id, draft_text, process_notes}` (§9.2) | certificate or reasons it failed |
| GET | `/log` | `?limit=20` | `{"entries": [...]}` newest first |
| GET | `/stats` | — | analytics JSON (§9.3) |
| GET | `/dashboard` | — | HTML analytics page (§9.3) |
| GET | `/health` | — | `{status, llm_available}` |

`/submit` response shape:

```json
{
  "content_id": "uuid4",
  "creator_id": "test-user-1",
  "content_type": "text",
  "attribution": "likely_ai | uncertain | likely_human",
  "ai_score": 0.87,
  "confidence": 0.87,
  "status": "classified",
  "signals": {
    "llm":         {"score": 0.92, "available": true, "weight": 0.5, "details": {"reasoning": "..."}},
    "stylometric": {"score": 0.78, "available": true, "weight": 0.3, "details": {"sentence_length_cv": 0.18, "...": "..."}},
    "lexical":     {"score": 0.85, "available": true, "weight": 0.2, "details": {"ai_phrases": ["it is important to note"], "...": "..."}}
  },
  "adjustments": ["signal_disagreement", "short_text"],
  "label": {"variant": "high_confidence_ai", "headline": "...", "body": "..."},
  "timestamp": "2026-10-05T14:32:10.123Z"
}
```

Errors are always JSON: `{"error": "<code>", "message": "<human sentence>"}`.

---

## 3. Detection signals

Every signal returns the same shape so scoring can treat them uniformly:

```python
{"name": "llm", "score": 0.0–1.0 or None, "available": bool, "details": {...}}
```

`score` is always **AI-likelihood** (0 = reads fully human, 1 = reads fully AI). `available: False` means the signal could not
run (API down, text too short to measure). The signal is then dropped and the remaining weights are renormalised.

### Signal 1 — LLM judge (Groq, `openai/gpt-oss-120b`)
- **Measures:** holistic semantic and stylistic coherence: generic "assistant voice", hedged balanced framing, absence of
  lived specifics, over-smooth transitions.
- **Why it differs between human and AI writing:** instruction-tuned models write in a recognisable register (balanced,
  impersonal, summarising). Another LLM is good at recognising that register.
- **Output:** the model is prompted to reply with JSON `{"ai_likelihood": 0–100, "reasoning": "<one sentence>"}` at
  temperature 0. We divide by 100 and clamp. If the JSON can't be parsed or the API errors, `available=False`.
- **Blind spots:** LLMs are badly calibrated and over-confident. They tend to call any formal or non-native English "AI".
  The output isn't deterministic across model versions, and it's easily fooled by AI text with injected typos/slang.
- **Note:** the handout's `meta-llama/llama-4-scout-17b-16e-instruct` is no longer served by Groq (404 / not in model list).
  We use `openai/gpt-oss-120b`, falling back to `openai/gpt-oss-20b`.

### Signal 2 — Stylometric heuristics (pure Python)
- **Measures:** structural statistics:
  1. **Sentence-length burstiness**: coefficient of variation (stdev/mean) of words per sentence. Human writing mixes
     3-word and 30-word sentences (CV ≈ 0.5–0.9). AI prose is metronomic (CV ≈ 0.15–0.35).
     Sub-score `= ramp(cv, human=0.70 → 0.0, ai=0.20 → 1.0)`.
  2. **Vocabulary diversity**: moving-average type-token ratio (MATTR, window 50 words, so long texts aren't penalised).
     AI text reuses a "safe" mid-frequency vocabulary but also avoids repetition. In practice the strongest tell is
     *long average word length* with *moderate-high TTR*. Sub-score from average word length:
     `ramp(avg_word_len, human=4.2 → 0.0, ai=5.6 → 1.0)`.
  3. **Punctuation texture**: rate of "human" punctuation (`? ! … — ( ) "`, ellipses, ALL-CAPS words) per sentence. AI
     prose is mostly commas and periods. Sub-score `= ramp(informal_punct_per_sentence, human=0.8 → 0.0, ai=0.0 → 1.0)`.
  `ramp(x, a→0, b→1)` = linear interpolation clamped to [0,1].
  **Stylometric score = 0.45·burstiness + 0.35·word_length + 0.20·punctuation.**
- **Requires** ≥ 3 sentences and ≥ 40 words, otherwise `available=False` (statistics on 2 sentences are noise).
- **Blind spots:** blind to meaning. Formal human writing (academic, legal, technical) looks AI-uniform. Poetry with line
  breaks and no periods breaks sentence segmentation. AI told to "vary sentence length" defeats it.

### Signal 3 — Lexical markers (pure Python) *(stretch: ensemble)*
- **Measures:** frequency of known LLM stock phrases ("it is important to note", "furthermore", "delve", "tapestry",
  "in today's fast-paced world", "stakeholders", "paradigm shift", "plays a crucial role", ...) versus informal human
  markers (lowercase sentence-initial "i", "lol", "honestly", "kinda", "gonna", contractions, first-person anecdotes).
- **Output:** `ai_hits_per_100w` and `human_hits_per_100w` → `score = clamp(0.5 + 0.18·ai_rate − 0.12·human_rate, 0, 1)`.
  It starts at 0.5 (no evidence either way).
- **Why it's distinct:** signals 1 and 2 judge overall register and shape; this one counts specific surface tokens. It
  can fire when the other two are fooled (formal-looking but cliché-heavy text), and it is fully explainable ("matched:
  'it is important to note', 'furthermore'").
- **Blind spots:** trivially evaded by find-and-replace. Penalises human writers who use "furthermore" (students, ESL
  writers taught formal connectives). The phrase list goes stale as models change.

### How signals combine
```
weights = {llm: 0.50, stylometric: 0.30, lexical: 0.20}       # renormalised over available signals
raw     = Σ wᵢ·scoreᵢ / Σ wᵢ
```
The LLM gets the most weight because it is the most informative in tests. It is capped at 50% so it can never decide
alone: a 0.95 LLM score with two neutral heuristics (0.5) yields ≈ 0.73, which is **uncertain**, not AI.

---

## 4. Uncertainty representation

### Two numbers
- **`ai_score`** ∈ [0,1]: the system's estimated probability-like AI-likelihood after adjustments.
- **`confidence`** ∈ [0.5,1]: how sure we are *of the attribution we're showing* = `max(ai_score, 1 − ai_score)`.
  So "likely_ai @ 0.87" and "likely_human @ 0.87" are both "fairly sure". A confidence of 0.6 means **"slightly better than
  a coin flip — we would not act on this"**, and it always lands in the uncertain band.

### Adjustments (applied in order, each recorded in `adjustments[]`)
1. **Signal disagreement:** if `max(scores) − min(scores) > 0.45`, shrink toward 0.5 by 35%:
   `s = 0.5 + (s − 0.5)·0.65`. Signals arguing = we don't know.
2. **Short text:** if `< 60 words`, shrink by 40%. Under 60 words nothing is reliable.
3. **Only one signal available** (e.g. Groq down and text too short for stylometrics): shrink by 50%.

### Thresholds (asymmetric on purpose)
| ai_score | attribution | label variant | rationale |
|---|---|---|---|
| **≥ 0.80** | `likely_ai` | high_confidence_ai | high bar: false positives hurt real people |
| **0.35 – 0.80** | `uncertain` | uncertain | wide middle band: we'd rather say "don't know" |
| **≤ 0.35** | `likely_human` | high_confidence_human | lower bar: the cost of being wrong is smaller |

So 0.51 → uncertain ("Origin unclear"), 0.95 → likely_ai. The answer changes, not just the number.

### Validation plan
Run at least these 4 inputs (handout set) plus 4 of my own (short poem, ESL-style formal paragraph, AI text with injected
slang, personal diary entry). Record every signal score separately in `docs/calibration.md`. Pass criteria:
clear AI ≥ 0.80, clear human ≤ 0.35, both borderlines in 0.35–0.80. If a case misses, print signals separately and tune the
ramp endpoints, **not** the thresholds (thresholds are product decisions; ramps are measurement).

---

## 5. Transparency label design

Shown to readers under the piece. No jargon, no raw decimals in the headline. The label text is fixed per variant so it can
be reviewed, translated and audited.

| Variant | Headline | Body |
|---|---|---|
| **high_confidence_ai** (ai_score ≥ 0.80) | **Likely AI-generated** | "Our automated checks found strong signs that this piece was mostly or entirely generated by an AI tool. Automated checks can be wrong, and the creator can appeal this label." |
| **uncertain** (0.35–0.80) | **Origin unclear** | "Our automated checks couldn't tell with confidence whether a person or an AI tool wrote this piece. This note is here to give you context. It is not an accusation." |
| **high_confidence_human** (≤ 0.35) | **Likely human-written** | "Our automated checks found the patterns we usually see in a person's own writing, and no strong signs of AI generation." |
| under_review (appeal open) | **Label under review** | "The creator has asked for this piece's label to be reviewed by a person. Until that review is done, treat the earlier automated result with caution." |
| verified_human (§9.2) | **Verified human: creator shared their drafts** | "The creator completed an extra verification step by sharing an earlier draft that shows how this piece was revised over time. Certificate {certificate_id}, issued {date}." |

A small detail line under the body gives the confidence in words, not a decimal:
`confidence ≥ 0.90 → "Our confidence: high"`, `0.75–0.90 → "Our confidence: moderate"`, `< 0.75 → "Our confidence: low"`.

Design choices: the AI label is the only one that mentions appeals, because it is the label that can harm someone. The
uncertain label explicitly disclaims accusation. The human label says "no strong signs" rather than "proven human",
because we can't prove absence.

---

## 6. Appeals workflow

- **Who:** only the creator of the content (`creator_id` must match the submission's `creator_id`). In production this
  would be the authenticated session user. Here the id in the body stands in for auth.
- **Provides:** `content_id`, `creator_id`, `creator_reasoning` (20–2,000 chars), optional `evidence_url`.
- **System does:**
  1. 404 if the content is unknown, 403 if the creator doesn't match, 400 if the reasoning is missing or too short, 409 if
     an appeal is already open.
  2. `contents.status` ← `under_review`, store `appeal_reasoning` and `appeal_filed_at`, and switch the label to the
     under_review variant.
  3. Append audit event `appeal_filed` containing the **original decision snapshot** (attribution, ai_score, confidence,
     every signal score) and the reasoning, so the appeal sits next to the decision it contests.
  4. Return 201 `{appeal_id, content_id, status: "under_review", original_decision, message}`.
- **Reviewer view (`GET /appeals`):** for each open appeal: content_id, creator_id, filed_at, the creator's reasoning, a
  300-char excerpt of the text, original attribution + ai_score + confidence, per-signal scores, the LLM's one-sentence
  reasoning, matched lexical phrases, and stylometric metrics. Oldest first (FIFO fairness).
- **Resolution:** `POST /appeals/<id>/resolve` with `upheld` (keep the automated label) or `overturned` (switch to
  `likely_human`), plus a reviewer note. Status becomes `resolved_upheld` / `resolved_overturned`, and an `appeal_resolved`
  audit event is written. No automated re-classification.

---

## 7. Anticipated edge cases

1. **Repetitive, simple-vocabulary poetry.** A villanelle or a children's poem repeats whole lines and uses short words
   ("the rain, the rain, it falls again"). Repetition lowers vocabulary diversity, and line-based verse with few periods
   becomes one or two giant "sentences", so burstiness can't be measured. Mitigation: <3 sentences → stylometrics
   unavailable. Repetition is not treated as an AI tell. Expect "uncertain", not "AI".
2. **Non-native English writers using textbook-formal connectives.** "Furthermore, it is important to consider..." is
   exactly what ESL curricula teach. The lexical signal and the LLM both fire. Mitigation: the 0.80 AI threshold plus the
   50% LLM weight cap means that even two signals agreeing usually lands in uncertain unless stylometrics also agrees. The
   appeal path exists for this case (the handout's appeal example is exactly this).
3. **Formal human academic/technical prose.** The handout's monetary-policy paragraph has uniform sentence length and long
   words, so stylometrics reads it as AI. It should land in uncertain because the casual-marker signal is neutral.
4. **AI text with injected casual noise** ("lol", typos, lowercase). This defeats lexical and partly stylometrics. The LLM
   may still catch the generic structure, so the signals disagree, the disagreement shrink applies, and the result is
   uncertain. That is acceptable: we prefer missing it to a false accusation.
5. **Very short submissions** (a haiku, a two-line caption). There isn't enough signal. The short-text shrink
   guarantees uncertain.
6. **Mixed human/AI pieces** (human draft polished by AI). The system outputs one document-level score, so the answer is
   honestly "uncertain". Per-paragraph attribution is out of scope.

---

## 8. Production layer

### Rate limiting (Flask-Limiter, keyed by client IP, in-memory storage)
- `/submit`: **10 per minute; 100 per day.**
  - A real writer submits a few pieces per session. 10/min still lets someone upload a small poetry collection in one
    sitting without hitting the wall.
  - 100/day is an order of magnitude above even a very prolific poster (≈ 5–20 pieces/day), but stops a script from using
    us as a free AI detector or probing the classifier thousands of times to learn how to evade it.
  - Each submit makes one Groq call. Groq's free tier for gpt-oss-120b is ~30 requests/min, so 10/min per client keeps one
    abuser from exhausting the shared LLM quota.
- `/appeal`: **5 per hour.** Appeals are rare and go to a human queue, so spam would waste reviewer time.
- `/certificate`: **5 per hour** (same reasoning).
- Exceeded limits return **429** JSON `{"error": "rate_limited", "message": ..., "limit": "10 per 1 minute"}`.

### Audit log (SQLite `provenance.db`, table `audit_log`, append-only)
Columns: `id, timestamp (ISO-8601 UTC, ms), event_type (submission | appeal_filed | appeal_resolved |
certificate_issued | certificate_denied), content_id, creator_id, content_type, attribution, ai_score, confidence,
llm_score, stylometric_score, lexical_score, signals_used (JSON list), status, label_variant, appeal_reasoning,
details (JSON)`.
A second table `contents` holds the current state of each piece (one row per content_id). The audit log is never updated
in place. `GET /log?limit=N` returns newest-first entries as JSON.

---

## 9. Stretch features (spec written before building them)

### 9.1 Ensemble detection
Three signals (§3), weighted 0.50 / 0.30 / 0.20. **Conflict resolution:** (a) weighted mean, so no signal can decide
alone; (b) if the spread between highest and lowest signal > 0.45 the result shrinks 35% toward 0.5; (c) missing signals
are dropped and weights renormalised, and a single surviving signal triggers a 50% shrink. Each signal's score and weight
appears in the response and in the audit log.

### 9.2 Provenance certificate: "Verified human: creator shared their drafts"
- **Verification step:** the creator submits an **earlier draft** of the same piece plus a short process note (≥ 40 chars:
  how and when they wrote it). Real writing has revision history. Someone passing off AI output usually has no earlier draft.
- **Checks:** (1) caller is the creator, (2) content's current attribution is not `likely_ai` (an AI-flagged piece must go
  through appeal first), (3) draft is ≥ 40 words, (4) `difflib.SequenceMatcher` similarity between draft and final is
  between **0.30 and 0.95**: similar enough to be the same piece, different enough to show real revision. Identical text is
  not a draft and unrelated text is not this piece.
- **Result:** certificate `{certificate_id: "PG-XXXXXXXX", content_id, creator_id, issued_at, method: "draft_history",
  draft_similarity}` is stored, the content's label switches to the `verified_human` variant (different headline, names the
  certificate id), and the audit event `certificate_issued` is written. Failures return 422 with the failed checks and are
  logged as `certificate_denied`.
- **Honest limitation:** a determined faker can ask an AI for a "rough draft". The certificate raises the cost of faking
  rather than proving authorship.

### 9.3 Analytics dashboard
`GET /stats` (JSON) and `GET /dashboard` (HTML that renders the stats). Metrics:
1. **Detection pattern:** count and % of likely_ai / uncertain / likely_human.
2. **Appeal rate:** appeals filed ÷ submissions, plus **overturn rate** (overturned ÷ resolved).
3. **Signal disagreement rate** *(my extra metric)*: % of submissions where the disagreement shrink fired. A rising rate
   means the signals are drifting apart (e.g. a new model the phrase list doesn't know). It's an early warning that
   detection is degrading.
4. Also shown: average confidence, submissions by content type, certificates issued.

### 9.4 Multi-modal: image submissions via structured metadata
`POST /submit` with `content_type: "image"`, `creator_id`, `metadata` (object) and optional `description` (alt-text/caption).
Signals for images:
- **Metadata provenance signal** (pure Python): AI generator names in `software`/`creator_tool` (Midjourney, DALL·E,
  Stable Diffusion, Firefly, ComfyUI, Automatic1111, Leonardo, Ideogram, Flux) → 0.95. IPTC
  `digital_source_type == "trainedAlgorithmicMedia"` → 0.97. `parameters`/`prompt`/`seed`/`cfg_scale` keys → 0.9. Camera
  `make` + `model` present with exposure data → 0.15. Generator-typical square sizes (512/768/1024/2048 px, multiple of
  64) add +0.1. No evidence → 0.5.
- **LLM judge on the description + metadata** (Groq): same JSON contract as text signal 1.
- Weights: metadata 0.65, LLM 0.35 (metadata is harder evidence than prose). The same thresholds, labels, log and
  appeals apply.
- **Blind spot:** metadata is trivially stripped (screenshots, social-media re-encodes), so stripped metadata gives 0.5,
  which leads to uncertain.

---

## AI Tool Plan

| Milestone | Spec sections given to the AI tool | What I ask it to generate | How I verify |
|---|---|---|---|
| **M3**: submission endpoint + first signal | §3 Signal 1, §2 API contract, **Architecture diagram** | Flask app skeleton with `POST /submit` stub returning a hardcoded response; `llm_signal(text)` returning the §3 shape; `GET /log`; SQLite audit-log helper | Call `llm_signal()` directly on the 4 handout texts and inspect score + reasoning before wiring. curl `/submit` and check content_id/attribution/confidence/label keys. Check that `/log` shows a structured row |
| **M4**: second signal + confidence scoring | §3 Signals 2–3 + "How signals combine", §4 Uncertainty (thresholds table + adjustments), **diagram** | `stylometric_signal`, `lexical_signal`, `combine_signals()` | Unit-test that the thresholds match §4 exactly (0.80 / 0.35, shrink factors). Run the 8 calibration texts and print each signal separately. Clear AI must score clearly higher than clear human |
| **M5**: production layer | §5 Label table, §6 Appeals, §8 Rate limiting + audit-log schema, **diagram** | `build_label()`, `POST /appeal`, `GET /appeals`, resolve endpoint, Flask-Limiter config | Force all 3 label variants (unit test on boundary scores 0.80/0.35/0.799). File an appeal and confirm `status: under_review` + `appeal_reasoning` in `/log`. Run the 12-request curl loop and confirm 10×200/201 then 429 |
| Stretch | §9.1–9.4 | certificate endpoint, `/stats` + `/dashboard`, image metadata signal | Tests per feature. Manual curl of each |

Rules I'm holding myself to: give the AI the spec section verbatim, never "make an AI detector". Diff the output against the
spec numbers. Any threshold the AI "improves" gets reverted to the spec value.
