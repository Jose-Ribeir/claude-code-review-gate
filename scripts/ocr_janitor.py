#!/usr/bin/env python3
"""review-gate janitor (port plan C9): garbage-collect the gate's state.

The gate is append-heavy by design (D12): every review leaves a `git clone
--no-hardlinks` under the store's clones/, a state record, verdict records
that outlive their pushes, and marker files whose sessions are gone. This
script reclaims that, manually -- the gate itself never deletes anything a
running review might still need:

  (a) clone dirs whose owning state entry is done/failed older than --days
      (default 7), or whose owning state entry is gone entirely,
  (b) stale pending-* / pushed-repo-* markers in data/ whose repository no
      longer exists on disk,
  (c) rotation of the per-repo review-gate-findings.jsonl logs (keep the
      newest --log-bytes, default 10 MiB; whole lines only),
  (d) pruning of verdict records older than --verdict-days (default 30) --
      NEVER a verdict whose tip is still reachable in a live recorded repo
      (verified with git; anything unverifiable is kept),
  (e) sticky-error states older than --days: REPORTED, never auto-cleared --
      whether a tip that failed 3 times should be retried is a human
      decision (OCR_FORCE_REVIEW=1 on the next push).

`--dry-run` prints every action without performing it. Pure stdlib.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_DAYS = 7            # clones + sticky-error report horizon
DEFAULT_VERDICT_DAYS = 30   # verdict pruning horizon
DEFAULT_LOG_BYTES = 10 * 1024 * 1024
ATTEMPT_CAP = 3             # mirrors review-gate.py: the sticky error state
DAY = 86400.0


def _scrub_env(env):
    """A copy of `env` with every GIT_TRACE* key removed (see conftest.py)."""
    return {k: v for k, v in env.items() if not k.startswith("GIT_TRACE")}


def _run(cmd, env, timeout=30):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, env=_scrub_env(env))


def _load_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _iter_states(root):
    """Every async state record under the store: (path, dict-or-None).

    None marks an unparseable file. JSON side files with no "state" key
    (manifests) are skipped.
    """
    out = []
    try:
        dirs = sorted((root / "async").iterdir())
    except OSError:
        return out
    for d in dirs:
        if not d.is_dir():
            continue
        try:
            paths = sorted(d.glob("*.json"))
        except OSError:
            continue
        for p in paths:
            data = _load_json(p)
            if data is None or "state" in data:
                out.append((p, data))
    return out


def _terminal_ts(data):
    """done_ts / failed_ts of a state record, or None."""
    for key in ("done_ts", "failed_ts"):
        try:
            v = float(data.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return None


def _fmt_age(seconds):
    return f"{max(0.0, seconds) / DAY:.1f} days"


def _remove(path, dry):
    if not dry:
        try:
            import shutil
            shutil.rmtree(path)
            return True
        except OSError:
            try:
                os.unlink(path)
                return True
            except OSError:
                return False
    return True


# --- (a) clone dirs ------------------------------------------------------------

def gc_clones(root, states, days, now, dry, emit):
    clones = root / "clones"
    try:
        entries = sorted(p for p in clones.iterdir() if p.is_dir())
    except OSError:
        return
    by_run = {}
    for p, data in states:
        if data and data.get("run_id"):
            by_run.setdefault(str(data["run_id"]), (p, data))
    for clone in entries:
        # make_review_clone names them {tip[:12]}-{run_id}; the run_id is the
        # unique claim token, so it alone identifies the owning state entry.
        run_id = clone.name.split("-", 1)[1] if "-" in clone.name else ""
        owner = by_run.get(run_id)
        if owner is None:
            emit("clone", clone,
                 "no owning state entry (orphan)", "removed orphan clone")
            _remove(clone, dry)
            continue
        _, data = owner
        state = data.get("state")
        if state in ("running", "claimed"):
            continue  # a live review is reading it
        if state in ("done", "failed"):
            ts = _terminal_ts(data)
            try:
                ts = ts if ts else clone.stat().st_mtime
            except OSError:
                ts = now
            if now - ts > days * DAY:
                emit("clone", clone,
                     f"state {state}, {_fmt_age(now - ts)} old",
                     "removed clone")
                _remove(clone, dry)
        # any other state value: keep, we do not know what it means


# --- (b) stale markers ---------------------------------------------------------

def gc_markers(root, dry, emit):
    data = root / "data"
    try:
        pendings = sorted(data.glob("pending-*"))
        pushed = sorted(data.glob("pushed-repo-*"))
    except OSError:
        return
    for p in pendings:
        body = _load_json(p)
        repo = str((body or {}).get("repo") or "")
        if body is None or not repo or not os.path.isdir(repo):
            why = "unreadable" if body is None else f"repo gone: {repo or '(none recorded)'}"
            emit("marker", p, why, "removed stale pending marker")
            _remove(p, dry)
    for p in pushed:
        try:
            repo = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not repo or not os.path.isdir(repo):
            emit("marker", p, f"repo gone: {repo or '(none recorded)'}",
                 "removed stale pushed-repo marker")
            _remove(p, dry)


# --- (c) findings-log rotation --------------------------------------------------

def rotate_logs(states, log_bytes, dry, emit):
    if log_bytes <= 0:
        return
    git_dirs = sorted({str(d.get("git_dir")) for _, d in states
                       if d and d.get("git_dir")})
    for gd in git_dirs:
        log = Path(gd) / "review-gate-findings.jsonl"
        try:
            size = log.stat().st_size
        except OSError:
            continue
        if size <= log_bytes:
            continue
        try:
            raw = log.read_bytes()
        except OSError:
            continue
        tail = raw[size - log_bytes:]
        nl = tail.find(b"\n")
        if nl >= 0:
            tail = tail[nl + 1:]
        if not tail:
            continue
        if not dry:
            tmp = log.with_name(log.name + f".janitor-{os.getpid()}")
            try:
                tmp.write_bytes(tail)
                os.replace(str(tmp), str(log))
            except OSError:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                emit("log", log, f"{size} bytes (rotation failed; log kept whole)",
                     "rotation FAILED")
                continue
        emit("log", log, f"{size} -> {len(tail)} bytes (kept newest)",
             "rotated findings log")


# --- (d) verdict pruning --------------------------------------------------------

def _tip_reachable(repo, tip, env):
    """True/False/None: is `tip` reachable from any ref in the repo at `repo`?

    None means "could not verify" (not a git repo, git failed) -- the caller
    KEEPS the verdict in that case: never prune what cannot be checked.
    """
    if not os.path.isdir(repo):
        return False  # the repo is gone; nothing there can be live
    try:
        out = _run(["git", "-C", repo, "rev-parse", "--absolute-git-dir"], env)
    except Exception:
        return None
    if out.returncode != 0:
        return None  # not a git repo (or git broken): unsure
    try:
        out = _run(["git", "-C", repo, "cat-file", "-e", tip + "^{commit}"], env)
    except Exception:
        return None
    if out.returncode != 0:
        return False  # the repo does not even have the object: not reachable
    try:
        out = _run(["git", "-C", repo, "for-each-ref", "--contains", tip], env)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return bool(out.stdout.strip())


def gc_verdicts(root, states, verdict_days, env, now, dry, emit):
    repos_by_tip = {}
    for _, data in states:
        if not data:
            continue
        tip = str(data.get("tip") or "")
        repo = str(data.get("repo_root") or "")
        if tip and repo:
            repos_by_tip.setdefault(tip, set()).add(repo)
    try:
        paths = sorted((root / "verdicts").glob("*.json"))
    except OSError:
        return
    for p in paths:
        data = _load_json(p)
        if data is None:
            emit("verdict", p, "unparseable record -- left for the doctor",
                 "kept corrupt verdict")
            continue
        try:
            ts = float(data.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0
        age = now - ts
        if age <= verdict_days * DAY:
            continue  # young: not even a candidate
        tip = str(data.get("tip") or "")
        repos = repos_by_tip.get(tip)
        if not repos:
            emit("verdict", p, f"{_fmt_age(age)} old, no repo recorded for the tip",
                 "kept verdict (unsure)")
            continue
        # NEVER prune a verdict whose tip is still reachable in a live repo;
        # anything unverifiable is kept ("if unsure, keep").
        reachable, unsure = False, False
        for repo in sorted(repos):
            r = _tip_reachable(repo, tip, env)
            if r is True:
                reachable = True
                break
            if r is None:
                unsure = True
        if reachable or unsure:
            emit("verdict", p,
                 f"{_fmt_age(age)} old but the tip is "
                 + ("still reachable in a live repo" if reachable else "unverifiable"),
                 "kept verdict (live)")
            continue
        emit("verdict", p,
             f"{_fmt_age(age)} old, tip not reachable in any recorded repo",
             "pruned verdict")
        if not dry:
            try:
                os.unlink(p)
            except OSError:
                pass


# --- (e) sticky-error report ----------------------------------------------------

def report_sticky(states, days, now, emit):
    for p, data in states:
        if not data or data.get("state") != "failed":
            continue
        try:
            attempts = int(data.get("attempts") or 0)
        except (TypeError, ValueError):
            attempts = 0
        if attempts < ATTEMPT_CAP:
            continue
        ts = _terminal_ts(data) or 0
        if now - ts > days * DAY:
            emit("sticky", p,
                 f"attempts {attempts}, {_fmt_age(now - ts)} old, "
                 f"reason {str(data.get('reason') or '?')!r}",
                 "sticky error state (REPORT ONLY -- not auto-cleared; "
                 "retry with OCR_FORCE_REVIEW=1 on the next push)")


def janitor(env=None, days=DEFAULT_DAYS, verdict_days=DEFAULT_VERDICT_DAYS,
            log_bytes=DEFAULT_LOG_BYTES, dry=False, now=None, root=None):
    """Run every GC action against the store. Returns (lines, summary).

    `lines` are human-readable report lines; `summary` counts actions by
    kind. Never raises for a store problem -- the janitor reports it.
    """
    env = dict(os.environ) if env is None else dict(env)
    now = time.time() if now is None else now
    if root is None:
        root = Path(env.get("OCR_STATE_DIR", "").strip()
                    or os.path.join(os.path.expanduser("~"), ".local",
                                    "state", "review-gate"))
    root = Path(root)
    lines = []
    summary = {"clone": 0, "marker": 0, "log": 0, "verdict": 0, "sticky": 0}

    def emit(kind, path, detail, action):
        summary[kind] = summary.get(kind, 0) + 1
        prefix = "[dry-run] " if dry else ""
        lines.append(f"{prefix}{action}: {path} ({detail})")

    states = _iter_states(root)
    gc_clones(root, states, days, now, dry, emit)
    gc_markers(root, dry, emit)
    rotate_logs(states, log_bytes, dry, emit)
    gc_verdicts(root, states, verdict_days, env, now, dry, emit)
    report_sticky(states, days, now, emit)
    lines.append(f"janitor: {summary['clone']} clone(s), {summary['marker']} marker(s), "
                 f"{summary['log']} log rotation(s), {summary['verdict']} verdict action(s), "
                 f"{summary['sticky']} sticky error state(s) reported"
                 + (" -- dry run, nothing was modified" if dry else ""))
    return lines, summary


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="review-gate janitor: GC of clones, markers, logs, and verdicts "
                    "in the external state store.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, metavar="N",
                        help=f"age horizon for clones and the sticky-error report "
                             f"(default {DEFAULT_DAYS})")
    parser.add_argument("--verdict-days", type=int, default=DEFAULT_VERDICT_DAYS,
                        metavar="N",
                        help=f"age horizon for verdict pruning "
                             f"(default {DEFAULT_VERDICT_DAYS})")
    parser.add_argument("--log-bytes", type=int, default=DEFAULT_LOG_BYTES,
                        metavar="N",
                        help=f"keep the newest N bytes of each findings log "
                             f"(default {DEFAULT_LOG_BYTES})")
    parser.add_argument("--dry-run", action="store_true",
                        help="print every action without performing it")
    args = parser.parse_args(argv)

    lines, _ = janitor(days=args.days, verdict_days=args.verdict_days,
                       log_bytes=args.log_bytes, dry=args.dry_run)
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
