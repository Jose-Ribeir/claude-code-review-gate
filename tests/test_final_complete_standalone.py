"""Complete SPEC-16 standalone orchestration through the public review seam.

Providers, workers, required verification, dynamic evidence, and status writes
are scripted. Materialization cleanup and the real SQLite memory/trust boundary
remain observable for every scenario.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rg_final_review  # noqa: E402
from rg_final_lenses import REQUIRED_LENS_NAMES  # noqa: E402
from rg_final_publish import STATUS_CONTEXT  # noqa: E402
from rg_final_store import FinalStore  # noqa: E402


SOURCE = "a" * 40
TARGET = "b" * 40
BASE = "c" * 40


def item():
    return {
        "provider": "gitlab",
        "id": "gitlab:111!17",
        "iid": 17,
        "url": "https://gitlab.example.test/group/project/-/merge_requests/17",
        "title": "Return the normalized value",
        "description": "Correct the return value.\n\nCloses #88",
        "state": "opened",
        "author": {"username": "contributor"},
        "linked_intent": [{"key": "#88", "source": "description", "role": "direct"}],
        "source": {
            "project_id": 222,
            "project_path": "fork/project",
            "branch": "feature",
            "sha": SOURCE,
            "url": "https://example.test/fork.git",
        },
        "target": {
            "project_id": 111,
            "project_path": "group/project",
            "branch": "main",
            "sha": TARGET,
            "base_sha": BASE,
            "url": "https://example.test/target.git",
        },
    }


def worker_result(findings=(), warnings=()):
    return {
        "schema": "final-review-worker-result/v1",
        "status": "success",
        "summary": "lens complete",
        "findings": list(findings),
        "warnings": list(warnings),
        "unreviewed": [],
    }


def required_result(status="pass"):
    return {
        "status": status,
        "blocking": status == "block",
        "commit": SOURCE,
        "suites": ([{
            "name": "unit", "command": "python -m unittest", "source": ".ocr/config.json",
            "state": "pass", "blocking": False, "output": "OK",
            "evidence_fingerprint": "required-unit", "attempts": [{"returncode": 0}],
        }] if status == "pass" else []),
        "findings": [],
        "warnings": [],
    }


def blocking_finding():
    return {
        "id": "return-contract", "path": "app.py", "start_line": 2, "end_line": 2,
        "severity": "high", "confidence": 0.97,
        "content": "The implementation breaks the caller's return contract.",
        "evidence": "The unchanged caller requires the previous normalized shape.",
    }


def stop_result():
    return {
        "schema": "final-review-worker-result/v1", "status": "STOP",
        "stop": {
            "reason_code": "unsafe-sandbox",
            "reason": "The credential-free execution boundary was demonstrably lost.",
            "category": "unsafe", "evidence": {"credential_free": False},
            "finding": blocking_finding(),
        },
    }


def dynamic_result(state="not_applicable", *, complete=True, findings=(), tests=()):
    return {
        "schema": "final-review-dynamic-result/v1",
        "unit_key": "individual:1",
        "state": state,
        "complete": complete,
        "findings": list(findings),
        "warnings": [],
        "tests": list(tests),
        "coverage": {
            "status": "not_applicable" if state == "not_applicable" else "existing_tests",
            "summary": "Dynamic evidence completed",
        },
        "telemetry": {"executor_attempts": 1},
        "revisions": {"target_sha": TARGET, "head_sha": SOURCE, "merge_base_sha": BASE},
    }


class Provider:
    def __init__(self, current=None):
        self.current = deepcopy(current or item())
        self.reads = []
        self.revalidations = []

    def read(self, reference, repo_root=None):
        self.reads.append((reference, repo_root))
        return deepcopy(self.current)

    def revalidate(self, pinned, repo_root=None):
        self.revalidations.append(deepcopy(pinned))
        return deepcopy(self.current)


class StatusWriter:
    def __init__(self, response=None):
        self.response = response or (json.dumps({
            "id": 9001, "name": STATUS_CONTEXT, "sha": SOURCE,
        }), None)
        self.calls = []

    def __call__(self, argv, cwd, timeout):
        self.calls.append(list(argv))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def assert_enforced_adverse(output, writer, publication_state):
    publication = output["publication"]
    assert publication["status"] == "published"
    assert publication["provider_state"] == "failed"
    assert publication["local_only"] is False
    assert publication["enforced"] is True
    assert publication["official"] is True
    assert publication["official_green"] is False
    assert publication["decision"]["publication_state"] == publication_state
    assert publication["decision"]["can_publish_status"] is True
    assert publication["decision"]["can_publish_pass"] is False
    assert output["trust"]["local_only"] is False
    assert output["trust"]["enforced"] is True
    assert output["trust"]["official"] is True
    if writer is not None:
        assert len(writer.calls) == 1
        assert "state=failed" in writer.calls[0]
        assert f"context={STATUS_CONTEXT}" in writer.calls[0]


@pytest.fixture
def standalone(tmp_path, monkeypatch):
    repository = tmp_path / "immutable"
    repository.mkdir()
    (repository / "app.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    cleaned = []
    requests = []
    stages = []
    context_calls = []

    class Materialization:
        def __enter__(self):
            return SimpleNamespace(
                path=repository, head_sha=SOURCE, target_sha=TARGET, merge_base_sha=BASE,
                diff="diff --git a/app.py b/app.py\n-return 1\n+return 2\n",
                submodules=(), lfs_files=(), lfs_complete=True,
                lfs_irrelevance={}, warnings=[],
            )

        def __exit__(self, exc_type, exc, traceback):
            cleaned.append(True)

    def context(review, **kwargs):
        context_calls.append(kwargs)
        return {
            "contract": "final-review-context/v1",
            "contract_version": 1,
            "review_mode": "complete-repository",
            "reviewer_instructions": "Read the complete pinned tree",
            "search_roots": [str(repository)],
            "target_sha": TARGET,
            "head_sha": SOURCE,
            "merge_base_sha": BASE,
            "diff": review.diff,
            "documents": [{"path": "AGENTS.md", "kind": "instructions", "unchanged": True}],
            "files": {"all": ["app.py"], "implementation": ["app.py"], "tests": []},
            "intent": {
                "description": {"text": item()["description"], "source": "provider"},
                "tickets": [{"key": "#88", "title": "Normalize", "body": "Return two"}],
                "references": item()["linked_intent"], "listed_only": [], "warnings": [],
            },
            "completeness": {
                "complete": True, "submodules": [], "lfs_files": [], "lfs_complete": True,
            },
            "warnings": [],
        }

    monkeypatch.setattr(rg_final_review, "materialize_review", lambda *args, **kwargs: Materialization())
    monkeypatch.setattr(rg_final_review, "collect_review_context", context, raising=False)

    def worker(request, **kwargs):
        requests.append(deepcopy(request))
        return worker_result()

    monkeypatch.setattr(rg_final_review, "invoke_worker", worker)

    def required(repository_arg, commit):
        stages.append("required")
        return required_result()

    def dynamic(repository_arg, target_sha, head_sha, **kwargs):
        stages.append("dynamic")
        return dynamic_result()

    return SimpleNamespace(
        tmp_path=tmp_path, monkeypatch=monkeypatch, repository=repository,
        cleaned=cleaned, requests=requests, stages=stages, context_calls=context_calls,
        required=required, dynamic=dynamic,
    )


def run(standalone, provider, store_path, **kwargs):
    return rg_final_review.run_final_review(
        item()["url"], provider=provider, store_path=store_path,
        required_runner=standalone.required, dynamic_runner=standalone.dynamic,
        environ={}, **kwargs,
    )


def test_clean_fully_covered_standalone_passes_and_publishes_on_trusted_rerun(standalone):
    store_path = standalone.tmp_path / "final.sqlite3"
    provider = Provider()
    forbidden = StatusWriter(RuntimeError("first independent run must not write"))

    first = run(standalone, provider, store_path)

    assert first["complete"] is True
    assert first["execution_complete"] is True
    assert first["state"] == "pass"
    assert first["finding_verdict"] == "pass"
    assert first["exit_code"] == 0
    assert first["trust"]["independent"] is True
    assert first["trust"]["official"] is False
    assert forbidden.calls == []
    assert standalone.stages == ["required", "dynamic", "required", "dynamic"]
    individual_requests = [request for request in standalone.requests
                           if request["unit_key"] == "individual:1"]
    combined_requests = [request for request in standalone.requests
                         if request["unit_key"] == "combined"]
    assert [request["lens"] for request in individual_requests] == list(REQUIRED_LENS_NAMES)
    assert [request["lens"] for request in combined_requests] == list(REQUIRED_LENS_NAMES)
    assert all(request["review_context"]["contract"] == "final-review-context/v1"
               for request in standalone.requests)
    assert all(request["finding_memory"]["required"] is False
               for request in standalone.requests)
    assert len(standalone.context_calls) == 1
    assert standalone.context_calls[0]["description"] == item()["description"]
    assert standalone.context_calls[0]["linked_intent"] == item()["linked_intent"]
    assert standalone.cleaned == [True]

    writer = StatusWriter()
    second = run(standalone, provider, store_path, publish=True, publication_runner=writer)

    assert second["complete"] is True
    assert second["state"] == "pass"
    assert second["exit_code"] == 0
    assert second["trust"]["independent"] is False
    assert second["publication"]["status"] == "published"
    assert second["publication"]["provider_state"] == "success"
    assert second["publication"]["official_green"] is True
    assert second["trust"]["local_only"] is False
    assert second["trust"]["enforced"] is True
    assert second["trust"]["official"] is True
    assert len(writer.calls) == 1
    assert "state=success" in writer.calls[0]
    assert f"context={STATUS_CONTEXT}" in writer.calls[0]
    assert standalone.cleaned == [True, True]

    with FinalStore(store_path) as store:
        report = store.machine_report(second["run_id"])
        coverage = {row["kind"]: row["status"] for row in report["units"][0]["coverage"]}
        assert coverage["review_context"] == "complete"
        assert coverage["intent"] == "complete"
        assert coverage["mandatory_lenses"] == "complete"
        assert coverage["required_tests"] == "pass"
        assert coverage["dynamic_tests"] == "not_applicable"
        assert report["freshness"]["state"] == "fresh"
        assert report["publications"][0]["status"] == "published"
        assert report["units"][0]["tests"]
        assert all(test["state"] == "pass" for test in report["units"][0]["tests"]
                   if test["kind"] == "required")
        assert not [test for test in report["units"][0]["tests"]
                    if test["kind"] in ("existing_test", "ephemeral_test")
                    and test["state"] == "pass"]


def test_ordinary_finding_completes_both_test_stages_and_publishes_adverse(standalone):
    store_path = standalone.tmp_path / "final.sqlite3"
    provider = Provider()
    run(standalone, provider, store_path)

    def worker(request, **kwargs):
        standalone.requests.append(deepcopy(request))
        finding = [blocking_finding()] if (
            request["unit_key"] == "individual:1" and request["lens"] == "correctness") else []
        return worker_result(finding)

    standalone.monkeypatch.setattr(rg_final_review, "invoke_worker", worker)
    writer = StatusWriter()
    output = run(standalone, provider, store_path, publish=True, publication_runner=writer)

    assert output["complete"] is True
    assert output["execution_complete"] is True
    assert output["state"] == "findings"
    assert output["finding_verdict"] == "block"
    assert output["exit_code"] == 1
    assert standalone.stages[-4:] == ["required", "dynamic", "required", "dynamic"]
    assert all(unit["execution_complete"] for unit in output["units"])
    assert output["units"][0]["state"] == "findings"
    assert output["units"][1]["state"] == "pass"
    assert_enforced_adverse(output, writer, "findings")
    assert standalone.cleaned == [True, True]
    with FinalStore(store_path) as store:
        report = store.machine_report(output["run_id"])
        assert len(report["units"][0]["findings"]) == 1
        assert report["units"][1]["findings"] == []
        assert [row["status"] for row in report["publications"]] == ["published"]


def test_newer_standalone_findings_replace_an_older_green_on_same_head(standalone):
    store_path = standalone.tmp_path / "replacement.sqlite3"
    provider = Provider()
    run(standalone, provider, store_path)
    green_writer = StatusWriter()
    green = run(
        standalone, provider, store_path,
        publish=True, publication_runner=green_writer,
    )
    assert green["publication"]["provider_state"] == "success"

    def finding_worker(request, **kwargs):
        finding = [blocking_finding()] if (
            request["unit_key"] == "individual:1" and request["lens"] == "correctness"
        ) else []
        return worker_result(finding)

    standalone.monkeypatch.setattr(rg_final_review, "invoke_worker", finding_worker)
    adverse_writer = StatusWriter()
    adverse = run(
        standalone, provider, store_path,
        publish=True, publication_runner=adverse_writer,
    )

    assert adverse["run_seq"] == green["run_seq"] + 1
    assert adverse["state"] == "findings"
    assert_enforced_adverse(adverse, adverse_writer, "findings")
    assert "state=success" in green_writer.calls[0]
    for writer in (green_writer, adverse_writer):
        assert any(f"statuses/{SOURCE}" in argument for argument in writer.calls[0])
        assert f"context={STATUS_CONTEXT}" in writer.calls[0]


def establish_continuity(standalone, provider, store_path):
    first = run(standalone, provider, store_path)
    assert first["state"] == "pass"
    return first


@pytest.mark.parametrize("missing", ["intent", "context", "lens", "required", "dynamic"])
def test_missing_intent_context_or_mandatory_stage_is_not_complete(
        standalone, missing):
    store_path = standalone.tmp_path / f"{missing.replace('/', '-')}.sqlite3"
    provider = Provider()
    establish_continuity(standalone, provider, store_path)
    original_context = rg_final_review.collect_review_context
    original_required = standalone.required
    original_dynamic = standalone.dynamic

    if missing in ("intent", "context"):
        def incomplete_context(review, **kwargs):
            acquired = deepcopy(original_context(review, **kwargs))
            if missing == "intent":
                acquired["intent"].update({
                    "description": None, "tickets": [],
                    "warnings": ["#88: scripted tracker unavailable"],
                })
            else:
                acquired["completeness"]["complete"] = False
            return acquired
        standalone.monkeypatch.setattr(rg_final_review, "collect_review_context", incomplete_context)
    elif missing == "lens":
        original_worker = rg_final_review.invoke_worker

        def incomplete_worker(request, **kwargs):
            if request["unit_key"] == "individual:1" and request["lens"] == "security":
                raise RuntimeError("scripted mandatory lens failure")
            return original_worker(request, **kwargs)
        standalone.monkeypatch.setattr(rg_final_review, "invoke_worker", incomplete_worker)
    elif missing == "required":
        def incomplete_required(repository, commit):
            standalone.stages.append("required")
            return required_result("incomplete")
        standalone.required = incomplete_required
    else:
        def incomplete_dynamic(repository, target_sha, head_sha, **kwargs):
            standalone.stages.append("dynamic")
            result = dynamic_result("degraded", complete=False)
            result["coverage"]["status"] = "unknown"
            return result
        standalone.dynamic = incomplete_dynamic

    writer = StatusWriter()
    output = run(standalone, provider, store_path, publish=True, publication_runner=writer)

    assert output["state"] == "incomplete"
    assert output["complete"] is False
    assert output["exit_code"] == 2
    assert output["freshness"]["state"] == "fresh"
    assert_enforced_adverse(output, writer, "incomplete")
    assert standalone.cleaned == [True, True]
    if missing in ("required", "dynamic"):
        assert "dynamic" in standalone.stages[-4:]
        assert output["dynamic_checks"] is not None
    if missing == "lens":
        assert output["static_lenses_complete"] is False
        assert output["finding_verdict"] is None
    standalone.required = original_required
    standalone.dynamic = original_dynamic


def test_memory_is_carried_explicitly_and_a_quiet_rerun_is_not_clean(standalone):
    store_path = standalone.tmp_path / "memory.sqlite3"
    provider = Provider()

    def finding_worker(request, **kwargs):
        standalone.requests.append(deepcopy(request))
        finding = [blocking_finding()] if (
            request["unit_key"] == "individual:1" and request["lens"] == "correctness") else []
        return worker_result(finding)

    standalone.monkeypatch.setattr(rg_final_review, "invoke_worker", finding_worker)
    first = run(standalone, provider, store_path)
    assert first["state"] == "findings"
    second = run(standalone, provider, store_path, publish=True,
                 publication_runner=StatusWriter())

    second_individual = next(request for request in standalone.requests
                             if request["run_id"] == second["run_id"]
                             and request["unit_key"] == "individual:1")
    assert second_individual["finding_memory"]["required"] is True
    pending = second_individual["finding_memory"]["findings"][0]
    assert second["state"] == "findings"
    assert second["complete"] is True
    assert second["finding_memory"]["pending_thread_ids"] == []
    assert second["finding_memory"]["unresolved_thread_ids"] == [pending["thread_id"]]
    assert_enforced_adverse(second, None, "findings")
    with FinalStore(store_path) as store:
        history = store.finding_history(pending["thread_id"])
        assert [event["event_type"] for event in history["events"]] == [
            "observed", "observed", "carried"]
        assert store.official_findings(second["run_id"])[0]["carried_into_run"] is True

    def clean_worker(request, **kwargs):
        standalone.requests.append(deepcopy(request))
        return worker_result()

    standalone.monkeypatch.setattr(rg_final_review, "invoke_worker", clean_worker)
    third = run(standalone, provider, store_path, publish=True,
                publication_runner=StatusWriter())

    assert third["finding_verdict"] == "block"
    assert third["state"] == "incomplete"
    assert third["complete"] is False
    assert third["finding_memory"]["quiet_rerun_cleared"] is False
    assert third["finding_memory"]["pending_thread_ids"] == [pending["thread_id"]]
    assert_enforced_adverse(third, None, "findings")
    assert standalone.cleaned == [True, True, True]


def test_stale_revision_publishes_adverse_only_to_original_pinned_head(standalone):
    class MovingProvider(Provider):
        def read(self, reference, repo_root=None):
            current = super().read(reference, repo_root)
            if len(self.reads) == 4:
                current["source"] = deepcopy(current["source"])
                current["source"]["sha"] = "d" * 40
            return current

    store_path = standalone.tmp_path / "stale.sqlite3"
    provider = MovingProvider()
    establish_continuity(standalone, provider, store_path)
    writer = StatusWriter()

    output = run(standalone, provider, store_path, publish=True, publication_runner=writer)

    assert output["state"] == "stale"
    assert output["complete"] is False
    assert output["exit_code"] == 3
    assert output["freshness"]["restart_required"] is True
    assert output["freshness"]["automatic_restart"] is False
    assert_enforced_adverse(output, writer, "stale")
    assert any(f"statuses/{SOURCE}" in argument for argument in writer.calls[0])
    assert not any("d" * 40 in argument for argument in writer.calls[0])
    assert standalone.cleaned == [True, True]
    with FinalStore(store_path) as store:
        assert all(unit["state"] == "stale" for unit in store.machine_report(
            output["run_id"])["units"])


def test_valid_stop_skips_only_dependent_combined_work_and_publishes_incomplete(standalone):
    store_path = standalone.tmp_path / "stop.sqlite3"
    provider = Provider()
    establish_continuity(standalone, provider, store_path)

    def stopping_worker(request, **kwargs):
        standalone.requests.append(deepcopy(request))
        if request["unit_key"] == "individual:1" and request["lens"] == "correctness":
            return stop_result()
        return worker_result()

    standalone.monkeypatch.setattr(rg_final_review, "invoke_worker", stopping_worker)
    stages_before = list(standalone.stages)
    writer = StatusWriter()
    output = run(standalone, provider, store_path, publish=True, publication_runner=writer)

    assert output["state"] == "incomplete"
    assert output["complete"] is False
    assert output["execution_complete"] is False
    assert output["finding_verdict"] is None
    assert standalone.stages == stages_before
    assert [request["lens"] for request in standalone.requests
            if request["run_id"] == output["run_id"]] == ["correctness"]
    assert output["units"][1]["skipped_work"]
    assert_enforced_adverse(output, writer, "incomplete")
    assert standalone.cleaned == [True, True]
    with FinalStore(store_path) as store:
        report = store.machine_report(output["run_id"])
        assert len(report["units"][0]["findings"]) == 1
        assert {row["name"] for row in report["units"][1]["skipped_work"]} >= {
            "required_tests", "dynamic_tests"}
        assert [row["status"] for row in report["publications"]] == ["published"]


def test_non_actionable_finding_uses_existing_warn_authority_not_findings_state(standalone):
    store_path = standalone.tmp_path / "warning.sqlite3"
    provider = Provider()
    establish_continuity(standalone, provider, store_path)

    def worker(request, **kwargs):
        if request["unit_key"] == "individual:1" and request["lens"] == "correctness":
            finding = blocking_finding()
            del finding["start_line"], finding["end_line"]
            return worker_result([finding])
        return worker_result()

    standalone.monkeypatch.setattr(rg_final_review, "invoke_worker", worker)
    writer = StatusWriter()
    output = run(standalone, provider, store_path, publish=True, publication_runner=writer)

    assert output["finding_verdict"] == "warn"
    assert output["state"] == "incomplete"
    assert output["state"] != "findings"
    assert output["complete"] is False
    assert output["exit_code"] == 2
    assert_enforced_adverse(output, writer, "findings")
    assert standalone.cleaned == [True, True]


def test_fresh_store_pass_is_local_and_untrusted_even_when_publication_is_requested(standalone):
    writer = StatusWriter()
    output = run(standalone, Provider(), standalone.tmp_path / "fresh.sqlite3",
                 publish=True, publication_runner=writer)

    assert output["state"] == "pass"
    assert output["complete"] is True
    assert output["trust"]["independent"] is True
    assert output["trust"]["local_only"] is True
    assert output["trust"]["enforced"] is False
    assert output["trust"]["official"] is False
    assert output["publication"]["status"] == "local_only"
    assert output["publication"]["official_green"] is False
    assert "independent-run" in output["publication"]["decision"]["green_blockers"]
    assert writer.calls == []


def test_multiref_stack_default_execution_contract_is_unchanged(standalone, monkeypatch):
    captured = {}

    def stack_runner(references, **kwargs):
        captured["references"] = references
        captured.update(kwargs)
        return {"state": "incomplete", "stage": "S16-06B"}

    monkeypatch.setattr(rg_final_review, "run_final_stack", stack_runner)
    output = rg_final_review.run_final_review(["one", "two"], environ={})

    assert output["stage"] == "S16-06B"
    assert captured["references"] == ["one", "two"]
    assert captured["execute_checks"] is False
    assert captured["required_runner"] is None
    assert captured["dynamic_runner"] is None


def test_publication_permission_loss_preserves_complete_local_unenforced_result(standalone):
    store_path = standalone.tmp_path / "permission.sqlite3"
    provider = Provider()
    establish_continuity(standalone, provider, store_path)
    writer = StatusWriter((None, "403 Forbidden: status permission denied"))

    output = run(standalone, provider, store_path, publish=True, publication_runner=writer)

    assert output["state"] == "pass"
    assert output["complete"] is True
    assert output["exit_code"] == 0
    assert len(writer.calls) == 1
    assert output["publication"]["status"] == "local_only"
    assert output["publication"]["local_only"] is True
    assert output["publication"]["enforced"] is False
    assert output["publication"]["official_green"] is False
    assert "403 Forbidden" in output["publication"]["error"]
    assert output["trust"]["local_only"] is True
    assert output["trust"]["enforced"] is False
    assert output["trust"]["official"] is False
    assert standalone.cleaned == [True, True]
    with FinalStore(store_path) as store:
        report = store.machine_report(output["run_id"])
        assert report["run"]["state"] == "pass"
        assert report["run"]["completed_at"]
        assert report["publications"][0]["status"] == "local_only"
