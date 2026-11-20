# Local run diagnostics — 2026-11-20

This bundle preserves local review-gate run output for troubleshooting. It contains evidence only; no product source or test expectations are changed, and no tests were rerun for this bundle.

Base revision when collected: `4f14db8` (`origin/main` at collection time). The source logs describe older revisions and runs; consult each record for its own SHA and timestamp.

## Start here: failure index

### 1. Live pre-push reviews timed out without a verdict

Two `spec-15/race-regression-checks` pushes ended in `state=failed`, `reason=review`:

- A 600-second timeout with three attempts: `live-state/async/cc513703ca1cbd06/1dacf08767c6e197-673b61d161442b1e8a74525d10093e9fec136782.json`.
- A 1200-second timeout with two attempts at tip `12eee1714a3b4bbab544e5a1d2f0789a5fb131db`: `live-state/async/cc513703ca1cbd06/e2725667957b1651-12eee1714a3b4bbab544e5a1d2f0789a5fb131db.json`.

The latter supervisor log is empty. The records establish timeout and blocked push behavior, but **do not establish why the reviewer stopped making progress**. See `notes/spec16-prepush-failure.md` for the original operator record.

### 2. SPEC-16 has one incomplete real-model scenario and one intermittent test failure

`notes/spec16-final-verification.md` records:

- Full suite: **2,495 passed, 5 skipped, 1 failed**.
- Failure: `tests/test_parallel_chunks.py::test_a_limit_stops_the_sibling_but_finished_chunks_keep_their_records`; expected chunks `[0,1,2,3]`, received `[0,1,2]`.
- The same failure reproduced on isolated base `e272566`, while another full-suite run passed it. This supports a pre-existing timing-sensitive defect rather than a SPEC-16-only regression, but it remains unresolved.
- Deterministic acceptance: **21/21** matched expectations.
- Real-model acceptance: **3/4 green**; `spec16-06-final-only-comment-testing-standards` remained incomplete after its 1800-second timeout.

`notes/spec16-verification.md` also records an earlier diagnosis of 14 serial static calls taking approximately 36–54 minutes, followed by bounded-concurrency fixes. Treat that as historical context, not proof that every later timeout has the same cause.

### 3. SPEC-15 waiver and evidence-selection runs are inconsistent across attempts

The short logs are under `acceptance/spec15/`; complete per-step transcripts are in each simulator's `results.json`, with async state and verdicts below its `rg-state/`.

| Run | Result | Evidence |
| --- | --- | --- |
| `proof-A-F.log` | Scenario C expected a waived re-push to pass but remained blocked; all required waiver messages were absent. | `simulators/sim-scratch-spec15-proof/results.json` |
| `final-A-F.log` | Scenario C stopped with a harness error: no recorded finding on `tests/test_feature.py`. A and B passed. | `simulators/sim-scratch-spec15-final/results.json` |
| `final-C-D-proof.log` | C and D both passed. This is an important comparison point for the earlier waiver failures. | `simulators/sim-scratch-spec15-CD-proof/results.json` |
| `final-proof-A-F.log` | C passed, but D's initial boundary-regression step unexpectedly passed and lacked the existing-test finding. A later step blocked, so the overall outcome was `block` while the scenario still failed its assertions. | `simulators/sim-scratch-spec15-final-proof/results.json` |
| `final-C-D-F.log` | C passed; D did not reach review because `git commit` exited 1 with no captured stderr. | `simulators/sim-scratch-spec15-CDF/results.json` |
| `final-D-E-F.log` | All three scenario expectations passed, but `target_untouched=false` failed the run's isolation check. | `simulators/sim-scratch-spec15-DEF/results.json` |

These are not all the same failure. Separate reviewer/waiver behavior, scenario orchestration, and fixture/setup investigation before assigning a common root cause.

### 4. SPEC-14 dogfood scenarios passed, but the isolation check failed

`acceptance/spec14/dogfood-run.log` and `acceptance/spec14/dogfood-results.json` show both scenarios meeting their expected outcomes while `target_untouched=false`. The `==== FAILURES RECORDED ====` trailer therefore does not mean either scenario's expected push outcome failed; the recorded failure is the original-repository contamination check.

### 5. Merge-validation and CI records are historical evidence

`workspace-run-ledger/merge-all-prs/` contains the complete local merge-validation ledger for PRs #6, #8, and #9, including raw CI output, merge simulations, resolutions, and final state.

Notable historical entries include the test git-identity defect found in simulation, merge conflicts and arity-resolution risks, the pre-existing `parallel_chunks` flake, and two post-merge test jobs observed stalled in `in_progress`. The run-state file records those PRs as merged. Do not interpret these old branch results as current CI status for this diagnostics branch.

### 6. Focused post-fix checks passed, with earlier redaction findings documented

- `notes/focused-review.md` records two credential-redaction defects from an earlier focused review.
- `notes/focused-verification.md` records the follow-up result: 101 + 62 focused tests passed and the verdict was `ALL_PASS`.

Keep the earlier findings paired with their later verification; they are not presented as unresolved failures in this bundle.

## Bundle layout

- `acceptance/`: all discovered SPEC-14/SPEC-15 acceptance `.log` files, the hook-install log, dogfood result/state, and simulator run summaries.
- `simulators/`: every discovered simulator supervisor/debug `.log`, `results.json` transcript, async JSON state, verdicts, and waivers for the target project.
- `live-state/`: target-repository records from `~/.local/state/review-gate`, including the actual failed async states and matching verdicts.
- `workspace-run-ledger/`: complete `.delegate-kit` merge-validation and port-planning ledgers from the workspace.
- `repo-run-metadata/`: the repository's local `.delegate-kit` SPEC-16 scheduling/partition metadata.
- `notes/`: existing human-readable failure, review, and verification records.

## Scope and exclusions

The bundle contains 349 files (about 2.8 MB), including this index, and preserves raw records rather than only edited excerpts. Empty supervisor logs and three zero-byte `checks_required.json` command outputs are preserved intentionally as evidence. Scratch `origin.git/`, `work/`, `rg-state/clones/`, caches, and unrelated repositories' global review-gate state were deliberately excluded. Those directories contain cloned source and bulky repositories, not additional target-run log evidence.

Absolute local paths remain in the raw evidence so references can be correlated. Existing simulator fixtures can contain intentional placeholder credential strings. A scan for common live-token and private-key formats found no matches; this was a targeted check, not a guarantee that every possible secret format is absent.

## Suggested investigation order

1. Use the successful `CD-proof` run as the control against the failed waiver/re-push transcripts; compare finding IDs, anchors, waiver state, and exact revision sequence.
2. For `final-proof-A-F`, determine why the initial source-boundary step passed and why only the later test-evidence step blocked.
3. Treat `final-A-F` and `final-C-D-F` as setup/orchestration failures unless deeper evidence shows otherwise; neither produced a useful review verdict for its failing step.
4. Inspect the empty live supervisor log and process/runtime telemetry for the 1200-second timeout before increasing timeouts again.
5. Reproduce `parallel_chunks` under controlled load and timing rather than attributing it to unrelated SPEC-16 changes.
6. Investigate every `target_untouched=false` result as an isolation defect, even when scenario expectations passed.
