"""Public-seam tests for the versioned SPEC-16 acceptance registry."""
from __future__ import annotations

import copy
import json
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


def _synthetic_static_finding(lens, finding_id, content, *source_paths, **overrides):
    """Use the persisted worker-evidence shape emitted by real static lenses."""
    finding = {
        "finding_key": finding_id,
        "finding_id": f"accepted-uuid-{finding_id}",
        "lens": lens,
        "path": "app.py",
        "content": content,
        "state": "open",
        "severity": "high",
        "evidence": {
            "lens": lens,
            "worker": {
                "id": finding_id,
                "lens": lens,
                "category": lens,
                "content": content,
                "evidence": [
                    {"source": path, "detail": content} for path in source_paths
                ],
            },
        },
        "anchors": [{
            "anchor_type": "content",
            "details": {"path": source_paths[0] if source_paths else "app.py"},
            "evidence_fingerprint": f"synthetic-{finding_id}",
        }],
    }
    finding.update(overrides)
    return finding


def _unchanged_contract_evidence(lens="compatibility/migrations"):
    return _synthetic_static_finding(
        lens, "COMPAT-001",
        "New app.py returns 1, contradicting the unchanged ADR-001 contract in "
        "docs/ADR.md which requires 2.",
        "app.py", "docs/ADR.md",
    )


def _comment_standard_evidence(lens="documentation/domain"):
    return _synthetic_static_finding(
        lens, "DOC-1",
        "The public function in app.py has no docstring, violating the accurate "
        "docstring requirement in AGENTS.md.",
        "app.py", "AGENTS.md",
    )


def _testing_standard_evidence(lens="compatibility/migrations"):
    return _synthetic_static_finding(
        lens, "COMPAT-2",
        "The app.py behavior change ships without the permanent regression test "
        "required by AGENTS.md; no test file was added.",
        "app.py", "AGENTS.md",
    )


