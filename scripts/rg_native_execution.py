"""Native-scheduler host for bounded repository and test execution workers."""
from __future__ import annotations

import json
import threading
import time

from rg_execution_ownership import execution_ownership
from rg_native_executor import NativeHostCompletion

NATIVE_EXECUTION_TRANSPORT = "native-execution/owned-process-v1"


class _ExecutionSession:
    def __init__(self, task, callback, started_at_unix):
        self.task = task
        self.native_session_id = f"execution:{task.run_id}:{task.task_id}"
        self._callback = callback
        self._started_at = started_at_unix
        self._lock = threading.RLock()
        self._processes = set()
        self._cancel_reason = None
        self._timer = None

    def start(self):
        worker = threading.Thread(
            target=self._run, name="native-execution-" + self.task.task_id, daemon=True,
        )
        worker.start()
        deadline = self.task.runtime_deadline_at_unix or (
            time.time() + self.task.timeout_seconds
            if self.task.timeout_seconds is not None
            else self.task.deadline_at_unix
        )
        remaining = max(0.0, deadline - time.time())
        self._timer = threading.Timer(
            remaining, self._cancel, args=("worker-deadline-exceeded",),
        )
        self._timer.daemon = True
        self._timer.start()

    def own_process(self, process):
        with self._lock:
            if self._cancel_reason is None:
                self._processes.add(process)
                return
        self.kill_process(process)
        self.close_process(process)

    def release_process(self, process):
        with self._lock:
            self._processes.discard(process)

    def kill_process(self, process):
        from rg_final_review import _kill_owned_process

        _kill_owned_process(process)

    def close_process(self, process):
        from rg_final_review import _close_owned_pipes

        _close_owned_pipes(process)

    def cancel(self):
        self._cancel("operator-cancelled")

    def _cancel(self, reason):
        with self._lock:
            if self._cancel_reason is not None:
                return
            self._cancel_reason = reason
            processes = list(self._processes)
        for process in processes:
            self.kill_process(process)
            self.close_process(process)

    def _run(self):
        error = None
        stdout = ""
        return_code = 0
        try:
            with execution_ownership(self):
                result = self.task.execute()
            if not isinstance(result, dict):
                raise TypeError("native execution runner must return a result object")
            stdout = json.dumps({
                "schema": "final-review-worker-result/v1",
                "status": "success",
                "summary": self.task.lens,
                "findings": [],
                "warnings": [],
                "execution_result": result,
            })
        except Exception as exc:
            error = f"native {self.task.lens} execution failed: {exc}"
            return_code = 1
        finished = time.time()
        with self._lock:
            reason = self._cancel_reason
            running_processes = len(self._processes)
        if self._timer is not None:
            self._timer.cancel()
        self._callback(NativeHostCompletion(
            native_session_id=self.native_session_id,
            return_code=return_code,
            stdout=stdout,
            stderr="",
            started_at_unix=self._started_at,
            finished_at_unix=finished,
            cleanup={
                "complete": running_processes == 0,
                "status": "complete" if running_processes == 0 else "incomplete",
                "owned_processes_remaining": running_processes,
            },
            error=error,
            cancellation_reason=reason,
        ))


class NativeExecutionHost:
    """Execute Python runner tasks with run-scoped child process ownership."""

    transport = NATIVE_EXECUTION_TRANSPORT

    def launch(self, task, callback):
        session = _ExecutionSession(task, callback, time.time())
        session.start()
        return session
