"""Ticket #14: native top-level session executor contract."""
from dataclasses import replace
import json
import os
import signal
import sys
import threading
import time

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
from rg_final_store import FinalStore  # noqa: E402


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
        deadline_at_unix=time.time() + 200.0,
        prompt="Return the contracted result.",
        repo_root=tmp_path,
        artifact_path=tmp_path / (task_id + ".json"),
    )


def test_completion_handle_preserves_native_identity_without_worker_publication(tmp_path):
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
    serialized = completion.artifact.as_dict()
    assert not task.artifact_path.exists()
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
        "deadline_at_unix": task.deadline_at_unix,
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
    assert not task.artifact_path.exists()


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
    assert completions[-1].artifact.payload == {
        "schema": "final-review-worker-result/v1", "status": "success", "task": "task-a",
    }
    assert not first_task.artifact_path.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX RPC process identity")
def test_kimchi_rpc_host_correlates_prompt_and_settles_with_explicit_identity(tmp_path):
    capture = tmp_path / "launch.json"
    commands = tmp_path / "commands.json"
    executable = tmp_path / "rpc-kimchi-double"
    payload = {
        "schema": "final-review-worker-result/v1",
        "status": "success",
        "summary": "RPC double with Unicode separators \u2028 and \u2029",
        "findings": [],
        "warnings": [],
    }
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        f"pathlib.Path({str(capture)!r}).write_text(json.dumps({{"
        "'argv': sys.argv[1:], 'env': dict(os.environ), 'pid': os.getpid(),"
        " 'parent_pid': os.getppid(), 'process_group': os.getsid(0)}))\n"
        "command = json.loads(sys.stdin.buffer.readline())\n"
        f"pathlib.Path({str(commands)!r}).write_text(json.dumps(command))\n"
        "def emit(record):\n"
        "    sys.stdout.buffer.write(json.dumps(record).encode('utf-8') + b'\\n')\n"
        "    sys.stdout.buffer.flush()\n"
        "emit({'id': command['id'], 'type': 'response', 'command': 'prompt',"
        "      'success': True, 'data': {'disposition': 'started'}})\n"
        f"emit({{'type': 'message_end', 'message': {{'role': 'assistant',"
        f"      'content': [{{'type': 'text', 'text': json.dumps({payload!r})}}],"
        "      'stopReason': 'stop'}})\n"
        "emit({'type': 'agent_end', 'messages': [], 'willRetry': False})\n"
        "emit({'type': 'agent_settled', 'aborted': False})\n"
        "while sys.stdin.buffer.readline():\n"
        "    pass\n",
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
        _task(tmp_path), model="other-provider-model",
        allow_tools=("Read", "Bash(git diff *)"),
        deny_tools=("Write", "Bash(gh *)"),
    )
    completions = []
    delivered = threading.Event()

    def completed(completion):
        completions.append(completion)
        delivered.set()

    handle = executor.start(task, completed)
    assert delivered.wait(5), "RPC host did not deliver its completion callback"

    launched = json.loads(capture.read_text(encoding="utf-8"))
    command = json.loads(commands.read_text(encoding="utf-8"))
    assert launched["parent_pid"] == os.getpid()
    assert launched["process_group"] == launched["pid"]
    assert handle.native_session_id == "rpc:run-1:task-a"
    assert handle.transport == "native-top-level/kimchi-rpc-v1"
    assert launched["argv"][:5] == [
        "--mode", "rpc", "--no-session", "--no-approve", "--no-auto-update",
    ]
    assert "--model" in launched["argv"]
    assert launched["argv"][launched["argv"].index("--model") + 1] == "other-provider-model"
    assert "--mcp-config" in launched["argv"]
    assert "--plan" in launched["argv"]
    assert "Bash(git diff *)" in launched["argv"]
    assert "--deny-tool" in launched["argv"]
    assert "Write" in launched["argv"]
    assert "Bash(gh *)" in launched["argv"]
    assert command == {"id": "run-1:task-a:prompt", "type": "prompt", "message": task.prompt}
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
    assert len(completions) == 1
    assert completions[0].error is None, completions[0].error
    assert completions[0].state == "completed"
    assert completions[0].artifact.payload == payload


