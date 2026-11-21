"""Unit tests for tests/sim_harness.py (docs-gate 07).

Everything here is model-free: scratch creation and remote isolation, the
installer's global-side-effect redirect, push-env hygiene, the commit-op
applier, and the async two-step push driver -- the last exercised against a
FAKE pre-push hook script that plays the gate's "is still running" answer and
then its block/allow verdicts, so no `kimchi` worker ever runs.
"""
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import sim_harness as sim  # noqa: E402
from conftest import force_rmtree  # noqa: E402
from test_process_groups import _wait_gone  # noqa: E402


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
    # Quoted, backslashes doubled: a bare Windows path is a bad config line.
    origin = str(scratch.origin).replace("\\", "\\\\")
    scratch.gitconfig.write_text(
        f"[url \"git@example.com:laundered.git\"]\n\tinsteadOf = \"{origin}\"\n")
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
    cfg = f"[core]\n\thooksPath = {hookdir.as_posix()}\n"  # a "\" would need escaping
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


def test_drive_push_transport_failure_is_a_harness_error_not_a_block(tmp_path):
    """The 2026-11-11 defect, in miniature: something outside the harness
    deletes the fake origin mid-scenario; the next push dies with git's
    'does not appear to be a git repository' and NOT A WORD from the gate.
    That must surface as a SimError (harness error), never as a gate
    'block' the scenario assertions then chew on."""
    target = _toy_repo(tmp_path)
    scratch = sim.make_scratch(target, tmp_path / "scratch")
    env, _ = _fake_gate_env(scratch, _FAKE_GATE_BLOCK, tmp_path)
    sim.apply_commit(scratch.work, sim.CommitSpec(message="c", write={"f.py": "z = 1\n"}))
    force_rmtree(scratch.origin)  # the deleter: outside the harness
    with pytest.raises(sim.SimError, match="not a gate verdict"):
        sim.drive_push(scratch, env, poll_s=0.05)
    try:
        sim.drive_push(scratch, env, poll_s=0.05)
    except sim.SimError as exc:
        assert "VANISHED" in str(exc)
        assert str(scratch.origin) in str(exc)
        assert "does not appear to be a git repository" in str(exc)
    else:
        raise AssertionError("expected SimError")


_FAKE_GATE_DELETE_ORIGIN_ON_SECOND_PUSH = """#!/usr/bin/env bash
# Plays the async denial, but on the second invocation something OUTSIDE the
# gate deletes the bare remote -- the observed real-run failure mode.
cat >/dev/null   # consume the ref updates git feeds a pre-push hook
N_FILE="$FAKE_GATE_STATE/n"
N=$(cat "$N_FILE" 2>/dev/null || echo 0)
N=$((N + 1)); echo "$N" > "$N_FILE"
if [ "$N" -eq 2 ]; then
  rm -rf "$FAKE_ORIGIN"
fi
echo "review-gate: the review of main (abc) is still running." >&2
echo "To get the verdict, run the push again." >&2
exit 1
"""


def test_drive_push_detects_the_origin_vanishing_between_retries(tmp_path):
    target = _toy_repo(tmp_path)
    scratch = sim.make_scratch(target, tmp_path / "scratch")
    env, state = _fake_gate_env(
        scratch, _FAKE_GATE_DELETE_ORIGIN_ON_SECOND_PUSH, tmp_path)
    env["FAKE_ORIGIN"] = str(scratch.origin)
    sim.apply_commit(scratch.work, sim.CommitSpec(message="c", write={"f.py": "z = 1\n"}))
    t0 = time.monotonic()
    with pytest.raises(sim.SimError, match="VANISHED mid-scenario"):
        sim.drive_push(scratch, env, timeout_s=30, poll_s=0.05)
    assert time.monotonic() - t0 < 20, "detected at the next poll, not at the deadline"
    assert state.joinpath("n").read_text().strip() == "2", "failed fast on the third attempt"


