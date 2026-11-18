"""Declared required verification for immutable final-review trees."""
import json
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import rg_config  # noqa: E402
import rg_final_execution as execution  # noqa: E402
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


def test_github_action_invocation_is_discovered_as_unassessable(tmp_path):
    workflow = """jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
"""
    repo, commit = _repo(tmp_path / "source", {
        ".github/workflows/verify.yml": workflow,
    })

    declarations = verification.discover_required_verification(repo, commit)
    result = verification.run_required_verification(repo, commit)

    assert len(declarations) == 1
    assert declarations[0]["state"] == "unassessable"
    assert "actions/checkout@v4" in declarations[0]["error"]
    assert result["status"] == "incomplete"
    assert result["status"] != "absent"
    assert result["suites"][0]["state"] == "unassessable"
    assert result["suites"][0]["attempts"] == []
    assert result["blocking"] is False
    assert result["findings"] == []
    assert "actions/checkout@v4" in result["warnings"][0]


def test_github_matrix_makes_its_required_commands_unassessable(tmp_path):
    workflow = """jobs:
  verify:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python: ['3.11', '3.12']
    steps:
      - run: tools/must-not-run
"""
    repo, commit = _repo(tmp_path / "source", {
        ".github/workflows/verify.yml": workflow,
        "tools/must-not-run": "#!/bin/sh\nexit 99\n",
    })

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "incomplete"
    assert len(result["suites"]) == 1
    assert result["suites"][0]["state"] == "unassessable"
    assert result["suites"][0]["attempts"] == []
    assert "matrix" in result["warnings"][0]


def test_github_reusable_workflow_job_is_not_mistaken_for_absent_ci(tmp_path):
    workflow = """jobs:
  verify:
    uses: example/required/.github/workflows/tests.yml@v1
"""
    repo, commit = _repo(tmp_path / "source", {
        ".github/workflows/verify.yaml": workflow,
    })

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "incomplete"
    assert len(result["suites"]) == 1
    assert result["suites"][0]["state"] == "unassessable"
    assert result["suites"][0]["attempts"] == []
    assert "reusable workflow" in result["warnings"][0]
    assert "example/required" in result["warnings"][0]


def test_unresolved_github_expression_is_unassessable_not_a_shell_failure(tmp_path):
    workflow = """jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - run: printf '%s\\n' '${{ github.sha }}'
"""
    repo, commit = _repo(tmp_path / "source", {
        ".github/workflows/verify.yml": workflow,
    })

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "incomplete"
    assert result["blocking"] is False
    assert result["suites"][0]["state"] == "unassessable"
    assert result["suites"][0]["attempts"] == []
    assert "expression" in result["warnings"][0]


def test_github_literal_block_preserves_nested_python_heredoc_indentation(tmp_path):
    workflow = """jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - run: |
          python3 - <<'PY'
          import json
          from pathlib import Path
          for path in Path('.').glob('*.json'):
              try:
                  payload = json.loads(path.read_text(encoding='utf-8'))
              except ValueError as error:
                  raise SystemExit(f'{path}: {error}')
              assert payload['valid'] is True
          print('nested-heredoc-ok')
          PY
"""
    repo, commit = _repo(tmp_path / "source", {
        ".github/workflows/verify.yml": workflow,
        "data.json": '{"valid": true}\n',
    })

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "pass", result
    assert result["suites"][0]["output"] == "nested-heredoc-ok\n"
    assert result["suites"][0]["attempts"][0]["returncode"] == 0


def test_github_block_content_preserves_literal_lines_and_rejects_complex_folding(
        tmp_path):
    workflow = """jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - run: |
          printf 'literal-start\\n'

          if true; then
            printf 'literal-nested\\n'
          fi
      - run: printf 'next-step\\n'
      - run: >
          printf 'folded-must-not-run\\n'

          printf 'also-must-not-run\\n'
"""
    repo, commit = _repo(tmp_path / "source", {
        ".github/workflows/verify.yml": workflow,
    })

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "incomplete", result
    assert [suite["command"] for suite in result["suites"][:2]] == [
        "printf 'literal-start\\n'\n\nif true; then\n"
        "  printf 'literal-nested\\n'\nfi",
        "printf 'next-step\\n'",
    ]
    assert [suite["output"] for suite in result["suites"][:2]] == [
        "literal-start\nliteral-nested\n",
        "next-step\n",
    ]
    folded = result["suites"][2]
    assert folded["state"] == "unassessable"
    assert folded["command"] is None
    assert folded["attempts"] == []
    assert "folded-must-not-run" not in result["suites"][0]["output"]
    assert "also-must-not-run" not in result["suites"][1]["output"]


