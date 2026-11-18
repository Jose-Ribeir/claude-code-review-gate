"""Independent static-lens contracts for explicit SPEC-16 final review."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rg_final_review  # noqa: E402
import rg_final_lenses  # noqa: E402
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


def test_lenses_are_bounded_concurrent_but_keep_raw_identity_and_result_order(tmp_path):
    lock = threading.Lock()
    active = 0
    maximum = 0

    def worker(request):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            # Deliberately finish in a different order from dispatch. A response
            # must still be normalized against the request's own raw lens ID.
            delay = (len(LENS_NAMES) - LENS_NAMES.index(request["lens"])) * 0.08
            time.sleep(delay)
            return _response(summary=request["lens"], findings=[{
                "path": "app.py", "start_line": 2, "end_line": 2,
                "severity": "low", "confidence": 0.9,
                "content": request["lens"],
            }])
        finally:
            with lock:
                active -= 1

    aggregate = run_independent_lenses(
        _request(tmp_path), worker, max_workers=4, environ={},
    )

    assert 2 <= maximum <= 4
    assert aggregate.complete is True
    assert [outcome.name for outcome in aggregate.outcomes] == list(LENS_NAMES)
    assert [outcome.summary for outcome in aggregate.outcomes] == list(LENS_NAMES)
    assert [finding["lens"] for finding in aggregate.findings] == list(LENS_NAMES)
    assert [finding["content"] for finding in aggregate.findings] == list(LENS_NAMES)


@pytest.mark.parametrize("value,expected", [
    (None, 4), ("", 4), ("invalid", 4), ("0", 1), ("-8", 1),
    ("1", 1), ("2", 2), ("4", 4), ("99", 4),
])
def test_lens_worker_operator_limit_is_clamped(value, expected):
    environ = {} if value is None else {"RG_FINAL_LENS_WORKERS": value}
    assert rg_final_lenses.lens_worker_limit(environ) == expected


@pytest.mark.parametrize("status", ["success", "completed", "completed_with_warnings"])
def test_only_the_exact_successful_status_enum_is_accepted(tmp_path, status):
    aggregate = run_independent_lenses(
        _request(tmp_path), lambda request: _response(status=status), max_workers=1,
    )
    assert aggregate.complete is True

    rejected = run_independent_lenses(
        _request(tmp_path), lambda request: _response(status=status.upper()),
        max_workers=1,
    )
    assert rejected.complete is False
    assert all(outcome.state == "incomplete" for outcome in rejected.outcomes)
    assert rejected.outcomes[0].warnings[0]["details"]["received_status"] == status.upper()


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
            raise rg_final_review.WorkerError(
                "security backend failed",
                details={"received_status": "finished", "return_code": 0,
                         "stdout_tail": "status diagnostic", "reason": "invalid-status"},
            )
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
    assert security.warnings[0]["details"]["received_status"] == "finished"
    assert security.warnings[0]["details"]["return_code"] == 0
    assert security.warnings[0]["details"]["stdout_tail"] == "status diagnostic"
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


@pytest.mark.parametrize("placeholder", [None, "", "   ", "...", "<summary>", "{{summary}}"])
def test_placeholder_summary_is_incomplete_without_dropping_valid_findings(
        tmp_path, placeholder):
    finding = {
        "path": "app.py", "start_line": 2, "end_line": 2,
        "severity": "high", "confidence": 0.99, "content": "Caller contract is broken",
    }

    def worker(request):
        if request["lens"] == "correctness":
            response = _response(findings=[finding], summary=placeholder)
            if placeholder is None:
                del response["summary"]
            return response
        return _response(summary="Inspected app.py and found no issue in this lens")

    aggregate = run_independent_lenses(_request(tmp_path), worker, max_workers=1)

    outcome = aggregate.outcomes[0]
    assert outcome.state == "incomplete"
    assert outcome.error
    assert outcome.findings[0]["content"] == "Caller contract is broken"
    assert aggregate.findings == outcome.findings
    assert aggregate.complete is False
    assert aggregate.finding_verdict is None
    rejection = outcome.warnings[-1]
    assert rejection["code"] == "required-lens-failed"
    assert rejection["details"]["received_status"] == "success"
    assert rejection["details"]["summary_rejection"]
    assert outcome.worker_evidence["rejected"][-1]["field"] == "summary"


def test_non_english_evidence_summary_is_not_rejected_for_language(tmp_path):
    aggregate = run_independent_lenses(
        _request(tmp_path),
        lambda request: _response(summary="已检查 app.py 及调用方，未发现该变更引入的缺陷"),
        max_workers=1,
    )

    assert aggregate.complete is True
    assert aggregate.finding_verdict == "pass"
    assert all("app.py" in outcome.summary for outcome in aggregate.outcomes)


def test_worker_evidence_is_bounded_anchored_and_sanitized(tmp_path):
    secret = "glpat-never-record-this-token"
    sources = [
        "/Users/alice/private/app.py", "../outside.py", "~/.ssh/id_rsa",
        "C:\\private\\app.py", {"path": "src/app.py", "ignored": "source dump"},
    ]
    sources.extend({"path": f"src/file-{index}.py"} for index in range(30))
    response = _response(
        summary=f"Inspected src/app.py; host=/Users/alice/repo token={secret}",
        sources_read=sources,
        evidence_references=[{
            "path": "src/app.py", "location": "lines 10-12",
            "rationale": (f"Caller return contract; credential={secret}; "
                          "client_secret=plain-evidence-secret; /Users/alice/repo/app.py"),
            "source_excerpt": "do not persist this arbitrary code excerpt",
        }, {
            "path": "/home/alice/other.py", "location": "line 1",
        }],
    )

    outcome = rg_final_lenses.normalize_worker_result(response, REQUIRED_LENSES[0])
    evidence = outcome.worker_evidence
    serialized = json.dumps(evidence)

    assert outcome.state == "completed_with_warnings"
    assert 0 < len(evidence["sources_read"]) <= rg_final_lenses.MAX_WORKER_EVIDENCE_ITEMS
    assert 0 < len(evidence["rejected"]) <= rg_final_lenses.MAX_WORKER_EVIDENCE_REJECTIONS
    assert evidence["sources_read"][0] == {"path": "src/app.py"}
    assert evidence["evidence_references"][0]["path"] == "src/app.py"
    assert evidence["evidence_references"][0]["location"] == "lines 10-12"
    assert evidence["disclosure"] == "sources-declared"
    assert evidence["tool_calls_verified"] is False
    assert "not-independently-verified" in evidence["verification"]
    for forbidden in (secret, "plain-evidence-secret", "/Users/alice", "/home/alice",
                      ".ssh", "source_excerpt", "source dump", "C:\\private"):
        assert forbidden not in serialized
    assert "[redacted]" in serialized
    assert "[host-path]" in serialized
    assert outcome.warnings[0]["code"] == "worker-evidence-rejected"
    assert all(len(entry["path"]) <= rg_final_lenses.MAX_WORKER_EVIDENCE_PATH
               for entry in evidence["sources_read"])


def test_references_without_read_disclosure_stay_distinct_from_no_disclosure(tmp_path):
    references = [{"path": "app.py", "location": "line 2", "rationale": "Changed return"}]
    referenced = rg_final_lenses.normalize_worker_result(
        _response(evidence_references=references), REQUIRED_LENSES[0])
    empty = rg_final_lenses.normalize_worker_result(_response(), REQUIRED_LENSES[0])

    assert referenced.worker_evidence["disclosure"] == "references-without-sources-read"
    assert empty.worker_evidence["disclosure"] == "no-sources-declared"
    assert referenced.worker_evidence["tool_calls_verified"] is False


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


def test_explicit_finding_closures_are_detached_for_trusted_finalization(tmp_path):
    resolution = {
        "finding_id": "prior-id", "content_anchor": "sha256:prior",
        "anchors": [{"anchor_type": "content", "anchor": "sha256:prior",
                     "evidence_fingerprint": "new-tree"}],
        "resolution_evidence": {"diff": "the offending assignment was removed"},
        "revalidation": {"status": "passed", "test": "test_caller_contract"},
    }
    falsification = {
        "finding_id": "other-prior", "content_anchor": "sha256:other",
        "rationale": "The existing transaction wrapper already persists the value",
        "evidence": {"inspection": "app.py:40-49"},
    }
    original = deepcopy(resolution)

    def worker(request):
        if request["lens"] == "correctness":
            return _response(
                finding_resolutions=[resolution], finding_falsifications=[falsification])
        return _response()

    aggregate = run_independent_lenses(_request(tmp_path), worker)

    assert aggregate.complete is True
    assert [entry["closure_type"] for entry in aggregate.finding_closures] == [
        "resolved", "falsified",
    ]
    assert all(entry["lens"] == "correctness" for entry in aggregate.finding_closures)
    assert aggregate.finding_closures[0]["revalidation"]["status"] == "passed"
    assert resolution == original


def test_malformed_closure_claim_is_an_incomplete_lens_not_a_memory_write(tmp_path):
    def worker(request):
        if request["lens"] == "correctness":
            return _response(finding_resolutions={"finding_id": "not-a-list"})
        return _response()

    aggregate = run_independent_lenses(_request(tmp_path), worker)

    assert aggregate.complete is False
    assert aggregate.finding_closures == ()
    assert "finding_resolutions must be an array" in aggregate.outcomes[0].error


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
        list_open=lambda pinned, repo_root=None: [item["url"]],
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
                "documents": [
                    {"path": "README.md", "kind": "instructions", "unchanged": True},
                    {"path": "docs/ADR.md", "kind": "adr", "unchanged": True},
                ],
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


def test_standalone_persists_bounded_worker_evidence_for_individual_and_combined(
        orchestrated):
    secret = "glpat-worker-evidence-must-not-escape"

    def worker(request):
        if request["lens"] == "correctness":
            return _response(
                summary=f"Inspected app.py and its return contract; token={secret}",
                sources_read=[
                    {"path": "app.py", "rationale": "Changed implementation and return caller"},
                    {"path": "/Users/alice/app.py", "rationale": "host path must be rejected"},
                ],
                evidence_references=[{
                    "path": "app.py", "location": "line 2",
                    "rationale": "Checked the changed return value against its caller",
                }],
            )
        return _response(summary="Inspected app.py and found no defect for this evidence duty")

    output, manifest = orchestrated.run(worker)

    assert output["state"] == "pass"
    assert output["exit_code"] == 0
    assert len(orchestrated.requests) == 14
    serialized = json.dumps(manifest)
    assert secret not in serialized
    assert "/Users/alice" not in serialized
    for unit in manifest["units"]:
        lenses = {lens["name"]: lens for lens in unit["lenses"]}
        assert len(lenses) == 7
        for lens in lenses.values():
            record = lens["evidence"]["worker_evidence"]
            method = lens["evidence"]["method"]
            assert record["received_status"] == "success"
            assert record["verification"] == "worker-declared-not-independently-verified"
            assert record["tool_calls_verified"] is False
            assert method["unit_id"] == unit["unit_id"]
            assert method["changed_paths"] == ["app.py"]
            assert [document["path"] for document in method["documents"]] == [
                "README.md", "docs/ADR.md",
            ]
            assert "review_duty" in method["instruction_keys"]
        disclosed = lenses["correctness"]["evidence"]["worker_evidence"]
        assert disclosed["sources_read"] == [{
            "path": "app.py", "rationale": "Changed implementation and return caller",
        }]
        assert disclosed["evidence_references"][0]["location"] == "line 2"
        assert disclosed["rejected"][0]["reason"] == "path-not-repo-relative"
        assert disclosed["disclosure"] == "sources-declared"
        assert all(lenses[name]["evidence"]["worker_evidence"]["disclosure"] ==
                   "no-sources-declared" for name in LENS_NAMES if name != "correctness")


def test_standalone_placeholder_summary_preserves_finding_and_return_diagnostics(
        orchestrated, monkeypatch):
    def worker(request):
        if request["lens"] == "correctness":
            response = _response(
                summary="...",
                findings=[{
                    "path": "app.py", "start_line": 2, "end_line": 2,
                    "severity": "high", "confidence": 0.99,
                    "content": "The changed return violates its caller",
                }],
            )
            response["_worker_diagnostics"] = {
                "reason": "response-received", "return_code": 0,
                "stdout_tail": "bounded scripted response",
            }
            return response
        return _response(summary="Inspected app.py; no defect found for this lens")

    output, manifest = orchestrated.run(worker)

    assert output["state"] == "incomplete"
    assert output["finding_verdict"] is None
    individual = manifest["units"][0]
    correctness = next(lens for lens in individual["lenses"]
                       if lens["name"] == "correctness")
    record = correctness["evidence"]["worker_evidence"]
    assert correctness["state"] == "incomplete"
    assert individual["findings"][0]["content"] == "The changed return violates its caller"
    assert record["received_status"] == "success"
    assert record["received_summary"] == "..."
    assert record["diagnostics"]["return_code"] == 0
    assert record["diagnostics"]["stdout_tail"] == "bounded scripted response"
    assert record["rejected"][-1]["reason"] == "placeholder-only"
    assert "worker-result-rejected" not in record["disclosure"]


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


def test_rejected_worker_status_is_retained_in_bounded_lens_evidence(orchestrated):
    def worker(request):
        if request["lens"] == "security":
            raise rg_final_review.WorkerError(
                "security worker returned unsupported completion status 'finished'",
                details={
                    "reason": "invalid-status", "received_status": "finished",
                    "return_code": 0, "stdout_tail": "bounded unsupported-status response",
                },
            )
        return _response(summary="Inspected app.py and completed this lens without findings")

    output, manifest = orchestrated.run(worker)

    assert output["state"] == "incomplete"
    for unit in manifest["units"]:
        security = next(lens for lens in unit["lenses"] if lens["name"] == "security")
        record = security["evidence"]["worker_evidence"]
        assert record["received_status"] == "finished"
        assert record["disclosure"] == "worker-result-rejected"
        assert record["diagnostics"] == {
            "reason": "invalid-status", "return_code": 0,
            "stdout_tail": "bounded unsupported-status response",
        }
        assert record["sources_read"] == []
        assert record["evidence_references"] == []
        assert record["tool_calls_verified"] is False


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
