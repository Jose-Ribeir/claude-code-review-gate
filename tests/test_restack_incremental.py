"""Incremental review range + verdict combination (0.14.0, PR2 = restack-b).

A restack plus genuinely new work is the NORMAL push: restack-a's join is
all-or-nothing there, so it re-paid the whole-branch review. These tests pin
the incremental behaviour:

  - the review range shrinks to the uncovered commits only -- the covered
    majority is never re-reviewed (asserted through the stub trace: one new
    review call, whose range and manifest cover only the uncovered commits);
  - a quiet incremental run clears NOTHING: every prior finding still goes
    through re-anchor/resolver against the new tip, so a finding-free
    incremental review leaves the prior findings in the verdict;
  - an `unverified` prior (no evidence it was fixed, flagged code no longer
    locatable) stays blocking after the join;
  - a commit without a usable patch-id (binary changes are SHA-keyed) enters
    the incremental range INDIVIDUALLY when uncovered -- it never widens the
    range to the covered commits around it.

Real git throughout, in the style of tests/test_restack_coverage.py.
"""
import glob
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_STUB = os.path.join(_HERE, "stub_reviewer.py")

sys.path.insert(0, _SCRIPTS)
import rg_store  # noqa: E402
from hook_output import parse_pretooluse  # noqa: E402
from conftest import scrub_git_trace  # noqa: E402

_spec = importlib.util.spec_from_file_location("review_gate_restack_b", _GATE)
rg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rg)

SHA40 = re.compile(r"[0-9a-f]{40}")


def _git(args, cwd, check=True, env=None):
    return subprocess.run(["git"] + args, cwd=str(cwd), env=env, capture_output=True,
                          text=True, check=check).stdout.strip()


def _config(work):
    _git(["config", "user.email", "t@example.com"], cwd=work)
    _git(["config", "user.name", "t"], cwd=work)
    _git(["config", "commit.gpgsign", "false"], cwd=work)


@pytest.fixture
def repo(tmp_path):
    """A clone with two files on a pushed main (`a.txt`, `chat.txt`) and a bare origin."""
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
    (work / "chat.txt").write_text("chat\n")
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


def _env(tmp_path):
    env = scrub_git_trace(dict(os.environ))
    for k in ("OCR_IN_REVIEW", "OCR_FAIL_OPEN", "OCR_ADVISORY", "OCR_FORCE_REVIEW",
              "OCR_LEGACY_RANGE", "OCR_INLINE_BUDGET", "OCR_COVERAGE_V1",
              "STUB_SLEEP", "STUB_VERDICT", "STUB_TRACE", "STUB_FINDINGS_FOR"):
        env.pop(k, None)
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "gate-data")
    env["OCR_REVIEWER_CMD"] = f'"{sys.executable}" "{_STUB}"'
    env["STUB_TRACE"] = str(tmp_path / "stub.trace")
    env["OCR_INLINE_BUDGET"] = "30"
    return env


def _run(work, cmd, env):
    """Run the hook-mode gate over `cmd`; return (decision, stderr)."""
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash",
                          "cwd": str(work), "tool_input": {"command": cmd}})
    proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                          capture_output=True, text=True, cwd=str(work), env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    decision, reason = parse_pretooluse(proc.stdout)
    return decision, reason + "\n" + proc.stderr


def _trace(tmp_path):
    p = tmp_path / "stub.trace"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _restack(work):
    """Rewrite every commit after main with identical content (new SHAs, same
    patch-ids and tip tree), the way a rebase onto a moved base does. The fixed
    committer date makes the rewrite deterministic."""
    env = scrub_git_trace(dict(os.environ))
    env["GIT_COMMITTER_DATE"] = "2026-11-14T12:00:00 +0000"
    _git(["rebase", "--force-rebase", "main"], cwd=work, env=env)


def _rid(work):
    return rg_store.remote_url(str(work), "origin")


def _record(work, tip):
    return rg_store.load_verdict(_rid(work), tip)


