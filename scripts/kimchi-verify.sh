#!/usr/bin/env bash
#
# Real-kimchi verification: run this ONCE on a machine that has kimchi
# installed, from any directory, and paste back ./kimchi-verify-report.txt.
#
#   bash scripts/kimchi-verify.sh
#
# It prints kimchi's version and model list, runs the doctor, checks every
# effective kimchi model pin against the list, probes the `:high` / `:max`
# thinking suffix, and times a sample review (a tiny commit pushed through the
# real gate from a temp repo; your repos and global git config are untouched).
# Exit status is non-zero when any check fails. The logic lives in
# kimchi_verify.py beside this file; see its header for the KIMCHI_VERIFY_*
# knobs. Works in git-bash on Windows.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# A WORKING Python 3 (skip the Windows Store alias stubs).
PY=""
for c in python3 python py; do
  p="$(command -v "$c" 2>/dev/null)" || continue
  case "$p" in *[Ww]indows[Aa]pps*) continue ;; esac
  if "$p" -c "import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)" >/dev/null 2>&1; then PY="$p"; break; fi
done
if [ -z "$PY" ]; then
  echo "kimchi-verify: no working Python 3 found (tried python3, python, py). Install Python 3 and re-run." >&2
  exit 1
fi
exec "$PY" "$HERE/kimchi_verify.py" "$@"
