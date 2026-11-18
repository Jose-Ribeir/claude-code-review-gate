"""Public-seam tests for the versioned SPEC-16 acceptance registry."""
from __future__ import annotations

import copy
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rg_final_acceptance as acceptance  # noqa: E402
import rg_final_store as final_store  # noqa: E402
from rg_final_store import FinalStore, StoreError  # noqa: E402


EXPECTED_SCENARIO_IDS = (
    "spec16-01-standalone-provider-parity",
    "spec16-02-linear-stack-individual-combined",
    "spec16-03-omitted-graph-items-corrected-command",
    "spec16-04-intermediate-defect-survives-composition",
    "spec16-05-unchanged-domain-contract",
    "spec16-06-final-only-comment-testing-standards",
    "spec16-07-foreign-fork-isolation",
    "spec16-08-existing-regression-output",
    "spec16-09-ephemeral-probe-attribution-cleanup",
    "spec16-10-dynamic-degradation-warnings",
    "spec16-11-required-suite-outcomes",
    "spec16-12-revision-race-stale",
    "spec16-13-invalid-composition-and-graphs",
    "spec16-14-publication-permission-degradation",
    "spec16-15-local-waiver-isolation",
    "spec16-16-finding-memory-lifecycle",
    "spec16-17-safe-completion-and-scoped-stop",
    "spec16-18-untrusted-execution-cleanup",
    "spec16-19-same-store-supersession",
    "spec16-20-fresh-store-independence",
    "spec16-21-versioned-history-and-views",
)


def test_install_lists_all_actionable_scenarios_without_seeding_results(tmp_path):
    with FinalStore(tmp_path / "acceptance.sqlite3") as store:
        registry = acceptance.AcceptanceRegistry(store)
        installed = registry.install()
        scenarios = registry.list()

        assert installed["version"] == acceptance.CURRENT_ACCEPTANCE_VERSION
        assert tuple(item["scenario_id"] for item in scenarios) == EXPECTED_SCENARIO_IDS
        assert len(scenarios) == 21
        for position, scenario in enumerate(scenarios, 1):
            assert scenario["scenario_id"].startswith(f"spec16-{position:02d}-")
            assert scenario["version"] == acceptance.CURRENT_ACCEPTANCE_VERSION
            assert scenario["title"].strip()
            assert scenario["behavior"].strip()
            assert scenario["expected_state"] in acceptance.RESULT_STATES
            assert scenario["required_evidence"]
            assert all(item.strip() for item in scenario["required_evidence"])
            assert scenario["dispatch_key"].strip()
            assert scenario["cleanup_assertions"]
            assert scenario["warning_assertions"]
            assert scenario["trust_assertions"]
        assert registry.history() == []
        assert acceptance.ACCEPTANCE_STATES == {
            "pending", "running", "pass", "findings", "incomplete", "stale", "error",
        }


def evidence_for(registry, scenario_id, *, version=None):
    scenarios = registry.list(version) if version else registry.list()
    scenario = next(item for item in scenarios if item["scenario_id"] == scenario_id)
    return {key: {"observed": key} for key in scenario["required_evidence"]}


def review_stack():
    return {
        "provider": "gitlab",
        "target_project": "group/project",
        "root_target_ref": "main",
        "root_target_sha": "b" * 40,
        "items": [{
            "item_key": "123",
            "source_project": "fork/project",
            "source_ref": "feature",
            "source_sha": "a" * 40,
            "target_project": "group/project",
            "target_ref": "main",
            "target_sha": "b" * 40,
        }],
    }


def test_attempt_results_are_independent_durable_and_human_inspectable(tmp_path):
    path = tmp_path / "attempts.sqlite3"
    scenario_id = EXPECTED_SCENARIO_IDS[0]
    with FinalStore(path) as store:
        registry = acceptance.AcceptanceRegistry(store)
        first = registry.start(scenario_id, details={"operator": "first"})
        second = registry.start(scenario_id)
        assert uuid.UUID(first["acceptance_run_id"])
        assert uuid.UUID(second["acceptance_run_id"])
        assert first["acceptance_run_id"] != second["acceptance_run_id"]
        assert first["state"] == second["state"] == "running"
        assert first["result"] is second["result"] is None

        evidence = evidence_for(registry, scenario_id)
        completed = registry.complete(
            first["acceptance_run_id"], "pass", evidence=evidence,
            details={"process_exit": 0},
        )
        failed = registry.complete(
            second["acceptance_run_id"], "error", error="harness exited before execution",
        )
        assert completed["state"] == "pass"
        assert completed["result"]["evidence"] == evidence
        assert completed["result"]["details"] == {"process_exit": 0}
        assert completed["completed_at"] == completed["result"]["recorded_at"]
        assert failed["state"] == "error"
        assert failed["error"] == "harness exited before execution"
        assert failed["result"]["evidence"] == {}
        assert completed["result"]["result_id"] != failed["result"]["result_id"]

        history = registry.history(scenario_id)
        assert [item["acceptance_run_id"] for item in history] == [
            first["acceptance_run_id"], second["acceptance_run_id"],
        ]
        assert history[0]["details"] == {"process_exit": 0}
        with pytest.raises(StoreError, match="already complete"):
            registry.complete(first["acceptance_run_id"], "incomplete")

    with FinalStore(path) as reopened:
        retained = acceptance.AcceptanceRegistry(reopened).history(scenario_id)
        assert retained == history
        retained[0]["result"]["evidence"].clear()
        assert acceptance.AcceptanceRegistry(reopened).history(scenario_id)[0]["result"][
            "evidence"
        ]


