"""UltraMemory client-side cache — the ejected file (`~/.ultramemory/cache.json`).

Shared by the Claude Code recall-first hook and the Hermes provider to cut recall token
spend: (a) memoize full recall responses for an identical query (TTL 5 min → repeat turns
make zero HTTP calls), and (b) track per-session "seen" fact_id sets (24 h) so follow-up
requests can send `exclude_ids` and dedupe the briefing.

Contract (checklist C1, token-economics 2026-07-05):
  - Storage: ``~/.ultramemory/cache.json`` — dir created 0700, file 0600, every write is
    atomic tmp+rename, every read-modify-write runs under an exclusive ``fcntl.flock``,
    and the JSON document carries a top-level ``{"version": 1}`` field.
  - Bounds: LRU — at most 500 entries total (memo + seen) and ~1MB serialized; the
    least-recently-used entries are evicted first.
  - Kill switch: ``ULTRAMEMORY_CACHE=off`` (checked per call) makes every function a
    no-op that returns a miss (``None`` / empty set) and touches no files.
  - Resilience: a corrupt or unreadable cache file silently starts fresh; every public
    call fails soft to a miss — this module must NEVER crash the host tool.

Importable standalone: stdlib only, no package-relative imports.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time

try:  # fcntl is POSIX-only; degrade to lock-free best-effort elsewhere (never crash the host).
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

__all__ = ["memo_get", "memo_put", "seen_get", "seen_add"]

VERSION = 1
MEMO_TTL_SECONDS = 300        # 5 min — identical query inside this window is a memo hit
SEEN_TTL_SECONDS = 24 * 3600  # 24 h — a session's seen fact_id set expires after a day
MAX_ENTRIES = 500             # LRU cap on total entries (memo keys + seen sessions)
MAX_BYTES = 1_000_000         # ~1MB cap on the serialized cache file


# --------------------------------------------------------------------------- plumbing

def _disabled() -> bool:
    """Kill switch: ULTRAMEMORY_CACHE=off disables everything (re-read on every call)."""
    return os.environ.get("ULTRAMEMORY_CACHE", "").strip().lower() == "off"


def _cache_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".ultramemory")


def _cache_path() -> str:
    return os.path.join(_cache_dir(), "cache.json")


def _fresh() -> dict:
    return {"version": VERSION, "memo": {}, "seen": {}}


def _norm(value) -> str:
    """Normalize a key component: str, strip, lowercase, collapse inner whitespace."""
    return " ".join(str(value or "").strip().lower().split())


def _memo_key(query, scope, space) -> str:
    """sha256 of the normalized (query, scope, space) triple."""
    material = "\x1f".join((_norm(query), _norm(scope), _norm(space)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _open_locked(path: str) -> int:
    """Open cache.json (creating it 0600) and take an exclusive flock.

    Robust to the atomic tmp+rename of a concurrent writer replacing the inode between
    our open() and flock(): re-check that the locked fd still IS the file at `path`,
    retrying a few times before settling for a best-effort lock.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)  # O_CREAT mode is umask-filtered; enforce 0600 explicitly
    except OSError:  # pragma: no cover - exotic filesystems
        pass
    if fcntl is None:  # pragma: no cover - non-POSIX platforms
        return fd
    for _ in range(5):
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            if os.fstat(fd).st_ino == os.stat(path).st_ino:
                return fd  # we hold the lock on the live inode
        except OSError:
            pass  # file vanished mid-race; reopen and retry
        os.close(fd)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)  # give up on the race guard; still serialize writers
    return fd


