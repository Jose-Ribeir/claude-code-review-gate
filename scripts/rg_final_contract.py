"""Versioned SPEC-16 review-contract seeds for the final-review store.

This module contains data, not a parallel operator manual.  The canonical
runtime representation is installed into SQLite by :mod:`rg_final_store`;
stable rule and decision keys let later migrations add versions without
rewriting the meaning of a historical run.

No acceptance scenarios are defined here.  Their registry is a later unit.
"""

CURRENT_CONTRACT_VERSION = "spec-16/v1"
IMPLEMENTATION_VERSION = "spec-16.C3/v1"

# The text deliberately records the complete approved behavior contract, while
# remaining structured enough for contract-show and historical report views.
_RULES = (
    ("explicit-command", "Explicit final review", "Final whole-stack review begins only through an explicit final-review command. Incremental pre-push success is never final approval, and the system never infers that a push is the last one."),
    ("provider-selection", "Provider-selected immutable inputs", "References are resolved through authenticated provider read access independently of author, local branches, checkout state, and fork ownership. Only provider-pinned source and target SHAs are reviewed."),
    ("provider-graph", "Provider dependency graph", "The open-item provider source/target graph is the sole dependency authority. Supplied items must form one complete linear stack; omissions, duplicates, gaps, cycles, branching, mixed roots or repositories, and inconsistent ordering are rejected without inventing composition order."),
    ("omission-confirmation", "Explicit omission correction", "Discovery identifies omitted ancestors and descendants but never silently adds them. The error includes the complete corrected command so the operator can explicitly confirm a new invocation."),
    ("intent-not-graph", "Intent is not dependency metadata", "Descriptions and linked issues or specifications are mandatory retrieval attempts and review evidence, never dependency declarations. Prose-only dependencies require provider-target correction and missing intent remains a visible warning rather than invented requirements."),
    ("individual-and-combined", "Individual and composed review", "Every supplied item receives an individual review from its complete declared-target merge-base diff and pinned source tree. The ordered composition also receives a combined review against the pinned root target and a prospective integration review against the current target."),
    ("pass-requirement", "Complete stack pass requirement", "An official stack pass requires every individual unit and the combined unit to pass. A descendant repair cannot erase an intermediate defect, and combined success cannot clear an individual failure."),
    ("immutable-materialization", "Disposable immutable trees", "Review, composition, required-test, and dynamic-check trees are clean disposable clones or worktrees populated only from pinned provider revisions. Caller changes and uncommitted content never contaminate review evidence, and composition conflicts or unreproducible trees make affected scopes incomplete."),
    ("complete-repository", "Complete repository availability", "Every reviewer can iteratively inspect the complete repository, including unchanged standards, ADRs, glossary and domain documentation, callers, tests, required recursive submodules, and required LFS content. Missing required content produces incomplete evidence, not a reduced-scope pass."),
    ("independent-lenses", "Independent mandatory lenses", "Correctness, repository standards, documentation/domain, testing, security, compatibility/migration, and intent lenses run independently at every individual and combined level. Combined review adds cross-item contracts and integration behavior; no small-diff exemption disables a lens."),
    ("aggregation", "Aggregate after lens completion", "Lens contexts remain independent and findings aggregate only after every applicable lens completes or explicitly degrades. A mandatory lens that cannot finish makes its scope incomplete. Model-declared verdicts never bypass finding validation and falsification."),
    ("sole-verdict", "Sole verdict authority", "compute_verdict remains the only finding-based pass/block authority. The supervisor adds freshness, coverage, trust, and execution metadata but never creates a competing severity rule or infers green from absent output."),
    ("final-states", "Final review states", "Recorded final states are pass, findings, stale, and incomplete. Warnings may accompany any state where meaningful. Publication trust qualifiers are separate and visible."),
    ("required-verification", "Required verification", "Repository-declared required suites and CI checks run on every applicable individual and composed tree. A reproduced required failure blocks; a required stage that cannot execute or be assessed makes the scope incomplete. Absence of a declared suite is recorded rather than presented as a passing suite."),
    ("dynamic-checks", "Isolated dynamic checks", "Explicit final review opts into SPEC-15 dynamic checks at every individual and combined level in dedicated throwaway clones. Existing regression tests, relevant race detectors, or one-shot ephemeral probes are selected without changing pre-push defaults or permanently authoring tests."),
    ("dynamic-degradation", "Dynamic evidence limits", "A reproduced dynamic failure enters findings with verbatim output and existing_test or ephemeral_test attribution. Fail-once/pass-once behavior, uncovered behavior, selector or probe build failures, and dynamic timeouts are warnings with telemetry, never fabricated verification or a mandatory-suite substitute."),
    ("normal-completion", "Broad evidence before aggregation", "After materialization, ordinary findings do not short-circuit other independent mandatory lenses and tests. The run retains broad evidence even when a blocking finding is already known."),
    ("structured-stop", "Scoped structured stops", "A structured STOP is used only when continuation would make dependent work unsafe, invalid, or untrustworthy. The supervisor identifies affected trees and dependents, permits independent safe units to finish, and records every skipped mandatory stage with an incomplete result and original stop evidence."),
    ("finding-memory", "Same-store finding memory", "Unresolved final-review findings persist with content and evidence anchors and are supplied to subsequent runs in the same store for explicit revalidation. Quiet reruns clear nothing; retirement requires changed evidence plus positive resolution evidence or an explicit recorded falsification."),
    ("waiver-isolation", "Official waiver isolation", "Machine-local waivers can be displayed as operator context but are excluded from official final-review suppression. They cannot turn a published final verdict green, and no provider-visible waiver mechanism is implied."),
    ("freshness", "Revision freshness", "The complete revision tuple is pinned at start and fetched again before finalization. Source, target, retarget, ancestor, or base movement makes the run stale, prevents publication of a pass, preserves evidence under the old SHAs, and requires explicit restart."),
    ("run-identity", "Run identity and same-store ordering", "Each run has a durable UUID and a monotonic sequence within its store. Claims and reports bind run, store, stack, unit, contract, acceptance, and exact revision identity. A newer-started run supersedes older runs over the same stack only in that store, and all historical rows are retained."),
    ("store-boundary", "Explicit store continuity boundary", "Every verdict carries a persistent store_instance_id. Finding continuity, acceptance history, and monotonic supersession apply only to processes sharing that database. A fresh unrelated store performs an independent review and cannot claim continuity or official supersession; continuity-required official results become unenforced."),
    ("contract-history", "Versioned contract and decision history", "The detailed contract, decision log, and acceptance identities used by a run are database records under stable versions. Upgrades add versions without rewriting historical runs or the rules under which they were judged, and database views expose current and historical records."),
    ("reports", "Auditable machine and human reports", "Every unit and stack composition can generate machine-readable and human-readable reports from SQLite. Reports enumerate revisions, coverage, lenses, tests, findings and anchors, warnings, stops, skipped work, trust qualifiers, and publication attempts; the combined report links all individual reports."),
    ("publication", "Distinct trustworthy publication", "Final-review statuses use a dedicated context and are published only after final state and trust calculation. Every participating item reflects both individual and combined results and includes run and exact revision identity. Missing permission retains local-only, unenforced output rather than manufacturing green."),
    ("security", "Untrusted-code security boundary", "Fetched code, descriptions, tests, probes, and build definitions are untrusted. Workers and tests run without provider-write credentials or unrelated secrets; only the trusted least-privilege orchestrator accesses metadata and publication, and cleanup covers success, failure, stop, and stale paths."),
    ("observability", "Structured observability", "Graph discovery, execution coverage, dynamic checks, stale outcomes, critical stops, store identity, and publication degradation emit structured telemetry without secrets or unrelated source content."),
    ("external-bot-boundary", "External bot exclusion", "Final-review storage neither imports nor deduplicates the external review bot and promises no memory or suppression across that separate system."),
)

