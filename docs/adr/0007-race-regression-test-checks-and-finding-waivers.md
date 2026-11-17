# ADR 0007: Race & regression test checks and granular finding waivers

- **Status:** Accepted
- **Date:** 2026-11-17
- **Deciders:** review-gate maintainers (operator-confirmed, grilled across
  four rounds behind SPEC-15)
- **Implementation:** the `parallel`/`timeout` checks-config keys
  (SPEC-15.1, `b80a823`); the waiver ledger and operator commands
  (SPEC-15.2, `de2a99d`); the parallel command-check scheduler with the
  isolated tip clone (SPEC-15.3, `00bcf0b`); the executor contract
  warnings and `evidence_source` (SPEC-15.4, `52a31b3`); the reference
  test-check executor `examples/test-check/` (SPEC-15.5, `480c511`);
  the command-check anchor fallback (SPEC-15.6, `41ce469`).

## Context

The reviewer reads diffs; it cannot verify behavior. Two defect classes
slip through that a reading model structurally cannot catch: **race
conditions**, which only manifest under an actual interleaving (ideally a
race detector), and **behavioral regressions** — a long-standing feature
silently stopping working because of the change under review. The nearest
existing machinery is structural: impact analysis finds broken callers of
renamed symbols, and `known_defects` re-detects previously flagged
problems. Neither answers "does the old feature still work".

The gate's only escape hatch was `git push --no-verify`, which waives
*everything*. A pusher with three findings — one they must fix and two
they expect and accept — could not acknowledge just the two: bypassing to
land the push also bypassed the one, and anything *new* on the next push
rode through on the same blanket bypass.

## Decision Drivers

- A defect a reader cannot see must be caught by **execution**, and the
  evidence must be auditable (verbatim output, named test), not a summary.
- The reviewer session and the dynamic check must not serialize — the
  feature should add wall-clock margin, not wall-clock time.
- Probe files are gate-authored; they must never appear in reviewer
  evidence or the user's working tree.
- A cheap one-shot selector model must never stall or falsely block a
  push (the spotter rule); every selector failure degrades in exactly one
  attempt.
- Acknowledging an expected finding must be **narrow** (per finding),
  **loud** (every verdict surface lists what was suppressed), and
  **self-lapsing** (a code change under the finding revives it).
- `compute_verdict` is the only verdict authority (ADR 0001); no new check
  or operator command may smuggle a block or a pass around it.
