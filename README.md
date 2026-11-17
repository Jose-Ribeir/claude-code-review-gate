# kimchi-review-gate — Blocking AI code-review gate for Kimchi Studio

![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)
![Status: port in progress](https://img.shields.io/badge/status-port--in--progress-orange.svg)

> **AI code review + a blocking pre-push gate, native to Kimchi Studio.**
> Runs on your Kimchi Inference subscription — no token borrowing, no external binary.

**kimchi-review-gate** is a port of
[claude-code-review-gate](https://github.com/Jose-Ribeir/claude-code-review-gate)
(v0.13.x) to Kimchi Studio. It adds AI code review and a **blocking pre-push gate**
to your workflow. The headless review runs via `kimchi -p`, so all inference happens
on your Kimchi subscription. The review methodology is adapted from
[open-code-review](https://github.com/alibaba/open-code-review) (see NOTICE).

This is a **port in progress** (chunked; see the plan in the parent repo). The
reviewer worker is stubbed until the worker-invocation chunk lands; the gate
machinery (hooks, supervisor, verdict store, state machine) is real.

## What it does

Push a branch containing a confident high-severity bug, and the gate blocks it:

```console
$ git push
[review-gate] review-gate blocked this push (high-severity issues):
  [high] db.py:6 - SQL injection: `user_input` is concatenated directly into the
         query string; an attacker can pass `' OR '1'='1` or `'; DROP TABLE users; --`.
  [medium] db.py:10-11 - the sqlite3 connection is never closed; callers leak the handle.

Fix the issues, or bypass with: git push --no-verify
```

## Two enforcement layers (why there are two hooks)

1. **The git `pre-push` hook is the PRIMARY gate** (installed by default into the
   repository's `.git/hooks/pre-push`). It sees every push that reaches git: your
   terminal, scripts, heredocs, `python -c`, `gh`, GUIs — anything that runs git.
   Git resolves the refspec before invoking it, so it always knows exactly what is
   being sent.
2. **The Kimchi bash hook is the UX layer** (`~/.config/kimchi/harness/hooks/bash/review-gate.sh`).
   It intercepts push attempts made by the agent inside a gated repository early,
   and surfaces the block reason verbatim to the model. It only gates repositories
   that installed the pre-push layer; everywhere else it is inert.

The review itself is always **asynchronous**: the hook spawns one detached
supervisor (singleflight) and immediately blocks with "review started — re-run
this push". Re-running the push joins the recorded verdict instantly. This is not
a limitation bolted on later — the Kimchi hook timeout is 5 s and a real review
takes minutes, so "deny + join on retry" is the only correct mode.

## Install

There are two install paths. They are **not** equivalent: only the first one
installs enforcement.

### Primary (recommended): per-repo install — both enforcement layers

```console
$ git clone https://github.com/Jose-Ribeir/kimchi-review-gate.git
$ cd /path/to/the-repo-you-want-gated
$ bash /path/to/kimchi-review-gate/scripts/install-kimchi-hook.sh
```

Run once per repository you want gated. It installs three things:

1. **the git `pre-push` hook** (the PRIMARY gate) into that repository's
   `.git/hooks/pre-push` — this is the layer that sees every push reaching git:
   your terminal, scripts, heredocs, `python -c`, `gh`, GUIs (run from inside
   a linked `git worktree`, it lands in the shared common-dir `hooks/`, since
   git reads a worktree's hooks from the common dir);
2. **the Kimchi bash hook** (the UX layer) at
   `~/.config/kimchi/harness/hooks/bash/review-gate.sh` — installed once, and
   inert in any repository that did not install the pre-push layer; it surfaces
   the block reason verbatim to the model when the agent tries to push;
3. **the deny extension** at
   `~/.config/kimchi/harness/extensions/review-gate-deny.ts` — inert outside
   review sessions (it only acts while the supervisor runs the worker with
   `OCR_IN_REVIEW=1`).

Uninstall with `bash scripts/uninstall-kimchi-hook.sh` from inside the gated
repository: it removes all three components; it restores a pre-existing
`.git/hooks/pre-push` it displaced **unless** the current pre-push is not ours
(no `review-gate.py` marker — e.g. a `pre-push` you replaced by hand, the
natural way to disable the gate without uninstalling) — then it prints a
warning (`WARNING: <dir>/pre-push exists and is not ours; leaving it in place
and keeping <dir>/pre-push.user untouched.`), leaves that file in place, and
leaves `pre-push.user` untouched.
(`scripts/install-git-hook.sh` / `uninstall-git-hook.sh` are upstream
holdovers that manage only the pre-push layer.)

The explicit human escape is `git push --no-verify` (or `OCR_FAIL_OPEN=1`) —
see the threat model above.

### Experimental: `kimchi install` — skills + extension only

The repository doubles as a Kimchi package (the `pi` manifest in
`package.json` declares the skill and the extension):

```console
$ kimchi install /path/to/kimchi-review-gate
```

This installs the `review` skill and the deny extension under
`~/.config/kimchi/harness/` for **discovery convenience** — e.g. invoking
`/skill:review` manually in a session, or browsing the skill via
`kimchi resources list`.

**A package install does NOT gate anything.** Package-installed components
carry no git `pre-push` hook, so the primary gate — the layer that covers
terminal, script, `gh`, and GUI pushes — is absent, and the Kimchi bash hook
(the UX layer) is not registered either. **You still need
`scripts/install-kimchi-hook.sh` per repository for enforcement.** The
package ships the skill and the extension so they are easy to find and invoke
by hand; it is not a substitute for the installer.

**Why no package hooks (C8 open question, deliberately not shipped):** Kimchi
packages *may* also ship `hooks/hooks.json` (the full Claude Code hook event
set, honored by the plugin-package hook adapter), which in principle would
fire the gate from package-installed components alone. Whether such package
carried hooks get their own timeout — and whether it is at or above the
supervisor join budget, rather than the 5 s core bash-hook budget — is
**unverified**. Since the async "deny + join on retry" design depends on that
timing, this package deliberately ships **no hooks**: `hooks/hooks.json` is
excluded from the npm tarball, and the shipped bash hook + repo pre-push
install (Decision D1 of the port plan) remain the default enforcement
mechanism. Revisit after the timeout experiment.

## Threat model (and the human escape hatch)

The gate assumes the commits being pushed are **untrusted input**:

- The reviewer never runs in your repository. It runs in a
  `git clone --no-hardlinks` copy at the push tip (a clone copies no hooks, has
  its own refs/config, and cannot reach the real repo's object database), with
  `node_modules` symlinked in for TypeScript work and `plugins` keys scrubbed
  from tsconfig files.
- Verdicts live **outside any repository** (`~/.local/state/review-gate/`,
  mode 0600, written only by the supervisor). A hostile repository cannot
  pre-plant a clean verdict for itself: it cannot write there, and it cannot
  know which (remote, tip) keys the store will consult.
- Unparseable verdict state fails closed: a corrupt, partial, or unparseable
  verdict record blocks the push, never allows it.
- A push command this gate cannot positively parse is refused: `git push --all`,
  `git push --mirror`, `git push --tags`, a bare `git push` on `push.default`,
  `git -c k=v push` — all block with "re-run with an explicit refspec"
  (`git push <remote> <ref>)`. Unknown is never allowed.
- The gate **fails closed**: a missing `python3`, a missing `kimchi` CLI, a
  crashed review, a timeout, or unparseable reviewer output blocks the push.
  A failed run is retried by the next push of the same tip (up to 3
  attempts); past that the tip is `exhausted` — a loud block naming
  `review-gate.py --reset-tip <tip>`, cleared only by that command or by
  pushing a new tip.
- **Invariant: a push that lands without gate output means the gate was
  absent — never infer a PASS from a landed push.** Silence is absence of
  approval, not approval.
- **The reviewer worker itself is sandboxed by a deny extension** (D13).
  Headless `-p` sessions ignore permission flags (verified on this build), so
  the worker loads `extensions/review-gate-deny.ts` via `-e` and runs with
  `OCR_IN_REVIEW=1`: `write`/`edit`/`web_fetch`/`web_search`/`mcp__*` are
  hard-denied, and `bash` is an **allowlist** — git read verbs (`diff`, `log`,
  `show`, `rev-parse`, `cat-file`, `blame`, `ls-files`, `grep`, `status`,
  `describe`, `merge-base`) plus `grep`/`rg`/`find`/`ls`/`wc`/`cat`/`head`/
  `tail` — deliberately NOT `sed`: sed's script language writes files
  (`sed -n 'w /tmp/out' f`) and GNU sed executes the shell (`e` command,
  `s///e` flag), so no flag can make it read-only; everything else is denied
  with a reason naming what to do instead (network clients, git write/network verbs, git escapes like
  `-C`/`--git-dir`/`-c`, interpreters and package managers, and the
  `kimchi`/`claude` binaries themselves). Parsing is quote-tolerant
  (`git log --grep "git push"` allows; `bash -c "git push"` denies) and
  ambiguous input denies. Because a `-e` extension that silently fails to
  load is invisible to permission flags, the supervisor runs a **startup
  canary**: one cheap headless run with the same argv, prompted to write a
  canary file. If that write succeeds, the extension never loaded — the run
  aborts, an attempt burns, and the error-state deny points at the doctor.
  Not covered: tsserver (LSP) retains a residual surface beyond the scrubbed
  tsconfig `plugins` key — the reviewer can still ask the language server to
  read code it should not need. That residual is documented, not fixed (D6
  kept LSP in v1 deliberately).

**The explicit human escape is `git push --no-verify`** (or `OCR_FAIL_OPEN=1`).
A gate that cannot be bypassed by its owner is a denial-of-service tool, not a
control. `--no-verify` skips the git pre-push hook entirely and is the documented
emergency path; use it deliberately.

Every honored `OCR_FAIL_OPEN=1` bypass is audited: the gate appends a
`gate_skipped` line to its telemetry log (`review-gate-debug.log` under the
external state store) naming the repository, the time, and the revision range
that went unreviewed, so exceptions can be accounted for after the fact.
`git push --no-verify` is different: it never invokes the hook, so no gate code
runs at all and **no telemetry entry can be written** — that escape is
undetectable by design, stated here rather than faked as detection.

## Modes and environment

- default: block confident high-severity findings (any high-severity finding
  blocks; medium blocks at high confidence)
- `OCR_ADVISORY=1` — warn only, never block
- `OCR_MODEL=…` — override the reviewer model pin
- `OCR_TIMEOUT`, `OCR_INLINE_BUDGET` — review wall-clock and hook join budget
- `OCR_KIMI_ARGS` / `OCR_KIMI_EXTRA_ARGS` — worker argv escape hatches, applied
  identically in EVERY worker (reviewer, docs-summary generator): an
  `OCR_KIMI_ARGS` value replaces the per-purpose default argv wholesale (neither
  the defaults nor any extras are consulted under an override);
  `OCR_KIMI_EXTRA_ARGS` only rides along inside a per-purpose default argv —
  each worker's default-args builder decides its own extras (e.g. the
  docs-summary generator applies them, then pins its own `--model`)
- `OCR_STUB_WORKER=1` — run the stub reviewer instead of `kimchi` (tests)
- `OCR_FAIL_OPEN=1` / `git push --no-verify` — the bypasses

## Intent context: commit messages and linked tickets

The reviewer no longer reads only the diff — it also reads *why* the change
exists, delivered as fenced, clearly-labelled context:

- **Commit messages (default: ON).** The pushed commits' messages — subjects
  and bodies, **verbatim**, never paraphrased — are appended to the review
  payload after the diffs, fenced as untrusted author-controlled *claims to
  check against the diff*, never as ground truth and never as instructions.
  On a large (chunked) push each chunk sees in full only the messages of
  commits that touched that chunk's files; commits that touched none of them
  appear subject-only as background. The newest 10 commits stay verbatim
  inside a budget of ~4,000 tokens; beyond that, messages drop to subject
  lines with a visible `trim_notice` so you know context was dropped (a
  100-line subject tail; anything older is omitted with a count).
- **Linked tickets (default: OFF).** When `intent.tickets` is enabled, the
  gate discovers issue keys — per-repo override first, then the branch name
  (e.g. `feature/PROJ-123-…`), then keys mentioned in commit messages — and
  fetches each ticket's **title and description only, never comments**,
  through your own local sign-ins: `gh issue view` for GitHub (falling back
  to `gh pr view` when the key is a PR number), `glab api` for GitLab, and
  the Jira side-fetch (below) for Jira. At most **3 tickets** are fetched;
  the rest are listed by ID only. Descriptions past 4,000 characters are
  hard-truncated with a visible marker. Successful fetches cache for **24
  hours** in the gate's store (`<store root>/tickets/`, keyed by
  tracker+key), so re-pushes review instantly.
