"""Unit tests for the UltraMemory Hermes memory provider.

Stubs the Hermes host modules (`agent.memory_provider`, `tools.registry`) so the plugin imports and
its ABC conformance is validated WITHOUT Hermes installed, then exercises every hook against a
mocked UltraMemory API via httpx.MockTransport (no network).
"""
import importlib.util
import json
import os
import re
import sys
import types
from abc import ABC, abstractmethod

import httpx
import pytest


def _have(modname: str) -> bool:
    try:
        return importlib.util.find_spec(modname) is not None
    except Exception:
        return False


def _install_host_stubs():
    """Stub the Hermes host modules ONLY if they aren't really importable, and without clobbering
    any real top-level `agent`/`tools` package that might exist in a full-repo test run."""
    if not _have("agent.memory_provider"):
        agent_pkg = sys.modules.get("agent")
        if agent_pkg is None:
            agent_pkg = types.ModuleType("agent")
            agent_pkg.__path__ = []  # mark as package
            sys.modules["agent"] = agent_pkg
        mp = types.ModuleType("agent.memory_provider")

        class MemoryProvider(ABC):
            @property
            @abstractmethod
            def name(self): ...

            @abstractmethod
            def is_available(self): ...

            @abstractmethod
            def initialize(self, session_id, **kwargs): ...

            @abstractmethod
            def get_tool_schemas(self): ...

        mp.MemoryProvider = MemoryProvider
        sys.modules["agent.memory_provider"] = mp

    if not _have("tools.registry"):
        tools_pkg = sys.modules.get("tools")
        if tools_pkg is None:
            tools_pkg = types.ModuleType("tools")
            tools_pkg.__path__ = []
            sys.modules["tools"] = tools_pkg
        reg = types.ModuleType("tools.registry")
        reg.tool_error = lambda msg: json.dumps({"error": msg})
        sys.modules["tools.registry"] = reg


_install_host_stubs()

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("um_provider", os.path.join(_HERE, "__init__.py"))
um = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(um)


