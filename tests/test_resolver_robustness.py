"""The resolver's output is model output: it must never crash the gate, and a
verdict it cannot back with evidence must never quietly decide a push.

Regressions for two failures seen on a real push (0.8.0):

1. The resolver answered ``{"resolutions": {"<id>": true}}`` instead of
   per-finding objects, and the gate died on ``True.get(...)`` -- the push was
   blocked "to preserve gate integrity" and nothing was recorded.
2. On the next push the resolver saw only the incremental diff since the last
   reviewed tip, which no longer contained the fix, and answered
   ``still_present`` with empty evidence for code that was already gone.
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
_spec = importlib.util.spec_from_file_location("review_gate_rr", _GATE)
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def _tiny_repo(tmp_path, files):
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    _git(["config", "user.email", "t@t.com"], cwd=work)
    _git(["config", "user.name", "t"], cwd=work)
    _git(["config", "commit.gpgsign", "false"], cwd=work)
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    _git(["config", "core.hooksPath", str(hooks)], cwd=work)
    for name, content in files.items():
        (work / name).write_text(content, encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    return work


def _amend(work, files):
    for name, content in files.items():
        (work / name).write_text(content, encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "--amend", "--no-edit"], cwd=work)
    return _git(["rev-parse", "HEAD"], cwd=work)


def _env(tmp_path, trace="stub.trace", **kw):
    env = dict(os.environ)
    for k in list(env):
        if k.startswith(("OCR_", "STUB_")):
            env.pop(k)
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "gate-data")
    env["OCR_REVIEWER_CMD"] = f'"{sys.executable}" "{_STUB}"'
    env["STUB_TRACE"] = str(tmp_path / trace)
    env["OCR_INLINE_BUDGET"] = "30"
    for k, v in kw.items():
        env[k] = str(v)
    return env


def _hook(work, env, cmd="git push -f origin main"):
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash",
                          "cwd": str(work), "tool_input": {"command": cmd}})
    proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                          capture_output=True, text=True, cwd=str(work), env=env,
                          timeout=90)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    return out["permissionDecision"], out.get("permissionDecisionReason", "")


def _trace(tmp_path, name):
    p = tmp_path / name
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _state(work, tip, timeout=60):
    """The run's final state, once it has one."""
    common = review_gate._git_common_dir(str(work))
    path = review_gate._state_path(common, tip)
    deadline = time.monotonic() + timeout
    while True:
        st = review_gate._read_state(path) or {}
        if st.get("state") in ("done", "failed") or time.monotonic() > deadline:
            return st
        time.sleep(0.2)


_FINDING = {"severity": "high", "content": "bad function in x",
            "existing_code": "def bad(): pass"}
_FID = review_gate._finding_id({"path": "x.py", **_FINDING})


