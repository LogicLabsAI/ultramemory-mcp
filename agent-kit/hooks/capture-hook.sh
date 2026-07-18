#!/usr/bin/env bash
# UltraMemory capture hook for Claude Code (Stop event).
# When a turn finishes it sends the FULL turn — the last real user message plus every assistant
# text block AND every tool_result from that turn — to UltraMemory, which extracts and saves the
# durable facts (deduped server-side). Every Nth turn it also emits an in-band snapshot nudge
# (see ULTRAMEMORY_SNAPSHOT_EVERY) so the model authors a wayback-grade session snapshot itself.
# Limit-aware (v1.8.0): a 429 limit_reached reply is surfaced to the user via the hook's
# systemMessage JSON field (writes are paused + when they resume) instead of being discarded.
# Observable (v1.9.4): a dead key (401/403) exits before the snapshot nudge, and every run
# appends one metadata-only receipt line to ~/.ultramemory/hook.log (ULTRAMEMORY_HOOK_LOG=off
# disables) — never transcript contents, memory contents, or the key.
# Fail-open by design: any error captures nothing and exits 0, so it can never block Claude Code.
set -uo pipefail

API_BASE="${ULTRAMEMORY_API_BASE:-https://api.ultramemory.us}"
API_KEY="${ULTRAMEMORY_API_KEY:-}"

payload="$(cat)"                               # the Stop event JSON on stdin
command -v curl    >/dev/null 2>&1 || exit 0   # fail-open if prerequisites are missing
command -v python3 >/dev/null 2>&1 || exit 0
[ -n "$API_KEY" ] || exit 0

# T3 (1.9.4): append ONE metadata-only receipt per invocation to ~/.ultramemory/hook.log —
# ISO ts | capture | outcome | duration_ms | injected_chars | retry_used. NEVER transcript
# contents, memory contents, or the key (Rule 6). ULTRAMEMORY_HOOK_LOG=off disables. Entirely
# best-effort/fail-open: any logging failure is swallowed and the hook continues unaffected.
# (The stop_hook_active loop-prevention exit below is a same-turn duplicate, not a capture
# attempt, so it is intentionally not receipted.)
UM_T0=0
[ "${ULTRAMEMORY_HOOK_LOG:-on}" = "off" ] || UM_T0="$(python3 -c 'import time; print(int(time.monotonic()*1000))' 2>/dev/null || printf 0)"
hook_log() {
  [ "${ULTRAMEMORY_HOOK_LOG:-on}" = "off" ] && return 0
  UM_OUTCOME="${1:-abstain}" UM_INJECTED="${2:-0}" UM_T0="$UM_T0" python3 - <<'PYLOG' 2>/dev/null || true
import datetime, os, time

try:
    t0 = int(os.environ.get("UM_T0") or 0)
    duration_ms = max(0, int(time.monotonic() * 1000) - t0) if t0 else 0
    directory = os.path.join(os.path.expanduser("~"), ".ultramemory")
    path = os.path.join(directory, "hook.log")
    # dir 0700 / file 0600 + os.replace rotation at 1 MiB (single generation) — the same
    # on-disk idiom as cli.py _write_cache_skeleton.
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    try:
        if os.path.getsize(path) > 1048576:
            os.replace(path, path + ".1")
    except OSError:
        pass
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    outcome = os.environ.get("UM_OUTCOME") or "abstain"
    injected = os.environ.get("UM_INJECTED") or "0"
    with open(path, "a", encoding="utf-8") as f:
        f.write("%s | capture | %s | %d | %s | 0\n" % (ts, outcome, duration_ms, injected))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
except Exception:
    pass
PYLOG
  return 0
}

# Loop prevention: skip when this Stop event was raised by a stop hook already running.
# This guard runs BEFORE any stdout, so the Nth-turn nudge below can never fire in a loop.
stop_active="$(printf '%s' "$payload" | python3 -c 'import json,sys
try: print(str(json.load(sys.stdin).get("stop_hook_active", False)).lower())
except Exception: pass')"
[ "$stop_active" = "true" ] && exit 0

transcript_path="$(printf '%s' "$payload" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("transcript_path",""))
except Exception: pass')"
[ -n "$transcript_path" ] || { hook_log abstain; exit 0; }
[ -r "$transcript_path" ] || { hook_log abstain; exit 0; }

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
)" || { hook_log abstain; exit 0; }
[ -n "$body" ] || { hook_log abstain; exit 0; }

