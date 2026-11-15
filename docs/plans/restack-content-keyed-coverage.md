# Plan: content-keyed review coverage for restack-heavy workflows

Status: agreed in a grilling session on 2026-11-13 (all open decisions settled,
see Decision log); spec for review, not started. Target: this repo; the user
merges the port and the Claude variant afterwards, so keep the change
self-contained.
`RG` = `scripts/review-gate.py`, `RS` = `scripts/rg_store.py`. Line numbers are
as of `7465d89`.

Companion plan: `stacked-branch-review-base.md` (base resolution via
`resolve_review_base`, and the policy that commits reachable from
`refs/remotes/<remote>/*` count as reviewed). This plan assumes that base
helper; where it isn't landed yet, the same range resolution code path is used.

## Problem

On a piece-stack workflow (frequent rebases/restacks of branches such as
`piece/connector-grouping-core` and `piece/connector-titles` in the dogfood
repo), every restack makes the gate re-review the whole branch. Measured on
2026-11-13 in the dogfood repo:

- branch delta vs `origin/main`: **54 commits**;
- two completed runs: **1052.9 s and 1585.6 s**, both `block`, both over the
  same base with different tips;
- an aborted run elsewhere burned **4199 s over 15 of 27 chunk reviews**
  (per-chunk sessions 340–802 s), then terminated `failed` on budget — and the
  push stayed walled.

A one-line fix on top of a restacked branch pays the same 17–26 minutes as the
first push of the branch, because nothing about "already reviewed" survives a
SHA rewrite.

## Root causes

Identity is SHA-based at three independent layers, all content-blind:

1. **Verdict store.** Records live at
   `~/.local/state/review-gate/verdicts/<remote12>-<tip>.json` (RS:65). Coverage
   is a set of full commit SHAs (`RS:306` → `git rev-list base..tip`); the skip
   check is a SHA-subset test (`RS:141`, called at RG:10510). A restacked tip is
   a new filename; the old record is never consulted.
2. **Ledger.** `_record_key(path, old_path, status, base_oid)` (RG:5436) and the
   delta lookup's same-prefix glob (RG:5711-5720) key every per-file judgement
   to the base blob. A restack changes `base_oid`, so every record misses even
   for files whose blobs are byte-identical.
3. **Markers/state.** In-repo pass markers and async state are keyed by
   `(base[:16], tip)` SHAs (RG:1479, RG:3672).

Compounding mechanisms:

4. **Fail-closed stance.** "The same tip under a wider base is a NEW review,
   never a replay" (RG:10519, `tests/test_stacked_base.py`). Deliberate, and
   correct for genuinely wider ranges — but it fires identically on pure
   content-preserving rewrites.
5. **Sticky failure.** At ATTEMPT_CAP a `failed` terminal state replays its deny
   for MARKER_TTL instead of retrying (RG:4056-4061, RG:10649-10657). The
   aborted run above produced no verdict record (`verdict:"" blocked:false
   reason:budget`), yet the hook fail-closed and the branch stayed walled while
   sibling pushes consumed the findings context instead (cross-branch ledger
   reuse, RG:5702-5741).
6. **No content identity anywhere.** Repo-wide grep for `patch-id` / content
   hashing of commits: zero matches.

Consequence pair, both structural: **quadratic review cost** under frequent
restacking, and **stale finding aliases** (same code re-derived under new SHAs,
partially folded by `_findings_similar` RG:8689, unresolved priors kept
blocking as "no longer at the tip" RG:4569-4571).

## Decision log (settled 2026-11-13)

| # | Decision | Settled answer |
|---|---|---|
| Q1 | Fix target | This repo only; user merges the two variants later |
| Q2 | Identity model | Tree-hash fast path + patch-id coverage + SHA fallback |
| Q3 | Posture | Reuse *coverage* on patch-set match; findings still go through resolver re-judging; no patch-id ⇒ no reuse |
| Q7 | Granularity | **Incremental**: review only uncovered patches, combine with prior verdict |
| Q8 | Ledger | De-key from `base_oid` in the same change |
| Q9 | Records | Extend with patch-ids + tree hash; bump `protocol_version`; legacy records are SHA-join only |
| Q4/Q10 | Failures | Failed runs retry on re-push; TTL only rate-limits respawns; cap N=3 per tip; then explicit `exhausted` state, reset via documented command |
| Q12 | Combining | Worst-of verdict; union of findings; resolver is the only clearing path |
| Q13 | Branch | Content-keyed identity; branch name stored as metadata for audit |
| Q14 | No-patch-id commits | SHA-keyed individual coverage; fail-closed only on machinery errors |
| Q11 | Verification | Unit tests + simulated-restack integration tests + a real dogfood day |

Out of scope: shipping workflows/overrides (`OCR_ADVISORY` etc. stay as they
are), the claude-code-review-gate variant.

## Design

### Coverage set: one computation, three keys

`compute_coverage_set(root, base, tip) -> CoverageSet(tip_tree, commits, patch_ids, sha_only)`

For each commit in the resolved range `base..tip` (the same range the review
would walk):

