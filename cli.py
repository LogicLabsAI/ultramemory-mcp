"""``ultramemory`` console entry point.

A tiny, dependency-light installer that wires the UltraMemory memory provider into a Hermes
install without the interactive ``hermes memory setup`` wizard:

    ultramemory enable --key um_live_…

What ``enable`` does (idempotent, all writes atomic, secret kept out of world-readable files):

  1. writes ``ULTRAMEMORY_API_KEY=<key>`` into ``$HERMES_HOME/.env`` (chmod 600) — the secret
     never lands in the non-secret JSON, matching the provider's own contract;
  2. delegates the *non-secret* options (base_url / gated / auto_capture / recall_k) to the
     provider's existing ``save_config`` so there's a single source of truth for that file;
  3. sets ``memory.provider: ultramemory`` in the Hermes config (``$HERMES_HOME/config.yaml``,
     falling back to ``~/.hermes/config.yaml``) so the provider is actually selected;
  4. ejects the client-side token-economics cache skeleton at ``~/.ultramemory/cache.json``
     (the user-editable file shared by the provider and the Claude Code recall hook) — an
     existing cache.json is NEVER clobbered.

Provider behaviour itself is untouched — this module only orchestrates the host-side wiring the
provider can't do from inside a hook.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Optional

try:
    from . import DEFAULT_BASE_URL, UltraMemoryProvider
except ImportError:  # run as a flat script (dev / CI gate) rather than the installed package
    DEFAULT_BASE_URL = "https://api.ultramemory.us"
    UltraMemoryProvider = None  # only `enable` needs it; imported lazily there

__all__ = ["main"]


def _hermes_home() -> str:
    """Resolve $HERMES_HOME the same way the provider does, so the CLI and the running provider
    agree on where config lives."""
    try:  # prefer Hermes' own resolver when it's importable
        from hermes_constants import get_hermes_home

        return os.fspath(get_hermes_home())  # str even when the resolver yields a pathlib.Path
    except Exception:
        return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def _upsert_env(env_path: str, key: str, value: str) -> None:
    """Idempotently set ``KEY=value`` in a ``.env`` file: replace an existing assignment in place,
    else append. Written atomically and chmod 600 (it holds a bearer secret)."""
    os.makedirs(os.path.dirname(env_path) or ".", exist_ok=True)
    lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    assign = f"{key}={value}"
    pat = re.compile(rf"^\s*(export\s+)?{re.escape(key)}\s*=")
    replaced = False
    for i, line in enumerate(lines):
        if pat.match(line):
            lines[i] = assign
            replaced = True
            break
    if not replaced:
        lines.append(assign)
    tmp = env_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, env_path)
    try:
        os.chmod(env_path, 0o600)
    except Exception:
        pass


def _set_memory_provider(hermes_home: str, provider: str = "ultramemory") -> str:
    """Set ``memory.provider: <provider>`` in the Hermes config file, atomically.

    Uses PyYAML when it's importable (round-trips the whole document); otherwise falls back to a
    minimal, surgical text edit that only touches the ``memory.provider`` key — so we never need a
    YAML dependency just to flip one setting. Returns the config path written.
    """
    path = os.path.join(hermes_home, "config.yaml")
    if not os.path.exists(path):
        alt = os.path.expanduser("~/.hermes/config.yaml")
        if os.path.exists(alt):
            path = alt
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    try:
        import yaml  # type: ignore

        data = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
            if isinstance(loaded, dict):
                data = loaded
        mem = data.get("memory")
        if not isinstance(mem, dict):
            mem = {}
        mem["provider"] = provider
        data["memory"] = mem
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp, path)
        return path
    except ImportError:
        pass  # fall through to the no-dependency text edit

    # --- no-PyYAML fallback: surgically set memory.provider without reformatting the file ---
    text = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    lines = text.splitlines()

    mem_idx: Optional[int] = None
    mem_indent = ""
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)memory\s*:\s*$", line)
        if m:
            mem_idx = i
            mem_indent = m.group(1)
            break

    if mem_idx is None:
        # no memory: block at all — append a fresh one
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append("memory:")
        lines.append("  provider: " + provider)
    else:
        child_indent = mem_indent + "  "
        prov_idx: Optional[int] = None
        # scan the children of memory: for an existing provider: line
        j = mem_idx + 1
        while j < len(lines):
            line = lines[j]
            if line.strip() == "":
                j += 1
                continue
            cur_indent = len(line) - len(line.lstrip())
            if cur_indent <= len(mem_indent):
                break  # dedented out of the memory: block
            if re.match(rf"^{re.escape(child_indent)}provider\s*:", line):
                prov_idx = j
                break
            j += 1
        if prov_idx is not None:
            lines[prov_idx] = f"{child_indent}provider: {provider}"
        else:
            lines.insert(mem_idx + 1, f"{child_indent}provider: {provider}")

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")
    os.replace(tmp, path)
    return path


_PROVIDER_SHIM = '''"""UltraMemory memory provider shim for Hermes Agent.

