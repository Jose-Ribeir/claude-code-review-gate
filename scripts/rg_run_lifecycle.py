"""Finite whole-run deadlines and watchdog diagnostics for review execution."""
from __future__ import annotations

import math
import os
import threading
import time


DEFAULT_RUN_TIMEOUT_SECONDS = 1800.0
DEFAULT_WATCHDOG_MARGIN_SECONDS = 30.0


def _finite_positive(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite positive number of seconds")
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name} must be a finite positive number of seconds"
        ) from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} must be a finite positive number of seconds")
    return seconds


def run_timeout_from_env(environ=None, *, default=DEFAULT_RUN_TIMEOUT_SECONDS):
    """Read the explicit whole-run budget; worker/stage budgets stay separate."""
    source = os.environ if environ is None else environ
    fallback = _finite_positive(default, "default run timeout")
    value = source.get("RG_FINAL_REVIEW_TIMEOUT")
    if value is None:
        return fallback
    return _finite_positive(value, "RG_FINAL_REVIEW_TIMEOUT")


def watchdog_margin_diagnostics(timeout_seconds, environ=None, *,
                                margin_seconds=DEFAULT_WATCHDOG_MARGIN_SECONDS):
    """Describe the cleanup window separating controlled and external expiry."""
    source = os.environ if environ is None else environ
    timeout = _finite_positive(timeout_seconds, "RG_FINAL_REVIEW_TIMEOUT")
    margin = _finite_positive(margin_seconds, "watchdog margin")
    external_value = source.get("RG_FINAL_REVIEW_EXTERNAL_WATCHDOG_TIMEOUT")
    external = None if external_value in (None, "") else _finite_positive(
        external_value, "RG_FINAL_REVIEW_EXTERNAL_WATCHDOG_TIMEOUT",
    )
    controlled = timeout if external is None else min(timeout, external - margin)
    warnings = []
    if external is None:
        warnings.append(
            "No external watchdog timeout declared; keep any host kill beyond "
            f"the {timeout:g}s controlled deadline plus {margin:g}s cleanup margin."
        )
    elif external <= margin:
        controlled = 0.0
        warnings.append(
            f"External watchdog {external:g}s leaves no {margin:g}s cleanup margin; "
            "controlled execution cannot be scheduled safely."
        )
    elif controlled < timeout:
        controlled = max(0.0, controlled)
        warnings.append(
            f"External watchdog {external:g}s leaves an available margin of "
            f"{external - timeout:g}s, below the required {margin:g}s; the configured "
            f"{timeout:g}s run budget is shortened to {controlled:g}s to avoid "
            "external kill as the normal timeout path."
        )
    return {
        "timeout_seconds": timeout,
        "external_watchdog_seconds": external,
        "required_margin_seconds": margin,
        "available_margin_seconds": None if external is None else external - timeout,
        "controlled_deadline_seconds": controlled,
        "warnings": warnings,
    }


class RunDeadlineExpired(RuntimeError):
    """No new work may dispatch after the owning run reaches its deadline."""

    def __init__(self, run_id):
        super().__init__(f"run {run_id} has passed its whole-run deadline")
        self.run_id = run_id


class RunDeadline:
    """One-shot run budget with thread-safe fencing and scheduler callbacks."""

    def __init__(self, run_id, timeout_seconds, *, environ=None, on_start=None,
                 on_expire=None, clock=time.monotonic, timer_factory=threading.Timer):
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("run_id must be non-empty")
        self.run_id = run_id
        self.timeout_seconds = _finite_positive(
            timeout_seconds, "RG_FINAL_REVIEW_TIMEOUT",
        )
        self.watchdog = watchdog_margin_diagnostics(self.timeout_seconds, environ)
        self._on_start = on_start
        self._on_expire = on_expire
        self._clock = clock
        self._timer_factory = timer_factory
        self._timer = None
        self._started_at = None
        self._deadline_at = None
        self._expired = False
        self._start_count = 0
        self._expiry_count = 0
        self._handles = {}
        self._lock = threading.RLock()

    @property
    def expired(self):
        with self._lock:
            return self._expired

    def start(self):
        """Start once, reporting watchdog warnings before dispatching work."""
        with self._lock:
            if self._started_at is not None:
                raise RuntimeError(f"run {self.run_id} deadline was already started")
            if self.watchdog["controlled_deadline_seconds"] <= 0:
                raise ValueError("external watchdog leaves no controlled execution window")
            self._started_at = self._clock()
            self._deadline_at = (
                self._started_at + self.watchdog["controlled_deadline_seconds"]
            )
            self._start_count += 1
            diagnostics = self.diagnostics()
            self._timer = self._timer_factory(
                self.watchdog["controlled_deadline_seconds"], self._expire,
            )
            if self._on_start is not None:
                self._on_start(diagnostics)
            self._timer.start()
            return diagnostics

    def diagnostics(self):
        with self._lock:
            return {
                "run_id": self.run_id,
                "timeout_seconds": self.timeout_seconds,
                "start_count": self._start_count,
                "expiry_count": self._expiry_count,
                "watchdog": dict(self.watchdog),
            }

    def ensure_active(self, run_id):
        """Return remaining seconds or fence dispatch before any host launch."""
        with self._lock:
            if self._started_at is None:
                raise RuntimeError(f"run {self.run_id} deadline was not started")
            if self._expired:
                raise RunDeadlineExpired(self.run_id)
            remaining = self._deadline_at - self._clock()
            if remaining <= 0:
                self._expire()
                raise RunDeadlineExpired(self.run_id)
            if run_id != self.run_id:
                raise ValueError(
                    f"run {run_id} does not belong to deadline owner {self.run_id}"
                )
            return remaining

    def own(self, run_id, handle):
        """Register one exact cancellation handle, or cancel it if already late."""
        with self._lock:
            if run_id != self.run_id:
                raise ValueError(
                    f"run {run_id} does not belong to deadline owner {self.run_id}"
                )
            if not self._expired:
                self._handles[id(handle)] = handle
                return True
        handle.cancel()
        return False

    def release(self, handle):
        """Release a terminal handle without cancelling any other run work."""
        with self._lock:
            self._handles.pop(id(handle), None)

    def _expire(self):
        with self._lock:
            if self._expired:
                return
            self._expired = True
            self._expiry_count += 1
            handles = list(self._handles.values())
            details = self.diagnostics()
            details["reason"] = "run-deadline-exceeded"
            details["owned_cancellation_count"] = len(handles)
        for handle in handles:
            handle.cancel()
        if self._on_expire is not None:
            self._on_expire(details)

    def close(self):
        """Stop the timer without turning ordinary completion into expiry."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
