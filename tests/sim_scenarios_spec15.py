"""SPEC-15 race/regression acceptance scenarios against a scratch copy of
the gate repository itself (spec ticket spec-15-race-regression-checks,
testing-decision seam 2). Data only -- executed by tests/sim_harness.py.

What is seeded, and why
-----------------------
SEED_FILES installs the world the six scenarios need, as the scratch
baseline commit (before the hook is installed):

  .ocr/config.json    the per-repo checks config wiring ONE command check,
                      "race-regression", to the reference executor below --
                      parallel (its own tip clone) with a generous timeout
                      (selector call + go build + tests + one re-run each;
                      the 60 s default is the stuck-gate budget, not a
                      model-plus-tests budget). It replaces the repo's own
                      ADR-duty config: the scratch's pushes are sim files,
                      not the repo's real docs corpus.

  scripts/test_check  the SPEC-15.5 reference executor, copied verbatim from
                      examples/test-check/test_check (SEED_EXECUTABLES chmods
                      it +x: the command-check contract requires a
                      repo-relative EXECUTABLE).

  go.mod, simgo/racy.go
                      a tiny Go module inside the gate repo's scratch so a
                      race probe can run `go test -race` against REAL changed
                      Go code inside the executor's clone. The baseline
                      version is safe (mutex); scenario A's commit makes the
                      same function racy (unsynchronized `total += v`).

  simlib/feature.py, tests/test_feature.py
                      a long-standing behavioral contract with long-standing
                      coverage: scenario B's commit flips the inclusive
                      bound `<=` to `<`, the selected existing test fails,
                      and reproduce-then-block fires.

  simconst/limits.py  plain constants, no behavior: scenario E's constants-
                      only change leaves the selector nothing to run, which
                      is the canonical coverage-gap warning (never a block).

Scenarios (spec seam 2): A race caught by a probe blocks; B a regression in
a long-standing feature blocks via a selected existing test; C the same
blocking push, every expected finding on its paths waived (the PLURAL
placeholder), passes with the waiver loudly listed -- and because the
executor's deterministic coverage rule re-selects the existing test rather
than authoring a fresh ephemeral probe, the re-push reproduces the same
waived finding instead of minting a new one; D every expected finding
waived, then the finding's code altered, the waiver lapses, it blocks
again; E a coverage-gapped push passes with warnings; F a clean push
passes.
"""
import json
from pathlib import Path

from sim_harness import CommitSpec, PushStep, Scenario

# --- seeded baseline ----------------------------------------------------------

# The reference executor, copied byte-for-byte from the repo's example so
# the acceptance always runs the shipped artifact, never a drift copy.
_EXECUTOR = (Path(__file__).resolve().parent.parent / "examples" / "test-check"
             / "test_check").read_text(encoding="utf-8")

_CHECKS_CONFIG = json.dumps({
    "checks": {
        "race-regression": {
            "command": "scripts/test_check",
            "globs": ["simgo/**", "simlib/**", "simconst/**",
                      "tests/test_feature.py"],
            "parallel": True,
            "timeout": 900,
            "mode": "block",
        },
    },
}, indent=2) + "\n"

_GO_MOD = "module gate-sim\n\ngo 1.21\n"

# The safe baseline: one mutex guards the shared total.
_RACY_GO_SAFE = '''package simgo

import "sync"

// CountConcurrently sums values using one goroutine per value. The import
// path of this package is "gate-sim/simgo" (module gate-sim, see go.mod).
func CountConcurrently(values []int) int {
\ttotal := 0
\tvar mu sync.Mutex
\tvar wg sync.WaitGroup
\tfor _, v := range values {
\t\twg.Add(1)
\t\tgo func(v int) {
\t\t\tdefer wg.Done()
\t\t\tmu.Lock()
\t\t\ttotal += v
\t\t\tmu.Unlock()
\t\t}(v)
\t}
\twg.Wait()
\treturn total
}
'''

# Scenario A's push: the mutex is gone, the goroutines race on `total`.
# No existing test covers this package, so the selector must author an
# ephemeral probe; `go test -race` reports DATA RACE, twice.
_RACY_GO_RACY = '''package simgo

import "sync"

// CountConcurrently sums values using one goroutine per value. The import
// path of this package is "gate-sim/simgo" (module gate-sim, see go.mod).
// This concurrency is not covered by any test in this repository: a probe
// should exercise it with the race detector.
func CountConcurrently(values []int) int {
\ttotal := 0
\tvar wg sync.WaitGroup
\tfor _, v := range values {
\t\twg.Add(1)
\t\tgo func(v int) {
\t\t\tdefer wg.Done()
\t\t\ttotal += v
\t\t}(v)
\t}
\twg.Wait()
\treturn total
}
'''

