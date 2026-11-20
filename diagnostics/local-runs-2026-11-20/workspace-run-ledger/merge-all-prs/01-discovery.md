# Discovery ledger — "merge all open PRs into main" reconnaissance

Read-only recon. Performed 2026-11-18. No repo was cloned, no file/git state modified except this ledger file. No remote-mutating command was run.

## Search scope
- Root: `/Users/jose_ribeir/work/code-review` (find .git dirs to maxdepth 4)
- Sibling project-like dirs under `/Users/jose_ribeir/work` (find .git to maxdepth 3): `code-review-spec16-investigation`, `delegate-kit`, `director-mode`, `kimchi`, `kimchi-studio`, `memory system`. (`director-mode` had no `.git`.)

## Repos found (fully enumerated)

Under /Users/jose_ribeir/work/code-review (recursive finds):
- sim-scratch-spec15/work
- sim-scratch-spec15-final/work
- sim-scratch-spec15-final-proof/work
- sim-scratch-spec15-CD-proof/work
- sim-scratch-spec15-DEF/work
- sim-scratch-spec15-CDF/work
- sim-scratch-spec15-A-E/work
- sim-scratch-spec15-proof/work
- claude-code-review-gate
- worktrees/spec-14-recovery
- worktrees/pushgate-integration

Sibling dirs:
- code-review-spec16-investigation
- delegate-kit
- kimchi
- kimchi-studio
- memory system/Thyra-memory

## Auth status
- `gh`: authenticated to github.com as **Jose-Ribeir** (keyring). Scopes: gist, read:org, repo, workflow. Protocol https. Active account true.
- `glab`: authenticated to gitlab.com as **jose.ribeiro4** (keyring). REST/GraphQL endpoints configured. https protocol.

## Per-repo details (remote, branch, status, log)

### GitHub-remote repos

#### claude-code-review-gate (`/Users/jose_ribeir/work/code-review/claude-code-review-gate`)
- origin: `https://github.com/Jose-Ribeir/claude-code-review-gate.git`
- branch: `spec-15/race-regression-checks`
- status: dirty — untracked `.delegate-kit/`, `.kimchi/`
- log: 12eee17 Remove trailing space in stop fixture; f5ebb45 [W3] Redact structured authorization evidence; 5463af9 [W2] Make dynamic probe evidence deterministic

Note: two git worktrees also point at this same GitHub repo see below.

#### worktrees/spec-14-recovery (`/Users/jose_ribeir/work/code-review/worktrees/spec-14-recovery`)
- origin: `https://github.com/Jose-Ribeir/claude-code-review-gate.git` (same repo as above)
- branch: `integrate/spec-14-push-findings`
- status: clean
- log: 1cd1149 fix: fourth review round — clamp at zero bars…; 10befff fix: second push-review round…; de775b3 fix: five push-review findings…

#### worktrees/pushgate-integration (`/Users/jose_ribeir/work/code-review/worktrees/pushgate-integration`)
- origin: `https://github.com/Jose-Ribeir/claude-code-review-gate.git` (same repo)
- branch: `integration/push-gate`
- status: dirty — untracked `.ocr/`
- log: 895abdb test(pg8): create the fixture dir before git init…; 8a793ac fix(pg8): split numstat records…; e0e6d1a fix(pg8): parse numstat renames via -z…

#### delegate-kit (`/Users/jose_ribeir/work/delegate-kit`)
- origin: `https://github.com/Thyra-AI/delegate-kit.git`
- branch: `kimchi`
- status: clean
- log: f07402e kimchi: round 4 — selftests isolated to a temp kit root…; 28937f2 kimchi: round 3…; ed9106e kimchi: round 2…

#### memory system/Thyra-memory (`/Users/jose_ribeir/work/memory system/Thyra-memory`)
- origin: `https://github.com/Jose-Ribeir/Reformo-memory.git`
- branch: `feat/reformo-kimchi`
- status: clean
- log: d651412 test: use temporary absolute paths for URI verification; a51ed6c fix: close migration connections…; 8b6af01 ci: install pytest for the API test matrix

