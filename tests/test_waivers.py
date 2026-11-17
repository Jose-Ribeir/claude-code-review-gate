"""SPEC-15.2 granular finding waivers, end to end against the stub reviewer:
block -> `waive F2 F3` -> the next push with unchanged findings passes with the
waived list in the verdict text, the recorded JSON, and telemetry; the same
finding with its code altered blocks again (the waiver lapses); `unwaive`
restores blocking; `waive --list` shows the ledger; unknown ids and empty
ledgers degrade with notes, never failures; waived findings are never deleted
from the record.

Harness modelled on tests/test_memory_gate.py. Key mechanics: push 1 is denied
so the remote stays at base, which means push 2's range still contains the
defect commit and re-reviews the (unchanged) finding files -- the waiver
anchor is content-anchored over a LOCAL window (the finding's location
+/-10 lines at the tip), so an edit inside that window lapses the waiver
while a far-field edit elsewhere in the file does not (the documented,
conservative design decision).
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
import ocr_telemetry as ot  # noqa: E402
from hook_output import parse_pretooluse  # noqa: E402

_spec = importlib.util.spec_from_file_location("review_gate_wv", _GATE)
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)

SIG_X = "if user.is_service_account:  # quota check skipped for service accounts"
SIG_Y = "return self._cache.get(key) if key in self._cache else None  # racy read"
CONTENT_X = "quota check is missing for service accounts"
CONTENT_Y = "cache read races the concurrent invalidation"
FINDINGS_FOR = {
    "app/x.py": {"severity": "high", "content": CONTENT_X, "existing_code": SIG_X},
    "app/y.py": {"severity": "high", "content": CONTENT_Y, "existing_code": SIG_Y},
}

BASE_FILES = {"app/x.py": "def handle(user):\n    pass\n",
              "app/y.py": "def cache_read(key):\n    pass\n"}
DEFECT_FILES = {"app/x.py": f"def handle(user):\n    {SIG_X}\n    pass\n",
                "app/y.py": f"def cache_read(key):\n    {SIG_Y}\n    pass\n"}


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def _repo(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    remote, work = tmp_path / "origin.git", tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    for k, v in (("user.email", "t@t.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(["config", k, v], cwd=work)
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    _git(["config", "core.hooksPath", str(hooks)], cwd=work)
    for name, content in BASE_FILES.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(content, encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    return work


def _env(tmp_path, **kw):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OCR_", "STUB_")) or k == "OCR_BACKEND"}
    env["OCR_STATE_DIR"] = os.environ["OCR_STATE_DIR"]
    env.update({"CLAUDE_PLUGIN_DATA": str(tmp_path / "gate-data"),
                "OCR_REVIEWER_CMD": f'"{sys.executable}" "{_STUB}"',
                "STUB_TRACE": str(tmp_path / "stub.trace"),
                "OCR_INLINE_BUDGET": "30",
                "STUB_FINDINGS_FOR": json.dumps(FINDINGS_FOR)})
    env.update({k: str(v) for k, v in kw.items()})
    return env


def _push(work, env):
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash", "cwd": str(work),
                          "tool_input": {"command": "git push -f origin main"}})
    proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                          capture_output=True, text=True, cwd=str(work), env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return parse_pretooluse(proc.stdout)


def _commit_files(work, files, message):
    for name, content in files.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(content, encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", message], cwd=work)


def _gate_cmd(work, env, *args):
    return subprocess.run([sys.executable, _GATE, *args],
                          capture_output=True, text=True, cwd=str(work), env=env, timeout=60)


def _last_record(work):
    git_dir = review_gate._git_dir(str(work))
    entries = review_gate._read_history(git_dir, 1)
    assert entries, "no recorded review"
    return entries[-1]


def _recorded_findings(work):
    return [f for f in _last_record(work).get("findings") or []
            if isinstance(f, dict)]


def _by_path(findings, path):
    hits = [f for f in findings if f.get("path") == path]
    assert len(hits) == 1, f"expected exactly one finding on {path}: {hits}"
    return hits[0]


def _display_id(f):
    return str(f.get("id") or "").strip() or review_gate._finding_id(f)[:12]


def _blocked_push_with_two_findings(work, env):
    """The shared opening: the two scripted high findings block a push, and
    the findings are recorded with no waivers in play."""
    _commit_files(work, DEFECT_FILES, "add quota path and racy cache read")
    decision, reason = _push(work, env)
    assert decision == "deny", reason
    entry = _last_record(work)
    assert entry["verdict"] == "block"
    fx, fy = _by_path(_recorded_findings(work), "app/x.py"), \
        _by_path(_recorded_findings(work), "app/y.py")
    assert fx["severity"] == "high" and fy["severity"] == "high"
    assert not entry.get("waived"), "nothing was waived yet"
    return _display_id(fx), _display_id(fy)


def _done_state(work):
    common = review_gate._git_common_dir(str(work))
    tip = review_gate._head_sha(str(work))
    deadline = time.monotonic() + 60
    while True:
        st = review_gate._read_state(review_gate._state_path(common, tip)) or {}
        if st.get("state") in ("done", "failed") or time.monotonic() > deadline:
            return st
        time.sleep(0.2)


# ---------------------------------------------------------------------------
# block -> waive -> next push passes, waived listed in text/JSON/telemetry
# ---------------------------------------------------------------------------

def test_waived_findings_pass_and_are_listed_everywhere(tmp_path):
    work = _repo(tmp_path)
    env = _env(tmp_path, OCR_TELEMETRY="1")
    idx, idy = _blocked_push_with_two_findings(work, env)

    # The operator waives both findings between pushes (a between-pushes act,
    # nothing on the push command line).
    proc = _gate_cmd(work, env, "waive", idx, idy)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "2 active waiver(s)" in proc.stdout, proc.stdout + proc.stderr

    # Push 2: a new commit on an unrelated file, so the range (remote is still
    # at base after the deny) re-reviews the UNCHANGED finding files. Both
    # anchors match -> waived -> the push passes.
    _commit_files(work, {"README.md": "v2\n"}, "unrelated change")
    decision, reason = _push(work, env)
    assert decision == "pass", reason

    # Text: the done state's reasons carry the waived list, loudly.
    st = _done_state(work)
    assert st.get("verdict") == "pass", st
    reasons = str(st.get("reasons") or "")
    assert "waived: [" in reasons, reasons
    assert idx in reasons and idy in reasons, reasons
    assert "never deleted" in reasons, reasons

    # JSON: the recorded verdict keeps the waived findings -- never deleted --
    # and they left the blocking findings list.
    entry = _last_record(work)
    assert entry["verdict"] == "pass"
    assert entry.get("waived"), entry
    assert {w["path"] for w in entry["waived"]} == {"app/x.py", "app/y.py"}
    assert all(w.get("waived") is True for w in entry["waived"])
    assert {w["content"] for w in entry["waived"]} == {CONTENT_X, CONTENT_Y}
    assert not [f for f in entry.get("findings") or []
                if f.get("path") in ("app/x.py", "app/y.py")]

    # Telemetry: the run record carries the waived list whenever suppression
    # happened.
    common = review_gate._git_common_dir(str(work))
    recs = [r for r in ot.load(common, days=1) if r.get("waived")]
    assert recs, "no telemetry run record carries the waived list"
    rec = recs[-1]
    assert {w["path"] for w in rec["waived"]} == {"app/x.py", "app/y.py"}
    assert rec["verdict"] == "pass"


# ---------------------------------------------------------------------------
# a code change lapses the waiver: the finding is judged fresh and blocks
# ---------------------------------------------------------------------------

def test_waiver_lapses_when_the_finding_code_changes(tmp_path):
    work = _repo(tmp_path)
    env = _env(tmp_path)
    idx, idy = _blocked_push_with_two_findings(work, env)

    proc = _gate_cmd(work, env, "waive", idx, idy)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # A change INSIDE the finding's local window (the touch lands a line
    # below the finding, well inside its +/-10-line context) changes the
    # window text, so x's anchor no longer matches: the waiver lapsed and
    # the finding is judged fresh. y.py is untouched and stays waived.
    _commit_files(work, {"app/x.py": DEFECT_FILES["app/x.py"] + "# touched\n",
                         "README.md": "v2\n"}, "touch the finding's file")
    decision, reason = _push(work, env)
    assert decision == "deny", reason
    assert "app/x.py" in [f.get("path") for f in _recorded_findings(work)]

    entry = _last_record(work)
    assert entry["verdict"] == "block"
    assert {w["path"] for w in entry.get("waived") or []} == {"app/y.py"}
    # The deny reason lists the still-waived y only, never the lapsed x.
    assert idy in reason and "waived: [" in reason
    assert idx not in reason, reason


# ---------------------------------------------------------------------------
# the anchor's window semantics: far-field edits survive, local ones lapse
# ---------------------------------------------------------------------------

def test_waiver_survives_a_far_field_edit(tmp_path):
    """A change far from the finding's local window (its location +/-10
    lines) does NOT lapse the waiver: the window text at the tip is
    unchanged, so the anchor still matches and the push passes -- the
    whole-file-blob scheme's over-lapping is gone."""
    finding_only = {"app/x.py": {"severity": "high", "content": CONTENT_X,
                                 "existing_code": SIG_X}}
    env = _env(tmp_path, STUB_FINDINGS_FOR=json.dumps(finding_only))
    long_defect = {"app/x.py": ("def handle(user):\n    " + SIG_X + "\n    pass\n"
                                + "".join(f"# filler {i}\n"
                                           for i in range(1, 31)))}
    work = _repo(tmp_path)
    _commit_files(work, long_defect, "add the quota defect plus filler")
    decision, reason = _push(work, env)
    assert decision == "deny", reason
    fx = _by_path(_recorded_findings(work), "app/x.py")
    idx = _display_id(fx)

    proc = _gate_cmd(work, env, "waive", idx)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # The far-field edit: 20+ lines below the finding (its window is lines
    # 1..12), inside the same file. The whole-file blob would have moved;
    # the window does not.
    _commit_files(work, {"app/x.py": long_defect["app/x.py"].replace(
        "# filler 30\n", "# filler 30 -- edited far from the finding\n")},
        "edit the file far from the finding")
    decision, reason = _push(work, env)
    assert decision == "pass", reason
    entry = _last_record(work)
    assert entry["verdict"] == "pass"
    assert {w["path"] for w in entry.get("waived") or []} == {"app/x.py"}


