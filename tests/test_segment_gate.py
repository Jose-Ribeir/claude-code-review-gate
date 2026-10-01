"""Part S (0.11.0) in the gate: big files reviewed in stable units.

Real git repositories, the real supervisor (run in-process so the module's limits
can be lowered) and the stub reviewer (tests/stub_reviewer.py), which traces the
manifest it was given -- items, tasks, the text of every file the items name -- and
answers with scripted findings and dep verdicts.
"""
import json
import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_async_gate as ag  # noqa: E402
import test_resolver_robustness as rr  # noqa: E402
from test_async_gate import review_gate  # noqa: E402

import ocr_segment  # noqa: E402



# --- fixtures and helpers ------------------------------------------------------------------

def func(i, n=40, extra="", body="    y{j} = x + {j}\n"):
    lines = "".join(body.format(j=j) for j in range(n))
    return f"def f{i}(x):\n{lines}{extra}    return y0 + {i}\n"


def module(n_funcs=40, overrides=None, tail=""):
    overrides = overrides or {}
    parts = [overrides[i] if i in overrides else func(i) for i in range(n_funcs)]
    return "import os\n\n\n" + "\n\n".join(parts) + "\n" + tail


def push_big(tmp_path, tip_files, base_files=None):
    """A repo whose pushed base holds `base_files` and whose HEAD holds `tip_files` on top."""
    work = rr._tiny_repo(tmp_path, base_files or {"keep.py": "x = 0\n"})
    return work, commit(work, tip_files)


def commit(work, files, msg="c"):
    for name, content in files.items():
        p = work / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="")
    ag._git(["add", "-A"], cwd=work)
    ag._git(["commit", "-q", "-m", msg], cwd=work)
    return ag._git(["rev-parse", "HEAD"], cwd=work)


_RUN = {"n": 0}


def run(work, tip, tmp_path, monkeypatch, trace=None):
    """One supervisor run for `tip`, with its own stub trace; (final state, calls)."""
    _RUN["n"] += 1
    name = trace or f"t{_RUN['n']}.trace"
    monkeypatch.setenv("STUB_TRACE", str(tmp_path / name))
    st = ag._supervise_inproc(work, tip, f"run{_RUN['n']}")
    return st, rr._trace(tmp_path, name)


def reviews(calls):
    return [c for c in calls if not c.get("resolve_file")]


def kinds(call, kind):
    return [i for i in (call["manifest"] or {}).get("items") or [] if i["kind"] == kind]


def unit_names(calls):
    return sorted(i["unit"] for c in calls for i in kinds(c, "unit_diff"))


def fp_of(work, tip):
    return review_gate._compute_fingerprint(str(work), tip)


def common_of(work):
    return review_gate._git_common_dir(str(work))


def seg_recs(work, tip, sub="seg"):
    d = review_gate._fp_dir(common_of(work), fp_of(work, tip)) / sub
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))]


def result_findings(work):
    """The findings of the latest completed review (the findings log's last line)."""
    path = review_gate._findings_log_path(review_gate._git_dir(str(work)))
    return json.loads(path.read_text(encoding="utf-8").splitlines()[-1])["findings"]


def file_record(work, tip, path):
    base = ag._git(["rev-parse", "origin/main"], cwd=work)
    entries, _ = review_gate._collect_diff_entries(str(work), base, tip)
    e = next(x for x in entries if x["path"] == path)
    return ag._record(common_of(work), fp_of(work, tip), e)


def drop_file_records(work, tip):
    d = review_gate._fp_dir(common_of(work), fp_of(work, tip))
    for p in d.glob("*.json"):
        p.unlink()


# --- the first review of a big file -------------------------------------------------------------

