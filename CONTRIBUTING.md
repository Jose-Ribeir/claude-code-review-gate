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
python bin/sync-local-install.py            # copy working tree -> snapshot
python bin/sync-local-install.py --check    # report drift, exit 1 if stale
python bin/sync-local-install.py --prune    # also drop superseded snapshots
```

Restart Claude Code afterwards. The global git hook from `bin/install-git-hook.sh`
needs none of this — it bakes in an absolute path to `bin/` and always runs live
code, so the two can silently disagree about which version is in force.

## Test the commit gate

- **Verdict parser:** pipe a sample review JSON into `python bin/ocr_verdict.py` and
  confirm it prints `block` / `warn` / `pass`.
- **Gate core:** run `python bin/review-gate.py --mode git` inside a repo with staged
  changes; it should print findings and exit non-zero only on a confident
  high-severity block. Use `OCR_ADVISORY=1` to confirm warn-only behavior.
- **Everywhere hook:** `bin/install-git-hook.sh`, commit from a terminal, then
  `bin/uninstall-git-hook.sh` to restore your previous `core.hooksPath`.
- **Plugin hook:** after `bin/sync-local-install.py`, restart Claude Code and push
  through it. If behavior does not match your edits, run `--check` — a stale
  snapshot is the usual cause.

Do not call `claude -p` in CI — it needs interactive auth.

## Attribution rules (important)

The reviewer prompts and rubric are **adapted from
[open-code-review](https://github.com/alibaba/open-code-review)** under Apache-2.0.
If you change any file that carries an "adapted from open-code-review" header
(`skills/review/SKILL.md`, `skills/review/rubric.md`, `agents/code-reviewer.md`,
`bin/ocr_verdict.py`), **keep the header** and keep the [NOTICE](NOTICE) file
accurate (Apache-2.0 §4 requires retaining notices and stating changes).

## Style

- Python: keep it `ruff`-clean (the CI lints it).
- Shell: POSIX-ish bash with `set -euo pipefail`; avoid GNU-only flags so the
  scripts run under macOS, Linux, and Git Bash on Windows.
- Reference plugin files via `${CLAUDE_PLUGIN_ROOT}` — never absolute paths.

## Pull requests

Keep PRs focused, describe the behavior change, and update `CHANGELOG.md` under
`[Unreleased]`.
