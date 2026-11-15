"""Regression tests for the Claude Code PreToolUse adapter scripts/gate-hook.sh.

This adapter emits the Claude Code hookSpecificOutput shape (never "allow";
every non-blocking outcome is a silent pass-through), so it has its own
decision reader -- tests/hook_output.py parses the Kimchi {"decision": ...}
shape instead.

Two pre-push review-gate findings fixed here:

  [medium] the raw `*git*push*` prefilter under-matched quote-spliced pushes
           (`git pu""sh origin main`, `git pu\\sh origin main`): their JSON
           payload contains no literal `push`, so the hook exited 0 and the
           push went UNREVIEWED. The sibling hooks/sh/gate-hook.sh documents
           the miss as proven live. The substring test now runs on a
           de-spliced copy of the payload.

  [low]    a nonzero review-gate.py exit WITHOUT a verdict killed this hook
           via `set -euo pipefail` with no JSON; Claude Code treats a non-2
           nonzero PreToolUse exit as a non-blocking error, so the call
           proceeded unreviewed. The hook now mirrors gate-hook.ps1's
           "No output and a NON-zero exit ... Block rather than let the
           silence read as approval": it emits the deny JSON instead.
"""
import json
import os
import shutil
import subprocess

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HOOK = os.path.join(_ROOT, "scripts", "gate-hook.sh")
_BASH = shutil.which("bash")

needs_bash = pytest.mark.skipif(not _BASH, reason="bash not available")


def _payload(cmd):
    return json.dumps({"session_id": "s1", "cwd": _ROOT,
                       "tool_name": "Bash", "tool_input": {"command": cmd}})


def _env(**extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OCR_", "STUB_", "GIT_TRACE"))}
    # Keep the conftest's private state store (same reason as
    # test_hook_passthrough: stripping OCR_STATE_DIR would touch the real one).
    env["OCR_STATE_DIR"] = os.environ["OCR_STATE_DIR"]
    env.update(extra)
    return env


def _run(hook, cmd, **env):
    return subprocess.run([_BASH, str(hook)], input=_payload(cmd),
                          capture_output=True, text=True, env=_env(**env), timeout=60)


def _decision(proc):
    """Map the adapter's stdout to ("deny", reason) / ("pass", "")."""
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    text = proc.stdout.strip()
    if not text:
        return "pass", ""
    obj = json.loads(text)
    hso = obj.get("hookSpecificOutput") or {}
    decision = hso.get("permissionDecision")
    assert decision != "allow", f"hook must never auto-approve: {text}"
    if decision is None:
        return "pass", ""
    return decision, hso.get("permissionDecisionReason", "")


@needs_bash
class TestQuoteSplicedPrefilter:
    """[medium] the prefilter must see through `git pu""sh`-style splicing."""

    @pytest.fixture
    def no_python_path(self, tmp_path):
        """A PATH where no WORKING python resolves: the hook must then take
        its fail-closed deny branch for any push it considers a push, and
        pass through anything else WITHOUT ever touching Python."""
        shim = tmp_path / "bin"
        shim.mkdir()
        for name in ("python3", "python", "py"):
            (shim / name).write_text("#!/bin/sh\nexit 1\n")
            (shim / name).chmod(0o755)
        return str(shim) + ":/usr/bin:/bin"

    @pytest.mark.parametrize("cmd", [
        "git push origin main",         # control: the plain form always routed
        'git pu""sh origin main',       # quote-spliced subcommand, a real push
        "git pu''sh origin main",
        "git pu\\sh origin main",       # backslash-spliced
    ])
    def test_a_spliced_push_reaches_the_fail_closed_deny(self, no_python_path, cmd):
        # With no working Python the hook DENIES a real push (fail closed).
        # A splice sliding past the prefilter used to pass through UNREVIEWED.
        decision, reason = _decision(_run(_HOOK, cmd, PATH=no_python_path))
        assert decision == "deny", (cmd, reason)
        assert "could not be reviewed" in reason

    @pytest.mark.parametrize("cmd", [
        "git status",
        "git log --oneline",
        "echo hello",
    ])
    def test_non_pushes_still_pass_when_python_is_missing(self, no_python_path, cmd):
        # The de-spliced test must stay as tight as the old one: non-pushes
        # pass through silently even without Python, they are NOT denied.
        assert _decision(_run(_HOOK, cmd, PATH=no_python_path))[0] == "pass"

    def test_a_quoted_push_string_over_routes_exactly_as_before(self, no_python_path):
        # `echo git push` matches the loose prefilter (this predates the fix;
        # the prefilter is deliberately over-approximate). With no working
        # Python that lands on the fail-closed deny -- unchanged by this fix,
        # locked here so a future prefilter change cannot silently loosen it.
        decision, _ = _decision(_run(_HOOK, "echo git push", PATH=no_python_path))
        assert decision == "deny"


@needs_bash
class TestCrashWithoutVerdict:
    """[low] nonzero review-gate.py exit + no stdout must DENY, not vanish."""

    def _install(self, tmp_path, gate_body):
        """A copy of the adapter next to a fake review-gate.py ($DIR-bound)."""
        d = tmp_path / "gate"
        d.mkdir()
        hook = d / "gate-hook.sh"
        shutil.copy(_HOOK, hook)
        (d / "review-gate.py").write_text(gate_body)
        return hook

    def test_a_crash_without_a_verdict_denies(self, tmp_path):
        # review-gate.py dies in a way its own fail-closed wrapper missed:
        # exit 3, no verdict on stdout. The old pipeline died under
        # `set -euo pipefail` with the same code and NO JSON -- Claude Code
        # treats that as a non-blocking error and the call goes through
        # unreviewed. Mirrors gate-hook.ps1: deny instead.
        hook = self._install(tmp_path, "import sys\nsys.exit(3)\n")
        decision, reason = _decision(_run(hook, "git push origin main"))
        assert decision == "deny", (decision, reason)
        assert "exited 3 without returning a verdict" in reason
        assert "not reviewed" in reason

    def test_the_fail_open_escape_hatch_bypasses_the_crash_deny(self, tmp_path):
        hook = self._install(tmp_path, "import sys\nsys.exit(3)\n")
        assert _decision(_run(hook, "git push origin main", OCR_FAIL_OPEN="1"))[0] == "pass"

    def test_a_clean_exit_without_output_still_passes_through(self, tmp_path):
        # Pass-through is silence + exit 0: review-gate.py had nothing to say.
        hook = self._install(tmp_path, "import sys\nsys.exit(0)\n")
        assert _decision(_run(hook, "git push origin main"))[0] == "pass"

    def test_a_verdict_on_stdout_is_relayed_with_the_gates_exit_code(self, tmp_path):
        verdict = '{"hookSpecificOutput":{"hookEventName":"PreToolUse",' \
                  '"permissionDecision":"deny","permissionDecisionReason":"not reviewed"}}'
        hook = self._install(
            tmp_path,
            "import sys\nsys.stdout.write(%r + '\\n')\nsys.exit(2)\n" % verdict)
        proc = _run(hook, "git push origin main")
        assert proc.returncode == 2, (proc.returncode, proc.stderr)
        obj = json.loads(proc.stdout)
        assert obj["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert obj["hookSpecificOutput"]["permissionDecisionReason"] == "not reviewed"
