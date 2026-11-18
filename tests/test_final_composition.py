"""Real-Git vertical tests for ordered final-review stack composition."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rg_final_compose  # noqa: E402
import rg_final_review  # noqa: E402
from rg_final_store import FinalStore  # noqa: E402


def _git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        timeout=30, check=check,
    )


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _item(iid, source_branch, target_branch, source_sha, target_sha, repo):
    return {
        "provider": "gitlab",
        "id": f"gitlab:111!{iid}",
        "iid": iid,
        "url": f"https://gitlab.example.test/group/project/-/merge_requests/{iid}",
        "title": f"Item {iid}",
        "state": "opened",
        "source": {
            "project_id": 111,
            "project_path": "group/project",
            "url": str(repo),
            "http_url": str(repo),
            "branch": source_branch,
            "sha": source_sha,
        },
        "target": {
            "project_id": 111,
            "project_path": "group/project",
            "url": str(repo),
            "http_url": str(repo),
            "branch": target_branch,
            "sha": target_sha,
            "base_sha": target_sha,
        },
    }


def _graph(items, root_target):
    return {
        "schema": "final-review-graph/v1",
        "provider": "gitlab",
        "target_project": {"project_id": 111, "project_path": "group/project"},
        "root_target": root_target,
        "items": items,
        "item_keys": [item["id"] for item in items],
        "references": [item["url"] for item in items],
    }


def _repository(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for key, value in (
        ("user.name", "Composition Test"),
        ("user.email", "composition@example.test"),
        ("commit.gpgsign", "false"),
    ):
        _git(repo, "config", key, value)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    root_sha = _commit(repo, "root")
    root_target = _item(0, "unused", "main", root_sha, root_sha, repo)["target"]

    _git(repo, "checkout", "-q", "-b", "feature/base")
    (repo / "base.txt").write_text("base\nfirst\n", encoding="utf-8")
    first_sha = _commit(repo, "first")
    first = _item(1, "feature/base", "main", first_sha, root_sha, repo)

    _git(repo, "checkout", "-q", "-b", "feature/head")
    (repo / "base.txt").write_text("base\nfirst\nsecond\n", encoding="utf-8")
    (repo / "second.txt").write_text("second\n", encoding="utf-8")
    second_sha = _commit(repo, "second")
    second = _item(2, "feature/head", "feature/base", second_sha, first_sha, repo)

    _git(repo, "checkout", "-q", "main")
    (repo / "current.txt").write_text("current target\n", encoding="utf-8")
    current_sha = _commit(repo, "current root")
    return repo, root_target, [first, second], current_sha


def test_compose_stack_applies_base_most_first_and_builds_prospective_tree(tmp_path):
    repo, root_target, items, current_sha = _repository(tmp_path)
    (repo / "uncommitted.txt").write_text("caller only\n", encoding="utf-8")
    before = {item["source"]["branch"]: item["source"]["sha"] for item in items}
    graph = _graph(items, root_target)

    with rg_final_compose.compose_stack(
        graph, current_root_sha=current_sha,
    ) as composition:
        combined = composition["combined"]
        prospective = composition["prospective"]
        combined_path = Path(combined["repository"]["path"])
        prospective_path = Path(prospective["repository"]["path"])

        assert composition["schema"] == "final-review-composition/v1"
        assert composition["complete"] is True
        assert combined["target_sha"] == root_target["sha"]
        assert combined["head_sha"] == items[-1]["source"]["sha"]
        assert combined_path != repo
        assert prospective_path != repo
        assert (combined_path / "base.txt").read_text(encoding="utf-8") == \
            "base\nfirst\nsecond\n"
        assert (combined_path / "second.txt").read_text(encoding="utf-8") == "second\n"
        assert not (combined_path / "current.txt").exists()
        assert not (combined_path / "uncommitted.txt").exists()
        assert (prospective_path / "base.txt").read_text(encoding="utf-8") == \
            "base\nfirst\nsecond\n"
        assert (prospective_path / "current.txt").read_text(encoding="utf-8") == \
            "current target\n"
        assert not (prospective_path / "uncommitted.txt").exists()
        assert combined["applied_item_keys"] == [item["id"] for item in items]
        assert prospective["applied_item_keys"] == [item["id"] for item in items]
        assert "second" in combined["diff"]
        assert "current target" not in combined["diff"]

        assert _git(repo, "rev-parse", "feature/base").stdout.strip() == \
            before["feature/base"]
        assert _git(repo, "rev-parse", "feature/head").stdout.strip() == \
            before["feature/head"]
        assert (repo / "uncommitted.txt").read_text(encoding="utf-8") == "caller only\n"

    assert not combined_path.exists()
    assert not prospective_path.exists()
    assert _git(repo, "worktree", "list", "--porcelain").stdout.count("worktree ") == 1


def test_prospective_conflict_is_scoped_incomplete_without_touching_combined_evidence(tmp_path):
    repo, root_target, items, current_sha = _repository(tmp_path)
    (repo / "base.txt").write_text("base\ncurrent conflict\n", encoding="utf-8")
    current_sha = _commit(repo, "conflicting current target")
    before = _git(repo, "rev-parse", "main").stdout.strip()

    with rg_final_compose.compose_stack(
        _graph(items, root_target), current_root_sha=current_sha,
    ) as composition:
        assert composition["complete"] is False
        assert composition["combined"]["complete"] is True
        prospective = composition["prospective"]
        assert prospective["complete"] is False
        assert prospective["repository"] is None
        assert prospective["diff"] == ""
        assert prospective["applied_item_keys"] == []
        assert prospective["errors"][0]["code"] == "composition-conflict"
        assert prospective["errors"][0]["details"]["item_key"] == items[0]["id"]
        assert any(error["scope"] == "prospective" for error in composition["errors"])
        assert (Path(composition["combined"]["repository"]["path"]) /
                "second.txt").is_file()
        assert "current conflict" not in json.dumps(composition["combined"]["repository"])

    assert _git(repo, "rev-parse", "main").stdout.strip() == before
    assert _git(repo, "status", "--porcelain").stdout == ""


def test_disjoint_but_inconsistent_pins_are_unreproducible_not_a_review_pass(tmp_path):
    repo, root_target, items, _ = _repository(tmp_path)
    first, _ = items
    _git(repo, "checkout", "-q", "-b", "feature/inconsistent", root_target["sha"])
    (repo / "independent.txt").write_text("independent\n", encoding="utf-8")
    independent_sha = _commit(repo, "independent head")
    inconsistent = _item(
        3, "feature/inconsistent", first["source"]["branch"], independent_sha,
        first["source"]["sha"], repo,
    )
    # The provider graph branches look linear, but the target SHA is not the
    # independent head's parent. Disjoint patches apply; reproduction fails.
    inconsistent["target"]["base_sha"] = root_target["sha"]
    graph = _graph([first, inconsistent], root_target)

    with rg_final_compose.compose_stack(graph) as composition:
        combined = composition["combined"]
        assert combined["complete"] is False
        assert combined["applied_item_keys"] == [first["id"], inconsistent["id"]]
        assert combined["repository"] is None
        assert combined["errors"][0]["code"] == "composition-unreproducible"
        assert "do not reproduce" in combined["errors"][0]["message"]
        assert combined["errors"][0]["details"]["expected_head"] == independent_sha
        assert composition["complete"] is False


def test_provider_merge_base_mismatch_is_explicit_and_never_auto_corrected(tmp_path):
    repo, root_target, items, _ = _repository(tmp_path)
    items[0]["target"]["base_sha"] = "0" * 40

    with rg_final_compose.compose_stack(_graph(items, root_target)) as composition:
        for kind in ("combined", "prospective"):
            unit = composition[kind]
            assert unit["complete"] is False
            assert unit["applied_item_keys"] == []
            assert unit["errors"][0]["code"] == "composition-unreproducible"
            assert unit["errors"][0]["details"]["item_key"] == items[0]["id"]
            assert unit["errors"][0]["details"]["expected"] == "0" * 40


class StackProvider:
    def __init__(self, items):
        self.items = {item["url"]: copy.deepcopy(item) for item in items}
        self.reads = []
        self.revalidations = []

    def read(self, reference, repo_root=None):
        self.reads.append(reference)
        return copy.deepcopy(self.items[reference])

    def list_open(self, pinned, repo_root=None):
        return list(self.items)

    def revalidate(self, pinned, repo_root=None):
        self.revalidations.append(pinned["id"])
        return copy.deepcopy(self.items[pinned["url"]])


def _worker_script(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text(
        "import json, os, pathlib, sys\n"
        "request = json.loads(pathlib.Path(sys.argv[-1]).read_text())\n"
        "trace = pathlib.Path(os.environ['STACK_WORKER_TRACE'])\n"
        "with trace.open('a') as stream:\n"
        "    stream.write(json.dumps({'lens': request['lens'],"
        " 'unit_key': request['unit_key'],"
        " 'capabilities': request['capabilities'],"
        " 'repository': request['repository'],"
        " 'composition': request.get('composition')}) + '\\n')\n"
        "findings = []\n"
        "if os.environ.get('STACK_BLOCK_UNIT') == request['unit_key'] and"
        " request['lens'] == 'correctness':\n"
        "    findings.append({'id': 'composed-defect', 'path': 'base.txt',"
        " 'start_line': 2, 'end_line': 2, 'severity': 'high', 'confidence': 0.99,"
        " 'content': 'The composed change breaks an actual caller.'})\n"
        "print(json.dumps({'schema': 'final-review-worker-result/v1',"
        " 'status': 'success', 'summary': request['lens'] + ' complete',"
        " 'findings': findings, 'warnings': []}))\n",
        encoding="utf-8",
    )
    return script


def test_multi_ref_review_composes_and_links_all_individual_and_combined_units(tmp_path):
    repo, root_target, items, current_sha = _repository(tmp_path)
    trace = tmp_path / "worker-trace.json"
    store_path = tmp_path / "state" / "final-review.sqlite3"
    provider = StackProvider(items)
    worker = _worker_script(tmp_path)

    output = rg_final_review.run_final_stack(
        [item["url"] for item in items], provider=provider, repo_root=repo,
        store_path=store_path, worker=[sys.executable, str(worker)],
        environ={**os.environ, "STACK_WORKER_TRACE": str(trace)},
        current_root_sha=current_sha,
    )

    assert output["schema"] == "final-review-result/v1"
    assert output["stage"] == "S16-06B"
    assert output["item_keys"] == [item["id"] for item in items]
    assert output["complete"] is False
    assert output["missing_stages"] == ["required_and_dynamic_tests", "provider_publication"]
    assert output["state"] == "incomplete"
    assert output["exit_code"] == 2
    assert output["composition"]["complete"] is True
    assert output["trust"]["official"] is False
    assert output["trust"]["enforced"] is False
    assert output["trust"]["static_lenses_complete"] is True
    assert output["finding_verdict"] == "pass"
    assert len(output["units"]) == 4
    assert [unit["unit_key"] for unit in output["units"]] == [
        "individual:1", "individual:2", "combined", "prospective",
    ]
    assert all(unit["static_lenses_complete"] for unit in output["units"])
    assert all(unit["finding_verdict"] == "pass" for unit in output["units"])
    assert output["units"][2]["head_sha"] == items[-1]["source"]["sha"]
    assert output["units"][3]["base_sha"] == current_sha
    assert output["units"][3]["head_sha"] != items[-1]["source"]["sha"]
    for unit in output["units"]:
        assert Path(unit["machine_report_path"]).is_file()
        assert Path(unit["human_report_path"]).is_file()
        assert not Path(unit["repository_path"]).exists()
    assert output["units"][2]["individual_unit_ids"] == [
        output["units"][0]["unit_id"], output["units"][1]["unit_id"],
    ]
    assert len(output["units"][2]["individual_reports"]) == 2

    traces = [json.loads(line) for line in trace.read_text().splitlines()]
    assert len(traces) == 28
    combined = [entry for entry in traces if entry["unit_key"] == "combined"]
    prospective = [entry for entry in traces if entry["unit_key"] == "prospective"]
    assert len(combined) == len(prospective) == 7
    assert all(entry["capabilities"] == [
        "cross-item-contracts", "integration-behavior",
    ] for entry in combined)
    assert all(entry["capabilities"] == ["integration-behavior"]
               for entry in prospective)
    assert all(entry["composition"]["item_keys"] == output["item_keys"]
               for entry in combined + prospective)
    combined_path = Path(combined[0]["repository"]["path"])
    prospective_path = Path(prospective[0]["repository"]["path"])
    assert not combined_path.exists()
    assert not prospective_path.exists()

    with FinalStore(store_path) as store:
        machine = store.machine_report(output["run_id"])
        assert [unit["kind"] for unit in machine["units"]] == [
            "individual", "individual", "combined", "prospective",
        ]
        assert machine["units"][2]["individual_unit_ids"] == [
            machine["units"][0]["unit_id"], machine["units"][1]["unit_id"],
        ]
        assert all(len(unit["lenses"]) == 7 for unit in machine["units"])
        assert machine["publications"] == []
        for unit in machine["units"]:
            coverage = {entry["kind"]: entry["status"] for entry in unit["coverage"]}
            assert coverage["mandatory_lenses"] == "complete"
            assert coverage["required_tests"] == "not_implemented"
            assert coverage["dynamic_tests"] == "not_implemented"
            assert coverage["provider_publication"] == "not_implemented"
        assert machine["run"]["state"] == "incomplete"
        assert machine["run"]["metadata"]["complete"] is False
    assert provider.revalidations == [item["id"] for item in items]


def test_composition_conflict_persists_skipped_scope_and_never_emits_official_pass(tmp_path):
    repo, root_target, items, current_sha = _repository(tmp_path)
    (repo / "base.txt").write_text("base\ncurrent conflict\n", encoding="utf-8")
    current_sha = _commit(repo, "conflicting current target")
    trace = tmp_path / "worker-trace.json"

    output = rg_final_review.run_final_stack(
        [item["url"] for item in items], provider=StackProvider(items), repo_root=repo,
        store_path=tmp_path / "state.sqlite3",
        worker=[sys.executable, str(_worker_script(tmp_path))],
        environ={**os.environ, "STACK_WORKER_TRACE": str(trace)},
        current_root_sha=current_sha,
    )

    assert output["complete"] is False
    assert output["state"] == "incomplete"
    assert output["exit_code"] == 2
    assert output["finding_verdict"] is None
    assert output["composition"]["combined"]["complete"] is True
    prospective = output["units"][-1]
    assert prospective["unit_key"] == "prospective"
    assert prospective["composition_complete"] is False
    assert prospective["static_lenses_complete"] is False
    assert prospective["composition_errors"][0]["code"] == "composition-conflict"
    assert output["trust"]["official"] is False
    assert all(unit["static_lenses_complete"] for unit in output["units"][:3])
    assert len(trace.read_text().splitlines()) == 21
    with FinalStore(output["store_path"]) as store:
        report = store.machine_report(output["run_id"], prospective["unit_id"])
        unit = report["units"][0]
        coverage = {entry["kind"]: entry["status"] for entry in unit["coverage"]}
        assert coverage["composition"] == "incomplete"
        assert coverage["mandatory_lenses"] == "incomplete"
        assert {entry["name"] for entry in unit["skipped_work"]} == set(
            entry["name"] for entry in unit["lenses"]
        )
        assert unit["state"] == "incomplete"
        assert report["publications"] == []


def test_combined_blocking_finding_uses_compute_verdict_but_is_not_official(tmp_path):
    repo, root_target, items, current_sha = _repository(tmp_path)
    trace = tmp_path / "worker-trace.json"

    output = rg_final_review.run_final_stack(
        [item["url"] for item in items], provider=StackProvider(items), repo_root=repo,
        store_path=tmp_path / "state.sqlite3",
        worker=[sys.executable, str(_worker_script(tmp_path))],
        environ={**os.environ, "STACK_WORKER_TRACE": str(trace),
                 "STACK_BLOCK_UNIT": "combined"},
        current_root_sha=current_sha,
    )

    assert output["finding_verdict"] == "block"
    assert output["state"] == "incomplete"
    assert output["exit_code"] == 1
    assert output["complete"] is False
    combined = output["units"][2]
    assert combined["state"] == "findings"
    assert combined["finding_verdict"] == "block"
    assert output["units"][0]["finding_verdict"] == "pass"
    assert output["units"][3]["finding_verdict"] == "pass"
    assert output["trust"]["official"] is False
    with FinalStore(output["store_path"]) as store:
        unit = store.machine_report(output["run_id"], combined["unit_id"])["units"][0]
        assert len(unit["findings"]) == 1
        assert unit["findings"][0]["finding_key"] == "composed-defect"
        assert unit["findings"][0]["severity"] == "high"
        assert store.machine_report(output["run_id"])["publications"] == []


def test_multi_ref_operator_main_runs_validated_stack_and_emits_linked_reports(
        tmp_path, monkeypatch, capsys):
    repo, root_target, items, _ = _repository(tmp_path)
    provider = StackProvider(items)
    trace = tmp_path / "worker-trace.json"
    worker = _worker_script(tmp_path)
    neutral_home = tmp_path / "neutral-home"
    neutral_home.mkdir()
    store_path = tmp_path / "final-review-state" / "final-review.sqlite3"
    monkeypatch.setattr(rg_final_review, "GitLabProvider", lambda: provider)
    monkeypatch.setenv("HOME", str(neutral_home))
    monkeypatch.setenv("FINAL_REVIEW_STORE", str(store_path))
    monkeypatch.setenv("OCR_STATE_DIR", str(tmp_path / "operator-state"))
    monkeypatch.setenv("STACK_WORKER_TRACE", str(trace))
    monkeypatch.setenv(
        "RG_FINAL_WORKER",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(worker))}",
    )

    exit_code = rg_final_review.main(
        ["final-review", *[item["url"] for item in items]])
    captured = capsys.readouterr()
    output = json.loads(captured.out)

    assert exit_code in (0, 1)
    assert output["item_keys"] == [item["id"] for item in items]
    assert output["stage"] == "S16-07"
    assert output["complete"] is True
    assert output["state"] in ("pass", "findings")
    assert output["composition"]["complete"] is True
    assert output["graph"]["references"] == [item["url"] for item in items]
    assert output["item_keys"] == [item["id"] for item in items]
    assert output["run_seq"] == 1
    assert Path(output["store_path"]) == store_path
    assert store_path.is_file()
    assert not (neutral_home / ".final-review").exists()
    assert not (neutral_home / ".local" / "state" / "review-gate").exists()
    assert output["units"][2]["individual_reports"]
    assert Path(output["machine_report_path"]).is_file()
    assert Path(output["units"][2]["combined_report_path"]).is_file()
    assert "stage S16-07 is complete" in captured.err
    assert len(trace.read_text().splitlines()) == 28


def test_invalid_or_reordered_graph_is_rejected_before_worktree_creation(tmp_path):
    repo, root_target, items, current_sha = _repository(tmp_path)
    graph = _graph(items, root_target)
    graph["items"] = list(reversed(graph["items"]))
    graph["item_keys"] = list(reversed(graph["item_keys"]))

    with pytest.raises(ValueError, match="non-linear"):
        with rg_final_compose.compose_stack(graph, current_root_sha=current_sha):
            pass

    assert _git(repo, "worktree", "list", "--porcelain").stdout.count("worktree ") == 1


@pytest.mark.parametrize("coordinate", ["root", "source", "target"])
def test_composition_enforces_project_coordinates_before_worktree_creation(
        tmp_path, coordinate):
    repo, root_target, items, current_sha = _repository(tmp_path)
    graph = _graph(items, root_target)
    if coordinate == "root":
        graph["root_target"] = {**root_target, "project_id": 999}
    elif coordinate == "source":
        graph["items"][0]["source"]["project_id"] = 999
    else:
        graph["items"][1]["target"]["project_id"] = 999

    with pytest.raises(ValueError, match="project coordinates"):
        with rg_final_compose.compose_stack(graph, current_root_sha=current_sha):
            pass

    assert _git(repo, "worktree", "list", "--porcelain").stdout.count("worktree ") == 1


def test_composition_preserves_valid_fork_source_coordinates(tmp_path):
    repo, root_target, items, current_sha = _repository(tmp_path)
    items[1]["source"].update({
        "project_id": 222,
        "project_path": "contributor/fork",
    })

    with rg_final_compose.compose_stack(
        _graph(items, root_target), current_root_sha=current_sha,
    ) as composition:
        assert composition["complete"] is True
        assert composition["combined"]["applied_item_keys"] == [
            item["id"] for item in items
        ]
        assert composition["prospective"]["complete"] is True
