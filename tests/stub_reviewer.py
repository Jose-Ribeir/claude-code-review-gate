"""Stand-in for `claude -p /review-gate:review ...` in the end-to-end tests.

Invoked through OCR_REVIEWER_CMD (see review-gate.py's _test_reviewer_cmd),
which only honours a script under this directory. Behaviour is driven by
environment variables so one command line serves every scenario:

  STUB_SLEEP         seconds to wait before answering (default 0)
  STUB_VERDICT       pass | warn | block | exit1 | garbage | limit  (default pass)
  STUB_TRACE         a file to append one line per invocation to (optional)
  STUB_FAIL_ON_CALL  N — exit1 on the Nth call (1-based, counted via STUB_TRACE)
  STUB_VERDICT_FOR   path — the chunk containing this path returns block

The last non-flag argument is the range the gate asked to review; it is echoed
into the trace so a test can assert what was reviewed.
"""
import json
import os
import sys
import time

# Parse arguments: range is the last non-flag arg; --paths-file is optional.
paths_file = None
args = sys.argv[1:]
i = 0
while i < len(args):
    if args[i] == "--paths-file" and i + 1 < len(args):
        paths_file = args[i + 1]
        i += 2
    else:
        i += 1
rng = args[-1] if args and not args[-1].startswith("--") else ""

manifest = None
if paths_file:
    try:
        manifest = json.loads(open(paths_file, encoding="utf-8").read())
    except Exception:
        pass

trace = os.environ.get("STUB_TRACE")
if trace:
    with open(trace, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "pid": os.getpid(), "cwd": os.getcwd(), "range": rng,
            "ts": time.time(), "paths_file": paths_file,
            "manifest": manifest,
        }) + "\n")

# Check STUB_FAIL_ON_CALL: fail on the Nth invocation.
fail_on = os.environ.get("STUB_FAIL_ON_CALL", "")
if fail_on and trace:
    try:
        n = int(fail_on)
        with open(trace, encoding="utf-8") as fh:
            call_count = sum(1 for line in fh if line.strip())
        if call_count == n:
            sys.stdout.write("stub: forced failure on call " + str(n) + "\n")
            sys.exit(1)
    except Exception:
        pass

time.sleep(float(os.environ.get("STUB_SLEEP", "0") or 0))

# STUB_VERDICT_FOR: if this chunk contains the named path, return block.
verdict_for = os.environ.get("STUB_VERDICT_FOR", "")
verdict = os.environ.get("STUB_VERDICT", "pass")
if verdict_for and manifest:
    chunk_paths = manifest.get("paths") or []
    if verdict_for in chunk_paths:
        verdict = "block"

if verdict == "exit1":
    sys.stdout.write("Not logged in. Please run /login\n")
    sys.exit(1)
if verdict == "garbage":
    sys.stdout.write("I could not review this.\n")
    sys.exit(0)
if verdict == "limit":
    # Real limit output captured 2026-09-23.
    sys.stdout.write("You've hit your session limit · resets 3:20pm (Europe/Lisbon)\n")
    sys.exit(1)

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
