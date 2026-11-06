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
KIMCHI_HOOKS_DIR="${KIMCHI_HOOKS_DIR:-$HOME/.config/kimchi/harness/hooks/bash}"
EXT_DIR="${OCR_EXT_DIR:-$HOME/.config/kimchi/harness/extensions}"
EXT_FILE="$EXT_DIR/review-gate-deny.ts"
STATE_DIR="${OCR_STATE_DIR:-$HOME/.local/state/review-gate}"

# (a) repo-level pre-push: restore the preserved user hook if there is one.
if [ -f "$HOOKS_DIR/pre-push" ] && grep -q "review-gate.py" "$HOOKS_DIR/pre-push" 2>/dev/null; then
  rm -f "$HOOKS_DIR/pre-push"
  echo "Removed $HOOKS_DIR/pre-push"
fi
if [ -f "$HOOKS_DIR/pre-push.user" ]; then
  mv "$HOOKS_DIR/pre-push.user" "$HOOKS_DIR/pre-push"
  chmod +x "$HOOKS_DIR/pre-push" 2>/dev/null || true
  echo "Restored your previous pre-push hook."
fi

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
