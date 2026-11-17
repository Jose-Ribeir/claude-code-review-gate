"""Searchable complete-repository context for explicit final review.

The merge-base patch identifies what changed; it is deliberately not the
context boundary.  This module indexes the pinned checkout (including
unchanged repository knowledge) and returns paths plus acquisition metadata,
not serialized source files.  Reviewers can therefore search for callers,
read implementation/tests, and consult decisions and domain language on
demand without receiving an invented call graph or a second copy of the tree.
"""
from pathlib import Path
import subprocess

import rg_ticket_fetch


CONTRACT_VERSION = 1
REVIEWER_INSTRUCTIONS = (
    "Search and read the complete pinned checkout, including unchanged files, "
    "callers, implementation, tests, instructions, standards, ADRs and domain "
    "documents. The merge-base diff scopes findings, not the available context; "
    "record only dependencies established from code or provider metadata."
)


class ContextCompletenessError(RuntimeError):
    """Required repository evidence is absent from the materialized tree."""


def _git(root, *args):
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True,
            timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContextCompletenessError(f"cannot inspect complete repository: {exc}") from exc
    if proc.returncode:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise ContextCompletenessError(
            f"cannot inspect complete repository with git {args[0]}: {detail}"
        )
    return proc.stdout


def _paths(output):
    return tuple(
        item.decode("utf-8", "surrogateescape")
        for item in output.split(b"\0") if item
    )


def _tracked_files(review):
    """Return every tracked regular file, recursing through pinned submodules."""
    roots = [("", review.path)]
    roots.extend((name, review.path / name) for name in review.submodules)
    files = []
    for prefix, root in roots:
        if not root.is_dir():
            raise ContextCompletenessError(f"required submodule is missing: {prefix or root}")
        for relative in _paths(_git(root, "ls-files", "-z")):
            candidate = root / relative
            # A superproject's gitlink is a directory, not source evidence. Its
            # own tracked files are added by the corresponding recursive root.
            if not candidate.is_file() and not candidate.is_symlink():
                continue
            name = f"{prefix}/{relative}" if prefix else relative
            files.append(name)
    return tuple(sorted(set(files)))


def _changed_files(review):
    return set(_paths(_git(
        review.path, "diff", "--name-only", "-z", "--no-renames",
        review.merge_base_sha, review.head_sha,
    )))


def _is_changed(name, changed):
    if name in changed:
        return True
    # A changed gitlink pins a different complete submodule tree. Avoid claiming
    # that documents within it are unchanged relative to the reviewed range.
    return any(name.startswith(f"{changed_name.rstrip('/')}/")
               for changed_name in changed)


def _document_kind(name):
    path = Path(name)
    base = path.name.lower()
    stem = path.stem.lower()
    parts = {part.lower() for part in path.parts}
    if (base in {"agents.md", "claude.md", "contributing.md", "readme.md"}
            or base == "copilot-instructions.md"
            or ".cursor" in parts or ".claude" in parts):
        return "instructions"
    if ({"adr", "adrs", "decisions", "decision"} & parts
            or stem.startswith("adr-") or stem.startswith("adr_")):
        return "adr"
    if "glossary" in stem or stem in {"terminology", "ubiquitous-language"}:
        return "glossary"
    if "domain" in parts or "domain" in stem:
        return "domain"
    if ({"standard", "standards", "style", "styleguide", "conventions"} & parts
            or any(word in stem for word in ("standard", "style-guide", "styleguide"))):
        return "standards"
    return None


def _documents(files, changed):
    documents = []
    for name in files:
        kind = _document_kind(name)
        if kind is None:
            continue
        documents.append({
            "path": name,
            "kind": kind,
            "unchanged": not _is_changed(name, changed),
        })
    return documents


_CODE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".hpp", ".java",
    ".js", ".jsx", ".kt", ".kts", ".lua", ".m", ".php", ".py", ".rb",
    ".rs", ".scala", ".sh", ".sql", ".swift", ".ts", ".tsx", ".vue",
}
_TEST_DIRECTORIES = {"test", "tests", "spec", "specs", "__tests__"}


def _is_test(name):
    path = Path(name)
    base = path.name.lower()
    return (_TEST_DIRECTORIES & {part.lower() for part in path.parts[:-1]}
            or base.startswith("test_") or base.endswith("_test.py")
            or ".test." in base or ".spec." in base)


def _file_inventory(files):
    tests, implementation = [], []
    for name in files:
        if Path(name).suffix.lower() not in _CODE_SUFFIXES:
            continue
        if _is_test(name):
            tests.append(name)
        elif _document_kind(name) is None:
            implementation.append(name)
    return {"all": list(files), "implementation": implementation, "tests": tests}


def _is_lfs_pointer(path):
    try:
        with path.open("rb") as stream:
            prefix = stream.read(256)
    except OSError as exc:
        raise ContextCompletenessError(f"cannot read required LFS content {path}: {exc}") from exc
    return (prefix.startswith(b"version https://git-lfs.github.com/spec/v1\n")
            and b"\noid sha256:" in prefix)


