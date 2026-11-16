# ADR 0006 — Universal local-only push gate: default-on hooks, tip-bound verdicts, MR-consistency consult, config surface, ledger hygiene

## Status

Accepted (2026-11-16). Follows the house rule ADR 0002/0003/0004/0005
established: environment switches and operator-visible behaviour carry
decisions. Grown from a near-miss incident post-mortem (a "docs-only" MR
that carried 13,913 lines of smuggled implementation through its review
window) and a grilling session; the synthesis spec lives in the project
plans (`universal-local-only-push-gate`).

- (a) the global pre-push hook surface becomes default-on, with chaining;
- (b) verdicts bind (destination remote, ref, tip) with tree-diff
  carry-forward and delete-ref invalidation;
- (c) verdicts never transfer across destination remotes;
- (d) the review base resolves MR target → upstream → origin/HEAD;
- (e) the MR-context consult runs three cheap consistency checks always,
  cross-MR overlap above a size threshold, and degrades to "no context";
- (f) fail posture is fail-closed by default with a single audited
  bypass;
- (g) the user-level config surface (`review.depth`,
  `review.timeoutSeconds`, `review.failOpen`) with env > file > default
  precedence;
- (h) ledger hygiene: 90-day TTL, lazy daily prune, 7-day sandbox
  expiry, audited bypass records, corrupt entries kept and warned;
- (i) web-created MR/PRs produce no local event — a declared boundary.

## Context and decisions

### a) Default-on hook surface with chaining
The gate blocked nothing on the incident machine because its universal
surface (the git pre-push hook) was optional and uninstalled. The
installer now installs the global hook by default; `--no-git-hook`
preserves the legacy opt-in. A pre-existing global `core.hooksPath`
(e.g. husky) is chained via an installer-written wrapper: the gate's
hook runs first, and on pass the previous hook runs with its stdin
replayed and its exit code final. Backup semantics
(`reviewGate.prevHooksPath`) and `--chain-into <repo>` are unchanged.
Writing git *config* is installation, not the git mutation the design
rules out — the gate never mutates repository, history, or remote; its
only act is refusing the push.

### b) Tip-bound verdicts with tree-diff carry-forward
A verdict blesses exactly one tip SHA per (destination remote, ref).
Any push whose head differs re-reviews *unless* the pushed content
matches the previously reviewed state: either the tip tree is identical
(rebase, reorder, content-identical amend), or the pushed range's
patch-ids equal the reviewed range's (a rebase onto an ADVANCED
upstream, where the tree moved but no reviewed change did) — the
verdict is then carried forward without a reviewer call and announced. An amend that changes any content is a
fresh review; this is the control that would have caught the incident
(the "fix the ADR wording" amend that swept in the whole series would
have produced a non-empty range and been reviewed — and blocked —
locally before reaching the remote). Ref-deletion pushes invalidate
that ref's verdicts immediately.

### c) Destination-remote scoping
Pushes to different remotes are different trust relationships: a
verdict recorded against a fork remote can never satisfy a push of the
same tip to the canonical remote. Verdict lookup pins the destination
remote with no cross-remote fallback.

### d) Review base resolution order
`resolve_review_base` gains the MR target branch as its first source
(only when the consult returned a context), then upstream tracking,
then origin/HEAD merge-base — unchanged fallback behaviour when no MR
context exists. Stacked branches are reviewed against their own piece
scope, not against main, which both saves reviewer budget and makes
scope smuggling stand out.

### e) MR-context consult
On push to a branch with a discoverable open MR/PR (read-only `gh` /
`glab` lookups, 24h positive and negative caching, ~5s per-call
timeouts, never-raise degradation to "no MR context"), three cheap
checks always run: (1) declared scope vs computed diff — a
`documentation` label or docs-only prose contradicting non-`docs/**`
paths in the push; (2) claimed counts vs actual file/line counts;
(3) MR-recorded head vs the pushed tip. Findings block with an
`MR-CONSISTENCY:` prefix — except check (3) when the recorded head is
an ancestor of the pushed tip (the normal iterative push), which is
downgraded to a non-blocking `MR-CONSISTENCY-ADVISORY:` line; only
rewritten history blocks. Above the large-diff threshold, a fourth
check intersects changed paths with other open MRs (the "entire series
in one MR" signature). The consult depends only on CLIs and caches the
repo already patterns after `ocr_forge` and ticket-fetch.

### f) Fail posture and the audited bypass
Fail-closed is the default on all surfaces, including reviewer crash,
absent reviewer CLI, offline, and configured timeout. The audited
escape — `OCR_FAIL_OPEN=1` (env) or `review.failOpen=true` (config) —
is recorded in `bypasses.jsonl` (mechanism, remote, ref, tip,
timestamp) and announced loudly. `git push --no-verify` is NOT part of
that record by design: git never invokes the hook for it, so a local
hook can never observe it — the forge's own audit trail is the only
evidence of a `--no-verify` bypass. Hook-mode and git-mode behaviour
are aligned for the escapes the gate can see.

### g) Config surface
User-level `~/.config/review-gate/config.json` (override
`OCR_CONFIG_FILE`) with namespaced keys: `review.depth`
(`full`|`structural`|`large-diff-scoped`; default `full`),
`review.timeoutSeconds` (int ≥ 0; default 0 = uncapped),
`review.failOpen` (bool; default false). Env overrides
`OCR_REVIEW_DEPTH`, `OCR_REVIEW_TIMEOUT_SECONDS`, `OCR_FAIL_OPEN` take
precedence over the file, which takes precedence over defaults.
Invalid values warn and fall through to the next level; missing or
malformed files neither crash nor simulate configuration. Default
operator-visible behaviour is full-depth, uncapped review; the depth
and cap modes are documented user-selectable alternatives — see
`docs/config-reference.md`.

### h) Ledger hygiene
Verdicts carry `recorded_at` and expire after 90 days; pruning is lazy
(`maybe_prune`, once per day by marker) and also expires reviewer
sandboxes (`clones/`) after 7 days. Legacy entries without the
timestamp are kept and restamped rather than expired. Corrupt verdict
files are warned about and *kept* — unparseable evidence already fails
closed on lookup; deleting it would launder record-keeping failures.
Ledger write failure warns, never blocks, never simulates a verdict.

### i) The web-creation boundary
A web-created MR/PR fires no local event, and closing that gap would
require a remote dependency (webhook, polling) the design excludes by
decision. Coverage instead: (1) MR creation is always preceded by a
push, so the tip it is created against already carries a verdict;
(2) on the next push to that branch, the consult discovers the MR
(`first_seen` against a previously cached negative) and retroactively
runs consistency checks against the existing tip, announced. The
boundary is stated in user-facing docs rather than papered over.

## Consequences

- Operators who relied on the harness-only surfaces now get the git
  pre-push hook at install time; `--no-git-hook` is the documented
  opt-out and `--chain-into` is unchanged.
- The verdict record layout gains `recorded_at`; readers tolerant of
  unknown fields are unaffected (same convention as past store
  evolution).
- Large pushes to MR-backed branches now pay consult latency
  (cache-bounded), in return for three objective contradiction checks
  that do not need the model.
- Future config-surface changes require updating this ADR or a
  successor (house rule), and every environment switch named here
  joins the review fingerprint where it can alter verdict meaning.
- The incident-MR operational remediation (force-pushing the clean
  ADR-only commit) is out of scope for this ADR.
