"""Stand-in for `claude -p /review-gate:review ...` in the end-to-end tests.

Invoked through OCR_REVIEWER_CMD (see review-gate.py's _test_reviewer_cmd),
which only honours a script under this directory. Behaviour is driven by
environment variables so one command line serves every scenario:

  STUB_SLEEP    seconds to wait before answering (default 0)
  STUB_VERDICT  pass | warn | block | exit1 | garbage   (default pass)
  STUB_TRACE    a file to append one line per invocation to (optional)

The last argv element is the range the gate asked to review; it is echoed
into the trace so a test can assert what was reviewed.
"""
import json
import os
import sys
import time

rng = sys.argv[-1] if len(sys.argv) > 1 else ""
trace = os.environ.get("STUB_TRACE")
if trace:
    with open(trace, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"pid": os.getpid(), "cwd": os.getcwd(), "range": rng,
                             "ts": time.time()}) + "\n")

time.sleep(float(os.environ.get("STUB_SLEEP", "0") or 0))

verdict = os.environ.get("STUB_VERDICT", "pass")
if verdict == "exit1":
    sys.stdout.write("Not logged in. Please run /login\n")
    sys.exit(1)
if verdict == "garbage":
    sys.stdout.write("I could not review this.\n")
    sys.exit(0)

finding = {
    "path": "app/x.py", "start_line": 1, "end_line": 2, "confidence": 0.95,
    "category": "correctness", "evidence": "stub",
}
findings = []
if verdict == "warn":
    findings = [dict(finding, severity="medium", content="stub medium finding")]
elif verdict == "block":
    findings = [dict(finding, severity="high", content="stub high finding")]
sys.stdout.write(json.dumps({"status": "success", "verdict": verdict, "findings": findings}))
