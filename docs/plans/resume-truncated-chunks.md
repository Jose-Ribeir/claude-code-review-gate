# Plan: large pushes must converge, then get fast

Status: draft, not started. Merges the earlier 0.9.5 truncated-chunks draft with the
speed-up plan (parallel chunks and precomputed diffs).

Baseline: `main` at `101d213`, identical to `origin/main`. It includes the unreleased
`-z` / `--no-abbrev` fixes (`3dff6bb`, `101d213`). Line anchors below are approximate:
those commits shifted `scripts/review-gate.py` by about 15–25 lines around
`_collect_diff_entries`. Locate code by symbol name.

## Why these belong together

Three things make a large push slow or never finish:

1. **Resumes redo paid work.** A truncated file never gets a ledger record, so every resume
   reviews it again (Part A).
2. **Each chunk wastes turns on plumbing.** It runs git diffs and, probably, retypes every
   diff into the reviewer's prompt (Part B).
3. **Chunks run one at a time** (Part C).

Part A comes first. It is a correctness fix: without it, a push with about five or more
files over 400 changed lines can never finish within the run budget, however fast each
chunk is. Parts B and C then cut the time per run. B also gives A a deterministic,
gate-side truncation signal, and C's per-chunk ledger writes are what make partial
parallel runs resumable.

## What we know

- **Real case:** `feat/llm-provider-baseline` in thyra-ai, tip `56bf432`, 40 files in
  10 chunks of about 10 minutes each (9–13 minutes observed). Three runs, none finished:
  - run 1: c0–c1, then the usage limit;
  - run 2: c0–c5, then `OCR_RUN_BUDGET` (3600 s);
  - run 3: c0–c1, then the machine slept.

  Every run restarted at c0.
- **Why c0–c4 never stick.** They are large single files (49–58 KB, 760–900 changed
  lines). The skill truncates any per-file diff over 400 lines or 16 KB
  (`skills/review/SKILL.md` §3) and reports `diff truncated`. `_write_run_records` writes no
  record for a truncated file, and none for the whole chunk when the warning names no
  file (`"*"`).
- **Resume is per file, not per chunk.** `_plan_review` re-forms chunks from the files that
  have no ledger record. Chunk indices are not stable between runs, and that is fine.
- **Telemetry, last 7 days.** `review` calls average **314 s**; `resolve` and `recheck`
  average about 60 s.
- **One archived small review:** 11 turns and 67 s. 7 Read, 3 Bash (git), 3 Grep and
  1 Agent, so about half the turns are plumbing Python could do.
- **The reviewer subagent has only `Read, Grep`** (`agents/code-reviewer.md:4`). The
  orchestrator therefore has to retype every diff into the Agent prompt. A 1,500-line diff
  is about 20–30k output tokens, which is minutes per chunk. This is suspected, not yet
  measured (Part B0).
- **Before `101d213`, blob OIDs in diff entries were 7-char abbreviations.** Every carried
  finding looked changed and was sent to the resolver again, which added a resolve call
  (about 60 s) to every resume. That is now fixed. Records and resolutions keyed on short
  OIDs stop matching once, so each file gets one full re-review.
- **Version skew:** `.claude-plugin/plugin.json` still says `0.9.1`, but the CHANGELOG has
  0.9.2–0.9.4 released and an `[Unreleased]` section. Fix this in the first release below.

---

## Part A — resumed pushes keep truncated reviews (release 0.9.5)

**Decided: A1 (flag), as a stop-gap.** Part A makes resumes converge with today's review
quality. The real fix for big files is Part S, which reviews them in stable, content-hashed
units, so they are never truncated. Part A stays as the fallback for files Part S refuses
(minified or generated).

Ledger records are already written after each chunk (`_write_run_records` runs inside the
chunk loop), so the super-thinker's "persist per chunk" item needs nothing new.

### Reuse scope of truncated records (decided: A1)

- **A1. Recommended: a `truncated: true` flag.**
  - The record is valid for its `(key, head_oid)` like any other record.
  - The same path, status, base and blob give the same per-file diff, so a re-review sees
    the same truncated input. It costs tokens and gains nothing.
- **A2. Conservative: `scope_tip = <tip>`.**
  - The record is valid only for that exact tip.
  - Every new tip pays for a full re-review, with no gain in quality.

