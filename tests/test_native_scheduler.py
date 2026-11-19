"""Ticket #15: global event-driven native task scheduling."""
from __future__ import annotations

import itertools
import json
import sys
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from rg_final_store import FinalStore  # noqa: E402
from rg_native_executor import NativeExecutor, NativeTask  # noqa: E402
from rg_native_scheduler import NativeScheduler, SchedulerTask  # noqa: E402
from rg_run_lifecycle import RunDeadline  # noqa: E402
from test_native_executor import _DeadlineTimer, _FakeHost  # noqa: E402
from test_native_persistence import review_stack  # noqa: E402
from test_final_stops import _blocking_finding, _stop_result  # noqa: E402


def _result(summary="complete", **changes):
    result = {
        "schema": "final-review-worker-result/v1",
        "status": "success",
        "summary": summary,
        "findings": [],
        "warnings": [],
        "unreviewed": [],
    }
    result.update(changes)
    return result


def _task(tmp_path, unit_id, task_id, *, run_id):
    return NativeTask(
        run_id=run_id,
        task_id=task_id,
        session_name=f"review-{run_id}-{task_id}",
        unit_id=unit_id,
        stage="individual",
        lens="correctness",
        revision_tuple=("b" * 40, "a" * 40, "b" * 40),
        input_sha256="a" * 64,
        backend="kimchi",
        model="kimi-k3.1-pro",
        deadline_at_unix=200.0,
        prompt="Return the contracted result.",
        repo_root=tmp_path,
        artifact_path=tmp_path / f"{task_id}.json",
    )


def _scheduler(tmp_path, **kwargs):
    store = FinalStore(tmp_path / "scheduler.sqlite3")
    started = store.start_run(review_stack())
    run_id = started["run_id"]
    host = _FakeHost()
    executor = NativeExecutor(host, store=store, deadline=kwargs.pop("deadline", None))
    scheduler = NativeScheduler(executor, **kwargs)
    return store, run_id, started["unit_ids"]["individual:1"], host, scheduler


def test_native_completion_commits_artifact_and_defers_finalization_until_close(tmp_path):
    store, run_id, unit_id, host, scheduler = _scheduler(tmp_path)
    finalized = []
    task = _task(tmp_path, unit_id, "task-a", run_id=run_id)
    scheduler.submit(SchedulerTask(task), on_finalize=finalized.append)

    assert set(host.sessions) == {"task-a"}
    host.sessions["task-a"].finish(_result())

    view = store.manifest(run_id)["native_tasks"][0]
    assert len(view["artifacts"]) == 1
    assert view["artifacts"][0]["message_id"] == "native-completion:task-a"
    assert view["artifact_messages"][0]["accepted"] is True
    assert json.loads(task.artifact_path.read_text(encoding="utf-8")) == (
        view["artifacts"][0]["artifact"]
    )
    assert finalized == []

    scheduler.close_run(run_id)

    assert [item.task.task.task_id for item in finalized] == ["task-a"]
    assert scheduler.snapshot(run_id)["finalized"] is True
    store.close()


def test_supervisor_commit_failure_has_failed_evidence_and_no_success_file(
        tmp_path, monkeypatch):
    store, run_id, unit_id, host, scheduler = _scheduler(tmp_path)
    task = _task(tmp_path, unit_id, "task-a", run_id=run_id)
    completions = []
    scheduler.submit(SchedulerTask(task), on_complete=completions.append)

    def reject(*_args, **_kwargs):
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(store, "commit_native_artifact", reject)
    host.sessions["task-a"].finish(_result("untrusted"))

    result = scheduler.snapshot(run_id)["tasks"]["task-a"]
    assert result.state == "failed"
    assert result.artifact is None
    assert result.source_artifact is not None
    assert result.error == "native supervisor rejected completion: injected commit failure"
    assert not task.artifact_path.exists()
    events = store.manifest(run_id)["native_tasks"][0]["events"]
    assert not any(event["event_type"] == "completed" for event in events)
    assert [event["event_type"] for event in events].count("failed") == 1
    assert events[-1]["details"]["error"] == result.error
    assert len(completions) == 1
    store.close()


def test_parser_exception_is_one_failed_scheduler_completion(tmp_path):
    store = FinalStore(tmp_path / "parser.sqlite3")
    started = store.start_run(review_stack())
    run_id = started["run_id"]
    host = _FakeHost()

    def malformed(_stdout):
        raise ValueError("injected parser failure")

    scheduler = NativeScheduler(NativeExecutor(
        host, store=store, result_parser=malformed,
    ))
    task = _task(tmp_path, started["unit_ids"]["individual:1"], "task-a", run_id=run_id)
    completions = []
    scheduler.submit(SchedulerTask(task), on_complete=completions.append)

    host.sessions["task-a"].finish(_result("unparsed"))

    assert len(completions) == 1
    assert completions[0].state == "failed"
    assert completions[0].error == "native result parsing failed: injected parser failure"
    assert completions[0].artifact is None
    assert not task.artifact_path.exists()
    events = store.manifest(run_id)["native_tasks"][0]["events"]
    assert [event["event_type"] for event in events].count("failed") == 1
    assert not any(event["event_type"] == "completed" for event in events)
    store.close()


