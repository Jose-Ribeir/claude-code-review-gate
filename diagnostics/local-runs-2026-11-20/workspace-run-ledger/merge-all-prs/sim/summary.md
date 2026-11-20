# Merge simulation summary — claude-code-review-gate (PR #6, #8, #9 → main)

## Full sequence (order pr6, pr8, pr9; method merge)

Target head: 9e865761682b9403a34153c53642c8560556ed9a

| step | part | result | conflicted files | files_changed | stacked_on_previous | contains_later_parts | already_in_prefix | carries_squashed_parts | criss_cross |
|---|---|---|---|---|---|---|---|---|---|
| 1 | pr6 | conflict | CHANGELOG.md | (n/a, listed empty on conflict) | null | [] | false | [] | false |
| 2 | pr8 | skipped (upstream step did not merge cleanly) | – | – | false | [] | null | [] | null |
| 3 | pr9 | skipped (upstream step did not merge cleanly) | – | – | false | [] | null | [] | null |

Simulation exit code: 1. `already_applied_commits`/`noop` false; single merge-base (1), no criss-cross. pr8 and pr9 were never attempted once pr6 conflicted.

## Hypothetical "can later parts land" run (skip step 1 = pr6; order pr8, pr9)

| step | part | result | conflicted files |
|---|---|---|---|
| 1 | pr8 | conflict | scripts/review-gate.py, scripts/rg_config.py |
| 2 | pr9 | skipped (upstream step did not merge cleanly) | – |

Exit code: 1. pr9 was not attempted once pr8 conflicted.

## Per-part isolation
- pr6 alone → conflicts on CHANGELOG.md against main.
- pr8 alone (without pr6) → still conflicts on scripts/review-gate.py + scripts/rg_config.py against main.
- pr9 lands only after pr8 (unchecked here because pr8 conflicts).

## Ancestry (order evidence) — all 6 ordered pairs exit 1
No part is an ancestor of another among part-6, part-8, part-9. Diverged branches; pr9 is built on top of pr8's history (shared SPEC-14/15 commits) but not a descendant.
- part-8 vs part-9 merge-base = 8385a974 (shared SPEC-14 history). part-8 ⊄ part-9, part-9 ⊄ part-8.
- cherry 6→8, 6→9, 8→9: all `+` lines in the sampled head-40 (no `-` = none of A's patches already applied in B).

## Fork points from main (merge-base target ↔ part-N)
- pr6: merge-base 1dacf087, rev-list-count 19 unique commits
- pr8: merge-base 8385a974, rev-list-count 3 unique commits
- pr9: merge-base e2725667, rev-list-count 50 unique commits

(Fork bases differ: parts 8/9 branched off shared SPEC-14 history rather than directly off current main tip; main head in this snapshot = 9e865761.)

## Main CI probe
`gh api .../commits/main/check-runs`: all completed with conclusion `success` — lint, test (3.12), node, test (3.9). Main is NOT failing the same test jobs.