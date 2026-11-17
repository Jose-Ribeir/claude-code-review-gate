"""Immutable, disposable repository materialization for final review.

The public context manager is deliberately transport-neutral: callers provide a
readable repository and full commit IDs, then consume a complete checkout and a
merge-base-to-head patch while the context is active. No caller working-tree
content is used, and the clone is removed on every exit path.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import uuid

import rg_store


class MaterializationError(RuntimeError):
    """A pinned review tree or its complete diff could not be produced."""


@dataclass(frozen=True)
class MaterializedReview:
    """The complete evidence available for one immutable review unit."""

    path: Path
    target_sha: str
    head_sha: str
    merge_base_sha: str
    diff: str
    submodules: tuple = ()
    lfs_files: tuple = ()
    lfs_complete: bool = True


def _git(repo, *args, input_bytes=None):
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(repo), input=input_bytes,
            capture_output=True, timeout=300, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MaterializationError(f"git {args[0]} failed: {exc}") from exc
    if proc.returncode:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise MaterializationError(
            f"git {args[0]} failed with exit code {proc.returncode}: {detail}"
        )
    return proc.stdout


def _recursive_submodules(path):
    _git(path, "submodule", "update", "--init", "--recursive", "--force",
         "--checkout")
    output = _git(path, "submodule", "status", "--recursive").decode(
        "utf-8", "surrogateescape")
    paths = []
    for line in output.splitlines():
        if not line:
            continue
        if line[0] in "-+U":
            raise MaterializationError(f"submodule is not at its pinned commit: {line}")
        fields = line[1:].split(None, 2)
        if len(fields) < 2:
            raise MaterializationError(f"could not parse submodule status: {line}")
        paths.append(fields[1])
    return tuple(paths)


def _lfs_files_in(repo):
    listing = _git(repo, "ls-files", "-z")
    if not listing:
        return ()
    attrs = _git(repo, "check-attr", "-z", "filter", "--stdin",
                 input_bytes=listing)
    fields = attrs.split(b"\0")
    files = []
    for index in range(0, len(fields) - 2, 3):
        if fields[index + 1] == b"filter" and fields[index + 2] == b"lfs":
            files.append(fields[index].decode("utf-8", "surrogateescape"))
    return tuple(files)


def _materialize_lfs(path, submodules, required):
    roots = [("", path)] + [(relative, path / relative) for relative in submodules]
    all_files = []
    for prefix, root in roots:
        files = _lfs_files_in(root)
        if required and files:
            _git(root, "lfs", "pull")
            unresolved = []
            for relative in files:
                content = (root / relative).read_bytes()
                if (content.startswith(b"version https://git-lfs.github.com/spec/v1\n")
                        and b"\noid sha256:" in content):
                    unresolved.append(relative)
            if unresolved:
                raise MaterializationError(
                    "required Git LFS content is still unresolved: "
                    + ", ".join(unresolved)
                )
        all_files.extend(f"{prefix}/{name}" if prefix else name for name in files)
    return tuple(all_files), bool(required or not all_files)


def _pinned_sha(value, label):
    sha = str(value).lower()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha):
        raise MaterializationError(f"{label} must be a full immutable commit SHA")
    return sha


@contextmanager
def materialize_review(repository, target_sha, head_sha, *, target_repository=None,
                       require_lfs=False):
    """Yield a clean detached clone at ``head_sha`` and its complete patch.

    ``target_sha`` and ``head_sha`` must be full commit IDs. ``diff`` is a
    binary-capable patch from their merge base to ``head_sha``; in particular,
    target-only changes are not treated as source removals. For a fork, pass
    ``target_repository`` when its target object is absent from the source.
    LFS paths are always disclosed. Set ``require_lfs`` to fetch their content
    recursively or receive ``MaterializationError`` rather than a pointer-only
    tree. ``lfs_complete`` records whether required LFS retrieval was requested.
    """
    target = _pinned_sha(target_sha, "target_sha")
    head = _pinned_sha(head_sha, "head_sha")
    run_id = f"final-{uuid.uuid4().hex}"
    clone_path = rg_store.clones_dir() / f"{head[:12]}-{run_id}"
    try:
        made = rg_store.make_review_clone(str(repository), head, run_id)
        if not made:
            raise MaterializationError(
                f"could not clone and check out immutable head {head}"
            )
        path = Path(made)
        # The pre-push clone convenience symlinks local node_modules and scrubs
        # tracked tsconfig files. Final evidence permits neither alteration.
        node_modules = path / "node_modules"
        if node_modules.is_symlink():
            node_modules.unlink()
        _git(path, "reset", "--hard", "--quiet", head)
        _git(path, "clean", "-ffdx", "--quiet")
        actual = _git(path, "rev-parse", "HEAD").decode("ascii").strip()
        if actual != head:
            raise MaterializationError(
                f"materialized head {actual} does not match requested {head}"
            )
        if target_repository is not None:
            _git(path, "fetch", "--no-tags", "--quiet", str(target_repository), target)
        submodules = _recursive_submodules(path)
        lfs_files, lfs_complete = _materialize_lfs(path, submodules, require_lfs)
        merge_base = _git(path, "merge-base", target, head).decode("ascii").strip()
        patch = _git(
            path, "diff", "--no-ext-diff", "--no-textconv", "--full-index",
            "--binary", merge_base, head,
        ).decode("utf-8", "surrogateescape")
        review = MaterializedReview(
            path, target, head, merge_base, patch, submodules=submodules,
            lfs_files=lfs_files, lfs_complete=lfs_complete,
        )
    except Exception:
        rg_store.remove_review_clone(str(clone_path))
        raise

    try:
        yield review
    finally:
        rg_store.remove_review_clone(str(clone_path))
