"""Durable, store-local SPEC-16 final-review identity, evidence, and reports.

The database is one dedicated, portable SQLite file, never a coding tool's
application database or a file inside a repository under review. Only Python's
standard-library sqlite3 is used. All writes are explicit transactions; a write
transaction is acquired with BEGIN IMMEDIATE so its run sequence and same-store
supersession decisions are atomic. The module records verdicts supplied by the
supervisor and delegates official finding verdicts to the existing ocr_verdict
authority.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import ocr_verdict
import rg_final_acceptance
import rg_final_contract
import rg_final_memory
import rg_final_provider
import rg_final_telemetry

SCHEMA_VERSION = 4
REPORT_SCHEMA = "final-review-report/v1"
PUBLICATION_STATUS_CONTEXT = "review-gate/final-review"
FINAL_STATES = frozenset(("pass", "findings", "stale", "incomplete"))
NATIVE_TASK_SCHEMA = "native-review-task/v1"
NATIVE_EVENT_SCHEMA = "native-review-event/v1"
NATIVE_ARTIFACT_SCHEMA = "native-review-artifact/v1"
NATIVE_EVENT_TYPES = frozenset((
    "queued", "started", "completed", "failed", "warning", "timeout", "cancelled",
    "cleanup", "finalization",
))
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


class StoreError(Exception):
    """The final-review store cannot provide a trustworthy answer."""


class StoreBusyError(StoreError):
    """A SQLite writer remained unavailable after bounded retries."""


class _PublicationAttempt:
    """One audit row finalized while its same-store writer guard is held."""

    def __init__(self, store, publication_id, run_id, current):
        self.store = store
        self.publication_id = publication_id
        self.run_id = run_id
        self.current = current
        self.finalized = False

    def record(self, status, status_context, details=None, error=""):
        if self.finalized:
            raise StoreError("publication attempt was already finalized")
        cursor = self.store._conn.execute(
            "UPDATE publications SET status = ?, status_context = ?, details_json = ?, "
            "error = ? WHERE publication_id = ? AND run_id = ?",
            (str(status), str(status_context), _json(details), str(error),
             self.publication_id, self.run_id),
        )
        if cursor.rowcount != 1:
            raise StoreError("publication audit row is unavailable")
        self.finalized = True

    def degrade_trust(self, reason):
        return self.store._degrade_publication_trust(self.run_id, reason)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json(value):
    return json.dumps(value if value is not None else {}, sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"))


def _loads(value):
    return json.loads(value) if value else {}


def _identifier():
    return str(uuid.uuid4())


def _required_text(mapping, key):
    value = str(mapping.get(key, "")).strip()
    if not value:
        raise ValueError(f"missing required {key}")
    return value


def _sha(value, field):
    text = str(value or "").strip()
    if not _SHA.fullmatch(text):
        raise ValueError(f"{field} must be an immutable 40-character commit SHA")
    return text.lower()


def _finite_time(value, field, *, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or \
            not math.isfinite(float(value)):
        raise ValueError(f"{field} must be a finite Unix timestamp")
    return float(value)


def _native_identity(value):
    if not isinstance(value, dict):
        raise ValueError("native task identity must be an object")
    required = {
        "run_id", "task_id", "session_name", "unit_id", "stage", "lens",
        "revision_tuple", "input_sha256", "backend", "model", "deadline_at_unix",
    }
    if set(value) != required:
        raise ValueError("native task identity has missing or unknown fields")
    normalized = {
        key: _required_text(value, key) for key in (
            "run_id", "task_id", "session_name", "unit_id", "stage", "lens",
            "backend", "model",
        )
    }
    revisions = value["revision_tuple"]
    if not isinstance(revisions, (list, tuple)) or not revisions or any(
            not isinstance(item, str) or not item.strip() for item in revisions):
        raise ValueError("native task revision_tuple must contain immutable revision identities")
    normalized["revision_tuple"] = list(revisions)
    normalized["input_sha256"] = _required_text(value, "input_sha256").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized["input_sha256"]):
        raise ValueError("native task input_sha256 must be a SHA-256 digest")
    normalized["deadline_at_unix"] = _finite_time(
        value["deadline_at_unix"], "deadline_at_unix",
    )
    return normalized


def _native_task(value):
    if not isinstance(value, dict) or value.get("schema") != NATIVE_TASK_SCHEMA:
        raise ValueError("native task has the wrong schema")
    if set(value) != {"schema", "identity", "transport"}:
        raise ValueError("native task has missing or unknown fields")
    return {
        "schema": NATIVE_TASK_SCHEMA,
        "identity": _native_identity(value.get("identity")),
        "transport": _required_text(value, "transport"),
    }


def _native_artifact(value):
    if not isinstance(value, dict) or value.get("schema") != NATIVE_ARTIFACT_SCHEMA:
        raise ValueError("native artifact has the wrong schema")
    fields = {"schema", "transport", "native_session_id", "identity", "payload", "timing"}
    if set(value) != fields:
        raise ValueError("native artifact has missing or unknown fields")
    payload = value.get("payload")
    if not isinstance(payload, dict) or not {"schema", "status"} <= set(payload):
        raise ValueError("native artifact worker result is incomplete")
    if payload["schema"] != "final-review-worker-result/v1":
        raise ValueError("native artifact worker result has the wrong schema")
    if str(payload["status"]).upper() == "STOP":
        # STOP is terminal worker evidence, not a successful worker completion.
        # Scheduler validation remains authoritative for its structured payload.
        if "stop" in payload and not isinstance(payload["stop"], dict):
            raise ValueError("native artifact STOP payload must be an object")
    else:
        required_payload = {"summary", "findings", "warnings", "unreviewed"}
        if not required_payload <= set(payload):
            raise ValueError("native artifact worker result is incomplete")
        if payload["status"] not in ("success", "completed", "completed_with_warnings"):
            raise ValueError("native artifact worker result is not a completed result")
        if not isinstance(payload["summary"], str) or not payload["summary"].strip():
            raise ValueError("native artifact worker result summary is incomplete")
        if any(not isinstance(payload[key], list)
               for key in ("findings", "warnings", "unreviewed")):
            raise ValueError("native artifact worker result collections must be arrays")
        if any(not isinstance(finding, dict) for finding in payload["findings"]):
            raise ValueError("native artifact worker result contains an invalid finding")
    timing = value.get("timing")
    if not isinstance(timing, dict) or set(timing) != {
            "started_at_unix", "finished_at_unix", "duration_seconds"}:
        raise ValueError("native artifact timing has missing or unknown fields")
    started = _finite_time(timing["started_at_unix"], "started_at_unix")
    finished = _finite_time(timing["finished_at_unix"], "finished_at_unix")
    duration = _finite_time(timing["duration_seconds"], "duration_seconds")
    if finished < started:
        raise ValueError("native artifact finish precedes its start")
    if duration != finished - started:
        raise ValueError("native artifact duration does not match its exact timing")
    return {
        "schema": NATIVE_ARTIFACT_SCHEMA,
        "transport": _required_text(value, "transport"),
        "native_session_id": _required_text(value, "native_session_id"),
        "identity": _native_identity(value.get("identity")),
        "payload": payload,
        "timing": {
            "started_at_unix": started,
            "finished_at_unix": finished,
            "duration_seconds": duration,
        },
    }


def _native_event(value):
    if not isinstance(value, dict) or value.get("schema") != NATIVE_EVENT_SCHEMA:
        raise ValueError("native event has the wrong schema")
    fields = {
        "schema", "event_id", "event_type", "identity", "transport",
        "native_session_id", "timing", "details",
    }
    if set(value) != fields:
        raise ValueError("native event has missing or unknown fields")
    event_type = _required_text(value, "event_type")
    if event_type not in NATIVE_EVENT_TYPES:
        raise ValueError(f"unsupported native event type {event_type}")
    timing = value.get("timing")
    if not isinstance(timing, dict) or set(timing) != {
            "occurred_at_unix", "started_at_unix", "finished_at_unix", "duration_seconds"}:
        raise ValueError("native event timing has missing or unknown fields")
    occurred = _finite_time(timing["occurred_at_unix"], "occurred_at_unix")
    started = _finite_time(timing["started_at_unix"], "started_at_unix", optional=True)
    finished = _finite_time(timing["finished_at_unix"], "finished_at_unix", optional=True)
    duration = _finite_time(timing["duration_seconds"], "duration_seconds", optional=True)
    if event_type == "started" and started is None:
        started = occurred
    if event_type in ("completed", "timeout", "cancelled") and finished is None:
        finished = occurred
    if event_type == "queued" and any(
            item is not None for item in (started, finished, duration)):
        raise ValueError("queued native events cannot have execution timing")
    if event_type == "started" and (finished is not None or duration is not None):
        raise ValueError("started native events cannot have completion timing")
    if started is None and duration is not None:
        raise ValueError("native event duration requires a start time")
    if finished is not None and finished != occurred:
        raise ValueError("native event occurrence must equal its finish time")
    if started is not None and finished is not None:
        if finished < started:
            raise ValueError("native event finish precedes its start")
        measured = finished - started
        if duration is not None and duration != measured:
            raise ValueError("native event duration does not match its exact timing")
        duration = measured
    session = value.get("native_session_id")
    if session is not None:
        session = _required_text({"native_session_id": session}, "native_session_id")
    if event_type in ("started", "completed", "timeout", "cancelled") and session is None:
        raise ValueError(f"{event_type} native events require native_session_id")
    details = value.get("details")
    if not isinstance(details, dict):
        raise ValueError("native event details must be an object")
    return {
        "schema": NATIVE_EVENT_SCHEMA,
        "event_id": _required_text(value, "event_id"),
        "event_type": event_type,
        "identity": _native_identity(value.get("identity")),
        "transport": _required_text(value, "transport"),
        "native_session_id": session,
        "timing": {
            "occurred_at_unix": occurred,
            "started_at_unix": started,
            "finished_at_unix": finished,
            "duration_seconds": duration,
        },
        "details": details,
    }


_SCHEMA_V1 = (
    """CREATE TABLE store_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE contract_versions (
        version TEXT PRIMARY KEY,
        implementation_version TEXT NOT NULL,
        document_hash TEXT NOT NULL,
        current INTEGER NOT NULL DEFAULT 0 CHECK(current IN (0, 1)),
        installed_at TEXT NOT NULL
    )""",
    """CREATE TABLE contract_rules (
        contract_version TEXT NOT NULL REFERENCES contract_versions(version),
        rule_key TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        sort_order INTEGER NOT NULL,
        PRIMARY KEY(contract_version, rule_key),
        UNIQUE(contract_version, sort_order)
    )""",
    """CREATE TABLE contract_decisions (
        contract_version TEXT NOT NULL REFERENCES contract_versions(version),
        decision_key TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        sort_order INTEGER NOT NULL,
        PRIMARY KEY(contract_version, decision_key),
        UNIQUE(contract_version, sort_order)
    )""",
    """CREATE TABLE stacks (
        stack_id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        target_project TEXT NOT NULL,
        root_target_ref TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        run_seq INTEGER NOT NULL UNIQUE,
        stack_id TEXT NOT NULL REFERENCES stacks(stack_id),
        contract_version TEXT NOT NULL REFERENCES contract_versions(version),
        acceptance_version TEXT,
        state TEXT NOT NULL DEFAULT 'incomplete'
            CHECK(state IN ('pass', 'findings', 'stale', 'incomplete')),
        continuity_required INTEGER NOT NULL CHECK(continuity_required IN (0, 1)),
        independent INTEGER NOT NULL CHECK(independent IN (0, 1)),
        started_at TEXT NOT NULL,
        completed_at TEXT,
        superseded_by_run_id TEXT REFERENCES runs(run_id),
        metadata_json TEXT NOT NULL,
        trust_json TEXT NOT NULL
    )""",
    "CREATE INDEX runs_stack_seq ON runs(stack_id, run_seq)",
    """CREATE TABLE run_items (
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        position INTEGER NOT NULL CHECK(position > 0),
        item_key TEXT NOT NULL,
        source_project TEXT NOT NULL,
        source_ref TEXT NOT NULL,
        source_sha TEXT NOT NULL,
        target_project TEXT NOT NULL,
        target_ref TEXT NOT NULL,
        target_sha TEXT NOT NULL,
        provider_data_json TEXT NOT NULL,
        PRIMARY KEY(run_id, position),
        UNIQUE(run_id, item_key)
    )""",
    """CREATE TABLE units (
        unit_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        unit_key TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('individual', 'combined', 'prospective')),
        item_position INTEGER,
        base_sha TEXT NOT NULL,
        head_sha TEXT NOT NULL,
        target_ref TEXT,
        source_ref TEXT,
        state TEXT NOT NULL DEFAULT 'incomplete'
            CHECK(state IN ('pass', 'findings', 'stale', 'incomplete')),
        summary TEXT NOT NULL DEFAULT '',
        metadata_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(run_id, unit_key),
        FOREIGN KEY(run_id, item_position) REFERENCES run_items(run_id, position)
    )""",
    """CREATE TABLE lenses (
        lens_id TEXT PRIMARY KEY,
        unit_id TEXT NOT NULL REFERENCES units(unit_id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        state TEXT NOT NULL,
        summary TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )""",
    "CREATE INDEX lenses_unit ON lenses(unit_id)",
    """CREATE TABLE test_evidence (
        test_id TEXT PRIMARY KEY,
        unit_id TEXT NOT NULL REFERENCES units(unit_id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        state TEXT NOT NULL,
        output TEXT NOT NULL,
        evidence_fingerprint TEXT NOT NULL,
        details_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )""",
    "CREATE INDEX test_evidence_unit ON test_evidence(unit_id)",
    """CREATE TABLE findings (
        finding_id TEXT PRIMARY KEY,
        unit_id TEXT NOT NULL REFERENCES units(unit_id) ON DELETE CASCADE,
        finding_key TEXT NOT NULL,
        content TEXT NOT NULL,
        severity TEXT NOT NULL,
        confidence TEXT NOT NULL,
        state TEXT NOT NULL,
        lens TEXT,
        path TEXT,
        line INTEGER,
        evidence_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        UNIQUE(unit_id, finding_key)
    )""",
    "CREATE INDEX findings_key ON findings(finding_key, state)",
    """CREATE TABLE finding_anchors (
        anchor_id TEXT PRIMARY KEY,
        finding_id TEXT NOT NULL REFERENCES findings(finding_id) ON DELETE CASCADE,
        anchor_type TEXT NOT NULL,
        anchor TEXT NOT NULL,
        evidence_fingerprint TEXT NOT NULL,
        details_json TEXT NOT NULL,
        UNIQUE(finding_id, anchor_type, anchor, evidence_fingerprint)
    )""",
    """CREATE TABLE warnings (
        warning_id TEXT PRIMARY KEY,
        unit_id TEXT NOT NULL REFERENCES units(unit_id) ON DELETE CASCADE,
        code TEXT NOT NULL,
        message TEXT NOT NULL,
        scope TEXT NOT NULL,
        details_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )""",
    "CREATE INDEX warnings_unit ON warnings(unit_id)",
    """CREATE TABLE coverage (
        coverage_id TEXT PRIMARY KEY,
        unit_id TEXT NOT NULL REFERENCES units(unit_id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        summary TEXT NOT NULL,
        details_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        UNIQUE(unit_id, kind, status, summary)
    )""",
    """CREATE TABLE stops (
        stop_id TEXT PRIMARY KEY,
        unit_id TEXT NOT NULL REFERENCES units(unit_id) ON DELETE CASCADE,
        reason TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )""",
    """CREATE TABLE stop_units (
        stop_id TEXT NOT NULL REFERENCES stops(stop_id) ON DELETE CASCADE,
        unit_id TEXT NOT NULL REFERENCES units(unit_id),
        PRIMARY KEY(stop_id, unit_id)
    )""",
    """CREATE TABLE skipped_work (
        skipped_id TEXT PRIMARY KEY,
        stop_id TEXT REFERENCES stops(stop_id) ON DELETE CASCADE,
        unit_id TEXT NOT NULL REFERENCES units(unit_id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        details_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )""",
    "CREATE INDEX skipped_work_unit ON skipped_work(unit_id)",
    """CREATE TABLE publications (
        publication_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        unit_id TEXT REFERENCES units(unit_id),
        item_key TEXT,
        status TEXT NOT NULL,
        status_context TEXT NOT NULL,
        details_json TEXT NOT NULL,
        error TEXT NOT NULL,
        attempted_at TEXT NOT NULL
    )""",
    "CREATE INDEX publications_run ON publications(run_id)",
)

_SCHEMA_V2 = (
    "ALTER TABLE run_items ADD COLUMN base_sha TEXT",
    "ALTER TABLE run_items ADD COLUMN reference TEXT",
    "ALTER TABLE run_items ADD COLUMN ancestors_json TEXT NOT NULL DEFAULT '[]'",
    """CREATE TABLE finding_threads (
        thread_id TEXT PRIMARY KEY,
        stack_id TEXT NOT NULL REFERENCES stacks(stack_id),
        unit_key TEXT NOT NULL,
        finding_key TEXT NOT NULL,
        content_anchor TEXT NOT NULL,
        origin_finding_id TEXT NOT NULL UNIQUE REFERENCES findings(finding_id),
        created_at TEXT NOT NULL,
        UNIQUE(stack_id, unit_key, content_anchor)
    )""",
    "CREATE INDEX finding_threads_scope ON finding_threads(stack_id, unit_key, content_anchor)",
    """CREATE TABLE finding_events (
        event_id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL REFERENCES finding_threads(thread_id),
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        unit_id TEXT NOT NULL REFERENCES units(unit_id),
        finding_id TEXT REFERENCES findings(finding_id),
        event_type TEXT NOT NULL
            CHECK(event_type IN ('observed', 'reopened', 'carried', 'resolved', 'falsified')),
        anchors_json TEXT NOT NULL,
        evidence_anchor TEXT NOT NULL,
        previous_evidence_anchor TEXT,
        evidence_json TEXT NOT NULL,
        rationale TEXT NOT NULL,
        resolution_json TEXT NOT NULL,
        revalidation_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )""",
    "CREATE INDEX finding_events_history ON finding_events(thread_id, recorded_at, event_id)",
    "CREATE INDEX finding_events_run ON finding_events(run_id, event_type)",
    """CREATE TABLE freshness_checks (
        check_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
        state TEXT NOT NULL CHECK(state IN ('fresh', 'stale', 'incomplete')),
        changes_json TEXT NOT NULL,
        errors_json TEXT NOT NULL,
        pinned_json TEXT NOT NULL,
        current_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )""",
    "CREATE INDEX freshness_checks_run ON freshness_checks(run_id, recorded_at, check_id)",
)

_SCHEMA_V3 = (
    """CREATE TABLE acceptance_versions (
        version TEXT PRIMARY KEY,
        implementation_version TEXT NOT NULL,
        document_hash TEXT NOT NULL,
        current INTEGER NOT NULL DEFAULT 0 CHECK(current IN (0, 1)),
        installed_at TEXT NOT NULL
    )""",
    """CREATE TABLE acceptance_scenarios (
        scenario_version TEXT NOT NULL REFERENCES acceptance_versions(version),
        scenario_id TEXT NOT NULL,
        title TEXT NOT NULL,
        behavior TEXT NOT NULL,
        expected_state TEXT NOT NULL
            CHECK(expected_state IN ('pass', 'findings', 'incomplete', 'stale', 'error')),
        required_evidence_json TEXT NOT NULL,
        dispatch_key TEXT NOT NULL,
        assertions_json TEXT NOT NULL,
        sort_order INTEGER NOT NULL,
        PRIMARY KEY(scenario_version, scenario_id),
        UNIQUE(scenario_version, sort_order),
        UNIQUE(scenario_version, dispatch_key)
    )""",
    """CREATE TABLE acceptance_runs (
        acceptance_run_id TEXT PRIMARY KEY,
        scenario_id TEXT NOT NULL,
        scenario_version TEXT NOT NULL,
        review_run_id TEXT REFERENCES runs(run_id),
        dispatch_key TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending'
            CHECK(state IN ('pending', 'running', 'pass', 'findings',
                            'incomplete', 'stale', 'error')),
        details_json TEXT NOT NULL,
        error TEXT NOT NULL,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        FOREIGN KEY(scenario_version, scenario_id)
            REFERENCES acceptance_scenarios(scenario_version, scenario_id)
    )""",
    "CREATE INDEX acceptance_runs_scenario ON "
    "acceptance_runs(scenario_id, scenario_version, created_at, acceptance_run_id)",
    "CREATE INDEX acceptance_runs_review ON acceptance_runs(review_run_id)",
    """CREATE TABLE acceptance_results (
        result_id TEXT PRIMARY KEY,
        acceptance_run_id TEXT NOT NULL UNIQUE REFERENCES acceptance_runs(acceptance_run_id),
        state TEXT NOT NULL
            CHECK(state IN ('pass', 'findings', 'incomplete', 'stale', 'error')),
        evidence_json TEXT NOT NULL,
        error TEXT NOT NULL,
        details_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL
    )""",
    "CREATE INDEX acceptance_results_state ON acceptance_results(state, recorded_at, result_id)",
)

_SCHEMA_V4 = (
    """CREATE TABLE native_tasks (
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        unit_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        lens TEXT NOT NULL,
        identity_json TEXT NOT NULL,
        transport TEXT NOT NULL,
        registered_at TEXT NOT NULL,
        PRIMARY KEY(run_id, task_id),
        FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
        FOREIGN KEY(unit_id) REFERENCES units(unit_id) ON DELETE CASCADE
    )""",
    "CREATE INDEX native_tasks_scope ON native_tasks(run_id, unit_id, stage, lens, task_id)",
    """CREATE TABLE native_task_events (
        run_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        unit_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        lens TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN (
            'queued', 'started', 'completed', 'failed', 'warning', 'timeout', 'cancelled',
            'cleanup', 'finalization')),
        transport TEXT NOT NULL,
        native_session_id TEXT,
        occurred_at_unix REAL NOT NULL,
        started_at_unix REAL,
        finished_at_unix REAL,
        duration_seconds REAL,
        details_json TEXT NOT NULL,
        event_sha256 TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        PRIMARY KEY(run_id, event_id),
        FOREIGN KEY(run_id, task_id) REFERENCES native_tasks(run_id, task_id) ON DELETE CASCADE
    )""",
    "CREATE INDEX native_task_events_timeline ON "
    "native_task_events(run_id, task_id, occurred_at_unix, event_id)",
    "CREATE INDEX native_task_events_scope ON "
    "native_task_events(run_id, unit_id, stage, lens, event_type)",
    """CREATE TABLE native_artifacts (
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        artifact_sha256 TEXT NOT NULL,
        schema TEXT NOT NULL,
        transport TEXT NOT NULL,
        native_session_id TEXT NOT NULL,
        artifact_json TEXT NOT NULL,
        started_at_unix REAL NOT NULL,
        finished_at_unix REAL NOT NULL,
        duration_seconds REAL NOT NULL,
        recorded_at TEXT NOT NULL,
        PRIMARY KEY(run_id, task_id),
        UNIQUE(run_id, message_id),
        FOREIGN KEY(run_id, task_id) REFERENCES native_tasks(run_id, task_id) ON DELETE CASCADE
    )""",
    "CREATE INDEX native_artifacts_scope ON native_artifacts(run_id, task_id, artifact_sha256)",
    """CREATE TABLE native_artifact_messages (
        run_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        accepted INTEGER NOT NULL CHECK(accepted IN (0, 1)),
        reason TEXT NOT NULL,
        artifact_sha256 TEXT,
        recorded_at TEXT NOT NULL,
        PRIMARY KEY(run_id, message_id),
        UNIQUE(run_id, event_id),
        FOREIGN KEY(run_id, task_id) REFERENCES native_tasks(run_id, task_id) ON DELETE CASCADE,
        FOREIGN KEY(run_id, event_id)
            REFERENCES native_task_events(run_id, event_id) ON DELETE CASCADE
    )""",
    "CREATE INDEX native_artifact_messages_task ON "
    "native_artifact_messages(run_id, task_id, message_id)",
)


def resolve_store_path(path=None, *, environ=None):
    """Resolve the standalone database without consulting coding-tool state.

    An explicit path wins, then ``FINAL_REVIEW_STORE``, then the neutral
    ``~/.final-review/final-review.sqlite3`` default. This function performs no
    filesystem or repository inspection and contains no provider selection.
    """
    if path is None:
        environment = os.environ if environ is None else environ
        configured = str(environment.get("FINAL_REVIEW_STORE", "")).strip()
        path = configured or Path.home() / ".final-review" / "final-review.sqlite3"
    return Path(path).expanduser().absolute()


class FinalStore:
    """A persistent final-review SQLite store.

    ``path`` follows :func:`resolve_store_path` precedence. ``busy_timeout`` is
    per SQLite lock wait in seconds and ``busy_retries`` bounds additional
    BEGIN/COMMIT attempts. Instances may be shared by threads; independent
    processes coordinate through SQLite WAL and locks.
    """

    def __init__(self, path=None, busy_timeout=5.0, busy_retries=4, retry_delay=0.025):
        self.path = resolve_store_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self.busy_timeout = float(busy_timeout)
        self.busy_retries = int(busy_retries)
        self.retry_delay = float(retry_delay)
        if self.busy_timeout < 0 or self.busy_retries < 0 or self.retry_delay < 0:
            raise ValueError("busy timeout and retry settings must be non-negative")
        self._lock = threading.RLock()
        self._closed = False
        self._observability = {}
        self._conn = sqlite3.connect(
            str(self.path), timeout=self.busy_timeout, isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout * 1000)}")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._initialize()
        self.install_contract(rg_final_contract.contract_seed())
        self.install_acceptance(rg_final_acceptance.acceptance_seed())
        self.store_instance_id = self._load_store_instance_id()

    @property
    def connection(self):
        """The SQLite connection, for PRAGMA inspection and migration tooling."""
        return self._conn

    @property
    def schema_version(self):
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def _ensure_open(self):
        if self._closed:
            raise StoreError("final-review store is closed")

    def _is_busy(self, exc):
        message = str(exc).lower()
        return "locked" in message or "busy" in message

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._ensure_open()
            attempts = self.busy_retries + 1
            for attempt in range(attempts):
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as exc:
                    if not self._is_busy(exc):
                        raise
                    if attempt + 1 == attempts:
                        raise StoreBusyError("could not acquire final-review SQLite writer") from exc
                    time.sleep(self.retry_delay * (attempt + 1))
            try:
                yield
            except BaseException:
                self._conn.rollback()
                raise
            else:
                for attempt in range(attempts):
                    try:
                        self._conn.commit()
                        break
                    except sqlite3.OperationalError as exc:
                        if not self._is_busy(exc):
                            self._conn.rollback()
                            raise
                        if attempt + 1 == attempts:
                            self._conn.rollback()
                            raise StoreBusyError("could not commit final-review SQLite writer") from exc
                        time.sleep(self.retry_delay * (attempt + 1))

    def _initialize(self):
        existing_version = self.schema_version
        if existing_version > SCHEMA_VERSION:
            raise StoreError(
                f"database schema {existing_version} is newer than supported {SCHEMA_VERSION}"
            )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        with self._transaction():
            applied = {
                row[0] for row in self._conn.execute("SELECT version FROM schema_migrations")
            }
            if 1 not in applied:
                if existing_version not in (0, 1):
                    raise StoreError(f"unsupported database schema {existing_version}")
                if existing_version == 1:
                    raise StoreError("database schema version has no migration record")
                for statement in _SCHEMA_V1:
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                    (_now(),),
                )
                self._conn.execute("PRAGMA user_version = 1")
            if 2 not in applied:
                if existing_version not in (0, 1, 2):
                    raise StoreError(f"unsupported database schema {existing_version}")
                if existing_version == 2:
                    raise StoreError("database schema version has no migration record")
                for statement in _SCHEMA_V2:
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (2, ?)",
                    (_now(),),
                )
                self._conn.execute("PRAGMA user_version = 2")
            if 3 not in applied:
                if existing_version not in (0, 1, 2, 3):
                    raise StoreError(f"unsupported database schema {existing_version}")
                if existing_version == 3:
                    raise StoreError("database schema version has no migration record")
                for statement in _SCHEMA_V3:
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (3, ?)",
                    (_now(),),
                )
                self._conn.execute("PRAGMA user_version = 3")
            if 4 not in applied:
                if existing_version not in (0, 1, 2, 3, 4):
                    raise StoreError(f"unsupported database schema {existing_version}")
                if existing_version == 4:
                    raise StoreError("database schema version has no migration record")
                for statement in _SCHEMA_V4:
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (4, ?)",
                    (_now(),),
                )
                self._conn.execute("PRAGMA user_version = 4")
            self._conn.execute(
                "INSERT OR IGNORE INTO store_metadata(key, value) VALUES "
                "('store_instance_id', ?)", (_identifier(),)
            )

    def _load_store_instance_id(self):
        row = self._conn.execute(
            "SELECT value FROM store_metadata WHERE key = 'store_instance_id'"
        ).fetchone()
        if row is None:
            raise StoreError("database has no store_instance_id")
        return row[0]

    def _telemetry_for_run(self, run_id, supplied=None):
        """Return the bounded collector associated with one durable run.

        A second store process can append supersession/publication observations
        to an older run. Replay only the already-sanitized persisted event ring
        in that case, then restore its historical counters and sequence.
        """
        if supplied is not None:
            self._observability[run_id] = supplied
        collector = self._observability.get(run_id)
        if collector is not None:
            return collector
        row = self._conn.execute(
            "SELECT metadata_json FROM runs WHERE run_id = ?", (run_id,),
        ).fetchone()
        if row is None:
            raise StoreError(f"unknown run {run_id}")
        collector = rg_final_telemetry.FinalReviewTelemetry()
        persisted = _loads(row["metadata_json"]).get("observability") or {}
        for event in persisted.get("events", [])[-rg_final_telemetry.MAX_EVENTS:]:
            if not isinstance(event, dict):
                continue
            collector.record(event.get("kind"), {
                key: value for key, value in event.items()
                if key not in ("schema", "kind", "sequence")
            })
        counters = persisted.get("counters") or {}
        for name in ("accepted", "rejected", "dropped"):
            value = counters.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                setattr(collector, "_" + name, max(getattr(collector, "_" + name), value))
        by_kind = counters.get("by_kind") or {}
        if isinstance(by_kind, dict):
            collector._by_kind = Counter({
                kind: value for kind, value in by_kind.items()
                if kind in rg_final_telemetry.EVENT_KINDS
                and isinstance(value, int) and not isinstance(value, bool) and value >= 0
            })
        collector._sequence = collector._accepted
        self._observability[run_id] = collector
        return collector

    def _persist_observability(self, run_id):
        """Store a detached, size-checked snapshot in the existing run JSON."""
        collector = self._telemetry_for_run(run_id)
        snapshot = json.loads(collector.to_json())
        row = self._conn.execute(
            "SELECT metadata_json FROM runs WHERE run_id = ?", (run_id,),
        ).fetchone()
        if row is None:
            raise StoreError(f"unknown run {run_id}")
        metadata = _loads(row["metadata_json"])
        metadata["observability"] = snapshot
        self._conn.execute(
            "UPDATE runs SET metadata_json = ? WHERE run_id = ?",
            (_json(metadata), run_id),
        )

    def _observe(self, subject_run_id, recorder, **fields):
        """Emit and durably retain telemetry without affecting gate behavior."""
        collector = None
        try:
            collector = self._telemetry_for_run(subject_run_id)
            getattr(collector, recorder)(**fields)
            self._persist_observability(subject_run_id)
        except Exception:
            # Closed-schema rejection and internal instrumentation failures use
            # the existing rejection counter; neither may abort review evidence.
            try:
                collector = collector or self._telemetry_for_run(subject_run_id)
                collector.record("observability-internal-warning", {})
                self._persist_observability(subject_run_id)
            except Exception:
                pass

    def _observe_unit(self, unit_id, status):
        row = self._unit_row(unit_id)
        self._observe(
            row["run_id"], "unit_executed", unit_key=row["unit_key"],
            kind=row["kind"], status=status,
        )

    @staticmethod
    def _bounded_count(value, default=0):
        if isinstance(value, bool):
            return default
        try:
            return min(max(int(value), 0), rg_final_telemetry.MAX_COUNT)
        except (TypeError, ValueError, OverflowError):
            return default

    @staticmethod
    def _lens_telemetry_state(state):
        if state in ("completed", "completed_with_warnings", "incomplete", "stopped", "skipped"):
            return state
        return "completed" if state == "complete" else "incomplete"

    @staticmethod
    def _test_telemetry_state(state, coverage=""):
        if state in ("pass", "block", "absent", "findings", "degraded", "not_applicable",
                     "incomplete", "reproduced_failure", "flake", "uncovered", "skipped",
                     "unknown", "fail"):
            return state
        if state in ("passed", "success", "ok"):
            return "pass"
        if state in ("failed", "failure"):
            return "fail"
        if state in ("error", "timeout", "incomplete") or coverage in (
                "unknown", "uncovered", "incomplete", "error"):
            return "degraded" if coverage != "incomplete" else "incomplete"
        return "unknown"

    @staticmethod
    def _test_telemetry_coverage(execution, status):
        if execution == "required":
            return "absent" if status == "absent" else "declared"
        if status in ("declared", "existing_tests", "ephemeral_only", "uncovered", "unknown",
                      "not_applicable"):
            return status
        if status == "absent":
            return "absent"
        return "unknown"

    def _observe_test_coverage(self, unit_id, kind, status, details):
        if kind not in ("required_tests", "dynamic_tests"):
            return
        execution = "required" if kind == "required_tests" else "dynamic"
        row = self._unit_row(unit_id)
        marker = (row["run_id"], unit_id, execution)
        emitted = getattr(self, "_test_coverage_observed", set())
        self._test_coverage_observed = emitted
        if marker in emitted:
            return
        emitted.add(marker)
        details = details if isinstance(details, dict) else {}
        telemetry = details.get("telemetry") if isinstance(details.get("telemetry"), dict) else {}
        dynamic_state = details.get("state", status)
        coverage = self._test_telemetry_coverage(execution, status)
        if execution == "dynamic" and coverage == "unknown" and status == "not_applicable":
            coverage = "not_applicable"
        evidence_source = ("existing_test" if coverage == "existing_tests" else
                           "ephemeral_test" if coverage == "ephemeral_only" else "")
        self._observe(
            row["run_id"], "tests_executed", unit_key=row["unit_key"],
            execution=execution,
            status=self._test_telemetry_state(dynamic_state, coverage), coverage=coverage,
            executed=self._bounded_count(telemetry.get("runs", 0)),
            attempts=self._bounded_count(
                telemetry.get("executor_attempts", telemetry.get("selector_attempts", 0))),
            findings=self._bounded_count(details.get("finding_count", 0)),
            warnings=self._bounded_count(details.get("warning_count", 0)),
            evidence_source=evidence_source,
        )

    def store_identity(self):
        """Inspect the explicit boundary for ordering and finding continuity."""
        self._ensure_open()
        return {
            "store_instance_id": self.store_instance_id,
            "schema_version": self.schema_version,
            "continuity_scope": "store",
            "path": str(self.path),
        }

    def close(self):
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    @staticmethod
    def _contract_hash(document):
        return hashlib.sha256(_json(document).encode("utf-8")).hexdigest()

    def install_contract(self, document):
        """Install an additive, immutable contract version idempotently."""
        version = _required_text(document, "version")
        implementation = _required_text(document, "implementation_version")
        rules = document.get("rules")
        decisions = document.get("decision_log")
        if not isinstance(rules, list) or not rules:
            raise ValueError("contract rules must be a non-empty list")
        if not isinstance(decisions, list):
            raise ValueError("contract decision_log must be a list")
        normalized = {
            "version": version,
            "implementation_version": implementation,
            "rules": [dict(rule) for rule in rules],
            "decision_log": [dict(item) for item in decisions],
        }
        digest = self._contract_hash(normalized)
        with self._transaction():
            row = self._conn.execute(
                "SELECT document_hash FROM contract_versions WHERE version = ?", (version,)
            ).fetchone()
            if row is not None:
                if row[0] != digest:
                    raise ValueError(f"contract version {version} is immutable")
                return
            self._conn.execute("UPDATE contract_versions SET current = 0 WHERE current = 1")
            self._conn.execute(
                "INSERT INTO contract_versions(version, implementation_version, document_hash, "
                "current, installed_at) VALUES (?, ?, ?, 1, ?)",
                (version, implementation, digest, _now()),
            )
            for order, rule in enumerate(rules):
                self._conn.execute(
                    "INSERT INTO contract_rules(contract_version, rule_key, title, body, "
                    "sort_order) VALUES (?, ?, ?, ?, ?)",
                    (version, _required_text(rule, "rule_key"), _required_text(rule, "title"),
                     _required_text(rule, "body"), order),
                )
            for order, decision in enumerate(decisions):
                self._conn.execute(
                    "INSERT INTO contract_decisions(contract_version, decision_key, title, body, "
                    "sort_order) VALUES (?, ?, ?, ?, ?)",
                    (version, _required_text(decision, "decision_key"),
                     _required_text(decision, "title"), _required_text(decision, "body"), order),
                )

    def contract_view(self, version=None):
        """Return a detached current or historical contract; missing is ``{}``."""
        with self._lock:
            return self._contract_view(version)

    def _contract_view(self, version=None):
        self._ensure_open()
        if version is None:
            row = self._conn.execute(
                "SELECT version FROM contract_versions WHERE current = 1"
            ).fetchone()
            if row is None:
                return {}
            version = row[0]
        header = self._conn.execute(
            "SELECT version, implementation_version FROM contract_versions WHERE version = ?",
            (version,),
        ).fetchone()
        if header is None:
            return {}
        rules = [
            {"rule_key": row["rule_key"], "title": row["title"], "body": row["body"]}
            for row in self._conn.execute(
                "SELECT rule_key, title, body FROM contract_rules "
                "WHERE contract_version = ? ORDER BY sort_order", (version,)
            )
        ]
        decisions = [
            {"decision_key": row["decision_key"], "title": row["title"], "body": row["body"]}
            for row in self._conn.execute(
                "SELECT decision_key, title, body FROM contract_decisions "
                "WHERE contract_version = ? ORDER BY sort_order", (version,)
            )
        ]
        return {
            "version": header["version"],
            "implementation_version": header["implementation_version"],
            "rules": rules,
            "decision_log": decisions,
        }

    @staticmethod
    def _normalize_acceptance(document):
        version = _required_text(document, "version")
        implementation = _required_text(document, "implementation_version")
        supplied = document.get("scenarios")
        if not isinstance(supplied, list) or not supplied:
            raise ValueError("acceptance scenarios must be a non-empty list")
        scenarios = []
        identifiers = set()
        dispatch_keys = set()
        for order, raw in enumerate(supplied):
            if not isinstance(raw, dict):
                raise ValueError("acceptance scenario must be a mapping")
            scenario_id = _required_text(raw, "scenario_id")
            dispatch_key = _required_text(raw, "dispatch_key")
            if scenario_id in identifiers:
                raise ValueError(f"duplicate acceptance scenario {scenario_id}")
            if dispatch_key in dispatch_keys:
                raise ValueError(f"duplicate acceptance dispatch key {dispatch_key}")
            identifiers.add(scenario_id)
            dispatch_keys.add(dispatch_key)
            expected_state = _required_text(raw, "expected_state")
            if expected_state not in rg_final_acceptance.RESULT_STATES:
                raise ValueError(f"invalid expected acceptance state {expected_state}")
            evidence = raw.get("required_evidence")
            if not isinstance(evidence, (list, tuple)) or not evidence or \
                    any(not str(item).strip() for item in evidence):
                raise ValueError(f"scenario {scenario_id} requires non-empty evidence keys")
            evidence = [str(item).strip() for item in evidence]
            if len(set(evidence)) != len(evidence):
                raise ValueError(f"scenario {scenario_id} has duplicate evidence keys")
            assertions = {}
            for name in ("cleanup_assertions", "warning_assertions", "trust_assertions"):
                values = raw.get(name)
                if not isinstance(values, (list, tuple)) or not values or \
                        any(not str(item).strip() for item in values):
                    raise ValueError(f"scenario {scenario_id} requires {name}")
                assertions[name] = [str(item).strip() for item in values]
            scenarios.append({
                "scenario_id": scenario_id,
                "title": _required_text(raw, "title"),
                "behavior": _required_text(raw, "behavior"),
                "expected_state": expected_state,
                "required_evidence": evidence,
                "dispatch_key": dispatch_key,
                "assertions": assertions,
                "sort_order": order,
            })
        return {
            "version": version,
            "implementation_version": implementation,
            "scenarios": scenarios,
        }

    def install_acceptance(self, document):
        """Install an immutable acceptance version without creating any runs."""
        normalized = self._normalize_acceptance(document)
        version = normalized["version"]
        digest = self._contract_hash(normalized)
        with self._transaction():
            existing = self._conn.execute(
                "SELECT document_hash FROM acceptance_versions WHERE version = ?", (version,)
            ).fetchone()
            if existing is not None:
                if existing[0] != digest:
                    raise ValueError(f"acceptance version {version} is immutable")
                return
            self._conn.execute("UPDATE acceptance_versions SET current = 0 WHERE current = 1")
            self._conn.execute(
                "INSERT INTO acceptance_versions(version, implementation_version, "
                "document_hash, current, installed_at) VALUES (?, ?, ?, 1, ?)",
                (version, normalized["implementation_version"], digest, _now()),
            )
            for scenario in normalized["scenarios"]:
                assertions = scenario["assertions"]
                self._conn.execute(
                    "INSERT INTO acceptance_scenarios(scenario_version, scenario_id, title, "
                    "behavior, expected_state, required_evidence_json, dispatch_key, "
                    "assertions_json, sort_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (version, scenario["scenario_id"], scenario["title"],
                     scenario["behavior"], scenario["expected_state"],
                     _json(scenario["required_evidence"]), scenario["dispatch_key"],
                     _json(assertions), scenario["sort_order"]),
                )

    @staticmethod
    def _acceptance_scenario(row):
        assertions = _loads(row["assertions_json"])
        return {
            "scenario_id": row["scenario_id"],
            "version": row["scenario_version"],
            "title": row["title"],
            "behavior": row["behavior"],
            "expected_state": row["expected_state"],
            "required_evidence": _loads(row["required_evidence_json"]),
            "dispatch_key": row["dispatch_key"],
            "cleanup_assertions": assertions["cleanup_assertions"],
            "warning_assertions": assertions["warning_assertions"],
            "trust_assertions": assertions["trust_assertions"],
        }

    def resolve_acceptance_version(self, version=None):
        """Return an installed acceptance identity, defaulting to the current definition.

        This read-only seam lets supervisors select the same identity for a new review
        run and its linked acceptance attempts without importing a version constant.
        No acceptance result is required or created.
        """
        with self._lock:
            self._ensure_open()
            if version is None:
                current = self._conn.execute(
                    "SELECT version FROM acceptance_versions WHERE current = 1"
                ).fetchone()
                if current is None:
                    raise StoreError("no current acceptance registry installed")
                return current[0]
            version = str(version).strip()
            installed = self._conn.execute(
                "SELECT 1 FROM acceptance_versions WHERE version = ?", (version,)
            ).fetchone()
            if installed is None:
                raise StoreError(f"unknown acceptance version {version}")
            return version

    def acceptance_view(self, version=None):
        """Return a detached current or historical registry; missing is ``{}``."""
        with self._lock:
            self._ensure_open()
            if version is None:
                current = self._conn.execute(
                    "SELECT version FROM acceptance_versions WHERE current = 1"
                ).fetchone()
                if current is None:
                    return {}
                version = current[0]
            header = self._conn.execute(
                "SELECT version, implementation_version FROM acceptance_versions WHERE version = ?",
                (version,),
            ).fetchone()
            if header is None:
                return {}
            scenarios = [
                self._acceptance_scenario(row) for row in self._conn.execute(
                    "SELECT * FROM acceptance_scenarios WHERE scenario_version = ? "
                    "ORDER BY sort_order", (version,)
                )
            ]
            return {
                "schema": rg_final_acceptance.ACCEPTANCE_SCHEMA,
                "version": header["version"],
                "implementation_version": header["implementation_version"],
                "scenarios": scenarios,
            }

    def start_acceptance_run(self, scenario_id, *, version=None, review_run_id=None,
                             details=None):
        """Create a distinct running acceptance attempt for an installed scenario."""
        scenario_id = str(scenario_id).strip()
        if not scenario_id:
            raise ValueError("missing required scenario_id")
        acceptance_run_id = _identifier()
        with self._transaction():
            version = self.resolve_acceptance_version(version)
            scenario = self._conn.execute(
                "SELECT dispatch_key FROM acceptance_scenarios "
                "WHERE scenario_version = ? AND scenario_id = ?", (version, scenario_id)
            ).fetchone()
            if scenario is None:
                raise StoreError(f"unknown acceptance scenario {scenario_id} at version {version}")
            if review_run_id is not None and self._conn.execute(
                    "SELECT 1 FROM runs WHERE run_id = ?", (review_run_id,)).fetchone() is None:
                raise StoreError(f"unknown run {review_run_id}")
            now = _now()
            self._conn.execute(
                "INSERT INTO acceptance_runs(acceptance_run_id, scenario_id, scenario_version, "
                "review_run_id, dispatch_key, state, details_json, error, created_at, started_at) "
                "VALUES (?, ?, ?, ?, ?, 'running', ?, '', ?, ?)",
                (acceptance_run_id, scenario_id, version, review_run_id,
                 scenario["dispatch_key"], _json(details), now, now),
            )
            row = self._conn.execute(
                "SELECT * FROM acceptance_runs WHERE acceptance_run_id = ?",
                (acceptance_run_id,),
            ).fetchone()
            return self._acceptance_run_public(row)

    def complete_acceptance_run(self, acceptance_run_id, state, *, evidence=None,
                                error="", details=None):
        """Atomically record an independent result and terminal attempt state."""
        if state not in rg_final_acceptance.RESULT_STATES:
            raise ValueError(f"invalid acceptance state {state}")
        error = str(error).strip()
        if state == "error" and not error:
            raise ValueError("error acceptance results require a human-readable error")
        result_id = _identifier()
        with self._transaction():
            run = self._conn.execute(
                "SELECT * FROM acceptance_runs WHERE acceptance_run_id = ?",
                (acceptance_run_id,),
            ).fetchone()
            if run is None:
                raise StoreError(f"unknown acceptance run {acceptance_run_id}")
            if run["state"] not in ("pending", "running"):
                raise StoreError(f"acceptance run {acceptance_run_id} is already complete")
            scenario = self._conn.execute(
                "SELECT * FROM acceptance_scenarios WHERE scenario_version = ? AND scenario_id = ?",
                (run["scenario_version"], run["scenario_id"]),
            ).fetchone()
            required = _loads(scenario["required_evidence_json"])
            if state in ("pass", "findings", "stale"):
                if not isinstance(evidence, dict):
                    raise ValueError(f"{state} acceptance results require evidence")
                missing = [key for key in required if not self._evidence_present(evidence.get(key))]
                if missing:
                    raise ValueError(
                        f"{state} acceptance result is missing evidence: {', '.join(missing)}"
                    )
            now = _now()
            self._conn.execute(
                "INSERT INTO acceptance_results(result_id, acceptance_run_id, state, "
                "evidence_json, error, details_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (result_id, acceptance_run_id, state, _json(evidence), error,
                 _json(details), now),
            )
            self._conn.execute(
                "UPDATE acceptance_runs SET state = ?, error = ?, details_json = ?, "
                "completed_at = ? WHERE acceptance_run_id = ?",
                (state, error, _json(details), now, acceptance_run_id),
            )
            row = self._conn.execute(
                "SELECT * FROM acceptance_runs WHERE acceptance_run_id = ?",
                (acceptance_run_id,),
            ).fetchone()
            return self._acceptance_run_public(row)

    def _acceptance_run_public(self, row):
        scenario = self._conn.execute(
            "SELECT * FROM acceptance_scenarios WHERE scenario_version = ? AND scenario_id = ?",
            (row["scenario_version"], row["scenario_id"]),
        ).fetchone()
        result = self._conn.execute(
            "SELECT * FROM acceptance_results WHERE acceptance_run_id = ?",
            (row["acceptance_run_id"],),
        ).fetchone()
        return {
            "acceptance_run_id": row["acceptance_run_id"],
            "scenario_id": row["scenario_id"],
            "version": row["scenario_version"],
            "review_run_id": row["review_run_id"],
            "dispatch_key": row["dispatch_key"],
            "state": row["state"],
            "details": _loads(row["details_json"]),
            "error": row["error"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "scenario": self._acceptance_scenario(scenario),
            "result": None if result is None else {
                "result_id": result["result_id"],
                "acceptance_run_id": result["acceptance_run_id"],
                "state": result["state"],
                "evidence": _loads(result["evidence_json"]),
                "error": result["error"],
                "details": _loads(result["details_json"]),
                "recorded_at": result["recorded_at"],
            },
        }

    def acceptance_run(self, acceptance_run_id):
        with self._lock:
            self._ensure_open()
            row = self._conn.execute(
                "SELECT * FROM acceptance_runs WHERE acceptance_run_id = ?",
                (acceptance_run_id,),
            ).fetchone()
            if row is None:
                raise StoreError(f"unknown acceptance run {acceptance_run_id}")
            return self._acceptance_run_public(row)

    def acceptance_history(self, *, scenario_id=None, version=None, review_run_id=None):
        """Return independent attempts oldest first, including retained results."""
        with self._lock:
            self._ensure_open()
            clauses = []
            parameters = []
            if scenario_id is not None:
                clauses.append("scenario_id = ?")
                parameters.append(str(scenario_id))
            if version is not None:
                clauses.append("scenario_version = ?")
                parameters.append(str(version))
            if review_run_id is not None:
                clauses.append("review_run_id = ?")
                parameters.append(str(review_run_id))
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = self._conn.execute(
                "SELECT * FROM acceptance_runs" + where +
                " ORDER BY created_at, acceptance_run_id", tuple(parameters),
            ).fetchall()
            return [self._acceptance_run_public(row) for row in rows]

    @staticmethod
    def _normalize_stack(stack):
        if not isinstance(stack, dict):
            raise ValueError("stack must be a mapping")
        normalized = {
            "provider": _required_text(stack, "provider"),
            "target_project": _required_text(stack, "target_project"),
            "root_target_ref": _required_text(stack, "root_target_ref"),
            "root_target_sha": _sha(stack.get("root_target_sha"), "root_target_sha"),
        }
        items = stack.get("items")
        if not isinstance(items, (list, tuple)) or not items:
            raise ValueError("stack must contain at least one item")
        normalized_items = []
        seen = set()
        for position, item in enumerate(items, 1):
            source_sha = _sha(item.get("source_sha"), "source_sha")
            target_sha = _sha(item.get("target_sha"), "target_sha")
            item_key = _required_text(item, "item_key")
            if item_key in seen:
                raise ValueError(f"duplicate item_key {item_key}")
            seen.add(item_key)
            provider_data = dict(item.get("provider_data") or {})
            provider_target = provider_data.get("target") or {}
            base_sha = item.get("base_sha", provider_target.get("base_sha"))
            if base_sha is not None:
                base_sha = _sha(base_sha, "base_sha")
            ancestors = item.get("ancestors", provider_data.get("ancestors", []))
            if not isinstance(ancestors, (list, tuple)):
                raise ValueError("ancestors must be a list of provider revision items")
            reference = str(item.get("reference") or provider_data.get("url") or "").strip()
            normalized_items.append({
                "position": position,
                "item_key": item_key,
                "source_project": _required_text(item, "source_project"),
                "source_ref": _required_text(item, "source_ref"),
                "source_sha": source_sha,
                "target_project": _required_text(item, "target_project"),
                "target_ref": _required_text(item, "target_ref"),
                "target_sha": target_sha,
                "base_sha": base_sha,
                "reference": reference or None,
                "ancestors": list(ancestors),
                "provider_data": provider_data,
            })
        normalized["items"] = normalized_items
        return normalized

    @staticmethod
    def _stack_key(stack):
        identity = {
            "provider": stack["provider"],
            "target_project": stack["target_project"],
            "root_target_ref": stack["root_target_ref"],
            "items": [
                {"item_key": item["item_key"], "source_project": item["source_project"]}
                for item in stack["items"]
            ],
        }
        return hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()

    @staticmethod
    def _trust(independent, continuity_required, supplied=None):
        trust = dict(supplied or {})
        trust.update({
            "continuity_scope": "store",
            "independent": bool(independent),
            "continuity_available": not bool(independent),
            "local_only": bool(trust.get("local_only", True)),
            "enforced": bool(trust.get("enforced", False)),
        })
        if continuity_required and independent:
            trust["local_only"] = True
            trust["enforced"] = False
            trust["degraded_reason"] = "fresh store has no same-store history"
        return trust

    def start_run(self, stack, contract_version=None, acceptance_version=None,
                  continuity_required=False, metadata=None, telemetry=None):
        """Atomically persist a run, its immutable revisions, and initial units.

        The returned identity includes ``unit_ids`` keyed by ``individual:N``
        and ``combined``. Unit and run states begin as ``incomplete``. No run
        is removed when a newer-started run supersedes it.
        """
        stack = self._normalize_stack(stack)
        if contract_version is None:
            current = self.contract_view()
            if not current:
                raise StoreError("no current contract installed")
            contract_version = current["version"]
        if self.contract_view(contract_version) == {}:
            raise StoreError(f"unknown contract version {contract_version}")
        acceptance_version = self.resolve_acceptance_version(acceptance_version)
        stack_key = self._stack_key(stack)
        run_id = _identifier()
        units = []
        for item in stack["items"]:
            units.append((
                _identifier(), f"individual:{item['position']}", "individual", item["position"],
                item["target_sha"], item["source_sha"], item["target_ref"], item["source_ref"],
            ))
        units.append((
            _identifier(), "combined", "combined", None, stack["root_target_sha"],
            stack["items"][-1]["source_sha"], stack["root_target_ref"], None,
        ))
        with self._transaction():
            prior = self._conn.execute(
                "SELECT count(*) FROM runs WHERE stack_id = ?", (stack_key,)
            ).fetchone()[0]
            independent = prior == 0
            run_seq = self._conn.execute(
                "SELECT COALESCE(MAX(run_seq), 0) + 1 FROM runs"
            ).fetchone()[0]
            self._conn.execute(
                "INSERT OR IGNORE INTO stacks(stack_id, provider, target_project, "
                "root_target_ref, created_at) VALUES (?, ?, ?, ?, ?)",
                (stack_key, stack["provider"], stack["target_project"],
                 stack["root_target_ref"], _now()),
            )
            self._conn.execute(
                "INSERT INTO runs(run_id, run_seq, stack_id, contract_version, "
                "acceptance_version, state, continuity_required, independent, started_at, "
                "metadata_json, trust_json) VALUES (?, ?, ?, ?, ?, 'incomplete', ?, ?, ?, ?, ?)",
                (run_id, run_seq, stack_key, contract_version, acceptance_version,
                 int(bool(continuity_required)), int(independent), _now(), _json(metadata),
                 _json(self._trust(independent, continuity_required))),
            )
            for item in stack["items"]:
                self._conn.execute(
                    "INSERT INTO run_items(run_id, position, item_key, source_project, "
                    "source_ref, source_sha, target_project, target_ref, target_sha, "
                    "provider_data_json, base_sha, reference, ancestors_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, item["position"], item["item_key"], item["source_project"],
                     item["source_ref"], item["source_sha"], item["target_project"],
                     item["target_ref"], item["target_sha"], _json(item["provider_data"]),
                     item["base_sha"], item["reference"], _json(item["ancestors"])),
                )
            unit_ids = {}
            for unit_id, key, kind, position, base, head, target_ref, source_ref in units:
                unit_ids[key] = unit_id
                self._conn.execute(
                    "INSERT INTO units(unit_id, run_id, unit_key, kind, item_position, base_sha, "
                    "head_sha, target_ref, source_ref, metadata_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)",
                    (unit_id, run_id, key, kind, position, base, head,
                     target_ref, source_ref, _now()),
                )
            self._conn.execute(
                "UPDATE runs SET superseded_by_run_id = ? WHERE stack_id = ? AND run_id != ? "
                "AND run_seq < ? AND superseded_by_run_id IS NULL",
                (run_id, stack_key, run_id, run_seq),
            )
            trust = self._trust(independent, continuity_required)
            self._observability[run_id] = (
                telemetry or rg_final_telemetry.FinalReviewTelemetry())
            self._observe(
                run_id, "store_bound", store_instance_id=self.store_instance_id,
                run_id=run_id, run_seq=run_seq,
                continuity_required=bool(continuity_required), independent=independent,
                continuity_available=not independent,
                enforced=bool(trust.get("enforced")), official=bool(trust.get("official")),
            )
            for _unit_id, key, kind, _position, _base, _head, _target_ref, _source_ref in units:
                self._observe(run_id, "unit_executed", unit_key=key, kind=kind,
                              status="incomplete")
            superseded = self._rows(
                "SELECT run_id FROM runs WHERE superseded_by_run_id = ?", (run_id,))
            for prior_run in superseded:
                self._observe(
                    prior_run["run_id"], "run_superseded",
                    store_instance_id=self.store_instance_id, run_id=prior_run["run_id"],
                    superseded_by_run_id=run_id, publication_blocked=True,
                )
        return {
            "run_id": run_id,
            "run_seq": run_seq,
            "stack_key": stack_key,
            "store_instance_id": self.store_instance_id,
            "contract_version": contract_version,
            "acceptance_version": acceptance_version,
            "unit_ids": unit_ids,
            "trust": trust,
        }

    def _native_task_row(self, run_id, task_id):
        row = self._conn.execute(
            "SELECT * FROM native_tasks WHERE run_id = ? AND task_id = ?",
            (run_id, task_id),
        ).fetchone()
        if row is None:
            raise StoreError(f"unknown native task {task_id} in run {run_id}")
        return row

    @staticmethod
    def _native_event_public(row, identity):
        return {
            "schema": NATIVE_EVENT_SCHEMA,
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "identity": dict(identity),
            "transport": row["transport"],
            "native_session_id": row["native_session_id"],
            "timing": {
                "occurred_at_unix": row["occurred_at_unix"],
                "started_at_unix": row["started_at_unix"],
                "finished_at_unix": row["finished_at_unix"],
                "duration_seconds": row["duration_seconds"],
            },
            "details": _loads(row["details_json"]),
            "recorded_at": row["recorded_at"],
        }

    def _native_task_public(self, row):
        identity = _loads(row["identity_json"])
        events = [
            self._native_event_public(event, identity)
            for event in self._rows(
                "SELECT * FROM native_task_events WHERE run_id = ? AND task_id = ? "
                "ORDER BY occurred_at_unix, event_id", (row["run_id"], row["task_id"]),
            )
        ]
        artifacts = [
            {
                "message_id": item["message_id"],
                "artifact_sha256": item["artifact_sha256"],
                "artifact": _loads(item["artifact_json"]),
                "recorded_at": item["recorded_at"],
            }
            for item in self._rows(
                "SELECT * FROM native_artifacts WHERE run_id = ? AND task_id = ? "
                "ORDER BY message_id", (row["run_id"], row["task_id"]),
            )
        ]
        messages = [
            {
                "message_id": item["message_id"],
                "event_id": item["event_id"],
                "accepted": bool(item["accepted"]),
                "reason": item["reason"] or None,
                "artifact_sha256": item["artifact_sha256"],
                "recorded_at": item["recorded_at"],
            }
            for item in self._rows(
                "SELECT * FROM native_artifact_messages WHERE run_id = ? AND task_id = ? "
                "ORDER BY message_id", (row["run_id"], row["task_id"]),
            )
        ]
        return {
            "schema": row["schema"] if "schema" in row.keys() else NATIVE_TASK_SCHEMA,
            "identity": identity,
            "transport": row["transport"],
            "registered_at": row["registered_at"],
            "events": events,
            "artifacts": artifacts,
            "artifact_messages": messages,
        }

    def _native_tasks_public(self, run_id):
        return [
            self._native_task_public(row)
            for row in self._rows(
                "SELECT * FROM native_tasks WHERE run_id = ? "
                "ORDER BY unit_id, stage, lens, task_id", (run_id,),
            )
        ]

    def register_native_task(self, task):
        """Register immutable supervisor-owned native identity idempotently."""
        normalized = _native_task(task)
        identity = normalized["identity"]
        identity_json = _json(identity)
        with self._transaction():
            unit = self._conn.execute(
                "SELECT run_id FROM units WHERE unit_id = ?", (identity["unit_id"],),
            ).fetchone()
            if unit is None or unit["run_id"] != identity["run_id"]:
                raise StoreError("native task unit does not belong to its run")
            existing = self._conn.execute(
                "SELECT * FROM native_tasks WHERE run_id = ? AND task_id = ?",
                (identity["run_id"], identity["task_id"]),
            ).fetchone()
            if existing is not None:
                if existing["identity_json"] != identity_json or \
                        existing["transport"] != normalized["transport"]:
                    raise StoreError("registered native task identity is immutable")
                return self._native_task_public(existing)
            self._conn.execute(
                "INSERT INTO native_tasks(run_id, task_id, unit_id, stage, lens, "
                "identity_json, transport, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (identity["run_id"], identity["task_id"], identity["unit_id"],
                 identity["stage"], identity["lens"], identity_json,
                 normalized["transport"], _now()),
            )
            return self._native_task_public(
                self._native_task_row(identity["run_id"], identity["task_id"]),
            )

    def _insert_native_event(self, normalized):
        """Insert one new normalized event inside the current write transaction."""
        identity = normalized["identity"]
        digest = hashlib.sha256(_json(normalized).encode("utf-8")).hexdigest()
        recorded_at = _now()
        self._conn.execute(
            "INSERT INTO native_task_events(run_id, event_id, task_id, unit_id, stage, "
            "lens, event_type, transport, native_session_id, occurred_at_unix, "
            "started_at_unix, finished_at_unix, duration_seconds, details_json, "
            "event_sha256, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (identity["run_id"], normalized["event_id"], identity["task_id"],
             identity["unit_id"], identity["stage"], identity["lens"],
             normalized["event_type"], normalized["transport"],
             normalized["native_session_id"], normalized["timing"]["occurred_at_unix"],
             normalized["timing"]["started_at_unix"], normalized["timing"]["finished_at_unix"],
             normalized["timing"]["duration_seconds"], _json(normalized["details"]),
             digest, recorded_at),
        )
        return self._conn.execute(
            "SELECT * FROM native_task_events WHERE run_id = ? AND event_id = ?",
            (identity["run_id"], normalized["event_id"]),
        ).fetchone()

    def record_native_event(self, event):
        """Append a deterministic lifecycle edge once, including exact stage timing."""
        normalized = _native_event(event)
        identity = normalized["identity"]
        with self._transaction():
            task = self._native_task_row(identity["run_id"], identity["task_id"])
            if _loads(task["identity_json"]) != identity or \
                    task["transport"] != normalized["transport"]:
                raise StoreError("native event identity does not match its registered task")
            existing = self._conn.execute(
                "SELECT * FROM native_task_events WHERE run_id = ? AND event_id = ?",
                (identity["run_id"], normalized["event_id"]),
            ).fetchone()
            if existing is not None:
                digest = hashlib.sha256(_json(normalized).encode("utf-8")).hexdigest()
                if existing["event_sha256"] != digest:
                    raise StoreError("native event idempotent identity has conflicting content")
                return self._native_event_public(existing, identity)
            row = self._insert_native_event(normalized)
            return self._native_event_public(row, identity)

    def commit_native_artifact(self, identity, message_id, *, artifact, received_at_unix):
        """Commit a validated result, event and receipt as one idempotent write."""
        identity = _native_identity(identity)
        message_id = _required_text({"message_id": message_id}, "message_id")
        artifact = _native_artifact(artifact)
        received_at_unix = _finite_time(received_at_unix, "received_at_unix")
        if received_at_unix < artifact["timing"]["finished_at_unix"]:
            raise ValueError("native artifact was received before it finished")
        if artifact["identity"] != identity:
            raise StoreError("native artifact identity does not match its message")
        artifact_json = _json(artifact)
        artifact_digest = hashlib.sha256(artifact_json.encode("utf-8")).hexdigest()
        request_sha256 = hashlib.sha256(_json({
            "message_id": message_id, "identity": identity, "artifact": artifact,
        }).encode("utf-8")).hexdigest()
        event_id = f"artifact-commit:{message_id}"
        with self._transaction():
            task = self._native_task_row(identity["run_id"], identity["task_id"])
            if _loads(task["identity_json"]) != identity or \
                    task["transport"] != artifact["transport"]:
                raise StoreError("native artifact identity does not match its registered task")
            receipt = self._conn.execute(
                "SELECT * FROM native_artifact_messages WHERE run_id = ? AND message_id = ?",
                (identity["run_id"], message_id),
            ).fetchone()
            if receipt is not None:
                if receipt["request_sha256"] != request_sha256:
                    raise StoreError("native artifact message idempotent identity has conflicting content")
            else:
                existing_artifact = self._conn.execute(
                    "SELECT message_id FROM native_artifacts WHERE run_id = ? AND task_id = ?",
                    (identity["run_id"], identity["task_id"]),
                ).fetchone()
                if existing_artifact is not None:
                    raise StoreError("native task already has an accepted artifact")
                event = _native_event({
                    "schema": NATIVE_EVENT_SCHEMA,
                    "event_id": event_id,
                    "event_type": "completed",
                    "identity": identity,
                    "transport": artifact["transport"],
                    "native_session_id": artifact["native_session_id"],
                    "timing": {
                        "occurred_at_unix": artifact["timing"]["finished_at_unix"],
                        "started_at_unix": artifact["timing"]["started_at_unix"],
                        "finished_at_unix": artifact["timing"]["finished_at_unix"],
                        "duration_seconds": artifact["timing"]["duration_seconds"],
                    },
                    "details": {
                        "artifact_sha256": artifact_digest,
                        "message_id": message_id,
                    },
                })
                event_row = self._insert_native_event(event)
                recorded_at = _now()
                self._conn.execute(
                    "INSERT INTO native_artifacts(run_id, task_id, message_id, "
                    "artifact_sha256, schema, transport, native_session_id, artifact_json, "
                    "started_at_unix, finished_at_unix, duration_seconds, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (identity["run_id"], identity["task_id"], message_id, artifact_digest,
                     NATIVE_ARTIFACT_SCHEMA, artifact["transport"],
                     artifact["native_session_id"], artifact_json,
                     artifact["timing"]["started_at_unix"],
                     artifact["timing"]["finished_at_unix"],
                     artifact["timing"]["duration_seconds"], recorded_at),
                )
                self._conn.execute(
                    "INSERT INTO native_artifact_messages(run_id, message_id, task_id, "
                    "event_id, request_sha256, accepted, reason, artifact_sha256, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (identity["run_id"], message_id, identity["task_id"], event_id,
                     request_sha256, 1, "", artifact_digest, recorded_at),
                )
                receipt = self._conn.execute(
                    "SELECT * FROM native_artifact_messages WHERE run_id = ? AND message_id = ?",
                    (identity["run_id"], message_id),
                ).fetchone()
            artifact_row = self._conn.execute(
                "SELECT * FROM native_artifacts WHERE run_id = ? AND message_id = ?",
                (identity["run_id"], message_id),
            ).fetchone()
            event_row = self._conn.execute(
                "SELECT * FROM native_task_events WHERE run_id = ? AND event_id = ?",
                (identity["run_id"], receipt["event_id"]),
            ).fetchone()
            return {
                "schema": "native-review-artifact-commit/v1",
                "message_id": message_id,
                "accepted": bool(receipt["accepted"]),
                "reason": receipt["reason"] or None,
                "artifact": {
                    "message_id": message_id,
                    "artifact_sha256": artifact_row["artifact_sha256"],
                    "artifact": _loads(artifact_row["artifact_json"]),
                    "recorded_at": artifact_row["recorded_at"],
                },
                "event": self._native_event_public(event_row, identity),
            }

    def _unit_row(self, unit_id):
        row = self._conn.execute("SELECT * FROM units WHERE unit_id = ?", (unit_id,)).fetchone()
        if row is None:
            raise StoreError(f"unknown unit {unit_id}")
        return row

    def add_unit(self, run_id, unit_key, kind, base_sha, head_sha, item_position=None,
                 target_ref=None, source_ref=None, metadata=None):
        """Add a prospective or replacement unit without changing revision history."""
        if kind not in ("individual", "combined", "prospective"):
            raise ValueError("invalid unit kind")
        unit_id = _identifier()
        with self._transaction():
            if self._conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is None:
                raise StoreError(f"unknown run {run_id}")
            self._conn.execute(
                "INSERT INTO units(unit_id, run_id, unit_key, kind, item_position, base_sha, "
                "head_sha, target_ref, source_ref, metadata_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (unit_id, run_id, str(unit_key), kind, item_position,
                 _sha(base_sha, "base_sha"), _sha(head_sha, "head_sha"), target_ref,
                 source_ref, _json(metadata), _now()),
            )
            self._observe(run_id, "unit_executed", unit_key=str(unit_key), kind=kind,
                          status="incomplete")
        return unit_id

    def set_unit_state(self, unit_id, state, summary=None):
        if state not in FINAL_STATES:
            raise ValueError(f"invalid final state {state}")
        with self._transaction():
            self._unit_row(unit_id)
            if summary is None:
                cursor = self._conn.execute(
                    "UPDATE units SET state = ? WHERE unit_id = ?", (state, unit_id)
                )
            else:
                cursor = self._conn.execute(
                    "UPDATE units SET state = ?, summary = ? WHERE unit_id = ?",
                    (state, str(summary), unit_id),
                )
            if cursor.rowcount != 1:
                raise StoreError(f"unknown unit {unit_id}")
            row = self._unit_row(unit_id)
            self._observe(row["run_id"], "unit_executed", unit_key=row["unit_key"],
                          kind=row["kind"], status=state)

    def finish_run(self, run_id, state, trust=None):
        """Record a supervisor-computed final state and effective trust qualifiers."""
        if state not in FINAL_STATES:
            raise ValueError(f"invalid final state {state}")
        with self._transaction():
            row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise StoreError(f"unknown run {run_id}")
            effective = self._trust(row["independent"], row["continuity_required"], trust)
            self._conn.execute(
                "UPDATE runs SET state = ?, completed_at = ?, trust_json = ? WHERE run_id = ?",
                (state, _now(), _json(effective), run_id),
            )
        return effective

    def record_lens(self, unit_id, name, state, summary="", evidence=None):
        lens_id = _identifier()
        with self._transaction():
            self._unit_row(unit_id)
            self._conn.execute(
                "INSERT INTO lenses(lens_id, unit_id, name, state, summary, evidence_json, "
                "recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (lens_id, unit_id, _required_text({"name": name}, "name"),
                 _required_text({"state": state}, "state"), str(summary), _json(evidence), _now()),
            )
            row = self._unit_row(unit_id)
            details = evidence if isinstance(evidence, dict) else {}
            self._observe(
                row["run_id"], "lens_executed", unit_key=row["unit_key"], lens=str(name),
                status=self._lens_telemetry_state(state),
                findings=self._bounded_count(details.get("finding_count", 0)),
                warnings=self._bounded_count(details.get("warning_count", 0)),
                unreviewed=self._bounded_count(len(details.get("unreviewed") or ())),
            )
        return lens_id

    def record_test(self, unit_id, name, kind, state, output="", evidence_fingerprint="",
                    details=None):
        test_id = _identifier()
        with self._transaction():
            self._unit_row(unit_id)
            self._conn.execute(
                "INSERT INTO test_evidence(test_id, unit_id, name, kind, state, output, "
                "evidence_fingerprint, details_json, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (test_id, unit_id, str(name), str(kind), str(state), str(output),
                 str(evidence_fingerprint), _json(details), _now()),
            )
            row = self._unit_row(unit_id)
            execution = "required" if kind == "required" else "dynamic"
            details = details if isinstance(details, dict) else {}
            attempts = details.get("attempts") or ()
            evidence_source = details.get("evidence_source") or ""
            if evidence_source not in ("", "existing_test", "ephemeral_test"):
                evidence_source = ""
            coverage = self._test_telemetry_coverage(execution, state)
            if execution == "dynamic" and evidence_source:
                coverage = ("existing_tests" if evidence_source == "existing_test"
                            else "ephemeral_only")
            self._observe(
                row["run_id"], "tests_executed", unit_key=row["unit_key"],
                execution=execution,
                status=self._test_telemetry_state(state), coverage=coverage,
                executed=1, attempts=self._bounded_count(len(attempts), 1),
                findings=0, warnings=0, evidence_source=evidence_source,
            )
        return test_id

    def test_evidence_for_unit(self, unit_id):
        """Return detached test rows for supervisor closure corroboration."""
        with self._lock:
            self._unit_row(unit_id)
            return [
                {"test_id": row["test_id"], "name": row["name"], "kind": row["kind"],
                 "state": row["state"], "output": row["output"],
                 "evidence_fingerprint": row["evidence_fingerprint"],
                 "details": _loads(row["details_json"]), "recorded_at": row["recorded_at"]}
                for row in self._rows(
                    "SELECT * FROM test_evidence WHERE unit_id = ? "
                    "ORDER BY recorded_at, test_id", (unit_id,))
            ]

    def _insert_finding_row(self, unit_id, finding_key, content, severity, confidence,
                            state, lens, path, line, anchors, evidence):
        finding_id = _identifier()
        self._conn.execute(
            "INSERT INTO findings(finding_id, unit_id, finding_key, content, severity, "
            "confidence, state, lens, path, line, evidence_json, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (finding_id, unit_id, str(finding_key), str(content), str(severity),
             str(confidence), str(state), lens, path, line, _json(evidence), _now()),
        )
        for anchor in anchors:
            normalized = rg_final_memory.normalize_anchors([anchor])
            item = normalized[0] if normalized[0]["anchor_type"] != "content" or \
                str(anchor.get("anchor_type") or "") == "content" else normalized[1]
            self._conn.execute(
                "INSERT INTO finding_anchors(anchor_id, finding_id, anchor_type, anchor, "
                "evidence_fingerprint, details_json) VALUES (?, ?, ?, ?, ?, ?)",
                (_identifier(), finding_id, item["anchor_type"], item["anchor"],
                 item["evidence_fingerprint"], _json(item["details"])),
            )
        return finding_id

    def _finding_by_id(self, finding_id):
        item = self._conn.execute(
            "SELECT * FROM findings WHERE finding_id = ?", (finding_id,)
        ).fetchone()
        if item is None:
            raise StoreError(f"unknown finding {finding_id}")
        finding = {
            "finding_id": item["finding_id"], "finding_key": item["finding_key"],
            "content": item["content"], "severity": item["severity"],
            "confidence": item["confidence"], "state": item["state"],
            "lens": item["lens"], "path": item["path"], "line": item["line"],
            "evidence": _loads(item["evidence_json"]), "recorded_at": item["recorded_at"],
        }
        finding["anchors"] = [
            {"anchor_id": anchor["anchor_id"], "anchor_type": anchor["anchor_type"],
             "anchor": anchor["anchor"], "evidence_fingerprint": anchor["evidence_fingerprint"],
             "details": _loads(anchor["details_json"])}
            for anchor in self._rows(
                "SELECT * FROM finding_anchors WHERE finding_id = ? ORDER BY anchor_id",
                (finding_id,))
        ]
        return finding

    def _thread(self, thread_id):
        row = self._conn.execute(
            "SELECT * FROM finding_threads WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"unknown finding thread {thread_id}")
        return row

    def _thread_events(self, thread_id, through_seq=None):
        sql = (
            "SELECT e.*, r.run_seq FROM finding_events e "
            "JOIN runs r ON r.run_id = e.run_id WHERE e.thread_id = ?"
        )
        parameters = [thread_id]
        if through_seq is not None:
            sql += " AND r.run_seq <= ?"
            parameters.append(through_seq)
        sql += " ORDER BY r.run_seq, e.recorded_at, e.event_id"
        return self._rows(sql, tuple(parameters))

    @staticmethod
    def _event_state(events):
        if not events:
            return None
        return ("falsified" if events[-1]["event_type"] == "falsified" else
                "resolved" if events[-1]["event_type"] == "resolved" else "open")

    def _insert_event(self, thread_id, run_id, unit_id, finding_id, event_type,
                      anchors, evidence_anchor, previous_evidence_anchor, *, evidence=None,
                      rationale="", resolution=None, revalidation=None):
        event_id = _identifier()
        self._conn.execute(
            "INSERT INTO finding_events(event_id, thread_id, run_id, unit_id, finding_id, "
            "event_type, anchors_json, evidence_anchor, previous_evidence_anchor, "
            "evidence_json, rationale, resolution_json, revalidation_json, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, thread_id, run_id, unit_id, finding_id, event_type,
             _json(list(anchors)), evidence_anchor, previous_evidence_anchor, _json(evidence),
             str(rationale), _json(resolution), _json(revalidation), _now()),
        )
        return event_id

    def _track_observation(self, unit, finding_id, finding_key, anchors, evidence):
        finding = self._finding_by_id(finding_id)
        normalized = rg_final_memory.normalize_anchors(anchors, finding)
        content_anchor = rg_final_memory.finding_content_anchor(finding, normalized)
        evidence_anchor = rg_final_memory.finding_evidence_anchor(
            normalized, evidence=evidence, finding=finding)
        run = self._conn.execute("SELECT stack_id FROM runs WHERE run_id = ?",
                                 (unit["run_id"],)).fetchone()
        thread = self._conn.execute(
            "SELECT * FROM finding_threads WHERE stack_id = ? AND unit_key = ? "
            "AND content_anchor = ?",
            (run["stack_id"], unit["unit_key"], content_anchor),
        ).fetchone()
        if thread is None:
            thread_id = _identifier()
            self._conn.execute(
                "INSERT INTO finding_threads(thread_id, stack_id, unit_key, finding_key, "
                "content_anchor, origin_finding_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (thread_id, run["stack_id"], unit["unit_key"], str(finding_key),
                 content_anchor, finding_id, _now()),
            )
            previous = None
            event_type = "observed"
        else:
            thread_id = thread["thread_id"]
            events = self._thread_events(thread_id)
            previous = events[-1]["evidence_anchor"] if events else None
            event_type = "reopened" if self._event_state(events) in ("resolved", "falsified") \
                else "observed"
        self._insert_event(
            thread_id, unit["run_id"], unit["unit_id"], finding_id, event_type,
            normalized, evidence_anchor, previous, evidence=evidence,
        )
        return thread_id

    def record_finding(self, unit_id, finding_key, content, severity, confidence,
                       state="open", lens=None, path=None, line=None, anchors=(), evidence=None):
        if str(state) in ("resolved", "falsified"):
            raise ValueError(f"{state} requires the explicit finding-memory closure seam")
        finding_data = {
            "finding_key": finding_key, "content": content, "lens": lens,
            "path": path, "line": line,
        }
        normalized = rg_final_memory.normalize_anchors(anchors, finding_data)
        # Normalize a caller-supplied anchor list before storing it, but retain
        # the historical behavior of storing no anchors when none were given.
        stored_anchors = normalized if anchors else ()
        with self._transaction():
            unit = self._unit_row(unit_id)
            finding_id = self._insert_finding_row(
                unit_id, finding_key, content, severity, confidence, state,
                lens, path, line, stored_anchors, evidence,
            )
            self._track_observation(unit, finding_id, finding_key, stored_anchors, evidence)
        return finding_id

    def _run_row(self, run_id):
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise StoreError(f"unknown run {run_id}")
        return row

    def _snapshot(self, stack_id, through_seq):
        threads = self._rows(
            "SELECT * FROM finding_threads WHERE stack_id = ? ORDER BY created_at, thread_id",
            (stack_id,),
        )
        return [
            {"thread": thread, "events": self._thread_events(thread["thread_id"], through_seq)}
            for thread in threads
        ]

    @staticmethod
    def _latest_finding_event(events):
        return next((event for event in reversed(events) if event["finding_id"] is not None), None)

    def _thread_public(self, entry, run_id=None):
        thread = entry["thread"]
        events = entry["events"]
        event = self._latest_finding_event(events)
        if event is None:
            return None
        finding = self._finding_by_id(event["finding_id"])
        latest = events[-1]
        finding.update({
            "thread_id": thread["thread_id"],
            "memory_thread_id": thread["thread_id"],
            "memory_state": self._event_state(events),
            "origin_finding_id": thread["origin_finding_id"],
            "origin_run_id": next(
                (item["run_id"] for item in events
                 if item["finding_id"] == thread["origin_finding_id"]),
                event["run_id"],
            ),
            "unit_key": thread["unit_key"],
            "content_anchor": thread["content_anchor"],
            "evidence_anchor": latest["evidence_anchor"],
            "inherited": run_id is not None and event["run_id"] != run_id,
            "carried_into_run": run_id is not None and any(
                item["run_id"] == run_id and item["event_type"] == "carried" for item in events),
        })
        return finding

    def rerun_context(self, run_id):
        """Outstanding prior-run threads; omission can never acknowledge them."""
        with self._lock:
            run = self._run_row(run_id)
            findings = []
            for entry in self._snapshot(run["stack_id"], run["run_seq"] - 1):
                if self._event_state(entry["events"]) != "open":
                    continue
                finding = self._thread_public(entry)
                finding["instruction"] = rg_final_memory.CARRY_FORWARD_INSTRUCTION
                findings.append(finding)
            return {
                "schema": rg_final_memory.RERUN_CONTEXT_SCHEMA,
                "store_instance_id": self.store_instance_id,
                "run_id": run_id,
                "stack_key": run["stack_id"],
                "required": bool(findings),
                "findings": findings,
                "local_waivers_are_official_suppression": False,
            }

    def carry_finding(self, run_id, unit_id, thread_id, *, finding_id=None,
                      anchors=None, evidence=None):
        """Append an explicit current-run carry without changing prior findings."""
        with self._transaction():
            run = self._run_row(run_id)
            unit = self._unit_row(unit_id)
            if unit["run_id"] != run_id:
                raise ValueError("carried finding unit does not belong to run")
            thread = self._thread(thread_id)
            if thread["stack_id"] != run["stack_id"] or thread["unit_key"] != unit["unit_key"]:
                raise ValueError("finding thread does not match the run stack and unit scope")
            previous_events = self._thread_events(thread_id, run["run_seq"])
            previous = previous_events[-1] if previous_events else None
            original = self._finding_by_id(thread["origin_finding_id"])
            if finding_id is None:
                if anchors is None:
                    stored_anchors = _loads(previous["anchors_json"]) if previous is not None else []
                else:
                    stored_anchors = rg_final_memory.normalize_anchors(anchors, original)
                finding_id = self._insert_finding_row(
                    unit_id, original["finding_key"], original["content"], original["severity"],
                    original["confidence"], "open", original["lens"], original["path"],
                    original["line"], stored_anchors, evidence,
                )
            else:
                current = self._finding_by_id(finding_id)
                current_unit = self._conn.execute(
                    "SELECT u.run_id, u.unit_id FROM findings f "
                    "JOIN units u ON u.unit_id = f.unit_id WHERE f.finding_id = ?",
                    (finding_id,),
                ).fetchone()
                if current_unit is None or current_unit["run_id"] != run_id or \
                        current_unit["unit_id"] != unit_id:
                    raise ValueError("carried finding does not belong to the current run and unit")
                stored_anchors = current["anchors"]
            current_finding = self._finding_by_id(finding_id)
            normalized = rg_final_memory.normalize_anchors(stored_anchors, current_finding)
            if rg_final_memory.finding_content_anchor(current_finding, normalized) != \
                    thread["content_anchor"]:
                raise ValueError("carried finding content anchor does not match its thread")
            evidence_anchor = rg_final_memory.finding_evidence_anchor(
                normalized, finding=current_finding)
            self._insert_event(
                thread_id, run_id, unit_id, finding_id, "carried", normalized, evidence_anchor,
                previous["evidence_anchor"] if previous is not None else None, evidence=evidence,
            )
        return finding_id

    @staticmethod
    def _evidence_present(value):
        if isinstance(value, str):
            return bool(value.strip())
        return bool(value)

    def _closure_scope(self, run_id, thread_id):
        run = self._run_row(run_id)
        thread = self._thread(thread_id)
        if thread["stack_id"] != run["stack_id"]:
            raise StoreError("finding thread belongs to a different stack")
        unit = self._conn.execute(
            "SELECT * FROM units WHERE run_id = ? AND unit_key = ?",
            (run_id, thread["unit_key"]),
        ).fetchone()
        if unit is None:
            raise StoreError("finding thread has no matching unit in this run")
        events = self._thread_events(thread_id, run["run_seq"])
        if self._event_state(events) != "open":
            raise ValueError("finding thread is not open at this run")
        return thread, unit, events

    @staticmethod
    def _closure_source_proof(evidence, unit):
        if not isinstance(evidence, dict) or not isinstance(evidence.get("source"), dict):
            raise ValueError("closure requires independently verified current-tree evidence")
        verification = evidence.get("verification") or {}
        source = evidence["source"]
        if (verification.get("schema") != rg_final_memory.CLOSURE_VERIFICATION_SCHEMA
                or verification.get("independent_tree_inspection") is not True):
            raise ValueError("current-tree evidence lacks independent supervisor verification")
        if (source.get("unit_key") != unit["unit_key"]
                or source.get("head_sha") != unit["head_sha"]
                or verification.get("unit_key") != unit["unit_key"]
                or verification.get("head_sha") != unit["head_sha"]):
            raise ValueError("current-tree evidence unit or SHA does not match its closure unit")
        if not isinstance(source.get("path"), str) or not source["path"]:
            raise ValueError("current-tree evidence requires a source path")
        start, end = source.get("start_line"), source.get("end_line")
        if (not isinstance(start, int) or isinstance(start, bool) or start < 1
                or not isinstance(end, int) or isinstance(end, bool) or end < start):
            raise ValueError("current-tree evidence requires an exact source line range")
        body = source.get("body")
        if not isinstance(body, str) or not body.strip():
            raise ValueError("current-tree evidence requires its exact source body")
        if source.get("body_sha256") != hashlib.sha256(body.encode("utf-8")).hexdigest():
            raise ValueError("current-tree source body fingerprint is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", str(source.get("file_sha256") or "")):
            raise ValueError("current-tree source file fingerprint is missing")
        return source

    def _closure_test_proof(self, revalidation, unit):
        if not isinstance(revalidation, dict):
            raise ValueError("positive closure requires independently executed test evidence")
        verification = revalidation.get("verification") or {}
        if (verification.get("schema") != rg_final_memory.CLOSURE_VERIFICATION_SCHEMA
                or verification.get("independently_executed") is not True):
            raise ValueError("revalidation lacks independent supervisor execution proof")
        if (revalidation.get("unit_key") != unit["unit_key"]
                or revalidation.get("head_sha") != unit["head_sha"]
                or verification.get("unit_key") != unit["unit_key"]
                or verification.get("head_sha") != unit["head_sha"]):
            raise ValueError("revalidation unit or SHA does not match its closure unit")
        row = self._conn.execute(
            "SELECT t.* FROM test_evidence t JOIN units u ON u.unit_id = t.unit_id "
            "WHERE t.test_id = ? AND t.unit_id = ? AND u.run_id = ?",
            (revalidation.get("test_id"), unit["unit_id"], unit["run_id"]),
        ).fetchone()
        if row is None or row["kind"] not in ("required", "existing_test", "ephemeral_test"):
            raise ValueError("revalidation does not identify a known test in the current unit")
        if row["state"].strip().lower() not in ("pass", "passed", "success", "succeeded"):
            raise ValueError("positive closure requires a successful independently executed test")
        if not row["evidence_fingerprint"]:
            raise ValueError("revalidation test has no actual evidence fingerprint")
        expected = {
            "test": row["name"], "kind": row["kind"], "state": row["state"],
            "output": row["output"], "evidence_fingerprint": row["evidence_fingerprint"],
        }
        for field, actual in expected.items():
            claimed = revalidation.get(field)
            if field == "state":
                claimed = str(claimed or "").strip().lower()
                actual = actual.strip().lower()
            if claimed != actual:
                raise ValueError(f"revalidation {field} does not match recorded test evidence")

    def resolve_finding(self, run_id, thread_id, *, anchors, resolution_evidence,
                        revalidation):
        """Append positive closure only with independently bound tree/test proof."""
        with self._transaction():
            thread, unit, events = self._closure_scope(run_id, thread_id)
            original = self._finding_by_id(thread["origin_finding_id"])
            normalized = rg_final_memory.normalize_anchors(anchors, original)
            if not anchors or rg_final_memory.finding_content_anchor(original, normalized) != \
                    thread["content_anchor"]:
                raise ValueError("resolution must retain the original content anchor")
            previous = events[-1]
            current_anchor = rg_final_memory.finding_evidence_anchor(normalized, finding=original)
            if current_anchor == previous["evidence_anchor"]:
                raise ValueError("positive resolution requires changed code or evidence anchors")
            source = self._closure_source_proof(resolution_evidence, unit)
            expected_anchor = rg_final_memory.current_source_anchor(source)
            if not any(anchor == expected_anchor for anchor in normalized):
                raise ValueError("resolution source anchor was not independently computed")
            self._closure_test_proof(revalidation, unit)
            latest = self._latest_finding_event(events)
            self._insert_event(
                thread_id, run_id, unit["unit_id"], latest["finding_id"], "resolved",
                normalized, current_anchor, previous["evidence_anchor"], evidence={},
                resolution=resolution_evidence, revalidation=revalidation,
            )
        return self.finding_history(thread_id)

    def falsify_finding(self, run_id, thread_id, rationale, *, evidence=None):
        """Append falsification only with clear rationale and independent proof."""
        rationale = str(rationale or "").strip()
        if len(rationale) < 20:
            raise ValueError("finding falsification requires a clear non-empty rationale")
        with self._transaction():
            thread, unit, events = self._closure_scope(run_id, thread_id)
            if not isinstance(evidence, dict):
                raise ValueError("finding falsification requires independent evidence")
            if evidence.get("source") is not None:
                self._closure_source_proof(evidence, unit)
            if evidence.get("revalidation") is not None:
                self._closure_test_proof(evidence["revalidation"], unit)
            if evidence.get("source") is None and evidence.get("revalidation") is None:
                raise ValueError(
                    "finding falsification requires verified tree or executed-test evidence")
            previous = events[-1]
            latest = self._latest_finding_event(events)
            self._insert_event(
                thread_id, run_id, unit["unit_id"], latest["finding_id"], "falsified",
                _loads(previous["anchors_json"]), previous["evidence_anchor"],
                previous["evidence_anchor"], evidence=evidence, rationale=rationale,
            )
        return self.finding_history(thread_id)

    def finding_history(self, thread_id):
        """Return the complete append-only thread, including immutable snapshots."""
        with self._lock:
            thread = self._thread(thread_id)
            events = []
            for event in self._thread_events(thread_id):
                events.append({
                    "event_id": event["event_id"], "thread_id": thread_id,
                    "run_id": event["run_id"], "run_seq": event["run_seq"],
                    "unit_id": event["unit_id"], "event_type": event["event_type"],
                    "anchors": _loads(event["anchors_json"]),
                    "evidence_anchor": event["evidence_anchor"],
                    "previous_evidence_anchor": event["previous_evidence_anchor"],
                    "evidence": _loads(event["evidence_json"]),
                    "rationale": event["rationale"],
                    "resolution_evidence": _loads(event["resolution_json"]),
                    "revalidation": _loads(event["revalidation_json"]),
                    "recorded_at": event["recorded_at"],
                    "finding": (self._finding_by_id(event["finding_id"])
                                if event["finding_id"] is not None else None),
                })
            return {
                "thread_id": thread_id, "store_instance_id": self.store_instance_id,
                "stack_key": thread["stack_id"], "unit_key": thread["unit_key"],
                "finding_key": thread["finding_key"],
                "content_anchor": thread["content_anchor"],
                "origin_finding_id": thread["origin_finding_id"],
                "state": self._event_state(self._thread_events(thread_id)),
                "events": events,
            }

    def memory_status(self, run_id):
        """Derive this run's historical memory view from events, never quiet output."""
        with self._lock:
            run = self._run_row(run_id)
            current = self._snapshot(run["stack_id"], run["run_seq"])
            prior = {
                entry["thread"]["thread_id"]: entry
                for entry in self._snapshot(run["stack_id"], run["run_seq"] - 1)
            }
            unresolved = []
            pending = []
            for entry in current:
                thread_id = entry["thread"]["thread_id"]
                if self._event_state(entry["events"]) != "open":
                    continue
                unresolved.append(thread_id)
                previous = prior.get(thread_id)
                handled = any(
                    event["run_id"] == run_id and
                    event["event_type"] in ("carried", "resolved", "falsified")
                    for event in entry["events"]
                )
                if previous and self._event_state(previous["events"]) == "open" and not handled:
                    pending.append(thread_id)
            return {
                "schema": "final-review-finding-memory/v1",
                "store_instance_id": self.store_instance_id,
                "run_id": run_id, "stack_key": run["stack_id"],
                "unresolved_thread_ids": unresolved,
                "pending_thread_ids": pending,
                "complete_for_finalization": not pending,
                "clear_allowed": not unresolved and not pending,
                "quiet_rerun_cleared": False,
                "carry_forward_instruction": rg_final_memory.CARRY_FORWARD_INSTRUCTION,
            }

    def official_findings(self, run_id):
        """Open current and inherited findings, with no local-waiver filtering."""
        with self._lock:
            run = self._run_row(run_id)
            findings = []
            for entry in self._snapshot(run["stack_id"], run["run_seq"]):
                if self._event_state(entry["events"]) == "open":
                    finding = self._thread_public(entry, run_id)
                    if finding is not None:
                        findings.append(finding)
            return findings

    def pinned_revisions(self, run_id):
        """Reconstruct complete provider-read inputs from immutable run rows."""
        with self._lock:
            self._run_row(run_id)
            pinned = []
            for row in self._rows(
                    "SELECT * FROM run_items WHERE run_id = ? ORDER BY position", (run_id,)):
                provider_data = _loads(row["provider_data_json"])
                source_data = provider_data.get("source") or {}
                target_data = provider_data.get("target") or {}
                item = {
                    "provider": provider_data.get("provider"),
                    "id": provider_data.get("id"),
                    "iid": provider_data.get("iid"),
                    "url": provider_data.get("url") or row["reference"],
                    "source": {
                        "project_id": source_data.get("project_id"),
                        "project_path": row["source_project"],
                        "branch": row["source_ref"], "sha": row["source_sha"],
                    },
                    "target": {
                        "project_id": target_data.get("project_id"),
                        "project_path": row["target_project"],
                        "branch": row["target_ref"], "sha": row["target_sha"],
                        "base_sha": row["base_sha"],
                    },
                }
                pinned.append({
                    "item_key": row["item_key"], "reference": row["reference"],
                    "pinned": item, "ancestors": _loads(row["ancestors_json"]),
                })
            return pinned

    def record_freshness(self, run_id, assessment):
        """Persist fresh/stale/incomplete evidence and require explicit restart on movement."""
        if not isinstance(assessment, dict) or assessment.get("schema") != \
                rg_final_memory.FRESHNESS_SCHEMA:
            raise ValueError("freshness assessment has an invalid schema")
        state = assessment.get("state")
        if state not in ("fresh", "stale", "incomplete"):
            raise ValueError("invalid freshness state")
        changes = list(assessment.get("changes") or ())
        errors = list(assessment.get("errors") or ())
        expected_state = "stale" if changes else "incomplete" if errors else "fresh"
        if state != expected_state or bool(assessment.get("fresh")) != (state == "fresh"):
            raise ValueError("freshness state does not match its evidence")
        check_id = _identifier()
        recorded_at = _now()
        with self._transaction():
            self._run_row(run_id)
            self._conn.execute(
                "INSERT INTO freshness_checks(check_id, run_id, state, changes_json, "
                "errors_json, pinned_json, current_json, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (check_id, run_id, state, _json(changes), _json(errors),
                 _json(assessment.get("pinned") or {}),
                 _json(assessment.get("current") or {}), recorded_at),
            )
            units = self._rows("SELECT unit_id FROM units WHERE run_id = ?", (run_id,))
            for unit in units:
                summary = ("Complete pinned revision tuple remains fresh" if state == "fresh" else
                           "Pinned source, target, base, retarget, or ancestor revisions moved"
                           if state == "stale" else
                           "Complete current revision evidence is unavailable")
                self._conn.execute(
                    "INSERT INTO coverage(coverage_id, unit_id, kind, status, summary, "
                    "details_json, recorded_at) VALUES (?, ?, 'freshness', ?, ?, ?, ?)",
                    (_identifier(), unit["unit_id"], state, summary,
                     _json({"check_id": check_id, "changes": changes, "errors": errors,
                            "automatic_restart": False}), recorded_at),
                )
                if state != "fresh":
                    self._conn.execute(
                        "INSERT INTO warnings(warning_id, unit_id, code, message, scope, "
                        "details_json, recorded_at) VALUES (?, ?, ?, ?, 'freshness', ?, ?)",
                        (_identifier(), unit["unit_id"],
                         "revision-stale" if state == "stale" else "freshness-check",
                         summary, _json({"check_id": check_id, "changes": changes,
                                        "errors": errors, "restart_required": state == "stale",
                                        "automatic_restart": False}), recorded_at),
                    )
                    self._conn.execute(
                        "UPDATE units SET state = ?, summary = ? WHERE unit_id = ?",
                        (state, summary if state == "stale" else
                         "Freshness could not be established; no official pass is available",
                         unit["unit_id"]),
                    )
            if state != "fresh":
                self._conn.execute("UPDATE runs SET state = ? WHERE run_id = ?", (state, run_id))
            if state == "stale":
                self._observe(
                    run_id, "stale_outcome", run_id=run_id, changed_count=len(changes),
                    incomplete_count=len(errors), restart_required=True,
                    pass_publication_allowed=False,
                )
        return {
            **assessment, "check_id": check_id, "run_id": run_id,
            "recorded_at": recorded_at, "restart_required": state == "stale",
            "automatic_restart": False,
            "restart": {"required": state == "stale", "automatic": False},
            "publication_pass_allowed": state == "fresh",
        }

    def latest_freshness(self, run_id):
        """Latest durable freshness assessment for this run, or ``None``."""
        with self._lock:
            self._run_row(run_id)
            row = self._conn.execute(
                "SELECT * FROM freshness_checks WHERE run_id = ? "
                "ORDER BY recorded_at DESC, check_id DESC LIMIT 1", (run_id,),
            ).fetchone()
            if row is None:
                return None
            state = row["state"]
            return {
                "schema": rg_final_memory.FRESHNESS_SCHEMA,
                "check_id": row["check_id"], "run_id": run_id, "state": state,
                "fresh": state == "fresh", "changes": _loads(row["changes_json"]),
                "errors": _loads(row["errors_json"]), "pinned": _loads(row["pinned_json"]),
                "current": _loads(row["current_json"]), "recorded_at": row["recorded_at"],
                "restart_required": state == "stale", "automatic_restart": False,
                "restart": {"required": state == "stale", "automatic": False},
                "publication_pass_allowed": state == "fresh",
            }

    def _publication_target(self, run_id, unit_id=None, item_key=None):
        """Resolve and cross-check one unit's fork-aware status destination."""
        run = self._run_row(run_id)
        unit = None
        item = None
        if unit_id is not None:
            unit = self._unit_row(unit_id)
            if unit["run_id"] != run_id:
                raise StoreError("publication unit does not belong to run")
        if unit is not None and unit["kind"] == "individual":
            item = self._conn.execute(
                "SELECT * FROM run_items WHERE run_id = ? AND position = ?",
                (run_id, unit["item_position"]),
            ).fetchone()
        elif item_key is not None:
            item = self._conn.execute(
                "SELECT * FROM run_items WHERE run_id = ? AND item_key = ?",
                (run_id, str(item_key)),
            ).fetchone()
        if item_key is not None and item is not None and item["item_key"] != str(item_key):
            raise StoreError("publication item does not match the individual unit")
        if item is not None and unit is not None and unit["kind"] == "individual" and \
                unit["item_position"] != item["position"]:
            raise StoreError("publication item does not match the individual unit")
        return run, unit, item

    @staticmethod
    def _publication_identity(run, unit, item):
        provider_data = _loads(item["provider_data_json"]) if item is not None else {}
        source = provider_data.get("source") or {}
        target = provider_data.get("target") or {}
        return {
            "store_instance_id": None,  # filled by publication_decision
            "run_id": run["run_id"],
            "run_seq": run["run_seq"],
            "stack_key": run["stack_id"],
            "contract_version": run["contract_version"],
            "unit_id": unit["unit_id"] if unit is not None else None,
            "unit_key": unit["unit_key"] if unit is not None else None,
            "item_key": item["item_key"] if item is not None else None,
            "source_project": source.get("project_id") or
            (item["source_project"] if item is not None else None),
            "source_ref": item["source_ref"] if item is not None else None,
            "head_sha": item["source_sha"] if item is not None else None,
            "target_project": target.get("project_id") or
            (item["target_project"] if item is not None else None),
            "target_ref": item["target_ref"] if item is not None else None,
            "target_sha": item["target_sha"] if item is not None else None,
            "base_sha": (item["base_sha"] or unit["base_sha"])
            if item is not None and unit is not None else
            (item["base_sha"] if item is not None else None),
        }

    @staticmethod
    def _verdict_findings(findings):
        normalized = []
        for original in findings:
            finding = dict(original)
            if "start_line" not in finding and finding.get("line") is not None:
                finding["start_line"] = finding["line"]
            if "end_line" not in finding and finding.get("line") is not None:
                finding["end_line"] = finding["line"]
            normalized.append(finding)
        return normalized

    @staticmethod
    def _coverage_complete(unit, coverage, stopped, skipped):
        required_status = coverage.get("required_tests")
        dynamic_status = coverage.get("dynamic_tests")
        required_done = required_status not in (
            None, "", "incomplete", "STOP", "stop", "stopped", "skipped",
        )
        dynamic_done = dynamic_status not in (
            None, "", "incomplete", "error", "STOP", "stop", "stopped", "skipped",
        )
        composition_done = unit["kind"] != "combined" or \
            coverage.get("composition") == "complete"
        return bool(
            unit["state"] in ("pass", "findings") and
            coverage.get("repository") == "complete" and
            coverage.get("mandatory_lenses") == "complete" and
            required_done and dynamic_done and composition_done and
            not stopped and not skipped
        )

    def publication_decision(self, run_id, *, unit_id=None, item_key=None, local_waivers=(),
                             trusted_orchestrator=False):
        """Derive provider-neutral status and official-green eligibility.

        The existing finding verdict remains the only severity authority. Full
        individual+combined+prospective execution evidence, run completion,
        assessed pins, same-store ordering, supported provider capability, and
        enforceable trust are mandatory gates. A completed adverse outcome may
        replace an older status without relaxing any official-green gate.
        Local waiver records are visible but never enter verdict input.
        """
        with self._lock:
            run, selected_unit, item = self._publication_target(run_id, unit_id, item_key)
            findings = self.official_findings(run_id)
            finding_memory = self.memory_status(run_id)
            freshness = self.latest_freshness(run_id)
            trust = _loads(run["trust_json"])
            trust.update({
                "store_instance_id": self.store_instance_id,
                "continuity_scope": "store",
                "waiver_isolation": "local-context-only",
                "local_waivers_applied": False,
            })
            units = self._rows(
                "SELECT * FROM units WHERE run_id = ? "
                "AND kind IN ('individual', 'combined', 'prospective') ORDER BY rowid",
                (run_id,),
            )
            scope_units = []
            stopped = False
            lens_count = test_count = warning_count = stop_count = 0
            for unit in units:
                coverage = {
                    row["kind"]: row["status"] for row in self._rows(
                        "SELECT kind, status FROM coverage WHERE unit_id = ?", (unit["unit_id"],),
                    )
                }
                unit_stops = self._rows(
                    "SELECT DISTINCT s.stop_id FROM stops s LEFT JOIN stop_units su "
                    "ON su.stop_id = s.stop_id WHERE s.unit_id = ? OR su.unit_id = ?",
                    (unit["unit_id"], unit["unit_id"]),
                )
                skipped = self._conn.execute(
                    "SELECT count(*) FROM skipped_work WHERE unit_id = ?", (unit["unit_id"],),
                ).fetchone()[0]
                complete = self._coverage_complete(
                    unit, coverage, bool(unit_stops), bool(skipped),
                )
                stopped = stopped or bool(unit_stops)
                stop_count += len(unit_stops)
                lens_count += self._conn.execute(
                    "SELECT count(*) FROM lenses WHERE unit_id = ?", (unit["unit_id"],),
                ).fetchone()[0]
                test_count += self._conn.execute(
                    "SELECT count(*) FROM test_evidence WHERE unit_id = ?", (unit["unit_id"],),
                ).fetchone()[0]
                warning_count += self._conn.execute(
                    "SELECT count(*) FROM warnings WHERE unit_id = ?", (unit["unit_id"],),
                ).fetchone()[0]
                scope_units.append({
                    "unit_id": unit["unit_id"], "unit_key": unit["unit_key"],
                    "kind": unit["kind"], "state": unit["state"],
                    "complete": complete, "stopped": bool(unit_stops),
                    "skipped_work": skipped, "coverage": coverage,
                })
            evidence_complete = bool(units) and all(unit["complete"] for unit in scope_units)
            all_pass = bool(units) and all(unit["state"] == "pass" for unit in scope_units)
            fresh = bool(freshness and freshness["fresh"])
            scope_complete = bool(evidence_complete and fresh and not stopped)
            run_completed = run["completed_at"] is not None
            superseded = run["superseded_by_run_id"] is not None
            finding_verdict = ocr_verdict.compute_verdict({
                "findings": self._verdict_findings(findings),
            })
            if ((freshness and freshness["state"] == "stale") or run["state"] == "stale" or
                    any(unit["state"] == "stale" for unit in scope_units)):
                state = "stale"
            elif stopped:
                state = "incomplete"
            elif findings or run["state"] == "findings":
                state = "findings"
            elif run["state"] == "incomplete" or not scope_complete:
                state = "incomplete"
            elif run["state"] == "pass" and (
                    not finding_memory["complete_for_finalization"] or not all_pass):
                state = "incomplete"
            else:
                state = run["state"]

            latest = self._conn.execute(
                "SELECT run_id FROM runs WHERE stack_id = ? ORDER BY run_seq DESC LIMIT 1",
                (run["stack_id"],),
            ).fetchone()
            current = bool(latest and latest["run_id"] == run_id and not superseded)
            provider_data = _loads(item["provider_data_json"]) if item is not None else {}
            reference = (item["reference"] if item is not None else None) or \
                provider_data.get("url")
            stack = self._conn.execute(
                "SELECT provider FROM stacks WHERE stack_id = ?", (run["stack_id"],),
            ).fetchone()
            provider = stack["provider"] if stack is not None else None
            provider_supported = provider in ("gitlab", "github")
            item_provider = provider_data.get("provider")
            if item_provider is not None and item_provider != provider:
                provider_supported = False
            if reference:
                try:
                    reference_provider = rg_final_provider.provider_name_for_references(reference)
                except (ValueError, rg_final_provider.ProviderError):
                    provider_supported = False
                else:
                    if reference_provider != provider:
                        provider_supported = False
            elif provider == "github":
                # GitHub source repository and host identity are mandatory for
                # its commit-status transport; never guess them.
                provider_supported = False

            status_blockers = []
            if run["state"] not in FINAL_STATES:
                status_blockers.append("invalid-final-state")
            if not run_completed:
                status_blockers.append("run-not-completed")
            if not current:
                status_blockers.append("run-superseded")
            if selected_unit is None:
                status_blockers.append("unit-required")
            elif selected_unit["kind"] not in ("individual", "combined"):
                status_blockers.append("unsupported-unit-kind")
            if item is None:
                status_blockers.append("publication-target-required")
            if not provider_supported:
                status_blockers.append("unsupported-provider")
            if freshness is None:
                # An assessed stale/incomplete result is useful adverse
                # evidence; the complete absence of an assessment is not.
                status_blockers.append("freshness-not-assessed")
            if bool(run["independent"]):
                status_blockers.append("independent-run")
            if not trusted_orchestrator:
                status_blockers.append("untrusted-caller")
            if trust.get("local_only"):
                status_blockers.append("trust-local-only")
            if not trust.get("enforced"):
                status_blockers.append("trust-not-enforced")
            if trust.get("official") is False:
                status_blockers.append("trust-not-official")

            green_blockers = list(status_blockers)
            if not scope_complete:
                green_blockers.append("scope-incomplete")
            if not all_pass:
                green_blockers.append("units-not-pass")
            if not fresh:
                green_blockers.append("revisions-not-fresh")
            if stopped:
                green_blockers.append("stop-recorded")
            if findings:
                green_blockers.append("official-findings")
            if finding_verdict != "pass":
                green_blockers.append("finding-verdict-not-pass")
            if not finding_memory["clear_allowed"]:
                green_blockers.append("finding-memory-not-clear")
            if state != "pass":
                green_blockers.append("final-state-not-pass")
            green_blockers = list(dict.fromkeys(green_blockers))
            can_publish_status = not status_blockers
            can_publish_pass = not green_blockers
            publication_state = "pass" if can_publish_pass else (
                state if state in ("findings", "stale", "incomplete") else "incomplete"
            )
            stale = state == "stale"
            identity = self._publication_identity(run, selected_unit, item)
            identity["store_instance_id"] = self.store_instance_id
            report = {
                "findings": len(findings), "lenses": lens_count, "tests": test_count,
                "warnings": warning_count, "stops": stop_count,
                "completed_units": sum(unit["complete"] for unit in scope_units),
                "total_units": len(scope_units), "freshness": (
                    freshness["state"] if freshness else "not_checked"
                ),
            }
            return {
                "schema": "final-review-publication-decision/v1",
                "run_id": run_id, "stack_key": run["stack_id"], "state": state,
                "publication_state": publication_state,
                "store_instance_id": self.store_instance_id,
                "unit_id": unit_id, "item_key": identity["item_key"],
                "identity": identity,
                "transport": {"reference": reference, "provider": provider},
                "scope": {
                    "complete": scope_complete, "evidence_complete": evidence_complete,
                    "all_pass": all_pass, "stopped": stopped, "units": scope_units,
                },
                "report": report,
                "run_completed": run_completed,
                "official_findings": findings,
                "finding_verdict": finding_verdict,
                "suppressed_finding_ids": [],
                "local_waivers_applied": False,
                "local_waiver_context": rg_final_memory.local_waiver_context(
                    findings, local_waivers),
                "memory": finding_memory, "freshness": freshness, "trust": trust,
                "status_blockers": status_blockers,
                "green_blockers": green_blockers,
                "can_publish": can_publish_status,
                "can_publish_status": can_publish_status,
                "can_publish_pass": can_publish_pass,
                "official_green": can_publish_pass,
                "superseded": superseded,
                "current": current,
                "independent": bool(run["independent"]),
                "restart_required": stale, "automatic_restart": False,
                "restart": {"required": stale, "automatic": False},
            }

    def _degrade_publication_trust(self, run_id, reason):
        """Degrade an existing run without changing its verdict or completion."""
        run = self._run_row(run_id)
        effective = self._trust(
            run["independent"], run["continuity_required"], _loads(run["trust_json"]),
        )
        effective.update({
            "local_only": True, "enforced": False, "official": False,
            "degraded_reason": f"publication degraded: {str(reason)}",
        })
        self._conn.execute(
            "UPDATE runs SET trust_json = ? WHERE run_id = ?", (_json(effective), run_id),
        )
        return effective

    @contextmanager
    def publication_guard(self, run_id, unit_id=None, item_key=None):
        """Serialize final publication against newer runs in this store.

        The writer transaction remains held across the provider request. A
        newer ``start_run`` therefore commits only after an older in-flight
        request finishes, and must publish its own newer outcome afterwards.
        An already superseded run yields ``current=False`` and no request may
        be made by its caller. Each guard owns exactly one durable audit row.
        """
        with self._transaction():
            run, _unit, item = self._publication_target(run_id, unit_id, item_key)
            if item is not None:
                item_key = item["item_key"]
            latest = self._conn.execute(
                "SELECT run_id FROM runs WHERE stack_id = ? ORDER BY run_seq DESC LIMIT 1",
                (run["stack_id"],),
            ).fetchone()
            current = latest is not None and latest["run_id"] == run_id and \
                run["superseded_by_run_id"] is None
            publication_id = _identifier()
            self._conn.execute(
                "INSERT INTO publications(publication_id, run_id, unit_id, item_key, status, "
                "status_context, details_json, error, attempted_at) "
                "VALUES (?, ?, ?, ?, 'attempting', ?, '{}', '', ?)",
                (publication_id, run_id, unit_id, item_key, PUBLICATION_STATUS_CONTEXT, _now()),
            )
            yield _PublicationAttempt(self, publication_id, run_id, current)

    def record_warning(self, unit_id, code, message, scope="", details=None):
        warning_id = _identifier()
        with self._transaction():
            self._unit_row(unit_id)
            self._conn.execute(
                "INSERT INTO warnings(warning_id, unit_id, code, message, scope, details_json, "
                "recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (warning_id, unit_id, str(code), str(message), str(scope),
                 _json(details), _now()),
            )
            if code == "structured-stop-rejected":
                row = self._unit_row(unit_id)
                self._observe(
                    row["run_id"], "stop_evaluated", outcome="rejected",
                    stage=str(scope), reason_code="structured-stop-rejected", category="",
                    affected_units=0, skipped_stages=0,
                )
        return warning_id

    def record_coverage(self, unit_id, kind, status, summary="", details=None):
        coverage_id = _identifier()
        with self._transaction():
            self._unit_row(unit_id)
            self._conn.execute(
                "INSERT OR IGNORE INTO coverage(coverage_id, unit_id, kind, status, summary, "
                "details_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (coverage_id, unit_id, str(kind), str(status), str(summary),
                 _json(details), _now()),
            )
            row = self._conn.execute(
                "SELECT coverage_id FROM coverage WHERE unit_id = ? AND kind = ? AND status = ? "
                "AND summary = ?", (unit_id, str(kind), str(status), str(summary))
            ).fetchone()
            self._observe_test_coverage(unit_id, str(kind), str(status), details)
        return row[0]

    def record_stop(self, unit_id, reason, evidence=None, affected_unit_ids=(), skipped_work=()):
        """Atomically record a STOP, affected scopes, and all skipped work."""
        stop_id = _identifier()
        affected = list(affected_unit_ids) or [unit_id]
        with self._transaction():
            self._unit_row(unit_id)
            for affected_id in affected:
                self._unit_row(affected_id)
            self._conn.execute(
                "INSERT INTO stops(stop_id, unit_id, reason, evidence_json, recorded_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (stop_id, unit_id, str(reason), _json(evidence), _now()),
            )
            for affected_id in affected:
                self._conn.execute(
                    "INSERT INTO stop_units(stop_id, unit_id) VALUES (?, ?)",
                    (stop_id, affected_id),
                )
            for skipped in skipped_work:
                skipped_unit = skipped.get("unit_id", unit_id)
                self._unit_row(skipped_unit)
                self._conn.execute(
                    "INSERT INTO skipped_work(skipped_id, stop_id, unit_id, kind, name, "
                    "details_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (_identifier(), stop_id, skipped_unit, _required_text(skipped, "kind"),
                     _required_text(skipped, "name"), _json(skipped.get("details")), _now()),
                )
            details = evidence if isinstance(evidence, dict) else {}
            if details.get("schema") == "final-review-stop/v1" or (
                    details.get("reason_code") and details.get("category") and details.get("stage")):
                row = self._unit_row(unit_id)
                self._observe(
                    row["run_id"], "stop_evaluated", outcome="validated",
                    stage=str(details.get("stage") or ""),
                    reason_code=str(details.get("reason_code") or ""),
                    category=str(details.get("category") or ""),
                    affected_units=len(affected), skipped_stages=len(skipped_work),
                )
        return stop_id

    def record_publication_observability(self, run_id, *, provider, status, reason,
                                         local_only, enforced, official_green):
        """Persist publication ordering/degradation inside its existing guard."""
        with self._lock:
            run = self._run_row(run_id)
            if status == "superseded" and run["superseded_by_run_id"]:
                self._observe(
                    run_id, "run_superseded", store_instance_id=self.store_instance_id,
                    run_id=run_id, superseded_by_run_id=run["superseded_by_run_id"],
                    publication_blocked=True,
                )
            if status in ("local_only", "superseded"):
                self._observe(
                    run_id, "publication_degraded", provider=provider, status=status,
                    reason=reason, local_only=bool(local_only), enforced=bool(enforced),
                    official_green=bool(official_green),
                )

    def record_publication(self, run_id, item_key=None, unit_id=None, status="attempted",
                           status_context="review-gate/final", details=None, error=""):
        publication_id = _identifier()
        with self._transaction():
            if self._conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone() is None:
                raise StoreError(f"unknown run {run_id}")
            if unit_id is not None:
                unit = self._unit_row(unit_id)
                if unit["run_id"] != run_id:
                    raise StoreError("publication unit does not belong to run")
            self._conn.execute(
                "INSERT INTO publications(publication_id, run_id, unit_id, item_key, status, "
                "status_context, details_json, error, attempted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (publication_id, run_id, unit_id, item_key, str(status),
                 str(status_context), _json(details), str(error), _now()),
            )
        return publication_id

    def is_superseded(self, run_id):
        row = self._conn.execute(
            "SELECT superseded_by_run_id FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"unknown run {run_id}")
        return row[0] is not None

    def run_history(self, stack_key=None):
        """Return all runs, oldest first, including superseded historical rows."""
        self._ensure_open()
        if stack_key is None:
            rows = self._conn.execute("SELECT run_id FROM runs ORDER BY run_seq").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT run_id FROM runs WHERE stack_id = ? ORDER BY run_seq", (stack_key,)
            ).fetchall()
        return [self.manifest(row[0]) for row in rows]

    def _rows(self, sql, parameters=()):
        return self._conn.execute(sql, parameters).fetchall()

    def _unit_manifest(self, row, individual_ids, publications):
        unit_id = row["unit_id"]
        result = {
            "unit_id": unit_id,
            "unit_key": row["unit_key"],
            "kind": row["kind"],
            "item_position": row["item_position"],
            "base_sha": row["base_sha"],
            "head_sha": row["head_sha"],
            "target_ref": row["target_ref"],
            "source_ref": row["source_ref"],
            "state": row["state"],
            "summary": row["summary"],
            "metadata": _loads(row["metadata_json"]),
        }
        result["lenses"] = [
            {"lens_id": item["lens_id"], "name": item["name"], "state": item["state"],
             "summary": item["summary"], "evidence": _loads(item["evidence_json"]),
             "recorded_at": item["recorded_at"]}
            for item in self._rows(
                "SELECT * FROM lenses WHERE unit_id = ? ORDER BY recorded_at, lens_id", (unit_id,))
        ]
        result["tests"] = [
            {"test_id": item["test_id"], "name": item["name"], "kind": item["kind"],
             "state": item["state"], "output": item["output"],
             "evidence_fingerprint": item["evidence_fingerprint"],
             "details": _loads(item["details_json"]), "recorded_at": item["recorded_at"]}
            for item in self._rows(
                "SELECT * FROM test_evidence WHERE unit_id = ? ORDER BY recorded_at, test_id",
                (unit_id,))
        ]
        findings = []
        for item in self._rows(
            "SELECT * FROM findings WHERE unit_id = ? ORDER BY recorded_at, finding_id", (unit_id,)
        ):
            finding = {
                "finding_id": item["finding_id"], "finding_key": item["finding_key"],
                "content": item["content"], "severity": item["severity"],
                "confidence": item["confidence"], "state": item["state"],
                "lens": item["lens"], "path": item["path"], "line": item["line"],
                "evidence": _loads(item["evidence_json"]), "recorded_at": item["recorded_at"],
            }
            finding["anchors"] = [
                {"anchor_id": anchor["anchor_id"], "anchor_type": anchor["anchor_type"],
                 "anchor": anchor["anchor"], "evidence_fingerprint": anchor["evidence_fingerprint"],
                 "details": _loads(anchor["details_json"])}
                for anchor in self._rows(
                    "SELECT * FROM finding_anchors WHERE finding_id = ? ORDER BY anchor_id",
                    (item["finding_id"],))
            ]
            findings.append(finding)
        result["findings"] = findings
        result["warnings"] = [
            {"warning_id": item["warning_id"], "code": item["code"], "message": item["message"],
             "scope": item["scope"], "details": _loads(item["details_json"]),
             "recorded_at": item["recorded_at"]}
            for item in self._rows(
                "SELECT * FROM warnings WHERE unit_id = ? ORDER BY recorded_at, warning_id",
                (unit_id,))
        ]
        result["coverage"] = [
            {"coverage_id": item["coverage_id"], "kind": item["kind"], "status": item["status"],
             "summary": item["summary"], "details": _loads(item["details_json"]),
             "recorded_at": item["recorded_at"]}
            for item in self._rows(
                "SELECT * FROM coverage WHERE unit_id = ? ORDER BY recorded_at, coverage_id",
                (unit_id,))
        ]
        stops = []
        for item in self._rows(
            "SELECT * FROM stops WHERE unit_id = ? ORDER BY recorded_at, stop_id", (unit_id,)
        ):
            stops.append({
                "stop_id": item["stop_id"], "reason": item["reason"],
                "evidence": _loads(item["evidence_json"]), "recorded_at": item["recorded_at"],
                "affected_unit_ids": [scope[0] for scope in self._rows(
                    "SELECT unit_id FROM stop_units WHERE stop_id = ? ORDER BY unit_id",
                    (item["stop_id"],))],
            })
        result["stops"] = stops
        result["skipped_work"] = [
            {"skipped_id": item["skipped_id"], "stop_id": item["stop_id"],
             "unit_id": item["unit_id"], "kind": item["kind"], "name": item["name"],
             "details": _loads(item["details_json"]), "recorded_at": item["recorded_at"]}
            for item in self._rows(
                "SELECT * FROM skipped_work WHERE unit_id = ? ORDER BY recorded_at, skipped_id",
                (unit_id,))
        ]
        result["publications"] = [item for item in publications if item["unit_id"] == unit_id]
        if row["kind"] == "combined":
            result["individual_unit_ids"] = list(individual_ids)
        return result

    def manifest(self, run_id, unit_id=None):
        """Generate the JSON-serializable run report directly from SQLite.

        Supplying ``unit_id`` limits detailed units while ``all_unit_ids`` and
        combined ``individual_unit_ids`` preserve links to the complete run.
        Returned structures are detached and safe for callers to modify.
        """
        with self._lock:
            self._ensure_open()
            run = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise StoreError(f"unknown run {run_id}")
            stack = self._conn.execute(
                "SELECT * FROM stacks WHERE stack_id = ?", (run["stack_id"],)
            ).fetchone()
            units = self._rows("SELECT * FROM units WHERE run_id = ? ORDER BY rowid", (run_id,))
            all_unit_ids = [item["unit_id"] for item in units]
            if unit_id is not None:
                selected = [item for item in units if item["unit_id"] == unit_id]
                if not selected:
                    raise StoreError(f"unknown unit {unit_id} in run {run_id}")
            else:
                selected = units
            individual_ids = [item["unit_id"] for item in units if item["kind"] == "individual"]
            items = [
                {"position": item["position"], "item_key": item["item_key"],
                 "source_project": item["source_project"], "source_ref": item["source_ref"],
                 "source_sha": item["source_sha"], "target_project": item["target_project"],
                 "target_ref": item["target_ref"], "target_sha": item["target_sha"],
                 "provider_data": _loads(item["provider_data_json"])}
                for item in self._rows(
                    "SELECT * FROM run_items WHERE run_id = ? ORDER BY position", (run_id,))
            ]
            publications = [
                {"publication_id": item["publication_id"], "run_id": item["run_id"],
                 "unit_id": item["unit_id"], "item_key": item["item_key"],
                 "status": item["status"], "status_context": item["status_context"],
                 "details": _loads(item["details_json"]), "error": item["error"],
                 "attempted_at": item["attempted_at"]}
                for item in self._rows(
                    "SELECT * FROM publications WHERE run_id = ? ORDER BY attempted_at, "
                    "publication_id", (run_id,))
            ]
            run_metadata = _loads(run["metadata_json"])
            return {
                "schema": REPORT_SCHEMA,
                "store": self.store_identity(),
                "observability": run_metadata.get("observability", {}),
                "run": {
                    "run_id": run_id, "run_seq": run["run_seq"], "stack_key": run["stack_id"],
                    "contract_version": run["contract_version"],
                    "acceptance_version": run["acceptance_version"], "state": run["state"],
                    "continuity_required": bool(run["continuity_required"]),
                    "started_at": run["started_at"], "completed_at": run["completed_at"],
                    "superseded_by_run_id": run["superseded_by_run_id"],
                    "metadata": run_metadata,
                },
                "stack": {
                    "stack_key": stack["stack_id"], "provider": stack["provider"],
                    "target_project": stack["target_project"],
                    "root_target_ref": stack["root_target_ref"],
                },
                "revisions": {
                    "root_target_sha": self._conn.execute(
                        "SELECT base_sha FROM units WHERE run_id = ? AND kind = 'combined'",
                        (run_id,),
                    ).fetchone()[0],
                    "items": items,
                },
                "contract": self.contract_view(run["contract_version"]),
                "trust": _loads(run["trust_json"]),
                "finding_memory": self.memory_status(run_id),
                "freshness": self.latest_freshness(run_id),
                "units": [self._unit_manifest(item, individual_ids, publications) for item in selected],
                "all_unit_ids": all_unit_ids,
                "publications": publications,
                "native_tasks": self._native_tasks_public(run_id),
            }

    def machine_report(self, run_id, unit_id=None):
        """Alias emphasizing the machine-readable public report seam."""
        return self.manifest(run_id, unit_id)

    @staticmethod
    def _details(value):
        return _json(value) if value else "{}"

    def human_report(self, run_id, unit_id=None):
        """Render an auditable Markdown report from the same manifest as JSON."""
        report = self.manifest(run_id, unit_id)
        run = report["run"]
        stack = report["stack"]
        trust = report["trust"]
        lines = [
            "# Final review report", "",
            f"Run: {run['run_id']} (sequence {run['run_seq']})",
            f"Store: {report['store']['store_instance_id']}",
            f"Stack: {stack['stack_key']}",
            f"Provider: {stack['provider']}; target: {stack['target_project']} "
            f"({stack['root_target_ref']})",
            f"Root target SHA: {report['revisions']['root_target_sha']}",
            f"Contract: {run['contract_version']}",
            f"Acceptance: {run['acceptance_version'] or 'not recorded'}",
            f"State: {run['state']}",
            "Finding memory: Content and evidence anchors require explicit carry-forward; "
            f"{len(report['finding_memory']['unresolved_thread_ids'])} unresolved thread(s)",
            f"Freshness: {report['freshness']['state'] if report['freshness'] else 'not checked'}",
            f"Trust: {'ENFORCED' if trust.get('enforced') else 'UNENFORCED'}; "
            f"{'local-only' if trust.get('local_only') else 'provider-capable'}; "
            f"{'independent' if trust.get('independent') else 'same-store continuity'}",
            f"Trust details: {self._details(trust)}",
            f"Superseded by: {run['superseded_by_run_id'] or 'no newer run in this store'}",
            "Observability: " + self._details(
                (report["observability"].get("counters") or {})
                if report["observability"] else {}),
            "", "## Pinned item revisions", "",
        ]
        for item in report["revisions"]["items"]:
            lines.append(
                f"- {item['position']}. {item['item_key']}: {item['source_project']} "
                f"{item['source_ref']}@{item['source_sha']} -> {item['target_project']} "
                f"{item['target_ref']}@{item['target_sha']}"
            )
        lines.extend(("", "## Review units", ""))
        for unit in report["units"]:
            lines.extend((
                f"### {unit['unit_key']} ({unit['kind']})", "",
                f"Unit: {unit['unit_id']}", f"State: {unit['state']}",
                f"Base: {unit['base_sha']}", f"Head: {unit['head_sha']}",
            ))
            if unit["kind"] == "combined":
                lines.append("Individual reports: " + ", ".join(unit["individual_unit_ids"]))
            lines.append("")
            lines.append("Lenses:")
            lines.extend(
                f"- {item['name']}: {item['state']} {item['summary']}" for item in unit["lenses"]
            ) or lines.append("- none recorded")
            lines.append("Tests and evidence:")
            lines.extend(
                f"- {item['name']} [{item['kind']}]: {item['state']}; "
                f"fingerprint={item['evidence_fingerprint'] or 'none'}\n"
                f"  Output: {item['output']}" for item in unit["tests"]
            ) or lines.append("- none recorded")
            lines.append("Findings:")
            for finding in unit["findings"]:
                lines.append(
                    f"- {finding['finding_key']} [{finding['severity']}/"
                    f"{finding['confidence']}; {finding['state']}]: {finding['content']}"
                )
                for anchor in finding["anchors"]:
                    lines.append(
                        f"  Anchor {anchor['anchor_type']}: {anchor['anchor']} "
                        f"evidence={anchor['evidence_fingerprint'] or 'none'}"
                    )
            if not unit["findings"]:
                lines.append("- none recorded")
            lines.append("Warnings:")
            lines.extend(
                f"- {item['code']}: {item['message']} {self._details(item['details'])}"
                for item in unit["warnings"]
            ) or lines.append("- none recorded")
            lines.append("Coverage:")
            lines.extend(
                f"- {item['kind']}: {item['status']} {item['summary']}" for item in unit["coverage"]
            ) or lines.append("- none recorded")
            lines.append("Stops:")
            lines.extend(f"- {item['reason']}" for item in unit["stops"]) or \
                lines.append("- none recorded")
            lines.append("Skipped work:")
            lines.extend(
                f"- {item['kind']} {item['name']} (stop {item['stop_id']})"
                for item in unit["skipped_work"]
            ) or lines.append("- none recorded")
            lines.append("Publications:")
            lines.extend(
                f"- {item['status']} {item['status_context']}: {item['error'] or 'no error'} "
                f"{self._details(item['details'])}" for item in unit["publications"]
            ) or lines.append("- none recorded")
            lines.append("")
        unassociated = [item for item in report["publications"] if item["unit_id"] is None]
        lines.append("## Run-level publication attempts")
        lines.extend(
            f"- {item['item_key'] or 'stack'}: {item['status']} {item['status_context']}: "
            f"{item['error'] or 'no error'} {self._details(item['details'])}"
            for item in unassociated
        ) or lines.append("- none recorded")
        return "\n".join(lines) + "\n"
