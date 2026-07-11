# Checklist item format

The shared source of truth. One JSON object with an `items` array; each element is an item.

```json
{
  "run": "human-readable run name",
  "repos": { "WEB": "/abs/path/to/web", "API": "/abs/path/to/api" },
  "items": [
    {
      "id": "F3",
      "title": "Short imperative title",
      "files": ["/abs/path/to/file.tsx"],
      "spec": "Exactly what to change and to what. Quote target strings verbatim (old -> new). State the WHY in one line so the worker can resolve ambiguity toward intent.",
      "acceptance_criteria": [
        "A concrete, independently checkable condition (greppable / observable).",
        "Another condition. Each must be verifiable by reading the file or running a command — not 'looks good'."
      ],
      "status": "todo",
      "verify_only": false
    }
  ]
}
```

## Rules

- **files** are ABSOLUTE paths. The orchestrator groups items by file to avoid write races. If an
  item legitimately spans files, list them all; that whole set becomes one worker's scope.
- **spec** quotes exact strings (old -> new). Ground every fact (paths, literals, schemas) against
  source BEFORE writing the item — a wrong spec produces a confidently-wrong fix.
- **acceptance_criteria** are the verifier's checklist. Each must be observable (a grep result, a
  type-check pass, a rendered string). Avoid subjective criteria.
- **status**: `todo | done | blocked | verified`.
- **verify_only**: `true` marks items already applied (e.g. earlier ad-hoc work) that the verifier
  should still re-check retroactively. Workers skip these; the verifier does not.
- Keep items ATOMIC. If a "fix" has two independent acceptance conditions on different concerns,
  prefer splitting so a partial failure is precise.
