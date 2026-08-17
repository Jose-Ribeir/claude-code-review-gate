---
name: review
description: AI code review of your changes (open-code-review methodology, run natively in Claude Code). Reviews the working diff or staged changes, or scans whole files, and prints findings plus a block/warn/pass verdict. Use for "review my changes", "review staged", "scan this repo", or as the engine behind the commit gate.
---

<!--
  The multi-phase review methodology orchestrated here (single-reviewer with
  cross-file visibility, plan-for-large-diffs, "falsify, don't verify" filtering,
  cross-file dedup, and project summary) is adapted from open-code-review (ocr):
  https://github.com/alibaba/open-code-review — Apache License, Version 2.0.
  Modified for this project: re-expressed as a native Claude Code orchestrator
  that spawns one independent reviewer subagent for the full change set and emits
  a severity/confidence verdict. Cross-file fan-out is reserved for large diffs
  (>15 files) where a single context would be diluted.
  See the repository NOTICE file for full attribution.
-->

# review-gate — orchestrator

You are orchestrating an AI code review. Follow these steps exactly.

## 0. Parse arguments (`$ARGUMENTS`)

- `--staged` — review staged changes only (`git diff --staged`).
- `--unpushed` — review all commits not yet pushed to the upstream branch. Used
  by the pre-push gate. File list: `git diff @{u}..HEAD --name-only`; falls back
  to `git diff origin/main..HEAD --name-only` if no upstream tracking branch is
  set. Per-file diff: `git diff @{u}..HEAD -- <path>` (or the fallback ref).
- `--scan` — full-file scan of the repo (or of `paths` if given) instead of a diff review.
- `--json` — print ONLY the machine-readable JSON output object (no prose). The
  push gate relies on this. Without it, print a human-readable report.
- `--rule <path>` — explicit rule file (highest precedence).
- `--summary` — also produce a project summary (implied by `--scan`).
- Any non-flag arguments are treated as path filters (files or directories).

## 1. Select files

- Default (working review): `git diff --name-only` plus untracked files
  (`git ls-files --others --exclude-standard`).
- `--staged`: `git diff --staged --name-only`.
- `--unpushed`: `git diff @{u}..HEAD --name-only`. If that fails (no upstream
  tracking branch), fall back to `git diff origin/main..HEAD --name-only`, then
  `git diff origin/master..HEAD --name-only`. Use the same ref for per-file diffs.
- `--scan`: `git ls-files` (optionally filtered by the given `paths`).
- Apply `allowlist.md`: keep only allowed extensions; drop default exclusions
  (tests, vendored/generated, lockfiles, VCS/tooling). Skip binary files and
  pure deletions.
- **Safety ceiling:** if more than **40** files remain, review the 40 with the
  largest diffs and record a warning that the rest were skipped.

If no files survive selection: emit a `skipped` result (see §6) and stop.

## 2. Resolve the rule (review checklist) per file

Precedence, highest first:
1. `--rule <path>` if given.
2. Project rule: nearest `.ocr/rule.json` walking up from the file.
3. Global rule: `~/.ocr/rule.json` (or `$OCR_RULE_FILE` if set).
4. System default: `${CLAUDE_PLUGIN_ROOT}/skills/review/rubric.md`.

A rule file may map glob patterns to checklist text and may set `merge: true` to
prepend the system rubric. If no override matches a file, use the system rubric.

After resolving the base rubric for each file, also apply:

5. **Language rule:** load `${CLAUDE_PLUGIN_ROOT}/skills/review/rules/<lang>.md`
   matched by file extension and append it to the resolved rubric:
   - `.py`, `.pyi` → `python.md`
   - `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs` → `typescript.md`
   - `.go` → `go.md`
   - `.rs` → `rust.md`
   If no mapping matches, skip.

6. **LLM-authored rule:** always append
   `${CLAUDE_PLUGIN_ROOT}/skills/review/rules/llm-authored-code.md` to the rubric
   for every file (this repo is primarily LLM-authored; remove this step for
   non-LLM projects by adding `"llm_authored": false` to `.ocr/config.json`).

For the single-reviewer case (≤15 files), include per-file language and LLM rules
as a `language_rules` note alongside each entry in the `files` list passed to the
subagent.

## 3. Spawn the reviewer subagent

Every Agent tool call in this skill — the reviewer(s) here, and the filter
agent in §3b — **must use `run_in_background: false`**. The step right after
each spawn (parse its JSON, then 3a/3b/4/6) depends on that agent's result as
its very next action, which is exactly the case the Agent tool itself says
warrants foreground execution. Backgrounding it is worse than inefficient
here: a backgrounded agent's result arrives later as an async task
notification, in a turn of its own. The pre-push gate runs this skill
headlessly via `claude -p`, which reports only your last completed turn as
its result. If any such notification — including a stray duplicate — lands
*after* you've already printed the `--json` verdict in step 6, you will
produce one more (harmless-looking) turn acknowledging it, and that turn's
prose becomes the entire captured output, silently replacing the JSON and
false-blocking the push with an unparseable-output error. Running every
subagent in the foreground removes this race structurally: there is no later
turn for a stray notification to land in, because you cannot proceed past
the spawn until the real result is already in hand.

**Default path (≤ 15 files):** spawn **one** `code-reviewer` subagent via the
Agent tool. Pass it a prompt containing:

