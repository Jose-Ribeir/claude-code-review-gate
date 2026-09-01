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
#
# Failure policy: FAIL CLOSED on timeout, subprocess error, or unparseable
# output — and, as of 0.3.0, on a missing Python 3 or a reviewer the git hook
# cannot locate (both used to fail open, contradicting this very paragraph).
#
# What still fails OPEN, in full:
#   1. "claude not found" — deliberate; there is no sensible gate without it.
#   2. The hook failing to LAUNCH, timing out, or dying abnormally. Claude Code
#      treats a hook it could not start or had to kill as non-blocking, and no
#      code in here can override that. It is why hooks/hooks.json's timeout must
#      stay above OCR_TIMEOUT, and why /review-gate:doctor exists.
#   3. OCR_FAIL_OPEN=1 (one-shot bypass) / OCR_ADVISORY=1 (permanent warn-only).
#
# Raise OCR_TIMEOUT (default 1800 s) if legitimate reviews routinely time out,
# and raise hooks/hooks.json's timeout to stay above it.
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
_MARKER_PREFIXES = (MARKER_PREFIX, POST_DELIVERED_PREFIX, HOOKSPATH_WARNED_PREFIX)

# --mode post limits. The findings log is append-only and never pruned, so the
# scan is bounded from the newest end rather than reading the whole file; the
# context cap keeps one pathological review from flooding the session it is
# reporting into.
POST_SCAN_CAP = 200
POST_FINDING_LIMIT = 10
POST_MAX_CONTEXT = 3000

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


def _has_unpushed_commits(repo_root=None):
    # repo_root is not optional in spirit: without it this asked the PROCESS
    # cwd, which in hook mode is wherever Claude Code was launched from. On a
    # `cd other-repo && git push` it therefore answered about the session's
    # repo, found nothing unpushed, and allowed a push it had never looked at.
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


def _save_raw_output(git_dir, text, head_sha=""):
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
    return _archive_raw_output(git_dir, text, head_sha)


def _archive_raw_output(git_dir, text, head_sha=""):
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
        sha = (head_sha or "nohead")[:7]
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


# A `cd` at a command position. The separator class carries \n and \r
# explicitly: a Bash tool call is routinely MULTI-LINE, and without them
# `cd repo\ngit push` matched nothing at all (re.finditer is not re.MULTILINE,
# so `^` only ever meant the start of the whole string). Same omission is what
# made _REAL_PUSH below miss a push on its own line.
# A quote counts as a command boundary because `bash -c "cd /repo && ..."`
# really does start a command right after it. That is only safe because
# _mask_quoted has already blanked every quoted span that is DATA -- the only
# quotes reaching this pattern are a cd's own target and the body of a -c.
_CD_TARGET = re.compile(
    r"""(?:^|[;&|\n\r"']|&&|\|\|)\s*cd\s+(?:"([^"]*)"|'([^']*)'|([^\s;&|]+))"""
)


# `cmd <<MARKER` / `<<'MARKER'` / `<<-MARKER`, which opens a heredoc.
#
# The lookahead is load-bearing, and its exact shape has been wrong twice.
#
# What separates a real opener from the same characters used as data is what
# FOLLOWS the marker: an opener is followed by end-of-line, a redirection, or a
# pipe/separator -- never by a bare word. `echo "a <<EOF b"` fails on the ` b"`.
#
# It first allowed only redirections, which rejected the extremely ordinary
# `python - <<'PY' | tee log` and `cat <<'EOF' | grep x`, so their bodies went
# unstripped and the misparse it exists to prevent came straight back.
_HEREDOC = re.compile(
    r"""<<-?\s*(['"]?)([A-Za-z_][A-Za-z0-9_]*)\1(?=\s*(?:[0-9]*[<>|&;]|$))"""
)


