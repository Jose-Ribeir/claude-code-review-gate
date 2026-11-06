"""PR4 of docs/plans/stacked-branch-review-base.md: three ways a finding that was
fixed (or already owed) came back.

* an orphan -- a finding about a file outside the context that was reviewed --
  goes to its OWN file's record, not into every record written;
* the sibling search runs after the resolver, from the priors still owed, at
  their re-anchored lines, and never lands on a line the fix itself wrote;
* a carried finding the reviewer reports again does not print twice.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_async_gate import _big_repo, _git as _agit, _item, _ledger_ctx, _record  # noqa: E402
from test_impact_gate import (  # noqa: E402
    _EVAL, _LIB, _commit, _env, _push, _repo, _review_manifest, _state,
)
import test_impact_gate  # noqa: E402

review_gate = test_impact_gate.review_gate


def _finding(path, content="owed", **kw):
    return dict({"path": path, "severity": "high", "content": content,
                 "existing_code": "x = 0", "start_line": 1, "end_line": 1,
                 "confidence": 0.9}, **kw)


def _ok(findings):
    return {"status": "success", "findings": findings, "warnings": []}


def _write_record_for(common, fp, e, findings=()):
    key = review_gate._record_key(e["path"], "", e["status"], e["old_oid"])
    review_gate._write_ledger_record(common, fp, key, e["new_oid"], e["path"], "", e["status"],
                                     e["old_oid"], list(findings), 0, "run-A")


# ---------------------------------------------------------------------------
# Orphans
# ---------------------------------------------------------------------------

def test_an_orphan_goes_only_to_its_own_files_record(tmp_path):
    repo = _big_repo(tmp_path, n_files=4)
    common, fp, _, tip, allowed = _ledger_ctx(repo)
    by_path = {e["path"]: e for e in allowed}
    a, b, c = by_path["mod0.py"], by_path["mod1.py"], by_path["mod2.py"]
    _write_record_for(common, fp, c)               # c was reviewed before: it has a record
    items = [_item(a), _item(b), _item(c, "carry", record=_record(common, fp, c))]

    review_gate._write_run_records(_ok([_finding("mod2.py", "about c")]), items[:2],
                                   common, fp, "r1", str(repo), tip, own_items=items)

    assert [f["content"] for f in _record(common, fp, c)["findings"]] == ["about c"]
    assert _record(common, fp, a)["findings"] == []
    assert _record(common, fp, b)["findings"] == []


def test_an_orphan_with_no_record_of_its_own_is_kept_on_the_records_written(tmp_path):
    repo = _big_repo(tmp_path, n_files=4)
    common, fp, _, tip, allowed = _ledger_ctx(repo)
    by_path = {e["path"]: e for e in allowed}
    a, b, later = by_path["mod0.py"], by_path["mod1.py"], by_path["mod3.py"]
    items = [_item(a), _item(b), _item(later)]     # `later` is reviewed in another chunk

    review_gate._write_run_records(
        _ok([_finding("mod3.py", "about later"), _finding("elsewhere.py", "outside")]),
        items[:2], common, fp, "r1", str(repo), tip, own_items=items)

    for e in (a, b):
        assert sorted(f["content"] for f in _record(common, fp, e)["findings"]) == [
            "about later", "outside"]
    assert _record(common, fp, later) is None      # nothing was written for it, nothing lost


def test_an_orphan_about_a_file_active_in_another_chunk_stays_on_this_chunks_records(tmp_path):
    """`later` has a (truncated) record at its new_oid, but another chunk of this run
    reviews it and rewrites that record from its own findings only: a finding put
    there would be lost. It stays on the records written here instead."""
    repo = _big_repo(tmp_path, n_files=4)
    common, fp, _, tip, allowed = _ledger_ctx(repo)
    by_path = {e["path"]: e for e in allowed}
    a, later = by_path["mod0.py"], by_path["mod3.py"]
    key = review_gate._record_key(later["path"], "", later["status"], later["old_oid"])
    review_gate._write_ledger_record(common, fp, key, later["new_oid"], later["path"], "",
                                     later["status"], later["old_oid"], [], 0, "run-A",
                                     truncated=True)
    items = [_item(a), _item(later)]

    review_gate._write_run_records(_ok([_finding("mod3.py", "about later")]), items[:1],
                                   common, fp, "r1", str(repo), tip, own_items=items)

    assert [f["content"] for f in _record(common, fp, a)["findings"]] == ["about later"]
    assert _record(common, fp, later)["findings"] == []     # not touched from here


# ---------------------------------------------------------------------------
# Siblings
# ---------------------------------------------------------------------------

_FINDING = {"x.py": {"severity": "high", "content": "eval of request input",
                     "existing_code": "return eval(req.args['expr'])"}}


def test_sibling_search_does_not_re_raise_the_fixed_line(tmp_path):
    """The prior was raised at stale lines (the stub says 1-2); the code now
    sits at line 32. Re-anchored first, the search treats that line as the
    finding's own place and the reviewer is not handed it as a "sibling"."""
    work = _repo(tmp_path, {"x.py": "a = 0\n", "y.py": "b = 0\n"})
    tip1 = _commit(work, {"x.py": _LIB + _EVAL})
    decision, _ = _push(work, _env(tmp_path, "t1.trace", STUB_FINDINGS_FOR=json.dumps(_FINDING)),
                        cmd="git push origin main")
    assert decision == "deny"
    _state(work, tip1)

    tip2 = _commit(work, {"x.py": _LIB + _EVAL + "# touched\n"}, amend=True)
    decision, reason = _push(work, _env(tmp_path, "t2.trace", STUB_CONFIRM_SIBLINGS="1"))
    _state(work, tip2)
    assert _review_manifest(tmp_path, "t2.trace").get("known_defects") in (None, [])
    assert "(same defect as an earlier finding)" not in reason, reason
    assert decision == "deny" and "eval of request input" in reason, reason  # still owed, once


