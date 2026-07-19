#!/usr/bin/env bash
# UltraMemory Agent Kit — one-line guided installer.
#
#   bash <(curl -fsSL https://ultramemory.io/kit.sh)                 # interactive
#   bash <(curl -fsSL https://ultramemory.io/kit.sh) --tier 3 --non-interactive   # CI (reads $ULTRAMEMORY_API_KEY)
#   bash <(curl -fsSL https://ultramemory.io/kit.sh) --dry-run       # print the plan, change nothing
#   bash <(curl -fsSL https://ultramemory.io/kit.sh) --uninstall     # remove what the kit installed
#
# Tier 2 = Turbo Token Saver (recall-first hook + client cache, project-scoped).
# Tier 3 = Tier 2 + the harness (skills/subagents/hooks, global) + MCP (Context7 keyless / Exa BYO)
#          + optional Playwright Human Vision Control.
# Bring your own UltraMemory key (https://ultramemory.io). Prompts read from /dev/tty so they work
# even under `curl | bash`. All logic is inside main(), invoked last, so a truncated download can't
# execute a partial script. Idempotent; writes a manifest so `--uninstall` removes only what it added.
set -uo pipefail

KIT_VERSION="1.9.7"
REPO_RAW="${ULTRAMEMORY_KIT_RAW:-https://raw.githubusercontent.com/LogicLabsAI/ultramemory-mcp/main}"
API_BASE="${ULTRAMEMORY_API_BASE:-https://api.ultramemory.us}"
UM_DIR="$HOME/.ultramemory"
MANIFEST="$UM_DIR/install-manifest.json"
HOME_CLAUDE="$HOME/.claude"
TS="$(date +%Y%m%d%H%M%S 2>/dev/null || echo now)"

DRYRUN=0; NONINTERACTIVE=0; TIER=""; DO_UNINSTALL=0; WANT_EXA=""; WANT_PW=""
KEY="${ULTRAMEMORY_API_KEY:-}"; EXA_KEY=""

c_ok(){ printf '  \033[32m✓\033[0m %s\n' "$*"; }
c_info(){ printf '\033[36m›\033[0m %s\n' "$*"; }
c_warn(){ printf '\033[33m!\033[0m %s\n' "$*"; }
c_err(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; }
die(){ c_err "$*"; exit 1; }

# Ask on /dev/tty so prompts work under `curl | bash` (stdin is the piped script, not the keyboard).
ask(){ # prompt varname [silent]
  local p="$1" __v="$2" s="${3:-}" ans=""
  if [ "$NONINTERACTIVE" = 1 ] || [ ! -r /dev/tty ]; then eval "$__v=\"\${$__v:-}\""; return 0; fi
  if [ "$s" = silent ]; then printf '%s' "$p" >/dev/tty; IFS= read -rs ans </dev/tty; printf '\n' >/dev/tty
  else printf '%s' "$p" >/dev/tty; IFS= read -r ans </dev/tty; fi
  [ -n "$ans" ] && eval "$__v=\"\$ans\""
}
yesno(){ # prompt default(y/n) -> returns 0 for yes
  local p="$1" d="${2:-n}" a=""
  if [ "$NONINTERACTIVE" = 1 ] || [ ! -r /dev/tty ]; then [ "$d" = y ]; return; fi
  printf '%s [%s] ' "$p" "$([ "$d" = y ] && echo Y/n || echo y/N)" >/dev/tty
  IFS= read -r a </dev/tty || true; a="${a:-$d}"
  case "$a" in y|Y|yes|YES) return 0;; *) return 1;; esac
}

# act "description" -- cmd...   (in dry-run just prints; else runs)
act(){ local desc="$1"; shift; [ "$1" = "--" ] && shift
  if [ "$DRYRUN" = 1 ]; then printf '  [dry-run] %s\n' "$desc"; return 0; fi
  "$@"; }

