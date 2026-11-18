"""Vertical SPEC-16 normal-completion and structured-stop execution tests."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rg_final_execution  # noqa: E402
import rg_final_review  # noqa: E402
from rg_final_execution import execute_unit  # noqa: E402
from rg_final_lenses import REQUIRED_LENS_NAMES  # noqa: E402
from rg_final_store import FinalStore  # noqa: E402


def _worker_result(findings=(), warnings=()):
    return {
        "schema": "final-review-worker-result/v1",
        "status": "success",
        "summary": "lens complete",
        "findings": list(findings),
        "warnings": list(warnings),
        "unreviewed": [],
    }


def _required_result(status="absent"):
    return {
        "status": status,
        "blocking": status == "block",
        "commit": "a" * 40,
        "suites": [],
        "findings": [],
        "warnings": [],
    }


def _dynamic_result(unit_key="individual:1"):
    return {
        "schema": "final-review-dynamic-result/v1",
        "unit_key": unit_key,
        "state": "not_applicable",
        "complete": True,
        "coverage": {"status": "not_applicable", "summary": "No changed paths"},
        "findings": [],
        "warnings": [],
        "tests": [],
        "telemetry": {},
        "revisions": {},
    }


def _item():
    return {
        "provider": "gitlab",
        "id": "gitlab:1!7",
        "iid": 7,
        "url": "https://gitlab.example.test/group/project/-/merge_requests/7",
        "title": "Pinned change",
        "source": {
            "project_id": 1, "project_path": "group/project", "branch": "feature",
            "sha": "a" * 40, "url": "https://example.test/source.git",
        },
        "target": {
            "project_id": 1, "project_path": "group/project", "branch": "main",
            "sha": "b" * 40, "base_sha": "c" * 40,
            "url": "https://example.test/target.git",
        },
    }


def _provider(item):
    return SimpleNamespace(
        read=lambda reference, repo_root=None: deepcopy(item),
        revalidate=lambda pinned, repo_root=None: deepcopy(pinned),
    )


def _blocking_finding(confidence=0.99):
    return {
        "id": "critical-contract",
        "path": "app.py",
        "start_line": 2,
        "end_line": 2,
        "severity": "critical",
        "confidence": confidence,
        "content": "The pinned implementation breaks its caller.",
        "evidence": "The caller requires the old return contract.",
    }


def _stop_result(confidence=0.99, *, code="unsafe-sandbox", category="unsafe"):
    return {
        "schema": "final-review-worker-result/v1",
        "status": "STOP",
        "stop": {
            "reason_code": code,
            "reason": "The credential-free sandbox boundary was demonstrably lost.",
            "category": category,
            "evidence": {"observed": "provider-write-token", "credential_free": False},
            "finding": _blocking_finding(confidence),
        },
    }


def _materialization(monkeypatch, repository, item, cleaned):
    class Materialization:
        def __enter__(self):
            return SimpleNamespace(
                path=repository, head_sha=item["source"]["sha"],
                target_sha=item["target"]["sha"], merge_base_sha=item["target"]["base_sha"],
                diff="diff --git a/app.py b/app.py\n", submodules=(), lfs_files=(), lfs_complete=True,
            )

        def __exit__(self, exc_type, exc, traceback):
            cleaned.append(True)

    monkeypatch.setattr(rg_final_review, "materialize_review", lambda *args, **kwargs: Materialization())


def test_execute_final_review_completes_safe_lenses_and_tests_after_blocking_finding(
        tmp_path, monkeypatch):
    repository = tmp_path / "immutable"
    repository.mkdir()
    cleaned = []
    requests = []
    calls = []
    item = _item()

    _materialization(monkeypatch, repository, item, cleaned)

    def worker(request, **kwargs):
        requests.append(request["lens"])
        return _worker_result([_blocking_finding()] if request["lens"] == "correctness" else [])

    def required(repository_arg, commit):
        calls.append(("required", Path(repository_arg), commit))
        return _required_result()

    def dynamic(repository_arg, target_sha, head_sha, **kwargs):
        calls.append(("dynamic", Path(repository_arg), target_sha, head_sha))
        return _dynamic_result()

    monkeypatch.setattr(rg_final_review, "invoke_worker", worker)

    output = rg_final_review.execute_final_review(
        item["url"], provider=_provider(item), store_path=tmp_path / "final.sqlite3",
        required_runner=required, dynamic_runner=dynamic, environ={},
    )

    assert requests == list(REQUIRED_LENS_NAMES)
    assert [call[0] for call in calls] == ["required", "dynamic"]
    assert output["execution_complete"] is True
    assert output["finding_verdict"] == "block"
    assert output["state"] == "incomplete"  # C13/C14 still own composition/publication.
    assert output["stops"] == []
    assert output["trust"]["official"] is False
    assert cleaned == [True]
    human_report = Path(output["human_report_path"]).read_text(encoding="utf-8")
    assert "Stage: S16-04B" in human_report
    assert "Tests, composition and provider publication are not implemented" not in human_report
    with FinalStore(output["store_path"]) as store:
        report = store.machine_report(output["run_id"])
        assert len(report["units"][0]["findings"]) == 1
        assert report["publications"] == []
        coverage = {row["kind"]: row["status"] for row in report["units"][0]["coverage"]}
        assert coverage["required_tests"] == "absent"
        assert coverage["dynamic_tests"] == "not_applicable"


def test_valid_unsafe_sandbox_stop_records_original_finding_and_every_skip(
        tmp_path, monkeypatch):
    repository = tmp_path / "immutable"
    repository.mkdir()
    item = _item()
    cleaned = []
    requests = []
    _materialization(monkeypatch, repository, item, cleaned)

    def worker(request, **kwargs):
        requests.append(request["lens"])
        if request["lens"] == "correctness":
            return _stop_result()
        return _worker_result()

    def forbidden(*args, **kwargs):
        raise AssertionError("dependent tests must not execute after an unsafe sandbox STOP")

    monkeypatch.setattr(rg_final_review, "invoke_worker", worker)
    output = rg_final_review.execute_final_review(
        item["url"], provider=_provider(item), store_path=tmp_path / "final.sqlite3",
        required_runner=forbidden, dynamic_runner=forbidden, environ={},
    )

    assert requests == ["correctness"]
    assert output["state"] == "incomplete"
    assert output["execution_complete"] is False
    assert output["finding_verdict"] is None
    assert output["stops"][0]["reason_code"] == "unsafe-sandbox"
    assert output["stops"][0]["evidence"]["credential_free"] is False
    assert output["trust"]["official"] is False
    assert cleaned == [True]
    with FinalStore(output["store_path"]) as store:
        report = store.machine_report(output["run_id"])
        unit = report["units"][0]
        assert unit["state"] == "incomplete"
        assert unit["findings"][0]["content"] == _blocking_finding()["content"]
        assert unit["findings"][0]["lens"] == "lens:correctness"
        assert len(unit["lenses"]) == len(REQUIRED_LENS_NAMES)
        assert unit["lenses"][0]["state"] == "stopped"
        assert all(lens["state"] == "skipped" for lens in unit["lenses"][1:])
        coverage = {row["kind"]: row["status"] for row in unit["coverage"]}
        assert coverage["required_tests"] == "skipped"
        assert coverage["dynamic_tests"] == "skipped"
        assert unit["stops"][0]["evidence"]["reason_code"] == "unsafe-sandbox"
        skipped_tests = {row["name"] for row in unit["skipped_work"]
                         if row["name"] in ("required_tests", "dynamic_tests")}
        assert skipped_tests == {"required_tests", "dynamic_tests"}
        assert report["publications"] == []


def test_later_stop_joins_running_safe_peers_and_marks_pending_lenses(tmp_path):
    request = {
        "unit_key": "individual:1",
        "repository": {"path": str(tmp_path), "access": "full_tree",
                       "head_sha": "a" * 40, "target_sha": "b" * 40},
        "diff": "diff --git a/app.py b/app.py\n",
    }
    requests = []

    def worker(lens_request):
        lens = lens_request["lens"]
        requests.append(lens)
        if lens == "repository-standards":
            return _stop_result()
        if lens not in ("correctness", "compatibility/migrations", "change-intent"):
            time.sleep(0.15)
        return _worker_result()

    def forbidden(*args, **kwargs):
        raise AssertionError("dependent tests must not run after a validated STOP")

    execution = execute_unit(
        request, worker, required_runner=forbidden, dynamic_runner=forbidden,
        environ={},
    )

    assert requests == list(REQUIRED_LENS_NAMES[:5])
    assert [outcome.state for outcome in execution.aggregation.outcomes] == [
        "completed", "stopped", "completed", "completed", "completed",
        "skipped", "skipped",
    ]
    assert execution.aggregation.complete is False
    assert execution.checks_complete is False
    assert execution.stop["stage"] == "lens:repository-standards"
    assert execution.required is None and execution.dynamic is None
    assert {item["name"] for item in execution.skipped_work} >= {
        "compatibility/migrations", "change-intent", "required_tests", "dynamic_tests",
    }


def test_reverse_completion_stop_uses_declared_lens_order_and_joins_peer_evidence(
        tmp_path, monkeypatch):
    request = {
        "unit_key": "individual:1",
        "repository": {"path": str(tmp_path), "access": "full_tree",
                       "head_sha": "a" * 40, "target_sha": "b" * 40},
        "diff": "diff --git a/app.py b/app.py\n",
    }
    validated = {
        "lens:repository-standards": threading.Event(),
        "lens:documentation/domain": threading.Event(),
    }
    original_validate = rg_final_execution.validate_stop
    requests = []

    def observe_validation(result, *, stage="execution"):
        stop = original_validate(result, stage=stage)
        if stop is not None and stage in validated:
            validated[stage].set()
        return stop

    def stopped(lens, code):
        result = _stop_result(code=code)
        result["stop"]["reason"] = f"{lens} invalidated dependent test evidence"
        result["stop"]["scope"] = "correctness"  # Untrusted and deliberately too narrow.
        return result

    def peer_finding(lens):
        finding = _blocking_finding()
        finding.update({"id": f"{lens}-evidence", "content": f"{lens} peer finding"})
        return finding

    def worker(lens_request):
        lens = lens_request["lens"]
        requests.append(lens)
        if lens == "correctness":
            return _worker_result([peer_finding(lens)])
        if lens == "repository-standards":
            assert validated["lens:documentation/domain"].wait(5)
            return stopped(lens, "repository-stop")
        if lens == "documentation/domain":
            return stopped(lens, "documentation-stop")
        if lens == "testing":
            assert validated["lens:repository-standards"].wait(5)
            return _worker_result([peer_finding(lens)])
        if lens == "security":
            return _worker_result([peer_finding(lens)])
        return _worker_result()

    def forbidden(*args, **kwargs):
        raise AssertionError("both dependent tests must skip after concurrent STOPs")

    monkeypatch.setattr(rg_final_execution, "validate_stop", observe_validation)
    execution = execute_unit(
        request, worker, required_runner=forbidden, dynamic_runner=forbidden,
        environ={"RG_FINAL_LENS_WORKERS": "4"},
    )

    assert requests == list(REQUIRED_LENS_NAMES[:5])
    assert [outcome.name for outcome in execution.aggregation.outcomes] == \
        list(REQUIRED_LENS_NAMES)
    assert [outcome.state for outcome in execution.aggregation.outcomes] == [
        "completed", "stopped", "stopped", "completed", "completed",
        "skipped", "skipped",
    ]
    assert execution.stop["stage"] == "lens:repository-standards"
    assert execution.stop["reason_code"] == "repository-stop"
    outcomes = {outcome.name: outcome for outcome in execution.aggregation.outcomes}
    documentation_stop = outcomes["documentation/domain"].warnings[0]["details"]["stop"]
    assert documentation_stop["reason_code"] == "documentation-stop"
    assert [finding["id"] for finding in execution.aggregation.findings] == [
        "correctness-evidence", "testing-evidence", "security-evidence",
    ]
    for lens in REQUIRED_LENS_NAMES[5:]:
        skipped_stop = outcomes[lens].warnings[0]["details"]["stop"]
        assert skipped_stop["reason_code"] == "repository-stop"
    test_skips = [item for item in execution.skipped_work
                  if item["name"] in ("required_tests", "dynamic_tests")]
    assert len(test_skips) == 2
    assert all(item["details"]["reason_code"] == "repository-stop"
               for item in test_skips)
    assert execution.required is None and execution.dynamic is None


def test_stack_supervisor_scopes_stop_to_dependents_and_preserves_safe_individuals(
        tmp_path, monkeypatch):
    from test_final_composition import StackProvider, _repository

    repo, _root, items, current_sha = _repository(tmp_path)
    trace = tmp_path / "trace.json"
    worker_script = tmp_path / "worker.py"
    worker_script.write_text(
        "import json, os, pathlib, sys\n"
        "r=json.loads(pathlib.Path(sys.argv[-1]).read_text())\n"
        "with pathlib.Path(os.environ['TRACE']).open('a') as f: f.write(json.dumps({"
        "'unit':r['unit_key'],'lens':r['lens']})+'\\n')\n"
        "stop=r['unit_key']=='combined' and r['lens']=='correctness'\n"
        "data={'schema':'final-review-worker-result/v1',"
        "'status':'STOP' if stop else 'success','summary':'done','findings':[],"
        "'warnings':[],'stop':{'reason_code':'untrustworthy-tree','reason':'composed evidence changed',"
        "'category':'untrustworthy','scope':'individual:1','evidence':{'before':'a','after':'b'},"
        "'finding':{'path':'base.txt','start_line':2,'end_line':2,'severity':'critical',"
        "'confidence':0.99,'content':'composed evidence is untrustworthy'}}}\n"
        "if not stop: data.pop('stop')\n"
        "print(json.dumps(data))\n",
        encoding="utf-8")
    output = rg_final_review.execute_final_stack(
        [item["url"] for item in items], provider=StackProvider(items), repo_root=repo,
        store_path=tmp_path / "stack.sqlite3", worker=[sys.executable, str(worker_script)],
        environ={**dict(__import__('os').environ), "TRACE": str(trace)},
        current_root_sha=current_sha,
        required_runner=lambda *args: _required_result(),
        dynamic_runner=lambda *args, **kwargs: _dynamic_result(kwargs.get("unit_key")),
    )

    traces = [__import__('json').loads(line) for line in trace.read_text().splitlines()]
    # The combined correctness sentinel stops before another request is started;
    # every dependent mandatory lens/test remains explicitly skipped, not green.
    assert len(traces) == 2 * len(REQUIRED_LENS_NAMES) + 1
    assert any(item["unit"] == "combined" and item["lens"] == "correctness"
               for item in traces)
    assert [unit["unit_key"] for unit in output["units"]] == [
        "individual:1", "individual:2", "combined", "prospective"]
    assert output["units"][0]["execution_complete"] is True
    assert output["units"][1]["execution_complete"] is True
    assert output["units"][2]["state"] == "incomplete"
    assert output["units"][3]["execution_complete"] is False
    assert output["execution_complete"] is False
    assert output["state"] == "incomplete"
    assert output["stops"][0]["reason_code"] == "untrustworthy-tree"
    assert output["trust"]["official"] is False
    machine = __import__("json").loads(
        Path(output["machine_report_path"]).read_text(encoding="utf-8"))
    assert machine["orchestration"]["stage"] == "S16-04B"
    assert machine["orchestration"]["missing_stages"] == ["provider_publication"]
    assert "Stage: S16-04B" in Path(
        output["human_report_path"]).read_text(encoding="utf-8")
    prospective_machine = __import__("json").loads(
        Path(output["units"][3]["machine_report_path"]).read_text(encoding="utf-8"))
    assert prospective_machine["orchestration"]["stage"] == "S16-04B"
    assert prospective_machine["orchestration"]["missing_stages"] == [
        "provider_publication"]
    with FinalStore(output["store_path"]) as store:
        report = store.machine_report(output["run_id"])
        combined = report["units"][2]
        prospective = report["units"][3]
        assert set(combined["stops"][0]["affected_unit_ids"]) == {
            combined["unit_id"], prospective["unit_id"]}
        assert len(prospective["lenses"]) == len(REQUIRED_LENS_NAMES)
        assert all(lens["state"] == "skipped" for lens in prospective["lenses"])
        assert {row["name"] for row in prospective["skipped_work"]} >= {
            "required_tests", "dynamic_tests"}
        assert report["publications"] == []


@pytest.mark.parametrize("field,value", [
    ("reason", None),
    ("evidence", {}),
    ("category", "subjective-preference"),
    ("finding", None),
], ids=["reason", "evidence", "category", "finding"])
def test_invalid_structured_stop_never_suppresses_safe_mandatory_work(
        tmp_path, monkeypatch, field, value):
    repository = tmp_path / "immutable"
    repository.mkdir()
    item = _item()
    requests = []
    tests = []
    _materialization(monkeypatch, repository, item, [])
    invalid = _stop_result()
    invalid["stop"][field] = value

    def worker(request, **kwargs):
        requests.append(request["lens"])
        return deepcopy(invalid) if request["lens"] == "correctness" else _worker_result()

    monkeypatch.setattr(rg_final_review, "invoke_worker", worker)
    output = rg_final_review.execute_final_review(
        item["url"], provider=_provider(item), store_path=tmp_path / "final.sqlite3",
        required_runner=lambda *args: tests.append("required") or _required_result(),
        dynamic_runner=lambda *args, **kwargs: tests.append("dynamic") or _dynamic_result(),
        environ={},
    )

    assert requests == list(REQUIRED_LENS_NAMES)
    assert tests == ["required", "dynamic"]
    assert output["stops"] == []
    assert output["rejected_stops"][0]["stage"] == "lens:correctness"
    assert output["execution_complete"] is True


def test_low_confidence_subjective_stop_is_visible_but_never_skips_safe_work(
        tmp_path, monkeypatch):
    repository = tmp_path / "immutable"
    repository.mkdir()
    item = _item()
    cleaned = []
    requests = []
    tests = []
    _materialization(monkeypatch, repository, item, cleaned)

    def worker(request, **kwargs):
        requests.append(request["lens"])
        return _stop_result(0.2) if request["lens"] == "security" else _worker_result()

    monkeypatch.setattr(rg_final_review, "invoke_worker", worker)
    output = rg_final_review.execute_final_review(
        item["url"], provider=_provider(item), store_path=tmp_path / "final.sqlite3",
        required_runner=lambda *args: tests.append("required") or _required_result(),
        dynamic_runner=lambda *args, **kwargs: tests.append("dynamic") or _dynamic_result(),
        environ={},
    )

    assert requests == list(REQUIRED_LENS_NAMES)
    assert tests == ["required", "dynamic"]
    assert output["execution_complete"] is True
    assert output["stops"] == []
    assert output["rejected_stops"][0]["stage"] == "lens:security"
    assert "high-confidence" in output["rejected_stops"][0]["error"]
    # The subjective STOP is rejected, but the ordinary high-severity finding
    # still follows compute_verdict rather than being silently discarded.
    assert output["finding_verdict"] == "block"
    assert cleaned == [True]


def test_verification_failure_retains_independent_dynamic_evidence_and_cleans_up(
        tmp_path, monkeypatch):
    repository = tmp_path / "immutable"
    repository.mkdir()
    item = _item()
    cleaned = []
    dynamic_calls = []
    _materialization(monkeypatch, repository, item, cleaned)

    def failed_required(*args):
        raise RuntimeError("verification executor unavailable")

    def dynamic(*args, **kwargs):
        dynamic_calls.append(args)
        return _dynamic_result()

    monkeypatch.setattr(
        rg_final_review, "invoke_worker", lambda request, **kwargs: _worker_result())
    output = rg_final_review.execute_final_review(
        item["url"], provider=_provider(item), store_path=tmp_path / "final.sqlite3",
        required_runner=failed_required, dynamic_runner=dynamic, environ={},
    )

    assert len(dynamic_calls) == 1
    assert output["complete"] is False
    assert output["execution_complete"] is False
    assert output["stops"] == []
    assert output["required_verification"]["status"] == "incomplete"
    assert output["dynamic_checks"]["complete"] is True
    assert output["trust"]["official"] is False
    assert cleaned == [True]
    with FinalStore(output["store_path"]) as store:
        report = store.machine_report(output["run_id"])
        unit = report["units"][0]
        coverage = {row["kind"]: row["status"] for row in unit["coverage"]}
        assert coverage["required_tests"] == "incomplete"
        assert coverage["dynamic_tests"] == "not_applicable"
        assert report["publications"] == []


def test_fundamental_required_test_invalidation_skips_only_dependent_dynamic_work(
        tmp_path, monkeypatch):
    repository = tmp_path / "immutable"
    repository.mkdir()
    item = _item()
    cleaned = []
    requests = []
    dynamic_calls = []
    _materialization(monkeypatch, repository, item, cleaned)

    def worker(request, **kwargs):
        requests.append(request["lens"])
        return _worker_result()

    stop = _stop_result(code="fundamental-build-invalidation", category="invalid")
    stop["stop"]["reason"] = "The shared build definition cannot compile any dependent test."
    stop["stop"]["evidence"] = {"compiler_exit": 2, "diagnostic": "unknown build rule"}

    monkeypatch.setattr(rg_final_review, "invoke_worker", worker)
    output = rg_final_review.execute_final_review(
        item["url"], provider=_provider(item), store_path=tmp_path / "final.sqlite3",
        required_runner=lambda *args: stop,
        dynamic_runner=lambda *args, **kwargs: dynamic_calls.append(args),
        environ={},
    )

    assert requests == list(REQUIRED_LENS_NAMES)
    assert dynamic_calls == []
    assert output["state"] == "incomplete"
    assert output["execution_complete"] is False
    assert output["stops"][0]["stage"] == "required verification"
    assert output["stops"][0]["reason_code"] == "fundamental-build-invalidation"
    assert cleaned == [True]
    with FinalStore(output["store_path"]) as store:
        report = store.machine_report(output["run_id"])
        unit = report["units"][0]
        assert all(lens["state"] == "completed" for lens in unit["lenses"])
        assert unit["findings"][0]["lens"] == "required verification"
        coverage = {row["kind"]: row["status"] for row in unit["coverage"]}
        assert coverage["required_tests"] == "stopped"
        assert coverage["dynamic_tests"] == "skipped"
        assert report["publications"] == []