def test_a_resolved_finding_is_not_searched_for_siblings(tmp_path):
    work = _repo(tmp_path, {"x.py": "a = 0\n", "y.py": "b = 0\n"})
    tip1 = _commit(work, {"x.py": _LIB + _EVAL, "y.py": _LIB + _EVAL.replace("handle", "other")})
    decision, _ = _push(work, _env(tmp_path, "t1.trace", STUB_FINDINGS_FOR=json.dumps(_FINDING)),
                        cmd="git push origin main")
    assert decision == "deny"
    _state(work, tip1)

    tip2 = _commit(work, {"x.py": _LIB + "def handle(req):\n    return safe(req)\n"}, amend=True)
    fid = review_gate._finding_id({"path": "x.py", **_FINDING["x.py"]})
    resolve = json.dumps({fid: {"status": "resolved", "evidence_path": "x.py",
                                "evidence_quote": "return safe(req)"}})
    _push(work, _env(tmp_path, "t2.trace", STUB_RESOLVE=resolve, STUB_CONFIRM_SIBLINGS="1"))
    _state(work, tip2)
    assert _review_manifest(tmp_path, "t2.trace").get("known_defects") in (None, [])


def test_sibling_search_skips_lines_the_fix_added(tmp_path):
    work = _repo(tmp_path, {"x.py": _LIB + _EVAL, "y.py": _LIB + _EVAL.replace("handle", "other")})
    blob1 = _agit(["rev-parse", "HEAD:x.py"], cwd=work)
    # The "fix" wraps the call and writes a line that still carries the signature.
    _commit(work, {"x.py": _LIB + "def handle(req):\n    if ok(req):\n"
                           "        return eval(req.args['expr'])\n    return None\n"})
    tip = _agit(["rev-parse", "HEAD"], cwd=work)
    prior = dict(_FINDING["x.py"], path="x.py", start_line=900, end_line=900)

    def hits(p):
        got = review_gate._known_defects(str(work), tip, [p], {"x.py", "y.py"})[0]
        return {(d["path"], d["line"]) for d in got}

    assert ("x.py", 33) in hits(prior)                       # stale lines: the old search
    assert hits(dict(prior, target_oid=blob1)) == {("y.py", 32)}


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

