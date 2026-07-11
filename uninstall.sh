#!/usr/bin/env bash
# UltraMemory Agent Kit — uninstaller. Manifest-driven: removes ONLY what install.sh recorded in
# ~/.ultramemory/install-manifest.json (MCP servers, hook/skill/agent files, settings.json hook
# entries, the managed CLAUDE.md block), restores the .bak.<ts> copies it made, and never touches
# your own rules or config. Idempotent (missing = skip).
#
#   uninstall.sh              # remove
#   uninstall.sh --dry-run    # print the removal plan, change nothing
set -uo pipefail

UM_DIR="$HOME/.ultramemory"
MANIFEST="$UM_DIR/install-manifest.json"
DRYRUN=0
[ "${1:-}" = "--dry-run" ] && DRYRUN=1
DRYLBL=""; [ "$DRYRUN" = 1 ] && DRYLBL=" (dry-run)"

c_ok(){ printf '  \033[32m✓\033[0m %s\n' "$*"; }
c_info(){ printf '\033[36m›\033[0m %s\n' "$*"; }
c_warn(){ printf '\033[33m!\033[0m %s\n' "$*"; }
act(){ local d="$1"; shift; [ "$1" = "--" ] && shift; if [ "$DRYRUN" = 1 ]; then printf '  [dry-run] %s\n' "$d"; else "$@"; fi; }

[ -f "$MANIFEST" ] || { c_warn "no manifest at $MANIFEST — nothing recorded to remove."; exit 0; }

read_field(){ python3 - "$MANIFEST" "$1" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); [print(x) for x in d.get("items",{}).get(sys.argv[2],[])]
PY
}

remove_settings_hook(){ # file substr
  local file="$1" sub="$2"; [ -f "$file" ] || return 0
  if [ "$DRYRUN" = 1 ]; then printf '  [dry-run] strip hooks matching %s from %s\n' "$sub" "$file"; return 0; fi
  python3 - "$file" "$sub" <<'PY'
import json,os,sys
f,sub=sys.argv[1],sys.argv[2]
try: d=json.load(open(f))
except Exception: sys.exit(0)
h=d.get("hooks",{})
for ev in list(h):
    groups=[]
    for g in h[ev]:
        g["hooks"]=[hk for hk in g.get("hooks",[]) if sub not in (hk.get("command") or "")]
        if g["hooks"]: groups.append(g)
    if groups: h[ev]=groups
    else: del h[ev]
if not h: d.pop("hooks",None)
if d: json.dump(d,open(f,"w"),indent=2); open(f,"a").write("\n")
else: os.remove(f)
PY
}

strip_claude_block(){ # file
  local file="$1"; [ -f "$file" ] || return 0
  grep -q 'UltraMemory harness (managed)' "$file" 2>/dev/null || return 0
  if [ "$DRYRUN" = 1 ]; then printf '  [dry-run] remove managed harness block from %s\n' "$file"; return 0; fi
  python3 - "$file" <<'PY'
import sys,re
f=sys.argv[1]; t=open(f).read()
t=re.sub(r"\n?# --- UltraMemory harness \(managed\) ---.*?# --- end UltraMemory harness ---\n?", "\n", t, flags=re.S)
open(f,"w").write(t)
PY
}

c_info "Uninstalling UltraMemory Agent Kit$DRYLBL"

# 1. MCP servers
if command -v claude >/dev/null 2>&1; then
  read_field mcp | while IFS= read -r name; do
    [ -n "$name" ] || continue
    if [ "$DRYRUN" = 1 ]; then printf '  [dry-run] claude mcp remove %s\n' "$name"; continue; fi
    claude mcp remove "$name" >/dev/null 2>&1 && c_ok "removed MCP $name"
  done
else c_warn "claude CLI not found — skipping MCP removal"; fi

# 2. settings.json hook entries (project + global)
for sub in recall-first-hook.sh harness-gate.sh harness-reminder.sh; do
  remove_settings_hook "./.claude/settings.json" "$sub"
  remove_settings_hook "$HOME/.claude/settings.json" "$sub"
done
c_ok "settings.json hook entries stripped"

# 3. managed CLAUDE.md block
strip_claude_block "./CLAUDE.md"

# 4. files
read_field file | while IFS= read -r f; do [ -n "$f" ] && [ -e "$f" ] && { act "rm $f" -- rm -f "$f"; c_ok "removed $f"; }; done

# 5. skill dirs
read_field skilldir | while IFS= read -r d; do [ -n "$d" ] && [ -d "$d" ] && { act "rm -rf $d" -- rm -rf "$d"; c_ok "removed $d"; }; done

# 6. restore backups
read_field backup | while IFS= read -r b; do
  [ -n "$b" ] && [ -f "$b" ] && { orig="${b%.bak.*}"; act "restore $orig from $b" -- cp "$b" "$orig"; c_ok "restored $orig"; }
done

# 7. runtime cache + manifest
[ -f "$HOME/.ultramemory/cache.json" ] && act "rm ~/.ultramemory/cache.json" -- rm -f "$HOME/.ultramemory/cache.json"
act "rm manifest" -- rm -f "$MANIFEST"

printf '\n'; c_ok "Uninstall complete$([ "$DRYRUN" = 1 ] && echo ' (dry-run — nothing changed)' || echo '')."
c_info "Your own CLAUDE.md rules, settings, and files were left untouched."
