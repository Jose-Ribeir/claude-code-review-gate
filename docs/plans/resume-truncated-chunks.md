# Plan: large pushes must converge, then get fast

Status: Parts A, B0, B, S and C are implemented (0.9.5 to 0.12.0; Part S deviations are listed in
the 0.11.0 CHANGELOG entry and `docs/benchmark-part-s.md`, Part C's in the 0.12.0 one). Reviewed by a
super-thinker on 2026-10-01; its fixes are applied below. Merges the earlier 0.9.5 truncated-chunks draft with the speed-up plan.

Baseline: `main` at `101d213` (the `-z` / `--no-abbrev` fixes, `3dff6bb` and `101d213`,
are unreleased). Line anchors are approximate: locate code by symbol name.

## Summary

| Release | Part | What it fixes |
|---|---|---|
| 0.9.5 | A | **Convergence.** Truncated reviews carry on resume, and a chunk that always times out is split instead of looping. |
| 0.9.6 | B0 | **Time metrics.** Every run logs where the time went. No behaviour change. |
| 0.10.0 | B | **Speed and full review up to ~1,500 lines.** Python writes the diffs, the reviewer reads them directly (no retyping), and the size limits go up. One prompt edit. |
| 0.11.0 | S | **Big files in stable units, with cross-function checks.** Unit-level caching, plus caller verification. |
| 0.12.0 | C | **Parallel chunks.** Concurrency 2, one worktree per slot. |

**Rules for every release:**
- One environment flag restores the previous behaviour, and the old path is kept for one
  release.
- CHANGELOG and README are updated, `plugin.json` is bumped, and
  `scripts/sync-local-install.py` is run.
- The replay benchmark runs before and after (see "Risks").

## What we know

- **Real case:** `feat/llm-provider-baseline` in thyra-ai, tip `56bf432`, 40 files in
  10 chunks of about 10 minutes each (9–13 minutes observed). Three runs, none finished:
  - run 1: c0–c1, then the usage limit;
  - run 2: c0–c5, then `OCR_RUN_BUDGET` (3600 s);
  - run 3: c0–c1, then the machine slept.

  Every run restarted at c0.
- **Why c0–c4 never stick.** They are large single files (49–58 KB, 760–900 changed
  lines). The skill truncates any per-file diff over 400 lines or 16 KB (SKILL §3).
  `_write_run_records` writes no record for a truncated file, and none for the whole chunk
  when the truncation warning names no file (`"*"`).
- **Resume is per file.** `_plan_review` re-forms chunks from the files that have no
  record. Ledger records are already written after each chunk.
- **Telemetry, last 7 days.** `review` calls average **314 s**; `resolve` and `recheck`
  average about 60 s.
- **One archived small review:** 11 turns and 67 s. 7 Read, 3 Bash (git), 3 Grep and
  1 Agent.
- **The reviewer has only `Read, Grep`**, so the orchestrator retypes every diff into the
  Agent prompt: about 20–30k output tokens for a 1,500-line diff. Suspected to be the main
  cost; B0 measures it.
- **Timeouts:** a chunk timeout is not retried and saves nothing.
- **Version skew:** `plugin.json` says `0.9.1`, but the CHANGELOG has 0.9.2–0.9.4 and an
  `[Unreleased]` section.
- **Already in place:** stream-json output has been in use since 0.9.2.

---

## Part A — convergence (0.9.5)

The decision is **A1**: a truncated review is cached with a `truncated: true` flag. It is a
stop-gap. Part B raises the limits, and Part S removes truncation for big files. After
that, Part A covers only files that can't be split (minified or generated).

Carrying a truncated review is **not fail-open relative to today**: a single successful run
today passes the same truncated review. But it makes the truncated state sticky, so it must
stay visible (spec 5).

### Spec

1. **`_write_run_records`.**
   - For files in `_truncated_paths(result)`, or for every chunk file when `"*"` is set,
     write the record with `truncated: true`.
   - Also flag a file when the gate's own size check says its diff exceeds the thresholds.
     This uses numstat lines plus bytes, and only when numstat is numeric (binary `-` is
     handled separately).
   - **Never downgrade.** A valid non-flagged record is kept. A non-truncated write
     overwrites a truncated one.
2. **`_read_ledger_record`.** Returns the dict including `truncated`; an absent flag means
   not truncated. **No `_LEDGER_SCHEMA` bump.**
3. **`_plan_ledger` / `_find_delta_record`.**
   - An exact hit means `carry`, flagged or not.
   - Delta search skips flagged records.
   - A cost-rule flip never keeps a flagged prior: treat the file as `full` / `no_record`.
4. **`_attach_to_carried_records` / `_drop_self_resolved`.** Read, modify and write the
   loaded dict, changing only `findings`, so `truncated`, `chain_depth` and `run_id`
   survive. Writes stay atomic.
5. **Visibility.**
   - Every carried flagged record re-emits
     `{file, message: "diff truncated; reviewer saw stat + hunk headers only"}`, and
     status becomes `completed_with_warnings`.
   - The verdict summary counts **"N files effectively unreviewed (truncated)"**.
6. **Timeouts never loop.**
   - On a chunk timeout, persist a per-file `timeout_attempts` marker.
   - Next run, halve that chunk, down to 1 file.
   - After 2 timeouts at 1 file, fail the run with a terminal reason that names the file:
     "file X cannot be reviewed within the timeout". No endless "still running, re-push".
7. **Dead config and docs.**
   - Delete `_CHECKPOINT_TTL` / `OCR_CHECKPOINT_TTL`.
   - README: the "Chunk cache TTL" row becomes the per-file ledger description, and the
     run-budget row says "re-push to resume; recorded files are carried".
   - Fix the `_reap_async` docstring.
8. **Release.**
   - `plugin.json` 0.9.5, which fixes the skew.
   - The `[Unreleased]` `-z` / `--no-abbrev` fixes go into the 0.9.5 CHANGELOG entry.
   - Run `sync-local-install.py`.

### Separate behaviour change, shipped in 0.9.5 and tested on its own

**`_classify_priors`:** when a carried record's `head_oid` equals the current blob, replay
all severities without the resolver. Nothing can have been fixed on an identical blob.
This applies to **every** carried record, not just truncated ones, and it saves resolver
calls on every resume.

### Tests (`tests/test_async_gate.py`, stub reviewer, `OCR_CHUNK_FILES=1`)

- (a) A truncated file plus budget exhaustion: run 2 on the same tip does not review it
  again. The verdict keeps its findings, the warning, and the "effectively unreviewed"
  count.
- (b) `"*"` flags every chunk file, and all of them carry.
- (c) A new tip with that file's blob changed is `full`, never a delta from a flagged
  record.
- (d) A new tip with that file's blob unchanged is `carry`.
- (e) Rewrites keep the flag.
- (f) A truncated write doesn't overwrite a non-truncated record.
- (g) The size check flags a file over 400 lines without any reviewer warning.
- (h) An identical-blob prior replays without the resolver, including when its
  `existing_code` is no longer found.
- (i) A stub sleeps past the timeout for one file. Run 2 uses chunk size 1, and run 3 fails
  terminally naming that file.

---

## Part B0 — time metrics (0.9.6, no behaviour change)

1. **Per model call.** `_run_review_once` parses the stream-json it already captures.
   Results go in a module-level `_LAST_CALL_STATS`; the 3-tuple return is unchanged.
   - From the `result` event: `num_turns`, `duration_ms`, `duration_api_ms` and
     `total_cost_usd`.
   - Per tool use: name and input bytes.
   - Time to first event.
   - Wall time of the Agent tool call, `tool_use` → `tool_result`, which separates
     orchestrator time from reviewer time.
2. **Per run phase.** Timers for worktree creation, plan and ledger, impact analysis, diff
   build, each chunk (index, file count, lines, outcome), resolver and recheck, and total.
3. **Where the numbers land.**
   - Telemetry JSONL: additive keys, `_SCHEMA` stays 1.
   - The debug log, always on, one line per phase and per call. The log rotates.
   - The state file, so "still running" can show "chunk 3/10, avg 6m12s per chunk".
4. **Privacy.** Telemetry and log lines carry **counts, bytes and timings only**, never
   paths or code.
5. **`--telemetry-report`:** average and p90 seconds, turns, cost and Agent-input KB per
   kind, the per-phase breakdown, and the orchestrator vs reviewer split.
6. **Tests.** The stub emits a `result` event after many tool events. The test checks that
   stdout is fully drained and parsed.

**Decision point after 3–5 real pushes.** B0 confirms whether the retyping dominates.
Part B ships either way, because the higher limits matter. B0 sets the expectation.

---

## Part B — reviewer reads Python-built diffs, higher limits (0.10.0)

**Decided: variant 2.** It removes the retyping. It needs one edit to
`agents/code-reviewer.md`, which invalidates every cached review once (one full re-review
per active branch).

**Design the new reviewer input to be forward-compatible with Part S**, so S can avoid a
second prompt edit and a second invalidation. The manifest has:
- `items[]`, each with `kind: file_diff | unit_diff | context`;
- an optional `tasks[]`.

Rollback: `OCR_PRECOMPUTED_DIFFS=0` restores the old path for one release.

### Design

- **One manifest builder.** `_build_review_manifest(...)` replaces the two duplicated
  builders. A manifest is **always written**, including the small all-`full` case, so
  `tests/fixtures/argv_golden.json` is updated deliberately. The `plan is None` fallback
  stays manifest-less.
- **Diff files live in the gate's own run directory**, next to
  `manifest-{run_id}-{k}.json` (`.git/review-gate-async/run-{run_id}/diffs/<k>/<nnn>.diff`),
  **never inside the reviewed worktree.**
  - The tip tree is attacker-controlled. A tracked or symlinked `.review-gate` there could
    redirect writes or plant fake diffs.
  - **Nothing tracked is ever excluded from review.**
  - Files are written in binary mode with LF line endings. Paths stay short (MAX_PATH).
  - They are removed in the `finally`, and by the reaper for orphaned runs.
  - **Spike:** confirm that the headless reviewer may Read that directory. If not, pass it
    with `--add-dir` on the `claude -p` call.
- **Manifest:** `items[]`, each with
  `{kind: "file_diff", path, old_path, mode, file, lines, bytes, truncated, binary}`. The
  old `files[]` key keeps its shape for compatibility, because tests assert exact equality
  on it.
- **Diffs are generated with `ocr_impact.git_runner`**, which sets `GIT_LITERAL_PATHSPECS=1`
  and keeps the return code.
  - full: `git diff -M <range> -- "<path>"`. Renames pass both paths.
  - delta: `git diff <from_oid> <to_oid>`, with a `# path: … (delta since last review)`
    header line.
  - binary: flagged, no file.
  - Return code ≠ 0 or empty output: **fail closed for that file.** It goes to the
    orchestrator's own git path, as today; never an empty diff.
- **Higher limits, owned by Python.** There is one definition, which Part S references.
  - Per file: up to **~1,500 changed lines or 64 KB** in full. Above that, `-U0`, then stat
    plus hunk headers. Lines are capped at 500 chars.
  - **Chunk line budget:** one budget that counts diff lines plus context lines. Packing
    respects it, and the largest file degrades first only when a single file can't fit.
  - Diff files are multi-line, so the reviewer can page them with Read offset/limit. Its
    Read budget is raised for diff files.
- **`_truncated_paths`** = Python's own `truncated` set. Model truncation warnings count
  only for files without a precomputed diff.
- **Reviewer prompt (`code-reviewer.md`).**
  - It reads `items[].file`.
  - Diff files are labelled **untrusted data**.
  - Reading diff files is allowed and does not use up the change-set Read budget.
- **SKILL.md.**
  - Pass the manifest's item list to the reviewer; don't run `git diff` for items that
    have a file.
  - Don't re-truncate.
  - §R (resolver) gets the same treatment, plus `old_path` for renames.
- **`code-filter`** (no tools) still gets the cited files' diffs inline. That happens only
  for block candidates.
- **Process-group kill** (moved here from Part C, since it is needed regardless).
  - POSIX: the reviewer starts with `start_new_session=True`, and `_kill_child` uses
    `os.killpg`.
  - Windows: keep `taskkill /T`.
  - Today only the `claude` process dies, and its children survive.

### Tests

- A manifest is always written, and the golden argv is updated.
- Diff files for: full, delta (header present), rename (both paths), binary (flagged),
  git failure (falls back to the orchestrator path), CRLF, glob-character paths (`[x].py`),
  non-ASCII paths.
- **Security:** the tip contains a tracked `.review-gate/` directory, a file named that, and
  (POSIX) a symlink.
  - Those paths are reviewed like any other.
  - Nothing is ever written inside the worktree.
- **Limits:**
  - a 900-line file is reviewed in full, untruncated;
  - a file over the cap gets `-U0`, then stat + hunk headers;
  - the chunk line budget is respected;
  - `truncated` reaches `_truncated_paths` and the flagged record.
- The stub reviewer reads the item files and asserts their content.
- **Process groups:** killing a reviewer kills its child processes (POSIX test; Windows
  `/T` test).
- Resolver manifest: `files[]` unchanged, and the new items present.

---

## Part S — big files reviewed in stable units, with cross-function checks (0.11.0)

One release, as decided. Rollback: `OCR_SEGMENT=0` returns to whole-file diffs with the
Part B limits.

**Which files.** Only files over Part B's per-file limit (~1,500 lines or 64 KB) are
segmented. Everything else keeps whole-file review.

### Units with content identity

- **Python:** `ast` top-level functions and classes. Oversized classes are split into
  methods. Reuse `ocr_impact._ast_defs`.
- **TS/JS, Go, Rust, shell, PowerShell:** a column-0 plus brace-depth heuristic. Reuse
  `ocr_impact._regex_defs` where possible.
  - **Limitation:** a big class in these languages is one unit. Any edit re-reviews all of
    its parts.
- **Markdown:** heading sections.
- **Everything else, and module-level code:** content-defined cuts at blank or column-0
  lines, 24–120 lines each.
- Leading comments and decorators belong to the unit that follows them.
- **Identity:**
  - **`blake2b` full 256-bit** of the normalised text (CRLF → LF, trailing whitespace
    stripped).
  - **For AST units, the parent qualname is part of the identity.** A method moved into
    another class is a change.
  - For heuristic and content-cut units, a hash that matches at a different position counts
    as **changed**, not as a free move.

### What happens on a push

1. Segment the file at the base and at the tip.
2. **Pair the units.**
   - Units whose hash (and parent) appear on both sides are unchanged and never reviewed.
     Matching is multiset, so duplicates match by count.
   - The rest are paired by name, then by position. A rename is reviewed as a modification.
   - **Removed units produce a deletion item.**
3. **Coverage check (fail-closed).**
   - The union of the review items' line ranges must equal the changed tip lines from
     `git diff -U0`, plus the base-side deleted lines.
   - If it doesn't match, that file falls back to whole-file review (Part B limits, then the
     Part A path), and the gap is logged.
