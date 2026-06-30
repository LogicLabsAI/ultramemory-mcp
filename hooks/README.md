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

## Make it global (every project, every session)

Put the script at `~/.claude/hooks/recall-first-hook.sh` and add the same `hooks` block to your
**`~/.claude/settings.json`**, changing the command path to an absolute one:
```json
"command": "$HOME/.claude/hooks/recall-first-hook.sh"
```
Export `ULTRAMEMORY_API_KEY` in your shell profile so it's present for every session.

## How it works
- Reads the `UserPromptSubmit` event JSON from stdin and extracts your `prompt`.
- `POST $ULTRAMEMORY_API_BASE/api/v1/recall` with `{"query": <prompt>, "k": 5}` and
  `Authorization: Bearer $ULTRAMEMORY_API_KEY`.
- Emits `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": …}}`
  with the recalled facts — the supported way to add context for this event.
- The key is read from the environment and is **never** logged or written to disk.

## Notes
- `UserPromptSubmit` always fires on every submission (the `matcher` field is ignored for this
  event; it's kept empty for forward-compatibility).
- Requires `curl` and `python3` (both standard on macOS/Linux). No `pip install` needed.
