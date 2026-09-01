# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.3] - 2026-09-01

### Fixed
- **A heredoc opener followed by a pipe was not recognised.** The lookahead
  added in 0.4.2 allowed only *redirections* after the marker, so
  `cat <<'EOF' | grep x` and `python - <<'PY' | tee log` — the shape this
  project's own tooling uses constantly — were not treated as openers, their
  bodies went unstripped, and body text resembling a `cd` chain was misparsed
  exactly as before the stripping existed. What actually distinguishes an
  opener from the same characters used as data is what *follows* the marker:
  end-of-line, a redirection, or a pipe/separator — never a bare word. The
  lookahead now says that, which is wider without being weaker.
- **`_resolve_pushed_repo`'s docstring still described the removed single-hop
  behaviour** after the implementation moved to the folded chain — the same
  hazard this release line hit twice already: a comment asserting something the
  code stopped doing.

## [0.4.2] - 2026-09-01

### Fixed
- **Only the last `cd` hop was anchored to the session directory.** A relative
  hop is relative to the hop before it, so in a two-hop chain the second lives
  under the first, not under the session dir. Joining it onto the base produced
  a path that either did not exist — falling through to the fallback this code
  exists to avoid — or existed and pointed somewhere unrelated. `_effective_cd`
  now folds the chain in order: an **absolute** hop re-anchors and clears an
  earlier unknown (it fully determines the destination regardless of what came
  before), a relative hop after an unknown one stays unknown, and `cd -` is
  unfollowable since it needs the shell's directory history.
- **Heredoc bodies were parsed as commands.** A body the command *writes* is
  data the shell never runs. This was not academic: the change adding these
  tests was itself denied by the gate, because the heredoc it was writing
  contained a relative `cd` chain as test data — the parser read it as real,
  resolved it to nothing, and the unknown-target rule blocked an innocent
  command. `_strip_heredocs` drops bodies before parsing, keeping the opener
  line (a real `cd` can share it) and handling quoted and unquoted markers.
  - Stripping is deliberately narrow, because a first cut of it was worse than
    the problem: it matched `<<WORD` anywhere on a line and, with no matching
    terminator, discarded everything to the end of the command — which could
    swallow a genuine `cd` and a genuine push and leave the parser blind. An
    opener must now **end its line** (bar trailing redirections), and a body is
    dropped **only when a terminator is actually found**. Unterminated input
    therefore errs toward reading the body as code — at worst resolving the
    wrong directory and *denying* — rather than toward deleting real commands
    and allowing. Wrong-and-blocking is recoverable; blind-and-allowing is the
    fail-open this whole line of work exists to close.
  - Known residual: a `cd` chain inside a **quoted argument** is still parsed
    as code. Stripping quoted spans wholesale is not safe, since
    `cd "path with spaces"` is exactly that shape.

## [0.4.1] - 2026-09-01

### Fixed
Three high-severity findings the gate raised against its own 0.4.0 release,
all in the `cd`-resolution code that release introduced.
- **A multi-line command matched nothing.** `_cd_targets`' separator
  alternation had no newline case and `re.finditer` is not `re.MULTILINE`, so
  `^` only ever meant the start of the whole string — `cd repo\ngit push`
  yielded zero targets, and a Bash tool call is routinely multi-line.
- **`_looks_like_real_push` had the same gap, and there it was worse.** A
  `git push` on its own line returned `False`, so the unknown-target block that
  function gates silently never fired — dead for exactly the command shape it
  was meant to catch.
- **Relative `cd` targets were not anchored.** Both resolvers called
  `os.path.isdir` on the raw capture, which Python resolves against the *hook
  process's* cwd — the very confusion this code path exists to correct. A new
  `_cd_candidate` expands `~` and joins a relative target onto the session's
  cwd. It still does not `expandvars`: `$T` was set by the shell running the
  command, not in ours.

## [0.4.0] - 2026-09-01

