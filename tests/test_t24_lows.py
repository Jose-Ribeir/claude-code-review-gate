"""t24 low-bucket regression tests for scripts/review-gate.py.

Each test names the cycle-3 finding line it pins.
"""
import importlib.util
import os
import sys
import time

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)

_spec = importlib.util.spec_from_file_location("review_gate", os.path.join(_SCRIPTS, "review-gate.py"))
rg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rg)


# --- 337: _env_int -----------------------------------------------------------

def test_env_int_defaults_and_floors(monkeypatch):
    monkeypatch.delenv("OCR_T24", raising=False)
    assert rg._env_int("OCR_T24", 42, floor=5) == 42
    monkeypatch.setenv("OCR_T24", "")
    assert rg._env_int("OCR_T24", 42, floor=5) == 42
    monkeypatch.setenv("OCR_T24", "garbage")
    assert rg._env_int("OCR_T24", 42, floor=5) == 42
    monkeypatch.setenv("OCR_T24", "3")
    assert rg._env_int("OCR_T24", 42, floor=5) == 5
    monkeypatch.setenv("OCR_T24", "100")
    assert rg._env_int("OCR_T24", 42, floor=5) == 100


# --- 1367: _record_review one O_APPEND write ---------------------------------

def test_record_review_large_record_is_one_json_line(tmp_path):
    git_dir = str(tmp_path / ".git")
    finding = {"severity": "low", "path": "x.py", "start_line": "1",
               "end_line": "1", "content": "z" * 40000}
    path = rg._record_review(git_dir, "a" * 40, "main", "git", "pass",
                             False, False, {"findings": [finding]})
    assert path is not None
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    import json as _json
    rec = _json.loads(lines[0])
    assert rec["head"] == "a" * 40
    assert rec["findings"][0]["content"] == "z" * 40000


# --- 1665: multiple heredoc openers on one line --------------------------------

def test_strip_heredocs_drops_all_bodies_of_one_line():
    cmd = "cat <<A <<B\nbody-a\nA\ngit push origin main\nB\necho done"
    out = rg._strip_heredocs(cmd)
    assert "git push" not in out  # second heredoc's body must also be dropped
    assert "echo done" in out


def test_strip_heredocs_missing_terminator_strips_nothing():
    cmd = "cat <<A <<B\nbody-a\nA\ngit push origin main"
    out = rg._strip_heredocs(cmd)
    assert "git push" in out  # B has no terminator: conservative, keep all


# --- 1972: bare `git branch` is a listing -------------------------------------

def test_pre_push_git_commands_bare_branch_is_listing():
    assert rg._pre_push_git_commands("git branch && git push origin main") == []
    assert rg._pre_push_git_commands("git branch -l && git push origin main") == []
    assert rg._pre_push_git_commands("git branch -d feat && git push origin main") == ["branch"]


# --- 6343: added lines starting with '++' stay in the added corpus -------------

def test_quote_in_added_lines_keeps_double_plus_content(monkeypatch):
    diff = ("diff --git a/x.c b/x.c\n--- a/x.c\n+++ b/x.c\n@@ -1,1 +1,2 @@\n"
            " context\n+for (;;)\n+++i;\n")
    monkeypatch.setattr(rg, "_git", lambda args, cwd=None: (diff, 0))
    assert rg._quote_in_added_lines(["diff"], "++i;", "/repo") is True


# --- 5540: cat-file --batch parser skips non-blob object bodies ----------------

def test_cat_blob_texts_skips_non_blob_objects(monkeypatch):
    body = b"blob-one\n"
    stream = (
        b"a" * 40 + b" tag 12\nnotablobbody\n"          # 12-byte tag body + \n
        + b"b" * 40 + b" blob 9\n" + body + b"\n"
    )

    class _P:
        returncode = 0
        stdout = stream

    monkeypatch.setattr(rg.subprocess, "run", lambda *a, **k: _P())
    out = rg._cat_blob_texts("/repo", ["a" * 40, "b" * 40])
    assert out == {"b" * 40: "blob-one\n"}


# --- 5169: _finding_id tolerates malformed field types -------------------------

def test_finding_id_malformed_fields_do_not_crash():
    fid = rg._finding_id({"path": ["a.py"], "content": 5, "existing_code": None})
    assert isinstance(fid, str) and fid


# --- 7766: unknown chunk status fails closed -----------------------------------

def test_merge_chunk_results_unknown_status_is_errors():
    merged = rg._merge_chunk_results([{"findings": [], "warnings": []}])  # no status key
    assert merged["status"] == "completed_with_errors"
    merged = rg._merge_chunk_results([{"status": "success", "findings": [], "warnings": []}])
    assert merged["status"] == "success"