Auto-generated by `ultramemory enable`; do not edit. The real provider ships in
the pip-installed `ultramemory` package (`pip install ultramemory-hermes`).
Hermes discovers memory providers by directory scan (plugins/memory/__init__.py),
so this thin module re-exports the package's register() entry point and provider
class into the scanned $HERMES_HOME/plugins/ dir. The API key is read from
$HERMES_HOME/.env (ULTRAMEMORY_API_KEY), which `ultramemory enable` writes.

Discovery markers (keep — Hermes text-scans for these): MemoryProvider / register_memory_provider
"""
import sys

_SITE = "{SITE_PACKAGES}"  # written by `ultramemory enable`; where the ultramemory package lives
if _SITE and _SITE not in sys.path:
    sys.path.insert(0, _SITE)

from ultramemory import register, UltraMemoryProvider  # noqa: F401,E402

__all__ = ["register", "UltraMemoryProvider"]
'''


def _write_provider_shim(hermes_home: str, name: str = "ultramemory") -> str:
    """Plant the provider shim at $HERMES_HOME/plugins/<name>/ (FLAT — never under
    plugins/memory/, upstream issue #48822). Idempotent, atomic. Returns the dir."""
    import ultramemory as _um
    site = os.path.dirname(os.path.dirname(os.path.abspath(_um.__file__)))
    plugin_dir = os.path.join(hermes_home, "plugins", name)
    os.makedirs(plugin_dir, exist_ok=True)
    path = os.path.join(plugin_dir, "__init__.py")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(_PROVIDER_SHIM.replace("{SITE_PACKAGES}", site))
    os.replace(tmp, path)
    # plugin.yaml: machine-owned — ALWAYS rewritten; version derived, never a literal
    try:
        from importlib.metadata import version as _pkg_version
        ver = _pkg_version("ultramemory-hermes")
    except Exception:
        ver = "0.0.0-source"  # running from a source checkout
    yml = os.path.join(plugin_dir, "plugin.yaml")
    tmp_y = yml + ".tmp"
    with open(tmp_y, "w", encoding="utf-8") as f:
        f.write('name: ultramemory\nversion: %s\ndescription: "UltraMemory — metamemory-gated, self-learning long-term memory."\n' % ver)
    os.replace(tmp_y, yml)
    return plugin_dir


def _write_cache_skeleton() -> str:
    """Eject the user-editable client cache skeleton at ``~/.ultramemory/cache.json``.

    Idempotent: an existing cache.json is NEVER clobbered (it may hold live memo/seen state).
    Matches cache.py's on-disk contract: dir 0700, file 0600, atomic tmp+rename, document
    ``{"version": 1, "memo": {}, "seen": {}}``. Returns the cache path either way.
    """
    directory = os.path.join(os.path.expanduser("~"), ".ultramemory")
    path = os.path.join(directory, "cache.json")
    if os.path.exists(path):
        return path  # idempotent — never clobber live cache state
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)  # makedirs mode is umask-filtered; enforce 0700
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps({"version": 1, "memo": {}, "seen": {}}, separators=(",", ":")))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return path


