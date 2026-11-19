"""Global event-driven scheduling for native review tasks."""
from __future__ import annotations

from dataclasses import dataclass, replace
import threading
import time
from typing import Callable, Optional, Tuple

from rg_final_execution import StopRequestError, _rejected_result, validate_stop
from rg_native_executor import NativeArtifact, NativeExecutor, NativeTask
from rg_run_lifecycle import RunDeadlineExpired


_TERMINAL_STATES = frozenset(("completed", "failed", "cancelled", "skipped"))


@dataclass(frozen=True)
class SchedulerTask:
    """One native worker and its scheduler-owned resource/dependency metadata."""

    task: NativeTask
    resource: str = "inference"
    dependencies: Tuple[str, ...] = ()
    speculative: bool = False


@dataclass(frozen=True)
class ScheduledResult:
    """The scheduler's stable terminal view of one submitted task."""

    task: SchedulerTask
    state: str
    artifact: Optional[NativeArtifact]
    eligible: bool
    error: Optional[str] = None
    source_artifact: Optional[NativeArtifact] = None


@dataclass
class _Entry:
    definition: SchedulerTask
    on_finalize: Optional[Callable[[ScheduledResult], None]]
    on_complete: Optional[Callable[[ScheduledResult], None]]
    state: str = "queued"
    handle: object = None
    artifact: Optional[NativeArtifact] = None
    source_artifact: Optional[NativeArtifact] = None
    error: Optional[str] = None
    eligible: bool = True
    callback_received: bool = False
    completion_notified: bool = False
    finalized: bool = False

    def result(self):
        return ScheduledResult(
            task=self.definition, state=self.state, artifact=self.artifact,
            eligible=self.eligible, error=self.error,
            source_artifact=self.source_artifact,
        )


