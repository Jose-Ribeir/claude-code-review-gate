# Impact review (2nd pass) — #9 and #6 remaining questions
Scope: static, read-only (git show/diff/grep on refs only). Refs: target=9e86576 (tree=e272566), part-9=12eee17 (base e272566), part-6=895abdb (base 1dacf08).

## Q1 — Failing tests (from ci/digest.md)

### #9 head (branch spec-15/race-regression-checks, run 44358089609)
- Failing (both 3.9 + 3.12): `tests/test_final_materialize.py::test_materializes_target_from_a_separate_repository_and_cleans_failures` — `git commit -q -m 'fork change'` exit 128, "Author identity unknown".
- File is NEW in #9's diff (`git diff --name-status e272566..part-9 -- tests/test_final_materialize.py` → `A`); module `scripts/rg_final_materialize.py` also new (diff hunk @@ -0,0 +1,269).
- Cause is #9's own test bug:
  - `part-9:tests/test_final_materialize.py:32-35` `_commit` runs plain `git commit` (no `-c user.*`, `_git` at :21-26 passes no env → inherits CI env);
  - identity is configured only in the upstream repo (:42-43) and the unrelated repo (:226-227), never in the fork `source` clone created at :200-204 (`git clone` does not propagate user.*), so the commit at :204 fails when no ambient identity exists;
  - CI has none: `ci/failed-6.txt` shows the runner overrides HOME to a temp dir whose global config contains only `safe.directory`.
- Verdict: **#9 CAUSES it** (deterministic in CI). Merging main does not heal it — the file exists only on #9. Needs a fix (set user.* in the clone or pass `-c` in `_commit`).

### #6 head (branch integration/push-gate, run 43923470682)
All three failure classes are stale-base artifacts: #6 forked at 1dacf08, which predates main's fix commit `e272566 "tests: repair cmdshim and cleanup compatibility across platforms"` (2026-11-17; #6's failing run is 2026-11-16):
- `test_cmdshim.py::test_unwrap_follows_an_npm_shim_to_the_native_exe` (3.12+3.9): fixed by e272566 in `scripts/ocr_cmdshim.py` `unwrap()` — separator normalization added (e272566:scripts/ocr_cmdshim.py:46-53). #6's diff (1dacf08..part-6) touches neither `scripts/ocr_cmdshim.py` nor `tests/test_cmdshim.py` (not in sim/files-6.txt; `git diff 1dacf08..part-6` stat lists only the 15 known files).
- `test_sim_harness.py::test_drive_push_transport_failure_is_a_harness_error_not_a_block` and `::test_run_scenario_propagates_transport_failures_as_harness_errors` (3.9): `TypeError: rmtree() got unexpected 'onexc'` from `tests/conftest.py:97` — fixed by e272566 in `tests/conftest.py:98-101` (version-gated onexc/onerror). #6 does not touch `tests/conftest.py` or `tests/test_sim_harness.py`.
- `test_parallel_chunks.py::test_a_limit_stops_the_sibling_but_finished_chunks_keep_their_records` (3.9 only, one job): asserts stub calls == [0,1,2,3] but got [0,1,2] — the 4th chunk's worker was killed before its trace write. The test's own docstring flags kill-pipeline latency under load (part-6:tests/test_parallel_chunks.py, hunk after `def test_a_limit_stops_the_sibling...`: "The 40 s bound tolerates kill-pipeline latency under load (was 30 s sleep / 25 s bound)"). #6's diff does not touch the chunk scheduler/limit-kill path; its only touch to the executed review path is a 2-line prompt-note append (`_depth_prompt_note()` → "" at default depth) at hunk @@ -3832,6 +4193,8 in scripts/review-gate.py. 3.12 passed the same test, and the test file is byte-identical between 1dacf08 and e272566 → timing/load sensitivity on a slow 3.9 runner, not a #6 regression. Merge with main (green tree) replaces nothing here (no overlap) and is expected to be green; residual flake risk is pre-existing.
- Verdict: **#6 causes none of its branch's failures**; merging #6 into main (which contains e272566) FIXES the cmdshim + onexc/sim_harness failures; parallel_chunks is unrelated/pre-existing timing sensitivity.

## Q2 — Standalone dependence

#9 lands first (on e272566 tree):
- Imports: all `rg_final_*` modules are new in #9; their external imports (rg_store, rg_config, rg_ticket_fetch, rg_tickets, rg_jira_fetch, ocr_verdict, ocr_profile) all exist at e272566 (`git ls-tree e272566 scripts/`). No import of `rg_mr_consult` (#6-only) or any #8-only file (`#8` only modifies ocr_docsquality.py / review-gate.py / rg_config.py / agents/docs-checker.md, all pre-existing).
- rg_store API: #9 calls only `clones_dir()`, `remove_review_clone()`, `store_root()` (part-9:scripts/rg_final_materialize.py:233,263,269; rg_ticket_fetch.py:270) — all defined at e272566:scripts/rg_store.py:284,414,37. #6's new rg_store functions (`prune`, `maybe_prune`, `invalidate_ref`, `record_bypass`, from `git diff 1dacf08..part-6 -- scripts/rg_store.py`) are unused by #9.
- Config keys: #9's rg_config.py delta is validation-only (accept `required` in checks entries, hunk @@ -321); #9 reads `load_settings()["checks"]`, provided by main (e272566:scripts/rg_config.py:97,133). No key defined only in #6/#8 is read. #8's rg_config.py diff is docstring-only (no keys at all).
- Agents: #9 modifies existing agents/code-reviewer.md and agents/docs-checker.md (both in e272566 tree); references no #6/#8-only agent file.
→ #9 is fully self-contained against main.

#6 lands second (after #9):
- #6's added code imports rg_mr_consult (own), and references agents/deep-reviewer.md + agents/docs-checker.md (part-6:scripts/review-gate.py:732-736) — both exist on main (e272566 agents/ ls-tree). No #8-only symbol: `git diff 1dacf08..part-6 -- scripts/review-gate.py | grep ocr_docsquality` → no added call sites.
- The only config key #6 adds is `review.depth` (rg_config.py:288 in part-6), consumed by #6's own `_depth_prompt_note`. #8 defines no new keys.
→ #6 has zero dependence on #8.

## Q3 — Partial feature

- #9 without #6/#8: **harmless**. The whole-stack final review is wired exclusively as standalone operator subcommands dispatched at the very top of `main()` — part-9:scripts/review-gate.py:14396-14404 (`final-review` → `rg_final_review.main`; `contract`/`acceptance`/`final-report` → `rg_final_views.main`), with comments stating "standalone operator command, never a pre-push mode" and "Nothing attaches to the push command line; --no-verify stays untouched". Push-hook behavior is unchanged; without #6/#8 the feature is simply unexercised, not broken. (The only red is the Q1 test bug, which is #9-internal.)
- #6 without #8: **harmless**. #6's universal local-only push gate is self-contained (own rg_mr_consult + rg_store extensions + review.depth key) and calls no #8 surface (grep above). Missing #8 only means custom-checks/docs-quality duties stay at their main behavior — incomplete, non-breaking.

## Verdicts
- #9: risky-alone — wiring is safe/inert, but it lands a deterministically CI-red new test (test_final_materialize git-identity bug) that merging main will not heal.
- #6: safe-alone — all branch CI failures are stale-base artifacts healed by the merge with main; only residual is the pre-existing timing sensitivity of test_parallel_chunks.
