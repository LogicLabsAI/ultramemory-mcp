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
     falling back to ``~/.hermes/config.yaml``) so the provider is actually selected.

Provider behaviour itself is untouched — this module only orchestrates the host-side wiring the
provider can't do from inside a hook.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Optional

from . import DEFAULT_BASE_URL, UltraMemoryProvider

__all__ = ["main"]


def _hermes_home() -> str:
    """Resolve $HERMES_HOME the same way the provider does, so the CLI and the running provider
    agree on where config lives."""
    try:  # prefer Hermes' own resolver when it's importable
        from hermes_constants import get_hermes_home

        return get_hermes_home()
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


def _cmd_enable(args: argparse.Namespace) -> int:
    key = (args.key or os.environ.get("ULTRAMEMORY_API_KEY") or "").strip()
    if not key:
        print(
            "error: no API key. Pass --key um_… (get one at https://ultramemory.us) "
            "or set ULTRAMEMORY_API_KEY.",
            file=sys.stderr,
        )
        return 2

    hermes_home = (args.hermes_home or _hermes_home()).rstrip("/") or _hermes_home()
    os.makedirs(hermes_home, exist_ok=True)

    # 1) secret -> $HERMES_HOME/.env (never the JSON)
    env_path = os.path.join(hermes_home, ".env")
    _upsert_env(env_path, "ULTRAMEMORY_API_KEY", key)

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
    provider = UltraMemoryProvider()
    if values:
        provider.save_config(values, hermes_home)

    # 3) select the provider in the Hermes config
    cfg_path = _set_memory_provider(hermes_home, provider.name)

    base = values.get("base_url") or DEFAULT_BASE_URL
    print("UltraMemory enabled.")
    print(f"  api key   -> {env_path} (ULTRAMEMORY_API_KEY, chmod 600)")
    print(f"  provider  -> {cfg_path} (memory.provider: {provider.name})")
    print(f"  base url  -> {base}")
    print("Restart Hermes (or start a new session) to pick up the change.")
    return 0


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