_DECISIONS = (
    ("command-shape", "One provider-neutral operator command", "The existing standalone operator-command pattern serves local and CI use; chat interfaces are thin delegations and incremental pre-push remains separate."),
    ("gitlab-first", "GitLab first behind a common adapter", "Provider normalization, graph metadata, pinned revisions, and publication transport belong to adapters. GitHub later reuses identical verdict, memory, store, stop, cleanup, and trust semantics."),
    ("complete-stack-input", "Users supply every item", "Discovery verifies the provider graph and proposes corrected commands but does not silently mutate the requested stack or automatically resolve conflicts."),
    ("three-review-views", "Individual, combined, and prospective views", "Individual units expose intermediate breakage, ordered composition exposes final state and cross-item contracts, and prospective integration exposes destination incompatibility."),
    ("whole-tree-access", "Whole repository is an availability guarantee", "Reviewers receive iterative full-tree access rather than a prompt containing every repository byte. Standards and unchanged domain documents remain assessable."),
    ("validated-findings", "Validation before authoritative findings", "Every finding and warning traverses the existing validation and falsification stream; high-confidence actionable findings use compute_verdict while speculative concerns and optional nits stay visible non-blocking warnings."),
    ("dynamic-opt-in", "Explicit final invocation opts into dynamic checks", "Final review does not promote dynamic execution into a pre-push duty or alter per-repository pre-push defaults."),
    ("required-vs-best-effort", "Required and dynamic failures differ", "Declared required-suite degradation is incomplete, while best-effort dynamic coverage degradation is a warning; both remain explicit."),
    ("stop-is-not-severity", "Critical finding alone is not a stop", "Continuation stops only when dependent work would be invalid, unsafe, or untrustworthy, with scopes and skipped mandatory work recorded."),
    ("anchors-for-memory", "Anchors drive revalidation", "Content and evidence anchors, not quiet output or lucky reruns, govern same-store finding carry-forward, resolution, and falsification."),
    ("no-local-official-waiver", "Local waivers stay local", "Existing machine-local waiver storage is not an official final-review suppression input."),
    ("stale-restart", "Movement requires explicit restart", "Runs never enter an automatic review loop as stack revisions move, and old evidence remains bound to old revisions."),
    ("sqlite-external", "Embedded external SQLite", "The store follows the existing external store-root convention, uses WAL, foreign keys, explicit transactions, bounded writer retries, and migrations, and introduces no server."),
    ("store-local-order", "No cross-store ordering fiction", "Monotonic supersession and finding continuity have meaning only in a shared persistent store; fresh CI stores honestly report independent and unenforced continuity."),
    ("database-views", "Database-backed operator views", "Contract show, acceptance views, and auditable reports derive from database records rather than parallel canonical Markdown manuals; the required architectural ADR remains separate."),
    ("publication-after-trust", "Publish after final trust", "Publication follows final-state, freshness, and trust calculation and never confuses a local process exit with enforced merge policy."),
    ("least-privilege", "Trusted supervisor and isolated workers", "Provider metadata and status mutation remain in the trusted supervisor while untrusted review and test execution receive no mutation credentials."),
    ("retain-history", "Historical rows are immutable evidence", "Superseded, stale, stopped, and failed runs and their original contract and acceptance versions are preserved rather than deleted or rewritten."),
    ("ci-persistence", "CI needs persistent authoritative state", "Official continuity in CI requires a persistent provisioned store or submission to an authoritative installation; ephemeral per-job databases cannot counterfeit history."),
    ("scope-cost", "Explicit invocation is the cost boundary", "Whole-stack evaluation may require substantial model, Git, storage, and test work, and budget exhaustion never silently falls back to incremental mode."),
)


def contract_seed(
    version=CURRENT_CONTRACT_VERSION,
    implementation_version=IMPLEMENTATION_VERSION,
    extra_rules=(),
    extra_decisions=(),
):
    """Return a detached contract document suitable for a versioned migration.

    ``extra_rules`` and ``extra_decisions`` support additive future contract
    migrations. Existing keys retain their order and meaning.
    """
    rules = [
        {"rule_key": key, "title": title, "body": body}
        for key, title, body in _RULES
    ]
    rules.extend(dict(rule) for rule in extra_rules)
    decisions = [
        {"decision_key": key, "title": title, "body": body}
        for key, title, body in _DECISIONS
    ]
    decisions.extend(dict(decision) for decision in extra_decisions)
    return {
        "version": str(version),
        "implementation_version": str(implementation_version),
        "rules": rules,
        "decision_log": decisions,
    }