def test_drive_push_block_without_any_git_fatal_stays_a_verdict(tmp_path):
    """The guard only bites on pushes the gate never answered: gate denials
    (which always name review-gate) still classify as 'block'."""
    target = _toy_repo(tmp_path)
    scratch = sim.make_scratch(target, tmp_path / "scratch")
    env, _ = _fake_gate_env(scratch, _FAKE_GATE_BLOCK, tmp_path)
    sim.apply_commit(scratch.work, sim.CommitSpec(message="c", write={"f.py": "z = 1\n"}))
    outcome, _, attempts = sim.drive_push(scratch, env, poll_s=0.05)
    assert outcome == "block" and len(attempts) == 1
    assert sim.GATE_MARKER_RE.search(attempts[0]["output"])


def test_run_scenario_propagates_transport_failures_as_harness_errors(tmp_path):
    """run_scenario must not swallow a transport failure into an outcome:
    the SimError propagates to main(), which records outcome='error'."""
    target = _toy_repo(tmp_path)
    scratch = sim.make_scratch(target, tmp_path / "scratch")
    env, _ = _fake_gate_env(scratch, _FAKE_GATE_BLOCK, tmp_path)
    force_rmtree(scratch.origin)
    scenario = sim.Scenario(name="s", description="d", expect="block",
                            commits=[sim.CommitSpec(message="c", write={"f.py": "z = 1\n"})])
    with pytest.raises(sim.SimError, match="not a gate verdict"):
        sim.run_scenario(scratch, env, scenario)


def test_settle_deadline_default_spans_a_real_multi_phase_review():
    """A real review = inline docs-summary regeneration + the review itself
    (measured ~1100 s combined on kimchi-studio, docs-gate 07). The default
    deadline must comfortably span that, or drive_push declares a healthy
    long review dead."""
    import inspect
    assert inspect.signature(sim.drive_push).parameters["timeout_s"].default >= 3600
    assert inspect.signature(sim.run_scenario).parameters["timeout_s"].default >= 3600


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
# Finding-id placeholders (operator-command argv expansion)
# --------------------------------------------------------------------------

def _write_findings_log(scratch, entries):
    """Fabricate the append-only findings log the placeholders read: one
    JSON verdict entry per line, oldest first (the gate's _record_review
    shape -- head plus a findings list)."""
    log = scratch.work / ".git" / "review-gate-findings.jsonl"
    with log.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def test_placeholders_expand_singular_and_plural_ids(tmp_path):
    """{finding:<path>} stays the FIRST id recorded on the path; the plural
    {findings:<path>} expands to EVERY accepted display id on that path in
    the LATEST verdict -- reviewer ids verbatim, else the gate's stable
    sha256(path, existing_code, content)[:12] -- one argv token each, in
    recorded order. A stale id from an OLDER verdict is never resurrected:
    the latest verdict is what the gate's waive resolves against."""
    target = _toy_repo(tmp_path)
    scratch = sim.make_scratch(target, tmp_path / "scratch")
    older = {"head": "old", "findings": [
        {"path": "app.py", "id": "stale-1", "content": "old"}]}
    latest = {"head": "new", "findings": [
        {"path": "app.py", "id": "rev-42", "content": "c1"},
        {"path": "other.py", "id": "rev-9", "content": "c2"},
        {"path": "app.py", "existing_code": "print('v1')",
         "content": "boom"},
    ]}
    _write_findings_log(scratch, [older, latest])
    sha = hashlib.sha256(
        "\x00".join(("app.py", "print('v1')", "boom")).encode("utf-8")
    ).hexdigest()[:12]
    argv = ["{python}", "-c",
            "import json,sys;print(json.dumps(sys.argv[1:]))",
            "{finding:app.py}", "{findings:app.py}", "{finding:other.py}"]
    rc, out = sim._run_gate_command(
        scratch, {"PATH": os.environ.get("PATH", "")}, argv, _SCRIPTS_DIR)
    assert rc == 0, out
    assert json.loads(out.strip().splitlines()[-1]) == [
        "rev-42",              # singular: first match only
        "rev-42", sha,         # plural: every match, in recorded order
        "rev-9"]


