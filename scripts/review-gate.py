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
try:
    _CHECKPOINT_TTL = int(os.environ.get("OCR_CHECKPOINT_TTL", str(24 * 3600)))
    _CHECKPOINT_TTL = max(3600, _CHECKPOINT_TTL)
except ValueError:
    _CHECKPOINT_TTL = 24 * 3600
# --- review ledger (0.8.0) ---------------------------------------------------
LEDGER_DIR = "review-gate-ledger"
try:
    _LEDGER_TTL = int(os.environ.get("OCR_LEDGER_TTL", str(7 * 24 * 3600)))
    _LEDGER_TTL = max(3600, _LEDGER_TTL)
except ValueError:
    _LEDGER_TTL = 7 * 24 * 3600
try:
    _LEDGER_MAX_RECORDS = int(os.environ.get("OCR_LEDGER_MAX_RECORDS", "5000"))
    _LEDGER_MAX_RECORDS = max(100, _LEDGER_MAX_RECORDS)
except ValueError:
    _LEDGER_MAX_RECORDS = 5000
_LEDGER_SCHEMA = 1
_CHAIN_DEPTH_MAX = 5   # max delta-chain length before forcing full
# Env vars that affect the review prompt and therefore invalidate cached records.
_FINGERPRINT_ENV_VARS = (
    "OCR_MODEL", "OCR_BLOCK_SEVERITY", "OCR_BLOCK_CONFIDENCE",
    "OCR_CLAUDE_ARGS", "OCR_CLAUDE_EXTRA_ARGS",
)
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
    def __init__(self, msg="", is_timeout=False):
        super().__init__(msg)
        self.is_timeout = is_timeout


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


