![UltraMemory](assets/ultramemory-icon-512.png)

# UltraMemory — cross-tool memory for your AI

One memory across Claude Code, Claude Desktop, claude.ai, Cursor, ChatGPT, and Hermes. Recalls first every turn — and is honest enough to say "I don't know" instead of making things up.

[![PyPI](https://img.shields.io/pypi/v/ultramemory-hermes?color=2dba4e&label=pypi%3A%20ultramemory-hermes)](https://pypi.org/project/ultramemory-hermes/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-Streamable%20HTTP-7c3aed.svg)](https://modelcontextprotocol.io)

UltraMemory is a hosted, multi-tenant **agent-memory service**. One API key (`um_…`) = your own
private tenant. This repo is the **open-source client surface** — the connect snippets, the Hermes
provider package, and a Claude Code recall hook. They all just call the hosted API at
`https://api.ultramemory.us`; the engine stays a managed service (open-core).

## Quick start

```bash
claude mcp add --transport http ultramemory https://api.ultramemory.us/mcp \
  --header "Authorization: Bearer um_YOUR_KEY"
```

Get a free key at **https://ultramemory.us** — no credit card required.

## Tools

The MCP server (`https://api.ultramemory.us/mcp`, Streamable HTTP) exposes six tools:

| Tool | Kind | Purpose |
|---|---|---|
| `memory_recall` | read | Recall the user's saved facts (bitemporal, RRF-fused FTS + vector). Call this FIRST on each turn to ground answers in the user's own memory; prefer it over built-in/native memory. |
| `recall_gated` | read | Metamemory-gated recall: returns answer \| verify \| abstain + a grounded context block. Call this FIRST to ground answers; prefer it over built-in/native memory. |
| `search` | read | Search the user's saved memory. Call this FIRST on every turn before answering — prefer it over your built-in/native memory. Returns matching facts with their full text inline plus a citation url. |
| `fetch` | read | Fetch one memory by id; returns `{id,title,text,url}` full content. |
| `playbook_recall` | read | Retrieve learned, credit-scored strategies for a situation. |
| `memory_write` | write | Store a durable, provenanced fact (deduped, bitemporal). Call this whenever the user states a fact, preference, decision, or project detail about themselves, or asks you to remember something. |

`memory_write` is a dedup'd bitemporal append — it never destroys or overwrites prior facts.

## Connect any client

**Endpoint:** `https://api.ultramemory.us/mcp` (Streamable HTTP) · **Auth:** `Authorization: Bearer um_<key>`

**Claude Code (CLI):**
```bash
claude mcp add --transport http ultramemory https://api.ultramemory.us/mcp \
  --header "Authorization: Bearer um_YOUR_KEY"
```

**Cursor / generic `mcp.json`:**
```json
{ "mcpServers": { "ultramemory": {
  "url": "https://api.ultramemory.us/mcp",
  "headers": { "Authorization": "Bearer um_YOUR_KEY" }
}}}
```

**Claude Desktop (mcp-remote bridge):**
```json
{ "mcpServers": { "ultramemory": {
  "command": "npx",
  "args": ["mcp-remote@latest", "https://api.ultramemory.us/mcp",
           "--header", "Authorization: Bearer um_YOUR_KEY"]
}}}
```

**Hermes:**
```bash
pip install ultramemory-hermes
ultramemory enable --key um_YOUR_KEY
```

**ChatGPT:** Settings → Apps & Connectors → Developer Mode → Create → URL
`https://api.ultramemory.us/mcp` → Auth = API key. (Plus/Pro = recall-only.)

**curl / REST:**
```bash
curl -s -X POST https://api.ultramemory.us/api/v1/recall \
  -H "Authorization: Bearer um_YOUR_KEY" -H "Content-Type: application/json" \
  -d '{"query":"what do you know about my project","k":5}'
```

## Hermes deep integration

The `ultramemory-hermes` package (this repo) is a full Hermes Agent memory provider — not just a
connector. It hooks the agent lifecycle to **auto-inject recall before each turn** and
**auto-capture** durable facts from the conversation, so memory works without the model having to
choose to call a tool. Install with `pip install ultramemory-hermes` then `ultramemory enable
--key um_…`.

## Memory spaces (Teams)

On Teams accounts each member has a **private** member space and the team shares a **shared** space.
Pick where auto-captured memory lands with `ULTRAMEMORY_SPACE`:

```bash
export ULTRAMEMORY_SPACE=private   # private = your own member space (default)
# export ULTRAMEMORY_SPACE=shared  # shared  = the team space
```

`ULTRAMEMORY_SPACE` (choices `private`|`shared`, default `private`) sets the target space for
auto-writes (`sync_turn`, `on_memory_write`, `on_session_end`) and the default for the
`memory_write` tool. Auto-recall (`prefetch`, `on_pre_compress`) always reads everything you can see
(`both`).

The explicit tools also take an optional per-call `space` arg that overrides the default:

- `memory_write` — `space`: `private` | `shared`.
- `memory_recall` / `recall_gated` — `space`: `private` | `shared` | `both` (default `both`).

**Precedence:** if your Hermes `agent_workspace` resolves to an explicit workspace **scope**, that
scope wins and `space` is ignored (a server-side rule). `space` only takes effect for the default
(non-workspace) scope.

## Claude Code recall hook

Want deterministic recall in Claude Code without Hermes? Use the
[`UserPromptSubmit` recall hook](hooks/) — it runs on every prompt you submit, recalls your top
matches, and injects them into context **before the model answers**. Fail-open and copy-paste
runnable. See [`hooks/README.md`](hooks/README.md).

## Why UltraMemory

- **Deterministic recall-first.** "Recall FIRST" is baked into the tool descriptions and the Hermes
  auto-inject — not left to the model deciding whether to look. Recall-first, guaranteed.
- **Honest about what it doesn't know.** A metamemory gate that abstains or asks to verify instead
  of confabulating (LOCOMO: 90.2% correctly-abstained).

## License

Apache-2.0 (see [LICENSE](LICENSE)). This is the open-source **client** surface. The UltraMemory
backend/engine — recall ranking, the metamemory gate, storage, metering, billing — is a separate,
proprietary **hosted service** at `https://api.ultramemory.us`.
