"""Unit tests for the autoconfig engine (`ultramemory configure`).

The merge engine touches customer config files — the #1 corruption risk (Cline GH #9663
precedent) — so every safe-merge helper, the consent runner's zero-write guarantees, the
manifest "setting" rows, and the surgical restore path are exercised here against
sandboxed temp dirs ONLY. No test reads or writes the real $HOME.
"""
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib

import pytest

import autoconfig
from autoconfig import ABSENT, ProposedChange

REPO = os.path.dirname(os.path.abspath(__file__))
REAL_HOME = os.path.expanduser("~")  # captured at import, BEFORE any HOME monkeypatch


# ------------------------------------------------------------------ helpers ----
def _sandbox_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _make_stub(changes, detected=True):
    class Stub(autoconfig.PlatformAdapter):
        def detect(self):
            return detected

        def plan(self, model=None, items=None):
            return list(changes)

    Stub.name = "claude-code"
    return Stub


def _backups(directory):
    return sorted(p for p in os.listdir(directory) if ".um-backup-" in p)


# --------------------------------------------------- JSON merge semantics ----
def test_json_merge_preserves_unknown_keys_and_dedupes_arrays(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(
        json.dumps(
            {
                "customKey": {"nested": [1, 2]},
                "permissions": {"allow": ["mcp__other__tool"], "deny": ["Bash(rm *)"]},
            }
        )
    )
    changes = [
        ProposedChange("claude-code", str(f), "model", ABSENT, "opus", "persist", "set model"),
        ProposedChange(
            "claude-code",
            str(f),
            "permissions.allow",
            ["mcp__other__tool"],
            ["mcp__other__tool", "mcp__ultramemory__memory_recall", "mcp__ultramemory__memory_write"],
            "persist",
            "allow the UltraMemory tools",
        ),
    ]
    autoconfig.apply_changes(changes)
    data = json.loads(f.read_text())
    # unknown keys preserved
    assert data["customKey"] == {"nested": [1, 2]}
    assert data["permissions"]["deny"] == ["Bash(rm *)"]
    # merge applied; array appended with dedupe (existing entry not duplicated)
    assert data["model"] == "opus"
    assert data["permissions"]["allow"] == [
        "mcp__other__tool",
        "mcp__ultramemory__memory_recall",
        "mcp__ultramemory__memory_write",
    ]
    # second run is idempotent: no duplicate array entries
    autoconfig.apply_changes(changes)
    data2 = json.loads(f.read_text())
    assert data2["permissions"]["allow"] == data["permissions"]["allow"]


# ------------------------------------------------------- JSONC survival ----
JSONC_FIXTURE = """{
  // cursor keeps user notes here — this comment must survive
  "approvalMode": "manual", /* inline note */
  "mcpAllowlist": ["other:tool"],
  "editor": {
    "vimMode": false, // trailing-comma tolerated below
  },
}
"""


def test_jsonc_comment_survival(tmp_path):
    f = tmp_path / "permissions.jsonc"
    f.write_text(JSONC_FIXTURE)
    prior, final = autoconfig.jsonc_set_file(str(f), "approvalMode", "auto-review")
    assert prior == "manual" and final == "auto-review"
    autoconfig.jsonc_set_file(str(f), "maxMode", True)
    autoconfig.jsonc_set_file(str(f), "mcpAllowlist", ["other:tool", "ultramemory:memory_recall"])
    text = f.read_text()
    assert "// cursor keeps user notes here — this comment must survive" in text
    assert "/* inline note */" in text
    assert "// trailing-comma tolerated below" in text
    data = autoconfig.jsonc_read(str(f))
    assert data["approvalMode"] == "auto-review"
    assert data["maxMode"] is True
    assert data["mcpAllowlist"] == ["other:tool", "ultramemory:memory_recall"]
    assert data["editor"] == {"vimMode": False}


def test_jsonc_detected_for_json_extension_with_comments(tmp_path):
    # Cursor's permissions.json is JSONC despite the .json extension — detect_format
    # must fall back to the JSONC writer so comments survive.
    f = tmp_path / "permissions.json"
    f.write_text('{\n  // keep me\n  "a": 1,\n}\n')
    assert autoconfig.detect_format(str(f)) == "jsonc"
    row = autoconfig.apply_change(
        ProposedChange("cursor", str(f), "b", ABSENT, 2, "persist", "add b")
    )
    assert "// keep me" in f.read_text()
    assert autoconfig.jsonc_read(str(f)) == {"a": 1, "b": 2}
    assert row["prior"] == {"absent": True}


# ------------------------------------------------ TOML root-key ordering ----
TOML_FIXTURE = """# codex config — comment must survive
existing_root = "keep"

[mcp_servers.other]
url = "https://example.com"
"""


def test_toml_root_key_ordering_regression(tmp_path):
    # NAIVE PATH (the grounded failure mode): appending a root key after a [table]
    # silently makes it a key of that table — this must fail root lookup.
    naive = tmp_path / "naive.toml"
    naive.write_text(TOML_FIXTURE)
    with open(naive, "a", encoding="utf-8") as fh:
        fh.write('model_reasoning_effort = "high"\n')
    parsed = tomllib.loads(naive.read_text())
    assert "model_reasoning_effort" not in parsed  # naive append FAILS at root...
    assert parsed["mcp_servers"]["other"]["model_reasoning_effort"] == "high"  # ...lands in the table

    # TOMLKIT PATH (the engine): root key inserted BEFORE the first table.
    good = tmp_path / "config.toml"
    good.write_text(TOML_FIXTURE)
    prior, final = autoconfig.toml_set(str(good), "model_reasoning_effort", "high")
    assert prior is ABSENT and final == "high"
    parsed = tomllib.loads(good.read_text())
    assert parsed["model_reasoning_effort"] == "high"  # at root, NOT inside the table
    assert parsed["existing_root"] == "keep"
    assert parsed["mcp_servers"]["other"] == {"url": "https://example.com"}
    assert "# codex config — comment must survive" in good.read_text()


# ------------------------------------------------------ backup discipline ----
def test_backup_created_once_per_file_per_run(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"a": 1}))
    changes = [
        ProposedChange("claude-code", str(f), "model", ABSENT, "opus", "persist", "m"),
        ProposedChange("claude-code", str(f), "effortLevel", ABSENT, "xhigh", "persist", "e"),
    ]
    autoconfig.apply_changes(changes)  # one run = one shared backed_up set
    baks = _backups(tmp_path)
    assert len(baks) == 1, baks
    # the backup holds the PRE-run bytes
    assert json.loads((tmp_path / baks[0]).read_text()) == {"a": 1}


