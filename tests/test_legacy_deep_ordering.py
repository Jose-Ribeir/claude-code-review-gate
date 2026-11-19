"""Completion ordering and interruption behavior for bounded legacy deep work."""
import itertools
import json
import threading
import time

import pytest

from test_legacy_deep_decomposition import _CoverageHost, _ManualHost, _run_deep, review_gate


@pytest.mark.parametrize(
    "completion_order", list(itertools.permutations(range(3))),
)
def test_verdict_and_finding_order_are_deterministic_under_out_of_order_completion(
        tmp_path, monkeypatch, completion_order):
    paths = [f"pkg/file-{index}.py" for index in range(5)]
    host = _ManualHost(expected_tasks=3)
    outcome = {}

    def invoke():
        try:
            outcome["result"] = _run_deep(tmp_path, monkeypatch, host, paths)
        except BaseException as exc:  # propagate worker failures to the test thread
            outcome["error"] = exc

    worker = threading.Thread(target=invoke)
    worker.start()
    assert host.all_launched.wait(5), "native tasks were not dispatched"
    ordered_tasks = list(host.tasks.values())
    for index in completion_order:
        task = ordered_tasks[index]
        payload = host.payload(task)
        manifest = host.manifest(task)
        if manifest["paths"]:
            path = manifest["paths"][0]
            payload["findings"] = [{
                "path": path,
                "start_line": 1,
                "end_line": 1,
                "severity": "high",
                "confidence": 0.95,
                "content": "defect " + path,
            }]
        host.sessions[task.task_id].finish(payload)
    worker.join(5)

    assert not worker.is_alive()
    assert "error" not in outcome, outcome.get("error")
    result = outcome["result"]
    assert result["verdict"] == "block"
    assert [finding["content"] for finding in result["findings"]] == [
        "defect " + host.manifest(task)["paths"][0]
        for task in ordered_tasks if host.manifest(task)["paths"]
    ]


def test_interruption_retains_completed_artifacts_and_fences_late_completion(
        tmp_path, monkeypatch):
    paths = [f"pkg/file-{index}.py" for index in range(10)]
    host = _ManualHost(expected_tasks=4)
    outcome = {}

    def invoke():
        try:
            _run_deep(tmp_path, monkeypatch, host, paths, timeout=0.1)
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=invoke)
    worker.start()
    assert host.all_launched.wait(5)
    for task in list(host.tasks.values())[:2]:
        host.complete(task)
    worker.join(5)

    assert not worker.is_alive()
    error = outcome["error"]
    assert isinstance(error, review_gate.ReviewGateError)
    assert error.is_timeout
    journal_path = review_gate._async_dir(str(tmp_path)) / (
        "native-deep-partitioned-deep-tasks.json"
    )
    before = json.loads(journal_path.read_text(encoding="utf-8"))
    assert len(before["native_tasks"]) == 4
    completed = [task for task in before["native_tasks"] if task["artifacts"]]
    assert len(completed) == 2
    unfinished = [task for task in host.tasks.values() if host.sessions[
        task.task_id
    ].cancel_calls]
    assert len(unfinished) == 2
    for task in unfinished:
        host.sessions[task.task_id].finish(host.payload(task))
    after = json.loads(journal_path.read_text(encoding="utf-8"))
    differences = [
        (old["identity"]["task_id"], key)
        for old, new in zip(before["native_tasks"], after["native_tasks"])
        for key in old if old[key] != new[key]
    ]
    assert after == before, differences

    class ResumedHost(_CoverageHost):
        def __init__(self):
            super().__init__()
            self.launched = []

        def launch(self, task, callback):
            self.launched.append(task)
            return super().launch(task, callback)

    resumed_host = ResumedHost()
    resumed = _run_deep(tmp_path, monkeypatch, resumed_host, paths)

    assert resumed["verdict"] == "pass"
    assert len(resumed_host.launched) == 2
    assert all(task.runtime_deadline_at_unix > time.time() - 5
               for task in resumed_host.launched)


def test_supervisor_cancellation_owns_active_partitioned_sessions(tmp_path, monkeypatch):
    host = _ManualHost(expected_tasks=3)
    outcome = {}

    def invoke():
        try:
            _run_deep(tmp_path, monkeypatch, host, [
                f"pkg/file-{index}.py" for index in range(5)
            ], timeout=10)
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=invoke)
    worker.start()
    assert host.all_launched.wait(5)
    review_gate._kill_active_children()
    worker.join(2)

    assert not worker.is_alive()
    assert isinstance(outcome["error"], review_gate.ReviewGateError)
    assert all(session.cancel_calls == 1 for session in host.sessions.values())
    assert review_gate._active_native_handles() == []


@pytest.mark.parametrize("mutation", ["omitted", "repeated", "identity"])
def test_incomplete_repeated_or_misidentified_evidence_is_rejected(
        tmp_path, monkeypatch, mutation):
    class InvalidEvidenceHost(_CoverageHost):
        def payload(self, task):
            payload = super().payload(task)
            manifest = self.manifest(task)
            evidence = payload["native_deep_evidence"]
            if manifest["paths"]:
                if mutation == "omitted":
                    evidence["reviewed_paths"] = manifest["paths"][1:]
                elif mutation == "repeated":
                    evidence["reviewed_paths"] = manifest["paths"] + manifest["paths"][:1]
                else:
                    evidence["manifest_sha256"] = "0" * 64
            return payload

    with pytest.raises(review_gate.ReviewGateError, match="incomplete"):
        _run_deep(tmp_path, monkeypatch, InvalidEvidenceHost(), [
            f"pkg/file-{index}.py" for index in range(5)
        ])
