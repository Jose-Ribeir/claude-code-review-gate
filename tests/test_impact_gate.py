"""0.9.0 end to end: impact sites, known defects, notes, ledger rules, run log.

The gate computes, in Python, where the symbols a push changed are used outside
the files under review, and hands those call sites to the reviewer. These tests
drive the real hook with the stub reviewer (tests/stub_reviewer.py) and assert
on what reached the reviewer's manifest, what blocked, and what was logged.
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
_spec = importlib.util.spec_from_file_location("review_gate_ig", _GATE)
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def _repo(tmp_path, files):
    tmp_path.mkdir(parents=True, exist_ok=True)
    remote, work = tmp_path / "origin.git", tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    for k, v in (("user.email", "t@t.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(["config", k, v], cwd=work)
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    _git(["config", "core.hooksPath", str(hooks)], cwd=work)
    _write(work, files)
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    return work


def _write(work, files):
    for name, content in files.items():
        p = work / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _commit(work, files, amend=False):
    _write(work, files)
    _git(["add", "."], cwd=work)
    _git(["commit", "-q"] + (["--amend", "--no-edit"] if amend else ["-m", "c"]), cwd=work)
    return _git(["rev-parse", "HEAD"], cwd=work)


def _env(tmp_path, trace, **kw):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OCR_", "STUB_"))}
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
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    return out["permissionDecision"], out.get("permissionDecisionReason", "")


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


_LIB = "".join(f"# filler {i}\n" for i in range(30))
_LIB_V1 = _LIB + "def compute_total(items):\n    return sum(items)\n"
_LIB_V2 = _LIB + "def compute_total(items):\n    return sum(items) if items else None\n"
_CALLER = "from lib import compute_total\n\n\ndef report(xs):\n    return compute_total(xs) + 1\n"


def test_first_push_sends_callers_in_untouched_files(tmp_path):
    """A caller the push never touched is exactly what a review of the diff
    misses. With call sites found, the gate uses a manifest, not the bare
    0.7.0 command line."""
    work = _repo(tmp_path, {"lib.py": _LIB_V1, "app.py": _CALLER})
    tip = _commit(work, {"lib.py": _LIB_V2})
    decision, reason = _push(work, _env(tmp_path, "t.trace"), cmd="git push origin main")
    assert decision == "allow", reason
    impact = _review_manifest(tmp_path, "t.trace").get("impact") or {}
    # Both uses in app.py: the import (line 1) and the call (line 5).
    assert sorted((s["path"], s["line"]) for s in impact.get("sites") or []) == [
        ("app.py", 1), ("app.py", 5)]
    assert impact["symbols"][0]["change"] == "body"
    assert _state(work, tip).get("state") == "done"


def test_no_callers_keeps_the_golden_command_line(tmp_path):
    work = _repo(tmp_path, {"lib.py": _LIB_V1})
    _commit(work, {"lib.py": _LIB_V2})
    decision, _ = _push(work, _env(tmp_path, "t.trace"), cmd="git push origin main")
    assert decision == "allow"
    assert all(c.get("paths_file") is None for c in _calls(tmp_path, "t.trace"))


def test_ocr_impact_0_turns_it_off(tmp_path):
    work = _repo(tmp_path, {"lib.py": _LIB_V1, "app.py": _CALLER})
    _commit(work, {"lib.py": _LIB_V2})
    _push(work, _env(tmp_path, "t.trace", OCR_IMPACT="0"), cmd="git push origin main")
    assert all(c.get("paths_file") is None for c in _calls(tmp_path, "t.trace"))


def test_fix_that_breaks_a_carried_caller_blocks_at_the_call_site(tmp_path):
    """The gap this release closes: T1 blocks on lib.py, T2 fixes lib.py by
    changing what compute_total returns. app.py is carried -- reviewed in T1 and
    unchanged -- but its call site still reaches the reviewer, and a broken
    caller blocks, anchored where the fix must land."""
    work = _repo(tmp_path, {"lib.py": _LIB_V1, "app.py": "x = 0\n"})
    tip1 = _commit(work, {"lib.py": _LIB_V1 + "def bad(): pass\n", "app.py": _CALLER})
    finding = {"lib.py": {"severity": "high", "content": "bad helper",
                          "existing_code": "def bad(): pass"}}
    decision, _ = _push(work, _env(tmp_path, "t1.trace", STUB_FINDINGS_FOR=json.dumps(finding)),
                        cmd="git push origin main")
    assert decision == "deny"
    _state(work, tip1)

    tip2 = _commit(work, {"lib.py": _LIB_V2}, amend=True)
    decision, reason = _push(work, _env(tmp_path, "t2.trace", STUB_IMPACT_BREAK="1"))
    assert _state(work, tip2).get("state") == "done"
    manifest = _review_manifest(tmp_path, "t2.trace")
    # Delta or cost-rule full, lib.py is the only file reviewed; app.py is not.
    assert [f["path"] for f in manifest["files"]] == ["lib.py"]
    assert "app.py" in manifest["carried"]
    assert decision == "deny" and "(caller of changed code) app.py:5" in reason, reason

    # The finding is also kept on app.py's own record, so it stays owed.
    common = review_gate._git_common_dir(str(work))
    entries, _ = review_gate._collect_diff_entries(str(work), _git(["rev-parse", "origin/main"], cwd=work), tip2)
    e = next(x for x in entries if x["path"] == "app.py")
    fp = review_gate._compute_fingerprint(str(work), tip2)
    key = review_gate._record_key("app.py", "", e["status"], e["old_oid"])
    rec = review_gate._read_ledger_record(review_gate._record_path(common, fp, key, e["new_oid"]),
                                          fp, key, e["new_oid"])
    assert rec and any(f.get("impact_site") for f in rec["findings"]), rec


def test_callers_in_another_chunk_are_sent_to_the_defining_chunk(tmp_path):
    """With chunking, a caller reviewed in chunk 2 is invisible to chunk 1's
    reviewer. Only the chunk's own files are excluded from its bundle."""
    work = _repo(tmp_path, {"lib.py": _LIB_V1, "app.py": _CALLER})
    tip = _commit(work, {"lib.py": _LIB_V2, "app.py": _CALLER + "# touched\n"})
    items = [{"entry": {"path": p, "old_path": "", "status": "M",
                        "old_oid": _git(["rev-parse", f"HEAD~1:{p}"], cwd=work),
                        "new_oid": _git(["rev-parse", f"HEAD:{p}"], cwd=work)},
              "mode": "full", "record": None, "from_oid": ""} for p in ("lib.py", "app.py")]
    review_gate._TELE.clear()
    impact = review_gate._compute_impact(str(work), tip, items)
    alone = review_gate._impact_bundle(str(work), tip, impact, ["lib.py"])
    assert {s["path"] for s in alone["sites"]} == {"app.py"}
    assert review_gate._impact_bundle(str(work), tip, impact, ["lib.py", "app.py"]) is None


