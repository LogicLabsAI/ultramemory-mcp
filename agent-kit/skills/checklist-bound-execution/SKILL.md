---
name: checklist-bound-execution
description: This skill should be used whenever work from an explicit checklist or plan is fanned out to multiple spawned agents (Workflow tool scripts or Task/Agent subagents). Trigger phrases include "run these through a workflow", "build from this atomic checklist", "spawn agents for these tasks", "make the agents follow the plan and not drift", "verify each item is actually done", and "use the harness". It binds every spawned agent to its EXACT checklist item verbatim (never from memory), forces a structured proof of completion, and runs an independent adversarial verifier that re-checks each item against its acceptance criteria with loop-back on failure. Use it to prevent agent drift, improvisation, and "done but not really done" outcomes.
version: 1.0.0
---

# Checklist-Bound Execution

A harness for fanning checklist / plan items out to spawned agents WITHOUT drift. Use it any time
two or more agents execute items from a shared plan and "did the agent actually do its exact job?"
matters.

## Why this exists (read first)

Spawned agents (Workflow agents, Task subagents) start with a FRESH, isolated context. They do not
see the conversation, the plan, or your intent unless it is handed to them. When the assignment is
vague — or worse, fails to arrive (the Workflow `args` global is unreliable and has come through as
`undefined`) — the agent reconstructs the task from whatever it can read and IMPROVISES. That is the
root cause of drift, not "lack of discipline."

Enforcement is therefore NOT a single instruction. It is four mechanisms together:

1. **Determinism of assignment** — each agent receives its item VERBATIM, embedded in its prompt.
2. **Structured proof** — each agent must return a schema-validated completion record (validated at
   the tool layer, so it cannot hand-wave; the runtime retries on mismatch).
3. **Independent verification** — a separate, read-only agent re-checks each item against its
   acceptance criteria, adversarially, and can FAIL it.
4. **Loop-back** — a failed item is sent back to a worker to fix, then re-verified, up to a bound.

A skill alone is advisory. This skill works because it pairs the procedure (here) with the Workflow
script's deterministic control flow + structured-output schemas + a verifier stage. An optional
`SubagentStop` hook can add a hard, model-can't-skip gate; this skill does not require one.

## The non-negotiable invariants

- **One item, one job.** A worker does ONLY its assigned item(s). It touches ONLY the files the item
  names. Anything outside scope is a `deviation` — reported, not silently done.
- **Verbatim, not from memory.** The item's `spec` and `acceptance_criteria` are the source of
  truth. If they conflict with the worker's prior assumptions, the item wins. If the item is
  ambiguous or impossible as written, the worker returns `status: "blocked"` with the reason — it
  does NOT guess.
- **No `args`, no filesystem in the script.** Workflow scripts cannot read files or rely on `args`.
  Embed the checklist as a literal in the script (see `references/workflow-template.js`). Workers may
  ALSO be given the checklist file path to re-read as a cross-check.
- **Verifier is read-only and adversarial.** It tries to prove the item is NOT done. It cannot edit
  (so it cannot bias itself by fixing). Default to FAIL when evidence is missing.
- **Done means verified.** An item is complete only when its verifier verdict is `pass: true`.

## The machine-readable checklist

The plan must be a structured artifact, not prose. Each item: `id, title, files[], spec,
acceptance_criteria[], status`, optional `verify_only`. See `references/checklist-format.md` for the
schema + example. Convert any prose plan (e.g. an atomic-plan `.md`) into this form BEFORE fanning
out — and ground every fact in the spec (paths, literals, schemas) against source first. A wrong
spec produces a confidently-wrong fix that the verifier will (rightly) keep failing.

## The orchestration pattern (Workflow)

Use the Workflow tool. Full template in `references/workflow-template.js`:

0. **Arm the deterministic gate (main session, BEFORE launching the workflow).** Write the run's
   global gate command as line 1 of `.claude/.harness-active` in the repo, and clear any stale
   counter, e.g.:
   `printf '%s\n' 'npm run build && npm test' > .claude/.harness-active && rm -f .claude/.harness-iter`
   This arms the global `Stop` hook (`~/.claude/hooks/harness-gate.sh`) so this turn CANNOT end
   until that gate passes. Do this from the MAIN session — the Workflow script has no filesystem
   access and cannot write the sentinel. The hook removes the sentinel automatically once the gate
   is green; to abandon a run, delete `.claude/.harness-active` yourself to disarm.
1. **Embed** the checklist as a JS literal in the script (fixes the `args` / filesystem limits).
2. **Partition to avoid races** — group items by file; one worker per file (two agents editing one
   file race). Use `isolation: 'worktree'` only if two workers truly must touch the same file.
3. **Pipeline per group**: worker stage (`schema = PROOF`) then verifier stage (`schema = VERDICT`),
   so each group verifies as soon as its worker finishes — no barrier.
4. **Loop-back**: if a verdict fails, re-run a worker on just the failed items (bounded, e.g. 2
   retries), then re-verify.
5. **Global gate** at the end (build / lint / tests) and a written report of every item's final
   verdict, including anything dropped or blocked.

Items already applied (e.g. earlier ad-hoc) get `verify_only: true`: skip the worker, still run the
verifier so prior work is validated through the same gate.

## Worker contract

Given verbatim: `id, title, files, spec, acceptance_criteria`. The worker reads the named files,
makes the MINIMAL change that satisfies EVERY acceptance criterion, self-checks each criterion, and
returns the PROOF schema (`references/schemas.md`). It reports deviations and never expands scope.

## Verifier contract

Given verbatim: `id, acceptance_criteria, files` plus the worker's PROOF. The verifier RE-READS the
actual resulting code (not the worker's claims), checks each acceptance criterion with evidence
(grep / read), and returns the VERDICT schema. Adversarial: assume not-done until proven. Read-only.

## Reusable agent types

Global subagents that preload this skill: `checklist-worker` (Read/Edit/Write/Bash) and
`checklist-verifier` (read-only). Pass them to Workflow agents via `agentType`. New agent defs load
on the next session — if they are not yet active, inline the worker/verifier contract into the agent
prompt instead (the template does exactly this, so it is portable either way).

## Definition of done

Every item has a `pass: true` verdict, the global gate (build / lint / tests) is green, and the
report lists each item, its verdict, and any deviations / drops. Update the checklist `status` to
reflect final verdicts.
