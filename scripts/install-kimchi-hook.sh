#!/usr/bin/env bash
#
# Installs the review-gate for Kimchi Studio, in a repository:
#   (a) the repo-level git pre-push hook (the PRIMARY gate -- it sees every
#       push that reaches git from anywhere: terminals, scripts, heredocs,
#       `gh`, GUIs);
#   (b) the Kimchi bash hook at ~/.config/kimchi/harness/hooks/bash/
#       review-gate.sh (the UX layer -- surfaces the block reason to the
#       agent verbatim). It is inert outside repositories that installed (a).
#   (c) the deny extension slot (~/.config/kimchi/harness/extensions/
#       review-gate-deny.ts) -- reserved; the extension lands with a later
#       chunk and this is only its install path.
#
# Run from inside the repository to gate (or pass --repo <path>).
# Revert with uninstall-kimchi-hook.sh.
#
# Ported from scripts/install-git-hook.sh (claude-code-review-gate).
set -euo pipefail

SCR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # .../scripts
GATE_ROOT="$(cd "$SCR_DIR/.." && pwd)"

REPO="$(pwd)"
if [ "${1:-}" = "--repo" ]; then
  [ -d "${2:-}" ] || { echo "usage: install-kimchi-hook.sh [--repo <path>]" >&2; exit 2; }
  REPO="$2"
fi
GITDIR="$(git -C "$REPO" rev-parse --absolute-git-dir 2>/dev/null)" || {
  echo "not a git repository: $REPO" >&2
  exit 2
}
HOOKS_DIR="$GITDIR/hooks"
mkdir -p "$HOOKS_DIR"

KIMCHI_HOOKS_DIR="${KIMCHI_HOOKS_DIR:-$HOME/.config/kimchi/harness/hooks/bash}"
EXT_DIR="${OCR_EXT_DIR:-$HOME/.config/kimchi/harness/extensions}"
EXT_FILE="$EXT_DIR/review-gate-deny.ts"
STATE_DIR="${OCR_STATE_DIR:-$HOME/.local/state/review-gate}"

# Version stamped into the installed hook (advisory only).
SCR_VERSION="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
  "$GATE_ROOT/.claude-plugin/plugin.json" 2>/dev/null | head -1)"
SCR_VERSION="${SCR_VERSION:-unknown}"

# (a) repo-level pre-push. Preserve any pre-existing hook (renamed to
# pre-push.user; our hook chains into it).
PRE_PUSH="$HOOKS_DIR/pre-push"
if [ -f "$PRE_PUSH" ] && ! grep -q "review-gate.py" "$PRE_PUSH" 2>/dev/null; then
  mv "$PRE_PUSH" "$HOOKS_DIR/pre-push.user"
  echo "Preserved existing pre-push hook as $HOOKS_DIR/pre-push.user (chained)."
fi
sed -e "s|__SCR_DIR__|$SCR_DIR|g" -e "s|__SCR_VERSION__|$SCR_VERSION|g" \
  "$GATE_ROOT/hooks/git/pre-push" > "$PRE_PUSH"
chmod +x "$PRE_PUSH"

# (b) the Kimchi bash hook (UX layer).
mkdir -p "$KIMCHI_HOOKS_DIR"
sed -e "s|__GATE_DIR__|$SCR_DIR|g" \
  "$GATE_ROOT/hooks/sh/gate-hook.sh" > "$KIMCHI_HOOKS_DIR/review-gate.sh"
chmod +x "$KIMCHI_HOOKS_DIR/review-gate.sh"

# (c) the deny extension slot (C3 placeholder: install path only).
if [ ! -f "$EXT_FILE" ]; then
  mkdir -p "$EXT_DIR" 2>/dev/null || true
  echo "note: deny extension slot reserved at $EXT_FILE (ships with a later chunk; nothing installed now)"
fi

# The runtime pointer both hooks resolve when their stamped path goes stale.
mkdir -p "$STATE_DIR"
printf '%s' "$SCR_DIR" > "$STATE_DIR/gate-dir"
chmod 700 "$STATE_DIR" 2>/dev/null || true

cat <<EOF
Installed the review-gate (Kimchi port).
  repo          : $REPO
  pre-push      : $PRE_PUSH (PRIMARY gate)
  kimchi hook   : $KIMCHI_HOOKS_DIR/review-gate.sh (UX layer, inert outside gated repos)
  ext slot      : $EXT_FILE (reserved)
  reviewer      : $SCR_DIR/review-gate.py
  version       : $SCR_VERSION

The gate FAILS CLOSED: a review that times out, crashes, or returns
unparseable output BLOCKS the push rather than letting it through, as do a
missing Python 3 or a reviewer this hook cannot locate.

Bypasses:
  git push --no-verify   skip hooks entirely for one push (the human escape)
  OCR_FAIL_OPEN=1        one-shot fail-open escape hatch
  OCR_ADVISORY=1         warn only, never block

Uninstall: $SCR_DIR/uninstall-kimchi-hook.sh
EOF