def test_a_big_new_file_is_reviewed_in_units_and_never_as_a_truncated_diff(tmp_path, gate_env):
    work, tip = push_big(tmp_path, {"big.py": module(40)})
    st, calls = run(work, tip, tmp_path, gate_env)
    assert st["state"] == "done" and not st.get("unreviewed_truncated")
    (call,) = reviews(calls)
    assert not kinds(call, "file_diff")                       # no whole-file diff at all
    units = kinds(call, "unit_diff")
    assert sorted(u["unit"] for u in units if u["unit_kind"] == "function") == sorted(
        f"f{i}" for i in range(40))
    assert all(u["truncated"] is False and u["path"] == "big.py" and u["part"] == 1 for u in units)
    f7 = next(u for u in units if u["unit"] == "f7")
    text = call["item_files"]["unit:big.py:f7:1"]
    assert f"@@ -0,0 +{f7['start_line']},{f7['end_line'] - f7['start_line'] + 1} @@" in text
    assert "+def f7(x):" in text and "# unit: f7 (function)" in text
    # imports and a signature index, for context
    ctx = next(i for i in kinds(call, "context") if i["role"] == "file_context")
    assert "[CHANGED]" in call["item_files"][f"context:file_context:big.py:{call['manifest']['items'].index(ctx)}"]
    assert call["manifest"]["paths"] == ["big.py"] and call["manifest"]["tasks"] == []
    # the per-file record is complete, never flagged truncated
    rec = file_record(work, tip, "big.py")
    assert rec is not None and not review_gate._is_truncated_record(rec)
    assert len(seg_recs(work, tip)) == len(units)


def test_ocr_segment_0_restores_the_0_10_0_review_of_a_big_file(tmp_path, gate_env):
    gate_env.setenv("OCR_SEGMENT", "0")
    work, tip = push_big(tmp_path, {"big.py": module(40)})
    st, calls = run(work, tip, tmp_path, gate_env)
    (call,) = reviews(calls)
    assert not kinds(call, "unit_diff")
    (item,) = kinds(call, "file_diff")
    assert item["truncated"] is True and item["level"] == "stat"        # Part B's degradation
    assert st.get("unreviewed_truncated") == 1
    assert seg_recs(work, tip) == []
    assert review_gate._is_truncated_record(file_record(work, tip, "big.py"))


def test_no_item_exceeds_the_chunk_budget_and_every_unit_is_in_exactly_one_chunk(tmp_path, gate_env):
    gate_env.setattr(review_gate, "_CHUNK_DIFF_LINES", 400)
    work, tip = push_big(tmp_path, {"big.py": module(40)})
    st, calls = run(work, tip, tmp_path, gate_env)
    assert st["state"] == "done"
    rev = reviews(calls)
    assert len(rev) > 3
    seen = []
    for c in rev:
        lines = sum(i["lines"] for i in kinds(c, "unit_diff"))
        assert lines <= 400 and all(i["lines"] <= 400 for i in kinds(c, "unit_diff"))
        seen += [i["unit"] for i in kinds(c, "unit_diff")]
    assert sorted(seen) == unit_names(calls) and len(seen) == len(set(seen)) == 41   # 40 + the preamble


def test_a_unit_too_big_for_one_item_is_split_into_parts_that_share_one_record(tmp_path, gate_env):
    gate_env.setattr(review_gate, "_CHUNK_DIFF_LINES", 400)
    huge = func(0, n=1700)
    work, tip = push_big(tmp_path, {"big.py": "import os\n\n\n" + huge + "\n\n\n" + func(1)})
    st, calls = run(work, tip, tmp_path, gate_env)
    assert st["state"] == "done"
    parts = sorted((i["part"], i["parts"], i["lines"]) for c in reviews(calls)
                   for i in kinds(c, "unit_diff") if i["unit"] == "f0")
    assert [p[0] for p in parts] == list(range(1, len(parts) + 1)) and len(parts) > 3
    assert all(p[1] == len(parts) and p[2] <= 400 for p in parts)
    f0 = [r for r in seg_recs(work, tip) if r["unit"] == "f0"]
    assert len(f0) == 1                                       # one record for all the parts


# --- the unit cache ---------------------------------------------------------------------------------