# fetch <repo-relpath> <dest>  — from a local checkout if detected, else raw GitHub.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd 2>/dev/null || echo "")"
SRCBASE=""
[ -n "$SCRIPT_DIR" ] && [ -d "$SCRIPT_DIR/agent-kit" ] && SRCBASE="$SCRIPT_DIR"
fetch(){ # relpath dest — write to a same-dir temp, then atomically rename (new inode; a running hook keeps its old bytes)
  local rel="$1" dest="$2" dir tmp; dir="$(dirname "$dest")"; mkdir -p "$dir"
  tmp="$(mktemp "$dir/.um-fetch.XXXXXX")" || return 1
  if [ -n "$SRCBASE" ] && [ -f "$SRCBASE/$rel" ]; then cp "$SRCBASE/$rel" "$tmp"
  else curl -fsSL "$REPO_RAW/$rel" -o "$tmp" || { rm -f "$tmp"; return 1; }; fi
  chmod 644 "$tmp"
  mv -f "$tmp" "$dest"
}

# manifest accumulation (temp lines: "type\tvalue"), flushed to JSON at the end.
MFTMP=""; record(){ [ "$DRYRUN" = 1 ] && return 0; printf '%s\t%s\n' "$1" "$2" >> "$MFTMP"; }
backup(){ [ -f "$1" ] && { act "backup $1 -> $1.bak.$TS" -- cp "$1" "$1.bak.$TS"; record backup "$1.bak.$TS"; }; }

ensure_gitignore(){ # add a line to ./.gitignore if in a git repo and not present
  local line="$1"; { [ -e .git ] || git rev-parse --is-inside-work-tree >/dev/null 2>&1; } || return 0
  grep -qxF "$line" .gitignore 2>/dev/null && return 0
  act "gitignore += $line" -- sh -c "printf '%s\n' '$line' >> .gitignore"
}

