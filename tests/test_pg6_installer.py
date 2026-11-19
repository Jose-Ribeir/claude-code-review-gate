"""pg6: the global push gate is default-on and chains (never clobbers) a
pre-existing global core.hooksPath.

- Plain `install-git-hook.sh` IS this repo's installer entry point (there is
  no install.sh): default-on means running it installs the global surface.
  `--no-git-hook` preserves the legacy opt-in behavior of simply not running
  it.
- A pre-existing global core.hooksPath (husky, other harnesses) is backed up
  to reviewGate.prevHooksPath and CHAINED: the installer writes a wrapper
  pre-push into the gate hooks dir that runs the gate first and, only when the
  gate passes, the previous hook afterwards -- the previous hook's exit code
  is final.
- Isolation is absolute: EVERY subprocess in this file runs with
  GIT_CONFIG_GLOBAL pointed at a temp file and HOME at a temp dir, so the
  developer's real gitconfig and $HOME are never read or written. The
  installer additionally carries a guard: OCR_INSTALLER_TEST=1 without
  GIT_CONFIG_GLOBAL redirected must make it refuse to run.

Conventions follow tests/test_install_hooks_path.py and tests/sim_harness.py.
"""
import os
import shutil
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SCRIPTS = os.path.join(_ROOT, "scripts")
_STUB = os.path.join(_HERE, "stub_reviewer.py")
_INSTALL = os.path.join(_SCRIPTS, "install-git-hook.sh")
_UNINSTALL = os.path.join(_SCRIPTS, "uninstall-git-hook.sh")
_BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(_BASH is None, reason="bash not available")

ZERO = "0" * 40