# P3-C1(b): stop discarding the capture response — a 429 limit_reached must reach the USER
# (writes are paused + when they resume), not /dev/null. curl -w appends the HTTP status code on
# its own final line; any curl/parse trouble emits nothing and the hook still exits 0 (fail-open,
# never blocking). On a 429 the friendly server detail is emitted via the hook JSON-output
# `systemMessage` field (shown to the user, non-blocking — never exit 2 / decision:block) and the
# Nth-turn snapshot nudge below is skipped (its memory_write would hit the same paused-writes limit).
curl_rc=0
response="$(printf '%s' "$body" | curl -s --max-time "${ULTRAMEMORY_HOOK_DEADLINE:-9}" -w '\n%{http_code}' -X POST "$API_BASE/api/v1/capture" \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  --data @- 2>/dev/null)" || { curl_rc=$?; response=""; }
http_code="${response##*$'\n'}"
# T3 (1.9.4): classify this capture attempt for the receipt line (dead_key and http_429 log at
# their own early-exits below; a later Nth-turn nudge upgrades a clean "abstain" to "injected").
um_injected=0
if [ "$curl_rc" -eq 28 ]; then
  um_outcome="timeout"                                # curl 28 = --max-time hit
else
  case "$http_code" in
    2??) um_outcome="abstain" ;;                      # captured fine; nothing injected (yet)
    [0-9][0-9][0-9]) um_outcome="http_${http_code}" ;;
    *) um_outcome="http_000" ;;                       # curl failed before any HTTP status
  esac
fi
if [ "$http_code" = "429" ]; then
  printf '%s' "${response%$'\n'*}" | python3 -c 'import json, sys
try:
    detail = json.load(sys.stdin).get("detail")
except Exception:
    detail = None
if not (isinstance(detail, str) and detail.strip()):
    detail = "usage limit reached — memory writes pause and resume automatically; your memories are safe."
print(json.dumps({"systemMessage": "[UltraMemory] " + detail.strip()}))' 2>/dev/null || true
  hook_log "$um_outcome"
  exit 0
fi

# T2 (1.9.4): dead key — exit BEFORE the Nth-turn snapshot nudge below (mirror of the 429
# early-exit above), so a dead key never solicits doomed memory_write calls. The key is NEVER
# echoed, in whole or in part. A dead key is a 401, or a 403 whose body parses as the API's own
# JSON {"detail": ...} auth/suspension shape; a 403 WITHOUT that shape (e.g. a WAF block page)
# keeps um_outcome="http_403" and falls through to the normal hook_log path below.
if [ "$http_code" = "401" ]; then
  hook_log dead_key
  exit 0
fi
if [ "$http_code" = "403" ]; then
  if printf '%s' "${response%$'\n'*}" | python3 -c 'import json, sys
try:
    body = json.load(sys.stdin)
except Exception:
    sys.exit(1)
sys.exit(0 if isinstance(body, dict) and "detail" in body else 1)' 2>/dev/null; then
    hook_log dead_key
    exit 0
  fi
fi

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
[ "$snapshot_every" -eq 0 ] && { hook_log "$um_outcome"; exit 0; }   # disabled

session_id="$(printf '%s' "$payload" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("session_id",""))
except Exception: pass')"
[ -n "$session_id" ] || { hook_log "$um_outcome"; exit 0; }

counter_file="${TMPDIR:-/tmp}/ultramemory-turns-${session_id}"
count="$(cat "$counter_file" 2>/dev/null || printf 0)"
case "$count" in ''|*[!0-9]*) count=0 ;; esac
count=$((count + 1))
printf '%s' "$count" > "$counter_file" 2>/dev/null || true

if [ "$((count % snapshot_every))" -eq 0 ]; then
  nudge_json="$(python3 -c 'import json
nudge = ("Compose a session snapshot per the ultramemory-snapshot Skill rubric "
         "(blocker → approaches tried → what worked → how verified; wayback rules: "
         "named entities, absolute dates, concrete values) and save it with the UltraMemory "
         "memory_write tool now.")
print(json.dumps({"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": nudge}}))')"
  if [ -n "$nudge_json" ]; then
    printf '%s\n' "$nudge_json"
    # T3: nudge emitted — a clean run logs "injected"; an http_*/timeout outcome keeps
    # precedence (same rule as the recall hook) with injected_chars still recorded.
    [ "$um_outcome" = "abstain" ] && um_outcome="injected"
    um_injected="${#nudge_json}"
  fi
fi
hook_log "$um_outcome" "$um_injected"
exit 0
