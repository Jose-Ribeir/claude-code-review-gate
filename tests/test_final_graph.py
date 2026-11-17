"""Linear open-MR discovery through the provider/graph public seam."""
import copy
import json
import os
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)

import rg_final_gitlab  # noqa: E402
import rg_final_graph  # noqa: E402
import rg_final_provider  # noqa: E402


def linear_item(iid, source_branch, target_branch, *, project_id=111):
    source_sha = f"{iid:040d}"
    target_sha = f"{iid + 1000:040d}"
    return {
        "provider": "gitlab",
        "id": f"gitlab:{project_id}!{iid}",
        "iid": iid,
        "url": f"https://gitlab.example.test/group/project/-/merge_requests/{iid}",
        "title": f"Item {iid}",
        "description": "Arbitrary author intent, not a dependency declaration",
        "state": "opened",
        "author": {"username": f"author-{iid}", "name": "Another Author"},
        "source": {
            "project_id": 222,
            "project_path": "contributor/fork",
            "url": "https://gitlab.example.test/contributor/fork",
            "http_url": "https://gitlab.example.test/contributor/fork.git",
            "ssh_url": "git@gitlab.example.test:contributor/fork.git",
            "branch": source_branch,
            "sha": source_sha,
        },
        "target": {
            "project_id": project_id,
            "project_path": "group/project",
            "url": "https://gitlab.example.test/group/project",
            "http_url": "https://gitlab.example.test/group/project.git",
            "ssh_url": "git@gitlab.example.test:group/project.git",
            "branch": target_branch,
            "sha": target_sha,
            "base_sha": f"{iid + 2000:040d}",
        },
        "linked_intent": [],
    }


class ScriptedOpenProvider:
    def __init__(self, items):
        self.items = {item["id"]: copy.deepcopy(item) for item in items}
        self.reads = []
        self.list_calls = []

    def read(self, reference, repo_root=None):
        self.reads.append((reference, repo_root))
        for item in self.items.values():
            if reference in (item["url"], str(item["iid"]), f"!{item['iid']}", item["id"]):
                return copy.deepcopy(item)
        raise AssertionError(f"unexpected provider read: {reference}")

    def list_open(self, pinned, repo_root=None):
        self.list_calls.append((pinned["id"], repo_root))
        return [item["url"] for item in self.items.values()]


def test_single_item_preserves_provider_metadata_and_exposes_composition_identity(tmp_path):
    item = linear_item(1, "feature/base", "main")
    original = copy.deepcopy(item)
    provider = ScriptedOpenProvider([item])

    graph = rg_final_graph.discover_linear_graph(
        [f"  {item['url']}/  "], provider=provider, repo_root=tmp_path)

    assert graph["schema"] == "final-review-graph/v1"
    assert graph["items"] == [original]
    assert graph["item_keys"] == [item["id"]]
    assert graph["references"] == [item["url"]]
    assert graph["root_target"] == item["target"]
    assert graph["target_project"] == {
        "project_id": 111,
        "project_path": "group/project",
    }
    assert graph["revisions"] == [{
        "item_key": item["id"],
        "revision_tuple": [
            222, "feature/base", item["source"]["sha"],
            111, "main", item["target"]["sha"], item["target"]["base_sha"],
        ],
    }]
    assert graph["command"]["argv"] == ["review-gate.py", "final-review", item["url"]]
    assert provider.list_calls == [(item["id"], tmp_path)]


def three_items():
    return [
        linear_item(1, "feature/base", "main"),
        linear_item(2, "feature/middle", "feature/base"),
        linear_item(3, "feature/head", "feature/middle"),
    ]


def test_omitted_ancestors_abort_with_complete_base_most_corrected_command():
    items = three_items()
    provider = ScriptedOpenProvider(items)

    with pytest.raises(rg_final_graph.GraphOmissionError) as captured:
        rg_final_graph.discover_linear_graph([items[2]["url"]], provider=provider)

    error = captured.value
    assert error.code == "omitted-items"
    assert error.omitted_ancestors == [items[0]["id"], items[1]["id"]]
    assert error.omitted_descendants == []
    assert [item["iid"] for item in error.omitted_items] == [1, 2]
    assert error.corrected_argv == (
        "review-gate.py", "final-review",
        items[0]["url"], items[1]["url"], items[2]["url"],
    )
    assert error.corrected_command == (
        "review-gate.py final-review " + " ".join(item["url"] for item in items)
    )
    assert error.corrected_command in str(error)