Both share one rule: **a truncated record is never used as a delta base, and never kept as
the prior for a cost-rule flip.** Its findings may be incomplete, and a delta review would
never revisit the part of the file the base review didn't see.

The spec below assumes A1. For A2, replace the flag with `scope_tip`, pass `tip` into
`_read_ledger_record` / `_plan_ledger`, and treat a mismatch as a miss.

### Spec

1. **`_write_run_records`.**
   - For files in `_truncated_paths(result)`, or for every chunk file when `"*"` is set,
     write the record normally with `truncated: true`.
   - Also flag a file when the gate's own size check says its diff exceeds the skill
     thresholds: more than 400 changed lines or more than 16 KB. This uses the entry's
     `lines` plus a byte count. Detection then doesn't depend on how the reviewer words
     its warning.
   - Part B replaces this check with the exact set of files Python truncated.
   - **Never downgrade.** If a valid record without the flag already exists, keep it. A
     non-truncated write overwrites a truncated one.
2. **`_read_ledger_record`.**
   - Same signature and validation. Return the dict, including `truncated`. An absent flag
     means not truncated.
   - **Don't bump `_LEDGER_SCHEMA`**: that would invalidate every existing record.
3. **`_plan_ledger` / `_find_delta_record`.**
   - An exact hit means `carry`, whether or not the record is flagged.
   - `_find_delta_record` skips flagged records.
   - A cost-rule flip never keeps a flagged prior: treat the file as `full` / `no_record`.
4. **`_attach_to_carried_records` / `_drop_self_resolved`.**
   - Read, modify and write the loaded dict, updating only `findings`.
   - Rebuilding the record through positional `_write_ledger_record` arguments drops
     `truncated`, and risks losing `chain_depth` and `run_id`.
   - Keep the record write atomic (tmp + rename).
5. **Verdict parity.**
   - For every carried record with `truncated: true`, re-emit
     `{file, message: "diff truncated; reviewer saw stat + hunk headers only"}` and set
     status to `completed_with_warnings`.
   - A resumed verdict then matches what a fresh run would show.
6. **`_classify_priors`.**
   - If medium and high findings on a carried record with an unchanged blob go to the
     resolver, every resume pays for that again.
   - When `head_oid` equals the current blob, replay all severities without the resolver.
     Nothing can have been fixed on an identical blob.
7. **Dead config and docs.**
   - Delete `_CHECKPOINT_TTL` / `OCR_CHECKPOINT_TTL`. CHANGELOG: "removed, unused since
     0.8.0".
   - README: replace the "Chunk cache TTL" row with "Per-file ledger records (30-day TTL,
     fp-keyed). Re-pushing carries every file already reviewed, including files whose diff
     was truncated. A truncated review is never used as a delta base."
   - README, per-run-budget row: "re-push to resume; files already recorded are carried".
   - Remove the "chunk cache" wording from the `_reap_async` docstring.
8. **Release.**
   - Set `plugin.json` to 0.9.5, which also fixes the 0.9.1 skew.
   - Fold the `[Unreleased]` `-z` / `--no-abbrev` fixes into the 0.9.5 CHANGELOG entry.
   - Run `scripts/sync-local-install.py`.

### Tests (`tests/test_async_gate.py`, stub reviewer, `OCR_CHUNK_FILES=1`)

- (a) The stub marks A truncated, and run 1 exhausts the budget after A. Run 2 on the same
  tip does not review A again. The verdict still has A's findings and a truncation
  warning.
- (b) A `"*"` warning flags every file in the chunk, and they all carry on resume.
- (c) A new tip where A's blob changed gives `full` / `no_record`. The flagged record is
  not used as a delta base.
- (d) A new tip where A's blob is unchanged gives `carry` under A1, and a miss under A2.
- (e) Rewriting through `_attach_to_carried_records` / `_drop_self_resolved` keeps
  `truncated`.
- (f) A truncated write does not overwrite an existing non-truncated record.
- (g) The gate's size check flags a file over 400 lines even when the reviewer reports no
  warning.

---

## Part B — Python precomputes diffs (release 0.10.0)

**Decided: B-variant 1.** The orchestrator Reads the precomputed diff files and passes
them to the reviewer as today. There is no edit to `agents/code-reviewer.md`, so no
ledger invalidation.

