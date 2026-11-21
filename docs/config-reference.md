# Review-gate configuration reference

Configuration for the review gate lives in two user-level files:

- `~/.config/review-gate/config.json`: run behavior (`review.*`), described
  first below;
- `~/.config/review-gate/settings.json`: which model and thinking level each
  review agent uses per backend, plus the `behavior` knobs; see
  [Settings file](#settings-file-agents-and-behavior).

Precedence for `config.json`, highest first:

1. environment variable,
2. `~/.config/review-gate/config.json` (override the path with `OCR_CONFIG_FILE`),
3. built-in default.

Invalid values at any level warn and fall through to the next level; a
missing or malformed config file is not an error. Decision record: ADR
0006.

## File shape

```json
{
  "review": {
    "depth": "full",
    "timeoutSeconds": 0,
    "failOpen": false
  }
}
```

## Keys

| Key | Values | Default | Env override | Notes |
|---|---|---|---|---|
| `review.depth` | `full`, `structural`, `large-diff-scoped` | `full` | `OCR_REVIEW_DEPTH` | `full` is the operator default: always full-depth review. `structural` reviews per-file skeletons and hotspots; `large-diff-scoped` scopes very large diffs and names the files not deeply read. |
| `review.timeoutSeconds` | integer ≥ 0 | `0` (uncapped) | `OCR_REVIEW_TIMEOUT_SECONDS` | Wall-clock cap for the reviewer call. On expiry the gate fails closed; the escape routes below apply. Values above the hook harness's own timeout (`hooks/hooks.json`, **900s**) trigger a loud gate warning: under the PreToolUse adapters the host kills the hook at 900s, and a killed hook is a silent fail-open. The value is warned about, never clamped. |
| `review.failOpen` | boolean | `false` | `OCR_FAIL_OPEN` (`1`/`true`/`yes`) | When true, gate-internal failures allow the push after recording an audited bypass. Prefer the one-shot env usage; a persistent file setting silently weakens every push. |

## Settings file (agents and behavior)

One schema-validated source for the model and thinking level of every review
agent under each backend (`claude`, `kimchi`), plus a small `behavior`
section. Implementation: `scripts/rg_settings.py`; the JSON schema
`schemas/settings.schema.json` is generated from it and kept in step by a
test. Decision record: ADR 0010 (and ADR 0005 for the provider guard).

### Location and precedence

| Layer | Source |
|---|---|
| 1 (highest) | environment variables (`OCR_*`, listed below) |
| 2 | project file `<repo>/.ocr/settings.json` |
| 3 | user file `~/.config/review-gate/settings.json` (`OCR_SETTINGS_FILE` relocates it) |
| 4 | built-in defaults (`scripts/rg_settings.py`) |

Resolution is per key. With no file and no override the gate behaves as the
defaults below. A missing file is normal; an **unreadable or invalid** file
is an error that names the file and every problem (the gate denies and the
doctor fails). There is deliberately no fallback to defaults, so a typo in a
pinned model is seen instead of silently replaced. On the kimchi backend a
file or env value naming an Anthropic or OpenAI model is rejected (the ADR 0005
guard); the claude backend is exempt.

### File shape

```json
{
  "agents": {
    "spotter": {
      "kimchi": {"model": "deepseek-v4.1-flash-1017", "thinking": "high"},
      "claude": {"model": "haiku"}
    }
  },
  "forge": {"name": "gitlab", "host": "git.example.org"},
  "behavior": {
    "inline_wait_seconds": {"hook": 300, "git": 300},
    "spotter_skip": {"enabled": false, "max_files": 3, "max_changed_lines": 60},
    "canary_cache": {"enabled": true, "ttl_days": 7},
    "docs_checker_parallel": {"enabled": true}
  }
}
```

### Agents: model and thinking defaults

Keys are `agents.<agent>.<backend>.model` and
`agents.<agent>.<backend>.thinking` (`off`, `low`, `medium`, `high`, `max`).
Thinking is applied on the kimchi backend as a `:<level>` suffix on `--model`
(for example `kimchi-dev/kimi-k3.1-pro:high`); the claude Task tool takes no
thinking level, so the claude `thinking` keys are accepted but not passed.
`-` below means no level is passed. Agent names map to the gate's tiers:
`spotter` is the spotter, `code-reviewer` is tier 1 (fast lane) and
`deep-reviewer` is tier 2 (deep lane).

| Agent | kimchi model | claude model | Thinking (default) | Env override (kimchi / claude) |
|---|---|---|---|---|
| `spotter` | `deepseek-v4.1-flash-1017` | `haiku` | `high` | `OCR_SPOTTER_MODEL` / `OCR_CLAUDE_SPOTTER_MODEL` |
| `code-reviewer` | `kimchi-dev/kimi-k3.1-pro` | `sonnet` | `high` | `OCR_TIER1_MODEL` / `OCR_CLAUDE_TIER1_MODEL` |
| `deep-reviewer` | `kimchi-dev/kimi-k3.1-pro` | `opus` | `max` | `OCR_TIER2_MODEL` / `OCR_CLAUDE_TIER2_MODEL` |
| `docs-checker` | `kimchi-dev/glm-5.3` | `sonnet` | `high` | none |
| `code-filter` | `kimchi-dev/deepseek-v4.1-flash-1017` | `haiku` | `high` | none |
| `code-resolver` | `kimchi-dev/glm-5.3` | `haiku` | `high` | none |
| `docs-summary` | `kimchi-dev/glm-5.3` | `sonnet` | - | `OCR_DOCS_SUMMARY_MODEL` / `OCR_CLAUDE_DOCS_SUMMARY_MODEL` |
| `orchestrator` | `kimchi-dev/kimi-k3.1-pro` | `sonnet` | - | `OCR_MODEL` / `OCR_CLAUDE_MODEL` |
| `canary` | `kimchi-dev/kimi-k3.1-pro` | `haiku` | - (never suffixed) | none |

Notes:

- Under claude the shared (non-`CLAUDE`) variables are honoured only when they
  name an Anthropic model (a kimchi id must never reach `claude --model`);
  `OCR_MODEL` is honoured only without a vendor prefix. A blank variable is
  unset. `OCR_KIMI_ARGS` (full argv override) is left untouched: no model or
  thinking suffix is added to it.
- The model variables are the pre-existing switches and keep working
  unchanged. Environment overrides exist for models only, not for thinking
  levels.
- The claude defaults are aliases, not dated ids, so the pin tracks the model
  family.
- **Deprecated ids** (the doctor and `set` warn about them when pinned in a
  file or env, not for the built-in defaults): `kimi-k3` is replaced by
  `kimi-k3.1-pro` (retires 2026-12-17), and bare `deepseek-v4.1-flash` by
  `deepseek-v4.1-flash-1017` (retires 2026-12-02). Only the exact id matches.

### `forge` keys

| Key | Default | Meaning |
|---|---|---|
| `forge.name` | not set (detect) | `github` or `gitlab`. Pins the forge the merge-request lookup talks to for the origin remote. |
| `forge.host` | not set (any host) | Limits the `forge.name` pin to one host (`git.example.org`, optionally `:port`). A remote on any other host is detected as usual. |

The forge for a remote host is chosen in this order, first match wins:

1. `forge.name` (and `forge.host`, when set) from the project or user settings file.
2. A known public host: `github.com` is github, `gitlab.com` is gitlab.
3. A host that `glab auth status` (checked first) or `gh auth status` says it
   is logged in to. Each call is bounded at 5 s; a missing or slow CLI counts
   as "not logged in".
4. A guess from the host name: a name containing `github` is github, one
   containing `gitlab` is gitlab.

If none applies the forge is "not recognised" and the merge-request lookup is
skipped with a reason line; this is never a failure. The settings command
(`show`) and the doctor print the effective forge for `origin` and which of
these steps chose it (`setting`, `logged-in host` or `name guess`), for example
`forge: gitlab [setting: forge.name=gitlab for git.example.org in <file>]`.
Set it with `py scripts/rg_settings_cli.py set forge.name gitlab`.

### `behavior` knobs

| Key | Default | Env override | Meaning |
|---|---|---|---|
| `behavior.inline_wait_seconds.hook` | `300` (integer >= 0; effective cap 840) | `OCR_INLINE_BUDGET` | Seconds the **claude** hook waits inline for the verdict before denying with "still running". A **kimchi** hook ignores this key and keeps a fixed 3 s wait (`OCR_INLINE_BUDGET`, never above 4 s) because kimchi kills a bash hook at 5 s; an unknown harness is treated as kimchi. |
| `behavior.inline_wait_seconds.git` | `300` | `OCR_INLINE_BUDGET_GIT` | Seconds the pre-push (git mode) waits when stderr is not a terminal (a push run inside an agent's Bash tool, hard ceiling 600 s). A push at a human terminal waits the full reviewer timeout. |
| `behavior.spotter_skip.enabled` | `false` | `OCR_SPOTTER_SKIP` (`1`/`0`, `true`/`false`, `yes`/`no`, `on`/`off`) | Skip the spotter for a docs-only push or a tiny change. **Off by default** until the cross-file recall scenarios have been run on a real kimchi. Risk-glob escalation to the deep lane is unaffected. Known limitation: the review fingerprint does not include the skip state (ADR 0010). |
| `behavior.spotter_skip.max_files` | `3` | none | A push touching more files than this runs the spotter. |
| `behavior.spotter_skip.max_changed_lines` | `60` | none | A push changing more lines than this runs the spotter. |
| `behavior.canary_cache.enabled` | `true` | `OCR_CANARY_CACHE` (boolean as above) | Remember a passed kimchi deny-extension canary instead of re-running it per supervisor. The key is the kimchi version, the deny-extension hash and the canary model; a failed, corrupt, expired or future-dated entry is a miss. |
| `behavior.canary_cache.ttl_days` | `7` | none | How long a passed canary is remembered. |
| `behavior.docs_checker_parallel.enabled` | `true` | none | Run the docs-checker beside the reviewer instead of after it. |

The inline wait timing out always **denies** the push (the re-push joins the
running review); it never allows. An environment override wins over both
files for the keys that have one (`0` forces a boolean off even when a file
enables it).

Related, not in the settings file:

| Variable | Default | Meaning |
|---|---|---|
| `OCR_DOCS_SUMMARY_WAIT` | `5` (seconds) | How long the manifest build waits for a background docs-summary regeneration before using the diff-based docs context and warning `docs summary unavailable` (ADR 0010). |

### Commands

```
py scripts/rg_settings_cli.py show [--repo PATH] [--json]
py scripts/rg_settings_cli.py set <key> <value> [--project|--user]
py scripts/rg_settings_cli.py validate
```

(Use `python3` on macOS/Linux.) The same commands are available inside the
host as `/review-gate:settings` (`commands/settings.md`).

- `show` (the default) prints every effective value for every agent x backend
  model and thinking level, `forge.name` / `forge.host` (`(not set: detected)`
  when unset) and every behavior knob, with its source (`env:<VAR>`,
  `project:<path>`, `user:<path>` or `default`) and which files exist. The last
  line is `effective forge for origin`, with the chosen forge and its source
  (see `forge` keys above). `--json` gives the same as data.
- `set` validates before writing and writes nothing on error (exit 1). It
  writes the user file unless `--project` is given, rejects Anthropic/OpenAI
  models on `agents.*.kimchi.model`, warns on deprecated ids, and says when a
  higher-precedence source still wins. It never overwrites an invalid file.
  Example: `py scripts/rg_settings_cli.py set behavior.spotter_skip.enabled true --user`.
- `validate` checks the project and user files and reports each problem.
- `scripts/ocr_doctor.py` also reports the effective forge for `origin` and its
  source (an info line, never a failure) and checks the settings (invalid files, the
  provider guard, deprecated ids and, when kimchi is installed, pins against
  `kimchi --list-models`). `scripts/kimchi-verify.sh` runs the real-kimchi
  checks (every pin against the model list, the `:high` and `:max` suffixes, a
  timed sample review) and writes `kimchi-verify-report.txt`.

### Repository rule file

This repository's `.ocr/rule.json` sets `risk_globs` to `[]`. The loader only
appends entries to the built-in risk globs, so an empty list keeps the default
escalation (migrations, auth, payments, concurrency and the like go to the deep
lane) while ordinary source changes use the fast lane. The earlier `["**"]`
forced every push into the deep lane.

## Review range and waivers

### Review range explanation

`py scripts/review-gate.py --print-range` prints the range a push of `HEAD`
would review on stdout and explains it on stderr, changing nothing in the
repository or the state store (the merge-request lookup runs live and the
24 h cache is neither read nor written):

- the base and `rule -- reason`: `fast-forward`, `mr-target`, `boundary`,
  `boundary-chain`, `already-on-remote` or `unevaluable`;
- the commit count and changed-line count;
- the merge-request lookup status, the forge command and the MR target branch;
- the target-branch fetch line and the target-ref freshness line;
- the earlier completed runs the push would join, and the forge choice
  (`forge: ...`) when a lookup was attempted.

`boundary-chain` is the rule used when the target branch was merged into the
branch: the commits no remote ref holds start at several boundaries that lie
on one line of history, and the newest one is the base, so the target
branch's own commits are not counted as the branch's. Boundaries on separate
lines keep the wider merge-base (it over-reviews, never skips).

Before it takes the merge-base against the MR target, the gate fetches that one
branch into `refs/remotes/<remote>/<target>` (no other ref, no tags, no
working tree). The fetch is bounded at 10 s and fails open: a failure or
timeout prints a `WARNING: could not fetch ...` line, uses the local ref and
may give a wider range.

A completed review joins whether it passed or **blocked**: a blocked run still
reviewed its commits, so they count as covered, and old files the fix touches
are re-reviewed whole. A run that timed out or failed writes no record and
never joins; one that finished with files unreviewed is named as not joined.
`OCR_BASE_V1=1` restores the old base selection; `OCR_COVERAGE_V1=1` restores
the old SHA-only join.

### Waiver identity for test findings

A test finding's waiver anchor and finding id ignore run-dependent tokens in
its evidence (run times, durations, timestamps), so the same failure keeps one
id across runs. The exact list of ignored token types, and what is deliberately
not ignored, is in the
[ADR 0007 addendum](adr/0007-race-regression-test-checks-and-finding-waivers.md#addendum-2026-11-21-run-dependent-tokens-in-test-evidence).
`review-gate.py waive --list` marks each waiver that no finding of the last
recorded verdict matches with `NO LONGER MATCHES`. Waivers and test-finding
ids created before this change stop matching once and must be granted again;
reviewer-finding ids are unchanged.

## Related environment surface (pre-existing)

| Variable | Meaning |
|---|---|
| `OCR_CONFIG_FILE` | Redirects the config file location (tests, sandboxed runs). |
| `OCR_STATE_DIR` | Redirects the external state store (`~/.local/state/review-gate` default). |
| `OCR_FAIL_OPEN` | One-shot fail-open for a single invocation; every use is recorded in `bypasses.jsonl` and announced. |

`git push --no-verify` skips git-hook enforcement entirely for that
push. It never invokes the hook at all, so it is **unrecordable by
design**: nothing lands in `bypasses.jsonl`, and the forge's own audit
trail is the only evidence. The recorded bypass is the
`OCR_FAIL_OPEN` / `review.failOpen` path. All bypass records live in
the state store with timestamp, mechanism, remote, ref, and tip.

A git-mode push that goes through `OCR_FAIL_OPEN` also writes an
**unverified** entry (`kind`, `remote`, `ref`, `base`, `tip`, `reason`, `ts`) to
`<store>/unverified/`, one file per remote, ref and tip. It sits beside, never
inside, `verdicts/`, so it can never count as a reviewed commit. The next
git-mode push on that branch prints a warning on stderr naming the base (up to
the last three entries, with a count of older ones): an earlier base was never
reviewed. The warning never blocks and does not change the exit code; an entry
for the tip being pushed now is not reported.

## Installer flags

`install-git-hook.sh` installs the global pre-push hook **by default**
(chaining any pre-existing global `core.hooksPath` — see ADR 0006(a)).
Pass `--no-git-hook` for the legacy opt-in behaviour, `--chain-into
<repo>` to also chain a repo-local hook set, and the documented
uninstall to restore the backed-up previous hooksPath.
