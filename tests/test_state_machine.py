"""State-machine + verdict-store unit tests (Kimchi port, C1).

Covers: flock singleflight (exactly one supervisor per tip), stale-pid
respawn, the 3-attempt sticky error state with the doctor pointer, and the
external verdict store's contract (0600, keyed (remote, tip), covered commit
set, corrupt => deny).
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")

sys.path.insert(0, _SCRIPTS)
import rg_store  # noqa: E402
_spec = importlib.util.spec_from_file_location("review_gate_sm", _GATE)
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    _git(["config", "user.email", "t@example.com"], cwd=work)
    _git(["config", "user.name", "t"], cwd=work)
    (work / "a.txt").write_text("one\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    (work / "b.txt").write_text("two\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "feat"], cwd=work)
    return work


# --- the verdict store ---------------------------------------------------------

def test_recorded_verdicts_are_0600_and_keyed_remote_tip(tmp_path):
    rg_store.record_verdict("https://example.com/repo.git", "tip1", "base1",
                            "pass", False, "", ["c1", "c2"])
    p = rg_store.verdict_path("https://example.com/repo.git", "tip1")
    assert p.exists()
    assert p.stat().st_mode & 0o777 == 0o600
    assert str(rg_store.store_root()) in str(p)  # the private OCR_STATE_DIR
    rec = rg_store.load_verdict("https://example.com/repo.git", "tip1")
    assert rec["verdict"] == "pass" and rec["commits"] == ["c1", "c2"]
    # A different remote or tip has no record.
    assert rg_store.load_verdict("https://example.com/other.git", "tip1") is None
    assert rg_store.load_verdict("https://example.com/repo.git", "tip2") is None


@pytest.mark.parametrize("body", [
    "{corrupt",
    "[]",
    '{"remote": "r"}',                                  # partial
    '{"remote": "r", "tip": "t", "verdict": "pass", "commits": "not-a-list"}',
    '{"remote": "r", "tip": "t", "verdict": "pass", "commits": [], "protocol_version": 99}',
])
def test_corrupt_or_partial_verdict_records_raise_never_allow(tmp_path, body):
    rg_store.verdict_path("rem", "tipx").parent.mkdir(parents=True, exist_ok=True)
    rg_store.verdict_path("rem", "tipx").write_text(body, encoding="utf-8")
    with pytest.raises(rg_store.StoreError):
        rg_store.load_verdict("rem", "tipx")


def test_covers_is_a_subset_check():
    rec = {"commits": ["a", "b", "c"], "capped": False}
    assert rg_store.covers(rec, ["b", "c"])
    assert rg_store.covers(rec, [])
    assert not rg_store.covers(rec, ["b", "d"])
    assert not rg_store.covers(None, ["a"])


def test_remote_url_resolves_and_falls_back(repo, tmp_path):
    url = rg_store.remote_url(str(repo), "origin")
    assert str(tmp_path / "origin.git") in url
    assert rg_store.remote_url(str(repo), "no-such-remote") == "no-such-remote"


def test_record_commits_lists_the_range(repo):
    tip = _git(["rev-parse", "HEAD"], cwd=repo)
    base = _git(["rev-parse", "origin/main"], cwd=repo)
    commits = rg_store.record_commits(str(repo), base, tip)
    assert commits == [tip]
    assert rg_store.record_commits(str(repo), tip, tip) == []


# --- the reviewer sandbox (D12) -------------------------------------------------

def test_the_sandbox_is_a_clone_with_scrubbed_tsconfig_and_symlinked_node_modules(repo, tmp_path):
    # Give the repo TS trimmings. They must be COMMITTED: the sandbox is a
    # clone at the push tip, so only committed content is in it.
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "left-behind.txt").write_text("x")
    (repo / "tsconfig.json").write_text(json.dumps(
        {"compilerOptions": {"strict": True}, "plugins": [{"name": "evil"}]}), encoding="utf-8")
    (repo / "nested").mkdir()
    (repo / "nested" / "tsconfig.build.json").write_text(
        json.dumps({"extends": "../tsconfig.json", "plugins": [{"name": "evil2"}]}),
        encoding="utf-8")
    _git(["add", "tsconfig.json", "nested"], cwd=repo)
    _git(["commit", "-q", "-m", "ts trimmings"], cwd=repo)
    tip = _git(["rev-parse", "HEAD"], cwd=repo)

    clone = rg_store.make_review_clone(str(repo), tip, "run1")
    assert clone and os.path.isdir(clone)
    assert "clones" in clone                                    # under the store
    # It is a CLONE, not a worktree: its .git is a directory of its own, and
    # the source repo has no worktree bookkeeping for it.
    assert os.path.isdir(os.path.join(clone, ".git"))
    assert "run1" not in _git(["worktree", "list"], cwd=repo)
    # Checked out at the tip, detached.
    assert _git(["rev-parse", "HEAD"], cwd=clone) == tip
    # tsconfig plugins scrubbed (in the copy only).
    assert "plugins" not in json.loads(Path(clone, "tsconfig.json").read_text())
    assert "plugins" not in json.loads(Path(clone, "nested", "tsconfig.build.json").read_text())
    assert "plugins" in json.loads(Path(repo, "tsconfig.json").read_text())
    # node_modules is a SYMLINK to the real one (read surface, not write).
    assert os.path.islink(os.path.join(clone, "node_modules"))
    # Teardown refuses paths that are not ours.
    rg_store.remove_review_clone(str(repo))
    assert os.path.isdir(clone)
    rg_store.remove_review_clone(clone)
    assert not os.path.isdir(clone)


def test_the_sandbox_scrub_ignores_unparseable_tsconfig(repo, tmp_path):
    (repo / "tsconfig.json").write_text("{not json", encoding="utf-8")
    _git(["add", "tsconfig.json"], cwd=repo)
    _git(["commit", "-q", "-m", "unparseable tsconfig"], cwd=repo)
    tip = _git(["rev-parse", "HEAD"], cwd=repo)
    clone = rg_store.make_review_clone(str(repo), tip, "run2")
    assert clone and Path(clone, "tsconfig.json").exists()  # left untouched
    rg_store.remove_review_clone(clone)


# --- the run state machine ------------------------------------------------------

def _state_path_for(repo, tip):
    common = review_gate._git_common_dir(str(repo))
    return review_gate._state_path(common, tip)


def test_a_stale_running_state_is_restarted(repo, tmp_path):
    tip = _git(["rev-parse", "HEAD"], cwd=repo)
    base = _git(["rev-parse", "origin/main"], cwd=repo)
    # The hook subprocess keys the state file by (tip, base); plant there.
    path = review_gate._state_path(
        review_gate._git_common_dir(str(repo)), tip, base)
    old_run = "deadbeef"
    review_gate._write_state(path, {
        "state": "running", "run_id": old_run, "tip": tip, "repo_root": str(repo),
        "git_dir": str(repo / ".git"), "common_dir": review_gate._git_common_dir(str(repo)),
        "heartbeat_ts": time.time() - 10 * review_gate.STALE_S,   # long silent
        "attempts": 0, "protocol_version": review_gate.PROTOCOL_VERSION,
    })
    # _drive_review sees a supervisor silent past STALE_S and restarts.
    meta = {"tip": tip, "branch": "main", "base": "", "range": "",
            "repo_root": str(repo), "git_dir": str(repo / ".git"),
            "common_dir": review_gate._git_common_dir(str(repo)), "commit_count": "1",
            "push_ref": "refs/heads/main", "remote": "origin", "remote_url": "x"}
    env = dict(os.environ, OCR_STUB_WORKER="1", OCR_INLINE_BUDGET="3",
               STUB_SLEEP="0")
    import subprocess as sp
    proc = sp.Popen([sys.executable, _GATE, "--mode", "hook"], stdin=sp.PIPE,
                    stdout=sp.PIPE, stderr=sp.PIPE, text=True, cwd=str(repo), env=env)
    payload = json.dumps({"tool_name": "bash", "cwd": str(repo),
                          "input": {"command": "git push origin main"}})
    out, _ = proc.communicate(payload, timeout=60)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        st = review_gate._read_state(path) or {}
        if st.get("state") == "done":
            break
        time.sleep(0.2)
    st = review_gate._read_state(path) or {}
    assert st.get("state") == "done"
    assert st["run_id"] != old_run      # respawned under a new run id


def test_three_failed_attempts_make_the_error_state_sticky(repo, tmp_path):
    tip = _git(["rev-parse", "HEAD"], cwd=repo)
    path = _state_path_for(repo, tip)
    now = time.time()
    review_gate._write_state(path, {
        "state": "failed", "run_id": "r1", "tip": tip, "repo_root": str(repo),
        "git_dir": str(repo / ".git"), "common_dir": review_gate._git_common_dir(str(repo)),
        "failed_ts": now, "attempts": 3, "reason": "review", "detail": "boom",
        "protocol_version": review_gate.PROTOCOL_VERSION,
    })
    st = review_gate._read_state(path)
    # A fresh failed state at the cap is returned as-is: no more restarts.
    assert review_gate._drive_review(
        review_gate._git_common_dir(str(repo)), str(repo),
        {"tip": tip, "repo_root": str(repo)}, "hook", 3).get("state") == "failed"
    # ...and the deny message points at the doctor.
    reason = review_gate._failed_reason(st, "hook")
    assert "3 times" in reason
    assert "ocr_doctor.py" in reason


def test_the_supervisor_flock_is_singleflight(repo, tmp_path, monkeypatch):
    tip = _git(["rev-parse", "HEAD"], cwd=repo)
    path = _state_path_for(repo, tip)
    spawned = []
    real = review_gate._spawn_supervisor

    def counting_spawn(state_path, run_id, repo_root):
        spawned.append(run_id)
        return real(state_path, run_id, repo_root)

    monkeypatch.setattr(review_gate, "_spawn_supervisor", counting_spawn)
    import threading
    meta = {"tip": tip, "branch": "main", "base": "", "range": "",
            "repo_root": str(repo), "git_dir": str(repo / ".git"),
            "common_dir": review_gate._git_common_dir(str(repo)), "commit_count": "1",
            "push_ref": "refs/heads/main", "remote": "origin", "remote_url": "x"}
    results = []

    def drive():
        try:
            results.append(review_gate._drive_review(
                review_gate._git_common_dir(str(repo)), str(repo), meta, "hook", 2))
        except Exception as exc:  # noqa: BLE001
            results.append(exc)

    os.environ["OCR_STUB_WORKER"] = "1"
    os.environ["STUB_SLEEP"] = "5"          # hold the run open while both claim
    ts = [threading.Thread(target=drive) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    assert len(spawned) == 1, spawned        # exactly one supervisor
    # Cleanup: let the run finish.
    os.environ["STUB_SLEEP"] = "0"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        st = review_gate._read_state(path) or {}
        if st.get("state") == "done":
            break
        time.sleep(0.3)
    assert (review_gate._read_state(path) or {}).get("state") == "done"


def test_state_files_are_written_0600(repo, tmp_path):
    tip = _git(["rev-parse", "HEAD"], cwd=repo)
    path = _state_path_for(repo, tip)
    review_gate._write_state(path, {"state": "claimed", "tip": tip})
    assert path.stat().st_mode & 0o777 == 0o600