### B0. Time metrics in the logs (ships first, no behaviour change)

Goal: every run's log says where the time went.

1. **Per model call.** `_run_review_once` parses the stream-json events. Stats go in a
   module-level `_LAST_CALL_STATS`; the 3-tuple return is unchanged.
   - From the `result` event: `num_turns`, `duration_ms`, `duration_api_ms` and
     `total_cost_usd`.
   - Per tool use: name and input bytes. The Agent call's input bytes measure the diff
     retyping.
   - Time to first event (CLI startup).
   - Wall-clock time of the Agent tool call: from the `tool_use` event to its
     `tool_result`. This separates orchestrator time from reviewer time.
2. **Per run phase.** The supervisor wraps each phase in a timer: worktree creation, plan
   and ledger lookup, impact analysis, diff precompute (Part B), each chunk (with chunk
   index, file count, changed lines, outcome), resolver and recheck, and total.
   - Written to `_TELE["phases"]`.
3. **Where the numbers land.**
   - **Telemetry JSONL**, `review-gate-telemetry/<date>.jsonl`: full per-call and
     per-phase records. These are additive keys; `_SCHEMA` stays 1.
   - **Debug log**, `review-gate-debug.log`: one line per phase and per call, with
     `phase=… seconds=… turns=… agent_ms=… cost=…`. Written always, not only under
     `OCR_DEBUG`, because it is cheap and it is what you read after a slow push.
   - **State file**: `phase_timings` summary, so `_still_running_reason` can show
     "chunk 3/10, avg 6m12s per chunk".
4. **`--telemetry-report`**:
   - per kind: average and p90 seconds, turns, cost, and Agent-input KB;
   - a per-phase breakdown;
   - "orchestrator vs reviewer" time split.
5. **Tests.** The stub reviewer emits a minimal stream-json `result` event so the parsing
   is covered. Existing telemetry tests keep passing, because only keys are added.

After a few real pushes, the B0 numbers set the expected gain from Part B. They also tell
us whether to revisit variant 2 later.

### Design

- **One manifest builder.** A new `_build_review_manifest(...)` replaces the two
  duplicated builders (single-context and chunked).
- **Always write a manifest**, including the small all-`full` case that runs without
  `--paths-file` today.
  - Update `tests/fixtures/argv_golden.json` deliberately.
  - The `plan is None` fallback stays manifest-less.
- **Diffs are one file per changed file**, never inside the manifest.
  - The manifest is single-line JSON. The Read tool truncates at about 25k tokens and can't
    page a single line (measured).
  - Location: `.review-gate/diffs/<k>/<nnn>.diff` inside the review worktree, written after
    the per-chunk `git clean`. The reviewer's cwd is the worktree, so Read needs no extra
    permission. **Verify this in a spike.**
  - The allowlist excludes `.review-gate/` from review.
- **The manifest gains a sibling key:**
  `diffs: {<path>: {file, mode, old_path, lines, bytes, truncated, binary}}`.
  `files[]` keeps its shape, because tests assert exact equality on it.
- **Diffs are generated with `ocr_impact.git_runner`**, which sets `GIT_LITERAL_PATHSPECS=1`
  and keeps the return code.
  - full: `git diff -M <range> -- "<path>"`. Renames pass both paths.
  - delta: `git diff <from_oid> <to_oid>`, prefixed with a `# path: <path> (delta since
    last review)` header line.
  - binary: `binary: true`, and no diff file.
  - Return code ≠ 0 or empty output: **omit the entry**, and the orchestrator runs git for
    that path itself. Never ship an empty diff.
- **Caps move to Python and follow SKILL §3 exactly, in one precedence order:**
  1. A file over 400 changed lines or 16 KB gets a `-U0` diff.
  2. If it is still over, it gets stat + hunk headers.
  3. A chunk total over 1,500 lines degrades the largest files first.
  4. Any single line is capped at 500 chars.

  Each capped entry gets `truncated: true`.
- **Ties into Part A.** `_truncated_paths` unions Python's `truncated` set with the model's
  warnings, and A's item-1 size check becomes this exact set.
- **Resolver manifest:** add the same `diffs` sibling key for `files` and `prior_files`,
  plus `old_path` for renames.