def test_a_run_killed_after_chunk_one_resumes_with_only_the_rest(tmp_path, gate_env):
    gate_env.setattr(review_gate, "_CHUNK_DIFF_LINES", 400)
    work, tip = push_big(tmp_path, {"big.py": module(40)})
    # the run budget runs out after the first chunk (as a usage limit or a sleep would end it)
    gate_env.setattr(review_gate, "_RUN_BUDGET", 1)
    gate_env.setenv("STUB_SLEEP", "1.3")
    st, calls1 = run(work, tip, tmp_path, gate_env)
    assert st["state"] == "failed" and st["reason"] == "budget" and len(reviews(calls1)) == 1
    done = {i["unit"] for i in kinds(calls1[0], "unit_diff")}
    assert done and file_record(work, tip, "big.py") is None   # nothing carried yet: not final
    gate_env.setattr(review_gate, "_RUN_BUDGET", 3600)
    gate_env.setenv("STUB_SLEEP", "0")
    st, calls2 = run(work, tip, tmp_path, gate_env)
    assert st["state"] == "done"
    again = {i["unit"] for c in reviews(calls2) for i in kinds(c, "unit_diff")}
    assert not (done & again)                                   # nothing reviewed twice
    assert len(done) + len(again) == 41
    assert file_record(work, tip, "big.py") is not None


def test_an_identical_second_run_makes_zero_items_and_a_one_unit_edit_exactly_one(tmp_path, gate_env):
    work, tip = push_big(tmp_path, {"big.py": module(40)})
    st, calls = run(work, tip, tmp_path, gate_env)
    assert len(reviews(calls)) == 1
    drop_file_records(work, tip)               # as if the file's record had been lost
    st, calls = run(work, tip, tmp_path, gate_env)
    assert st["state"] == "done" and reviews(calls) == []       # every unit is cached: no model call
    # one function edited: only that unit is a miss (the file record is gone, so units decide)
    edited = module(40, {5: func(5, extra="    y_new = 1\n")})
    tip2 = commit(work, {"big.py": edited})
    drop_file_records(work, tip2)
    st, calls = run(work, tip2, tmp_path, gate_env)
    assert st["state"] == "done"
    assert unit_names(calls) == ["f5"]
    (call,) = reviews(calls)
    assert "+    y_new = 1" in call["item_files"]["unit:big.py:f5:1"]


def test_findings_move_with_their_unit_when_fifty_lines_are_inserted_above(tmp_path, gate_env):
    gate_env.setenv("STUB_UNIT_FINDINGS", json.dumps({"f7": {"severity": "medium", "content": "f7 is odd", "offset": 3}}))
    work, tip = push_big(tmp_path, {"big.py": module(40)})
    st, calls = run(work, tip, tmp_path, gate_env)
    assert st["state"] == "done" and st["verdict"] == "warn"
    rec = file_record(work, tip, "big.py")
    (f,) = [x for x in rec["findings"] if "f7 is odd" in x["content"]]
    old_line = f["start_line"]
    assert old_line == 4 + 7 * 44 + 3        # function 7 starts at line 312; the finding is 3 lines in
    # 50 lines inserted into function 1; function 7's finding must move by exactly 50
    tip2 = commit(work, {"big.py": module(40, {1: func(1, extra="".join(f"    z{j} = {j}\n" for j in range(50)))})})
    drop_file_records(work, tip2)
    st, calls = run(work, tip2, tmp_path, gate_env)
    assert st["state"] == "done" and unit_names(calls) == ["f1"]
    rec2 = file_record(work, tip2, "big.py")
    (g,) = [x for x in rec2["findings"] if "f7 is odd" in x["content"]]
    assert g["start_line"] == old_line + 50 and g["end_line"] == old_line + 50
    assert g["existing_code"].strip() in (work / "big.py").read_text(encoding="utf-8").splitlines()[g["start_line"] - 1]


