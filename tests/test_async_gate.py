"""End-to-end tests for the 0.6.0 asynchronous gate.

Everything here runs the REAL pieces: a temporary git repository with a
bare remote, review-gate.py as a subprocess in `--mode hook` exactly as the
adapters run it, a genuinely detached `--mode supervise` child, and a stub
reviewer (tests/stub_reviewer.py) in place of `claude -p`. Nothing is
monkeypatched, so what passes here is what runs in production.

Why the gate looks like this is documented at ASYNC_DIR in review-gate.py:
the desktop app kills a CLI that is silent for ~16 minutes, and a
PreToolUse hook is silent for as long as it runs.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_STUB = os.path.join(_HERE, "stub_reviewer.py")

sys.path.insert(0, _SCRIPTS)
_spec = importlib.util.spec_from_file_location("review_gate_async", _GATE)
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)


def _git(args, cwd, env=None):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                          check=True, env=env).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A working repo with one pushed commit on main and a bare origin."""
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    _git(["config", "user.email", "t@example.com"], cwd=work)
    _git(["config", "user.name", "t"], cwd=work)
    _git(["config", "commit.gpgsign", "false"], cwd=work)
    # The developer's own global review-gate pre-push hook must not fire on
    # this fixture's pushes: a repo-local hooksPath shadows it.
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    _git(["config", "core.hooksPath", str(hooks)], cwd=work)
    (work / "a.txt").write_text("one\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    return work


def _commit(work, name="feat", msg="change"):
    (work / f"{name}.txt").write_text(msg + "\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", msg], cwd=work)
    return _git(["rev-parse", "HEAD"], cwd=work)


def _env(tmp_path, **stub):
    """Environment for a hook run: isolated data dir, stub reviewer, no
    inherited in-review/bypass state, generous-but-bounded budget."""
    env = dict(os.environ)
    for k in ("OCR_IN_REVIEW", "OCR_FAIL_OPEN", "OCR_ADVISORY", "OCR_FORCE_REVIEW",
              "OCR_LEGACY_RANGE", "OCR_INLINE_BUDGET", "STUB_SLEEP", "STUB_VERDICT", "STUB_TRACE"):
        env.pop(k, None)
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "gate-data")
    env["OCR_REVIEWER_CMD"] = f'"{sys.executable}" "{_STUB}"'
    env["STUB_TRACE"] = str(tmp_path / "stub.trace")
    env["OCR_INLINE_BUDGET"] = "30"  # the clamp minimum
    for k, v in stub.items():
        env[k] = str(v)
    return env


def _hook(work, cmd, env, session="s1", timeout=120):
    payload = json.dumps({"session_id": session, "tool_name": "Bash",
                          "cwd": str(work), "tool_input": {"command": cmd}})
    t0 = time.monotonic()
    proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                          capture_output=True, text=True, cwd=str(work), env=env,
                          timeout=timeout)
    elapsed = time.monotonic() - t0
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    return out["permissionDecision"], out.get("permissionDecisionReason", ""), elapsed, proc.stderr


def _trace(tmp_path):
    p = tmp_path / "stub.trace"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _wait_state(work, tip, want, timeout=60):
    common = review_gate._git_common_dir(str(work))
    path = review_gate._state_path(common, tip)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = review_gate._read_state(path) or {}
        if st.get("state") in want:
            return st
        time.sleep(0.2)
    raise AssertionError(f"state never reached {want}: {review_gate._read_state(path)}")


# --- inline verdicts ----------------------------------------------------------

def test_a_short_review_answers_inline_and_pushes(repo, tmp_path):
    tip = _commit(repo)
    decision, reason, elapsed, _ = _hook(repo, "git push origin main", _env(tmp_path))
    assert decision == "allow", reason
    assert elapsed < 25
    st = _wait_state(repo, tip, {"done"})
    assert st["verdict"] == "pass" and st["blocked"] is False
    # What was reviewed: exactly the commits origin/main lacks.
    (trace,) = _trace(tmp_path)
    assert trace["range"].endswith(".." + tip)
    base = _git(["rev-parse", "origin/main"], cwd=repo)
    assert trace["range"].startswith(base)


