"""Vertical SPEC-16 operator views over real temporary SQLite stores.

Every assertion about an operator view goes through ``review-gate.py``. Store
APIs are used only to establish review and acceptance history before invoking
the public, read-only commands.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "review-gate.py"
sys.path.insert(0, str(ROOT / "scripts"))

import rg_final_acceptance as acceptance  # noqa: E402
import rg_final_contract as contract  # noqa: E402
from rg_final_store import FinalStore  # noqa: E402
import test_final_complete_standalone as standalone_case  # noqa: E402
from test_final_complete_standalone import standalone as standalone_harness  # noqa: E402, F401
import test_final_stack as stack_case  # noqa: E402


OLD_CONTRACT = contract.CURRENT_CONTRACT_VERSION
OLD_ACCEPTANCE = acceptance.CURRENT_ACCEPTANCE_VERSION
NEW_CONTRACT = "spec-16/operator-test-v2"
NEW_ACCEPTANCE = "spec-16/acceptance-operator-test-v2"
SCENARIO_ID = "spec16-01-standalone-provider-parity"


def review_stack(source_sha="a" * 40, target_sha="b" * 40):
    return {
        "provider": "gitlab",
        "target_project": "group/project",
        "root_target_ref": "main",
        "root_target_sha": target_sha,
        "items": [{
            "item_key": "123",
            "source_project": "contributor/fork",
            "source_ref": "feature",
            "source_sha": source_sha,
            "target_project": "group/project",
            "target_ref": "main",
            "target_sha": target_sha,
            "base_sha": target_sha,
            "reference": "https://gitlab.example.test/group/project/-/merge_requests/123",
        }],
    }


def upgraded_documents():
    contract_v2 = contract.contract_seed(
        version=NEW_CONTRACT,
        implementation_version="spec-16.C17/test-v2",
        extra_rules=[{
            "rule_key": "operator-view-v2-only",
            "title": "Only the upgraded contract has this rule",
            "body": "Historical reports must continue to render their original rules.",
        }],
        extra_decisions=[{
            "decision_key": "operator-view-v2-decision",
            "title": "An additive decision",
            "body": "The new decision must not leak into an old report.",
        }],
    )
    acceptance_v2 = acceptance.acceptance_seed(
        version=NEW_ACCEPTANCE,
        implementation_version="spec-16.C17/acceptance-test-v2",
    )
    changed = next(
        scenario for scenario in acceptance_v2["scenarios"]
        if scenario["scenario_id"] == SCENARIO_ID
    )
    changed["title"] = "Current registry title"
    changed["required_evidence"].append("current_only_evidence")
    changed["trust_assertions"].append("current_only_trust_assertion")
    return contract_v2, acceptance_v2


def complete_scenario(store, scenario_id=SCENARIO_ID, *, version=None, review_run_id=None,
                      state="pass"):
    registry = acceptance.AcceptanceRegistry(store)
    started = registry.start(
        scenario_id, version=version, review_run_id=review_run_id,
        details={"operator-fixture": True},
    )
    evidence = {
        key: {"observed": key, "version": started["version"]}
        for key in started["scenario"]["required_evidence"]
    }
    return registry.complete(
        started["acceptance_run_id"], state,
        evidence=evidence if state in ("pass", "findings", "stale") else None,
        error="fixture incomplete" if state == "incomplete" else "",
        details={"result-origin": "deterministic-fixture"},
    )


def upgraded_history(path):
    """Create old and new runs/results under additive contract migrations."""
    with FinalStore(path) as store:
        registry = acceptance.AcceptanceRegistry(store)
        old_run = store.start_run(
            review_stack(), acceptance_version=OLD_ACCEPTANCE,
            metadata={"fixture": "historical"},
        )
        individual = old_run["unit_ids"]["individual:1"]
        combined = old_run["unit_ids"]["combined"]
        store.record_lens(individual, "correctness", "completed", "Historical lens")
        store.record_test(
            individual, "pytest historical", "required_suite", "passed",
            output="1 historical test passed", evidence_fingerprint="e" * 64,
        )
        store.record_finding(
            individual, "historical-finding", "Historical finding remains auditable",
            severity="low", confidence="low", lens="correctness", path="widget.py", line=9,
            anchors=[{
                "anchor_type": "content", "anchor": "sha256:historical",
                "evidence_fingerprint": "e" * 64,
            }],
        )
        store.record_warning(individual, "historical-warning", "Historical warning")
        store.record_coverage(individual, "repository", "complete", "Historical coverage")
        store.record_publication(
            old_run["run_id"], item_key="123", unit_id=combined,
            status="local_only", error="read-only fixture",
        )
        store.set_unit_state(individual, "pass")
        store.set_unit_state(combined, "pass")
        store.finish_run(old_run["run_id"], "pass")
        old_acceptance = complete_scenario(
            store, version=OLD_ACCEPTANCE, review_run_id=old_run["run_id"],
        )
        old_contract = store.contract_view(OLD_CONTRACT)

        contract_v2, acceptance_v2 = upgraded_documents()
        store.install_contract(contract_v2)
        registry.install(acceptance_v2)
        new_run = store.start_run(
            review_stack(source_sha="c" * 40, target_sha="d" * 40),
            contract_version=NEW_CONTRACT, acceptance_version=NEW_ACCEPTANCE,
            metadata={"fixture": "current"},
        )
        new_acceptance = complete_scenario(store, review_run_id=new_run["run_id"])
        assert new_acceptance["version"] == NEW_ACCEPTANCE
        old_manifest = store.manifest(old_run["run_id"])

        return {
            "store_id": store.store_instance_id,
            "old_run": old_run,
            "new_run": new_run,
            "old_acceptance": old_acceptance,
            "new_acceptance": new_acceptance,
            "old_contract": old_contract,
            "old_manifest": old_manifest,
            "history_before_views": registry.history(),
        }


def invoke(path, *args, expected=0, env=None):
    command = [sys.executable, str(GATE), *args, "--store", str(path)]
    process = subprocess.run(
        command, cwd=str(ROOT), env=env or os.environ.copy(),
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert process.returncode == expected, (
        f"command: {command}\nstdout: {process.stdout}\nstderr: {process.stderr}"
    )
    if expected == 0:
        assert process.stdout.strip(), process.stderr
    else:
        assert process.stderr.strip(), process.stdout
    return process


def json_view(path, *args):
    process = invoke(path, *args, "--json")
    return json.loads(process.stdout)


def install_current_acceptance(path):
    with FinalStore(path) as store:
        _contract, acceptance_document = upgraded_documents()
        acceptance.AcceptanceRegistry(store).install(acceptance_document)


def assert_supervisor_acceptance_identity(path, output, *, version=NEW_ACCEPTANCE):
    """Check one real supervisor result, its reports, and an explicit linked attempt."""
    run_id = output["run_id"]
    assert output["acceptance_version"] == version
    report_paths = {Path(output["machine_report_path"])}
    human_paths = {Path(output["human_report_path"])}
    for unit in output.get("units", ()):
        if unit.get("machine_report_path"):
            report_paths.add(Path(unit["machine_report_path"]))
        if unit.get("human_report_path"):
            human_paths.add(Path(unit["human_report_path"]))
    for report_path in report_paths:
        assert json.loads(report_path.read_text(encoding="utf-8"))["run"][
            "acceptance_version"
        ] == version
    for human_path in human_paths:
        assert f"Acceptance: {version}" in human_path.read_text(encoding="utf-8")

    with FinalStore(path) as store:
        assert store.resolve_acceptance_version() == version
        assert store.machine_report(run_id)["run"]["acceptance_version"] == version
        assert f"Acceptance: {version}" in store.human_report(run_id)
        registry = acceptance.AcceptanceRegistry(store)
        # Run metadata never manufactures acceptance evidence or an attempt.
        assert registry.history(review_run_id=run_id) == []
        attempt = registry.start(SCENARIO_ID, review_run_id=run_id)
        assert attempt["version"] == version
        assert attempt["review_run_id"] == run_id
        assert attempt["state"] == "running"
        assert attempt["result"] is None

    listed = json_view(path, "final-report", "list")
    listed_run = next(run for run in listed["runs"] if run["run_id"] == run_id)
    assert listed_run["acceptance_version"] == version
    assert json_view(path, "final-report", "show", run_id)["run"][
        "acceptance_version"
    ] == version
    assert f"Acceptance: {version}" in invoke(path, "final-report", "show", run_id).stdout
    history = json_view(
        path, "acceptance", "history", SCENARIO_ID, "--review-run-id", run_id,
    )
    assert [item["acceptance_run_id"] for item in history["acceptance_runs"]] == [
        attempt["acceptance_run_id"],
    ]
    assert history["acceptance_runs"][0]["review_run_id"] == run_id
    assert history["acceptance_runs"][0]["result"] is None
    readable = invoke(
        path, "acceptance", "history", SCENARIO_ID, "--review-run-id", run_id,
    ).stdout
    assert f"Review run: {run_id}" in readable
    assert "No result recorded." in readable


def test_standalone_supervisor_resolves_current_acceptance_into_all_public_reports(
        standalone_harness, tmp_path):  # noqa: F811
    path = tmp_path / "standalone-current-acceptance.sqlite3"
    install_current_acceptance(path)

    output = standalone_case.run(
        standalone_harness, standalone_case.Provider(), path,
    )
    execution = standalone_case.rg_final_review.execute_final_review(
        standalone_case.item()["url"], provider=standalone_case.Provider(), store_path=path,
        required_runner=standalone_harness.required, dynamic_runner=standalone_harness.dynamic,
        environ={},
    )

    assert output["state"] == "pass"
    assert execution["stage"] == standalone_case.rg_final_review.EXECUTION_STAGE
    assert_supervisor_acceptance_identity(path, output)
    assert_supervisor_acceptance_identity(path, execution)


def test_stack_supervisor_resolves_current_acceptance_into_every_public_report(tmp_path):
    path = tmp_path / "stack-current-acceptance.sqlite3"
    install_current_acceptance(path)
    harness = stack_case.Harness(tmp_path)

    output = harness.run(path)
    execution = stack_case.rg_final_review.execute_final_stack(
        harness.references, provider=harness.provider, repo_root=harness.repo,
        store_path=path, worker=[sys.executable, str(harness.worker)],
        environ=harness.environ, current_root_sha=harness.current_sha,
        required_runner=harness.required, dynamic_runner=harness.dynamic,
    )

    assert output["state"] == "pass"
    assert execution["stage"] == stack_case.rg_final_review.EXECUTION_STAGE
    assert_supervisor_acceptance_identity(path, output)
    assert_supervisor_acceptance_identity(path, execution)


def test_contract_show_is_database_backed_current_and_explicit_historical(tmp_path):
    path = tmp_path / "contract.sqlite3"
    fixture = upgraded_history(path)

    current = json_view(path, "contract", "show")
    historical = json_view(path, "contract", "show", "--version", OLD_CONTRACT)

    assert current["store"]["store_instance_id"] == fixture["store_id"]
    assert current["selection"] == "current"
    assert current["contract"]["version"] == NEW_CONTRACT
    assert current["contract"]["implementation_version"] == "spec-16.C17/test-v2"
    assert current["contract"]["rules"][-1]["rule_key"] == "operator-view-v2-only"
    assert historical["selection"] == "historical"
    assert historical["contract"] == fixture["old_contract"]
    assert historical["contract"]["version"] == OLD_CONTRACT
    assert all(
        rule["rule_key"] != "operator-view-v2-only"
        for rule in historical["contract"]["rules"]
    )

    readable = invoke(path, "contract", "show", "--version", OLD_CONTRACT).stdout
    assert "# Final review contract" in readable
    assert f"Store: {fixture['store_id']}" in readable
    assert f"Version: {OLD_CONTRACT}" in readable
    assert "Selection: historical" in readable
    assert "## Rules" in readable
    assert "## Decision log" in readable
    assert "Explicit final review" in readable


def test_acceptance_list_has_expected_evidence_and_exact_registry_versions(tmp_path):
    path = tmp_path / "acceptance-list.sqlite3"
    fixture = upgraded_history(path)

    current = json_view(path, "acceptance", "list")
    historical = json_view(path, "acceptance", "list", "--version", OLD_ACCEPTANCE)

    assert current["store_instance_id"] == fixture["store_id"]
    assert current["acceptance_version"] == NEW_ACCEPTANCE
    assert current["selection"] == "current"
    changed = next(item for item in current["scenarios"] if item["scenario_id"] == SCENARIO_ID)
    assert changed["title"] == "Current registry title"
    assert changed["required_evidence"][-1] == "current_only_evidence"
    assert changed["trust_assertions"][-1] == "current_only_trust_assertion"
    assert historical["acceptance_version"] == OLD_ACCEPTANCE
    assert historical["selection"] == "historical"
    original = next(
        item for item in historical["scenarios"] if item["scenario_id"] == SCENARIO_ID
    )
    assert original == fixture["old_acceptance"]["scenario"]
    assert "current_only_evidence" not in original["required_evidence"]

    readable = invoke(path, "acceptance", "list", "--version", OLD_ACCEPTANCE).stdout
    assert f"Store: {fixture['store_id']}" in readable
    assert f"Acceptance version: {OLD_ACCEPTANCE}" in readable
    assert SCENARIO_ID in readable
    assert "Expected state: pass" in readable
    assert "Required evidence:" in readable
    assert "gitlab_result" in readable
    assert "Cleanup assertions:" in readable
    assert "Trust assertions:" in readable


def test_acceptance_status_and_history_include_definitions_attempts_and_results(tmp_path):
    path = tmp_path / "acceptance-results.sqlite3"
    fixture = upgraded_history(path)

    old_status = json_view(
        path, "acceptance", "status", SCENARIO_ID, "--version", OLD_ACCEPTANCE,
    )
    current_status = json_view(
        path, "acceptance", "status", "--acceptance-run-id",
        fixture["new_acceptance"]["acceptance_run_id"],
    )
    history = json_view(path, "acceptance", "history", SCENARIO_ID)
    old_history = json_view(
        path, "acceptance", "history", SCENARIO_ID, "--version", OLD_ACCEPTANCE,
    )

    retained = old_status["acceptance_run"]
    assert retained == fixture["old_acceptance"]
    assert old_status["store_instance_id"] == fixture["store_id"]
    assert old_status["acceptance_version"] == OLD_ACCEPTANCE
    assert retained["result"]["evidence"]["gitlab_result"]["version"] == OLD_ACCEPTANCE
    assert retained["scenario"]["required_evidence"] == \
        fixture["old_acceptance"]["scenario"]["required_evidence"]
    assert current_status["acceptance_run"] == fixture["new_acceptance"]
    assert current_status["acceptance_version"] == NEW_ACCEPTANCE
    assert retained["review_run_id"] == fixture["old_run"]["run_id"]
    assert current_status["acceptance_run"]["review_run_id"] == fixture["new_run"]["run_id"]
    assert [item["review_run_id"] for item in history["acceptance_runs"]] == [
        fixture["old_run"]["run_id"], fixture["new_run"]["run_id"],
    ]
    assert [item["acceptance_run_id"] for item in history["acceptance_runs"]] == [
        fixture["old_acceptance"]["acceptance_run_id"],
        fixture["new_acceptance"]["acceptance_run_id"],
    ]
    assert [item["version"] for item in history["acceptance_runs"]] == [
        OLD_ACCEPTANCE, NEW_ACCEPTANCE,
    ]
    assert len(old_history["acceptance_runs"]) == 1
    assert old_history["acceptance_runs"][0] == fixture["old_acceptance"]

    readable = invoke(
        path, "acceptance", "status", SCENARIO_ID, "--version", OLD_ACCEPTANCE,
    ).stdout
    assert "Acceptance run:" in readable
    assert fixture["old_acceptance"]["acceptance_run_id"] in readable
    assert f"Review run: {fixture['old_run']['run_id']}" in readable
    assert "Result: pass" in readable
    assert "Expected evidence:" in readable
    assert "Result evidence:" in readable
    assert "deterministic-fixture" in readable


def test_report_list_shows_all_runs_original_versions_and_exact_revisions(tmp_path):
    path = tmp_path / "run-list.sqlite3"
    fixture = upgraded_history(path)

    view = json_view(path, "final-report", "list")
    assert view["store"]["store_instance_id"] == fixture["store_id"]
    assert [item["run_id"] for item in view["runs"]] == [
        fixture["old_run"]["run_id"], fixture["new_run"]["run_id"],
    ]
    old, new = view["runs"]
    assert old["contract_version"] == OLD_CONTRACT
    assert old["acceptance_version"] == OLD_ACCEPTANCE
    assert old["state"] == "pass"
    assert old["superseded_by_run_id"] == fixture["new_run"]["run_id"]
    assert old["revisions"] == fixture["old_manifest"]["revisions"]
    assert new["contract_version"] == NEW_CONTRACT
    assert new["acceptance_version"] == NEW_ACCEPTANCE
    assert new["revisions"]["root_target_sha"] == "d" * 40
    assert new["revisions"]["items"][0]["source_sha"] == "c" * 40

    readable = invoke(path, "final-report", "list").stdout
    assert f"Store: {fixture['store_id']}" in readable
    assert fixture["old_run"]["run_id"] in readable
    assert fixture["new_run"]["run_id"] in readable
    assert f"Contract: {OLD_CONTRACT}" in readable
    assert f"Acceptance: {NEW_ACCEPTANCE}" in readable
    assert "a" * 40 in readable
    assert "c" * 40 in readable


def test_run_and_unit_reports_retain_historical_contract_and_audit_evidence(tmp_path):
    path = tmp_path / "reports.sqlite3"
    fixture = upgraded_history(path)
    run_id = fixture["old_run"]["run_id"]
    unit_id = fixture["old_run"]["unit_ids"]["individual:1"]
    combined_id = fixture["old_run"]["unit_ids"]["combined"]

    report = json_view(path, "final-report", "show", run_id)
    unit = json_view(path, "final-report", "show", run_id, "--unit", unit_id)

    assert report == fixture["old_manifest"]
    assert report["store"]["store_instance_id"] == fixture["store_id"]
    assert report["run"]["contract_version"] == OLD_CONTRACT
    assert report["run"]["acceptance_version"] == OLD_ACCEPTANCE
    assert report["contract"] == fixture["old_contract"]
    assert all(rule["rule_key"] != "operator-view-v2-only" for rule in report["contract"]["rules"])
    assert report["revisions"]["items"][0]["source_sha"] == "a" * 40
    assert len(unit["units"]) == 1
    assert unit["units"][0]["unit_id"] == unit_id
    assert unit["all_unit_ids"] == [unit_id, combined_id]
    assert unit["units"][0]["tests"][0]["output"] == "1 historical test passed"
    assert unit["units"][0]["findings"][0]["content"] == \
        "Historical finding remains auditable"

    readable = invoke(path, "final-report", "show", run_id, "--unit", unit_id).stdout
    assert "# Final review report" in readable
    assert fixture["store_id"] in readable
    assert OLD_CONTRACT in readable
    assert "Historical lens" in readable
    assert "1 historical test passed" in readable
    assert "Historical finding remains auditable" in readable
    assert "Historical warning" in readable
    assert "Historical coverage" in readable


def test_export_json_and_markdown_are_generated_from_selected_database_report(tmp_path):
    path = tmp_path / "export.sqlite3"
    fixture = upgraded_history(path)
    run_id = fixture["old_run"]["run_id"]
    unit_id = fixture["old_run"]["unit_ids"]["individual:1"]
    machine_path = tmp_path / "machine.json"
    human_path = tmp_path / "human.md"

    machine = invoke(
        path, "final-report", "export", run_id, "--unit", unit_id,
        "--format", "json", "--output", str(machine_path),
    )
    human = invoke(
        path, "final-report", "export", run_id,
        "--format", "markdown", "--output", str(human_path),
    )

    assert str(machine_path) in machine.stdout
    assert fixture["store_id"] in machine.stdout
    assert run_id in machine.stdout
    exported = json.loads(machine_path.read_text(encoding="utf-8"))
    assert len(exported["units"]) == 1
    assert exported["units"][0]["unit_id"] == unit_id
    assert exported["run"]["contract_version"] == OLD_CONTRACT
    markdown = human_path.read_text(encoding="utf-8")
    assert markdown.startswith("# Final review report\n")
    assert f"Store: {fixture['store_id']}" in markdown
    assert "Historical finding remains auditable" in markdown
    assert str(human_path) in human.stdout


def test_history_and_views_never_rewrite_historical_rows_during_upgrades(tmp_path):
    path = tmp_path / "immutable.sqlite3"
    fixture = upgraded_history(path)
    before_results = copy.deepcopy(fixture["history_before_views"])
    before_manifest = copy.deepcopy(fixture["old_manifest"])

    json_view(path, "contract", "show")
    json_view(path, "acceptance", "list")
    json_view(path, "acceptance", "history")
    json_view(path, "final-report", "list")
    json_view(path, "final-report", "show", fixture["old_run"]["run_id"])

    with FinalStore(path) as reopened:
        assert reopened.acceptance_history() == before_results
        assert reopened.manifest(fixture["old_run"]["run_id"]) == before_manifest
        old_row = reopened.connection.execute(
            "SELECT state, evidence_json, error, details_json, recorded_at "
            "FROM acceptance_results WHERE acceptance_run_id = ?",
            (fixture["old_acceptance"]["acceptance_run_id"],),
        ).fetchone()
        assert dict(old_row) == {
            "state": fixture["old_acceptance"]["result"]["state"],
            "evidence_json": json.dumps(
                fixture["old_acceptance"]["result"]["evidence"],
                sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            ),
            "error": "",
            "details_json": json.dumps(
                fixture["old_acceptance"]["result"]["details"],
                sort_keys=True, ensure_ascii=False, separators=(",", ":"),
            ),
            "recorded_at": fixture["old_acceptance"]["result"]["recorded_at"],
        }
        assert reopened.store_instance_id == fixture["store_id"]


@pytest.mark.parametrize("args, message", [
    (("contract", "show", "--version", "not-installed"), "contract version"),
    (("acceptance", "list", "--version", "not-installed"), "acceptance version"),
    (("acceptance", "status", "not-a-scenario"), "no acceptance attempts"),
    (("acceptance", "status", "--acceptance-run-id", "not-an-attempt"), "unknown acceptance run"),
    (("acceptance", "history", "not-a-scenario"), "no acceptance attempts"),
    (("final-report", "list"), "no final-review runs"),
    (("final-report", "show", "not-a-run"), "unknown run"),
    (("final-report", "export", "not-a-run"), "unknown run"),
])
def test_selection_and_nonexistent_ids_are_clear_nonempty_failures(tmp_path, args, message):
    path = tmp_path / "errors.sqlite3"
    if args[:2] in (("contract", "show"), ("acceptance", "list")) or \
            args[1:2] == ("status",) and "--acceptance-run-id" in args or \
            args[1:2] in (("show",), ("export",)):
        upgraded_history(path)
    else:
        with FinalStore(path):
            pass
    process = invoke(path, *args, expected=1)
    assert message in process.stderr
    assert not process.stdout


def test_unit_selection_must_belong_to_run_and_failed_export_writes_nothing(tmp_path):
    path = tmp_path / "bad-unit.sqlite3"
    fixture = upgraded_history(path)
    output = tmp_path / "must-not-exist.json"
    other_unit = fixture["new_run"]["unit_ids"]["combined"]

    process = invoke(
        path, "final-report", "export", fixture["old_run"]["run_id"],
        "--unit", other_unit, "--output", str(output), expected=1,
    )
    assert "unknown unit" in process.stderr
    assert fixture["old_run"]["run_id"] in process.stderr
    assert not output.exists()


def test_empty_history_and_empty_scoped_run_list_are_not_empty_successes(tmp_path):
    path = tmp_path / "empty-selection.sqlite3"
    fixture = upgraded_history(path)

    no_history = invoke(
        path, "acceptance", "history", SCENARIO_ID,
        "--review-run-id", "not-a-review-run", expected=1,
    )
    assert "no acceptance attempts" in no_history.stderr
    no_runs = invoke(
        path, "final-report", "list", "--stack", "not-a-stack", expected=1,
    )
    assert "no final-review runs" in no_runs.stderr
    assert "not-a-stack" in no_runs.stderr
    assert fixture["store_id"] not in no_runs.stdout


def test_store_option_uses_requested_database_not_process_default(tmp_path, monkeypatch):
    requested = tmp_path / "requested.sqlite3"
    fixture = upgraded_history(requested)
    default_root = tmp_path / "default-state"
    env = os.environ.copy()
    env["OCR_STATE_DIR"] = str(default_root)
    monkeypatch.setenv("OCR_STATE_DIR", str(default_root))

    view = json_view(requested, "contract", "show")
    direct = subprocess.run(
        [sys.executable, str(GATE), "contract", "show", "--version", OLD_CONTRACT,
         "--store", str(requested), "--json"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=30, check=False,
    )

    assert direct.returncode == 0, direct.stderr
    assert json.loads(direct.stdout)["store_instance_id"] == fixture["store_id"]
    assert view["store_instance_id"] == fixture["store_id"]
    assert not (default_root / "final-review.sqlite3").exists()


def test_legacy_null_and_old_contract_continuation_remain_exact_after_upgrade(tmp_path):
    path = tmp_path / "legacy-and-continuation.sqlite3"
    with FinalStore(path) as store:
        registry = acceptance.AcceptanceRegistry(store)
        legacy = store.start_run(
            review_stack(), acceptance_version=OLD_ACCEPTANCE,
            metadata={"fixture": "pre-acceptance-identity"},
        )
        store.connection.execute(
            "UPDATE runs SET acceptance_version = NULL WHERE run_id = ?", (legacy["run_id"],),
        )
        contract_document, acceptance_document = upgraded_documents()
        store.install_contract(contract_document)
        registry.install(acceptance_document)

        current = store.start_run(review_stack(source_sha="c" * 40, target_sha="d" * 40))
        continuation = store.start_run(
            review_stack(source_sha="e" * 40, target_sha="f" * 40),
            contract_version=OLD_CONTRACT, acceptance_version=OLD_ACCEPTANCE,
            metadata={"continues_run_id": legacy["run_id"]},
        )
        old_attempt = complete_scenario(
            store, version=OLD_ACCEPTANCE, review_run_id=continuation["run_id"],
        )
        new_attempt = complete_scenario(store, review_run_id=current["run_id"])
        legacy_before_views = copy.deepcopy(store.manifest(legacy["run_id"]))

    assert current["acceptance_version"] == NEW_ACCEPTANCE
    assert continuation["acceptance_version"] == OLD_ACCEPTANCE
    assert continuation["contract_version"] == OLD_CONTRACT
    assert old_attempt["version"] == continuation["acceptance_version"]
    assert new_attempt["version"] == current["acceptance_version"]

    legacy_report = json_view(path, "final-report", "show", legacy["run_id"])
    current_report = json_view(path, "final-report", "show", current["run_id"])
    continued_report = json_view(path, "final-report", "show", continuation["run_id"])
    assert legacy_report["run"]["acceptance_version"] is None
    assert current_report["run"]["acceptance_version"] == NEW_ACCEPTANCE
    assert continued_report["run"]["acceptance_version"] == OLD_ACCEPTANCE
    assert continued_report["run"]["contract_version"] == OLD_CONTRACT
    assert "Acceptance: not recorded" in invoke(
        path, "final-report", "show", legacy["run_id"],
    ).stdout

    listed = json_view(path, "final-report", "list")
    identities = {run["run_id"]: run["acceptance_version"] for run in listed["runs"]}
    assert identities == {
        legacy["run_id"]: None,
        current["run_id"]: NEW_ACCEPTANCE,
        continuation["run_id"]: OLD_ACCEPTANCE,
    }
    old_status = json_view(
        path, "acceptance", "status", "--acceptance-run-id",
        old_attempt["acceptance_run_id"],
    )
    new_status = json_view(
        path, "acceptance", "status", "--acceptance-run-id",
        new_attempt["acceptance_run_id"],
    )
    assert old_status["acceptance_run"]["review_run_id"] == continuation["run_id"]
    assert old_status["acceptance_version"] == OLD_ACCEPTANCE
    assert new_status["acceptance_run"]["review_run_id"] == current["run_id"]
    assert new_status["acceptance_version"] == NEW_ACCEPTANCE

    with FinalStore(path) as reopened:
        assert reopened.manifest(legacy["run_id"]) == legacy_before_views


def test_acceptance_and_final_report_scope_mismatches_do_not_cross_link(tmp_path):
    path = tmp_path / "scope-mismatches.sqlite3"
    fixture = upgraded_history(path)
    old_run_id = fixture["old_run"]["run_id"]
    new_run_id = fixture["new_run"]["run_id"]
    old_attempt_id = fixture["old_acceptance"]["acceptance_run_id"]
    new_unit_id = fixture["new_run"]["unit_ids"]["individual:1"]

    mismatches = [
        (("acceptance", "status", "--acceptance-run-id", old_attempt_id,
          "--version", NEW_ACCEPTANCE), "not at version"),
        (("acceptance", "status", "--acceptance-run-id", old_attempt_id,
          "--review-run-id", new_run_id), "does not belong to review run"),
        (("acceptance", "history", SCENARIO_ID, "--version", NEW_ACCEPTANCE,
          "--review-run-id", old_run_id), "no acceptance attempts"),
        (("acceptance", "status", SCENARIO_ID, "--version", OLD_ACCEPTANCE,
          "--review-run-id", new_run_id), "no acceptance attempts"),
        (("final-report", "show", old_run_id, "--unit", new_unit_id), "unknown unit"),
    ]
    for args, message in mismatches:
        process = invoke(path, *args, expected=1)
        assert message in process.stderr

    with FinalStore(path) as store:
        assert store.acceptance_history() == fixture["history_before_views"]
        assert store.manifest(old_run_id)["run"]["acceptance_version"] == OLD_ACCEPTANCE
        assert store.manifest(new_run_id)["run"]["acceptance_version"] == NEW_ACCEPTANCE