- `tip_tree` = `git rev-parse tip^{tree}` — the fast-path key.
- `patch_ids` = `git patch-id --stable` of each commit's diff, for commits that
  have one. One `git diff | git patch-id --stable` pipe per commit is fine, but
  prefer a single `git log -p --no-merges base..tip | git patch-id --stable`
  style pass per range if the pairing with commit SHAs stays unambiguous —
  decide at implementation, the observable contract is a map `sha → patch-id`.
- `sha_only` = commits with **no usable patch-id**: merges, binary-only diffs,
  empty commits. Covered by SHA individually (Q14).

Empty range or any git error **fails closed**: the machinery-error path keeps
today's full-range behaviour. Partial patch-id computation (some commits fail)
puts those commits in `sha_only`, never aborts the review.

### Records: extend, bump, never backfill (Q9)

`pass` records (RS:86-103) gain:

```
protocol_version: <bumped>
tip_tree: <40-hex>
patch_ids: […]        # sorted, deduped
sha_only:   […]       # SHAs covered without a patch-id
branch:     <name>    # metadata for audit, never a join key (Q13)
```

Legacy records (no `patch_ids`) join **only** by the current SHA-subset rule.
No backfill: recomputing patch-ids for old SHAs is migration code with a
wrong-backfill risk, and costs at most one re-review per branch. TTL GC is
unchanged.

### Join logic

On each push, after range resolution:

1. **Fast path.** If a recorded `pass` verdict has the same
   (`remote`, `tip_tree`) **and** its covered range's exclusion conditions
   match (same remote-tracking state: the range's `base` is an ancestor, per
   the existing `covers` base rule RS:165-173), the diff is byte-identical to a
   reviewed one → join instantly.
2. **General path.** Otherwise compute the push's `CoverageSet` and subtract
   the union of `patch_ids ∪ sha_only` over that tip's joinable records that
   pass the existing ancestor/base validation. What remains is the
   **incremental range**: only genuinely-unseen commits.
3. **Uncovered SHA-only commits** (restacked binary/merge/empty commits) enter
   the incremental range individually. Their presence never widens the range
   beyond themselves (Q14).
4. If the incremental range is empty → join, combining verdicts as below.
   Uncovered recorded ranges are still deleted and superseded as today
   (RG:10514-10520), but only *after* the new record exists.

Coverage reuse never carries **findings trust**: every prior finding still goes
through classification, re-anchoring, and the resolver against the new tip
(Q3). What is reused is the decision "these patches were reviewed", not "these
findings are fine."

### Incremental review and verdict combination (Q7, Q12)

- The reviewer receives the diff of the incremental range only. Its prompt gets
  the fact that this is a partial re-review after a restack, so it uses Grep
  rather than expecting full context.
- Combined verdict = **worst-of** (prior, incremental). Combined findings =
  **union** deduped by `_finding_id`, then the existing carried-finding passes
  (re-anchor RG:6743-6780, siblings, resolver) run over the union.
- A quiet incremental review **clears nothing**: only a resolver verdict with
  added-side evidence clears a prior. An `unverified` prior stays blocking, as
  today.
- `_run_resolver` failure semantics from `stacked-branch-review-base.md` PR2
  (distinct `failed(reason)` status) is assumed; if that PR isn't landed, this
  plan does not regress it either way.

### Ledger de-keying (Q8)

- Record identity drops `base_oid`: `(path, old_path, status, head blob oid,
  criteria fingerprint)`. `base_oid` stays in the record as metadata.
- Exact hit: `head_oid` unchanged => reuse, regardless of base (today at
  RG:5656 the same check exists; the change is that the *key* no longer
  requires the same base).
- Delta records: `_find_delta_record` (RG:5702-5741) no longer globs by
  base-keyed prefix; it selects the newest same-path record whose `source_tip`
  is an ancestor of the current tip — the ancestry rule that already exists —
  plus same criteria fingerprint.
- Identical-blob carry stays unscoped (content-addressed), as in the companion
  plan's invariants.

### Retry / exhausted state machine (Q4, Q10)

- `failed(reason)` with reason ∈ {budget, timeout, crash, unparseable-output,
  spawn-failure} is **retriable**: the next push for the same tip spawns a
  fresh attempt, as long as attempts for this tip < 3.
- MARKER_TTL no longer replays a deny; it only rate-limits respawns (a re-push
  within TTL of the *last attempt start* joins/awaits that attempt instead of
  forking another).
- Attempt #>3 for the same `tip` writes state `exhausted`: the hook denies with
  a distinct message naming `review-gate.py --reset-tip <tip>` (new flag; also
  documented to be equivalent to removing the state file). `exhausted` is
  cleared only by that command or a new tip.
- Blocking still requires a real `block` verdict or `exhausted`; the "could not
  complete — blocking to preserve gate integrity" deny (RG:10645-10657) is
  reserved for in-flight joins inside the respawn window and machinery errors.

### Where computation happens

The hook computes the tree hash (one rev-parse) and hands it to the supervisor
via the state record. Patch-id sets are computed by the supervisor (needs the
full range; seconds-scale for 50+ commits) and cached in the state record so a
re-push joins without recomputation.

## Not adopted

