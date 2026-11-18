"""Provider-neutral primitives for explicit final review.

Adapters return JSON-friendly dictionaries and accept an injected runner with
``(argv, cwd, timeout) -> (stdout, note)``.  Runner notes are transport
failures; provider adapters turn them into explicit exceptions rather than
silently reviewing incomplete metadata.
"""
import json
import re
import subprocess
from typing import Protocol
from urllib.parse import quote, urlsplit


FETCH_TIMEOUT = 10

_LOCAL_NUMERIC = re.compile(r"[1-9][0-9]*$")
_GITHUB_PULL_PATH = re.compile(r"^/[^/]+/[^/]+/pull/[1-9][0-9]*/?$")
_GITLAB_MR_PATH = re.compile(r"^/.+/-/merge_requests/[1-9][0-9]*/?$")


def _github_remote(repo_root):
    """Recognize only github.com origins; never contact the remote."""
    try:
        process = subprocess.run(
            ["git", "-C", str(repo_root or "."), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if process.returncode:
        return False
    remote = process.stdout.strip()
    if "://" in remote:
        return (urlsplit(remote).hostname or "").lower() == "github.com"
    match = re.fullmatch(r"(?:[^@]+@)?([^:]+):.+", remote)
    return bool(match and match.group(1).lower() == "github.com")


def provider_name_for_references(references, repo_root=None):
    """Select a forge from canonical refs, sigils, or a local GitHub origin.

    Unknown references retain the historical GitLab default and are validated
    by that adapter. Bare numerics select GitHub only for a github.com origin;
    this keeps existing GitLab numeric callers unchanged elsewhere.
    """
    if isinstance(references, str):
        references = [references]
    else:
        references = list(references)
    selected = set()
    bare_numeric = bool(references)
    for reference in references:
        if not isinstance(reference, str):
            bare_numeric = False
            continue
        value = reference.strip()
        if re.fullmatch(r"#[1-9][0-9]*", value):
            selected.add("github")
            bare_numeric = False
        elif re.fullmatch(r"![1-9][0-9]*", value):
            selected.add("gitlab")
            bare_numeric = False
        elif not _LOCAL_NUMERIC.fullmatch(value):
            bare_numeric = False
        if "://" not in value:
            continue
        parsed = urlsplit(value)
        if parsed.netloc.lower() == "github.com" and _GITHUB_PULL_PATH.fullmatch(parsed.path):
            selected.add("github")
        elif _GITLAB_MR_PATH.fullmatch(parsed.path):
            selected.add("gitlab")
    if len(selected) > 1:
        raise ProviderReferenceError("final-review references contain mixed GitHub and GitLab providers")
    if selected:
        return selected.pop()
    if bare_numeric and _github_remote(repo_root):
        return "github"
    return "gitlab"


def select_provider(references, *, provider=None, repo_root=None,
                    gitlab_factory=None, github_factory=None):
    """Instantiate the selected adapter, always preserving injection precedence."""
    if provider is not None:
        return provider
    name = provider_name_for_references(references, repo_root=repo_root)
    if name == "github":
        if github_factory is None:
            from rg_final_github import GitHubProvider
            github_factory = GitHubProvider
        return github_factory()
    if gitlab_factory is None:
        from rg_final_gitlab import GitLabProvider
        gitlab_factory = GitLabProvider
    return gitlab_factory()


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
