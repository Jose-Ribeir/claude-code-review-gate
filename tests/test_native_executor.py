"""Ticket #14: native top-level session executor contract."""
from dataclasses import replace
import json
import os
import sys
import threading

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
from rg_native_executor import (  # noqa: E402
    KimchiNativeHost,
    NativeCapabilityUnavailable,
    NativeExecutor,
    NativeHostCompletion,
    NativeTask,
    probe_kimchi_native_capability,
)
from rg_run_lifecycle import RunDeadline, RunDeadlineExpired  # noqa: E402


class _DeadlineTimer:
    def __init__(self):
        self.callback = None

    def __call__(self, _delay, callback):
        self.callback = callback
        return self

    def start(self):
        pass

    def cancel(self):
        pass

    def fire(self):
        self.callback()


class _FakeSession:
    def __init__(self, task, callback):
        self.native_session_id = "fake-native-" + task.task_id
        self._callback = callback
        self.cancel_calls = 0

    def cancel(self):
        self.cancel_calls += 1
        self._callback(NativeHostCompletion(
            native_session_id=self.native_session_id,
            return_code=-9,
            stdout="",
            started_at_unix=100.0,
            finished_at_unix=101.0,
        ))

    def emit(self, stdout, return_code):
        self._callback(NativeHostCompletion(
            native_session_id=self.native_session_id,
            return_code=return_code,
            stdout=stdout,
            started_at_unix=100.0,
            finished_at_unix=100.25,
        ))

    def finish(self, payload, *, enveloped=False):
        stdout = json.dumps({"result": json.dumps(payload)} if enveloped else payload)
        self.emit(stdout, 0)


class _FakeHost:
    transport = "test-double/native-host-v1"

    def __init__(self):
        self.sessions = {}

    def launch(self, task, callback):
        session = _FakeSession(task, callback)
        self.sessions[task.task_id] = session
        return session


def _task(tmp_path, task_id="task-a"):
    return NativeTask(
        run_id="run-1",
        task_id=task_id,
        session_name="review-run-1-" + task_id,
        unit_id="unit-1",
        stage="individual",
        lens="correctness",
        revision_tuple=("base-sha", "source-sha", "target-sha"),
        input_sha256="a" * 64,
        backend="kimchi",
        model="kimi-k3.1-pro",
        deadline_at_unix=200.0,
        prompt="Return the contracted result.",
        repo_root=tmp_path,
        artifact_path=tmp_path / (task_id + ".json"),
    )


def test_completion_handle_and_atomic_artifact_preserve_native_identity(tmp_path):
    host = _FakeHost()
    executor = NativeExecutor(host)
    task = _task(tmp_path)
    completions = []

    handle = executor.start(task, completions.append)
    assert handle.task == task
    assert handle.session_name == task.session_name
    assert handle.native_session_id == "fake-native-task-a"
    assert handle.transport == "test-double/native-host-v1"
    assert not task.artifact_path.exists()

    payload = {
        "schema": "final-review-worker-result/v1",
        "status": "success",
        "summary": "Inspected app.py.",
        "findings": [],
        "warnings": [],
    }
    host.sessions[task.task_id].finish(payload)

    assert len(completions) == 1
    completion = completions[0]
    assert completion.handle == handle
    assert completion.state == "completed"
    assert completion.artifact.task == task
    assert completion.artifact.native_session_id == handle.native_session_id
    assert completion.artifact.duration_seconds == pytest.approx(0.25)
    serialized = json.loads(task.artifact_path.read_text(encoding="utf-8"))
    assert serialized == completion.artifact.as_dict()
    assert serialized["schema"] == "native-review-artifact/v1"
    assert serialized["transport"] == "test-double/native-host-v1"
    assert serialized["identity"] == {
        "run_id": "run-1",
        "task_id": "task-a",
        "session_name": "review-run-1-task-a",
        "unit_id": "unit-1",
        "stage": "individual",
        "lens": "correctness",
        "revision_tuple": ["base-sha", "source-sha", "target-sha"],
        "input_sha256": "a" * 64,
        "backend": "kimchi",
        "model": "kimi-k3.1-pro",
        "deadline_at_unix": 200.0,
    }
    assert serialized["payload"] == payload
    assert serialized["timing"] == {
        "started_at_unix": 100.0,
        "finished_at_unix": 100.25,
        "duration_seconds": pytest.approx(0.25),
    }
    assert list(tmp_path.glob("*.tmp-*")) == []