def test_claimed_pass_requires_every_declared_evidence_item(tmp_path):
    with FinalStore(tmp_path / "evidence.sqlite3") as store:
        registry = acceptance.AcceptanceRegistry(store)
        run = registry.start(EXPECTED_SCENARIO_IDS[0])
        with pytest.raises(ValueError, match="missing evidence"):
            registry.complete(run["acceptance_run_id"], "pass", evidence={"gitlab_result": "ok"})
        assert store.acceptance_run(run["acceptance_run_id"])["state"] == "running"
        assert store.acceptance_run(run["acceptance_run_id"])["result"] is None
        incomplete = registry.complete(
            run["acceptance_run_id"], "incomplete", error="evidence collection stopped",
        )
        assert incomplete["state"] == "incomplete"
        assert incomplete["error"] == "evidence collection stopped"


def test_acceptance_upgrade_retains_original_attempt_and_definitions(tmp_path):
    original_id = EXPECTED_SCENARIO_IDS[19]
    with FinalStore(tmp_path / "versions.sqlite3") as store:
        registry = acceptance.AcceptanceRegistry(store)
        review = store.start_run(
            review_stack(), acceptance_version=acceptance.CURRENT_ACCEPTANCE_VERSION,
        )
        old = registry.start(original_id, review_run_id=review["run_id"])
        old_evidence = evidence_for(registry, original_id)
        registry.complete(old["acceptance_run_id"], "pass", evidence=old_evidence)
        original_definition = copy.deepcopy(old["scenario"])
        assert registry.history(review_run_id=review["run_id"])[0][
            "acceptance_run_id"
        ] == old["acceptance_run_id"]

        upgraded = acceptance.acceptance_seed(
            version="spec-16/test-v2", implementation_version="spec-16.C16/test-v2",
        )
        changed = next(
            item for item in upgraded["scenarios"] if item["scenario_id"] == original_id
        )
        changed["title"] = "Fresh-store independence with an added audit assertion"
        changed["trust_assertions"].append("future_audit_assertion")
        registry.install(upgraded)
        registry.install(upgraded)

        assert store.acceptance_view()["version"] == "spec-16/test-v2"
        historical = store.acceptance_view(acceptance.CURRENT_ACCEPTANCE_VERSION)
        assert len(historical["scenarios"]) == 21
        retained = registry.history(original_id, version=acceptance.CURRENT_ACCEPTANCE_VERSION)
        assert len(retained) == 1
        assert retained[0]["scenario"] == original_definition
        assert retained[0]["result"]["evidence"] == old_evidence
        assert retained[0]["version"] == acceptance.CURRENT_ACCEPTANCE_VERSION
        assert retained[0]["review_run_id"] == review["run_id"]
        assert store.manifest(review["run_id"])["run"]["acceptance_version"] == \
            acceptance.CURRENT_ACCEPTANCE_VERSION

        current = registry.start(original_id)
        assert current["version"] == "spec-16/test-v2"
        assert "future_audit_assertion" in current["scenario"]["trust_assertions"]
        old_again = registry.start(original_id, version=acceptance.CURRENT_ACCEPTANCE_VERSION)
        assert old_again["scenario"] == original_definition
        assert len(registry.history(original_id)) == 3
        assert len(registry.history(review_run_id=review["run_id"])) == 1
        assert registry.history(version="not-installed") == []
        with pytest.raises(StoreError, match="unknown run"):
            registry.start(original_id, review_run_id="not-a-review-run")

        changed_again = copy.deepcopy(upgraded)
        changed_again["scenarios"][0]["behavior"] = "rewritten behavior under the same version"
        with pytest.raises(ValueError, match="immutable"):
            registry.install(changed_again)
        assert store.acceptance_view("spec-16/test-v2")["scenarios"][0]["behavior"] != \
            "rewritten behavior under the same version"