def _extract_json(text):
    """Pull the review JSON object out of claude's stdout. Returns dict or None."""
    if not text:
        return None
    text = text.strip()
    # 1) whole thing
    try:
        return json.loads(text)
    except Exception:
        pass
    # 2) fenced ```json ... ``` block (last one)
    import re

    blocks = re.findall(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    for b in reversed(blocks):
        try:
            return json.loads(b)
        except Exception:
            continue
    # 3) balanced scan from each '{' in turn (json.JSONDecoder.raw_decode stops
    # at the object's own matching brace and ignores everything after it, unlike
    # a naive find("{")..rfind("}") span, which grabs the LAST '}' anywhere in
    # the text -- including one inside trailing prose the model appended after
    # the JSON despite being told to print only the object -- and turns a valid
    # verdict into an unparseable-output failure.
    #
    # Only a dict carrying "findings" (mandatory per the skill's --json contract)
    # is accepted as a candidate. Without that check the first '{' that happens
    # to decode would win even if it's an unrelated JSON value the model quoted
    # from the reviewed diff itself (e.g. a config fixture) before the real
    # verdict -- and take the LAST candidate, not the first, since that quoted
    # case necessarily precedes the model's actual answer.
    decoder = json.JSONDecoder()
    idx, match = text.find("{"), None
    while idx != -1:
        try:
            obj, end = decoder.raw_decode(text, idx)
            if isinstance(obj, dict) and "findings" in obj:
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


def _debug_log(line):
    """Append one line to the OCR_DEBUG forensic log. Best-effort and silent on
    failure -- a diagnostic aid must never be able to break the gate it exists
    to help debug. Lives beside _park_pending's data, outside .git, so it
    survives whatever state the repo itself is in."""
    try:
        data = _gate_data_dir()
        data.mkdir(parents=True, exist_ok=True)
        with (data / "review-gate-debug.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
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


def _run_review(repo_root, mode, git_dir=None, head_sha="", push_range="",
                paths_file=None, timeout=None, raw_tag=""):
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
    if not claude:
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
    if paths_file:
        base_prompt = PROMPT_RANGE.format(rng=push_range) if push_range else PROMPT
        # Strip exactly the trailing " --json" suffix (both PROMPT constants end
        # with it).  Do NOT use rstrip(" --json") — that strips a character SET.
        _SUFFIX = " --json"
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
        if paths_file:
            cmd = stub + ["--paths-file", paths_file, push_range]
        else:
            cmd = stub + [push_range]
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
            global _ACTIVE_CHILD
            _ACTIVE_CHILD = proc
            try:
                out_text, err_text = proc.communicate(timeout=_run_timeout)
            except subprocess.TimeoutExpired:
                _kill_child(proc)
                proc.communicate()
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
            if debug:
                _debug_log(
                    f"end child_pid={proc.pid} outcome=rc{proc.returncode} "
                    f"duration_s={time.monotonic()-started_mono:.1f}"
                )
            _ACTIVE_CHILD = None
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
    result = _extract_json(out_text)
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
        raise ReviewGateError(
            "could not parse review output - blocking commit to preserve gate integrity.\n"
            f"  Claude stdout (first 400 chars): {out_text[:400]!r}\n"
            f"{_auth_hint(out_text or '')}"
            f"{bypass}"
        )
    return result, True, raw_name


# --- the review runs elsewhere: state file, supervisor, inline join ----------
# The reviewer child currently being waited on by _run_review, so the
# supervisor's heartbeat thread can kill it on a fence break or deadline.
_ACTIVE_CHILD = None
# The genuine class, captured at import: tests substitute subprocess.Popen
# with stand-ins carrying made-up pids, and those must never reach taskkill.
_REAL_POPEN = subprocess.Popen


def _kill_child(proc):
    """Kill a reviewer and everything it spawned. Scoped to one known pid.

    Only a real Popen gets the tree kill: tests hand _run_review a stand-in
    with a made-up pid, and `taskkill /PID 4242 /T /F` on a developer's box
    would hit whatever process happens to own that number.
    """
    try:
        proc.kill()
    except Exception:
        pass
    if type(proc) is _REAL_POPEN:
        _tree_kill(proc.pid)


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
                stale_pids = (st2.get("reviewer_pid"), st2.get("supervisor_pid"))
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
            child = _ACTIVE_CHILD
            if child is not None:
                fields["reviewer_pid"] = child.pid
            try:
                _update_state_owned(state_path, run_id, **fields)
            except _Fenced:
                fenced["hit"] = True
                if child is not None:
                    _kill_child(child)
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

        fp = _compute_fingerprint(review_root, tip)
        _prune_ledger(common_dir)
        plan, planner_warnings = _plan_review(review_root, base, tip, common_dir, fp)

        if plan is None or plan == []:
            # git diff failed OR no allowed files: fall back to single-context (0.7.0 path).
            result, ran, raw_name = _run_review(review_root, mode, git_dir, tip, push_range)
            chunks_new = 1 if ran else 0
            if planner_warnings and isinstance(result, dict):
                result = dict(result)
                result["warnings"] = (_planner_warning_objs(planner_warnings)
                                      + list(result.get("warnings") or []))
        else:
            active_items = [p for p in plan if p["mode"] in ("delta", "full")]
            carry_items = [p for p in plan if p["mode"] == "carry"]
            carry_paths = [p["entry"]["path"] for p in carry_items]

            if not active_items:
                # All files already reviewed in a prior run: replay from ledger.
                ran = True  # got a valid result (from records)
                result = {"status": "replayed", "findings": [], "warnings": []}
                chunks_new = 0
                plan_summary = f"carried {len(carry_items)} file(s), 0 reviewed"
            elif len(active_items) > _CHUNK_THRESHOLD:
                # Multi-chunk path.
                result, ran, raw_name, chunks_new = _run_chunked(
                    state_path, run_id, common_dir, review_root, mode, git_dir,
                    tip, push_range, active_items, planner_warnings, fenced, progress,
                    fp=fp, carry_paths=carry_paths,
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
                if all_full and not carry_items:
                    # Golden argv: byte-identical to 0.7.0 (no --paths-file).
                    result, ran, raw_name = _run_review(
                        review_root, mode, git_dir, tip, push_range
                    )
                    chunks_new = 1 if ran else 0
                else:
                    # Single context with paths-file (some delta or some carry).
                    manifest_path = str(
                        _async_dir(common_dir) / f"manifest-{run_id}-sc.json"
                    )
                    manifest = {
                        "paths": [item["entry"]["path"] for item in active_items],
                        "renames": [
                            [item["entry"]["old_path"], item["entry"]["path"]]
                            for item in active_items if item["entry"].get("old_path")
                        ],
                        "other_changed": [],
                        "files": [
                            {
                                "path": item["entry"]["path"],
                                "mode": item["mode"],
                                "from_oid": item.get("from_oid") or "",
                                "to_oid": item["entry"].get("new_oid") or "",
                            }
                            for item in active_items
                        ],
                        "carried": carry_paths,
                    }
                    try:
                        tmp = manifest_path + ".tmp"
                        Path(tmp).write_text(
                            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
                        )
                        os.replace(tmp, manifest_path)
                    except Exception as exc:
                        raise ReviewGateError(
                            f"could not write single-context manifest: {exc}"
                        )
                    try:
                        result, ran, raw_name = _run_review(
                            review_root, mode, git_dir, tip, push_range,
                            paths_file=manifest_path,
                        )
                    finally:
                        try:
                            Path(manifest_path).unlink(missing_ok=True)
                        except Exception:
                            pass
                    chunks_new = 1 if ran else 0
                if ran and _ledger_enabled():
                    _write_run_records(result, active_items, common_dir, fp, run_id)
                n_delta = sum(1 for p in active_items if p["mode"] == "delta")
                n_full = len(active_items) - n_delta
                plan_summary = (
                    f"reviewed {len(active_items)} file(s) ({n_delta} delta, {n_full} full)"
                    + (f", carried {len(carry_items)}" if carry_items else "")
                )

            if planner_warnings and isinstance(result, dict):
                result = dict(result)
                result["warnings"] = (_planner_warning_objs(planner_warnings)
                                      + list(result.get("warnings") or []))

            # Handle prior findings from carry/delta records.
            to_resolve, auto_resolved, carried_findings = _classify_priors(
                plan, tip, review_root, common_dir, fp, run_id
            )
            resolver_results = {}
            if to_resolve and active_items:
                resolver_results = _run_resolver(
                    review_root, mode, git_dir, tip, push_range,
                    to_resolve, active_items, common_dir, fp, run_id,
                )

            # Process resolver output: guard, write resolutions, build still_present.
            still_present = []
            for p in to_resolve:
                fid = p["id"]
                res = resolver_results.get(fid) or {"status": "still_present"}
                if (res.get("status") == "resolved"
                        and _guard_resolution(res, active_items, push_range, review_root)):
                    _write_resolution(
                        common_dir, fp, fid,
                        p["record"].get("head_oid") or "",
                        res.get("evidence_path") or "",
                        _blob_oids_at(review_root, tip,
                                      [res.get("evidence_path") or ""]).get(
                            res.get("evidence_path") or "", ""),
                        res.get("evidence_quote") or "",
                        run_id,
                    )
                else:
                    f = dict(p["finding"])
                    f, _ = _reanchor_finding(f, review_root, tip)
                    still_present.append(dict(f, provenance="still_present"))

            # Re-anchor carried findings and mark provenance.
            anchored_carried = []
            for f in carried_findings:
                f2, _ = _reanchor_finding(f, review_root, tip)
                anchored_carried.append(f2)

            # Merge new findings (from reviewer) with priors.
            prior_findings = still_present + anchored_carried
            if isinstance(result, dict):
                new_findings = list(result.get("findings") or [])
                for nf in new_findings:
                    nf.setdefault("provenance", "new")
                # Python dedup: drop new findings that nearly duplicate a still_present.
                deduped_new = []
                for nf in new_findings:
                    if any(_findings_similar(nf, sp) for sp in still_present):
                        continue
                    deduped_new.append(nf)
                all_findings = deduped_new + prior_findings
                result = dict(result, findings=all_findings)
                if plan_summary:
                    result = dict(result, plan_summary=plan_summary)

        if plan is not None and plan_summary and isinstance(result, dict):
            result.setdefault("plan_summary", plan_summary)
    except _Fenced:
        stop.set()
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
    except ReviewGateError as exc:
        failure = ("review", str(exc))
        result, ran, raw_name, chunks_new = None, False, "", progress["new"]
    except BaseException as exc:  # noqa: BLE001 -- the file must always say why
        failure = ("crash", f"{type(exc).__name__}: {exc}")
        result, ran, raw_name, chunks_new = None, False, "", progress["new"]
    finally:
        stop.set()
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
    """Drop async state/logs older than MARKER_TTL; chunks use CHECKPOINT_TTL.

    The worktree sweep skips any worktree owned by a running state whose
    heartbeat is younger than STALE_S.  Chunk cache files live in chunks/ and
    are swept on their own longer TTL.
    """
    try:
        now = time.time()
        cutoff = now - MARKER_TTL

        # Collect live-run worktree paths so the sweep below can skip them.
        live_worktrees = set()
        for p in _async_dir(common_dir).glob("*.json"):
            try:
                st = _read_state(p) or {}
                if st.get("state") in ("running", "claimed"):
                    hb = float(st.get("heartbeat_ts") or st.get("claimed_ts") or 0)
                    if now - hb < STALE_S:
                        wt = st.get("worktree")
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
    """Return {path: oid} for the given paths at tip. Missing paths map to ''."""
    if not paths or not tip:
        return {p: "" for p in paths}
    out, rc = _git(
        ["ls-tree", "-r", "--full-tree", tip, "--"] + list(paths), cwd=root
    )
    result = {}
    if rc == 0:
        for line in out.splitlines():
            tab = line.find("\t")
            if tab == -1:
                continue
            meta, fpath = line[:tab].split(), line[tab + 1:]
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
    """
    raw_out, rc = _git(
        ["diff", "--raw", "-M", "--full-index", f"{base}..{tip}"], cwd=root
    )
    if rc != 0:
        return None, ["could not run git diff --raw; skipping chunking"]

    stat_out, _ = _git(["diff", "-M", "--numstat", f"{base}..{tip}"], cwd=root)

    entries = {}
    warnings = []
    for line in raw_out.splitlines():
        if not line.startswith(":"):
            continue
        parts = line[1:].split("\t", 2)
        if not parts:
            continue
        meta = parts[0].split()
        if len(meta) < 5:
            continue
        old_oid, new_oid, status_score = meta[2], meta[3], meta[4]
        status = status_score[0]
        if status == "D" or new_oid.strip("0") == "":
            continue  # pure deletion
        if status in ("R", "C") and len(parts) >= 3:
            old_path, new_path = parts[1], parts[2]
        elif len(parts) >= 2:
            old_path = new_path = parts[1]
        else:
            continue
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

    # Fill in line counts from numstat.
    _RENAME_RE = re.compile(r'\{([^}]*) => ([^}]*)\}')
    for line in (stat_out or "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        added_s, deleted_s, path_s = parts
        if added_s == "-" or deleted_s == "-":
            continue  # binary
        try:
            lines = int(added_s) + int(deleted_s)
        except ValueError:
            continue
        m = _RENAME_RE.search(path_s)
        if m:
            prefix, suffix = path_s[:m.start()], path_s[m.end():]
            new_path = (prefix + m.group(2) + suffix).replace("//", "/")
        else:
            new_path = path_s
        if new_path in entries:
            entries[new_path]["lines"] = lines

    return list(entries.values()), warnings


def _group_into_chunks(entries):
    """Group by top-level directory, splitting at CHUNK_LINES / CHUNK_FILES."""
    by_dir = {}
    for e in entries:
        top = e["path"].split("/")[0] if "/" in e["path"] else ""
        by_dir.setdefault(top, []).append(e)

    chunks, current, current_lines = [], [], 0
    for dir_entries in by_dir.values():
        for e in dir_entries:
            if e["lines"] > _CHUNK_LINES:
                # Oversized file: flush, then give it its own chunk.
                if current:
                    chunks.append(current)
                current, current_lines = [], 0
                chunks.append([e])
                continue
            if current and (current_lines + e["lines"] > _CHUNK_LINES
                            or len(current) >= _CHUNK_FILES):
                chunks.append(current)
                current, current_lines = [], 0
            current.append(e)
            current_lines += e["lines"]
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


def _compute_fingerprint(root, tip):
    """sha256 of model, PROTOCOL_VERSION, skill/rubric/rules/agents file contents,
    .ocr/ tree OID, and prompt-affecting env vars. The plugin version is
    intentionally excluded so code-only releases keep the ledger."""
    h = hashlib.sha256()
    h.update(_MODEL.encode("utf-8"))
    h.update(b"\x00")
    h.update(str(PROTOCOL_VERSION).encode("utf-8"))
    h.update(b"\x00")
    for rel in ("skills/review/SKILL.md", "skills/review/rubric.md"):
        try:
            h.update((Path(_PLUGIN_ROOT) / rel).read_bytes())
        except OSError:
            pass
        h.update(b"\x00")
    for subdir in ("skills/review/rules", "agents"):
        d = Path(_PLUGIN_ROOT) / subdir
        if d.is_dir():
            for f in sorted(d.glob("*.md")):
                try:
                    h.update(f.read_bytes())
                except OSError:
                    pass
                h.update(b"\x00")
    h.update((_ocr_tree_oid(root, tip) or "").encode("utf-8"))
    h.update(b"\x00")
    for var in _FINGERPRINT_ENV_VARS:
        h.update((os.environ.get(var) or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _read_ledger_record(record_path, fp, key, head_oid):
    """Return the record dict or None (miss, corrupt, mismatch, expired)."""
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
                         status, base_oid, findings, chain_depth, run_id):
    """Write one per-file ledger record. Best-effort: never breaks the gate."""
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
        if int(data.get("chain_depth") or 0) >= _CHAIN_DEPTH_MAX:
            continue
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
            plan.append({"entry": e, "mode": "carry", "record": record,
                         "from_oid": head_oid, "miss_reason": "none"})
            continue
        delta_record, from_oid = _find_delta_record(common_dir, fp, key, head_oid)
        if delta_record is not None:
            plan.append({"entry": e, "mode": "delta", "record": delta_record,
                         "from_oid": from_oid, "miss_reason": "none"})
            continue
        plan.append({"entry": e, "mode": "full", "record": None,
                     "from_oid": "", "miss_reason": "no_record"})
    return plan, warnings


def _classify_priors(plan_items, tip, review_root, common_dir, fp, run_id):
    """Decide the fate of prior findings from carry/delta records.

    Returns (to_resolve, auto_resolved, carried_findings):
      to_resolve   — list of {id, finding, record} for the resolver
      auto_resolved — findings where the target file no longer exists
      carried_findings — findings replayed as-is (low/info, blob unchanged)
    """
    has_active = any(item["mode"] in ("delta", "full") for item in plan_items)
    to_resolve, auto_resolved, carried_findings = [], [], []

    for item in plan_items:
        record = item.get("record")
        if record is None or item["mode"] == "full":
            continue
        for f in record.get("findings") or []:
            fpath = f.get("path") or item["entry"]["path"]
            sev = (f.get("severity") or "").lower()
            blob = _blob_oids_at(review_root, tip, [fpath]).get(fpath, "")
            if not blob:
                auto_resolved.append(f)
                continue
            fid = f.get("id") or _finding_id(f)
            # Check if already resolved from a previous run
            evidence_blob = record.get("head_oid") or ""
            # We don't have evidence_path/blob yet; that comes from the resolver output.
            # For now, add to resolver if criteria met; guard validates on return.
            if sev in ("high", "medium", "critical") and has_active:
                to_resolve.append({"id": fid, "finding": f, "record": record})
            elif sev in ("low", "info", ""):
                # Only route to resolver if the blob changed
                recorded_blob = record.get("head_oid") or ""
                if blob != recorded_blob:
                    to_resolve.append({"id": fid, "finding": f, "record": record})
                else:
                    carried_findings.append(dict(f, provenance="carried"))
            else:
                # Unknown severity: treat as medium (route to resolver)
                if has_active:
                    to_resolve.append({"id": fid, "finding": f, "record": record})
                else:
                    carried_findings.append(dict(f, provenance="carried"))

    return to_resolve, auto_resolved, carried_findings


def _run_resolver(repo_root, mode, git_dir, tip, push_range, to_resolve,
                  active_plan_items, common_dir, fp, run_id):
    """Invoke the resolver agent once. Returns {id: {status, evidence_path, ...}}.

    On failure returns all still_present (fail closed).
    """
    if not to_resolve:
        return {}
    prior_data = [{"id": p["id"], **p["finding"]} for p in to_resolve]
    manifest_path = str(_async_dir(common_dir) / f"resolver-{run_id}.json")
    try:
        _tmp = manifest_path + ".tmp"
        Path(_tmp).write_text(
            json.dumps({
                "resolve": prior_data,
                "active_paths": [item["entry"]["path"] for item in active_plan_items],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(_tmp, manifest_path)
    except Exception as exc:
        _warn(f"review-gate: could not write resolver manifest: {exc}")
        return {p["id"]: {"status": "still_present"} for p in to_resolve}
    try:
        result, _, _ = _run_review(
            repo_root, mode, git_dir, tip, push_range,
            paths_file=manifest_path,
            timeout=_CHUNK_TIMEOUT,
            raw_tag="-resolve",
        )
        if not isinstance(result, dict):
            raise ReviewGateError("resolver returned non-dict")
        resolutions = result.get("resolutions") or {}
        if not isinstance(resolutions, dict):
            resolutions = {}
        return resolutions
    except (ReviewGateError, ReviewLimitError):
        return {p["id"]: {"status": "still_present"} for p in to_resolve}
    finally:
        try:
            Path(manifest_path).unlink(missing_ok=True)
        except Exception:
            pass


def _guard_resolution(resolution, active_plan_items, push_range, review_root):
    """True if a 'resolved' verdict passes the Python guard.

    A resolution is accepted only when:
    - evidence_path names a delta/full file in this push
    - evidence_quote appears verbatim (whitespace-normalised) on the added
      side of that file's diff in this push
    """
    if not isinstance(resolution, dict) or resolution.get("status") != "resolved":
        return True  # still_present always passes
    evidence_path = resolution.get("evidence_path") or ""
    evidence_quote = (resolution.get("evidence_quote") or "").strip()
    if not evidence_path or not evidence_quote:
        return False
    active_paths = {item["entry"]["path"] for item in active_plan_items}
    if evidence_path not in active_paths:
        return False
    if not push_range:
        return False
    diff_out, rc = _git(
        ["diff", push_range, "--", evidence_path], cwd=review_root
    )
    if rc != 0:
        return False
    added = "\n".join(
        line[1:] for line in diff_out.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    return " ".join(evidence_quote.split()) in " ".join(added.split())


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
        all_records = []
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
        if len(all_records) > _LEDGER_MAX_RECORDS:
            all_records.sort(reverse=True)  # keep newest
            for _, stale in all_records[_LEDGER_MAX_RECORDS:]:
                try:
                    stale.unlink()
                except OSError:
                    pass
    except Exception:
        pass


def _plan_to_chunks(active_items):
    """Group active (delta/full) plan items into chunks for _run_chunked.

    Returns a list of lists of plan_item dicts (each item has 'entry', 'mode',
    'from_oid'). Mirrors _group_into_chunks but operates on plan items.
    """
    entries = [item["entry"] for item in active_items]
    path_to_item = {item["entry"]["path"]: item for item in active_items}
    grouped_entries = _group_into_chunks(entries)
    return [
        [path_to_item[e["path"]] for e in chunk_entries]
        for chunk_entries in grouped_entries
    ]


def _write_run_records(result, active_items, common_dir, fp, run_id):
    """Write per-file ledger records after a successful single-context review.

    Only writes records when status is success/completed_with_warnings and no
    global diff_truncated warning is present. Best-effort.
    """
    if not isinstance(result, dict):
        return
    status = result.get("status", "")
    if status not in ("success", "completed_with_warnings"):
        return
    warnings = result.get("warnings") or []
    for w in warnings:
        wtype = w.get("type") if isinstance(w, dict) else ""
        wtext = str(w) if not isinstance(w, dict) else ""
        if wtype == "diff_truncated" or "diff_truncated" in wtext:
            return
    findings = result.get("findings") or []
    by_path = {}
    for f in findings:
        p = f.get("path") or ""
        by_path.setdefault(p, []).append(f)
    for item in active_items:
        e = item["entry"]
        path = e["path"]
        old_path = e.get("old_path") or ""
        prev = item.get("record")
        chain_depth = (int(prev.get("chain_depth") or 0) + 1) if (prev and item["mode"] == "delta") else 0
        key = _record_key(path, old_path, e["status"], e["old_oid"])
        head_oid = e["new_oid"]
        file_findings = by_path.get(path) or []
        _write_ledger_record(
            common_dir, fp, key, head_oid, path, old_path,
            e["status"], e["old_oid"], file_findings, chain_depth, run_id,
        )


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
    for r in chunk_results:
        if not isinstance(r, dict):
            continue
        all_findings.extend(r.get("findings") or [])
        all_warnings.extend(r.get("warnings") or [])
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
    return {
        "status": worst_status,
        "findings": merged,
        "warnings": all_warnings,
        "summary": {
            "files_reviewed": files_reviewed,
            "findings": len(merged),
            "high": high, "medium": medium, "low": low,
        },
    }


def _run_chunked(state_path, run_id, common_dir, review_root, mode, git_dir,
                 tip, push_range, active_items, planner_warnings, fenced,
                 progress=None, fp="", carry_paths=None):
    """Run per-chunk reviews with fencing, budget and retry.

    active_items is a list of plan_item dicts (mode delta/full). Chunks are
    computed internally via _plan_to_chunks. carry_paths is the list of
    already-carried file paths (for the manifest's carried field).

    Returns (merged_result, True, raw_name, chunks_new) on success.
    chunks_new is the count of chunks reviewed in THIS run.
    Raises ReviewLimitError, ReviewBudgetError, ReviewGateError, or _Fenced.
    """
    chunks = _plan_to_chunks(active_items)
    total = len(chunks)
    budget_end = time.monotonic() + _RUN_BUDGET
    chunk_results = []
    chunks_done = 0
    chunks_new = 0

    for k, chunk_items in enumerate(chunks):
        if fenced["hit"]:
            raise _Fenced()

        # Progress update (fence-checked).
        try:
            _update_state_owned(
                state_path, run_id,
                chunk_index=k, chunks_total=total, chunks_done=chunks_done,
            )
        except _Fenced:
            raise

        # Check run budget before starting a new chunk.
        if time.monotonic() > budget_end:
            try:
                _update_state_owned(
                    state_path, run_id,
                    chunks_done=chunks_done, chunks_total=total,
                )
            except _Fenced:
                raise
            raise ReviewBudgetError(
                f"run budget ({_RUN_BUDGET}s) exhausted after "
                f"{chunks_done}/{total} chunks; re-push to resume"
            )

        # Write manifest for this chunk atomically; fail closed on error.
        manifest_path = str(
            _async_dir(common_dir) / f"manifest-{run_id}-{k}.json"
        )
        # other_changed: paths in OTHER chunks that are also being reviewed
        other_changed = [
            item["entry"]["path"]
            for i, ch in enumerate(chunks)
            for item in ch if i != k
        ]
        manifest = {
            "chunk_index": k, "chunks_total": total,
            "paths": [item["entry"]["path"] for item in chunk_items],
            "renames": [
                [item["entry"]["old_path"], item["entry"]["path"]]
                for item in chunk_items if item["entry"].get("old_path")
            ],
            "other_changed": other_changed,
            "files": [
                {
                    "path": item["entry"]["path"],
                    "mode": item["mode"],
                    "from_oid": item.get("from_oid") or "",
                    "to_oid": item["entry"].get("new_oid") or "",
                }
                for item in chunk_items
            ],
            "carried": carry_paths or [],
        }
        try:
            tmp = manifest_path + ".tmp"
            Path(tmp).write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, manifest_path)
        except Exception as exc:
            raise ReviewGateError(
                f"could not write chunk manifest for chunk {k}: {exc}"
            )

        # Clean worktree so one chunk can't leave state for the next.
        _git(["clean", "-fdxq"], cwd=review_root)
        _git(["checkout", "-q", "--", "."], cwd=review_root)

        # Run the review (retry once on non-timeout errors).
        result = None
        last_exc = None
        try:
            for attempt in range(2):
                if fenced["hit"]:
                    raise _Fenced()
                try:
                    result, _, _ = _run_review(
                        review_root, mode, git_dir, tip, push_range,
                        paths_file=manifest_path,
                        timeout=_CHUNK_TIMEOUT,
                        raw_tag=f"-c{k}",
                    )
                    last_exc = None
                    break
                except ReviewLimitError:
                    raise  # propagate immediately; do not retry limits
                except ReviewGateError as exc:
                    last_exc = exc
                    if exc.is_timeout or attempt > 0:
                        raise
                    # Non-timeout error: retry once.
                    continue
            if last_exc is not None:
                raise last_exc
        finally:
            # Always clean up the manifest (success or failure).
            try:
                Path(manifest_path).unlink(missing_ok=True)
            except Exception:
                pass

        # Fence-check before persisting: if another run claimed the state while
        # the reviewer was running, do not write records.
        try:
            _update_state_owned(
                state_path, run_id,
                chunks_done=chunks_done + 1, chunk_index=k, chunks_total=total,
            )
        except _Fenced:
            raise

        # Write ledger records for this chunk (only reached if not fenced).
        if fp and _ledger_enabled():
            _write_run_records(result, chunk_items, common_dir, fp, run_id)

        chunk_results.append(result)
        chunks_done += 1
        chunks_new += 1
        if progress is not None:
            progress["new"] = chunks_new

    merged = _merge_chunk_results(chunk_results, planner_warnings)
    # The per-chunk snapshots each hold one chunk; the run's record and the
    # stable last-output path must show all of them.
    raw_name = _save_raw_output(
        git_dir, json.dumps(merged, ensure_ascii=False, indent=2), tip, "-merged"
    )
    return merged, True, raw_name, chunks_new


def _mode_supervise(argv):
    try:
        i = argv.index("--state")
        state_path = argv[i + 1]
        j = argv.index("--run-id")
        run_id = argv[j + 1]
    except (ValueError, IndexError):
        return 2
    return _supervise(state_path, run_id)


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
        count += f", chunk {int(cd) + 1}/{int(ct)}"
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
        else:
            prefix = ""
        lines.append(f"  [{sev}] {prefix}{loc} - {content}")
    # limit=0 means "all of them" -- used by --history, which is read on demand
    # and has no context budget to protect, unlike the gate's own messages.
    return "\n".join(lines if not limit else lines[:limit])


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
    if verdict == "pass" and not count:
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
            chunk_note = f", chunk {int(cd) + 1}/{int(ct)}" if cd is not None and ct else ""
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
