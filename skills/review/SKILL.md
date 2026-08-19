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
  by the pre-push gate. File list: `git diff -M @{u}..HEAD --name-status`; falls back
  to `git diff -M origin/main..HEAD --name-status` if no upstream tracking branch is
  set. Per-file diff: `git diff -M @{u}..HEAD -- <path>` (or the fallback ref).
- `--scan` — full-file scan of the repo (or of `paths` if given) instead of a diff review.
- `--json` — print ONLY the machine-readable JSON output object (no prose). The
  push gate relies on this. Without it, print a human-readable report.
- `--rule <path>` — explicit rule file (highest precedence).
- `--summary` — also produce a project summary (implied by `--scan`).
- Any non-flag arguments are treated as path filters (files or directories).

## 1. Select files and collect diffs

**Determine the revision range** for `--unpushed`:
1. Try `git rev-parse @{u}` — if it succeeds, use `@{u}..HEAD` as `<range>`.
2. If it fails (no upstream tracking branch), run
   `git remote show origin | grep 'HEAD branch'` to get the default branch name,
   then use `origin/<defaultBranch>..HEAD` as `<range>`.
3. If both fail, fall back to `origin/main..HEAD`.

**File list — pass `-M` (rename detection) everywhere:**
- Default (working review): `git diff -M --name-status` plus untracked files
  (`git ls-files --others --exclude-standard`).
- `--staged`: `git diff -M --staged --name-status`.
- `--unpushed`: `git diff -M --name-status <range>`.
- `--scan`: `git ls-files` (optionally filtered by the given `paths`).

Parse `name-status` output: `R*` lines are renames — record both the old path and
the new path. For renames, per-file diff uses: `git diff -M <range> -- <old-path> <new-path>`.
Treat the **new path** as the canonical file path for the review; note the old path for
symbol extraction in §2b.

Apply `allowlist.md`: keep only allowed extensions; drop default exclusions
(tests, vendored/generated, lockfiles, VCS/tooling). Skip binary files and
pure deletions.

**Safety ceiling:** if more than **40** files remain, review the 40 with the largest
diffs and record a warning that the rest were skipped.

If no files survive selection: emit a `skipped` result (see §6) and stop.

**Collection-time oversize guard (before fetching full diffs):**

Run `git diff -M --numstat <range>` (or the appropriate variant for the mode) to get
added/deleted line counts per file without fetching content. For any file where
`added + deleted > 400` lines:
- Fetch `git diff -M -U0 <range> -- <path>` (zero-context: change lines only, no
  surrounding context). Mark the file `oversize: true`.
- For renamed files: `git diff -M -U0 <range> -- <old-path> <new-path>`.

For all other files fetch `git diff -M <range> -- <path>` normally.

For untracked files (in working-review mode): synthesize an all-added diff from the
file content. If the file content exceeds 400 lines, read only hunk structure (treat
it as oversize: use `head -n 400` equivalent, mark `oversize: true`).

**Retain all diffs in memory.** §2b and §3 both consume them; do not re-run
`git diff` for the same file. §2b runs on these in-memory diffs before §3 truncation.

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

## 2b. Symbol context extraction

**Purpose:** pre-compute where changed symbols are referenced outside the change set,
so the reviewer gets curated, bounded cross-file evidence instead of grepping freely.

Skip this entire step in `--scan` mode. Proceed to §3 with
`cross_file_context` absent (not emitted) — the rules file's fallback handles this.

---

**Step 1 — Extract changed symbols from in-memory diffs (no file reads):**

Scan each file's in-memory diff. Consider only lines starting with `-` or `+` (not
space-prefixed context lines). Do **not** process `--` file header lines.

Extract symbols in exactly these three categories. Definition lines are:
`def <name>(`, `class <name>[(:]`, `function <name>(`, `fn <name>(`,
`const <name> =` / `let <name> =` / `export const <name>`,
top-level shell function `<name>()`, `Function <name>` / `function <name>` (PowerShell).
Only consider lines at the top level (column 0 after the `+`/`-` prefix) or at
class level (indented by exactly one indent level). Ignore local/nested definitions.

