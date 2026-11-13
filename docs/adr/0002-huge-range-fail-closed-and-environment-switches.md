# ADR 0002 — Unevaluable huge-range counts fail closed; environment switches carry decisions

## Status
Accepted (2026-11-13). Covers two config-surface changes the docs-checker flagged as
lacking a recorded decision.

## Context

### a) Huge-range refusal on unevaluable counts
`_refuse_huge_range` refuses an unbounded push (`base == _EMPTY_TREE`, i.e. no remote ref
bounds the range — unseeded remote or first push of a new repo) whose commit count exceeds
`OCR_FULL_HISTORY_LIMIT`. Before this change the guard fired only when the count was
successfully computed **and** exceeded the limit; a `git rev-list --count` failure fell
through silently, and the push was reviewed whole.

### b) `OCR_INDEX` environment switch and the event-graph index
The gate builds a lazy `.ocr/index` event-graph (`scripts/ocr_index.py`) that every gated
review consults for coupling/impact context. The switch is the disable knob
(`os.environ.get("OCR_INDEX", "1")`), default ON.

Both change the gate's decision surface (when a push is refused; what context feeds the
verdict) without any recorded decision, so the docs-checker's `config_surface` family
correctly flagged them.

## Decision

a) **An unevaluable count is treated as an unbounded count: the push is refused** with the
huge-range reason and the same `_bypass_hint(mode)` escape as every other refusal. The
guard exists to refuse pushes it cannot show to be bounded; a push it cannot count is
exactly that. Fail-closed here is consistent with every other "cannot see what this is"
case in the file (unparseable payloads, ambiguous repos, corrupt verdict records).

b) **`OCR_INDEX` ships default ON with an env-only disable**. The index is derived data
(rebuilt on demand, never authoritative) and cheap enough to be the default; an env var —
not a config key, not per-repo — keeps the surface minimal and matches the repo's other
aggregation knobs (`OCR_TELEMETRY`, `OCR_RUN_BUDGET`). Disabling the index degrades the
context spotter to its pre-index behavior; verdict authority never depends on it.

## Consequences

- A corrupt command/exec environment that breaks `git rev-list --count` blocks unbounded
  pushes instead of reviewing more history than intended; the refusal text names
  `OCR_FAIL_OPEN=1` as the documented escape.
- Operators must set `OCR_INDEX=0` to skip index builds in constrained environments;
  doctor (`ocr_doctor.py`) reports index health under both settings.
- Future config-surface changes to either surface require updating this ADR (or a
  successor), keeping the docs-checker satisfied.

## Alternatives considered

- (a) Fail open on unevaluable count — rejected: nullifies the SPEC-09 guard exactly where
  it matters (the genuinely unbounded pushes).
- (b) Per-repo config for the index — rejected: adds a settings dimension before any user
  asked for one; env var is reversible and has no cache-identity implications (the index
  is derived, never fingerprinted).
