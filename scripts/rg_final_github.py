"""Read provider-pinned github.com pull-request metadata for final review.

Only read-only ``gh api`` calls are made. A full github.com URL determines the
target repository; repository-local ``17``/``#17`` references use gh's
``{owner}``/``{repo}`` checkout substitution. Base and head repository data is
read from the PR itself, so arbitrary authors and head forks retain their own
immutable identity. The compare API supplies the actual merge-base anchor.
"""
import json
import re
from urllib.parse import quote, unquote, urlsplit

import rg_tickets
from rg_final_provider import (
    FETCH_TIMEOUT,
    ProviderError,
    ProviderReadError,
    ProviderReferenceError,
    ensure_unchanged,
    run_cli,
)


_PR_PATH = re.compile(r"^/([^/]+)/([^/]+)/pull/([1-9][0-9]*)/?$")
_LOCAL_REFERENCE = re.compile(r"#?([1-9][0-9]*)$")


class GitHubMetadataError(ProviderError):
    """GitHub returned JSON that cannot establish an immutable review item."""


def normalize_reference(reference):
    """Normalize one github.com PR URL or repository-local numeric ID."""
    if not isinstance(reference, str):
        raise ProviderReferenceError("GitHub PR reference must be a string")
    reference = reference.strip()
    local = _LOCAL_REFERENCE.fullmatch(reference)
    if local:
        return {
            "provider": "github", "iid": int(local.group(1)),
            "host": None, "project_path": None,
        }

    parsed = urlsplit(reference)
    match = _PR_PATH.fullmatch(parsed.path)
    if (parsed.scheme not in ("http", "https") or parsed.netloc.lower() != "github.com"
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or match is None):
        raise ProviderReferenceError(
            f"not an unambiguous github.com pull-request reference: {reference!r}")
    owner = unquote(match.group(1))
    repository = unquote(match.group(2))
    if (any(part in ("", ".", "..") for part in (owner, repository))
            or "/" in owner or "/" in repository):
        raise ProviderReferenceError(f"invalid GitHub repository path in {reference!r}")
    return {
        "provider": "github", "iid": int(match.group(3)),
        "host": "github.com", "project_path": f"{owner}/{repository}",
    }