def test_waiver_anchor_distinguishes_same_signature_at_different_locations(tmp_path):
    """Two findings sharing path+signature at different locations keep
    DISTINCT anchors: the location window carries the uniqueness the old
    blob oid provided, so an unrelated unchanged finding can never ride
    another's waiver."""
    work = _repo(tmp_path)
    code = ("def a():\n    " + SIG_X + "\n    pass\n"
            + "".join(f"# filler {i}\n" for i in range(1, 25))
            + "\ndef b():\n    " + SIG_X + "\n    pass\n")
    _commit_files(work, {"app/x.py": code}, "same signature, two locations")
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    f1 = {"path": "app/x.py", "existing_code": SIG_X,
          "start_line": 2, "end_line": 2}
    f2 = {"path": "app/x.py", "existing_code": SIG_X,
          "start_line": 30, "end_line": 30}
    a1 = review_gate._waiver_anchor(str(work), tip, f1)
    a2 = review_gate._waiver_anchor(str(work), tip, f2)
    assert a1 and a2, (a1, a2)
    assert a1 != a2


def test_waiver_anchor_fails_closed_without_path_sig_or_blob(tmp_path):
    """No path, no signature line findable at the tip location, or no blob
    at the tip: the anchor is "", which never matches -- the gate never
    waives blind."""
    work = _repo(tmp_path)
    _commit_files(work, DEFECT_FILES, "defects")
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    good = {"path": "app/x.py", "existing_code": SIG_X,
            "start_line": 2, "end_line": 2}
    assert review_gate._waiver_anchor(str(work), tip, good)
    assert review_gate._waiver_anchor(str(work), tip, dict(good, path="")) == ""
    # Without existing_code the anchor falls back to the tip line AT THE
    # FINDING'S LOCATION -- blank text there yields no signature at all.
    blank = _git(["show", f"{tip}:app/x.py"], cwd=work)
    assert blank  # precondition: the finding's file exists at the tip
    _commit_files(work, {"blank.py": "\n\n\n"}, "a file with no signatureable line")
    blank_tip = _git(["rev-parse", "HEAD"], cwd=work)
    assert review_gate._waiver_anchor(
        str(work), blank_tip,
        {"path": "blank.py", "start_line": 1, "end_line": 1}) == ""
    assert review_gate._waiver_anchor(
        str(work), tip, dict(good, path="app/missing.py")) == ""
    assert review_gate._waiver_anchor(str(work), "", good) == ""


