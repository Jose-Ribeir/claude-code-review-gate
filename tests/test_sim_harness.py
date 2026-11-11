"""Unit tests for tests/sim_harness.py (docs-gate 07).

Everything here is model-free: scratch creation and remote isolation, the
installer's global-side-effect redirect, push-env hygiene, the commit-op
applier, and the async two-step push driver -- the last exercised against a
FAKE pre-push hook script that plays the gate's "is still running" answer and
then its block/allow verdicts, so no `kimchi` worker ever runs.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import sim_harness as sim  # noqa: E402


def _git(args, cwd, check=True):
    return subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True,
                          check=check).stdout.strip()


def _toy_repo(tmp_path, remote_url="git@example.com:some/real-remote.git"):
    """A target repo whose origin is a REAL-looking ssh remote -- exactly what
    the harness must rewrite and fence off."""
    target = tmp_path / "target"
    target.mkdir()
    _git(["init", "-q", "-b", "main"], target)
    for k, v in (("user.email", "t@t.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(["config", k, v], target)
    (target / "app.py").write_text("print('v1')\n")
    _git(["add", "."], target)
    _git(["commit", "-q", "-m", "base"], target)
    _git(["remote", "add", "origin", remote_url], target)
    return target


# --------------------------------------------------------------------------
# Scratch creation + remote isolation
# --------------------------------------------------------------------------

def test_make_scratch_rewrites_every_remote_to_a_local_bare_origin(tmp_path):
    target = _toy_repo(tmp_path)
    scratch = sim.make_scratch(target, tmp_path / "scratch")
    assert (scratch.origin / "HEAD").exists(), "the fake origin is a bare repo"
    remotes = _git(["remote", "-v"], scratch.work)
    assert "git@example.com" not in remotes
    assert str(scratch.origin) in remotes
    # The baseline commit exists on the fake origin.
    assert _git(["rev-parse", "HEAD"], scratch.work) == \
        _git(["rev-parse", f"origin/{scratch.branch}"], scratch.work)
    sim.assert_remote_isolation(scratch)  # raises on any leak


def test_isolation_assertion_rejects_a_remote_pointing_outside(tmp_path):
    target = _toy_repo(tmp_path)
    scratch = sim.make_scratch(target, tmp_path / "scratch")
    _git(["remote", "add", "evil", "git@example.com:some/real-remote.git"], scratch.work)
    with pytest.raises(sim.SimError, match="not a local path"):
        sim.assert_remote_isolation(scratch)
    _git(["remote", "remove", "evil"], scratch.work)
    _git(["remote", "add", "local-but-outside", str(tmp_path)], scratch.work)
    with pytest.raises(sim.SimError, match="escapes the scratch root"):
        sim.assert_remote_isolation(scratch)


def test_isolation_assertion_applies_url_rewrites(tmp_path):
    """A url.*.insteadOf in the (scratch-redirected) config cannot launder a
    local-looking remote into a network URL: the effective URL is checked."""
    target = _toy_repo(tmp_path)
    scratch = sim.make_scratch(target, tmp_path / "scratch")
    env = sim.push_env(scratch, _SCRIPTS_DIR)
    scratch.gitconfig.write_text(
        f"[url \"git@example.com:laundered.git\"]\n\tinsteadOf = {scratch.origin}\n")
    with pytest.raises(sim.SimError, match="not a local path"):
        sim.assert_remote_isolation(scratch, env=env)


def test_seed_files_are_committed_into_the_baseline(tmp_path):
    target = _toy_repo(tmp_path)
    scratch = sim.make_scratch(target, tmp_path / "scratch",
                               seed_files={"docs/adr/0085-seed.md": "# seeded\n"})
    assert (scratch.work / "docs/adr/0085-seed.md").is_file()
    assert "seeded" in _git(["show", "origin/main:docs/adr/0085-seed.md"], scratch.work)
    # The seed exists only in the scratch: the original knows no such commit.
    assert _git(["log", "--oneline"], target) != _git(["log", "--oneline"], scratch.work)


# --------------------------------------------------------------------------
# Installer redirect
# --------------------------------------------------------------------------

def test_install_gate_hook_redirects_every_global_side_effect(tmp_path):
    target = _toy_repo(tmp_path)
    scratch = sim.make_scratch(target, tmp_path / "scratch")
    hook = sim.install_gate_hook(scratch, _SCRIPTS_DIR)
    assert hook.is_file() and os.access(hook, os.X_OK)
    hp = _git(["config", "--file", str(scratch.gitconfig), "--get", "core.hooksPath"],
              scratch.root)
    assert Path(hp).resolve() == scratch.hooks.resolve()


def test_push_env_scrubs_stub_and_session_state(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_STUB_WORKER", "1")
    monkeypatch.setenv("OCR_REVIEWER_CMD", "x")
    monkeypatch.setenv("GIT_TRACE2_EVENT", "/tmp/trace")
    target = _toy_repo(tmp_path)
    scratch = sim.make_scratch(target, tmp_path / "scratch")
    env = sim.push_env(scratch, _SCRIPTS_DIR)
    assert not any(k.startswith(("OCR_STUB", "GIT_TRACE")) for k in env)
    assert "OCR_REVIEWER_CMD" not in env
    assert env["GIT_CONFIG_GLOBAL"] == str(scratch.gitconfig)
    assert env["SCR_GATE_DIR"] == str(_SCRIPTS_DIR)


# --------------------------------------------------------------------------
# Commit-op application
# --------------------------------------------------------------------------

def test_apply_commit_write_delete_and_replace(tmp_path):
    target = _toy_repo(tmp_path)
    scratch = sim.make_scratch(target, tmp_path / "scratch")
    sim.apply_commit(scratch.work, sim.CommitSpec(
        message="c",
        write={"new.py": "x = 1\n", "nested/deep.py": "y = 2\n"},
        replace=[("app.py", "print('v1')", "print('v2')")],
    ))
    assert "print('v2')" in (scratch.work / "app.py").read_text()
    assert (scratch.work / "nested/deep.py").is_file()
    sim.apply_commit(scratch.work, sim.CommitSpec(message="d", delete=["new.py"]))
    assert not (scratch.work / "new.py").exists()
    with pytest.raises(sim.SimError, match="exactly 1"):
        sim.apply_commit(scratch.work, sim.CommitSpec(
            message="bad", replace=[("app.py", "no such anchor", "z")]))
    with pytest.raises(sim.SimError, match="missing"):
        sim.apply_commit(scratch.work, sim.CommitSpec(message="bad", delete=["gone.py"]))


# --------------------------------------------------------------------------
# The async two-step driver (against a FAKE gate hook -- no worker)
# --------------------------------------------------------------------------

_FAKE_GATE_STILL_RUNNING_THEN_ALLOW = """#!/usr/bin/env bash
# Plays the gate's async answer: deny twice "still running", then allow.
cat >/dev/null   # consume the ref updates git feeds a pre-push hook
N_FILE="$FAKE_GATE_STATE/n"
N=$(cat "$N_FILE" 2>/dev/null || echo 0)
N=$((N + 1)); echo "$N" > "$N_FILE"
if [ "$N" -lt 3 ]; then
  echo "review-gate: the review of main (abc) is still running." >&2
  echo "To get the verdict, run the push again." >&2
  exit 1
