"""Bounded, sanitized SPEC-16 final-review observability contracts.

These tests exercise the C19 helper directly. No assertion claims that runtime
review orchestration already emits these events; that wiring is a later change.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from rg_final_telemetry import (  # noqa: E402
    EVENT_KINDS,
    MAX_EVENTS,
    MAX_SERIALIZED_BYTES,
    FinalReviewTelemetry,
    TelemetryValidationError,
    make_event,
)

RUN_ID = "0f0ab92b-07a4-4f1f-b50f-ed1cf325cd5c"
NEWER_RUN_ID = "ec65e74f-e2a1-455a-8a02-b029cf0fb466"
STORE_ID = "fdbaa2f4-bf53-43dc-93c2-d5f01de82f2c"


def _graph(telemetry, **overrides):
    fields = {
        "provider": "gitlab",
        "status": "validated",
        "supplied_count": 3,
        "discovered_count": 3,
        "omitted_ancestor_count": 0,
        "omitted_descendant_count": 0,
        "error_code": "",
    }
    fields.update(overrides)
    return telemetry.graph_discovered(**fields)


def _unit(telemetry, **overrides):
    unit_kind = overrides.pop("kind", "individual")
    fields = {
        "unit_key": "individual:1",
        "kind": unit_kind,
        "status": "findings",
    }
    fields.update(overrides)
    return telemetry.unit_executed(**fields)


def _lens(telemetry, **overrides):
    fields = {
        "unit_key": "combined",
        "lens": "documentation/domain",
        "status": "completed_with_warnings",
        "findings": 1,
        "warnings": 2,
        "unreviewed": 0,
    }
    fields.update(overrides)
    return telemetry.lens_executed(**fields)


def _tests(telemetry, **overrides):
    fields = {
        "unit_key": "combined",
        "execution": "dynamic",
        "status": "degraded",
        "coverage": "uncovered",
        "executed": 0,
        "attempts": 1,
        "findings": 0,
        "warnings": 2,
        "evidence_source": "",
    }
    fields.update(overrides)
    return telemetry.tests_executed(**fields)


def _stale(telemetry, **overrides):
    fields = {
        "run_id": RUN_ID,
        "changed_count": 2,
        "incomplete_count": 0,
        "restart_required": True,
        "pass_publication_allowed": False,
    }
    fields.update(overrides)
    return telemetry.stale_outcome(**fields)


def _stop(telemetry, **overrides):
    fields = {
        "outcome": "validated",
        "stage": "lens:security",
        "reason_code": "unsafe-generated-command",
        "category": "unsafe",
        "affected_units": 2,
        "skipped_stages": 9,
    }
    fields.update(overrides)
    return telemetry.stop_evaluated(**fields)


def _store(telemetry, **overrides):
    fields = {
        "store_instance_id": STORE_ID,
        "run_id": RUN_ID,
        "run_seq": 4,
        "continuity_required": True,
        "independent": False,
        "continuity_available": True,
        "enforced": True,
        "official": True,
    }
    fields.update(overrides)
    return telemetry.store_bound(**fields)


def _superseded(telemetry, **overrides):
    fields = {
        "store_instance_id": STORE_ID,
        "run_id": RUN_ID,
        "superseded_by_run_id": NEWER_RUN_ID,
        "publication_blocked": True,
    }
    fields.update(overrides)
    return telemetry.run_superseded(**fields)


def _publication(telemetry, **overrides):
    fields = {
        "provider": "github",
        "status": "local_only",
        "reason": "publication-provider-write-failed",
        "local_only": True,
        "enforced": False,
        "official_green": False,
    }
    fields.update(overrides)
    return telemetry.publication_degraded(**fields)


def test_graph_discovery_is_structured_and_counted():
    telemetry = FinalReviewTelemetry()

    event = _graph(telemetry)

    assert event["schema"] == "final-review-telemetry-event/v1"
    assert event["kind"] == "graph_discovery"
    assert event["sequence"] == 1
    assert event["provider"] == "gitlab"
    assert event["status"] == "validated"
    assert event["supplied_count"] == event["discovered_count"] == 3
    assert telemetry.snapshot()["counters"] == {
        "accepted": 1,
        "rejected": 0,
        "dropped": 0,
        "by_kind": {"graph_discovery": 1},
    }


def test_graph_rejection_preserves_omission_counts_without_corrected_command_text():
    telemetry = FinalReviewTelemetry()

    event = _graph(
        telemetry,
        status="rejected",
        supplied_count=1,
        discovered_count=3,
        omitted_ancestor_count=1,
        omitted_descendant_count=1,
        error_code="omitted-items",
    )

    assert event["error_code"] == "omitted-items"
    assert event["omitted_ancestor_count"] == 1
    assert event["omitted_descendant_count"] == 1
    assert set(event) == {
        "schema", "kind", "sequence", "provider", "status", "supplied_count",
        "discovered_count", "omitted_ancestor_count", "omitted_descendant_count",
        "error_code",
    }


@pytest.mark.parametrize("kind", ["individual", "combined", "prospective"])
@pytest.mark.parametrize("status", ["pass", "findings", "stale", "incomplete"])
def test_unit_coverage_uses_only_store_final_states(kind, status):
    telemetry = FinalReviewTelemetry()
    unit_key = f"{kind}:2" if kind == "individual" else kind

    event = _unit(telemetry, kind=kind, unit_key=unit_key, status=status)

    assert event["kind"] == "unit_coverage"
    assert event["unit_kind"] == kind
    assert event["unit_key"] == unit_key
    assert event["status"] == status
    assert "verdict" not in event
    assert "finding_verdict" not in event


def test_lens_execution_retains_coverage_counts_not_worker_prose():
    telemetry = FinalReviewTelemetry()

    event = _lens(telemetry)

    assert event["kind"] == "lens_coverage"
    assert event["lens"] == "documentation/domain"
    assert (event["findings"], event["warnings"], event["unreviewed"]) == (1, 2, 0)
    assert "summary" not in event
    assert "content" not in event
    assert "sources_read" not in event
    assert "evidence_references" not in event
    assert "stdout_tail" not in event


@pytest.mark.parametrize(
    "execution,status,coverage,evidence_source",
    [
        ("required", "pass", "declared", ""),
        ("required", "block", "declared", ""),
        ("required", "incomplete", "declared", ""),
        ("required", "absent", "absent", ""),
        ("dynamic", "findings", "existing_tests", "existing_test"),
        ("dynamic", "findings", "ephemeral_only", "ephemeral_test"),
        ("dynamic", "degraded", "uncovered", ""),
        ("dynamic", "degraded", "unknown", ""),
        ("dynamic", "not_applicable", "not_applicable", ""),
    ],
)
def test_required_and_dynamic_test_coverage_stay_distinct(
        execution, status, coverage, evidence_source):
    telemetry = FinalReviewTelemetry()

    event = _tests(
        telemetry,
        execution=execution,
        status=status,
        coverage=coverage,
        evidence_source=evidence_source,
    )

    assert event["kind"] == "test_coverage"
    assert event["execution"] == execution
    assert event["status"] == status
    assert event["coverage"] == coverage
    assert event["evidence_source"] == evidence_source
    assert "output" not in event
    assert "command" not in event


def test_stale_outcome_records_movement_and_explicit_restart_guards():
    telemetry = FinalReviewTelemetry()

    event = _stale(telemetry)

    assert event["kind"] == "stale_outcome"
    assert event["run_id"] == RUN_ID
    assert event["changed_count"] == 2
    assert event["restart_required"] is True
    assert event["pass_publication_allowed"] is False
    assert "pinned" not in event and "current" not in event


def test_validated_and_rejected_stops_have_separate_counters_and_scopes():
    telemetry = FinalReviewTelemetry()

    validated = _stop(telemetry)
    rejected = _stop(
        telemetry,
        outcome="rejected",
        stage="dynamic checks",
        reason_code="missing-stop-evidence",
        category="",
        affected_units=0,
        skipped_stages=0,
    )

    assert validated["category"] == "unsafe"
    assert validated["affected_units"] == 2
    assert validated["skipped_stages"] == 9
    assert rejected["outcome"] == "rejected"
    assert rejected["category"] == ""
    assert rejected["affected_units"] == rejected["skipped_stages"] == 0
    assert telemetry.snapshot()["counters"]["by_kind"]["structured_stop"] == 2


def test_store_identity_and_supersession_bind_the_exact_same_store_boundary():
    telemetry = FinalReviewTelemetry()

    store = _store(telemetry)
    superseded = _superseded(telemetry)

    assert store["kind"] == "store_identity"
    assert store["store_instance_id"] == STORE_ID
    assert store["run_seq"] == 4
    assert store["independent"] is False
    assert superseded["kind"] == "run_supersession"
    assert superseded["store_instance_id"] == STORE_ID
    assert superseded["run_id"] == RUN_ID
    assert superseded["superseded_by_run_id"] == NEWER_RUN_ID
    assert superseded["publication_blocked"] is True


def test_fresh_independent_store_trust_fields_are_not_inferred_by_telemetry():
    telemetry = FinalReviewTelemetry()

    event = _store(
        telemetry,
        independent=True,
        continuity_available=False,
        enforced=False,
        official=False,
    )

    assert event["independent"] is True
    assert event["continuity_available"] is False
    assert event["enforced"] is event["official"] is False
    assert "degraded_reason" not in event


def test_publication_degradation_is_explicitly_not_an_official_green():
    telemetry = FinalReviewTelemetry()

    event = _publication(telemetry)

    assert event["kind"] == "publication_degradation"
    assert event["provider"] == "github"
    assert event["status"] == "local_only"
    assert event["reason"] == "publication-provider-write-failed"
    assert event["local_only"] is True
    assert event["enforced"] is event["official_green"] is False


def test_snapshot_is_bounded_json_and_returns_detached_data():
    telemetry = FinalReviewTelemetry()
    _graph(telemetry)
    _lens(telemetry)

    snapshot = telemetry.snapshot()
    encoded = telemetry.to_json()
    decoded = json.loads(encoded)

    assert telemetry.schema == "final-review-telemetry/v1"
    assert snapshot["schema"] == telemetry.schema
    assert decoded == snapshot
    assert len(encoded.encode("utf-8")) <= MAX_SERIALIZED_BYTES
    snapshot["events"][0]["provider"] = "mutated"
    snapshot["counters"]["by_kind"]["graph_discovery"] = 999
    assert telemetry.snapshot()["events"][0]["provider"] == "gitlab"
    assert telemetry.snapshot()["counters"]["by_kind"]["graph_discovery"] == 1


def test_event_retention_is_bounded_and_accounted_for_without_losing_counters():
    telemetry = FinalReviewTelemetry(max_events=2)

    _unit(telemetry, unit_key="individual:1")
    _unit(telemetry, unit_key="individual:2")
    _unit(telemetry, unit_key="individual:3")

    snapshot = telemetry.snapshot()
    assert len(snapshot["events"]) == 2
    assert [event["unit_key"] for event in snapshot["events"]] == [
        "individual:2", "individual:3",
    ]
    assert [event["sequence"] for event in snapshot["events"]] == [2, 3]
    assert snapshot["counters"] == {
        "accepted": 3,
        "rejected": 0,
        "dropped": 1,
        "by_kind": {"unit_coverage": 3},
    }


@pytest.mark.parametrize("maximum", [0, -1, MAX_EVENTS + 1, True, 1.5, "12"])
def test_invalid_retention_limits_fail_at_construction(maximum):
    with pytest.raises(ValueError, match="max_events"):
        FinalReviewTelemetry(max_events=maximum)


def test_default_snapshot_stays_below_the_serialized_bound_at_capacity():
    telemetry = FinalReviewTelemetry()
    for index in range(MAX_EVENTS + 20):
        _graph(telemetry, supplied_count=index, discovered_count=index)

    assert len(telemetry.snapshot()["events"]) == MAX_EVENTS
    assert telemetry.snapshot()["counters"]["accepted"] == MAX_EVENTS + 20
    assert telemetry.snapshot()["counters"]["dropped"] == 20
    assert len(telemetry.to_json().encode("utf-8")) <= MAX_SERIALIZED_BYTES


def test_make_event_exposes_strict_validation_for_non_collecting_callers():
    fields = {
        "unit_key": "combined",
        "unit_kind": "combined",
        "status": "pass",
    }

    event = make_event("unit_coverage", fields)

    assert event == {
        "schema": "final-review-telemetry-event/v1",
        "kind": "unit_coverage",
        **fields,
    }


def test_make_event_requires_every_meaningful_field_and_rejects_unknown_fields():
    with pytest.raises(TelemetryValidationError, match="missing"):
        make_event("unit_coverage", {"unit_key": "combined", "unit_kind": "combined"})

    with pytest.raises(TelemetryValidationError, match="unknown"):
        make_event("unit_coverage", {
            "unit_key": "combined", "unit_kind": "combined", "status": "pass",
            "source_code": "TOKEN = 'do-not-record'",
        })


def test_collector_drops_malformed_events_and_counts_rejection_without_raising():
    telemetry = FinalReviewTelemetry()

    result = telemetry.record("graph_discovery", {"provider": "gitlab"})
    unknown = telemetry.record("not-a-final-event", {})

    assert result is None
    assert unknown is None
    assert telemetry.snapshot()["events"] == []
    assert telemetry.snapshot()["counters"] == {
        "accepted": 0,
        "rejected": 2,
        "dropped": 0,
        "by_kind": {},
    }


@pytest.mark.parametrize("count", [-1, True, 1.5, "3", 1_000_001])
def test_non_integer_or_unbounded_counts_are_rejected(count):
    telemetry = FinalReviewTelemetry()

    assert _graph(telemetry, supplied_count=count) is None
    assert telemetry.snapshot()["counters"]["accepted"] == 0
    assert telemetry.snapshot()["counters"]["rejected"] == 1


def test_run_sequence_must_be_a_positive_bounded_integer():
    telemetry = FinalReviewTelemetry()

    assert _store(telemetry, run_seq=0) is None
    assert _store(telemetry, run_seq=True) is None
    assert _store(telemetry, run_seq=1) is not None


@pytest.mark.parametrize("identity", ["", "not-a-uuid", "a" * 40, RUN_ID + "/../../secret"])
def test_run_store_and_supersession_ids_must_be_real_uuid_identities(identity):
    telemetry = FinalReviewTelemetry()

    assert _stale(telemetry, run_id=identity) is None
    assert _store(telemetry, store_instance_id=identity) is None
    assert _superseded(telemetry, superseded_by_run_id=identity) is None
    assert telemetry.snapshot()["counters"]["accepted"] == 0


@pytest.mark.parametrize("unit_key", [
    "../../secret.py", "combined; cat secrets", "individual:0", "arbitrary-source-text",
])
def test_unit_keys_cannot_be_paths_or_source_text(unit_key):
    telemetry = FinalReviewTelemetry()

    assert _unit(telemetry, unit_key=unit_key) is None
    assert unit_key not in telemetry.to_json()


def test_lens_names_are_limited_to_contract_lenses_and_combined_capabilities():
    telemetry = FinalReviewTelemetry()

    assert _lens(telemetry, lens="cross-item-contracts") is not None
    assert _lens(telemetry, lens="integration-behavior") is not None
    assert _lens(telemetry, lens="read ~/.ssh/id_rsa") is None
    assert "id_rsa" not in telemetry.to_json()


@pytest.mark.parametrize("secret", [
    "glpat-abcDEF1234567890",
    "ghp_abcDEF1234567890",
    "github_pat_abcDEF1234567890",
    "Bearer abcdef123456",
    "-----BEGIN PRIVATE KEY-----",
])
def test_secret_shaped_values_are_rejected_even_in_allowlisted_code_fields(secret):
    telemetry = FinalReviewTelemetry()

    assert _graph(telemetry, error_code=secret) is None
    assert secret not in telemetry.to_json()
    assert telemetry.snapshot()["counters"]["rejected"] == 1


def test_provider_tokens_and_unrelated_payload_fields_never_reach_a_snapshot():
    telemetry = FinalReviewTelemetry()
    payload = {
        "provider": "gitlab",
        "status": "validated",
        "supplied_count": 1,
        "discovered_count": 1,
        "omitted_ancestor_count": 0,
        "omitted_descendant_count": 0,
        "error_code": "",
        "gitlab_token": "glpat-never-persist-this",
        "unrelated_source": "def authenticate(password): return password",
    }

    assert telemetry.record("graph_discovery", payload) is None
    serialized = telemetry.to_json()
    assert "glpat-never-persist-this" not in serialized
    assert "authenticate" not in serialized
    assert "gitlab_token" not in serialized
    assert "unrelated_source" not in serialized


def test_boolean_fields_require_real_booleans_not_truthy_strings():
    telemetry = FinalReviewTelemetry()

    assert _stale(telemetry, restart_required="yes") is None
    assert _store(telemetry, independent=1) is None
    assert _superseded(telemetry, publication_blocked="false") is None
    assert _publication(telemetry, local_only=0) is None
    assert telemetry.snapshot()["counters"]["accepted"] == 0


def test_stale_events_cannot_claim_publication_or_automatic_continuation():
    telemetry = FinalReviewTelemetry()

    assert _stale(telemetry, restart_required=False) is None
    assert _stale(telemetry, pass_publication_allowed=True) is None


def test_stop_scope_and_category_validation_prevents_worker_scope_widening():
    telemetry = FinalReviewTelemetry()

    assert _stop(telemetry, category="critical") is None
    assert _stop(telemetry, outcome="rejected", affected_units=1) is None
    assert _stop(telemetry, outcome="rejected", skipped_stages=1) is None
    assert _stop(
        telemetry, outcome="rejected", category="", affected_units=0, skipped_stages=0,
    ) is not None


def test_publication_degradation_cannot_be_used_to_manufacture_green():
    telemetry = FinalReviewTelemetry()

    assert _publication(telemetry, official_green=True) is None
    assert _publication(telemetry, status="published") is None
    assert _publication(telemetry, reason="ok") is None
    assert telemetry.snapshot()["counters"]["accepted"] == 0


def test_all_public_event_kinds_have_working_semantic_recorders():
    telemetry = FinalReviewTelemetry()

    _graph(telemetry)
    _unit(telemetry)
    _lens(telemetry)
    _tests(telemetry)
    _stale(telemetry)
    _stop(telemetry)
    _store(telemetry)
    _superseded(telemetry)
    _publication(telemetry)

    snapshot = telemetry.snapshot()
    assert set(EVENT_KINDS) == {event["kind"] for event in snapshot["events"]}
    assert snapshot["counters"]["by_kind"] == {kind: 1 for kind in EVENT_KINDS}
    assert snapshot["counters"]["accepted"] == len(EVENT_KINDS)


def test_input_mappings_are_not_retained_or_mutated():
    telemetry = FinalReviewTelemetry()
    fields = {
        "unit_key": "individual:1",
        "unit_kind": "individual",
        "status": "incomplete",
    }
    original = copy.deepcopy(fields)

    event = telemetry.record("unit_coverage", fields)
    fields["status"] = "pass"
    event["unit_key"] = "combined"

    assert fields["unit_key"] == original["unit_key"]
    assert telemetry.snapshot()["events"][0]["status"] == "incomplete"
    assert telemetry.snapshot()["events"][0]["unit_key"] == "individual:1"
