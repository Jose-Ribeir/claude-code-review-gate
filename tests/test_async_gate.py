"""End-to-end tests for the asynchronous gate (Kimchi port, hook level).

Everything here runs the REAL pieces: a temporary git repository with a bare
remote, review-gate.py as a subprocess in `--mode hook` exactly as the Kimchi
bash adapter runs it, a genuinely detached `--mode supervise` child, and the
stub reviewer (tests/kimi_stub.py via OCR_STUB_WORKER=1) in place of
`kimchi -p`. Nothing is monkeypatched, so what passes here is what runs in
production.

Ported from test_async_gate.py (claude-code-review-gate): the hook-level
portion. The chunk/ledger/segment suites live in their own files; the state
store moved OUT of the repository (D12), so every test runs against a private
OCR_STATE_DIR (see conftest.py).
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_STUB = os.path.join(_HERE, "kimi_stub.py")

sys.path.insert(0, _SCRIPTS)
from hook_output import parse_pretooluse  # noqa: E402
from conftest import scrub_git_trace  # noqa: E402
_spec = importlib.util.spec_from_file_location("review_gate_async", _GATE)
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)


def _git(args, cwd, env=None):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                          check=True, env=env).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A working repo with one pushed commit on main and a bare origin."""
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    _git(["config", "user.email", "t@example.com"], cwd=work)
    _git(["config", "user.name", "t"], cwd=work)
    _git(["config", "commit.gpgsign", "false"], cwd=work)
    # The developer's own global review-gate pre-push hook must not fire on
    # this fixture's pushes: a repo-local hooksPath shadows it.
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    _git(["config", "core.hooksPath", str(hooks)], cwd=work)
    (work / "a.txt").write_text("one\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    return work


def _commit(work, name="feat", msg="change"):
    (work / f"{name}.txt").write_text(msg + "\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", msg], cwd=work)
    return _git(["rev-parse", "HEAD"], cwd=work)


def _env(tmp_path, **stub):
    """Environment for a hook run: isolated state store, stub worker, no
    inherited in-review/bypass state, the 3 s hook budget."""
    env = scrub_git_trace(dict(os.environ))
    for k in ("OCR_IN_REVIEW", "OCR_FAIL_OPEN", "OCR_ADVISORY", "OCR_FORCE_REVIEW",
              "OCR_LEGACY_RANGE", "OCR_INLINE_BUDGET", "STUB_SLEEP", "STUB_VERDICT", "STUB_TRACE"):
        env.pop(k, None)
    # OCR_STATE_DIR is inherited from conftest's per-test private store: the
    # in-process review_gate module reads the same one, so state written by the
    # subprocess is visible to _wait_state.
    env["OCR_STUB_WORKER"] = "1"
    env["STUB_TRACE"] = str(tmp_path / "stub.trace")
    for k, v in stub.items():
        env[k] = str(v)
    return env


def _hook(work, cmd, env, session="s1", timeout=120):
    payload = json.dumps({"session_id": session, "tool_name": "bash", "cwd": str(work),
                          "input": {"command": cmd}})
    t0 = time.monotonic()
    proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                          capture_output=True, text=True, cwd=str(work), env=env,
                          timeout=timeout)
    elapsed = time.monotonic() - t0
    assert proc.returncode == 0, proc.stderr
    decision, reason = parse_pretooluse(proc.stdout)
    return decision, reason, elapsed, proc.stderr


def _trace(tmp_path):
    p = tmp_path / "stub.trace"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _wait_state(work, tip, want, timeout=60):
    common = review_gate._git_common_dir(str(work))
    path = review_gate._state_path(common, tip)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = review_gate._read_state(path) or {}
        if st.get("state") in want:
            return st
        time.sleep(0.2)
    raise AssertionError(f"state never reached {want}: {review_gate._read_state(path)}")


def _verdict_record(work, tip, remote="origin"):
    import rg_store
    url = rg_store.remote_url(str(work), remote)
    return rg_store.load_verdict(url, tip)


# --- inline verdicts ----------------------------------------------------------

def test_a_short_review_answers_inline_and_pushes(repo, tmp_path):
    tip = _commit(repo)
    decision, reason, elapsed, _ = _hook(repo, "git push origin main", _env(tmp_path))
    assert decision == "pass", reason
    assert elapsed < 10
    st = _wait_state(repo, tip, {"done"})
    assert st["verdict"] == "pass" and st["blocked"] is False
    # The EXTERNAL store backs the verdict (D12), keyed (remote, tip).
    rec = _verdict_record(repo, tip)
    assert rec is not None and rec["verdict"] == "pass"
    base = _git(["rev-parse", "origin/main"], cwd=repo)
    assert tip in rec["commits"] and base not in rec["commits"]
    # What was reviewed: exactly the commits origin/main lacks.
    (trace,) = _trace(tmp_path)
    assert trace["range"].endswith(".." + tip)
    assert trace["range"].startswith(base)


