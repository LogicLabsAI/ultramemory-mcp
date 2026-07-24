# UltraMemory — Cline plugin

One memory for every AI you use — recall-first, and honest enough to say "I don't know"
instead of making things up.

This is a native Cline `AgentPlugin` package. In every session it registers:

1. **The UltraMemory remote MCP server** — streamable HTTP at
   `https://api.ultramemory.us/mcp`, authenticated with
   `Authorization: Bearer $ULTRAMEMORY_API_KEY` (read from the environment at
   session start — never hardcoded). This exposes the UltraMemory tools
   (`memory_recall`, `search`, `memory_write`, `recall_gated`, `recall_verified`,
   `memory_feedback`, `fetch`) to the agent.
2. **The active-recall rule** — injected into the system prompt: recall before
   answering via `memory_recall`, persist durable facts with `memory_write`, and
   abstain honestly when memory does not know.

> **Scope caveat:** works on Cline SDK, CLI, and Kanban — not the VS Code/JetBrains extensions yet.
> (VS Code / JetBrains users: add the same MCP server via the Cline panel →
> MCP Servers → Configure MCP Servers — see the repo root README's Cline section.)

## Install

This package is not published to npm yet (npm publish deferred) — install it via
`cline plugin install <path-or-git>`:

```bash
# from a clone of this repo (local path install)
cline plugin install ./plugins/cline

# or clone + install in one go
git clone https://github.com/LogicLabsAI/ultramemory-mcp.git
cline plugin install ./ultramemory-mcp/plugins/cline
```

Then confirm it loaded by running `cline config` and checking the plugin tab.

## Setup

Set your UltraMemory API key in the environment before starting Cline
(your um_ key is at <https://app.ultramemory.us> → Settings → API keys):

```bash
export ULTRAMEMORY_API_KEY=<your key>
```

If the variable is unset, the plugin still registers the MCP server with a clear
placeholder token and logs a setup hint — requests will return 401 until the key
is set.