def _strip_heredocs(cmd):
    """Drop heredoc BODIES before parsing a command for cds and pushes.

    A heredoc body is data the command writes, not commands the shell runs.
    Parsing it as code is not academic: writing a test file that happens to
    contain the string `cd repo && cd sub && git push` made this very gate
    block the Bash call adding those tests. The body determined a `cd` chain
    that never executed, the chain resolved to nothing, and the
    unknown-target rule denied an entirely innocent command.

    Only the body goes; the opener line stays, since a real `cd` can share it.
    """
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
            # No terminator anywhere. Then this was not a heredoc we can trust
            # -- and stripping to the end of the command would delete real
            # commands, including the cd and the push this parse exists to
            # find. Strip nothing and keep reading.
            continue
        i = j + 1  # past the body AND the terminator line
    return "\n".join(out)


# Each branch is written so a backslash can be consumed exactly ONE way. The
# first version used a backreference with `(?:\\.|(?!\1).)*`, whose two
# alternatives both match a backslash -- on an unterminated quote the engine
# tries every pairing of a backslash run and the match goes exponential. That
# is a denial of service in a hook that runs on every Bash call, so the classes
# below exclude the backslash and leave `\\.` as the only way to consume it.
# Single quotes take no escapes in POSIX sh, hence the simpler branch.
_QUOTED = re.compile(
    r"'[^']*'"  # single-quoted: literal through to the next quote
    r'|"(?:[^"\\]|\\.)*"',  # double-quoted: backslash escapes apply
    re.S,
)
# Runners whose `-c` argument is SHELL. Deliberately not every program with a
# -c flag: python's is Python, and reading it as shell turned a harmless
# `python -c "print('git push')"` into a push.
_SHELLS = ("bash", "sh", "zsh", "dash", "ksh", "bash.exe", "sh.exe")
# `-c`, or a combined short-flag cluster ending in it: `-lc`, `-ec`.
_DASH_C = re.compile(r"-[A-Za-z]*c$")
# Tokens that end one simple command and begin the next. A NEWLINE is one of
# them, and it has to be tokenized explicitly: str.split() throws newlines away
# as ordinary whitespace, so a multi-line `ls` followed by `bash -c "..."` on
# the next line looked like ONE command whose first word was `ls`, and the
# shell test failed on a genuine invocation.
_SEPARATORS = ("&&", "||", ";", "|", "&", ";;", "(", ")", "{", "}", "\n")


def _tokenize(text):
    """Shell tokens, with separators isolated even without spaces around them.

    A `\\n|\\S+` split cannot do this: `\\S+` swallows punctuation, so
    `echo done;bash -c ...` produced the single token `done;bash` and the
    separator was invisible. shlex's punctuation_chars mode splits `;`, `&&`,
    `||`, `|` and friends properly, which is what this needs and what hand
    rolling kept getting wrong.

    Lines are fed separately so a newline survives as its own separator token;
    shlex treats it as plain whitespace. An unbalanced quote makes shlex raise,
    and that falls back to a plain split rather than losing the line.
    """
    toks = []
    for i, line in enumerate(text.splitlines()):
        if i:
            toks.append("\n")
        try:
            lex = shlex.shlex(line, punctuation_chars=True, posix=False)
            lex.whitespace_split = True
            lex.commenters = ""  # `#` is not a comment to us; it is just text
            toks.extend(lex)
        except ValueError:
            toks.extend(line.split())
    return toks
# Deciding whether a quoted span is CODE asks whether the simple command that
# owns the `-c` MENTIONS a shell -- not which token is the runner. Five
# spellings of the latter each missed a real invocation, and every miss is a
# fail-open, because a false negative blanks a genuine
# `bash -c "cd /repo && git push"` into data and hides both the cd and the push:
#   - the token before `-c` missed `bash -lc` and `bash -eu -c`;
#   - walking back over "-" tokens missed flags with separate values
#     (`--rcfile FILE`, `-o pipefail`);
#   - the command's first word missed every wrapper (`sudo bash -c`), and
#     newlines vanished in str.split();
#   - a wrapper skip-list still missed wrappers with their own arguments
#     (`timeout 30 bash -c`).
# Membership needs none of that, and it errs toward calling a span CODE, which
# is the right direction: data read as code costs at worst a conservative
# block, while code read as data is the fail-open.


