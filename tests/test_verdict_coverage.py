"""A4: strict verdict coverage -- no vacuous authorizations.

Two holes are pinned here:
  - the join path (`_verdict_backing`) used to skip the `covers()` check
    entirely when `push_range` was empty, so a record for (remote, tip)
    authorized a push whose commit set could not even be enumerated;
  - `rg_store.covers` treated a capped (truncated) record as covering
    ANYTHING, so a push whose extra commits fell below the cap was waved
    through on the strength of a set that never listed them.

Fix under test: an empty range DENIES (no coverage); a capped record covers
exactly the commits it lists; a record that does not cover the push's range
is dropped and the review restarts (never a replay).
"""
import importlib.util
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")

sys.path.insert(0, _SCRIPTS)
import rg_store  # noqa: E402

_spec = importlib.util.spec_from_file_location("review_gate_coverage", _GATE)
rg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rg)


def _git(args, cwd, check=True):
    return subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, text=True,
                          check=check).stdout.strip()


def _repo(tmp_path):
    """main: c1 <- c2 <- c3, all unpushed (no remote needed for these tests)."""
    work = tmp_path / "work"
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    _git(["config", "user.email", "t@example.com"], cwd=work)
    _git(["config", "user.name", "t"], cwd=work)
    _git(["config", "commit.gpgsign", "false"], cwd=work)
    shas = []
    for i in range(3):
        (work / f"f{i}.txt").write_text(f"{i}\n")
        _git(["add", "."], cwd=work)
        _git(["commit", "-q", "-m", f"c{i+1}"], cwd=work)
        shas.append(_git(["rev-parse", "HEAD"], cwd=work))
    return work, shas  # shas[0]=c1 (oldest) .. shas[2]=c3 (tip)


# --- rg_store.covers: a capped record covers only the commits it lists ------

def test_covers_requires_subset_even_when_capped():
    rec = {"commits": ["c1"], "capped": True}
    # The old behavior: capped => True for ANY commits. A4: only c1.
    assert rg_store.covers(rec, ["c1"]) is True
    assert rg_store.covers(rec, ["c1", "c2"]) is False
    assert rg_store.covers(rec, []) is True


def test_covers_of_an_uncapped_record_is_the_subset_check():
    rec = {"commits": ["c1", "c2"], "capped": False}
    assert rg_store.covers(rec, ["c2"]) is True
    assert rg_store.covers(rec, ["c2", "c3"]) is False
    assert rg_store.covers(None, []) is False


def test_remove_verdict_drops_the_record():
    rg_store.record_verdict("origin", "t" * 40, "b" * 40, "pass", False, "", ["c1"])
    assert rg_store.load_verdict("origin", "t" * 40) is not None
    rg_store.remove_verdict("origin", "t" * 40)
    assert rg_store.load_verdict("origin", "t" * 40) is None
    rg_store.remove_verdict("origin", "t" * 40)  # absent: no error


# --- _record_range_within: the capped blind spot ----------------------------

def test_range_within_for_a_capped_record(tmp_path):
    work, (c1, c2, c3) = _repo(tmp_path)
    rec = {"base": c2, "capped": True}
    assert rg._record_range_within(str(work), rec, c2) is True   # same range
    assert rg._record_range_within(str(work), rec, c3) is True   # subset range
    # Wider range: commits below the cap the record cannot vouch for.
    assert rg._record_range_within(str(work), rec, c1) is False
    assert rg._record_range_within(str(work), rec, "") is False  # unbounded push


def test_range_within_of_an_unbounded_record():
    # A record with no base covered everything its tip reaches.
    assert rg._record_range_within("nowhere", {"base": "", "capped": True}, "abc") is True


# --- _verdict_backing: the join path -----------------------------------------

def _backing(work, tip, push_base, push_range, rec_base, rec_commits, capped=False):
    remote = "origin"
    rg_store.record_verdict(remote, tip, rec_base, "pass", False, "", rec_commits)
    if capped:
        rec = rg_store.load_verdict(remote, tip)
        rec["capped"] = True
        rg_store._write_json(rg_store.verdict_path(remote, tip), rec)
    common = rg._git_common_dir(str(work))
    return rg._verdict_backing(common, tip, push_base, remote, str(work), push_range)


def test_empty_push_range_backed_by_a_record_denies(tmp_path):
    work, (c1, c2, c3) = _repo(tmp_path)
    # A record that would cover the tip's commits -- but the push range is
    # EMPTY, so no commit set can be enumerated and the record authorizes
    # nothing. Old behavior: "ok" (vacuous). A4: "uncovered" -> the caller
    # DENIES.
    assert _backing(work, c3, c1, "", c1, [c1, c2, c3]) == "uncovered"


def test_covering_record_with_a_range_is_ok(tmp_path):
    work, (c1, c2, c3) = _repo(tmp_path)
    assert _backing(work, c3, c1, f"{c1}..{c3}", c1, [c1, c2, c3]) == "ok"
    # A narrower push range (newer base) is a subset: still ok.
    assert _backing(work, c3, c2, f"{c2}..{c3}", c1, [c1, c2, c3]) == "ok"


def test_non_covering_record_is_dropped_and_restarts(tmp_path):
    work, (c1, c2, c3) = _repo(tmp_path)
    # The record only vouches for c3 (reviewed as c2..c3); the push sends
    # c1..c3. Old behavior: "uncovered" DENY loop. A4: the record is
    # dropped and the review restarts ("missing").
    assert _backing(work, c3, c1, f"{c1}..{c3}", c2, [c3]) == "missing"
    assert rg_store.load_verdict("origin", c3) is None, "the stale record must be gone"


def test_capped_record_does_not_cover_a_wider_range(tmp_path):
    work, (c1, c2, c3) = _repo(tmp_path)
    # Capped record for c2..c3; the push sends c1..c3. Even though c3 is in
    # the capped set, the range is wider than what was reviewed.
    assert _backing(work, c3, c1, f"{c1}..{c3}", c2, [c3], capped=True) == "missing"
    assert rg_store.load_verdict("origin", c3) is None