def test_carried_findings_do_not_double_print(tmp_path):
    work = _repo(tmp_path, {"a.py": "a = 0\n", "b.py": "b = 0\n"})
    tip1 = _commit(work, {"a.py": "a = 1\n", "b.py": "b = 1\ndef bad(): pass\n"})
    finding = {"b.py": {"severity": "high", "content": "bad function in b",
                        "existing_code": "def bad(): pass"}}
    decision, _ = _push(work, _env(tmp_path, "t1.trace", STUB_FINDINGS_FOR=json.dumps(finding)),
                        cmd="git push origin main")
    assert decision == "deny"
    _state(work, tip1)

    # b.py is carried; the reviewer, reading through a.py, reports it again.
    tip2 = _commit(work, {"a.py": "a = 2\n"}, amend=True)
    decision, reason = _push(work, _env(tmp_path, "t2.trace", STUB_FINDINGS_FOR=json.dumps(finding),
                                        STUB_FINDINGS_OUTSIDE="1"))
    _state(work, tip2)
    assert decision == "deny"
    assert reason.count("bad function in b") == 1, reason


def test_findings_without_line_numbers_match_on_path_severity_and_content():
    head = "unsafe eval of the request expression reaches the interpreter unchecked"
    a = {"path": "x.py", "severity": "high", "content": head.replace(" of ", "  of ")}
    b = dict(a, content=head + ", again")
    assert review_gate._findings_similar(a, b)
    assert review_gate._findings_similar(b, a)
    assert not review_gate._findings_similar(a, dict(b, severity="medium"))
    assert not review_gate._findings_similar(a, dict(b, path="y.py"))
    assert not review_gate._findings_similar(a, dict(b, content="a different defect entirely"))
    # With lines on both sides they must still overlap.
    c = dict(b, start_line=40, end_line=41)
    assert review_gate._findings_similar(c, dict(c, content=c["content"] + " more"))
    assert not review_gate._findings_similar(dict(c, start_line=1, end_line=2), c)


def test_a_finding_with_lines_is_never_the_same_as_one_without():
    """Content alone (the first 60 characters) only identifies a finding when neither
    side has a place: a new defect at line 210 must not vanish into a carried finding
    that happens to open with the same words."""
    head = "Missing null check on the result of parse, the caller dereferences it"
    bare = {"path": "x.py", "severity": "medium", "content": head}
    placed = dict(bare, start_line=210, end_line=210)
    assert not review_gate._findings_similar(bare, placed)
    assert not review_gate._findings_similar(placed, bare)
    assert len(review_gate._merge_near_dup_findings([bare, placed])) == 2
    assert len(review_gate._merge_near_dup_findings([bare, dict(bare)])) == 1


def test_a_re_reported_resolved_prior_keeps_its_resolution_withheld_even_when_held_matches():
    """P (provisionally resolved) and S (still owed) open alike and have no lines. The
    reviewer's report matches S, so it is dropped as already held -- but it re-reports P
    just the same, and P's resolution must not be written."""
    head = "the request expression reaches eval without a check on the caller side"
    base = {"path": "x.py", "severity": "high", "existing_code": "eval(e)"}
    p = dict(base, content=head + " (first site)")
    s = dict(base, content=head + " (second site)")
    nf = dict(base, content=head, provenance="new")
    prov = [({"id": "pid", "finding": p}, {"status": "resolved"}, "x.py", "blob")]

    deduped, override = review_gate._dedup_new_findings([nf], [s], prov)

    assert deduped == []                    # S already prints it once
    assert override == {"pid"}
    deduped, override = review_gate._dedup_new_findings([nf], [], prov)
    assert deduped == [nf] and override == {"pid"}
