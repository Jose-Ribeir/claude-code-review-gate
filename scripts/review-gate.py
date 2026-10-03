#!/usr/bin/env python3
#
# review-gate — commit gate core.
#
# Runs the review skill headlessly via the official `claude` CLI (the compliant,
# subscription-friendly path — no token leaves Claude Code, no third-party tool),
# parses the JSON verdict, and converts it into either:
#   --mode hook : a Claude Code PreToolUse permissionDecision (deny/allow) on stdout
#   --mode git  : a process exit code (1 = block, 0 = allow)
#
# A third mode reports rather than decides:
#   --mode post : a Claude Code PostToolUse additionalContext payload on stdout,
#                 replaying the record an earlier review already wrote. This is
#                 the ONLY channel that puts non-blocking findings in front of
#                 the model -- see _mode_post for why the obvious ones do not.
#                 It reports on the push itself when that push succeeded, and
#                 on any later tool call when it did not (see _flush_pending).
#
# Failure policy: FAIL CLOSED on timeout, subprocess error, or unparseable
# output — and, as of 0.3.0, on a missing Python 3 or a reviewer the git hook
# cannot locate (both used to fail open, contradicting this very paragraph).
#
# Since 0.6.0 the review does not run INSIDE the hook. It runs under a
# detached supervisor (--mode supervise) that the hook only joins, for at most
# OCR_INLINE_BUDGET seconds; past that the hook DENIES with "still running,
# re-run the push" and the retry joins the same review. See ASYNC_DIR for
# why: the desktop app kills a CLI that stays silent for ~16 minutes, and a
# hook is silent for as long as it runs.
#
# What still fails OPEN, in full:
#   1. "claude not found" — deliberate; there is no sensible gate without it.
#   2. The hook failing to LAUNCH, or the join loop itself outliving
#      hooks/hooks.json's timeout (900 s; the budget is clamped to 840 so it
#      cannot). Claude Code treats a hook it could not start or had to kill
#      as non-blocking, and no code in here can override that. It is why
#      /review-gate:doctor exists.
#   3. OCR_FAIL_OPEN=1 (one-shot bypass) / OCR_ADVISORY=1 (permanent warn-only).
#
# Raise OCR_TIMEOUT (default 1800 s) if legitimate reviews routinely time out.
# It is enforced by the supervisor, outside the hook, so hooks/hooks.json's
# timeout must NOT follow it up: that one has to stay under the host's wall.
#
# The orchestrated review methodology this drives is adapted from open-code-review
# (ocr): https://github.com/alibaba/open-code-review (Apache-2.0). See NOTICE.
import hashlib
import json
import os
from collections import deque
import re
import shutil
import subprocess
import sys
import threading
from concurrent import futures as _futures
import shlex
import time
from pathlib import Path

# Import the verdict logic from its sibling WITHOUT leaving a __pycache__ behind.
# The plugin runs from ~/.claude/plugins/cache/<...>/<version>/, which the plugin
# manager treats as an immutable snapshot and which sync-local-install.py diffs
# for drift; writing .pyc files into it on every push dirties both. The process
# is short-lived, so losing bytecode caching costs nothing measurable.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ocr_verdict import compute_verdict  # noqa: E402
import ocr_impact  # noqa: E402
import ocr_segment  # noqa: E402
import ocr_telemetry  # noqa: E402

PROMPT = "/review-gate:review --unpushed --json"
# With an explicit range the skill reviews exactly what the remote is about to
# gain, instead of re-deriving `@{u}..HEAD` and reaching the same wrong answer
# the gate used to.
PROMPT_RANGE = "/review-gate:review --range {rng} --json"

# The plugin's own root (scripts/.. == the plugin dir). Passed explicitly so the
# review skill still resolves when we skip user settings below.
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Model for the headless review session. Pinned deliberately: without --model
# the spawned session inherits whatever model the PARENT Claude Code session is
# on, so a user on Opus pays Opus cache-read rates ($0.50/M) for every gate run
# -- ~5x Haiku and ~1.7x Sonnet, on a workload that re-reads its whole context
# on every tool call. Sonnet is the default because review quality matters (a
# gate that emits false positives gets bypassed, and a bypassed gate has zero
# recall); set OCR_MODEL=haiku to trade some precision for cost, or =opus if you
# want maximum depth and accept the bill.
_MODEL = os.environ.get("OCR_MODEL", "sonnet").strip() or "sonnet"

DEFAULT_CLAUDE_ARGS = [
    # The reviewer's input is an UNTRUSTED diff. Anything it is pre-approved to
    # run is therefore reachable by prompt injection from a hostile branch, so
    # the allowlist is read-only: the review reads code and asks git what
    # changed, and needs nothing else. Each rule is its own argv element -- the
    # documented form is `--allowedTools "Bash(git log *)" "Bash(git diff *)"`,
    # and note the SPACE before `*`, not a colon: a `param:value` rule against
    # Bash's primary `command` field is ignored (with a startup warning) because
    # it would be bypassable by a compound command.
    #
    # Everything outside this list still *exists*, it just is not pre-approved,
    # and a headless session has nobody to prompt -- so it is refused. That is
    # only true while --dangerously-skip-permissions is absent; see pre-push,
    # which used to set it by default and no longer does.
    "--allowedTools",
    "Bash(git diff *)",
    "Bash(git ls-files *)",
    "Bash(git log *)",
    "Bash(git show *)",
    "Bash(git rev-parse *)",
    "Bash(git status *)",
    "Read",
    "Grep",
    "Glob",
    "Task",
    # Belt and braces: a bare tool name removes the tool from the model's
    # context entirely rather than merely denying calls to it. The review skill
    # promises "Never modify files" (skills/review/SKILL.md); this enforces it.
    "--disallowedTools",
    "Write",
    "Edit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    # Pin the model rather than inheriting the parent session's (see above).
    "--model", _MODEL,
    # Load NO settings sources. The user's ~/.claude/settings.json is where
    # global hooks live; in a headless review session those fire on every tool
    # call (each one a subprocess, and any that inject context add tokens to a
    # context that is already re-read on every call).
    #
    # `project` used to be loaded here for that cost reason, but project
    # settings live in the repo BEING REVIEWED -- on a hostile branch they are
    # attacker-controlled, and settings can define hooks, which execute. An
    # empty value loads none of the three (`none` is not a valid source name;
    # the CLI accepts user/project/local only). Auth is unaffected --
    # OAuth/keychain is not a settings source.
    "--setting-sources", "",
    # Load the review plugin from disk. Required because --setting-sources
    # above drops the user-level enabledPlugins registry.
    "--plugin-dir", _PLUGIN_ROOT,
    # No MCP servers. --mcp-config is given an empty object so there is nothing
    # to load; --strict-mcp-config makes that authoritative and ignores every
    # other MCP configuration. A code review needs Bash/Read/Grep/Glob and
    # nothing else, and each connected server's tool schemas cost context.
    "--mcp-config", '{"mcpServers":{}}',
    "--strict-mcp-config",
    # Move per-machine sections (cwd, env, git status) out of the system prompt
    # so the cached prefix stays stable across runs.
    "--exclude-dynamic-system-prompt-sections",
    # Emit NDJSON (one event per turn) instead of the bare last-turn text.
    # This lets _extract_from_stream_json scan EVERY assistant turn for the
    # verdict -- a stray task-notification ack that lands after the verdict
    # becomes a later event, not a replacement, so it can no longer cause a
    # "could not parse review output" failure.
    "--output-format", "stream-json",
    "--verbose",   # required when --output-format stream-json is used in -p mode
]
try:
    TIMEOUT = int(os.environ.get("OCR_TIMEOUT", "1800"))
    if TIMEOUT <= 0:
        TIMEOUT = 1800
except ValueError:
    TIMEOUT = 1800
MARKER_TTL = 3600  # seconds
MARKER_PREFIX = "scr-push-reviewed-"

# --- asynchronous review state (0.6.0) ---------------------------------------
# Why this exists. The Claude desktop app kills a session's CLI process after
# roughly 16 minutes (measured: 976 s) without a stream-json frame while a turn
# is pending, and a PreToolUse hook produces no frames for as long as it runs.
# So every review longer than that killed the CLI -- not the hook: the push
# never ran, the reviewer kept burning tokens as an orphan, its verdict went
# to a dead pipe, and the next resume synthesised "[Request interrupted by
# user for tool use]". Four of five pushes on 2026-09-21 died this way; the
# survivor's hook took 973 s. No timeout in here could engage, because the
# process that would have enforced it was the one being killed.
#
# So the hook must RETURN well inside that wall regardless of how long the
# review takes. The review runs under a detached supervisor (--mode supervise)
# that outlives the hook; the hook waits inline only up to _inline_budget()
# and otherwise DENIES with "still running, re-run the push" -- never allows,
# an unreviewed push is the one thing this gate exists to stop -- and the
# retry joins the same review. State is keyed by the TIP being pushed and
# lives in the repository's common git dir, so two worktrees, two adapters
# or two sessions pushing the same commits share one review.
ASYNC_DIR = "review-gate-async"
_INLINE_BUDGET_DEFAULT = 600
# hooks/hooks.json's PreToolUse timeout is 900 s and the app wall is ~975 s;
# a budget at or above the hooks timeout would let Claude Code kill the hook
# (treated as non-blocking, i.e. fail-open) before the budget deny fires.
_INLINE_BUDGET_MAX = 840
_INLINE_BUDGET_MIN = 30
# Under the Bash tool's 600 s ceiling, for a terminal push made through Claude.
_INLINE_BUDGET_GIT_DEFAULT = 300
POLL_S = 1.0
HEARTBEAT_S = 10
STALE_S = 45          # a supervisor silent this long is presumed dead
LOCK_STALE_S = 60
ATTEMPT_CAP = 2       # automatic restarts of a failed review, per tip, per TTL

# --- chunking / checkpoint (0.7.0) -------------------------------------------
try:
    _CHUNK_THRESHOLD = int(os.environ.get("OCR_CHUNK_THRESHOLD", "15"))
    _CHUNK_THRESHOLD = max(1, _CHUNK_THRESHOLD)
except ValueError:
    _CHUNK_THRESHOLD = 15
try:
    _CHUNK_LINES = int(os.environ.get("OCR_CHUNK_LINES", "1200"))
    _CHUNK_LINES = max(100, _CHUNK_LINES)
except ValueError:
    _CHUNK_LINES = 1200
try:
    _CHUNK_FILES = int(os.environ.get("OCR_CHUNK_FILES", "8"))
    _CHUNK_FILES = max(1, _CHUNK_FILES)
except ValueError:
    _CHUNK_FILES = 8
# --- precomputed diffs (0.10.0) ----------------------------------------------
# Python builds each file's diff and the reviewer READS it (items[] of the
# manifest) instead of the orchestrator retyping every diff into an Agent
# prompt. One definition of the limits, owned here, not by SKILL.md: a file's
# diff is delivered in full up to this many changed lines / bytes, then with no
# context lines (-U0), then as hunk headers only; no line is longer than
# _DIFF_LINE_CAP chars. OCR_PRECOMPUTED_DIFFS=0 restores the 0.9.x path.
_PRECOMPUTED_MAX_LINES = 1500
_PRECOMPUTED_MAX_BYTES = 64 * 1024
_DIFF_LINE_CAP = 500
try:
    # One budget per chunk, in lines of diff (context lines included). It
    # replaces _CHUNK_LINES (changed lines) for packing when diffs are
    # precomputed; a file whose own diff would not fit it degrades first.
    _CHUNK_DIFF_LINES = int(os.environ.get("OCR_CHUNK_DIFF_LINES", "3000"))
    _CHUNK_DIFF_LINES = max(200, _CHUNK_DIFF_LINES)
except ValueError:
    _CHUNK_DIFF_LINES = 3000
try:
    _CHUNK_TIMEOUT = int(os.environ.get("OCR_CHUNK_TIMEOUT", "1200"))
    _CHUNK_TIMEOUT = max(60, _CHUNK_TIMEOUT)
except ValueError:
    _CHUNK_TIMEOUT = 1200
try:
    _RUN_BUDGET = int(os.environ.get("OCR_RUN_BUDGET", "3600"))
    _RUN_BUDGET = max(1, _RUN_BUDGET)
except ValueError:
    _RUN_BUDGET = 3600
try:
    _MAX_FILES = int(os.environ.get("OCR_MAX_FILES", "40"))
    _MAX_FILES = max(1, _MAX_FILES)
except ValueError:
    _MAX_FILES = 40
# --- review ledger (0.8.0) ---------------------------------------------------
LEDGER_DIR = "review-gate-ledger"
# 30 days: a branch picked up again after a week or two keeps its review
# history. The record cap below bounds disk use; the TTL only retires the
# records of branches that have truly gone stale.
try:
    _LEDGER_TTL = int(os.environ.get("OCR_LEDGER_TTL", str(30 * 24 * 3600)))
    _LEDGER_TTL = max(3600, _LEDGER_TTL)
except ValueError:
    _LEDGER_TTL = 30 * 24 * 3600
try:
    _LEDGER_MAX_RECORDS = int(os.environ.get("OCR_LEDGER_MAX_RECORDS", "5000"))
    _LEDGER_MAX_RECORDS = max(100, _LEDGER_MAX_RECORDS)
except ValueError:
    _LEDGER_MAX_RECORDS = 5000
_LEDGER_SCHEMA = 1
# The per-file diff cap of skills/review/SKILL.md §3: past either limit the
# skill degrades that file's diff to stat + hunk headers, so the reviewer saw
# only part of it. The gate re-checks it itself (_diff_exceeds_caps), because the
# skill's own `diff truncated` warning is not always emitted.
_TRUNC_MAX_LINES = 400
_TRUNC_MAX_BYTES = 16 * 1024
# A chunk that timed out is retried in halves (_timeout_marks); a marker older
# than this is forgotten, so a transient stall cannot condemn a file for long.
_TIMEOUT_MARK_TTL = 24 * 3600
# The warning the skill emits for a truncated diff; the gate re-emits it for
# every file whose recorded review was truncated (_surface_truncated).
_TRUNCATED_MSG = "diff truncated; reviewer saw stat + hunk headers only"
# Cost rule: a file with a delta base is reviewed over its whole push range
# instead when that diff is at most this many times the delta's size. It costs
# about the same, and it also shows changes that are only harmful together --
# which a chain of small deltas never shows at once. Replaces a fixed cap on
# the delta chain, which forced a full re-review however large the file.
_COST_RULE_RATIO = 1.5
# Env vars that change the review CRITERIA and so invalidate cached records.
# CLI plumbing (OCR_CLAUDE_ARGS/EXTRA_ARGS) is deliberately absent: it changes
# how the reviewer runs, not what it is asked to find.
_FINGERPRINT_ENV_VARS = (
    "OCR_MODEL", "OCR_BLOCK_SEVERITY", "OCR_BLOCK_CONFIDENCE",
)
# Agents whose prompts decide which findings exist. The resolver only judges
# whether a known finding was fixed, so editing it keeps the ledger.
_FINGERPRINT_AGENTS = ("code-reviewer.md", "code-filter.md")
# -----------------------------------------------------------------------------

PROTOCOL_VERSION = 1   # bumped whenever state-file semantics change

_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
# git exports these to its own hooks (pre-push runs with GIT_DIR set, among
# others). Inherited into a reviewer whose cwd is a detached worktree, they
# would point every git call back at the main tree. Scrubbed from the
# supervisor and the reviewer alike.
_GIT_ENV_SCRUB = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_PREFIX",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
)
# Wall-clock at process start. The inline budget is measured from here, not
# from the moment the join begins: the git calls before it already spent
# seconds of the same hook timeout.
_HOOK_T0 = time.time()


def _inline_budget(mode):
    """Seconds this hook may wait for the review before denying with 'retry'.

    Hook mode: OCR_INLINE_BUDGET (default 600, clamped so it can never reach
    hooks.json's timeout). Git mode: OCR_INLINE_BUDGET_GIT (default 300) when
    stderr is not a terminal -- i.e. the push is running inside Claude's Bash
    tool, whose hard ceiling is 600 s -- and the full reviewer timeout when it
    is, because a human at a terminal can simply wait.
    """
    if mode == "git":
        try:
            if sys.stderr is not None and sys.stderr.isatty():
                return TIMEOUT
        except Exception:
            pass
        name, default = "OCR_INLINE_BUDGET_GIT", _INLINE_BUDGET_GIT_DEFAULT
    else:
        name, default = "OCR_INLINE_BUDGET", _INLINE_BUDGET_DEFAULT
    try:
        val = int(os.environ.get(name, "") or default)
    except ValueError:
        val = default
    return max(_INLINE_BUDGET_MIN, min(_INLINE_BUDGET_MAX, val))

# 0.5.5's in-progress marker. Superseded by the async state file (see
# ASYNC_DIR), which carries a heartbeat instead of a bare timestamp and so can
# tell "still running" from "killed". Kept in _MARKER_PREFIXES for one release
# so the sweep collects the ones 0.5.5 left behind; nothing writes it.
INPROGRESS_PREFIX = "scr-push-inprogress-"

# Markers written by --mode post (see _mode_post). Both follow MARKER_PREFIX's
# discipline -- claimed atomically, swept by _reap_markers on the same TTL --
# and exist only to make a repeated report shut up:
#   scr-post-delivered-*   this exact review was already injected into this
#                          session's context; do not inject it again.
#   scr-hookspath-warned-* this session was already told the git adapter is
#                          shadowed here; it is a static per-repo fact and
#                          repeating it every push trains the reader to skip it.
POST_DELIVERED_PREFIX = "scr-post-delivered-"
HOOKSPATH_WARNED_PREFIX = "scr-hookspath-warned-"

# The pre-0.3.x per-commit marker. Nothing writes it any more, but the sweep
# only ever globbed the prefixes in use, so every one ever written is still
# sitting in .git -- 516 of them in one real repo, one per commit reviewed by
# the old pre-commit gate. Reaping is keyed on mtime, so listing it here
# collects the stragglers on the next push and then costs nothing.
_LEGACY_MARKER_PREFIX = "scr-reviewed-"
_MARKER_PREFIXES = (
    MARKER_PREFIX,
    POST_DELIVERED_PREFIX,
    HOOKSPATH_WARNED_PREFIX,
    _LEGACY_MARKER_PREFIX,
    INPROGRESS_PREFIX,
)

# --mode post limits. The findings log is append-only and never pruned, so the
# scan is bounded from the newest end rather than reading the whole file; the
# context cap keeps one pathological review from flooding the session it is
# reporting into.
POST_SCAN_CAP = 200
POST_FINDING_LIMIT = 10
POST_MAX_CONTEXT = 3000

# A review that has been recorded but not yet reported parks one of these, and
# delivery clears it. It is what unties reporting from the pushing tool call:
# PostToolUse does not fire for a call that FAILED, so a rejected push -- or a
# `git push && gh pr create` whose second half blew up -- used to review the
# commits, write the findings, and tell nobody. Any later Bash call flushes it.
#
# Deliberately NOT in .git, unlike every other marker here: the shell adapter
# has to answer "is anything waiting?" before it knows which repository the
# next tool call is even about, so this has to live at a path it can glob
# without first resolving a repo. It sits beside the breadcrumb, in the one
# directory that survives plugin upgrades.
PENDING_PREFIX = "pending-"

# Appended to whatever else is being reported, never on its own.
_SHADOW_NOTE = (
    "  NOTE: this repo sets its own core.hooksPath, which shadows the global "
    "review-gate git hook - pushes from a plain terminal here are NOT gated."
)

# Where non-blocking findings survive. review-gate-last-output.json is
# overwritten on every run and the push markers held nothing but an epoch
# float, so a warn/pass verdict's findings -- precisely the ones that do NOT
# stop the push, and are therefore the easiest to lose -- became unrecoverable
# the moment the next review started. FINDINGS_LOG is append-only and is never
# pruned by this tool: one JSON line per completed review, kept forever.
FINDINGS_LOG = "review-gate-findings.jsonl"
# Per-run snapshots of claude's raw stdout. Large, and the findings themselves
# already live in FINDINGS_LOG, so this directory IS rotated.
HISTORY_DIR = "review-gate-history"
# Cap on a single FINDINGS_LOG line, so one pathological review cannot turn the
# log into an unreadable multi-megabyte record. The full text stays in
# HISTORY_DIR, and the entry says so via "truncated": true.
_MAX_LOG_LINE = 256 * 1024
DEFAULT_HISTORY_LIMIT = 50


def _history_limit():
    """How many raw-stdout snapshots to keep. OCR_HISTORY_LIMIT=0 keeps all.

    Read per call rather than at import so a caller can change it without
    re-importing, and so the value is testable. Only the verbose snapshots are
    ever rotated -- see _prune_history.
    """
    try:
        n = int(os.getenv("OCR_HISTORY_LIMIT", str(DEFAULT_HISTORY_LIMIT)))
    except ValueError:
        return DEFAULT_HISTORY_LIMIT
    return DEFAULT_HISTORY_LIMIT if n < 0 else n  # negative would delete everything


class ReviewGateError(Exception):
    """Raised to fail the gate closed (timeout, subprocess crash, parse error).

    Within this module, 'claude not found' is the only condition allowed to
    remain fail-open. The adapters add their own (see the module header) and
    Claude Code adds one more that no code here can reach: a hook that fails to
    launch or gets killed is treated as non-blocking.
    """
    def __init__(self, msg="", is_timeout=False, is_parse_failure=False):
        super().__init__(msg)
        self.is_timeout = is_timeout
        self.is_parse_failure = is_parse_failure


class ReviewLimitError(ReviewGateError):
    """Raised when the reviewer hits a session/usage/rate limit.

    Carries an optional resets_at epoch (float) parsed from the output;
    None means unknown, so the gate applies a 15-minute default hold.
    """
    def __init__(self, msg="", resets_at=None):
        super().__init__(msg)
        self.resets_at = resets_at


class ReviewBudgetError(ReviewGateError):
    """Raised when the per-run wall-clock budget is exhausted mid-chunked-review.

    Budget exhaustion is not an attempt: the next push resumes from the
    checkpoint and the attempt counter is left unchanged.
    """


class ReviewChunkTimeout(ReviewGateError):
    """A chunk timed out and the files in it are marked to be retried in halves.

    Like a budget stop it is progress, not an attempt: every retry is smaller,
    and a file that times out on its own twice ends in ReviewUnreviewableError.
    """
    def __init__(self, msg=""):
        super().__init__(msg, is_timeout=True)


class ReviewUnreviewableError(ReviewGateError):
    """A single file timed out twice on its own: it cannot be reviewed within
    OCR_CHUNK_TIMEOUT. Terminal for this tip -- retrying the same call is a loop."""


class _Fenced(Exception):
    """Raised when _update_state_owned detects the run_id changed.

    A supervisor that catches this exits without writing anything -- a newer
    run owns the tip and must not be overwritten.
    """


def _warn(msg):
    sys.stderr.write("[review-gate] " + msg + "\n")


def _git(args, cwd=None):
    try:
        out = subprocess.run(
            # encoding is explicit for the same reason as in _run_review: git
            # emits UTF-8 (branch names, paths), text=True alone would decode
            # it with the locale's codepage.
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            # The supervisor runs DETACHED_PROCESS, with no console: on Windows
            # every console child it starts without this flag gets a window of
            # its own, one per git call -- dozens per review.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return out.stdout.strip(), out.returncode
    except Exception:
        return "", 1


def _repo_root():
    out, rc = _git(["rev-parse", "--show-toplevel"])
    return out if rc == 0 and out else os.getcwd()


def _git_dir(repo_root=None):
    """Absolute path to the git dir, or "" when we are not in a repo.

    Two things matter here. It asks for --absolute-git-dir rather than
    --git-dir, because the latter answers the bare relative string ".git" when
    cwd happens to be the repo root -- and every caller then resolves that
    against the PROCESS cwd, which in hook mode is wherever Claude Code was
    launched from, not the repo. And it returns "" on failure rather than
    falling back to ".git": _save_raw_output mkdir -p's whatever it is given,
    so the old fallback would CREATE a bogus .git directory in the cwd of any
    non-repo the gate ran in. A gate that promises to only read must not
    scatter directories around.
    """
    out, rc = _git(["rev-parse", "--absolute-git-dir"], cwd=repo_root)
    return out if rc == 0 and out else ""


def _git_common_dir(repo_root=None):
    """Absolute path of the repository's COMMON git dir, or "".

    `--absolute-git-dir` answers the per-worktree private dir, so two worktrees
    of one repository pushing the same commits would each run their own review.
    The async state is keyed by tip and belongs to the repository, so it lives
    in the directory all worktrees share. `--path-format=absolute` needs git
    2.31; older gits answer a path relative to cwd, which is resolved here.
    """
    out, rc = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repo_root)
    if rc != 0 or not out:
        out, rc = _git(["rev-parse", "--git-common-dir"], cwd=repo_root)
        if rc != 0 or not out:
            return ""
        if not os.path.isabs(out):
            out = os.path.normpath(os.path.join(repo_root or os.getcwd(), out))
    return out


def _head_sha(repo_root=None):
    out, rc = _git(["rev-parse", "HEAD"], cwd=repo_root)
    return out if rc == 0 and out else ""


def _branch(repo_root=None):
    """Current branch name, or "" (detached HEAD, or not a repo).

    Recorded with each review so the findings log can be read months later
    without having to work out which branch a bare sha belonged to.
    """
    out, rc = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root)
    return out if rc == 0 and out and out != "HEAD" else ""


_ZERO_SHA = "0" * 40
# Sentinel: several branches gain commits in one push, which no single
# revision range can express. Callers deny rather than review a subset.
_MULTI_REF = "\x00multi-ref"


def _read_push_refs():
    """The ref updates git feeds a pre-push hook on stdin, as (local, remote).

    Format per line: `<local ref> <local sha> <remote ref> <remote sha>`.
    This is the AUTHORITATIVE answer to "what is being sent, and where" -- and
    the gate used to throw it away, asking `@{u}..HEAD` instead. Those diverge
    the moment you push to a ref that is not your branch's upstream:
    `git push origin mybranch:main` with mybranch already pushed reports zero
    unpushed commits, so the gate allowed five commits onto main having
    reviewed none of them. Observed in a real repo, not hypothesised.

    Deletions (local sha all-zero) are skipped: there is no content to review.
    Returns [] when stdin is empty or unreadable, which puts callers back on
    the old heuristic rather than failing.
    """
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return []
        raw = sys.stdin.read()
    except Exception:
        return []
    refs = []
    for line in (raw or "").splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts
        if local_sha == _ZERO_SHA:
            continue  # branch deletion: no content to review
        if remote_ref and not remote_ref.startswith("refs/heads/"):
            # Tags and other non-branch refs. A `--follow-tags` push carries
            # them alongside the branch, and their commits are already covered
            # by it; counting them would turn ordinary pushes into multi-ref
            # ones for no gain.
            continue
        refs.append((local_sha, remote_sha))
    return refs


def _range_for_refs(refs, repo_root=None):
    """The revision range actually being pushed, or "" when nothing is.

    `<remote sha>..<local sha>` per ref update -- exactly what the remote is
    about to gain. A brand-new remote ref has an all-zero remote sha and no
    such base, so it falls back to the same default-branch heuristic the skill
    uses; that is a guess, but a guess about a NEW branch, not a silent skip.
    """
    found = []
    for local_sha, remote_sha in refs:
        # The remote sha is whatever the REMOTE reported during negotiation,
        # and the client only needs the objects it must send -- so that commit
        # may not exist locally at all. `git log <missing>..<local>` then fails
        # with "unknown revision", and treating a failure as "no commits" would
        # skip the review exactly when we are least sure. Confirm the base is
        # present before using it, and fall back when it is not.
        base = ""
        if remote_sha != _ZERO_SHA:
            _o, _rc = _git(["cat-file", "-e", remote_sha + "^{commit}"], cwd=repo_root)
            if _rc == 0:
                base = remote_sha
        if not base:
            for cand in ("origin/HEAD", "origin/main", "origin/master"):
                out, rc = _git(["rev-parse", "--verify", "--quiet", cand], cwd=repo_root)
                if rc == 0 and out:
                    base = cand
                    break
        if not base:
            continue
        rng = base + ".." + local_sha
        out, rc = _git(["log", rng, "--oneline"], cwd=repo_root)
        if rc != 0:
            # Still unevaluable. Assume it carries commits: over-reviewing
            # costs a review, under-reviewing costs the gate.
            found.append(rng)
        elif out.strip():
            found.append(rng)
    if not found:
        return ""
    if len(found) > 1:
        # More than one branch is gaining commits in a single push. There is no
        # single `A..B` that expresses that, and reviewing just one of them
        # would leave the rest unreviewed -- the very fail-open this function
        # exists to close. Say so and let the caller refuse.
        return _MULTI_REF
    return found[0]


def _has_unpushed_commits(repo_root=None, push_range=None):
    """Is there anything to review for this push?

    When the caller knows the range being pushed (git mode, from the pre-push
    refs), that is the answer -- it describes what the REMOTE is about to gain.
    Only without it does this fall back to asking whether HEAD is ahead of its
    own upstream, which is a different question and answers "no" for a
    `branch:main` push whose branch was already pushed.

    repo_root is not optional in spirit either: without it this asked the
    PROCESS cwd, which in hook mode is wherever Claude Code was launched from.
    """
    if push_range and push_range != _MULTI_REF:
        out, rc = _git(["log", push_range, "--oneline"], cwd=repo_root)
        if rc == 0:
            return bool(out.strip())
    for ref in ("@{u}", "origin/main", "origin/master", "origin/HEAD"):
        out, rc = _git(["log", f"{ref}..HEAD", "--oneline"], cwd=repo_root)
        if rc == 0:
            return bool(out.strip())
    return True  # unknown -> let the reviewer decide


def _is_advisory(repo_root):
    if os.environ.get("OCR_ADVISORY", "").strip().lower() in ("1", "true", "yes"):
        return True
    for name in (".ocr/config.json", ".ocr/config"):
        p = Path(repo_root) / name
        if not p.exists():
            continue
        try:
            txt = p.read_text(encoding="utf-8")
            if name.endswith(".json"):
                if json.loads(txt).get("blocking") is False:
                    return True
            elif "blocking" in txt and "false" in txt.lower():
                return True
        except Exception:
            continue
    return False


def _write_gate_pointer():
    """Record where this plugin's scripts/ dir currently lives.

    The global git hook is copied to ~/.config/review-gate/hooks/pre-push once
    at install time and never updated, so it cannot know where the plugin moved
    to after an upgrade -- the cache dir is versioned, and 0.3.0 renamed bin/ to
    scripts/ on top of that. It reads this pointer instead of a baked path.

    ${CLAUDE_PLUGIN_DATA} is the right home for it: it survives plugin updates,
    unlike the versioned cache. When that is not set (a --plugin-dir dev
    install), fall back to a slot under the config dir keyed the same way.

    Best-effort throughout: this is housekeeping and must never break the gate.
    """
    try:
        data_dir = os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
        if not data_dir:
            cfg = os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or os.path.join(
                os.path.expanduser("~"), ".claude"
            )
            data_dir = os.path.join(cfg, "plugins", "data", "review-gate-local")
        target = Path(data_dir)
        target.mkdir(parents=True, exist_ok=True)
        here = os.path.dirname(os.path.abspath(__file__))
        ptr = target / "gate-dir"
        # Avoid a pointless write (and mtime churn) when nothing moved.
        if ptr.exists() and ptr.read_text(encoding="utf-8").strip() == here:
            return
        ptr.write_text(here, encoding="utf-8")
    except Exception:
        pass


def _in_review():
    """True when this process is running inside the headless review session.

    _run_review sets OCR_IN_REVIEW=1 in the child environment; the child is
    given --plugin-dir, so this plugin's push gate is registered there too.
    """
    return os.environ.get("OCR_IN_REVIEW", "").strip().lower() in ("1", "true", "yes")


# Longest finding description we will echo back. Long enough for a real finding,
# short enough that a hostile diff cannot flood the parent session's context.
_MAX_CONTENT = 500


def _sanitize(text, limit=_MAX_CONTENT):
    """Make reviewer-supplied text safe to echo into the parent session.

    Everything in a finding originates in the diff under review, which on a
    hostile branch is attacker-controlled -- and in hook mode this text is
    placed in permissionDecisionReason, i.e. injected straight into the
    CALLING session's context. Strip control characters (ANSI escapes, CR, and
    embedded newlines that would let one finding forge extra report lines) and
    cap the length.
    """
    s = str(text)
    s = "".join(ch if ch.isprintable() else " " for ch in s)
    s = " ".join(s.split())
    if len(s) > limit:
        # -3, not -1: the marker is "..." since 0.3.4. A single "…" mojibakes
        # to a replacement character on Windows, where this text reaches a
        # terminal through git's stderr in --mode git.
        s = s[: limit - 3].rstrip() + "..."
    return s


def _raw_output_path(git_dir):
    return Path(git_dir) / "review-gate-last-output.json"


def _findings_log_path(git_dir):
    return Path(git_dir) / FINDINGS_LOG


def _history_dir(git_dir):
    return Path(git_dir) / HISTORY_DIR


def _save_raw_output(git_dir, text, head_sha="", tag=""):
    """Best-effort dump of claude's raw stdout. Returns the archived filename.

    A finding can be syntactically valid JSON yet still be missing fields the
    reviewer was told to always include (e.g. start_line/content) --
    _format_reasons then has nothing to show but "?" placeholders for that
    entry. Keeping the untouched raw output around lets a blocked user inspect
    what the reviewer actually said instead of re-running the whole review
    from scratch just to see full detail.

    Two copies are written: the stable review-gate-last-output.json path (still
    overwritten every run, still what the block message points at) and a
    timestamped snapshot under HISTORY_DIR, because the stable path alone meant
    one push destroyed the previous push's evidence.
    """
    if not git_dir:
        return ""
    try:
        Path(git_dir).mkdir(parents=True, exist_ok=True)
        _raw_output_path(git_dir).write_text(text or "", encoding="utf-8")
    except Exception:
        pass
    return _archive_raw_output(git_dir, text, head_sha, tag)


def _archive_raw_output(git_dir, text, head_sha="", tag=""):
    """Write one timestamped snapshot of the raw output. Returns its filename.

    Named <UTC stamp>-<sha7>.json so the file sorts chronologically and can be
    matched back to the FINDINGS_LOG entry that references it. Best-effort:
    archiving is bookkeeping and must never break the gate.
    """
    if not git_dir:
        return ""
    try:
        d = _history_dir(git_dir)
        d.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        sha = (head_sha or "nohead")[:7] + re.sub(r"[^A-Za-z0-9-]", "", tag or "")
        data = (text or "").encode("utf-8")
        # Claim the name and commit to it in ONE step. The obvious spelling --
        # while (d / name).exists(): name = next_one -- is check-then-act: the
        # two adapters can archive the same HEAD inside the same UTC second,
        # both see the same name free, and one snapshot then overwrites the
        # other, which is the exact collision this loop exists to prevent.
        # O_CREAT|O_EXCL makes the filesystem arbitrate instead.
        name = f"{stamp}-{sha}.json"
        n = 2
        while True:
            try:
                # 0o666 explicitly: os.open defaults to 0o777, which would
                # leave these snapshots executable on POSIX (0o755 under the
                # usual umask) while every sibling artifact this tool writes
                # goes through Python's io layer and lands at 0o644.
                fd = os.open(str(d / name), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
                break
            except FileExistsError:
                if n > 100:
                    return ""  # something is very wrong; do not spin
                name = f"{stamp}-{sha}-{n}.json"
                n += 1
        with os.fdopen(fd, "wb") as fh:  # fdopen owns the fd and closes it
            fh.write(data)
        _prune_history(d)
        return name
    except Exception:
        return ""


def _prune_history(dirpath):
    """Keep only the newest _history_limit() raw snapshots (0 == keep all).

    Rotation applies to the verbose stdout dumps ONLY. The findings extracted
    from them live in FINDINGS_LOG, which is never pruned -- silently dropping
    findings is the exact failure this whole mechanism exists to prevent.
    """
    limit = _history_limit()
    if not limit:
        return
    try:
        files = []
        for path in Path(dirpath).glob("*.json"):
            try:
                files.append((path.stat().st_mtime, path))
            except OSError:
                continue  # vanished under a concurrent gate run
        files.sort(reverse=True)
        for _, stale in files[limit:]:
            try:
                stale.unlink()
            except OSError:
                continue
    except Exception:
        pass  # housekeeping must never break the gate


def _record_review(git_dir, head_sha, branch, mode, verdict, advisory, blocked, result, raw_name=""):
    """Append this review to FINDINGS_LOG. Returns the log path, or None.

    Written for EVERY completed review, blocking or not, because the
    non-blocking ones are the ones nothing else keeps: a warn/pass verdict lets
    the push through, prints its findings once to a stderr stream nobody
    re-reads, and is then overwritten in review-gate-last-output.json by the
    next run. Findings are stored verbatim (JSON-encoded, so control characters
    cannot escape the line); readers sanitize at print time.

    One line, one buffered append per process, so concurrent adapters interleave
    records rather than corrupting each other's.
    """
    if not git_dir:
        return None
    try:
        findings = result.get("findings", []) if isinstance(result, dict) else []
        if not isinstance(findings, list):
            findings = []
        entry = {
            "ts": time.time(),
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "head": head_sha,
            "branch": branch,
            "mode": mode,
            "verdict": verdict,
            "advisory": bool(advisory),
            "blocked": bool(blocked),
            "finding_count": len(findings),
            "findings": findings,
            "truncated": False,
            "raw": f"{HISTORY_DIR}/{raw_name}" if raw_name else "",
        }
        unreviewed = result.get("unreviewed_truncated") if isinstance(result, dict) else None
        if isinstance(unreviewed, list) and unreviewed:
            entry["unreviewed_truncated"] = len(unreviewed)
        # Shed findings until the line fits, rather than truncating the string
        # and leaving unparseable JSON behind. The dropped detail is still in
        # the raw snapshot this entry points at.
        line = ""
        for keep in (len(findings), 5, 0):
            entry["findings"] = findings[:keep]
            entry["truncated"] = keep < len(findings)
            line = json.dumps(entry, ensure_ascii=False, default=str)
            if len(line) <= _MAX_LOG_LINE:
                break
        path = _findings_log_path(git_dir)
        Path(git_dir).mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return path
    except Exception:
        return None


def _read_history(git_dir, limit=10):
    """Return the last `limit` recorded reviews (0 == all), oldest first.

    A malformed line is skipped, never fatal: the log is append-only and may
    have been half-written by a killed process, and a broken tail must not
    hide the intact records before it.
    """
    if not git_dir:
        return []
    entries = []
    try:
        with _findings_log_path(git_dir).open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
    except OSError:
        return []
    return entries[-limit:] if limit else entries


def _marker_path(git_dir, head_sha):
    """Path of the "this push was already reviewed" marker.

    Keyed on HEAD sha alone, which is narrower than it looks: it identifies the
    COMMITS, not the push. Reviewing HEAD and then pushing a different ref that
    resolves to the same HEAD within MARKER_TTL skips the second review. That is
    the intended behaviour -- the same commits do not need reviewing twice, and
    it is what lets the two adapters avoid double-reviewing one push -- but it
    does mean the marker is not a per-remote or per-ref record.
    """
    return Path(git_dir) / f"{MARKER_PREFIX}{head_sha}"


def _marker_fresh(path):
    try:
        return path.exists() and (time.time() - path.stat().st_mtime) < MARKER_TTL
    except Exception:
        return False


def _write_marker(marker, head_sha, verdict, advisory, reasons):
    """Record the marker AND what the review that wrote it found.

    The marker used to hold a bare epoch float. That was enough to skip the
    duplicate review, but it meant the paired adapter's short-circuit dropped
    the findings on the floor -- they were shown exactly once, by whichever
    process happened to run first. Freshness still comes from the file's mtime,
    so the payload costs nothing; markers written by older versions hold a bare
    float and still parse (see _read_marker).
    """
    payload = {
        "ts": time.time(),
        "head": head_sha,
        "verdict": verdict,
        "advisory": bool(advisory),
        "reasons": reasons or "",
    }
    marker.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_marker(marker):
    """Payload of the run that wrote this marker; {} for legacy/unreadable ones.

    Markers written by earlier versions hold a bare epoch float, and a marker
    is not a trusted store either way -- anything that does not parse as an
    object is treated as "no recorded findings" rather than as an error.
    """
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _prior_findings_note(prior):
    """Re-surface the findings recorded by an earlier review of this HEAD.

    Every line is re-sanitized on the way out: the text was produced by
    _format_reasons (already sanitized) but has since been through a file that
    anything with write access to the git dir could have edited.
    """
    lines = [
        "  " + _sanitize(line, 600)
        for line in str(prior.get("reasons") or "").splitlines()
        if line.strip()
    ]
    if not lines:
        return ""
    verdict = _sanitize(prior.get("verdict", "?"), 20)
    return (
        f"already reviewed at this HEAD (verdict: {verdict}) - findings from that run:\n"
        + "\n".join(lines)
    )


def _reap_markers(git_dir, keep=None):
    """Delete markers too old to short-circuit anything.

    A marker is named for the HEAD sha it reviewed and is only ever honored
    within MARKER_TTL, but nothing removed the expired ones -- so the git dir
    accumulated one file per passing push, forever. Sweep them whenever a new
    marker is written: self-limiting, and no separate cleanup entry point to
    remember to run.

    Only EXPIRED markers go. A fresh one for some other sha is still load-bearing
    -- the paired adapter may be mid-push against a different HEAD, and deleting
    it would cost a duplicate review rather than save anything.
    """
    try:
        cutoff = time.time() - MARKER_TTL
        paths = []
        for prefix in _MARKER_PREFIXES:
            paths.extend(Path(git_dir).glob(f"{prefix}*"))
        for path in paths:
            if keep is not None and path == keep:
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue  # already gone, or held by a concurrent gate run
    except Exception:
        pass  # housekeeping must never break the gate


def _marker_digest(*parts):
    """Stable short digest for a marker filename.

    Hashed rather than concatenated because one of the parts is the hook
    payload's session_id: it arrives from outside, and nothing guarantees it is
    a safe path component. A digest is fixed-length, separator-free, and cannot
    climb out of the git dir.
    """
    raw = "\x00".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def _claim_marker(path):
    """Create `path` as an empty marker, atomically. True if WE created it.

    check-then-act (`if not path.exists(): path.touch()`) is wrong here for the
    same reason it was wrong in _archive_raw_output: both adapters can run
    against one push, see the file missing, and both report.

    0o666 explicitly -- os.open defaults to 0o777, which would leave these
    executable on POSIX while every sibling artifact this tool writes lands at
    0o644.

    Note which way the error case falls: an existing marker means "already
    said this" and returns False, but a marker we could not WRITE returns True.
    Bookkeeping that fails must not suppress the report -- this whole mode
    exists because findings were being missed, so a duplicate injection is the
    cheap error and silence is the expensive one.
    """
    try:
        os.close(os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666))
        return True
    except FileExistsError:
        return False
    except Exception:
        return True


def _latest_record_for_head(git_dir, head, cap=POST_SCAN_CAP):
    """Newest FINDINGS_LOG entry recorded for `head`, or None.

    Scans from the newest end, holding at most `cap` lines. FINDINGS_LOG is
    append-only and deliberately never pruned, so reading it whole (as
    _read_history does, for a command a human invokes on demand) would grow
    without bound on a long-lived repo -- and the record this wants is by
    construction one of the last few.
    """
    if not git_dir or not head:
        return None
    try:
        with _findings_log_path(git_dir).open("r", encoding="utf-8", errors="replace") as fh:
            tail = deque(fh, maxlen=cap or None)
    except OSError:
        return None
    for line in reversed(tail):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue  # half-written line from a killed process; keep looking
        if isinstance(obj, dict) and obj.get("head") == head:
            return obj
    return None


# `cmd <<MARKER` / `<<'MARKER'` / `<<-MARKER`, opening a heredoc.
#
# This is the ONE piece of the removed command parser that came back, and it
# came back because the simplification broke a real workflow within minutes of
# shipping: a `git commit` whose MESSAGE discussed a cd chain and a push was
# denied as an ambiguous push. In this repo, whose commit messages routinely
# quote commands, that is not an edge case.
#
# A heredoc body is data the command WRITES. The shell never runs it, so
# parsing it as code is simply wrong -- unlike quoted arguments or `bash -c`,
# where the old parser was guessing at intent and kept guessing wrong. That is
# the line: this transformation is decidable, the others were not.
#
# The opener must END its line, bar a redirection or pipe/separator; a body is
# dropped only when a terminator is actually found, since stripping to
# end-of-command would delete the real commands after it.
_HEREDOC = re.compile(
    r"""<<-?\s*(['"]?)([A-Za-z_][A-Za-z0-9_]*)\1(?=\s*(?:[0-9]*[<>|&;]|$))"""
)


def _strip_heredocs(cmd):
    """Drop heredoc BODIES before scanning a command for cds and pushes."""
    if not cmd or "<<" not in cmd:
        return cmd or ""
    lines, out, i = cmd.splitlines(), [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        m = _HEREDOC.search(line)
        if not m:
            continue
        marker = m.group(2)
        j = i
        while j < len(lines) and lines[j].strip() != marker:
            j += 1
        if j >= len(lines):
            continue  # no terminator: not a heredoc we can trust; strip nothing
        i = j + 1  # past the body AND the terminator line
    return "\n".join(out)


# Which repository a push targets, and whether we can be sure.
#
# This replaces ~400 lines of shell parsing -- heredoc stripping, quote
# masking, a shlex tokenizer, five spellings of `bash -c`. That machinery
# existed to make every exotic command WORK; it produced eleven repair
# commits, several of them fail-opens the gate itself caught. In a
# fail-closed tool the right answer to an ambiguous command is not a better
# parser, it is to refuse and say so.
#
# So: resolve the ordinary shapes, and treat everything else as unknown.
# `_gate_repo` returns (repo_root, ambiguous); an ambiguous push is denied
# with an actionable message rather than silently reviewed in the wrong repo.
_CD = re.compile(
    # A quote is a command boundary too: `bash -c "cd /repo && ..."` really does
    # start a command there. The same rule makes a commit message that contains
    # a cd chain read as ambiguous, which blocks. Erring toward doubt is the
    # point -- see _gate_repo.
    "(?:^|[;&|\\n\\r\"']|&&|\\|\\|)\\s*cd\\s+(?:\"([^\"]*)\"|'([^']*)'|([^\\s;&|]+))"
)
# Anything we cannot expand ourselves: `$VAR`, `${VAR}`, `$(cmd)`, backticks.
# `$` was set by the shell running the command, not by ours.
_UNEXPANDABLE = re.compile(r"[$`]")
# A Git Bash (MSYS) or Cygwin spelling of a Windows drive: `/j/codigo/repo`,
# `/cygdrive/j/codigo/repo`. Claude's Bash tool on Windows IS Git Bash, whose
# `pwd` prints paths this way, so Claude routinely pushes as `cd /j/... &&
# git push`. That shell translates the prefix for programs linked against
# its runtime; this hook runs under native Python, which is not one of them.
_MSYS_DRIVE = re.compile(r"^(?:/cygdrive)?/([A-Za-z])(?:/(.*))?$")


def _native_path(raw):
    """A cd target as THIS process's filesystem understands it.

    On Windows a drive-less rooted path such as `/j/codigo/repo` is not the
    drive J: -- it is the directory `j/codigo/repo` off the root of whatever
    drive the process happens to be on, which exists for nobody.
    `os.path.isabs` still says True, so without this the hop re-anchored to a
    directory that is not there and the push was denied as ambiguous. A gate
    that cannot read the shell's own spelling of a real repository is
    blocking the wrong thing.
    """
    if os.name != "nt":
        return raw
    m = _MSYS_DRIVE.match(raw.replace("\\", "/"))
    if not m:
        return raw
    drive, rest = m.group(1).upper(), m.group(2) or ""
    return drive + ":\\" + rest.replace("/", "\\")


def _cd_targets(cmd):
    """Directories a command cds into before it reaches `git push`, in order.

    Bounded at the push COMMAND, not at the first literal occurrence of the
    words. Splitting on the substring truncates at a mere mention -- `echo
    "remember to git push later" && cd /real-repo && git push` would lose the
    real cd, fall back to the session directory, and review the wrong repo
    without saying so. That is the fail-open this whole resolution exists to
    close, so it must not be reintroduced by the thing that simplifies it.
    """
    if not cmd:
        return []
    code = _strip_heredocs(cmd)
    stop = _REAL_PUSH.search(code)
    head = code[: stop.start()] if stop else code
    return [next(g for g in m.groups() if g is not None and g != "") or ""
            for m in _CD.finditer(head)
            if any(g for g in m.groups())]


def _gate_repo(payload):
    """(repo_root, ambiguous) for the push described by a PreToolUse payload.

    The hook's own cwd is the SESSION's directory, not the pushed repo, and
    Claude Code routinely pushes as `cd <repo> && git push`. Reading the
    session dir instead let a push be reviewed in the wrong repo -- and where
    that repo had nothing unpushed, allowed with no review at all.

    Ambiguity is anything this does not resolve literally: an unexpanded
    variable, a command substitution, a `cd -`, a target that is not a
    directory, or a chain that ends somewhere unresolvable. Callers deny on
    it. That is deliberately blunter than the parser it replaced: a heredoc
    or a quoted argument that merely CONTAINS `cd ... && git push` now reads
    as ambiguous and blocks, where before it was silently misrouted. Blocking
    is visible, bypassable, and correct-by-default; misrouting is neither.
    """
    cmd, cwd = "", ""
    if isinstance(payload, dict):
        cmd = (payload.get("tool_input") or {}).get("command", "") or ""
        cwd = str(payload.get("cwd") or "")
    cur, unknown = (cwd or os.getcwd()), False
    # `git -C <dir> push` changes directory for that one command: a final cd,
    # applied after every explicit one. Without it `git -C /repo push` was
    # resolved against the session directory -- and, containing no "git push"
    # substring, was not even routed to this hook until 0.6.0.
    hops = _cd_targets(cmd)
    c_dir = _push_c_dir(cmd)
    if c_dir:
        hops = hops + [c_dir]
    for raw in hops:
        try:
            if raw == "-" or _UNEXPANDABLE.search(raw):
                unknown = True  # cannot follow THIS hop -- but see below
                continue
            t = _native_path(os.path.expanduser(raw))
            if os.path.isabs(t):
                # An absolute hop re-anchors and clears an earlier unknown: it
                # fully determines where we are regardless of what came before,
                # so `cd "$OLDPWD" && cd /srv/repo && git push` is knowable.
                cur, unknown = t, False
            elif unknown:
                continue  # relative to a place we do not know; still unknown
            else:
                cur = os.path.normpath(os.path.join(cur, t))
            if not unknown and not os.path.isdir(cur):
                return "", True
        except Exception:
            return "", True
    if unknown:
        return "", True
    out, rc = _git(["rev-parse", "--show-toplevel"], cwd=cur)
    if rc == 0 and out:
        return out, False
    return "", True


def _fail_open_requested():
    return os.environ.get("OCR_FAIL_OPEN", "").strip().lower() in ("1", "true", "yes")


# `git push` at a COMMAND position rather than anywhere in the string. Kept
# because the adapters trigger on a bare "git push" SUBSTRING -- deliberately
# loose, since over-reviewing is cheap -- while a DENY is held to this stricter
# test. Without it, a grep pattern or commit message that merely mentions a
# push could be blocked for a reason about pushing.
_REAL_PUSH = re.compile(
    # start of string, a command separator, a newline, or a quote
    "(?:^|[;&|\\n\\r\"']|&&|\\|\\|)\\s*"
    "(?:[A-Za-z_][A-Za-z0-9_]*=\\S*\\s+)*"   # env prefixes: FOO=bar git push
    "git\\s+"
    # git's own options, including the two that take a separate value:
    # `-C <dir>` (which _gate_repo honours as a final cd) and `-c k=v`.
    "(?:(?:-[Cc]\\s+(?:\"[^\"]*\"|'[^']*'|\\S+)|-\\S+|\\S+=\\S+)\\s+)*"
    "push\\b"
)
# Same shape, capturing git's global options and the subcommand, for every
# `git <sub>` at a command position -- used to vet what runs BEFORE the push.
_GIT_CMD = re.compile(
    "(?:^|[;&|\\n\\r\"']|&&|\\|\\|)\\s*"
    "(?:[A-Za-z_][A-Za-z0-9_]*=\\S*\\s+)*"
    "git\\s+"
    "((?:(?:-[Cc]\\s+(?:\"[^\"]*\"|'[^']*'|\\S+)|-\\S+|\\S+=\\S+)\\s+)*)"
    "([A-Za-z][A-Za-z0-9-]*)"
)
_GIT_C_DIR = re.compile(r"(?:^|\s)-C\s+(?:\"([^\"]*)\"|'([^']*)'|(\S+))")


def _looks_like_real_push(cmd):
    """True when the command actually invokes `git push`, not merely mentions it."""
    return bool(_REAL_PUSH.search(_strip_heredocs(cmd or "")))


# --- what does this command push? -------------------------------------------
# Hook mode used to review `@{u}..HEAD` of the CHECKED-OUT branch whatever the
# command said: `git push -u origin feat/instagram` while feat/p3 was checked
# out reviewed p3 (observed, record in hand). The command names the ref; read
# it. Everything the parser cannot read literally is refused, not guessed.

# git subcommands that may precede the push in the same command. Read-only
# by construction: anything that can move a ref or change what `<src>`
# resolves to (switch, checkout, commit, rebase, stash, pull, reset,
# update-ref, ...) would let the hook review one tip and git push another.
_PRE_PUSH_ALLOWED = frozenset({
    "status", "log", "diff", "rev-parse", "remote", "fetch", "ls-files",
    "show", "ls-remote", "describe", "rev-list", "merge-base", "cat-file",
    "for-each-ref", "show-ref", "shortlog", "blame", "name-rev", "var",
    "version", "help",
})
# `git branch` is allowed only in its listing forms.
_BRANCH_LIST_FLAGS = ("--show-current", "--list", "-a", "-r", "-v", "-vv", "--all", "--remotes")

# push options that take a separate value (or `=value`), and bare flags.
_PUSH_VALUE_OPTS = frozenset({
    "--repo", "-o", "--push-option", "--receive-pack", "--exec",
    "--force-with-lease", "--signed", "--recurse-submodules",
})
# Of those, the ones whose value is OPTIONAL (bare form is legal).
_PUSH_OPTIONAL_VALUE = frozenset({"--force-with-lease", "--signed", "--recurse-submodules"})
_PUSH_FLAGS = frozenset({
    "-u", "--set-upstream", "-f", "--force", "--force-if-includes",
    "--no-force-if-includes", "--no-force-with-lease", "-n", "--dry-run",
    "--tags", "--follow-tags", "--no-follow-tags", "--all", "--branches",
    "--mirror", "-d", "--delete", "--no-verify", "--verify", "-q", "--quiet",
    "-v", "--verbose", "--progress", "--no-progress", "--porcelain",
    "--prune", "--thin", "--no-thin", "--atomic", "--no-atomic", "-4",
    "--ipv4", "-6", "--ipv6", "--no-recurse-submodules", "--no-signed",
    "--no-tags",
})


def _push_segment(cmd):
    """The text of the push command itself: from `push` to the next separator.

    Quotes are respected so a quoted argument may contain `;` or `&&`.
    Returns (segment, count) where count is how many `git push` commands the
    string contains -- more than one is refused by the caller: the parser
    describes ONE push, and reviewing the first would leave the rest unreviewed.
    """
    code = _strip_heredocs(cmd or "")
    matches = list(_REAL_PUSH.finditer(code))
    if not matches:
        return "", 0
    start = matches[0].end()
    i, n, q = start, len(code), ""
    while i < n:
        ch = code[i]
        if q:
            if ch == "\\" and q == '"':
                i += 2
                continue
            if ch == q:
                q = ""
        elif ch in "\"'":
            q = ch
        elif ch == "&" and i > start and code[i - 1] == ">":
            pass  # `2>&1`: the & belongs to the redirection, not a separator
        elif ch in ";&|\n\r":
            break
        i += 1
    return _drop_redirections(code[start:i]), len(matches)


# `2>&1`, `>out`, `2> /dev/null`, `<in`: shell plumbing around the push, not
# arguments to it. Claude's habitual form is `git push origin main 2>&1`.
_REDIR_TOKEN = re.compile(r"^\d*(?:>>?|<)(?:&\d+|\S*)$")


def _drop_redirections(segment):
    out, skip = [], False
    for tok in segment.split():
        if skip:
            skip = False
            continue
        if _REDIR_TOKEN.match(tok):
            # A bare operator (`>`/`2>`/`<`) takes the NEXT token as its target.
            if re.fullmatch(r"\d*(?:>>?|<)", tok):
                skip = True
            continue
        out.append(tok)
    return " ".join(out)


def _pre_push_git_commands(cmd):
    """git subcommands at a command position BEFORE the push. [] when none."""
    code = _strip_heredocs(cmd or "")
    stop = _REAL_PUSH.search(code)
    head = code[: stop.start()] if stop else code
    found = []
    for m in _GIT_CMD.finditer(head):
        sub = m.group(2)
        tail = head[m.end():m.end() + 80]
        if sub == "branch":
            first = tail.split()[0] if tail.split() else ""
            if first in _BRANCH_LIST_FLAGS:
                continue
        if sub in _PRE_PUSH_ALLOWED:
            continue
        found.append(sub)
    return found


def _push_c_dir(cmd):
    """The `-C <dir>` of the push command itself, or "". A final cd, in effect."""
    code = _strip_heredocs(cmd or "")
    m = _REAL_PUSH.search(code)
    if not m:
        return ""
    c = _GIT_C_DIR.search(code[m.start():m.end()])
    if not c:
        return ""
    return next((g for g in c.groups() if g), "")


def _parse_push(cmd):
    """Describe the ONE push a command performs.

    Returns a dict with `kind` in:
      branch      one branch refspec (src, dst, remote, tags flag)
      delete      only deletions -> nothing to review
      dry_run     --dry-run -> nothing reaches the remote
      tags        --tags / tag refspecs only (checked by _hook_target)
      multi       --all / --mirror / --branches / several refspecs
      multi_push  more than one `git push` in the command
      no_verify   --no-verify (refused: git mode is the backstop)
      unparseable an option or shape this parser does not know
    Unknown `--opt=value` is skipped; an unknown bare option is `unparseable`
    rather than consumed as a remote name -- the safe direction.
    """
    seg, count = _push_segment(cmd)
    if count == 0:
        return {"kind": "unparseable", "reason": "no push command found"}
    if count > 1:
        return {"kind": "multi_push", "reason": f"{count} push commands in one call"}
    try:
        toks = shlex.split(seg, posix=True)
    except ValueError as exc:
        return {"kind": "unparseable", "reason": f"cannot tokenise: {exc}"}
    out = {
        "kind": "branch", "remote": "", "refspecs": [], "src": "", "dst": "",
        "tags": False, "follow_tags": False, "dry_run": False, "delete": False,
        "set_upstream": False, "reason": "",
    }
    positional, i, opts_done = [], 0, False
    while i < len(toks):
        t = toks[i]
        i += 1
        if opts_done or not t.startswith("-") or t == "-":
            positional.append(t)
            continue
        if t == "--":
            opts_done = True
            continue
        if _UNEXPANDABLE.search(t):
            return {"kind": "unparseable", "reason": f"unexpandable option {t!r}"}
        name, has_eq = (t.split("=", 1)[0], "=" in t)
        if name in _PUSH_VALUE_OPTS:
            if not has_eq and name not in _PUSH_OPTIONAL_VALUE:
                if i >= len(toks):
                    return {"kind": "unparseable", "reason": f"{name} needs a value"}
                val = toks[i]
                i += 1
                if name == "--repo":
                    out["remote"] = val
            elif has_eq and name == "--repo":
                out["remote"] = t.split("=", 1)[1]
            continue
        if name in _PUSH_FLAGS:
            if name in ("-n", "--dry-run"):
                out["dry_run"] = True
            elif name == "--tags":
                out["tags"] = True
            elif name == "--follow-tags":
                out["follow_tags"] = True
            elif name in ("--all", "--branches", "--mirror"):
                out["kind"] = "multi"
                out["reason"] = f"{name} pushes more than one ref"
            elif name in ("-d", "--delete"):
                out["delete"] = True
            elif name == "--no-verify":
                return {"kind": "no_verify", "reason": "--no-verify"}
            elif name in ("-u", "--set-upstream"):
                out["set_upstream"] = True
            continue
        if has_eq:
            continue  # unknown --opt=value: self-contained, skip it
        return {"kind": "unparseable", "reason": f"unknown option {t!r}"}
    if any(_UNEXPANDABLE.search(p) for p in positional):
        return {"kind": "unparseable", "reason": "unexpandable argument"}
    if out["dry_run"]:
        out["kind"] = "dry_run"
        return out
    if out["kind"] == "multi":
        return out
    if positional:
        if not out["remote"]:
            out["remote"] = positional[0]
            positional = positional[1:]
        out["refspecs"] = positional
    if out["delete"]:
        out["kind"] = "delete"
        return out
    specs = []
    for spec in out["refspecs"]:
        spec = spec.lstrip("+")
        src, _, dst = spec.partition(":")
        if not src:
            continue  # `:dst` deletes; nothing to review
        specs.append((src, dst))
    if len(specs) > 1:
        out["kind"] = "multi"
        out["reason"] = f"{len(specs)} refspecs"
        return out
    if len(specs) == 1:
        out["src"], out["dst"] = specs[0]
    elif out["refspecs"]:
        out["kind"] = "delete"  # every refspec was a deletion
        return out
    if out["tags"] and not specs:
        out["kind"] = "tags"
    return out


def _hook_target(repo_root, cmd):
    """Resolve what a push command sends: the tip to review and its range.

    Returns (decision, info). decision is one of:
      "review"  info = {tip, branch, base, range, remote, dst}
      "allow"   info = {"why": ...}    nothing gains commits
      "deny"    info = {"why": ...}    refused, fail closed
    OCR_LEGACY_RANGE=1 restores the 0.5.x behaviour (checked-out HEAD against
    its upstream) for a shape this parser cannot read.
    """
    tgt = _parse_push(cmd)
    kind = tgt.get("kind")
    legacy = os.environ.get("OCR_LEGACY_RANGE", "").strip().lower() in ("1", "true", "yes")
    if kind in ("dry_run", "delete"):
        return "allow", {"why": kind}
    if kind == "multi_push":
        return "deny", {"why": (
            "review-gate: this command runs more than one `git push`. A review covers one "
            "push; reviewing the first would leave the rest unreviewed. Run them as "
            "separate commands."
        )}
    if kind == "no_verify":
        return "deny", {"why": (
            "review-gate: `--no-verify` disables the git pre-push adapter, which is the "
            "backstop for this gate. Push without it."
        )}
    if kind == "multi":
        return "deny", {"why": (
            "review-gate: this push updates more than one branch at once "
            f"({_sanitize(tgt.get('reason') or '', 80)}), and a review covers a single "
            "revision range. Push the branches separately, or set OCR_FAIL_OPEN=1 for a "
            "one-shot bypass."
        )}
    if kind == "unparseable":
        if legacy:
            return _legacy_target(repo_root)
        return "deny", {"why": (
            "review-gate: could not read what this push sends "
            f"({_sanitize(tgt.get('reason') or '', 120)}), so it was not reviewed. "
            "Blocking, because a gate that cannot see the commits must not wave them "
            "through.\n\nUse the plain form: git push [-u] <remote> <branch>\n"
            "  - OCR_LEGACY_RANGE=1 (in the environment Claude Code was launched from) "
            "reviews the checked-out branch against its upstream instead."
        )}
    remote = tgt.get("remote") or ""
    # --tags / --follow-tags: a tag may point at commits the remote has never
    # seen. `git push --tags` used to be allowed as "no branch"; that uploads
    # every commit those tags reach.
    # Checked whenever tags ride along, not only when they are all that is
    # pushed: `git push origin main --tags` carries them too.
    if kind == "tags" or tgt.get("tags") or tgt.get("follow_tags"):
        out, rc = _git(["rev-list", "--tags", "--not", "--remotes=" + (remote or "origin"),
                        "--max-count=1"], cwd=repo_root)
        if rc != 0:
            return "deny", {"why": "review-gate: could not evaluate which tagged commits the "
                                   "remote lacks; push the branch first, or without tags."}
        if out.strip() and (kind == "tags" or tgt.get("tags")):
            return "deny", {"why": (
                "review-gate: `--tags` would upload commits the remote does not have yet "
                "(reachable only from local tags). Push the branch that contains them "
                "first, so it is reviewed, then push the tags."
            )}
        if kind == "tags":
            return "allow", {"why": "tags already on remote"}
    src, dst = tgt.get("src") or "", tgt.get("dst") or ""
    if not src:
        # `git push` / `git push origin`: what git itself would send.
        full, rc = _git(["rev-parse", "--symbolic-full-name", "@{push}"], cwd=repo_root)
        if rc == 0 and full.startswith("refs/remotes/"):
            rest = full[len("refs/remotes/"):]
            rem, _, dst = rest.partition("/")
            remote = remote or rem
            src = _branch(repo_root) or "HEAD"
        elif tgt.get("set_upstream") or remote:
            src = _branch(repo_root)
            dst = src
        else:
            if legacy:
                return _legacy_target(repo_root)
            return "deny", {"why": (
                "review-gate: this branch has no push destination configured, so what "
                "`git push` would send is undefined. Name it: git push -u <remote> <branch>"
            )}
    if not remote:
        remote = "origin"
    if src == "HEAD":
        src_branch = _branch(repo_root) or ""
    else:
        src_branch = src
    if not dst:
        dst = src_branch or src
    dst = dst[len("refs/heads/"):] if dst.startswith("refs/heads/") else dst
    if dst.startswith("refs/tags/"):
        return "deny", {"why": "review-gate: pushing to a tag ref is not reviewable as a "
                               "branch push; push the branch, then the tag."}
    tip, rc = _git(["rev-parse", "--verify", "--quiet", src + "^{commit}"], cwd=repo_root)
    if rc != 0 or not tip:
        return "deny", {"why": f"review-gate: `{_sanitize(src, 80)}` does not name a commit "
                               "in this repository, so nothing could be reviewed."}
    base = ""
    for cand in (f"refs/remotes/{remote}/{dst}", "origin/HEAD", "origin/main", "origin/master"):
        ref, rc = _git(["rev-parse", "--verify", "--quiet", cand + "^{commit}"], cwd=repo_root)
        if rc != 0 or not ref:
            continue
        mb, rc = _git(["merge-base", ref, tip], cwd=repo_root)
        if rc == 0 and mb:
            base = mb
            break
    if not base:
        base = _EMPTY_TREE  # brand-new repository: everything is new
    rng = base + ".." + tip
    if base != _EMPTY_TREE:
        out, rc = _git(["log", rng, "--oneline", "--max-count=1"], cwd=repo_root)
        if rc == 0 and not out.strip():
            return "allow", {"why": "remote already has these commits"}
    return "review", {
        "tip": tip, "branch": src_branch or src, "base": base, "range": rng,
        "remote": remote, "dst": dst,
    }


def _legacy_target(repo_root):
    """0.5.x semantics: the checked-out HEAD against whatever it is ahead of."""
    tip = _head_sha(repo_root)
    if not tip:
        return "deny", {"why": "review-gate: no HEAD to review."}
    if not _has_unpushed_commits(repo_root, ""):
        return "allow", {"why": "nothing unpushed"}
    return "review", {"tip": tip, "branch": _branch(repo_root), "base": "", "range": "",
                      "remote": "", "dst": ""}


def _hookspath_shadowed(repo_root):
    """True when a repo-local core.hooksPath hides the global git adapter.

    install-git-hook.sh installs by setting the GLOBAL core.hooksPath, but git
    resolves the LOCAL one first. So any repo that manages its own hooks --
    husky, lefthook, a hand-rolled scripts/git-hooks -- silently drops the
    global gate out of the chain, with nothing to announce it. Pushes made
    through Claude Code are still covered by the PreToolUse adapter; pushes
    from a plain terminal in such a repo are not gated at all.
    """
    local, rc = _git(["config", "--local", "--get", "core.hooksPath"], cwd=repo_root)
    if rc != 0 or not local:
        return False
    glob_, rc = _git(["config", "--global", "--get", "core.hooksPath"], cwd=repo_root)
    if rc != 0 or not glob_:
        return False  # the global adapter is not installed; there is nothing to shadow
    try:
        if os.path.normcase(os.path.abspath(local)) == os.path.normcase(os.path.abspath(glob_)):
            return False  # both point at the same hooks
    except Exception:
        pass
    # A repo is free to chain into us from its own hook dir; that is not
    # shadowing. Detecting that needs a STRONG signal, though. A bare
    # "review-gate" substring is not one: the repo that prompted this check has
    # a pre-push whose comments discuss review-gate at length precisely to
    # explain that it does NOT invoke it, which read as "chained" and hid the
    # very fail-open this function exists to report. Require something you only
    # write when actually running the gate -- the script's own filename, or the
    # global hooks dir being exec'd.
    #
    # The bias is deliberate: a false "shadowed" on a repo that does chain is
    # noise, while a false "not shadowed" is the silent fail-open itself.
    try:
        hook = Path(repo_root or ".") / local / "pre-push"
        if hook.is_file():
            body = hook.read_text(encoding="utf-8", errors="replace")
            if "review-gate.py" in body or glob_ in body:
                return False
    except Exception:
        pass
    return True


def _is_valid_verdict(obj):
    """Return True only if obj is a properly-shaped verdict or resolver result."""
    if not isinstance(obj, dict):
        return False
    if "resolutions" in obj:
        return True  # resolver path
    return "status" in obj and "verdict" in obj and "findings" in obj


def _num_turns_from_stream_json(text):
    """Extract num_turns from the result event of --output-format stream-json output."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
            if isinstance(ev, dict) and ev.get("type") == "result":
                return ev.get("num_turns")
        except Exception:
            continue
    return None


def _extract_from_stream_json(text):
    """Scan ALL assistant-turn events for a valid verdict; return the last match.

    With --output-format stream-json, claude emits NDJSON where each assistant
    turn is its own event. A stray notification ack after the verdict becomes a
    later event whose text has no verdict JSON -- it cannot replace the earlier
    turn's payload because we scan every event, not just the last.
    """
    if not text:
        return None
    match = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "assistant":
            content = event.get("message", {}).get("content", [])
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    candidate = _extract_json(block.get("text", ""))
                    if candidate is not None:
                        match = candidate
        elif event_type == "result":
            candidate = _extract_json(event.get("result") or "")
            if candidate is not None:
                match = candidate
    return match


def _extract_json(text):
    """Pull the review JSON object out of claude's stdout. Returns dict or None."""
    if not text:
        return None
    text = text.strip()
    # 1) whole thing
    try:
        obj = json.loads(text)
        if _is_valid_verdict(obj):
            return obj
    except Exception:
        pass
    # 2) fenced ```json ... ``` block (last one)
    import re

    blocks = re.findall(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    for b in reversed(blocks):
        try:
            obj = json.loads(b)
            if _is_valid_verdict(obj):
                return obj
        except Exception:
            continue
    # 3) balanced scan from each '{' in turn (json.JSONDecoder.raw_decode stops
    # at the object's own matching brace and ignores everything after it, unlike
    # a naive find("{")..rfind("}") span, which grabs the LAST '}' anywhere in
    # the text -- including one inside trailing prose the model appended after
    # the JSON despite being told to print only the object -- and turns a valid
    # verdict into an unparseable-output failure.
    #
    # Only a dict carrying "findings"/"resolutions" that also satisfies
    # _is_valid_verdict is accepted as a candidate. Without that check the first
    # '{' that happens to decode would win even if it's an unrelated JSON value
    # the model quoted from the reviewed diff itself (e.g. a config fixture)
    # before the real verdict -- and take the LAST candidate, not the first,
    # since that quoted case necessarily precedes the model's actual answer.
    decoder = json.JSONDecoder()
    idx, match = text.find("{"), None
    while idx != -1:
        try:
            obj, end = decoder.raw_decode(text, idx)
            if _is_valid_verdict(obj):
                match = obj
            idx = text.find("{", end)
        except json.JSONDecodeError:
            idx = text.find("{", idx + 1)
    return match


def _find_claude():
    """Locate the claude CLI so the gate works from any shell, not just inside
    Claude Code. Order: PATH; then OCR_CLAUDE_BIN / CLAUDE_CODE_EXECPATH; then,
    on Windows, the Desktop App's bundled claude.exe (a versioned path that is
    not on PATH) — newest version wins."""
    found = shutil.which("claude")
    if found:
        return found
    for env in ("OCR_CLAUDE_BIN", "CLAUDE_CODE_EXECPATH"):
        exe = os.environ.get(env, "")
        if exe and os.path.isfile(exe):
            return exe
    import glob

    roots = [p for p in (os.environ.get("LOCALAPPDATA"), os.path.join(os.path.expanduser("~"), "AppData", "Local")) if p]
    cands = []
    for root in roots:
        cands += glob.glob(os.path.join(root, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude", "claude-code", "*", "claude.exe"))
    if cands:
        cands.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        return cands[0]
    return None


# Substrings that identify a credentials problem rather than a review problem.
# Matched case-insensitively against whatever claude printed.
_AUTH_MARKERS = (
    "oauth session expired",
    "failed to authenticate",
    "authentication_error",
    "invalid api key",
    "please run /login",
    # Keep these as full phrases. A bare "credentials" substring also matches
    # unrelated crash text that merely mentions the word, and a wrong auth hint
    # is worse than none -- it sends people to re-login over a real bug.
    "invalid credentials",
    "credentials expired",
    "expired credentials",
)

# Patterns that identify a session/usage/rate limit in the reviewer output.
# Anchored to avoid false matches on "limit" as a generic word.
_LIMIT_PATTERNS = [
    re.compile(r"hit your (?:\w+ )?limit", re.IGNORECASE),
    re.compile(r"usage limit reached", re.IGNORECASE),
    re.compile(r"\brate limit\b", re.IGNORECASE),
    re.compile(r"session limit", re.IGNORECASE),
]
_RESETS_AT_RE = re.compile(
    r"resets\s+(\d{1,2}(?::\d{2})?(?:\s*[aApP][mM])?)\s*\(([^)]+)\)",
    re.IGNORECASE,
)


def _parse_resets_at(text):
    """Parse 'resets 3:20pm (Europe/Lisbon)' from text, return epoch or None."""
    m = _RESETS_AT_RE.search(text or "")
    if not m:
        return None
    time_str, tz_str = m.group(1).strip(), m.group(2).strip()
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_str)
    except Exception:
        return None
    try:
        import datetime
        ts = time_str.lower().replace(" ", "")
        is_pm = ts.endswith("pm")
        is_am = ts.endswith("am")
        if is_pm or is_am:
            ts = ts[:-2]
        if ":" in ts:
            h, m_val = int(ts.split(":")[0]), int(ts.split(":")[1])
        else:
            h, m_val = int(ts), 0
        if is_pm and h != 12:
            h += 12
        elif is_am and h == 12:
            h = 0
        now = datetime.datetime.now(tz)
        reset = now.replace(hour=h, minute=m_val, second=0, microsecond=0)
        if reset <= now:
            reset += datetime.timedelta(days=1)
        return reset.timestamp()
    except Exception:
        return None


def _check_limit(out_text, err_text=""):
    """Check if output signals a usage limit. Returns (bool, resets_at_or_None)."""
    combined = (out_text or "") + " " + (err_text or "")
    for pat in _LIMIT_PATTERNS:
        if pat.search(combined):
            return True, _parse_resets_at(combined)
    return False, None


def _auth_hint(output):
    """Extra guidance when claude's output looks like a login/credentials failure.

    Returns "" for anything else, so the caller can always interpolate it.

    This stays FAIL CLOSED on purpose. Only a missing binary fails open; an
    expired login is the tool being present but unusable, and treating that as
    "no gate needed" would make expiring credentials a silent bypass. So block,
    but say what to actually fix -- the generic parse error sends people into
    the review skill when nothing there is wrong.

    Re-login is interactive and cannot be done from inside a hook: the headless
    subprocess this gate spawns has no terminal to complete the OAuth flow.
    """
    low = (output or "").lower()
    if not any(m in low for m in _AUTH_MARKERS):
        return ""
    return (
        "  This is a CREDENTIALS failure, not a review failure -- the review never ran.\n"
        "  Fix it : run `claude` in an interactive terminal and log in via /login,\n"
        "    then retry. The headless session the gate spawns cannot complete an\n"
        "    OAuth flow itself (no terminal to hand the browser callback to).\n"
        "  Note   : a Claude Code Desktop session refreshes its own auth in-process,\n"
        "    so the app keeps working while the on-disk credentials the CLI reads go\n"
        "    stale -- the gate breaks with no visible sign anything logged out.\n"
    )


def _bypass_hint(mode):
    """Emergency-bypass instruction text, mode-aware.

    OCR_FAIL_OPEN is read from this process's own os.environ (line ~330). In
    --mode hook this process inherits Claude Code's own launch environment, NOT
    the shell environment of the `git commit` Bash tool call that triggered the
    PreToolUse hook -- so an inline `OCR_FAIL_OPEN=1 git commit ...` prefix is a
    no-op in hook mode: the gate denies again and the printed bypass appears to
    not work, with no indication why.
    """
    if mode == "hook":
        return (
            "  Emergency one-shot bypass : export OCR_FAIL_OPEN=1 in the environment\n"
            "    Claude Code itself is launched from (a shell prefix on `git commit` will\n"
            "    NOT work in hook mode -- this process inherits Claude Code's env, not the\n"
            "    Bash tool call's)."
        )
    return "  Emergency one-shot bypass : OCR_FAIL_OPEN=1 git commit ..."


def _downgrade_hint(mode):
    """Permanent block->advisory downgrade instruction text, mode-aware.

    OCR_ADVISORY has the identical env-inheritance limitation as OCR_FAIL_OPEN
    (see _bypass_hint) -- an inline prefix on `git commit` never reaches this
    process in hook mode. But this message is `permissionDecisionReason`, which
    Claude Code surfaces back to the SAME agent whose commit just got blocked
    -- unlike a human at a terminal reading a git CLI error, that agent has a
    Write tool. A file-based alternative (.ocr/config.json {"blocking": false})
    is therefore not a neutral workaround here: it reads as a self-service,
    persistent, repo-wide kill switch the blocked agent could just write and
    commit. Keep the session-scoped env-var route as the primary hint (it
    still requires a human to re-launch Claude Code with the var set -- not
    something the agent can do to itself mid-session) and only mention the
    file route with an explicit human-only/persistent/repo-wide label.
    """
    if mode == "hook":
        return (
            '  Downgrade to advisory (warn-only) : export OCR_ADVISORY=1 in the environment\n'
            "    Claude Code itself is launched from (a shell prefix on `git commit` will NOT\n"
            "    work in hook mode -- this process inherits Claude Code's env, not the Bash\n"
            '    tool call\'s). A human can also add (or create) {"blocking": false} in\n'
            "    .ocr/config.json, but that is a PERSISTENT, REPO-WIDE change committed into\n"
            "    the repo -- a human decision, not something to self-apply from inside a\n"
            "    blocked session."
        )
    return "Downgrade to advisory (warn-only): OCR_ADVISORY=1 git commit ..."


def _debug_enabled():
    return os.environ.get("OCR_DEBUG", "").strip().lower() in ("1", "true", "yes")


# The debug log is always on for the metric lines below and rotates by size:
# 1 MiB, three older files kept as review-gate-debug.log.1 .. .3.
_DEBUG_LOG_BYTES = 1024 * 1024
_DEBUG_LOG_KEEP = 3
_DEBUG_LOG_LOCK = threading.Lock()


def _debug_log(line):
    """Append one line to the forensic log. Best-effort and silent on
    failure -- a diagnostic aid must never be able to break the gate it exists
    to help debug. Lives beside _park_pending's data, outside .git, so it
    survives whatever state the repo itself is in. Rotates (see above)."""
    try:
        with _DEBUG_LOG_LOCK:      # parallel chunks (0.12.0) log from worker threads
            ocr_telemetry.rotating_append(
                _gate_data_dir() / "review-gate-debug.log", line,
                max_bytes=_DEBUG_LOG_BYTES, keep=_DEBUG_LOG_KEEP)
    except Exception:
        pass


def _metric_log(event, **fields):
    """One always-on log line of counts, bytes and timings (never a path or
    code: ocr_telemetry.metric_line turns anything else into `?`)."""
    try:
        run = str(_TELE.get("run_id") or "")
        _debug_log(ocr_telemetry.metric_line(event, run=run[:16] or None, **fields))
    except Exception:
        pass


# Cheap secondary hardening with no supporting evidence either way: isolates
# the reviewer child from the parent's console on Windows (no shared process
# group, so a Ctrl-Break/Ctrl-C broadcast to the console can't reach the
# parent through it; no console handle at all, safe since every stdio stream
# below is piped, and it rules out the child resetting console modes on exit
# and leaving the parent's terminal in a bad state). getattr guards let this
# run harmlessly on a platform where the constants don't exist. The primary,
# evidence-backed hypothesis is _SESSION_BRIDGE_ENV below -- keep this, but it
# is not where the crash is most likely to actually be.
_WIN_FLAGS = (
    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
)

# Confirmed present on a real Claude Code Desktop session (a live OCR_DEBUG
# smoke test on 2026-09-11 captured this exact list from an actual crash-prone
# session, not a guess): together these tell a CLI process "you are the
# desktop host's SDK child, on messaging channel X, tracking session Y" --
# state an independent reviewer should not inherit regardless of whether it is
# the crash's cause. The git pre-push adapter runs with NONE of these present
# and that is the known-good baseline this restores. Scrubbed by default (see
# OCR_UNSET_ENV) because the worst-case regression is a visible reviewer auth
# error the gate already reports -- not a silent one -- which is a better
# trade than leaving a live IPC channel and its token in a second process's
# hands. Left inherited on purpose: CLAUDECODE (the CLI may use it to suppress
# interactive behaviour, harmless to inherit; first thing to add via
# OCR_UNSET_ENV if scrubbing this bundle alone doesn't stop the crash),
# CLAUDE_CODE_EXECPATH (read directly by _find_claude, not identity), and the
# per-feature flags (DISABLE_CRON, EAGER_FLUSH, etc.) which configure
# behaviour rather than claim a session.
_SESSION_BRIDGE_ENV = (
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_HOST_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_PID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH",
    "CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH",
    "CLAUDE_CODE_OAUTH_SCOPES",
)

# Values safe to log verbatim in the OCR_DEBUG breadcrumb (not secret-adjacent
# -- unlike the socket/token/session-id members of _SESSION_BRIDGE_ENV, which
# are logged as names only, same as everything else).
_DEBUG_SAFE_VALUES = (
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_AGENT_SDK_VERSION",
)


def _test_reviewer_cmd():
    """argv to run INSTEAD of `claude -p ...`, for the end-to-end tests only.

    OCR_REVIEWER_CMD is honoured solely when its first element is a file under
    this plugin's own tests/ directory. Environment variables reach hooks from
    the target repository's settings (`env` in .claude/settings.json is applied
    to the CLI and inherited), so an unconditional seam would hand a hostile
    repo arbitrary command execution as the "reviewer". Installed copies ship
    no tests/, which makes the seam inert there.
    """
    raw = os.environ.get("OCR_REVIEWER_CMD", "").strip()
    if not raw:
        return None
    try:
        # Non-POSIX splitting keeps backslashes intact (Windows paths) but
        # also keeps the surrounding quotes on each token; strip those.
        argv = [t[1:-1] if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'" else t
                for t in shlex.split(raw, posix=False)]
    except ValueError:
        return None
    if not argv:
        return None
    tests_dir = os.path.realpath(os.path.join(_PLUGIN_ROOT, "tests"))
    try:
        is_py = os.path.basename(argv[0]).lower().startswith("python")
        script = os.path.realpath(argv[1] if (is_py and len(argv) > 1) else argv[0])
    except Exception:
        return None
    if not (script.startswith(tests_dir + os.sep) and os.path.isfile(script)):
        return None
    return argv


# What this supervisor process learned about its run, for the local run log
# (scripts/ocr_telemetry.py). One supervisor runs one review, so a module
# global is enough; _supervise resets it.
_TELE = {}


# Time metrics of the model call in flight (ocr_telemetry.stream_stats of its
# stream-json stdout), set by _run_review_once and read by _run_review right
# after it returns or raises. Counts, bytes and timings only. Per thread: since
# 0.12.0 several chunks' reviewers run at once, one per worker thread.
_CALL_STATS_TLS = threading.local()


def _last_call_stats():
    d = getattr(_CALL_STATS_TLS, "stats", None)
    if d is None:
        d = _CALL_STATS_TLS.stats = {}
    return d

# Per-call keys copied into the telemetry entry and the log line.
_CALL_SCALARS = ("turns", "api_s", "cost_usd", "first_event_s", "agent_calls",
                 "agent_input_bytes", "agent_wall_s", "orchestrator_s")


def _tele_call(kind, seconds, outcome, manifest=None, stats=None):
    entry = {"kind": kind, "seconds": round(seconds, 2), "outcome": outcome}
    if manifest:
        try:
            entry["manifest_bytes"] = os.path.getsize(manifest)
        except OSError:
            pass
    for key in _CALL_SCALARS:
        if stats and stats.get(key) is not None:
            entry[key] = stats[key]
    if stats and stats.get("tools"):
        entry["tools"] = {k: list(v) for k, v in stats["tools"].items()}
    _TELE.setdefault("calls", []).append(entry)
    _metric_log("call", kind=kind, outcome=outcome, s=entry["seconds"],
                manifest_bytes=entry.get("manifest_bytes"),
                tools={k: v[0] for k, v in (entry.get("tools") or {}).items()},
                **{k: entry.get(k) for k in _CALL_SCALARS})
    return entry


_PHASE_CLOCK = {"t": None}


def _mark_phase(name, **counts):
    """Close the run phase that began at the previous mark (or at the start of
    the supervised run) and name it. The supervisor's phases run one after the
    other, so a mark per boundary is the whole timer."""
    now = time.monotonic()
    began = _PHASE_CLOCK["t"]
    _PHASE_CLOCK["t"] = now
    if began is None:
        return
    entry = dict({"name": name, "seconds": round(now - began, 2)},
                 **{k: v for k, v in counts.items() if v is not None})
    _TELE.setdefault("phases", []).append(entry)
    _metric_log("phase", name=name, s=entry["seconds"],
                **{k: v for k, v in counts.items()})


def _tele_chunk(index, total, files, lines, outcome, seconds):
    entry = {"index": index, "of": total, "files": files, "lines": lines,
             "outcome": outcome, "seconds": round(seconds, 2)}
    _TELE.setdefault("chunks", []).append(entry)
    _metric_log("chunk", index=index, of=total, files=files, lines=lines,
                outcome=outcome, s=entry["seconds"])


def _run_review(repo_root, mode, git_dir=None, head_sha="", push_range="",
                paths_file=None, timeout=None, raw_tag="", resolve_file=None):
    """_run_review_once, timed into the run log."""
    kind = ("recheck" if "recheck" in raw_tag else "resolve") if resolve_file else "review"
    started = time.monotonic()
    outcome = "error"
    _last_call_stats().clear()
    try:
        out = _run_review_once(repo_root, mode, git_dir, head_sha, push_range,
                               paths_file=paths_file, timeout=timeout,
                               raw_tag=raw_tag, resolve_file=resolve_file)
        outcome = "ok" if out[1] else "skipped"
        return out
    except ReviewLimitError:
        outcome = "limit"
        raise
    except ReviewGateError as exc:
        outcome = "timeout" if getattr(exc, "is_timeout", False) else "error"
        raise
    finally:
        _tele_call(kind, time.monotonic() - started, outcome, resolve_file or paths_file,
                   stats=dict(_last_call_stats()))


def _note_call_stats(out_text, started_at):
    """Parse the reviewer's stream-json into this thread's call stats. Never raises
    and never changes what the call returns: this only reads what it already captured."""
    try:
        stats = _last_call_stats()
        stats.clear()
        stats.update(ocr_telemetry.stream_stats(out_text, started_at))
    except Exception:
        pass


def _run_review_once(repo_root, mode, git_dir=None, head_sha="", push_range="",
                     paths_file=None, timeout=None, raw_tag="", resolve_file=None):
    """Return (result_dict, True, raw_archive_name) on success.

    paths_file: path to a chunk manifest JSON; if given, --paths-file is added
      to the skill prompt so the reviewer processes only that chunk's files.
    timeout: override the global TIMEOUT for this call (used by _run_chunked).
    raw_tag: suffix appended to head_sha in the history filename so each
      chunk's raw output gets a distinct file.

    Raises ReviewGateError (with .is_timeout=True for timeouts),
    ReviewLimitError when the reviewer reports a usage/session limit, or
    returns (None, False, "") when claude is not installed (fail-open).
    """
    claude = _find_claude()
    # The test stub runs INSTEAD of claude, so it must not need claude to be
    # installed -- CI runners have none, and every end-to-end test there was
    # silently "skipped (fail-open)" instead of exercising the gate.
    if not claude and not _test_reviewer_cmd():
        _warn("`claude` CLI not found on PATH or CLAUDE_CODE_EXECPATH - skipping review (fail-open).")
        return None, False, ""
    # OCR_CLAUDE_ARGS replaces the defaults wholesale (full escape hatch, also
    # discards the cost controls AND the read-only tool allowlist);
    # OCR_CLAUDE_EXTRA_ARGS appends to them, which is what callers usually want.
    override = os.environ.get("OCR_CLAUDE_ARGS")
    if override:
        args = shlex.split(override)
    else:
        args = list(DEFAULT_CLAUDE_ARGS)
        args += shlex.split(os.environ.get("OCR_CLAUDE_EXTRA_ARGS", ""))
    bypass = _bypass_hint(mode)
    # The child is given --plugin-dir, so THIS plugin -- including its
    # PreToolUse push gate -- is registered inside the review session too.
    # Without a marker in the environment, every Bash call the reviewer makes
    # pays a Python spawn, and a push from inside a review would nest a whole
    # second review. Both adapters short-circuit on this (see _in_review).
    child_env = dict(os.environ)
    child_env["OCR_IN_REVIEW"] = "1"
    # The cwd is the untrusted branch, whose AGENTS.md/CLAUDE.md a session would
    # load. --setting-sources "" also blocks them, but OCR_CLAUDE_ARGS can drop it.
    child_env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
    # unset/empty -> scrub _SESSION_BRIDGE_ENV (the default, see its comment);
    # "none" (case-insensitive) -> scrub nothing, the pre-0.6 behaviour, for a
    # repo whose reviewer genuinely needs the desktop host's auth relay;
    # anything else -> exactly that list, REPLACING the default rather than
    # adding to it (a partial scrub of this bundle is its own novel, untested
    # state -- see _SESSION_BRIDGE_ENV). Unset and explicitly-empty are treated
    # identically: `set X=` on Windows deletes the variable outright, so a
    # script cannot tell "cleared" from "never set" apart anyway. Names split
    # on comma, semicolon, or whitespace (a `;`-separated PATH habit is a
    # common typo here) and matched case-insensitively on Windows, where
    # os.environ's keys are already upper-cased by CPython regardless of how
    # the variable was actually set.
    _override = os.environ.get("OCR_UNSET_ENV", "").strip()
    if not _override:
        _unset_names = list(_SESSION_BRIDGE_ENV)
    elif _override.lower() == "none":
        _unset_names = []
    else:
        _unset_names = [n for n in re.split(r"[,;\s]+", _override) if n]
    scrubbed = []
    for _name in _unset_names:
        _key = _name.upper() if os.name == "nt" else _name
        if child_env.pop(_key, None) is not None:
            scrubbed.append(_key)

    debug = _debug_enabled()
    creationflags = _WIN_FLAGS if sys.platform == "win32" else 0
    _SUFFIX = " --json"
    if resolve_file:
        base_prompt = PROMPT_RANGE.format(rng=push_range) if push_range else PROMPT
        if base_prompt.endswith(_SUFFIX):
            base_prompt = base_prompt[: -len(_SUFFIX)]
        rf_fwd = resolve_file.replace("\\", "/")
        prompt = f'{base_prompt} --resolve "{rf_fwd}" --json'
    elif paths_file:
        base_prompt = PROMPT_RANGE.format(rng=push_range) if push_range else PROMPT
        # Strip exactly the trailing " --json" suffix (both PROMPT constants end
        # with it).  Do NOT use rstrip(" --json") — that strips a character SET.
        if base_prompt.endswith(_SUFFIX):
            base_prompt = base_prompt[: -len(_SUFFIX)]
        # Forward slashes + double quotes so a path with spaces and backslashes
        # (e.g. C:\Users\John Doe\...) survives the slash-command arg parser.
        pf_fwd = paths_file.replace("\\", "/")
        prompt = f'{base_prompt} --paths-file "{pf_fwd}" --json'
    else:
        prompt = PROMPT_RANGE.format(rng=push_range) if push_range else PROMPT
    cmd = [claude, "-p", prompt] + args
    stub = _test_reviewer_cmd()
    if stub:
        # Range is always last so stub's sys.argv[-1] still gives the range.
        if resolve_file:
            cmd = stub + ["--resolve", resolve_file, push_range]
        elif paths_file:
            cmd = stub + ["--paths-file", paths_file, push_range]
        else:
            cmd = stub + [push_range]
    # POSIX: the reviewer leads its own process group, so _kill_child can take
    # down everything it spawned (os.killpg); before, only `claude` itself died
    # and its children outlived a timeout. Windows keeps taskkill /T (_tree_kill).
    popen_extra = {} if sys.platform == "win32" else {"start_new_session": True}
    _run_timeout = timeout if timeout is not None else TIMEOUT
    # Wall-clock for the log line (so it lines up with Event Viewer/Task
    # Manager timestamps when correlating with a crash); monotonic for the
    # duration math below, which a wall-clock adjustment mid-review must not
    # skew.
    started_at = time.time()
    started_mono = time.monotonic()
    try:
        with subprocess.Popen(
            cmd,
            cwd=repo_root,
            # No shared stdin handle with the parent -- one fewer thing to have
            # in common with whatever the parent's own console/pipes are doing.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # Explicit, because text=True alone decodes with
            # locale.getpreferredencoding() -- cp1252 on a default Windows box.
            # The reviewer emits UTF-8, so every em-dash in a finding was being
            # mangled at capture and then stored mangled forever: the findings
            # log, the raw snapshot, and now the context injected into the
            # session all carried it. errors="replace" because a corrupted byte
            # must not take the whole review down.
            encoding="utf-8",
            errors="replace",
            env=child_env,
            creationflags=creationflags,
            **popen_extra,
        ) as proc:
            if debug:
                # Names only for everything, except the short, non-secret
                # allowlist in _DEBUG_SAFE_VALUES -- never the socket/token/
                # session-id members of _SESSION_BRIDGE_ENV. ANTHROPIC* is
                # included so a debug run also shows whether the reviewer's
                # auth path is an inherited API key rather than the host relay.
                present = sorted(
                    k for k in child_env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))
                )
                # From the ORIGINAL environment, not child_env: several
                # _DEBUG_SAFE_VALUES names are also in _SESSION_BRIDGE_ENV, so
                # by default they're already gone from child_env by this
                # point -- reading child_env here would silently log {} and
                # defeat the point of allowlisting them.
                safe_values = {k: v for k, v in os.environ.items() if k in _DEBUG_SAFE_VALUES}
                _debug_log(
                    f"start ts={started_at:.3f} mode={mode} head={head_sha} own_pid={os.getpid()} "
                    f"child_pid={proc.pid} cwd={repo_root!r} scrubbed={scrubbed} "
                    f"present_after_scrub={present} safe_values={safe_values}"
                )
            # Mirrors subprocess.run's own Popen usage exactly, including which
            # exceptions kill the child -- a rewrite that only handled
            # TimeoutExpired would leave an orphaned `claude.exe` running (and
            # burning tokens) whenever this hook is cancelled or errors out for
            # any other reason, including KeyboardInterrupt (hence
            # BaseException, not Exception, below).
            #
            # _kill_child is a TREE kill (taskkill /T on Windows, the process
            # group elsewhere) scoped to this one known pid: the claude.exe on
            # PATH may be a launcher whose real node child would otherwise
            # survive. Never a kill-by-name.
            _register_child(proc)
            try:
                out_text, err_text = proc.communicate(timeout=_run_timeout)
            except subprocess.TimeoutExpired:
                _kill_child(proc)
                partial = proc.communicate()
                # What the reviewer had done before it was cut off is the most
                # useful timing there is: where did the timeout go.
                _note_call_stats(partial[0] if isinstance(partial, tuple) and partial else "",
                                 started_at)
                if debug:
                    _debug_log(
                        f"end child_pid={proc.pid} outcome=timeout "
                        f"duration_s={time.monotonic()-started_mono:.1f}"
                    )
                raise
            except BaseException:
                _kill_child(proc)
                if debug:
                    _debug_log(
                        f"end child_pid={proc.pid} outcome=exception "
                        f"duration_s={time.monotonic()-started_mono:.1f}"
                    )
                raise
            finally:
                _unregister_child(proc)
            _note_call_stats(out_text, started_at)
            if debug:
                _debug_log(
                    f"end child_pid={proc.pid} outcome=rc{proc.returncode} "
                    f"duration_s={time.monotonic()-started_mono:.1f}"
                )
    except subprocess.TimeoutExpired:
        if mode == "hook":
            # This process is the detached supervisor, which inherits Claude
            # Code's own launch environment, NOT the shell env of the `git
            # push` Bash tool call -- an inline `OCR_TIMEOUT=<n> git push`
            # prefix never reaches it. Since 0.6.0 the timeout is enforced
            # here, outside the hook, so raising it no longer needs (and must
            # not get) a matching rise in hooks/hooks.json: that timeout has
            # to stay under the desktop app's ~16 min session wall.
            escalation = (
                f"  Give Claude more time : export OCR_TIMEOUT={TIMEOUT * 2} in the environment\n"
                f"    Claude Code itself is launched from (a shell prefix on `git push` will\n"
                f"    NOT work in hook mode). Leave hooks/hooks.json's PreToolUse timeout\n"
                f"    alone: the review runs detached from the hook, and that timeout must\n"
                f"    stay below the host's session watchdog."
            )
        else:
            escalation = f"  Give Claude more time : OCR_TIMEOUT={TIMEOUT * 2} git push ..."
        used = _run_timeout
        raise ReviewGateError(
            f"review timed out after {used}s - blocking commit to preserve gate integrity.\n"
            f"{escalation}\n"
            f"{bypass}",
            is_timeout=True,
        )
    except Exception as exc:
        raise ReviewGateError(
            f"review process error ({exc}) - blocking commit to preserve gate integrity.\n"
            f"{bypass}"
        )
    raw_name = _save_raw_output(git_dir, out_text, head_sha, raw_tag)
    # A non-zero exit means claude never got as far as producing a review, so the
    # output is an error string, not malformed JSON. Diagnose that separately:
    # reporting "could not parse review output" for a login failure sends people
    # looking at the review skill when the real fault is the CLI's credentials.
    # Note claude writes these errors to STDOUT, so stderr is often empty.
    if proc.returncode != 0:
        # Check for usage/session limit before treating as a generic error.
        is_limit, resets_at = _check_limit(out_text, err_text)
        if is_limit:
            raise ReviewLimitError(
                f"usage limit (exit {proc.returncode}): the reviewer could not run.\n{bypass}",
                resets_at=resets_at,
            )
        # Strip BEFORE falling through: a whitespace-only stdout is truthy, so
        # `stdout or stderr` would select it and discard a real stderr message,
        # leaving detail empty and hiding why the review failed.
        detail = (out_text or "").strip() or (err_text or "").strip()
        raise ReviewGateError(
            f"`claude` exited {proc.returncode} without running the review -- blocking commit "
            "to preserve gate integrity.\n"
            f"  {claude}\n"
            f"  Output (first 400 chars): {detail[:400]!r}\n"
            f"{_auth_hint(detail)}"
            f"{bypass}"
        )
    result = _extract_from_stream_json(out_text) or _extract_json(out_text)
    if result is None:
        # Exit 0 but no JSON. Check for limit before auth hint.
        is_limit, resets_at = _check_limit(out_text)
        if is_limit:
            raise ReviewLimitError(
                f"usage limit (exit 0): the reviewer could not run.\n{bypass}",
                resets_at=resets_at,
            )
        # Auth failures have been seen to exit 0 too
        # (the Desktop-bundled claude.exe does exactly this), so still check.
        _num_turns = _num_turns_from_stream_json(out_text)
        _turns_note = f" ({_num_turns} turn(s) captured)" if _num_turns is not None else ""
        _raw_note = f"  Raw output saved: {raw_name!r}\n" if raw_name else ""
        raise ReviewGateError(
            f"could not parse review output{_turns_note} - blocking commit to preserve gate integrity.\n"
            f"{_raw_note}"
            f"  Claude stdout (first 400 chars): {out_text[:400]!r}\n"
            f"{_auth_hint(out_text or '')}"
            f"{bypass}",
            is_parse_failure=True,
        )
    return result, True, raw_name


# --- the review runs elsewhere: state file, supervisor, inline join ----------
# The reviewer children currently being waited on by _run_review (one per
# chunk in flight since 0.12.0), so the supervisor's heartbeat thread can kill
# them all on a fence break, and a failing chunk can stop its siblings.
_ACTIVE_CHILDREN = set()
_ACTIVE_CHILDREN_LOCK = threading.Lock()


def _register_child(proc):
    with _ACTIVE_CHILDREN_LOCK:
        _ACTIVE_CHILDREN.add(proc)


def _unregister_child(proc):
    with _ACTIVE_CHILDREN_LOCK:
        _ACTIVE_CHILDREN.discard(proc)


def _active_children():
    """The children in flight, lowest pid first (a stable order for the state file)."""
    with _ACTIVE_CHILDREN_LOCK:
        return sorted(_ACTIVE_CHILDREN, key=lambda c: getattr(c, "pid", 0) or 0)


def _kill_active_children():
    for child in _active_children():
        _kill_child(child)
# The genuine class, captured at import: tests substitute subprocess.Popen
# with stand-ins carrying made-up pids, and those must never reach taskkill.
_REAL_POPEN = subprocess.Popen


def _kill_child(proc):
    """Kill a reviewer and everything it spawned. Scoped to one known pid.

    Only a real Popen gets the tree kill: tests hand _run_review a stand-in
    with a made-up pid, and `taskkill /PID 4242 /T /F` on a developer's box
    would hit whatever process happens to own that number.
    """
    # The tree first: taskkill /T finds the children through the parent's pid,
    # which is gone once the parent has been killed.
    if type(proc) is _REAL_POPEN:
        _tree_kill(proc.pid)
    try:
        proc.kill()
    except Exception:
        pass


def _tree_kill(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return
    if pid <= 0:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            import signal
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def _async_dir(common_dir):
    return Path(common_dir) / ASYNC_DIR


def _state_path(common_dir, tip):
    return _async_dir(common_dir) / f"{tip}.json"


def _read_state(path):
    """The state file as a dict, {} when missing, None when half-written."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else {}


def _write_state(path, data):
    """Atomic replace. Retried: on Windows the rename is refused while another
    process (a 1 s poller, --mode post, a second hook) has the file open."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    last = None
    for _ in range(40):
        try:
            os.replace(str(tmp), str(path))
            return True
        except PermissionError as exc:
            last = exc
            time.sleep(0.05)
    _unlink(tmp)
    raise last if last else OSError("could not write state")


class _StateLock:
    """O_EXCL lock file guarding every state transition for one tip.

    Held for milliseconds. A lock older than LOCK_STALE_S belongs to a process
    that died between claim and release and is broken by the next taker.
    """

    def __init__(self, state_path):
        self.path = Path(str(state_path) + ".lock")
        self.fd = None
        self.token = f"{os.getpid()}:{_new_run_id()}"

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_STALE_S + 5
        while True:
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
                os.write(self.fd, self.token.encode())
                os.close(self.fd)
                self.fd = None
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > LOCK_STALE_S:
                        _unlink(self.path)
                        continue
                except OSError:
                    continue
                if time.monotonic() > deadline:
                    raise ReviewGateError("could not acquire the review state lock")
                time.sleep(0.1)

    def __exit__(self, *exc):
        # Release only a lock that is still OURS. If a waiter judged us stale
        # and took the lock, unlinking here would free it from under them.
        try:
            if self.path.read_text(encoding="utf-8") == self.token:
                _unlink(self.path)
        except OSError:
            pass
        return False


def _new_run_id():
    import secrets
    return secrets.token_hex(8)


def _supervisor_env():
    """Environment for the supervisor: the parent's, minus the session bridge
    (see _SESSION_BRIDGE_ENV), minus git's hook exports, and NOT in-review --
    the supervisor is the gate, only the reviewer it spawns is the review."""
    env = dict(os.environ)
    for name in _SESSION_BRIDGE_ENV + _GIT_ENV_SCRUB + ("OCR_IN_REVIEW",):
        key = name.upper() if os.name == "nt" else name
        env.pop(key, None)
    return env


def _spawn_supervisor(state_path, run_id, repo_root):
    """Start `--mode supervise` fully detached from this hook.

    Detached means: own process group, no console, none of this process's
    stdio (the hook's stdout is Claude Code's pipe -- an inherited handle
    there would keep the hook "running" until the review ended, which is
    precisely the hang this replaces; with all three std handles redirected
    and close_fds=True, CPython >= 3.7 passes ONLY those three to the child).
    On Windows the supervisor also breaks out of any job object, so a host
    that kills its job on close cannot take the review down with the CLI.
    Verified 2026-09-22 through both the bash and the PowerShell adapters:
    the hook's stdout reaches EOF in < 0.3 s while the child runs on.
    """
    log = Path(str(state_path)[:-5] + ".supervisor.log")
    cmd = [sys.executable, os.path.abspath(__file__), "--mode", "supervise",
           "--state", str(state_path), "--run-id", run_id]
    kw = dict(cwd=repo_root or None, stdin=subprocess.DEVNULL, close_fds=True,
              env=_supervisor_env())
    with log.open("ab") as fh:
        kw["stdout"] = fh
        kw["stderr"] = fh
        if sys.platform == "win32":
            base = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
            try:
                return subprocess.Popen(cmd, creationflags=base | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw).pid
            except OSError:
                return subprocess.Popen(cmd, creationflags=base, **kw).pid
        return subprocess.Popen(cmd, start_new_session=True, **kw).pid


def _drive_review(common_dir, repo_root, meta, mode, budget):
    """Get a verdict for meta["tip"], starting a review if none is under way.

    Returns the terminal state dict (state "done" or "failed"), or None when
    the inline budget ran out with the review still running. Never raises for
    a review problem -- those become "failed" states with a reason.
    """
    tip = meta["tip"]
    state_path = _state_path(common_dir, tip)
    force = os.environ.get("OCR_FORCE_REVIEW", "").strip().lower() in ("1", "true", "yes")
    deadline = _HOOK_T0 + budget
    forced_once = False
    while True:
        st = _read_state(state_path)
        if st is None:  # mid-write by someone else; look again
            time.sleep(0.1)
            continue
        s = st.get("state")
        now = time.time()
        # A non-terminal state from an older protocol is stale: start fresh.
        if s in ("running", "claimed", "failed"):
            if int(st.get("protocol_version") or 0) < PROTOCOL_VERSION:
                s = None  # treat as absent; fall through to (re)start
        if s == "done":
            if now - float(st.get("done_ts") or 0) < MARKER_TTL and not (force and not forced_once):
                return st
        elif s in ("running", "claimed"):
            beat = float(st.get("heartbeat_ts") or st.get("claimed_ts") or 0)
            if now - beat < STALE_S:
                if time.time() >= deadline:
                    return None
                time.sleep(POLL_S)
                continue
            # silent too long: the supervisor is dead. Fall through to restart.
        elif s == "failed":
            fresh = now - float(st.get("failed_ts") or 0) < MARKER_TTL
            # Immediate deny for a usage-limit failure: do not restart until
            # resets_at has passed (or the 15-min default hold expires), unless
            # OCR_FORCE_REVIEW=1 overrides.
            if fresh and st.get("reason") == "limit" and not (force and not forced_once):
                resets_at = st.get("resets_at")
                if resets_at is not None:
                    if now < float(resets_at):
                        return st
                else:
                    # Unknown reset time: 15-minute default hold.
                    if now - float(st.get("failed_ts") or 0) < 900:
                        return st
            if fresh and int(st.get("attempts") or 0) >= ATTEMPT_CAP and not (force and not forced_once):
                return st
            if forced_once:
                return st  # the run THIS call started failed; retry on the next push, not now
        # (Re)start, under the lock, re-checking that nobody beat us to it.
        with _StateLock(state_path):
            st2 = _read_state(state_path) or {}
            if st2 != st and st2.get("state") in ("running", "claimed", "done"):
                if not (force and not forced_once):
                    continue  # someone else moved it; re-evaluate
            stale_pids = ()
            if st2.get("state") in ("running", "claimed"):
                pids = st2.get("reviewer_pids")
                stale_pids = tuple(dict.fromkeys(
                    (list(pids) if isinstance(pids, list) else [])
                    + [st2.get("reviewer_pid"), st2.get("supervisor_pid")]))
            attempts = int(st2.get("attempts") or 0) if st2.get("state") == "failed" else 0
            if force:
                attempts = 0
            run_id = _new_run_id()
            new = dict(meta)
            new.update({
                "state": "claimed", "run_id": run_id, "claimed_ts": time.time(),
                "mode": mode, "attempts": attempts,
                "protocol_version": PROTOCOL_VERSION,
            })
            _write_state(state_path, new)
            forced_once = True
            # The old run is fenced by the new run_id already (its next
            # heartbeat sees it and exits); the kill is belt and braces and
            # happens outside the lock so a slow taskkill cannot make a
            # legitimate hold look stale to a waiter.
        for pid in stale_pids:
            _tree_kill(pid)
        try:
            # Nothing is written here after the spawn: the supervisor
            # records its own pid, and a write from this side could land
            # on top of its `running` transition.
            _spawn_supervisor(state_path, run_id, repo_root)
        except Exception as exc:
            new.update({"state": "failed", "failed_ts": time.time(),
                        "attempts": attempts + 1, "reason": "spawn",
                        "detail": f"could not start the review supervisor ({exc})"})
            _write_state(state_path, new)
            return new


def _supervise(state_path, run_id):
    """_supervise_run, then one line in the local run log (best-effort)."""
    _TELE.clear()
    _TELE.update({"ts": time.time(), "run_id": run_id})
    _PHASE_CLOCK["t"] = time.monotonic()
    rc = 1
    try:
        rc = _supervise_run(state_path, run_id)
        return rc
    finally:
        # Everything after the last named phase: merging, dedup, the verdict
        # and state writes -- or, when the run failed, the time it died in.
        try:
            _mark_phase("finish" if rc == 0 else "failed")
            _metric_log("run", s=round(time.time() - _TELE["ts"], 1), rc=rc,
                        calls=len(_TELE.get("calls") or []),
                        chunks=len(_TELE.get("chunks") or []))
        except Exception:
            pass
        try:
            _write_telemetry(state_path, run_id)
        except Exception:
            pass


def _write_telemetry(state_path, run_id):
    if not ocr_telemetry.enabled():
        return
    st = _read_state(Path(state_path)) or {}
    if st.get("run_id") != run_id:
        return  # fenced: the run that took over logs its own line
    rec = dict(_TELE)
    rec.update({
        "repo": st.get("repo_root") or "", "tip": st.get("tip") or "",
        "base": st.get("base") or "", "branch": st.get("branch") or "",
        "mode": st.get("mode") or "", "state": st.get("state") or "",
        "verdict": st.get("verdict") or "", "blocked": bool(st.get("blocked")),
        "reason": st.get("reason") or "",
        "seconds": round(time.time() - rec.get("ts", time.time()), 1),
    })
    ocr_telemetry.append(common_dir_of(Path(state_path)), rec)


def _run_review_with_retry(call):
    """Run `call()` and retry once on a parse failure (exit 0, unparseable output)."""
    for attempt in range(2):
        try:
            return call()
        except ReviewGateError as exc:
            if attempt == 0 and exc.is_parse_failure and not exc.is_timeout:
                _warn("[gate] parse failure on first attempt; retrying once.")
                continue
            raise


def _supervise_run(state_path, run_id):
    """The detached worker: run ONE review for the tip named in state_path.

    Writes `running` with a heartbeat every HEARTBEAT_S; a hook that sees no
    heartbeat for STALE_S presumes this process dead and restarts under a new
    run_id -- and this process, seeing a run_id that is no longer its own,
    kills its reviewer and leaves (fencing). Ends by writing `done` (with the
    verdict and the sanitised findings text a retry replays) or `failed`
    (with why). Never touches inherited stdio; never raises out.
    """
    import threading

    state_path = Path(state_path)
    st = _read_state(state_path) or {}
    if st.get("run_id") != run_id:
        return 0  # superseded before we even started
    repo_root = st.get("repo_root") or os.getcwd()
    tip = st.get("tip") or ""
    branch = st.get("branch") or ""
    push_range = st.get("range") or ""
    base = st.get("base") or ""
    git_dir = st.get("git_dir") or _git_dir(repo_root)
    mode = st.get("mode") or "hook"
    common_dir = common_dir_of(state_path)
    now = time.time()
    st.update({"state": "running", "supervisor_pid": os.getpid(), "started_ts": now,
               "heartbeat_ts": now, "deadline_ts": now + TIMEOUT})
    try:
        _write_state(state_path, st)
    except Exception:
        return 1

    stop = threading.Event()
    fenced = {"hit": False}

    def _beat():
        while not stop.wait(HEARTBEAT_S):
            fields = {"heartbeat_ts": time.time()}
            children = _active_children()
            if children:
                fields["reviewer_pid"] = children[0].pid     # pre-0.12.0 readers
                fields["reviewer_pids"] = [c.pid for c in children]
            try:
                _update_state_owned(state_path, run_id, **fields)
            except _Fenced:
                fenced["hit"] = True
                _kill_active_children()
                return
            except Exception:
                pass

    t = threading.Thread(target=_beat, daemon=True)
    t.start()

    worktree, cwd_note = "", ""
    # failure is (reason_str, detail_str)
    failure = None
    limit_info = None   # (resets_at, chunks_done, chunks_total) for limit failures
    result, ran, raw_name = None, False, ""
    chunks_new = 0   # chunks reviewed in this run
    progress = {"new": 0}
    plan_summary = ""
    diffs = None     # precomputed diffs (_build_diffs); None on the 0.9.x path
    try:
        worktree = _make_worktree(repo_root, tip, run_id)
        if worktree:
            review_root = worktree
            # Record worktree path in state so the reaper can protect it.
            try:
                _update_state_owned(state_path, run_id, worktree=worktree)
            except _Fenced:
                _remove_worktree(repo_root, worktree)
                return 0
        else:
            review_root = repo_root
            cwd_note = (
                "review-gate could not create a detached worktree for this tip, so the "
                "reviewer read the LIVE working tree; findings may describe files as they "
                "were during the review rather than at the pushed commit."
            )
        _mark_phase("worktree", ok=bool(worktree))

        force_review = os.environ.get("OCR_FORCE_REVIEW", "").strip().lower() in ("1", "true", "yes")
        fp = _compute_fingerprint(review_root, tip)
        _TELE.update(fp=fp, fp_parts=_fingerprint_parts(review_root, tip))
        _prune_ledger(common_dir)
        plan, planner_warnings = _plan_review(review_root, base, tip, common_dir, fp)
        _TELE["plan"] = [
            {"path": p["entry"]["path"], "mode": p["mode"],
             "miss_reason": p.get("miss_reason") or "",
             "delta_lines": p.get("delta_lines"), "full_lines": p.get("full_lines"),
             "chain_depth": (p.get("record") or {}).get("chain_depth")}
            for p in (plan or [])
        ]
        _mark_phase("plan", files=len(plan or []))

        if plan is None or plan == []:
            # git diff failed OR no allowed files: fall back to single-context (0.7.0 path).
            result, ran, raw_name = _run_review_with_retry(
                lambda: _run_review(review_root, mode, git_dir, tip, push_range)
            )
            chunks_new = 1 if ran else 0
            _mark_phase("review", chunks=chunks_new)
            if planner_warnings and isinstance(result, dict):
                result = dict(result)
                result["warnings"] = (_planner_warning_objs(planner_warnings)
                                      + list(result.get("warnings") or []))
        else:
            # Part S: a flagged-truncated record of a file that CAN be reviewed in
            # units is no longer carried -- the file gets its proper review.
            seg = None
            if _seg_enabled():
                seg = _SegState(review_root, base, tip, common_dir, fp,
                                run_id, _ledger_enabled() and not force_review)
                seg.convert_truncated_carries(plan)
            active_items = [p for p in plan if p["mode"] in ("delta", "full")]
            carry_items = [p for p in plan if p["mode"] == "carry"]
            carry_paths = [p["entry"]["path"] for p in carry_items]
            push_paths = {p["entry"]["path"] for p in plan}

            # Priors are classified before the review: their known defects go
            # to the reviewer, and the resolver judges them afterwards.
            to_resolve, auto_resolved, carried_findings = _classify_priors(
                plan, tip, review_root, common_dir, fp, run_id
            )
            _mark_phase("priors", priors=len(to_resolve) + len(carried_findings))
            impact = _compute_impact(review_root, tip, active_items)
            if impact and impact.get("warnings"):
                _TELE["impact_warnings"] = impact["warnings"][:50]
            defects, defect_notes = _known_defects(
                review_root, tip, [p["finding"] for p in to_resolve] + carried_findings,
                push_paths)
            _mark_phase("impact", sites=len((_TELE.get("impact") or {}).get("sites") or []))

            def _review_extras(k, chunk_paths, chunk_items=None):
                extras = {}
                bundle = _impact_bundle(review_root, tip, impact, chunk_paths,
                                        seg=seg, k=k, chunk_items=chunk_items)
                if bundle:
                    extras["impact"] = bundle
                if defects and k == 0:  # each defect needs judging once
                    extras["known_defects"] = defects
                return extras

            # Files that timed out in an earlier run: retried in smaller chunks (even
            # when few enough for one context), and given up on after two timeouts
            # on their own. OCR_FORCE_REVIEW tries them once more.
            timeout_marks = _timeout_marks(common_dir, fp, active_items)
            if timeout_marks and not force_review:
                stuck = _stuck_paths(timeout_marks)
                if stuck:
                    raise ReviewUnreviewableError(_unreviewable_message(stuck, _CHUNK_TIMEOUT))

            # Precomputed diffs: Python builds every active file's diff once, here,
            # so chunks can be packed by what they will really deliver and the
            # reviewer reads files instead of having diffs retyped into its prompt.
            diffs = None
            if active_items and _precomputed_enabled():
                diffs = _build_diffs(review_root, base, tip, active_items)
                planner_warnings = list(planner_warnings or []) + _diff_warnings(diffs)
                _mark_phase(
                    "diffs", files=len(diffs),
                    lines=sum(d["lines"] for d in diffs.values()),
                    bytes=sum(d["bytes"] for d in diffs.values()),
                    truncated=sum(1 for d in diffs.values() if d["truncated"]),
                    fallback=sum(1 for d in diffs.values() if d["failed"]),
                    binary=sum(1 for d in diffs.values() if d["binary"]))

            # Part S: files over Part B's limit are reviewed in units, with their callers
            # checked; what is already cached replays here.
            seg_local, seg_foreign, seg_unverified = [], [], []
            if seg is not None and diffs is not None and active_items:
                seg.plan_active(active_items, diffs)
                seg.annotate_impact(impact)
                if seg.files or seg.declined:
                    _mark_phase("segment", files=len(seg.files), units=seg.stats["units"],
                                hits=seg.stats["hits"], deps=seg.stats["deps"],
                                declined=seg.stats["declined"])
                seg_local, seg_foreign, seg_unverified = seg.report()
                prior_ids = {p["id"] for p in to_resolve} | {
                    f.get("id") or _finding_id(f) for f in carried_findings}
                more_resolve, more_carried = _seg_classify_foreign(
                    seg_foreign, review_root, tip, common_dir, fp, set(prior_ids))
                to_resolve += more_resolve
                carried_findings += more_carried
                seg_local = _seg_suppress_resolved(seg_local, prior_ids, review_root, tip, common_dir, fp)

            def _single(call):
                """The single-context review: a timeout there marks its files too."""
                try:
                    return _run_review_with_retry(call)
                except ReviewGateError as exc:
                    if not exc.is_timeout:
                        raise
                    if fenced["hit"]:
                        raise _Fenced()
                    err = _timeout_failure(exc, common_dir, fp, active_items, TIMEOUT, "review")
                    if err is exc:
                        raise
                    raise err from exc

            if not active_items:
                # All files already reviewed in a prior run: replay from ledger.
                ran = True  # got a valid result (from records)
                result = {"status": "replayed", "findings": [], "warnings": []}
                chunks_new = 0
                plan_summary = f"carried {len(carry_items)} file(s), 0 reviewed"
            elif (len(active_items) > _CHUNK_THRESHOLD or timeout_marks
                  or (seg is not None and seg.files)):
                # Multi-chunk path.
                result, ran, raw_name, chunks_new = _run_chunked(
                    state_path, run_id, common_dir, review_root, mode, git_dir,
                    tip, push_range, active_items, planner_warnings, fenced, progress,
                    fp=fp, carry_paths=carry_paths, chunk_extras=_review_extras,
                    timeout_marks=timeout_marks, diffs=diffs, seg=seg, impact=impact,
                )
                n_delta = sum(1 for p in active_items if p["mode"] == "delta")
                n_full = len(active_items) - n_delta
                plan_summary = (
                    f"reviewed {len(active_items)} file(s) ({n_delta} delta, {n_full} full)"
                    + (f", carried {len(carry_items)}" if carry_items else "")
                )
            else:
                # Single-context path.
                all_full = all(p["mode"] == "full" for p in active_items)
                extras = _review_extras(0, [p["entry"]["path"] for p in active_items])
                if diffs is None and all_full and not carry_items and not extras:
                    # Golden argv: byte-identical to 0.7.0 (no --paths-file).
                    # Only with OCR_PRECOMPUTED_DIFFS=0: otherwise a manifest is
                    # always written, since the diffs reach the reviewer through it.
                    result, ran, raw_name = _single(
                        lambda: _run_review(review_root, mode, git_dir, tip, push_range)
                    )
                    chunks_new = 1 if ran else 0
                else:
                    # Single context with paths-file (some delta or some carry).
                    manifest_path = str(
                        _async_dir(common_dir) / f"manifest-{run_id}-sc.json"
                    )
                    manifest = _build_review_manifest(
                        common_dir, run_id, None, None, active_items, [], carry_paths,
                        extras, diffs)
                    _write_manifest_file(manifest_path, manifest,
                                         "single-context manifest")
                    try:
                        result, ran, raw_name = _single(
                            lambda: _run_review(
                                review_root, mode, git_dir, tip, push_range,
                                paths_file=manifest_path,
                            )
                        )
                    finally:
                        try:
                            Path(manifest_path).unlink(missing_ok=True)
                        except Exception:
                            pass
                        if diffs is not None:
                            _remove_run_dir(common_dir, run_id, sub=0)
                    chunks_new = 1 if ran else 0
                if ran and _ledger_enabled():
                    _write_run_records(result, active_items, common_dir, fp, run_id,
                                       review_root, tip,
                                       precomputed=_owned_truncation(diffs))
                n_delta = sum(1 for p in active_items if p["mode"] == "delta")
                n_full = len(active_items) - n_delta
                plan_summary = (
                    f"reviewed {len(active_items)} file(s) ({n_delta} delta, {n_full} full)"
                    + (f", carried {len(carry_items)}" if carry_items else "")
                )

            _mark_phase("review", chunks=chunks_new)
            if seg is not None:
                seg_unverified = seg.report()[2]       # includes the checks that stayed unsure just now
                if seg.segmented_paths():
                    plan_summary += (f"; {len(seg.segmented_paths())} big file(s) in units "
                                     f"({seg.stats['hits']} of {seg.stats['units']} cached)")
            if planner_warnings and isinstance(result, dict):
                result = dict(result)
                result["warnings"] = (_planner_warning_objs(planner_warnings)
                                      + list(result.get("warnings") or []))

            # Handle prior findings from carry/delta records (classified above).
            resolver_results, resolver_warnings, judged = {}, [], {}
            if to_resolve:
                resolver_results, resolver_warnings = _run_resolver(
                    review_root, mode, git_dir, tip, push_range,
                    to_resolve, active_items, common_dir, fp, run_id, diffs=diffs,
                )
                tip_cache = {}

                def _judge_all(items):
                    for p in items:
                        judged[p["id"]] = _judge_resolution(
                            p, resolver_results[p["id"]], active_items, push_range,
                            review_root, tip, tip_cache)

                _judge_all(to_resolve)
                _mark_phase("resolve", priors=len(to_resolve))
                # An answer nothing backs gets one more look, told to read the tip.
                recheck = [p for p in to_resolve if judged[p["id"]] == "unverified"]
                if recheck:
                    again, more = _run_resolver(
                        review_root, mode, git_dir, tip, push_range,
                        recheck, active_items, common_dir, fp, run_id, recheck=True,
                        diffs=diffs,
                    )
                    resolver_results.update(again)
                    resolver_warnings += more
                    _judge_all(recheck)
                    _mark_phase("recheck", priors=len(recheck))
                _TELE["resolver"] = {
                    "sent": len(to_resolve), "recheck": len(recheck),
                    "warnings": list(resolver_warnings), "judged": dict(judged),
                }
                n_unverified = sum(1 for v in judged.values() if v == "unverified")
                if n_unverified:
                    resolver_warnings.append(
                        f"resolver: {n_unverified} prior finding(s) unverified - no evidence "
                        "they were fixed, and the flagged code is no longer at the tip; "
                        "kept as blocking until a resolver run can back a verdict")
                if resolver_warnings and isinstance(result, dict):
                    result = dict(result)
                    result["warnings"] = (list(result.get("warnings") or [])
                                          + _planner_warning_objs(resolver_warnings))

            # Process resolver output: guard check, defer writing resolutions until
            # after the reviewer dedup check (scenario: reviewer overrides resolver).
            provisional_resolved = []  # (p, res, ev_path, ev_blob)
            still_present = []
            for p in to_resolve:
                fid = p["id"]
                res = resolver_results.get(fid)
                if not isinstance(res, dict):
                    res = _no_evidence()
                ev_path = res.get("evidence_path") or ""
                ev_blob = (_blob_oids_at(review_root, tip, [ev_path]).get(ev_path, "")
                           if ev_path else "")
                verdict_p = judged.get(fid, "unverified")
                if verdict_p == "resolved":
                    provisional_resolved.append((p, res, ev_path, ev_blob))
                else:
                    f = dict(p["finding"])
                    f, _ = _reanchor_finding(f, review_root, tip)
                    still_present.append(dict(f, provenance=verdict_p))

            # Re-anchor carried findings and mark provenance.
            anchored_carried = []
            for f in carried_findings:
                f2, _ = _reanchor_finding(f, review_root, tip)
                anchored_carried.append(f2)

            # Merge new findings (from reviewer) with priors.
            prior_findings = still_present + anchored_carried + seg_local
            if isinstance(result, dict):
                new_findings = list(result.get("findings") or [])
                site_ids = {s.get("id") for s in (_TELE.get("impact") or {}).get("sites") or []}
                defect_ids = {d.get("sid") for d in defects}
                for nf in new_findings:
                    if nf.get("sibling_of") and nf["sibling_of"] in defect_ids:
                        nf.setdefault("provenance", "sibling")
                    elif nf.get("impact_site") and nf["impact_site"] in site_ids:
                        nf.setdefault("provenance", "impact")
                    nf.setdefault("provenance", "new")
                # Python dedup: drop new findings that nearly duplicate a still_present.
                # Detect if the reviewer re-confirms a provisionally-resolved finding.
                reviewer_override = set()
                deduped_new = []
                # A flagged-truncated record Part S re-reviews still carries what the partial
                # review found: the proper review must not report the same defect again.
                reviewed_again = set(seg.trials) if seg is not None else set()
                held = still_present + [f for f in anchored_carried if f.get("path") in reviewed_again]
                for nf in new_findings:
                    if any(_findings_similar(nf, sp) for sp in held):
                        continue
                    deduped_new.append(nf)
                    for prov_p, _, _, _ in provisional_resolved:
                        if _findings_similar(nf, prov_p["finding"]):
                            reviewer_override.add(prov_p["id"])

                # Write resolutions only for findings NOT overridden by the reviewer.
                self_resolved = {}
                for p, res, ev_path, ev_blob in provisional_resolved:
                    if p["id"] in reviewer_override:
                        continue
                    _write_resolution(
                        common_dir, fp, p["id"],
                        p.get("target_oid") or p["record"].get("head_oid") or "",
                        ev_path, ev_blob,
                        res.get("evidence_quote") or "",
                        run_id,
                    )
                    if ev_path and ev_path == (p["finding"].get("path") or ""):
                        self_resolved.setdefault(ev_path, set()).add(p["id"])
                if self_resolved and _ledger_enabled():
                    _drop_self_resolved(active_items, self_resolved, common_dir, fp, run_id)
                if ran and carry_items and _ledger_enabled():
                    _attach_to_carried_records(deduped_new, carry_items, common_dir, fp,
                                               run_id, review_root, tip)

                # Info notes: same code as a finding, elsewhere. Never blocking,
                # never recorded; a prior's in-push matches went to the reviewer.
                notes = defect_notes + _new_finding_notes(
                    review_root, tip,
                    [nf for nf in deduped_new if nf.get("provenance") == "new"], push_paths)

                all_findings = deduped_new + prior_findings + notes + seg_unverified
                result = dict(result, findings=all_findings)
                result = _surface_truncated(result, plan, common_dir, fp,
                                            set(_owned_truncation(diffs)))
                if plan_summary:
                    result = dict(result, plan_summary=plan_summary)

        if plan is not None and plan_summary and isinstance(result, dict):
            result.setdefault("plan_summary", plan_summary)
    except _Fenced:
        stop.set()
        _remove_run_dir(common_dir, run_id)
        if worktree:
            _remove_worktree(repo_root, worktree)
        return 0  # a newer run owns this tip now; say nothing
    except ReviewLimitError as exc:
        # Usage limit: record without incrementing attempts.
        cur_st = _read_state(state_path) or {}
        limit_info = (
            exc.resets_at,
            int(cur_st.get("chunks_done") or 0),
            int(cur_st.get("chunks_total") or 0),
        )
        failure = ("limit", str(exc))
        result, ran, raw_name, chunks_new = None, False, "", 0
    except ReviewBudgetError as exc:
        # Budget exhaustion: not an attempt; next push resumes from checkpoint.
        failure = ("budget", str(exc))
        result, ran, raw_name, chunks_new = None, False, "", progress["new"]
    except ReviewChunkTimeout as exc:
        # A timeout whose files are marked for splitting: progress, not an attempt.
        failure = ("timeout", str(exc))
        result, ran, raw_name, chunks_new = None, False, "", progress["new"]
    except ReviewUnreviewableError as exc:
        failure = ("unreviewable", str(exc))
        result, ran, raw_name, chunks_new = None, False, "", progress["new"]
    except ReviewGateError as exc:
        failure = ("review", str(exc))
        result, ran, raw_name, chunks_new = None, False, "", progress["new"]
    except BaseException as exc:  # noqa: BLE001 -- the file must always say why
        failure = ("crash", f"{type(exc).__name__}: {exc}")
        result, ran, raw_name, chunks_new = None, False, "", progress["new"]
    finally:
        stop.set()
        _remove_run_dir(common_dir, run_id)
        if worktree:
            _remove_worktree(repo_root, worktree)

    st = _read_state(state_path) or st
    if st.get("run_id") != run_id or fenced["hit"]:
        return 0  # a newer run owns this tip now; say nothing
    if failure is not None:
        if failure[0] == "limit":
            resets_at, cd, ct = limit_info
            st.update({
                "state": "failed", "failed_ts": time.time(),
                "reason": "limit", "detail": _sanitize(failure[1], 1500),
                "resets_at": resets_at,
                "chunks_done": cd, "chunks_total": ct,
                # attempts unchanged: a limit is not an attempt
            })
        elif failure[0] == "budget":
            # Budget exhaustion: not an attempt; resume on next push.
            # Preserve the chunks_done written by _run_chunked.
            st.update({
                "state": "failed", "failed_ts": time.time(),
                "reason": "budget", "detail": _sanitize(failure[1], 1500),
                # attempts unchanged; chunks_done already in state from _run_chunked
            })
        elif failure[0] == "timeout":
            # The timed-out chunk's files are marked: the next push splits it, so
            # this is progress toward a verdict, not an attempt that burns the cap.
            st.update({
                "state": "failed", "failed_ts": time.time(),
                "reason": "timeout", "detail": _sanitize(failure[1], 1500),
            })
        elif failure[0] == "unreviewable":
            # Terminal for this tip: re-running the same call is the loop this
            # exists to end, so the attempt cap is reached at once.
            st.update({
                "state": "failed", "failed_ts": time.time(), "attempts": ATTEMPT_CAP,
                "reason": "unreviewable", "detail": _sanitize(failure[1], 1500),
            })
        else:
            # Increment attempts only when no new chunks were reviewed.
            new_attempts = int(st.get("attempts") or 0) + (
                1 if chunks_new == 0 else 0
            )
            st.update({
                "state": "failed", "failed_ts": time.time(),
                "attempts": new_attempts,
                "reason": failure[0], "detail": _sanitize(failure[1], 1500),
            })
        try:
            _write_state(state_path, st)
        except Exception:
            pass
        return 1
    if not ran:
        # claude not installed: the one deliberately fail-open case.
        st.update({"state": "done", "done_ts": time.time(), "verdict": "skipped",
                   "blocked": False, "reasons": "", "finding_count": 0,
                   "note": "`claude` CLI not found - review skipped (fail-open)."})
        _write_state(state_path, st)
        return 0
    if cwd_note and isinstance(result, dict):
        result = dict(result)
        result["findings"] = [{
            "severity": "info", "path": "review-gate", "start_line": "-",
            "end_line": "-", "content": cwd_note,
        }] + list(result.get("findings") or [])
    verdict = compute_verdict(result)
    if isinstance(result, dict):
        _TELE["findings"] = [
            {"id": f.get("id") or _finding_id(f), "path": f.get("path"),
             "start_line": f.get("start_line"), "severity": f.get("severity"),
             "confidence": f.get("confidence"), "category": f.get("category"),
             "provenance": f.get("provenance") or "new",
             "content": str(f.get("content") or "")[:300]}
            for f in (result.get("findings") or []) if isinstance(f, dict)
        ]
        _TELE["warnings"] = [w if isinstance(w, dict) else {"message": str(w)}
                             for w in (result.get("warnings") or [])][:50]
        if isinstance(result.get("cross_file_context_summary"), dict):
            _TELE["model_cross_file"] = result["cross_file_context_summary"]
        if isinstance(result.get("impact_verdicts"), dict):
            _TELE["site_verdicts"] = result["impact_verdicts"]
    reasons = _format_reasons(result)
    advisory = _is_advisory(repo_root)
    blocked = verdict == "block" and not advisory
    record = _record_review(git_dir, tip, branch, mode, verdict, advisory, blocked, result, raw_name)
    if not blocked:
        # The pass-only legacy marker, unchanged: an older global git hook
        # reads its presence as "reviewed and passed", so a block must never
        # be written under it. Blocks replay from this state file instead.
        marker = _marker_path(git_dir, tip) if git_dir else None
        if marker:
            try:
                _write_marker(marker, tip, verdict, advisory, reasons)
                _reap_markers(git_dir, keep=marker)
            except Exception:
                pass
    try:
        _update_state_owned(state_path, run_id, **{
            "state": "done", "done_ts": time.time(), "verdict": verdict,
            "blocked": bool(blocked), "reasons": reasons,
            "finding_count": (
                len(result.get("findings") or []) if isinstance(result, dict) else 0
            ),
            "unreviewed_truncated": (
                len(result.get("unreviewed_truncated") or []) if isinstance(result, dict) else 0
            ),
            "record": str(record) if record else "", "raw": raw_name,
        })
    except _Fenced:
        return 0  # superseded just before the final write; say nothing
    except Exception:
        pass
    _reap_async(common_dir_of(state_path))
    return 0


def common_dir_of(state_path):
    return str(Path(state_path).parent.parent)


def _make_worktree(repo_root, tip, run_id):
    """A detached worktree at `tip` for the reviewer to read, or "".

    The review now runs while the session goes on editing, switching and
    stashing in the live tree, and the skill reads files with Read/Grep -- so
    without this a 20-minute review describes a tree that no longer matches
    the commits being pushed. Also keeps the reviewer's scratch files out of
    the user's tree. Under the plugin data dir, never inside .git.
    """
    try:
        base = _gate_data_dir() / "worktrees"
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{tip[:12]}-{run_id}"
        out, rc = _git(["worktree", "add", "--detach", str(path), tip], cwd=repo_root)
        if rc == 0 and path.is_dir():
            return str(path)
    except Exception:
        pass
    return ""


def _is_gate_worktree(path):
    """True only for a directory this gate made with _make_worktree: under its own
    worktrees/ dir and a linked worktree (a `.git` FILE, never the `.git` directory
    of a real checkout). Anything doubtful is False -- callers use this to decide
    whether a destructive reset (git clean / checkout) may run there."""
    try:
        base = os.path.normcase(os.path.realpath(str(_gate_data_dir() / "worktrees")))
        real = os.path.realpath(str(path))
        if not os.path.normcase(real).startswith(base + os.sep):
            return False
        return os.path.isfile(os.path.join(real, ".git"))
    except Exception:
        return False


def _remove_worktree(repo_root, path):
    """Tear down a worktree _make_worktree created. Refuses anything else:
    the only directory this gate ever deletes is one under its own
    worktrees/ dir, never a path handed to it by mistake."""
    try:
        base = os.path.realpath(str(_gate_data_dir() / "worktrees"))
        real = os.path.realpath(str(path))
        if not real.startswith(base + os.sep):
            return
        _git(["worktree", "remove", "--force", path], cwd=repo_root)
        if os.path.isdir(real):
            shutil.rmtree(real, ignore_errors=True)
        _git(["worktree", "prune"], cwd=repo_root)
    except Exception:
        pass


def _reap_async(common_dir):
    """Drop async state/logs older than MARKER_TTL, and the retired chunks/ cache.
    Also the run-*/ diff directories of runs that died (0.10.0).

    The worktree sweep skips any worktree owned by a running state whose
    heartbeat is younger than STALE_S.  The 0.7.0 chunk cache (chunks/) is
    removed outright: a resumed run now carries per-file ledger records, which
    have their own TTL (OCR_LEDGER_TTL) and are pruned by _prune_ledger.
    """
    try:
        now = time.time()
        cutoff = now - MARKER_TTL

        # Collect live-run worktree paths so the sweep below can skip them.
        live_worktrees = set()
        live_runs = set()
        for p in _async_dir(common_dir).glob("*.json"):
            try:
                st = _read_state(p) or {}
                if st.get("state") in ("running", "claimed"):
                    hb = float(st.get("heartbeat_ts") or st.get("claimed_ts") or 0)
                    if now - hb < STALE_S:
                        live_runs.add(str(st.get("run_id") or ""))
                        wts = st.get("worktrees")
                        for wt in [st.get("worktree")] + (wts if isinstance(wts, list) else []):
                            if wt:
                                live_worktrees.add(os.path.realpath(str(wt)))
            except Exception:
                continue

        for p in _async_dir(common_dir).glob("*"):
            try:
                if p.is_dir() and p.name == "chunks":
                    # 0.7.0 chunk cache: remove entirely; 0.8.0 uses the ledger.
                    shutil.rmtree(p, ignore_errors=True)
                    continue
                if p.is_dir() and p.name.startswith("run-"):
                    # A run's precomputed diffs (0.10.0). The run removes them itself
                    # when it ends; what is left here belongs to a run that died.
                    if (p.name[4:] not in live_runs and p.stat().st_mtime < cutoff
                            and not p.is_symlink()):
                        shutil.rmtree(p, ignore_errors=True)
                    continue
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                continue

        wts = _gate_data_dir() / "worktrees"
        if wts.is_dir():
            for p in wts.iterdir():
                try:
                    if p.is_dir():
                        if os.path.realpath(str(p)) in live_worktrees:
                            continue  # protected: belongs to a live run
                        if p.stat().st_mtime < cutoff:
                            shutil.rmtree(p, ignore_errors=True)
                except OSError:
                    continue
    except Exception:
        pass


# --- chunking helpers (0.7.0) -------------------------------------------------

# Allowed source extensions — must stay in sync with skills/review/allowlist.md.
# A parity test in tests/test_review_gate.py verifies this.
_ALLOWED_EXTS = frozenset({
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".cs", ".go", ".rs",
    ".java", ".kt", ".kts", ".scala", ".swift", ".py", ".pyi", ".rb",
    ".rake", ".gemspec", ".php", ".pl", ".pm", ".lua", ".r", ".jl", ".dart",
    ".groovy", ".ex", ".exs", ".erl", ".hrl", ".ets", ".clj", ".cljs", ".vb",
    ".fs", ".m", ".mm", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue",
    ".svelte", ".astro", ".sql", ".sh", ".bash", ".zsh", ".fish", ".ps1",
    ".psm1", ".html", ".htm", ".css", ".scss", ".sass", ".less", ".tf",
    ".hcl", ".proto", ".graphql", ".gql", ".ftl", ".ftlh", ".ftlx",
    ".po", ".pot",
})

# Directory names that are always excluded from review.
_EXCLUDED_DIRS = frozenset({
    "vendor", "node_modules", "dist", "build", "out", "target",
    ".next", "__generated__", ".git", ".idea", ".vscode",
    "tests", "__tests__", "testdata",
})

# Exact filenames that are always excluded (lockfiles).
_EXCLUDED_FILES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "go.sum",
    "cargo.lock", "poetry.lock", "composer.lock",
})

_EXCLUDED_SUFFIX_RE = re.compile(
    r'(\.min\.js|\.pb\.go|\.generated\.[^/\\]+)$', re.IGNORECASE
)
_EXCLUDED_TEST_RE = re.compile(
    r'(_test\.go|\.test\.(js|jsx|ts|tsx)|\.spec\.(js|jsx|ts|tsx)'
    r'|/test_[^/]+\.py|/_?[^/]*_test\.py)$', re.IGNORECASE
)
_CTRL_CHAR_RE = re.compile(r'[\x00-\x1f\x7f]')


def _is_allowed_path(path):
    """True if this path should be reviewed per allowlist.md."""
    name = os.path.basename(path)
    if name.lower() in _EXCLUDED_FILES:
        return False
    ext = os.path.splitext(name)[1].lower()
    if ext not in _ALLOWED_EXTS:
        return False
    if _EXCLUDED_SUFFIX_RE.search(name):
        return False
    norm = path.replace("\\", "/")
    parts = norm.split("/")
    if any(p.lower() in _EXCLUDED_DIRS for p in parts[:-1]):
        return False
    if _EXCLUDED_TEST_RE.search(norm):
        return False
    return True


def _blob_oids_at(root, tip, paths):
    """Return {path: oid} for the given paths at tip. Missing paths map to ''.

    -z for the same reason as in _collect_diff_entries: a C-quoted non-ASCII
    path never matched its key, so the file looked deleted at tip.
    """
    if not paths or not tip:
        return {p: "" for p in paths}
    out, rc = _git(
        ["ls-tree", "-r", "-z", "--full-tree", tip, "--"] + list(paths), cwd=root
    )
    result = {}
    if rc == 0:
        for rec in out.split("\0"):
            tab = rec.find("\t")
            if tab == -1:
                continue
            meta, fpath = rec[:tab].split(), rec[tab + 1:]
            if len(meta) >= 3:
                result[fpath] = meta[2]
    for p in paths:
        if p not in result:
            result[p] = ""
    return result


def _ocr_tree_oid(root, tip):
    """SHA1 of the .ocr/ tree at tip, or "" if not present."""
    out, rc = _git(["ls-tree", "--full-tree", tip, "--", ".ocr"], cwd=root)
    if rc != 0 or not out:
        return ""
    parts = out.split()
    return parts[2] if len(parts) >= 3 else ""


def _plugin_version():
    """Version from .claude-plugin/plugin.json, or "" on error."""
    try:
        pj = Path(_PLUGIN_ROOT) / ".claude-plugin" / "plugin.json"
        return json.loads(pj.read_text(encoding="utf-8")).get("version", "")
    except Exception:
        return ""


def _collect_diff_entries(root, base, tip):
    """Return (entries, warnings) for the range base..tip.

    Each entry: {path, old_path, status, old_oid, new_oid, lines}.
    Returns (None, warnings) on a git error.

    Both git calls use -z: without it git's default core.quotePath=true
    C-quotes any non-ASCII path ("caf\\303\\251.py"), which the allowlist then
    rejects on its extension -- an unreviewed file passing the gate. -z
    output is never quoted, and renames arrive as separate fields rather than
    numstat's ambiguous `{a => b}` shorthand.

    --no-abbrev, not --full-index (which only widens patch `index` lines):
    --raw OIDs are otherwise abbreviated, yet they key ledger records and are
    compared with the full blob OIDs from _blob_oids_at.
    """
    raw_out, rc = _git(
        ["diff", "--raw", "-z", "-M", "--no-abbrev", f"{base}..{tip}"], cwd=root
    )
    if rc != 0:
        return None, ["could not run git diff --raw; skipping chunking"]

    stat_out, _ = _git(["diff", "-M", "--numstat", "-z", f"{base}..{tip}"], cwd=root)

    entries = {}
    warnings = []
    # Records are ":meta\0path\0", or ":meta\0src\0dst\0" for R/C.
    fields = raw_out.split("\0")
    i = 0
    while i < len(fields):
        head = fields[i]
        i += 1
        if not head.startswith(":"):
            continue
        meta = head[1:].split()
        if len(meta) < 5:
            continue
        old_oid, new_oid, status_score = meta[2], meta[3], meta[4]
        status = status_score[0]
        n_paths = 2 if status in ("R", "C") else 1
        paths = fields[i:i + n_paths]
        i += n_paths
        if len(paths) < n_paths:
            break
        if status == "D" or new_oid.strip("0") == "":
            continue  # pure deletion
        old_path, new_path = paths[0], paths[-1]
        for p in (old_path, new_path):
            if _CTRL_CHAR_RE.search(p):
                warnings.append(f"skipped {p!r}: path contains control characters")
                break
        else:
            entries[new_path] = {
                "path": new_path,
                "old_path": old_path if old_path != new_path else "",
                "status": status_score,
                "old_oid": old_oid,
                "new_oid": new_oid,
                "lines": 0,
            }

    # Fill in line counts from numstat. Records are "add\tdel\tpath\0", or
    # "add\tdel\t\0src\0dst\0" for a rename/copy (empty path field).
    fields = (stat_out or "").split("\0")
    i = 0
    while i < len(fields):
        parts = fields[i].split("\t", 2)
        i += 1
        if len(parts) < 3:
            continue
        added_s, deleted_s, path_s = parts
        if path_s == "":
            if i + 1 >= len(fields):
                break
            new_path = fields[i + 1]
            i += 2
        else:
            new_path = path_s
        if added_s == "-" or deleted_s == "-":
            continue  # binary
        try:
            lines = int(added_s) + int(deleted_s)
        except ValueError:
            continue
        if new_path in entries:
            entries[new_path]["lines"] = lines

    return list(entries.values()), warnings


def _group_into_chunks(entries, sizes=None, budget=None):
    """Group by top-level directory, splitting at CHUNK_LINES / CHUNK_FILES.

    `sizes` ({path: lines}) and `budget` replace each entry's changed-line count
    and _CHUNK_LINES: the precomputed-diff path packs by the lines of diff it
    actually delivers (_CHUNK_DIFF_LINES).
    """
    limit = budget or _CHUNK_LINES

    def size(e):
        if sizes and e["path"] in sizes:
            return sizes[e["path"]]
        return e["lines"]

    by_dir = {}
    for e in entries:
        top = e["path"].split("/")[0] if "/" in e["path"] else ""
        by_dir.setdefault(top, []).append(e)

    chunks, current, current_lines = [], [], 0
    for dir_entries in by_dir.values():
        for e in dir_entries:
            n = size(e)
            if n > limit:
                # Oversized file: flush, then give it its own chunk.
                if current:
                    chunks.append(current)
                current, current_lines = [], 0
                chunks.append([e])
                continue
            if current and (current_lines + n > limit
                            or len(current) >= _CHUNK_FILES):
                chunks.append(current)
                current, current_lines = [], 0
            current.append(e)
            current_lines += n
    if current:
        chunks.append(current)
    return chunks if chunks else [[]]


def _plan_chunks(root, base, tip):
    """Return (None, warnings) for single-chunk mode, or (chunks, warnings).

    Returns None when the reviewable file count is <= _CHUNK_THRESHOLD so the
    caller uses exactly today's single-context path.  Above the threshold
    returns a list of lists of entry dicts.
    """
    if not base:
        base = _EMPTY_TREE
    entries, warnings = _collect_diff_entries(root, base, tip)
    if entries is None:
        return None, warnings

    allowed = [e for e in entries if _is_allowed_path(e["path"])]
    if not allowed:
        return None, warnings

    if len(allowed) > _MAX_FILES:
        allowed.sort(key=lambda e: e["lines"], reverse=True)
        skipped = len(allowed) - _MAX_FILES
        warnings.append(
            f"file ceiling: {skipped} file(s) skipped (only the {_MAX_FILES} with "
            "the largest diffs are reviewed; set OCR_MAX_FILES to raise the cap)"
        )
        allowed = allowed[:_MAX_FILES]

    if len(allowed) <= _CHUNK_THRESHOLD:
        return None, warnings  # below threshold: single-context mode

    return _group_into_chunks(allowed), warnings


# --- review ledger (0.8.0) ---------------------------------------------------


def _ledger_enabled():
    return os.environ.get("OCR_LEDGER", "1").strip().lower() not in ("0", "false", "no")


def _ledger_dir(common_dir):
    return Path(common_dir) / LEDGER_DIR


def _fp_dir(common_dir, fp):
    return _ledger_dir(common_dir) / fp[:16]


def _record_key(path, old_path, status, base_oid):
    """16-hex key that identifies a file entry independent of its head blob."""
    raw = "\x00".join([path or "", old_path or "", status or "", base_oid or ""])
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def _finding_id(f):
    """Stable sha256 id for a finding (path, existing_code, content)."""
    raw = "\x00".join([
        f.get("path") or "",
        f.get("existing_code") or "",
        f.get("content") or "",
    ])
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def _record_path(common_dir, fp, key, head_oid):
    return _fp_dir(common_dir, fp) / f"{key}-{head_oid[:16]}.json"


def _resolution_path(common_dir, fp, res_id, target_oid, evidence_path, evidence_blob_oid):
    ctx_raw = "\x00".join([target_oid or "", evidence_path or "", evidence_blob_oid or ""])
    ctx16 = hashlib.sha256(ctx_raw.encode("utf-8", "replace")).hexdigest()[:16]
    return _fp_dir(common_dir, fp) / "resolutions" / f"{res_id[:16]}-{ctx16}.json"


def _fingerprint_parts(root, tip):
    """{input name: short hash} for every input of the review CRITERIA: model,
    rubric, language rules, the reviewer and filter prompts, the repo's .ocr/
    tree, and the criteria env vars. Kept by name so the run log can say which
    one changed.

    Mechanics are deliberately absent -- SKILL.md orchestration, the state
    PROTOCOL_VERSION, CLI args, the resolver prompt, the plugin version -- so a
    plugin update does not throw every earlier review away."""
    def digest(data):
        return hashlib.sha256(data).hexdigest()[:16]

    def file_digest(path):
        try:
            return digest(Path(path).read_bytes())
        except OSError:
            return ""

    parts = {"model": digest(_MODEL.encode("utf-8")),
             "rubric.md": file_digest(Path(_PLUGIN_ROOT) / "skills/review/rubric.md")}
    rules = Path(_PLUGIN_ROOT) / "skills/review/rules"
    if rules.is_dir():
        for f in sorted(rules.glob("*.md")):
            parts[f"rules/{f.name}"] = file_digest(f)
    for name in _FINGERPRINT_AGENTS:
        parts[f"agents/{name}"] = file_digest(Path(_PLUGIN_ROOT) / "agents" / name)
    parts[".ocr/"] = _ocr_tree_oid(root, tip) or ""
    for var in _FINGERPRINT_ENV_VARS:
        parts[f"env:{var}"] = digest((os.environ.get(var) or "").encode("utf-8"))
    return parts


def _compute_fingerprint(root, tip):
    """sha256 over _fingerprint_parts. A record reviewed under other criteria
    is not trusted."""
    parts = _fingerprint_parts(root, tip)
    raw = "\x00".join(f"{k}={v}" for k, v in parts.items())
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_truncated_record(record):
    """True for a record whose review saw only part of the file's diff. The flag
    is optional: a record without it (every one written before 0.9.5) is complete."""
    return isinstance(record, dict) and record.get("truncated") is True


def _read_ledger_record(record_path, fp, key, head_oid):
    """Return the record dict or None (miss, corrupt, mismatch, expired).

    The dict is returned as stored, so `truncated` (see _is_truncated_record) is
    in it when it was set."""
    try:
        raw = Path(record_path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("schema") != _LEDGER_SCHEMA:
        return None
    if data.get("fp") != fp or data.get("key") != key or data.get("head_oid") != head_oid:
        return None
    try:
        if time.time() - Path(record_path).stat().st_mtime > _LEDGER_TTL:
            return None
    except OSError:
        return None
    return data


def _write_ledger_record(common_dir, fp, key, head_oid, path, old_path,
                         status, base_oid, findings, chain_depth, run_id, truncated=False):
    """Write one per-file ledger record. Best-effort: never breaks the gate.

    truncated=True marks a review that saw only part of the diff. It is stored
    only when set, so every other record stays byte-identical to 0.9.4's."""
    try:
        rpath = _record_path(common_dir, fp, key, head_oid)
        rpath.parent.mkdir(parents=True, exist_ok=True)
        stamped = [dict(f, id=_finding_id(f)) for f in (findings or [])]
        data = {
            "schema": _LEDGER_SCHEMA, "fp": fp, "key": key,
            "path": path, "old_path": old_path or "",
            "status": status, "base_oid": base_oid, "head_oid": head_oid,
            "findings": stamped, "chain_depth": int(chain_depth or 0),
            "reviewed_ts": time.time(), "run_id": run_id or "",
        }
        if truncated:
            data["truncated"] = True
        _write_state(rpath, data)
        try:
            rpath.touch(exist_ok=True)  # refresh mtime for TTL
        except Exception:
            pass
    except Exception:
        pass


def _find_delta_record(common_dir, fp, key, head_oid):
    """Find the newest valid ledger record for `key` with a *different* head OID.

    Used to identify a delta base: the file was reviewed at X, now at head_oid,
    so we review only X→head_oid. Returns (record, from_oid) or (None, '').
    A truncated record is never a base: its review did not cover all of X.
    """
    fp_d = _fp_dir(common_dir, fp)
    if not fp_d.is_dir():
        return None, ""
    candidates = []
    prefix = key + "-"
    for p in fp_d.glob(f"{prefix}*.json"):
        if p.name == f"{key}-{head_oid[:16]}.json":
            continue  # exact match already checked by caller
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict) or data.get("schema") != _LEDGER_SCHEMA:
            continue
        if data.get("fp") != fp or data.get("key") != key:
            continue
        if _is_truncated_record(data):
            continue  # the reviewer saw only part of that state: nothing to build a delta on
        try:
            if time.time() - p.stat().st_mtime > _LEDGER_TTL:
                continue
        except OSError:
            continue
        candidates.append((data.get("reviewed_ts") or 0, data))
    if not candidates:
        return None, ""
    candidates.sort(reverse=True)
    record = candidates[0][1]
    from_oid = record.get("head_oid") or ""
    return (record, from_oid) if from_oid else (None, "")


def _read_resolution(common_dir, fp, res_id, target_oid, evidence_path, evidence_blob_oid):
    """Return a matching resolution dict, or None."""
    rpath = _resolution_path(
        common_dir, fp, res_id, target_oid, evidence_path, evidence_blob_oid
    )
    try:
        data = json.loads(rpath.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if (data.get("target_oid") != target_oid or
            data.get("evidence_path") != evidence_path or
            data.get("evidence_blob_oid") != evidence_blob_oid):
        return None
    return data


def _find_valid_resolution(common_dir, fp, fid, target_oid, review_root, tip):
    """Return the first valid resolution for finding `fid`, or None.

    A resolution is valid when target_oid matches AND the evidence_path blob
    at the current tip equals the recorded evidence_blob_oid (fix not reverted).
    """
    res_dir = _fp_dir(common_dir, fp) / "resolutions"
    if not res_dir.is_dir():
        return None
    prefix = fid[:16] + "-"
    for p in res_dir.glob(f"{prefix}*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("target_oid") != target_oid:
            continue
        ev_path = data.get("evidence_path") or ""
        ev_blob = data.get("evidence_blob_oid") or ""
        if not ev_path or not ev_blob:
            continue
        current = _blob_oids_at(review_root, tip, [ev_path]).get(ev_path, "")
        if current != ev_blob:
            continue
        return data
    return None


def _write_resolution(common_dir, fp, res_id, target_oid, evidence_path,
                      evidence_blob_oid, evidence_quote, run_id):
    """Persist a finding resolution. Best-effort."""
    try:
        rpath = _resolution_path(
            common_dir, fp, res_id, target_oid, evidence_path, evidence_blob_oid
        )
        rpath.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "res_id": res_id, "target_oid": target_oid,
            "evidence_path": evidence_path, "evidence_blob_oid": evidence_blob_oid,
            "evidence_quote": evidence_quote or "",
            "resolved_ts": time.time(), "run_id": run_id or "",
        }
        _write_state(rpath, data)
    except Exception:
        pass


def _plan_review(root, base, tip, common_dir, fp):
    """Classify every diff entry as carry, delta, or full.

    Returns (plan_items, warnings) where each item is a dict:
      {entry, mode, record, from_oid, miss_reason}
    Returns (None, warnings) when git diff fails (same as _plan_chunks).
    Returns ([], warnings) when no reviewable files.
    """
    if not base:
        base = _EMPTY_TREE
    entries, warnings = _collect_diff_entries(root, base, tip)
    if entries is None:
        return None, warnings

    allowed = [e for e in entries if _is_allowed_path(e["path"])]
    if not allowed:
        return [], warnings

    if len(allowed) > _MAX_FILES:
        allowed.sort(key=lambda e: e["lines"], reverse=True)
        skipped = len(allowed) - _MAX_FILES
        warnings.append(
            f"file ceiling: {skipped} file(s) skipped (only the {_MAX_FILES} with "
            "the largest diffs are reviewed; set OCR_MAX_FILES to raise the cap)"
        )
        allowed = allowed[:_MAX_FILES]

    force = os.environ.get("OCR_FORCE_REVIEW", "").strip().lower() in ("1", "true", "yes")
    use_ledger = _ledger_enabled() and not force

    plan = []
    for e in allowed:
        if not use_ledger:
            plan.append({"entry": e, "mode": "full", "record": None,
                         "from_oid": "", "miss_reason": "none"})
            continue
        key = _record_key(e["path"], e.get("old_path") or "", e["status"], e["old_oid"])
        head_oid = e["new_oid"]
        rec_path = _record_path(common_dir, fp, key, head_oid)
        record = _read_ledger_record(rec_path, fp, key, head_oid)
        if record is not None:
            # An exact hit carries whether or not the record is flagged truncated:
            # re-reviewing the same blob would truncate it the same way.
            plan.append({"entry": e, "mode": "carry", "record": record,
                         "from_oid": head_oid, "miss_reason": "none"})
            continue
        delta_record, from_oid = _find_delta_record(common_dir, fp, key, head_oid)
        if delta_record is not None:
            if _is_truncated_record(delta_record):  # _find_delta_record skips these; belt and braces
                plan.append({"entry": e, "mode": "full", "record": None,
                             "from_oid": "", "miss_reason": "no_record"})
                continue
            delta_lines = _blob_diff_lines(root, from_oid, head_oid)
            full_lines = int(e.get("lines") or 0)
            item = {"entry": e, "mode": "delta", "record": delta_record,
                    "from_oid": from_oid, "miss_reason": "none",
                    "delta_lines": delta_lines, "full_lines": full_lines}
            if delta_lines is not None and full_lines <= _COST_RULE_RATIO * delta_lines:
                # Keeps the record: the findings still owed on it go to the resolver.
                item.update(mode="full", miss_reason="cost_rule")
            plan.append(item)
            continue
        plan.append({"entry": e, "mode": "full", "record": None,
                     "from_oid": "", "miss_reason": "no_record"})
    return plan, warnings


def _blob_diff_lines(root, from_oid, to_oid):
    """Added+removed lines between two blobs, or None when git can't say."""
    out, rc = _git(["diff", "--numstat", from_oid, to_oid], cwd=root)
    if rc != 0:
        return None
    total = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            total += int(parts[0]) + int(parts[1])
    return total


def _classify_priors(plan_items, tip, review_root, common_dir, fp, run_id):
    """Decide the fate of prior findings from carry/delta records.

    Returns (to_resolve, auto_resolved, carried_findings):
      to_resolve   — list of {id, finding, record} for the resolver
      auto_resolved — findings where the target file no longer exists
      carried_findings — findings replayed as-is: any severity on an identical
                         blob, or low/info with nothing to re-judge
    """
    has_active = any(item["mode"] in ("delta", "full") for item in plan_items)
    to_resolve, auto_resolved, carried_findings = [], [], []
    seen = set()

    for item in plan_items:
        record = item.get("record")
        # A cost-rule full item keeps its delta base's record, and with it the
        # findings still owed on that file.
        if record is None:
            continue
        for f in record.get("findings") or []:
            fid = f.get("id") or _finding_id(f)
            if fid in seen:
                continue  # the same finding can sit on several records of one context
            seen.add(fid)
            fpath = f.get("path") or item["entry"]["path"]
            sev = (f.get("severity") or "").lower()
            blob = _blob_oids_at(review_root, tip, [fpath]).get(fpath, "")
            if not blob:
                auto_resolved.append(f)
                continue
            target_oid = f.get("target_oid") or record.get("head_oid") or ""
            if _find_valid_resolution(common_dir, fp, fid, target_oid,
                                      review_root, tip) is not None:
                continue  # suppressed by existing resolution
            # Identical blob: the record was written at the file's current content and
            # the finding is about exactly that content, so nothing in the file can
            # have been fixed since. Every severity replays as-is -- no resolver call
            # (it only ever finds a fix in some OTHER file for such a prior), and no
            # dependence on the flagged code still being locatable.
            if blob == target_oid and record.get("head_oid") == blob:
                carried_findings.append(dict(f, provenance="carried"))
                continue
            # A file that changed since the finding is re-judged even when nothing
            # else is under review: the fix may sit in a push whose resolver run
            # never got recorded, and replaying the finding would block on it forever.
            changed = bool(target_oid) and blob != target_oid
            if sev in ("high", "medium", "critical") and (has_active or changed):
                to_resolve.append({"id": fid, "finding": f, "record": record,
                                   "target_oid": target_oid})
            elif sev in ("low", "info", ""):
                if blob != target_oid:
                    to_resolve.append({"id": fid, "finding": f, "record": record,
                                       "target_oid": target_oid})
                else:
                    carried_findings.append(dict(f, provenance="carried"))
            else:
                # Unknown severity, or high/medium with nothing reviewed this run.
                if has_active or changed:
                    to_resolve.append({"id": fid, "finding": f, "record": record,
                                       "target_oid": target_oid})
                else:
                    carried_findings.append(dict(f, provenance="carried"))

    return to_resolve, auto_resolved, carried_findings


# --- impact analysis and known defects (0.9.0) --------------------------------
# Deterministic context for the reviewer, computed here rather than by the model:
# where the symbols this push changed are called outside the files under
# review (scripts/ocr_impact.py), and where code in the push matches a defect an
# earlier review reported. Both only ADD context or findings; any failure leaves
# the review exactly as it would have been without them.

def _impact_enabled():
    return os.environ.get("OCR_IMPACT", "1").strip().lower() not in ("0", "false", "no")


def _siblings_enabled():
    return os.environ.get("OCR_SIBLINGS", "1").strip().lower() not in ("0", "false", "no")


def _is_null_oid(oid):
    return not oid or set(oid) == {"0"}


def _compute_impact(review_root, tip, active_items):
    """Changed symbols of the active files and every place they are used.

    A delta file is compared with the state it was last reviewed at; any other
    file with its base version, i.e. everything the push changed in it.
    Returns None when disabled or on failure.
    """
    if not _impact_enabled() or not active_items or not tip:
        return None
    try:
        run = ocr_impact.git_runner(review_root)
        symbols, unsupported, warnings = [], [], []
        for item in active_items:
            e = item["entry"]
            old_oid = item.get("from_oid") if item["mode"] == "delta" else e.get("old_oid")
            new_oid = e.get("new_oid")
            old_text = None if _is_null_oid(old_oid) else run(["cat-file", "-p", old_oid])[0]
            new_text = None if _is_null_oid(new_oid) else run(["cat-file", "-p", new_oid])[0]
            got = ocr_impact.changed_symbols(e["path"], old_text, new_text)
            if not got.get("supported"):
                unsupported.append(e["path"])
            symbols += [s for s in got.get("symbols") or [] if s.get("change") != "added"]
            warnings += got.get("warnings") or []
        refs = ocr_impact.find_references(run, tip, symbols, is_allowed=_is_allowed_path)
        return {"symbols": symbols, "sites": refs.get("sites") or [],
                "ref_counts": refs.get("ref_counts") or {}, "unsupported": unsupported,
                "warnings": warnings + list(refs.get("warnings") or [])}
    except Exception as exc:
        return {"symbols": [], "sites": [], "ref_counts": {}, "unsupported": [],
                "warnings": [f"impact analysis failed ({exc})"]}


def _impact_bundle(review_root, tip, impact, chunk_paths, seg=None, k=None, chunk_items=None):
    """The `impact` manifest field for one reviewer context, or None.

    Covers symbols defined in this context's files, and call sites OUTSIDE them
    -- including files another chunk reviews, which this reviewer cannot see.

    With `seg` (Part S) a segmented file's symbols go to the chunk that holds
    their unit, the symbol budget scales with the push (up to 60), and a call
    site of such a symbol shows its whole enclosing unit instead of +-6 lines.
    """
    if not impact:
        return None
    paths = set(chunk_paths)
    symbols, kw = impact["symbols"], {}
    if seg is not None and chunk_items is not None:
        symbols = seg.route_symbols(impact, chunk_items, k)
        kw["max_symbols"] = min(_SEG_MAX_SYMBOLS, max(30, len(symbols)))
    try:
        b = ocr_impact.build_bundle(
            ocr_impact.git_runner(review_root), tip, symbols, impact["sites"],
            impact["ref_counts"], include_paths=paths, exclude_paths=paths, **kw)
        if seg is not None and chunk_items is not None:
            seg.widen_sites(b)
    except Exception as exc:
        _TELE.setdefault("impact_warnings", []).append(f"bundle failed ({exc})")
        return None
    b["unsupported"] = [p for p in impact["unsupported"] if p in paths]
    defined = {s.get("name"): s.get("defined_in") for s in b.get("symbols") or []}
    tele = _TELE.setdefault("impact", {"symbols": [], "sites": [], "dropped_symbols": [],
                                       "unsupported": [], "truncated": False})
    tele["symbols"] += [{k: s.get(k) for k in ("name", "change", "defined_in", "ref_count",
                                                "sites_included")}
                        for s in b.get("symbols") or []]
    tele["sites"] += [{"id": s.get("id"), "path": s.get("path"), "line": s.get("line"),
                       "name": s.get("name"), "tier": s.get("tier"),
                       "defined_in": s.get("defined_in") or defined.get(s.get("name"))}
                      for s in b.get("sites") or []]
    tele["dropped_symbols"] += list(b.get("dropped_symbols") or [])
    tele["unsupported"] += b["unsupported"]
    tele["truncated"] = bool(tele["truncated"] or b.get("truncated"))
    if not (b.get("sites") or b.get("dropped_symbols") or b["unsupported"]):
        return None
    return b


def _known_defects(review_root, tip, prior_findings, push_paths):
    """Places matching a prior high/medium finding's code.

    Returns (for_reviewer, notes): hits in files of this push go to the
    reviewer as `known_defects`; hits in untouched files become info notes --
    old code must not block an unrelated push.
    """
    if not _siblings_enabled() or not prior_findings:
        return [], []
    run = ocr_impact.git_runner(review_root)
    for_reviewer, notes, seen = [], [], set()
    for f in prior_findings:
        if (f.get("severity") or "").lower() not in ("high", "medium", "critical"):
            continue
        try:
            got = ocr_impact.find_siblings(run, tip, dict(f, id=f.get("id") or _finding_id(f)),
                                           push_paths=push_paths, is_allowed=_is_allowed_path)
        except Exception:
            continue
        for s in got.get("siblings") or []:
            if s.get("sid") in seen:
                continue
            seen.add(s.get("sid"))
            if s.get("in_push"):
                entry = {k: s.get(k) for k in ("sid", "of", "of_content", "path", "line")}
                entry["text"] = str(s.get("text") or "")[:200]
                for_reviewer.append(entry)
            else:
                notes.append(_sibling_note(s, f, untouched=True))
    _TELE["siblings"] = {"to_reviewer": for_reviewer, "noted": list(notes)}
    return for_reviewer, notes


def _sibling_note(s, f, untouched=False):
    where = "a file this push does not touch" if untouched else "this push"
    return {
        "severity": "info", "path": s.get("path"), "start_line": s.get("line"),
        "end_line": s.get("line"), "category": "correctness", "provenance": "sibling_note",
        "content": (f"same code as the {f.get('severity')} finding at {f.get('path')}:"
                    f"{f.get('start_line')} ({str(f.get('content') or '')[:160]}) appears "
                    f"here, in {where} - check whether the same defect applies"),
    }


def _new_finding_notes(review_root, tip, new_findings, push_paths):
    """Info notes where a NEW high/medium finding's code also appears. They
    never block; the next push's reviewer judges them as known defects."""
    if not _siblings_enabled():
        return []
    run = ocr_impact.git_runner(review_root)
    notes, taken = [], {(f.get("path"), f.get("start_line")) for f in new_findings}
    for f in new_findings:
        if (f.get("severity") or "").lower() not in ("high", "medium", "critical"):
            continue
        try:
            got = ocr_impact.find_siblings(run, tip, dict(f, id=f.get("id") or _finding_id(f)),
                                           push_paths=push_paths, is_allowed=_is_allowed_path)
        except Exception:
            continue
        for s in got.get("siblings") or []:
            if (s.get("path"), s.get("line")) in taken:
                continue
            taken.add((s.get("path"), s.get("line")))
            notes.append(_sibling_note(s, f, untouched=not s.get("in_push")))
    _TELE.setdefault("siblings", {"to_reviewer": [], "noted": []})["noted"] += notes
    return notes


def _rewrite_record_findings(common_dir, fp, key, rec, findings):
    """Store `findings` on an existing record, changing nothing else: the
    `truncated` flag, chain_depth, run_id and timestamps are the record's own
    facts about the review that wrote it, and a rewrite of the findings is not
    a new review. Atomic, best-effort."""
    try:
        data = dict(rec)
        data["findings"] = [dict(f, id=_finding_id(f)) for f in (findings or [])]
        _write_state(_record_path(common_dir, fp, key, rec["head_oid"]), data)
    except Exception:
        pass


def _attach_to_carried_records(findings, carry_items, common_dir, fp, run_id, review_root, tip):
    """Also keep a finding about a carried file on THAT file's record, so it
    stays owed however the files that revealed it change later."""
    by_path = {item["entry"]["path"]: item for item in carry_items}
    grouped = {}
    for f in findings:
        if f.get("path") in by_path and (f.get("severity") or "").lower() != "info":
            grouped.setdefault(f["path"], []).append(f)
    for path, fs in grouped.items():
        e = by_path[path]["entry"]
        key = _record_key(path, e.get("old_path") or "", e["status"], e["old_oid"])
        rec = _read_ledger_record(_record_path(common_dir, fp, key, e["new_oid"]),
                                  fp, key, e["new_oid"])
        if rec is None:
            continue
        stamped = [dict(f, target_oid=f.get("target_oid") or e["new_oid"]) for f in fs]
        merged = _dedup_by_id(list(rec.get("findings") or []) + stamped)
        if len(merged) != len(rec.get("findings") or []):
            _rewrite_record_findings(common_dir, fp, key, rec, merged)


_RESOLVER_STATUSES = ("resolved", "still_present")


def _no_evidence():
    """A still_present nobody backed: the gate re-checks it against the tip."""
    return {"status": "still_present", "evidence_path": "", "evidence_quote": ""}


def _normalize_resolutions(raw, to_resolve):
    """Validate the resolver's `resolutions` map, one value at a time.

    Returns ({id: {status, evidence_path, evidence_quote}}, [warning]) with an
    entry for every id in `to_resolve` and none other. A value that is not an
    object, or whose status is not one of _RESOLVER_STATUSES, becomes an
    evidence-free still_present -- never an exception: this is model output, and
    `{"<id>": true}` has been seen in the field.
    """
    warnings = []
    if not isinstance(raw, dict):
        if raw not in (None, {}):
            warnings.append(f"resolver: `resolutions` was {type(raw).__name__}, not an object")
        raw = {}
    out = {}
    for p in to_resolve:
        fid = p["id"]
        val = raw.get(fid)
        short = fid[:12]
        if val is None:
            warnings.append(f"resolver: no verdict for finding {short}")
            out[fid] = _no_evidence()
            continue
        if not isinstance(val, dict):
            warnings.append(f"resolver: verdict for finding {short} is "
                            f"{type(val).__name__}, not an object")
            out[fid] = _no_evidence()
            continue
        status = val.get("status")
        if status not in _RESOLVER_STATUSES:
            warnings.append(f"resolver: verdict for finding {short} has "
                            f"status {status!r}")
            out[fid] = _no_evidence()
            continue
        ev_path, ev_quote = val.get("evidence_path"), val.get("evidence_quote")
        out[fid] = {
            "status": status,
            "evidence_path": ev_path if isinstance(ev_path, str) else "",
            "evidence_quote": ev_quote if isinstance(ev_quote, str) else "",
        }
    return out, warnings


def _since_finding_specs(review_root, tip, to_resolve):
    """{path, from_oid, to_oid} per prior whose file changed since the finding.

    The incremental delta only covers the change since the last recorded review;
    a fix made in a push whose resolver never got recorded is outside it. The
    since-finding diff (finding's target blob -> tip blob) always contains it.
    """
    paths = sorted({p["finding"].get("path") or "" for p in to_resolve} - {""})
    tips = _blob_oids_at(review_root, tip, paths) if (paths and tip) else {}
    specs, seen = [], set()
    for p in to_resolve:
        path = p["finding"].get("path") or ""
        frm, to = p.get("target_oid") or "", tips.get(path, "")
        if not (path and frm and to) or frm == to or (path, frm) in seen:
            continue
        seen.add((path, frm))
        specs.append({"path": path, "from_oid": frm, "to_oid": to})
    return specs


def _resolver_diff_items(repo_root, common_dir, run_id, tag, active_plan_items,
                         prior_specs, diffs):
    """items[] for the resolver: the active files' diffs (built for the review,
    `diffs`) and, for each prior whose file changed since it was raised, the
    since-finding diff -- all written under diffs/res<tag>/ for the agent to Read."""
    directory = _run_dir(common_dir, run_id) / "diffs" / f"res{tag}"
    entries = [({"path": it["entry"]["path"], "old_path": it["entry"].get("old_path"),
                 "mode": it["mode"]}, diffs.get(it["entry"]["path"]))
               for it in active_plan_items]
    items = _diff_items(directory, entries, role="active", mark_owned=False)
    run = ocr_impact.git_runner(repo_root)
    since = []
    for n, spec in enumerate(prior_specs):
        try:
            d = _build_item_diff(run, {
                "path": spec["path"], "blobs": (spec["from_oid"], spec["to_oid"]),
                "header": f"# path: {spec['path']} (changes since the finding was raised)"})
        except Exception:
            d = _failed_diff()
        since.append(({"path": spec["path"], "mode": "since"}, d))
    items += _diff_items(directory / "since", since, role="since", mark_owned=False)
    return items


def _run_resolver(repo_root, mode, git_dir, tip, push_range, to_resolve,
                  active_plan_items, common_dir, fp, run_id, recheck=False, diffs=None):
    """Invoke the resolver agent once. `diffs` ({path: diff result}, see
    _build_diffs) adds items[] with the diff files it should Read.

    Returns ({id: {status, evidence_path, evidence_quote}}, [warning]); every id
    in `to_resolve` is present. On failure every id is an evidence-free
    still_present, which the caller re-checks against the tip (fail closed).
    """
    if not to_resolve:
        return {}, []
    fallback = {p["id"]: _no_evidence() for p in to_resolve}
    # Priors are reviewer output about an untrusted diff; cap what goes back into a prompt.
    prior_data = [
        {**{k: (v[:2000] if isinstance(v, str) else v) for k, v in p["finding"].items()},
         "id": p["id"]}
        for p in to_resolve
    ]
    tag = "-recheck" if recheck else ""
    manifest_path = str(_async_dir(common_dir) / f"resolver-{run_id}{tag}.json")
    try:
        prior_specs = _since_finding_specs(repo_root, tip, to_resolve)
        manifest = {
            "resolve": prior_data,
            "active_paths": [item["entry"]["path"] for item in active_plan_items],
            "files": [
                {"path": item["entry"]["path"], "mode": item["mode"],
                 "from_oid": item.get("from_oid") or "",
                 "to_oid": item["entry"].get("new_oid") or ""}
                for item in active_plan_items
            ],
            "prior_files": prior_specs,
            "recheck": bool(recheck),
        }
        if diffs is not None:
            manifest["items"] = _resolver_diff_items(
                repo_root, common_dir, run_id, tag, active_plan_items, prior_specs, diffs)
        _tmp = manifest_path + ".tmp"
        Path(_tmp).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        os.replace(_tmp, manifest_path)
    except Exception as exc:
        _remove_run_dir(common_dir, run_id, sub=f"res{tag}")
        _warn(f"review-gate: could not write resolver manifest: {exc}")
        return fallback, [f"resolver: could not write manifest ({exc})"]
    try:
        result, _, _ = _run_review(
            repo_root, mode, git_dir, tip, push_range,
            resolve_file=manifest_path,
            timeout=_CHUNK_TIMEOUT,
            raw_tag="-resolve" + tag,
        )
        if not isinstance(result, dict):
            raise ReviewGateError("resolver returned non-dict")
        return _normalize_resolutions(result.get("resolutions"), to_resolve)
    except (ReviewGateError, ReviewLimitError) as exc:
        return fallback, [f"resolver: failed ({_sanitize(str(exc), 200)})"]
    finally:
        try:
            Path(manifest_path).unlink(missing_ok=True)
        except Exception:
            pass
        if diffs is not None:
            _remove_run_dir(common_dir, run_id, sub=f"res{tag}")


def _norm_ws(text):
    return " ".join((text or "").split())


def _quote_in_added_lines(diff_args, quote, review_root):
    diff_out, rc = _git(diff_args, cwd=review_root)
    if rc != 0:
        return False
    added = "\n".join(
        line[1:] for line in diff_out.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    return _norm_ws(quote) in _norm_ws(added)


def _guard_resolution(resolution, active_plan_items, push_range, review_root,
                      prior=None, tip=""):
    """True if a 'resolved' verdict passes the Python guard.

    A resolution is accepted only when evidence_quote appears verbatim
    (whitespace-normalised) on the added side of either:
    - the diff in this push of evidence_path, a delta/full file, or
    - with `prior` and `tip`, the since-finding diff of the finding's own file
      (its target blob -> its tip blob): a fix from an earlier push whose
      resolution was never recorded is still a fix, and still an addition.
    """
    if not isinstance(resolution, dict) or resolution.get("status") != "resolved":
        return True  # still_present always passes
    evidence_path = resolution.get("evidence_path") or ""
    evidence_quote = (resolution.get("evidence_quote") or "").strip()
    if not evidence_path or not evidence_quote:
        return False
    item = next((i for i in active_plan_items if i["entry"]["path"] == evidence_path), None)
    if item is not None:
        # A reviewed file's earlier lines predate the finding; only the fix's
        # own additions can count as evidence that it was fixed. That holds
        # for a cost-rule full item too: its push-range diff also shows lines
        # that were already there when the finding was made.
        if (item.get("record") is not None and item.get("from_oid")
                and item["entry"].get("new_oid")):
            diff_args = ["diff", item["from_oid"], item["entry"]["new_oid"]]
        elif push_range:
            diff_args = ["diff", push_range, "--", evidence_path]
        else:
            diff_args = None
        if diff_args and _quote_in_added_lines(diff_args, evidence_quote, review_root):
            return True
    if prior is None or not tip:
        return False
    if evidence_path != (prior["finding"].get("path") or ""):
        return False
    target_oid = prior.get("target_oid") or ""
    tip_oid = _blob_oids_at(review_root, tip, [evidence_path]).get(evidence_path, "")
    if not target_oid or not tip_oid or target_oid == tip_oid:
        return False
    if _quote_in_added_lines(["diff", target_oid, tip_oid], evidence_quote, review_root):
        return True
    # Fallback: evidence_quote may span context + added lines (e.g. a newly inserted
    # line quoted together with its unchanged neighbour). Accept only when:
    # - the full quote is present in the current tip file (the fix is there), AND
    # - it was NOT present in the blob at target_oid (so it was genuinely new,
    #   not pre-existing unchanged code that a hallucinating resolver cited).
    # Note: target_oid and tip_oid are BLOB OIDs here; use `git show <oid>` (no
    # `:<path>` suffix) to read the blob content directly.
    tip_text = _tip_text(review_root, tip, evidence_path, {})
    if not (tip_text and _norm_ws(evidence_quote) in _norm_ws(tip_text)):
        return False
    orig_blob, rc = _git(["show", target_oid], cwd=review_root)
    if rc != 0:
        return False  # can't read original blob; fail closed rather than open
    return _norm_ws(evidence_quote) not in _norm_ws(orig_blob)


def _tip_text(review_root, tip, path, cache):
    if path not in cache:
        out, rc = _git(["show", f"{tip}:{path}"], cwd=review_root) if (path and tip) else ("", 1)
        cache[path] = out if rc == 0 else ""
    return cache[path]


def _judge_resolution(prior, res, active_plan_items, push_range, review_root, tip,
                      _cache=None):
    """'resolved' | 'still_present' | 'unverified' for one normalized verdict.

    resolved      -- the guard found the fix on an added side (see _guard_resolution).
    still_present -- backed by the tip: the resolver's still_present quote, or
                     failing that the finding's own existing_code, is in the
                     tip file. A diff that no longer shows the code proves
                     nothing, so the tip decides.
    unverified    -- neither: nothing at the tip supports "still present" and
                     nothing in a diff supports "resolved".
    """
    cache = {} if _cache is None else _cache
    if res.get("status") == "resolved" and _guard_resolution(
            res, active_plan_items, push_range, review_root, prior=prior, tip=tip):
        return "resolved"
    f = prior["finding"]
    if res.get("status") == "still_present":
        quote = _norm_ws(res.get("evidence_quote"))
        path = res.get("evidence_path") or f.get("path") or ""
        if quote and quote in _norm_ws(_tip_text(review_root, tip, path, cache)):
            return "still_present"
    code = _norm_ws(f.get("existing_code"))
    if code and code in _norm_ws(_tip_text(review_root, tip, f.get("path") or "", cache)):
        return "still_present"
    return "unverified"


def _reanchor_finding(f, review_root, tip):
    """Try to update a finding's lines by locating its existing_code at tip.

    Returns the (possibly updated) finding and a bool indicating success.
    """
    existing_code = (f.get("existing_code") or "").strip()
    path = f.get("path") or ""
    if not existing_code or not path:
        return f, False
    out, rc = _git(["show", f"{tip}:{path}"], cwd=review_root)
    if rc != 0 or not out:
        return dict(f, unanchored=True), False
    norm_code = " ".join(existing_code.split())
    lines = out.splitlines()
    span = existing_code.count("\n") + 1
    for i in range(len(lines) - span + 1):
        block = " ".join(" ".join(lines[i:i + span]).split())
        if norm_code == block:
            return dict(f, start_line=i + 1, end_line=i + span), True
    return dict(f, unanchored=True), False


def _prune_ledger(common_dir):
    """Remove stale/excess ledger records. Best-effort, called once per run."""
    try:
        led_dir = _ledger_dir(common_dir)
        if not led_dir.is_dir():
            return
        now = time.time()
        cutoff = now - _LEDGER_TTL
        all_records, seg_records = [], []
        for fp_dir in list(led_dir.iterdir()):
            if not fp_dir.is_dir():
                continue
            try:
                # Prune whole fp dir if its mtime is stale
                if fp_dir.stat().st_mtime < cutoff:
                    shutil.rmtree(fp_dir, ignore_errors=True)
                    continue
            except OSError:
                continue
            for rec_file in fp_dir.glob("*.json"):
                try:
                    mtime = rec_file.stat().st_mtime
                    if mtime < cutoff:
                        rec_file.unlink()
                    else:
                        all_records.append((mtime, rec_file))
                except OSError:
                    continue
            for mark in (fp_dir / "timeouts").glob("*.json"):
                try:
                    if now - mark.stat().st_mtime > _TIMEOUT_MARK_TTL:
                        mark.unlink()
                except OSError:
                    continue
            # Part S records (units and caller checks): the same TTL, and their own cap
            for sub in ("seg", "dep"):
                for rec_file in (fp_dir / sub).glob("*.json"):
                    try:
                        mtime = rec_file.stat().st_mtime
                        if mtime < cutoff:
                            rec_file.unlink()
                        else:
                            seg_records.append((mtime, rec_file))
                    except OSError:
                        continue
        for records, cap in ((all_records, _LEDGER_MAX_RECORDS),
                             (seg_records, _LEDGER_MAX_RECORDS * _SEG_RECORD_CAP_FACTOR)):
            if len(records) > cap:
                records.sort(reverse=True)  # keep newest
                for _, stale in records[cap:]:
                    try:
                        stale.unlink()
                    except OSError:
                        pass
    except Exception:
        pass


# --- chunk timeouts (0.9.5) ----------------------------------------------------
# A chunk that times out saves nothing, so retrying it unchanged would time out
# again, forever. Instead every file in it gets a marker (kept beside the ledger
# records, keyed like them: a changed blob starts afresh), and the next run
# retries those files in halves down to one file. A file that times out ALONE
# twice cannot be reviewed within OCR_CHUNK_TIMEOUT: the review then fails with
# a terminal reason naming it, instead of "still running, re-push" for ever.

def _timeout_mark_path(common_dir, fp, key, head_oid):
    return _fp_dir(common_dir, fp) / "timeouts" / f"{key}-{head_oid[:16]}.json"


def _item_record_key(item):
    e = item["entry"]
    return _record_key(e["path"], e.get("old_path") or "", e["status"], e["old_oid"])


def _read_timeout_mark(path):
    """The marker at `path`, or None (absent, corrupt, expired)."""
    try:
        if time.time() - Path(path).stat().st_mtime > _TIMEOUT_MARK_TTL:
            return None
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _timeout_marks(common_dir, fp, items):
    """{path: marker} for the items that timed out in an earlier run.

    A marker from a shorter timeout than the one in force is ignored: whoever
    raised OCR_CHUNK_TIMEOUT wants the file tried again, at any size."""
    out = {}
    if not (fp and common_dir) or not _ledger_enabled():
        return out
    for item in items:
        e = item["entry"]
        mark = _read_timeout_mark(_timeout_mark_path(
            common_dir, fp, _item_record_key(item), e["new_oid"]))
        if mark and int(mark.get("timeout_s") or 0) >= _CHUNK_TIMEOUT:
            out[e["path"]] = mark
    return out


def _stuck_paths(marks):
    """Files that timed out twice on their own."""
    return [p for p, m in marks.items() if int(m.get("solo_attempts") or 0) >= 2]


def _chunk_members(items):
    """How many pieces of work a chunk holds: one per file, except that a file
    reviewed in units (Part S) counts the groups of units it brought."""
    return sum(max(1, int(it.get("members") or 1)) for it in items)


def _note_chunk_timeout(common_dir, fp, chunk_items, timeout_s):
    """Mark every file of a chunk that timed out. Returns the path of a file that
    has now timed out twice on its own, "" when the chunk is merely to be split
    next time, or None when nothing could be recorded (so nothing will change)."""
    n, stuck = _chunk_members(chunk_items), ""
    try:
        for item in chunk_items:
            e = item["entry"]
            mark_path = _timeout_mark_path(
                common_dir, fp, _item_record_key(item), e["new_oid"])
            prev = _read_timeout_mark(mark_path) or {}
            if int(prev.get("timeout_s") or 0) < timeout_s:
                prev = {}
            solo = int(prev.get("solo_attempts") or 0) + (1 if n == 1 else 0)
            mark_path.parent.mkdir(parents=True, exist_ok=True)
            _write_state(mark_path, {
                "path": e["path"], "chunk_size": n, "timeout_s": timeout_s,
                "attempts": int(prev.get("attempts") or 0) + 1, "solo_attempts": solo,
                "ts": time.time(),
            })
            if n == 1 and solo >= 2:
                stuck = e["path"]
    except Exception:
        return None
    return stuck


def _clear_timeout_mark(common_dir, fp, key, head_oid):
    """A file that has a record was reviewed: its timeouts are behind it."""
    try:
        _timeout_mark_path(common_dir, fp, key, head_oid).unlink(missing_ok=True)
    except Exception:
        pass


def _unreviewable_message(paths, timeout_s):
    names = ", ".join(sorted(paths)[:5]) + (" ..." if len(paths) > 5 else "")
    return (
        f"file {names} cannot be reviewed within the timeout: it timed out on its own "
        f"twice at OCR_CHUNK_TIMEOUT={timeout_s}s. Split the file, or raise OCR_CHUNK_TIMEOUT "
        "in the environment Claude Code was launched from, then push again."
    )


def _timeout_failure(exc, common_dir, fp, items, timeout_s, label):
    """The exception to raise for a review call that timed out, after marking
    its files: ReviewUnreviewableError for a file that timed out alone twice,
    ReviewChunkTimeout when they will be split next push, `exc` itself when
    nothing could be recorded."""
    if not (fp and common_dir) or not _ledger_enabled():
        return exc
    stuck = _note_chunk_timeout(common_dir, fp, items, timeout_s)
    if stuck is None:
        return exc
    if stuck:
        return ReviewUnreviewableError(_unreviewable_message([stuck], timeout_s))
    n = _chunk_members(items)
    if n == 1:
        return ReviewChunkTimeout(
            f"{label} timed out after {timeout_s}s on one file alone; one more timeout "
            "and that file is declared unreviewable")
    what = "piece(s) of work (files, or units of a big file)" if any(
        it.get("seg_file") is not None for it in items) else "file(s)"
    return ReviewChunkTimeout(
        f"{label} of {n} {what} timed out after {timeout_s}s; the next push retries "
        "them in smaller chunks")


def _plan_to_chunks(active_items, marks=None, sizes=None, budget=None):
    """Group active (delta/full) plan items into chunks for _run_chunked.

    Returns a list of lists of plan_item dicts (each item has 'entry', 'mode',
    'from_oid'). Mirrors _group_into_chunks but operates on plan items.
    `marks` (see _timeout_marks) splits any chunk holding a file that timed
    out before: into pieces half the size it timed out at, down to one file.
    """
    entries = [item["entry"] for item in active_items]
    path_to_item = {item["entry"]["path"]: item for item in active_items}
    grouped_entries = _group_into_chunks(entries, sizes, budget)
    chunks = [
        [path_to_item[e["path"]] for e in chunk_entries]
        for chunk_entries in grouped_entries
    ]
    if not marks:
        return chunks
    out = []
    for chunk in chunks:
        if not chunk:
            out.append(chunk)
            continue
        cap = len(chunk)
        for item in chunk:
            mark = marks.get(item["entry"]["path"])
            if mark:
                cap = min(cap, max(1, int(mark.get("chunk_size") or 1) // 2))
        out.extend(chunk[i:i + cap] for i in range(0, len(chunk), cap))
    return out


# --- precomputed diffs (0.10.0) ----------------------------------------------
# Python writes each file's diff to disk, in the gate's OWN run directory, and
# the reviewer reads it (manifest items[]). The directory is
# `<git common dir>/review-gate-async/run-<run_id>/diffs/<k>/<nnn>.diff`:
# never inside the reviewed worktree. The tip tree is attacker-controlled, so a
# tracked or symlinked `.review-gate` there could redirect a write or plant a
# fake diff, and excluding such a path from review would be a way to hide a
# change. File names are generated here, never taken from the tree.

def _precomputed_enabled():
    return os.environ.get("OCR_PRECOMPUTED_DIFFS", "1").strip().lower() not in ("0", "false", "no")


def _chunk_concurrency():
    """OCR_CHUNK_CONCURRENCY: chunks reviewed at once (0.12.0), default 2, clamped
    to 1-4. 1 is the sequential review of 0.11.0, exactly."""
    try:
        n = int(os.environ.get("OCR_CHUNK_CONCURRENCY", "2"))
    except ValueError:
        n = 2
    return min(4, max(1, n))


def _run_dir(common_dir, run_id):
    return _async_dir(common_dir) / f"run-{run_id}"


def _remove_run_dir(common_dir, run_id, sub=None):
    """Delete a run's diff directory (or one `diffs/<sub>` of it). Refuses
    anything that is not under this repository's review-gate-async/run-*."""
    try:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", str(run_id or "")):
            return
        target = _run_dir(common_dir, run_id)
        if sub is not None:
            target = target / "diffs" / str(sub)
        base = os.path.realpath(str(_async_dir(common_dir)))
        real = os.path.realpath(str(target))
        if real.startswith(base + os.sep) and os.path.isdir(real):
            shutil.rmtree(real, ignore_errors=True)
    except Exception:
        pass


# `git diff` arguments shared by every precomputed diff. --no-color: a user's
# color.ui=always would put escapes in the file; core.quotePath=false keeps a
# non-ASCII path readable in the headers; --no-ext-diff / --no-textconv: what the
# reviewer reads is git's own text diff, whatever the config or attributes say.
_DIFF_ARGS = ["-c", "core.quotePath=false", "diff", "--no-ext-diff",
              "--no-textconv", "--no-color"]
_BINARY_DIFF_RE = re.compile(r"^(?:Binary files .* differ|GIT binary patch)$", re.M)


def _item_diff_spec(item, base, tip):
    """What to diff for an active plan item: its delta blobs, or its whole range
    (both paths of a rename, so git can pair them)."""
    e = item["entry"]
    if item.get("mode") == "delta" and item.get("from_oid"):
        return {"path": e["path"], "blobs": (item["from_oid"], e["new_oid"]),
                "header": f"# path: {e['path']} (delta since last review)"}
    return {"path": e["path"], "range": f"{base or _EMPTY_TREE}..{tip}",
            "paths": [p for p in (e.get("old_path"), e["path"]) if p]}


def _diff_command(spec, unified=None):
    args = list(_DIFF_ARGS)
    if unified is not None:
        args.append(f"-U{unified}")
    if spec.get("blobs"):
        return args + list(spec["blobs"])
    return args + ["-M", spec["range"], "--"] + list(spec["paths"])


def _cap_diff_lines(text):
    """(text with no line longer than _DIFF_LINE_CAP chars, how many were cut)."""
    cut, out = 0, []
    for line in text.split("\n"):
        if len(line) > _DIFF_LINE_CAP:
            cut += 1
            line = line[:_DIFF_LINE_CAP] + f" ...[cut {len(line) - _DIFF_LINE_CAP} chars]"
        out.append(line)
    return "\n".join(out), cut


def _count_changed(text):
    """Added + removed lines of a unified diff. Structural, not by prefix: a
    removed SQL comment is `--- x` and an added `++ x` is `+++ x`, which must
    not be mistaken for file headers."""
    n, in_hunk = 0, False
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line[:1] in ("+", "-"):
            n += 1
    return n


def _hunk_headers_only(text):
    """The file headers and the `@@` lines of a diff, nothing in between."""
    out, in_hunk = [], False
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            in_hunk = False
            out.append(line)
        elif line.startswith("@@"):
            in_hunk = True
            out.append(line)
        elif not in_hunk:
            out.append(line)
    return "\n".join(out)


def _build_item_diff(run, spec, budget=None):
    """One file's diff text, degraded as little as the limits allow.

    Returns {text, lines, bytes, changed, truncated, binary, level, failed}.
    level: full | u0 (every changed line, no context lines) | stat (hunk headers
    only; truncated) | none. `failed` -- git failed or said nothing -- means the
    file gets NO precomputed diff and goes to the orchestrator's own git path:
    never an empty diff the reviewer would take for "no change".
    """
    res = {"text": "", "lines": 0, "bytes": 0, "changed": 0, "truncated": False,
           "binary": False, "level": "none", "failed": False}
    full, rc = run(_diff_command(spec))
    if rc != 0 or not full.strip():
        res["failed"] = True
        return res
    if _BINARY_DIFF_RE.search(full):
        res["binary"] = True
        return res

    def measure(text):
        return (text.count("\n") + 1, len(text.encode("utf-8", "replace")) + 1,
                _count_changed(text))

    def fits(m):
        return (m[2] <= _PRECOMPUTED_MAX_LINES and m[1] <= _PRECOMPUTED_MAX_BYTES
                and (budget is None or m[0] <= budget))

    text, cut = _cap_diff_lines(full)
    level = "full"
    if not fits(measure(text)):
        u0, rc0 = run(_diff_command(spec, 0))
        u0_text, u0_cut = _cap_diff_lines(u0) if (rc0 == 0 and u0.strip()) else (None, 0)
        if u0_text is not None and fits(measure(u0_text)):
            text, cut, level = u0_text, u0_cut, "u0"
        else:
            text = _hunk_headers_only(u0_text if u0_text is not None else text)
            cut, level = 0, "stat"
    notes = []
    if spec.get("header"):
        notes.append(spec["header"])
    if level == "u0":
        notes.append("# context lines omitted (-U0); every changed line is shown")
    elif level == "stat":
        notes.append("# diff truncated: too large for the review limits; hunk headers "
                     "only. Read the file at the hunks below.")
    if notes:
        text = "\n".join(notes) + "\n" + text
    lines, size, changed = measure(text)
    res.update(text=text, lines=lines, bytes=size, changed=changed,
               truncated=(level == "stat" or cut > 0), level=level)
    return res


def _failed_diff():
    return {"text": "", "lines": 0, "bytes": 0, "changed": 0, "truncated": False,
            "binary": False, "level": "none", "failed": True}


def _build_diffs(review_root, base, tip, active_items, budget=None):
    """{path: _build_item_diff result} for the active items. Never raises: a
    file whose diff could not be built is marked failed (orchestrator fallback)."""
    run = ocr_impact.git_runner(review_root)
    out = {}
    for item in active_items:
        path = item["entry"]["path"]
        try:
            out[path] = _build_item_diff(run, _item_diff_spec(item, base, tip),
                                         _CHUNK_DIFF_LINES if budget is None else budget)
        except Exception:
            out[path] = _failed_diff()
    return out


def _diff_warnings(diffs):
    """Planner warnings for what the reviewer could not be shown at all."""
    n_bin = sum(1 for d in (diffs or {}).values() if d.get("binary"))
    if not n_bin:
        return []
    return [f"{n_bin} file(s) with a reviewable extension are binary to git "
            "(NUL bytes or a `-diff` attribute): their content was not shown to the reviewer"]


def _write_diff_file(directory, n, text, name=None):
    """Write one diff as `<nnn>.diff` (bytes, LF) in `directory`; "" on failure.
    The name is generated here -- never derived from a path in the reviewed tree."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        f = directory / (name or f"{n:03d}.diff")
        f.write_bytes((text + "\n").encode("utf-8", "replace"))
        return str(f).replace("\\", "/")
    except OSError:
        return ""


def _diff_items(directory, entries, role=None, mark_owned=True):
    """file_diff manifest items for `entries` [(meta, diff result)], writing each
    diff to `directory`. An item without `file` (git failed, or the file could
    not be written) is the orchestrator's to collect with git. `mark_owned`
    records on the diff result that Python delivered it to the reviewer."""
    items = []
    for n, (meta, d) in enumerate(entries):
        it = {"kind": "file_diff", "path": meta["path"], "old_path": meta.get("old_path") or "",
              "mode": meta.get("mode") or "full", "file": "", "lines": 0, "bytes": 0,
              "truncated": False, "binary": False}
        if role:
            it["role"] = role
        if d and not d["failed"]:
            it.update(lines=d["lines"], bytes=d["bytes"], truncated=d["truncated"],
                      binary=d["binary"], level=d["level"])
            if d["text"]:
                it["file"] = _write_diff_file(directory, n, d["text"])
            if mark_owned:
                d["owned"] = bool(it["file"]) or bool(d["binary"])
        items.append(it)
    return items


def _context_only(item):
    """A virtual item (Part S) whose work is caller checks alone."""
    return item.get("seg_file") is not None and not any(
        a["kind"] in ("unit", "filediff") for a in item["seg_work"])


def _build_review_manifest(common_dir, run_id, k, total, chunk_items, other_changed,
                           carry_paths, extras=None, diffs=None):
    """The reviewer's manifest for one chunk (k, total) or the single-context
    review (k None). The one builder for both.

    Keys kept from 0.8/0.9: paths, renames, other_changed, files[] (path, mode,
    from_oid, to_oid), carried, plus the impact / known_defects extras. With
    `diffs` (_build_diffs) it adds `items[]` -- one file_diff per file whose diff
    is written under this run's diffs/<k>/ -- `context` items for the files the
    reviewer should treat as background, and `tasks[]` (0.11.0, Part S: caller
    checks, empty otherwise). A chunk's virtual items (see _SegState) add
    `unit_diff` items for the changed units of a big file, a `file_context` item per
    such file, and a `caller` (+ `callee_diff`) context item per task.
    """
    manifest = {}
    if k is not None:
        manifest.update(chunk_index=k, chunks_total=total)
    # A chunk of caller checks alone (Part S's retry) has files in its items[] as
    # context only: they are not under review, so they are not in paths/files.
    all_items = chunk_items
    chunk_items = [it for it in chunk_items if not _context_only(it)]
    manifest.update({
        "paths": [item["entry"]["path"] for item in chunk_items],
        "renames": [
            [item["entry"]["old_path"], item["entry"]["path"]]
            for item in chunk_items if item["entry"].get("old_path")
        ],
        "other_changed": list(other_changed or []),
        "files": [
            {
                "path": item["entry"]["path"],
                "mode": item["mode"],
                "from_oid": item.get("from_oid") or "",
                "to_oid": item["entry"].get("new_oid") or "",
            }
            for item in chunk_items
        ],
        "carried": list(carry_paths or []),
    })
    if diffs is not None:
        directory = _run_dir(common_dir, run_id) / "diffs" / str(0 if k is None else k)
        manifest["items"] = _diff_items(
            directory,
            [({"path": it["entry"]["path"], "old_path": it["entry"].get("old_path"),
               "mode": it["mode"]}, diffs.get(it["entry"]["path"]))
             for it in chunk_items
             if it.get("seg_file") is None
             or any(a["kind"] == "filediff" for a in it["seg_work"])])
        tasks = []
        for it in all_items:
            if it.get("seg_file") is not None:
                seg_items, seg_tasks = it["seg"].manifest_items(directory, it)
                manifest["items"] += seg_items
                tasks += seg_tasks
        manifest["items"] += (
            [{"kind": "context", "role": "other_changed", "path": p}
             for p in manifest["other_changed"]]
            + [{"kind": "context", "role": "carried", "path": p}
               for p in manifest["carried"]])
        manifest["tasks"] = tasks
    manifest.update(extras or {})
    return manifest


def _write_manifest_file(manifest_path, manifest, what):
    """Atomically write a manifest; ReviewGateError (fail closed) when it can't be."""
    try:
        tmp = manifest_path + ".tmp"
        Path(tmp).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, manifest_path)
    except Exception as exc:
        raise ReviewGateError(f"could not write {what}: {exc}")


def _owned_truncation(diffs):
    """{path: truncated} for the files whose diff Python delivered (or whose
    binary-ness it established). For these, Python's flag replaces whatever the
    model's warnings say."""
    return {p: bool(d.get("truncated")) for p, d in (diffs or {}).items() if d.get("owned")}


def _truncated_paths(result, owned=None):
    """Paths the reviewer saw only partially (skill warning or finding flag).
    Contains "*" when a diff truncation names no file: which one is unknown.

    `owned` is the set of files whose diff Python delivered (precomputed): for
    those, Python's own `truncated` flag is the truth, so the model's warnings
    and flags about them are ignored. A `"*"` warning is kept only when some file
    of the call has no precomputed diff (the caller passes `owned=None` then)."""
    out = set()
    owned = owned or ()
    for w in (result.get("warnings") or []):
        if isinstance(w, dict):
            msg = str(w.get("message") or "").lower()
            diff_trunc = w.get("type") == "diff_truncated" or "diff truncated" in msg
            if w.get("file") and (diff_trunc or "truncat" in msg):
                if w["file"] not in owned:
                    out.add(w["file"])
            elif diff_trunc:
                out.add("*")
    for f in (result.get("findings") or []):
        if isinstance(f, dict) and f.get("diff_truncated") and f.get("path")                 and f["path"] not in owned:
            out.add(f["path"])
    return out


def _surface_truncated(result, plan, common_dir, fp, owned=None):
    """Keep truncation visible once it is cached.

    A truncated review is carried like any other (re-reviewing the same blob
    would truncate it the same way), which would let the file pass silently on
    every later run. So, for every file of this push whose recorded review is
    flagged truncated -- carried or reviewed just now -- and every file the
    reviewer warned about: re-emit the skill's truncation warning (when the
    result has none for that file), make the status completed_with_warnings,
    and count them in `summary.unreviewed_truncated` / `unreviewed_truncated`
    (the paths), which _format_reasons turns into the verdict line. The verdict
    itself is untouched: this only adds, never downgrades.
    """
    if not isinstance(result, dict):
        return result
    in_push = {p["entry"]["path"] for p in plan or []}
    flagged = set()
    if fp and common_dir and _ledger_enabled():
        for p in plan or []:
            e = p["entry"]
            key = _item_record_key(p)
            rec = _read_ledger_record(_record_path(common_dir, fp, key, e["new_oid"]),
                                      fp, key, e["new_oid"])
            if _is_truncated_record(rec):
                flagged.add(e["path"])
    flagged |= {w for w in _truncated_paths(result, owned) if w != "*"} & in_push
    if not flagged:
        return result
    warnings = list(result.get("warnings") or [])
    warned = {w.get("file") for w in warnings
              if isinstance(w, dict) and "diff truncated" in str(w.get("message") or "").lower()}
    for path in sorted(flagged - warned):
        warnings.append({"file": path, "message": _TRUNCATED_MSG})
    summary = dict(result["summary"]) if isinstance(result.get("summary"), dict) else {}
    summary["unreviewed_truncated"] = len(flagged)
    out = dict(result, warnings=warnings, summary=summary, unreviewed_truncated=sorted(flagged))
    if out.get("status") != "completed_with_errors":
        out["status"] = "completed_with_warnings"
    return out


def _dedup_by_id(findings):
    seen, out = set(), []
    for f in findings:
        fid = f.get("id") or _finding_id(f)
        if fid not in seen:
            seen.add(fid)
            out.append(f)
    return out


def _record_findings_for(item, new_by_path, orphans, active_paths):
    """A record keeps what is still owed on this file state: its new findings,
    findings about files outside this context, and the unjudged-away priors of
    the state it was delta-reviewed from."""
    path = item["entry"]["path"]
    own = list(new_by_path.get(path) or [])
    carried = []
    prev = item.get("record")
    if prev:
        for f in prev.get("findings") or []:
            if any(_findings_similar(f, n) for n in own):
                continue
            carried.append(f)
    return _dedup_by_id(own + list(orphans) + carried)


def _diff_exceeds_caps(item, review_root=""):
    """True when the gate's own size check says the skill capped this file's diff.

    Applies SKILL.md §3's per-file cap (_TRUNC_MAX_LINES / _TRUNC_MAX_BYTES) to
    what the reviewer was handed: the delta for a delta item, the push-range diff
    otherwise. Only a numeric line count counts -- binary files have none
    (numstat prints `-`; the entry keeps 0) and are not this check's business.
    The byte size needs git and is skipped without a review_root.
    """
    e = item["entry"]
    if item.get("mode") == "delta" and item.get("from_oid"):
        old_oid = item["from_oid"]
        lines = item.get("delta_lines")
        if not isinstance(lines, int):
            lines = e.get("lines")
    else:
        old_oid, lines = e.get("old_oid"), e.get("lines")
    new_oid = e.get("new_oid")
    if not isinstance(lines, int) or lines <= 0:
        return False
    if lines > _TRUNC_MAX_LINES:
        return True
    if not review_root or _is_null_oid(new_oid):
        return False
    if _is_null_oid(old_oid):
        out, rc = _git(["cat-file", "-s", new_oid], cwd=review_root)
        size = int(out) if rc == 0 and out.isdigit() else 0
    else:
        out, rc = _git(["diff", "--no-ext-diff", old_oid, new_oid], cwd=review_root)
        size = len(out.encode("utf-8", "replace")) if rc == 0 else 0
    return size > _TRUNC_MAX_BYTES


def _write_run_records(result, active_items, common_dir, fp, run_id, review_root="", tip="",
                       precomputed=None):
    """Write per-file ledger records after a completed review of `active_items`.

    None at all unless the review finished cleanly. A file whose diff the
    reviewer saw only partly -- named by a truncation warning, any file of the
    chunk when the warning names none ("*"), or over the gate's own size cap --
    gets its record flagged `truncated`: the same blob would be truncated the
    same way, so a resume must not review it again, but the flag keeps that
    state visible (_surface_truncated) and keeps it from being a delta base.
    A flagged write never replaces a valid unflagged record; an unflagged one
    replaces a flagged record. Best-effort. Returns the paths flagged.

    `precomputed` ({path: truncated}, see _owned_truncation) covers the files
    whose diff Python itself delivered to the reviewer: for those the flag is
    Python's own and the model's warnings are ignored. For every other file
    (git failed for it, or the 0.9.x path) the rules above apply.
    """
    flagged_paths = set()
    if not isinstance(result, dict):
        return flagged_paths
    if result.get("status", "") not in ("success", "completed_with_warnings"):
        return flagged_paths
    precomputed = precomputed or {}
    owned_here = {item["entry"]["path"] for item in active_items} & set(precomputed)
    truncated = _truncated_paths(result, owned_here)
    # A "*" names no file: with a precomputed diff for EVERY file of the chunk
    # there is no file it can be about.
    if len(owned_here) == len(active_items):
        truncated.discard("*")
    active_paths = {item["entry"]["path"] for item in active_items}
    findings = [f for f in (result.get("findings") or []) if isinstance(f, dict)]
    oids = _blob_oids_at(review_root, tip, sorted({f.get("path") for f in findings if f.get("path")})) \
        if (review_root and tip) else {}
    stamped = [dict(f, target_oid=f.get("target_oid") or oids.get(f.get("path") or "", ""))
               for f in findings]
    new_by_path, orphans = {}, []
    for f in stamped:
        p = f.get("path") or ""
        if p in active_paths:
            new_by_path.setdefault(p, []).append(f)
        else:
            orphans.append(f)
    for item in active_items:
        e = item["entry"]
        path = e["path"]
        old_path = e.get("old_path") or ""
        prev = item.get("record")
        chain_depth = (int(prev.get("chain_depth") or 0) + 1) if (prev and item["mode"] == "delta") else 0
        key = _record_key(path, old_path, e["status"], e["old_oid"])
        if path in precomputed:
            flagged = bool(precomputed[path])
        else:
            flagged = ("*" in truncated or path in truncated
                       or _diff_exceeds_caps(item, review_root))
        if flagged:
            existing = _read_ledger_record(
                _record_path(common_dir, fp, key, e["new_oid"]), fp, key, e["new_oid"])
            if existing is not None and not _is_truncated_record(existing):
                continue  # a complete review of this very blob is already on file
            flagged_paths.add(path)
        _write_ledger_record(
            common_dir, fp, key, e["new_oid"], path, old_path, e["status"], e["old_oid"],
            _record_findings_for(item, new_by_path, orphans, active_paths),
            chain_depth, run_id, truncated=flagged,
        )
        _clear_timeout_mark(common_dir, fp, key, e["new_oid"])
    return flagged_paths


def _drop_self_resolved(active_items, resolved_ids_by_path, common_dir, fp, run_id):
    """Rewrite an active file's record without priors whose fix lies in that
    same file: a property of this content, so it holds for any tip carrying it.
    Fixes evidenced in another file stay in the record; the resolution entry,
    which is keyed on that other file's blob, suppresses them instead."""
    for item in active_items:
        e = item["entry"]
        ids = resolved_ids_by_path.get(e["path"])
        if not ids:
            continue
        key = _record_key(e["path"], e.get("old_path") or "", e["status"], e["old_oid"])
        rec = _read_ledger_record(_record_path(common_dir, fp, key, e["new_oid"]),
                                  fp, key, e["new_oid"])
        if rec is None:
            continue
        kept = [f for f in rec.get("findings") or [] if (f.get("id") or _finding_id(f)) not in ids]
        if len(kept) != len(rec.get("findings") or []):
            _rewrite_record_findings(common_dir, fp, key, rec, kept)


def _update_state_owned(state_path, run_id, **fields):
    """Fence-checked state update under _StateLock.

    Raises _Fenced when another run has taken over (run_id mismatch).
    """
    with _StateLock(state_path):
        st = _read_state(state_path) or {}
        if st.get("run_id") != run_id:
            raise _Fenced()
        st.update(fields)
        _write_state(state_path, st)
    return st


def _findings_overlap(f1, f2):
    """True when two findings' line ranges overlap."""
    try:
        s1, e1 = int(f1.get("start_line") or 0), int(f1.get("end_line") or 0)
        s2, e2 = int(f2.get("start_line") or 0), int(f2.get("end_line") or 0)
        e1 = e1 or s1
        e2 = e2 or s2
        return s1 <= e2 and s2 <= e1 and (s1 or s2) > 0
    except (TypeError, ValueError):
        return False


def _findings_similar(f1, f2):
    """True when two findings have the same path, severity, and overlapping title."""
    if f1.get("path") != f2.get("path"):
        return False
    if f1.get("severity") != f2.get("severity"):
        return False
    if not _findings_overlap(f1, f2):
        return False
    def _norm(f):
        c = re.sub(r'\s+', ' ', str(f.get("content") or "").lower().strip())
        return c[:60]
    return _norm(f1) == _norm(f2)


def _merge_near_dup_findings(findings):
    """Remove near-duplicates; keep the one with higher confidence."""
    kept = []
    for f in findings:
        merged = False
        for i, k in enumerate(kept):
            if _findings_similar(k, f):
                if float(f.get("confidence") or 0) > float(k.get("confidence") or 0):
                    kept[i] = f
                merged = True
                break
        if not merged:
            kept.append(f)
    return kept


def _planner_warning_objs(warnings):
    """Planner warnings in the skill's {file, message} shape."""
    return [w if isinstance(w, dict) else {"file": None, "message": str(w)}
            for w in warnings]


def _merge_chunk_results(chunk_results, planner_warnings=None):
    """Merge chunk review results into one combined result dict.

    `status` follows the skill's vocabulary:
      success < completed_with_warnings < completed_with_errors
    `verdict` stays in block/warn/pass and is recomputed later by
    compute_verdict(findings).  `summary` is the {files_reviewed,
    findings, high, medium, low} object the skill emits.
    """
    all_findings = []
    all_warnings = _planner_warning_objs(planner_warnings or [])
    # Skill-vocabulary status ordering.
    _STATUS_RANK = {
        "success": 0,
        "completed_with_warnings": 1,
        "completed_with_errors": 2,
    }
    worst_status_rank = 0
    cross_symbols, verdicts, dep_verdicts = [], {}, {}
    for r in chunk_results:
        if not isinstance(r, dict):
            continue
        all_findings.extend(r.get("findings") or [])
        all_warnings.extend(r.get("warnings") or [])
        cfs = r.get("cross_file_context_summary")
        if isinstance(cfs, dict) and isinstance(cfs.get("symbols"), list):
            cross_symbols.extend(cfs["symbols"])
        if isinstance(r.get("impact_verdicts"), dict):
            verdicts.update(r["impact_verdicts"])
        if isinstance(r.get("dep_verdicts"), dict):
            dep_verdicts.update(r["dep_verdicts"])
        s = r.get("status") or ""
        rank = _STATUS_RANK.get(s, 0)
        if rank > worst_status_rank:
            worst_status_rank = rank
    merged = _merge_near_dup_findings(all_findings)
    worst_status = ["success", "completed_with_warnings", "completed_with_errors"][
        worst_status_rank
    ]
    high = sum(1 for f in merged if f.get("severity") == "high")
    medium = sum(1 for f in merged if f.get("severity") == "medium")
    low = sum(1 for f in merged if f.get("severity") == "low")
    files_reviewed = len({f.get("path") for f in merged if f.get("path")})
    out = {
        "status": worst_status,
        "findings": merged,
        "warnings": all_warnings,
        "summary": {
            "files_reviewed": files_reviewed,
            "findings": len(merged),
            "high": high, "medium": medium, "low": low,
        },
    }
    if cross_symbols:
        out["cross_file_context_summary"] = {"symbols": cross_symbols}
    if verdicts:
        out["impact_verdicts"] = verdicts
    if dep_verdicts:
        out["dep_verdicts"] = dep_verdicts
    return out


# --- big files in stable units, with caller checks (0.11.0, Part S) ---------------
# A file whose diff is over Part B's per-file limit (_build_item_diff's `u0` or
# `stat` level) is cut into UNITS (scripts/ocr_segment.py) and compared with its
# base unit by unit: a unit whose text is on both sides is never reviewed again,
# so a push that edits one function of a 2,000-line file reviews that function,
# and a run killed halfway resumes with what is left. The reviewer is handed each
# changed unit's base -> tip diff (`unit_diff` items, absolute line numbers; never
# truncated) instead of a degraded whole-file diff.
#
# Each changed named unit A also gets its same-file callers checked: the caller B
# goes in as a `context` item with a `dep:<n>` task ("does B still handle A's
# arguments, return, exceptions, await and state?"), and the reviewer answers in
# `dep_verdicts`. One hop per push -- no cascade through unchanged units. A
# verdict that is missing, malformed or `unsure` is `unsure`, never `ok`.
#
# Records (all under the ledger's fingerprint directory, which already holds
# every input of the review criteria):
#   seg/<sha256(key)>.json  one finished unit review (findings, anchored by
#                           `anchor_hash` + `rel_start`/`rel_end` in the unit)
#   dep/<sha256(key)>.json  one caller check: ok | broken | unsure | pending
# The file's own per-file record is written only when every unit and every
# caller check that touches it is final, so a file is carried whole only once
# nothing is owed on it. OCR_SEGMENT=0 restores the 0.10.0 behaviour exactly.

SEG_VERSION = "1"
_SEG_RECORD_CAP_FACTOR = 4     # unit + caller-check records kept, as a multiple of the record cap
_SEG_DEP_PER_UNIT = 6          # caller checks per changed unit
_SEG_CTX_UNIT_LINES = 80       # lines of one caller context unit
_SEG_CTX_CHUNK_LINES = 400     # caller context lines per chunk
_SEG_WIDEN_BYTES = 12 * 1024   # a cross-file call site widened to its whole unit
_SEG_WIDEN_TOTAL = 48 * 1024
_SEG_MAX_SYMBOLS = 60          # impact symbols per chunk, at most (scales with the push)
_SEG_VERDICTS = ("ok", "broken", "unsure")
_SEG_TASK_TEXT = (
    "Verify that the caller still handles the callee's change: the arguments it "
    "passes, the return value it relies on, exceptions the callee now raises, "
    "async/await, and any state or ordering it assumes. Answer ok, broken (and "
    "emit a finding at the caller, with dep_task set to this task's id) or unsure.")


def _seg_enabled():
    return _precomputed_enabled() and os.environ.get(
        "OCR_SEGMENT", "1").strip().lower() not in ("0", "false", "no")


def _seg_part_lines():
    return max(100, _CHUNK_DIFF_LINES // 2)


def _seg_key(fp, lang, path, base_hash, tip_hash):
    return (f"seg:{SEG_VERSION}:{fp}:{lang}:{ocr_segment.path_hash(path)}:"
            f"{base_hash or '-'}:{tip_hash or '-'}")


def _dep_key(fp, lang, path_b, hash_b, path_a, hash_a):
    return (f"dep:{SEG_VERSION}:{fp}:{lang}:{ocr_segment.path_hash(path_b)}:{hash_b}:"
            f"{ocr_segment.path_hash(path_a)}:{hash_a}")


def _seg_rec_path(common_dir, fp, sub, key):
    return _fp_dir(common_dir, fp) / sub / (hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + ".json")


def _read_seg_rec(common_dir, fp, sub, key):
    """The unit (sub="seg") or caller-check (sub="dep") record for `key`, or None
    (absent, corrupt, another key or fingerprint, expired)."""
    path = _seg_rec_path(common_dir, fp, sub, key)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - path.stat().st_mtime > _LEDGER_TTL:
            return None
    except Exception:
        return None
    if (not isinstance(data, dict) or data.get("schema") != _LEDGER_SCHEMA
            or data.get("key") != key or data.get("fp") != fp):
        return None
    return data


def _write_seg_rec(common_dir, fp, sub, key, payload):
    """Best-effort, like every ledger write."""
    try:
        path = _seg_rec_path(common_dir, fp, sub, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_state(path, dict(payload, schema=_LEDGER_SCHEMA, fp=fp, key=key, ts=time.time()))
        try:
            path.touch(exist_ok=True)
        except Exception:
            pass
        return True
    except Exception:
        return False


def _clean_dep_verdicts(raw):
    """{task id: ok|broken|unsure} from the reviewer's `dep_verdicts`, strictly:
    anything else (a non-object, a value that is not one of the three) is simply
    absent, and an absent verdict counts as `unsure` -- never as `ok`."""
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(k, str) and isinstance(v, str) and v.strip().lower() in _SEG_VERDICTS:
                out[k] = v.strip().lower()
    return out


def _seg_anchor(f, unit, tip_lines):
    """`f` with where it sits in its unit: rel_start/rel_end (0-based offsets from
    the unit's first line) and anchor_hash (the hash of the anchored lines), so a
    later push that only moves the unit can place the finding again."""
    try:
        s = int(f.get("start_line"))
        e = int(f.get("end_line") or s)
    except (TypeError, ValueError):
        return None
    if not (unit["start"] <= s <= unit["end"]):
        return None
    e = max(s, min(e, unit["end"]))
    return dict(f, rel_start=s - unit["start"], rel_end=e - unit["start"],
                anchor_hash=ocr_segment.text_hash("", "anchor", tip_lines[s - 1:e][:200]))


def _seg_remap(f, unit, tip_lines):
    """The stored finding `f` at its unit's CURRENT position, or None.

    Lines are the unit's start plus rel_start; the anchored text must hash to the
    stored anchor_hash. When it does not, the finding's `existing_code` is looked
    for inside the unit; failing that the finding is dropped (the caller logs
    `replay_dropped`) -- a finding pinned on the wrong line is worse than none."""
    try:
        rs, re_ = int(f["rel_start"]), int(f["rel_end"])
    except (KeyError, TypeError, ValueError):
        return None
    out = {k: v for k, v in f.items() if k not in ("rel_start", "rel_end", "anchor_hash")}
    s, e = unit["start"] + rs, unit["start"] + re_
    if rs >= 0 and re_ >= rs and e <= unit["end"] and ocr_segment.text_hash(
            "", "anchor", tip_lines[s - 1:e][:200]) == f.get("anchor_hash"):
        return dict(out, start_line=s, end_line=e)
    code = [ocr_segment.norm_line(x).strip() for x in str(f.get("existing_code") or "").splitlines()
            if x.strip()]
    if code:
        want = [" ".join(x.split()) for x in code]
        body = [" ".join(x.split()) for x in tip_lines[unit["start"] - 1:unit["end"]]]
        for i in range(len(body) - len(want) + 1):
            if body[i:i + len(want)] == want:
                return dict(out, start_line=unit["start"] + i, end_line=unit["start"] + i + len(want) - 1)
    return None


def _seg_lines_numbered(sf, a, b):
    return ocr_segment.render_numbered(sf.tip_lines, a, b)


class _SegFile:
    """One file under Part S. Either reviewed in units (`delta` False), or a small
    delta-mode file that is reviewed as before and only gets its callers checked."""

    def __init__(self, item, seg_base, seg_tip, changes, tip_text, delta=False):
        self.item = item
        self.entry = item["entry"]
        self.path = self.entry["path"]
        self.old_path = self.entry.get("old_path") or ""
        self.lang = seg_tip["lang"]
        self.method = seg_tip["method"]
        self.base_lines, self.tip_lines = seg_base["lines"], seg_tip["lines"]
        self.base_units, self.tip_units = seg_base["units"], seg_tip["units"]
        self.tip_text = tip_text
        self.changes = changes
        self.delta = delta
        self.deps = []
        self.extra = []          # findings in this file, outside every unit under review
        self.orphans = []        # findings about other files, raised in this file's chunks
        self.result = None       # delta files: the chunk result waiting for its caller checks
        self.written = False
        self._parts = {}

    def parts(self, ch):
        """The unit's diff, as a list of texts (one per part): never truncated."""
        if id(ch) not in self._parts:
            self._parts[id(ch)] = ocr_segment.unit_diff_parts(
                self.path, self.old_path, ch, self.base_lines, self.tip_lines, _seg_part_lines())
        return self._parts[id(ch)]

    def change_at(self, line, among=None):
        for ch in (among if among is not None else self.changes):
            t = ch["tip"]
            if t and t["start"] <= line <= t["end"]:
                return ch
        return None

    def ready(self):
        return (all(ch["final"] for ch in self.changes) if not self.delta else True) and \
            all(d["status"] == "final" for d in self.deps)


class _SegState:
    """Everything Part S knows about one run: the segmented files, their cached
    and pending work, the chunk plan, and what to write after each chunk."""

    def __init__(self, review_root, base, tip, common_dir, fp, run_id, use_cache):
        self.review_root, self.base, self.tip = review_root, base, tip
        self.common_dir, self.fp, self.run_id = common_dir, fp, run_id
        self.use_cache = bool(use_cache and common_dir and fp)
        self.write = bool(common_dir and fp and _ledger_enabled())
        self.run = ocr_impact.git_runner(review_root)
        self.files = {}              # path -> _SegFile
        self.declined = {}           # path -> reason
        self.replayed = []           # findings of cached units, at their current lines
        self.replayed_foreign = []   # cached findings about OTHER files (need re-judging)
        self.unverified = []         # info findings: a caller check that stayed unsure
        self.warnings = []
        self.dep_seq = 0
        self.first_chunk = {}
        self.stats = {"units": 0, "hits": 0, "items": 0, "deps": 0, "dep_hits": 0,
                      "dropped": 0, "declined": 0}
        self.diffs = {}
        self.trials = {}             # flagged-truncated carries that can be segmented
        self._counters = {}          # directory -> numbering of its unit and context files
        self.orig_diffs = {}         # path -> the whole-file diff replaced by units

    # --- building ------------------------------------------------------------------

    def _decline(self, path, reason):
        self.declined[path] = reason
        self.stats["declined"] += 1
        _metric_log("seg_decline", reason=reason)
        return None

    def _blob(self, oid):
        out, rc = self.run(["cat-file", "-p", oid])
        return out if rc == 0 else None

    def build(self, item, delta=False, trial=False):
        """The _SegFile for a plan item, or None -- the file is then reviewed whole,
        exactly as without Part S (the reason is in self.declined). Never raises.
        A `trial` only proves the file can be segmented: nothing is looked up or
        registered until complete() decides the file really is reviewed in units."""
        path = item["entry"]["path"]
        try:
            sf = self._build(item, delta)
        except Exception as exc:
            return self._decline(path, "error_" + type(exc).__name__[:30])
        if sf is not None and not trial:
            self.complete(sf)
        return sf

    def _build(self, item, delta):
        e = item["entry"]
        path = e["path"]
        old_oid = item.get("from_oid") if delta else e.get("old_oid")
        new_oid = e.get("new_oid")
        base_text = "" if _is_null_oid(old_oid) else self._blob(old_oid)
        tip_text = self._blob(new_oid) if not _is_null_oid(new_oid) else None
        if base_text is None or tip_text is None:
            return self._decline(path, "unreadable")
        if "\0" in base_text or "\0" in tip_text:
            return self._decline(path, "binary")
        seg_t = ocr_segment.segment(path, tip_text)
        seg_b = ocr_segment.segment(path, base_text)
        if max(len(seg_t["units"]), len(seg_b["units"])) > ocr_segment.MAX_UNITS:
            return self._decline(path, "too_many_units")
        for seg in (seg_t, seg_b):
            for i, u in enumerate(seg["units"]):
                u["idx"] = i
        pairing = ocr_segment.pair_units(seg_b["units"], seg_t["units"],
                                         seg_b["lines"], seg_t["lines"])
        changes = ocr_segment.changes_of(pairing, seg_b["units"], seg_t["units"])
        if not delta:
            if _is_null_oid(old_oid):
                tip_changed, base_changed = set(range(1, len(seg_t["lines"]) + 1)), set()
            else:
                out, rc = self.run(
                    ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "-U0",
                     "--ignore-space-at-eol", "--ignore-cr-at-eol", "--ignore-blank-lines",
                     old_oid, new_oid])
                if rc != 0:
                    return self._decline(path, "diff_failed")
                tip_changed, base_changed = ocr_segment.parse_changed_lines(out)
            gaps = ocr_segment.check_coverage(
                seg_b["units"], seg_t["units"], tip_changed, base_changed,
                len(seg_b["lines"]), len(seg_t["lines"]), seg_b["lines"], seg_t["lines"])
            if gaps:
                _debug_log(f"seg coverage gap, file reviewed whole: {gaps[0]}")
                return self._decline(path, "coverage")
            for ch in changes:
                for side, lines in ((ch["tip"], seg_t["lines"]), (ch["base"], seg_b["lines"])):
                    if side and ocr_segment.max_line_len(lines, side["start"], side["end"]) > _DIFF_LINE_CAP:
                        # a cut line is a line the reviewer did not see: not a unit review
                        return self._decline(path, "long_lines")
        sf = _SegFile(item, seg_b, seg_t, changes, tip_text, delta=delta)
        for ch in changes:
            bh = ch["base"]["hash"] if ch["base"] else ""
            th = ch["tip"]["hash"] if ch["tip"] else ""
            ch.update(key=_seg_key(self.fp, sf.lang, path, bh, th), done=set(), findings=[],
                      final=False, record=None)
        return sf

    def complete(self, sf):
        """The file is reviewed in units (or, `delta`, gets caller checks): register
        it, replay what is cached and plan its caller checks."""
        self.files[sf.path] = sf
        if not sf.delta:
            self.stats["units"] += len(sf.changes)
            for ch in sf.changes:
                self._lookup(sf, ch)
        self._plan_deps(sf)

    def _lookup(self, sf, ch):
        """A cached review of exactly this unit change replays its findings."""
        if not self.use_cache:
            return
        rec = _read_seg_rec(self.common_dir, self.fp, "seg", ch["key"])
        if rec is None:
            return
        ch["final"], ch["record"] = True, rec
        self.stats["hits"] += 1
        for f in rec.get("findings") or []:
            if not isinstance(f, dict):
                continue
            if "rel_start" in f and ch["tip"] and f.get("path") == sf.path:
                g = _seg_remap(f, ch["tip"], sf.tip_lines)
                if g is None:
                    self.stats["dropped"] += 1
                    _metric_log("replay_dropped", kind="unit")
                    continue
                ch["findings"].append(g)
                self.replayed.append(dict(g, provenance="carried"))
            elif f.get("path") != sf.path:
                self.replayed_foreign.append(dict(f, provenance="carried"))
            else:   # a finding of this file outside its units: kept as it was
                sf.extra.append(f)
                self.replayed.append(dict(f, provenance="carried"))

    # --- caller checks -------------------------------------------------------------

    def _plan_deps(self, sf):
        """Same-file callers of every changed named unit become caller checks. A unit
        that was removed or renamed is checked under its OLD name too: a caller that
        still uses it is exactly what such a change breaks."""
        kinds_ = ("function", "method", "class")
        jobs = [(ch, ch["tip"], False) for ch in sf.changes if ch["tip"] and ch["tip"]["kind"] in kinds_]
        for ch in sf.changes:
            b, t = ch["base"], ch["tip"]
            if b and b["kind"] in kinds_ and (t is None or b["qualname"] != t["qualname"]):
                jobs.append((ch, b, True))
        if not jobs:
            return
        calls = ocr_segment.unit_calls(sf.lang, sf.tip_text, sf.tip_lines, sf.tip_units)
        units = list(sf.tip_units) + [{"provides": u["provides"], "kind": u["kind"]}
                                      for _, u, stale in jobs if stale]
        extra, targets = len(sf.tip_units), []
        for _, u, stale in jobs:
            if stale:
                targets.append(extra)
                extra += 1
            else:
                targets.append(u["idx"])
        edges = ocr_segment.callers_of(sf.lang, units, calls, targets)
        for (ch, callee, stale), target in zip(jobs, targets):
            for hit in edges.get(target, [])[:_SEG_DEP_PER_UNIT]:
                caller = sf.tip_units[hit["caller"]]
                if caller is ch["tip"]:
                    continue
                key = _dep_key(self.fp, sf.lang, sf.path, caller["hash"], sf.path,
                               ("-" if stale else "") + callee["hash"])
                a = max(caller["start"], min(hit["line"] - 30, caller["end"] - _SEG_CTX_UNIT_LINES + 1))
                dep = {"ch": ch, "callee": callee, "stale": stale, "caller": caller,
                       "name": hit["name"], "line": hit["line"],
                       "key": key, "status": "needed", "attempt": 1, "pending": False,
                       "ctx": (a, min(caller["end"], a + _SEG_CTX_UNIT_LINES - 1)),
                       "id": "", "findings": [], "file": sf}
                rec = _read_seg_rec(self.common_dir, self.fp, "dep", key) if self.use_cache else None
                self.stats["deps"] += 1
                st = (rec or {}).get("status")
                if st in ("ok", "broken", "unsure_final"):
                    dep["status"] = "final"
                    self.stats["dep_hits"] += 1
                    self._replay_dep(sf, dep, rec)
                elif st == "unsure":
                    dep["attempt"] = int(rec.get("attempts") or 1) + 1
                    dep["pending"] = True
                elif st == "pending":
                    dep["pending"] = True
                elif self.write and self.use_cache:
                    _write_seg_rec(self.common_dir, self.fp, "dep", key,
                                   {"kind": "dep", "status": "pending", "path": sf.path})
                sf.deps.append(dep)

    def _replay_dep(self, sf, dep, rec):
        if rec.get("status") == "unsure_final":
            self._unverified(sf, dep)
            return
        for f in rec.get("findings") or []:
            if isinstance(f, dict) and "rel_start" in f:
                g = _seg_remap(f, dep["caller"], sf.tip_lines)
                if g is None:
                    self.stats["dropped"] += 1
                    _metric_log("replay_dropped", kind="dep")
                    continue
                dep["findings"].append(g)
                self.replayed.append(dict(g, provenance="carried"))

    def _unverified(self, sf, dep):
        c, a = dep["caller"], dep["callee"]
        f = {"severity": "info", "path": sf.path, "start_line": c["start"], "end_line": c["start"],
             "category": "correctness", "provenance": "unverified_dependency",
             "content": (f"unverified dependency: could not confirm that {c.get('qualname') or 'this code'} "
                         f"still handles the change to {a.get('qualname') or 'a unit it calls'} "
                         "(the reviewer could not decide twice); check this caller by hand")}
        self.unverified.append(f)

    # --- the plan ------------------------------------------------------------------

    def convert_truncated_carries(self, plan):
        """Part A carries a record flagged `truncated` instead of reviewing the same
        blob again. A file Part S CAN segment finally gets its proper review: the
        item becomes a full one (its record kept, so what it still owed is
        re-judged as before). A file that cannot be segmented keeps being carried
        -- reviewing it whole would truncate it the same way, and never converge."""
        for p in plan or []:
            if p["mode"] != "carry" or not _is_truncated_record(p.get("record")):
                continue
            sf = self.build(p, trial=True)
            if sf is None:
                continue
            self.trials[p["entry"]["path"]] = sf
            p.update(mode="full", from_oid="", miss_reason="no_record")

    def plan_active(self, active_items, diffs):
        """Segment the active files that are over Part B's limit, and give every
        small delta file its caller checks. Mutates `diffs` for the segmented
        files: Python delivers them in units, so the whole-file flags no longer
        apply (a model's truncation warning about them is ignored)."""
        run = ocr_impact.git_runner(self.review_root)
        for item in active_items:
            path = item["entry"]["path"]
            saved_item, saved_d = dict(item), diffs.get(path)
            try:
                self._plan_one(item, diffs, run)
            except Exception as exc:
                # Part S is an optimisation: whatever goes wrong, the file is reviewed whole
                self.files.pop(path, None)
                self.orig_diffs.pop(path, None)
                item.clear()
                item.update(saved_item)
                if saved_d is not None:
                    diffs[path] = saved_d
                self._decline(path, "error_" + type(exc).__name__[:30])
        self.diffs = diffs
        for sf in self.files.values():
            if sf.delta and sf.deps:
                self._cap_delta_deps(sf)
        self.finalize_ready()
        _TELE["seg"] = dict(self.stats, files=len(self.files),
                            declined={k: v for k, v in list(self.declined.items())[:20]})
        return self

    def _plan_one(self, item, diffs, run):
        path = item["entry"]["path"]
        d = diffs.get(path)
        usable = d is not None and not d["failed"] and not d["binary"]
        over = usable and d["level"] != "full"
        sf = None
        trial = self.trials.get(path)
        if trial is not None:
            # a flagged-truncated carry that CAN be segmented: reviewed in units when
            # its diff is still over the limit, plainly (and properly) when it is not
            if over:
                self.complete(trial)
                sf = trial
        elif over:
            if item["mode"] == "delta":
                # the units carry the cache now: this file is reviewed over the whole push
                # range (its record stays, for what it still owes)
                saved = dict(item)
                item.update(mode="full", miss_reason="seg")
                sf = self.build(item)
                if sf is None:
                    item.clear()
                    item.update(saved)
                else:
                    try:
                        diffs[path] = d = _build_item_diff(
                            run, _item_diff_spec(item, self.base, self.tip), _CHUNK_DIFF_LINES)
                    except Exception:
                        diffs[path] = d = _failed_diff()
            else:
                sf = self.build(item)
        elif usable and item["mode"] == "delta":
            sf = self.build(item, delta=True)
            if sf is not None and not sf.deps:
                del self.files[path]
                sf = None
        if sf is not None and not sf.delta and d is not None:
            self.orig_diffs[path] = d
            diffs[path] = dict(d, owned=True, truncated=False, segmented=True)

    def abandon(self, diffs):
        """Part S gave up on this run (a planning error): every unit-reviewed file goes
        back to the whole-file diff it had, flags and all, and is reviewed as in 0.10.0."""
        for path, d in self.orig_diffs.items():
            diffs[path] = d
        self.files = {}
        self.orig_diffs = {}

    def _cap_delta_deps(self, sf):
        """A delta file's chunk cannot be split, so its caller checks must fit one."""
        kept, total = [], 0
        for d in sf.deps:
            if d["status"] != "needed":
                kept.append(d)
                continue
            n = d["ctx"][1] - d["ctx"][0] + 1
            if total + n > _SEG_CTX_CHUNK_LINES:
                self.warnings.append(
                    f"caller checks capped: {sf.path} has more caller context than one chunk holds; "
                    "the callers of one changed unit were not checked")
                continue
            total += n
            kept.append(d)
        sf.deps = kept

    def active_files(self):
        return [sf for sf in self.files.values() if not sf.delta]

    def segmented_paths(self):
        return {sf.path for sf in self.active_files()}

    # --- chunks --------------------------------------------------------------------

    def _dep_size(self, d, callee_in_chunk):
        n = d["ctx"][1] - d["ctx"][0] + 1
        if not callee_in_chunk:
            n += min(_seg_part_lines(), sum(len(t.splitlines()) for t in d["file"].parts(d["ch"])[:1]))
        return n

    def _clusters(self, sf):
        """Work of one file in call-graph order: [{atoms, size, ctx}] where a cluster
        is one changed unit (its diff parts) with the caller checks that hang off it."""
        out = []
        if sf.delta:
            deps = [d for d in sf.deps if d["status"] == "needed"]
            if not deps:
                return out
            d0 = self.diffs.get(sf.path) or {}
            atoms = [{"kind": "filediff", "size": int(d0.get("lines") or 1)}]
            for d in deps:
                atoms.append({"kind": "dep", "dep": d, "size": self._dep_size(d, True), "callee_in": True})
            ctx = sum(a["size"] for a in atoms[1:])
            return [{"atoms": atoms, "size": sum(a["size"] for a in atoms), "ctx": ctx,
                     "pending": any(d["pending"] for d in deps), "ch": None}]
        for ch in sf.changes:
            atoms = []
            if not ch["final"]:
                parts = sf.parts(ch)
                for j, text in enumerate(parts):
                    atoms.append({"kind": "unit", "ch": ch, "part": j, "parts": len(parts),
                                  "text": text, "size": text.count("\n") + 1})
            for d in [d for d in sf.deps if d["ch"] is ch and d["status"] == "needed"]:
                atoms.append({"kind": "dep", "dep": d, "callee_in": not ch["final"],
                              "size": self._dep_size(d, not ch["final"])})
            if atoms:
                ctx = sum(a["size"] for a in atoms if a["kind"] == "dep")
                out.append({"atoms": atoms, "size": sum(a["size"] for a in atoms), "ctx": ctx,
                            "pending": any(a["kind"] == "dep" and a["dep"]["pending"] for a in atoms),
                            "ch": ch})
        # keep units linked by a call (a changed caller of a changed unit) side by side
        by_ch = {id(c["ch"]): i for i, c in enumerate(out)}
        parent = list(range(len(out)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for i, c in enumerate(out):
            for a in c["atoms"]:
                if a["kind"] != "dep":
                    continue
                other = next((x for x in sf.changes if x["tip"] is a["dep"]["caller"]), None)
                j = by_ch.get(id(other)) if other is not None else None
                if j is not None:
                    parent[find(i)] = find(j)
        order = sorted(range(len(out)), key=lambda i: (find(i), i))
        first = {}
        for i in order:
            first.setdefault(find(i), len(first))
        order.sort(key=lambda i: (first[find(i)], i))
        return [out[i] for i in order]

    def groups_of(self, sf, budget, ucap):
        """A file's clusters packed into groups of at most `budget` lines (and `ucap`
        clusters, and the caller context cap). A file is split only when it alone
        exceeds the budget; a cluster alone exceeding it (a monolithic unit) is
        split into its parts."""
        groups, cur = [], None

        def flush():
            nonlocal cur
            if cur:
                groups.append(cur)
            cur = None

        def add(atoms, size, ctx, pending, members=1):
            nonlocal cur
            if cur and (cur["size"] + size > budget or cur["ctx"] + ctx > _SEG_CTX_CHUNK_LINES
                        or cur["members"] + members > ucap):
                flush()
            if cur is None:
                cur = {"sf": sf, "atoms": [], "size": 0, "ctx": 0, "members": 0, "pending": False,
                       "plain": None}
            cur["atoms"] += atoms
            cur["size"] += size
            cur["ctx"] += ctx
            cur["members"] += members
            cur["pending"] = cur["pending"] or pending

        for cl in self._clusters(sf):
            if sf.delta or cl["size"] <= budget:
                add(cl["atoms"], cl["size"], cl["ctx"], cl["pending"])
                continue
            flush()
            for a in cl["atoms"]:
                dep = a["kind"] == "dep"
                add([a], a["size"], a["size"] if dep else 0, dep and a["dep"]["pending"])
        flush()
        return groups

    def plan_chunks(self, plain_items, marks, sizes, budget, impact=None):
        """Chunks of work, as lists of items _build_review_manifest understands:
        the plain (whole-file) active items as they are, and for each segmented file
        one virtual item per chunk holding that chunk's share of its units and
        caller checks. Priority to chunks with caller checks owed from an earlier
        run (`pending`)."""
        marks = marks or {}
        groups = []
        for item in plain_items:
            p = item["entry"]["path"]
            groups.append({"sf": None, "plain": item, "size": max(1, (sizes or {}).get(p) or 1),
                           "ctx": 0, "members": 1, "pending": False, "atoms": []})
        for sf in self.files.values():
            ucap = 1 << 30
            mark = marks.get(sf.path)
            if mark:
                ucap = max(1, int(mark.get("chunk_size") or 1) // 2)
            own = self.groups_of(sf, budget, ucap)
            if sf.delta:
                if not own:
                    continue        # nothing to check: the file stays an ordinary plain item
                # its delta diff rides in the virtual item below, not as a plain item
                groups = [g for g in groups if g.get("plain") is not sf.item]
            groups += own
        groups = self._link_order(groups, impact)
        chunks, cur, size, ctx, files = [], [], 0, 0, set()
        for g in groups:
            f = {g["sf"].path if g["sf"] else g["plain"]["entry"]["path"]}
            if cur and (size + g["size"] > budget or ctx + g["ctx"] > _SEG_CTX_CHUNK_LINES
                        or len(files | f) > _CHUNK_FILES):
                chunks.append(cur)
                cur, size, ctx, files = [], 0, 0, set()
            cur.append(g)
            size += g["size"]
            ctx += g["ctx"]
            files |= f
        if cur:
            chunks.append(cur)
        out = []
        for ch in chunks:
            cap = sum(g["members"] for g in ch)
            for g in ch:
                p = g["sf"].path if g["sf"] else g["plain"]["entry"]["path"]
                if marks.get(p):
                    cap = min(cap, max(1, int(marks[p].get("chunk_size") or 1) // 2))
            piece, n = [], 0
            for g in ch:                     # pieces of at most `cap` members (whole groups)
                if piece and n + g["members"] > cap:
                    out.append(piece)
                    piece, n = [], 0
                piece.append(g)
                n += g["members"]
            if piece:
                out.append(piece)
        out.sort(key=lambda gs: not any(g["pending"] for g in gs))
        result = []
        for k, gs in enumerate(out):
            items, by_sf = [], {}
            for g in gs:
                if g["sf"] is None:
                    items.append(g["plain"])
                    continue
                vi = by_sf.get(g["sf"].path)
                if vi is None:
                    vi = self._virtual(g["sf"])
                    by_sf[g["sf"].path] = vi
                    items.append(vi)
                    self.first_chunk.setdefault(g["sf"].path, k)
                vi["seg_work"] += g["atoms"]
                vi["members"] += g["members"]
            self._assign_ids(items)
            result.append(items)
        return result

    def _virtual(self, sf):
        it = sf.item
        return {"entry": it["entry"], "mode": it["mode"] if sf.delta else "full",
                "record": it.get("record"), "from_oid": (it.get("from_oid") or "") if sf.delta else "",
                "miss_reason": it.get("miss_reason"), "delta_lines": it.get("delta_lines"),
                "seg_file": sf, "seg": self, "seg_work": [], "members": 0}

    def _assign_ids(self, items):
        for vi in items:
            for a in vi.get("seg_work") or []:
                if a["kind"] == "dep" and not a["dep"]["id"]:
                    self.dep_seq += 1
                    a["dep"]["id"] = f"dep:{self.dep_seq}"

    def _link_order(self, groups, impact):
        """Groups of files that call each other (a call site of a changed symbol, from
        the impact search) are put side by side, so they tend to share a chunk."""
        if not impact or len(groups) < 3:
            return groups
        defined = {}
        for s in impact.get("symbols") or []:
            defined.setdefault(s.get("name"), set()).add(s.get("defined_in"))
        path_of = [g["sf"].path if g["sf"] else g["plain"]["entry"]["path"] for g in groups]
        have = set(path_of)
        links = {}
        for s in impact.get("sites") or []:
            for d in defined.get(s.get("name"), ()):
                if d in have and s.get("path") in have and d != s["path"]:
                    links.setdefault(d, set()).add(s["path"])
                    links.setdefault(s["path"], set()).add(d)
        if not links:
            return groups
        out, seen = [], set()
        for i, g in enumerate(groups):
            if i in seen:
                continue
            comp, todo = set(), [path_of[i]]
            while todo:
                p = todo.pop()
                if p in comp:
                    continue
                comp.add(p)
                todo += list(links.get(p, ()))
            for j, g2 in enumerate(groups):
                if j not in seen and path_of[j] in comp:
                    seen.add(j)
                    out.append(g2)
        return out

    # --- what the reviewer is handed -------------------------------------------------

    def manifest_items(self, directory, vi):
        """(items, tasks) for one virtual item: unit_diff items, a file-context item,
        and a context item plus a task for every caller check. Files are written
        under `directory` with names generated here."""
        sf = vi["seg_file"]
        work = vi["seg_work"]
        items, tasks = [], []
        # one numbering per chunk directory: two files of a chunk must not share a name
        files = self._counters.setdefault(str(directory), {"u": 0, "c": 0})

        def write(prefix, ext, text):
            files[prefix] += 1
            return _write_diff_file(directory, files[prefix], text,
                                    name=f"{prefix}{files[prefix]:03d}{ext}")

        def meta(text):
            return {"lines": text.count("\n") + 1, "bytes": len(text.encode("utf-8", "replace")) + 1}

        units = [a for a in work if a["kind"] == "unit"]
        if units:
            changed_ids = {a["ch"]["tip"]["idx"] for a in units if a["ch"]["tip"]}
            pre = ocr_segment.preamble_lines(sf.tip_units, sf.tip_lines)
            index = ocr_segment.signature_index(sf.tip_units, changed_ids)
            text = (f"# {sf.path} at the tip: context only. Only the unit_diff items of this file are "
                    "under review; the other units are NOT part of this review.\n"
                    f"# imports and preamble (first {len(pre)} lines):\n" + "\n".join(pre)
                    + "\n# units of the file (line range, signature); [CHANGED] = under review:\n"
                    + "\n".join(index))
            items.append({"kind": "context", "role": "file_context", "path": sf.path,
                          "file": write("c", ".txt", text)})
        for a in units:
            ch = a["ch"]
            u = ch["tip"] or ch["base"]
            text = a["text"]
            it = {"kind": "unit_diff", "path": sf.path, "old_path": sf.old_path, "mode": "full",
                  "unit": u.get("qualname") or u.get("sig") or u["kind"], "unit_kind": u["kind"],
                  "start_line": ch["tip"]["start"] if ch["tip"] else 0,
                  "end_line": ch["tip"]["end"] if ch["tip"] else 0,
                  "part": a["part"] + 1, "parts": a["parts"], "file": write("u", ".diff", text),
                  "truncated": False, "binary": False, "level": "full"}
            if not ch["tip"]:
                it.update(deleted=True, base_start=ch["base"]["start"], base_end=ch["base"]["end"])
            it.update(meta(text))
            items.append(it)
        in_chunk = {id(a["ch"]) for a in units}
        for a in work:
            if a["kind"] != "dep":
                continue
            d = a["dep"]
            callee = d["callee"]
            if id(d["ch"]) not in in_chunk and not a.get("callee_in"):
                text = "\n".join(sf.parts(d["ch"])[:1])
                items.append({"kind": "context", "role": "callee_diff", "task": d["id"], "path": sf.path,
                              "unit": callee.get("qualname") or callee.get("sig"),
                              "start_line": callee["start"], "end_line": callee["end"],
                              "file": write("c", ".txt",
                                            "# the change to the callee (a unit already reviewed) -- "
                                            "context for the caller check " + d["id"] + "\n" + text)})
            a0, b0 = d["ctx"]
            items.append({"kind": "context", "role": "caller", "task": d["id"], "path": sf.path,
                          "unit": d["caller"].get("qualname") or d["caller"].get("sig"),
                          "start_line": a0, "end_line": b0,
                          "file": write("c", ".txt", f"# caller {d['id']}: {sf.path} lines {a0}-{b0} "
                                                      "at the tip (numbered)\n" + _seg_lines_numbered(sf, a0, b0))})
            tasks.append({"id": d["id"], "type": "dep_check",
                          "callee": dict(
                              {"path": sf.path, "unit": callee.get("qualname") or callee.get("sig"),
                               "start_line": callee["start"], "end_line": callee["end"]},
                              **({"removed_or_renamed": True} if d["stale"] else {})),
                          "caller": {"path": sf.path, "unit": d["caller"].get("qualname") or d["caller"].get("sig"),
                                     "start_line": a0, "end_line": b0},
                          "instruction": _SEG_TASK_TEXT})
        return items, tasks

    # --- impact routing --------------------------------------------------------------

    def annotate_impact(self, impact):
        """Tag each impact symbol defined in a segmented file with the unit change it
        belongs to (by line: the tip unit, or the base unit for a removal)."""
        for s in (impact or {}).get("symbols") or []:
            sf = self.files.get(s.get("defined_in"))
            if sf is None or sf.delta:
                continue
            line = int(s.get("line") or 0)
            if s.get("change") in ("removed", "renamed"):
                ch = next((c for c in sf.changes if c["base"] and c["base"]["start"] <= line <= c["base"]["end"]), None)
            else:
                ch = sf.change_at(line)
            s["_unit"] = ch["id"] if ch else None

    def route_symbols(self, impact, chunk_items, k):
        """The impact symbols for chunk k: those of plain files as before, and for a
        segmented file only the symbols whose unit is in this chunk (a symbol with no
        unit goes with the file's first chunk)."""
        here = {}
        for vi in chunk_items:
            sf = vi.get("seg_file")
            if sf is not None and not sf.delta:
                here[sf.path] = {a["ch"]["id"] for a in vi["seg_work"] if a["kind"] == "unit"}
        out = []
        for s in impact["symbols"]:
            sf = self.files.get(s.get("defined_in"))
            if sf is None or sf.delta:
                out.append(s)
                continue
            if s["defined_in"] not in here:
                continue
            u = s.get("_unit")
            if (u is not None and u in here[s["defined_in"]]) or (
                    u is None and self.first_chunk.get(s["defined_in"]) == k):
                out.append({a: b for a, b in s.items() if a != "_unit"})
        return out

    def widen_sites(self, bundle):
        """A call site in a file outside the review, of a symbol of a segmented file,
        shows its whole enclosing unit (up to 12 KB) instead of +-6 lines."""
        total, cache = 0, {}
        for site in bundle.get("sites") or []:
            if site.get("defined_in") not in self.files:
                continue
            path = site.get("path")
            if path not in cache:
                out, rc = self.run(["show", f"{self.tip}:{path}"])
                seg = ocr_segment.segment(path, out) if rc == 0 else None
                cache[path] = seg
            seg = cache[path]
            if not seg:
                continue
            line = int(site.get("line") or 0)
            u = next((x for x in seg["units"] if x["start"] <= line <= x["end"] and x["kind"] != "region"), None)
            if u is None:
                continue
            text = ocr_segment.render_numbered(seg["lines"], u["start"], u["end"])
            if len(text.encode("utf-8", "replace")) > _SEG_WIDEN_BYTES \
                    or total + len(text.encode("utf-8", "replace")) > _SEG_WIDEN_TOTAL:
                continue
            total += len(text.encode("utf-8", "replace"))
            site["snippet"] = text
            site["widened"] = True

    # --- after a chunk ---------------------------------------------------------------

    def apply(self, vitems, result):
        """Fold one chunk's answer into the unit and caller-check records.

        Everything is written only for a result of a review that finished cleanly
        (the status rule of _write_run_records); a caller check with no valid
        verdict is `unsure`. Returns nothing: findings stay in `result`, which the
        caller merges as always."""
        ok = isinstance(result, dict) and result.get("status", "") in ("success", "completed_with_warnings")
        findings = [f for f in ((result or {}).get("findings") or []) if isinstance(f, dict)] if ok else []
        verdicts = _clean_dep_verdicts((result or {}).get("dep_verdicts")) if ok else {}
        chunk = {vi["seg_file"].path: vi for vi in vitems if vi.get("seg_file") is not None}
        deps = {a["dep"]["id"]: a["dep"] for vi in vitems for a in vi.get("seg_work") or []
                if a["kind"] == "dep"}
        if not ok:
            return
        # progress and attribution
        in_chunk = {}
        for vi in vitems:
            for a in vi.get("seg_work") or []:
                if a["kind"] == "unit":
                    a["ch"]["done"].add(a["part"])
                    in_chunk.setdefault(vi["seg_file"].path, []).append(a["ch"])
        orphans = []
        for f in findings:
            task = f.get("dep_task")
            if task in deps:
                deps[task]["findings"].append(f)
                continue
            sf = (chunk.get(f.get("path")) or {}).get("seg_file")
            if sf is not None and not sf.delta:
                try:
                    line = int(f.get("start_line"))
                except (TypeError, ValueError):
                    line = 0
                ch = sf.change_at(line, in_chunk.get(sf.path, []))
                if ch is not None:
                    ch["findings"].append(f)
                else:
                    sf.extra.append(f)
                continue
            orphans.append(f)
        for vi in vitems:
            sf = vi.get("seg_file")
            if sf is not None:
                if sf.delta:
                    sf.result = result   # its record is written by _write_run_records, when its checks are final
                else:
                    sf.orphans += orphans
        oids = _blob_oids_at(self.review_root, self.tip, sorted(
            {f.get("path") for f in orphans if f.get("path")})) if orphans else {}
        stamped = [dict(f, target_oid=f.get("target_oid") or oids.get(f.get("path") or "", ""))
                   for f in orphans]
        # unit records
        for vi in vitems:
            sf = vi.get("seg_file")
            if sf is None or sf.delta:
                continue
            for ch in {id(a["ch"]): a["ch"] for a in vi["seg_work"] if a["kind"] == "unit"}.values():
                parts = len(sf.parts(ch))
                if ch["final"] or len(ch["done"]) < parts:
                    continue
                recf = []
                for f in ch["findings"]:
                    g = _seg_anchor(f, ch["tip"], sf.tip_lines) if ch["tip"] else None
                    recf.append(dict(g if g is not None else f, target_oid=f.get("target_oid") or sf.entry["new_oid"]))
                if self.write:
                    _write_seg_rec(self.common_dir, self.fp, "seg", ch["key"], {
                        "kind": "seg", "path": sf.path, "lang": sf.lang, "unit": (ch["tip"] or ch["base"]).get("qualname") or "",
                        "findings": recf + stamped, "run_id": self.run_id})
                ch["final"] = True
        # caller checks
        for tid, d in deps.items():
            sf = d["file"]
            v = verdicts.get(tid, "unsure")
            if v == "unsure" and tid not in verdicts:
                _metric_log("dep_verdict_missing")
            self._finish_dep(sf, d, v)
        for sf in {vi["seg_file"] for vi in vitems if vi.get("seg_file") is not None}:
            self.finalize(sf)

    def _finish_dep(self, sf, d, verdict):
        rec = {"kind": "dep", "path": sf.path, "attempts": d["attempt"], "run_id": self.run_id}
        if verdict == "ok":
            d["status"] = "final"
            rec["status"] = "ok"
        elif verdict == "broken":
            d["status"] = "final"
            if not d["findings"]:   # `broken` with nothing to show: still never silent
                a = d["callee"]
                d["findings"].append({
                    "severity": "medium", "confidence": 0.6, "category": "correctness",
                    "path": sf.path, "start_line": d["caller"]["start"], "end_line": d["caller"]["start"],
                    "content": (f"{d['caller'].get('qualname') or 'a caller'} may be broken by the change to "
                                f"{a.get('qualname') or 'a unit it calls'} (the reviewer answered `broken` "
                                "without a finding)"), "evidence": "dep_verdicts", "dep_task": d["id"]})
            rec["status"] = "broken"
            rec["findings"] = []
            for f in d["findings"]:
                g = _seg_anchor(f, d["caller"], sf.tip_lines)
                rec["findings"].append(dict(g if g is not None else f,
                                            target_oid=f.get("target_oid") or sf.entry["new_oid"]))
        elif d["attempt"] >= 2:
            d["status"] = "final"
            rec["status"] = "unsure_final"
            self._unverified(sf, d)
        else:
            d["status"] = "retry"          # asked once more, in this run, after the other chunks
            rec["status"] = "unsure"
        if self.write:
            _write_seg_rec(self.common_dir, self.fp, "dep", d["key"], rec)

    def retry_chunks(self):
        """The caller checks that were `unsure` the first time, asked once more: chunks
        of context-only virtual items (the callee's diff, the caller, the task)."""
        todo = [d for sf in self.files.values() for d in sf.deps if d["status"] == "retry"]
        if not todo:
            return []
        chunks, cur, ctx = [], {}, 0
        for d in todo:
            d["attempt"] = 2
            d["findings"] = []
            n = self._dep_size(d, False)
            if cur and ctx + n > _SEG_CTX_CHUNK_LINES:
                chunks.append(cur)
                cur, ctx = {}, 0
            sf = d["file"]
            vi = cur.get(sf.path)
            if vi is None:
                vi = self._virtual(sf)
                vi["mode"] = "full"
                cur[sf.path] = vi
            vi["seg_work"].append({"kind": "dep", "dep": d, "size": n, "callee_in": False})
            vi["members"] = 1
            ctx += n
        if cur:
            chunks.append(cur)
        out = []
        for c in chunks:
            items = list(c.values())
            self._assign_ids(items)
            out.append(items)
        return out

    # --- finishing a file -----------------------------------------------------------

    def finalize_ready(self):
        for sf in list(self.files.values()):
            self.finalize(sf)

    def finalize(self, sf):
        """Write the file's own per-file record once nothing is owed on it: every unit
        reviewed (or cached) and every caller check final. That record is what makes
        the next push of this same file state a plain carry."""
        if sf.written or not sf.ready() or not self.write:
            return False
        if any(d["status"] != "final" for d in sf.deps):
            return False
        item, e = sf.item, sf.entry
        if sf.delta:
            if sf.result is None:
                return False
            _write_run_records(sf.result, [item], self.common_dir, self.fp, self.run_id,
                               self.review_root, self.tip,
                               precomputed={sf.path: bool((self.diffs.get(sf.path) or {}).get("truncated"))})
            sf.written = True
            return True
        own = []
        for ch in sf.changes:
            own += ch["findings"]
        for d in sf.deps:
            own += d["findings"]
        own += sf.extra
        oids = _blob_oids_at(self.review_root, self.tip, [sf.path])
        stamped_own = [dict(f, target_oid=f.get("target_oid") or oids.get(sf.path, "")) for f in own]
        orphans = sf.orphans + self.replayed_foreign
        key = _record_key(sf.path, sf.old_path, e["status"], e["old_oid"])
        _write_ledger_record(
            self.common_dir, self.fp, key, e["new_oid"], sf.path, sf.old_path, e["status"], e["old_oid"],
            _record_findings_for(item, {sf.path: stamped_own}, orphans, {sf.path}), 0, self.run_id,
            truncated=False)
        _clear_timeout_mark(self.common_dir, self.fp, key, e["new_oid"])
        sf.written = True
        return True

    def report(self):
        """Findings the run owes besides the reviewer's: replayed units, unverified
        caller checks."""
        return list(self.replayed), list(self.replayed_foreign), list(self.unverified)


def _seg_classify_foreign(findings, review_root, tip, common_dir, fp, seen):
    """Findings a cached unit review had about OTHER files: the same fate as the
    priors of a carried record -- re-judged when that file changed since, replayed
    when it did not, dropped when the file is gone or a resolution already covers
    them. `seen` (finding ids) is extended."""
    to_resolve, carried = [], []
    for f in findings:
        fid = f.get("id") or _finding_id(f)
        if fid in seen:
            continue
        seen.add(fid)
        fpath = f.get("path") or ""
        blob = _blob_oids_at(review_root, tip, [fpath]).get(fpath, "") if fpath else ""
        target = f.get("target_oid") or ""
        if not blob or _find_valid_resolution(common_dir, fp, fid, target, review_root, tip) is not None:
            continue
        if target and blob != target:
            to_resolve.append({"id": fid, "finding": f, "target_oid": target,
                               "record": {"head_oid": target, "findings": []}})
        else:
            carried.append(f)
    return to_resolve, carried


def _seg_suppress_resolved(findings, priors, review_root, tip, common_dir, fp):
    """Replayed findings of this file's own cached units, minus the ones a recorded
    resolution already covers and the ones a prior classification holds (`priors`,
    finding ids: those are being re-judged, and replaying them as well would show
    a finding twice). Two units with the same text keep their own findings: the
    same finding on two lines is two findings."""
    out = []
    for f in findings:
        fid = f.get("id") or _finding_id(f)
        if fid in priors:
            continue
        if _find_valid_resolution(common_dir, fp, fid, f.get("target_oid") or "",
                                  review_root, tip) is not None:
            continue
        out.append(f)
    return out


def _seg_check_impact_verdicts(result, manifest):
    """Part S requires a verdict for every impact site a chunk was given: a missing
    one is a warning (the site was not judged), not a silent pass."""
    sites = [s.get("id") for s in ((manifest.get("impact") or {}).get("sites") or []) if isinstance(s, dict)]
    if not sites or not isinstance(result, dict):
        return result
    got = result.get("impact_verdicts") if isinstance(result.get("impact_verdicts"), dict) else {}
    missing = [i for i in sites if i not in got]
    if not missing:
        return result
    _metric_log("impact_verdict_missing", n=len(missing))
    warn = {"file": None, "message": f"impact: the reviewer gave no verdict for {len(missing)} "
                                     "call site(s); they were not judged"}
    return dict(result, warnings=list(result.get("warnings") or []) + [warn])


def _run_chunked(state_path, run_id, common_dir, review_root, mode, git_dir,
                 tip, push_range, active_items, planner_warnings, fenced,
                 progress=None, fp="", carry_paths=None, chunk_extras=None,
                 timeout_marks=None, diffs=None, seg=None, impact=None):
    """Run per-chunk reviews with fencing, budget and retry.

    diffs ({path: _build_item_diff result}, see _build_diffs) switches on the
    precomputed-diff path: chunks are packed by the lines of diff delivered
    (_CHUNK_DIFF_LINES) and each chunk's manifest carries items[] with the diff
    files the reviewer reads. None keeps the 0.9.x manifest.

    active_items is a list of plan_item dicts (mode delta/full). Chunks are
    computed internally via _plan_to_chunks. carry_paths is the list of
    already-carried file paths (for the manifest's carried field).
    chunk_extras(k, chunk_paths) returns extra manifest fields for chunk k
    (impact sites, known defects) or None; with `seg` it is called as
    chunk_extras(k, chunk_paths, chunk_items). timeout_marks is _timeout_marks()
    of the active items (read here when not given): chunks holding a file that
    timed out before are split.

    seg (a _SegState, Part S) replaces the packing: files reviewed in units are
    cut into chunks of unit diffs and caller checks, and after the last chunk the
    caller checks that stayed `unsure` are asked once more. With nothing to
    review (every unit cached) there are no chunks at all.

    Returns (merged_result, True, raw_name, chunks_new) on success.
    chunks_new is the count of chunks reviewed in THIS run.
    Raises ReviewLimitError, ReviewBudgetError, ReviewChunkTimeout,
    ReviewUnreviewableError, ReviewGateError, or _Fenced.
    """
    if timeout_marks is None:
        timeout_marks = _timeout_marks(common_dir, fp, active_items)
    chunks = None
    if seg is not None and seg.files:
        try:
            sizes = {p: d["lines"] for p, d in (diffs or {}).items() if not d["failed"] and d["lines"]}
            done_in_units = seg.segmented_paths()
            chunks = seg.plan_chunks(
                [it for it in active_items if it["entry"]["path"] not in done_in_units],
                timeout_marks, sizes, _CHUNK_DIFF_LINES, impact)
            planner_warnings = list(planner_warnings or []) + list(seg.warnings)
        except Exception as exc:
            _metric_log("seg_plan_error", kind=type(exc).__name__[:30])
            _debug_log(f"seg planning failed, reviewing whole files: {type(exc).__name__}: {exc}")
            seg.abandon(diffs or {})
            seg, chunks = None, None
    if chunks is not None:
        pass
    elif diffs is not None:
        sizes = {p: d["lines"] for p, d in diffs.items() if not d["failed"] and d["lines"]}
        chunks = _plan_to_chunks(active_items, timeout_marks, sizes, _CHUNK_DIFF_LINES)
    else:
        chunks = _plan_to_chunks(active_items, timeout_marks)
    total = len(chunks)
    budget_end = time.monotonic() + _RUN_BUDGET
    results_by_k = {}
    progress_n = {"done": 0, "new": 0, "secs": 0.0}

    conc = _chunk_concurrency() if len(chunks) > 1 else 1
    slots = [review_root]
    extra_worktrees = []
    if conc > 1:
        extra_worktrees = _make_slot_worktrees(review_root, tip, run_id, conc - 1)
        if extra_worktrees:
            try:
                _update_state_owned(state_path, run_id,
                                    worktrees=[review_root] + extra_worktrees)
            except _Fenced:
                for wt in extra_worktrees:
                    _remove_worktree(review_root, wt)
                raise
        slots += extra_worktrees
        conc = len(slots)
    stop_evt = threading.Event()   # a sibling failed hard: no more attempts

    def _prepare(k, chunk_items):
        """Write chunk k's manifest (main thread: the impact bundle touches _TELE)."""
        manifest_path = str(_async_dir(common_dir) / f"manifest-{run_id}-{k}.json")
        # other_changed: paths in OTHER chunks that are also being reviewed
        chunk_paths = [item["entry"]["path"] for item in chunk_items if not _context_only(item)]
        own = {it["entry"]["path"] for it in chunk_items}
        other_changed = [
            item["entry"]["path"]
            for i, ch in enumerate(chunks)
            for item in ch
            if i != k and not _context_only(item) and item["entry"]["path"] not in own
        ]
        if chunk_extras is None:
            extras = None
        elif seg is not None:
            extras = chunk_extras(k, chunk_paths, chunk_items)
        else:
            extras = chunk_extras(k, chunk_paths)
        manifest = _build_review_manifest(
            common_dir, run_id, k, len(chunks), chunk_items, other_changed, carry_paths,
            extras, diffs)
        _write_manifest_file(manifest_path, manifest, f"chunk manifest for chunk {k}")
        return manifest, manifest_path

    def _execute(slot_root, k, total_now, chunk_items, manifest_path):
        """Review one chunk in its slot's worktree (a worker thread, or inline at
        concurrency 1). Returns (result, seconds); raises what the review raised."""
        result = None
        last_exc = None
        chunk_t0 = time.monotonic()
        chunk_outcome = "error"
        try:
            # Clean worktree so one chunk can't leave state for the next. Only a
            # worktree the gate made: with no worktree the slot is the user's live
            # tree, and these commands would discard their uncommitted work.
            if _is_gate_worktree(slot_root):
                _git(["clean", "-fdxq"], cwd=slot_root)
                _git(["checkout", "-q", "--", "."], cwd=slot_root)
            else:
                _metric_log("slot_reset_skipped")
            # Run the review (retry once on non-timeout errors).
            for attempt in range(2):
                if fenced["hit"]:
                    chunk_outcome = "fenced"
                    raise _Fenced()
                if stop_evt.is_set():
                    chunk_outcome = "stopped"
                    raise _ChunkStopped()
                try:
                    result, _, _ = _run_review(
                        slot_root, mode, git_dir, tip, push_range,
                        paths_file=manifest_path,
                        timeout=_CHUNK_TIMEOUT,
                        raw_tag=f"-c{k}",
                    )
                    last_exc = None
                    break
                except ReviewLimitError:
                    chunk_outcome = "limit"
                    raise  # propagate immediately; do not retry limits
                except ReviewGateError as exc:
                    last_exc = exc
                    if fenced["hit"]:
                        chunk_outcome = "fenced"
                        raise _Fenced()
                    if stop_evt.is_set():
                        # killed by the gate because a sibling failed: not this chunk's fault
                        chunk_outcome = "stopped"
                        raise _ChunkStopped() from exc
                    if exc.is_timeout:
                        # Not retried: it would time out again. Mark the files so
                        # the next push splits the chunk (or gives up on one file).
                        chunk_outcome = "timeout"
                        err = _timeout_failure(
                            exc, common_dir, fp, chunk_items, _CHUNK_TIMEOUT,
                            f"chunk {k + 1}/{total_now}")
                        if err is exc:
                            raise
                        raise err from exc
                    if attempt > 0:
                        raise
                    # Non-timeout error: retry once.
                    continue
            if last_exc is not None:
                raise last_exc
            chunk_outcome = "ok"
        finally:
            # Always clean up the manifest and the chunk's diff files (success or failure).
            try:
                Path(manifest_path).unlink(missing_ok=True)
            except Exception:
                pass
            if diffs is not None:
                _remove_run_dir(common_dir, run_id, sub=k)
            chunk_secs = time.monotonic() - chunk_t0
            try:
                _tele_chunk(k, total_now, len(chunk_items),
                            sum(int(it["entry"].get("lines") or 0) for it in chunk_items),
                            chunk_outcome, chunk_secs)
            except Exception:
                pass
        return result, chunk_secs

    def _persist(k, chunk_items, manifest, result, chunk_secs, running):
        """Record a finished chunk (main thread: it owns the state file and the ledger)."""
        progress_n["secs"] += chunk_secs
        # Fence-check before persisting: if another run claimed the state while
        # the reviewer was running, do not write records.
        _update_state_owned(
            state_path, run_id,
            chunks_done=progress_n["done"] + 1, chunk_index=k, chunks_total=len(chunks),
            chunks_running=running,
            chunk_avg_s=round(progress_n["secs"] / (progress_n["new"] + 1), 1),
        )
        # Write ledger records for this chunk (only reached if not fenced).
        if seg is not None:
            result = _seg_check_impact_verdicts(result, manifest)
        if fp and _ledger_enabled():
            _write_run_records(result, [it for it in chunk_items if it.get("seg_file") is None],
                               common_dir, fp, run_id, review_root, tip,
                               precomputed=_owned_truncation(diffs))
        if seg is not None:
            try:
                seg.apply([it for it in chunk_items if it.get("seg_file") is not None], result)
            except Exception as exc:    # the chunk's findings stay; nothing is recorded for its units
                _metric_log("seg_apply_error", kind=type(exc).__name__[:30])
                _debug_log(f"seg apply failed: {type(exc).__name__}: {exc}")
        results_by_k[k] = result
        progress_n["done"] += 1
        progress_n["new"] += 1
        if progress is not None:
            progress["new"] = progress_n["new"]

    # Failure policy (Part C): a fence or a usage limit stops everything now; any
    # other failure lets the chunks in flight finish and record, then raises. When
    # several fail, the strongest wins: fence > limit > gate error > budget.
    failure = {"rank": 0, "exc": None}

    def _fail(rank, exc):
        if rank > failure["rank"]:
            failure.update(rank=rank, exc=exc)

    pool = _futures.ThreadPoolExecutor(max_workers=conc) if conc > 1 else _InlineExecutor()
    inflight = {}      # future -> (k, chunk_items, manifest, slot)
    free_slots = list(reversed(slots))
    next_k, retried = 0, False
    try:
        while True:
            # Dispatch while there is a free slot, work left and nothing has failed.
            while free_slots and failure["exc"] is None and next_k < len(chunks):
                if fenced["hit"]:
                    raise _Fenced()
                k, chunk_items = next_k, chunks[next_k]
                total = len(chunks)
                # Progress update (fence-checked).
                _update_state_owned(
                    state_path, run_id,
                    chunk_index=k, chunks_total=total, chunks_done=progress_n["done"],
                    chunks_running=len(inflight) + 1,
                )
                # Check run budget before starting a new chunk.
                if time.monotonic() > budget_end:
                    _update_state_owned(
                        state_path, run_id,
                        chunks_done=progress_n["done"], chunks_total=total,
                        chunks_running=len(inflight),
                    )
                    _fail(1, ReviewBudgetError(
                        f"run budget ({_RUN_BUDGET}s) exhausted after "
                        f"{progress_n['done']}/{total} chunks; re-push to resume"))
                    break
                # Write manifest for this chunk atomically; fail closed on error.
                manifest, manifest_path = _prepare(k, chunk_items)
                slot = free_slots.pop()
                fut = pool.submit(_execute, slot, k, total, chunk_items, manifest_path)
                inflight[fut] = (k, chunk_items, manifest, slot)
                next_k += 1

            if not inflight:
                if failure["exc"] is not None:
                    raise failure["exc"]
                if next_k < len(chunks):
                    continue
                if seg is not None and not retried:
                    retried = True              # caller checks that were unsure: once more
                    more = seg.retry_chunks()
                    if more:
                        chunks.extend(more)
                        continue
                break

            done, _ = _futures.wait(list(inflight), return_when=_futures.FIRST_COMPLETED)
            for fut in sorted(done, key=lambda f: inflight[f][0]):
                k, chunk_items, manifest, slot = inflight.pop(fut)
                free_slots.append(slot)
                try:
                    result, chunk_secs = fut.result()
                except _Fenced:
                    raise
                except _ChunkStopped:
                    continue                    # a sibling's failure is the one reported
                except ReviewLimitError as exc:
                    _fail(3, exc)
                    stop_evt.set()
                    _kill_active_children()
                    continue
                except ReviewGateError as exc:
                    _fail(2, exc)
                    continue
                except Exception as exc:        # noqa: BLE001 -- a worker's crash is a gate error
                    _fail(2, exc)
                    continue
                _persist(k, chunk_items, manifest, result, chunk_secs, len(inflight))
    except BaseException:
        # Whatever leaves (a fence above all): no child outlives the run.
        stop_evt.set()
        _kill_active_children()
        raise
    finally:
        pool.shutdown(wait=True)
        for wt in extra_worktrees:
            _remove_worktree(review_root, wt)

    merged = _merge_chunk_results([results_by_k[k] for k in sorted(results_by_k)],
                                  planner_warnings)
    # The per-chunk snapshots each hold one chunk; the run's record and the
    # stable last-output path must show all of them.
    raw_name = _save_raw_output(
        git_dir, json.dumps(merged, ensure_ascii=False, indent=2), tip, "-merged"
    )
    return merged, True, raw_name, progress_n["new"]


class _ChunkStopped(Exception):
    """A chunk's reviewer was stopped because a sibling chunk failed (Part C)."""


class _InlineExecutor:
    """Concurrency 1: run each chunk on the calling thread, at submit time, so the
    review is exactly the sequential one. Returns completed futures."""

    def submit(self, fn, *args):
        fut = _futures.Future()
        try:
            fut.set_result(fn(*args))
        except BaseException as exc:    # noqa: BLE001 -- handed back through the future
            fut.set_exception(exc)
        return fut

    def shutdown(self, wait=True):
        pass


def _make_slot_worktrees(review_root, tip, run_id, n):
    """Up to n more worktrees at `tip` for parallel chunks (Part C), named after the
    run's own with an `-s<i>` suffix. Only beside a gate-made worktree: a review
    reading the live tree runs one chunk at a time, and a slot is never the live
    tree. A slot that cannot be made just means fewer slots."""
    base = os.path.realpath(str(_gate_data_dir() / "worktrees"))
    if not os.path.realpath(str(review_root)).startswith(base + os.sep):
        return []
    made = []
    for i in range(1, n + 1):
        wt = _make_worktree(review_root, tip, f"{run_id}-s{i}")
        if not wt:
            _metric_log("slot_worktree_failed", slot=i)
            break
        made.append(wt)
    return made


def _mode_supervise(argv):
    try:
        i = argv.index("--state")
        state_path = argv[i + 1]
        j = argv.index("--run-id")
        run_id = argv[j + 1]
    except (ValueError, IndexError):
        return 2
    return _supervise(state_path, run_id)


def _chunk_progress(st):
    """", chunk 3/10" for one chunk at a time; ", chunks 2/10 done, 2 running" when
    several run at once (Part C)."""
    cd, ct = int(st.get("chunks_done") or 0), int(st.get("chunks_total") or 0)
    try:
        running = int(st.get("chunks_running") or 0)
    except (TypeError, ValueError):
        running = 0
    if running > 1:
        return f", chunks {cd}/{ct} done, {running} running"
    return f", chunk {cd + 1}/{ct}"


def _still_running_reason(st, budget, mode):
    tip = _sanitize(str(st.get("tip") or ""), 40)[:7]
    branch = _sanitize(str(st.get("branch") or "?"), 80)
    started = float(st.get("started_ts") or st.get("claimed_ts") or time.time())
    elapsed = max(0, int((time.time() - started) // 60))
    stamp = time.strftime("%H:%MZ", time.gmtime(started))
    n = st.get("commit_count")
    count = f", {n} commit(s)" if n else ""
    # Show chunk progress when the chunked reviewer is running.
    cd, ct = st.get("chunks_done"), st.get("chunks_total")
    if cd is not None and ct:
        count += _chunk_progress(st)
        avg = st.get("chunk_avg_s")
        if isinstance(avg, (int, float)) and avg > 0:
            count += f", avg {int(avg) // 60}m{int(avg) % 60:02d}s per chunk"
    retry = "re-run this exact `git push` command" if mode == "hook" else "run the push again"
    return (
        f"review-gate: the review of {branch} ({tip}{count}) is still running "
        f"(started {stamp}, {elapsed} min ago). The push was NOT executed.\n"
        f"To get the verdict, {retry}: the gate waits up to {int(budget // 60)} more minutes "
        "and answers as soon as the review finishes. Do not commit, amend or rebase this "
        "branch meanwhile - a new tip discards the review. Unrelated work is fine; the "
        "result is also reported after your next executed Bash call once it is ready."
    )


def _failed_reason(st, mode):
    why = _sanitize(str(st.get("reason") or "error"), 40)
    detail = str(st.get("detail") or "")
    attempts = int(st.get("attempts") or 0)
    if why == "limit":
        # Usage-limit failure: show chunk progress and reset time.
        cd = int(st.get("chunks_done") or 0)
        ct = int(st.get("chunks_total") or 0)
        progress = f" — {cd}/{ct} chunks saved" if ct else ""
        resets_at = st.get("resets_at")
        if resets_at:
            try:
                import datetime
                when = datetime.datetime.fromtimestamp(float(resets_at)).strftime("%H:%M")
                after = f"after {when}"
            except Exception:
                after = "after ~15 min"
        else:
            after = "after ~15 min"
        return (
            f"review-gate: usage limit{progress}; re-push {after}; "
            f"OCR_FORCE_REVIEW=1 retries now\n{_bypass_hint(mode)}"
        )
    if why == "unreviewable":
        return (
            "review-gate: the review cannot complete - blocking to preserve gate integrity.\n"
            + "\n".join("  " + line for line in detail.splitlines()[:12]) + "\n"
            + "  Not retried automatically; OCR_FORCE_REVIEW=1 (in the environment Claude Code "
            "was launched from) tries once more.\n" + _bypass_hint(mode)
        )
    if why == "timeout":
        cd = int(st.get("chunks_done") or 0)
        ct = int(st.get("chunks_total") or 0)
        progress = f" ({cd}/{ct} chunks saved)" if ct else ""
        return (
            f"review-gate: a review chunk timed out{progress}; the files it held are retried "
            "in smaller chunks - run the push again.\n"
            + "\n".join("  " + line for line in detail.splitlines()[:12]) + "\n"
            + _bypass_hint(mode)
        )
    head = f"review-gate: the review could not complete ({why}) - blocking to preserve gate integrity.\n"
    if attempts >= ATTEMPT_CAP:
        head += (
            f"  This tip failed {attempts} times; it will not be retried automatically for "
            f"{MARKER_TTL // 60} min. OCR_FORCE_REVIEW=1 (in the environment Claude Code was "
            "launched from) retries now.\n"
        )
    body = "\n".join("  " + line for line in detail.splitlines()[:12]) if detail else ""
    return head + body + ("\n" if body else "") + _bypass_hint(mode)


def _replay_note(st):
    """What a retry says when the verdict was recorded earlier."""
    age = max(0, int((time.time() - float(st.get("done_ts") or time.time())) // 60))
    verdict = _sanitize(str(st.get("verdict") or "?"), 20)
    lines = ["  " + _sanitize(line, 600) for line in str(st.get("reasons") or "").splitlines()
             if line.strip()]
    head = f"review recorded {age} min ago for these exact commits (verdict: {verdict})"
    if not lines:
        return head
    return head + " - findings from that run:\n" + "\n".join(lines)


def _format_reasons(result, limit=20):
    lines = []
    for f in result.get("findings", []) if isinstance(result, dict) else []:
        # Every field here came out of the diff under review, so all of it is
        # sanitized before it reaches the caller's context (see _sanitize).
        sev = _sanitize(f.get("severity", "?"), 20)
        path = _sanitize(f.get("path", "?"), 200)
        s, e = _sanitize(f.get("start_line", "?"), 12), _sanitize(f.get("end_line", "?"), 12)
        loc = f"{path}:{s}" if s == e else f"{path}:{s}-{e}"
        # A finding can be syntactically valid JSON yet still miss the fields
        # it needs to be actionable (the reviewer skipped them, usually under
        # output-length pressure). Say so explicitly instead of printing a
        # bare "- " that looks like display truncation rather than a defect
        # in the review itself.
        content = _sanitize(f.get("content") or "").strip() or (
            "(reviewer omitted a description for this finding - see raw output log)"
        )
        prov = f.get("provenance", "")
        if prov == "carried":
            prefix = "(carried) "
        elif prov == "still_present":
            prefix = "(still present) "
        elif prov == "impact":
            prefix = "(caller of changed code) "
        elif prov == "sibling":
            prefix = "(same defect as an earlier finding) "
        elif prov == "sibling_note":
            prefix = "(note) "
        elif prov == "unverified":
            prefix = ("(unverified: the resolver gave no evidence either way and the "
                      "flagged code is no longer at the tip - check by hand) ")
        else:
            prefix = ""
        lines.append(f"  [{sev}] {prefix}{loc} - {content}")
    # limit=0 means "all of them" -- used by --history, which is read on demand
    # and has no context budget to protect, unlike the gate's own messages.
    out = "\n".join(lines if not limit else lines[:limit])
    paths = result.get("unreviewed_truncated") if isinstance(result, dict) else None
    if isinstance(paths, list) and paths:
        # Cached truncated reviews (see _surface_truncated): said on every verdict, because
        # a pass that carries them is otherwise indistinguishable from a full review.
        n = len(paths)
        names = ", ".join(_sanitize(p, 120) for p in paths[:5]) + (" ..." if n > 5 else "")
        out += ("\n" if out else "") + (
            f"  {n} file{'s' if n != 1 else ''} effectively unreviewed (truncated): the "
            f"reviewer saw only part of the diff - {names}")
    return out


def _output_hints(git_dir, record=None):
    """Where to look afterwards: this run's raw output, and the kept log."""
    if not git_dir:
        return ""
    hint = f"\n  Full reviewer output: {_raw_output_path(git_dir)}"
    if record:
        hint += (
            f"\n  Findings log (kept, append-only): {record}"
            f"\n  Replay past findings: python \"{os.path.abspath(__file__)}\" --history"
        )
    return hint


def _print_telemetry_report(argv):
    """`review-gate.py --telemetry-report [--days N]` - summarise the local run
    log of this repository (see scripts/ocr_telemetry.py). Returns an exit code."""
    days = 7
    if "--days" in argv:
        i = argv.index("--days")
        try:
            days = max(1, int(argv[i + 1]))
        except (IndexError, ValueError):
            pass
    common = _git_common_dir(_repo_root())
    if not common:
        _warn("not inside a git repository - no run log here.")
        return 1
    sys.stdout.write(f"{ocr_telemetry.telemetry_dir(common)} (last {days} day(s))\n\n")
    sys.stdout.write(ocr_telemetry.report(ocr_telemetry.load(common, days)) + "\n")
    return 0


def _print_history(argv):
    """`review-gate.py --history [N]` - replay recorded reviews. Returns an exit code.

    Without this there is no command that shows a passing review's findings
    again: they are printed once, to a stderr stream that scrolls past with the
    push output, and nothing else surfaces them.
    """
    limit = 10
    i = argv.index("--history")
    if i + 1 < len(argv):
        try:
            limit = max(0, int(argv[i + 1]))
        except ValueError:
            pass  # not a count -- keep the default
    repo_root = _repo_root()
    git_dir = _git_dir(repo_root)
    if not git_dir:
        _warn("not inside a git repository - no review history here.")
        return 1
    entries = _read_history(git_dir, limit)
    if not entries:
        _warn(f"no reviews recorded yet ({_findings_log_path(git_dir)}).")
        return 0
    out = sys.stdout
    out.write(f"{_findings_log_path(git_dir)}\n\n")
    for e in entries:
        flags = [name for name, on in (
            ("BLOCKED", e.get("blocked")),
            ("advisory", e.get("advisory")),
            ("truncated", e.get("truncated")),
            (f"{e.get('unreviewed_truncated')} unreviewed (truncated)", e.get("unreviewed_truncated")),
        ) if on]
        out.write(
            "{at}  {head}  {verdict}  {n} finding(s)  branch={branch}{flags}\n".format(
                at=_sanitize(e.get("at", "?"), 32),
                head=_sanitize(e.get("head", "?"), 40)[:12] or "?",
                verdict=_sanitize(e.get("verdict", "?"), 16),
                n=e.get("finding_count", 0),
                branch=_sanitize(e.get("branch") or "-", 80),
                flags=f"  [{', '.join(flags)}]" if flags else "",
            )
        )
        body = _format_reasons(e, limit=0)
        if body:
            out.write(body + "\n")
        if e.get("raw"):
            out.write(f"  raw: {_sanitize(e['raw'], 200)}\n")
        out.write("\n")
    return 0


def _post_label(entry):
    n = entry.get("finding_count") or 0
    verdict = _sanitize(entry.get("verdict") or "?", 16)
    if verdict == "block" and entry.get("advisory"):
        # The loudest case in the whole mode: a block-level finding that let the
        # push through because blocking is off. Nothing else stops it, so the
        # wording has to carry the weight the exit code no longer does.
        return f"BLOCK-level findings, NOT enforced (advisory mode) - {n} finding(s)"
    return f"verdict: {verdict} - {n} finding(s)"


def _post_context(entry, git_dir, shadow=False):
    """additionalContext body for one recorded review; "" means stay silent."""
    verdict = str(entry.get("verdict") or "")
    count = entry.get("finding_count") or 0
    # A clean pass used to say nothing at all, on the theory that "the gate ran"
    # was already observable from the PreToolUse status line. It is not, to the
    # only reader that matters here: silence is indistinguishable from "no
    # review happened", so the model went and checked the log anyway -- the
    # exact chore this mode exists to remove. One line ends it. No raw-output
    # or replay pointers: a clean pass has nothing to go and read.
    unreviewed = int(entry.get("unreviewed_truncated") or 0)
    if verdict == "pass" and not count and not unreviewed:
        return "review-gate: pass - no findings." + ("\n" + _SHADOW_NOTE if shadow else "")
    lines = ["review-gate: " + _post_label(entry)]
    body = _format_reasons(entry, limit=POST_FINDING_LIMIT)
    if body:
        lines.append(body)
    shown = len(entry.get("findings") or [])
    if count > shown:
        # _record_review sheds findings to fit _MAX_LOG_LINE, all the way to
        # zero. Say so, rather than rendering an empty block that reads like
        # the review found nothing worth describing.
        lines.append(f"  ({count - shown} more finding(s) not recorded in the log line)")
    if unreviewed:
        lines.append(f"  {unreviewed} file(s) effectively unreviewed (truncated): the reviewer "
                     "saw only part of their diffs")
    raw = entry.get("raw")
    if raw and git_dir:
        lines.append(f"  Raw reviewer output: {Path(git_dir) / _sanitize(str(raw), 200)}")
    lines.append(f'  Replay: python "{os.path.abspath(__file__)}" --history 1')
    if shadow:
        lines.append(_SHADOW_NOTE)
    return "\n".join(lines)[:POST_MAX_CONTEXT]


def _gate_data_dir():
    """Plugin-local scratch beside the gate-dir pointer: the one location that
    survives plugin upgrades. Holds breadcrumbs and parked reports."""
    data = os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
    if not data:
        cfg = os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or os.path.join(
            os.path.expanduser("~"), ".claude"
        )
        data = os.path.join(cfg, "plugins", "data", "review-gate-local")
    return Path(data)


def _unlink(path):
    """Best-effort delete. Already gone is the same as deleted."""
    try:
        Path(path).unlink()
    except OSError:
        pass


def _park_pending(session_id, repo_root, head, kind="review", extra=None):
    """Note that a review is recorded and has not been reported yet.

    Written for every verdict the gate lets through, cleared the moment it is
    delivered. See PENDING_PREFIX for why this lives outside .git.

    kind="async" is the other note: a push was DENIED because its review was
    still running when the inline budget ran out. --mode post watches that
    note's state file and announces the verdict on a later tool call, so
    "do other work meanwhile" is actionable rather than a guess.
    """
    try:
        data = _gate_data_dir()
        data.mkdir(parents=True, exist_ok=True)
        name = PENDING_PREFIX + _marker_digest(session_id, repo_root, head, kind)
        body = {"session": session_id or "", "repo": repo_root or "", "head": head or "",
                "kind": kind}
        if extra:
            body.update(extra)
        (data / name).write_text(json.dumps(body), encoding="utf-8")
    except Exception:
        pass  # a lost note costs a report, never a push


def _pending_entries():
    """Every parked report, newest first, sweeping the ones nobody will claim.

    Same TTL discipline as _reap_markers, and self-limiting for the same
    reason: the sweep rides on the next read rather than needing a cleanup
    entry point somebody has to remember to run.
    """
    entries = []
    try:
        paths = sorted(
            _gate_data_dir().glob(f"{PENDING_PREFIX}*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return entries
    cutoff = time.time() - MARKER_TTL
    for path in paths:
        try:
            if path.stat().st_mtime < cutoff:
                _unlink(path)  # older than any push it could still describe
                continue
            info = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            _unlink(path)  # unreadable or half-written: it will never be useful
            continue
        if isinstance(info, dict):
            entries.append((path, info))
    return entries


def _pending_is_ours(info, session_id):
    """Whose parked report is this?

    One written by a Claude session belongs to that session and nobody else:
    flushing it elsewhere would put one repository's findings in front of an
    agent working in another, which is the same confident-and-wrong report the
    freshness check in _deliver already exists to prevent. One written by the
    git adapter has no session at all -- it came from a plain terminal push --
    so it goes to whichever session is actually sitting in that repository.
    """
    owner = str(info.get("session") or "")
    if owner:
        return owner == session_id
    repo, here = info.get("repo") or "", _repo_root()
    if not repo or not here:
        return False
    try:
        return os.path.realpath(repo) == os.path.realpath(here)
    except OSError:
        return False


def _clear_pending(repo_root, session_id):
    """Drop parked notes for repo_root that THIS session was entitled to flush.

    Ownership is checked, not just the repo: two sessions can be pushing the
    same repository, and clearing the other one's note would leave it with a
    review nothing will ever report -- reintroducing the exact hole the note
    was added to close.
    """
    for path, info in _pending_entries():
        if (info.get("repo") or "") != (repo_root or ""):
            continue
        owner = str(info.get("session") or "")
        if info.get("kind") == "async":
            st = _read_state(str(info.get("state") or "")) or {}
            if st.get("state") in ("running", "claimed"):
                continue  # still worth announcing later
        # A sessionless note is one this session could have flushed itself, and
        # the review it points at has just been delivered here.
        if not owner or owner == session_id:
            _unlink(path)


def _breadcrumb_path(session_id):
    """Where the gate records which repo it just reviewed, for --mode post.

    Delivery used to re-derive the pushed repo by parsing the command all over
    again -- the same fragile work, duplicated, with its own failure modes. The
    gate has already resolved it (it had to, in order to review the right
    thing), so it simply writes it down and the reporter reads it.

    Keyed by session so two concurrent sessions cannot read each other's.
    """
    return _gate_data_dir() / ("pushed-repo-" + _marker_digest(session_id or "nosession"))


def _drop_breadcrumb(session_id, repo_root):
    """Best-effort: a missing breadcrumb only costs a fallback, never a crash."""
    try:
        p = _breadcrumb_path(session_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(repo_root or "", encoding="utf-8")
    except Exception:
        pass


def _read_breadcrumb(session_id):
    try:
        p = _breadcrumb_path(session_id)
        if time.time() - p.stat().st_mtime > MARKER_TTL:
            return ""  # stale: from some earlier push, not this one
        val = p.read_text(encoding="utf-8").strip()
        return val if val and os.path.isdir(val) else ""
    except Exception:
        return ""


def _emit_post_context(text):
    """The one place a PostToolUse payload is written to stdout."""
    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": text,
                }
            }
        )
    )


def _deliver(repo_root, session_id, head=""):
    """Report body for the review recorded at `head` (default: repo_root's HEAD).

    Shared by both delivery paths -- the push that just ran, and a later flush
    of one that never got reported -- so the two cannot drift apart. `head` is
    the pushed TIP when the note carries one: since 0.6.0 a push may send a
    branch other than the checked-out one.
    """
    git_dir = _git_dir(repo_root)
    head = head or _head_sha(repo_root)
    if not git_dir or not head:
        return ""  # not a repo, or a detached/unborn HEAD -- nothing to replay

    entry = _latest_record_for_head(git_dir, head)
    if not entry:
        return ""  # no review recorded for these commits

    # The record must describe THIS push, not merely this HEAD. Resolving the
    # repo from a shell command is best-effort (see _gate_repo), so
    # when it guesses wrong it tends to land on whatever repository the session
    # happens to sit in -- whose HEAD has a record too, often months old. That
    # is exactly how a live test reported a different repo's stale findings as
    # though they were this push's. A real push's review is seconds old, so
    # requiring freshness turns "we guessed wrong" into silence rather than
    # into a confident, wrong report. Same window the gate already treats a
    # review as still describing the current push.
    try:
        if time.time() - float(entry.get("ts") or 0) > MARKER_TTL:
            return ""
    except (TypeError, ValueError):
        return ""

    # Claim BEFORE emitting, not after: both adapters can run against one push,
    # and check-then-act would let both report.
    key = _marker_digest(head, entry.get("ts") or entry.get("at") or "", session_id)
    delivered = Path(git_dir) / f"{POST_DELIVERED_PREFIX}{key}"
    if not _claim_marker(delivered):
        return ""  # this exact review is already in this session's context

    # Once per session, not once per push: the condition is a static property of
    # the repo, and repeating it every time trains the reader to skip it.
    shadow = False
    if _hookspath_shadowed(repo_root):
        warned = Path(git_dir) / f"{HOOKSPATH_WARNED_PREFIX}{_marker_digest(session_id or head)}"
        shadow = _claim_marker(warned)

    text = _post_context(entry, git_dir, shadow)
    if text:
        _reap_markers(git_dir, keep=delivered)
    return text


def _flush_pending(session_id):
    """Deliver reviews that were recorded and never reported.

    This is what makes reporting survive a push that failed. PostToolUse fires
    only for a tool call that actually ran and SUCCEEDED (verified: 760 failed
    Bash calls produced 0 PostToolUse hooks), so hanging delivery off the push
    itself meant a rejected push -- or a `git push && gh pr create` whose second
    half blew up -- reviewed the commits, wrote the findings to FINDINGS_LOG,
    and told nobody. The gate parks a note at review time instead, and any
    later tool call cashes it in.
    """
    out = []
    for path, info in _pending_entries():
        if not _pending_is_ours(info, session_id):
            continue
        repo_root = info.get("repo") or ""
        if info.get("kind") == "async":
            text = _async_note(path, info, session_id)
            if text:
                out.append(text)
            continue
        if repo_root and os.path.isdir(repo_root):
            text = _deliver(repo_root, session_id, str(info.get("head") or ""))
            if text:
                # Say WHICH repository, always. A deferred report arrives
                # detached from the push that earned it, and one session
                # routinely spans several repos in a conversation -- the gate
                # exists because Claude pushes as `cd <repo> && git push`. So
                # the reader cannot assume this describes wherever they
                # currently are, and the body never says. Naming it is the fix;
                # withholding the report unless the repo still matches would
                # re-open the hole this whole path was added to close.
                out.append(
                    f"review-gate: deferred report for {_sanitize(repo_root, 200)} - "
                    "the push that triggered this review never reported it.\n" + text
                )
        # Cleared either way. A note we looked at and had nothing to say about
        # is spent: leaving it would re-ask the same question on every
        # subsequent tool call for the rest of the TTL.
        _unlink(path)
    if out:
        _emit_post_context("\n\n".join(out)[:POST_MAX_CONTEXT])
    return 0


def _async_note(path, info, session_id):
    """Announce a review that was still running when its push was denied.

    done/failed -> report once and drop the note. Still running -> a short
    reminder at most every five minutes, and the note stays. A note whose
    state file has vanished is spent.
    """
    state_path = str(info.get("state") or "")
    repo_root = str(info.get("repo") or "")
    tip = str(info.get("head") or "")
    label = f"{_sanitize(repo_root, 200)} {_sanitize(tip, 40)[:7]}"
    st = _read_state(state_path) if state_path else {}
    if st is None:
        return ""  # mid-write; next call
    s = st.get("state")
    if not st or s not in ("running", "claimed", "done", "failed"):
        _unlink(path)
        return ""
    if s == "done":
        _unlink(path)
        body = ""
        if repo_root and os.path.isdir(repo_root):
            body = _deliver(repo_root, session_id, tip)
        verdict = _sanitize(str(st.get("verdict") or "?"), 20)
        head = (
            f"review-gate: the review of {label} that was still running when the push was "
            f"denied has finished (verdict: {verdict}"
            + (", BLOCKED" if st.get("blocked") else "") + "). "
            + ("Fix the findings, then push again." if st.get("blocked")
               else "Re-run the same `git push` to have it go through.")
        )
        return head + ("\n" + body if body else "")
    if s == "failed":
        _unlink(path)
        return (
            f"review-gate: the review of {label} that was still running when the push was "
            f"denied could not complete ({_sanitize(str(st.get('reason') or 'error'), 40)}). "
            "Re-run the `git push` to see the reason and retry."
        )
    try:
        last = float(info.get("notified_ts") or 0)
    except (TypeError, ValueError):
        last = 0
    if time.time() - last < 300:
        return ""
    try:
        info["notified_ts"] = time.time()
        Path(path).write_text(json.dumps(info), encoding="utf-8")
    except Exception:
        pass
    started = float(st.get("started_ts") or st.get("claimed_ts") or time.time())
    mins = max(0, int((time.time() - started) // 60))
    return (
        f"review-gate: the review of {label} is still running ({mins} min). Re-run the same "
        "`git push` when you want to wait for its verdict."
    )


def _mode_resume(argv):
    """`--mode resume`: SessionStart context about a review this session left.

    A host that kills the CLI mid-hook leaves the next process a dangling tool
    call, which Claude Code reports as "[Request interrupted by user]". If a
    review of this session's last pushed repo is running or finished, say so,
    so the model does not narrate a user interruption that never happened.
    Silent when there is nothing to say.
    """
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    session_id = str(payload.get("session_id") or "") if isinstance(payload, dict) else ""
    repo_root = _read_breadcrumb(session_id) if session_id else ""
    if not repo_root:
        return 0
    common = _git_common_dir(repo_root)
    if not common:
        return 0
    lines = []
    cutoff = time.time() - MARKER_TTL
    for p in sorted(_async_dir(common).glob("*.json")):
        st = _read_state(p) or {}
        s = st.get("state")
        ts = float(st.get("done_ts") or st.get("failed_ts") or st.get("heartbeat_ts")
                   or st.get("claimed_ts") or 0)
        if s not in ("running", "claimed", "done", "failed") or ts < cutoff:
            continue
        tip = _sanitize(str(st.get("tip") or ""), 40)[:7]
        branch = _sanitize(str(st.get("branch") or "?"), 80)
        if s in ("running", "claimed"):
            alive = time.time() - float(st.get("heartbeat_ts") or st.get("claimed_ts") or 0) < STALE_S
            cd, ct = st.get("chunks_done"), st.get("chunks_total")
            chunk_note = _chunk_progress(st) if cd is not None and ct else ""
            what = ("is still running" + chunk_note) if alive else "was interrupted"
        elif s == "done":
            what = "finished: " + ("BLOCKED" if st.get("blocked") else
                                   _sanitize(str(st.get("verdict") or "?"), 20))
        else:
            why = _sanitize(str(st.get("reason") or "error"), 40)
            cd, ct = st.get("chunks_done"), st.get("chunks_total")
            chunk_note = f", {cd}/{ct} chunks saved" if cd is not None and ct else ""
            what = f"failed ({why}{chunk_note})"
        lines.append(f"  - {branch} @ {tip}: {what}")
    if not lines:
        return 0
    text = (
        f"review-gate: a push review in {_sanitize(repo_root, 200)} was under way when this "
        "session's previous process ended:\n" + "\n".join(lines) + "\n"
        "If you did not intend to cancel it, re-run the same `git push`; the gate answers "
        "from the recorded verdict or keeps waiting on the running review. A dangling "
        "\"[Request interrupted by user]\" on that push is the host restarting the process, "
        "not necessarily a user action."
    )
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}
    }))
    return 0


def _mode_post(argv):
    """`--mode post`: put a recorded review in front of the model.

    This is the ONLY channel that does so for non-blocking findings. Verified
    2026-09 against Claude Code's own transcripts: a PostToolUse hook returning
    additionalContext produces a `hook_additional_context` record -- the
    delivery vehicle -- while a PreToolUse `permissionDecisionReason` on an
    ALLOW produces none. It is logged UI-side and goes nowhere else, which is
    why blocks (delivered as the tool_result of a deny) were never the problem
    and warns always were.

    Two ways in. A `git push` that succeeded reports its own review directly.
    Anything else flushes whatever earlier push never managed to -- see
    _flush_pending for why that second path has to exist.

    Best-effort and silent throughout. This stdout is parsed by Claude Code, so
    the only acceptable outputs are one JSON object or nothing -- never a
    traceback.
    """
    session_id, cmd = "", ""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        session_id = str(payload.get("session_id") or "")
        cmd = (payload.get("tool_input") or {}).get("command", "")

    # Same command-position test the PreToolUse adapter applies (0.6.0: a
    # `git -C <dir> push` is a push; a command that mentions one is not).
    if cmd and not _looks_like_real_push(cmd):
        return _flush_pending(session_id)

    # The repo the GATE resolved, not one re-derived here. Delivery does no
    # command parsing at all: the breadcrumb is written by the adapter that
    # already had to work this out in order to review the right thing.
    repo_root = _read_breadcrumb(session_id) or _repo_root()
    if not repo_root:
        return 0
    # The pushed tip is in this session's parked note for the repo; a push may
    # send a branch other than the checked-out one. No note: fall back to HEAD.
    heads = []
    for _p, info in _pending_entries():
        if info.get("kind", "review") != "review" or (info.get("repo") or "") != repo_root:
            continue
        if _pending_is_ours(info, session_id) and info.get("head"):
            heads.append(str(info["head"]))
    texts = []
    for h in heads or [""]:
        t = _deliver(repo_root, session_id, h)
        if t:
            texts.append(t)
    # Reported, or deliberately silent about -- either way this push's note has
    # served its purpose and must not be flushed again by the next tool call.
    _clear_pending(repo_root, session_id)
    if texts:
        _emit_post_context("\n\n".join(texts)[:POST_MAX_CONTEXT])
    return 0


# --- inside the review: what the reviewer's Bash may write ------------------
# The reviewer's allowlist is read-only in intent (`Bash(git diff *)`, `git
# log`, ...) but not in effect: git's diff/log/show accept `--output=<file>`,
# and `git log -1 --format='<any text>' --output=<path>` writes arbitrary
# content anywhere -- verified 2026-09-22, Claude Code's matcher lets it
# through and the file appears. A shell redirection into the working tree is
# admitted too (only paths OUTSIDE the tree are refused by the host). Since
# the reviewer's input is an untrusted diff, a prompt-injected reviewer could
# forge this gate's own markers and state files -- the one thing that turns
# "the review can be fooled" into "the review can be skipped". So the plugin's
# hook, which is registered inside the review session too, vets every Bash
# call there instead of blanket-allowing it.
# `--output` and every abbreviation git's option parser would accept for it
# (`--o`, `--ou`, ... are ambiguous with --output-indicator-* today, but that
# is git's business, not a property this guard should lean on).
_OUTPUT_OPT = re.compile(r"(?:^|\s)--o(?:u(?:t(?:p(?:u(?:t)?)?)?)?)?(?:=|\s|$)")
_REDIRECT = re.compile(
    r"(?<![<>&])(?:\d*>>?|&>>?|>\|)\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s;&|)]+))"
)
_FORBIDDEN_COMPONENTS = frozenset({"..", ".git", ".claude", ".ocr", ".config"})


def _guard_reviewer_command(cmd):
    """Reason to refuse a Bash command inside the review session, or "".

    Scratch files in the reviewer's own cwd (a detached worktree) are fine;
    writes anywhere else are not. Only file-writing shapes are judged here --
    the host's own allowlist already refuses non-git commands.
    """
    code = _strip_heredocs(cmd or "")
    # Judge TOKENS, not the raw text: `git log --grep="--output"` mentions the
    # option inside a quoted string and writes nothing. Fall back to the raw
    # match only when the command cannot be tokenised at all.
    try:
        toks = shlex.split(code, posix=True)
    except ValueError:
        toks = None
    if toks is None:
        hit = bool(_OUTPUT_OPT.search(code))
    else:
        hit = any(_OUTPUT_OPT.match(" " + t) for t in toks)
    if hit:
        return "review-gate: `--output` writes a file; the reviewer is read-only. Print to stdout instead."
    for m in _REDIRECT.finditer(code):
        target = next((g for g in m.groups() if g is not None), "")
        if not target or target.startswith("&"):
            continue  # `>&2`, `2>&1`
        norm = target.replace("\\", "/")
        if norm in ("/dev/null", "NUL", "nul"):
            continue
        if _UNEXPANDABLE.search(target):
            return f"review-gate: redirection target {target!r} is not a literal path; the reviewer may only write scratch files under its own directory."
        if norm.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", norm):
            return f"review-gate: redirection to {target!r} leaves the reviewer's directory; the reviewer is read-only outside it."
        parts = [c for c in norm.split("/") if c not in ("", ".")]
        if any(c.lower() in _FORBIDDEN_COMPONENTS for c in parts):
            return f"review-gate: redirection to {target!r} reaches a directory the reviewer must not write to."
    return ""


def _mode_guard(argv):
    """`--mode guard`: PreToolUse decision for a Bash call INSIDE the review."""
    try:
        payload = json.load(sys.stdin) or {}
    except Exception:
        payload = {}
    cmd = ""
    if isinstance(payload, dict) and (payload.get("tool_name") in (None, "", "Bash")):
        cmd = (payload.get("tool_input") or {}).get("command", "") or ""
    why = _guard_reviewer_command(cmd) if cmd else ""
    if why:
        _emit_hook("deny", why)
    _emit_hook("allow")


def _emit_hook(decision, reason=""):
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
    }
    if reason:
        out["hookSpecificOutput"]["permissionDecisionReason"] = reason
    sys.stdout.write(json.dumps(out))
    sys.exit(0)  # hook itself always exits 0; the decision is in the payload


def _fail_closed(mode, msg):
    """Block the commit (or deny the hook) with a clear message.

    Called for ReviewGateError AND for unhandled exceptions anywhere in main()
    so that any internal crash fails closed rather than open.
    """
    if mode == "hook":
        _emit_hook("deny", msg)  # exits 0; decision is in payload
    else:
        _warn(msg)
        sys.exit(1)


def main(argv):
    # Mode is parsed INSIDE the safety net so an IndexError from a malformed
    # '--mode' flag (e.g. '--mode' with no value) is also caught and fails
    # closed rather than crashing without emitting a deny payload.
    mode = "git"  # safe default for the except clause below

    # Read-only query over the persisted log. Handled before anything else so
    # it never spawns a review, touches a marker, or needs a hook payload.
    if "--history" in argv:
        sys.exit(_print_history(argv))
    if "--telemetry-report" in argv:
        sys.exit(_print_telemetry_report(argv))

    _mode_arg0 = ""
    if "--mode" in argv:
        _i0 = argv.index("--mode")
        _mode_arg0 = argv[_i0 + 1] if _i0 + 1 < len(argv) else ""
    # The detached worker. Dispatched before anything that reads stdin or
    # writes the gate pointer: it is not a hook and has no payload.
    if _mode_arg0 == "supervise":
        sys.exit(_mode_supervise(argv))
    # SessionStart context and the in-review Bash guard. Both are best-effort
    # reporters: a crash must go quiet (resume) or fail closed for that ONE
    # reviewer command (guard), never surface as a hook error.
    if _mode_arg0 == "resume":
        try:
            sys.exit(_mode_resume(argv))
        except SystemExit:
            raise
        except Exception:
            sys.exit(0)
    if _mode_arg0 == "guard":
        try:
            _mode_guard(argv)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            _emit_hook("deny", f"review-gate: guard error ({type(exc).__name__}) - refusing this command.")

    # --mode post REPORTS; it does not decide. Handled here, ahead of the
    # fail-closed safety net below, because that net answers an unhandled
    # exception with a deny payload -- which for a PostToolUse hook would be
    # both meaningless and noisy. A reporting path that cannot run must go
    # quiet, never block: the whole reason this mode exists is that findings
    # were being lost, and a crash here must not also cost a push.
    _mode_arg = ""
    if "--mode" in argv:
        _i = argv.index("--mode")
        _mode_arg = argv[_i + 1] if _i + 1 < len(argv) else ""
    if _mode_arg == "post":
        try:
            # Inside the headless review session this plugin is loaded via
            # --plugin-dir, so this hook is registered there too and would fire
            # on every Bash call the reviewer makes.
            sys.exit(0 if _in_review() else _mode_post(argv))
        except SystemExit:
            raise
        except Exception:
            sys.exit(0)

    # Top-level safety net: any unhandled exception in main() fails closed.
    # Without this, a crash in compute_verdict(), _format_reasons(), or any
    # other helper exits the process with a non-zero code WITHOUT emitting a
    # deny payload — in hook mode Claude Code would treat that as a non-blocking
    # error and let the commit through, defeating the fail-closed policy.
    try:
        if "--mode" in argv:
            mode = argv[argv.index("--mode") + 1]
        _main_inner(argv, mode)
    except SystemExit:
        raise  # propagate intentional exits (allow/deny both use sys.exit)
    except Exception as exc:  # noqa: BLE001
        _fail_closed(mode, f"review-gate internal error ({type(exc).__name__}: {exc}) - blocking commit.")


def _main_inner(argv, mode):
    # Re-entry guard. _run_review passes --plugin-dir to the child, so this
    # plugin's own push gate is registered inside the review session. Without
    # this, every Bash call the reviewer makes spawns a Python process, and a
    # push from inside a review would recurse into a second full review.
    # Checked before anything else, including stdin, so the cost is one env
    # lookup on the hot path.
    if _in_review():
        if mode == "hook":
            _mode_guard(argv)  # exits
        sys.exit(0)

    # Keep the global git hook's pointer current. Done on every run so an
    # upgrade self-heals on the next push through Claude Code, without the user
    # having to re-run install-git-hook.sh.
    _write_gate_pointer()

    # Hook mode: consume the PreToolUse payload on stdin (and only gate pushes).
    payload = {}
    if mode == "hook":
        try:
            payload = json.load(sys.stdin) or {}
            cmd = (payload.get("tool_input") or {}).get("command", "")
            # Command-position test, not a substring: `git -C <dir> push` is
            # a push (the adapters route it here since 0.6.0), and a command
            # that merely mentions one is not.
            if not _looks_like_real_push(cmd):
                _emit_hook("allow")
        except Exception:
            payload = {}  # if we can't read it, fall through and review anyway

    # WHICH repository is being pushed. In git mode the pre-push hook already
    # runs inside it, so the process cwd is right by construction. In hook mode
    # it is not: this process inherits the cwd Claude Code was launched from,
    # and Claude routinely pushes as `cd <repo> && git push`. Reading the
    # process cwd there gated the SESSION's repo instead -- which usually has
    # nothing unpushed, so the gate allowed a push it had never reviewed. That
    # is a silent fail-open, and it is total in any repo where the global git
    # hook is absent or shadowed by a repo-local core.hooksPath.
    cmd = ""
    if mode == "hook":
        cmd = (payload.get("tool_input") or {}).get("command", "") if isinstance(payload, dict) else ""
        if not _looks_like_real_push(cmd):
            _emit_hook("allow")  # mentions a push; does not perform one
        _resolved, _ambiguous = _gate_repo(payload)
        if _ambiguous:
            # There is a `cd` we cannot follow, so we do not know what these
            # commits are. Blocking is the same rule the rest of this file
            # applies to every other "cannot run" case: a gate that does not
            # know what it is looking at must not wave a push through.
            if _fail_open_requested():
                _emit_hook("allow")
            _fail_closed(
                mode,
                "review-gate: could not determine which repository this push targets, so it "
                "was not reviewed. Blocking, because a gate that cannot see the commits must "
                "not wave them through.\n\nThe push command changes directory to something "
                "this hook cannot resolve (an unexpanded shell variable, a command "
                "substitution, or a path that is not a git repository).\n\nFix it:\n"
                "  - Use a literal path: cd /full/path/to/repo && git push\n"
                "  - Or run the push from the directory Claude Code was started in.\n"
                "  - Emergency bypass: set OCR_FAIL_OPEN=1 in the environment Claude Code "
                "itself was launched from.\n"
                "  - Or push from a plain terminal, which this adapter does not gate.",
            )
        repo_root = _resolved or _repo_root()
        _drop_breadcrumb(str(payload.get("session_id") or "") if isinstance(payload, dict) else "",
                         repo_root)
        # Anything before the push that can move a ref -- `git switch x &&
        # git push`, `git commit && git push` -- would have the hook review one
        # tip and git send another. Only read-only git may precede a push.
        bad = _pre_push_git_commands(cmd)
        if bad and not _fail_open_requested():
            _fail_closed(
                mode,
                f"review-gate: `git {_sanitize(bad[0], 40)}` runs before the push in the same "
                "command, so the commits git would send are not the commits this hook can "
                "see. Run the push as its own command, after the others have completed.",
            )
    else:
        repo_root = _repo_root()

    # allow() takes an optional reason, and it is worth being precise about
    # where that reason ends up, because this comment used to claim the
    # opposite and a fix was built on the claim.
    #
    # VERIFIED 2026-09 against Claude Code's transcripts: permissionDecisionReason
    # on an ALLOW decision does NOT reach the model. It is recorded in the
    # hook's own `hook_success` entry -- visible UI-side, useful when debugging
    # -- and produces no `hook_additional_context` companion, which is the
    # record that actually delivers text into the session. Only the DENY path
    # reaches the model, as the tool_result of the refused call. That asymmetry
    # is the entire bug: blocks were always seen, non-blocking findings never
    # were.
    #
    # Model delivery for the non-blocking case is the PostToolUse hook
    # (--mode post). The reason string below is kept because it costs nothing
    # and is genuinely useful in the hook record; it is not a delivery channel.
    allow = (
        (lambda reason="": _emit_hook("allow", reason))
        if mode == "hook"
        else (lambda reason="": sys.exit(0))
    )

    # WHAT is being pushed. Git mode: the refs git feeds a pre-push hook on
    # stdin are authoritative. Hook mode runs BEFORE git, so the command line
    # is all there is -- and since 0.6.0 it is read rather than ignored.
    push_range, tip, branch, base = "", "", "", ""
    if mode == "git":
        refs = _read_push_refs()
        push_range = _range_for_refs(refs, repo_root)
        if push_range == _MULTI_REF:
            if _fail_open_requested():
                allow()
            _fail_closed(
                mode,
                "review-gate: this push updates more than one branch at once, and a review "
                "covers a single revision range. Reviewing one of them would leave the rest "
                "unreviewed, so it is refused instead.\n\nPush the branches separately, or "
                "set OCR_FAIL_OPEN=1 for a one-shot bypass.",
            )
        if not _has_unpushed_commits(repo_root, push_range):
            allow()
        if push_range and ".." in push_range:
            base, tip = push_range.split("..", 1)
            if not re.fullmatch(r"[0-9a-f]{40}", tip or ""):
                tip = _head_sha(repo_root)
        else:
            tip = _head_sha(repo_root)
        branch = _branch(repo_root)
    else:
        decision, info = _hook_target(repo_root, cmd)
        if decision == "allow":
            allow()
        if decision == "deny":
            if _fail_open_requested():
                allow()
            _fail_closed(mode, str(info.get("why") or "review-gate: refused."))
        tip, branch = info["tip"], info.get("branch") or ""
        base, push_range = info.get("base") or "", info.get("range") or ""
        if not push_range and not _has_unpushed_commits(repo_root, ""):
            allow()

    # Anchored on repo_root, which is now genuinely the repo being pushed
    # rather than whatever directory this process happens to sit in. It may
    # still be "" outside a repo, so every consumer below guards for that.
    git_dir = _git_dir(repo_root)
    if not tip or not git_dir:
        if _fail_open_requested():
            allow()
        _fail_closed(mode, "review-gate: could not resolve the commit to review - blocking.")
    common_dir = _git_common_dir(repo_root) or git_dir
    marker = _marker_path(git_dir, tip)

    # The legacy pass marker: the other adapter, or an older gate, already
    # reviewed these exact commits and passed them within the TTL. Replay what
    # it found instead of allowing silently.
    force = os.environ.get("OCR_FORCE_REVIEW", "").strip().lower() in ("1", "true", "yes")
    if _marker_fresh(marker) and not force:
        note = _prior_findings_note(_read_marker(marker))
        if note:
            _warn(note + _output_hints(git_dir, _findings_log_path(git_dir)))
        allow(note)

    session_id = str(payload.get("session_id") or "") if isinstance(payload, dict) else ""
    count = ""
    if push_range and base != _EMPTY_TREE:
        out, rc = _git(["rev-list", "--count", push_range], cwd=repo_root)
        count = out.strip() if rc == 0 else ""
    meta = {
        "tip": tip, "branch": branch, "base": base, "range": push_range,
        "repo_root": repo_root, "git_dir": git_dir, "commit_count": count,
    }
    budget = _inline_budget(mode)
    try:
        st = _drive_review(common_dir, repo_root, meta, mode, budget)
    except ReviewGateError as exc:
        if _fail_open_requested():
            allow()
        _fail_closed(mode, f"review-gate: {exc} - blocking commit to preserve gate integrity.")
        return  # unreachable

    if st is None:
        # Budget spent, review still running. Deny -- an allow would push
        # unreviewed commits -- and say exactly how to collect the verdict.
        # The parked note lets --mode post announce it when it lands.
        cur = _read_state(_state_path(common_dir, tip)) or meta
        _park_pending(session_id, repo_root, tip, kind="async",
                      extra={"state": str(_state_path(common_dir, tip))})
        if _fail_open_requested():
            allow()
        _fail_closed(mode, _still_running_reason(cur, budget, mode))
        return  # unreachable

    # This call delivers the verdict itself; a note parked by an earlier
    # budget deny for the same tip must not announce it a second time.
    _unlink(_gate_data_dir() / (PENDING_PREFIX + _marker_digest(session_id, repo_root, tip, "async")))

    if st.get("state") == "failed":
        if _fail_open_requested():
            _warn(
                "OCR_FAIL_OPEN=1 set - bypassing fail-closed gate. Reason:\n  "
                f"{_sanitize(str(st.get('detail') or st.get('reason') or ''), 400)}\n"
                "[!] This bypass should be used sparingly and intentionally."
            )
            allow()
        _fail_closed(mode, _failed_reason(st, mode))
        return  # unreachable

    # done
    if st.get("verdict") == "skipped":
        _warn(str(st.get("note") or "review skipped"))
        allow()  # fail-open: only reaches here when claude is not installed

    reasons = str(st.get("reasons") or "")
    hints = _output_hints(git_dir, st.get("record") or None)
    replayed = time.time() - float(st.get("done_ts") or time.time()) > 5
    if st.get("blocked") and not _is_advisory(repo_root):
        reason = "review-gate blocked this commit (high-severity issues):\n" + (
            reasons or "  (see review output)"
        ) + f"{hints}\n\nFix the issues above, then commit again.\n{_downgrade_hint(mode)}"
        if replayed:
            reason = "review-gate: " + _replay_note(st) + "\n" + reason
        _fail_closed(mode, reason)
        return  # unreachable

    # Passed (or advisory): park the report before letting the push run.
    # Delivery normally happens on the push's own PostToolUse hook and clears
    # this note in passing; the note is what covers the case where that hook
    # never fires because the push failed.
    _park_pending(session_id, repo_root, tip)
    note = ""
    if reasons:
        verdict = _sanitize(str(st.get("verdict") or "?"), 20)
        advisory = _is_advisory(repo_root)
        label = "advisory (blocking disabled)" if advisory else f"verdict: {verdict}"
        note = f"{label} - findings:\n{reasons}"
        if replayed:
            note = _replay_note(st) + "\n" + note
        _warn(note + hints)
    allow(note)


if __name__ == "__main__":
    main(sys.argv)