4. **Cache lookup per changed unit.**
   - A hit replays its findings.
   - A miss produces a `unit_diff` item: the unit's base→tip diff with absolute tip line
     numbers. It is never truncated.
   - A monolithic unit too big for one item is split into parts. Any edit re-reviews all
     parts.
5. **Packing.**
   - Within Part B's single chunk line budget.
   - **All changed units of a file go in one chunk**; a file is split only if it alone
     exceeds the budget, and then along call-graph components.
   - Cross-file changed units linked by call edges are packed together.
   - Packing never affects identity.
6. **Context per file per chunk:** imports and preamble (≤60 lines), a signature index of
   all units with changed ones marked, and `other_changed`.
7. **After each chunk**, write the unit records.
   - The **per-file record** (not truncated) is written only when **every changed unit and
     every caller check** touching the file is final.
   - Otherwise the run ends as budget-exhausted, and pending work is scheduled first on
     resume.
8. **Truncation for segmented files:**
   - model warnings (`diff truncated`, `"*"`) are **ignored**, because Python owns their
     diffs;
   - existing `truncated: true` records for files that can be segmented are treated as
     `no_record`, so they finally get a proper review.

**Cache key**, where `path_hash` is a hash of the tip path:
`seg:<SEG_VERSION>:<fingerprint>:<lang>:<path_hash>:<base_unit_hash or ->:<tip_unit_hash or ->`.
No schema bump.

