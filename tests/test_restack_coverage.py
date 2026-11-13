"""Content-keyed review coverage for restack-heavy workflows (0.14.0, PR1).

A restack rewrites every commit SHA but no content; the gate used to pay the
full 17-26 minute re-review for that, because nothing about "already reviewed"
survived a SHA rewrite. These tests pin the new join:

  - a pure restack (same content, new SHAs) joins the recorded pass with
    zero chunk calls (telemetry), via the tip-tree fast path or the
    patch-id union;
  - a restack plus new work reviews only the NEW work (restack-b: the
    incremental range is the uncovered commits; restack-a's interim
    all-or-nothing full-range re-review is superseded);
  - pre-fix (protocol_version 1) records join by SHA-subset only, never via
    patch-ids, and are never backfilled;
  - commits with no usable patch-id (binary changes) are SHA-covered and
    never poison the rest of the coverage set;
  - `OCR_COVERAGE_V1=1` restores the pre-change SHA-only behaviour end to end.

Real git throughout, in the style of tests/test_stacked_base.py.
"""
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

_spec = importlib.util.spec_from_file_location("review_gate_restack", _GATE)
rg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rg)

ZERO = "0" * 40
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
              "STUB_SLEEP", "STUB_VERDICT", "STUB_TRACE"):
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


def _write_record(work, tip, record):
    path = rg_store.verdict_path(_rid(work), tip)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


# --- the pure restack joins the recorded pass -------------------------------

def test_pure_restack_same_content_joins_recorded_pass(repo, tmp_path):
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    _commit(repo, "f1.txt")
    old_tip = _commit(repo, "f2.txt")
    env = _env(tmp_path)

    decision, out = _run(repo, "git push -u origin feat", env)
    assert decision == "pass", out
    rec = _await_record(repo, old_tip)
    assert rec["verdict"] == "pass"
    assert rec["protocol_version"] == 2
    assert len(rec["patch_ids"]) == 2        # every commit is content-keyed
    assert rec["sha_only"] == []
    assert rec["tip_tree"] == _sha(repo, "feat^{tree}")
    assert rec["branch"] == "feat"

    _restack(repo)
    tip = _sha(repo, "feat")
    assert tip != old_tip                              # every SHA rewritten...
    assert _sha(repo, "feat^{tree}") == rec["tip_tree"]  # ...same content

    decision, out = _run(repo, "git push -u origin feat", env)
    assert decision == "pass", out
    assert "already-reviewed" in out                    # the join note, not silence
    assert len(_trace(tmp_path)) == 1, "a pure restack was re-reviewed"
    # The join adopted the verdict for the new tip, so a re-push joins on the
    # record too (marker + store), with no review either.
    decision, out = _run(repo, "git push -u origin feat", env)
    assert decision == "pass", out
    assert len(_trace(tmp_path)) == 1


def test_restack_plus_new_commit_still_reviews(repo, tmp_path):
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    _commit(repo, "f1.txt")
    _commit(repo, "f2.txt")
    env = _env(tmp_path)

    decision, out = _run(repo, "git push -u origin feat", env)
    assert decision == "pass", out
    _await_record(repo, _sha(repo, "feat"))

    _restack(repo)
    restacked = _sha(repo, "feat")   # every SHA rewritten, content identical
    _commit(repo, "f3.txt")                # new work on top of the restack
    tip = _sha(repo, "feat")
    decision, out = _run(repo, "git push -u origin feat", env)
    assert decision == "pass", out
    traces = _trace(tmp_path)
    assert len(traces) == 2, "restack + new commit must re-review the new work"
    # restack-b: only the new commit is reviewed -- the covered commits' content
    # is never re-paid (restack-a's interim full-range expectation is superseded
    # by PR2's incremental semantics).
    assert traces[1]["range"] == f"{restacked}..{tip}"   # the new commit only


# --- legacy records: SHA-subset only, never patch-ids ------------------------

def test_legacy_record_joins_by_sha_only(repo, tmp_path):
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    c1 = _commit(repo, "f1.txt")
    c2 = _commit(repo, "f2.txt")
    fork = _sha(repo, "main")
    env = _env(tmp_path)

    # A pre-fix record: protocol_version 1, SHA coverage only. It carries no
    # patch_ids and is never backfilled, so identical CONTENT can never join it.
    _write_record(repo, c2, {
        "remote": _rid(repo), "tip": c2, "base": fork, "verdict": "pass",
        "blocked": False, "reasons": "", "commits": [c1, c2],
        "capped": False, "note": "", "ts": 0.0, "protocol_version": 1,
    })

    # Never via patch-ids: a pure restack of the very content the legacy record
    # reviewed still re-reviews, because the record cannot vouch for content.
    _restack(repo)
    new_tip = _sha(repo, "feat")
    decision, out = _run(repo, "git push -u origin feat", env)
    assert decision == "pass", out
    assert len(_trace(tmp_path)) == 1, "a legacy record joined a restack by content"
    rg_store.remove_verdict(_rid(repo), new_tip)   # back to the legacy record alone
    assert "patch_ids" not in _record(repo, c2)    # never backfilled

    # By SHA only: pushing an ancestor tip whose commits the legacy record
    # already covers joins with no review at all.
    _git(["switch", "-q", "-c", "older", c1], cwd=repo)
    decision, out = _run(repo, "git push -u origin older", env)
    assert decision == "pass", out
    assert "already-reviewed" in out
    assert len(_trace(tmp_path)) == 1              # still only the review above


