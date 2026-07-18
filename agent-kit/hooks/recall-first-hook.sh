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
# Observable (v1.9.4): deadline-aware timeouts (ULTRAMEMORY_HOOK_DEADLINE, default 9s) so the
# briefing always emits before the registration kill; once-per-session dead-key (401/403) notice;
# one metadata-only receipt line per run in ~/.ultramemory/hook.log (ULTRAMEMORY_HOOK_LOG=off
# disables) — never memory contents, the query, or the key.
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
import json, os, sys, time

# T3 (1.9.4): per-invocation receipt state — outcome/injected_chars/retry_used/cache flag.
# main() overwrites t0 at its start (module init is microseconds earlier; this is the fallback).
ST = {"t0": time.monotonic(), "outcome": None, "injected": 0, "retry": 0, "cache": False}


def emit(block, cap=9500):
    # (e) hard client truncation: Claude Code caps additionalContext at 10,000 chars — stay
    # safely under it at 9,500 by default. P3-C1 flag ④: policy turns pass a HIGHER cap
    # (ULTRAMEMORY_HOOK_POLICY_BUDGET, default 12000) so [COMPANY POLICY] cards arrive whole.
    block = block[:cap]
    if block.strip():
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": block}}))
        return len(block)  # T3: injected_chars for the receipt line (output shape unchanged)
    return 0


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