### Added
- **Findings now actually reach the model, via a PostToolUse adapter.** This is
  the headline fix, and it exists because the 0.3.3 mechanism below never
  worked. Verified against Claude Code's own transcript records: a PreToolUse
  `permissionDecisionReason` on an **allow** decision produces a `hook_success`
  entry and nothing else, while a PostToolUse `additionalContext` produces a
  separate `hook_additional_context` record — and that companion record is what
  delivers text into the session. So blocking findings (delivered as the
  `tool_result` of a deny) were always seen, and non-blocking ones never were,
  no matter how loudly they were printed.
  - `hooks/hooks.json` gains a `PostToolUse` entry matching `Bash`, dispatching
    to new `scripts/post-hook.sh` / `post-hook.ps1`. Timeout 30s, not 1920: this
    path only reads a file and must never be able to stall a session.
  - The new adapters **fail open**, the deliberate inverse of `gate-hook.*`.
    That pair decides whether a push proceeds, so it must block when it cannot
    run; this pair decides only whether a report is printed, so it goes quiet.
    Nothing but a JSON object ever reaches its stdout.
  - `review-gate.py --mode post` replays the record an earlier review already
    wrote to `FINDINGS_LOG`. Silent on a clean pass, loud about a block-level
    finding that advisory mode let through, and never renders an empty findings
    block when the log shed them to fit `_MAX_LOG_LINE`.
  - Delivery is deduplicated by `scr-post-delivered-*` markers, claimed with
    `O_CREAT|O_EXCL` and swept by the existing `MARKER_TTL` reaper: silent on a
    repeat within one session, re-delivered to a new session whose context
    genuinely lacks the findings.
  - Known gap: PostToolUse does not fire for a tool call that fails, so a
    rejected push delivers nothing. The marker is claimed only after emitting,
    so the retry reports instead. A command that updates the ref yet exits
    non-zero and is never retried (a multi-ref push with one ref rejected,
    `git push && gh pr create` where `gh` fails) leaves the findings in the log
    for `--history`.
- **`/review-gate:doctor` detects a shadowed git adapter.** It only ever read
  `core.hooksPath` from **global** config, but git resolves the **local** one
  first — so any repo managing its own hooks (husky, lefthook, a hand-rolled
  `scripts/git-hooks`) silently drops the global gate out of the chain while
  the doctor reported a clean bill of health. Plain-terminal pushes in such a
  repo are ungated, and nothing announced it. `--mode post` surfaces the same
  warning once per session.

### Fixed
- **0.3.3's "hook mode reports non-blocking findings to the calling session"
  was not true.** `permissionDecisionReason` on an allow reaches the hook's own
  record, visible UI-side and useful when debugging, and goes no further. The
  reason string is kept for that purpose; the comment asserting it was a
  delivery channel is corrected in place, because in a fail-closed tool a false
  load-bearing comment is how the next regression ships.
- **The gate reviewed whatever repo the process happened to sit in.** In hook
  mode `repo_root` came from the process cwd — the directory Claude Code was
  launched from — while Claude routinely pushes as `cd <repo> && git push`. The
  gate therefore inspected the *session's* repo, found nothing unpushed, and
  allowed a push it had never looked at. Silent, and total in any repo where
  the git adapter is absent or shadowed. `_resolve_pushed_repo` now derives the
  target from the command, and `_has_unpushed_commits` takes the repo instead
  of asking the cwd. (A comment at the call site had described this intent
  since before the code could deliver it.)
- **Reviewer output was decoded with the locale's codepage.** Both
  `subprocess.run` calls used `text=True` with no `encoding=`, so on a default
  Windows box the reviewer's UTF-8 was read as cp1252: every em-dash in a
  finding became `â€"` and was then stored that way permanently, in the
  findings log, the raw snapshot, and the context injected into the session.
- **Em-dashes and ellipses in terminal-bound strings** mojibake through git's
  stderr on Windows. All such strings are ASCII now.

