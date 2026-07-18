# Claude Code recall hook — deterministic recall, every turn

Most memory tools only recall **if the model decides** to call the memory tool — so it still
forgets. This hook removes the guesswork: it runs on **every** prompt you submit in Claude Code
(the `UserPromptSubmit` event), recalls your top matches from UltraMemory, and injects them into
the context **before the model answers**. What it guarantees is a deterministic **injection attempt**
of prompt-relevant matches before the model answers (fail-open — see below), because the harness runs
it, not the model. It does **not** make the agent actively recall for its own mid-reasoning lookups —
that needs the active-recall `CLAUDE.md` rule (the kit ships one in
`agent-kit/templates/CLAUDE.md.tmpl`); the hook and that rule are complementary.

It is **fail-open**: any problem (no key, no network, no `python3`, empty results) injects
nothing and exits `0`, so it can never block or slow your prompt beyond a short timeout.

It is also **token-economical**: it requests the compact **preview** briefing tier, memoizes
responses for 5 minutes, dedupes facts already injected this session, and hard-caps every
injection at 9,500 characters — see [Token economics](#token-economics-preview-tier-memoization-dedupe).

## Install (project-scoped — recommended to start)

1. Copy the hook into your project's Claude config and make it executable:
   ```bash
   mkdir -p .claude/hooks
   cp hooks/recall-first-hook.sh .claude/hooks/recall-first-hook.sh
   chmod +x .claude/hooks/recall-first-hook.sh
   # optional but recommended: enables 5-min memoization + per-session dedupe
   # (also satisfied by `pip install ultramemory-hermes`; without either, the hook
   # still works — it just skips the cache)
   cp cache.py .claude/hooks/cache.py
   ```
2. Export your UltraMemory key (get one free at https://ultramemory.io — no credit card required):
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
               "timeout": 20
             }
           ]
         }
       ]
     }
   }
   ```

That's it. Submit a prompt and the hook recalls relevant memories into context automatically.

## Token economics — preview tier, memoization, dedupe

The hook is built to keep recall cheap (~150–400 tokens/turn instead of ~2.8K) without losing the
anti-confabulation guarantees:

- **Preview tier.** Requests are sent with `mode: "preview"`: the server renders non-policy facts
  as one-line summaries (`fact_id · entity · key: head… (fetch for full)`) under a tight character
  budget. **Whole `[COMPANY POLICY]` cards are exempt** — they always render in full, exactly as in
  full mode.
- **Memoization.** Responses are memoized in `~/.ultramemory/cache.json` (created `0700`/`0600`)
  for **5 minutes** — an identical query within that window renders from cache and makes **zero
  HTTP calls**. The cache module (`cache.py`) ships in the `ultramemory-hermes` package; for a
  copied hook, copy `cache.py` next to it (see Install). Without it the hook runs uncached.
- **Per-session dedupe.** The hook keys a 24-hour "seen" set on the hook event's `session_id` and
  sends the fact_ids it already injected as `exclude_ids`, so later turns spend their budget on
  *new* facts instead of repeating old ones.
- **Client confidence threshold.** Abstain responses inject nothing (as before). Additionally,
  `ULTRAMEMORY_MIN_CONFIDENCE` skips injections whose gate band is below it. The decision encodes
  the band (`answer` = high, `verify` = medium, `abstain` = low).
- **Policy override.** A briefing containing `[COMPANY POLICY]` is **always injected whole**,
  regardless of decision or threshold — company policy must reach the model even when the gate
  abstains.
- **Hard cap.** The injected context is truncated at **9,500 characters** (Claude Code's
  `additionalContext` limit is 10,000). Turns whose briefing contains a `[COMPANY POLICY]` block
  escalate the cap to `ULTRAMEMORY_HOOK_POLICY_BUDGET` (default **12,000**) so whole policies are
  never truncated client-side.
- **Inline limit notices.** When any enforced usage limit is at **≥80%**, the recall response
  carries a limit state (the `X-Limit-State` header / `limit_notice` field) and the hook appends
  one short line to the injected context — `[UltraMemory] You're at 87% of your 5-hour limit …` —
  so you see it in the terminal, live percentage every turn. A `429` limit-reached response
  injects the friendly limit message (window, exact reset time, upgrade link) instead of failing
  silent. Both paths stay fail-open and never block the turn.

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `ULTRAMEMORY_HOOK_BUDGET` | `2000` | `max_characters` budget for the preview briefing |
| `ULTRAMEMORY_HOOK_POLICY_BUDGET` | `12000` | injection cap for turns whose briefing contains a `[COMPANY POLICY]` block — replaces the default 9,500-char cap on those turns so whole policies arrive untruncated |
| `ULTRAMEMORY_MIN_CONFIDENCE` | `low` | `low` \| `medium` \| `high` — minimum gate band to inject; `low` (default) injects medium/high (verify/answer), `high` injects only answer-grade recalls. `[COMPANY POLICY]` briefings bypass this |
| `ULTRAMEMORY_CACHE` | *(unset)* | set to `off` to disable memoization + dedupe entirely (every call hits the API, no cache file is touched) |
| `ULTRAMEMORY_HOOK_DEADLINE` | `9` | overall wall-clock deadline for the recall hook, in seconds — the primary request timeout is clamped to `min(6, remaining)` and the verified retry runs only when enough deadline remains, so the hook always emits before the registration timeout can kill it |
| `ULTRAMEMORY_HOOK_LOG` | *(on)* | set to `off` to disable the per-invocation status line both hooks append to `~/.ultramemory/hook.log` — **metadata only** (timestamp, hook, outcome, duration, injected chars, retry flag); never memory contents, queries, or keys |

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

