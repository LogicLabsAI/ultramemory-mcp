#!/usr/bin/env bash
# test-uninstall-parity.sh — U-02: sandboxed E2E proof of honest uninstall parity.
#
# Verifies uninstall.sh restore_setting_rows() undoes `ultramemory configure` setting rows
# byte-exactly, including created_parents pruning (1.9.8.1 parity with `configure --restore`):
#   case 1 CREATED-FILE    — the apply created the whole file -> uninstall removes it
#   case 2 CREATED-PARENTS — the apply created nested parents in a pre-existing file -> leaf +
#                            empty shells pruned; file byte-identical to the pre-install state
#   case 3 SIBLING-SAFE    — pre-existing parent holds a user sibling key -> sibling survives,
#                            parent is NOT pruned
#
# Sandboxed: NO network is touched. Each case runs with HOME redirected to its own fresh
# mktemp dir — uninstall.sh resolves the manifest HOME-relative ($HOME/.ultramemory/
# install-manifest.json, no env override exists), so HOME redirection IS the supported drive
# path. cwd is also moved into the temp HOME so uninstall.sh's ./.claude/settings.json and
# ./CLAUDE.md references stay in-sandbox. Fixture manifests are built directly (install.sh is
# NOT run). uninstall.sh has no confirm prompt; it is run non-dry, non-interactive (</dev/null).
# Each target's PRE-INSTALL state is byte-snapshotted first (file content, or recorded absence).
# Prints PASS/FAIL per case; exit 0 iff all pass.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNINSTALL="$REPO_DIR/uninstall.sh"
[ -f "$UNINSTALL" ] || { echo "FAIL: uninstall.sh not found at $UNINSTALL"; exit 1; }

ROOT="$(mktemp -d)"   # every byte this test writes lives under this fresh temp root
trap 'rm -rf "$ROOT"' EXIT
FAILS=0

write_json(){ # <path> <json> — write in the uninstaller's output format (indent=2 + trailing \n)
  python3 - "$1" "$2" <<'PY'
import json,os,sys
p,s=sys.argv[1],sys.argv[2]
os.makedirs(os.path.dirname(p),exist_ok=True)
open(p,"w").write(json.dumps(json.loads(s),indent=2)+"\n")
PY
}

json_eq(){ # <file-a> <file-b> — python json-load equality
  python3 - "$1" "$2" <<'PY'
import json,sys
sys.exit(0 if json.load(open(sys.argv[1]))==json.load(open(sys.argv[2])) else 1)
PY
}

no_empty_shells(){ # <file> — fail if any empty dict shell remains anywhere in the doc
  python3 - "$1" <<'PY'
import json,sys
def bad(x):
    if isinstance(x,dict): return (not x) or any(bad(v) for v in x.values())
    if isinstance(x,list): return any(bad(v) for v in x)
    return False
sys.exit(1 if bad(json.load(open(sys.argv[1]))) else 0)
PY
}

sibling_alive(){ # <file> — case 3: cfg["p"]["user"]==1 (parent not pruned, user sibling intact)
  python3 - "$1" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
sys.exit(0 if isinstance(d.get("p"),dict) and d["p"].get("user")==1 else 1)
PY
}

write_manifest(){ # <home> <row-json> — one "setting" row at the HOME-relative path uninstall.sh reads
  mkdir -p "$1/.ultramemory"
  write_json "$1/.ultramemory/install-manifest.json" "{\"items\":{\"setting\":[$2]}}"
}

run_uninstall(){ # <home> — non-dry, non-interactive; HOME + cwd sandboxed to the case dir
  ( cd "$1" && HOME="$1" bash "$UNINSTALL" </dev/null >"$1/uninstall.log" 2>&1 )
}

finish_case(){ # <ok 1|0> <label> <home> <why>
  if [ "$1" = 1 ]; then printf 'PASS: %s\n' "$2"
  else
    printf 'FAIL: %s —%s\n' "$2" "$4"
    [ -f "$3/uninstall.log" ] && sed 's/^/    | /' "$3/uninstall.log"
    FAILS=$((FAILS+1))
  fi
}

