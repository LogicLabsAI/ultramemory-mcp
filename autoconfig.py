"""UltraMemory host-platform auto-configuration engine (``ultramemory configure``).

Consent-first by design: marketplace policy makes silent settings modification a listing
blocker (Anthropic Software Directory Policy 2026-04-15 §1.B — see
research/autoconfig-grounding-2026-07-21/marketplace-policy.json), so this engine is a
SEPARATE, explicit command that the installer never auto-runs. Guarantees:

  * NOTHING is written without an explicit per-item "yes" — interactive prompts default
    to No; non-interactive runs require ``--yes`` (optionally scoped with ``--items``);
    a non-interactive run without ``--yes`` exits 2 having written nothing;
  * ``--dry-run`` prints the full plan and exits 0 with zero writes;
  * every config file is backed up to ``<file>.um-backup-<ISO8601>`` before its FIRST
    write of a run (once per file per run);
  * every applied change is recorded as a ``"setting"`` row in the SAME manifest the kit
    installer writes (``~/.ultramemory/install-manifest.json`` — install.sh:21), so
    ``ultramemory configure --restore`` and ``uninstall.sh`` can surgically revert
    exactly the keys we changed to their recorded prior values — never a blind backup
    copy (the user may have changed other keys since).

Per-platform adapters register themselves into ``ADAPTERS`` via ``@register_adapter``.
This module ships the ENGINE only — the registry starts empty; the adapters land with
the platform-adapter checklist items (Clusters B/C), each grounded in
research/autoconfig-grounding-2026-07-21/platform-<name>.json.

Safe-merge helpers provided for adapters:
  * strict JSON  — parser round-trip, temp file + atomic rename (``json_set`` et al.);
  * JSONC        — offset-preserving surgical edit that keeps comments (``jsonc_set_file``);
  * TOML         — tomlkit (lazy import), with the root-keys-before-tables guard: a new
    root key is inserted BEFORE the first ``[table]`` so it can never silently become a
    key of that table (``toml_set``);
  * YAML-via-CLI — shell out to the platform's own ``config set`` (``yaml_cli_set``),
    e.g. Hermes/OpenClaw, where the platform CLI is the sanctioned merge path.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Type

__all__ = [
    "ABSENT",
    "ADAPTERS",
    "EngineError",
    "PLATFORM_NAMES",
    "PlatformAdapter",
    "ProposedChange",
    "apply_change",
    "apply_changes",
    "append_setting_rows",
    "backup_file",
    "detect_format",
    "json_delete",
    "json_read",
    "json_set",
    "jsonc_delete_file",
    "jsonc_read",
    "jsonc_set_file",
    "manifest_path",
    "read_setting_rows",
    "register_adapter",
    "restore_rows",
    "run_cli",
    "run_configure",
    "run_restore",
    "setting_row",
    "toml_delete",
    "toml_set",
    "yaml_cli_set",
]


class EngineError(RuntimeError):
    """A config file could not be safely read, merged, or written. Nothing was clobbered."""


class _Absent:
    """Sentinel: the key did not exist before (distinct from an explicit ``null`` value)."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - repr cosmetics
        return "<absent>"


ABSENT = _Absent()

#: The nine platform names the ``configure`` subcommand accepts (besides all/auto).
PLATFORM_NAMES = [
    "claude-code",
    "codex",
    "gemini",
    "cursor",
    "cline",
    "vscode",
    "openclaw",
    "hermes",
    "windsurf",
]


# --------------------------------------------------------------------------- model ----
@dataclasses.dataclass
class ProposedChange:
    """One proposed, individually-consentable configuration change.

    ``kind`` is ``"persist"`` (a globally-persistable setting) or ``"onboard-hook"``
    (wiring the session-start onboarding hook — session-only settings can't persist,
    so the hook proposes them at session start instead).
    """

    platform: str
    file: str
    key_path: str
    prior_value: Any
    new_value: Any
    kind: str  # "persist" | "onboard-hook"
    description: str


class PlatformAdapter:
    """Base class for per-platform adapters (filled in by the Cluster B/C items).

    Contract:
      * ``detect()``       -> True when the platform appears installed on this machine;
      * ``plan()``         -> list[ProposedChange] (read-only — must not write);
      * ``apply(changes)`` -> manifest ``"setting"`` rows for what was actually written;
      * ``restore(rows)``  -> revert rows to their recorded priors; returns the rows it
        could NOT restore (kept in the manifest with a warning).

    ``apply``/``restore`` default to the engine's format-dispatched generics; adapters
    for CLI-merged platforms (hermes, openclaw) override them with ``yaml_cli_set``.
    """

    name: str = ""

    def detect(self) -> bool:
        raise NotImplementedError

    def plan(
        self,
        model: Optional[str] = None,
        items: Optional[List[str]] = None,
    ) -> List[ProposedChange]:
        raise NotImplementedError

    def apply(
        self,
        changes: List[ProposedChange],
        backed_up: Optional[Set[str]] = None,
    ) -> List[dict]:
        return apply_changes(changes, backed_up=backed_up)

    def restore(self, rows: List[dict]) -> List[dict]:
        return restore_rows(rows)


#: name -> adapter class. Starts EMPTY: the per-platform items (B1-B5, C1-C4) register
#: their adapters here with @register_adapter("<name>").
ADAPTERS: Dict[str, Type[PlatformAdapter]] = {}


def register_adapter(name: str) -> Callable[[Type[PlatformAdapter]], Type[PlatformAdapter]]:
    """Class decorator: register a PlatformAdapter subclass under ``name``."""

    def deco(cls: Type[PlatformAdapter]) -> Type[PlatformAdapter]:
        cls.name = name
        ADAPTERS[name] = cls
        return cls

    return deco


# ------------------------------------------------------------------------- backups ----
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def backup_file(path: str, backed_up: Optional[Set[str]] = None) -> Optional[str]:
    """Copy ``path`` to ``<path>.um-backup-<ISO8601>`` before the FIRST write of a run.

    ``backed_up`` is the run-scoped set of already-backed-up paths — pass the same set
    for every write of one run so each file is backed up exactly once per run. Returns
    the backup path, or None when nothing was copied (already backed up / file absent).
    """
    path = os.path.expanduser(path)
    if backed_up is not None:
        if path in backed_up:
            return None
        backed_up.add(path)
    if not os.path.exists(path):
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")  # ISO 8601 basic (no colons)
    bak = "%s.um-backup-%s" % (path, stamp)
    n = 2
    while os.path.exists(bak):
        bak = "%s.um-backup-%s-%d" % (path, stamp, n)
        n += 1
    shutil.copy2(path, bak)
    return bak


def _atomic_write_text(path: str, text: str) -> None:
    """Write ``text`` to ``path`` via a same-dir temp file + atomic rename, preserving
    the original file mode when the file already existed."""
    mode = None
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        pass
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = path + ".um-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
    os.replace(tmp, path)


def _merge_value(prior: Any, new: Any) -> Any:
    """Merge semantics for a single key: lists append-dedupe (existing entries kept,
    new entries appended in order, exact duplicates skipped); everything else replaces."""
    if isinstance(prior, list) and isinstance(new, list):
        merged = list(prior)
        for item in new:
            if item not in merged:
                merged.append(item)
        return merged
    return new


# --------------------------------------------------------------------- strict JSON ----
def json_read(path: str) -> dict:
    """Read a strict-JSON object file. Missing file -> {}. Corrupt file -> EngineError
    (never treat an unparseable customer file as empty — that path clobbers)."""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        raise EngineError("%s: not valid JSON (%s) — refusing to modify" % (path, e)) from e
    if not isinstance(data, dict):
        raise EngineError("%s: top-level JSON value is not an object — refusing to modify" % path)
    return data


def _walk_parent(data: dict, dotted: str, create: bool) -> Tuple[Optional[dict], str]:
    """Return (parent_object, leaf_key) for a dotted path; parent is None when an
    intermediate segment is missing (create=False) or not an object."""
    parts = dotted.split(".")
    node: Any = data
    for seg in parts[:-1]:
        if not isinstance(node, dict):
            return None, parts[-1]
        if seg not in node:
            if not create:
                return None, parts[-1]
            node[seg] = {}
        node = node[seg]
    if not isinstance(node, dict):
        return None, parts[-1]
    return node, parts[-1]


def json_set(
    path: str,
    dotted: str,
    value: Any,
    backed_up: Optional[Set[str]] = None,
    merge_lists: bool = True,
) -> Tuple[Any, Any]:
    """Set ``dotted`` in a strict-JSON file (parser round-trip, temp + atomic rename).

    Unknown keys are preserved verbatim (only the named key is touched); list values
    append-dedupe into an existing list (``merge_lists=False`` replaces instead — the
    restore path uses it to put a recorded prior list back EXACTLY). Returns
    ``(prior, final)`` where prior is ABSENT when the key did not exist."""
    path = os.path.expanduser(path)
    data = json_read(path)
    parent, leaf = _walk_parent(data, dotted, create=True)
    if parent is None:
        raise EngineError("%s: %s is not an object path" % (path, dotted))
    prior: Any = parent[leaf] if leaf in parent else ABSENT
    final = (
        _merge_value(prior, value)
        if merge_lists and isinstance(prior, list) and isinstance(value, list)
        else value
    )
    parent[leaf] = final
    backup_file(path, backed_up)
    _atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return prior, final


def json_delete(path: str, dotted: str, backed_up: Optional[Set[str]] = None) -> Any:
    """Delete ``dotted`` from a strict-JSON file. Returns the removed value (or ABSENT)."""
    path = os.path.expanduser(path)
    data = json_read(path)
    parent, leaf = _walk_parent(data, dotted, create=False)
    if parent is None or leaf not in parent:
        return ABSENT
    removed = parent.pop(leaf)
    backup_file(path, backed_up)
    _atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return removed


