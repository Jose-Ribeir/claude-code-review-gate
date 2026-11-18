"""Ticket #17: reusable finite run deadline and ownership contracts."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from rg_run_lifecycle import (  # noqa: E402
    RunDeadline,
    RunDeadlineExpired,
    run_timeout_from_env,
    watchdog_margin_diagnostics,
)


class _Timer:
    def __init__(self):
        self.delay = None
        self.callback = None
        self.cancelled = False

    def __call__(self, delay, callback):
        assert self.delay is None
        self.delay = delay
        self.callback = callback
        return self

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.callback()


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "not-a-number", ""])
def test_run_timeout_requires_a_finite_positive_value(value):
    with pytest.raises(ValueError, match="RG_FINAL_REVIEW_TIMEOUT"):
        run_timeout_from_env({"RG_FINAL_REVIEW_TIMEOUT": value})


def test_run_timeout_uses_an_explicit_finite_default_and_override():
    assert run_timeout_from_env({}) == 1800.0
    assert run_timeout_from_env({"RG_FINAL_REVIEW_TIMEOUT": "12.5"}) == 12.5
    with pytest.raises(ValueError, match="finite"):
        run_timeout_from_env({}, default=float("nan"))


def test_expiry_fences_later_dispatch_and_runs_callbacks_exactly_once():
    timer = _Timer()
    started = []
    expired = []
    deadline = RunDeadline(
        "run-a", 12.5, timer_factory=timer, clock=lambda: 1000.0,
        on_start=started.append, on_expire=expired.append,
    )

    diagnostics = deadline.start()

    assert deadline.ensure_active("run-a") == pytest.approx(12.5)
    assert timer.delay == 12.5
    assert started == [diagnostics]
    assert diagnostics["timeout_seconds"] == 12.5
    assert diagnostics["watchdog"]["warnings"]

    timer.fire()

    assert expired[0]["run_id"] == "run-a"
    assert expired[0]["reason"] == "run-deadline-exceeded"
    assert expired[0]["expiry_count"] == 1
    with pytest.raises(RunDeadlineExpired, match="run-a"):
        deadline.ensure_active("run-a")
    with pytest.raises(RunDeadlineExpired, match="run-a"):
        deadline.ensure_active("run-other")
    timer.fire()
    assert len(expired) == 1


def test_watchdog_margin_diagnostics_require_a_controlled_cleanup_window():
    missing = watchdog_margin_diagnostics(60.0, {})
    assert missing["controlled_deadline_seconds"] == 60.0
    assert missing["external_watchdog_seconds"] is None
    assert missing["required_margin_seconds"] == 30.0
    assert len(missing["warnings"]) == 1

    insufficient = watchdog_margin_diagnostics(60.0, {
        "RG_FINAL_REVIEW_EXTERNAL_WATCHDOG_TIMEOUT": "75",
    })
    assert insufficient["controlled_deadline_seconds"] == 45.0
    assert "15s" in insufficient["warnings"][0]

    sufficient = watchdog_margin_diagnostics(60.0, {
        "RG_FINAL_REVIEW_EXTERNAL_WATCHDOG_TIMEOUT": "90",
    })
    assert sufficient["controlled_deadline_seconds"] == 60.0
    assert sufficient["warnings"] == []