# ------------------------------------------------- full flow via consent ----
def _full_apply(monkeypatch, tmp_path):
    """configure --yes on a sandbox HOME with a stub adapter; returns (home, target)."""
    home = _sandbox_home(monkeypatch, tmp_path)
    target = home / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps(
            {
                "model": "sonnet",  # pre-exists WITH ANOTHER VALUE
                "keep": {"mine": True},
                "permissions": {"allow": ["mcp__other__tool"]},
            }
        )
    )
    changes = [
        ProposedChange("claude-code", str(target), "model", "sonnet", "opus", "persist", "set model"),
        ProposedChange("claude-code", str(target), "effortLevel", ABSENT, "xhigh", "persist", "set effort"),
        ProposedChange(
            "claude-code",
            str(target),
            "permissions.allow",
            ["mcp__other__tool"],
            ["mcp__ultramemory__memory_recall"],
            "persist",
            "allow tool",
        ),
    ]
    monkeypatch.setitem(autoconfig.ADAPTERS, "claude-code", _make_stub(changes))
    rc = autoconfig.run_configure(platform="claude-code", yes=True)
    assert rc == 0
    return home, target


def test_manifest_rows_exact(monkeypatch, tmp_path):
    home, target = _full_apply(monkeypatch, tmp_path)
    mf = home / ".ultramemory" / "install-manifest.json"
    assert mf.exists()
    rows = json.loads(mf.read_text())["items"]["setting"]
    assert len(rows) == 3
    for row in rows:
        # EXACTLY the seven spec'd fields — nothing more, nothing less
        assert set(row) == {"type", "platform", "path", "key", "prior", "new", "created"}
        assert row["type"] == "setting"
        assert row["platform"] == "claude-code"
        assert row["path"] == str(target)
        assert row["created"]
    by_key = {r["key"]: r for r in rows}
    assert by_key["model"]["prior"] == {"value": "sonnet"}
    assert by_key["model"]["new"] == {"value": "opus"}
    assert by_key["effortLevel"]["prior"] == {"absent": True}
    assert by_key["effortLevel"]["new"] == {"value": "xhigh"}
    assert by_key["permissions.allow"]["prior"] == {"value": ["mcp__other__tool"]}
    assert by_key["permissions.allow"]["new"] == {
        "value": ["mcp__other__tool", "mcp__ultramemory__memory_recall"]
    }


def test_restore_surgical(monkeypatch, tmp_path):
    home, target = _full_apply(monkeypatch, tmp_path)
    # the user changes an UNRELATED key after our apply — restore must NOT clobber it
    # (surgical key-level revert from recorded priors, never a blind backup copy)
    data = json.loads(target.read_text())
    data["keep"] = {"mine": "user-edited-since"}
    data["userAddedLater"] = 42
    target.write_text(json.dumps(data, indent=2) + "\n")

    rc = autoconfig.run_configure(restore=True)
    assert rc == 0
    after = json.loads(target.read_text())
    assert after["model"] == "sonnet"  # pre-existing OTHER value restored exactly
    assert "effortLevel" not in after  # absent-before key removed
    assert after["permissions"]["allow"] == ["mcp__other__tool"]  # array back to prior
    assert after["keep"] == {"mine": "user-edited-since"}  # user edit survives
    assert after["userAddedLater"] == 42  # user addition survives
    # restored rows dropped from the manifest
    mf = home / ".ultramemory" / "install-manifest.json"
    assert json.loads(mf.read_text())["items"]["setting"] == []


def test_restore_prunes_created_parent_objects(monkeypatch, tmp_path):
    """Z2-FIX-1: a settings file with NO `permissions` key gets permissions.defaultMode
    + permissions.allow applied; --restore must remove the leaves AND the `permissions`
    container the apply itself created — parsed JSON back to the exact original (no
    empty `"permissions": {}` shell left behind)."""
    home = _sandbox_home(monkeypatch, tmp_path)
    target = home / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    original = {"model": "sonnet", "keep": {"mine": True}}  # NO permissions key
    target.write_text(json.dumps(original))
    changes = [
        ProposedChange(
            "claude-code", str(target), "permissions.defaultMode", ABSENT, "auto",
            "persist", "set mode",
        ),
        ProposedChange(
            "claude-code", str(target), "permissions.allow", ABSENT,
            ["mcp__ultramemory__memory_recall"], "persist", "allow tool",
        ),
    ]
    monkeypatch.setitem(autoconfig.ADAPTERS, "claude-code", _make_stub(changes))
    rc = autoconfig.run_configure(platform="claude-code", yes=True)
    assert rc == 0
    applied = json.loads(target.read_text())
    assert applied["permissions"] == {
        "defaultMode": "auto",
        "allow": ["mcp__ultramemory__memory_recall"],
    }

    rc = autoconfig.run_configure(restore=True)
    assert rc == 0
    after = json.loads(target.read_text())
    assert after == original  # exact parsed round-trip
    assert "permissions" not in after  # the apply-created shell is pruned, not left {}


