#!/usr/bin/env bash
#
# Reverts install-kimchi-hook.sh: removes the repo-level pre-push (restoring
# any preserved pre-push.user), the Kimchi bash hook, and the deny-extension
# slot if present. Only removes files that are recognizably ours; never
# touches a hook someone else put there.
#
# The verdict store (~/.local/state/review-gate/) is NOT removed: it holds
# review history. The janitor (a later chunk) owns its garbage collection.
set -euo pipefail

SCR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REPO="$(pwd)"
if [ "${1:-}" = "--repo" ]; then
  [ -d "${2:-}" ] || { echo "usage: uninstall-kimchi-hook.sh [--repo <path>]" >&2; exit 2; }
  REPO="$2"
fi
GITDIR="$(git -C "$REPO" rev-parse --absolute-git-dir 2>/dev/null)" || {
  echo "not a git repository: $REPO" >&2
  exit 2
}
HOOKS_DIR="$GITDIR/hooks"
# A3: the pre-push was installed into core.hooksPath when the repo sets one
# (install-kimchi-hook.sh honors it); uninstall from wherever git would
# actually run it. The default $GITDIR/hooks is still swept below for
# recognizably-ours leftovers from older, mis-placed installs.
HOOKS_CFG="$(git -C "$REPO" config --get core.hooksPath 2>/dev/null || true)"
if [ -n "$HOOKS_CFG" ]; then
  REPO_TOP="$(git -C "$REPO" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$REPO")"
  case "$HOOKS_CFG" in
    /*) CFG_HOOKS_DIR="$HOOKS_CFG" ;;
    *)  CFG_HOOKS_DIR="$REPO_TOP/$HOOKS_CFG" ;;
  esac
else
  CFG_HOOKS_DIR=""
fi

# (a) repo-level pre-push: restore the preserved user hook if there is one.
# Runs for the configured hooks dir (when set) and the default one, in that
# order; only files recognizably ours are ever touched.
_DIRS=()
if [ -n "$CFG_HOOKS_DIR" ]; then _DIRS+=("$CFG_HOOKS_DIR"); fi
if [ "$CFG_HOOKS_DIR" != "$HOOKS_DIR" ]; then _DIRS+=("$HOOKS_DIR"); fi
for dir in "${_DIRS[@]}"; do
  if [ -f "$dir/pre-push" ] && grep -q "review-gate.py" "$dir/pre-push" 2>/dev/null; then
    rm -f "$dir/pre-push"
    echo "Removed $dir/pre-push"
  fi
  if [ -f "$dir/pre-push.user" ]; then
    mv "$dir/pre-push.user" "$dir/pre-push"
    chmod +x "$dir/pre-push" 2>/dev/null || true
    echo "Restored your previous pre-push hook at $dir/pre-push."
  fi
done
KIMCHI_HOOKS_DIR="${KIMCHI_HOOKS_DIR:-$HOME/.config/kimchi/harness/hooks/bash}"
EXT_DIR="${OCR_EXT_DIR:-$HOME/.config/kimchi/harness/extensions}"
EXT_FILE="$EXT_DIR/review-gate-deny.ts"
STATE_DIR="${OCR_STATE_DIR:-$HOME/.local/state/review-gate}"

# (b) the Kimchi bash hook.
hook="$KIMCHI_HOOKS_DIR/review-gate.sh"
if [ -f "$hook" ] && grep -q "review-gate" "$hook" 2>/dev/null; then
  rm -f "$hook"
  echo "Removed $hook"
fi

# (c) the deny extension slot.
if [ -f "$EXT_FILE" ] && grep -q "review-gate" "$EXT_FILE" 2>/dev/null; then
  rm -f "$EXT_FILE"
  echo "Removed $EXT_FILE"
fi

# The runtime pointer.
if [ -f "$STATE_DIR/gate-dir" ]; then
  rm -f "$STATE_DIR/gate-dir"
  echo "Removed $STATE_DIR/gate-dir"
fi

echo "The review-gate is uninstalled for $REPO."
echo "The verdict store at $STATE_DIR was left in place (review history; a later"
echo "chunk ships the janitor that garbage-collects it)."