fi
echo "$N" > "$FAKE_GATE_STATE/allowed"
exit 0
"""

_FAKE_GATE_BLOCK = """#!/usr/bin/env bash
cat >/dev/null
echo "review-gate blocked this commit (high-severity issues):" >&2
echo "  - contradicts recorded decision ADR-0085" >&2
exit 1
"""


def _fake_gate_env(scratch, script, tmp_path):
    hookdir = scratch.hooks
    hookdir.mkdir(parents=True, exist_ok=True)
    hook = hookdir / "pre-push"
    hook.write_text(script)
    hook.chmod(0o755)
    state = tmp_path / "fake-gate-state"
    state.mkdir(exist_ok=True)
    env = sim.push_env(scratch, _SCRIPTS_DIR)
    cfg = f"[core]\n\thooksPath = {hookdir}\n"
    scratch.gitconfig.write_text(cfg)
    env["FAKE_GATE_STATE"] = str(state)
    return env, state


_SCRIPTS_DIR = str(Path(_HERE).parent / "scripts")


def test_drive_push_joins_the_verdict_on_re_push(tmp_path):
    target = _toy_repo(tmp_path)
    scratch = sim.make_scratch(target, tmp_path / "scratch")
    env, state = _fake_gate_env(scratch, _FAKE_GATE_STILL_RUNNING_THEN_ALLOW, tmp_path)
    sim.apply_commit(scratch.work, sim.CommitSpec(message="c", write={"f.py": "z = 1\n"}))
    outcome, transcript, attempts = sim.drive_push(scratch, env, poll_s=0.05)
    assert outcome == "pass"
    assert len(attempts) == 3, "two still-running denials, then the joined verdict"
    assert "still running" in transcript.lower()
    assert state.joinpath("allowed").read_text().strip() == "3"
    # The joined pass actually landed the objects on the fake origin.
    assert _git(["rev-parse", "HEAD"], scratch.work) == \
        _git(["rev-parse", "origin/main"], scratch.work)


def test_drive_push_block_is_terminal_not_retried(tmp_path):
    target = _toy_repo(tmp_path)
    scratch = sim.make_scratch(target, tmp_path / "scratch")
    env, _ = _fake_gate_env(scratch, _FAKE_GATE_BLOCK, tmp_path)
    sim.apply_commit(scratch.work, sim.CommitSpec(message="c", write={"f.py": "z = 1\n"}))
    outcome, transcript, attempts = sim.drive_push(scratch, env, poll_s=0.05)
    assert outcome == "block" and len(attempts) == 1
    assert sim.BLOCK_BANNER in transcript
    assert _git(["rev-parse", "HEAD"], scratch.work) != \
        _git(["rev-parse", "origin/main"], scratch.work)


def test_drive_push_times_out_when_the_verdict_never_settles(tmp_path):
    always_running = _FAKE_GATE_STILL_RUNNING_THEN_ALLOW.replace("-lt 3", "-lt 99999")
    target = _toy_repo(tmp_path)
    scratch = sim.make_scratch(target, tmp_path / "scratch")
    env, _ = _fake_gate_env(scratch, always_running, tmp_path)
    sim.apply_commit(scratch.work, sim.CommitSpec(message="c", write={"f.py": "z = 1\n"}))
    t0 = time.monotonic()
    with pytest.raises(sim.SimError, match="never reached a verdict"):
        sim.drive_push(scratch, env, timeout_s=0.2, poll_s=0.05)
    assert time.monotonic() - t0 < 20


# --------------------------------------------------------------------------
# run_scenario assertions and the reset contract
# --------------------------------------------------------------------------

def test_run_scenario_asserts_block_message_content(tmp_path):
    target = _toy_repo(tmp_path)
    scratch = sim.make_scratch(target, tmp_path / "scratch")
    env, _ = _fake_gate_env(scratch, _FAKE_GATE_BLOCK, tmp_path)
    present = sim.Scenario(name="s", description="d", expect="block",
                           commits=[sim.CommitSpec(message="c", write={"f.py": "z = 1\n"})],
                           expect_message=["ADR-0085"])
    r = sim.run_scenario(scratch, env, present)
    assert r.ok and r.outcome == "block"
    missing = sim.Scenario(name="s", description="d", expect="block",
                           commits=[sim.CommitSpec(message="c", write={"f.py": "z = 1\n"})],
                           expect_message=["a string the denial never said"])
    r = sim.run_scenario(scratch, env, missing)
    assert not r.ok and any("missing required text" in f for f in r.failures)
    wrong = sim.Scenario(name="s", description="d", expect="pass",
                         commits=[sim.CommitSpec(message="c", write={"f.py": "z = 1\n"})])
    r = sim.run_scenario(scratch, env, wrong)
    assert not r.ok and any("expected 'pass', got 'block'" in f for f in r.failures)


def test_reset_to_origin_discards_a_blocked_commit_but_keeps_gitignored_cache(tmp_path):
    target = _toy_repo(tmp_path)
    scratch = sim.make_scratch(target, tmp_path / "scratch")
    cache = scratch.work / ".ocr/docs-summary.md"
    cache.parent.mkdir(parents=True)
    cache.write_text("cached\n")
    (scratch.work / ".git/info").mkdir(exist_ok=True)
    with (scratch.work / ".git/info/exclude").open("a") as fh:
        fh.write(".ocr/\n")
    tip0 = _git(["rev-parse", "origin/main"], scratch.work)
    sim.apply_commit(scratch.work, sim.CommitSpec(message="c", write={"f.py": "z = 1\n"}))
    sim.reset_to_origin(scratch)
    assert _git(["rev-parse", "HEAD"], scratch.work) == tip0
    assert not (scratch.work / "f.py").exists()
    assert cache.is_file(), "a gitignored docs summary must survive the reset"


# --------------------------------------------------------------------------
# Original-repo integrity
# --------------------------------------------------------------------------

def test_repo_snapshot_comparison_catches_any_mutation(tmp_path):
    target = _toy_repo(tmp_path)
    before = sim.repo_snapshot(target)
    sim.repo_snapshot(target)  # reads alone compare equal
    sim.assert_repo_unchanged(before, sim.repo_snapshot(target))
    (target / "app.py").write_text("print('dirty')\n")
    with pytest.raises(sim.SimError, match="working-tree status changed"):
        sim.assert_repo_unchanged(before, sim.repo_snapshot(target))


_FAKE_REVIEW_GATE_PY = """#!/usr/bin/env python3
# Stand-in for review-gate.py: the async gate's two answers, no worker.
import os, sys
state = os.path.join(os.environ["GIT_CONFIG_GLOBAL"] + ".gate", "n")
os.makedirs(os.path.dirname(state), exist_ok=True)
try:
    n = int(open(state).read())