def test_gitlab_root_docker_and_global_settings_are_not_jobs(tmp_path):
    pipeline = """image: ruby:3.2
types: [test]
services:
  - name: docker:24.0-dind
    command: ["printf", "service-command-is-not-a-job"]
variables:
  DOCKER_HOST: tcp://docker:2376
  ENTRYPOINT: "printf 'variable-command-is-not-a-job\\n'"
cache:
  key: docker-cache
  paths: [vendor/]
before_script: printf 'global-before-is-not-a-job\\n'
after_script:
  - printf 'global-after-is-not-a-job\\n'
"required tests":
  script: printf 'quoted-required-ok\\n'
'integration tests':
  script:
    - printf 'single-quoted-required-ok\\n'
"""
    repo, commit = _repo(tmp_path / "source", {".gitlab-ci.yml": pipeline})

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "pass", result
    assert result["warnings"] == []
    assert [(suite["name"], suite["command"], suite["output"])
            for suite in result["suites"]] == [
        ("gitlab:required tests:script-1", "printf 'quoted-required-ok\\n'",
         "quoted-required-ok\n"),
        ("gitlab:integration tests:script-1", "printf 'single-quoted-required-ok\\n'",
         "single-quoted-required-ok\n"),
    ]


def test_gitlab_unknown_and_inline_job_commands_remain_unassessable(tmp_path):
    pipeline = """image: ruby:3.2
unsupported_root: printf 'unsupported-root-evidence\\n'
"inline tests": {script: "printf 'inline-job-evidence\\n'"}
'required tests':
  script: printf 'quoted-required-ok\\n'
"""
    repo, commit = _repo(tmp_path / "source", {".gitlab-ci.yml": pipeline})

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "incomplete", result
    assert result["status"] != "absent"
    assert result["blocking"] is False
    assert len(result["suites"]) == 3
    by_name = {suite["name"]: suite for suite in result["suites"]}
    assert by_name["gitlab:required tests:script-1"]["state"] == "pass"
    assert by_name["gitlab:required tests:script-1"]["output"] == "quoted-required-ok\n"
    unsupported = by_name["gitlab:unsupported_root"]
    inline = by_name["gitlab:inline tests"]
    assert unsupported["state"] == "unassessable"
    assert inline["state"] == "unassessable"
    assert unsupported["attempts"] == []
    assert inline["attempts"] == []
    evidence = "\n".join(result["warnings"])
    assert "unsupported-root-evidence" in evidence
    assert "inline-job-evidence" in evidence


def test_gitlab_include_is_unassessable_instead_of_absent(tmp_path):
    pipeline = """include:
  - remote: https://example.invalid/required-tests.yml
"""
    repo, commit = _repo(tmp_path / "source", {".gitlab-ci.yml": pipeline})

    declarations = verification.discover_required_verification(repo, commit)
    result = verification.run_required_verification(repo, commit)

    assert len(declarations) == 1
    assert declarations[0]["state"] == "unassessable"
    assert result["status"] == "incomplete"
    assert result["suites"][0]["attempts"] == []
    assert result["blocking"] is False
    assert "include" in result["warnings"][0]
    assert "https://example.invalid/required-tests.yml" in result["warnings"][0]


def test_gitlab_setup_and_matrix_constructs_are_unassessable(tmp_path):
    pipeline = """.required_setup:
  script:
    - tools/must-not-run
extended_tests:
  extends: .required_setup
  script:
    - tools/must-not-run
matrix_tests:
  parallel:
    matrix:
      - PYTHON: ['3.11', '3.12']
  script:
    - tools/must-not-run
"""
    repo, commit = _repo(tmp_path / "source", {
        ".gitlab-ci.yml": pipeline,
        "tools/must-not-run": "#!/bin/sh\nexit 99\n",
    })

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "incomplete"
    assert result["blocking"] is False
    assert len(result["suites"]) == 2
    assert all(suite["state"] == "unassessable" for suite in result["suites"])
    assert all(suite["attempts"] == [] for suite in result["suites"])
    assert any("extends" in warning for warning in result["warnings"])
    assert any("matrix" in warning for warning in result["warnings"])