class NativeScheduler:
    """Dispatch native work only from submissions and terminal events."""

    def __init__(self, executor: NativeExecutor, *, store=None, clock=time.time,
                 inference_limit=4, execution_limit=2, stop_validator=validate_stop,
                 on_start=None, on_completion=None, on_finalized=None):
        for name, value in (("inference_limit", inference_limit),
                            ("execution_limit", execution_limit)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self._executor = executor
        self._store = executor.store if store is None else store
        self._clock = clock
        self._stop_validator = stop_validator
        self._on_start = on_start
        self._on_completion = on_completion
        self._on_finalized = on_finalized
        self._limits = {"inference": inference_limit, "execution": execution_limit}
        self._active = {"inference": 0, "execution": 0}
        self._pending = []
        self._entries = {}
        self._runs = {}
        self._lock = threading.RLock()

    def submit(self, definition: SchedulerTask, *, on_finalize=None, on_complete=None):
        task = definition.task
        with self._lock:
            run = self._runs.setdefault(task.run_id, {
                "closed": False, "finalized": False, "cancelled": None, "cleaning": False,
                "stopped": set(),
            })
            if run["closed"] or run["cancelled"] is not None:
                raise RuntimeError(f"run {task.run_id} is closed or cancelled")
            if task.task_id in self._entries:
                raise ValueError(f"duplicate native task {task.task_id}")
            if definition.resource not in self._limits:
                raise ValueError(f"unsupported native resource class {definition.resource}")
            missing = [task_id for task_id in definition.dependencies
                       if task_id not in self._entries]
            if missing:
                raise ValueError("unknown native task dependencies: " + ", ".join(missing))
            if any(self._entries[task_id].definition.task.run_id != task.run_id
                   for task_id in definition.dependencies):
                raise ValueError("native task dependencies must belong to the same run")
            entry = _Entry(definition, on_finalize, on_complete)
            entry.eligible = all(
                self._entries[task_id].eligible and task_id not in run["stopped"]
                for task_id in definition.dependencies
            )
            self._entries[task.task_id] = entry
            if entry.eligible:
                self._pending.append(entry)
            else:
                entry.state = "skipped"
        if entry.eligible:
            self._drain()
        else:
            self._notify_complete(entry)
        self._maybe_finalize(task.run_id)
        return definition

    def _dependencies_completed(self, entry):
        pending = list(entry.definition.dependencies)
        ancestors = set()
        while pending:
            task_id = pending.pop()
            if task_id in ancestors:
                continue
            ancestors.add(task_id)
            pending.extend(self._entries[task_id].definition.dependencies)
        return all(self._entries[task_id].state == "completed" for task_id in ancestors)

    def _drain(self):
        while True:
            with self._lock:
                entry = next((
                    candidate for candidate in self._pending
                    if self._runs[candidate.definition.task.run_id]["cancelled"] is None and
                    candidate.eligible and
                    (candidate.definition.speculative or self._dependencies_completed(candidate)) and
                    self._active[candidate.definition.resource] <
                    self._limits[candidate.definition.resource]
                ), None)
                if entry is None:
                    return
                self._pending.remove(entry)
                entry.state = "running"
                self._active[entry.definition.resource] += 1
            try:
                handle = self._executor.start(
                    entry.definition.task,
                    lambda completion, current=entry: self._completed(current, completion),
                )
            except RunDeadlineExpired:
                with self._lock:
                    self._active[entry.definition.resource] -= 1
                    entry.state = "queued"
                    self._pending.append(entry)
                self.expire_run(entry.definition.task.run_id)
                return
            except Exception as exc:
                with self._lock:
                    self._active[entry.definition.resource] -= 1
                    entry.state = "failed"
                    entry.error = f"native launch failed: {exc}"
                if self._store is not None:
                    task = entry.definition.task
                    self._store.record_native_event({
                        "schema": "native-review-event/v1",
                        "event_id": "scheduler:launch-failed:" + task.task_id,
                        "event_type": "failed",
                        "identity": task.identity(),
                        "transport": self._executor.transport,
                        "native_session_id": None,
                        "timing": {
                            "occurred_at_unix": self._clock(),
                            "started_at_unix": None, "finished_at_unix": None,
                            "duration_seconds": None,
                        },
                        "details": {"error": entry.error},
                    })
                self._notify_complete(entry)
                continue
            entry.handle = handle
            if self._on_start is not None:
                self._on_start(handle)
            with self._lock:
                cancelled = self._runs[entry.definition.task.run_id]["cancelled"] is not None
            if cancelled:
                handle.cancel()

    def _validated_artifact(self, entry, artifact):
        task = entry.definition.task
        try:
            stop = self._stop_validator(artifact.payload, stage=task.stage)
        except StopRequestError as exc:
            payload = _rejected_result(artifact.payload, exc)
            return replace(artifact, payload=payload), None
        # Preserve a valid STOP envelope for execute_unit's deterministic,
        # lens-order-aware handling. Rejected STOPs are downgraded above.
        return artifact, stop

    def _apply_stop(self, source, stop):
        del stop  # Scheduler eligibility uses only trusted graph ancestry, never worker scope.
        with self._lock:
            source_id = source.definition.task.task_id
            run_id = source.definition.task.run_id
            self._runs[run_id]["stopped"].add(source_id)
            affected = set()
            changed = True
            while changed:
                changed = False
                for task_id, entry in self._entries.items():
                    if entry.definition.task.run_id != run_id or task_id in affected:
                        continue
                    if any(parent == source_id or parent in affected
                           for parent in entry.definition.dependencies):
                        affected.add(task_id)
                        changed = True
            skipped = []
            for task_id in affected:
                entry = self._entries[task_id]
                entry.eligible = False
                if entry.state in ("queued", "waiting"):
                    entry.state = "skipped"
                    if entry in self._pending:
                        self._pending.remove(entry)
                    skipped.append(entry)
        for entry in skipped:
            self._notify_complete(entry)

    def _notify_complete(self, entry):
        with self._lock:
            if entry.completion_notified:
                return
            entry.completion_notified = True
            callback = entry.on_complete
        if callback is not None:
            callback(entry.result())

    def _completed(self, entry, completion):
        with self._lock:
            if entry.state != "running" or entry.callback_received:
                return
            entry.callback_received = True
        artifact = completion.artifact
        source_artifact = artifact
        stop = None
        error = completion.error
        state = completion.state
        if completion.state == "completed" and artifact is not None:
            try:
                if self._store is None:
                    raise RuntimeError("native artifact completion requires a store")
                artifact, stop = self._validated_artifact(entry, artifact)
                self._store.commit_native_artifact(
                    entry.definition.task.identity(),
                    "native-completion:" + entry.definition.task.task_id,
                    artifact=artifact.as_dict(),
                    received_at_unix=self._clock(),
                )
            except Exception as exc:
                artifact = None
                stop = None
                state = "failed"
                error = f"native supervisor rejected completion: {exc}"
        with self._lock:
            entry.state = state
            entry.artifact = artifact
            entry.source_artifact = source_artifact
            entry.error = error
            self._active[entry.definition.resource] -= 1
        if state == "cancelled":
            self.cancel_run(
                entry.definition.task.run_id,
                completion.details.get("reason") or "operator-cancelled",
            )
        if stop is not None and entry.eligible:
            self._apply_stop(entry, stop)
        if self._on_completion is not None:
            self._on_completion(completion)
        self._drain()
        self._notify_complete(entry)
        self._maybe_finalize(entry.definition.task.run_id)

    def _maybe_finalize(self, run_id):
        callbacks = []
        newly_finalized = False
        with self._lock:
            run = self._runs[run_id]
            if run["cancelled"] is not None:
                if run["cleaning"]:
                    return False
                newly_finalized = not run["finalized"]
                run["finalized"] = True
            else:
                if not run["closed"] or run["finalized"] or run["cleaning"]:
                    return run["finalized"]
                entries = [entry for entry in self._entries.values()
                           if entry.definition.task.run_id == run_id]
                if any(entry.state not in _TERMINAL_STATES for entry in entries):
                    return False
                newly_finalized = True
                run["finalized"] = True
                for entry in entries:
                    entry.finalized = True
                    if entry.eligible and entry.state == "completed" and \
                            entry.on_finalize is not None:
                        callbacks.append((entry.on_finalize, entry.result()))
        for callback, result in callbacks:
            callback(result)
        if newly_finalized and self._on_finalized is not None:
            self._on_finalized(run_id)
        return True

    def _record_cleanup(self, entry, reason):
        if self._store is None:
            return
        task = entry.definition.task
        self._store.register_native_task({
            "schema": "native-review-task/v1",
            "identity": task.identity(),
            "transport": self._executor.transport,
        })
        occurred = self._clock()
        self._store.record_native_event({
            "schema": "native-review-event/v1",
            "event_id": "scheduler:cleanup:" + task.task_id,
            "event_type": "cleanup",
            "identity": task.identity(),
            "transport": self._executor.transport,
            "native_session_id": None,
            "timing": {
                "occurred_at_unix": occurred, "started_at_unix": None,
                "finished_at_unix": None, "duration_seconds": None,
            },
            "details": {"reason": reason, "resource": entry.definition.resource},
        })

    def cancel_run(self, run_id, reason="operator-cancelled"):
        """Fence one run and cancel only its exact native session handles."""
        with self._lock:
            run = self._runs[run_id]
            if run["cancelled"] is not None:
                return
            run["cancelled"] = reason
            run["cleaning"] = True
            entries = [entry for entry in self._entries.values()
                       if entry.definition.task.run_id == run_id]
            handles = []
            for entry in entries:
                entry.eligible = False
                if entry.state in ("queued", "waiting"):
                    entry.state = "cancelled"
                    if entry in self._pending:
                        self._pending.remove(entry)
                elif entry.state == "running" and entry.handle is not None:
                    handles.append(entry.handle)
        for handle in handles:
            handle.cancel()
        for entry in entries:
            if entry.state not in ("completed", "failed", "skipped"):
                self._record_cleanup(entry, reason)
        for entry in entries:
            if entry.state in _TERMINAL_STATES:
                self._notify_complete(entry)
        with self._lock:
            run["cleaning"] = False
        self._maybe_finalize(run_id)

    def expire_run(self, run_id):
        self.cancel_run(run_id, "run-deadline-exceeded")

    def close_run(self, run_id):
        """Close admission and finalize only after every submitted task is terminal."""
        with self._lock:
            self._runs[run_id]["closed"] = True
        self._drain()
        return self._maybe_finalize(run_id)

    def snapshot(self, run_id):
        with self._lock:
            run = self._runs[run_id]
            return {
                "run_id": run_id,
                "closed": run["closed"],
                "finalized": run["finalized"],
                "cancelled": run["cancelled"],
                "tasks": {
                    task_id: entry.result()
                    for task_id, entry in self._entries.items()
                    if entry.definition.task.run_id == run_id
                },
            }
