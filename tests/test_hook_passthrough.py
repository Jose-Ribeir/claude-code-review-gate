"""The PreToolUse adapters must never grant permission.

A hook answering `permissionDecision: "allow"` auto-approves the tool call and
bypasses the user's own permission prompts. gate-hook fires on every Bash call,
so an allow on its non-push paths approved arbitrary commands. Every
non-blocking outcome is now a pass-through (exit 0, no permissionDecision) and
only "deny" is ever emitted.
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

from hook_output import parse_pretooluse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_ROOT, "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_SH = os.path.join(_SCRIPTS, "gate-hook.sh")
_PS1 = os.path.join(_SCRIPTS, "gate-hook.ps1")

_BASH = shutil.which("bash")
_PWSH = shutil.which("powershell") or shutil.which("pwsh")


def _payload(cmd):
    return json.dumps({"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": cmd}})


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OCR_", "STUB_"))}
    env.update(extra)
    return env


def _run(argv, cmd, **env):
    return subprocess.run(argv, input=_payload(cmd), capture_output=True, text=True,
                          env=_env(**env), timeout=120)


def _assert_passes(proc):
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", proc.stdout
    assert "permissionDecision" not in proc.stdout


def test_no_script_can_emit_an_allow_decision():
    pat = re.compile(r"""permissionDecision["']?\s*[:=]\s*["']allow""")
    for name in ("gate-hook.sh", "gate-hook.ps1", "review-gate.py"):
        text = open(os.path.join(_SCRIPTS, name), encoding="utf-8").read()
        hits = [ln for ln in text.splitlines() if pat.search(ln)]
        assert not hits, (name, hits)


def test_the_parser_helper_rejects_an_allow():
    allow = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                               "permissionDecision": "allow"}})
    with pytest.raises(AssertionError):
        parse_pretooluse(allow)
    assert parse_pretooluse("")[0] == "pass"


# --- review-gate.py ----------------------------------------------------------

def test_core_passes_through_a_command_that_is_not_a_push():
    _assert_passes(_run([sys.executable, _GATE, "--mode", "hook"], "ls -la"))
    _assert_passes(_run([sys.executable, _GATE, "--mode", "hook"], "echo git push"))


def test_core_guard_passes_through_a_safe_command_and_still_denies_a_write():
    argv = [sys.executable, _GATE, "--mode", "hook"]
    _assert_passes(_run(argv, "git diff HEAD~1", OCR_IN_REVIEW="1"))
    proc = _run(argv, "git diff --output=x.txt HEAD~1", OCR_IN_REVIEW="1")
    assert parse_pretooluse(proc.stdout)[0] == "deny"


# --- gate-hook.sh ------------------------------------------------------------

needs_bash = pytest.mark.skipif(not _BASH, reason="bash not available")


@needs_bash
def test_sh_adapter_passes_through_a_non_push():
    _assert_passes(_run([_BASH, _SH], "rm -rf /tmp/whatever"))
    _assert_passes(_run([_BASH, _SH], "git status"))


@needs_bash
def test_sh_adapter_passes_through_inside_the_review_and_still_vets_writes():
    _assert_passes(_run([_BASH, _SH], "git diff HEAD~1", OCR_IN_REVIEW="1"))
    proc = _run([_BASH, _SH], "git diff --output=x.txt HEAD~1", OCR_IN_REVIEW="1")
    assert parse_pretooluse(proc.stdout)[0] == "deny"


@needs_bash
def test_sh_adapter_fails_closed_when_the_gate_is_missing_and_fail_open_passes(tmp_path):
    broken = tmp_path / "gate-hook.sh"
    shutil.copy(_SH, broken)  # no review-gate.py beside it: a broken install
    proc = _run([_BASH, str(broken)], "git push origin main")
    assert parse_pretooluse(proc.stdout)[0] == "deny"
    # OCR_FAIL_OPEN lets the push go; it must not APPROVE it.
    _assert_passes(_run([_BASH, str(broken)], "git push origin main", OCR_FAIL_OPEN="1"))


# --- gate-hook.ps1 -----------------------------------------------------------

needs_ps = pytest.mark.skipif(not _PWSH, reason="PowerShell not available")


def _ps(cmd, **env):
    return _run([_PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", _PS1], cmd, **env)


@needs_ps
def test_ps1_adapter_passes_through_a_non_push():
    _assert_passes(_ps("rm -rf /tmp/whatever"))


@needs_ps
def test_ps1_adapter_passes_through_inside_the_review_without_a_write_shape():
    _assert_passes(_ps("git diff HEAD~1", OCR_IN_REVIEW="1"))