@pytest.mark.parametrize("scenario,expected_error", [
    ("prompt-error", "native RPC prompt failed: Model not found: invalid/model"),
    (
        "wrong-response-id",
        "native RPC protocol error: uncorrelated response id='wrong' command='prompt'",
    ),
    (
        "malformed-json",
        "native RPC malformed JSON: Expecting property name enclosed in double quotes: "
        "line 1 column 2 (char 1)",
    ),
    ("process-exit", "native RPC process exited 7 before agent_settled"),
    ("empty-settlement", "native RPC agent_settled without a final assistant message"),
])
def test_rpc_failures_deliver_exactly_one_specific_terminal_callback(
        tmp_path, scenario, expected_error):
    executable = tmp_path / (scenario + "-rpc-double")
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"scenario = {scenario!r}\n"
        "command = json.loads(sys.stdin.buffer.readline())\n"
        "def emit(record):\n"
        "    sys.stdout.buffer.write(json.dumps(record).encode() + b'\\n')\n"
        "    sys.stdout.buffer.flush()\n"
        "if scenario == 'malformed-json':\n"
        "    sys.stdout.buffer.write(b'{bad}\\n'); sys.stdout.buffer.flush()\n"
        "elif scenario == 'wrong-response-id':\n"
        "    emit({'id': 'wrong', 'type': 'response', 'command': 'prompt', 'success': True})\n"
        "elif scenario == 'process-exit':\n"
        "    sys.exit(7)\n"
        "else:\n"
        "    emit({'id': command['id'], 'type': 'response', 'command': 'prompt',\n"
        "          'success': scenario != 'prompt-error',\n"
        "          'error': 'Model not found: invalid/model',\n"
        "          'data': {'disposition': 'started'}})\n"
        "    if scenario == 'empty-settlement':\n"
        "        emit({'type': 'agent_settled', 'aborted': False})\n"
        "while sys.stdin.buffer.readline():\n"
        "    pass\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    executor = NativeExecutor(KimchiNativeHost(str(executable), environ={}))
    completions = []
    delivered = threading.Event()

    def completed(completion):
        completions.append(completion)
        delivered.set()

    executor.start(_task(tmp_path), completed)
    assert delivered.wait(5), "RPC failure did not reach its terminal callback"

    assert len(completions) == 1
    assert completions[0].state == "failed"
    assert completions[0].artifact is None
    assert completions[0].error == expected_error
    assert completions[0].details["cleanup"]["direct_child_reaped"] is True
    assert not (tmp_path / "task-a.json").exists()


@pytest.mark.parametrize("cancellation", ["operator", "worker-deadline"])
def test_rpc_abort_is_correlated_reaped_and_truthful_about_cancellation(
        tmp_path, cancellation):
    commands_path = tmp_path / "rpc-commands.jsonl"
    executable = tmp_path / "abort-rpc-double"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "def emit(record):\n"
        "    sys.stdout.buffer.write(json.dumps(record).encode() + b'\\n')\n"
        "    sys.stdout.buffer.flush()\n"
        "while True:\n"
        "    line = sys.stdin.buffer.readline()\n"
        "    if not line:\n"
        "        break\n"
        "    command = json.loads(line)\n"
        f"    with pathlib.Path({str(commands_path)!r}).open('ab') as stream:\n"
        "        stream.write(json.dumps(command).encode() + b'\\n')\n"
        "    emit({'id': command['id'], 'type': 'response',\n"
        "          'command': command['type'], 'success': True,\n"
        "          'data': {'disposition': 'started'} if command['type'] == 'prompt' else {}})\n"
        "    if command['type'] == 'abort':\n"
        "        emit({'type': 'agent_settled', 'aborted': True})\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    task = replace(
        _task(tmp_path),
        deadline_at_unix=time.time() + (0.2 if cancellation == "worker-deadline" else 30),
    )
    executor = NativeExecutor(KimchiNativeHost(str(executable), environ={}))
    completions = []
    delivered = threading.Event()

    def completed(completion):
        completions.append(completion)
        delivered.set()

    handle = executor.start(task, completed)
    if cancellation == "operator":
        handle.cancel()
    assert delivered.wait(5), "RPC abort did not complete bounded cleanup"

    expected_reason = (
        "worker-deadline-exceeded" if cancellation == "worker-deadline"
        else "operator-cancelled"
    )
    assert len(completions) == 1
    assert completions[0].state == "cancelled"
    assert completions[0].details["reason"] == expected_reason
    assert completions[0].details["cleanup"]["complete"] is True
    records = [json.loads(line) for line in commands_path.read_text().splitlines()]
    assert [record["type"] for record in records] == ["prompt", "abort"]
    assert records[1]["id"] == "run-1:task-a:abort"
    assert not task.artifact_path.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable double")
