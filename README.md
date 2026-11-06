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

```console
$ bash scripts/install-kimchi-hook.sh     # from inside the repository to gate
```

This installs (a) the repo-level git `pre-push` hook, (b) the Kimchi bash hook at
`~/.config/kimchi/harness/hooks/bash/review-gate.sh`, and (c) reserves the
deny-extension slot (`~/.config/kimchi/harness/extensions/review-gate-deny.ts`,
landed by a later chunk). Uninstall with
`bash scripts/uninstall-kimchi-hook.sh`, which removes all of them.

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

**The explicit human escape is `git push --no-verify`** (or `OCR_FAIL_OPEN=1`).
A gate that cannot be bypassed by its owner is a denial-of-service tool, not a
control. `--no-verify` skips the git pre-push hook entirely and is the documented
emergency path; use it deliberately.

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