def test_gitlab_inline_required_script_failure_blocks_end_to_end(tmp_path):
    pipeline = """required_tests:
  script: exit 99
"""
    repo, commit = _repo(tmp_path / "source", {".gitlab-ci.yml": pipeline})

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "block", result
    assert result["blocking"] is True
    assert len(result["suites"]) == 1
    suite = result["suites"][0]
    assert suite["name"] == "gitlab:required_tests:script-1"
    assert suite["command"] == "exit 99"
    assert suite["state"] == "fail"
    assert [attempt["returncode"] for attempt in suite["attempts"]] == [99, 99]
    assert len(result["findings"]) == 1
    finding = result["findings"][0]
    assert finding["name"] == suite["name"]
    assert finding["evidence_fingerprint"] == suite["evidence_fingerprint"]

    unit = execution.execute_unit(
        {"repository": {
            "path": str(repo), "head_sha": commit, "access": "full_tree",
        }, "diff": ""},
        worker=lambda _request: {
            "schema": "final-review-worker-result/v1",
            "status": "success", "summary": "lens complete",
            "findings": [], "warnings": [], "unreviewed": [],
        },
        required_runner=verification.run_required_verification,
    ).as_dict()
    assert unit["required_verification"]["status"] == "block"
    assert unit["complete"] is True
    assert unit["finding_verdict"] == "block"
    assert len(unit["required_verification"]["findings"]) == 1


def test_gitlab_literal_block_before_script_and_script_run(tmp_path):
    pipeline = """required_tests:
  before_script: |
    printf 'before-ok\\n'
  script: |
    printf 'script-ok\\n'
"""
    repo, commit = _repo(tmp_path / "source", {".gitlab-ci.yml": pipeline})

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "pass", result
    assert [(suite["name"], suite["command"], suite["output"])
            for suite in result["suites"]] == [
        ("gitlab:required_tests:before_script-1", "printf 'before-ok\\n'", "before-ok\n"),
        ("gitlab:required_tests:script-1", "printf 'script-ok\\n'", "script-ok\n"),
    ]
    assert all(len(suite["attempts"]) == 1 for suite in result["suites"])


def test_gitlab_list_block_scalar_runs_its_literal_body(tmp_path):
    pipeline = """required_tests:
  script:
    - |
      printf 'list-'
      printf 'block-ok\\n'
"""
    repo, commit = _repo(tmp_path / "source", {".gitlab-ci.yml": pipeline})

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "pass", result
    assert len(result["suites"]) == 1
    suite = result["suites"][0]
    assert suite["command"] == "printf 'list-'\nprintf 'block-ok\\n'"
    assert suite["output"] == "list-block-ok\n"
    assert len(suite["attempts"]) == 1


def test_gitlab_folded_blocks_run_simple_cases_and_retain_complex_evidence(tmp_path):
    pipeline = """folded_tests:
  before_script: >
    printf 'before-folded-ok\\n' &&
    true
  script: >
    printf 'script-folded-ok\\n' &&
    true
complex_folded_tests:
  script: >
    printf 'complex-must-not-run\\n'

    printf 'also-must-not-run\\n'
"""
    repo, commit = _repo(tmp_path / "source", {".gitlab-ci.yml": pipeline})

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "incomplete", result
    by_name = {suite["name"]: suite for suite in result["suites"]}
    assert len(by_name) == 3
    before = by_name["gitlab:folded_tests:before_script-1"]
    script = by_name["gitlab:folded_tests:script-1"]
    assert before["command"] == "printf 'before-folded-ok\\n' && true"
    assert before["output"] == "before-folded-ok\n"
    assert script["command"] == "printf 'script-folded-ok\\n' && true"
    assert script["output"] == "script-folded-ok\n"
    complex_suite = by_name["gitlab:complex_folded_tests:script"]
    assert complex_suite["state"] == "unassessable"
    assert complex_suite["attempts"] == []
    assert "complex-must-not-run" in result["warnings"][0]
    assert "also-must-not-run" in result["warnings"][0]


def test_gitlab_unsupported_nonempty_scripts_are_unassessable_never_executed(tmp_path):
    pipeline = """flow_tests:
  script: [echo flow-must-not-run]
nested_tests:
  script:
    unexpected: echo nested-must-not-run
tagged_tests:
  script: !!str echo tagged-must-not-run
explicit_indent_tests:
  script: |2
    echo indent-must-not-run
"""
    repo, commit = _repo(tmp_path / "source", {".gitlab-ci.yml": pipeline})

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "incomplete", result
    assert result["status"] != "absent"
    assert result["blocking"] is False
    assert result["findings"] == []
    assert len(result["suites"]) == 4
    assert all(suite["state"] == "unassessable" for suite in result["suites"])
    assert all(suite["command"] is None for suite in result["suites"])
    assert all(suite["attempts"] == [] for suite in result["suites"])
    evidence = "\n".join(result["warnings"])
    assert "flow-must-not-run" in evidence
    assert "nested-must-not-run" in evidence
    assert "tagged-must-not-run" in evidence
    assert "indent-must-not-run" in evidence


