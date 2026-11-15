# Spec: Unified review gate (one core, Claude and Kimchi profiles)

Status: draft, local file only. No issue tracker is configured (run `/setup-matt-pocock-skills` to set one up, then publish with the `ready-for-agent` label if wanted).
Seam check with the user: user deferred to the proposed default (see Testing Decisions).

## Problem Statement

I maintain one code-review gate in two places. The `main` branch is a Claude Code plugin that reviews unpushed commits with `claude -p` before a push. The `kimchi` branch is a port of the same engine to the Kimchi Studio harness, reviewing with `kimchi -p`. The two branches have unrelated git histories, so they cannot be merged normally. Each has since grown fixes and features the other lacks: `kimchi` has fail-closed hardening, verdict fixes and the docs-gate, while `main` has the Claude-specific plumbing and some Claude features that never reached `kimchi`. Every improvement has to be written twice, the two engines drift, and reviewing a Kimchi repo versus a Claude repo means remembering which branch to install. I want one maintained gate that works with either harness.

## Solution

A single repository with one shared core engine and two thin profiles, `claude` and `kimchi`. A profile supplies everything harness-specific: how to launch the headless reviewer, which models to use, how hooks are wired and installed, where state lives, and how the harness shows a block to the agent. The core owns everything else: range resolution, chunking, the reviewer pipeline, verdict computation, ledgers, memory, telemetry and the docs-gate.

The backend is chosen automatically by default (from the harness that fired the hook, or the CLI available), can be pinned per repo in the repo's review config, and can be overridden by an environment variable. A push triggers exactly one review no matter how many harnesses are installed on the repo: one shared git pre-push hook and one verdict store, with the Claude and Kimchi hooks acting only as a UX layer that surfaces the result to the agent.

The work happens on a new integration branch cut from the `kimchi` tree. The `main` and `kimchi` branches stay untouched as references. Nothing is pushed until the owner says so.

## User Stories

