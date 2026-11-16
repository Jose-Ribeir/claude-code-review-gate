# Live checks (not collected by pytest)

These scripts drive the real gate end to end. They are slower than the unit
suite, so they are run by hand before a backend is relied on, never by CI.

| Script | What it proves | Needs |
|---|---|---|
| `live_matrix.py <checkout> <out.json> [filters]` | 30 `git push` scenarios through the shared pre-push hook: clean and blocking verdicts, expired auth, garbage output, a crash, the kimchi deny-extension canary, claude's read-only argv, env scrubbing, backend selection precedence, exactly one review per push | Python only; claude and kimchi are simulated by `tests/fake_backend.py` |
| `live_hook.py <checkout> <out.json>` | 6 `--mode hook` scenarios (the call a harness PreToolUse hook makes), with both CLIs installed and `OCR_HARNESS` set the way the adapters set it | the same |
| `smoke_claude.py <path to review-gate.py>` | One REAL `claude -p` review of a scratch repo with a seeded bug | the real `claude` CLI, logged in; costs one review |

`<checkout>` is a checkout of this repository. Results are written as JSON
(one entry per scenario with its checks) and printed as `[PASS]`/`[FAIL]` lines.

## Pre-adoption check for real kimchi

The kimchi backend was developed against a simulated `kimchi` (its stream
format, the `-e` deny extension and the canary are modelled, not observed).
Before anyone relies on it, on a machine with the real Kimchi CLI installed
and logged in:

```bash
python tests/live/live_matrix.py "$PWD" /tmp/matrix.json     # use a real temp path on Windows
python tests/live/live_hook.py   "$PWD" /tmp/hook.json
```

Both must end `30/30` and `6/6`. Where a scenario fakes the CLI it will still
use the fake: the point of running on a real machine is the scenarios that find
the real `kimchi` on PATH (selection) plus a manual push of a repo with a seeded
bug, which must be blocked with the right finding. Record the result in the
PR or commit that adopts the backend.