def test_a_cached_finding_survives_a_rebase_of_the_base(tmp_path, gate_env):
    """The file's record is keyed by its base blob; the units' records are not: a base that
    moved under the same tip still replays every unit that did not change."""
    gate_env.setenv("STUB_UNIT_FINDINGS", json.dumps({"f3": {"severity": "high", "content": "f3 bad", "offset": 2}}))
    work, tip = push_big(tmp_path, {"big.py": module(40)}, {"keep.py": "x = 0\n", "big.py": "# v1\n"})
    st, calls = run(work, tip, tmp_path, gate_env)
    assert st["verdict"] == "block"
    # a different base for big.py: the file record cannot be found, the units can
    ag._git(["checkout", "-q", "-B", "other", "HEAD~1"], cwd=work)
    commit(work, {"big.py": "# v2 of the base\nz = 1\n"})
    ag._git(["push", "-q", "-f", "origin", "other:main"], cwd=work)
    ag._git(["checkout", "-q", "main"], cwd=work)
    ag._git(["fetch", "-q", "origin"], cwd=work)
    st, calls = run(work, tip, tmp_path, gate_env)
    assert st["state"] == "done"
    assert not [i for c in reviews(calls) for i in kinds(c, "unit_diff") if i["unit"] == "f3"]
    assert st["verdict"] == "block" and "f3 bad" in st["reasons"]       # replayed, still blocking


def test_duplicate_units_replay_to_the_right_occurrence(tmp_path, gate_env):
    dup = func(9, n=30)
    src = "import os\n\n\n" + "\n\n\n".join([func(0), dup, func(1), dup, func(2)]) + "\n"
    big = src + "".join(f"\n\n{func(10 + i, n=40)}" for i in range(40))
    gate_env.setenv("STUB_UNIT_FINDINGS", json.dumps({"f9": {"severity": "medium", "content": "dup bad", "offset": 4}}))
    work, tip = push_big(tmp_path, {"big.py": big}, {"keep.py": "x = 0\n", "big.py": "# v1\n"})
    st, calls = run(work, tip, tmp_path, gate_env)
    assert st["state"] == "done"
    seg = ocr_segment.segment("big.py", big)
    starts = [u["start"] + 4 for u in seg["units"] if u["qualname"] == "f9"]
    assert len(starts) == 2
    first = sorted(f["start_line"] for f in result_findings(work) if "dup bad" in f["content"])
    assert first == starts
    # again from the units alone (the base moved, so the file record is not found)
    ag._git(["checkout", "-q", "-B", "other", "HEAD~1"], cwd=work)
    commit(work, {"big.py": "# v2 of the base\nz = 1\n"})
    ag._git(["push", "-q", "-f", "origin", "other:main"], cwd=work)
    ag._git(["checkout", "-q", "main"], cwd=work)
    ag._git(["fetch", "-q", "origin"], cwd=work)
    st, calls = run(work, tip, tmp_path, gate_env)
    assert st["state"] == "done"
    # every unit replayed from its record; only the base's own lines, now gone, are a new change
    assert [i["deleted"] for c in reviews(calls) for i in kinds(c, "unit_diff")] == [True]
    again = sorted(f["start_line"] for f in result_findings(work) if "dup bad" in f["content"])
    assert again == starts, (again, starts)


# --- a truncated record is no longer carried when the file can be segmented -----------------------

def test_an_old_truncated_record_of_a_segmentable_file_is_reviewed_again(tmp_path, gate_env):
    work, tip = push_big(tmp_path, {"big.py": module(40)})
    gate_env.setenv("OCR_SEGMENT", "0")                      # 0.10.0: a stat-level, flagged review
    st, calls = run(work, tip, tmp_path, gate_env)
    assert review_gate._is_truncated_record(file_record(work, tip, "big.py"))
    gate_env.delenv("OCR_SEGMENT")
    st, calls = run(work, tip, tmp_path, gate_env)
    assert st["state"] == "done" and not st.get("unreviewed_truncated")
    assert len(unit_names(calls)) == 41                       # a proper review, in units, at last
    assert not review_gate._is_truncated_record(file_record(work, tip, "big.py"))
    st, calls = run(work, tip, tmp_path, gate_env)            # and now it is simply carried
    assert reviews(calls) == [] and not st.get("unreviewed_truncated")


