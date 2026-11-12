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
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_async_gate import review_gate  # noqa: E402
from test_process_groups import _wait_gone  # noqa: E402

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
