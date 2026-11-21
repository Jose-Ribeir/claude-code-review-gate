"""Quality guard (ticket 07): scripts/rg_quality_guard.py.

* The catch-rate check runs the four planted-defect fixtures
  (tests/fixtures/miss_categories) through the REAL gate with the stub reviewer.
  This file is the CI wiring: every change must keep every category caught.
* A deliberately weakened reviewer makes the same check fail -- the proof the
  guard can fail at all.
* The comparison flags a category/finding the later run lost, from recorded
  outcomes (no gate run needed).
* The real-model command is exercised with ``--stub`` only (no model cost): the
  cost is stated and confirmed before anything runs, and a missing recorded
  range fails clearly.
"""
import json
import os
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)

import rg_quality_guard as qg  # noqa: E402


# ---------------------------------------------------------------------------
# Catch-rate with the stub reviewer (real gate, ~5 s per fixture)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def scripted_outcome():
    return qg.run_catch_rate(reviewer="scripted", label="scripted")


@pytest.fixture(scope="module")
def weakened_outcome():
    # M1 and M3 are enough to prove the guard fails: a plain finding and the
    # eleven-caller impact finding.
    return qg.run_catch_rate(reviewer="weakened", categories=["m1", "m3"], label="weakened")


class TestCatchRate:
    def test_every_planted_defect_is_caught_by_the_scripted_reviewer(self, scripted_outcome):
        """The required check for every speed change."""
        assert set(scripted_outcome["cases"]) == set(qg.CATEGORIES)
        assert qg.catch_failed(scripted_outcome) == [], qg.format_outcome(scripted_outcome)

    def test_m3_catches_all_eight_spec_files(self, scripted_outcome):
        case = scripted_outcome["cases"]["m3"]
        assert len(case["required_paths"]) == 8
        assert case["missing_paths"] == []

    def test_outcome_carries_findings_wall_time_and_split(self, scripted_outcome):
        for name, case in scripted_outcome["cases"].items():
            assert case["findings"], name
            assert case["wall_s"] > 0
            assert set(case["split"]) >= {"model_s", "model_turns", "lenses_s", "tests_s",
                                          "probes_s"}

    def test_weakened_reviewer_fails_the_check(self, weakened_outcome):
        """Proof the guard works: a reviewer that reports nothing is caught."""
        assert qg.catch_failed(weakened_outcome) == ["m1", "m3"]
        assert weakened_outcome["cases"]["m3"]["findings"] == []

    def test_comparison_flags_the_weakened_run_as_a_regression(
            self, scripted_outcome, weakened_outcome):
        scripted = dict(scripted_outcome)
        scripted["cases"] = {k: v for k, v in scripted_outcome["cases"].items()
                             if k in weakened_outcome["cases"]}
        cmp_ = qg.compare_outcomes(scripted, weakened_outcome)
        assert cmp_["ok"] is False
        assert cmp_["lost_categories"] == ["m1", "m3"]
        assert cmp_["lost_findings"]

    def test_comparison_of_a_run_with_itself_is_clean(self, scripted_outcome):
        cmp_ = qg.compare_outcomes(scripted_outcome, scripted_outcome)
        assert cmp_["ok"] is True
        assert cmp_["lost_findings"] == [] and cmp_["gained_findings"] == []

    def test_cli_exit_codes(self, capsys):
        assert qg.main(["catch-rate", "--categories", "m1"]) == 0
        assert qg.main(["catch-rate", "--categories", "m1", "--reviewer", "weakened"]) == 1
        out = capsys.readouterr().out
        assert "CATCH-RATE OK" in out and "CATCH-RATE FAIL" in out

    def test_unknown_category_and_reviewer_are_input_errors(self, capsys):
        assert qg.main(["catch-rate", "--categories", "m9"]) == 2
        assert "unknown category" in capsys.readouterr().err
        with pytest.raises(qg.GuardError):
            qg.run_catch_rate(reviewer="strong")


# ---------------------------------------------------------------------------
# Comparison from recorded outcomes
# ---------------------------------------------------------------------------

def _finding(path, line=1, category="correctness", content="x"):
    return {"path": path, "start_line": line, "severity": "high",
            "category": category, "content": content}


def _outcome(kind, label, cases):
    return {"schema": qg.SCHEMA, "kind": kind, "label": label, "cases": cases}


def _case(findings, wall=10.0, split=None, **extra):
    case = {"findings": findings, "wall_s": wall, "split": split or {"model_s": wall - 2}}
    case.update(extra)
    return case