def test_a_file_that_cannot_be_segmented_keeps_being_carried_not_reviewed_forever(tmp_path, gate_env):
    longline = "    s = '" + "x" * 700 + "'\n"
    big = module(40, {3: func(3, extra=longline)})            # a changed unit with an uncuttable line
    work, tip = push_big(tmp_path, {"big.py": big})
    st, calls = run(work, tip, tmp_path, gate_env)
    (call,) = reviews(calls)
    assert not kinds(call, "unit_diff") and kinds(call, "file_diff")[0]["truncated"] is True
    assert review_gate._TELE["seg"]["declined"] == {"big.py": "long_lines"}
    assert st["unreviewed_truncated"] == 1
    st, calls = run(work, tip, tmp_path, gate_env)            # Part A: carried, not re-reviewed
    assert reviews(calls) == [] and st["unreviewed_truncated"] == 1


def test_a_model_truncation_warning_about_a_unit_reviewed_file_is_ignored(tmp_path, gate_env):
    gate_env.setenv("STUB_TRUNCATE_FOR", "*")
    work, tip = push_big(tmp_path, {"big.py": module(40)})
    st, calls = run(work, tip, tmp_path, gate_env)
    assert st["state"] == "done" and not st.get("unreviewed_truncated")
    assert not review_gate._is_truncated_record(file_record(work, tip, "big.py"))


# --- the coverage tripwire ------------------------------------------------------------------------

def test_a_gap_in_the_coverage_falls_back_to_whole_file_review_and_says_so(tmp_path, gate_env):
    gate_env.setattr(ocr_segment, "check_coverage", lambda *a, **k: ["forced gap"])
    work, tip = push_big(tmp_path, {"big.py": module(40)})
    st, calls = run(work, tip, tmp_path, gate_env)
    (call,) = reviews(calls)
    assert not kinds(call, "unit_diff")
    (item,) = kinds(call, "file_diff")
    assert item["path"] == "big.py" and item["level"] == "stat"
    assert review_gate._TELE["seg"]["declined"] == {"big.py": "coverage"}
    assert review_gate._is_truncated_record(file_record(work, tip, "big.py"))


def test_coverage_holds_for_random_edits_of_a_real_git_diff(tmp_path, gate_env):
    """Every changed line of a real `git diff` lies in a unit the segmenter knows: the gate
    never has to fall back for a plain edit."""
    import random
    rnd = random.Random(11)
    base_text = module(14, {i: func(i, n=18) for i in range(14)})
    work = rr._tiny_repo(tmp_path, {"keep.py": "x = 0\n", "m.py": base_text})
    base = ag._git(["rev-parse", "HEAD"], cwd=work)
    lines = base_text.split("\n")
    for round_no in range(12):
        tl = list(lines)
        for _ in range(rnd.randint(1, 6)):
            i = rnd.randrange(3, len(tl) - 3)
            op = rnd.choice(("edit", "ins", "del", "dupe"))
            if op == "edit":
                tl[i] += "  # edit"
            elif op == "ins":
                tl.insert(i, f"    extra{round_no} = {i}")
            elif op == "del":
                del tl[i]
            else:
                tl[i:i] = tl[i:i + 5]
        tip = commit(work, {"m.py": "\n".join(tl)}, msg=f"r{round_no}")
        entries, _ = review_gate._collect_diff_entries(str(work), base, tip)
        item = {"entry": entries[0], "mode": "full", "record": None, "from_oid": "", "miss_reason": "x"}
        state = review_gate._SegState(str(work), base, tip, common_of(work), "f" * 64, "r", False)
        sf = state.build(item)
        assert sf is not None and state.declined == {}, (round_no, state.declined)
        ag._git(["reset", "-q", "--hard", base], cwd=work)


# --- findings: a moved anchor ----------------------------------------------------------------------

