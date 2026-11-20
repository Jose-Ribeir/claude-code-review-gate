"""Windows .cmd launcher shims: no worker prompt may be cut at a newline.

On Windows an npm-installed `claude` is `claude.CMD`; cmd.exe ends the command
line at the first newline in an argument, so a multi-line `-p` prompt (the
partial re-review note, the retry reminder) reached the CLI truncated and the
review failed with "Unknown command: /review-gate:review". The gate now
launches the native executable behind an npm shim, and flattens a prompt when
the binary is still a batch file.
"""
import importlib.util
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, _SCRIPTS)
import ocr_cmdshim  # noqa: E402

_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_spec = importlib.util.spec_from_file_location("review_gate_cmdshim", _GATE)
rg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rg)

_NPM_SHIM = (
    '@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n:start\r\n'
    'SETLOCAL\r\nCALL :find_dp0\r\n'
    '"%dp0%\\node_modules\\pkg\\bin\\tool.exe"   %*\r\n')


@pytest.fixture
def as_windows(monkeypatch):
    monkeypatch.setattr(ocr_cmdshim.sys, "platform", "win32")


def _shim(tmp_path, text=_NPM_SHIM, with_exe=True):
    shim = tmp_path / "tool.CMD"
    shim.write_bytes(text.encode())
    exe = tmp_path / "node_modules" / "pkg" / "bin" / "tool.exe"
    if with_exe:
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")
    return shim, exe


def test_unwrap_follows_an_npm_shim_to_the_native_exe(tmp_path, as_windows):
    shim, exe = _shim(tmp_path)
    assert ocr_cmdshim.unwrap(str(shim)) == os.path.normpath(str(exe))


def test_unwrap_keeps_the_shim_when_it_cannot_be_followed(tmp_path, as_windows):
    shim, _ = _shim(tmp_path, with_exe=False)          # target missing
    assert ocr_cmdshim.unwrap(str(shim)) == str(shim)
    node, _ = _shim(tmp_path, text='@ECHO off\r\nnode "%dp0%\\cli.js" %*\r\n')
    assert ocr_cmdshim.unwrap(str(node)) == str(node)  # a node-script shim
    assert ocr_cmdshim.unwrap(str(tmp_path / "gone.cmd")) == str(tmp_path / "gone.cmd")


def test_unwrap_leaves_everything_else_alone(tmp_path, monkeypatch):
    shim, _ = _shim(tmp_path)
    monkeypatch.setattr(ocr_cmdshim.sys, "platform", "linux")
    assert ocr_cmdshim.unwrap(str(shim)) == str(shim)
    assert ocr_cmdshim.unwrap(None) is None
    monkeypatch.setattr(ocr_cmdshim.sys, "platform", "win32")
    assert ocr_cmdshim.unwrap("C:/bin/claude.exe") == "C:/bin/claude.exe"


def test_prompt_is_flattened_only_for_a_batch_launcher(as_windows):
    prompt = "/review-gate:review --range a..b --json\n\nCONTEXT: partial\nre-review"
    flat = ocr_cmdshim.prompt_for("C:/x/claude.CMD", prompt)
    assert "\n" not in flat
    assert flat == "/review-gate:review --range a..b --json CONTEXT: partial re-review"
    assert ocr_cmdshim.prompt_for("C:/x/claude.exe", prompt) == prompt
    assert ocr_cmdshim.prompt_for("C:/x/claude.cmd", "one line") == "one line"


def test_prompt_untouched_off_windows(monkeypatch):
    monkeypatch.setattr(ocr_cmdshim.sys, "platform", "linux")
    assert ocr_cmdshim.prompt_for("/x/claude.cmd", "a\nb") == "a\nb"


@pytest.mark.skipif(sys.platform != "win32", reason="the .cmd shim is Windows-only")
def test_partial_review_reaches_a_batch_launcher_as_one_line(tmp_path, monkeypatch):
    """The regression itself: a partial re-review's prompt, through a real
    unrecognised .cmd launcher on PATH, arrives as a single line."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "claude.cmd").write_text("@echo off\r\nexit /b 0\r\n")
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("OCR_BACKEND", "claude")
    for var in ("OCR_CLAUDE_BIN", "OCR_STUB_WORKER", "OCR_REVIEWER_CMD", "OCR_CLAUDE_ARGS"):
        monkeypatch.delenv(var, raising=False)
    seen = {}

    class _Stop(BaseException):
        pass

    real_popen = rg.subprocess.Popen

    def _popen(cmd, *a, **kw):
        # Settings lookup shells out to git (rev-parse) before the launch;
        # only the reviewer launch is the one under test.
        if cmd and cmd[0] == "git":
            return real_popen(cmd, *a, **kw)
        seen["cmd"] = cmd
        raise _Stop

    monkeypatch.setattr(rg.subprocess, "Popen", _popen)
    with pytest.raises(_Stop):
        rg._run_review_once(str(tmp_path), "git", head_sha="abc",
                            push_range="a..b", partial=True)
    cmd = seen["cmd"]
    assert cmd[0].lower().endswith("claude.cmd")
    prompt = cmd[cmd.index("-p") + 1]
    assert prompt.startswith("/review-gate:review --range a..b")
    assert "PARTIAL re-review" in prompt and "\n" not in prompt