def _mask_quoted(cmd):
    """Blank the inside of quoted spans that are DATA rather than code.

    `git commit -m "cd repo && git push"` contains neither a cd chain nor a
    push -- it contains a message. Parsing it as code was a way to make this
    gate misread a command: a chain that never ran resolved to nothing, and
    the unknown-target rule denied something innocent.

    Two spans are kept verbatim:
      cd         -- `cd "path with spaces"` really is the target, and blanking
                    it would lose the very hop being followed.
      <shell> -c -- really does execute its argument as shell (see above).

    Quotes themselves are preserved so token boundaries do not shift, and an
    unbalanced quote simply fails to match and is left alone.
    """
    if not cmd or ("'" not in cmd and '"' not in cmd):
        return cmd or ""
    out, last, prev = [], 0, ""
    # Whether the simple command being scanned mentions a shell. Tracked as a
    # running flag rather than re-scanned per span: a backward walk over the
    # accumulated tokens was O(current length) each time, which is the same
    # quadratic shape this function has already had to remove once.
    seg_has_shell = False
    for m in _QUOTED.finditer(cmd):
        gap = cmd[last : m.start()]
        for t in _tokenize(gap):
            if t in _SEPARATORS:
                seg_has_shell = False  # a new simple command starts here
            elif os.path.basename(t) in _SHELLS:
                seg_has_shell = True
            prev = t
        out.append(gap)
        if prev == "cd" or (_DASH_C.match(prev) and seg_has_shell):
            out.append(m.group(0))
        else:
            q = m.group(0)[0]  # the pattern no longer captures it
            out.append(q + " " * (len(m.group(0)) - 2) + q)
        # A quoted span is ONE token to whatever follows it, kept or blanked,
        # and it is neither "cd" nor a -c.
        prev = '""'
        last = m.end()
    out.append(cmd[last:])
    return "".join(out)


def _shell_code(cmd):
    """The part of a command the shell will actually RUN, for parsing.

    Heredoc bodies go first (they are written, not run), then quoted data is
    blanked. Everything that reads a command for cds or pushes goes through
    here, so the two rules stay in one place.
    """
    return _mask_quoted(_strip_heredocs(cmd or ""))


def _cd_targets(cmd):
    """Every directory a shell command cds into before it reaches `git push`.

    In command order. `~` and `-` are kept rather than filtered: _effective_cd
    gives them meaning (expandable, and unfollowable, respectively), and
    dropping a hop silently would misplace every hop after it.
    """
    if not cmd:
        return []
    code = _shell_code(cmd)
    # Bound the scan at the push COMMAND, not the first literal occurrence of
    # the text: `echo remember to git push` is a mention, and truncating there
    # would hide a real cd that comes after it.
    stop = _REAL_PUSH.search(code)
    head = code[: stop.start()] if stop else code
    targets = []
    for m in _CD_TARGET.finditer(head):
        target = next((g for g in m.groups() if g), "")
        if target:
            targets.append(target)
    return targets


# A target we cannot resolve lexically: `$VAR`, `${VAR}`, `$(cmd)`, backticks.
# Deliberately not expanded from OUR environment -- those were set by the shell
# running the command, so substituting our values would answer confidently wrong.
_UNEXPANDABLE = re.compile(r"[$`]")


def _effective_cd(cmd, base):
    """The directory a command has reached by the time it runs `git push`.

    Folds the WHOLE chain instead of looking only at the last hop, because a
    relative hop is relative to the PREVIOUS one: in `cd repo && cd sub &&
    git push`, `sub` lives under `repo`, not under the session directory.
    Joining it against the session base produced a path that either did not
    exist (falling through to a fallback) or, worse, existed and pointed
    somewhere unrelated.

    Returns "" when the chain cannot be followed. An absolute hop re-anchors
    and clears an earlier unknown -- `cd $T && cd /srv/repo` ends at
    /srv/repo no matter what `$T` was -- while a relative hop after an
    unknown one stays unknown, since there is nothing to join it onto.
    """
    targets = _cd_targets(cmd)
    if not targets:
        return base or ""
    cur, unknown = base or "", False
    for raw in targets:
        if raw == "-" or _UNEXPANDABLE.search(raw):
            unknown = True  # `cd -` needs shell history; `$VAR` needs its shell
            continue
        try:
            t = os.path.expanduser(raw)
            if os.path.isabs(t):
                cur, unknown = t, False
            elif unknown or not cur:
                continue
            else:
                cur = os.path.normpath(os.path.join(cur, t))
        except Exception:
            unknown = True
    return "" if unknown else cur


