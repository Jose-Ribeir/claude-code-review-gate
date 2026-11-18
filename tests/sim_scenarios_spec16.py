"""SPEC-16 acceptance dispatch records.

C16 supplies two small, real deterministic checks through temporary stores.
The remaining records are deliberate C18 hooks: ``None`` means no implementation
has run, and :class:`rg_final_acceptance.AcceptanceRegistry` records that as
``incomplete``. No record imports a provider client or performs a provider write.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

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
            assert [item["run"]["run_id"] for item in history] == [
                first["run_id"], second["run_id"],
            ]
            retained = store.manifest(first["run_id"])
            assert retained["run"]["superseded_by_run_id"] == second["run_id"]
            assert retained["publications"][0]["status"] == "superseded"
            return {
                "state": "pass",
                "evidence": {
                    "superseded_run": {
                        "older_run_id": first["run_id"],
                        "newer_run_id": second["run_id"],
                        "superseded_by": retained["run"]["superseded_by_run_id"],
                    },
                    "blocked_publication": retained["publications"][0],
                    "same_store_history": [
                        {"run_id": item["run"]["run_id"], "run_seq": item["run"]["run_seq"]}
                        for item in history
                    ],
                },
                "details": {"mode": "deterministic", "provider_writes": False},
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
            assert context["store_instance_id"] == fresh.store_instance_id
            assert context["findings"] == []
            assert len(fresh_history) == 1
            assert fresh_history[0]["run"]["run_id"] != established["run_id"]
            assert fresh_history[0]["run"]["run_seq"] == 1
            assert fresh.is_superseded(independent["run_id"]) is False

            return {
                "state": "pass",
                "evidence": {
                    "independent_identity": {
                        "original_store": original.store_instance_id,
                        "fresh_store": fresh.store_instance_id,
                        "original_run": established["run_id"],
                        "fresh_run": independent["run_id"],
                    },
                    "unenforced_trust": independent["trust"],
                    "isolated_history": {
                        "fresh_run_count": len(fresh_history),
                        "carried_findings": context["findings"],
                        "official_supersession": False,
                    },
                },
                "details": {"mode": "deterministic", "provider_writes": False},
            }


# Every key is durable registry data. A None value is an explicit unexecuted
# hook, not a successful default; C18 replaces these with real/deterministic
# harness functions while retaining the same keys.
DISPATCH = {
    "standalone_provider_parity": None,
    "linear_stack_individual_combined": None,
    "omitted_graph_items_corrected_command": None,
    "intermediate_defect_survives_composition": None,
    "unchanged_domain_contract": None,
    "final_only_comment_testing_standards": None,
    "foreign_fork_isolation": None,
    "existing_regression_output": None,
    "ephemeral_probe_attribution_cleanup": None,
    "dynamic_degradation_warnings": None,
    "required_suite_outcomes": None,
    "revision_race_stale": None,
    "invalid_composition_and_graphs": None,
    "publication_permission_degradation": None,
    "local_waiver_isolation": None,
    "finding_memory_lifecycle": None,
    "safe_completion_and_scoped_stop": None,
    "untrusted_execution_cleanup": None,
    "same_store_supersession": same_store_supersession,
    "fresh_store_independence": fresh_store_independence,
    "versioned_history_and_views": None,
}
STUBBED_DISPATCH = frozenset(key for key, handler in DISPATCH.items() if handler is None)


def dispatch_handlers():
    """Return a detached handler map for C18 to extend without global mutation."""
    return dict(DISPATCH)


def run_scenario(registry, scenario_id, *, version=None, context=None, review_run_id=None):
    """Reusable harness entry point; it never fills in a verdict for a stub."""
    return registry.dispatch(
        scenario_id, version=version, context=context, review_run_id=review_run_id,
        handlers=dispatch_handlers(),
    )
