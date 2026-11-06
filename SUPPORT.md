# Support

- **Bugs, questions, feature requests:** open an issue at
  <https://github.com/Jose-Ribeir/claude-code-review-gate/issues>.
- **Something not working?** First run `/review-gate:doctor` in Claude Code. It checks, read-only,
  that the push gate is wired up and able to run (Python 3, `claude`, `git`, the hook adapters, the
  global git hook, version skew), and tells you what to fix. Paste its output into the issue.
- **A push was blocked and you disagree?** Attach the finding text (see
  `.git/review-gate-findings.jsonl`, or `python scripts/review-gate.py --history`). Please remove
  anything private from it first.
- **Bypassing in an emergency:** see "Configuration" and "Safety & limitations" in the
  [README](README.md) (`OCR_FAIL_OPEN=1`, `OCR_ADVISORY=1`, `scripts/uninstall-git-hook.sh`).
- **Contributing:** see [CONTRIBUTING.md](CONTRIBUTING.md).
- **Privacy:** see [PRIVACY.md](PRIVACY.md).

This is a volunteer-maintained project; there is no guaranteed response time.
