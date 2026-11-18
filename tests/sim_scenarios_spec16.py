"""Executable SPEC-16 acceptance dispatches.

The fast path executes focused, existing pytest verticals against real
repositories/stores and recorded read-only provider doubles.  Every required
registry evidence item points at the resulting JUnit artifact; no handler
manufactures a green observation.  Selected behavior/dynamic scenarios can
instead run through :mod:`sim_harness` with the gate's selected real backend.
Inference/setup failures are returned as ``incomplete`` with their exact
observable reason, never downgraded to deterministic pass evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import sim_harness as sim  # noqa: E402
from rg_final_store import FinalStore  # noqa: E402


def _stack():
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
            "provider_data": {"iid": 123, "author": "deterministic-fixture"},
        }],
    }


def _nodes(*names):
    return list(names)


def _pytest_spec(expected_state, *shared, **evidence_nodes):
    """Associate each declared evidence key with the tests proving it."""
    return {
        "expected_state": expected_state,
        "evidence": {
            key: _nodes(*shared, *nodes) for key, nodes in evidence_nodes.items()
        },
    }


CLI = "tests/test_final_review_cli.py"
STACK = "tests/test_final_stack.py"
GITHUB_STACK = "tests/test_final_github_stack.py"
GRAPH = "tests/test_final_graph.py"
DYNAMIC = "tests/test_final_dynamic.py"
VERIFY = "tests/test_final_verification.py"
MEMORY = "tests/test_final_memory.py"
PUBLISH = "tests/test_final_publication.py"
STOPS = "tests/test_final_stops.py"
CONTEXT = "tests/test_final_context.py"
VIEWS = "tests/test_final_views.py"
REGISTRY = "tests/test_final_acceptance_registry.py"

PYTEST_SCENARIOS = {
    "standalone_provider_parity": _pytest_spec(
        "pass",
        gitlab_result=_nodes(f"{CLI}::test_standalone_reviews_full_immutable_tree_and_persists_complete_result"),
        github_result=_nodes(f"{GITHUB_STACK}::test_one_standalone_github_pr_runs_individual_combined_and_status_on_shared_harness"),
        shared_engine_identity=_nodes(
            f"{CLI}::test_standalone_reviews_full_immutable_tree_and_persists_complete_result",
            f"{GITHUB_STACK}::test_one_standalone_github_pr_runs_individual_combined_and_status_on_shared_harness",
        ),
    ),
    "linear_stack_individual_combined": _pytest_spec(
        "pass",
        individual_reports=_nodes(f"{STACK}::test_three_item_stack_runs_complete_units_links_reports_and_publishes_each_item"),
        combined_report=_nodes(f"{STACK}::test_three_item_stack_runs_complete_units_links_reports_and_publishes_each_item"),
        all_or_nothing_verdicts=_nodes(f"{STACK}::test_no_item_is_green_unless_all_three_individuals_and_combined_pass"),
    ),
    "omitted_graph_items_corrected_command": _pytest_spec(
        "error",
        omitted_ancestor_error=_nodes(f"{GRAPH}::test_omitted_ancestors_abort_with_complete_base_most_corrected_command"),
        omitted_descendant_error=_nodes(f"{GRAPH}::test_omitted_descendants_abort_with_complete_base_most_corrected_command"),
        corrected_commands=_nodes(
            f"{GRAPH}::test_omitted_ancestors_abort_with_complete_base_most_corrected_command",
            f"{GRAPH}::test_omitted_descendants_abort_with_complete_base_most_corrected_command",
        ),
    ),
    "intermediate_defect_survives_composition": _pytest_spec(
        "findings",
        individual_finding=_nodes(f"{STACK}::test_no_item_is_green_unless_all_three_individuals_and_combined_pass[individual:2]"),
        combined_behavior=_nodes(f"{STACK}::test_no_item_is_green_unless_all_three_individuals_and_combined_pass[individual:2]"),
        independent_verdicts=_nodes(
            f"{STACK}::test_no_item_is_green_unless_all_three_individuals_and_combined_pass[individual:2]",
            "tests/test_final_composition.py::test_combined_blocking_finding_uses_compute_verdict_but_is_not_official",
        ),
    ),
    "unchanged_domain_contract": _pytest_spec(
        "findings",
        unchanged_document=_nodes(f"{CLI}::test_unchanged_domain_contract_reaches_documentation_lens"),
        domain_finding=_nodes(f"{CLI}::test_unchanged_domain_contract_reaches_documentation_lens"),
        complete_tree_evidence=_nodes(
            f"{CLI}::test_unchanged_domain_contract_reaches_documentation_lens",
            f"{CONTEXT}::test_collects_unchanged_repository_knowledge_without_serializing_sources",
        ),
    ),
    "final_only_comment_testing_standards": _pytest_spec(
        "findings",
        incremental_scope_exclusion=_nodes(f"{CLI}::test_final_comment_and_testing_standards_are_both_visible"),
        comment_finding=_nodes(f"{CLI}::test_final_comment_and_testing_standards_are_both_visible"),
        testing_standard_finding=_nodes(f"{CLI}::test_final_comment_and_testing_standards_are_both_visible"),
    ),
    "foreign_fork_isolation": _pytest_spec(
        "pass",
        provider_fork_revision=_nodes(f"{CLI}::test_numeric_reference_and_arbitrary_fork_use_provider_repository_identity"),
        author_identity=_nodes(
            f"{CLI}::test_numeric_reference_and_arbitrary_fork_use_provider_repository_identity",
            "tests/test_final_provider_github.py::test_read_maps_arbitrary_author_head_fork_full_body_and_complete_revision_identity",
        ),
        local_branch_independence=_nodes("tests/test_final_materialize.py::test_materializes_target_from_a_separate_repository_and_cleans_failures"),
        isolation_evidence=_nodes(
            f"{CLI}::test_standalone_reviews_full_immutable_tree_and_persists_complete_result",
            "tests/test_final_materialize.py::test_materializes_target_from_a_separate_repository_and_cleans_failures",
        ),
    ),
    "existing_regression_output": _pytest_spec(
        "findings",
        required_suite_identity=_nodes(f"{DYNAMIC}::test_public_seam_reproduces_existing_failure_in_its_own_clean_clone"),
        verbatim_failure_output=_nodes(f"{DYNAMIC}::test_public_seam_reproduces_existing_failure_in_its_own_clean_clone"),
        blocking_verdict=_nodes(f"{DYNAMIC}::test_public_seam_reproduces_existing_failure_in_its_own_clean_clone"),
    ),
    "ephemeral_probe_attribution_cleanup": _pytest_spec(
        "findings",
        reproduced_failure=_nodes(f"{DYNAMIC}::test_ephemeral_failure_is_clean_credential_free_and_visibly_uncovered"),
        evidence_source_attribution=_nodes(f"{DYNAMIC}::test_ephemeral_failure_is_clean_credential_free_and_visibly_uncovered"),
        permanent_coverage_warning=_nodes(f"{DYNAMIC}::test_ephemeral_failure_is_clean_credential_free_and_visibly_uncovered"),
        probe_absence=_nodes(f"{DYNAMIC}::test_ephemeral_failure_is_clean_credential_free_and_visibly_uncovered"),
    ),
    "dynamic_degradation_warnings": _pytest_spec(
        "pass",
        fail_once_warning=_nodes(f"{DYNAMIC}::test_fail_once_pass_once_is_a_visible_flake_warning"),
        probe_build_warning=_nodes(f"{DYNAMIC}::test_unbuildable_probe_is_a_warning_not_fabricated_verification"),
        no_coverage_warning=_nodes(f"{DYNAMIC}::test_coverage_and_selector_gaps_are_warnings_with_one_attempt"),
        timeout_warning=_nodes(f"{DYNAMIC}::test_outer_executor_timeout_warns_once_and_cleans_clone"),
        verification_not_claimed=_nodes(
            f"{DYNAMIC}::test_fail_once_pass_once_is_a_visible_flake_warning",
            f"{DYNAMIC}::test_unbuildable_probe_is_a_warning_not_fabricated_verification",
            f"{DYNAMIC}::test_coverage_and_selector_gaps_are_warnings_with_one_attempt",
            f"{DYNAMIC}::test_outer_executor_timeout_warns_once_and_cleans_clone",
        ),
    ),
    "required_suite_outcomes": _pytest_spec(
        "incomplete",
        reproduced_failure_state=_nodes(f"{VERIFY}::test_reproduced_required_failure_reruns_once_and_blocks_with_evidence"),
        unexecutable_suite_state=_nodes(f"{VERIFY}::test_unexecutable_declared_suite_is_incomplete_and_not_rerun"),
        suite_execution_evidence=_nodes(
            f"{VERIFY}::test_reproduced_required_failure_reruns_once_and_blocks_with_evidence",
            f"{VERIFY}::test_unexecutable_declared_suite_is_incomplete_and_not_rerun",
        ),
    ),
    "revision_race_stale": _pytest_spec(
        "stale",
        source_movement=_nodes(f"{CLI}::test_revision_movement_is_stale_and_preserves_worker_evidence"),
        retarget_movement=_nodes(f"{MEMORY}::test_every_source_target_retarget_and_base_coordinate_is_freshness_sensitive"),
        base_movement=_nodes(f"{STACK}::test_moved_root_requires_restart_and_prevents_every_official_green"),
        stale_results=_nodes(
            f"{CLI}::test_revision_movement_is_stale_and_preserves_worker_evidence",
            f"{MEMORY}::test_every_source_target_retarget_and_base_coordinate_is_freshness_sensitive",
        ),
        pass_publication_blocked=_nodes(f"{STACK}::test_moved_root_requires_restart_and_prevents_every_official_green"),
    ),
    "invalid_composition_and_graphs": _pytest_spec(
        "incomplete",
        composition_conflict=_nodes("tests/test_final_composition.py::test_composition_conflict_persists_skipped_scope_and_never_emits_official_pass"),
        branching_graph=_nodes(f"{GRAPH}::test_branch_points_are_rejected_even_when_one_branch_was_not_supplied"),
        prose_only_dependency=_nodes(f"{GRAPH}::test_prose_only_dependency_does_not_create_a_graph_edge"),
        cycle=_nodes(f"{GRAPH}::test_provider_branch_cycle_is_rejected"),
        duplicate=_nodes(f"{GRAPH}::test_duplicate_explicit_references_are_rejected"),
        mixed_target=_nodes(
            f"{GRAPH}::test_multiple_target_projects_are_rejected_before_open_discovery",
            f"{GRAPH}::test_disconnected_independent_root_targets_are_rejected",
        ),
        inconsistent_order=_nodes(f"{GRAPH}::test_inconsistent_explicit_order_is_never_silently_reordered"),
        no_invented_order=_nodes(
            f"{GRAPH}::test_gap_between_supplied_items_cannot_be_filled_by_inference",
            f"{GRAPH}::test_inconsistent_explicit_order_is_never_silently_reordered",
        ),
    ),
    "publication_permission_degradation": _pytest_spec(
        "pass",
        publication_error=_nodes(f"{PUBLISH}::test_permission_and_transport_failures_preserve_local_unenforced_evidence"),
        local_only_result=_nodes(f"{PUBLISH}::test_permission_and_transport_failures_preserve_local_unenforced_evidence"),
        unenforced_result=_nodes(f"{PUBLISH}::test_permission_and_transport_failures_preserve_local_unenforced_evidence"),
        official_green_absent=_nodes(f"{GITHUB_STACK}::test_github_permission_loss_leaves_stack_pass_local_and_unenforced"),
    ),
    "local_waiver_isolation": _pytest_spec(
        "findings",
        visible_local_waiver=_nodes(f"{MEMORY}::test_local_prepush_waivers_are_visible_but_never_official_suppression"),
        official_finding=_nodes(f"{MEMORY}::test_local_prepush_waivers_are_visible_but_never_official_suppression"),
        official_verdict_not_green=_nodes(f"{PUBLISH}::test_local_prepush_waiver_is_context_only_at_publication_seam"),
    ),
    "finding_memory_lifecycle": _pytest_spec(
        "findings",
        quiet_rerun_blocking=_nodes(f"{MEMORY}::test_quiet_rerun_never_clears_an_unresolved_prior_finding"),
        changed_evidence=_nodes(f"{MEMORY}::test_positive_resolution_requires_changed_evidence_and_revalidation"),
        positive_revalidation=_nodes(f"{MEMORY}::test_positive_resolution_requires_changed_evidence_and_revalidation"),
        resolution_event=_nodes(f"{MEMORY}::test_positive_resolution_requires_changed_evidence_and_revalidation"),
        falsification_event=_nodes(f"{MEMORY}::test_falsification_is_explicit_rationaled_and_does_not_need_a_code_change"),
    ),
    "safe_completion_and_scoped_stop": _pytest_spec(
        "incomplete",
        ordinary_finding_safe_lenses=_nodes(f"{STOPS}::test_execute_final_review_completes_safe_lenses_and_tests_after_blocking_finding"),
        critical_stop_scope=_nodes(f"{STOPS}::test_stack_supervisor_scopes_stop_to_dependents_and_preserves_safe_individuals"),
        independent_work_completed=_nodes(f"{STOPS}::test_stack_supervisor_scopes_stop_to_dependents_and_preserves_safe_individuals"),
        skipped_mandatory_stages=_nodes(f"{STOPS}::test_valid_unsafe_sandbox_stop_records_original_finding_and_every_skip"),
        skipped_states=_nodes(f"{STOPS}::test_valid_unsafe_sandbox_stop_records_original_finding_and_every_skip"),
    ),
    "untrusted_execution_cleanup": _pytest_spec(
        "pass",
        source_snapshot_unchanged=_nodes(f"{DYNAMIC}::test_ephemeral_failure_is_clean_credential_free_and_visibly_uncovered"),
        worker_environment_scrubbed=_nodes(f"{CLI}::test_standalone_reviews_full_immutable_tree_and_persists_complete_result"),
        probes_unpublished=_nodes(f"{DYNAMIC}::test_ephemeral_failure_is_clean_credential_free_and_visibly_uncovered"),
        scratch_registry_empty=_nodes(
            f"{CLI}::test_standalone_reviews_full_immutable_tree_and_persists_complete_result",
            f"{VERIFY}::test_timeout_is_incomplete_and_disposable_clone_is_still_removed",
        ),
    ),
    "versioned_history_and_views": _pytest_spec(
        "pass",
        original_contract_version=_nodes("tests/test_final_store.py::test_contract_upgrade_never_rewrites_historical_run_or_rules"),
        original_acceptance_version=_nodes(f"{REGISTRY}::test_acceptance_upgrade_retains_original_attempt_and_definitions"),
        current_view=_nodes(f"{VIEWS}::test_history_and_views_never_rewrite_historical_rows_during_upgrades"),
        historical_view=_nodes(f"{VIEWS}::test_history_and_views_never_rewrite_historical_rows_during_upgrades"),
        retained_result=_nodes(f"{REGISTRY}::test_acceptance_upgrade_retains_original_attempt_and_definitions"),
    ),
}


def _artifact_root(call, leaf="evidence"):
    context = call.get("context") or {}
    configured = context.get("artifact_root") or context.get("test_tmp_path")
    base = Path(configured) if configured else Path(tempfile.mkdtemp(prefix="spec16-acceptance-"))
    root = base / f"{call['scenario']['scenario_id']}-{call['acceptance_run_id']}" / leaf
    root.mkdir(parents=True, exist_ok=False)
    return root


def _provider_free_pytest_environment(root):
    env = dict(os.environ)
    for key in list(env):
        upper = key.upper()
        if upper.startswith("STUB_") or any(part in upper for part in (
            "GITLAB", "GITHUB", "GLAB_", "GH_", "RG_FINAL_", "OCR_TEST_SELECTOR",
        )):
            env.pop(key, None)
    for key in ("SSH_AUTH_SOCK", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_CONFIG_COUNT"):
        env.pop(key, None)
    env.update({
        "OCR_STATE_DIR": str(root / "external-state"),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    })
    return env


def _junit_summary(path):
    tree = ET.parse(path)
    cases = tree.findall(".//testcase")
    failures = [case for case in cases if case.find("failure") is not None or case.find("error") is not None]
    skipped = [case for case in cases if case.find("skipped") is not None]
    return {
        "tests": len(cases),
        "failures": len(failures),
        "skips": len(skipped),
        "skipped_ids": [f"{case.get('classname')}::{case.get('name')}" for case in skipped],
        "executed_ids": [f"{case.get('classname')}::{case.get('name')}" for case in cases],
    }


def _pytest_evidence_items(spec, artifact, summary):
    evidence = {}
    for key, nodes in spec["evidence"].items():
        digest = hashlib.sha256(
            (artifact["sha256"] + "\0" + key + "\0" + "\0".join(nodes)).encode("utf-8")
        ).hexdigest()
        evidence[key] = {
            "evidence_id": "sha256:" + digest,
            "evidence_type": "pytest",
            "status": "passed" if not summary["failures"] and not summary["skips"] else "incomplete",
            "artifact": artifact["artifact"],
            "junit_sha256": artifact["sha256"],
            "tests": nodes,
        }
    return evidence


def _pytest_acceptance(call):
    spec = PYTEST_SCENARIOS[call["scenario"]["dispatch_key"]]
    root = _artifact_root(call)
    junit = root / "pytest-results.xml"
    log = root / "pytest-output.log"
    nodes = list(dict.fromkeys(
        node for selected in spec["evidence"].values() for node in selected
    ))
    command = [
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "--junitxml", str(junit), *nodes,
    ]
    before = sim.repo_snapshot(ROOT)
    try:
        process = subprocess.run(
            command, cwd=str(ROOT), env=_provider_free_pytest_environment(root),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=float((call.get("context") or {}).get("pytest_timeout", 300)),
            check=False,
        )
        output = (process.stdout or "") + (process.stderr or "")
        returncode = process.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        output = f"{type(exc).__name__}: {exc}"
        returncode = None
    log.write_text(output, encoding="utf-8")
    if returncode is None:
        return {
            "state": "incomplete", "error": f"pytest evidence unavailable: {output}",
            "evidence": {"dispatch": {"artifact": str(log), "status": "unavailable"}},
            "details": {"mode": "deterministic", "provider_writes": False, "executed": False},
        }
    if not junit.is_file():
        return {
            "state": "error", "error": "pytest produced no JUnit evidence artifact",
            "evidence": {"dispatch": {"artifact": str(log), "status": "missing-junit"}},
            "details": {"mode": "deterministic", "provider_writes": False, "executed": True},
        }
    summary = _junit_summary(junit)
    artifact = sim.persist_simulation_artifact(root, "pytest-execution.json", {
        "evidence_type": "pytest", "command": command[2:], "returncode": returncode,
        "junit_artifact": str(junit), "summary": summary,
        "source_repository_unchanged": before == sim.repo_snapshot(ROOT),
        "provider_writes": False,
    })
    # Use the JUnit bytes, not the summary document, as the immutable evidence ID.
    junit_digest = hashlib.sha256(junit.read_bytes()).hexdigest()
    artifact.update({
        "artifact": str(junit), "sha256": junit_digest,
        "summary_artifact": artifact["artifact"],
    })
    evidence = _pytest_evidence_items(spec, artifact, summary)
    details = {
        "mode": "deterministic", "provider_writes": False, "executed": True,
        "pytest": {"returncode": returncode, **summary},
        "source_repository_unchanged": before == sim.repo_snapshot(ROOT),
        "summary_artifact": artifact["summary_artifact"],
    }
    if before != sim.repo_snapshot(ROOT):
        return {"state": "error", "error": "acceptance pytest run mutated the source repository",
                "evidence": evidence, "details": details}
    if returncode != 0 or summary["failures"] or not summary["tests"]:
        return {
            "state": "error",
            "error": f"acceptance pytest evidence failed (exit {returncode}): {output[-4000:]}",
            "evidence": evidence, "details": details,
        }
    if summary["skips"]:
        return {
            "state": "incomplete",
            "error": "acceptance pytest evidence skipped: " + ", ".join(summary["skipped_ids"]),
            "evidence": evidence, "details": details,
        }
    state = spec["expected_state"]
    error = ""
    if state == "error":
        error = "Declared validation errors were reproduced and verified; see the pytest evidence."
    elif state == "incomplete":
        error = "Declared incomplete outcomes were reproduced and verified; no pass is claimed."
    return {"state": state, "error": error, "evidence": evidence, "details": details}


def _real_model_requested(call):
    context = call.get("context") or {}
    return context.get("real_model") is True or context.get("mode") in {
        "real-model", "real-model-simulation",
    }


def _incomplete_real_model(call, reason, *, artifact=None):
    return {
        "state": "incomplete", "error": reason,
        "evidence": {"real_model_simulation": artifact or {"status": "not_executed"}},
        "details": {
            "mode": "real-model-simulation", "provider_writes": False,
            "executed": bool(artifact), "incomplete_reason": reason,
        },
    }


def _static_units(report):
    result = report.get("result") or {}
    return result.get("units") or []


def _scrub_git_trace(monkeypatch):
    for key in list(os.environ):
        if key.startswith("GIT_TRACE"):
            monkeypatch.delenv(key, raising=False)


def _real_model_static(call):
    key = call["scenario"]["dispatch_key"]
    domain = key == "unchanged_domain_contract"
    root = _artifact_root(call, "real-model")
    monkeypatch = pytest.MonkeyPatch()
    _scrub_git_trace(monkeypatch)
    try:
        import test_final_review_cli as fixture_module

        fixture = fixture_module.make_standalone_fixture(
            root / "fixture", monkeypatch, domain_contract=domain, standards=not domain,
        )
        try:
            report = sim.run_final_review_simulation(
                fixture.ref, repo_root=fixture.repo, artifact_dir=root / "execution",
                environ=os.environ.copy(),
                timeout=float((call.get("context") or {}).get("real_model_timeout", 7200)),
            )
        finally:
            monkeypatch.undo()
    except sim.SimError as exc:
        monkeypatch.undo()
        return _incomplete_real_model(call, str(exc))
    except (OSError, subprocess.SubprocessError) as exc:
        monkeypatch.undo()
        return _incomplete_real_model(
            call, f"real-model fixture/setup unavailable: {type(exc).__name__}: {exc}",
        )

    artifact = report.get("artifact")
    result = report.get("result") or {}
    if report.get("error") and not result:
        return _incomplete_real_model(call, report["error"], artifact=artifact)
    if result.get("state") == "incomplete":
        warnings = [
            warning for unit in _static_units(report) for warning in unit.get("warnings") or []
            if warning.get("code") in {"required-lens-failed", "freshness-check", "materialization"}
        ]
        reason = warnings[0].get("message") if warnings else (report.get("stderr") or "")[-2000:]
        return _incomplete_real_model(
            call, "real-model inference/stage incomplete: " + (reason or "unknown incomplete state"),
            artifact=artifact,
        )
    if report.get("error") or not report.get("source_unchanged") or not report["cleanup"]["clean"]:
        return {
            "state": "error", "error": report.get("error") or "real-model cleanup verification failed",
            "evidence": {"real_model_simulation": artifact}, "details": {
                "mode": "real-model-simulation", "provider_writes": False, "executed": True,
            },
        }
    units = _static_units(report)
    individual = next((unit for unit in units if unit.get("unit_key") == "individual:1"), {})
    findings = individual.get("findings") or []
    if domain:
        selected = [finding for finding in findings if finding.get("lens") == "documentation/domain"]
        unchanged = sim._git(["show", f"{fixture.head}:docs/ADR.md"], fixture.repo).stdout
        base_document = sim._git(["show", f"{fixture.base}:docs/ADR.md"], fixture.repo).stdout
        changed = sim._git([
            "diff", "--name-only", fixture.base, fixture.head, "--", "docs/ADR.md",
        ], fixture.repo).stdout.strip()
        coverage = {row["kind"]: row for row in individual.get("coverage") or []}
        evidence = {
            "unchanged_document": {
                "path": "docs/ADR.md", "unchanged_in_diff": not changed,
                "base_and_head_identical": unchanged == base_document, "document": unchanged,
            },
            "domain_finding": {"findings": selected},
            "complete_tree_evidence": {
                "run_id": result.get("run_id"),
                "repository": coverage.get("repository"), "diff": coverage.get("diff"),
                "mandatory_lenses": coverage.get("mandatory_lenses"),
            },
        }
        missing = not selected
    else:
        comments = [finding for finding in findings if finding.get("lens") == "repository-standards"]
        testing = [finding for finding in findings if finding.get("lens") == "testing"]
        evidence = {
            "incremental_scope_exclusion": {
                "review_mode": "complete-repository", "full_tree_run_id": result.get("run_id"),
                "lenses": [lens.get("name") for lens in individual.get("lenses") or []],
            },
            "comment_finding": {"findings": comments},
            "testing_standard_finding": {"findings": testing},
        }
        missing = not comments or not testing
    for item in evidence.values():
        item.update({"evidence_type": "real-model-simulation", "simulation_artifact": artifact})
    details = {
        "mode": "real-model-simulation", "provider_writes": False, "executed": True,
        "backend": report["backend"], "simulation_artifact": artifact,
        "source_repository_unchanged": report["source_unchanged"], "cleanup": report["cleanup"],
    }
    if missing or result.get("state") != "findings":
        return {
            "state": "error",
            "error": "real-model review completed without the scenario's required finding(s)",
            "evidence": evidence, "details": details,
        }
    return {"state": "findings", "evidence": evidence, "details": details}


def _real_dynamic_unavailable_reason(result):
    infrastructure = {
        "dynamic-selector", "dynamic-executor", "dynamic-executor-exit",
        "dynamic-executor-result", "dynamic-timeout", "dynamic-materialization",
    }
    for warning in result.get("warnings") or []:
        if warning.get("code") in infrastructure:
            detail = warning.get("details") or {}
            return warning.get("message", "dynamic inference unavailable") + (
                ": " + str(detail.get("output")) if detail.get("output") else ""
            )
    return ""


def _real_model_dynamic(call):
    with pytest.MonkeyPatch.context() as monkeypatch:
        _scrub_git_trace(monkeypatch)
        return _real_model_dynamic_scrubbed(call)


def _real_model_dynamic_scrubbed(call):
    key = call["scenario"]["dispatch_key"]
    probe = key == "ephemeral_probe_attribution_cleanup"
    root = _artifact_root(call, "real-model")
    try:
        import test_final_dynamic as fixture_module

        repo, target, head = fixture_module._repository(root / "fixture", broken=not probe)
        if probe:
            # Rebase the behavior-only change onto a target without permanent
            # coverage, so the real selector must author an ephemeral probe.
            fixture_module._git(repo, "checkout", "-q", "main")
            fixture_module._git(repo, "rm", "-q", "tests/test_dsl.py")
            target = fixture_module._commit(repo, "remove permanent coverage for probe acceptance")
            fixture_module._git(repo, "checkout", "-q", "feature")
            fixture_module._git(repo, "rebase", "-q", "main")
            (repo / "dsl.py").write_text(
                "import threading\n\ndef verdict():\n"
                "    # Deliberate unsynchronized read/modify/write race.\n"
                "    global counter\n    counter = 0\n"
                "    def worker():\n        global counter\n"
                "        for _ in range(2000):\n            counter += 1\n"
                "    threads = [threading.Thread(target=worker) for _ in range(8)]\n"
                "    for thread in threads: thread.start()\n"
                "    for thread in threads: thread.join()\n"
                "    return 'race' if counter != 16000 else 'ok'\n",
                encoding="utf-8",
            )
            head = fixture_module._commit(repo, "introduce uncovered regression probe subject")
        report = sim.run_dynamic_simulation(
            repo, target, head, artifact_dir=root / "execution", environ=os.environ.copy(),
            timeout=float((call.get("context") or {}).get("real_model_timeout", 600)),
        )
    except sim.SimError as exc:
        return _incomplete_real_model(call, str(exc))
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return _incomplete_real_model(
            call, f"real-model dynamic fixture/setup unavailable: {type(exc).__name__}: {exc}",
        )
    artifact = report.get("artifact")
    result = report.get("result") or {}
    unavailable = _real_dynamic_unavailable_reason(result)
    if unavailable:
        return _incomplete_real_model(
            call, "real-model dynamic inference/execution incomplete: " + unavailable,
            artifact=artifact,
        )
    if report.get("error") or not report["cleanup"]["clean"]:
        return {
            "state": "error", "error": report.get("error") or "dynamic cleanup verification failed",
            "evidence": {"real_model_simulation": artifact}, "details": {
                "mode": "real-model-simulation", "provider_writes": False, "executed": True,
            },
        }
    findings = result.get("findings") or []
    source = "ephemeral_test" if probe else "existing_test"
    selected = [finding for finding in findings if finding.get("evidence_source") == source]
    warnings = result.get("warnings") or []
    coverage_warning = next((warning for warning in warnings if warning.get("code") == "dynamic-coverage"), None)
    if probe:
        evidence = {
            "reproduced_failure": {"findings": selected},
            "evidence_source_attribution": {"required": source, "observed": [finding.get("evidence_source") for finding in findings]},
            "permanent_coverage_warning": coverage_warning or {},
            "probe_absence": {
                "source_probe_root_absent": not report["cleanup"]["source_probe_root_remaining"],
                "scratch_cleanup": report["cleanup"],
            },
        }
    else:
        evidence = {
            "required_suite_identity": {"suite": "tests/test_dsl.py", "kind": "existing_test"},
            "verbatim_failure_output": {"findings": selected},
            "blocking_verdict": {"state": result.get("state"), "finding_count": len(selected)},
        }
    for item in evidence.values():
        item.update({"evidence_type": "real-model-simulation", "simulation_artifact": artifact})
    details = {
        "mode": "real-model-simulation", "provider_writes": False, "executed": True,
        "backend": report["backend"], "simulation_artifact": artifact,
        "source_repository_unchanged": report["source_unchanged"], "cleanup": report["cleanup"],
    }
    if not selected or result.get("state") != "findings" or (probe and not coverage_warning):
        return {
            "state": "error",
            "error": f"real-model dynamic selector/executor did not reproduce the required {source} evidence",
            "evidence": evidence, "details": details,
        }
    return {"state": "findings", "evidence": evidence, "details": details}


def _label_deterministic(evidence):
    return {
        key: {"evidence_type": "deterministic-acceptance", "observation": observation}
        for key, observation in evidence.items()
    }


def same_store_supersession(_context):
    """Prove the existing store guard refuses an older publication attempt."""
    with tempfile.TemporaryDirectory(prefix="spec16-same-store-") as root:
        with FinalStore(Path(root) / "review.sqlite3") as store:
            first = store.start_run(_stack(), acceptance_version="spec-16/v1")
            second = store.start_run(_stack(), acceptance_version="spec-16/v1")
            assert store.is_superseded(first["run_id"]) is True
            assert store.is_superseded(second["run_id"]) is False
            with store.publication_guard(first["run_id"]) as attempt:
                assert attempt.current is False
                attempt.record(
                    "superseded", "review-gate/final-review",
                    details={"deterministic": True, "provider_write": False},
                )
            history = store.run_history(first["stack_key"])
            assert [item["run"]["run_id"] for item in history] == [first["run_id"], second["run_id"]]
            retained = store.manifest(first["run_id"])
            assert retained["run"]["superseded_by_run_id"] == second["run_id"]
            assert retained["publications"][0]["status"] == "superseded"
            evidence = {
                "superseded_run": {
                    "older_run_id": first["run_id"], "newer_run_id": second["run_id"],
                    "superseded_by": retained["run"]["superseded_by_run_id"],
                },
                "blocked_publication": retained["publications"][0],
                "same_store_history": [
                    {"run_id": item["run"]["run_id"], "run_seq": item["run"]["run_seq"]}
                    for item in history
                ],
            }
            return {
                "state": "pass", "evidence": _label_deterministic(evidence),
                "details": {"mode": "deterministic", "provider_writes": False, "executed": True},
            }


def fresh_store_independence(_context):
    """Prove a second database has no identity, finding, or ordering continuity."""
    with tempfile.TemporaryDirectory(prefix="spec16-fresh-stores-") as root:
        root_path = Path(root)
        with FinalStore(root_path / "original.sqlite3") as original, \
                FinalStore(root_path / "fresh.sqlite3") as fresh:
            established = original.start_run(_stack(), acceptance_version="spec-16/v1")
            original.record_finding(
                established["unit_ids"]["individual:1"], "retained-local-finding",
                "Only the original store owns this finding", "high", "high",
            )
            independent = fresh.start_run(
                _stack(), acceptance_version="spec-16/v1", continuity_required=True,
            )
            context = fresh.rerun_context(independent["run_id"])
            fresh_history = fresh.run_history(independent["stack_key"])
            assert original.store_instance_id != fresh.store_instance_id
            assert independent["trust"]["independent"] is True
            assert independent["trust"]["continuity_available"] is False
            assert independent["trust"]["enforced"] is False
            assert independent["trust"]["local_only"] is True
            assert context["findings"] == []
            assert len(fresh_history) == 1
            assert fresh.is_superseded(independent["run_id"]) is False
            evidence = {
                "independent_identity": {
                    "original_store": original.store_instance_id, "fresh_store": fresh.store_instance_id,
                    "original_run": established["run_id"], "fresh_run": independent["run_id"],
                },
                "unenforced_trust": independent["trust"],
                "isolated_history": {
                    "fresh_run_count": len(fresh_history), "carried_findings": context["findings"],
                    "official_supersession": False,
                },
            }
            return {
                "state": "pass", "evidence": _label_deterministic(evidence),
                "details": {"mode": "deterministic", "provider_writes": False, "executed": True},
            }


def _real_model_dispatch(call):
    key = call["scenario"]["dispatch_key"]
    if key in {"unchanged_domain_contract", "final_only_comment_testing_standards"}:
        return _real_model_static(call)
    if key in {"existing_regression_output", "ephemeral_probe_attribution_cleanup"}:
        return _real_model_dynamic(call)
    return _pytest_acceptance(call)


def _dispatch(call):
    if _real_model_requested(call):
        return _real_model_dispatch(call)
    return _pytest_acceptance(call)


REAL_MODEL_DISPATCH = frozenset({
    "unchanged_domain_contract", "final_only_comment_testing_standards",
    "existing_regression_output", "ephemeral_probe_attribution_cleanup",
})

DISPATCH = {
    "standalone_provider_parity": _dispatch,
    "linear_stack_individual_combined": _dispatch,
    "omitted_graph_items_corrected_command": _dispatch,
    "intermediate_defect_survives_composition": _dispatch,
    "unchanged_domain_contract": _dispatch,
    "final_only_comment_testing_standards": _dispatch,
    "foreign_fork_isolation": _dispatch,
    "existing_regression_output": _dispatch,
    "ephemeral_probe_attribution_cleanup": _dispatch,
    "dynamic_degradation_warnings": _dispatch,
    "required_suite_outcomes": _dispatch,
    "revision_race_stale": _dispatch,
    "invalid_composition_and_graphs": _dispatch,
    "publication_permission_degradation": _dispatch,
    "local_waiver_isolation": _dispatch,
    "finding_memory_lifecycle": _dispatch,
    "safe_completion_and_scoped_stop": _dispatch,
    "untrusted_execution_cleanup": _dispatch,
    "same_store_supersession": same_store_supersession,
    "fresh_store_independence": fresh_store_independence,
    "versioned_history_and_views": _dispatch,
}
STUBBED_DISPATCH = frozenset(key for key, handler in DISPATCH.items() if handler is None)


def dispatch_handlers():
    return dict(DISPATCH)


def run_scenario(registry, scenario_id, *, version=None, context=None, review_run_id=None):
    return registry.dispatch(
        scenario_id, version=version, context=context, review_run_id=review_run_id,
        handlers=dispatch_handlers(),
    )


def main(argv=None):
    """Execute registered scenarios and retain all artifacts under --artifact-root."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True, help="acceptance SQLite database")
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--scenario", action="append", default=[])
    parser.add_argument("--mode", choices=("deterministic", "real-model"), default="deterministic")
    parser.add_argument("--timeout", type=float, default=900, help="per scenario inference deadline (seconds)")
    args = parser.parse_args(argv)
    with FinalStore(args.store) as store:
        registry = __import__("rg_final_acceptance").AcceptanceRegistry(store, handlers=DISPATCH)
        selected = args.scenario or [item["scenario_id"] for item in registry.list()]
        results = [run_scenario(
            registry, scenario_id,
            context={
                "mode": args.mode, "artifact_root": args.artifact_root,
                "real_model_timeout": args.timeout, "pytest_timeout": max(300, args.timeout),
            },
        ) for scenario_id in selected]
    print(json.dumps([{
        "scenario_id": result["scenario_id"],
        "state": result["state"], "expected_state": result["scenario"]["expected_state"],
        "error": result["error"], "acceptance_run_id": result["acceptance_run_id"],
    } for result in results], indent=2))
    return 0 if all(result["state"] == result["scenario"]["expected_state"] for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