@pytest.fixture(autouse=True)
def _sandbox_cache_home(monkeypatch, tmp_path):
    """Isolate ~/.ultramemory/cache.json for EVERY test (token economics C3 regression fix).

    prefetch() now consults/writes the memo + seen cache by default (ULTRAMEMORY_PREVIEW unset),
    so without a sandboxed HOME: (a) cross-test memo hits skip HTTP — the 402-paywall fallback
    and space=both assertions never see a request — and (b) every run reads/writes the user's
    REAL ~/.ultramemory/cache.json. Each test gets its own throwaway HOME instead.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ULTRAMEMORY_CACHE", raising=False)
    monkeypatch.delenv("ULTRAMEMORY_PREVIEW", raising=False)


class _Recorder:
    def __init__(self):
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode() or "{}") if request.content else {}
        self.calls.append((request.url.path, body))
        path = request.url.path
        if path == "/api/v1/recall/gated":
            if "nothing" in body.get("query", ""):
                return httpx.Response(200, json={"event_id": "e1", "decision": "abstain", "confidence": 0.1,
                                                 "results": [], "context_block": "retrieve first"})
            return httpx.Response(200, json={"event_id": "e2", "decision": "answer", "confidence": 0.91,
                                             "results": [{"entity": "Acme", "key": "plan", "value": "Pro"}],
                                             "context_block": "Acme · plan = Pro"})
        if path == "/api/v1/recall":
            return httpx.Response(200, json={"results": [{"entity": "Acme", "key": "plan", "value": "Pro"}],
                                             "count": 1})
        if path == "/api/v1/permanent":
            return httpx.Response(200, json={"fact_id": "f1", "deduped": False, "superseded": 0})
        if path == "/api/v1/rollup":
            return httpx.Response(200, json={"written": 1, "deduped": 0, "skipped": 0})
        if path == "/api/v1/playbook/recall":
            return httpx.Response(200, json={"results": [], "count": 0})
        if path == "/api/v1/feedback":
            return httpx.Response(200, json={"event_id": body.get("event_id"), "recorded": True})
        if path.startswith("/api/v1/playbook/") and path.endswith("/outcome"):
            return httpx.Response(200, json={"entry_id": path.split("/")[4], "uses": 1, "wins": 1 if body.get("win") else 0})
        return httpx.Response(404, json={"error": "no route"})


def _make(monkeypatch, recorder, workspace="Acme Corp/Bot!", **env):
    monkeypatch.setenv("ULTRAMEMORY_API_KEY", env.get("api_key", "um_live_test"))
    monkeypatch.setenv("HERMES_HOME", "/tmp/nonexistent-hermes-home-um-tests")
    for k in ("gated", "auto_capture", "recall_k", "feedback", "space"):
        if k in env:
            monkeypatch.setenv("ULTRAMEMORY_" + k.upper(), env[k])
    p = um.UltraMemoryProvider()
    p.initialize("sess-1", agent_workspace=workspace, platform="cli")
    p._client = httpx.Client(
        transport=httpx.MockTransport(recorder.handler),
        base_url=p._base_url,
        headers={"Authorization": f"Bearer {p._api_key}", "Content-Type": "application/json"},
    )
    return p


def test_name_and_availability(monkeypatch):
    p = _make(monkeypatch, _Recorder())
    assert p.name == "ultramemory"
    assert p.is_available() is True


def test_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("ULTRAMEMORY_API_KEY", raising=False)
    monkeypatch.setenv("HERMES_HOME", "/tmp/nonexistent-hermes-home-um-tests")
    assert um.UltraMemoryProvider().is_available() is False


def test_scope_slugified_to_pattern(monkeypatch):
    p = _make(monkeypatch, _Recorder(), workspace="Acme Corp/Bot! 2026")
    assert re.fullmatch(r"[A-Za-z0-9_:.\-]+", p._scope)
    assert len(p._scope) <= 64


def test_prefetch_gated_answer_injects_block(monkeypatch):
    rec = _Recorder()
    p = _make(monkeypatch, rec)
    out = p.prefetch("what plan is Acme on?")
    assert "Remembered (UltraMemory" in out and "Acme" in out and "Pro" in out
    assert rec.calls[-1][0] == "/api/v1/recall/gated"
    assert rec.calls[-1][1]["scope"] == p._scope


def test_prefetch_gated_abstain_injects_nothing(monkeypatch):
    p = _make(monkeypatch, _Recorder())
    assert p.prefetch("tell me nothing grounded") == ""


def test_prefetch_gated_abstain_with_results_injects_marked(monkeypatch):
    """R0-A: abstain WITH results still injects the briefing, clearly marked low-confidence, instead
    of vanishing (mirrors the server _ask_decision remap). A true-empty result set still injects ''."""
    class _AbstainRec(_Recorder):
        def handler(self, request):
            body = json.loads(request.content.decode() or "{}") if request.content else {}
            self.calls.append((request.url.path, body))
            if request.url.path == "/api/v1/recall/gated":
                return httpx.Response(200, json={
                    "event_id": "e-abstain", "decision": "abstain", "confidence": 0.37,
                    "results": [{"entity": "Acme", "key": "plan", "value": "Pro", "fact_id": "f-abstain"}],
                    "context_block": "Acme · plan = Pro",
                })
            return httpx.Response(404, json={"error": "no route"})

    p = _make(monkeypatch, _AbstainRec())
    out = p.prefetch("what plan is Acme on?")
    assert "Acme" in out and "Pro" in out       # the fact reaches the agent (not dropped)
    assert "low confidence" in out.lower()       # clearly marked as low confidence


def test_prefetch_ungated(monkeypatch):
    rec = _Recorder()
    p = _make(monkeypatch, rec, gated="false")
    out = p.prefetch("plan?")
    assert "Remembered (UltraMemory)" in out
    assert rec.calls[-1][0] == "/api/v1/recall"


def test_tool_write_and_recall(monkeypatch):
    rec = _Recorder()
    p = _make(monkeypatch, rec)
    res = json.loads(p.handle_tool_call("memory_write", {"entity": "Acme", "key": "plan", "value": "Pro"}))
    assert res["stored"] is True and res["fact_id"] == "f1"
    assert rec.calls[-1][0] == "/api/v1/permanent" and rec.calls[-1][1]["scope"] == p._scope
    res2 = json.loads(p.handle_tool_call("recall_gated", {"query": "plan?"}))
    assert res2["decision"] == "answer"


def test_tool_write_validation(monkeypatch):
    p = _make(monkeypatch, _Recorder())
    res = json.loads(p.handle_tool_call("memory_write", {"entity": "", "key": "", "value": ""}))
    assert "error" in res


def test_unknown_tool(monkeypatch):
    p = _make(monkeypatch, _Recorder())
    assert "error" in json.loads(p.handle_tool_call("nope", {}))


def test_sync_turn_and_on_memory_write(monkeypatch):
    rec = _Recorder()
    p = _make(monkeypatch, rec)
    p.sync_turn("hi there", "hello back")
    assert rec.calls[-1][0] == "/api/v1/permanent"
    assert rec.calls[-1][1]["entity"].startswith("session:")
    p.on_memory_write("add", "user", "likes dark mode", {"key": "pref:theme"})
    assert rec.calls[-1][1]["key"] == "pref:theme"
    n = len(rec.calls)
    p.on_memory_write("remove", "user", "x")  # no API delete -> no call
    assert len(rec.calls) == n


def test_on_session_end_digest(monkeypatch):
    rec = _Recorder()
    p = _make(monkeypatch, rec)
    p.on_session_end([{"role": "user", "content": "first"}, {"role": "assistant", "content": "ok"},
                      {"role": "user", "content": "second"}])
    # session_end now posts a whole-session rollup (both roles), not a user-only permanent digest
    assert rec.calls[-1][0] == "/api/v1/rollup"
    body = rec.calls[-1][1]
    assert "first" in body["session_text"] and "second" in body["session_text"]
    assert "ok" in body["session_text"]  # assistant content is now included (both roles)
    assert body["source"].endswith(":session_end")
    assert body["scope"] == p._scope
    assert "observed_at" in body


def test_auto_capture_off(monkeypatch):
    rec = _Recorder()
    p = _make(monkeypatch, rec, auto_capture="false")
    p.sync_turn("a", "b")
    assert rec.calls == []  # nothing captured


def test_tool_schemas_shape(monkeypatch):
    p = _make(monkeypatch, _Recorder())
    schemas = p.get_tool_schemas()
    assert {s["name"] for s in schemas} == {"memory_write", "memory_recall", "recall_gated", "playbook_recall", "playbook_outcome"}
    for s in schemas:
        assert s["parameters"]["type"] == "object"
    by_name = {s["name"]: s for s in schemas}
    # `space` is exposed on exactly the three space-aware tools, with the right enums, and is OPTIONAL.
    write = by_name["memory_write"]["parameters"]
    assert write["properties"]["space"]["enum"] == ["private", "shared"]
    assert "space" not in write.get("required", [])
    for name in ("memory_recall", "recall_gated"):
        params = by_name[name]["parameters"]
        assert params["properties"]["space"]["enum"] == ["private", "shared", "both"]
        assert "space" not in params.get("required", [])
    # …and NOT on the playbook tools (playbook bodies carry no space).
    assert "space" not in by_name["playbook_recall"]["parameters"]["properties"]
    assert "space" not in by_name["playbook_outcome"]["parameters"]["properties"]


def test_resilient_when_api_down(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("down")

    monkeypatch.setenv("ULTRAMEMORY_API_KEY", "um_live_test")
    monkeypatch.setenv("HERMES_HOME", "/tmp/nonexistent-hermes-home-um-tests")
    p = um.UltraMemoryProvider()
    p.initialize("s", agent_workspace="w")
    p._client = httpx.Client(transport=httpx.MockTransport(boom), base_url=p._base_url)
    assert p.prefetch("q") == ""
    assert p.on_pre_compress([{"role": "user", "content": "q"}]) == ""
    p.sync_turn("a", "b")  # must not raise
    assert "error" in json.loads(p.handle_tool_call("memory_recall", {"query": "q"}))


def test_prefetch_gated_402_falls_back_to_plain_recall(monkeypatch):
    """Free tier: recall_gated is paywalled (402) — prefetch must fall back to plain recall and
    STILL inject memory (instead of silently injecting nothing), and surface the gap once."""
    rec = _Recorder()

    def handler(request):
        if request.url.path == "/api/v1/recall/gated":
            return httpx.Response(402, json={"detail": "recall_gated requires a paid plan"})
        return rec.handler(request)

    p = _make(monkeypatch, rec)
    p._client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=p._base_url,
        headers={"Authorization": f"Bearer {p._api_key}", "Content-Type": "application/json"},
    )
    out = p.prefetch("what plan is Acme on?")
    assert "Remembered (UltraMemory)" in out and "Acme" in out  # fell back to plain recall
    assert rec.calls[-1][0] == "/api/v1/recall"  # the fallback path was used
    assert "gated_paywall" in p._notified  # surfaced once


# Window-accurate 429 mock — the shape the backend actually emits for a hit usage window
# (tenancy._limit_exceeded): friendly detail + machine-readable X-Limit-* headers.
_LIMIT_429_DETAIL = (
    "You've reached your free plan's 5-hour limit (25 of 25). Your memories are safe — recall "
    "still works. It resets at 2026-07-07T09:15:00+00:00, or upgrade for more headroom: "
    "https://app.ultramemory.us/upgrade"
)
_LIMIT_429_HEADERS = {
    "Retry-After": "1800",
    "X-Limit-Window": "5h",
    "X-Limit-Reset": "2026-07-07T09:15:00+00:00",
    "X-Upgrade-Url": "https://app.ultramemory.us/upgrade",
}


def _make_with_429_writes(monkeypatch, rec):
    """Provider whose /api/v1/permanent writes 429 with the window-accurate structured response."""
    def handler(request):
        if request.url.path == "/api/v1/permanent":
            return httpx.Response(429, json={"detail": _LIMIT_429_DETAIL}, headers=_LIMIT_429_HEADERS)
        return rec.handler(request)

    p = _make(monkeypatch, rec)
    p._client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=p._base_url,
        headers={"Authorization": f"Bearer {p._api_key}", "Content-Type": "application/json"},
    )
    return p


def test_sync_turn_limit_429_relays_window_accurate_message(monkeypatch):
    """A blocked capture (429) must not be swallowed silently nor raise — the WINDOW-ACCURATE
    message (from the structured response, never an assumed window) rides the next turn's
    recall block, in-conversation."""
    rec = _Recorder()
    p = _make_with_429_writes(monkeypatch, rec)
    p.sync_turn("hi there", "hello back")  # must not raise even though the write is blocked
    out = p.prefetch("what plan is Acme on?")  # next turn: the limit message rides the recall block
    assert "5-hour limit" in out and "2026-07-07T09:15:00+00:00" in out
    assert "https://app.ultramemory.us/upgrade" in out
    assert "Remembered (UltraMemory" in out  # recall itself keeps working (fail-open)


def test_tool_memory_write_429_returns_structured_limit_reached(monkeypatch):
    """The memory_write tool relays a structured limit_reached (correct window + reset + upgrade
    link from the response headers) instead of the generic 'write failed'."""
    rec = _Recorder()
    p = _make_with_429_writes(monkeypatch, rec)
    res = json.loads(p.handle_tool_call("memory_write", {"entity": "Acme", "key": "plan", "value": "Pro"}))
    assert res["status"] == "limit_reached"
    assert res["window"] == "5h"
    assert res["reset_at"] == "2026-07-07T09:15:00+00:00"
    assert res["upgrade_url"] == "https://app.ultramemory.us/upgrade"
    assert res["memory_safe"] is True
    assert res["message"] == _LIMIT_429_DETAIL
    assert "error" not in res


def test_prefetch_attaches_limit_notice_once_per_turn(monkeypatch):
    """A >=80% X-Limit-State advisory on a recall response renders the wedge notice on the recall
    block at most once per turn; sync_turn closes the turn and the next turn re-attaches a fresh
    notice — even when recall abstains."""
    rec = _Recorder()
    state = json.dumps({"window": "5h", "label": "5-hour", "pct": 88, "used": 22, "cap": 25,
                        "resets_at": "2026-07-07T09:15:00+00:00", "scope": "org"})

    def handler(request):
        resp = rec.handler(request)
        if request.url.path == "/api/v1/recall/gated":
            resp.headers["X-Limit-State"] = state
        return resp

    p = _make(monkeypatch, rec)
    p._client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=p._base_url,
        headers={"Authorization": f"Bearer {p._api_key}", "Content-Type": "application/json"},
    )
    out1 = p.prefetch("what plan is Acme on?")
    assert "You're at 88% of your 5-hour limit (22 of 25)" in out1
    assert "https://app.ultramemory.us/upgrade" in out1
    out2 = p.prefetch("what seats does Acme have?")  # same turn -> the notice attached once only
    assert "88%" not in out2
    p.sync_turn("q", "a")  # closes the turn
    out3 = p.prefetch("tell me nothing grounded")  # abstain -> empty block, the notice still rides
    assert "You're at 88% of your 5-hour limit" in out3


def test_sync_turn_sends_feedback_when_memory_used(monkeypatch):
    rec = _Recorder()
    p = _make(monkeypatch, rec)
    p.prefetch("what plan is Acme on?")
    p.sync_turn("what plan is Acme on?", "Acme is on the Pro plan now.")
    fb = [c for c in rec.calls if c[0] == "/api/v1/feedback"]
    assert fb, "expected a feedback call"
    assert fb[-1][1]["event_id"] == "e2"
    assert fb[-1][1]["correct"] is True


def test_feedback_marks_unused_memory_incorrect(monkeypatch):
    rec = _Recorder()
    p = _make(monkeypatch, rec)
    p.prefetch("what plan is Acme on?")
    p.sync_turn("what plan is Acme on?", "Sorry, I do not have that on file right now.")
    fb = [c for c in rec.calls if c[0] == "/api/v1/feedback"]
    assert fb
    assert fb[-1][1]["correct"] is False


def test_no_feedback_when_disabled_or_abstain(monkeypatch):
    rec = _Recorder()
    p = _make(monkeypatch, rec, feedback="false")
    p.prefetch("what plan is Acme on?")
    p.sync_turn("q", "Acme is on the Pro plan")
    assert not [c for c in rec.calls if c[0] == "/api/v1/feedback"]

    rec2 = _Recorder()
    p2 = _make(monkeypatch, rec2)
    p2.prefetch("tell me nothing grounded")
    p2.sync_turn("q", "whatever")
    assert not [c for c in rec2.calls if c[0] == "/api/v1/feedback"]


def test_playbook_outcome_tool_reports_win(monkeypatch):
    rec = _Recorder(); p = _make(monkeypatch, rec)
    res = json.loads(p.handle_tool_call("playbook_outcome", {"entry_id": "pb1", "win": True}))
    assert rec.calls[-1][0] == "/api/v1/playbook/pb1/outcome"
    assert rec.calls[-1][1] == {"win": True}
    assert res["wins"] == 1


def test_playbook_outcome_validation(monkeypatch):
    p = _make(monkeypatch, _Recorder())
    assert "error" in json.loads(p.handle_tool_call("playbook_outcome", {"win": True}))


def test_write_body_carries_default_space_private(monkeypatch):
    """With no `space` config, every auto-write body targets the private member space."""
    rec = _Recorder()
    p = _make(monkeypatch, rec)
    p.sync_turn("hi there", "hello back")
    assert rec.calls[-1][0] == "/api/v1/permanent"
    assert rec.calls[-1][1]["space"] == "private"


def test_write_body_space_shared_when_configured(monkeypatch):
    """ULTRAMEMORY_SPACE=shared routes auto-writes to the shared team space."""
    rec = _Recorder()
    p = _make(monkeypatch, rec, space="shared")
    p.sync_turn("hi there", "hello back")
    assert rec.calls[-1][0] == "/api/v1/permanent"
    assert rec.calls[-1][1]["space"] == "shared"


def test_recall_bodies_carry_space_both(monkeypatch):
    """Auto-recall always reads everything it can see (space=both) — gated and plain paths."""
    rec = _Recorder()
    p = _make(monkeypatch, rec)
    p.prefetch("what plan is Acme on?")
    assert rec.calls[-1][0] == "/api/v1/recall/gated"
    assert rec.calls[-1][1]["space"] == "both"

    rec2 = _Recorder()
    p2 = _make(monkeypatch, rec2, gated="false")
    p2.prefetch("plan?")
    assert rec2.calls[-1][0] == "/api/v1/recall"
    assert rec2.calls[-1][1]["space"] == "both"


def test_tool_space_overrides_and_invalid_fallback(monkeypatch):
    """The explicit tools accept a per-call `space`; invalid values fall back (write→self._space,
    recall→both)."""
    rec = _Recorder()
    p = _make(monkeypatch, rec)  # default _space == "private"
    # memory_write tool override → shared
    p.handle_tool_call("memory_write", {"entity": "Acme", "key": "plan", "value": "Pro", "space": "shared"})
    assert rec.calls[-1][0] == "/api/v1/permanent"
    assert rec.calls[-1][1]["space"] == "shared"
    # memory_recall tool override → private
    p.handle_tool_call("memory_recall", {"query": "plan?", "space": "private"})
    assert rec.calls[-1][0] == "/api/v1/recall"
    assert rec.calls[-1][1]["space"] == "private"
    # invalid write override falls back to the configured default (private)
    p.handle_tool_call("memory_write", {"entity": "Acme", "key": "plan", "value": "Pro", "space": "bogus"})
    assert rec.calls[-1][1]["space"] == "private"
    # invalid recall override falls back to both
    p.handle_tool_call("memory_recall", {"query": "plan?", "space": "bogus"})
    assert rec.calls[-1][1]["space"] == "both"


# ---------------------------------------------------------------------------
# cache.py (checklist C1) — memo roundtrip+TTL, seen-set, kill-switch, corrupt file.
# Loaded standalone (same spec_from_file_location pattern as `um` above); every test
# points HOME at a pytest tmp dir so ~/.ultramemory/cache.json lands in a sandbox.
# ---------------------------------------------------------------------------

_cache_spec = importlib.util.spec_from_file_location("um_cache", os.path.join(_HERE, "cache.py"))
um_cache = importlib.util.module_from_spec(_cache_spec)
_cache_spec.loader.exec_module(um_cache)


def _tmp_home(monkeypatch, tmp_path):
    """Point the cache at a throwaway HOME; return the sandboxed cache.json path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ULTRAMEMORY_CACHE", raising=False)
    return os.path.join(str(tmp_path), ".ultramemory", "cache.json")