### Findings and line numbers

- **Stored on each finding:** `anchor_hash`, `rel_start` and `rel_end`.
- **Replay** uses the paired unit instance, so duplicates replay to the right occurrence.
  The line is the unit start plus `rel_start`.
- **The anchor was edited since:** fall back to matching the finding's `existing_code`
  snippet. If that fails too, drop the finding and log `replay_dropped`.
- **When and where:** Python does this after each chunk, before dedup, the resolver and
  known_defects.

### Cross-function checks (in the same release)

1. **Call edges.** Python uses `ast`; other languages use a name-reference regex.
   - The regex skips names under 4 characters, names defined twice, and a stoplist.
   - Caps: 8 callers per changed unit, or 4 for regex languages.
   - Cross-file references come from `find_references`, mapped to their enclosing unit.
2. **Unit-driven impact.** `changed_symbols` is fed the changed units, so body-only
   changes are included.
   - `max_symbols` scales with the push, up to 60.
   - Each site is routed to the chunk holding its callee.
   - `impact_verdicts` are required.
3. **Caller context units with tasks.**
   - Same-file callers of a changed unit A become `context` items with a `dep:<n>` task:
     "verify B still handles A's arguments, return, exceptions, `await` and state".
   - The reviewer returns `dep_verdicts`.
   - Caps: 6 callers per unit, 80 lines per context unit, 400 context lines per chunk, all
     inside the chunk budget.
   - Cross-file sites widen to the enclosing unit, up to 12 KB.