def test_restore_preserves_preexisting_empty_parent(monkeypatch, tmp_path):
    """Z2-FIX-1 over-pruning guard: a `"permissions": {}` that PRE-EXISTED the apply
    (even as an empty object) must survive --restore exactly as before — only
    containers the apply itself created may be pruned."""
    home = _sandbox_home(monkeypatch, tmp_path)
    target = home / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    original = {"model": "sonnet", "permissions": {}}  # pre-existing EMPTY parent
    target.write_text(json.dumps(original))
    changes = [
        ProposedChange(
            "claude-code", str(target), "permissions.defaultMode", ABSENT, "auto",
            "persist", "set mode",
        ),
        ProposedChange(
            "claude-code", str(target), "permissions.allow", ABSENT,
            ["mcp__ultramemory__memory_recall"], "persist", "allow tool",
        ),
    ]
    monkeypatch.setitem(autoconfig.ADAPTERS, "claude-code", _make_stub(changes))
    rc = autoconfig.run_configure(platform="claude-code", yes=True)
    assert rc == 0

    rc = autoconfig.run_configure(restore=True)
    assert rc == 0
    after = json.loads(target.read_text())
    assert after == original
    assert after["permissions"] == {}  # still present exactly as before the apply


def test_uninstall_sh_restores_setting_rows(monkeypatch, tmp_path):
    """A3 round-trip: configure --yes on a sandbox HOME -> keys set -> uninstall.sh ->
    EXACT prior state, including a key that pre-existed with another value."""
    home, target = _full_apply(monkeypatch, tmp_path)
    assert json.loads(target.read_text())["model"] == "opus"  # keys set
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    env = dict(os.environ, HOME=str(home))
    proc = subprocess.run(
        ["bash", os.path.join(REPO, "uninstall.sh")],
        cwd=str(workdir),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    after = json.loads(target.read_text())
    assert after == {
        "model": "sonnet",
        "keep": {"mine": True},
        "permissions": {"allow": ["mcp__other__tool"]},
    }
    assert not (home / ".ultramemory" / "install-manifest.json").exists()


# ---------------------------------------------------------- zero-write paths ----
def test_dry_run_writes_nothing(monkeypatch, tmp_path, capsys):
    home = _sandbox_home(monkeypatch, tmp_path)
    target = tmp_path / "settings.json"
    original = json.dumps({"model": "sonnet"})
    target.write_text(original)
    changes = [
        ProposedChange("claude-code", str(target), "model", "sonnet", "opus", "persist", "set model")
    ]
    monkeypatch.setitem(autoconfig.ADAPTERS, "claude-code", _make_stub(changes))
    rc = autoconfig.run_configure(platform="claude-code", dry_run=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "model" in out and "dry-run" in out  # the plan was printed
    assert target.read_text() == original  # file byte-identical
    assert _backups(tmp_path) == []  # no backup created
    assert not (home / ".ultramemory" / "install-manifest.json").exists()  # no manifest


def test_consent_default_deny_noninteractive(monkeypatch, tmp_path):
    home = _sandbox_home(monkeypatch, tmp_path)
    target = tmp_path / "settings.json"
    original = json.dumps({"model": "sonnet"})
    target.write_text(original)
    changes = [
        ProposedChange("claude-code", str(target), "model", "sonnet", "opus", "persist", "set model")
    ]
    monkeypatch.setitem(autoconfig.ADAPTERS, "claude-code", _make_stub(changes))
    monkeypatch.setattr(sys, "stdin", io.StringIO())  # non-tty, nothing to read
    rc = autoconfig.run_configure(platform="claude-code", yes=False)
    assert rc == 2  # default-deny: no --yes + non-tty
    assert target.read_text() == original  # zero writes
    assert _backups(tmp_path) == []
    assert not (home / ".ultramemory" / "install-manifest.json").exists()


def test_interactive_default_is_no(monkeypatch, tmp_path):
    """Pressing Enter (empty answer) at the per-item prompt must mean No."""
    _sandbox_home(monkeypatch, tmp_path)
    target = tmp_path / "settings.json"
    original = json.dumps({"model": "sonnet"})
    target.write_text(original)
    changes = [
        ProposedChange("claude-code", str(target), "model", "sonnet", "opus", "persist", "set model")
    ]
    monkeypatch.setitem(autoconfig.ADAPTERS, "claude-code", _make_stub(changes))

    class TtyStdin(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", TtyStdin("\n"))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    rc = autoconfig.run_configure(platform="claude-code", yes=False)
    assert rc == 0
    assert target.read_text() == original  # declined by default -> zero writes


# ==================================================================================
# Platform adapters (Clusters B/C) — sandboxed round-trips only; NEVER the real $HOME.
# ==================================================================================
UM_TOOLS = [
    "memory_recall",
    "memory_write",
    "search",
    "fetch",
    "recall_gated",
    "recall_verified",
    "playbook_recall",
    "memory_feedback",
]


# ----------------------------------------------------- B1: Claude Code adapter ----
def test_b1_claude_code_roundtrip_and_idempotent(monkeypatch, tmp_path):
    """B1 Verify: sandbox HOME round-trip asserts all 5 keys; pre-existing settings
    keys preserved; second run idempotent (no dup array entries/hooks); ~/.claude.json
    NEVER touched."""
    home = _sandbox_home(monkeypatch, tmp_path)
    claude = home / ".claude"
    claude.mkdir()
    settings = claude / "settings.json"
    settings.write_text(json.dumps({
        "theme": "dark",
        "permissions": {"allow": ["mcp__other__tool"], "deny": ["Bash(rm *)"]},
        "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "x.sh"}]}]},
    }))
    registration = home / ".claude.json"  # MCP registration file — must NEVER be touched
    registration.write_text('{"mcpServers": {"ultramemory": {}}}')
    reg_bytes = registration.read_text()

    rc = autoconfig.run_configure(platform="claude-code", yes=True)
    assert rc == 0
    data = json.loads(settings.read_text())
    # all 5 keys (B1)
    assert data["model"] == "opus"
    assert data["effortLevel"] == "xhigh"
    assert data["permissions"]["defaultMode"] == "auto"
    for t in UM_TOOLS:
        assert "mcp__ultramemory__%s" % t in data["permissions"]["allow"]
    ss = data["hooks"]["SessionStart"]
    assert ss == [{
        "matcher": "startup|clear",
        "hooks": [{"type": "command", "command": "~/.claude/hooks/onboard-ultramemory.sh"}],
    }]
    # pre-existing keys preserved
    assert data["theme"] == "dark"
    assert data["permissions"]["allow"][0] == "mcp__other__tool"
    assert data["permissions"]["deny"] == ["Bash(rm *)"]
    assert data["hooks"]["UserPromptSubmit"] == [
        {"hooks": [{"type": "command", "command": "x.sh"}]}
    ]
    # ~/.claude.json untouched (never registration surgery — grounded corruption gotcha)
    assert registration.read_text() == reg_bytes

    # second run idempotent: nothing planned, file byte-identical
    before = settings.read_text()
    rc = autoconfig.run_configure(platform="claude-code", yes=True)
    assert rc == 0
    assert settings.read_text() == before
    allow = json.loads(settings.read_text())["permissions"]["allow"]
    assert len(allow) == len(set(allow))  # no dup array entries
    assert len(json.loads(settings.read_text())["hooks"]["SessionStart"]) == 1  # no dup hooks


# ----------------------------------------------------------- B2: Codex adapter ----
CODEX_TOML = """# codex config — user comment must survive
existing_root = "keep"

[mcp_servers.ultramemory]
url = "https://api.ultramemory.us/mcp"

[mcp_servers.other]
url = "https://example.com"
"""


def test_b2_codex_roundtrip(monkeypatch, tmp_path):
    """B2 Verify: tomlkit round-trip preserves comments; config parses via python
    tomllib; hooks.json valid JSON; trust-gate copy present; model NOT set by default."""
    home = _sandbox_home(monkeypatch, tmp_path)
    codex = home / ".codex"
    codex.mkdir()
    cfg = codex / "config.toml"
    cfg.write_text(CODEX_TOML)

    rc = autoconfig.run_configure(platform="codex", yes=True)
    assert rc == 0
    text = cfg.read_text()
    assert "# codex config — user comment must survive" in text
    parsed = tomllib.loads(text)  # config parses (tomllib smoke)
    # root keys landed at ROOT (never inside a [table] — the grounded ordering trap)
    assert parsed["model_reasoning_effort"] == "high"
    assert parsed["approval_policy"] == "on-request"
    assert parsed["sandbox_mode"] == "workspace-write"
    assert parsed["existing_root"] == "keep"
    assert "model" not in parsed  # NOT set by default (Statsig/account-gated)
    # per-server allowlist under the EXISTING registration only
    assert parsed["mcp_servers"]["ultramemory"]["enabled_tools"] == UM_TOOLS
    assert parsed["mcp_servers"]["other"] == {"url": "https://example.com"}
    # hooks.json valid JSON with the SessionStart onboarding entry
    hooks = json.loads((codex / "hooks.json").read_text())
    entries = hooks["hooks"]["SessionStart"]
    assert len(entries) == 1 and entries[0]["matcher"] == "startup|clear"
    assert entries[0]["hooks"][0]["command"].endswith("onboard-ultramemory.sh")


def test_b2_codex_trust_gate_copy_and_items_xhigh(monkeypatch, tmp_path):
    home = _sandbox_home(monkeypatch, tmp_path)
    codex = home / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text(CODEX_TOML)
    # the hook consent copy MUST tell the user about the /hooks trust gate (item B2)
    plan = autoconfig.CodexAdapter().plan()
    hook_changes = [c for c in plan if c.kind == "onboard-hook"]
    assert len(hook_changes) == 1
    assert "/hooks" in hook_changes[0].description
    assert "trust" in hook_changes[0].description.lower()
    # xhigh only with explicit --items xhigh (scoped: ONLY that change applies)
    rc = autoconfig.run_configure(platform="codex", yes=True, items="xhigh")
    assert rc == 0
    parsed = tomllib.loads((codex / "config.toml").read_text())
    assert parsed["model_reasoning_effort"] == "xhigh"
    assert "approval_policy" not in parsed  # --items scoped the run to xhigh only
    assert not (codex / "hooks.json").exists()


# ------------------------------------------------------ B3: Gemini CLI adapter ----
def _fake_run_cli(monkeypatch, table):
    """Monkeypatch autoconfig.run_cli: table maps argv[0] -> (rc, stdout)."""
    def fake(argv, timeout=20):
        return table.get(argv[0], (127, ""))
    monkeypatch.setattr(autoconfig, "run_cli", fake)


def test_b3_gemini_v2_roundtrip(monkeypatch, tmp_path):
    """B3 Verify (v2 fixture): nested keys written, unknown keys preserved, hook wired
    when `gemini --version` >= 0.26.0."""
    home = _sandbox_home(monkeypatch, tmp_path)
    gem = home / ".gemini"
    gem.mkdir()
    settings = gem / "settings.json"
    settings.write_text(json.dumps({
        "general": {"vimMode": True},
        "mcpServers": {"other": {"httpUrl": "https://example.com"}},
    }))
    _fake_run_cli(monkeypatch, {"gemini": (0, "0.29.5")})

    rc = autoconfig.run_configure(platform="gemini", yes=True)
    assert rc == 0
    data = json.loads(settings.read_text())
    assert data["general"]["defaultApprovalMode"] == "auto_edit"
    assert data["general"]["vimMode"] is True  # sibling key preserved
    assert data["modelConfigs"]["overrides"] == [autoconfig.GeminiAdapter.THINKING_OVERRIDE]
    assert data["mcpServers"]["ultramemory"]["trust"] is True
    assert data["mcpServers"]["ultramemory"]["includeTools"] == UM_TOOLS
    assert data["mcpServers"]["other"] == {"httpUrl": "https://example.com"}  # untouched
    ss = data["hooks"]["SessionStart"]
    assert len(ss) == 1 and ss[0]["matcher"] == "startup"
    assert ss[0]["hooks"][0]["command"].endswith("onboard-ultramemory.sh")
    assert "name" not in data.get("model", {})  # model routing left alone without --model


def test_b3_gemini_v1_flat_skips_with_zero_writes(monkeypatch, tmp_path, capsys):
    """B3 Verify (v1 fixture): skip+warn, zero writes."""
    home = _sandbox_home(monkeypatch, tmp_path)
    gem = home / ".gemini"
    gem.mkdir()
    settings = gem / "settings.json"
    original = json.dumps({"theme": "Default", "selectedAuthType": "oauth-personal"})
    settings.write_text(original)
    _fake_run_cli(monkeypatch, {"gemini": (0, "0.29.5")})

    rc = autoconfig.run_configure(platform="gemini", yes=True)
    assert rc == 0
    assert settings.read_text() == original  # zero writes
    assert _backups(gem) == []
    assert not (home / ".ultramemory" / "install-manifest.json").exists()
    out = capsys.readouterr().out
    assert "v1 FLAT" in out and "auto-migrates" in out  # warn + upgrade hint


def test_b3_gemini_hooks_version_gate(monkeypatch, tmp_path):
    """Hook skipped+warned below 0.26.0; every other key still lands."""
    home = _sandbox_home(monkeypatch, tmp_path)
    (home / ".gemini").mkdir()
    settings = home / ".gemini" / "settings.json"
    settings.write_text("{}")
    _fake_run_cli(monkeypatch, {"gemini": (0, "0.25.0")})
    rc = autoconfig.run_configure(platform="gemini", yes=True)
    assert rc == 0
    data = json.loads(settings.read_text())
    assert data["general"]["defaultApprovalMode"] == "auto_edit"
    assert "hooks" not in data  # version-gated: no SessionStart below 0.26.0


# ---------------------------------------------------------- B4: Cursor adapter ----
CURSOR_PERMISSIONS_JSONC = """{
  // user's own note — must survive
  "terminalAllowlist": ["git status"],
  "mcpAllowlist": ["other:tool"],
}
"""


def test_b4_cursor_roundtrip(monkeypatch, tmp_path):
    """B4 Verify: sandbox round-trip; JSONC comments preserved; no `model` object
    written; allowlist appended (never clobbered, never empty)."""
    home = _sandbox_home(monkeypatch, tmp_path)
    cursor = home / ".cursor"
    cursor.mkdir()
    perms = cursor / "permissions.json"
    perms.write_text(CURSOR_PERMISSIONS_JSONC)

    rc = autoconfig.run_configure(platform="cursor", yes=True)
    assert rc == 0
    cli = json.loads((cursor / "cli-config.json").read_text())
    assert cli["version"] == 1
    assert cli["approvalMode"] == "auto-review"
    assert cli["maxMode"] is True
    assert "model" not in cli  # undocumented CLI-managed object — NEVER hand-written
    text = perms.read_text()
    assert "// user's own note — must survive" in text  # JSONC comments preserved
    pdata = autoconfig.jsonc_read(str(perms))
    assert pdata["terminalAllowlist"] == ["git status"]
    assert pdata["mcpAllowlist"] == ["other:tool"] + ["ultramemory:%s" % t for t in UM_TOOLS]
    assert pdata["mcpAllowlist"] != []  # never an empty allowlist
    hooks = json.loads((cursor / "hooks.json").read_text())
    assert hooks["version"] == 1
    assert hooks["hooks"]["sessionStart"][0]["command"].endswith("onboard-ultramemory.sh")
    # disclosure copy: mcpAllowlist REPLACES the in-app editor (item B4 mandate)
    perms.write_text(CURSOR_PERMISSIONS_JSONC)  # reset so plan proposes again
    plan = autoconfig.CursorAdapter().plan()
    allow_changes = [c for c in plan if c.key_path == "mcpAllowlist"]
    assert allow_changes and "REPLACES" in allow_changes[0].description


def test_b4_cursor_empty_allowlist_regression(monkeypatch, tmp_path):
    """Never write "mcpAllowlist": [] — restore of a prior-ABSENT key REMOVES the key
    (an empty array would lock every MCP tool out — grounded)."""
    home = _sandbox_home(monkeypatch, tmp_path)
    cursor = home / ".cursor"
    cursor.mkdir()  # no permissions.json at all — key will be created fresh

    rc = autoconfig.run_configure(platform="cursor", yes=True)
    assert rc == 0
    perms = cursor / "permissions.json"
    data = autoconfig.jsonc_read(str(perms))
    assert data["mcpAllowlist"] == ["ultramemory:%s" % t for t in UM_TOOLS]
    assert data["mcpAllowlist"] != []

    rc = autoconfig.run_configure(restore=True)
    assert rc == 0
    after = autoconfig.jsonc_read(str(perms))
    assert "mcpAllowlist" not in after  # key removed entirely — NOT left as []


# ---------------------------------------------------------- B5: Hermes adapter ----
FAKE_HERMES_CLI = """#!/usr/bin/env python3
# Sandbox stand-in for the Hermes CLI: `hermes config set/get <dotted> [value]`
# persisted to $HERMES_HOME/config.yaml (JSON body — JSON is valid YAML).
import json, os, sys
home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
cfg = os.path.join(home, "config.yaml")
def load():
    try:
        with open(cfg) as f:
            return json.load(f)
    except Exception:
        return {}
a = sys.argv[1:]
if len(a) >= 3 and a[0] == "config" and a[1] == "get":
    node = load()
    for seg in a[2].split("."):
        if not isinstance(node, dict) or seg not in node:
            sys.exit(1)
        node = node[seg]
    print(node if isinstance(node, str) else json.dumps(node))
    sys.exit(0)
if len(a) == 4 and a[0] == "config" and a[1] == "set":
    d = load(); node = d
    parts = a[2].split(".")
    for seg in parts[:-1]:
        node = node.setdefault(seg, {})
    node[parts[-1]] = a[3]
    os.makedirs(home, exist_ok=True)
    with open(cfg, "w") as f:
        json.dump(d, f, indent=2)
    sys.exit(0)
sys.exit(2)
"""


def _install_fake_hermes(monkeypatch, tmp_path):
    """Sandbox HERMES_HOME + a fake `hermes` CLI on PATH; returns the hermes home."""
    home = _sandbox_home(monkeypatch, tmp_path)
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cli = bindir / "hermes"
    cli.write_text(FAKE_HERMES_CLI)
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", "%s%s%s" % (bindir, os.pathsep, os.environ.get("PATH", "")))
    return home, hermes_home


def test_b5_hermes_cli_roundtrip(monkeypatch, tmp_path):
    """B5 Verify: `hermes config get agent.reasoning_effort` == xhigh in sandbox
    HERMES_HOME; approvals.mode + model UNTOUCHED by default; honesty note in copy."""
    _home, hermes_home = _install_fake_hermes(monkeypatch, tmp_path)
    (hermes_home / "config.yaml").write_text(json.dumps({"approvals": {"mode": "smart"}}))

    plan = autoconfig.HermesAdapter().plan()
    assert [c.key_path for c in plan] == ["agent.reasoning_effort"]
    assert "#16462" in plan[0].description  # MCP-approvals honesty note (item B5 copy)

    rc = autoconfig.run_configure(platform="hermes", yes=True)
    assert rc == 0
    proc = subprocess.run(
        ["hermes", "config", "get", "agent.reasoning_effort"],
        capture_output=True, text=True, timeout=30,
        env=dict(os.environ),
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "xhigh"
    cfg = json.loads((hermes_home / "config.yaml").read_text())
    assert cfg["approvals"]["mode"] == "smart"  # UNTOUCHED by default
    assert "model" not in cfg  # only with --model
    # manifest row records the CLI-read prior so rollback = hermes config set <prior>
    mf = _home / ".ultramemory" / "install-manifest.json"
    rows = json.loads(mf.read_text())["items"]["setting"]
    assert rows[0]["platform"] == "hermes"
    assert rows[0]["key"] == "agent.reasoning_effort"
    assert rows[0]["new"] == {"value": "xhigh"}


# ----------------------------------------------------------- C1: Cline adapter ----
def test_c1_cline_roundtrip_and_taskstart_hook(monkeypatch, tmp_path):
    """C1 Verify: JSON parse round-trip; hook file executable; fixture TaskStart stdin
    -> onboarding JSON out; restore deletes the hook file + reverts JSON keys."""
    home = _sandbox_home(monkeypatch, tmp_path)
    _fake_run_cli(monkeypatch, {})  # cline CLI probe -> (127, "") — file probing path
    settings = home / ".cline" / "data" / "settings" / "cline_mcp_settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "mcpServers": {
            "ultramemory": {"url": "https://api.ultramemory.us/mcp", "disabled": False},
            "other": {"type": "sse", "url": "https://example.com"},
        }
    }))

    rc = autoconfig.run_configure(platform="cline", yes=True)
    assert rc == 0
    data = json.loads(settings.read_text())  # JSON parse round-trip
    um_server = data["mcpServers"]["ultramemory"]
    assert um_server["type"] == "streamableHttp"  # always explicit — no silent SSE
    assert um_server["autoApprove"] == UM_TOOLS
    assert um_server["url"] == "https://api.ultramemory.us/mcp"  # untouched
    assert data["mcpServers"]["other"] == {"type": "sse", "url": "https://example.com"}

    hook = home / ".cline" / "hooks" / "TaskStart"
    assert hook.exists() and os.access(str(hook), os.X_OK)  # executable, extensionless
    proc = subprocess.run(
        [str(hook)], input="{}", capture_output=True, text=True, timeout=30,
        env=dict(os.environ, HOME=str(home)),
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)  # onboarding JSON out
    assert out["cancel"] is False
    assert "--thinking xhigh" in out["contextModification"]

    # C1 rollback: A3 restore + delete hook file (adapter-dispatched via restore_rows)
    rc = autoconfig.run_configure(restore=True)
    assert rc == 0
    assert not hook.exists()
    after = json.loads(settings.read_text())
    assert "type" not in after["mcpServers"]["ultramemory"]
    assert "autoApprove" not in after["mcpServers"]["ultramemory"]


