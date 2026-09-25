---
name: doctor
description: Check that the review-gate push gate is actually wired up and able to run. Reports which adapters are active, whether the reviewer can be located, and any version skew between the plugin and the global git hook. Use when a push was not reviewed, when a gate error told you to run the doctor, or after installing or upgrading the plugin.
allowed-tools: Bash, Read
---

# review-gate — doctor

Diagnose whether the push gate can actually run on this machine, and report it
plainly. **Read only — never install, modify, or repair anything.** Say what is
wrong and what the user should run; let them decide.

This exists because of one hard constraint: **a hook that fails to launch,
times out, or dies abnormally is treated by Claude Code as non-blocking.** If
the gate cannot start, pushes are simply not reviewed, and nothing announces
it. That failure is undetectable from inside the hook, so it has to be
checkable on demand. That is this command.

## What to check

Run these and collect the results. Prefer one `Bash` call per group; keep going
if a command fails — a failure *is* a result.

**1. Prerequisites**

- `claude --version` — the reviewer shells out to this. Missing ⇒ the gate
  **fails open by design** (there is no gate without the tool).
- A working Python 3: try `python3 -c "import sys; print(sys.version)"`, then
  `python`, then `py`. Ignore any interpreter whose path contains
  `WindowsApps` — those are Store alias stubs that resolve but cannot execute.
  Missing ⇒ the gate now **fails closed** (blocks pushes) as of 0.3.0.
- `git --version`.
- On Windows only: is Git Bash present? Check `bash --version`, and note the
  path — a `bash.exe` under `System32` is WSL, **not** Git Bash. Also look for
  `bin\bash.exe` or `usr\bin\bash.exe` next to `git.exe`'s install root, since
  Git for Windows' "command line only" option keeps bash off `PATH` while
  Claude Code can still use it.

**2. Plugin wiring (the default adapter)**

- Read `${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json` and report `version`.
- Read `${CLAUDE_PLUGIN_ROOT}/hooks/hooks.json`. Confirm there is a `PreToolUse`
  entry matching `Bash` (it carries `if: "Bash(git *)"`, which is best-effort
  only — the adapters re-check the command themselves, so treat a missing or
  different `if` as cosmetic, not a fault), and report each entry's `timeout`.
- Confirm there is also a `PostToolUse` entry matching `Bash`. This is the
  adapter that puts **non-blocking** findings into the session's context after
  a push; without it, a `warn` verdict is recorded but never surfaced to the
  model. It deliberately has **no** `if` clause and a short `timeout` (30s) —
  it only reads a file. Flag a large timeout here as wrong, not as safe.
- List `pending-*` in the plugin data dir (`$CLAUDE_PLUGIN_DATA`, else
  `~/.claude/plugins/data/review-gate-local`). These are reviews recorded but
  not yet reported — the mechanism that lets findings survive a push that
  failed. A handful is normal and they expire after an hour. Many, all stale,
  means delivery is not running: check the `PostToolUse` entry above.
- Check all four adapter scripts exist and are readable:
  `${CLAUDE_PLUGIN_ROOT}/scripts/gate-hook.sh`, `gate-hook.ps1`,
  `post-hook.sh`, and `post-hook.ps1`.
- Check the timeout ordering, which since 0.6.0 is the reverse of what it
  used to be: `OCR_INLINE_BUDGET` (default 600, clamped to at most 840) **<**
  the `PreToolUse` entry's `timeout` (900) **<** ~975 (the desktop app's
  session watchdog: a CLI silent that long is killed) **<** `OCR_TIMEOUT`
  (default 1800, enforced by the detached supervisor, not by the hook). A
  `PreToolUse` timeout **above 900** is wrong, not safe: it lets the app kill
  the whole session before the hook's own deadline ever fires. A budget at or
  above the hook timeout would let Claude Code kill the hook (non-blocking,
  i.e. fail-open) before the "still running" deny fires; the clamp prevents
  it, but report the configured value.
- Confirm the `SessionStart` entry exists (both scripts). Besides the Python
  probe it emits the "a review was running when this session's previous
  process ended" context, which is what keeps a host-killed process from
  being narrated as a user interruption.
