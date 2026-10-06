"""Part C (0.12.0): chunks reviewed in parallel, one worktree per slot.

End to end through the hook with the stub reviewer (tests/stub_reviewer.py), which
scripts a chunk by the manifest's own chunk_index (STUB_CHUNK_VERDICT,
STUB_CHUNK_SLEEP), so the scripted chunk is the same one at any concurrency.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_async_gate as ag  # noqa: E402
from test_async_gate import review_gate  # noqa: E402


def _calls(tmp_path, name="stub.trace"):
    p = tmp_path / name
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _reviews(tmp_path):
    return [c for c in _calls(tmp_path) if not c.get("resolve_file")]


def _env(tmp_path, conc=2, **extra):
    e = ag._chunk_env(tmp_path, **extra)
    e["OCR_CHUNK_CONCURRENCY"] = str(conc)
    return e


def _push(work, env, timeout=120):
    return ag._hook(work, "git push origin main", env, timeout=timeout)


def _worktrees(work):
    out = ag._git(["worktree", "list", "--porcelain"], cwd=work)
    return [ln.split(" ", 1)[1] for ln in out.splitlines() if ln.startswith("worktree ")]


def _gate_worktree_dirs(tmp_path):
    d = tmp_path / "gate-data" / "worktrees"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def _recorded(work):
    """Paths with a per-file ledger record (not timeout markers or resolutions)."""
    ledger = review_gate._ledger_dir(review_gate._git_common_dir(str(work)))
    if not ledger.exists():
        return set()
    return {json.loads(p.read_text(encoding="utf-8")).get("path")
            for p in ledger.rglob("*.json")
            if p.parent.name not in ("timeouts", "resolutions")}


def _overlap(calls):
    """Pairs of reviewer calls whose runs overlapped (each stub call sleeps `secs`)."""
    return sorted(c["ts"] for c in calls)


def test_concurrency_is_clamped_and_defaults_to_two(monkeypatch):
    monkeypatch.delenv("OCR_CHUNK_CONCURRENCY", raising=False)
    assert review_gate._chunk_concurrency() == 2
    for raw, want in (("1", 1), ("3", 3), ("0", 1), ("-5", 1), ("9", 4), ("x", 2)):
        monkeypatch.setenv("OCR_CHUNK_CONCURRENCY", raw)
        assert review_gate._chunk_concurrency() == want, raw


def test_two_chunks_run_at_once_each_in_its_own_worktree(tmp_path):
    work = ag._big_repo(tmp_path, n_files=4)
    tip = ag._git(["rev-parse", "HEAD"], cwd=work)
    decision, reason, _, _ = _push(work, _env(tmp_path, STUB_SLEEP=3))
    st = ag._wait_state(work, tip, {"done"}, timeout=90)
    assert st["verdict"] == "pass", st
    calls = _reviews(tmp_path)
    assert len(calls) == 4
    starts = _overlap(calls)
    # Two slots: the first two calls start together, well inside one call's 3 s.
    assert starts[1] - starts[0] < 2.0, starts
    cwds = {os.path.realpath(c["cwd"]) for c in calls}
    assert len(cwds) == 2, cwds
    for cwd in cwds:
        assert "worktrees" in cwd and os.path.realpath(str(work)) != cwd
    assert any(c.endswith("-s1") for c in cwds), cwds
    # All gone afterwards, on disk and in git's bookkeeping.
    assert _gate_worktree_dirs(tmp_path) == []
    assert [os.path.realpath(w) for w in _worktrees(work)] == [os.path.realpath(str(work))]


def test_concurrency_one_runs_one_chunk_at_a_time_in_one_worktree(tmp_path):
    work = ag._big_repo(tmp_path, n_files=4)
    tip = ag._git(["rev-parse", "HEAD"], cwd=work)
    _push(work, _env(tmp_path, conc=1, STUB_SLEEP=1))
    ag._wait_state(work, tip, {"done"}, timeout=90)
    calls = _reviews(tmp_path)
    assert len(calls) == 4
    starts = _overlap(calls)
    assert all(b - a >= 0.9 for a, b in zip(starts, starts[1:])), starts
    assert len({os.path.realpath(c["cwd"]) for c in calls}) == 1
    assert [c["manifest"]["chunk_index"] for c in calls] == [0, 1, 2, 3]


def test_out_of_order_completion_gives_the_same_merged_output(tmp_path):
    """Chunk 0 is the slow one, so chunk 1 finishes first: the merged output is the
    same, in chunk order, as a sequential run's."""
    findings = json.dumps({f"mod{i}.py": {"severity": "medium", "content": f"issue {i}"}
                           for i in range(4)})
    merged = {}
    for conc in (1, 2):
        sub = tmp_path / f"c{conc}"
        sub.mkdir()
        work = ag._big_repo(sub, n_files=4)
        tip = ag._git(["rev-parse", "HEAD"], cwd=work)
        env = _env(sub, conc=conc, STUB_FINDINGS_FOR=findings,
                   STUB_CHUNK_SLEEP=json.dumps({"0": 3}))
        _push(work, env)
        ag._wait_state(work, tip, {"done"}, timeout=90)
        git_dir = Path(review_gate._git_dir(str(work)))
        out = json.loads(review_gate._raw_output_path(str(git_dir)).read_text(encoding="utf-8"))
        merged[conc] = [(f.get("path"), f.get("content")) for f in out.get("findings") or []]
    assert merged[1] == merged[2] and len(merged[2]) == 4, merged