def _load(fd: int) -> dict:
    """Parse the cache document; corrupt/unreadable/wrong-shape → silently start fresh."""
    try:
        with os.fdopen(os.dup(fd), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if (
            not isinstance(data, dict)
            or data.get("version") != VERSION
            or not isinstance(data.get("memo"), dict)
            or not isinstance(data.get("seen"), dict)
        ):
            return _fresh()
        return data
    except Exception:
        return _fresh()


def _prune_expired(data: dict, now: float) -> bool:
    """Drop expired/malformed entries in place; return True when anything was removed."""
    dirty = False
    for key in list(data["memo"].keys()):
        entry = data["memo"].get(key)
        if not isinstance(entry, dict) or now - float(entry.get("ts", 0)) > MEMO_TTL_SECONDS:
            del data["memo"][key]
            dirty = True
    for sid in list(data["seen"].keys()):
        entry = data["seen"].get(sid)
        if not isinstance(entry, dict) or now - float(entry.get("ts", 0)) > SEEN_TTL_SECONDS:
            del data["seen"][sid]
            dirty = True
    return dirty


def _evict_lru(data: dict) -> None:
    """Remove the single least-recently-used entry across memo + seen."""
    entries = [("memo", k, float(v.get("at", v.get("ts", 0)))) for k, v in data["memo"].items()]
    entries += [("seen", k, float(v.get("at", v.get("ts", 0)))) for k, v in data["seen"].items()]
    if not entries:
        return
    kind, key, _ = min(entries, key=lambda item: item[2])
    del data[kind][key]


def _bounded_blob(data: dict) -> str:
    """Serialize, enforcing the LRU caps: ≤MAX_ENTRIES entries and ≤~MAX_BYTES bytes."""
    while len(data["memo"]) + len(data["seen"]) > MAX_ENTRIES:
        _evict_lru(data)
    blob = json.dumps(data, separators=(",", ":"))
    while len(blob.encode("utf-8")) > MAX_BYTES and (data["memo"] or data["seen"]):
        _evict_lru(data)
        blob = json.dumps(data, separators=(",", ":"))
    return blob


def _atomic_write(directory: str, path: str, blob: str) -> None:
    """Write via 0600 tempfile in the same dir + os.replace (atomic rename)."""
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="cache.json.", suffix=".tmp", dir=directory)
    try:
        os.fchmod(tmp_fd, 0o600)  # mkstemp already creates 0600; enforce anyway
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(blob)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _with_cache(op, default):
    """Run ``op(data, now) -> (result, dirty)`` under the file lock; persist when dirty.

    Any failure anywhere (I/O, permissions, JSON, locking) degrades to ``default`` —
    a miss — so the host tool never sees an exception from this module.
    """
    now = time.time()
    try:
        directory = _cache_dir()
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            os.chmod(directory, 0o700)  # makedirs mode is umask-filtered; enforce 0700
        except OSError:
            pass
        path = _cache_path()
        fd = _open_locked(path)
        try:
            data = _load(fd)
            pruned = _prune_expired(data, now)
            result, dirty = op(data, now)
            if dirty or pruned:
                _atomic_write(directory, path, _bounded_blob(data))
            return result
        finally:
            os.close(fd)  # releases the flock
    except Exception:
        return default


# ------------------------------------------------------------------------- public API

def memo_get(query, scope=None, space=None):
    """Return the memoized full recall response for (query, scope, space), else None.

    Key = sha256 of the normalized triple; TTL 300 s. A hit refreshes LRU recency.
    Disabled (ULTRAMEMORY_CACHE=off) or any error → None (a miss).
    """
    if _disabled():
        return None
    key = _memo_key(query, scope, space)

    def op(data, now):
        entry = data["memo"].get(key)
        if not isinstance(entry, dict) or now - float(entry.get("ts", 0)) > MEMO_TTL_SECONDS:
            return None, False
        entry["at"] = now  # LRU touch
        return entry.get("response"), True

    return _with_cache(op, None)


def memo_put(query, scope=None, space=None, response=None):
    """Store the FULL recall response for (query, scope, space); TTL 300 s.

    Disabled (ULTRAMEMORY_CACHE=off) or any error → silent no-op.
    """
    if _disabled():
        return None
    key = _memo_key(query, scope, space)

    def op(data, now):
        data["memo"][key] = {"ts": now, "at": now, "response": response}
        return None, True

    return _with_cache(op, None)


def seen_get(session_id):
    """Return the set of fact_ids already delivered to this session (24 h window).

    Disabled (ULTRAMEMORY_CACHE=off) or any error → empty set (a miss).
    """
    if _disabled():
        return set()
    sid = str(session_id or "")

    def op(data, now):
        entry = data["seen"].get(sid)
        if not isinstance(entry, dict):
            return set(), False
        entry["at"] = now  # LRU touch
        ids = entry.get("fact_ids")
        return (set(ids) if isinstance(ids, list) else set()), True

    return _with_cache(op, set())


def seen_add(session_id, fact_ids):
    """Union fact_ids into the session's seen-set; refreshes its 24 h expiry.

    Disabled (ULTRAMEMORY_CACHE=off), empty fact_ids, or any error → silent no-op.
    """
    if _disabled():
        return None
    new_ids = [str(f) for f in (fact_ids or []) if f]
    if not new_ids:
        return None
    sid = str(session_id or "")

    def op(data, now):
        entry = data["seen"].get(sid)
        current = set(entry.get("fact_ids") or []) if isinstance(entry, dict) else set()
        current.update(new_ids)
        data["seen"][sid] = {"ts": now, "at": now, "fact_ids": sorted(current)}
        return None, True

    return _with_cache(op, None)