def test_cache_memo_roundtrip_and_ttl(monkeypatch, tmp_path):
    cache_file = _tmp_home(monkeypatch, tmp_path)
    resp = {"decision": "answer", "context_block": "Acme · plan = Pro",
            "results": [{"fact_id": "f1"}]}
    assert um_cache.memo_get("what plan?", "scope1", "both") is None  # cold miss
    um_cache.memo_put("what plan?", "scope1", "both", response=resp)
    assert um_cache.memo_get("what plan?", "scope1", "both") == resp  # hit
    assert um_cache.memo_get("  WHAT   plan? ", "scope1", "both") == resp  # normalized key
    assert um_cache.memo_get("what plan?", "other-scope", "both") is None  # scope in key
    assert os.path.exists(cache_file)
    # TTL: age the stored entry past MEMO_TTL_SECONDS (300 s) → same lookup misses.
    assert um_cache.MEMO_TTL_SECONDS == 300
    with open(cache_file) as fh:
        doc = json.load(fh)
    for entry in doc["memo"].values():
        entry["ts"] -= um_cache.MEMO_TTL_SECONDS + 1
    with open(cache_file, "w") as fh:
        json.dump(doc, fh)
    assert um_cache.memo_get("what plan?", "scope1", "both") is None


def test_cache_seen_set_roundtrip(monkeypatch, tmp_path):
    _tmp_home(monkeypatch, tmp_path)
    assert um_cache.seen_get("sess-A") == set()  # cold miss
    um_cache.seen_add("sess-A", ["f1", "f2"])
    um_cache.seen_add("sess-A", ["f2", "f3"])  # union — no duplicates
    assert um_cache.seen_get("sess-A") == {"f1", "f2", "f3"}
    assert um_cache.seen_get("sess-B") == set()  # per-session isolation