def test_kimchi_capability_probe_is_bounded_and_never_falls_back(tmp_path):
    required_help = (
        "--mode rpc --no-session --no-approve --no-auto-update --model "
        "--mcp-config --allow-tool --deny-tool --plan"
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
    assert capability.transport == "native-top-level/kimchi-rpc-v1"
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


def test_process_launch_failure_gets_one_failed_callback_with_task_identity(tmp_path):
    class BrokenHost(_FakeHost):
        def launch(self, task, callback):
            raise OSError("injected spawn failure")

    executor = NativeExecutor(BrokenHost())
    completions = []

    handle = executor.start(_task(tmp_path), completions.append)

    assert handle.native_session_id == "unlaunched:run-1:task-a"
    assert len(completions) == 1
    assert completions[0].handle == handle
    assert completions[0].state == "failed"
    assert completions[0].artifact is None
    assert completions[0].error == "native session launch failed: injected spawn failure"
    assert not (tmp_path / "task-a.json").exists()


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


@pytest.mark.skipif(os.name != "posix", reason="POSIX descendant and pipe ownership")
def test_native_deadline_streams_progress_reaps_pipes_and_sanitizes_tails(tmp_path):
    descendant_path = tmp_path / "descendant.pid"
    executable = tmp_path / "timeout-kimchi-double"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"pathlib.Path({str(descendant_path)!r}).write_text(str(child.pid))\n"
        "def emit(record):\n"
        "    sys.stdout.buffer.write(json.dumps(record).encode() + b'\\n')\n"
        "    sys.stdout.buffer.flush()\n"
        "while True:\n"
        "    line = sys.stdin.buffer.readline()\n"
        "    if not line:\n"
        "        break\n"
        "    command = json.loads(line)\n"
        "    emit({'id': command['id'], 'type': 'response',\n"
        "          'command': command['type'], 'success': True,\n"
        "          'data': {'disposition': 'started'} if command['type'] == 'prompt' else {}})\n"
        "    if command['type'] == 'prompt':\n"
        "        emit({'type': 'message_end', 'message': {'role': 'assistant',\n"
        "              'content': [{'type': 'text', 'text': 'progress token=stdout-secret'}],\n"
        "              'stopReason': 'stop'}})\n"
        "        print('tool failed: bearer stderr-secret', file=sys.stderr, flush=True)\n"
        "    if command['type'] == 'abort':\n"
        "        emit({'type': 'agent_settled', 'aborted': True})\n"
        "        os._exit(0)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    progress = []
    emitted = threading.Event()

    def on_progress(event):
        progress.append(event)
        emitted.set()

    class CapturingHost(KimchiNativeHost):
        def launch(self, task, callback):
            self.session = super().launch(task, callback)
            return self.session

    host = CapturingHost(
        str(executable),
        environ={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)},
        progress=on_progress,
    )
    timer = _DeadlineTimer()
    deadline = RunDeadline("run-1", 30, timer_factory=timer)
    executor = NativeExecutor(host, deadline=deadline)
    completions = []
    delivered = threading.Event()

    def completed(completion):
        completions.append(completion)
        delivered.set()

    started = time.monotonic()
    deadline.start()
    handle = executor.start(_task(tmp_path), completed)
    try:
        assert emitted.wait(5), "native test double did not stream its initial progress"
        timer.fire()
        host.session.process.wait(timeout=5)
        assert delivered.wait(10), "native cancellation did not finish bounded pipe cleanup"
        assert time.monotonic() - started < 8
        completion = completions[0]
        assert completion.handle == handle
        assert completion.state == "cancelled"
        assert "progress" in completion.stdout
        assert "tool failed" in completion.stderr
        assert "stdout-secret" not in completion.details["stdout_tail"]
        assert "stderr-secret" not in completion.details["stderr_tail"]
        assert len(completion.details["stdout_tail"]) <= 512
        assert len(completion.details["stderr_tail"]) <= 512
        assert completion.details["cleanup"]["complete"] is True
        assert completion.details["cleanup"]["pipe_drain_completed"] is True
        assert not task_artifact_exists(tmp_path)
        assert progress and all(event["event"] == "stream-progress" for event in progress)
        assert all("token" not in str(event) for event in progress)

        descendant = int(descendant_path.read_text())
        gone_by = time.monotonic() + 5
        while time.monotonic() < gone_by:
            try:
                os.kill(descendant, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("pipe-holding descendant outlived native cleanup")
    finally:
        deadline.close()
        handle.cancel()
        if descendant_path.exists():
            try:
                os.kill(int(descendant_path.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def task_artifact_exists(tmp_path):
    return (tmp_path / "task-a.json").exists()


def test_malformed_native_result_is_a_durable_failed_event(tmp_path):
    with FinalStore(tmp_path / "native-failure.sqlite3") as store:
        started = store.start_run({
            "provider": "gitlab", "target_project": "group/project",
            "root_target_ref": "main", "root_target_sha": "b" * 40,
            "items": [{
                "item_key": "123", "source_project": "fork/project",
                "source_ref": "feature", "source_sha": "a" * 40,
                "target_project": "group/project", "target_ref": "main",
                "target_sha": "b" * 40, "provider_data": {"iid": 123},
            }],
        })
        task = replace(
            _task(tmp_path), run_id=started["run_id"],
            session_name="failure-session",
            unit_id=started["unit_ids"]["individual:1"],
            revision_tuple=("b" * 40, "a" * 40, "b" * 40),
        )
        host = _FakeHost()
        completions = []
        NativeExecutor(host, store=store).start(task, completions.append)

        host.sessions[task.task_id].emit("not-json", 0)
        events = store.manifest(started["run_id"])["native_tasks"][0]["events"]
        assert [event["event_type"] for event in events].count("queued") == 1
        assert [event["event_type"] for event in events].count("started") == 1
        assert [event["event_type"] for event in events].count("failed") == 1
        assert completions[0].state == "failed"
        assert not task.artifact_path.exists()
        failure = next(event for event in events if event["event_type"] == "failed")
        assert failure["details"] == {"error": "native session returned no valid final-review worker result"}
        diagnostic = next(
            event for event in events
            if event["event_type"] == "warning" and event["details"].get("diagnostic") is True
        )
        assert diagnostic["details"]["stdout_tail"] == "not-json"
        assert diagnostic["details"]["cleanup"] is None
        assert diagnostic["details"]["return_code"] == 0
        assert diagnostic["details"]["stderr_tail"] == ""


@pytest.mark.skipif(os.name != "posix", reason="POSIX descendant process-group control")
def test_cancel_wakes_pipe_watcher_when_descendant_escapes_group(tmp_path):
    capture = tmp_path / "escaped-pid"
    executable = tmp_path / "parent-with-escaped-child"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, subprocess, sys, time\n"
        "grandchild = subprocess.Popen("
        "[sys.executable, '-c', 'import time; time.sleep(60)'], "
        "start_new_session=True)\n"
        f"pathlib.Path({str(capture)!r}).write_text(str(grandchild.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    executor = NativeExecutor(KimchiNativeHost(str(executable), environ={}))
    completed = threading.Event()

    def finish(completion):
        assert completion.state == "cancelled"
        completed.set()

    handle = executor.start(_task(tmp_path), finish)
    deadline = threading.Event()
    for _ in range(100):
        if capture.exists():
            deadline.set()
            break
        deadline.wait(0.05)
    assert deadline.is_set(), "native child did not expose its detached descendant"
    try:
        handle.cancel()
        assert completed.wait(5), "descendant-held stdout kept cancellation waiting"
    finally:
        if capture.exists():
            try:
                os.kill(int(capture.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        handle.cancel()


@pytest.mark.skipif(os.name != "posix", reason="POSIX worker deadline behavior")
def test_queue_time_is_not_charged_as_worker_runtime(tmp_path):
    executable = tmp_path / "eventual-native-double"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "request = json.loads(sys.stdin.buffer.readline())\n"
        "print(json.dumps({'id': request['id'], 'type': 'response', 'command': 'prompt',"
        " 'success': True, 'data': {'disposition': 'started'}}), flush=True)\n"
        "time.sleep(0.2)\n"
        "payload = {'schema': 'final-review-worker-result/v1', 'status': 'success',"
        " 'summary': 'fresh execution budget', 'findings': [], 'warnings': [],"
        " 'unreviewed': []}\n"
        "print(json.dumps({'type': 'message_end', 'message':"
        " {'role': 'assistant', 'content': json.dumps(payload)}}), flush=True)\n"
        "print(json.dumps({'type': 'agent_settled', 'aborted': False}), flush=True)\n"
        "os._exit(0)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    completions = []
    delivered = threading.Event()
    executor = NativeExecutor(KimchiNativeHost(str(executable), environ={}))
    task = replace(
        _task(tmp_path),
        deadline_at_unix=1.0,
        timeout_seconds=1.0,
    )

    def complete(completion):
        completions.append(completion)
        delivered.set()

    executor.start(task, complete)
    assert delivered.wait(8), "worker was cancelled because it queued past an absolute timestamp"
    assert completions[0].state == "completed"
    assert completions[0].artifact.payload["summary"] == "fresh execution budget"


@pytest.mark.skipif(os.name != "posix", reason="POSIX native CLI invocation")
def test_auto_model_does_not_override_harness_model_selection(tmp_path):
    capture = tmp_path / "model-argv.json"
    executable = tmp_path / "model-check-native-double"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "request = json.loads(sys.stdin.buffer.readline())\n"
        "print(json.dumps({'id': request['id'], 'type': 'response', 'command': 'prompt',"
        " 'success': True, 'data': {'disposition': 'started'}}), flush=True)\n"
        "payload = {'schema': 'final-review-worker-result/v1', 'status': 'success',"
        " 'summary': 'model selection respected', 'findings': [], 'warnings': [],"
        " 'unreviewed': []}\n"
        "print(json.dumps({'type': 'message_end', 'message':"
        " {'role': 'assistant', 'content': json.dumps(payload)}}), flush=True)\n"
        "print(json.dumps({'type': 'agent_settled', 'aborted': False}), flush=True)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    delivered = threading.Event()
    executor = NativeExecutor(KimchiNativeHost(str(executable), environ={}))
    executor.start(
        replace(_task(tmp_path), model="auto"),
        lambda _completion: delivered.set(),
    )
    assert delivered.wait(5)
    launched = json.loads(capture.read_text())
    assert "--model" not in launched


@pytest.mark.skipif(os.name != "posix", reason="POSIX native RPC lifecycle")
def test_provider_retry_status_allows_native_recovery_and_uses_final_message(tmp_path):
    executable = tmp_path / "retrying-native-double"
    payload = {
        "schema": "final-review-worker-result/v1", "status": "success",
        "summary": "completed after native transient retry", "findings": [],
        "warnings": [], "unreviewed": [],
    }
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "def emit(record):\n"
        "    print(json.dumps(record), flush=True)\n"
        "prompt = json.loads(sys.stdin.buffer.readline())\n"
        "emit({'id': prompt['id'], 'type': 'response', 'command': 'prompt',"
        "      'success': True, 'data': {'disposition': 'started'}})\n"
        "emit({'type': 'message_update', 'assistantMessageEvent':"
        "      {'type': 'error', 'error': 'Retrying…'}})\n"
        "emit({'type': 'message_end', 'message': {'role': 'assistant',"
        "      'content': 'Retrying…', 'stopReason': 'error',"
        "      'errorMessage': 'Retrying…'}})\n"
        "emit({'type': 'agent_end', 'messages': [], 'willRetry': True})\n"
        "emit({'type': 'agent_start'})\n"
        f"emit({{'type': 'message_end', 'message': {{'role': 'assistant',"
        f"      'content': json.dumps({payload!r}), 'stopReason': 'stop'}}}})\n"
        "emit({'type': 'agent_end', 'messages': [], 'willRetry': False})\n"
        "emit({'type': 'agent_settled', 'aborted': False})\n"
        "while sys.stdin.buffer.readline():\n"
        "    pass\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    completions = []
    delivered = threading.Event()

    def completed(completion):
        completions.append(completion)
        delivered.set()

    executor = NativeExecutor(KimchiNativeHost(str(executable), environ={}))
    executor.start(_task(tmp_path), completed)
    assert delivered.wait(5), "transient retry status aborted native recovery"
    assert len(completions) == 1
    assert completions[0].state == "completed"
    assert completions[0].artifact.payload["summary"] == "completed after native transient retry"


@pytest.mark.skipif(os.name != "posix", reason="POSIX native RPC lifecycle")
def test_unrecovered_provider_error_fails_at_final_settlement(tmp_path):
    executable = tmp_path / "terminal-provider-error-double"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "def emit(record):\n"
        "    print(json.dumps(record), flush=True)\n"
        "prompt = json.loads(sys.stdin.buffer.readline())\n"
        "emit({'id': prompt['id'], 'type': 'response', 'command': 'prompt',"
        "      'success': True, 'data': {'disposition': 'started'}})\n"
        "emit({'type': 'message_end', 'message': {'role': 'assistant',"
        "      'content': 'provider unavailable', 'stopReason': 'error',"
        "      'errorMessage': 'provider unavailable'}})\n"
        "emit({'type': 'agent_end', 'messages': [], 'willRetry': False})\n"
        "emit({'type': 'agent_settled', 'aborted': True})\n"
        "while sys.stdin.buffer.readline():\n"
        "    pass\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    completions = []
    delivered = threading.Event()

    def completed(completion):
        completions.append(completion)
        delivered.set()

    executor = NativeExecutor(KimchiNativeHost(str(executable), environ={}))
    executor.start(_task(tmp_path), completed)
    assert delivered.wait(5)
    assert len(completions) == 1
    assert completions[0].state == "failed"
    assert completions[0].artifact is None
    assert completions[0].error == "native RPC provider error: provider unavailable"


@pytest.mark.parametrize(
    "following_structured", [None, {
        "schema": "final-review-worker-result/v1", "status": "finished",
        "summary": "newer structured report", "findings": [], "warnings": [],
    }],
    ids=["trailing-commentary", "newer-structured-object"],
)
@pytest.mark.skipif(os.name != "posix", reason="POSIX native RPC lifecycle")
def test_latest_parseable_worker_result_survives_later_assistant_text(
        tmp_path, following_structured):
    executable = tmp_path / "multi-message-native-double"
    payload = {
        "schema": "final-review-worker-result/v1", "status": "success",
        "summary": "structured result before commentary", "findings": [],
        "warnings": [], "unreviewed": [],
    }
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "def emit(record):\n"
        "    print(json.dumps(record), flush=True)\n"
        "prompt = json.loads(sys.stdin.buffer.readline())\n"
        "emit({'id': prompt['id'], 'type': 'response', 'command': 'prompt',"
        "      'success': True, 'data': {'disposition': 'started'}})\n"
        f"emit({{'type': 'message_end', 'message': {{'role': 'assistant',"
        f"      'content': json.dumps({payload!r}), 'stopReason': 'toolCalls'}}}})\n"
        + (
            "emit({'type': 'message_end', 'message': {'role': 'assistant',"
            "      'content': 'The prior turn contained the completed JSON result.'}})\n"
            if following_structured is None else
            f"emit({{'type': 'message_end', 'message': {{'role': 'assistant',"
            f"      'content': json.dumps({following_structured!r})}}}})\n"
        )
        + "emit({'type': 'agent_end', 'messages': [], 'willRetry': False})\n"
          "emit({'type': 'agent_settled', 'aborted': False})\n"
          "while sys.stdin.buffer.readline():\n"
          "    pass\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    completions = []
    delivered = threading.Event()

    def completed(completion):
        completions.append(completion)
        delivered.set()

    executor = NativeExecutor(KimchiNativeHost(str(executable), environ={}))
    executor.start(_task(tmp_path), completed)
    assert delivered.wait(5), "pre-chatter structured output was discarded"
    assert len(completions) == 1
    if following_structured is not None:
        assert completions[0].state == "failed"
        assert completions[0].artifact is None
        assert completions[0].error == "native RPC newest structured worker result is invalid"
        assert completions[0].details["stdout_tail"] == json.dumps(following_structured)
        assert completions[0].details["last_assistant_tail"] == json.dumps(
            following_structured
        )
        return
    assert completions[0].state == "completed"
    assert completions[0].artifact.payload == payload


@pytest.mark.parametrize("prior_valid_result", [False, True])
@pytest.mark.parametrize("structured_replacement", [False, True])
@pytest.mark.skipif(os.name != "posix", reason="POSIX native RPC lifecycle")
def test_provider_error_requires_valid_replacement_before_settlement(
        tmp_path, prior_valid_result, structured_replacement):
    payload = {
        "schema": "final-review-worker-result/v1", "status": "success",
        "summary": "Inspected app.py and its callers.", "findings": [],
        "warnings": [], "unreviewed": [],
    }
    replacement = (
        {"schema": payload["schema"], "status": "success", "summary": "...",
         "findings": [], "warnings": [], "unreviewed": []}
        if structured_replacement else "No replacement structured result was provided."
    )
    expected_text = json.dumps(replacement) if structured_replacement else replacement
    executable = tmp_path / "provider-error-non-result-double"
    script = [
        f"#!{sys.executable}\n",
        "import json, sys\n",
        "def emit(record):\n",
        "    print(json.dumps(record), flush=True)\n",
        "prompt = json.loads(sys.stdin.buffer.readline())\n",
        "emit({'id': prompt['id'], 'type': 'response', 'command': 'prompt',"
        "      'success': True, 'data': {'disposition': 'started'}})\n",
    ]
    if prior_valid_result:
        script.append(
            f"emit({{'type': 'message_end', 'message': {{'role': 'assistant',"
            f"      'content': json.dumps({payload!r}), 'stopReason': 'stop'}}}})\n"
        )
    script.extend([
        "emit({'type': 'message_end', 'message': {'role': 'assistant',"
        "      'content': 'provider unavailable', 'stopReason': 'error',"
        "      'errorMessage': 'provider unavailable'}})\n",
        f"emit({{'type': 'message_end', 'message': {{'role': 'assistant',"
        f"      'content': {expected_text!r}, 'stopReason': 'stop'}}}})\n",
        "emit({'type': 'agent_end', 'messages': [], 'willRetry': False})\n",
        "emit({'type': 'agent_settled', 'aborted': False})\n",
        "while sys.stdin.buffer.readline():\n",
        "    pass\n",
    ])
    executable.write_text("".join(script), encoding="utf-8")
    executable.chmod(0o755)
    completions = []
    delivered = threading.Event()

    def completed(completion):
        completions.append(completion)
        delivered.set()

    executor = NativeExecutor(KimchiNativeHost(str(executable), environ={}))
    executor.start(_task(tmp_path), completed)
    assert delivered.wait(5), "non-recovery commentary incorrectly completed"
    assert len(completions) == 1
    assert completions[0].state == "failed"
    assert completions[0].artifact is None
    assert completions[0].error == "native RPC provider error: provider unavailable"
    expected_stdout = (
        json.dumps(payload)
        if prior_valid_result and not structured_replacement else expected_text
    )
    assert completions[0].details["stdout_tail"] == expected_stdout
    assert completions[0].details["last_assistant_tail"] == expected_text
