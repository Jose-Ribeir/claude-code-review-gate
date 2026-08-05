#!/usr/bin/env bash
#
# Installs the "everywhere" push gate: sets a global core.hooksPath whose
# pre-push hook runs review-gate on EVERY push (terminal, IDE, or Claude Code),
# in every repo. This is OPTIONAL — the plugin already gates pushes made
# through Claude Code without it.
#
# Revert with uninstall-git-hook.sh.
set -euo pipefail

BIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOKS_DIR="${SCR_HOOKS_DIR:-$HOME/.config/review-gate/hooks}"

mkdir -p "$HOOKS_DIR"

# Canonicalize before comparing: git stores whatever string was set (e.g.
# "C:/Users/me/..." on Windows) while $HOME expands to "/c/Users/me/..." under
# Git Bash. A plain string compare sees those as different directories, so
# re-running the installer would "back up" our own hooks dir over itself and
# uninstall would then restore core.hooksPath to us instead of unsetting it.
_canon() { [ -d "$1" ] && (cd "$1" 2>/dev/null && pwd) || printf '%s' "$1"; }

PREV="$(git config --global --get core.hooksPath || true)"
if [ -n "$PREV" ] && [ "$(_canon "$PREV")" != "$(_canon "$HOOKS_DIR")" ]; then
  git config --global reviewGate.prevHooksPath "$PREV"
  echo "Backed up existing global core.hooksPath: $PREV"
fi

# Versions of this plugin before the commit->push migration installed a
# pre-commit hook here. Left in place it still fires on every commit, and
# because the gate now reviews `@{u}..HEAD` it would review the WRONG state at
# commit time (HEAD is the parent; the staged changes are invisible). Remove
# ours if we recognize it; leave anything unrecognized alone and warn.
STALE="$HOOKS_DIR/pre-commit"
if [ -f "$STALE" ]; then
  if grep -q "review-gate" "$STALE" 2>/dev/null; then
    rm -f "$STALE"
    echo "Removed stale review-gate pre-commit hook (superseded by pre-push)."
  else
    echo "WARNING: $STALE exists and is not ours — leaving it in place."
  fi
fi

# Materialize the pre-push with the plugin bin path baked in. The plugin root
# no longer needs substituting: review-gate.py derives it from its own location
# and passes --plugin-dir itself (see DEFAULT_CLAUDE_ARGS).
sed -e "s|__SCR_BIN__|$BIN_DIR|g" "$BIN_DIR/pre-push" > "$HOOKS_DIR/pre-push"
chmod +x "$HOOKS_DIR/pre-push"

git config --global core.hooksPath "$HOOKS_DIR"

cat <<EOF
Installed the global pre-push gate.
  hooks dir : $HOOKS_DIR
  reviewer  : $BIN_DIR/review-gate.py

WARNING: global core.hooksPath now applies to ALL your repositories and
overrides each repo's .git/hooks/pre-push (this hook still RUNS a repo-local
pre-push if one exists, so existing hooks keep working).

Modes:
  default              block confident high-severity findings
  OCR_ADVISORY=1       warn only, never block
  OCR_MODEL=haiku      cheaper review model (default: sonnet)
  git push --no-verify bypass entirely

Uninstall: $BIN_DIR/uninstall-git-hook.sh
EOF
