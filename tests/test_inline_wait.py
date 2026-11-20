"""Ticket 04: the inline wait.

One push waits for the verdict (claude hook, git/terminal mode); a kimchi hook
keeps its short wait and the deny-then-re-push flow; a wait that runs out DENIES
and never allows, and the re-push joins the running review. Driven through the
real gate (`--mode hook` / `--mode git`, a detached supervisor) behind the fake
backends, with a slow stub reviewer.
"""
import json
import os
import subprocess
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, _HERE)
sys.path.insert(0, _SCRIPTS)
from test_async_gate import _commit, _git, _hook, _wait_state, repo  # noqa: E402,F401
from test_claude_profile import _backend_env, _launches  # noqa: E402

_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_CLI = os.path.join(_SCRIPTS, "rg_settings_cli.py")
ZERO = "0" * 40


def _git_mode(work, tip, env):
    base = _git(["rev-parse", "origin/main"], cwd=work)
    line = f"refs/heads/main {tip} refs/heads/main {base}\n"
    t0 = time.monotonic()
    proc = subprocess.run([sys.executable, _GATE, "--mode", "git", "--remote", "origin"],
                          input=line, capture_output=True, text=True, cwd=str(work),
                          env=env, timeout=180)
    return proc, time.monotonic() - t0


def test_claude_hook_returns_the_verdict_on_the_first_push(repo, tmp_path):
    tip = _commit(repo)
    env = _backend_env(tmp_path, OCR_BACKEND="claude", OCR_HARNESS="claude",
                       STUB_SLEEP=6, OCR_INLINE_BUDGET=60)
    decision, reason, elapsed, _ = _hook(repo, "git push origin main", env)
    assert decision == "pass", reason
    assert elapsed >= 5, "the stub takes ~6 s, so the verdict was not awaited"
    assert _wait_state(repo, tip, {"done"})["verdict"] == "pass"
    assert len(_launches(tmp_path)) == 1


def test_claude_hook_default_wait_is_not_the_old_four_second_clamp(repo, tmp_path):
    """No OCR_INLINE_BUDGET at all: the built-in claude-hook wait (300 s) applies."""
    tip = _commit(repo)
    env = _backend_env(tmp_path, OCR_BACKEND="claude", OCR_HARNESS="claude",
                       STUB_SLEEP=6, OCR_INLINE_BUDGET=None)
    decision, reason, elapsed, _ = _hook(repo, "git push origin main", env)
    assert decision == "pass", reason
    assert elapsed >= 5
    _wait_state(repo, tip, {"done"})


def test_git_mode_returns_the_verdict_on_the_first_push(repo, tmp_path):
    tip = _commit(repo)
    env = _backend_env(tmp_path, OCR_BACKEND="claude", STUB_SLEEP=6,
                       OCR_INLINE_BUDGET_GIT=60, OCR_INLINE_BUDGET=None)
    proc, elapsed = _git_mode(repo, tip, env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert elapsed >= 5
    assert _wait_state(repo, tip, {"done"})["verdict"] == "pass"


def test_kimchi_hook_keeps_its_short_wait_then_joins_on_repush(repo, tmp_path):
    tip = _commit(repo)
    env = _backend_env(tmp_path, OCR_BACKEND="kimchi", OCR_HARNESS="kimchi",
                       OCR_SKIP_CANARY="1", STUB_SLEEP=12, OCR_INLINE_BUDGET=60)
    decision, reason, elapsed, _ = _hook(repo, "git push origin main", env)
    assert decision == "deny" and "is still running" in reason, reason
    assert elapsed < 9, f"a kimchi hook must answer inside its short wait ({elapsed:.1f}s)"
    _wait_state(repo, tip, {"done"})
    decision, reason, _, _ = _hook(repo, "git push origin main", env)
    assert decision == "pass", reason
    assert len(_launches(tmp_path)) == 1, "the re-push must join, not re-review"


def test_wait_exceeded_denies_never_allows_and_repush_joins(repo, tmp_path):
    tip = _commit(repo)
    env = _backend_env(tmp_path, OCR_BACKEND="claude", OCR_HARNESS="claude",
                       STUB_SLEEP=12, OCR_INLINE_BUDGET=2)
    decision, reason, elapsed, _ = _hook(repo, "git push origin main", env)
    assert decision == "deny", reason       # never "pass" while the review runs
    assert "is still running" in reason and "NOT executed" in reason
    assert elapsed < 10
    # The review keeps running under the supervisor; the re-push joins it.
    env["OCR_INLINE_BUDGET"] = "60"
    decision, reason, _, _ = _hook(repo, "git push origin main", env)
    assert decision == "pass", reason
    assert len(_launches(tmp_path)) == 1
    assert _wait_state(repo, tip, {"done"})["verdict"] == "pass"


def test_git_mode_wait_exceeded_blocks_the_push(repo, tmp_path):
    tip = _commit(repo)
    env = _backend_env(tmp_path, OCR_BACKEND="claude", STUB_SLEEP=12,
                       OCR_INLINE_BUDGET_GIT=2, OCR_INLINE_BUDGET=None)
    proc, _ = _git_mode(repo, tip, env)
    assert proc.returncode != 0, "a git push must not go through while the review runs"
    assert "still running" in (proc.stdout + proc.stderr)
    _wait_state(repo, tip, {"done"})


def _show(env_extra, tmp_path):
    env = dict(os.environ)
    for k in ("OCR_INLINE_BUDGET", "OCR_INLINE_BUDGET_GIT"):
        env.pop(k, None)
    env["OCR_SETTINGS_FILE"] = str(tmp_path / "none.json")
    env.update(env_extra)
    out = subprocess.run([sys.executable, _CLI, "show", "--json"], capture_output=True,
                         text=True, env=env, cwd=str(tmp_path), timeout=60)
    assert out.returncode == 0, out.stderr
    return {r["key"]: r for r in json.loads(out.stdout)}


def test_settings_show_reports_the_wait_and_its_source(tmp_path):
    rows = _show({}, tmp_path)
    assert rows["behavior.inline_wait_seconds.hook"]["value"] == 300
    assert rows["behavior.inline_wait_seconds.hook"]["source"] == "default"
    assert rows["behavior.inline_wait_seconds.git"]["value"] == 300
    rows = _show({"OCR_INLINE_BUDGET": "42", "OCR_INLINE_BUDGET_GIT": "17"}, tmp_path)
    assert rows["behavior.inline_wait_seconds.hook"]["value"] == 42
    assert rows["behavior.inline_wait_seconds.hook"]["source"] == "env:OCR_INLINE_BUDGET"
    assert rows["behavior.inline_wait_seconds.git"]["source"] == "env:OCR_INLINE_BUDGET_GIT"


def test_settings_file_sets_the_wait(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"behavior": {"inline_wait_seconds": {"hook": 120}}}))
    rows = _show({"OCR_SETTINGS_FILE": str(f)}, tmp_path)
    got = rows["behavior.inline_wait_seconds.hook"]
    assert got["value"] == 120 and got["source"].startswith(("project:", "user:"))
