"""Tip-bound verdicts with tree-diff carry-forward (push-gate ticket pg4).

The verdict model of the push-gate spec: a verdict blesses exactly one tip per
(destination remote, ref). A rebase, reorder, or content-identical amend
produces a NEW tip whose tree content is identical to the last-reviewed state
for that (remote, ref), so the recorded pass carries forward -- re-recorded at
the new tip, the reviewer NOT invoked. Any content difference is a fresh full
review. Ref-deletion pushes invalidate (remote, ref) verdicts immediately, and
verdicts are scoped to the destination remote: a verdict recorded against
remote A can never satisfy a push to remote B. The OCR_FAIL_OPEN=1 escape in
git mode is recorded in the ledger (rg_store.record_bypass) and announced
loudly before the push proceeds.

Subprocess end to end, exactly as the installed pre-push hook runs the gate
(`--mode git`, ref updates on stdin, `--remote <name>`), against real temp
repos with bare remotes; the reviewer is tests/stub_reviewer.py and its
invocations are counted through STUB_TRACE. Every run uses the conftest's
private OCR_STATE_DIR and a GIT_TRACE-scrubbed environment.
"""
import json
import os
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_STUB = os.path.join(_HERE, "stub_reviewer.py")

sys.path.insert(0, _SCRIPTS)
import rg_store  # noqa: E402
from hook_output import parse_pretooluse  # noqa: E402
from conftest import scrub_git_trace  # noqa: E402

ZERO = "0" * 40


def _git(args, cwd, check=True, env=None):
    return subprocess.run(["git"] + args, cwd=str(cwd), env=env, capture_output=True,
                          text=True, check=check).stdout.strip()


def _config(work):
    _git(["config", "user.email", "t@example.com"], cwd=work)
    _git(["config", "user.name", "t"], cwd=work)
    _git(["config", "commit.gpgsign", "false"], cwd=work)


@pytest.fixture
def repo(tmp_path):
    """A clone with a pushed main and a bare origin (the push destination)."""
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    _config(work)
    # A repo-local hooksPath shadows the developer's own global pre-push hook.
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    _git(["config", "core.hooksPath", str(hooks)], cwd=work)
    (work / "a.txt").write_text("one\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    return work


def _commit(work, name, text=None):
    (work / name).write_text((text or name) + "\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", f"add {name}"], cwd=work)
    return _git(["rev-parse", "HEAD"], cwd=work)


def _sha(work, rev):
    return _git(["rev-parse", rev], cwd=work)


def _env(tmp_path, **kw):
    env = scrub_git_trace(dict(os.environ))
    for k in ("OCR_IN_REVIEW", "OCR_FAIL_OPEN", "OCR_ADVISORY", "OCR_FORCE_REVIEW",
              "OCR_LEGACY_RANGE", "OCR_INLINE_BUDGET", "OCR_COVERAGE_V1",
              "STUB_SLEEP", "STUB_VERDICT", "STUB_TRACE"):
        env.pop(k, None)
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "gate-data")
    env["OCR_REVIEWER_CMD"] = f'"{sys.executable}" "{_STUB}"'
    env["STUB_TRACE"] = str(tmp_path / "stub.trace")
    env["OCR_INLINE_BUDGET"] = "30"
    env.update({k: str(v) for k, v in kw.items()})
    return env


def _git_mode(work, lines, env, *extra):
    """Run `--mode git` the way scripts/pre-push does: ref updates on stdin."""
    return subprocess.run([sys.executable, _GATE, "--mode", "git", "--remote", "origin",
                           *extra],
                          input="".join(line + "\n" for line in lines),
                          capture_output=True, text=True, cwd=str(work), env=env,
                          timeout=180)


def _push_cmd(work, cmd, env):
    """Run `--mode hook` the way the PreToolUse adapter does."""
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash",
                          "cwd": str(work), "tool_input": {"command": cmd}})
    return subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                          capture_output=True, text=True, cwd=str(work), env=env,
                          timeout=180)


def _trace(tmp_path):
    p = tmp_path / "stub.trace"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _rid(work, remote="origin"):
    return rg_store.remote_url(str(work), remote)


