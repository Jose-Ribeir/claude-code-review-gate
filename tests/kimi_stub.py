"""Stand-in for `kimchi -p "/skill:review ..."` in the Kimchi-port tests.

Mirrors tests/stub_reviewer.py's contract (the upstream test stub), trimmed to
what the C1 skeleton exercises: the gate machinery, not the chunking/segment
machinery (those keep using stub_reviewer.py through OCR_REVIEWER_CMD).

Invoked through OCR_STUB_WORKER=1 (or OCR_REVIEWER_CMD, which only honours a
script under this directory). Behaviour is driven by environment variables so
one command line serves every scenario:

  STUB_SLEEP         seconds to wait before answering (default 0)
  STUB_VERDICT       pass | warn | block | exit1 | garbage | limit  (default pass)
  STUB_TRACE         a file to append one line per invocation to (optional)
  STUB_RESOLVE       JSON mapping finding_id -> {status, evidence_path,
                     evidence_quote} (used when --resolve <file> is in argv)

The last non-flag argument is the range the gate asked to review; it is echoed
into the trace so a test can assert what was reviewed. Output is the verdict
JSON as text (the gate's _extract_json finds it), which is what a real
`kimchi -p ... --mode json` reply reduces to after extraction.
"""
import json
import os
import sys
import time

# Parse arguments: range is the last non-flag arg; --paths-file and --resolve are optional.
paths_file = None
resolve_file = None
args = sys.argv[1:]
i = 0
while i < len(args):
    if args[i] == "--paths-file" and i + 1 < len(args):
        paths_file = args[i + 1]
        i += 2
    elif args[i] == "--resolve" and i + 1 < len(args):
        resolve_file = args[i + 1]
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

resolve_input = None
if resolve_file:
    try:
        resolve_input = json.loads(open(resolve_file, encoding="utf-8").read())
    except Exception:
        pass

trace = os.environ.get("STUB_TRACE")
if trace:
    with open(trace, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "pid": os.getpid(), "cwd": os.getcwd(), "range": rng,
            "ts": time.time(), "paths_file": paths_file,
            "resolve_file": resolve_file,
            "resolve_manifest": resolve_input,
            "manifest": manifest,
        }) + "\n")

time.sleep(float(os.environ.get("STUB_SLEEP", "0") or 0))

finding = {
    "path": "app/x.py", "start_line": 1, "end_line": 2, "confidence": 0.95,
    "category": "correctness", "evidence": "stub",
}

# --- resolve mode -------------------------------------------------------
if resolve_file is not None:
    rv = os.environ.get("STUB_RESOLVE_VERDICT", "pass").strip().lower()
    if rv == "fail":
        sys.stdout.write("resolver stub: exit1\n")
        sys.exit(1)
    if rv == "garbage":
        sys.stdout.write("I could not resolve this.\n")
        sys.exit(0)
    scripted = {}
    raw_resolve = os.environ.get("STUB_RESOLVE", "")
    if (resolve_input or {}).get("recheck") and "STUB_RESOLVE_RECHECK" in os.environ:
        raw_resolve = os.environ["STUB_RESOLVE_RECHECK"]
    if raw_resolve:
        try:
            scripted = json.loads(raw_resolve)
        except Exception:
            pass
    priors = (resolve_input or {}).get("resolve") or []
    resolutions = {}
    for p in priors:
        fid = p.get("id") or ""
        if fid in scripted:
            resolutions[fid] = scripted[fid]
        else:
            resolutions[fid] = {"status": "still_present", "evidence_path": "", "evidence_quote": ""}
    sys.stdout.write(json.dumps({"resolutions": resolutions}))
    sys.exit(0)

# --- review mode --------------------------------------------------------
verdict = os.environ.get("STUB_VERDICT", "pass")

if verdict == "exit1":
    sys.stdout.write("Not logged in. Please run /login\n")
    sys.exit(1)
if verdict == "garbage":
    sys.stdout.write("I could not review this.\n")
    sys.exit(0)
if verdict == "limit":
    sys.stdout.write("You've hit your session limit · resets 3:20pm (Europe/Lisbon)\n")
    sys.exit(1)

findings = []
if verdict == "warn":
    findings = [dict(finding, severity="medium", content="stub medium finding")]
elif verdict == "block":
    findings = [dict(finding, severity="high", content="stub high finding")]

out = {"status": "success", "verdict": verdict, "findings": findings}
sys.stdout.write(json.dumps(out))
sys.exit(0)