def test_c1_cline_skips_when_not_registered(monkeypatch, tmp_path, capsys):
    """No partial server entry is ever created when ultramemory isn't registered."""
    home = _sandbox_home(monkeypatch, tmp_path)
    _fake_run_cli(monkeypatch, {})
    settings = home / ".cline" / "data" / "settings" / "cline_mcp_settings.json"
    settings.parent.mkdir(parents=True)
    original = json.dumps({"mcpServers": {"other": {"type": "sse", "url": "https://example.com"}}})
    settings.write_text(original)
    rc = autoconfig.run_configure(platform="cline", yes=True)
    assert rc == 0
    assert json.loads(settings.read_text()) == json.loads(original)  # no partial entry
    assert "not registered" in capsys.readouterr().out


# -------------------------------------------------- C2: VS Code/Copilot adapter ----
VSCODE_SETTINGS_JSONC = """{
  // user's editor prefs — must survive
  "editor.fontSize": 13,
  "chat.defaultModel": "auto",
}
"""


def _vscode_settings(home):
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Code" / "User" / "settings.json"
    return home / ".config" / "Code" / "User" / "settings.json"


def test_c2_vscode_roundtrip(monkeypatch, tmp_path):
    """C2 Verify: JSONC comments preserved; settings.json still parses (jsonc parse);
    FLAT keys written (never nested "chat" objects); hook file created standalone."""
    home = _sandbox_home(monkeypatch, tmp_path)
    settings = _vscode_settings(home)
    settings.parent.mkdir(parents=True)
    settings.write_text(VSCODE_SETTINGS_JSONC)
    _fake_run_cli(monkeypatch, {"code": (0, "1.126.0\nabc123\narm64")})

    rc = autoconfig.run_configure(platform="vscode", yes=True)
    assert rc == 0
    text = settings.read_text()
    assert "// user's editor prefs — must survive" in text  # JSONC comments preserved
    data = autoconfig.jsonc_read(str(settings))  # still parses
    # FLAT dotted keys (VS Code style) — never a nested {"chat": {...}} object
    assert data["chat.defaultModel"] == "opus"
    assert data["chat.permissions.default"] == "autoApprove"
    assert data["chat.hooks.enabled"] is True
    assert "chat" not in data
    assert data["editor.fontSize"] == 13
    hook = home / ".copilot" / "hooks" / "ultramemory.json"
    hook_data = json.loads(hook.read_text())
    entry = hook_data["hooks"]["SessionStart"][0]
    assert entry["hooks"][0]["command"].endswith("onboard-ultramemory.sh")
    # A1: Copilot honors ONLY top-level additionalContext — the installed invocation
    # must pass the shape-selecting env var to the kit hooks.
    assert entry["hooks"][0]["command"].startswith("ULTRAMEMORY_HOOK_SHAPE=copilot ")

    # restore is flat-key surgical too: prior "auto" back, added keys removed
    rc = autoconfig.run_configure(restore=True)
    assert rc == 0
    after = autoconfig.jsonc_read(str(settings))
    assert after["chat.defaultModel"] == "auto"
    assert "chat.permissions.default" not in after
    assert "chat.hooks.enabled" not in after
    assert "chat" not in after
    assert "// user's editor prefs — must survive" in settings.read_text()


