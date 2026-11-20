# pr6 gather facts

- state: OPEN; isDraft: false; author: Jose-Ribeir; createdAt: 2026-11-16T17:54:06Z; url: https://github.com/Jose-Ribeir/claude-code-review-gate/pull/6
- source → target: `integration/push-gate` (head, oid 895abdb489eb3ecf1aca10aac7a91c6421da1d72) → `main` (base, oid 1dacf08767c6e19763e1e2bd2967949369e2cf18)
- isCrossRepository: false
- mergeable: CONFLICTING (stable across 3× 30s polls); pr_rest mergeable_state: `dirty`; rebaseable: false; draft: false
- mergeStateStatus: DIRTY
- reviewDecision: "" (empty; no reviews — reviews list is empty `[]`, no per-review states/commit_ids)
- Required checks: NONE reported — `checks_required` failed (exit=1): "no required checks reported on the 'integration/push-gate' branch". Full CI statusCheckRollup: lint SUCCESS, test (3.9) FAILURE, test (3.12) FAILURE, node SUCCESS (all in workflow `ci`, run 43923470682). None marked required.
- autoMergeRequest: null
- Merge queue: isInMergeQueue: false; isMergeQueueEnabled: false; mergeQueueEntry: null
- Review threads: 0 total / 0 unresolved (nodes `[]`)
- Children PRs: none — `gh pr list --base integration/push-gate --state open` returned `[]`
- Dependency/ordering declaration in PR body: none found. Body contains summary, spec/flow pseudocode, diff tree, evidence, "Merge Danger" (door/blast radius), and changelog/ADR references — no explicit depends-on/stacking/ordering statement. Quoting the nearest placement text verbatim: "## Merge Danger", "**Door:** two-way. All changes are behind the install and config surfaces; the verdict store gains only additive fields (`recorded_at`, restamped lazily).", "**Blast Radius:** install-time behavior. The installer now writes global `core.hooksPath` by default (backup + chaining preserved; `--no-git-hook` restores legacy). Fail-closed means an offline machine blocks pushes unless `OCR_FAIL_OPEN=1` is given one-shot (recorded, announced). No remote/CI/webhook side effects."