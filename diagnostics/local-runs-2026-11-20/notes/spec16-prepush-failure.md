# SPEC-16 deployment: pre-push review failure record

## Context

- Repository: `/Users/jose_ribeir/work/code-review/claude-code-review-gate`.
- Remote: `https://github.com/Jose-Ribeir/claude-code-review-gate.git`.
- Branch/ref: `spec-15/race-regression-checks`.
- Immutable reviewed tip: `12eee1714a3b4bbab544e5a1d2f0789a5fb131db`.
- Pre-push range: `e2725667957b1651e6e666f52e69890b72c95490..12eee1714a3b4bbab544e5a1d2f0789a5fb131db`, 50 commits.
- The pre-push hook is the existing incremental commit-gate workflow, not SPEC-16's new explicit `final-review` operation.
- Earlier PR 7 for this branch has already merged. A new follow-up PR is required after pushing the new commits.

## Failures

1. Push started deep review `de6178a9fa62c971` using the default 600-second limit. It timed out without a review verdict. The branch tip was not modified and the push was not executed.
2. Retry used the hook-documented `OCR_DEEP_TIMEOUT=1200` setting, not a bypass. Review `ef4e27a2ae079db7` also timed out without a review verdict. Its supervisor log was empty on inspection, so the underlying reason for lack of completion has not been established. No claim is made that the review passed or that its report was incomplete due to a substantive source defect.

Both records identify state `failed`, reason `review`, with timeout failure details. No retries produced an external push or PR.

Stored state:

`/Users/jose_ribeir/.local/state/review-gate/async/cc513703ca1cbd06/e2725667957b1651-12eee1714a3b4bbab544e5a1d2f0789a5fb131db.json`

Empty supervisor log on inspection:

`/Users/jose_ribeir/.local/state/review-gate/async/cc513703ca1cbd06/e2725667957b1651-12eee1714a3b4bbab544e5a1d2f0789a5fb131db.supervisor.log`

## Operator-authorized continuation

The operator explicitly requested: "push with no verify and use the reviewer for the pr on the new opened one". Continuation therefore uses `git push --no-verify` for this approved push only. It does not claim a successful pre-push review and does not bypass the new SPEC-16 final-review workflow to be run against the new PR.

No bypass environment variable, branch-tip change, merge, database overwrite, provider credential modification, or other approval expansion is authorized by this record.

## Broader acceptance limitation

The deterministic acceptance matrix is 21/21. Three real-model scenarios have succeeded. Scenario `spec16-06-final-only-comment-testing-standards` remains incomplete after its 1800-second final-review timeout. It is still uncertified. Evidence and full regression status are recorded separately in `spec16-final-verification.md`.
