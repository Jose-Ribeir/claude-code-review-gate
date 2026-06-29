#!/usr/bin/env bash
#
# Installs the "everywhere" commit gate: sets a global core.hooksPath whose
# pre-commit runs review-gate on EVERY commit (terminal, IDE, or
# Claude Code), in every repo. This is OPTIONAL — the plugin already gates
# commits made through Claude Code without it.
#
# Revert with uninstall-git-hook.sh.
set -euo pipefail

BIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$BIN_DIR/.." && pwd)"
# A path the `claude` binary accepts on this OS (mixed-mode on Windows/Git Bash).
PLUGIN_ROOT_ARG="$(cygpath -m "$PLUGIN_ROOT" 2>/dev/null || echo "$PLUGIN_ROOT")"
HOOKS_DIR="${SCR_HOOKS_DIR:-$HOME/.config/review-gate/hooks}"

mkdir -p "$HOOKS_DIR"

PREV="$(git config --global --get core.hooksPath || true)"
if [ -n "$PREV" ] && [ "$PREV" != "$HOOKS_DIR" ]; then
  git config --global reviewGate.prevHooksPath "$PREV"
  echo "Backed up existing global core.hooksPath: $PREV"
fi

# Materialize the pre-commit with the plugin bin path + plugin root baked in.
sed -e "s|__SCR_BIN__|$BIN_DIR|g" -e "s|__PLUGIN_ROOT__|$PLUGIN_ROOT_ARG|g" \
  "$BIN_DIR/pre-commit" > "$HOOKS_DIR/pre-commit"
chmod +x "$HOOKS_DIR/pre-commit"

git config --global core.hooksPath "$HOOKS_DIR"

cat <<EOF
Installed the global pre-commit gate.
  hooks dir : $HOOKS_DIR
  reviewer  : $BIN_DIR/review-gate.py

WARNING: global core.hooksPath now applies to ALL your repositories and
overrides each repo's .git/hooks/pre-commit (this hook still RUNS a repo-local
pre-commit if one exists, so existing hooks keep working).

Modes:
  default            block confident high-severity findings
  OCR_ADVISORY=1     warn only, never block
  git commit --no-verify   bypass entirely

Uninstall: $BIN_DIR/uninstall-git-hook.sh
EOF
