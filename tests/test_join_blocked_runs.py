"""R05: a completed review that blocked counts as having covered its commits.

The retry after a block reviews only what is new: the earlier blocking findings
are re-verified at the new tip (or replayed when their file did not change), an
old file the fix touches is re-reviewed whole, and a run that timed out, failed,
or left files unreviewed never joins. The explain output says which runs were
joined and why.

Subprocess end to end, the way the pre-push hook runs the gate (`--mode git`),
on a real repo with a bare origin; the reviewer is tests/stub_reviewer.py and
its invocations (range, manifest, the diff text it was handed) are traced.
"""
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")
sys.path.insert(0, _HERE)
sys.path.insert(0, _SCRIPTS)
import rg_store  # noqa: E402
from test_pg4_carry_forward import (  # noqa: E402,F401
    ZERO, _commit, _env, _git, _git_mode, _rid, _sha, _trace, repo)

FINDING = json.dumps({"f1.py": {"severity": "high", "content": "earlier high finding"}})


def _push(repo, env):
    tip = _sha(repo, "HEAD")
    proc = _git_mode(repo, [f"refs/heads/feat {tip} refs/heads/feat {ZERO}"], env)
    return proc, proc.stdout + proc.stderr


def _paths(call):
    return (call.get("manifest") or {}).get("paths") or []


def _edit(repo, name, text, msg="fix"):
    (repo / name).write_text(text)
    _git(["add", "."], cwd=repo)
    _git(["commit", "-q", "-m", msg], cwd=repo)
    return _sha(repo, "HEAD")


def _reviews(tmp_path):
    return [t for t in _trace(tmp_path) if not t.get("resolve_file")]


def _blocked_branch(repo, tmp_path):
    """feat = f1.py (flagged high by the stub) + f2.py, pushed once and blocked."""
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    _commit(repo, "f1.py", "rate = 0\nprint(rate)")
    first_tip = _commit(repo, "f2.py", "old two")
    env = _env(tmp_path, STUB_FINDINGS_FOR=FINDING)
    proc, out = _push(repo, env)
    assert proc.returncode == 1 and "earlier high finding" in out, out
    rec = rg_store.load_verdict(_rid(repo), first_tip)
    assert rec is not None and rec["blocked"] is True
    assert len(_reviews(tmp_path)) == 1
    return first_tip, env


# --- a blocked run followed by a small fix ---------------------------------------

def test_fix_after_a_block_reviews_only_the_fix_and_carries_the_finding(repo, tmp_path):
    first_tip, env = _blocked_branch(repo, tmp_path)
    fix_tip = _edit(repo, "f3.py", "the fix\n")
    proc, out = _push(repo, env)
    reviews = _reviews(tmp_path)
    assert len(reviews) == 2, out
    # The stub was asked about the fix commit alone, not f1/f2 again.
    assert reviews[1]["range"] == f"{first_tip}..{fix_tip}", reviews[1]["range"]
    assert _paths(reviews[1]) == ["f3.py"]
    assert set(reviews[1]["item_files"]) == {"review:f3.py"}
    # The earlier blocking finding is not silently dropped: f1.py did not
    # change, so it is replayed, and the push still blocks on it.
    assert proc.returncode == 1 and "earlier high finding" in out, out
    # The explain lines say which run was joined and why.
    assert f"joined: blocked run at {first_tip[:12]} covers 2 of 3 commit(s)" in out, out
    assert "review scope: 1 new commit(s)" in out, out


def test_a_fix_that_worked_clears_the_earlier_finding(repo, tmp_path):
    first_tip, env = _blocked_branch(repo, tmp_path)
    env.pop("STUB_FINDINGS_FOR")          # the stub finds nothing new
    _edit(repo, "f1.py", "rate = 1\nprint(rate)\n")
    env["STUB_RESOLVE"] = json.dumps({"*": {
        "status": "resolved", "evidence_path": "f1.py", "evidence_quote": "rate = 1"}})
    proc, out = _push(repo, env)
    assert proc.returncode == 0, out
    resolves = [t for t in _trace(tmp_path) if t.get("resolve_file")]
    assert len(resolves) == 1, "the earlier finding was not re-verified at the tip"
    assert "earlier high finding" not in out


def test_a_fix_that_did_not_work_blocks_again(repo, tmp_path):
    first_tip, env = _blocked_branch(repo, tmp_path)
    env.pop("STUB_FINDINGS_FOR")
    _edit(repo, "f1.py", "rate = 0\nprint(rate)\nprint('cosmetic')\n")
    env["STUB_RESOLVE"] = json.dumps({"*": {
        "status": "still_present", "evidence_path": "f1.py",
        "evidence_quote": "rate = 0"}})
    proc, out = _push(repo, env)
    assert proc.returncode == 1, out
    assert "earlier high finding" in out, out
    resolves = [t for t in _trace(tmp_path) if t.get("resolve_file")]
    assert len(resolves) == 1


# --- an old file the fix touches is re-reviewed whole ----------------------------

