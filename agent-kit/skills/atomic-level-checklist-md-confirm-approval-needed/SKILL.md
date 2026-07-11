---
name: atomic-level-checklist-md-confirm-approval-needed
description: atomic level checklist md confirm approval needed
---

Create a step-by-step atomic-level checklist as a .md file so we can walk through every item and make sure nothing is missed.

## The loop — slower than you want to move, ALWAYS

1. **Smoke-test FIRST (baseline).** Before writing the plan, look at the current state from multiple angles. Run the read-only checks that tell us what's already there, what's broken, what the expected behavior is. Write the baseline into the checklist as "BEFORE" state so we have a diff anchor.

2. **Build the atomic checklist .md** with this structure per item:
   - **Every atomic step MUST begin with a GitHub-flavored markdown checkbox: `- [ ]`** — so we can visually confirm completion at a glance. Unchecked = not done. `[x]` = done AND verified. Never mark `[x]` until the verification command has passed.
   - What to do (exact file + line / exact command)
   - Why (root cause / constraint / contract the item serves)
   - Verification command (proves it happened — not "ran without error," but "state matches expectation")
   - Rollback path (how to undo in one step)
   - Owner (which parallel agent or master)

   Example row format:
   ```markdown
   - [ ] **P1.3 — Cap Redis at 512MB with allkeys-lru**
     - File: `docker-compose.yml` line 47 (redis service command)
     - Why: Redis was unbounded → OOM risk under load
     - Verify: `docker exec redis redis-cli CONFIG GET maxmemory` → returns `536870912`
     - Rollback: `git revert <commit>`
     - Owner: worker-A
   ```

3. **Break the checklist into clusters** that can run in parallel without conflicting (no shared-file writes, no single-writer constraints, no sequential dependencies). Master agent assigns clusters to parallel workers.

4. **Explain the plan back to the user in plain English** — what I think I'm about to do, why, where the risk is. **Do NOT start any work until the user approves.**

## Rules during execution (non-negotiable)

- **Workers DO NOT RUSH.** Every worker takes the time to verify its own work before reporting complete. If a step fails or looks wrong, STOP and report — do not paper over it to stay on pace. Quality over speed is a hard rule; speed-rushing is how bugs get shipped.
- **Each worker self-verifies every checkbox it closes.** Not "I ran the command" — actually re-read the file, re-query the state, confirm the intended change is present. Only THEN flip `- [ ]` → `- [x]` and paste the verification command's actual output as evidence directly below the item.
- **Scope discipline — if a worker sees something adjacent that looks wrong but isn't in its cluster, REPORT IT, do not fix it.** One item per checkbox. Drifting out of scope causes merge conflicts and untracked changes.
- **No forbidden flag-words** ("quick fix", "workaround", "patch", "temporary", "good enough", "hack"). If one appears, stop and propose an architectural alternative.

## Master verification (after all workers report)

- **Master agent re-runs every verification command** itself. Do not trust the workers' self-reports alone — verify independently. Any `[x]` that fails master re-verification gets reverted to `[ ]` with a note explaining why, and the discrepancy is reported to the user.
- **Master agent runs an end-to-end smoke test** that proves the whole thing works together, not just that each piece is present. A full-stack probe: the feature/fix actually does what it was supposed to do. Add this as the final `- [ ]` item in the checklist, checked off only when the end-to-end smoke test passes.
- **If anything fails verification or smoke test:** STOP. Do not self-repair. Report the discrepancy to the user with diagnosis and proposed fix. Wait for approval.

## Final output to the user

- The checklist .md file with EVERY box now `[x]` (or explicitly flagged as blocked/skipped with reason) + evidence pasted inline under each item
- A one-paragraph summary: what changed, what was verified, what remains open
- Rollback anchors (git commits, DO snapshots, backup files) so the user can undo

---

**This skill exists because rushing parallel execution is how silent bugs get shipped. The checklist is the receipt. Verification is the proof. Smoke-test is the truth. The `[x]` is only ever earned — never assumed.**
