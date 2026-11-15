# ADR 0004 — Coverage rollback switch (`OCR_COVERAGE_V1`) and the `--reset-tip` operator command

## Status

Accepted (2026-11-14). Covers the two new decision/config surfaces the
restack-coverage work (content-keyed coverage + retry/exhausted state machine)
introduced without a recorded decision, following the house rule ADR 0002/0003
established: environment switches and operator-facing commands carry decisions.

- (a) `OCR_COVERAGE_V1=1` — the rollback switch for content-keyed review
  coverage (`scripts/review-gate.py::_coverage_v1`, `scripts/rg_store.py`);
- (b) `review-gate.py --reset-tip <tip>` — the operator command that clears a
  tip's `failed`/`exhausted` run state
  (`scripts/review-gate.py::_reset_tip`).

## Context

### a) `OCR_COVERAGE_V1`: a kill switch for the new join semantics
Verdict records moved to protocol v2 (content-keyed coverage: per-commit
`patch_ids`, a `tip_tree` fast-path key, a `sha_only` list for commits without
a usable patch-id). The restack join — a restack that rewrites every SHA but
no content joins its recorded verdict instead of re-reviewing the branch — is
new behavior in the gate's decision surface: a push may now be allowed with
ZERO reviewer invocations on the strength of previously-reviewed content. New
joins of this kind need an escape hatch the operator can pull without a code
change if the patch-id/tree-hash heuristics ever vouch for work a human would
consider unreviewed.

### b) `--reset-tip`: the one way out of an exhausted tip
The retry/exhausted state machine replaced the old sticky error state: a run
that dies on budget, timeout, crash, unparseable output, or spawn failure is
retried by the next push of the same tip (up to 3 attempts); at the cap the tip
enters `exhausted` and is walled. Without a documented command to clear that
state, an exhausted tip could only be escaped by pushing a NEW tip (or by
hand-deleting state files under `.git/review-gate-async/`, which is an
undocumented internal layout). `--reset-tip` is that command: the documented
equivalent of removing the state file(s) for the tip, and nothing else.

## Decision

a) **`OCR_COVERAGE_V1=1` restores the pre-change SHA-only join end to end.**
The switch is env-only (not a config key, not per-repo), unset by default, and
checked live at join time — it gates the v2-record join and the incremental
(restack-b) range reduction, and makes `rg_store` write the exact v1 record
shape (`content_keyed=False`). It exists as a rollback path, not a supported
operating mode: the recorded decision is the v2 content-keyed behavior, and
the switch is the escape if a defect in it ever authorizes a push it should
not have. Env var (reversible, no cache-identity implications) matches the
repo's other knob conventions (`OCR_INDEX`, `OCR_TELEMETRY`).

b) **`--reset-tip <tip>` clears run state, never verdict state.** It removes
only the async run-state file(s) naming that tip (`failed`/`exhausted`/
still-running bookkeeping), is standalone like `--history` (no review, no
markers, no hook payload), and exits 1 when nothing matched so scripts can
tell "cleared" from "nothing to clear". It is deliberately NOT a way to
dismiss findings: recorded `block` verdicts live in the external verdict
store, untouched by this command, and still bind the tip.

## Consequences

- Changing any of these surfaces now requires updating this ADR (or a
  successor), keeping the docs-checker's `config_surface` family satisfied.
- With `OCR_COVERAGE_V1=1`, a pure restack is re-reviewed in full (the
  pre-change behavior): the rollback trades review cost for the guarantee
  that only exact SHA identity can vouch for a push.
- An exhausted tip is a loud block naming `--reset-tip`; the command clears
  the wall but re-arms the attempt counter, so a genuinely broken
  environment fails again rather than silently allowing.
- `--reset-tip` cannot be used to bypass a real `block` verdict — the verdict
  record in the external store is unaffected, exactly as if the operator had
  removed the state file by hand.

## Alternatives considered

- (a) No rollback switch — rejected: the join allows pushes with zero reviewer
  invocations; shipping that with no operator escape contradicts the repo's
  fail-closed posture for new authority.
- (b) Auto-expiry of `exhausted` state after a TTL — rejected: an exhausted tip
  already means three failed attempts; silently un-walling it re-arms a
  known-bad path without the operator seeing it. The command keeps the
  operator in the loop.