def _cmd_enable(args: argparse.Namespace) -> int:
    key = (args.key or os.environ.get("ULTRAMEMORY_API_KEY") or "").strip()
    if not key:
        print(
            "error: no API key. Pass --key um_… (get one at https://ultramemory.us) "
            "or set ULTRAMEMORY_API_KEY.",
            file=sys.stderr,
        )
        return 2

    hermes_home = os.fspath(args.hermes_home or _hermes_home()).rstrip("/") or os.fspath(_hermes_home())
    os.makedirs(hermes_home, exist_ok=True)

    # 1) secret -> $HERMES_HOME/.env (never the JSON)
    env_path = os.path.join(hermes_home, ".env")
    _upsert_env(env_path, "ULTRAMEMORY_API_KEY", key)

    # 1b) provider shim -> $HERMES_HOME/plugins/ultramemory/ — Hermes discovers by
    #     DIRECTORY SCAN; Python entry points are NOT consulted.
    shim_dir = _write_provider_shim(hermes_home, "ultramemory")
    # best-effort: can Hermes' interpreter import us? (default topology = separate venv)
    hermes_bin = shutil.which("hermes")
    if hermes_bin:
        try:
            with open(hermes_bin, "r", encoding="utf-8", errors="replace") as f:
                shebang = f.readline().strip()
            hpy = shebang[2:].strip() if shebang.startswith("#!") else ""
            if hpy and os.path.exists(hpy):
                rc = subprocess.run([hpy, "-c", "import ultramemory"], capture_output=True, timeout=10).returncode
                if rc != 0:
                    print("  WARN: Hermes' interpreter cannot import ultramemory —")
                    print("        the shim's sys.path fallback should cover it; if `hermes memory status`")
                    if "pipx" in hpy:  # pipx-managed Hermes -> inject into its venv
                        print("        still shows NOT installed, run: pipx inject hermes-agent ultramemory-hermes")
                    else:  # uv / plain-venv Hermes -> install into ITS interpreter
                        print(f"        still shows NOT installed, run: uv pip install --python {hpy} ultramemory-hermes")
        except Exception:
            pass

    # 2) non-secret options -> reuse the provider's own save_config (single source of truth)
    values: dict[str, str] = {}
    if args.base_url:
        values["base_url"] = args.base_url.rstrip("/")
    if args.gated is not None:
        values["gated"] = "true" if args.gated else "false"
    if args.auto_capture is not None:
        values["auto_capture"] = "true" if args.auto_capture else "false"
    if args.recall_k is not None:
        values["recall_k"] = str(args.recall_k)
    if UltraMemoryProvider is None:
        print("error: run `enable` from the installed package (pip install ultramemory-hermes).", file=sys.stderr)
        return 2
    provider = UltraMemoryProvider()
    if values:
        provider.save_config(values, hermes_home)

    # 3) select the provider in the Hermes config
    cfg_path = _set_memory_provider(hermes_home, provider.name)

    # 4) eject the client-side token-economics cache skeleton (idempotent)
    cache_path = _write_cache_skeleton()

    base = values.get("base_url") or DEFAULT_BASE_URL
    print("UltraMemory enabled.")
    print(f"  api key   -> {env_path} (ULTRAMEMORY_API_KEY, chmod 600)")
    print(f"  shim      -> {shim_dir} (provider shim; Hermes discovers by directory scan)")
    print(f"  provider  -> {cfg_path} (memory.provider: {provider.name})")
    print(f"  base url  -> {base}")
    print(f"  cache     -> {cache_path} (recall memo + per-session dedupe; delete to reset)")
    print(
        "  tunables  -> ULTRAMEMORY_CACHE=off (disable cache) | "
        "ULTRAMEMORY_PREVIEW=off (full prefetch, no preview tier) | "
        "ULTRAMEMORY_HOOK_BUDGET (hook recall chars, default 2000) | "
        "ULTRAMEMORY_MIN_CONFIDENCE (hook inject floor, default low)"
    )
    print("Restart Hermes (or start a new session) to pick up the change.")
    return 0


def _cmd_disable(args: argparse.Namespace) -> int:
    import shutil as _sh
    hermes_home = os.fspath(getattr(args, "hermes_home", None) or _hermes_home()).rstrip("/") or os.fspath(_hermes_home())
    plugin_dir = os.path.join(hermes_home, "plugins", "ultramemory")
    removed = os.path.isdir(plugin_dir)
    if removed:
        _sh.rmtree(plugin_dir, ignore_errors=True)
    try:
        _set_memory_provider(hermes_home, "builtin")   # reuse existing helper; only flips the selection
        reset = True
    except Exception:
        reset = False
    print("UltraMemory disabled — shim removed." if removed else "UltraMemory shim not present.")
    print("memory.provider reset to 'builtin'." if reset else "Set memory.provider back to 'builtin' in config.yaml.")
    print("Note: ULTRAMEMORY_API_KEY remains in $HERMES_HOME/.env — remove it manually if desired.")
    return 0


