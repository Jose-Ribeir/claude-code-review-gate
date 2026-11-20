---
name: settings
description: Show, set and validate the review gate's per-agent model and thinking-level settings (and its behavior knobs), with the source of every effective value (environment, project file, user file, or built-in default). Use to see which model each review agent runs with, to pin a different one, or to check a settings file after editing it.
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/rg_settings_cli.py:*)
---

# review-gate — settings

One schema-validated source for which model and thinking level each review
agent (spotter, code-reviewer, deep-reviewer, docs-checker, code-filter,
code-resolver, docs-summary, orchestrator, canary) uses under each backend
(`claude`, `kimchi`), plus a small `behavior` section (inline wait, spotter
skip thresholds, canary cache TTL, docs-checker parallelism).

Values resolve, highest first: the existing `OCR_*` environment overrides, the
**project file** `<repo>/.ocr/settings.json`, the **user file**
`~/.config/review-gate/settings.json` (`OCR_SETTINGS_FILE` relocates it), then
the built-in defaults. With no file and no override the gate behaves exactly as
it did before this command existed.

## Usage

```
/review-gate:settings                                  # show everything
/review-gate:settings set <key> <value> [--project|--user]
/review-gate:settings validate
```

Keys look like `agents.<agent>.<backend>.model`,
`agents.<agent>.<backend>.thinking` (`off|low|medium|high|max`) or
`behavior.<group>.<knob>` (for example `behavior.spotter_skip.max_files`).
`set` writes the user file unless `--project` is given.

## What to do

Run, from the reviewed repository, the form the user asked for. With no
arguments run `show`:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/rg_settings_cli.py
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/rg_settings_cli.py set <key> <value> [--project|--user]
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/rg_settings_cli.py validate
```

Report the output plainly:

- `show` prints one line per entry: `key = value [source]`. Sources are
  `env:<VAR>`, `project:<path>`, `user:<path>` or `default`. Point out any entry
  whose source is not `default`, and any `env:` source that is silently
  overriding a file value.
- `set` validates first and writes nothing on failure. A rejected value prints
  `error: ...` and exits non-zero: relay the message and the allowed values; do
  not edit the settings file by hand to get around it. An anthropic or openai
  model on the `kimchi` backend is rejected (ADR 0005: those providers are not
  permitted there). A deprecated id is accepted with a warning that names its
  retirement date and its replacement.
- `validate` checks both files. An invalid file is an error naming every
  problem; the gate never falls back to defaults for an invalid file, so tell
  the user to fix it before the next push.

The doctor (`scripts/ocr_doctor.py`) runs the same file validation, the provider guard, the
deprecated-model warnings and a check of pinned ids against the backend's model
list when one can be obtained.
