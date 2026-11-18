"""GitHub PR parity through provider, graph, and trusted status public seams.

All ``gh`` reads and writes are in-memory doubles. Repository-local provider
selection uses only temporary Git remotes; no test contacts a live provider.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rg_final_github  # noqa: E402
import rg_final_graph  # noqa: E402
import rg_final_provider  # noqa: E402
from rg_final_publish import STATUS_CONTEXT  # noqa: E402
from rg_final_store import FinalStore  # noqa: E402
import test_final_publication as gitlab_publication  # noqa: E402


SOURCE_SHA = "a" * 40
TARGET_SHA = "b" * 40
BASE_SHA = "c" * 40
PR_URL = "https://github.com/group/project/pull/17"


@pytest.fixture
def store(tmp_path):
    with FinalStore(tmp_path / "final.sqlite3") as opened:
        yield opened


def repository_payload(project_id, full_name):
    return {
        "id": project_id,
        "full_name": full_name,
        "html_url": f"https://github.com/{full_name}",
        "clone_url": f"https://github.com/{full_name}.git",
        "ssh_url": f"git@github.com:{full_name}.git",
    }


def compare_payload(base_sha=BASE_SHA):
    return {"merge_base_commit": {"sha": base_sha}, "base_commit": {"sha": TARGET_SHA}}


def pr_payload():
    target = repository_payload(111, "group/project")
    source = repository_payload(222, "contributor/fork")
    return {
        "number": 17,
        "title": "Compose the final GitHub stack",
        "body": "Implement the provider seam.\n\nFixes #88; see SPEC-16.",
        "state": "open",
        "html_url": PR_URL,
        "user": {"login": "another-author"},
        "head": {
            "label": "contributor:feature/final-review",
            "ref": "feature/final-review",
            "sha": SOURCE_SHA,
            "repo": source,
        },
        "base": {
            "label": "group:main",
            "ref": "main",
            "sha": TARGET_SHA,
            "repo": target,
        },
    }


def metadata_responses(payload=None, compare=None):
    return [
        (json.dumps(payload or pr_payload()), None),
        (json.dumps(compare or compare_payload()), None),
    ]


class FakeGh:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, cwd, timeout):
        self.calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout})
        if not self.responses:
            return None, "not found"
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class ScriptedGhWrite:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, cwd, timeout):
        self.calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout})
        response = self.responses.pop(0) if self.responses else None
        if isinstance(response, BaseException):
            raise response
        if response is not None:
            return response
        return json.dumps({
            "id": 9001 + len(self.calls), "state": "success",
            "context": STATUS_CONTEXT,
        }), None

    @property
    def fields(self):
        fields = {}
        for argument in self.calls[-1]["argv"]:
            if argument.startswith(("state=", "context=", "description=", "target_url=")):
                key, value = argument.split("=", 1)
                fields[key] = value
        return fields


def test_full_github_url_reads_pr_and_immutable_compare_without_checkout_inference():
    runner = FakeGh(metadata_responses())

    item = rg_final_github.GitHubProvider(runner=runner).read(PR_URL)

    assert item["provider"] == "github"
    assert item["iid"] == 17
    assert item["url"] == PR_URL
    assert [call["argv"] for call in runner.calls] == [
        ["gh", "api", "--hostname", "github.com", "repos/group/project/pulls/17"],
        ["gh", "api", "--hostname", "github.com",
         f"repos/group/project/compare/{TARGET_SHA}...{SOURCE_SHA}"],
    ]
    assert all(call["cwd"] is None for call in runner.calls)
    assert all(call["timeout"] == rg_final_provider.FETCH_TIMEOUT for call in runner.calls)


@pytest.mark.parametrize("reference", ["17", "#17"])
def test_repository_local_numeric_id_uses_gh_repository_placeholders(reference, tmp_path):
    runner = FakeGh(metadata_responses())

    item = rg_final_github.GitHubProvider(runner=runner).read(
        reference, repo_root=str(tmp_path))

    assert item["iid"] == 17
    assert runner.calls[0]["argv"] == [
        "gh", "api", "repos/{owner}/{repo}/pulls/17",
    ]
    assert runner.calls[0]["cwd"] == str(tmp_path)
    assert runner.calls[1]["argv"][-1] == \
        f"repos/group/project/compare/{TARGET_SHA}...{SOURCE_SHA}"


def test_read_maps_arbitrary_author_head_fork_full_body_and_complete_revision_identity():
    item = rg_final_github.GitHubProvider(runner=FakeGh(metadata_responses())).read(PR_URL)

    assert item["id"] == "github:111#17"
    assert item["title"] == "Compose the final GitHub stack"
    assert item["description"] == pr_payload()["body"]
    assert item["state"] == "opened"
    assert item["author"] == {"username": "another-author", "name": ""}
    assert item["linked_intent"] == [
        {"key": "#88", "source": "description", "role": "direct"},
        {"key": "SPEC-16", "source": "description", "role": "direct"},
    ]
    assert item["source"] == {
        "project_id": 222,
        "project_path": "contributor/fork",
        "url": "https://github.com/contributor/fork",
        "http_url": "https://github.com/contributor/fork.git",
        "ssh_url": "git@github.com:contributor/fork.git",
        "branch": "feature/final-review",
        "sha": SOURCE_SHA,
    }
    assert item["target"] == {
        "project_id": 111,
        "project_path": "group/project",
        "url": "https://github.com/group/project",
        "http_url": "https://github.com/group/project.git",
        "ssh_url": "git@github.com:group/project.git",
        "branch": "main",
        "sha": TARGET_SHA,
        "base_sha": BASE_SHA,
    }
    assert rg_final_provider.revision_tuple(item) == (
        222, "feature/final-review", SOURCE_SHA,
        111, "main", TARGET_SHA, BASE_SHA,
    )
    assert "dependencies" not in item


def test_null_body_is_empty_intent_metadata():
    payload = pr_payload()
    payload["body"] = None

    item = rg_final_github.GitHubProvider(
        runner=FakeGh(metadata_responses(payload))).read(PR_URL)

    assert item["description"] == ""
    assert item["linked_intent"] == []


def test_revalidate_refreshes_all_metadata_when_pinned_revisions_are_unchanged():
    runner = FakeGh(metadata_responses())
    provider = rg_final_github.GitHubProvider(runner=runner)
    pinned = provider.read(PR_URL)
    runner.responses.extend(metadata_responses())

    assert provider.revalidate(pinned) == pinned
    assert len(runner.calls) == 4


@pytest.mark.parametrize("revision", [
    "source_sha", "target_sha", "base_sha", "source_branch", "target_branch",
    "source_project", "target_project",
])
def test_revalidate_rejects_head_base_retarget_and_fork_movement(revision):
    runner = FakeGh(metadata_responses())
    provider = rg_final_github.GitHubProvider(runner=runner)
    pinned = provider.read(PR_URL)
    changed = pr_payload()
    comparison = compare_payload()
    if revision == "source_sha":
        changed["head"]["sha"] = "d" * 40
    elif revision == "target_sha":
        changed["base"]["sha"] = "e" * 40
    elif revision == "base_sha":
        comparison["merge_base_commit"]["sha"] = "f" * 40
    elif revision == "source_branch":
        changed["head"]["ref"] = "feature/moved"
    elif revision == "target_branch":
        changed["base"]["ref"] = "release"
    elif revision == "source_project":
        changed["head"]["repo"] = repository_payload(333, "contributor/fork")
    else:
        changed["base"]["repo"] = repository_payload(333, "group/project")
    runner.responses.extend(metadata_responses(changed, comparison))

    with pytest.raises(rg_final_provider.RevisionChangedError,
                       match=revision.replace("_", " ")):
        provider.revalidate(pinned)


@pytest.mark.parametrize("response", [
    (None, "gh not found on PATH"),
    RuntimeError("authenticated gh is unavailable"),
])
def test_read_transport_failures_are_explicit_provider_errors(response):
    with pytest.raises(rg_final_provider.ProviderReadError):
        rg_final_github.GitHubProvider(runner=FakeGh([response])).read(PR_URL)


@pytest.mark.parametrize("raw", ["not-json", "[]"])
def test_invalid_pr_json_is_a_metadata_error(raw):
    with pytest.raises(rg_final_github.GitHubMetadataError, match="JSON"):
        rg_final_github.GitHubProvider(runner=FakeGh([(raw, None)])).read(PR_URL)


def test_missing_merge_base_is_not_replaced_with_a_branch_or_merge_commit():
    response = compare_payload()
    del response["merge_base_commit"]["sha"]

    with pytest.raises(rg_final_github.GitHubMetadataError, match="merge_base_commit.sha"):
        rg_final_github.GitHubProvider(runner=FakeGh([
            (json.dumps(pr_payload()), None), (json.dumps(response), None),
        ])).read(PR_URL)


@pytest.mark.parametrize("reference", [
    "", "0", "-1", "feature-branch", "!17",
    "https://gitlab.example.test/group/project/pull/17",
    "https://github.example.test/group/project/pull/17",
    "https://github.com/group/project/issues/17",
    "https://github.com/group/project/pull/17/files",
    "https://github.com/group/project/pull/17?diff=split",
])
def test_invalid_or_ambiguous_references_fail_before_any_gh_call(reference):
    runner = FakeGh()

    with pytest.raises(rg_final_provider.ProviderReferenceError):
        rg_final_github.GitHubProvider(runner=runner).read(reference)

    assert runner.calls == []


def test_response_number_and_target_repository_must_match_full_url():
    payload = pr_payload()
    payload["number"] = 18

    with pytest.raises(rg_final_github.GitHubMetadataError, match="does not match"):
        rg_final_github.GitHubProvider(runner=FakeGh([
            (json.dumps(payload), None),
        ])).read(PR_URL)


def test_open_pr_discovery_returns_canonical_urls_for_every_page():
    runner = FakeGh(metadata_responses())
    provider = rg_final_github.GitHubProvider(runner=runner)
    pinned = provider.read(PR_URL)
    runner.responses.extend([
        (json.dumps([{"html_url": f"https://github.com/group/project/pull/{number}"}
                     for number in range(1, 101)]), None),
        (json.dumps([{"html_url": "https://github.com/group/project/pull/101"}]), None),
    ])

    references = provider.list_open(pinned, repo_root="/checkout")

    assert len(references) == 101
    assert references[-1] == "https://github.com/group/project/pull/101"
    listings = runner.calls[2:]
    assert [call["argv"] for call in listings] == [
        ["gh", "api", "--hostname", "github.com",
         "repos/group/project/pulls?state=open&per_page=100&page=1"],
        ["gh", "api", "--hostname", "github.com",
         "repos/group/project/pulls?state=open&per_page=100&page=2"],
    ]
    assert all("--method" not in call["argv"] for call in runner.calls)
    assert all(call["cwd"] == "/checkout" for call in listings)


def stack_pr(number, source_branch, target_branch):
    payload = pr_payload()
    payload["number"] = number
    payload["html_url"] = f"https://github.com/group/project/pull/{number}"
    payload["title"] = f"Stack item {number}"
    payload["body"] = "Provider edges, not prose dependencies."
    # Every link is a same-repository branch dependency: a child's base repo
    # must be the actual repository owning its parent's head branch.
    payload["head"]["repo"] = repository_payload(111, "group/project")
    payload["head"]["ref"] = source_branch
    payload["head"]["sha"] = f"{number:040d}"
    payload["base"]["ref"] = target_branch
    payload["base"]["sha"] = f"{number + 1000:040d}"
    return payload


class StackGh:
    def __init__(self, pulls):
        self.pulls = {pull["number"]: pull for pull in pulls}
        self.calls = []

    def __call__(self, argv, cwd, timeout):
        self.calls.append(list(argv))
        endpoint = argv[-1]
        if "/pulls?" in endpoint:
            page = int(endpoint.rsplit("page=", 1)[1])
            assert page == 1
            return json.dumps([
                {"html_url": pull["html_url"]} for pull in self.pulls.values()
            ]), None
        if "/compare/" in endpoint:
            number = next(number for number, pull in self.pulls.items()
                          if pull["head"]["sha"] in endpoint)
            return json.dumps({
                "merge_base_commit": {"sha": f"{number + 2000:040d}"},
                "base_commit": {"sha": self.pulls[number]["base"]["sha"]},
            }), None
        number = int(endpoint.rsplit("/", 1)[1])
        return json.dumps(self.pulls[number]), None


def test_real_github_adapter_discovers_same_base_most_linear_graph_and_revision_tuples():
    pulls = [
        stack_pr(1, "feature/base", "main"),
        stack_pr(2, "feature/middle", "feature/base"),
        stack_pr(3, "feature/head", "feature/middle"),
    ]
    runner = StackGh(pulls)
    provider = rg_final_github.GitHubProvider(runner=runner)
    references = [pull["html_url"] for pull in pulls]

    graph = rg_final_graph.discover_linear_graph(references, provider=provider)

    assert graph["provider"] == "github"
    assert graph["item_keys"] == [f"github:111#{number}" for number in (1, 2, 3)]
    assert graph["references"] == references
    assert graph["target_project"] == {
        "project_id": 111, "project_path": "group/project",
    }
    assert graph["root_target"]["branch"] == "main"
    assert graph["command"]["argv"] == ["review-gate.py", "final-review", *references]
    assert graph["revisions"] == [{
        "item_key": f"github:111#{pull['number']}",
        "revision_tuple": [
            111, pull["head"]["ref"], pull["head"]["sha"],
            111, pull["base"]["ref"], pull["base"]["sha"],
            f"{pull['number'] + 2000:040d}",
        ],
    } for pull in pulls]
    assert all(call[:2] == ["gh", "api"] for call in runner.calls)


def test_github_open_discovery_still_rejects_omitted_ancestors_and_descendants():
    pulls = [
        stack_pr(1, "feature/base", "main"),
        stack_pr(2, "feature/middle", "feature/base"),
        stack_pr(3, "feature/head", "feature/middle"),
    ]
    provider = rg_final_github.GitHubProvider(runner=StackGh(pulls))

    with pytest.raises(rg_final_graph.GraphOmissionError) as captured:
        rg_final_graph.discover_linear_graph([pulls[2]["html_url"]], provider=provider)

    assert captured.value.omitted_ancestors == [
        "github:111#1", "github:111#2",
    ]
    assert captured.value.omitted_descendants == []
    assert captured.value.corrected_argv[-3:] == tuple(pull["html_url"] for pull in pulls)


def test_provider_auto_selection_uses_full_url_and_injected_provider_takes_precedence():
    github = object()
    gitlab = object()
    injected = object()

    assert rg_final_provider.select_provider(
        [PR_URL], gitlab_factory=lambda: gitlab, github_factory=lambda: github) is github
    assert rg_final_provider.select_provider(
        ["https://gitlab.example.test/group/project/-/merge_requests/17"],
        gitlab_factory=lambda: gitlab, github_factory=lambda: github) is gitlab
    assert rg_final_provider.select_provider(
        ["not-even-a-reference"], provider=injected,
        gitlab_factory=lambda: pytest.fail("injected provider was replaced"),
        github_factory=lambda: pytest.fail("injected provider was replaced")) is injected


def test_provider_auto_selection_rejects_mixed_forge_urls_before_transport():
    with pytest.raises(rg_final_provider.ProviderReferenceError, match="mixed"):
        rg_final_provider.select_provider([
            PR_URL, "https://gitlab.example.test/group/project/-/merge_requests/18",
        ])


def test_repository_local_selection_uses_sigil_or_github_origin_without_network(tmp_path):
    github = object()
    gitlab = object()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run([
        "git", "-C", str(tmp_path), "remote", "add", "origin",
        "git@github.com:group/project.git",
    ], check=True)

    assert rg_final_provider.select_provider(
        ["17"], repo_root=str(tmp_path),
        gitlab_factory=lambda: gitlab, github_factory=lambda: github) is github
    assert rg_final_provider.select_provider(
        ["#17"], gitlab_factory=lambda: gitlab, github_factory=lambda: github) is github
    assert rg_final_provider.select_provider(
        ["!17"], gitlab_factory=lambda: gitlab, github_factory=lambda: github) is gitlab


def test_providerless_graph_discovery_selects_github_from_references():
    pulls = [stack_pr(1, "feature/base", "main")]
    runner = StackGh(pulls)
    original = rg_final_github.GitHubProvider

    class Factory(original):
        def __init__(self):
            super().__init__(runner=runner)

    previous = rg_final_github.GitHubProvider
    rg_final_github.GitHubProvider = Factory
    try:
        graph = rg_final_graph.discover_linear_graph([pulls[0]["html_url"]])
    finally:
        rg_final_github.GitHubProvider = previous

    assert graph["provider"] == "github"
    assert graph["item_keys"] == ["github:111#1"]


ORIGINAL_GITLAB_STACK = gitlab_publication.stack


def github_stack(*items):
    """Adapt the mature publication fixture instead of duplicating its store setup."""
    data = ORIGINAL_GITLAB_STACK(*items)
    data["provider"] = "github"
    for item in data["items"]:
        number = item["item_key"]
        item["reference"] = f"https://github.com/group/project/pull/{number}"
        item["source_project"] = "fork/project"
        item["target_project"] = "group/project"
        provider_data = item["provider_data"]
        provider_data["provider"] = "github"
        provider_data["id"] = f"github:111#{number}"
        provider_data["url"] = item["reference"]
        provider_data["source"].update({
            "project_id": 222, "project_path": "fork/project",
        })
        provider_data["target"].update({
            "project_id": 111, "project_path": "group/project",
        })
    return data


def ready_github_run(store, monkeypatch, **kwargs):
    monkeypatch.setattr(gitlab_publication, "stack", github_stack)
    return gitlab_publication.ready_run(store, **kwargs)


def test_trusted_github_publication_uses_source_fork_status_api_and_same_identity(
        store, monkeypatch):
    identity = ready_github_run(store, monkeypatch)
    transport = ScriptedGhWrite((json.dumps({
        "id": 812, "state": "success", "context": STATUS_CONTEXT,
    }), None))

    result = gitlab_publication.publish_individual(
        store, transport, identity, report_url=gitlab_publication.REPORT_URL)

    assert result["status"] == "published"
    assert result["provider_state"] == "success"
    assert result["official_green"] is True
    assert result["decision"]["finding_verdict"] == "pass"
    assert result["identity"]["source_project"] == 222
    assert result["identity"]["item_key"] == "123"
    call = transport.calls[0]
    assert call["argv"][:3] == ["gh", "api", "--hostname"]
    assert "--method" in call["argv"]
    assert call["argv"][call["argv"].index("--method") + 1] == "POST"
    assert f"repos/fork/project/statuses/{gitlab_publication.SOURCE}" in call["argv"]
    assert transport.fields["state"] == "success"
    assert transport.fields["context"] == STATUS_CONTEXT
    assert transport.fields["target_url"] == gitlab_publication.REPORT_URL
    for expected in (
        identity["run_id"], identity["unit_ids"]["individual:1"],
        store.store_instance_id, gitlab_publication.SOURCE, "individual:1",
    ):
        assert expected in transport.fields["description"]


@pytest.mark.parametrize("state", ["findings", "stale", "incomplete"])
def test_github_non_pass_maps_to_failure_never_success(store, monkeypatch, state):
    identity = ready_github_run(store, monkeypatch, state=state)
    transport = ScriptedGhWrite((json.dumps({
        "id": 813, "state": "failure", "context": STATUS_CONTEXT,
    }), None))

    result = gitlab_publication.publish_individual(store, transport, identity)

    assert result["decision"]["can_publish_pass"] is False
    assert result["provider_state"] == "failure"
    assert result["official_green"] is False
    assert transport.fields["state"] == "failure"


@pytest.mark.parametrize("response", [
    (None, "403 Forbidden: commit status permission denied"),
    RuntimeError("gh transport unavailable"),
    ("not-json", None),
], ids=["permission", "exception", "invalid-response"])
def test_github_write_failure_preserves_local_only_complete_evidence(
        store, monkeypatch, response):
    identity = ready_github_run(store, monkeypatch)
    transport = ScriptedGhWrite(response)

    result = gitlab_publication.publish_individual(store, transport, identity)

    assert result["status"] == "local_only"
    assert result["official_green"] is False
    assert result["provider_state"] is None
    assert result["error"]
    report = store.machine_report(identity["run_id"])
    assert report["run"]["state"] == "pass"
    assert report["trust"]["local_only"] is True
    assert report["trust"]["enforced"] is False
    assert report["trust"]["official"] is False
    assert len(report["publications"]) == 1


def test_github_old_run_guard_prevents_late_status_write(store, monkeypatch):
    old = ready_github_run(store, monkeypatch, continued=False)
    new = ready_github_run(store, monkeypatch, continued=False)
    old_transport = ScriptedGhWrite()
    new_transport = ScriptedGhWrite()

    old_result = gitlab_publication.publish_individual(store, old_transport, old)
    new_result = gitlab_publication.publish_individual(store, new_transport, new)

    assert old_result["status"] == "superseded"
    assert old_transport.calls == []
    assert new_result["official_green"] is True
    assert len(new_transport.calls) == 1


def test_untrusted_github_caller_cannot_access_status_transport(store, monkeypatch):
    identity = ready_github_run(store, monkeypatch)
    transport = ScriptedGhWrite()
    from rg_final_publish import FinalPublisher

    result = FinalPublisher(
        store, runner=transport, trusted_orchestrator=False,
    ).publish_individual(identity["run_id"], identity["unit_ids"]["individual:1"])

    assert result["status"] == "local_only"
    assert result["official_green"] is False
    assert transport.calls == []
