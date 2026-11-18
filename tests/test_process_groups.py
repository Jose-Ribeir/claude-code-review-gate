"""0.10.0: a reviewer is killed together with everything it spawned.

POSIX: the reviewer starts in its own session and `_kill_child` takes the group
down (`os.killpg`); before, only `claude` died and its children survived, holding
the output pipe open. Windows: `taskkill /T` must run while the parent still
exists, which is how it finds the children.
"""
import os
import signal
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_async_gate import review_gate  # noqa: E402

_STUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stub_reviewer.py")


_PARENT = """\
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1], "w").write(str(child.pid))
time.sleep(120)
"""


def _alive(pid):
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                             text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); it is dead for our purposes.
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split()[2] != "Z"
    except OSError:
        return True


def _wait_gone(pid, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.2)
    return False


def _spawn_parent(tmp_path):
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "parent.py"
    script.write_text(_PARENT)
    kw = {"start_new_session": True} if sys.platform != "win32" else {
        "creationflags": review_gate._WIN_FLAGS}
    proc = review_gate.subprocess.Popen([sys.executable, str(script), str(pidfile)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not (pidfile.exists() and pidfile.read_text().strip()):
        time.sleep(0.1)
    return proc, int(pidfile.read_text())


def test_killing_a_reviewer_kills_its_children(tmp_path):
    proc, child_pid = _spawn_parent(tmp_path)
    try:
        assert _alive(child_pid)
        review_gate._kill_child(proc)
        proc.wait(timeout=15)
        assert _wait_gone(child_pid), "the reviewer's child survived the kill"
    finally:
        for pid in (proc.pid, child_pid):
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   capture_output=True)
                else:
                    os.kill(pid, signal.SIGKILL)
            except Exception:
                pass


def test_the_reviewer_is_started_in_its_own_session_on_posix(monkeypatch, tmp_path):
    seen = {}
    import test_review_gate as trg
    monkeypatch.setattr(review_gate.subprocess, "Popen", trg._fake_popen(seen))
    # _find_claude is only an alias; patching it does not reach the _find_kimi
    # call the worker invocation makes, so patch the real name.
    monkeypatch.setattr(review_gate, "_find_kimi", lambda: "kimchi")
    monkeypatch.setenv("OCR_SKIP_CANARY", "1")
    monkeypatch.delenv("OCR_REVIEWER_CMD", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    review_gate._run_review_once(str(tmp_path), "hook", str(tmp_path), "t" * 40, "a..b")
    if sys.platform == "win32":
        assert "start_new_session" not in seen
        assert seen["creationflags"] == review_gate._WIN_FLAGS      # no console window, own group
    else:
        assert seen["start_new_session"] is True
        assert seen["creationflags"] == 0


def test_a_timed_out_reviewer_whose_child_holds_the_pipe_does_not_hang_the_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_REVIEWER_CMD", f'"{sys.executable}" "{_STUB}"')
    monkeypatch.setenv("STUB_SPAWN_CHILD", str(tmp_path / "grandchild.pid"))
    monkeypatch.setenv("STUB_SLEEP", "120")
    monkeypatch.setattr(review_gate, "_find_claude", lambda: None)
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        t0 = time.monotonic()
        with pytest.raises(review_gate.ReviewGateError) as exc:
            review_gate._run_review_once(
                str(tmp_path), "hook", str(tmp_path), "t" * 40, "a..b", timeout=3,
            )
        assert exc.value.is_timeout
        assert time.monotonic() - t0 < 10
        assert unrelated.poll() is None, "an unrelated process group was killed"
        pid = int((tmp_path / "grandchild.pid").read_text())
        assert _wait_gone(pid), "the reviewer's child outlived the timeout kill"
    finally:
        unrelated.kill()
        unrelated.wait(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group guard")
def test_legacy_timeout_reap_is_bounded_and_exposes_cleanup_evidence(monkeypatch, tmp_path):
    import rg_final_review as final_review

    class _PipeHoldingProcess:
        pid = os.getpgrp()
        returncode = None
        stdout = None
        stderr = None

        def __init__(self):
            self.communicate_timeouts = []
            self.killed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def communicate(self, timeout=None):
            self.communicate_timeouts.append(timeout)
            if len(self.communicate_timeouts) == 1:
                raise subprocess.TimeoutExpired("reviewer", timeout, output="partial")
            return "partial", ""

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

        def wait(self, timeout=None):
            assert timeout is not None
            return self.returncode

    process = _PipeHoldingProcess()
    monkeypatch.setattr(review_gate.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setenv("OCR_REVIEWER_CMD", f'"{sys.executable}" "{_STUB}"')
    monkeypatch.setenv("OCR_SKIP_CANARY", "1")

    with pytest.raises(review_gate.ReviewGateError) as captured:
        review_gate._run_review_once(
            str(tmp_path), "hook", str(tmp_path), "t" * 40, "a..b", timeout=0.25,
        )

    assert process.killed
    assert process.communicate_timeouts == [0.25, final_review._REAP_TIMEOUT]
    assert captured.value.cleanup["status"] == "incomplete"
    assert captured.value.cleanup["tree_termination_verified"] is False
    assert captured.value.cleanup["termination_method"] == "direct-child"
    assert captured.value.cleanup["pipe_drain_completed"] is True