1. As a developer using Claude Code, I want pushes from Claude Code to be reviewed by `claude -p`, so that I use my Claude subscription and nothing else.
2. As a developer using Kimchi Studio, I want pushes from Kimchi to be reviewed by `kimchi -p`, so that I use my Kimchi Inference subscription.
3. As a developer pushing from a plain terminal or IDE, I want the repo's configured backend to be used, so that the review is the same regardless of the client.
4. As a developer, I want the backend to be auto-detected by default, so that I never have to configure anything for the common case.
5. As a developer, I want to pin a backend in the repo's review config, so that a repo always reviews with the backend its team agreed on.
6. As a developer, I want an environment variable to override both auto-detection and the repo pin, so that I can try the other backend for one push.
7. As a developer, I want a clear message naming the backend that was chosen and why (override, repo pin or auto-detect), so that surprises are easy to diagnose.
8. As a developer with both harnesses installed on one repo, I want a single push to trigger exactly one review, so that I do not pay for or wait on two reviews.
9. As a developer, I want one shared verdict store per repo, so that a re-run push joins the verdict already computed instead of starting over.
10. As a developer, I want the agent in either harness to be told why a push was blocked, so that it can fix the findings without my help.
11. As a developer, I want the block to still work when a push comes from a harness other than the one that started the review, so that the gate cannot be bypassed by switching clients.
12. As a developer, I want the gate to fail closed when the backend CLI is missing, crashes, times out or returns unparseable output, so that a broken reviewer never lets a bad push through silently.
13. As a developer, I want a clear, actionable error when the chosen backend CLI is not installed, so that I know to install it or switch backend.
14. As a developer, I want high-severity findings to block and medium findings to block only at high confidence, so that the gate stays useful rather than noisy.
15. As a developer, I want the verdict to be computed deterministically by the core and never by the model, so that the same findings always produce the same outcome.
16. As a developer, I want the same verdict rules to apply under both profiles, so that switching backend does not change what is blocked.
17. As a developer, I want an advisory mode that warns without blocking, so that I can adopt the gate gradually.
18. As a developer, I want an explicit, logged fail-open escape, so that I can unblock myself in an emergency and the skip is recorded.
19. As a developer, I want unparseable or unsupported push forms to block rather than pass, so that unusual push commands cannot dodge the review.
20. As a developer, I want stacked branches to be reviewed against the right base, so that I only see findings for my own commits.
21. As a developer, I want long reviews to survive the harness's idle timeout through a detached supervisor, so that big pushes can be reviewed at all.
22. As a developer, I want the review to resume rather than restart after a truncated chunk or an interrupted run, so that I do not lose paid work.
23. As a developer, I want large changes to be chunked and reviewed in parallel, so that big pushes finish in reasonable time.
24. As a developer, I want the reviewer to run in an isolated copy of the pushed tip, so that it cannot modify my working tree.
25. As a developer, I want the reviewer's tool access restricted by the active profile, so that the reviewer cannot write outside its sandbox under either harness.
26. As a developer, I want a startup canary that proves the sandbox restriction is active, so that a silently missing restriction fails the review instead of weakening it.
27. As a developer, I want commit messages and optional tickets passed to the reviewer as untrusted intent context, so that the review can flag code that does not match its stated purpose.
28. As a developer, I want ticket discovery to work from branch names and commit messages, so that I do not paste ticket numbers by hand.
29. As a Kimchi user, I want Jira tickets fetched through the desktop Atlassian connector, so that no credentials are stored by the gate.
30. As a Claude user, I want GitHub and GitLab ticket fetching through their CLIs when enabled, so that intent context is available without extra setup.
31. As a developer, I want ticket lookups to be opt-in, so that no data leaves the machine unless I enabled it.
32. As a developer, I want ticket keys from trackers the gate does not support (for example Linear `LLM-####` keys) to be ignored safely, so that an unknown key never breaks or blocks a push.
33. As a developer, I want the docs-gate (docs-checker, ADR and documentation consistency) available under both profiles, so that documentation drift is caught regardless of backend.
34. As a developer, I want dismissed findings remembered across pushes, so that the same false positive is not re-raised.
35. As a developer, I want a dismiss command in both harnesses, so that I can demote a finding without editing files.
36. As a developer, I want per-repo rules and language rule files honoured under both profiles, so that review standards follow the repo, not the backend.
37. As a developer, I want a doctor command that checks hooks, Python, the backend CLI, the state directory and stale installs, so that I can self-diagnose.
38. As a developer, I want the doctor to report which profile each installed hook belongs to and flag duplicates, so that double-install mistakes are visible.
39. As a developer, I want an installer per profile that is idempotent, so that I can re-run it safely.
40. As a developer, I want an uninstaller per profile that restores the previous state, so that I can remove the gate cleanly.
41. As a developer, I want the state directory location to be supplied by the profile, so that Claude's plugin data dir and Kimchi's external store both keep working.
42. As a developer on Windows, macOS or Linux, I want both profiles to work on each, so that the gate is not tied to one operating system.
43. As a Claude Code user, I want every feature that exists on `main` today to still exist after unification, so that I lose nothing by switching to the unified gate. (Feature inventory is an open item.)
44. As a Kimchi user, I want every feature that exists on `kimchi` today to still exist after unification, so that I lose nothing by switching.
45. As a Kimchi user, I want the Claude-only features that `kimchi` lacks to become available under the kimchi profile where the harness can support them, so that the profiles converge.
46. As the maintainer, I want one place to fix a verdict, hardening or collector bug, so that a fix reaches both profiles at once.
47. As the maintainer, I want a profile to be a small, well-defined unit with a documented contract, so that adding a third harness later is straightforward.
48. As the maintainer, I want one module naming prefix across the codebase, so that the code is consistent instead of mixing two conventions.
49. As the maintainer, I want one changelog and one README that describe both profiles, so that docs do not drift.
50. As the maintainer, I want the unification work to live on a separate integration branch, so that `main` and `kimchi` remain intact references until I am satisfied.
51. As the maintainer, I want nothing pushed to GitHub without my explicit instruction, so that unfinished work never leaks.
52. As the maintainer, I want the stale branches identified, so that I can delete the ones already merged or fully subsumed.
53. As the maintainer, I want telemetry to record the profile and backend used for each review, so that I can compare cost and quality across them.
54. As the maintainer, I want calibration and benchmark tooling to work against either backend, so that I can compare models fairly.
55. As a contributor, I want CI to run the core suite plus both profiles' suites, so that a change that breaks one profile is caught before merge.
56. As a contributor, I want a simulated-backend harness, so that I can test the whole push gate without calling a real model.

## Implementation Decisions

