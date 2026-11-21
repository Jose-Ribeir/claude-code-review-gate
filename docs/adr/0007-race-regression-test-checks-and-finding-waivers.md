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
`review_root`. Start failures are self-cleaning: a clone factory that
raises (including after partial creation) or a worker thread that cannot
be constructed or started removes the clone (or its deterministic
intended path) and its `worktrees` registration and degrades to the same
skip warning — never an orphaned directory, never a crash of the push.
The supervisor's cleanup join is bounded (5 s after the per-check budgets
should have finished): a worker still running is warned about
("still running; parallel-check findings discarded") and its findings
discarded, and an abnormal exit that never joined still drains the
worker's warnings and telemetry, so degradation is observable and never
silent. The extra clone costs one local copy of the tip per opted-in
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
 The
wrapper may also carry a `telemetry` object of actual run counts
(`selector_attempts`, `tests_selected`, `probes_authored`, `runs`,
`reproduce_results` with `fail`/`pass`/`flake`/`build`/`error`), which
the gate merges into that check's `command_checks` telemetry entry;
malformed values degrade to a warning and are ignored. The gate exports
the check's resolved per-check budget to the executor as
`OCR_CHECK_TIMEOUT_S` (string seconds, on top of the inherited
environment) so a cooperative executor clamps its own selector/test/probe
budgets to finish before the gate's kill (a small grace margin, 5 s or
5% of the budget, whichever is smaller); without the variable the
executor's knobs and defaults stand unchanged. The selector's probe
objects may carry an optional `path` — the repo-relative file whose
behavior the probe targets, defaulting to the first matched file when
absent.

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

**Evidence binding (why waiver identity includes the finding's
evidence):** a test-evidenced finding (`evidence_source` set) says "this
test failed, here is the output" — the acknowledged thing is the FAILURE,
not the file's first line. Executor findings report `start_line` 1 on the
failing test's target, so without binding, every such finding under one
head-of-file window shared a single anchor and a waiver granted for one
reproduced failure silently suppressed a *different* one. The anchor for
these findings therefore also carries a deterministic fingerprint of the
finding's quoted evidence (`content`, whitespace-normalized): the same
evidence keeps the waiver (a far-field code edit still does not lapse
it), while different failure evidence is a **changed finding** — fresh
and blocking — even when the code at the reported path is unchanged. An
evidence-source finding with no evidence to fingerprint fails closed
(`""`), the same never-waive-blind posture as a missing path or
signature. Reviewer findings without `evidence_source` keep the plain
location fallback unchanged.

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
  the containment, and telemetry makes the quality measurable: the
  reference executor emits actual counts per run (`selector_attempts`,
  `tests_selected`, `probes_authored`, `runs`, `reproduce_results`:
  fail/pass/flake/build/error) through the contract's `telemetry`
  field, which the gate merges into that check's `command_checks` entry
  alongside its own `seconds`, `files`, `findings`, and `warnings`
  counts.
- Waiver sids for test-evidenced findings include the evidence
  fingerprint; a ledger granted before that binding simply stops matching
  those findings (fail-closed fresh judgment) — the ledger is
  machine-local, so no migration is needed.
- Waiver sids keyed to content anchors survive rebases and line movement
  but not in-window code changes; stale sids keyed to old anchoring
  schemes simply stop matching (fail-closed fresh judgment) — the ledger
  is machine-local, so no migration was ever needed.
- The static backstop (rubric thread-safety lens, language rules,
  risk-glob escalation) stays: races the dynamic path misses still have
  a blocking reviewer lane.
- Promotion of a built-in test-check duty is deferred until dogfooding
  defends defaults (a later ADR, per the ADR 0006 bar).

## Addendum (2026-11-21): run-dependent tokens in test evidence

Status: accepted. Amends "Evidence binding" in section 6.

**Problem.** A test finding quotes the run's output, which carries run times
("1 failed in 0.41s"). The evidence fingerprint hashed them, so the same
failure got a new waiver anchor, and a new finding id, on every run; a waiver
never matched twice.

