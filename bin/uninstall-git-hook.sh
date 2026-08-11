#!/usr/bin/env bash
#
# Reverts install-git-hook.sh: restores any previous global core.hooksPath, or
# unsets it if there was none.
set -euo pipefail

HOOKS_DIR="${SCR_HOOKS_DIR:-$HOME/.config/review-gate/hooks}"

# Canonicalize before comparing: git stores whatever string was set (e.g.
# "C:/Users/me/...") while $HOME expands to "/c/Users/me/..." under Git Bash.
_canon() { [ -d "$1" ] && (cd "$1" 2>/dev/null && pwd) || printf '%s' "$1"; }

PREV="$(git config --global --get reviewGate.prevHooksPath || true)"

# Installers before the _canon fix could record OUR OWN hooks dir as the
# "previous" path (the string forms differed, so the self-check missed). Blindly
# restoring that points core.hooksPath straight back at us and the uninstall
# silently no-ops. Detect the poisoned value and unset instead of restoring.
if [ -n "$PREV" ] && [ "$(_canon "$PREV")" = "$(_canon "$HOOKS_DIR")" ]; then
  echo "Ignoring stale self-referencing backup ($PREV) written by an older installer."
  PREV=""
  git config --global --unset reviewGate.prevHooksPath || true
fi

if [ -n "$PREV" ]; then
  git config --global core.hooksPath "$PREV"
  git config --global --unset reviewGate.prevHooksPath || true
  echo "Restored previous global core.hooksPath: $PREV"
else
  git config --global --unset core.hooksPath || true
  echo "Removed global core.hooksPath."
fi

# Remove the hook files we materialized, so the gate cannot come back if
# core.hooksPath is ever pointed here again. `pre-commit` is from versions
# before the commit->push migration. Only delete files that are recognizably
# ours; never touch a hook someone else put there.
for _h in pre-push pre-commit; do
  _f="$HOOKS_DIR/$_h"
  if [ -f "$_f" ] && grep -q "review-gate" "$_f" 2>/dev/null; then
    rm -f "$_f"
    echo "Removed $_f"
  fi
done
# Clean up the directory if we left it empty (ignore failure if not).
rmdir "$HOOKS_DIR" 2>/dev/null || true

echo "The 'everywhere' git gate is uninstalled. The Claude Code gate (plugin hook) is unaffected."