If a write hits a usage limit (HTTP `429`), the hook **surfaces the friendly limit message to
you** via the hook `systemMessage` output field — which window, that writes are paused, the exact
reset time, and the upgrade link — instead of discarding the response. It still exits `0` and
never blocks the turn (the Nth-turn snapshot nudge below is skipped on those turns, since its
`memory_write` would hit the same paused-writes limit).

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
               "timeout": 30
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
- Reads the `UserPromptSubmit` event JSON from stdin: `.prompt` becomes the query and
  `.session_id` keys the per-session dedupe set (non-JSON stdin falls back to being the query).
- Consults the 5-minute memo cache first — a hit renders from cache with **no HTTP call**.
- On a miss, `POST $ULTRAMEMORY_API_BASE/api/v1/recall/gated` with
  `{"query": <prompt>, "k": 5, "mode": "preview", "max_characters": $ULTRAMEMORY_HOOK_BUDGET,
  "exclude_ids": [<already-seen fact_ids>]}` (plus `"scope"` when `ULTRAMEMORY_SCOPE` is set) and
  `Authorization: Bearer $ULTRAMEMORY_API_KEY` — the **metamemory-gated** endpoint, which returns
  a decision plus a ready-to-use sectioned briefing in `context_block` (tier **T1**, see below).
- When the gate **abstains** (memory has nothing grounded) or the confidence band is below
  `ULTRAMEMORY_MIN_CONFIDENCE`, the hook injects nothing and exits `0` — quiet by design, no
  noise. Exception: a `[COMPANY POLICY]` briefing is always injected whole.
- Otherwise emits `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": …}}`
  with the returned `context_block` (the sectioned briefing, truncated at 9,500 chars) — the
  supported way to add context for this event.
- The key is read from the environment and is **never** logged or written to disk.

## Latency tiers

UltraMemory recall comes in three latency tiers so you trade speed for depth deliberately:

- **T1 — sectioned assembly briefing (<200ms).** The default for this hook and the MCP
  `recall_gated` tool: metamemory-gated recall returns a ready-to-use, sectioned `context_block`
  (facts + usage notes + card bodies) assembled server-side with **no extra LLM hop** — fast
  enough to run on **every** prompt. It abstains (injects nothing) when memory has nothing
  grounded.
- **T2 — ask / Haiku digest (~1–2s).** The `/me/memory/ask` path reranks the candidate pool and
  streams a synthesized natural-language answer from a small model (Haiku). Use it when you want a
  written answer over your memory, not just the raw briefing.
- **T3 — agentic multi-hop (deferred).** Iterative, tool-using deep recall for hard multi-step
  questions. Deferred pending a pricing decision; not yet available.

This hook uses **T1** — the right default for recall-first injection: it stays well under the
prompt-submit budget while still returning the whole grounded briefing.