def _blocked_on_x(tmp_path):
    """T1: x.py gets a high finding and the push is denied."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    work = _tiny_repo(tmp_path, {"x.py": "x = 0\n", "stable.py": "# stable\n"})
    (work / "x.py").write_text("x = 1\ndef bad(): pass\n")
    (work / "stable.py").write_text("# stable v2\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "t1"], cwd=work)
    tip1 = _git(["rev-parse", "HEAD"], cwd=work)
    decision, reason = _hook(work, _env(tmp_path, "t1.trace",
                                        STUB_FINDINGS_FOR=json.dumps({"x.py": _FINDING})),
                             cmd="git push origin main")
    assert decision == "deny", reason
    assert _state(work, tip1).get("state") == "done"
    return work, _git(["rev-parse", "HEAD:x.py"], cwd=work)


# ---------------------------------------------------------------------------
# Bug 1: malformed per-finding values
# ---------------------------------------------------------------------------

def _prior(fid="a" * 64, **kw):
    f = {"path": "x.py", "severity": "high", "content": "c", "existing_code": "def bad(): pass"}
    f.update(kw)
    return {"id": fid, "finding": f, "record": {}, "target_oid": ""}


def test_normalize_turns_every_malformed_value_into_an_evidence_free_still_present():
    ids = ["b" * 64, "c" * 64, "d" * 64, "e" * 64, "f" * 64, "0" * 64]
    to_resolve = [_prior(i) for i in ids]
    raw = {
        ids[0]: True,                                   # the bug
        ids[1]: {"evidence_path": "x.py"},              # no status
        ids[2]: {"status": "fixed"},                    # unknown status
        ids[3]: "resolved",                             # bare string
        # ids[4] missing entirely
        ids[5]: {"status": "resolved", "evidence_path": "x.py",
                 "evidence_quote": "def good(): pass"},
        "not-asked": {"status": "resolved"},
    }
    out, warnings = review_gate._normalize_resolutions(raw, to_resolve)
    assert set(out) == set(ids)
    for i in ids[:5]:
        assert out[i] == {"status": "still_present", "evidence_path": "",
                          "evidence_quote": ""}, i
    assert out[ids[5]]["status"] == "resolved"
    assert out[ids[5]]["evidence_quote"] == "def good(): pass"
    assert len(warnings) == 5
    assert any("bool" in w for w in warnings)


def test_normalize_survives_a_non_dict_outer_value():
    out, warnings = review_gate._normalize_resolutions([True], [_prior()])
    assert out["a" * 64]["status"] == "still_present"
    assert warnings


def test_run_resolver_does_not_crash_on_boolean_resolutions(monkeypatch, tmp_path):
    monkeypatch.setattr(review_gate, "_run_review",
                        lambda *a, **kw: ({"resolutions": {"a" * 64: True}}, True, ""))
    (tmp_path / review_gate.ASYNC_DIR).mkdir()
    items = [{"mode": "delta", "from_oid": "1" * 40,
              "entry": {"path": "x.py", "new_oid": "2" * 40}}]
    res, warnings = review_gate._run_resolver(
        ".", "hook", "", "tip", "b..t", [_prior()], items, str(tmp_path), "fp", "run1")
    assert res["a" * 64]["status"] == "still_present"
    assert any("not an object" in w for w in warnings)


def test_boolean_resolutions_block_loudly_instead_of_crashing(tmp_path):
    """The exact output seen in the field. The run must finish (state done, not
    failed), and whatever blocks must say why it blocks."""
    work, _ = _blocked_on_x(tmp_path)
    tip2 = _amend(work, {"x.py": "x = 1\ndef good(): pass\n"})
    env = _env(tmp_path, "t2.trace", STUB_RESOLVE=json.dumps({_FID: True}))
    decision, reason = _hook(work, env)
    st = _state(work, tip2)
    assert st.get("state") == "done", st
    assert decision == "deny", reason
    assert "unverified" in reason, reason
    resolves = [c for c in _trace(tmp_path, "t2.trace") if c.get("resolve_file")]
    # The first answer was unusable, so the finding was re-checked once.
    assert len(resolves) == 2
    assert resolves[1]["resolve_manifest"].get("recheck") is True


# ---------------------------------------------------------------------------
# Bug 2: a still_present verdict must be backed by the tip
# ---------------------------------------------------------------------------

def _repo_with_history(tmp_path, versions):
    """One commit per content of x.py. Returns (work, [commit], [blob])."""
    work = tmp_path / "g"
    work.mkdir()
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    _git(["config", "user.email", "t@t.com"], cwd=work)
    _git(["config", "user.name", "t"], cwd=work)
    _git(["config", "commit.gpgsign", "false"], cwd=work)
    commits, blobs = [], []
    for content in versions:
        (work / "x.py").write_text(content)
        _git(["add", "."], cwd=work)
        _git(["commit", "-q", "-m", "v"], cwd=work)
        commits.append(_git(["rev-parse", "HEAD"], cwd=work))
        blobs.append(_git(["rev-parse", "HEAD:x.py"], cwd=work))
    return work, commits, blobs


def test_guard_accepts_a_fix_made_before_the_incremental_delta(tmp_path):
    # v0 carries the finding, v1 fixes it (that run's resolver crashed), v2 is
    # the incremental change the next push reviews.
    work, commits, blobs = _repo_with_history(tmp_path, [
        "def bad(): pass\n",
        "def good(): pass\n",
        "def good(): pass\ny = 2\n",
    ])
    items = [{"mode": "delta", "from_oid": blobs[1], "record": {},
              "entry": {"path": "x.py", "old_path": "", "status": "M",
                        "old_oid": blobs[0], "new_oid": blobs[2]}}]
    res = {"status": "resolved", "evidence_path": "x.py", "evidence_quote": "def good(): pass"}
    rng = f"{commits[1]}..{commits[2]}"
    # Without the prior, only the incremental delta counts -- the old behaviour.
    assert not review_gate._guard_resolution(res, items, rng, str(work))
    prior = dict(_prior(), target_oid=blobs[0])
    assert review_gate._guard_resolution(res, items, rng, str(work),
                                         prior=prior, tip=commits[2])
    # A line that was already there when the finding was made is still no evidence.
    stale = dict(res, evidence_quote="x = 0")
    assert not review_gate._guard_resolution(stale, items, rng, str(work),
                                             prior=prior, tip=commits[2])


def test_guard_fallback_rejects_evidence_unchanged_in_original(tmp_path):
    # evidence_quote present in BOTH target_oid and tip_oid (never added) → rejected.
    work, commits, blobs = _repo_with_history(tmp_path, [
        "def good(): pass\nx = 0\n",
        "def good(): pass\nx = 1\n",
    ])
    res = {"status": "resolved", "evidence_path": "x.py",
           "evidence_quote": "def good(): pass"}
    rng = f"{commits[0]}..{commits[1]}"
    prior = dict(_prior(), target_oid=blobs[0])
    # Quote is in tip but was already in the original; the fallback must reject it.
    assert not review_gate._guard_resolution(res, [], rng, str(work),
                                             prior=prior, tip=commits[1])


def test_guard_fallback_accepts_context_spanning_quote(tmp_path):
    # evidence_quote spans an unchanged context line + the new added line → accepted.
    work, commits, blobs = _repo_with_history(tmp_path, [
        '"--output-format", "stream-json",\n]\n',
        '"--output-format", "stream-json",\n"--verbose",\n]\n',
    ])
    res = {
        "status": "resolved",
        "evidence_path": "x.py",
        # Quote deliberately spans the unchanged line and the newly-added line.
        "evidence_quote": '"--output-format", "stream-json",\n"--verbose",',
    }
    rng = f"{commits[0]}..{commits[1]}"
    prior = dict(_prior(), target_oid=blobs[0])
    # Quote is in tip AND was absent from original: fallback must accept it.
    assert review_gate._guard_resolution(res, [], rng, str(work),
                                         prior=prior, tip=commits[1])


def test_guard_fallback_fails_closed_on_missing_blob(tmp_path):
    # If git show <target_oid> fails (e.g. object missing / GC'd), the fallback
    # must return False (fail closed) rather than True (fail open).
    work, commits, blobs = _repo_with_history(tmp_path, [
        '"--output-format", "stream-json",\n]\n',
        '"--output-format", "stream-json",\n"--verbose",\n]\n',
    ])
    res = {
        "status": "resolved",
        "evidence_path": "x.py",
        "evidence_quote": '"--output-format", "stream-json",\n"--verbose",',
    }
    rng = f"{commits[0]}..{commits[1]}"
    # Supply a nonexistent target_oid so git show fails; guard must fail closed.
    prior = dict(_prior(), target_oid="0" * 40)
    assert not review_gate._guard_resolution(res, [], rng, str(work),
                                             prior=prior, tip=commits[1])


def test_judge_requires_tip_evidence_for_still_present(tmp_path):
    work, commits, blobs = _repo_with_history(tmp_path, [
        "def bad(): pass\n", "def good(): pass\n"])
    gone = dict(_prior(), target_oid=blobs[0])
    empty = {"status": "still_present", "evidence_path": "", "evidence_quote": ""}
    judge = review_gate._judge_resolution
    # Empty evidence and the flagged code is gone: unverified, not still_present.
    assert judge(gone, empty, [], "", str(work), commits[1]) == "unverified"
    # A quote that is not in the tip is no evidence either.
    fake = dict(empty, evidence_path="x.py", evidence_quote="def bad(): pass")
    assert judge(gone, fake, [], "", str(work), commits[1]) == "unverified"
    # The flagged code is still at the tip: verified still_present, whatever
    # the resolver said about evidence.
    assert judge(gone, empty, [], "", str(work), commits[0]) == "still_present"
    real = dict(empty, evidence_path="x.py", evidence_quote="def good(): pass")
    assert judge(gone, real, [], "", str(work), commits[1]) == "still_present"


def test_resolver_manifest_carries_the_since_finding_diff_spec(monkeypatch, tmp_path):
    work, commits, blobs = _repo_with_history(tmp_path, [
        "def bad(): pass\n", "def good(): pass\n", "def good(): pass\ny = 2\n"])
    seen = {}

    def _fake_review(*a, **kw):
        seen.update(json.loads(Path(kw["resolve_file"]).read_text(encoding="utf-8")))
        return {"resolutions": {}}, True, ""

    monkeypatch.setattr(review_gate, "_run_review", _fake_review)
    (tmp_path / review_gate.ASYNC_DIR).mkdir()
    prior = dict(_prior(), target_oid=blobs[0])
    review_gate._run_resolver(str(work), "hook", "", commits[2], "b..t", [prior], [],
                              str(tmp_path), "fp", "run1", recheck=True)
    assert seen["prior_files"] == [{"path": "x.py", "from_oid": blobs[0],
                                    "to_oid": blobs[2]}]
    assert seen["recheck"] is True


# ---------------------------------------------------------------------------
# Crash-then-retry, end to end
# ---------------------------------------------------------------------------

def test_fix_whose_resolver_run_failed_is_resolved_on_the_next_push(tmp_path):
    """T1 blocks on x.py. T2 fixes it but the resolver fails outright. T3 is an
    unrelated change to x.py: its incremental delta no longer shows the fix,
    yet the resolver must be able to prove it and the push must pass."""
    work, blob1 = _blocked_on_x(tmp_path)

    tip2 = _amend(work, {"x.py": "x = 1\ndef good(): pass\n"})
    decision, reason = _hook(work, _env(tmp_path, "t2.trace", STUB_RESOLVE_VERDICT="fail"))
    assert _state(work, tip2).get("state") == "done"
    assert decision == "deny" and "unverified" in reason, reason

    tip3 = _amend(work, {"x.py": "x = 1\ndef good(): pass\ny = 2\n"})
    resolve = json.dumps({_FID: {"status": "resolved", "evidence_path": "x.py",
                                 "evidence_quote": "def good(): pass"}})
    decision, reason = _hook(work, _env(tmp_path, "t3.trace", STUB_RESOLVE=resolve))
    assert decision == "allow", reason
    assert _state(work, tip3).get("state") == "done"
    calls = _trace(tmp_path, "t3.trace")
    review = next(c for c in calls if not c.get("resolve_file"))
    assert {f["path"]: f["mode"] for f in review["manifest"]["files"]} == {"x.py": "delta"}
    resolver = next(c for c in calls if c.get("resolve_file"))
    specs = resolver["resolve_manifest"]["prior_files"]
    assert {"path": "x.py", "from_oid": blob1,
            "to_oid": _git(["rev-parse", "HEAD:x.py"], cwd=work)} in specs


def test_evidence_free_still_present_is_rechecked_not_silently_blocking(tmp_path):
    """The field's second failure: every prior answered still_present with empty
    evidence although the code was gone. The gate re-checks once and, if the
    resolver still cannot back its answer, says so in the block reason."""
    work, _ = _blocked_on_x(tmp_path)
    _amend(work, {"x.py": "x = 1\ndef good(): pass\n"})
    decision, reason = _hook(work, _env(tmp_path, "t2.trace"))
    assert decision == "deny" and "unverified" in reason, reason
    assert len([c for c in _trace(tmp_path, "t2.trace") if c.get("resolve_file")]) == 2

    # The re-check can clear it: a scripted answer for the recheck call only.
    work2, _ = _blocked_on_x(tmp_path / "second")
    _amend(work2, {"x.py": "x = 1\ndef good(): pass\n"})
    recheck = json.dumps({_FID: {"status": "resolved", "evidence_path": "x.py",
                                 "evidence_quote": "def good(): pass"}})
    decision, reason = _hook(work2, _env(tmp_path / "second", "t2.trace",
                                         STUB_RESOLVE_RECHECK=recheck))
    assert decision == "allow", reason


def test_carried_file_changed_since_the_finding_goes_to_the_resolver(tmp_path):
    """After a failed resolver run, a push that only touches another file carries
    x.py -- which did change since the finding, so it is re-judged, not replayed."""
    work, _ = _blocked_on_x(tmp_path)
    _amend(work, {"x.py": "x = 1\ndef good(): pass\n"})
    decision, _ = _hook(work, _env(tmp_path, "t2.trace", STUB_RESOLVE_VERDICT="fail"))
    assert decision == "deny"

    _amend(work, {"stable.py": "# stable v3\n"})
    resolve = json.dumps({_FID: {"status": "resolved", "evidence_path": "x.py",
                                 "evidence_quote": "def good(): pass"}})
    decision, reason = _hook(work, _env(tmp_path, "t3.trace", STUB_RESOLVE=resolve))
    assert decision == "allow", reason


def test_unchanged_flagged_code_still_blocks_as_still_present(tmp_path):
    """The loosening must not open a hole: code that is still there blocks, with
    the ordinary label, even when the resolver's answer is malformed."""
    work, _ = _blocked_on_x(tmp_path)
    _amend(work, {"x.py": "x = 2\ndef bad(): pass\n"})
    decision, reason = _hook(work, _env(tmp_path, "t2.trace",
                                        STUB_RESOLVE=json.dumps({_FID: True})))
    assert decision == "deny", reason
    assert "(still present)" in reason and "unverified" not in reason, reason
    # Python verified it against the tip; no second resolver call needed.
    assert len([c for c in _trace(tmp_path, "t2.trace") if c.get("resolve_file")]) == 1
