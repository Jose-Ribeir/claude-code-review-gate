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
import signal
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Callable, Dict, Optional, Protocol, Tuple

from rg_final_env import scrub_environment
from rg_final_review import _object_from_text


NATIVE_ARTIFACT_SCHEMA = "native-review-artifact/v1"
KIMCHI_NATIVE_TRANSPORT = "native-top-level/kimchi-cli-no-session-v1"
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


def _atomic_write(path: Path, value: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp-", dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class NativeExecutor:
    """Own native session callbacks, result envelopes, and atomic artifacts."""

    def __init__(self, host: NativeHost, *, deadline=None, store=None):
        self._host = host
        self._deadline = deadline
        self._store = store

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
            "event_id": f"executor:{task.task_id}:{event_type}",
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
        self._record(
            handle.task, completion.state, finished,
            native_session_id=handle.native_session_id,
            started=started, finished=finished, details=details,
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
        state = {"handle": None, "terminal": False}
        early = []

        def host_completed(host_completion: NativeHostCompletion) -> None:
            with lock:
                handle = state["handle"]
                if handle is None:
                    early.append(host_completion)
                    return
                if state["terminal"]:
                    return
                state["terminal"] = True
            if self._deadline is not None:
                self._deadline.release(handle)
            self._complete(handle, host_completion, on_completion)

        def cancel() -> None:
            with lock:
                if state["terminal"]:
                    return
                state["terminal"] = True
                handle = state["handle"]
            if self._deadline is not None:
                self._deadline.release(handle)
            # Cancellation intent is fenced before the native callback can race us.
            session.cancel()
            expired = self._deadline is not None and self._deadline.expired
            details = {
                "reason": "run-deadline-exceeded" if expired else "operator-cancelled",
                "expiry_count": (
                    self._deadline.diagnostics()["expiry_count"]
                    if self._deadline is not None else 0
                ),
                "cancellation_status": "native-handle-signalled",
            }
            self._notify(
                NativeCompletion(handle=handle, state="cancelled", artifact=None),
                on_completion, started=started_at, finished=time.time(), details=details,
            )

        session = self._host.launch(task, host_completed)
        started_at = time.time()
        self._record(
            task, "started", started_at, native_session_id=session.native_session_id,
            started=started_at,
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
            if state["terminal"]:
                pending.clear()
            elif pending:
                state["terminal"] = True
        if self._deadline is not None:
            self._deadline.own(task.run_id, handle)
        for host_completion in pending:
            if self._deadline is not None:
                self._deadline.release(handle)
            self._complete(handle, host_completion, on_completion)
        return handle

    def _complete(self, handle: NativeSessionHandle, host: NativeHostCompletion,
                  on_completion: Callable[[NativeCompletion], None]) -> None:
        if host.return_code:
            error = f"native session exited {host.return_code}"
            self._notify(
                NativeCompletion(
                    handle=handle, state="failed", artifact=None, error=error,
                ),
                on_completion, started=host.started_at_unix,
                finished=host.finished_at_unix, details={"error": error},
            )
            return
        payload = _object_from_text(host.stdout)
        if payload is None:
            error = "native session returned no valid final-review worker result"
            self._notify(
                NativeCompletion(
                    handle=handle, state="failed", artifact=None, error=error,
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
        _atomic_write(handle.task.artifact_path, artifact.as_dict())
        self._notify(
            NativeCompletion(handle=handle, state="completed", artifact=artifact),
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


class _ProcessSession:
    """A direct OS process used as the CLI's native cancellation handle."""

    def __init__(self, process: subprocess.Popen, started_at_unix: float):
        self.process = process
        self.started_at_unix = started_at_unix
        self.native_session_id = "pid:" + str(process.pid)

    def cancel(self) -> None:
        if self.process.poll() is not None:
            return
        if os.name == "posix" and self.process.pid != os.getpgrp():
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
                return
            except ProcessLookupError:
                return
        self.process.kill()

    def wait(self, callback: Callable[[NativeHostCompletion], None]) -> None:
        stdout, _stderr = self.process.communicate()
        callback(NativeHostCompletion(
            native_session_id=self.native_session_id,
            return_code=self.process.returncode,
            stdout=stdout,
            started_at_unix=self.started_at_unix,
            finished_at_unix=time.time(),
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
        "-p", "--mode", "--no-session", "--no-approve", "--no-auto-update",
        "--model", "--mcp-config", "--allow-tool", "--deny-tool",
    )
    missing = [flag for flag in required if flag not in help_text]
    if missing:
        return NativeCapability(
            False, KIMCHI_NATIVE_TRANSPORT, str(selected),
            "kimchi lacks required direct-session flags: " + ", ".join(missing),
        )
    return NativeCapability(
        True, KIMCHI_NATIVE_TRANSPORT, str(selected),
        "required direct CLI flags present; help-only check, inference not exercised",
    )


class KimchiNativeHost:
    """Launch direct ``kimchi -p --no-session`` top-level native sessions.

    No Agent tool or nested harness session is involved. Required repository and
    test executables remain ordinary descendants of the direct session process.
    """

    transport = KIMCHI_NATIVE_TRANSPORT

    def __init__(self, executable: str, *, environ):
        self._executable = executable
        self._environ = _top_level_environment(environ)

    @classmethod
    def checked(cls, executable=None, *, environ=None, timeout=3.0):
        """Return an explicitly supported host or raise without fallback."""
        capability = probe_kimchi_native_capability(
            executable, environ=environ, timeout=timeout,
        )
        if not capability.supported:
            raise NativeCapabilityUnavailable(capability)
        return cls(capability.executable, environ=os.environ if environ is None else environ)

    def launch(self, task: NativeTask,
               callback: Callable[[NativeHostCompletion], None]) -> _ProcessSession:
        command = [
            self._executable, "-p", task.prompt,
            "--mode", "json", "--no-session", "--no-approve", "--no-auto-update",
            "--model", task.model, "--mcp-config", '{"mcpServers":{}}',
        ]
        for rule in task.allow_tools:
            command.extend(("--allow-tool", rule))
        for rule in task.deny_tools:
            command.extend(("--deny-tool", rule))
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
            command, cwd=str(task.repo_root), env=environ, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", **options,
        )
        session = _ProcessSession(process, started)
        watcher = threading.Thread(
            target=session.wait, args=(callback,),
            name="native-session-" + task.task_id, daemon=True,
        )
        watcher.start()
        return session
