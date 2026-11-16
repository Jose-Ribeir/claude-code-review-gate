"""External verdict store + reviewer sandbox for the Kimchi review-gate port.

D12 of the port plan: the verdict store lives OUTSIDE any repository, at
~/.local/state/review-gate/ (0600 files, 0700 dirs, supervisor-written only),
keyed (remote, tip) plus the covered commit set. A repository -- whose CONTENT
is the untrusted input under review -- cannot write there, so it cannot
pre-plant a clean verdict for itself.

Corrupt, partial, or unparseable verdict JSON raises StoreError; the caller
DENIES. Never parse-or-allow.

This module also owns the reviewer sandbox: `git clone --no-hardlinks` of the
push tip into a scratch dir under the store (a clone has its own refs, config,
and object database, and copies no hooks -- unlike a git worktree, which
shares all three with the real repository). For TypeScript work,
node_modules is symlinked in (a read surface for the reviewer, never a write
surface) and `plugins` keys are scrubbed from tsconfig.json files in the copy.

`OCR_STATE_DIR` overrides the root (tests must never write into the real
~/.local/state).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# The reviewer sandbox clones live under the store, never inside any repo.
_CLONES = "clones"
# How many commits a verdict record may cover before the set is capped (the
# store is a ledger, not a history dump; the subset check only needs identity).
_MAX_COMMITS = 5000

# Ledger hygiene (push-gate spec, "Ledger hygiene" decision): verdicts are
# time-boxed to 90 days so a stale entry can never masquerade as coverage for
# a push nobody reviewed; reviewer sandboxes rot out after a week; and both
# are pruned lazily (at most once a day) so neither the store nor the run cost
# grows with use. A ref-deletion push invalidates that ref's verdicts
# immediately (invalidate_ref); every bypass is loud by construction (the
# append-only bypasses.jsonl audit trail).
VERDICT_TTL_SECONDS = 90 * 24 * 3600
_CLONE_TTL_SECONDS = 7 * 24 * 3600
_PRUNE_INTERVAL_SECONDS = 24 * 3600
_PRUNE_MARKER = ".last-prune"
_BYPASS_LOG = "bypasses.jsonl"


def store_root():
    """The store root, created with 0700 (best-effort) on first use."""
    root = Path(os.environ.get("OCR_STATE_DIR", "").strip()
                or os.path.join(os.path.expanduser("~"), ".local", "state", "review-gate"))
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return root


class StoreError(Exception):
    """A verdict record exists but cannot be trusted: corrupt/partial/unparseable."""


def _write_json(path, data):
    """Atomic replace with 0600 permissions (verdicts are supervisor-written only)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    os.chmod(tmp, 0o600)
    last = None
    for _ in range(40):
        try:
            os.replace(str(tmp), str(path))
            return True
        except PermissionError as exc:
            last = exc
            import time
            time.sleep(0.05)
    try:
        tmp.unlink()
    except OSError:
        pass
    raise last if last else OSError("could not write verdict record")


def _remote_key(remote):
    return hashlib.sha256(str(remote or "").encode("utf-8", "replace")).hexdigest()[:12]


_HEX40 = re.compile(r"^[0-9a-f]{40}$")


def _dedup_strs(values):
    """A coverage list as the store writes it: str-only, non-empty, sorted, deduped.

    A stray non-str entry is dropped rather than failing the write (coverage
    is a join accelerator, never a reason to lose the verdict itself).
    """
    return sorted({str(v) for v in (values or []) if isinstance(v, str) and v})


def verdict_path(remote, tip):
    """The verdict record for (remote, tip). `remote` is the push remote URL
    when it can be resolved (see remote_url), the name otherwise."""
    return store_root() / "verdicts" / f"{_remote_key(remote)}-{tip}.json"


