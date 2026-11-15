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
              "OCR_LEGACY_RANGE", "OCR_INLINE_BUDGET", "STUB_SLEEP", "STUB_VERDICT", "STUB_TRACE",
              "OCR_FULL_HISTORY_LIMIT"):
        env.pop(k, None)
    # OCR_STATE_DIR is inherited from conftest's per-test private store: the
    # in-process review_gate module reads the same one, so state written by the
    # subprocess is visible to _wait_state.
    env["OCR_STUB_WORKER"] = "1"
    env["STUB_TRACE"] = str(tmp_path / "stub.trace")
    for k, v in stub.items():
        env[k] = str(v)
    return env


def _chunk_env(tmp_path, **extra):
    """Like _env but with threshold/files set so 4 files produce 4 chunks."""
    e = _env(tmp_path, **extra)
    e["OCR_CHUNK_THRESHOLD"] = "3"   # >3 files → chunking
    e["OCR_CHUNK_FILES"] = "1"       # 1 file per chunk
    e["OCR_CHUNK_LINES"] = "99999"   # don't split on lines
    e["OCR_RUN_BUDGET"] = "9999"     # won't exhaust budget
    return e


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


def _supervise_inproc(repo, tip, run_id, attempts=0):
    """One supervisor run, in this process, against the real stub reviewer."""
    common = review_gate._git_common_dir(str(repo))
    state_path = review_gate._state_path(common, tip)
    base = _git(["rev-parse", "origin/main"], cwd=repo)
    review_gate._write_state(state_path, {
        "state": "claimed", "run_id": run_id, "claimed_ts": time.time(), "tip": tip,
        "branch": "main", "base": base, "range": f"{base}..{tip}", "repo_root": str(repo),
        "git_dir": review_gate._git_dir(str(repo)), "mode": "hook", "attempts": attempts,
        "protocol_version": review_gate.PROTOCOL_VERSION,
    })
    review_gate._supervise(str(state_path), run_id)
    return review_gate._read_state(state_path)


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


def test_the_failed_async_note_never_reads_as_finished(tmp_path):
    # A flush of a note whose review FAILED must be unambiguous in the first
    # sentence: the review did not finish, so nothing was approved.
    tip = "a" * 40
    state_path = tmp_path / "state.json"
    review_gate._write_state(str(state_path), {
        "state": "failed", "run_id": "r1", "tip": tip, "branch": "main",
        "failed_ts": time.time(), "attempts": 1, "reason": "review",
        "detail": "boom", "protocol_version": review_gate.PROTOCOL_VERSION,
    })
    note_path = tmp_path / "note.json"
    note_path.write_text(json.dumps(
        {"kind": "async", "state": str(state_path), "repo": str(tmp_path), "head": tip}),
        encoding="utf-8")
    note = review_gate._async_note(str(note_path),
                                   {"state": str(state_path), "repo": str(tmp_path),
                                    "head": tip}, "s1")
    assert "did NOT finish" in note
    assert "failed" in note
    assert "has finished" not in note
    assert "Re-run the `git push`" in note


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


# --- huge-range guard (SPEC-09) ----------------------------------------------

