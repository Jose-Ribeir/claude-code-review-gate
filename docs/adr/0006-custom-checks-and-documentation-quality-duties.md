# ADR 0006: The checks config surface and the documentation-quality duties

- **Status:** Accepted
- **Date:** 2026-11-16
- **Deciders:** review-gate maintainers (operator-confirmed, grilled via the
  docs-quality-checks spec behind SPEC-14)
- **Implementation:** the layered `checks` resolver in
  `scripts/rg_config.py` (SPEC-14.1, `e582f80`); the evidence and
  finding-contract module `scripts/ocr_docsquality.py` with catalog
  `scripts/deslop_catalog.json` (SPEC-14.2, `6d40dec`); the docs-checker
  persona's quality-duty sections (SPEC-14.3, `9ac38dd`); the gate plumbing
  for the `docs_quality` payload, spawn rule, and command-check execution
  (SPEC-14.4, `8f501d7`).

## Context

Until this surface, the gate judged documentation only as a constraint on
code: the docs-checker's three legacy duties (contradiction with a decision
record, missing decision record, stale drift) all ask "does the code change
disagree with the docs". The reverse surface was unchecked — an ADR could
ignore the repository's own structure conventions, read like
machine-generated slurry, misuse the project's glossary vocabulary, or cite
files, functions, and other ADRs that do not exist or do not say what it
claims, and the push landed. Prose and claims were nobody's job.

There was also no home for personal checks. Every new check idea became a
bespoke feature touching gate code, because the settings resolver knew
exactly the keys it shipped and nothing else.

## Decision drivers

- **Helpful out of the box, never presumptuous.** A default must apply to
  any repository; a duty that presupposes conventions a repository may not
  follow must start off.
- **Code proves existence, the model judges truth** — the arch_flags
  pattern: deterministic pre-evidence computed gate-side, agent judgment on
  top of it, never regex verdicts on prose.
- **One agent for documentation.** All documentation judgment happens in one
  spawn, in one context, at one cost.
- **A drifting model cannot block a push on a nit-pick.** Anything weaker
  than a proven pattern must degrade, never block.
- **`compute_verdict` remains the only verdict authority** (ADR 0001);
  quality findings ride the normal findings stream and falsify pass.

## Decision

1. **A `checks` object on the existing layered settings.** Built-in defaults
   first, then the global store-root config (`<store root>/config.json`),
   then the per-repo override (`<repo>/.ocr/config.json`); per-repo wins. Invalid values degrade with a load
   note and are never fatal — no config error can break the gate. Per check:
   `enabled`, `mode` (`"block"` or `"warn"`, default block), `globs`
   (repo-relative; the check runs only when matched files are in the push
   diff), plus the duty options `sections`, `template`, `sentence_cap`,
   `sweep`, and `command`. Unknown keys are noted and ignored.
2. **Built-in duties and their defaults.** `docs-deslop` ships **enabled**
   (it applies to any repository with markdown; globs `["**/*.md"]`).
   `adr-wait-what` and `adr-integrity` ship **disabled** — they presuppose
   ADR conventions (a structure, a glossary, a records root) a repository
   may not follow (globs `["docs/adr/**/*.md", "**/adr/*.md"]`). Everything
   blocks by default; `"warn"` mode demotes to a warning at the verdict.
