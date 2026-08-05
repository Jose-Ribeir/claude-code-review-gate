#!/usr/bin/env bash
#
# Reverts install-git-hook.sh: restores any previous global core.hooksPath, or
# unsets it if there was none.
set -euo pipefail

PREV="$(git config --global --get reviewGate.prevHooksPath || true)"
if [ -n "$PREV" ]; then
  git config --global core.hooksPath "$PREV"
  git config --global --unset reviewGate.prevHooksPath || true
  echo "Restored previous global core.hooksPath: $PREV"
else
  git config --global --unset core.hooksPath || true
  echo "Removed global core.hooksPath (none was set before)."
fi

# Remove the hook files we materialized, so the gate cannot come back if
# core.hooksPath is ever pointed here again. `pre-commit` is from versions
# before the commit->push migration. Only delete files that are recognizably
# ours; never touch a hook someone else put there.
HOOKS_DIR="${SCR_HOOKS_DIR:-$HOME/.config/review-gate/hooks}"
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
