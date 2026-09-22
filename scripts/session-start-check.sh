#!/usr/bin/env bash
#
# SessionStart adapter for review-gate. Claude Code has no "on install" hook,
# so this is the earliest and only reliable place to warn a user that the
# push gate can't run -- before they hit a blocked `git push` and have to
# guess why. Fires on every session start/resume; that is a feature here, not
# noise: a missing Python 3 is a persistent environment problem that stays
# true across sessions until fixed, and stays silent the moment it is.
#
# Non-blocking by design (SessionStart cannot stop Claude Code from starting
# anyway) and prints nothing when a working Python is found. Sibling of
# session-start-check.ps1 -- same probe, same message. See gate-hook.sh for
# why two adapters exist (no platform-conditional mechanism for hooks).
set -uo pipefail

# Re-entry guard: the headless review Claude Code spawns via --plugin-dir has
# this plugin (and this hook) registered too. That session only exists
# because Python already ran it, so the probe is redundant there.
if [ "${OCR_IN_REVIEW:-}" = "1" ]; then
  exit 0
fi

# Same interpreter search as gate-hook.sh: skip Windows Store alias stubs.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
payload="$(cat 2>/dev/null || true)"
for _c in python3 python py; do
  _p="$(command -v "$_c" 2>/dev/null)" || continue
  case "$_p" in *[Ww]indows[Aa]pps*) continue ;; esac
  if "$_p" -c "import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)" >/dev/null 2>&1; then
    # Python is fine. If this session's previous process died mid-review (the
    # desktop app does that to a CLI silent for ~16 min), say so -- otherwise
    # the model reads the dangling tool call as a user interruption. Best
    # effort; the only acceptable outputs are one JSON object or nothing.
    if [ -f "$DIR/review-gate.py" ]; then
      printf '%s' "$payload" | "$_p" "$DIR/review-gate.py" --mode resume 2>/dev/null || true
    fi
    exit 0
  fi
done

cat <<'EOF'
review-gate: no working Python 3 interpreter found. The push gate FAILS CLOSED and will block `git push` until this is fixed.
  - Install Python 3, then restart Claude Code so it picks up the new PATH.
  - Run /review-gate:doctor for a full diagnosis.
  - Emergency bypass: set OCR_FAIL_OPEN=1 in the environment Claude Code itself was launched from.
EOF
exit 0
