#!/usr/bin/env bash
# End-to-end demo of Provenance Guard. Start the server first:
#   PORT=5001 PROVENANCE_DB=demo.db .venv/bin/python app.py
# then:  bash scripts/demo.sh            (or BASE=http://localhost:5000 bash scripts/demo.sh)
# Step "ratelimit" is separate because it burns the per-minute quota:  bash scripts/demo.sh ratelimit
set -u
BASE="${BASE:-http://localhost:5001}"
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
pretty() { "$PY" -m json.tool; }
post() { curl -s -X POST "$BASE$1" -H "Content-Type: application/json" -d "$2"; }
step() { printf '\n\n===== %s =====\n' "$1"; }
field() { "$PY" -c "import sys,json; print(json.load(sys.stdin)['$1'])"; }

if [ "${1:-}" = "ratelimit" ]; then
  step "Rate limit: 12 rapid submissions (limit 10/minute)"
  for i in $(seq 1 12); do
    code=$(curl -s -o /tmp/pg_rl_body.json -w "%{http_code}" -X POST "$BASE/submit" \
      -H "Content-Type: application/json" \
      -d '{"text": "This is a test submission for rate limit testing purposes only.", "creator_id": "ratelimit-test"}')
    echo "request $i -> HTTP $code"
  done
  step "Body of the last (rate-limited) response"
  pretty < /tmp/pg_rl_body.json
  exit 0
fi

step "GET /health"
curl -s "$BASE/health" | pretty

step "1) POST /submit — clearly AI-generated text (expect likely_ai)"
R1=$(post /submit '{"creator_id": "demo-ai-poster", "title": "On AI", "text": "Artificial intelligence represents a transformative paradigm shift in modern society. It is important to note that while the benefits of AI are numerous, it is equally essential to consider the ethical implications. Furthermore, stakeholders across various sectors must collaborate to ensure responsible deployment."}')
echo "$R1" | pretty

step "2) POST /submit — clearly human-written text (expect likely_human)"
R2=$(post /submit '{"creator_id": "maya-writes", "title": "Ramen review", "text": "ok so i finally tried that new ramen place downtown and honestly? underwhelming. the broth was fine but they put WAY too much sodium in it and i was thirsty for like three hours after. my friend got the spicy version and said it was better. probably wont go back unless someone drags me there"}')
echo "$R2" | pretty

step "3) POST /submit — formal human writing by a non-native speaker (expect uncertain)"
R3=$(post /submit '{"creator_id": "linh-nguyen", "title": "Two school systems", "text": "In my country the education system is very different from here. Furthermore, it is important to consider that students must respect the teacher in all situations. When I was student in Hanoi, we did not ask questions during the class, because this is considered impolite. Moreover, the exams decide everything about your future. In conclusion, I think both systems have advantages and disadvantages, but I prefer that here the students can discuss with the professor."}')
echo "$R3" | pretty
CID3=$(echo "$R3" | field content_id)

step "4) POST /submit — image via structured metadata (multi-modal stretch)"
post /submit '{"creator_id": "pixel-artist", "content_type": "image", "description": "A hyper-detailed portrait of an astronaut cat floating in a nebula, cinematic lighting, 8k", "metadata": {"software": "Midjourney v6", "width": 1024, "height": 1024, "prompt": "astronaut cat, nebula, cinematic lighting --ar 1:1"}}' | pretty

step "5) POST /appeal — creator of submission 3 contests the label"
post /appeal "{\"content_id\": \"$CID3\", \"creator_id\": \"linh-nguyen\", \"creator_reasoning\": \"I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical.\"}" | pretty

step "6) GET /content/<id> — status is now under_review and the label changed"
curl -s "$BASE/content/$CID3" | pretty

step "7) GET /appeals — what a human reviewer sees"
curl -s "$BASE/appeals" | pretty

step "8) POST /certificate — creator of submission 2 shares an earlier draft (stretch)"
CID2=$(echo "$R2" | field content_id)
post /certificate "{\"content_id\": \"$CID2\", \"creator_id\": \"maya-writes\", \"process_notes\": \"Wrote this on my phone on the bus home right after dinner, then cleaned it up the next morning.\", \"draft_text\": \"ok so i tried the new ramen place downtown. underwhelming honestly. broth was fine but WAY too salty and i was thirsty for hours after. my friend got the spicy one and liked it more. probably not going back unless someone makes me\"}" | pretty

step "9) GET /log?limit=8 — structured audit log (newest first)"
curl -s "$BASE/log?limit=8" | pretty

step "10) GET /stats — analytics (dashboard HTML at $BASE/dashboard)"
curl -s "$BASE/stats" | pretty
