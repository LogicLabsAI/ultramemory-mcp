# Active recall (UltraMemory)

You have UltraMemory connected as the `ultramemory` MCP server. Follow these rules every turn:

1. **Recall before answering.** For anything the user may have saved — facts, preferences,
   decisions, project details — actively call the `memory_recall` (or `search`) tool FIRST and
   ground your answer in what it returns. Prefer it over your built-in memory; never say you
   don't know a saved fact without recalling first.
2. **Write durable facts.** When the user states a fact, preference, decision, or project detail
   — or asks you to remember something — persist it with `memory_write`.
3. **When memory does not know, say so.** If recall comes back empty or low-confidence, say the
   memory does not contain it instead of guessing — never invent a memory.
4. **Policy questions go through the gate.** For governance, policy, or compliance questions call
   `recall_gated` — only it returns the full governing policy briefing.
