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
KIMCHI_HOOKS_DIR="${KIMCHI_HOOKS_DIR:-$HOME/.config/kimchi/harness/hooks/bash}"
EXT_DIR="${OCR_EXT_DIR:-$HOME/.config/kimchi/harness/extensions}"
EXT_FILE="$EXT_DIR/review-gate-deny.ts"
STATE_DIR="${OCR_STATE_DIR:-$HOME/.local/state/review-gate}"

# Version stamped into the installed hook (advisory only).
SCR_VERSION="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
  "$GATE_ROOT/.claude-plugin/plugin.json" 2>/dev/null | head -1)"
SCR_VERSION="${SCR_VERSION:-unknown}"

# (a) repo-level pre-push. Git runs hooks from core.hooksPath when the repo
# sets one (husky, lefthook, a hand-rolled scripts/git-hooks); installing
# into $GITDIR/hooks there would be a SILENT MIS-INSTALL -- git would never
# run our hook. Honor the configured path, resolved the way git resolves it
# (relative to the repo's work-tree top).
HOOKS_CFG="$(git -C "$REPO" config --get core.hooksPath 2>/dev/null || true)"
if [ -n "$HOOKS_CFG" ]; then
  REPO_TOP="$(git -C "$REPO" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$REPO")"
  case "$HOOKS_CFG" in
    /*) HOOKS_DIR="$HOOKS_CFG" ;;
    *)  HOOKS_DIR="$REPO_TOP/$HOOKS_CFG" ;;
  esac
  echo "core.hooksPath is set to $HOOKS_CFG; installing the pre-push into $HOOKS_DIR"
else
  HOOKS_DIR="$GITDIR/hooks"
fi
if ! mkdir -p "$HOOKS_DIR" 2>/dev/null; then
  cat >&2 <<EOF
ERROR: cannot create the hook directory $HOOKS_DIR (from core.hooksPath =
$HOOKS_CFG). The pre-push gate was NOT installed: git would not run a hook
from $GITDIR/hooks while core.hooksPath points elsewhere, so refusing to
mis-install it there. Fix the permissions on $HOOKS_DIR (or clear
  git -C "$REPO" config --unset core.hooksPath
if that setting is stale), then re-run this installer.
EOF
  exit 1
fi
if ! [ -w "$HOOKS_DIR" ] || ! [ -x "$HOOKS_DIR" ]; then
  cat >&2 <<EOF
ERROR: the hook directory $HOOKS_DIR (from core.hooksPath = $HOOKS_CFG) is
not writable, so the pre-push gate cannot be installed there. It was NOT
installed into $GITDIR/hooks either: git would never run it from there while
core.hooksPath points elsewhere. Grant write access to $HOOKS_DIR (or unset
core.hooksPath), then re-run this installer.
EOF
  exit 1
fi

# Preserve any pre-existing hook (renamed to pre-push.user; our hook chains
# into it).
PRE_PUSH="$HOOKS_DIR/pre-push"
if [ -f "$PRE_PUSH" ] && ! grep -q "review-gate.py" "$PRE_PUSH" 2>/dev/null; then
  if ! mv "$PRE_PUSH" "$HOOKS_DIR/pre-push.user" 2>/dev/null; then
    cat >&2 <<EOF
ERROR: an existing pre-push hook lives at $PRE_PUSH and could not be
preserved as $HOOKS_DIR/pre-push.user (permissions?). Nothing was installed:
overwriting your hook without chaining it would discard your checks. Fix the
permissions on $HOOKS_DIR, then re-run this installer.
EOF
    exit 1
  fi
  echo "Preserved existing pre-push hook as $HOOKS_DIR/pre-push.user (chained)."
fi
if ! sed -e "s|__SCR_DIR__|$SCR_DIR|g" -e "s|__SCR_VERSION__|$SCR_VERSION|g" \
    "$GATE_ROOT/hooks/git/pre-push" > "$PRE_PUSH" 2>/dev/null; then
  echo "ERROR: could not write $PRE_PUSH (permissions?). The gate was NOT installed." >&2
  exit 1
fi
chmod +x "$PRE_PUSH" || { echo "ERROR: could not chmod +x $PRE_PUSH." >&2; exit 1; }

# Stale-copy cleanup: when core.hooksPath points elsewhere, git never runs a
# hook left in the default $GITDIR/hooks -- a gate copy there is a shadowed
# lie (the gate appears installed while pushes run ungated). Remove it, but
# only after the new install succeeded (the gate must never go absent) and
# only when it is recognizably ours; a foreign hook is never touched.
if [ "$HOOKS_DIR" != "$GITDIR/hooks" ] && [ -f "$GITDIR/hooks/pre-push" ] \
   && grep -q "review-gate.py" "$GITDIR/hooks/pre-push" 2>/dev/null; then
  rm -f "$GITDIR/hooks/pre-push"
  echo "Removed a stale review-gate pre-push from $GITDIR/hooks:"
  echo "  core.hooksPath points at $HOOKS_DIR, so git never ran that copy."
fi

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
