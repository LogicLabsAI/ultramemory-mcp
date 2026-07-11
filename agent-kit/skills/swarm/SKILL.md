---
name: swarm
description: "Parallel sub-agent fan-out. When a turn has 2+ INDEPENDENT read-only sub-questions (multi-source research, multi-target verification, several independent file reads, multiple log/config/db inspections), spawn one sub-agent per question in a SINGLE message so they run concurrently — instead of doing serial reads. Invoke whenever you catch yourself about to do a 3rd serial Bash/Read/Grep in one turn on independent questions."
license: MIT
---

# Swarm — Parallel Sub-Agent Fan-Out

## Why this exists

Under load, agents drift back to serial execution — reading one file, then another, then another —
even when the questions are independent. Serial reads waste wall-clock and the human's attention.
This skill is the durable fix: when the pattern matches, fan out.

## When to invoke (all three true)

1. **2+ independent questions** — each answerable without another's result.
2. **Read-only or non-conflicting** — no shared file writes, no single-writer state (a git commit,
   a service restart, one config file two agents would both edit).
3. **Bounded synthesis** — you can hold the union of N short reports in context.

**Trigger patterns:** multi-source research (web + repo + package registry + docs) → one agent per
source; multi-target verification (logs + config + git + db state) → one agent per target; 3+
independent file reads → one agent per file (or grouped); a verifier cluster after a fix → fan out
across measurement angles.

## When NOT to

Sequential dependencies (each step needs the prior result); shared writes (race conditions);
single-writer operations (restart, commit); trivial single-tool work (overhead exceeds benefit).

## How

Use the Agent tool with several calls **in one message** so they run in parallel:

```
Agent({ description: "...", subagent_type: "general-purpose", prompt: "<self-contained brief>" })
Agent({ description: "...", subagent_type: "general-purpose", prompt: "<self-contained brief>" })
Agent({ description: "...", subagent_type: "general-purpose", prompt: "<self-contained brief>" })
```

For long investigations, pass `run_in_background: true` and collect results as they land.

## Briefing rules (each worker starts with ZERO context)

Every brief must be self-contained: (1) **context** — the system + why it matters (1–3 sentences);
(2) **question** — exactly one (or 2–3 tightly related); (3) **tools/methods** — where to look,
exact commands; (4) **constraints** — read-only, cap the report; (5) **output structure** — numbered
sections; (6) **success criteria**. Keep each brief under ~300 words and each worker report under
~500 words — concision keeps synthesis tractable.

## Master / worker discipline

You are the master. Decide what to fan out; brief each worker; track each as a task; synthesize
results as they arrive; **verify each worker's output** (workers can be wrong, miss context, or
hallucinate — trust but check); reconcile disagreements yourself. Cap a fan-out at ~8 workers;
beyond that, synthesis gets unwieldy.

## Self-check (auto-fire)

Before a 2nd-or-later serial read-only action in the same turn: "Could the next 3–5 actions run in
parallel via spawned agents?" If yes, fan out instead of continuing serial.