#### getkimchi/kimchi (checkout: `/Users/jose_ribeir/work/kimchi`)
- origin: `git@gitlab.com:castai/kimchi/kimchi.git` (GitLab); github remote: `https://github.com/getkimchi/kimchi.git`
- branch: `master`
- status: dirty — untracked `global/`, `package-manager-store/`
- log: 907571f45 fix(agents): skip malformed agent files (#1783); a658eb699 feat(acp): reasoning effort config…; 8ecad957f feat(redaction): consolidate registry…

#### kimchi-studio (`/Users/jose_ribeir/work/kimchi-studio`)
- origin: `git@gitlab.com:castai/kimchi/kimchi-studio.git`
- branch: `main`
- status: dirty — untracked `.diag2.cjs`, `e2e/specs/_qa-agent/*` (4 files), `packages/app/src/main/scheduledTaskChains.prototype.html`
- log: 4cfa32d5a Remote sessions: lifecycle logging…; f5dbca3c0 feat(ui): session screens…; 6e2563ac9 E2E: streamed mermaid diagram…

### GitLab-remote repo with no local provider remote listed here
#### code-review-spec16-investigation (`/Users/jose_ribeir/work/code-review-spec16-investigation`)
- origin: `/Users/jose_ribeir/work/code-review/claude-code-review-gate` (LOCAL filesystem path → claude-code-review-gate checkout dir; NOT a hosted remote; aliases the gate repo)
- branch: `spec-15/race-regression-checks`
- status: dirty — untracked `.delegate-kit/`
- log: same as claude-code-review-gate (12eee17, f5ebb45, 5463af9)

### local-only bare-remote repos (sim harness fixtures — non-hosted `origin.git` paths)
All share identical shape:
- origin = local path `…/<sim-dir>/origin.git` (fetch+push); branch `spec-15/race-regression-checks`; status **clean**; log varies per scenario. No hosting provider, so no PR/MR listing applies. Repos:
- sim-scratch-spec15/work → origin.git in sim-scratch-spec15; log: 4b06fb1/4ca8f79/261de0f
- sim-scratch-spec15-final/work → sim-scratch-spec15-final; log: e972137/66c0e8f/cb9c0df
- sim-scratch-spec15-final-proof/work → sim-scratch-spec15-final-proof; log: 2cb9c51/8af3231/f1fa038
- sim-scratch-spec15-CD-proof/work → sim-scratch-spec15-CD-proof; log: 0793ee2/699ce66/0d5cd55
- sim-scratch-spec15-DEF/work → sim-scratch-spec15-DEF; log: 8966eca/3321840/6f75cc9
- sim-scratch-spec15-CDF/work → sim-scratch-spec15-CDF; log: a1c709e/f43c45d/25993d2
- sim-scratch-spec15-A-E/work → sim-scratch-spec15-A-E; log: 1ddbe20/1f37803/261de0f
- sim-scratch-spec15-proof/work → sim-scratch-spec15-proof; log: f743ac1/8c6c6ee/1502e8e

### director-mode
- No `.git` found; not a repo.

## Open PRs / MRs

### Jose-Ribeir/claude-code-review-gate (GitHub) — 3 open PRs
Raw `gh pr list` JSON:
```
[{"author":{"id":"MDQ6VXNlcjkxMjQ4MDU4","login":"Jose-Ribeir"},"baseRefName":"main","headRefName":"spec-15/race-regression-checks","isDraft":false,"mergeable":"MERGEABLE","number":9,"title":"SPEC-16: explicit final whole-stack review","updatedAt":"2026-11-18T17:23:09Z"},
 {"author":{"login":"Jose-Ribeir"},"baseRefName":"main","headRefName":"integrate/spec-14-push-findings","isDraft":true,"mergeable":"CONFLICTING","number":8,"title":"SPEC-14: custom checks + documentation quality gates (deslop, wait-what, adr-integrity)","updatedAt":"2026-11-17T20:21:04Z"},
 {"author":{"login":"Jose-Ribeir"},"baseRefName":"main","headRefName":"integration/push-gate","isDraft":false,"mergeable":"CONFLICTING","number":6,"title":"feat(gate): universal local-only push gate — default-on install, tip-bound verdicts, MR-consistency consult","updatedAt":"2026-11-16T17:55:40Z"}]
```
- #9 SPEC-16… spec-15/race-regression-checks → main, not draft, MERGEABLE
- #8 SPEC-14… integrate/spec-14-push-findings → main, DRAFT, CONFLICTING
- #6 feat(gate)… integration/push-gate → main, not draft, CONFLICTING

### Thyra-AI/delegate-kit (GitHub) — 0 open PRs (`gh pr list` returned `[]`)

### Jose-Ribeir/Reformo-memory (GitHub) — 0 open PRs (`gh pr list` returned `[]`)

### getkimchi/kimchi (GitHub mirror of GitLab kimchi) — 27 open PRs (count via gh)
Raw `gh pr list --json number,isDraft,headRefName,baseRefName`:
```
1797 draft feat/document-cli-telemetry -> master
1795 open fix/session-title-skill-expansion -> master
1794 draft feat/global-claude-md-context -> master
1793 draft feature/LLM-4596-kimchi-harness-configure-codex -> master
1792 open feat/agent-skipped-files-collapsible -> master
1786 open ao/tmp-skills/root -> master
1782 open nojira-agent-rules-discovery -> master
1776 open ui/truncate-width-memo -> master
1775 open fix/update-platform-probes -> master
1772 open fix/update-readonly-install-target -> master
1770 draft perf/gate-ledger-scenario -> fix/pr-latency-stack
1769 draft fix/pr-latency-stack -> master
1760 open fix/acp-provider-model-labels -> master
1752 draft refactor/pr-cost-bridge-package -> master
1735 open feat/workflow-batch-cli -> master
1725 draft fix/mcp-session-scoped-surface -> master
1718 open fix/pii-resume-attest-seed -> master
1714 draft fix/redaction-local-path -> master
1709 open benchmark-memory-eval -> master
1700 draft perf/todos-two-tool-surface -> master
1681 open fix/agent-type-fails-closed-1678 -> master
1664 open feat/tui-collapse-work -> master
1654 draft feat/encourage-codemode -> master
1653 draft feat-btw-acp -> master
1594 open ao/you-are-an-4/mcp-config-reload -> master
1550 draft feat-pi-upgrade-trigger -> master
1534 draft feat/pr-cost-legacy-cleanup -> master
```
Authors seen: josemonteiro, martinserts, Zilv1nas, Mahamadou828, cast-vytautas, mncast, zilvinasu, tautvydasLiekis, mikenorgate, mszostok, vmizg, michalsallem-prcstp, MateuszOtmianowski. Base-master PRs: 25; base `fix/pr-latency-stack`: #1770. Mergeable mostly UNKNOWN (CI pending); MERGEABLE on #1786, #1709, #1681, #1664.

### castai/kimchi/kimchi (GitLab) — 7 open MRs
```
!172 Draft: LLM-4415: support KT provider refs in TB2.1   (benchmarks) ← (llm-4415-kt-harness)
!164 Draft: feat(benchmark): Terminal-Bench 4.0 CI support (62-task v1) (benchmarks) ← (tb4-pipeline)
!160 fix(benchmark): pin @deepseek-ai/dsh to 0.1.7-alpha.1 (benchmarks) ← (fix/dsh-version-pin)
!150 Draft: fix(benchmark): classify unrecognized agent_setup failures… (benchmarks) ← (fix-moonshot-quota-exceeded-classification)
!113 Limit benchmark concurrency (benchmarks) ← (limit-benchmark-concurrency)
!108 Draft: feat(benchmark): replace max_tokens CI input… (benchmarks) ← (feat/max-output-tokens)
!78  Update guidelines to a potentially better learned prompt (master) ← (update-guidelines-to-better-learned-prompt)
```
All target base `benchmarks` except !78 which targets `master`.

### castai/kimchi/kimchi-studio (GitLab) — 30 open MRs (page 1 of default listing)
```
!1077 Draft fix(LLM-4323): filter noisy session-search terms (main) ← fix/LLM-4323/noise-filter
!1076 Draft feat(prompt-rail): cap navigation at 30 rolling markers (main) ← feat/prompt-rail/rolling-markers
!1070 Draft feat(LLM-3131): add the CLI session import dialog (feat/LLM-3131/import-harness-sessions) ← feat/LLM-3131/import-ui
!1068 Draft feat(LLM-3131): import and sync harness sessions (main) ← feat/LLM-3131/import-harness-sessions
!1060 fix(acp): clear restored background task indicators (main) ← fix/restored-background-task-settles
!1053 Draft feat(settings): offer Auto-memory to the Cast AI org… (main) ← ao/memory-override/castai-memory-row
!1042 LLM-4595: Recover from harness startup failures (main) ← fix/harness-auth-recovery-LLM-4595
!1037 refactor(LLM-3841) (part 2/2): shared Default mode lookup (llm-3841-create-skill-spec) ← llm-3841-default-surface-helper
!1034 fix(updates): report a failed install… (main) ← fix/LLM-4550/update-failure-correlation
!1027 feat(LLM-3841) (part 1/2): Create skill action in Skills settings (main) ← llm-3841-create-skill-spec
!1023 fix(session-search): honest phrase matching… (main) ← fix/LLM-4323/session-search-relevance
!979  Draft feat(LLM-4325/chat): project rail for the chat sidebar (main) ← feat/LLM-4325/chat-rail
!974  Draft feat(LLM-4325/projects): project instructions and context files (main) ← feat/LLM-4325/project-setup
!970  Draft feat(LLM-4325/sidebar): move project navigation into the session sidebar (main) ← feat/LLM-4325/sidebar-grouping
!930  Connector human-readable approvals and internal-id scrub (4/4) (piece/connector-grouping-ui) ← piece/connector-approvals
!929  Connector tool-call grouping blocks (3/4) (piece/connector-grouping-core) ← piece/connector-grouping-ui
!928  Tool-family registry and display-field join core (2/4) (piece/connector-titles) ← piece/connector-grouping-core
!927  Connector branded tool titles, logos, and wire-format resolution (1/4) (main) ← piece/connector-titles
!902  Draft LLM-4355: Image preview overlaps dialog elements… (main) ← fix/llm-4355-bot
!883  Draft feat(side-chat): WorkPanel tab chrome, history panel, /btw fixes (main) ← feat-btw-chats
!879  I7: GA graduation — open the org-connectors section beyond @cast.ai (main) ← ao/implement-i7-gitlab/i7-ga-graduation
!852  Remove the Model setting from Code mode… (main) ← remove-code-mode-model-setting
!851  feat(timeline): intelligent-UI component markers… (spike) (main) ← spike/intelligent-ui
!850  Draft fix(connectors): relocate the npm runtime mirror… (main) ← fix/npm-runtime-sandbox-visibility
!837  Draft feat(scm): gh/glab auth foundation… (main) ← ao/implement-chunks-1-scm-auth-foundation
!836  Draft feat(mcp): hot-apply harness MCP config… (main) ← ao/you-are-an-2/mcp-hot-apply
!832  LLM-4236: derive session project labels from cwd… (main) ← fix/llm-4236-wrong-project-label
!830  Draft feat(code): consolidate subheader UX… (main) ← feat/subheaders-cleanup
!825  Draft refactor(files): share the DirEntry sort… (main) ← nojira/share-dir-entry-sort
!599  Draft LLM-3809: Improve "worked for" UI and spacing (main) ← conversation-spacing
```

## Prose summary

- Two CLI providers are authenticated: `gh` (GitHub, Jose-Ribeir) and `glab` (GitLab, jose.ribeiro4). PR/MR listing was possible for all hosted repos; no provider was missing for any hosted remote.
- 4 distinct **GitHub** repos have open PRs: `Jose-Ribeir/claude-code-review-gate` (3), `getkimchi/kimchi` (27). `Thyra-AI/delegate-kit` and `Jose-Ribeir/Reformo-memory` have 0.
- 2 distinct **GitLab** repos have open MRs: `castai/kimchi/kimchi` (7), `castai/kimchi/kimchi-studio` (30).
- 8 `sim-scratch-*` repos are local sim fixtures with non-hosted `origin.git` remotes — no PRs, no provider; not merge candidates.
- `code-review-spec16-investigation` is a checkout whose remote is a local path into `claude-code-review-gate`; its distinct hosted stream is covered by the gate repo listing.
- `director-mode` is not a git repo.
- NOTE for the eventual "merge all into main" run: the gate repo's 3 PRs target `main`; kimchi's 27 PRs mostly target `master` (1 targets `fix/pr-latency-stack`, #1770); kimchi's GitLab MRs target base `benchmarks` (7) and only !78 targets `master`; kimchi-studio MRs predominantly target `main` but several target other branches (!1070, !1037, !930/!929/!928). "main" is repo-specific. Two gate PRs (#8, #6) are CONFLICTING; multiple kimchi PRs marked UNKNOWN mergeable (CI pending). This context should inform any later merge decision rather than choosing a repo here.

## Stopping-condition check
More than one repo has open PRs/MRs (gate + kimchi + kimchi-studio + >1 provider) — ALL reported; no single repo selected.