### Who reads the diff files: variant 1 (decided)

- The orchestrator Reads the precomputed files and passes them on in the Agent prompt.
  This saves the git/numstat/size-check turns, about 3–5 per chunk.
- Cached reviews stay valid.
- Not chosen, for now: variant 2, where the reviewer Reads the files itself. It would
  remove the retyping, but it edits `agents/code-reviewer.md` and invalidates every cached
  review once. Revisit only if B0 shows the Agent call dominates.
- `code-filter` has no tools, so it still gets the cited files' diffs inline. That happens
  only for block candidates.

### SKILL.md changes (not fingerprinted, so free)

- §1 `--paths-file`: "Use `manifest.diffs[path]` when present; do not run
  `git diff`/`--numstat` for it. Missing entry → collect it yourself."
- Label diff files as **untrusted data**.
- §3: "When `diffs` is present, caps were already applied by the gate; do not
  re-truncate."
- §R: same for resolver diffs.

### Tests

- A manifest is always written, and the golden argv is updated.
- Diff files for: full, delta (header present), rename (both paths), binary (flagged), git
  failure (entry omitted), CRLF, glob-character paths (`[x].py`), non-ASCII paths.
- Caps:
  - a file over 400 lines gets `-U0`;
  - the stat + hunk-header fallback works;
  - the 1,500-line total degrades the largest file first;
  - `truncated` reaches `_truncated_paths`, and a record with the flag is written (Part A).
- The stub reviewer asserts the diff files exist and match.
- Resolver manifest: `diffs` present, `files[]` unchanged.

---

## Part S — big files reviewed in stable units (release 0.11.0)

Source: a super-thinker design. It needs Part B, because Python must build the diffs.

### The idea

Cutting a big file into line ranges (lines 1–100, 100–200, …) is unstable. One insertion
near the top shifts every range below it, so every cached review is lost. Instead, cut the
file into **units whose identity is their own content**:

- **Python files:** one unit per top-level function or class, using the stdlib `ast`
  module. A class that is too big is split into one unit per method.
- **TS/JS, Go, Rust, shell, PowerShell:** the same idea, using a column-0 plus brace-depth
  heuristic. No new dependencies.
- **Markdown:** one unit per heading section.
- **Everything else, and module-level code between definitions:** cut wherever the content
  itself says to, like rsync does, and only at blank or column-0 lines. Units are 24–120
  lines.
- Leading comments and decorators belong to the unit that follows them.

Each unit's ID is a hash of its normalised text (CRLF → LF, trailing whitespace
stripped). **Its line numbers are not part of its identity.** Inserting 50 lines into
function 1 changes only function 1's hash. Functions 2–10 keep their IDs, and their cached
reviews replay with line numbers shifted by +50.

### What happens on a push

1. **Who uses it.** Only files that would be truncated today (more than 400 changed lines
   or more than 16 KB of diff). Small files keep the per-file ledger unchanged.
2. **Segment both sides.** Python segments the file at the base and at the tip.
3. **Pair the units.**
   - Hashes present on both sides are unchanged. Moved functions count as unchanged too.
     Unchanged units are **never reviewed**.
   - The remaining units are paired by name (`def:Class.method`), then by position.
   - A renamed function is therefore reviewed as a modification.
4. **Look up each changed unit's cache.** Hit: its findings replay. Miss: the unit becomes
   a review item.
5. **Each review item is the unit's base→tip diff, never truncated.**
   - Diff headers use absolute tip line numbers.
   - A monolithic function too big for one item is split into parts of 400 lines or fewer.
6. **Packing.** Items are packed into chunks within the existing budgets. A chunk can hold
   up to 40 items but still at most 8 files. Packing never affects identity, so repacking
   invalidates nothing.
7. **Context block.** Once per file per chunk, the reviewer gets: the file's imports and
   preamble, a signature index of every unit (`L123  def foo(x)`, with changed units
   marked), and `other_changed`. It can still Read and Grep for callers.
8. **After each chunk**, Python writes one cache record per reviewed unit. When all of a
   file's changed units are covered, it also writes the normal per-file record (not
   truncated), so the next push of the same tip is a plain `carry`.

### Cache key

```
seg:<SEG_VERSION>:<fingerprint>:<lang>:<path_hash>:<base_unit_hash or ->:<tip_unit_hash or ->
```