### Changed
- **The gate blocks when it cannot determine which repository a push targets**
  (an unexpanded `$VAR`, a command substitution, a path that is not a repo),
  honouring `OCR_FAIL_OPEN`. Same rule the rest of the tool applies to every
  "cannot run" case. Deliberately narrower than the review trigger: reviews
  fire on a loose `git push` substring because over-reviewing is cheap, but a
  block additionally requires `git push` at a command position, so a command
  that merely *mentions* it — a grep pattern, a heredoc, a commit message —
  is never denied for a reason about pushing.

## [0.3.3] - 2026-08-28

### Added
- **Non-blocking findings are now persisted instead of destroyed.** A `warn`
  or `pass` verdict lets the push through, printed its findings once to stderr,
  and left the detail only in `review-gate-last-output.json` — which
  `_save_raw_output` overwrites on every run (`scripts/review-gate.py:280`
  before this change). Medium/low findings on a passing push were therefore
  unrecoverable as soon as the next review started.
  - `_record_review` appends one JSON line per completed review — findings
    verbatim, verdict, branch, HEAD sha, mode, timestamp — to
    `.git/review-gate-findings.jsonl`. Written for **every** verdict, blocking
    or not, and never pruned by this tool.
  - `_archive_raw_output` also snapshots the reviewer's raw stdout to
    `.git/review-gate-history/<UTC>-<sha7>.json`. Only these snapshots rotate
    (`OCR_HISTORY_LIMIT`, default 50, `0` = keep all); the findings log does not.
  - `review-gate.py --history [N]` replays recorded reviews. Without it there
    was no command that could show a passing review's findings again.
- **Push markers carry the findings of the review that wrote them.** They held
  a bare epoch float, so when the paired adapter short-circuited on a fresh
  marker it allowed silently and the first run's findings were shown exactly
  once. `_write_marker`/`_read_marker`/`_prior_findings_note` now replay them;
  markers written by older versions still parse (treated as "no findings").
- **Hook mode reports non-blocking findings to the calling session.** `allow()`
  takes an optional reason, so a `warn` verdict rides back in
  `permissionDecisionReason` instead of going to a stderr stream Claude Code
  does not surface on an allow decision.


### Fixed
- **`_archive_raw_output` claimed a snapshot filename with
  `while (d / name).exists()` and only then wrote it** — check-then-act, so
  the two adapters archiving the same HEAD inside one UTC second could both
  see the same name free and one would overwrite the other, which is the
  collision that loop exists to prevent. Found by the gate reviewing its own
  commit. The name is now taken with `os.open(..., O_CREAT|O_EXCL|O_WRONLY)`
  and the retry moves on instead of overwriting (bounded at 100 attempts).
  Findings were never at risk — they are written separately to the findings
  log — but a raw snapshot could be lost, leaving one review's log entry
  pointing at another run's stdout.
- **Archived snapshots were created executable on POSIX.** `os.open` defaults
  to mode `0o777` (`0o755` under the usual umask), while every sibling
  artifact this tool writes goes through Python's io layer and lands at
  `0o644`. The mode is now passed explicitly as `0o666`.

## [0.3.2] - 2026-08-20

### Added
- **A `SessionStart` check warns immediately if no working Python 3 is
  found**, instead of the user only finding out when a real `git push` gets
  denied. Claude Code has no plugin "on install" hook, so this fires on every
  session start/resume — silent when Python is fine, and a short warning
  (with the fix steps and `/review-gate:doctor` pointer) when it isn't.
  `scripts/session-start-check.sh` / `.ps1`, same two-adapter pattern as the
  push gate itself.

### Fixed
- **The push matcher's `if: "Bash(*git push*)"` was invalid syntax and
  silently failed open.** Per Claude Code's own hooks docs, an unparseable
  `if` pattern makes the hook run regardless of the command — `*text*`
  substring wildcards aren't documented syntax. So the gate hook fired on
  *every* Bash call, not just `git push`, confirmed when it denied an
  unrelated `git config --global --get core.hooksPath` for lack of a working
  Python interpreter. `if` is now `Bash(git *)` (valid, still best-effort),
  and `gate-hook.sh`/`gate-hook.ps1` now read the PreToolUse payload and check
  for `git push` themselves — before ever resolving Python — so a non-push
  Bash call can no longer be blocked by a missing/unreachable interpreter.

