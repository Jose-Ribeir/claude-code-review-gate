"""Declared required verification for immutable final-review trees."""
import json
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import rg_config  # noqa: E402
import rg_final_verification as verification  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(path: Path, files: dict[str, str]) -> tuple[Path, str]:
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "verification@example.invalid")
    _git(path, "config", "user.name", "Verification Test")
    _git(path, "config", "commit.gpgsign", "false")
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        if content.startswith("#!"):
            target.chmod(0o755)
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "review tree")
    return path, _git(path, "rev-parse", "HEAD")


def test_repository_without_declared_required_suite_is_explicitly_absent(tmp_path):
    repo, commit = _repo(tmp_path / "source", {"app.py": "VALUE = 1\n"})

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "absent"
    assert result["blocking"] is False
    assert result["suites"] == []
    assert result["warnings"] == [
        "no required verification suite is declared by repository checks or CI"
    ]
    assert result["commit"] == commit
    assert not (repo / ".git" / "verification-clones").exists()


def test_declared_required_check_passes_in_a_disposable_clone(tmp_path):
    script = "#!/bin/sh\nprintf 'required-ok %s %s\\n' \"$PWD\" \"$(git rev-parse HEAD)\"\n"
    repo, commit = _repo(tmp_path / "source", {
        ".ocr/config.json": (
            '{"checks":{"required-smoke":{"command":"tools/required-check",'
            '"required":true,"globs":["**/*"]}}}'
        ),
        "tools/required-check": script,
    })

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "pass"
    assert result["blocking"] is False
    assert result["warnings"] == []
    assert len(result["suites"]) == 1
    suite = result["suites"][0]
    assert suite["name"] == "required-smoke"
    assert suite["kind"] == "required"
    assert suite["state"] == "pass"
    assert suite["source"] == ".ocr/config.json"
    assert suite["command"] == "tools/required-check"
    assert len(suite["attempts"]) == 1
    assert suite["attempts"][0]["returncode"] == 0
    assert suite["evidence_fingerprint"].startswith("sha256:")
    marker, clone, verified_commit = suite["output"].strip().split()
    assert marker == "required-ok"
    assert clone != str(repo)
    assert verified_commit == commit
    assert not Path(clone).exists()
    assert not (repo / ".git" / "verification-clones").exists()


def test_reproduced_required_failure_reruns_once_and_blocks_with_evidence(tmp_path):
    count = tmp_path / "attempt-count"
    script = (
        "#!/bin/sh\n"
        f"printf 'attempt\\n' >> '{count}'\n"
        "printf 'deliberate-required-failure\\n'\n"
        "exit 7\n"
    )
    repo, commit = _repo(tmp_path / "source", {
        ".ocr/config.json": (
            '{"checks":{"required-tests":{"command":"tools/required-check",'
            '"required":true,"globs":["**/*"]}}}'
        ),
        "tools/required-check": script,
    })

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "block"
    assert result["blocking"] is True
    assert result["suites"][0]["state"] == "fail"
    assert result["suites"][0]["blocking"] is True
    assert [a["returncode"] for a in result["suites"][0]["attempts"]] == [7, 7]
    assert count.read_text(encoding="utf-8").splitlines() == ["attempt", "attempt"]
    assert "deliberate-required-failure" in result["suites"][0]["output"]
    assert result["findings"] == [{
        "category": "required-verification",
        "severity": "high",
        "confidence": 1.0,
        "name": "required-tests",
        "content": "required verification 'required-tests' failed on its initial run and one rerun",
        "evidence_fingerprint": result["suites"][0]["evidence_fingerprint"],
    }]


def test_required_command_receives_only_an_explicit_safe_environment(
        monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "provider-write-secret")
    monkeypatch.setenv("GLAB_TOKEN", "another-provider-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "unrelated-secret")
    monkeypatch.setenv("PYTHONPATH", "/unrelated/injection")
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(tmp_path / "missing" / "trace.json"))
    script = (
        "#!/bin/sh\n"
        "printf '%s|%s|%s|%s|%s\\n' \"${GH_TOKEN-unset}\" "
        "\"${GLAB_TOKEN-unset}\" \"${AWS_SECRET_ACCESS_KEY-unset}\" "
        "\"${PYTHONPATH-unset}\" \"${GIT_TRACE2_EVENT-unset}\"\n"
    )
    repo, commit = _repo(tmp_path / "source", {
        ".ocr/config.json": json.dumps({"checks": {"safe": {
            "command": "tools/required-check", "required": True,
        }}}),
        "tools/required-check": script,
    })

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "pass", result
    assert result["suites"][0]["output"].strip() == "unset|unset|unset|unset|unset"
    assert "provider-write-secret" not in json.dumps(result)
    assert "unrelated-secret" not in json.dumps(result)