def record_verdict(remote, tip, base, verdict, blocked, reasons, commits, note="",
                   intent=None, coverage=None, branch="", content_keyed=True):
    """Write the verdict record for (remote, tip) with its covered commit set.

    `intent` is an optional extras dict recording the intent context the
    review actually used (sources present, the intent text, counts, fetch
    notes) -- provenance for a later before/after quality study. The store
    tolerates extra keys at protocol_version 1, so this adds NO version bump,
    and load_verdict tolerates its absence in older records.

    `coverage` (restack-a, protocol_version 2) keys the record by CONTENT as
    well as SHA: the tip's tree hash and the stable patch-id of every commit
    in the range, so a restack that rewrites every SHA but no content still
    joins this verdict. `content_keyed=False` writes the exact v1 shape
    (OCR_COVERAGE_V1: the rollback switch).
    """
    commits = [str(c) for c in (commits or [])][:_MAX_COMMITS]
    record = {
        "remote": str(remote or ""),
        "tip": str(tip or ""),
        "base": str(base or ""),
        "verdict": str(verdict or ""),
        "blocked": bool(blocked),
        "reasons": str(reasons or ""),
        "commits": commits,
        "capped": len(commits) >= _MAX_COMMITS,
        "note": str(note or ""),
        "ts": _now(),
        "recorded_at": _now(),
        "protocol_version": 1,
    }
    if content_keyed:
        cov = coverage if isinstance(coverage, dict) else {}
        tip_tree = str(cov.get("tip_tree") or "")
        if not _HEX40.match(tip_tree):
            tip_tree = ""      # anything not a real tree hash fails to "no tree"
        record["protocol_version"] = 2
        record["tip_tree"] = tip_tree
        record["patch_ids"] = _dedup_strs(cov.get("patch_ids"))
        record["sha_only"] = _dedup_strs(cov.get("sha_only"))
        record["branch"] = str(branch or "")
    if intent is not None:
        # An extras dict, copied so a later caller-side mutation cannot reach
        # into the record. A stray non-dict value is kept as text rather than
        # dropped (an extra is never a reason to fail a verdict write).
        record["intent"] = dict(intent) if isinstance(intent, dict) else {"text": str(intent)}
    return _write_json(verdict_path(remote, tip), record)


def load_verdict(remote, tip):
    """The verdict record for (remote, tip), or None when absent.

    Raises StoreError when a record EXISTS but is corrupt, partial, or
    unparseable -- the caller must DENY (never parse-or-allow).
    """
    return _load_verdict_at(verdict_path(remote, tip))


def _load_verdict_at(path):
    """load_verdict for an explicit record path (records_for_remote scans
    the store by remote key, so it cannot go through (remote, tip))."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StoreError(f"verdict record unreadable ({exc})") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise StoreError("verdict record is not parseable JSON") from exc
    if not isinstance(data, dict):
        raise StoreError("verdict record is not a JSON object")
    # Partial writes are guarded by the atomic replace, but a record that
    # claims to be a verdict without the fields the join depends on is
    # corrupt-by-definition, not a pass.
    for field in ("remote", "tip", "verdict", "commits"):
        if field not in data:
            raise StoreError(f"verdict record is partial (missing {field!r})")
    if not isinstance(data["commits"], list):
        raise StoreError("verdict record has a malformed covered commit set")
    try:
        version = int(data.get("protocol_version") or 0)
    except (TypeError, ValueError):
        raise StoreError("verdict record is from an unknown protocol version") from None
    if version not in (1, 2):
        raise StoreError("verdict record is from an unknown protocol version")
    if version == 2:
        # v2 adds the content-keyed coverage the restack join depends on. A
        # record that claims v2 without it is corrupt by definition, exactly
        # like a record missing `commits`: it cannot vouch for what it never
        # recorded, so it must DENY rather than degrade to a SHA-only join.
        for field in ("tip_tree", "patch_ids", "sha_only"):
            if field not in data:
                raise StoreError(f"verdict record is partial (missing {field!r})")
        if not isinstance(data["tip_tree"], str) \
                or not isinstance(data["patch_ids"], list) \
                or not isinstance(data["sha_only"], list):
            raise StoreError("verdict record has malformed coverage fields")
    return data


def records_for_remote(remote):
    """Every verdict record for `remote` (any tip), skipping corrupt ones.

    The restack join scans the whole ledger for the remote rather than the
    one (remote, tip) slot a re-push lands in. A corrupt record authorizes
    nothing (the direct lookup DENIES), but it must not blind the scan to
    the usable records beside it -- so it is skipped, not fatal.
    """
    out = []
    try:
        prefix = _remote_key(remote) + "-"
        for path in sorted((store_root() / "verdicts").glob(prefix + "*.json")):
            try:
                rec = _load_verdict_at(path)
            except StoreError:
                continue
            # A record that vanished between the glob and the read (concurrent
            # remove_verdict / in-flight replace / TTL prune; the store is
            # machine-global) is FileNotFoundError -> None, not StoreError:
            # skipping it here keeps None out of the join's record list.
            if rec is not None:
                out.append(rec)
    except Exception:
        return []
    return out


def covers(record, commits):
    """True when every commit in `commits` is inside the record's covered set.

    A capped record's set was truncated at the newest _MAX_COMMITS commits,
    so it vouches for exactly the commits it lists -- never for older ones
    the cap cut off (A4: no vacuous authorizations). A record that is not
    capped lists every commit its review saw, and the subset check is the
    whole contract.
    """
    if record is None:
        return False
    return set(commits or []) <= set(record.get("commits") or [])


def remove_verdict(remote, tip):
    """Delete the verdict record for (remote, tip), if present. Used when a
    record exists but does not cover the push being made (A4): it authorizes
    nothing, so it is dropped and the review restarts rather than replaying."""
    try:
        os.unlink(verdict_path(remote, tip))
    except FileNotFoundError:
        pass


def remote_url(repo_root, remote):
    """The remote's URL (the stable identity of a push target), or the name."""
    remote = str(remote or "").strip()
    if not remote:
        return ""
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", remote], cwd=repo_root or None,
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return remote