- **Architecture:** one repo, one core, two profiles. A profile is the only place that may know about a specific harness's CLI, environment variables, hook mechanism, install location, state location and model names.
- **Starting point:** a new integration branch cut from the `kimchi` tree, because it is already the functional superset (hardening, verdict fixes, docs-gate, doctor, janitor, report, calibration, Electron rules). The two histories are not merged. `main` and `kimchi` are left untouched.
- **Claude profile:** rebuilt on top of the new core from `main`'s Claude-specific pieces: locating and launching the `claude` CLI, its permission and tool flags, its plugin data directory for state, plugin manifests, hook wiring through the plugin hooks file, the PowerShell and bash hook adapters, and the Claude slash commands. Any `main` feature absent from `kimchi` (inventory still to be confirmed) is ported into the core or the claude profile.
- **Kimchi profile:** the existing Kimchi pieces become the kimchi profile: locating and launching the `kimchi` CLI, model pins per role, the sandbox deny extension, the bash hook installed into the Kimchi harness, the Jira side-fetch worker, and the external state store.
- **Core responsibilities:** range resolution including stacked bases, chunking and parallel chunk review, the reviewer pipeline (spotter, reviewer, deep reviewer, filter, resolver, docs-checker), hallucination check, dedup, verdict computation, ledger and resume, dismissal memory, impact and collector analysis, forge and ticket abstraction, telemetry, doctor, janitor, report and calibration.
- **Profile contract (to be finalised during implementation):** a profile must provide (a) how to find and launch the headless reviewer for a given role with a prompt and a restricted tool set, (b) role-to-model mapping and thinking level, (c) environment scrubbing rules, (d) state root location, (e) how a block is surfaced to the harness's agent, (f) install and uninstall behaviour, (g) the sandbox verification canary, and (h) the persona delivery method (inline versus agent files).
- **Backend selection:** highest priority is an environment-variable override, then a per-repo setting in the repo's review config, then auto-detection from the harness that fired the hook, then from which backend CLI is available. The chosen backend and the reason are recorded in the verdict record and shown to the user.
- **Single review per push:** the git pre-push hook is the primary gate and the only thing that starts a review. The per-harness hooks are a UX layer that read the shared verdict and surface it; they do not start a second review. One verdict store per repo is shared by all profiles.
- **Verdict policy:** one policy in the core for all profiles: any high finding blocks, medium blocks at confidence 0.7 or above, an unreviewed state warns, and failures fail closed. The model never decides the outcome.
- **Fail-closed behaviour:** unchanged from today and applies to both profiles, including missing CLI, crash, timeout, unparseable output, unparseable push forms and corrupt verdicts.
- **Naming:** one module prefix across the codebase, defaulting to `ocr_` (the user deferred the choice). It matches the existing `OCR_*` environment variables and means renaming only the three `rg_*` intent and ticket modules back, instead of renaming about fifteen modules forward. The two prefixes must not coexist at the end.
- **Git hook install:** both profiles use the shared git pre-push install used by the `kimchi` branch (user decision). The global hooks-path install from `main` is retired.
- **Intent context:** commit-message intent and optional ticket fetch stay in the core with fetchers selected by profile and config. Linear is not supported (user decision); unsupported ticket keys are ignored safely.
- **Migration safety:** nothing is pushed or published. Old branches are references only. Stale branches (the already-merged stacked-base branch, and the docs-gate branch that is fully contained in `kimchi`) are candidates for deletion, decided by the owner later.
- **Working setup:** the existing local clone is used with worktrees: reference worktrees for `main` and `kimchi`, and a working tree for the integration branch.

## Testing Decisions

- **What makes a good test:** drive the gate from the outside and assert on observable behaviour (exit code and message of the push gate, the verdict record, which backend was launched, what the agent is told), never on internal helper structure.
- **Seam (one; the user deferred to this default):** the push gate entry point. A test creates a fixture repo, stages a push, runs the gate exactly as a git pre-push or harness hook would, with a fake backend executable on the path that returns scripted reviewer output, and asserts the outcome (block, warn or pass), the verdict record and the backend chosen. Parameterise the same scenarios across both profiles so one scenario set proves both. No lower seams unless a bug cannot be reproduced at this level.
- **Scenarios the seam must cover:** backend selection precedence (override, repo pin, auto-detect), both harnesses installed with one push producing one review, missing backend CLI fails closed, timeout and garbage output fail closed, high and medium-confidence blocking, advisory and fail-open escapes with logging, unsupported push forms, stacked base, resume after interruption, sandbox canary failure, dismissal memory, docs-gate findings, intent findings.
- **Modules tested through the seam:** core pipeline and verdict, both profiles' launch and install behaviour, state store, doctor.
- **Prior art:** the existing async-gate, fail-closed, verdict-coverage, state-machine and hook-passthrough tests, and the existing simulated-backend harness and fixture repos from the `kimchi` branch. Reuse and generalise these rather than adding a new harness.
- **CI:** run the shared scenarios against both profiles on Linux, and add Windows if the PowerShell adapters are kept.

## Out of Scope

- Merging the `main` and `kimchi` git histories.
- Adding a third harness (the profile contract should make it possible, but it is not built here).
- Changing the verdict policy, thresholds or the review methodology.
- Publishing a release, pushing branches, deleting remote branches, or editing the upstream marketplace listing.
- Rewriting the review pipeline's internal algorithms.
- Reviewing getkimchi/kimchi pull requests (the local review clones are targets for testing only).
- Linear ticket support (`LLM-####` keys).

## Further Notes

- **Decisions settled with the user:** git hook install is the shared pre-push (both profiles); Linear is not needed; the user has no extra list of Claude-only features beyond what the diff found (the Claude plumbing: plugin manifests, hook wiring, PowerShell adapters, slash commands, compatibility shim), so that is the inventory to preserve; the module prefix and the test seam were deferred to the defaults above.
- **Remaining open items:** none blocking. The profile contract is finalised during implementation.
- **Branch facts:** the `main` and `kimchi` histories share no merge base. `kimchi` was scaffolded from `main`'s tree as of the stacked-base merge, then both evolved in parallel and re-implemented the same v2 and intent-context features. The docs-gate branch is a strict ancestor of `kimchi`. The stacked-base branch is already merged into `main`.
- **Highest merge-conflict areas if anyone attempts a merge anyway:** the core review script, the review skill orchestrator, the async-gate tests, the README and CHANGELOG, and the three renamed intent/ticket modules.
- **Credentials:** GitHub credentials for the `Jose-Ribeir` account were stored via Git Credential Manager on this machine. No push has been performed.
