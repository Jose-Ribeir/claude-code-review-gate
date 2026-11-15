# Architecture split proposal — `scripts/review-gate.py`

**Status:** future work (t28 follow-through) · **Origin:** cycle3-299 findings
`[medium 0.9] review-gate.py:1 Divergent Change / Shotgun Surgery` and
`[medium 0.9] review-gate.py:7986 _SegState Large Class` · **Disposition:**
structural debt is accepted as a single module for now; both findings are
formally dismissed (see `.kimchi/docs/disposition-299.md`), and this document
is the ticketed plan for the eventual split.

## Why now is not the time

`review-gate.py` is an 11 642-line single module hosting at least nine unrelated
subsystems (env/model config, git plumbing, hook payload parsing, verdict JSON
rescue, process supervision, worktree management, ledger + resolutions,
intent/tickets, segmentation + chunk scheduling, reporting). A full split is
correct but **dangerous in one move**:

- The persisted ledger under `.git/review-gate-ledger/` replays recorded
  verdicts by fingerprint. `agents/code-reviewer.md`, the module's review
  payloads, and _finding/class identity all participate in fingerprints. A
  wholesale module move changes import structure and (if done carelessly) the
  exact report text and ordering — **orphaning every cached unit and recorded
  verdict**, i.e. one full re-review per active branch and a dead ledger.
- ~300 module-level defs are referenced by dozens of tests via
  `rg = importlib...` seams; moving everything at once invalidates the test
  harness the same week the fixes from t23–t26 landed.

So the split happens **one leaf module at a time**, keeping review fingerprints
stable per move (see "Roll-out order" and "Fingerprint stability rules").

## Seam inventory (existing `# --- ... ---` banners)

The module is already section-bannered; the banners ARE the split plan.
Approximate anchors at dismiss time (lines drift; banners are the stable refs):

| Banner (anchor line) | Subsystem | Suggested module |
|---|---|---|
| (top – 262) env/model config, provider guard, timeouts | configuration | `rg_config_env.py`* |
| `# --- C3: the deny-extension startup canary` (263) | canary probe | `rg_canary.py` |
| `# --- asynchronous review state` (367) → `# --- commit intent` (445) | async state, checkpoints, commit intent | `rg_async_state.py`, `rg_intent.py` |
| `# --- review ledger` (476) → 523 utilities | history/markers, misc helpers | already partially `rg_*.py`; extend `rg_history.py` |
| `# --- what does this command push?` (1907) | push classifier, `-C`/subshell vetting, `_find_kimi`, git plumbing | `rg_push.py` |
| `# --- SPEC-06 ... improvised report families` (2514) | verdict JSON rescue/normalize | `rg_verdict_rescue.py` |
| `# --- shared worker-invocation plumbing` (3276) | worker cmd assembly, scrub | `rg_worker.py` |
| `# --- the review runs elsewhere` (3572) | state file, supervisor, inline join | `rg_supervisor.py` |
| `# --- risk gate (t10)` (3914) | lane selection | `rg_risk.py` |
| `# --- chunking helpers` (5077), `# --- chunk timeouts` (6828), `# --- precomputed diffs` (7015), `# --- chunk-aware run budget` (7038) | clustering/chunk planning | `rg_chunks.py` |
| `# --- review ledger` (5394) | ledger read/write, resolutions, dismiss | `rg_ledger.py` |
| `# --- impact analysis and known defects` (6125) | impact graphs, known defects | `rg_impact_gate.py` |
| `# --- architectural-change flags` (8038) | arch flags | fold into `rg_chunks.py` or `rg_context.py` |
| `# --- big files in stable units ... Part S` (8761) region incl. `_SegState` (7986) | segmentation + run records | see _SegState decomposition below |
| `# --- inside the review` (11305) → EOF | reviewer command guard, report render, main/CLI dispatch | `rg_report.py`, `rg_cli_main.py` |

\* new files; `scripts/` already carries the `rg_*.py` convention
(`rg_tickets.py`, `rg_config.py`, `rg_jira_fetch.py`, …).

## `_SegState` decomposition

_SegState (banner anchor 7986) is a 928-line class with 35 methods and 18
instance fields covering blob I/O, unit pairing, caching, chunk planning, and
result writing for one run — five responsibilities in one object. Target shape:

1. **`SegRunState`** (plain record, `dataclasses.dataclass` or `NamedTuple`):
   the 18 fields verbatim, zero behaviour. Everything below takes/returns it.
2. **Segmenter** (`rg_segment.py`): blob I/O + unit pairing — the `build/convert`
   family (`_build`, `convert_truncated_carries`, truncation owns).
3. **SegCache** (`rg_segcash.py` or inside `rg_segment.py`): the cached-unit
   lookup/read plus `_is_truncated_record` interplay.
4. **ChunkWriter** (fold into `rg_ledger.py`): `finalize`, `_write_run_records`,
   `_clear_timeout_mark` — the result-writing half.

Alternative flagged by the finding and still on the table: move the whole
Part S subsystem next to `ocr_segment.py` instead of splitting it. Decide at
t28 kickoff; the record-first decomposition above is compatible with either.

## Roll-out order (leaf modules first)

Each step is one commit, reviewable on its own, with the full suite green:

1. **Pure leaves with no ledger contact**:`rg_verdict_rescue.py` (SPEC-06),
   `rg_push.py` (push classifier), `rg_canary.py`, `rg_risk.py`. These change
   what the reviewer is *asked*, not how findings are identified.
2. **State/supervision**: `rg_async_state.py`, `rg_worker.py`,
   `rg_supervisor.py`, `rg_intent.py`.
3. **Ledger layer** (`rg_ledger.py`, `rg_history.py`) — move via
   re-export shims first (`from rg_ledger import *` inside review-gate for one
   release) so `_finding_id` source text is untouched during the move.
4. **Chunk planning** (`rg_chunks.py` incl. timeouts, run budget, arch flags).
5. **_SegState decomposition** last (record → collaborators, per above), once
   the surrounding seams are gone and its 35 methods have a smaller blast radius.
6. Final: shrink `review-gate.py` to CLI dispatch + `_main_inner` wiring
   (`rg_cli_main.py`), delete re-export shims.

## Fingerprint stability rules (apply to every step)

- Do **not** edit moved function bodies in the same commit as the move —
  move verbatim, clean up later. Report text, finding ids, and the reviewer's
  `paths_file`/manifest bytes must be byte-identical after each move.
- Keep `agents/code-reviewer.md` and prompt templates out of split commits;
  they are part of unit fingerprints (see CHANGELOG 0.10.0 "Every cached review
  is invalidated once" for the cost of ignoring this).
- Run `tests/test_resolution_validity.py`, `tests/test_stacked_base.py`,
  `tests/test_ledger_scenarios.py` on every split commit; a fingerprint drift
  shows up there first.

## The explicit warning

A single big-bang split **orphans the ledger**: every recorded verdict's
replay/invalidation keys, the resolution store under
`.git/review-gate-ledger/<fp>/resolutions/`, and the memory dismissals in
`.ocr/memory.jsonl` (which match on category + signature line + path glob, all
anchored to `scripts/review-gate.py`) would silently stop matching future
findings. Dismissals recorded during the 2026-11-12 wave — including the two
this document justifies — would become inert exactly when the module they
excused disappears. Accept that trade deliberately at t28: re-record or migrate
`.ocr/memory.jsonl` path globs as part of the split, and expect one scripted
re-review wave per active branch after step 6.