def test_the_reviewer_reads_a_detached_clone_not_the_live_tree(repo, tmp_path):
    tip = _commit(repo)
    _hook(repo, "git push origin main", _env(tmp_path))
    _wait_state(repo, tip, {"done"})
    (trace,) = _trace(tmp_path)
    assert os.path.realpath(trace["cwd"]) != os.path.realpath(str(repo))
    assert "clones" in trace["cwd"]          # the store's clones/ dir, not .git
    # ...and it is gone afterwards, and left nothing in the repo's worktrees.
    assert not os.path.isdir(trace["cwd"])
    assert "worktree" not in _git(["worktree", "list"], cwd=repo)


def test_a_block_denies_with_findings_and_replays_on_retry(repo, tmp_path):
    tip = _commit(repo)
    env = _env(tmp_path, STUB_VERDICT="block")
    decision, reason, _, _ = _hook(repo, "git push origin main", env)
    assert decision == "deny" and "stub high finding" in reason
    _wait_state(repo, tip, {"done"})
    decision, reason, elapsed, _ = _hook(repo, "git push origin main", env)
    assert decision == "deny" and "stub high finding" in reason
    assert elapsed < 10
    assert len(_trace(tmp_path)) == 1  # no second review


def test_a_reviewer_failure_fails_closed_with_the_reason(repo, tmp_path):
    _commit(repo)
    decision, reason, _, _ = _hook(repo, "git push origin main", _env(tmp_path, STUB_VERDICT="exit1"))
    assert decision == "deny"
    assert "could not complete" in reason


def test_a_missing_reviewer_denies_instead_of_failing_open(repo, tmp_path):
    # D9: `kimchi` absent with no stub is an error-state deny, never a silent
    # allow. Point KIMCHI_BIN at nothing and disable the stub.
    _commit(repo)
    env = _env(tmp_path)
    env.pop("OCR_STUB_WORKER")
    # A PATH with git but no kimchi (the Kimchi app bundle is on the dev
    # machine's PATH; /usr/bin has no kimchi).
    env["PATH"] = "/usr/bin:/bin"
    decision, reason, _, _ = _hook(repo, "git push origin main", env)
    assert decision == "deny"
    assert "kimchi" in reason


# --- past the budget ----------------------------------------------------------

def test_a_long_review_is_denied_past_the_budget_and_joined_by_the_retry(repo, tmp_path):
    tip = _commit(repo)
    env = _env(tmp_path, STUB_SLEEP=10)  # budget is 3 s
    decision, reason, elapsed, _ = _hook(repo, "git push origin main", env, timeout=60)
    assert decision == "deny", reason
    assert "still running" in reason and "re-run this exact `git push`" in reason
    assert 2 <= elapsed <= 8
    # The supervisor outlived the hook process that started it.
    st = review_gate._read_state(
        review_gate._state_path(review_gate._git_common_dir(str(repo)), tip))
    assert st["state"] == "running"
    # Once the review lands, the retry joins it without a new review.
    _wait_state(repo, tip, {"done"})
    decision, reason, elapsed, _ = _hook(repo, "git push origin main", env, timeout=60)
    assert decision == "pass", reason
    assert len(_trace(tmp_path)) == 1
    assert _verdict_record(repo, tip)["verdict"] == "pass"


def test_two_hooks_at_once_share_one_review(repo, tmp_path):
    tip = _commit(repo)
    env = _env(tmp_path, STUB_SLEEP=2)
    payload = json.dumps({"session_id": "s1", "tool_name": "bash", "cwd": str(repo),
                          "input": {"command": "git push origin main"}})
    procs = [subprocess.Popen([sys.executable, _GATE, "--mode", "hook"], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              cwd=str(repo), env=env) for _ in range(2)]
    outs = [p.communicate(payload, timeout=120)[0] for p in procs]
    decisions = [parse_pretooluse(o)[0] for o in outs]
    assert decisions == ["pass", "pass"]
    assert len(_trace(tmp_path)) == 1
    _wait_state(repo, tip, {"done"})


# --- what gets reviewed -------------------------------------------------------

def test_the_named_branch_is_reviewed_not_the_checked_out_one(repo, tmp_path):
    main_tip = _commit(repo, "m", "on main")
    _git(["push", "-q", "origin", "main"], cwd=repo)
    _git(["switch", "-q", "-c", "feat/x"], cwd=repo)
    feat_tip = _commit(repo, "f", "on feat")
    _git(["switch", "-q", "main"], cwd=repo)
    (repo / "dirty.txt").write_text("uncommitted\n")
    decision, reason, _, _ = _hook(repo, "git push -u origin feat/x", _env(tmp_path))
    assert decision == "pass", reason
    (trace,) = _trace(tmp_path)
    assert trace["range"] == f"{main_tip}..{feat_tip}"
    st = _wait_state(repo, feat_tip, {"done"})
    assert st["branch"] == "feat/x"


