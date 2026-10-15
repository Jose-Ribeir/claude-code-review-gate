# Plan: correct review base and non-stale findings for stacked branches

Status: proposed, not started. Target: 0.13.0 (PR1), then 0.13.x.
`RG` = `scripts/review-gate.py`. Line numbers are as of `90796c3`.

## Problem

On a chain of stacked branches (PR9a → PR9b → PR9c, none merged, all cut from an
older main), pushing PR9b reported:

1. **Phantom deletions.** "Voice dictation deleted from chat-composer.tsx", a
   file PR9b never touched. The feature had landed on main after the fork.
2. **Stale findings that recur.** Two warnings on `approval_resumption_service.py`
   that were already fixed earlier in the chain kept coming back, push after push.

## Root causes

### Symptom 1: the base is wrong (certain)

- In git mode, `_range_for_refs` (RG:617-662) handles a **new** remote ref (zero
  remote sha), or a remote sha that isn't present locally, by using the literal
  `origin/HEAD` (else `origin/main`/`origin/master`) as the base. There's no
  merge-base.
- Every consumer then diffs `git diff base..tip`, which is a **tree** diff:
  `_collect_diff_entries` RG:3699, `_item_diff_spec` RG:5011, the evidence diff
  RG:4650, and the skill's `--range`. So everything main gained after the fork
  shows up reversed, and every unmerged parent in the stack is re-reviewed.
- Hook mode (`_hook_target` RG:1671-1690) does take a merge-base, but only
  against the first of `refs/remotes/<r>/<dst>`, `origin/HEAD`, and so on. A new
  stacked branch therefore still re-reviews its whole unmerged parent chain.
  There are no phantom deletions in hook mode.
- Side bugs on the same path:
  - A non-fast-forward remote sha is used raw as a tree base, so the diff
    contains reverts of other people's commits.
  - A symbolic base can move mid-review.
  - The state cache is keyed by tip only (RG:2613, RG:965), so a tip that is
    `done` replays its old verdict whatever base produced it.

### Symptom 2: findings persistence (a chain of four mechanisms)

1. **Shared ledger, keyed by the base blob.** Records live in
   `<common-dir>/review-gate-ledger/` and are shared by all branches. PR9b's
   push picks PR9a's pre-fix record as its delta base. This is legitimate by
   design.
2. **Fragile resolutions.** A stored resolution is valid only while the
   evidence file's blob is unchanged (RG:4074). Any later edit to that file
   invalidates it, and in a stack that happens all the time. So the prior goes
   back to the resolver.
3. **Resolver failure becomes a permanent block.** On a timeout or OOM (the
   machine was low on RAM), `_run_resolver` (RG:4543) yields `_no_evidence()`.
   That becomes unverified, gets one recheck, and is then **kept blocking**
   (RG:3190-3236, warning at 3207). Nothing is persisted, so the same thing
   happens on the next push. **This is the most likely proximate cause.**
4. **Re-emission keeps it alive.**
   - `_write_run_records` (RG:5352-5357) re-writes every unresolved prior into
     the new record.
   - Orphans are copied into **every** active file's record (RG:5430-5459).
   - The new record becomes the next delta base, so the finding replays again.
   - Siblings (`_known_defects` RG:4346) run before the resolver and before
     re-anchoring, so they can re-raise the fixed line itself.
   - Dedup gaps (`_findings_similar` RG:5510, `held` at RG:3263) let duplicates
     through.

Inherent and not a bug: a reviewer honestly re-raising a judgment call on a full
review. Everything else above is mechanism and can be fixed.

## Diagnose first (on the real repo, about 10 minutes)

Do this before PR2 to confirm which persistence mechanism actually fired:

- **Symptom 1.** In `.git/review-gate-telemetry/<date>.jsonl`, find the PR9b
  push's `range`.
  - Check whether its base equals `git rev-parse origin/HEAD`.
  - Check whether `git diff $(git merge-base origin/main PR9b) origin/main -- '*chat-composer.tsx'`
    is non-empty.
- **Resolver failure.** In that push's `resolver.judged`, look for missing ids,
  or `unverified` verdicts with a duration near the timeout. Check Event Viewer
  for the reviewer process being killed.
- **Orphan spread.** Run
  `grep -l approval_resumption_service <common-dir>/review-gate-ledger/*/*.json`.
  Many records holding the same fid means orphans were copied.
- **Fragile resolution.** In `resolutions/*.json`, compare `evidence_blob_oid`
  with `git rev-parse PR9b:<evidence path>`. A mismatch only on the evidence
  blob means mechanism 2.
- **Siblings.** If the recurring finding's line is the fixed line and it is
  flagged as a sibling, siblings re-raised it.

## Design

