"""The doctor reports the backend the gate would select and checks THAT CLI.

Same rules as the gate (scripts/ocr_profile.py), fake `claude` / `kimchi`
executables (tests/fake_backend.py) answering `--version`. The kimchi-only
checks (TESTED_PIN, the deny extension) must not run for a claude machine, and
a claude machine without a working claude CLI must FAIL the doctor just as a
kimchi machine without kimchi does.
"""
import importlib.util
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, _HERE)
sys.path.insert(0, _SCRIPTS)
import fake_backend  # noqa: E402

_spec = importlib.util.spec_from_file_location("ocr_doctor_backend_test",
                                               os.path.join(_SCRIPTS, "ocr_doctor.py"))
ocr_doctor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ocr_doctor)

_SIGNALS = ("OCR_BACKEND", "OCR_HARNESS", "OCR_CLAUDE_BIN", "KIMCHI_BIN", "CLAUDECODE")


def _env(tmp_path, clis, **extra):
    """A doctor env where only the fakes in `clis` are claude/kimchi."""
    env = {k: v for k, v in os.environ.items()
           if k not in _SIGNALS and not k.startswith(("CLAUDE_CODE_", "KIMCHI_", "PI_"))}
    shim = tmp_path / "shim"
    for name in clis:
        fake_backend.install(shim, name)
    keep = [d for d in env.get("PATH", "").split(os.pathsep)
            if d and not any(os.path.isfile(os.path.join(d, n + ext))
                             for n in ("claude", "kimchi") for ext in ("", ".exe", ".cmd", ".bat"))]
    env["PATH"] = os.pathsep.join([str(shim)] + keep)
    home = tmp_path / "nohome"
    home.mkdir(exist_ok=True)
    env.update(LOCALAPPDATA=str(home), USERPROFILE=str(home), HOME=str(home),
               OCR_STATE_DIR=str(tmp_path / "store"), OCR_EXT_DIR=str(tmp_path / "ext"))
    env.update(extra)
    return env


def _checks(tmp_path, clis, **extra):
    out = {}
    for c in ocr_doctor.run_checks(env=_env(tmp_path, clis, **extra)):
        out.setdefault(c["name"], []).append(c)
    return out


def test_a_claude_code_session_reports_claude_and_checks_the_claude_cli(tmp_path):
    checks = _checks(tmp_path, ("claude", "kimchi"), CLAUDECODE="1")
    (backend,) = checks["backend"]
    assert backend["status"] == "ok" and backend["detail"].startswith("claude (auto-detected")
    (claude,) = checks["claude"]
    assert claude["status"] == "ok" and "2.1.0" in claude["detail"]
    # Nothing kimchi-specific is checked, and the canary hint is claude's.
    assert "kimchi" not in checks and "deny-ext" not in checks
    assert "no startup canary" in checks["canary-hint"][0]["detail"]


def test_an_explicit_kimchi_choice_checks_kimchi_even_inside_claude_code(tmp_path):
    checks = _checks(tmp_path, ("claude", "kimchi"), CLAUDECODE="1", OCR_BACKEND="kimchi")
    assert checks["backend"][0]["detail"].startswith("kimchi (OCR_BACKEND override)")
    assert "kimchi" in checks and "claude" not in checks
    assert "deny-ext" in checks or any(n.startswith("deny") for n in checks)


def test_a_missing_claude_cli_fails_the_doctor_for_a_claude_machine(tmp_path):
    checks = _checks(tmp_path, ("kimchi",), OCR_BACKEND="claude")
    assert checks["backend"][0]["status"] == "ok"
    (claude,) = checks["claude"]
    assert claude["status"] == "fail" and "OCR_CLAUDE_BIN" in claude["detail"]


def test_a_missing_kimchi_cli_still_fails_the_doctor_by_default(tmp_path):
    checks = _checks(tmp_path, ())
    assert checks["backend"][0]["detail"].startswith("kimchi (default)")
    assert checks["kimchi"][0]["status"] == "fail"


def test_an_unusable_explicit_backend_is_a_doctor_failure_not_a_silent_fallback(tmp_path):
    checks = _checks(tmp_path, ("claude", "kimchi"), OCR_BACKEND="gemini")
    (backend,) = checks["backend"]
    assert backend["status"] == "fail" and "gemini" in backend["detail"]
    assert "claude" not in checks and "kimchi" not in checks


def test_the_repo_pin_is_honoured_by_the_doctor(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".ocr").mkdir(parents=True)
    (repo / ".ocr" / "config.json").write_text('{"backend": "claude"}', encoding="utf-8")
    env = _env(tmp_path, ("claude", "kimchi"))
    got = {c["name"]: c for c in ocr_doctor.run_checks(repo=str(repo), env=env)}
    assert got["backend"]["detail"].startswith("claude (repo config pin)")