class GitHubProvider:
    """Provider-neutral ``read``/``revalidate``/``list_open`` adapter using gh."""

    def __init__(self, runner=None):
        self.runner = runner or run_cli

    def read(self, reference, repo_root=None):
        """Fetch PR identity and its immutable base/head merge comparison."""
        ref = normalize_reference(reference)
        number = ref["iid"]
        if ref["project_path"] is None:
            endpoint = f"repos/{{owner}}/{{repo}}/pulls/{number}"
        else:
            project = quote(ref["project_path"], safe="/")
            endpoint = f"repos/{project}/pulls/{number}"
        data = self._request(endpoint, ref["host"], repo_root)
        if not isinstance(data, dict):
            raise GitHubMetadataError("gh returned non-object JSON for a pull request")
        if data.get("number") != number:
            raise GitHubMetadataError(
                f"GitHub PR response number {data.get('number')!r} does not match #{number}")

        head = self._object(data, "head")
        base = self._object(data, "base")
        source_data = self._object(head, "repo", "head.repo")
        target_data = self._object(base, "repo", "base.repo")
        source = self._repository(source_data, "head.repo")
        target = self._repository(target_data, "base.repo")
        source_sha = self._text(head, "sha", "head.sha")
        target_sha = self._text(base, "sha", "base.sha")
        host = urlsplit(self._text(data, "html_url")).netloc.lower() or ref["host"]
        compare = (
            f"repos/{quote(target['project_path'], safe='/')}/compare/"
            f"{quote(target_sha, safe='')}...{quote(source_sha, safe='')}"
        )
        comparison = self._request(compare, host, repo_root)
        if not isinstance(comparison, dict):
            raise GitHubMetadataError("gh returned non-object JSON for a PR comparison")
        merge_base = self._object(comparison, "merge_base_commit")
        base_sha = self._text(merge_base, "sha", "merge_base_commit.sha")
        return self._item(data, source, target, source_sha, target_sha, base_sha)

    def revalidate(self, pinned, repo_root=None):
        """Re-read by canonical URL and reject every neutral revision change."""
        current = self.read(pinned["url"], repo_root=repo_root)
        ensure_unchanged(pinned, current)
        return current

    def list_open(self, pinned, repo_root=None):
        """List canonical open PR URLs in the pinned target repository."""
        project = quote(pinned["target"]["project_path"], safe="/")
        host = urlsplit(pinned["url"]).netloc.lower() or None
        references = []
        page = 1
        while True:
            endpoint = f"repos/{project}/pulls?state=open&per_page=100&page={page}"
            batch = self._request(endpoint, host, repo_root)
            if not isinstance(batch, list):
                raise ProviderReadError("gh open-PR discovery returned a non-array response")
            for data in batch:
                if not isinstance(data, dict) or not isinstance(data.get("html_url"), str):
                    raise ProviderReadError("open pull request is missing its canonical html_url")
                try:
                    reference = normalize_reference(data["html_url"])
                except ProviderReferenceError as exc:
                    raise ProviderReadError(
                        "open pull request has an invalid canonical html_url") from exc
                if reference["project_path"] != pinned["target"]["project_path"]:
                    raise ProviderReadError(
                        "open-PR discovery returned a different target repository")
                references.append(data["html_url"])
            if len(batch) < 100:
                return references
            page += 1

    def _request(self, endpoint, host, repo_root):
        argv = ["gh", "api"]
        if host:
            argv.extend(["--hostname", host])
        argv.append(endpoint)
        try:
            out, note = self.runner(argv, repo_root, FETCH_TIMEOUT)
        except Exception as exc:
            raise ProviderReadError(f"gh read failed for {endpoint}: {exc}") from exc
        if out is None:
            raise ProviderReadError(note or f"gh could not read {endpoint}")
        try:
            return json.loads(out)
        except (TypeError, ValueError) as exc:
            raise GitHubMetadataError(f"gh returned invalid JSON for {endpoint}") from exc

    @classmethod
    def _repository(cls, data, label):
        repository_id = data.get("id")
        if isinstance(repository_id, bool) or not isinstance(repository_id, (int, str)) \
                or repository_id == "":
            raise GitHubMetadataError(f"GitHub metadata field {label}.id must be a repository id")
        full_name = cls._text(data, "full_name", f"{label}.full_name")
        if len(full_name.split("/")) != 2 or any(
                part in ("", ".", "..") for part in full_name.split("/")):
            raise GitHubMetadataError(f"GitHub metadata field {label}.full_name is invalid")
        return {
            "project_id": repository_id,
            "project_path": full_name,
            "url": cls._text(data, "html_url", f"{label}.html_url"),
            "http_url": cls._text(data, "clone_url", f"{label}.clone_url"),
            "ssh_url": cls._text(data, "ssh_url", f"{label}.ssh_url"),
        }

    def _item(self, data, source_project, target_project,
              source_sha, target_sha, base_sha):
        author_data = self._object(data, "user")
        author = {
            "username": self._text(author_data, "login", "user.login"),
            "name": "",
        }
        body = data.get("body")
        if body is None:
            body = ""
        if not isinstance(body, str):
            raise GitHubMetadataError("GitHub metadata field body must be a string")
        title = data.get("title")
        if not isinstance(title, str):
            raise GitHubMetadataError("GitHub metadata field title must be a string")
        state = self._text(data, "state")
        target = dict(target_project)
        target.update({
            "branch": self._text(self._object(data, "base"), "ref", "base.ref"),
            "sha": target_sha,
            "base_sha": base_sha,
        })
        source = dict(source_project)
        source.update({
            "branch": self._text(self._object(data, "head"), "ref", "head.ref"),
            "sha": source_sha,
        })
        return {
            "provider": "github",
            "id": f"github:{target['project_id']}#{data['number']}",
            "iid": data["number"],
            "url": self._text(data, "html_url"),
            "title": title,
            "description": body,
            "state": "opened" if state == "open" else state,
            "author": author,
            "source": source,
            "target": target,
            "linked_intent": [
                {"key": key, "source": "description", "role": "direct"}
                for key in rg_tickets.extract_keys(body)
            ],
        }

    @staticmethod
    def _object(data, field, label=None):
        value = data.get(field)
        if not isinstance(value, dict):
            raise GitHubMetadataError(
                f"GitHub metadata field {label or field} must be an object")
        return value

    @staticmethod
    def _text(data, field, label=None):
        value = data.get(field)
        if not isinstance(value, str) or not value:
            raise GitHubMetadataError(
                f"GitHub metadata field {label or field} must be non-empty text")
        return value
