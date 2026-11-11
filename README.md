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
   your terminal, scripts, heredocs, `python -c`, `gh`, GUIs;
2. **the Kimchi bash hook** (the UX layer) at
   `~/.config/kimchi/harness/hooks/bash/review-gate.sh` — installed once, and
   inert in any repository that did not install the pre-push layer; it surfaces
   the block reason verbatim to the model when the agent tries to push;
3. **the deny extension** at
   `~/.config/kimchi/harness/extensions/review-gate-deny.ts` — inert outside
   review sessions (it only acts while the supervisor runs the worker with
   `OCR_IN_REVIEW=1`).

Uninstall with `bash scripts/uninstall-kimchi-hook.sh` from inside the gated
repository: it removes all three components and restores any pre-existing
`.git/hooks/pre-push` it displaced. (`scripts/install-git-hook.sh` /
`uninstall-git-hook.sh` are upstream holdovers that manage only the pre-push
layer.)

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
  Three failed attempts put the tip in a sticky error state whose message
  points at the doctor command.
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
  `tail`/`sed -n`; everything else is denied with a reason naming what to do
  instead (network clients, git write/network verbs, git escapes like
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
- `OCR_STUB_WORKER=1` — run the stub reviewer instead of `kimchi` (tests)
- `OCR_FAIL_OPEN=1` / `git push --no-verify` — the bypasses

## Relationship to upstream

This repository is a flat port of `claude-code-review-gate` (D2 of the port
plan): files keep upstream's layout so fixes can be diffed across, with the
Kimchi seams (hook wiring, worker invocation, verdict store) documented in the
port plan. LICENSE and NOTICE carry the upstream and open-code-review
attribution.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