# ---- python helpers (jq isn't stock on macOS; python3 is what the hook needs anyway) ----
merge_settings_hook(){ # file event command [timeout]  — idempotently add a command hook
  local file="$1" event="$2" cmd="$3" tmo="${4:-}"
  if [ "$DRYRUN" = 1 ]; then printf '  [dry-run] wire %s -> %s in %s\n' "$event" "$cmd" "$file"; return 0; fi
  mkdir -p "$(dirname "$file")"
  python3 - "$file" "$event" "$cmd" "$tmo" <<'PY'
import json,os,sys
f,event,cmd,tmo=sys.argv[1],sys.argv[2],sys.argv[3],sys.argv[4]
timeout=int(tmo) if tmo else (600 if event=="Stop" else 20)
d={}
if os.path.exists(f):
    try: d=json.load(open(f)) or {}
    except Exception: d={}
h=d.setdefault("hooks",{}); arr=h.setdefault(event,[])
for g in arr:
    for hk in g.get("hooks",[]):
        if hk.get("command")==cmd:
            open(f,"w").write(json.dumps(d,indent=2)+"\n"); sys.exit(0)
arr.append({"matcher":"","hooks":[{"type":"command","command":cmd,"timeout":timeout}]})
tmp=f+".tmp"; open(tmp,"w").write(json.dumps(d,indent=2)+"\n"); os.replace(tmp,f)
PY
}
set_env_key(){ # file KEY value  — write env var into a (git-ignored) settings json, chmod 600.
  # Prints "created" (file didn't exist) or "modified" on STDOUT for the envkey manifest record;
  # the [dry-run] line goes to STDERR so `$( )` capture stays clean.
  local file="$1" k="$2" v="$3"
  if [ "$DRYRUN" = 1 ]; then printf '  [dry-run] set %s in %s (env)\n' "$k" "$file" >&2; return 0; fi
  python3 - "$file" "$k" "$v" <<'PY'
import json,os,sys
f,k,v=sys.argv[1],sys.argv[2],sys.argv[3]
d={}; existed=os.path.exists(f)
if existed:
    try: d=json.load(open(f)) or {}
    except Exception: d={}
d.setdefault("env",{})[k]=v
tmp=f+".tmp"; open(tmp,"w").write(json.dumps(d,indent=2)+"\n"); os.replace(tmp,f)
os.chmod(f,0o600)
print("modified" if existed else "created")
PY
}
recall_rule_paragraph(){ # the active-recall rule text (mirrors agent-kit/templates/CLAUDE.md.tmpl:22-30)
  cat <<'RULE'
**Recall first — actively, not just passively.**
> UltraMemory recall-first hook injects prompt-relevant memory before each turn, but that is PASSIVE
> and prompt-scoped. For anything the project should already know (a URL, a prior decision, where a
> credential lives, whether we have done X, what the fix was last time) ACTIVELY call the
> `memory_recall` or `search` tool FIRST; never answer from working memory or say
> I-do-not-have-it / not-grounded-this-session without recalling. Persist durable facts and decisions
> with `memory_write`. Pair with current library/API docs (Context7) and current external facts
> (Exa) — that trio is the trifecta. The hook and this active-recall rule work TOGETHER; do not pick
> one.
RULE
}
recall_rule_block(){ # sentinel-wrapped managed block — embedded fetch fallback + exact manual paste
  printf '# --- UltraMemory harness (managed) ---\n\n'
  recall_rule_paragraph
  printf '# --- end UltraMemory harness ---\n'
}
place_claude_rule(){ # file  — insert the managed harness rule block if the sentinel isn't present
  local file="$1" tmpl="$UM_DIR/.CLAUDE.md.tmpl"
  if [ -f "$file" ] && grep -q 'UltraMemory harness (managed)' "$file" 2>/dev/null; then
    if grep -q 'Recall first — actively' "$file" 2>/dev/null; then c_ok "CLAUDE.md rule already present"; return 0; fi
    # upgrade path: managed block predates the recall rule — append the paragraph INSIDE the block
    if [ "$DRYRUN" = 1 ]; then printf '  [dry-run] append recall rule inside managed block in %s\n' "$file"; return 0; fi
    backup "$file"
    recall_rule_paragraph > "$file.rule.tmp"
    python3 - "$file" "$file.rule.tmp" <<'PY'
import os,sys
f,rp=sys.argv[1],sys.argv[2]
rule=open(rp).read()
out=[]; done=False
for ln in open(f).read().splitlines(True):
    if not done and ln.rstrip("\n")=="# --- end UltraMemory harness ---":
        out.append("\n"+rule); done=True
    out.append(ln)
tmp=f+".tmp"; open(tmp,"w").write("".join(out)); os.replace(tmp,f)
PY
    rm -f "$file.rule.tmp"
    record file "$file"
    c_ok "CLAUDE.md managed block upgraded with the recall rule"
    return 0
  fi
  if [ "$DRYRUN" = 1 ]; then printf '  [dry-run] insert harness rule into %s\n' "$file"; return 0; fi
  fetch agent-kit/templates/CLAUDE.md.tmpl "$tmpl" 2>/dev/null || recall_rule_block > "$tmpl"
  backup "$file"
  { [ -f "$file" ] && cat "$file"; printf '\n'; cat "$tmpl"; } > "$file.tmp" && mv "$file.tmp" "$file"
  record file "$file"
}

mcp_add(){ # name cmd...  — idempotent claude mcp add
  local name="$1"; shift
  if [ "$DRYRUN" = 1 ]; then printf '  [dry-run] claude mcp add %s\n' "$name"; return 0; fi
  if ! command -v claude >/dev/null 2>&1; then c_warn "claude CLI not found — skipping MCP '$name' (install Claude Code, then re-run)"; return 0; fi
  if claude mcp get "$name" >/dev/null 2>&1; then c_ok "MCP '$name' already configured"; return 0; fi
  act "claude mcp add $name" -- claude mcp "$@" && record mcp "$name"
}

verify_key(){ # returns 0 if the key works against the live recall endpoint
  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 \
    "$API_BASE/api/v1/recall/gated" \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d '{"query":"installer smoke test","k":1,"mode":"preview"}' 2>/dev/null || echo 000)"
  [ "$code" = 200 ] && { c_ok "recall check OK ($API_BASE/api/v1/recall/gated -> 200)"; return 0; }
  c_warn "recall check returned $code (401 = bad key; 000 = offline). Memory won't work until the key is valid."
  return 1
}

