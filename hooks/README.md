# Claude Code recall hook — deterministic recall, every turn

Most memory tools only recall **if the model decides** to call the memory tool — so it still
forgets. This hook removes the guesswork: it runs on **every** prompt you submit in Claude Code
(the `UserPromptSubmit` event), recalls your top matches from UltraMemory, and injects them into
the context **before the model answers**. Recall-first, guaranteed — because the harness runs it,
not the model.

It is **fail-open**: any problem (no key, no network, no `curl`/`python3`, empty results) injects
nothing and exits `0`, so it can never block or slow your prompt beyond a short timeout.

## Install (project-scoped — recommended to start)

1. Copy the hook into your project's Claude config and make it executable:
   ```bash
   mkdir -p .claude/hooks
   cp hooks/recall-first-hook.sh .claude/hooks/recall-first-hook.sh
   chmod +x .claude/hooks/recall-first-hook.sh
   ```
2. Export your UltraMemory key (get one free at https://ultramemory.us — no credit card required):
   ```bash
   export ULTRAMEMORY_API_KEY=um_YOUR_KEY
   # optional, defaults to https://api.ultramemory.us
   # export ULTRAMEMORY_API_BASE=https://api.ultramemory.us
   # optional: recall ONLY this project's memory scope (see "Per-project memory" below)
   # export ULTRAMEMORY_SCOPE=my-project
   ```
3. Register the hook in `.claude/settings.json`:
   ```json
   {
     "hooks": {
       "UserPromptSubmit": [
         {
           "matcher": "",
           "hooks": [
             {
               "type": "command",
               "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/recall-first-hook.sh",
               "timeout": 10
             }
           ]
         }
       ]
     }
   }
   ```

That's it. Submit a prompt and the hook recalls relevant memories into context automatically.

## Capture hook — automatic memory writing on every turn

The recall hook reads; the **capture hook** writes. Registered on the `Stop` event, it runs when
each turn finishes and sends the **full turn** to UltraMemory: the last real user message plus
every assistant text block **and every tool result** from that turn (tool output is prefixed
`[tool result]` so the extractor can read it — on a debugging session the durable facts often live
in the passing test count or the exact error string, not the prose). UltraMemory distills the
**durable facts** (your projects, preferences, decisions) into rich, self-contained memories in your
private memory. Each turn's text is truncated at **24,000 characters** per side, and the payload
carries an `observed_at` timestamp so relative dates ("yesterday") resolve to absolute ones. Repeated
facts are **deduped server-side**, and it is **fail-open**: any problem (no key, no network, no
`curl`/`python3`, nothing durable) writes nothing and exits `0`, so it never blocks or slows your
turn.

It reuses the same environment variables as the recall hook — `ULTRAMEMORY_API_KEY` (required) and
`ULTRAMEMORY_API_BASE` (optional, defaults to `https://api.ultramemory.us`).

1. Copy the hook into your project's Claude config and make it executable:
   ```bash
   cp hooks/capture-hook.sh .claude/hooks/capture-hook.sh
   chmod +x .claude/hooks/capture-hook.sh
   ```
2. Register the hook in `.claude/settings.json` (alongside the `UserPromptSubmit` block above):
   ```json
   {
     "hooks": {
       "Stop": [
         {
           "matcher": "",
           "hooks": [
             {
               "type": "command",
               "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/capture-hook.sh",
               "timeout": 10
             }
           ]
         }
       ]
     }
   }
   ```

### Session snapshots — the model authors its own rollup

Every few turns the capture hook also emits an **in-band snapshot nudge**: it asks the model that
just did the work to compose a wayback-grade session snapshot (blocker → approaches tried → what
worked → how verified) and save it with the UltraMemory `memory_write` tool. The narrative of a
whole session lives in no single turn, so this puts the model in the author seat for the rollup —
no server round-trip needed.

- **Cadence:** `ULTRAMEMORY_SNAPSHOT_EVERY` (default `5`) — a nudge fires every Nth turn. Set it to
  `1` to snapshot every turn, or `0` to disable snapshots entirely.
- **Requires Claude Code ≥ 2.1.163** (the version that added `Stop` → `hookSpecificOutput.additionalContext`).
  On older Claude Code the field is silently inert — the per-turn capture still runs, so this
  degrades gracefully with no version detection.
- The nudge relies on the **`ultramemory-snapshot` Skill** (below) for the full rubric, so the hook
  output itself stays tiny (well under Claude Code's 10,000-char output cap).

**Accepted gap:** the `Stop` event does **not** fire on user interrupts, and turns that end in an
API error fire `StopFailure` (whose hook output is ignored). Those turns are captured only by the
per-turn sweep on the surrounding turns — the snapshot nudge may skip them.

### Install the snapshot Skill

The nudge points the model at a Skill that holds the snapshot rubric and the exact `memory_write`
shape. Install it once:

```bash
mkdir -p ~/.claude/skills/ultramemory-snapshot
cp skills/ultramemory-snapshot/SKILL.md ~/.claude/skills/ultramemory-snapshot/SKILL.md
```

Without the Skill the nudge still fires; the Skill just gives the model the versioned rubric so
snapshots stay consistent and high-fidelity.

## Make it global (every project, every session)

Put the script at `~/.claude/hooks/recall-first-hook.sh` and add the same `hooks` block to your
**`~/.claude/settings.json`**, changing the command path to an absolute one:
```json
"command": "$HOME/.claude/hooks/recall-first-hook.sh"
```
Export `ULTRAMEMORY_API_KEY` in your shell profile so it's present for every session.

## Per-project memory (optional)

By default the hook recalls from your account's default memory pool — the same across every
project. To keep projects separate, set `ULTRAMEMORY_SCOPE` **per project** in that project's
`.claude/settings.json`:

```json
{ "env": { "ULTRAMEMORY_SCOPE": "my-project" } }
```

With a scope set, the hook recalls **only** memories written under that scope (e.g. by MCP tools
told to use `scope="my-project"`, or a Hermes agent whose workspace resolves to it) — project A's
memories never surface in project B. Leave it unset for one shared memory across all projects.

## How it works
- Reads the `UserPromptSubmit` event JSON from stdin and extracts your `prompt`.
- `POST $ULTRAMEMORY_API_BASE/api/v1/recall` with `{"query": <prompt>, "k": 5}` (plus
  `"scope"` when `ULTRAMEMORY_SCOPE` is set) and `Authorization: Bearer $ULTRAMEMORY_API_KEY`.
- Emits `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": …}}`
  with the recalled facts — the supported way to add context for this event.
- The key is read from the environment and is **never** logged or written to disk.

## Notes
- `UserPromptSubmit` always fires on every submission (the `matcher` field is ignored for this
  event; it's kept empty for forward-compatibility).
- Requires `curl` and `python3` (both standard on macOS/Linux). No `pip install` needed.
