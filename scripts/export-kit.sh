#!/usr/bin/env bash
# export-kit.sh — curate the private ~/.claude harness source of truth into the public agent-kit/.
#
#   scripts/export-kit.sh            # export: copy allowlisted files, regenerate templates +
#                                    #   onboarding, write kit-manifest.json, then deny-scan (fail-closed)
#   scripts/export-kit.sh --check    # drift check: re-hash live ~/.claude sources vs the manifest;
#                                    #   nonzero exit if the kit is stale (source changed) or a dest is missing
#
# Only files in scripts/kit-manifest.in are copied (explicit allowlist, never a wildcard). Two
# in-repo skills (swarm, playwright-human-mode) are hand-curated and NOT copied here — they are
# still deny-scanned. The deny-scan is the safety net: any personal path / secret / business token
# ANYWHERE under agent-kit/ hard-fails the export so nothing private can ship by accident.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
SRC="${ULTRAMEMORY_HARNESS_SRC:-$HOME/.claude}"
KIT="$REPO/agent-kit"
ALLOW="$SCRIPT_DIR/kit-manifest.in"
MANIFEST="$KIT/kit-manifest.json"
KIT_VERSION="1.9.7"
MODE="${1:-export}"

sha() { if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'; else sha256sum "$1" | awk '{print $1}'; fi; }
die() { printf 'export-kit: %s\n' "$*" >&2; exit 1; }

[ -f "$ALLOW" ] || die "allowlist not found: $ALLOW"
[ -d "$SRC" ]   || die "harness source not found: $SRC (set ULTRAMEMORY_HARNESS_SRC)"

# ---------------------------------------------------------------- --check (drift) ----
if [ "$MODE" = "--check" ]; then
  [ -f "$MANIFEST" ] || die "no manifest to check ($MANIFEST) — run export first"
  drift=0
  # For every allowlisted source, compare the live source sha to the source_sha256 recorded in the
  # manifest. A mismatch means ~/.claude changed and the shipped kit is stale.
  while IFS='|' read -r src dest; do
    case "$src" in ''|\#*) continue ;; esac
    src="$(printf '%s' "$src" | xargs)"; dest="$(printf '%s' "$dest" | xargs)"
    live="$SRC/$src"
    [ -f "$live" ] || { echo "DRIFT: source missing: $src"; drift=1; continue; }
    live_sha="$(sha "$live")"
    rec_sha="$("$REPO/scripts/.mf-get.py" "$MANIFEST" "$dest" source_sha256 2>/dev/null || true)"
    if [ -z "$rec_sha" ]; then echo "DRIFT: $dest not in manifest"; drift=1; continue; fi
    if [ "$live_sha" != "$rec_sha" ]; then echo "DRIFT: $src changed since last export ($dest is stale)"; drift=1; fi
    [ -f "$KIT/$dest" ] || { echo "DRIFT: exported file missing: $dest"; drift=1; }
  done < "$ALLOW"
  [ "$drift" -eq 0 ] && { echo "export --check: no drift"; exit 0; } || exit 1
fi

# ---------------------------------------------------------------- export ----
echo "export-kit: source=$SRC  kit=$KIT  version=$KIT_VERSION"
ROWS="$(mktemp)"; trap 'rm -f "$ROWS"' EXIT

while IFS='|' read -r src dest; do
  case "$src" in ''|\#*) continue ;; esac
  src="$(printf '%s' "$src" | xargs)"; dest="$(printf '%s' "$dest" | xargs)"
  live="$SRC/$src"
  [ -f "$live" ] || die "allowlisted source missing: $live"
  mkdir -p "$KIT/$(dirname "$dest")"
  cp "$live" "$KIT/$dest"
  printf '%s|%s|%s|%s\n' "$dest" "$(sha "$KIT/$dest")" "$src" "$(sha "$live")" >> "$ROWS"
  echo "  copied  $src -> agent-kit/$dest"
done < "$ALLOW"

# --- first-party Tier-2 hooks (from THIS repo, not ~/.claude) so the plugin is self-contained ---
# Tier 3 includes Tier 2 (Turbo Token Saver): the recall-first hook + client cache travel with the
# kit. These are already public in this repo; copy them next to the harness hooks in the kit.
for f in hooks/recall-first-hook.sh hooks/capture-hook.sh hooks/recall-rule-reminder.sh; do
  if [ -f "$REPO/$f" ]; then mkdir -p "$KIT/hooks"; cp "$REPO/$f" "$KIT/$f"; echo "  bundled agent-kit/$f (first-party)"; fi
done
if [ -f "$REPO/cache.py" ]; then cp "$REPO/cache.py" "$KIT/hooks/cache.py"; echo "  bundled agent-kit/hooks/cache.py (first-party)"; fi

# --- generated: settings.hooks.json (installer wiring for a GLOBAL ~/.claude install) ---
mkdir -p "$KIT/templates"
cat > "$KIT/templates/settings.hooks.json" <<'JSON'
{
  "//": "Merge these into ~/.claude/settings.json (global) — the UltraMemory Agent Kit installer does this for you. Paths assume the kit's hooks are installed under ~/.claude/hooks/.",
  "hooks": {
    "UserPromptSubmit": [
      { "matcher": "", "hooks": [
        { "type": "command", "command": "$HOME/.claude/hooks/recall-first-hook.sh", "timeout": 20 },
        { "type": "command", "command": "$HOME/.claude/hooks/recall-rule-reminder.sh", "timeout": 5 },
        { "type": "command", "command": "$HOME/.claude/hooks/harness-reminder.sh", "timeout": 5 }
      ] }
    ],
    "Stop": [
      { "matcher": "", "hooks": [
        { "type": "command", "command": "$HOME/.claude/hooks/harness-gate.sh", "timeout": 600 }
      ] }
    ]
  }
}
JSON
echo "  wrote   agent-kit/templates/settings.hooks.json"

# --- generated: HARNESS-ONBOARDING.md (generic placement-contract quickstart) ---
cat > "$KIT/HARNESS-ONBOARDING.md" <<'MD'
# Harness onboarding (generic drop-in)

The UltraMemory Agent Kit installs the harness *machinery* (skills, subagents, hooks) globally
under `~/.claude/`. This doc is the last-mile step that wires the **routing rule** and a **static
gate** into a specific project. Everything here is READ-ONLY until you say "go".

1. **Verify** the global machinery exists: `~/.claude/skills/{harness,checklist-bound-execution}`,
   `~/.claude/agents/{checklist-worker,checklist-verifier}.md`, `~/.claude/hooks/harness-gate.sh`.
2. **Place the routing rule** from `templates/CLAUDE.md.tmpl` into your project's `CLAUDE.md`
   (Placement Contract): if a `0.x` hard-rules band exists, add it as the next free `0.x` right
   above RULE 1; else create the band as RULE 0.7. Never renumber existing rules.
3. **Author a static gate** `scripts/gate.sh` from `templates/gate.sh.tmpl` — STATIC only (build /
   lint / tests / type-check; never deploy or paid/live calls); fail loud on a missing validator.
4. **Smoke-test** it: clean tree exits 0; plant one invalid file → nonzero; revert → 0.
5. **Gitignore** `.claude/.harness-active` and `.claude/.harness-iter`.

Then, on any multi-file build: ground an atomic checklist, get approval, and run
`checklist-bound-execution` — it arms the `.claude/.harness-active` Stop gate and loops bound
workers + an adversarial verifier until the gate is green.
MD
echo "  wrote   agent-kit/HARNESS-ONBOARDING.md"

# --- write kit-manifest.json (dest sha + source sha, for integrity + drift) ---
python3 - "$MANIFEST" "$KIT_VERSION" "$ROWS" <<'PY'
import json, sys
manifest, version, rows = sys.argv[1], sys.argv[2], sys.argv[3]
files = []
for line in open(rows):
    line = line.rstrip("\n")
    if not line:
        continue
    dest, dsha, src, ssha = line.split("|", 3)
    files.append({"path": "agent-kit/" + dest, "sha256": dsha, "source": src, "source_sha256": ssha, "mode": "copy"})
json.dump({"kit": "ultramemory-agent-kit", "version": version, "files": files}, open(manifest, "w"), indent=2)
open(manifest, "a").write("\n")
print("  wrote   agent-kit/kit-manifest.json (%d files)" % len(files))
PY

# --- tiny helper the --check path uses to read a field back out of the manifest ---
cat > "$SCRIPT_DIR/.mf-get.py" <<'PY'
#!/usr/bin/env python3
import json, sys
mf, dest, field = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.load(open(mf))
for f in data.get("files", []):
    if f.get("path") == "agent-kit/" + dest or f.get("path") == dest:
        print(f.get(field, "")); break
PY
chmod +x "$SCRIPT_DIR/.mf-get.py"

# ---------------------------------------------------------------- deny-scan (fail-closed) ----
echo "export-kit: deny-scan agent-kit/ ..."
scan_fail=0
run_scan() { # label  extended-regex
  hits="$(grep -rEn "$2" "$KIT" 2>/dev/null | grep -v 'kit-manifest.json' | head -5 || true)"
  if [ -n "$hits" ]; then echo "  DENY  $1"; printf '        %s\n' "$hits"; scan_fail=1; else echo "  ok    no $1"; fi
}
run_scan "personal path"     '/Users/jameslindsay'
run_scan "supabase secret"   'sb_secret_'
run_scan "um_ live key"      'um_[A-Za-z0-9]{12,}'
run_scan "sk- secret"        'sk-[A-Za-z0-9]{12,}'
run_scan "aws key"           'AKIA[A-Z0-9]{12,}'
run_scan "business token"    'optimusclaw|openclaw|discord|digitalocean|droplet|coupscout|mindflows'
run_scan "private-skill ref" 'mac-mini-browser|\.env\.local|fast-supervised-browser|Hard Rule #'
[ "$scan_fail" -eq 0 ] || die "deny-scan FAILED — refusing to ship private content (fix the flagged files)"

echo "export-kit: OK"