### Base resolution: one helper for both modes and the skill

`resolve_review_base(remote, tip, remote_sha=None) -> ReviewBase(base_sha, commits, reason)`

The base is **always a 40-hex sha** (or `_EMPTY_TREE`), and is **always an
ancestor of tip**. The range string stays `"<base>..<tip>"`, so every
`..`-splitting consumer keeps working, and with an ancestor base the two-dot
diff is exactly the net change.

1. **Fast-forward of an existing ref.** If `remote_sha` is non-zero, present
   locally (`cat-file -e`), and `merge-base --is-ancestor remote_sha tip`, then
   base = `remote_sha`. This keeps the 0.5.1 semantics unchanged.
2. **Everything else (new ref, missing sha, non-ff).** Compute
   `git rev-list tip --topo-order --reverse --boundary --not --remotes=<remote> [remote_sha if present]`.
   - A non-ff `remote_sha` becomes a **set exclusion**, never a tree base.
   - A `rev-list` error follows the 0.5.2 rule: assume the range carries
     commits, and use base = `_EMPTY_TREE`.
3. **Pick the base from the result.**
   - No commits: allow, and log the reason.
   - One boundary: that boundary is the base.
   - Several boundaries (main merged into the child, criss-cross):
     `merge-base --octopus` of the boundaries. If that fails, use the parent of
     the first commit.
   - No boundary (root commit, shallow graft): `_EMPTY_TREE`.
   - Every candidate is an ancestor of tip and already published by
     construction, so no separate clamp is needed.
4. **Hook mode** calls the same helper with `remote_sha` =
   `refs/remotes/<r>/<dst>` if it exists. Multi-ref refusal stays as it is.
5. **Remove the empty-range fallthrough.** The `range == ""` path that falls
   through to `_EMPTY_TREE` and `--unpushed` (RG:8162-8170) is removed from push
   paths.
   - `_has_unpushed_commits` becomes a thin wrapper over the helper.
   - The skill's `--unpushed` calls `review-gate.py --print-range` instead of
     re-deriving `@{u}..HEAD` (SKILL.md:25-28, 218-225).
6. **State cache** is keyed by `(base[:16], tip)` in the same PR.

Precedent: pre-commit's pre-push hook uses `rev-list --not --remotes=<remote>`
for new refs. GitHub PRs diff from the merge-base.

**Policy: commits reachable from `refs/remotes/<remote>/*` are treated as
already reviewed.**

- Existing-ref pushes already rely on this, since the remote sha is the base.
  The stricter handling of new refs was an accident, not a guarantee.
- Pushes made with `--no-verify`, and colleagues' pushes, were already outside
  the guarantee.
- Known residual risk: a stale tracking ref after a remote rewind. This is
  already true today; document it.

Not adopted:

- **Parent metadata** (git-town, Graphite, av, or a `review-gate.base.<b>`
  config). When the parent is pushed, `--remotes` already covers it. When it
  isn't, using it as the base would hide unreviewed commits.
- **A reviewed-ledger** (`refs/review-gate/approved/<tip>`). This is the only
  safe way to narrow a stack that is *not yet pushed*. Defer it until someone
  needs it.

### Findings persistence

In priority order:

1. **Resolver failure is its own status.**
   - `failed(reason)` is distinct from `unverified`.
   - Retry once with half the batch.
   - Then block with a distinct "resolver unavailable" message and
     `resolver.failed` telemetry.
   - Don't consume the recheck, and don't re-write the priors into the new
     record as if they were fresh.
   - Still fail-closed.
2. **Resolution validity by quote presence.** A stored resolution stays valid
   while both of these hold at tip, checked with one `cat-file --batch`:
   - the evidence quote is still present in the evidence file;
   - the original `existing_code` is absent from the target.

   "Resolved needs added-side evidence" still governs the *first* verdict.
3. **Resolutions suppress everywhere, at read time.**
   - `_classify_priors` consults `resolutions/` for every prior, whichever
     record carried it.
   - `_write_run_records` never re-emits a prior whose fid has a valid
     resolution.
   - Do **not** mutate other records based on cross-file evidence: records are
     content-addressed and shared by other tips.
4. **Orphans go only to their own path's record**, so `_drop_self_resolved`
   covers them.
5. **Siblings run after the resolver.**
   - Skip resolved findings.
   - Exclude the re-anchored span and any line among the fix's added lines.
6. **Dedup fixes.**
   - Add `anchored_carried` to `held` (RG:3263).
   - Unanchored findings dedup on path + severity + normalised content.
