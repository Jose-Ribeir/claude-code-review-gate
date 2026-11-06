# Part S benchmark: the manual half

Part S (0.11.0, units and caller checks) was specified with a 15-scenario benchmark
(`docs/plans/resume-truncated-chunks.md`, "Tests"). What can be checked without a model
is automated, and runs in the normal suite; what needs a model to judge is here.

## What the suite already proves (no model)

`tests/part_s_scenarios.py` holds the 15 seeded scenarios as base/tip pairs of a small
repository, and `tests/test_segment_scenarios.py` runs each one through the real gate with
the stub reviewer and asserts what the reviewer is **handed**: the changed unit with real
line numbers, the same-file caller as a `dep:<n>` task with its context, the cross-file call
site (widened to its whole unit), the unit index that shows a twin or the other half of a
pair, the pair of changed units that must agree in one chunk. The remaining plan tests
(a 2,000-changed-line file, resume, record pruning, moves, renames, coverage, replay of
duplicate units, CRLF, a `"*"` truncation warning, a pending caller check, a missing
verdict) are in `tests/test_segment.py` and `tests/test_segment_gate.py`.

| # | Scenario | Automated check | Needs a model for |
|---|---|---|---|
| 1 | body-only change returns `None`, same-file caller dereferences | unit + task + caller context | noticing the dereference |
| 2 | same, caller in another file | impact site, widened to the caller's whole function | same |
| 3 | new `TimeoutError`, caller catches only `ValueError` | task, caller context shows the `except` | same |
| 4 | parameters reordered, three callers in two files | two tasks + the cross-file site | same |
| 5 | default `retries=3` -> `0` | unit diff + task | same |
| 6 | `def` -> `async def`, caller does not `await` | unit diff + task | same |
| 7 | module constant changed | changed region delivered; **no caller edge to a constant** | the bug (documented limit) |
| 8 | rename, same-file and cross-file callers | the pair, a task under the OLD name, the renamed-symbol site | same |
| 9 | sibling fix (one twin fixed) | the changed unit; the twin is in the unit index | the bug (documented limit) |
| 10 | lock pairing | the changed half; the other half is in the index | the bug (documented limit) |
| 11 | serializer and deserializer both changed | both units in one chunk, with their call edge | agreeing on the format |
| 12 | removed dataclass field still read elsewhere | the class unit + the importing file as a site | the bug (`cfg.timeout` is an attribute read, not a name reference) |
| 13 | resume convergence | automated end to end (budget kill, resume, nothing reviewed twice) | - |
| 14 | re-push after a prepended 50-line docstring | every function replays, findings move by exactly 50 | - |
| 15 | TypeScript variants of 1 and 4 | regex callers, capped at four | noticing |

Scenario 14 differs from the plan in one detail: the prepended docstring is text that was
not there before, so its own (one or two) regions are reviewed in one call; every function
is replayed. The plan said "zero calls".

## Running it with a real model

1. Build a throwaway repository per scenario:

   ```python
   import sys; sys.path.insert(0, "tests")
   import part_s_scenarios as ps
   # for each sc in ps.S: init a repo, commit sc["base"] and push it to a bare remote, commit
   # sc["tip"], then push with the gate installed. `ps.seed_repo(work, sc, commit)` does the commits.
   ```

   The files are small: force them into units with a tiny limit by editing the module constant
   (`_PRECOMPUTED_MAX_LINES = 0` in `scripts/review-gate.py`), or pad `big.py` with ~1,600 lines.
2. Run each scenario three ways, three times each:
   - **whole-file**: `OCR_SEGMENT=0` (the 0.10.0 review, Part B limits);
   - **Unit**: units without caller checks (comment out the `_plan_deps` call in a scratch copy,
     or read the verdicts of the `unit_diff` items only);
   - **Unit+**: the default.
3. Record whether the seeded bug was reported (a finding at the seeded line), and the `review`
   calls' count and tokens from `$CLAUDE_PLUGIN_DATA/review-gate-debug.log`.

## Ship criteria (from the plan)

- Unit+ at least as good as whole-file on 1-6, 8, 11 and 13-15; scenarios 11, 13 and 14 must be 3/3.
- On 7, 9, 10 and 12, Unit+ at least as good as today's large-file baseline; the gaps are the
  documented limits above.
- **Historical replay**: replay the blocking findings in `.git/review-gate-findings.jsonl` of a real
  repository before and after: every past blocking finding reproduces in at least 2 of 3 runs, no
  blocked push becomes a pass, and the warn/pass sample gains under 20% new blocks.
- **Cost**: median calls no higher than today; p90 tokens per chunk at most 1.5x.
- **Reflexive-`ok` check**: if `dep_verdicts` are 100% `ok` on the seeded bugs, the prompt is being
  ignored: fix `agents/code-reviewer.md` ("Units, context and tasks") before shipping.

None of this has been run against a model yet; the automated half is green, the manual half is
open.
