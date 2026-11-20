"""Ticket 14: scripts/kimchi-verify.sh (kimchi_verify.py), the one command to run
on a machine with a real kimchi. Here it runs against the fake kimchi on PATH
(tests/fake_backend.py) with the stub reviewer, so the real-kimchi run stays a
manual step but the script's own logic -- model-list parsing, the pin check, the
suffix probe, the timed sample review, the report -- is covered.
"""
import os
import shutil
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, _HERE)
sys.path.insert(0, _SCRIPTS)
from test_claude_profile import _backend_env  # noqa: E402

_VERIFY = os.path.join(_SCRIPTS, "kimchi_verify.py")
_SH = os.path.join(_SCRIPTS, "kimchi-verify.sh")


def _verify(tmp_path, runner=None, **extra):
    """(proc, report text) of one verify run; cwd and report live in tmp_path."""
    report = tmp_path / "out" / "kimchi-verify-report.txt"
    (tmp_path / "out").mkdir(exist_ok=True)
    knobs = dict(KIMCHI_VERIFY_REPORT=str(report), KIMCHI_VERIFY_CALL_TIMEOUT=60,
                 KIMCHI_VERIFY_PUSH_TIMEOUT=150, KIMCHI_VERIFY_TARGET_S=120, OCR_SKIP_CANARY=1)
    knobs.update(extra)
    env = _backend_env(tmp_path, clis=("kimchi",), **knobs)
    argv = runner or [sys.executable, _VERIFY]
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", cwd=str(tmp_path / "out"), env=env, timeout=220)
    text = report.read_text(encoding="utf-8") if report.exists() else ""
    return proc, text


def test_success_exits_zero_and_writes_the_report(tmp_path):
    proc, text = _verify(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr + text
    assert "RESULT: ALL CHECKS PASSED" in text
    assert "kimchi 1.34.3" in text                         # --version
    assert "kimchi-dev/kimi-k3.1-pro" in text              # the model list
    assert "[PASS] model pins in model list" in text
    assert "[PASS] thinking suffix `kimchi-dev/kimi-k3.1-pro:high`" in text
    assert "[PASS] thinking suffix `kimchi-dev/kimi-k3.1-pro:max`" in text
    assert "agents.code-reviewer.kimchi.model" in text     # the effective settings
    assert "ocr_doctor" in text
    assert "[PASS] sample review verdict" in text
    assert "[PASS] timing: small push verdict < 120s" in text
    assert "[FAIL]" not in text


def test_report_has_per_session_timings_and_the_debug_rows(tmp_path):
    proc, text = _verify(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr + text
    assert "per-session timings:" in text
    assert "event=call" in text and "event=run" in text
    assert " s=" in text
    assert "session " in text and "outcome=" in text
    assert "whole push (all attempts):" in text


def test_every_pin_is_checked_and_a_missing_id_is_named(tmp_path):
    # the reviewer's default model is not in the list; everything else is
    proc, text = _verify(tmp_path,
                         FAKE_MODELS="deepseek-v4.1-flash-1017,glm-5.3")
    assert proc.returncode != 0
    assert "[FAIL] pin tier1 (code-reviewer)" in text
    assert "`kimi-k3.1-pro`" in text and "NOT in kimchi's model list" in text
    assert "[FAIL] pin session/orchestrator" in text
    assert "[FAIL] pin canary" in text
    assert "[FAIL] pin tier2 (deep-reviewer)" in text
    assert "next: " in text and "rg_settings_cli.py set agents.code-reviewer.kimchi.model" in text
    assert "pin spotter" not in text.replace("spotter  ", "")   # a listed pin raises no FAIL
    assert "RESULT: FAILED" in text
    assert "FAILED" in proc.stdout


def test_a_pin_changed_in_the_settings_is_the_one_checked(tmp_path):
    proc, text = _verify(tmp_path, OCR_TIER1_MODEL="kimchi-dev/glm-9-imaginary:high")
    assert proc.returncode != 0
    assert "`glm-9-imaginary`" in text                  # suffix stripped, id named
    assert "[FAIL] pin tier1 (code-reviewer)" in text


def test_a_rejected_high_suffix_is_reported_clearly(tmp_path):
    proc, text = _verify(tmp_path, FAKE_REJECT_SUFFIX="high")
    assert proc.returncode != 0
    assert "[FAIL] thinking suffix `kimchi-dev/kimi-k3.1-pro:high`" in text
    assert "REJECTED or failed" in text
    assert "unknown thinking level 'high'" in text      # kimchi's own error text
    assert "[PASS] thinking suffix `kimchi-dev/kimi-k3.1-pro:max`" in text
    assert "next: " in text


def test_a_rejected_max_suffix_is_reported_clearly(tmp_path):
    proc, text = _verify(tmp_path, FAKE_REJECT_SUFFIX="max")
    assert proc.returncode != 0
    assert "[FAIL] thinking suffix `kimchi-dev/kimi-k3.1-pro:max`" in text
    assert "unknown thinking level 'max'" in text


def test_no_kimchi_installed_fails_with_a_next_step(tmp_path):
    # a PATH with no kimchi at all
    report = tmp_path / "r.txt"
    env = _backend_env(tmp_path, clis=(), KIMCHI_VERIFY_REPORT=str(report))
    proc = subprocess.run([sys.executable, _VERIFY], capture_output=True, text=True,
                          cwd=str(tmp_path), env=env, timeout=120)
    text = report.read_text(encoding="utf-8")
    assert proc.returncode != 0
    assert "[FAIL] kimchi installed" in text and "next: install kimchi" in text


def test_the_timing_target_is_judged(tmp_path):
    proc, text = _verify(tmp_path, KIMCHI_VERIFY_TARGET_S="0.001")
    assert proc.returncode != 0
    assert "[FAIL] timing: small push verdict < 0s" in text
    assert "[PASS] sample review verdict" in text


def test_does_not_touch_global_git_config_or_the_cwd(tmp_path):
    home = tmp_path / "nohome"
    proc, _ = _verify(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (home / ".gitconfig").exists()
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["kimchi-verify-report.txt"]


def _bash():
    b = shutil.which("bash")
    if not b:
        return None
    try:
        out = subprocess.run([b, "-c", "echo ok"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return b if out.stdout.strip() == "ok" else None


def test_the_shell_wrapper_runs_the_same_thing(tmp_path):
    bash = _bash()
    if not bash:
        pytest.skip("no working bash")
    proc, text = _verify(tmp_path, runner=[bash, _SH.replace("\\", "/")])
    assert proc.returncode == 0, proc.stdout + proc.stderr + text
    assert "RESULT: ALL CHECKS PASSED" in text