- **Stated precedence.** Every context source is rendered as its own labelled
  block — manual requirement text, ticket, commit messages — with the
  precedence stated next to them: when sources conflict, the manual text
  wins, then the ticket, then the commit messages, and conflicts are flagged,
  never silently resolved. (Manual context stays king: the existing
  `requirement_background` lane outranks every automated source. This version
  has no `--intent-file` flag.)

### The `intent_gap` finding family

The reviewer treats every commit message as a *falsifiable claim about the
diff*. When a claim contradicts the code, it may raise an `intent_gap`
finding — kept only after a self-check that quotes the exact claim, quotes
the contradicting code, and argues the implied correction would not break
the behaviour the message describes. Vague or empty messages ("wip",
"cleanup") are ignored rather than hallucinated upon.

- **Blocking rule:** an intent finding blocks the push **only** when the
  claimed work is *entirely absent* from the changes — "the message says this
  ships X, and X is nowhere in the diff". Every other mismatch is a
  **warning and can never block**. The verdict is recomputed from the
  findings, so the model's own severity marking is advisory; `compute_verdict`
  stays the sole verdict authority (ADR 0001).
- **Asymmetry invariant:** intent can *add* findings, never remove them. A
  defect finding stands even when a commit message asserts the behaviour is
  deliberate — you cannot talk the gate out of a bug.
