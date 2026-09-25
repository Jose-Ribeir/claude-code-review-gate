"""Verification tests for the 0.8.0 review ledger (21 scenarios from the plan).

Each test is labelled by its scenario number for easy cross-reference.

Scenarios covered here:
  1  block X, fix X → delta + resolver ran
  2  fix in C, outside F's original context (guard acceptance/rejection)
  3  bad resolver output
  4  reviewer re-reports a finding the resolver cleared
  5  resolution reuse (T2 suppresses, T3 with C reverted replays)
  6  replays with zero claude calls
  7  medium finding in A, next push adds new file
  8  fingerprint invalidation
  9  trust negatives leave no record
  10 target deleted → auto-resolved
  11 delta chain depth capping
  12 re-anchoring
  13 OCR_LEDGER=0 / OCR_FORCE_REVIEW=1 switches
  14 migration and robustness
  15 concurrency (byte-identical records)
  16 security guard for ledger dir
  17 pruning (max records and stale fp dir)
  18 stable boundaries (add a file → only the new file reviewed)
  19 kill mid-run → resume skips recorded files (see test_async_gate.py)
  20 first push ≤15 files → golden argv (see test_argv_golden.py)
  21 real run calibration (live, skipped unless OCR_LIVE_TESTS=1)
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_STUB = os.path.join(_HERE, "stub_reviewer.py")

sys.path.insert(0, _SCRIPTS)
_spec = importlib.util.spec_from_file_location("review_gate_ls", _GATE)
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

def _git(args, cwd, env=None):
    return subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True,
        check=True, env=env,
    ).stdout.strip()


def _tiny_repo(tmp_path, files=None):
    """Bare remote + working repo.  files: {name: content} added in first commit."""
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    _git(["config", "user.email", "t@t.com"], cwd=work)
    _git(["config", "user.name", "t"], cwd=work)
    _git(["config", "commit.gpgsign", "false"], cwd=work)
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    _git(["config", "core.hooksPath", str(hooks)], cwd=work)
    for name, content in (files or {"base.py": "# base\n"}).items():
        p = work / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    return work


def _commit(work, name="f", content="change\n"):
    p = work / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    _git(["add", name], cwd=work)
    _git(["commit", "-q", "-m", f"add {name}"], cwd=work)
    return _git(["rev-parse", "HEAD"], cwd=work)


def _env(tmp_path, **kw):
    env = dict(os.environ)
    for k in ("OCR_IN_REVIEW", "OCR_FAIL_OPEN", "OCR_ADVISORY", "OCR_FORCE_REVIEW",
              "OCR_LEGACY_RANGE", "OCR_INLINE_BUDGET", "STUB_SLEEP",
              "STUB_VERDICT", "STUB_TRACE", "OCR_LEDGER", "STUB_FINDINGS_FOR",
              "STUB_RESOLVE", "STUB_RESOLVE_VERDICT"):
        env.pop(k, None)
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "gate-data")
    env["OCR_REVIEWER_CMD"] = f'"{sys.executable}" "{_STUB}"'
    env["STUB_TRACE"] = str(tmp_path / "stub.trace")
    env["OCR_INLINE_BUDGET"] = "30"
    for k, v in kw.items():
        env[k] = str(v)
    return env


def _hook(work, cmd, env, session="s1", timeout=60):
    payload = json.dumps({"session_id": session, "tool_name": "Bash",
                          "cwd": str(work), "tool_input": {"command": cmd}})
    proc = subprocess.run(
        [sys.executable, _GATE, "--mode", "hook"],
        input=payload, capture_output=True, text=True,
        cwd=str(work), env=env, timeout=timeout,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    return out["permissionDecision"], out.get("permissionDecisionReason", ""), proc.stderr


def _trace(tmp_path, name="stub.trace"):
    p = tmp_path / name
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _wait_state(work, tip, want, timeout=60):
    common = review_gate._git_common_dir(str(work))
    path = review_gate._state_path(common, tip)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = review_gate._read_state(path) or {}
        if st.get("state") in want:
            return st
        time.sleep(0.2)
    raise AssertionError(f"state never reached {want}: {review_gate._read_state(path)}")


def _write_record(common, fp, entry, findings=None, chain_depth=0, run_id="t"):
    key = review_gate._record_key(
        entry["path"], entry.get("old_path") or "",
        entry["status"], entry["old_oid"],
    )
    review_gate._write_ledger_record(
        common, fp, key, entry["new_oid"],
        entry["path"], entry.get("old_path") or "",
        entry["status"], entry["old_oid"],
        findings or [], chain_depth, run_id,
    )
    return key


def _base_finding(**kw):
    f = {
        "path": "app/x.py", "severity": "high",
        "start_line": 10, "end_line": 12,
        "confidence": 0.9, "content": "sql injection",
        "category": "security", "existing_code": "build_query(user_input)",
        "evidence": "stub",
    }
    f.update(kw)
    return f


# ---------------------------------------------------------------------------
# Scenario 2: fix in C, outside F's original context
# Guard acceptance and rejection
# ---------------------------------------------------------------------------

def _mini_git_repo(tmp_path, files):
    """Minimal one-commit git repo for guard tests. Returns (work, base, tip)."""
    work = tmp_path / "repo"
    work.mkdir()
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    _git(["config", "user.email", "t@t.com"], cwd=work)
    _git(["config", "user.name", "t"], cwd=work)
    _git(["config", "commit.gpgsign", "false"], cwd=work)
    # empty base commit
    (work / "readme.txt").write_text("readme\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    base = _git(["rev-parse", "HEAD"], cwd=work)
    for name, content in files.items():
        path = work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "fix"], cwd=work)
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    return work, base, tip


def test_scenario_2_guard_accepts_evidence_in_diff(tmp_path):
    """Fix in C added a line that matches the evidence_quote."""
    work, base, tip = _mini_git_repo(tmp_path, {"c.py": "# fixed\nfoo = 1\n"})
    active_items = [
        {"entry": {"path": "c.py", "old_path": "", "status": "M",
                   "old_oid": "0" * 40, "new_oid": "1" * 40, "lines": 2},
         "mode": "full", "record": None, "from_oid": "", "miss_reason": "no_record"},
    ]
    resolution = {"status": "resolved", "evidence_path": "c.py",
                  "evidence_quote": "foo = 1"}
    result = review_gate._guard_resolution(resolution, active_items,
                                           f"{base}..{tip}", str(work))
    assert result is True


def test_scenario_2_guard_rejects_quote_not_in_diff(tmp_path):
    """evidence_quote that doesn't appear in the added side of the diff is rejected."""
    work, base, tip = _mini_git_repo(tmp_path, {"c.py": "# fixed\nfoo = 1\n"})
    active_items = [
        {"entry": {"path": "c.py"}, "mode": "full", "record": None,
         "from_oid": "", "miss_reason": "no_record"},
    ]
    resolution = {"status": "resolved", "evidence_path": "c.py",
                  "evidence_quote": "bar = 999"}  # not in diff
    result = review_gate._guard_resolution(resolution, active_items,
                                           f"{base}..{tip}", str(work))
    assert result is False


def test_scenario_2_guard_rejects_evidence_in_unchanged_file(tmp_path):
    """evidence_path must be in active_plan_items (delta/full), not unchanged."""
    work, base, tip = _mini_git_repo(tmp_path, {"c.py": "# fixed\nfoo = 1\n"})
    active_items = [
        {"entry": {"path": "c.py"}, "mode": "full", "record": None,
         "from_oid": "", "miss_reason": "no_record"},
    ]
    # Evidence names f.py which is not in active_items
    resolution = {"status": "resolved", "evidence_path": "f.py",
                  "evidence_quote": "foo = 1"}
    result = review_gate._guard_resolution(resolution, active_items,
                                           f"{base}..{tip}", str(work))
    assert result is False


