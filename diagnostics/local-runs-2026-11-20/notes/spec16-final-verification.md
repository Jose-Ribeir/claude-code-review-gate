# SPEC-16 final verification

## Delivery

- Approved spec: SPEC-16, local tracker `spec-16-final-review.md`.
- Branch: `spec-15/race-regression-checks`.
- Final commit: `12eee1714a3b4bbab544e5a1d2f0789a5fb131db` (`Remove trailing space in stop fixture`), following the redaction fix in `f5ebb45`.
- Work is committed. `.delegate-kit/` and `.kimchi/` are untracked harness artifacts and were not staged. No push or live provider mutation was performed.

Implementation provides provider-neutral `final-review` operation with GitLab and GitHub adapters, standalone and fully composed linear-stack units, full immutable repository context and independent static lenses, bounded concurrency, required verification and SPEC-15 dynamic probes, finding memory with verified closure, scoped critical stops, stale detection and run ordering, guarded individual/combined publication, versioned SQLite acceptance contract/registry/views, and runtime telemetry.

## Verification evidence

### Full suite

Final command: `python3 -m pytest -q` followed by scoped Ruff over changed Python.

- **2,495 passed, 5 skipped, 1 failed**.
- Ruff on all changed Python: **All checks passed**.
- `git diff --check e272566...HEAD`: passed after a trailing-whitespace correction. All 12 affected stop tests and AST validation pass after that final non-behavioral edit.
- Sole failure: `tests/test_parallel_chunks.py::test_a_limit_stops_the_sibling_but_finished_chunks_keep_their_records`. Its assertion expects chunks `[0,1,2,3]` and received `[0,1,2]`. Isolated worktree execution against unmodified base `e272566` reproduced the same failure, so this is not attributed to SPEC-16. A different full-suite run passed 2,494 tests with that test green, confirming temporal behavior rather than unconditional service regression. It remains a candidate pre-existing bug to investigate separately.

### Deterministic acceptance

Final store `/tmp/spec16-final-certified.sqlite3`, artifacts `/tmp/spec16-final-certified/`.

All **21** registered scenarios matched declared expected states (functional pass, intentional findings/error/stale/incomplete expectations). No scenario is promoted from seeded definition only. Operator smoke `acceptance status` read spec16-05 from the raw SQLite-backed store successfully; SQL count confirmed 21 result runs.

### Real-model acceptance

- spec16-05 unchanged-domain-contract: **findings**, as expected. Acceptance run `645ff0cc-fb29-4602-99e5-866c4e879a2d`. Full checkout identified actual unchanged ADR contradiction under independent compatible/spec lenses; harness was corrected to require evidence-specific findings without demanding one specific lens name.
- spec16-06 comment/testing-standards: **incomplete**, not certified. Latest run reached its documented 1800s full-review timeout: `a666257b-f2f5-4bfb-abc1-68e909de766e`. No timeout was masked as a pass and no expected finding was fabricated.
- spec16-08 existing-regression-output: **findings**, as expected. Acceptance run `f33dc6f8-0e1a-4aab-b41a-c98192480cce`.
- spec16-09 ephemeral-probe-attribution-cleanup: **findings**, as expected. Acceptance run `5bb40595-487c-4bcb-8db7-a36e49b83d90`. Deterministic interleaving and model-authored direct invariant probe reproduced failure; sanctioned probes were removed. Probe diagnostics are opt-in, bounded and sanitized.

Conclusion: deterministic certification is complete; real-model acceptance is **3/4 green with one incomplete** and cannot be called fully green.

## Code review

Parallel Standards and Spec reviews plus follow-up correctness/security checks found actual defects. Substantive fixes include omitted-stack standalone validation, complete numeric/URL stack routing, unsupported and valid inline/quoted/global CI truth, current-branch revision checks, provider project-keyed graph edges, provider-neutral adverse status replacement, warning-only intent/dynamic policy, independently verified source/test-backed closure, untrusted credential/helper isolation, intent/document indexing and evidence quality, process-group cleanup, concurrent lens/STOP correctness, and sanitized diagnostics/persistent worker evidence.

Hard standards gaps (Unreleased changelog and config/operator ADR surfaces) were fixed. Remaining reviewer judgment-call architecture smells include duplicated legacy/current execution wrappers and store responsibility breadth; they were not hidden behind a claim of perfect modularity, and service commands still share compute_verdict as the sole finding-verdict authority.

## Portable database and trust boundary

No Kimchi application database is used or altered. The independent SQLite store holds schema/schema migrations, versioned decisions, acceptance registry/results, revision/run/unit states, findings/anchors, stops, tests, trust and publication history. Any stdlib SQLite client can inspect it; generic external readers do not need Kimchi or review-gate configuration.

Location resolution: explicit constructor/`--store` > nonempty `FINAL_REVIEW_STORE` > `~/.final-review/final-review.sqlite3`. Database permissions are tightened appropriately. Source checkout and coding-tool configuration paths cannot select its location. Reopening and exporting its data was exercised in a subsequent clean process. Same-store sequencing/continuity is deliberately distinguished from fresh-store independent/unenforced operation.

No push or real provider status mutation was performed; publication behavior was exercised through doubles only.