def test_c2_vscode_permissions_key_probe_skips_when_old(monkeypatch, tmp_path, capsys):
    """Feature-probe: `code` missing or < 1.124 -> chat.permissions.default skipped
    with a warning (never leave junk keys); the rest still lands."""
    home = _sandbox_home(monkeypatch, tmp_path)
    settings = _vscode_settings(home)
    settings.parent.mkdir(parents=True)
    settings.write_text("{}")
    _fake_run_cli(monkeypatch, {"code": (127, "")})
    rc = autoconfig.run_configure(platform="vscode", yes=True)
    assert rc == 0
    data = autoconfig.jsonc_read(str(settings))
    assert "chat.permissions.default" not in data  # probed out — no junk
    assert data["chat.defaultModel"] == "opus"
    assert data["chat.hooks.enabled"] is True
    assert "skipping the Experimental chat.permissions.default" in capsys.readouterr().out


# -------------------------------------------------------- C3: OpenClaw adapter ----
FAKE_OPENCLAW_CLI = """#!/usr/bin/env python3
# Sandbox stand-in for the OpenClaw CLI (used only when the real one can't run —
# node engine gate). Implements: --version, config get/set, hooks enable/list against
# $OPENCLAW_STATE_DIR/openclaw.json.
import json, os, sys
state = os.environ.get("OPENCLAW_STATE_DIR") or os.path.expanduser("~/.openclaw")
cfg = os.path.join(state, "openclaw.json")
def load():
    try:
        with open(cfg) as f:
            return json.load(f)
    except Exception:
        return {}
def save(d):
    os.makedirs(state, exist_ok=True)
    with open(cfg, "w") as f:
        json.dump(d, f, indent=2)
a = sys.argv[1:]
if a[:1] == ["--version"]:
    print("OpenClaw fake 2026.7.1-2")
    sys.exit(0)
if len(a) >= 3 and a[0] == "config" and a[1] == "get":
    node = load()
    for seg in a[2].split("."):
        if not isinstance(node, dict) or seg not in node:
            sys.exit(1)
        node = node[seg]
    print(node if isinstance(node, str) else json.dumps(node))
    sys.exit(0)
if len(a) == 4 and a[0] == "config" and a[1] == "set":
    d = load(); node = d
    parts = a[2].split(".")
    for seg in parts[:-1]:
        node = node.setdefault(seg, {})
    node[parts[-1]] = a[3]
    save(d)
    sys.exit(0)
if len(a) == 3 and a[0] == "hooks" and a[1] == "enable":
    d = load()
    d.setdefault("hooks", {}).setdefault("internal", {}).setdefault("entries", {})[a[2]] = {"enabled": True}
    d["hooks"]["internal"]["enabled"] = True
    save(d)
    sys.exit(0)
if a[:2] == ["hooks", "list"]:
    hd = os.path.join(state, "hooks")
    for n in (sorted(os.listdir(hd)) if os.path.isdir(hd) else []):
        print(n)
    sys.exit(0)
sys.exit(2)
"""


