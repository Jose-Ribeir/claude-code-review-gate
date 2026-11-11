"""t19 end-to-end intent fixture: a lying commit message meets the gate.

One tiny two-commit repo: the second commit's message claims a fix the diff
does not contain ("fix: retry on timeout", but the diff only adds an unrelated
logging helper). The gate runs against the canned stub reviewer (no live
model): the stub's intent_gap verdict is seeded through STUB_INTENT_FINDINGS,
the same technique test_ledger_scenarios.py / test_impact_gate.py use for
STUB_FINDINGS_FOR / STUB_VERDICT.

External behaviour pinned here (spec: Testing Decisions -> End-to-end):

  a. the assembled manifest carries the chunk-scoped commit_intent claim
     (sha, subject, body, touched files) for the pushed commit;
  b. the final decision BLOCKS on a well-formed self-checked blocking
     intent_gap, and a warning intent_gap alone warns but lets the push
     through;
  c. the recorded review (the append-only findings log) keeps the intent
     finding's contracted fields verbatim;
  d. the block/warn text marks intent findings distinctly ([intent-blocking]
     / [intent-warning]), quoting the claim -- never blurred with a plain
     code defect.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_STUB = os.path.join(_HERE, "stub_reviewer.py")

sys.path.insert(0, _SCRIPTS)
from hook_output import parse_pretooluse  # noqa: E402
_spec = importlib.util.spec_from_file_location("review_gate_intent_e2e", _GATE)
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)


# ---------------------------------------------------------------------------
# Tiny two-commit fixture repo (same shape as test_impact_gate.py / ledger)
# ---------------------------------------------------------------------------

def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


_SUBJECT = "fix: retry on timeout"
_BODY = "The request path now retries transient timeouts with backoff."
_BASE_SRC = "def fetch(url):\n    return http_get(url)\n"
_LIE_SRC = (_BASE_SRC + "\n\n# unrelated helper: nothing here retries\n"
            "def log_request(msg):\n    print(msg)\n")
_AMENDED_SRC = _LIE_SRC.replace("print(msg)", "print(msg.upper())")


def _two_commit_repo(tmp_path):
    """First commit pushed to origin; second commit (the lying one) unpushed."""
    remote, work = tmp_path / "origin.git", tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    for k, v in (("user.email", "t@t.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(["config", k, v], cwd=work)
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    _git(["config", "core.hooksPath", str(hooks)], cwd=work)
    (work / "retry.py").write_text(_BASE_SRC, encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    # The lie: the message claims a retry fix; the diff adds a logging helper.
    (work / "retry.py").write_text(_LIE_SRC, encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", _SUBJECT, "-m", _BODY], cwd=work)
    return work


def _env(tmp_path, trace, **kw):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OCR_", "STUB_", "GIT_TRACE"))}
    env.update({"CLAUDE_PLUGIN_DATA": str(tmp_path / "gate-data"),
                "OCR_REVIEWER_CMD": f'"{sys.executable}" "{_STUB}"',
                "STUB_TRACE": str(tmp_path / trace), "OCR_INLINE_BUDGET": "30"})
    env.update({k: str(v) for k, v in kw.items()})
    return env


def _push(work, env, cmd="git push -f origin main"):
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash", "cwd": str(work),
                          "tool_input": {"command": cmd}})
    proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                          capture_output=True, text=True, cwd=str(work), env=env, timeout=90)
    assert proc.returncode == 0, proc.stderr
    return parse_pretooluse(proc.stdout)


def _state(work, tip, timeout=60):
    common = review_gate._git_common_dir(str(work))
    path = review_gate._state_path(common, tip)
    deadline = time.monotonic() + timeout
    while True:
        st = review_gate._read_state(path) or {}
        if st.get("state") in ("done", "failed") or time.monotonic() > deadline:
            return st
        time.sleep(0.2)


def _calls(tmp_path, trace):
    p = tmp_path / trace
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def _review_manifest(tmp_path, trace):
    return next((c.get("manifest") or {}) for c in _calls(tmp_path, trace)
                if not c.get("resolve_file"))


# ---------------------------------------------------------------------------
# Canned intent_gap verdicts (passed to the stub as STUB_INTENT_FINDINGS)
# ---------------------------------------------------------------------------

def _blocking_gap():
    return {
        "type": "intent_gap",
        "claim": _SUBJECT,
        "against": {"path": "retry.py", "lines": "5-6",
                    "evidence": "def log_request(msg): print(msg) -- a logging "
                                "helper; no retry or backoff code exists"},
        "severity": "blocking",
        "confidence": 0.95,
        "self_check": {"fix_safe": True,
                       "rationale": "the whole claimed fix is absent from the diff"},
    }


def _warning_gap():
    return {
        "type": "intent_gap",
        "claim": _BODY,
        "against": {"path": "retry.py", "lines": "5-6",
                    "evidence": "def log_request(msg): print(msg)"},
        "severity": "warning",
        "confidence": 0.6,
        "self_check": {"fix_safe": True,
                       "rationale": "no backoff arithmetic added; a partial mismatch"},
    }


# ---------------------------------------------------------------------------
# The end-to-end fixture test
# ---------------------------------------------------------------------------

def test_intent_e2e_lying_commit_message_blocks_and_marks_intent(tmp_path):
    work = _two_commit_repo(tmp_path)
    tip = _git(["rev-parse", "HEAD"], cwd=work)

    # --- Push 1: a blocking + a warning intent finding -> the push is denied.
    gaps = json.dumps([_blocking_gap(), _warning_gap()])
    env1 = _env(tmp_path, "stub.trace", STUB_INTENT_FINDINGS=gaps, OCR_LEDGER="0")
    decision1, reason1 = _push(work, env1)
    assert decision1 == "deny", reason1
    st1 = _state(work, tip)
    assert st1["state"] == "done", st1
    assert st1.get("verdict") == "block", st1.get("verdict")
    assert st1.get("blocked") is True

    # (a) The assembled manifest carries the chunk-scoped commit_intent claim.
    manifest = _review_manifest(tmp_path, "stub.trace")
    intent = manifest.get("commit_intent")
    assert intent, f"no commit_intent in manifest: {sorted(manifest)}"
    assert intent["commit_count"] == 1
    assert [c["subject"] for c in intent["commits"]] == [_SUBJECT]
    c0 = intent["commits"][0]
    assert c0["sha"] == tip[:12]
    assert _BODY in c0["body"]
    assert "retry.py" in c0["files"]
    assert intent["subject_only"] == []

    # (d) The block text marks intent findings distinctly and quotes the claim.
    reasons1 = st1.get("reasons", "")
    assert "[intent-blocking]" in reasons1, reasons1
    assert "[intent-warning]" in reasons1, reasons1
    assert _SUBJECT in reasons1
    # The intent line is family-labelled, not a defect-formatted line.
    blocking_line = next(ln for ln in reasons1.splitlines()
                         if "[intent-blocking]" in ln)
    assert "intent gap" in blocking_line
    assert _SUBJECT not in blocking_line or "claim:" in blocking_line
    # No plain [high]/[medium] defect line snuck in from the stub defaults.
    assert "[high]" not in reasons1 and "[medium]" not in reasons1

    # (c) The recorded review keeps the intent finding's contracted fields.
    record_path = st1.get("record")
    assert record_path and Path(record_path).exists(), st1.get("record")
    entries = [json.loads(ln) for ln in Path(record_path).read_text().splitlines()
               if ln.strip()]
    entry = next(e for e in reversed(entries) if e.get("head") == tip)
    found = [f for f in entry["findings"] if f.get("type") == "intent_gap"]
    assert len(found) == 2, entry["findings"]
    blocking = next(f for f in found if f["severity"] == "blocking")
    assert blocking["claim"] == _SUBJECT
    assert blocking["against"]["path"] == "retry.py"
    assert blocking["against"]["evidence"]
    assert blocking["self_check"]["fix_safe"] is True

    # --- Push 2: warning intent finding only -> warn, but never a block.
    (work / "retry.py").write_text(_AMENDED_SRC, encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "--amend", "--no-edit"], cwd=work)
    tip2 = _git(["rev-parse", "HEAD"], cwd=work)

    warn_only = json.dumps([_warning_gap()])
    env2 = _env(tmp_path, "stub2.trace", STUB_INTENT_FINDINGS=warn_only,
                OCR_LEDGER="0")
    decision2, reason2 = _push(work, env2)
    assert decision2 == "pass", reason2
    st2 = _state(work, tip2)
    assert st2["state"] == "done", st2
    assert st2.get("verdict") == "warn", st2.get("verdict")
    assert st2.get("blocked") is False
    reasons2 = st2.get("reasons", "")
    assert "[intent-warning]" in reasons2, reasons2
    assert _BODY in reasons2
    assert "[intent-blocking]" not in reasons2
    # Intent rides the manifest on the warning path too.
    intent2 = _review_manifest(tmp_path, "stub2.trace").get("commit_intent")
    assert intent2 and intent2["commits"][0]["subject"] == _SUBJECT