## [0.3.1] - 2026-08-20

### Changed
- **Review token spend cut ~70-85% per run.** `git diff` now passes `-M` for
  rename detection, so a moved file no longer renders as a full delete+add. A
  new symbol-context step resolves cross-file references via Serena or
  `git grep HEAD` instead of giving the reviewer unrestricted Bash and telling
  it to go read files itself. Hard caps added throughout: per-file and total
  diff size, symbols per file, git-grep calls, snippets, and reference counts.

### Fixed
- **The gate's own README/header lied about failing open.** `gate-hook.sh`
  still opened with "Fails open" while its body (rewritten in 0.3.0) denies
  the push when there's no working Python. Corrected to match actual
  behavior.
- **The shell resolver had no test coverage.** `_resolve_gate_dir` is the
  mechanism the whole plugin depends on to find its own reviewer; a
  regression there could silently reintroduce the exact bug 0.3.0 closed.
  Added `tests/test_pre_push_resolver.py`, which caught a real Windows bug:
  `${hint%/bin}` only strips a forward-slash suffix, so a `C:\...\bin` hint
  skipped the legacy fallback and blocked pushes even with a working gate.
  Both path separators are now handled.
- **Backgrounded review subagents could race the JSON verdict.** The
  orchestrator never specified `run_in_background: false`, so a stray async
  notification landing after the final `--json` verdict was printed could
  silently replace it, false-blocking headless pushes with an
  unparseable-output error.

### Security
- **Dropped `Bash(git grep *)` from the headless allowlist.**
  `git grep -O/--open-files-in-pager=<cmd>` executes an arbitrary
  caller-supplied command, and a prefix-wildcard allowlist pattern can't
  exclude just that flag. All textual searches now go through the `Grep`
  tool instead, which has no equivalent flag and isn't exploitable via
  prompt injection.

## [0.3.0] - 2026-08-14

A hardening release, ahead of publishing the plugin. Three of these were silent
fail-opens in a tool whose headline claim is that it fails closed.

### Security
- **The reviewer could be turned into a shell by the code it was reviewing.**
  Its input is an untrusted diff, and it was handed tools to match:
  `--allowedTools "Bash Read Grep Glob Task"` pre-approved *unrestricted* Bash,
  `--setting-sources project` loaded the reviewed repo's own
  `.claude/settings.json` (settings can define hooks, and hooks execute), and
  the global git-hook adapter defaulted to `--dangerously-skip-permissions` on
  top. Composed, a hostile branch could run arbitrary code on `git push`. The
  allowlist is now read-only (`git diff`/`ls-files`/`log`/`show`/`rev-parse`/
  `status`, plus `Read`/`Grep`/`Glob`/`Task`), Write/Edit/NotebookEdit/WebFetch/
  WebSearch are removed from the model's context outright, settings sources are
  empty, and skip-permissions is no longer defaulted — which is what makes the
  allowlist binding, since a headless session has nobody to prompt and refuses
  anything outside it.
- **Findings are sanitized before being echoed back.** In hook mode they land in
  `permissionDecisionReason`, i.e. straight into the calling session's context,
  and they originate in the diff. Control characters are stripped and length is
  capped, so one finding cannot forge extra report lines.
- **Added a re-entry guard.** The review session is launched with
  `--plugin-dir`, so this plugin's own push gate was registered inside it: every
  Bash call the reviewer made spawned a Python process, and a push from within a
  review would have recursed into a second full review. `OCR_IN_REVIEW=1` now
  short-circuits both adapters.

### Fixed
- **Review markers no longer accumulate in the git dir.** A marker is written per
  reviewed HEAD sha so the paired adapter (the Claude Code hook and the global
  git hook) can skip re-reviewing the same push, and is only ever honored within
  `MARKER_TTL`. Nothing removed the expired ones, so `.git/` gained one
  `scr-push-reviewed-*` file per passing push, forever. `_reap_markers()` now
  sweeps expired markers whenever a new one is written. Only expired markers go:
  a fresh marker for another sha is still load-bearing, since the paired adapter
  may be mid-push against a different HEAD. Failures during the sweep are
  swallowed — housekeeping must never break the gate.