# --- the extended record shape ----------------------------------------------

def test_record_with_patch_ids_round_trips():
    rg_store.record_verdict(
        "origin", "t" * 40, "b" * 40, "pass", False, "", ["c2", "c1"],
        coverage={"tip_tree": "a" * 40, "patch_ids": ["p2", "p1", "p2"],
                  "sha_only": ["c2", "c1", "c1"]},
        branch="feat")
    rec = rg_store.load_verdict("origin", "t" * 40)
    assert rec["protocol_version"] == 2
    assert rec["tip_tree"] == "a" * 40
    assert rec["patch_ids"] == ["p1", "p2"]          # sorted, deduped
    assert rec["sha_only"] == ["c1", "c2"]
    assert rec["branch"] == "feat"
    assert rec["commits"] == ["c2", "c1"]            # the commit set is untouched

    # A v2 record missing the fields the join depends on is corrupt by
    # definition, exactly like a v1 record missing `commits`.
    rg_store.verdict_path("origin", "u" * 40).parent.mkdir(parents=True, exist_ok=True)
    rg_store.verdict_path("origin", "u" * 40).write_text(json.dumps({
        "remote": "origin", "tip": "u" * 40, "verdict": "pass",
        "commits": [], "protocol_version": 2,
    }), encoding="utf-8")
    with pytest.raises(rg_store.StoreError):
        rg_store.load_verdict("origin", "u" * 40)


# --- binary commits: SHA coverage, never poison ------------------------------

def test_binary_only_commit_falls_back_to_sha_coverage(repo):
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    c1 = _commit(repo, "f1.txt")
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02\xff" * 64)   # git sees NULs: binary
    _git(["add", "."], cwd=repo)
    _git(["commit", "-q", "-m", "binary asset"], cwd=repo)
    c2 = _sha(repo, "HEAD")
    c3 = _commit(repo, "f3.txt")
    fork = _sha(repo, "main")

    cs = rg.compute_coverage_set(str(repo), fork, c3)
    assert cs is not None
    assert set(cs.patch_ids) == {c1, c3}     # the text commits are content-keyed
    assert cs.sha_only == [c2]               # the binary one is SHA-keyed
    assert cs.tip_tree == _sha(repo, "feat^{tree}")

    # SHA coverage still covers the range: a record holding those SHAs joins
    # a same-SHA push of the binary commit itself (and never via a patch-id).
    rid = _rid(repo)
    rg_store.record_verdict(
        rid, c3, fork, "pass", False, "", cs.commits,
        coverage={"tip_tree": cs.tip_tree,
                  "patch_ids": sorted(set(cs.patch_ids.values())),
                  "sha_only": sorted(cs.sha_only)},
        branch="feat")
    note = rg._coverage_join(str(repo), rid, fork, c2, "feat")
    assert note, "a SHA-covered binary commit did not join"
    adopted = rg_store.load_verdict(rid, c2)
    assert adopted["sha_only"] == [c2] and c2 not in adopted["patch_ids"]

    # And it never poisons the range: a restack rewrites the binary commit's
    # SHA too, so (only its SHA covering it) the join refuses -- a full-range
    # review, never a skip and never an error.
    _restack(repo)
    assert not rg._coverage_join(str(repo), rid, fork, _sha(repo, "feat"), "feat")


# --- the tree-hash fast path -------------------------------------------------

def test_tree_hash_fast_path_skips_patch_id_computation(repo, monkeypatch):
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    _commit(repo, "f1.txt")
    _commit(repo, "f2.txt")
    fork = _sha(repo, "main")
    tip = _sha(repo, "feat")
    tree = _sha(repo, "feat^{tree}")
    rid = _rid(repo)

    # A recorded pass from a previous push of this very tree (its own tip is
    # some restacked ancestor; the tree is what matches).
    rg_store.record_verdict(
        rid, "f" * 40, fork, "pass", False, "", [tip],
        coverage={"tip_tree": tree, "patch_ids": [], "sha_only": []},
        branch="feat")

    def forbidden(*args, **kwargs):
        raise AssertionError("the tree-hash fast path must not compute patch-ids")

    monkeypatch.setattr(rg, "compute_coverage_set", forbidden)
    note = rg._coverage_join(str(repo), rid, fork, tip, "feat")
    assert note and "already-reviewed" in note
    rec = rg_store.load_verdict(rid, tip)      # the join adopted the verdict
    assert rec["verdict"] == "pass" and rec["tip_tree"] == tree


# --- the rollback switch ------------------------------------------------------

def test_ocr_coverage_v1_restores_sha_only_behaviour(repo, tmp_path):
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    _commit(repo, "f1.txt")
    _commit(repo, "f2.txt")
    env = _env(tmp_path)
    env["OCR_COVERAGE_V1"] = "1"

    decision, out = _run(repo, "git push -u origin feat", env)
    assert decision == "pass", out
    rec = _await_record(repo, _sha(repo, "feat"))
    assert rec["protocol_version"] == 1        # the pre-change record shape
    assert "patch_ids" not in rec

    _restack(repo)
    decision, out = _run(repo, "git push -u origin feat", env)
    assert decision == "pass", out
    assert len(_trace(tmp_path)) == 2, "OCR_COVERAGE_V1=1 must re-review a restack"