def test_plural_placeholder_omits_unmatched_paths_and_raises_when_empty(tmp_path):
    """A path the latest verdict says nothing about resolves to nothing,
    and an empty log is a harness error -- a scenario that waives a finding
    nothing recorded is broken, not silently passing."""
    target = _toy_repo(tmp_path)
    scratch = sim.make_scratch(target, tmp_path / "scratch")
    _write_findings_log(scratch, [{"head": "new", "findings": [
        {"path": "other.py", "id": "rev-9", "content": "c"}]}])
    with pytest.raises(sim.SimError, match="no recorded finding"):
        sim._finding_ids_for_path(scratch, "app.py")
    with pytest.raises(sim.SimError, match="no recorded finding"):
        sim._last_finding_id_for_path(scratch, "app.py")
    (scratch.work / ".git" / "review-gate-findings.jsonl").unlink(missing_ok=True)
    assert sim._read_findings_log(scratch) == []
    with pytest.raises(sim.SimError, match="no recorded finding"):
        sim._finding_ids_for_path(scratch, "other.py")


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


# --------------------------------------------------------------------------
# SPEC-16 real-model simulation boundary (all transport/model calls doubled)
# --------------------------------------------------------------------------

def test_real_model_environment_removes_every_double_and_provider_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "model-auth-may-remain")
    monkeypatch.setenv("GITLAB_WRITE_TOKEN", "must-not-survive")
    monkeypatch.setenv("GH_TOKEN", "must-not-survive")
    monkeypatch.setenv("RG_FINAL_WORKER", "stub-reviewer")
    monkeypatch.setenv("RG_FINAL_DYNAMIC_EXECUTOR", "stub-executor")
    monkeypatch.setenv("OCR_TEST_SELECTOR_CMD", "stub-selector")
    monkeypatch.setenv("STUB_FINAL_RESULT", '{"findings":[]}')
    env = sim.real_model_environment({
        **os.environ, "RG_TEST_PROVIDER_SCRIPT": str(tmp_path / "provider.json"),
    }, artifact_dir=tmp_path / "artifacts")

    assert env["OPENAI_API_KEY"] == "model-auth-may-remain"
    assert env["GLAB_TOKEN"] == "recorded-read-only-provider-fixture"
    assert env["OCR_STATE_DIR"] == str((tmp_path / "artifacts").resolve() / "state")
    for key in (
        "GITLAB_WRITE_TOKEN", "GH_TOKEN", "RG_FINAL_WORKER",
        "RG_FINAL_DYNAMIC_EXECUTOR", "OCR_TEST_SELECTOR_CMD", "STUB_FINAL_RESULT",
    ):
        assert key not in env


def test_selected_inference_backend_is_an_exact_incomplete_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(sim.shutil, "which", lambda _name, path=None: None)
    with pytest.raises(sim.SimError, match="not on PATH") as captured:
        sim.selected_inference_backend({"OCR_BACKEND": "kimchi", "PATH": "/bin"}, repo_root=tmp_path)
    assert "not executed" in str(captured.value)


def test_final_review_simulation_persists_real_cli_evidence_and_read_audit(tmp_path, monkeypatch):
    target = _toy_repo(tmp_path)
    calls = tmp_path / "calls.json"
    calls.write_text(json.dumps([{"argv": ["--glab", "api", "projects/111"]}]))
    script = tmp_path / "provider.json"
    script.write_text(json.dumps({"calls": str(calls)}))
    monkeypatch.setattr(sim, "selected_inference_backend", lambda *args, **kwargs: {
        "name": "kimchi", "reason": "test", "executable": "/bin/kimchi", "notes": [],
    })
    result = {
        "state": "pass", "run_id": "real-run",
        "units": [{"unit_key": "individual:1", "repository_path": str(tmp_path / "removed")}],
    }
    observed = {}

    def runner(command, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr="")

    report = sim.run_final_review_simulation(
        "https://gitlab.example.test/group/project/-/merge_requests/17",
        repo_root=target, artifact_dir=tmp_path / "simulation",
        environ={**os.environ, "RG_TEST_PROVIDER_SCRIPT": str(script)}, runner=runner,
    )

    assert report["state"] == "pass" and report["source_unchanged"] is True
    assert report["provider"] == {
        "calls": 1, "provider_writes": False, "mutating_argv": [], "audit": str(calls),
    }
    assert report["cleanup"]["clean"] is True
    assert report["artifact"]["evidence_id"].startswith("sha256:")
    assert Path(report["artifact"]["artifact"]).is_file()
    assert observed["env"]["GLAB_TOKEN"] == "recorded-read-only-provider-fixture"
    assert "RG_FINAL_WORKER" not in observed["env"]
    assert json.loads(Path(report["artifact"]["artifact"]).read_text())["result"] == result


