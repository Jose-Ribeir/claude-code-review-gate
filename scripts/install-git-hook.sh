#!/usr/bin/env bash
#
# Installs the "everywhere" push gate: sets a global core.hooksPath whose
# pre-push hook runs review-gate on EVERY push (terminal, IDE, or Claude Code),
# in every repo. This is OPTIONAL — the plugin already gates pushes made
# through Claude Code without it.
#
# Revert with uninstall-git-hook.sh.
set -euo pipefail

SCR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOKS_DIR="${SCR_HOOKS_DIR:-$HOME/.config/review-gate/hooks}"

# Stamped into the installed hook purely so /review-gate:doctor can report
# version skew between this copy and the running plugin.
SCR_VERSION="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
  "$SCR_DIR/../.claude-plugin/plugin.json" 2>/dev/null | head -1)"
SCR_VERSION="${SCR_VERSION:-unknown}"

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

# Materialize the pre-push. Both substitutions are ADVISORY ONLY -- the copy
# resolves the reviewer at runtime (see _resolve_gate_dir there). That matters
# because this file is written once and never updated: baking the path in as a
# contract is what made every pre-0.3.0 install silently fail open when the
# plugin moved. The directory is kept as a last-resort hint, and the version is
# only ever read back by /review-gate:doctor.
sed -e "s|__SCR_DIR__|$SCR_DIR|g" -e "s|__SCR_VERSION__|$SCR_VERSION|g" \
  "$SCR_DIR/pre-push" > "$HOOKS_DIR/pre-push"
chmod +x "$HOOKS_DIR/pre-push"

git config --global core.hooksPath "$HOOKS_DIR"

cat <<EOF
Installed the global pre-push gate.
  hooks dir : $HOOKS_DIR
  reviewer  : $SCR_DIR/review-gate.py
  version   : $SCR_VERSION

WARNING: global core.hooksPath now applies to ALL your repositories and
overrides each repo's .git/hooks/pre-push (this hook still RUNS a repo-local
pre-push if one exists, so existing hooks keep working).

Modes:
  default              block confident high-severity findings
  OCR_ADVISORY=1       warn only, never block
  OCR_MODEL=haiku      cheaper review model (default: sonnet)
  git push --no-verify bypass entirely

The gate FAILS CLOSED: a review that times out (OCR_TIMEOUT, default 1800s),
crashes, or returns unparseable output BLOCKS the push rather than letting it
through. So does a missing Python 3 or a reviewer this hook cannot locate.

It still fails OPEN in these cases:
  - 'claude' is not installed        (deliberate: there is no gate without it)
  - OCR_FAIL_OPEN=1 / OCR_ADVISORY=1 (the escape hatches)
and it is BYPASSED entirely by 'git push --no-verify'.

If a broken reviewer ever traps you, OCR_FAIL_OPEN=1 is the one-shot bypass and
OCR_ADVISORY=1 downgrades to warn-only permanently.

Uninstall: $SCR_DIR/uninstall-git-hook.sh
EOF
