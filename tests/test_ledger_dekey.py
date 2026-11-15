"""Ledger de-keying: base_oid is metadata, not identity (PR3 of
restack-content-keyed-coverage).

A restack rewrites the base blob (`old_oid`) of the range even when the
branch's own content is byte-identical; keying every record by the base made
each file's judgement miss and be re-derived from scratch. Identity is
(path, old_path, status) plus the head blob oid in the record's filename and
the criteria fingerprint of the directory it lives in: an unchanged head blob
reuses its record regardless of base, and delta records are selected among
same-path records by the existing source_tip ancestry rule, not by a
base-keyed prefix glob.
"""
import importlib.util
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")

sys.path.insert(0, _HERE)
sys.path.insert(0, _SCRIPTS)
_spec = importlib.util.spec_from_file_location("review_gate_dekey", _GATE)
rg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rg)

import test_segment_gate as sg  # noqa: E402  (full-run harness + helpers)
import test_async_gate as ag  # noqa: E402

FP = "d" * 64
FEAT = "refs/heads/feat"
OTHER = "refs/heads/other"


@pytest.fixture(autouse=True)
def _fresh_run_counter():
    """sg.run numbers supervisor runs with a module-level counter that is
    shared across every test module importing it; earlier modules leave it
    far past zero, and our e2e test asserts the record's run_id is "run1".
    Start each of our tests from a fresh counter (restored afterwards)."""
    before = sg._RUN["n"]
    sg._RUN["n"] = 0
    yield
    sg._RUN["n"] = before

V1 = "".join(f"v{i} = {i}\n" for i in range(30))          # the original base content
V1_PRIME = "".join(f"w{i} = {i}\n" for i in range(30))        # what a moved base holds
V2 = V1.replace("v7 = 7", "v7 = 70")                       # the branch's reviewed content
V3 = V2.replace("v8 = 8", "v8 = 80")                       # the branch's changed content
V4 = V1.replace("v9 = 9", "v9 = 90")                      # a sibling branch's content

_FINDING = {"path": "x.py", "severity": "high", "content": "v7 is bad",
            "existing_code": "v7 = 70", "start_line": 8, "end_line": 8}


def _git(args, cwd):
    import subprocess
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


class RestackRepo:
    """`main` holds x.py; `feat` holds the branch under review; `other` is a
    sibling off main. main can advance (the range's base blob moves) while the
    branch's work is re-applied on top of it -- a restack."""

    def __init__(self, tmp_path):
        self.work = work = tmp_path / "repo"
        work.mkdir()
        _git(["init", "-b", "main", str(work)], cwd=tmp_path)
        _git(["config", "user.email", "t@t.com"], cwd=work)
        _git(["config", "user.name", "t"], cwd=work)
        _git(["config", "commit.gpgsign", "false"], cwd=work)
        self.main = self._commit(V1, "base")
        _git(["checkout", "-q", "-b", "feat"], cwd=work)
        self.common = str(tmp_path / "common")

    def _commit(self, text, msg):
        (self.work / "x.py").write_text(text)
        _git(["add", "."], cwd=self.work)
        _git(["commit", "-q", "-m", msg], cwd=self.work)
        return _git(["rev-parse", "HEAD"], cwd=self.work)

    def branch_commit(self, text, msg="work"):
        return self._commit(text, msg)

    def advance_main(self, text):
        _git(["checkout", "-q", "main"], cwd=self.work)
        self.main2 = self._commit(text, "main moved")
        _git(["checkout", "-q", "feat"], cwd=self.work)

    def reapply(self, text, msg="restacked"):
        """The branch's work re-applied on the moved base: a new tip whose
        history does not contain the old one."""
        _git(["reset", "-q", "--hard", self.main2], cwd=self.work)
        return self._commit(text, msg)

    def sibling_commit(self, text):
        _git(["checkout", "-q", "-b", "other", self.main], cwd=self.work)
        tip = self._commit(text, "sibling")
        _git(["checkout", "-q", "feat"], cwd=self.work)
        return tip

    def entry(self, base, tip):
        entries, _ = rg._collect_diff_entries(str(self.work), base, tip)
        return next(e for e in entries if e["path"] == "x.py")

    def write_record(self, base, tip_for_entry, head_blob, source_tip,
                     source_ref=FEAT, findings=None):
        e = self.entry(base, tip_for_entry)
        key = rg._record_key(e["path"], "", e["status"], e["old_oid"])
        rg._write_ledger_record(
            self.common, FP, key, head_blob, "x.py", "", e["status"], e["old_oid"],
            findings or [], 0, "r1", source_tip=source_tip, source_ref=source_ref)
        return key

    def plan(self, base, tip, push_ref=FEAT):
        plan, _ = rg._plan_review(str(self.work), base, tip, self.common, FP,
                                 push_ref=push_ref)
        return next(p for p in plan if p["entry"]["path"] == "x.py")


# --- an unchanged head blob reuses its record across a base rewrite -----------------------------

