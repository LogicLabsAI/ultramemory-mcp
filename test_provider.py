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
    for k in ("gated", "auto_capture", "recall_k", "feedback"):
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
    assert rec.calls[-1][0] == "/api/v1/permanent"
    assert "first" in rec.calls[-1][1]["value"] and "second" in rec.calls[-1][1]["value"]


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


def test_sync_turn_write_quota_429_notifies_and_survives(monkeypatch):
    """An over-quota write (429) must not be swallowed silently nor raise — it notifies once so the
    agent/operator knows memory stopped growing."""
    rec = _Recorder()

    def handler(request):
        if request.url.path == "/api/v1/permanent":
            return httpx.Response(429, json={"detail": "monthly quota exceeded"})
        return rec.handler(request)

    p = _make(monkeypatch, rec)
    p._client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=p._base_url,
        headers={"Authorization": f"Bearer {p._api_key}", "Content-Type": "application/json"},
    )
    p.sync_turn("hi there", "hello back")  # must not raise even though the write is blocked
    assert "write_quota" in p._notified


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