# --- 9421: _chunk_progress never renders chunk N+1/N -----------------------------

def test_chunk_progress_done_equals_total_while_running():
    assert rg._chunk_progress({"chunks_done": 10, "chunks_total": 10, "chunks_running": 0}) \
        == ", chunks 10/10 done"
    assert rg._chunk_progress({"chunks_done": 9, "chunks_total": 10, "chunks_running": 1}) \
        == ", chunk 10/10"


# --- 6565: corrupt timeout-mark field types read as 0 ---------------------------

def test_mark_int_corrupt_values():
    assert rg._mark_int({"timeout_s": "soon"}, "timeout_s") == 0
    assert rg._mark_int({}, "attempts") == 0
    assert rg._stuck_paths({"a.py": {"solo_attempts": "x"}}) == []
    assert rg._stuck_paths({"a.py": {"solo_attempts": 2}}) == ["a.py"]


# --- 6570: stuck-marking policy --------------------------------------------------

def _mk_items(n, oid="1" * 40):
    return [{"entry": {"path": f"f{i}.py", "old_path": "", "status": "M",
                       "old_oid": "0" * 40, "new_oid": oid}} for i in range(n)]


def test_note_chunk_timeout_solo_twice_marks_stuck(tmp_path):
    common, fp = str(tmp_path / "common"), "f" * 32
    one = _mk_items(1)
    assert rg._note_chunk_timeout(common, fp, one, 100) == ""     # first solo timeout
    assert rg._note_chunk_timeout(common, fp, one, 100) == "f0.py"  # second: stuck


def test_note_chunk_timeout_multi_member_never_solo(tmp_path):
    common, fp = str(tmp_path / "common"), "f" * 32
    two = _mk_items(2)
    assert rg._note_chunk_timeout(common, fp, two, 100) == ""
    assert rg._note_chunk_timeout(common, fp, two, 100) == ""  # not solo: no escalation


# --- 6476: fp-dir prune sees timeouts/ subdir mtime -------------------------------

def test_prune_ledger_keeps_dir_with_fresh_timeout_marks(tmp_path):
    fp_dir = tmp_path / rg.LEDGER_DIR / "a1b2c3"
    (fp_dir / "timeouts").mkdir(parents=True)
    old = time.time() - rg._LEDGER_TTL - 3600
    os.utime(fp_dir, (old, old))  # stale by its own mtime
    rg._prune_ledger(str(tmp_path))
    assert fp_dir.is_dir()  # fresh timeouts/ subdir protects it
    os.utime(fp_dir / "timeouts", (old, old))
    rg._prune_ledger(str(tmp_path))
    assert not fp_dir.is_dir()  # whole tree stale: pruned


# --- 6397: _guard_resolution uses the caller's batch caches ----------------------

def test_guard_resolution_batch_oids_path():
    res = {"status": "resolved", "evidence_path": "x.py", "evidence_quote": "q" * 40}
    prior = {"finding": {"path": "x.py"}, "target_oid": "t0"}
    # tip blob == target blob: the fallback branch refuses before any git call
    assert rg._guard_resolution(res, [], "", "/repo", prior=prior, tip="tip",
                                tip_oids={"x.py": "t0"}) is False


# --- 9618: _replay_note emits no duplicate findings ------------------------------

def test_replay_note_head_only():
    st = {"done_ts": time.time() - 120, "verdict": "pass",
          "reasons": "  [low] a.py:1 - something"}
    note = rg._replay_note(st)
    assert "2 min ago" in note and "verdict: pass" in note
    assert "something" not in note  # the reasons are printed by the caller


# --- 10449/10458: --print-range -------------------------------------------------

def test_print_range_base_v1_resolves_to_merge_base(monkeypatch, capsys):
    monkeypatch.setattr(rg, "_base_v1", lambda: True)
    monkeypatch.setattr(rg, "_head_sha", lambda repo_root=None: "t" * 40)

    def fake_git(args, cwd=None):
        if args[0] == "rev-parse":
            return ("r" * 40, 0)
        if args[0] == "merge-base":
            return ("b" * 40, 0)
        return ("", 1)

    monkeypatch.setattr(rg, "_git", fake_git)
    assert rg._print_range("/repo") == 0
    assert capsys.readouterr().out == "b" * 40 + ".." + "t" * 40 + "\n"


def test_print_range_no_upstream_exits_1(monkeypatch):
    monkeypatch.setattr(rg, "_base_v1", lambda: True)
    monkeypatch.setattr(rg, "_head_sha", lambda repo_root=None: "t" * 40)
    monkeypatch.setattr(rg, "_git", lambda args, cwd=None: ("", 1))
    assert rg._print_range("/repo") == 1