# The long-standing feature: an inclusive sum.
_FEATURE_SAFE = '''"""simlib.feature -- a long-standing behavioral contract (acceptance sim)."""


def add_until(start, end):
    """Sum every integer from start through end, INCLUSIVE of end."""
    total = 0
    value = start
    while value <= end:
        total += value
        value += 1
    return total
'''

# Scenario B's push: the boundary flips exclusive. The long-standing test
# (selected and run by the executor) fails on the end value, twice.
_FEATURE_FLIPPED = _FEATURE_SAFE.replace("while value <= end:",
                                         "while value < end:")

# The long-standing coverage. The sys.path bootstrap is needed because the
# executor runs pytest with cwd the tests/ directory.
_TEST_FEATURE = '''"""Long-standing boundary contract for simlib.feature (acceptance sim)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simlib.feature import add_until


def test_includes_the_end_value():
    assert add_until(1, 5) == 15
'''

# Scenario B also touches the test file (a far-field trailing note) so the
# selector's manifest carries the existing test, not just the feature.
_TEST_FEATURE_TOUCHED = _TEST_FEATURE + (
    "\n\n# acceptance sim: this suite is the long-standing coverage of "
    "simlib.feature.\n")

# D starts on an INDEPENDENT first-line anchor: a waiver from scenario C
# (which shares the machine-local store) must not suppress its first push.
_TEST_FEATURE_D_INITIAL = _TEST_FEATURE.replace(
    "Long-standing boundary contract", "Scenario D boundary contract")

# Scenario D strengthens the contract INSIDE the finding's anchor window
# (the docstring sits on line 1): the waiver lapses and the still-failing
# boundary is judged fresh again.
_TEST_FEATURE_STRENGTHENED = '''"""Long-standing boundary contract for simlib.feature, with the single-value
boundary made explicit (acceptance sim)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simlib.feature import add_until


def test_includes_the_end_value():
    assert add_until(1, 5) == 15
    assert add_until(2, 2) == 2  # the single-value boundary
'''

_CONSTANTS = '''"""simconst.limits -- plain constants, no behavior to test (acceptance sim)."""
MAX_RETRIES = 3
BACKOFF_SECONDS = 2
QUEUE_CAPACITY = 1000
'''

_CONSTANTS_V2 = '''"""simconst.limits -- plain constants, no behavior to test (acceptance sim)."""
MAX_RETRIES = 5
BACKOFF_SECONDS = 2
QUEUE_CAPACITY = 2000
'''

SEED_FILES = {
    ".ocr/config.json": _CHECKS_CONFIG,
    "scripts/test_check": _EXECUTOR,
    "go.mod": _GO_MOD,
    "simgo/racy.go": _RACY_GO_SAFE,
    "simlib/feature.py": _FEATURE_SAFE,
    "tests/test_feature.py": _TEST_FEATURE,
    "simconst/limits.py": _CONSTANTS,
}
SEED_EXECUTABLES = ["scripts/test_check"]

# --- shared payloads ----------------------------------------------------------

_UNRELATED_DOC = """# Unrelated housekeeping note (acceptance sim C)

Sim acceptance scenario C: an unrelated change so the re-push after the
waiver gets a new tip and a fresh judgment, exactly the way a pusher's
next real commit would.
"""

_CLEAN_DOC = """# Clean addition (acceptance sim F)

A documentation-only push: nothing matches the test check's globs, so the
check costs nothing, and the prose is plain.
"""

# Reads the run telemetry's recorded warnings back, the surface where a
# pass-with-warnings push is loud (the deny text carries only findings).
# {gate} arrives as argv[1]; its directory is the scripts dir under test.
_TELEMETRY_WARNINGS_CMD = [
    "{python}", "-c",
    "import os,sys;sys.path.insert(0,os.path.dirname(os.path.abspath("
    "sys.argv[1])));import ocr_telemetry as t;r=t.load(os.path.join("
    "os.getcwd(),'.git'),days=2);print('\\n'.join(str(w.get('message')) "
    "for x in r for w in (x.get('warnings') or [])) or "
    "'(no warnings recorded)')",
    "{gate}",
]

# --- the six scenarios --------------------------------------------------------

