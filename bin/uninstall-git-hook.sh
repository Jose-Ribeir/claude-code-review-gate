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

echo "The 'everywhere' git gate is uninstalled. The Claude Code gate (plugin hook) is unaffected."
