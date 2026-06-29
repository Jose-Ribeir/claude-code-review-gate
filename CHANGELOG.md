# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-06-29

### Added
- Orchestrator skill `/subscription-code-review:review` — diff review, `--staged`,
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

[Unreleased]: https://github.com/OWNER/subscription-code-review/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/OWNER/subscription-code-review/releases/tag/v0.1.0
