"""Part S's benchmark scenarios, as far as they can be automated (see tests/part_s_scenarios.py).

Every scenario goes through the real gate with the stub reviewer, and the assertion is on what the
reviewer is HANDED: the changed unit, the caller check (task + context), the cross-file call site,
the unit index. Whether a model then notices the seeded bug is the manual half of the benchmark
(docs/benchmark-part-s.md).
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import part_s_scenarios  # noqa: E402
import test_resolver_robustness as rr  # noqa: E402
from test_async_gate import review_gate  # noqa: E402
from test_segment_gate import (  # noqa: E402,F401
    commit, drop_file_records, file_record, func, kinds, module, push_big,
    result_findings, reviews, run, unit_names,
)


def _context_text(call, item):
    return call["item_files"][f"context:{item['role']}:{item['path']}:{call['manifest']['items'].index(item)}"]


@pytest.mark.parametrize("sc", part_s_scenarios.S, ids=lambda s: s["name"])
def test_the_reviewer_is_handed_what_the_seeded_bug_needs(sc, tmp_path, gate_env):
    gate_env.setattr(review_gate, "_PRECOMPUTED_MAX_LINES", 0)           # force the file into units
    if sc.get("chunk_budget"):
        gate_env.setattr(review_gate, "_CHUNK_DIFF_LINES", sc["chunk_budget"])
    work = rr._tiny_repo(tmp_path, sc["base"])
    tip = commit(work, sc["tip"])
    st, calls = run(work, tip, tmp_path, gate_env)
    assert st["state"] == "done", st
    revs = reviews(calls)
    assert revs
    # the changed unit, in full, with real line numbers
    assert sc["units"] <= {u for c in revs for u in (i["unit"] for i in kinds(c, "unit_diff"))}
    diff_text = "".join(t for c in revs for k, t in c["item_files"].items() if k.startswith("unit:"))
    for needle in sc.get("diff_text", []):
        assert needle in diff_text, needle
    # the caller checks, exactly
    tasks = {(t["callee"]["unit"], t["caller"]["unit"]) for c in revs for t in c["manifest"]["tasks"]}
    assert tasks == sc["tasks"], tasks
    callers = "".join(_context_text(c, i) for c in revs for i in kinds(c, "context") if i["role"] == "caller")
    for needle in sc.get("caller_text", []):
        assert needle in callers, needle
    # the unit index
    index = "".join(_context_text(c, i) for c in revs for i in kinds(c, "context") if i["role"] == "file_context")
    for needle in sc.get("index", []):
        assert needle in index, needle
    # cross-file callers: the impact bundle names them, whole-unit snippets
    sites = [(s["name"], s["path"], s["snippet"]) for c in revs
             for s in ((c["manifest"].get("impact") or {}).get("sites") or [])]
    for name, path in sc["impact"]:
        assert any(n == name and p == path for n, p, _ in sites), (name, path, sites)
    snippets = "".join(sn for _, _, sn in sites)
    for needle in sc.get("site_text", []):
        assert needle in snippets, needle
    if sc.get("tasks_cap"):
        assert len(tasks) == sc["tasks_cap"]
    # two changed units that must agree travel together
    for a, b in sc.get("same_chunk", []):
        together = [c for c in revs if {a, b} <= {i["unit"] for i in kinds(c, "unit_diff")}]
        assert together, (a, b, [[i["unit"] for i in kinds(c, "unit_diff")] for c in revs])
    # nothing in the worktree, and one verdict per task came back
    assert all(c["cwd_diffs"] == [] for c in revs)


def test_scenario_14_a_prepended_docstring_replays_every_function_at_its_new_line(tmp_path, gate_env):
    gate_env.setenv("STUB_UNIT_FINDINGS", json.dumps({
        "f7": {"severity": "medium", "content": "f7 is odd", "offset": 3},
        "f30": {"severity": "medium", "content": "f30 is odd", "offset": 5}}))
    work, tip = push_big(tmp_path, {"big.py": module(40)})
    st, calls = run(work, tip, tmp_path, gate_env)
    before = {f["content"]: f["start_line"] for f in result_findings(work)}
    doc = '"""\n' + "\n".join(f"line {i} of a long module docstring" for i in range(48)) + '\n"""\n'   # 50 lines
    tip2 = commit(work, {"big.py": doc + module(40)})
    drop_file_records(work, tip2)
    st, calls = run(work, tip2, tmp_path, gate_env)
    assert st["state"] == "done"
    names = unit_names(calls)
    assert not [n for n in names if n.startswith("f")]          # no function is reviewed again
    after = {f["content"]: f["start_line"] for f in result_findings(work)}
    assert after == {k: v + 50 for k, v in before.items()}, (before, after)
    # the docstring is code of its own: new regions, reviewed in ONE call (the one deviation from
    # the plan's "zero calls": text that was not there before is a change)
    assert len(names) == 2 and len(reviews(calls)) == 1


def test_impact_symbols_go_to_the_chunk_of_their_unit_and_a_site_shows_its_whole_unit(tmp_path, gate_env):
    gate_env.setattr(review_gate, "_PRECOMPUTED_MAX_LINES", 1)
    gate_env.setattr(review_gate, "_CHUNK_DIFF_LINES", 14)                 # about one changed unit per chunk
    base = "import os\n\n\n" + "\n\n".join(func(i, n=10) for i in range(6)) + "\n"
    tip = "import os\n\n\n" + "\n\n".join(
        func(i, n=10, extra="    z = 1\n" if i in (1, 3, 5) else "") for i in range(6)) + "\n"
    use = ("from big import f1, f3\n\n\ndef run_all():\n    first = f3(1)\n    second = f1(2)\n"
           "    return first + second\n\n\ndef unrelated():\n    return 0\n")
    work = rr._tiny_repo(tmp_path, {"keep.py": "x = 0\n", "big.py": base, "use.py": use})
    tip_sha = commit(work, {"big.py": tip})
    st, calls = run(work, tip_sha, tmp_path, gate_env)
    assert st["state"] == "done"
    revs = reviews(calls)
    assert len(revs) >= 3
    placed = {}
    for k, c in enumerate(revs):
        in_chunk = {i["unit"] for i in kinds(c, "unit_diff")}
        symbols = {s["name"] for s in ((c["manifest"].get("impact") or {}).get("symbols") or [])}
        assert symbols <= in_chunk, (symbols, in_chunk)           # a symbol rides with its unit's chunk
        for s in symbols:
            placed.setdefault(s, []).append(k)
    # (f5 has no call site anywhere, so no chunk carries a bundle for it)
    assert sorted(placed) == ["f1", "f3"] and all(len(v) == 1 for v in placed.values())
    sites = [s for c in revs for s in ((c["manifest"].get("impact") or {}).get("sites") or [])]
    assert {s["name"] for s in sites} == {"f1", "f3"}
    # the call inside run_all() shows the whole function; the import line (module code) keeps its +-6 lines
    wide = [s for s in sites if s.get("widened")]
    assert {s["name"] for s in wide} == {"f1", "f3"}
    assert all("def run_all" in s["snippet"] and "def unrelated" not in s["snippet"] for s in wide)
    assert all(not s.get("widened") for s in sites if s["snippet"].startswith("1: from big import"))


def test_a_unit_chunk_that_times_out_is_retried_in_smaller_pieces(tmp_path, gate_env):
    gate_env.setattr(review_gate, "_CHUNK_TIMEOUT", 2)
    gate_env.setenv("STUB_SLEEP_FOR", "big.py")
    gate_env.setenv("STUB_SLEEP_FOR_SECS", "6")
    work, tip = push_big(tmp_path, {"big.py": module(40)})
    st, calls1 = run(work, tip, tmp_path, gate_env)
    assert st["state"] == "failed" and st["reason"] == "timeout"
    assert len(kinds(calls1[0], "unit_diff")) == 41
    st, calls2 = run(work, tip, tmp_path, gate_env)
    assert st["state"] == "failed"
    assert 0 < len(kinds(calls2[0], "unit_diff")) <= 20            # half of what timed out


def test_old_unit_and_caller_check_records_are_pruned(tmp_path, monkeypatch):
    import time
    common = tmp_path / "common"
    fp = "a" * 64
    base = review_gate._fp_dir(str(common), fp)
    now = time.time()
    for sub in ("seg", "dep"):
        (base / sub).mkdir(parents=True)
        old = base / sub / "old.json"
        old.write_text("{}")
        os.utime(old, (now - review_gate._LEDGER_TTL - 100,) * 2)
        for n in range(5):
            f = base / sub / f"n{n}.json"
            f.write_text("{}")
            os.utime(f, (now - n * 10,) * 2)
    monkeypatch.setattr(review_gate, "_LEDGER_MAX_RECORDS", 1)
    monkeypatch.setattr(review_gate, "_SEG_RECORD_CAP_FACTOR", 3)    # at most 3 seg+dep records in all
    review_gate._prune_ledger(str(common))
    left = sorted(p.parent.name + "/" + p.name for sub in ("seg", "dep") for p in (base / sub).glob("*.json"))
    assert len(left) == 3 and not [x for x in left if "old" in x]
    assert {"seg/n0.json", "dep/n0.json"} <= set(left)               # the newest survive


def test_the_doctor_documents_the_part_s_flag():
    text = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "commands", "doctor.md"), encoding="utf-8").read()
    assert "OCR_SEGMENT" in text


def test_seg_records_cannot_be_forged_for_another_key_or_fingerprint(tmp_path):
    common, fp = str(tmp_path / "common"), "b" * 64
    key = review_gate._seg_key(fp, "python", "a.py", "h1", "h2")
    assert review_gate._write_seg_rec(common, fp, "seg", key, {"kind": "seg", "findings": []})
    assert review_gate._read_seg_rec(common, fp, "seg", key)["kind"] == "seg"
    assert review_gate._read_seg_rec(common, "c" * 64, "seg", key) is None
    other = review_gate._seg_key(fp, "python", "a.py", "h1", "h3")
    assert review_gate._read_seg_rec(common, fp, "seg", other) is None
    # a record planted under the right file name but with another key inside is refused
    path = review_gate._seg_rec_path(common, fp, "seg", other)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": 1, "fp": fp, "key": key, "findings": []}))
    assert review_gate._read_seg_rec(common, fp, "seg", other) is None
    # the key carries the version, the fingerprint, the language, the path hash, both unit hashes
    assert key.startswith(f"seg:{review_gate.SEG_VERSION}:{fp}:python:")
    assert key.endswith(":h1:h2") and review_gate._seg_key(fp, "python", "a.py", "", "h2").endswith(":-:h2")
    assert review_gate._seg_key(fp, "python", "b.py", "h1", "h2") != key        # the path is in the key