# ---- case 1: CREATED-FILE — apply created the whole file; uninstall must remove it ----------
H1="$(mktemp -d "$ROOT/case1.XXXXXX")"
T1="$H1/targets/created.json"
PRE1="absent"; [ -e "$T1" ] && PRE1="present"   # pre-install snapshot: recorded absence
write_manifest "$H1" "{\"type\":\"setting\",\"platform\":\"test\",\"path\":\"$T1\",\"key\":\"a.b.c\",\"prior\":{\"absent\":true},\"new\":{\"value\":1},\"created\":true,\"created_parents\":[\"a\",\"a.b\"]}"
write_json "$T1" '{"a":{"b":{"c":1}}}'          # post-apply state
ok=1; why=""
run_uninstall "$H1" || { ok=0; why="$why uninstall.sh exited non-zero;"; }
[ "$PRE1" = absent ] || { ok=0; why="$why fixture bug: target pre-existed;"; }
[ ! -e "$T1" ] || { ok=0; why="$why apply-created file still exists after uninstall;"; }
finish_case "$ok" "case 1 CREATED-FILE: apply-created file removed (matches recorded pre-install absence)" "$H1" "$why"

# ---- case 2: CREATED-PARENTS — pre-existing file; leaf + empty created shells pruned --------
H2="$(mktemp -d "$ROOT/case2.XXXXXX")"
T2="$H2/targets/settings.json"
write_json "$T2" '{"keep":1}'                   # pre-install state (tool output format)
cp "$T2" "$H2/pre.snapshot"                     # byte snapshot
write_manifest "$H2" "{\"type\":\"setting\",\"platform\":\"test\",\"path\":\"$T2\",\"key\":\"x.y.z\",\"prior\":{\"absent\":true},\"new\":{\"value\":2},\"created\":false,\"created_parents\":[\"x\",\"x.y\"]}"
write_json "$T2" '{"keep":1,"x":{"y":{"z":2}}}' # post-apply state
ok=1; why=""
run_uninstall "$H2" || { ok=0; why="$why uninstall.sh exited non-zero;"; }
if [ -f "$T2" ]; then
  cmp -s "$T2" "$H2/pre.snapshot" || { ok=0; why="$why not byte-identical to pre-install snapshot;"; }
  json_eq "$T2" "$H2/pre.snapshot" || { ok=0; why="$why json != pre-install state;"; }
  no_empty_shells "$T2" || { ok=0; why="$why empty dict shell(s) remain;"; }
else ok=0; why="$why target missing after uninstall;"; fi
finish_case "$ok" "case 2 CREATED-PARENTS: leaf reverted + empty shells pruned; byte-identical to pre-state" "$H2" "$why"

# ---- case 3: SIBLING-SAFE — pre-existing parent with a user sibling stays untouched ---------
H3="$(mktemp -d "$ROOT/case3.XXXXXX")"
T3="$H3/targets/settings.json"
write_json "$T3" '{"p":{"user":1}}'             # pre-install state (tool output format)
cp "$T3" "$H3/pre.snapshot"                     # byte snapshot
write_manifest "$H3" "{\"type\":\"setting\",\"platform\":\"test\",\"path\":\"$T3\",\"key\":\"p.ours\",\"prior\":{\"absent\":true},\"new\":{\"value\":3},\"created\":false,\"created_parents\":[]}"
write_json "$T3" '{"p":{"user":1,"ours":3}}'    # post-apply state
ok=1; why=""
run_uninstall "$H3" || { ok=0; why="$why uninstall.sh exited non-zero;"; }
if [ -f "$T3" ]; then
  sibling_alive "$T3" || { ok=0; why="$why p.user sibling lost or parent p pruned;"; }
  cmp -s "$T3" "$H3/pre.snapshot" || { ok=0; why="$why not byte-identical to pre-install snapshot;"; }
  json_eq "$T3" "$H3/pre.snapshot" || { ok=0; why="$why json != pre-install state;"; }
else ok=0; why="$why target missing after uninstall (parent p was pruned);"; fi
finish_case "$ok" "case 3 SIBLING-SAFE: p.user survives, pre-existing parent p not pruned" "$H3" "$why"

printf '\n'
if [ "$FAILS" -eq 0 ]; then echo "test-uninstall-parity: ALL 3 CASES PASS"; exit 0
else echo "test-uninstall-parity: $FAILS case(s) FAILED"; exit 1; fi
