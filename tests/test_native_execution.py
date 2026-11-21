"""Owned repository/test execution on the native scheduler."""
import os
import signal
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rg_final_native  # noqa: E402
import rg_final_review  # noqa: E402
from rg_execution_ownership import run_owned_process  # noqa: E402
from rg_final_store import FinalStore  # noqa: E402
from rg_native_scheduler import NativeScheduler  # noqa: E402
from test_native_executor import _DeadlineTimer  # noqa: E402
from test_final_complete_standalone import standalone  # noqa: E402, F401
from test_final_complete_standalone import Provider, item  # noqa: E402
from test_final_native import ImmediateHost, native_options  # noqa: E402


def test_required_work_uses_independent_bounded_execution_slots(standalone):  # noqa: F811
    both_started = threading.Event()
    lock = threading.Lock()
    active = 0
    max_active = 0
    completed = []

    def required(repository, commit):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                both_started.set()
        try:
            assert both_started.wait(2), (
                "independent unit execution remained behind the unit barrier"
            )
            result = standalone.required(repository, commit)
            completed.append((Path(repository).name, commit))
            return result
        finally:
            with lock:
                active -= 1

    host = ImmediateHost()
    output = rg_final_review.run_final_review(
        item()["url"], provider=Provider(),
        store_path=standalone.tmp_path / "native-execution.sqlite3",
        required_runner=required, dynamic_runner=standalone.dynamic,
        environ=native_options(host, RG_FINAL_EXECUTION_WORKERS="2"),
        native_host=host,
    )

    assert output["complete"] is True
    assert max_active == 2
    assert len(completed) == 2
    assert len(host.tasks) == 14


def test_execution_expiry_kills_owned_process_group_and_records_timeout(standalone):  # noqa: F811
    _assert_execution_expiry_records_timeout(standalone)


def test_expiry_teardown_waits_for_worker_to_record_finalization(standalone):  # noqa: F811
    # A task's completion wakes its waiter before its worker thread records the
    # run finalization. Hold the worker in that window: teardown must not close
    # the store underneath it (the CI race, made deterministic).
    real_finalize = NativeScheduler._maybe_finalize

    def slow_worker_finalize(self, run_id):
        if threading.current_thread().name.startswith("native-execution-"):
            time.sleep(0.5)
        return real_finalize(self, run_id)

    standalone.monkeypatch.setattr(NativeScheduler, "_maybe_finalize", slow_worker_finalize)
    _assert_execution_expiry_records_timeout(standalone)


def _assert_execution_expiry_records_timeout(standalone):  # noqa: F811
    timer = _DeadlineTimer()
    real_deadline = rg_final_native.RunDeadline

    def controlled_deadline(run_id, timeout_seconds, **kwargs):
        return real_deadline(
            run_id, timeout_seconds, timer_factory=timer, **kwargs,
        )

    standalone.monkeypatch.setattr(rg_final_native, "RunDeadline", controlled_deadline)
    descendant_path = standalone.tmp_path / "execution-descendant.pid"
    process_started = threading.Event()

    def required(repository, commit):
        process_started.set()
        run_owned_process([
            sys.executable, "-c",
            "import pathlib, subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            f"pathlib.Path({str(descendant_path)!r}).write_text(str(child.pid)); time.sleep(60)",
        ], capture_output=True, text=True)
        return standalone.required(repository, commit)

    host = ImmediateHost()
    outputs = []
    errors = []

    def run():
        try:
            outputs.append(rg_final_review.run_final_review(
                item()["url"], provider=Provider(),
                store_path=standalone.tmp_path / "native-execution-expiry.sqlite3",
                required_runner=required, dynamic_runner=standalone.dynamic,
                environ=native_options(host), native_host=host,
            ))
        except BaseException as exc:
            errors.append(exc)

    review = threading.Thread(target=run)
    review.start()
    assert process_started.wait(5)
    timer.fire()
    review.join(8)

    try:
        assert not review.is_alive(), "owned test process survived run expiry"
        assert not errors
        assert outputs[0]["complete"] is False
        assert outputs[0]["state"] == "incomplete"
        with FinalStore(standalone.tmp_path / "native-execution-expiry.sqlite3") as store:
            native_tasks = store.manifest(outputs[0]["run_id"])["native_tasks"]
            execution_events = [
                event for task in native_tasks if task["identity"]["stage"].startswith(
                    "execution:"
                ) for event in task["events"]
            ]
            assert any(event["event_type"] == "timeout" for event in execution_events)
            assert any(event["event_type"] == "finalization" for event in execution_events)
            assert not any(task["artifacts"] for task in native_tasks
                           if task["identity"]["stage"].startswith("execution:"))
        if descendant_path.exists():
            descendant = int(descendant_path.read_text())
            gone_by = time.monotonic() + 3
            while time.monotonic() < gone_by:
                try:
                    os.kill(descendant, 0)
                except ProcessLookupError:
                    break
                except OSError:
                    if os.name != "nt":  # Windows reports a dead pid as WinError 87
                        raise
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("required-test descendant outlived run cancellation")
    finally:
        if descendant_path.exists():
            try:
                os.kill(int(descendant_path.read_text()),
                        getattr(signal, "SIGKILL", signal.SIGTERM))
            except OSError:  # already gone (ProcessLookupError, or WinError 87 on Windows)
                pass