class TestCompare:
    def test_lost_finding_is_flagged_even_when_faster(self):
        before = _outcome("real-model", "old", {
            "mr-1000": _case([_finding("a.py"), _finding("b.py")], wall=100)})
        after = _outcome("real-model", "new", {
            "mr-1000": _case([_finding("a.py")], wall=30)})
        cmp_ = qg.compare_outcomes(before, after)
        assert cmp_["ok"] is False
        assert [f["finding"] for f in cmp_["lost_findings"]] == ["b.py [correctness]"]
        assert cmp_["timing"]["mr-1000"]["wall_s"] == [100, 30]
        text = qg.format_comparison(cmp_)
        assert "LOST FINDING" in text and "b.py" in text and "100 -> 30 (-70" in text
        assert "FAIL" in text

    def test_real_model_findings_match_on_file_and_category_not_line_or_wording(self):
        before = _outcome("real-model", "old", {"c": _case([_finding("a.py", 10, content="foo")])})
        after = _outcome("real-model", "new", {"c": _case([_finding("a.py", 14, content="bar")])})
        assert qg.compare_outcomes(before, after)["ok"] is True

    def test_gained_findings_are_reported_not_failed(self):
        before = _outcome("real-model", "old", {"c": _case([_finding("a.py")])})
        after = _outcome("real-model", "new", {
            "c": _case([_finding("a.py"), _finding("z.py")])})
        cmp_ = qg.compare_outcomes(before, after)
        assert cmp_["ok"] is True
        assert [g["finding"] for g in cmp_["gained_findings"]] == ["z.py [correctness]"]

    def test_missing_case_and_unmeasured_case_fail(self):
        before = _outcome("real-model", "old", {"a": _case([]), "b": _case([])})
        after = _outcome("real-model", "new", {"a": _case([], error="no gate run record")})
        cmp_ = qg.compare_outcomes(before, after)
        assert cmp_["lost_cases"] == ["b"]
        assert cmp_["errors"] == ["a: no gate run record"]
        assert cmp_["ok"] is False

    def test_catch_rate_category_loss(self):
        before = _outcome("catch-rate", "old", {
            "m1": _case([_finding("a.py")], caught=True),
            "m2": _case([_finding("b.py")], caught=True)})
        after = _outcome("catch-rate", "new", {
            "m1": _case([_finding("a.py")], caught=True),
            "m2": _case([], caught=False)})
        cmp_ = qg.compare_outcomes(before, after)
        assert cmp_["lost_categories"] == ["m2"]
        assert "LOST CATEGORY  m2" in qg.format_comparison(cmp_)

    def test_catch_rate_category_that_was_never_caught_is_not_a_loss(self):
        before = _outcome("catch-rate", "old", {"m1": _case([], caught=False)})
        after = _outcome("catch-rate", "new", {"m1": _case([], caught=False)})
        assert qg.compare_outcomes(before, after)["ok"] is True

    def test_mixed_kinds_and_foreign_files_are_input_errors(self):
        a = _outcome("real-model", "a", {})
        b = _outcome("catch-rate", "b", {})
        with pytest.raises(qg.GuardError, match="cannot compare"):
            qg.compare_outcomes(a, b)
        with pytest.raises(qg.GuardError, match="not a quality-guard-outcome"):
            qg.compare_outcomes({"cases": {}}, a)

    def test_compare_command_reads_files_and_sets_the_exit_code(self, tmp_path, capsys):
        old = tmp_path / "old.json"
        new = tmp_path / "new.json"
        old.write_text(json.dumps(_outcome("real-model", "old", {"c": _case([_finding("a.py")])})))
        new.write_text(json.dumps(_outcome("real-model", "new", {"c": _case([])})))
        assert qg.main(["compare", str(old), str(new)]) == 1
        assert "LOST FINDING" in capsys.readouterr().out
        assert qg.main(["compare", str(old), str(old)]) == 0
        assert qg.main(["compare", str(old), str(tmp_path / "nope.json")]) == 2
        assert "cannot read outcome file" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Time split
# ---------------------------------------------------------------------------

def test_time_split_buckets_model_lenses_tests_and_probes():
    record = {
        "phases": [{"name": "worktree", "seconds": 1.0}, {"name": "impact", "seconds": 2.5},
                   {"name": "review", "seconds": 40.0}, {"name": "lens-security", "seconds": 7.0},
                   {"name": "finish", "seconds": 0.5}],
        "calls": [{"kind": "review", "seconds": 30.0, "turns": 12},
                  {"kind": "review", "seconds": 8.0, "turns": 4},
                  {"kind": "resolve", "seconds": 1.0}],
        "command_checks": [{"name": "unit", "seconds": 4.2}],
    }
    split = qg.time_split(record)
    assert split == {"model_s": 39.0, "model_calls": 3, "model_turns": 16, "lenses_s": 7.0,
                     "tests_s": 4.2, "probes_s": 3.5}
    assert qg.time_split(None)["model_calls"] == 0


