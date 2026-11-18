"""Versioned SPEC-16 acceptance definitions and execution dispatch.

Scenario definitions are immutable within an acceptance version and are seeded
into :class:`rg_final_store.FinalStore`. Installing definitions never creates a
run or result. Only an explicit harness outcome can complete an acceptance run.

The dispatch seam takes callables supplied by the deterministic or real-model
harness. Missing callables produce an ``incomplete`` result, never a pass; no
provider transport is imported or called here.
"""
from __future__ import annotations

import copy

CURRENT_ACCEPTANCE_VERSION = "spec-16/v1"
IMPLEMENTATION_VERSION = "spec-16.C16/v1"
ACCEPTANCE_SCHEMA = "final-review-acceptance/v1"
RESULT_STATES = frozenset(("pass", "findings", "incomplete", "stale", "error"))
ACCEPTANCE_STATES = frozenset(("pending", "running")) | RESULT_STATES
TERMINAL_STATES = RESULT_STATES

_CLEANUP = ("source_repository_unchanged", "registered_scratch_clones_removed")
_WARNINGS = ("unexpected_warnings_absent",)
_TRUST = ("provider_writes_doubled", "trust_qualifiers_recorded")


def _scenario(number, slug, title, behavior, expected_state, evidence, *,
              cleanup=(), warnings=(), trust=()):
    return {
        "scenario_id": f"spec16-{number:02d}-{slug}",
        "title": title,
        "behavior": behavior,
        "expected_state": expected_state,
        "required_evidence": list(evidence),
        "dispatch_key": slug.replace("-", "_"),
        "cleanup_assertions": list(_CLEANUP + tuple(cleanup)),
        "warning_assertions": list(_WARNINGS + tuple(warnings)),
        "trust_assertions": list(_TRUST + tuple(trust)),
    }