def test_json_result_envelope_preserves_the_contracted_worker_payload(tmp_path):
    host = _FakeHost()
    executor = NativeExecutor(host)
    task = _task(tmp_path)
    completions = []
    executor.start(task, completions.append)
    payload = {"schema": "final-review-worker-result/v1", "status": "success"}

    host.sessions[task.task_id].finish(payload, enveloped=True)

    assert completions[0].state == "completed"
    assert completions[0].artifact.payload == payload
    assert json.loads(task.artifact_path.read_text())["payload"] == payload


def test_two_task_spike_cancels_only_one_and_fences_late_output(tmp_path):
    host = _FakeHost()
    executor = NativeExecutor(host)
    first_task = _task(tmp_path, "task-a")
    second_task = _task(tmp_path, "task-b")
    completions = []

    first_handle = executor.start(first_task, completions.append)
    second_handle = executor.start(second_task, completions.append)

    assert set(host.sessions) == {"task-a", "task-b"}
    assert first_handle.native_session_id != second_handle.native_session_id
    assert host.sessions["task-a"].cancel_calls == 0

    second_handle.cancel()

    assert host.sessions["task-b"].cancel_calls == 1
    assert host.sessions["task-a"].cancel_calls == 0
    assert len(completions) == 1
    cancelled = completions[0]
    assert cancelled.handle == second_handle
    assert cancelled.state == "cancelled"
    assert cancelled.artifact is None
    assert not second_task.artifact_path.exists()

    host.sessions["task-b"].finish({"status": "success", "late": True})
    assert len(completions) == 1
    assert not second_task.artifact_path.exists()

    host.sessions["task-a"].finish({
        "schema": "final-review-worker-result/v1", "status": "success", "task": "task-a",
    })
    assert len(completions) == 2
    assert completions[-1].handle == first_handle
    assert completions[-1].state == "completed"
    assert json.loads(first_task.artifact_path.read_text())["payload"] == {
        "schema": "final-review-worker-result/v1", "status": "success", "task": "task-a",
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX direct-process identity")
def test_direct_kimchi_adapter_is_top_level_and_restricts_supplied_environment(tmp_path):
    capture = tmp_path / "launch.json"
    executable = tmp_path / "direct-kimchi-double"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        f"pathlib.Path({str(capture)!r}).write_text(json.dumps({{"
        "'argv': sys.argv[1:], 'env': dict(os.environ), 'pid': os.getpid(),"
        " 'parent_pid': os.getppid(), 'process_group': os.getsid(0)}))\n"
        "print(json.dumps({'schema': 'final-review-worker-result/v1',"
        " 'status': 'success', 'summary': 'direct double', 'findings': [], 'warnings': []}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    host = KimchiNativeHost(str(executable), environ={
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "OPENAI_API_KEY": "model-auth-only",
        "GITHUB_TOKEN": "provider-write-must-not-cross",
        "UNRELATED_SECRET": "must-not-cross",
        "KIMCHI_PARENT_SESSION_ID": "interactive-parent",
        "KIMCHI_SESSION_ID": "interactive-session",
        "KIMCHI_CHILD_SESSION": "1",
        "KIMCHI_MESSAGING_SOCKET": "interactive-socket",
        "KIMCHI_PERMISSIONS_MODE": "yolo",
        "ALLOWED_CONTEXT": "preserved",
    })
    executor = NativeExecutor(host)
    task = replace(
        _task(tmp_path),
        allow_tools=("Read", "Bash(git diff *)"),
        deny_tools=("Write", "Bash(gh *)"),
    )
    completions = []
    delivered = threading.Event()

    def completed(completion):
        completions.append(completion)
        delivered.set()

    handle = executor.start(task, completed)
    assert delivered.wait(5), "direct host did not deliver its completion callback"

    launched = json.loads(capture.read_text(encoding="utf-8"))
    assert launched["parent_pid"] == os.getpid()
    assert launched["process_group"] == launched["pid"]
    assert handle.native_session_id == "pid:" + str(launched["pid"])
    assert handle.transport == "native-top-level/kimchi-cli-no-session-v1"
    assert launched["argv"] == [
        "-p", task.prompt,
        "--mode", "json",
        "--no-session",
        "--no-approve",
        "--no-auto-update",
        "--model", task.model,
        "--mcp-config", '{"mcpServers":{}}',
        "--allow-tool", "Read",
        "--allow-tool", "Bash(git diff *)",
        "--deny-tool", "Write",
        "--deny-tool", "Bash(gh *)",
    ]
    environment = launched["env"]
    assert environment["OPENAI_API_KEY"] == "model-auth-only"
    assert environment["ALLOWED_CONTEXT"] == "preserved"
    assert environment["KIMCHI_REMOTE_RUN"] == "0"
    assert environment["KIMCHI_NO_UPDATE_CHECK"] == "1"
    assert environment["KIMCHI_TAGS"] == (
        "review-run:run-1,review-task:task-a,review-session:review-run-1-task-a"
    )
    for name in (
        "GITHUB_TOKEN", "UNRELATED_SECRET", "KIMCHI_PARENT_SESSION_ID",
        "KIMCHI_SESSION_ID", "KIMCHI_CHILD_SESSION", "KIMCHI_MESSAGING_SOCKET",
        "KIMCHI_PERMISSIONS_MODE",
    ):
        assert name not in environment
    assert len(completions) == 1 and completions[0].state == "completed"


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable double")
def test_kimchi_capability_probe_is_bounded_and_never_falls_back(tmp_path):
    required_help = (
        "-p --mode --no-session --no-approve --no-auto-update --model "
        "--mcp-config --allow-tool --deny-tool"
    )
    supported = tmp_path / "supported-kimchi"
    supported.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "assert sys.argv[1:] == ['--help']\n"
        f"print({required_help!r})\n",
        encoding="utf-8",
    )
    supported.chmod(0o755)

    capability = probe_kimchi_native_capability(str(supported), environ={}, timeout=2.0)
    assert capability.supported
    assert capability.transport == "native-top-level/kimchi-cli-no-session-v1"
    assert capability.inference_tested is False
    assert isinstance(KimchiNativeHost.checked(
        str(supported), environ={}, timeout=2.0,
    ), KimchiNativeHost)

    unsupported = tmp_path / "legacy-cli-only"
    unsupported.write_text(
        f"#!{sys.executable}\nprint('-p --mode')\n", encoding="utf-8",
    )
    unsupported.chmod(0o755)
    capability = probe_kimchi_native_capability(str(unsupported), environ={}, timeout=2.0)
    assert not capability.supported
    assert "--no-session" in capability.reason
    with pytest.raises(NativeCapabilityUnavailable) as rejected:
        KimchiNativeHost.checked(str(unsupported), environ={}, timeout=2.0)
    assert rejected.value.capability == capability

    sleeping = tmp_path / "unresponsive-cli"
    sleeping.write_text(
        f"#!{sys.executable}\nimport time\ntime.sleep(30)\n", encoding="utf-8",
    )
    sleeping.chmod(0o755)
    capability = probe_kimchi_native_capability(str(sleeping), environ={}, timeout=0.01)
    assert not capability.supported
    assert "timed out" in capability.reason


