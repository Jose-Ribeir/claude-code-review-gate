# SPEC-16 verification checkpoint

## Scope

Implementation base: `e2725667957b1651e6e666f52e69890b72c95490`.
Target branch: `spec-15/race-regression-checks`.
Approved spec: SPEC-16 in the local tracker. GitHub/GitLab providers, standalone/linear-stack reviews, dynamic required tests, versioned acceptance and observability are implemented through successive committed clusters.

No provider write/push was performed. Automated provider writes are doubles. No Kimchi application database was opened or modified.

## Store independence

Commit `51f7694` establishes explicit path > `FINAL_REVIEW_STORE` > neutral `~/.final-review/final-review.sqlite3` resolution. `rg_store.store_root()` is no longer the final-review DB authority. Portability tests exercise raw stdlib SQLite access and second-process exports with OCR/Kimchi/Claude configuration absent.

## Evidence already verified

- 21/21 deterministic scenario dispatches matched declared expected states repeatedly before targeted follow-up fixes.
- Initial real-model existing-test and ephemeral-race cases returned findings. Subsequent same-tree selector variability produced a passing probe, not a false block; the acceptance runner correctly rejected it.
- Independent Standards/Spec review plus follow-up correctness/security reviews identified actual defects; fixes cover graph discovery/fork identity, supported/unsupported CI parsing and status, current GitLab heads, provider-neutral publication of adverse finalized runs, warning-only intent/dynamic degradation, independently verified finding closures, plain ADR discovery, running environment/scoped STOP behavior, subprocess cleanup and sanitized evidence.
- Portable-store hermeticity failure found in the multi-ref CLI test is fixed with temporary `FINAL_REVIEW_STORE`/HOME.
- `tests/test_parallel_chunks.py::test_a_limit_stops_the_sibling_but_finished_chunks_keep_their_records` fails identically on isolated base `e272566` and on current work; it is a pre-existing test/race defect outside SPEC-16's scope.

## Real-model static scenarios

Earlier two-lens-timeout failure was diagnosed as 14 serial static calls (individual+combined) requiring ~36–54m, not failure to spawn inference. Real static worker responses were accepted only with missing required-lens warnings because the default worker prompt lacked explicit status semantics; summaries then copy-pasted `"..."` because the prompt example contained that placeholder.

Fixes through `65ae2d4` add bounded concurrent required lenses (4 max, explicit env knob), exact status/schema instructions, rejection evidence including sanitized status/tail content, process-group kill/reaping, stable STOP choice, stack env propagation, plain ADR document indexing, and evidence-specific summary/source reference validation. Diagnostic/source content is bounded and sanitized. No mandatory lens is dropped or replaced by a smoke/fallback model.

Release candidate static runs use `spec16-05-unchanged-domain-contract` and `spec16-06-final-only-comment-testing-standards`; outcomes must be taken from SQLite/evidence, not agent summaries. Do not accept a finding-free pass just because all model calls completed.

## Ephemeral evidence variability

Real09 release run `8ef8ae5f-f853-4c54-9992-d2b663c406f1` authored and ran a probe once, reproduce result `pass`, hence zero findings despite a planted race. Analysis confirmed the same planted race was caught by correct repeated-invariant probes in prior runs. Passing probes are not included in `findings`-derived tests, so no failed evidence was dropped. The selector prompt lacked explicit behavioral-assertion/repeated-race invariant instructions. Preserve genuine block expectation while improving deterministic race fixture, selector guidance and simulation-only diagnostic evidence; no automatic rerun-for-green or permanent probe residue.

## Pending

- Four current real-model scenarios finish; only actual verdict/evidence is acceptable.
- Full pytest after final code changes and scoped Ruff (global unrelated legacy lint errors remain).
- Full deterministic matrix after final evidence/probe changes.
- Focused final review of post-review runtime fixes.
- If a model genuinely remains unable to identify a valid fixture's defect, retain `error`/incomplete evidence and disclose it, never synthesize expected findings.
