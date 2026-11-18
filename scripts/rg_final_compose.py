"""Ordered, disposable combined and prospective final-review composition.

The input is the complete graph validated by :mod:`rg_final_graph`; this module
never discovers, adds, removes, or reorders provider items. Each pinned
merge-base patch is applied base-most first in a detached worktree. Conflicts
are evidence, not an invitation to choose a side. A combined tree must exactly
reproduce the final pinned source tree. Prospective integration receives an
evidence-only commit whose parent is the current root target.

``compose_stack`` is a context manager yielding JSON-compatible dictionaries.
Its ``combined`` and ``prospective`` entries each contain ``complete``,
``errors``, ``applied_item_keys``, and (on success) ``repository`` and ``diff``.
All repository paths are valid only inside the context. This is the hand-off to
later verification/publication layers; it does not calculate a review verdict.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import subprocess
import tempfile

import rg_final_graph


COMPOSITION_SCHEMA = "final-review-composition/v1"
_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")


class CompositionError(RuntimeError):
    """A composition scope could not produce a trustworthy tree."""

    def __init__(self, message, *, code="composition-unreproducible", details=None):
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


class CompositionConflict(CompositionError):
    """A pinned patch conflicts; no conflict may be resolved automatically."""

    def __init__(self, message, *, details=None):
        super().__init__(message, code="composition-conflict", details=details)


def _git(repo, *args, input_bytes=None, env=None, check=True):
    try:
        process = subprocess.run(
            ["git", *args], cwd=str(repo), input=input_bytes,
            capture_output=True, timeout=300, check=False, env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CompositionError(f"git {args[0]} could not run: {exc}") from exc
    if check and process.returncode:
        detail = process.stderr.decode("utf-8", "replace").strip()
        raise CompositionError(
            f"git {args[0]} failed with exit code {process.returncode}: {detail}",
            details={"exit_code": process.returncode, "stderr": detail},
        )
    return process


def _output(repo, *args):
    return _git(repo, *args).stdout.decode("utf-8", "surrogateescape").strip()


def _sha(value, label):
    text = str(value or "")
    if not _FULL_SHA.fullmatch(text):
        raise ValueError(f"{label} must be a full 40-character commit SHA")
    return text.lower()


def _repository_url(side):
    return str(side.get("http_url") or side.get("url") or "").strip()


def _branch_coordinate(side):
    return side.get("project_id"), side.get("branch")


def _validate_graph(graph):
    if not isinstance(graph, dict) or graph.get("schema") != rg_final_graph.GRAPH_SCHEMA:
        raise ValueError("composition requires a validated final-review-graph/v1 graph")
    items = graph.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("composition requires at least one validated item")
    keys = [item.get("id") for item in items]
    if graph.get("item_keys") != keys or len(set(keys)) != len(keys) \
            or any(not isinstance(key, str) or not key for key in keys):
        raise ValueError("composition item identity does not match the validated graph")
    root = graph.get("root_target")
    if not isinstance(root, dict):
        raise ValueError("composition requires a pinned root target")
    _sha(root.get("sha"), "root target sha")
    if not _repository_url(root):
        raise ValueError("composition requires a readable root-target repository")
    previous = None
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("source"), dict) \
                or not isinstance(item.get("target"), dict):
            raise ValueError("composition items must retain provider source/target metadata")
        _sha(item["source"].get("sha"), "source sha")
        _sha(item["target"].get("sha"), "target sha")
        if not _repository_url(item["source"]):
            raise ValueError("composition requires a readable source repository")
        expected_target = root if previous is None else previous["source"]
        if _branch_coordinate(item["target"]) != _branch_coordinate(expected_target):
            raise ValueError(
                "composition cannot join mismatched project coordinates or reorder "
                "a non-linear provider graph")
        previous = item
    return items, root


def _unit(kind, target_sha, head_sha):
    return {
        "kind": kind,
        "complete": False,
        "target_sha": target_sha,
        "head_sha": head_sha,
        "merge_base_sha": target_sha,
        "repository": None,
        "diff": "",
        "applied_item_keys": [],
        "errors": [],
    }


def _record_error(unit, error, item_key=None):
    details = dict(getattr(error, "details", {}) or {})
    if item_key is not None:
        details["item_key"] = item_key
    unit["errors"].append({
        "code": getattr(error, "code", "composition-unreproducible"),
        "message": str(error),
        "details": details,
    })


def _ensure_commit(repository, url, sha):
    if _git(repository, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode:
        _git(repository, "fetch", "--no-tags", "--quiet", url, sha)
    actual = _output(repository, "rev-parse", f"{sha}^{{commit}}")
    if actual != sha:
        raise CompositionError(
            f"repository resolved pinned commit {sha} as {actual}",
            details={"expected": sha, "actual": actual},
        )


def _prepare_repositories(temp, root, items, current_root_sha):
    repository = temp / "repository"
    _git(temp, "clone", "--no-hardlinks", "--quiet", "--no-checkout",
         _repository_url(root), str(repository))
    _ensure_commit(repository, _repository_url(root), root["sha"])
    _ensure_commit(repository, _repository_url(root), current_root_sha)
    for item in items:
        source = item["source"]
        target = item["target"]
        _ensure_commit(repository, _repository_url(source), source["sha"])
        _ensure_commit(repository, _repository_url(target), target["sha"])
    combined = temp / "combined"
    prospective = temp / "prospective"
    _git(repository, "worktree", "add", "--quiet", "--detach",
         str(combined), root["sha"])
    _git(repository, "worktree", "add", "--quiet", "--detach",
         str(prospective), current_root_sha)
    return repository, combined, prospective


def _patch(repository, item):
    source = item["source"]
    target = item["target"]
    merge_base = _output(repository, "merge-base", target["sha"], source["sha"])
    provider_base = target.get("base_sha")
    if provider_base and str(provider_base).lower() != merge_base:
        raise CompositionError(
            f"provider merge base {provider_base} does not reproduce as {merge_base}",
            details={"expected": str(provider_base).lower(), "actual": merge_base},
        )
    return _git(
        repository, "diff", "--no-ext-diff", "--no-textconv", "--full-index",
        "--binary", merge_base, source["sha"],
    ).stdout


def _apply_patch(worktree, patch, item):
    process = _git(
        worktree, "apply", "--3way", "--index", "--whitespace=nowarn", "-",
        input_bytes=patch, check=False,
    )
    if process.returncode:
        status = _output(worktree, "status", "--porcelain=v1", "--untracked-files=all")
        _git(worktree, "apply", "--abort", check=False)
        detail = process.stderr.decode("utf-8", "replace").strip()
        raise CompositionConflict(
            f"{item['id']} could not be composed without conflict: {detail}",
            details={"item_key": item["id"], "stderr": detail, "status": status},
        )


def _compose_patches(repository, worktree, items, unit):
    for item in items:
        try:
            patch = _patch(repository, item)
            _apply_patch(worktree, patch, item)
        except CompositionError as exc:
            _record_error(unit, exc, item["id"])
            return False
        unit["applied_item_keys"].append(item["id"])
    return True


def _clean_status(worktree):
    status = _output(worktree, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise CompositionError(
            "composed worktree differs from its recorded commit",
            details={"status": status},
        )


def _finish_combined(repository, worktree, items, unit):
    expected_head = items[-1]["source"]["sha"]
    reproduced_base = _output(repository, "merge-base", unit["target_sha"], expected_head)
    if reproduced_base != unit["target_sha"]:
        raise CompositionError(
            "the final pinned head is not based on the composed root target",
            details={"expected_merge_base": unit["target_sha"],
                     "actual_merge_base": reproduced_base},
        )
    tree = _output(worktree, "write-tree")
    expected_tree = _output(repository, "rev-parse", f"{expected_head}^{{tree}}")
    if tree != expected_tree:
        raise CompositionError(
            "ordered patches do not reproduce the final pinned source tree",
            details={"expected_tree": expected_tree, "actual_tree": tree,
                     "expected_head": expected_head},
        )
    _git(worktree, "reset", "--soft", "--quiet", expected_head)
    _clean_status(worktree)
    unit["complete"] = True
    unit["head_sha"] = expected_head
    unit["repository"] = {
        "path": str(worktree), "access": "full_tree", "head_sha": expected_head,
        "target_sha": unit["target_sha"], "merge_base_sha": unit["target_sha"],
        "submodules": [], "lfs_files": [], "lfs_complete": False,
        "source_repository": _output(worktree, "rev-parse", "--git-common-dir"),
    }
    unit["diff"] = _git(
        repository, "diff", "--no-ext-diff", "--no-textconv", "--full-index",
        "--binary", unit["target_sha"], expected_head,
    ).stdout.decode("utf-8", "surrogateescape")


def _finish_prospective(repository, worktree, items, unit):
    tree = _output(worktree, "write-tree")
    environment = os.environ.copy()
    for prefix in ("GIT_AUTHOR_", "GIT_COMMITTER_"):
        environment[prefix + "NAME"] = "review-gate final composition"
        environment[prefix + "EMAIL"] = "review-gate@localhost"
        environment[prefix + "DATE"] = "2000-01-01T00:00:00Z"
    message = (
        "Final-review prospective integration\n\n"
        f"Current root target: {unit['target_sha']}\n"
        "Base-most items: " + ", ".join(item["id"] for item in items) + "\n"
    )
    head = _git(
        repository, "commit-tree", tree, "-p", unit["target_sha"],
        input_bytes=message.encode("utf-8"), env=environment,
    ).stdout.decode("ascii").strip()
    _git(worktree, "reset", "--soft", "--quiet", head)
    _clean_status(worktree)
    unit["complete"] = True
    unit["head_sha"] = head
    unit["repository"] = {
        "path": str(worktree), "access": "full_tree", "head_sha": head,
        "target_sha": unit["target_sha"], "merge_base_sha": unit["target_sha"],
        "submodules": [], "lfs_files": [], "lfs_complete": False,
        "evidence_only_commit": True,
        "source_repository": _output(worktree, "rev-parse", "--git-common-dir"),
    }
    unit["diff"] = _git(
        repository, "diff", "--no-ext-diff", "--no-textconv", "--full-index",
        "--binary", unit["target_sha"], head,
    ).stdout.decode("utf-8", "surrogateescape")


def _finish_scope(repository, worktree, items, unit):
    if not _compose_patches(repository, worktree, items, unit):
        return
    try:
        if unit["kind"] == "combined":
            _finish_combined(repository, worktree, items, unit)
        else:
            _finish_prospective(repository, worktree, items, unit)
    except CompositionError as exc:
        _record_error(unit, exc)
        unit["repository"] = None
        unit["diff"] = ""


@contextmanager
def compose_stack(graph, *, current_root_sha=None):
    """Compose one validated graph and yield scoped evidence until cleanup.

    ``current_root_sha`` is pinned by the trusted supervisor immediately before
    calling this function. It defaults to the graph's root SHA, making combined
    and prospective views distinct even when their trees initially coincide.
    Setup failures and ordinary Git failures are returned as incomplete scopes;
    invalid graph inputs raise ``ValueError`` before any tree is reviewed.
    """
    items, root = _validate_graph(graph)
    pinned_root_sha = _sha(root["sha"], "root target sha")
    current_root_sha = _sha(current_root_sha or pinned_root_sha, "current root sha")
    final_head = items[-1]["source"]["sha"]
    result = {
        "schema": COMPOSITION_SCHEMA,
        "complete": False,
        "root_target_sha": pinned_root_sha,
        "current_root_sha": current_root_sha,
        "item_keys": [item["id"] for item in items],
        "combined": _unit("combined", pinned_root_sha, final_head),
        "prospective": _unit("prospective", current_root_sha, final_head),
    }
    with tempfile.TemporaryDirectory(prefix="review-gate-final-compose-") as directory:
        temp = Path(directory)
        repository = None
        worktrees = []
        try:
            repository, combined_path, prospective_path = _prepare_repositories(
                temp, root, items, current_root_sha)
            worktrees = [combined_path, prospective_path]
            _finish_scope(repository, combined_path, items, result["combined"])
            _finish_scope(repository, prospective_path, items, result["prospective"])
        except CompositionError as exc:
            for unit in (result["combined"], result["prospective"]):
                if not unit["errors"]:
                    _record_error(unit, exc)
                unit["repository"] = None
                unit["diff"] = ""
        result["complete"] = all(
            result[kind]["complete"] for kind in ("combined", "prospective"))
        result["errors"] = [
            {"scope": kind, **error}
            for kind in ("combined", "prospective")
            for error in result[kind]["errors"]
        ]
        try:
            yield result
        finally:
            if repository is not None:
                for worktree in reversed(worktrees):
                    _git(repository, "worktree", "remove", "--force", str(worktree),
                         check=False)
