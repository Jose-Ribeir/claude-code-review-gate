# Privacy

**review-gate** (the `claude-code-review-gate` plugin) has no server, no account, no analytics
and no telemetry endpoint. The author does not receive any data from your use of it. This page
describes exactly what leaves your machine and what the plugin stores on it. The statements
below were checked against the plugin's source; if you find a discrepancy, please open an
issue (see [SUPPORT.md](SUPPORT.md)).

## What leaves your machine

The plugin makes **no network requests of its own**. Its scripts (Python standard library,
bash, PowerShell) contain no HTTP/socket code, and nothing is sent to the author, to
open-code-review, or to any third party.

The one flow that does leave your machine is the review itself:

- To review code, the plugin starts a headless `claude -p` session (the Claude Code CLI you
  already have installed and are logged in to). That session reads the changes being reviewed
  and sends them to **Anthropic**, through your own Claude Code login, exactly as any other
  Claude Code session does.
- What the reviewer sees: the diff of each reviewable file in the commits you are about to
  push, and, for context, other files in a checkout of those commits that it reads with
  its `Read`, `Grep`, `Glob` and read-only `git` tools. Reviewable files are source files
  selected by an allowlist (see `skills/review/allowlist.md`), so lockfiles, vendored code and
  similar are skipped by default. Anything the reviewer reads becomes part of the prompt context
  Anthropic processes.
- Anything in the reviewed files, including secrets committed to them, is therefore sent to
  Anthropic as part of the review. Do not enable the gate for repositories whose contents you
  are not allowed to send to Anthropic.
- How Anthropic handles that data is governed by your agreement with Anthropic (your Claude
  plan, or your organisation's terms), not by this plugin. See
  <https://www.anthropic.com/legal/privacy>.

The reviewer is started with no MCP servers and no `WebFetch` / `WebSearch` tools, and it loads
no user or project settings.

## What is stored locally

Everything is written on your machine, by the plugin, and never uploaded by it.

Inside the reviewed repository's git directory (`.git/`, never committed or pushed):

- `review-gate-findings.jsonl`, `review-gate-last-output.json`, `review-gate-history/`:
  findings (file paths, line numbers, finding text), verdicts, branch and commit ids, and the
  reviewer's raw output. The findings log is never pruned; the history keeps the newest 50 runs
  (`OCR_HISTORY_LIMIT`).
- `review-gate-ledger/`: per-file review records that let a re-push skip files that were already
  reviewed (30 days, `OCR_LEDGER_TTL`; disable with `OCR_LEDGER=0`).
- `review-gate-async/`: state, logs and the diffs prepared for the reviewer of a running review
  (removed after the run or after one hour).
- `review-gate-telemetry/<date>.jsonl`: a local run log (on by default, turn it off with
  `OCR_TELEMETRY=0`). It records what the review found, how files were classified and how long
  each model call took, including file paths, line numbers and finding text. It is a file in
  your `.git` directory only: it is not sent anywhere. It is not rotated out by time; delete the
  directory whenever you like.
- small marker files (`scr-*`) that avoid repeating a report.

In the plugin data directory (`${CLAUDE_PLUGIN_DATA}`, or `~/.claude/plugins/data/review-gate-local/`):

- `gate-dir`: the path of the plugin's scripts, so the optional git hook can find them.
- `pending-*`: notes that a review result is waiting to be reported to the session.
- `worktrees/`: temporary detached git worktrees of the commits under review (removed when the
  review ends).
- `review-gate-debug.log`: counts, sizes and timings only (never paths or code); with
  `OCR_DEBUG=1` it also records extra diagnostics (environment variable names, not secrets).

The headless reviewer session itself is a normal Claude Code session and may leave its own
transcript in Claude Code's own data directory, under Claude Code's own retention settings.

## Removing your data

Delete the files and directories listed above (they are all plain files): remove
`.git/review-gate-*` and `.git/scr-*` in a repository (run `git worktree prune` afterwards) and
the plugin data directory. Uninstalling the plugin does not delete these files automatically. If
you installed the optional global git hook, also run `scripts/uninstall-git-hook.sh` (see the
README).

## Changes

Changes to this policy are made in this file and noted in [CHANGELOG.md](CHANGELOG.md).
