"""The hook adapters tell the core which harness fired them (OCR_HARNESS).

Backend selection (scripts/ocr_profile.py) reads OCR_HARNESS ahead of any
environment sniffing, and sniffing is unreliable from a hook: the detached
supervisor scrubs the harness's session variables. So each adapter exports the
hint for the core it launches: scripts/gate-hook.sh (and .ps1) -> claude,
hooks/sh/gate-hook.sh -> kimchi.
"""
import json
import os
import shutil
import subprocess

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BASH = shutil.which("bash")

# A stand-in core: answers with a block that quotes the harness hint it was
# started with, so the adapter's own relay of the core's answer is the proof.
_ECHO_CORE = (
    "import json, os, sys\n"
    "sys.stdin.read()\n"
    "why = 'harness=' + os.environ.get('OCR_HARNESS', '<unset>')\n"
    "print(json.dumps({'decision': 'block', 'reason': why, 'hookSpecificOutput': {\n"
    "    'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',\n"
    "    'permissionDecisionReason': why}}))\n")


def _adapter_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OCR_", "STUB_", "GIT_TRACE"))}
    env["OCR_STATE_DIR"] = os.environ["OCR_STATE_DIR"]
    env.update(extra)
    return env


@pytest.mark.skipif(_BASH is None, reason="needs bash")
def test_the_claude_bash_adapter_exports_harness_claude(tmp_path):
    for name in ("gate-hook.sh",):
        shutil.copy(os.path.join(_ROOT, "scripts", name), tmp_path / name)
    (tmp_path / "review-gate.py").write_text(_ECHO_CORE, encoding="utf-8")
    payload = json.dumps({"tool_name": "Bash", "cwd": str(tmp_path),
                          "tool_input": {"command": "git push origin main"}})
    proc = subprocess.run([_BASH, str(tmp_path / "gate-hook.sh")], input=payload,
                          capture_output=True, text=True, env=_adapter_env(), timeout=60)
    assert "harness=claude" in proc.stdout, (proc.stdout, proc.stderr)


@pytest.mark.skipif(_BASH is None, reason="needs bash")
def test_the_kimchi_bash_adapter_exports_harness_kimchi(tmp_path):
    gate_dir = tmp_path / "gate"
    gate_dir.mkdir()
    (gate_dir / "review-gate.py").write_text(_ECHO_CORE, encoding="utf-8")
    repo_ = tmp_path / "repo"
    (repo_ / ".git" / "hooks").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_)], check=True)
    (repo_ / ".git" / "hooks" / "pre-push").write_text("# review-gate\n")
    payload = json.dumps({"tool_name": "bash", "cwd": str(repo_),
                          "input": {"command": "git push origin main"}})
    proc = subprocess.run([_BASH, os.path.join(_ROOT, "hooks", "sh", "gate-hook.sh")],
                          input=payload, capture_output=True, text=True, timeout=60,
                          env=_adapter_env(OCR_GATE_DIR=str(gate_dir)))
    assert "harness=kimchi" in proc.stdout, (proc.stdout, proc.stderr)