- In the current repository, list `.git/review-gate-async/*.json` (in the
  common git dir) and report each state: `running` with a `heartbeat_ts`
  older than ~45 s is a dead supervisor (the next push restarts it);
  `failed` with `attempts >= 2` will not be retried for an hour unless
  `OCR_FORCE_REVIEW=1`; `done` is a recorded verdict a retry replays. Offer to
  delete a stuck file only when asked. Also list `worktrees/` under the plugin
  data dir; anything older than an hour there is a leak (`git worktree prune`
  in the repo cleans git's side).

**2b. Review ledger** (new in 0.8.0)

- Check for the ledger directory at `.git/review-gate-ledger/` (or
  `<common-dir>/review-gate-ledger/` for worktrees). Its presence means the
  incremental reviewer is in use.
- Count the total number of record files (`.json`) across all fingerprint
  subdirectories. Report it as `N records` and name the cap
  (`OCR_LEDGER_MAX_RECORDS`, default 5000).
- Report the active **fingerprint** hash (first 16 hex chars of the sha256 of
  the current model + `PROTOCOL_VERSION` + prompt-file contents + `.ocr/`
  tree OID + relevant env vars). The fingerprint is printed by the gate at
  the start of each run; it is also stored as the first path component of
  every record. If the fingerprint changed since the last run (different
  subdirectory from the most recently modified record) report the miss reason
  as `fp_mismatch` and note that all files will be reviewed in full until a
  new record is written.
- Report the **hit rate of the last run**, if the snapshot
  (`.git/review-gate-history/<latest>-*.json`) includes the
  `ledger_stats` field: `{full, delta, carry, total}`. A first push will
  show all full; subsequent pushes with unchanged files should show mostly
  carry.
- Note the configured TTL (`OCR_LEDGER_TTL`, default 30 days) and whether any
  records appear older than it.
- Report the local run log (0.9.0): whether `.git/review-gate-telemetry/`
  exists, its size, and the date of the newest line. `python
  scripts/review-gate.py --telemetry-report` prints the summary; if
  `OCR_TELEMETRY=0` is set, say the log is off.
- If `OCR_LEDGER=0` is set in the environment or in a project settings file,
  report it as **warn**: the incremental review is disabled and every push is
  reviewed in full. This is intentional (kill-switch) but worth surfacing.
- If `OCR_FORCE_REVIEW=1` is set, report it as **warn**: ledger reads are
  bypassed for the next push, which reviews every file in full and resets the
  attempt counter. Records are still written after that run.

**3. Global git hook (the optional "everywhere" adapter)**

- `git config --global --get core.hooksPath` — empty means this adapter is not
  installed, which is fine and the default. Say so without alarm.
- `git config --local --get core.hooksPath` — **check this even when the global
  one looks healthy.** Git resolves the local setting first, so a repo that
  manages its own hooks (husky, lefthook, a hand-rolled `scripts/git-hooks`)
  silently takes the global gate out of the chain. If a local value is set, it
  differs from the global one, and `<local>/pre-push` does not mention
  `review-gate`, report it as **`fail`**:

  > git adapter **shadowed** in this repo — the global hook is installed but a
  > repo-local `core.hooksPath` overrides it. Pushes from a plain terminal here
  > are **not gated at all**. Pushes made through Claude Code are still covered
  > by the plugin adapter.

  This is a config-induced silent fail-open and nothing else announces it, so
  do not soften it. The repo owns that setting, so the fix is theirs: chain the
  global hook from their own `pre-push`, or accept plugin-only coverage.
- If set, read `<hooksPath>/pre-push` and report its stamped
  `SCR_INSTALLED_VERSION`. Compare with `plugin.json`'s version and flag any
  mismatch as **version skew**: the hook is a copy made at install time and is
  never updated in place.
- If that file still contains `__SCR_BIN__` or assigns a `SCR_BIN` ending in
  `/bin`, it predates 0.3.0. It should still work (the resolver accepts a legacy
  hint and falls back to the `scripts/` sibling), but recommend re-running
  `scripts/install-git-hook.sh` for a clean state.

**4. Reviewer discoverability**

- Look for the pointer the plugin writes on every run:
  `<CLAUDE_CONFIG_DIR or ~/.claude>/plugins/data/*/gate-dir`. Report the path it
  contains and whether `review-gate.py` exists there. This is what lets an
  already-installed git hook survive a plugin upgrade, so an absent or stale
  pointer is worth reporting.

**5. Environment overrides in effect**

Report any of these that are set, since each changes the verdict: `OCR_MODEL`,
`OCR_TIMEOUT`, `OCR_ADVISORY`, `OCR_FAIL_OPEN`, `OCR_BLOCK_SEVERITY`,
`OCR_BLOCK_CONFIDENCE`, `OCR_CLAUDE_ARGS`, `OCR_CLAUDE_EXTRA_ARGS`,
`OCR_INLINE_BUDGET`, `OCR_INLINE_BUDGET_GIT`, `OCR_FORCE_REVIEW`,
`OCR_LEGACY_RANGE`, `OCR_UNSET_ENV`, `OCR_LEDGER`, `OCR_LEDGER_TTL`,
`OCR_LEDGER_MAX_RECORDS`, `OCR_IMPACT`, `OCR_SIBLINGS`, `OCR_TELEMETRY`.

Also read the current repository's `.claude/settings.json` (and
`settings.local.json`) and flag any `env` entry that sets an `OCR_*` variable:
project settings are applied to the CLI process and inherited by hooks, so a
repository can weaken its own gate (`OCR_FAIL_OPEN`, `OCR_ADVISORY`) without
anyone launching Claude Code differently. That is a finding, not a
configuration.

Call out `OCR_FAIL_OPEN` and `OCR_ADVISORY` specifically — they mean the gate is
**not currently blocking**, which is exactly the thing a user running the doctor
is usually trying to find out.

Note that in hook mode these are read from the environment **Claude Code itself
was launched from**, not from the shell of the `git push` call. A user who set
one inline and saw no effect has hit that, not a bug.

## How to report

Print a short table — check, status (`ok` / `warn` / `fail`), and detail — then
a one-line verdict:

- **Gate is active and blocking** — prerequisites present, at least one adapter
  wired, no fail-open override set.
- **Gate is active but advisory** — working, but `OCR_ADVISORY`/`OCR_FAIL_OPEN`
  means it will not block.
- **Gate is NOT active** — say precisely which link is broken and give the one
  command that fixes it.

Then list any recommended actions, most important first. If everything passes,
say so in one line and stop — do not pad the report.

Finally, remind the user of the limits no configuration removes:
`git push --no-verify` bypasses the git-hook adapter, and pushing from a plain
terminal bypasses the plugin adapter unless the global hook is installed **and
is not shadowed by a repo-local `core.hooksPath`** (check 3 above — installing
the global hook is necessary but not sufficient).