def test_final_review_simulation_never_hides_timeout_or_provider_mutation(tmp_path, monkeypatch):
    target = _toy_repo(tmp_path)
    calls = tmp_path / "calls.json"
    calls.write_text(json.dumps([{"argv": ["--glab", "api", "-f", "state=success"]}]))
    script = tmp_path / "provider.json"
    script.write_text(json.dumps({"calls": str(calls)}))
    monkeypatch.setattr(sim, "selected_inference_backend", lambda *args, **kwargs: {
        "name": "kimchi", "reason": "test", "executable": "/bin/kimchi", "notes": [],
    })

    def timeout_runner(*args, **kwargs):
        raise subprocess.TimeoutExpired("kimchi", 1)

    timeout_root = tmp_path / "timeout"
    abandoned = timeout_root / "state" / "clones" / "abc-final-timeout"
    abandoned.mkdir(parents=True)
    timed_out = sim.run_final_review_simulation(
        "ref", repo_root=target, artifact_dir=timeout_root, environ={},
        runner=timeout_runner,
    )
    assert timed_out["state"] == "incomplete"
    assert "TimeoutExpired" in timed_out["error"]
    assert not abandoned.exists()
    assert timed_out["cleanup"]["clean"] is True
    assert timed_out["cleanup"]["registered_clones_removed_after_timeout"] == [str(abandoned)]

    mutation = sim.run_final_review_simulation(
        "ref", repo_root=target, artifact_dir=tmp_path / "mutation",
        environ={"RG_TEST_PROVIDER_SCRIPT": str(script)},
        runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout='{"state":"pass"}', stderr=""),
    )
    assert mutation["provider"]["provider_writes"] is True
    assert "forbidden provider mutation" in mutation["error"]