def _cmd_kit(args: argparse.Namespace) -> int:
    """Run the UltraMemory Agent Kit installer / uninstaller / exporter.

    ``install`` / ``uninstall`` run the shell scripts from a local checkout when present (``uvx``
    from a source tree), else download them from the pinned repo (the
    ``uvx --from ultramemory-hermes ultramemory`` path — the entry point is ``ultramemory``).
    ``export`` / ``check`` are maintainer tools and require the source checkout.
    """
    import subprocess
    import tempfile
    import urllib.request

    raw = os.environ.get(
        "ULTRAMEMORY_KIT_RAW",
        "https://raw.githubusercontent.com/LogicLabsAI/ultramemory-mcp/main",
    )
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [here, os.path.dirname(here), os.getcwd()]

    def _local(rel: str) -> Optional[str]:
        for base in candidates:
            p = os.path.join(base, rel)
            if os.path.isfile(p):
                return p
        return None

    action = args.kit_action
    passthrough = list(getattr(args, "rest", []) or [])

    if action in ("install", "uninstall"):
        name = "install.sh" if action == "install" else "uninstall.sh"
        path = _local(name)
        if path is None:
            data = urllib.request.urlopen(raw + "/" + name, timeout=30).read()  # noqa: S310
            tf = tempfile.NamedTemporaryFile("wb", suffix=".sh", delete=False)
            tf.write(data)
            tf.close()
            path = tf.name
        return subprocess.call(["bash", path, *passthrough])

    # export / check — maintainer tools, need the source tree
    path = _local("scripts/export-kit.sh")
    if path is None:
        print(
            "error: `ultramemory kit " + action + "` needs the ultramemory-mcp source checkout "
            "(it curates ~/.claude into agent-kit/). Clone the repo and run it there.",
            file=sys.stderr,
        )
        return 2
    return subprocess.call(["bash", path, *(["--check"] if action == "check" else [])])


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Diagnose the Tier-2 recall-first install: 7 static checks, plus a live API round-trip
    with ``--probe``. One line per check (ok / WARN / FAIL prefix); exits non-zero if ANY check
    FAILs (WARN alone stays 0) so it's CI-able. Never prints the key value — presence only.
    """
    import shutil
    import time
    from datetime import datetime, timedelta, timezone

    failed = False

    def line(status: str, label: str, msg: str) -> None:
        nonlocal failed
        if status == "FAIL":
            failed = True
        print(f"{status:<4} {label:<12} {msg}")

    def _read_json(path: str) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    settings_json = os.path.join(".claude", "settings.json")
    settings_local = os.path.join(".claude", "settings.local.json")

    # 1) API key — install.sh contract: ./.claude/settings.local.json env, else the environment.
    env_block = _read_json(settings_local).get("env")
    key = ((env_block or {}).get("ULTRAMEMORY_API_KEY") or "").strip()
    key_src = f"{settings_local} env" if key else None
    if not key:
        key = (os.environ.get("ULTRAMEMORY_API_KEY") or "").strip()
        key_src = "process environment" if key else None
    if key:
        line("ok", "api key", f"present via {key_src} (value masked)")
    else:
        line(
            "FAIL",
            "api key",
            f"ULTRAMEMORY_API_KEY not set in {settings_local} (env) or the environment",
        )

    # find the UserPromptSubmit registration first — it names the installed hook path.
    reg_file: Optional[str] = None
    reg_cmd: Optional[str] = None
    reg_timeout = None
    for sf in (settings_json, settings_local):
        if reg_cmd:
            break
        groups = (_read_json(sf).get("hooks") or {}).get("UserPromptSubmit") or []
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            for hk in group.get("hooks") or []:
                if isinstance(hk, dict) and "recall-first-hook.sh" in str(hk.get("command", "")):
                    reg_file, reg_cmd, reg_timeout = sf, str(hk.get("command")), hk.get("timeout")
                    break
            if reg_cmd:
                break

    # 2) hook file exists + executable — resolve the path the way install.sh places/registers it
    #    (./.claude/hooks/recall-first-hook.sh, registered via ${CLAUDE_PROJECT_DIR}).
    hook_path = os.path.join(".claude", "hooks", "recall-first-hook.sh")
    if reg_cmd:
        proj = os.environ.get("CLAUDE_PROJECT_DIR") or "."
        cand = reg_cmd.replace("${CLAUDE_PROJECT_DIR}", proj).replace("$CLAUDE_PROJECT_DIR", proj)
        cand = os.path.expanduser(os.path.expandvars(cand))  # global regs use $HOME/~ (.claude/...)
        if os.path.isfile(cand):
            hook_path = cand
    if not os.path.isfile(hook_path):
        line("FAIL", "hook file", f"missing: {hook_path}")
    elif not os.access(hook_path, os.X_OK):
        line("FAIL", "hook file", f"{hook_path} exists but is not executable (chmod +x it)")
    else:
        line("ok", "hook file", f"{hook_path} (executable)")

    # 3) settings registration + configured timeout (WARN below the 1.9.4 floor of 20).
    #    Project-local registration is preferred; the global ~/.claude/settings.json (same JSON
    #    shape) is consulted only when no local entry exists.
    reg_global = False
    if not reg_cmd:
        home_settings = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
        groups = (_read_json(home_settings).get("hooks") or {}).get("UserPromptSubmit") or []
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            for hk in group.get("hooks") or []:
                if isinstance(hk, dict) and "recall-first-hook.sh" in str(hk.get("command", "")):
                    reg_cmd, reg_timeout, reg_global = str(hk.get("command")), hk.get("timeout"), True
                    break
            if reg_cmd:
                break
    if reg_cmd and reg_global:
        if reg_timeout is None:
            line("ok", "registration", "UserPromptSubmit (global ~/.claude/settings.json, no timeout set — Claude Code default applies)")
        elif isinstance(reg_timeout, (int, float)) and reg_timeout < 20:
            line("WARN", "registration", f"UserPromptSubmit (global ~/.claude/settings.json, timeout {reg_timeout}) < 20 — raise it to 20")
        else:
            line("ok", "registration", f"UserPromptSubmit (global ~/.claude/settings.json, timeout {reg_timeout})")
    elif reg_cmd:
        if reg_timeout is None:
            line("ok", "registration", f"UserPromptSubmit in {reg_file} (no timeout set — Claude Code default applies)")
        elif isinstance(reg_timeout, (int, float)) and reg_timeout < 20:
            line("WARN", "registration", f"UserPromptSubmit in {reg_file}, timeout {reg_timeout} < 20 — raise it to 20")
        else:
            line("ok", "registration", f"UserPromptSubmit in {reg_file}, timeout {reg_timeout}")
    else:
        line(
            "FAIL",
            "registration",
            f"no UserPromptSubmit entry for recall-first-hook.sh in {settings_json}, {settings_local}, or ~/.claude/settings.json",
        )

    # 4) cache.py adjacent to the hook (the hook also accepts the parent dir; without either,
    #    caching quietly disables — degraded, not broken).
    hook_dir = os.path.dirname(hook_path) or "."
    adjacent = os.path.join(hook_dir, "cache.py")
    parent = os.path.join(hook_dir, os.pardir, "cache.py")
    if os.path.isfile(adjacent):
        line("ok", "cache.py", adjacent)
    elif os.path.isfile(parent):
        line("ok", "cache.py", f"{parent} (parent dir — repo-checkout layout)")
    else:
        line("WARN", "cache.py", f"missing next to the hook ({adjacent}) — token-economics caching is disabled")

    # 5) CLAUDE.md recall-rule sentinel (active-recall half of the trifecta) — the project-local
    #    ./CLAUDE.md or the global ~/.claude/CLAUDE.md both satisfy it.
    sentinel = "Recall first — actively"
    rule_file: Optional[str] = None
    for md_path in ("CLAUDE.md", os.path.join(os.path.expanduser("~"), ".claude", "CLAUDE.md")):
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                if sentinel in f.read():
                    rule_file = md_path
                    break
        except OSError:
            continue
    if rule_file:
        line("ok", "CLAUDE.md", f'recall rule present in {rule_file} ("{sentinel}")')
    else:
        line("WARN", "CLAUDE.md", f'recall-rule sentinel "{sentinel}" not in ./CLAUDE.md or ~/.claude/CLAUDE.md — hook is passive-only (re-run the installer)')

    # 6) python3 on PATH (the hook and cache.py both need it).
    py3 = shutil.which("python3")
    if py3:
        line("ok", "python3", py3)
    else:
        line("FAIL", "python3", "not on PATH — the recall hook cannot run")

    # 7) hook.log tail — dead_key / timeout outcome counts over the last 24h.
    log_path = os.path.join(os.path.expanduser("~"), ".ultramemory", "hook.log")
    if not os.path.exists(log_path):
        line("ok", "hook.log", f"no log yet at {log_path} (hook not run yet, or ULTRAMEMORY_HOOK_LOG=off)")
    else:
        dead = timed_out = total = 0
        cutoff_aware = datetime.now(timezone.utc) - timedelta(hours=24)
        cutoff_naive = datetime.now() - timedelta(hours=24)
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                for raw in f:
                    parts = [p.strip() for p in raw.split("|")]
                    if len(parts) < 3:
                        continue
                    try:
                        ts = datetime.fromisoformat(parts[0].replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if ts < (cutoff_aware if ts.tzinfo else cutoff_naive):
                        continue
                    total += 1
                    if parts[2] == "dead_key":
                        dead += 1
                    elif parts[2] == "timeout":
                        timed_out += 1
            status = "WARN" if (dead or timed_out) else "ok"
            line(status, "hook.log", f"last 24h: dead_key={dead} timeout={timed_out} ({total} runs)")
        except OSError:
            line("WARN", "hook.log", f"unreadable: {log_path}")

    # --probe: live POST, same request shape as install.sh verify_key (timeout 20).
    if args.probe:
        import urllib.error
        import urllib.request

        base = (os.environ.get("ULTRAMEMORY_API_BASE") or DEFAULT_BASE_URL).rstrip("/")
        url = base + "/api/v1/recall/gated"
        if not key:
            line("FAIL", "probe", f"skipped — no API key to send to {url}")
        else:
            body = json.dumps({"query": "installer smoke test", "k": 1, "mode": "preview"}).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
                method="POST",
            )
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
                    code = resp.status
            except urllib.error.HTTPError as e:
                code = e.code
            except Exception:
                code = 0
            ms = int((time.monotonic() - t0) * 1000)
            if code == 200:
                line("ok", "probe", f"{url} -> 200 OK ({ms} ms)")
            elif code == 401:
                line("FAIL", "probe", f"{url} -> 401 dead key ({ms} ms) — rotate it at https://app.ultramemory.us")
            elif code == 0:
                line("FAIL", "probe", f"{url} -> offline (connection failed after {ms} ms)")
            else:
                line("FAIL", "probe", f"{url} -> HTTP {code} ({ms} ms)")

    return 1 if failed else 0


def _add_bool_flag(p: argparse.ArgumentParser, name: str, help_text: str) -> None:
    """Add a paired --flag/--no-flag that defaults to None (= 'leave as configured')."""
    dest = name.replace("-", "_")
    p.add_argument(f"--{name}", dest=dest, action="store_true", default=None, help=help_text)
    p.add_argument(f"--no-{name}", dest=dest, action="store_false", help=f"disable {name}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ultramemory",
        description="UltraMemory — wire the self-learning memory provider into Hermes.",
    )
    sub = parser.add_subparsers(dest="command")

    enable = sub.add_parser(
        "enable",
        help="enable UltraMemory in this Hermes install (writes the key + selects the provider)",
    )
    enable.add_argument("--key", help="UltraMemory API key (um_…). Defaults to $ULTRAMEMORY_API_KEY.")
    enable.add_argument(
        "--base-url",
        help=f"API base URL (default {DEFAULT_BASE_URL})",
    )
    enable.add_argument(
        "--recall-k", type=int, help="facts to recall per turn (1-100)"
    )
    _add_bool_flag(enable, "gated", "use metamemory-gated recall for auto-inject (default on)")
    _add_bool_flag(enable, "auto-capture", "auto-persist each completed turn (default on)")
    enable.add_argument(
        "--hermes-home",
        help="override $HERMES_HOME (where .env / config.yaml live)",
    )
    enable.set_defaults(func=_cmd_enable)

    disable = sub.add_parser(
        "disable",
        help="disable UltraMemory in this Hermes install (removes the shim, resets memory.provider to builtin)",
    )
    disable.add_argument(
        "--hermes-home",
        help="override $HERMES_HOME (where plugins/ and config.yaml live)",
    )
    disable.set_defaults(func=_cmd_disable)

    kit = sub.add_parser(
        "kit",
        help="install / uninstall the UltraMemory Agent Kit (Tier 3), or export/check it (maintainer)",
    )
    kit.add_argument(
        "kit_action",
        choices=["install", "uninstall", "export", "check"],
        help="install (guided), uninstall (manifest-driven), export (curate ~/.claude → agent-kit/), "
        "check (drift guard)",
    )
    kit.add_argument(
        "rest",
        nargs=argparse.REMAINDER,
        help="extra args passed through to the installer (e.g. --tier 3 --dry-run)",
    )
    kit.set_defaults(func=_cmd_kit)

    doctor = sub.add_parser(
        "doctor",
        help="diagnose the recall-first install (7 static checks; --probe adds a live API round-trip)",
    )
    doctor.add_argument(
        "--probe",
        action="store_true",
        help="also POST a 1-fact gated recall to the live API (reports 200 / 401 dead key / offline + RTT ms)",
    )
    doctor.set_defaults(func=_cmd_doctor)

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
