"""A stored resolution stays valid by content, not by the evidence file's blob.

The old rule dropped a resolution as soon as the evidence file's blob moved on,
so an unrelated edit to that file re-sent a fixed finding to the resolver (or
re-blocked the push when the resolver was down). The rule is now: the evidence
quote is still in the evidence file AND the flagged code is still gone from the
finding's file. The exact-blob match stays as the fast path, and a resolution
written without the new fields keeps the exact-blob rule (fail closed).
"""
import importlib.util
import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_STUB = os.path.join(_HERE, "stub_reviewer.py")

sys.path.insert(0, _SCRIPTS)
from hook_output import parse_pretooluse  # noqa: E402
_spec = importlib.util.spec_from_file_location("review_gate_rv", _GATE)
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)

FP = "f" * 16
BAD = "def bad(): pass"
GOOD = "def good(): pass"
_FINDING = {"severity": "high", "content": "bad function in x", "existing_code": BAD}
_FID = review_gate._finding_id({"path": "x.py", **_FINDING})


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def _init(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main", str(path)], cwd=path.parent)
    _git(["config", "user.email", "t@t.com"], cwd=path)
    _git(["config", "user.name", "t"], cwd=path)
    _git(["config", "commit.gpgsign", "false"], cwd=path)
    hooks = path.parent / "no-hooks"
    hooks.mkdir(exist_ok=True)
    _git(["config", "core.hooksPath", str(hooks)], cwd=path)
    return path


def _commit(work, files, msg="c"):
    for name, content in files.items():
        (work / name).write_text(content, encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", msg], cwd=work)
    return _git(["rev-parse", "HEAD"], cwd=work)


def _blob(work, tip, path):
    return _git(["rev-parse", f"{tip}:{path}"], cwd=work)


# ---------------------------------------------------------------------------
# In-process: _find_valid_resolution
# ---------------------------------------------------------------------------

class _Fixed:
    """x.py had BAD at `bad_tip`; `fix_tip` removed it and put GOOD in c.py.
    The resolution is written the way the gate writes it, at fix_tip."""

    def __init__(self, tmp_path):
        self.work = _init(tmp_path / "repo")
        self.common = str(self.work / ".git")
        self.bad_tip = _commit(self.work, {"x.py": f"x = 1\n{BAD}\n", "c.py": "# c\n"})
        self.target_oid = _blob(self.work, self.bad_tip, "x.py")
        self.fix_tip = _commit(self.work, {"x.py": f"x = 1\n{GOOD}\n",
                                           "c.py": f"# c\n{GOOD}\n"})
        self.finding = dict(_FINDING, path="x.py")
        ev_blob = _blob(self.work, self.fix_tip, "c.py")
        review_gate._write_resolution(
            self.common, FP, _FID, self.target_oid, "c.py", ev_blob, GOOD, "r1",
            extra=review_gate._resolution_target_facts(str(self.work), self.fix_tip,
                                                       self.finding),
            review_root=str(self.work))

    def valid(self, tip, blobs=None):
        return review_gate._find_valid_resolution(
            self.common, FP, _FID, self.target_oid, str(self.work), tip, blobs) is not None

    def stored(self):
        d = review_gate._fp_dir(self.common, FP) / "resolutions"
        (p,) = list(d.glob("*.json"))
        return p, json.loads(p.read_text(encoding="utf-8"))


def test_the_resolution_records_what_it_needs_to_be_revalidated(tmp_path):
    fx = _Fixed(tmp_path)
    _, data = fx.stored()
    assert data["target_path"] == "x.py"
    assert data["flagged_code"] == BAD
    assert data["flagged_code_absent"] is True
    assert data["target_blob_oid"] == _blob(fx.work, fx.fix_tip, "x.py")
    assert fx.valid(fx.fix_tip)  # fast path: nothing moved


def test_orphan_cross_file_resolution_survives_a_later_edit_to_the_evidence_file(tmp_path):
    """The fix is evidenced in c.py, not in the finding's own file. A later,
    unrelated edit to c.py changes its blob but not the evidence."""
    fx = _Fixed(tmp_path)
    tip = _commit(fx.work, {"c.py": f"# c, with a header comment\n{GOOD}\nz = 2\n"})
    assert _blob(fx.work, tip, "c.py") != review_gate._blob_oids_at(
        str(fx.work), fx.fix_tip, ["c.py"])["c.py"]
    assert fx.valid(tip)
    # ...and again, with the finding's own file edited around (not reverted).
    tip2 = _commit(fx.work, {"x.py": f"x = 2\n# more\n{GOOD}\n"})
    assert fx.valid(tip2)


def test_resolution_is_invalidated_when_the_flagged_code_reappears(tmp_path):
    """Anti-revert: the evidence file moved on (so the blob fast path misses) and
    the code the finding was about is back in x.py."""
    fx = _Fixed(tmp_path)
    tip = _commit(fx.work, {"x.py": f"x = 1\n{BAD}\n", "c.py": f"# c2\n{GOOD}\n"})
    assert not fx.valid(tip)


def test_flagged_code_reappearing_invalidates_even_with_the_evidence_blob_unchanged(tmp_path):
    fx = _Fixed(tmp_path)
    tip = _commit(fx.work, {"x.py": f"x = 1\n  {BAD}   \n"})  # whitespace-insensitive
    assert not fx.valid(tip)


def test_resolution_is_invalidated_when_the_evidence_quote_is_removed(tmp_path):
    fx = _Fixed(tmp_path)
    tip = _commit(fx.work, {"c.py": "# c, the fix was taken out again\n"})
    assert not fx.valid(tip)


def test_a_deleted_evidence_file_invalidates_the_resolution(tmp_path):
    fx = _Fixed(tmp_path)
    (fx.work / "c.py").unlink()
    _git(["add", "-A", "c.py"], cwd=fx.work)
    _git(["commit", "-q", "-m", "rm"], cwd=fx.work)
    assert not fx.valid(_git(["rev-parse", "HEAD"], cwd=fx.work))


def test_a_deleted_finding_file_keeps_only_the_exact_blob_rule(tmp_path):
    """The finding's file is gone from tip but a blob was recorded: it may have
    moved (a rename), so the absence cannot be verified. Exact-blob only."""
    fx = _Fixed(tmp_path)
    (fx.work / "x.py").unlink()
    _git(["add", "-A", "x.py"], cwd=fx.work)
    _git(["commit", "-q", "-m", "rm"], cwd=fx.work)
    tip = _git(["rev-parse", "HEAD"], cwd=fx.work)
    assert fx.valid(tip)  # evidence blob unchanged: the exact-blob rule holds
    tip2 = _commit(fx.work, {"c.py": f"# c3\n{GOOD}\n"})
    assert not fx.valid(tip2)  # evidence moved on: cannot verify, fail closed


def test_a_renamed_finding_file_does_not_validate_by_content(tmp_path):
    fx = _Fixed(tmp_path)
    _git(["mv", "x.py", "y.py"], cwd=fx.work)
    (fx.work / "y.py").write_text(f"x = 1\n{BAD}\n", encoding="utf-8")
    tip = _commit(fx.work, {"c.py": f"# c4\n{GOOD}\n"})
    assert not fx.valid(tip)


def test_a_file_absent_at_resolution_and_at_tip_stays_valid_by_content(tmp_path):
    """No blob was ever recorded (the file was already gone): nothing to verify."""
    work = _init(tmp_path / "repo")
    common = str(work / ".git")
    t1 = _commit(work, {"x.py": f"{BAD}\n", "c.py": "# c\n"})
    target = _blob(work, t1, "x.py")
    (work / "x.py").unlink()
    _git(["add", "-A", "x.py"], cwd=work)
    _git(["commit", "-q", "-m", "rm"], cwd=work)
    t2 = _commit(work, {"c.py": f"# c\n{GOOD}\n"})
    facts = review_gate._resolution_target_facts(str(work), t2, dict(_FINDING, path="x.py"))
    assert facts["target_blob_oid"] == "" and facts["flagged_code_absent"] is True
    review_gate._write_resolution(common, FP, _FID, target, "c.py", _blob(work, t2, "c.py"),
                                  GOOD, "r", extra=facts, review_root=str(work))
    t3 = _commit(work, {"c.py": f"# c edited\n{GOOD}\n"})
    assert review_gate._find_valid_resolution(common, FP, _FID, target, str(work), t3)


def test_a_resolution_without_the_new_fields_keeps_the_exact_blob_rule(tmp_path):
    """Fail closed: written by an older gate, there is nothing to re-check by content."""
    fx = _Fixed(tmp_path)
    p, data = fx.stored()
    for k in ("target_path", "flagged_code", "flagged_code_absent", "target_blob_oid"):
        data.pop(k)
    p.write_text(json.dumps(data), encoding="utf-8")
    assert fx.valid(fx.fix_tip)
    tip = _commit(fx.work, {"c.py": f"# c, edited\n{GOOD}\n"})
    assert not fx.valid(tip)


def test_a_resolution_with_an_empty_quote_keeps_the_exact_blob_rule(tmp_path):
    fx = _Fixed(tmp_path)
    p, data = fx.stored()
    data["evidence_quote"] = ""
    p.write_text(json.dumps(data), encoding="utf-8")
    tip = _commit(fx.work, {"c.py": f"# c, edited\n{GOOD}\n"})
    assert not fx.valid(tip)


def test_flagged_code_that_survived_the_fix_is_left_to_the_quote_check(tmp_path):
    """A fix evidenced elsewhere while the flagged line stays (a guard in the
    caller): there is no absence to re-check, only the quote."""
    work = _init(tmp_path / "repo")
    common = str(work / ".git")
    t1 = _commit(work, {"x.py": f"{BAD}\n", "c.py": "# c\n"})
    target = _blob(work, t1, "x.py")
    t2 = _commit(work, {"c.py": f"# c\n{GOOD}\n"})
    finding = dict(_FINDING, path="x.py")
    facts = review_gate._resolution_target_facts(str(work), t2, finding)
    assert facts["flagged_code_absent"] is False
    review_gate._write_resolution(common, FP, _FID, target, "c.py",
                                  _blob(work, t2, "c.py"), GOOD, "r", extra=facts,
                                  review_root=str(work))
    t3 = _commit(work, {"c.py": f"# c edited\n{GOOD}\n"})
    def find(tip):
        return review_gate._find_valid_resolution(
            common, FP, _FID, target, str(work), tip)
    assert find(t3) is not None
    t4 = _commit(work, {"c.py": "# c, fix removed\n"})
    assert find(t4) is None


def _rewrite_quote(fx, quote):
    p, data = fx.stored()
    data["evidence_quote"] = quote
    p.write_text(json.dumps(data), encoding="utf-8")


def test_a_short_quote_is_not_enough_to_keep_a_resolution_valid(tmp_path):
    fx = _Fixed(tmp_path)
    _rewrite_quote(fx, "pass")  # still in the edited evidence, but says nothing
    tip = _commit(fx.work, {"c.py": "# c, fix gone\ndef other(): pass\n"})
    assert not fx.valid(tip)


def test_the_quote_must_match_on_token_boundaries(tmp_path):
    fx = _Fixed(tmp_path)
    tip = _commit(fx.work, {"c.py": "# c\ndef good(): password = 1\n"})
    assert not fx.valid(tip)  # "def good(): pass" is a prefix of "...password"
    tip2 = _commit(fx.work, {"c.py": "# c\nundef good(): pass\n"})
    assert not fx.valid(tip2)  # ...and a suffix of "undef good(): pass"


def test_the_quote_still_matches_across_whitespace_changes(tmp_path):
    fx = _Fixed(tmp_path)
    tip = _commit(fx.work, {"c.py": "# c\ndef   good():\n    pass\n"})
    assert fx.valid(tip)


def _stored_one(common):
    d = review_gate._fp_dir(common, FP) / "resolutions"
    (p,) = list(d.glob("*.json"))
    return json.loads(p.read_text(encoding="utf-8"))


def test_a_quote_absent_from_the_evidence_at_write_time_is_not_stored_as_content_rule(tmp_path):
    work = _init(tmp_path / "repo")
    common = str(work / ".git")
    t1 = _commit(work, {"x.py": f"{BAD}\n", "c.py": "# c\n"})
    target = _blob(work, t1, "x.py")
    t2 = _commit(work, {"x.py": f"{GOOD}\n", "c.py": f"# c\n{GOOD}\n"})
    facts = review_gate._resolution_target_facts(str(work), t2, dict(_FINDING, path="x.py"))
    review_gate._write_resolution(common, FP, _FID, target, "c.py", _blob(work, t2, "c.py"),
                                  "def invented(): pass", "r", extra=facts,
                                  review_root=str(work))
    data = _stored_one(common)
    assert "target_path" not in data and "flagged_code" not in data
    # exact-blob rule: valid while the evidence blob is unchanged, not after.
    def find(tip):
        return review_gate._find_valid_resolution(
            common, FP, _FID, target, str(work), tip)
    assert find(t2) is not None
    t3 = _commit(work, {"c.py": f"# c edited\n{GOOD}\ndef invented(): pass\n"})
    assert find(t3) is None


def test_a_too_short_quote_is_not_stored_as_content_rule(tmp_path):
    work = _init(tmp_path / "repo")
    common = str(work / ".git")
    t1 = _commit(work, {"x.py": f"{BAD}\n", "c.py": "# c\n"})
    target = _blob(work, t1, "x.py")
    t2 = _commit(work, {"x.py": f"{GOOD}\n", "c.py": "# c\nreturn\n"})
    facts = review_gate._resolution_target_facts(str(work), t2, dict(_FINDING, path="x.py"))
    review_gate._write_resolution(common, FP, _FID, target, "c.py", _blob(work, t2, "c.py"),
                                  "return", "r", extra=facts, review_root=str(work))
    assert "target_path" not in _stored_one(common)


def test_validity_reads_both_blobs_in_one_cat_file_call(tmp_path, monkeypatch):
    fx = _Fixed(tmp_path)
    tip = _commit(fx.work, {"x.py": f"x = 3\n{GOOD}\n", "c.py": f"# c5\n{GOOD}\n"})
    calls = []
    real = subprocess.run

    def spy(cmd, *a, **kw):
        calls.append(list(cmd))
        return real(cmd, *a, **kw)

    monkeypatch.setattr(review_gate.subprocess, "run", spy)
    assert fx.valid(tip, blobs={})
    batch = [c for c in calls if c[:3] == ["git", "cat-file", "--batch"]]
    assert len(batch) == 1, calls


def test_cat_blob_texts_survives_missing_and_odd_oids(tmp_path):
    work = _init(tmp_path / "repo")
    tip = _commit(work, {"a.txt": "héllo\n", "b.txt": "second\n"})
    a, b = _blob(work, tip, "a.txt"), _blob(work, tip, "b.txt")
    got = review_gate._cat_blob_texts(str(work), [a, "0" * 40, b, "", a])
    assert got == {a: "héllo\n", b: "second\n"}
    assert review_gate._cat_blob_texts(str(work), []) == {}
    assert review_gate._cat_blob_texts("", [a]) == {}


# ---------------------------------------------------------------------------
# In-process: a resolved prior is not re-emitted into the next record
# ---------------------------------------------------------------------------

def _prior_record_item(fx, other=None):
    """A delta item for x.py whose previous record still holds the prior (and,
    optionally, an unrelated one)."""
    tip = fx.fix_tip
    entry = {"path": "x.py", "old_path": "", "status": "M", "old_oid": "0" * 40,
             "new_oid": _blob(fx.work, tip, "x.py"), "lines": 2}
    prior = dict(fx.finding, id=_FID, target_oid=fx.target_oid)
    findings = [prior] + ([other] if other else [])
    prev = {"head_oid": fx.target_oid, "findings": findings, "chain_depth": 0}
    return {"entry": entry, "mode": "delta", "record": prev, "from_oid": fx.target_oid}, tip


def _written(fx, item):
    e = item["entry"]
    key = review_gate._record_key(e["path"], "", e["status"], e["old_oid"])
    rec = review_gate._read_ledger_record(
        review_gate._record_path(fx.common, FP, key, e["new_oid"]), FP, key, e["new_oid"])
    assert rec is not None
    return rec["findings"]


def test_a_resolved_prior_is_not_re_emitted_into_the_next_record(tmp_path):
    fx = _Fixed(tmp_path)
    other = dict(_FINDING, path="x.py", content="a different defect", existing_code="y = 0")
    other["target_oid"] = fx.target_oid
    item, tip = _prior_record_item(fx, other=other)
    result = {"status": "success", "findings": []}
    review_gate._write_run_records(result, [item], fx.common, FP, "r2", str(fx.work), tip)
    kept = _written(fx, item)
    assert [f["content"] for f in kept] == ["a different defect"]


def test_an_unresolved_prior_is_still_carried_into_the_next_record(tmp_path):
    fx = _Fixed(tmp_path)
    item, tip = _prior_record_item(fx)
    # The flagged code is back: the resolution no longer holds, the prior is owed.
    tip = _commit(fx.work, {"x.py": f"x = 1\n{BAD}\n", "c.py": f"# c9\n{GOOD}\n"})
    item["entry"]["new_oid"] = _blob(fx.work, tip, "x.py")
    review_gate._write_run_records({"status": "success", "findings": []}, [item],
                                   fx.common, FP, "r2", str(fx.work), tip)
    assert [f["id"] for f in _written(fx, item)] == [_FID]


def test_the_source_record_is_never_rewritten_from_cross_file_evidence(tmp_path):
    """Records are content-addressed and shared by other tips: suppression is a
    read-time property, the record that carried the prior is left as it is."""
    fx = _Fixed(tmp_path)
    item, tip = _prior_record_item(fx)
    old_entry = {"path": "x.py", "old_path": "", "status": "M", "old_oid": "0" * 40,
                 "new_oid": fx.target_oid}
    old_key = review_gate._record_key("x.py", "", "M", "0" * 40)
    review_gate._write_ledger_record(fx.common, FP, old_key, fx.target_oid, "x.py", "", "M",
                                     "0" * 40, item["record"]["findings"], 0, "r0")
    rpath = review_gate._record_path(fx.common, FP, old_key, fx.target_oid)
    before = rpath.read_bytes()
    review_gate._write_run_records({"status": "success", "findings": []}, [item],
                                   fx.common, FP, "r2", str(fx.work), tip)
    assert rpath.read_bytes() == before
    del old_entry


def test_classify_priors_suppresses_a_prior_carried_by_any_record(tmp_path):
    """Read time: the resolution covers the prior whichever record carried it,
    including after the evidence file was edited."""
    fx = _Fixed(tmp_path)
    tip = _commit(fx.work, {"c.py": f"# c, edited later\n{GOOD}\n"})
    entry = {"path": "x.py", "old_path": "", "status": "M", "old_oid": "0" * 40,
             "new_oid": _blob(fx.work, tip, "x.py"), "lines": 2}
    prior = dict(fx.finding, id=_FID, target_oid=fx.target_oid)
    record = {"head_oid": fx.target_oid, "findings": [prior]}
    item = {"entry": entry, "mode": "carry", "record": record, "from_oid": fx.target_oid}
    to_resolve, auto, carried = review_gate._classify_priors(
        [item], tip, str(fx.work), fx.common, FP, "r3")
    assert (to_resolve, auto, carried) == ([], [], [])
    # With the fix reverted it goes back to the resolver.
    tip2 = _commit(fx.work, {"x.py": f"x = 7\n{BAD}\n", "c.py": "# c, fix gone\n"})
    to_resolve, auto, carried = review_gate._classify_priors(
        [item], tip2, str(fx.work), fx.common, FP, "r3")
    assert [p["id"] for p in to_resolve] == [_FID]


def test_the_segment_callers_apply_the_same_rule(tmp_path):
    fx = _Fixed(tmp_path)
    tip = _commit(fx.work, {"c.py": f"# c, edited later\n{GOOD}\n"})
    f = dict(fx.finding, id=_FID, target_oid=fx.target_oid)
    assert review_gate._seg_suppress_resolved(
        [f], set(), str(fx.work), tip, fx.common, FP) == []
    to_resolve, carried = review_gate._seg_classify_foreign(
        [f], str(fx.work), tip, fx.common, FP, set())
    assert (to_resolve, carried) == ([], [])
    tip2 = _commit(fx.work, {"c.py": "# fix removed\n"})
    assert review_gate._seg_suppress_resolved(
        [f], set(), str(fx.work), tip2, fx.common, FP) == [f]


# ---------------------------------------------------------------------------
# End to end: no resolver call after an unrelated edit to the evidence file
# ---------------------------------------------------------------------------

def _tiny_repo(tmp_path, files):
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _init(work)
    for name, content in files.items():
        (work / name).write_text(content, encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    return work


def _env(tmp_path, trace, **kw):
    env = dict(os.environ)
    for k in list(env):
        if k.startswith(("OCR_", "STUB_")):
            env.pop(k)
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "gate-data")
    env["OCR_REVIEWER_CMD"] = f'"{sys.executable}" "{_STUB}"'
    env["STUB_TRACE"] = str(tmp_path / trace)
    env["OCR_INLINE_BUDGET"] = "30"
    for k, v in kw.items():
        env[k] = str(v)
    return env


def _hook(work, env, cmd):
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash",
                          "cwd": str(work), "tool_input": {"command": cmd}})
    proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                          capture_output=True, text=True, cwd=str(work), env=env,
                          timeout=90)
    assert proc.returncode == 0, proc.stderr
    return parse_pretooluse(proc.stdout)


