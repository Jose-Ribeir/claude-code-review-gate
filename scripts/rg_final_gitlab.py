"""Read provider-pinned GitLab merge-request metadata for final review.

Only read-only ``glab api`` calls are made.  A full URL determines both the
GitLab host and target project; a repository-local ``17``/``!17`` delegates
project resolution to glab's existing ``:fullpath`` remote convention.
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


_MR_PATH = re.compile(r"^/(.+)/-/merge_requests/([1-9][0-9]*)/?$")
_LOCAL_REFERENCE = re.compile(r"!?([1-9][0-9]*)$")


class GitLabMetadataError(ProviderError):
    """GitLab returned JSON that cannot establish an immutable review item."""


def normalize_reference(reference):
    """Normalize one GitLab MR reference without consulting the checkout.

    The returned JSON-friendly dictionary contains ``iid`` plus explicit URL
    coordinates.  ``host`` and ``project_path`` are None for local numerics.
    """
    if not isinstance(reference, str):
        raise ProviderReferenceError("GitLab MR reference must be a string")
    reference = reference.strip()
    local = _LOCAL_REFERENCE.fullmatch(reference)
    if local:
        return {"provider": "gitlab", "iid": int(local.group(1)),
                "host": None, "project_path": None}

    parsed = urlsplit(reference)
    match = _MR_PATH.fullmatch(parsed.path)
    if (parsed.scheme not in ("http", "https") or not parsed.netloc
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or match is None):
        raise ProviderReferenceError(
            f"not an unambiguous GitLab merge-request reference: {reference!r}")
    project_path = unquote(match.group(1))
    if (not project_path or project_path.startswith("/")
            or any(part in ("", ".", "..") for part in project_path.split("/"))):
        raise ProviderReferenceError(f"invalid GitLab project path in {reference!r}")
    return {"provider": "gitlab", "iid": int(match.group(2)),
            "host": parsed.netloc, "project_path": project_path}


class GitLabProvider:
    """Provider-neutral ``read``/``revalidate`` adapter backed by glab."""

    def __init__(self, runner=None):
        self.runner = runner or run_cli

    def read(self, reference, repo_root=None):
        """Fetch an MR and current source/target heads as immutable metadata.

        Repository-branch reads, rather than potentially stale MR diff refs,
        establish both heads.  ``target.base_sha`` retains GitLab's merge-base
        diff anchor.  Branch heads must agree with the MR diff anchors before
        any immutable metadata is returned.
        """
        return self._read(reference, repo_root)

    def revalidate(self, pinned, repo_root=None):
        """Re-read current refs and reject movement even with stale diff refs."""
        return self._read(pinned["url"], repo_root, pinned=pinned)

    def _read(self, reference, repo_root, pinned=None):
        ref = normalize_reference(reference)
        iid = ref["iid"]
        if ref["project_path"] is None:
            endpoint = f"projects/:fullpath/merge_requests/{iid}"
        else:
            project = quote(ref["project_path"], safe="")
            endpoint = f"projects/{project}/merge_requests/{iid}"
        data = self._request(endpoint, ref["host"], repo_root)
        if data.get("iid") != iid:
            raise GitLabMetadataError(
                f"GitLab MR response iid {data.get('iid')!r} does not match !{iid}")

        target_id = self._project_id(data, "target_project_id")
        source_id = self._project_id(data, "source_project_id")
        diff_revisions = self._mr_revisions(data)
        target_project = self._read_project(target_id, ref["host"], repo_root)
        source_project = (target_project if source_id == target_id else
                          self._read_project(source_id, ref["host"], repo_root))
        source_branch = self._text(data, "source_branch")
        target_branch = self._text(data, "target_branch")
        source_head = self._read_branch(
            source_id, source_branch, ref["host"], repo_root)
        target_head = self._read_branch(
            target_id, target_branch, ref["host"], repo_root)
        current = self._item(
            data, target_project, source_project,
            (source_head, target_head, diff_revisions[2]),
        )
        if pinned is not None:
            # Report branch movement as staleness before rejecting the stale MR
            # anchors, so callers requeue instead of merely losing freshness.
            ensure_unchanged(pinned, current)
        diff_head, diff_start, _ = diff_revisions
        if source_head != diff_head:
            raise GitLabMetadataError(
                "GitLab source branch head does not match diff_refs.head_sha: "
                f"{source_head!r} != {diff_head!r}")
        if target_head != diff_start:
            raise GitLabMetadataError(
                "GitLab target branch head does not match diff_refs.start_sha: "
                f"{target_head!r} != {diff_start!r}")
        return current

    def _request(self, endpoint, host, repo_root):
        argv = ["glab", "api"]
        if host is not None:
            argv.extend(["--hostname", host])
        argv.append(endpoint)
        try:
            out, note = self.runner(argv, repo_root, FETCH_TIMEOUT)
        except Exception as exc:
            raise ProviderReadError(f"glab read failed for {endpoint}: {exc}") from exc
        if out is None:
            raise ProviderReadError(note or f"glab could not read {endpoint}")
        try:
            data = json.loads(out)
        except (TypeError, ValueError) as exc:
            raise GitLabMetadataError(
                f"glab returned invalid JSON for {endpoint}") from exc
        if not isinstance(data, dict):
            raise GitLabMetadataError(
                f"glab returned non-object JSON for {endpoint}")
        return data

    def _read_project(self, project_id, host, repo_root):
        endpoint = f"projects/{quote(str(project_id), safe='')}"
        data = self._request(endpoint, host, repo_root)
        if data.get("id") != project_id:
            raise GitLabMetadataError(
                f"GitLab project response id {data.get('id')!r} does not match {project_id!r}")
        return {
            "project_id": project_id,
            "project_path": self._text(data, "path_with_namespace"),
            "url": self._text(data, "web_url"),
            "http_url": self._text(data, "http_url_to_repo"),
            "ssh_url": self._text(data, "ssh_url_to_repo"),
        }

    def _read_branch(self, project_id, branch, host, repo_root):
        project = quote(str(project_id), safe="")
        encoded_branch = quote(branch, safe="")
        endpoint = f"projects/{project}/repository/branches/{encoded_branch}"
        data = self._request(endpoint, host, repo_root)
        if data.get("name") != branch:
            raise GitLabMetadataError(
                f"GitLab branch response name {data.get('name')!r} "
                f"does not match {branch!r}")
        commit = self._object(data, "commit")
        return self._text(commit, "id", "branch.commit.id")

    def _mr_revisions(self, data):
        diff_refs = self._object(data, "diff_refs")
        head_sha = self._text(diff_refs, "head_sha", "diff_refs.head_sha")
        target_sha = self._text(diff_refs, "start_sha", "diff_refs.start_sha")
        base_sha = self._text(diff_refs, "base_sha", "diff_refs.base_sha")
        if data.get("sha") not in (None, head_sha):
            raise GitLabMetadataError("GitLab MR sha disagrees with diff_refs.head_sha")
        return head_sha, target_sha, base_sha

    def _item(self, data, target_project, source_project, revisions):
        head_sha, target_sha, base_sha = revisions
        author_data = self._object(data, "author")
        author = {
            "username": self._text(author_data, "username", "author.username"),
            "name": author_data.get("name") or "",
        }
        if not isinstance(author["name"], str):
            raise GitLabMetadataError("GitLab metadata field author.name must be a string")
        description = data.get("description")
        if description is None:
            description = ""
        if not isinstance(description, str):
            raise GitLabMetadataError("GitLab metadata field description must be a string")
        target = dict(target_project)
        target.update({
            "branch": self._text(data, "target_branch"),
            "sha": target_sha,
            "base_sha": base_sha,
        })
        source = dict(source_project)
        source.update({
            "branch": self._text(data, "source_branch"),
            "sha": head_sha,
        })
        title = data.get("title")
        if not isinstance(title, str):
            raise GitLabMetadataError("GitLab metadata field title must be a string")
        return {
            "provider": "gitlab",
            "id": f"gitlab:{target['project_id']}!{data['iid']}",
            "iid": data["iid"],
            "url": self._text(data, "web_url"),
            "title": title,
            "description": description,
            "state": self._text(data, "state"),
            "author": author,
            "source": source,
            "target": target,
            "linked_intent": [
                {"key": key, "source": "description", "role": "direct"}
                for key in rg_tickets.extract_keys(description)
            ],
        }

    @classmethod
    def _project_id(cls, data, field):
        value = data.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, str)) or value == "":
            raise GitLabMetadataError(
                f"GitLab metadata field {field} must be a project id")
        return value

    @staticmethod
    def _object(data, field):
        value = data.get(field)
        if not isinstance(value, dict):
            raise GitLabMetadataError(f"GitLab metadata field {field} must be an object")
        return value

    @staticmethod
    def _text(data, field, label=None):
        value = data.get(field)
        if not isinstance(value, str) or not value:
            raise GitLabMetadataError(
                f"GitLab metadata field {label or field} must be non-empty text")
        return value
