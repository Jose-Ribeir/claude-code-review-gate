# ADR 0001: Loosening verdict validation for enumerated improvised report families

- **Status:** Accepted
- **Date:** 2026-11-10
- **Deciders:** review-gate maintainers (operator-confirmed, grilled via the
  wayfinder process behind the "why the review-gate failed on 2026-11-10" map)
- **Implementation:** `_rescue_verdict` in `scripts/review-gate.py`
  (SPEC-06), reachable only after contract validation and near-miss
  normalization both fail.

## Context

On 2026-11-10 the review-gate discarded **16 finished, content-correct
reviews** in a single session. The reviewer workers did their job — they read
the diffs, found real issues, and produced complete JSON reports — but instead
of the contracted top-level findings-list schema, they emitted improvised
chunk-report schemas: per-axis objects (`standards` / `spec`) with counts and
nested findings, sometimes wrapped inside a top-level `review` object.

The gate's verdict validator was a single strict contract: any payload that did
not match it was treated as unparseable output, which fails closed (by design —
unparseable verdict state must block, never allow). The result was perverse:
sixteen genuinely good reviews were thrown away, every chunk burned its parse
retries, and the pushes blocked on infrastructure grounds rather than on any
review finding. Failing closed was protecting nothing — the signal existed and
was correct; only the envelope was wrong.

## Decision drivers

- **Fail-closed validation vs. banking good reviews.** Strictness exists so a
  hostile or malfunctioning reviewer cannot smuggle an unearned pass past the
  gate. But when finished, correct reviews are discarded wholesale, the gate
  destroys value it already produced and burns the operator's time and budget.
- **The incident's false "verdict: PASS" attribution must not recur in gate
  form.** Absence of gate output must stay absence of approval; an
  unreconstructable signal must never collapse into a pass.
- **`compute_verdict` must remain the only verdict authority.** The model's own
  `verdict` key stays advisory, in the rescue path as everywhere else.

## Decision

Loosen validation **conservatively, gate-side, and only for the enumerated
families** observed in the incident:

1. **Conservative rescue of exactly two improvised families.** When contract
   validation fails, a rescue pass recognizes (a) standards/spec axis reports
   and (b) review-wrapped reports. Recognition requires at least one real axis
   object; a lone `review` key or an unrelated counts dict is not a verdict and
   stays unrecognized. Findings are reconstructed only from actual finding
   objects (path/line/content) present in the JSON — never invented from prose.
2. **Warn fallback for counts without reconstructable findings.** Per-axis
   counts with no reconstructable finding objects (or counts exceeding them)
   map to a **warn** verdict plus a telemetry record. Real signal that can no
   longer be trusted as complete must degrade to a warning, never to a silent
   pass.
3. **No findings at all maps to the honest pass** — an axis report that
   reconstructs zero findings and carries no unaccounted counts is simply a
   clean review in an improvised envelope.
4. **Verdict always recomputed by `compute_verdict`** from the reconstructed
   findings list; the model's `verdict` key is never trusted.
5. **Anything else stays a parse failure** and keeps failing closed, exactly as
   before.
6. **Prompt-side counterpart:** chunk reviewer sessions are required to spawn
   the formatter subagent and relay its output verbatim as the final message
   (same rule as the final verdict), so the improvised families should stop
   being produced at the source. The gate-side rescue is a safety net for the
   ones that still slip through.

The rescue is also **telemetry-instrumented**: parse failures, rescues, and
warn-fallbacks each emit distinct telemetry records, so any drift — the
reviewer drifting away from the contract, or the rescue becoming a routine
path — is measurable rather than invisible.

## Consequences

- **The validator is no longer a single contract.** It is a contract plus an
  enumerated set of recognized improvisations. This is a deliberate, bounded
  loosening: new improvised shapes are *not* auto-accepted; they still fail
  closed, and extending the set is an explicit decision (a new ADR), not a
  code change.
- **Drift is measurable.** If the prompt-side formatter mandate erodes and
  improvised reports rise again, the telemetry shows it, and the cost shows up
  as rescued/warn-fallback records rather than as silently lost reviews.
- **The invariant "never silently pass" is preserved.** Reconstructed findings
  re-enter the normal verdict computation; unreconstructable signal degrades to
  warn; unrecognized shapes block. Absent gates still produce absence of
  output — never an inferred pass.
- **Good finished reviews are banked** instead of discarded, at the cost of
  ~one extra code path in the validator that must stay conservative and
  reviewed against the enumerated families.

## References

- Wayfinder map "why the review-gate failed on 2026-11-10" and its three
  closed research tickets (root-cause evidence and incident timeline).
- Spec: `.kimchi/plans/review-gate-fixes-spec.md`, "Implementation Decisions"
  (Verdict contract — gate/prompt side; Docs; Telemetry) and "Further Notes".
- Implementation: `_rescue_verdict` / `_verdict_candidate` in
  `scripts/review-gate.py`.