4. **Dependency-check records:**
   `dep:<segver>:<fp>:<lang>:<path_hash_B>:<B_hash>:<path_hash_A>:<A_hash>`.
   - `ok` and `broken` are final for that pair of texts.
   - `unsure` is retried once, then recorded as a non-blocking "unverified dependency".
   - **A missing verdict counts as `unsure`, never `ok`.** Unknown task ids are ignored, and
     the schema is validated strictly.
   - Pairs that didn't fit the budget are stored as `pending` and scheduled first on the
     next run.
   - There is no cascade through unchanged units: one hop per push.
5. **Removed and renamed units are fed to §2b deterministically.**
6. **Delta mode on small files** also gets caller context units and dep records.
7. **Prompt injection.** A hostile caller could tell the reviewer to answer `ok`. These
   checks are additive: today callers get only ±6-line impact sites. So a forced `ok` falls
   back to today's level, never below it.

**Rejected:** callee contract digests in cache keys, propagation through unchanged units,
and an always-on cross-unit pass.

**Reviewer prompt.** Part B's `items[]` / `tasks[]` contract carries `unit_diff`, `context`
items and `dep` tasks. If S still needs a `code-reviewer.md` edit, that is a second
one-time invalidation; accept it.

### Tests

**Units:**
- A 10-function file with 50 lines inserted into function 1 gives one changed unit, and
  function 7's findings move by +50.