def _bypasses():
    path = rg_store.store_root() / "bypasses.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _reviewed_feat(repo, tmp_path, env):
    """Review a two-commit feat branch once; return its recorded tip."""
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    _commit(repo, "f1.txt")
    tip = _commit(repo, "f2.txt")
    line = f"refs/heads/feat {tip} refs/heads/feat {ZERO}"
    proc = _git_mode(repo, [line], env)
    assert proc.returncode == 0, proc.stderr
    rec = rg_store.load_verdict(_rid(repo), tip)
    assert rec is not None and rec["verdict"] == "pass"
    assert len(_trace(tmp_path)) == 1
    return tip


# --- tree-diff carry-forward ---------------------------------------------------

def test_identical_tree_amend_carries_the_verdict_forward(repo, tmp_path):
    """A content-identical amend (rebase/reorder share the tree) reuses the
    recorded pass: the verdict is carried to the new tip, announced as carried
    forward, and the reviewer is NOT invoked again."""
    tip = _reviewed_feat(repo, tmp_path, _env(tmp_path))

    # Amend with identical content: new SHA, same tree (GIT_COMMITTER_DATE
    # makes the rewrite deterministic).
    env_git = scrub_git_trace(dict(os.environ))
    env_git["GIT_COMMITTER_DATE"] = "2026-11-14T12:00:00 +0000"
    _git(["commit", "-q", "--amend", "--no-edit"], cwd=repo, env=env_git)
    new_tip = _sha(repo, "HEAD")
    assert new_tip != tip
    assert _sha(repo, "HEAD^{tree}") == _sha(repo, f"{tip}^{{tree}}")

    proc = _git_mode(repo, [f"refs/heads/feat {new_tip} refs/heads/feat {ZERO}"],
                     _env(tmp_path))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "carried forward (identical tree)" in out, out
    assert len(_trace(tmp_path)) == 1, "an identical-tree push was re-reviewed"
    # Tip-bound: the carried verdict is re-recorded against the NEW tip.
    carried = rg_store.load_verdict(_rid(repo), new_tip)
    assert carried is not None and carried["verdict"] == "pass"
    assert carried["tip"] == new_tip


def test_rebase_onto_advanced_upstream_carries_the_verdict(repo, tmp_path):
    """A rebase of feat onto an ADVANCED upstream rewrites the SHAs AND the tip
    tree (the upstream commits joined the tree), but the pushed range's patch
    identity still equals the reviewed range's: the recorded pass carries
    forward and the reviewer is NOT invoked again (spec story 7's first named
    case -- the tip-tree fast path alone cannot vouch for this push)."""
    tip = _reviewed_feat(repo, tmp_path, _env(tmp_path))

    # Upstream advances: a new commit on main, really pushed so origin's
    # tracking ref holds it, then feat is rebased on top.
    _git(["switch", "-q", "main"], cwd=repo)
    _commit(repo, "m2.txt")
    _git(["push", "-q", "origin", "main"], cwd=repo)
    _git(["switch", "-q", "feat"], cwd=repo)
    _git(["rebase", "-q", "main"], cwd=repo)
    new_tip = _sha(repo, "HEAD")
    assert new_tip != tip
    assert _sha(repo, "HEAD^{tree}") != _sha(repo, f"{tip}^{{tree}}"), \
        "the rebase moved the tree -- tree equality cannot vouch for this push"

    proc = _git_mode(repo, [f"refs/heads/feat {new_tip} refs/heads/feat {ZERO}"],
                     _env(tmp_path))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert len(_trace(tmp_path)) == 1, \
        "a patch-identical rebase onto an advanced upstream was re-reviewed"
    assert "carried forward (identical patch-ids)" in out, out
    # Tip-bound: the carried verdict is re-recorded against the NEW tip.
    carried = rg_store.load_verdict(_rid(repo), new_tip)
    assert carried is not None and carried["verdict"] == "pass"
    assert carried["tip"] == new_tip


def test_content_changed_amend_runs_a_full_review(repo, tmp_path):
    """An amend whose tree differs -- the incident shape ("fixed the wording"
    sweeping in a feature) -- can never ride the old verdict: a fresh review."""
    _reviewed_feat(repo, tmp_path, _env(tmp_path))

    (repo / "swept-in.txt").write_text("a whole feature\n")
    _git(["add", "."], cwd=repo)
    _git(["commit", "-q", "--amend", "--no-edit"], cwd=repo)
    new_tip = _sha(repo, "HEAD")

    proc = _git_mode(repo, [f"refs/heads/feat {new_tip} refs/heads/feat {ZERO}"],
                     _env(tmp_path))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "carried forward" not in out
    assert len(_trace(tmp_path)) == 2, "a content-changed amend skipped review"