def test_fix_touching_an_old_file_reviews_the_whole_file_diff(repo, tmp_path):
    """The planted defect spans commits: the old commit set `rate = 0`, the fix
    divides by it. Only a whole-file review shows the reviewer both hunks."""
    first_tip, env = _blocked_branch(repo, tmp_path)
    env.pop("STUB_FINDINGS_FOR")
    _edit(repo, "f1.py", "rate = 0\nprint(rate)\ntotal = 10 / rate\n")
    proc, out = _push(repo, env)
    reviews = _reviews(tmp_path)
    assert len(reviews) == 2, out
    assert _paths(reviews[1]) == ["f1.py"]          # f2.py is covered, not re-reviewed
    shown = reviews[1]["item_files"]["review:f1.py"]
    assert "+rate = 0" in shown, shown               # the OLD hunk (earlier commit)
    assert "+total = 10 / rate" in shown, shown      # the NEW hunk (the fix)
    assert "uncovered commits only" not in shown


def test_fix_touching_only_new_code_stays_narrow(repo, tmp_path):
    first_tip, env = _blocked_branch(repo, tmp_path)
    _edit(repo, "f3.py", "new file\n")
    _push(repo, env)
    assert _paths(_reviews(tmp_path)[1]) == ["f3.py"]


# --- a pass record still joins -----------------------------------------------------

def test_a_pass_record_still_joins(repo, tmp_path):
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    _commit(repo, "f1.py")
    first_tip = _commit(repo, "f2.py")
    env = _env(tmp_path)
    proc, out = _push(repo, env)
    assert proc.returncode == 0, out
    fix_tip = _edit(repo, "f3.py", "the fix\n")
    proc, out = _push(repo, env)
    assert proc.returncode == 0, out
    reviews = _reviews(tmp_path)
    assert len(reviews) == 2 and reviews[1]["range"] == f"{first_tip}..{fix_tip}"
    assert f"joined: pass run at {first_tip[:12]}" in out, out


# --- runs that did not complete never join ------------------------------------------

def _no_join_reviews_all(repo, tmp_path, env_fail):
    """The first push does not complete; the push after a fix reviews the WHOLE
    range, never just the fix."""
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    _commit(repo, "f1.py")
    _commit(repo, "f2.py")
    proc, out = _push(repo, env_fail)
    assert proc.returncode == 1, out
    assert rg_store.load_verdict(_rid(repo), _sha(repo, "HEAD")) is None
    fix_tip = _edit(repo, "f3.py", "the fix\n")
    proc, out = _push(repo, _env(tmp_path))
    assert proc.returncode == 0, out
    last = _reviews(tmp_path)[-1]
    assert last["range"] == f"{_sha(repo, 'main')}..{fix_tip}", last["range"]
    assert sorted(_paths(last)) == ["f1.py", "f2.py", "f3.py"]
    assert "joined:" not in out, out


def test_a_failed_run_does_not_join(repo, tmp_path):
    _no_join_reviews_all(repo, tmp_path, _env(tmp_path, STUB_VERDICT="exit1"))


def test_a_timed_out_run_does_not_join(repo, tmp_path):
    _no_join_reviews_all(
        repo, tmp_path, _env(tmp_path, STUB_SLEEP=30, OCR_TIMEOUT=4, OCR_INLINE_BUDGET=60))


def test_a_run_that_left_files_unreviewed_does_not_join(repo, tmp_path):
    """A review that finished but left files unreviewed (a chunk given up on)
    is flagged incomplete, and _join_records never offers it for a join."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("review_gate_r05", _GATE)
    rg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rg)
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    _commit(repo, "f1.py")
    tip = _commit(repo, "f2.py")
    base = _sha(repo, "main")
    remote = _rid(repo)
    commits = rg_store.record_commits(str(repo), base, tip)
    rg_store.record_verdict(remote, tip, base, "warn", False, "x", commits)
    assert [r["tip"] for r in rg._join_records(str(repo), remote, base, passes_only=False)] == [tip]
    rg._mark_record_incomplete(remote, tip, {"unreviewed": ["f1.py"]})
    assert rg_store.load_verdict(remote, tip)["incomplete"] is True
    assert rg._join_records(str(repo), remote, base, passes_only=False) == []
    assert len(rg._join_records(str(repo), remote, base, passes_only=False,
                                with_incomplete=True)) == 1
    # a complete result never flags a record
    rg_store.record_verdict(remote, tip, base, "pass", False, "", commits)
    rg._mark_record_incomplete(remote, tip, {"unreviewed": [], "findings": []})
    assert not rg_store.load_verdict(remote, tip).get("incomplete")


# --- the explain output ---------------------------------------------------------------

def test_print_range_explains_the_joined_run(repo, tmp_path):
    first_tip, env = _blocked_branch(repo, tmp_path)
    _edit(repo, "f3.py", "the fix\n")
    proc = subprocess.run([sys.executable, _GATE, "--print-range"], cwd=str(repo),
                          env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert f"joined: blocked run at {first_tip[:12]} covers 2 of 3 commit(s)" in proc.stderr
    assert "review scope: 1 new commit(s)" in proc.stderr, proc.stderr
    assert len(_reviews(tmp_path)) == 1, "--print-range must not review anything"


def test_print_range_says_when_nothing_joined(repo, tmp_path):
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    _commit(repo, "f1.py")
    proc = subprocess.run([sys.executable, _GATE, "--print-range"], cwd=str(repo),
                          env=_env(tmp_path), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "joined runs: none" in proc.stderr, proc.stderr
