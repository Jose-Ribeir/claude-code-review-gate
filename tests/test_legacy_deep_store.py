"""Failure fencing for the durable legacy deep journal."""
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from rg_legacy_deep import DeepExecutionError, LegacyDeepStore  # noqa: E402


def identity():
    return {
        "run_id": "run-1", "task_id": "task-a", "session_name": "legacy-task-a",
        "unit_id": "legacy-deep:task-a", "stage": "legacy-deep", "lens": "deep-files",
        "revision_tuple": ["a" * 40, "a..b"], "input_sha256": "a" * 64,
        "backend": "kimchi", "model": "test-model", "deadline_at_unix": 200.0,
    }


def artifact(task_identity):
    return {
        "schema": "native-review-artifact/v1", "transport": "test-double/native-host-v1",
        "native_session_id": "fake-native-task-a", "identity": task_identity,
        "payload": {"status": "success", "verdict": "pass", "findings": []},
        "timing": {
            "started_at_unix": 100.0, "finished_at_unix": 100.25,
            "duration_seconds": 0.25,
        },
    }


def event(task_identity, event_id, event_type):
    return {
        "schema": "native-review-event/v1", "event_id": event_id,
        "event_type": event_type, "identity": task_identity,
        "transport": "test-double/native-host-v1", "native_session_id": "fake-native-task-a",
        "timing": {
            "occurred_at_unix": 101.0, "started_at_unix": None,
            "finished_at_unix": None, "duration_seconds": None,
        },
        "details": {},
    }


def test_artifact_io_failure_rolls_back_journal_success(tmp_path, monkeypatch):
    task_identity = identity()
    path = tmp_path / "journal.json"
    artifact_path = tmp_path / "artifact.json"
    store = LegacyDeepStore(path)
    store.register_native_task({
        "schema": "native-review-task/v1", "identity": task_identity,
        "transport": "test-double/native-host-v1",
    })
    original = __import__("rg_legacy_deep")._atomic_json

    def write(target, value):
        if Path(target) == artifact_path:
            raise OSError("injected legacy artifact I/O failure")
        return original(target, value)

    monkeypatch.setattr("rg_legacy_deep._atomic_json", write)
    with pytest.raises(OSError, match="injected legacy artifact I/O failure"):
        store.commit_native_artifact(
            task_identity, "message-a", artifact=artifact(task_identity),
            received_at_unix=101.0, artifact_path=artifact_path,
        )

    recorded = json.loads(path.read_text())["native_tasks"][0]
    assert recorded["artifacts"] == []
    assert not any(item["event_type"] == "completed" for item in recorded["events"])
    assert not artifact_path.exists()


def test_same_attempt_cancellation_fences_commit_but_new_attempt_can_resume(tmp_path):
    task_identity = identity()
    store = LegacyDeepStore(tmp_path / "journal.json")
    store.register_native_task({
        "schema": "native-review-task/v1", "identity": task_identity,
        "transport": "test-double/native-host-v1",
    })
    store.record_native_event(event(task_identity, "executor:task-a:started", "started"))
    store.record_native_event(event(task_identity, "scheduler:fence:task-a", "cancelled"))

    with pytest.raises(DeepExecutionError, match="fenced"):
        store.commit_native_artifact(
            task_identity, "message-a", artifact=artifact(task_identity),
            received_at_unix=101.0, artifact_path=tmp_path / "artifact.json",
        )

    store.record_native_event(event(
        task_identity, "executor:attempt-2:task-a:started", "started",
    ))
    accepted = store.commit_native_artifact(
        task_identity, "message-a", artifact=artifact(task_identity),
        received_at_unix=101.0, artifact_path=tmp_path / "artifact.json",
    )

    assert accepted["accepted"] is True
    assert (tmp_path / "artifact.json").exists()