def test_a_replayed_finding_whose_anchor_was_edited_is_found_by_its_code_or_dropped():
    lines = ["def f():", "    a = 1", "    risky(a)", "    return a"]
    unit = {"start": 10, "end": 13}
    tip = [""] * 9 + lines
    f = {"path": "m.py", "start_line": 12, "end_line": 12, "content": "x", "existing_code": "risky(a)"}
    stored = review_gate._seg_anchor(f, unit, tip)
    assert (stored["rel_start"], stored["rel_end"]) == (2, 2)
    ok = review_gate._seg_remap(stored, {"start": 30, "end": 33}, [""] * 29 + lines)
    assert ok["start_line"] == 32 and "rel_start" not in ok and "anchor_hash" not in ok
    # the anchored line was edited in place: the code snippet no longer matches either -> dropped
    edited = [""] * 9 + ["def f():", "    a = 1", "    safe(a)", "    return a"]
    assert review_gate._seg_remap(stored, unit, edited) is None
    # the line moved inside the unit: found again by its existing_code
    moved = [""] * 9 + ["def f():", "    a = 1", "    b = 2", "    risky(a)"]
    found = review_gate._seg_remap(stored, unit, moved)
    assert found["start_line"] == 13
    assert review_gate._seg_remap({"path": "m.py", "content": "x"}, unit, tip) is None   # no anchor at all


def test_dep_verdicts_are_validated_strictly():
    clean = review_gate._clean_dep_verdicts
    assert clean({"dep:1": "ok", "dep:2": " Broken ", "dep:3": "unsure"}) == {
        "dep:1": "ok", "dep:2": "broken", "dep:3": "unsure"}
    assert clean({"dep:1": True, "dep:2": "fine", "dep:3": None, 4: "ok", "dep:5": ["ok"]}) == {}
    assert clean(["ok"]) == {} and clean(None) == {} and clean("ok") == {}


# --- caller checks ------------------------------------------------------------------------------------

def small_module(callee_body="    return x + 1\n", n_filler=4, n_callers=1):
    parts = [func(i, n=10) for i in range(n_filler)]
    parts.append(f"def compute(x):\n{callee_body}")
    for c in range(n_callers):
        parts.append(f"def use_it{c}(v):\n    r = compute(v)\n    return r.value\n")
    return "import os\n\n\n" + "\n\n".join(parts) + "\n"


def dep_repo(tmp_path, gate_env, n_callers=1, **stub):
    """compute() changes in a file with callers of it, forced into units by a tiny limit."""
    gate_env.setattr(review_gate, "_PRECOMPUTED_MAX_LINES", 1)
    for k, v in stub.items():
        gate_env.setenv(k, str(v))
    work = rr._tiny_repo(tmp_path, {"keep.py": "x = 0\n", "big.py": small_module(n_callers=n_callers)})
    tip = commit(work, {"big.py": small_module("    return None\n", n_callers=n_callers)})
    return work, tip


def test_a_same_file_caller_of_a_changed_unit_gets_a_context_item_and_a_task(tmp_path, gate_env):
    work, tip = dep_repo(tmp_path, gate_env)
    st, calls = run(work, tip, tmp_path, gate_env)
    assert st["state"] == "done" and st["verdict"] == "pass"
    (call,) = reviews(calls)
    m = call["manifest"]
    assert unit_names(calls) == ["compute"]
    (task,) = m["tasks"]
    assert task["type"] == "dep_check" and task["callee"]["unit"] == "compute"
    assert task["caller"]["unit"] == "use_it0" and task["id"].startswith("dep:")
    assert "verify" in task["instruction"].lower()
    (c,) = [i for i in m["items"] if i["kind"] == "context" and i["role"] == "caller"]
    assert c["task"] == task["id"] and c["path"] == "big.py"
    text = call["item_files"][f"context:caller:big.py:{m['items'].index(c)}"]
    assert "r = compute(v)" in text and f"{c['start_line']:>6} |" in text
    assert not [i for i in m["items"] if i.get("role") == "callee_diff"]   # compute's diff is in the chunk
    # the stub said ok: a final record, and the file is carried afterwards
    (dep,) = seg_recs(work, tip, "dep")
    assert dep["status"] == "ok"
    assert file_record(work, tip, "big.py") is not None
    st, calls = run(work, tip, tmp_path, gate_env)
    assert reviews(calls) == []


