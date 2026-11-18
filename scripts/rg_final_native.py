"""Standalone final-review lens work on the existing native scheduler."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
import threading
import time

from rg_final_lenses import lens_worker_limit
from rg_native_executor import NativeExecutor, NativeTask
from rg_native_scheduler import NativeScheduler, SchedulerTask
from rg_run_lifecycle import RunDeadline, RunDeadlineExpired, run_timeout_from_env


class NativeStandaloneExecution:
    """Submit every eligible unit/lens before either unit aggregates evidence."""

    def __init__(self, store, host, *, environ, run_id, prompt_factory, progress=None):
        self._environ = environ
        self._host = host
        self._root = store.path.parent / "native" / run_id
        self._prompt_factory = prompt_factory
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
        self.scheduler = NativeScheduler(
            executor,
            inference_limit=lens_worker_limit(environ),
        )
        self.deadline.start()

    @property
    def transport(self):
        return self._host.transport

    @staticmethod
    def task_id(request):
        return f"{request['unit_id']}:{request['lens']}"

    def prepare(self, requests):
        """Register all individual work before speculative combined peers."""
        individual_ids = []
        combined = []
        for request in requests:
            task_id = self.task_id(request)
            if request.get("unit_key") == "combined":
                combined.append((request, task_id))
                continue
            self._submit(request, task_id)
            individual_ids.append(task_id)
        for request, task_id in combined:
            self._submit(
                request,
                task_id,
                dependencies=tuple(individual_ids),
                speculative=True,
            )

    def _submit(self, request, task_id, *, dependencies=(), speculative=False):
        ready = threading.Event()
        with self._lock:
            self._waiters[task_id] = ready
        serialized = json.dumps(
            request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
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
            SchedulerTask(
                task,
                dependencies=dependencies,
                speculative=speculative,
            ),
            on_complete=self._completed,
        )

    def _completed(self, result):
        task = result.task.task
        with self._lock:
            self._results[task.task_id] = result
            ready = self._waiters[task.task_id]
        duration = (
            ""
            if result.artifact is None
            else f"; {result.artifact.duration_seconds:.3f}s"
        )
        self._progress.write(
            f"review-gate final-review native: {task.unit_id}/{task.lens} "
            f"{result.state}{duration}\n"
        )
        self._progress.flush()
        ready.set()

    def worker(self, request):
        task_id = self.task_id(request)
        with self._lock:
            ready = self._waiters[task_id]
        ready.wait()
        with self._lock:
            result = self._results[task_id]
        if self.deadline.expired:
            raise RunDeadlineExpired(self.deadline.run_id)
        artifact = result.source_artifact or result.artifact
        if result.state != "completed" or artifact is None:
            raise RuntimeError(
                f"native {request['lens']} task {result.state}: "
                f"{result.error or 'mandatory work was not eligible'}"
            )
        # Reuse the original worker envelope: execute_unit owns rejection and
        # normalization once, while the scheduler's committed downgrade stays
        # available as the stable native transport artifact.
        return deepcopy(artifact.payload)

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
