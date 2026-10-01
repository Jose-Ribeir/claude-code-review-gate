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
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
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
    t0 = time.monotonic()
    with pytest.raises(review_gate.ReviewGateError) as exc:
        review_gate._run_review_once(str(tmp_path), "hook", str(tmp_path), "t" * 40, "a..b",
                                     timeout=3)
    assert exc.value.is_timeout
    assert time.monotonic() - t0 < 60
    pid = int((tmp_path / "grandchild.pid").read_text())
    assert _wait_gone(pid), "the reviewer's child outlived the timeout kill"
