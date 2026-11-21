"""Native top-level session execution contracts for asynchronous review work.

The executor owns completion callbacks and artifact fencing. Hosts own only the
launch and native cancellation mechanism; test doubles must use a transport
name that cannot be mistaken for production native execution.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Callable, Dict, Optional, Protocol, Tuple

from rg_final_env import scrub_environment
from rg_final_lenses import worker_summary_rejection
from rg_final_review import (
    _PIPE_CLOSE_TIMEOUT,
    _REAP_TIMEOUT,
    _close_owned_pipes,
    _kill_owned_process,
    _object_from_text,
    _sanitize_worker_diagnostic,
    WORKER_COMPLETION_STATUSES,
)


NATIVE_ARTIFACT_SCHEMA = "native-review-artifact/v1"
KIMCHI_NATIVE_TRANSPORT = "native-top-level/kimchi-rpc-v1"
_SESSION_BRIDGE_ENV = frozenset((
    "KIMCHI_PARENT_SESSION_ID", "KIMCHI_SESSION_ID", "KIMCHI_CHILD_SESSION",
    "KIMCHI_MESSAGING_SOCKET", "KIMCHI_MESSAGING_TOKEN", "PI_PACKAGE_DIR",
    "PI_ENTRYPOINT",
))


@dataclass(frozen=True)
class NativeTask:
    """Identity and input for one independent named inference session."""

    run_id: str
    task_id: str
    session_name: str
    unit_id: str
    stage: str
    lens: str
    revision_tuple: Tuple[str, ...]
    input_sha256: str
    backend: str
    model: str
    deadline_at_unix: float
    prompt: str
    repo_root: Path
    artifact_path: Path
    allow_tools: Tuple[str, ...] = ()
    deny_tools: Tuple[str, ...] = ()
    extensions: Tuple[str, ...] = ()
    runtime_deadline_at_unix: Optional[float] = None
    timeout_seconds: Optional[float] = None
    attempt: int = 1
    execute: Optional[Callable[[], Dict[str, object]]] = None

    def identity(self) -> Dict[str, object]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_name": self.session_name,
            "unit_id": self.unit_id,
            "stage": self.stage,
            "lens": self.lens,
            "revision_tuple": list(self.revision_tuple),
            "input_sha256": self.input_sha256,
            "backend": self.backend,
            "model": self.model,
            "deadline_at_unix": self.deadline_at_unix,
        }


@dataclass(frozen=True)
class NativeHostCompletion:
    """One terminal host notification, independent of transport encoding."""

    native_session_id: str
    return_code: int
    stdout: str
    started_at_unix: float
    finished_at_unix: float
    stderr: str = ""
    cleanup: Optional[Dict[str, object]] = None
    error: Optional[str] = None
    cancellation_reason: Optional[str] = None
    last_assistant_text: str = ""


class NativeHostSession(Protocol):
    native_session_id: str

    def cancel(self) -> None:
        """Cancel exactly this native session without naming other processes."""


class NativeHost(Protocol):
    transport: str

    def launch(self, task: NativeTask,
               callback: Callable[[NativeHostCompletion], None]) -> NativeHostSession:
        """Launch a top-level session and arrange exactly one terminal callback."""


@dataclass(frozen=True)
class NativeSessionHandle:
    """The supervisor-owned identity and cancellation handle for one task."""

    task: NativeTask
    session_name: str
    native_session_id: str
    transport: str
    _cancel: Callable[[], None] = field(compare=False, repr=False)

    def cancel(self) -> None:
        """Idempotently fence this task, then invoke its native cancellation handle."""
        self._cancel()


@dataclass(frozen=True)
class NativeArtifact:
    """A trusted, atomically published result envelope."""

    task: NativeTask
    transport: str
    native_session_id: str
    payload: Dict[str, object]
    started_at_unix: float
    finished_at_unix: float

    @property
    def duration_seconds(self) -> float:
        return self.finished_at_unix - self.started_at_unix

    def as_dict(self) -> Dict[str, object]:
        return {
            "schema": NATIVE_ARTIFACT_SCHEMA,
            "transport": self.transport,
            "native_session_id": self.native_session_id,
            "identity": self.task.identity(),
            "payload": self.payload,
            "timing": {
                "started_at_unix": self.started_at_unix,
                "finished_at_unix": self.finished_at_unix,
                "duration_seconds": self.duration_seconds,
            },
        }


@dataclass(frozen=True)
class NativeCompletion:
    """The sole executor-to-orchestrator completion notification."""

    handle: NativeSessionHandle
    state: str
    artifact: Optional[NativeArtifact]
    error: Optional[str] = None
    stdout: str = field(default="", repr=False)
    stderr: str = field(default="", repr=False)
    details: Dict[str, object] = field(default_factory=dict)


class NativeExecutor:
    """Own native session callbacks and uncommitted result envelopes."""

    def __init__(self, host: NativeHost, *, deadline=None, store=None,
                 result_parser=None, diagnostic_environ=None):
        self._host = host
        self._deadline = deadline
        self._store = store
        self._result_parser = result_parser or _object_from_text
        self._diagnostic_environ = os.environ if diagnostic_environ is None else diagnostic_environ

    def _diagnostics(self, host: NativeHostCompletion) -> Dict[str, object]:
        return {
            "return_code": host.return_code,
            "stdout_tail": _sanitize_worker_diagnostic(
                host.stdout, self._diagnostic_environ,
            ),
            "stderr_tail": _sanitize_worker_diagnostic(
                host.stderr, self._diagnostic_environ,
            ),
            "cleanup": host.cleanup,
            "last_assistant_tail": _sanitize_worker_diagnostic(
                host.last_assistant_text or host.stdout, self._diagnostic_environ,
            ),
        }

    @property
    def store(self):
        return self._store

    @property
    def transport(self):
        return self._host.transport

    def _record(self, task, event_type, occurred, *, native_session_id=None,
                started=None, finished=None, details=None):
        if self._store is None:
            return
        self._store.record_native_event({
            "schema": "native-review-event/v1",
            "event_id": (
                f"executor:{task.task_id}:{event_type}" if task.attempt == 1 else
                f"executor:attempt-{task.attempt}:{task.task_id}:{event_type}"
            ),
            "event_type": event_type,
            "identity": task.identity(),
            "transport": self._host.transport,
            "native_session_id": native_session_id,
            "timing": {
                "occurred_at_unix": occurred,
                "started_at_unix": started,
                "finished_at_unix": finished,
                "duration_seconds": (
                    finished - started if started is not None and finished is not None else None
                ),
            },
            "details": {} if details is None else details,
        })

    def _notify(self, completion, on_completion, *, started, finished, details=None):
        handle = completion.handle
        if completion.state == "completed":
            on_completion(completion)
            return
        reason = completion.details.get("reason", "")
        event_type = (
            "timeout" if completion.state == "cancelled" and "deadline" in str(reason)
            else completion.state
        )
        try:
            self._record(
                handle.task, event_type, finished,
                native_session_id=handle.native_session_id,
                started=started, finished=finished, details=details,
            )
            diagnostic = {
                key: completion.details[key]
                for key in (
                    "return_code", "stdout_tail", "stderr_tail", "cleanup",
                    "last_assistant_tail",
                )
                if key in completion.details
            }
            if any(diagnostic.get(key) for key in ("stdout_tail", "stderr_tail", "cleanup")):
                diagnostic["diagnostic"] = True
                self._record(
                    handle.task, "warning", finished,
                    native_session_id=handle.native_session_id,
                    started=started, finished=finished, details=diagnostic,
                )
        except Exception as exc:
            error = f"native completion event failed: {exc}"
            completion = NativeCompletion(
                handle=handle, state="failed", artifact=None, error=error,
                stdout=completion.stdout, stderr=completion.stderr,
                details={**completion.details, "event_error": error},
            )
        on_completion(completion)

    def start(self, task: NativeTask,
              on_completion: Callable[[NativeCompletion], None]) -> NativeSessionHandle:
        """Start without nesting or blocking; completion arrives on the host thread."""
        if self._deadline is not None:
            self._deadline.ensure_active(task.run_id)
        if self._store is not None:
            self._store.register_native_task({
                "schema": "native-review-task/v1",
                "identity": task.identity(),
                "transport": self._host.transport,
            })
        queued_details = {}
        if self._deadline is not None:
            queued_details = {
                "run_timeout_seconds": self._deadline.timeout_seconds,
                "watchdog": self._deadline.diagnostics()["watchdog"],
            }
        self._record(task, "queued", time.time(), details=queued_details)
        lock = threading.Lock()
        state = {"handle": None, "terminal": False, "cancelling": False}
        early = []

        def cancellation_details(host_completion):
            expired = self._deadline is not None and self._deadline.expired
            details = self._diagnostics(host_completion)
            details.update({
                "reason": host_completion.cancellation_reason or (
                    "run-deadline-exceeded" if expired else "operator-cancelled"
                ),
                "expiry_count": (
                    self._deadline.diagnostics()["expiry_count"]
                    if self._deadline is not None else 0
                ),
                "cancellation_status": "native-handle-signalled",
            })
            return details

        def host_completed(host_completion: NativeHostCompletion) -> None:
            with lock:
                handle = state["handle"]
                if handle is None:
                    early.append(host_completion)
                    return
                if state["terminal"]:
                    return
                state["terminal"] = True
                cancelling = (
                    state["cancelling"] or host_completion.cancellation_reason is not None
                )
            if self._deadline is not None:
                self._deadline.release(handle)
            if cancelling:
                details = cancellation_details(host_completion)
                lifecycle_details = {
                    key: details[key] for key in (
                        "reason", "expiry_count", "cancellation_status",
                    )
                }
                self._notify(
                    NativeCompletion(
                        handle=handle, state="cancelled", artifact=None,
                        stdout=host_completion.stdout, stderr=host_completion.stderr,
                        details=details,
                    ),
                    on_completion, started=started_at, finished=time.time(),
                    details=lifecycle_details,
                )
            else:
                self._complete(handle, host_completion, on_completion)

        def cancel() -> None:
            with lock:
                if state["terminal"] or state["cancelling"]:
                    return
                state["cancelling"] = True
            # Fence first; the host callback publishes cancellation only after its
            # owned process and managed pipes have finished bounded cleanup.
            session.cancel()

        try:
            session = self._host.launch(task, host_completed)
        except Exception as exc:
            started_at = time.time()
            session_id = f"unlaunched:{task.run_id}:{task.task_id}"
            handle = NativeSessionHandle(
                task=task, session_name=task.session_name, native_session_id=session_id,
                transport=self._host.transport, _cancel=lambda: None,
            )
            with lock:
                state["handle"] = handle
            if self._deadline is not None:
                self._deadline.own(task.run_id, handle)
            host_completed(NativeHostCompletion(
                native_session_id=session_id, return_code=1, stdout="",
                started_at_unix=started_at, finished_at_unix=time.time(),
                error=f"native session launch failed: {exc}",
            ))
            return handle
        started_at = time.time()
        self._record(
            task, "started", started_at, native_session_id=session.native_session_id,
            started=started_at,
            details=(
                {} if task.timeout_seconds is None
                else {"timeout_seconds": task.timeout_seconds}
            ),
        )
        handle = NativeSessionHandle(
            task=task,
            session_name=task.session_name,
            native_session_id=session.native_session_id,
            transport=self._host.transport,
            _cancel=cancel,
        )
        with lock:
            state["handle"] = handle
            pending = list(early)
            early.clear()
        if self._deadline is not None:
            self._deadline.own(task.run_id, handle)
        for host_completion in pending:
            host_completed(host_completion)
        return handle

    def _complete(self, handle: NativeSessionHandle, host: NativeHostCompletion,
                  on_completion: Callable[[NativeCompletion], None]) -> None:
        if host.error or host.return_code:
            error = host.error or f"native session exited {host.return_code}"
            self._notify(
                NativeCompletion(
                    handle=handle, state="failed", artifact=None, error=error,
                    stdout=host.stdout, stderr=host.stderr,
                    details=self._diagnostics(host),
                ),
                on_completion, started=host.started_at_unix,
                finished=host.finished_at_unix, details={"error": error},
            )
            return
        try:
            payload = self._result_parser(host.stdout)
        except Exception as exc:
            payload = None
            error = f"native result parsing failed: {exc}"
            self._notify(
                NativeCompletion(
                    handle=handle, state="failed", artifact=None, error=error,
                    stdout=host.stdout, stderr=host.stderr,
                    details=self._diagnostics(host),
                ),
                on_completion, started=host.started_at_unix,
                finished=host.finished_at_unix, details={"error": error},
            )
            return
        if payload is None:
            error = "native session returned no valid final-review worker result"
            self._notify(
                NativeCompletion(
                    handle=handle, state="failed", artifact=None, error=error,
                    stdout=host.stdout, stderr=host.stderr,
                    details=self._diagnostics(host),
                ),
                on_completion, started=host.started_at_unix,
                finished=host.finished_at_unix, details={"error": error},
            )
            return
        artifact = NativeArtifact(
            task=handle.task,
            transport=handle.transport,
            native_session_id=handle.native_session_id,
            payload=payload,
            started_at_unix=host.started_at_unix,
            finished_at_unix=host.finished_at_unix,
        )
        self._notify(
            NativeCompletion(
                handle=handle, state="completed", artifact=artifact,
                stdout=host.stdout, stderr=host.stderr,
            ),
            on_completion, started=host.started_at_unix,
            finished=host.finished_at_unix, details={"outcome": "completed"},
        )


def _top_level_environment(environ) -> Dict[str, str]:
    child = scrub_environment(environ, preserve_model_auth=True)
    for key in list(child):
        if key in _SESSION_BRIDGE_ENV or key.startswith("KIMCHI_PERMISSIONS"):
            child.pop(key, None)
    # Kimchi's own explicitly named inference credential is not a provider token.
    if isinstance(environ, dict) and isinstance(environ.get("KIMCHI_API_KEY"), str):
        child["KIMCHI_API_KEY"] = environ["KIMCHI_API_KEY"]
    child["KIMCHI_REMOTE_RUN"] = "0"
    child["KIMCHI_NO_UPDATE_CHECK"] = "1"
    return child


class _RpcSession:
    """One explicit task identity backed by the documented JSONL RPC protocol."""

    def __init__(self, task, process: subprocess.Popen, started_at_unix: float,
                 callback: Callable[[NativeHostCompletion], None], *, progress=None):
        self.task = task
        self.process = process
        self.started_at_unix = started_at_unix
        self.native_session_id = f"rpc:{task.run_id}:{task.task_id}"
        self._prompt_id = f"{task.run_id}:{task.task_id}:prompt"
        self._abort_id = f"{task.run_id}:{task.task_id}:abort"
        self._commands = {self._prompt_id: "prompt", self._abort_id: "abort"}
        self._responses = set()
        self._progress = progress
        self._callback = callback
        self._state = threading.RLock()
        self._write_lock = threading.Lock()
        self._cleanup_lock = threading.Lock()
        self._outcome = None
        self._error = None
        self._provider_error = None
        self._cancel_reason = None
        self._latest_result_valid = None
        self._latest_result_text = None
        self._stdout = ""
        self._stderr = []
        self._cleanup = None
        self._kill_timer = None
        self._worker_timer = None
        self._readers = []

    def start(self):
        self._readers = [
            threading.Thread(
                target=self._read_stdout, name=self.native_session_id + "-stdout", daemon=True,
            ),
            threading.Thread(
                target=self._read_stderr, name=self.native_session_id + "-stderr", daemon=True,
            ),
        ]
        for reader in self._readers:
            reader.start()
        watcher = threading.Thread(
            target=self._wait, name="native-rpc-" + self.task.task_id, daemon=True,
        )
        watcher.start()
        self._send(self._prompt_id, "prompt", {"message": self.task.prompt})
        runtime_deadline = (
            self.task.runtime_deadline_at_unix
            if self.task.runtime_deadline_at_unix is not None
            else (
                time.time() + self.task.timeout_seconds
                if self.task.timeout_seconds is not None
                else self.task.deadline_at_unix
            )
        )
        remaining = runtime_deadline - time.time()
        if remaining <= 0:
            self._cancel("worker-deadline-exceeded")
        else:
            self._worker_timer = threading.Timer(
                remaining, self._cancel, args=("worker-deadline-exceeded",),
            )
            self._worker_timer.daemon = True
            self._worker_timer.start()

    def _stream_progress(self, stream, chunks, byte_count):
        if self._progress is None:
            return
        try:
            self._progress({
                "event": "stream-progress",
                "native_session_id": self.native_session_id,
                "stream": stream,
                "chunks": chunks,
                "bytes": byte_count,
            })
        except Exception:
            pass

    def _read_stdout(self):
        buffer = b""
        chunks = 0
        byte_count = 0
        try:
            while True:
                chunk = os.read(self.process.stdout.fileno(), 8192)
                if not chunk:
                    break
                chunks += 1
                byte_count += len(chunk)
                self._stream_progress("stdout", chunks, byte_count)
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.endswith(b"\r"):
                        line = line[:-1]
                    try:
                        record = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        self._fail(f"native RPC malformed JSON: {exc}")
                        continue
                    self._handle_record(record)
            if buffer:
                self._fail("native RPC protocol error: unterminated JSONL record")
        except (OSError, ValueError) as exc:
            with self._state:
                active = self.process.poll() is None and self._outcome is None
            if active:
                self._fail(f"native RPC stdout failed: {type(exc).__name__}: {exc}")

    def _read_stderr(self):
        chunks = 0
        byte_count = 0
        try:
            while True:
                chunk = os.read(self.process.stderr.fileno(), 8192)
                if not chunk:
                    break
                chunks += 1
                byte_count += len(chunk)
                self._stderr.append(chunk.decode("utf-8", "replace"))
                self._stream_progress("stderr", chunks, byte_count)
        except (OSError, ValueError):
            pass

    def _handle_record(self, record):
        if not isinstance(record, dict) or not isinstance(record.get("type"), str):
            self._fail("native RPC protocol error: record must be an object with a type")
            return
        record_type = record["type"]
        if record_type == "response":
            self._handle_response(record)
        elif record_type == "message_end":
            message = record.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                return
            content = message.get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "".join(
                    str(block.get("text", "")) for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            else:
                self._fail("native RPC protocol error: assistant content must be text or blocks")
                return
            reason = message.get("stopReason")
            with self._state:
                self._stdout = text
                cancelling = self._cancel_reason is not None
            candidate = None
            if reason not in ("error", "aborted"):
                candidate = _object_from_text(text)
            if candidate is not None:
                with self._state:
                    self._latest_result_text = text
                    self._latest_result_valid = self._valid_worker_result(candidate)
                    if self._latest_result_valid:
                        self._provider_error = None
            if reason in ("error", "aborted") and not cancelling:
                detail = message.get("errorMessage") or "provider supplied no error detail"
                self._record_provider_error(reason, detail)
        elif record_type == "message_update":
            update = record.get("assistantMessageEvent")
            if isinstance(update, dict) and update.get("type") == "error":
                detail = update.get("error") or update.get("reason") or "provider failure"
                self._record_provider_error("error", detail)
        elif record_type == "agent_settled":
            self._settle(bool(record.get("aborted", False)))

    @staticmethod
    def _valid_worker_result(result):
        return isinstance(result, dict) and (
            result.get("status") in WORKER_COMPLETION_STATUSES
            and worker_summary_rejection(result) in ("", None)
            and all(
                isinstance(result.get(key), list)
                for key in ("findings", "warnings")
            )
            and isinstance(result.get("unreviewed", []), list)
        )

    def _record_provider_error(self, reason, detail):
        with self._state:
            if self._cancel_reason is None:
                self._provider_error = (
                    f"native RPC provider {reason}: {str(detail)[:8192]}"
                )

    def _handle_response(self, record):
        request_id = record.get("id")
        command = record.get("command")
        expected = self._commands.get(request_id)
        if expected is None or command != expected or request_id in self._responses:
            self._fail(
                "native RPC protocol error: uncorrelated response "
                f"id={request_id!r} command={command!r}"
            )
            return
        self._responses.add(request_id)
        if not record.get("success"):
            detail = record.get("error") or "command supplied no error detail"
            if command == "abort":
                self._force_cleanup()
            else:
                self._fail(f"native RPC prompt failed: {detail}")
            return
        if command == "prompt":
            data = record.get("data")
            disposition = data.get("disposition") if isinstance(data, dict) else None
            if disposition == "handled":
                self._fail("native RPC prompt was handled without starting agent work")
            elif disposition not in ("started", "queued"):
                self._fail(f"native RPC protocol error: invalid prompt disposition {disposition!r}")

    def _send(self, request_id, command, values=None):
        record = {"id": request_id, "type": command}
        record.update(values or {})
        try:
            encoded = json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"
            with self._write_lock:
                self.process.stdin.write(encoded)
                self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            self._fail(f"native RPC stdin write failed: {type(exc).__name__}: {exc}")

    def _close_stdin(self):
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass

    def _settle(self, aborted):
        with self._state:
            if self._outcome is not None:
                return
            if self._cancel_reason is not None:
                self._outcome = "cancelled"
            elif aborted or self._provider_error:
                self._outcome = "failed"
                self._error = self._provider_error or (
                    "native RPC agent aborted before a successful settlement"
                )
            elif self._latest_result_valid is False:
                self._outcome = "failed"
                self._error = "native RPC newest structured worker result is invalid"
            else:
                self._outcome = "completed"
        self._close_stdin()
        self._schedule_cleanup()

    def _fail(self, error):
        with self._state:
            if self._outcome is not None:
                return
            self._outcome = "cancelled" if self._cancel_reason is not None else "failed"
            self._error = error
        self._close_stdin()
        self._force_cleanup()

    def _schedule_cleanup(self):
        with self._cleanup_lock:
            if self._kill_timer is not None:
                return
            self._kill_timer = threading.Timer(_REAP_TIMEOUT, self._force_cleanup)
            self._kill_timer.daemon = True
            self._kill_timer.start()

    def _force_cleanup(self):
        self._close_stdin()
        with self._cleanup_lock:
            if self._cleanup is None:
                self._cleanup = _kill_owned_process(self.process)

    def cancel(self) -> None:
        self._cancel("operator-cancelled")

    def _cancel(self, reason):
        with self._state:
            if self._outcome is not None or self._cancel_reason is not None:
                return
            self._cancel_reason = reason
        self._send(self._abort_id, "abort")
        self._schedule_cleanup()

    def _wait(self):
        self.process.wait()
        # Reaping the RPC parent is not enough: tool descendants may retain both
        # process-group membership and the managed pipes after the parent exits.
        self._force_cleanup()
        drain_deadline = time.monotonic() + _REAP_TIMEOUT
        for reader in self._readers:
            reader.join(max(0.0, drain_deadline - time.monotonic()))
        if any(reader.is_alive() for reader in self._readers):
            _close_owned_pipes(self.process)
            close_deadline = time.monotonic() + _PIPE_CLOSE_TIMEOUT
            for reader in self._readers:
                reader.join(max(0.0, close_deadline - time.monotonic()))
        with self._state:
            outcome = self._outcome
            if outcome is None:
                outcome = "cancelled" if self._cancel_reason is not None else "failed"
                self._error = self._provider_error or (
                    f"native RPC process exited {self.process.returncode} before agent_settled"
                )
            if outcome == "completed" and self.process.returncode:
                outcome = "failed"
                self._error = f"native RPC process exited {self.process.returncode} after agent_settled"
            result_text = self._latest_result_text or self._stdout
            if outcome == "completed" and not result_text.strip():
                outcome = "failed"
                self._error = "native RPC agent_settled without a final assistant message"
            error = self._error
            reason = self._cancel_reason
            stdout = result_text
            last_assistant_text = self._stdout
        for timer in (self._worker_timer, self._kill_timer):
            if timer is not None:
                timer.cancel()
        pipes_done = all(not reader.is_alive() for reader in self._readers)
        cleanup = self._cleanup or {
            "status": "complete",
            "termination_method": "stdin-eof",
            "direct_child_reaped": True,
            "tree_termination_verified": True,
            "pipe_drain_completed": pipes_done,
            "errors": [],
        }
        cleanup.update({
            "process_pid": self.process.pid,
            "direct_child_reaped": self.process.returncode is not None,
            "pipe_drain_completed": pipes_done,
        })
        cleanup["complete"] = bool(
            cleanup["direct_child_reaped"] and cleanup.get("tree_termination_verified")
            and pipes_done
        )
        cleanup["status"] = "complete" if cleanup["complete"] else "incomplete"
        if outcome == "failed":
            return_code = self.process.returncode
            if not return_code:
                return_code = 1
        else:
            return_code = self.process.returncode
        self._callback(NativeHostCompletion(
            native_session_id=self.native_session_id,
            return_code=return_code,
            stdout=stdout,
            stderr="".join(self._stderr),
            started_at_unix=self.started_at_unix,
            finished_at_unix=time.time(),
            cleanup=cleanup,
            error=error,
            cancellation_reason=reason if outcome == "cancelled" else None,
            last_assistant_text=last_assistant_text,
        ))


@dataclass(frozen=True)
class NativeCapability:
    """A bounded host-check result, never evidence of live model acceptance."""

    supported: bool
    transport: str
    executable: Optional[str]
    reason: str
    inference_tested: bool = False


class NativeCapabilityUnavailable(RuntimeError):
    """The explicit native adapter is unavailable; no fallback is selected."""

    def __init__(self, capability: NativeCapability):
        super().__init__(capability.reason)
        self.capability = capability


def probe_kimchi_native_capability(executable=None, *, environ=None,
                                   timeout=3.0) -> NativeCapability:
    """Check required direct CLI flags using help only, never an inference call."""
    source = os.environ if environ is None else environ
    selected = executable or shutil.which("kimchi", path=source.get("PATH"))
    if not selected:
        return NativeCapability(
            False, KIMCHI_NATIVE_TRANSPORT, None,
            "kimchi executable not found; native execution has no automatic fallback",
        )
    bounded_timeout = min(5.0, max(0.1, float(timeout)))
    try:
        completed = subprocess.run(
            [str(selected), "--help"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", check=False,
            env=_top_level_environment(source), timeout=bounded_timeout,
        )
    except subprocess.TimeoutExpired:
        return NativeCapability(
            False, KIMCHI_NATIVE_TRANSPORT, str(selected),
            f"kimchi --help timed out after {bounded_timeout:g}s",
        )
    except OSError as exc:
        return NativeCapability(
            False, KIMCHI_NATIVE_TRANSPORT, str(selected),
            f"kimchi --help could not run: {type(exc).__name__}: {exc}",
        )
    if completed.returncode:
        return NativeCapability(
            False, KIMCHI_NATIVE_TRANSPORT, str(selected),
            f"kimchi --help exited {completed.returncode}",
        )
    help_text = completed.stdout + "\n" + completed.stderr
    required = (
        "--mode", "rpc", "--no-session", "--no-approve", "--no-auto-update",
        "--model", "--mcp-config", "--allow-tool", "--deny-tool", "--plan",
    )
    missing = [flag for flag in required if flag not in help_text]
    if missing:
        return NativeCapability(
            False, KIMCHI_NATIVE_TRANSPORT, str(selected),
            "kimchi lacks required direct-session flags: " + ", ".join(missing),
        )
    return NativeCapability(
        True, KIMCHI_NATIVE_TRANSPORT, str(selected),
        "required RPC CLI flags present; help-only check, inference not exercised",
    )


class KimchiNativeHost:
    """Launch top-level ``kimchi --mode rpc --no-session`` task sessions."""

    transport = KIMCHI_NATIVE_TRANSPORT

    def __init__(self, executable: str, *, environ, progress=None):
        self._executable = executable
        self._environ = _top_level_environment(environ)
        self._progress = progress

    @classmethod
    def checked(cls, executable=None, *, environ=None, timeout=3.0, progress=None):
        """Return an explicitly supported host or raise without fallback."""
        capability = probe_kimchi_native_capability(
            executable, environ=environ, timeout=timeout,
        )
        if not capability.supported:
            raise NativeCapabilityUnavailable(capability)
        return cls(
            capability.executable,
            environ=os.environ if environ is None else environ,
            progress=progress,
        )

    def launch(self, task: NativeTask,
               callback: Callable[[NativeHostCompletion], None]) -> _RpcSession:
        command = [
            self._executable, "--mode", "rpc", "--no-session", "--no-approve",
            "--no-auto-update", "--mcp-config", '{"mcpServers":{}}', "--plan",
        ]
        if task.model and task.model != "auto":
            command.extend(("--model", task.model))
        for rule in task.allow_tools:
            command.extend(("--allow-tool", rule))
        for rule in task.deny_tools:
            command.extend(("--deny-tool", rule))
        for extension in task.extensions:
            command.extend(("-e", extension))
        environ = dict(self._environ)
        environ["KIMCHI_TAGS"] = (
            f"review-run:{task.run_id},review-task:{task.task_id},"
            f"review-session:{task.session_name}"
        )
        options = {"start_new_session": True} if os.name == "posix" else {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        started = time.time()
        process = subprocess.Popen(
            command, cwd=str(task.repo_root), env=environ, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options,
        )
        session = _RpcSession(
            task, process, started, callback, progress=self._progress,
        )
        session.start()
        return session
