# Extra merge-sequence simulations (recorded exit codes + per-step results)

Both runs use the merge-validator simulate-sequence.sh against target
`refs/merge-validator/target`.

## Order: part-9 then part-6  (simulation-9-6.json, exit=1)

- Step 1: part-9 (merge) -> **clean** (no conflicts)
- Step 2: part-6 (merge) -> **conflict**, conflicted file: `CHANGELOG.md`
  (Auto-merged; CHANGELOG.md content conflict; README.md / review-gate.py /
  rg_config.py / rg_store.py auto-merged cleanly)

## Order: part-9 then part-8  (simulation-9-8.json, exit=1)

- Step 1: part-9 (merge) -> **clean** (no conflicts)
- Step 2: part-8 (merge) -> **conflict**, conflicted files:
  `scripts/review-gate.py`, `scripts/rg_config.py`
  (docs-checker.md, adr 0006, test_docs_gate.py, etc. auto-merged cleanly)

Both orders fail at step 2 (exit 1), so part-9 is not enough to make the
later step land clean on target. Note: these are target-based merges of each
pair; part-9 alone merges cleanly onto target, but part-6 and part-8 each
still conflict when stacked after part-9.

Any further investigation (e.g. 6-then-8, 8-then-6) should reuse the existing
simulation.json / cherry files in this directory.