def test_final_review_simulation_timeout_kills_tree_and_cleans_scratch_clones(
        tmp_path, monkeypatch):
    target = _toy_repo(tmp_path)
    fake_scripts = tmp_path / "fake-final-scripts"
    fake_scripts.mkdir()
    marker = tmp_path / "outer-grandchild-survived"
    pid_file = tmp_path / "outer-grandchild.pid"
    (fake_scripts / "review-gate.py").write_text(
        "import os, pathlib, subprocess, sys, time\n"
        "clone = pathlib.Path(os.environ['OCR_STATE_DIR']) / 'clones' / 'abc-final-timeout'\n"
        "clone.mkdir(parents=True, exist_ok=True)\n"
        "child_code = \"import pathlib,sys,time; time.sleep(1); "
        "pathlib.Path(sys.argv[1]).write_text('survived')\"\n"
        "child = subprocess.Popen([sys.executable, '-c', child_code, "
        "os.environ['GRANDCHILD_MARKER']], stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "pathlib.Path(os.environ['GRANDCHILD_PID']).write_text(str(child.pid))\n"
        "print('outer-ready', flush=True)\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sim, "_SCRIPTS", str(fake_scripts))
    monkeypatch.setattr(sim, "selected_inference_backend", lambda *args, **kwargs: {
        "name": "claude", "reason": "test", "executable": "/bin/claude", "notes": [],
    })
    grandchild = None
    try:
        report = sim.run_final_review_simulation(
            "ref", repo_root=target, artifact_dir=tmp_path / "outer-timeout",
            environ={
                "PATH": os.environ.get("PATH", ""),
                "GRANDCHILD_MARKER": str(marker),
                "GRANDCHILD_PID": str(pid_file),
            },
            timeout=0.3,
        )
        grandchild = int(pid_file.read_text())
        time.sleep(1.2)
        assert not marker.exists(), "outer CLI grandchild created a post-timeout side effect"
        assert _wait_gone(grandchild), "outer CLI grandchild remained in flight"
        assert report["state"] == "incomplete"
        assert "TimeoutExpired" in report["error"]
        assert report["returncode"] not in (None, 0)
        assert "outer-ready" in report["stdout"]
        abandoned = tmp_path / "outer-timeout" / "state" / "clones" / "abc-final-timeout"
        assert not abandoned.exists()
        assert report["cleanup"]["registered_clones_removed_after_timeout"] == [str(abandoned)]
        assert report["cleanup"]["clean"] is True
    finally:
        if grandchild is not None and not _wait_gone(grandchild, timeout=0.1):
            try:
                os.kill(grandchild, signal.SIGKILL)
            except OSError:
                pass


def test_dynamic_simulation_executes_pinned_seam_and_verifies_cleanup(tmp_path, monkeypatch):
    target = _toy_repo(tmp_path)
    head = _git(["rev-parse", "HEAD"], target)
    monkeypatch.setattr(sim, "selected_inference_backend", lambda *args, **kwargs: {
        "name": "kimchi", "reason": "test", "executable": "/bin/kimchi", "notes": [],
    })
    observed = {}

    def dynamic_runner(repository, target_sha, head_sha, **kwargs):
        observed.update(kwargs)
        observed["process_state_dir"] = os.environ.get("OCR_STATE_DIR")
        return {
            "schema": "final-review-dynamic-result/v1", "state": "findings",
            "findings": [{"evidence_source": "existing_test"}], "warnings": [],
        }

    report = sim.run_dynamic_simulation(
        target, head, head, artifact_dir=tmp_path / "dynamic",
        environ={"RG_FINAL_DYNAMIC_EXECUTOR": "must-be-removed"},
        dynamic_runner=dynamic_runner,
    )

    assert report["state"] == "findings" and report["source_unchanged"] is True
    assert report["cleanup"] == {
        "registered_scratch_clones": [], "materialized_repositories_remaining": [],
        "clean": True, "source_probe_root_remaining": False,
    }
    assert observed["unit_key"] == "individual:1"
    assert observed["process_state_dir"] == str(
        (tmp_path / "dynamic").resolve() / "state"
    )
    assert "RG_FINAL_DYNAMIC_EXECUTOR" not in observed["environ"]
    assert Path(report["artifact"]["artifact"]).is_file()


def test_dynamic_simulation_refuses_an_unsupported_selected_backend(tmp_path, monkeypatch):
    target = _toy_repo(tmp_path)
    monkeypatch.setattr(sim, "selected_inference_backend", lambda *args, **kwargs: {
        "name": "claude", "reason": "test", "executable": "/bin/claude", "notes": [],
    })
    with pytest.raises(sim.SimError, match="reference dynamic selector requires"):
        sim.run_dynamic_simulation(target, "a" * 40, "b" * 40, artifact_dir=tmp_path / "dynamic")


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


# --------------------------------------------------------------------------
# Frozen target: pinned commit, independent clone, ref-level delta
# --------------------------------------------------------------------------

def _run_main(tmp_path, target, monkeypatch=None, before_scratch=None):
    """Run main() on `target`; `before_scratch()` fires after the start
    snapshot and before the clone, i.e. while the run is going."""
    sc_mod = tmp_path / "scen.py"
    sc_mod.write_text(
        "from sim_harness import CommitSpec, Scenario\n"
        "SCENARIOS = [Scenario(name='ok', description='d', expect='pass',\n"
        "  commits=[CommitSpec(message='c', write={'f.py': 'z = 1\\n'})])]\n")
    root = tmp_path / "scratch"
    if before_scratch:
        real = sim.make_scratch

        def wrapped(*a, **kw):
            before_scratch()
            return real(*a, **kw)
        monkeypatch.setattr(sim, "make_scratch", wrapped)
    rc = sim.main(["--repo", str(target), "--scenarios", str(sc_mod),
                   "--workdir", str(root), "--gate-scripts", _fake_gate_scripts(tmp_path),
                   "--inline-budget", "0"])
    return rc, json.loads((root / "results.json").read_text()), root


def test_report_records_the_pinned_commit_and_the_untouched_target(tmp_path, capsys):
    target = _toy_repo(tmp_path)
    head = _git(["rev-parse", "HEAD"], target)
    rc, data, _root = _run_main(tmp_path, target)
    assert rc == 0, capsys.readouterr().out
    assert data["pinned_commit"] == head
    assert data["target_untouched"] is True
    assert "target_mutation" not in data


def test_scratch_is_cloned_from_the_pinned_commit_even_if_the_source_moved(tmp_path):
    target = _toy_repo(tmp_path)
    pinned = _git(["rev-parse", "HEAD"], target)
    (target / "app.py").write_text("print('v2')\n")
    _git(["commit", "-qam", "newer"], target)
    scratch = sim.make_scratch(target, tmp_path / "scratch", pinned=pinned)
    assert scratch.pinned_commit == pinned
    assert _git(["rev-parse", "HEAD"], scratch.work) == pinned
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], scratch.work) == "main"
    assert (scratch.work / "app.py").read_text() == "print('v1')\n"