@pytest.mark.parametrize("limits,expected", [(None, (4, 2)), ((2, 1), (2, 1))])
def test_resource_limits_are_global_across_units_and_classes(tmp_path, limits, expected):
    options = {}
    if limits is not None:
        options = {"inference_limit": limits[0], "execution_limit": limits[1]}
    store, run_id, unit_id, host, scheduler = _scheduler(tmp_path, **options)
    other_unit = next(
        unit["unit_id"] for unit in store.manifest(run_id)["units"]
        if unit["unit_id"] != unit_id
    )
    for resource, count in (("inference", 6), ("execution", 4)):
        for index in range(count):
            task_id = f"{resource}-{index}"
            task = _task(
                tmp_path, unit_id if index % 2 == 0 else other_unit, task_id,
                run_id=run_id,
            )
            scheduler.submit(SchedulerTask(task, resource=resource))

    launched = set(host.sessions)
    assert len([name for name in launched if name.startswith("inference")]) == expected[0]
    assert len([name for name in launched if name.startswith("execution")]) == expected[1]

    host.sessions["inference-0"].finish(_result())
    assert f"inference-{expected[0]}" in host.sessions
    host.sessions["execution-0"].finish(_result())
    assert f"execution-{expected[1]}" in host.sessions
    assert len([name for name in host.sessions if name.startswith("inference")]) == expected[0] + 1
    assert len([name for name in host.sessions if name.startswith("execution")]) == expected[1] + 1
    store.close()


@pytest.mark.parametrize("option", ["inference_limit", "execution_limit"])
@pytest.mark.parametrize("value", [0, -1, 1.5, True, "2"])
def test_resource_limits_require_positive_integers(tmp_path, option, value):
    with pytest.raises(ValueError, match=option):
        _scheduler(tmp_path, **{option: value})


@pytest.mark.parametrize("ordering", list(itertools.permutations(("root", "dependent", "independent"))))
def test_validated_stop_has_order_independent_dependency_finalization(tmp_path, ordering):
    case = tmp_path / "-".join(ordering)
    case.mkdir()
    store, run_id, unit_id, host, scheduler = _scheduler(case)
    finalized = []
    definitions = [
        SchedulerTask(_task(case, unit_id, "root", run_id=run_id)),
        SchedulerTask(
            _task(case, unit_id, "dependent", run_id=run_id),
            dependencies=("root",), speculative=True,
        ),
        SchedulerTask(
            _task(case, unit_id, "leaf", run_id=run_id),
            dependencies=("dependent",),
        ),
        SchedulerTask(_task(case, unit_id, "independent", run_id=run_id)),
    ]
    for definition in definitions:
        scheduler.submit(definition, on_finalize=finalized.append)

    assert scheduler.close_run(run_id) is False
    stop = _stop_result()
    stop.update({
        "summary": "Validated dependency stop", "findings": [],
        "warnings": [], "unreviewed": [],
    })
    for task_id in ordering:
        host.sessions[task_id].finish(stop if task_id == "root" else _result(task_id))

    snapshot = scheduler.snapshot(run_id)
    assert snapshot["finalized"] is True
    assert [result.task.task.task_id for result in finalized] == ["root", "independent"]
    assert snapshot["tasks"]["dependent"].state == "completed"
    assert snapshot["tasks"]["dependent"].artifact is not None
    assert snapshot["tasks"]["dependent"].eligible is False
    assert snapshot["tasks"]["leaf"].state == "skipped"
    assert "leaf" not in host.sessions
    assert len(store.manifest(run_id)["native_tasks"][0]["artifacts"]) == 1
    store.close()


def test_ordinary_blocking_finding_does_not_stop_dependents(tmp_path):
    store, run_id, unit_id, host, scheduler = _scheduler(tmp_path)
    finalized = []
    root = SchedulerTask(_task(tmp_path, unit_id, "root", run_id=run_id))
    dependent = SchedulerTask(
        _task(tmp_path, unit_id, "dependent", run_id=run_id),
        dependencies=("root",), speculative=True,
    )
    scheduler.submit(root, on_finalize=finalized.append)
    scheduler.submit(dependent, on_finalize=finalized.append)
    scheduler.close_run(run_id)

    host.sessions["root"].finish(_result(findings=[_blocking_finding()]))
    host.sessions["dependent"].finish(_result())

    assert [result.task.task.task_id for result in finalized] == ["root", "dependent"]
    assert scheduler.snapshot(run_id)["tasks"]["dependent"].eligible is True
    store.close()


