"""Ticket #12: durable native lifecycle events and supervisor artifacts."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rg_final_store as final_store  # noqa: E402
from rg_final_store import FinalStore, StoreError  # noqa: E402
from rg_native_executor import NativeExecutor, NativeTask  # noqa: E402
from rg_run_lifecycle import RunDeadline  # noqa: E402
from test_native_executor import _DeadlineTimer, _FakeHost  # noqa: E402


def review_stack(source_sha="a" * 40, target_sha="b" * 40):
    return {
        "provider": "gitlab",
        "target_project": "group/project",
        "root_target_ref": "main",
        "root_target_sha": target_sha,
        "items": [
            {
                "item_key": "123",
                "source_project": "fork/project",
                "source_ref": "feature",
                "source_sha": source_sha,
                "target_project": "group/project",
                "target_ref": "main",
                "target_sha": target_sha,
                "provider_data": {"iid": 123},
            }
        ],
    }


def test_native_migration_preserves_historical_incomplete_run_meaning(tmp_path):
    path = tmp_path / "native-migration.sqlite3"
    with FinalStore(path) as store:
        started = store.start_run(review_stack())
        unit_id = started["unit_ids"]["individual:1"]
        store.record_lens(unit_id, "correctness", "incomplete", "Former interruption")
        store.finish_run(started["run_id"], "incomplete")
        before = store.manifest(started["run_id"])

    with FinalStore(path) as reopened:
        after = reopened.manifest(started["run_id"])
        tables = {
            row[0] for row in reopened.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert reopened.schema_version == 4
        assert reopened.connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 4"
        ).fetchone() is not None

    assert final_store.SCHEMA_VERSION == 4
    assert {
        "native_tasks", "native_task_events", "native_artifacts",
        "native_artifact_messages",
    } <= tables
    assert after["native_tasks"] == []
    assert after["run"] == before["run"]
    assert after["run"]["state"] == "incomplete"
    assert after["run"]["completed_at"] == before["run"]["completed_at"]
    assert after["units"][0]["state"] == "incomplete"
    assert after["units"][0]["lenses"] == before["units"][0]["lenses"]
    assert after["contract"] == before["contract"]
    assert after["trust"] == before["trust"]


def native_identity(started, task_id="task-correctness", lens="correctness", **changes):
    identity = {
        "run_id": started["run_id"],
        "task_id": task_id,
        "session_name": "review-" + task_id,
        "unit_id": started["unit_ids"]["individual:1"],
        "stage": "individual",
        "lens": lens,
        "revision_tuple": ["b" * 40, "a" * 40, "b" * 40],
        "input_sha256": "a" * 64,
        "backend": "kimchi",
        "model": "kimi-k3.1-pro",
        "deadline_at_unix": 200.0,
    }
    identity.update(changes)
    return identity


def native_task(identity):
    return {
        "schema": "native-review-task/v1",
        "identity": identity,
        "transport": "test-double/native-host-v1",
    }


def native_event(identity, event_id, event_type, occurred, **timing):
    return {
        "schema": "native-review-event/v1",
        "event_id": event_id,
        "event_type": event_type,
        "identity": identity,
        "transport": "test-double/native-host-v1",
        "native_session_id": None if event_type == "queued" else "native-session-a",
        "timing": {
            "occurred_at_unix": occurred,
            "started_at_unix": timing.get("started"),
            "finished_at_unix": timing.get("finished"),
            "duration_seconds": timing.get("duration"),
        },
        "details": timing.get("details", {}),
    }


def test_registered_task_lifecycle_is_exactly_once_and_preserves_timing(tmp_path):
    with FinalStore(tmp_path / "lifecycle.sqlite3") as store:
        started = store.start_run(review_stack())
        identity = native_identity(started)
        task = native_task(identity)

        registered = store.register_native_task(task)
        assert store.register_native_task(task) == registered
        assert registered["schema"] == "native-review-task/v1"
        assert registered["identity"] == identity
        assert registered["events"] == []
        assert registered["artifacts"] == []
        assert registered["artifact_messages"] == []

        events = [
            native_event(identity, "event-queued", "queued", 10.0),
            native_event(identity, "event-started", "started", 11.0, started=11.0),
            native_event(
                identity, "event-warning", "warning", 12.0,
                details={"message": "bounded diagnostic"},
            ),
            native_event(
                identity, "event-completed", "completed", 13.25,
                started=11.0, finished=13.25, duration=2.25,
                details={"outcome": "success"},
            ),
            native_event(identity, "event-cleanup", "cleanup", 14.0),
            native_event(identity, "event-finalization", "finalization", 15.0),
        ]
        recorded = []
        for event in events:
            first = store.record_native_event(event)
            assert store.record_native_event(event) == first
            recorded.append(first)

        assert [item["event_type"] for item in recorded] == [
            "queued", "started", "warning", "completed", "cleanup", "finalization",
        ]
        assert recorded[0]["timing"] == {
            "occurred_at_unix": 10.0,
            "started_at_unix": None,
            "finished_at_unix": None,
            "duration_seconds": None,
        }
        assert recorded[3]["timing"]["duration_seconds"] == pytest.approx(2.25)
        view = store.manifest(started["run_id"])["native_tasks"][0]
        assert view["events"] == recorded

        conflicting_task = {
            **task, "identity": {**identity, "lens": "security"},
        }
        with pytest.raises(StoreError, match="immutable"):
            store.register_native_task(conflicting_task)
        with pytest.raises(StoreError, match="idempotent"):
            store.record_native_event({**events[0], "details": {"changed": True}})

        assert store.manifest(started["run_id"])["native_tasks"][0]["events"] == recorded

        readable = store.human_report(started["run_id"])
        for expected in (
            "## Native task lifecycle",
            "### task-correctness",
            "Transport: test-double/native-host-v1",
            "Session: review-task-correctness",
            "- queued @ 10.0",
            "- completed @ 13.25; duration=2.25s",
            "Artifact callbacks:\n- none recorded",
            "Committed artifacts:\n- none recorded",
        ):
            assert expected in readable


def native_artifact(identity, payload=None):
    return {
        "schema": "native-review-artifact/v1",
        "transport": "test-double/native-host-v1",
        "native_session_id": "native-session-a",
        "identity": identity,
        "payload": payload or {
            "schema": "final-review-worker-result/v1",
            "status": "success",
            "summary": "Inspected app.py completely.",
            "findings": [],
            "warnings": [],
            "unreviewed": [],
        },
        "timing": {
            "started_at_unix": 11.0,
            "finished_at_unix": 13.25,
            "duration_seconds": 2.25,
        },
    }


def test_supervisor_artifact_commit_is_atomic_versioned_and_idempotent(tmp_path):
    with FinalStore(tmp_path / "artifact.sqlite3") as store:
        started = store.start_run(review_stack())
        identity = native_identity(started)
        store.register_native_task(native_task(identity))
        store.record_native_event(native_event(identity, "queue", "queued", 10.0))
        store.record_native_event(
            native_event(identity, "start", "started", 11.0, started=11.0),
        )
        artifact = native_artifact(identity)

        committed = store.commit_native_artifact(
            identity, "message-correctness", artifact=artifact, received_at_unix=13.5,
        )
        assert store.commit_native_artifact(
            identity, "message-correctness", artifact=artifact, received_at_unix=99.0,
        ) == committed

        assert committed["schema"] == "native-review-artifact-commit/v1"
        assert committed["accepted"] is True
        assert committed["reason"] is None
        assert committed["artifact"]["artifact" ] == artifact
        assert committed["event"]["event_type"] == "completed"
        assert committed["event"]["timing"] == {
            "occurred_at_unix": 13.25,
            "started_at_unix": 11.0,
            "finished_at_unix": 13.25,
            "duration_seconds": pytest.approx(2.25),
        }
        view = store.manifest(started["run_id"])["native_tasks"][0]
        assert view["artifacts"] == [committed["artifact"]]
        assert len(view["artifact_messages"]) == 1
        assert view["artifact_messages"][0]["accepted"] is True
        assert [event["event_id"] for event in view["events"]] == [
            "queue", "start", "artifact-commit:message-correctness",
        ]
        assert view["events"][-1] == committed["event"]

        changed = native_artifact(identity, payload={
            "schema": "final-review-worker-result/v1",
            "status": "success",
            "summary": "A different result must not replace the first.",
            "findings": [],
            "warnings": [],
            "unreviewed": [],
        })
        with pytest.raises(StoreError, match="idempotent"):
            store.commit_native_artifact(
                identity, "message-correctness", artifact=changed, received_at_unix=14.0,
            )
        assert store.manifest(started["run_id"])["native_tasks"][0]["artifacts"] == [
            committed["artifact"],
        ]


def test_artifact_commit_rolls_back_partial_state_and_preserves_lens_evidence(tmp_path):
    with FinalStore(tmp_path / "artifact-atomic.sqlite3") as store:
        started = store.start_run(review_stack())
        identity = native_identity(started)
        unit_id = identity["unit_id"]
        store.record_lens(
            unit_id, identity["lens"], "incomplete", "Prior interruption",
            evidence={"source": "trusted-existing-lens"},
        )
        store.register_native_task(native_task(identity))
        store.record_native_event(native_event(identity, "queue", "queued", 10.0))
        store.record_native_event(
            native_event(identity, "start", "started", 11.0, started=11.0),
        )
        before = store.manifest(started["run_id"])
        artifact = native_artifact(identity)

        partial = native_artifact(identity, payload={
            "schema": "final-review-worker-result/v1", "status": "success",
        })
        with pytest.raises(ValueError, match="incomplete"):
            store.commit_native_artifact(
                identity, "message-correctness", artifact=partial, received_at_unix=13.5,
            )
        assert store.manifest(started["run_id"]) == before

        store.connection.execute(
            "CREATE TRIGGER reject_artifact_message BEFORE INSERT ON native_artifact_messages "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            store.commit_native_artifact(
                identity, "message-correctness", artifact=artifact, received_at_unix=13.5,
            )
        assert store.manifest(started["run_id"]) == before
        store.connection.execute("DROP TRIGGER reject_artifact_message")

        committed = store.commit_native_artifact(
            identity, "message-correctness", artifact=artifact, received_at_unix=13.5,
        )
        store.record_native_event(
            native_event(identity, "late-warning", "warning", 12.0),
        )
        after = store.manifest(started["run_id"])
        task = after["native_tasks"][0]
        assert [event["event_id"] for event in task["events"]] == [
            "queue", "start", "late-warning", "artifact-commit:message-correctness",
        ]
        assert task["artifacts"] == [committed["artifact"]]
        assert len(task["artifact_messages"]) == 1
        assert after["units"] == before["units"]


def test_artifact_file_failure_rolls_back_the_entire_success_commit(tmp_path, monkeypatch):
    with FinalStore(tmp_path / "artifact-io.sqlite3") as store:
        started = store.start_run(review_stack())
        identity = native_identity(started)
        store.register_native_task(native_task(identity))
        artifact_path = tmp_path / "rejected.json"

        def fail_write(_path, _artifact):
            raise OSError("injected artifact I/O failure")

        monkeypatch.setattr(final_store, "_atomic_artifact_write", fail_write)
        with pytest.raises(OSError, match="injected artifact I/O failure"):
            store.commit_native_artifact(
                identity, "message-correctness", artifact=native_artifact(identity),
                received_at_unix=13.5, artifact_path=artifact_path,
            )

        recorded = store.manifest(started["run_id"])["native_tasks"][0]
        assert recorded["artifacts"] == []
        assert not any(event["event_type"] == "completed" for event in recorded["events"])
        assert not artifact_path.exists()


def test_database_commit_failure_removes_newly_published_artifact(tmp_path, monkeypatch):
    with FinalStore(tmp_path / "artifact-db-commit.sqlite3") as store:
        started = store.start_run(review_stack())
        identity = native_identity(started)
        store.register_native_task(native_task(identity))
        artifact_path = tmp_path / "uncommitted.json"
        class CommitFailure:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def commit(self):
                raise sqlite3.OperationalError("injected database commit failure")

        monkeypatch.setattr(store, "_conn", CommitFailure(store._conn))
        with pytest.raises(sqlite3.DatabaseError, match="injected database commit failure"):
            store.commit_native_artifact(
                identity, "message-correctness", artifact=native_artifact(identity),
                received_at_unix=13.5, artifact_path=artifact_path,
            )

        assert not artifact_path.exists()
        recorded = store.manifest(started["run_id"])["native_tasks"][0]
        assert recorded["artifacts"] == []
        assert not any(event["event_type"] == "completed" for event in recorded["events"])


def test_superseded_run_cannot_commit_late_artifact_even_without_scheduler(tmp_path):
    with FinalStore(tmp_path / "artifact-superseded.sqlite3") as store:
        first = store.start_run(review_stack())
        store.start_run(review_stack(source_sha="c" * 40))
        identity = native_identity(first)
        store.register_native_task(native_task(identity))
        artifact_path = tmp_path / "late.json"

        with pytest.raises(StoreError, match="superseded"):
            store.commit_native_artifact(
                identity, "message-correctness", artifact=native_artifact(identity),
                received_at_unix=13.5, artifact_path=artifact_path,
            )

        recorded = store.manifest(first["run_id"])["native_tasks"][0]
        assert recorded["artifacts"] == []
        assert not any(event["event_type"] == "completed" for event in recorded["events"])
        assert not artifact_path.exists()


def test_executor_records_expiry_cancellation_through_existing_native_store(tmp_path):
    with FinalStore(tmp_path / "executor-events.sqlite3") as store:
        started = store.start_run(review_stack())
        run_id = started["run_id"]
        task = NativeTask(
            run_id=run_id,
            task_id="task-correctness",
            session_name="review-task-correctness",
            unit_id=started["unit_ids"]["individual:1"],
            stage="individual",
            lens="correctness",
            revision_tuple=("b" * 40, "a" * 40, "b" * 40),
            input_sha256="a" * 64,
            backend="kimchi",
            model="kimi-k3.1-pro",
            deadline_at_unix=200.0,
            prompt="Return the contracted result.",
            repo_root=tmp_path,
            artifact_path=tmp_path / "result.json",
        )
        timer = _DeadlineTimer()
        deadline = RunDeadline(run_id, 30, timer_factory=timer)
        executor = NativeExecutor(_FakeHost(), deadline=deadline, store=store)
        deadline.start()

        executor.start(task, lambda _completion: None)
        timer.fire()

        events = store.manifest(run_id)["native_tasks"][0]["events"]
        assert [event["event_type"] for event in events] == [
            "queued", "started", "timeout",
        ]
        assert events[0]["details"]["run_timeout_seconds"] == 30.0
        assert events[0]["details"]["watchdog"]["warnings"]
        assert events[-1]["details"] == {
            "reason": "run-deadline-exceeded",
            "expiry_count": 1,
            "cancellation_status": "native-handle-signalled",
        }
        assert not task.artifact_path.exists()