- **`SEG_VERSION`** sits inside the key. Changing how files are cut invalidates only unit
  records, never per-file records.
- **No schema bump.** Unit records are a new key space in the same ledger.
- **The path is part of the key (decided).** `path_hash` is a hash of the tip path, so the
  key never holds the path itself.
  - Identical text can mean different things under different imports, so two files never
    share a unit review.
  - Cost: a renamed file has its changed units reviewed again. Its unchanged units are
    still never reviewed.

### Findings and line numbers

- Each finding gets extra optional fields: `anchor_hash` (the unit it sits in), and
  `rel_start` / `rel_end` (line offsets inside that unit).
- **Replay:** find the unit with that hash in the current tip; the absolute line is the
  unit's start plus `rel_start`.
- **The anchor unit was edited since:** fall back to matching the finding's
  `existing_code` snippet. If that fails too, drop the finding and log `replay_dropped`.
- **Who does this:** Python, after each chunk, before dedup, the resolver and
  known_defects run.

### Edge cases

- **A single function over 300 lines:** a container (class or impl block) is split into
  methods. Otherwise the function keeps one identity and its diff is split into parts.
  Any edit to it re-reviews all its parts.
- **Minified or generated files** (a line over 2,000 chars, or average lines over 300
  chars): not segmented. They fall back to Part A's truncated-flag path.
- **Python with syntax errors:** fall back to indent blocks, then content cuts. The key
  gets `lang=python-regex`.
- **Brace heuristics fooled** by template literals or heredocs: a function ends up in two
  units. Both are still reviewed. The reviewer's view is clumsier, but nothing is
  truncated.
- **A reformat** (indentation or line-wrapping changes): affected units are reviewed once,
  then cached.

### Tests that lock in the requirement

1. A 10-function file with 50 lines inserted into function 1 gives exactly one changed
   unit. The other 9 hashes are identical. Function 7's replayed findings move by +50.
2. A 2,000-line file of plain regions with one insertion gives at most 2 changed regions.
3. Moving function 3 to the end gives zero changed units.
4. Renaming function 3 gives one pair, reviewed as a modification.
5. A file with 900 changed lines:
   - no item is over 400 changed lines;
   - an identical second run has zero review items;
   - after editing one unit, exactly one item is reviewed.
6. A run killed after chunk 1 keeps chunk 1's unit records, and the resume reviews only
   the rest.

### Later (Phase 2 of the design)

- **Signature-change context.** When a unit's signature changes, callers in the same file
  are included as read-only context units.
- **Resolver short-circuit.** A finding whose `anchor_hash` still exists is still open,
  with no resolver call. Otherwise only that unit's diff goes to the resolver.
- **Cross-file move detection.**
- **Unit-level delta reviews.**
- **A lower segmentation threshold**, for example 200 lines, once this is stable.

---

## Part C — parallel chunks (release 0.12.0)

### Design

- **`OCR_CHUNK_CONCURRENCY`**: default **2**, clamped to 1–4. A value of 1 gives today's
  exact sequential behaviour.
- **Manifests are built up front on the main thread.** `chunk_extras` → `_impact_bundle`
  mutates `_TELE` non-atomically, so it must not run in worker threads. Workers only run
  `_run_review`.
- **One worktree per slot.**
  - Slot 0 is the existing `review_root`. Slots 1..N-1 come from `_make_worktree` with a
    `-s{i}` suffix.
  - Each slot runs its own `git clean` / `checkout` before each chunk.
  - If a slot's worktree can't be created, run with fewer slots. Never fall back to the
    live working tree.
  - State gets `worktrees: [...]`; keep `worktree` for compatibility.
  - `_reap_async` protects every listed worktree, and the `finally` removes them all.
- **Child registry.**
  - `_ACTIVE_CHILD` becomes a lock-guarded set, `_ACTIVE_CHILDREN`.
  - `_run_review_once` adds its own process and removes it in a `finally`. This also fixes
    today's stale pointer after a timeout or exception.
  - `_beat` writes `reviewer_pids`, plus `reviewer_pid` = the first one for compatibility.
    On `_Fenced` it kills **all** of them.
  - `_drive_review`'s stale restart kills every pid in `reviewer_pids`.