# --- delete-push short-circuit --------------------------------------------------

def test_delete_push_invalidates_the_refs_verdicts(repo, tmp_path):
    """`git push origin :feat` retires the ref: every verdict recorded for
    (remote, ref) is invalidated immediately, announced, and nothing is
    reviewed (a deletion carries no commits)."""
    tip = _reviewed_feat(repo, tmp_path, _env(tmp_path))

    line = f"refs/heads/feat {ZERO} refs/heads/feat {tip}"
    proc = _git_mode(repo, [line], _env(tmp_path))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "invalidated" in out, out
    assert rg_store.load_verdict(_rid(repo), tip) is None, \
        "a verdict survived the deletion of its ref"
    assert len(_trace(tmp_path)) == 1, "a delete-push invoked the reviewer"


def test_delete_push_via_hook_mode_invalidates_too(repo, tmp_path):
    """The PreToolUse parser's `git push origin --delete feat` shape takes the
    same short-circuit: verdicts for the deleted ref are dropped."""
    tip = _reviewed_feat(repo, tmp_path, _env(tmp_path))

    proc = _push_cmd(repo, "git push origin --delete feat", _env(tmp_path))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    decision, reason = parse_pretooluse(proc.stdout)
    assert decision != "deny", out + reason
    assert rg_store.load_verdict(_rid(repo), tip) is None
    assert len(_trace(tmp_path)) == 1


# --- destination-remote scoping --------------------------------------------------

def test_a_verdict_on_origin_never_satisfies_a_fork_push(repo, tmp_path):
    """Story 17: trust is scoped to the remote the review serves. The very
    tip origin reviewed must be reviewed again for a push to fork, and the
    fork verdict is keyed to the fork remote only."""
    fork = tmp_path / "fork.git"
    _git(["init", "--bare", "-b", "main", str(fork)], cwd=tmp_path)
    _git(["remote", "add", "fork", str(fork)], cwd=repo)
    tip = _reviewed_feat(repo, tmp_path, _env(tmp_path))
    origin_rec = rg_store.load_verdict(_rid(repo), tip)

    line = f"refs/heads/feat {tip} refs/heads/feat {ZERO}"
    env = _env(tmp_path)
    proc = _git_mode(repo, [line], env, )  # same tip, destination: origin (control)
    assert proc.returncode == 0, proc.stderr
    assert len(_trace(tmp_path)) == 1, "origin's own verdict failed its re-push"

    proc_fork = subprocess.run(
        [sys.executable, _GATE, "--mode", "git", "--remote", "fork"],
        input=line + "\n", capture_output=True, text=True, cwd=str(repo), env=env,
        timeout=180)
    out = proc_fork.stdout + proc_fork.stderr
    assert proc_fork.returncode == 0, out
    assert "carried forward" not in out and "already-reviewed" not in out
    assert len(_trace(tmp_path)) == 2, \
        "origin's verdict satisfied a push to fork without a review"
    fork_rec = rg_store.load_verdict(_rid(repo, "fork"), tip)
    assert fork_rec is not None and fork_rec["remote"] == _rid(repo, "fork")
    # origin's record is untouched and still keyed to origin.
    assert rg_store.load_verdict(_rid(repo), tip) is not None
    assert origin_rec["remote"] == _rid(repo) != fork_rec["remote"]


# --- audited bypass (git mode) ---------------------------------------------------