_SCENARIOS = (
    _scenario(
        1, "standalone-provider-parity", "Standalone GitLab and GitHub parity",
        "One standalone GitLab MR and one standalone GitHub PR both review successfully through the same engine.",
        "pass", ("gitlab_result", "github_result", "shared_engine_identity"),
    ),
    _scenario(
        2, "linear-stack-individual-combined", "Three individual reports and one combined report",
        "A three-item linear stack produces three individual reports and one combined report, and no item officially passes unless all four verdicts pass.",
        "pass", ("individual_reports", "combined_report", "all_or_nothing_verdicts"),
        trust=("official_pass_requires_every_unit",),
    ),
    _scenario(
        3, "omitted-graph-items-corrected-command", "Omissions abort with corrected commands",
        "Omitted ancestor and omitted descendant cases abort with the complete corrected command.",
        "error", ("omitted_ancestor_error", "omitted_descendant_error", "corrected_commands"),
    ),
    _scenario(
        4, "intermediate-defect-survives-composition", "Intermediate defects remain visible",
        "A defect introduced in one item and repaired or masked by a descendant remains visible at the individual level while the combined behavior is evaluated separately.",
        "findings", ("individual_finding", "combined_behavior", "independent_verdicts"),
    ),
    _scenario(
        5, "unchanged-domain-contract", "Unchanged domain documents remain authoritative",
        "Code contradicting an unchanged ADR or glossary entry produces a documentation/domain finding even when the document is absent from every diff.",
        "findings", ("unchanged_document", "domain_finding", "complete_tree_evidence"),
    ),
    _scenario(
        6, "final-only-comment-testing-standards", "Final review exceeds incremental scope",
        "Comment and testing-standard findings that incremental local scope excludes are available to final review.",
        "findings", ("incremental_scope_exclusion", "comment_finding", "testing_standard_finding"),
    ),
    _scenario(
        7, "foreign-fork-isolation", "Another author's fork is isolated",
        "Another author's fork contribution is fetched, reviewed, isolated, and never confused with a local branch.",
        "pass", ("provider_fork_revision", "author_identity", "local_branch_independence", "isolation_evidence"),
        trust=("fork_credentials_not_in_worker",),
    ),
    _scenario(
        8, "existing-regression-output", "Existing regression failures block verbatim",
        "An existing regression suite failure blocks with verbatim output.",
        "findings", ("required_suite_identity", "verbatim_failure_output", "blocking_verdict"),
    ),
    _scenario(
        9, "ephemeral-probe-attribution-cleanup", "Ephemeral probes block with attribution",
        "An ephemeral race or regression probe reproduces a failure, blocks with evidence-source attribution, emits its permanent-coverage warning, and leaves no probe behind.",
        "findings", ("reproduced_failure", "evidence_source_attribution", "permanent_coverage_warning", "probe_absence"),
        cleanup=("ephemeral_probe_removed",), warnings=("permanent_coverage_gap_recorded",),
    ),
    _scenario(
        10, "dynamic-degradation-warnings", "Dynamic degradation never claims verification",
        "A fail-once/pass-once test warns; a probe build failure, no dynamic coverage, or dynamic executor timeout warns without claiming verification.",
        "pass", ("fail_once_warning", "probe_build_warning", "no_coverage_warning", "timeout_warning", "verification_not_claimed"),
        warnings=("dynamic_degradation_is_not_pass_evidence",),
    ),
    _scenario(
        11, "required-suite-outcomes", "Required failure blocks and degradation is incomplete",
        "A required suite failure blocks and an unexecutable declared required suite produces incomplete.",
        "incomplete", ("reproduced_failure_state", "unexecutable_suite_state", "suite_execution_evidence"),
        warnings=("required_degradation_is_not_dynamic_warning",),
    ),
    _scenario(
        12, "revision-race-stale", "Revision movement is stale",
        "A push, retarget, or base movement before completion produces stale and cannot publish a pass.",
        "stale", ("source_movement", "retarget_movement", "base_movement", "stale_results", "pass_publication_blocked"),
        trust=("stale_pass_never_published",),
    ),
    _scenario(
        13, "invalid-composition-and-graphs", "Invalid graphs fail without guessing",
        "A composition conflict, branching graph, prose-only dependency, cycle, duplicate, mixed target, and inconsistent order each produce their explicit validation/incomplete outcomes without guessing.",
        "incomplete", ("composition_conflict", "branching_graph", "prose_only_dependency", "cycle", "duplicate", "mixed_target", "inconsistent_order", "no_invented_order"),
    ),
    _scenario(
        14, "publication-permission-degradation", "Publication denial is unenforced",
        "Unavailable status publication produces local-only/unenforced output rather than an official green check.",
        "pass", ("publication_error", "local_only_result", "unenforced_result", "official_green_absent"),
        trust=("local_only_unenforced", "no_manufactured_green",),
    ),
    _scenario(
        15, "local-waiver-isolation", "Local waivers do not suppress official findings",
        "A machine-local waiver remains visible but cannot suppress an official final finding.",
        "findings", ("visible_local_waiver", "official_finding", "official_verdict_not_green"),
        trust=("local_waiver_not_official_suppression",),
    ),
    _scenario(
        16, "finding-memory-lifecycle", "Finding memory requires positive closure",
        "A prior internal finding remains blocking across a quiet rerun, retires after changed evidence and positive revalidation, and records a falsified finding when disproved.",
        "findings", ("quiet_rerun_blocking", "changed_evidence", "positive_revalidation", "resolution_event", "falsification_event"),
    ),
    _scenario(
        17, "safe-completion-and-scoped-stop", "Safe work finishes and stops stay scoped",
        "Ordinary findings do not prematurely stop safe independent lenses; a genuine critical stop halts only affected dependents and records every skipped mandatory stage as incomplete.",
        "incomplete", ("ordinary_finding_safe_lenses", "critical_stop_scope", "independent_work_completed", "skipped_mandatory_stages", "skipped_states"),
    ),
    _scenario(
        18, "untrusted-execution-cleanup", "Untrusted execution leaves no mutations or secrets",
        "No run mutates the source repository, stores provider-write credentials in the sandbox, publishes gate-authored probes, or leaves registered scratch clones behind.",
        "pass", ("source_snapshot_unchanged", "worker_environment_scrubbed", "probes_unpublished", "scratch_registry_empty"),
        cleanup=("nested_worktrees_removed", "downloaded_content_removed"),
        trust=("provider_write_credentials_absent",),
    ),
    _scenario(
        19, "same-store-supersession", "Older same-store runs cannot overwrite newer results",
        "An older run in one persistent store cannot overwrite the newer result for the same stack after supersession.",
        "pass", ("superseded_run", "blocked_publication", "same_store_history"),
        trust=("superseded_publication_blocked",),
    ),
    _scenario(
        20, "fresh-store-independence", "Fresh stores are independent and unenforced",
        "A fresh store performs an independent review but cannot assert another store's finding continuity or official supersession.",
        "pass", ("independent_identity", "unenforced_trust", "isolated_history"),
        trust=("cross_store_continuity_absent", "cross_store_supersession_absent"),
    ),
    _scenario(
        21, "versioned-history-and-views", "Upgrades retain original run meanings",
        "Contract and acceptance upgrades preserve prior runs under their original versions and expose generated current and historical views.",
        "pass", ("original_contract_version", "original_acceptance_version", "current_view", "historical_view", "retained_result"),
    ),
)