- **Branch-scoped verdicts.** Re-introduces the core/titles mismatch; coverage
  is content-keyed by design (Q13).
- **Backfilling patch-ids into legacy records.** Migration risk exceeds the
  one-review-per-branch saving (Q9).
- **All-or-nothing join.** Keeps quadratic cost on every push that contains new
  work (Q7).
- **Periodic canary full re-reviews.** Complexity without a demonstrated miss
  rate; if the dogfood day shows interaction escapes, revisit.
- **Full-range fallback for any no-patch-id commit.** One binary asset in
  branch history would permanently re-trigger full reviews (Q14).
- **Changing the hooks/UX surface** (deny message format, `OCR_ADVISORY` /
  `OCR_FAIL_OPEN`). Untouched.

## PR sequence

Each PR ships on its own with a CHANGELOG entry. PR1, PR3 and PR4 are
independent; PR2 depends on PR1.

### PR1: coverage set, extended records, join — no range reduction yet

**Scope:**
- `compute_coverage_set` + unit-level git helpers (RG, new module section).
- Record extension + `protocol_version` bump (RS:86-103, RS:65).
- Join fast path + general path with **all-or-nothing** combination as the
  intermediate step (full re-review when anything is uncovered).
- Legacy-record SHA-only join rule.

**New real-git tests**, style of `tests/test_stacked_base.py` /
`tests/test_async_gate.py`:
- `test_pure_restack_same_content_joins_recorded_pass`
- `test_restack_plus_new_commit_still_reviews` (full range, interim behaviour)
- `test_legacy_record_joins_by_sha_only`
- `test_record_with_patch_ids_round_trips`
- `test_binary_only_commit_falls_back_to_sha_coverage`
- `test_tree_hash_fast_path_skips_patch_id_computation`

**Rollback:** `OCR_COVERAGE_V1=1` restores SHA-only behaviour.

### PR2: incremental range + verdict combination

**Scope:**
- Range reduction to uncovered patches (join general path).
- Review prompt amendment for partial re-reviews.
- Worst-of/union combination and the "resolver-only clears" rule.
- Uncovered recorded ranges superseded after the new record lands.

**Tests:**
- `test_restack_plus_one_new_commit_reviews_only_that_commit` — assert zero
  chunk calls for covered content (telemetry counts).
- `test_quiet_incremental_run_never_clears_prior_findings`
- `test_unverified_prior_stays_blocking_after_join`
- `test_no_patchid_commit_reviews_itself_only_not_the_range`

### PR3: ledger de-keying

**Scope:** record key (RG:5436), exact hit (RG:5656), `_find_delta_record`
(RG:5702-5741), `source_tip` ancestry selection, metadata demotion of
`base_oid`.

**Tests:**
- `test_ledger_record_survives_restack_with_same_blobs`
- `test_delta_record_selected_by_source_tip_ancestry_not_base_prefix`
- `test_different_blobs_still_miss_after_restack`

### PR4: retry / exhausted state machine

**Scope:** RG:4056-4061 (ATTEMPT_CAP/TTL replay), RG:10649-10657 (deny text),
attempt counter per tip in the state record, `exhausted` state, and the
`--reset-tip` flag itself (lands here; nothing in PR1 depends on it).

**Tests:**
- `test_failed_run_retries_on_next_push`
- `test_ttl_rate_limits_respawn_but_does_not_replay_deny`
- `test_third_failure_enters_exhausted_and_blocks_loudly`
- `test_reset_tip_clears_exhausted`
- `test_real_block_verdict_still_blocks_without_retry`

### Dogfood day (after PR1+PR2, before tag)

Run the dogfood repo's piece branches through at least: one pure restack, one
restack + new commit, one restack + binary change, one induced budget failure +
retry. Success criteria: pure restack joins instantly; restack + one commit
reviews one commit's diff; no branch sits behind a sticky wall; recycled
findings dedupe to one instance per content.

## Open decisions (with recommendations)

| Decision | Recommendation |
|---|---|
| patch-id pass shape (per-commit pipe vs one `git log -p` pass) | Implementation detail; contract is a `sha → patch-id` map. |
| Criteria fingerprint scope for ledger re-key | Keep current fp; unchanged by this plan. |
| Legacy ledger records keyed to old base | Leave until TTL expiry; do not migrate. |
| Interaction-escape detection beyond resolver+falsify | Defer; measure during dogfood day. |

## Invariants to preserve

- Fail closed: an unevaluable range or a coverage-computation error means
  full-range review, never a skip.
- Commits reachable from `refs/remotes/<remote>/*` count as already reviewed
  (unchanged policy from the companion plan).
- `resolved` needs added-side evidence; `still_present` needs evidence at tip;
  the resolver is the only clearing path.
- Coverage reuse never carries findings trust.
- Patch-id only ever **narrows** the range to genuinely-unseen content; a
  commit without a patch-id is SHA-covered, never silently skipped.
- Legacy records are never patch-id-joinable.
- `exhausted` blocks loudly with a user action; failures never silently replay.
- Multi-ref pushes are refused; tag refs are filtered before counting.
- Finding text is untrusted (capped into prompts).