def _lfs_completeness(review, binary_irrelevance):
    """Audit actual files, never trusting a successful transport exit alone."""
    evidence = dict(getattr(review, "lfs_irrelevance", ()) or ())
    evidence.update(binary_irrelevance or {})
    warnings = list(getattr(review, "warnings", ()) or ())
    unknown = sorted(set(evidence) - set(review.lfs_files))
    if unknown:
        raise ContextCompletenessError(
            "LFS irrelevance evidence names non-LFS paths: " + ", ".join(unknown)
        )
    unresolved = []
    for name in review.lfs_files:
        path = review.path / name
        if not path.is_file():
            raise ContextCompletenessError(f"required Git LFS file is missing: {name}")
        if _is_lfs_pointer(path):
            unresolved.append(name)

    missing = []
    warned = {warning.get("path") for warning in warnings
              if isinstance(warning, dict)}
    for name in unresolved:
        reason = evidence.get(name)
        if not isinstance(reason, str) or not reason.strip():
            missing.append(name)
        elif name not in warned:
            warnings.append({
                "code": "LFS_CONTENT_UNAVAILABLE",
                "path": name,
                "message": f"Git LFS content is unavailable for {name}; review remains binary-incomplete",
                "evidence": reason.strip(),
            })
    if missing:
        raise ContextCompletenessError(
            "missing required Git LFS content: " + ", ".join(missing)
            + "; an unavailable binary is waivable only with explicit, non-empty irrelevance evidence"
        )
    return warnings, not unresolved


def _description_value(value):
    if isinstance(value, dict):
        value = value.get("description")
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _acquire_intent(description, description_fetcher, intent_fetcher, **request):
    warnings = []
    text = _description_value(description)
    if text is None and description_fetcher is not None:
        try:
            text = _description_value(description_fetcher())
        except Exception as exc:  # a read-only enrichment cannot hide the tree
            warnings.append(f"review description acquisition failed: {exc}")
    if text is None:
        warnings.append("review description unavailable from the provider read-only seam")

    fetcher = intent_fetcher or rg_ticket_fetch.fetch_final_intent
    try:
        acquired = fetcher(description=text, **request)
        if not isinstance(acquired, dict):
            raise TypeError("intent fetcher did not return an object")
    except Exception as exc:
        acquired = {}
        warnings.append(f"linked issue/spec intent acquisition failed: {exc}")
    if text is None:
        text = _description_value(acquired.get("description"))
    tickets = acquired.get("tickets") or []
    if not isinstance(tickets, list):
        warnings.append("linked issue/spec intent response had invalid tickets; ignored")
        tickets = []
    for warning in acquired.get("warnings") or acquired.get("notes") or []:
        if str(warning) not in warnings:
            warnings.append(str(warning))
    return {
        "description": ({"text": text, "source": "provider"} if text is not None else None),
        "tickets": tickets,
        "references": acquired.get("references", request.get("linked_intent")),
        "listed_only": acquired.get("listed_only") or [],
        "warnings": warnings,
    }


def collect_review_context(review, *, description=None, description_fetcher=None,
                           linked_intent=None, commit_messages=(), branch=None,
                           settings=None, intent_fetcher=None, runner=None,
                           cache_dir=None, binary_irrelevance=None):
    """Return the compact ``final-review-context/v1`` evidence contract.

    ``review`` is a :class:`MaterializedReview` and remains open only for the
    caller's context manager. No source contents are copied into the result.
    Intent acquisition is always attempted through the existing read-only
    ticket seam; misses are warnings and never become fabricated requirements.
    Missing required LFS/submodule content is a completeness error. An LFS
    pointer may degrade to a warning only with path-specific, non-empty
    ``binary_irrelevance`` evidence supplied by the trusted caller.
    """
    files = _tracked_files(review)
    changed = _changed_files(review)
    lfs_warnings, lfs_complete = _lfs_completeness(review, binary_irrelevance)
    intent = _acquire_intent(
        description, description_fetcher, intent_fetcher,
        linked_intent=linked_intent, commit_messages=commit_messages,
        branch=branch, settings=settings, repo_root=str(review.path),
        runner=runner, cache_dir=cache_dir,
    )
    completeness = {
        "complete": True,
        "submodules": list(review.submodules),
        "lfs_files": list(review.lfs_files),
        "lfs_complete": lfs_complete,
    }
    return {
        "contract": "final-review-context/v1",
        "contract_version": CONTRACT_VERSION,
        "review_mode": "complete-repository",
        "reviewer_instructions": REVIEWER_INSTRUCTIONS,
        "search_roots": [str(review.path)],
        "target_sha": review.target_sha,
        "head_sha": review.head_sha,
        "merge_base_sha": review.merge_base_sha,
        "diff": review.diff,
        "documents": _documents(files, changed),
        "files": _file_inventory(files),
        "intent": intent,
        "completeness": completeness,
        "warnings": lfs_warnings,
    }


__all__ = ["CONTRACT_VERSION", "ContextCompletenessError", "collect_review_context"]
