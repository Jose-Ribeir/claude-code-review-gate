# Impact review — Part #8 (SPEC-14 custom checks + documentation quality gates, `integrate/spec-14-push-findings`)

Scope judged: #8 lands LAST, on top of `refs/merge-validator/target` (9e865761 = main + #9 + #6).
Diff range: `8385a97..refs/merge-validator/part-8` (7 files, 3 commits: de775b3, 10befff, 1cd1149). No CI runs on #8's branch.
Verification: `git merge-tree --write-tree refs/merge-validator/target refs/merge-validator/part-8` → conflicts ONLY in `scripts/review-gate.py` (one region) and `scripts/rg_config.py` (docstring). All other files auto-merge.

## F1 (CENTERPIECE, R — DEP-03/DEP-07): `_run_one_command_check` return-arity conflict

Function: `scripts/review-gate.py`, def at target:9810 / part-8:9391 / base 8385a97:9373.

- Base (8385a97) and #8: **2-tuple** `(findings, error_note_or_None)` — part-8:9391-9440 (e.g. part-8:9418 `return [], f"command not executable or missing: {command}"`).
- Main after #9 (the SPEC-15 rewrite, `refs/merge-validator/part-9` == target here; #6 does NOT touch this function): **4-tuple** `(findings, check_warnings, error_note_or_None, telemetry)` + new `timeout=` kwarg + `OCR_CHECK_TIMEOUT_S` env — target:9810-9880 (e.g. target:9855 `return [], [], f"command not executable or missing: {command}", {}`).
- #8 does not change the arity itself — it adds new 2-tuple error returns (symlink-escape guard) and rewrites the "command not executable" line, which #9 also rewrote to 4-tuple. That is the exact conflict hunk (merge-tree output lines ~9865-9897 of the conflicted file).

Every caller (exhaustive, via `git grep` on each ref):

