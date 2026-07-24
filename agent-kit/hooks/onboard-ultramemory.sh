#!/bin/sh
# UltraMemory — session-start onboarding (companion to `ultramemory configure`).
#
# Persistable settings are handled by `ultramemory configure` (explicit per-item
# consent). SESSION-ONLY settings (ultracode / max effort; model when not persisted)
# cannot live in settings files — this hook is their vehicle: at session start it reads
# the platform's event JSON on stdin, and ONLY when the session is detectably running
# below its best available model/effort it emits the platform's context-injection
# output proposing a one-touch, this-session-only upgrade.
#
# Silent (exit 0, no output) when:
#   * the session is already optimal,
#   * the event supplies no model/effort info (never nag on a guess),
#   * ~/.ultramemory/onboard-optout exists (opt-out: `touch ~/.ultramemory/onboard-optout`).
#
# Platform schemas handled today: Claude Code SessionStart — stdin includes the active
# model (grounding: platform-claude-code.json session_start_hook); output is JSON with
# hookSpecificOutput.additionalContext (context for the agent) + systemMessage (shown
# to the user). Other platforms' session-start events (Codex hooks.json, Gemini hooks,
# Cursor sessionStart, Cline TaskStart, Windsurf/Devin hooks) pipe through the same
# detector; their adapters (checklist Clusters B/C) wire this script into each
# platform's hook config. Most accept the Claude-compatible JSON as-is — but NOT all:
# Copilot CLI honors ONLY a top-level additionalContext key, so its adapter invokes the
# kit hooks with ULTRAMEMORY_HOOK_SHAPE=copilot to select that output shape.
#
# POSIX sh; needs python3 (same dependency as every other kit hook). Always exits 0 —
# an onboarding nudge must never break a session.

[ -f "${HOME:-}/.ultramemory/onboard-optout" ] && exit 0
command -v python3 >/dev/null 2>&1 || exit 0

payload=$(cat 2>/dev/null || printf '')
[ -n "$payload" ] || exit 0

# Program arrives via heredoc-stdin; the event payload via argv (stdin is the heredoc).
python3 - "$payload" 2>/dev/null <<'PY'
import json
import sys

PROMPT = (
    "UltraMemory: session-only boosts available — reply 'approve model' / "
    "'approve ultracode' to apply for this session"
)

try:
    event = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
if not isinstance(event, dict):
    sys.exit(0)


def model_string(ev):
    m = ev.get("model")
    if isinstance(m, str):
        return m
    if isinstance(m, dict):
        for k in ("id", "model", "name", "display_name", "displayName"):
            v = m.get(k)
            if isinstance(v, str):
                return v
    return ""


def effort_string(ev):
    for k in ("effort", "effortLevel", "effort_level"):
        v = ev.get(k)
        if isinstance(v, str):
            return v
    return ""


model = model_string(event).lower()
effort = effort_string(event).lower()

# Optimal = best-model family (newest Opus line / Fable). Everything else that the
# event names is a candidate for the session-only upgrade prompt.
model_suboptimal = bool(model) and ("opus" not in model and "fable" not in model)
effort_suboptimal = bool(effort) and effort not in ("xhigh", "max")

if not (model_suboptimal or effort_suboptimal):
    sys.exit(0)  # already optimal, or nothing detectable — stay silent

out = {
    "systemMessage": PROMPT,
    "hookSpecificOutput": {
        "hookEventName": event.get("hook_event_name") or "SessionStart",
        "additionalContext": (
            PROMPT
            + ". If the user replies 'approve model', switch this session to the best "
            "available model. If the user replies 'approve ultracode', run this session "
            "at maximum effort with standing multiagent-workflow permission. These are "
            "session-only; nothing is persisted. To stop these prompts: "
            "touch ~/.ultramemory/onboard-optout"
        ),
    },
}
print(json.dumps(out, ensure_ascii=False))
PY
exit 0