def acceptance_seed(version=CURRENT_ACCEPTANCE_VERSION,
                    implementation_version=IMPLEMENTATION_VERSION, extra_scenarios=()):
    """Return a detached, additive acceptance document suitable for migration."""
    scenarios = copy.deepcopy(list(_SCENARIOS))
    scenarios.extend(copy.deepcopy(list(extra_scenarios)))
    return {
        "schema": ACCEPTANCE_SCHEMA,
        "version": str(version),
        "implementation_version": str(implementation_version),
        "scenarios": scenarios,
    }


class AcceptanceRegistry:
    """Public install/list/run/history/dispatch seam over a ``FinalStore``."""

    def __init__(self, store, handlers=None):
        self.store = store
        self.handlers = dict(handlers or {})

    def install(self, document=None):
        document = acceptance_seed() if document is None else document
        self.store.install_acceptance(document)
        return self.store.acceptance_view(document["version"])

    def list(self, version=None):
        return self.store.acceptance_view(version).get("scenarios", [])

    def start(self, scenario_id, *, version=None, review_run_id=None, details=None):
        return self.store.start_acceptance_run(
            scenario_id, version=version, review_run_id=review_run_id, details=details,
        )

    def complete(self, acceptance_run_id, state, *, evidence=None, error="", details=None):
        return self.store.complete_acceptance_run(
            acceptance_run_id, state, evidence=evidence, error=error, details=details,
        )

    def history(self, scenario_id=None, *, version=None, review_run_id=None):
        return self.store.acceptance_history(
            scenario_id=scenario_id, version=version, review_run_id=review_run_id,
        )

    def dispatch(self, scenario_id, handlers=None, *, version=None, review_run_id=None,
                 context=None, details=None):
        """Start, invoke, and durably complete one scenario without provider I/O.

        A handler receives one mapping containing the registry, store, pinned
        scenario definition, acceptance run IDs, and caller context. It returns
        a mapping with ``state`` and optional ``evidence``, ``error`` and
        ``details``. Handler exceptions become human-readable ``error`` results.
        """
        run = self.start(
            scenario_id, version=version, review_run_id=review_run_id, details=details,
        )
        run_id = run["acceptance_run_id"]
        scenario = run["scenario"]
        dispatch_key = scenario["dispatch_key"]
        active_handlers = self.handlers if handlers is None else handlers
        handler = active_handlers.get(dispatch_key)
        if not callable(handler):
            return self.complete(
                run_id, "incomplete",
                evidence={"dispatch": {"key": dispatch_key, "status": "stubbed"}},
                error=f"scenario dispatch {dispatch_key!r} is intentionally stubbed for C18",
                details={"provider_writes": False, "executed": False},
            )
        try:
            outcome = handler({
                "registry": self,
                "store": self.store,
                "scenario": scenario,
                "acceptance_run_id": run_id,
                "review_run_id": review_run_id,
                "context": context,
            })
            if not isinstance(outcome, dict):
                raise ValueError("scenario handler must return an outcome mapping")
            return self.complete(
                run_id, outcome.get("state", ""),
                evidence=outcome.get("evidence"), error=outcome.get("error", ""),
                details=outcome.get("details"),
            )
        except Exception as exc:
            current = self.store.acceptance_run(run_id)
            if current["state"] not in ("pending", "running"):
                raise
            message = str(exc).strip() or "no exception message"
            return self.complete(
                run_id, "error",
                evidence={"dispatch": {"key": dispatch_key, "status": "raised"}},
                error=f"{type(exc).__name__}: {message}",
                details={"provider_writes": False, "executed": True},
            )