def test_gitlab_optional_hidden_and_non_script_jobs_do_not_create_suites(tmp_path):
    pipeline = """optional_tests:
  allow_failure: TRUE # explicitly diagnostic
  script: exit 99
.hidden_template:
  script: |
    exit 98
trigger_only:
  trigger: example/other-project
"optional quoted tests":
  allow_failure: true
  script: exit 97
'.hidden quoted template':
  script: exit 96
variables:
  REQUIRED_FLAG: '1'
"""
    repo, commit = _repo(tmp_path / "source", {".gitlab-ci.yml": pipeline})

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "absent", result
    assert result["blocking"] is False
    assert result["suites"] == []
    assert result["findings"] == []


def test_gitlab_quoted_and_whitespace_job_names_run_simple_commands(tmp_path):
    pipeline = """"required tests":
  script: printf 'double-inline-ok\\n'
'unit tests':
  script:
    - printf 'single-list-ok\\n'
required integration tests:
  script: |
    printf 'bare-block-ok\\n'
"""
    repo, commit = _repo(tmp_path / "source", {".gitlab-ci.yml": pipeline})

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "pass", result
    assert [(suite["name"], suite["command"], suite["output"])
            for suite in result["suites"]] == [
        ("gitlab:required tests:script-1", "printf 'double-inline-ok\\n'",
         "double-inline-ok\n"),
        ("gitlab:unit tests:script-1", "printf 'single-list-ok\\n'",
         "single-list-ok\n"),
        ("gitlab:required integration tests:script-1", "printf 'bare-block-ok\\n'",
         "bare-block-ok\n"),
    ]


def test_gitlab_quoted_jobs_retain_unassessable_construct_evidence(tmp_path):
    pipeline = """.required_setup:
  script: exit 99
"extended tests":
  extends: .required_setup
  script: exit 98
'flow tests':
  script: [echo flow-must-not-run]
"""
    repo, commit = _repo(tmp_path / "source", {".gitlab-ci.yml": pipeline})

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "incomplete", result
    assert len(result["suites"]) == 2
    assert all(suite["state"] == "unassessable" for suite in result["suites"])
    assert all(suite["attempts"] == [] for suite in result["suites"])
    evidence = "\n".join(result["warnings"])
    assert "extended tests" in evidence
    assert "extends" in evidence
    assert "flow tests" in evidence
    assert "flow-must-not-run" in evidence


def test_gitlab_unparseable_required_looking_key_is_not_absent(tmp_path):
    pipeline = '"required tests:\n  script: exit 99\n'
    repo, commit = _repo(tmp_path / "source", {".gitlab-ci.yml": pipeline})

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "incomplete", result
    assert result["status"] != "absent"
    assert len(result["suites"]) == 1
    assert result["suites"][0]["state"] == "unassessable"
    assert result["suites"][0]["attempts"] == []
    assert '"required tests:' in result["warnings"][0]
    assert "script: exit 99" in result["warnings"][0]


def test_gitlab_quoted_failure_blocks_instead_of_passing_as_absent(tmp_path):
    repo, commit = _repo(tmp_path / "source", {
        ".gitlab-ci.yml": '"true":\n  script: exit 99\n',
    })

    result = verification.run_required_verification(repo, commit)

    assert result["status"] == "block", result
    assert result["suites"][0]["name"] == "gitlab:true:script-1"
    assert result["suites"][0]["state"] == "fail"
    unit = execution.execute_unit(
        {"repository": {
            "path": str(repo), "head_sha": commit, "access": "full_tree",
        }, "diff": ""},
        worker=lambda _request: {
            "schema": "final-review-worker-result/v1",
            "status": "success", "summary": "lens complete",
            "findings": [], "warnings": [], "unreviewed": [],
        },
        required_runner=verification.run_required_verification,
    ).as_dict()
    assert unit["required_verification"]["status"] == "block"
    assert unit["complete"] is True
    assert unit["finding_verdict"] == "block"
