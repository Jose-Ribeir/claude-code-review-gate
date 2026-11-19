# Feature → agent map (and where quality and speed are decided)

**Date:** 2026-11-19 · **Basis:** `agents/*.md`, `skills/review/SKILL.md`, `scripts/review-gate.py`,
`scripts/rg_*.py`, `scripts/ocr_*.py`, ADR 0001/0008/0009, `docs/review-latency-analysis.md`,
`docs/calibration.md`. Read from source; nothing here was re-measured. Statements marked
*(hypothesis)* are inferences to test, not facts.

## 1. The six agents

| Agent | File | Tools | Model tier (claude profile / kimchi profile) | Runs when | Returns |
|---|---|---|---|---|---|
| **context-spotter** | `agents/context-spotter.md` | none | `haiku` / `deepseek-v4-flash` (`OCR_SPOTTER_MODEL`) | every push with a diff (not `--scan`) | *demands* (callers, docs, event couples, references) + `risk.score`; never findings |
| **code-reviewer** (tier 1) | `agents/code-reviewer.md` | Read, Grep (≤3 Reads/file, ≤5 Grep per review) | `sonnet` / `kimi-k3 @ max` (`OCR_TIER1_MODEL`) | every push on the fast lane | findings JSON |
| **deep-reviewer** (tier 2) | `agents/deep-reviewer.md` | Read, Grep, Glob, Bash (read-only git) | `opus` / `kimi-k3 @ max` (`OCR_TIER2_MODEL`) | **instead of** code-reviewer when risk-escalated | findings JSON (same contract) |
| **docs-checker** | `agents/docs-checker.md` | none declared in frontmatter (reads docs itself per its prompt) | `kimchi-dev/glm-5.3 @ high` (pinned in SKILL.md; no claude tier) | only if the manifest carries `docs_context` or `docs_quality` | findings JSON incl. stale-doc, missing-ADR, prose-quality |
| **code-filter** | `agents/code-filter.md` | none | `deepseek-v4.1-flash-1017 @ high` (pinned) | only if the verdict would otherwise be `block` | `{drop_ids}` — drop-only falsify |
| **code-resolver** | `agents/code-resolver.md` | Read, Grep | `glm-5.3 @ high` (pinned) | `--resolve` mode only (re-check prior findings after a fix) | per-finding `resolved` / `still_present` + quote |

The orchestrating session itself is also a model call: the `/review-gate:review` skill is an LLM
following `SKILL.md`, and it is pinned to the reviewer-tier model (`OCR_MODEL`).

Backend tiers are chosen in `scripts/review-gate.py:183-213` (`_tier_model`); the claude defaults are
`spotter=haiku, tier1=sonnet, tier2=opus`. The kimchi side is guarded against anthropic/openai
models; the claude side is exempt (ADR 0005).

## 2. Pipeline for a normal pre-push review

```
git push ──► pre-push hook (primary gate)        ──► detached supervisor ──► `claude -p` / `kimchi -p` worker
             PreToolUse hook (UX layer)               (deny first push, join on re-push)
                                                              │
   ┌────────────────────── deterministic Python (<1 s total, no model) ──────────────────────┐
   │ range + stacked base · risk gate · chunking · precomputed diffs · Part-S units ·        │
   │ impact sites · known defects · ledger/priors · memory (dismissals) · docs_context ·     │
   │ commit/ticket intent · command checks (parallel subset starts here)                     │
   └──────────────────────────────────────────┬──────────────────────────────────────────────┘
                                               ▼
 ┌─ inside the review session (an LLM following SKILL.md) ─────────────────────────────────┐
 │ 2b symbol context (greps)                                                               │
 │ 2c  context-spotter ──► ocr_demands.py (validate) ──► 2d ocr_collectors.py (evidence)   │
 │ 3   code-reviewer   (or deep-reviewer if risk.score ≥ 7 / deep_review glob hit)         │
 │      └ >15 files: one reviewer per top-level dir, ≤4 in parallel                        │
 │ 3   docs-checker        (only if docs context)           ← runs AFTER the reviewer      │
 │ 3a  hallucination check (string match)        ┐                                         │
 │ 3b  code-filter         (only if would block) │ ← all run by the LLM session            │
 │ 4   dedup (≥4 findings) · 6 render + verdict  ┘                                         │
 └──────────────────────────────────────────┬──────────────────────────────────────────────┘
                                               ▼
        Python re-parses, normalizes, and computes the verdict (ocr_verdict.compute_verdict):
        any high blocks · medium blocks at confidence ≥ 0.7 · fail closed on anything odd
```

