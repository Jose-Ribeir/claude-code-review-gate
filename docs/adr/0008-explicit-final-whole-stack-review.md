# ADR 0008: Explicit final whole-stack review and its local trust boundary

- **Status:** Accepted
- **Date:** 2026-11-18
- **Deciders:** review-gate maintainers, following the settled SPEC-16 record.
- **Implementation:** the `scripts/rg_final_*.py` supervisor, store, execution,
  memory, and publication modules; C19 adds `scripts/rg_final_telemetry.py`.

## Context

Incremental pre-push review evaluates the latest uncovered push range. Green
pushes therefore do not add up to final whole-change approval, and diff-only
review cannot check unchanged standards, ADRs, domain documents, callers, or
tests. Individual-only stack review misses integration defects; combined-only
review can hide an intermediate defect repaired by a descendant.

No signal reliably identifies a final push. Final approval consequently needs
an explicit scope, immutable revision identity, durable evidence, and trust
semantics that do not confuse local output with enforced provider policy or
unrelated databases with shared history. This decision preserves ADR 0001's
sole verdict authority, ADR 0004's standalone-command precedent, ADR 0006's
evidence/degradation duties, and ADR 0007's isolated dynamic checks, anchors,
and local-waiver distinction.

## Decision

### 1. Explicit invocation and complete linear scope

`review-gate.py final-review <ref>…` serves local and CI use; one reference is
the one-item form and multiple references explicitly name one complete
base-first linear stack. Chat interfaces are thin delegations. Pre-push remains
a separate fast-feedback facility: no green or presumed-final push invokes or
substitutes for this command.

Authenticated provider source/target edges are the sole dependency authority,
not authorship, local branches, fork ownership, or prose. Discovery validates
open items, target/root identity, uniqueness, connectivity, ordering, and
linearity. Omitted ancestors/descendants produce the complete corrected command
for explicit rerun; discovery never silently changes the supplied set or order.
Gaps, cycles, branches, mixed targets, prose-only dependencies, and inconsistent
ordering require correction rather than inferred composition.

Every item receives an individual review against its declared-target merge
base and pinned source. The ordered heads also receive a combined review against
the pinned root target and a prospective integration view against the current
target. An official pass requires every individual and the combined result;
a descendant repair or combined success cannot erase an intermediate defect.

### 2. Complete evidence and one verdict authority

Immutable disposable trees are materialized only from provider revisions.
Independent mandatory lenses at each individual and combined level can
iteratively inspect the complete repository, including unchanged instructions,
ADRs, glossaries/domain documents, implementation, callers, tests, and required
submodules/LFS content. Combined review additionally covers cross-item contracts
and integration behavior. Complete repository access is an availability
guarantee, not a requirement to serialize every byte into a prompt. Missing
content needed for assessment makes that scope incomplete.

Descriptions and linked intent are mandatory retrieval attempts and untrusted
evidence, never graph edges or invented requirements. Lens contexts stay
independent until all applicable work finishes or explicitly degrades; an
unfinished mandatory lens cannot produce a warning-only pass. All findings and
warnings traverse existing validation/falsification. Only
`ocr_verdict.compute_verdict` determines finding-based pass/block; worker
verdicts, absent output, telemetry, reports, and store views have no competing
authority. Final states are `pass`, `findings`, `stale`, and `incomplete`, with
publication trust qualified separately.

### 3. Required evidence differs from best-effort dynamic coverage

Repository-declared required suites run at every applicable level. Reproduced
required failures enter ordinary blocking findings; an unexecutable or
unassessable required stage makes its scope incomplete. No declaration means
recorded absence, not a passing suite.

Explicit final review opts into ADR 0007's isolated dynamic checks at each
individual and combined level without changing pre-push defaults. Reproduced
dynamic failures become attributed findings with durable verbatim evidence.
Fail-once/pass-once, selector/probe/build failures, uncovered behavior, and
dynamic timeouts are warnings with telemetry, never fabricated verification or
required-suite substitutes. Required-stage degradation therefore remains
`incomplete`, while best-effort dynamic degradation remains visible warning
coverage.

Ordinary findings do not short-circuit other useful stages. A structured STOP
is valid only with a high-confidence actionable critical finding and positive
evidence that continuation would be `unsafe`, `invalid`, or `untrustworthy`.
The trusted supervisor validates it and maps affected trees/dependents; worker
scope is advisory. Rejected stops stay visible and safe work continues. A
validated stop preserves its original finding, lets independent safe units
finish, and records every affected/skipped mandatory stage as incomplete.

### 4. Freshness and memory require positive proof

The complete source/target/base/ancestor tuple is pinned at start and re-fetched
before finalization. Movement makes the run stale, blocks publication of a pass,
preserves evidence under old SHAs, and requires explicit restart rather than an
automatic review loop.

Unresolved findings persist with content/evidence anchors and are explicitly
revalidated on same-store reruns. Quiet reruns clear nothing. Retirement needs
changed evidence plus positive resolution evidence, or recorded falsification.
Existing machine-local waivers may be shown as operator context but are excluded
from official final suppression and cannot make a provider result green. This
memory does not ingest or suppress the separate external review bot.