def _resolve_pushed_repo(payload):
    """The repository the push actually happened in.

    Neither the process cwd nor the payload's `cwd` is reliable: both are the
    SESSION's directory, and Claude Code routinely runs pushes as
    `cd <repo> && git push`. Reading the session dir instead cost us a real
    failure in testing -- the hook reported a different repository's month-old
    findings as though they described the push that had just completed, which
    is worse than saying nothing at all.

    So: the folded `cd` chain wins (see _effective_cd), then the payload's
    cwd, then the process cwd. Each candidate is confirmed by asking git to
    resolve it.

    The chain is FOLDED, not sampled. An earlier version took only the last
    hop and joined it onto the session dir, which is wrong whenever that hop
    is relative -- it belongs under the hop before it, not under the base.
    _effective_cd also decides what "unresolvable" means: shell parsing cannot
    expand variables, so `cd "$T"` yields a literal `$T` and the chain goes
    unknown from there unless a later ABSOLUTE hop re-anchors it. An
    unresolvable chain means we do not know, and the freshness check in
    _mode_post is what keeps "we do not know" from turning into a confident
    report about the wrong repo.
    """
    cmd, cwd = "", ""
    if isinstance(payload, dict):
        cmd = (payload.get("tool_input") or {}).get("command", "") or ""
        cwd = str(payload.get("cwd") or "")
    # _effective_cd folds the whole cd chain onto the session dir, so a
    # relative hop is joined onto the hop before it rather than onto the base.
    candidates = [_effective_cd(cmd, cwd or os.getcwd())]
    if cwd:
        candidates.append(cwd)
    candidates.append(os.getcwd())
    for cand in candidates:
        try:
            if not cand or not os.path.isdir(cand):
                continue
        except Exception:
            continue
        out, rc = _git(["rev-parse", "--show-toplevel"], cwd=cand)
        if rc == 0 and out:
            return out
    return ""


def _fail_open_requested():
    return os.environ.get("OCR_FAIL_OPEN", "").strip().lower() in ("1", "true", "yes")


def _push_target_unknown(payload):
    """True when the command cds somewhere we cannot resolve to a repository.

    This is the distinction the GATE needs and the reporter does not. Both use
    _resolve_pushed_repo, but its fallback chain always yields something -- and
    for the gate, silently falling back to the session's repo is precisely the
    fail-open being fixed: it reviews a repo nobody is pushing, finds nothing
    unpushed, and allows.

    So the gate asks this separately: was there a `cd` we could not follow?
    "No cd at all" means the session's cwd IS the target and everything is
    fine. "There was a cd and it resolved to nothing" -- an unexpanded `$VAR`,
    a command substitution, a directory that is not a repo -- means we do not
    know where these commits are going, and a gate that does not know what it
    is looking at must not wave the push through.
    """
    cmd, cwd = "", ""
    if isinstance(payload, dict):
        cmd = (payload.get("tool_input") or {}).get("command", "") or ""
        cwd = str(payload.get("cwd") or "")
    if not _cd_targets(cmd):
        return False  # no cd at all: the session's cwd IS the target
    # The whole chain, folded. "" means a hop we could not follow -- `cd $VAR`,
    # `cd -`, or a relative hop after one of those.
    target = _effective_cd(cmd, cwd or os.getcwd())
    try:
        if not target or not os.path.isdir(target):
            return True
    except Exception:
        return True
    out, rc = _git(["rev-parse", "--show-toplevel"], cwd=target)
    return not (rc == 0 and out)


