# claude-code-review-gate — Blocking AI code-review gate for Claude Code

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Claude Code Plugin](https://img.shields.io/badge/Claude%20Code-plugin-8A2BE2.svg)](https://code.claude.com/docs/en/plugins)
![Status: beta](https://img.shields.io/badge/status-beta-orange.svg)

> **AI code review + a blocking pre-push gate, native to Claude Code.**
> Runs on your Claude subscription the compliant way — no token borrowing, no external binary.

**claude-code-review-gate** is a Claude Code plugin that adds AI code review and a **blocking pre-push gate** to your workflow. Claude Code does the inference itself, so review runs **on your Claude subscription** with no token borrowing and no third-party binary. The review methodology is adapted from [open-code-review](https://github.com/alibaba/open-code-review). Install the plugin and review with `/review-gate:review`.

## Demo

Push a branch containing a confident high-severity bug, and the gate blocks it:

```console
$ git push
[review-gate] review-gate blocked this push (high-severity issues):
  [high] db.py:6 - SQL injection: `user_input` is concatenated directly into the
         query string; an attacker can pass `' OR '1'='1` or `'; DROP TABLE users; --`.
  [medium] db.py:10-11 - the sqlite3 connection is never closed; callers leak the handle.

Fix the issues, or bypass with: git push --no-verify
```

Switch the bug to a parameterized query and the push sails through (`verdict: pass`).
Want advisory-only? Set `OCR_ADVISORY=1` and findings are printed but never block.

<!-- A short GIF of the above (block → fix → pass) can replace this console block. -->

## What it is

- **On-demand review** of your working diff or staged changes, and a **full-file scan** of a repo — run as a skill: `/review-gate:review`.
- A **blocking push gate**: reviews every unpushed commit **once per push** (not once per commit) and **blocks confident high-severity findings**. Default gates pushes made through Claude Code; an optional installer extends it to **every** push (terminal, IDE, or Claude Code).
- **One reviewer subagent** for the whole change set, so cross-file defects — a symbol renamed in one file and stale in another, a guard removed in A but still assumed by B — are visible. Large diffs (>15 files) fan out by directory.
- An **independent falsify pass** before anything blocks: a second subagent that never saw the reviewer's reasoning gets only the diff and the findings, and drops a finding **only** with direct counter-evidence.
- A **deterministic verdict** (`block` / `warn` / `pass`) decided by auditable code, not the model's discretion.
- **Fails closed** on timeout, crash, unparseable output, a missing Python 3, or a reviewer it cannot locate — a broken gate blocks rather than waving changes through. `OCR_FAIL_OPEN=1` is the emergency bypass. See [Safety & limitations](#safety--limitations) for the cases that still fail open.

## Why this exists (and how it relates to open-code-review)

[open-code-review](https://github.com/alibaba/open-code-review) (ocr) is an excellent AI code-review CLI. But running its binary on a **Claude Pro/Max subscription** means feeding your subscription/OAuth token to a third-party tool — which **violates Anthropic's Terms of Service and is actively blocked** (those tokens are for Claude Code and claude.ai only).

This plugin gives you the **same review methodology the compliant way**: Claude Code reviews your code *itself*, inside the official client, on your subscription. **No token leaves Claude Code, and the ocr binary never runs.** The reviewer prompts, scope rules, and rubric are **adapted from open-code-review** (Apache-2.0) and re-expressed as native Claude Code skills and subagents — see [Credits](#license--credits).

## How it works

```
/review-gate:review  (orchestrator skill, runs on the main agent)
        │  select unpushed/changed files → allowlist → rule hierarchy
        │  (+ per-language rules and LLM-authored-code rules per file)
        ▼
   ONE code-reviewer subagent for the whole change set
        │   (>15 files: fan out by top-level directory, max 4 parallel)
   ┌────┴──────────────────── code-reviewer ─────────────────────┐
   │  risk scan → gather evidence (Grep REQUIRED for any         │
   │  cross-file claim) → emit Finding[] JSON with `evidence`    │
   └─────────────────────────────────────────────────────────────┘
        │  hallucination check: existing_code not in the diff → downgrade
        ▼
   code-filter subagent (only if something would block)
        │  fresh context, never saw the reviewer's reasoning;
        │  drops a finding ONLY on direct counter-evidence
        ▼
   dedup → verdict: block | warn | pass → text, or JSON for the gate
```

The push gate runs `claude -p "/review-gate:review --unpushed --json"` headlessly and maps the verdict to a decision: a Claude Code **PreToolUse** hook returns allow/deny (default wiring), or a **git pre-push** hook returns an exit code (the optional "everywhere" wiring).

That headless session is deliberately isolated from your interactive one — pinned model, no user-level settings or hooks, no MCP servers. See [Cost](#cost) for why that matters.

## Install

**1. From the marketplace (recommended).** In Claude Code:
```
/plugin marketplace add Jose-Ribeir/claude-code-review-gate
/plugin install review-gate@claude-code-review-gate
```

**2. Local / development install:**
```
claude --plugin-dir /path/to/claude-code-review-gate
```

**3. (Optional) gate EVERY push, everywhere.** By default the gate only fires for pushes made through Claude Code. To gate terminal/IDE pushes in every repo too:
```
bash scripts/install-git-hook.sh     # sets a global core.hooksPath
bash scripts/uninstall-git-hook.sh   # reverts it
```
> ⚠️ This sets a **global** `core.hooksPath`, which applies to all your repos and overrides each repo's `.git/hooks/pre-push` (this hook still runs a repo-local pre-push if one exists, so existing hooks keep working). Requires the `claude` CLI on your `PATH`.

**4. Confirm it's actually wired up.**
```
/review-gate:doctor
```
Worth doing once after install or upgrade. A hook that fails to *launch* is treated by Claude Code as non-blocking, so a gate that cannot start does not announce itself — it just stops reviewing. The doctor is how you check.

> **Upgrading from a pre-0.3 install?** Re-run `bash scripts/install-git-hook.sh` if you use the global git hook. The executables moved from `bin/` to `scripts/`; existing hooks self-heal (they now resolve the reviewer at runtime, and a compatibility shim is kept at the old path until 0.5.0), but re-running gives you a clean install. `/review-gate:doctor` reports whether yours is stale.

> **Upgrading from a pre-0.2 install?** Earlier versions installed a **`pre-commit`** hook. It fires on every commit, and since the gate now reviews `@{u}..HEAD` it would review the wrong state at commit time (during a pre-commit hook the new commit does not exist yet, so `HEAD` is still its parent). Re-run `bash scripts/install-git-hook.sh` — it removes the stale `pre-commit` and installs `pre-push` in its place.

**Requirements:** Claude Code with an authenticated Claude subscription; Python 3 and Git. No API key.

> **No Python 3 found?** The plugin checks for a working interpreter at the start of every Claude Code session and warns you right away if it can't find one — you don't have to wait for a blocked `git push` to discover it. Install Python 3, then restart Claude Code so it picks up the new `PATH`.

> **Windows:** the gate ships both a Git Bash and a PowerShell adapter, so Git for Windows' "Git from the command line only" setup (which keeps `bash.exe` off `PATH`) is fine. The global git hook installed in step 3 does need a bash, which Git for Windows always bundles. Run `/review-gate:doctor` if you want to confirm what is wired up.

## Usage

```bash
# Review your current working changes
/review-gate:review

# Review every unpushed commit (what the push gate uses)
/review-gate:review --unpushed

# Review only staged changes
/review-gate:review --staged

# Full-repo scan with a project summary
/review-gate:review --scan --summary

# Use a specific rule file, output machine-readable JSON
/review-gate:review --unpushed --rule ./.ocr/rule.json --json
```

**The push gate in action:** push a branch containing a confident high-severity bug and the push is blocked with the findings listed. Fix it, or bypass once:
```bash
git push --no-verify
```

## Configuration

| Setting | Default | How to change | Effect |
|---|---|---|---|
| Gate scope | Claude Code commits | run `scripts/install-git-hook.sh` (→ everywhere) / `uninstall-git-hook.sh` | which commits are reviewed |
| Mode | **block** | `OCR_ADVISORY=1`, or `.ocr/config.json` `{"blocking": false}` | block vs warn-only |
| Block threshold | `high` & `confidence ≥ 0.7` | `OCR_BLOCK_SEVERITY`, `OCR_BLOCK_CONFIDENCE` | what is severe/sure enough to block |
| Reviewer timeout | `1800`s | `OCR_TIMEOUT` | fail-**closed** deadline for `claude -p`, enforced by the detached supervisor (blocks the push). Do **not** raise `hooks/hooks.json`'s `timeout` with it -- see [Long reviews](#long-reviews-and-the-desktop-app) |
| Inline wait | `600`s (clamped 30..840) | `OCR_INLINE_BUDGET` | how long a `git push` through Claude Code waits for the review before answering "still running, re-run the push" |
| Inline wait, git hook | `300`s (full `OCR_TIMEOUT` at a terminal) | `OCR_INLINE_BUDGET_GIT` | same, for the pre-push adapter under Claude's Bash tool (whose ceiling is 600 s) |
| Re-review a recorded tip | -- | `OCR_FORCE_REVIEW=1` | ignore a verdict recorded for these exact commits within the last hour |
| Legacy range | -- | `OCR_LEGACY_RANGE=1` | review the checked-out branch against its upstream when the push command cannot be parsed (0.5.x behaviour) |
| Per-file rules | built-in rubric + per-language rules | `.ocr/rule.json` (project), `~/.ocr/rule.json` (global), `--rule <path>` | the review checklist per file |
| Review model | `sonnet` | `OCR_MODEL` (`haiku` / `sonnet` / `opus`) | **cost lever.** The review runs in its own headless session; without a pin it would inherit the parent session's model and pay its cache-read rate on a workload that re-reads context every tool call |
| Extra `claude` flags | — | `OCR_CLAUDE_EXTRA_ARGS` | appended to the defaults |
| All `claude` flags | see `DEFAULT_CLAUDE_ARGS` | `OCR_CLAUDE_ARGS` | replaces the defaults **wholesale** — discards the cost controls too |
| Bypass once | — | `OCR_FAIL_OPEN=1` in Claude Code's launch environment | skip the gate for one push (`--no-verify` is refused through Claude Code: it would disable the git-hook backstop) |
| Findings log | `.git/review-gate-findings.jsonl` | — | one JSON line per completed review, **append-only and never pruned** |
| Raw-output snapshots | newest `50`, in `.git/review-gate-history/` | `OCR_HISTORY_LIMIT` (`0` = keep all) | full reviewer stdout per run |
| Chunk threshold | `15` files | `OCR_CHUNK_THRESHOLD` | pushes with more than this many reviewable files are split into per-directory chunks reviewed one at a time; below the threshold the whole diff is reviewed in a single context (today's behaviour) |
| Files per chunk | `8` | `OCR_CHUNK_FILES` | maximum number of files in one chunk; a directory group that exceeds this is split further |
| Lines per chunk | `1200` | `OCR_CHUNK_LINES` | maximum added+deleted lines per chunk; an individual file over this limit gets its own chunk. Used only with `OCR_PRECOMPUTED_DIFFS=0`: since 0.10.0 chunks are packed by the row below |
| Diff lines per chunk | `3000` | `OCR_CHUNK_DIFF_LINES` | 0.10.0: one budget per chunk, counted in lines of diff delivered to the reviewer (context lines included). A file whose own diff would not fit it is degraded first (no context lines, then hunk headers) |
| Precomputed diffs | on | `OCR_PRECOMPUTED_DIFFS=0` to disable | 0.10.0: the gate builds each file's diff and the reviewer reads it from a file, instead of the orchestrator retyping every diff into an Agent prompt. `0` restores the 0.9.x path exactly (inline diffs, 400 lines / 16 KB per file, no manifest for a small first push). Kept for one release |
| Big files in units | on | `OCR_SEGMENT=0` to disable | 0.11.0: a file whose diff is over the per-file limit (1,500 changed lines / 64 KB) is cut into units (functions, classes, methods, regions) and only the units that really changed are reviewed, each from its own unit diff with absolute line numbers; same-file callers of a changed unit are checked (`dep_verdicts`). `0` restores the 0.10.0 review of such a file exactly (stat-level truncated diff, flagged). Needs `OCR_PRECOMPUTED_DIFFS` on. Kept for one release |
| Chunks at once | `2` | `OCR_CHUNK_CONCURRENCY` (1-4) | 0.12.0: chunks reviewed in parallel, each in its own detached worktree (`<tip>-<run>-s<i>` beside the run's own, removed when the run ends). A fence or a usage limit stops every chunk at once; any other failure lets the chunks in flight finish and record first. Results are merged in chunk order, so the verdict does not depend on which chunk finished first. `1` restores the sequential review exactly. A review that could not get a worktree (it reads the live tree) runs one chunk at a time |
| Per-chunk timeout | `1200`s | `OCR_CHUNK_TIMEOUT` | `claude -p` deadline per chunk; a timeout fails closed and is not retried as is: the files in the chunk are marked, and the next push retries them in halves, down to one file. A file that times out alone twice ends the review with a terminal reason naming it (`OCR_FORCE_REVIEW=1` tries once more; raising this value also resets the marks) |
| Per-run budget | `3600`s | `OCR_RUN_BUDGET` | wall-clock budget for the whole chunked run; when the next chunk can't start within it the supervisor writes `failed(budget)`. Re-push to resume: recorded files are carried, only the rest is reviewed |
| File ceiling | `40` | `OCR_MAX_FILES` | maximum reviewable files across all chunks; the largest-diff files are kept when the ceiling fires |
| Resume record | per file | `OCR_LEDGER_TTL` (see the two rows below) | a finished chunk's files are recorded one by one in `.git/review-gate-ledger/` (replacing the 0.7.0 chunk cache and its `OCR_CHECKPOINT_TTL`, removed in 0.9.5); a re-push of the same tip, or of a later one, carries every recorded file instead of reviewing it again. A file whose diff the reviewer saw only partly (over 400 lines or 16 KB, per the skill's cap) is recorded as `truncated`: it is carried too -- re-reviewing the same content would truncate it the same way -- but every verdict then says `N files effectively unreviewed (truncated)` |
| Review ledger | on | `OCR_LEDGER=0` to disable | per-file content-addressed cache that powers incremental re-review; `0` reviews every file in full, matching 0.7.0 behaviour without the chunk cache |
| Ledger TTL | `2592000`s (30 days) | `OCR_LEDGER_TTL` | a ledger record older than this is treated as a miss; the mtime is touched on each reuse, so actively-changing files keep their records alive |
| Ledger record cap | `5000` | `OCR_LEDGER_MAX_RECORDS` | maximum number of records kept across all fingerprint dirs; oldest are pruned first, once per run at start |

Rule precedence (highest first): `--rule` → project `.ocr/rule.json` → global `~/.ocr/rule.json` → built-in `skills/review/rubric.md`, then the matching `skills/review/rules/<lang>.md` and `rules/llm-authored-code.md` appended. See `examples/.ocr/rule.json`.

### Long reviews and the desktop app

The Claude desktop app kills a session's CLI process after roughly **16 minutes** with
no output while a turn is pending, and a PreToolUse hook produces no output for as
long as it runs. Before 0.6.0 that meant every review longer than ~16 min killed the
session underneath the gate: the push never ran, the reviewer kept going as an orphan,
and the next message opened with a synthesised `[Request interrupted by user]`.

Since 0.6.0 the review runs under a **detached supervisor** that outlives the hook.
The `git push` waits for it inline for up to `OCR_INLINE_BUDGET` (600 s) and then:

- **finished** -> the verdict is delivered exactly as before (deny with findings, or
  allow);
- **still running** -> the push is **denied** (never allowed unreviewed) with
  *"review still running, re-run this exact `git push`"*. The retry joins the same
  review and answers as soon as it finishes; nothing is reviewed twice. A
  PostToolUse note also announces the verdict on the next executed Bash call.

Every verdict, blocking or not, is recorded per pushed **tip** for an hour in
`.git/review-gate-async/<tip>.json`, so a retry after a block answers instantly instead
of paying the full review again. `OCR_FORCE_REVIEW=1` re-reviews anyway.

The reviewer reads a **detached worktree** at the pushed commit, so the session can
keep editing, switching and stashing while the review runs, and the reviewer's scratch
files never land in your tree. What is reviewed is what the command **sends**: the
branch named in `git push [-u] <remote> <branch>` against the remote-tracking ref (or
`@{push}` for a bare `git push`), not the checked-out `HEAD`. Compound commands that can
move a ref before the push (`git switch x && git push`, `git commit && git push`), more
than one push per command, `--no-verify`, and option shapes the parser does not know are
refused with an explanation.

### Where findings go

A **blocking** finding is impossible to miss — it stops the push. A `warn` or `pass`
finding used to be almost impossible to *keep*: it was printed once to stderr, and the
only file holding the detail (`review-gate-last-output.json`) was overwritten by the
next run. Medium and low findings on a passing push were effectively gone.

Now every completed review is recorded, whatever the verdict:

```bash
# Replay the last 10 recorded reviews, with their findings
python ~/.claude/plugins/.../review-gate/scripts/review-gate.py --history

# ...or just read the log; it is one JSON object per line
cat .git/review-gate-findings.jsonl | tail -1
```

| What | Where | Retention |
|---|---|---|
| Findings, verdict, branch, HEAD sha, timestamp | `.git/review-gate-findings.jsonl` | **kept forever** — nothing in the plugin deletes from it |
| Full reviewer stdout for one run | `.git/review-gate-history/<UTC>-<sha7>.json` | newest `OCR_HISTORY_LIMIT` (default 50) |
| Full reviewer stdout for the *latest* run | `.git/review-gate-last-output.json` | overwritten every run (unchanged) |

Non-blocking findings are also **reported** rather than swallowed: in hook mode they come
back in the push's `permissionDecisionReason`, so the calling Claude Code session sees them,
and the "already reviewed this HEAD" short-circuit replays the other adapter's findings
instead of allowing silently.

All of this lives in `.git/`, so it is per-clone, never committed, and never pushed.

### Incremental re-review (0.8.0)

After a block, the typical cycle is: fix the flagged file, re-push, wait for a full review again. 0.8.0 cuts that wait by keeping a **review ledger** — a content-addressed per-file record of findings stored at `.git/review-gate-ledger/`.

On each push the planner classifies every file in the diff:

| Class | Condition | What happens |
|---|---|---|
| **carry** | Same before/after blob OIDs as in a prior record | Findings replayed from the record; no reviewer call |
| **delta** | Same base blob, different head blob, and the push-range diff is more than 1.5× the delta | Reviewer sees only the `old → new` diff for that file |
| **full** | No prior record, record expired/corrupt, or the push-range diff is at most 1.5× the delta (the *cost rule*) | Reviewed over the whole push range; a cost-rule full keeps the record's owed findings |

Only delta and full files are sent to the reviewer. A no-change re-push (same blobs) returns a verdict with zero `claude -p` calls.

**Resolver pass.** After the reviewer finishes, one targeted `--resolve` call re-judges every high/medium prior finding from carried/delta records. A finding the resolver clears is suppressed from the verdict; if the reviewer independently re-reports the same finding, the resolution is discarded and the reviewer wins. A later push where the resolving evidence has been reverted brings the finding back.

The resolver is judged against the tip, not trusted. A `resolved` verdict must quote the fix from the added side of either this push's diff or the finding's *since-raised* diff (the file as it was when the finding was made, compared with the tip), so a fix made in an earlier push whose resolver run failed still counts. A `still_present` verdict must quote the offending code as it exists at the tip; failing that, the gate checks the finding's own `existing_code` at the tip. A verdict that neither check backs, including a malformed one such as `{"<id>": true}`, is sent back to the resolver once with a re-check flag. If it is still unbacked, the finding keeps blocking and is labelled `(unverified: ...)` in the block reason, and the next push checks it again. Priors in a file that changed since the finding are re-judged even on a push where nothing else is reviewed. A prior on an unchanged carried file (the record and the finding are both of the file's current blob) replays as `(carried)` without a resolver call (0.9.5): nothing in an identical file can have been fixed since.

**Truncated reviews carry too (0.9.5).** With `OCR_PRECOMPUTED_DIFFS=0` the skill degrades a file's diff to stat + hunk headers past 400 lines or 16 KB, and the gate checks the same cap itself (since 0.10.0 the limits below apply, and the gate's own flag is the truth). Such a file's record is flagged `truncated`, and a resume carries it instead of reviewing it again (it would be truncated the same way, and a big push never converged). It is never a delta base, a changed blob is reviewed in full, and each verdict says `N files effectively unreviewed (truncated)` and re-emits the skill's `diff truncated` warning, so the state stays visible however many pushes it is carried through.

**Precomputed diffs and higher limits (0.10.0).** The reviewer used to get its diffs retyped by the orchestrator into an Agent prompt (about 20-30k output tokens for a 1,500-line diff, the suspected main cost of a 314 s review call). Now Python runs `git diff` for each file, once, with literal pathspecs (so `[x].py` and `:(exclude)x` are names, not patterns), and writes the result as a text file under `.git/review-gate-async/run-<id>/diffs/<chunk>/<nnn>.diff`; the manifest's `items[]` lists them (`kind: file_diff`, with `path`, `old_path`, `mode`, `file`, `lines`, `bytes`, `truncated`, `binary`, `level`; `context` items name the carried and other-changed files; `tasks[]` is reserved) and the reviewer Reads them. The directory is the gate's own and **never inside the reviewed worktree**: the tip tree is attacker-controlled, so a tracked `.review-gate` directory, file or symlink there can neither redirect a write nor plant a diff, and nothing tracked is ever excluded from review. File names are generated, never taken from the tree. The files are removed when the run ends (and by the reaper for a run that died). The headless reviewer reads them with the arguments it already has: no `--add-dir` is needed (measured with `claude` 2.1.281, from the orchestrator and from the `code-reviewer` subagent), and the tool allowlist is unchanged; if a future `claude` restricts reads outside the working directory, `OCR_CLAUDE_EXTRA_ARGS="--add-dir <git-dir>/review-gate-async"` grants it.

The limits are one definition, owned by Python: a file's diff is delivered in full up to **1,500 changed lines or 64 KB**; past that without context lines (`-U0`, every change still shown, not truncated), and past that as hunk headers only (`truncated`, flagged in the ledger and in every verdict as before). A line over 500 characters is cut and counts as truncated. A chunk is packed by the diff lines it delivers (`OCR_CHUNK_DIFF_LINES`). A file whose diff git could not produce is not given an empty one: it goes to the orchestrator's own git path, as in 0.9.x. Model warnings about truncation count only for files without a precomputed diff. The diff directory is also where the resolver reads (`items[]` with `role: active` / `since`). The reviewer prompt changed, so **every cached review is invalidated once** (one full re-review per active branch).

**Big files in stable units, with caller checks (0.11.0).** A file over Part B's per-file limit used to be reviewed as a degraded diff (hunk headers only, flagged `truncated`), and any edit to it re-reviewed all of it. Now `scripts/ocr_segment.py` cuts it into **units** and compares the base's units with the tip's by content: a Python file by `ast` (top-level functions and classes, an oversized class into its methods, decorators and leading comments with the unit after them), a TS/JS/Go/Rust/shell/PowerShell/Java/Ruby/PHP file by the definitions the impact search already finds (a big class stays one unit), Markdown by heading, everything else (and module-level code) by content-defined regions of 24-120 lines. A unit's identity is the **blake2b-256 of its normalised text** (CRLF = LF, trailing whitespace ignored; for a Python unit also its parent's qualname, so a method moved into another class is a change). A unit whose text is on both sides is never reviewed, wherever it moved; a moved heuristic unit counts as changed; a removed unit gets a deletion item; renames are paired by name, then position. The reviewer gets each changed unit's base -> tip diff as a `unit_diff` item (absolute line numbers, **never truncated**; a monolithic unit too big for one item is split into parts), plus a context item with the imports and an index of every unit of the file.

**Fail-closed coverage.** Every changed line `git diff -U0` reports (whitespace and blank lines excluded) must lie in a unit, or the whole file is reviewed as before and the gap is logged. A file that cannot be cut at all (a changed line over 500 characters: minified, generated) keeps Part B/Part A behaviour: stat-level, flagged, carried. A model's `diff truncated` warning about a unit-reviewed file is ignored, and an old `truncated` record of a file that can be cut is no longer carried: it gets its proper review.

**Cache.** Each finished unit review is a record `seg:<version>:<fingerprint>:<lang>:<path hash>:<base unit hash>:<tip unit hash>` under `.git/review-gate-ledger/<fp>/seg/`, holding its findings with an `anchor_hash` and `rel_start`/`rel_end`; a hit replays them at the unit's current lines (a line whose anchor text changed is found by its `existing_code`, or dropped and logged `replay_dropped`). A run killed halfway resumes with only the units it had not finished. The file's own per-file record is written only when every unit and every caller check touching it is final.

**Caller checks.** For each changed named unit A (and each removed or renamed one, under its old name), up to six same-file callers B (an `ast` call edge in Python; a name-reference regex elsewhere, skipping names under four characters, on a stoplist or defined twice, capped at four) become a `context` item and a `dep:<n>` task: does B still handle A's arguments, return, exceptions, `await` and state? The reviewer answers `dep_verdicts` (`ok` / `broken` / `unsure`); `broken` is a finding at B. A verdict that is missing, malformed or `unsure` is `unsure`, never `ok`: it is asked once more in the same run, then recorded as a non-blocking "unverified dependency" note. The results are records `dep:...` keyed by both texts, so an unchanged pair is never asked twice; a check still owed when a run ends (`pending`) is scheduled first on the next one. One hop per push: nothing cascades through unchanged units. A small file reviewed as a delta gets the same caller checks. Callers in other files are the impact bundle's (0.9.0), whose snippet for a symbol of a unit-reviewed file now shows the whole enclosing unit (up to 12 KB) and whose symbol budget scales with the push (up to 60); each symbol rides with the chunk that holds its unit.

**Packing.** All the changed units of a file go in one chunk; a file is split only when it alone exceeds `OCR_CHUNK_DIFF_LINES` (along call-graph components), caller context is capped at 80 lines per unit and 400 per chunk, and files that call each other are packed together. Packing never changes a unit's identity, so the cache is unaffected. A chunk that times out is split by units. **The reviewer prompt changed: every cached review is invalidated once.** See `docs/benchmark-part-s.md` for what is automated and what still needs a real model.

**The reviewer's process group is killed with it (0.10.0).** On POSIX the reviewer starts in its own session and a timeout or fence kills the whole group (`killpg`); before, only `claude` died and its children outlived it, and could hold the output pipe open. On Windows the tree kill (`taskkill /T`) now runs before the kill of the parent, which is when it can still find the children.

**A chunk that times out is split, not repeated (0.9.5).** The files of a chunk that hits `OCR_CHUNK_TIMEOUT` are marked; the next push retries them in halves, down to one file, and a file that times out alone twice fails the review with a terminal reason naming it.

**Summary line.** Each verdict now includes a line such as:
```
reviewed 3 files (2 delta, 1 full), re-checked 2 prior findings (1 resolved), carried 41 files
```

**Kill switch.** `OCR_LEDGER=0` disables all ledger reads and writes; every file is reviewed in full on every push. `OCR_FORCE_REVIEW=1` bypasses ledger *reads* (all full for one run) but still writes records afterwards.

**Limits.**
- A carry survives an upstream rebase only for files whose base blob is unchanged.
- Impact analysis (below) finds callers by exact name. Calls through dynamic dispatch, reflection, string-keyed registries or aliases are left to the reviewer's own cross-file step, which still runs.

### Targeted re-checks instead of re-reviews (0.9.0)

Nothing already reviewed is reviewed again. What a change can break elsewhere is checked instead:

- **Impact analysis.** Python lists the symbols each reviewed file changed, including body-only changes and fixes that only delete lines, and `git grep`s the tip for where they are used: first in files that import the changed module, then everywhere. The reviewer gets those call sites, including ones in carried files and in files the push never touched, and answers `ok` / `broken` / `unsure` for each. A broken caller blocks, anchored at the call site (`(caller of changed code)`), and is also kept on that file's own ledger record. With chunked reviews, a chunk's bundle includes callers in *other* chunks' files. Caps: 30 symbols, 5 sites per symbol, 40 sites, 12 KB, allocated round-robin so one widely used name cannot crowd out the rest. `OCR_IMPACT=0` turns it off.
- **Same defect elsewhere.** Where code in the push matches a prior high/medium finding's line, the reviewer is asked whether it is the same defect; a confirmed one blocks (`(same defect as an earlier finding)`). Matches of a *new* finding, and matches in files the push does not touch, are shown as non-blocking `(note)` lines. `OCR_SIBLINGS=0` turns it off.
- **Cost rule.** A file with a delta base is reviewed over its whole push range when that diff is at most 1.5× the delta: about the same cost, and it also shows changes that are only harmful together. There is no longer a fixed cap on how many deltas can chain.
- **Criteria, not mechanics.** The ledger fingerprint covers what the review looks for — model, rubric, language rules, the reviewer and filter prompts, the repo's `.ocr/`, `OCR_MODEL`/`OCR_BLOCK_*`. Orchestration (SKILL.md), CLI args, the resolver prompt and the plugin version no longer invalidate earlier reviews.

**Local run log.** Every review appends one JSON line to `.git/review-gate-telemetry/<date>.jsonl`: file classes and the cost rule's numbers, the symbols and call sites found (and the reviewer's own cross-file step's, side by side), the per-site verdicts, same-defect matches, the resolver's inputs and outcomes, findings with provenance, the verdict and each model call's duration. It stays inside `.git` and holds paths, line numbers and finding text from your code. `python scripts/review-gate.py --telemetry-report [--days N]` summarises it; `OCR_TELEMETRY=0` turns it off.

**Where the time went (0.9.6).** The same line carries time metrics, and `--telemetry-report` summarises them: per model call the turns, API time, cost, time to the first event, tool use, the bytes of the reviewer's Agent-tool inputs and the split between the orchestrator's own time and its Agent calls' wall time; per run the time of each phase (worktree, plan, priors, impact, diffs, review, resolve, recheck, finish) and of each chunk (files, changed lines, outcome); averages and p90. While a review runs, "still running" shows the chunk it is on and the average time per chunk. These keys, and the log lines below, hold **counts, bytes and timings only: never a path or any code**. Separately from `OCR_TELEMETRY`, one line per phase, chunk, call and run is always appended to `review-gate-debug.log` in the plugin data dir (`~/.claude/plugins/data/review-gate-local/` unless `CLAUDE_PLUGIN_DATA` is set); it rotates at 1 MiB and keeps three older files (`.1`-`.3`). `OCR_DEBUG=1` still adds its verbose per-call lines to the same file.


### Optional: Serena MCP (enhanced cross-file analysis for interactive sessions)

Before spawning the reviewer, the orchestrator pre-computes where your changed symbols are
referenced elsewhere in the repo. In headless pre-push runs the gate always uses `git grep`
on committed state (`HEAD`) — MCP servers are intentionally excluded from the gate's isolated
session (see [Cost](#cost)). In interactive `/review-gate:review` sessions, if the
[Serena MCP server](https://github.com/oraios/serena) is connected in Claude Code, the
orchestrator additionally uses language-server-precise symbol resolution for signature-changed
symbols. Removed and renamed names always use `git grep` regardless of Serena availability,
since a language server cannot find references to a symbol that no longer exists — and `git grep`
additionally catches references in configs, templates, and string literals.

No configuration is required. Serena is detected automatically. Without it the plugin is
fully functional.

## Cost

The gate runs the review in a **separate headless `claude -p` session**. That session re-reads its whole context on every tool call, and it makes many of them, so anything loaded into it is paid for repeatedly. The gate therefore isolates it from your interactive environment:

| Isolation | Flag | Why |
|---|---|---|
| **Pinned model** | `--model` (`OCR_MODEL`, default `sonnet`) | Without a pin the review inherits the **parent session's** model. On Opus that is `$0.50/M` cache reads vs Sonnet `$0.30/M` vs Haiku `$0.10/M` — on a read-dominated workload, a straight multiple of your bill. |
| **No user settings** | `--setting-sources ""` | Global hooks live in `~/.claude/settings.json` and would fire on **every tool call** of the review. Auth is unaffected — OAuth/keychain is not a settings source. |
| **Plugin loaded from disk** | `--plugin-dir` | Required, because skipping user settings also skips the plugin registry. |
| **No MCP servers** | `--mcp-config` (empty) + `--strict-mcp-config` | Each connected server's tool schemas cost context in a session that only needs Bash/Read/Grep/Glob. |
| **Stable cache prefix** | `--exclude-dynamic-system-prompt-sections` | Keeps per-machine sections out of the cached prefix. |

**Turning the cost down further:**
- `OCR_MODEL=haiku` — ~5× cheaper cache reads than Opus. Trades some review precision; a gate that emits false positives gets bypassed, and a bypassed gate has zero recall, so weigh it.
- Reviews run **once per push**, not once per commit. A 10-commit push is one review.
- The falsify pass only runs when a finding would actually block, so a clean push never pays for it.

> `OCR_CLAUDE_ARGS` **replaces** these defaults wholesale — using it discards every control above. Prefer `OCR_CLAUDE_EXTRA_ARGS`, which appends.

> **Not** used: `claude --bare`. It would skip hooks, CLAUDE.md, and MCP in a single flag, but it also forces auth to `ANTHROPIC_API_KEY`/`apiKeyHelper` and never reads OAuth — which would break subscription auth and bill the API directly, defeating the point of this plugin.

## How it compares to open-code-review

| | open-code-review (ocr) | claude-code-review-gate |
|---|---|---|
| Runs as | external Go binary | native Claude Code skill + subagents |
| Auth on a subscription | borrows the token (ToS-blocked) | Claude Code's own auth (compliant) |
| Review unit | one isolated session per file | one reviewer for the change set (fan out >15 files) |
| Cross-file defects | only if the model calls a lookup tool | in scope by default; cross-file claims require cited `Grep` evidence |
| Falsify pass | separate LLM call per file | separate subagent, gated on would-block |
| Severity / confidence | not in the schema | first-class, drives the gate |
| Push blocking | left to the caller | deterministic verdict + gate |
| Line anchoring | fuzzy diff matching + LLM re-anchor | true line numbers from real file reads, plus a string-match hallucination check |

## FAQ

**Why not just use open-code-review directly?** On a Claude subscription you'd have to feed your token to ocr, which Anthropic blocks as a ToS violation. This plugin delivers the same methodology compliantly, inside Claude Code.

**Does this use my Claude subscription? Is that allowed?** Yes and yes — the reviewing is done by Claude Code itself (the official client) via headless `claude -p`, which is the supported way to script it. Nothing is sent to ocr or any third party.

**Will it block my pushes? How do I bypass?** By default it blocks confident high-severity findings. Use `git push --no-verify` for a one-off, `OCR_ADVISORY=1` (or `.ocr/config.json` `{"blocking": false}`) for warn-only, or uninstall the global hook.

**What happens if the review times out or crashes?** It **blocks** — the gate fails closed, so a broken reviewer can't silently wave changes through. The same applies to a missing Python 3 or a reviewer the git hook can't locate after an upgrade. `OCR_FAIL_OPEN=1` is the emergency bypass; in hook mode it must be exported in the environment Claude Code itself was launched from, since a shell prefix on `git push` never reaches the hook process. The full list of what still fails open is in [Safety & limitations](#safety--limitations).

**Why is it expensive / how do I make it cheaper?** See [Cost](#cost). The short version: set `OCR_MODEL=haiku`, and make sure you're on a current install — pre-0.2 installed a per-**commit** hook that also bypassed the cost controls.

**Is it affiliated with Alibaba or Anthropic?** No.

## Safety & limitations

- **Fails closed** by design — a timeout, crash, unparseable review, missing Python 3, or an unlocatable reviewer **blocks** the push. Bypass with `OCR_FAIL_OPEN=1`, or downgrade permanently with `OCR_ADVISORY=1`.
- **Never allows an unreviewed push while its review is still running** — past the inline budget it denies and tells the caller to re-run the push; see [Long reviews](#long-reviews-and-the-desktop-app).
- **It still fails open in these cases**, and it is worth knowing which:
  - **`claude` is not installed** — deliberate; there is no gate without the tool.
  - **The hook fails to launch, times out, or dies abnormally.** Claude Code treats a hook it could not start or had to kill as *non-blocking*, and no code inside the hook can change that. On Windows this is why both a Git Bash and a PowerShell adapter are registered — if neither can start, the gate is silently absent. Run `/review-gate:doctor` to check.
  - **`OCR_FAIL_OPEN=1` or `OCR_ADVISORY=1`** — the intended escape hatches.
- **Two structural limits**: `git push --no-verify` skips the git-hook wiring entirely, and pushing from a terminal skips the plugin wiring unless you installed the global hook.
- **Carried files are not re-reviewed** — a behavioural interaction between a fix and a carried file (no blob change in the carried file) is outside the scope of the incremental check. Use `OCR_FORCE_REVIEW=1` for a ground-truth sweep after a large refactor.
- AI review is **advisory assistance, not a guarantee** — it complements, not replaces, tests and human review.
- **Full-file scans can be token-heavy** on large repos; a 40-file ceiling keeps per-push cost bounded. See [Cost](#cost) for the per-session controls.
- The **global git hook affects all push paths** — read the install warning.
- `severity`/`confidence` are model-estimated, not calibrated; tune the thresholds if blocking is too eager or too lax.
- **Cross-file findings are only as good as the reviewer's `Grep` evidence.** The orchestrator drops unverified cross-file claims, which trades some recall for precision.

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Note: changes that touch the open-code-review-derived prompt text or rubric must preserve the attribution headers and the [NOTICE](NOTICE) file.

## License & Credits

Licensed under the [Apache License 2.0](LICENSE).

The review methodology, scope rules, the "falsify, don't verify" filter, and the review rubric are **adapted from [open-code-review](https://github.com/alibaba/open-code-review)** (Apache-2.0). This project re-expresses that methodology natively inside Claude Code and adds a severity/confidence schema and a deterministic verdict gate. See [NOTICE](NOTICE) for full attribution. Not affiliated with Alibaba or Anthropic.
