"""SPEC-14 promotion dogfood: this repository's OWN promoted .ocr/config.json
(adr-wait-what + adr-integrity at mode block, commit fec83fc) firing through
the real gate. Data only -- executed by tests/sim_harness.py against a
scratch clone of claude-code-review-gate at main, with --gate-scripts
pointed at the checkout's own scripts/ (main IS the landed feature).

No SEED_FILES: the config under test is the tracked .ocr/config.json, so it
comes along in the clone and survives the harness's reset_to_origin. The
built-in ADR globs (docs/adr/**/*.md, **/adr/*.md) match this repo's ADR
layout, and .gitignore already excludes the inline-regenerated
.ocr/docs-summary.md, so fabricated commits stay clean.

The repo's config pins no `sections` list, so the wait-what duty's fallback
superset applies (Status, Date, Context, Decision, Consequences). Scenario A
carries every section but states its Decision before its Context -- the
unconditional wait-what rule. Scenario B rewrites the same record in proper
order. Both records make only claims verifiable at the tip (sentences stay
under the 25-word STE cap, prose carries no deslop-catalog tells):

  - load_settings in scripts/rg_config.py resolves the checks config
    (greppable; the function's contract says exactly that);
  - a repository overrides duty settings in .ocr/config.json at its root
    (rg_config.repo_config_path names that path, and the file is tracked in
    the tree under review);
  - ADR 0006 records that the adr-wait-what and adr-integrity duties start
    disabled by default (its Decision section says so verbatim).

ADR numbering: this repo's records run to 0006, so the fabricated record is
0007. Assertions stay loose: the harness asserts the gate's block banner
itself; the scenario only requires the record's path and the order words.
"""
from sim_harness import CommitSpec, Scenario

# Scenario A. Every fallback-superset section is present, but Decision
# precedes Context -- the unconditional wait-what rule. The claims in the
# Decision are verifiable at the tip, so adr-integrity stays silent and the
# block is the ordering finding (which quotes the misplaced ruling and the
# section order).
_ADR_0007_OUT_OF_ORDER = """# ADR 0007: Repositories opt into ADR quality duties per check

- **Status:** Accepted
- **Date:** 2026-11-17

## Decision

The adr-wait-what and adr-integrity duties block pushes in this repository.
A repository overrides a duty's settings in its own .ocr/config.json at the
repository root. The load_settings function in scripts/rg_config.py
resolves the gate's checks configuration, and the per-repository file wins
over the built-in defaults.

## Context

ADR 0006 recorded that the adr-wait-what and adr-integrity duties ship
disabled by default, because they presuppose conventions a repository may
not follow. This repository keeps decision records under docs/adr, so an
ADR (architecture decision record) that ignores the house structure or
asserts unverifiable facts should not land silently.

## Consequences

A push that adds or edits a record under docs/adr is judged against the
house ADR form and the verifiable-claims rule before it lands.
"""

# Scenario B. The same record in proper order: Context before Decision,
# every fallback-superset section present, every claim verifiable at the
# tip, short clean sentences. Both ADR duties and the default-on deslop
# duty stay silent.
_ADR_0007_FIXED = """# ADR 0007: Repositories opt into ADR quality duties per check

- **Status:** Accepted
- **Date:** 2026-11-17

## Context

ADR 0006 recorded that the adr-wait-what and adr-integrity duties ship
disabled by default, because they presuppose conventions a repository may
not follow. This repository keeps decision records under docs/adr, so an
ADR (architecture decision record) that ignores the house structure or
asserts unverifiable facts should not land silently.

## Decision

The adr-wait-what and adr-integrity duties block pushes in this repository.
A repository overrides a duty's settings in its own .ocr/config.json at the
repository root. The load_settings function in scripts/rg_config.py
resolves the gate's checks configuration, and the per-repository file wins
over the built-in defaults.

## Consequences

A push that adds or edits a record under docs/adr is judged against the
house ADR form and the verifiable-claims rule before it lands.
"""

SCENARIOS = [
    Scenario(
        name="A-dogfood-decision-before-context-blocks",
        description="ADR-0007 states its Decision before its Context; the "
                    "repo's own promoted config (adr-wait-what, mode block) "
                    "must block the push",
        commits=[CommitSpec(
            message="sim A: record the per-repo quality-duty opt-in",
            write={"docs/adr/0007-per-repo-quality-duty-opt-in.md":
                   _ADR_0007_OUT_OF_ORDER},
        )],
        expect="block",
        expect_message=["docs/adr/0007", "decision", "context"],
    ),
    Scenario(
        name="B-dogfood-fixed-record-passes",
        description="ADR-0007 rewritten with Context before Decision and "
                    "only verifiable claims; adr-wait-what and adr-integrity "
                    "(both mode block) stay silent and the push lands",
        commits=[CommitSpec(
            message="sim B: fix the record to proper ADR structure",
            write={"docs/adr/0007-per-repo-quality-duty-opt-in.md":
                   _ADR_0007_FIXED},
        )],
        expect="pass",
        forbid_message=["blocked this commit"],
    ),
]