def _trace(tmp_path, name):
    p = tmp_path / name
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _done(work, tip, timeout=60):
    common = review_gate._git_common_dir(str(work))
    path = review_gate._state_path(common, tip)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = review_gate._read_state(path) or {}
        if st.get("state") in ("done", "failed"):
            return st
        time.sleep(0.2)
    raise AssertionError(f"no final state: {review_gate._read_state(path)}")


def test_a_later_edit_to_the_evidence_file_does_not_send_the_finding_back_to_the_resolver(tmp_path):
    work = _tiny_repo(tmp_path, {"x.py": "x = 0\n", "c.py": "# c init\n"})
    # T1: x.py is flagged.
    (work / "x.py").write_text(f"x = 1\n{BAD}\n")
    (work / "c.py").write_text("# c v2\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "t1"], cwd=work)
    tip1 = _git(["rev-parse", "HEAD"], cwd=work)
    env1 = _env(tmp_path, "t1.trace", STUB_FINDINGS_FOR=json.dumps({"x.py": _FINDING}))
    decision, reason = _hook(work, env1, "git push origin main")
    assert decision == "deny", reason
    assert _done(work, tip1).get("state") == "done"

    # T2: fixed in x.py, with the fix evidenced in c.py: a resolution is written.
    (work / "x.py").write_text(f"x = 1\n{GOOD}\n")
    (work / "c.py").write_text(f"# c fixed\n{GOOD}\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "--amend", "--no-edit"], cwd=work)
    tip2 = _git(["rev-parse", "HEAD"], cwd=work)
    resolve = json.dumps({_FID: {"status": "resolved", "evidence_path": "c.py",
                                 "evidence_quote": GOOD}})
    env2 = _env(tmp_path, "t2.trace", STUB_RESOLVE=resolve, STUB_VERDICT="pass")
    decision, reason = _hook(work, env2, "git push -f origin main")
    assert decision == "pass", reason
    assert _done(work, tip2).get("state") == "done"
    assert len([c for c in _trace(tmp_path, "t2.trace") if c.get("resolve_file")]) == 1

    # T3: c.py is edited again; the quote is still there. No resolver call, push passes.
    (work / "c.py").write_text(f"# c fixed\n{GOOD}\nextra = 1\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "--amend", "--no-edit"], cwd=work)
    tip3 = _git(["rev-parse", "HEAD"], cwd=work)
    env3 = _env(tmp_path, "t3.trace", STUB_VERDICT="pass")
    decision, reason = _hook(work, env3, "git push -f origin main")
    assert decision == "pass", reason
    assert _done(work, tip3).get("state") == "done"
    assert [c for c in _trace(tmp_path, "t3.trace") if c.get("resolve_file")] == []
