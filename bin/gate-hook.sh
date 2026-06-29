#!/usr/bin/env bash
#
# PreToolUse adapter for the Claude Code commit gate (the default wiring).
# Referenced from hooks/hooks.json. Reads the PreToolUse payload on stdin and
# lets review-gate.py emit the permissionDecision JSON. Fails open.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Pick a WORKING Python interpreter; skip Windows Store alias stubs.
PY=""
for _c in python3 python py; do
  _p="$(command -v "$_c" 2>/dev/null)" || continue
  case "$_p" in *[Ww]indows[Aa]pps*) continue ;; esac
  if "$_p" -c "import sys" >/dev/null 2>&1; then PY="$_p"; break; fi
done

if [ -z "$PY" ] || [ ! -f "$DIR/review-gate.py" ]; then
  # Fail open: allow the commit if we can't run the reviewer.
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
  exit 0
fi

exec "$PY" "$DIR/review-gate.py" --mode hook
