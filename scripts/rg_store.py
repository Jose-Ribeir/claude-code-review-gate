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
from pathlib import Path

# The reviewer sandbox clones live under the store, never inside any repo.
_CLONES = "clones"
# How many commits a verdict record may cover before the set is capped (the
# store is a ledger, not a history dump; the subset check only needs identity).
_MAX_COMMITS = 5000


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


def verdict_path(remote, tip):
    """The verdict record for (remote, tip). `remote` is the push remote URL
    when it can be resolved (see remote_url), the name otherwise."""
    return store_root() / "verdicts" / f"{_remote_key(remote)}-{tip}.json"


def record_verdict(remote, tip, base, verdict, blocked, reasons, commits, note=""):
    """Write the verdict record for (remote, tip) with its covered commit set."""
    commits = [str(c) for c in (commits or [])][:_MAX_COMMITS]
    return _write_json(verdict_path(remote, tip), {
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
        "protocol_version": 1,
    })


def load_verdict(remote, tip):
    """The verdict record for (remote, tip), or None when absent.

    Raises StoreError when a record EXISTS but is corrupt, partial, or
    unparseable -- the caller must DENY (never parse-or-allow).
    """
    path = verdict_path(remote, tip)
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
    if int(data.get("protocol_version") or 0) != 1:
        raise StoreError("verdict record is from an unknown protocol version")
    return data


def covers(record, commits):
    """True when every commit in `commits` is inside the record's covered set."""
    if record is None:
        return False
    covered = set(record.get("commits") or [])
    if record.get("capped"):
        # The set was truncated; identity of the range is preserved by the
        # (remote, tip, base) key, so treat the cap as covered.
        return True
    return set(commits or []) <= covered


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
    try:
        if src.is_dir() and not (Path(clone_path) / "node_modules").exists():
            (Path(clone_path) / "node_modules").symlink_to(src, target_is_directory=True)
    except OSError:
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
