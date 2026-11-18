"""Bounded, sanitized observability for explicit SPEC-16 final review.

This C19 module is a standalone helper, not an orchestration integration. A
supervisor may retain one :class:`FinalReviewTelemetry` per store run and persist
its snapshot in an existing evidence/details record. It has no import or write
dependency on ``FinalStore`` and never interprets findings, assigns a final
state, computes verdicts, or decides whether publication may occur.

Every event uses a closed field set. The only strings admitted are contracted
enums, canonical UUIDs, supervisor-owned unit keys, and short lowercase machine
codes. Counts and booleans describe already-observed outcomes; there is no
free-form text, source content, provider response, command, output, URL, path,
credential, or environment payload. Unknown and malformed events fail soft at
the collector and remain visible in its rejection counter.

Retention and serialized size are bounded. When the ring is full, older events
are dropped while total accepted/dropped counters remain intact. ``snapshot``
returns detached JSON-compatible values; ``to_json`` is suitable for storage in
an existing store details/evidence JSON column without adding a second verdict
authority or another operational source of truth.
"""
from __future__ import annotations

from collections import Counter, deque
from collections.abc import Mapping
import copy
import json
import re
import uuid


SCHEMA = "final-review-telemetry/v1"
EVENT_SCHEMA = "final-review-telemetry-event/v1"
EVENT_KINDS = (
    "graph_discovery",
    "unit_coverage",
    "lens_coverage",
    "test_coverage",
    "stale_outcome",
    "structured_stop",
    "store_identity",
    "run_supersession",
    "publication_degradation",
)
MAX_EVENTS = 128
MAX_EVENT_BYTES = 1024
MAX_SERIALIZED_BYTES = 160 * 1024
MAX_COUNT = 1_000_000

