"""Ticket 05: honest failures.

A review that fails tells the pusher why (the worker's real error plus the first
lines of the supervisor log), a supervisor that dies or never starts counts as an
attempt (so the tip exhausts instead of looping on "still running"), a transcript
that merely mentions a rate limit is not reported as a usage limit, and a
deep-lane timeout is retried once before it fails with its real reason.

Driven through the real gate (`--mode hook`, a detached supervisor) behind the
fake `claude` backend; only external behavior is asserted (deny text, states
reached, launches the backend saw).
"""
import os
import shutil
import subprocess
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SCRIPTS = os.path.join(_ROOT, "scripts")
sys.path.insert(0, _HERE)
sys.path.insert(0, _SCRIPTS)
from hook_output import parse_pretooluse  # noqa: E402
from test_async_gate import _commit, _git, _hook, _wait_state, repo, review_gate  # noqa: E402,F401
from test_claude_profile import _backend_env, _launches  # noqa: E402


def _env(tmp_path, **extra):
    extra.setdefault("OCR_INLINE_BUDGET", 60)
    return _backend_env(tmp_path, OCR_BACKEND="claude", OCR_HARNESS="claude", **extra)


def _own_claude(tmp_path, source):
    """Replace the fake `claude` on PATH with a worker running `source`."""
    script = tmp_path / "hf_worker.py"
    script.write_text(source, encoding="utf-8")
    shim = tmp_path / "shim"
    if sys.platform == "win32":
        (shim / "claude.cmd").write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="ascii")
    else:
        path = shim / "claude"
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        path.chmod(0o755)


# --- (a) the deny message carries the real error and the supervisor log -------

def test_deny_shows_the_worker_error_and_the_supervisor_log(repo, tmp_path):
    _commit(repo)
    env = _env(tmp_path, FAKE_SCENARIO="auth")
    decision, reason, _, _ = _hook(repo, "git push origin main", env)
    assert decision == "deny"
    assert "Invalid API key" in reason, reason                 # the worker's own error
    assert "Supervisor log" in reason, reason
    assert "supervisor-started" in reason, reason              # a line of that log
    # bounded: a handful of short lines, never the whole log
    assert len(reason) < 4000


# --- (b) a supervisor that never starts exhausts the tip ----------------------

_SABOTAGE = (
    "import os, sys\n"
    "if 'supervise' in sys.argv:\n"
    "    sys.stderr.write('HF-DIED: the supervisor could not start\\n')\n"
    "    sys.stderr.flush()\n"
    "    os._exit(7)\n"
)


def _age_state(repo_, tip, seconds=300):
    common = review_gate._git_common_dir(str(repo_))
    path = review_gate._state_path(common, tip)
    st = review_gate._read_state(path)
    for key in ("claimed_ts", "heartbeat_ts"):
        if key in st:
            st[key] = float(st[key]) - seconds
    review_gate._write_state(path, st)
    return path


def _wait_log(path, needle, timeout=30):
    log = str(path)[:-5] + ".supervisor.log"
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with open(log, encoding="utf-8", errors="replace") as fh:
                if needle in fh.read():
                    return
        except OSError:
            pass
        time.sleep(0.2)
    raise AssertionError(f"{needle!r} never reached {log}")


def test_a_supervisor_that_never_starts_exhausts_the_tip(repo, tmp_path):
    tip = _commit(repo)
    hooks = tmp_path / "sabotage"
    hooks.mkdir()
    (hooks / "sitecustomize.py").write_text(_SABOTAGE, encoding="utf-8")
    env = _env(tmp_path, OCR_INLINE_BUDGET=1, PYTHONPATH=str(hooks))
    for _ in range(3):
        decision, reason, _, _ = _hook(repo, "git push origin main", env)
        assert decision == "deny" and "still running" in reason, reason
        path = review_gate._state_path(review_gate._git_common_dir(str(repo)), tip)
        _wait_log(path, "HF-DIED")
        _age_state(repo, tip)          # no heartbeat for minutes: the supervisor is gone
    decision, reason, _, _ = _hook(repo, "git push origin main", env)
    assert decision == "deny"
    assert "exhausted" in reason and "--reset-tip" in reason, reason
    assert "supervisor died or never started" in reason, reason
    assert "HF-DIED" in reason, reason        # the supervisor log's first lines
    assert _launches(tmp_path) == []          # no review ever ran
    st = review_gate._read_state(review_gate._state_path(
        review_gate._git_common_dir(str(repo)), tip))
    assert st["state"] == "exhausted" and st["reason"] == "crash"
    # exhausted-tip behavior is unchanged: a re-push replays it, no new supervisor
    decision, reason, _, _ = _hook(repo, "git push origin main", env)
    assert decision == "deny" and "exhausted" in reason


# --- (c) a transcript that merely mentions a limit is not a usage limit -------