- **No-Python failed OPEN.** Both adapters skipped the review and allowed the
  push when no working interpreter was found, while the docs insisted a missing
  `claude` binary was the only fail-open path. They now block, and say how to
  recover.
- **Windows without Git Bash had no gate at all.** Claude Code runs a shell-form
  hook under Git Bash on Windows, or PowerShell when Git Bash is absent — so
  `bash "..."` never started there, and a hook that fails to launch is treated
  as non-blocking. `scripts/gate-hook.ps1` now covers that case. Hooks have no
  platform condition, so both entries fire and the PowerShell one defers only
  when Git Bash is *positively* confirmed (by installation, not `PATH`; a WSL
  bash in `System32` does not count). When unsure it runs — a duplicate review
  is cheaper than an unreviewed push.
- **The push matcher missed wrapped commands.** `Bash(git push:*)` fires for
  plain, compound (`cd x && git push`), env-prefixed and `;`-separated commands,
  but not for `command git push` or `bash -c "git push"`. Now `Bash(*git push*)`,
  which catches those and stays selective.
- **The gate could create a stray `.git/` directory.** `_git_dir()` asked for
  `--git-dir`, which answers the bare relative string `".git"` when cwd is the
  repo root; callers resolved that against the process cwd (in hook mode,
  wherever Claude Code was launched from) and then `mkdir -p`'d it. It now uses
  `--absolute-git-dir`, anchors on the repo root, and returns empty outside a
  repo.
- **Every push wrote `.pyc` files into the installed plugin snapshot**, a
  directory the plugin manager treats as immutable and `sync-local-install.py`
  diffs for drift. `sys.dont_write_bytecode` is set before the sibling import.
- **CI had been red on `main` since `69a4452`** — the verdict smoke test still
  asserted the pre-`_is_actionable` answer. The fixture was stale, not the code.
  CI now also runs the test suite (which it never did) across a 3.9/3.12 matrix,
  and validates both manifests.
- `.gitattributes` pinned `bin/pre-commit`, renamed to `pre-push` back in
  `6aaf562`. The catch-all still normalized it, so the explicit CRLF protection
  on the repo's most CRLF-fragile file had been quietly absent.
- Corrected the fail-open documentation in five places, and the module
  docstring's `OCR_TIMEOUT` default (600s → the actual 1800s).

### Changed
- **BREAKING: executables moved from `bin/` to `scripts/`.** A plugin's `bin/`
  is added to the Bash tool's `PATH`, so every file in it became a bare command
  in every Bash call for every user with the plugin enabled — including
  `pre-push` (a template that resolves an unsubstituted placeholder, reviews
  nothing, and exits 0) and `sync-local-install.py` (a dev tool that writes into
  `~/.claude/plugins`). None were meant to be typed.
  - **If you installed the global git hook, re-run
    `bash scripts/install-git-hook.sh`.** You do not have to: the materialized
    hook no longer trusts a baked path, and a compatibility shim remains at
    `bin/review-gate.py` (removal target: **0.5.0**). Re-running just gives you
    a clean install. `/review-gate:doctor` reports whether yours is stale.
- **The global git hook resolves the reviewer at runtime.** It previously baked
  an absolute path into a file outside the repo that no upgrade rewrites, so a
  plugin upgrade — including this one — orphaned it, and an orphaned path meant
  the push went through unreviewed. It now checks `$SCR_GATE_DIR`, then a
  pointer written under `${CLAUDE_PLUGIN_DATA}` (which survives updates, unlike
  the versioned cache dir), then the install-time hint, and **blocks with repair
  instructions** if it finds nothing. This also retires the version-skew hazard
  `CONTRIBUTING.md` has warned about.