Categories:
- `removed` — a `-` definition line with no corresponding `+` definition for the
  **same name** in the same file's diff.
- `renamed` — a removed definition where a `+` line in the **same diff hunk**
  introduces a definition with a **different name** and an identical or near-identical
  parameter list. When unsure, classify as `removed` (both tiers search the old name —
  no coverage is lost).
- `signature_changed` — the same name appears on both a `-` and a `+` definition
  line in the same file with a **different parameter list**. Use only the first line
  of multi-line parameter lists.

Skip: same name + same parameter list on `-` and `+` (moved within file). Removed
import lines. Added-only symbols. Symbols in new files. When unsure if a line is
a definition, skip it.

**Cap:** max 3 symbols per file, max 10 symbols total. Priority:
removed > renamed > signature_changed; within tier, prefer exported/public names.
When the cap fires, record the count of dropped symbols as `symbols_dropped`.

If 0 symbols extracted → skip Steps 2–4, proceed to §3 with:
```json
{"serena_used": false, "truncated": false, "symbols_dropped": 0, "symbols": []}
```
Always emit `cross_file_context` (absence means extraction was skipped, which has a
different meaning in the rules file).

---

**Step 2 — Detect Serena availability (interactive sessions only):**

Check own tool list. If `mcp__serena__find_referencing_symbols` is visible → Serena
is available for `signature_changed` symbols. If not visible, use the git-grep path
for all symbols. MCP servers are intentionally absent in headless gate runs
(`--strict-mcp-config`), so Serena is never available there.

`removed` and `renamed` symbols (old names) **always use the git-grep path**,
regardless of Serena availability. A language server cannot find references to a
symbol that no longer exists; `git grep` also catches string/config/template
references.

If any Serena call errors or returns empty results for a symbol whose definition is
visible in the diff → abandon Serena for this review; use git-grep for all remaining
symbols.

---

**Step 3 — Serena path (signature_changed symbols only, when Serena is available):**

Exactly these calls per symbol, no others:
1. `mcp__serena__activate_project(repo_root)` — once per review (skip if already
   activated earlier in this session).
2. `mcp__serena__find_symbol(name, relative_path_of_defining_file)`.
3. `mcp__serena__find_referencing_symbols(...)` on that result.

Keep only refs in files **not** in the current change set. Record as
`{path, line, snippet}` (line ±3 lines). Do not call `get_symbols_overview` or
Read any files.

---

**Step 4 — Git-grep path (removed/renamed always; all symbols when Serena unavailable):**

For each symbol, search its **old name** via Bash:
```
git grep -n "\b<name>\b" HEAD
```

Skip the search and record `note: "name too generic"` if the name is fewer than 4
characters or is a common word: `get`, `set`, `run`, `data`, `main`, `init`, `key`,
`value`, `name`, `id`.

Regex-escape the name; if it starts or ends with a non-word character, drop the
corresponding `\b`.

Discard hits in files that are in the current change set.

Result handling:
- 0 external hits → `external_refs: []`. (Positive evidence: no incomplete refactor.)
- > 10 external files → record `ref_count_note: "<N> files reference this name"` and
  the first 3 paths as `sample_files`. No further git-grep calls for this symbol.
- 1–10 files → for each non-test file up to 5 (max 2 test files, tagged
  `in_tests: true`): `git grep -n -C 3 "\b<name>\b" HEAD -- <file>` (keep at most
  the first 20 matching lines). Never Read these files.

**Global call ceiling:** max 20 git-grep Bash calls total across all symbols in §2b.
When the ceiling is hit, remaining symbols get `note: "search budget exhausted"` and
their count is added to `symbols_dropped`.

---

**Step 5 — Assemble `cross_file_context`:**

Caps (applied in order):
- Max 5 `external_refs` per symbol.
- Max 15 snippets total across all symbols.
- Each snippet ≤ 12 lines.
- Whole bundle ≤ 10,000 characters.

When the character cap binds: drop `in_tests: true` snippets first, then trim
round-robin across symbols, preserving at least 1 ref per symbol that has any.
Set `truncated: true` when any cap fires.

