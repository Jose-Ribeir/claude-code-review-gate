"""GitLab final-review metadata through a fake authenticated ``glab`` CLI."""
import json
import os
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)

import rg_final_gitlab  # noqa: E402
import rg_final_provider  # noqa: E402


SOURCE_SHA = "a" * 40
TARGET_SHA = "b" * 40
BASE_SHA = "c" * 40


def mr_payload():
    return {
        "id": 9017,
        "iid": 17,
        "title": "Compose the final stack",
        "description": "Implement the provider seam.\n\nCloses #88; see SPEC-16.",
        "state": "opened",
        "web_url": "https://gitlab.example.test/group/project/-/merge_requests/17",
        "author": {"username": "another-author", "name": "Another Author"},
        "source_project_id": 222,
        "target_project_id": 111,
        "source_branch": "feature/final-review",
        "target_branch": "main",
        "sha": SOURCE_SHA,
        "diff_refs": {
            "base_sha": BASE_SHA,
            "head_sha": SOURCE_SHA,
            "start_sha": TARGET_SHA,
        },
    }


def project_payload(project_id, path):
    return {
        "id": project_id,
        "path_with_namespace": path,
        "web_url": f"https://gitlab.example.test/{path}",
        "http_url_to_repo": f"https://gitlab.example.test/{path}.git",
        "ssh_url_to_repo": f"git@gitlab.example.test:{path}.git",
    }


def branch_payload(branch, sha):
    return {"name": branch, "commit": {"id": sha}}


def metadata_responses(payload=None, *, source_head=None, target_head=None):
    payload = payload or mr_payload()
    source_head = source_head or payload["diff_refs"]["head_sha"]
    target_head = target_head or payload["diff_refs"]["start_sha"]
    return [
        (json.dumps(payload), None),
        (json.dumps(project_payload(111, "group/project")), None),
        (json.dumps(project_payload(222, "contributor/fork")), None),
        (json.dumps(branch_payload(payload["source_branch"], source_head)), None),
        (json.dumps(branch_payload(payload["target_branch"], target_head)), None),
    ]


class FakeGlab:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, cwd, timeout):
        self.calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout})
        if not self.responses:
            return None, "not found"
        return self.responses.pop(0)


def test_full_url_reads_the_addressed_merge_request_without_using_the_checkout():
    payload = mr_payload()
    runner = FakeGlab(metadata_responses(payload))
    provider = rg_final_gitlab.GitLabProvider(runner=runner)

    item = provider.read("https://gitlab.example.test/group/project/-/merge_requests/17")

    assert item["provider"] == "gitlab"
    assert item["iid"] == 17
    assert item["url"] == payload["web_url"]
    assert runner.calls[0] == {
        "argv": [
            "glab", "api", "--hostname", "gitlab.example.test",
            "projects/group%2Fproject/merge_requests/17",
        ],
        "cwd": None,
        "timeout": rg_final_gitlab.FETCH_TIMEOUT,
    }


def test_repository_local_numeric_reference_uses_glab_fullpath_substitution(tmp_path):
    payload = mr_payload()
    payload["iid"] = 23
    payload["web_url"] = "https://gitlab.example.test/group/project/-/merge_requests/23"
    runner = FakeGlab(metadata_responses(payload))
    provider = rg_final_gitlab.GitLabProvider(runner=runner)

    item = provider.read("23", repo_root=str(tmp_path))

    assert item["iid"] == 23
    assert runner.calls[0] == {
        "argv": ["glab", "api", "projects/:fullpath/merge_requests/23"],
        "cwd": str(tmp_path),
        "timeout": rg_final_gitlab.FETCH_TIMEOUT,
    }


