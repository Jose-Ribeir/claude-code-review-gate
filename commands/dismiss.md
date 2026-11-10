---
name: dismiss
description: Dismiss a finding from the gate's last recorded verdict so future re-raises of the same defect class are demoted to low severity with a "previously dismissed" note. Takes a finding id; use the ids printed by the refusal hint or the plugin's --history output.
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/review-gate.py --dismiss:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/review-gate.py --history:*)
---

# review-gate — dismiss

Record a finding as **dismissed** in the repository's local memory store
`.ocr/memory.jsonl` (untracked, append-only, per developer). This command is
the **only** write path to that store: the gate reads it on every verdict, but
never writes it, and push bypasses never teach it.

A dismissed finding is identified by its **category**, a **path glob**, and
the review-gate **code signature** (the same signature-line normalization the
gate uses for sibling search). The next time the reviewer raises the same
defect class, the gate demotes it to `low` severity, prefixes its content with
"previously dismissed", and logs a warning for every demotion — so nothing is
silently hidden.

## Usage

```
/review-gate:dismiss <finding-id> [--glob <pattern>]
```

- `<finding-id>` — a finding from the gate's **last recorded verdict**, by its
  full id or an unambiguous prefix. The refusal message lists the dismissable
  ids with their severity, path, and summary when the id does not resolve;
  `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/review-gate.py --history` replays the
  recorded verdicts themselves.
- `--glob <pattern>` — optional path glob to record instead of the finding's
  exact path, when the dismissal applies to a whole area (e.g. `vendor/**`).

## What to do

Run, from the reviewed repository:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/review-gate.py --dismiss <finding-id> [--glob <pattern>]
```

Report the outcome plainly:

- On success the command prints the dismissed finding and where it was
  recorded. Tell the user that future matches will be demoted to low severity
  with a "previously dismissed" note, never silently dropped — and that the
  store is local to this checkout, so other developers keep seeing the
  finding.
- On refusal (unknown or ambiguous id, no recorded verdict, not a git
  repository) relay the printed list of dismissable ids and ask the user to
  pick one. Do **not** guess an id, and do **not** edit
  `.ocr/memory.jsonl` by hand — a store line that did not come through this
  command is not auditable.

Dismissals are per-developer and unopinionated about other checkouts: if the
whole team should never see a defect class, that is a rubric/allowlist
decision, not a dismissal.
