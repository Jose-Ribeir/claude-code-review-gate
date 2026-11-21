# ruff: noqa: F811  (the `repo` fixture is imported, then used as a parameter)
"""R06: a push that went through the fail-open switch is recorded as unverified.

The record is the gate's memory of what it did not check: (base, tip, time,
reason) in the review-gate store, written only when OCR_FAIL_OPEN let a push
through. The next push on the branch prints a warning that an earlier base was
never reviewed and names it. The warning is information only -- it never
changes the exit code -- and an unverified entry never counts as covered when
the next push joins earlier runs.

Everything goes through the gate's git mode as the installed pre-push hook runs
it, with the stub reviewer. Nothing is monkeypatched.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts"))
sys.path.insert(0, _HERE)

import rg_store  # noqa: E402
from test_pg4_carry_forward import (  # noqa: E402,F401,F811
    ZERO, _bypasses, _commit, _env, _git, _git_mode, _rid, _sha, _trace, repo)


def _feat(repo):
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    return _sha(repo, "main")


def _line(tip, old=ZERO):
    return f"refs/heads/feat {tip} refs/heads/feat {old}"


def _unverified(repo):
    return rg_store.list_unverified(_rid(repo), "refs/heads/feat")


def _bypass_with_blocked_review(repo, tmp_path):
    """Push a one-commit feat branch whose review blocks, through the fail-open
    switch. Returns (base, tip)."""
    base = _feat(repo)
    tip = _commit(repo, "f1.txt")
    proc = _git_mode(repo, [_line(tip)], _env(tmp_path, STUB_VERDICT="block", OCR_FAIL_OPEN="1"))
    assert proc.returncode == 0, proc.stderr
    return base, tip


def test_a_fail_open_push_writes_an_unverified_entry(repo, tmp_path):
    base, tip = _bypass_with_blocked_review(repo, tmp_path)
    entries = _unverified(repo)
    assert len(entries) == 1, entries
    e = entries[0]
    assert e["base"] == base and e["tip"] == tip
    assert e["remote"] == _rid(repo) and e["ref"] == "feat"
    assert e["reason"] == "blocked_verdict"
    assert isinstance(e["ts"], (int, float)) and e["ts"] > 0
    assert [b for b in _bypasses() if b["mechanism"] == "OCR_FAIL_OPEN"]   # audit log kept


def test_a_normal_push_writes_no_unverified_entry(repo, tmp_path):
    _feat(repo)
    tip = _commit(repo, "f1.txt")
    # Passing review, no switch.
    ok = _git_mode(repo, [_line(tip)], _env(tmp_path))
    assert ok.returncode == 0, ok.stderr
    assert _unverified(repo) == []
    # A refused (blocked) push with no switch writes none either.
    tip2 = _commit(repo, "f2.txt")
    refused = _git_mode(repo, [_line(tip2, tip)], _env(tmp_path, STUB_VERDICT="block"))
    assert refused.returncode == 1, refused.stderr
    assert _unverified(repo) == []
    assert "never reviewed" not in ok.stderr + refused.stderr


def test_the_next_push_warns_naming_the_base_and_keeps_its_exit_code(repo, tmp_path):
    base, tip = _bypass_with_blocked_review(repo, tmp_path)
    tip2 = _commit(repo, "f2.txt")
    # Passing next push: warns, exits 0.
    ok = _git_mode(repo, [_line(tip2, tip)], _env(tmp_path))
    assert ok.returncode == 0, ok.stderr
    assert "never reviewed" in ok.stderr and base in ok.stderr, ok.stderr
    assert "WARNING" in ok.stderr
    # A blocking next push: the warning is there and the block still blocks.
    tip3 = _commit(repo, "f3.txt")
    blocked = _git_mode(repo, [_line(tip3, tip2)], _env(tmp_path, STUB_VERDICT="block"))
    assert blocked.returncode == 1, blocked.stderr
    assert "never reviewed" in blocked.stderr and base in blocked.stderr, blocked.stderr


def test_the_bypassing_push_itself_does_not_warn_about_itself(repo, tmp_path):
    _feat(repo)
    tip = _commit(repo, "f1.txt")
    env = _env(tmp_path, STUB_VERDICT="block", OCR_FAIL_OPEN="1")
    first = _git_mode(repo, [_line(tip)], env)
    again = _git_mode(repo, [_line(tip)], env)
    assert first.returncode == 0 and again.returncode == 0, again.stderr
    assert "never reviewed" not in first.stderr + again.stderr
    assert len(_unverified(repo)) == 1


def test_another_branch_is_not_warned_about_it(repo, tmp_path):
    _bypass_with_blocked_review(repo, tmp_path)
    _git(["switch", "-q", "main"], cwd=repo)
    _git(["switch", "-q", "-c", "other"], cwd=repo)
    tip = _commit(repo, "o1.txt")
    proc = _git_mode(repo, [f"refs/heads/other {tip} refs/heads/other {ZERO}"], _env(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert "never reviewed" not in proc.stderr


def test_an_unverified_entry_never_counts_as_covered_in_the_join(repo, tmp_path):
    """The bypassed push's commit was never reviewed (the reviewer failed). The
    next push must review it, not treat the unverified entry as covering it.
    (The remote never received the bypassed tip -- the forge rejected it -- so
    the next push starts from the same base again: the join has the unverified
    entry in reach and must not use it.)"""
    base = _feat(repo)
    tip = _commit(repo, "f1.txt")
    bypass = _git_mode(repo, [_line(tip)], _env(tmp_path, STUB_VERDICT="exit1", OCR_FAIL_OPEN="1"))
    assert bypass.returncode == 0, bypass.stderr
    assert len(_unverified(repo)) == 1
    assert rg_store.load_verdict(_rid(repo), tip) is None   # no verdict was ever recorded
    reviews_before = len(_trace(tmp_path))

    tip2 = _commit(repo, "f2.txt")
    proc = _git_mode(repo, [_line(tip2)], _env(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert "never reviewed" in proc.stderr and base in proc.stderr
    ranges = [t["range"] for t in _trace(tmp_path)[reviews_before:] if not t.get("resolve_file")]
    assert ranges == [f"{base}..{tip2}"], ranges   # the whole branch, bypassed commit included