def test_omitted_descendants_abort_with_complete_base_most_corrected_command():
    items = three_items()
    provider = ScriptedOpenProvider(items)

    with pytest.raises(rg_final_graph.GraphOmissionError) as captured:
        rg_final_graph.discover_linear_graph([items[0]["url"]], provider=provider)

    error = captured.value
    assert error.omitted_ancestors == []
    assert error.omitted_descendants == [items[1]["id"], items[2]["id"]]
    assert error.corrected_argv[-3:] == tuple(item["url"] for item in items)


def test_complete_linear_stack_is_returned_in_base_most_order_without_mutating_items():
    items = three_items()
    provider = ScriptedOpenProvider(items)

    graph = rg_final_graph.discover_linear_graph(
        [item["url"] for item in items], provider=provider)

    assert [item["iid"] for item in graph["items"]] == [1, 2, 3]
    assert graph["item_keys"] == [item["id"] for item in items]
    assert graph["root_target"]["branch"] == "main"
    assert [entry["item_key"] for entry in graph["revisions"]] == graph["item_keys"]
    assert graph["command"]["text"].endswith(" ".join(item["url"] for item in items))


@pytest.mark.parametrize("references", [
    lambda items: [items[0]["url"], items[1]["url"], items[0]["url"]],
    lambda items: [items[0]["url"], "1", items[1]["url"]],
])
def test_duplicate_explicit_references_are_rejected(references):
    items = three_items()
    provider = ScriptedOpenProvider(items)

    with pytest.raises(rg_final_graph.DuplicateReferenceError) as captured:
        rg_final_graph.discover_linear_graph(references(items), provider=provider)

    assert captured.value.code == "duplicate-reference"
    assert items[0]["id"] in str(captured.value)


def test_inconsistent_explicit_order_is_never_silently_reordered():
    items = three_items()
    provider = ScriptedOpenProvider(items)

    with pytest.raises(rg_final_graph.GraphOrderError) as captured:
        rg_final_graph.discover_linear_graph(
            [items[0]["url"], items[2]["url"], items[1]["url"]], provider=provider)

    assert captured.value.code == "inconsistent-order"
    assert captured.value.expected == [item["id"] for item in items]
    assert captured.value.actual == [items[0]["id"], items[2]["id"], items[1]["id"]]


def test_omitted_middle_item_is_an_explicit_ancestor_omission_not_a_reorder():
    items = three_items()
    provider = ScriptedOpenProvider(items)

    with pytest.raises(rg_final_graph.GraphOmissionError) as captured:
        rg_final_graph.discover_linear_graph(
            [items[0]["url"], items[2]["url"]], provider=provider)

    assert captured.value.omitted_ancestors == [items[1]["id"]]
    assert captured.value.corrected_argv[-3:] == tuple(item["url"] for item in items)


def test_gap_between_supplied_items_cannot_be_filled_by_inference():
    first, _, third = three_items()
    provider = ScriptedOpenProvider([first, third])

    with pytest.raises(rg_final_graph.GraphGapError) as captured:
        rg_final_graph.discover_linear_graph([first["url"], third["url"]], provider=provider)

    assert captured.value.code == "graph-gap"


def test_provider_branch_cycle_is_rejected():
    first = linear_item(1, "feature/one", "feature/two")
    second = linear_item(2, "feature/two", "feature/one")
    provider = ScriptedOpenProvider([first, second])

    with pytest.raises(rg_final_graph.GraphCycleError) as captured:
        rg_final_graph.discover_linear_graph([first["url"], second["url"]], provider=provider)

    assert captured.value.code == "cycle"


def test_multiple_target_projects_are_rejected_before_open_discovery():
    first, second, _ = three_items()
    second = copy.deepcopy(second)
    second["id"] = "gitlab:999!2"
    second["target"]["project_id"] = 999
    provider = ScriptedOpenProvider([first, second])

    with pytest.raises(rg_final_graph.MultipleTargetError) as captured:
        rg_final_graph.discover_linear_graph([first["url"], second["url"]], provider=provider)

    assert captured.value.code == "multiple-target-projects"
    assert provider.list_calls == []


def test_disconnected_independent_root_targets_are_rejected():
    first = linear_item(1, "feature/one", "main")
    second = linear_item(2, "feature/two", "release")
    provider = ScriptedOpenProvider([first, second])

    with pytest.raises(rg_final_graph.RootTargetError) as captured:
        rg_final_graph.discover_linear_graph([first["url"], second["url"]], provider=provider)

    assert captured.value.code == "multiple-root-targets"


