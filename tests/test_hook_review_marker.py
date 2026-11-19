"""Hooks stay out of the way while a review worker runs (review-speed ticket 07).

review-gate.py marks its worker with OCR_IN_REVIEW=1 and loads this plugin into
it, so every hook entry fires on every Bash call the reviewer makes. With the
marker set each entry must exit at once, silently, WITHOUT starting an
interpreter -- except a command shaped like a file write, which still goes to
the guard (a prompt-injected reviewer must not write through `--output=` or a
redirection).

The no-interpreter claims are proven with a TRIPWIRE: a PATH whose python3 /
python / py are fake interpreters that append to a log when invoked. A positive
control (the same PATH, marker unset, a real push) proves the tripwire can
fire, so an empty log is evidence and not a PATH that finds nothing.

Also covers the kimchi UX hook's interpreter search: it must reach Python
through the Windows launcher `py` when python3 / python are Store stubs.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_ROOT, "scripts")
_GATE_HOOK = os.path.join(_SCRIPTS, "gate-hook.sh")
_POST_HOOK = os.path.join(_SCRIPTS, "post-hook.sh")
_KIMCHI_HOOK = os.path.join(_ROOT, "hooks", "sh", "gate-hook.sh")
_BASH = shutil.which("bash")

needs_bash = pytest.mark.skipif(not _BASH, reason="bash not available")

_PUSH = "git push origin main"


def _payload(cmd):
    return json.dumps({"session_id": "s1", "cwd": _ROOT,
                       "tool_name": "Bash", "tool_input": {"command": cmd}})


def _env(path, **extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OCR_", "STUB_", "GIT_TRACE"))}
    env["OCR_STATE_DIR"] = os.environ["OCR_STATE_DIR"]
    env["PATH"] = path
    env.update(extra)
    return env


def _tripwire(tmp_path, names=("python3", "python", "py")):
    """A bin dir of fake interpreters that log each call and exit 1.

    Returns (PATH, log_file). /usr/bin:/bin stays on PATH for coreutils only;
    the fakes come first so they shadow any real interpreter there.
    """
    shim = tmp_path / "bin"
    shim.mkdir()
    log = tmp_path / "interpreter-calls.log"
    for name in names:
        f = shim / name
        f.write_text('#!/bin/sh\necho "%s $*" >> "%s"\nexit 1\n' % (name, log.as_posix()))
        f.chmod(0o755)
    return "%s:/usr/bin:/bin" % shim, log


def _calls(log):
    return log.read_text().splitlines() if log.exists() else []


def _run(hook, cmd, path, **env):
    return subprocess.run([_BASH, str(hook)], input=_payload(cmd), capture_output=True,
                          text=True, env=_env(path, **env), timeout=60)


# --- the tripwire itself ------------------------------------------------------

@needs_bash
def test_tripwire_control_a_push_without_the_marker_does_start_an_interpreter(tmp_path):
    path, log = _tripwire(tmp_path)
    _run(_GATE_HOOK, _PUSH, path)
    assert _calls(log), "control failed: the tripwire never fired, so the tests below prove nothing"


# --- marker set: no interpreter, no output -------------------------------------

@needs_bash
@pytest.mark.parametrize("hook", [_GATE_HOOK, _POST_HOOK], ids=["pre", "post"])
@pytest.mark.parametrize("cmd", [_PUSH, "git status", "ls -la", "git diff HEAD~1"])
def test_marker_set_exits_silently_without_starting_an_interpreter(tmp_path, hook, cmd):
    path, log = _tripwire(tmp_path)
    proc = _run(hook, cmd, path, OCR_IN_REVIEW="1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", proc.stdout
    assert _calls(log) == []


@needs_bash
@pytest.mark.parametrize("hook", [_GATE_HOOK, _POST_HOOK], ids=["pre", "post"])
def test_marker_exit_needs_no_external_binary(tmp_path, hook):
    # Strongest form: PATH is EMPTY, so a `cat`, `dirname` or subshell-to-an-
    # external before the guard would fail (command not found under set -e).
    # Builtins only before the exit.
    empty = tmp_path / "empty"
    empty.mkdir()
    proc = _run(hook, "git status", str(empty), OCR_IN_REVIEW="1")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


@needs_bash
def test_marker_off_the_post_hook_is_unchanged(tmp_path):
    # No marker: a non-push with nothing parked is still a silent exit.
    path, log = _tripwire(tmp_path)
    state = tmp_path / "data"
    state.mkdir()
    proc = _run(_POST_HOOK, "ls", path, CLAUDE_PLUGIN_DATA=str(state))
    assert proc.returncode == 0 and proc.stdout == ""
    assert _calls(log) == []


# --- the nested-review guard still holds ----------------------------------------

@needs_bash
@pytest.mark.parametrize("cmd", ["git diff --output=x.txt HEAD~1", "echo hi > out.txt"])
def test_marker_set_still_denies_a_file_writing_command_when_it_cannot_vet_it(tmp_path, cmd):
    # Writes are the one shape that may pay an interpreter. With only fake
    # (failing) interpreters the hook must refuse, not wave it through.
    path, log = _tripwire(tmp_path)
    proc = _run(_GATE_HOOK, cmd, path, OCR_IN_REVIEW="1")
    assert proc.returncode == 0, proc.stderr
    obj = json.loads(proc.stdout)
    hso = obj["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "file-writing command" in hso["permissionDecisionReason"]
    assert _calls(log), "the guard path should have looked for an interpreter"


@pytest.mark.skipif(shutil.which("powershell") is None, reason="powershell not available")
@pytest.mark.parametrize("script", ["gate-hook.ps1", "post-hook.ps1"])
def test_powershell_entries_exit_silently_with_the_marker(script):
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         os.path.join(_SCRIPTS, script)],
        input=_payload("git status"), capture_output=True, text=True,
        env=_env(os.environ["PATH"], OCR_IN_REVIEW="1"), timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


# --- the kimchi UX hook finds Python through the `py` launcher ------------------

def _extract_find_python():
    text = open(_KIMCHI_HOOK, encoding="utf-8").read()
    m = re.search(r"^_find_python\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert m, "_find_python not found in hooks/sh/gate-hook.sh"
    return m.group(0)


def _find_python(tmp_path, shims):
    """Run the kimchi hook's own _find_python against a PATH of `shims`.

    `shims` maps name -> shell body. Returns (rc, stdout, PATH-dir).
    """
    shim = tmp_path / "bin"
    shim.mkdir()
    for name, body in shims.items():
        f = shim / name
        f.write_text("#!/bin/sh\n" + body + "\n")
        f.chmod(0o755)
    script = _extract_find_python() + '_find_python\n'
    proc = subprocess.run([_BASH, "-c", script], capture_output=True, text=True,
                          env=_env("%s:/usr/bin:/bin" % shim), timeout=60)
    return proc, shim


# A Windows Store alias stub: resolves on PATH, prints the Store message and
# exits nonzero instead of running Python.
_STORE_STUB = 'echo "Python was not found; run without arguments to install from the Microsoft Store" >&2\nexit 9009'
# A working interpreter for the version probe (`-c "import sys; ..."`).
_WORKING = "exit 0"


@needs_bash
def test_kimchi_hook_finds_python_through_py_when_python3_and_python_are_store_stubs(tmp_path):
    proc, shim = _find_python(tmp_path, {
        "python3": _STORE_STUB, "python": _STORE_STUB, "py": _WORKING})
    assert proc.returncode == 0, proc.stderr
    assert os.path.basename(proc.stdout.strip()) == "py", proc.stdout


@needs_bash
def test_kimchi_hook_still_prefers_a_working_python3(tmp_path):
    proc, shim = _find_python(tmp_path, {"python3": _WORKING, "python": _WORKING, "py": _WORKING})
    assert proc.returncode == 0, proc.stderr
    assert os.path.basename(proc.stdout.strip()) == "python3"


@needs_bash
def test_kimchi_hook_reports_no_python_when_every_candidate_is_a_stub(tmp_path):
    proc, shim = _find_python(tmp_path, {
        "python3": _STORE_STUB, "python": _STORE_STUB, "py": _STORE_STUB})
    assert proc.returncode == 1
    assert proc.stdout.strip() == ""