3. **One agent, three new duty sections.** The existing docs-checker
   persona gains the three quality duties alongside its three legacy ones;
   each runs only when its flag in the manifest's `docs_quality.enabled_duties`
   is true. The checker spawns when the manifest carries `docs_context`
   (today's rule) **or** `docs_quality`; absent both, no spawn and no cost.
4. **Deterministic evidence, agent judgment.** The gate-side helper
   `scripts/ocr_docsquality.py` computes per-duty evidence against the
   review tree at the tip — the deslop catalog scan (163 entries snapshotted
   from the deslop skill's references into `scripts/deslop_catalog.json`),
   throat-clearing, em-dash density, and template skeletons for
   docs-deslop; STE surface stats (sentence-length outliers, acronym table,
   passive-voice estimate) for adr-wait-what; existence facts
   (`missing_paths`, `dangling_links`, `unresolved_adr_refs`) for
   adr-integrity — and emits it into the manifest's `docs_quality.evidence`
   block in the established arch_flags pattern. Context-before-Decision is
   enforced unconditionally; an ADR that opens with its conclusion failed
   its reader before line three. The ADR structure a duty checks against is
   the repository's own — a `sections` list or a `template` file — with the
   plugin's superset (Status, Date, Context, Decision, Consequences) only as
   fallback. The helper follows the house pattern: JSON in, JSON out,
   fail-soft, telemetry, generated schema
   (`schemas/docsquality.schema.json`).
5. **The deslop cluster rule is enforced gate-side.** A deslop finding must
   carry a `tells` array with at least three co-located tells (phrase plus
   line) or `document_wide: true`; anything weaker is demoted to note
   severity by `ocr_docsquality.py --validate` and can never block — a
   drifting model cannot nit-pick a lone em dash into a blocked push.
6. **Quality findings anchor at the documentation file** they concern — a
   deliberate, documented exception (the `docs_quality_duty` field) to the
   checker's code-anchoring rule, for the quality duties only. They merge
   into the same findings stream, pass the same falsify pass, and enter
   `compute_verdict` like every other finding.
7. **User command checks.** Declaring a check name with a string `command`
   creates a command check (default `{enabled: true, mode: "block", globs: []}`
   — the declaration itself is the enabling act); a declaration without
   `command` reads as a typo of a built-in name and is ignored with a note.
   Execution contract: the repo-relative executable runs with cwd at the
   review tree root, receives the matched changed files as a JSON array on
   stdin, and answers with a findings JSON array (bare or wrapped in
   `{"findings": [...]}`) on stdout, within 60 seconds. Nonzero exit,
   timeout, or malformed output discards the check's findings and records a
   warning plus telemetry — never a silent pass, never a crash. `"warn"`
   mode clamps severity to at most medium and confidence to at most 0.5;
   mode only downgrades, and the verdict stays `compute_verdict`'s.
8. **Docs-only pushes pay for enabled quality duties.** With an enabled
   quality duty and matching files in the diff, a docs-only push now runs
   the documentation checker — the docs changer pays for the docs check,
   mirroring the docs-gate 05 freshness cost rule. With no payload, the
   0.7.0 no-manifest path is unchanged. The `sweep` option (default off)
   widens ADR-duty evidence to every `*.md` under the records root; the
   change set stays the findings scope.
9. **v1 is markdown only.** The `commit_messages` and `code_comments` deslop
   stubs ship in the surface, inert, so the toggle shape is ready for v2
   without a later config-format change.

## Consequences

- **Extending the surface has two distinct costs.** A personal check costs a
  config block and an executable in the reviewed repository — no gate code.
  A new built-in duty costs a persona section, an evidence class in the
  helper, and a manifest flag — an explicit decision (a new ADR), not a
  drive-by.
- **Every failure path is fail-soft and observable.** Config errors degrade
  to load notes; command-check errors discard that check's findings with a
  warning and a telemetry record; sub-threshold deslop findings demote to
  notes. Nothing new can crash the gate, and nothing new can silently pass
  it.
- **A docs-only push may now pay a checker run.** Operators who push
  documentation to repositories with quality duties enabled see one agent
  spawn where 0.7.0 spawned none; disabling the duties restores the old
  cost profile.
- **The ADR duties are opt-in and expect conventions to exist.** A
  repository enabling them should have ADRs under the watched globs and,
  for full value, a structure sections list or template and a glossary —
  none of which the gate imposes; absent them the duties fall back to the
  superset and degrade their checks rather than invent conventions.
- **The v2 stubs change nothing in v1.** Setting `commit_messages` or
  `code_comments` options has no effect until their implementation lands;
  they exist so tomorrow's surface does not need a format break.
- **This ADR is itself subject to the duties it records** — the deslop scan
  and the wait-what structure expectations apply to this file, which is why
  Context precedes Decision.

## References

- Spec: `.kimchi/tracker/tickets/spec-14-docs-quality-checks.md` (stories
  1–25) and the working doc `.kimchi/plans/docs-quality-checks-spec.md`.
- Land commits: `e582f80` (checks config surface), `6d40dec` (evidence and
  finding-contract module), `9ac38dd` (persona duty sections), `8f501d7`
  (gate plumbing).
- Manifest contract: `skills/review/SKILL.md` §2 (`docs_quality`) and §3
  ("Documentation check").
- ADR 0001 (verdict authority, fail-soft degradation as recorded skill).
