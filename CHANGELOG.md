# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.1] - 2026-08-14

### Fixed
- **An incomplete finding can no longer block a push on its own.**
  `compute_verdict` decided block/warn/pass from severity + confidence alone,
  with no check that a finding carries anything a human can act on. In practice
  findings have come back with `severity`/`confidence`/`path` but no
  `content`/`start_line`/`end_line` — the reviewing LLM dropping fields while
  retyping findings through the filter/dedup/hallucination-check steps. Those
  blocked a push while telling the developer nothing. `_is_actionable()` is now
  required alongside the severity/confidence check before returning `block`; an
  incomplete finding still counts toward `warn` and stays fully visible. The
  skill's aggregation step is also hardened to carry every field forward
  unchanged rather than retyping findings by hand.
- **Raw reviewer output is persisted, and incomplete findings say so.**
  `_format_reasons` used to render a field-less finding as a bare `path:? - `
  with nothing after the dash, which reads as display truncation rather than a
  defect in the review — with no way to see what the reviewer actually said
  short of re-running it. Every run now dumps `claude`'s raw stdout to
  `<git-dir>/review-gate-last-output.json` (inside `.git`, never tracked),
  referenced from both the block and advisory messages.
- **The verdict JSON is parsed by a balanced-brace scan, not a first/last span.**
  `json.loads(text[find("{"):rfind("}")+1])` grabbed the *last* `}` anywhere in
  the model's output, including one in trailing prose appended after the verdict
  — turning a valid verdict into an unparseable-output failure. Now
  `JSONDecoder.raw_decode` runs from each `{` in turn, keeping the last dict
  carrying `findings` as the answer.
- **A failed `claude` invocation is no longer reported as a bad review.**
  `_run_review` never checked the subprocess exit code, so every failure mode
  collapsed into "could not parse review output" — which points at the review
  skill when the actual fault is the CLI. An expired login is the common case:
  `claude` prints `Failed to authenticate: OAuth session expired` to **stdout**,
  where the gate expects JSON. The gate now reports the exit code, which binary
  ran, and its output, and recognizes credential failures specifically. Note a
  Claude Code Desktop session refreshes its auth in-process, so the app keeps
  working while the on-disk credentials the CLI reads go stale — the gate breaks
  with no visible sign anything logged out. Still fails CLOSED: an unusable
  login must not become a silent bypass.

### Added
- **`bin/sync-local-install.py`** — refreshes the local plugin snapshot from the
  working tree. When the plugin is installed from a `directory` marketplace,
  Claude Code runs a version-pinned *copy* under `~/.claude/plugins/cache/`, not
  your checkout, and `${CLAUDE_PLUGIN_ROOT}` resolves there — so committing a fix
  changes nothing the gate enforces until the snapshot is refreshed, with no
  warning that the two have diverged. `--check` reports drift (exit 1), `--prune`
  drops superseded snapshots. The global git hook is unaffected: it bakes in an
  absolute path to `bin/` and always runs live code, so the two gates can
  silently disagree about which version is in force.
- `tests/test_review_gate.py` — unit tests for the `_extract_json` balanced-brace
  parser: the trailing-prose stray-brace case that motivated the fix, plus
  whole-string JSON, fenced blocks, unrelated JSON without `findings`,
  last-match-wins, and no-JSON/malformed/empty inputs.

## [0.2.0] - 2026-08-10

### Changed — BREAKING
- **The gate is now pre-push, not pre-commit.** It reviews every unpushed commit
  once per push (`git diff @{u}..HEAD`) instead of reviewing staged changes on
  every commit. A 10-commit push is one review.
- **`bin/install-git-hook.sh` now installs `pre-push`** (`bin/pre-push` replaces
  `bin/pre-commit`). **Re-run the installer if you installed before this
  release**: the old `pre-commit` hook still fires on every commit, and because
  the gate now reviews `@{u}..HEAD` it reviews the *wrong* state at commit time
  — during a pre-commit hook the new commit does not exist yet, so `HEAD` is
  still its parent. The installer detects and removes the stale hook.