def _now():
    import time
    return time.time()


# --- the reviewer sandbox (D12: clone, never a worktree) ----------------------

def async_dir(common_dir):
    """The per-repo async run-state dir, under the EXTERNAL store (D12), keyed
    by the canonical git common dir. Never inside the repository: the repo's
    own .git is content the review cannot be trusted to have written."""
    key = hashlib.sha256(str(common_dir or "").encode("utf-8", "replace")).hexdigest()[:16]
    d = store_root() / "async" / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def clones_dir():
    d = store_root() / _CLONES
    d.mkdir(parents=True, exist_ok=True)
    return d


def make_review_clone(repo_root, tip, run_id):
    """A `git clone --no-hardlinks` of `tip` for the reviewer to read, or "".

    A clone carries its own refs, config, hooks, and a copied object database:
    nothing in it can move the real repository's refs or fire its hooks (which
    a worktree shares, making verdict forgery and hook RCE reachable from the
    reviewed content). node_modules is symlinked in for TS repos and `plugins`
    keys are scrubbed from tsconfig.json files in the copy.
    """
    try:
        path = clones_dir() / f"{tip[:12]}-{run_id}"
        out = subprocess.run(
            ["git", "clone", "--no-hardlinks", "--quiet", "--no-checkout",
             str(repo_root), str(path)],
            capture_output=True, text=True, timeout=300,
        )
        if out.returncode != 0 or not path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            return ""
        co = subprocess.run(
            ["git", "-C", str(path), "checkout", "--quiet", "--detach", tip],
            capture_output=True, text=True, timeout=120,
        )
        if co.returncode != 0:
            shutil.rmtree(path, ignore_errors=True)
            return ""
        _symlink_node_modules(repo_root, path)
        _scrub_tsconfig_plugins(path)
        return str(path)
    except Exception:
        return ""


def _symlink_node_modules(repo_root, clone_path):
    """node_modules is the reviewer's read surface, never its write surface."""
    src = Path(repo_root) / "node_modules"
    dst = Path(clone_path) / "node_modules"
    try:
        if src.is_dir() and not dst.exists():
            try:
                dst.symlink_to(src, target_is_directory=True)
            except OSError:
                if os.name != "nt":
                    raise
                # Windows: a symlink needs a privilege most accounts lack; a
                # directory junction needs none and is the same read surface.
                import _winapi
                _winapi.CreateJunction(str(src), str(dst))
    except Exception:
        pass


_TSCONFIG = re.compile(r"^tsconfig[^/]*\.json$|/tsconfig[^/]*\.json$")


