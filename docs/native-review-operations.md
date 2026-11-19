# Native review operations

Native execution is opt-in while ticket #21 acceptance is incomplete. Set `RG_FINAL_REVIEW_TRANSPORT=native` for explicit final and whole-stack reviews, or `OCR_DEEP_TRANSPORT=native` for Kimchi deep reviews. An unset `OCR_DEEP_TRANSPORT` uses the legacy deep transport. No capability failure selects another transport silently.

## Prerequisites and configuration

The native inference host needs a `kimchi` executable whose help advertises `--mode rpc`, `--no-session`, model selection, tool rules, and non-interactive permission controls. The host checks these flags without starting inference. Repository checks still need their declared Git and toolchain executables; the review does not install them. Protocol doubles supply deterministic tests, but they are not real-session acceptance evidence.

| Variable | Default | Effect |
| --- | ---: | --- |
| `RG_FINAL_LENS_WORKERS` | 4 | Maximum concurrent inference tasks across all units in one run. |
| `RG_FINAL_EXECUTION_WORKERS` | 2 | Maximum concurrent required/dynamic execution tasks. These tasks do not consume inference slots. |
| `RG_FINAL_WORKER_TIMEOUT` | 900 seconds | Per-worker deadline for inference and repository execution. RPC workers receive `abort` before bounded process-group cleanup. |
| `RG_FINAL_REVIEW_TIMEOUT` | 1800 seconds | Whole-run deadline. Expiry stops queued work and cancels every resource owned by that run. |
| `RG_FINAL_REVIEW_EXTERNAL_WATCHDOG_TIMEOUT` | unset | External kill deadline. Keep it beyond the controlled deadline and the 30-second cleanup margin. |

Required and dynamic work starts after its unit's mandatory lenses finish. Lens dependencies and trusted STOP propagation decide eligibility; test timing never changes the verdict. Individual execution does not wait for combined evaluation. Every execution subprocess belongs to the run resource owner.

## Evidence and failures

The supervisor validates and commits an inference result before its artifact file and `completed` event become visible. Cancelled, superseded, or otherwise fenced results remain diagnostics and do not replace active evidence. Parser, protocol, process, persistence, and validation failures produce unsuccessful structured events. A late callback cannot publish a success file.

Run timelines include queued, started, timeout or cancelled, cleanup, and finalization events. Finalization waits for every submitted worker to terminate. `timeout` means a run or worker deadline expired; `cancelled` means an operator or supervisor requested cancellation. Required verification failures, unavailable tools, malformed RPC responses, and incomplete mandatory evidence all prevent a complete green result.

Legacy deep journals retain accepted artifacts on interruption. A later attempt validates journal identity, artifact-file equality, and coverage before reusing them. It executes only unfinished tasks. Missing files, conflicting content, and omitted or repeated coverage fail closed instead of triggering replacement inference.

Real-model scenario 06, the publication-disabled PR #9 rerun, session counts, and comparable timings remain ticket #21 acceptance work. Until those pass, this document does not certify native execution or authorize a default cutover.