# `git push` at a COMMAND position -- optionally behind env-var prefixes and
# git's own flags -- rather than anywhere in the string.
_REAL_PUSH = re.compile(
    r"(?:^|[;&|\n\r\"']|&&|\|\|)\s*"    # start, separator, new LINE, or a quote
    # \n matters: a multi-line Bash call putting `git push` on its own line
    # matched none of the alternatives, so this returned False for a genuine
    # push and the unknown-target block it gates silently never fired.
    r"(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"  # env-var prefixes: FOO=bar git push
    r"git\s+"
    r"(?:(?:-\S+|\S+=\S+)\s+)*"         # git's own flags AND their values: git -c a=b push
    r"push\b"
)


def _looks_like_real_push(cmd):
    """True when the command actually invokes `git push`, not merely mentions it.

    The adapters gate on a bare "git push" SUBSTRING, which is deliberately
    loose -- over-reviewing is cheap. Blocking is not. A command that merely
    contains the words (a grep pattern, a heredoc, a commit message, a Python
    string) must never be denied for a reason about pushing, so the block below
    is held to this stricter test while the review trigger keeps the loose one.

    Heredoc bodies go first, for the same reason: a script or test file being
    written by the command is data, and a `git push` inside it is not one the
    shell will run.
    """
    return bool(_REAL_PUSH.search(_shell_code(cmd)))


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


def _hook_timeout_budget():
    """Return hooks/hooks.json's PreToolUse 'timeout' (int seconds), or None if
    it can't be read/parsed. Never raises -- this is advisory message text, not
    gate logic, so a missing/malformed file must not crash the review."""
    try:
        path = Path(os.path.dirname(os.path.abspath(__file__))) / ".." / "hooks" / "hooks.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"])
    except Exception:
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


