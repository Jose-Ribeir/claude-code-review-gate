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
import tempfile
import threading
import time
from typing import Callable, Dict, Optional, Protocol, Tuple

from rg_final_env import scrub_environment
from rg_final_review import (
    _PIPE_CLOSE_TIMEOUT,
    _REAP_TIMEOUT,
    _close_owned_pipes,
    _kill_owned_process,
    _object_from_text,
    _sanitize_worker_diagnostic,
)


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
    extensions: Tuple[str, ...] = ()

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
        state = {"handle": None, "terminal": False, "cancelling": False}
        early = []

        def cancellation_details(host_completion):
            expired = self._deadline is not None and self._deadline.expired
            details = self._diagnostics(host_completion)
            details.update({
                "reason": "run-deadline-exceeded" if expired else "operator-cancelled",
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
                cancelling = state["cancelling"]
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
                    stdout=host.stdout, stderr=host.stderr,
                    details=self._diagnostics(host),
                ),
                on_completion, started=host.started_at_unix,
                finished=host.finished_at_unix, details={"error": error},
            )
            return
        payload = self._result_parser(host.stdout)
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
        _atomic_write(handle.task.artifact_path, artifact.as_dict())
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


class _ProcessSession:
    """A direct OS process used as the CLI's native cancellation handle."""

    def __init__(self, process: subprocess.Popen, started_at_unix: float, progress=None):
        self.process = process
        self.started_at_unix = started_at_unix
        self.native_session_id = "pid:" + str(process.pid)
        self._progress = progress
        self._cleanup = None
        self._cleanup_lock = threading.Lock()

    def cancel(self) -> None:
        # A direct parent may already be reaped while its descendants still own
        # the managed pipes; group cleanup is still required in that state.
        with self._cleanup_lock:
            if self._cleanup is not None:
                return
            self._cleanup = _kill_owned_process(self.process)

    def _drain(self, name, stream, parts):
        chunks = 0
        byte_count = 0
        try:
            while True:
                chunk = stream.readline()
                if not chunk:
                    break
                parts.append(chunk)
                chunks += 1
                byte_count += len(chunk.encode("utf-8", "replace"))
                if self._progress is not None:
                    try:
                        self._progress({
                            "event": "stream-progress",
                            "native_session_id": self.native_session_id,
                            "stream": name,
                            "chunks": chunks,
                            "bytes": byte_count,
                        })
                    except Exception:
                        pass
        except (OSError, ValueError):
            # A bounded timeout may close an inherited pipe that a surviving
            # descendant kept open. The captured prefix remains diagnostic data.
            pass

    def wait(self, callback: Callable[[NativeHostCompletion], None]) -> None:
        output = {"stdout": [], "stderr": []}
        readers = [
            threading.Thread(
                target=self._drain, args=(name, stream, output[name]),
                name=f"{self.native_session_id}-{name}", daemon=True,
            )
            for name, stream in (("stdout", self.process.stdout),
                                 ("stderr", self.process.stderr))
        ]
        for reader in readers:
            reader.start()
        self.process.wait()
        drain_deadline = time.monotonic() + _REAP_TIMEOUT
        for reader in readers:
            reader.join(max(0.0, drain_deadline - time.monotonic()))
        abandoned = [reader for reader in readers if reader.is_alive()]
        with self._cleanup_lock:
            cleanup = self._cleanup
            if abandoned and cleanup is not None:
                # An escaped descendant can retain a duplicated pipe after the
                # model's process group dies. Close only our managed endpoints,
                # then give the readers one final bounded opportunity to exit.
                cleanup["errors"].append("pipe-drain-timeout")
                _close_owned_pipes(self.process)
                close_deadline = time.monotonic() + _PIPE_CLOSE_TIMEOUT
                for reader in abandoned:
                    reader.join(max(0.0, close_deadline - time.monotonic()))
            if cleanup is not None:
                cleanup["direct_child_reaped"] = self.process.returncode is not None
                cleanup["pipe_drain_completed"] = (
                    not abandoned and not any(reader.is_alive() for reader in readers)
                )
                if not cleanup["direct_child_reaped"] or not cleanup["pipe_drain_completed"]:
                    cleanup["tree_termination_verified"] = False
                cleanup["complete"] = bool(
                    cleanup["direct_child_reaped"] and cleanup["tree_termination_verified"]
                    and cleanup["pipe_drain_completed"]
                )
                cleanup["status"] = "complete" if cleanup["complete"] else "incomplete"
        callback(NativeHostCompletion(
            native_session_id=self.native_session_id,
            return_code=self.process.returncode,
            stdout="".join(output["stdout"]),
            stderr="".join(output["stderr"]),
            started_at_unix=self.started_at_unix,
            finished_at_unix=time.time(),
            cleanup=cleanup,
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
            command, cwd=str(task.repo_root), env=environ, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", **options,
        )
        session = _ProcessSession(process, started, progress=self._progress)
        watcher = threading.Thread(
            target=session.wait, args=(callback,),
            name="native-session-" + task.task_id, daemon=True,
        )
        watcher.start()
        return session