flush_manifest(){
  [ "$DRYRUN" = 1 ] && return 0
  mkdir -p "$UM_DIR"; chmod 700 "$UM_DIR" 2>/dev/null || true
  python3 - "$MANIFEST" "$KIT_VERSION" "$TIER" "$MFTMP" <<'PY'
import json,sys
mf,ver,tier,tmp=sys.argv[1:5]
items={"backup":[],"file":[],"mcp":[],"skilldir":[],"settings":[],"envkey":[]}
try:
    for ln in open(tmp):
        t,v=ln.rstrip("\n").split("\t",1); items.setdefault(t,[]).append(v)
except FileNotFoundError: pass
# envkey rows are "path\tkey\tcreated|modified" -> objects the uninstaller strips surgically
items["envkey"]=[{"path":p,"key":k,"created":c=="created"}
                 for p,k,c in (v.split("\t") for v in items["envkey"] if v.count("\t")==2)]
json.dump({"kit_version":ver,"tier":tier,"items":items},open(mf,"w"),indent=2); open(mf,"a").write("\n")
PY
  c_ok "manifest -> $MANIFEST"
}

# ---------------------------------------------------------------- tiers ----
install_tier2(){
  # Tier 1 — register the UltraMemory MCP (the eight memory tools). Idempotent + key-refresh.
  if command -v claude >/dev/null 2>&1; then
    if claude mcp get ultramemory 2>/dev/null | grep -q "api.ultramemory.us/mcp"; then
      act "refresh ultramemory MCP registration" -- claude mcp remove ultramemory
    fi
    mcp_add ultramemory add --transport http ultramemory https://api.ultramemory.us/mcp \
      --header "Authorization: Bearer $KEY"
  else
    c_warn "claude CLI not found — register UltraMemory manually:"
    printf '  claude mcp add --transport http ultramemory https://api.ultramemory.us/mcp \\\n    --header "Authorization: Bearer <your um_ key>"\n'
  fi
  c_info "Tier 2 — Turbo Token Saver (project-scoped recall hook + cache)"
  act "mkdir -p ./.claude/hooks" -- mkdir -p ./.claude/hooks
  if [ "$DRYRUN" != 1 ]; then
    fetch agent-kit/hooks/recall-first-hook.sh ./.claude/hooks/recall-first-hook.sh
    fetch agent-kit/hooks/cache.py ./.claude/hooks/cache.py
    fetch agent-kit/hooks/capture-hook.sh ./.claude/hooks/capture-hook.sh
    chmod +x ./.claude/hooks/recall-first-hook.sh ./.claude/hooks/capture-hook.sh
    record file "./.claude/hooks/recall-first-hook.sh"; record file "./.claude/hooks/cache.py"
    record file "./.claude/hooks/capture-hook.sh"
  else printf '  [dry-run] fetch recall-first-hook.sh + cache.py + capture-hook.sh into ./.claude/hooks\n'; fi
  merge_settings_hook "./.claude/settings.json" UserPromptSubmit '${CLAUDE_PROJECT_DIR}/.claude/hooks/recall-first-hook.sh'
  record settings "UserPromptSubmit:recall-first-hook.sh"
  merge_settings_hook "./.claude/settings.json" Stop '${CLAUDE_PROJECT_DIR}/.claude/hooks/capture-hook.sh' 20
  record settings "Stop:capture-hook.sh"
  created="$(set_env_key "./.claude/settings.local.json" ULTRAMEMORY_API_KEY "$KEY")"
  record envkey "$(printf '%s\t%s\t%s' "./.claude/settings.local.json" "ULTRAMEMORY_API_KEY" "${created:-modified}")"
  ensure_gitignore ".claude/settings.local.json"
  # Ship the active-recall CLAUDE.md rule alongside the hook (idempotent: place_claude_rule
  # no-ops if the managed sentinel is already present, so a later Tier-3 run won't double-insert).
  place_claude_rule "./CLAUDE.md"
  c_ok "Tier 2 wired (recall-first on every prompt)"
}