_EVAL = "def handle(req):\n    return eval(req.args['expr'])\n"


def test_known_defect_in_the_push_goes_to_the_reviewer_and_blocks_once_confirmed(tmp_path):
    work = _repo(tmp_path, {"x.py": "a = 0\n", "y.py": "b = 0\n"})
    tip1 = _commit(work, {"x.py": _LIB + _EVAL, "y.py": _LIB + _EVAL.replace("handle", "other")})
    finding = {"x.py": {"severity": "high", "content": "eval of request input",
                        "existing_code": "return eval(req.args['expr'])"}}
    decision, _ = _push(work, _env(tmp_path, "t1.trace", STUB_FINDINGS_FOR=json.dumps(finding)),
                        cmd="git push origin main")
    assert decision == "deny"
    _state(work, tip1)

    # T2 fixes x.py only. y.py is carried with the same line.
    tip2 = _commit(work, {"x.py": _LIB + "def handle(req):\n    return safe(req)\n"}, amend=True)
    fid = review_gate._finding_id({"path": "x.py", **finding["x.py"]})
    resolve = json.dumps({fid: {"status": "resolved", "evidence_path": "x.py",
                                "evidence_quote": "return safe(req)"}})
    decision, reason = _push(work, _env(tmp_path, "t2.trace", STUB_RESOLVE=resolve,
                                        STUB_CONFIRM_SIBLINGS="1"))
    _state(work, tip2)
    defects = _review_manifest(tmp_path, "t2.trace").get("known_defects") or []
    assert [(d["path"], d["line"]) for d in defects] == [("y.py", 32)]
    assert decision == "deny" and "(same defect as an earlier finding) y.py:32" in reason, reason