- A 2,000-line region file with one insertion gives at most 2 changed regions.
- **Moves:** moving function 3 within its parent gives zero changed units. Moving it into a
  class gives one item. A heuristic unit moved to a different position is changed.
- A rename gives one pair.
- **A 2,000-changed-line file:**
  - no item exceeds the budget;
  - an identical second run makes zero items;
  - after a one-unit edit, exactly one item is reviewed.
- A run killed after chunk 1 resumes with only the rest.

**Safety:**
- A coverage property test over the fixture repo: every changed line is in exactly one
  item, and a forced gap falls back to whole-file review.
- CRLF and LF versions give identical hashes.
- Duplicate units replay to the right occurrence.
- A `"*"` warning on a segmented file is ignored.
- Pending caller checks block the per-file record.
- A missing `dep_verdict` counts as `unsure`.
- An old `truncated` record of a segmentable file is not carried.

**Benchmark** (synthetic repo, 3 runs per scenario: whole-file vs Unit vs Unit+):
1. A body-only change now returns `None`; a same-file caller dereferences the result.
2. The same, with the caller in another file.
3. A function now raises `TimeoutError`; its caller catches only `ValueError`.
4. Parameters reordered, with 3 callers across 2 files.
5. A default changed from `retries=3` to `0`.
6. `def` became `async def`, and a caller doesn't `await`.
7. A module constant changed, breaking an assumption elsewhere.
8. A rename in a 2,000-line file, with same-file and cross-file callers.
9. A sibling fix: one twin fixed, the other not.
10. Lock pairing.
11. Two changed units in one file must agree (a serializer/deserializer pair).
12. A removed dataclass field is still read elsewhere.
13. Resume convergence.
14. A no-op re-push after a prepended 50-line docstring makes zero calls, with correct
    lines.