| Caller | Ref | Expects | Breaks if 2-tuple wins? |
|---|---|---|---|
| `scripts/review-gate.py:10024` `got, check_warnings, note, tele_extra = _run_one_command_check(review_root, check["command"], matched, timeout=check.get("timeout"))` inside `_command_check_findings` | target / part-9 | 4 | **Yes** — `ValueError: not enough values to unpack` on every command-check run that hits an error path (escape / not-executable / timeout / bad exit / bad JSON). NOT caught by the surrounding `except ReviewLimitError/BudgetError/...` handlers (target:5463-5490 are specific classes) → push run fails as machinery error. |
| `scripts/review-gate.py:9489` `got, note = _run_one_command_check(...)` | part-8 / 8385a97 | 2 | Breaks if main's 4-tuple function body survives with this caller (too many values). |
| `tests/test_docs_gate.py:1496` (`got[0], got[1], got[2]` from the raw return) | target | ≥3 elems | **Yes** — `IndexError: tuple index out of range` on `got[2]`. |
| `tests/test_docs_gate.py:1923` (`return review_gate._run_one_command_check(...)` helper, results consumed as 4-tuple by SPEC-15 tests) | target | 4 | Yes. |
| `tests/test_executor_testcheck.py:435, 453` `findings, warnings, note, tele = review_gate._run_one_command_check(...)` | target (new file from #9) | 4 | **Yes** — ValueError. |

The breakage is **conditional**: command checks are opt-in (need `checks.<name>.command` + `enabled` in `.review-gate/checks.json`), so default-config pushes don't hit the function at all — but any repo using a custom command check gets a hard failure on every degrade path. Silent-loss variant: resolving the conflict by taking main's side wholesale **drops #8's symlink-escape security fix** (part-8:9398-9410 realpath/`os.path.commonpath` guard) with no test failure to signal it.

**Correct resolution required**: keep main's 4-tuple contract (returns `[], [], note, {}`) and port #8's realpath guard into it, including `exe = real_exe` (part-8:9412) and keeping main's env/budget lines. This is a manual port, not a side-pick.

## F2 (B — DEP-03, low): rg_config.py docstring-only conflict
`scripts/rg_config.py:31-41`: #8 rewords the `checks.command` docstring; main's version additionally documents `parallel` and `timeout` (SPEC-15.1) added by #9. Take main's text (or merge both wordings); docstring only, zero code impact. Resolution: keep main's, optionally fold in #8's "runs over matched changed files" phrasing.

## F3 (OK — DEP-03/DEP-08): contract/config/agent surface #8 adds is additive
- `scopes` key added to the docs_quality payload (review-gate.py `_docs_quality_block`, target:9547 / part-8:9135): consumed only by the reviewer LLM via `agents/docs-checker.md:49-51` (#8 adds the `scopes` doc paragraph; #9's overlapping edit at docs-checker.md:15-34 adds the final-review lens — different regions, auto-merges cleanly, no semantic clash).
- Chunk-0-only `docs_quality` attach (`_run_chunked`, target ~12250-12290): #8 stops re-shipping the full docs payload into every chunk manifest (N-fold duplication fix). No target-side code consumer reads per-chunk `docs_quality` (only `manifest["docs_quality"] = docs_quality` at target:10385-10386 and the agent doc); `_build_review_manifest` signature identical in both refs (target:10309 / part-8:9523, `docs_quality=None`). Behavior change is intended dedupe, not a break.
- `_warn_mode_clamp` clamp fix (part-8:9068-9080): function identical between 8385a97 and target → #8's hunk merges cleanly. Covered by #8's new test `tests/test_docs_gate.py::test_warn_mode_holds_when_block_confidence_is_zero`.
- `ocr_docsquality.py`: all changes are to private helpers (`_collect_path_mentions` gains defaulted `doc_rel=""`, part-8:228; `_scan_ste` paragraph rework; `_phrase_regex` fix). Public API unchanged: `compute_evidence(repo_root, files, *, sentence_cap=...)` (part-8:473 == target:429), `validate_findings(text)` (part-8:554 == target:510). Target's callers (review-gate.py:9602, 9667, 9741) unaffected. #9/#6 did not touch `ocr_docsquality.py` or `tests/test_docsquality.py`.

## F4 (OK — DEP-05): no config keys or files provided only by #9/#6
#8's rg_config.py diff is docstring-only; the `checks` schema keys it relies on (`command`, `globs`, `mode`, `enabled`, `sweep`, `sentence_cap`) all pre-exist in base 8385a97. No new env vars, migrations, agents, or infra. (#9's `parallel`/`timeout` keys are main's addition and #8 doesn't reference them.)

## F5 (B→OK — DEP-08, no CI signal): obvious breakage check on #8's 3 commits
- `python3 -m py_compile` on part-8's `scripts/review-gate.py`, `scripts/ocr_docsquality.py`, `scripts/rg_config.py`: **passes** (3.12; 3.9 not locally installable, but no 3.10+-only syntax — match, walrus-in-new-contexts, PEP 604 — observed; stdlib-only, `os.path.commonpath` is 3.5+).
- No imports of modules/symbols defined only in #9/#6 (`_ParallelCommandChecks`, `OCR_CHECK_TIMEOUT_S` consumers, `test_executor_testcheck.py` are main-side only). All symbols #8 touches (`_SEVERITY_RANK`, `_DOC_QUALITY_OPTION_KEYS`, `_records_root`, `_ManifestScope`, `chunk_extras`) exist with identical signatures on target.
- Auto-merged `tests/test_docs_gate.py` is consistent with the post-merge (4-tuple) world, since #8's test additions target the clamp, not the command-check arity.

## DEP scorecard
- DEP-01/02 (migrations): N/A — no migrations.
- DEP-03: **At risk via F1** (provider return-contract divergence; must resolve to main's 4-tuple).
- DEP-04: OK — all #8 features are behind opt-in checks config; default pushes unchanged.
- DEP-05: OK (F4).
- DEP-06: N/A (no external deps).
- DEP-07: **F1** is the semantic conflict; F2 trivial.
- DEP-08: OK (F5).
- DEP-09: N/A (no backports).

## Verdict
**risky-alone (merge-resolution risk, not standalone-broken)**: #8 is internally self-consistent (2-tuple function + matching caller + tests, compiles clean) and lands on no missing dependency — it would be safe if the others never landed. The danger is entirely in landing LAST on top of #9's arity rewrite: the `review-gate.py` conflict MUST be hand-ported (4-tuple returns around #8's symlink guard + keep `exe = real_exe` + keep main's timeout/env). A naive "take ours/theirs" resolution either crashes every configured command check at runtime (review-gate.py:10024, tests at test_docs_gate.py:1496/1923, test_executor_testcheck.py:435/453) or silently drops #8's symlink-escape security fix. Require: post-merge CI run of tests/test_docs_gate.py + tests/test_executor_testcheck.py before marking the merge good.