- `mode`: `review` or `scan`
- `files`: a JSON-style list of `{path, diff, language_rules}` objects — one per
  selected file, where `diff` is the output of `git diff [--staged] -- <path>`
  (for untracked files synthesize an all-added diff; omit `diff` in scan mode),
  and `language_rules` is the combined language-specific + LLM-authored rule text
  resolved in §2. Collect all per-file diffs and rules yourself before spawning.
- `rubric`: the resolved checklist from §2. If per-file overrides exist, note
  them inline next to the relevant file entries.
- `requirement_background`: optional, if the user supplied one.
- `repo_root`: the absolute repository root.

The single reviewer sees the full change set and must catch both per-file bugs
and cross-file inconsistencies (renamed symbols, removed mechanisms that still
exist in other files, caller/callee drift). This is intentional.

**Large-diff escalation (> 15 files):** group the selected files by top-level
directory (first path segment). Spawn one `code-reviewer` subagent per group,
running at most **4 groups in parallel** — issue all of them as parallel
Agent tool-use blocks within a single message (per the "run agents in
parallel" pattern), each with `run_in_background: false`. Do not let any
group run in the background: per the note above §3, a single backgrounded
group is enough to trigger the race, and with up to 4 groups in flight the
odds of a stray/duplicate notification only go up. Pass each group subagent
the same fields as above but with only its group's `files` list; add an
`other_changed_dirs` field listing the other groups' directories so the reviewer
has cross-group awareness.

The subagent (or each group subagent) returns a JSON array of findings, each
with a `path` field. Parse the result; if the subagent returns non-JSON or
errors, record a warning for all its files and continue (never abort for one
error).

**Carry every field forward unchanged.** Steps 3a-6 below have you filter,
downgrade, and re-render findings, which means retyping each finding object
by hand rather than passing the subagent's JSON straight through. When you do,
copy every field of a finding verbatim (`path`, `start_line`, `end_line`,
`content`, etc.) — do not paraphrase, shorten, or silently drop a field for
any finding, no matter how far down the array it sits or how large the change
set is. A finding missing `content` or its line numbers is unusable to the
person reading the verdict, and `compute_verdict` will refuse to let an
incomplete finding block a push on its own — so a dropped field doesn't just
degrade the report, it silently weakens the gate.

## 3a. Hallucination check

For each finding that has a non-empty `existing_code`:
- Search the corresponding file's diff text for that string (normalise whitespace
  before comparing).
- If `existing_code` does not appear anywhere in that file's diff **and** does not
  appear in the current file content (use `Read` on the file to check): downgrade
  the finding's `confidence` by `0.3` and record a warning
  `{file, message: "existing_code not found in diff or file — confidence downgraded"}`.
- A finding whose confidence drops below `0.7` no longer triggers `block`.

This is a cheap string-match hallucination detector — a finding quoting code that
doesn't exist in the change set is evidence of a hallucinated anchor.

## 3b. Filter pass (independent falsify)

**Skip this step entirely** unless at least one finding has
`severity == "high"` AND `confidence >= 0.7` — i.e. unless the verdict would
otherwise be `block`. The filter exists to protect against a false-positive
block; when nothing would block, it costs a subagent context load and cannot
change the outcome.

Assign each surviving finding a temporary id (`"f-0"`, `"f-1"`, …).

Spawn a `code-filter` subagent via the Agent tool, with `run_in_background: false`
(see the note above §3 — step 4/6 depend on its result next). Pass it a prompt containing:
- `diffs`: the combined raw diff text for all reviewed files (concatenated).
- `findings`: the findings JSON array with the temporary ids attached.

The filter agent runs in its own fresh context — it never sees the reviewer's
reasoning, so its falsify pass is genuinely independent. It returns
`{"drop_ids": [...]}`. Remove all findings whose id is in `drop_ids` before
proceeding to dedup and verdict.

If the filter call fails or returns non-JSON, log a warning and continue with all
findings (never abort for a filter error).

## 4. Global dedup (only if ≥ 4 total findings)

Across all findings, cluster ones that make the **same claim** (e.g. the same
missing check repeated across files). Keep one canonical finding per cluster and
drop the rest. Do **not** merge distinct issues just because they sit near each
other or share a file. Preserve per-file detail when severities differ.

## 5. Project summary (only with `--scan` or `--summary`)

Produce a concise markdown summary: **Top Issues** (5–10, by impact, grouping
repeated root causes), **Module Hotspots** (paths with high density/severity),
**Cross-Cutting Concerns** (patterns across files, with representative paths),
**Quick Wins**. Do not restate every finding.

## 6. Compute the verdict and render

Tally `high`/`medium`/`low`. Determine `verdict`:
- `block` if any finding has `severity == "high"` AND `confidence >= 0.7`;
- else `warn` if any `high` or `medium` exists;
- else `pass`.

`status`: `completed_with_errors` if any subagent errored; else
`completed_with_warnings` if warnings exist; else `success`; `skipped` if no
files were reviewable.

**If `--json`:** print ONLY this object (no other text):
```json
{
  "status": "...",
  "verdict": "block | warn | pass",
  "summary": {"files_reviewed": 0, "findings": 0, "high": 0, "medium": 0, "low": 0},
  "findings": [ /* Finding objects, schema in schemas/finding.schema.json */ ],
  "project_summary": "optional markdown (scan/summary only)",
  "warnings": [ {"file": "...", "message": "..."} ]
}
```

**Otherwise (human mode):** print a readable report — group findings by file,
show `path:start-end  [severity/confidence] (category)` then the content and any
suggestion, then a one-line footer: `verdict: <…>  (N high, M medium, K low across F files)`.

Never modify files. This skill only reads and reports.