def test_cache_kill_switch_off_is_noop_miss(monkeypatch, tmp_path):
    cache_file = _tmp_home(monkeypatch, tmp_path)
    monkeypatch.setenv("ULTRAMEMORY_CACHE", "off")
    assert um_cache.memo_put("q", "s", "b", response={"x": 1}) is None
    assert um_cache.memo_get("q", "s", "b") is None
    assert um_cache.seen_add("sess", ["f1"]) is None
    assert um_cache.seen_get("sess") == set()
    assert not os.path.exists(cache_file)  # true no-op: touches no files


def test_cache_corrupt_file_tolerated(monkeypatch, tmp_path):
    cache_file = _tmp_home(monkeypatch, tmp_path)
    os.makedirs(os.path.dirname(cache_file), exist_ok=True)
    with open(cache_file, "w") as fh:
        fh.write("{ this is not json !!!")
    assert um_cache.memo_get("q", "s", "b") is None  # silent miss, no crash
    assert um_cache.seen_get("sess") == set()        # silent miss, no crash
    um_cache.memo_put("q", "s", "b", response={"ok": True})  # write starts fresh
    assert um_cache.memo_get("q", "s", "b") == {"ok": True}


# ---------------------------------------------------------------------------
# Token economics (checklist C5) — provider-level memoization, seen-set dedupe,
# kill-switch, corrupt-cache resilience, preview request shape, prompt stability.
# HOME is sandboxed per test by the autouse `_sandbox_cache_home` fixture, so
# ~/.ultramemory/cache.json is always a pytest tmp dir — never the real one.
# These fail if C1 (cache.py), C3 (__init__.py prefetch/system_prompt_block), or
# the cache logic the C2 hook shares (memo/seen/kill-switch/corrupt) is reverted.
# ---------------------------------------------------------------------------


