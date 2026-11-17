"""Provider-neutral primitives for explicit final review.

Adapters return JSON-friendly dictionaries and accept an injected runner with
``(argv, cwd, timeout) -> (stdout, note)``.  Runner notes are transport
failures; provider adapters turn them into explicit exceptions rather than
silently reviewing incomplete metadata.
"""
import subprocess
from typing import Protocol


FETCH_TIMEOUT = 10


class ProviderError(RuntimeError):
    """Base class for final-review provider failures."""


class ProviderReadError(ProviderError):
    """An authenticated provider read could not be completed."""


class ProviderReferenceError(ProviderError, ValueError):
    """A final-review reference is invalid or ambiguous."""


class RevisionChangedError(ProviderError):
    """A source, target, or merge-base revision moved after being pinned."""

    def __init__(self, field, expected, current):
        self.field = field
        self.expected = expected
        self.current = current
        super().__init__(f"{field} changed during final review: {expected!r} -> {current!r}")


class FinalReviewProvider(Protocol):
    """Compact provider-neutral adapter contract consumed by the supervisor."""

    def read(self, reference, repo_root=None):
        """Return one JSON-friendly item with source/target pinned SHAs."""

    def revalidate(self, pinned, repo_root=None):
        """Return fresh identical-revision metadata or raise RevisionChangedError."""


_REVISION_FIELDS = (
    ("source project", ("source", "project_id")),
    ("source branch", ("source", "branch")),
    ("source sha", ("source", "sha")),
    ("target project", ("target", "project_id")),
    ("target branch", ("target", "branch")),
    ("target sha", ("target", "sha")),
    ("target base sha", ("target", "base_sha")),
)


def revision_tuple(item):
    """The provider-neutral immutable identity used for freshness checks."""
    return tuple(item[group][field] for _, (group, field) in _REVISION_FIELDS)


def ensure_unchanged(pinned, current):
    """Raise a field-specific error when any pinned revision coordinate moves."""
    for label, (group, field) in _REVISION_FIELDS:
        expected = pinned[group].get(field)
        actual = current[group].get(field)
        if expected != actual:
            raise RevisionChangedError(label, expected, actual)


def run_cli(argv, cwd, timeout):
    """Run one read-only provider CLI command using the ticket-fetch contract."""
    try:
        proc = subprocess.run(argv, cwd=cwd or None, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"{argv[0]} timed out after {timeout}s"
    except FileNotFoundError:
        return None, f"{argv[0]} not found on PATH"
    except OSError as exc:
        return None, f"{argv[0]} could not run ({exc})"
    if proc.returncode != 0:
        first_err = (proc.stderr or "").strip().splitlines()
        detail = f" ({first_err[0][:160]})" if first_err else ""
        return None, (f"{' '.join(str(a) for a in argv)} exited "
                      f"{proc.returncode}{detail}")
    return proc.stdout, None
