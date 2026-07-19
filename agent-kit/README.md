# UltraMemory Agent Kit (Tier 3)

The **UltraMemory Agent Kit** turns Claude Code into a recall-first, ground-before-you-build agent.
It bundles three things:

1. **Turbo Token Saver (Tier 2).** The recall-first hook + client-side token-economics cache — every
   prompt recalls your relevant memory *before* the model answers, in a compact preview tier
   (memoized 5 min, per-session dedupe, hard-capped), and the model abstains instead of guessing when
   memory has nothing grounded.
2. **The harness.** The grounding + `checklist-bound-execution` methodology as installable skills and
   subagents (`checklist-worker`, `checklist-verifier`) plus a Stop-gate hook — so multi-file builds
   ride a machine-readable checklist with an adversarial verifier and a green gate, instead of
   unbound fan-out.
3. **Optional MCP + browser setup.** Context7 (keyless docs) + Exa (bring-your-own key) for current
   library/API docs and web facts, and our **Playwright Human Vision Control** skill for supervised,
   visible browser co-working (the installer offers to install the standard Playwright MCP).

**Bring your own key.** The kit needs an UltraMemory API key to make recall work — get one at
<https://ultramemory.io> (free tier, no card). The kit is the technique; the memory service is the
engine.

---

## Install — two ways

### 1. One-line guided installer (simplest)

```bash
bash <(curl -fsSL https://ultramemory.io/kit.sh)
```

It prompts for your UltraMemory key, lets you pick **Tier 2** (Turbo Token Saver only) or **Tier 3**
(everything), offers Context7 / Exa / Playwright, wires everything, and runs an "it's working" check.
Non-interactive: `bash <(curl -fsSL https://ultramemory.io/kit.sh) --tier 3 --non-interactive`
(reads `ULTRAMEMORY_API_KEY` from the environment). Preview first with `--dry-run`.

You can also drive it from the published CLI: `uvx --from ultramemory-hermes ultramemory kit install`.

### 2. Claude Code plugin marketplace (advanced / team)

```
/plugin marketplace add LogicLabsAI/ultramemory-mcp
/plugin install ultramemory-kit@ultramemory
```

Installing the plugin brings the skills, the `checklist-worker`/`checklist-verifier` subagents, the
recall + harness hooks, and the Context7/Exa MCP servers together in one enable. Set
`ULTRAMEMORY_API_KEY` in your environment so the recall hook can authenticate, and replace the
`<USER_EXA_KEY>` placeholder in the plugin's `.mcp.json` if you want Exa.

---

## What it installs (and where)

- **Global (`~/.claude/`):** the harness skills (`harness`, `checklist-bound-execution`,
  `atomic-level-checklist-…`, `research`, `swarm`), the subagents (`checklist-worker`,
  `checklist-verifier`), and the hooks (`recall-first-hook.sh`, `harness-gate.sh`,
  `harness-reminder.sh`, `cache.py`). The hook wiring is merged into `~/.claude/settings.json`.
- **Per-project (`./.claude/`, `./CLAUDE.md`, `./scripts/gate.sh`):** the recall hook can also be
  installed project-scoped, the harness **routing rule** is placed in your project's `CLAUDE.md`
  (between `# --- UltraMemory harness (managed) ---` sentinels), and a static `scripts/gate.sh` is
  written from the template.
- **MCP servers:** `context7` (keyless) and, on consent, `exa` (your key) and `playwright`.

**The Stop-gate is dormant by default.** `harness-gate.sh` only blocks a turn when a run is armed
(a `.claude/.harness-active` file exists, which the `checklist-bound-execution` skill writes for you).
On every other turn it is a no-op — it never runs your tests on unrelated work.

## Uninstall

```bash
bash <(curl -fsSL https://ultramemory.io/kit.sh) --uninstall     # or: uvx --from ultramemory-hermes ultramemory kit uninstall
```

Uninstall is **manifest-driven** (`~/.ultramemory/install-manifest.json`): it removes only what the
kit added — the MCP servers it registered, the hook files, the `settings.json` entries, the managed
CLAUDE.md block, and the harness skills/agents — and restores the `.bak` copies it made. It never
touches your own rules or config. Use `--dry-run` to see the plan first.

## Maintenance

The harness assets are curated from a private source of truth by `scripts/export-kit.sh` (with a
`--check` drift guard). Business rules and secrets never ship — the exporter deny-scans every byte
and fails closed on any personal path, secret, or private reference.

MIT licensed. Issues + source: <https://github.com/LogicLabsAI/ultramemory-mcp>.