@pytest.mark.parametrize("return_code,stdout,error", [
    (17, json.dumps({
        "schema": "final-review-worker-result/v1", "status": "success",
    }), "native session exited 17"),
    (0, "not-json", "native session returned no valid final-review worker result"),
])
def test_failed_or_malformed_native_session_never_publishes_an_artifact(
        tmp_path, return_code, stdout, error):
    host = _FakeHost()
    executor = NativeExecutor(host)
    task = _task(tmp_path)
    completions = []
    executor.start(task, completions.append)

    host.sessions[task.task_id].emit(stdout, return_code)

    assert len(completions) == 1
    assert completions[0].state == "failed"
    assert completions[0].artifact is None
    assert completions[0].error == error
    assert not task.artifact_path.exists()


def test_run_expiry_fences_dispatch_and_cancels_only_owned_native_handles(tmp_path):
    timer = _DeadlineTimer()
    deadline = RunDeadline("run-1", 30, timer_factory=timer)
    host = _FakeHost()
    executor = NativeExecutor(host, deadline=deadline)
    other_host = _FakeHost()
    other_executor = NativeExecutor(other_host)
    task = _task(tmp_path, "owned")
    other_task = replace(
        _task(tmp_path, "unrelated"), run_id="run-other",
        session_name="review-run-other-unrelated",
    )
    completions = []
    deadline.start()
    handle = executor.start(task, completions.append)
    other_executor.start(other_task, lambda _completion: None)

    timer.fire()

    assert host.sessions["owned"].cancel_calls == 1
    assert other_host.sessions["unrelated"].cancel_calls == 0
    assert [completion.state for completion in completions] == ["cancelled"]
    handle.cancel()
    assert host.sessions["owned"].cancel_calls == 1
    assert len(completions) == 1

    with pytest.raises(RunDeadlineExpired):
        executor.start(_task(tmp_path, "late-dispatch"), completions.append)
    assert "late-dispatch" not in host.sessions

    host.sessions["owned"].finish({"status": "success", "late": True})
    assert len(completions) == 1
    assert not task.artifact_path.exists()