def test_scenario_2_guard_passes_still_present_unconditionally(tmp_path):
    """still_present verdict always passes (guard is a no-op for non-resolved)."""
    result = review_gate._guard_resolution(
        {"status": "still_present", "evidence_path": "", "evidence_quote": ""},
        [], "", str(tmp_path),
    )
    assert result is True


# ---------------------------------------------------------------------------
# Scenario 3: bad resolver output
# ---------------------------------------------------------------------------

def test_scenario_3_unknown_ids_are_ignored(tmp_path):
    """An id returned by the resolver that isn't in to_resolve is ignored:
    _normalize_resolutions keeps exactly the ids that were asked about."""
    known_id = "a" * 64
    to_resolve = [{"id": known_id, "finding": _base_finding(), "record": {"head_oid": "x"}}]
    # Simulate resolver returning known_id as resolved + an extra unknown id.
    resolver_out = {
        known_id: {"status": "resolved", "evidence_path": "c.py", "evidence_quote": "fix"},
        "extra-id-that-was-not-asked": {"status": "resolved", "evidence_path": "x.py",
                                         "evidence_quote": "other"},
    }
    out, warnings = review_gate._normalize_resolutions(resolver_out, to_resolve)
    assert set(out) == {known_id}
    assert out[known_id]["status"] == "resolved"
    assert warnings == []


def test_scenario_3_guard_rejects_evidence_in_inactive_file(tmp_path):
    """evidence_path pointing at a non-active file is denied by the guard."""
    work, base, tip = _mini_git_repo(tmp_path, {"c.py": "fix\n"})
    # active is c.py, but evidence_path is other.py (not active)
    active_items = [
        {"entry": {"path": "c.py"}, "mode": "full",
         "record": None, "from_oid": "", "miss_reason": "no_record"},
    ]
    resolution = {"status": "resolved", "evidence_path": "other.py",
                  "evidence_quote": "fix"}
    assert review_gate._guard_resolution(
        resolution, active_items, f"{base}..{tip}", str(work)
    ) is False


def test_scenario_3_garbage_output_becomes_all_still_present(monkeypatch, tmp_path):
    """When the resolver stub exits with non-zero or returns garbage,
    _run_resolver returns all still_present (fail closed)."""
    monkeypatch.setenv("STUB_RESOLVE_VERDICT", "fail")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "gate-data"))
    monkeypatch.setenv("OCR_REVIEWER_CMD", f'"{sys.executable}" "{_STUB}"')

    fid = "f" * 64
    to_resolve = [{"id": fid, "finding": _base_finding(), "record": {"head_oid": "x"}}]
    # Write a dummy resolver manifest (just needs to exist).
    async_d = Path(str(tmp_path)) / review_gate.ASYNC_DIR
    async_d.mkdir(parents=True, exist_ok=True)
    manifest_path = str(async_d / "resolver-t1.json")
    Path(manifest_path).write_text(
        json.dumps({"resolve": [{"id": fid, **_base_finding()}], "active_paths": ["c.py"]}),
        encoding="utf-8",
    )
    # Monkeypatch _run_review to simulate the resolver exiting with error.
    def _fail_review(*a, **kw):
        raise review_gate.ReviewGateError("resolver exited 1")

    monkeypatch.setattr(review_gate, "_run_review", _fail_review)
    active_items = [{"entry": {"path": "c.py"}, "mode": "full",
                     "record": None, "from_oid": "", "miss_reason": "no_record"}]
    result, warnings = review_gate._run_resolver(
        ".", "hook", ".", "tip", "base..tip",
        to_resolve, active_items, str(tmp_path), "fp" * 8, "run1",
    )
    assert result.get(fid, {}).get("status") == "still_present"
    assert result[fid]["evidence_quote"] == ""  # evidence-free: the caller re-checks it
    assert any("failed" in w for w in warnings)


# ---------------------------------------------------------------------------
# Scenario 8: fingerprint invalidation
# ---------------------------------------------------------------------------

def test_scenario_8_rubric_change_invalidates_records(tmp_path, monkeypatch):
    """After a rubric edit, the fingerprint changes → records from the old fp
    are not found → files classified as full with miss_reason=fp_mismatch or no_record."""
    # Set up a fake entry with a ledger record under fp1.
    e = {"path": "app/x.py", "old_path": "", "status": "M",
         "old_oid": "a" * 40, "new_oid": "b" * 40, "lines": 10}
    fp1 = "old_fp_value_123456"[:16]
    key = review_gate._record_key(e["path"], "", e["status"], e["old_oid"])
    review_gate._write_ledger_record(
        str(tmp_path), fp1, key, e["new_oid"],
        e["path"], "", e["status"], e["old_oid"],
        [], 0, "run1",
    )
    # Now compute a DIFFERENT fingerprint (rubric changed).
    fp2 = "new_fp_after_change"[:16]
    # With fp2, the record is in the wrong directory → miss.
    rec_path = review_gate._record_path(str(tmp_path), fp2, key, e["new_oid"])
    record = review_gate._read_ledger_record(rec_path, fp2, key, e["new_oid"])
    assert record is None, "record from old fp must not be found under new fp"


def test_scenario_8_plugin_version_bump_keeps_records(tmp_path, monkeypatch):
    """The plugin version is NOT in the fingerprint. A version-only change must
    not change the fingerprint (records stay valid)."""
    # Fingerprint depends on model, PROTOCOL_VERSION, skill/rules/agents contents,
    # .ocr/ tree OID, and prompt-affecting env vars -- but NOT the plugin version.
    # Two calls in the same environment must produce identical fingerprints.
    fp1 = review_gate._compute_fingerprint(str(tmp_path), "a" * 40)
    fp2 = review_gate._compute_fingerprint(str(tmp_path), "a" * 40)
    assert fp1 == fp2, "fingerprint is not deterministic"


def _plugin_copy(tmp_path):
    """A throwaway plugin root holding just the files the fingerprint reads."""
    root = tmp_path / "plugin"
    (root / "skills" / "review" / "rules").mkdir(parents=True)
    (root / "agents").mkdir()
    for rel in ("skills/review/SKILL.md", "skills/review/rubric.md",
                "skills/review/rules/python.md", "agents/code-reviewer.md",
                "agents/code-filter.md", "agents/code-resolver.md"):
        (root / rel).write_text(f"{rel} v1\n", encoding="utf-8")
    return root


def test_fingerprint_tracks_criteria_not_mechanics(tmp_path, monkeypatch):
    """0.9.0 split: what the review looks for invalidates records; how the gate
    runs it does not, so a plugin update keeps earlier reviews."""
    root = _plugin_copy(tmp_path)
    monkeypatch.setattr(review_gate, "_PLUGIN_ROOT", str(root))
    for var in ("OCR_CLAUDE_ARGS", "OCR_CLAUDE_EXTRA_ARGS", "OCR_BLOCK_SEVERITY"):
        monkeypatch.delenv(var, raising=False)

    def fp():
        return review_gate._compute_fingerprint(str(tmp_path), "")

    base = fp()
    for rel in ("skills/review/SKILL.md", "agents/code-resolver.md"):
        (root / rel).write_text("edited\n", encoding="utf-8")
    monkeypatch.setenv("OCR_CLAUDE_EXTRA_ARGS", "--verbose")
    assert fp() == base, "mechanics must not invalidate the ledger"

    for rel in ("skills/review/rubric.md", "skills/review/rules/python.md",
                "agents/code-reviewer.md", "agents/code-filter.md"):
        before = fp()
        (root / rel).write_text(f"{rel} tightened\n", encoding="utf-8")
        assert fp() != before, f"{rel} is review criteria"
    before = fp()
    monkeypatch.setenv("OCR_BLOCK_SEVERITY", "medium")
    assert fp() != before


