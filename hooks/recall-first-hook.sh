#!/usr/bin/env bash
# UltraMemory recall-first hook for Claude Code (UserPromptSubmit event).
# On every prompt it recalls your top matches from UltraMemory and injects them as context.
# Token-economical (v1.7.0): requests the PREVIEW briefing tier (server renders non-policy facts
# as one-liners; whole [COMPANY POLICY] cards are exempt), memoizes responses for 5 minutes and
# dedupes per-session via cache.py (exclude_ids), applies a client confidence threshold, and
# hard-caps the injection at 9,500 chars (Claude Code's additionalContext limit is 10,000).
# Fail-open by design: any error injects nothing and exits 0, so it can never block your prompt.
set -uo pipefail

API_BASE="${ULTRAMEMORY_API_BASE:-https://api.ultramemory.us}"
API_KEY="${ULTRAMEMORY_API_KEY:-}"
SCOPE="${ULTRAMEMORY_SCOPE:-}"   # optional: recall only this project's scope (opt-in; empty = account default)
# Where this script lives — lets the python block find cache.py in a repo checkout (../cache.py)
# or copied right next to the hook. No cache.py anywhere -> caching quietly disables.
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)" || HOOK_DIR=""

command -v python3 >/dev/null 2>&1 || exit 0   # fail-open if prerequisites are missing
[ -n "$API_KEY" ] || exit 0

# Single stdlib-python program (urllib for HTTP): parse the hook JSON, consult the memo cache
# (hit -> render from cache, zero HTTP), else POST a preview-tier gated recall, then render.
UM_API_BASE="$API_BASE" UM_API_KEY="$API_KEY" UM_SCOPE="$SCOPE" UM_HOOK_DIR="$HOOK_DIR" python3 -c '
import json, os, sys


def emit(block):
    # (e) hard client truncation: Claude Code caps additionalContext at 10,000 chars — stay
    # safely under it at 9,500, ALWAYS (policy blocks included; last-resort safety cap).
    block = block[:9500]
    if block.strip():
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": block}}))


def load_cache():
    # cache.py ships in the ultramemory package (pip install ultramemory-hermes); in a repo
    # checkout it sits one level above hooks/, or you can copy it next to the installed hook.
    # Missing everywhere -> return None and the hook runs uncached (still fully functional).
    try:
        from ultramemory import cache
        return cache
    except Exception:
        pass
    try:
        import importlib.util
        hook_dir = os.environ.get("UM_HOOK_DIR", "")
        if hook_dir:
            for cand in (os.path.join(hook_dir, "cache.py"), os.path.join(hook_dir, os.pardir, "cache.py")):
                if os.path.isfile(cand):
                    spec = importlib.util.spec_from_file_location("um_cache", cand)
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    return mod
    except Exception:
        pass
    return None


def main():
    raw = sys.stdin.read()
    # (a) Claude Code hook JSON: .prompt is the query, .session_id keys the per-session
    # dedupe (seen) set; non-JSON stdin falls back to being the query itself.
    query, session_id = raw, ""
    try:
        evt = json.loads(raw)
        if isinstance(evt, dict):
            query = str(evt.get("prompt") or "")
            session_id = str(evt.get("session_id") or "")
    except Exception:
        pass
    query = query.strip()
    if not query:
        return
    scope = os.environ.get("UM_SCOPE", "").strip()
    cache = load_cache()

    # (b) memo first: an identical query within 5 minutes renders from cache — zero HTTP.
    data = cache.memo_get(query, scope, None) if cache else None
    if not isinstance(data, dict):
        try:
            budget = int(os.environ.get("ULTRAMEMORY_HOOK_BUDGET") or 2000)
        except Exception:
            budget = 2000
        seen = sorted(cache.seen_get(session_id)) if cache else []
        # (c) preview-tier request: the server renders non-policy facts as one-liners under a
        # tight budget (whole [COMPANY POLICY] cards exempt); exclude_ids dedupes fact_ids this
        # session already holds so freed budget flows to new facts.
        req = {"query": query, "k": 5, "mode": "preview", "max_characters": budget, "exclude_ids": seen}
        if scope:
            req["scope"] = scope
        import urllib.request
        r = urllib.request.Request(
            os.environ.get("UM_API_BASE", "https://api.ultramemory.us").rstrip("/") + "/api/v1/recall/gated",
            data=json.dumps(req).encode("utf-8"),
            headers={"Authorization": "Bearer " + os.environ.get("UM_API_KEY", ""), "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(r, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return  # fail-open: network/HTTP/JSON trouble injects nothing
        if not isinstance(data, dict):
            return
        if cache:
            cache.memo_put(query, scope, None, response=data)

    block = (data.get("context_block") or "").strip()
    # Policy-bearing briefings are ALWAYS injected whole, regardless of the threshold below —
    # the anti-confabulation wedge (the gate may abstain while a governing policy still binds).
    if "[COMPANY POLICY]" not in block:
        # Metamemory gate: when memory is unsure/empty it abstains — stay quiet, inject nothing.
        if data.get("decision") == "abstain":
            return
        # (d) client threshold: the decision encodes the confidence band (metamemory Rule 10:
        # high -> answer, medium -> verify, low -> abstain). Skip when the band is below
        # ULTRAMEMORY_MIN_CONFIDENCE; the default "low" skips nothing beyond the abstain rule,
        # so only medium/high (verify/answer) inject — exactly the previous behavior.
        bands = {"low": 0, "medium": 1, "high": 2}
        floor = bands.get(os.environ.get("ULTRAMEMORY_MIN_CONFIDENCE", "low").strip().lower(), 0)
        band = {"abstain": 0, "verify": 1, "answer": 2}.get(str(data.get("decision")), 0)
        if band < floor:
            return
    if not block:
        return
    # (f) remember what this session has now been shown -> the next turn dedupes via exclude_ids.
    if cache:
        ids = [f.get("fact_id") for f in (data.get("results") or []) if isinstance(f, dict) and f.get("fact_id")]
        cache.seen_add(session_id, ids)
    emit(block)


try:
    main()
except Exception:
    pass  # fail-open, always
'
exit 0
