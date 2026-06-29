<!--
  The multi-phase review methodology orchestrated here (per-file isolated review,
  plan-for-large-diffs, "falsify, don't verify" filtering, cross-file dedup, and
  project summary) is adapted from open-code-review (ocr):
  https://github.com/alibaba/open-code-review — Apache License, Version 2.0.
  Modified for this project: re-expressed as a native Claude Code orchestrator
  that fans out per-file subagents and emits a severity/confidence verdict.
  See the repository NOTICE file for full attribution.
-->
---
name: review
description: AI code review of your changes (open-code-review methodology, run natively in Claude Code). Reviews the working diff or staged changes, or scans whole files, and prints findings plus a block/warn/pass verdict. Use for "review my changes", "review staged", "scan this repo", or as the engine behind the commit gate.
---

# subscription-code-review — orchestrator

You are orchestrating an AI code review by fanning out one isolated
`code-reviewer` subagent per file. Follow these steps exactly.

## 0. Parse arguments (`$ARGUMENTS`)

- `--staged` — review staged changes only (`git diff --staged`). Used by the commit gate.
- `--scan` — full-file scan of the repo (or of `paths` if given) instead of a diff review.
- `--json` — print ONLY the machine-readable JSON output object (no prose). The
  commit gate relies on this. Without it, print a human-readable report.
- `--rule <path>` — explicit rule file (highest precedence).
- `--summary` — also produce a project summary (implied by `--scan`).
- Any non-flag arguments are treated as path filters (files or directories).

## 1. Select files

- Default (working review): `git diff --name-only` plus untracked files
  (`git ls-files --others --exclude-standard`).
- `--staged`: `git diff --staged --name-only`.
- `--scan`: `git ls-files` (optionally filtered by the given `paths`).
- Apply `allowlist.md`: keep only allowed extensions; drop default exclusions
  (tests, vendored/generated, lockfiles, VCS/tooling). Skip binary files and
  pure deletions.
- **Safety ceiling:** if more than **40** files remain, review the 40 with the
  largest diffs and record a warning that the rest were skipped (keeps cost and
  latency bounded — this runs on every commit).

If no files survive selection: emit a `skipped` result (see §6) and stop.

## 2. Resolve the rule (review checklist) per file

Precedence, highest first:
1. `--rule <path>` if given.
2. Project rule: nearest `.ocr/rule.json` walking up from the file.
3. Global rule: `~/.ocr/rule.json` (or `$OCR_RULE_FILE` if set).
4. System default: `${CLAUDE_PLUGIN_ROOT}/skills/review/rubric.md`.

A rule file may map glob patterns to checklist text and may set `merge: true` to
prepend the system rubric. If no override matches a file, use the system rubric.

## 3. Fan out one subagent per file (in parallel)

For each selected file, spawn the **`code-reviewer`** subagent via the Task tool.
**Launch them in parallel, at most ~6–8 in flight at once** (start the next as
each returns). Pass each subagent a prompt containing:

- `mode`: `review` or `scan`
- `path`: the file path
- `diff`: that file's unified diff — `git diff [--staged] -- <path>` (review
  mode). For an untracked file, synthesize an all-added diff. Omit in scan mode.
- `other_changed_files`: the other selected paths
- `rubric`: the resolved checklist for this file (§2)
- `requirement_background`: optional, if the user supplied one
- `repo_root`: the absolute repository root

Each subagent returns a JSON array of findings. Parse each; if a subagent returns
non-JSON or errors, record a warning for that file and continue (never abort the
whole run for one file).

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