# --------------------------------------------------------------------------- JSONC ----
def _jsonc_to_strict(text: str) -> str:
    """Return a SAME-LENGTH copy of ``text`` with // and /* */ comments and trailing
    commas blanked to spaces (newlines kept), so json.loads works AND every remaining
    character keeps its original offset — the key property the surgical editor needs."""
    out = list(text)
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            out[i] = " "
            out[i + 1] = " "
            i += 2
            while i < n - 1 and not (text[i] == "*" and text[i + 1] == "/"):
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n - 1:
                out[i] = " "
                out[i + 1] = " "
                i += 2
            continue
        i += 1
    s = "".join(out)
    # second pass: blank trailing commas (string-aware — never touch commas in strings)
    res = list(s)
    i, in_str = 0, False
    while i < len(s):
        c = s[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
        elif c == ",":
            j = i + 1
            while j < len(s) and s[j] in " \t\r\n":
                j += 1
            if j < len(s) and s[j] in "}]":
                res[i] = " "
        i += 1
    return "".join(res)


def _jsonc_loads(text: str) -> Any:
    try:
        return json.loads(_jsonc_to_strict(text))
    except ValueError as e:
        raise EngineError("not valid JSONC: %s" % e) from e


def jsonc_read(path: str) -> dict:
    """Read a JSONC (comments + trailing commas tolerated) object file. Missing -> {}."""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if not text.strip():
        return {}
    data = _jsonc_loads(text)
    if not isinstance(data, dict):
        raise EngineError("%s: top-level JSONC value is not an object" % path)
    return data


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _scan_value(s: str, i: int) -> int:
    """End index (exclusive) of the JSON value starting at ``i`` in strict text."""
    c = s[i]
    if c in "{[":
        depth, in_str, j = 0, False, i
        while j < len(s):
            ch = s[j]
            if in_str:
                if ch == "\\":
                    j += 2
                    continue
                if ch == '"':
                    in_str = False
                j += 1
                continue
            if ch == '"':
                in_str = True
            elif ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        raise EngineError("unterminated container in JSONC")
    if c == '"':
        j = i + 1
        while j < len(s):
            if s[j] == "\\":
                j += 2
                continue
            if s[j] == '"':
                return j + 1
            j += 1
        raise EngineError("unterminated string in JSONC")
    j = i
    while j < len(s) and s[j] not in ",}] \t\r\n":
        j += 1
    return j


def _find_member(s: str, obj_start: int, key: str) -> Optional[Tuple[int, int, int]]:
    """In strict text, find member ``key`` of the object whose ``{`` is at obj_start.
    Returns (key_start, value_start, value_end) or None."""
    i = obj_start + 1
    while True:
        i = _skip_ws(s, i)
        if i >= len(s) or s[i] == "}":
            return None
        if s[i] != '"':
            raise EngineError("malformed JSONC object (expected a string key)")
        kend = _scan_value(s, i)
        k = json.loads(s[i:kend])
        j = _skip_ws(s, kend)
        if j >= len(s) or s[j] != ":":
            raise EngineError("malformed JSONC object (expected ':')")
        vstart = _skip_ws(s, j + 1)
        vend = _scan_value(s, vstart)
        if k == key:
            return i, vstart, vend
        i = _skip_ws(s, vend)
        if i < len(s) and s[i] == ",":
            i += 1


def _object_indent(text: str, brace: int) -> str:
    """Member indentation for the object opening at ``brace``: the brace line's indent + 2."""
    line_start = text.rfind("\n", 0, brace) + 1
    indent = ""
    for ch in text[line_start:brace]:
        if ch in " \t":
            indent += ch
        else:
            break
    return indent + "  "


def _jsonc_edit(text: str, dotted: str, value: Any, parts: Optional[List[str]] = None) -> str:
    """Pure-text surgical set of ``dotted`` to ``value`` in JSONC ``text``: replaces
    only the value span of an existing key, or inserts a new member right after the
    parent's ``{`` — every comment elsewhere survives byte-for-byte. (A comment INSIDE
    a replaced value span is the one thing that cannot survive.) ``parts`` overrides
    the default dot-split — VS Code settings.json keys are FLAT strings that contain
    dots (e.g. "chat.defaultModel"), so its adapter passes ``parts=[key]``."""
    strict = _jsonc_to_strict(text)
    root = _skip_ws(strict, 0)
    if root >= len(strict) or strict[root] != "{":
        raise EngineError("JSONC root is not an object")
    parts = parts if parts is not None else dotted.split(".")
    obj = root
    for depth, seg in enumerate(parts):
        member = _find_member(strict, obj, seg)
        last = depth == len(parts) - 1
        if member is None:
            # build the remaining chain as one nested value and insert it here
            node_val: Any = value
            for missing in reversed(parts[depth + 1 :]):
                node_val = {missing: node_val}
            has_members = strict[_skip_ws(strict, obj + 1)] != "}"
            pair = json.dumps(seg) + ": " + json.dumps(node_val, ensure_ascii=False)
            if has_members:
                ins = "\n" + _object_indent(text, obj) + pair + ","
            else:
                ins = pair
            return text[: obj + 1] + ins + text[obj + 1 :]
        _kstart, vstart, vend = member
        if last:
            return text[:vstart] + json.dumps(value, ensure_ascii=False) + text[vend:]
        if strict[vstart] != "{":
            raise EngineError("%s: %s is not an object" % (dotted, seg))
        obj = vstart
    raise EngineError("unreachable")  # pragma: no cover


def jsonc_set_file(
    path: str,
    dotted: str,
    value: Any,
    backed_up: Optional[Set[str]] = None,
    merge_lists: bool = True,
    literal_key: bool = False,
) -> Tuple[Any, Any]:
    """Set ``dotted`` in a JSONC file via offset-preserving surgical edit (comments
    survive). List values append-dedupe (``merge_lists=False`` replaces — restore path).
    ``literal_key=True`` treats ``dotted`` as ONE flat key even when it contains dots
    (VS Code settings.json keys are flat — "chat.defaultModel" is a single member).
    Returns ``(prior, final)``."""
    path = os.path.expanduser(path)
    text = "{}"
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read() or "{}"
    data = _jsonc_loads(text)
    if not isinstance(data, dict):
        raise EngineError("%s: top-level JSONC value is not an object" % path)
    node: Any = data
    prior: Any = ABSENT
    parts = [dotted] if literal_key else dotted.split(".")
    for seg in parts[:-1]:
        node = node.get(seg) if isinstance(node, dict) else None
    if isinstance(node, dict) and parts[-1] in node:
        prior = node[parts[-1]]
    final = (
        _merge_value(prior, value)
        if merge_lists and isinstance(prior, list) and isinstance(value, list)
        else value
    )
    new_text = _jsonc_edit(text, dotted, final, parts=parts)
    _jsonc_loads(new_text)  # validate before touching disk
    backup_file(path, backed_up)
    _atomic_write_text(path, new_text)
    return prior, final


def jsonc_delete_file(
    path: str,
    dotted: str,
    backed_up: Optional[Set[str]] = None,
    literal_key: bool = False,
) -> Any:
    """Surgically remove ``dotted`` (and its separator comma) from a JSONC file.
    ``literal_key=True`` treats ``dotted`` as ONE flat key (VS Code settings.json)."""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return ABSENT
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    strict = _jsonc_to_strict(text)
    root = _skip_ws(strict, 0)
    if root >= len(strict) or strict[root] != "{":
        raise EngineError("%s: JSONC root is not an object" % path)
    parts = [dotted] if literal_key else dotted.split(".")
    obj = root
    member = None
    for depth, seg in enumerate(parts):
        member = _find_member(strict, obj, seg)
        if member is None:
            return ABSENT
        if depth < len(parts) - 1:
            if strict[member[1]] != "{":
                return ABSENT
            obj = member[1]
    kstart, vstart, vend = member  # type: ignore[misc]
    removed = json.loads(strict[vstart:vend])
    start, end = kstart, vend
    j = _skip_ws(strict, vend)
    if j < len(strict) and strict[j] == ",":
        end = j + 1  # consume the trailing separator
    else:
        k = kstart - 1
        while k > obj and strict[k] in " \t\r\n":
            k -= 1
        if strict[k] == ",":
            start = k  # last member: consume the leading separator
    new_text = text[:start] + text[end:]
    _jsonc_loads(new_text)  # validate before touching disk
    backup_file(path, backed_up)
    _atomic_write_text(path, new_text)
    return removed


# ---------------------------------------------------------------------------- TOML ----
def _tomlkit():
    try:
        import tomlkit  # noqa: PLC0415 — lazy: `import autoconfig` must work without it
    except ImportError as e:  # pragma: no cover - environment-dependent
        raise EngineError(
            "tomlkit is required for TOML config edits (pip install tomlkit)"
        ) from e
    return tomlkit


def _toml_unwrap(v: Any) -> Any:
    return v.unwrap() if hasattr(v, "unwrap") else v


_TOML_TABLE_LINE = re.compile(r"^\s*\[", re.M)


def toml_set(
    path: str,
    dotted: str,
    value: Any,
    backed_up: Optional[Set[str]] = None,
    merge_lists: bool = True,
) -> Tuple[Any, Any]:
    """Set ``dotted`` in a TOML file via tomlkit (comments preserved).

    Root-key ordering guard: TOML root keys must appear BEFORE any ``[table]`` — a key
    appended after a table silently becomes that table's key (grounded:
    platform-codex-cli.json safe_merge_method). A NEW root key in a document that has
    tables is therefore inserted at text level before the first table header; the
    result is round-trip validated before the atomic write. Returns ``(prior, final)``."""
    tomlkit = _tomlkit()
    path = os.path.expanduser(path)
    text = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    try:
        doc = tomlkit.parse(text)
    except Exception as e:
        raise EngineError("%s: not valid TOML (%s) — refusing to modify" % (path, e)) from e

    parts = dotted.split(".")
    node: Any = doc
    for seg in parts[:-1]:
        if seg in node:
            node = node[seg]
        else:
            t = tomlkit.table()
            node[seg] = t
            node = t
    leaf = parts[-1]
    prior: Any = _toml_unwrap(node[leaf]) if leaf in node else ABSENT
    final = (
        _merge_value(prior, value)
        if merge_lists and isinstance(prior, list) and isinstance(value, list)
        else value
    )

    root_new_key = len(parts) == 1 and leaf not in doc
    m = _TOML_TABLE_LINE.search(text)
    if root_new_key and m is not None:
        # never append a root key after a [table] — insert it before the first table
        val_text = tomlkit.item(final).as_string()
        insert_at = m.start()
        new_text = text[:insert_at] + "%s = %s\n" % (leaf, val_text) + text[insert_at:]
    else:
        node[leaf] = final
        new_text = tomlkit.dumps(doc)

    # round-trip validation: the key must resolve to `final` at the intended path
    check: Any = tomlkit.parse(new_text)
    for seg in parts:
        if seg not in check:
            raise EngineError("%s: TOML write validation failed for %s" % (path, dotted))
        check = check[seg]
    if _toml_unwrap(check) != final:
        raise EngineError("%s: TOML write validation failed for %s" % (path, dotted))

    backup_file(path, backed_up)
    _atomic_write_text(path, new_text)
    return prior, final


def toml_delete(path: str, dotted: str, backed_up: Optional[Set[str]] = None) -> Any:
    """Remove ``dotted`` from a TOML file via tomlkit. Returns removed value or ABSENT."""
    tomlkit = _tomlkit()
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return ABSENT
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    try:
        doc = tomlkit.parse(text)
    except Exception as e:
        raise EngineError("%s: not valid TOML (%s) — refusing to modify" % (path, e)) from e
    parts = dotted.split(".")
    node: Any = doc
    for seg in parts[:-1]:
        if seg not in node:
            return ABSENT
        node = node[seg]
    leaf = parts[-1]
    if leaf not in node:
        return ABSENT
    removed = _toml_unwrap(node[leaf])
    del node[leaf]
    backup_file(path, backed_up)
    _atomic_write_text(path, tomlkit.dumps(doc))
    return removed


# --------------------------------------------------------------------- YAML via CLI ----
def run_cli(argv: List[str], timeout: int = 20) -> Tuple[int, str]:
    """Run a platform CLI (list argv, never a shell string). Returns (rc, stdout)."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    return p.returncode, p.stdout or ""


def yaml_cli_set(
    config_path: str,
    set_argv: List[str],
    get_argv: Optional[List[str]] = None,
    backed_up: Optional[Set[str]] = None,
    timeout: int = 20,
) -> Any:
    """Set a YAML-backed config value via the platform's OWN ``config set`` CLI (the
    sanctioned, leaf-merge-safe path for Hermes/OpenClaw — no hand-rolled YAML surgery).

    Backs up ``config_path`` before the first write of the run. When ``get_argv`` is
    given, the prior value is read first (for the manifest row). Returns the prior
    value (string, or ABSENT when unreadable/empty)."""
    prior: Any = ABSENT
    if get_argv:
        rc, out = run_cli(get_argv, timeout=timeout)
        if rc == 0 and out.strip():
            prior = out.strip()
    backup_file(config_path, backed_up)
    rc, _out = run_cli(set_argv, timeout=timeout)
    if rc != 0:
        raise EngineError("platform CLI failed (%s -> exit %d)" % (" ".join(set_argv), rc))
    return prior


# ---------------------------------------------------------------- apply + manifest ----
def detect_format(path: str) -> str:
    """Best-effort file-format detection for the generic writers: extension first, then
    (for .json) a strict-parse probe with JSONC fallback — Cursor's permissions.json is
    JSONC despite its extension (grounded: platform-cursor.json)."""
    lower = path.lower()
    if lower.endswith(".toml"):
        return "toml"
    if lower.endswith(".jsonc"):
        return "jsonc"
    if lower.endswith((".yaml", ".yml")):
        return "yaml-cli"
    if lower.endswith(".json"):
        real = os.path.expanduser(path)
        if os.path.exists(real):
            try:
                with open(real, "r", encoding="utf-8") as f:
                    text = f.read()
                if text.strip():
                    json.loads(text)
            except ValueError:
                try:
                    _jsonc_loads(text)
                    return "jsonc"
                except EngineError:
                    pass
        return "json"
    return "unknown"


def _wrap(v: Any) -> dict:
    """Manifest encoding for prior/new: {'absent': true} when the key did not exist,
    else {'value': <json value>} — one field, no ambiguity with real null values."""
    if v is ABSENT:
        return {"absent": True}
    return {"value": v}


def _unwrap(w: Any) -> Tuple[bool, Any]:
    """Inverse of _wrap: returns (absent, value)."""
    if isinstance(w, dict) and w.get("absent"):
        return True, None
    if isinstance(w, dict) and "value" in w:
        return False, w["value"]
    return False, w


def setting_row(change: ProposedChange, prior: Any, new: Any) -> dict:
    """One manifest ``"setting"`` row — EXACTLY these seven fields."""
    return {
        "type": "setting",
        "platform": change.platform,
        "path": change.file,
        "key": change.key_path,
        "prior": _wrap(prior),
        "new": _wrap(new),
        "created": _now_iso(),
    }


def _read_tree(path: str, fmt: str) -> Optional[dict]:
    """Parsed object tree of a config file for ancestor bookkeeping (Z2-FIX-1).
    Returns None when the file is unreadable or ``fmt`` has no generic reader."""
    try:
        if fmt == "json":
            return json_read(path)
        if fmt == "jsonc":
            return jsonc_read(path)
        if fmt == "toml":
            tomlkit = _tomlkit()
            real = os.path.expanduser(path)
            if not os.path.exists(real):
                return {}
            with open(real, "r", encoding="utf-8") as f:
                text = f.read()
            return _toml_unwrap(tomlkit.parse(text))
    except Exception:
        return None
    return None


def _missing_ancestors(path: str, dotted: str, fmt: str) -> List[str]:
    """Dotted paths of the ancestor containers of ``dotted`` that do NOT yet exist in
    the file — i.e. exactly the parents the coming write itself will create. Recorded
    on the manifest row (``created_parents``) so ``--restore`` can prune them once the
    restore empties them (Z2-FIX-1). A parent that pre-existed before the apply — even
    as an empty object — is never listed here, so it can never be pruned."""
    parts = dotted.split(".")
    if len(parts) < 2:
        return []
    tree = _read_tree(path, fmt)
    if tree is None:
        return []
    missing: List[str] = []
    node: Any = tree
    prefix: List[str] = []
    for seg in parts[:-1]:
        prefix.append(seg)
        if missing or not isinstance(node, dict) or seg not in node:
            missing.append(".".join(prefix))
            node = None
        else:
            node = node[seg]
    return missing


def apply_change(
    change: ProposedChange,
    backed_up: Optional[Set[str]] = None,
    fmt: Optional[str] = None,
) -> dict:
    """Apply one change through the format-matched safe-merge writer and return its
    manifest row. The prior value is re-read from disk at apply time (the plan may be
    stale), so ``--restore`` reverts to what was REALLY there. Ancestor containers the
    write itself creates are recorded on the row as ``created_parents`` (field present
    only when non-empty) so ``--restore`` can prune the empty shells it leaves behind
    (Z2-FIX-1) — a parent that pre-existed is never recorded, so never pruned."""
    fmt = fmt or detect_format(change.file)
    created_parents = _missing_ancestors(change.file, change.key_path, fmt)
    if fmt == "json":
        prior, final = json_set(change.file, change.key_path, change.new_value, backed_up)
    elif fmt == "jsonc":
        prior, final = jsonc_set_file(change.file, change.key_path, change.new_value, backed_up)
    elif fmt == "toml":
        prior, final = toml_set(change.file, change.key_path, change.new_value, backed_up)
    else:
        raise EngineError(
            "%s: no generic writer for format %r — the platform adapter must apply this "
            "change itself (e.g. via yaml_cli_set)" % (change.file, fmt)
        )
    row = setting_row(change, prior, final)
    if created_parents:
        row["created_parents"] = created_parents
    return row


def apply_changes(
    changes: List[ProposedChange],
    backed_up: Optional[Set[str]] = None,
) -> List[dict]:
    """Apply a list of consented changes; returns their manifest rows."""
    if backed_up is None:
        backed_up = set()
    return [apply_change(ch, backed_up=backed_up) for ch in changes]


def manifest_path() -> str:
    """The SAME manifest install.sh writes: ~/.ultramemory/install-manifest.json
    (install.sh:21 — UM_DIR=$HOME/.ultramemory, MANIFEST=$UM_DIR/install-manifest.json)."""
    return os.path.join(os.path.expanduser("~"), ".ultramemory", "install-manifest.json")


def _manifest_read(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _manifest_write(path: str, data: dict) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    _atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def append_setting_rows(rows: List[dict], path: Optional[str] = None) -> str:
    """Append ``"setting"`` rows to the kit install manifest (kit_version/tier and every
    other item type are preserved verbatim; the manifest is created if absent)."""
    mf = path or manifest_path()
    data = _manifest_read(mf)
    items = data.setdefault("items", {})
    if not isinstance(items, dict):
        items = {}
        data["items"] = items
    lst = items.setdefault("setting", [])
    if not isinstance(lst, list):
        lst = []
        items["setting"] = lst
    lst.extend(rows)
    _manifest_write(mf, data)
    return mf


def read_setting_rows(path: Optional[str] = None) -> List[dict]:
    mf = path or manifest_path()
    data = _manifest_read(mf)
    rows = data.get("items", {}).get("setting", []) if isinstance(data.get("items"), dict) else []
    return [r for r in rows if isinstance(r, dict)]


# -------------------------------------------------------------------------- restore ----
def _prune_created_parents(path: str, created: Any, fmt: str, backed_up: Set[str]) -> None:
    """Z2-FIX-1: after a delete-when-absent leaf restore, remove the ancestor containers
    that the APPLY ITSELF created (the row's recorded ``created_parents``) and that the
    restore has now left empty — deepest first. Parents that pre-existed before the
    apply (even as empty objects) were never recorded, so they are never pruned.
    Best-effort: a prune failure warns but never fails the row (its leaf is restored)."""
    if not isinstance(created, list):
        return
    ancestors = sorted(
        {str(a) for a in created if a and str(a).strip()},
        key=lambda a: a.count("."),
        reverse=True,  # deepest first — an emptied child may empty its parent
    )
    for anc in ancestors:
        try:
            tree = _read_tree(path, fmt)
            if tree is None:
                return
            node = _get_dotted(tree, anc)
            if not isinstance(node, dict) or node:
                continue  # missing, not an object, or not empty — leave it alone
            if fmt == "json":
                json_delete(path, anc, backed_up)
            elif fmt == "jsonc":
                jsonc_delete_file(path, anc, backed_up)
            elif fmt == "toml":
                toml_delete(path, anc, backed_up)
        except EngineError as e:
            print("  ! could not prune empty %s in %s — %s" % (anc, path, e))
            return


def _restore_row(row: dict, backed_up: Set[str]) -> bool:
    """Surgically revert ONE manifest row: set its key back to the recorded prior value
    (or remove the key when it did not exist before). Only the named key is touched —
    NEVER a blind backup copy. Returns True when the row was restored (drop it)."""
    path = os.path.expanduser(str(row.get("path") or ""))
    key = str(row.get("key") or "")
    if not path or not key:
        return True  # malformed row — nothing actionable, drop it
    if not os.path.exists(path):
        print("  ! %s no longer exists — nothing to revert for %s" % (path, key))
        return True
    absent, prior = _unwrap(row.get("prior"))
    fmt = detect_format(path)
    try:
        # merge_lists=False: restore REPLACES with the recorded prior exactly — the
        # append-dedupe merge can never remove the entries we added.
        if fmt == "json":
            if absent:
                json_delete(path, key, backed_up)
            else:
                json_set(path, key, prior, backed_up, merge_lists=False)
        elif fmt == "jsonc":
            if absent:
                jsonc_delete_file(path, key, backed_up)
            else:
                jsonc_set_file(path, key, prior, backed_up, merge_lists=False)
        elif fmt == "toml":
            if absent:
                toml_delete(path, key, backed_up)
            else:
                toml_set(path, key, prior, backed_up, merge_lists=False)
        else:
            print(
                "  ! %s (%s): no generic restore for this format — the %s adapter must "
                "revert %s" % (path, fmt, row.get("platform") or "platform", key)
            )
            return False
    except EngineError as e:
        print("  ! restore failed for %s :: %s — %s" % (path, key, e))
        return False
    if absent:
        # Z2-FIX-1: the leaf never existed before the apply — also prune any ancestor
        # container the apply itself created that this restore has now left empty.
        _prune_created_parents(path, row.get("created_parents"), fmt, backed_up)
    print("  restored %s :: %s" % (path, key))
    return True


def restore_rows(rows: List[dict], backed_up: Optional[Set[str]] = None) -> List[dict]:
    """Revert rows (newest first) to their recorded priors; returns the rows that could
    NOT be restored so the caller keeps them in the manifest.

    Rows whose platform adapter OVERRIDES ``restore`` are dispatched to that adapter —
    the CLI-merged platforms (hermes/openclaw: the platform's own ``config set`` is the
    only sanctioned merge path) and the hook-file installs (cline TaskStart, openclaw
    hook dir) cannot be reverted by the generic format writers. Adapters that keep the
    base ``restore`` go through the generic per-row path unchanged."""
    if backed_up is None:
        backed_up = set()
    dispatched: Dict[str, List[dict]] = {}
    generic: List[dict] = []
    for row in rows:
        cls = ADAPTERS.get(str(row.get("platform") or ""))
        if cls is not None and cls.restore is not PlatformAdapter.restore:
            dispatched.setdefault(cls.name, []).append(row)
        else:
            generic.append(row)
    kept: List[dict] = []
    for name, group in dispatched.items():
        try:
            kept.extend(ADAPTERS[name]().restore(group))
        except Exception as e:  # a broken adapter must not lose the rows
            print("  ! %s adapter restore failed — %s (rows kept in the manifest)" % (name, e))
            kept.extend(group)
    g_kept: List[dict] = []
    for row in reversed(generic):
        if not _restore_row(row, backed_up):
            g_kept.append(row)
    g_kept.reverse()
    kept.extend(g_kept)
    return kept


def run_restore(dry_run: bool = False) -> int:
    """``ultramemory configure --restore``: revert every manifest ``"setting"`` row to
    its recorded prior value, then drop the restored rows from the manifest."""
    mf = manifest_path()
    rows = read_setting_rows(mf)
    if not rows:
        print("Nothing to restore — no configure-owned settings recorded in %s." % mf)
        return 0
    if dry_run:
        print("UltraMemory configure --restore — plan (dry-run, nothing will be written):")
        for row in reversed(rows):
            absent, prior = _unwrap(row.get("prior"))
            tgt = "remove the key" if absent else "revert to %s" % json.dumps(prior, ensure_ascii=False)
            print("  [%s] %s :: %s -> %s" % (row.get("platform"), row.get("path"), row.get("key"), tgt))
        return 0
    kept = restore_rows(rows)
    data = _manifest_read(mf)
    items = data.get("items")
    if isinstance(items, dict):
        items["setting"] = kept
        _manifest_write(mf, data)
    if kept:
        print("! %d row(s) could not be restored automatically — kept in %s." % (len(kept), mf))
        return 1
    print("All configure-owned settings restored to their prior values.")
    return 0


# ------------------------------------------------------------------- consent runner ----
def _display(v: Any) -> str:
    if v is ABSENT:
        return "«absent»"
    try:
        return json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(v)


def _parse_items(items: Optional[str]) -> List[str]:
    if not items:
        return []
    return [t.strip() for t in items.split(",") if t.strip()]


def _matches_items(change: ProposedChange, tokens: List[str]) -> bool:
    """``--items`` token matching: a token selects a change when it equals the change's
    full dotted key_path, its last segment, or the proposed new value (all
    case-insensitive). The value form is what the adapter items mandate — e.g.
    ``--items xhigh`` (codex, platform-codex-cli.json: xhigh is Responses-API/model-
    gated) and ``--items unrestricted`` (cursor) opt into the gated maximums."""
    if not tokens:
        return True
    kp = change.key_path.lower()
    leaf = kp.split(".")[-1]
    cands = {kp, leaf}
    if isinstance(change.new_value, str):
        cands.add(change.new_value.lower())
    return any(t.lower() in cands for t in tokens)


def _safe_detect(adapter: PlatformAdapter) -> bool:
    try:
        return bool(adapter.detect())
    except NotImplementedError:
        return False
    except Exception:
        return False


def _ask_yn(prompt: str) -> bool:
    """Interactive per-item consent — default is No."""
    try:
        ans = input(prompt)
    except EOFError:
        return False
    return ans.strip().lower() in ("y", "yes")


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


def run_configure(
    platform: str = "auto",
    yes: bool = False,
    items: Optional[str] = None,
    dry_run: bool = False,
    model: Optional[str] = None,
    restore: bool = False,
) -> int:
    """The ``ultramemory configure`` entry point (delegated from cli.py).

    Consent contract (marketplace-policy grounding — silent settings modification is a
    listing blocker): every proposed change is listed with file, key, and before->after;
    interactive approval is PER ITEM with default No; ``--yes`` (optionally scoped by
    ``--items``) is the only non-interactive approval; a non-interactive run without
    ``--yes`` exits 2 with zero writes; ``--dry-run`` always exits 0 with zero writes."""
    if restore:
        return run_restore(dry_run=dry_run)

    tokens = _parse_items(items)
    if platform == "all":
        names = list(PLATFORM_NAMES)
    elif platform == "auto":
        names = []
        for n in PLATFORM_NAMES:
            cls = ADAPTERS.get(n)
            if cls is not None and _safe_detect(cls()):
                names.append(n)
    else:
        names = [platform]

    print("UltraMemory configure — proposed changes (per-item consent; nothing changes without your yes)")
    plan: List[Tuple[PlatformAdapter, ProposedChange]] = []
    for name in names:
        cls = ADAPTERS.get(name)
        if cls is None:
            print("  [%s] no changes planned — platform adapter not yet available in this build" % name)
            continue
        adapter = cls()
        if platform not in ("auto",) and not _safe_detect(adapter):
            print("  [%s] not detected on this machine — skipped" % name)
            continue
        try:
            changes = adapter.plan(model=model, items=tokens)
        except NotImplementedError:
            print("  [%s] no changes planned — platform adapter not yet available in this build" % name)
            continue
        except EngineError as e:
            print("  [%s] plan failed: %s" % (name, e))
            continue
        if not changes:
            print("  [%s] already tuned — nothing to change" % name)
            continue
        for ch in changes:
            print(
                "  [%s] %s :: %s  %s -> %s  (%s) — %s"
                % (name, ch.file, ch.key_path, _display(ch.prior_value), _display(ch.new_value), ch.kind, ch.description)
            )
            plan.append((adapter, ch))
    if platform == "auto" and not names:
        print("  no supported platforms detected (adapters register with the platform items)")

    if dry_run:
        print("dry-run: plan only — nothing was written.")
        return 0
    if not plan:
        print("Nothing to do.")
        return 0

    filtered = [(a, ch) for a, ch in plan if _matches_items(ch, tokens)]
    if tokens and not filtered:
        print("No planned change matches --items %s — nothing changed." % ",".join(tokens))
        return 0

    if yes:
        approved = filtered
    else:
        if not _stdin_is_tty():
            print(
                "error: explicit consent required — nothing was changed. Re-run interactively, "
                "or non-interactively with --yes (optionally scoped: --items <csv>), or preview "
                "with --dry-run.",
                file=sys.stderr,
            )
            return 2
        approved = []
        for adapter, ch in filtered:
            prompt = (
                "  [%s] %s\n      %s :: %s  %s -> %s\n  Apply this change? [y/N] "
                % (adapter.name, ch.description, ch.file, ch.key_path, _display(ch.prior_value), _display(ch.new_value))
            )
            if _ask_yn(prompt):
                approved.append((adapter, ch))
    if not approved:
        print("No items approved — nothing changed.")
        return 0

    backed_up: Set[str] = set()
    rows: List[dict] = []
    by_adapter: Dict[int, Tuple[PlatformAdapter, List[ProposedChange]]] = {}
    for adapter, ch in approved:
        by_adapter.setdefault(id(adapter), (adapter, []))[1].append(ch)
    try:
        for adapter, chs in by_adapter.values():
            rows.extend(adapter.apply(chs, backed_up=backed_up))
    except EngineError as e:
        if rows:
            append_setting_rows(rows)  # record what DID land so --restore can revert it
        print("error: %s" % e, file=sys.stderr)
        return 1
    if rows:
        append_setting_rows(rows)
    print(
        "Applied %d change(s). Backups: <file>.um-backup-<timestamp> next to each modified "
        "file. Revert anytime: ultramemory configure --restore" % len(rows)
    )
    return 0


# ===================================================================================
# Platform adapters (checklist Clusters B/C). EVERY config key, path, and gotcha below
# is grounded in research/autoconfig-grounding-2026-07-21/platform-<name>.json — cited
# per adapter. Adapters plan READ-ONLY; writes go through the engine's safe-merge
# helpers (or the platform's own CLI where that is the sanctioned merge path).
# ===================================================================================

#: The eight UltraMemory MCP tools (founder ruling: pre-approve ONLY these — per-tool
#: enumeration, marketing-honest). Order per checklist items B1/B2.
ULTRAMEMORY_TOOLS = [
    "memory_recall",
    "memory_write",
    "search",
    "fetch",
    "recall_gated",
    "recall_verified",
    "playbook_recall",
    "memory_feedback",
]

#: The kit-shipped session-start onboarding script (checklist B1 literal; E1 ships it).
ONBOARD_SCRIPT = "~/.claude/hooks/onboard-ultramemory.sh"


def _onboard_cmd_abs() -> str:
    """Absolute path to the onboarding script for platforms whose hook runners may not
    expand ``~`` (cursor runs user hooks with cwd ~/.cursor/ — platform-cursor.json)."""
    return os.path.expanduser(ONBOARD_SCRIPT)


def _detect_dir_or_cli(dirs: List[str], clis: List[str]) -> bool:
    for d in dirs:
        if d and os.path.isdir(os.path.expanduser(d)):
            return True
    return any(shutil.which(c) for c in clis)


def _get_dotted(data: Any, dotted: str) -> Any:
    """Read a dotted path out of nested dicts; ABSENT when any segment is missing."""
    node = data
    for seg in dotted.split("."):
        if not isinstance(node, dict) or seg not in node:
            return ABSENT
        node = node[seg]
    return node


def _parse_version(text: str) -> Optional[Tuple[int, int, int]]:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def _write_executable(path: str, text: str) -> None:
    """Create an executable hook script (0755) via temp + atomic rename."""
    _atomic_write_text(path, text)
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass


# ------------------------------------------------------------- B1: Claude Code ----
@register_adapter("claude-code")
class ClaudeCodeAdapter(PlatformAdapter):
    """Claude Code CLI (grounding: platform-claude-code.json, confidence high).

    Targets ONLY ~/.claude/settings.json (strict JSON). NEVER touches ~/.claude.json —
    MCP registration stays ``claude mcp add`` (known corruption gotcha, grounded)."""

    SETTINGS = "~/.claude/settings.json"

    def detect(self) -> bool:
        return _detect_dir_or_cli(["~/.claude"], ["claude"])

    def plan(
        self,
        model: Optional[str] = None,
        items: Optional[List[str]] = None,
    ) -> List[ProposedChange]:
        path = os.path.expanduser(self.SETTINGS)
        data = json_read(path)
        changes: List[ProposedChange] = []

        want_model = model or "opus"  # alias tracks the newest Opus (grounded default)
        if data.get("model") != want_model:
            changes.append(ProposedChange(
                "claude-code", path, "model",
                data["model"] if "model" in data else ABSENT, want_model, "persist",
                'default model alias ("opus" tracks the newest Opus; --model overrides, '
                "e.g. claude-fable-5; ANTHROPIC_MODEL in your shell would still win)",
            ))

        if data.get("effortLevel") != "xhigh":
            changes.append(ProposedChange(
                "claude-code", path, "effortLevel",
                data["effortLevel"] if "effortLevel" in data else ABSENT, "xhigh", "persist",
                'persistable maximum effort ("max"/"ultracode" are NOT valid settings '
                "values — the session-start onboarding hook offers those per session)",
            ))

        cur_mode = _get_dotted(data, "permissions.defaultMode")
        if cur_mode != "auto":
            changes.append(ProposedChange(
                "claude-code", path, "permissions.defaultMode", cur_mode, "auto", "persist",
                'low-friction permission mode "auto" (honored ONLY at user scope; if your '
                'account/model is ineligible Claude Code ignores it — use "acceptEdits" '
                "as the fallback)",
            ))

        rules = ["mcp__ultramemory__%s" % t for t in ULTRAMEMORY_TOOLS]
        cur_allow = _get_dotted(data, "permissions.allow")
        have = cur_allow if isinstance(cur_allow, list) else []
        if any(r not in have for r in rules):
            changes.append(ProposedChange(
                "claude-code", path, "permissions.allow", cur_allow, rules, "persist",
                "pre-approve ONLY the 8 UltraMemory MCP tools (per-tool enumeration, "
                "marketing-honest; appended — your existing allow rules are kept)",
            ))

        cur_hooks = _get_dotted(data, "hooks.SessionStart")
        wired = isinstance(cur_hooks, list) and "onboard-ultramemory.sh" in json.dumps(cur_hooks)
        if not wired:
            entry = {
                "matcher": "startup|clear",
                "hooks": [{"type": "command", "command": ONBOARD_SCRIPT}],
            }
            changes.append(ProposedChange(
                "claude-code", path, "hooks.SessionStart", cur_hooks, [entry], "onboard-hook",
                "session-start onboarding hook (proposes the session-only ultracode/max "
                "upgrade; appended — existing hooks kept; takes effect from your NEXT session)",
            ))
        return changes


# ------------------------------------------------------------------ B2: Codex ----
@register_adapter("codex")
class CodexAdapter(PlatformAdapter):
    """OpenAI Codex CLI (grounding: platform-codex-cli.json).

    ~/.codex/config.toml is merged via tomlkit with the root-keys-before-tables guard
    (the grounded #1 corruption trap); hooks live in ~/.codex/hooks.json (JSON) and are
    gated by Codex's hash-based trust review — the consent copy says so explicitly."""

    CONFIG = "~/.codex/config.toml"
    HOOKS = "~/.codex/hooks.json"

    def detect(self) -> bool:
        return _detect_dir_or_cli(["~/.codex"], ["codex"])

    def plan(
        self,
        model: Optional[str] = None,
        items: Optional[List[str]] = None,
    ) -> List[ProposedChange]:
        tokens = [t.lower() for t in (items or [])]
        tomlkit = _tomlkit()
        path = os.path.expanduser(self.CONFIG)
        text = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        try:
            doc = tomlkit.parse(text)
        except Exception as e:
            raise EngineError("%s: not valid TOML (%s) — refusing to plan against it" % (path, e)) from e
        changes: List[ProposedChange] = []

        # xhigh is Responses-API-only and model-gated (grounded) — "high" is the
        # universal safe max; xhigh ONLY with explicit --items xhigh.
        effort = "xhigh" if "xhigh" in tokens else "high"
        cur_effort = _toml_unwrap(doc["model_reasoning_effort"]) if "model_reasoning_effort" in doc else ABSENT
        if cur_effort != effort:
            changes.append(ProposedChange(
                "codex", path, "model_reasoning_effort", cur_effort, effort, "persist",
                'persistable reasoning effort ("high" = universal safe max; "xhigh" is '
                "Responses-API/model-gated — opt in with --items xhigh)",
            ))

        cur_ap = _toml_unwrap(doc["approval_policy"]) if "approval_policy" in doc else ABSENT
        if cur_ap != "on-request":
            changes.append(ProposedChange(
                "codex", path, "approval_policy", cur_ap, "on-request", "persist",
                'low-friction-but-safe approvals ("never"/"danger-full-access" are NEVER shipped)',
            ))

        cur_sm = _toml_unwrap(doc["sandbox_mode"]) if "sandbox_mode" in doc else ABSENT
        if cur_sm != "workspace-write":
            changes.append(ProposedChange(
                "codex", path, "sandbox_mode", cur_sm, "workspace-write", "persist",
                "workspace-write sandbox (pairs with on-request approvals; never danger-full-access)",
            ))

        # per-server allowlist ONLY if the UltraMemory registration exists (item B2)
        mcp = doc.get("mcp_servers")
        if mcp is not None and "ultramemory" in mcp:
            server = mcp["ultramemory"]
            cur_tools = _toml_unwrap(server["enabled_tools"]) if "enabled_tools" in server else ABSENT
            have = cur_tools if isinstance(cur_tools, list) else []
            if any(t not in have for t in ULTRAMEMORY_TOOLS):
                changes.append(ProposedChange(
                    "codex", path, "mcp_servers.ultramemory.enabled_tools",
                    cur_tools, list(ULTRAMEMORY_TOOLS), "persist",
                    "per-server allowlist: expose exactly the 8 UltraMemory tools "
                    "(enabled_tools — no blanket bypass)",
                ))

        # model: rollout is Statsig/account-gated (grounded) — ONLY with --model
        if model:
            cur_model = _toml_unwrap(doc["model"]) if "model" in doc else ABSENT
            if cur_model != model:
                changes.append(ProposedChange(
                    "codex", path, "model", cur_model, model, "persist",
                    "model override (--model given; by default Codex tracks its own "
                    "rolling flagship — model ids are account-gated via Statsig)",
                ))

        # SessionStart onboarding in ~/.codex/hooks.json — trust-gate copy is MANDATORY
        hooks_path = os.path.expanduser(self.HOOKS)
        hooks_data = json_read(hooks_path)
        cur_ss = _get_dotted(hooks_data, "hooks.SessionStart")
        wired = isinstance(cur_ss, list) and "onboard-ultramemory.sh" in json.dumps(cur_ss)
        if not wired:
            entry = {
                "matcher": "startup|clear",
                "hooks": [{"type": "command", "command": _onboard_cmd_abs()}],
            }
            changes.append(ProposedChange(
                "codex", hooks_path, "hooks.SessionStart", cur_ss, [entry], "onboard-hook",
                "session-start onboarding hook. IMPORTANT: Codex will NOT run it until "
                "you trust it — open Codex, run /hooks, and choose Trust (hash-based "
                "trust gate; re-review on any change)",
            ))
        return changes


# ------------------------------------------------------------- B3: Gemini CLI ----
@register_adapter("gemini")
class GeminiAdapter(PlatformAdapter):
    """Google Gemini CLI (grounding: platform-gemini-cli.json).

    ~/.gemini/settings.json is plain JSON but has a SCHEMA FORK: v1 flat (pre-2025-09-17,
    real in the wild — this very Mac ran 0.1.0 flat) vs v2 nested. v1 -> skip+warn with
    an upgrade hint, ZERO writes; never hand-write the v2 migration. YOLO is NOT a
    persistable value (settingsSchema.ts) — auto_edit is the persistable ceiling. Hooks
    need CLI >= 0.26.0 (version-gated via ``gemini --version``)."""

    SETTINGS = "~/.gemini/settings.json"
    #: v1 flat-format indicator keys (grounded: local 0.1.0 install + settings docs)
    V1_KEYS = ("theme", "selectedAuthType", "autoAccept", "preferredEditor", "coreTools", "contextFileName")
    #: v2 nested-format top-level sections (grounded: v2 schema list in safe_merge_method)
    V2_KEYS = ("general", "ui", "model", "modelConfigs", "tools", "mcp", "mcpServers",
               "security", "hooks", "context", "advanced")
    #: grounded modelConfigs override shape (docs/cli/generation-settings.md)
    THINKING_OVERRIDE = {
        "match": {"model": "gemini-3-pro-preview"},
        "modelConfig": {"generateContentConfig": {"thinkingConfig": {"thinkingLevel": "HIGH"}}},
    }

    def detect(self) -> bool:
        return _detect_dir_or_cli(["~/.gemini"], ["gemini"])

    def plan(
        self,
        model: Optional[str] = None,
        items: Optional[List[str]] = None,
    ) -> List[ProposedChange]:
        path = os.path.expanduser(self.SETTINGS)
        data = json_read(path)
        if data and any(k in data for k in self.V1_KEYS) and not any(k in data for k in self.V2_KEYS):
            raise EngineError(
                "%s uses the v1 FLAT settings schema (pre-2025-09-17). Nothing was "
                "changed — upgrade Gemini CLI and launch it once (it auto-migrates to "
                "the v2 nested schema), then re-run `ultramemory configure`" % path
            )
        changes: List[ProposedChange] = []

        # model.name ONLY with --model (preview access is entitlement-gated; default
        # routing/Auto is the grounded recommendation — leave routing alone)
        if model:
            cur = _get_dotted(data, "model.name")
            if cur != model:
                changes.append(ProposedChange(
                    "gemini", path, "model.name", cur, model, "persist",
                    "model pin (--model given; preview models are entitlement-checked "
                    "at startup — an inaccessible pin gets reset to Auto)",
                ))

        cur_over = _get_dotted(data, "modelConfigs.overrides")
        have = cur_over if isinstance(cur_over, list) else []
        if self.THINKING_OVERRIDE not in have:
            changes.append(ProposedChange(
                "gemini", path, "modelConfigs.overrides", cur_over,
                [dict(self.THINKING_OVERRIDE)], "persist",
                'pin thinkingLevel "HIGH" for Gemini 3 (already the built-in default '
                "preset — this pins it against future default changes)",
            ))

        cur_mode = _get_dotted(data, "general.defaultApprovalMode")
        if cur_mode != "auto_edit":
            changes.append(ProposedChange(
                "gemini", path, "general.defaultApprovalMode", cur_mode, "auto_edit", "persist",
                'auto-approve edit tools ("yolo" is NOT persistable — settingsSchema.ts; '
                "full yolo stays a --yolo launch flag)",
            ))

        cur_trust = _get_dotted(data, "mcpServers.ultramemory.trust")
        if cur_trust is not True:
            changes.append(ProposedChange(
                "gemini", path, "mcpServers.ultramemory.trust", cur_trust, True, "persist",
                "trust ONLY the ultramemory server (bypasses tool-call confirmations "
                "for this one server — no blanket bypass)",
            ))

        cur_inc = _get_dotted(data, "mcpServers.ultramemory.includeTools")
        have_inc = cur_inc if isinstance(cur_inc, list) else []
        if any(t not in have_inc for t in ULTRAMEMORY_TOOLS):
            changes.append(ProposedChange(
                "gemini", path, "mcpServers.ultramemory.includeTools", cur_inc,
                list(ULTRAMEMORY_TOOLS), "persist",
                "expose exactly the 8 UltraMemory tools (includeTools allowlist; note "
                "excludeTools would take precedence if you set it)",
            ))

        # hooks.SessionStart needs Gemini CLI >= 0.26.0 (GA/enabled-by-default there)
        cur_ss = _get_dotted(data, "hooks.SessionStart")
        wired = isinstance(cur_ss, list) and "onboard-ultramemory.sh" in json.dumps(cur_ss)
        if not wired:
            rc, out = run_cli(["gemini", "--version"])
            ver = _parse_version(out) if rc == 0 else None
            if ver is not None and ver >= (0, 26, 0):
                entry = {
                    "matcher": "startup",
                    "hooks": [{"type": "command", "name": "ultramemory-onboard",
                               "command": _onboard_cmd_abs()}],
                }
                changes.append(ProposedChange(
                    "gemini", path, "hooks.SessionStart", cur_ss, [entry], "onboard-hook",
                    "session-start onboarding (advisory-only: systemMessage + "
                    "additionalContext — Gemini hooks cannot pop a native dialog)",
                ))
            else:
                print(
                    "  [gemini] SessionStart hooks need Gemini CLI >= 0.26.0 (found: %s) "
                    "— skipping the onboarding hook; upgrade and re-run configure"
                    % (".".join(str(v) for v in ver) if ver else "not detected")
                )
        return changes


# ----------------------------------------------------------------- B4: Cursor ----
@register_adapter("cursor")
class CursorAdapter(PlatformAdapter):
    """Cursor IDE + CLI (grounding: platform-cursor.json).

    Three files, three parsers: ~/.cursor/cli-config.json is PURE JSON (CLI-managed
    ``model`` object is UNDOCUMENTED — never written; model goes to the onboarding
    prompt); ~/.cursor/permissions.json is JSONC (defining mcpAllowlist REPLACES the
    in-app allowlist editor — disclosed in the consent copy; NEVER write an empty
    array); ~/.cursor/hooks.json carries the sessionStart hook (version must stay 1)."""

    CLI_CONFIG = "~/.cursor/cli-config.json"
    PERMISSIONS = "~/.cursor/permissions.json"
    HOOKS = "~/.cursor/hooks.json"

    def detect(self) -> bool:
        return _detect_dir_or_cli(["~/.cursor"], ["cursor-agent"])

    def plan(
        self,
        model: Optional[str] = None,
        items: Optional[List[str]] = None,
    ) -> List[ProposedChange]:
        tokens = [t.lower() for t in (items or [])]
        changes: List[ProposedChange] = []

        cli_path = os.path.expanduser(self.CLI_CONFIG)
        cli_data = json_read(cli_path)
        if not os.path.exists(cli_path):
            # grounded minimal-create shape includes version 1 (safe_merge_method)
            changes.append(ProposedChange(
                "cursor", cli_path, "version", ABSENT, 1, "persist",
                "create ~/.cursor/cli-config.json with the documented version field",
            ))
        # "unrestricted" (zero prompts) ONLY with explicit --items unrestricted
        mode = "unrestricted" if "unrestricted" in tokens else "auto-review"
        if cli_data.get("approvalMode") != mode:
            changes.append(ProposedChange(
                "cursor", cli_path, "approvalMode",
                cli_data["approvalMode"] if "approvalMode" in cli_data else ABSENT, mode, "persist",
                'CLI approval mode ("auto-review" = classifier-gated middle ground; '
                '"unrestricted" = zero prompts, only via --items unrestricted)',
            ))
        if cli_data.get("maxMode") is not True:
            changes.append(ProposedChange(
                "cursor", cli_path, "maxMode",
                cli_data["maxMode"] if "maxMode" in cli_data else ABSENT, True, "persist",
                "persist Max Mode for the CLI model picker (the IDE model/Max Mode "
                "live in internal state — the onboarding prompt covers those; the "
                "CLI-managed `model` object is undocumented and never written)",
            ))

        perm_path = os.path.expanduser(self.PERMISSIONS)
        perm_data = jsonc_read(perm_path)
        entries = ["ultramemory:%s" % t for t in ULTRAMEMORY_TOOLS]
        cur_allow = perm_data.get("mcpAllowlist") if "mcpAllowlist" in perm_data else ABSENT
        have = cur_allow if isinstance(cur_allow, list) else []
        if any(e not in have for e in entries):
            changes.append(ProposedChange(
                "cursor", perm_path, "mcpAllowlist", cur_allow, entries, "persist",
                "IDE allowlist for exactly the 8 UltraMemory tools. DISCLOSURE: while "
                "permissions.json defines mcpAllowlist it REPLACES Cursor's in-app "
                "allowlist editor (read-only in-app) — decline this item to keep "
                "in-app management. Takes effect once a Run Mode is enabled",
            ))

        hooks_path = os.path.expanduser(self.HOOKS)
        hooks_data = json_read(hooks_path)
        if not os.path.exists(hooks_path):
            changes.append(ProposedChange(
                "cursor", hooks_path, "version", ABSENT, 1, "persist",
                "create ~/.cursor/hooks.json with the required version: 1",
            ))
        cur_ss = _get_dotted(hooks_data, "hooks.sessionStart")
        wired = isinstance(cur_ss, list) and "onboard-ultramemory.sh" in json.dumps(cur_ss)
        if not wired:
            entry = {"command": _onboard_cmd_abs()}
            changes.append(ProposedChange(
                "cursor", hooks_path, "hooks.sessionStart", cur_ss, [entry], "onboard-hook",
                "sessionStart onboarding hook (fires in the IDE AND cursor-agent CLI; "
                "covers the IDE-only model picker / Max Mode / Run Mode choices)",
            ))
        return changes

    def apply(
        self,
        changes: List[ProposedChange],
        backed_up: Optional[Set[str]] = None,
    ) -> List[dict]:
        if backed_up is None:
            backed_up = set()
        perm_path = os.path.expanduser(self.PERMISSIONS)
        rows: List[dict] = []
        for ch in changes:
            # permissions.json is JSONC despite .json (grounded) — force the JSONC
            # writer so user comments always survive; the other two files are pure JSON.
            fmt = "jsonc" if ch.file == perm_path else None
            rows.append(apply_change(ch, backed_up=backed_up, fmt=fmt))
        return rows


# ----------------------------------------------------------------- B5: Hermes ----
@register_adapter("hermes")
class HermesAdapter(PlatformAdapter):
    """Hermes Agent (grounding: platform-hermes.json).

    config.yaml is merged ONLY via Hermes's own CLI (``hermes config set`` — leaf-merge
    safe; no hand-rolled YAML surgery). $HERMES_HOME resolved from the env, NEVER a
    hardcoded ~/.hermes (get_hermes_home semantics — the 1.9.7 PosixPath lesson).
    approvals.mode stays UNTOUCHED by default (smart is sane); "off" only explicit.
    Session-start onboarding rides the EXISTING provider plugin (register() in
    __init__.py registers on_session_start) — no config write needed for it."""

    def detect(self) -> bool:
        hh = os.environ.get("HERMES_HOME")
        if hh and os.path.isdir(hh):
            return True
        return _detect_dir_or_cli(["~/.hermes"], ["hermes"])

    @staticmethod
    def _home() -> str:
        # HERMES_HOME env var overrides; ~/.hermes is the documented fallback
        return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")

    def _config(self) -> str:
        return os.path.join(self._home(), "config.yaml")

    def plan(
        self,
        model: Optional[str] = None,
        items: Optional[List[str]] = None,
    ) -> List[ProposedChange]:
        tokens = [t.lower() for t in (items or [])]
        if shutil.which("hermes") is None:
            raise EngineError(
                "hermes CLI not found — `hermes config set` is the only sanctioned "
                "merge path for config.yaml (leaf-merge safe; no hand-rolled YAML)"
            )
        cfg = self._config()
        changes: List[ProposedChange] = []

        rc, out = run_cli(["hermes", "config", "get", "agent.reasoning_effort"])
        cur_effort: Any = out.strip() if rc == 0 and out.strip() else ABSENT
        if cur_effort != "xhigh":
            changes.append(ProposedChange(
                "hermes", cfg, "agent.reasoning_effort", cur_effort, "xhigh", "persist",
                "durable global max reasoning effort (no session re-arming needed). "
                "Honesty note: Hermes MCP tools bypass the approval system today "
                "(hermes-agent #16462/#32877), so 'pre-approve UltraMemory tools' is a "
                "no-op here — recall already never stalls on approval prompts",
            ))

        if model:
            rc, out = run_cli(["hermes", "config", "get", "model.default"])
            cur_model: Any = out.strip() if rc == 0 and out.strip() else ABSENT
            if cur_model != model:
                changes.append(ProposedChange(
                    "hermes", cfg, "model.default", cur_model, model, "persist",
                    "model pin (--model given; on managed fleets /etc/hermes/config.yaml "
                    "leaf-merges OVER this and silently wins — disclosed, not fought)",
                ))

        # approvals.mode UNTOUCHED by default — "off" (== --yolo) ONLY with --items off
        if "off" in tokens:
            rc, out = run_cli(["hermes", "config", "get", "approvals.mode"])
            cur_mode: Any = out.strip() if rc == 0 and out.strip() else ABSENT
            if cur_mode != "off":
                changes.append(ProposedChange(
                    "hermes", cfg, "approvals.mode", cur_mode, "off", "persist",
                    'approvals OFF (== --yolo; docs: trusted environments ONLY — the '
                    'default "smart" mode is already low-friction and stays untouched '
                    "unless you explicitly opt in with --items off)",
                ))
        return changes

    def apply(
        self,
        changes: List[ProposedChange],
        backed_up: Optional[Set[str]] = None,
    ) -> List[dict]:
        if backed_up is None:
            backed_up = set()
        cfg = self._config()
        rows: List[dict] = []
        for ch in changes:
            prior = yaml_cli_set(
                cfg,
                ["hermes", "config", "set", ch.key_path, str(ch.new_value)],
                get_argv=["hermes", "config", "get", ch.key_path],
                backed_up=backed_up,
            )
            rows.append(setting_row(ch, prior, ch.new_value))
        return rows

    def restore(self, rows: List[dict]) -> List[dict]:
        """`hermes config set` back to the recorded prior (item B5 rollback). Rows with
        no recorded prior are kept — Hermes has no grounded unset verb."""
        backed_up: Set[str] = set()
        cfg = self._config()
        kept: List[dict] = []
        for row in reversed(rows):
            key = str(row.get("key") or "")
            absent, prior = _unwrap(row.get("prior"))
            if not key or absent:
                print(
                    "  ! hermes: no recorded prior for %s — left as-is (revert manually "
                    "with `hermes config set %s <value>`)" % (key or "<row>", key)
                )
                kept.append(row)
                continue
            try:
                yaml_cli_set(cfg, ["hermes", "config", "set", key, str(prior)], backed_up=backed_up)
                print("  restored %s :: %s" % (cfg, key))
            except EngineError as e:
                print("  ! restore failed for %s :: %s — %s" % (cfg, key, e))
                kept.append(row)
        kept.reverse()
        return kept


# ------------------------------------------------------------------ C1: Cline ----
#: TaskStart onboarding hook (grounded: platform-cline.json — executable, extensionless,
#: returns {"cancel": false, "contextModification": ...}; no persisted thinking key
#: exists, so the prompt suggests the session-only `--thinking xhigh` alias).
_CLINE_TASKSTART = """#!/bin/sh
# UltraMemory — Cline TaskStart onboarding (installed by `ultramemory configure`).
# Cline persists NO thinking-level key (grounding: platform-cline.json), so maximum
# reasoning is per-run only — this hook suggests the session-only `--thinking xhigh`
# alias at task start. Opt out: touch ~/.ultramemory/onboard-optout
[ -f "${HOME:-}/.ultramemory/onboard-optout" ] && exit 0
cat >/dev/null 2>&1 || true
cat <<'JSON'
{"cancel": false, "contextModification": "UltraMemory onboarding: Cline has no persisted thinking-level setting, so maximum reasoning is session-only. Suggest to the user, once: relaunch as `cline --thinking xhigh` (make it stick with a shell alias: alias cline='cline --thinking xhigh'). To stop these prompts: touch ~/.ultramemory/onboard-optout"}
JSON
exit 0
"""


@register_adapter("cline")
class ClineAdapter(PlatformAdapter):
    """Cline CLI (grounding: platform-cline.json). CLI scope ONLY — the VS Code
    extension's model/auto-approve/YOLO state lives in VS Code GlobalState SQLite
    (state.vscdb, no declarative interface — GH #3796): DECLINED, never touched.
    All writes are parse->modify->stringify JSON (the #1 corruption precedent is
    text-patching this very file — GH #9663)."""

    def detect(self) -> bool:
        return _detect_dir_or_cli(["~/.cline"], ["cline"])

    @staticmethod
    def _mcp_path() -> str:
        """The real CLI MCP config path. Docs disagree (~/.cline/mcp.json vs
        data/settings/cline_mcp_settings.json) — ask `cline config mcp --json` first
        (live-probed 2026-07-21: it prints the SERVER LIST, not a path, so the probe
        only helps if a future CLI returns a path field), then probe the grounded
        candidates; CLINE_DATA_DIR relocates the data dir (the sandbox mechanism)."""
        rc, out = run_cli(["cline", "config", "mcp", "--json"])
        if rc == 0 and out.strip():
            try:
                probe = json.loads(out)
                if isinstance(probe, dict):
                    for k in ("path", "configPath", "file"):
                        v = probe.get(k)
                        if isinstance(v, str) and v.strip():
                            return os.path.expanduser(v)
            except ValueError:
                pass
        data_dir = os.environ.get("CLINE_DATA_DIR")
        if data_dir:
            return os.path.join(os.path.expanduser(data_dir), "settings", "cline_mcp_settings.json")
        for cand in ("~/.cline/data/settings/cline_mcp_settings.json", "~/.cline/mcp.json"):
            p = os.path.expanduser(cand)
            if os.path.exists(p):
                return p
        return os.path.expanduser("~/.cline/data/settings/cline_mcp_settings.json")

    @staticmethod
    def _hooks_dir() -> str:
        return os.environ.get("CLINE_HOOKS_DIR") or os.path.expanduser("~/.cline/hooks")

    def plan(
        self,
        model: Optional[str] = None,
        items: Optional[List[str]] = None,
    ) -> List[ProposedChange]:
        changes: List[ProposedChange] = []
        mcp_path = self._mcp_path()
        data = json_read(mcp_path)
        servers = data.get("mcpServers")
        if isinstance(servers, dict) and "ultramemory" in servers:
            server = servers["ultramemory"] if isinstance(servers["ultramemory"], dict) else {}
            cur_type = server.get("type", ABSENT) if "type" in server else ABSENT
            if cur_type != "streamableHttp":
                changes.append(ProposedChange(
                    "cline", mcp_path, "mcpServers.ultramemory.type", cur_type,
                    "streamableHttp", "persist",
                    '"type": "streamableHttp" made explicit — omitting it silently '
                    "downgrades the connection to legacy SSE (grounded)",
                ))
            cur_aa = server.get("autoApprove", ABSENT) if "autoApprove" in server else ABSENT
            have = cur_aa if isinstance(cur_aa, list) else []
            if any(t not in have for t in ULTRAMEMORY_TOOLS):
                changes.append(ProposedChange(
                    "cline", mcp_path, "mcpServers.ultramemory.autoApprove", cur_aa,
                    list(ULTRAMEMORY_TOOLS), "persist",
                    "auto-approve exactly the 8 UltraMemory tools. DISCLOSURE: per-tool "
                    "autoApprove is honored only while the blanket 'Use MCP servers' "
                    "toggle is OFF (cline #9357); CLI headless runs auto-approve by "
                    "default anyway (disclosed, not just exploited)",
                ))
        else:
            print(
                "  [cline] ultramemory not registered in %s — register it first "
                "(cline mcp install / ultramemory kit), then re-run configure" % mcp_path
            )

        hook = os.path.join(self._hooks_dir(), "TaskStart")
        if not os.path.exists(hook):
            changes.append(ProposedChange(
                "cline", hook, "hook-file", ABSENT, "install TaskStart onboarding hook",
                "onboard-hook",
                "TaskStart onboarding hook (executable, extensionless — Cline has no "
                "persisted thinking key, so it suggests the session-only "
                "`--thinking xhigh` alias; extension state in SQLite GlobalState is "
                "NEVER touched)",
            ))
        return changes

    def apply(
        self,
        changes: List[ProposedChange],
        backed_up: Optional[Set[str]] = None,
    ) -> List[dict]:
        if backed_up is None:
            backed_up = set()
        rows: List[dict] = []
        for ch in changes:
            if ch.key_path == "hook-file":
                backup_file(ch.file, backed_up)  # safety: plan only proposes when absent
                _write_executable(ch.file, _CLINE_TASKSTART)
                rows.append(setting_row(ch, ABSENT, ch.new_value))
                continue
            rows.append(apply_change(ch, backed_up=backed_up))
        return rows

    def restore(self, rows: List[dict]) -> List[dict]:
        """Item C1 rollback: A3 restore for the JSON rows + delete the hook file."""
        backed_up: Set[str] = set()
        kept: List[dict] = []
        for row in reversed(rows):
            if str(row.get("key") or "") == "hook-file":
                path = os.path.expanduser(str(row.get("path") or ""))
                try:
                    if path and os.path.exists(path):
                        os.remove(path)
                    print("  removed %s" % path)
                except OSError as e:
                    print("  ! could not remove %s — %s" % (path, e))
                    kept.append(row)
                continue
            if not _restore_row(row, backed_up):
                kept.append(row)
        kept.reverse()
        return kept


# --------------------------------------------------------- C2: VS Code/Copilot ----
@register_adapter("vscode")
class VSCodeAdapter(PlatformAdapter):
    """VS Code + GitHub Copilot agent mode (grounding: platform-vscode-copilot.json).

    User settings.json is JSONC — surgically edited so comments survive. The Experimental
    ``chat.permissions.default`` key is feature-probed via ``code --version`` (>= 1.124,
    the release that added it) and skipped with a warning otherwise — never leave junk
    keys (VS Code renames chat.* keys weekly). The SessionStart hook goes in a NEW
    standalone file ~/.copilot/hooks/ultramemory.json (folder loads all *.json — zero
    merge). Per-tool 'always allow' grants live in internal storage (not file-seedable)
    — the onboarding prompt covers them, and Thinking Effort (picker-only, no settings
    key) too."""

    HOOK_FILE = "~/.copilot/hooks/ultramemory.json"

    @staticmethod
    def _settings_path() -> str:
        if sys.platform == "darwin":
            return os.path.expanduser("~/Library/Application Support/Code/User/settings.json")
        if os.name == "nt":  # pragma: no cover - windows path, grounded
            base = os.environ.get("APPDATA") or os.path.expanduser("~")
            return os.path.join(base, "Code", "User", "settings.json")
        return os.path.expanduser("~/.config/Code/User/settings.json")

    def detect(self) -> bool:
        return os.path.isdir(os.path.dirname(self._settings_path())) or bool(shutil.which("code"))

    def plan(
        self,
        model: Optional[str] = None,
        items: Optional[List[str]] = None,
    ) -> List[ProposedChange]:
        path = self._settings_path()
        data = jsonc_read(path)
        changes: List[ProposedChange] = []

        want_model = model or "opus"  # family alias — resilient (exact-ID keys are
        # silently ignored on mismatch, vscode#319131; grounded recommendation)
        cur = data.get("chat.defaultModel", ABSENT) if "chat.defaultModel" in data else ABSENT
        if cur != want_model:
            changes.append(ProposedChange(
                "vscode", path, "chat.defaultModel", cur, want_model, "persist",
                'default chat model family ("opus" resolves to the newest in family; '
                'use "auto" if your Copilot plan lacks it)',
            ))

        # Experimental key — feature-probe before proposing; warn+skip, never junk
        cur_perm = data.get("chat.permissions.default", ABSENT) if "chat.permissions.default" in data else ABSENT
        if cur_perm != "autoApprove":
            rc, out = run_cli(["code", "--version"])
            ver = _parse_version(out) if rc == 0 else None
            if ver is not None and ver >= (1, 124, 0):
                changes.append(ProposedChange(
                    "vscode", path, "chat.permissions.default", cur_perm, "autoApprove", "persist",
                    'Bypass Approvals for new chat sessions (EXPERIMENTAL key, added '
                    "1.124 — enterprise policy can clamp it back to Default Approvals)",
                ))
            else:
                print(
                    "  [vscode] `code --version` unavailable or < 1.124 (found: %s) — "
                    "skipping the Experimental chat.permissions.default key (never "
                    "leave junk keys)"
                    % (".".join(str(v) for v in ver) if ver else "not detected")
                )

        cur_hooks = data.get("chat.hooks.enabled", ABSENT) if "chat.hooks.enabled" in data else ABSENT
        if cur_hooks is not True:
            changes.append(ProposedChange(
                "vscode", path, "chat.hooks.enabled", cur_hooks, True, "persist",
                "enable agent hooks (Preview, 1.109+) so the SessionStart onboarding "
                "file is loaded",
            ))

        hook_path = os.path.expanduser(self.HOOK_FILE)
        hook_data = json_read(hook_path)
        cur_ss = _get_dotted(hook_data, "hooks.SessionStart")
        wired = isinstance(cur_ss, list) and "onboard-ultramemory.sh" in json.dumps(cur_ss)
        if not wired:
            # A1: Copilot CLI honors ONLY top-level additionalContext (not the Claude
            # hookSpecificOutput envelope) — ULTRAMEMORY_HOOK_SHAPE=copilot makes the kit
            # hooks emit that shape (recall-first-hook.sh shape switch).
            entry = {"hooks": [{"type": "command",
                                "command": "ULTRAMEMORY_HOOK_SHAPE=copilot " + _onboard_cmd_abs()}]}
            changes.append(ProposedChange(
                "vscode", hook_path, "hooks.SessionStart", cur_ss, [entry], "onboard-hook",
                "SessionStart onboarding in a standalone ~/.copilot/hooks/ file (zero "
                "merge; covers picker-only Thinking Effort and per-tool approvals — "
                "neither is file-persistable)",
            ))
        return changes

    def apply(
        self,
        changes: List[ProposedChange],
        backed_up: Optional[Set[str]] = None,
    ) -> List[dict]:
        if backed_up is None:
            backed_up = set()
        settings = self._settings_path()
        rows: List[dict] = []
        for ch in changes:
            if ch.file == settings:
                # settings.json is JSONC AND its keys are FLAT dotted strings
                # ("chat.defaultModel" is ONE member, not nested) — literal-key
                # surgical JSONC write so comments survive and no nested junk appears.
                prior, final = jsonc_set_file(
                    ch.file, ch.key_path, ch.new_value, backed_up, literal_key=True
                )
                rows.append(setting_row(ch, prior, final))
                continue
            rows.append(apply_change(ch, backed_up=backed_up))
        return rows

    def restore(self, rows: List[dict]) -> List[dict]:
        """Flat-key JSONC restore for settings.json rows (the generic path would treat
        "chat.defaultModel" as nested); everything else restores generically."""
        backed_up: Set[str] = set()
        settings = self._settings_path()
        kept: List[dict] = []
        for row in reversed(rows):
            path = os.path.expanduser(str(row.get("path") or ""))
            key = str(row.get("key") or "")
            if path == settings and key:
                absent, prior = _unwrap(row.get("prior"))
                try:
                    if not os.path.exists(path):
                        print("  ! %s no longer exists — nothing to revert for %s" % (path, key))
                    elif absent:
                        jsonc_delete_file(path, key, backed_up, literal_key=True)
                        print("  restored %s :: %s" % (path, key))
                    else:
                        jsonc_set_file(path, key, prior, backed_up, merge_lists=False, literal_key=True)
                        print("  restored %s :: %s" % (path, key))
                except EngineError as e:
                    print("  ! restore failed for %s :: %s — %s" % (path, key, e))
                    kept.append(row)
                continue
            if not _restore_row(row, backed_up):
                kept.append(row)
        kept.reverse()
        return kept


# --------------------------------------------------------------- C3: OpenClaw ----
#: HOOK.md + handler for the native discovery-based hooks system (format verified
#: against the installed openclaw 2026.7.1-2 docs/automation/hooks.md and live-probed
#: sandboxed 2026-07-21: discovered by `openclaw hooks list`, enabled cleanly).
_OPENCLAW_HOOK_MD = """---
name: ultramemory-onboard
description: "UltraMemory session-start onboarding — one-line optional-tuning nudge on /new and /reset"
metadata:
  { "openclaw": { "emoji": "🧠", "events": ["command:new", "command:reset"] } }
---

# UltraMemory onboarding

Posts a one-line reply at session start (`/new` / `/reset`) pointing at
`ultramemory configure --platform openclaw` (explicit per-item consent, undoable).
Silent when `~/.ultramemory/onboard-optout` exists. Changes made by configure take
effect after `openclaw gateway restart` — run it when convenient (configure NEVER
auto-restarts the gateway).
"""

_OPENCLAW_HANDLER_JS = """// UltraMemory — OpenClaw session-start onboarding (installed by `ultramemory configure`).
// Replies once per /new or /reset with the optional-tuning line; silent when the
// opt-out file exists. Never throws — an onboarding nudge must not break a session.
import { existsSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

const handler = async (event) => {
  if (event.type !== "command" || (event.action !== "new" && event.action !== "reset")) {
    return;
  }
  try {
    if (existsSync(join(homedir(), ".ultramemory", "onboard-optout"))) {
      return;
    }
  } catch {
    return;
  }
  event.messages.push(
    "UltraMemory: optional one-time tuning available — run `ultramemory configure " +
      "--platform openclaw` (per-item consent, undoable). Config changes take effect " +
      "after `openclaw gateway restart` — run it when convenient. " +
      "Opt out: touch ~/.ultramemory/onboard-optout",
  );
};

export default handler;
"""


@register_adapter("openclaw")
class OpenClawAdapter(PlatformAdapter):
    """OpenClaw gateway (grounding: platform-openclaw.json).

    openclaw.json is JSON5 (comments + trailing commas) — NEVER parsed/rewritten
    directly; every merge shells out to ``openclaw config set/get/unset`` (the
    sanctioned key-by-key path). Exec permissions are UNTOUCHED — grounded: MCP calls
    never prompt and the unsandboxed default is already security=full/ask=off ("don't
    fix friction that isn't there"). The gateway is NEVER auto-restarted (founder-gated
    hard precedent 2026-07-21) — every consent copy says so."""

    HOOK_NAME = "ultramemory-onboard"

    def detect(self) -> bool:
        state = os.environ.get("OPENCLAW_STATE_DIR")
        if state and os.path.isdir(state):
            return True
        return _detect_dir_or_cli(["~/.openclaw"], ["openclaw"])

    @staticmethod
    def _state_dir() -> str:
        # OPENCLAW_STATE_DIR replaces ~/.openclaw entirely (independent trust scope)
        return os.environ.get("OPENCLAW_STATE_DIR") or os.path.expanduser("~/.openclaw")

    def _config(self) -> str:
        return os.path.join(self._state_dir(), "openclaw.json")

    def _hook_dir(self) -> str:
        return os.path.join(self._state_dir(), "hooks", self.HOOK_NAME)

    def plan(
        self,
        model: Optional[str] = None,
        items: Optional[List[str]] = None,
    ) -> List[ProposedChange]:
        if shutil.which("openclaw") is None:
            raise EngineError(
                "openclaw CLI not found — `openclaw config set` is the only sanctioned "
                "merge path (openclaw.json is JSON5 and is never parsed directly)"
            )
        cfg = self._config()
        changes: List[ProposedChange] = []
        restart_note = (
            "takes effect after `openclaw gateway restart` — run it when convenient "
            "(configure NEVER restarts the gateway)"
        )

        rc, out = run_cli(["openclaw", "config", "get", "agents.defaults.thinkingDefault"])
        cur_think: Any = out.strip() if rc == 0 and out.strip() else ABSENT
        if cur_think != "xhigh":
            changes.append(ProposedChange(
                "openclaw", cfg, "agents.defaults.thinkingDefault", cur_think, "xhigh", "persist",
                "persistable max thinking (value is provider/model-gated — validation "
                "rejects it where unsupported); exec permissions stay UNTOUCHED "
                "(MCP calls never prompt; unsandboxed default is already full); " + restart_note,
            ))

        if model:
            rc, out = run_cli(["openclaw", "config", "get", "agents.defaults.model.primary"])
            cur_model: Any = out.strip() if rc == 0 and out.strip() else ABSENT
            if cur_model != model:
                changes.append(ProposedChange(
                    "openclaw", cfg, "agents.defaults.model.primary", cur_model, model, "persist",
                    "primary model (--model given; must resolve to a provider you have "
                    "an auth profile for); " + restart_note,
                ))

        hook_dir = self._hook_dir()
        if not os.path.exists(os.path.join(hook_dir, "HOOK.md")):
            changes.append(ProposedChange(
                "openclaw", hook_dir, "hook-dir", ABSENT,
                "install + `openclaw hooks enable %s`" % self.HOOK_NAME, "onboard-hook",
                "session-start onboarding hook (native hooks dir: HOOK.md + handler; "
                "replies on /new//reset; hooks are lazy-loaded so it is enabled via "
                "`openclaw hooks enable`); " + restart_note,
            ))
        return changes

    def apply(
        self,
        changes: List[ProposedChange],
        backed_up: Optional[Set[str]] = None,
    ) -> List[dict]:
        if backed_up is None:
            backed_up = set()
        cfg = self._config()
        rows: List[dict] = []
        for ch in changes:
            if ch.key_path == "hook-dir":
                os.makedirs(ch.file, exist_ok=True)
                _atomic_write_text(os.path.join(ch.file, "HOOK.md"), _OPENCLAW_HOOK_MD)
                _atomic_write_text(os.path.join(ch.file, "handler.js"), _OPENCLAW_HANDLER_JS)
                backup_file(cfg, backed_up)  # `hooks enable` rewrites openclaw.json via the CLI
                rc, _out = run_cli(["openclaw", "hooks", "enable", self.HOOK_NAME])
                if rc != 0:
                    print(
                        "  ! hook files installed at %s but `openclaw hooks enable %s` "
                        "exited %d — run it yourself when convenient"
                        % (ch.file, self.HOOK_NAME, rc)
                    )
                rows.append(setting_row(ch, ABSENT, ch.new_value))
                continue
            prior = yaml_cli_set(
                cfg,
                ["openclaw", "config", "set", ch.key_path, str(ch.new_value)],
                get_argv=["openclaw", "config", "get", ch.key_path],
                backed_up=backed_up,
            )
            rows.append(setting_row(ch, prior, ch.new_value))
        return rows

    def restore(self, rows: List[dict]) -> List[dict]:
        """Item C3 rollback: `openclaw config set` prior (or `config unset` when the
        key did not exist — grounded verb) + `openclaw hooks disable` + remove the
        hook dir. Never touches openclaw.json directly; never restarts the gateway."""
        backed_up: Set[str] = set()
        cfg = self._config()
        kept: List[dict] = []
        for row in reversed(rows):
            key = str(row.get("key") or "")
            if key == "hook-dir":
                run_cli(["openclaw", "hooks", "disable", self.HOOK_NAME])
                d = os.path.expanduser(str(row.get("path") or ""))
                if d and os.path.isdir(d):
                    shutil.rmtree(d, ignore_errors=True)
                print("  removed openclaw hook %s (disabled first)" % d)
                continue
            absent, prior = _unwrap(row.get("prior"))
            argv = (
                ["openclaw", "config", "unset", key]
                if absent
                else ["openclaw", "config", "set", key, str(prior)]
            )
            backup_file(cfg, backed_up)
            rc, _out = run_cli(argv)
            if rc != 0:
                print("  ! `%s` exited %d — row kept in the manifest" % (" ".join(argv), rc))
                kept.append(row)
                continue
            print("  restored %s :: %s" % (cfg, key))
        kept.reverse()
        return kept


# ---------------------------------------------------------- C4: Windsurf/Devin ----
@register_adapter("windsurf")
class WindsurfDevinAdapter(PlatformAdapter):
    """Windsurf -> Devin Desktop / Devin CLI (grounding: platform-windsurf-devin.json).

    Targets ONLY the current config system: ~/.config/devin/config.json (JSONC-tolerant
    parse — the grounded gotcha; plain json.load can fail on comments). The legacy
    Cascade-era ~/.codeium/* tree is left UNTOUCHED (Cascade EOL 2026-07-01; two
    parallel config systems — only the Devin side is current). The session-only
    reasoning level (Alt+T / macOS Opt+T) has NO persistable key — the SessionStart
    onboarding covers it."""

    CONFIG = "~/.config/devin/config.json"

    def detect(self) -> bool:
        return _detect_dir_or_cli(["~/.config/devin"], ["devin"])

    def plan(
        self,
        model: Optional[str] = None,
        items: Optional[List[str]] = None,
    ) -> List[ProposedChange]:
        path = os.path.expanduser(self.CONFIG)
        data = jsonc_read(path)
        changes: List[ProposedChange] = []

        want_model = model or "opus"  # family short name resolves to the latest (grounded)
        cur = _get_dotted(data, "agent.model")
        if cur != want_model:
            changes.append(ProposedChange(
                "windsurf", path, "agent.model", cur, want_model, "persist",
                'default model family ("opus" resolves to the newest in family; USER '
                "config only — project configs do not honor agent.model)",
            ))

        rules = ["mcp__ultramemory__%s" % t for t in ULTRAMEMORY_TOOLS]
        cur_allow = _get_dotted(data, "permissions.allow")
        have = cur_allow if isinstance(cur_allow, list) else []
        if any(r not in have for r in rules):
            changes.append(ProposedChange(
                "windsurf", path, "permissions.allow", cur_allow, rules, "persist",
                "pre-approve ONLY the 8 UltraMemory MCP tools (Devin Local prompts "
                "before EVERY MCP call by default — without this, recall stalls on "
                "first use; your deny/ask entries are never touched)",
            ))

        cur_ss = _get_dotted(data, "hooks.SessionStart")
        wired = isinstance(cur_ss, list) and "onboard-ultramemory.sh" in json.dumps(cur_ss)
        if not wired:
            entry = {"hooks": [{"type": "command", "command": _onboard_cmd_abs()}]}
            changes.append(ProposedChange(
                "windsurf", path, "hooks.SessionStart", cur_ss, [entry], "onboard-hook",
                "SessionStart onboarding (Claude-compatible format; covers the "
                "session-only reasoning level — Alt+T, macOS Opt+T — which has no "
                "persistable key; legacy ~/.codeium/* stays untouched)",
            ))
        return changes

    def apply(
        self,
        changes: List[ProposedChange],
        backed_up: Optional[Set[str]] = None,
    ) -> List[dict]:
        if backed_up is None:
            backed_up = set()
        # config.json is JSONC-tolerant (grounded) — force the surgical JSONC writer
        # so user comments survive even when the file currently parses as strict JSON.
        return [apply_change(ch, backed_up=backed_up, fmt="jsonc") for ch in changes]