Nothing the model says is the verdict; `compute_verdict` is the sole authority (ADR 0001).

## 3. Feature → who runs it

| Feature | Runs in | Agent(s) | Model-free? |
|---|---|---|---|
| Block/warn/pass verdict | `ocr_verdict.py` | none | yes |
| Push interception, async deny/join, fail-closed | hooks + `review-gate.py` supervisor | none | yes |
| Sandbox (deny extension + startup canary) | `review-gate-deny.ts`, canary probe | one extra headless session (canary) | no (canary only) |
| Per-file bug review | skill §3 | **code-reviewer** | no |
| Cross-file breakage (callers, renames, signatures) | `ocr_impact.py` finds sites → reviewer judges `impact_verdicts` | code-reviewer | search is model-free; judgment is not |
| Context demands → evidence | spotter → `ocr_demands.py` → `ocr_collectors.py` | **context-spotter** | only the first hop |
| Risk escalation (migrations, auth, payments, concurrency, `**/api/**`) | `_DEFAULT_RISK_GLOBS` + spotter `risk.score` | **deep-reviewer** | globs yes, score no |
| Comment-consistency nits (non-blocking) | deep lane | deep-reviewer | no |
| Documentation enforcement (ADR contradiction, missing ADR, stale docs) | skill §3 docs step | **docs-checker** | doc context assembled mechanically |
| Docs prose quality / ADR form / claim integrity | `docs_quality` + `ocr_docsquality.py` | docs-checker | partly |
| Falsify pass | skill §3b | **code-filter** | no |
| Hallucination check, dedup, render | skill §3a/§4/§6 | the orchestrating session (no subagent) | **could be** (see §5) |
| Intent claims (commit messages, tickets) | manifest → reviewer persona | code-reviewer (`intent_gap`) | fetch is model-free |
| Big files: stable units, resume, replay | `ocr_segment.py` + ledger | code-reviewer sees units | yes |
| Chunking / parallel chunks / run budget | `_run_chunked` | one worker per chunk | yes |
| Re-check priors after a fix | `--resolve` | **code-resolver** (gate re-checks itself if it fails) | no |
| Dismissal memory | `ocr_memory.py`, `/dismiss` | none | yes |
| Custom command checks / test checks (SPEC-15), incl. parallel subset | `review-gate.py` | none | yes |
| MR/PR consult (docs-only MR carrying code) | `rg_mr_consult.py` | none | yes |
| **Final whole-stack review** (`final-review`) | `rg_final_*.py` | 7 lens workers per unit (below) | orchestration yes, lenses no |
| Doctor, janitor, calibration, telemetry, report | `ocr_*.py` | none | yes |

### Final-review lenses (ADR 0008)

Seven independent workers per review unit, each using the code-reviewer persona (the
`documentation/domain` lens uses the docs-checker persona), in the immutable full-tree checkout,
up to 4 at once (`RG_FINAL_LENS_WORKERS`): `correctness`, `repository-standards`,
`documentation/domain`, `testing`, `security`, `compatibility/migrations`, `change-intent`.
Units: 1 for a single ref, or N individual + 1 combined + 1 prospective for an N-item stack, so
an N-stack is up to **7 × (N+2)** worker sessions. Native transport
(`RG_FINAL_REVIEW_TRANSPORT=native`, `OCR_DEEP_TRANSPORT=native`) replaces nested orchestration
with a supervisor-side scheduler (4 inference + 2 execution slots); it is opt-in until real-model
acceptance #21 (ADR 0009).

## 4. Where time goes (from `docs/review-latency-analysis.md`, kimchi corpus)

- Python machinery is under 1 s; the reviewer session is **35–47 s on 8–13-line diffs, ~180 s at
  330 lines, ~764 s at 501 lines**. 99.9% of wall time is model calls.
- Retries double the cost when a call errors (one run: 115 s failed + 180 s retry).
- Canary probe adds roughly 10–40 s per supervisor.
- Fast orchestration tier was **tried and failed** (flash session could not drive persona
  spawns and verdict emission; 6/6 unparseable). Do not retry without changing what the
  orchestrator has to do.