def test_schema_migration_records_are_stable_across_reopen(tmp_path):
    path = tmp_path / "migration.sqlite3"
    with FinalStore(path) as store:
        assert store.schema_version == final_store.SCHEMA_VERSION
        versions = [row[0] for row in store.connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )]
        assert versions == list(range(1, final_store.SCHEMA_VERSION + 1))
    with FinalStore(path) as reopened:
        assert reopened.schema_version == final_store.SCHEMA_VERSION
        assert [row[0] for row in reopened.connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )] == versions
        assert len(acceptance.AcceptanceRegistry(reopened).list()) == 21


def test_dispatch_coverage_is_explicit_and_stubs_cannot_pass(tmp_path):
    import sim_scenarios_spec16 as scenarios

    with FinalStore(tmp_path / "dispatch.sqlite3") as store:
        registry = acceptance.AcceptanceRegistry(store, handlers=scenarios.DISPATCH)
        definitions = registry.list()
        assert set(scenarios.DISPATCH) == {item["dispatch_key"] for item in definitions}
        assert scenarios.STUBBED_DISPATCH == {
            item["dispatch_key"] for item in definitions
            if scenarios.DISPATCH[item["dispatch_key"]] is None
        }
        assert len(scenarios.STUBBED_DISPATCH) <= 19
        assert {"same_store_supersession", "fresh_store_independence"} <= (
            set(scenarios.DISPATCH) - scenarios.STUBBED_DISPATCH
        )

        stubbed = registry.dispatch(
            EXPECTED_SCENARIO_IDS[0], handlers={"standalone_provider_parity": None},
        )
        assert stubbed["state"] == "incomplete"
        assert stubbed["result"] is not None
        assert stubbed["result"]["details"] == {"provider_writes": False, "executed": False}
        assert "intentionally stubbed for C18" in stubbed["error"]
        assert stubbed["result"]["evidence"]["dispatch"]["status"] == "stubbed"


@pytest.mark.parametrize("scenario_index", (18, 19))
def test_real_deterministic_dispatch_persists_end_to_end(tmp_path, scenario_index):
    import sim_scenarios_spec16 as scenarios

    scenario_id = EXPECTED_SCENARIO_IDS[scenario_index]
    with FinalStore(tmp_path / f"deterministic-{scenario_index}.sqlite3") as store:
        registry = acceptance.AcceptanceRegistry(store, handlers=scenarios.DISPATCH)
        result = scenarios.run_scenario(
            registry, scenario_id, context={"test_tmp_path": str(tmp_path)},
        )

        assert result["state"] == "pass"
        assert result["result"]["state"] == "pass"
        assert result["completed_at"]
        assert set(result["result"]["evidence"]) == set(
            result["scenario"]["required_evidence"]
        )
        assert all(result["result"]["evidence"].values())
        assert result["result"]["details"]["provider_writes"] is False
        assert result["result"]["details"]["mode"] == "deterministic"
        retained = registry.history(scenario_id)
        assert len(retained) == 1
        assert retained[0]["acceptance_run_id"] == result["acceptance_run_id"]
        assert retained[0]["result"] == result["result"]


def test_handler_exception_and_false_green_are_recorded_as_errors(tmp_path):
    with FinalStore(tmp_path / "dispatch-errors.sqlite3") as store:
        registry = acceptance.AcceptanceRegistry(store)

        def explode(_context):
            raise RuntimeError("deterministic fixture failed")

        failed = registry.dispatch(EXPECTED_SCENARIO_IDS[0], handlers={
            "standalone_provider_parity": explode,
        })
        assert failed["state"] == "error"
        assert failed["error"] == "RuntimeError: deterministic fixture failed"
        assert failed["result"]["details"]["executed"] is True

        false_green = registry.dispatch(EXPECTED_SCENARIO_IDS[0], handlers={
            "standalone_provider_parity": lambda _context: {
                "state": "pass", "evidence": {"gitlab_result": "only one provider"},
            },
        })
        assert false_green["state"] == "error"
        assert "missing evidence" in false_green["error"]
        assert registry.history(EXPECTED_SCENARIO_IDS[0])[-1]["result"] is not None


def test_acceptance_seed_is_fully_detached_between_migrations():
    first = acceptance.acceptance_seed()
    second = acceptance.acceptance_seed()
    first["scenarios"][0]["required_evidence"].append("unexpected")
    first["scenarios"][0]["cleanup_assertions"].clear()
    first["scenarios"][0]["warning_assertions"].append("unexpected")
    first["scenarios"][0]["trust_assertions"].append("unexpected")
    assert second == acceptance.acceptance_seed()
    assert first["scenarios"][0]["scenario_id"] == second["scenarios"][0]["scenario_id"]
    assert first["scenarios"][0]["required_evidence"] != second["scenarios"][0][
        "required_evidence"
    ]