class _EconRecorder:
    """Like _Recorder, but gated-recall results carry fact_ids (the seen-set fuel)."""

    def __init__(self):
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode() or "{}") if request.content else {}
        self.calls.append((request.url.path, body))
        if request.url.path == "/api/v1/recall/gated":
            return httpx.Response(200, json={
                "event_id": "e9", "decision": "answer", "confidence": 0.9,
                "results": [{"fact_id": "fA", "entity": "Acme", "key": "plan", "value": "Pro"},
                            {"fact_id": "fB", "entity": "Acme", "key": "seats", "value": "5"}],
                "context_block": "Acme · plan = Pro\nAcme · seats = 5",
            })
        return httpx.Response(404, json={"error": "no route"})


def _gated_calls(rec):
    return [c for c in rec.calls if c[0] == "/api/v1/recall/gated"]


def test_prefetch_memoizes_identical_query(monkeypatch):
    """2nd identical prefetch within the 5-min TTL renders from the memo — 1 HTTP request."""
    rec = _EconRecorder()
    p = _make(monkeypatch, rec)
    out1 = p.prefetch("what plan is Acme on?")
    out2 = p.prefetch("what plan is Acme on?")
    assert len(_gated_calls(rec)) == 1  # transport saw exactly one request
    assert "Remembered (UltraMemory" in out1 and out1 == out2  # cache renders identically