def _run_synthetic_static_acceptance(tmp_path, monkeypatch, scenario_index, findings):
    """Run acceptance with real fixture/doc checks and doubled inference output."""
    import sim_scenarios_spec16 as scenarios

    def simulated_review(_reference, *, artifact_dir, **_kwargs):
        artifact_dir = Path(artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        unit_id = "00000000-0000-0000-0000-000000000001"
        lenses = sorted({finding["lens"] for finding in findings})
        stored_unit = {
            "unit_key": "individual:1",
            "unit_id": unit_id,
            "state": "findings",
            "findings": findings,
            "warnings": [],
            "lenses": [{"name": lens, "state": "completed"} for lens in lenses],
            "coverage": [
                {"kind": "repository", "status": "complete"},
                {"kind": "diff", "status": "complete"},
                {"kind": "mandatory_lenses", "status": "complete"},
            ],
        }
        machine_path = artifact_dir / "synthetic-machine-report.json"
        machine_path.write_text(json.dumps({"units": [stored_unit]}), encoding="utf-8")
        result = {
            "run_id": "00000000-0000-0000-0000-000000000101",
            "state": "findings",
            "machine_report_path": str(machine_path),
            "units": [{
                "unit_key": "individual:1", "unit_id": unit_id,
                "state": "findings", "lenses": stored_unit["lenses"],
            }],
        }
        return {
            "artifact": scenarios.sim.persist_simulation_artifact(
                artifact_dir, "synthetic-review-output.json", result,
            ),
            "result": result,
            "error": "",
            "stderr": "",
            "backend": {"name": "deterministic-test-double"},
            "source_unchanged": True,
            "cleanup": {
                "clean": True, "registered_scratch_clones": [],
                "materialized_repositories_remaining": [],
            },
        }

    monkeypatch.setattr(
        scenarios.sim, "run_final_review_simulation", simulated_review,
    )
    scenario_id = EXPECTED_SCENARIO_IDS[scenario_index]
    with FinalStore(tmp_path / f"static-{scenario_index}.sqlite3") as store:
        registry = acceptance.AcceptanceRegistry(store, handlers=scenarios.DISPATCH)
        result = scenarios.run_scenario(
            registry, scenario_id,
            context={"real_model": True, "test_tmp_path": str(tmp_path)},
        )
        history = registry.history(scenario_id)
    assert result["result"]["details"]["provider_writes"] is False
    assert result["result"]["details"]["source_repository_unchanged"] is True
    assert result["result"]["details"]["cleanup"]["clean"] is True
    assert len(history) == 1 and history[0]["result"] == result["result"]
    return result


@pytest.mark.parametrize("lens", ("compatibility/migrations", "change-intent"))
def test_real_static_accepts_unchanged_contract_contradiction_from_any_static_lens(
        tmp_path, monkeypatch, lens):
    finding = _unchanged_contract_evidence(lens)
    result = _run_synthetic_static_acceptance(tmp_path, monkeypatch, 4, [finding])

    assert result["state"] == "findings"
    evidence = result["result"]["evidence"]
    assert evidence["domain_finding"]["findings"] == [finding]
    assert evidence["unchanged_document"]["unchanged_in_diff"] is True
    assert evidence["unchanged_document"]["base_and_head_identical"] is True
    assert "ADR-001" in evidence["unchanged_document"]["document"]
    assert all(
        evidence["complete_tree_evidence"][kind]["status"] == "complete"
        for kind in ("repository", "diff", "mandatory_lenses")
    )
    assert result["result"]["result_id"]
    assert result["result"]["recorded_at"] == result["completed_at"]


def test_real_static_accepts_comment_and_testing_rules_by_evidence_not_lens_name(
        tmp_path, monkeypatch):
    comment = _comment_standard_evidence()
    testing = _testing_standard_evidence()
    unrelated = _synthetic_static_finding(
        "security", "SEC-1", "No exploitable security defect was found.", "app.py",
    )
    result = _run_synthetic_static_acceptance(
        tmp_path, monkeypatch, 5, [comment, testing, unrelated],
    )

    assert result["state"] == "findings"
    evidence = result["result"]["evidence"]
    assert evidence["comment_finding"]["findings"] == [comment]
    assert evidence["testing_standard_finding"]["findings"] == [testing]
    assert evidence["incremental_scope_exclusion"]["review_mode"] == "complete-repository"
    assert set(evidence["incremental_scope_exclusion"]["lenses"]) == {
        "compatibility/migrations", "documentation/domain", "security",
    }


@pytest.mark.parametrize("bad_slot,bad_finding", (
    (
        "comment",
        _synthetic_static_finding(
            "repository-standards", "CERT-1",
            "Rotate the unrelated TLS certificate before its expiry.", "app.py", "AGENTS.md",
        ),
    ),
    (
        "testing",
        _synthetic_static_finding(
            "testing", "MIGRATION-1",
            "An unrelated schema migration should use a shorter lock timeout.",
            "app.py", "AGENTS.md",
        ),
    ),
    (
        "testing",
        _synthetic_static_finding(
            "testing", "TOOL-1",
            "app.py needs the permanent regression test required by AGENTS.md, but "
            "execution failed: python not found; no test result is available.",
            "app.py", "AGENTS.md",
        ),
    ),
    (
        "comment",
        _synthetic_static_finding(
            "repository-standards", "DYNAMIC-1",
            "app.py has no docstring under AGENTS.md, but the dynamic probe failed",
            "app.py", "AGENTS.md", evidence_source="ephemeral_test",
        ),
    ),
    (
        "comment",
        _synthetic_static_finding(
            "repository-standards", "UNPINNED-1",
            "No test, docstring, or document in app.py or AGENTS.md states which "
            "return value the external caller needs; this is not a docstring-rule finding.",
            "app.py", "AGENTS.md",
        ),
    ),
))
def test_real_static_rejects_unrelated_or_unavailable_rule_certificates(
        tmp_path, monkeypatch, bad_slot, bad_finding):
    # The companion finding uses the legacy selector's expected lens, so only
    # the invalid certificate prevents a false green under the old matching.
    good_comment = _comment_standard_evidence("repository-standards")
    good_testing = _testing_standard_evidence("testing")
    findings = [
        bad_finding if bad_slot == "comment" else good_comment,
        bad_finding if bad_slot == "testing" else good_testing,
    ]
    result = _run_synthetic_static_acceptance(tmp_path, monkeypatch, 5, findings)

    assert result["state"] == "error"
    assert "required finding(s)" in result["error"]
    evidence = result["result"]["evidence"]
    expected_comment = [] if bad_slot == "comment" else [good_comment]
    expected_testing = [] if bad_slot == "testing" else [good_testing]
    assert evidence["comment_finding"]["findings"] == expected_comment
    assert evidence["testing_standard_finding"]["findings"] == expected_testing


@pytest.mark.parametrize("bad_finding", (
    _synthetic_static_finding(
        "documentation/domain", "SEC-2", "An unrelated security header could be tightened.",
        "app.py", "docs/ADR.md",
    ),
    _synthetic_static_finding(
        "documentation/domain", "MIGRATION-2",
        "An unrelated migration could retain an additional deployment index.",
        "app.py", "docs/ADR.md",
    ),
    _synthetic_static_finding(
        "documentation/domain", "TOOL-2",
        "The unchanged ADR-001 contract in docs/ADR.md contradicts app.py, but "
        "validation failed with FileNotFoundError: python not found.",
        "app.py", "docs/ADR.md",
    ),
))
def test_real_static_domain_rejects_nits_and_tool_failures(tmp_path, monkeypatch, bad_finding):
    result = _run_synthetic_static_acceptance(tmp_path, monkeypatch, 4, [bad_finding])

    assert result["state"] == "error"
    assert result["result"]["evidence"]["domain_finding"]["findings"] == []


@pytest.mark.parametrize("scenario_index", (4, 5))
def test_real_static_missing_actual_finding_remains_an_error(tmp_path, monkeypatch, scenario_index):
    result = _run_synthetic_static_acceptance(tmp_path, monkeypatch, scenario_index, [])

    assert result["state"] == "error"
    assert "completed without the scenario's required finding(s)" in result["error"]
    assert result["result"]["details"]["executed"] is True
    assert result["completed_at"]
