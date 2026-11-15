"""C2: the Kimchi worker env scrub (port spec sections 3-4).

The reviewer subprocess must not inherit the interactive session's bridge
vars (sockets, ids, permission state) -- this replaces upstream's
_SESSION_BRIDGE_ENV claude list with the Kimchi deny list plus every
KIMCHI_PERMISSIONS* variant, while the OCR_*/STUB_* control vars stay.
"""
import importlib.util
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, _SCRIPTS)
_spec = importlib.util.spec_from_file_location("review_gate_env", os.path.join(_SCRIPTS, "review-gate.py"))
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)


def _env_with_everything():
    env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}
    for name in review_gate._SESSION_BRIDGE_ENV:
        env[name] = "leak-" + name
    # Prefix variants beyond the named list.
    env["KIMCHI_PERMISSIONS"] = "leak-permissions"
    env["KIMCHI_PERMISSIONS_MODE"] = "leak-mode"
    env["KIMCHI_PERMISSIONS_STRICT"] = "leak-strict"
    # Control vars that must survive.
    env.update({"OCR_IN_REVIEW": "1", "OCR_STUB_WORKER": "1", "STUB_TRACE": "/tmp/t"})
    return env


def _env_with_git_trace():
    """What a Kimchi Studio session hands the hook: the git-attribution
    trace vars, pointing at temp dirs that vanish when the session ends."""
    env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}
    for name in ("GIT_TRACE", "GIT_TRACE_REFS", "GIT_TRACE2_EVENT",
                 "GIT_TRACE_PARENT", "GIT_TRACE_FUTURE_FLAG"):
        env[name] = "/tmp/vanished-kimchi-session/" + name
    return env


def test_the_scrubbed_env_has_none_of_the_deny_listed_keys():
    env = _env_with_everything()
    removed = review_gate._scrub_worker_env(env)
    for name in review_gate._SESSION_BRIDGE_ENV:
        assert name not in env, name
    for name in ("KIMCHI_PERMISSIONS", "KIMCHI_PERMISSIONS_MODE",
                 "KIMCHI_PERMISSIONS_STRICT"):
        assert name not in env, name
    # The scrub names what it removed (the OCR_DEBUG breadcrumb).
    assert set(removed) >= set(review_gate._SESSION_BRIDGE_ENV)
    assert "KIMCHI_PERMISSIONS_STRICT" in removed


def test_the_scrub_keeps_ocr_and_stub_control_vars():
    env = _env_with_everything()
    review_gate._scrub_worker_env(env)
    assert env["OCR_IN_REVIEW"] == "1"
    assert env["OCR_STUB_WORKER"] == "1"
    assert env["STUB_TRACE"] == "/tmp/t"


def test_ocr_unset_env_none_disables_the_default_scrub():
    env = _env_with_everything()
    try:
        os.environ["OCR_UNSET_ENV"] = "none"
        review_gate._scrub_worker_env(env)
        assert env["KIMCHI_PARENT_SESSION_ID"] == "leak-KIMCHI_PARENT_SESSION_ID"
    finally:
        del os.environ["OCR_UNSET_ENV"]


def test_ocr_unset_env_replaces_the_default_list():
    env = _env_with_everything()
    try:
        os.environ["OCR_UNSET_ENV"] = "PI_PACKAGE_DIR KIMCHI_MESSAGING_TOKEN"
        review_gate._scrub_worker_env(env)
        assert "PI_PACKAGE_DIR" not in env
        assert "KIMCHI_MESSAGING_TOKEN" not in env
        assert "KIMCHI_PARENT_SESSION_ID" in env     # a replaced list scrubs nothing else
    finally:
        del os.environ["OCR_UNSET_ENV"]


def test_the_supervisor_env_is_scrubbed_the_same_way():
    os.environ.update({k: "leak-" + k for k in review_gate._SESSION_BRIDGE_ENV})
    os.environ["KIMCHI_PERMISSIONS_MODE"] = "leak-mode"
    try:
        env = review_gate._supervisor_env()
        for name in review_gate._SESSION_BRIDGE_ENV + ("KIMCHI_PERMISSIONS_MODE",):
            assert name not in env, name
    finally:
        for k in list(review_gate._SESSION_BRIDGE_ENV) + ["KIMCHI_PERMISSIONS_MODE"]:
            os.environ.pop(k, None)


# --- C2-fix: Kimchi's git-attribution GIT_TRACE* vars point at temp dirs
# that vanish with the session; any git WRITE run inheriting them dies exit
# 128 (the supervisor's `git clone --no-hardlinks` was proven to fail live).

def test_the_scrubbed_worker_env_has_no_git_trace_vars():
    env = _env_with_git_trace()
    removed = review_gate._scrub_worker_env(env)
    for name in ("GIT_TRACE", "GIT_TRACE_REFS", "GIT_TRACE2_EVENT",
                 "GIT_TRACE_PARENT", "GIT_TRACE_FUTURE_FLAG"):
        assert name not in env, name
        assert name in removed, name


def test_the_scrubbed_worker_env_has_no_git_trace_vars_even_with_unset_env_none():
    # A broken git is broken regardless of the scrub policy knob: the trace
    # scrub is unconditional.
    env = _env_with_git_trace()
    try:
        os.environ["OCR_UNSET_ENV"] = "none"
        review_gate._scrub_worker_env(env)
    finally:
        del os.environ["OCR_UNSET_ENV"]
    assert not [k for k in env if k.startswith("GIT_TRACE")]


def test_the_supervisor_env_has_no_git_trace_vars():
    os.environ.update(_env_with_git_trace())
    try:
        env = review_gate._supervisor_env()
        assert not [k for k in env if k.startswith("GIT_TRACE")]
    finally:
        for k in list(_env_with_git_trace()):
            os.environ.pop(k, None)


def test_the_test_environment_itself_has_no_git_trace_vars():
    # The conftest autouse scrub: every test subprocess (and every env helper
    # that copies os.environ) runs git without the trace vars, even when the
    # developer's Kimchi session exported them into pytest's own environment.
    assert not [k for k in os.environ if k.startswith("GIT_TRACE")]