def test_prefetch_second_request_sends_exclude_ids(monkeypatch):
    """Fact_ids delivered on turn 1 ride the next request as exclude_ids (session dedupe)."""
    rec = _EconRecorder()
    p = _make(monkeypatch, rec)
    p.prefetch("what plan is Acme on?")          # delivers fA, fB -> seen-set
    p.prefetch("how many seats does Acme have?")  # new query -> real HTTP, deduped
    gated = _gated_calls(rec)
    assert len(gated) == 2
    assert "exclude_ids" not in gated[0][1]        # nothing seen on the first turn
    assert gated[1][1]["exclude_ids"] == ["fA", "fB"]  # sorted seen-set on the second


def test_cache_kill_switch_prefetch_makes_two_requests(monkeypatch):
    """ULTRAMEMORY_CACHE=off disables memo+seen: identical prefetches both hit HTTP."""
    monkeypatch.setenv("ULTRAMEMORY_CACHE", "off")
    rec = _EconRecorder()
    p = _make(monkeypatch, rec)
    p.prefetch("what plan is Acme on?")
    p.prefetch("what plan is Acme on?")
    gated = _gated_calls(rec)
    assert len(gated) == 2                                     # no memo hit
    assert all("exclude_ids" not in b for _, b in gated)       # no seen-set either