@pytest.mark.parametrize("blocked_by", ["capacity", "dependency"])
def test_expiry_cleans_only_owned_handles_and_never_dispatches_queued_work(
        tmp_path, monkeypatch, blocked_by):
    store = FinalStore(tmp_path / "deadline.sqlite3")
    started = store.start_run(review_stack())
    run_id = started["run_id"]
    unit_id = started["unit_ids"]["individual:1"]
    timer = _DeadlineTimer()
    owner = {}
    deadline = RunDeadline(
        run_id, 30, timer_factory=timer,
        on_expire=lambda _details: owner["scheduler"].expire_run(run_id),
    )
    host = _FakeHost()
    executor = NativeExecutor(host, deadline=deadline, store=store)
    scheduler = NativeScheduler(
        executor, inference_limit=1 if blocked_by == "capacity" else 2,
    )
    owner["scheduler"] = scheduler
    deadline.start()
    finalized = []
    scheduler.submit(
        SchedulerTask(_task(tmp_path, unit_id, "root", run_id=run_id)),
        on_finalize=finalized.append,
    )
    dependencies = () if blocked_by == "capacity" else ("root",)
    scheduler.submit(
        SchedulerTask(
            _task(tmp_path, unit_id, "queued", run_id=run_id),
            dependencies=dependencies,
        ),
        on_finalize=finalized.append,
    )
    scheduler.close_run(run_id)

    def forbidden_fallback(*args, **kwargs):
        raise AssertionError("scheduler attempted a hidden process fallback")

    monkeypatch.setattr("rg_native_executor.subprocess.Popen", forbidden_fallback)
    timer.fire()

    snapshot = scheduler.snapshot(run_id)
    assert snapshot["finalized"] is True
    assert snapshot["cancelled"] == "run-deadline-exceeded"
    assert [result.state for result in snapshot["tasks"].values()] == [
        "cancelled", "cancelled",
    ]
    assert set(host.sessions) == {"root"}
    assert host.sessions["root"].cancel_calls == 1
    assert finalized == []
    host.sessions["root"].finish(_result("late"))
    assert len(store.manifest(run_id)["native_tasks"]) == 2
    assert all(
        not task["artifacts"] for task in store.manifest(run_id)["native_tasks"]
    )
    store.close()


def test_duplicate_and_late_completion_cannot_rewrite_artifact_or_repeat_finalize(tmp_path):
    store, run_id, unit_id, host, scheduler = _scheduler(tmp_path)
    finalized = []
    task = _task(tmp_path, unit_id, "task-a", run_id=run_id)
    scheduler.submit(SchedulerTask(task), on_finalize=finalized.append)
    session = host.sessions["task-a"]

    session.finish(_result("accepted"))
    assert scheduler.close_run(run_id) is True
    before = store.manifest(run_id)
    session.finish(_result("duplicate"))
    session.emit(json.dumps(_result("late")), 0)
    assert scheduler.close_run(run_id) is True

    assert store.manifest(run_id) == before
    assert len(finalized) == 1
    assert finalized[0].artifact.payload["summary"] == "accepted"
    assert len(store.manifest(run_id)["native_tasks"][0]["artifacts"]) == 1
    store.close()


def test_rejected_stop_is_committed_as_visible_ordinary_evidence(tmp_path):
    store, run_id, unit_id, host, scheduler = _scheduler(tmp_path)
    finalized = []
    root = SchedulerTask(_task(tmp_path, unit_id, "root", run_id=run_id))
    dependent = SchedulerTask(
        _task(tmp_path, unit_id, "dependent", run_id=run_id),
        dependencies=("root",), speculative=True,
    )
    scheduler.submit(root, on_finalize=finalized.append)
    scheduler.submit(dependent, on_finalize=finalized.append)
    scheduler.close_run(run_id)
    rejected_stop = _stop_result(confidence=0.1)
    rejected_stop.update({
        "summary": "Rejected stop", "findings": [], "warnings": [], "unreviewed": [],
    })

    host.sessions["root"].finish(rejected_stop)
    host.sessions["dependent"].finish(_result())

    assert [result.task.task.task_id for result in finalized] == ["root", "dependent"]
    root_result = scheduler.snapshot(run_id)["tasks"]["root"]
    assert root_result.artifact.payload["status"] == "success"
    assert any(
        warning["code"] == "structured-stop-rejected"
        for warning in root_result.artifact.payload["warnings"]
    )
    store.close()