def test_waiver_anchor_falls_back_to_tip_text_for_command_checks(tmp_path):
    """Command checks (the SPEC-15 test executor) emit outputs, not diff
    hunks: findings carry NO existing_code. Their anchor falls back to the
    tip text at the finding's own location -- otherwise every command-check
    finding would be permanently unwaivable, which spec story 23 forbids.
    The fallback keeps the same window semantics: far-field edits never
    lapse; an in-window edit does."""
    work = _repo(tmp_path)
    # Line 1 must satisfy ocr_impact.signature_line: a normalized line of
    # >= 20 chars that is not "boring" (a bare `x = 1` or short `def` is not
    # signable, so the finding's location must carry a distinctive line).
    code = ("def feature_with_a_long_distinctive_name(argument):\n"
            "    return 1\n" + "".join(
        f"# filler {i}\n" for i in range(1, 31)))
    _commit_files(work, {"app/check_target.py": code}, "executor target")
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    finding = {"path": "app/check_target.py", "start_line": 1, "end_line": 1,
               "content": "FAIL test_smoke: assert 2 == 3\n",
               "evidence_source": "existing_test"}
    a1 = review_gate._waiver_anchor(str(work), tip, finding)
    assert a1, "a command-check finding without existing_code must anchor"
    _commit_files(work, {"app/check_target.py": code.replace(
        "# filler 30\n", "# filler 30 -- far-field edit\n")}, "far-field")
    tip2 = _git(["rev-parse", "HEAD"], cwd=work)
    assert review_gate._waiver_anchor(str(work), tip2, finding) == a1
    _commit_files(work, {"app/check_target.py": code.replace(
        "return 1\n", "return 2\n")}, "in-window")
    tip3 = _git(["rev-parse", "HEAD"], cwd=work)
    assert review_gate._waiver_anchor(str(work), tip3, finding) != a1


