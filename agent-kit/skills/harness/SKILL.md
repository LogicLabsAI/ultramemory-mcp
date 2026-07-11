---
name: harness
description: One-command anti-drift pipeline. Use when the user types /harness, or says "run this through the harness", "build with rails", or "no-drift build". Runs research -> grounded atomic checklist -> STOP for approval -> checklist-bound-execution workflow (which arms the Stop-hook gate) -> gated verification.
---

# Harness — one command, full rails

Task: $ARGUMENTS

Run these stages IN ORDER. Never start building before Stage 2 approval.

## 1 — Ground the plan (no memory)

Follow `atomic-level-checklist-md-confirm-approval-needed` for the task above: verify every fact
(paths, literal strings, schemas, endpoints) against actual source — never from memory — and emit a
machine-readable checklist in `checklist-bound-execution` format: each item =
`{id, title, files[], spec, acceptance_criteria[], status}`.

## 2 — STOP for approval (hard gate)

Present the checklist AND the proposed global gate command (e.g. `npm run build && npm test`).
Do NOT proceed until the user explicitly approves both the plan and the gate command.

## 3 — Execute through the harness

Follow `checklist-bound-execution`. Its step 0 arms the deterministic gate by writing the approved
command to `.claude/.harness-active`, then it embeds the checklist verbatim in the Workflow template,
partitions by file, and runs worker -> verifier with bounded loop-back using agentType
`checklist-worker` / `checklist-verifier`.

## 4 — Report

Summarize each item's final verdict plus any deviations or blocks. On turn end, the global `Stop`
hook runs the gate: on failure it blocks the stop and feeds back exactly what to fix; on green it
disarms automatically. You are not done until the gate is green.