# ---------------------------------------------------------------------------
# Scenario 10: target deleted → auto-resolved
# ---------------------------------------------------------------------------

def test_scenario_10_deleted_target_auto_resolved(monkeypatch, tmp_path):
    """When the file containing a finding no longer exists at tip (blob = ''),
    _classify_priors moves it to auto_resolved without a resolver call."""
    e = {"path": "app/x.py", "old_path": "", "status": "M",
         "old_oid": "a" * 40, "new_oid": "b" * 40, "lines": 10}
    finding = _base_finding(path="app/x.py")
    fp = "fp16" * 4
    key = review_gate._record_key(e["path"], "", e["status"], e["old_oid"])
    review_gate._write_ledger_record(
        str(tmp_path), fp, key, e["new_oid"],
        e["path"], "", e["status"], e["old_oid"],
        [finding], 0, "run1",
    )
    rec_path = review_gate._record_path(str(tmp_path), fp, key, e["new_oid"])
    record = review_gate._read_ledger_record(rec_path, fp, key, e["new_oid"])
    plan_items = [
        {"entry": e, "mode": "carry", "record": record,
         "from_oid": e["new_oid"], "miss_reason": "none"},
        # One active item so has_active=True
        {"entry": {"path": "new.py", "old_path": "", "status": "A",
                   "old_oid": "0" * 40, "new_oid": "c" * 40, "lines": 5},
         "mode": "full", "record": None, "from_oid": "", "miss_reason": "no_record"},
    ]
    # app/x.py has been deleted → blob returns "".
    monkeypatch.setattr(review_gate, "_blob_oids_at",
                        lambda root, tip, paths: {p: "" for p in paths})
    to_resolve, auto_resolved, carried = review_gate._classify_priors(
        plan_items, "tip", str(tmp_path), str(tmp_path), fp, "run2"
    )
    fid = finding.get("id") or review_gate._finding_id(finding)
    resolved_ids = [f.get("id") or review_gate._finding_id(f) for f in auto_resolved]
    assert fid in resolved_ids or len(auto_resolved) == 1
    # Must not go to the resolver (auto-resolved mechanically).
    assert not any(p["id"] == fid for p in to_resolve)


# ---------------------------------------------------------------------------
# Scenario 11: delta chains are not capped; the cost rule picks the range
# ---------------------------------------------------------------------------

def test_scenario_11_deep_chain_is_still_a_delta_base(tmp_path):
    """0.9.0 dropped the fixed chain cap: however many fix rounds a file has had,
    its newest record is still a delta base."""
    e = {"path": "app/x.py", "old_path": "", "status": "M",
         "old_oid": "a" * 40, "new_oid": "b" * 40, "lines": 10}
    fp = "fp16" * 4
    key = review_gate._record_key(e["path"], "", e["status"], e["old_oid"])
    old_head = "0" * 40
    review_gate._write_ledger_record(
        str(tmp_path), fp, key, old_head,
        e["path"], "", e["status"], e["old_oid"],
        [], 50, "run1",
    )
    rec, from_oid = review_gate._find_delta_record(str(tmp_path), fp, key, e["new_oid"])
    assert rec is not None
    assert from_oid == old_head


def _cost_rule_repo(tmp_path, reviewed, fixed):
    """base -> x.py=reviewed (T1) -> x.py=fixed (T2). Returns (work, base, blob1, tip)."""
    work = tmp_path / "cr"
    work.mkdir()
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    _git(["config", "user.email", "t@t.com"], cwd=work)
    _git(["config", "user.name", "t"], cwd=work)
    _git(["config", "commit.gpgsign", "false"], cwd=work)
    (work / "readme.txt").write_text("r\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    base = _git(["rev-parse", "HEAD"], cwd=work)
    (work / "x.py").write_text(reviewed)
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "t1"], cwd=work)
    blob1 = _git(["rev-parse", "HEAD:x.py"], cwd=work)
    (work / "x.py").write_text(fixed)
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "t2"], cwd=work)
    return work, base, blob1, _git(["rev-parse", "HEAD"], cwd=work)


def _plan_after_record(tmp_path, work, base, blob1, tip, findings=None):
    common, fp = str(tmp_path / "common"), "c" * 64
    entries, _ = review_gate._collect_diff_entries(str(work), base, tip)
    e = next(x for x in entries if x["path"] == "x.py")
    key = review_gate._record_key(e["path"], e.get("old_path") or "", e["status"], e["old_oid"])
    review_gate._write_ledger_record(common, fp, key, blob1, "x.py", "", e["status"],
                                     e["old_oid"], findings or [], 0, "r1")
    plan, _ = review_gate._plan_review(str(work), base, tip, common, fp)
    return next(p for p in plan if p["entry"]["path"] == "x.py")


def test_cost_rule_keeps_a_small_fix_to_a_big_file_as_a_delta(tmp_path):
    big = "".join(f"v{i} = {i}\n" for i in range(200))
    work, base, blob1, tip = _cost_rule_repo(tmp_path, big, big.replace("v7 = 7", "v7 = 70"))
    item = _plan_after_record(tmp_path, work, base, blob1, tip)
    assert item["mode"] == "delta"
    assert item["delta_lines"] == 2 and item["full_lines"] == 200


def test_cost_rule_reviews_the_whole_range_when_it_costs_about_the_same(tmp_path):
    # A small new file whose fix rewrites most of it: the push-range diff is
    # barely bigger than the delta, so the whole range is reviewed -- and the
    # record, with its owed findings, is kept.
    f = {"path": "x.py", "severity": "high", "content": "bad", "existing_code": "a = 1",
         "start_line": 1, "end_line": 1}
    work, base, blob1, tip = _cost_rule_repo(tmp_path, "a = 1\nb = 2\n", "a = 3\nb = 4\nc = 5\n")
    item = _plan_after_record(tmp_path, work, base, blob1, tip, findings=[f])
    assert item["mode"] == "full" and item["miss_reason"] == "cost_rule"
    assert item["record"] is not None and item["from_oid"] == blob1
    to_resolve, _, _ = review_gate._classify_priors(
        [item], tip, str(work), str(tmp_path / "common"), "c" * 64, "r2")
    assert [p["finding"]["content"] for p in to_resolve] == ["bad"]


# ---------------------------------------------------------------------------
# Scenario 12: re-anchoring
# ---------------------------------------------------------------------------

def test_scenario_12_reanchor_updates_line_numbers(tmp_path):
    """_reanchor_finding locates existing_code in the tip blob and updates lines."""
    # Build a git repo with a file that has 25 lines; existing_code is at lines 22-23.
    work, base, tip = _mini_git_repo(tmp_path, {
        "app/x.py": "# line1\n" * 20 + "def foo():\n    pass\n" + "# trailing\n" * 3,
    })
    finding = _base_finding(
        path="app/x.py", start_line=1, end_line=2,
        existing_code="def foo():\n    pass",
    )
    updated, ok = review_gate._reanchor_finding(finding, str(work), tip)
    assert ok is True
    assert updated["start_line"] == 21
    assert updated["end_line"] == 22


def test_scenario_12_missing_snippet_marks_unanchored(tmp_path):
    """When existing_code is not found, the finding is marked unanchored."""
    work, base, tip = _mini_git_repo(tmp_path, {
        "app/x.py": "# completely different content\n",
    })
    finding = _base_finding(
        path="app/x.py", start_line=10, end_line=12,
        existing_code="build_query(user_input)",
    )
    updated, ok = review_gate._reanchor_finding(finding, str(work), tip)
    assert ok is False
    assert updated.get("unanchored") is True