def hook_log():
    # T3 (1.9.4): append ONE metadata-only receipt per invocation to ~/.ultramemory/hook.log —
    # ISO ts | recall | outcome | duration_ms | injected_chars | retry_used. NEVER memory
    # contents, the query, or the key (Rule 6). ULTRAMEMORY_HOOK_LOG=off disables. The WHOLE
    # logger is try/except-wrapped: a logging failure can never break the fail-open contract.
    try:
        if (os.environ.get("ULTRAMEMORY_HOOK_LOG") or "").strip().lower() == "off":
            return
        import datetime
        outcome = ST["outcome"] or ("cache_hit" if ST["cache"] else ("injected" if ST["injected"] else "abstain"))
        duration_ms = int((time.monotonic() - ST["t0"]) * 1000)
        directory = os.path.join(os.path.expanduser("~"), ".ultramemory")
        path = os.path.join(directory, "hook.log")
        # dir 0700 / file 0600 + os.replace rotation at 1 MiB (single generation) — the same
        # on-disk idiom as cli.py _write_cache_skeleton.
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        try:
            if os.path.getsize(path) > 1048576:
                os.replace(path, path + ".1")
        except OSError:
            pass
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s | recall | %s | %d | %d | %d\n" % (ts, outcome, duration_ms, ST["injected"], ST["retry"]))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        pass


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
    # T1 (1.9.4): deadline-aware. t0 + ULTRAMEMORY_HOOK_DEADLINE (default 9s) bound ALL HTTP in
    # this run so emit() always fires before the hook registration timeout can kill the process.
    ST["t0"] = t0 = time.monotonic()
    try:
        deadline = float(os.environ.get("ULTRAMEMORY_HOOK_DEADLINE") or 9)
    except Exception:
        deadline = 9.0
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
    query = query.strip()[:4096]  # server RecallRequest.query max_length (schemas.py) — prevents 422s and oversized bodies
    if not query:
        return
    scope = os.environ.get("UM_SCOPE", "").strip()
    cache = load_cache()
    # E1 (1.9.6): budget is computed ABOVE the cache check so the verified-retry body
    # below can reference it on BOTH the memo-hit and miss paths.
    try:
        budget = int(os.environ.get("ULTRAMEMORY_HOOK_BUDGET") or 2000)
    except Exception:
        budget = 2000

    # (b) memo first: an identical query within 5 minutes renders from cache — zero HTTP.
    data = cache.memo_get(query, scope, None) if cache else None
    ST["cache"] = isinstance(data, dict)  # T3: a memo hit logs as cache_hit
    if not isinstance(data, dict):
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
        # T1: primary request never exceeds min(6, remaining) of the deadline budget.
        remaining = deadline - (time.monotonic() - t0)
        try:
            with urllib.request.urlopen(r, timeout=min(6, max(0.5, remaining))) as resp:
                advisory = resp.headers.get("X-Limit-State") or ""
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            ST["outcome"] = "http_%d" % e.code
            # T2 (1.9.4): dead key = 401, or a 403 whose body parses as the API JSON
            # {"detail": "<string>"} auth/suspension shape. Surface it ONCE per session (one
            # additionalContext line + one stderr line) instead of dying silently; dedupe via a
            # TMPDIR marker file keyed by session_id (same pattern as the capture-hook
            # counter_file). The key itself is NEVER printed, in whole or in part (Rule 6 /
            # hooks README privacy contract). A 403 WITHOUT that shape (e.g. a WAF block page)
            # is NOT a dead key: it keeps the http_403 outcome, emits NO dead-key notice, and
            # returns below as a plain HTTP failure (fail-open, no injection).
            dead_key = e.code == 401
            if not dead_key and e.code == 403:
                try:  # e.read() may be empty or non-JSON — any trouble means not a dead key
                    dead_key = isinstance(json.loads(e.read().decode("utf-8")).get("detail"), str)
                except Exception:
                    dead_key = False
            if dead_key:
                ST["outcome"] = "dead_key"
                # E5 (1.9.6): empty session_id falls back to a STABLE constant (once per
                # session semantics) — a pid fallback would spam the notice every invocation.
                marker = os.path.join(os.environ.get("TMPDIR") or "/tmp",
                                      "ultramemory-deadkey-" + (session_id or "nosession"))
                if not os.path.exists(marker):
                    msg = ("[UltraMemory] API key rejected (HTTP %d) — recall is offline. "
                           "Rotate your key at https://app.ultramemory.us" % e.code)
                    ST["injected"] = emit(msg)
                    print(msg, file=sys.stderr)
                    try:
                        open(marker, "w").close()
                    except Exception:
                        pass
                return
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
                ST["injected"] = emit("[UltraMemory] " + detail.strip())
            return
        except Exception as e:
            # fail-open: network/HTTP/JSON trouble injects nothing (T3 records the why)
            import socket
            if isinstance(e, socket.timeout) or isinstance(getattr(e, "reason", None), socket.timeout):
                ST["outcome"] = "timeout"
            else:
                ST["outcome"] = "error:" + type(e).__name__
            return
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
            # T1 (1.9.4): attempt the retry ONLY with >= 2.5s of deadline left; clamp its
            # timeout to the remainder so emit() below always gets to run.
            remaining = deadline - (time.monotonic() - t0)
            if block and results and remaining >= 2.5:
                try:
                    import urllib.error  # noqa: F401
                    import urllib.request
                    # E1 (1.9.6): rebuild the retry body from in-scope state — NEVER dict(req)
                    # (a miss-branch local; NameError on memo hits silently killed the retry).
                    # exclude_ids = the SESSION-SEEN set: exclude_ids shapes briefing rendering,
                    # so excluding the CURRENT results would gut the verified block.
                    vbody = {"query": query, "k": 5, "mode": "preview", "verified": True,
                             "max_characters": budget,
                             "exclude_ids": (sorted(cache.seen_get(session_id)) if cache else [])}
                    if scope:
                        vbody["scope"] = scope
                    vreq = urllib.request.Request(
                        os.environ.get("UM_API_BASE", "https://api.ultramemory.us").rstrip("/") + "/api/v1/recall/gated",
                        data=json.dumps(vbody).encode("utf-8"),
                        headers={"Authorization": "Bearer " + os.environ.get("UM_API_KEY", ""), "Content-Type": "application/json"},
                        method="POST",
                    )
                    ST["retry"] = 1  # T3: verified retry attempted this run
                    with urllib.request.urlopen(vreq, timeout=max(0.5, remaining)) as vresp:
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
    ST["injected"] = emit(block, cap)


try:
    main()
except Exception as exc:
    ST["outcome"] = ST["outcome"] or ("error:" + type(exc).__name__)  # fail-open, always
finally:
    hook_log()  # T3: the receipt itself is fully try/except-wrapped — logging can never block
'
exit 0