SCENARIOS = [
    Scenario(
        name="A-race-probe-blocks",
        description="a fabricated data race in changed Go code is caught by "
                    "an ephemeral probe run under go test -race and blocks",
        commits=[CommitSpec(
            message="sim A: drop the mutex from CountConcurrently",
            write={"simgo/racy.go": _RACY_GO_RACY},
        )],
        expect="block",
        expect_message=["data race", "ephemeral probe"],
    ),
    Scenario(
        name="B-regression-blocks",
        description="a fabricated behavioral regression in a long-standing "
                    "feature blocks via the selected existing test",
        commits=[CommitSpec(
            message="sim B: flip the inclusive boundary in simlib.feature",
            write={"simlib/feature.py": _FEATURE_FLIPPED,
                   "tests/test_feature.py": _TEST_FEATURE_TOUCHED},
        )],
        expect="block",
        expect_message=["reproduced failure of existing test",
                        "tests/test_feature.py"],
    ),
    Scenario(
        name="C-waived-repush-passes",
        description="the same blocking push, re-pushed after every expected "
                    "finding on its paths is waived, passes with the waiver "
                    "loudly listed",
        commits=[],    # the steps carry everything (expect = final step)
        expect="pass",
        steps=[
            PushStep(
                label="the boundary regression blocks",
                commits=[CommitSpec(
                    message="sim C: flip the inclusive boundary in "
                            "simlib.feature",
                    write={"simlib/feature.py": _FEATURE_FLIPPED,
                           "tests/test_feature.py": _TEST_FEATURE_TOUCHED},
                )],
                expect="block",
                expect_message=["reproduced failure of existing test"],
            ),
            PushStep(
                label="waive every expected finding, then re-push on a new tip",
                # The PLURAL placeholder waives EVERY accepted finding the
                # blocked verdict recorded on those paths -- the reviewer
                # correctness finding on simlib/feature.py, the existing-test
                # reproduction on tests/test_feature.py, and any ephemeral
                # probe of any shape anchored on either. Waiving only the
                # existing test (the singular) left the others blocking:
                # the acceptance failure this scenario exists to catch. It
                # stays surgical per content anchor: a finding that appears
                # differently on the next push gets a different stable id,
                # misses the waived ledger entry, and blocks -- genuinely new
                # problems are never waved through.
                commands=[["{python}", "{gate}", "waive",
                           "{findings:simlib/feature.py}",
                           "{findings:tests/test_feature.py}"]],
                commits=[CommitSpec(
                    message="sim C: unrelated housekeeping doc",
                    write={"docs/sim-unrelated.md": _UNRELATED_DOC},
                )],
                expect="pass",
                expect_message=["waived: [", "suppressed by waiver",
                                "never deleted"],
                forbid_message=["blocked this commit"],
            ),
        ],
    ),
    Scenario(
        name="D-waiver-lapse-blocks",
        description="the finding's code/evidence altered since the waiver: "
                    "the anchor no longer matches, the waiver lapses, it blocks again",
        commits=[],    # block, waive, alter the finding, re-push on a new tip
        expect="block",
        steps=[
            PushStep(
                label="the independent boundary regression blocks",
                commits=[CommitSpec(
                    message="sim D: flip the inclusive boundary in "
                            "simlib.feature",
                    write={"simlib/feature.py": _FEATURE_FLIPPED,
                           "tests/test_feature.py": _TEST_FEATURE_D_INITIAL,
                           # Scenario C's successful push advanced the fake
                           # origin WITH the flipped feature already, so a
                           # pure flip here has no diff to commit. This
                           # unrelated file makes D's setup commit non-empty.
                           "docs/sim-d-setup.md": _UNRELATED_DOC},
                )],
                expect="block",
                expect_message=["reproduced failure of existing test"],
            ),
            PushStep(
                label="waive every expected finding, then strengthen the "
                      "finding's contract test",
                # Plural waivers (see scenario C): every finding the block
                # recorded on the two paths is waived -- and none of it may
                # save the strengthened-test re-push. The strengthened
                # test changes the finding's anchor window, so the waiver
                # lapses and the still-failing boundary is judged fresh.
                commands=[["{python}", "{gate}", "waive",
                           "{findings:simlib/feature.py}",
                           "{findings:tests/test_feature.py}"]],
                commits=[CommitSpec(
                    message="sim D: strengthen the boundary contract test",
                    write={"tests/test_feature.py": _TEST_FEATURE_STRENGTHENED},
                )],
                expect="block",
                expect_message=["reproduced failure of existing test"],
                forbid_message=["suppressed by waiver"],
            ),
        ],
    ),
    Scenario(
        name="E-coverage-gap-passes-with-warning",
        description="a constants-only change leaves nothing to select or "
                    "probe: the push passes with the canonical coverage "
                    "warning, never a block",
        commits=[],    # the steps carry everything (expect = final step)
        expect="pass",
        steps=[
            PushStep(
                label="the constants-only push",
                commits=[CommitSpec(
                    message="sim E: raise the retry and queue constants",
                    write={"simconst/limits.py": _CONSTANTS_V2},
                )],
                expect="pass",
                forbid_message=["blocked this commit", "reproduced failure"],
            ),
            PushStep(
                label="the coverage warning is recorded loudly",
                commands=[_TELEMETRY_WARNINGS_CMD],
                expect="pass",
                expect_message=["no permanent test coverage"],
            ),
        ],
    ),
    Scenario(
        name="F-clean-push-passes",
        description="a clean documentation-only push: no glob match, no "
                    "check, no findings",
        commits=[CommitSpec(
            message="sim F: add a clean documentation note",
            write={"docs/sim-clean.md": _CLEAN_DOC},
        )],
        expect="pass",
        forbid_message=["blocked this commit", "no permanent test coverage"],
    ),
]