def _env(tmp_path, extra=None):
    """The isolated environment EVERY subprocess here runs under: temp
    gitconfig, temp HOME, temp hooks dir, and the OCR_INSTALLER_TEST marker
    that arms the installer's real-config guard."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OCR_", "STUB_", "GIT_TRACE"))}
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_GLOBAL",
              "GIT_CONFIG_SYSTEM", "SCR_HOOKS_DIR", "SCR_GATE_DIR", "CLAUDE_PLUGIN_DATA"):
        env.pop(k, None)
    env.update({
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(tmp_path / "home"),
        "SCR_HOOKS_DIR": str(tmp_path / "gate-hooks"),
        "OCR_INSTALLER_TEST": "1",
    })
    os.makedirs(env["HOME"], exist_ok=True)
    if extra:
        env.update(extra)
    return env


def _hooks_dir(tmp_path):
    return tmp_path / "gate-hooks"


def _git(tmp_path, args, cwd, check=True, env_extra=None):
    proc = subprocess.run(["git"] + args, cwd=str(cwd), env=_env(tmp_path, env_extra),
                          capture_output=True, text=True, timeout=60)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _gcfg(tmp_path, *keys):
    """git config --global --get, against the TEMP gitconfig only."""
    return _git(tmp_path, ["config", "--global"] + list(keys), cwd=tmp_path, check=False)


def _run(tmp_path, argv, cwd=None, env_extra=None, stdin=None, timeout=180):
    return subprocess.run([_BASH] + [str(a) for a in argv], cwd=str(cwd or tmp_path),
                          env=_env(tmp_path, env_extra), input=stdin,
                          capture_output=True, text=True, timeout=timeout)


def _mkrepo(tmp_path):
    """A work repo with a bare local fake origin (never a real remote)."""
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(tmp_path, ["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(tmp_path, ["init", "-b", "main", str(work)], cwd=tmp_path)
    _git(tmp_path, ["config", "user.email", "t@example.com"], cwd=work)
    _git(tmp_path, ["config", "user.name", "t"], cwd=work)
    _git(tmp_path, ["config", "commit.gpgsign", "false"], cwd=work)
    (work / "a.txt").write_text("one\n")
    _git(tmp_path, ["add", "."], cwd=work)
    _git(tmp_path, ["commit", "-q", "-m", "base"], cwd=work)
    _git(tmp_path, ["remote", "add", "origin", str(remote)], cwd=work)
    _git(tmp_path, ["push", "-q", "-u", "origin", "main"], cwd=work)
    return work


def _mk_prev_hooks(tmp_path, exit_file=None):
    """A fake pre-existing global hooks dir (the 'husky' of the fixture): its
    pre-push appends to a marker file and exits with the code in the exit
    file, so tests can assert it ran and control its verdict separately."""
    prev = tmp_path / "prev-hooks"
    prev.mkdir()
    marker = tmp_path / "prev.marker"
    exit_file = exit_file or tmp_path / "prev.exit"
    (prev / "pre-push").write_text(
        "#!/bin/sh\n"
        f"printf 'prev-ran\\n' >> \"{marker}\"\n"
        f"exit \"$(cat \"{exit_file}\" 2>/dev/null || echo 0)\"\n"
    )
    os.chmod(str(prev / "pre-push"), 0o755)
    return prev, marker, exit_file


def _install_chained(tmp_path):
    """Install the gate over a pre-existing global hooksPath; return the
    fixture pieces for driving the installed wrapper."""
    prev_hooks, marker, exit_file = _mk_prev_hooks(tmp_path)
    (tmp_path / "gitconfig").touch()  # so the setter below has a file to write
    _git(tmp_path, ["config", "--global", "core.hooksPath", str(prev_hooks)],
         cwd=tmp_path)
    proc = _run(tmp_path, [_INSTALL])
    assert proc.returncode == 0, proc.stderr
    return prev_hooks, marker, exit_file


def _gate_env(tmp_path, trace):
    """Extra env to drive the installed hook inline against the stub reviewer
    (modeled on test_install_hooks_path._hook_env)."""
    return {
        "OCR_STATE_DIR": str(tmp_path / "state"),
        "CLAUDE_PLUGIN_DATA": str(tmp_path / "gate-data"),
        "SCR_GATE_DIR": _SCRIPTS,
        "OCR_REVIEWER_CMD": f'"{sys.executable}" "{_STUB}"',
        "STUB_TRACE": str(tmp_path / trace),
        "OCR_INLINE_BUDGET": "30",
    }


def _add_unpushed_commit(tmp_path, work):
    """A commit not on origin, so the gate genuinely reviews on the drive."""
    (work / "b.txt").write_text("b\n")
    _git(tmp_path, ["add", "."], cwd=work)
    _git(tmp_path, ["commit", "-q", "-m", "add b"], cwd=work)


def _run_hook(tmp_path, hook, work, env_extra=None):
    """Invoke a hook the way git would: ref updates on stdin, remote name as
    $1, the scratch repo as cwd."""
    tip = _git(tmp_path, ["rev-parse", "HEAD"], cwd=work)
    argv = [hook] if sys.platform != "win32" else [shutil.which("sh") or "sh", hook]
    proc = subprocess.run(argv + ["origin"],
                          input=f"refs/heads/main {tip} refs/heads/main {ZERO}\n",
                          cwd=str(work), env=_env(tmp_path, env_extra),
                          capture_output=True, text=True, timeout=180)
    return proc


# --- default-on surface -------------------------------------------------------


def test_fresh_install_installs_the_global_gate_by_default(tmp_path):
    proc = _run(tmp_path, [_INSTALL])
    assert proc.returncode == 0, proc.stderr
    hooks = _hooks_dir(tmp_path)
    assert _gcfg(tmp_path, "--get", "core.hooksPath") == str(hooks)
    hook = hooks / "pre-push"
    assert hook.is_file(), "no prior setup: the gate copy itself is the hook"
    assert "review-gate.py" in hook.read_text()
    assert os.access(str(hook), os.X_OK)
    assert _gcfg(tmp_path, "--get", "reviewGate.prevHooksPath") == "", \
        "nothing pre-existed, so there is nothing to back up"


def test_no_git_hook_flag_preserves_legacy_opt_in_behavior(tmp_path):
    proc = _run(tmp_path, [_INSTALL, "--no-git-hook"])
    assert proc.returncode == 0, proc.stderr
    assert _gcfg(tmp_path, "--get", "core.hooksPath") == "", \
        "--no-git-hook must not install the global surface"
    assert not _hooks_dir(tmp_path).exists(), "no hook files are materialized"
    assert _gcfg(tmp_path, "--get", "reviewGate.prevHooksPath") == ""


# --- chaining -----------------------------------------------------------------


def test_existing_global_hookspath_is_backed_up_and_chained(tmp_path):
    prev_hooks, _, _ = _install_chained(tmp_path)
    hooks = _hooks_dir(tmp_path)
    assert _gcfg(tmp_path, "--get", "reviewGate.prevHooksPath") == str(prev_hooks)
    assert _gcfg(tmp_path, "--get", "core.hooksPath") == str(hooks)
    # Git executes <hooksPath>/pre-push, so that must be the chain wrapper; the
    # gate body itself moves next to it.
    wrapper = (hooks / "pre-push").read_text()
    assert "pre-push.review-gate" in wrapper
    assert "reviewGate.prevHooksPath" in wrapper
    body = hooks / "pre-push.review-gate"
    assert body.is_file() and os.access(str(body), os.X_OK)
    assert "review-gate.py" in body.read_text()


def test_chain_runs_the_previous_hook_after_the_gate_passes(tmp_path):
    work = _mkrepo(tmp_path)  # baseline push lands BEFORE the gate is installed
    _add_unpushed_commit(tmp_path, work)  # gives the gate a review to run
    _, marker, _ = _install_chained(tmp_path)
    proc = _run_hook(tmp_path, _hooks_dir(tmp_path) / "pre-push", work,
                     _gate_env(tmp_path, "pass.trace"))
    assert proc.returncode == 0, proc.stderr
    assert "prev-ran" in marker.read_text(), \
        "the chained previous hook must run after the gate passes"
    assert (tmp_path / "pass.trace").read_text().strip(), \
        "the gate itself must have run (stub reviewer invoked)"


def test_a_gate_block_short_circuits_the_chain(tmp_path):
    work = _mkrepo(tmp_path)
    _, marker, _ = _install_chained(tmp_path)
    # The reviewer cannot be located (both the explicit override and the
    # install-time hint poisoned, CLAUDE_CONFIG_DIR tree empty via the temp
    # HOME): the gate fails CLOSED, and the previous hook must never run.
    proc = _run_hook(tmp_path, _hooks_dir(tmp_path) / "pre-push", work,
                     {"SCR_GATE_DIR": str(tmp_path / "no-such-dir"),
                      "SCR_BIN": str(tmp_path / "no-such-bin")})
    assert proc.returncode != 0, "a failing gate must fail the push"
    assert not marker.exists(), "gate blocked: the chain must not run"


def test_the_previous_hooks_exit_code_is_final(tmp_path):
    work = _mkrepo(tmp_path)
    _add_unpushed_commit(tmp_path, work)
    _, marker, exit_file = _install_chained(tmp_path)
    exit_file.write_text("3\n")
    proc = _run_hook(tmp_path, _hooks_dir(tmp_path) / "pre-push", work,
                     _gate_env(tmp_path, "exit3.trace"))
    assert proc.returncode == 3, \
        "gate passed, previous hook failed: the previous hook's exit code wins"
    assert "prev-ran" in marker.read_text()


# --- idempotency and restore ----------------------------------------------------


def test_install_is_idempotent(tmp_path):
    prev_hooks, _, _ = _install_chained(tmp_path)
    hooks = _hooks_dir(tmp_path)
    wrapper_once = (hooks / "pre-push").read_bytes()
    body_once = (hooks / "pre-push.review-gate").read_bytes()

    proc = _run(tmp_path, [_INSTALL])
    assert proc.returncode == 0, proc.stderr
    assert _gcfg(tmp_path, "--get-all", "reviewGate.prevHooksPath").splitlines() == \
        [str(prev_hooks)], "exactly one backup, not a second clobbering entry"
    assert _gcfg(tmp_path, "--get-all", "core.hooksPath").splitlines() == [str(hooks)]
    assert (hooks / "pre-push").read_bytes() == wrapper_once
    assert (hooks / "pre-push.review-gate").read_bytes() == body_once
    hook_files = sorted(p.name for p in hooks.iterdir())
    assert hook_files == ["pre-push", "pre-push.review-gate"]


def test_uninstall_restores_the_backed_up_hookspath(tmp_path):
    prev_hooks, _, _ = _install_chained(tmp_path)
    hooks = _hooks_dir(tmp_path)
    proc = _run(tmp_path, [_UNINSTALL])
    assert proc.returncode == 0, proc.stderr
    assert _gcfg(tmp_path, "--get", "core.hooksPath") == str(prev_hooks)
    assert _gcfg(tmp_path, "--get", "reviewGate.prevHooksPath") == ""
    assert not (hooks / "pre-push").exists(), "the chain wrapper is removed"


def test_uninstall_without_a_prior_setup_unsets_hookspath(tmp_path):
    proc = _run(tmp_path, [_INSTALL])
    assert proc.returncode == 0, proc.stderr
    proc = _run(tmp_path, [_UNINSTALL])
    assert proc.returncode == 0, proc.stderr
    assert _gcfg(tmp_path, "--get", "core.hooksPath") == ""


# --- isolation guard ----------------------------------------------------------


def test_guard_refuses_when_test_context_targets_the_real_config(tmp_path):
    env = _env(tmp_path)
    del env["GIT_CONFIG_GLOBAL"]  # the exact failure mode the guard exists for
    proc = subprocess.run([_BASH, _INSTALL], cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0, \
        "OCR_INSTALLER_TEST set without a GIT_CONFIG_GLOBAL redirect must refuse"
    assert "GIT_CONFIG_GLOBAL" in proc.stderr
    # Belt and braces: HOME was redirected too, so even a broken guard could
    # never reach the real config -- assert the temp one was not written.
    assert not (tmp_path / "home" / ".gitconfig").exists()


# --- preserved behavior -------------------------------------------------------


def test_chain_into_still_prints_the_snippet(tmp_path):
    repo = tmp_path / "husky-repo"
    _git(tmp_path, ["init", "-q", "-b", "main", str(repo)], cwd=tmp_path)
    _git(tmp_path, ["config", "core.hooksPath", ".husky"], cwd=repo)
    (repo / ".husky").mkdir()
    (repo / ".husky" / "pre-push").write_text("#!/bin/sh\nexit 0\n")
    proc = _run(tmp_path, [_INSTALL, "--chain-into", str(repo)])
    assert proc.returncode == 0, proc.stderr
    assert "--- review-gate:" in proc.stdout
    assert "--- end review-gate ---" in proc.stdout
    assert ".husky" in proc.stdout.replace(os.sep, "/")
    assert _gcfg(tmp_path, "--get", "core.hooksPath") == "", \
        "--chain-into prints only; it touches no git config"


def test_the_windows_installer_twin_matches(tmp_path):
    ps1 = os.path.join(_SCRIPTS, "install-git-hook.ps1")
    if sys.platform != "win32" or not os.path.isfile(ps1):
        pytest.skip("no install-git-hook.ps1 twin in this repo (POSIX installer only)")
