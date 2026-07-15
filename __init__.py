"""UltraMemory memory provider for Hermes Agent (M6).

The premium, deep integration the MCP front door structurally can't offer:

  * prefetch        -> metamemory-GATED recall injected before each turn, so the agent
                       abstains-and-retrieves instead of confabulating (returns nothing when
                       memory has nothing grounded — never injects noise).
  * sync_turn       -> auto-captures each completed turn (deduped, idempotent server-side).
  * on_memory_write -> mirrors Hermes' built-in memory tool into UltraMemory.
  * on_pre_compress -> surfaces durable facts so they survive context compression.
  * on_session_end  -> persists a session digest (server consolidates nightly).
  * tools           -> the same pull-based surface as the MCP server
                       (memory_write / memory_recall / recall_gated / playbook_recall).

Tenancy (Rule 3): one UltraMemory API key == one tenant, resolved at the single server-side
chokepoint — we never send a tenant id from here. Hermes' agent_workspace / agent_identity maps
to a per-agent `scope` *within* that tenant, so separate agents/workspaces keep separate memory
under one account. The provider is synchronous (Hermes runs these hooks on its own threads) and
NEVER raises out of a hook — a memory backend hiccup must not break the agent.

Memory spaces (Teams): orthogonal to `scope`, each tenant has a `private` member space, a `shared`
team space, and `both` (read across them). `ULTRAMEMORY_SPACE` (private|shared, default private)
sets the target space for auto-writes and the `memory_write` tool default; auto-recall always reads
`both`, and the explicit recall tools take an optional `space` (private|shared|both). Precedence is
a backend rule: an explicit Hermes workspace `scope` takes priority over `space` (space only drives
resolution when `scope` is the default), so workspace-scoped deployments are unaffected.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("ultramemory.provider")

# --- client-side cache (token economics, C3): memoizes gated recall responses (5 min TTL) and
# tracks per-session seen fact_ids so follow-up prefetches can send exclude_ids. Fail-open: if
# cache.py can't be loaded, `_cache` is None and prefetch behaves exactly as without a cache. ---
try:
    from . import cache as _cache  # installed package: ultramemory.cache
except Exception:  # pragma: no cover - flat checkout / by-path import (test_provider.py)
    try:
        import importlib.util as _ilu

        _cache_spec = _ilu.spec_from_file_location(
            "ultramemory_client_cache",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.py"),
        )
        _cache = _ilu.module_from_spec(_cache_spec)
        _cache_spec.loader.exec_module(_cache)
    except Exception:
        _cache = None

# --- decouple from the host so the plugin imports & unit-tests without Hermes installed ---
try:  # real ABC at runtime inside Hermes
    from agent.memory_provider import MemoryProvider
except Exception:  # pragma: no cover - standalone import path
    from abc import ABC

    class MemoryProvider(ABC):  # minimal shim; Hermes supplies the real ABC in production
        pass

try:  # Hermes helper: formats a tool failure as a JSON string
    from tools.registry import tool_error
except Exception:  # pragma: no cover
    def tool_error(msg: str) -> str:
        return json.dumps({"error": msg})


DEFAULT_BASE_URL = "https://api.ultramemory.us"
# App-portal links for limit messaging — fallbacks when a 429/402 response carries no
# X-Upgrade-Url / X-Billing-Url header (mirrors the backend's app_base_url default).
DEFAULT_UPGRADE_URL = "https://app.ultramemory.us/upgrade"
DEFAULT_BILLING_URL = "https://app.ultramemory.us/app/billing"
# >=80% advisory limit-state header on successful data-path responses (U9): compact JSON
# {window,label,pct,used,cap,resets_at,scope}; absent below 80% (zero extra bytes).
_LIMIT_STATE_HEADER = "X-Limit-State"
# scope must satisfy the API's Scope pattern: ^[A-Za-z0-9_:.\-]+$ (max 64)
_SCOPE_DISALLOWED = re.compile(r"[^A-Za-z0-9_:.\-]")


def _slug_scope(s: Optional[str]) -> str:
    s = _SCOPE_DISALLOWED.sub("-", (s or "").strip())[:64].strip("-._:")
    return s or "default"


def _as_bool(v: Any, default: bool = True) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _as_int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(str(v).strip())
    except Exception:
        return default
    return max(lo, min(hi, n))


def _last_user_text(messages: Optional[List[Dict[str, Any]]]) -> str:
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"]
    return ""


def _fact_lines(facts: List[Dict[str, Any]], limit: int) -> List[str]:
    return [
        f"- {f.get('entity')} · {f.get('key')}: {f.get('value')}"
        for f in (facts or [])[:limit]
        if isinstance(f, dict)
    ]


def _render_limit_notice(header_value: str) -> Optional[str]:
    """Render the >=80% X-Limit-State advisory as the every-turn wedge notice (U9). scope='member'
    is the caller's own seat window (U14) -> route-to-admin wording; otherwise the upgrade link.
    Malformed header -> None: advisory only, never break a working recall block."""
    try:
        state = json.loads(header_value)
        pct, label, used, cap = state["pct"], state["label"], state["used"], state["cap"]
        resets_at, scope = state["resets_at"], state.get("scope")
        pct, used = min(int(pct), 100), min(int(used), int(cap))  # display clamp — never "104%" / "(26 of 25)"
    except Exception:
        return None
    lead = (
        f"You're at {pct}% of your {label} limit ({used} of {cap}). "
        f"At 100% new memory writes pause and resume automatically at {resets_at}"
    )
    if scope == "member":
        return f"{lead}. To increase your limit, contact your workspace admin."
    return f"{lead} — or upgrade your plan: {DEFAULT_UPGRADE_URL}."


def _limit_reached_payload(data: Optional[Dict[str, Any]], headers: Any) -> Dict[str, Any]:
    """Structured limit_reached dict for a 429, WINDOW-ACCURATE from the response itself
    (X-Limit-Window / X-Limit-Reset / X-Upgrade-Url headers + the friendly detail) — never an
    assumed window. No X-Limit-Window means the per-minute rate limiter fired (window 'rpm')."""
    detail = data.get("detail") if isinstance(data, dict) else None
    window = headers.get("X-Limit-Window") if headers is not None else None
    upgrade = (headers.get("X-Upgrade-Url") if headers is not None else None) or DEFAULT_UPGRADE_URL
    if window:
        return {
            "status": "limit_reached",
            "message": detail or "You've reached a usage limit on your current plan.",
            "window": window,
            "reset_at": headers.get("X-Limit-Reset"),
            "upgrade_url": upgrade,
            "memory_safe": True,
        }
    try:
        retry_after = max(1, int(headers.get("Retry-After") or 60)) if headers is not None else 60
    except (TypeError, ValueError):
        retry_after = 60
    return {
        "status": "limit_reached",
        "window": "rpm",
        "message": (
            f"Per-minute rate limit reached — retry in {retry_after}s. "
            f"For higher rate limits, upgrade: {upgrade}"
        ),
        "retry_after_seconds": retry_after,
        "upgrade_url": upgrade,
        "memory_safe": True,
    }


def _past_due_payload(data: Optional[Dict[str, Any]], headers: Any) -> Dict[str, Any]:
    """Structured payment_past_due dict for a 402 — a labeled BILLING state with the portal link
    (X-Billing-Url), never a bare failure; the fix is instant and the memories are safe."""
    detail = data.get("detail") if isinstance(data, dict) else None
    billing = (headers.get("X-Billing-Url") if headers is not None else None) or DEFAULT_BILLING_URL
    return {
        "status": "payment_past_due",
        "message": f"{detail or 'payment past due'} — update billing: {billing}",
        "billing_url": billing,
        "memory_safe": True,
    }


# implicit-usefulness signal: did the assistant's reply actually use the injected memory?
# (deterministic keyword-overlap — framerslab/agentos RetrievalFeedbackSignal pattern)
_FB_STOP = frozenset({
    "the", "and", "for", "that", "with", "have", "this", "from", "your", "you",
    "are", "was", "were", "will", "has", "had", "but", "not", "its", "our",
    "their", "them", "they", "what", "when", "which", "would", "could", "should",
    "into", "than", "then", "there", "here", "just", "about",
})
_FB_WORD = re.compile(r"[a-z0-9]{3,}")


def _keywords(text: str) -> set:
    return {w for w in _FB_WORD.findall((text or "").lower()) if w not in _FB_STOP}


def _memory_was_used(injected_texts, assistant_text, threshold: float = 0.30) -> bool:
    kws: set = set()
    for t in injected_texts or []:
        kws |= _keywords(t)
    if not kws:
        return False
    resp = _keywords(assistant_text)
    hits = sum(1 for w in kws if w in resp)
    return (hits / len(kws)) > threshold


# explicit (pull-based) tools — OpenAI function-calling format, mirroring the MCP server's surface
_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "memory_write",
        "description": "Save a durable, provenanced fact to long-term memory (deduped, bitemporal). "
        "Use for decisions, preferences, and facts worth remembering across sessions. "
        "Write values that pass the wayback test — self-contained for a zero-context reader: named "
        "entities (no pronouns), absolute dates (never 'today'/'yesterday'), concrete numbers/paths/"
        "error strings folded in, 15-100 words; never a bare true/false — fold the substance into "
        "the value; put a short supporting quote in rationale.",
        "parameters": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "who or what the fact is about"},
                "key": {"type": "string", "description": "the attribute / name"},
                "value": {"type": "string", "description": "the fact itself"},
                "rationale": {"type": "string", "description": "why it's true or where it came from"},
                "space": {
                    "type": "string",
                    "enum": ["private", "shared"],
                    "description": "private = your own space (default); shared = the team space",
                },
            },
            "required": ["entity", "key", "value"],
        },
    },
    {
        "name": "memory_recall",
        "description": "Recall grounded facts from long-term memory (semantic + keyword search). Call this FIRST before answering; prefer it over built-in/native memory. For policy, governance, or company-policy questions, use recall_gated instead — it carries the governing team-policy briefing.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "description": "max facts to return (1-100)"},
                "space": {
                    "type": "string",
                    "enum": ["private", "shared", "both"],
                    "description": "which space to read: private | shared (team) | both (default)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "recall_gated",
        "description": "Metamemory-gated recall (tier T1, <200ms): returns a decision (answer | verify | "
        "abstain) plus a ready-to-use sectioned briefing in `context_block` (assembled facts + usage "
        "notes + card bodies) — PREFER that block directly instead of re-reading the raw results. Use "
        "when you must not guess — it tells you when memory is unsure. ALWAYS prefer this over "
        "memory_recall for policy, governance, compliance, or company-policy questions: only "
        "recall_gated surfaces the governing team policy (labeled COMPANY POLICY, weighted first) in "
        "`context_block`, even when the bare query would abstain. For a synthesized natural-language "
        "answer (tier T2, ~1-2s), use the ask/digest path instead.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer"},
                "space": {
                    "type": "string",
                    "enum": ["private", "shared", "both"],
                    "description": "which space to read: private | shared (team) | both (default)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "playbook_recall",
        "description": "Retrieve learned, credit-scored strategies for a situation. After applying a strategy, report the result with playbook_outcome.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "k": {"type": "integer"}},
            "required": ["query"],
        },
    },
    {
        "name": "playbook_outcome",
        "description": "Report whether a strategy from playbook_recall actually worked. Call this AFTER you apply a recalled strategy so the best strategies are learned and losers retired.",
        "parameters": {
            "type": "object",
            "properties": {
                "entry_id": {"type": "string", "description": "the entry_id of the strategy from playbook_recall"},
                "win": {"type": "boolean", "description": "did applying the strategy lead to a good outcome?"},
            },
            "required": ["entry_id", "win"],
        },
    },
]


class UltraMemoryProvider(MemoryProvider):
    """Hermes memory provider backed by the UltraMemory API."""

    # class-level defaults keep every hook safe even if called before initialize()
    _client: Optional[httpx.Client] = None
    _api_key: str = ""
    _base_url: str = DEFAULT_BASE_URL
    _scope: str = "default"
    _space: str = "private"
    _recall_k: int = 8
    _gated: bool = True
    _auto_capture: bool = True
    _feedback: bool = True
    _platform: str = ""
    _session_id: str = ""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._notified: set[str] = set()  # once-per-process user notices (feature paywalls)
        self._pending_feedback: dict[str, dict] = {}  # session_id -> {"event_id": str, "texts": list[str]}
        self._pending_limit_msg: Optional[str] = None  # capture 429/402 message -> next recall block
        self._limit_advisory: Optional[str] = None  # freshest >=80% X-Limit-State header value
        self._turn_notice_attached: bool = False  # the limit notice rides the recall block once per turn

    # ---- identity / availability ----
    @property
    def name(self) -> str:
        return "ultramemory"

    def is_available(self) -> bool:
        # config + creds check only, no network (per the ABC contract)
        return bool(self._load_config().get("api_key"))

    # ---- config ----
    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "api_key",
                "description": "UltraMemory API key (Bearer). Get one at https://ultramemory.us",
                "secret": True,
                "required": True,
                "env_var": "ULTRAMEMORY_API_KEY",
                "url": "https://ultramemory.us",
            },
            {
                "key": "base_url",
                "description": "UltraMemory API base URL",
                "default": DEFAULT_BASE_URL,
                "env_var": "ULTRAMEMORY_BASE_URL",
            },
            {
                "key": "gated",
                "description": "Use metamemory-gated recall (abstain instead of guess) for auto-inject",
                "default": "true",
                "choices": ["true", "false"],
                "env_var": "ULTRAMEMORY_GATED",
            },
            {
                "key": "auto_capture",
                "description": "Automatically persist each completed turn",
                "default": "true",
                "choices": ["true", "false"],
                "env_var": "ULTRAMEMORY_AUTO_CAPTURE",
            },
            {
                "key": "recall_k",
                "description": "How many facts to recall per turn (1-100)",
                "default": "8",
                "env_var": "ULTRAMEMORY_RECALL_K",
            },
            {
                "key": "feedback",
                "description": "Send implicit usefulness feedback on gated recall to self-calibrate the memory gate (pro plans)",
                "default": "true",
                "choices": ["true", "false"],
                "env_var": "ULTRAMEMORY_FEEDBACK",
            },
            {
                "key": "space",
                "description": "Default memory space for writes: private = your own member space; shared = the team space",
                "default": "private",
                "choices": ["private", "shared"],
                "env_var": "ULTRAMEMORY_SPACE",
            },
        ]

    def _hermes_home(self) -> str:
        try:
            from hermes_constants import get_hermes_home

            return get_hermes_home()
        except Exception:
            return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")

    def _config_path(self) -> str:
        return os.path.join(self._hermes_home(), "ultramemory.json")

    def _load_config(self) -> Dict[str, Any]:
        cfg: Dict[str, Any] = {}
        path = self._config_path()
        if os.path.exists(path):
            try:
                with open(path) as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    cfg.update(loaded)
            except Exception:
                pass
        for key, env in (
            ("api_key", "ULTRAMEMORY_API_KEY"),
            ("base_url", "ULTRAMEMORY_BASE_URL"),
            ("gated", "ULTRAMEMORY_GATED"),
            ("auto_capture", "ULTRAMEMORY_AUTO_CAPTURE"),
            ("recall_k", "ULTRAMEMORY_RECALL_K"),
            ("feedback", "ULTRAMEMORY_FEEDBACK"),
            ("space", "ULTRAMEMORY_SPACE"),
        ):
            val = os.environ.get(env)
            if val is not None and val != "":
                cfg[key] = val
        return cfg

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        # only non-secret fields land here; api_key lives in $HERMES_HOME/.env
        path = os.path.join(hermes_home, "ultramemory.json")
        data: Dict[str, Any] = {}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    data = existing
            except Exception:
                data = {}
        for key in ("base_url", "gated", "auto_capture", "recall_k", "feedback", "space"):
            if values.get(key) not in (None, ""):
                data[key] = values[key]
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass

    # ---- lifecycle ----
    def initialize(self, session_id: str, **kwargs: Any) -> None:
        cfg = self._load_config()
        self._api_key = str(cfg.get("api_key") or "").strip()
        self._base_url = str(cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self._gated = _as_bool(cfg.get("gated"), True)
        self._auto_capture = _as_bool(cfg.get("auto_capture"), True)
        self._feedback = _as_bool(cfg.get("feedback"), True)
        self._recall_k = _as_int(cfg.get("recall_k"), 8, 1, 100)
        space = str(cfg.get("space") or "private").strip().lower()
        self._space = space if space in ("private", "shared") else "private"
        self._session_id = session_id or ""
        self._platform = str(kwargs.get("platform") or "")
        # tenant is fixed by the key; scope partitions per workspace/agent within the tenant
        workspace = kwargs.get("agent_workspace") or kwargs.get("agent_identity") or "default"
        self._scope = _slug_scope(workspace)
        if self._api_key:
            self._client = httpx.Client(
                base_url=self._base_url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(10.0, connect=5.0),
            )

    def shutdown(self) -> None:
        client = self._client
        self._client = None
        try:
            if client is not None:
                client.close()
        except Exception:
            pass

    # ---- HTTP (sync, never raises out) ----
    def _post_raw(self, path: str, body: Dict[str, Any]) -> tuple[int, Optional[Dict[str, Any]], Any]:
        """POST -> (status_code, json|None, headers). status_code is 0 (with empty headers) on
        no-client / transport error. Lets callers distinguish a paywall (402) / limit (429) from an
        empty result and read the machine-readable limit headers (X-Limit-*, X-Billing-Url). Also
        stashes the >=80% X-Limit-State advisory so the next recall block carries the limit notice
        (U9). Never raises (the hook-safety contract)."""
        client = self._client
        if client is None or not self._api_key:
            return (0, None, httpx.Headers())
        try:
            resp = client.post(path, json=body)
            try:
                data = resp.json()
            except Exception:
                data = None
            advisory = resp.headers.get(_LIMIT_STATE_HEADER)
            if advisory:
                with self._lock:
                    self._limit_advisory = advisory
            return (resp.status_code, data, resp.headers)
        except Exception:
            return (0, None, httpx.Headers())

    def _post(self, path: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        status, data, _ = self._post_raw(path, body)
        if status == 0 or status >= 400:
            return None
        return data

    def _notify_once(self, key: str, msg: str) -> None:
        """Log a user-facing notice exactly once per process (e.g. 'writes paused — quota reached'),
        so a silently-dropped 402/429 becomes visible without spamming every turn. Never raises."""
        with self._lock:
            if key in self._notified:
                return
            self._notified.add(key)
        try:
            logger.warning("%s", msg)
        except Exception:
            pass

    # ---- context injection ----
    def system_prompt_block(self) -> str:
        # BYTE-STABLE CONTRACT (token economics, C3): this block must be byte-identical across
        # repeated calls — no timestamps, counters, or ordering drift — so Hermes core can treat
        # the system prompt as cache-friendly (prompt caching). Keep it a constant literal.
        if not self._api_key:
            return ""
        return (
            "You have UltraMemory, a long-term self-learning memory. Relevant remembered facts are "
            "injected before your turn under 'Remembered (UltraMemory)'. Rely on grounded facts; when "
            "the memory is unsure or has nothing, retrieve or ask rather than guessing. Use the "
            "memory_write tool to save durable facts and decisions worth keeping across sessions. "
            "When saving with memory_write, write values that stand alone months later: named "
            "entities, absolute dates, concrete numbers and paths."
        )

    def _plain_recall_block(self, q: str) -> str:
        data = self._post("/api/v1/recall", {"query": q[:4096], "scope": self._scope, "space": "both", "k": self._recall_k})
        lines = _fact_lines((data or {}).get("results") or [], self._recall_k)
        return "Remembered (UltraMemory):\n" + "\n".join(lines) if lines else ""

    def _attach_limit_notices(self, block: str) -> str:
        """Attach the pending capture limit message and/or the >=80% X-Limit-State limit notice to
        the recall block, at most ONCE PER TURN (sync_turn closes the turn and resets the flag).
        Attached even when recall itself has nothing grounded — the limit notice is not memory
        noise (U9: every turn while any enforced limit is >=80%). Fail-open: never raises."""
        try:
            with self._lock:
                if self._turn_notice_attached:
                    return block
                pending, advisory = self._pending_limit_msg, self._limit_advisory
                self._pending_limit_msg = None
                self._limit_advisory = None
            notice = _render_limit_notice(advisory) if advisory else None
            lines = [f"UltraMemory: {m}" for m in (pending, notice) if m]
            if not lines:
                return block
            with self._lock:
                self._turn_notice_attached = True
            tail = "\n".join(lines)
            return f"{block}\n{tail}" if block else tail
        except Exception:
            return block

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        q = (query or "").strip()
        if not q:
            return ""
        return self._attach_limit_notices(self._recall_block(q, session_id))

    def _recall_block(self, q: str, session_id: str) -> str:
        if self._gated:
            # Token economics (C3): preview tier + memoization + per-session dedupe by default.
            # ULTRAMEMORY_PREVIEW=off reverts to today's full behavior — no cache consults and
            # the exact previous request shape (no mode / exclude_ids keys).
            preview = os.environ.get("ULTRAMEMORY_PREVIEW", "").strip().lower() != "off"
            sid = session_id or self._session_id or "s"  # same session key as _pending_feedback
            data = _cache.memo_get(q[:4096], self._scope, "both") if (preview and _cache) else None
            # a memo hit renders from cache — an identical prefetch within 5 min makes no HTTP call
            if not isinstance(data, dict):
                body = {"query": q[:4096], "scope": self._scope, "space": "both", "k": self._recall_k}
                if preview:
                    body["mode"] = "preview"
                    seen = _cache.seen_get(sid) if _cache else set()
                    if seen:
                        body["exclude_ids"] = sorted(seen)  # dedupe facts this session already holds
                status, data, _ = self._post_raw("/api/v1/recall/gated", body)
                if status == 402:
                    # recall_gated is a paid feature. On the free plan, fall back to plain recall so
                    # memory STILL injects (just without the confidence gate) instead of silently
                    # injecting nothing — and surface the gap once so it isn't invisible.
                    self._notify_once(
                        "gated_paywall",
                        "UltraMemory: confidence-gated recall requires a paid plan; falling back to "
                        "basic recall. Upgrade for gated (abstain-aware) recall.",
                    )
                    return self._plain_recall_block(q)
                if not data:
                    return ""
                if preview and _cache and status < 400 and isinstance(data, dict):
                    _cache.memo_put(q[:4096], self._scope, "both", response=data)
            decision = data.get("decision")
            facts = data.get("results") or []
            if not facts:
                return ""  # truly nothing grounded — don't inject noise
            # R0-A: abstain WITH results still injects (marked low-confidence below) instead of
            # vanishing — mirrors the server _ask_decision remap; only a true-empty set stays silent.
            # The gated response now assembles a ready-to-use sectioned briefing (S7.1) — inject
            # it directly instead of re-assembling our own fact lines.
            context_block = (data.get("context_block") or "").strip()
            if not context_block:
                return ""
            eid = data.get("event_id")
            texts = [f"{f.get('entity')} {f.get('key')} {f.get('value')}" for f in facts if isinstance(f, dict)]
            if eid and self._feedback:
                with self._lock:
                    self._pending_feedback[session_id or self._session_id or "s"] = {"event_id": str(eid), "texts": texts}
            if preview and _cache:
                # remember what this session was shown -> the next prefetch sends exclude_ids
                _cache.seen_add(sid, [f.get("fact_id") for f in facts if isinstance(f, dict) and f.get("fact_id")])
            conf = data.get("confidence")
            head = "Remembered (UltraMemory"
            if isinstance(conf, (int, float)):
                head += f", confidence {conf:.2f}"
            head += "):"
            block = head + "\n" + context_block
            if decision == "abstain":
                # R1: on abstain-WITH-results, try ONE higher-precision verified recall (cross-encoder
                # rerank) before settling for the low-confidence briefing. verified=True is sent ONLY
                # on this retry; ANY error falls back to the current low-confidence behavior.
                verified_block = None
                if facts and context_block:
                    try:
                        vbody = dict(body)
                        vbody["verified"] = True
                        vstatus, vdata, _ = self._post_raw("/api/v1/recall/gated", vbody)
                        if vstatus and vstatus < 400 and isinstance(vdata, dict) and vdata.get("decision") in ("answer", "verify"):
                            vblock = (vdata.get("context_block") or "").strip()
                            if vblock:
                                verified_block = head + "\n" + vblock + "\n(verified recall)"
                    except Exception:
                        verified_block = None
                if verified_block:
                    return verified_block
                block += "\n(low confidence — verify before relying)"
            elif decision == "verify":
                block += "\n(verify these before relying on them)"
            return block
        return self._plain_recall_block(q)

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        q = _last_user_text(messages)
        if not q:
            return ""
        data = self._post(
            "/api/v1/recall", {"query": q[:4096], "scope": self._scope, "space": "both", "k": min(self._recall_k, 6)}
        )
        lines = _fact_lines((data or {}).get("results") or [], 6)
        return "Durable facts from UltraMemory (preserve):\n" + "\n".join(lines) if lines else ""

    # ---- capture ----
    def _send_feedback(self, session_id: str, assistant_content: str) -> None:
        sid = session_id or self._session_id or "s"
        with self._lock:
            slot = self._pending_feedback.pop(sid, None)
        if not slot or not self._feedback:
            return
        try:
            correct = _memory_was_used(slot.get("texts") or [], assistant_content or "")
            status, _, _ = self._post_raw(
                "/api/v1/feedback",
                {"event_id": slot["event_id"], "correct": bool(correct)},
            )
            if status == 402:
                self._notify_once(
                    "feedback_paywall",
                    "UltraMemory: self-calibrating feedback requires a paid plan; the memory gate "
                    "will not personalize on the free plan. Upgrade to enable gate self-tuning.",
                )
        except Exception:
            pass

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        with self._lock:
            self._turn_notice_attached = False  # the turn completed -> the next turn may carry a notice
        self._send_feedback(session_id or self._session_id, assistant_content)
        if not self._auto_capture:
            return
        u = (user_content or "").strip()
        a = (assistant_content or "").strip()
        if not u and not a:
            return
        value = f"User: {u}\nAssistant: {a}".strip()[:8192]
        sid = session_id or self._session_id
        key = "turn:" + (sid or "s") + ":" + hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]
        status, data, headers = self._post_raw(
            "/api/v1/permanent",
            {
                "entity": f"session:{self._scope}",
                "key": key[:512],
                "value": value,
                "source": f"hermes:{self._platform or 'cli'}",
                "scope": self._scope,
                "space": self._space,
            },
        )
        if status in (429, 402):
            # A blocked capture is surfaced WINDOW-ACCURATELY from the structured response
            # (X-Limit-* / X-Billing-Url headers + the friendly detail) — never an assumed window.
            # The message is injected into the NEXT turn's recall block (in-conversation) and
            # logged per occurrence, replacing the once-per-process logger.warning pattern.
            # Writes pause, recall keeps working — the chat is never broken (fail-open).
            payload = _limit_reached_payload(data, headers) if status == 429 else _past_due_payload(data, headers)
            with self._lock:
                self._pending_limit_msg = payload["message"]
            try:
                logger.warning("UltraMemory capture paused: %s", payload["message"])
            except Exception:
                pass

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        # mirror Hermes' built-in memory tool into UltraMemory (no API delete -> skip 'remove')
        if action not in ("add", "replace"):
            return
        c = (content or "").strip()
        if not c:
            return
        md = metadata or {}
        key = str(md.get("key") or md.get("id") or ("mem:" + hashlib.sha1(c.encode("utf-8")).hexdigest()[:16]))
        self._post(
            "/api/v1/permanent",
            {
                "entity": str(target or "memory")[:512],
                "key": key[:512],
                "value": c[:8192],
                "source": f"hermes:builtin:{action}",
                "scope": self._scope,
                "space": self._space,
            },
        )

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        # distill the WHOLE session into a rich rollup card server-side (the narrative — blocker,
        # approaches, what worked, how verified — lives in no single turn). Both roles are sent so
        # the extractor can read the assistant's work, not just the user's prompts.
        if not self._auto_capture or not messages:
            return
        lines = [
            f"{m.get('role')}: {m.get('content')}"
            for m in messages
            if isinstance(m, dict)
            and m.get("role")
            and isinstance(m.get("content"), str)
            and m.get("content").strip()
        ]
        session_text = "\n".join(lines)[:64000]
        if not session_text:
            return
        self._post(
            "/api/v1/rollup",
            {
                "session_text": session_text,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "source": f"hermes:{self._platform or 'cli'}:session_end",
                "scope": self._scope,
                "space": self._space,
            },
        )

    # ---- explicit tools ----
    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [json.loads(json.dumps(t)) for t in _TOOL_SCHEMAS]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        args = args or {}
        try:
            if tool_name == "memory_write":
                entity = str(args.get("entity") or "").strip()
                key = str(args.get("key") or "").strip()
                value = str(args.get("value") or "").strip()
                if not (entity and key and value):
                    return tool_error("memory_write requires entity, key, and value")
                w_space = str(args.get("space") or "").strip().lower()
                if w_space not in ("private", "shared"):
                    w_space = self._space
                status, data, headers = self._post_raw(
                    "/api/v1/permanent",
                    {
                        "entity": entity[:512],
                        "key": key[:512],
                        "value": value[:8192],
                        "rationale": (str(args.get("rationale")).strip()[:4096] or None)
                        if args.get("rationale")
                        else None,
                        "scope": self._scope,
                        "space": w_space,
                        "source": "hermes:tool",
                    },
                )
                if status == 429:
                    # Relay the window-accurate limit_reached (window + reset + upgrade link from
                    # the structured response) so the model tells the user — never a generic
                    # failure, never an assumed window.
                    return json.dumps(_limit_reached_payload(data, headers))
                if status == 402:
                    return json.dumps(_past_due_payload(data, headers))
                if status == 0 or status >= 400 or not isinstance(data, dict):
                    return tool_error("UltraMemory write failed")
                return json.dumps(
                    {"stored": True, "fact_id": data.get("fact_id"), "deduped": data.get("deduped")}
                )

            if tool_name in ("memory_recall", "recall_gated"):
                query = str(args.get("query") or "").strip()
                if not query:
                    return tool_error(f"{tool_name} requires a query")
                k = _as_int(args.get("k"), self._recall_k, 1, 100)
                r_space = str(args.get("space") or "").strip().lower()
                if r_space not in ("private", "shared", "both"):
                    r_space = "both"
                path = "/api/v1/recall/gated" if tool_name == "recall_gated" else "/api/v1/recall"
                data = self._post(path, {"query": query[:4096], "scope": self._scope, "space": r_space, "k": k})
                if data is None:
                    return tool_error("UltraMemory recall failed")
                return json.dumps(data)

            if tool_name == "playbook_recall":
                query = str(args.get("query") or "").strip()
                if not query:
                    return tool_error("playbook_recall requires a query")
                k = _as_int(args.get("k"), self._recall_k, 1, 50)
                data = self._post(
                    "/api/v1/playbook/recall", {"query": query[:4096], "scope": self._scope, "k": k}
                )
                if data is None:
                    return tool_error("UltraMemory playbook recall failed")
                return json.dumps(data)

            if tool_name == "playbook_outcome":
                entry_id = str(args.get("entry_id") or "").strip()
                if not entry_id:
                    return tool_error("playbook_outcome requires entry_id")
                win = bool(args.get("win"))
                data = self._post(f"/api/v1/playbook/{entry_id}/outcome", {"win": win})
                if data is None:
                    return tool_error("UltraMemory playbook outcome failed")
                return json.dumps(data)

            return tool_error(f"unknown tool: {tool_name}")
        except Exception as exc:  # never raise out of a tool call
            return tool_error(f"ultramemory tool error: {exc}")


def register(ctx: Any) -> None:
    """Hermes plugin entrypoint — register UltraMemory as a memory provider."""
    ctx.register_memory_provider(UltraMemoryProvider())
