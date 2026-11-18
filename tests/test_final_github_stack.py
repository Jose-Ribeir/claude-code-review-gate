"""Standalone and three-item GitHub reviews on the shared final-review harness.

The mature GitLab stack fixture supplies the real Git graph, worker, required
and dynamic runners. This module only translates provider metadata and the
scripted commit-status transport.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rg_final_github  # noqa: E402
import rg_final_review  # noqa: E402
from rg_final_graph import GraphOmissionError  # noqa: E402
from rg_final_publish import STATUS_CONTEXT  # noqa: E402
from rg_final_store import FinalStore  # noqa: E402
import test_final_stack as shared  # noqa: E402


def github_item(item):
    item = deepcopy(item)
    number = item["iid"]
    item.update({
        "provider": "github",
        "id": f"github:111#{number}",
        "url": f"https://github.com/group/project/pull/{number}",
    })
    item["source"].update({
        "project_id": 111, "project_path": "group/project",
    })
    item["target"].update({
        "project_id": 111, "project_path": "group/project",
    })
    return item


class GitHubStatusWriter:
    def __init__(self, response=None):
        self.calls = []
        self.response = response

    def __call__(self, argv, cwd, timeout):
        self.calls.append(list(argv))
        if self.response is not None:
            if isinstance(self.response, BaseException):
                raise self.response
            return self.response
        return json.dumps({
            "id": 9000 + len(self.calls), "state": "success",
            "context": STATUS_CONTEXT,
        }), None


class Harness(shared.Harness):
    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.items = [github_item(item) for item in self.items]
        self.provider = shared.Provider(self.items)
        self.writer = GitHubStatusWriter()

    def standalone(self, store_path, **kwargs):
        options = {
            "provider": self.provider,
            "repo_root": self.repo,
            "store_path": store_path,
            "worker": [sys.executable, str(self.worker)],
            "environ": self.environ,
            "required_runner": self.required,
            "dynamic_runner": self.dynamic,
            "context_kwargs": {"intent_fetcher": self.intent},
        }
        options.update(kwargs)
        return rg_final_review.run_final_review(self.references[0], **options)


def auto_provider(monkeypatch, harness):
    """Exercise reference dispatch while keeping every provider read scripted."""
    monkeypatch.setattr(rg_final_github, "GitHubProvider", lambda: harness.provider)


def test_one_standalone_github_pr_runs_individual_combined_and_status_on_shared_harness(
        tmp_path, monkeypatch):
    harness = Harness(tmp_path)
    auto_provider(monkeypatch, harness)
    store_path = tmp_path / "standalone.sqlite3"

    with pytest.raises(GraphOmissionError) as omitted:
        harness.standalone(store_path, provider=None)
    assert omitted.value.corrected_argv[-3:] == tuple(harness.references)
    assert harness.writer.calls == []

    first = harness.run(store_path, provider=None)
    assert first["state"] == "pass"
    assert first["graph"]["references"] == harness.references
    output = harness.run(
        store_path, provider=None, publish=True, publication_runner=harness.writer)

    assert output["stage"] == "S16-07"
    assert output["complete"] is True
    assert output["state"] == "pass"
    assert output["exit_code"] == 0
    assert output["finding_verdict"] == "pass"
    assert output["graph"]["references"] == harness.references
    assert output["item_keys"] == [f"github:111#{number}" for number in (1, 2, 3)]
    assert [unit["unit_key"] for unit in output["units"]] == [
        "individual:1", "individual:2", "individual:3", "combined", "prospective"]
    assert all(unit["execution_complete"] for unit in output["units"])
    assert len(output["publications"]) == 3
    assert all(publication["official_green"] for publication in output["publications"])
    assert len(harness.writer.calls) == 3
    for item, argv in zip(harness.items, harness.writer.calls):
        assert argv[:2] == ["gh", "api"]
        assert f"repos/group/project/statuses/{item['source']['sha']}" in argv
        assert "state=success" in argv
        assert f"context={STATUS_CONTEXT}" in argv
    with FinalStore(store_path) as store:
        report = store.machine_report(output["run_id"])
        assert [entry["provider_data"]["url"] for entry in report["revisions"]["items"]] == \
            harness.references
        assert all(entry["provider_data"]["provider"] == "github"
                   for entry in report["revisions"]["items"])
        assert len(report["publications"]) == 3


def test_three_github_prs_run_five_units_and_publish_each_head_on_shared_harness(
        tmp_path, monkeypatch):
    harness = Harness(tmp_path)
    auto_provider(monkeypatch, harness)
    store_path = tmp_path / "stack.sqlite3"

    first = harness.run(store_path, provider=None)
    assert first["state"] == "pass"
    harness.required_calls.clear()
    harness.dynamic_calls.clear()
    output = harness.run(
        store_path, provider=None, publish=True, publication_runner=harness.writer)

    assert output["state"] == "pass"
    assert output["complete"] is True
    assert output["exit_code"] == 0
    assert output["execution_complete"] is True
    assert output["graph"]["references"] == harness.references
    assert output["item_keys"] == [f"github:111#{n}" for n in (1, 2, 3)]
    assert [unit["unit_key"] for unit in output["units"]] == [
        "individual:1", "individual:2", "individual:3", "combined", "prospective",
    ]
    assert all(unit["state"] == "pass" for unit in output["units"])
    assert len(harness.required_calls) == 5
    assert len(harness.dynamic_calls) == 5
    assert len(output["publications"]) == 3
    assert all(publication["official_green"] for publication in output["publications"])
    assert len(harness.writer.calls) == 3
    for item, argv in zip(harness.items, harness.writer.calls):
        assert argv[:2] == ["gh", "api"]
        assert f"repos/group/project/statuses/{item['source']['sha']}" in argv
        assert "state=success" in argv
        assert f"context={STATUS_CONTEXT}" in argv
    with FinalStore(store_path) as store:
        report = store.machine_report(output["run_id"])
        assert report["run"]["state"] == "pass"
        assert len(report["publications"]) == 3
        assert report["finding_memory"]["clear_allowed"] is True


def test_composed_worker_resolution_closes_only_its_inherited_unit_finding(tmp_path):
    harness = Harness(tmp_path)
    store_path = tmp_path / "composed-resolution.sqlite3"
    harness.environ["STACK_FINDING_UNIT"] = "combined"
    first = harness.run(store_path)
    assert first["state"] == "findings"
    harness.environ.pop("STACK_FINDING_UNIT")

    trace = tmp_path / "resolution-trace.jsonl"
    harness.environ["RESOLUTION_TRACE"] = str(trace)
    worker = tmp_path / "resolution-worker.py"
    worker.write_text(
        "import json, os, pathlib, sys\n"
        "request = json.loads(pathlib.Path(sys.argv[-1]).read_text())\n"
        "prior = request['finding_memory']['findings']\n"
        "with pathlib.Path(os.environ['RESOLUTION_TRACE']).open('a') as stream:\n"
        "    stream.write(json.dumps({'unit_key': request['unit_key'],"
        " 'lens': request['lens'], 'memory_required': bool(prior),"
        " 'pending': prior[0] if prior and request['unit_key'] == 'combined'"
        " and request['lens'] == 'correctness' else None}) + '\\n')\n"
        "result = {'schema': 'final-review-worker-result/v1', 'status': 'success',"
        " 'summary': request['lens'] + ' complete', 'findings': [],"
        " 'warnings': [], 'unreviewed': []}\n"
        "if prior and request['unit_key'] == 'combined'"
        " and request['lens'] == 'correctness':\n"
        "    pending = prior[0]\n"
        "    anchors = pending['anchors']\n"
        "    for anchor in anchors: anchor['evidence_fingerprint'] = 'ignored-worker-claim'\n"
        "    head_sha = request['repository']['head_sha']\n"
        "    body = (pathlib.Path(request['repository']['path']) / 'app.txt')"
        ".read_text().splitlines()[1]\n"
        "    source = {'path': 'app.txt', 'start_line': 2, 'end_line': 2,"
        " 'body': body, 'unit_key': 'combined', 'head_sha': head_sha}\n"
        "    result['finding_resolutions'] = [{"
        " 'finding_id': pending['finding_id'],"
        " 'content_anchor': pending['content_anchor'],"
        " 'thread_id': pending['thread_id'], 'anchors': anchors,"
        " 'resolution_evidence': {'claim': 'composition caller contract restored',"
        " 'source': source},"
        " 'revalidation': {'test': 'stack-required', 'state': 'pass',"
        " 'output': 'required OK', 'evidence_fingerprint': 'required-' + head_sha,"
        " 'unit_key': 'combined', 'head_sha': head_sha}}]\n"
        "print(json.dumps(result))\n",
        encoding="utf-8",
    )

    second = harness.run(
        store_path, worker=[sys.executable, str(worker)], environ=harness.environ)

    assert second["state"] == "pass"
    assert second["complete"] is True
    assert second["finding_memory"]["clear_allowed"] is True
    recorded = [entry for entry in second["finding_closures"] if entry["state"] == "recorded"]
    assert len(recorded) == 1
    assert recorded[0]["event_type"] == "resolved"
    traces = [json.loads(line) for line in trace.read_text().splitlines()]
    assert len(traces) == 5 * 7
    combined = [entry for entry in traces if entry["unit_key"] == "combined"]
    assert len(combined) == 7
    assert all(entry["memory_required"] for entry in combined)
    assert not any(entry["memory_required"] for entry in traces
                   if entry["unit_key"].startswith("individual:"))
    pending = next(entry["pending"] for entry in combined if entry["pending"])
    with FinalStore(store_path) as store:
        history = store.finding_history(pending["thread_id"])
        assert [event["event_type"] for event in history["events"]] == [
            "observed", "resolved"]
        assert history["events"][-1]["revalidation"]["test"] == "stack-required"
        assert history["events"][-1]["resolution_evidence"]["source"]["body"] == "item 1"
        assert store.official_findings(second["run_id"]) == []


@pytest.mark.parametrize("blocked_key", ["individual:1", "individual:2", "individual:3", "combined"])
def test_github_stack_blocks_every_pr_status_when_any_required_unit_blocks(
        tmp_path, blocked_key):
    harness = Harness(tmp_path)
    harness.environ["STACK_FINDING_UNIT"] = blocked_key

    output = harness.run(
        tmp_path / f"{blocked_key.replace(':', '-')}.sqlite3",
        publish=True, publication_runner=harness.writer)

    assert output["state"] == "findings"
    assert output["finding_verdict"] == "block"
    assert output["complete"] is True
    assert harness.writer.calls == []
    assert output["publications"] == []
    assert len(output["items"]) == 3
    assert all(not item["official_green"] for item in output["items"])
    assert all(not item["green_allowed"] for item in output["items"])
    assert all("units-not-pass" in item["publication_decision"]["green_blockers"]
               for item in output["items"])


def test_github_permission_loss_leaves_stack_pass_local_and_unenforced(tmp_path):
    harness = Harness(tmp_path)
    store_path = tmp_path / "permission.sqlite3"
    harness.run(store_path)
    harness.writer.response = (None, "403 Forbidden: commit statuses write denied")

    output = harness.run(store_path, publish=True, publication_runner=harness.writer)

    assert output["state"] == "pass"
    assert output["complete"] is True
    assert output["exit_code"] == 0
    assert len(output["publications"]) == 3
    assert all(publication["status"] == "local_only" for publication in output["publications"])
    assert all(not publication["official_green"] for publication in output["publications"])
    assert output["trust"]["local_only"] is True
    assert output["trust"]["official"] is False
    # The first permission failure degrades trusted publication; subsequent
    # per-PR attempts remain local-only without further provider writes.
    assert len(harness.writer.calls) == 1


@pytest.mark.parametrize("entrypoint", [
    rg_final_review.run_final_stack,
    rg_final_review.execute_final_stack,
])
def test_all_legacy_stack_entrypoints_auto_select_github_before_graph(
        tmp_path, monkeypatch, entrypoint):
    harness = Harness(tmp_path)
    auto_provider(monkeypatch, harness)
    observed = []

    def stop_at_graph(references, *, provider, repo_root=None):
        observed.append((list(references), provider, repo_root))
        raise RuntimeError("stop after provider dispatch")

    monkeypatch.setattr(rg_final_review, "discover_linear_graph", stop_at_graph)
    with pytest.raises(RuntimeError, match="stop after provider dispatch"):
        entrypoint(harness.references, repo_root=harness.repo, environ={})

    assert observed == [(harness.references, harness.provider, str(harness.repo))]