- **Visually distinct:** reports render intent findings as `[intent-blocking]`
  / `[intent-warning]`, so "your message misdescribes your code" is never
  confused with "your code is broken".

### Fail open, always

Ticket enrichment can never delay or block a push: a missing `gh`/`glab`, an
offline network, a lookup timeout (5 s per CLI call), a non-zero exit, or
unexpected output is a **silent skip with a human-readable note** in the
review record; the review proceeds on commit messages alone. Misses are never
cached — a transient VPN outage is retried on the next push.

### Jira

A Jira-style key (`PROJ-123`) is fetched by a **side-fetch worker** that rides
the selected review backend. Under **claude** it is a headless
`claude -p --strict-mcp-config --mcp-config <temp file>` run that enables only
the first Atlassian/Jira entry of `~/.claude.json` (override with
`OCR_JIRA_MCP_CONFIG`). Under **kimchi** (described below) it is one tiny
separate headless `kimchi -p --no-session --mcp-config <temp file>` run whose
temp config enables ONLY the persisted desktop Atlassian connector
(`__kimchi-studio-connector__atlassian`, read verbatim from
`~/.config/kimchi/harness/mcp.json`), reusing the desktop app's existing Jira
login — **no token is ever stored in gate config**. The **review worker's
strict no-MCP posture is unchanged**: its MCP config stays empty; the fetch
lives entirely in that one separate run (30 s timeout, overridable via the
`jira.timeout` setting). Set `OCR_JIRA_MCP_CONFIG` to point the side-fetch at
a different harness config (tests and non-standard installs must never read
the developer's real connector config). Every failure mode — no config file,
no connector entry, CLI missing, timeout, unparseable reply, auth failure —
is the same silent miss note as any other tracker; the result lands in the
same shared 24 h cache.

### Settings

Two optional files, merged key by key (per-repo wins). Missing files mean
defaults; unreadable files or invalid values degrade to defaults *with a
note* — the gate never fails on config.

| File | Scope |
|---|---|
| `<store root>/config.json` — the gate's external store root (`$OCR_STATE_DIR`, else `~/.local/state/review-gate`) | machine-wide defaults |
| `<repo>/.ocr/config.json` — the config file the gate already honours for `blocking` | per-repo overrides; also the **only** place `intent.ticket` is accepted |

Keys (under an `"intent"` object, the `"jira"` block, or the `"checks"`
object; switches accept booleans, `0`/`1`, and on/off-style words):

| Key | Default | Meaning |
|---|---|---|
| `intent.messages` | `true` | inject pushed commit messages into the review |
| `intent.tickets` | `false` | discover and fetch linked tickets |
| `intent.ticket` | — | explicit ticket key (e.g. `"PROJ-123"`); per-repo file only — a global value is ignored with a note |
| `intent.tracker_order` | `["github", "gitlab", "jira"]` | tracker backends tried per key, in order; unknown names are dropped with a note |
| `jira` | `{}` | Jira coordinates, e.g. `{"server": "https://you.atlassian.net", "project": "PROJ", "timeout": 30}` — the side-fetch names the server in its prompt; a positive `timeout` overrides the 30 s default |
| `checks.<name>.enabled` | per duty | on/off switch — a disabled check costs nothing, ever |
| `checks.<name>.mode` | `"block"` | `"block"` or `"warn"`; warn clamps severity to at most medium and confidence to at most 0.5, and can never block a push |
| `checks.<name>.globs` | per duty | repo-relative globs; the check runs only when matching files are in the push diff |
| `checks.<name>.command` | — | repo-relative executable; naming one turns the entry into a command check (see "Custom checks" below) |
| `checks.<name>.sections` | — | ADR structure section list for `adr-wait-what` (else the fallback superset) |
| `checks.<name>.template` | — | path to an ADR template file for `adr-wait-what` |
| `checks.<name>.sentence_cap` | — | positive integer; STE sentence-length cap for `adr-wait-what` |
| `checks.<name>.sweep` | `false` | widen ADR-duty evidence to every `*.md` under the records root; the findings scope stays the change set |
| `checks.<name>.parallel` | `false` | command checks only: declares the check runs concurrently with the review session, against its own throwaway tip clone (SPEC-15.3) — the clone is removed with the run's other temporaries; if it cannot be created the check is skipped with a warning (isolation is never traded for throughput). Non-parallel checks stay sequential, post-review, against the review tree |
| `checks.<name>.timeout` | `60` | command checks only: the check's answer budget in seconds, clamped to 1–3600 (SPEC-15.1; the default matches the 60 s budget every check already got) |

The `checks` object resolves through the same layering as everything else —
built-in defaults, then the store-root `config.json`, then the per-repo
`.ocr/config.json` — and invalid values degrade to load notes, never
failures. Built-ins and their defaults: `docs-deslop` `{enabled: true,
mode: "block", globs: ["**/*.md"]}`; `adr-wait-what` and `adr-integrity`
`{enabled: false, mode: "block", globs: ["docs/adr/**/*.md", "**/adr/*.md"]}`
(the ADR duties presuppose conventions a repo may not follow, so they start
off). Declaring a new name with a string `command` enables a command check
with `{enabled: true, mode: "block", globs: []}`; a declaration without
`command` is ignored with a note — a typo can never break the gate. The v2
stubs `commit_messages` and `code_comments` ship inert (v1 is markdown
only). See "Custom checks" below for the full contract (ADR 0006).

Discovery order: **override file > branch name > commit messages**
(commit-derived keys are marked background). Key shapes: Jira-style
`PROJ-123` and issue-style `#123`. Caps: 10 verbatim commits / ~4k tokens of
messages, 3 tickets fetched (the rest ID-only), 24 h cache TTL, and a
2,000-character cap on the intent text kept in each verdict record.

Every review record carries the intent context actually used — sources,
text, per-chunk counts, fetch notes — as tolerated extra fields (no store
protocol change), so intent's effect on findings can be measured later from
existing records.

### Ops: Jira side-fetch smoke check

One manual verification of the Jira path with the real connector:

1. **Connector present:** the desktop app has Atlassian connected —
   `grep -c '__kimchi-studio-connector__atlassian' ~/.config/kimchi/harness/mcp.json`
   prints `1`.
2. **Enable:** `"intent": {"tickets": true}` plus a `"jira": {"server": "…"}`
   block in the store-root `config.json`, and `"intent": {"ticket": "PROJ-123"}`
   (a real issue) in the gated repo's `.ocr/config.json`.
3. **Push** any commit and let the review finish. The cache then holds the
   ticket — `<store root>/tickets/jira--PROJ-123.json` with the fetched
   `title`/`body` — and the review record names the ticket as a source with
   no fetch notes.
4. **Fail-open drill:** remove the cache file, point
   `OCR_JIRA_MCP_CONFIG=/nonexistent.json` at a missing config (or disconnect
   the connector), and push again. The review still completes on commit
   messages alone, with a Jira miss note recorded — the push is never stalled
   beyond the bounded side-fetch and never blocked by Jira.

## Custom checks

The `checks` object layers two kinds of check on top of the review: three
built-in documentation-quality duties and any command checks you declare.

### Built-in documentation-quality duties

All three are sections of the one documentation-checker agent — one spawn,
one cost — and each runs only when it is enabled AND matching files are in
the push diff. The documentation checker spawns when the review manifest
carries `docs_context` (the pre-existing rule) or `docs_quality`; with
neither, no spawn and no cost.

- **`docs-deslop`** (default ON): changed markdown against the deslop
  rulebook — empty words, metaphor verbs claiming mechanism, AI shape. A
  gate-side scan (catalog hits, throat-clearing, em-dash density, template
  skeletons from `scripts/deslop_catalog.json`) feeds the agent as
  evidence. Cluster rule, enforced gate-side: a finding needs ≥3 co-located
  tells or `document_wide: true`, else it is demoted to a note that can
  never block.
- **`adr-wait-what`** (default OFF): changed ADRs against the repo's own
  structure (your `sections` list or `template` file, else the Status / Date
  / Context / Decision / Consequences superset), Context-before-Decision
  (unconditional), STE-style readability judged from deterministic surface
  stats (`sentence_cap` configurable), and glossary vocabulary.
- **`adr-integrity`** (default OFF): every claim a changed ADR makes,
  verified end to end — referenced files, functions, and config keys exist
  and do what is claimed; cited ADRs actually say what the ADR says they
  say. Guilty until proven innocent, scoped to claims about present reality
  (forward-looking intentions and predictions are not claims): a claim the
  checker cannot verify — unreadable cited thing, indeterminable behavior,
  unreachable external reference — is a blocking finding, never a silent
  pass. Existence facts are computed deterministically; the agent judges
  truth. `sweep` (default off) widens evidence to the whole records root.

**Docs-only pushes:** with an enabled quality duty and matching files in the
diff, a docs-only push now runs the documentation checker — the docs changer
pays for the docs check (mirroring the freshness cost rule). With no enabled
duty the no-manifest path is unchanged: no spawn, no cost.

### Command checks

Declare any executable in your repo as a push check:

```json
{
  "checks": {
    "my-lint": {"command": "scripts/my_lint.sh", "globs": ["**/*.py"], "mode": "warn"}
  }
}
```

- The executable is repo-relative, must be executable, and runs with cwd at
  the review tree root (the pushed tip). An absolute path or any `..`
  component is rejected before anything runs — warning plus telemetry, the
  check is skipped.
- The matched changed files arrive on **stdin** as a JSON array of
  repo-relative paths.
- Findings come back on **stdout** as a JSON array (bare or wrapped as
  `{"findings": [...]}`) of finding objects in the gate's findings shape.
  A check may additionally report its own degradations in a `warnings`
  array (`{"findings": [...], "warnings": [...]}`) — coverage gaps,
  unbuildable probes, flake demotions; they surface in the gate output and
  telemetry like every other check degradation, and a malformed warnings
  field degrades to a note, never a failure.
- A finding may carry `evidence_source: "existing_test" | "ephemeral_test"`
  (which kind of test evidenced it). The gate validates the value against
  that enumeration: absent is fine (non-test checks), an unknown value
  drops the tag with a note while the finding survives. A finding tagged
  `ephemeral_test` should come with the check's no-permanent-coverage
  warning — wording that states the lack (a negation near "permanent
  test"/"coverage", e.g. "no permanent coverage for the race probe");
  its absence surfaces a gate-side warning naming no-permanent-coverage,
  never a verdict change. A warning that merely mentions coverage does
  not pair. The tag flows into the verdict output and telemetry untouched
  by demotion passes.
- The check gets **60 seconds by default**, configurable per check via
  `timeout` (SPEC-15.1; clamped to 1–3600 s). Nonzero exit, timeout, or
  malformed output discards that check's findings and records a warning
  plus telemetry — never a silent pass, never a gate crash.
- `mode: "warn"` clamps severity to at most medium and confidence to at most
  0.5; mode only downgrades, and `compute_verdict` stays the sole verdict
  authority.
- A check whose globs match nothing in the diff costs nothing.

## Relationship to upstream

This repository is a flat port of `claude-code-review-gate` (D2 of the port
plan): files keep upstream's layout so fixes can be diffed across, with the
Kimchi seams (hook wiring, worker invocation, verdict store) documented in the
port plan. LICENSE and NOTICE carry the upstream and open-code-review
attribution.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