# ---------------------------------------------------------------------------
# real-model: cost statement, confirmation, inputs (stub reviewer only)
# ---------------------------------------------------------------------------

class TestRealModel:
    def test_cost_is_stated_and_confirmation_is_required(self, capsys):
        rows = [{"input": "mr-1000", "lines": 1000, "usd": 1.0}]
        assert qg.confirm_cost(1.0, rows, 1, False, False, interactive=False) is False
        out = capsys.readouterr()
        assert "Expected cost: about $1.00" in out.out
        assert "Refusing to spend model money" in out.err
        assert qg.confirm_cost(1.0, rows, 1, False, False, ask=lambda _p: "y",
                               interactive=True) is True
        assert qg.confirm_cost(1.0, rows, 1, False, False, ask=lambda _p: "n",
                               interactive=True) is False
        assert qg.confirm_cost(1.0, rows, 1, False, True) is True
        assert qg.confirm_cost(1.0, rows, 1, True, False) is True

    def test_declined_run_spends_nothing(self, capsys, monkeypatch):
        monkeypatch.setattr(qg.sys.stdin, "isatty", lambda: False, raising=False)
        assert qg.main(["real-model", "--inputs", "mr-1000"]) == 2
        out = capsys.readouterr()
        assert "Expected cost" in out.out
        assert "Running gate version" not in out.out
        assert "Aborted before spending anything" in out.err

    def test_cost_estimate_scales_with_lines_and_versions(self):
        small = [{"name": "a", "steps": [{"lines": 100}]}]
        big = [{"name": "b", "steps": [{"lines": 10000}]}]
        t_small, _ = qg.estimate_cost(small)
        t_big, rows = qg.estimate_cost(big)
        assert t_big > t_small
        assert rows[0]["lines"] == 10000
        assert qg.estimate_cost(small, versions=2)[0] == round(2 * t_small, 2)

    def test_missing_recorded_range_fails_clearly(self, tmp_path, capsys):
        import subprocess
        repo = tmp_path / "other"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        code = qg.main(["real-model", "--stub", "--inputs", "recorded-range",
                        "--repo", str(repo)])
        err = capsys.readouterr().err
        assert code == 2
        assert "1dacf08..673b61d" in err and "is not in the history" in err
        assert "git fetch" in err

    def test_malformed_range_is_an_input_error(self, tmp_path, capsys):
        assert qg.main(["real-model", "--stub", "--inputs", "recorded-range",
                        "--recorded-range", "nodots"]) == 2
        assert "BASE..TIP" in capsys.readouterr().err

    def test_stub_run_reports_findings_wall_time_and_split_for_each_input(
            self, tmp_path, capsys):
        out_dir = tmp_path / "out"
        code = qg.main(["real-model", "--stub", "--inputs", "fix-after-block,mr-1000",
                        "--out-dir", str(out_dir)])
        text = capsys.readouterr().out
        assert code == 0, text
        assert "fix-after-block" in text and "mr-1000" in text
        assert "SQL built from user input" in text
        assert "wall " in text and "model " in text and "lenses " in text
        assert "tests " in text and "probes " in text
        (path,) = list(out_dir.glob("real-model-1-*.json"))
        outcome = json.loads(path.read_text(encoding="utf-8"))
        assert outcome["stub"] is True
        assert set(outcome["cases"]) == {"fix-after-block", "mr-1000"}
        assert outcome["cases"]["fix-after-block"]["lines"] == 2
        assert outcome["cases"]["mr-1000"]["lines"] > 900
        assert qg.compare_outcomes(outcome, outcome)["ok"] is True


class TestGateVersions:
    def test_a_ref_is_extracted_from_git_history(self, tmp_path):
        root, label = qg.resolve_gate(tmp_path, gate_ref="HEAD")
        assert label == "HEAD"
        assert (root / "scripts" / "review-gate.py").is_file()
        assert (root / "tests" / "stub_reviewer.py").is_file()

    def test_unknown_ref_and_both_selectors_are_input_errors(self, tmp_path):
        with pytest.raises(qg.GuardError, match="cannot read gate version"):
            qg.resolve_gate(tmp_path, gate_ref="no-such-ref-xyz")
        with pytest.raises(qg.GuardError, match="not both"):
            qg.resolve_gate(tmp_path, gate_ref="HEAD", gate_root=str(tmp_path))
        with pytest.raises(qg.GuardError, match="no scripts/review-gate.py"):
            qg.resolve_gate(tmp_path, gate_root=str(tmp_path))
