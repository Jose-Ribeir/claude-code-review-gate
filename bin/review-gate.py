#!/usr/bin/env python3
#
# COMPATIBILITY SHIM — scheduled for removal in 0.5.0.
#
# The real implementation moved to ../scripts/review-gate.py in 0.3.0, because
# a plugin's bin/ directory is added to the Bash tool's PATH: every file in it
# becomes a bare command in every Bash call for every user with this plugin
# enabled. `pre-push` and `sync-local-install.py` had no business being there.
#
# This one file stays behind because removing it outright would have been a
# silent fail-open. Installs from before 0.3.0 baked an absolute path to this
# directory into $HOME/.config/review-gate/hooks/pre-push — a file outside the
# repo that no upgrade rewrites. With bin/ gone, that hook's `-f` test would
# fail, its RC would stay 0, and the push would sail through UNREVIEWED. An
# upgrade must not turn a working gate into an absent one.
#
# Newer pre-push copies resolve the reviewer at runtime and never reach here.
#
# To drop the shim early: re-run scripts/install-git-hook.sh, then delete bin/.
import os
import runpy
import sys

_REAL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "review-gate.py",
)

if not os.path.isfile(_REAL):
    # Fail CLOSED, consistent with the gate's policy: in git mode a non-zero
    # exit blocks the push. Hook mode is unreachable from a legacy pre-push,
    # which only ever invokes --mode git.
    sys.stderr.write(
        "[review-gate] BLOCKED: this is a compatibility shim and the real\n"
        "reviewer is missing at:\n  %s\n\n"
        "Re-run scripts/install-git-hook.sh to repair, or\n"
        "uninstall-git-hook.sh to remove the gate.\n"
        "Emergency bypass: OCR_FAIL_OPEN=1 git push\n" % _REAL
    )
    sys.exit(1)

# run_path rather than exec/import: the real module derives the plugin root from
# its own __file__, so it must believe it is running as scripts/review-gate.py.
sys.argv[0] = _REAL
runpy.run_path(_REAL, run_name="__main__")
