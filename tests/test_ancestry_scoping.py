"""Ancestry scoping of ledger records (PR5 of stacked-branch-review-base).

A ledger record stores `source_tip`, the tip whose review wrote it, and
`source_ref`, the ref that push targeted. A delta base is skipped only when that
tip is not an ancestor of the tip being reviewed AND the two pushes targeted
different known refs: priors from a sibling branch describe code this tip does
not contain, while an amend + force push to the SAME ref is the same line of
work. An exact record for the same blob (CARRY) is content-addressed and stays
unscoped.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")

sys.path.insert(0, _SCRIPTS)
_spec = importlib.util.spec_from_file_location("review_gate_anc", _GATE)
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)

FP = "a" * 64
REF_A, REF_B = "refs/heads/A", "refs/heads/B"
BIG = "".join(f"v{i} = {i}\n" for i in range(200))


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def _commit(work, text, msg):
    (work / "x.py").write_text(text)
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", msg], cwd=work)
    return _git(["rev-parse", "HEAD"], cwd=work), _git(["rev-parse", "HEAD:x.py"], cwd=work)


class Repo:
    """main (base) with x.py; branch A edits line 7; branch B (a sibling off main)
    edits line 9; branch C continues A with another edit."""

    def __init__(self, tmp_path):
        self.work = work = tmp_path / "repo"
        work.mkdir()
        _git(["init", "-b", "main", str(work)], cwd=tmp_path)
        _git(["config", "user.email", "t@t.com"], cwd=work)
        _git(["config", "user.name", "t"], cwd=work)
        _git(["config", "commit.gpgsign", "false"], cwd=work)
        self.base, _ = _commit(work, BIG, "base")
        _git(["checkout", "-q", "-b", "A"], cwd=work)
        self.a_tip, self.a_blob = _commit(work, BIG.replace("v7 = 7", "v7 = 70"), "a")
        _git(["checkout", "-q", "-b", "C"], cwd=work)
        self.c_tip, self.c_blob = _commit(work, BIG.replace("v7 = 7", "v7 = 70").replace("v8 = 8", "v8 = 80"), "c")
        _git(["checkout", "-q", "main"], cwd=work)
        _git(["checkout", "-q", "-b", "B"], cwd=work)
        self.b_tip, self.b_blob = _commit(work, BIG.replace("v9 = 9", "v9 = 90"), "b")
        self.common = str(tmp_path / "common")

    def entry(self, tip):
        entries, _ = review_gate._collect_diff_entries(str(self.work), self.base, tip)
        return next(e for e in entries if e["path"] == "x.py")

    def write(self, blob, source_tip, findings=None, source_ref=REF_A):
        e = self.entry(self.a_tip)
        key = review_gate._record_key(e["path"], "", e["status"], e["old_oid"])
        review_gate._write_ledger_record(
            self.common, FP, key, blob, "x.py", "", e["status"], e["old_oid"],
            findings or [], 0, "r1", source_tip=source_tip, source_ref=source_ref)
        return key

    def plan(self, tip, push_ref=REF_B):
        plan, _ = review_gate._plan_review(str(self.work), self.base, tip, self.common, FP,
                                           push_ref=push_ref)
        return next(p for p in plan if p["entry"]["path"] == "x.py")


_FINDING = {"path": "x.py", "severity": "high", "content": "bad", "existing_code": "v7 = 70",
            "start_line": 8, "end_line": 8}


def test_priors_from_a_non_ancestor_branch_are_not_replayed(tmp_path):
    r = Repo(tmp_path)
    r.write(r.a_blob, r.a_tip, [_FINDING])
    # B does not contain A's tip: A's record must not be B's delta base.
    item = r.plan(r.b_tip)
    assert item["mode"] == "full" and item["miss_reason"] == "no_record"
    assert item["record"] is None
    # C builds on A: the very same record is a valid delta base.
    item = r.plan(r.c_tip)
    assert item["mode"] == "delta" and item["from_oid"] == r.a_blob
    assert item["record"]["source_tip"] == r.a_tip


def test_writer_stores_source_tip_and_ref_only_when_given(tmp_path):
    r = Repo(tmp_path)
    key = r.write(r.a_blob, r.a_tip)
    rec = review_gate._read_ledger_record(
        review_gate._record_path(r.common, FP, key, r.a_blob), FP, key, r.a_blob)
    assert rec["source_tip"] == r.a_tip and rec["source_ref"] == REF_A
    key = r.write(r.b_blob, "", source_ref="")
    rec = review_gate._read_ledger_record(
        review_gate._record_path(r.common, FP, key, r.b_blob), FP, key, r.b_blob)
    assert "source_tip" not in rec and "source_ref" not in rec


def test_an_amended_force_push_to_the_same_ref_keeps_its_delta_base(tmp_path):
    r = Repo(tmp_path)
    r.write(r.a_blob, r.a_tip, [_FINDING])
    # A' replaces A on the SAME branch (amend + force push): A's tip is no longer
    # an ancestor, but it is the same line of work, so its record stays the base.
    _git(["checkout", "-q", "A"], cwd=r.work)
    _git(["reset", "-q", "--hard", r.base], cwd=r.work)
    a2_tip, a2_blob = _commit(
        r.work, BIG.replace("v7 = 7", "v7 = 70").replace("v8 = 8", "v8 = 80"), "a amended")
    anc = subprocess.run(["git", "merge-base", "--is-ancestor", r.a_tip, a2_tip],
                         cwd=r.work).returncode
    assert anc != 0
    item = r.plan(a2_tip, push_ref=REF_A)
    assert item["mode"] == "delta" and item["from_oid"] == r.a_blob
    assert item["record"]["source_ref"] == REF_A
    # The same rewrite pushed to a DIFFERENT ref is a stranger: skipped.
    item = r.plan(a2_tip, push_ref=REF_B)
    assert item["mode"] == "full" and item["record"] is None


def test_unknown_ref_on_either_side_stays_eligible(tmp_path):
    r = Repo(tmp_path)
    r.write(r.a_blob, r.a_tip, [_FINDING])
    item = r.plan(r.b_tip, push_ref="")  # current push ref unknown
    assert item["miss_reason"] != "no_record" and item["from_oid"] == r.a_blob


def test_record_with_unknown_ref_stays_eligible(tmp_path):
    r = Repo(tmp_path)
    r.write(r.a_blob, r.a_tip, [_FINDING], source_ref="")  # pushed from a mode that knew no ref
    item = r.plan(r.b_tip, push_ref=REF_B)
    assert item["miss_reason"] != "no_record" and item["from_oid"] == r.a_blob


def test_identical_blob_carry_still_works_across_branches(tmp_path):
    r = Repo(tmp_path)
    # D: another sibling of A whose x.py ends up byte-identical to A's.
    _git(["checkout", "-q", "main"], cwd=r.work)
    _git(["checkout", "-q", "-b", "D"], cwd=r.work)
    d_tip, d_blob = _commit(r.work, BIG.replace("v7 = 7", "v7 = 70"), "d")
    assert d_blob == r.a_blob and d_tip != r.a_tip
    r.write(r.a_blob, r.a_tip, [_FINDING])
    item = r.plan(d_tip)  # A's tip is not an ancestor of D's, yet the blob is the same
    assert item["mode"] == "carry"
    assert item["record"]["findings"][0]["content"] == "bad"


def test_legacy_record_without_source_tip_stays_eligible(tmp_path):
    r = Repo(tmp_path)
    key = r.write(r.a_blob, r.a_tip, [_FINDING])
    rpath = review_gate._record_path(r.common, FP, key, r.a_blob)
    data = json.loads(Path(rpath).read_text(encoding="utf-8"))
    del data["source_tip"]  # as written before PR5
    Path(rpath).write_text(json.dumps(data), encoding="utf-8")
    item = r.plan(r.b_tip)  # unscoped, even on a branch that does not contain A
    # (the cost rule may turn this small edit into a full review, but the record
    # is still the base: it is kept, not dropped as a stranger's)
    assert item["miss_reason"] != "no_record"
    assert item["record"] is not None and item["from_oid"] == r.a_blob


def test_source_tip_object_missing_locally_is_skipped(tmp_path):
    r = Repo(tmp_path)
    r.write(r.a_blob, "f" * 40, [_FINDING])
    item = r.plan(r.c_tip)
    assert item["mode"] == "full" and item["record"] is None


def test_ancestry_is_checked_once_per_distinct_source_tip(tmp_path, monkeypatch):
    r = Repo(tmp_path)
    r.write(r.a_blob, r.a_tip)
    calls = []
    real = review_gate._git

    def spy(args, cwd=None):
        if "--is-ancestor" in args:
            calls.append(args)
        return real(args, cwd=cwd)

    monkeypatch.setattr(review_gate, "_git", spy)
    e = r.entry(r.c_tip)
    key = review_gate._record_key(e["path"], "", e["status"], e["old_oid"])
    cache = {}
    for _ in range(3):
        rec, _ = review_gate._find_delta_record(
            r.common, FP, key, r.c_blob, tip=r.c_tip, root=str(r.work), ancestry_cache=cache,
            ref=REF_B)
        assert rec is not None
    assert len(calls) == 1


def test_truncated_record_is_still_never_a_delta_base(tmp_path):
    r = Repo(tmp_path)
    e = r.entry(r.a_tip)
    key = review_gate._record_key(e["path"], "", e["status"], e["old_oid"])
    review_gate._write_ledger_record(
        r.common, FP, key, r.a_blob, "x.py", "", e["status"], e["old_oid"], [], 0, "r1",
        truncated=True, source_tip=r.a_tip)
    assert r.plan(r.c_tip)["mode"] == "full"


def test_a_non_string_source_tip_is_skipped_not_a_crash():
    # A hand-edited or corrupt record must never crash planning: it is simply
    # not an ancestor, so the record is skipped (over-review, not a replay).
    cache = {}
    for bad in (["abc"], {"a": 1}, 7, None, ""):
        assert review_gate._tip_is_ancestor(".", bad, "a" * 40, cache) is False
    assert cache == {}
