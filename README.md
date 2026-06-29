# claude-code-review-gate — Blocking AI code-review gate for Claude Code

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Claude Code Plugin](https://img.shields.io/badge/Claude%20Code-plugin-8A2BE2.svg)](https://code.claude.com/docs/en/plugins)
![Status: beta](https://img.shields.io/badge/status-beta-orange.svg)

> **AI code review + a blocking pre-commit gate, native to Claude Code.**
> Runs on your Claude subscription the compliant way — no token borrowing, no external binary.

**claude-code-review-gate** is a Claude Code plugin that adds AI code review and a **blocking pre-commit gate** to your workflow. Claude Code does the inference itself, so review runs **on your Claude subscription** with no token borrowing and no third-party binary. The review methodology is adapted from [open-code-review](https://github.com/alibaba/open-code-review). Install the plugin and review with `/review-gate:review`.

<!-- TODO: add a demo GIF here — a `git commit` blocked on a high-severity finding, then passing after `--no-verify`. -->

## What it is

- **On-demand review** of your working diff or staged changes, and a **full-file scan** of a repo — run as a skill: `/review-gate:review`.
- A **blocking commit gate**: reviews staged changes and **blocks confident high-severity findings**. Default gates commits made through Claude Code; an optional installer extends it to **every** commit (terminal, IDE, or Claude Code).
- A **per-file reviewer subagent** with its own isolated context, fanned out in parallel, that reads the real files and returns a structured **severity + confidence** finding schema.
- A **deterministic verdict** (`block` / `warn` / `pass`) decided by auditable code, not the model's discretion. **Fails open** — a review hiccup never traps your commit.

## Why this exists (and how it relates to open-code-review)

[open-code-review](https://github.com/alibaba/open-code-review) (ocr) is an excellent AI code-review CLI. But running its binary on a **Claude Pro/Max subscription** means feeding your subscription/OAuth token to a third-party tool — which **violates Anthropic's Terms of Service and is actively blocked** (those tokens are for Claude Code and claude.ai only).

This plugin gives you the **same review methodology the compliant way**: Claude Code reviews your code *itself*, inside the official client, on your subscription. **No token leaves Claude Code, and the ocr binary never runs.** The reviewer prompts, scope rules, and rubric are **adapted from open-code-review** (Apache-2.0) and re-expressed as native Claude Code skills and subagents — see [Credits](#license--credits).

## How it works

```
/review-gate:review  (orchestrator skill, runs on the main agent)
        │  select changed/staged files → apply allowlist + rule hierarchy
        ▼
   fan out, in parallel, one isolated subagent per file
        │
   ┌────┴─────────────── code-reviewer (per file) ───────────────┐
   │  Read the real file → triage (if large) → review only new   │
   │  lines → "falsify, don't verify" pass → emit Finding[] JSON  │
   └─────────────────────────────────────────────────────────────┘
        │  collect findings → global dedup → (scan) project summary
        ▼
   verdict: block | warn | pass   →   render text, or JSON for the gate
```

The commit gate runs `claude -p "/review-gate:review --staged --json"` headlessly and maps the verdict to a decision: a Claude Code **PreToolUse** hook returns allow/deny (default wiring), or a **git pre-commit** hook returns an exit code (the optional "everywhere" wiring).

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

**3. (Optional) gate EVERY commit, everywhere.** By default the gate only fires for commits made through Claude Code. To gate terminal/IDE commits in every repo too:
```
bash bin/install-git-hook.sh     # sets a global core.hooksPath
bash bin/uninstall-git-hook.sh   # reverts it
```
> ⚠️ This sets a **global** `core.hooksPath`, which applies to all your repos and overrides each repo's `.git/hooks/pre-commit` (this hook still runs a repo-local pre-commit if one exists, so existing hooks keep working). Requires the `claude` CLI on your `PATH`.

**Requirements:** Claude Code with an authenticated Claude subscription; Python 3 and Git (Git Bash on Windows). No API key.

## Usage

```bash
# Review your current working changes
/review-gate:review

# Review only staged changes (what the commit gate uses)
/review-gate:review --staged

# Full-repo scan with a project summary
/review-gate:review --scan --summary

# Use a specific rule file, output machine-readable JSON
/review-gate:review --staged --rule ./.ocr/rule.json --json
```

**The commit gate in action:** commit a change containing a confident high-severity bug and the commit is blocked with the findings listed. Fix it, or bypass once:
```bash
git commit --no-verify
```

## Configuration

| Setting | Default | How to change | Effect |
|---|---|---|---|
| Gate scope | Claude Code commits | run `bin/install-git-hook.sh` (→ everywhere) / `uninstall-git-hook.sh` | which commits are reviewed |
| Mode | **block** | `OCR_ADVISORY=1`, or `.ocr/config.json` `{"blocking": false}` | block vs warn-only |
| Block threshold | `high` & `confidence ≥ 0.7` | `OCR_BLOCK_SEVERITY`, `OCR_BLOCK_CONFIDENCE` | what is severe/sure enough to block |
| Reviewer timeout | `240`s | `OCR_TIMEOUT` | fail-open deadline for `claude -p` |
| Per-file rules | built-in rubric | `.ocr/rule.json` (project), `~/.ocr/rule.json` (global), `--rule <path>` | the review checklist per file |
| `claude` flags | `--allowedTools "Bash Read Grep Glob Task"` | `OCR_CLAUDE_ARGS` | headless invocation tuning |
| Bypass once | — | `git commit --no-verify` | skip the gate for one commit |

Rule precedence (highest first): `--rule` → project `.ocr/rule.json` → global `~/.ocr/rule.json` → built-in `skills/review/rubric.md`. See `examples/.ocr/rule.json`.

## How it compares to open-code-review

| | open-code-review (ocr) | claude-code-review-gate |
|---|---|---|
| Runs as | external Go binary | native Claude Code skill + subagents |
| Auth on a subscription | borrows the token (ToS-blocked) | Claude Code's own auth (compliant) |
| Per-file isolation | per-file API session | per-file subagent (isolated context) |
| Severity / confidence | not in the schema | first-class, drives the gate |
| Commit blocking | left to the caller | deterministic verdict + gate |
| Line anchoring | fuzzy diff matching | true line numbers from real file reads |

## FAQ

**Why not just use open-code-review directly?** On a Claude subscription you'd have to feed your token to ocr, which Anthropic blocks as a ToS violation. This plugin delivers the same methodology compliantly, inside Claude Code.

**Does this use my Claude subscription? Is that allowed?** Yes and yes — the reviewing is done by Claude Code itself (the official client) via headless `claude -p`, which is the supported way to script it. Nothing is sent to ocr or any third party.

**Will it block my commits? How do I bypass?** By default it blocks confident high-severity findings. Use `git commit --no-verify` for a one-off, `OCR_ADVISORY=1` (or `.ocr/config.json` `{"blocking": false}`) for warn-only, or uninstall the global hook.

**Is it affiliated with Alibaba or Anthropic?** No.

## Safety & limitations

- **Fails open** by design — if the reviewer can't run (no `claude`, timeout, bad output) the commit proceeds with a warning.
- AI review is **advisory assistance, not a guarantee** — it complements, not replaces, tests and human review.
- **Full-file scans can be token-heavy** on large repos; a file ceiling keeps per-commit cost bounded.
- The **global git hook affects all commit paths** — read the install warning.
- `severity`/`confidence` are model-estimated, not calibrated; tune the thresholds if blocking is too eager or too lax.

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Note: changes that touch the open-code-review-derived prompt text or rubric must preserve the attribution headers and the [NOTICE](NOTICE) file.

## License & Credits

Licensed under the [Apache License 2.0](LICENSE).

The review methodology, scope rules, the "falsify, don't verify" filter, and the review rubric are **adapted from [open-code-review](https://github.com/alibaba/open-code-review)** (Apache-2.0). This project re-expresses that methodology natively inside Claude Code and adds a severity/confidence schema and a deterministic verdict gate. See [NOTICE](NOTICE) for full attribution. Not affiliated with Alibaba or Anthropic.