def test_a_broken_caller_is_a_finding_at_the_caller_and_replays(tmp_path, gate_env):
    work, tip = dep_repo(tmp_path, gate_env, STUB_DEP_VERDICT="broken")
    st, calls = run(work, tip, tmp_path, gate_env)
    assert st["verdict"] == "block" and "caller broken by the change to compute" in st["reasons"]
    (f,) = [x for x in result_findings(work) if x.get("dep_task")]
    caller = next(i for i in kinds(calls[0], "context") if i["role"] == "caller")
    assert f["path"] == "big.py" and f["start_line"] == caller["start_line"]
    assert [d["status"] for d in seg_recs(work, tip, "dep")] == ["broken"]
    # lose the file's record: unit and caller check both replay, with no model call at all
    drop_file_records(work, tip)
    st, calls = run(work, tip, tmp_path, gate_env)
    assert reviews(calls) == [] and st["verdict"] == "block"
    (g,) = [x for x in result_findings(work) if x.get("dep_task")]
    assert g["start_line"] == f["start_line"]


def test_an_unsure_caller_check_is_asked_once_more_in_the_same_run(tmp_path, gate_env):
    work, tip = dep_repo(tmp_path, gate_env, STUB_DEP_VERDICT="unsure_first")
    st, calls = run(work, tip, tmp_path, gate_env)
    assert st["state"] == "done" and st["verdict"] == "pass"
    first, second = reviews(calls)
    assert kinds(first, "unit_diff") and first["manifest"]["tasks"]
    # the second look: no unit under review, no path under review -- only the check, with the callee's change
    assert not kinds(second, "unit_diff") and second["manifest"]["paths"] == []
    assert {i["role"] for i in kinds(second, "context")} == {"caller", "callee_diff"}
    assert second["manifest"]["tasks"][0]["id"] == first["manifest"]["tasks"][0]["id"]
    cd = next(i for i in kinds(second, "context") if i["role"] == "callee_diff")
    assert "-    return x + 1" in second["item_files"][
        f"context:callee_diff:big.py:{second['manifest']['items'].index(cd)}"]
    assert [d["status"] for d in seg_recs(work, tip, "dep")] == ["ok"]
    assert file_record(work, tip, "big.py") is not None


def test_a_missing_dep_verdict_is_unsure_never_ok_and_ends_as_an_unverified_note(tmp_path, gate_env):
    work, tip = dep_repo(tmp_path, gate_env, STUB_DEP_VERDICT="missing")
    st, calls = run(work, tip, tmp_path, gate_env)
    assert st["state"] == "done" and st["verdict"] == "pass"          # non-blocking
    assert len(reviews(calls)) == 2                                    # asked, then once more
    notes = [f for f in result_findings(work) if f.get("provenance") == "unverified_dependency"]
    assert len(notes) == 1 and notes[0]["severity"] == "info"
    assert "unverified dependency" in notes[0]["content"] and "use_it0" in notes[0]["content"]
    assert [d["status"] for d in seg_recs(work, tip, "dep")] == ["unsure_final"]
    # and it stays visible from the record
    drop_file_records(work, tip)
    st, calls = run(work, tip, tmp_path, gate_env)
    assert reviews(calls) == []
    assert [f for f in result_findings(work) if f.get("provenance") == "unverified_dependency"]


def test_garbage_dep_verdicts_count_as_unsure(tmp_path, gate_env):
    work, tip = dep_repo(tmp_path, gate_env, STUB_DEP_VERDICT="garbage")
    st, calls = run(work, tip, tmp_path, gate_env)
    assert [d["status"] for d in seg_recs(work, tip, "dep")] == ["unsure_final"]


