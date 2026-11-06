#!/usr/bin/env bash
#
# PostToolUse adapter for the Claude Code push gate. Sibling of gate-hook.sh,
# and referenced from hooks/hooks.json. Runs AFTER a `git push` has actually
# happened and replays whatever the review already recorded, so the findings
# land in the session's context instead of in a terminal stream.
#
# Why this exists at all: verified 2026-09 against Claude Code's transcripts, a
# PreToolUse `permissionDecisionReason` on an ALLOW decision never reaches the
# model -- only a DENY does. So blocking findings were always seen and
# non-blocking ones never were, no matter how loudly they were printed: the
# terminal copy is one `| tail` (or one noisy pre-push hook) away from
# invisible. A PostToolUse hook returning additionalContext is the one channel
# that delivers, and it does not care where in the output stream it sits.
#
# Failure policy: FAIL OPEN -- the exact opposite of gate-hook.sh, deliberately.
# gate-hook decides whether a push proceeds, so a gate that cannot run must
# block. This one decides only whether a report is printed. A reporting path
# that cannot run must go quiet: it must never block a push, and must never put
# anything but a JSON object on a stdout that Claude Code parses.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Re-entry guard. review-gate.py runs the headless review with --plugin-dir, so
# this plugin -- including this hook -- is registered inside the review session
# too, where it would fire on every Bash call the reviewer makes.
if [ "${OCR_IN_REVIEW:-}" = "1" ]; then
  exit 0
fi

# Read the payload once and short-circuit non-pushes before touching Python.
# hooks.json deliberately carries no `if:` clause for this entry -- that
# mechanism is documented to fail open when it cannot parse a pattern, so it
# would run anyway -- which means this hook fires on EVERY Bash call. The
# substring check is what keeps that cheap. Same rule review-gate.py applies.
payload="$(cat)"
case "$payload" in
  *'git push'*) ;;
  *)
    # Not a push -- but a review may still be PARKED from one that already
    # happened and never got reported, because PostToolUse does not fire for a
    # tool call that failed. Flushing that is the whole reason this hook runs
    # on commands other than pushes.
    #
    # The glob below is the entire cheap test: no subprocess, no Python, and on
    # the overwhelmingly common path -- nothing parked -- it costs one readdir.
    # Mirrors _gate_data_dir() in review-gate.py; keep the two in step.
    _data="${CLAUDE_PLUGIN_DATA:-}"
    if [ -z "$_data" ]; then
      _data="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/data/review-gate-local"
    fi
    set -- "$_data"/pending-*
    [ -e "$1" ] || exit 0
    ;;
esac

# Pick a WORKING Python interpreter; skip Windows Store alias stubs.
PY=""
for _c in python3 python py; do
  _p="$(command -v "$_c" 2>/dev/null)" || continue
  case "$_p" in *[Ww]indows[Aa]pps*) continue ;; esac
  if "$_p" -c "import sys" >/dev/null 2>&1; then PY="$_p"; break; fi
done

# No Python, or a broken install: say nothing. Unlike the gate, there is
# nothing here worth failing closed over -- the findings are still in
# .git/review-gate-findings.jsonl and `--history` still replays them.
if [ -z "$PY" ] || [ ! -f "$DIR/review-gate.py" ]; then
  exit 0
fi

printf '%s' "$payload" | "$PY" "$DIR/review-gate.py" --mode post || true
exit 0