def _run_review(repo_root, mode, git_dir=None, head_sha=""):
    """Return (result_dict, True, raw_archive_name) on success.

    Raises ReviewGateError on timeout, subprocess error, a non-zero claude exit
    (the review never ran, so the output is an error string rather than
    malformed JSON), or unparseable output, so that main() can fail the gate
    closed.  Only 'claude not found' still returns (None, False) to allow the
    commit — there is no gate without the tool.
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
    try:
        proc = subprocess.run(
            [claude, "-p", PROMPT] + args,
            cwd=repo_root,
            capture_output=True,
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
            timeout=TIMEOUT,
            env=child_env,
        )
    except subprocess.TimeoutExpired:
        if mode == "hook":
            # gate-hook.sh execs this script with no argv/stdin parsing of OCR_TIMEOUT
            # (see gate-hook.sh) -- it only ever sees whatever environment Claude Code's
            # own PreToolUse hook launcher was started with, NOT the shell env of the
            # `git commit` Bash tool call. An inline `OCR_TIMEOUT=<n> git commit ...`
            # prefix therefore never reaches this process in hook mode.
            budget = _hook_timeout_budget()
            budget_str = f"currently {budget}s" if budget is not None else "see hooks/hooks.json"
            escalation = (
                f"  Give Claude more time : export OCR_TIMEOUT={TIMEOUT * 2} in the environment\n"
                f"    Claude Code itself is launched from (a shell prefix on `git commit` will\n"
                f"    NOT work in hook mode -- this process inherits Claude Code's env, not the\n"
                f"    Bash tool call's). Also raise hooks/hooks.json's PreToolUse 'timeout'\n"
                f"    ({budget_str}) to stay above the new OCR_TIMEOUT -- Claude Code kills\n"
                f"    this hook at that fixed harness deadline regardless of OCR_TIMEOUT, which\n"
                f"    silently reopens the fail-open path this gate exists to close."
            )
        else:
            escalation = f"  Give Claude more time : OCR_TIMEOUT={TIMEOUT * 2} git commit ..."
        raise ReviewGateError(
            f"review timed out after {TIMEOUT}s - blocking commit to preserve gate integrity.\n"
            f"{escalation}\n"
            f"{bypass}"
        )
    except Exception as exc:
        raise ReviewGateError(
            f"review process error ({exc}) - blocking commit to preserve gate integrity.\n"
            f"{bypass}"
        )
    raw_name = _save_raw_output(git_dir, proc.stdout, head_sha)
    # A non-zero exit means claude never got as far as producing a review, so the
    # output is an error string, not malformed JSON. Diagnose that separately:
    # reporting "could not parse review output" for a login failure sends people
    # looking at the review skill when the real fault is the CLI's credentials.
    # Note claude writes these errors to STDOUT, so stderr is often empty.
    if proc.returncode != 0:
        # Strip BEFORE falling through: a whitespace-only stdout is truthy, so
        # `stdout or stderr` would select it and discard a real stderr message,
        # leaving detail empty and hiding why the review failed.
        detail = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        raise ReviewGateError(
            f"`claude` exited {proc.returncode} without running the review -- blocking commit "
            "to preserve gate integrity.\n"
            f"  {claude}\n"
            f"  Output (first 400 chars): {detail[:400]!r}\n"
            f"{_auth_hint(detail)}"
            f"{bypass}"
        )
    result = _extract_json(proc.stdout)
    if result is None:
        # Exit 0 but no JSON. Auth failures have been seen to exit 0 too
        # (the Desktop-bundled claude.exe does exactly this), so still check.
        raise ReviewGateError(
            "could not parse review output - blocking commit to preserve gate integrity.\n"
            f"  Claude stdout (first 400 chars): {proc.stdout[:400]!r}\n"
            f"{_auth_hint(proc.stdout or '')}"
            f"{bypass}"
        )
    return result, True, raw_name


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
        lines.append(f"  [{sev}] {loc} - {content}")
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
    # A clean pass says nothing. This body is injected on every push forever,
    # and "the gate ran" is already observable: the PreToolUse hook shows its
    # status line for the whole review, and --history replays any of them.
    if verdict == "pass" and not count:
        return ""
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
        lines.append(
            "  NOTE: this repo sets its own core.hooksPath, which shadows the global "
            "review-gate git hook - pushes from a plain terminal here are NOT gated."
        )
    return "\n".join(lines)[:POST_MAX_CONTEXT]


_COMMIT = re.compile(
    r"(?:^|[;&|\n\r\"']|&&|\|\|)\s*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*git\s+(?:(?:-\S+|\S+=\S+)\s+)*commit\b"
)


def _commits_in_same_command(cmd):
    """True when the command creates the commit it then pushes.

    A PreToolUse hook runs BEFORE the command, so at gate time the new commit
    does not exist yet and HEAD still points at its parent: there is nothing
    unpushed to review, and the plugin adapter allows without looking at
    anything. The git pre-push adapter does see it, because it runs after the
    commit -- so this is a gap only where that adapter is absent or shadowed
    by a repo-local core.hooksPath.

    Structural: no pre-tool hook can review a commit that does not exist. The
    least it can do is not let the push pass in silence.
    """
    code = _shell_code(cmd)
    m = _COMMIT.search(code)
    if not m:
        return False
    # _REAL_PUSH, not a substring search: `git commit -m "wip" && echo remember
    # to git push later` only commits, and calling that "pushed unreviewed"
    # would be a false alarm about work that was never sent anywhere.
    at = _REAL_PUSH.search(code)
    return bool(at) and m.start() < at.start()


def _push_was_a_noop(payload):
    """True when git had nothing to push, so "unreviewed" would be noise."""
    try:
        resp = payload.get("tool_response") or {}
        blob = f"{resp.get('stdout', '')}\n{resp.get('stderr', '')}"
    except Exception:
        return False
    return "Everything up-to-date" in blob


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


def _mode_post(argv):
    """`--mode post`: replay this HEAD's recorded review as PostToolUse context.

    This is the ONLY channel that puts non-blocking findings in front of the
    model. Verified 2026-09 against Claude Code's own transcripts: a PostToolUse
    hook returning additionalContext produces a `hook_additional_context`
    record -- the delivery vehicle -- while a PreToolUse
    `permissionDecisionReason` on an ALLOW produces none. It is logged UI-side
    and goes nowhere else, which is why blocks (delivered as the tool_result of
    a deny) were never the problem and warns always were.

    PostToolUse fires only for a tool call that actually ran and succeeded
    (verified: 760 failed Bash calls produced 0 PostToolUse hooks), so a
    rejected push never reaches this code. That is deliberate rather than a
    hole: the delivered-marker is claimed here, so an attempt that never got
    here leaves it unset and the retry -- same HEAD, same record -- reports
    then. The residual gap is a command that updates the reviewed ref yet exits
    non-zero and is never retried (a multi-ref push where one ref is rejected;
    `git push && gh pr create` where gh fails). Those findings stay in
    FINDINGS_LOG, reachable via --history.

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
        # Same substring rule the PreToolUse adapter applies. The shell adapter
        # already checked; this guards a direct invocation.
        if cmd and "git push" not in cmd:
            return 0

    # Resolved from the COMMAND, not the process cwd -- see _resolve_pushed_repo.
    repo_root = _resolve_pushed_repo(payload)
    if not repo_root:
        return 0
    git_dir = _git_dir(repo_root)
    head = _head_sha(repo_root)
    if not git_dir or not head:
        return 0  # not a repo, or a detached/unborn HEAD -- nothing to replay

    entry = _latest_record_for_head(git_dir, head)
    if not entry:
        # Usually uninteresting: nothing was pushed, or this repo is not gated.
        # One shape is worth saying out loud rather than passing in silence --
        # see _commits_in_same_command.
        if not _commits_in_same_command(cmd) or _push_was_a_noop(payload):
            return 0
        marker = Path(git_dir) / f"{POST_DELIVERED_PREFIX}{_marker_digest('unreviewed', head, session_id)}"
        if not _claim_marker(marker):
            return 0
        _emit_post_context(
            "review-gate: these commits were NOT reviewed.\n"
            "  The command created the commit and pushed it in one step, so the gate ran\n"
            "  before that commit existed and had nothing to look at. The global git hook\n"
            "  covers this shape, but it did not run here (not installed, or shadowed by a\n"
            "  repo-local core.hooksPath -- /review-gate:doctor will say which).\n"
            "  To review them now: commit and push as two separate commands next time, or\n"
            "  run /review-gate:review over the pushed range."
        )
        _reap_markers(git_dir, keep=marker)
        return 0

    # The record must describe THIS push, not merely this HEAD. Resolving the
    # repo from a shell command is best-effort (see _resolve_pushed_repo), so
    # when it guesses wrong it tends to land on whatever repository the session
    # happens to sit in -- whose HEAD has a record too, often months old. That
    # is exactly how a live test reported a different repo's stale findings as
    # though they were this push's. A real push's review is seconds old, so
    # requiring freshness turns "we guessed wrong" into silence rather than
    # into a confident, wrong report. Same window the gate already treats a
    # review as still describing the current push.
    try:
        if time.time() - float(entry.get("ts") or 0) > MARKER_TTL:
            return 0
    except (TypeError, ValueError):
        return 0

    # Claim BEFORE emitting, not after: both adapters can run against one push,
    # and check-then-act would let both report. Claiming a marker we then decide
    # not to use (a clean pass) costs nothing -- we would have stayed silent.
    key = _marker_digest(head, entry.get("ts") or entry.get("at") or "", session_id)
    delivered = Path(git_dir) / f"{POST_DELIVERED_PREFIX}{key}"
    if not _claim_marker(delivered):
        return 0  # this exact review is already in this session's context

    # Once per session, not once per push: the condition is a static property of
    # the repo, and repeating it every time trains the reader to skip it.
    shadow = False
    if _hookspath_shadowed(repo_root):
        warned = Path(git_dir) / f"{HOOKSPATH_WARNED_PREFIX}{_marker_digest(session_id or head)}"
        shadow = _claim_marker(warned)

    text = _post_context(entry, git_dir, shadow)
    if not text:
        return 0
    _emit_post_context(text)
    _reap_markers(git_dir, keep=delivered)
    return 0


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
            _emit_hook("allow")
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
            if "git push" not in cmd:
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
    if mode == "hook":
        # Both conditions, and the second is what keeps this safe: the review
        # trigger is a loose "git push" substring, so without it any command
        # merely CONTAINING those words behind an unresolvable cd would be
        # denied -- a grep pattern, a heredoc, a Python string. Over-reviewing
        # is cheap; over-blocking is not.
        if _push_target_unknown(payload) and _looks_like_real_push(
            (payload.get("tool_input") or {}).get("command", "") if isinstance(payload, dict) else ""
        ):
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
        repo_root = _resolve_pushed_repo(payload) or _repo_root()
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

    if not _has_unpushed_commits(repo_root):
        allow()

    # Anchored on repo_root, which is now genuinely the repo being pushed
    # rather than whatever directory this process happens to sit in (see the
    # resolution above -- this comment described the intent long before the
    # code achieved it). It may still be "" outside a repo, so every consumer
    # below guards for that rather than inventing a path.
    git_dir = _git_dir(repo_root)
    head_sha = _head_sha(repo_root)
    marker = _marker_path(git_dir, head_sha) if (git_dir and head_sha) else None

    # The other adapter already reviewed this exact staged tree and passed it.
    # Replay what it found instead of allowing silently: the duplicate review
    # is what we are skipping, not the report.
    if marker and _marker_fresh(marker):
        note = _prior_findings_note(_read_marker(marker))
        if note:
            _warn(note + _output_hints(git_dir, _findings_log_path(git_dir) if git_dir else None))
        allow(note)

    try:
        result, ran, raw_name = _run_review(repo_root, mode, git_dir, head_sha)
    except ReviewGateError as exc:
        # Fail closed: block the commit unless OCR_FAIL_OPEN=1 is set.
        if os.environ.get("OCR_FAIL_OPEN", "").strip().lower() in ("1", "true", "yes"):
            _warn(
                f"OCR_FAIL_OPEN=1 set - bypassing fail-closed gate. Reason:\n  {exc}\n"
                "[!] This bypass should be used sparingly and intentionally."
            )
            allow()
            return  # unreachable; allow() calls sys.exit / _emit_hook
        _fail_closed(mode, str(exc))
        return  # unreachable

    if not ran:
        allow()  # fail-open: only reaches here when claude is not installed

    verdict = compute_verdict(result)
    advisory = _is_advisory(repo_root)
    reasons = _format_reasons(result)
    blocked = verdict == "block" and not advisory

    # Persist BEFORE deciding, and for every verdict. A non-blocking review is
    # the one that needs this: it lets the push through, so nothing forces
    # anyone to read it, and its raw output is overwritten by the next run.
    record = _record_review(
        git_dir, head_sha, _branch(repo_root), mode, verdict, advisory, blocked, result, raw_name
    )
    hints = _output_hints(git_dir, record)

    if blocked:
        reason = "review-gate blocked this commit (high-severity issues):\n" + (
            reasons or "  (see review output)"
        ) + f"{hints}\n\nFix the issues above, then commit again.\n{_downgrade_hint(mode)}"
        _fail_closed(mode, reason)
        return  # unreachable

    # Passed (or advisory): record the marker -- with the findings in it, so the
    # paired adapter's short-circuit can replay them -- report, and allow.
    if marker:
        try:
            _write_marker(marker, head_sha, verdict, advisory, reasons)
        except Exception:
            pass
        _reap_markers(git_dir, keep=marker)
    note = ""
    if reasons:
        label = "advisory (blocking disabled)" if advisory else f"verdict: {verdict}"
        note = f"{label} - findings:\n{reasons}"
        _warn(note + hints)
    allow(note)


if __name__ == "__main__":
    main(sys.argv)