def test_a_push_of_commits_the_remote_already_has_is_allowed_without_review(repo, tmp_path):
    decision, _, _, _ = _hook(repo, "git push origin main", _env(tmp_path))
    assert decision == "pass"
    assert _trace(tmp_path) == []


def test_a_ref_moving_command_before_the_push_is_refused(repo, tmp_path):
    _commit(repo)
    for cmd in ("git switch main && git push origin main",
                "git commit -am x && git push origin main",
                "git push origin main; git push origin main"):
        decision, reason, _, _ = _hook(repo, cmd, _env(tmp_path))
        assert decision == "deny", cmd
        assert "review-gate" in reason
    assert _trace(tmp_path) == []


def test_no_verify_and_unknown_options_are_refused(repo, tmp_path):
    _commit(repo)
    for cmd in ("git push --no-verify origin main", "git push --frobnicate origin main"):
        decision, reason, _, _ = _hook(repo, cmd, _env(tmp_path))
        assert decision == "deny", cmd
    assert _trace(tmp_path) == []


def test_bare_pushes_without_an_explicit_refspec_are_refused(repo, tmp_path):
    # D14: unknown is never allowed.
    _commit(repo)
    for cmd in ("git push", "git push origin", "git -c k=v push", "VAR=x git push"):
        decision, reason, _, _ = _hook(repo, cmd, _env(tmp_path))
        assert decision == "deny", cmd
        assert "could not parse push command" in reason
        assert "explicit refspec" in reason
    assert _trace(tmp_path) == []


def test_tags_pointing_at_unpushed_commits_are_refused(repo, tmp_path):
    _commit(repo)
    _git(["tag", "v1"], cwd=repo)
    decision, reason, _, _ = _hook(repo, "git push --tags origin", _env(tmp_path))
    assert decision == "deny" and "tags" in reason.lower()
    _git(["push", "-q", "origin", "main"], cwd=repo)
    decision, _, _, _ = _hook(repo, "git push --tags origin", _env(tmp_path))
    assert decision == "pass"


def test_git_C_is_honoured_as_the_push_directory(repo, tmp_path):
    tip = _commit(repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    payload = json.dumps({"session_id": "s1", "tool_name": "bash", "cwd": str(elsewhere),
                          "input": {"command": f'git -C "{repo}" push origin main'}})
    proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                          capture_output=True, text=True, cwd=str(elsewhere), env=_env(tmp_path))
    assert parse_pretooluse(proc.stdout)[0] == "pass", proc.stderr
    _wait_state(repo, tip, {"done"})


