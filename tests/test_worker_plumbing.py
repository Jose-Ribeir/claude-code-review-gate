"""docs-gate 09: the shared worker-invocation builder
(scripts/review-gate.py::_worker_invocation).

Both headless worker callers (_run_review_once, _run_docs_summary_once)
resolve "what to spawn" through one builder: kimi/binary and stub resolution
with the D9 fail-closed missing-worker check, the OCR_KIMI_ARGS /
OCR_KIMI_EXTRA_ARGS rule, the worker environment (OCR_IN_REVIEW=1 plus the
session scrub), and the C3 canary circuit -- all as a single copy. These
tests pin the builder's contract at its own seam; the spawn loops, prompts,
stub argv modes, output parsing and error wrapping stay per-caller and are
covered by the argv goldens, the deny-canary suite and the generator suites.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_async_gate import review_gate  # noqa: E402
from test_process_groups import _wait_gone  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import rg_final_review as final_review  # noqa: E402
import rg_final_lenses as final_lenses  # noqa: E402

_FAKE_KIMI = "/fake/bin/kimchi"


@pytest.fixture
def clean_worker_env(monkeypatch):
    """Deterministic resolution input: none of the worker-plumbing env vars
    inherited from the developer's shell, and the canary pre-satisfied (the
    canary tests reset it themselves)."""
    for key in ("OCR_REVIEWER_CMD", "OCR_STUB_WORKER", "OCR_KIMI_ARGS",
                "OCR_KIMI_EXTRA_ARGS", "OCR_SKIP_CANARY", "OCR_UNSET_ENV"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setitem(review_gate._canary_state, "probed", True)
    return monkeypatch


def _exploding_builder():
    raise AssertionError("the default-args builder ran under an override")


def test_missing_worker_and_no_stub_fails_closed_with_the_callers_message(
        clean_worker_env, monkeypatch):
    monkeypatch.setattr(review_gate, "_find_kimi", lambda: None)
    for label in ("review worker message", "docs-summary worker message"):
        with pytest.raises(review_gate.ReviewGateError) as exc:
            review_gate._worker_invocation(review_gate._review_model_args, label)
        assert str(exc.value) == label


def test_an_override_does_not_excuse_a_missing_worker(clean_worker_env,
                                                      monkeypatch):
    monkeypatch.setattr(review_gate, "_find_kimi", lambda: None)
    monkeypatch.setenv("OCR_KIMI_ARGS", "--mode json")
    with pytest.raises(review_gate.ReviewGateError):
        review_gate._worker_invocation(review_gate._review_model_args, "missing")


def test_a_stub_satisfies_the_worker_requirement_without_a_kimchi_binary(
        clean_worker_env, monkeypatch):
    monkeypatch.setattr(review_gate, "_find_kimi", lambda: None)
    monkeypatch.setenv("OCR_STUB_WORKER", "1")
    kimi, stub, args, _env, _scrubbed = review_gate._worker_invocation(
        review_gate._review_model_args, "missing")
    assert kimi is None
    assert stub[0] == sys.executable and stub[1].endswith("kimi_stub.py")
    assert args == review_gate._review_model_args()


def test_an_override_replaces_the_default_argv_wholesale_and_skips_the_builder(
        clean_worker_env, monkeypatch):
    monkeypatch.setattr(review_gate, "_find_kimi", lambda: _FAKE_KIMI)
    monkeypatch.setenv("OCR_KIMI_ARGS", "--mode json --custom flag")
    _kimi, _stub, args, _env, _scrubbed = review_gate._worker_invocation(
        _exploding_builder, "missing")
    assert args == ["--mode", "json", "--custom", "flag"]


def test_without_an_override_the_per_caller_default_args_builder_is_used(
        clean_worker_env, monkeypatch):
    monkeypatch.setattr(review_gate, "_find_kimi", lambda: _FAKE_KIMI)
    for builder, expected in (
            (review_gate._review_model_args, review_gate._review_model_args()),
            (review_gate._docs_summary_model_args,
             review_gate._docs_summary_model_args()),
            (lambda: ["--sentinel"], ["--sentinel"])):
        _kimi, _stub, args, _env, _scrubbed = review_gate._worker_invocation(
            builder, "missing")
        assert args == expected


def test_the_canary_probes_once_across_both_worker_callers(
        clean_worker_env, monkeypatch):
    """Whichever caller runs first pays the C3 probe; the second caller in the
    same process does not re-probe (the guard is a single shared copy)."""
    probes = []
    monkeypatch.setattr(review_gate, "_find_kimi", lambda: _FAKE_KIMI)
    monkeypatch.setattr(review_gate, "_canary_probe",
                        lambda kimi, args, env: probes.append((kimi, args, env)))
    monkeypatch.setitem(review_gate._canary_state, "probed", False)
    review_args = review_gate._review_model_args()
    review_gate._worker_invocation(review_gate._review_model_args, "missing")
    review_gate._worker_invocation(review_gate._docs_summary_model_args, "missing")
    assert len(probes) == 1
    kimi, args, env = probes[0]
    assert kimi == _FAKE_KIMI and args == review_args
    assert env["OCR_IN_REVIEW"] == "1"


def test_the_canary_guard_conditions_match_the_documented_circuit(
        clean_worker_env, monkeypatch):
    """No probe for stub workers (no kimchi involved), under an OCR_KIMI_ARGS
    override (the caller owns that sandbox), or with OCR_SKIP_CANARY=1."""
    probes = []
    monkeypatch.setattr(review_gate, "_find_kimi", lambda: _FAKE_KIMI)
    monkeypatch.setattr(review_gate, "_canary_probe",
                        lambda *a: probes.append(a))
    build = lambda: review_gate._worker_invocation(  # noqa: E731
        review_gate._review_model_args, "missing")
    # Stub worker: skipped.
    monkeypatch.setitem(review_gate._canary_state, "probed", False)
    monkeypatch.setenv("OCR_STUB_WORKER", "1")
    build()
    monkeypatch.delenv("OCR_STUB_WORKER")
    # Wholesale argv override: skipped.
    monkeypatch.setenv("OCR_KIMI_ARGS", "--mode json")
    build()
    monkeypatch.delenv("OCR_KIMI_ARGS")
    # Emergency escape hatch: skipped.
    monkeypatch.setenv("OCR_SKIP_CANARY", "1")
    build()
    monkeypatch.delenv("OCR_SKIP_CANARY")
    assert probes == []


def test_the_worker_environment_is_marked_and_scrubbed_identically(
        clean_worker_env, monkeypatch):
    """Both callers hand the worker an environment marked OCR_IN_REVIEW=1 with
    the session-bridge, permission and git-trace variables scrubbed out -- one
    scrub, applied in exactly one place."""
    monkeypatch.setattr(review_gate, "_find_kimi", lambda: _FAKE_KIMI)
    monkeypatch.setenv("KIMCHI_PARENT_SESSION_ID", "sess-1")
    monkeypatch.setenv("KIMCHI_PERMISSIONS_MODE", "yolo")
    monkeypatch.setenv("KIMCHI_PERMISSIONS_CUSTOM", "x")
    monkeypatch.setenv("GIT_TRACE2_EVENT", "/tmp/trace")
    envs, scrubbed_lists = [], []
    for builder in (review_gate._review_model_args,
                    review_gate._docs_summary_model_args):
        _kimi, _stub, _args, child_env, scrubbed = review_gate._worker_invocation(
            builder, "missing")
        envs.append(child_env)
        scrubbed_lists.append(sorted(scrubbed))
    for child_env in envs:
        assert child_env["OCR_IN_REVIEW"] == "1"
        for gone in ("KIMCHI_PARENT_SESSION_ID", "KIMCHI_PERMISSIONS_MODE",
                     "KIMCHI_PERMISSIONS_CUSTOM", "GIT_TRACE2_EVENT"):
            assert gone not in child_env
    assert envs[0] == envs[1]
    assert scrubbed_lists[0] == scrubbed_lists[1]
# --- step 2: the docs-summary worker adopts the review worker's spawn -------
# discipline (docs-gate 09): registered in _ACTIVE_CHILDREN so the
# supervisor's kill path (_kill_active_children) can stop it in flight, and a
# timeout takes down the WHOLE process tree (_kill_child / _tree_kill), not
# just the direct launcher child. Before, the generator worker ran via
# subprocess.run: invisible to the supervisor, and a timed-out launcher left
# its real model child behind.

_STUB_REVIEWER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "stub_reviewer.py")


def _docs_summary_stub_env(monkeypatch, sleep):
    monkeypatch.setenv("OCR_REVIEWER_CMD", f'"{sys.executable}" "{_STUB_REVIEWER}"')
    monkeypatch.delenv("OCR_STUB_WORKER", raising=False)
    monkeypatch.delenv("OCR_KIMI_ARGS", raising=False)
    monkeypatch.setenv("STUB_SLEEP", str(sleep))


def test_a_timed_out_generator_worker_leaves_no_process_tree(
        tmp_path, monkeypatch):
    _docs_summary_stub_env(monkeypatch, sleep=120)
    monkeypatch.setenv("STUB_SPAWN_CHILD", str(tmp_path / "grandchild.pid"))
    monkeypatch.setattr(review_gate, "TIMEOUT", 3)
    started = time.monotonic()
    with pytest.raises(review_gate.ReviewGateError) as exc:
        review_gate._run_docs_summary_once(str(tmp_path), "t" * 40, ["docs/a.md"])
    assert exc.value.is_timeout
    assert "timed out after 3s" in str(exc.value)
    assert time.monotonic() - started < 60
    grandchild = int((tmp_path / "grandchild.pid").read_text())
    assert _wait_gone(grandchild), \
        "the generator worker's child outlived the timeout kill"
    # The registration is unwound on the way out, like the review worker's.
    assert review_gate._active_children() == []


def test_an_in_flight_generator_worker_is_supervisor_killable(
        tmp_path, monkeypatch):
    _docs_summary_stub_env(monkeypatch, sleep=120)
    monkeypatch.setattr(review_gate, "TIMEOUT", 300)
    outcome = {}

    def _worker():
        try:
            review_gate._run_docs_summary_once(str(tmp_path), "t" * 40,
                                               ["docs/a.md"])
            outcome["no-error"] = True
        except review_gate.ReviewGateError as exc:
            outcome["error"] = exc
        except BaseException as exc:  # the assertion below must see it
            outcome["unexpected"] = exc

    # daemon: if the worker ever became un-killable again the
    # assertions below fail fast instead of hanging the suite
    # for the stub's full sleep.
    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not review_gate._active_children():
        time.sleep(0.05)
    children = review_gate._active_children()
    assert children, "the in-flight generator worker was never registered"
    worker_pid = children[0].pid
    review_gate._kill_active_children()
    thread.join(timeout=15)
    assert not thread.is_alive(), "killing the worker did not end the call"
    # A supervisor kill is a generation FAILURE, not a timeout: the same
    # fail-closed message the other non-zero exits produce.
    err = outcome.get("error")
    assert "unexpected" not in outcome and err is not None
    assert not err.is_timeout
    assert "could not regenerate" in str(err)
    assert _wait_gone(worker_pid)
    assert review_gate._active_children() == []


# --- R8: the final-review worker's exact completion contract and owned tree ---

def test_default_final_worker_prompt_states_exact_success_status_contract(
        tmp_path, monkeypatch):
    monkeypatch.setattr(final_review.ocr_profile, "select_backend", lambda **kwargs: (
        SimpleNamespace(name="claude")
    ))
    monkeypatch.setattr(final_review.shutil, "which", lambda *args, **kwargs: "/bin/claude")
    command = final_review._default_worker_command(
        "/tmp/request.json", tmp_path, {},
        {"lens": "correctness", "review_duty": "Check behavior."},
    )
    prompt = command[command.index("-p") + 1]

    assert '"schema":"final-review-worker-result/v1"' in prompt
    assert '"status":"success"' in prompt
    assert '"success" | "completed" | "completed_with_warnings"' in prompt
    assert "success and completed both mean" in prompt
    assert "completed_with_warnings means" in prompt
    assert "only successful completion values" in prompt
    assert "Complete tool calls before returning the structured result" in prompt
    assert "The final assistant message must contain that object as text" in prompt
    assert "claim it was returned earlier" in prompt
    assert "Do not finish with a tool-call-only response" in prompt
    assert '"summary":"..."' not in prompt
    assert "angle-bracket templates" in prompt
    assert "sources_read" in prompt
    assert "evidence_references" in prompt
    assert "repository-relative path" in prompt
    assert "not treated as independently verified" in prompt


def _final_request(tmp_path):
    return {
        "lens": "correctness",
        "repository": {"path": str(tmp_path)},
        "diff": "diff --git a/app.py b/app.py\n",
    }


def test_echoed_placeholder_summary_is_incomplete_with_transport_diagnostics(tmp_path):
    script = tmp_path / "echo-placeholder.py"
    request_path_record = tmp_path / "completed-request-path"
    script.write_text(
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(request_path_record)!r}).write_text(sys.argv[-1])\n"
        "print(json.dumps({'schema':'final-review-worker-result/v1','status':'success',"
        "'summary':'...','findings':[{'path':'app.py','start_line':2,'end_line':2,"
        "'severity':'high','confidence':0.99,'content':'A genuine retained defect'}],"
        "'warnings':[],'unreviewed':[]}))\n",
        encoding="utf-8",
    )

    raw = final_review.invoke_worker(
        _final_request(tmp_path), command=[sys.executable, str(script)],
        environ={"PATH": os.environ.get("PATH", "")},
    )
    outcome = final_lenses.normalize_worker_result(raw, final_lenses.REQUIRED_LENSES[0])

    assert outcome.state == "incomplete"
    assert outcome.findings[0]["content"] == "A genuine retained defect"
    assert outcome.worker_evidence["received_status"] == "success"
    assert outcome.worker_evidence["received_summary"] == "..."
    assert outcome.worker_evidence["diagnostics"]["return_code"] == 0
    assert 0 < len(outcome.worker_evidence["diagnostics"]["stdout_tail"]) <= 512
    assert outcome.warnings[-1]["details"]["summary_rejection"] == "placeholder-only"
    assert outcome.warnings[-1]["details"]["return_code"] == 0
    request_path = Path(request_path_record.read_text())
    assert not request_path.exists(), "raw request survived the sanitized evidence hand-off"
    assert not request_path.parent.exists(), "raw request directory was not cleaned"


def test_final_worker_rejection_has_bounded_sanitized_structured_diagnostics(tmp_path):
    script = tmp_path / "invalid-status.py"
    script.write_text(
        "import json\n"
        "print(json.dumps({'schema':'final-review-worker-result/v1',"
        "'status':'finished','summary':'token=do-not-log-known-model-secret-' + 'x'*2000,"
        "'findings':[],'warnings':[]}))\n",
        encoding="utf-8",
    )

    with pytest.raises(final_review.WorkerError) as captured:
        final_review.invoke_worker(
            _final_request(tmp_path), command=[sys.executable, str(script)],
            environ={"PATH": os.environ.get("PATH", ""),
                     "OPENAI_API_KEY": "known-model-secret"},
        )

    details = captured.value.details
    assert details["received_status"] == "finished"
    assert details["return_code"] == 0
    assert details["expected_status"] == [
        "success", "completed", "completed_with_warnings",
    ]
    assert 0 < len(details["stdout_tail"]) <= 600
    assert "do-not-log" not in json.dumps(details)
    assert "known-model-secret" not in json.dumps(details)
    assert "x" * 700 not in details["stdout_tail"]


def test_worker_diagnostic_preserves_prefix_and_suffix_within_limit():
    text = (
        "INITIAL_CAUSE: token=known-model-secret; "
        + "unimportant-middle-" * 200
        + "FINAL_STACK_LINE"
    )
    sanitized = final_review._sanitize_worker_diagnostic(
        text, {"OPENAI_API_KEY": "known-model-secret"},
    )
    assert len(sanitized) <= 512
    assert sanitized.startswith("INITIAL_CAUSE:")
    assert sanitized.endswith("FINAL_STACK_LINE")
    assert "...[truncated]..." in sanitized
    assert "known-model-secret" not in sanitized


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group ownership")
def test_process_group_guard_never_targets_the_callers_group(monkeypatch):
    process = SimpleNamespace(pid=os.getpgrp(), killed=False)
    process.poll = lambda: None
    process.kill = lambda: setattr(process, "killed", True)
    monkeypatch.setattr(
        final_review.os, "killpg",
        lambda *args: (_ for _ in ()).throw(AssertionError("current group targeted")),
    )
    final_review._kill_owned_process(process)
    assert process.killed is True


def test_final_worker_timeout_kills_grandchild_and_cleans_request(tmp_path):
    marker = tmp_path / "grandchild-survived"
    pid_file = tmp_path / "grandchild.pid"
    request_path_record = tmp_path / "request-path"
    script = tmp_path / "slow-worker.py"
    script.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child_code = \"import pathlib,sys,time; time.sleep(1); "
        "pathlib.Path(sys.argv[1]).write_text('survived')\"\n"
        f"child = subprocess.Popen([sys.executable, '-c', child_code, {str(marker)!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        f"pathlib.Path({str(request_path_record)!r}).write_text(sys.argv[-1])\n"
        "print('worker-ready', flush=True)\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    grandchild = None
    try:
        with pytest.raises(final_review.WorkerError) as captured:
            final_review.invoke_worker(
                _final_request(tmp_path), command=[sys.executable, str(script)],
                environ={"PATH": os.environ.get("PATH", "")}, timeout=0.3,
            )
        grandchild = int(pid_file.read_text())
        time.sleep(1.2)
        assert not marker.exists(), "timed-out worker grandchild created a side effect"
        assert _wait_gone(grandchild), "timed-out worker grandchild remained in flight"
        details = captured.value.details
        assert details["reason"] == "timeout"
        assert details["return_code"] not in (None, 0)
        assert "worker-ready" in details["stdout_tail"]
        request_path = Path(request_path_record.read_text())
        assert not request_path.exists(), "the isolated request directory was not cleaned"
    finally:
        if grandchild is not None and not _wait_gone(grandchild, timeout=0.1):
            try:
                os.kill(grandchild, signal.SIGKILL)
            except OSError:
                pass


# --- R11: redact complete response credentials before diagnostic truncation ---

@pytest.mark.parametrize("text,secrets", [
    ('{"access_token":"json-token-value","client_secret":"json-secret-value"}',
     ("json-token-value", "json-secret-value")),
    ("access_token='whole token with spaces'; client_secret=plain-secret",
     ("whole token with spaces", "plain-secret")),
    ("aws_secret_access_key=underscored-secret proxy_authorization=proxy-secret",
     ("underscored-secret", "proxy-secret")),
    ("Authorization: Basic dXNlcjpwYXNz and Authorization: Bearer bearer.token.value",
     ("dXNlcjpwYXNz", "bearer.token.value")),
    ("https://alice:url-password@example.test/path?client_secret=url-secret",
     ("url-password", "url-secret")),
])
def test_response_diagnostic_redacts_actual_credential_formats(text, secrets):
    sanitized = final_review._sanitize_worker_diagnostic(
        text + " diagnostic-marker", {"UNRELATED": "not-expanded"},
    )
    for secret in secrets:
        assert secret not in sanitized
    assert "[redacted]" in sanitized
    assert sanitized.endswith("diagnostic-marker")


def test_response_diagnostics_redact_status_and_complete_values_before_truncate(tmp_path):
    secrets = {
        "access": "access-token-status-secret",
        "tail": "complete tail secret with spaces",
        "client": "truncated-client-secret",
        "basic": "ZmluYWwtcmV2aWV3OnNlY3JldA==",
        "url": "url-user-password",
        "known": "known-provider-secret-that-must-not-expand",
    }
    script = tmp_path / "credential-status.py"
    script.write_text(
        "import json\n"
        f"secrets = {secrets!r}\n"
        "payload = {'schema':'final-review-worker-result/v1',"
        " 'status':'finished access_token=' + secrets['access'], 'findings':[], 'warnings':[],"
        " 'summary':('padding-' * 150) + ' access_token=\"' + secrets['tail'] + '\"'"
        " + ' client_secret=' + secrets['client']"
        " + ' Authorization: Basic ' + secrets['basic']"
        " + ' https://alice:' + secrets['url'] + '@example.test/'"
        " + ' known=' + secrets['known']}\n"
        "print(json.dumps(payload))\n",
        encoding="utf-8",
    )
    with pytest.raises(final_review.WorkerError) as captured:
        final_review.invoke_worker(
            _final_request(tmp_path), command=[sys.executable, str(script)],
            environ={"PATH": os.environ.get("PATH", ""),
                     "OPENAI_API_KEY": secrets["known"]},
        )
    details = captured.value.details
    serialized = json.dumps(details)
    for secret in secrets.values():
        assert secret not in serialized
    assert "access_token" in details["received_status"]
    assert 0 < len(details["stdout_tail"]) <= 512
    assert details["received_status"].endswith("[redacted]")
    assert set(details) == {
        "reason", "received_status", "return_code", "stdout_tail", "expected_status",
    }


def test_unverified_timeout_retains_worker_request_and_exposes_incomplete_cleanup(
        tmp_path, monkeypatch):
    command_seen = []
    cleanup = {
        "status": "incomplete", "complete": False, "direct_child_reaped": True,
        "tree_termination_verified": False, "pipe_drain_completed": False,
        "termination_method": "direct-child", "errors": ["taskkill-invocation:OSError"],
    }

    def timed_out(command, **_kwargs):
        command_seen.append(command)
        assert Path(command[-1]).exists()
        error = subprocess.TimeoutExpired(command, 1, output=b"partial-timeout-output")
        error.returncode = -9
        error.cleanup = cleanup
        raise error

    monkeypatch.setattr(final_review, "run_owned_process", timed_out)
    with pytest.raises(final_review.WorkerError) as captured:
        final_review.invoke_worker(_final_request(tmp_path), command=["worker"], environ={})
    assert captured.value.details["reason"] == "timeout"
    assert captured.value.details["cleanup"] == cleanup
    assert "partial-timeout-output" in captured.value.details["stdout_tail"]
    request = Path(command_seen[0][-1])
    assert request.exists(), "unverified descendants must not race request deletion"
    request.unlink()
    request.parent.rmdir()


def test_simulation_does_not_delete_clones_while_descendants_are_unverified(
        tmp_path, monkeypatch):
    import sim_harness as simulation_harness

    monkeypatch.setattr(simulation_harness, "repo_snapshot", lambda _root: {"same": True})
    monkeypatch.setattr(simulation_harness, "selected_inference_backend", lambda *_a, **_k: {
        "name": "claude", "reason": "simulated", "executable": None, "notes": [],
    })
    abandoned = tmp_path / "simulation" / "state" / "clones" / "abc-final-timeout"
    abandoned.mkdir(parents=True)
    cleanup = {
        "status": "incomplete", "complete": False, "direct_child_reaped": True,
        "tree_termination_verified": False, "pipe_drain_completed": False,
        "errors": ["taskkill-timeout"],
    }

    def runner(*_args, **_kwargs):
        error = subprocess.TimeoutExpired("worker", 1, output=b"partial")
        error.cleanup = cleanup
        raise error

    report = simulation_harness.run_final_review_simulation(
        "ref", repo_root=tmp_path, artifact_dir=tmp_path / "simulation",
        environ={}, runner=runner,
    )
    assert abandoned.exists()
    assert report["state"] == "incomplete"
    assert report["cleanup"]["process"] == cleanup
    assert report["cleanup"]["clean"] is False
    assert report["cleanup"]["registered_clones_removed_after_timeout"] == []


# --- R11: simulated Windows failures; only POSIX below proves real tree kills ---

class _SimulatedPipe:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _SimulatedWindowsProcess:
    def __init__(self, drain_hangs):
        self.pid = 7319
        self.returncode = None
        self.drain_hangs = drain_hangs
        self.communicate_calls = []
        self.stdout = _SimulatedPipe()
        self.stderr = _SimulatedPipe()
        self.kill_called = False

    def communicate(self, timeout=None):
        self.communicate_calls.append(timeout)
        if len(self.communicate_calls) == 1:
            raise subprocess.TimeoutExpired(
                ["worker"], timeout, output=b"initial-", stderr=b"initial-err",
            )
        if self.drain_hangs:
            raise subprocess.TimeoutExpired(
                ["worker"], timeout, output=b"partial-stdout", stderr=b"partial-stderr",
            )
        return "partial-stdout", "partial-stderr"

    def poll(self):
        return self.returncode

    def kill(self):
        self.kill_called = True
        self.returncode = -9

    def wait(self, timeout=None):
        assert timeout is not None
        return self.returncode


def _simulate_windows_process(monkeypatch, process, taskkill_outcome):
    popen_options = {}
    taskkill_options = {}

    def popen(_command, **options):
        popen_options.update(options)
        return process

    def run(_command, **options):
        taskkill_options.update(options)
        if isinstance(taskkill_outcome, BaseException):
            raise taskkill_outcome
        return taskkill_outcome

    monkeypatch.setattr(final_review.os, "name", "nt")
    monkeypatch.setattr(final_review.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(final_review.subprocess, "Popen", popen)
    monkeypatch.setattr(final_review.subprocess, "run", run)
    monkeypatch.setattr(final_review, "_TASKKILL_TIMEOUT", 0.01)
    monkeypatch.setattr(final_review, "_REAP_TIMEOUT", 0.01)
    monkeypatch.setattr(final_review, "_PIPE_CLOSE_TIMEOUT", 0.01)
    return popen_options, taskkill_options


@pytest.mark.parametrize("taskkill_outcome", [
    SimpleNamespace(returncode=1),
    FileNotFoundError("simulated missing taskkill"),
    subprocess.TimeoutExpired(["taskkill"], 0.01),
])
def test_windows_failed_tree_kill_is_incomplete_and_pipe_drains_are_bounded(
        monkeypatch, taskkill_outcome):
    process = _SimulatedWindowsProcess(drain_hangs=True)
    popen_options, taskkill_options = _simulate_windows_process(
        monkeypatch, process, taskkill_outcome,
    )
    with pytest.raises(subprocess.TimeoutExpired) as captured:
        final_review.run_owned_process(
            ["worker"], cwd=".", env={}, timeout=0.01,
        )
    error = captured.value
    cleanup = error.cleanup
    assert process.kill_called
    assert process.stdout.closed and process.stderr.closed
    assert all(timeout is not None for timeout in process.communicate_calls)
    assert taskkill_options["timeout"] == 0.01
    assert popen_options["creationflags"] == 512
    assert "start_new_session" not in popen_options
    assert error.output == b"partial-stdout"
    assert error.stderr == b"partial-stderr"
    assert cleanup["status"] == "incomplete"
    assert cleanup["complete"] is False
    assert cleanup["direct_child_reaped"] is True
    assert cleanup["tree_termination_verified"] is False
    assert cleanup["pipe_drain_completed"] is False
    assert error.tree_termination_verified is False


def test_windows_direct_child_only_success_is_not_reported_as_tree_cleanup(monkeypatch):
    process = _SimulatedWindowsProcess(drain_hangs=False)
    _simulate_windows_process(monkeypatch, process, FileNotFoundError("missing"))
    with pytest.raises(subprocess.TimeoutExpired) as captured:
        final_review.run_owned_process(["worker"], cwd=".", env={}, timeout=0.01)
    cleanup = captured.value.cleanup
    assert cleanup["direct_child_reaped"] is True
    assert cleanup["pipe_drain_completed"] is True
    assert cleanup["tree_termination_verified"] is False
    assert cleanup["status"] == "incomplete"


def test_windows_taskkill_success_still_requires_reap_and_pipe_evidence(monkeypatch):
    process = _SimulatedWindowsProcess(drain_hangs=False)
    _simulate_windows_process(monkeypatch, process, SimpleNamespace(returncode=0))
    with pytest.raises(subprocess.TimeoutExpired) as captured:
        final_review.run_owned_process(["worker"], cwd=".", env={}, timeout=0.01)
    cleanup = captured.value.cleanup
    assert cleanup == {
        "status": "complete", "termination_method": "taskkill-tree+direct-child",
        "direct_child_reaped": True, "tree_termination_verified": True,
        "pipe_drain_completed": True, "errors": [], "taskkill_returncode": 0,
        "complete": True,
    }


@pytest.mark.skipif(os.name != "posix", reason="real POSIX nested process-group proof")
def test_posix_timeout_reaps_nested_pipe_holders_and_preserves_partial_output(tmp_path):
    marker = tmp_path / "nested-survived"
    child_pid_file = tmp_path / "child.pid"
    nested_pid_file = tmp_path / "nested.pid"
    script = tmp_path / "nested-worker.py"
    nested_code = (
        "import pathlib,sys,time; time.sleep(1); "
        f"pathlib.Path({str(marker)!r}).write_text('survived'); time.sleep(120)"
    )
    child_code = (
        "import pathlib,subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable,'-c',{nested_code!r}]); "
        f"pathlib.Path({str(nested_pid_file)!r}).write_text(str(p.pid)); time.sleep(120)"
    )
    script.write_text(
        "import pathlib,subprocess,sys,time\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid))\n"
        f"for _ in range(100):\n"
        f"    if pathlib.Path({str(nested_pid_file)!r}).exists(): break\n"
        "    time.sleep(0.01)\n"
        "print('nested-ready', flush=True)\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    pids = []
    try:
        with pytest.raises(subprocess.TimeoutExpired) as captured:
            final_review.run_owned_process(
                [sys.executable, str(script)], cwd=str(tmp_path),
                env={"PATH": os.environ.get("PATH", "")}, timeout=0.5,
            )
        output = captured.value.output
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        assert "nested-ready" in output
        cleanup = captured.value.cleanup
        assert cleanup["status"] == "complete"
        assert cleanup["direct_child_reaped"] is True
        assert cleanup["tree_termination_verified"] is True
        pids = [int(path.read_text()) for path in (child_pid_file, nested_pid_file)]
        assert all(_wait_gone(pid) for pid in pids)
        time.sleep(1.1)
        assert not marker.exists()
    finally:
        for pid in pids:
            if not _wait_gone(pid, timeout=0.1):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