15. TS variants of scenarios 1 and 4.

**Ship when:**
- Unit+ is at least as good as whole-file on 1–6, 8, 11 and 13–15. Scenarios 11, 13 and 14
  pass 3/3.
- On 7, 9, 10 and 12, Unit+ is at least as good as today's large-file baseline. Gaps are
  documented.
- **Historical replay:** every past blocking finding reproduces in at least 2/3 runs, no
  blocked push becomes a pass, and the warn/pass sample gains under 20% new blocks.
- **Cost:** median calls are no higher than today, and p90 tokens per chunk at most 1.5×.
- **Reflexive-`ok` check:** `dep_verdicts` of 100% `ok` on the seeded bugs means the prompt
  is being ignored, so fix it first.

**Size.** About 1.5–2k lines including tests, roughly 4–6 agent-days.

**Confidence (super-thinker's estimate):**
- About 0.85 that it is no worse than today for big files.
- About 0.65 that it beats whole-file review on same-file cross-function bugs.
- Low for shared state, constants and ordering; those are documented as limits.

### Later (not scheduled)

- **Phase 1.5:**
  - `contract_delta` per unit;
  - a gated stage-2 call, at most one, only when a contract moved and some callers are
    pending;
  - a sibling-fix grep;
  - an interface digest, used only to prioritise budgets.
- **Phase 2:**
  - module constants and class fields as symbols;
  - cross-file callee signatures for Python;
  - a resolver short-circuit by `anchor_hash`;
  - cross-file moves;
  - unit-level delta reviews;
  - a lower segmentation threshold.

---

## Part C — parallel chunks (0.12.0)

As planned. Rollback: `OCR_CHUNK_CONCURRENCY=1` gives today's exact sequential behaviour.

- **`OCR_CHUNK_CONCURRENCY`**: default **2**, clamped to 1–4.
- **Manifests and diff files are built up front on the main thread.** `_impact_bundle`
  mutates `_TELE`. Workers only run `_run_review`.
- **One worktree per slot.**
  - Slot 0 is the existing `review_root`. Slots 1..N-1 come from `_make_worktree` with a
    `-s{i}` suffix.
  - Each slot runs its own `git clean` / `checkout` before each chunk.
  - If a slot's worktree can't be created, run with fewer slots. Never fall back to the
    live tree.
  - State gets `worktrees: [...]` (keep `worktree`). The reaper protects all of them, and
    the `finally` removes all of them.
- **Child registry.**
  - A lock-guarded set, `_ACTIVE_CHILDREN`.
  - `_run_review_once` adds and removes its own process in a `finally`.
  - `_beat` writes `reviewer_pids` (and `reviewer_pid` for compatibility). On `_Fenced` it
    kills all of them, as a group (Part B).
  - The stale restart kills them all.
- **Failure policy.**
  - **Abort now** on `_Fenced` or a usage limit: kill everything and stop dispatching.
  - **Drain** on one chunk's timeout or second-attempt error: let in-flight siblings finish
    and record, then raise. Part A's timeout splitting applies.
  - Precedence when several fail: `_Fenced` > limit > gate error > budget.
- **Budget** is checked before each dispatch.
- **The main thread owns state and the ledger** (`as_completed`, fence-checked updates).
- **Progress:** `chunks_done` counts completed chunks, plus `chunks_running`. The renderers
  show "N/M done, K running".
- **Deterministic merge:** results are sorted by `k`, and `-merged` is written last.
- **The resolver stays sequential.**

### Tests

- Pin the order-dependent tests to concurrency 1: `test_budget_exhausted_deterministic`,
  `test_kill_and_resume`, `test_attempts_increments_when_no_new_chunks_reviewed`.
- Fix the `STUB_FAIL_ON_CALL` race (key on the manifest's `chunk_index`).
- **At concurrency 2:**
  - the wall clock drops;
  - each slot has its own worktree, cleaned up afterwards and protected by the reaper while
    live;
  - a fence kills every child;
  - a limit aborts the sibling, but completed records persist;
  - a timeout lets the sibling finish;
  - out-of-order completion gives the same merged output;
  - the budget is respected;
  - the progress fields stay consistent.

---

## Operations

- **Ledger pruning:**
  - unit and dep records get TTL/LRU pruning, the same as file records (30 days, plus a
    count cap);
  - the doctor reports the ledger size.
- **Disk:** per-run diff directories and slot worktrees are removed in the `finally` and by
  the reaper.
- **Doctor:**
  - reports the active flags (`OCR_PRECOMPUTED_DIFFS`, `OCR_SEGMENT`,
    `OCR_CHUNK_CONCURRENCY`);
  - checks that the diff directory is readable by the reviewer.
- **Downgrade note:** 0.9.4 reads `truncated: true` records as normal ones, and may use them
  as delta bases. This is acceptable, and documented.

## Follow-ups (not scheduled)

- **Docs: sleep and suspend** count against the run budget and the chunk timeout.
- **Docs: moving remote.** Records for files that changed upstream miss by design.
- **Headless rule overrides.** `~/.ocr/rule.json` and `$OCR_RULE_FILE` probably never
  worked headlessly.

## Risks

- **Attacker-controlled worktree content** (symlinks, tracked gate-looking paths). Handled
  by keeping gate files outside the worktree, and covered by tests.
- **Hash collision.** Handled by full 256-bit unit hashes.
- **Segmentation coverage gaps.** Handled by the coverage check, which falls back to
  whole-file review.
- **Two prompt edits** (Part B, and possibly Part S): each invalidates the cache once.
- **Usage limits** come sooner at concurrency 2.
- **Disk and creation time** for per-slot worktrees on big repos (`_git` 30 s timeout).
- **Quality.** Replay past blocked pushes from `.git/review-gate-findings.jsonl` before and
  after every release, and compare the high-severity findings.

## Order of work

1. **0.9.5 — Part A**, including timeout splitting, the identical-blob prior replay, the
   version fix and the unreleased fixes; sync.
2. **0.9.6 — B0 time metrics**; 3–5 real pushes.
3. **0.10.0 — Part B (variant 2, higher limits, process-group kill)**; replay benchmark.
4. **0.11.0 — Part S** (units plus cross-function checks); seeded benchmark and replay.
5. **0.12.0 — Part C**, at concurrency 2.

## Separate one-off (not this plan)

The stuck `56bf432` push is being rescued in its own session. That session seeds ledger
records from the chunk outputs already paid for (c0–c5). It changes no gate code.