## Notes
- `UserPromptSubmit` always fires on every submission (the `matcher` field is ignored for this
  event; it's kept empty for forward-compatibility).
- The recall hook requires only `python3` (stdlib HTTP — no `curl` needed anymore); the capture
  hook still requires `curl`. Both are standard on macOS/Linux. No `pip install` needed.

## Changelog

- **1.9.6** — Hermes provider fix + kit hardening. **Hermes shim:** `ultramemory enable` now plants
  a provider shim at `$HERMES_HOME/plugins/ultramemory/` (Hermes discovers memory providers by
  directory scan — Python entry points are not consulted) with a site-packages `sys.path` fallback
  for separate-venv Hermes installs; new `ultramemory disable` removes the shim and resets
  `memory.provider` to `builtin`. **Honest uninstall:** the installer records the exact env-key edit
  it makes and `uninstall.sh` surgically strips only that key — customer settings survive.
  **Tier-1 registration:** the installer registers (or key-refreshes) the UltraMemory MCP via
  `claude mcp add`. **Hook fixes:** the verified retry excludes only session-seen ids (so it no
  longer guts the verified briefing and can fire on cache-hit turns); timeout floors on the clamped
  requests; bounded non-blocking cache lock (fail-soft on contention). **Auto-capture wired** in the
  installer + plugin (Stop, timeout 20) with a `ULTRAMEMORY_CAPTURE=off` kill-switch.
- **1.9.5** — Robustness: doctor checks global `~/.claude/CLAUDE.md` + `settings.json` (kills
  false WARN/FAIL on global installs); WAF-403 no longer misreported as a dead key (dead_key =
  401, or 403 with the API's JSON detail body); recall query clamped to the server's 4096-char
  max; capture hook honors `ULTRAMEMORY_HOOK_DEADLINE` (default 9s); installer writes hooks
  atomically (temp + rename).
- **1.9.4** — Hook observability: the silent-death class is eliminated. **Deadline-aware
  timeouts:** the recall hook is governed by `ULTRAMEMORY_HOOK_DEADLINE` (default 9s) — the primary
  request is clamped to `min(6, remaining)`, the verified retry runs only when enough deadline
  remains, and the hook always emits before the registration timeout can kill it; registration
  timeouts raised to **20** (recall `UserPromptSubmit`) and **30** (capture `Stop`). **Dead-key
  notice:** a 401/403 now surfaces one once-per-session `[UltraMemory] API key rejected …` line
  with the rotation URL instead of dying silently (the key is never echoed). **hook.log receipts:**
  both hooks append one metadata-only line per invocation to `~/.ultramemory/hook.log`
  (`ULTRAMEMORY_HOOK_LOG=off` disables; never contents, queries, or keys; 1 MiB rotation).
  **Installer rule guard:** `install.sh` writes the active-recall rule from an embedded fallback on
  template-fetch failure, upgrades older managed blocks missing the recall paragraph, and warns
  with the exact manual paste if the sentinel is absent post-install. **Doctor command:** new
  `ultramemory doctor [--probe]` checks key, hook file, registration + timeout, cache module,
  CLAUDE.md rule sentinel, and hook.log health (`--probe` adds a live recall round-trip). **Docs:**
  `memory_feedback` added to the README tool table (eight tools).
- **1.8.0** — Limit visibility (usage-limits program U9/C7) + policy budget. **Recall hook:** when
  any enforced usage limit is ≥80% it appends the live notice as one `[UltraMemory] …` line to the
  injected context (rendered from the `X-Limit-State` header / `limit_notice` field, memo-cache
  compatible), and a `429` limit-reached response injects the friendly limit message (window, exact
  reset time, upgrade link) instead of silent fail-open — fail-open behavior otherwise preserved.
  **Capture hook:** stops piping the capture response to `/dev/null` — a `429` limit_reached is
  surfaced to the user via the hook `systemMessage` field (writes paused + when they resume),
  still exit `0`, never blocking. **Policy budget (flag ④, parked v1.7.1 candidate):** turns whose
  briefing contains `[COMPANY POLICY]` escalate the client injection cap from 9,500 to
  `ULTRAMEMORY_HOOK_POLICY_BUDGET` (default 12,000) so policies arrive whole; non-policy turns
  unchanged.
- **1.7.0** — Token-economics upgrade. **Fix:** stdin is now parsed as the Claude Code hook JSON —
  the query is always the extracted `.prompt` (falling back to raw stdin when stdin isn't JSON),
  never the whole hook JSON envelope, and `.session_id` keys the new per-session dedupe set. New:
  preview-tier requests (`mode: "preview"`, budget `ULTRAMEMORY_HOOK_BUDGET`, default 2000 chars),
  5-minute response memoization + 24-hour per-session `exclude_ids` dedupe via `cache.py`
  (`~/.ultramemory/cache.json`, kill-switch `ULTRAMEMORY_CACHE=off`), client confidence threshold
  `ULTRAMEMORY_MIN_CONFIDENCE` (default `low`), a hard 9,500-char injection cap, and
  `[COMPANY POLICY]` briefings always injected whole. Recall HTTP moved from `curl` to python
  stdlib (`urllib`).
