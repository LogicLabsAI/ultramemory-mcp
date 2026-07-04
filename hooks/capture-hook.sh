#!/usr/bin/env bash
# UltraMemory capture hook for Claude Code (Stop event).
# When a turn finishes it sends the FULL turn — the last real user message plus every assistant
# text block AND every tool_result from that turn — to UltraMemory, which extracts and saves the
# durable facts (deduped server-side). Every Nth turn it also emits an in-band snapshot nudge
# (see ULTRAMEMORY_SNAPSHOT_EVERY) so the model authors a wayback-grade session snapshot itself.
# Fail-open by design: any error captures nothing and exits 0, so it can never block Claude Code.
set -uo pipefail

API_BASE="${ULTRAMEMORY_API_BASE:-https://api.ultramemory.us}"
API_KEY="${ULTRAMEMORY_API_KEY:-}"

payload="$(cat)"                               # the Stop event JSON on stdin
command -v curl    >/dev/null 2>&1 || exit 0   # fail-open if prerequisites are missing
command -v python3 >/dev/null 2>&1 || exit 0
[ -n "$API_KEY" ] || exit 0

# Loop prevention: skip when this Stop event was raised by a stop hook already running.
# This guard runs BEFORE any stdout, so the Nth-turn nudge below can never fire in a loop.
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
import datetime, json, sys


def _content(entry):
    return (entry.get("message") or {}).get("content")


def text_blocks(entry):
    """Concatenated text from a message's text blocks (or raw string content)."""
    content = _content(entry)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()
    return ""


def tool_result_text(block):
    """Text carried by one tool_result block (its content may be a string or a block list)."""
    c = block.get("content")
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        parts = [b.get("text", "") for b in c
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()
    return ""


try:
    with open(sys.argv[1], encoding="utf-8") as f:
        lines = f.readlines()
except Exception:
    sys.exit(0)

entries = []
for line in lines:
    try:
        entries.append(json.loads(line))
    except Exception:
        continue

# Walk back to the LAST real user message: type=="user", not isMeta, with actual text.
# (tool_result-only "user" entries have no text and mark the turn's tool output, not its start.)
ui = -1
user_text = ""
for i in range(len(entries) - 1, -1, -1):
    e = entries[i]
    if not isinstance(e, dict) or e.get("isMeta"):
        continue
    if e.get("type") == "user":
        t = text_blocks(e)
        if t:
            ui = i
            user_text = t
            break

if ui < 0 or not user_text:
    sys.exit(0)

# assistant_text = every assistant text block AND every tool_result text AFTER that user
# message, in transcript order; tool results prefixed so the extractor can read them.
parts = []
for e in entries[ui + 1:]:
    if not isinstance(e, dict) or e.get("isMeta"):
        continue
    if e.get("type") == "assistant":
        t = text_blocks(e)
        if t:
            parts.append(t)
    content = _content(e)
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tr = tool_result_text(b)
                if tr:
                    parts.append("[tool result]\n" + tr)

assistant_text = "\n".join(parts).strip()
observed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
print(json.dumps({"user_text": user_text[:24000],
                  "assistant_text": assistant_text[:24000],
                  "observed_at": observed_at,
                  "source": "claude-code"}))
PY
)" || exit 0
[ -n "$body" ] || exit 0

printf '%s' "$body" | curl -s --max-time 8 -X POST "$API_BASE/api/v1/capture" \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  --data @- >/dev/null 2>&1

# --- Every Nth turn: in-band session-snapshot nudge (Stop additionalContext, CC >= 2.1.163) ---
# stop_hook_active already exited above, so this stdout costs exactly ONE continuation of the
# 8-continuation cap. The rubric lives in the ultramemory-snapshot Skill; this nudge stays tiny
# (well under the 10,000-char output cap). ULTRAMEMORY_SNAPSHOT_EVERY=0 disables it.
# ACCEPTED GAP (see hooks/README.md): Stop does not fire on user interrupts, and API-error turns
# fire StopFailure whose output is ignored — those turns are covered only by the per-turn capture
# above on the surrounding turns. On CC < 2.1.163 the field is silently inert (graceful).
snapshot_every="${ULTRAMEMORY_SNAPSHOT_EVERY:-5}"
case "$snapshot_every" in
  ''|*[!0-9]*) snapshot_every=5 ;;   # non-numeric -> default cadence
esac
[ "$snapshot_every" -eq 0 ] && exit 0   # disabled

session_id="$(printf '%s' "$payload" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("session_id",""))
except Exception: pass')"
[ -n "$session_id" ] || exit 0

counter_file="${TMPDIR:-/tmp}/ultramemory-turns-${session_id}"
count="$(cat "$counter_file" 2>/dev/null || printf 0)"
case "$count" in ''|*[!0-9]*) count=0 ;; esac
count=$((count + 1))
printf '%s' "$count" > "$counter_file" 2>/dev/null || true

if [ "$((count % snapshot_every))" -eq 0 ]; then
  python3 -c 'import json
nudge = ("Compose a session snapshot per the ultramemory-snapshot Skill rubric "
         "(blocker → approaches tried → what worked → how verified; wayback rules: "
         "named entities, absolute dates, concrete values) and save it with the UltraMemory "
         "memory_write tool now.")
print(json.dumps({"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": nudge}}))'
fi
exit 0
