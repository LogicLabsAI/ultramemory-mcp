#!/usr/bin/env bash
# UltraMemory recall-first hook for Claude Code (UserPromptSubmit event).
# On every prompt it recalls your top matches from UltraMemory and injects them as context.
# Token-economical (v1.7.0): requests the PREVIEW briefing tier (server renders non-policy facts
# as one-liners; whole [COMPANY POLICY] cards are exempt), memoizes responses for 5 minutes and
# dedupes per-session via cache.py (exclude_ids), applies a client confidence threshold, and
# hard-caps the injection at 9,500 chars (Claude Code's additionalContext limit is 10,000).
# Limit-aware (v1.8.0): surfaces the >=80% usage notice (X-Limit-State header / limit_notice
# field) inline as one "[UltraMemory] …" line, injects the friendly 429 limit_reached message
# instead of failing silent, and escalates the injection cap on [COMPANY POLICY] turns
# (ULTRAMEMORY_HOOK_POLICY_BUDGET, default 12000) so policies arrive whole.
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


def emit(block, cap=9500):
    # (e) hard client truncation: Claude Code caps additionalContext at 10,000 chars — stay
    # safely under it at 9,500 by default. P3-C1 flag ④: policy turns pass a HIGHER cap
    # (ULTRAMEMORY_HOOK_POLICY_BUDGET, default 12000) so [COMPANY POLICY] cards arrive whole.
    block = block[:cap]
    if block.strip():
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": block}}))


def limit_state_notice(raw):
    # P3-C1(a): render the X-Limit-State advisory header ({window,label,pct,used,cap,resets_at,
    # scope} — the server sends it ONLY when an enforced limit is >=80%) as the same wedge notice
    # the MCP layer emits. Malformed header -> None: advisory only, never break a working recall.
    try:
        state = json.loads(raw)
        label, resets = state["label"], state["resets_at"]
        cap = int(state["cap"])
        pct, used = min(int(state["pct"]), 100), min(int(state["used"]), cap)
    except Exception:
        return None
    lead = (f"You\x27re at {pct}% of your {label} limit ({used} of {cap}). "
            f"At 100% new memory writes pause and resume automatically at {resets}")
    if state.get("scope") == "member":
        return lead + ". To increase your limit, contact your workspace admin."
    return lead + " — or upgrade your plan: https://app.ultramemory.us/upgrade."


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
        import urllib.error
        import urllib.request
        r = urllib.request.Request(
            os.environ.get("UM_API_BASE", "https://api.ultramemory.us").rstrip("/") + "/api/v1/recall/gated",
            data=json.dumps(req).encode("utf-8"),
            headers={"Authorization": "Bearer " + os.environ.get("UM_API_KEY", ""), "Content-Type": "application/json"},
            method="POST",
        )
        advisory = ""
        try:
            with urllib.request.urlopen(r, timeout=8) as resp:
                advisory = resp.headers.get("X-Limit-State") or ""
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # P3-C1(a): a 429 limit_reached is INJECTED, not swallowed — the user must see why
            # memory paused + when it resumes. Every other status stays fail-open (inject nothing;
            # the turn is never blocked either way).
            if e.code == 429:
                try:
                    detail = json.loads(e.read().decode("utf-8")).get("detail")
                except Exception:
                    detail = None
                if not (isinstance(detail, str) and detail.strip()):
                    detail = "usage limit reached — memory writes pause and resume automatically; your memories are safe."
                emit("[UltraMemory] " + detail.strip())
            return
        except Exception:
            return  # fail-open: network/HTTP/JSON trouble injects nothing
        if not isinstance(data, dict):
            return
        # P3-C1(a): fold the >=80% advisory header into the response BEFORE memoizing, so a
        # cache replay within the memo TTL still carries the notice.
        if advisory and "limit_notice" not in data:
            rendered = limit_state_notice(advisory)
            if rendered:
                data["limit_notice"] = rendered
        if cache:
            cache.memo_put(query, scope, None, response=data)

    block = (data.get("context_block") or "").strip()
    # Policy-bearing briefings are ALWAYS injected whole, regardless of the gates below —
    # the anti-confabulation wedge (the gate may abstain while a governing policy still binds).
    policy = "[COMPANY POLICY]" in block
    if not policy:
        results = data.get("results") or []
        # R0-A: abstain WITH results still injects the briefing marked low-confidence (do not drop it);
        # a true-empty result set / empty block still injects nothing.
        if data.get("decision") == "abstain":
            # R1: on abstain-WITH-results, try ONE higher-precision verified recall (cross-encoder
            # rerank) before settling for the low-confidence briefing. verified=True is sent ONLY on
            # this retry; ANY error falls back to the original low-confidence block (fail-open).
            verified_block = None
            if block and results:
                try:
                    import urllib.error  # noqa: F401
                    import urllib.request
                    vbody = dict(req)
                    vbody["verified"] = True
                    vreq = urllib.request.Request(
                        os.environ.get("UM_API_BASE", "https://api.ultramemory.us").rstrip("/") + "/api/v1/recall/gated",
                        data=json.dumps(vbody).encode("utf-8"),
                        headers={"Authorization": "Bearer " + os.environ.get("UM_API_KEY", ""), "Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(vreq, timeout=8) as vresp:
                        vdata = json.loads(vresp.read().decode("utf-8"))
                    if isinstance(vdata, dict) and vdata.get("decision") in ("answer", "verify"):
                        vblock = (vdata.get("context_block") or "").strip()
                        if vblock:
                            verified_block = vblock + "\n(verified recall)"
                except Exception:
                    verified_block = None
            if verified_block:
                block = verified_block
            else:
                block = (block + "\n(low confidence — verify before relying)") if (block and results) else ""
        else:
            # (d) client threshold: the decision encodes the confidence band (metamemory Rule 10:
            # high -> answer, medium -> verify, low -> abstain). Skip when the band is below
            # ULTRAMEMORY_MIN_CONFIDENCE; the default "low" skips nothing beyond the abstain rule,
            # so only medium/high (verify/answer) inject — exactly the previous behavior.
            bands = {"low": 0, "medium": 1, "high": 2}
            floor = bands.get(os.environ.get("ULTRAMEMORY_MIN_CONFIDENCE", "low").strip().lower(), 0)
            band = {"abstain": 0, "verify": 1, "answer": 2}.get(str(data.get("decision")), 0)
            if band < floor:
                block = ""
    # (f) remember what this session has now been shown -> the next turn dedupes via exclude_ids.
    if block and cache:
        ids = [f.get("fact_id") for f in (data.get("results") or []) if isinstance(f, dict) and f.get("fact_id")]
        cache.seen_add(session_id, ids)
    # P3-C1 flag ④: policy turns escalate the injection cap (env-overridable, default 12000)
    # so whole [COMPANY POLICY] cards survive client truncation; non-policy turns keep 9500.
    cap = 9500
    if policy:
        try:
            cap = int(os.environ.get("ULTRAMEMORY_HOOK_POLICY_BUDGET") or 12000)
        except Exception:
            cap = 12000
    # P3-C1(a): append the >=80% limit notice as one short line the terminal user SEES — it
    # rides even briefing-less turns (abstain / below-threshold) while an enforced limit is hot.
    notice = data.get("limit_notice")
    if isinstance(notice, str) and notice.strip():
        line = "[UltraMemory] " + notice.strip()
        keep = max(0, cap - len(line) - 1)
        block = (block[:keep].rstrip() + "\n" + line) if block else line
    if not block:
        return
    emit(block, cap)


try:
    main()
except Exception:
    pass  # fail-open, always
'
exit 0
