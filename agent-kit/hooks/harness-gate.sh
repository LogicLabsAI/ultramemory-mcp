#!/usr/bin/env bash
# Harness gate — global Stop hook.
# Enforces a project's verification command before Claude can end its turn, but ONLY when a harness
# run is armed in this project (presence of .claude/.harness-active). On other turns it is a no-op,
# so it never runs your test suite on unrelated work.
#
# Registered in ~/.claude/settings.json under hooks.Stop.
# Arm:    write the gate command as line 1 of .claude/.harness-active (checklist-bound-execution does
#         this in its step 0). Disarm: automatic on a green gate, or delete the file to abandon.
set -uo pipefail
cat >/dev/null    # consume the Stop event JSON on stdin (no field needed here)

GATE_FILE=".claude/.harness-active"
[ -f "$GATE_FILE" ] || exit 0                     # not a harness run -> never block normal sessions
GATE_CMD="$(head -n1 "$GATE_FILE")"
[ -n "$GATE_CMD" ] || { rm -f "$GATE_FILE"; exit 0; }

# Bounded loop so a genuinely-stuck gate can't spin forever (stays under Claude Code's 8-block cap).
ITER_FILE=".claude/.harness-iter"
N=$(( $(cat "$ITER_FILE" 2>/dev/null || echo 0) + 1 ))
MAX=6
if [ "$N" -gt "$MAX" ]; then
  rm -f "$GATE_FILE" "$ITER_FILE"
  jq -n '{systemMessage:"Harness gate still failing after max attempts — stopping for human review."}'
  exit 0
fi
echo "$N" > "$ITER_FILE"

# Run the project's gate. Login shell so PATH includes node/npm/etc. Output is captured, not emitted.
if OUTPUT="$(bash -lc "$GATE_CMD" 2>&1)"; then
  rm -f "$GATE_FILE" "$ITER_FILE"                 # verified green -> disarm and allow the stop
  exit 0
fi

# Gate failed -> block the stop and feed the failure back to Claude as its next instruction.
TAIL="$(printf '%s' "$OUTPUT" | tail -n 25)"
if command -v jq >/dev/null 2>&1; then
  jq -n --arg r "Harness gate FAILED (attempt $N/$MAX). Fix these before finishing:
$TAIL" '{decision:"block", reason:$r}'
  exit 0
else
  printf 'Harness gate FAILED (attempt %s/%s). Fix before finishing:\n%s\n' "$N" "$MAX" "$TAIL" >&2
  exit 2
fi
