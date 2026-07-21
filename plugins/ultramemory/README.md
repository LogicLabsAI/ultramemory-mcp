# UltraMemory — Codex Plugin

One memory across Claude, ChatGPT, Cursor, Gemini CLI, Codex & Hermes — recalls first every turn
and says "I don't know" instead of guessing. Every recall passes an anti-confabulation gate
(answer | verify | abstain).

> The Turbo Token Saver recall hook ships for Claude Code & Hermes today; this Codex plugin
> provides the MCP connection and skills only.

## What this plugin does

Connects Codex to the hosted UltraMemory agent-memory service over MCP (Streamable HTTP —
https://api.ultramemory.us/mcp). Your key is read from the `ULTRAMEMORY_API_KEY` environment
variable — it is never stored in the plugin files.

## Install

1. Get an API key: https://app.ultramemory.us/app/connect
2. Export it in your shell:

   ```bash
   export ULTRAMEMORY_API_KEY="um_YOUR_KEY"
   ```

3. Add this repo as a plugin marketplace and install:

   ```bash
   codex plugin marketplace add LogicLabsAI/ultramemory-mcp
   ```

   Then pick **UltraMemory** from the Plugins Directory.

## Links

- Get a key: https://app.ultramemory.us/app/connect
- Docs: https://ultramemory.io/docs
