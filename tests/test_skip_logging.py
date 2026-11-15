"""Skip logging: every honored OCR_FAIL_OPEN bypass leaves a gate_skipped
telemetry line naming the repository, the time, and the push range that went
unreviewed, so gate exceptions are auditable after the fact. A normal run --
blocking or passing -- writes none.

git push --no-verify never invokes the hook, so it cannot appear here by
design; that limitation is documented in the README instead of faked.

Everything goes through the gate's normal entry point: review-gate.py as a
subprocess in hook mode (the Kimchi adapter path) and in git mode (the
installed pre-push hook path). Nothing is monkeypatched.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")

sys.path.insert(0, _SCRIPTS)
from hook_output import parse_pretooluse  # noqa: E402
from conftest import scrub_git_trace  # noqa: E402
_spec = importlib.util.spec_from_file_location("review_gate_skip", _GATE)
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A working repo with main pushed and one unpushed commit on a.py."""
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
    env = scrub_git_trace({k: v for k, v in os.environ.items()
                           if not k.startswith(("OCR_", "STUB_", "KIMCHI_"))})
    env["PATH"] = "/usr/bin:/bin"  # no reviewer here: a review must fail
    # The conftest's private store on BOTH sides: the subprocess writes run
    # state there, the in-process module reads it back.
    env["OCR_STATE_DIR"] = os.environ["OCR_STATE_DIR"]
    env["OCR_INLINE_BUDGET"] = "4"
    env.update({k: str(v) for k, v in kw.items()})
    return env


def _push(work, env):
    """--mode hook, exactly as the Kimchi bash adapter runs the gate."""
    payload = json.dumps({"session_id": "s1", "cwd": str(work),
                          "input": {"command": "git push origin main"}})
    proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                          capture_output=True, text=True, cwd=str(work), env=env,
                          timeout=120)
    assert proc.returncode == 0, proc.stderr
    return parse_pretooluse(proc.stdout)


def _git_push(work, env, ref_lines):
    """--mode git, exactly as the installed pre-push hook runs the gate:
    exit 0 lets the push proceed, exit 1 blocks it."""
    return subprocess.run([sys.executable, _GATE, "--mode", "git", "--remote", "origin"],
                          input=ref_lines, capture_output=True, text=True,
                          cwd=str(work), env=env, timeout=120)


def _wait_terminal(work, tip, timeout=120):
    path = review_gate._state_path(review_gate._git_common_dir(str(work)), tip)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = review_gate._read_state(path) or {}
        if st.get("state") in ("done", "failed"):
            return st
        time.sleep(0.2)
    raise AssertionError(f"state never settled: {review_gate._read_state(path)}")


def _skip_lines():
    """The k=v fields of every gate_skipped line in the always-on metric log."""
    log = review_gate._gate_data_dir() / "review-gate-debug.log"
    out = []
    if log.exists():
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if "event=gate_skipped" in line:
                out.append(dict(p.split("=", 1) for p in line.split()))
    return out


def test_fail_open_bypass_of_a_failed_review_is_logged(repo, tmp_path):
    tip = _git(["rev-parse", "HEAD"], cwd=repo)
    base = _git(["rev-parse", "origin/main"], cwd=repo)

    # No bypass requested: the failed review denies, and nothing is logged.
    decision, _ = _push(repo, _env(tmp_path))
    assert decision == "deny"
    assert _wait_terminal(repo, tip).get("state") == "failed"
    assert _skip_lines() == []

    # The same push with OCR_FAIL_OPEN=1 is allowed -- and audited.
    decision, _ = _push(repo, _env(tmp_path, OCR_FAIL_OPEN="1"))
    assert decision == "pass"
    skips = _skip_lines()
    assert len(skips) == 1
    entry = skips[0]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", entry["ts"])
    assert entry["via"] == "OCR_FAIL_OPEN"
    assert entry["mode"] == "hook"
    assert entry["repo"] == os.path.basename(str(repo))
    assert entry["repo_id"] == review_gate._marker_digest(str(repo))
    assert entry["base"] == base
    assert entry["tip"] == tip


def test_fail_open_multi_ref_push_is_logged_in_git_mode(repo, tmp_path):
    _git(["checkout", "-q", "-b", "topic"], cwd=repo)
    topic = _git(["rev-parse", "HEAD"], cwd=repo)
    _git(["checkout", "-q", "main"], cwd=repo)
    main_tip = _git(["rev-parse", "HEAD"], cwd=repo)
    old = _git(["rev-parse", "origin/main"], cwd=repo)
    lines = (f"refs/heads/main {main_tip} refs/heads/main {old}\n"
             f"refs/heads/topic {topic} refs/heads/topic {'0' * 40}\n")

    # Without the bypass the multi-ref push is refused, and nothing is logged.
    r = _git_push(repo, _env(tmp_path), lines)
    assert r.returncode == 1, r.stderr
    assert _skip_lines() == []

    # With it, the push proceeds -- leaving exactly one audited skip. A push
    # of two branches has no single revision range, so none is claimed.
    r = _git_push(repo, _env(tmp_path, OCR_FAIL_OPEN="1"), lines)
    assert r.returncode == 0, r.stderr
    skips = _skip_lines()
    assert len(skips) == 1
    entry = skips[0]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", entry["ts"])
    assert entry["via"] == "OCR_FAIL_OPEN"
    assert entry["mode"] == "git"
    assert entry["repo"] == os.path.basename(str(repo))
    assert entry["repo_id"] == review_gate._marker_digest(str(repo))
    assert "base" not in entry and "tip" not in entry


def test_a_normal_passing_run_writes_no_skip_entries(repo, tmp_path):
    tip = _git(["rev-parse", "HEAD"], cwd=repo)
    env = _env(tmp_path, OCR_STUB_WORKER="1", STUB_TRACE=str(tmp_path / "stub.trace"))
    _push(repo, env)  # may spawn the async review; the retry joins its verdict
    st = _wait_terminal(repo, tip)
    assert st.get("state") == "done", st
    assert st.get("verdict") == "pass", st
    decision, _ = _push(repo, env)
    assert decision == "pass"
    # A reviewed-and-passed push is not a bypass: no gate_skipped line, and no
    # skip entry hiding among the run's own metric lines.
    log = review_gate._gate_data_dir() / "review-gate-debug.log"
    text = log.read_text(encoding="utf-8") if log.exists() else ""
    assert "event=gate_skipped" not in text