# --- 5127: shared allowlist + ceiling helper -------------------------------------

def test_cap_reviewable_filter_and_ceiling(monkeypatch):
    monkeypatch.setattr(rg, "_MAX_FILES", 2)
    entries = [{"path": f"a{i}.py", "lines": i} for i in range(5)]
    entries.append({"path": "notes.txt", "lines": 99})  # not reviewable
    warnings = []
    allowed = rg._cap_reviewable(entries, warnings)
    assert [e["path"] for e in allowed] == ["a4.py", "a3.py"]
    assert warnings and "file ceiling" in warnings[0]


# --- 7497: size check alone flags a >400-line file (no reviewer warning) ----------

def test_diff_exceeds_caps_line_cap_alone():
    item = {"entry": {"path": "big.py", "old_path": "", "status": "M",
                      "old_oid": "0" * 40, "new_oid": "1" * 40,
                      "lines": rg._TRUNC_MAX_LINES + 1},
            "mode": "full"}
    assert rg._diff_exceeds_caps(item, review_root="") is True
    item["entry"]["lines"] = rg._TRUNC_MAX_LINES
    assert rg._diff_exceeds_caps(item, review_root="") is False


# --- 6924: fit checks budget the prepended notes --------------------------------

def test_build_item_diff_notes_counted_in_fit(monkeypatch):
    old = rg._PRECOMPUTED_MAX_BYTES
    text = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,2 @@\n +1\n" + "+x = 1\n" * 20
    header = "# path: x.py (delta since last review)"
    # Just under the cap WITHOUT notes, over WITH them -> must degrade to u0/stat.
    monkeypatch.setattr(rg, "_PRECOMPUTED_MAX_BYTES", len(text.encode()) + 10)
    runs = {"n": 0}

    def run(args):
        runs["n"] += 1
        return text, 0

    try:
        res = rg._build_item_diff(run, {"path": "x.py", "range": "a..b",
                                        "paths": ["x.py"], "header": header})
        assert res["level"] in ("u0", "stat")  # the header blew the budget
        assert res["text"].startswith(header)
    finally:
        monkeypatch.setattr(rg, "_PRECOMPUTED_MAX_BYTES", old)


# --- user-chosen judgement fixes ---------------------------------------------

def test_reanchor_prefers_the_occurrence_nearest_the_stale_line(monkeypatch):
    """Duplicated block: anchor to nearest, not blindly first (finding 6463)."""
    body = "x = 1\ny = 2\nz = 3\nx = 1\nw = 9\nx = 1\n"
    monkeypatch.setattr(rg, "_git",
                        lambda a, cwd=None: (body, 0) if a[:2] == ["show", "t:p"] else ("", 1))
    f, ok = rg._reanchor_finding(
        {"path": "p", "existing_code": "x = 1", "start_line": 4, "end_line": 4}, ".", "t")
    assert ok and (f["start_line"], f["end_line"]) == (4, 4)   # nearest is #3... line 4
    f2, ok2 = rg._reanchor_finding(
        {"path": "p", "existing_code": "x = 1", "start_line": 6, "end_line": 6}, ".", "t")
    assert ok2 and (f2["start_line"], f2["end_line"]) == (6, 6)
    f3, ok3 = rg._reanchor_finding(
        {"path": "p", "existing_code": "x = 1", "start_line": 1, "end_line": 1}, ".", "t")
    assert ok3 and (f3["start_line"], f3["end_line"]) == (1, 1)


def test_diff_exceeds_caps_measures_blob_sizes_not_the_painted_diff(monkeypatch):
    """7524: bounded-memory measurement via cat-file -s; conservative over count."""
    calls = []
    def fake_git(a, cwd=None):
        calls.append(a)
        if a[:2] == ["cat-file", "-s"]:
            return "\n".join("9000" for _ in a[2:]), 0
        return "", 0
    monkeypatch.setattr(rg, "_git", fake_git)
    item = {"entry": {"lines": 10, "old_oid": "a" * 40, "new_oid": "b" * 40,
                      "path": "big.py"}, "mode": "full"}
    assert rg._diff_exceeds_caps(item, ".") is True   # 9000+9000 > 16k
    assert calls and calls[-1][0] == "cat-file"                # no in-memory diff
    item2 = {"entry": {"lines": 10, "old_oid": "0" * 40, "new_oid": "b" * 40,
                       "path": "small.py"}, "mode": "full"}
    monkeypatch.setattr(rg, "_git", lambda a, cwd=None: ("100", 0))
    assert rg._diff_exceeds_caps(item2, ".") is False  # 100 < 16k