_FINAL_STATES = ("pass", "findings", "stale", "incomplete")
_LENS_NAMES = (
    "correctness",
    "repository-standards",
    "documentation/domain",
    "testing",
    "security",
    "compatibility/migrations",
    "change-intent",
    "cross-item-contracts",
    "integration-behavior",
)
_PUBLICATION_REASONS = (
    "invalid-final-state",
    "run-not-completed",
    "run-superseded",
    "unit-required",
    "unsupported-unit-kind",
    "publication-target-required",
    "unsupported-provider",
    "trust-local-only",
    "trust-not-enforced",
    "trust-not-official",
    "untrusted-caller",
    "publication-provider-write-failed",
)
_UUID_TEXT = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_UNIT_KEY = re.compile(r"^(?:individual:[1-9][0-9]{0,5}|combined|prospective)$")
_CODE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,79}$")
_SECRET_VALUE = re.compile(
    r"(?:bearer\s+|-----BEGIN(?:\s+[A-Z]+)*\s+PRIVATE\s+KEY-----|"
    r"(?:glpat|gldt|glrt|glcbt|glsoat)-[A-Za-z0-9]|"
    r"gh[pousr]_[A-Za-z0-9]|github_pat_[A-Za-z0-9]|"
    r"xox[baprs]-[A-Za-z0-9]|sk-[A-Za-z0-9]{8}|"
    r"AKIA[A-Z0-9]{12}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)


class TelemetryValidationError(ValueError):
    """An event would violate the closed observability contract."""


def _invalid(message):
    raise TelemetryValidationError(message)


def _enum(*values):
    allowed = frozenset(values)

    def validate(value, field):
        if not isinstance(value, str) or value not in allowed:
            _invalid(f"{field} must be one of {', '.join(values)}")
        return value

    return validate


def _count(value, field):
    if isinstance(value, bool) or not isinstance(value, int):
        _invalid(f"{field} must be an integer")
    if value < 0 or value > MAX_COUNT:
        _invalid(f"{field} must be between 0 and {MAX_COUNT}")
    return value


def _positive_count(value, field):
    value = _count(value, field)
    if value == 0:
        _invalid(f"{field} must be positive")
    return value


def _boolean(value, field):
    if not isinstance(value, bool):
        _invalid(f"{field} must be a boolean")
    return value


def _identity(value, field):
    if not isinstance(value, str) or not _UUID_TEXT.fullmatch(value):
        _invalid(f"{field} must be a canonical UUID")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise TelemetryValidationError(f"{field} must be a canonical UUID") from exc


def _unit_key(value, field):
    if not isinstance(value, str) or not _UNIT_KEY.fullmatch(value):
        _invalid(f"{field} must be an individual:N, combined, or prospective unit key")
    return value


def _code(value, field):
    if not isinstance(value, str) or not _CODE.fullmatch(value):
        _invalid(f"{field} must be a short lowercase machine code")
    if _SECRET_VALUE.search(value):
        _invalid(f"{field} contains credential-shaped material")
    return value


def _optional_code(value, field):
    if value == "":
        return value
    return _code(value, field)


def _stage(value, field):
    # These two aliases are emitted by rg_final_execution's public stop seam.
    if value in ("required verification", "dynamic checks"):
        return value
    return _code(value, field)


_PROVIDER = _enum("gitlab", "github")
_UNIT_KIND = _enum("individual", "combined", "prospective")
_UNIT_STATUS = _enum(*_FINAL_STATES)
_GRAPH_STATUS = _enum("validated", "rejected")
_LENS = _enum(*_LENS_NAMES)
_LENS_STATUS = _enum(
    "completed", "completed_with_warnings", "incomplete", "stopped", "skipped",
)
_TEST_EXECUTION = _enum("required", "dynamic")
_TEST_STATUS = _enum(
    "pass", "block", "absent", "findings", "degraded", "not_applicable",
    "incomplete", "reproduced_failure", "flake", "uncovered", "skipped",
    "unknown", "fail",
)
_TEST_COVERAGE = _enum(
    "declared", "absent", "existing_tests", "ephemeral_only", "uncovered",
    "unknown", "not_applicable",
)
_EVIDENCE_SOURCE = _enum("", "existing_test", "ephemeral_test")
_STOP_OUTCOME = _enum("validated", "rejected")
_STOP_CATEGORY = _enum("", "unsafe", "invalid", "untrustworthy")
_PUBLICATION_STATUS = _enum("local_only", "superseded")
_PUBLICATION_REASON = _enum(*_PUBLICATION_REASONS)

# All fields are required in make_event. Semantic recorder methods below supply
# defaults for counts which are genuinely optional at their production seam.
_EVENT_FIELDS = {
    "graph_discovery": {
        "provider": _PROVIDER,
        "status": _GRAPH_STATUS,
        "supplied_count": _count,
        "discovered_count": _count,
        "omitted_ancestor_count": _count,
        "omitted_descendant_count": _count,
        "error_code": _optional_code,
    },
    "unit_coverage": {
        "unit_key": _unit_key,
        "unit_kind": _UNIT_KIND,
        "status": _UNIT_STATUS,
    },
    "lens_coverage": {
        "unit_key": _unit_key,
        "lens": _LENS,
        "status": _LENS_STATUS,
        "findings": _count,
        "warnings": _count,
        "unreviewed": _count,
    },
    "test_coverage": {
        "unit_key": _unit_key,
        "execution": _TEST_EXECUTION,
        "status": _TEST_STATUS,
        "coverage": _TEST_COVERAGE,
        "executed": _count,
        "attempts": _count,
        "findings": _count,
        "warnings": _count,
        "evidence_source": _EVIDENCE_SOURCE,
    },
    "stale_outcome": {
        "run_id": _identity,
        "changed_count": _count,
        "incomplete_count": _count,
        "restart_required": _boolean,
        "pass_publication_allowed": _boolean,
    },
    "structured_stop": {
        "outcome": _STOP_OUTCOME,
        "stage": _stage,
        "reason_code": _code,
        "category": _STOP_CATEGORY,
        "affected_units": _count,
        "skipped_stages": _count,
    },
    "store_identity": {
        "store_instance_id": _identity,
        "run_id": _identity,
        "run_seq": _positive_count,
        "continuity_required": _boolean,
        "independent": _boolean,
        "continuity_available": _boolean,
        "enforced": _boolean,
        "official": _boolean,
    },
    "run_supersession": {
        "store_instance_id": _identity,
        "run_id": _identity,
        "superseded_by_run_id": _identity,
        "publication_blocked": _boolean,
    },
    "publication_degradation": {
        "provider": _PROVIDER,
        "status": _PUBLICATION_STATUS,
        "reason": _PUBLICATION_REASON,
        "local_only": _boolean,
        "enforced": _boolean,
        "official_green": _boolean,
    },
}


def _check_semantics(kind, event):
    if kind == "graph_discovery":
        if event["status"] == "validated" and event["error_code"]:
            _invalid("a validated graph cannot carry an error_code")
        if event["status"] == "rejected" and not event["error_code"]:
            _invalid("a rejected graph requires an error_code")
    elif kind == "stale_outcome":
        if event["changed_count"] == 0:
            _invalid("a stale outcome requires at least one changed revision coordinate")
        if not event["restart_required"] or event["pass_publication_allowed"]:
            _invalid("a stale outcome requires restart and forbids pass publication")
    elif kind == "structured_stop":
        if event["outcome"] == "validated" and not event["category"]:
            _invalid("a validated stop requires unsafe, invalid, or untrustworthy category")
        if event["outcome"] == "rejected" and (
                event["affected_units"] or event["skipped_stages"]):
            _invalid("a rejected stop cannot claim affected units or skipped stages")
    elif kind == "publication_degradation":
        if not event["local_only"] or event["enforced"] or event["official_green"]:
            _invalid("publication degradation must remain local-only, unenforced, and not green")


def make_event(kind, fields):
    """Validate and detach one event, or raise ``TelemetryValidationError``.

    The mapping must contain exactly the fields for ``kind``. This strict pure
    boundary is useful to non-collecting callers; the collector's ``record``
    method converts validation failures into bounded rejection counters.
    """
    if not isinstance(kind, str) or kind not in _EVENT_FIELDS:
        _invalid(f"unknown final-review telemetry event kind: {kind!r}")
    if not isinstance(fields, Mapping):
        _invalid("event fields must be a mapping")
    rules = _EVENT_FIELDS[kind]
    missing = sorted(set(rules) - set(fields))
    unknown = sorted(str(key) for key in set(fields) - set(rules))
    if missing:
        _invalid(f"{kind} is missing required fields: {', '.join(missing)}")
    if unknown:
        _invalid(f"{kind} contains unknown fields: {', '.join(unknown)}")
    event = {"schema": EVENT_SCHEMA, "kind": kind}
    for field, validate in rules.items():
        try:
            event[field] = validate(fields[field], field)
        except TelemetryValidationError:
            raise
        except Exception as exc:
            raise TelemetryValidationError(f"{field} is malformed") from exc
    _check_semantics(kind, event)
    try:
        encoded = json.dumps(event, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise TelemetryValidationError("event is not JSON serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
        _invalid(f"event exceeds {MAX_EVENT_BYTES} serialized bytes")
    return event


class FinalReviewTelemetry:
    """A fail-soft bounded event ring with process-local structured counters.

    Recording methods return their sanitized retained event, or ``None`` for a
    malformed event or an event immediately displaced by a full ring. No method
    performs I/O, reads the environment, or changes review/publication policy.
    """

    schema = SCHEMA

    def __init__(self, max_events=MAX_EVENTS):
        if isinstance(max_events, bool) or not isinstance(max_events, int) \
                or not 1 <= max_events <= MAX_EVENTS:
            raise ValueError(f"max_events must be an integer between 1 and {MAX_EVENTS}")
        self.max_events = max_events
        self._events = deque(maxlen=max_events)
        self._sequence = 0
        self._accepted = 0
        self._rejected = 0
        self._dropped = 0
        self._by_kind = Counter()

    def record(self, kind, fields):
        """Record one detached event; malformed input is counted and discarded."""
        try:
            event = make_event(kind, fields)
        except (TelemetryValidationError, TypeError, ValueError):
            self._rejected += 1
            return None
        if len(self._events) == self.max_events:
            self._dropped += 1
        self._sequence += 1
        self._accepted += 1
        self._by_kind[kind] += 1
        event["sequence"] = self._sequence
        self._events.append(event)
        return dict(event)

    def graph_discovered(self, *, provider, status, supplied_count, discovered_count,
                         omitted_ancestor_count=0, omitted_descendant_count=0,
                         error_code=""):
        return self.record("graph_discovery", {
            "provider": provider, "status": status, "supplied_count": supplied_count,
            "discovered_count": discovered_count,
            "omitted_ancestor_count": omitted_ancestor_count,
            "omitted_descendant_count": omitted_descendant_count,
            "error_code": error_code,
        })

    def unit_executed(self, *, unit_key, kind, status):
        return self.record("unit_coverage", {
            "unit_key": unit_key, "unit_kind": kind, "status": status,
        })

    def lens_executed(self, *, unit_key, lens, status, findings=0, warnings=0,
                      unreviewed=0):
        return self.record("lens_coverage", {
            "unit_key": unit_key, "lens": lens, "status": status,
            "findings": findings, "warnings": warnings, "unreviewed": unreviewed,
        })

    def tests_executed(self, *, unit_key, execution, status, coverage, executed=0,
                       attempts=0, findings=0, warnings=0, evidence_source=""):
        return self.record("test_coverage", {
            "unit_key": unit_key, "execution": execution, "status": status,
            "coverage": coverage, "executed": executed, "attempts": attempts,
            "findings": findings, "warnings": warnings,
            "evidence_source": evidence_source,
        })

    def stale_outcome(self, *, run_id, changed_count, incomplete_count=0,
                      restart_required=True, pass_publication_allowed=False):
        return self.record("stale_outcome", {
            "run_id": run_id, "changed_count": changed_count,
            "incomplete_count": incomplete_count,
            "restart_required": restart_required,
            "pass_publication_allowed": pass_publication_allowed,
        })

    def stop_evaluated(self, *, outcome, stage, reason_code, category,
                       affected_units=0, skipped_stages=0):
        return self.record("structured_stop", {
            "outcome": outcome, "stage": stage, "reason_code": reason_code,
            "category": category, "affected_units": affected_units,
            "skipped_stages": skipped_stages,
        })

    def store_bound(self, *, store_instance_id, run_id, run_seq, continuity_required,
                    independent, continuity_available, enforced, official):
        return self.record("store_identity", {
            "store_instance_id": store_instance_id, "run_id": run_id,
            "run_seq": run_seq, "continuity_required": continuity_required,
            "independent": independent, "continuity_available": continuity_available,
            "enforced": enforced, "official": official,
        })

    def run_superseded(self, *, store_instance_id, run_id, superseded_by_run_id,
                       publication_blocked):
        return self.record("run_supersession", {
            "store_instance_id": store_instance_id, "run_id": run_id,
            "superseded_by_run_id": superseded_by_run_id,
            "publication_blocked": publication_blocked,
        })

    def publication_degraded(self, *, provider, status, reason, local_only=True,
                             enforced=False, official_green=False):
        return self.record("publication_degradation", {
            "provider": provider, "status": status, "reason": reason,
            "local_only": local_only, "enforced": enforced,
            "official_green": official_green,
        })

    def snapshot(self):
        """Return detached counters and retained events, never a verdict record."""
        return {
            "schema": self.schema,
            "counters": {
                "accepted": self._accepted,
                "rejected": self._rejected,
                "dropped": self._dropped,
                "by_kind": {
                    kind: self._by_kind[kind] for kind in EVENT_KINDS
                    if self._by_kind[kind]
                },
            },
            "events": copy.deepcopy(list(self._events)),
        }

    def to_json(self):
        """Serialize one bounded snapshot for an existing store JSON field."""
        value = self.snapshot()
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        size = len(encoded.encode("utf-8"))
        if size > MAX_SERIALIZED_BYTES:
            raise ValueError(f"telemetry snapshot exceeds {MAX_SERIALIZED_BYTES} bytes")
        return encoded
