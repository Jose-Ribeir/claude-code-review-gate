# Review latency analysis — why a review takes 30 s or 20 minutes, and how to cut it

**Date:** 2026-11-09 · **Scope:** `kimchi-review-gate` real-model reviews (calibration corpus)
**Data source:** supervisor telemetry (`review-gate-debug.log` phase/call events, state files) from 15+ real reviews on fixture repos (8–13-line diffs) and the upstream repo (330–501-line diffs).

## TL;DR

**99.9% of review wall time is the model call; everything else is noise.** The gate's own
Python machinery (clone, plan, impact, diffs, verdict, state) takes **under 1 second**. The
reviewer session on `kimi-k3 @ max thinking` takes **35–47 s on small diffs** and scales
roughly linearly with diff/context size to **12.7 min on a 500-line diff**, with observed
extremes of 67–84 min when retries and orphaned supervisors stack. The knobs that matter
are: what model/thinking tier runs the *orchestration*, how big the context is, retries,
and (on TS repos) tsserver cold start.

## Measured breakdown

Per-run telemetry (worktree → plan → priors → impact → diffs → **call** → finish):

| Phase | Time (all runs) |
|---|---|
| worktree (clone sandbox) | 0.04–0.07 s |
| plan / priors / impact / diffs | 0.02–0.32 s |
| **reviewer call (the model session)** | **34.9–763.7 s** |
| finish (verdict, store, state) | 0.03–0.04 s |
| session first event (startup) | 0.63–0.69 s |

Reviewer call time vs diff size:

| Corpus | Diff lines | Call time |
|---|---|---|
| py-app / electron-ts fixtures | 8–13 | 35–47 s |
| upstream, docs/small change | ~0–small | 40 s, 119 s |
| upstream, 330-line diff | 330 | 180 s (+ a wasted 115 s error retry on one run) |
| upstream, 501-line diff | 501 | **764 s** |

Observed extremes: two supervisor runs totaled **4043 s** and **5040 s** — both stacked
failure modes (retries and/or orphaned supervisors from killed harness processes; see §3.5).

## Root causes, ranked

1. **Reviewer persona at `kimi-k3 @ max thinking` (D16 pin).** Every turn is a deep
   reasoning pass. The reviewer makes many turns (reads the manifest, diffs, referenced
   files, symbol context). This is the dominant cost and the deliberate quality choice.
2. **The main session runs on the reviewer-tier model too.** The worker argv pins
   `--model kimchi-dev/kimi-k3` for the *whole session* — the /skill:review orchestration
   (manifest reads, §2b greps, spawn bookkeeping) also pays the slow, deep model for what
   is mechanical work.
3. **Context volume scales with the repo and diff.** The skill reads precomputed diffs +
   cross-file symbol context; a 9k-line upstream repo with a 500-line diff produces a
   27 KB manifest and many follow-up reads. Each read is another max-thinking turn.
4. **Wasted retries.** One upstream run shows `outcome=error s=115.13` followed by
   `outcome=ok s=180.45` — the first call failed (pre-normalizer parse failure class) and
   the retry re-paid the whole session. Retry cost doubles latency exactly when the model
   misbehaves.
5. **Orphaned supervisors from killed harness processes.** The calibration harness was
   killed at the 3600 s safety cap mid-review; the detached supervisor kept running
   (correct — verdicts still landed), but the *next* harness run's push for the same tip
   waited on the fence, inflating apparent wall time (a 764 s review recorded as a 1145 s
   row; the 4043/5040 s totals are stacked cases).
6. **tsserver/LSP cold start (TS repos only — not in these numbers).** Predicted +10–60 s
   on the first TS review in a fresh clone; the upstream corpus is Python/MD so this
   hasn't fired yet, but electron-ts reviews pay a smaller version of it.
7. **Canary probe per supervisor process** (~10–40 s: one extra headless session before the
   first real call). One per push, by design (security), but it is pure overhead on
   already-known-good installs.