def test_a_new_findings_twin_in_an_untouched_file_is_only_a_note(tmp_path):
    work = _repo(tmp_path, {"old.py": _EVAL, "x.py": "a = 0\n"})
    tip = _commit(work, {"x.py": _LIB + _EVAL})
    finding = {"x.py": {"severity": "medium", "content": "eval of request input",
                        "existing_code": "return eval(req.args['expr'])"}}
    decision, reason = _push(work, _env(tmp_path, "t.trace", STUB_FINDINGS_FOR=json.dumps(finding)),
                             cmd="git push origin main")
    st = _state(work, tip)
    assert decision == "allow", reason
    assert st.get("verdict") == "warn"  # the note did not raise it
    assert "(note) old.py:2" in st.get("reasons", ""), st.get("reasons")


def test_every_run_is_logged_locally_and_reported(tmp_path):
    work = _repo(tmp_path, {"lib.py": _LIB_V1, "app.py": _CALLER})
    tip = _commit(work, {"lib.py": _LIB_V2})
    cross = json.dumps({"symbols": [{"name": "compute_total", "defined_in": "lib.py",
                                     "change": "body",
                                     "external_refs": [{"path": "app.py", "line": 5}]}]})
    _push(work, _env(tmp_path, "t.trace", STUB_CROSS_FILE=cross), cmd="git push origin main")
    _state(work, tip)
    common = review_gate._git_common_dir(str(work))
    logs = list((Path(common) / "review-gate-telemetry").glob("*.jsonl"))
    assert len(logs) == 1
    rec = json.loads(logs[0].read_text(encoding="utf-8").splitlines()[-1])
    assert rec["tip"] == tip and rec["verdict"] == "pass"
    assert [p["path"] for p in rec["plan"]] == ["lib.py"]
    assert rec["calls"][0]["kind"] == "review" and rec["calls"][0]["outcome"] == "ok"
    assert rec["impact"]["sites"][0]["path"] == "app.py"
    assert rec["model_cross_file"]["symbols"][0]["name"] == "compute_total"
    assert rec["site_verdicts"] == {s["id"]: "ok" for s in rec["impact"]["sites"]}
    assert "model" in rec["fp_parts"]

    proc = subprocess.run([sys.executable, _GATE, "--telemetry-report"], cwd=str(work),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "Runs: 1" in proc.stdout
    # The model's one site is also one Python found.
    assert "100%" in proc.stdout, proc.stdout


def test_git_helpers_never_open_a_console_window(monkeypatch):
    """The supervisor is DETACHED_PROCESS: a git child started without
    CREATE_NO_WINDOW gets a console window of its own on Windows -- dozens per
    review, and some left hanging with 0x800700e8."""
    import ocr_impact
    seen = []

    def fake_run(*a, **kw):
        seen.append(kw.get("creationflags", 0))
        return subprocess.CompletedProcess(a, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    review_gate._git(["status"])
    ocr_impact.git_runner(".")(["status"])
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert seen == [flag, flag]


def test_ocr_telemetry_0_writes_nothing(tmp_path):
    work = _repo(tmp_path, {"lib.py": _LIB_V1})
    tip = _commit(work, {"lib.py": _LIB_V2})
    _push(work, _env(tmp_path, "t.trace", OCR_TELEMETRY="0"), cmd="git push origin main")
    _state(work, tip)
    common = review_gate._git_common_dir(str(work))
    assert not (Path(common) / "review-gate-telemetry").exists()