def _fresh_repo(tmp_path, commits=3):
    """A repo whose remote holds NO branches: every push has no bounding ref,
    so resolve_review_base lands on _EMPTY_TREE (the unseeded-remote case)."""
    remote = tmp_path / "bare-origin.git"
    work = tmp_path / "fresh-work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    _git(["config", "user.email", "t@example.com"], cwd=work)
    _git(["config", "user.name", "t"], cwd=work)
    _git(["config", "commit.gpgsign", "false"], cwd=work)
    hooks = tmp_path / "fresh-no-hooks"
    hooks.mkdir(exist_ok=True)
    _git(["config", "core.hooksPath", str(hooks)], cwd=work)
    if commits:
        marks = []
        for i in range(commits):
            marks.append(
                "commit refs/heads/main\n"
                f"mark :{i + 1}\n"
                f"committer t <t@example.com> {1600000000 + i} +0000\n"
                f"data {len(f'c{i}')}\nc{i}\n"
                + (f"from :{i}\n" if i else "")
                + "M 644 inline f.txt\n"
                + f"data {len(str(i)) + 1}\n{i}\n")
        proc = subprocess.run(["git", "fast-import", "--quiet"], cwd=str(work),
                              input="".join(marks), capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
    return work


def test_an_unseeded_remote_push_past_the_history_limit_is_refused(tmp_path):
    work = _fresh_repo(tmp_path, commits=1000)
    decision, reason, _, _ = _hook(work, "git push origin main", _env(tmp_path))
    assert decision == "deny", reason
    assert "OCR_FULL_HISTORY_LIMIT" in reason
    assert "500" in reason and "1000" in reason
    # Remedy text: seed the remote (push base/main first) or narrow the base.
    assert "push origin main" in reason
    assert "narrow" in reason.lower()
    # A brand-new repository's first push legitimately hits this path; the
    # remedy must say exactly what to do about it.
    assert "first push" in reason
    # Refused BEFORE any review spawns.
    assert _trace(tmp_path) == []


def test_an_unseeded_remote_push_under_the_limit_reviews_normally(tmp_path):
    work = _fresh_repo(tmp_path, commits=3)
    env = _env(tmp_path, OCR_FULL_HISTORY_LIMIT=10)
    decision, reason, _, _ = _hook(work, "git push origin main", env)
    assert decision == "pass", reason
    assert len(_trace(tmp_path)) == 1


def test_full_history_limit_zero_disables_the_guard(tmp_path):
    work = _fresh_repo(tmp_path, commits=1000)
    env = _env(tmp_path, OCR_FULL_HISTORY_LIMIT=0)
    decision, reason, _, _ = _hook(work, "git push origin main", env)
    assert decision == "pass", reason


def test_a_bounded_range_never_trips_the_history_limit(repo, tmp_path):
    # Non-empty base: origin/main bounds the range, however many commits it
    # carries -- the guard is only for the unbounded (_EMPTY_TREE) case.
    for i in range(3):
        _commit(repo, f"f{i}", f"change {i}")
    env = _env(tmp_path, OCR_FULL_HISTORY_LIMIT=1)
    decision, reason, _, _ = _hook(repo, "git push origin main", env)
    assert decision == "pass", reason


def test_the_history_limit_refusal_honors_fail_open(tmp_path):
    work = _fresh_repo(tmp_path, commits=1000)
    env = _env(tmp_path, OCR_FAIL_OPEN=1)
    decision, reason, _, stderr = _hook(work, "git push origin main", env)
    assert decision == "pass"
    assert "OCR_FULL_HISTORY_LIMIT" in (reason or "") or "OCR_FULL_HISTORY_LIMIT" in stderr
    assert _trace(tmp_path) == []


def test_an_unevaluable_history_count_refuses_instead_of_proceeding(tmp_path):
    """SPEC-09 fails closed: when git rev-list --count cannot count the
    commits of an unseeded (_EMPTY_TREE-base) push, the guard must refuse
    (unknown counts as over-limit), not start a full-history review."""
    work = _fresh_repo(tmp_path, commits=0)
    for i in range(3):
        (work / "f.txt").write_text(f"c{i}\n")
        _git(["add", "."], cwd=work)
        _git(["commit", "-q", "-m", f"c{i}"], cwd=work)
    # The tip itself still resolves, but its root-ancestor object is gone,
    # so `git rev-list --count <tip>` fails while the push flow proceeds.
    first = _git(["rev-list", "--max-parents=0", "HEAD"], cwd=work)
    (Path(work) / ".git" / "objects" / first[:2] / first[2:]).unlink()
    decision, reason, _, _ = _hook(work, "git push origin main", _env(tmp_path))
    assert decision == "deny", reason
    assert "OCR_FULL_HISTORY_LIMIT" in reason
    assert "rev-list" in reason
    assert _trace(tmp_path) == []


# --- SPEC-08: chunk-aware run budget ------------------------------------------

def _budget_env(tmp_path, threshold="3", files="1", **extra):
    """Chunked-push env WITHOUT OCR_RUN_BUDGET, so the chunk-aware budget runs."""
    e = _env(tmp_path, **extra)
    e["OCR_CHUNK_THRESHOLD"] = threshold   # >threshold files -> chunked review
    e["OCR_CHUNK_FILES"] = files           # files per chunk
    e["OCR_CHUNK_LINES"] = "99999"         # don't split on lines
    if "OCR_RUN_BUDGET" not in extra:
        e.pop("OCR_RUN_BUDGET", None)      # else the flat override would win
    return e


def _budget_of(work, tip, env, chunks_total, timeout=180):
    """Push through the hook, wait for the verdict, and return the state's
    budget seconds: deadline_ts is realigned with the run budget once the
    chunk plan is known, so deadline_ts - started_ts is the budget."""
    decision, reason, _, stderr = _hook(work, "git push origin main", env, timeout=timeout)
    assert decision == "pass", (reason, stderr)
    st = _wait_state(work, tip, {"done"}, timeout=timeout)
    assert st.get("verdict") == "pass", st
    assert st.get("chunks_total") == chunks_total, st   # the chunked path ran
    return st["deadline_ts"] - st["started_ts"]


def _seed_chunk_telemetry(lines):
    """Prior per-chunk telemetry in this test's private store's debug log."""
    import rg_store
    data = rg_store.store_root() / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "review-gate-debug.log").write_text("".join(lines), encoding="utf-8")


def _chunk_line(run, secs):
    return (f"ts=2026-01-01T00:00:00Z event=chunk run={run} index=0 of=3 "
            f"files=2 lines=100 outcome=ok s={secs}\n")


def test_small_chunked_push_budgets_ocr_timeout(tmp_path):
    """First run, no telemetry: a 1-chunk push budgets exactly OCR_TIMEOUT --
    the flat behaviour the chunk-aware computation replaces."""
    work = _big_repo(tmp_path, n_files=2)
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    delta = _budget_of(work, tip, _budget_env(tmp_path, threshold="1", files="2"),
                       chunks_total=1)
    assert 1800 <= delta < 1890, delta        # OCR_TIMEOUT (+ planning slop)