- Per-repo config first (ADR 0006's "helpful out of the box, never
  presumptuous" bar): no built-in duty ships until dogfooding defends a
  default.

## Decision

### 1. Config keys: `parallel` and `timeout` per command check

Each command check gains two optional keys in the layered `checks`
config: `parallel` (boolean, default `false`) and `timeout` (seconds,
default 60 — the pre-existing `_COMMAND_CHECK_TIMEOUT_S`, clamped to
1–3600). Invalid values degrade to load notes, never fatal (ADR 0006
pattern). A model-plus-tests executor needs a budget far above the
stuck-gate default, but raising the default for every check would weaken
stuck-gate protection for everything else — so the generous budget is
opt-in per check, and the default is unchanged.

### 2. The isolated tip clone

A check declaring `parallel: true` runs **concurrently with the review
session spawn** against a **dedicated throwaway clone of the tip** — never
`review_root`. Rationale: reviewer greps and reads must never observe
gate-authored probe files (contaminated evidence), and a probe writing
into the review tree would mutate what the reviewer is reading. One clone
serves the run's whole parallel subset, is registered under the run's
`worktrees` list so the async reaper cannot sweep it mid-run, and is
removed on every exit path. A clone-creation failure **skips** the
parallel checks outright with a warning — isolation is never traded for
throughput; the checks never fall back to `review_root`. Non-parallel
checks keep the exact prior semantics: sequential, post-review, against
`review_root`. The extra clone costs one local copy of the tip per opted-in
push; accepted as the price of isolation.

### 3. Executor contract: `warnings` and `evidence_source`

The SPEC-14 command-check contract is extended. The executor may return
`{"findings": [...], "warnings": [...]}`; warnings entries are
`{"file": <path-or-null>, "message": str}` — coverage gaps, unbuildable
probes, and flake demotions ride there and surface like every other check
degradation, never as a block. Findings may carry
`evidence_source: "existing_test" | "ephemeral_test"` (which kind of test
evidenced them); the gate validates the value against that enumeration —
unknown values drop the tag with a note while the finding survives. A
finding tagged `ephemeral_test` should come paired with the check's
canonical no-permanent-coverage warning (wording that *states* the lack —
a negation near "permanent test"/"coverage"); if the executor omits it,
the gate adds its own pairing note so the blind spot is still loud.

### 4. Selector pin, one attempt

The selector is the cheap one-shot model (`deepseek-v4.1-flash`, a
grilling decision): it reads a bounded manifest of the matched changed
files and answers strict JSON — existing repo tests to run, or throwaway
probe sources, or nothing. A malformed reply, a timeout, or an exec
failure costs exactly **one attempt and one warning**; there is never a
retry. Probes live only inside the executor's clone and are destroyed
with it. The reference executor (`examples/test-check/`) implements the
whole pipeline and is copy-in installable per repo.

### 5. Reproduce-then-block

A failing test (existing or probe) is **re-run exactly once** before it
becomes a finding: a reproduced failure is a blocking finding quoting the
verbatim output and naming the test; failed-once-then-passed is a flake
warning; a build/run failure is a warning (the probe never became a
running test, so it is evidence about nothing). **Rejected alternative:**
base/tip twin-runs (run the probe on the base and the tip, block only on
the delta) — it doubles every run's cost and buys precision the waiver
system already provides; false positives are cured by waivers, not by
more execution. Coverage gaps (nothing selected, nothing probed) are
warnings, never blocks — pushing into under-tested areas must not lock
anyone out. Tuning block→warn later is a `mode` config flip, not a
redesign. Findings and warnings ride the existing stream through the
falsify pass into `compute_verdict`, which stays the sole verdict
authority — the warn-mode demotion clamp applies to test-check findings
like any others.

### 6. The waiver ledger

Operator commands, following the ADR 0004 precedent: `review-gate.py
waive <finding-id>… [--note TEXT]`, `unwaive <finding-id>…`,
`waive --list` — standalone, between pushes, never on the push command
line. The ledger is a store-root, **repo-scoped** file (machine-local
like the verdict store; no cross-machine sharing, no expiry policy —
expectations are permanent until revoked) recording
`{sid, finding_id, waived_at, note?}` entries.

**Waiver identity is the finding's content anchor** — the same anchoring
`_known_defects` uses, inverted: a digest of the finding's path, its
signature line, and the tip's local text window around the finding's
location (±10 lines). The signature line comes from the finding's
`existing_code`; **when a finding carries none** — command checks such as
the test executor emit outputs, not diff hunks — the anchor falls back to
the tip text **at the finding's own location**: without that fallback,
every command-check finding would be permanently unwaivable. The fallback
keeps the same fail-closed posture: no path, no findable signature at the
location, or no blob at the tip yields an anchor that never matches, and
a blank tip line at the location yields no signature at all.

**Lapse semantics:** an anchor match at verdict assembly suppresses the
finding from the blocking set; a miss — no waiver, or the code changed so
the window text changed — judges the finding fresh. A far-field edit
outside the window does not lapse the waiver; any in-window change does.
`unwaive` restores blocking; unknown ids and empty histories degrade to
notes, never failures.

**Loud, always:** the pass/block text, the recorded verdict JSON, and
telemetry all carry the `waived` list whenever suppression happened —
waived findings stay recorded (never deleted from the findings log), so a
review cannot silently die in the ledger.

**Enforcement point:** verdict assembly, after the merged findings stream
is assembled and before `compute_verdict`. Waivers filter the *inputs*
to the verdict; they never alter the rule. `--no-verify` remains the
unchanged nuclear option. The waiver system applies to every finding
class — reviewer, deep reviewer, docs checker, command checks alike.

## Consequences

- A push into a repo with no declared test check costs nothing: the
  feature is entirely opt-in per repo (globs must match, and the executor
  must be declared and executable).
- One cheap selector model call per matching push, plus the runtime of
  the selected tests/probes and exactly one re-run per failure.
- Selector-authored probe quality on a cheap model is the dogfooding risk
  to watch; the degrade contract (warning, never block, one attempt) is
  the containment, and telemetry (tests selected, probes authored, runs,
  reproduce results, duration, warnings) makes the quality measurable.
- Waiver sids keyed to content anchors survive rebases and line movement
  but not in-window code changes; stale sids keyed to old anchoring
  schemes simply stop matching (fail-closed fresh judgment) — the ledger
  is machine-local, so no migration was ever needed.
- The static backstop (rubric thread-safety lens, language rules,
  risk-glob escalation) stays: races the dynamic path misses still have
  a blocking reviewer lane.
- Promotion of a built-in test-check duty is deferred until dogfooding
  defends defaults (a later ADR, per the ADR 0006 bar).

## References

- SPEC-15 ticket: `.kimchi/tracker/tickets/spec-15-race-regression-checks.md`
  (four-round grilling record; placement investigation
  `.kimchi/docs/race-and-regression-check-placement.md`)
- ADR 0001 (compute_verdict as the sole verdict authority), ADR 0004
  (operator-command precedent), ADR 0006 (checks config surface, the
  fail-soft degrade pattern this ADR reuses)
- Reference executor: `examples/test-check/` (README + `test_check`);
  unit suites `tests/test_executor_testcheck.py`,
  `tests/test_parallel_checks.py`, `tests/test_waivers.py`;
  acceptance scenarios `tests/sim_scenarios_spec15.py` (seam 2, real
  gate + real model against a scratch copy of this repository).