def test_read_maps_author_fork_branches_and_provider_pinned_revisions():
    runner = FakeGlab(metadata_responses())
    provider = rg_final_gitlab.GitLabProvider(runner=runner)

    item = provider.read(mr_payload()["web_url"])

    assert item["id"] == "gitlab:111!17"
    assert item["title"] == "Compose the final stack"
    assert item["description"] == mr_payload()["description"]
    assert item["state"] == "opened"
    assert item["author"] == {"username": "another-author", "name": "Another Author"}
    assert item["source"] == {
        "project_id": 222,
        "project_path": "contributor/fork",
        "url": "https://gitlab.example.test/contributor/fork",
        "http_url": "https://gitlab.example.test/contributor/fork.git",
        "ssh_url": "git@gitlab.example.test:contributor/fork.git",
        "branch": "feature/final-review",
        "sha": SOURCE_SHA,
    }
    assert item["target"] == {
        "project_id": 111,
        "project_path": "group/project",
        "url": "https://gitlab.example.test/group/project",
        "http_url": "https://gitlab.example.test/group/project.git",
        "ssh_url": "git@gitlab.example.test:group/project.git",
        "branch": "main",
        "sha": TARGET_SHA,
        "base_sha": BASE_SHA,
    }
    assert [call["argv"][-1] for call in runner.calls] == [
        "projects/group%2Fproject/merge_requests/17",
        "projects/111",
        "projects/222",
        "projects/222/repository/branches/feature%2Ffinal-review",
        "projects/111/repository/branches/main",
    ]


def test_description_exposes_linked_intent_without_treating_it_as_an_edge():
    runner = FakeGlab(metadata_responses())

    item = rg_final_gitlab.GitLabProvider(runner=runner).read(mr_payload()["web_url"])

    assert item["linked_intent"] == [
        {"key": "#88", "source": "description", "role": "direct"},
        {"key": "SPEC-16", "source": "description", "role": "direct"},
    ]
    assert "dependencies" not in item


def test_bang_prefixed_repository_local_reference_is_accepted(tmp_path):
    runner = FakeGlab(metadata_responses())

    item = rg_final_gitlab.GitLabProvider(runner=runner).read("!17", repo_root=tmp_path)

    assert item["iid"] == 17
    assert runner.calls[0]["argv"][-1] == "projects/:fullpath/merge_requests/17"


def test_revalidate_refreshes_metadata_when_all_pinned_revisions_are_unchanged():
    runner = FakeGlab(metadata_responses())
    provider = rg_final_gitlab.GitLabProvider(runner=runner)
    pinned = provider.read(mr_payload()["web_url"])
    runner.responses.extend(metadata_responses())

    current = provider.revalidate(pinned)

    assert current == pinned
    assert len(runner.calls) == 10
    assert [call["argv"][-1] for call in runner.calls[-2:]] == [
        "projects/222/repository/branches/feature%2Ffinal-review",
        "projects/111/repository/branches/main",
    ]


@pytest.mark.parametrize("revision", [
    "source_sha", "target_sha", "base_sha", "source_branch", "target_branch",
])
def test_revalidate_rejects_source_target_and_retargeting_movement(revision):
    runner = FakeGlab(metadata_responses())
    provider = rg_final_gitlab.GitLabProvider(runner=runner)
    pinned = provider.read(mr_payload()["web_url"])
    changed = mr_payload()
    if revision == "source_sha":
        changed["sha"] = changed["diff_refs"]["head_sha"] = "d" * 40
    elif revision == "target_sha":
        changed["diff_refs"]["start_sha"] = "e" * 40
    elif revision == "base_sha":
        changed["diff_refs"]["base_sha"] = "f" * 40
    elif revision == "source_branch":
        changed["source_branch"] = "feature/moved"
    else:
        changed["target_branch"] = "release"
    runner.responses.extend(metadata_responses(changed))

    with pytest.raises(rg_final_provider.RevisionChangedError, match=revision.replace("_", " ")):
        provider.revalidate(pinned)


