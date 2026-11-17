"""Independent static-lens contracts for explicit SPEC-16 final review."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rg_final_review  # noqa: E402
from rg_final_lenses import (  # noqa: E402
    CROSS_ITEM_CONTRACTS,
    INTEGRATION_BEHAVIOR,
    REQUIRED_LENSES,
    aggregate_lenses,
    build_lens_request,
    run_independent_lenses,
)
from rg_final_store import FinalStore  # noqa: E402


LENS_NAMES = (
    "correctness",
    "repository-standards",
    "documentation/domain",
    "testing",
    "security",
    "compatibility/migrations",
    "change-intent",
)


def _request(tmp_path):
    return {
        "schema": "final-review-worker-request/v1",
        "stage": "S16-02A",
        "run_id": "run-1",
        "unit_id": "unit-1",
        "item": {"title": "A pinned change", "description": "Implement the contract"},
        "revision_tuple": [1, "feature", "a" * 40],
        "repository": {
            "path": str(tmp_path),
            "access": "full_tree",
            "head_sha": "a" * 40,
            "target_sha": "b" * 40,
            "merge_base_sha": "c" * 40,
        },
        "diff": "diff --git a/app.py b/app.py\n+return 1\n",
    }


def _response(findings=(), warnings=(), **overrides):
    response = {
        "schema": "final-review-worker-result/v1",
        "status": "success",
        "verdict": "pass",
        "summary": "worker summary",
        "findings": list(findings),
        "warnings": list(warnings),
        "unreviewed": [],
    }
    response.update(overrides)
    return response


def test_all_mandatory_lenses_run_independently_with_full_repository_requests(tmp_path):
    base = _request(tmp_path)
    original = deepcopy(base)
    requests = []

    def worker(request):
        requests.append(request)
        # A hostile or buggy worker cannot contaminate another lens's input.
        request["repository"]["path"] = "worker-mutated"
        request["item"]["title"] = "worker-mutated"
        return _response()

    aggregate = run_independent_lenses(base, worker)

    assert tuple(spec.name for spec in REQUIRED_LENSES) == LENS_NAMES
    assert [request["lens"] for request in requests] == list(LENS_NAMES)
    assert base == original
    assert len({id(request) for request in requests}) == len(LENS_NAMES)
    assert len({id(request["repository"]) for request in requests}) == len(LENS_NAMES)
    for request, spec in zip(requests, REQUIRED_LENSES):
        assert request["repository"]["access"] == "full_tree"
        assert request["repository"]["merge_base_sha"] == "c" * 40
        assert request["diff"] == base["diff"]
        assert request["review_duty"] == spec.duty
        assert request["capabilities"] == []
        for forbidden in ("findings", "warnings", "lens_results", "aggregation", "verdict"):
            assert forbidden not in request
    assert aggregate.complete is True
    assert aggregate.finding_verdict == "pass"
    assert [outcome.state for outcome in aggregate.outcomes] == ["completed"] * 7


def test_blocking_finding_does_not_short_circuit_lenses_and_model_verdict_is_ignored(tmp_path):
    requests = []

    def worker(request):
        requests.append(request["lens"])
        if request["lens"] == "correctness":
            return _response(
                findings=[{
                    "id": "defect",
                    "path": "app.py",
                    "line": 2,
                    "severity": "critical",
                    "confidence": 0.98,
                    "message": "The return value breaks its caller.",
                }],
                verdict="pass",
            )
        if request["lens"] == "testing":
            return _response(
                findings=[{
                    "path": "app.py",
                    "start_line": 2,
                    "end_line": 2,
                    "severity": "medium",
                    "confidence": 0.2,
                    "content": "The edge case lacks direct coverage.",
                }],
                warnings=["Optional follow-up remains visible"],
                verdict="block",
            )
        return _response(verdict="block")

    aggregate = run_independent_lenses(_request(tmp_path), worker)

    assert requests == list(LENS_NAMES)
    assert aggregate.complete is True
    assert aggregate.finding_verdict == "block"
    assert [(finding["lens"], finding["content"]) for finding in aggregate.findings] == [
        ("correctness", "The return value breaks its caller."),
        ("testing", "The edge case lacks direct coverage."),
    ]
    assert aggregate.findings[0]["severity"] == "high"
    assert aggregate.outcomes[3].state == "completed_with_warnings"
    assert aggregate.warnings[0]["lens"] == "testing"
    assert aggregate.warnings[0]["scope"] == "testing"


def test_required_lens_failure_is_incomplete_and_other_lenses_still_finish(tmp_path):
    calls = []

    def worker(request):
        calls.append(request["lens"])
        if request["lens"] == "security":
            raise RuntimeError("security backend failed")
        return _response()

    aggregate = run_independent_lenses(_request(tmp_path), worker)

    assert calls == list(LENS_NAMES)
    assert aggregate.complete is False
    assert aggregate.finding_verdict is None
    security = aggregate.outcomes[LENS_NAMES.index("security")]
    assert security.state == "incomplete"
    assert "security backend failed" in security.error
    assert security.warnings[0]["code"] == "required-lens-failed"
    assert security.warnings[0]["lens"] == "security"
    assert [outcome.state for outcome in aggregate.outcomes if outcome.name != "security"] == [
        "completed"
    ] * 6


def test_invalid_worker_result_is_not_a_warning_only_success(tmp_path):
    def worker(request):
        if request["lens"] == "documentation/domain":
            response = _response()
            response["warnings"] = "not-an-array"
            return response
        return _response()

    aggregate = run_independent_lenses(_request(tmp_path), worker)

    docs = aggregate.outcomes[LENS_NAMES.index("documentation/domain")]
    assert docs.state == "incomplete"
    assert "warnings must be arrays" in docs.error
    assert aggregate.complete is False
    assert aggregate.finding_verdict is None


def test_intent_falsification_filter_applies_to_every_lens_and_is_visible(tmp_path):
    def worker(request):
        if request["lens"] == "change-intent":
            return _response(findings=[{
                "type": "intent_gap",
                "severity": "blocking",
                "claim": "Implement the contract",
                # Missing against/self_check evidence: this cannot block or disappear quietly.
            }])
        return _response()

    aggregate = run_independent_lenses(_request(tmp_path), worker)

    assert aggregate.findings == ()
    assert aggregate.finding_verdict == "pass"
    intent = aggregate.outcomes[-1]
    assert intent.state == "completed_with_warnings"
    assert [warning["code"] for warning in intent.warnings] == ["intent-validation"]
    assert intent.warnings[0]["details"] == {"excluded": 1}


def test_unreviewed_scope_warns_but_does_not_manufacture_a_block(tmp_path):
    def worker(request):
        if request["lens"] == "compatibility/migrations":
            return _response(unreviewed=["large binary fixture"])
        return _response()

    aggregate = run_independent_lenses(_request(tmp_path), worker)

    assert aggregate.complete is True
    assert aggregate.finding_verdict == "warn"
    outcome = aggregate.outcomes[LENS_NAMES.index("compatibility/migrations")]
    assert outcome.state == "completed_with_warnings"
    assert outcome.unreviewed == ("large binary fixture",)


def test_cross_item_and_integration_are_reusable_labels_not_standalone_work():
    assert CROSS_ITEM_CONTRACTS == "cross-item-contracts"
    assert INTEGRATION_BEHAVIOR == "integration-behavior"
    assert CROSS_ITEM_CONTRACTS not in LENS_NAMES
    assert INTEGRATION_BEHAVIOR not in LENS_NAMES

    spec = deepcopy(REQUIRED_LENSES[0])
    request = build_lens_request(
        _request(Path("/unused")),
        spec,
        capabilities=(CROSS_ITEM_CONTRACTS, INTEGRATION_BEHAVIOR),
    )

    assert request["capabilities"] == [CROSS_ITEM_CONTRACTS, INTEGRATION_BEHAVIOR]
    assert request["lens"] == "correctness"


def test_aggregate_lenses_requires_every_applicable_lens(tmp_path):
    aggregate = run_independent_lenses(_request(tmp_path), lambda request: _response())

    incomplete = list(aggregate.outcomes[:-1])
    partial = aggregate_lenses(incomplete)

    assert partial.complete is False
    assert partial.finding_verdict is None
    assert partial.missing_lenses == ("change-intent",)


def _provider_item():
    return {
        "provider": "gitlab",
        "id": "17",
        "iid": 17,
        "url": "https://gitlab.example.test/group/project/-/merge_requests/17",
        "title": "Implement the pinned contract",
        "description": "Return the normalized value.",
        "state": "opened",
        "author": {"username": "author"},
        "source": {
            "project_id": 2,
            "project_path": "fork/project",
            "branch": "feature",
            "sha": "a" * 40,
            "url": "https://gitlab.example.test/fork/project.git",
        },
        "target": {
            "project_id": 1,
            "project_path": "group/project",
            "branch": "main",
            "sha": "b" * 40,
            "base_sha": "c" * 40,
            "url": "https://gitlab.example.test/group/project.git",
        },
    }


@pytest.fixture
def orchestrated(tmp_path, monkeypatch):
    repository = tmp_path / "immutable"
    (repository / "docs").mkdir(parents=True)
    (repository / "README.md").write_text("unchanged repository guidance\n", encoding="utf-8")
    (repository / "docs" / "ADR.md").write_text("binding architecture decision\n", encoding="utf-8")
    item = _provider_item()
    provider = SimpleNamespace(
        read=lambda reference, repo_root=None: deepcopy(item),
        revalidate=lambda pinned, repo_root=None: None,
    )
    requests = []
    stages = []
    store_path = tmp_path / "final.sqlite3"

    def run(worker, materialization_error=None):
        def invoke(request, **kwargs):
            requests.append(deepcopy(request))
            assert kwargs["command"] is None
            return worker(request)

        class Materialization:
            def __enter__(self):
                if materialization_error:
                    raise materialization_error
                return SimpleNamespace(
                    path=repository,
                    head_sha=item["source"]["sha"],
                    target_sha=item["target"]["sha"],
                    merge_base_sha=item["target"]["base_sha"],
                    diff=_request(repository)["diff"],
                    submodules=(),
                    lfs_files=(),
                    lfs_complete=True,
                )

            def __exit__(self, exc_type, exc, traceback):
                return False

        def context(review, **kwargs):
            return {
                "contract": "final-review-context/v1",
                "review_mode": "complete-repository",
                "search_roots": [str(repository)],
                "diff": review.diff,
                "documents": [],
                "files": {"all": ["app.py"], "implementation": ["app.py"], "tests": []},
                "intent": {
                    "description": {"text": item["description"], "source": "provider"},
                    "tickets": [], "references": [], "listed_only": [], "warnings": [],
                },
                "completeness": {"complete": True, "submodules": [], "lfs_files": []},
                "warnings": [],
            }

        def required(repository_arg, commit):
            stages.append("required")
            return {
                "status": "absent", "blocking": False, "commit": commit,
                "suites": [], "findings": [], "warnings": [],
            }

        def dynamic(repository_arg, target_sha, head_sha, **kwargs):
            stages.append("dynamic")
            return {
                "schema": "final-review-dynamic-result/v1",
                "unit_key": kwargs["unit_key"], "state": "not_applicable",
                "complete": True, "findings": [], "warnings": [], "tests": [],
                "coverage": {"status": "not_applicable", "summary": "No dynamic subject"},
                "telemetry": {"executor_attempts": 1},
                "revisions": {"target_sha": target_sha, "head_sha": head_sha,
                              "merge_base_sha": item["target"]["base_sha"]},
            }

        monkeypatch.setattr(rg_final_review, "invoke_worker", invoke)
        monkeypatch.setattr(rg_final_review, "collect_review_context", context)
        monkeypatch.setattr(rg_final_review, "materialize_review", lambda *args, **kwargs: Materialization())
        output = rg_final_review.run_final_review(
            "!17", repo_root=tmp_path, provider=provider, store_path=store_path,
            required_runner=required, dynamic_runner=dynamic, environ={},
        )
        with FinalStore(store_path) as store:
            manifest = store.machine_report(output["run_id"])
        return output, manifest

    return SimpleNamespace(run=run, requests=requests, stages=stages, repository=repository)


def _lens_coverage(unit):
    return {row["kind"]: row for row in unit["coverage"]}


def test_standalone_records_all_lens_results_before_finding_aggregation(orchestrated):
    def worker(request):
        if request["lens"] == "repository-standards":
            return _response(
                findings=[{
                    "id": "standard-contract",
                    "path": "app.py",
                    "start_line": 2,
                    "end_line": 2,
                    "severity": "high",
                    "confidence": 0.95,
                    "content": "The change violates the binding repository error contract.",
                }],
                warnings=[{
                    "code": "optional-standard", "message": "Consider a focused follow-up",
                }],
            )
        return _response()

    output, manifest = orchestrated.run(worker)

    assert [request["lens"] for request in orchestrated.requests] == [
        *LENS_NAMES, *LENS_NAMES,
    ]
    assert orchestrated.stages == ["required", "dynamic", "required", "dynamic"]
    assert output["stage"] == "S16-07"
    assert output["complete"] is True
    assert output["execution_complete"] is True
    assert output["state"] == "findings"
    assert output["finding_verdict"] == "block"
    assert output["exit_code"] == 1
    assert output["missing_stages"] == []
    individual = manifest["units"][0]
    lenses = {lens["name"]: lens for lens in individual["lenses"]}
    assert list(lenses) == list(LENS_NAMES)
    assert lenses["repository-standards"]["state"] == "completed_with_warnings"
    assert all(lens["state"] == "completed" for name, lens in lenses.items()
               if name != "repository-standards")
    assert all(lens["evidence"]["repository_access"] == "full_tree" for lens in lenses.values())
    assert all(lens["evidence"]["diff_sha256"] for lens in lenses.values())
    coverage = _lens_coverage(individual)
    assert coverage["mandatory_lenses"]["status"] == "complete"
    for name in LENS_NAMES:
        assert coverage[f"lens:{name}"]["status"] == lenses[name]["state"]
    assert individual["state"] == "findings"
    assert len(individual["findings"]) == 1
    assert individual["findings"][0]["lens"] == "repository-standards"
    assert individual["findings"][0]["finding_key"] == "standard-contract"
    assert [warning["code"] for warning in individual["warnings"]] == ["optional-standard"]
    assert individual["warnings"][0]["details"]["lens"] == "repository-standards"
    assert manifest["units"][1]["state"] == "findings"


def test_standalone_required_failure_persists_partial_evidence_but_never_passes(orchestrated):
    def worker(request):
        if request["lens"] == "security":
            raise RuntimeError("credential-free security worker failed")
        if request["lens"] == "correctness":
            return _response(findings=[{
                "path": "app.py", "start_line": 2, "end_line": 2,
                "severity": "high", "confidence": 0.99, "content": "Pinned defect",
            }])
        return _response()

    output, manifest = orchestrated.run(worker)

    assert len(orchestrated.requests) == 14
    assert orchestrated.stages == ["required", "dynamic", "required", "dynamic"]
    assert output["state"] == "incomplete"
    assert output["finding_verdict"] is None
    assert output["exit_code"] == 2
    unit = manifest["units"][0]
    lenses = {lens["name"]: lens for lens in unit["lenses"]}
    assert len(lenses) == 7
    assert lenses["security"]["state"] == "incomplete"
    assert lenses["correctness"]["state"] == "completed"
    assert _lens_coverage(unit)["mandatory_lenses"]["status"] == "incomplete"
    assert _lens_coverage(unit)["lens:security"]["status"] == "incomplete"
    assert any(warning["code"] == "required-lens-failed" and
               warning["details"]["lens"] == "security" for warning in unit["warnings"])
    assert unit["findings"][0]["content"] == "Pinned defect"


def test_materialization_failure_records_every_required_lens_as_incomplete(orchestrated):
    def worker(request):
        raise AssertionError("no worker may run without the immutable full repository")

    output, manifest = orchestrated.run(worker, RuntimeError("pinned clone unavailable"))

    assert output["finding_verdict"] is None
    assert output["state"] == "incomplete"
    assert orchestrated.requests == []
    assert orchestrated.stages == []
    unit = manifest["units"][0]
    lenses = {lens["name"]: lens for lens in unit["lenses"]}
    assert set(lenses) == set(LENS_NAMES)
    assert all(lens["state"] == "incomplete" for lens in lenses.values())
    assert _lens_coverage(unit)["mandatory_lenses"]["status"] == "incomplete"
    assert any(warning["code"] == "materialization" for warning in unit["warnings"])
