"""Bounded native decomposition of the legacy deep-review lane."""
import importlib.util
import json
from pathlib import Path
import re
import sys
import threading

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fake_backend  # noqa: E402
from test_native_executor import _FakeHost  # noqa: E402


spec = importlib.util.spec_from_file_location(
    "review_gate_legacy_deep", ROOT / "scripts" / "review-gate.py",
)
review_gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review_gate)


class _CoverageHost(_FakeHost):
    def launch(self, task, callback):
        session = super().launch(task, callback)
        self.complete(task)
        return session

    def complete(self, task):
        self.sessions[task.task_id].finish(self.payload(task))

    def manifest(self, task):
        match = re.search(r'--paths-file "([^"]+)"', task.prompt)
        assert match, task.prompt
        return json.loads(Path(match.group(1)).read_text(encoding="utf-8"))

    def payload(self, task):
        manifest = self.manifest(task)
        duties = manifest.get("native_deep_task", {}).get("duties", [])
        return {
            "status": "success",
            "verdict": "pass",
            "summary": {"files_reviewed": len(manifest["paths"]), "findings": 0},
            "findings": [],
            "warnings": [],
            "native_deep_evidence": {
                "task_id": task.task_id,
                "input_sha256": task.input_sha256,
                "manifest_sha256": manifest.get("manifest_sha256", ""),
                "reviewed_paths": manifest["paths"],
                "completed_duties": duties,
            },
        }


class _ManualHost(_CoverageHost):
    def __init__(self, expected_tasks):
        super().__init__()
        self.tasks = {}
        self.all_launched = threading.Event()
        self.expected_tasks = expected_tasks

    def launch(self, task, callback):
        session = _FakeHost.launch(self, task, callback)
        self.tasks[task.task_id] = task
        if len(self.tasks) == self.expected_tasks:
            self.all_launched.set()
        return session


def _run_deep(tmp_path, monkeypatch, host, paths, *, timeout=5):
    monkeypatch.setenv("OCR_BACKEND", "kimchi")
    monkeypatch.setenv("OCR_DEEP_TRANSPORT", "native")
    monkeypatch.setenv(
        "KIMCHI_BIN", fake_backend.install(tmp_path / "bin", "kimchi"),
    )
    monkeypatch.setenv("OCR_SKIP_CANARY", "1")
    manifest = review_gate._build_deep_manifest(paths, ["risky"], 7)
    manifest_path = tmp_path / "deep.json"
    review_gate._write_manifest_file(str(manifest_path), manifest, "test deep manifest")
    monkeypatch.setattr(
        "rg_legacy_deep.KimchiNativeHost.checked", lambda *args, **kwargs: host,
    )
    result, ran, _raw_name = review_gate._run_review(
        str(tmp_path), "hook", str(tmp_path), "t" * 40, "a..b",
        paths_file=str(manifest_path), timeout=timeout, tier="deep",
        common_dir=str(tmp_path), native=True, run_id="deep-partitioned",
    )
    assert ran is True
    return result


@pytest.mark.parametrize("reverse", [False, True])
def test_large_native_deep_review_covers_every_file_once_with_global_duties(
        tmp_path, monkeypatch, reverse):
    paths = [f"pkg/file-{index:02d}.py" for index in range(10)]
    if reverse:
        paths.reverse()
    host = _CoverageHost()

    result = _run_deep(tmp_path, monkeypatch, host, paths)

    assert result["verdict"] == "pass"
    assert len(host.sessions) == 4  # three bounded file tasks and one global task
    for task_id, session in host.sessions.items():
        assert session.native_session_id == "fake-native-" + task_id

    async_dir = review_gate._async_dir(str(tmp_path))
    journal_path = async_dir / "native-deep-partitioned-deep-tasks.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    native_tasks = journal["native_tasks"]
    for recorded in native_tasks:
        identity = recorded["identity"]
        artifact = recorded["artifacts"][0]["artifact"]
        assert artifact["identity"] == identity
        assert artifact["payload"]["native_deep_evidence"]["input_sha256"] == (
            identity["input_sha256"]
        )
        manifest = json.loads((async_dir / f"manifest-{identity['task_id']}.json").read_text(
            encoding="utf-8",
        ))
        assert sorted(manifest["paths"] + manifest["other_changed"]) == sorted(paths)
        if identity["lens"] == "deep-files":
            assert 1 <= len(manifest["paths"]) <= 4
            assert set(manifest["native_deep_task"]["duties"]) >= {
                "changed-file-contracts", "changed-call-chains",
            }

    covered = [
        path
        for recorded in native_tasks
        if recorded["identity"]["lens"] == "deep-files"
        for path in recorded["artifacts"][0]["artifact"]["payload"][
            "native_deep_evidence"
        ]["reviewed_paths"]
    ]
    global_tasks = [
        recorded for recorded in native_tasks
        if recorded["identity"]["lens"] == "deep-global"
    ]
    assert sorted(covered) == sorted(paths)
    assert len(covered) == len(set(covered))
    assert len(global_tasks) == 1
    duties = global_tasks[0]["artifacts"][0]["artifact"]["payload"][
        "native_deep_evidence"
    ]["completed_duties"]
    assert "cross-file-call-chains" in duties
    assert "global-invariants" in duties


def test_repeated_attempt_reuses_valid_journaled_artifacts_without_reexecution(
        tmp_path, monkeypatch):
    paths = [f"pkg/file-{index}.py" for index in range(5)]
    first = _run_deep(tmp_path, monkeypatch, _CoverageHost(), paths)
    async_dir = review_gate._async_dir(str(tmp_path))
    artifact_paths = sorted(async_dir.glob("native-deep-partitioned-deep-*.json"))
    artifact_paths = [path for path in artifact_paths if "tasks" not in path.name]
    before = {path: path.read_bytes() for path in artifact_paths}
    assert before

    resumed_host = _CoverageHost()
    resumed = _run_deep(tmp_path, monkeypatch, resumed_host, paths)

    assert resumed["verdict"] == first["verdict"]
    assert resumed_host.sessions == {}
    assert {path: path.read_bytes() for path in artifact_paths} == before


def test_malformed_journaled_coverage_is_rejected_without_reexecution(tmp_path, monkeypatch):
    host = _CoverageHost()
    paths = [f"pkg/file-{index}.py" for index in range(5)]
    _run_deep(tmp_path, monkeypatch, host, paths)
    async_dir = review_gate._async_dir(str(tmp_path))
    journal_path = async_dir / "native-deep-partitioned-deep-tasks.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    recorded = journal["native_tasks"][0]
    artifact = recorded["artifacts"][0]["artifact"]
    artifact["payload"]["native_deep_evidence"]["completed_duties"] = []
    artifact_path = async_dir / f"native-{recorded['identity']['task_id']}.json"
    artifact_path.write_text(json.dumps(artifact) + "\n", encoding="utf-8")
    journal_path.write_text(json.dumps(journal) + "\n", encoding="utf-8")
    resumed_host = _CoverageHost()

    with pytest.raises(review_gate.ReviewGateError, match="misidentified evidence"):
        _run_deep(tmp_path, monkeypatch, resumed_host, paths)

    assert resumed_host.sessions == {}
