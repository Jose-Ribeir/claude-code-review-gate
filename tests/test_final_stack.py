"""Complete GitLab stack orchestration through the public multi-ref seam.

All providers, workers, verification stages, and publication transports are
scripted. Every review tree comes from a real temporary Git graph.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rg_final_review  # noqa: E402
from rg_final_lenses import (  # noqa: E402
    CROSS_ITEM_CONTRACTS,
    INTEGRATION_BEHAVIOR,
    REQUIRED_LENS_NAMES,
)
from rg_final_publish import STATUS_CONTEXT  # noqa: E402
from rg_final_store import FinalStore  # noqa: E402


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        timeout=30, check=True,
    )


def commit(repo, message):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def item(iid, branch, target_branch, head, target, repo):
    return {
        "provider": "gitlab",
        "id": f"gitlab:111!{iid}",
        "iid": iid,
        "url": f"https://gitlab.example.test/group/project/-/merge_requests/{iid}",
        "title": f"Stack item {iid}",
        "description": f"Implement stack item {iid} without breaking its caller.",
        "state": "opened",
        "author": {"username": "stack-author"},
        "linked_intent": [],
        "commit_messages": [f"item {iid}"],
        "source": {
            "project_id": 111, "project_path": "group/project",
            "url": str(repo), "http_url": str(repo),
            "branch": branch, "sha": head,
        },
        "target": {
            "project_id": 111, "project_path": "group/project",
            "url": str(repo), "http_url": str(repo),
            "branch": target_branch, "sha": target, "base_sha": target,
        },
    }


def repository(tmp_path):
    """Build root <- one <- two <- three, plus a moved current root."""
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    for key, value in (
        ("user.name", "Final Stack Test"),
        ("user.email", "final-stack@example.test"),
        ("commit.gpgsign", "false"),
    ):
        git(repo, "config", key, value)
    (repo / "app.txt").write_text("root\n", encoding="utf-8")
    (repo / "README.md").write_text("# Stack fixture\n", encoding="utf-8")
    root_sha = commit(repo, "root")

    items = []
    previous_sha = root_sha
    target_branch = "main"
    for position in range(1, 4):
        branch = f"feature/{position}"
        git(repo, "checkout", "-q", "-b", branch)
        (repo / "app.txt").write_text(
            "root\n" + "".join(f"item {number}\n" for number in range(1, position + 1)),
            encoding="utf-8",
        )
        (repo / f"item-{position}.txt").write_text(f"item {position}\n", encoding="utf-8")
        head_sha = commit(repo, f"item {position}")
        items.append(item(position, branch, target_branch, head_sha, previous_sha, repo))
        previous_sha = head_sha
        target_branch = branch

    git(repo, "checkout", "-q", "main")
    (repo / "current.txt").write_text("current target\n", encoding="utf-8")
    current_sha = commit(repo, "current root")
    return repo, items, current_sha


class Provider:
    def __init__(self, items):
        self.items = {entry["url"]: deepcopy(entry) for entry in items}
        self.reads = []
        self.revalidations = []

    def read(self, reference, repo_root=None):
        self.reads.append(reference)
        return deepcopy(self.items[reference])

    def list_open(self, pinned, repo_root=None):
        return list(self.items)

    def revalidate(self, pinned, repo_root=None):
        self.revalidations.append(pinned["id"])
        return self.read(pinned["url"], repo_root=repo_root)


def worker_script(tmp_path):
    path = tmp_path / "stack-worker.py"
    path.write_text(
        "import json, os, pathlib, sys\n"
        "request = json.loads(pathlib.Path(sys.argv[-1]).read_text())\n"
        "with pathlib.Path(os.environ['STACK_TRACE']).open('a') as stream:\n"
        "    stream.write(json.dumps({'unit_key': request['unit_key'],"
        " 'lens': request['lens'], 'capabilities': request['capabilities'],"
        " 'repository_path': request['repository']['path'],"
        " 'context': request['review_context']['contract'],"
        " 'memory_required': request['finding_memory']['required']}) + '\\n')\n"
        "findings = []\n"
        "if os.environ.get('STACK_FINDING_UNIT') == request['unit_key'] and"
        " request['lens'] == 'correctness':\n"
        "    findings.append({'id': 'stack-defect-' + request['unit_key'],"
        " 'path': 'app.txt', 'start_line': 2, 'end_line': 2,"
        " 'severity': 'high', 'confidence': 0.99,"
        " 'content': 'The reviewed unit breaks its caller.',"
        " 'evidence': 'The unchanged caller requires the previous contract.'})\n"
        "stop = os.environ.get('STACK_STOP_UNIT') == request['unit_key'] and"
        " request['lens'] == 'correctness'\n"
        "result = {'schema': 'final-review-worker-result/v1',"
        " 'status': 'STOP' if stop else 'success',"
        " 'summary': request['lens'] + ' complete',"
        " 'findings': findings, 'warnings': [], 'unreviewed': []}\n"
        "if stop:\n"
        "    result['stop'] = {'reason_code': 'unsafe-sandbox',"
        " 'reason': 'The credential-free boundary was demonstrably lost.',"
        " 'category': 'unsafe', 'evidence': {'credential_free': False},"
        " 'finding': {'id': 'stack-stop-' + request['unit_key'],"
        " 'path': 'app.txt', 'start_line': 2, 'end_line': 2,"
        " 'severity': 'high', 'confidence': 0.99,"
        " 'content': 'Dependent evidence is unsafe after the boundary loss.',"
        " 'evidence': 'A provider credential crossed the worker boundary.'}}\n"
        "print(json.dumps(result))\n",
        encoding="utf-8",
    )
    return path


def required_result(commit_sha):
    return {
        "status": "pass", "blocking": False, "commit": commit_sha,
        "suites": [{
            "name": "stack-required", "command": "scripted required suite",
            "source": ".ocr/config.json", "state": "pass", "blocking": False,
            "output": "required OK", "evidence_fingerprint": "required-" + commit_sha,
            "attempts": [{"returncode": 0}],
        }],
        "findings": [], "warnings": [],
    }


def dynamic_result(unit_key, target_sha, head_sha):
    return {
        "schema": "final-review-dynamic-result/v1", "unit_key": unit_key,
        "state": "not_applicable", "complete": True, "findings": [],
        "warnings": [], "tests": [],
        "coverage": {"status": "not_applicable", "summary": "No dynamic probe applies"},
        "telemetry": {"executor_attempts": 1},
        "revisions": {
            "target_sha": target_sha, "head_sha": head_sha, "merge_base_sha": target_sha,
        },
    }


class StatusWriter:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, cwd, timeout):
        self.calls.append(list(argv))
        number = len(self.calls) + 8000
        return json.dumps({"id": number, "name": STATUS_CONTEXT}), None


class Harness:
    def __init__(self, tmp_path):
        self.repo, self.items, self.current_sha = repository(tmp_path)
        self.provider = Provider(self.items)
        self.trace = tmp_path / "worker-trace.jsonl"
        self.worker = worker_script(tmp_path)
        self.required_calls = []
        self.dynamic_calls = []
        self.writer = StatusWriter()
        self.environ = {**os.environ, "STACK_TRACE": str(self.trace)}

    @property
    def references(self):
        return [entry["url"] for entry in self.items]

    def required(self, repository_path, commit_sha):
        self.required_calls.append((Path(repository_path), commit_sha))
        return required_result(commit_sha)

    def dynamic(self, repository_path, target_sha, head_sha, **kwargs):
        self.dynamic_calls.append((Path(repository_path), target_sha, head_sha, kwargs["unit_key"]))
        return dynamic_result(kwargs["unit_key"], target_sha, head_sha)

    def run(self, store_path, **kwargs):
        options = {
            "provider": self.provider, "repo_root": self.repo,
            "store_path": store_path, "worker": [sys.executable, str(self.worker)],
            "environ": self.environ, "current_root_sha": self.current_sha,
            "required_runner": self.required, "dynamic_runner": self.dynamic,
            "context_kwargs": {"intent_fetcher": self.intent},
        }
        options.update(kwargs)
        return rg_final_review.run_final_review(self.references, **options)

    @staticmethod
    def intent(**kwargs):
        return {
            "description": kwargs.get("description"), "tickets": [],
            "references": kwargs.get("linked_intent") or [],
            "listed_only": [], "warnings": [],
        }

    def traces(self):
        if not self.trace.exists():
            return []
        return [json.loads(line) for line in self.trace.read_text().splitlines()]


def test_three_item_stack_runs_complete_units_links_reports_and_publishes_each_item(tmp_path):
    harness = Harness(tmp_path)
    store_path = tmp_path / "final.sqlite3"

    first = harness.run(store_path)
    assert first["complete"] is True
    assert first["state"] == "pass"
    assert first["trust"]["independent"] is True
    harness.required_calls.clear()
    harness.dynamic_calls.clear()

    output = harness.run(store_path, publish=True, publication_runner=harness.writer)

    assert output["schema"] == "final-review-result/v1"
    assert output["stage"] == "S16-07"
    assert output["complete"] is True
    assert output["missing_stages"] == []
    assert output["state"] == "pass"
    assert output["exit_code"] == 0
    assert output["finding_verdict"] == "pass"
    assert output["execution_complete"] is True
    assert output["freshness"]["state"] == "fresh"
    assert len(output["units"]) == 5
    assert [unit["unit_key"] for unit in output["units"]] == [
        "individual:1", "individual:2", "individual:3", "combined", "prospective",
    ]
    individuals = output["units"][:3]
    combined = output["units"][3]
    prospective = output["units"][4]
    assert all(unit["state"] == "pass" for unit in output["units"])
    assert all(unit["finding_verdict"] == "pass" for unit in output["units"])
    assert all(unit["static_lenses_complete"] for unit in output["units"])
    assert len(combined["individual_reports"]) == 3
    assert {entry["unit_id"] for entry in combined["individual_reports"]} == {
        unit["unit_id"] for unit in individuals
    }
    assert prospective["combined_report_path"] == combined["machine_report_path"]
    for unit in output["units"]:
        assert Path(unit["machine_report_path"]).is_file()
        assert Path(unit["human_report_path"]).is_file()
        assert unit["combined_report_path"] == combined["machine_report_path"]
        assert not Path(unit["repository_path"]).exists()

    traces = [entry for entry in harness.traces() if entry.get("unit_key")]
    latest = traces[-35:]
    assert len(latest) == 5 * len(REQUIRED_LENS_NAMES)
    for unit_key in ("individual:1", "individual:2", "individual:3", "combined", "prospective"):
        requests = [entry for entry in latest if entry["unit_key"] == unit_key]
        assert [entry["lens"] for entry in requests] == list(REQUIRED_LENS_NAMES)
        assert all(entry["context"] == "final-review-context/v1" for entry in requests)
        assert not Path(requests[0]["repository_path"]).exists()
    for unit_key in ("combined", "prospective"):
        assert all(entry["capabilities"] == [CROSS_ITEM_CONTRACTS, INTEGRATION_BEHAVIOR]
                   for entry in latest if entry["unit_key"] == unit_key)
    assert len(harness.required_calls) == 5
    assert len(harness.dynamic_calls) == 5
    assert {call[3] for call in harness.dynamic_calls} == {
        "individual:1", "individual:2", "individual:3", "combined", "prospective",
    }

    assert len(harness.writer.calls) == 3
    assert all("state=success" in call for call in harness.writer.calls)
    assert all(f"context={STATUS_CONTEXT}" in call for call in harness.writer.calls)
    assert len(output["publications"]) == 3
    assert all(publication["official_green"] for publication in output["publications"])
    assert len(output["items"]) == 3
    assert all(entry["official_green"] for entry in output["items"])
    assert output["trust"]["official"] is True
    assert output["trust"]["enforced"] is True

    with FinalStore(store_path) as store:
        report = store.machine_report(output["run_id"])
        assert len(report["units"]) == 5
        assert report["finding_memory"]["clear_allowed"] is True
        assert report["freshness"]["fresh"] is True
        assert len(report["publications"]) == 3
        for unit in report["units"]:
            coverage = {entry["kind"]: entry["status"] for entry in unit["coverage"]}
            assert coverage["review_context"] == "complete"
            assert coverage["intent"] == "complete"
            assert coverage["repository"] == "complete"
            assert coverage["mandatory_lenses"] == "complete"
            assert coverage["required_tests"] == "pass"
            assert coverage["dynamic_tests"] == "not_applicable"
            assert coverage["freshness"] == "fresh"
        assert {entry["kind"]: entry["status"]
                for entry in report["units"][0]["coverage"]}["composition"] == "not_applicable"
        assert {entry["kind"]: entry["status"]
                for entry in combined_coverage(report)}["composition"] == "complete"


def test_newer_stack_findings_replace_older_green_on_every_pinned_item(tmp_path):
    harness = Harness(tmp_path)
    store_path = tmp_path / "replacement.sqlite3"
    harness.run(store_path)
    green_writer = StatusWriter()
    green = harness.run(store_path, publish=True, publication_runner=green_writer)
    assert green["state"] == "pass"
    assert len(green_writer.calls) == 3
    assert all("state=success" in call for call in green_writer.calls)

    harness.environ["STACK_FINDING_UNIT"] = "individual:2"
    adverse_writer = StatusWriter()
    adverse = harness.run(store_path, publish=True, publication_runner=adverse_writer)

    assert adverse["run_seq"] == green["run_seq"] + 1
    assert adverse["state"] == "findings"
    assert adverse["complete"] is True
    assert len(adverse_writer.calls) == 3
    assert all("state=failed" in call for call in adverse_writer.calls)
    assert len(adverse["publications"]) == 3
    assert all(publication["status"] == "published"
               for publication in adverse["publications"])
    assert all(publication["enforced"] and not publication["official_green"]
               for publication in adverse["publications"])
    assert adverse["trust"]["local_only"] is False
    assert adverse["trust"]["enforced"] is True
    assert adverse["trust"]["official"] is True
    for entry, green_call, adverse_call in zip(
            adverse["items"], green_writer.calls, adverse_writer.calls):
        assert entry["publication"]["status"] == "published"
        assert entry["official_green"] is False
        assert entry["green_allowed"] is False
        assert entry["publication_decision"]["can_publish_status"] is True
        assert entry["publication_decision"]["can_publish_pass"] is False
        green_endpoint = next(arg for arg in green_call if arg.startswith("projects/"))
        adverse_endpoint = next(arg for arg in adverse_call if arg.startswith("projects/"))
        assert adverse_endpoint == green_endpoint
    with FinalStore(store_path) as store:
        report = store.machine_report(adverse["run_id"])
        coverage = {row["kind"]: row["status"] for row in combined_coverage(report)}
        assert coverage["provider_publication"] == "complete"
        assert len(report["publications"]) == 3


def combined_coverage(report):
    return next(unit for unit in report["units"] if unit["kind"] == "combined")["coverage"]


@pytest.mark.parametrize("blocked_key", [
    "individual:1", "individual:2", "individual:3", "combined",
])
def test_no_item_is_green_unless_all_three_individuals_and_combined_pass(
        tmp_path, blocked_key):
    harness = Harness(tmp_path)
    harness.environ["STACK_FINDING_UNIT"] = blocked_key

    output = harness.run(
        tmp_path / f"{blocked_key.replace(':', '-')}.sqlite3",
        publish=True, publication_runner=harness.writer,
    )

    assert output["complete"] is True
    assert output["execution_complete"] is True
    assert output["state"] == "findings"
    assert output["finding_verdict"] == "block"
    assert output["exit_code"] == 1
    assert next(unit for unit in output["units"]
                if unit["unit_key"] == blocked_key)["state"] == "findings"
    assert harness.writer.calls == []
    assert output["publications"] == []
    assert len(output["items"]) == 3
    for entry in output["items"]:
        assert entry["final_state"] == "findings"
        assert entry["official_green"] is False
        assert entry["green_allowed"] is False
        assert entry["publication_decision"]["can_publish_pass"] is False
        assert "units-not-pass" in entry["publication_decision"]["green_blockers"]
    with FinalStore(output["store_path"]) as store:
        report = store.machine_report(output["run_id"])
        blocked = next(unit for unit in report["units"]
                       if unit["unit_key"] == blocked_key)
        assert [finding["finding_key"] for finding in blocked["findings"]] == [
            f"stack-defect-{blocked_key}",
        ]
        assert all(not unit["findings"] for unit in report["units"]
                   if unit["unit_key"] != blocked_key)
        assert all(not unit["publications"] for unit in report["units"])


def test_moved_root_requires_restart_and_prevents_every_official_green(tmp_path):
    harness = Harness(tmp_path)
    original_root = harness.items[0]["target"]["sha"]

    class MovingRootProvider(Provider):
        def read(self, reference, repo_root=None):
            current = super().read(reference, repo_root=repo_root)
            if len(self.reads) > len(self.items) and current["target"]["branch"] == "main":
                current["target"]["sha"] = harness.current_sha
                current["target"]["base_sha"] = harness.current_sha
            return current

    harness.provider = MovingRootProvider(harness.items)
    store_path = tmp_path / "stale.sqlite3"
    output = harness.run(store_path, publish=True, publication_runner=harness.writer)

    assert output["state"] == "stale"
    assert output["complete"] is False
    assert output["exit_code"] == 3
    assert output["freshness"]["restart_required"] is True
    assert output["freshness"]["automatic_restart"] is False
    assert output["freshness"]["restart"] == {"required": True, "automatic": False}
    assert output["freshness"]["publication_pass_allowed"] is False
    assert {change["field"] for change in output["freshness"]["changes"]} == {
        "items[0].target.sha", "items[0].target.base_sha",
    }
    assert all(change["expected"] == original_root
               for change in output["freshness"]["changes"])
    assert all(change["current"] == harness.current_sha
               for change in output["freshness"]["changes"])
    assert output["publications"] == []
    assert harness.writer.calls == []
    assert output["trust"]["official"] is False
    assert all(not item["official_green"] and not item["green_allowed"]
               for item in output["items"])
    assert all(item["publication_decision"]["restart_required"] for item in output["items"])
    for unit in output["units"]:
        assert Path(unit["machine_report_path"]).is_file()
        assert Path(unit["human_report_path"]).is_file()
        assert not Path(unit["repository_path"]).exists()
    with FinalStore(store_path) as store:
        assert len(store.run_history()) == 1
        report = store.machine_report(output["run_id"])
        assert all(unit["state"] == "stale" for unit in report["units"])
        assert report["revisions"]["root_target_sha"] == original_root
        for unit in report["units"]:
            coverage = {entry["kind"]: entry["status"] for entry in unit["coverage"]}
            assert coverage["freshness"] == "stale"
            assert coverage["provider_publication"] in ("not_eligible", "not_applicable")
        assert report["publications"] == []


def test_critical_stop_skips_dependents_preserves_safe_evidence_and_never_publishes(tmp_path):
    harness = Harness(tmp_path)
    harness.environ["STACK_STOP_UNIT"] = "individual:2"
    store_path = tmp_path / "stop.sqlite3"

    output = harness.run(store_path, publish=True, publication_runner=harness.writer)

    assert output["state"] == "incomplete"
    assert output["complete"] is False
    assert output["execution_complete"] is False
    assert output["finding_verdict"] is None
    assert output["exit_code"] == 2
    assert [stop["reason_code"] for stop in output["stops"]] == ["unsafe-sandbox"]
    assert output["stops"][0]["evidence"] == {"credential_free": False}
    assert output["publications"] == []
    assert harness.writer.calls == []
    assert output["trust"]["official"] is False
    assert all(not item["official_green"] and not item["green_allowed"]
               for item in output["items"])

    traces = harness.traces()
    assert [(entry["unit_key"], entry["lens"]) for entry in traces] == [
        *(("individual:1", lens) for lens in REQUIRED_LENS_NAMES),
        ("individual:2", "correctness"),
    ]
    assert len(harness.required_calls) == len(harness.dynamic_calls) == 1
    assert harness.dynamic_calls[0][3] == "individual:1"
    safe = output["units"][0]
    origin = output["units"][1]
    dependents = output["units"][2:]
    assert safe["unit_key"] == "individual:1"
    assert safe["execution_complete"] is True
    assert safe["required_verification"]["status"] == "pass"
    assert safe["dynamic_checks"]["complete"] is True
    assert origin["execution_complete"] is False
    assert all(unit["inherited_stop"] and not unit["execution_complete"]
               for unit in dependents)
    for unit in output["units"]:
        assert Path(unit["machine_report_path"]).is_file()
        assert Path(unit["human_report_path"]).is_file()
        assert not unit["repository_path"] or not Path(unit["repository_path"]).exists()

    with FinalStore(store_path) as store:
        report = store.machine_report(output["run_id"])
        safe_report, origin_report, *dependent_reports = report["units"]
        safe_coverage = {entry["kind"]: entry["status"]
                         for entry in safe_report["coverage"]}
        assert safe_coverage["mandatory_lenses"] == "complete"
        assert safe_coverage["required_tests"] == "pass"
        assert safe_coverage["dynamic_tests"] == "not_applicable"
        assert safe_report["findings"] == []
        assert [finding["finding_key"] for finding in origin_report["findings"]] == [
            "stop-unsafe-sandbox",
        ]
        affected = set(origin_report["stops"][0]["affected_unit_ids"])
        assert affected == {unit["unit_id"] for unit in report["units"][1:]}
        for unit in [origin_report, *dependent_reports]:
            coverage = {entry["kind"]: entry["status"] for entry in unit["coverage"]}
            assert coverage["required_tests"] == "skipped"
            assert coverage["dynamic_tests"] == "skipped"
            assert {entry["name"] for entry in unit["skipped_work"]} >= {
                "required_tests", "dynamic_tests",
            }
        for unit in dependent_reports:
            coverage = {entry["kind"]: entry["status"] for entry in unit["coverage"]}
            assert all(coverage[kind] == "skipped" for kind in (
                "review_context", "intent", "repository", "diff", "composition",
            ))
            assert unit["findings"] == []
        assert report["publications"] == []


def _memory_worker(target_unit, closure_type, *, valid=False, observed=None):
    """Return a worker which first reports, then claims to close, one finding."""
    observed = observed if observed is not None else {}

    def invoke(request, **kwargs):
        result = {
            "schema": "final-review-worker-result/v1", "status": "success",
            "summary": "lens complete", "findings": [], "warnings": [], "unreviewed": [],
        }
        if request["lens"] != "correctness" or request["unit_key"] != target_unit:
            return result
        prior = request["finding_memory"]["findings"]
        if not prior:
            result["findings"] = [{
                "id": "closure-regression", "path": "app.txt",
                "start_line": 2, "end_line": 2, "severity": "high",
                "confidence": 0.99, "content": "The caller contract is broken.",
                "evidence": "The current line must satisfy the caller contract.",
            }]
            return result
        pending = prior[0]
        observed.update(pending)
        anchors = deepcopy(pending["anchors"])
        for anchor in anchors:
            anchor["evidence_fingerprint"] = "fabricated-changed-anchor"
        if closure_type == "falsified":
            evidence = {"claim": "trust the reviewer", "confidence": 1.0}
            if valid:
                lines = (Path(request["repository"]["path"]) / "app.txt").read_text(
                    encoding="utf-8").splitlines()
                evidence = {
                    "claim": "Pinned source proves the reported behavior is intentional.",
                    "source": {
                        "path": "app.txt", "start_line": 2, "end_line": 2,
                        "body": lines[1], "unit_key": target_unit,
                        "head_sha": request["repository"]["head_sha"],
                    },
                }
            result["finding_falsifications"] = [{
                "finding_id": pending["finding_id"],
                "content_anchor": pending["content_anchor"],
                "rationale": "The exact pinned source disproves the original contract claim.",
                "evidence": evidence,
            }]
            return result
        resolution = {
            "finding_id": pending["finding_id"],
            "content_anchor": pending["content_anchor"],
            "anchors": anchors,
            "resolution_evidence": {"claim": "fixed"},
            "revalidation": {"status": "passed", "revalidated": True},
        }
        if valid:
            lines = (Path(request["repository"]["path"]) / "app.txt").read_text(
                encoding="utf-8").splitlines()
            head_sha = request["repository"]["head_sha"]
            resolution["resolution_evidence"] = {
                "claim": "The caller contract now has exact current-tree and test proof.",
                "source": {
                    "path": "app.txt", "start_line": 2, "end_line": 2,
                    "body": lines[1], "unit_key": target_unit, "head_sha": head_sha,
                },
            }
            resolution["revalidation"] = {
                "test": "stack-required", "state": "pass", "output": "required OK",
                "evidence_fingerprint": "required-" + head_sha,
                "unit_key": target_unit, "head_sha": head_sha,
            }
        result["finding_resolutions"] = [resolution]
        return result

    return invoke


def _single_item_run(harness, store_path):
    harness.provider = Provider(harness.items[:1])
    return rg_final_review.run_final_review(
        harness.items[0]["url"], provider=harness.provider, repo_root=harness.repo,
        store_path=store_path, environ=harness.environ,
        required_runner=harness.required, dynamic_runner=harness.dynamic,
        context_kwargs={"intent_fetcher": harness.intent},
    )


@pytest.mark.parametrize("closure_type", ["resolved", "falsified"])
def test_standalone_orchestrator_rejects_fabricated_closure_evidence(
        tmp_path, monkeypatch, closure_type):
    harness = Harness(tmp_path)
    store_path = tmp_path / f"standalone-fabricated-{closure_type}.sqlite3"
    observed = {}
    monkeypatch.setattr(
        rg_final_review, "invoke_worker",
        _memory_worker("individual:1", closure_type, observed=observed),
    )

    first = _single_item_run(harness, store_path)
    second = _single_item_run(harness, store_path)

    assert first["state"] == "findings"
    assert second["state"] == "incomplete"
    assert second["finding_memory"]["clear_allowed"] is False
    assert second["finding_memory"]["pending_thread_ids"] == [observed["thread_id"]]
    assert second["finding_closures"][0]["state"] == "rejected"
    with FinalStore(store_path) as store:
        assert [
            event["event_type"]
            for event in store.finding_history(observed["thread_id"])["events"]
        ] == ["observed"]


@pytest.mark.parametrize("target_unit", [
    "individual:1", "individual:2", "individual:3", "combined", "prospective",
])
def test_every_stack_unit_rejects_worker_only_resolution_evidence(
        tmp_path, monkeypatch, target_unit):
    harness = Harness(tmp_path)
    store_path = tmp_path / f"fabricated-{target_unit.replace(':', '-')}.sqlite3"
    observed = {}
    monkeypatch.setattr(
        rg_final_review, "invoke_worker",
        _memory_worker(target_unit, "resolved", observed=observed),
    )

    first = harness.run(store_path)
    second = harness.run(store_path)

    assert first["state"] == "findings"
    assert second["state"] == "incomplete"
    assert second["finding_memory"]["clear_allowed"] is False
    assert second["finding_memory"]["pending_thread_ids"] == [observed["thread_id"]]
    assert second["finding_closures"][0]["state"] == "rejected"
    with FinalStore(store_path) as store:
        assert len(store.finding_history(observed["thread_id"])["events"]) == 1


def test_standalone_orchestrator_records_exact_current_tree_and_test_proof(
        tmp_path, monkeypatch):
    harness = Harness(tmp_path)
    store_path = tmp_path / "standalone-verified-resolution.sqlite3"
    observed = {}
    monkeypatch.setattr(
        rg_final_review, "invoke_worker",
        _memory_worker("individual:1", "resolved", valid=True, observed=observed),
    )

    first = _single_item_run(harness, store_path)
    second = _single_item_run(harness, store_path)

    assert first["state"] == "findings"
    assert second["state"] == "pass"
    assert second["finding_memory"]["clear_allowed"] is True
    assert second["finding_closures"][0]["state"] == "recorded"
    with FinalStore(store_path) as store:
        event = store.finding_history(observed["thread_id"])["events"][-1]
        assert event["event_type"] == "resolved"
        assert event["resolution_evidence"]["source"]["body"] == "item 1"
        assert event["resolution_evidence"]["source"]["file_sha256"]
        assert event["revalidation"]["test"] == "stack-required"
        assert event["revalidation"]["test_id"]


def test_standalone_orchestrator_falsifies_only_with_verified_tree_evidence(
        tmp_path, monkeypatch):
    harness = Harness(tmp_path)
    store_path = tmp_path / "standalone-verified-falsification.sqlite3"
    observed = {}
    monkeypatch.setattr(
        rg_final_review, "invoke_worker",
        _memory_worker("individual:1", "falsified", valid=True, observed=observed),
    )

    first = _single_item_run(harness, store_path)
    second = _single_item_run(harness, store_path)

    assert first["state"] == "findings"
    assert second["state"] == "pass"
    assert second["finding_closures"][0]["state"] == "recorded"
    with FinalStore(store_path) as store:
        event = store.finding_history(observed["thread_id"])["events"][-1]
        assert event["event_type"] == "falsified"
        assert event["evidence"]["source"]["body"] == "item 1"
        assert event["evidence"]["verification"]["independent_tree_inspection"] is True
        assert event["evidence"]["verification"]["head_sha"] == \
            first["revisions"]["source_sha"]


@pytest.mark.parametrize(("section", "field", "value"), [
    ("revalidation", "test", "unrelated-passing-test"),
    ("revalidation", "output", "unrelated passing output"),
    ("revalidation", "evidence_fingerprint", "unrelated-fingerprint"),
    ("revalidation", "unit_key", "combined"),
    ("resolution_evidence", "body", "fabricated current body"),
    ("resolution_evidence", "head_sha", "0" * 40),
])
def test_orchestrator_rejects_mismatched_source_or_test_identity(
        tmp_path, monkeypatch, section, field, value):
    harness = Harness(tmp_path)
    store_path = tmp_path / f"mismatch-{section}-{field}.sqlite3"
    observed = {}
    valid_worker = _memory_worker("individual:1", "resolved", valid=True, observed=observed)

    def mismatched_worker(request, **kwargs):
        result = valid_worker(request, **kwargs)
        for resolution in result.get("finding_resolutions") or ():
            target = resolution[section]
            if section == "resolution_evidence":
                target = target["source"]
            target[field] = value
        return result

    monkeypatch.setattr(rg_final_review, "invoke_worker", mismatched_worker)
    first = _single_item_run(harness, store_path)
    second = _single_item_run(harness, store_path)

    assert first["state"] == "findings"
    assert second["state"] == "incomplete"
    assert second["finding_closures"][0]["state"] == "rejected"
    assert second["finding_memory"]["clear_allowed"] is False
    with FinalStore(store_path) as store:
        assert len(store.finding_history(observed["thread_id"])["events"]) == 1
