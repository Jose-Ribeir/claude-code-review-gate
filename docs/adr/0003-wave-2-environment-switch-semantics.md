# ADR 0003 — Wave-2 environment-switch and config-surface semantics

## Status

Accepted (2026-11-13). Covers ALL remaining wave-2 config-surface changes the
docs-checker flagged as lacking a recorded decision at tip 7465d89, extending
ADR 0002 (which covers only the huge-range fail-closed guard and `OCR_INDEX`):

- (a) `KIMCHI_BIN` resolves env-first, not PATH-first
  (`scripts/review-gate.py::_find_kimi`);
- (b) `.ocr/config.json` advisory semantics: strict-JSON parse instead of
  substring-accept (`_advisory_says_nonblocking` / `_is_advisory`);
- (c) `OCR_CALIB_UPSTREAM` becomes REQUIRED — no built-in fallback
  (`scripts/ocr_calibrate.py`);
- (d) the gate's timeout-escalation advice now names `OCR_DEEP_TIMEOUT` and
  `OCR_CHUNK_TIMEOUT` for the first time (`_timeout_knob`);
- (e) the `_refuse_huge_range` fail-closed behavior (decision substance in ADR
  0002a; restated here for the git-fossil contract);
- (f) `_prune_ledger` mtime protection now also folds in the `resolutions/`
  subdirectory — a storage-semantics change whose decision substance is one
  bullet: a fingerprint directory whose records are stale but which holds a
  recently written resolution entry is kept alive, never rmtree'd wholesale.

The film-change records for this wave belong to the wave-2 fix push that
landed 2026-11-13 under the issues #56/#59 auth intent; later pushes inherit
this ADR as the recorded decision and must not re-attribute the changes.

## Context

The docs-checker's `config_surface` family fired on four changes in wave 2
that altered the gate's decision surface without any recorded decision:

### a) `KIMCHI_BIN` env-first resolution
`_find_kimi` used to resolve the reviewer binary PATH-first
(`shutil.which("kimchi")`) and only then consult `KIMCHI_BIN`. Wave 2 flips
the order: `os.environ.get("KIMCHI_BIN")` wins whenever it names an existing
file. An explicit override that loses to a stale PATH entry is not an
override; `rg_jira_fetch.py::_kimchi_bin` already resolves the same variable
env-first, so the gate was the inconsistent caller.

### b) Advisory config parse semantics
The previous code accepted any `.ocr/config.json` / `.ocr/config` text
containing the substrings "blocking" and "false" (case-insensitive, anywhere);
a comment asserting blocking must stay ON still flipped the gate into
advisory mode. The new `_advisory_says_nonblocking` accepts only a JSON
object with `"blocking": false`, or — the graceful fallback for a file that
does not parse as JSON — a literal `"blocking": false` pair matched verbatim
by regex. YAML `blocking: false` or prose mentioning both words no longer
downgrades the gate. Parse failures (including `json.loads`'s RecursionError
on pathologically nested input) are silent: advisory lookups never break the
gate, they only decline to downgrade it.

### c) `OCR_CALIB_UPSTREAM` is now required
`scripts/ocr_calibrate.py` used to fall back to a committed
`UPSTREAM_FALLBACK` developer-machine path. Wave 2 makes the variable
REQUIRED for the `--fp-upstream` commands with no built-in default: a
developer-machine path baked into a shipped tool silently produced
machine-specific calibrations.

### d) Timeout-escalation advice names the lane-specific knobs
`_timeout_knob` selects the env var the escalation text advises
(`export {knob}={2x}`): the deep lane runs on `OCR_DEEP_TIMEOUT`, the chunked
lane on `OCR_CHUNK_TIMEOUT`, and the earlier advice hardcoded `OCR_TIMEOUT`,
which was wrong advice for both lanes.

## Decision

a) **`KIMCHI_BIN` resolves env-first, PATH as fallback.** An operator-set
override must never be shadowed by whatever `kimchi` happens to sit on PATH.
Recorded rationale: parity with the repository's own existing practice
(`rg_jira_fetch.py::_kimchi_bin`) and the general principle that an explicit
override outranks ambient environment.

b) **Advisory mode requires strict evidence.** Only a JSON object with
`"blocking": false` — or, for non-JSON config files, that exact quoted pair
matched verbatim — puts the gate into advisory mode. Substring-accept is
rejected: it evidentially flipped on prose asserting the opposite. Parse
failures never break the gate and never downgrade it either; they are
silently treated as "no advisory evidence".

c) **`OCR_CALIB_UPSTREAM` has no built-in default.** Calibration against a
hard-coded developer checkout is worse than a loud, early failure; the script
dies at argument validation naming the env var, not inside git.

d) **Timeout advice names the knob that governs the lane that fired.**
Advice that instructs an export of a variable the lane ignores is advice
that cannot work.

e) **`_refuse_huge_range` stays fail-closed** per ADR 0002a; this ADR does not
re-litigate it, it only carries the fossil record for the rest of the wave.

f) **Resolution entries extend the fp-dir's liveness.** `_prune_ledger`
computes a fingerprint dir's protection mtime as the max over the dir itself
and its `timeouts/`, `seg/`, `dep/`, and `resolutions/` subdirectories, so a
freshly written resolution entry keeps its ledger dir alive.

## Consequences

- Changing any of these surfaces now requires updating this ADR (or a
  successor), keeping the docs-checker's `config_surface` family satisfied.
- Operators relying on the old PATH-first `KIMCHI_BIN` precedence (a PATH
  `kimchi` shadowing their override) — was itself the bug — no longer have
  that accident; the override always wins.
- `.ocr/config.json` files that relied on loose substring evidence for
  advisory mode must now set `{"blocking": false}` literally; the escape
  itself is unchanged (still env-free, per-repo, non-persistent).
- `ocr_calibrate.py` fails fast with an argparse error naming
  `OCR_CALIB_UPSTREAM` when the variable is unset or points at a missing
  checkout; no silent machine-specific fallback remains.
- Timeout-hint text is lane-accurate: raising the global `OCR_TIMEOUT` alone
  no longer moves the deep/chunk lanes, and the advice says exactly which
  knob does.
- Advisory lookup still cannot crash or hang the gate on pathological JSON.