def test_required_is_a_resolved_command_check_option_not_a_new_check_kind(
        monkeypatch, tmp_path):
    monkeypatch.setenv("OCR_STATE_DIR", str(tmp_path / "state-without-global-config"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".ocr").mkdir()
    (repo / ".ocr" / "config.json").write_text(json.dumps({"checks": {
        "required-tests": {
            "command": "tools/test", "required": True, "globs": ["**/*"],
        },
        "ordinary-check": {"command": "tools/other", "globs": ["**/*"]},
    }}), encoding="utf-8")

    settings = rg_config.load_settings(repo)

    assert settings["checks"]["required-tests"]["required"] is True
    assert settings["checks"]["ordinary-check"]["required"] is False
    assert all("required" not in note for note in settings["notes"])


def test_discovery_and_execution_use_the_commit_not_mutable_worktree(tmp_path):
    repo, commit = _repo(tmp_path / "source", {
        ".ocr/config.json": json.dumps({"checks": {"pinned": {
            "command": "tools/required-check", "required": True,
        }}}),
        "tools/required-check": "#!/bin/sh\nprintf 'committed-version\\n'\n",
        "app.txt": "committed\n",
    })
    (repo / ".ocr" / "config.json").write_text(json.dumps({"checks": {
        "working-tree-only": {"command": "tools/missing", "required": True},
    }}), encoding="utf-8")
    (repo / "tools" / "required-check").write_text(
        "#!/bin/sh\nprintf 'modified-version\\n'\nexit 9\n", encoding="utf-8")
    (repo / "app.txt").write_text("uncommitted\n", encoding="utf-8")

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "pass", result
    assert result["suites"][0]["name"] == "pinned"
    assert result["suites"][0]["output"].strip() == "committed-version"
    assert (repo / "app.txt").read_text(encoding="utf-8") == "uncommitted\n"


def test_only_declared_ci_commands_run_never_manifest_inferred_commands(tmp_path):
    workflow = """name: verification
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - name: Required repository check
        run: tools/github-check
      - name: Diagnostic only
        if: failure()
        run: tools/must-not-run
"""
    gitlab = """stages: [test]
required_tests:
  stage: test
  script:
    - tools/gitlab-check
optional_tests:
  stage: test
  allow_failure: true
  script:
    - tools/must-not-run
"""
    repo, commit = _repo(tmp_path / "source", {
        ".github/workflows/verify.yml": workflow,
        ".gitlab-ci.yml": gitlab,
        "package.json": json.dumps({"scripts": {
            "test": "tools/must-not-run",
        }}),
        "pyproject.toml": "[tool.pytest.ini_options]\n",
        "tools/github-check": "#!/bin/sh\nprintf 'github-required\\n'\n",
        "tools/gitlab-check": "#!/bin/sh\nprintf 'gitlab-required\\n'\n",
        "tools/must-not-run": "#!/bin/sh\nexit 99\n",
    })

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "pass", result
    by_source = {suite["source"]: suite for suite in result["suites"]}
    assert set(by_source) == {".github/workflows/verify.yml", ".gitlab-ci.yml"}
    assert by_source[".github/workflows/verify.yml"]["command"] == "tools/github-check"
    assert by_source[".gitlab-ci.yml"]["command"] == "tools/gitlab-check"
    assert {suite["output"].strip() for suite in result["suites"]} == {
        "github-required", "gitlab-required",
    }


def test_unexecutable_declared_suite_is_incomplete_and_not_rerun(tmp_path):
    repo, commit = _repo(tmp_path / "source", {
        ".ocr/config.json": json.dumps({"checks": {"missing-tool": {
            "command": "tools/does-not-exist", "required": True,
        }}}),
    })

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "incomplete"
    assert result["blocking"] is False
    assert result["findings"] == []
    attempt = result["suites"][0]["attempts"]
    assert len(attempt) == 1
    assert attempt[0]["returncode"] == 127
    assert result["suites"][0]["state"] == "incomplete"
    assert "not executable" in result["warnings"][0]


def test_failure_not_reproduced_on_the_single_rerun_is_incomplete_not_a_pass(tmp_path):
    marker = tmp_path / "first-attempt"
    script = (
        "#!/bin/sh\n"
        f"if [ -f '{marker}' ]; then printf 'rerun-passed\\n'; exit 0; fi\n"
        f"touch '{marker}'\n"
        "printf 'initial-failure\\n'\n"
        "exit 4\n"
    )
    repo, commit = _repo(tmp_path / "source", {
        ".ocr/config.json": json.dumps({"checks": {"flaky-required": {
            "command": "tools/required-check", "required": True,
        }}}),
        "tools/required-check": script,
    })

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "incomplete"
    assert result["blocking"] is False
    assert result["findings"] == []
    assert result["suites"][0]["state"] == "incomplete"
    assert [a["returncode"] for a in result["suites"][0]["attempts"]] == [4, 0]
    assert "not reproduced" in result["warnings"][0]


def test_timeout_is_incomplete_and_disposable_clone_is_still_removed(tmp_path):
    clone_path = tmp_path / "clone-path"
    script = (
        "#!/bin/sh\n"
        f"printf '%s' \"$PWD\" > '{clone_path}'\n"
        "sleep 2\n"
    )
    repo, commit = _repo(tmp_path / "source", {
        ".ocr/config.json": json.dumps({"checks": {"slow-required": {
            "command": "tools/required-check", "required": True, "timeout": 1,
        }}}),
        "tools/required-check": script,
    })

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "incomplete"
    assert result["blocking"] is False
    assert result["suites"][0]["state"] == "incomplete"
    assert result["suites"][0]["attempts"] == []
    assert "timed out" in result["warnings"][0]
    assert not Path(clone_path.read_text(encoding="utf-8")).exists()