install_tier3(){
  install_tier2
  c_info "Tier 3 — harness + MCP + Playwright (global ~/.claude)"
  # harness skills
  for s in harness checklist-bound-execution atomic-level-checklist-md-confirm-approval-needed research swarm; do
    if [ "$DRYRUN" != 1 ]; then
      fetch "agent-kit/skills/$s/SKILL.md" "$HOME_CLAUDE/skills/$s/SKILL.md"
      record skilldir "$HOME_CLAUDE/skills/$s"
    else printf '  [dry-run] install skill %s -> ~/.claude/skills/%s\n' "$s" "$s"; fi
  done
  # checklist-bound-execution references
  for r in workflow-template.js schemas.md checklist-format.md; do
    [ "$DRYRUN" != 1 ] && fetch "agent-kit/skills/checklist-bound-execution/references/$r" "$HOME_CLAUDE/skills/checklist-bound-execution/references/$r"
  done
  # subagents
  for a in checklist-worker checklist-verifier; do
    if [ "$DRYRUN" != 1 ]; then fetch "agent-kit/agents/$a.md" "$HOME_CLAUDE/agents/$a.md"; record file "$HOME_CLAUDE/agents/$a.md"
    else printf '  [dry-run] install agent %s\n' "$a"; fi
  done
  # harness hooks (global)
  for h in harness-gate.sh harness-reminder.sh; do
    if [ "$DRYRUN" != 1 ]; then fetch "agent-kit/hooks/$h" "$HOME_CLAUDE/hooks/$h"; chmod +x "$HOME_CLAUDE/hooks/$h"; record file "$HOME_CLAUDE/hooks/$h"
    else printf '  [dry-run] install hook %s\n' "$h"; fi
  done
  merge_settings_hook "$HOME_CLAUDE/settings.json" Stop "$HOME/.claude/hooks/harness-gate.sh"
  merge_settings_hook "$HOME_CLAUDE/settings.json" UserPromptSubmit "$HOME/.claude/hooks/harness-reminder.sh"
  record settings "Stop:harness-gate.sh"
  # project rule + static gate
  place_claude_rule "./CLAUDE.md"
  if [ ! -f ./scripts/gate.sh ]; then
    if [ "$DRYRUN" != 1 ]; then fetch agent-kit/templates/gate.sh.tmpl ./scripts/gate.sh; chmod +x ./scripts/gate.sh; record file "./scripts/gate.sh"
    else printf '  [dry-run] write ./scripts/gate.sh\n'; fi
  fi
  ensure_gitignore ".claude/.harness-active"; ensure_gitignore ".claude/.harness-iter"
  # MCP: Context7 keyless
  mcp_add context7 add --scope user context7 -- npx -y @upstash/context7-mcp
  # MCP: Exa BYO
  [ -z "$WANT_EXA" ] && { yesno "Add the Exa MCP (web search — needs your Exa API key)?" n && WANT_EXA=y || WANT_EXA=n; }
  if [ "$WANT_EXA" = y ]; then
    [ -z "$EXA_KEY" ] && ask "  Exa API key (blank to skip): " EXA_KEY silent
    [ -n "$EXA_KEY" ] && mcp_add exa add --scope user --transport http --header "x-api-key: $EXA_KEY" exa https://mcp.exa.ai/mcp || c_warn "no Exa key — skipped"
  fi
  # Playwright Human Vision Control (our wrapper + the standard MCP on consent)
  [ -z "$WANT_PW" ] && { yesno "Install Playwright (visible browser control) + our Human Vision skill?" n && WANT_PW=y || WANT_PW=n; }
  if [ "$WANT_PW" = y ]; then
    mcp_add playwright add --scope user playwright -- npx @playwright/mcp@latest --caps=vision
    if [ "$DRYRUN" != 1 ]; then fetch agent-kit/skills/playwright-human-mode/SKILL.md "$HOME_CLAUDE/skills/playwright-human-mode/SKILL.md"; record skilldir "$HOME_CLAUDE/skills/playwright-human-mode"
    else printf '  [dry-run] install playwright-human-mode skill\n'; fi
    c_ok "Playwright Human Vision Control installed"
  fi
  c_ok "Tier 3 installed"
}