def test_the_reviewer_reads_a_detached_worktree_not_the_live_tree(repo, tmp_path):
    tip = _commit(repo)
    _hook(repo, "git push origin main", _env(tmp_path))
    _wait_state(repo, tip, {"done"})
    (trace,) = _trace(tmp_path)
    assert os.path.realpath(trace["cwd"]) != os.path.realpath(str(repo))
    assert "worktrees" in trace["cwd"]
    # ...and it is gone afterwards, both on disk and in git's bookkeeping.
    assert not os.path.isdir(trace["cwd"])
    assert "worktrees" not in _git(["worktree", "list"], cwd=repo).replace(str(repo), "")


def test_a_block_denies_with_findings_and_replays_on_retry(repo, tmp_path):
    tip = _commit(repo)
    env = _env(tmp_path, STUB_VERDICT="block")
    decision, reason, _, _ = _hook(repo, "git push origin main", env)
    assert decision == "deny" and "stub high finding" in reason
    _wait_state(repo, tip, {"done"})
    decision, reason, elapsed, _ = _hook(repo, "git push origin main", env)
    assert decision == "deny" and "stub high finding" in reason
    assert elapsed < 10
    assert len(_trace(tmp_path)) == 1  # no second review


def test_a_reviewer_failure_fails_closed_with_the_reason(repo, tmp_path):
    _commit(repo)
    decision, reason, _, _ = _hook(repo, "git push origin main", _env(tmp_path, STUB_VERDICT="exit1"))
    assert decision == "deny"
    assert "could not complete" in reason
    assert "CREDENTIALS failure" in reason  # the auth hint survived the hop through the state file


# --- past the budget ----------------------------------------------------------

def test_a_long_review_is_denied_past_the_budget_and_joined_by_the_retry(repo, tmp_path):
    tip = _commit(repo)
    env = _env(tmp_path, STUB_SLEEP=45)  # budget is 30
    decision, reason, elapsed, _ = _hook(repo, "git push origin main", env, timeout=90)
    assert decision == "deny", reason
    assert "still running" in reason and "re-run this exact `git push`" in reason
    assert 28 <= elapsed <= 40
    # The supervisor outlived the hook process that started it.
    st = review_gate._read_state(review_gate._state_path(review_gate._git_common_dir(str(repo)), tip))
    assert st["state"] == "running"
    # The retry joins that same run and gets the verdict without a new review.
    decision, reason, elapsed, _ = _hook(repo, "git push origin main", env, timeout=90)
    assert decision == "allow", reason
    assert elapsed < 30
    assert len(_trace(tmp_path)) == 1
    # A note was parked for --mode post and consumed by the successful retry's
    # own report path, or left for a later flush -- either way no orphan
    # supervisor remains.
    st = _wait_state(repo, tip, {"done"})
    assert st["run_id"]


def test_two_hooks_at_once_share_one_review(repo, tmp_path):
    tip = _commit(repo)
    env = _env(tmp_path, STUB_SLEEP=8)
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash", "cwd": str(repo),
                          "tool_input": {"command": "git push origin main"}})
    procs = [subprocess.Popen([sys.executable, _GATE, "--mode", "hook"], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              cwd=str(repo), env=env) for _ in range(2)]
    outs = [p.communicate(payload, timeout=120)[0] for p in procs]
    decisions = [json.loads(o)["hookSpecificOutput"]["permissionDecision"] for o in outs]
    assert decisions == ["allow", "allow"]
    assert len(_trace(tmp_path)) == 1
    _wait_state(repo, tip, {"done"})


# --- what gets reviewed -------------------------------------------------------

def test_the_named_branch_is_reviewed_not_the_checked_out_one(repo, tmp_path):
    main_tip = _commit(repo, "m", "on main")
    _git(["push", "-q", "origin", "main"], cwd=repo)
    _git(["switch", "-q", "-c", "feat/x"], cwd=repo)
    feat_tip = _commit(repo, "f", "on feat")
    _git(["switch", "-q", "main"], cwd=repo)
    (repo / "dirty.txt").write_text("uncommitted\n")
    decision, reason, _, _ = _hook(repo, "git push -u origin feat/x", _env(tmp_path))
    assert decision == "allow", reason
    (trace,) = _trace(tmp_path)
    assert trace["range"] == f"{main_tip}..{feat_tip}"
    st = _wait_state(repo, feat_tip, {"done"})
    assert st["branch"] == "feat/x"


def test_a_push_of_commits_the_remote_already_has_is_allowed_without_review(repo, tmp_path):
    decision, _, _, _ = _hook(repo, "git push origin main", _env(tmp_path))
    assert decision == "allow"
    assert _trace(tmp_path) == []


