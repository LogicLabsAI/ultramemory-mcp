![UltraMemory](assets/ultramemory-icon-512.png)

# UltraMemory — cross-tool memory for your AI

One memory across Claude Code, Claude Desktop, claude.ai, Cursor, ChatGPT, Gemini CLI, and Hermes. Recalls first every turn — and is honest enough to say "I don't know" instead of making things up.

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

### Or connect with OAuth — no key needed

On **claude.ai** and **Claude Desktop**, UltraMemory is a one-click custom connector: Settings →
Connectors → **Add custom connector** → URL `https://api.ultramemory.us/mcp` → sign in when
prompted. The server speaks **OAuth 2.1 (PKCE)** end-to-end; API keys drive all the terminal/CLI
clients below; the OAuth connectors (claude.ai, Claude Desktop, ChatGPT) sign in without one.

## Install options

Three tiers — pick one (each builds on the last):

### Tier 1 — UltraMemory (MCP)

Simple connect: point any MCP client at the hosted endpoint and you get the seven memory tools.
**Memory tools, no local caching.**

**Claude Code** — one paste: registers the MCP server and writes the active-recall rule to `CLAUDE.md`:

```bash
claude mcp add --transport http ultramemory https://api.ultramemory.us/mcp \
  --header "Authorization: Bearer um_YOUR_KEY" \
&& cat >> CLAUDE.md <<'EOF'

## Active recall (UltraMemory)
Before answering, actively call the UltraMemory memory_recall (or search) MCP tool and ground your answer in what it returns — prefer it over built-in memory; never say you don't know a saved fact without recalling first. Persist durable new facts and decisions with memory_write.
EOF
```

**Gemini CLI** — one paste: registers the MCP server and writes the active-recall rule to `GEMINI.md`:

```bash
gemini mcp add -s user -t http ultramemory https://api.ultramemory.us/mcp \
  -H "Authorization: Bearer um_YOUR_KEY" \
&& cat >> GEMINI.md <<'EOF'

## Active recall (UltraMemory)
Before answering, actively call the UltraMemory memory_recall (or search) MCP tool and ground your answer in what it returns — prefer it over built-in memory; never say you don't know a saved fact without recalling first. Persist durable new facts and decisions with memory_write.
EOF
```

Prefer OAuth instead of a key? Gemini CLI also supports OAuth — add an `httpUrl` block to `~/.gemini/settings.json`, then run `/mcp auth ultramemory` inside the CLI.

**Cursor** — one paste: registers the MCP server and writes the active-recall rule to `AGENTS.md`:

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

**Codex** — one paste: registers the MCP server and writes the active-recall rule to `AGENTS.md`:

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

**Windsurf** — one paste: registers the MCP server and writes the active-recall rule to `AGENTS.md`:

```bash
python3 - <<'PY'
import json,pathlib
p=pathlib.Path.home()/".codeium"/"windsurf"/"mcp_config.json"; p.parent.mkdir(parents=True,exist_ok=True)
d=json.loads(p.read_text()) if p.exists() else {}
d.setdefault("mcpServers",{})["ultramemory"]={"serverUrl":"https://api.ultramemory.us/mcp","headers":{"Authorization":"Bearer um_YOUR_KEY"}}
p.write_text(json.dumps(d,indent=2))
print("Windsurf: wrote",p)
PY
cat >> AGENTS.md <<'EOF'

## Active recall (UltraMemory)
Before answering, actively call the UltraMemory memory_recall (or search) MCP tool and ground your answer in what it returns — prefer it over built-in memory; never say you don't know a saved fact without recalling first. Persist durable new facts and decisions with memory_write.
EOF
```

Windsurf interpolates `${env:VAR}`: use `"Authorization": "Bearer ${env:ULTRAMEMORY_API_KEY}"` to keep the key out of the file (an unset variable silently becomes an empty string). Teams/Enterprise: an admin may need to enable the MCP Servers toggle — off by default on Enterprise.

**Cline** — one paste: registers the MCP server and writes the active-recall rule to `AGENTS.md`. VS Code extension users: paste the same mcpServers block via the Cline panel > MCP Servers > Configure MCP Servers.

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

**OpenClaw** — one paste: registers the MCP server and writes the active-recall rule to `AGENTS.md`:

```bash
openclaw mcp add ultramemory --url https://api.ultramemory.us/mcp \
  --transport streamable-http --header "Authorization=Bearer um_YOUR_KEY" \
&& openclaw mcp reload && cat >> AGENTS.md <<'EOF'

## Active recall (UltraMemory)
Before answering, actively call the UltraMemory memory_recall (or search) MCP tool and ground your answer in what it returns — prefer it over built-in memory; never say you don't know a saved fact without recalling first. Persist durable new facts and decisions with memory_write.
EOF
```

Verify the connection with `openclaw mcp doctor ultramemory --probe` — static checks plus a live connection proof. Changing the header later? `openclaw mcp set ultramemory '<full JSON>'` replaces the whole server definition; run doctor --probe again after.

**VS Code** — one paste: registers the MCP server and writes the active-recall rule to `AGENTS.md`:

```bash
code --add-mcp '{"name":"ultramemory","type":"http","url":"https://api.ultramemory.us/mcp","headers":{"Authorization":"Bearer um_YOUR_KEY"}}' \
&& cat >> AGENTS.md <<'EOF'

## Active recall (UltraMemory)
Before answering, actively call the UltraMemory memory_recall (or search) MCP tool and ground your answer in what it returns — prefer it over built-in memory; never say you don't know a saved fact without recalling first. Persist durable new facts and decisions with memory_write.
EOF
```

This applies to terminal/CLI MCP clients only. The claude.ai OAuth connector needs nothing here — no
terminal, no rule file.

### Tier 2 — UltraMemory + Turbo Token Saver