def test_a_limit_stops_the_sibling_but_finished_chunks_keep_their_records(tmp_path):
    """Chunk 1 hits the usage limit while chunk 0 is still running: chunk 0 is killed
    (no record), no chunk after them starts, and the run fails as `limit`. A chunk
    that finished before the limit keeps its record: run at concurrency 2 with chunk 2
    hitting the limit after chunks 0 and 1 are recorded."""
    work = ag._big_repo(tmp_path, n_files=4)
    tip = ag._git(["rev-parse", "HEAD"], cwd=work)
    env = _env(tmp_path, STUB_CHUNK_VERDICT=json.dumps({"2": "limit"}),
               STUB_CHUNK_SLEEP=json.dumps({"3": 30}))
    decision, reason, _, _ = _push(work, env)
    assert decision == "deny", reason
    st = ag._wait_state(work, tip, {"failed"}, timeout=60)
    assert st.get("reason") == "limit" and int(st.get("attempts") or 0) == 0, st
    calls = _reviews(tmp_path)
    assert sorted(c["manifest"]["chunk_index"] for c in calls) == [0, 1, 2, 3]
    # chunk 3 (30 s) was killed: the run did not wait for it
    assert time.time() - min(c["ts"] for c in calls) < 25
    recorded = _recorded(work)
    assert {"mod0.py", "mod1.py"} <= recorded, recorded
    assert "mod3.py" not in recorded and "mod2.py" not in recorded, recorded
    assert _gate_worktree_dirs(tmp_path) == []


def test_a_timeout_lets_the_siblings_finish_and_record(tmp_path):
    """Chunk 0 hangs past the chunk timeout (60 s, its minimum) while the other slot
    reviews chunks 1-3: they finish and record, and the run then fails as a timeout
    with chunk 0's file marked to split."""
    work = ag._big_repo(tmp_path, n_files=4)
    tip = ag._git(["rev-parse", "HEAD"], cwd=work)
    env = _env(tmp_path, OCR_CHUNK_TIMEOUT="60", STUB_CHUNK_SLEEP=json.dumps({"0": 120}))
    _push(work, env, timeout=180)
    st = ag._wait_state(work, tip, {"failed"}, timeout=150)
    assert st.get("reason") in ("timeout", "unreviewable"), st
    calls = _reviews(tmp_path)
    assert sorted(c["manifest"]["chunk_index"] for c in calls) == [0, 1, 2, 3]
    recorded = _recorded(work)
    assert {"mod1.py", "mod2.py", "mod3.py"} <= recorded and "mod0.py" not in recorded, recorded
    assert _gate_worktree_dirs(tmp_path) == []


