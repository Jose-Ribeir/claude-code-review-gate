"""Standalone final-review lens and test work on the native scheduler."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
import threading
import time

from rg_final_dynamic import record_dynamic_result
from rg_final_execution import record_required_result
from rg_final_lenses import lens_worker_limit
from rg_native_execution import NativeExecutionHost
from rg_native_executor import NativeExecutor, NativeTask
from rg_native_scheduler import NativeScheduler, SchedulerTask
from rg_run_lifecycle import RunDeadline, RunDeadlineExpired, run_timeout_from_env


class NativeStandaloneExecution:
    """Submit eligible lens and execution tasks without an artificial unit barrier."""

    def __init__(self, store, host, *, environ, run_id, prompt_factory, progress=None,
                 required_runner=None, dynamic_runner=None, dynamic_kwargs=None):
        self._environ = environ
        self._host = host
        self._root = store.path.parent / "native" / run_id
        self._prompt_factory = prompt_factory
        self._required_runner = required_runner
        self._dynamic_runner = dynamic_runner
        self._dynamic_kwargs = dict(dynamic_kwargs or {})
        self._progress = sys.stderr if progress is None else progress
        self._waiters = {}
        self._results = {}
        self._lock = threading.Lock()
        self._closed = False
        self.scheduler = None
        self.deadline = RunDeadline(
            run_id,
            run_timeout_from_env(environ),
            environ=environ,
            on_expire=lambda _details: self.scheduler.expire_run(run_id),
        )
        executor = NativeExecutor(host, store=store, deadline=self.deadline)
        execution_executor = NativeExecutor(
            NativeExecutionHost(), store=store, deadline=self.deadline,
        )
        self.scheduler = NativeScheduler(
            executor,
            execution_executor=execution_executor,
            inference_limit=lens_worker_limit(environ),
            execution_limit=int(environ.get("RG_FINAL_EXECUTION_WORKERS", "2")),
        )
        self.deadline.start()

    @property
    def transport(self):
        return self._host.transport

    @staticmethod
    def task_id(request):
        return f"{request['unit_id']}:{request['lens']}"

    @staticmethod
    def execution_task_id(request, kind):
        return f"{request['unit_id']}:execution:{kind}"

    def prepare(self, requests, *, dependencies=None, speculative=None):
        """Register lens peers and their dependent required/dynamic execution."""
        lens_ids = {}
        if dependencies is not None:
            for request in requests:
                task_id = self.task_id(request)
                lens_ids.setdefault(request["unit_id"], []).append(task_id)
                self._submit(
                    request, task_id, dependencies=dependencies,
                    speculative=bool(dependencies) if speculative is None else speculative,
                )
        else:
            individual_ids = []
            combined = []
            for request in requests:
                task_id = self.task_id(request)
                if request.get("unit_key") == "combined":
                    combined.append((request, task_id))
                    continue
                lens_ids.setdefault(request["unit_id"], []).append(task_id)
                self._submit(request, task_id)
                individual_ids.append(task_id)
            for request, task_id in combined:
                lens_ids.setdefault(request["unit_id"], []).append(task_id)
                self._submit(
                    request, task_id, dependencies=tuple(individual_ids), speculative=True,
                )
        submitted_units = set()
        for request in requests:
            unit_id = request["unit_id"]
            if unit_id not in submitted_units:
                submitted_units.add(unit_id)
                self._submit_unit_execution(request, tuple(lens_ids[unit_id]))

    def _submit_unit_execution(self, request, lens_dependencies):
        if self._required_runner is not None:
            def required_call(request=request):
                return self._required_runner(
                    Path(request["repository"]["path"]), request["repository"]["head_sha"],
                )

            self._submit_execution(
                request, "required_tests", required_call, lens_dependencies,
            )
        if self._dynamic_runner is not None:
            def dynamic_call(request=request):
                options = dict(self._dynamic_kwargs)
                options.setdefault("unit_key", request.get("unit_key", "individual:1"))
                return self._dynamic_runner(
                    Path(request["repository"]["path"]), request["repository"]["target_sha"],
                    request["repository"]["head_sha"], **options,
                )

            self._submit_execution(
                request, "dynamic_tests", dynamic_call, lens_dependencies,
            )

    def _submit_execution(self, request, kind, call, dependencies):
        task_id = self.execution_task_id(request, kind)
        serialized = json.dumps(
            {"request": request, "execution": kind},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", task_id)
        ready = threading.Event()
        with self._lock:
            self._waiters[task_id] = ready
        task = NativeTask(
            run_id=request["run_id"],
            task_id=task_id,
            session_name="execution-" + safe_id,
            unit_id=request["unit_id"],
            stage="execution:" + kind,
            lens=kind,
            revision_tuple=tuple(str(value) for value in request["revision_tuple"]),
            input_sha256=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            backend="repository",
            model="none",
            deadline_at_unix=time.time() + float(
                self._environ.get("RG_FINAL_WORKER_TIMEOUT", "900")
            ),
            prompt="",
            repo_root=Path(request["repository"]["path"]),
            artifact_path=self._root / "artifacts" / (safe_id + ".json"),
            execute=call,
        )
        self.scheduler.submit(
            SchedulerTask(
                task, resource="execution", dependencies=dependencies,
                publish_artifact=False,
            ),
            on_complete=self._completed,
        )

    def _submit(self, request, task_id, *, dependencies=(), speculative=False):
        ready = threading.Event()
        with self._lock:
            self._waiters[task_id] = ready
        serialized = json.dumps(
            request, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", task_id)
        request_path = self._root / "requests" / (safe_id + ".json")
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text(serialized + "\n", encoding="utf-8")
        try:
            request_path.chmod(0o600)
        except OSError:
            pass
        repository_path = Path(request["repository"]["path"])
        task = NativeTask(
            run_id=request["run_id"],
            task_id=task_id,
            session_name="review-" + safe_id,
            unit_id=request["unit_id"],
            stage="lens:" + request["lens"],
            lens=request["lens"],
            revision_tuple=tuple(str(value) for value in request["revision_tuple"]),
            input_sha256=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            backend="kimchi",
            model=self._environ.get("RG_FINAL_NATIVE_MODEL", "kimi-k3.1-pro"),
            deadline_at_unix=time.time() + float(
                self._environ.get("RG_FINAL_WORKER_TIMEOUT", "900")
            ),
            prompt=self._prompt_factory(request, request_path),
            repo_root=repository_path,
            artifact_path=self._root / "artifacts" / (safe_id + ".json"),
        )
        self.scheduler.submit(
            SchedulerTask(task, dependencies=dependencies, speculative=speculative),
            on_complete=self._completed,
        )

    def _completed(self, result):
        task = result.task.task
        if task.stage == "execution:required_tests" and result.state == "completed":
            record_required_result(self.scheduler._store, task.unit_id, result.output)
        elif task.stage == "execution:dynamic_tests" and result.state == "completed":
            record_dynamic_result(self.scheduler._store, task.unit_id, result.output)
        with self._lock:
            self._results[task.task_id] = result
            ready = self._waiters[task.task_id]
        duration = (
            "" if result.artifact is None
            else f"; {result.artifact.duration_seconds:.3f}s"
        )
        self._progress.write(
            f"review-gate final-review native: {task.unit_id}/{task.lens} "
            f"{result.state}{duration}\n"
        )
        self._progress.flush()
        ready.set()

    def _result(self, task_id):
        with self._lock:
            ready = self._waiters[task_id]
        ready.wait()
        with self._lock:
            result = self._results[task_id]
        if self.deadline.expired:
            raise RunDeadlineExpired(self.deadline.run_id)
        return result

    def worker(self, request):
        result = self._result(self.task_id(request))
        artifact = result.source_artifact or result.artifact
        if result.state != "completed" or artifact is None:
            raise RuntimeError(
                f"native {request['lens']} task {result.state}: "
                f"{result.error or 'mandatory work was not eligible'}"
            )
        # Lens order and deterministic stop validation remain execute_unit-owned.
        return deepcopy(artifact.payload)

    def _execution_output(self, request, kind):
        result = self._result(self.execution_task_id(request, kind))
        if result.state != "completed" or result.output is None:
            raise RuntimeError(
                f"native {kind} task {result.state}: "
                f"{result.error or 'mandatory work was not eligible'}"
            )
        return deepcopy(result.output)

    def required_runner(self, request):
        return lambda *_args: self._execution_output(request, "required_tests")

    def dynamic_runner(self, request):
        return lambda *_args, **_kwargs: self._execution_output(request, "dynamic_tests")

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.scheduler.close_run(self.deadline.run_id)
        finally:
            self.deadline.close()

    def abort(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.scheduler.cancel_run(self.deadline.run_id)
        finally:
            self.deadline.close()
