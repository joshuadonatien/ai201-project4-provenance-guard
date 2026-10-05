# Provenance Guard: Walkthrough Video Script (~3 min)

The handout says short and unpolished is fine. Don't read this word for word. The "Say" lines are there so you
never go blank. Talk naturally, in your own words.

---

## How to record (macOS)

- **Option A:** press **Cmd + Shift + 5** → choose **"Record Selected Portion"** (or "Record Entire Screen") →
  **Options** → **Microphone: MacBook Microphone** (or your headset) → **Record**. To stop, click the stop
  button in the menu bar. The file is saved to the Desktop.
- **Option B:** open **QuickTime Player** → File → **New Screen Recording** → in the options, pick your microphone →
  Record.
- Do one 10-second test recording first to check that the mic is picked up.
- **Upload:** YouTube (visibility **Unlisted**) or Loom. Copy the link and **paste it into README.md where the
  video TODO is**. Open the link in a private window to make sure it plays without logging in.

## Pre-recording setup (do this before you hit record)

1. **Terminal tab 1 (server):**
   ```bash
   cd ~/ai201-project4-provenance-guard
   source .venv/bin/activate && rm -f demo.db && PORT=5001 PROVENANCE_DB=demo.db python app.py
   ```
   (Port 5000 on macOS is taken by AirPlay Receiver, which is why this uses 5001.)
2. **Terminal tab 2 (commands):**
   ```bash
   cd ~/ai201-project4-provenance-guard && source .venv/bin/activate
   curl -s http://localhost:5001/health | python3 -m json.tool     # expect "llm_available": true
   ```
   Increase the terminal font size (Cmd + `+`) so JSON is readable on video.
3. **Browser tab:** http://localhost:5001/dashboard. It will be empty now and fill in after the demo.
4. **Editor:** `planning.md` open, scrolled to the **Architecture → Diagram** section.
5. Health-check request done. **Don't send any submits before recording**, so the rate-limit quota stays fresh.

Tip: `bash scripts/demo.sh` prints a lot. You can run it once and scroll, or paste the individual commands below so
each result is on screen while you talk. The individual commands are copied from `scripts/demo.sh`.

---

## Shot list

### 0:00–0:20 · Intro
- **On screen:** editor showing `planning.md` (title + §1).
- **Say:** "Hi, I'm Joshua. This is Provenance Guard, my Project 4 for AI201. It's a backend a writing platform can
  call to estimate whether a piece was AI-generated, say how sure it is, show readers a plain-language label, and let
  creators appeal. My guiding rule was that calling a human's work AI is worse than missing an AI piece, and you'll
  see that in every design choice."

### 0:20–0:40 · Architecture
- **On screen:** the ASCII diagram in `planning.md` (Architecture → Diagram).
- **Say:** "A submission hits the rate limiter, gets validated, then goes to three independent signals: a Groq LLM
  judge running gpt-oss-120b, a stylometric analyzer for sentence rhythm, word length and punctuation, and a lexical
  analyzer that counts AI stock phrases against casual human markers. They're combined with weights 50, 30 and 20,
  pulled toward 0.5 when the evidence is weak, mapped to a label, and written to an append-only audit log."

### 0:40–1:25 · Three submissions, three different labels
- **On screen:** terminal tab 2. Paste each command and scroll to `attribution`, `confidence`, `signals` and `label`.

**1) Clear AI**
```bash
curl -s -X POST http://localhost:5001/submit -H "Content-Type: application/json" -d '{"creator_id": "demo-ai-poster", "title": "On AI", "text": "Artificial intelligence represents a transformative paradigm shift in modern society. It is important to note that while the benefits of AI are numerous, it is equally essential to consider the ethical implications. Furthermore, stakeholders across various sectors must collaborate to ensure responsible deployment."}' | python3 -m json.tool
```
- **Say:** "This is the handout's clear-AI paragraph. Each signal shows separately: in my run the LLM said 0.85,
  stylometric 0.839 and lexical 1.0, because it matched phrases like 'it is important to note'. Combined, that's
  likely AI at about 0.82, and the label says 'Likely AI-generated… the creator can appeal this label.'"

**2) Clear human**
```bash
curl -s -X POST http://localhost:5001/submit -H "Content-Type: application/json" -d '{"creator_id": "maya-writes", "title": "Ramen review", "text": "ok so i finally tried that new ramen place downtown and honestly? underwhelming. the broth was fine but they put WAY too much sodium in it and i was thirsty for like three hours after. my friend got the spicy version and said it was better. probably wont go back unless someone drags me there"}' | python3 -m json.tool
```
- **Say:** "The casual ramen review: all three signals are low, so ai_score is about 0.17 and confidence 0.83 the
  other way. The label is 'Likely human-written'."

**3) Formal human writing by a non-native speaker** (saved to a file so we can appeal it next)
```bash
curl -s -X POST http://localhost:5001/submit -H "Content-Type: application/json" -d '{"creator_id": "linh-nguyen", "title": "Two school systems", "text": "In my country the education system is very different from here. Furthermore, it is important to consider that students must respect the teacher in all situations. When I was student in Hanoi, we did not ask questions during the class, because this is considered impolite. Moreover, the exams decide everything about your future. In conclusion, I think both systems have advantages and disadvantages, but I prefer that here the students can discuss with the professor."}' | tee /tmp/pg_r3.json | python3 -m json.tool
```
- **Say:** "This is the hard case: an ESL writer using textbook connectives like 'Furthermore' and 'Moreover'.
  Stylometric and lexical lean AI, but the LLM says 0.3. The spread is over 0.45, so the disagreement rule pulls the
  score toward the middle, and it lands at about 0.55: 'Origin unclear… It is not an accusation.' Same pipeline,
  noticeably different confidence: 0.82, 0.83 and 0.55."

