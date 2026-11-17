"""Vertical SPEC-16 standalone final-review operator-command tests.

Every execution goes through ``review-gate.py final-review`` in a subprocess,
uses a scripted authenticated provider and reviewer, clones a real temporary
repository, and reopens the real SQLite store and persistent reports.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "review-gate.py"
STUB = ROOT / "tests" / "stub_reviewer.py"
LENS_NAMES = (
    "correctness",
    "repository-standards",
    "documentation/domain",
    "testing",
    "security",
    "compatibility/migrations",
    "change-intent",
)
sys.path.insert(0, str(ROOT / "scripts"))
import rg_store  # noqa: E402
from rg_final_store import FinalStore  # noqa: E402


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        timeout=30, check=True,
    ).stdout.strip()


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _repository(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    for key, value in (
        ("user.name", "Standalone Final Review Test"),
        ("user.email", "final@example.test"),
        ("commit.gpgsign", "false"),
    ):
        _git(repo, "config", key, value)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "ADR.md").write_text("unchanged binding decision\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "target.txt").write_text("target only\n", encoding="utf-8")
    target = _commit(repo, "target")
    _git(repo, "checkout", "-q", "-b", "feature", base)
    (repo / "README.md").write_text("feature one\n", encoding="utf-8")
    (repo / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    head = _commit(repo, "feature")
    _git(repo, "checkout", "-q", "main")
    (repo / "README.md").write_text("uncommitted local edit\n", encoding="utf-8")
    (repo / "local-only.txt").write_text("uncommitted local secret\n", encoding="utf-8")
    return repo, base, target, head


def _project(project_id, path, repo):
    return {
        "id": project_id,
        "path_with_namespace": path,
        "web_url": f"https://gitlab.example.test/{path}",
        "http_url_to_repo": str(repo),
        "ssh_url_to_repo": str(repo),
    }


@pytest.fixture
def standalone(tmp_path, monkeypatch):
    repo, base, target, head = _repository(tmp_path)
    target_project = _project(111, "group/project", repo)
    source_project = _project(222, "arbitrary-contributor/fork", repo)
    merge_request = {
        "id": 9017,
        "iid": 17,
        "title": "Review the immutable feature",
        "description": "Correct the return value.\n\nCloses #88",
        "state": "opened",
        "web_url": "https://gitlab.example.test/group/project/-/merge_requests/17",
        "author": {"username": "someone-else", "name": "Someone Else"},
        "source_project_id": 111,
        "target_project_id": 111,
        "source_branch": "contributor/feature",
        "target_branch": "main",
        "sha": head,
        "diff_refs": {"base_sha": base, "head_sha": head, "start_sha": target},
    }
    calls = tmp_path / "provider-calls.json"
    script = tmp_path / "provider-script.json"
    script.write_text(json.dumps({
        "merge_request": merge_request,
        "target_project": target_project,
        "source_project": source_project,
        "calls": str(calls),
    }), encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    glab = bindir / "glab"
    glab.write_text(
        f"#!{sys.executable}\n"
        "import runpy, sys\n"
        f"stub = {str(STUB)!r}\n"
        "sys.argv = [stub, '--glab', *sys.argv[1:]]\n"
        "runpy.run_path(stub, run_name='__main__')\n",
        encoding="utf-8",
    )
    glab.chmod(0o755)
    gh = bindir / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import runpy, sys\n"
        f"stub = {str(STUB)!r}\n"
        "sys.argv = [stub, '--gh', *sys.argv[1:]]\n"
        "runpy.run_path(stub, run_name='__main__')\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("OCR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("RG_TEST_PROVIDER_SCRIPT", str(script))
    monkeypatch.setenv(
        "RG_FINAL_WORKER",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(STUB))} --final-review",
    )
    monkeypatch.setenv("STUB_FINAL_TRACE", str(tmp_path / "worker-trace.json"))
    monkeypatch.setenv(
        "RG_FINAL_DYNAMIC_EXECUTOR",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(STUB))} --final-dynamic",
    )
    monkeypatch.setenv("GLAB_TOKEN", "provider-read-and-write-secret")
    monkeypatch.setenv("GITLAB_WRITE_TOKEN", "provider-write-secret")
    monkeypatch.setenv("GH_TOKEN", "another-provider-write-secret")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-reach-worker")
    return SimpleNamespace(
        monkeypatch=monkeypatch,
        repo=repo, base=base, target=target, head=head, ref=merge_request["web_url"],
        merge_request=merge_request, script=script, calls=calls,
        trace=tmp_path / "worker-trace.json", store_path=tmp_path / "state" / "final-review.sqlite3",
    )


def _gate(*args, cwd=None):
    return subprocess.run(
        [sys.executable, str(GATE), *args], cwd=str(cwd or ROOT),
        env=os.environ.copy(), capture_output=True, text=True, timeout=60, check=False,
    )


def _result(process, expected_exit):
    assert process.returncode == expected_exit, process.stderr + process.stdout
    assert process.stdout, process.stderr
    return json.loads(process.stdout)


def _script_final_result(standalone, result, lens="correctness"):
    standalone.monkeypatch.setenv("STUB_FINAL_RESULT_FOR", json.dumps({lens: result}))


def _worker_traces(standalone):
    return [json.loads(line) for line in
            standalone.trace.read_text(encoding="utf-8").splitlines()]


def _worker_trace(standalone):
    traces = _worker_traces(standalone)
    assert traces
    return traces[-1]


def _reports(output):
    machine_path = Path(output["machine_report_path"])
    human_path = Path(output["human_report_path"])
    assert machine_path.is_file()
    assert human_path.is_file()
    return json.loads(machine_path.read_text(encoding="utf-8")), human_path.read_text(
        encoding="utf-8"
    )


def _coverage(unit):
    return {entry["kind"]: entry for entry in unit["coverage"]}


def test_final_review_rejects_multiple_refs_as_reserved_for_stack_orchestration():
    result = _gate("final-review", "17", "18")

    assert result.returncode == 2
    assert "exactly one" in result.stderr
    assert "reserved" in result.stderr
    assert "multiple" in result.stderr.lower()
    assert not result.stdout


def test_standalone_reviews_full_immutable_tree_and_persists_complete_result(standalone):
    # A model-declared block is advisory: with no findings compute_verdict says pass.
    standalone.monkeypatch.setenv("STUB_FINAL_RESULT", json.dumps({"verdict": "block"}))

    output = _result(_gate("final-review", standalone.ref), 0)
    machine, human = _reports(output)
    traces = _worker_traces(standalone)
    trace = traces[0]
    materialized = Path(trace["request"]["repository"]["path"])

    assert output["schema"] == "final-review-result/v1"
    assert output["static_lenses_complete"] is True
    assert [lens["name"] for lens in output["lenses"]] == list(LENS_NAMES)
    assert all(lens["state"] == "completed" for lens in output["lenses"])
    assert output["complete"] is True
    assert output["execution_complete"] is True
    assert output["state"] == "pass"
    assert output["finding_verdict"] == "pass"
    assert output["exit_code"] == 0
    assert output["run_id"] == machine["run"]["run_id"]
    assert output["store_instance_id"] == machine["store"]["store_instance_id"]
    assert output["unit_key"] == "individual:1"
    assert output["unit_id"] == machine["units"][0]["unit_id"]
    assert output["contract_version"] == "spec-16/v1"
    assert output["revision_tuple"][:3] == [111, "contributor/feature", standalone.head]
    assert output["revisions"] == {
        "source_sha": standalone.head,
        "target_sha": standalone.target,
        "provider_base_sha": standalone.base,
        "merge_base_sha": standalone.base,
    }
    assert output["trust"]["local_only"] is True
    assert output["trust"]["enforced"] is False
    assert output["trust"]["official"] is False
    assert output["trust"]["stage_incomplete"] is False
    assert output["publication"]["status"] == "local_only"
    assert output["publication"]["official_green"] is False
    assert machine["run"]["state"] == "pass"
    assert machine["run"]["metadata"]["complete"] is True
    assert machine["run"]["metadata"]["missing_stages"] == []
    individual, combined = machine["units"]
    assert individual["state"] == "pass"
    assert combined["state"] == "pass"
    assert [lens["name"] for lens in combined["lenses"]] == list(LENS_NAMES)
    assert machine["publications"][0]["status"] == "local_only"
    assert [lens["name"] for lens in individual["lenses"]] == list(LENS_NAMES)
    assert all(lens["state"] == "completed" for lens in individual["lenses"])
    assert all(lens["evidence"]["diff_sha256"] for lens in individual["lenses"])
    assert all(lens["evidence"]["repository_access"] == "full_tree"
               for lens in individual["lenses"])
    coverage = _coverage(individual)
    assert coverage["repository"]["status"] == "complete"
    assert coverage["diff"]["status"] == "complete"
    assert all(coverage[f"lens:{name}"]["status"] == "completed" for name in LENS_NAMES)
    assert coverage["mandatory_lenses"]["status"] == "complete"
    assert coverage["review_context"]["status"] == "complete"
    assert coverage["intent"]["status"] == "complete"
    assert coverage["required_tests"]["status"] == "absent"
    assert coverage["dynamic_tests"]["status"] == "not_applicable"
    assert coverage["composition"]["status"] == "not_applicable"
    assert coverage["freshness"]["status"] == "fresh"
    assert _coverage(combined)["composition"]["status"] == "complete"

    requests = [entry["request"] for entry in traces]
    assert [request["lens"] for request in requests] == [*LENS_NAMES, *LENS_NAMES]
    assert [request["unit_key"] for request in requests] == [
        *(["individual:1"] * len(LENS_NAMES)), *(["combined"] * len(LENS_NAMES)),
    ]
    request = requests[0]
    assert request["schema"] == "final-review-worker-request/v1"
    assert request["repository"]["head_sha"] == standalone.head
    assert request["repository"]["target_sha"] == standalone.target
    assert request["repository"]["merge_base_sha"] == standalone.base
    assert request["item"]["author"] == {"username": "someone-else", "name": "Someone Else"}
    assert "http_url" not in json.dumps(request)
    assert "feature one" in request["diff"]
    assert "return 1" in request["diff"]
    assert "target only" not in request["diff"]
    assert "uncommitted local" not in request["diff"]
    assert trace["unchanged_adr"] == "unchanged binding decision\n"
    assert trace["local_only_exists"] is False
    assert not materialized.exists()
    assert not list(Path(rg_store.clones_dir()).glob("*-final-*"))
    assert trace["environment"] == {
        "GLAB_TOKEN": None,
        "GITLAB_WRITE_TOKEN": None,
        "GH_TOKEN": None,
        "UNRELATED_SECRET": None,
        "STUB_FINAL_TRACE": str(standalone.trace),
    }
    provider_calls = json.loads(standalone.calls.read_text(encoding="utf-8"))
    assert len(provider_calls) == 4
    assert all(call["provider_token_available"] for call in provider_calls)
    assert all(call["argv"][:2] == ["--glab", "api"] for call in provider_calls)
    assert "Completion: COMPLETE" in human
    assert output["run_id"] in human
    assert output["unit_id"] in human
    assert "correctness" in human

    with FinalStore(standalone.store_path) as reopened:
        assert reopened.machine_report(output["run_id"])["run"]["state"] == "pass"
        assert reopened.machine_report(output["run_id"])["publications"][0]["status"] == \
            "local_only"


def test_blocking_correctness_finding_is_normalized_and_never_an_official_pass(standalone):
    _script_final_result(standalone, {
        "verdict": "pass",
        "findings": [{
            "id": "return-contract",
            "severity": "critical",
            "confidence": 0.96,
            "path": "app.py",
            "start_line": 2,
            "end_line": 2,
            "content": "The implementation ignores the required return value.",
            "existing_code": "return 1",
            "evidence": "The caller requires the normalized value.",
        }],
        "warnings": [
            {"code": "speculative-concern", "message": "A caller may also need review",
             "scope": "app.py", "details": {"confidence": 0.2}},
            "plain reviewer warning",
        ],
    })

    output = _result(_gate("final-review", standalone.ref), 1)
    machine, human = _reports(output)

    assert output["finding_verdict"] == "block"
    assert output["state"] == "findings"
    assert output["complete"] is True
    unit = machine["units"][0]
    assert unit["state"] == "findings"
    assert machine["run"]["state"] == "findings"
    assert len(unit["findings"]) == 1
    finding = unit["findings"][0]
    assert finding["finding_key"] == "return-contract"
    assert finding["severity"] == "high"
    assert finding["confidence"] == "0.96"
    assert finding["lens"] == "correctness"
    assert finding["path"] == "app.py"
    assert finding["line"] == 2
    assert finding["state"] == "open"
    assert finding["evidence"]["worker"]["existing_code"] == "return 1"
    assert finding["anchors"][0]["anchor_type"] == "content"
    assert finding["anchors"][0]["anchor"].startswith("sha256:")
    assert [warning["code"] for warning in unit["warnings"]] == [
        "speculative-concern", "reviewer-warning", "required-verification",
    ]
    assert unit["warnings"][1]["message"] == "plain reviewer warning"
    assert machine["publications"] == []
    assert not standalone.calls.with_name("publication.json").exists()
    assert "The implementation ignores" in human
    assert not Path(_worker_trace(standalone)["request"]["repository"]["path"]).exists()


@pytest.mark.parametrize("failure", [
    ("STUB_FINAL_RAW_FOR", "not a worker result"),
    ("STUB_FINAL_EXIT_FOR", "9"),
])
@pytest.mark.parametrize("failed_lens", LENS_NAMES)
def test_worker_failure_is_incomplete_with_reports_and_cleanup(
        standalone, failed_lens, failure):
    key, value = failure
    standalone.monkeypatch.setenv(key, json.dumps({failed_lens: value}))

    output = _result(_gate("final-review", standalone.ref), 2)
    machine, _ = _reports(output)

    assert output["finding_verdict"] is None
    assert output["state"] == "incomplete"
    assert output["static_lenses_complete"] is False
    unit = machine["units"][0]
    assert unit["state"] == "incomplete"
    lenses = {lens["name"]: lens for lens in unit["lenses"]}
    assert list(lenses) == list(LENS_NAMES)
    assert lenses[failed_lens]["state"] == "incomplete"
    assert all(lens["state"] == "completed" for name, lens in lenses.items()
               if name != failed_lens)
    coverage = _coverage(unit)
    assert coverage[f"lens:{failed_lens}"]["status"] == "incomplete"
    assert coverage["mandatory_lenses"]["status"] == "incomplete"
    assert [warning["code"] for warning in unit["warnings"]] == [
        "required-lens-failed", "required-verification",
    ]
    warning = unit["warnings"][0]
    assert warning["code"] == "required-lens-failed"
    assert warning["details"]["lens"] == failed_lens
    assert coverage["required_tests"]["status"] == "absent"
    assert coverage["dynamic_tests"]["status"] == "not_applicable"
    assert unit["findings"] == []
    traces = _worker_traces(standalone)
    assert [entry["request"]["lens"] for entry in traces] == [*LENS_NAMES, *LENS_NAMES]
    assert not Path(traces[0]["request"]["repository"]["path"]).exists()
    assert not list(Path(rg_store.clones_dir()).glob("*-final-*"))


def test_unparseable_finding_cannot_be_dropped_into_a_clean_verdict(standalone):
    _script_final_result(standalone, {"findings": ["not an object"]})

    output = _result(_gate("final-review", standalone.ref), 2)
    machine, _ = _reports(output)

    assert output["finding_verdict"] is None
    assert output["static_lenses_complete"] is False
    unit = machine["units"][0]
    assert unit["state"] == "incomplete"
    lenses = {lens["name"]: lens for lens in unit["lenses"]}
    assert lenses["correctness"]["state"] == "incomplete"
    assert all(lens["state"] == "completed" for name, lens in lenses.items()
               if name != "correctness")
    assert [warning["code"] for warning in unit["warnings"]] == [
        "required-lens-failed", "required-verification",
    ]
    assert unit["warnings"][0]["code"] == "required-lens-failed"
    assert unit["warnings"][0]["details"]["lens"] == "correctness"
    assert "unparseable finding" in unit["warnings"][0]["message"]
    assert unit["findings"] == []


def test_revision_movement_is_stale_and_preserves_worker_evidence(standalone):
    scripted = json.loads(standalone.script.read_text(encoding="utf-8"))
    changed = dict(standalone.merge_request)
    moved = "d" * 40
    changed["sha"] = moved
    changed["diff_refs"] = dict(changed["diff_refs"], head_sha=moved)
    scripted["revalidated_merge_request"] = changed
    standalone.script.write_text(json.dumps(scripted), encoding="utf-8")
    _script_final_result(standalone, {
        "findings": [{
            "path": "app.py", "start_line": 2, "end_line": 2,
            "severity": "high", "confidence": 0.9, "content": "Pinned defect",
        }]
    })

    output = _result(_gate("final-review", standalone.ref), 3)
    machine, _ = _reports(output)

    assert output["state"] == "stale"
    assert output["finding_verdict"] == "block"
    assert machine["run"]["state"] == "stale"
    assert machine["units"][0]["state"] == "stale"
    assert machine["units"][0]["findings"][0]["content"] == "Pinned defect"
    assert any(warning["code"] == "revision-stale"
               for warning in machine["units"][0]["warnings"])
    assert machine["revisions"]["items"][0]["source_sha"] == standalone.head
    assert moved not in json.dumps(machine["revisions"])
    assert machine["publications"] == []
    assert not Path(_worker_trace(standalone)["request"]["repository"]["path"]).exists()


def test_numeric_reference_and_arbitrary_fork_use_provider_repository_identity(standalone):
    scripted = json.loads(standalone.script.read_text(encoding="utf-8"))
    scripted["merge_request"]["source_project_id"] = 222
    standalone.script.write_text(json.dumps(scripted), encoding="utf-8")

    output = _result(_gate("final-review", "!17", cwd=standalone.repo), 0)
    machine, _ = _reports(output)

    assert output["complete"] is True

    item = machine["revisions"]["items"][0]
    assert item["source_project"] == "arbitrary-contributor/fork"
    assert item["target_project"] == "group/project"
    assert item["source_ref"] == "contributor/feature"
    assert item["source_sha"] == standalone.head
    calls = json.loads(standalone.calls.read_text(encoding="utf-8"))
    assert calls[0]["argv"][-1] == "projects/:fullpath/merge_requests/17"
    assert any(call["argv"][-1] == "projects/222" for call in calls)
    assert len(calls) == 6


@pytest.mark.parametrize(("finding", "expected_verdict", "expected_exit", "unit_state"), [
    ({"path": "app.py", "start_line": 2, "end_line": 2, "severity": "medium",
      "confidence": 0.95, "content": "High-confidence medium defect"},
     "block", 1, "findings"),
    ({"path": "app.py", "severity": "high", "confidence": 0.99,
      "content": "High finding without an actionable line"},
     "warn", 2, "incomplete"),
])
def test_compute_verdict_alone_decides_normalized_finding_disposition(
        standalone, finding, expected_verdict, expected_exit, unit_state):
    _script_final_result(standalone, {
        "verdict": "pass" if expected_verdict == "block" else "block",
        "findings": [finding],
    })

    output = _result(_gate("final-review", standalone.ref), expected_exit)
    machine, _ = _reports(output)

    assert output["finding_verdict"] == expected_verdict
    assert output["state"] == ("findings" if expected_verdict == "block" else "incomplete")
    assert output["complete"] is (expected_verdict == "block")
    assert machine["units"][0]["state"] == unit_state
    assert len(machine["units"][0]["findings"]) == 1
    assert machine["units"][0]["findings"][0]["content"] == finding["content"]


def test_revalidation_read_failure_is_incomplete_not_stale_or_a_pass(standalone):
    scripted = json.loads(standalone.script.read_text(encoding="utf-8"))
    changed = dict(standalone.merge_request)
    changed["diff_refs"] = {}
    scripted["revalidated_merge_request"] = changed
    standalone.script.write_text(json.dumps(scripted), encoding="utf-8")

    output = _result(_gate("final-review", standalone.ref), 2)
    machine, _ = _reports(output)

    assert output["state"] == "incomplete"
    assert output["finding_verdict"] == "pass"
    unit = machine["units"][0]
    assert unit["state"] == "incomplete"
    assert unit["lenses"][0]["state"] == "completed"
    assert any(warning["code"] == "freshness-check" for warning in unit["warnings"])
    assert not any(warning["code"] == "revision-stale" for warning in unit["warnings"])
    assert machine["publications"] == []
    assert not Path(_worker_trace(standalone)["request"]["repository"]["path"]).exists()


def test_malformed_warning_array_also_makes_the_lens_incomplete(standalone):
    _script_final_result(
        standalone, {"findings": [], "warnings": "not-an-array"},
        lens="documentation/domain",
    )

    output = _result(_gate("final-review", standalone.ref), 2)
    machine, _ = _reports(output)

    assert output["finding_verdict"] is None
    unit = machine["units"][0]
    lenses = {lens["name"]: lens for lens in unit["lenses"]}
    assert lenses["documentation/domain"]["state"] == "incomplete"
    assert all(lens["state"] == "completed" for name, lens in lenses.items()
               if name != "documentation/domain")
    assert [warning["code"] for warning in unit["warnings"]] == [
        "required-lens-failed", "required-verification",
    ]
    assert "warnings must be arrays" in unit["warnings"][0]["message"]
    assert unit["warnings"][0]["details"]["lens"] == "documentation/domain"
    assert not Path(_worker_trace(standalone)["request"]["repository"]["path"]).exists()


def test_binary_merge_base_diff_reaches_the_worker_without_utf8_loss(standalone):
    _git(standalone.repo, "checkout", "-qf", "feature")
    (standalone.repo / "payload.bin").write_bytes(b"\xff\x00\x80binary\n")
    head = _commit(standalone.repo, "binary feature")
    scripted = json.loads(standalone.script.read_text(encoding="utf-8"))
    scripted["merge_request"]["sha"] = head
    scripted["merge_request"]["diff_refs"]["head_sha"] = head
    standalone.script.write_text(json.dumps(scripted), encoding="utf-8")

    output = _result(_gate("final-review", standalone.ref), 0)
    trace = _worker_trace(standalone)

    assert output["complete"] is True
    assert output["finding_verdict"] == "pass"
    assert "GIT binary patch" in trace["request"]["diff"]
    assert not Path(trace["request"]["repository"]["path"]).exists()


def test_materialization_failure_has_auditable_output_and_no_leftover_clone(standalone):
    scripted = json.loads(standalone.script.read_text(encoding="utf-8"))
    absent = "e" * 40
    scripted["merge_request"]["sha"] = absent
    scripted["merge_request"]["diff_refs"]["head_sha"] = absent
    standalone.script.write_text(json.dumps(scripted), encoding="utf-8")

    output = _result(_gate("final-review", standalone.ref), 2)
    machine, _ = _reports(output)

    assert output["state"] == "incomplete"
    assert output["finding_verdict"] is None
    unit = machine["units"][0]
    assert unit["state"] == "incomplete"
    assert _coverage(unit)["repository"]["status"] == "incomplete"
    assert any(warning["code"] == "materialization" for warning in unit["warnings"])
    assert not standalone.trace.exists()
    assert not list(Path(rg_store.clones_dir()).glob("*-final-*"))
    assert (standalone.repo / "local-only.txt").read_text(encoding="utf-8") == \
        "uncommitted local secret\n"
