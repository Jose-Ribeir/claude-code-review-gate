# Merge validation: Jose-Ribeir/claude-code-review-gate · merge-all-prs: **NO-GO as-is → GO-WITH-RISKS conditional on the authorized resolution plan**

Validated against the SHAs below. Re-run before merging/enqueuing if any head or target SHA moves.
Final target: `main` @ `9e86576` · Platform: GitHub · Ledger: `/Users/jose_ribeir/work/code-review/.delegate-kit/runs/merge-all-prs`

## Sequence

| # | Part | Head | Source → target (lands on) | Train / queue | Sim | Verdict |
|---|------|------|----------------------------|---------------|-----|---------|
| 1 | !9 (spec-15/race-regression-checks) | `12eee17` | feature → main (main) | none | clean | GO-WITH-RISKS |
| 2 | !6 (integration/push-gate) | `895abdb` | feature → main (main) | none | conflict CHANGELOG.md | GO (conditional) |
| 3 | !8 (integrate/spec-14-push-findings) | `1cd1149` | feature → main (main) | none | conflict review-gate.py + rg_config.py | GO (conditional) |

Order: #9 → #6 → #8, chosen from conflict topology; all three conflict sets proven under this order (order-independent conflict sets).

## Blockers

- **[SEQ-06] pr8**: is a draft; resolves by marking ready (user-authorized) before its merge. pr9/pr6 PASS.
- **[SIM-01] pr6**: conflicts CHANGELOG.md (adjacent appends, trivial). Resolve by concatenating both entries. pr8 also conflicts review-gate.py (overlapping logic, 4-tuple vs 2-tuple arity) + rg_config.py (docstring only). pr9 clean.
- **[GATE-02] pr9/pr6/pr8**: pr9 MERGEABLE (unstable); pr6 and pr8 DIRTY — resolve via authorized resolution.

## Risks

- **[SIM-05 / DEP-08] pr9**: main goes CI-red after pr9 lands — `tests/test_final_materialize.py::test_materializes_target_from_a_separate_repository_and_cleans_failures` fails deterministically (git commit exit 128 "Author identity unknown", both Pythons); merging main will not heal it. → Decision required: fix the test's git-identity bug on the branch before merging, or merge as-is accepting red CI until a follow-up.
- **[DEP-07 O1-4] pr8**: intra-pr8 pre-existing bug — scopes from pre-sweep matched; NOT a merge artifact, reported only, out of scope. (impact/overlap-1.json)
- **[DEP-07 O1-2] pr8**: rg_config.py docstring doc-loss — take main's text during resolution.
- **[DEP-03] pr8**: `_run_one_command_check` arity collision 4-tuple (main) × 2-tuple (pr8) — hand-port pr8's realpath guard into the 4-tuple shape (impact/pr8.md finding 1).
- **pre-existing**: pr6 residual `parallel_chunks` flake; branch CI failures healed by merging main.

## Unknowns (not verified, not passed)

- None material. Note: pr8 has no CI signal on its branch (py_compile clean); risk is mitigated by a local test run during resolution (specified as part of the resolution plan).

## Simulation

`9e86576` → pr9 merge-commit clean → pr6 merge-commit (conflict CHANGELOG.md) → pr8 merge-commit (conflict review-gate.py + rg_config.py).
Conflicts: CHANGELOG.md (pr6), review-gate.py + rg_config.py (pr8), none (pr9). Squash-carried parts: none. Criss-cross: no.
Hypothetical runs: none (all three parts are in the set).

## Production impact per prefix

| After part | Deployable alone? | Notes (migrations, flags, contracts, config) |
|------------|-------------------|----------------------------------------------|
| 1 (pr9) | risky (CI-red) | self-contained harmless wiring; lands a deterministically CI-red new test (git-identity bug) (impact/pr9-pr6-remaining.md) |
| 2 (pr6) | yes | safe alone; CHANGELOG concat is trivial |
| 3 (pr8) | risky (arity hand-port) | realpath guard hand-port + 4-tuple symlink-escape test required to be verifiable (impact/pr8.md) |

## Recommended actions (for a human, in order)

1. Decide: fix pr9's test git-identity bug on its branch before merging, or merge as-is accepting red CI until a follow-up.
2. Merge #9 with a merge commit.
3. Resolve #6's CHANGELOG (concat both entries) and merge.
4. Mark #8 ready, hand-port the realpath guard into the 4-tuple, add the symlink-escape test, run tests locally, push resolution, merge.
5. Verify main's CI after each landing.