def _await_record(work, tip, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            rec = _record(work, tip)
        except rg_store.StoreError:
            rec = None
        if rec is not None:
            return rec
        time.sleep(0.2)
    raise AssertionError(f"verdict record for {tip} never landed")


def _telemetry(work):
    """The run records the supervisor wrote for this repository."""
    out = []
    for path in sorted(glob.glob(os.path.join(str(work), ".git",
                                              "review-gate-telemetry", "*.jsonl"))):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    return out


def _manifest_paths(trace_line):
    return (trace_line.get("manifest") or {}).get("paths") or []


# --- the incremental range is only the uncovered work ------------------------

def test_restack_plus_one_new_commit_reviews_only_that_commit(repo, tmp_path):
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    _commit(repo, "f1.py")
    _commit(repo, "f2.py")
    env = _env(tmp_path)

    decision, out = _run(repo, "git push -u origin feat", env)
    assert decision == "pass", out
    _await_record(repo, _sha(repo, "feat"))

    _restack(repo)
    restacked = _sha(repo, "feat")        # every SHA rewritten, content identical
    _commit(repo, "f3.py")               # the only genuinely new work
    tip = _sha(repo, "feat")

    decision, out = _run(repo, "git push -u origin feat", env)
    assert decision == "pass", out
    traces = _trace(tmp_path)
    assert len(traces) == 2, "the covered commits were re-reviewed"
    assert traces[1]["range"] == f"{restacked}..{tip}"   # only the new commit
    assert _manifest_paths(traces[1]) == ["f3.py"]
    # What the reviewer was handed is only f3's diff -- not f1's or f2's
    # content, which the prior verdict already vouched for.
    assert set((traces[1].get("item_files") or {})) == {"review:f3.py"}

    # The record still covers the whole push (the union: prior coverage plus
    # the incremental review), so the next PURE restack joins with no review.
    rec = _await_record(repo, tip)
    assert rec["verdict"] == "pass"
    assert len(rec["patch_ids"]) == 3 and rec["sha_only"] == []
    _restack(repo)
    decision, out = _run(repo, "git push -u origin feat", env)
    assert decision == "pass", out
    assert "already-reviewed" in out
    assert len(_trace(tmp_path)) == 2, "the union coverage did not join a pure restack"


# --- a quiet incremental run clears nothing -----------------------------------

def test_quiet_incremental_run_never_clears_prior_findings(repo, tmp_path):
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    _commit(repo, "f1.py")
    _commit(repo, "f2.py")
    env = _env(tmp_path)
    env["STUB_FINDINGS_FOR"] = json.dumps(
        {"f2.py": {"severity": "medium", "content": "prior medium finding"}})

    decision, out = _run(repo, "git push -u origin feat", env)
    assert decision == "pass", out                 # medium: warn, never block
    assert "prior medium finding" in out
    _await_record(repo, _sha(repo, "feat"))
    assert "prior medium finding" in _record(repo, _sha(repo, "feat"))["reasons"]

    env.pop("STUB_FINDINGS_FOR")                   # the incremental run is quiet
    _restack(repo)
    _commit(repo, "f3.py")
    tip = _sha(repo, "feat")

    decision, out = _run(repo, "git push -u origin feat", env)
    assert decision == "pass", out
    traces = _trace(tmp_path)
    assert len(traces) == 2, "the quiet incremental run re-reviewed covered content"
    assert _manifest_paths(traces[1]) == ["f3.py"]
    # The prior finding was re-anchored and replayed, not cleared: only the
    # resolver can clear a prior, and nothing here resolved it.
    assert "prior medium finding" in out
    rec = _await_record(repo, tip)
    assert "prior medium finding" in rec["reasons"]
    assert rec["verdict"] == "warn"
    # And the pass marker carries it too, so a re-push within the TTL still
    # replays it instead of allowing silently.
    markers = list((repo / ".git").glob("scr-push-reviewed-*"))
    assert markers, "no pass marker for the incremental run's verdict"
    assert any("prior medium finding" in p.read_text() for p in markers)


# --- an unverified prior stays blocking --------------------------------------

def test_unverified_prior_stays_blocking_after_join(repo, tmp_path):
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    _commit(repo, "f1.py")
    env = _env(tmp_path)
    env["STUB_FINDINGS_FOR"] = json.dumps(
        {"f1.py": {"severity": "high", "content": "prior high finding"}})

    decision, out = _run(repo, "git push -u origin feat", env)
    assert decision == "deny", out
    _await_record(repo, _sha(repo, "feat"))       # the block is recorded (and owed)

    env.pop("STUB_FINDINGS_FOR")
    _restack(repo)
    (repo / "f1.py").write_text("f1\nchanged by the new commit\n")
    _git(["add", "."], cwd=repo)
    _git(["commit", "-q", "-m", "touch f1"], cwd=repo)
    tip = _sha(repo, "feat")

    decision, out = _run(repo, "git push -u origin feat", env)
    assert decision == "deny", out
    traces = _trace(tmp_path)
    assert len(traces) == 2, "the incremental review re-reviewed covered content"
    assert _manifest_paths(traces[1]) == ["f1.py"]
    assert "prior high finding" in out            # the prior is still owed
    # The resolver judged it (default stub: still_present with no evidence the
    # code is at the tip): `unverified`, which stays blocking by design.
    judged = {}
    for rec in _telemetry(repo):
        resolver = rec.get("resolver") or {}
        if resolver.get("judged"):
            judged.update(resolver["judged"])
    assert judged and set(judged.values()) == {"unverified"}
    rec = _await_record(repo, tip)
    assert rec["blocked"] is True
    assert "prior high finding" in rec["reasons"]


# --- a no-patch-id commit enters the range individually ----------------------

def test_no_patchid_commit_reviews_itself_only_not_the_range(repo, tmp_path):
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    _commit(repo, "f1.py")
    # A commit carrying a binary asset has no usable patch-id: it is SHA-keyed
    # (Q14), so a restack rewrites the only key that covered it.
    (repo / "f2.py").write_text("f2\n")
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02\xff" * 64)   # NULs: binary
    _git(["add", "."], cwd=repo)
    _git(["commit", "-q", "-m", "add f2 and a binary asset"], cwd=repo)
    bin_sha = _sha(repo, "HEAD")
    _commit(repo, "f3.py")
    env = _env(tmp_path)

    decision, out = _run(repo, "git push -u origin feat", env)
    assert decision == "pass", out
    rec = _await_record(repo, _sha(repo, "feat"))
    assert rec["sha_only"] == [bin_sha]

    _restack(repo)                        # the binary commit's SHA is rewritten...
    new_bin = _sha(repo, "feat^")         # ...so only IT is uncovered
    assert new_bin != bin_sha
    tip = _sha(repo, "feat")

    decision, out = _run(repo, "git push -u origin feat", env)
    assert decision == "pass", out
    traces = _trace(tmp_path)
    assert len(traces) == 2
    # The uncovered commit enters individually: the range names the commit
    # itself, never the covered commits around it (never a widening span).
    assert traces[1]["range"] == new_bin
    assert _manifest_paths(traces[1]) == ["f2.py"]    # blob.bin is not reviewable
    assert set((traces[1].get("item_files") or {})) == {"review:f2.py"}
    # The reviewer saw the commit's own diff of f2.py -- the add, not the
    # whole branch's change to the file.
    assert "+f2" in (traces[1].get("item_files") or {}).get("review:f2.py", "")

    # The new record covers the whole push again (union), keyed by content
    # wherever a patch-id exists and by SHA for the binary commit.
    rec = _await_record(repo, tip)
    assert rec["verdict"] == "pass"
    assert rec["sha_only"] == [new_bin]
    assert len(rec["patch_ids"]) == 2