**Decision.** Before a finding with an `evidence_source` is fingerprinted (the
waiver anchor's evidence part) or hashed into its `_finding_id`, its evidence
text is normalized: each line stripped, whitespace collapsed, blank lines
dropped (unchanged), and **only** the run-dependent tokens below replaced by a
fixed marker. Nothing else is touched: line numbers, expected/actual values,
test names and assertion text keep their digits, so a different failure, or a
changed assertion message, is still a different id. Fail-closed rules are
unchanged (no path, no signature, or no evidence, including evidence that is
nothing but run-dependent tokens, yields an anchor that never matches).
Reviewer findings (no `evidence_source`) are not normalized: their ids and
anchors are byte-for-byte what they were. The ledger format and the commands
are unchanged.

**Ignored token types (the exact list; case-insensitive).** Defined in
`_RUN_TOKEN_RES` in `scripts/review-gate.py`; extend the list there and here.

1. ISO-8601 date and time: `2026-11-21T10:11:12.345Z`, `2026-11-21 10:11:12+01:00`.
2. RFC-2822 / syslog-style date and time: `Sat, 21 Nov 2026 10:11:12 GMT`,
   `Nov 21 10:11:12`.
3. A clock time `HH:MM:SS[.fff]` only as a line-start log prefix or inside
   brackets/parentheses (`[10:11:12]`); a clock time elsewhere is left alone.
4. An epoch stamp of 9 to 13 digits behind a `timestamp`, `epoch`, `ts` or
   `time` key (`timestamp: 1763719872`).
5. Pytest `--durations` lines: the leading duration of `0.41s call tests/t.py::x`
   (also `setup`, `teardown`).
6. A duration after `in`, `took`, `taken`, `elapsed`, `duration`, `runtime` or
   `time` (optionally with `:` or `=`): `1 failed in 3.21s`, `Ran 1 test in
   0.002s`, `took 2 seconds`, `Time: 0:00:03`, `elapsed=120ms`. A duration is a
   number with a unit (`ms`, `msec`, `milliseconds`, `s`, `sec`, `seconds`,
   optionally led by `Nh` / `Nm` as in `1m 3s`), or `H:MM:SS[.fff]`. A bare
   number after these words (`assert x in 5`) is not a duration.
7. A bracketed duration: `(0.41s)`, `[12 ms]`, `(0:00:03)`.
8. Elapsed/seconds fields: a double-quoted JSON key containing `elapsed`,
   `duration`, `runtime`, `seconds` or `secs` with a numeric value
   (`"elapsed": 0.41`); a key `elapsed_`/`duration_`/`runtime_`/`time_`/
   `walltime_`/`took_` plus a unit suffix (`s`, `ms`, `sec`, `secs`, `seconds`,
   `millis`) with `:` or `=` (`elapsed_s=0.41`); a plain `elapsed:`, `seconds:`
   or `secs:` with a number.

**Not ignored** (deliberately, they can be part of the failure): bare numbers,
line numbers, dates without a time of day, a clock time in the middle of a
line, single-quoted or `=`-keyed values (`{'duration': 5}`, `sleep(seconds=5)`
are expected/actual values), temp directories, process ids, random seeds,
memory addresses, and durations without a unit. A failure whose evidence varies
in those still gets a new id on each run: that is the conservative side.

**Consequences.** A waiver granted before this change for evidence that
carried a run-dependent token stops matching once and must be granted again
(accepted; the ledger is machine-local, no migration). Evidence that carried
none of these tokens hashes exactly as before and keeps its waiver. `waive
--list` marks each ledger entry whose anchor no finding of the last recorded
verdict has (`NO LONGER MATCHES`) and ends with a count, so a user knows to
re-grant; the marker is advice, never a failure, and is skipped when the
recorded verdict was truncated. The finding id shown by the gate and used by
`waive`/`--dismiss` for a test finding now hashes the normalized evidence too,
so it is stable across runs; reviewer-finding ids are unchanged.

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