### Added
- **`/review-gate:doctor`** — reports whether the gate is actually wired up and
  able to run. Needed because a hook that fails to launch, times out, or dies
  abnormally is non-blocking and cannot detect its own absence.
- `displayName` in the plugin manifest, and a `pyproject.toml` pinning the lint
  and test configuration CI already enforces.

## [0.2.1] - 2026-08-14

### Fixed
- **An incomplete finding can no longer block a push on its own.**
  `compute_verdict` decided block/warn/pass from severity + confidence alone,
  with no check that a finding carries anything a human can act on. In practice
  findings have come back with `severity`/`confidence`/`path` but no
  `content`/`start_line`/`end_line` — the reviewing LLM dropping fields while
  retyping findings through the filter/dedup/hallucination-check steps. Those
  blocked a push while telling the developer nothing. `_is_actionable()` is now
  required alongside the severity/confidence check before returning `block`; an
  incomplete finding still counts toward `warn` and stays fully visible. The
  skill's aggregation step is also hardened to carry every field forward
  unchanged rather than retyping findings by hand.
- **Raw reviewer output is persisted, and incomplete findings say so.**
  `_format_reasons` used to render a field-less finding as a bare `path:? - `
  with nothing after the dash, which reads as display truncation rather than a
  defect in the review — with no way to see what the reviewer actually said
  short of re-running it. Every run now dumps `claude`'s raw stdout to
  `<git-dir>/review-gate-last-output.json` (inside `.git`, never tracked),
  referenced from both the block and advisory messages.
- **The verdict JSON is parsed by a balanced-brace scan, not a first/last span.**
  `json.loads(text[find("{"):rfind("}")+1])` grabbed the *last* `}` anywhere in
  the model's output, including one in trailing prose appended after the verdict
  — turning a valid verdict into an unparseable-output failure. Now
  `JSONDecoder.raw_decode` runs from each `{` in turn, keeping the last dict
  carrying `findings` as the answer.
- **A failed `claude` invocation is no longer reported as a bad review.**
  `_run_review` never checked the subprocess exit code, so every failure mode
  collapsed into "could not parse review output" — which points at the review
  skill when the actual fault is the CLI. An expired login is the common case:
  `claude` prints `Failed to authenticate: OAuth session expired` to **stdout**,
  where the gate expects JSON. The gate now reports the exit code, which binary
  ran, and its output, and recognizes credential failures specifically. Note a
  Claude Code Desktop session refreshes its auth in-process, so the app keeps
  working while the on-disk credentials the CLI reads go stale — the gate breaks
  with no visible sign anything logged out. Still fails CLOSED: an unusable
  login must not become a silent bypass.

### Added
- **`bin/sync-local-install.py`** — refreshes the local plugin snapshot from the
  working tree. When the plugin is installed from a `directory` marketplace,
  Claude Code runs a version-pinned *copy* under `~/.claude/plugins/cache/`, not
  your checkout, and `${CLAUDE_PLUGIN_ROOT}` resolves there — so committing a fix
  changes nothing the gate enforces until the snapshot is refreshed, with no
  warning that the two have diverged. `--check` reports drift (exit 1), `--prune`
  drops superseded snapshots. The global git hook is unaffected: it bakes in an
  absolute path to `bin/` and always runs live code, so the two gates can
  silently disagree about which version is in force.
- `tests/test_review_gate.py` — unit tests for the `_extract_json` balanced-brace
  parser: the trailing-prose stray-brace case that motivated the fix, plus
  whole-string JSON, fenced blocks, unrelated JSON without `findings`,
  last-match-wins, and no-JSON/malformed/empty inputs.

## [0.2.0] - 2026-08-10

### Changed — BREAKING
- **The gate is now pre-push, not pre-commit.** It reviews every unpushed commit
  once per push (`git diff @{u}..HEAD`) instead of reviewing staged changes on
  every commit. A 10-commit push is one review.