- The latency numbers come from the kimchi profile. No equivalent claude-profile measurements
  exist in the repo, so the sonnet/opus/haiku tiers are unmeasured.

## 5. Improvement levers

Ordered by expected value; each says what would have to be true.

### Speed

1. **Run the docs-checker concurrently with the reviewer.** Its inputs (diffs, `docs_context`,
   `arch_flags`, `docs_quality`) do not depend on reviewer findings, yet SKILL.md runs it after the
   reviewer finishes. Saves one full subagent call on any push with docs context. Low risk; the
   merge step already exists.
2. **Move the mechanical steps out of the LLM session.** §3a (string-match hallucination check),
   §4 (dedup), and the §6 render are deterministic or near-deterministic, yet each costs a
   session turn, and Python recomputes the verdict anyway. This is the safe form of the failed
   "fast orchestrator" experiment: shrink what the orchestrator does instead of weakening it.
   The native transport (ADR 0009) already does this for final-review and deep; the pre-push
   fast lane does not use it. *(hypothesis: also shrinks the retry class that was "unparseable
   output".)*
3. **Skip the spotter on trivially small or docs-only pushes.** It runs on every push, and
   its output only matters when there is outside-diff context to gather. Needs a deterministic
   size/path rule and a check that miss rates on the calibration corpus do not move.
4. **Cheaper retries.** Up to 3 attempts per tip, each a fresh session. Resume or reuse a
   transcript where the backend allows; at minimum, log `outcome=error` calls (the analysis
   already asks for this).
5. **Drop the per-supervisor canary** on installs that already passed it (cache by extension
   hash and CLI version).
6. **Final-review cost control.** `7 × (N+2)` sessions: share the immutable checkout and common
   evidence across lenses; consider skipping lenses with no applicable evidence (e.g.
   `compatibility/migrations` when nothing public/persisted changed), recorded as a visible
   skip so it never reads as a pass.
7. Chunk parallelism helps only when there are more than 15 files or 1200 lines; single-chunk
   pushes (the common case) get nothing from it.

### Quality

1. **The spotter is the quality ceiling for cross-file review.** The reviewer is told to treat
   `cross_file_context`, `impact_sites` and `evidence_bundle` as its sole cross-file evidence
   and may spend at most 5 Greps; the evidence is determined by a *haiku-tier* spotter's demands.
   If the spotter misses a coupling, nobody looks. Measure spotter recall against the Part-S
   scenarios in `docs/benchmark-part-s.md` before changing any reviewer model.
2. **The filter cannot falsify cross-file findings.** It has no tools, sees only the cited
   file's diff, and is told to keep anything it cannot prove wrong. Cross-file highs are exactly
   the findings the diff cannot refute, so they pass through effectively unchecked. *(hypothesis)*
   Give it read-only tools for findings whose evidence is outside the diff, or verify those
   mechanically against the impact site snippet.
3. **Pin the tiers deliberately and measure.** The claude profile defaults (haiku/sonnet/opus)
   have no calibration run in `docs/calibration.md`; the kimchi data does. Run
   `scripts/ocr_calibrate.py` on the claude backend before trusting either lane.
4. **Check the calibration rows that look wrong.** `docs/calibration.md` has rows expecting
   `block` that returned `pass`/`warn` while listing `critical`/`major` findings, mostly flagged
   `[cached verdict]`, and a `pass` row that blocked on a SQL injection in a "clean" variant
   (235 s). Several predate the verdict normalizer, so they may be stale. Re-run before drawing
   conclusions.
5. **Deep lane trigger breadth.** Globs like `**/api/**` and `**/session*.py` escalate a push to a
   minutes-long deep review; the code comments put escalation at roughly one in seven pushes.
   Track escalation rate and the deep lane's unique-finding yield; narrow globs that never
   produce a finding the fast lane would have missed.
6. **Docs-checker blocks pushes** (same bar as code findings) but has no tier pin on the claude
   profile and declares no tools in its frontmatter. Decide its model and tools explicitly.

### Measure before changing

`scripts/ocr_calibrate.py` for fixture timing and false-positive/negative rows;
`review-gate-debug.log` `event=call|run|chunk` rows for per-call turns and seconds; the Part-S
scenario set for cross-file recall. Capture a baseline per agent first: calls per push, seconds
per call, and findings kept versus dropped by the filter. That table does not exist yet and
would tell us which of the levers above is worth the effort.
