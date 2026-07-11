# Structured-output schemas

Pass these as the `schema` option to the Workflow `agent()` call. The tool layer validates the
return, so the agent physically cannot return a hand-wave; it retries on mismatch.

**The schema ROOT must be `type: "object"`** — Anthropic structured-output tools reject a root of
`type: "array"` (`400 ... input_schema.type: Input should be 'object'`). When a worker handles
several items at once, wrap the array in an object and read the field off the return:

```json
{ "type": "object", "additionalProperties": false, "required": ["proofs"],
  "properties": { "proofs": { "type": "array", "items": <PROOF> } } }
```

Then use `result.proofs` (and the analogous `{ "verdicts": [ <VERDICT> ] }` -> `result.verdicts`).

## PROOF — returned by a worker

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["item_id", "status", "files_changed", "change_summary", "acceptance_self_check", "evidence", "deviations"],
  "properties": {
    "item_id": { "type": "string" },
    "status": { "type": "string", "enum": ["done", "blocked", "skipped"] },
    "files_changed": { "type": "array", "items": { "type": "string" } },
    "change_summary": { "type": "string" },
    "acceptance_self_check": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["criterion", "met", "evidence"],
        "properties": {
          "criterion": { "type": "string" },
          "met": { "type": "boolean" },
          "evidence": { "type": "string" }
        }
      }
    },
    "evidence": { "type": "string", "description": "concrete proof: the new line(s), or a grep result" },
    "deviations": { "type": "string", "description": "anything beyond/short of spec, or 'none'" }
  }
}
```

## VERDICT — returned by a verifier

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["item_id", "pass", "criteria_results", "reason", "must_fix"],
  "properties": {
    "item_id": { "type": "string" },
    "pass": { "type": "boolean" },
    "criteria_results": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["criterion", "met", "evidence"],
        "properties": {
          "criterion": { "type": "string" },
          "met": { "type": "boolean" },
          "evidence": { "type": "string", "description": "what the verifier actually observed in the file/command" }
        }
      }
    },
    "reason": { "type": "string" },
    "must_fix": { "type": "string", "description": "if pass=false, the precise fix needed; else 'none'" }
  }
}
```