7. **Ancestry scoping.**
   - Records store `source_tip`.
   - Delta priors whose `source_tip` is not an ancestor of the current tip are
     ignored. Cache one `is-ancestor` check per distinct tip.
   - Identical-blob carry stays unscoped, because it is content-addressed. This
     keeps `tests/test_segment_gate.py:237` green.
   - Records with no `source_tip` (written before PR5) stay eligible until TTL
     expiry.

### Migration

- The base changes, so base blobs change and old records miss. The cost is a
  one-time full review per file per branch. This is acceptable: those records
  were keyed on a wrong base.
- No fp bump and no schema migration; the 30-day TTL cleans up the old records.
- Only the state-cache key changes (PR1).

## PR sequence

Each PR can ship on its own, with a CHANGELOG entry per PR.

### PR1: base helper (0.13.0, behaviour change)

**Scope:**
- add `resolve_review_base`
- rewire `_range_for_refs` (617-662), `_hook_target` (1671-1690) and the
  `main()` split (8164-8167)
- `commit_count` (8207-8209)
- `_has_unpushed_commits` (665-685)
- `--print-range` and its use in SKILL.md
- the `(base, tip)` state and marker key
- stale `@{u}..HEAD` comments in `scripts/pre-push:8` and `install-git-hook.sh`

**New real-git tests**, in the style of the `tests/test_async_gate.py` repo
fixture:
- `test_new_ref_cut_from_moved_main_has_no_phantom_deletions`
- `test_stacked_child_reviews_only_its_own_commits_git_mode` / `_hook_mode`
- `test_main_merged_into_child_over_reviews_but_never_deletes`
- `test_squash_merged_parent_falls_back_to_merge_base`
- `test_non_ff_remote_sha_is_a_set_exclusion_not_a_tree_base`
- `test_shallow_clone_without_boundary_reviews_whole_tree`
- `test_missing_remote_sha_uses_remote_refs_not_origin_main`
- `test_base_is_always_a_sha`
- `test_done_marker_is_not_replayed_under_a_different_base`

**Update:**
- `test_a_brand_new_remote_ref_falls_back_to_the_default_branch` and
  `test_a_remote_sha_the_client_does_not_have_falls_back_rather_than_skipping`
  (both pin `origin/main..`)
- `test_async_gate.py:207` should pass unchanged.

**Rollback:** `OCR_BASE_V1=1` restores the old selection in both modes.
`OCR_LEGACY_RANGE=1` stays.

**CHANGELOG must state the policy line above.**

### PR2: resolver failure status

**Scope:** `_run_resolver` (4543), 3190-3236, `_write_run_records`, telemetry.

**Test:**
`test_resolver_failing_twice_blocks_with_a_distinct_message_and_does_not_pollute_records`

### PR3: quote-presence validity and read-time suppression

**Scope:** `_find_valid_resolution` (4042-4096), `_classify_priors` (4179),
5352-5357, and the `_seg_*` callers (6729).

**Test:**
`test_orphan_cross_file_resolution_survives_a_later_edit_to_the_evidence_file`

### PR4: orphans, siblings, dedup

**Scope:** 5430-5459, `_known_defects` (4346) moved after the resolver,
`_findings_similar` (5510), `held` (3263).

**Tests:**
- `test_sibling_search_does_not_re_raise_the_fixed_line`
- `test_carried_findings_do_not_double_print`

### PR5: ancestry scoping

**Scope:** record writer and reader (`source_tip`), `_find_delta_record` (3985).

**Tests:**
- `test_priors_from_a_non_ancestor_branch_are_not_replayed`
- `test_identical_blob_carry_still_works_across_branches`

## Open decisions (with recommendations)

| Decision | Recommendation |
|---|---|
| Treat commits on `refs/remotes/<remote>/*` as reviewed | **Yes.** This is consistent with existing-ref behaviour. |
| Parent metadata or config for the base | **No.** It is either redundant or unsafe. |
| Reviewed-ledger refs for stacks that aren't pushed yet | **Later**, on demand. |
| Several boundaries | Octopus merge-base, falling back to the first commit's parent (over-review). |
| Empty review set | Allow, and log the reason. |
| Resolver failed twice | **Block** with a distinct message, not a warning. Reviewer override is the escape. |
| Records written before PR5 (no `source_tip`) | Keep them eligible until TTL expiry. |

## Invariants to preserve

- Fail closed: an unevaluable range carries commits, and an unproven prior
  blocks.
- `resolved` needs added-side evidence. `still_present` needs evidence at tip.
- Reviewer override: a re-confirmed finding gets no resolution written.
- Identical-blob replay without the resolver. Deleted-file priors are dropped.
- Truncated records are never a delta base. fp covers criteria only. Ledger IO
  is best-effort.
- Multi-ref pushes are refused. Tag refs are filtered before counting.
- Finding text is untrusted (capped into prompts).