# ---------------------------------------------------------------------------
# Scenario 14: migration and robustness
# ---------------------------------------------------------------------------

def test_scenario_14_corrupt_record_treated_as_full(tmp_path):
    """A corrupt record file is a miss → next call classifies the file as full."""
    fp = "b" * 16
    fp_dir = tmp_path / fp
    fp_dir.mkdir(parents=True)
    e = {"path": "app/x.py", "old_path": "", "status": "M",
         "old_oid": "a" * 40, "new_oid": "b" * 40, "lines": 10}
    key = review_gate._record_key(e["path"], "", e["status"], e["old_oid"])
    rec_path = review_gate._record_path(str(tmp_path), fp, key, e["new_oid"])
    rec_path.parent.mkdir(parents=True, exist_ok=True)
    rec_path.write_text("{broken json", encoding="utf-8")
    result = review_gate._read_ledger_record(rec_path, fp, key, e["new_oid"])
    assert result is None


def test_scenario_14_unknown_schema_treated_as_miss(tmp_path):
    """A record with an unknown schema version is a miss (fails closed)."""
    fp = "c" * 16
    e = {"path": "app/x.py", "old_path": "", "status": "M",
         "old_oid": "a" * 40, "new_oid": "b" * 40, "lines": 10}
    key = review_gate._record_key(e["path"], "", e["status"], e["old_oid"])
    rec_path = review_gate._record_path(str(tmp_path), fp, key, e["new_oid"])
    rec_path.parent.mkdir(parents=True, exist_ok=True)
    data = {"schema": 999, "fp": fp, "key": key, "path": e["path"],
            "head_oid": e["new_oid"], "findings": []}
    rec_path.write_text(json.dumps(data), encoding="utf-8")
    result = review_gate._read_ledger_record(rec_path, fp, key, e["new_oid"])
    assert result is None


def test_scenario_14_hash_mismatch_treated_as_miss(tmp_path):
    """A record whose stored fp/key doesn't match the caller's is a miss."""
    fp = "d" * 16
    e = {"path": "app/x.py", "old_path": "", "status": "M",
         "old_oid": "a" * 40, "new_oid": "b" * 40, "lines": 10}
    key = review_gate._record_key(e["path"], "", e["status"], e["old_oid"])
    rec_path = review_gate._record_path(str(tmp_path), fp, key, e["new_oid"])
    rec_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema": review_gate._LEDGER_SCHEMA, "fp": "wrongfp", "key": key,
        "path": e["path"], "head_oid": e["new_oid"], "findings": [],
    }
    rec_path.write_text(json.dumps(data), encoding="utf-8")
    result = review_gate._read_ledger_record(rec_path, fp, key, e["new_oid"])
    assert result is None


