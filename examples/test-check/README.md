# test-check — reference race & regression test executor

A **reference command check** (ADR 0006 §7 + SPEC-15): per push, a cheap
selector model reads the change under review and either picks **existing
tests** to run (with the toolchain's race detector where available) or
authors **ephemeral probes** — throwaway race/regression tests that live and
die inside the gate's throwaway tip clone. Nothing is committed, presented,
or left behind.

Reproduced failures (each failure is re-run exactly once) become **blocking
findings** quoting verbatim test output; coverage gaps, flakes, and
unbuildable probes become **warnings**, never blocks. Findings and warnings
ride the normal stream through the falsify pass into `compute_verdict`.

**Deterministic coverage rule:** the selector picks existing tests OR
authors probes — never both in one check run. When the selection names any
existing test, the executor runs those and *discards* the reply's probes
(never authored, never run, no probe scratch); probes execute only when the
selection names no existing test. The rule is what makes waiver acceptance
stable: a re-push of a known defect must reproduce the *same* existing-test
finding a waiver was granted for — it must never mint a semantically new
ephemeral probe (new output, new anchor) on every push, which no waiver
could ever match. Genuinely new problems still block: a different finding on
the next push gets a different content anchor and is judged fresh.

## Install into a gated repository

1. Copy the executor into your repo and commit it:

   ```console
   $ cp examples/test-check/test_check /path/to/repo/scripts/test_check
   $ chmod +x /path/to/repo/scripts/test_check
   ```

2. Declare the command check in `/path/to/repo/.ocr/config.json`:

   ```json
   {
     "checks": {
       "race-regression": {
         "command": "scripts/test_check",
         "globs": ["**/*.go", "**/*.py", "**/*.js"],
         "parallel": true,
         "timeout": 600,
         "mode": "block"
       }
     }
   }
   ```

   - `parallel: true` runs the executor **concurrently with the review**
     against its **own tip clone** (SPEC-15.3) — this is the supported
     deployment; sequential mode also works but adds its runtime to every
     push.
   - `timeout` must cover selector call + selected tests + one re-run each
     (600 s is a reasonable start; adjust to your suite). On timeout the
     gate degrades to a warning and the push continues.
   - `globs` should match the languages your team wants probed. A push with
     no matching paths costs nothing.

3. Commit the config. The next push runs the check.

## Environment knobs

| Variable | Default | Purpose |
|---|---|---|
| `OCR_TEST_SEVERITY` | `high` | Severity of reproduced-failure findings |
| `OCR_TEST_CONFIDENCE` | `0.9` | Confidence carried by those findings |
| `OCR_TEST_SELECTOR_TIMEOUT` | `240` | Seconds the selector call may take |
| `OCR_TEST_SELECTOR_CMD` | `kimchi -p … --model deepseek-v4.1-flash` | Override for the selector invocation (used by the test suite's stub; the command receives the manifest JSON on stdin) |

`OCR_CHECK_TIMEOUT_S` is not a knob but an INPUT the gate exports to the
executor (the check's resolved `timeout`, string seconds, on top of the
inherited environment): when present, the executor clamps its own selector
and per-subject run budgets so every phase finishes BEFORE the gate's kill
(a small grace margin — 5 s or 5% of the budget, whichever is smaller — is
left for the answer write). Standalone runs without the variable keep the
knobs and defaults above.

## Contract (winds both ways)

- **Convention for coverage warnings:** when a finding's evidence is an
  ephemeral probe, the executor must also emit the canonical
  no-permanent-coverage warning (matching the gate's negation-near-coverage
  pattern). If it does not, the gate adds its own pairing note so the
  blind spot is still loud. Example wording the executor uses:

  > no permanent test coverage for `<paths>`; ephemeral probe was the only
  > evidence — write a permanent test for this behavior.

- **Selector output is strict JSON:**
  `{"existing_tests": [paths], "probes": [{"name", "language", "source",
  "run_cmd"}]}`. A malformed selector reply costs exactly one attempt and
  one warning; no retry, no blocking. The executor enforces the
  deterministic coverage rule above on the reply: any probe returned
  alongside a non-empty `existing_tests` is dropped unauthored and unrun
  (telemetry reports `probes_authored: 0`). Each probe may also carry an
  optional `path` — the repo-relative file whose behavior the probe targets —
  which anchors the finding when it fails; when absent, the finding anchors
  on the first matched file (the default).
- **Probes** are materialized under `.test-check-probes/` inside the
  executor's clone and deleted after the run. They never touch your working
  tree and never appear in reviewer evidence.
- **Telemetry (SPEC-15.4):** the stdout wrapper may carry a `telemetry`
  object of actual run counts — `selector_attempts`, `tests_selected`,
  `probes_authored`, `runs`, and `reproduce_results`
  (`fail`/`pass`/`flake`/`build`/`error`) — which the gate merges into that
  check's `command_checks` telemetry entry (malformed values degrade to a
  warning and are ignored). This executor always emits it.

## Costs

One cheap selector model call per matching push, plus the runtime of the
selected tests/probes (and exactly one re-run per failure). A push whose
selector comes back empty pays the model call only.

Integration tests: `tests/test_executor_testcheck.py` (selector stubbed via
`OCR_TEST_SELECTOR_CMD`; a real Go data-race probe runs when the Go
toolchain is available, with a simulated fallback otherwise).