def test_callback_may_arrive_before_native_launch_returns(tmp_path):
    class EarlyHost(_FakeHost):
        def launch(self, task, callback):
            session = super().launch(task, callback)
            session.finish(_result("early"))
            return session

    store = FinalStore(tmp_path / "early.sqlite3")
    started = store.start_run(review_stack())
    run_id = started["run_id"]
    executor = NativeExecutor(EarlyHost(), store=store)
    scheduler = NativeScheduler(executor)
    finalized = []
    scheduler.submit(SchedulerTask(
        _task(tmp_path, started["unit_ids"]["individual:1"], "early", run_id=run_id),
    ), on_finalize=finalized.append)

    assert scheduler.snapshot(run_id)["tasks"]["early"].state == "completed"
    assert scheduler.close_run(run_id) is True
    assert len(finalized) == 1
    assert finalized[0].artifact.payload["summary"] == "early"
    store.close()


def test_cancelling_one_run_does_not_cancel_another_runs_children(tmp_path):
    store = FinalStore(tmp_path / "two-runs.sqlite3")
    first = store.start_run(review_stack())
    second = store.start_run(review_stack(source_sha="c" * 40))
    host = _FakeHost()
    scheduler = NativeScheduler(NativeExecutor(host, store=store))
    first_task = _task(
        tmp_path, first["unit_ids"]["individual:1"], "first", run_id=first["run_id"],
    )
    second_task = _task(
        tmp_path, second["unit_ids"]["individual:1"], "second", run_id=second["run_id"],
    )
    scheduler.submit(SchedulerTask(first_task))
    scheduler.submit(SchedulerTask(second_task))

    scheduler.cancel_run(first["run_id"])

    assert host.sessions["first"].cancel_calls == 1
    assert host.sessions["second"].cancel_calls == 0
    assert scheduler.snapshot(first["run_id"])["cancelled"] == "operator-cancelled"
    assert scheduler.snapshot(second["run_id"])["cancelled"] is None
    host.sessions["first"].finish(_result("late"))
    host.sessions["second"].finish(_result("safe"))
    assert second_task.artifact_path.exists()
    assert not first_task.artifact_path.exists()
    store.close()


def test_cancelled_run_waits_for_owned_worker_before_cleanup_and_finalization(tmp_path):
    finalized = []
    store, run_id, unit_id, host, scheduler = _scheduler(
        tmp_path, on_finalized=finalized.append,
    )
    task = _task(tmp_path, unit_id, "task-a", run_id=run_id)
    scheduler.submit(SchedulerTask(task))
    session = host.sessions["task-a"]

    def deferred_cancel():
        session.cancel_calls += 1

    session.cancel = deferred_cancel
    scheduler.close_run(run_id)
    scheduler.cancel_run(run_id)

    snapshot = scheduler.snapshot(run_id)
    assert snapshot["cancelled"] == "operator-cancelled"
    assert snapshot["finalized"] is False
    assert snapshot["tasks"]["task-a"].state == "running"
    assert finalized == []

    session.emit("", -9)

    assert scheduler.snapshot(run_id)["finalized"] is True
    assert scheduler.snapshot(run_id)["tasks"]["task-a"].state == "cancelled"
    assert finalized == [run_id]
    events = store.manifest(run_id)["native_tasks"][0]["events"]
    event_types = [event["event_type"] for event in events]
    assert event_types.count("finalization") == 1
    assert event_types[-1] == "finalization"
    assert event_types.index("cleanup") > event_types.index("cancelled")
    store.close()


def test_cancellation_during_validation_owns_finalization_order(tmp_path):
    from rg_final_execution import validate_stop

    store, run_id, unit_id, host, scheduler_owner = _scheduler(tmp_path)
    del scheduler_owner
    validating = threading.Event()
    release = threading.Event()

    def ordered_stop_validator(result, *, stage):
        validating.set()
        assert release.wait(5)
        return validate_stop(result, stage=stage)

    host = _FakeHost()
    scheduler = NativeScheduler(
        NativeExecutor(host, store=store), stop_validator=ordered_stop_validator,
    )
    finalized = []
    task = _task(tmp_path, unit_id, "task-a", run_id=run_id)
    scheduler.submit(SchedulerTask(task), on_finalize=finalized.append)
    completed = threading.Event()

    def finish():
        host.sessions["task-a"].finish(_result("evidence only"))
        completed.set()

    worker = threading.Thread(target=finish)
    worker.start()
    assert validating.wait(5)
    scheduler.close_run(run_id)

    scheduler.cancel_run(run_id)
    release.set()
    worker.join(5)

    assert completed.is_set()
    assert finalized == []
    result = scheduler.snapshot(run_id)["tasks"]["task-a"]
    assert result.eligible is False
    assert result.artifact is None
    assert result.source_artifact is not None
    assert not task.artifact_path.exists()
    recorded = store.manifest(run_id)["native_tasks"][0]
    assert recorded["artifacts"] == []
    assert not any(event["event_type"] == "completed" for event in recorded["events"])
    assert scheduler.snapshot(run_id)["finalized"] is True
    store.close()