- **`bin/install-git-hook.sh` now installs `pre-push`** (`bin/pre-push` replaces
  `bin/pre-commit`). **Re-run the installer if you installed before this
  release**: the old `pre-commit` hook still fires on every commit, and because
  the gate now reviews `@{u}..HEAD` it reviews the *wrong* state at commit time
  — during a pre-commit hook the new commit does not exist yet, so `HEAD` is
  still its parent. The installer detects and removes the stale hook.
- **The gate fails CLOSED**, not open: a timeout, crash, or unparseable review
  now blocks rather than allowing the push. Only a missing `claude` binary still
  fails open. `OCR_FAIL_OPEN=1` is the emergency bypass. README and
  `hooks/hooks.json` previously documented the old fail-open behavior.
- `OCR_TIMEOUT` default raised 600s → 1800s; the `hooks.json` harness timeout
  must stay above it (currently 1920s).

### Added
- **Cost isolation for the headless review session.** It now pins its model
  (`OCR_MODEL`, default `sonnet`) instead of inheriting the parent session's,
  loads only project settings (`--setting-sources project`, so global hooks do
  not fire on every tool call), loads the plugin from disk (`--plugin-dir`),
  connects no MCP servers (`--strict-mcp-config`), and keeps per-machine
  sections out of the cached prefix. See the README's Cost section.
- `OCR_CLAUDE_EXTRA_ARGS` — appends to the default `claude` flags.
  `OCR_CLAUDE_ARGS` still replaces them wholesale, which discards the cost
  controls; the bundled git hook now uses the appending form.
- Per-language review rules (`skills/review/rules/{python,typescript,go,rust}.md`)
  and `rules/llm-authored-code.md`, appended to the rubric per file.
- Independent falsify pass: a `code-filter` subagent with a fresh context gets
  only the diff and the findings and drops a finding solely on direct
  counter-evidence. Runs only when a finding would actually block.
- `evidence` field on findings; cross-file claims now require cited `Grep`
  results or the orchestrator drops them.
- Hallucination check: a finding whose `existing_code` is absent from the diff
  and the file has its confidence downgraded.
- `bin/uninstall-git-hook.sh` now removes the hook files it installed (both
  `pre-push` and a legacy `pre-commit`), not just the `core.hooksPath` setting.

### Changed
- Single reviewer for the whole change set instead of one subagent per file, so
  cross-file defects are visible. Diffs over 15 files fan out by directory.

### Added
- Extended file-type allowlist to match OCR v1.7.11–v1.7.13: FreeMarker
  (`.ftl`, `.ftlh`, `.ftlx`), gettext translation files (`.po`, `.pot`),
  Astro (`.astro`), fish shell (`.fish`), Python stubs (`.pyi`),
  Objective-C (`.m`, `.mm`), VB.NET/F# (`.vb`, `.fs`), Erlang headers/ETS
  (`.hrl`, `.ets`), Ruby build files (`.rake`, `.gemspec`), and `.htm`.
- Sample FreeMarker and PO/POT checklists in `examples/.ocr/rule.json`.

## [0.1.0] - 2026-06-29

### Added
- Orchestrator skill `/review-gate:review` — diff review, `--staged`,
  full-file `--scan`, `--json`, `--rule`, `--summary`.
- Per-file `code-reviewer` subagent (isolated context, parallel fan-out) that reads
  real files, folds plan → main → "falsify, don't verify" in-session, and emits a
  severity/confidence finding schema.
- Commit gate: default Claude Code PreToolUse hook, plus an optional global git hook
  (`bin/install-git-hook.sh`) for every-commit-everywhere coverage.
- Deterministic verdict parser (`bin/ocr_verdict.py`) and fail-open gate core
  (`bin/review-gate.py`).
- Rule hierarchy, allowlist/exclusions, finding JSON Schema, sample `.ocr/rule.json`.
- Apache-2.0 license with NOTICE attributing open-code-review.

[Unreleased]: https://github.com/Jose-Ribeir/claude-code-review-gate/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/Jose-Ribeir/claude-code-review-gate/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Jose-Ribeir/claude-code-review-gate/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Jose-Ribeir/claude-code-review-gate/releases/tag/v0.1.0