def test_apply_waivers_ledger_read_failure_is_observed(monkeypatch, tmp_path):
    """ADR 0006 (fail-soft AND observable): a waiver-ledger read failure
    inside _apply_waivers changes no verdict path (findings unchanged,
    nothing waived) AND leaves a house warning plus a waiver_errors
    telemetry note -- never silent."""
    work = _repo(tmp_path)

    def boom(_common_dir):
        raise RuntimeError("synthetic ledger failure")

    monkeypatch.setattr(review_gate.rg_store, "read_waivers", boom)
    review_gate._TELE.pop("waiver_errors", None)
    result = {"findings": [{"path": "app/x.py", "severity": "high",
                            "content": CONTENT_X, "existing_code": SIG_X}],
              "warnings": [{"message": "pre-existing warning"}]}
    out = review_gate._apply_waivers(str(work), "deadbeef" * 4, result, str(work))
    # Fail-soft: the findings are unchanged, nothing is waived.
    assert [f.get("content") for f in out["findings"]] == [CONTENT_X]
    assert not out.get("waived")
    # AND observable: a house warning plus a waiver_errors telemetry note.
    msgs = [str(w.get("message")) for w in out["warnings"]]
    assert any("waiver ledger could not be read" in m for m in msgs), msgs
    errs = review_gate._TELE.get("waiver_errors") or []
    assert errs and "synthetic ledger failure" in errs[-1], errs


# ---------------------------------------------------------------------------
# unwaive restores blocking
# ---------------------------------------------------------------------------

