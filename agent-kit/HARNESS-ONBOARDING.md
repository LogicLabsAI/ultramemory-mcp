# Harness onboarding (generic drop-in)

The UltraMemory Agent Kit installs the harness *machinery* (skills, subagents, hooks) globally
under `~/.claude/`. This doc is the last-mile step that wires the **routing rule** and a **static
gate** into a specific project. Everything here is READ-ONLY until you say "go".

1. **Verify** the global machinery exists: `~/.claude/skills/{harness,checklist-bound-execution}`,
   `~/.claude/agents/{checklist-worker,checklist-verifier}.md`, `~/.claude/hooks/harness-gate.sh`.
2. **Place the routing rule** from `templates/CLAUDE.md.tmpl` into your project's `CLAUDE.md`
   (Placement Contract): if a `0.x` hard-rules band exists, add it as the next free `0.x` right
   above RULE 1; else create the band as RULE 0.7. Never renumber existing rules.
3. **Author a static gate** `scripts/gate.sh` from `templates/gate.sh.tmpl` — STATIC only (build /
   lint / tests / type-check; never deploy or paid/live calls); fail loud on a missing validator.
4. **Smoke-test** it: clean tree exits 0; plant one invalid file → nonzero; revert → 0.
5. **Gitignore** `.claude/.harness-active` and `.claude/.harness-iter`.

Then, on any multi-file build: ground an atomic checklist, get approval, and run
`checklist-bound-execution` — it arms the `.claude/.harness-active` Stop gate and loops bound
workers + an adversarial verifier until the gate is green.