def test_prefetch_tolerates_corrupt_cache_file(monkeypatch, tmp_path):
    """A corrupt ~/.ultramemory/cache.json must never break prefetch — and is rebuilt fresh."""
    cache_file = os.path.join(str(tmp_path), ".ultramemory", "cache.json")
    os.makedirs(os.path.dirname(cache_file), exist_ok=True)
    with open(cache_file, "w") as fh:
        fh.write("{ this is not json !!!")
    rec = _EconRecorder()
    p = _make(monkeypatch, rec)
    out = p.prefetch("what plan is Acme on?")  # must not raise; falls through to HTTP
    assert "Remembered (UltraMemory" in out
    assert len(_gated_calls(rec)) == 1
    p.prefetch("what plan is Acme on?")        # cache was rebuilt -> memo hit, still 1 request
    assert len(_gated_calls(rec)) == 1


def test_prefetch_body_carries_preview_mode_and_off_restores_legacy_shape(monkeypatch):
    """Default gated prefetch requests mode=preview; ULTRAMEMORY_PREVIEW=off sends today's
    exact legacy body (no mode / exclude_ids keys) and skips the cache entirely."""
    rec = _EconRecorder()
    p = _make(monkeypatch, rec)
    p.prefetch("what plan is Acme on?")
    assert _gated_calls(rec)[-1][1]["mode"] == "preview"
    monkeypatch.setenv("ULTRAMEMORY_PREVIEW", "off")
    p.prefetch("what plan is Acme on?")  # identical query — but preview off bypasses the memo
    gated = _gated_calls(rec)
    assert len(gated) == 2
    assert gated[-1][1] == {"query": "what plan is Acme on?", "scope": p._scope,
                            "space": "both", "k": p._recall_k}


