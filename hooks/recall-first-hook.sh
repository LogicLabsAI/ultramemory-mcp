#!/usr/bin/env bash
# UltraMemory recall-first hook for Claude Code (UserPromptSubmit event).
# On every prompt it recalls your top matches from UltraMemory and injects them as context.
# Fail-open by design: any error injects nothing and exits 0, so it can never block your prompt.
set -uo pipefail

API_BASE="${ULTRAMEMORY_API_BASE:-https://api.ultramemory.us}"
API_KEY="${ULTRAMEMORY_API_KEY:-}"
SCOPE="${ULTRAMEMORY_SCOPE:-}"   # optional: recall only this project's scope (opt-in; empty = account default)

payload="$(cat)"                               # the UserPromptSubmit JSON on stdin
command -v curl    >/dev/null 2>&1 || exit 0   # fail-open if prerequisites are missing
command -v python3 >/dev/null 2>&1 || exit 0
[ -n "$API_KEY" ] || exit 0

prompt="$(printf '%s' "$payload" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("prompt",""))
except Exception: pass')"
[ -n "$prompt" ] || exit 0

body="$(printf '%s' "$prompt" | UM_SCOPE="$SCOPE" python3 -c 'import json,os,sys
req = {"query": sys.stdin.read(), "k": 5}
scope = os.environ.get("UM_SCOPE", "").strip()
if scope:
    req["scope"] = scope
print(json.dumps(req))')"

resp="$(printf '%s' "$body" | curl -s --max-time 6 -X POST "$API_BASE/api/v1/recall" \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" --data @-)" || exit 0

printf '%s' "$resp" | python3 -c '
import json, sys
try:
    results = (json.load(sys.stdin).get("results") or [])
except Exception:
    sys.exit(0)
if not results:
    sys.exit(0)
lines = ["[UltraMemory] Relevant saved memories — ground your answer in these:"]
for r in results:
    val = (r.get("value") or "").strip()
    if not val:
        continue
    label = " ".join(x for x in ((r.get("entity") or "").strip(), (r.get("key") or "").strip()) if x)
    lines.append(f"- {label}: {val}" if label else f"- {val}")
if len(lines) == 1:
    sys.exit(0)
print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "\n".join(lines)}}))
'
exit 0