> Your LLM numbers may differ by a few hundredths from `docs/evidence/demo_run.txt`. Read what's on screen.

### 1:25–1:50 · Appeal → under_review → audit log
- **On screen:** terminal tab 2.
```bash
CID3=$(python3 -c "import json; print(json.load(open('/tmp/pg_r3.json'))['content_id'])")
curl -s -X POST http://localhost:5001/appeal -H "Content-Type: application/json" -d "{\"content_id\": \"$CID3\", \"creator_id\": \"linh-nguyen\", \"creator_reasoning\": \"I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical.\"}" | python3 -m json.tool
curl -s "http://localhost:5001/log?limit=3" | python3 -m json.tool
```
- **Say:** "The creator appeals with their reasoning. Status goes to under_review, and readers now see 'Label under
  review'. In the audit log, the newest entry is appeal_filed. It has the reasoning, plus a snapshot of the original
  decision with every signal score, right next to the original submission entry. The log is append-only, so the
  appeal never overwrites the evidence it's contesting."

### 1:50–2:10 · Rate limit (429)
- **On screen:** terminal tab 2.
```bash
bash scripts/demo.sh ratelimit
```
- **Say:** "Submit is limited to 10 per minute and 100 per day per IP. Each submit spends shared Groq quota, and the
  cap stops people from probing the detector to learn how to evade it. Here, after the 10-per-minute limit, I get
  429s with a JSON error body that says which limit was hit."
- **Note:** I've already done 3 submits this minute, so the 429 may start around request 8 instead of 11. Say
  that's why. For a clean "10 then 429", **restart the server (tab 1: Ctrl+C, then the same command) or wait 60 s**
  before running it.

### 2:10–2:30 · Stretch: dashboard, certificate, image (quick)
- **On screen:** run the rest of the stretch steps, then switch to the browser and refresh `/dashboard`.
```bash
curl -s -X POST http://localhost:5001/submit -H "Content-Type: application/json" -d '{"creator_id": "pixel-artist", "content_type": "image", "description": "A hyper-detailed portrait of an astronaut cat floating in a nebula, cinematic lighting, 8k", "metadata": {"software": "Midjourney v6", "width": 1024, "height": 1024, "prompt": "astronaut cat, nebula, cinematic lighting --ar 1:1"}}' | python3 -m json.tool
```
(If you hit the per-minute limit, wait a minute first. To show the certificate, run `bash scripts/demo.sh` after a
restart, or point at step 8 of `docs/evidence/demo_run.txt`.)
- **Say:** "Images go through the same pipeline using metadata: Midjourney in the software field, plus a prompt and a
  1024-square size, gives likely AI. The certificate lets a creator share an earlier draft. If the word-level
  similarity is between 0.3 and 0.95, they get a 'Verified human' label with a certificate id. The dashboard shows
  the detection pattern, appeal and overturn rates, and my own metric, the signal-disagreement rate, which is an
  early warning that the signals are drifting apart."

### 2:30–2:50 · Two design decisions
- **On screen:** `planning.md` §4 thresholds table, then `detection/scoring.py` `SHORT_TEXT_RULES` comment.
- **Say (asymmetric thresholds):** "First, the thresholds are asymmetric: AI needs 0.80, human only 0.35, and
  everything in between is 'Origin unclear'. A false AI label hurts a real person, so the system needs far more
  evidence before it accuses anyone. The LLM is also capped at 50% weight, so it can never decide alone."
- **Say (short-text change):** "Second, a place I changed my spec. I planned a flat 40% shrink for anything under
  60 words, but that meant a short text could never score above 0.80. The handout's 43-word AI sample got stuck at
  0.726. So I made it graded: 40% under 25 words, 15% from 25 to 59. Now short text gets flagged only when every
  signal strongly agrees. I tuned the measurement, not the threshold, and calibration passed 7 of 8."

### 2:50–3:00 · Closing
- **Say:** "Limitations: it's evadable by a determined user, metadata can be stripped, and formal human writing still
  lands in uncertain. But it's honest about what it doesn't know, and every decision is logged and appealable. Thanks
  for watching."

---

## If something goes wrong on camera

- **Groq is slow or unavailable:** the LLM signal shows `"available": false` with the error in `details`, and the
  system still answers using the other two signals with weights renormalised to 0.6 / 0.4. **That's a feature, so
  mention it:** "the LLM is down, and you can see the system degrades gracefully instead of crashing." Requests can
  take up to the 20-second Groq timeout, so just keep talking while you wait.
- **Port in use** (`Address already in use`):
  ```bash
  lsof -nP -iTCP:5001 -sTCP:LISTEN
  kill <PID>
  ```
  Then start the server again.
- **Unexpected 429 during the demo:** you've used the 10/min quota. Wait a minute (or restart the server, since the
  counters are in memory) and continue. You can say: "and that's the rate limiter doing its job."
- **`/tmp/pg_r3.json` missing / CID3 empty:** re-run submission 3 (the `tee` command), then the appeal.
- **"An appeal for this piece is already open" (409):** you already appealed it. That's the duplicate-appeal guard.
  Show `/log` and move on.
- **Numbers differ slightly from the docs:** the LLM isn't perfectly deterministic across runs. Read what's on
  screen. The attributions should match.
- **You stumble:** keep going. Unpolished is fine. Cut it in QuickTime (Edit → Trim) only if it's really long.