@pytest.mark.parametrize(("side", "anchor", "moved"), [
    ("source", "head_sha", "d" * 40),
    ("target", "start_sha", "e" * 40),
])
def test_initial_read_rejects_branch_heads_that_disagree_with_stale_diff_refs(
        side, anchor, moved):
    heads = {"source_head": SOURCE_SHA, "target_head": TARGET_SHA}
    heads[f"{side}_head"] = moved
    runner = FakeGlab(metadata_responses(**heads))

    with pytest.raises(
            rg_final_gitlab.GitLabMetadataError,
            match=rf"{side} branch head.*diff_refs\.{anchor}"):
        rg_final_gitlab.GitLabProvider(runner=runner).read(mr_payload()["web_url"])


@pytest.mark.parametrize(("side", "moved"), [
    ("source", "d" * 40),
    ("target", "e" * 40),
])
def test_revalidate_detects_branch_movement_even_when_diff_refs_are_stale(side, moved):
    runner = FakeGlab(metadata_responses())
    provider = rg_final_gitlab.GitLabProvider(runner=runner)
    pinned = provider.read(mr_payload()["web_url"])
    heads = {"source_head": SOURCE_SHA, "target_head": TARGET_SHA}
    heads[f"{side}_head"] = moved
    # Deliberately leave the second MR response and every diff_ref unchanged.
    runner.responses.extend(metadata_responses(**heads))

    with pytest.raises(rg_final_provider.RevisionChangedError) as changed:
        provider.revalidate(pinned)

    assert changed.value.field == f"{side} sha"
    assert changed.value.expected == pinned[side]["sha"]
    assert changed.value.current == moved


def test_branch_head_read_failure_cannot_fall_back_to_diff_refs():
    runner = FakeGlab([
        (json.dumps(mr_payload()), None),
        (json.dumps(project_payload(111, "group/project")), None),
        (json.dumps(project_payload(222, "contributor/fork")), None),
        (None, "403 source branch unavailable"),
    ])

    with pytest.raises(rg_final_provider.ProviderReadError, match="source branch unavailable"):
        rg_final_gitlab.GitLabProvider(runner=runner).read(mr_payload()["web_url"])


def test_branch_response_must_identify_the_requested_branch():
    responses = metadata_responses()
    responses[3] = (json.dumps(branch_payload("feature/other", SOURCE_SHA)), None)
    runner = FakeGlab(responses)

    with pytest.raises(rg_final_gitlab.GitLabMetadataError, match="branch response name"):
        rg_final_gitlab.GitLabProvider(runner=runner).read(mr_payload()["web_url"])


def test_revalidate_rejects_source_project_movement_between_forks():
    runner = FakeGlab(metadata_responses())
    provider = rg_final_gitlab.GitLabProvider(runner=runner)
    pinned = provider.read(mr_payload()["web_url"])
    changed = mr_payload()
    changed["source_project_id"] = 333
    runner.responses.extend([
        (json.dumps(changed), None),
        (json.dumps(project_payload(111, "group/project")), None),
        (json.dumps(project_payload(333, "someone/another-fork")), None),
        (json.dumps(branch_payload("feature/final-review", SOURCE_SHA)), None),
        (json.dumps(branch_payload("main", TARGET_SHA)), None),
    ])

    with pytest.raises(rg_final_provider.RevisionChangedError, match="source project"):
        provider.revalidate(pinned)


def test_transport_failure_is_an_explicit_provider_read_error():
    provider = rg_final_gitlab.GitLabProvider(
        runner=FakeGlab([(None, "glab not found on PATH")]))

    with pytest.raises(rg_final_provider.ProviderReadError, match="glab not found"):
        provider.read(mr_payload()["web_url"])


def test_project_read_failure_does_not_fall_back_to_local_or_unpinned_data():
    runner = FakeGlab([(json.dumps(mr_payload()), None), (None, "403 Forbidden")])

    with pytest.raises(rg_final_provider.ProviderReadError, match="403 Forbidden"):
        rg_final_gitlab.GitLabProvider(runner=runner).read(mr_payload()["web_url"])


@pytest.mark.parametrize("raw", ["not-json", "[]"])
def test_invalid_provider_json_is_a_gitlab_metadata_error(raw):
    runner = FakeGlab([(raw, None)])

    with pytest.raises(rg_final_gitlab.GitLabMetadataError, match="JSON"):
        rg_final_gitlab.GitLabProvider(runner=runner).read(mr_payload()["web_url"])