def _scrub_tsconfig_plugins(clone_path):
    """Remove `plugins` keys from tsconfig.json files IN THE COPY.

    tsserver plugins execute arbitrary code at review time; the scrubbed copy
    is the only tree the reviewer reads (D12/D6 of the port plan).
    """
    for root, dirs, files in os.walk(clone_path):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules")]
        for name in files:
            if not _TSCONFIG.search("/" + name):
                continue
            p = Path(root) / name
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or "plugins" not in data:
                continue
            data.pop("plugins", None)
            try:
                p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
            except OSError:
                pass


def is_gate_clone(path):
    """True only for a directory this module created: under the store's own
    clones/ dir. Callers use this to decide whether a destructive teardown may
    run there."""
    try:
        base = os.path.realpath(str(clones_dir()))
        real = os.path.realpath(str(path))
        return real.startswith(base + os.sep)
    except Exception:
        return False


def remove_review_clone(path):
    """Tear down a clone make_review_clone created. Refuses anything else."""
    try:
        if not is_gate_clone(path):
            return
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def record_commits(repo_root, base, tip):
    """The commit set a review of `base..tip` covers (bounded, newest first)."""
    rng = tip if (not base or base == "4b825dc642cb6eb9a060e54bf8d69288fbee4904") else f"{base}..{tip}"
    try:
        out = subprocess.run(
            ["git", "rev-list", "--max-count", str(_MAX_COMMITS), rng],
            cwd=repo_root or None, capture_output=True, text=True, timeout=120,
        )
        if out.returncode == 0:
            return out.stdout.split()
    except Exception:
        pass
    return []


# --- ledger hygiene: TTL, lazy prune, ref invalidation, bypass audit ---------

def _warn(msg):
    """Every hygiene failure degrades to a printed warning: the ledger is an
    audit aid, and a store problem must never raise into the gate path (nor
    be mistaken for a verdict)."""
    try:
        print(f"rg_store: warning: {msg}", file=sys.stderr)
    except Exception:
        pass


