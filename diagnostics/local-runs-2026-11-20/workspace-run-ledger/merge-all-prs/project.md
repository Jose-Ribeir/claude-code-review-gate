# Project / Branch Settings: Jose-Ribeir/claude-code-review-gate

- Default branch: `main`
- Allowed merge methods: merge=true, squash=true, rebase=true (all three enabled)
- delete_branch_on_merge: false
- Classic branch protection on main: none (HTTP 404, "Branch not protected")
- Effective rulesets for main (rules/branches/main): none — empty `[]` array
- Repo rulesets (rulesets): none — empty `[]` array, so no enforcement states or bypass actors
- Merge queue on main: none — GraphQL mergeQueue returned null
- Native stacks: endpoint available (exit 0), returns empty `[]` — no stacks configured (not a 404)
- Open PRs with "part" in title: none (`[]`)
- Open PRs (number: head → base, draft, createdAt):
  - #9 `spec-15/race-regression-checks` → `main`, not draft, 2026-11-18T17:23:07Z (SPEC-16: explicit final whole-stack review)
  - #8 `integrate/spec-14-push-findings` → `main`, DRAFT, 2026-11-17T20:20:51Z (SPEC-14: custom checks + documentation quality gates)
  - #6 `integration/push-gate` → `main`, not draft, 2026-11-16T17:54:06Z (feat(gate): universal local-only push gate)