def _openclaw_usable_path():
    """A PATH that can run the REAL openclaw CLI (its node engine gate needs a new
    node first on PATH — grounded gotcha), or None if not runnable on this machine."""
    if not shutil.which("openclaw"):
        return None
    bins = glob.glob(os.path.join(REAL_HOME, ".nvm", "versions", "node", "*", "bin"))
    def _ver(p):
        m = re.search(r"v(\d+)\.(\d+)\.(\d+)", p)
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
    bins.sort(key=_ver, reverse=True)
    path = os.pathsep.join(bins + [os.environ.get("PATH", "")])
    try:
        p = subprocess.run(
            ["openclaw", "--version"], capture_output=True, text=True, timeout=90,
            env=dict(os.environ, PATH=path),
        )
        if p.returncode == 0 and p.stdout.strip():
            return path
    except Exception:
        pass
    return None


def test_c3_openclaw_sandbox_roundtrip(monkeypatch, tmp_path):
    """C3 Verify: `openclaw config get agents.defaults.thinkingDefault` == xhigh in a
    sandboxed OPENCLAW_STATE_DIR; hook listed by `openclaw hooks list`; the restart
    note is in the consent copy; openclaw.json is never parsed directly."""
    _sandbox_home(monkeypatch, tmp_path)
    state = tmp_path / "openclaw-state"
    state.mkdir()
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(state))
    path = _openclaw_usable_path()
    if path is None:  # real CLI not runnable here — fake stand-in keeps the round-trip
        bindir = tmp_path / "bin"
        bindir.mkdir()
        cli = bindir / "openclaw"
        cli.write_text(FAKE_OPENCLAW_CLI)
        cli.chmod(0o755)
        path = "%s%s%s" % (bindir, os.pathsep, os.environ.get("PATH", ""))
    monkeypatch.setenv("PATH", path)

    plan = autoconfig.OpenClawAdapter().plan()
    assert all("openclaw gateway restart" in c.description for c in plan)  # NEVER auto-restart
    assert not any(c.key_path.startswith("tools.exec") for c in plan)  # exec UNTOUCHED

    rc = autoconfig.run_configure(platform="openclaw", yes=True)
    assert rc == 0
    env = dict(os.environ)
    p = subprocess.run(
        ["openclaw", "config", "get", "agents.defaults.thinkingDefault"],
        capture_output=True, text=True, timeout=90, env=env,
    )
    assert p.returncode == 0
    assert p.stdout.strip() == "xhigh"
    hook_dir = state / "hooks" / "ultramemory-onboard"
    assert (hook_dir / "HOOK.md").exists()
    assert (hook_dir / "handler.js").exists()
    p = subprocess.run(["openclaw", "hooks", "list"], capture_output=True, text=True, timeout=90, env=env)
    assert p.returncode == 0
    assert "ultramemory" in p.stdout  # listed (table may wrap the full name)


