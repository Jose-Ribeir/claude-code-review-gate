"""C2: the fail-closed worker trio (port plan D9).

No parseable verdict => no verdict recorded => deny. Three ways a worker can
fail to produce one -- the binary is absent, the output is garbage, the
process hangs past the budget -- must all end in a `failed` state with the
attempts counter incremented, never a warn-and-allow, and 3 attempts must
make the error state sticky (no further supervisor spawns).
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

sys.path.insert(0, _SCRIPTS)
import rg_store  # noqa: E402
from hook_output import parse_pretooluse  # noqa: E402
_spec = importlib.util.spec_from_file_location("review_gate_fc", _GATE)
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split()[2] != "Z"
    except OSError:
        return True


@pytest.fixture
def repo(tmp_path):
    remote, work = tmp_path / "origin.git", tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    for k, v in (("user.email", "t@t.com"), ("user.name", "t"),
                 ("commit.gpgsign", "false"), ("core.hooksPath", str(tmp_path / "nh"))):
        _git(["config", k, v], cwd=work)
    (tmp_path / "nh").mkdir(exist_ok=True)
    (work / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    (work / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "feat"], cwd=work)
    return work


def _env(tmp_path, **kw):
    """A hook env with NO reviewer seam: no stub, no KIMCHI_BIN, and a PATH
    where `kimchi` is only what the test put there (or nothing)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OCR_", "STUB_", "KIMCHI_"))}
    env["PATH"] = "/usr/bin:/bin"
    # The conftest's private store on BOTH sides: the subprocess writes run
    # state / verdicts there, and the in-process _wait_terminal poll reads it.
    env.update({"OCR_STATE_DIR": os.environ["OCR_STATE_DIR"],
                "OCR_INLINE_BUDGET": "4"})
    env.update({k: str(v) for k, v in kw.items()})
    return env


def _push(work, env):
    payload = json.dumps({"session_id": "s1", "cwd": str(work),
                          "input": {"command": "git push origin main"}})
    proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                          capture_output=True, text=True, cwd=str(work), env=env,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr
    return parse_pretooluse(proc.stdout)


def _wait_terminal(work, tip, timeout=120):
    path = review_gate._state_path(review_gate._git_common_dir(str(work)), tip)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = review_gate._read_state(path) or {}
        if st.get("state") in ("done", "failed"):
            return st
        time.sleep(0.2)
    return review_gate._read_state(path) or {}


def _no_verdict_recorded(work):
    url = rg_store.remote_url(str(work), "origin")
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    return rg_store.load_verdict(url, tip) is None


def test_missing_kimchi_binary_denies_and_points_at_the_doctor(repo, tmp_path):
    tip = _git(["rev-parse", "HEAD"], cwd=repo)
    decision, _ = _push(repo, _env(tmp_path))
    assert decision == "deny"
    st = _wait_terminal(repo, tip)
    assert st.get("state") == "failed"
    assert int(st.get("attempts") or 0) == 1
    assert st.get("reason") == "review"
    assert "not found" in str(st.get("detail"))
    assert _no_verdict_recorded(repo)
    # Three attempts => sticky: the 4th push denies with the doctor pointer
    # instead of spawning yet another supervisor.
    for _ in range(2):
        _push(repo, _env(tmp_path))
        st = _wait_terminal(repo, tip)
        assert st.get("state") == "failed"
    assert int(st.get("attempts") or 0) >= 3
    decision, reason = _push(repo, _env(tmp_path))
    assert decision == "deny"
    assert "3 times" in reason
    assert "ocr_doctor.py" in reason
    assert _no_verdict_recorded(repo)


def test_garbage_worker_output_denies(repo, tmp_path):
    tip = _git(["rev-parse", "HEAD"], cwd=repo)
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "kimchi").write_text("#!/bin/sh\necho 'I could not review this.'\nexit 0\n")
    (shim / "kimchi").chmod(0o755)
    decision, _ = _push(repo, _env(tmp_path, PATH=f"{shim}:/usr/bin:/bin"))
    assert decision == "deny"
    st = _wait_terminal(repo, tip)
    assert st.get("state") == "failed"
    assert int(st.get("attempts") or 0) == 1
    assert "parse review output" in str(st.get("detail"))
    assert _no_verdict_recorded(repo)


def test_a_hung_worker_is_tree_killed_and_denied(repo, tmp_path):
    if sys.platform == "win32":
        import pytest
        pytest.skip("POSIX process-group kill")
    tip = _git(["rev-parse", "HEAD"], cwd=repo)
    shim = tmp_path / "bin"
    shim.mkdir()
    pidfile = tmp_path / "grandchild.pid"
    # The shim spawns a grandchild that would outlive it, then sleeps past the
    # gate's per-call budget (OCR_TIMEOUT). The tree kill must take both down.
    (shim / "kimchi").write_text(
        "#!/bin/sh\n"
        f"\"{sys.executable}\" -c 'import time; time.sleep(600)' &\n"
        f"echo $! > \"{pidfile}\"\n"
        "sleep 600\n")
    (shim / "kimchi").chmod(0o755)
    decision, _ = _push(repo, _env(tmp_path, PATH=f"{shim}:/usr/bin:/bin", OCR_TIMEOUT="6"))
    assert decision == "deny"
    st = _wait_terminal(repo, tip)
    assert st.get("state") == "failed"
    assert int(st.get("attempts") or 0) == 1
    # The process TREE died: the grandchild did not outlive the timeout kill.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and pidfile.exists() and not pidfile.read_text().strip():
        time.sleep(0.1)
    pid = int(pidfile.read_text().strip())
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.2)
    assert not _alive(pid), "the hung worker's grandchild survived the tree kill"
    assert _no_verdict_recorded(repo)
