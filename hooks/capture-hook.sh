#!/usr/bin/env bash
# UltraMemory capture hook for Claude Code (Stop event).
# When a turn finishes it sends the last user + assistant messages to UltraMemory,
# which extracts and saves the durable facts (deduped server-side).
# Fail-open by design: any error captures nothing and exits 0, so it can never block Claude Code.
set -uo pipefail

API_BASE="${ULTRAMEMORY_API_BASE:-https://api.ultramemory.us}"
API_KEY="${ULTRAMEMORY_API_KEY:-}"

payload="$(cat)"                               # the Stop event JSON on stdin
command -v curl    >/dev/null 2>&1 || exit 0   # fail-open if prerequisites are missing
command -v python3 >/dev/null 2>&1 || exit 0
[ -n "$API_KEY" ] || exit 0

# Loop prevention: skip when this Stop event was raised by a stop hook already running.
stop_active="$(printf '%s' "$payload" | python3 -c 'import json,sys
try: print(str(json.load(sys.stdin).get("stop_hook_active", False)).lower())
except Exception: pass')"
[ "$stop_active" = "true" ] && exit 0

transcript_path="$(printf '%s' "$payload" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("transcript_path",""))
except Exception: pass')"
[ -n "$transcript_path" ] || exit 0
[ -r "$transcript_path" ] || exit 0

body="$(python3 - "$transcript_path" <<'PY'
import json, sys

def text_of(entry):
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):   # keep text blocks; tool_result/tool_use blocks yield nothing
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()
    return ""

try:
    with open(sys.argv[1], encoding="utf-8") as f:
        lines = f.readlines()
except Exception:
    sys.exit(0)

user_text, assistant_text = "", ""
for line in reversed(lines):        # walk from the END for the LAST real user + assistant messages
    try:
        entry = json.loads(line)
    except Exception:
        continue
    if entry.get("isMeta"):         # skip meta entries
        continue
    kind = entry.get("type")
    if kind == "assistant" and not assistant_text:
        assistant_text = text_of(entry)
    elif kind == "user" and not user_text:
        user_text = text_of(entry)  # tool_result-only "user" entries have no text and stay skipped
    if user_text and assistant_text:
        break

if not user_text:
    sys.exit(0)
print(json.dumps({"user_text": user_text[:6000],
                  "assistant_text": assistant_text[:6000],
                  "source": "claude-code"}))
PY
)" || exit 0
[ -n "$body" ] || exit 0

printf '%s' "$body" | curl -s --max-time 8 -X POST "$API_BASE/api/v1/capture" \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  --data @- >/dev/null 2>&1
exit 0