# -------------------------------------------------- C4: Windsurf/Devin adapter ----
DEVIN_CONFIG_JSONC = """{
  // devin user config — comment must survive
  "permissions": {"allow": ["Exec(git)"], "deny": ["Exec(rm)"]},
  "mcpServers": {"other": {"url": "https://example.com", "transport": "http"}},
}
"""


def test_c4_windsurf_devin_roundtrip(monkeypatch, tmp_path):
    """C4 Verify: sandbox round-trip; config parses; JSONC comment survives; NO writes
    under ~/.codeium/ (legacy Cascade system untouched)."""
    home = _sandbox_home(monkeypatch, tmp_path)
    cfg = home / ".config" / "devin" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(DEVIN_CONFIG_JSONC)
    legacy = home / ".codeium" / "windsurf" / "mcp_config.json"
    legacy.parent.mkdir(parents=True)
    legacy_bytes = json.dumps({"mcpServers": {"other": {"serverUrl": "https://example.com"}}})
    legacy.write_text(legacy_bytes)
    legacy_listing = sorted(os.listdir(legacy.parent))

    rc = autoconfig.run_configure(platform="windsurf", yes=True)
    assert rc == 0
    text = cfg.read_text()
    assert "// devin user config — comment must survive" in text  # JSONC survives
    data = autoconfig.jsonc_read(str(cfg))  # config parses
    assert data["agent"]["model"] == "opus"
    assert data["permissions"]["allow"] == ["Exec(git)"] + [
        "mcp__ultramemory__%s" % t for t in UM_TOOLS
    ]
    assert data["permissions"]["deny"] == ["Exec(rm)"]  # deny/ask never touched
    assert data["mcpServers"]["other"] == {"url": "https://example.com", "transport": "http"}
    entry = data["hooks"]["SessionStart"][0]
    assert entry["hooks"][0]["command"].endswith("onboard-ultramemory.sh")
    # legacy Cascade config system UNTOUCHED — byte-identical, no new files
    assert legacy.read_text() == legacy_bytes
    assert sorted(os.listdir(legacy.parent)) == legacy_listing


