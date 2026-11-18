"""Native standalone review migration through the existing public orchestration."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rg_final_review  # noqa: E402
from rg_final_store import FinalStore  # noqa: E402
from rg_native_executor import NativeHostCompletion  # noqa: E402
from test_final_complete_standalone import standalone  # noqa: E402, F401
from test_final_complete_standalone import (  # noqa: E402
    Provider, item, stop_result, worker_result,
)


class _Session:
    def __init__(self, task, callback):
        self.task = task
        self.callback = callback
        self.native_session_id = "double-session-" + task.task_id
        self.cancel_calls = 0

    def cancel(self):
        self.cancel_calls += 1


class ImmediateHost:
    transport = "test-double/native-host-v1"

    def __init__(self, payload_factory=None):
        self.tasks = []
        self.payload_factory = payload_factory or (lambda _task: worker_result())

    def launch(self, task, callback):
        self.tasks.append(task)
        session = _Session(task, callback)
        callback(NativeHostCompletion(
            native_session_id=session.native_session_id,
            return_code=0,
            stdout=json.dumps(self.payload_factory(task)),
            started_at_unix=time.time(),
            finished_at_unix=time.time(),
        ))
        return session


def native_options(host, **changes):
    options = {
        "RG_FINAL_REVIEW_TRANSPORT": "native",
        "RG_FINAL_NATIVE_MODEL": "test-model",
        "RG_FINAL_LENS_WORKERS": "4",
    }
    options.update(changes)
    return options


class GatedHost:
    transport = "test-double/native-host-v1"

    def __init__(self, capacity):
        self.capacity = capacity
        self.tasks = []
        self.completed = set()
        self.active = 0
        self.max_active = 0
        self.at_capacity = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()

    def launch(self, task, callback):
        session = _Session(task, callback)
        with self.lock:
            self.tasks.append(task)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active == self.capacity:
                self.at_capacity.set()

        def finish():
            assert self.release.wait(5)
            finished = time.time()
            with self.lock:
                self.active -= 1
                self.completed.add(task.task_id)
            callback(NativeHostCompletion(
                native_session_id=session.native_session_id,
                return_code=0,
                stdout=json.dumps(worker_result()),
                started_at_unix=finished - 0.01,
                finished_at_unix=finished,
            ))

        threading.Thread(target=finish, daemon=True).start()
        return session


def test_native_standalone_persists_both_units_through_existing_execution(standalone):  # noqa: F811
    store_path = standalone.tmp_path / "native.sqlite3"
    host = ImmediateHost()

    def forbidden_legacy_worker(*args, **kwargs):
        raise AssertionError("native execution must not fall back to the legacy worker")

    standalone.monkeypatch.setattr(rg_final_review, "invoke_worker", forbidden_legacy_worker)
    output = rg_final_review.run_final_review(
        item()["url"],
        provider=Provider(),
        store_path=store_path,
        required_runner=standalone.required,
        dynamic_runner=standalone.dynamic,
        environ=native_options(host),
        native_host=host,
    )

    assert output["complete"] is True
    assert output["state"] == "pass"
    assert len(host.tasks) == 14
    assert {task.unit_id for task in host.tasks} == {
        output["unit_id"], output["combined_unit_id"],
    }
    assert len({task.session_name for task in host.tasks}) == 14
    assert standalone.stages == ["required", "dynamic", "required", "dynamic"]
    with FinalStore(store_path) as store:
        manifest = store.manifest(output["run_id"])
        assert len(manifest["native_tasks"]) == 14
        assert all(len(task["artifacts"]) == 1 for task in manifest["native_tasks"])
        for unit in manifest["units"]:
            assert len(unit["lenses"]) == 7
            assert all(lens["state"] == "completed" for lens in unit["lenses"])
            assert unit["tests"], "required evidence must be durable without waiting for the peer"


def test_native_global_cap_and_progress_are_bounded(standalone, capsys):  # noqa: F811
    host = GatedHost(capacity=2)
    store_path = standalone.tmp_path / "native-cap.sqlite3"
    outputs = []
    errors = []

    def required(repository, commit):
        assert len({task.unit_id for task in host.tasks}) == 2
        return standalone.required(repository, commit)

    def run():
        try:
            outputs.append(rg_final_review.run_final_review(
                item()["url"], provider=Provider(), store_path=store_path,
                required_runner=required, dynamic_runner=standalone.dynamic,
                environ=native_options(host, RG_FINAL_LENS_WORKERS="2"),
                native_host=host,
            ))
        except BaseException as exc:
            errors.append(exc)

    review = threading.Thread(target=run)
    review.start()
    try:
        assert host.at_capacity.wait(5)
    finally:
        host.release.set()
    review.join(10)

    assert not review.is_alive()
    assert not errors
    assert outputs[0]["complete"] is True
    assert host.max_active == 2
    assert len(host.completed) == 14
    assert standalone.stages == ["required", "dynamic", "required", "dynamic"]
    captured = capsys.readouterr()
    assert captured.out == ""
    progress = [line for line in captured.err.splitlines()
                if line.startswith("review-gate final-review native:")]
    assert len(progress) == 14


def test_native_stop_is_durable_and_all_mandatory_stages_are_explicit(standalone):  # noqa: F811
    stopped = {}

    def payload(task):
        if task.lens == "correctness" and not stopped:
            stopped[task.task_id] = True
            return stop_result()
        return worker_result()

    host = ImmediateHost(payload)
    store_path = standalone.tmp_path / "native-stop.sqlite3"
    output = rg_final_review.run_final_review(
        item()["url"], provider=Provider(), store_path=store_path,
        required_runner=standalone.required, dynamic_runner=standalone.dynamic,
        environ=native_options(host), native_host=host,
    )

    assert output["state"] == "incomplete"
    assert output["complete"] is False
    assert [stop["reason_code"] for stop in output["stops"]] == [
        "unsafe-sandbox", "unsafe-sandbox",
    ]
    assert output["stops"][0]["stage"] == "lens:correctness"
    assert standalone.stages == []
    with FinalStore(store_path) as store:
        report = store.machine_report(output["run_id"])
        stopped_artifacts = [
            entry["artifact"] for task in report["native_tasks"]
            for entry in task["artifacts"]
            if entry["artifact"]["payload"]["status"] == "STOP"
        ]
        assert len(stopped_artifacts) == 1
        for unit in report["units"]:
            coverage = {entry["kind"]: entry["status"] for entry in unit["coverage"]}
            assert coverage["required_tests"] == "skipped"
            assert coverage["dynamic_tests"] == "skipped"
            assert len(unit["lenses"]) == 7
