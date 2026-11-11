# Fixture suite — the four miss categories (review-gate v2, t12)

One minimal synthetic repository per miss category from
`.kimchi/plans/review-gate-v2-spec.md` (the four bugs a human reviewer caught
that a diff-scoped gate structurally could not). Each fixture is a REAL git
repo built programmatically by the test harness in temp dirs — nothing binary
is committed here; the fixture sources are string constants in
`builders.py`, one builder per category:

| builder        | miss category                                                       |
| -------------- | ------------------------------------------------------------------- |
| `build_m1()`   | Remount/filter-policy coupling: a TS/React-ish component whose remount semantics changed (slot swap + `key` removal in the diff) paired with an untouched sibling filtering the Lexical-shaped `{tag: 'history-merge'}` event (`tags.has('history-merge')`). |
| `build_m2()`   | Convention-doc violation: `docs/agents/comments.md` (a comment-length house rule) plus a `src/` file whose diff adds an over-long comment. |
| `build_m3()`   | Stranded spec callers: a shared helper with a multi-line `def` whose optional parameter changes in the diff, called from 11 call sites across 8 spec files in `tests/`. |
| `build_m4()`   | Dangling pointer: a diff that introduces a string referencing the nonexistent report `out/weekly-2026-W48.md`. |

Every fixture test asserts BOTH sides: the pre-v2 code path (the module
from `main`, or the parametrized absence of v2 machinery) misses, and the v2
machinery (event index, convention resolver, context-filter impact search,
pointer resolver, collectors bundle) catches.

`builders.py` also holds the shared harness: git helpers, a loader that
execs a `scripts/*.py` module from the project's `main` branch (`git show`),
the gate's own review/context path filters, and a bare-origin push through
`review-gate.py` driven by `tests/stub_reviewer.py` via `OCR_REVIEWER_CMD`.