def test_system_prompt_block_byte_stable(monkeypatch):
    """system_prompt_block must be byte-identical across calls (prompt-cache friendly)."""
    rec = _EconRecorder()
    p = _make(monkeypatch, rec)
    b1 = p.system_prompt_block()
    assert b1  # non-empty when an API key is configured
    assert p.system_prompt_block() == b1
    p.prefetch("what plan is Acme on?")   # state changes must not perturb the block
    assert p.system_prompt_block() == b1


# --- autoconfig B5: session-start onboarding hook on the provider plugin entrypoint ---
class _HookCtx:
    """Fake Hermes plugin context (Level-A harness): records registrations."""
    def __init__(self, with_hooks=True):
        self.providers = []
        self.hooks = []
        if not with_hooks:
            # older Hermes hosts have no register_hook at all
            self.register_hook = None  # type: ignore[assignment]

    def register_memory_provider(self, p):
        self.providers.append(p)

    def register_hook(self, event, cb):
        self.hooks.append((event, cb))


def test_register_adds_on_session_start_hook(monkeypatch, tmp_path, capsys):
    """B5 Verify: provider hook registers without error in the fake-hermes harness,
    and the callback prints the onboarding line (silent when opted out)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ctx = _HookCtx()
    um.register(ctx)
    assert len(ctx.providers) == 1 and ctx.providers[0].name == "ultramemory"
    assert len(ctx.hooks) == 1
    event, cb = ctx.hooks[0]
    assert event == "on_session_start"
    cb()  # prints the onboarding line (incl. the MCP-approvals honesty note)
    out = capsys.readouterr().out
    assert "ultramemory configure --platform hermes" in out
    assert "#16462" in out  # honest: MCP tools bypass approvals today
    # opt-out file silences it
    optout = tmp_path / ".ultramemory"
    optout.mkdir()
    (optout / "onboard-optout").write_text("")
    cb()
    assert capsys.readouterr().out == ""


def test_register_survives_host_without_register_hook():
    """Older Hermes hosts (no ctx.register_hook) must still register the provider."""
    ctx = _HookCtx(with_hooks=False)
    um.register(ctx)  # must not raise
    assert len(ctx.providers) == 1