def _failing_worker(first_line, body_line="", tail="boom"):
    return (
        "import sys\n"
        f"sys.stdout.write({first_line!r} + '\\n')\n"
        f"for _ in range(60):\n"
        f"    sys.stdout.write({body_line!r} + ' ' + 'padding ' * 8 + '\\n')\n"
        f"sys.stdout.write({tail!r} + '\\n')\n"
        "sys.exit(2)\n"
    )


def test_rate_limit_phrase_in_passing_is_not_a_usage_limit(repo, tmp_path):
    _commit(repo)
    env = _env(tmp_path)
    _own_claude(tmp_path, _failing_worker(
        "HF-ERR the rate limit helper crashed", "", ""))
    decision, reason, _, _ = _hook(repo, "git push origin main", env)
    assert decision == "deny"
    assert "usage limit" not in reason.lower(), reason
    assert "HF-ERR" in reason and "could not complete" in reason, reason


def test_limit_text_in_a_long_transcript_body_is_not_a_usage_limit(repo, tmp_path):
    _commit(repo)
    env = _env(tmp_path)
    _own_claude(tmp_path, _failing_worker(
        "HF-ERR backend crashed", "tool result: you've hit your session limit", "boom"))
    decision, reason, _, _ = _hook(repo, "git push origin main", env)
    assert decision == "deny"
    assert "usage limit" not in reason.lower(), reason
    assert "HF-ERR" in reason, reason


def test_a_real_usage_limit_is_still_reported(repo, tmp_path):
    _commit(repo)
    env = _env(tmp_path, STUB_VERDICT="limit", PYTHONIOENCODING="utf-8")
    decision, reason, _, _ = _hook(repo, "git push origin main", env)
    assert decision == "deny"
    assert "usage limit" in reason.lower(), reason


# --- (d) a deep-lane timeout is retried once, then fails with its real reason --

@pytest.fixture(scope="module")
def short_deep_gate(tmp_path_factory):
    """The plugin tree with the 60 s floor of OCR_DEEP_TIMEOUT lowered, so a
    deep-lane timeout takes seconds. Nothing else differs from the real gate."""
    dest = tmp_path_factory.mktemp("gate-copy")
    for name in ("scripts", "skills", "agents", "schemas", "extensions", "hooks",
                 "commands", ".claude-plugin"):
        src = os.path.join(_ROOT, name)
        if os.path.isdir(src):
            shutil.copytree(src, str(dest / name),
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    gate = dest / "scripts" / "review-gate.py"
    text = gate.read_text(encoding="utf-8")
    needle = '_env_int("OCR_DEEP_TIMEOUT", 600, floor=60)'
    assert needle in text
    gate.write_text(text.replace(needle, '_env_int("OCR_DEEP_TIMEOUT", 600, floor=1)'),
                    encoding="utf-8")
    return str(gate)


def _hook_with(gate, work, env, timeout=180):
    import json
    payload = json.dumps({"session_id": "s1", "tool_name": "bash", "cwd": str(work),
                          "input": {"command": "git push origin main"}})
    proc = subprocess.run([sys.executable, gate, "--mode", "hook"], input=payload,
                          capture_output=True, text=True, cwd=str(work), env=env,
                          timeout=timeout)
    assert proc.returncode == 0, proc.stderr
    return parse_pretooluse(proc.stdout)


def test_deep_timeout_is_retried_once_then_fails_with_its_real_reason(
        repo, tmp_path, short_deep_gate):
    (repo / "db" / "migrations").mkdir(parents=True)
    (repo / "db" / "migrations" / "001_users.py").write_text("x = 1\n")
    _git(["add", "."], cwd=repo)
    _git(["commit", "-q", "-m", "risky"], cwd=repo)
    tip = _git(["rev-parse", "HEAD"], cwd=repo)
    env = _env(tmp_path, STUB_SLEEP=60, OCR_DEEP_TIMEOUT=2)
    # First timeout: reported, one attempt burned, the same push retries.
    decision, reason = _hook_with(short_deep_gate, repo, env)
    assert decision == "deny" and "timed out after 2s" in reason, reason
    st = _wait_state(repo, tip, {"failed"})
    assert st["tier"] == "deep" and st["attempts"] == 1
    assert st["reason"] != "review"            # not the terminal reason yet
    # The re-push runs the deep review again ...
    decision, reason = _hook_with(short_deep_gate, repo, env)
    assert decision == "deny" and "timed out after 2s" in reason, reason
    assert len(_launches(tmp_path)) == 2
    # ... and the second timeout is final: the real reason, no third try.
    st = _wait_state(repo, tip, {"failed"})
    assert st["reason"] == "review" and st["attempts"] >= 3
    decision, reason = _hook_with(short_deep_gate, repo, env)
    assert decision == "deny" and "timed out after 2s" in reason, reason
    assert len(_launches(tmp_path)) == 2