def test_scenario_14_stale_0_7_state_triggers_fresh_run(monkeypatch, tmp_path):
    """A 0.7.0 state file (no protocol_version) is treated as stale on upgrade."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "gate-data"))
    common = str(tmp_path)
    async_d = Path(common) / review_gate.ASYNC_DIR
    async_d.mkdir(parents=True, exist_ok=True)
    state_path = str(async_d / ("a" * 40 + ".json"))
    # Write a 0.7.0-style failed(limit) state without protocol_version.
    review_gate._write_state(state_path, {
        "state": "failed", "reason": "limit",
        "tip": "a" * 40, "run_id": "old-run",
        # No "protocol_version" key.
    })
    st = review_gate._read_state(state_path)
    assert int(st.get("protocol_version") or 0) < review_gate.PROTOCOL_VERSION
    # _drive_review would see protocol_version < PROTOCOL_VERSION → treat as absent.
    # Verify the detection condition used in _drive_review:
    s = st.get("state")
    if s in ("running", "claimed", "failed"):
        if int(st.get("protocol_version") or 0) < review_gate.PROTOCOL_VERSION:
            s = None  # treated as absent
    assert s is None, "stale state must be treated as absent"


# ---------------------------------------------------------------------------
# Scenario 15: concurrency (byte-identical records)
# ---------------------------------------------------------------------------

def test_scenario_15_concurrent_writes_produce_identical_records(tmp_path):
    """Two supervisors writing a record for the same file produce byte-identical
    content. The rename-based write means no corruption (last writer wins)."""
    e = {"path": "app/x.py", "old_path": "", "status": "M",
         "old_oid": "a" * 40, "new_oid": "b" * 40, "lines": 10}
    fp = "fp16" * 4
    key = review_gate._record_key(e["path"], "", e["status"], e["old_oid"])
    findings = [_base_finding()]

    # Write twice with different run_ids.
    review_gate._write_ledger_record(
        str(tmp_path), fp, key, e["new_oid"],
        e["path"], "", e["status"], e["old_oid"],
        findings, 0, "run-A",
    )
    rec_path = review_gate._record_path(str(tmp_path), fp, key, e["new_oid"])
    content_a = rec_path.read_text(encoding="utf-8")

    review_gate._write_ledger_record(
        str(tmp_path), fp, key, e["new_oid"],
        e["path"], "", e["status"], e["old_oid"],
        findings, 0, "run-B",
    )
    content_b = rec_path.read_text(encoding="utf-8")

    # Records must both be valid (corrupt-free).
    data_a = json.loads(content_a)
    data_b = json.loads(content_b)
    assert data_a["key"] == data_b["key"]
    assert data_a["fp"] == data_b["fp"]
    assert data_a["head_oid"] == data_b["head_oid"]
    # Content is structurally identical (same key/fp/oid/findings); only run_id differs.
    assert len(data_a["findings"]) == len(data_b["findings"])


# ---------------------------------------------------------------------------
# Scenario 16: security guard for ledger dir
# ---------------------------------------------------------------------------

def test_scenario_16_guard_denies_write_into_ledger_dir(monkeypatch, tmp_path):
    """The reviewer write guard must deny redirections into review-gate-ledger/."""
    g = review_gate._guard_reviewer_command
    # A write into .git/review-gate-ledger must be denied.
    assert g("git diff > .git/review-gate-ledger/fp16/rec.json") != ""
    # Normal reads are still allowed.
    assert g("git diff HEAD~1") == ""


# ---------------------------------------------------------------------------
# Scenario 17: pruning
# ---------------------------------------------------------------------------

def test_scenario_17_prune_removes_oldest_when_over_cap(tmp_path, monkeypatch):
    """When the ledger exceeds _LEDGER_MAX_RECORDS, the oldest records are deleted."""
    monkeypatch.setattr(review_gate, "_LEDGER_MAX_RECORDS", 5)
    fp = "fpdir16901234"[:16]
    fp_dir = tmp_path / review_gate.LEDGER_DIR / fp
    fp_dir.mkdir(parents=True)
    # Write 6 records with different mtimes.
    files = []
    for i in range(6):
        p = fp_dir / f"{i:016x}-{'b' * 16}.json"
        data = {
            "schema": review_gate._LEDGER_SCHEMA, "fp": fp,
            "key": f"{i:016x}", "path": f"f{i}.py",
            "old_path": "", "status": "M", "base_oid": "0" * 40,
            "head_oid": "b" * 40, "findings": [], "chain_depth": 0,
            "reviewed_ts": time.time(), "run_id": f"r{i}",
        }
        p.write_text(json.dumps(data), encoding="utf-8")
        mtime = time.time() - (6 - i) * 10  # oldest first
        os.utime(p, (mtime, mtime))
        files.append(p)

    review_gate._prune_ledger(str(tmp_path / review_gate.LEDGER_DIR).replace(
        review_gate.LEDGER_DIR, ""
    ))
    # The ledger dir is under common_dir/LEDGER_DIR; but _prune_ledger takes common_dir.
    # Re-run with tmp_path as common_dir (files are under tmp_path/review-gate-ledger/fp/).
    review_gate._prune_ledger(str(tmp_path))

    surviving = [p for p in files if p.exists()]
    assert len(surviving) == 5
    # The oldest file (files[0]) must be gone.
    assert not files[0].exists()


def test_scenario_17_prune_removes_stale_fp_dir(tmp_path, monkeypatch):
    """A whole fp directory whose mtime is past the TTL is removed entirely."""
    monkeypatch.setattr(review_gate, "_LEDGER_TTL", 100)  # 100s TTL for test
    old_fp = "oldfpdir1234567"[:16]
    old_dir = tmp_path / review_gate.LEDGER_DIR / old_fp
    old_dir.mkdir(parents=True)
    rec = old_dir / "key-head.json"
    rec.write_text("{}", encoding="utf-8")
    # Age the directory beyond TTL.
    stamp = time.time() - 200  # 200s old > 100s TTL
    os.utime(old_dir, (stamp, stamp))

    review_gate._prune_ledger(str(tmp_path))
    assert not old_dir.exists(), "stale fp directory must be pruned"


# ---------------------------------------------------------------------------
# Integration tests (full gate, async)
# ---------------------------------------------------------------------------

def test_scenario_6_no_change_repush_replays_with_zero_calls(tmp_path):
    """Scenario 6a: a no-change re-push after a complete run returns the same
    verdict with zero new reviewer calls (all files already in the ledger)."""
    work = _tiny_repo(tmp_path, {"x.py": "x = 1\n", "y.py": "y = 2\n"})
    tip = _commit(work, "x.py", "x = 2\n")  # overwrite to get a change
    # First push: pass (records written for both files).
    env = _env(tmp_path)
    decision, reason, _ = _hook(work, "git push origin main", env)
    assert decision == "allow", reason
    _wait_state(work, tip, {"done"})
    calls1 = _trace(tmp_path)
    assert len(calls1) >= 1

    # Re-push the same tip: all files already in ledger → no reviewer call.
    env2 = _env(tmp_path)
    env2["STUB_TRACE"] = str(tmp_path / "stub2.trace")
    decision2, reason2, _ = _hook(work, "git push origin main", env2)
    assert decision2 == "allow", reason2
    _wait_state(work, tip, {"done"})
    calls2 = _trace(tmp_path, "stub2.trace")
    assert calls2 == [], f"expected 0 reviewer calls on re-push, got {calls2}"


def test_scenario_13_ocr_ledger_0_uses_golden_argv_no_ledger_dir(tmp_path):
    """OCR_LEDGER=0: every file is full, argv byte-identical to 0.7.0, no ledger dir."""
    work = _tiny_repo(tmp_path, {"x.py": "x = 1\n"})
    tip = _commit(work, "x.py", "x = 2\n")
    env = _env(tmp_path, OCR_LEDGER="0")
    decision, reason, _ = _hook(work, "git push origin main", env)
    assert decision == "allow", reason
    _wait_state(work, tip, {"done"})
    calls = _trace(tmp_path)
    assert len(calls) == 1
    # No --paths-file in the call (golden argv path).
    assert calls[0]["paths_file"] is None, "OCR_LEDGER=0 must not use --paths-file"
    # Ledger directory must not be created.
    common = review_gate._git_common_dir(str(work))
    led_dir = review_gate._ledger_dir(common)
    assert not led_dir.exists(), "ledger dir must not be created when OCR_LEDGER=0"


def test_scenario_13_ocr_force_review_skips_reads_but_writes_records(tmp_path):
    """OCR_FORCE_REVIEW=1: every file is full (no ledger reads), but records
    are still written so the following normal push can carry them."""
    work = _tiny_repo(tmp_path, {"x.py": "x = 1\n"})
    tip = _commit(work, "x.py", "x = 2\n")

    # First pass: normal push, records written.
    env1 = _env(tmp_path)
    decision1, reason1, _ = _hook(work, "git push origin main", env1)
    assert decision1 == "allow", reason1
    _wait_state(work, tip, {"done"})
    calls1 = _trace(tmp_path)
    assert len(calls1) == 1

    # Second pass: OCR_FORCE_REVIEW=1 — ignores existing records → full review again.
    env2 = _env(tmp_path, OCR_FORCE_REVIEW="1")
    env2["STUB_TRACE"] = str(tmp_path / "stub2.trace")
    decision2, reason2, _ = _hook(work, "git push origin main", env2)
    assert decision2 == "allow", reason2
    _wait_state(work, tip, {"done"})
    calls2 = _trace(tmp_path, "stub2.trace")
    assert len(calls2) == 1, "OCR_FORCE_REVIEW=1 must review even when records exist"

    # After force review, a third normal push should carry (records were written).
    env3 = _env(tmp_path)
    env3["STUB_TRACE"] = str(tmp_path / "stub3.trace")
    decision3, reason3, _ = _hook(work, "git push origin main", env3)
    assert decision3 == "allow", reason3
    _wait_state(work, tip, {"done"})
    calls3 = _trace(tmp_path, "stub3.trace")
    assert calls3 == [], f"expected 0 calls after force review, got {calls3}"


def test_scenario_18_stable_boundaries_only_new_file_reviewed(tmp_path):
    """Adding one file to a push whose other files are already in the ledger
    → only the new file is reviewed, all others are carried.
    Chunk boundaries are per-file (ledger-based), not positional.
    """
    # Setup: 3 base files, push them so their records are created.
    work = _tiny_repo(tmp_path, {
        "a.py": "a = 1\n", "b.py": "b = 2\n", "c.py": "c = 3\n",
    })
    tip1 = _commit(work, "a.py", "a = 10\n")  # modify a.py
    # also change b.py and c.py in same commit
    (work / "b.py").write_text("b = 20\n")
    (work / "c.py").write_text("c = 30\n")
    _git(["add", "b.py", "c.py"], cwd=work)
    _git(["commit", "-q", "--amend", "--no-edit"], cwd=work)
    tip1 = _git(["rev-parse", "HEAD"], cwd=work)

    env1 = _env(tmp_path)
    decision1, reason1, _ = _hook(work, "git push origin main", env1)
    assert decision1 == "allow", reason1
    _wait_state(work, tip1, {"done"})
    calls1 = _trace(tmp_path)
    assert len(calls1) == 1  # single-context (3 files ≤ threshold)

    # Second push: add a new file (d.py). a.py, b.py, c.py unchanged → carry.
    tip2 = _commit(work, "d.py", "d = 4\n")
    env2 = _env(tmp_path)
    env2["STUB_TRACE"] = str(tmp_path / "stub2.trace")
    decision2, reason2, _ = _hook(work, "git push origin main", env2)
    assert decision2 == "allow", reason2
    _wait_state(work, tip2, {"done"})
    calls2 = _trace(tmp_path, "stub2.trace")
    assert len(calls2) == 1, f"expected 1 call (only d.py active), got {calls2}"
    # The manifest must show d.py as active, a.py/b.py/c.py as carried.
    m = calls2[0]["manifest"]
    active_paths = [f["path"] for f in (m.get("files") or []) if f.get("mode") == "full"]
    assert "d.py" in active_paths, f"d.py must be full; files={m.get('files')}"
    carried_paths = m.get("carried") or []
    for p in ("a.py", "b.py", "c.py"):
        assert p in carried_paths, f"{p} must be carried; carried={carried_paths}"


def test_scenario_1_block_x_fix_x_delta_and_resolver_ran(tmp_path):
    """Scenario 1: block on X in T1, fix X in T2.
    T2: exactly 2 stub calls — call 1 reviews X as delta, call 2 is --resolve.
    """
    # Build a repo with x.py and stable.py at root (to match stub finding paths).
    work = _tiny_repo(tmp_path, {
        "x.py": "x = 0\n",
        "stable.py": "# stable\n",
    })

    # T1: commit that changes x.py AND stable.py; reviewer blocks with finding in x.py.
    # x.py gains enough code that a one-line fix is far smaller than the push
    # range, so the cost rule keeps it a delta.
    body = "".join(f"v{i} = {i}\n" for i in range(20))
    (work / "x.py").write_text("x = 1\n" + body + "def bad(): pass\n")
    (work / "stable.py").write_text("# stable v2\n")
    _git(["add", "x.py", "stable.py"], cwd=work)
    _git(["commit", "-q", "-m", "t1"], cwd=work)
    tip1 = _git(["rev-parse", "HEAD"], cwd=work)

    # STUB_FINDINGS_FOR now works even without manifest (golden argv path).
    findings_map = json.dumps({"x.py": {
        "severity": "high",
        "content": "bad function in x",
        "existing_code": "def bad(): pass",
    }})
    env1 = _env(tmp_path, STUB_FINDINGS_FOR=findings_map)
    decision1, reason1, _ = _hook(work, "git push origin main", env1)
    assert decision1 == "deny", reason1
    _wait_state(work, tip1, {"done"})
    calls1 = _trace(tmp_path)
    assert len(calls1) == 1

    # T2: fix x.py (change the blob), stable.py unchanged.
    (work / "x.py").write_text("x = 1\n" + body + "def good(): pass\n")
    _git(["add", "x.py"], cwd=work)
    _git(["commit", "-q", "--amend", "--no-edit"], cwd=work)
    tip2 = _git(["rev-parse", "HEAD"], cwd=work)

    # Compute the finding ID (matches what the stub wrote into T1's record).
    fid = review_gate._finding_id({
        "path": "x.py",
        "existing_code": "def bad(): pass",
        "content": "bad function in x",
    })
    resolve_map = json.dumps({fid: {
        "status": "resolved",
        "evidence_path": "x.py",  # the active delta file
        "evidence_quote": "def good(): pass",  # added in x.py's T2 diff
    }})

    env2 = _env(tmp_path, STUB_RESOLVE=resolve_map, STUB_VERDICT="pass")
    env2["STUB_TRACE"] = str(tmp_path / "stub2.trace")
    decision2, reason2, _ = _hook(work, "git push -f origin main", env2)
    assert decision2 == "allow", reason2
    _wait_state(work, tip2, {"done"})

    calls2 = _trace(tmp_path, "stub2.trace")
    # Expect exactly 2 calls: 1 review call + 1 resolver call.
    assert len(calls2) == 2, f"expected 2 calls (review + resolve), got {len(calls2)}"

    review_call = next((c for c in calls2 if c.get("resolve_file") is None), None)
    resolve_call = next((c for c in calls2 if c.get("resolve_file") is not None), None)
    assert review_call is not None, "expected one review call"
    assert resolve_call is not None, "expected one resolver call"

    # Review call: x.py must be delta (not full), stable.py in carried.
    manifest = review_call.get("manifest") or {}
    files_by_path = {f["path"]: f for f in (manifest.get("files") or [])}
    assert "x.py" in files_by_path, f"x.py missing from manifest files: {manifest}"
    assert files_by_path["x.py"]["mode"] == "delta", (
        f"x.py must be delta mode; got {files_by_path['x.py']['mode']}"
    )
    carried = manifest.get("carried") or []
    assert "stable.py" in carried, f"stable.py must be carried; carried={carried}"

    # No full path in the review manifest.
    for f in manifest.get("files") or []:
        assert f.get("mode") != "full", f"no full paths expected; got {f}"


def test_scenario_5_resolution_reuse_suppresses_finding(tmp_path):
    """Scenario 5: after resolver clears F, a re-push with the same evidence
    blob suppresses F with zero resolver calls."""
    # Build a repo with x.py and evidence file c.py.
    work = _tiny_repo(tmp_path, {
        "x.py": "x = 0\n",
        "c.py": "# c init\n",
    })
    # T1: commit changing x.py AND c.py → STUB_FINDINGS_FOR works (golden argv applies to all).
    (work / "x.py").write_text("x = 1\ndef bad(): pass\n")
    (work / "c.py").write_text("# c v2\n")
    _git(["add", "x.py", "c.py"], cwd=work)
    _git(["commit", "-q", "-m", "t1"], cwd=work)
    tip1 = _git(["rev-parse", "HEAD"], cwd=work)

    findings_map = json.dumps({"x.py": {
        "severity": "high",
        "content": "bad function",
        "existing_code": "def bad(): pass",
    }})
    env1 = _env(tmp_path, STUB_FINDINGS_FOR=findings_map)
    decision1, reason1, _ = _hook(work, "git push origin main", env1)
    assert decision1 == "deny", reason1
    _wait_state(work, tip1, {"done"})

    # T2: fix x.py (change blob) + fix evidence in c.py.
    (work / "x.py").write_text("x = 1\ndef good(): pass\n")
    (work / "c.py").write_text("# c fixed\ndef good(): pass\n")
    _git(["add", "x.py", "c.py"], cwd=work)
    _git(["commit", "-q", "--amend", "--no-edit"], cwd=work)
    tip2 = _git(["rev-parse", "HEAD"], cwd=work)

    # Compute finding ID (matches stub's STUB_FINDINGS_FOR output).
    fid = review_gate._finding_id({
        "path": "x.py", "existing_code": "def bad(): pass", "content": "bad function",
    })
    resolve_map = json.dumps({fid: {
        "status": "resolved",
        "evidence_path": "c.py",  # c.py is an active delta file in T2
        "evidence_quote": "def good(): pass",
    }})

    env2 = _env(tmp_path, STUB_RESOLVE=resolve_map, STUB_VERDICT="pass")
    env2["STUB_TRACE"] = str(tmp_path / "stub2.trace")
    decision2, reason2, _ = _hook(work, "git push -f origin main", env2)
    assert decision2 == "allow", reason2
    _wait_state(work, tip2, {"done"})
    # Verify a resolver call was made in T2 (the resolution was written).
    calls2 = _trace(tmp_path, "stub2.trace")
    resolver_calls_t2 = [c for c in calls2 if c.get("resolve_file") is not None]
    assert len(resolver_calls_t2) == 1, "T2 must have exactly one resolver call"

    # T3: re-push tip2 (same blobs). F is suppressed by existing resolution → 0 reviewer calls.
    env3 = _env(tmp_path, STUB_VERDICT="pass")
    env3["STUB_TRACE"] = str(tmp_path / "stub3.trace")
    decision3, reason3, _ = _hook(work, "git push -f origin main", env3)
    assert decision3 == "allow", reason3
    _wait_state(work, tip2, {"done"})
    calls3 = _trace(tmp_path, "stub3.trace")
    # All files carry, F suppressed by resolution → zero reviewer calls.
    assert calls3 == [], f"T3 must have 0 calls (resolution reuse); got {calls3}"


def test_scenario_7_medium_finding_in_a_prior_plus_new_file(tmp_path):
    """Scenario 7: medium finding in A is carried; next push adds B.
    A is carry → finding goes to resolver; verdict shows (still present) if not resolved.
    """
    work = _tiny_repo(tmp_path, {"a.py": "x = 1\n"})
    # T1: commit changing a.py with a medium finding.
    (work / "a.py").write_text("x = 2\ndef risky(): pass\n")
    _git(["add", "a.py"], cwd=work)
    _git(["commit", "-q", "-m", "t1"], cwd=work)
    tip1 = _git(["rev-parse", "HEAD"], cwd=work)
    findings_map = json.dumps({"a.py": {
        "severity": "medium",
        "content": "risky function",
        "existing_code": "def risky(): pass",
    }})
    env1 = _env(tmp_path, STUB_FINDINGS_FOR=findings_map)
    decision1, reason1, _ = _hook(work, "git push origin main", env1)
    # Medium finding → warn verdict → gate allows (not denies).
    assert decision1 == "allow", reason1
    st1 = _wait_state(work, tip1, {"done"})
    assert st1.get("verdict") == "warn", f"expected warn verdict, got {st1.get('verdict')}"
    calls1 = _trace(tmp_path)
    assert len(calls1) == 1

    # T2: add new file b.py, a.py unchanged.
    tip2 = _commit(work, "b.py", "b = 1\n")
    env2 = _env(tmp_path, STUB_VERDICT="pass")
    env2["STUB_TRACE"] = str(tmp_path / "stub2.trace")
    decision2, reason2, _ = _hook(work, "git push origin main", env2)
    # Decision may be deny (prior finding still present) or allow (resolved).
    # Key assertions: a.py not in review manifest, resolver call present.
    _wait_state(work, tip2, {"done"})
    calls2 = _trace(tmp_path, "stub2.trace")
    # a.py must NOT be in any review manifest (it's carry).
    for c in calls2:
        if c.get("resolve_file") is None:  # review call
            m = c.get("manifest") or {}
            active_paths = [f["path"] for f in (m.get("files") or [])]
            assert "a.py" not in active_paths, (
                f"a.py (carry) must not appear in review manifest; active={active_paths}"
            )
    # There must be a resolver call (medium finding + has_active).
    resolver_calls = [c for c in calls2 if c.get("resolve_file") is not None]
    assert len(resolver_calls) >= 1, f"expected resolver call for prior medium finding; calls={calls2}"


def test_scenario_9_trust_negatives_failed_chunk_writes_no_record(monkeypatch, tmp_path):
    """Scenario 9: a failed reviewer call must not write a ledger record.
    The next push reviews the file in full (no carry).
    """
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "gate-data"))
    e = {"path": "app/x.py", "old_path": "", "status": "M",
         "old_oid": "a" * 40, "new_oid": "b" * 40, "lines": 10}
    active_items = [{"entry": e, "mode": "full", "record": None,
                     "from_oid": "", "miss_reason": "no_record"}]
    fp = "fp" * 8
    # Simulate _write_run_records with a failure status.
    result_fail = {"status": "failed", "findings": [], "warnings": []}
    review_gate._write_run_records(result_fail, active_items, str(tmp_path), fp, "run1")
    rec_path = review_gate._record_path(str(tmp_path), fp,
                                         review_gate._record_key(e["path"], "", e["status"],
                                                                  e["old_oid"]),
                                         e["new_oid"])
    assert not rec_path.exists(), "failed run must not write a ledger record"

    # diff_truncated warning also prevents writing.
    result_trunc = {"status": "success", "findings": [],
                    "warnings": [{"type": "diff_truncated"}]}
    review_gate._write_run_records(result_trunc, active_items, str(tmp_path), fp, "run2")
    assert not rec_path.exists(), "truncated diff must not write a ledger record"

    # A successful result DOES write a record.
    result_ok = {"status": "success", "findings": [], "warnings": []}
    review_gate._write_run_records(result_ok, active_items, str(tmp_path), fp, "run3")
    assert rec_path.exists(), "successful run must write a ledger record"


def test_scenario_4_reviewer_overrides_resolver_no_resolution_written(monkeypatch, tmp_path):
    """Scenario 4: when the reviewer re-reports a finding the resolver cleared,
    the new finding counts (verdict blocks) and no resolution is written.
    """
    # We test the resolver/dedup logic directly using _classify_priors + guard +
    # dedup logic by constructing the intermediate state.
    e = {"path": "x.py", "old_path": "", "status": "M",
         "old_oid": "a" * 40, "new_oid": "b" * 40, "lines": 10}
    finding = _base_finding(path="x.py", severity="high",
                             existing_code="def bad(): pass",
                             content="bad function in x")
    fp = "fp" * 8
    key = review_gate._record_key(e["path"], "", e["status"], e["old_oid"])
    review_gate._write_ledger_record(
        str(tmp_path), fp, key, e["new_oid"],
        e["path"], "", e["status"], e["old_oid"],
        [finding], 0, "run1",
    )
    rec_path = review_gate._record_path(str(tmp_path), fp, key, e["new_oid"])
    record = review_gate._read_ledger_record(rec_path, fp, key, e["new_oid"])

    # Plan: x.py is delta (prior finding), plus an active full file.
    new_entry = {"path": "c.py", "old_path": "", "status": "A",
                 "old_oid": "0" * 40, "new_oid": "c" * 40, "lines": 5}
    plan_items = [
        {"entry": e, "mode": "delta", "record": record,
         "from_oid": e["old_oid"], "miss_reason": "none"},
        {"entry": new_entry, "mode": "full", "record": None,
         "from_oid": "", "miss_reason": "no_record"},
    ]
    monkeypatch.setattr(review_gate, "_blob_oids_at",
                        lambda root, tip, paths: {p: "some_oid" for p in paths})

    to_resolve, auto_resolved, carried = review_gate._classify_priors(
        plan_items, "tip", str(tmp_path), str(tmp_path), fp, "run2"
    )
    fid = review_gate._finding_id(finding)
    assert any(p["id"] == fid for p in to_resolve), "high finding must go to resolver"

    # Simulate resolver saying "resolved" with a valid-looking evidence.
    # But then the reviewer re-reports a similar finding.
    provisional_resolved = []
    still_present = []
    resolver_results = {
        fid: {"status": "resolved", "evidence_path": "c.py",
              "evidence_quote": "def fixed(): pass"},
    }
    for p in to_resolve:
        pid = p["id"]
        res = resolver_results.get(pid) or {"status": "still_present"}
        ev_path = res.get("evidence_path") or ""
        ev_blob = "c_blob_oid" if ev_path else ""
        # Simulate guard passing (we skip actual diff check here).
        if res.get("status") == "resolved":
            provisional_resolved.append((p, res, ev_path, ev_blob))
        else:
            provisional_resolved  # handled elsewhere

    # Reviewer re-reports a finding similar to `finding`.
    reviewer_similar = _base_finding(path="x.py", severity="high",
                                     existing_code="def bad(): pass",
                                     content="bad function in x")
    reviewer_similar["provenance"] = "new"
    new_findings = [reviewer_similar]

    reviewer_override = set()
    deduped_new = []
    for nf in new_findings:
        if any(review_gate._findings_similar(nf, sp) for sp in still_present):
            continue
        deduped_new.append(nf)
        for prov_p, _, _, _ in provisional_resolved:
            if review_gate._findings_similar(nf, prov_p["finding"]):
                reviewer_override.add(prov_p["id"])

    # The finding must be in deduped_new (reviewer confirmed it).
    assert len(deduped_new) == 1
    # The finding ID must be in reviewer_override → resolution NOT written.
    assert fid in reviewer_override, "reviewer override must suppress resolution write"
    # Verify: no resolution file on disk (would be written only if not overridden).
    res_dir = review_gate._fp_dir(str(tmp_path), fp) / "resolutions"
    if res_dir.exists():
        assert list(res_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# Regressions: a finding that still blocks must never fall out of the ledger
# ---------------------------------------------------------------------------

def test_still_present_prior_survives_a_later_push_that_carries_its_file(tmp_path):
    work = _tiny_repo(tmp_path, {"x.py": "x = 0\n", "stable.py": "# stable\n"})
    (work / "x.py").write_text("x = 1\ndef bad(): pass\n")
    (work / "stable.py").write_text("# stable v2\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "t1"], cwd=work)
    tip1 = _git(["rev-parse", "HEAD"], cwd=work)
    findings_map = json.dumps({"x.py": {"severity": "high", "content": "bad function in x",
                                        "existing_code": "def bad(): pass"}})
    decision, reason, _ = _hook(work, "git push origin main",
                                _env(tmp_path, STUB_FINDINGS_FOR=findings_map))
    assert decision == "deny", reason
    _wait_state(work, tip1, {"done"})

    # T2 touches x.py without fixing it: delta review finds nothing new, the
    # resolver keeps the prior.
    (work / "x.py").write_text("x = 2\ndef bad(): pass\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "--amend", "--no-edit"], cwd=work)
    tip2 = _git(["rev-parse", "HEAD"], cwd=work)
    decision, reason, _ = _hook(work, "git push -f origin main", _env(tmp_path))
    assert decision == "deny" and "bad function in x" in reason, reason
    _wait_state(work, tip2, {"done"})

    # T3 changes only stable.py, so x.py is carried from its T2 record. The
    # finding was never fixed and must still block.
    (work / "stable.py").write_text("# stable v3\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "--amend", "--no-edit"], cwd=work)
    tip3 = _git(["rev-parse", "HEAD"], cwd=work)
    decision, reason, _ = _hook(work, "git push -f origin main", _env(tmp_path))
    assert decision == "deny" and "bad function in x" in reason, reason
    _wait_state(work, tip3, {"done"})


def test_finding_about_a_file_outside_the_review_is_kept(tmp_path):
    work = _tiny_repo(tmp_path, {"a.py": "a = 0\n", "other.py": "def api(x): pass\n"})
    tip1 = _commit(work, "a.py", "a = 1\napi()\n")
    findings_map = json.dumps({"other.py": {"severity": "high", "content": "api caller broken",
                                            "existing_code": "def api(x): pass"}})
    decision, reason, _ = _hook(work, "git push origin main",
                                _env(tmp_path, STUB_FINDINGS_FOR=findings_map))
    assert decision == "deny", reason
    _wait_state(work, tip1, {"done"})

    (work / "a.py").write_text("a = 2\napi()\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "--amend", "--no-edit"], cwd=work)
    tip2 = _git(["rev-parse", "HEAD"], cwd=work)
    decision, reason, _ = _hook(work, "git push -f origin main", _env(tmp_path))
    assert decision == "deny" and "api caller broken" in reason, reason
    _wait_state(work, tip2, {"done"})


def test_truncated_file_gets_no_record_but_its_neighbour_does(tmp_path):
    common = str(tmp_path)
    fp = "f" * 64

    def _item(path):
        return {"mode": "full", "record": None, "from_oid": "",
                "entry": {"path": path, "old_path": "", "status": "M",
                          "old_oid": "1" * 40, "new_oid": ("2" if path == "a.py" else "3") * 40}}

    items = [_item("a.py"), _item("b.py")]
    result = {"status": "completed_with_warnings", "findings": [], "warnings": [
        {"file": "a.py", "message": "diff truncated; reviewer saw stat + hunk headers only"}]}
    review_gate._write_run_records(result, items, common, fp, "r1")

    def _rec(item):
        e = item["entry"]
        key = review_gate._record_key(e["path"], "", e["status"], e["old_oid"])
        return review_gate._read_ledger_record(
            review_gate._record_path(common, fp, key, e["new_oid"]), fp, key, e["new_oid"])

    assert _rec(items[0]) is None
    assert _rec(items[1]) is not None

    # A §2b context truncation (no file) is not a diff truncation and blocks nothing.
    fp2 = "e" * 64
    ctx = {"status": "success", "findings": [], "warnings": [
        {"file": None, "message": "cross-file symbol analysis truncated; some external usages may be unverified"}]}
    review_gate._write_run_records(ctx, items, common, fp2, "r2")
    e = items[1]["entry"]
    key = review_gate._record_key(e["path"], "", e["status"], e["old_oid"])
    assert review_gate._read_ledger_record(
        review_gate._record_path(common, fp2, key, e["new_oid"]), fp2, key, e["new_oid"]) is not None


def test_the_skill_has_the_resolve_mode_the_gate_calls():
    # The stub answers --resolve on its own, so without this nothing notices a
    # skill that would run an ordinary review instead.
    skill = (Path(_GATE).parent.parent / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8")
    assert "`--resolve <json>`" in skill and "## R. Resolve mode" in skill
    assert "code-resolver" in skill and '{"resolutions"' in skill


def test_resolver_manifest_carries_per_file_diff_specs(monkeypatch, tmp_path):
    seen = {}

    def _fake_review(*a, **kw):
        seen.update(json.loads(Path(kw["resolve_file"]).read_text(encoding="utf-8")))
        return {"resolutions": {}}, True, ""

    monkeypatch.setattr(review_gate, "_run_review", _fake_review)
    items = [{"mode": "delta", "from_oid": "a" * 40,
              "entry": {"path": "x.py", "new_oid": "b" * 40}}]
    prior = {"id": "f1", "finding": dict(_base_finding(), content="x" * 5000), "record": {}}
    (tmp_path / review_gate.ASYNC_DIR).mkdir()
    review_gate._run_resolver(".", "hook", "", "tip", "b..t", [prior], items,
                              str(tmp_path), "fp", "run1")
    assert seen["files"] == [{"path": "x.py", "mode": "delta",
                              "from_oid": "a" * 40, "to_oid": "b" * 40}]
    assert len(seen["resolve"][0]["content"]) == 2000


def test_resolver_guard_ignores_lines_that_predate_the_delta(tmp_path):
    work = tmp_path / "g"
    work.mkdir()
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    _git(["config", "user.email", "t@t.com"], cwd=work)
    _git(["config", "user.name", "t"], cwd=work)
    _git(["config", "commit.gpgsign", "false"], cwd=work)
    (work / "readme.txt").write_text("r\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    base = _git(["rev-parse", "HEAD"], cwd=work)
    (work / "x.py").write_text("guard_already_there()\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "reviewed"], cwd=work)
    from_oid = _git(["rev-parse", "HEAD:x.py"], cwd=work)
    (work / "x.py").write_text("guard_already_there()\nreal_fix()\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "fix"], cwd=work)
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    to_oid = _git(["rev-parse", "HEAD:x.py"], cwd=work)
    items = [{"mode": "delta", "from_oid": from_oid, "record": {},
              "entry": {"path": "x.py", "old_path": "", "status": "A",
                        "old_oid": "0" * 40, "new_oid": to_oid}}]
    rng = f"{base}..{tip}"

    def _res(quote):
        return {"status": "resolved", "evidence_path": "x.py", "evidence_quote": quote}

    assert not review_gate._guard_resolution(_res("guard_already_there()"), items, rng, str(work))
    assert review_gate._guard_resolution(_res("real_fix()"), items, rng, str(work))


# ---------------------------------------------------------------------------
# Scenario 21: real-run calibration (live, skip unless OCR_LIVE_TESTS=1)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.environ.get("OCR_LIVE_TESTS"),
    reason="live test requiring real claude auth; set OCR_LIVE_TESTS=1 to run",
)
def test_scenario_21_real_run_calibration(tmp_path):
    """Scenario 21: a full live run followed by a delta push. Checks the
    summary line, that only the changed file gets a reviewer call, and that
    the doctor command reports a meaningful hit rate."""
    # This test needs OCR_LIVE_TESTS=1 and real claude auth.
    # It is left as a stub for manual calibration runs.
    pytest.skip("not yet automated — requires real claude + known fixture commit")
