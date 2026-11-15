"""PR4 (restack-d): retry / exhausted state machine for failed runs.

Spec: docs/plans/restack-content-keyed-coverage.md, "Retry / exhausted state
machine" (decision log Q4/Q10). A retriable failure -- budget, timeout,
crash, unparseable output, spawn failure -- no longer walls the branch: the
next push for the same tip spawns a fresh attempt, up to ATTEMPT_CAP (3) per
tip. MARKER_TTL never replays a deny for a retriable failure; it only
rate-limits respawns (a re-push inside the window joins the in-flight
attempt). The third failure writes `exhausted`: the hook blocks loudly with
the reset instructions, and `--reset-tip <tip>` (equivalent to removing the
state file) is the only way through short of a new tip. Real `block`
verdicts are untouched: blocking requires a true block or `exhausted`.

Same rig as tests/test_async_gate.py: real repo, real hook subprocess, real
detached supervisor, stub reviewer (STUB_VERDICT=garbage induces a fast,
retriable unparseable-output failure).
"""
import os
import subprocess
import sys
import time

import pytest

import test_async_gate as ag

review_gate = ag.review_gate
_git = ag._git
_hook = ag._hook
_trace = ag._trace
_wait_state = ag._wait_state
_env = ag._env
_GATE = ag._GATE


@pytest.fixture
def repo(tmp_path):
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    _git(["config", "user.email", "t@example.com"], cwd=work)
    _git(["config", "user.name", "t"], cwd=work)
    _git(["config", "commit.gpgsign", "false"], cwd=work)
    _git(["config", "core.hooksPath", str(tmp_path / "no-hooks")], cwd=work)
    (tmp_path / "no-hooks").mkdir(exist_ok=True)
    (work / "a.txt").write_text("one\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    return work


def _commit(work, name="feat", msg="change"):
    return ag._commit(work, name, msg)


def _state(work, tip):
    return review_gate._read_state(
        review_gate._state_path(review_gate._git_common_dir(str(work)), tip)) or {}


def _garbage(tmp_path):
    """Env whose reviewer output cannot be parsed: a fast retriable failure."""
    return _env(tmp_path, STUB_VERDICT="garbage")


def _push_fail(work, tmp_path, session="s1"):
    """One push whose attempt fails unparseable; returns the failed state."""
    decision, reason, _, _ = _hook(work, "git push origin main", _garbage(tmp_path),
                                   session=session)
    assert decision == "deny", reason
    return _wait_state(work, _tip_of(work), {"failed"})


def _tip_of(work):
    return _git(["rev-parse", "HEAD"], cwd=work)


# --- retriable failures retry on the next push ---------------------------------

def test_failed_run_retries_on_next_push(repo, tmp_path):
    tip = _commit(repo)
    st = _push_fail(repo, tmp_path)
    assert st["state"] == "failed" and st["attempts"] == 1
    assert st["reason"] == "unparseable-output"      # retriable class
    # The next push spawns a FRESH attempt for the same tip -- it does not
    # replay the deny -- and this one succeeds.
    decision, reason, _, _ = _hook(repo, "git push origin main", _env(tmp_path),
                                   session="s2")
    assert decision == "pass", reason
    st = _wait_state(repo, tip, {"done"})
    assert st["verdict"] == "pass"
    # Two attempts ran: the failed one (retried once inside the run) and the
    # retry. No third.
    assert len(_trace(tmp_path)) == 3


def test_ttl_rate_limits_respawn_but_does_not_replay_deny(repo, tmp_path):
    tip = _commit(repo)
    first = _push_fail(repo, tmp_path)
    assert first["attempts"] == 1
    # A re-push well inside MARKER_TTL (seconds after the failure) must NOT
    # replay the failure deny: it spawns a fresh attempt and, while that
    # attempt is in flight, JOINS it (the still-running deny), never forks
    # another review and never replays the failure.
    env = _env(tmp_path, STUB_SLEEP=8)
    decision, reason, _, _ = _hook(repo, "git push origin main", env,
                                   session="s2", timeout=60)
    assert decision == "deny"
    assert "still running" in reason
    assert "could not complete" not in reason
    _wait_state(repo, tip, {"done"})
    # The verdict replay joins without a third review.
    decision, reason, _, _ = _hook(repo, "git push origin main", env,
                                   session="s3", timeout=60)
    assert decision == "pass", reason
    assert len(_trace(tmp_path)) == 3


def test_third_failure_enters_exhausted_and_blocks_loudly(repo, tmp_path):
    tip = _commit(repo)
    for i in range(3):
        st = _push_fail(repo, tmp_path, session=f"s{i + 1}")
        assert st["state"] == "failed", st
        assert st["attempts"] == i + 1, st
    assert st["state"] == "failed" and st["attempts"] == 3   # the third failure
    # The fourth push finds the exhausted tip: it blocks with the reset
    # instructions, it does not spawn a fourth attempt.
    n_trace = len(_trace(tmp_path))
    decision, reason, _, _ = _hook(repo, "git push origin main", _garbage(tmp_path),
                                   session="s4")
    assert decision == "deny"
    assert "--reset-tip" in reason
    assert tip in reason
    assert "exhaust" in reason.lower()
    assert len(_trace(tmp_path)) == n_trace       # nothing was re-run
    st = _state(repo, tip)
    assert st["state"] == "exhausted" and st["attempts"] >= 3


def test_reset_tip_clears_exhausted(repo, tmp_path):
    tip = _commit(repo)
    for i in range(3):
        _push_fail(repo, tmp_path, session=f"s{i + 1}")
    # The third failure leaves `failed` at the cap: the run that produced it
    # keeps its failed deny record, and the push that FINDS the capped state
    # walls it as `exhausted` (same as the sibling test above). Drive that
    # transition before exercising --reset-tip.
    decision, reason, _, _ = _hook(repo, "git push origin main", _garbage(tmp_path),
                                   session="s4")
    assert decision == "deny"
    assert "--reset-tip" in reason and "exhaust" in reason.lower()
    assert _state(repo, tip)["state"] == "exhausted"
    # The flag is equivalent to removing the state file: it clears the
    # exhausted/failed state for the tip and nothing else.
    proc = subprocess.run([sys.executable, _GATE, "--reset-tip", tip],
                          capture_output=True, text=True, cwd=str(repo),
                          env=_env(tmp_path), timeout=60)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    common = review_gate._git_common_dir(str(repo))
    assert not list(review_gate._async_dir(common).glob(f"*{tip}.json"))
    # The next push gets a fresh attempt -- and it succeeds.
    decision, reason, _, _ = _hook(repo, "git push origin main", _env(tmp_path),
                                  session="s5")
    assert decision == "pass", reason
    assert _wait_state(repo, tip, {"done"})["verdict"] == "pass"


def test_real_block_verdict_still_blocks_without_retry(repo, tmp_path):
    tip = _commit(repo)
    env = _env(tmp_path, STUB_VERDICT="block")
    decision, reason, _, _ = _hook(repo, "git push origin main", env)
    assert decision == "deny" and "stub high finding" in reason
    st = _wait_state(repo, tip, {"done"})
    assert st["verdict"] == "block" and st["blocked"] is True
    assert int(st.get("attempts") or 0) == 0      # a verdict is never an attempt
    # A re-push replays the block verdict verbatim: no retry semantics.
    decision, reason, elapsed, _ = _hook(repo, "git push origin main", env)
    assert decision == "deny" and "stub high finding" in reason
    assert elapsed < 10
    assert len(_trace(tmp_path)) == 1
    assert _state(repo, tip)["state"] == "done"
