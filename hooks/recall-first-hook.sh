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
# max_characters caps the assembled briefing so the injected additionalContext stays under the
# Claude Code ~10k limit; the server degrades an oversized hard policy to a head + fetch-pointer.
req = {"query": sys.stdin.read(), "k": 5, "max_characters": 9000}
scope = os.environ.get("UM_SCOPE", "").strip()
if scope:
    req["scope"] = scope
print(json.dumps(req))')"

resp="$(printf '%s' "$body" | curl -s --max-time 8 -X POST "$API_BASE/api/v1/recall/gated" \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" --data @-)" || exit 0

printf '%s' "$resp" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
# Metamemory gate: when memory is unsure/empty it abstains — stay quiet, inject nothing.
if data.get("decision") == "abstain":
    sys.exit(0)
# The gated response ships a ready-to-use sectioned briefing (tier T1) in context_block.
block = (data.get("context_block") or "").strip()
if not block:
    sys.exit(0)
print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": block}}))
'
exit 0