def _rmtree_best_effort(path):
    """Remove a sandbox tree, tolerating git's read-only object files (which
    Windows refuses to unlink without an S_IWRITE chmod first). Never raises."""
    import stat

    def _fix(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
        except OSError:
            return
        try:
            func(p)
        except OSError:
            pass

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(str(path), onexc=_fix)
        else:
            shutil.rmtree(str(path), onerror=_fix)
    except Exception:
        pass


def prune(state_dir=None):
    """Enforce ledger hygiene; returns {"verdicts_removed", "clones_removed"}.

    A verdict older than VERDICT_TTL_SECONDS is deleted: 90 days after a
    review, keeping the entry would only masquerade as coverage for content
    nobody has looked at in months (a re-push is cheap; a stale pass is not).
    Age is `recorded_at`; a legacy entry without one is treated as freshly
    important -- kept, and stamped on this write, so one prune sweeps the
    whole ledger into the new shape without deleting real coverage.

    Reviewer sandboxes under clones/ older than a week are removed (they are
    scratch space: a clone is one command away from being rebuilt).

    A corrupt or unreadable record is warned about and LEFT IN PLACE -- it
    already authorizes nothing (the direct lookup DENIES on it) and deleting
    anything the store could not parse would be laundering evidence. Every
    failure is a warning, never an exception.
    """
    counts = {"verdicts_removed": 0, "clones_removed": 0}
    try:
        root = Path(state_dir) if state_dir else store_root()
        now = _now()
    except Exception as exc:
        _warn(f"ledger prune skipped: store root unavailable ({exc})")
        return counts
    try:
        paths = sorted((root / "verdicts").glob("*.json"))
    except Exception as exc:
        _warn(f"ledger prune could not list verdicts ({exc})")
        paths = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            _warn(f"verdict record {path.name} unreadable ({exc}); left in place")
            continue
        if not isinstance(data, dict):
            _warn(f"verdict record {path.name} is not a JSON object; left in place")
            continue
        try:
            stamped = float(data.get("recorded_at"))
        except (TypeError, ValueError):
            stamped = None
        if stamped is None:
            data["recorded_at"] = now
            try:
                _write_json(path, data)
            except Exception as exc:
                _warn(f"could not stamp legacy verdict {path.name} ({exc})")
            continue
        if now - stamped > VERDICT_TTL_SECONDS:
            try:
                path.unlink()
                counts["verdicts_removed"] += 1
            except OSError as exc:
                _warn(f"could not remove expired verdict {path.name} ({exc})")
    try:
        clones = [
            p for p in (root / _CLONES).iterdir() if p.is_dir()
        ] if (root / _CLONES).is_dir() else []
    except OSError as exc:
        _warn(f"ledger prune could not list sandboxes ({exc})")
        clones = []
    for d in clones:
        try:
            age = now - d.stat().st_mtime
        except OSError as exc:
            _warn(f"could not stat sandbox {d.name} ({exc})")
            continue
        if age > _CLONE_TTL_SECONDS:
            _rmtree_best_effort(d)
            if not d.exists():
                counts["clones_removed"] += 1
            else:
                _warn(f"could not remove expired sandbox {d.name}")
    return counts


def maybe_prune(state_dir=None):
    """Run prune at most once per day (lazily scheduled on gate invocations).

    The marker file inside the store root holds the last run's timestamp; a
    fresh marker short-circuits. Returns the prune counts dict when a prune
    ran, None when it did not (marker fresh) or could not (warning emitted).
    """
    try:
        root = Path(state_dir) if state_dir else store_root()
        marker = root / _PRUNE_MARKER
        now = _now()
        last = None
        try:
            last = float(marker.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            try:
                last = marker.stat().st_mtime
            except OSError:
                last = None
        if last is not None and now - last < _PRUNE_INTERVAL_SECONDS:
            return None
        counts = prune(root)
        try:
            marker.write_text(str(now), encoding="utf-8")
        except OSError as exc:
            _warn(f"could not mark prune run ({exc}); next call will re-prune")
        return counts
    except Exception as exc:
        _warn(f"lazy prune skipped ({exc})")
        return None


def invalidate_ref(remote, ref):
    """Delete every verdict entry for (remote, `ref`); returns the count.

    Used for `git push --delete`: a verdict blesses a tip on a ref, and once
    the ref is gone the coverage it claimed belongs to history. Records are
    matched on the `branch` field recorded at protocol_version 2; records
    without it vouch for nothing by ref and are left alone. Corrupt records
    are warnings, never exceptions, and are skipped (left in place DENYING).

    The ref arrives in either spelling -- full (`refs/heads/x`, git mode) or
    short (`x`, the hook parser) -- and both belong to the same ledger key
    space, so the short/full juggling lives HERE: one call sweeps records
    written under either spelling of the same branch."""
    removed = 0
    try:
        ref = str(ref or "")
        short = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        forms = {ref, short, "refs/heads/" + short} if short else {ref}
        prefix = _remote_key(remote) + "-"
        paths = sorted((store_root() / "verdicts").glob(prefix + "*.json"))
    except Exception as exc:
        _warn(f"invalidate_ref could not scan the ledger ({exc})")
        return 0
    for path in paths:
        try:
            rec = _load_verdict_at(path)
        except StoreError as exc:
            _warn(f"verdict record {path.name} skipped during ref invalidation ({exc})")
            continue
        if rec is None or rec.get("branch") not in forms:
            continue
        try:
            os.unlink(path)
            removed += 1
        except OSError as exc:
            _warn(f"could not delete verdict {path.name} for {ref} ({exc})")
    return removed


def record_bypass(remote, ref, tip, mechanism, reason):
    """Append one audited bypass record to bypasses.jsonl in the store root.

    The escape hatches (mechanism is "OCR_FAIL_OPEN" or "review.failOpen")
    are deliberate and loud by construction: this append-only log is the
    audit trail that tells a bypass apart from an unnoticed hole. (`git push
    --no-verify` never invokes the hook at all -- it leaves no record here,
    by design; the forge's own audit log is the only evidence of that one.) A write failure
    warns rather than raising -- it must never block the gate an env var
    already told to open -- and then the bypass is NOT silent: the warning
    itself names it.
    """
    entry = {
        "remote": str(remote or ""),
        "ref": str(ref or ""),
        "tip": str(tip or ""),
        "mechanism": str(mechanism or ""),
        "reason": str(reason or ""),
        "ts": _now(),
    }
    try:
        path = store_root() / _BYPASS_LOG
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception as exc:
        _warn(f"could not record bypass ({mechanism}) for "
              f"({remote}, {ref}, {tip}): {exc}")
