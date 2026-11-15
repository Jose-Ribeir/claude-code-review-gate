"""Stand-in for `kimchi -p "/skill:review ..."` in the Kimchi-port tests.

The port's stub IS the upstream test stub's contract (the plan's stubbed tests
get a kimi-flavored stub "mirroring stub_reviewer.py"), and every knob the
segment / chunk / resolver suites script -- STUB_UNIT_FINDINGS, STUB_DEP_VERDICT,
STUB_CHUNK_VERDICT / STUB_CHUNK_SLEEP, STUB_FINDINGS_FOR, STUB_VERDICT_FOR,
STUB_STREAM, the item_files / cwd_diffs trace keys -- lives in
tests/stub_reviewer.py. Rather than keep a second, drifting copy of that
script, this shim executes it verbatim under this name: the gate's argv
(OCR_STUB_WORKER=1, see review-gate.py's _stub_worker_cmd) points HERE, so
one implementation serves either stub and the two can never disagree.

If a genuinely kimchi-specific reply shape is ever needed, branch on an env
var inside stub_reviewer.py (as STUB_VERDICT=limit's kimchi limit message
already does), never here.
"""
import os
import runpy
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
runpy.run_path(os.path.join(_HERE, "stub_reviewer.py"), run_name="__main__")
sys.exit(0)  # not reached: stub_reviewer.py always exits
