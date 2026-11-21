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


def _event_id(task, kind):
    prefix = "scheduler" if task.attempt == 1 else f"scheduler:attempt-{task.attempt}"
    return f"{prefix}:{kind}:{task.task_id}"


@dataclass(frozen=True)
class SchedulerTask:
    """One native worker and its scheduler-owned resource/dependency metadata."""

    task: NativeTask
    resource: str = "inference"
    dependencies: Tuple[str, ...] = ()
    speculative: bool = False
    publish_artifact: bool = True


@dataclass(frozen=True)
class ScheduledResult:
    """The scheduler's stable terminal view of one submitted task."""

    task: SchedulerTask
    state: str
    artifact: Optional[NativeArtifact]
    eligible: bool
    error: Optional[str] = None
    source_artifact: Optional[NativeArtifact] = None
    output: Optional[dict] = None


@dataclass
class _Entry:
    definition: SchedulerTask
    on_finalize: Optional[Callable[[ScheduledResult], None]]
    on_complete: Optional[Callable[[ScheduledResult], None]]
    state: str = "queued"
    handle: object = None
    artifact: Optional[NativeArtifact] = None
    source_artifact: Optional[NativeArtifact] = None
    output: Optional[dict] = None
    error: Optional[str] = None
    eligible: bool = True
    callback_received: bool = False
    completion_notified: bool = False
    finalized: bool = False
    fence_recorded: bool = False
    cleanup_recorded: bool = False

    def result(self):
        return ScheduledResult(
            task=self.definition, state=self.state, artifact=self.artifact,
            eligible=self.eligible, error=self.error,
            source_artifact=self.source_artifact, output=self.output,
        )