### 5. Versioned SQLite records and the exact store boundary

The external store-root database uses embedded SQLite, WAL, foreign keys,
explicit transactions, bounded writer retries, and additive migrations; no
server is introduced. Runs bind stack, unit, revision, store, contract, and
acceptance identities, while lenses, tests, findings/anchors, warnings,
coverage, stops, skipped work, trust, and publication attempts remain auditable.

Contract rules/decisions and acceptance definitions/results have stable versions
(currently both `spec-16/v1`) and implementation versions. Every run retains the
exact versions used; upgrades add versions without rewriting historical
meaning. Contract, acceptance, and current/historical reports are generated from
these records. An unexecuted acceptance scenario is not a pass. This ADR retains
architecture and rationale, not a parallel canonical manual or scenario list.

Every database has a persistent `store_instance_id`. Finding continuity,
acceptance history, monotonic run sequencing, and supersession apply **only** to
processes sharing that database. A newer same-store run supersedes an older
stack run, late old publication is blocked, and all historical rows remain.

A fresh/unrelated database may perform a complete fresh review, but is
independent: it cannot claim another store's finding memory, acceptance history,
or supersession. When required continuity is unavailable, its official result
is local-only and unenforced. CI needs a persistent provisioned store or an
authoritative-installation submission to claim continuity; an ephemeral job
store cannot manufacture it. No cross-store/cross-machine atomic ordering is
claimed without shared state.

### 6. Trusted publication and bounded observability

Only a least-privilege trusted supervisor handles provider metadata and status
publication. Workers and tests receive no provider-write tokens or unrelated
secrets and never mutate reviewed branches. After final-state/freshness/trust
calculation, the dedicated `review-gate/final-review` context publishes every
participating item's individual-plus-combined obligation with run/store/unit,
contract, and exact revision identity. Missing permission or transport failure
retains local-only, unenforced evidence, never manufactured green. Scratch trees
and probes are cleaned on success, failure, stop, and stale paths.

Structured counters/events cover graph discovery, unit/lens/test and dynamic
coverage, stale outcomes, validated/rejected stops, store identity, same-store
supersession, and publication degradation. `FinalReviewTelemetry` provides
allowlisted machine fields, bounded counts, event retention and serialization,
rejection/drop accounting, and detached JSON snapshots suitable for existing
store evidence/details records. It excludes credentials, source snippets,
commands, test output, URLs, paths, provider responses, and unrelated source;
it computes no verdict and owns no store writes.

C19 supplies only this standalone helper and direct tests. Later wiring must
call its recording methods at real supervisor/store seams and persist the
snapshot with the corresponding run. Until then integrated production telemetry
is not claimed, and acceptance-harness output does not imply runtime emission.

## Consequences

- Whole-stack review is intentionally expensive; exhaustion cannot silently
  fall back to incremental scope.
- Local, stale, independent, unenforced, superseded, and enforced provider
  outcomes remain distinguishable and historically auditable.
- Providers share verdict, memory, stop, store, and trust behavior; adapters
  own metadata/revision lookup and publication transport.
- Telemetry failure cannot alter review outcomes. Actual call-site wiring and
  snapshot persistence remain follow-up work to this C19 helper.

## Alternatives considered

- **Infer finality or replace incremental pre-push — rejected:** finality cannot
  be guessed, and the two scopes answer different questions.
- **Individual-only or combined-only review — rejected:** each leaves a distinct
  intermediate-item or integration blind spot; both are required.
- **Diff-only prompts — rejected:** bounded prompts cannot establish complete
  repository evidence; iterative full-tree access avoids that false assurance.
- **Worker verdicts, local waivers, quiet reruns, or automatic freshness
  restarts — rejected:** they respectively bypass the verdict authority, make
  private suppression official, permit rerun-until-green, or create moving-SHA
  review loops instead of positive closure and explicit restart.
- **Centralized cross-store coordination — rejected:** it would disguise the
  persistence requirement rather than preserve the honest same-store boundary.
- **A parallel canonical Markdown manual — rejected:** it would drift from
  versioned executable acceptance and historical run meaning; this ADR records
  decisions, while SQLite owns operational detail.
- **Unrestricted telemetry payloads — rejected:** raw diagnostics can disclose
  secrets/source and grow without bound; only bounded contracted data is valid.

## References

- SPEC-16: `.kimchi/tracker/tickets/spec-16-final-review.md` in the enclosing
  workspace (seven settled decision rounds).
- ADR 0001, ADR 0004, ADR 0006, and ADR 0007.
- Versioned contracts: `scripts/rg_final_contract.py` and
  `scripts/rg_final_acceptance.py`; store/publication semantics:
  `scripts/rg_final_store.py` and `scripts/rg_final_publish.py`.
- C19 helper/tests: `scripts/rg_final_telemetry.py` and
  `tests/test_final_observability.py`.
