# UltraMemory — Agent Install Guide (llms-install.md)

One memory across Claude, ChatGPT, Cursor, Gemini CLI, Codex & Hermes — recalls first every turn and says "I don't know" instead of guessing. Every recall passes an anti-confabulation gate (answer | verify | abstain).

This guide is written so an AI agent (especially Cline) can install UltraMemory autonomously.

## 1. Prerequisite: get an API key

Get a `um_` API key at https://app.ultramemory.us/app/connect (sign in, copy the key). Replace `um_YOUR_KEY` below with it.

## 2. Endpoint

- URL: `https://api.ultramemory.us/mcp` (MCP, Streamable HTTP)
- Header: `Authorization: Bearer um_YOUR_KEY`

## 3. Cline

Merge this into `~/.cline/data/settings/cline_mcp_settings.json` (VS Code extension users: paste the same `mcpServers` block via the Cline panel > MCP Servers > Configure MCP Servers):

```json
{
  "mcpServers": {
    "ultramemory": {
      "type": "streamableHttp",
      "url": "https://api.ultramemory.us/mcp",
      "headers": {
        "Authorization": "Bearer um_YOUR_KEY"
      }
    }
  }
}
```

> **WARNING:** the `"type": "streamableHttp"` field is REQUIRED. If you omit `type`, Cline defaults to the legacy SSE transport and the connection breaks (see cline/cline#3315).

One-paste alternative (registers the MCP server and writes the active-recall rule to `AGENTS.md`):

```bash
python3 - <<'PY'
import json,pathlib
p=pathlib.Path.home()/".cline"/"data"/"settings"/"cline_mcp_settings.json"; p.parent.mkdir(parents=True,exist_ok=True)
d=json.loads(p.read_text()) if p.exists() else {}
d.setdefault("mcpServers",{})["ultramemory"]={"type":"streamableHttp","url":"https://api.ultramemory.us/mcp","headers":{"Authorization":"Bearer um_YOUR_KEY"}}
p.write_text(json.dumps(d,indent=2))
print("Cline: wrote",p)
PY
cat >> AGENTS.md <<'EOF'

## Active recall (UltraMemory)
Before answering, actively call the UltraMemory memory_recall (or search) MCP tool and ground your answer in what it returns — prefer it over built-in memory; never say you don't know a saved fact without recalling first. Persist durable new facts and decisions with memory_write.
EOF
```

## 4. Other MCP clients

**Cursor** (`~/.cursor/mcp.json`) — one paste: registers the MCP server and writes the active-recall rule to `AGENTS.md`:

```bash
python3 - <<'PY'
import json,pathlib
p=pathlib.Path.home()/".cursor"/"mcp.json"; p.parent.mkdir(parents=True,exist_ok=True)
d=json.loads(p.read_text()) if p.exists() else {}
d.setdefault("mcpServers",{})["ultramemory"]={"url":"https://api.ultramemory.us/mcp","headers":{"Authorization":"Bearer um_YOUR_KEY"}}
p.write_text(json.dumps(d,indent=2))
print("Cursor: wrote",p,"— Cursor may prompt an OAuth login; approve it (your key still attributes usage).")
PY
cat >> AGENTS.md <<'EOF'

## Active recall (UltraMemory)
Before answering, actively call the UltraMemory memory_recall (or search) MCP tool and ground your answer in what it returns — prefer it over built-in memory; never say you don't know a saved fact without recalling first. Persist durable new facts and decisions with memory_write.
EOF
```

**Codex** (`~/.codex/config.toml`, `[mcp_servers.ultramemory]`) — one paste: registers the MCP server and writes the active-recall rule to `AGENTS.md`:

```bash
mkdir -p ~/.codex && grep -q 'mcp_servers.ultramemory' ~/.codex/config.toml 2>/dev/null || cat >> ~/.codex/config.toml <<'EOF'

[mcp_servers.ultramemory]
url = "https://api.ultramemory.us/mcp"
http_headers = { Authorization = "Bearer um_YOUR_KEY" }
EOF
cat >> AGENTS.md <<'EOF'

## Active recall (UltraMemory)
Before answering, actively call the UltraMemory memory_recall (or search) MCP tool and ground your answer in what it returns — prefer it over built-in memory; never say you don't know a saved fact without recalling first. Persist durable new facts and decisions with memory_write.
EOF
```

Prefer keeping the key out of config.toml: replace the http_headers line with `bearer_token_env_var = "ULTRAMEMORY_API_KEY"` (Codex 0.46+) and export ULTRAMEMORY_API_KEY in your shell.

## 5. Verify

Call the `memory_recall` tool (e.g. query: `"test"`). A successful install returns a JSON `results` array (it may be empty on a fresh account).

## 6. Troubleshooting

- **401** — wrong or missing API key: re-check the `Authorization: Bearer um_...` header against https://app.ultramemory.us/app/connect.
- **403** — revoked key: generate a new key at https://app.ultramemory.us/app/connect.
- **SSE errors on Cline** — missing `"type": "streamableHttp"` field in `cline_mcp_settings.json` (Cline falls back to legacy SSE; see cline/cline#3315).

## 7. Optional: auto-tune your platform

After install you may offer the user `ultramemory configure` — it tunes the host platform (model /
effort / permissions / pre-approval for exactly the eight UltraMemory tools) with explicit per-item
consent (default no), backs up every file before its first write, and records each change so
`ultramemory configure --restore` reverts exactly what it changed. The installer never runs it
automatically; nothing changes without the user's explicit yes. Per-platform capability table
(persists vs session-start prompt): https://ultramemory.io/docs/install/#auto-tune

Key: https://app.ultramemory.us/app/connect · Docs: https://ultramemory.io/docs