def test_a_command_embedding_a_quoted_push_string_does_not_block(repo, tmp_path):
    # The Kimchi regression: a quoted push string is DATA. Kimchi fires the
    # bash hook on the whole command, where quoted spans are single words, so
    # `git log --grep="git push origin main"` is a git LOG, never a push.
    # (The Python core, handed the raw string, fail-closed denies it; the UX
    # layer is what sees this shape in production.)
    _commit(repo)
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-push").write_text("# review-gate\n")   # mark repo as gated
    env = _env(tmp_path)
    bash = shutil.which("bash")
    sh = os.path.join(os.path.dirname(_SCRIPTS), "hooks", "sh", "gate-hook.sh")
    payload = json.dumps({"tool_name": "bash", "cwd": str(repo),
                          "input": {"command": 'git log --grep="git push origin main"'}})
    proc = subprocess.run([bash, sh], input=payload, capture_output=True, text=True,
                          cwd=str(repo), env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert parse_pretooluse(proc.stdout)[0] == "pass", (proc.stdout, proc.stderr)
    assert _trace(tmp_path) == []          # no review was started


# --- the verdict store is the authority (D12) ---------------------------------

def test_a_corrupt_verdict_record_denies(repo, tmp_path):
    tip = _commit(repo)
    env = _env(tmp_path, STUB_VERDICT="block")
    _hook(repo, "git push origin main", env)
    _wait_state(repo, tip, {"done"})
    import rg_store
    url = rg_store.remote_url(str(repo), "origin")
    rg_store.verdict_path(url, tip).write_text("{corrupt", encoding="utf-8")
    decision, reason, _, _ = _hook(repo, "git push origin main", env)
    assert decision == "deny"
    assert "corrupt" in reason


def test_an_amended_tip_after_a_verdict_is_denied_again(repo, tmp_path):
    tip = _commit(repo)
    _hook(repo, "git push origin main", _env(tmp_path))
    _wait_state(repo, tip, {"done"})
    # Amend: a new tip, no verdict for it.
    (repo / "feat.txt").write_text("amended\n")
    _git(["add", "."], cwd=repo)
    _git(["commit", "-q", "--amend", "-m", "amended"], cwd=repo)
    new_tip = _git(["rev-parse", "HEAD"], cwd=repo)
    decision, reason, _, _ = _hook(repo, "git push origin main", _env(tmp_path, STUB_SLEEP=10),
                                   timeout=60)
    assert decision == "deny", reason
    assert new_tip != tip


def test_a_planted_done_state_without_a_store_record_does_not_allow(repo, tmp_path):
    # A done state the external store does not back is not a verdict: the
    # state is invalidated and the review restarts (deny until it lands).
    tip = _commit(repo)
    env = _env(tmp_path, STUB_SLEEP=10)
    _hook(repo, "git push origin main", env, timeout=60)   # deny: still running
    _wait_state(repo, tip, {"running"})
    # Let the real review land, then delete its store record and forge a done
    # state: a done state the external store does not back is not a verdict.
    _wait_state(repo, tip, {"done"})
    import rg_store
    url = rg_store.remote_url(str(repo), "origin")
    rg_store.verdict_path(url, tip).unlink()
    common = review_gate._git_common_dir(str(repo))
    path = review_gate._state_path(common, tip)
    st = review_gate._read_state(path) or {}
    st.update({"state": "done", "done_ts": time.time(), "verdict": "pass",
               "blocked": False, "reasons": ""})
    review_gate._write_state(path, st)
    decision, reason, _, _ = _hook(repo, "git push origin main", _env(tmp_path, STUB_SLEEP=10),
                                   timeout=60)
    assert decision == "deny", reason   # the forged state did not allow


# --- inside the review: the write guard ----------------------------------------

def test_the_guard_refuses_output_and_escaping_redirections(tmp_path):
    env = _env(tmp_path)
    env["OCR_IN_REVIEW"] = "1"

    def guard(cmd):
        payload = json.dumps({"tool_name": "bash", "input": {"command": cmd}})
        proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                              capture_output=True, text=True, env=env, cwd=str(tmp_path))
        return parse_pretooluse(proc.stdout)[0]

    assert guard("git diff HEAD~1") == "pass"
    assert guard("git diff HEAD~1 > .review_hunks.txt") == "pass"
    assert guard("git diff HEAD~1 > sub/scratch.txt") == "pass"
    assert guard("git diff --output=x.txt HEAD~1") == "deny"
    assert guard("git log -1 --format=x --output out.txt") == "deny"
    assert guard("git diff > ../outside.txt") == "deny"
    assert guard(f"git diff > {tmp_path}/abs.txt") == "deny"
    assert guard("git diff > $HOME/x") == "deny"


# --- helpers shared with the later-chunk suites (C6/C7) -------------------------
# The chunk/ledger/segment suites are ported in their own chunks but import
# these fixtures from here (upstream layout: one shared module). They exercise
# in-process ledger functions, so no stub worker is involved.

def _big_repo(tmp_path, n_files=4):
    """Repo with a bare remote, one base commit already pushed, then n_files
    new files committed on top (ready to push)."""
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    _git(["config", "user.email", "t@example.com"], cwd=work)
    _git(["config", "user.name", "t"], cwd=work)
    _git(["config", "commit.gpgsign", "false"], cwd=work)
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    _git(["config", "core.hooksPath", str(hooks)], cwd=work)
    (work / "base.py").write_text("# base\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    # Add n_files new files (not yet committed) so the caller can commit them.
    for i in range(n_files):
        (work / f"mod{i}.py").write_text(f"x = {i}\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "add files"], cwd=work)
    return work


def _ledger_ctx(repo):
    """(common_dir, fp, base, tip, allowed entries) for the repo's pushed range."""
    tip = _git(["rev-parse", "HEAD"], cwd=repo)
    base = _git(["rev-parse", "origin/main"], cwd=repo)
    entries, _ = review_gate._collect_diff_entries(str(repo), base, tip)
    allowed = [e for e in entries if review_gate._is_allowed_path(e["path"])]
    return (review_gate._git_common_dir(str(repo)),
            review_gate._compute_fingerprint(str(repo), tip), base, tip, allowed)


def _item(e, mode="full", record=None):
    return {"entry": e, "mode": mode, "record": record, "from_oid": "",
            "miss_reason": "no_record"}


def _record(common, fp, e):
    key = review_gate._record_key(e["path"], e.get("old_path") or "", e["status"], e["old_oid"])
    return review_gate._read_ledger_record(
        review_gate._record_path(common, fp, key, e["new_oid"]), fp, key, e["new_oid"])
