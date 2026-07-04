---
name: ultramemory-snapshot
description: >-
  Compose and save a durable, wayback-grade session snapshot to UltraMemory. Use this whenever
  you are asked to write a session snapshot or rollup of the work just done — in particular when
  the UltraMemory capture hook nudges you to "Compose a session snapshot per the
  ultramemory-snapshot Skill rubric ... and save it with the UltraMemory memory_write tool now".
  The snapshot is ONE rich card written with the UltraMemory `memory_write` tool.
---

# UltraMemory session snapshot

Install: copy this file to `~/.claude/skills/ultramemory-snapshot/SKILL.md`
(`mkdir -p ~/.claude/skills/ultramemory-snapshot && cp -r skills/ultramemory-snapshot ~/.claude/skills/`).
It is invoked automatically by the every-Nth-turn nudge from `hooks/capture-hook.sh`
(cadence `ULTRAMEMORY_SNAPSHOT_EVERY`, default 5), or any time you decide the session is worth
snapshotting.

## What to write

Write exactly **one rich card** — a self-contained narrative of ~40–120 words — that captures the
durable substance of the work in this order:

1. **Blocker** — the concrete problem you were solving.
2. **Approaches tried** — what you attempted, including the ones that failed.
3. **What worked** — the fix / decision that resolved it.
4. **How it was verified** — the exact test, command output, version, or metric that proves it.

## Fidelity — the wayback test

Write so a reader with **zero context, months from now**, fully understands it:

- **Named entities, never pronouns** — "the OrderService retry bug", not "it" or "this".
- **Absolute dates only**, resolved against today's real date — "on 2026-07-04", never "today",
  "yesterday", or "last week".
- **Fold in the concrete substance** — exact commands, error strings, versions, file paths, ids,
  numbers, URLs.
- **Never a bare** "true"/"false"/"yes"/"done" — fold the substance into the value instead.
- Keep a short supporting quote from the session in `rationale` (under ~200 characters).

## BAD vs GOOD

**BAD** (thin, context-free — fails the wayback test):

> "Fixed the bug. It works now."

**GOOD** (rich, self-contained — passes the wayback test):

> "On 2026-07-04 the OrderService double-charge bug was resolved. First reproduced it with
> `pytest test_retry_duplicate_charge` (failing); tried widening the DB lock (no effect), then
> added an idempotency key to the retry path (commit 9f3c2a1). Verified by
> `test_retry_duplicate_charge` now passing in 0.42s and the duplicate-charge rate dropping to 0
> over the following hour."

## How to save it — the `memory_write` call

Call the UltraMemory `memory_write` tool with these parameters:

- `entity` — the project or system the snapshot is about (e.g. `"OrderService"`).
- `key` — a short, stable topic for this snapshot (e.g. `"session snapshot 2026-07-04"`).
- `value` — the narrative card described above (blocker → approaches → what worked → how verified).
- `rationale` — a one-line supporting quote from the session.
- `space` — `"private"` (your own member space) unless the snapshot is team knowledge.

Example arguments:

```json
{
  "entity": "OrderService",
  "key": "session snapshot 2026-07-04",
  "value": "On 2026-07-04 the OrderService double-charge bug was resolved. First reproduced it with `pytest test_retry_duplicate_charge` (failing); tried widening the DB lock (no effect), then added an idempotency key to the retry path (commit 9f3c2a1). Verified by test_retry_duplicate_charge now passing in 0.42s and the duplicate-charge rate dropping to 0 over the following hour.",
  "rationale": "added the idempotency key and the dupe-charge test passes",
  "space": "private"
}
```

Write the snapshot **once** per nudge. If nothing durable happened this session, do not write a
snapshot.