def test_missing_pinned_sha_is_a_gitlab_metadata_error_naming_the_field():
    payload = mr_payload()
    del payload["diff_refs"]["head_sha"]
    runner = FakeGlab([(json.dumps(payload), None)])

    with pytest.raises(rg_final_gitlab.GitLabMetadataError, match="diff_refs.head_sha"):
        rg_final_gitlab.GitLabProvider(runner=runner).read(mr_payload()["web_url"])


@pytest.mark.parametrize("reference", [
    "", "0", "-1", "feature-branch", "https://gitlab.example.test/group/project/issues/17",
])
def test_invalid_or_ambiguous_references_fail_before_any_provider_call(reference):
    runner = FakeGlab()

    with pytest.raises(rg_final_provider.ProviderReferenceError):
        rg_final_gitlab.GitLabProvider(runner=runner).read(reference)

    assert runner.calls == []


def test_same_source_and_target_project_is_read_once_with_one_repository_identity():
    payload = mr_payload()
    payload["source_project_id"] = 111
    runner = FakeGlab([
        (json.dumps(payload), None),
        (json.dumps(project_payload(111, "group/project")), None),
        (json.dumps(branch_payload("feature/final-review", SOURCE_SHA)), None),
        (json.dumps(branch_payload("main", TARGET_SHA)), None),
    ])

    item = rg_final_gitlab.GitLabProvider(runner=runner).read(payload["web_url"])

    assert [call["argv"][-1] for call in runner.calls] == [
        "projects/group%2Fproject/merge_requests/17",
        "projects/111",
        "projects/111/repository/branches/feature%2Ffinal-review",
        "projects/111/repository/branches/main",
    ]
    assert item["source"]["project_id"] == item["target"]["project_id"] == 111
    assert item["source"]["project_path"] == item["target"]["project_path"]
    assert item["source"]["http_url"] == item["target"]["http_url"]


def test_null_description_is_empty_intent_metadata_but_preserves_other_fields():
    payload = mr_payload()
    payload["description"] = None
    runner = FakeGlab(metadata_responses(payload))

    item = rg_final_gitlab.GitLabProvider(runner=runner).read(payload["web_url"])

    assert item["description"] == ""
    assert item["linked_intent"] == []
    assert item["source"]["sha"] == SOURCE_SHA


def test_revision_tuple_exposes_the_complete_provider_neutral_freshness_identity():
    runner = FakeGlab(metadata_responses())
    item = rg_final_gitlab.GitLabProvider(runner=runner).read(mr_payload()["web_url"])

    assert rg_final_provider.revision_tuple(item) == (
        222, "feature/final-review", SOURCE_SHA,
        111, "main", TARGET_SHA, BASE_SHA,
    )


def test_revalidate_rejects_target_project_movement():
    runner = FakeGlab(metadata_responses())
    provider = rg_final_gitlab.GitLabProvider(runner=runner)
    pinned = provider.read(mr_payload()["web_url"])
    changed = mr_payload()
    changed["target_project_id"] = 333
    runner.responses.extend([
        (json.dumps(changed), None),
        (json.dumps(project_payload(333, "group/moved-project")), None),
        (json.dumps(project_payload(222, "contributor/fork")), None),
        (json.dumps(branch_payload("feature/final-review", SOURCE_SHA)), None),
        (json.dumps(branch_payload("main", TARGET_SHA)), None),
    ])

    with pytest.raises(rg_final_provider.RevisionChangedError, match="target project"):
        provider.revalidate(pinned)


def test_raising_cli_boundary_becomes_a_provider_read_error():
    def unavailable(argv, cwd, timeout):
        raise OSError("authenticated glab is unavailable")

    with pytest.raises(rg_final_provider.ProviderReadError, match="glab is unavailable"):
        rg_final_gitlab.GitLabProvider(runner=unavailable).read(mr_payload()["web_url"])
