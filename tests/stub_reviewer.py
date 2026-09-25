"""Stand-in for `claude -p /review-gate:review ...` in the end-to-end tests.

Invoked through OCR_REVIEWER_CMD (see review-gate.py's _test_reviewer_cmd),
which only honours a script under this directory. Behaviour is driven by
environment variables so one command line serves every scenario:

  STUB_SLEEP         seconds to wait before answering (default 0)
  STUB_VERDICT       pass | warn | block | exit1 | garbage | limit  (default pass)
  STUB_TRACE         a file to append one line per invocation to (optional)
  STUB_FAIL_ON_CALL  N — exit1 on the Nth call (1-based, counted via STUB_TRACE)
  STUB_VERDICT_FOR   path — the chunk containing this path returns block
  STUB_FINDINGS_FOR  JSON mapping path → {severity, content} for path-scripted verdicts
  STUB_RESOLVE       JSON mapping finding_id → {status, evidence_path, evidence_quote}
                     Used when --resolve <file> is in argv; return resolver output.
                     Values are passed through verbatim, so a malformed answer
                     (e.g. `true`) can be scripted too.
  STUB_RESOLVE_RECHECK like STUB_RESOLVE, but used instead of it when the resolver
                     manifest has "recheck": true (the gate's second look).
  STUB_RESOLVE_VERDICT pass|fail|garbage  Controls resolver exit for all ids (default pass).

The last non-flag argument is the range the gate asked to review; it is echoed
into the trace so a test can assert what was reviewed.
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

# Determine per-file mode from manifest.
def _get_file_modes(m):
    """Return {path: mode} from manifest.files if present."""
    if not m:
        return {}
    files = m.get("files") or []
    return {f["path"]: f.get("mode", "full") for f in files if isinstance(f, dict)}

file_modes = _get_file_modes(manifest)

trace = os.environ.get("STUB_TRACE")
if trace:
    with open(trace, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "pid": os.getpid(), "cwd": os.getcwd(), "range": rng,
            "ts": time.time(), "paths_file": paths_file,
            "resolve_file": resolve_file,
            "resolve_manifest": resolve_input,
            "manifest": manifest,
            "file_modes": file_modes,
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

# --- resolve mode -------------------------------------------------------
if resolve_file is not None:
    rv = os.environ.get("STUB_RESOLVE_VERDICT", "pass").strip().lower()
    if rv == "fail":
        sys.stdout.write("resolver stub: exit1\n")
        sys.exit(1)
    if rv == "garbage":
        sys.stdout.write("I could not resolve this.\n")
        sys.exit(0)
    # Build resolutions from STUB_RESOLVE env var or mark all still_present.
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

# STUB_FINDINGS_FOR: per-path scripted findings (path → {severity, content}).
findings_for_raw = os.environ.get("STUB_FINDINGS_FOR", "")
if findings_for_raw:
    try:
        findings_map = json.loads(findings_for_raw)
        # With a manifest use its paths; without (golden argv), apply to all configured paths.
        chunk_paths = (manifest.get("paths") or []) if manifest else list(findings_map.keys())
        for path in chunk_paths:
            if path in findings_map:
                spec = findings_map[path]
                findings.append({
                    "path": path,
                    "start_line": 1, "end_line": 2, "confidence": 0.95,
                    "category": "correctness", "evidence": "stub",
                    "severity": spec.get("severity", "high"),
                    "content": spec.get("content", "stub scripted finding"),
                    "existing_code": spec.get("existing_code", "stub code"),
                })
    except Exception:
        pass

out = {"status": "success", "verdict": verdict, "findings": findings}

# 0.9.0 manifest fields. STUB_IMPACT_BREAK=1 reports every impact site as
# broken (a high finding anchored at the call site); otherwise each is "ok".
# STUB_CONFIRM_SIBLINGS=1 confirms every known defect as the same defect.
# STUB_CROSS_FILE is echoed verbatim as the §2b summary.
impact = (manifest or {}).get("impact") or {}
if impact.get("sites"):
    broken = os.environ.get("STUB_IMPACT_BREAK") == "1"
    out["impact_verdicts"] = {s["id"]: ("broken" if broken else "ok") for s in impact["sites"]}
    if broken:
        for s in impact["sites"]:
            findings.append({
                "path": s["path"], "start_line": s["line"], "end_line": s["line"],
                "severity": "high", "confidence": 0.95, "category": "correctness",
                "content": f"caller broken by the change to {s['name']}",
                "existing_code": s.get("text") or "", "evidence": "impact_sites",
                "impact_site": s["id"],
            })
if os.environ.get("STUB_CONFIRM_SIBLINGS") == "1":
    for d in (manifest or {}).get("known_defects") or []:
        findings.append({
            "path": d["path"], "start_line": d["line"], "end_line": d["line"],
            "severity": "high", "confidence": 0.95, "category": "security",
            "content": "same defect as the earlier finding", "existing_code": d.get("text") or "",
            "evidence": "known_defects", "sibling_of": d["sid"],
        })
if os.environ.get("STUB_CROSS_FILE"):
    out["cross_file_context_summary"] = json.loads(os.environ["STUB_CROSS_FILE"])
sys.stdout.write(json.dumps(out))
