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
| Reviewer timeout | `1800`s | `OCR_TIMEOUT` | fail-**closed** deadline for `claude -p` (blocks the push; keep `hooks/hooks.json`'s `timeout` above it) |
| Per-file rules | built-in rubric + per-language rules | `.ocr/rule.json` (project), `~/.ocr/rule.json` (global), `--rule <path>` | the review checklist per file |
| Review model | `sonnet` | `OCR_MODEL` (`haiku` / `sonnet` / `opus`) | **cost lever.** The review runs in its own headless session; without a pin it would inherit the parent session's model and pay its cache-read rate on a workload that re-reads context every tool call |
| Extra `claude` flags | — | `OCR_CLAUDE_EXTRA_ARGS` | appended to the defaults |
| All `claude` flags | see `DEFAULT_CLAUDE_ARGS` | `OCR_CLAUDE_ARGS` | replaces the defaults **wholesale** — discards the cost controls too |
| Bypass once | — | `git push --no-verify` | skip the gate for one push |

Rule precedence (highest first): `--rule` → project `.ocr/rule.json` → global `~/.ocr/rule.json` → built-in `skills/review/rubric.md`, then the matching `skills/review/rules/<lang>.md` and `rules/llm-authored-code.md` appended. See `examples/.ocr/rule.json`.

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
| **No user settings** | `--setting-sources project` | Global hooks live in `~/.claude/settings.json` and would fire on **every tool call** of the review. Auth is unaffected — OAuth/keychain is not a settings source. |
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
- **It still fails open in these cases**, and it is worth knowing which:
  - **`claude` is not installed** — deliberate; there is no gate without the tool.
  - **The hook fails to launch, times out, or dies abnormally.** Claude Code treats a hook it could not start or had to kill as *non-blocking*, and no code inside the hook can change that. On Windows this is why both a Git Bash and a PowerShell adapter are registered — if neither can start, the gate is silently absent. Run `/review-gate:doctor` to check.
  - **`OCR_FAIL_OPEN=1` or `OCR_ADVISORY=1`** — the intended escape hatches.
- **Two structural limits**: `git push --no-verify` skips the git-hook wiring entirely, and pushing from a terminal skips the plugin wiring unless you installed the global hook.
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
