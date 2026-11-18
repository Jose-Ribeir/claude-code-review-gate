"""Vertical tests for the SPEC-16 final-review SQLite store.

The public store seam is exercised with real temporary databases.  No test
reaches into private helpers or substitutes an in-memory repository.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rg_final_acceptance as acceptance  # noqa: E402
import rg_final_contract as contract  # noqa: E402
import rg_final_store as final_store  # noqa: E402
from rg_final_store import FinalStore, StoreBusyError, StoreError  # noqa: E402


def stack(source_sha="a" * 40, target_sha="b" * 40):
    return {
        "provider": "gitlab",
        "target_project": "group/project",
        "root_target_ref": "main",
        "root_target_sha": target_sha,
        "items": [
            {
                "item_key": "123",
                "source_project": "fork/project",
                "source_ref": "feature",
                "source_sha": source_sha,
                "target_project": "group/project",
                "target_ref": "main",
                "target_sha": target_sha,
                "provider_data": {"iid": 123, "author": "someone-else"},
            }
        ],
    }


def opened(tmp_path, name="review.sqlite3", **kwargs):
    store = FinalStore(tmp_path / name, **kwargs)
    assert store.store_instance_id
    return store


def start(store, **kwargs):
    return store.start_run(stack(**kwargs))


def test_default_database_uses_external_store_root(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_STATE_DIR", str(tmp_path / "external"))
    store = FinalStore()
    try:
        assert store.path == tmp_path / "external" / "final-review.sqlite3"
        assert store.path.exists()
        if os.name != "nt":
            assert store.path.stat().st_mode & 0o777 == 0o600
        assert not (ROOT / "final-review.sqlite3").exists()
    finally:
        store.close()


def test_fresh_store_persists_identity_and_database_guards(tmp_path):
    path = tmp_path / "nested" / "review.sqlite3"
    first = FinalStore(path, busy_timeout=0.05, busy_retries=2)
    identity = first.store_instance_id
    assert uuid.UUID(identity)
    assert first.schema_version == final_store.SCHEMA_VERSION
    assert first.store_identity() == {
        "store_instance_id": identity,
        "schema_version": final_store.SCHEMA_VERSION,
        "continuity_scope": "store",
        "path": str(path),
    }
    assert first.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert first.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert first.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 50
    assert path.parent.stat().st_mode & 0o777 == 0o700
    first.close()

    second = FinalStore(path)
    try:
        assert second.store_instance_id == identity
        # Reopening runs migrations and seed installation idempotently.
        assert second.connection.execute(
            "SELECT count(*) FROM schema_migrations"
        ).fetchone()[0] == final_store.SCHEMA_VERSION
        assert second.connection.execute(
            "SELECT count(*) FROM contract_versions WHERE version = ?",
            (contract.CURRENT_CONTRACT_VERSION,),
        ).fetchone()[0] == 1
    finally:
        second.close()


def test_independent_stores_do_not_share_identity_or_history(tmp_path):
    with opened(tmp_path, "one.sqlite3") as one, opened(tmp_path, "two.sqlite3") as two:
        first = start(one)
        one.set_unit_state(first["unit_ids"]["combined"], "pass")
        one.finish_run(first["run_id"], "pass")
        independent = start(two)

        assert one.store_instance_id != two.store_instance_id
        assert independent["trust"]["independent"] is True
        assert independent["trust"]["continuity_available"] is False
        assert independent["trust"]["enforced"] is False
        assert independent["trust"]["continuity_scope"] == "store"
        assert two.manifest(independent["run_id"])["run"]["run_seq"] == 1
        local_history = two.run_history(first["stack_key"])
        assert [entry["run"]["run_id"] for entry in local_history] == [independent["run_id"]]
        assert first["run_id"] not in json.dumps(local_history)


def test_start_run_records_complete_stack_revision_and_unit_identity(tmp_path):
    with opened(tmp_path) as store:
        identity = start(store)
        run_id = identity["run_id"]
        assert uuid.UUID(run_id)
        assert identity["run_seq"] == 1
        assert identity["stack_key"]
        assert identity["contract_version"] == contract.CURRENT_CONTRACT_VERSION
        assert identity["acceptance_version"] == acceptance.CURRENT_ACCEPTANCE_VERSION
        assert set(identity["unit_ids"]) == {"individual:1", "combined"}

        report = store.manifest(run_id)
        assert report["schema"] == "final-review-report/v1"
        assert report["store"]["store_instance_id"] == store.store_instance_id
        assert report["run"]["run_id"] == run_id
        assert report["run"]["state"] == "incomplete"
        assert report["run"]["acceptance_version"] == acceptance.CURRENT_ACCEPTANCE_VERSION
        assert report["stack"] == {
            "stack_key": identity["stack_key"],
            "provider": "gitlab",
            "target_project": "group/project",
            "root_target_ref": "main",
        }
        assert report["revisions"]["root_target_sha"] == "b" * 40
        assert report["revisions"]["items"] == [
            {
                "position": 1,
                "item_key": "123",
                "source_project": "fork/project",
                "source_ref": "feature",
                "source_sha": "a" * 40,
                "target_project": "group/project",
                "target_ref": "main",
                "target_sha": "b" * 40,
                "provider_data": {"iid": 123, "author": "someone-else"},
            }
        ]
        assert [unit["kind"] for unit in report["units"]] == ["individual", "combined"]
        individual, combined = report["units"]
        assert individual["unit_key"] == "individual:1"
        assert individual["item_position"] == 1
        assert individual["base_sha"] == "b" * 40
        assert individual["head_sha"] == "a" * 40
        assert combined["unit_key"] == "combined"
        assert combined["base_sha"] == "b" * 40
        assert combined["head_sha"] == "a" * 40
        assert combined["individual_unit_ids"] == [individual["unit_id"]]
        assert report["publications"] == []


def test_multi_item_combined_head_is_last_item_and_stack_identity_ignores_shas(tmp_path):
    value = stack()
    value["items"].append(
        {
            "item_key": "124",
            "source_project": "group/project",
            "source_ref": "feature-2",
            "source_sha": "c" * 40,
            "target_project": "group/project",
            "target_ref": "feature",
            "target_sha": "a" * 40,
        }
    )
    with opened(tmp_path) as store:
        first = store.start_run(value)
        value["root_target_sha"] = "d" * 40
        value["items"][0]["source_sha"] = "e" * 40
        second = store.start_run(value)
        assert first["stack_key"] == second["stack_key"]
        assert second["run_seq"] == 2
        assert store.manifest(second["run_id"])["revisions"]["root_target_sha"] == "d" * 40
        assert store.is_superseded(first["run_id"]) is True
        assert store.is_superseded(second["run_id"]) is False
        assert store.manifest(first["run_id"])["run"]["superseded_by_run_id"] == second["run_id"]
        combined = store.manifest(second["run_id"])["units"][-1]
        assert combined["head_sha"] == "c" * 40
        assert len(combined["individual_unit_ids"]) == 2


def test_run_sequence_is_atomic_across_connections(tmp_path):
    path = tmp_path / "shared.sqlite3"
    with FinalStore(path) as first, FinalStore(path) as second:
        stores = [first, second] * 4
        results = []
        errors = []
        barrier = threading.Barrier(len(stores))

        def make_run(db):
            try:
                barrier.wait(timeout=5)
                results.append(start(db)["run_seq"])
            except Exception as exc:  # pragma: no cover - assertion reports the error
                errors.append(exc)

        threads = [threading.Thread(target=make_run, args=(db,)) for db in stores]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert not errors
        assert sorted(results) == list(range(1, 9))
        assert [entry["run"]["run_seq"] for entry in first.run_history()] == list(range(1, 9))


def test_locked_writer_has_bounded_retry_and_typed_failure(tmp_path):
    path = tmp_path / "locked.sqlite3"
    with FinalStore(path, busy_timeout=0.01, busy_retries=2, retry_delay=0) as store:
        blocker = sqlite3.connect(path)
        blocker.execute("BEGIN EXCLUSIVE")
        began = time.monotonic()
        with pytest.raises(StoreBusyError):
            start(store)
        assert time.monotonic() - began < 2
        blocker.rollback()
        blocker.close()
        assert start(store)["run_seq"] == 1


def test_run_creation_rolls_back_stack_items_and_units_together(tmp_path):
    path = tmp_path / "atomic.sqlite3"
    with FinalStore(path) as store:
        store.connection.execute(
            "CREATE TRIGGER reject_combined BEFORE INSERT ON units "
            "WHEN NEW.unit_key = 'combined' BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            start(store)
        for table in ("runs", "run_items", "units", "stacks"):
            assert store.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        store.connection.execute("DROP TRIGGER reject_combined")
        assert start(store)["run_seq"] == 1


def test_invalid_inputs_and_states_fail_closed(tmp_path):
    with opened(tmp_path) as store:
        with pytest.raises(ValueError, match="at least one item"):
            store.start_run({**stack(), "items": []})
        with pytest.raises(ValueError, match="source_sha"):
            store.start_run(stack(source_sha="not-a-sha"))
        identity = start(store)
        unit_id = identity["unit_ids"]["combined"]
        with pytest.raises(ValueError, match="state"):
            store.set_unit_state(unit_id, "green")
        with pytest.raises(ValueError, match="state"):
            store.finish_run(identity["run_id"], " green ")
        with pytest.raises(StoreError, match="unknown run"):
            store.manifest("missing")
        with pytest.raises(StoreError, match="unknown unit"):
            store.set_unit_state("missing", "pass")


def test_contract_and_decision_log_are_versioned_database_records(tmp_path):
    with opened(tmp_path) as store:
        view = store.contract_view()
        assert view["version"] == contract.CURRENT_CONTRACT_VERSION
        assert view["implementation_version"] == contract.IMPLEMENTATION_VERSION
        assert len(view["rules"]) >= 20
        assert len(view["decision_log"]) >= 15
        keys = {rule["rule_key"] for rule in view["rules"]}
        assert {
            "explicit-command",
            "provider-graph",
            "individual-and-combined",
            "complete-repository",
            "independent-lenses",
            "required-verification",
            "dynamic-checks",
            "finding-memory",
            "waiver-isolation",
            "freshness",
            "store-boundary",
            "publication",
            "security",
            "reports",
        } <= keys
        assert all(rule["title"] and rule["body"] for rule in view["rules"])
        assert all(item["title"] and item["body"] for item in view["decision_log"])
        assert store.contract_view("missing") == {}


def test_contract_upgrade_never_rewrites_historical_run_or_rules(tmp_path):
    with opened(tmp_path) as store:
        original = start(store)
        original_contract = store.contract_view()
        upgraded = contract.contract_seed(
            version="test-contract-v2",
            implementation_version="test-next",
            extra_rules=[
                {
                    "rule_key": "future-rule",
                    "title": "Future rule",
                    "body": "A subsequent migration adds rather than rewrites.",
                }
            ],
        )
        store.install_contract(upgraded)
        store.install_contract(upgraded)
        current = store.contract_view("test-contract-v2")
        assert current["rules"][-1]["rule_key"] == "future-rule"
        historical = store.manifest(original["run_id"])["contract"]
        assert historical == original_contract
        assert store.contract_view()["version"] == "test-contract-v2"
        assert store.manifest(original["run_id"])["run"]["contract_version"] == \
            contract.CURRENT_CONTRACT_VERSION
        with pytest.raises(ValueError, match="immutable"):
            store.install_contract({**upgraded, "rules": upgraded["rules"][:-1]})


def test_unknown_contract_and_acceptance_versions_are_explicit(tmp_path):
    with opened(tmp_path) as store:
        with pytest.raises(StoreError, match="unknown contract"):
            store.start_run(stack(), contract_version="not-installed")
        with pytest.raises(StoreError, match="unknown acceptance"):
            store.start_run(stack(), acceptance_version="not-installed")
        identity = store.start_run(stack(), continuity_required=False)
        run = store.manifest(identity["run_id"])["run"]
        assert run["acceptance_version"] == acceptance.CURRENT_ACCEPTANCE_VERSION
        assert run["continuity_required"] is False


def test_acceptance_identity_defaults_to_current_and_preserves_two_run_history(tmp_path):
    path = tmp_path / "acceptance-history.sqlite3"
    with opened(tmp_path, path.name) as store:
        registry = acceptance.AcceptanceRegistry(store)
        old_version = store.resolve_acceptance_version()
        old_run = start(store)
        old_acceptance = registry.start(
            "spec16-01-standalone-provider-parity", review_run_id=old_run["run_id"],
        )
        upgraded = acceptance.acceptance_seed(version="spec-16/store-test-v2")
        registry.install(upgraded)

        new_run = start(store, source_sha="c" * 40)
        new_acceptance = registry.start(
            "spec16-01-standalone-provider-parity", review_run_id=new_run["run_id"],
        )

        assert old_version == acceptance.CURRENT_ACCEPTANCE_VERSION
        assert old_run["acceptance_version"] == old_version
        assert new_run["acceptance_version"] == "spec-16/store-test-v2"
        assert old_acceptance["version"] == old_run["acceptance_version"]
        assert new_acceptance["version"] == new_run["acceptance_version"]
        assert store.manifest(old_run["run_id"])["run"]["acceptance_version"] == old_version
        assert store.machine_report(
            old_run["run_id"], old_run["unit_ids"]["combined"]
        )["run"]["acceptance_version"] == old_version
        assert store.machine_report(
            new_run["run_id"], new_run["unit_ids"]["individual:1"]
        )["run"]["acceptance_version"] == "spec-16/store-test-v2"
        assert registry.history(review_run_id=old_run["run_id"])[0]["version"] == old_version
        assert registry.history(review_run_id=new_run["run_id"])[0]["version"] == \
            "spec-16/store-test-v2"

    with FinalStore(path) as reopened:
        history = reopened.run_history()
        assert [item["run"]["acceptance_version"] for item in history] == [
            acceptance.CURRENT_ACCEPTANCE_VERSION, "spec-16/store-test-v2",
        ]
        assert len(reopened.acceptance_view(old_version)["scenarios"]) == 21
        assert len(reopened.acceptance_view("spec-16/store-test-v2")["scenarios"]) == 21
        assert len(reopened.acceptance_history()) == 2


def test_reopen_does_not_backdate_legacy_empty_acceptance_identity(tmp_path):
    path = tmp_path / "legacy-empty-acceptance.sqlite3"
    with opened(tmp_path, path.name) as store:
        legacy = start(store)
        store.connection.execute(
            "UPDATE runs SET acceptance_version = NULL WHERE run_id = ?", (legacy["run_id"],)
        )
        assert store.acceptance_history() == []

    with FinalStore(path) as reopened:
        assert reopened.acceptance_history() == []
        assert reopened.resolve_acceptance_version() == acceptance.CURRENT_ACCEPTANCE_VERSION
        assert reopened.manifest(legacy["run_id"])["run"]["acceptance_version"] is None
        current = start(reopened, source_sha="c" * 40)
        assert current["acceptance_version"] == acceptance.CURRENT_ACCEPTANCE_VERSION
        assert reopened.manifest(legacy["run_id"])["run"]["acceptance_version"] is None


def test_full_run_evidence_round_trip_and_atomic_result_batch(tmp_path):
    with opened(tmp_path) as store:
        identity = start(store)
        run_id = identity["run_id"]
        unit_id = identity["unit_ids"]["individual:1"]
        combined_id = identity["unit_ids"]["combined"]
        store.record_lens(
            unit_id,
            "correctness",
            "completed",
            summary="Reviewed the complete range",
            evidence={"diff_bytes": 42},
        )
        store.record_test(
            unit_id,
            "tests/test_widget.py::test_save",
            "required_suite",
            "failed",
            output="E   AssertionError: save did not persist\n",
            evidence_fingerprint="f" * 64,
            details={"exit_code": 1, "rerun": "reproduced"},
        )
        finding_id = store.record_finding(
            unit_id,
            "save-contract",
            "Save drops the supplied value",
            severity="high",
            confidence="high",
            lens="correctness",
            path="widget.py",
            line=17,
            anchors=[
                {
                    "anchor_type": "content",
                    "anchor": "sha256:abc",
                    "evidence_fingerprint": "f" * 64,
                }
            ],
            evidence={"test": "tests/test_widget.py::test_save"},
        )
        assert uuid.UUID(finding_id)
        store.record_warning(
            unit_id, "intent-unavailable", "Linked issue could not be fetched",
            details={"attempted": True},
        )
        store.record_coverage(
            unit_id, "repository", "complete", "Complete checkout available",
            details={"submodules": "complete", "lfs": "complete"},
        )
        stop_id = store.record_stop(
            combined_id,
            "Composition conflict makes downstream checks invalid",
            evidence={"conflict": "widget.py"},
            affected_unit_ids=[combined_id],
            skipped_work=[
                {"unit_id": combined_id, "kind": "lens", "name": "integration"},
                {"unit_id": combined_id, "kind": "required_suite", "name": "pytest"},
            ],
        )
        assert uuid.UUID(stop_id)
        store.set_unit_state(unit_id, "findings")
        store.set_unit_state(combined_id, "incomplete")
        store.record_publication(
            run_id,
            item_key="123",
            unit_id=combined_id,
            status="local_only",
            status_context="review-gate/final",
            details={"reason": "status permission unavailable"},
            error="403 Forbidden",
        )
        trust = store.finish_run(
            run_id,
            "incomplete",
            trust={"local_only": True, "enforced": False, "reason": "no status permission"},
        )
        assert trust["local_only"] is True
        assert trust["enforced"] is False

        report = store.manifest(run_id)
        individual = report["units"][0]
        assert individual["lenses"][0]["name"] == "correctness"
        assert individual["lenses"][0]["evidence"] == {"diff_bytes": 42}
        assert individual["tests"][0] == {
            "test_id": individual["tests"][0]["test_id"],
            "name": "tests/test_widget.py::test_save",
            "kind": "required_suite",
            "state": "failed",
            "output": "E   AssertionError: save did not persist\n",
            "evidence_fingerprint": "f" * 64,
            "details": {"exit_code": 1, "rerun": "reproduced"},
            "recorded_at": individual["tests"][0]["recorded_at"],
        }
        assert individual["findings"][0]["finding_key"] == "save-contract"
        assert individual["findings"][0]["finding_id"] == finding_id
        assert individual["findings"][0]["state"] == "open"
        assert individual["findings"][0]["anchors"][0]["anchor_type"] == "content"
        assert individual["warnings"][0]["code"] == "intent-unavailable"
        assert individual["coverage"][0]["status"] == "complete"
        combined = report["units"][1]
        assert combined["stops"][0]["stop_id"] == stop_id
        assert combined["stops"][0]["affected_unit_ids"] == [combined_id]
        assert [item["name"] for item in combined["skipped_work"]] == ["integration", "pytest"]
        assert report["publications"][0]["run_id"] == run_id
        assert report["publications"][0]["unit_id"] == combined_id
        assert report["publications"][0]["status"] == "local_only"
        assert report["publications"][0]["error"] == "403 Forbidden"
        assert report["run"]["completed_at"]
        assert report["trust"]["reason"] == "no status permission"

        # JSON is valid machine output and can be archived without custom encoders.
        encoded = json.dumps(report, sort_keys=True)
        assert json.loads(encoded)["run"]["run_id"] == run_id


def test_human_report_lists_every_auditable_category_and_combined_links(tmp_path):
    with opened(tmp_path) as store:
        identity = start(store)
        run_id = identity["run_id"]
        individual = identity["unit_ids"]["individual:1"]
        combined = identity["unit_ids"]["combined"]
        store.record_lens(individual, "correctness", "completed")
        store.record_test(individual, "pytest", "required_suite", "passed", output="1 passed")
        store.record_finding(
            individual, "f1", "A blocking defect", severity="high", confidence="high"
        )
        store.record_warning(individual, "intent", "Missing intent")
        store.record_coverage(individual, "diff", "complete", "Full merge-base diff")
        store.record_stop(
            combined, "Unsafe continuation", affected_unit_ids=[combined],
            skipped_work=[{"unit_id": combined, "kind": "lens", "name": "security"}],
        )
        store.record_publication(
            run_id, item_key="123", status="degraded", error="read-only token"
        )
        store.finish_run(run_id, "incomplete", trust={"local_only": True, "enforced": False})

        text = store.human_report(run_id)
        for expected in (
            "# Final review report",
            store.store_instance_id,
            run_id,
            "a" * 40,
            "b" * 40,
            "Contract:",
            "Trust:",
            "UNENFORCED",
            "individual:1",
            "correctness",
            "pytest",
            "1 passed",
            "A blocking defect",
            "Missing intent",
            "Full merge-base diff",
            "Unsafe continuation",
            "security",
            "read-only token",
            "Individual reports:",
            individual,
        ):
            assert expected in text
        assert "State: incomplete" in text

        unit_text = store.human_report(run_id, combined)
        assert "combined" in unit_text
        assert "Individual reports:" in unit_text
        assert individual in unit_text
        assert "A blocking defect" not in unit_text


def test_machine_unit_report_is_linked_to_parent_without_losing_siblings(tmp_path):
    with opened(tmp_path) as store:
        identity = start(store)
        combined = identity["unit_ids"]["combined"]
        individual = identity["unit_ids"]["individual:1"]
        report = store.machine_report(identity["run_id"], combined)
        assert report["run"]["run_id"] == identity["run_id"]
        assert report["run"]["acceptance_version"] == acceptance.CURRENT_ACCEPTANCE_VERSION
        assert len(report["units"]) == 1
        assert report["units"][0]["unit_id"] == combined
        assert report["units"][0]["individual_unit_ids"] == [individual]
        assert report["all_unit_ids"] == [individual, combined]


def test_foreign_keys_and_idempotency_constraints_are_enforced(tmp_path):
    path = tmp_path / "constraints.sqlite3"
    with FinalStore(path) as store:
        identity = start(store)
        unit_id = identity["unit_ids"]["combined"]
        with pytest.raises(StoreError, match="unknown unit"):
            store.record_lens("missing-unit", "correctness", "completed")
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(
                "INSERT INTO units(unit_id, run_id, unit_key, kind, base_sha, head_sha) "
                "VALUES ('bad', 'missing-run', 'bad', 'combined', 'a', 'b')"
            )
        store.record_coverage(unit_id, "repository", "complete", "first")
        store.record_coverage(unit_id, "repository", "complete", "first")
        assert len(store.manifest(identity["run_id"])["units"][1]["coverage"]) == 1


def test_manifest_is_read_only_and_survives_reopen(tmp_path):
    path = tmp_path / "persistent.sqlite3"
    with FinalStore(path) as store:
        identity = start(store)
        store.finish_run(identity["run_id"], "stale")
        before = store.manifest(identity["run_id"])
        before["run"]["state"] = "pass"
        assert store.manifest(identity["run_id"])["run"]["state"] == "stale"
    with FinalStore(path) as reopened:
        report = reopened.manifest(identity["run_id"])
        assert report["run"]["state"] == "stale"
        assert report["run"]["superseded_by_run_id"] is None
        assert report["revisions"]["root_target_sha"] == "b" * 40


def test_continuity_requirement_degrades_fresh_store_but_not_same_store_restart(tmp_path):
    with opened(tmp_path) as store:
        fresh = store.start_run(stack(), continuity_required=True)
        assert fresh["trust"]["independent"] is True
        assert fresh["trust"]["enforced"] is False
        store.finish_run(
            fresh["run_id"], "pass", trust={"local_only": False, "enforced": True}
        )
        trust = store.manifest(fresh["run_id"])["trust"]
        assert trust["local_only"] is True
        assert trust["enforced"] is False
        assert trust["degraded_reason"] == "fresh store has no same-store history"

        continued = store.start_run(
            stack(source_sha="c" * 40), continuity_required=True
        )
        assert continued["trust"]["independent"] is False
        assert continued["trust"]["continuity_available"] is True
        store.finish_run(
            continued["run_id"], "pass", trust={"local_only": False, "enforced": True}
        )
        assert store.manifest(continued["run_id"])["trust"]["enforced"] is True


def test_close_is_idempotent_and_use_after_close_is_explicit(tmp_path):
    store = opened(tmp_path)
    identity = start(store)
    store.close()
    store.close()
    with pytest.raises(StoreError, match="closed"):
        store.manifest(identity["run_id"])
