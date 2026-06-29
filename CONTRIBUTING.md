# Contributing

Thanks for your interest in improving claude-code-review-gate.

## Develop locally

```bash
git clone https://github.com/Jose-Ribeir/claude-code-review-gate
claude --plugin-dir ./claude-code-review-gate
```
Then try `/review-gate:review --staged` in a repo with staged changes.

## Test the commit gate

- **Verdict parser:** pipe a sample review JSON into `python bin/ocr_verdict.py` and
  confirm it prints `block` / `warn` / `pass`.
- **Gate core:** run `python bin/review-gate.py --mode git` inside a repo with staged
  changes; it should print findings and exit non-zero only on a confident
  high-severity block. Use `OCR_ADVISORY=1` to confirm warn-only behavior.
- **Everywhere hook:** `bin/install-git-hook.sh`, commit from a terminal, then
  `bin/uninstall-git-hook.sh` to restore your previous `core.hooksPath`.

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
