"""Provider-neutral primitives for explicit final review.

Adapters return JSON-friendly dictionaries and accept an injected runner with
``(argv, cwd, timeout) -> (stdout, note)``.  Runner notes are transport
failures; provider adapters turn them into explicit exceptions rather than
silently reviewing incomplete metadata.
"""
import json
import subprocess
from typing import Protocol
from urllib.parse import quote, urlsplit


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


class FinalReviewGraphProvider(FinalReviewProvider, Protocol):
    """Provider reads needed to discover a complete open-item graph."""

    def list_open(self, pinned, repo_root=None):
        """Return canonical references for open items in the target project."""


def _gitlab_open_references(provider, pinned, repo_root):
    """List canonical open-MR URLs using the existing glab transport seam."""
    runner = getattr(provider, "runner", None)
    if runner is None:
        raise ProviderReadError("GitLab open-item discovery requires an authenticated glab runner")
    host = urlsplit(pinned["url"]).netloc or None
    project_id = quote(str(pinned["target"]["project_id"]), safe="")
    references = []
    page = 1
    while True:
        endpoint = (f"projects/{project_id}/merge_requests?state=opened&scope=all"
                    f"&per_page=100&page={page}")
        argv = ["glab", "api"]
        if host is not None:
            argv.extend(["--hostname", host])
        argv.append(endpoint)
        try:
            out, note = runner(argv, repo_root, FETCH_TIMEOUT)
        except Exception as exc:
            raise ProviderReadError(f"glab open-item discovery failed: {exc}") from exc
        if out is None:
            raise ProviderReadError(note or "glab could not discover open merge requests")
        try:
            batch = json.loads(out)
        except (TypeError, ValueError) as exc:
            raise ProviderReadError("glab open-item discovery returned invalid JSON") from exc
        if not isinstance(batch, list):
            raise ProviderReadError("glab open-item discovery returned a non-array response")
        for data in batch:
            if not isinstance(data, dict) or not isinstance(data.get("web_url"), str) \
                    or not data["web_url"]:
                raise ProviderReadError("open merge request is missing its canonical web_url")
            references.append(data["web_url"])
        if len(batch) < 100:
            return references
        page += 1


def list_open_references(provider, pinned, repo_root=None):
    """Read open-item references without making prose a graph authority."""
    list_open = getattr(provider, "list_open", None)
    try:
        if list_open is not None:
            return list(list_open(pinned, repo_root=repo_root))
        if pinned.get("provider") == "gitlab":
            return _gitlab_open_references(provider, pinned, repo_root)
        raise ProviderReadError("provider does not implement open-item discovery")
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderReadError(f"open-item discovery failed: {exc}") from exc


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
