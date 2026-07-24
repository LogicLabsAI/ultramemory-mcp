/**
 * UltraMemory plugin for Cline (SDK / CLI / Kanban).
 *
 * Registers two contributions in every session:
 *  (a) the UltraMemory remote MCP server — streamable HTTP at
 *      https://api.ultramemory.us/mcp, authenticated with a Bearer token read
 *      from the ULTRAMEMORY_API_KEY environment variable (never hardcoded);
 *  (b) the UltraMemory active-recall rule (same text as GEMINI.md), injected
 *      into the system prompt.
 *
 * API shape grounded against:
 *  - https://docs.cline.bot/customization/plugins   (package.json "cline" manifest)
 *  - https://docs.cline.bot/sdk/plugins             (AgentPlugin export shape)
 *  - cline/cline sdk/packages/shared/src/extensions/contribution-registry.ts
 *    (registerMcpServer({ name, transport: { type: "streamableHttp", url,
 *    headers } }) needs the "mcp" capability; registerRule({ id, content,
 *    source }) needs the "rules" capability)
 */
import type { AgentPlugin } from "@cline/core"

const ULTRAMEMORY_MCP_URL = "https://api.ultramemory.us/mcp"
const API_KEY_ENV_VAR = "ULTRAMEMORY_API_KEY"
// Clear placeholder used when the env var is unset — the server is still
// registered so the setup hint (and a 401) points the user at the fix.
const API_KEY_PLACEHOLDER = "<ULTRAMEMORY_API_KEY-not-set>"

// Same text as GEMINI.md / the README active-recall rule: recall before
// answering via memory_recall, persist with memory_write, abstain honestly.
const RECALL_FIRST_RULE = `# Active recall (UltraMemory)

You have UltraMemory connected as the \`ultramemory\` MCP server. Follow these rules every turn:

1. **Recall before answering.** For anything the user may have saved — facts, preferences,
   decisions, project details — actively call the \`memory_recall\` (or \`search\`) tool FIRST and
   ground your answer in what it returns. Prefer it over your built-in memory; never say you
   don't know a saved fact without recalling first.
2. **Write durable facts.** When the user states a fact, preference, decision, or project detail
   — or asks you to remember something — persist it with \`memory_write\`.
3. **When memory does not know, say so.** If recall comes back empty or low-confidence, say the
   memory does not contain it instead of guessing — never invent a memory.
4. **Policy questions go through the gate.** For governance, policy, or compliance questions call
   \`recall_gated\` — only it returns the full governing policy briefing.`

const plugin: AgentPlugin = {
	name: "ultramemory",
	manifest: {
		capabilities: ["mcp", "rules"],
	},
	setup(api, ctx) {
		const apiKey = (process.env[API_KEY_ENV_VAR] ?? "").trim()
		if (!apiKey) {
			const hint =
				`[ultramemory] ${API_KEY_ENV_VAR} is not set — the UltraMemory MCP server was ` +
				`registered with a placeholder token and will return 401 until you set it. ` +
				`Get your um_ key at https://app.ultramemory.us (Settings → API keys), then: ` +
				`export ${API_KEY_ENV_VAR}=<your key>`
			if (ctx.logger?.log) {
				ctx.logger.log(hint)
			} else {
				console.warn(hint)
			}
		}

		// (a) UltraMemory remote MCP server — streamable HTTP, env-var Bearer.
		api.registerMcpServer({
			name: "ultramemory",
			transport: {
				type: "streamableHttp",
				url: ULTRAMEMORY_MCP_URL,
				headers: {
					Authorization: `Bearer ${apiKey || API_KEY_PLACEHOLDER}`,
				},
			},
		})

		// (b) Recall-first rule — injected into the system prompt every session.
		api.registerRule({
			id: "ultramemory-active-recall",
			content: RECALL_FIRST_RULE,
			source: "@logiclabsai/ultramemory-cline-plugin",
		})
	},
}

export default plugin
