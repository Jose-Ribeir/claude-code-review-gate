"""The five docs-gate acceptance scenarios against a scratch copy of kimchi-studio
(spec user story 26). Data only -- executed by tests/sim_harness.py.

What is seeded, and why
-----------------------
kimchi-studio already carries 84 formal ADRs under docs/adr/, AGENTS.md,
GLOSSARY-MAP.md and README.md, so no corpus has to be invented. SEED_FILES
adds exactly ONE extra decision record, ADR-0085, to the scratch baseline
(before the hook is installed -- it is part of the pushed base commit):

  ADR-0085 ("The Kimchi CLI is spawned only by the ACP service layer") is a
  binding decision the harness controls word for word, so S1's contradiction
  and the assertion on the block message do not depend on how a model happens
  to interpret one of the project's 84 real ADRs. It is numbered next in
  sequence per ADR-0059 (ADR numbering is linted).

S3's record (ADR-0086) is NOT seeded: it is committed by the scenario itself,
which is the entire point of the scenario (the record is added by the push).
Note the gate's fix-recognition convention (spec: "a fix to a documentation
finding must touch at least one code file that references the record"): S3's
code file carries a `// decision: docs/adr/0086-...` reference comment, and
the push would re-block without it.
"""
from sim_harness import CommitSpec, Scenario

# --- seeded baseline record (scratch only, never the original) ---------------

_SEED_ADR = """# ADR-0085: The Kimchi CLI is spawned only by the ACP service layer

## Status

Accepted

## Context

The ACP service layer owns the CLI subprocess lifecycle: launch target
resolution, stdio wiring, crash recovery and permission brokering. Code that
spawns the CLI elsewhere bypasses all of it.

## Decision

Production code in `packages/app` must never spawn the Kimchi CLI or any
other child process directly. All CLI subprocess creation goes through the
ACP service layer (`packages/app/src/main/acp/`). Importing
`node:child_process` anywhere else under `packages/app/src` is forbidden.

## Consequences

New launch paths are added to the ACP service layer, never beside their
caller. This record is binding for the documentation gate.
"""

SEED_FILES = {"docs/adr/0085-cli-spawn-only-via-acp-service.md": _SEED_ADR}

# --- shared payloads ----------------------------------------------------------

_SLUG_TS = """import { kebabCase } from 'lodash-es';

export function slugifySessionTitle(title) {
  return kebabCase(title.trim()).slice(0, 80);
}
"""

_SLUG_TS_REFERENCING = """// decision: docs/adr/0086-lodash-es-for-slug-helpers.md
import { kebabCase } from 'lodash-es';

export function slugifySessionTitle(title) {
  return kebabCase(title.trim()).slice(0, 80);
}
"""

_PKGJSON_OLD = '    "@cast/analytics": "^0.1.0",'
_PKGJSON_NEW = '    "@cast/analytics": "^0.1.0",\n    "lodash-es": "^4.17.21",'

_DIRECT_LAUNCH_TS = """// A shortcut launch path that bypasses the ACP service layer entirely.
import { spawn } from 'node:child_process';

export function launchKimchiCliDirectly(args) {
  spawn('kimchi', args, { stdio: 'pipe' });
}
"""

_RECORD_ADR_0086 = """# ADR-0086: lodash-es for slug helpers

## Status

Accepted

## Context

Session titles need URL-safe slugs in the main process. Hand-rolled casing
rules drift; a maintained helper library is the smaller liability.

## Decision

Add the `lodash-es` dependency to `packages/app` and use its `kebabCase` for
slug generation in `packages/app/src/main/sim/slug.ts`. That module references
this record in a comment (the gate's fix-recognition convention).

## Consequences

The dependency manifest gained a runtime dependency -- recorded here, as the
documentation gate requires for flagged architectural changes.
"""

_CONFIG_OLD = (
    "      // KIMCHI_CLI_PATH: dev-only override pointing the harness at a custom\n"
    "      // CLI build (e.g. a local worktree) without touching the shared config\n"
    "      // file. Ignored in packaged builds: they launch the bundled binary only.\n"
    "      process.env.KIMCHI_CLI_PATH || this.config.kimchi.executablePath"
)
_CONFIG_NEW = "      this.config.kimchi.executablePath"

_GITSTATUS_OLD = (
    "export function untrackedPaths(files: GitFileStatus[]): string[] {\n"
    "  return files\n"
    "    .filter((f) => f.index === '?' && f.working === '?')\n"
    "    .map((f) => f.path);\n"
    "}"
)
_GITSTATUS_NEW = (
    "export function untrackedPaths(files: GitFileStatus[]): string[] {\n"
    "  const isUntracked = (f: GitFileStatus): boolean =>\n"
    "    f.index === '?' && f.working === '?';\n"
    "  return files.filter(isUntracked).map((f) => f.path);\n"
    "}"
)

# --- the five scenarios -------------------------------------------------------

SCENARIOS = [
    Scenario(
        name="S1-contradiction-blocks",
        description="code spawning the CLI directly contradicts seeded ADR-0085",
        commits=[CommitSpec(
            message="sim S1: direct CLI launch path bypassing the ACP service",
            write={"packages/app/src/main/sim/directCliLaunch.ts": _DIRECT_LAUNCH_TS},
        )],
        expect="block",
        expect_message=["0085", "child_process"],
    ),
    Scenario(
        name="S2-arch-change-without-record-blocks",
        description="new runtime dependency (dependency_manifest flag), no record",
        commits=[CommitSpec(
            message="sim S2: slugify session titles with lodash-es",
            write={"packages/app/src/main/sim/slug.ts": _SLUG_TS},
            replace=[("packages/app/package.json", _PKGJSON_OLD, _PKGJSON_NEW)],
        )],
        expect="block",
        expect_message=["missing decision record", "dependency_manifest"],
    ),
    Scenario(
        name="S3-same-change-with-record-passes",
        description="S2's change plus ADR-0086 and a referencing code touch",
        commits=[CommitSpec(
            message="sim S3: slugify session titles with lodash-es (recorded)",
            write={"packages/app/src/main/sim/slug.ts": _SLUG_TS_REFERENCING,
                   "docs/adr/0086-lodash-es-for-slug-helpers.md": _RECORD_ADR_0086},
            replace=[("packages/app/package.json", _PKGJSON_OLD, _PKGJSON_NEW)],
        )],
        expect="pass",
        forbid_message=["blocked this commit"],
    ),
    Scenario(
        name="S4-stale-readme-blocks",
        description="removing the KIMCHI_CLI_PATH override makes the README's "
                    "Dev-environment-variables row stale",
        commits=[CommitSpec(
            message="sim S4: drop the dev-only CLI path override",
            replace=[("packages/app/src/main/config/configManager.ts",
                      _CONFIG_OLD, _CONFIG_NEW)],
        )],
        expect="block",
        expect_message=["KIMCHI_CLI_PATH", "README"],
    ),
    Scenario(
        name="S5-clean-change-passes",
        description="behaviour-preserving tidy of untrackedPaths in shared utils",
        commits=[CommitSpec(
            message="sim S5: name the untracked-entry predicate",
            replace=[("packages/shared/src/utils/gitStatus.ts",
                      _GITSTATUS_OLD, _GITSTATUS_NEW)],
        )],
        expect="pass",
        forbid_message=["blocked this commit"],
    ),
]