## Speed-up options (impact × effort × risk)

| # | Change | Est. saving | Effort | Risk |
|---|---|---|---|---|
| 1 | **Split orchestration from review**: run the main worker session on a fast model (e.g. `glm-5.3-flash` / `auto`) and keep ONLY the reviewer persona spawn at `kimi-k3 @ max`. The argv `--model` pin becomes the orchestration tier; personas already carry their own D16 pins. | 20–40% of turns at 10–50× cheaper/faster per turn | small (argv default + golden regen + one test) | low — persona quality unchanged |
| 2 | **Drop the reviewer thinking tier from `max` → `high`** | 30–50% per turn | trivial (one prompt field) | **quality** — must re-run the FP study; the user picked the super-thinker pin deliberately |
| 3 | **Fix the retry waste**: the normalizer (already landed) removes the known first-call parse-failure class; add telemetry on `outcome=error` calls and consider cheaper retry (reuse the same session's transcript instead of a fresh session, if pi-mono allows resume) | removes 1× call doubling on failure runs | medium | low |
| 4 | **Cap context growth**: limit hunk context lines in precomputed diffs for large files; skip reading unchanged regions; batch manifest reads into fewer turns | 10–30% on big diffs | medium (skill edits) | low-medium (reviewer sight reduction — keep the truncation warnings) |
| 5 | **LSP warm start / skip when no TS in the diff** | 10–60 s on TS repos | small | low |
| 6 | **Falsify pass on clean-looking reviews**: the falsify/verification passes are conditional already; ensure they don't run when the reviewer found nothing (verify — if they do run unconditionally, gating them saves a whole subagent session on clean pushes) | one subagent session on clean pushes | small | low |
| 7 | **Cross-push amortization** (persistent reviewer daemon / session reuse / prompt caching across pushes of the same repo) | large on repeat pushes | high (architecture) | medium |
| 8 | **Parallel chunks** — already implemented (C6) for multi-chunk pushes; no effect on single-chunk (most) pushes | — | — | — |

**Recommended order:** #1 first (pure win, no quality tradeoff), then #3 and #6 (waste
removal), then measure. Only touch #2 (thinking tier) after the FP study completes and
only if the FP rate has headroom. #4/#5 as tuning. #7 only if the gate becomes a daily
latency complaint.

## What the numbers say NOT to bother with

- **The Python gate machinery is not the problem** (sub-second end-to-end).
- **Session startup is not the problem** (first event in 0.65 s).
- **Chunk parallelism is already there** for the cases it helps.

## Appendix: how to measure again after changes

```
cd kimchi-review-gate
.venv/bin/python scripts/ocr_calibrate.py --fn            # fixture timings, ~6 reviews
grep -E "event=(call|run)" /tmp/ocr-calib/state/data/review-gate-debug.log | tail
# wall_s column in docs/calibration.md is the end-to-end per-push view
```
The `call` rows (turns, api_s, first_event_s) are the model-cost ground truth; `run` rows
are the supervisor totals; the delta between `run` and row `wall_s` exposes
harness/fence/orphan overhead, which is measurement, not review latency.

## Postscript (2026-11-09): recommendation #1 tested and invalidated

Rec #1 (fast orchestration tier) was implemented and measured: with the worker
session on `kimchi-dev/glm-5.3-flash`, every FN fixture review ran 9–12 turns,
surfaced its findings, and then **died unparseable** ("could not parse review
output", verdict None on 6/6 rows) — with wall time tripled to 191–279 s by
error+retry churn. The skill's orchestration is not mechanical enough for a
flash tier: it drives the persona spawns and the verdict emission, and a weak
session model breaks both. **Default reverted to `kimchi-dev/kimi-k3`**;
`OCR_MODEL` remains the experiment hatch. The remaining valid levers are
#2 (reviewer thinking tier, quality-gated), #3 (retry waste — the normalizer
already removed the known class), #4 (context caps) and #7 (amortization).