def test_unwaive_restores_blocking(tmp_path):
    work = _repo(tmp_path)
    env = _env(tmp_path)
    idx, idy = _blocked_push_with_two_findings(work, env)

    proc = _gate_cmd(work, env, "waive", idx, idy)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # Push 2 passes (both waived)...
    _commit_files(work, {"README.md": "v2\n"}, "unrelated change")
    decision, reason = _push(work, env)
    assert decision == "pass", reason

    # ...then the operator unwaives just x: the next push blocks on it again,
    # while y stays suppressed.
    proc = _gate_cmd(work, env, "unwaive", idx)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "unwaived" in proc.stdout, proc.stdout + proc.stderr
    assert "1 active waiver(s)" in proc.stdout, proc.stdout + proc.stderr

    _commit_files(work, {"README.md": "v3\n"}, "another unrelated change")
    decision, reason = _push(work, env)
    assert decision == "deny", reason
    entry = _last_record(work)
    assert entry["verdict"] == "block"
    assert "app/x.py" in [f.get("path") for f in entry.get("findings") or []]
    assert {w["path"] for w in entry.get("waived") or []} == {"app/y.py"}


# ---------------------------------------------------------------------------
# waive --list shows the ledger
# ---------------------------------------------------------------------------

def test_waive_list_shows_the_ledger(tmp_path):
    work = _repo(tmp_path)
    env = _env(tmp_path)
    idx, idy = _blocked_push_with_two_findings(work, env)

    proc = _gate_cmd(work, env, "waive", idx, idy)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    proc = _gate_cmd(work, env, "waive", "--list")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert idx in out and idy in out, out
    assert out.count("waived ") >= 2, out          # one line per ledger entry
    assert "2 active waiver(s)" in out, out
    assert "2 finding(s) suppressed" in out, out


# ---------------------------------------------------------------------------
# degradation: unknown ids and empty ledgers are notes, never failures
# ---------------------------------------------------------------------------

def test_waive_unknown_id_is_a_note_not_a_failure(tmp_path):
    work = _repo(tmp_path)
    env = _env(tmp_path)
    _blocked_push_with_two_findings(work, env)

    proc = _gate_cmd(work, env, "waive", "deadbeef00")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "unknown finding" in proc.stdout + proc.stderr

    # The ledger is unchanged: still empty.
    proc = _gate_cmd(work, env, "waive", "--list")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "no waivers recorded" in proc.stdout, proc.stdout


def test_empty_ledger_and_missing_history_degrade(tmp_path):
    work = _repo(tmp_path)  # fresh repo: no recorded verdict, no ledger
    env = _env(tmp_path)

    proc = _gate_cmd(work, env, "waive", "--list")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "no waivers recorded" in proc.stdout, proc.stdout

    proc = _gate_cmd(work, env, "waive", "F2")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "no recorded verdicts" in proc.stdout + proc.stderr

    proc = _gate_cmd(work, env, "unwaive", "F2")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "nothing to unwaive" in proc.stdout, proc.stdout


def test_unwaive_unknown_id_is_a_note_not_a_failure(tmp_path):
    work = _repo(tmp_path)
    env = _env(tmp_path)
    idx, idy = _blocked_push_with_two_findings(work, env)
    proc = _gate_cmd(work, env, "waive", idx, idy)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    proc = _gate_cmd(work, env, "unwaive", "deadbeef00")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "no waiver for" in proc.stdout + proc.stderr

    # The ledger is unchanged: still 2 active waivers.
    proc = _gate_cmd(work, env, "waive", "--list")
    assert "2 active waiver(s)" in proc.stdout, proc.stdout


# ---------------------------------------------------------------------------
# the waiver ledger itself (store plumbing)
# ---------------------------------------------------------------------------

def test_ledger_layout_is_repo_scoped_json(tmp_path):
    work = _repo(tmp_path)
    env = _env(tmp_path)
    idx, idy = _blocked_push_with_two_findings(work, env)
    proc = _gate_cmd(work, env, "waive", idx, idy, "--note", "accepted risk")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    common = review_gate._git_common_dir(str(work))
    ledger = json.loads(Path(review_gate.rg_store.waiver_path(common))
                        .read_text(encoding="utf-8"))["waivers"]
    assert len(ledger) == 2
    assert all({"sid", "finding_id", "waived_at"} <= set(e) for e in ledger)
    assert any(e.get("note") == "accepted risk" for e in ledger)
    # read_waivers is what the gate consults: same entries, fail-soft.
    assert review_gate.rg_store.read_waivers(common) == ledger
    # Repo scoping: a different common dir maps to a different (empty) ledger.
    assert review_gate.rg_store.read_waivers(str(tmp_path / "other")) == []