def test_branch_points_are_rejected_even_when_one_branch_was_not_supplied():
    first, second, _ = three_items()
    alternate = linear_item(4, "feature/alternate", "feature/base")
    provider = ScriptedOpenProvider([first, second, alternate])

    with pytest.raises(rg_final_graph.BranchPointError) as captured:
        rg_final_graph.discover_linear_graph([first["url"], second["url"]], provider=provider)

    assert captured.value.code == "branch-point"


def test_prose_only_dependency_does_not_create_a_graph_edge():
    first = linear_item(1, "feature/base", "main")
    second = linear_item(2, "feature/unrelated", "main",
                         )
    second["description"] = "Depends on !1; please compose this after the base change."
    provider = ScriptedOpenProvider([first, second])

    with pytest.raises(rg_final_graph.ProseDependencyError) as captured:
        rg_final_graph.discover_linear_graph([first["url"], second["url"]], provider=provider)

    assert captured.value.code == "prose-only-dependency"
    assert "provider source/target edges" in str(captured.value)


class ScriptedGlab:
    def __init__(self, items):
        self.items = {item["iid"]: item for item in items}
        self.calls = []

    def __call__(self, argv, cwd, timeout):
        self.calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout})
        endpoint = argv[-1]
        if "merge_requests?state=opened" in endpoint:
            return json.dumps([{"web_url": item["url"]} for item in self.items.values()]), None
        if "/merge_requests/" in endpoint:
            iid = int(endpoint.rsplit("/", 1)[1])
            item = self.items[iid]
            return json.dumps({
                "iid": iid,
                "title": item["title"],
                "description": item["description"],
                "state": item["state"],
                "web_url": item["url"],
                "author": item["author"],
                "source_project_id": item["source"]["project_id"],
                "target_project_id": item["target"]["project_id"],
                "source_branch": item["source"]["branch"],
                "target_branch": item["target"]["branch"],
                "sha": item["source"]["sha"],
                "diff_refs": {
                    "base_sha": item["target"]["base_sha"],
                    "head_sha": item["source"]["sha"],
                    "start_sha": item["target"]["sha"],
                },
            }), None
        project_id = int(endpoint.rsplit("/", 1)[1])
        side = next(item["source"] if project_id == item["source"]["project_id"]
                    else item["target"] for item in self.items.values()
                    if project_id in (item["source"]["project_id"], item["target"]["project_id"]))
        return json.dumps({
            "id": project_id,
            "path_with_namespace": side["project_path"],
            "web_url": side["url"],
            "http_url_to_repo": side["http_url"],
            "ssh_url_to_repo": side["ssh_url"],
        }), None


def test_real_gitlab_adapter_discovers_open_references_with_read_only_paginated_api():
    items = three_items()[:2]
    runner = ScriptedGlab(items)
    provider = rg_final_gitlab.GitLabProvider(runner=runner)

    graph = rg_final_graph.discover_linear_graph(
        [item["url"] for item in items], provider=provider, repo_root="/checkout")

    assert graph["item_keys"] == [item["id"] for item in items]
    listing = next(call for call in runner.calls if "state=opened" in call["argv"][-1])
    assert listing == {
        "argv": [
            "glab", "api", "--hostname", "gitlab.example.test",
            "projects/111/merge_requests?state=opened&scope=all&per_page=100&page=1",
        ],
        "cwd": "/checkout",
        "timeout": rg_final_gitlab.FETCH_TIMEOUT,
    }
    assert all(call["argv"][:2] == ["glab", "api"] for call in runner.calls)
    assert graph["items"][0]["author"] == items[0]["author"]
    assert graph["items"][0]["source"]["project_path"] == "contributor/fork"


def test_supplied_non_open_item_is_not_promoted_into_the_open_graph():
    item = linear_item(1, "feature/base", "main")
    item["state"] = "merged"
    provider = ScriptedOpenProvider([item])

    with pytest.raises(rg_final_graph.GraphValidationError) as captured:
        rg_final_graph.discover_linear_graph([item["url"]], provider=provider)

    assert captured.value.code == "item-not-open"
    assert provider.list_calls == []


def test_open_listing_transport_failure_remains_a_typed_provider_error():
    item = linear_item(1, "feature/base", "main")
    scripted = ScriptedOpenProvider([item])
    item_from_provider = scripted.read(item["url"])

    def unavailable(argv, cwd, timeout):
        return None, "403 Forbidden"

    provider = rg_final_gitlab.GitLabProvider(runner=unavailable)
    with pytest.raises(rg_final_provider.ProviderReadError, match="403 Forbidden"):
        rg_final_provider.list_open_references(provider, item_from_provider)