- **The gate fails CLOSED**, not open: a timeout, crash, or unparseable review
  now blocks rather than allowing the push. Only a missing `claude` binary still
  fails open. `OCR_FAIL_OPEN=1` is the emergency bypass. README and
  `hooks/hooks.json` previously documented the old fail-open behavior.
- `OCR_TIMEOUT` default raised 600s → 1800s; the `hooks.json` harness timeout
  must stay above it (currently 1920s).

### Added
- **Cost isolation for the headless review session.** It now pins its model
  (`OCR_MODEL`, default `sonnet`) instead of inheriting the parent session's,
  loads only project settings (`--setting-sources project`, so global hooks do
  not fire on every tool call), loads the plugin from disk (`--plugin-dir`),
  connects no MCP servers (`--strict-mcp-config`), and keeps per-machine
  sections out of the cached prefix. See the README's Cost section.
- `OCR_CLAUDE_EXTRA_ARGS` — appends to the default `claude` flags.
  `OCR_CLAUDE_ARGS` still replaces them wholesale, which discards the cost
  controls; the bundled git hook now uses the appending form.
- Per-language review rules (`skills/review/rules/{python,typescript,go,rust}.md`)
  and `rules/llm-authored-code.md`, appended to the rubric per file.
- Independent falsify pass: a `code-filter` subagent with a fresh context gets
  only the diff and the findings and drops a finding solely on direct
  counter-evidence. Runs only when a finding would actually block.
- `evidence` field on findings; cross-file claims now require cited `Grep`
  results or the orchestrator drops them.
- Hallucination check: a finding whose `existing_code` is absent from the diff
  and the file has its confidence downgraded.
- `bin/uninstall-git-hook.sh` now removes the hook files it installed (both
  `pre-push` and a legacy `pre-commit`), not just the `core.hooksPath` setting.

### Changed
- Single reviewer for the whole change set instead of one subagent per file, so
  cross-file defects are visible. Diffs over 15 files fan out by directory.

### Added
- Extended file-type allowlist to match OCR v1.7.11–v1.7.13: FreeMarker
  (`.ftl`, `.ftlh`, `.ftlx`), gettext translation files (`.po`, `.pot`),
  Astro (`.astro`), fish shell (`.fish`), Python stubs (`.pyi`),
  Objective-C (`.m`, `.mm`), VB.NET/F# (`.vb`, `.fs`), Erlang headers/ETS
  (`.hrl`, `.ets`), Ruby build files (`.rake`, `.gemspec`), and `.htm`.
- Sample FreeMarker and PO/POT checklists in `examples/.ocr/rule.json`.

## [0.1.0] - 2026-06-29

### Added
- Orchestrator skill `/review-gate:review` — diff review, `--staged`,
  full-file `--scan`, `--json`, `--rule`, `--summary`.
- Per-file `code-reviewer` subagent (isolated context, parallel fan-out) that reads
  real files, folds plan → main → "falsify, don't verify" in-session, and emits a
  severity/confidence finding schema.
- Commit gate: default Claude Code PreToolUse hook, plus an optional global git hook
  (`bin/install-git-hook.sh`) for every-commit-everywhere coverage.
- Deterministic verdict parser (`bin/ocr_verdict.py`) and fail-open gate core
  (`bin/review-gate.py`).
- Rule hierarchy, allowlist/exclusions, finding JSON Schema, sample `.ocr/rule.json`.
- Apache-2.0 license with NOTICE attributing open-code-review.

[Unreleased]: https://github.com/Jose-Ribeir/claude-code-review-gate/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/Jose-Ribeir/claude-code-review-gate/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Jose-Ribeir/claude-code-review-gate/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Jose-Ribeir/claude-code-review-gate/releases/tag/v0.1.0
