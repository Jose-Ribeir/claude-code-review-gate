# clean-ts — clean-push fixture repo

Small CLEAN TypeScript fixture (exported helpers + callers, type-safe usage,
no seeded bugs) used by the C4/C5 acceptance runs as a clean-push fixture.

The git history (3 commits, on branch `main`) lives in the sibling bare repo
`../clean-ts.git`. This directory is just the worktree snapshot at the tip —
it deliberately carries no `.git`.

To use it, clone the bare repo:

    git clone tests/fixtures/repos/clean-ts.git my-clean-ts
    cd my-clean-ts && git log --oneline   # 3 commits, origin already set

The push-range under review is typically `$(git rev-parse HEAD~2)..HEAD`
(the `calc` + `index` commits).