class NativeScheduler:
    """Dispatch native work only from submissions and terminal events."""

    def __init__(self, executor: NativeExecutor, *, store=None, clock=time.time,
                 inference_limit=4, execution_limit=2, stop_validator=validate_stop,
                 on_start=None, on_completion=None, on_finalized=None,
                 execution_executor=None):
        for name, value in (("inference_limit", inference_limit),
                            ("execution_limit", execution_limit)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self._executor = executor
        self._execution_executor = execution_executor
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

    def _engine(self, entry):
        if entry.definition.resource == "execution" and self._execution_executor is not None:
            return self._execution_executor
        return self._executor

    def _run_state(self, run_id):
        return self._runs.setdefault(run_id, {
            "closed": False, "finalized": False, "cancelled": None, "cleaning": False,
            "stopped": set(), "finalized_event": threading.Event(),
        })

    def submit(self, definition: SchedulerTask, *, on_finalize=None, on_complete=None):
        task = definition.task
        with self._lock:
            run = self._run_state(task.run_id)
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
        # Ordinary failures preserve independent work. Only dependency terminality
        # gates dispatch; trusted STOP eligibility is handled separately.
        return all(self._entries[task_id].state in _TERMINAL_STATES for task_id in ancestors)

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
                handle = self._engine(entry).start(
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
                        "event_id": _event_id(task, "launch-failed"),
                        "event_type": "failed",
                        "identity": task.identity(),
                        "transport": self._engine(entry).transport,
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

    def _record_supervisor_failure(self, entry, error):
        if self._store is None:
            return
        task = entry.definition.task
        self._store.record_native_event({
            "schema": "native-review-event/v1",
            "event_id": _event_id(task, "failed"),
            "event_type": "failed",
            "identity": task.identity(),
            "transport": self._engine(entry).transport,
            "native_session_id": entry.handle.native_session_id if entry.handle else None,
            "timing": {
                "occurred_at_unix": self._clock(), "started_at_unix": None,
                "finished_at_unix": None, "duration_seconds": None,
            },
            "details": {"error": error, "publication": "rejected"},
        })

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
        output = None
        if completion.state == "completed" and artifact is not None and \
                not entry.definition.publish_artifact:
            output = artifact.payload.get("execution_result")
            artifact = None
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
                    artifact_path=entry.definition.task.artifact_path,
                )
            except Exception as exc:
                artifact = None
                stop = None
                state = "failed"
                error = f"native supervisor rejected completion: {exc}"
                try:
                    self._record_supervisor_failure(entry, error)
                except Exception as event_exc:
                    error += f"; native failure event failed: {event_exc}"
        with self._lock:
            entry.state = state
            entry.artifact = artifact
            entry.source_artifact = source_artifact
            entry.output = output
            entry.error = error
            self._active[entry.definition.resource] -= 1
        run_id = entry.definition.task.run_id
        with self._lock:
            run_cancelled = self._runs[run_id]["cancelled"] is not None
        if run_cancelled:
            self._record_cleanup(entry, self._runs[run_id]["cancelled"])
        if state == "cancelled":
            self.cancel_run(
                run_id, completion.details.get("reason") or "operator-cancelled",
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
        with self._lock:
            run = self._runs[run_id]
            if run["finalized"] or run["cleaning"]:
                return run["finalized"]
            if run["cancelled"] is None and not run["closed"]:
                return False
            entries = [entry for entry in self._entries.values()
                       if entry.definition.task.run_id == run_id]
            if any(entry.state not in _TERMINAL_STATES for entry in entries):
                return False
            for entry in entries:
                self._record_finalization(
                    entry,
                    run["cancelled"] if entry.state != "completed" else "run-closed",
                )
            run["finalized"] = True
            run["finalized_event"].set()
            for entry in entries:
                entry.finalized = True
                if run["cancelled"] is None and entry.eligible and \
                        entry.state == "completed" and entry.on_finalize is not None:
                    callbacks.append((entry.on_finalize, entry.result()))
        for callback, result in callbacks:
            callback(result)
        if self._on_finalized is not None:
            self._on_finalized(run_id)
        return True

    def _record_lifecycle(self, entry, suffix, event_type, reason):
        if self._store is None:
            return
        task = entry.definition.task
        self._store.register_native_task({
            "schema": "native-review-task/v1",
            "identity": task.identity(),
            "transport": self._engine(entry).transport,
        })
        occurred = self._clock()
        self._store.record_native_event({
            "schema": "native-review-event/v1",
            "event_id": _event_id(task, suffix),
            "event_type": event_type,
            "identity": task.identity(),
            "transport": self._engine(entry).transport,
            "native_session_id": (
                entry.handle.native_session_id if entry.handle else task.session_name
            ),
            "timing": {
                "occurred_at_unix": occurred, "started_at_unix": None,
                "finished_at_unix": None, "duration_seconds": None,
            },
            "details": {"reason": reason, "resource": entry.definition.resource},
        })

    def _record_fence(self, entry, reason):
        with self._lock:
            if entry.fence_recorded:
                return
            entry.fence_recorded = True
        event_type = "timeout" if "deadline" in str(reason) else "cancelled"
        self._record_lifecycle(entry, "fence", event_type, reason)

    def _record_cleanup(self, entry, reason):
        with self._lock:
            if entry.cleanup_recorded:
                return
            entry.cleanup_recorded = True
        self._record_lifecycle(entry, "cleanup", "cleanup", reason)

    def _record_finalization(self, entry, reason):
        self._record_lifecycle(
            entry, "finalization", "finalization", reason or "run-closed",
        )

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
            cancelled_entries = []
            for entry in entries:
                entry.eligible = False
                if entry.state in ("queued", "waiting", "running"):
                    cancelled_entries.append(entry)
                if entry.state in ("queued", "waiting"):
                    entry.state = "cancelled"
                    if entry in self._pending:
                        self._pending.remove(entry)
                elif entry.state == "running" and entry.handle is not None:
                    handles.append(entry.handle)
        for entry in cancelled_entries:
            self._record_fence(entry, reason)
        for handle in handles:
            handle.cancel()
        for entry in cancelled_entries:
            if entry.state in _TERMINAL_STATES:
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
            self._run_state(run_id)["closed"] = True
        self._drain()
        return self._maybe_finalize(run_id)

    def wait_finalized(self, run_id, timeout):
        """Block until the run's finalization events are recorded, or the bound elapses.

        A task's completion wakes its waiter before its worker thread records the
        run finalization, so a caller that tears down the store right after the
        waiter returns must wait here to avoid writing to a closed store.
        """
        with self._lock:
            event = self._run_state(run_id)["finalized_event"]
        return event.wait(timeout)

    def snapshot(self, run_id):
        with self._lock:
            run = self._run_state(run_id)
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
