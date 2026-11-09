# Review-gate calibration matrix (C5)

Real-model pushes through the full gate pipeline (hook -> supervisor ->
sandbox clone -> `kimchi -p "/skill:review --range A..B --json"` -> verdict
store), driven by `scripts/ocr_calibrate.py`. `stub` rows prove the plumbing
with the stub worker and no model. A `block` on a `pass` row is a false
positive; its findings are recorded verbatim.

| id | source | range | expected | verdict | blocked | decision | findings | wall_s | note |
|---|---|---|---|---|---|---|---|---|---|
| stub-py-app-a5ccc09-ddbb912 | py-app | a5ccc09..ddbb912 | pass | pass | no | pass | (none) | 1.5 | plumbing proof [stub worker] |
| py-app-48a4c0c-1199231 | py-app | 48a4c0c..1199231 | block | block | yes | deny | high/? app/main.py:28 main.py imports get_user, summarize, format_amount, and paginate, but never imports on_command from app.handlers. The ve; medium/? app/handlers.py:6 on_command accepts a `sender` parameter but never inspects it; dispatch_command is documented as 'the single entry point; low/? app/handlers.py:10 Both accepted commands just return {'ok': True} without performing any purge or reload. If this is an intentional stub,  | 36.6 | missing-sender: on_command |
| electron-ts-6220061-f8bc3db | electron-ts | 6220061..f8bc3db | block | block | yes | deny | high/? src/main/settings-window.ts:9 The settings window grants its renderer full Node.js access and disables context isolation. The existing main window (sr; low/? src/main/settings-window.ts:13 win.loadFile(path.join(__dirname, '../renderer/settings.html')) targets a file with no source counterpart — there are no; info/? src/main/settings-window.ts:4 No references to createSettingsWindow exist outside its definition. If this is intentional scaffolding for a follow-up c | 32.6 | nodeIntegration:true + contextIsolation:false |
| electron-ts-f8bc3db-45c5827 | electron-ts | f8bc3db..45c5827 | block | block | yes | deny | high/? src/renderer/diagnostics.ts:9 ipcRenderer.send("channel-x", report) has no matching ipcMain handler anywhere in the repo (src/main/ipc.ts only registe; info/? src/renderer/diagnostics.ts:9 Channel name "channel-x" is a magic string duplicated at the send site with no shared definition, which contributed to t | 30.6 | orphan channel-x |
| py-app-ddbb912-48a4c0c | py-app | ddbb912..48a4c0c | block | pass | no | pass | critical/? app/db.py:20 SQL injection: get_user() interpolates user_id into the query with an f-string (SELECT ... WHERE id = {user_id}). Any ca; major/? app/main.py:23 user_card() subscripts get_user()'s result without a None check: for any user_id that does not exist, get_user() returns; major/? app/db.py:13 connect() runs the CREATE TABLE schema via executescript() with no 'IF NOT EXISTS'. Calling connect() a second time on a; minor/? app/db.py:20 get_user() does no type coercion on user_id; ... | 0.5 | SQLi: get_user f-string SQL [cached verdict] |
| electron-ts-45c5827-948118d | electron-ts | 45c5827..948118d | block | warn | no | pass | high/high src/main/ipc.ts:12 ; low/medium src/main/ipc.ts:13 ; info/high src/main/ipc.ts:12  | 0.5 | diag:open no sender validation [cached verdict] |
| electron-ts-948118d-3cc8a7b | electron-ts | 948118d..3cc8a7b | block | pass | no | pass | blocker/? src/renderer/status.ts:1 Imports `formatBytes` from `../main/util`, but that module now only exports `formatSize` (renamed in 3cc8a7b). TS2305: M; blocker/? src/renderer/panel.ts:2 Same stale import of the removed `formatBytes` export (TS2305), with a call at line 6 in `refreshPanel()`. Build fails.; nit/? src/main/util.ts:3 Name `formatSize` is now slightly less precise — the function formats a byte count specifically and throws RangeError fo | 0.5 | formatBytes->formatSize rename break [cached verdict] |
| clean-ts-8fcc2a1-07cc15c | clean-ts | 8fcc2a1..07cc15c | pass | warn | no | pass | medium/? src/calc.ts:24 averageUnitPrice is the unweighted mean of per-line unitPrice values. A line with quantity 1 and price 100 counts the sa; low/? src/calc.ts:20 Lines with quantity 0 still contribute to averageUnitPrice, though they contribute nothing to itemCount or subtotal. Com; low/? src/calc.ts:16 No validation of negative quantity or unitPrice; negative values silently decrease itemCount/subtotal. If negative quant | 38.6 | clean: cart totals on stats helpers |
| clean-ts-07cc15c-0544617 | clean-ts | 07cc15c..0544617 | pass | pass | no | pass | info/? src/index.ts:25 discountPercent uses `subtotal > 100 ? 10 : 5`, so a subtotal of exactly 100 receives the lower tier. No spec available ; info/? src/index.ts:24 clamp() throws RangeError when min > max. Since floorPct/ceilPct are caller-supplied public parameters, passing floorPct; low/? src/index.ts:18 title is constructed as `Cart: ...`, so capitalize(title) never changes the string. Harmless, but redundant — either dro | 36.6 | clean: report builder wiring |
| py-app-a5ccc09-ddbb912 | py-app | a5ccc09..ddbb912 | pass | pass | no | pass | (none) | 1.6 | clean: summary reporting |
| py-app-ddbb912-ccc0e8b | py-app | ddbb912..ccc0e8b | pass | block | yes | deny | critical/? app/db.py:20 SQL injection: get_user() interpolates user_id into the query with an f-string (SELECT ... WHERE id = {user_id}). Any ca; major/? app/db.py:13 connect() runs the CREATE TABLE schema via executescript() with no 'IF NOT EXISTS'. Calling connect() a second time on a; minor/? app/db.py:20 get_user() does no type coercion on user_id; a string or float input produces malformed SQL or unexpected matches. Wheth; medium/? app/handlers.py:6 on_command accepts a `sender` parameter b... | 235.1 | clean-variant: safe user lookup + sender-checked commands |
| electron-ts-271c078-6220061 | electron-ts | 271c078..6220061 | pass | pass | no | pass | low/? src/renderer/status.ts:3 No reference to statusLine exists anywhere else in the repository (verified by repo-wide search). If this is intended to; low/? src/renderer/status.ts:4 total === 0 is guarded, but: (a) formatBytes in src/main/util.ts:4 throws RangeError on any negative input, so a negativ | 48.7 | clean: status line for report panel |
| upstream-41dad24-4fd8b02 | upstream | 41dad24..4fd8b02 | pass | pass | no | pass | (none) | 1145.1 | upstream: test: fix ruff lint errors (E731 lambdas, unused import) [verdict landed in the store after the harness's 19-min patience; corrected from the store] |
| upstream-4a40fa1-41dad24 | upstream | 4a40fa1..41dad24 | pass | pass | no | pass | (none) | 1144.8 | upstream: chore(release): 0.13.0 [verdict landed in the store after the harness's 19-min patience; corrected from the store] |
| upstream-e2bb9e6-4a40fa1 | upstream | e2bb9e6..4a40fa1 | pass | pass | no | pass | (none) | 1144.6 | upstream: [W5] ledger: scope delta priors by tip ancestry across refs [verdict landed in the store after the harness's 19-min patience; corrected from the store] |
| upstream-fbc880c-e2bb9e6 | upstream | fbc880c..e2bb9e6 | pass | warn | no | pass | (none) | 2340.9 | upstream: [W6] docs: stacked-branch review base and findings persistence [timed out waiting for the supervisor] |

## Experiment log: flash orchestration tier (2026-11-09, reverted)

Re-running the 6 FN reviews with the worker session on `kimchi-dev/glm-5.3-flash`
(latency-report rec #1) broke the pipeline: every review ran 9-12 turns, surfaced
the findings, then died unparseable (`could not parse review output`, verdict None
on all 6 rows; wall 191-279 s vs the 30-41 s baseline, from error+retry churn).
Pushes still denied (fail-closed held), but no verdicts were produced. The
session model default was reverted to `kimchi-dev/kimi-k3`; `OCR_MODEL` remains
the escape hatch. Rec #1 is invalidated by measurement — see
docs/review-latency-analysis.md.
| upstream-0356c55-fbc880c | upstream | 0356c55..fbc880c | pass | pass | no | pass | (none) | 0 | upstream: [W4] orphans to their own record, siblings after the resolver, dedup fixes [recorded from store after harness wedge] |
| upstream-ab54c53-0356c55 | upstream | ab54c53..0356c55 | pass | pass | no | pass | (none) | 0 | upstream: [W3] resolutions: quote-presence validity, read-time suppression [recorded from store after harness wedge] |

## Known issue: calibration harness wedge (2026-11-09)

Two consecutive `--fp-upstream` runs wedged mid-corpus: the harness process
blocks in `select()` with NO live child (sampled stack: `_PyEval_EvalFrameDefault
→ __select`), always AFTER a row's review completed (state done + verdict in the
store) but BEFORE the row is appended. Stranded verdicts are recoverable from
the store (this note documents the two manual reconciliations). The wedge is
in the harness's row-recording path, not in the gate — all reviews completed
and their verdicts landed. Fix candidates: the `_push_hook` join call's pipe
handling (a detached supervisor or killed run holding the hook's stdout pipe
would block `communicate()` despite the child having exited) — needs a bounded
read or `Popen`+explicit kill. Until fixed, run `--fp-upstream` in ~1-hour
slices and reconcile any stranded rows from the store.
| upstream-aab46bd-ab54c53 | upstream | aab46bd..ab54c53 | pass | pass | no | pass | (none) | 2350.0 | upstream: [W2] resolver: distinct failed status, half-batch retry [timed out waiting for the supervisor]  [store verdict recovered after infra-fail] |
| upstream-90cae6b-aab46bd | upstream | 90cae6b..aab46bd | pass | warn | no | pass | (none) | 80.9 | upstream: [W1] review base: always an ancestor sha via resolve_review_base [state=failed: could not parse review output (8 turn(s) captured) - blocking commit to preserve gate integrity. Raw output saved: '2026] |
| upstream-90796c3-90cae6b | upstream | 90796c3..90cae6b | pass | none | yes | deny | (none) | 342.0 | upstream: docs(plans): stacked-branch review base and stale findings plan [state=failed: could not parse review output (16 turn(s) captured) - blocking commit to preserve gate integrity. Raw output saved: '202] |
| upstream-bbb7b20-90796c3 | upstream | bbb7b20..90796c3 | pass | none | yes | deny | (none) | 245.3 | upstream: test: fix CI failures on Linux and Python 3.9 [state=failed: could not parse review output (7 turn(s) captured) - blocking commit to preserve gate integrity. Raw output saved: '2026] |
| upstream-1993be5-bbb7b20 | upstream | 1993be5..bbb7b20 | pass | none | yes | deny | (none) | 139.0 | upstream: fix(hooks): ps1 hook can only print deny; exact-match doctor version probes [state=failed: could not parse review output (9 turn(s) captured) - blocking commit to preserve gate integrity. Raw output saved: '2026] |
| upstream-49d945c-1993be5 | upstream | 49d945c..1993be5 | pass | none | yes | deny | (none) | 92.9 | upstream: chore(release): 0.12.4 [state=failed: could not parse review output (10 turn(s) captured) - blocking commit to preserve gate integrity. Raw output saved: '202] |
| upstream-2b47c6f-49d945c | upstream | 2b47c6f..49d945c | pass | none | yes | deny | (none) | 205.2 | upstream: fix(doctor): narrow allowed-tools to the read-only probes it runs [state=failed: could not parse review output (9 turn(s) captured) - blocking commit to preserve gate integrity. Raw output saved: '2026] |
| upstream-c6dd780-2b47c6f | upstream | c6dd780..2b47c6f | pass | none | yes | deny | (none) | 427.8 | upstream: fix(hooks): never auto-approve Bash; pass through instead of allow [state=failed: could not parse review output (11 turn(s) captured) - blocking commit to preserve gate integrity. Raw output saved: '202] |
| upstream-da28b22-c6dd780 | upstream | da28b22..c6dd780 | pass | pass | no | pass | (none) | 98.8 | upstream: chore(release): 0.12.3 |