def test_a_pending_caller_check_blocks_the_file_record_and_is_scheduled_first(tmp_path, gate_env):
    work, tip = dep_repo(tmp_path, gate_env, STUB_DEP_VERDICT="unsure_first", STUB_SLEEP="1.3")
    gate_env.setattr(review_gate, "_RUN_BUDGET", 1)        # ends the run before the second look
    st, calls = run(work, tip, tmp_path, gate_env)
    assert st["state"] == "failed" and st["reason"] == "budget" and len(reviews(calls)) == 1
    assert [d["status"] for d in seg_recs(work, tip, "dep")] == ["unsure"]
    assert len(seg_recs(work, tip)) == 1                        # the unit itself is final
    assert file_record(work, tip, "big.py") is None             # but the file is not: a check is owed
    gate_env.setattr(review_gate, "_RUN_BUDGET", 3600)
    gate_env.setenv("STUB_SLEEP", "0")
    st, calls = run(work, tip, tmp_path, gate_env)
    (call,) = reviews(calls)
    assert not kinds(call, "unit_diff") and call["manifest"]["tasks"]       # only the owed check
    assert st["state"] == "done"
    assert [d["status"] for d in seg_recs(work, tip, "dep")] == ["ok"]
    assert file_record(work, tip, "big.py") is not None


def test_callers_per_unit_are_capped_at_six_tasks(tmp_path, gate_env):
    work, tip = dep_repo(tmp_path, gate_env, n_callers=10)
    st, calls = run(work, tip, tmp_path, gate_env)
    (call,) = reviews(calls)
    assert len(call["manifest"]["tasks"]) == 6
    assert len([i for i in kinds(call, "context") if i["role"] == "caller"]) == 6


def test_a_small_delta_file_gets_its_callers_checked_too(tmp_path, gate_env):
    work = rr._tiny_repo(tmp_path, {"keep.py": "x = 0\n", "big.py": small_module(n_filler=1)})
    body1 = "    a = x\n    b = a\n    c = b\n    d = c\n    return d\n"
    tip1 = commit(work, {"big.py": small_module(body1, n_filler=1)})
    st, calls = run(work, tip1, tmp_path, gate_env)
    assert st["state"] == "done"
    # a second, small change to compute(): reviewed as a delta of the first review
    tip2 = commit(work, {"big.py": small_module(body1.replace("return d", "return None"), n_filler=1)})
    st, calls = run(work, tip2, tmp_path, gate_env)
    assert st["state"] == "done"
    (call,) = reviews(calls)
    assert call["manifest"]["files"][0]["mode"] == "delta"
    assert call["manifest"]["tasks"], call["manifest"]
    assert kinds(call, "file_diff") and not kinds(call, "unit_diff")
    assert [d["status"] for d in seg_recs(work, tip2, "dep")] == ["ok"]
    assert file_record(work, tip2, "big.py") is not None


def test_ocr_segment_0_means_no_caller_checks_either(tmp_path, gate_env):
    gate_env.setenv("OCR_SEGMENT", "0")
    work, tip = dep_repo(tmp_path, gate_env)
    st, calls = run(work, tip, tmp_path, gate_env)
    (call,) = reviews(calls)
    assert call["manifest"]["tasks"] == [] and not kinds(call, "unit_diff")
    assert not [i for i in kinds(call, "context") if i["role"] == "caller"]


def test_two_unit_reviewed_files_in_one_chunk_have_their_own_files(tmp_path, gate_env):
    gate_env.setattr(review_gate, "_CHUNK_DIFF_LINES", 10000)
    work, tip = push_big(tmp_path, {"big.py": module(40), "big2.py": module(40, {3: func(3, extra="    q = 1\n")})})
    st, calls = run(work, tip, tmp_path, gate_env)
    assert st["state"] == "done" and len(reviews(calls)) == 1 and len(reviews(calls)[0]["manifest"]["paths"]) == 2
    items = [i for c in reviews(calls) for i in (c["manifest"]["items"]) if i.get("file")]
    files = [i["file"] for i in items]
    assert len(files) == len(set(files))                      # no name is written twice in a chunk
    texts = {}
    for c in reviews(calls):
        for i in c["manifest"]["items"]:
            if i["kind"] == "unit_diff" and i["unit"] == "f3":
                texts[i["path"]] = c["item_files"][f"unit:{i['path']}:f3:1"]
    assert "+    q = 1" in texts["big2.py"] and "+    q = 1" not in texts["big.py"]
