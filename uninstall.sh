#!/usr/bin/env bash
# UltraMemory Agent Kit — uninstaller. Manifest-driven: removes ONLY what install.sh recorded in
# ~/.ultramemory/install-manifest.json (MCP servers, hook/skill/agent files, settings.json hook
# entries, the managed CLAUDE.md block), reverts `ultramemory configure` setting rows to their
# recorded prior values, restores the .bak.<ts> copies it made, and never touches
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

strip_env_key(){ # file key created(true|false)
  local file="$1" key="$2" created="$3"; [ -f "$file" ] || return 0
  if [ "$DRYRUN" = 1 ]; then printf '  [dry-run] strip env %s from %s\n' "$key" "$file"; return 0; fi
  python3 - "$file" "$key" "$created" <<'PY'
import json,os,sys
f,key,created=sys.argv[1],sys.argv[2],sys.argv[3]=="true"
try: d=json.load(open(f))
except Exception: sys.exit(0)
env=d.get("env")
if isinstance(env,dict): env.pop(key,None)
if isinstance(env,dict) and not env: d.pop("env",None)
if created and not d:
    os.remove(f)
else:
    tmp=f+".tmp"; open(tmp,"w").write(json.dumps(d,indent=2)+"\n"); os.replace(tmp,f); os.chmod(f,0o600)
PY
}

restore_setting_rows(){ # revert manifest "setting" rows (written by `ultramemory configure`) to
  # their recorded prior values — surgical, key-level (mirrors strip_env_key: only the keys WE set
  # are touched; the user may have changed other keys since, and those are left alone). Rows are
  # {"type":"setting","platform":…,"path":…,"key":…,"prior":…,"new":…,"created":…,
  # "created_parents":[…]} where prior/new are {"value":v} or {"absent":true}; "created" true means
  # the apply created the whole file; "created_parents" lists the ancestor key-paths the apply
  # itself created (present only when non-empty). Non-plain-JSON targets defer to `ultramemory
  # configure --restore` (the engine has format-matched writers; this standalone helper stays
  # dependency-free).
  python3 - "$MANIFEST" "$DRYRUN" <<'PY'
import json,os,sys
mf,dry=sys.argv[1],sys.argv[2]=="1"
try: d=json.load(open(mf))
except Exception: sys.exit(0)
rows=[r for r in d.get("items",{}).get("setting",[]) if isinstance(r,dict) and r.get("path") and r.get("key")]
def prior(w):  # {"absent":true} | {"value":v} -> (absent, value)
    if isinstance(w,dict) and w.get("absent"): return True,None
    if isinstance(w,dict) and "value" in w: return False,w["value"]
    return False,w
def prune(cfg,created):  # 1.9.8 parity (autoconfig _prune_created_parents): after an absent-prior
    # leaf pop, drop the ancestor containers the APPLY ITSELF created that are now empty dicts —
    # deepest first (an emptied child may empty its parent). Ancestors NOT listed in
    # created_parents pre-existed the apply and are NEVER pruned; rows without the field no-op.
    if not isinstance(created,list): return
    for anc in sorted({str(a) for a in created if a and str(a).strip()},key=lambda a:len(a.split(".")),reverse=True):
        segs=anc.split("."); parent=cfg
        for seg in segs[:-1]:
            parent=parent.get(seg) if isinstance(parent,dict) else None
        if not isinstance(parent,dict): continue
        if isinstance(parent.get(segs[-1]),dict) and not parent[segs[-1]]: parent.pop(segs[-1],None)
for r in reversed(rows):
    p=os.path.expanduser(r["path"]); k=r["key"]
    if dry:
        extra=""
        if r.get("created_parents"): extra+="; would prune the created parent containers it left empty"
        if r.get("created"): extra+="; would remove the file if left empty (the apply created it)"
        print("  [dry-run] revert %s :: %s to its recorded prior%s"%(p,k,extra)); continue
    if not os.path.exists(p): continue
    if not p.endswith(".json"):
        print("  ! %s: not plain JSON — run `ultramemory configure --restore` to revert %s"%(p,k)); continue
    try: cfg=json.load(open(p))
    except Exception: print("  ! %s unreadable — skipped %s"%(p,k)); continue
    absent,val=prior(r.get("prior"))
    node=cfg; parts=k.split("."); ok=isinstance(node,dict)
    for seg in parts[:-1]:
        node=node.get(seg) if isinstance(node,dict) else None
        if not isinstance(node,dict): ok=False; break
    if not ok or not isinstance(node,dict): continue
    if absent:
        node.pop(parts[-1],None); prune(cfg,r.get("created_parents"))
    else: node[parts[-1]]=val
    if r.get("created") and isinstance(cfg,dict) and not cfg:  # mirror strip_env_key: the apply
        os.remove(p); print("  removed %s (the apply created it; empty after restore)"%p); continue
    tmp=p+".tmp"; open(tmp,"w").write(json.dumps(cfg,indent=2)+"\n"); os.replace(tmp,p)
    print("  restored %s :: %s"%(p,k))
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
for sub in recall-first-hook.sh harness-gate.sh harness-reminder.sh capture-hook.sh recall-rule-reminder.sh; do
  remove_settings_hook "./.claude/settings.json" "$sub"
  remove_settings_hook "$HOME/.claude/settings.json" "$sub"
done
c_ok "settings.json hook entries stripped"

# 2b. env keys the kit wrote into settings files (envkey manifest entries; strips ONLY our key —
# old manifests have no envkey list and degrade to the legacy `file` rm loop unchanged)
read_envkeys(){ python3 - "$MANIFEST" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
for e in d.get("items",{}).get("envkey",[]):
    if isinstance(e,dict) and e.get("path") and e.get("key"):
        print("%s\t%s\t%s"%(e["path"],e["key"],"true" if e.get("created") else "false"))
PY
}
read_envkeys | while IFS=$'\t' read -r ep ek ec; do
  [ -n "$ep" ] && { strip_env_key "$ep" "$ek" "$ec"; c_ok "stripped env $ek from $ep"; }
done

# 2c. "setting" rows written by `ultramemory configure` — surgically revert ONLY the keys we set
# to their recorded prior values (honest-uninstall parity; never a blind backup copy)
restore_setting_rows

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