# --------------------- A1: recall-first hook output shape (Claude vs Copilot) ----
_HOOK_RESPONSE = {
    "decision": "answer",
    "context_block": "hello from the shape test",
    "results": [{"fact_id": "f-shape-1"}],
}


def _serve_recall():
    """Local one-thread HTTP server standing in for POST /api/v1/recall/gated."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.dumps(_HOOK_RESPONSE).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep pytest output clean
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _run_hook(hook_path, home, base, prompt, shape=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ULTRAMEMORY_")}
    env.update({
        "HOME": str(home),  # sandbox ~/.ultramemory (cache + hook.log) — never real $HOME
        "ULTRAMEMORY_API_BASE": base,
        "ULTRAMEMORY_API_KEY": "um_test_shape",
        "ULTRAMEMORY_HOOK_LOG": "off",
        "no_proxy": "127.0.0.1,localhost",
    })
    if shape is not None:
        env["ULTRAMEMORY_HOOK_SHAPE"] = shape
    proc = subprocess.run(
        ["bash", hook_path],
        input=json.dumps({"prompt": prompt}),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize(
    "hook",
    [
        os.path.join(REPO, "hooks", "recall-first-hook.sh"),
        os.path.join(REPO, "agent-kit", "hooks", "recall-first-hook.sh"),
    ],
    ids=["hooks-copy", "agent-kit-copy"],
)
def test_a1_recall_hook_both_output_shapes(hook, tmp_path):
    """A1: default output = Claude hookSpecificOutput envelope (unchanged);
    ULTRAMEMORY_HOOK_SHAPE=copilot = ONLY the top-level additionalContext key."""
    srv = _serve_recall()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    home = tmp_path / "hookhome"
    home.mkdir()
    try:
        d = _run_hook(hook, home, base, "shape default")
        assert set(d) == {"hookSpecificOutput"}
        assert d["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert d["hookSpecificOutput"]["additionalContext"] == _HOOK_RESPONSE["context_block"]
        d = _run_hook(hook, home, base, "shape copilot", shape="copilot")
        assert set(d) == {"additionalContext"}
        assert d["additionalContext"] == _HOOK_RESPONSE["context_block"]
    finally:
        srv.shutdown()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
