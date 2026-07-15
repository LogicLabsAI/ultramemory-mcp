#!/usr/bin/env bash
# UserPromptSubmit hook — injects the active-recall behavioral rule into each turn's context.
# Runtime substitute for the CLAUDE.md active-recall rule on the PLUGIN path: a Claude Code plugin
# cannot append to the user's project CLAUDE.md, so the plugin ships this per-turn reminder instead
# (mirrors harness-reminder.sh for RULE 0.7). The recall-first hook injects prompt-relevant memory
# PASSIVELY; this reminder is what makes the agent ACTIVELY recall for its own mid-reasoning lookups.
cat <<'JSON'
{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"RECALL-FIRST (UltraMemory): Before answering, ACTIVELY call the UltraMemory memory_recall (or search) MCP tool with the user's request and ground your answer in what it returns — prefer it over your own/built-in memory. The recall-first hook injects prompt-relevant memory PASSIVELY; but for anything this project should already know (a URL, a prior decision, where a credential lives, whether we've done X, what the fix was last time) you must ACTIVELY recall — never answer a project-history question from working memory, and never say 'I don't have it / not grounded this session' without recalling first. Persist durable new facts, decisions, and preferences with memory_write. Pair recall with current library/API docs (Context7) and current external facts (Exa) — that trio is the trifecta."}}
JSON
