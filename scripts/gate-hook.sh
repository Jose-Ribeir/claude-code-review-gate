#!/usr/bin/env bash
#
# PreToolUse adapter for the Claude Code push gate (the default wiring).
# Referenced from hooks/hooks.json. Reads the PreToolUse payload on stdin and
# lets review-gate.py emit the permissionDecision JSON.
#
# Failure policy: FAIL CLOSED. If there is no working Python, or review-gate.py
# is missing, this denies the push rather than allowing it -- a gate that cannot
# run must not wave a push through. OCR_FAIL_OPEN=1 is the escape hatch.
# (Said "Fails open" until 0.3.0, which was true of the old body and is exactly
# the drift this release set out to remove.) Matches gate-hook.ps1.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Re-entry guard. review-gate.py runs the headless review with --plugin-dir, so
# this plugin -- including this very hook -- is registered inside the review
# session too. Without this, EVERY Bash call the reviewer makes spawns a Python
# process just to be told "not a push". review-gate.py guards it as well; this
# catches it before the spawn.
if [ "${OCR_IN_REVIEW:-}" = "1" ]; then
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
  exit 0
fi

# Pick a WORKING Python interpreter; skip Windows Store alias stubs.
PY=""
for _c in python3 python py; do
  _p="$(command -v "$_c" 2>/dev/null)" || continue
  case "$_p" in *[Ww]indows[Aa]pps*) continue ;; esac
  if "$_p" -c "import sys" >/dev/null 2>&1; then PY="$_p"; break; fi
done

if [ -z "$PY" ] || [ ! -f "$DIR/review-gate.py" ]; then
  # FAIL CLOSED. This used to allow, which quietly contradicted the whole
  # premise: a gate that cannot run is not a reason to wave a push through.
  # OCR_FAIL_OPEN=1 is the deliberate escape hatch.
  # `case` and `printf` are builtins. Nothing on this path may depend on an
  # external binary -- we are here precisely because the environment is broken.
  case "${OCR_FAIL_OPEN:-}" in
    1|[Tt][Rr][Uu][Ee]|[Yy][Ee][Ss])
      printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"}}'
      exit 0
      ;;
  esac
  # Built with printf, not python -- this is the branch where Python is the
  # thing that is missing. \n is escaped for JSON, and $DIR is the only
  # interpolation (a path, so no quoting hazard worth encoding around).
  if [ -z "$PY" ]; then
    why="no working Python 3 interpreter found"
  else
    why="review-gate.py missing at $DIR (broken install)"
  fi
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"review-gate: %s, so the push could not be reviewed. Blocking, because a gate that cannot run must not wave a push through.\\n\\nFix it:\\n  - Install Python 3 (or repair the plugin install), then retry.\\n  - Run /review-gate:doctor to see what is missing.\\n  - Emergency bypass: set OCR_FAIL_OPEN=1 in the environment Claude Code itself was launched from. A shell prefix on `git push` will NOT work -- this hook inherits Claude Code'"'"'s environment, not the Bash tool call'"'"'s.\\n  - Or push from a plain terminal, which this adapter does not gate."}}' "$why"
  exit 0
fi

exec "$PY" "$DIR/review-gate.py" --mode hook