def test_scratch_shares_no_files_with_the_source(tmp_path):
    target = _toy_repo(tmp_path)
    scratch = sim.make_scratch(target, tmp_path / "scratch")
    objects = scratch.work / ".git" / "objects"
    assert not (objects / "info" / "alternates").exists()
    files = [p for p in objects.rglob("*") if p.is_file()]
    assert files

    def ident(p):
        st = os.stat(p)
        return (st.st_dev, st.st_ino)

    assert all(os.stat(p).st_nlink == 1 for p in files), "an object file is hardlinked"
    source_ids = {ident(p) for p in (target / ".git" / "objects").rglob("*") if p.is_file()}
    assert not source_ids & {ident(p) for p in files}


def test_a_source_commit_during_the_run_is_reported_as_source_moved(tmp_path, monkeypatch, capsys):
    target = _toy_repo(tmp_path)
    before = _git(["rev-parse", "HEAD"], target)

    def user_commits():
        (target / "app.py").write_text("print('v2')\n")
        _git(["commit", "-qam", "user commit"], target)

    rc, data, _root = _run_main(tmp_path, target, monkeypatch, user_commits)
    after = _git(["rev-parse", "HEAD"], target)
    assert data["pinned_commit"] == before
    assert data["target_untouched"] is False and rc == 1
    mut = data["target_mutation"]
    assert mut["kind"] == "source moved"
    assert mut["harness_writes"] == []
    assert {"ref": "refs/heads/main", "before": before, "after": after} in mut["refs"]
    assert {"ref": "HEAD", "before": before, "after": after} in mut["refs"]
    assert "refs/heads/main" in mut["message"] and before in mut["message"]
    assert "target mutated" not in mut["message"]
    assert "WARNING SOURCE MOVED" in capsys.readouterr().out


def test_a_harness_write_to_the_target_is_not_reported_as_a_source_move(tmp_path, monkeypatch):
    target = _toy_repo(tmp_path)

    def harness_bug():
        sim._git(["update-ref", "refs/heads/oops", "HEAD"], target)

    rc, data, _root = _run_main(tmp_path, target, monkeypatch, harness_bug)
    assert data["target_untouched"] is False and rc == 1
    mut = data["target_mutation"]
    assert mut["kind"] == "target mutated"
    assert mut["harness_writes"] == ["git update-ref refs/heads/oops HEAD"]
    assert [r["ref"] for r in mut["refs"]] == ["refs/heads/oops"]
    assert mut["refs"][0]["before"] is None


def test_ref_delta_always_names_the_ref(tmp_path):
    target = _toy_repo(tmp_path)
    before = sim.repo_snapshot(target)
    _git(["branch", "side"], target)
    change = sim.describe_target_change(before, sim.repo_snapshot(target))
    assert change["refs"] == [{"ref": "refs/heads/side", "before": None,
                               "after": before["head"]}]
    assert "refs changed" not in change["message"]
    with pytest.raises(sim.SimError, match="refs/heads/side"):
        sim.assert_repo_unchanged(before, sim.repo_snapshot(target))
    assert sim.describe_target_change(before, before) is None