def test_blocking_verdict_fail_open_is_recorded_and_loud(repo, tmp_path):
    """OCR_FAIL_OPEN=1 yields to a BLOCKING verdict in git mode only through
    the audited escape: the bypass lands in the ledger with mechanism, remote,
    ref and tip, and a loud stderr announcement precedes the allow."""
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    tip = _commit(repo, "f1.txt")
    line = f"refs/heads/feat {tip} refs/heads/feat {ZERO}"

    blocked = _git_mode(repo, [line], _env(tmp_path, STUB_VERDICT="block"))
    assert blocked.returncode == 1, blocked.stderr
    assert _bypasses() == []

    env = _env(tmp_path, STUB_VERDICT="block", OCR_FAIL_OPEN="1")
    proc = _git_mode(repo, [line], env)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert len(_trace(tmp_path)) >= 1
    entries = [b for b in _bypasses() if b["mechanism"] == "OCR_FAIL_OPEN"]
    assert entries, "a fail-open bypass went unrecorded"
    entry = entries[-1]
    assert entry["remote"] == _rid(repo)
    assert entry["ref"] == "refs/heads/feat"
    assert entry["tip"] == tip
    assert "BYPASS" in proc.stderr.upper(), proc.stderr


import importlib.util as _ilu  # pg8 fix-round: _mr_diff_facts brace-rename regression

_rg_spec = _ilu.spec_from_file_location("review_gate_pg4fix", _GATE)
_rg = _ilu.module_from_spec(_rg_spec)
_rg_spec.loader.exec_module(_rg)


def test_mr_diff_facts_parses_numstat_brace_renames(tmp_path):
    """`docs/{old.md => new.md}` must resolve to `docs/new-adrs.md`-style new
    paths; the brace form previously leaked a stray `}` suffix so a docs rename
    misclassified as code could false-block a docs-only MR."""
    work = tmp_path / "work"
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    _config(work)
    docs = work / "docs"
    docs.mkdir()
    (docs / "old-adrs.md").write_text("x\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-m", "base"], cwd=work)
    base = _git(["rev-parse", "HEAD"], cwd=work)
    _git(["mv", "docs/old-adrs.md", "docs/new-adrs.md"], cwd=work)
    _git(["commit", "-m", "rename"], cwd=work)
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    paths, _ = _rg._mr_diff_facts(str(work), base, tip)
    assert paths == ["docs/new-adrs.md"], paths



def test_mr_diff_facts_keeps_literal_braces_in_long_form_names(tmp_path):
    """Gate self-review round 2: 'vet/a{1}.md => web/b{2}.txt' (long form,
    literal braces) must stay intact — the naive '{'-in-path detector
    corrupted it to 'ab{2.txt'."""
    work = tmp_path / "work"
    for d in ("vet", "web"):
        (work / d).mkdir(parents=True)
    _git(["init", "-b", "main", str(work)], cwd=work)
    _config(work)
    (work / "vet" / "a{1}.md").write_text("x\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-m", "base"], cwd=work)
    base = _git(["rev-parse", "HEAD"], cwd=work)
    _git(["mv", "vet/a{1}.md", "web/b{2}.txt"], cwd=work)
    _git(["commit", "-m", "rename"], cwd=work)
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    paths, _ = _rg._mr_diff_facts(str(work), base, tip)
    assert paths == ["web/b{2}.txt"], paths


def test_mr_diff_facts_brace_form_with_braces_in_the_suffix(tmp_path):
    """`{docs => web}/a{1}.md` (literal braces after the close) must resolve
    to `web/a{1}.md`."""
    work = tmp_path / "work"
    for d in ("docs", "web"):
        (work / d).mkdir(parents=True)
    _git(["init", "-b", "main", str(work)], cwd=work)
    _config(work)
    (work / "docs" / "a{1}.md").write_text("x\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-m", "base"], cwd=work)
    base = _git(["rev-parse", "HEAD"], cwd=work)
    _git(["mv", "docs/a{1}.md", "web/a{1}.md"], cwd=work)
    _git(["commit", "-m", "rename"], cwd=work)
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    paths, _ = _rg._mr_diff_facts(str(work), base, tip)
    assert paths == ["web/a{1}.md"], paths



def test_mr_diff_facts_survives_embedded_tab_in_path(tmp_path):
    """Round-4 self-review: a legal POSIX tab inside a non-rename path must
    not truncate it (split on the first two tabs only)."""
    work = tmp_path / "work"
    _git(["init", "-b", "main", str(work)], cwd=work)
    _config(work)
    (work / "docs").mkdir()
    (work / "docs" / "a\tb.md").write_text("x\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-m", "add tab-named file"], cwd=work)
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    paths, _ = _rg._mr_diff_facts(str(work), None, tip)
    assert paths == ["docs/a\tb.md"], paths
