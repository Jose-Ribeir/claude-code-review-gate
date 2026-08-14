# Contributing

Thanks for your interest in improving claude-code-review-gate.

## Develop locally

```bash
git clone https://github.com/Jose-Ribeir/claude-code-review-gate
claude --plugin-dir ./claude-code-review-gate
```
Then try `/review-gate:review --staged` in a repo with staged changes.

### If you installed the plugin from a local directory marketplace

Claude Code does **not** run your working tree. It copies a snapshot into
`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`, and
`${CLAUDE_PLUGIN_ROOT}` in [hooks/hooks.json](hooks/hooks.json) resolves to that
snapshot. The install record is version-pinned, so committing changes nothing the
gate actually runs — it keeps enforcing the old code, bugs included, with no
warning. Refresh it after changing plugin code:

```bash
python scripts/sync-local-install.py            # copy working tree -> snapshot
python scripts/sync-local-install.py --check    # report drift, exit 1 if stale
python scripts/sync-local-install.py --prune    # also drop superseded snapshots
```

Restart Claude Code afterwards. The global git hook from `scripts/install-git-hook.sh`
used to sidestep all of this by baking in an absolute path and always running live
code, which meant the two wirings could silently disagree about which version was
in force. Since 0.3.0 it resolves the reviewer at runtime, preferring the pointer
`review-gate.py` writes under `${CLAUDE_PLUGIN_DATA}` — which names whichever
install last ran the plugin hook, i.e. the snapshot. So both wirings now agree,
and `--check` matters for both.

## Test the commit gate

- **Verdict parser:** pipe a sample review JSON into `python scripts/ocr_verdict.py` and
  confirm it prints `block` / `warn` / `pass`.
- **Gate core:** run `python scripts/review-gate.py --mode git` inside a repo with staged
  changes; it should print findings and exit non-zero only on a confident
  high-severity block. Use `OCR_ADVISORY=1` to confirm warn-only behavior.
- **Everywhere hook:** `scripts/install-git-hook.sh`, commit from a terminal, then
  `scripts/uninstall-git-hook.sh` to restore your previous `core.hooksPath`.
- **Plugin hook:** after `scripts/sync-local-install.py`, restart Claude Code and push
  through it. If behavior does not match your edits, run `--check` — a stale
  snapshot is the usual cause.

Do not call `claude -p` in CI — it needs interactive auth.

## Attribution rules (important)

The reviewer prompts and rubric are **adapted from
[open-code-review](https://github.com/alibaba/open-code-review)** under Apache-2.0.
If you change any file that carries an "adapted from open-code-review" header
(`skills/review/SKILL.md`, `skills/review/rubric.md`, `agents/code-reviewer.md`,
`scripts/ocr_verdict.py`), **keep the header** and keep the [NOTICE](NOTICE) file
accurate (Apache-2.0 §4 requires retaining notices and stating changes).

## Style

- Python: keep it `ruff`-clean (the CI lints it).
- Shell: POSIX-ish bash with `set -euo pipefail`; avoid GNU-only flags so the
  scripts run under macOS, Linux, and Git Bash on Windows.
- Reference plugin files via `${CLAUDE_PLUGIN_ROOT}` — never absolute paths.
- **Nothing goes in `bin/`.** A plugin's `bin/` is added to the Bash tool's
  `PATH`, so every file there becomes a bare command in every Bash call for
  every user with the plugin enabled. Executables live in `scripts/`. The one
  file still in `bin/` is a compatibility shim for pre-0.3.0 git-hook installs,
  and it is scheduled for removal in 0.5.0.

## Hook wiring

`hooks/hooks.json` registers **two** `PreToolUse` entries, both matching
`Bash` with `if: "Bash(*git push*)"`. Notes worth keeping straight:

- **The `*…*` glob is deliberate.** A rule like `Bash(git push:*)` or
  `Bash(git push *)` does fire for plain, compound (`cd x && git push`),
  env-prefixed and `;`-separated commands — but *not* for `command git push` or
  `bash -c "git push"`. The surrounding wildcards catch those too, and stay
  selective enough that unrelated Bash calls never spawn the hook.
- **Two entries, because hooks have no platform condition.** `if` is permission
  rule syntax, not a platform test. Claude Code runs a shell-form command under
  Git Bash on Windows, or PowerShell when Git Bash isn't installed — so the
  PowerShell entry uses the exec form (`command` + `args`, which skips the shell
  and sidesteps execution policy) and `gate-hook.ps1` decides for itself whether
  it is needed. It defers only when Git Bash is *positively* confirmed; when in
  doubt it runs, because a duplicate review is cheaper than an unreviewed push.
- **`timeout` must stay above `OCR_TIMEOUT`** on *both* entries. Claude Code
  kills the hook at its own deadline regardless, and a killed hook is
  non-blocking — i.e. a fail-open.
- `statusMessage` is a documented field; a top-level `description` key is not,
  which is why this prose lives here instead of in the JSON.

## Pull requests

Keep PRs focused, describe the behavior change, and update `CHANGELOG.md` under
`[Unreleased]`.