def test_big_plan_budgets_the_cap(tmp_path):
    """21 chunks, no telemetry: the scaled budget is far above the flat
    OCR_TIMEOUT deadline and is clamped to the OCR_TIMEOUT_MAX cap."""
    work = _big_repo(tmp_path, n_files=21)
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    delta = _budget_of(work, tip, _budget_env(tmp_path), chunks_total=21)
    assert 3600 <= delta < 3690, delta        # capped at the 3600 s default


def test_the_cap_env_is_honored(tmp_path):
    work = _big_repo(tmp_path, n_files=21)
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    delta = _budget_of(work, tip, _budget_env(tmp_path, OCR_TIMEOUT_MAX=7200),
                       chunks_total=21)
    assert 7200 <= delta < 7290, delta


def test_invalid_cap_falls_back_to_3600(tmp_path):
    """A non-integer cap falls back to the 3600 s default. Large telemetry is
    seeded first so the UNCLAMPED budget far exceeds 3600 s -- otherwise the
    chunk-aware floor (max budget, OCR_TIMEOUT) would legitimately give 1800 s
    and the test could not tell cap fallback from floor (the stub reviews of a
    previous iteration seed tiny averages into the per-test store)."""
    _seed_chunk_telemetry([_chunk_line(f"r-{j}", 600) for j in range(25)])
    work = _big_repo(tmp_path, n_files=21)
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    delta = _budget_of(work, tip, _budget_env(tmp_path, OCR_TIMEOUT_MAX="abc"),
                       chunks_total=21)
    assert 3600 <= delta < 3690, delta


def test_cap_below_the_deadline_clamps_to_the_deadline(tmp_path):
    """A cap at or below OCR_TIMEOUT no longer resets to 3600 (the old reset
    would pin the budget to the flat deadline whenever OCR_TIMEOUT itself was
    raised past 3600): the effective ceiling is max(cap, OCR_TIMEOUT), so the
    budget clamps to the flat deadline."""
    _seed_chunk_telemetry([_chunk_line(f"r-{j}", 600) for j in range(25)])
    work = _big_repo(tmp_path, n_files=21)
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    delta = _budget_of(work, tip, _budget_env(tmp_path, OCR_TIMEOUT_MAX=100),
                       chunks_total=21)
    assert 1800 <= delta < 1890, delta


def test_raised_deadline_raises_the_default_ceiling_with_it(tmp_path):
    """OCR_TIMEOUT=7200 with no OCR_TIMEOUT_MAX: the effective ceiling is
    max(3600, OCR_TIMEOUT), so the budget can exceed 3600 -- it sits at the
    raised flat deadline instead of being dragged back down to the default
    cap."""
    _seed_chunk_telemetry([_chunk_line(f"r-{j}", 600) for j in range(25)])
    work = _big_repo(tmp_path, n_files=21)
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    delta = _budget_of(work, tip, _budget_env(tmp_path, OCR_TIMEOUT=7200),
                       chunks_total=21)
    assert 7200 <= delta < 7290, delta


def test_trailing_telemetry_sizes_the_budget(tmp_path):
    """Prior chunk telemetry feeds the budget: 600 s per chunk on average, 4
    chunks across 2 slots -> ceil(4/2) = 2 waves x 600 s = 1200 s of true wall
    time, floored at OCR_TIMEOUT = 1800 s (a `call` metric line must not leak
    into the chunk average). The dogfood review caught the old total-x-slots
    formula as double-counting parallelism."""
    _seed_chunk_telemetry([_chunk_line(f"r{i}", 600) for i in range(3)]
                          + ["ts=2026-01-01T00:00:00Z event=call run=r9 "
                             "kind=review outcome=ok s=99999\n"])
    work = _big_repo(tmp_path, n_files=4)
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    delta = _budget_of(work, tip,
                       _budget_env(tmp_path, OCR_TIMEOUT_MAX=9000), chunks_total=4)
    assert 1800 <= delta < 1890, delta


def test_run_budget_env_stays_a_flat_override(tmp_path):
    """An explicit OCR_RUN_BUDGET keeps today's flat meaning."""
    work = _big_repo(tmp_path, n_files=4)
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    delta = _budget_of(work, tip, _budget_env(tmp_path, OCR_RUN_BUDGET=6000),
                       chunks_total=4)
    assert 6000 <= delta < 6090, delta


def test_run_budget_equal_to_the_default_still_overrides(tmp_path):
    """OCR_RUN_BUDGET=3600, explicitly: even when it equals the default, the
    flat override must win -- otherwise the seeded telemetry's much larger
    chunk-aware budget would silently take over."""
    _seed_chunk_telemetry([_chunk_line(f"r{i}", 600) for i in range(3)])
    work = _big_repo(tmp_path, n_files=21)
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    delta = _budget_of(work, tip,
                       _budget_env(tmp_path, OCR_RUN_BUDGET=3600, OCR_TIMEOUT_MAX=9000),
                       chunks_total=21)
    assert 3600 <= delta < 3690, delta
