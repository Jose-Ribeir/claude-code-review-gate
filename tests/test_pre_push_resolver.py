"""The shell side of the fail-closed contract.

test_review_gate.py covers the Python thoroughly, but the mechanism this whole
release exists to fix lives in shell: scripts/pre-push resolving the reviewer at
runtime instead of trusting a path baked in at install time. A regression there
reproduces exactly the bug being closed -- an upgrade silently turning a working
gate into an absent one -- and it would be invisible to every Python test.

So these drive the real script through bash and assert on exit codes: 0 means
the push proceeds, 1 means blocked.
"""
import os
import shutil
import subprocess

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PRE_PUSH = os.path.join(_REPO, "scripts", "pre-push")
_BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(_BASH is None, reason="bash not available")


def _stub_plugin(tmp_path, name="plug"):
    """A plugin whose reviewer always passes, so exit code reflects resolution."""
    scripts = tmp_path / name / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "review-gate.py").write_text("import sys; sys.exit(0)\n", encoding="utf-8")
    return scripts


def _run(tmp_path, env_extra, cwd=None):
    env = dict(os.environ)
    # Neutralize the developer's real environment: without this the test would
    # silently pass by finding the actual installed plugin.
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "empty-config")
    env.pop("SCR_GATE_DIR", None)
    env.pop("SCR_BIN", None)
    env.pop("OCR_FAIL_OPEN", None)
    env.pop("OCR_IN_REVIEW", None)
    env.update(env_extra)
    return subprocess.run(
        [_BASH, _PRE_PUSH, "origin", "git@example.com:x/y.git"],
        cwd=str(cwd or tmp_path), env=env,
        input="", capture_output=True, text=True, timeout=120,
    )


def test_blocks_when_the_reviewer_cannot_be_found(tmp_path):
    # The regression that matters: a stale baked path must NOT mean "skip the
    # review and allow the push". Before 0.3.0 this exited 0.
    r = _run(tmp_path, {"SCR_BIN": str(tmp_path / "gone" / "bin")})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "BLOCKED" in r.stderr


def test_fail_open_escape_hatch_still_works(tmp_path):
    r = _run(tmp_path, {"SCR_BIN": str(tmp_path / "gone" / "bin"), "OCR_FAIL_OPEN": "1"})
    assert r.returncode == 0, r.stdout + r.stderr


def test_legacy_bin_hint_falls_back_to_the_scripts_sibling(tmp_path):
    # A hook installed before 0.3.0 has an absolute .../bin path baked in. That
    # directory no longer exists, so the resolver must find its scripts/ sibling
    # rather than give up -- otherwise upgrading silently disables the gate for
    # everyone who ran install-git-hook.sh.
    scripts = _stub_plugin(tmp_path)
    legacy_hint = str(scripts.parent / "bin")
    assert not os.path.exists(legacy_hint)
    r = _run(tmp_path, {"SCR_BIN": legacy_hint})
    assert r.returncode == 0, r.stdout + r.stderr


def test_explicit_override_wins(tmp_path):
    scripts = _stub_plugin(tmp_path)
    r = _run(tmp_path, {"SCR_GATE_DIR": str(scripts), "SCR_BIN": str(tmp_path / "gone")})
    assert r.returncode == 0, r.stdout + r.stderr


def test_pointer_file_is_used_when_no_hint_is_baked_in(tmp_path):
    scripts = _stub_plugin(tmp_path)
    cfg = tmp_path / "cfg"
    ptr_dir = cfg / "plugins" / "data" / "review-gate-someid"
    ptr_dir.mkdir(parents=True)
    (ptr_dir / "gate-dir").write_text(str(scripts), encoding="utf-8")
    env = {"CLAUDE_CONFIG_DIR": str(cfg)}
    r = _run(tmp_path, env)
    assert r.returncode == 0, r.stdout + r.stderr


def test_writer_and_reader_agree_on_the_pointer_location(tmp_path):
    """The half a unit test cannot see: _write_gate_pointer chooses a path, and
    pre-push globs for it. If either side moves, the git hook stops self-healing
    across upgrades and nothing else notices."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "rg", os.path.join(_REPO, "scripts", "review-gate.py")
    )
    rg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rg)

    cfg = tmp_path / "cfg"
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(cfg)
    os.environ.pop("CLAUDE_PLUGIN_DATA", None)
    try:
        rg._write_gate_pointer()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old

    # Exactly the glob scripts/pre-push uses: $cfg/plugins/data/*/gate-dir
    import glob
    found = glob.glob(str(cfg / "plugins" / "data" / "*" / "gate-dir"))
    assert found, "writer put the pointer where pre-push's glob will not find it"
    assert os.path.isfile(os.path.join(open(found[0], encoding="utf-8").read().strip(),
                                       "review-gate.py"))


# --- install-git-hook.sh --chain-into -----------------------------------------
# A repo-local core.hooksPath is resolved by git BEFORE the global one, so a
# repo managing its own hooks silently drops this gate: pushes from a plain
# terminal there are not gated at all. --chain-into prints the cure. It only
# prints -- the hook belongs to that repo and is under its version control.

_INSTALLER = os.path.join(_REPO, "scripts", "install-git-hook.sh")


def _chain_into(path):
    return subprocess.run(
        [_BASH, _INSTALLER, "--chain-into", str(path)],
        capture_output=True, text=True, timeout=60,
    )


def _repo_with_local_hookspath(tmp_path, hookspath, hook_body="#!/bin/sh\nexit 0\n"):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, timeout=60)
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.hooksPath", str(hookspath)],
        capture_output=True, timeout=60,
    )
    d = hookspath if os.path.isabs(str(hookspath)) else repo / hookspath
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(str(d), "pre-push"), "w", encoding="utf-8") as fh:
        fh.write(hook_body)
    return repo


def test_chain_into_reports_the_right_hook_for_an_absolute_hookspath(tmp_path):
    # The bug this caught in review: core.hooksPath may be ABSOLUTE, and
    # joining it onto the repo path produced nonsense like /repo/C:/repo/hooks.
    hooks = tmp_path / "abs-hooks"
    repo = _repo_with_local_hookspath(tmp_path, hooks)
    r = _chain_into(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert os.path.join(str(hooks), "pre-push") in r.stdout.replace("/", os.sep)


def test_chain_into_reports_the_right_hook_for_a_relative_hookspath(tmp_path):
    repo = _repo_with_local_hookspath(tmp_path, "myhooks")
    r = _chain_into(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "myhooks" in r.stdout and "pre-push" in r.stdout


def test_chain_into_says_nothing_to_do_when_already_chained(tmp_path):
    repo = _repo_with_local_hookspath(
        tmp_path, "myhooks",
        hook_body='#!/bin/sh\npython "$D/review-gate.py" --mode git\n',
    )
    r = _chain_into(repo)
    assert r.returncode == 0
    assert "Already chained" in r.stdout


def test_chain_into_says_nothing_to_do_without_a_local_hookspath(tmp_path):
    repo = tmp_path / "plain"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, timeout=60)
    r = _chain_into(repo)
    assert r.returncode == 0
    assert "does not set a repo-local core.hooksPath" in r.stdout


def test_chain_into_rejects_a_path_that_is_not_a_directory(tmp_path):
    r = _chain_into(tmp_path / "nope")
    assert r.returncode == 2
    assert "usage:" in r.stderr