def test_a_ref_moving_command_before_the_push_is_refused(repo, tmp_path):
    _commit(repo)
    for cmd in ("git switch main && git push origin main",
                "git commit -am x && git push origin main",
                "git push origin main; git push origin main"):
        decision, reason, _, _ = _hook(repo, cmd, _env(tmp_path))
        assert decision == "deny", cmd
        assert "review-gate" in reason
    assert _trace(tmp_path) == []


def test_no_verify_and_unknown_options_are_refused(repo, tmp_path):
    _commit(repo)
    for cmd in ("git push --no-verify origin main", "git push --frobnicate origin main"):
        decision, reason, _, _ = _hook(repo, cmd, _env(tmp_path))
        assert decision == "deny", cmd
    assert _trace(tmp_path) == []


def test_tags_pointing_at_unpushed_commits_are_refused(repo, tmp_path):
    _commit(repo)
    _git(["tag", "v1"], cwd=repo)
    decision, reason, _, _ = _hook(repo, "git push --tags origin", _env(tmp_path))
    assert decision == "deny" and "tags" in reason.lower()
    _git(["push", "-q", "origin", "main"], cwd=repo)
    decision, _, _, _ = _hook(repo, "git push --tags origin", _env(tmp_path))
    assert decision == "allow"


def test_git_C_is_honoured_as_the_push_directory(repo, tmp_path):
    tip = _commit(repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash", "cwd": str(elsewhere),
                          "tool_input": {"command": f'git -C "{repo}" push origin main'}})
    proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                          capture_output=True, text=True, cwd=str(elsewhere), env=_env(tmp_path))
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow", proc.stderr
    _wait_state(repo, tip, {"done"})


# --- inside the review: the write guard ----------------------------------------

def test_the_guard_refuses_output_and_escaping_redirections(tmp_path):
    env = _env(tmp_path)
    env["OCR_IN_REVIEW"] = "1"

    def guard(cmd):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
        proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                              capture_output=True, text=True, env=env, cwd=str(tmp_path))
        return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]

    assert guard("git diff HEAD~1") == "allow"
    assert guard("git diff HEAD~1 > .review_hunks.txt") == "allow"
    assert guard("git diff HEAD~1 > sub/scratch.txt") == "allow"
    assert guard("git diff --output=x.txt HEAD~1") == "deny"
    assert guard("git log -1 --format=x --output out.txt") == "deny"
    assert guard("git diff > .git/scr-push-reviewed-abc") == "deny"
    assert guard("git diff > ../outside.txt") == "deny"
    assert guard(f"git diff > {tmp_path}/abs.txt") == "deny"
    assert guard("git diff > $HOME/x") == "deny"


# --- SessionStart: the false "interrupted" narrative ----------------------------

def test_resume_context_names_a_review_the_previous_process_left(repo, tmp_path):
    tip = _commit(repo)
    env = _env(tmp_path, STUB_SLEEP=6)
    _hook(repo, "git push origin main", env)  # inline, done
    _wait_state(repo, tip, {"done"})
    payload = json.dumps({"session_id": "s1", "source": "resume"})
    proc = subprocess.run([sys.executable, _GATE, "--mode", "resume"], input=payload,
                          capture_output=True, text=True, env=env, cwd=str(repo))
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]
    assert ctx["hookEventName"] == "SessionStart"
    assert "finished: pass" in ctx["additionalContext"]
    assert "not necessarily a user action" in ctx["additionalContext"]
    # A session that pushed nothing gets nothing.
    proc = subprocess.run([sys.executable, _GATE, "--mode", "resume"],
                          input=json.dumps({"session_id": "nobody", "source": "resume"}),
                          capture_output=True, text=True, env=env, cwd=str(repo))
    assert proc.stdout.strip() == ""


# --- --mode post announces the verdict of a review that outlived its push ------

def test_post_announces_the_verdict_of_a_review_denied_for_time(repo, tmp_path):
    tip = _commit(repo)
    env = _env(tmp_path, STUB_SLEEP=40, STUB_VERDICT="warn")
    decision, _, _, _ = _hook(repo, "git push origin main", env, timeout=90)
    assert decision == "deny"
    _wait_state(repo, tip, {"done"}, timeout=60)
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash",
                          "tool_input": {"command": "ls"}})
    proc = subprocess.run([sys.executable, _GATE, "--mode", "post"], input=payload,
                          capture_output=True, text=True, env=env, cwd=str(repo))
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "has finished (verdict: warn)" in ctx
    assert "stub medium finding" in ctx