except (OSError, ValueError):
    n = 0
n += 1
open(state, "w").write(str(n))
if n < 2:
    sys.stderr.write(
        "review-gate: the review of main (abc) is still running. The push was NOT "
        "executed.\\nTo get the verdict, run the push again.\\n")
    sys.exit(1)
sys.exit(0)
"""


def _fake_gate_scripts(tmp_path):
    """A fake plugin dir: the repo's REAL installer + pre-push template, but a
    stub review-gate.py so the whole main() run stays offline and deterministic."""
    fake = tmp_path / "fake-gate" / "scripts"
    fake.mkdir(parents=True)
    real = Path(_SCRIPTS_DIR)
    for name in ("install-git-hook.sh", "pre-push"):
        (fake / name).write_text((real / name).read_text())
    (fake / "review-gate.py").write_text(_FAKE_REVIEW_GATE_PY)
    # The installer reads its plugin version from ../.claude-plugin/plugin.json.
    plugin_json = real.parent / ".claude-plugin" / "plugin.json"
    dest = fake.parent / ".claude-plugin"
    dest.mkdir()
    (dest / "plugin.json").write_text(plugin_json.read_text())
    return str(fake)


def test_main_end_to_end_with_a_fake_gate(tmp_path, capsys):
    """main() drives the real installer and the real push plumbing against a
    stub review-gate.py: the two-step join lands the push, the report is
    written, and the target repo is proven untouched."""
    target = _toy_repo(tmp_path)
    sc_mod = tmp_path / "scen.py"
    sc_mod.write_text(
        "from sim_harness import CommitSpec, Scenario\n"
        "SCENARIOS = [Scenario(name='ok', description='d', expect='pass',\n"
        "  commits=[CommitSpec(message='c', write={'f.py': 'z = 1\\n'})])]\n")
    root = tmp_path / "scratch"
    fake = _fake_gate_scripts(tmp_path)
    rc = sim.main(["--repo", str(target), "--scenarios", str(sc_mod),
                   "--workdir", str(root), "--gate-scripts", fake,
                   "--inline-budget", "0"])
    assert rc == 0, capsys.readouterr().out
    data = __import__("json").loads((root / "results.json").read_text())
    assert data["target_untouched"] is True
    (res,) = data["results"]
    assert res["ok"] and res["outcome"] == "pass" and res["pushes"] == 2
    # The gate hook the run used is the one the installer materialized.
    hook = (root / "hooks" / "pre-push").read_text()
    assert "review-gate.py" in hook