def test_ledger_record_survives_restack_with_same_blobs(tmp_path):
    r = RestackRepo(tmp_path)
    t1 = r.branch_commit(V2, "feat work")
    v2_blob = _git(["rev-parse", "HEAD:x.py"], cwd=r.work)
    r.write_record(r.main, t1, v2_blob, t1, findings=[_FINDING])
    # the restack: the base blob moves (main advanced), the branch's own
    # content is re-applied byte-identically, under a new tip
    r.advance_main(V1_PRIME)
    t2 = r.reapply(V2)
    e = r.entry(r.main2, t2)
    item = r.plan(r.main2, t2)
    # the record written under the OLD base is the one reused
    assert item["mode"] == "carry"
    assert item["record"] is not None and item["record"]["head_oid"] == v2_blob
    assert e["old_oid"] != item["record"]["base_oid"]  # reuse crossed the base change
    # and the judgement on it survives with it
    assert item["record"]["findings"][0]["content"] == "v7 is bad"


def test_delta_record_selected_by_source_tip_ancestry_not_base_prefix(tmp_path):
    r = RestackRepo(tmp_path)
    t1 = r.branch_commit(V2, "feat work")
    v2_blob = _git(["rev-parse", "HEAD:x.py"], cwd=r.work)
    r.write_record(r.main, t1, v2_blob, t1, findings=[_FINDING])
    # a NEWER record for the same path from a sibling branch the current tip
    # does not contain: a stranger, even though it was reviewed last. The blob
    # must come from the sibling tip itself: HEAD is back on feat here, so
    # HEAD:x.py is v2_blob and the stranger would clobber the t1 record
    # (same key, same head blob, same file) instead of being a second one.
    s_tip = r.sibling_commit(V4)
    v4_blob = _git(["rev-parse", f"{s_tip}:x.py"], cwd=r.work)
    assert v4_blob != v2_blob
    r.write_record(r.main, s_tip, v4_blob, s_tip, source_ref=OTHER)
    # the restack: the base moves, and the branch gains one more edit on top
    # of its already-reviewed tip (still an ancestor of what is pushed)
    r.advance_main(V1_PRIME)
    t2 = r.branch_commit(V3, "one more edit")
    v3_blob = _git(["rev-parse", "HEAD:x.py"], cwd=r.work)
    e = r.entry(r.main2, t2)
    item = r.plan(r.main2, t2)
    assert e["old_oid"] != _git(["rev-parse", f"{r.main}:x.py"], cwd=r.work)
    # the ancestor record is the delta base -- not the newer stranger, and
    # not anything keyed to the old base prefix
    assert item["mode"] == "delta"
    assert item["from_oid"] == v2_blob
    assert item["record"]["source_tip"] == t1
    assert item["record"]["head_oid"] == v2_blob


def test_different_blobs_still_miss_after_restack(tmp_path):
    r = RestackRepo(tmp_path)
    t1 = r.branch_commit(V2, "feat work")
    v2_blob = _git(["rev-parse", "HEAD:x.py"], cwd=r.work)
    r.write_record(r.main, t1, v2_blob, t1, findings=[_FINDING])
    # the restack changes the file itself: same path, different head blob
    r.advance_main(V1_PRIME)
    t2 = r.reapply(V3)
    v3_blob = _git(["rev-parse", "HEAD:x.py"], cwd=r.work)
    assert v3_blob != v2_blob
    item = r.plan(r.main2, t2)
    # the changed content is never carried as already-reviewed
    assert item["mode"] != "carry"
    assert item["mode"] in ("full", "delta")
    if item["mode"] == "delta":
        # a delta re-reviews the change against the prior blob; it does not
        # reuse the prior record's judgement as-is
        assert item["from_oid"] == v2_blob


# --- end to end: a restacked push re-derives nothing --------------------------------------------

def test_a_restacked_push_with_identical_blobs_makes_zero_review_calls(tmp_path, gate_env):
    work, tip1 = sg.push_big(tmp_path, {"small.py": "x = 1\n"},
                             {"keep.py": "x = 0\n", "small.py": "# v1\n"})
    st, calls = sg.run(work, tip1, tmp_path, gate_env)
    assert st["state"] == "done" and len(sg.reviews(calls)) == 1
    rec1 = sg.file_record(work, tip1, "small.py")
    assert rec1 is not None and rec1["run_id"] == "run1"
    # the restack: the base blob of small.py moves (main is rewritten under
    # the same history), and the branch gains a new (empty) tip
    ag._git(["checkout", "-q", "-B", "other", "HEAD~1"], cwd=work)
    sg.commit(work, {"small.py": "# v2 of the base\nz = 1\n"})
    ag._git(["push", "-q", "-f", "origin", "other:main"], cwd=work)
    ag._git(["checkout", "-q", "main"], cwd=work)
    ag._git(["fetch", "-q", "origin"], cwd=work)
    tip2 = ag._git(["commit", "--allow-empty", "-q", "-m", "restacked"], cwd=work)
    st2, calls2 = sg.run(work, tip2, tmp_path, gate_env)
    assert st2["state"] == "done"
    assert sg.reviews(calls2) == []                 # nothing re-derived
    rec2 = sg.file_record(work, tip2, "small.py")
    assert rec2 is not None and rec2["run_id"] == "run1"   # the SAME record survived