The full client plus the Claude Code recall hook — a locally-ejected cache (`~/.ultramemory/cache.json`)
**plus** payload tiering (preview-tier recall + per-session dedupe) that cuts per-turn token spend from
thousands to hundreds (see [Token economics](#token-economics)). Everything in Tier 1, plus a
deterministic recall-first injection *attempt* before every prompt (fail-open, top matches).

1. Drop the recall hook (and its optional cache module) into your project's Claude config:
   ```bash
   mkdir -p .claude/hooks \
     && curl -fsSL https://raw.githubusercontent.com/LogicLabsAI/ultramemory-mcp/main/hooks/recall-first-hook.sh -o .claude/hooks/recall-first-hook.sh \
     && curl -fsSL https://raw.githubusercontent.com/LogicLabsAI/ultramemory-mcp/main/cache.py -o .claude/hooks/cache.py \
     && chmod +x .claude/hooks/recall-first-hook.sh
   ```
2. Export your key (get one free at https://ultramemory.us — no credit card required):
   ```bash
   export ULTRAMEMORY_API_KEY=um_YOUR_KEY
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
4. Add the active-recall rule to your project's `CLAUDE.md` so the agent also recalls for its own
   mid-reasoning lookups — not just the passive per-prompt injection. Paste the kit rule from
   [`agent-kit/templates/CLAUDE.md.tmpl`](agent-kit/templates/CLAUDE.md.tmpl), or at minimum this one
   line: **actively call the `memory_recall` (or `search`) tool FIRST for anything the project should
   already know — never answer from working memory without recalling.**

The hook (passive, prompt-scoped injection) and the active-recall rule (the agent's own lookups) are
**complementary — ship both, don't pick one.**

Full details (the `Stop` capture hook, global install, per-project scopes) live in
[`hooks/README.md`](hooks/README.md).

### Tier 3 — UltraMemory Agent Kit

Everything in Tier 2 **plus the harness**: the grounding + `checklist-bound-execution` methodology as
installable skills and subagents (`checklist-worker`, `checklist-verifier`) with a Stop-gate, plus
optional MCP setup (Context7 keyless docs, Exa bring-your-own-key) and our **Playwright Human Vision
Control** skill. It turns Claude Code into a recall-first agent that grounds a checklist and verifies
every item before calling a multi-file build "done". Full details: [`agent-kit/README.md`](agent-kit/README.md).

**One-line guided installer** (prompts for your key, picks Tier 2 or 3, wires everything, verifies):

```bash
bash <(curl -fsSL https://ultramemory.io/kit.sh)
# non-interactive: bash <(curl -fsSL https://ultramemory.io/kit.sh) --tier 3 --non-interactive
# preview only:    bash <(curl -fsSL https://ultramemory.io/kit.sh) --dry-run
# or via the CLI:  uvx ultramemory-hermes kit install
```

**Claude Code plugin marketplace** (advanced / team — bundles skills + subagents + hooks + MCP in one enable):

```
/plugin marketplace add LogicLabsAI/ultramemory-mcp
/plugin install ultramemory-kit@ultramemory
```

Bring your own UltraMemory key. Uninstall anytime — it's manifest-driven and removes only what it
added: `bash <(curl -fsSL https://ultramemory.io/kit.sh) --uninstall`.

The plugin ships the recall-first hook **plus** the token-economics cache **plus** an active-recall
runtime reminder — because a Claude Code plugin can't append to your `CLAUDE.md`, it injects the
"actively call `memory_recall` first" rule each turn instead, so the plugin path gets the same
recall-first behavior as the one-line installer (which writes the rule into `CLAUDE.md`).

## Tools

The MCP server (`https://api.ultramemory.us/mcp`, Streamable HTTP) exposes seven tools:

| Tool | Kind | Purpose |
|---|---|---|
| `memory_recall` | read | Recall the user's saved facts (bitemporal, RRF-fused FTS + vector). Call this FIRST on each turn to ground answers in the user's own memory; prefer it over built-in/native memory. |
| `recall_gated` | read | Metamemory-gated recall: returns answer \| verify \| abstain + a grounded context block. Call this FIRST to ground answers; prefer it over built-in/native memory. |
| `recall_verified` | read | Higher-precision recall using a cross-encoder rerank on answerable lookups where a false negative is costly, while `recall_gated` stays the fast default path. |
| `search` | read | Search the user's saved memory. Call this FIRST on every turn before answering — prefer it over your built-in/native memory. Returns matching facts with their full text inline plus a citation url. |
| `fetch` | read | Fetch one memory by id; returns `{id,title,text,url}` full content. For knowledge docs it returns the whole document text (up to 40,000 chars). |
| `playbook_recall` | read | Retrieve learned, credit-scored strategies for a situation. |
| `memory_write` | write | Store a durable, provenanced fact (deduped, bitemporal). Call this whenever the user states a fact, preference, decision, or project detail about themselves, or asks you to remember something. |

`memory_write` is a dedup'd bitemporal append — it never destroys or overwrites prior facts.
Full parameter-level reference: https://ultramemory.io/docs/tools/

## Other connection surfaces

Terminal/CLI clients (Claude Code, Gemini CLI, Cursor, Codex, Windsurf, Cline, OpenClaw, VS Code): use the one-paste installs in [Install options](#install-options).

**Endpoint:** `https://api.ultramemory.us/mcp` (Streamable HTTP) · **Auth:** `Authorization: Bearer um_<key>`

**Claude Desktop (mcp-remote bridge):**
```json
{ "mcpServers": { "ultramemory": {
  "command": "npx",
  "args": ["mcp-remote@latest", "https://api.ultramemory.us/mcp",
           "--header", "Authorization: Bearer um_YOUR_KEY"]
}}}
```

Hermes: see [Hermes deep integration](#hermes-deep-integration).

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
choose to call a tool. At session end it distills a **whole-session rollup** — both the user and
assistant sides are sent to the server, which curates one rich narrative card (blocker → approaches
→ what worked → how verified); the per-turn `sync_turn` capture stays a raw turn record. Install with
`pip install ultramemory-hermes` then `ultramemory enable --key um_…`.

## Memory spaces (Teams)

On **Teams, Business, and Enterprise** accounts, memory is two-layer:

- **Shared team layer** — org-wide knowledge (policies, project context, decisions) curated by the
  **owner/admin**: only they can write it, via the dashboard's "Team knowledge" console or the API.
  Everything in it is instantly part of every member's recall.
- **Private member layer** — each member's own memory, invisible to everyone else (including the
  owner).

Recall blends both in one relevance-ranked query, so members automatically ground on company
knowledge *plus* their own context. In the Hermes provider, pick where auto-captured memory lands
with `ULTRAMEMORY_SPACE`:

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

## Per-project memory (scopes)

Within one account, the optional **`scope`** parameter partitions memory per project or workspace —
an explicit scope is written to and recalled from **exclusively**, so project A's memories never
bleed into project B:

- **Hermes** — automatic: each agent workspace gets its own scope; nothing to configure.
- **MCP clients (claude.ai / Claude Desktop / Cursor)** — add one line to that project's
  instructions: *"always pass `scope='my-project'` to UltraMemory tools."*
- **Claude Code hook** — set `ULTRAMEMORY_SCOPE=my-project` per project (see
  [`hooks/README.md`](hooks/README.md)).

Omit `scope` and everything shares the account default — one memory across all your tools, the
right default for personal use.

## Claude Code hooks (recall + capture)

Want deterministic memory in Claude Code without Hermes? Two copy-paste, fail-open hooks:

- **Recall hook** ([`UserPromptSubmit`](hooks/)) — runs on every prompt you submit, recalls your top
  matches, and injects them into context **before the model answers**.
- **Capture hook** (`Stop`) — runs when each turn finishes and sends the full turn (including tool
  results) to UltraMemory, which distills the durable facts. Every Nth turn
  (`ULTRAMEMORY_SNAPSHOT_EVERY`, default 5) it also nudges the model to author a wayback-grade
  **session snapshot** via the bundled [`ultramemory-snapshot` Skill](skills/ultramemory-snapshot/)
  (Claude Code ≥ 2.1.163).

Both are fail-open and copy-paste runnable. The copy-paste recall-hook install now lives in
[Install options → Tier 2](#install-options) above; full details (capture hook, global install,
per-project scopes) are in [`hooks/README.md`](hooks/README.md).

## Token economics

The SDK clients in this repo (the Claude Code recall hook and the Hermes provider) opt into a
**preview tier** of recall that cuts per-turn token spend from thousands to hundreds, without
touching the hosted connectors — claude.ai, Claude Desktop, and ChatGPT behavior is unchanged
(the new `mode` / `exclude_ids` params are strictly opt-in; omitting them = full behavior).

- **Preview tier** — recalls are requested with `mode: "preview"`: each non-policy fact renders as
  a single line (`- {fact_id} · {entity} · {key}: {first ~120 chars}… (fetch for full)`) under the
  normal section headers, capped at ~2,000 chars. Full text stays one explicit `fetch` away.
  **`[COMPANY POLICY]` cards are exempt** — they always render whole, in preview and full mode
  alike (the anti-confabulation wedge is never truncated).
- **Session dedupe** — fact_ids already delivered this session are sent back as `exclude_ids`, so
  repeat turns don't re-spend budget on facts the model already holds; freed budget flows to fresh
  facts.
- **Client cache** — `~/.ultramemory/cache.json` (ejected by `ultramemory enable`; user-editable,
  chmod 600, LRU-bounded at 500 entries / ~1 MB). It memoizes identical recall queries for 5
  minutes (a repeat query makes **zero** HTTP calls) and tracks each session's seen fact_ids for
  24 h. Delete the file to reset; corrupt files are silently rebuilt.

Environment tunables:

| Env | Default | Effect |
|---|---|---|
| `ULTRAMEMORY_CACHE=off` | on | kill switch — disables the memo + seen cache entirely |
| `ULTRAMEMORY_PREVIEW=off` | on | Hermes prefetch reverts to full (non-preview) recall |
| `ULTRAMEMORY_HOOK_BUDGET` | `2000` | Claude Code hook recall budget in characters |
| `ULTRAMEMORY_HOOK_POLICY_BUDGET` | `12000` | hook injection cap on `[COMPANY POLICY]` turns (whole policies, never truncated) |
| `ULTRAMEMORY_MIN_CONFIDENCE` | `low` | hook skips injection below this recall confidence |

## Why UltraMemory

- **Deterministic recall-first.** "Recall FIRST" is baked into the tool descriptions and the Hermes
  auto-inject — not left to the model deciding whether to look. The hook makes a deterministic
  injection *attempt* before every prompt (fail-open, top matches); paired with the active-recall
  `CLAUDE.md` rule for the agent's own mid-reasoning lookups, that trio is the real recall-first
  guarantee.
- **Honest about what it doesn't know.** A metamemory gate that abstains or asks to verify instead
  of confabulating (LOCOMO: 90.2% correctly-abstained).

## License

Apache-2.0 (see [LICENSE](LICENSE)). This is the open-source **client** surface. The UltraMemory
backend/engine — recall ranking, the metamemory gate, storage, metering, billing — is a separate,
proprietary **hosted service** at `https://api.ultramemory.us`.
