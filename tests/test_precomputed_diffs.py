"""0.10.0 precomputed diffs: Python builds each file's diff, the reviewer reads it.

Unit tests drive the diff builder and the manifest builder against real git
repositories; the end-to-end tests run the real hook and detached supervisor
against the stub reviewer, which reads the manifest's diff files while the gate
still has them on disk (trace key `item_files`) and reports which `*.diff` files
exist inside its own working directory (`cwd_diffs`: the reviewed worktree).
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_async_gate as ag  # noqa: E402
import test_resolver_robustness as rr  # noqa: E402
from test_async_gate import review_gate  # noqa: E402

_STUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stub_reviewer.py")


def _git(args, cwd):
    return subprocess.run(["git", "-c", "core.autocrlf=false"] + args, cwd=str(cwd),
                          capture_output=True, text=True, check=True).stdout.strip()


def _init(tmp_path, name="repo"):
    work = tmp_path / name
    _git(["init", "-q", "-b", "main", str(work)], cwd=tmp_path)
    _git(["config", "user.email", "t@t.com"], cwd=work)
    _git(["config", "user.name", "t"], cwd=work)
    _git(["config", "commit.gpgsign", "false"], cwd=work)
    return work


def _commit_files(work, files, msg="c", binary=()):
    for name, content in files.items():
        p = work / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            with open(p, "w", encoding="utf-8", newline="") as fh:  # write_text(newline=) is 3.10+
                fh.write(content)
    _git(["add", "-A"], cwd=work)
    _git(["commit", "-q", "-m", msg], cwd=work)
    return _git(["rev-parse", "HEAD"], cwd=work)


def _items_between(work, base, tip, mode="full", records=None):
    entries, _ = review_gate._collect_diff_entries(str(work), base, tip)
    return [{"entry": e, "mode": mode, "record": None, "from_oid": "",
             "miss_reason": "no_record"} for e in entries]


def _build(work, base, tip, items, budget=None):
    return review_gate._build_diffs(str(work), base, tip, items, budget=budget)


# --- the diff builder ----------------------------------------------------------------------

def test_a_full_diff_is_built_with_context_and_both_sides(tmp_path):
    work = _init(tmp_path)
    base = _commit_files(work, {"a.py": "".join(f"line {i}\n" for i in range(20))})
    tip = _commit_files(work, {"a.py": "".join(
        f"line {i}\n" if i != 10 else "changed\n" for i in range(20))})
    items = _items_between(work, base, tip)
    d = _build(work, base, tip, items)["a.py"]
    assert not d["failed"] and d["level"] == "full" and not d["truncated"]
    assert "diff --git a/a.py b/a.py" in d["text"]
    assert "-line 10" in d["text"] and "+changed" in d["text"] and " line 9" in d["text"]
    assert d["changed"] == 2 and d["lines"] == d["text"].count("\n") + 1


def test_a_delta_diff_is_blob_to_blob_with_a_header(tmp_path):
    work = _init(tmp_path)
    v1 = _commit_files(work, {"a.py": "x = 1\n"})
    v2 = _commit_files(work, {"a.py": "x = 1\ny = 2\n"})
    v3 = _commit_files(work, {"a.py": "x = 1\ny = 2\nz = 3\n"})
    item = _items_between(work, v1, v3)[0]
    item["mode"] = "delta"
    item["from_oid"] = _git(["rev-parse", f"{v2}:a.py"], cwd=work)
    d = _build(work, v1, v3, [item])["a.py"]
    assert d["text"].splitlines()[0] == "# path: a.py (delta since last review)"
    assert "+z = 3" in d["text"] and "+y = 2" not in d["text"]


def test_a_rename_diff_passes_both_paths_so_git_pairs_them(tmp_path):
    work = _init(tmp_path)
    body = "".join(f"line {i}\n" for i in range(30))
    base = _commit_files(work, {"old_name.py": body})
    _git(["mv", "old_name.py", "new_name.py"], cwd=work)
    (work / "new_name.py").write_bytes((body + "added\n").encode("utf-8"))
    _git(["add", "-A"], cwd=work)
    _git(["commit", "-q", "-m", "mv"], cwd=work)
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    items = _items_between(work, base, tip)
    assert items[0]["entry"]["old_path"] == "old_name.py"
    d = _build(work, base, tip, items)["new_name.py"]
    assert "rename from old_name.py" in d["text"] and "rename to new_name.py" in d["text"]
    assert "+added" in d["text"] and "+line 0" not in d["text"]   # not a whole-file addition


def test_a_binary_file_is_flagged_and_gets_no_diff_file(tmp_path):
    work = _init(tmp_path)
    base = _commit_files(work, {"keep.py": "x = 1\n"})
    tip = _commit_files(work, {"blob.py": b"\x00\x01\x02 not text \x00" * 4})
    items = _items_between(work, base, tip)
    diffs = _build(work, base, tip, items)
    assert diffs["blob.py"]["binary"] and not diffs["blob.py"]["failed"]
    manifest = review_gate._build_review_manifest(
        str(work / ".git"), "r1", 0, 1, items, [], [], None, diffs)
    (item,) = [i for i in manifest["items"] if i["kind"] == "file_diff"]
    assert item["binary"] is True and item["file"] == ""
    assert review_gate._diff_warnings(diffs) and "binary" in review_gate._diff_warnings(diffs)[0]
    # Python established it, so the model's warnings about it are not consulted.
    assert diffs["blob.py"]["owned"] is True


def test_a_git_failure_means_no_diff_file_and_the_orchestrator_collects_it(tmp_path):
    work = _init(tmp_path)
    base = _commit_files(work, {"a.py": "x = 1\n"})
    tip = _commit_files(work, {"a.py": "x = 2\n"})
    items = _items_between(work, base, tip)
    for failing in (lambda args: ("", 1), lambda args: ("", 0)):   # error, and silence
        d = review_gate._build_item_diff(failing, review_gate._item_diff_spec(items[0], base, tip))
        assert d["failed"] and d["text"] == ""
    diffs = {"a.py": review_gate._failed_diff()}
    common = work / ".git"
    manifest = review_gate._build_review_manifest(
        str(common), "r1", 0, 1, items, [], [], None, diffs)
    (item,) = [i for i in manifest["items"] if i["kind"] == "file_diff"]
    assert item["file"] == "" and item["binary"] is False and item["path"] == "a.py"
    assert not diffs["a.py"].get("owned")
    assert not (Path(common) / review_gate.ASYNC_DIR / "run-r1").exists()   # nothing written


def test_diff_files_are_lf_even_for_a_crlf_file(tmp_path):
    work = _init(tmp_path)
    base = _commit_files(work, {"keep.py": "x = 1\n"})
    tip = _commit_files(work, {"win.py": "a = 1\r\nb = 2\r\nc = 3\r\n"})
    items = _items_between(work, base, tip)
    diffs = _build(work, base, tip, items)
    manifest = review_gate._build_review_manifest(
        str(work / ".git"), "r1", 0, 1, items, [], [], None, diffs)
    data = Path(manifest["items"][0]["file"]).read_bytes()
    assert b"\r" not in data and b"+a = 1\n+b = 2\n+c = 3\n" in data


def test_glob_character_and_non_ascii_paths_are_diffed_literally(tmp_path):
    work = _init(tmp_path)
    base = _commit_files(work, {"keep.py": "x = 1\n", "x.py": "decoy = 0\n"})
    tip = _commit_files(work, {"[x].py": "real = 1\n", "x.py": "decoy = 1\n", "café.py": "ok = 1\n"})
    items = _items_between(work, base, tip)
    diffs = _build(work, base, tip, items)
    assert "+real = 1" in diffs["[x].py"]["text"] and "decoy" not in diffs["[x].py"]["text"]
    assert "+ok = 1" in diffs["café.py"]["text"]
    assert "café.py" in diffs["café.py"]["text"]        # core.quotePath=false
    assert "decoy = 1" in diffs["x.py"]["text"] and "real" not in diffs["x.py"]["text"]


def test_diff_files_are_written_in_the_run_dir_with_generated_names(tmp_path):
    work = _init(tmp_path)
    base = _commit_files(work, {"keep.py": "x = 1\n"})
    tip = _commit_files(work, {"a.py": "a = 1\n", "sub/b.py": "b = 1\n"})
    items = _items_between(work, base, tip)
    diffs = _build(work, base, tip, items)
    common = work / ".git"
    manifest = review_gate._build_review_manifest(
        str(common), "run9", 3, 5, items, ["other.py"], ["carried.py"], {"impact": {"x": 1}}, diffs)
    assert (manifest["chunk_index"], manifest["chunks_total"]) == (3, 5)
    files = [i["file"] for i in manifest["items"] if i["kind"] == "file_diff"]
    base_dir = (common / review_gate.ASYNC_DIR / "run-run9" / "diffs" / "3").as_posix()
    assert files == [f"{base_dir}/000.diff", f"{base_dir}/001.diff"]
    assert {i["role"] for i in manifest["items"] if i["kind"] == "context"} == {
        "other_changed", "carried"}
    assert manifest["tasks"] == [] and manifest["impact"] == {"x": 1}
    # files[] keeps its 0.8/0.9 shape, exactly.
    assert manifest["files"][0] == {"path": items[0]["entry"]["path"],
                                    "mode": "full", "from_oid": "",
                                    "to_oid": items[0]["entry"]["new_oid"]}
    review_gate._remove_run_dir(str(common), "run9")
    assert not (common / review_gate.ASYNC_DIR / "run-run9").exists()


def test_run_dir_removal_refuses_anything_outside_the_async_dir(tmp_path):
    common = tmp_path / "common"
    (common / review_gate.ASYNC_DIR).mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    for bad in ("../../victim", "..", "a/b", ""):
        review_gate._remove_run_dir(str(common), bad)
    review_gate._remove_run_dir(str(common), "x", sub="../../../victim")
    assert (victim / "keep.txt").exists()


# --- limits ---------------------------------------------------------------------------------

def _long_file(n, width=0):
    return "".join(f"value_{i} = {i}{' ' * width}\n" for i in range(n))


def test_a_900_line_file_is_reviewed_in_full_untruncated(tmp_path):
    work = _init(tmp_path)
    base = _commit_files(work, {"keep.py": "x = 1\n"})
    tip = _commit_files(work, {"big.py": _long_file(900)})
    d = _build(work, base, tip, _items_between(work, base, tip))["big.py"]
    assert d["level"] == "full" and d["truncated"] is False
    assert d["text"].count("\n+value_") == 900 - 1 + 1   # every added line is there


def test_a_file_over_the_cap_degrades_to_u0_then_to_hunk_headers(tmp_path, monkeypatch):
    work = _init(tmp_path)
    base = _commit_files(work, {"m.py": "".join(f"row {i}\n" for i in range(400))})
    tip = _commit_files(work, {"m.py": "".join(
        f"row {i}\n" if i % 20 else f"CHANGED {i}\n" for i in range(400))})
    items = _items_between(work, base, tip)
    full = _build(work, base, tip, items)["m.py"]
    assert full["level"] == "full" and full["changed"] == 40
    # Past the limit with context lines but not without: -U0, every change still shown.
    monkeypatch.setattr(review_gate, "_PRECOMPUTED_MAX_BYTES", full["bytes"] - 50)
    u0 = _build(work, base, tip, items)["m.py"]
    assert u0["level"] == "u0" and u0["truncated"] is False
    assert "-row 20" in u0["text"] and "+CHANGED 20" in u0["text"] and "\n row 19" not in u0["text"]
    assert "# context lines omitted" in u0["text"]
    # Past the limit even so: stat + hunk headers, flagged truncated.
    monkeypatch.setattr(review_gate, "_PRECOMPUTED_MAX_BYTES", 400)
    stat = _build(work, base, tip, items)["m.py"]
    assert stat["level"] == "stat" and stat["truncated"] is True
    assert "@@" in stat["text"] and "CHANGED" not in stat["text"] and "-row" not in stat["text"]
    assert "# diff truncated" in stat["text"]
    assert stat["text"].count("\n@@") == 0 or stat["text"].count("@@") >= 40
    # The line limit alone does it too.
    monkeypatch.setattr(review_gate, "_PRECOMPUTED_MAX_BYTES", 64 * 1024)
    monkeypatch.setattr(review_gate, "_PRECOMPUTED_MAX_LINES", 39)
    assert _build(work, base, tip, items)["m.py"]["level"] == "stat"


def test_a_line_over_the_cap_is_cut_and_counts_as_truncated(tmp_path):
    work = _init(tmp_path)
    base = _commit_files(work, {"keep.py": "x = 1\n"})
    tip = _commit_files(work, {"min.py": "a = 1\n" + "x" * 2000 + "\nb = 2\n"})
    d = _build(work, base, tip, _items_between(work, base, tip))["min.py"]
    assert d["truncated"] is True and d["level"] == "full"
    assert "...[cut 1501 chars]" in d["text"] and "x" * 501 not in d["text"]
    assert "+b = 2" in d["text"]


def test_a_file_that_cannot_fit_the_chunk_budget_degrades_first(tmp_path):
    work = _init(tmp_path)
    base = _commit_files(work, {"m.py": "".join(f"row {i}\n" for i in range(300))})
    tip = _commit_files(work, {"m.py": "".join(
        f"row {i}\n" if i % 10 else f"CHANGED {i}\n" for i in range(300))})
    items = _items_between(work, base, tip)
    roomy = _build(work, base, tip, items, budget=5000)["m.py"]
    tight = _build(work, base, tip, items, budget=roomy["lines"] - 5)["m.py"]
    assert roomy["level"] == "full" and tight["level"] != "full"
    assert tight["lines"] <= roomy["lines"] - 5 or tight["level"] == "stat"


def test_chunks_are_packed_by_delivered_diff_lines_within_the_budget():
    def entry(name):
        return {"path": name, "lines": 1}
    entries = [entry(f"f{i}.py") for i in range(6)]
    sizes = {"f0.py": 40, "f1.py": 40, "f2.py": 40, "f3.py": 100, "f4.py": 10, "f5.py": 10}
    chunks = review_gate._group_into_chunks(entries, sizes, 100)
    assert [[e["path"] for e in c] for c in chunks] == [
        ["f0.py", "f1.py"], ["f2.py"], ["f3.py"], ["f4.py", "f5.py"]]
    for c in chunks:
        total = sum(sizes[e["path"]] for e in c)
        assert total <= 100 or len(c) == 1
    # Without sizes the 0.9.x packing by changed lines is untouched.
    assert len(review_gate._group_into_chunks(entries)) == 1


def test_truncated_reaches_the_flagged_record_and_models_cannot_change_it(tmp_path):
    work = _init(tmp_path)
    base = _commit_files(work, {"keep.py": "x = 1\n"})
    tip = _commit_files(work, {"a.py": "a = 1\n", "b.py": "b = 1\n", "c.py": "c = 1\n"})
    items = _items_between(work, base, tip)
    common, fp = str(tmp_path / "common"), "f" * 64
    star = {"status": "completed_with_warnings", "findings": [],
            "warnings": [{"file": "b.py", "message": "diff truncated; reviewer saw stat + hunk headers only"},
                         {"file": None, "message": "diff truncated; reviewer saw stat + hunk headers only"}]}
    # a.py: Python truncated it. b.py: Python delivered it whole, the model's warning is ignored.
    # c.py: no precomputed diff, so the model's unnamed "*" warning still counts for it.
    flagged = review_gate._write_run_records(
        star, items, common, fp, "r1", precomputed={"a.py": True, "b.py": False})
    assert flagged == {"a.py", "c.py"}
    by_path = {i["entry"]["path"]: i for i in items}
    for path, want in (("a.py", True), ("b.py", False), ("c.py", True)):
        rec = ag._record(common, fp, by_path[path]["entry"])
        assert review_gate._is_truncated_record(rec) is want, path
    # With every file precomputed an unnamed "*" has nothing to be about.
    fp2 = "e" * 64
    assert review_gate._write_run_records(
        star, items, common, fp2, "r2",
        precomputed={"a.py": False, "b.py": False, "c.py": False}) == set()
    assert review_gate._truncated_paths(star, {"b.py"}) == {"*"}
    assert review_gate._truncated_paths({"warnings": star["warnings"][:1]}, {"b.py"}) == set()


# --- end to end -----------------------------------------------------------------------------

def _run_dirs(work):
    common = Path(review_gate._git_common_dir(str(work)))
    return sorted((common / review_gate.ASYNC_DIR).glob("run-*"))


def _calls(tmp_path, trace="stub.trace"):
    return rr._trace(tmp_path, trace)


def _push_ok(work, env, cmd="git push origin main"):
    decision, reason, _, _ = ag._hook(work, cmd, env, timeout=120)
    assert decision == "pass", reason


def test_a_small_push_always_gets_a_manifest_and_the_reviewer_reads_the_diff(tmp_path):
    work = ag._big_repo(tmp_path, n_files=2)
    tip = ag._git(["rev-parse", "HEAD"], cwd=work)
    _push_ok(work, ag._env(tmp_path))
    ag._wait_state(work, tip, {"done"})
    (call,) = [c for c in _calls(tmp_path) if not c.get("resolve_file")]
    assert call["paths_file"] is not None                 # the old "golden" no-manifest case is gone
    items = [i for i in call["manifest"]["items"] if i["kind"] == "file_diff"]
    assert sorted(i["path"] for i in items) == ["mod0.py", "mod1.py"]
    for i in items:
        text = call["item_files"][f"review:{i['path']}"]
        n = i["path"][3]
        assert f"diff --git a/{i['path']} b/{i['path']}" in text and f"+x = {n}" in text
        assert i["file"] and ".git/review-gate-async/run-" in i["file"]
        assert i["lines"] > 0 and i["truncated"] is False and i["binary"] is False
    assert call["manifest"]["tasks"] == []
    # The files outlive the call by no more than the run.
    assert _run_dirs(work) == []
    # And the reviewer's own working directory (the worktree) never held one.
    assert call["cwd_diffs"] == []


def test_ocr_precomputed_diffs_0_restores_the_old_path_exactly(tmp_path):
    work = ag._big_repo(tmp_path, n_files=2)
    tip = ag._git(["rev-parse", "HEAD"], cwd=work)
    _push_ok(work, ag._env(tmp_path, OCR_PRECOMPUTED_DIFFS="0"))
    ag._wait_state(work, tip, {"done"})
    (call,) = _calls(tmp_path)
    assert call["paths_file"] is None and call["manifest"] is None   # golden argv, no manifest
    assert _run_dirs(work) == []


def test_with_the_flag_off_a_chunked_manifest_has_no_items(tmp_path):
    work = ag._big_repo(tmp_path, n_files=4)
    tip = ag._git(["rev-parse", "HEAD"], cwd=work)
    _push_ok(work, ag._chunk_env(tmp_path, OCR_PRECOMPUTED_DIFFS="0"))
    ag._wait_state(work, tip, {"done"})
    manifests = [c["manifest"] for c in _calls(tmp_path)]
    assert len(manifests) == 4
    for m in manifests:
        assert "items" not in m and "tasks" not in m
        assert set(m) >= {"chunk_index", "chunks_total", "paths", "renames", "other_changed",
                          "files", "carried"}


def test_chunks_carry_their_own_diff_files_and_clean_up(tmp_path):
    work = ag._big_repo(tmp_path, n_files=4)
    tip = ag._git(["rev-parse", "HEAD"], cwd=work)
    _push_ok(work, ag._chunk_env(tmp_path))
    ag._wait_state(work, tip, {"done"})
    calls = _calls(tmp_path)
    assert len(calls) == 4
    seen = set()
    for c in calls:
        m = c["manifest"]
        (item,) = [i for i in m["items"] if i["kind"] == "file_diff"]
        assert f"/diffs/{m['chunk_index']}/000.diff" in item["file"]
        assert f"+x = {item['path'][3]}" in c["item_files"][f"review:{item['path']}"]
        others = {i["path"] for i in m["items"] if i["kind"] == "context"}
        assert others == set(m["other_changed"]) and item["path"] not in others
        seen.add(item["path"])
    assert len(seen) == 4 and _run_dirs(work) == []


def test_a_900_line_file_reaches_the_reviewer_untruncated_end_to_end(tmp_path):
    work = rr._tiny_repo(tmp_path, {"keep.py": "x = 0\n"})
    (work / "big.py").write_text(_long_file(900), encoding="utf-8")
    ag._git(["add", "."], cwd=work)
    ag._git(["commit", "-q", "-m", "big"], cwd=work)
    tip = ag._git(["rev-parse", "HEAD"], cwd=work)
    decision, reason = rr._hook(work, rr._env(tmp_path), cmd="git push origin main")
    assert decision == "pass", reason
    st = rr._state(work, tip)
    (call,) = _calls(tmp_path)
    (item,) = [i for i in call["manifest"]["items"] if i["kind"] == "file_diff"]
    assert item["truncated"] is False and item["level"] == "full"
    assert call["item_files"]["review:big.py"].count("\n+value_") == 900
    assert not st.get("unreviewed_truncated")
    # The model saying nothing about truncation and Python agreeing: a complete record.
    common = review_gate._git_common_dir(str(work))
    fp = review_gate._compute_fingerprint(str(work), tip)
    base = ag._git(["rev-parse", "origin/main"], cwd=work)
    entries, _ = review_gate._collect_diff_entries(str(work), base, tip)
    rec = ag._record(common, fp, entries[0])
    assert rec is not None and not review_gate._is_truncated_record(rec)


def test_a_file_over_the_cap_is_flagged_truncated_in_the_record_end_to_end(tmp_path):
    # Part B's own degradation, which since 0.11.0 is what OCR_SEGMENT=0 restores (and what a
    # file that cannot be segmented still gets): tests/test_segment_gate.py has the units.
    work = rr._tiny_repo(tmp_path, {"keep.py": "x = 0\n"})
    (work / "huge.py").write_text(_long_file(1600), encoding="utf-8")
    ag._git(["add", "."], cwd=work)
    ag._git(["commit", "-q", "-m", "huge"], cwd=work)
    tip = ag._git(["rev-parse", "HEAD"], cwd=work)
    decision, reason = rr._hook(work, rr._env(tmp_path, OCR_SEGMENT="0"), cmd="git push origin main")
    assert decision == "pass", reason
    st = rr._state(work, tip)
    (call,) = _calls(tmp_path)
    (item,) = [i for i in call["manifest"]["items"] if i["kind"] == "file_diff"]
    assert item["truncated"] is True and item["level"] == "stat"
    text = call["item_files"]["review:huge.py"]
    assert "@@" in text and "+value_5 " not in text and "# diff truncated" in text
    assert st.get("unreviewed_truncated") == 1
    common = review_gate._git_common_dir(str(work))
    fp = review_gate._compute_fingerprint(str(work), tip)
    base = ag._git(["rev-parse", "origin/main"], cwd=work)
    entries, _ = review_gate._collect_diff_entries(str(work), base, tip)
    assert review_gate._is_truncated_record(ag._record(common, fp, entries[0]))


def test_a_model_truncation_warning_about_a_precomputed_file_is_ignored(tmp_path):
    work = rr._tiny_repo(tmp_path, {"keep.py": "x = 0\n"})
    (work / "small.py").write_text("a = 1\n", encoding="utf-8")
    ag._git(["add", "."], cwd=work)
    ag._git(["commit", "-q", "-m", "small"], cwd=work)
    tip = ag._git(["rev-parse", "HEAD"], cwd=work)
    decision, reason = rr._hook(work, rr._env(tmp_path, STUB_TRUNCATE_FOR="*"),
                                cmd="git push origin main")
    assert decision == "pass", reason
    st = rr._state(work, tip)
    assert st["state"] == "done" and not st.get("unreviewed_truncated")


# --- security: the reviewed tree is hostile --------------------------------------------------

def _hostile_push(tmp_path, files, symlink_to=None):
    work = rr._tiny_repo(tmp_path, {"keep.py": "x = 0\n"})
    for name, content in files.items():
        p = work / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    if symlink_to is not None:
        os.symlink(str(symlink_to), str(work / ".review-gate"))
    ag._git(["add", "-A"], cwd=work)
    ag._git(["commit", "-q", "-m", "hostile"], cwd=work)
    tip = ag._git(["rev-parse", "HEAD"], cwd=work)
    decision, reason = rr._hook(work, rr._env(tmp_path), cmd="git push origin main")
    assert decision == "pass", reason
    rr._state(work, tip)
    return work, _calls(tmp_path)


def test_a_tracked_review_gate_directory_is_reviewed_like_any_other_path(tmp_path):
    work, calls = _hostile_push(tmp_path, {".review-gate/diffs/0/000.diff": "fake\n",
                                           ".review-gate/evil.py": "evil = 1\n",
                                           "ok.py": "ok = 1\n"})
    (call,) = calls
    paths = {i["path"] for i in call["manifest"]["items"] if i["kind"] == "file_diff"}
    assert {".review-gate/evil.py", "ok.py"} <= paths
    assert "+evil = 1" in call["item_files"]["review:.review-gate/evil.py"]
    assert "fake" not in "".join(call["item_files"].values())
    assert all(".git/review-gate-async/run-" in i["file"]
               for i in call["manifest"]["items"] if i["kind"] == "file_diff")
    # The only *.diff in the reviewed worktree is the one the branch itself tracks.
    assert [Path(p).relative_to(Path(call["cwd"])).as_posix() for p in call["cwd_diffs"]] == [
        ".review-gate/diffs/0/000.diff"]


def test_a_tracked_file_named_review_gate_is_not_written_through_or_excluded(tmp_path):
    work, calls = _hostile_push(tmp_path, {".review-gate": "i am a file\n", "ok.py": "ok = 1\n"})
    (call,) = calls
    # The path has no reviewable extension, so the allowlist (not the gate's own
    # files) decides, exactly as for any other such file; the others are untouched.
    assert [i["path"] for i in call["manifest"]["items"] if i["kind"] == "file_diff"] == ["ok.py"]
    tracked = ag._git(["ls-files"], cwd=work).split()
    assert ".review-gate" in tracked
    assert (work / ".review-gate").read_text(encoding="utf-8") == "i am a file\n"
    assert call["cwd_diffs"] == []


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_tracked_symlink_review_gate_is_not_written_through(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    work, calls = _hostile_push(tmp_path, {"ok.py": "ok = 1\n"}, symlink_to=outside)
    (call,) = calls
    assert [i["path"] for i in call["manifest"]["items"] if i["kind"] == "file_diff"] == ["ok.py"]
    assert list(outside.iterdir()) == []                 # nothing written through the link
    assert call["cwd_diffs"] == []


# --- the resolver -------------------------------------------------------------------------------

def test_the_resolver_manifest_keeps_files_and_gains_items(tmp_path):
    work, _ = rr._blocked_on_x(tmp_path)
    rr._amend(work, {"x.py": "x = 1\ndef good(): pass\n"})
    decision, reason = rr._hook(work, rr._env(tmp_path, "t2.trace"))
    resolver = next(c for c in rr._trace(tmp_path, "t2.trace") if c.get("resolve_file"))
    m = resolver["resolve_manifest"]
    assert [f["path"] for f in m["files"]] == ["x.py"]
    assert set(m["files"][0]) == {"path", "mode", "from_oid", "to_oid"}     # shape unchanged
    roles = {i["role"] for i in m["items"]}
    assert roles == {"active", "since"}
    active = next(i for i in m["items"] if i["role"] == "active")
    since = next(i for i in m["items"] if i["role"] == "since")
    assert active["path"] == "x.py" and active["file"]
    assert "+def good(): pass" in resolver["item_files"]["active:x.py"]
    assert "# path: x.py (changes since the finding was raised)" in resolver["item_files"]["since:x.py"]
    assert "-def bad(): pass" in resolver["item_files"]["since:x.py"] and since["file"]
    assert _run_dirs(work) == []


def test_without_precomputed_diffs_the_resolver_manifest_has_no_items(tmp_path):
    work, _ = rr._blocked_on_x(tmp_path)
    rr._amend(work, {"x.py": "x = 1\ndef good(): pass\n"})
    rr._hook(work, rr._env(tmp_path, "t2.trace", OCR_PRECOMPUTED_DIFFS="0"))
    resolver = next(c for c in rr._trace(tmp_path, "t2.trace") if c.get("resolve_file"))
    assert "items" not in resolver["resolve_manifest"]


# --- reaping ----------------------------------------------------------------------------------------

def test_the_reaper_removes_the_diffs_of_dead_runs_and_keeps_live_ones(tmp_path):
    common = tmp_path / "common"
    async_d = common / review_gate.ASYNC_DIR
    (async_d / "run-dead" / "diffs" / "0").mkdir(parents=True)
    (async_d / "run-dead" / "diffs" / "0" / "000.diff").write_text("x")
    (async_d / "run-live" / "diffs" / "0").mkdir(parents=True)
    (async_d / "run-live" / "diffs" / "0" / "000.diff").write_text("x")
    (async_d / "run-young" / "diffs").mkdir(parents=True)
    old = time.time() - review_gate.MARKER_TTL - 60
    for name in ("run-dead", "run-live"):
        os.utime(async_d / name, (old, old))
    review_gate._write_state(async_d / "tip.json", {
        "state": "running", "run_id": "live", "heartbeat_ts": time.time()})
    review_gate._reap_async(str(common))
    assert not (async_d / "run-dead").exists()
    assert (async_d / "run-live").exists() and (async_d / "run-young").exists()