Schema:
```json
{
  "serena_used": false,
  "truncated": false,
  "symbols_dropped": 0,
  "symbols": [
    {
      "name": "old_name",
      "kind": "function|method|class|constant",
      "change": "removed|renamed|signature_changed",
      "renamed_to": "new_name",
      "defined_in": "scripts/foo.py",
      "old_signature": "def old(x, y):",
      "new_signature": "def new(x, y, z):",
      "external_refs": [
        {"path": "scripts/bar.py", "line": 42, "snippet": "...", "in_tests": false}
      ],
      "ref_count_note": null,
      "sample_files": [],
      "note": null
    }
  ]
}
```

All paths are repo-relative with forward slashes.

**Large-diff escalation (when §3 spawns multiple group subagents):** each group
subagent receives the full `symbols` list filtered to entries whose `defined_in` is
in that group OR that have `external_refs` into that group.

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

**Apply diff size caps before building the reviewer prompt:**

- **Single-file cap:** For files marked `oversize: true`, use the `-U0` diff already
  collected, plus the `git diff --stat` line for that file, and set `diff_truncated: true`.
  For any file not already marked oversize that still has a diff exceeding 400 lines or
  16 KB, re-fetch with `-U0` and mark it `diff_truncated: true`.
- **Total budget:** if the sum of all embedded diff content exceeds 1,500 lines, degrade
  the largest files first: replace their full diff with stat + hunk headers only and set
  `diff_truncated: true`. **Never fully omit a file's entry.** A degraded entry is still
  an entry — the reviewer knows the file changed, sees hunk locations, and can Read it
  (see reviewer instructions for `diff_truncated` handling).

**Default path (≤ 15 files):** spawn **one** `code-reviewer` subagent via the
Agent tool. Pass it a prompt containing:

- `mode`: `review` or `scan`
- `files`: a JSON-style list of `{path, diff, language_rules, diff_truncated}` objects —
  one per selected file, where `diff` is the collected diff (subject to caps above),
  `language_rules` is the combined language-specific + LLM-authored rule text resolved
  in §2, and `diff_truncated` is `true` when that file's diff was capped (omit the field
  when `false`). For untracked files synthesize an all-added diff; omit `diff` in scan mode.
  Collect all per-file diffs and rules yourself before spawning.
- `rubric`: the resolved checklist from §2. If per-file overrides exist, note
  them inline next to the relevant file entries.
- `cross_file_context`: the bundle assembled in §2b (absent for `--scan` mode).
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
the same fields as above but with only its group's `files` list (with caps
applied); add an `other_changed_dirs` field listing the other groups'
directories so the reviewer has cross-group awareness. Pass the filtered
`cross_file_context` bundle (see §2b Step 5) for that group.

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

Evidence quoting a `cross_file_context` snippet verbatim counts as verified; check it
against the bundle, not against the file.

## 3b. Filter pass (independent falsify)

**Skip this step entirely** unless at least one finding has
`severity == "high"` AND `confidence >= 0.7` — i.e. unless the verdict would
otherwise be `block`. The filter exists to protect against a false-positive
block; when nothing would block, it costs a subagent context load and cannot
change the outcome.

Assign each surviving finding a temporary id (`"f-0"`, `"f-1"`, …).

Spawn a `code-filter` subagent via the Agent tool, with `run_in_background: false`
(see the note above §3 — step 4/6 depend on its result next). Pass it a prompt containing:
- `diffs`: for each **candidate blocking finding** (severity `high`, confidence ≥ 0.7),
  include only the diff(s) of the file(s) that finding cites, subject to the same per-file
  cap as §3. Do not include diffs of files with no candidate blocking findings.
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

**Unified truncation warnings — emit one warning entry for each of the following:**
- Any file with `diff_truncated: true`:
  `{file: "<path>", message: "diff truncated; reviewer saw stat + hunk headers only"}`
- `cross_file_context.truncated == true`:
  `{file: null, message: "cross-file symbol analysis truncated; some external usages may be unverified"}`
- `cross_file_context.symbols_dropped > 0`:
  `{file: null, message: "symbol extraction capped; <N> changed symbols were not searched for external references"}`

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