def test_the_budget_is_checked_before_each_dispatch(tmp_path):
    """Budget 5 s, chunks of 6 s, two slots: chunks 0 and 1 start together, then the
    budget stops the run before chunk 2; both finished chunks are recorded."""
    work = ag._big_repo(tmp_path, n_files=4)
    tip = ag._git(["rev-parse", "HEAD"], cwd=work)
    env = _env(tmp_path, STUB_SLEEP=6)
    env["OCR_RUN_BUDGET"] = "5"
    decision, reason, _, _ = _push(work, env)
    assert decision == "deny", reason
    st = ag._wait_state(work, tip, {"failed"}, timeout=60)
    assert st.get("reason") == "budget", st
    assert int(st.get("chunks_done") or 0) == 2, st
    assert sorted(c["manifest"]["chunk_index"] for c in _reviews(tmp_path)) == [0, 1]


def test_progress_fields_stay_consistent(tmp_path):
    work = ag._big_repo(tmp_path, n_files=4)
    tip = ag._git(["rev-parse", "HEAD"], cwd=work)
    common = review_gate._git_common_dir(str(work))
    state_path = review_gate._state_path(common, tip)
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash", "cwd": str(work),
                          "tool_input": {"command": "git push origin main"}})
    proc = subprocess.Popen([sys.executable, ag._GATE, "--mode", "hook"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, cwd=str(work),
                            env=_env(tmp_path, STUB_SLEEP=2))
    proc.stdin.write(payload)
    proc.stdin.close()
    proc.stdin = None  # else POSIX communicate() flushes the closed pipe and raises
    seen = []
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        st = review_gate._read_state(state_path) or {}
        if st.get("state") == "running" and st.get("chunks_total"):
            seen.append(st)
        if st.get("state") == "done":
            break
        time.sleep(0.1)
    proc.communicate(timeout=60)
    assert seen
    for st in seen:
        done, total = int(st.get("chunks_done") or 0), int(st["chunks_total"])
        running = int(st.get("chunks_running") or 0)
        assert 0 <= done <= total == 4 and 0 <= running <= 2, st
        assert done + running <= total, st
        wts = st.get("worktrees")
        if wts:
            assert st["worktree"] == wts[0] and len(wts) == 2, st
    assert any(int(st.get("chunks_running") or 0) == 2 for st in seen), seen


def test_the_renderers_show_chunks_running():
    st = {"chunks_done": 2, "chunks_total": 10, "chunks_running": 2}
    assert review_gate._chunk_progress(st) == ", chunks 2/10 done, 2 running"
    assert review_gate._chunk_progress(dict(st, chunks_running=1)) == ", chunk 3/10"
    assert review_gate._chunk_progress({"chunks_done": 2, "chunks_total": 10}) == ", chunk 3/10"


def test_a_fence_kills_every_child(tmp_path):
    """A newer run claims the tip while two chunks run: both reviewers die."""
    work = ag._big_repo(tmp_path, n_files=4)
    tip = ag._git(["rev-parse", "HEAD"], cwd=work)
    common = review_gate._git_common_dir(str(work))
    state_path = review_gate._state_path(common, tip)
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash", "cwd": str(work),
                          "tool_input": {"command": "git push origin main"}})
    proc = subprocess.Popen([sys.executable, ag._GATE, "--mode", "hook"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, cwd=str(work),
                            env=_env(tmp_path, STUB_SLEEP=60))
    proc.stdin.write(payload)
    proc.stdin.close()
    proc.stdin = None  # else POSIX communicate() flushes the closed pipe and raises
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and len(_reviews(tmp_path)) < 2:
        time.sleep(0.2)
    pids = [c["pid"] for c in _reviews(tmp_path)]
    assert len(pids) == 2, pids
    st = review_gate._read_state(state_path) or {}
    review_gate._write_state(state_path, dict(st, run_id="fenced-new-run"))
    # the heartbeat sees the fence within HEARTBEAT_S and kills both children
    deadline = time.monotonic() + review_gate.HEARTBEAT_S + 20
    while time.monotonic() < deadline and any(_alive(p) for p in pids):
        time.sleep(0.5)
    proc.communicate(timeout=60)
    assert not any(_alive(p) for p in pids), pids
    assert len(_reviews(tmp_path)) == 2          # nothing dispatched after the fence
    ledger = review_gate._ledger_dir(common)
    assert not (ledger.exists() and list(ledger.rglob("*.json")))


def _alive(pid):
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                             text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_the_reaper_protects_every_slot_of_a_live_run(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    common = tmp_path / "common"
    async_d = common / review_gate.ASYNC_DIR
    async_d.mkdir(parents=True)
    wts = tmp_path / "data" / "worktrees"
    old = time.time() - review_gate.MARKER_TTL - 60
    for name in ("live", "live-s1", "dead-s1"):
        (wts / name).mkdir(parents=True)
        os.utime(wts / name, (old, old))
    review_gate._write_state(async_d / "tip.json", {
        "state": "running", "run_id": "r", "heartbeat_ts": time.time(),
        "worktree": str(wts / "live"), "worktrees": [str(wts / "live"), str(wts / "live-s1")]})
    review_gate._reap_async(str(common))
    assert (wts / "live").exists() and (wts / "live-s1").exists()
    assert not (wts / "dead-s1").exists()


def test_no_slot_worktree_beside_the_live_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    assert review_gate._make_slot_worktrees(str(tmp_path), "a" * 40, "r", 3) == []



def test_chunked_review_without_a_worktree_never_wipes_the_live_tree(tmp_path):
    """No detached worktree can be made (the gate's worktrees/ path is a file), so the
    reviewer reads the live tree. The per-chunk reset (git clean -fdxq, git checkout --)
    must not run there: uncommitted edits and untracked or ignored files survive."""
    work = ag._big_repo(tmp_path, n_files=4)
    tip = ag._git(["rev-parse", "HEAD"], cwd=work)
    (tmp_path / "gate-data").mkdir(exist_ok=True)
    (tmp_path / "gate-data" / "worktrees").write_text("not a directory")
    (work / "base.py").write_text("# base\n# uncommitted edit\n")
    (work / "scratch.txt").write_text("untracked\n")
    (work / "secret.env").write_text("ignored\n")
    with open(work / ".git" / "info" / "exclude", "a", encoding="utf-8") as fh:
        fh.write("secret.env\n")
    (work / "notes").mkdir()
    (work / "notes" / "todo.md").write_text("untracked dir\n")
    _push(work, _env(tmp_path, STUB_SLEEP=1))
    st = ag._wait_state(work, tip, {"done"}, timeout=90)
    assert st["verdict"] == "pass", st
    calls = _reviews(tmp_path)
    assert len(calls) == 4   # chunked: the reset path was reached
    assert {os.path.realpath(c["cwd"]) for c in calls} == {os.path.realpath(str(work))}
    assert (work / "base.py").read_text() == "# base\n# uncommitted edit\n"
    assert (work / "scratch.txt").read_text() == "untracked\n"
    assert (work / "secret.env").read_text() == "ignored\n"
    assert (work / "notes" / "todo.md").read_text() == "untracked dir\n"


def test_only_a_gate_made_worktree_is_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    live = tmp_path / "live"
    (live / ".git").mkdir(parents=True)
    assert not review_gate._is_gate_worktree(str(live))
    assert not review_gate._is_gate_worktree(str(tmp_path / "missing"))
    wts = tmp_path / "data" / "worktrees"
    fake = wts / "t-r"          # under worktrees/ but a real checkout (.git directory)
    (fake / ".git").mkdir(parents=True)
    assert not review_gate._is_gate_worktree(str(fake))
    linked = wts / "t-r-s1"     # a linked worktree has a .git file
    linked.mkdir()
    (linked / ".git").write_text("gitdir: elsewhere\n")
    assert review_gate._is_gate_worktree(str(linked))
    assert not review_gate._is_gate_worktree(str(wts))


def test_kill_active_children_kills_every_registered_reviewer():
    procs = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
             for _ in range(2)]
    try:
        for p in procs:
            review_gate._register_child(p)
        assert [c.pid for c in review_gate._active_children()] == sorted(p.pid for p in procs)
        review_gate._kill_active_children()
        for p in procs:
            p.wait(timeout=15)
    finally:
        for p in procs:
            review_gate._unregister_child(p)
            if p.poll() is None:
                p.kill()
    assert review_gate._active_children() == []


@pytest.mark.parametrize("n", [1, 2])
def test_call_stats_are_per_thread(n):
    import threading
    seen = {}

    def work(i):
        review_gate._last_call_stats().clear()
        review_gate._last_call_stats()["i"] = i
        time.sleep(0.05)
        seen[i] = dict(review_gate._last_call_stats())

    ts = [threading.Thread(target=work, args=(i,)) for i in range(n + 1)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert all(seen[i] == {"i": i} for i in seen)
