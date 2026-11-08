"""C3 deny-extension startup canary (scripts/review-gate.py::_canary_probe).

The canary is the only detector for a silently-unloaded `-e` extension: a
headless run prompted to write a canary file MUST be blocked. These tests fake
the kimchi spawn at the subprocess layer, so they cover the decision logic
(the leak, the timeout, the clean pass) without needing a real session.
"""
import importlib.util
import os
import re
import subprocess
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")

_spec = importlib.util.spec_from_file_location("review_gate_canary", _GATE)
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)


class _FakeResult:
    def __init__(self):
        self.returncode = 0
        self.stdout = ""
        self.stderr = ""


def _canary_path_from(cmd):
    # The prompt embeds the canary path between fixed words.
    m = re.search(r"create the file (\S+) containing", cmd[2])
    assert m, "canary prompt did not embed a path: %r" % cmd[2]
    return m.group(1)


def _fake_kimi(monkeypatch, writes_canary, raises=None):
    """Replace the canary's subprocess.run. writes_canary=True models an
    extension that did NOT load: the write the prompt asks for succeeds."""
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = list(cmd)
        seen["env"] = dict(kw.get("env") or {})
        seen["timeout"] = kw.get("timeout")
        if raises is not None:
            raise raises
        if writes_canary:
            with open(_canary_path_from(cmd), "w", encoding="utf-8") as fh:
                fh.write("ok\n")
        return _FakeResult()

    monkeypatch.setattr(review_gate.subprocess, "run", run)
    return seen


ARGS = ["--mode", "json", "--no-session", "-e",
        os.path.join(_SCRIPTS, "..", "extensions", "review-gate-deny.ts")]


def test_a_loaded_extension_passes_the_canary(monkeypatch):
    seen = _fake_kimi(monkeypatch, writes_canary=False)
    review_gate._canary_probe("/fake/bin/kimchi", ARGS, {"OCR_IN_REVIEW": "1"})
    # The probe used the worker wiring: same argv, in-review env, a timeout.
    assert seen["cmd"][0] == "/fake/bin/kimchi" and seen["cmd"][1] == "-p"
    assert seen["cmd"][3:] == ARGS
    assert seen["env"]["OCR_IN_REVIEW"] == "1"
    assert seen["timeout"] == review_gate._CANARY_TIMEOUT


def test_a_successful_canary_write_is_an_error_state_deny(monkeypatch):
    _fake_kimi(monkeypatch, writes_canary=True)
    with pytest.raises(review_gate.ReviewGateError) as exc:
        review_gate._canary_probe("/fake/bin/kimchi", ARGS, {"OCR_IN_REVIEW": "1"})
    msg = str(exc.value)
    # Error-state deny: the run aborts, an attempt burns, doctor is the fix.
    assert "did NOT engage" in msg
    assert "burning an attempt" in msg
    assert "ocr_doctor" in msg
    assert "review-gate-deny.ts" in msg


def test_a_canary_that_cannot_run_fails_closed(monkeypatch):
    _fake_kimi(monkeypatch, writes_canary=False,
               raises=subprocess.TimeoutExpired(cmd=["kimi"], timeout=1))
    with pytest.raises(review_gate.ReviewGateError) as exc:
        review_gate._canary_probe("/fake/bin/kimchi", ARGS, {"OCR_IN_REVIEW": "1"})
    assert "could not be verified" in str(exc.value)
    assert "ocr_doctor" in str(exc.value)


def test_the_canary_leaves_no_files_behind(monkeypatch, tmp_path):
    # Even the leaking run's scratch dir is removed: the canary must not
    # itself litter the machine it is protecting.
    _fake_kimi(monkeypatch, writes_canary=True)
    before = set(os.listdir(str(tmp_path)))
    with pytest.raises(review_gate.ReviewGateError):
        review_gate._canary_probe("/fake/bin/kimchi", ARGS, {"OCR_IN_REVIEW": "1"})
    assert set(os.listdir(str(tmp_path))) == before


def test_the_worker_run_probes_once_and_honors_the_escape_hatch(monkeypatch):
    # _run_review_once must fire the canary before the first real worker call
    # (once per process) and honour OCR_SKIP_CANARY. (Stub workers never reach
    # the probe: `stub` short-circuits it in _run_review_once, and stub runs
    # spawn a real child process, so this test stops the run at the env scrub
    # that immediately follows the canary site instead.)
    calls = []

    def probe(kimi, args, env):
        calls.append(list(args))

    monkeypatch.setattr(review_gate, "_canary_probe", probe)
    monkeypatch.setattr(review_gate, "_find_kimi", lambda: "/fake/bin/kimchi")
    monkeypatch.delenv("OCR_STUB_WORKER", raising=False)
    review_gate._canary_state["probed"] = False

    class _Stop(Exception):
        pass

    def stop():
        raise _Stop  # the canary fires before this line in _run_review_once

    monkeypatch.setattr(review_gate, "_debug_enabled", stop)
    with pytest.raises(_Stop):
        review_gate._run_review_once("/nowhere", "hook")
    assert len(calls) == 1 and calls[0] == review_gate.DEFAULT_KIMI_ARGS
    # ...once per process even on repeated calls...
    with pytest.raises(_Stop):
        review_gate._run_review_once("/nowhere", "hook")
    assert len(calls) == 1
    # ...and never with OCR_SKIP_CANARY set.
    review_gate._canary_state["probed"] = False
    monkeypatch.setenv("OCR_SKIP_CANARY", "1")
    with pytest.raises(_Stop):
        review_gate._run_review_once("/nowhere", "hook")
    assert len(calls) == 1  # the escape hatch suppressed the probe
