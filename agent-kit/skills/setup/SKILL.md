---
name: setup
description: Guides Claude through first-time UltraMemory configuration after plugin install — API key, MCP server registration, test recall, and 401/403 troubleshooting.
---

# UltraMemory Setup

One memory across Claude, ChatGPT, Cursor, Gemini CLI, Codex & Hermes — recalls first every turn and says "I don't know" instead of guessing. Every recall passes an anti-confabulation gate (answer | verify | abstain).

Walk the user through first-time configuration, IN ORDER:

## 1. Check the API key

Check whether `ULTRAMEMORY_API_KEY` is set in the environment:

```bash
echo "${ULTRAMEMORY_API_KEY:+set}"
```

If it is empty, ask the user to get a key at **https://app.ultramemory.us/app/connect** (an `um_` key), then export it:

```bash
export ULTRAMEMORY_API_KEY=um_YOUR_KEY
```

## 2. Verify the MCP server is connected

```bash
claude mcp list
```

Look for an `ultramemory` entry. If it is missing, register it with:

```bash
claude mcp add --transport http ultramemory https://api.ultramemory.us/mcp \
  --header "Authorization: Bearer um_YOUR_KEY"
```

(Replace `um_YOUR_KEY` with the user's key from step 1.)

## 3. Run a test recall

Call the `memory_recall` MCP tool with a simple query (e.g. "test recall"). Confirm it returns a JSON response — an answer, or an honest abstain if memory is empty. Either confirms the connection works end to end.

## 4. Troubleshooting 401 / 403

- **401 Unauthorized** — the key is missing, malformed, or revoked. Re-check the `Authorization: Bearer um_...` header value against the key shown at https://app.ultramemory.us/app/connect, then re-run step 2 with the corrected key.
- **403 Forbidden** — the key is valid but not allowed for this action (e.g. revoked key, plan/seat limit, or a member key writing to a shared scope). Verify the key's status and plan at https://app.ultramemory.us/app/connect, or generate a fresh key and re-register.

Docs: https://ultramemory.io/docs