do_uninstall(){
  c_info "Uninstalling the UltraMemory Agent Kit"
  local un="$UM_DIR/.uninstall.sh"
  if [ -n "$SRCBASE" ] && [ -f "$SRCBASE/uninstall.sh" ]; then bash "$SRCBASE/uninstall.sh" "$@"; exit $?; fi
  mkdir -p "$UM_DIR"; curl -fsSL "$REPO_RAW/uninstall.sh" -o "$un" || die "could not fetch uninstall.sh"
  bash "$un" "$@"; exit $?
}

main(){
  while [ $# -gt 0 ]; do case "$1" in
    --dry-run) DRYRUN=1;;
    --non-interactive|-y) NONINTERACTIVE=1;;
    --tier) shift; TIER="${1:-}";;
    --tier=*) TIER="${1#*=}";;
    --key) shift; KEY="${1:-}";;
    --key=*) KEY="${1#*=}";;
    --exa) WANT_EXA=y;; --no-exa) WANT_EXA=n;;
    --playwright) WANT_PW=y;; --no-playwright) WANT_PW=n;;
    --uninstall) DO_UNINSTALL=1;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) c_warn "ignoring unknown arg: $1";;
  esac; shift; done

  [ "$DO_UNINSTALL" = 1 ] && do_uninstall $([ "$DRYRUN" = 1 ] && echo --dry-run)

  printf '\n\033[1mUltraMemory Agent Kit installer v%s\033[0m%s\n' "$KIT_VERSION" "$([ "$DRYRUN" = 1 ] && echo '  (dry-run)')"
  command -v curl >/dev/null 2>&1 || die "curl is required"
  command -v python3 >/dev/null 2>&1 || die "python3 is required"

  # key
  [ -z "$KEY" ] && ask "UltraMemory API key (um_… — get one free at https://ultramemory.io): " KEY silent
  [ -z "$KEY" ] && { [ "$DRYRUN" = 1 ] && KEY="um_DRYRUN"; }
  [ -z "$KEY" ] && die "no API key — pass --key um_… or set ULTRAMEMORY_API_KEY"
  case "$KEY" in um_*) : ;; *) c_warn "key doesn't start with um_ — continuing anyway";; esac

  # tier
  if [ -z "$TIER" ]; then
    if yesno "Install the full Agent Kit (Tier 3: harness + MCP + Playwright)? (No = Tier 2 recall only)" y; then TIER=3; else TIER=2; fi
  fi

  MFTMP="$(mktemp)"; trap 'rm -f "$MFTMP"' EXIT
  case "$TIER" in
    2) install_tier2;;
    3) install_tier3;;
    *) die "invalid tier: $TIER (use 2 or 3)";;
  esac

  flush_manifest
  c_info "Verifying…"
  [ "$DRYRUN" = 1 ] || verify_key || true
  # post-install verify: the recall rule must be in ./CLAUDE.md — WARN with the exact paste if not
  if [ "$DRYRUN" != 1 ] && ! grep -q 'Recall first — actively' ./CLAUDE.md 2>/dev/null; then
    c_warn "recall rule missing from ./CLAUDE.md — paste exactly this into ./CLAUDE.md:"
    recall_rule_block
  fi

  printf '\n'
  c_ok "Done (Tier $TIER)."
  c_info "Next: start a new Claude Code session in this project. Recall-first runs on every prompt."
  [ "$TIER" = 3 ] && c_info "Harness ready: on a multi-file build, ground a checklist then run the checklist-bound-execution skill."
  c_info "Uninstall anytime: bash <(curl -fsSL https://ultramemory.io/kit.sh) --uninstall"
}

main "$@"
