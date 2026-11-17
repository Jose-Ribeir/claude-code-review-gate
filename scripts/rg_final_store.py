"""Durable, store-local SPEC-16 final-review identity, evidence, and reports.

The database lives under the existing *external* review-gate store root, never
inside a repository under review.  Only Python's standard-library sqlite3 is
used.  All writes are explicit transactions; a write transaction is acquired
with BEGIN IMMEDIATE so its run sequence and same-store supersession decisions
are atomic.  The module records verdicts supplied by the supervisor; it does
not implement finding-severity or verdict logic.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import rg_final_contract
import rg_final_memory
import rg_store

SCHEMA_VERSION = 2
REPORT_SCHEMA = "final-review-report/v1"
FINAL_STATES = frozenset(("pass", "findings", "stale", "incomplete"))
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


class StoreError(Exception):
    """The final-review store cannot provide a trustworthy answer."""


class StoreBusyError(StoreError):
    """A SQLite writer remained unavailable after bounded retries."""


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


class FinalStore:
    """A persistent final-review SQLite store.

    ``path`` defaults to ``rg_store.store_root()/final-review.sqlite3``.
    ``busy_timeout`` is per SQLite lock wait in seconds and ``busy_retries``
    bounds additional BEGIN/COMMIT attempts.  Instances may be shared by
    threads; independent processes coordinate through SQLite WAL and locks.
    """

    def __init__(self, path=None, busy_timeout=5.0, busy_retries=4, retry_delay=0.025):
        self.path = Path(path) if path is not None else rg_store.store_root() / "final-review.sqlite3"
        self.path = self.path.expanduser().absolute()
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
                  continuity_required=False, metadata=None):
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
        return {
            "run_id": run_id,
            "run_seq": run_seq,
            "stack_key": stack_key,
            "store_instance_id": self.store_instance_id,
            "contract_version": contract_version,
            "acceptance_version": acceptance_version,
            "unit_ids": unit_ids,
            "trust": self._trust(independent, continuity_required),
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
        return test_id

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

    def resolve_finding(self, run_id, thread_id, *, anchors, resolution_evidence,
                        revalidation):
        """Append positive closure only for changed anchors and real revalidation."""
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
            if not self._evidence_present(resolution_evidence):
                raise ValueError("positive resolution requires resolution evidence")
            if not isinstance(revalidation, dict) or not revalidation:
                raise ValueError("positive resolution requires revalidation evidence")
            status = str(revalidation.get("status") or "").strip().lower()
            successful = status in (
                "pass", "passed", "success", "succeeded", "completed",
                "revalidated", "not_reproduced",
            ) or revalidation.get("revalidated") is True
            if not successful:
                raise ValueError("positive resolution requires successful revalidation")
            latest = self._latest_finding_event(events)
            self._insert_event(
                thread_id, run_id, unit["unit_id"], latest["finding_id"], "resolved",
                normalized, current_anchor, previous["evidence_anchor"], evidence={},
                resolution=resolution_evidence, revalidation=revalidation,
            )
        return self.finding_history(thread_id)

    def falsify_finding(self, run_id, thread_id, rationale, *, evidence=None):
        """Append explicit falsification; no anchor movement is required."""
        if not str(rationale or "").strip():
            raise ValueError("finding falsification requires a non-empty rationale")
        with self._transaction():
            thread, unit, events = self._closure_scope(run_id, thread_id)
            previous = events[-1]
            latest = self._latest_finding_event(events)
            self._insert_event(
                thread_id, run_id, unit["unit_id"], latest["finding_id"], "falsified",
                _loads(previous["anchors_json"]), previous["evidence_anchor"],
                previous["evidence_anchor"], evidence=evidence, rationale=str(rationale).strip(),
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

    def publication_decision(self, run_id, *, local_waivers=()):
        """C13 seam: official findings/trust/freshness with local waivers isolated.

        ``can_publish_pass`` is deliberately narrower than publication of a
        findings/stale/incomplete status. Local waiver records are returned as
        visible operator context but are never matched out of the official
        finding stream.
        """
        with self._lock:
            run = self._run_row(run_id)
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
            if ((freshness and freshness["state"] == "stale") or run["state"] == "stale"):
                state = "stale"
            elif findings:
                state = "findings"
            elif run["state"] == "pass" and (
                    not finding_memory["complete_for_finalization"] or
                    freshness is None or not freshness["fresh"]):
                state = "incomplete"
            else:
                state = run["state"]
            can_publish_pass = bool(
                state == "pass" and freshness and freshness["fresh"] and
                finding_memory["clear_allowed"] and not run["superseded_by_run_id"] and
                trust.get("enforced") and not trust.get("local_only")
            )
            stale = state == "stale"
            return {
                "schema": "final-review-publication-decision/v1",
                "run_id": run_id, "stack_key": run["stack_id"], "state": state,
                "store_instance_id": self.store_instance_id,
                "official_findings": findings,
                "suppressed_finding_ids": [],
                "local_waivers_applied": False,
                "local_waiver_context": rg_final_memory.local_waiver_context(
                    findings, local_waivers),
                "memory": finding_memory, "freshness": freshness, "trust": trust,
                "can_publish": state in FINAL_STATES,
                "can_publish_pass": can_publish_pass,
                "superseded": run["superseded_by_run_id"] is not None,
                "restart_required": stale, "automatic_restart": False,
                "restart": {"required": stale, "automatic": False},
            }

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
        return stop_id

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
            return {
                "schema": REPORT_SCHEMA,
                "store": self.store_identity(),
                "run": {
                    "run_id": run_id, "run_seq": run["run_seq"], "stack_key": run["stack_id"],
                    "contract_version": run["contract_version"],
                    "acceptance_version": run["acceptance_version"], "state": run["state"],
                    "continuity_required": bool(run["continuity_required"]),
                    "started_at": run["started_at"], "completed_at": run["completed_at"],
                    "superseded_by_run_id": run["superseded_by_run_id"],
                    "metadata": _loads(run["metadata_json"]),
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
