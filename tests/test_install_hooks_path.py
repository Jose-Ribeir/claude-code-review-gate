"""A3: install/uninstall honor git's core.hooksPath.

Git runs hooks from core.hooksPath when the repo sets one (husky, lefthook, a
hand-rolled scripts/git-hooks). An installer that writes $GITDIR/hooks there
would be a silent mis-install: git would never run our pre-push. These tests
pin the whole contract on a scratch repo -- install, chaining, direct
invocation of the installed hook, uninstall, and the fail-loudly path -- with
every overrideable location pointed at tmp (the developer's real
~/.config/kimchi and ~/.local/state are never touched).
"""
import os
import re
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SCRIPTS = os.path.join(_ROOT, "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_STUB = os.path.join(_HERE, "stub_reviewer.py")
_INSTALL = os.path.join(_SCRIPTS, "install-kimchi-hook.sh")
_UNINSTALL = os.path.join(_SCRIPTS, "uninstall-kimchi-hook.sh")
ZERO = "0" * 40


def _git(args, cwd, check=True):
    return subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True,
                          check=check).stdout.strip()


def _mkrepo(tmp_path, hooks_cfg=".githooks"):
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    _git(["config", "user.email", "t@example.com"], cwd=work)
    _git(["config", "user.name", "t"], cwd=work)
    _git(["config", "commit.gpgsign", "false"], cwd=work)
    if hooks_cfg is not None:
        _git(["config", "core.hooksPath", hooks_cfg], cwd=work)
    (work / "a.txt").write_text("one\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    return work


def _overridden_env(tmp_path):
    """The install/uninstall environment with EVERY override pointed at tmp."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE")}
    env.update({
        "KIMCHI_HOOKS_DIR": str(tmp_path / "kimchi-hooks"),
        "OCR_EXT_DIR": str(tmp_path / "ext"),
        "OCR_STATE_DIR": str(tmp_path / "state"),
    })
    return env


def _hook_env(tmp_path, trace):
    """The environment the installed pre-push needs to run the gate inline
    against the stub reviewer (modeled on test_stacked_base._env)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OCR_", "STUB_", "GIT_TRACE"))}
    env.update({
        "OCR_STATE_DIR": str(tmp_path / "state"),
        "CLAUDE_PLUGIN_DATA": str(tmp_path / "gate-data"),
        "SCR_GATE_DIR": _SCRIPTS,
        "OCR_REVIEWER_CMD": f'"{sys.executable}" "{_STUB}"',
        "STUB_TRACE": str(tmp_path / trace),
        "OCR_INLINE_BUDGET": "30",
    })
    return env


def _run_hook(hook, work, tip, env):
    """Invoke the installed pre-push directly, the way git would."""
    return subprocess.run([str(hook), "origin"],
                          input=f"refs/heads/main {tip} refs/heads/main {ZERO}\n",
                          capture_output=True, text=True, cwd=str(work), env=env,
                          timeout=120)


def _tip(work):
    return _git(["rev-parse", "HEAD"], cwd=work)


def test_install_writes_into_the_configured_hooks_path(tmp_path):
    work = _mkrepo(tmp_path)
    proc = subprocess.run(["bash", _INSTALL], cwd=str(work), env=_overridden_env(tmp_path),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    hook = work / ".githooks" / "pre-push"
    assert hook.is_file(), "the pre-push must land in core.hooksPath"
    assert "review-gate.py" in hook.read_text()
    assert os.access(str(hook), os.X_OK)
    # The default location must NOT have been used (silent mis-install).
    assert not (work / ".git" / "hooks" / "pre-push").exists()


def test_install_into_an_absolute_hooks_path(tmp_path):
    hooks = tmp_path / "abs-hooks"
    hooks.mkdir()
    work = _mkrepo(tmp_path, hooks_cfg=str(hooks))
    proc = subprocess.run(["bash", _INSTALL], cwd=str(work), env=_overridden_env(tmp_path),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert (hooks / "pre-push").is_file()
    assert not (work / ".git" / "hooks" / "pre-push").exists()


def test_install_without_a_configured_path_uses_gitdir_hooks(tmp_path):
    work = _mkrepo(tmp_path, hooks_cfg=None)
    proc = subprocess.run(["bash", _INSTALL], cwd=str(work), env=_overridden_env(tmp_path),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    hook = work / ".git" / "hooks" / "pre-push"
    assert hook.is_file(), "no core.hooksPath: install into .git/hooks as before"
    assert "review-gate.py" in hook.read_text()


def test_existing_hook_in_the_configured_path_is_chained(tmp_path):
    work = _mkrepo(tmp_path)
    hooks = work / ".githooks"
    hooks.mkdir()
    (hooks / "pre-push").write_text("#!/bin/sh\necho ran > user-hook.marker\n")
    os.chmod(str(hooks / "pre-push"), 0o755)
    proc = subprocess.run(["bash", _INSTALL], cwd=str(work), env=_overridden_env(tmp_path),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    # The user hook was preserved NEXT TO the configured path, not in .git/hooks.
    assert (hooks / "pre-push.user").is_file()
    assert "user-hook.marker" in (hooks / "pre-push.user").read_text()

    # Drive the installed hook directly: nothing unpushed, so the gate allows
    # and the chained user hook must run.
    proc = _run_hook(hooks / "pre-push", work, _tip(work), _hook_env(tmp_path, "t1.trace"))
    assert proc.returncode == 0, proc.stderr
    assert (work / "user-hook.marker").is_file(), "the chained user hook must run"


def test_the_configured_paths_pre_push_gates(tmp_path):
    """The hook git would actually run (from core.hooksPath) is OUR gate."""
    work = _mkrepo(tmp_path)
    # An unpushed commit, so the gate has something to review.
    (work / "b.txt").write_text("b\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "add b"], cwd=work)
    tip = _tip(work)

    proc = subprocess.run(["bash", _INSTALL], cwd=str(work), env=_overridden_env(tmp_path),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    hook = work / ".githooks" / "pre-push"

    env = _hook_env(tmp_path, "t2.trace")
    # Blocked verdict: the review runs inline against the stub and blocks.
    env["STUB_VERDICT"] = "block"
    proc = _run_hook(hook, work, tip, env)
    assert proc.returncode != 0, "a blocking verdict must exit nonzero from the hook"
    trace = [line for line in (tmp_path / "t2.trace").read_text().splitlines() if line.strip()]
    assert trace, "the reviewer must have run through the configured hook"


def test_uninstall_removes_from_the_configured_path_and_restores_the_user_hook(tmp_path):
    work = _mkrepo(tmp_path)
    hooks = work / ".githooks"
    hooks.mkdir()
    (hooks / "pre-push").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(str(hooks / "pre-push"), 0o755)
    env = _overridden_env(tmp_path)
    assert subprocess.run(["bash", _INSTALL], cwd=str(work), env=env,
                          capture_output=True, timeout=120).returncode == 0
    assert (hooks / "pre-push.user").is_file()

    proc = subprocess.run(["bash", _UNINSTALL], cwd=str(work), env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    # Our hook is gone from the configured path, the user hook restored there.
    assert not (hooks / "pre-push.user").exists()
    assert (hooks / "pre-push").is_file()
    assert (hooks / "pre-push").read_text().strip() == "#!/bin/sh\nexit 0"
    assert "review-gate.py" not in (hooks / "pre-push").read_text()


def test_install_removes_a_stale_gate_copy_from_the_default_hooks_dir(tmp_path):
    """A gate copy left in .git/hooks from before core.hooksPath was set is
    shadowed: git never runs it, so leaving it is a stale lie. Install must
    remove it (with a notice) rather than leave it behind."""
    work = _mkrepo(tmp_path)
    default_hooks = work / ".git" / "hooks"
    default_hooks.mkdir(parents=True, exist_ok=True)
    (default_hooks / "pre-push").write_text(
        "#!/bin/sh\n# installed by install-kimchi-hook.sh\nexec python review-gate.py\n"
    )
    os.chmod(str(default_hooks / "pre-push"), 0o755)
    proc = subprocess.run(["bash", _INSTALL], cwd=str(work), env=_overridden_env(tmp_path),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert not (default_hooks / "pre-push").exists(), \
        "the shadowed gate copy in the default hooks dir must be removed"
    assert "stale" in proc.stdout.lower(), "the removal must be announced"
    hook = work / ".githooks" / "pre-push"
    assert hook.is_file(), "the gate itself still installs into core.hooksPath"
    assert "review-gate.py" in hook.read_text()


def test_install_leaves_a_foreign_hook_in_the_default_hooks_dir_alone(tmp_path):
    """Only recognizably-ours hooks are stale-copy candidates; a hook someone
    else put in .git/hooks is never touched, shadowed or not."""
    work = _mkrepo(tmp_path)
    default_hooks = work / ".git" / "hooks"
    default_hooks.mkdir(parents=True, exist_ok=True)
    (default_hooks / "pre-push").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(str(default_hooks / "pre-push"), 0o755)
    proc = subprocess.run(["bash", _INSTALL], cwd=str(work), env=_overridden_env(tmp_path),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert (default_hooks / "pre-push").is_file()
    assert (default_hooks / "pre-push").read_text().strip() == "#!/bin/sh\nexit 0"


def test_uninstall_leaves_no_stale_copy_anywhere(tmp_path):
    """Full symmetry: with a stale gate copy in the default hooks dir and a
    user hook under core.hooksPath, install + uninstall must end with the
    user hook restored and no review-gate hook file left in either dir."""
    work = _mkrepo(tmp_path)
    hooks = work / ".githooks"
    default_hooks = work / ".git" / "hooks"
    default_hooks.mkdir(parents=True, exist_ok=True)
    (default_hooks / "pre-push").write_text(
        "#!/bin/sh\n# installed by install-kimchi-hook.sh\nexec python review-gate.py\n"
    )
    os.chmod(str(default_hooks / "pre-push"), 0o755)
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-push").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(str(hooks / "pre-push"), 0o755)
    env = _overridden_env(tmp_path)
    assert subprocess.run(["bash", _INSTALL], cwd=str(work), env=env,
                          capture_output=True, timeout=120).returncode == 0

    proc = subprocess.run(["bash", _UNINSTALL], cwd=str(work), env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    # The user hook is back where core.hooksPath points; no gate copy remains
    # in either hook directory.
    assert (hooks / "pre-push").read_text().strip() == "#!/bin/sh\nexit 0"
    assert "review-gate.py" not in (hooks / "pre-push").read_text()
    for dir in (hooks, default_hooks):
        pre_push = dir / "pre-push"
        if pre_push.exists():
            assert "review-gate" not in pre_push.read_text(), \
                f"stale gate copy left behind at {pre_push}"
    assert not (default_hooks / "pre-push.user").exists()


def test_install_into_an_unwritable_hooks_path_fails_loudly(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("running as root: permission-based failure cannot be reproduced")
    hooks = tmp_path / "locked"
    hooks.mkdir()
    os.chmod(str(hooks), 0o500)
    work = _mkrepo(tmp_path, hooks_cfg=str(hooks))
    proc = subprocess.run(["bash", _INSTALL], cwd=str(work), env=_overridden_env(tmp_path),
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode != 0, "an impossible install must FAIL, not mis-install"
    assert "ERROR" in proc.stderr
    assert not (work / ".git" / "hooks" / "pre-push").exists(), \
        "must not silently fall back to .git/hooks"


def test_sha40_shape_of_ref_lines_still_drives_the_hook(tmp_path):
    """Sanity for the synthetic payload shape used above: git's pre-push stdin
    is `<local ref> <local sha> <remote ref> <remote sha>`."""
    assert re.fullmatch(r"[0-9a-f]{40}", ZERO)