- **Failure policy (aligned with Part A: never throw away paid work).**
  - **Abort now** on `_Fenced` or `ReviewLimitError`: set a shared event, kill all children,
    stop dispatching.
  - **Drain** on one chunk's timeout or second-attempt `ReviewGateError`: stop dispatching
    new chunks, but let in-flight siblings finish and record, then raise.
  - Precedence when several fail: `_Fenced` > limit > gate error > budget.
- **Budget.** Checked before dispatching each chunk. In-flight chunks finish.
- **The main thread owns state and the ledger.**
  - It collects results with `as_completed`.
  - For each completion: a fence-checked `_update_state_owned`, then `_write_run_records`,
    including Part A's truncated records.
  - Workers never write state.
- **Progress.**
  - `chunks_done` counts completed chunks; it is no longer a prefix.
  - New `chunks_running: [k, ...]`. Keep `chunk_index` = the lowest running chunk.
  - Update `_still_running_reason`, `_failed_reason` and the session listing to show
    "N/M chunks done, K running".
- **Deterministic merge.** Sort results by `k` before `_merge_chunk_results`. The `-merged`
  raw snapshot is still written last.
- **The resolver and `_judge_resolution` stay sequential**, after all chunks.

### Tests

- Pin the existing order-dependent tests to `OCR_CHUNK_CONCURRENCY=1`:
  - `test_budget_exhausted_deterministic`;
  - `test_kill_and_resume`;
  - `test_attempts_increments_when_no_new_chunks_reviewed`.
- Fix the `STUB_FAIL_ON_CALL` race in `tests/stub_reviewer.py`: key on the manifest's
  `chunk_index` instead of counting trace lines.
- New tests at concurrency 2:
  - 4 chunks × `STUB_SLEEP=3` finish in under 9 s;
  - each slot has its own worktree, all are removed afterwards, and the reaper protects
    them while they are live;
  - a fence mid-run kills every child (no orphans);
  - a limit in one chunk aborts its sibling, the completed chunk's records persist, and the
    resume reviews only the rest;
  - a timeout in one chunk lets the sibling finish and record;
  - out-of-order completion gives the same merged output as sequential;
  - no chunk starts after the budget, and in-flight chunks finish;
  - `chunks_done` and `chunks_running` stay consistent.

---

## Follow-ups (not scheduled)

- **Per-chunk timeout.**
  - A timeout saves nothing and is not retried.
  - The 1,200 s `OCR_CHUNK_TIMEOUT` is close to the 9–13 minutes observed for solo files
    over 400 lines.
  - Options: a higher timeout for those chunks, or one retry while budget remains.
  - Part B's capping may make this moot. Re-measure after B.
- **Docs: sleep and suspend.** They count against both the run budget and the chunk
  timeout.
- **Docs: moving remote.** The record key includes the base blob, so when the remote moves
  between resumes, files that changed upstream miss by design.
- **Headless rule overrides.** `~/.ocr/rule.json` and `$OCR_RULE_FILE` probably never worked
  in the headless run: there is no `~` or env access, and Bash is git-only.

## Risks

- **Usage limits** come sooner at concurrency > 1: the same total tokens, compressed in
  time.
- **Disk and creation time.** There are N full-checkout worktrees, and `_git` has a 30 s
  timeout on big repos.
- **B-variant 2** invalidates the ledger once.
- **Quality.** Python's truncation must match SKILL §3 exactly.
  - Before and after each release, replay past blocked pushes from
    `.git/review-gate-findings.jsonl` and compare the high-severity findings.

## Order of work

1. **Part A → 0.9.5** (version-skew fix and the unreleased `-z` fixes included); sync the
   local install.
2. **B0 time metrics**, then a few real pushes to get a baseline.
3. **Part B (variant 1) → 0.10.0**, then the replay benchmark.
4. **Part S → 0.11.0**: segmented review of big files, then the replay benchmark again.
5. **Part C → 0.12.0**, at default concurrency 2. Unit records are independent, so
   parallel chunks are safe on top of S.

## Separate one-off (not this plan)

The stuck `56bf432` push is being rescued in its own session. That session seeds ledger
records from the chunk outputs already paid for (c0–c5), so only c6–c9 are reviewed. It
changes no gate code.
