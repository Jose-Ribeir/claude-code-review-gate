# ADR 0009: Native execution remains opt-in until real-session acceptance

- **Status:** Accepted
- **Date:** 2026-11-19
- **Deciders:** review-gate maintainers, following the settled SPEC-16
  rollout requirement.
- **Implementation:** `rg_native_executor.py`, `rg_native_scheduler.py`,
  `rg_final_native.py`, `rg_legacy_deep.py`, the final-review/store
  integration, and the operator runbook `docs/native-review-operations.md`.

## Context

Native execution replaces worker orchestration, not review criteria or the
verdict authority. It uses independently named, direct top-level Kimchi CLI
sessions and deterministic supervisor aggregation. It must not silently turn
into nested agents or a nested harness when native capability is unavailable.
The feature implementation and simulated coverage can land before its real
native-session acceptance (#21), but selecting the new transport before that
evidence exists would make uncertified behavior the production default.

The rollout also has to preserve historical run meaning. Adding task events,
artifact receipts, and scheduling metadata must not reclassify an interrupted
legacy run, manufacture a missing test result, or make an existing incomplete
run complete.

## Decision

1. **Both production defaults remain legacy until #21 is accepted.**
   `RG_FINAL_REVIEW_TRANSPORT` unset/blank or `legacy` selects the existing
   final-review worker; `OCR_DEEP_TRANSPORT` unset or `legacy` selects the
   existing deep reviewer. Exact, case-insensitive `native` values opt in after
   whitespace trimming. Unknown values are rejected before stack dispatch;
   blank is not a native opt-in. Native host capability failure produces a
   visible failure/incomplete outcome, never an implicit legacy fallback.

2. **Native scheduling is event-driven and bounded without changing evidence
   duties.** One run-scoped scheduler shares independent inference and
   execution resource classes, starting at four inference and two execution
   slots. These are conservative starting caps, not measured optima or a
   process-wide quota across independent runs. Every task has immutable run,
   unit, stage, lens, revision, input-digest, backend, model, artifact, and
   deadline identity plus an exact native cancellation handle. Host completion
   callbacks deliver output; the supervisor validates and atomically persists
   an artifact before the existing lens aggregation consumes it.

3. **Existing review stack shapes remain mandatory.** Standalone final review
   has individual and combined units. An N-item multi-reference stack has N
   individual units plus combined and prospective units. Each unit retains all
   seven required lenses. Legacy deep decomposition assigns each changed path
   once to a bounded subsystem task, retains all other changed paths as
   context, and adds one global task for cross-file chains, cross-file API
   contracts, and global invariants. Deterministic manifests and coverage
   evidence prevent omission or duplicate ownership.

4. **Trusted code remains the only join and verdict authority.** Lens outcomes
   retain required-lens order independent of callback order. Existing finding
   validation, falsification, memory, waivers, stop mapping, freshness,
   required verification, and `ocr_verdict.compute_verdict` semantics are
   unchanged. No DeepSeek service, model-generated aggregate verdict, nested
   agent, or nested review session is introduced.

5. **Native observability is additive.** Store schema v4 adds native task,
   lifecycle-event, artifact, and callback-receipt records. It does not alter
   existing run/unit/lens/test rows or reinterpret an empty `native_tasks`
   collection. Pre-native and legacy runs therefore retain their original
   completed or incomplete meanings. Human reports render native lifecycle
   only when native task records exist; the machine manifest remains the
   common source for both report forms.

6. **Publication and rollback preserve the trust boundary.** The native
   transport flag does not bypass publication eligibility. Provider green
   still requires complete scope, required evidence, memory, freshness, final
   pass, same-store continuity, non-supersession, and trusted orchestration.
   Pre-certification operators should use `--no-publish`. Rolling back is an
   explicit new legacy run after removing the native opt-in (or setting
   `legacy`); historical native artifacts remain available and are never
   rewritten into legacy evidence.

## Consequences

- Production behavior stays unchanged until operators deliberately opt in;
  test doubles explicitly select native and do not constitute #21 acceptance.
- Invalid transport names fail visibly instead of becoming accidental legacy
  runs. A native-capability failure likewise cannot conceal the architecture
  actually requested.
- A timeout can leave useful accepted artifacts, but their presence alone
  cannot finalize a run or publish green.
- Making native the default later is a separate acceptance-controlled change,
  not a consequence of merging these implementation and documentation tickets.
- The runbook is operational guidance, not a second canonical review contract.
  Versioned contract records, executable acceptance definitions, and durable
  run evidence retain their existing authority.

## Alternatives considered

- **Enable native immediately — rejected:** simulated coverage is not the
  required real-session acceptance and cannot certify the transport.
- **Fall back to legacy or nested sessions on native failure — rejected:** it
  would hide capability gaps and make reported transport identity false.
- **Rewrite inherited runs during migration — rejected:** additive evidence
  cannot justify changing what an earlier completed or interrupted run proved.
- **Create a model aggregation stage — rejected:** it would introduce a second
  finding/verdict authority and violate ADR 0001's trust boundary.

## References

- SPEC-16 native asynchronous review execution and timeout remediation,
  especially rollout user stories 55-57 and testing decisions excluding live
  acceptance from deterministic implementation tests.
- ADR 0001 (sole verdict authority) and ADR 0008 (complete final-review scope,
  historical evidence, and publication trust).
- Operator procedures: `docs/native-review-operations.md`.
- Durable native contracts: `native-review-task/v1`,
  `native-review-event/v1`, `native-review-artifact/v1`, and
  `native-review-artifact-commit/v1`.
