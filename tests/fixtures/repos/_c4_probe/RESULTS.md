# C4 acceptance — probe + results

Date: 2026-11-06 · kimchi 1.27.0 (darwin/arm64, `~/.local/bin/kimchi`).
Scratch project: `/tmp/rg-c4-test/proj` = clone of `tests/fixtures/repos/clean-ts.git`
with `.kimchi/skills/review` symlinked to the repo's `skills/review`.
Reproduce with `./probe.sh` (this directory).

## Method

    cd /tmp/rg-c4-test/proj
    OCR_IN_REVIEW=1 kimchi -a -p "/skill:review --range 8fcc2a1..0544617 --json" \
      --mode json --no-session > stream2.jsonl

Range `HEAD~2..HEAD` = the `calc` + `index` commits of clean-ts (3 new files,
strict-mode TS, `tsc --noEmit` clean, no seeded bugs).

## Results

| Acceptance check | Result |
|---|---|
| (i) skill loads headlessly | **PASS** (with `-a` — see finding 1): `skill_view` → `Loaded skill 'review'`; the orchestrator then followed SKILL.md §0–§6 (allowlist, rubric, per-file diffs with `-M`, language rules, electron grep check, §2b, verdict) |
| (ii) built-in subagent spawns with pinned model | **PASS**: two `Agent` calls — reviewer `subagent_type:"Reviewer"`, `model:"kimchi-dev/kimi-k3"`, `thinking:"max"`, prompt = full code-reviewer persona body (23,091 chars); falsify `subagent_type:"General-Purpose"`, `isolated:true`, `model:"kimchi-dev/deepseek-v4.1-flash-1017"`, `thinking:"high"`, prompt = code-filter body (7,694 chars). Evidence: `agent-spawn-evidence.jsonl` |
| (iii) verdict JSON parseable by the gate | **PASS**: `_extract_from_stream_json` (which runs `_extract_json` on every `message_end`/`turn_end` text block) extracted a schema-valid verdict — `verdict.json` in this directory |
| (iv) rubric/allowlist/rules byte-identical to upstream | **PASS**: `diff -r` upstream vs repo over `rubric.md`, `allowlist.md`, `rules/` (excluding the NEW `electron.md`) — empty |

Extracted verdict (clean fixture): `status: success`, `verdict: block` —
2 medium + 1 low finding, no highs. Under D15 a medium at confidence 0.85
blocks, which is what happened. Findings themselves: one plausible design nit
(unweighted `averageUnitPrice` mean) and one test-coverage note — i.e. the
fixture is "clean" for high-severity purposes but the reviewer does have
opinions. Detection-quality/false-positive calibration is C5's scope; C4 is
format acceptance only.

## Findings to bounce to the director

1. **`-a` / `--approve` is REQUIRED for project-local skill discovery in
   untrusted directories.** Without it, a `/skill:review` invocation in the
   /tmp clone silently finds no skill: `skill_view` → `Skill 'review' not
   found`, and the model then improvises a review with a NON-gate JSON schema
   (`verdict: "approve_with_comments"` — `_extract_json` correctly rejects
   it, so the gate still fails closed, but every review burns a full context
   for nothing). Verified both with a symlinked and a real-copied
   `.kimchi/skills/review` directory; both load fine with `-a`. Consequence
   for the port: the C2 worker argv (or the C1 supervisor, which owns the
   clone) must pass `--approve` when it runs the skill inside the scratch
   clone — OR ship the skill at user level (`~/.local/share/kimchi/skills/`
   / package manifest) instead of the clone. That decision belongs to the
   director/C8 packaging, not to this chunk.
   (Experiment #3 in `.kimchi/docs/kimchi-port-experiments.md` did not hit
   this because it ran from the trusted workspace directory, not /tmp.)

2. **No FALLBACK needed.** Subagent spawning works headlessly (finding above
   is a skill-discovery trust issue, not an Agent-spawn issue), so the
   SKILL.md ships without an inline-persona fallback — as instructed, the
   fallback was only to be added if spawning itself failed.

3. Electron auto-activation was exercised: the orchestrator ran the §2 item 6
   grep (`\bfrom\s+['"]electron['"]|require(...)`) over the selected files —
   no match, so `electron.md` was correctly NOT appended.

## Artifacts

- `probe.sh` — reproduction script (creates /tmp scratch, runs the probe,
  validates the verdict with `scripts/review-gate.py`'s extractors).
- `agent-spawn-evidence.jsonl` — the two `tool_execution_start` events for
  the Agent spawns (full args minus persona prompt bodies, which are the
  SKILL.md Appendix P texts).
- `verdict.json` — the extracted verdict object from the clean-fixture run.
- The full 21,583-event stream (33 MB) was transient (`/tmp`); not committed.
