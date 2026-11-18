"""Public environment-policy tests for final workers and dynamic sandboxes."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from rg_final_env import scrub_environment  # noqa: E402


def test_model_auth_policy_preserves_inference_key_but_removes_provider_write_token():
    env = scrub_environment({
        "ANTHROPIC_API_KEY": "model-key",
        "GITHUB_TOKEN": "provider-write-token",
        "PATH": "/usr/bin",
    }, preserve_model_auth=True)

    assert env == {
        "ANTHROPIC_API_KEY": "model-key",
        "PATH": "/usr/bin",
        "RG_FINAL_CREDENTIAL_FREE": "1",
    }


def test_model_auth_allowance_is_exact_not_a_provider_family_prefix():
    source = {
        "ANTHROPIC_API_KEY": "anthropic-key",
        "ANTHROPIC_AUTH_TOKEN": "anthropic-token",
        "CLAUDE_CODE_OAUTH_TOKEN": "claude-oauth",
        "KIMI_API_KEY": "kimi-key",
        "OPENAI_API_KEY": "openai-key",
        "ANTHROPIC_WORKSPACE_TOKEN": "unrelated-token",
        "CLAUDE_CODE_MESSAGING_TOKEN": "messaging-token",
        "KIMI_SESSION_TOKEN": "session-token",
        "OPENAI_PUBLISH_TOKEN": "publish-token",
        "PI_MESSAGING_TOKEN": "messaging-token",
        "CI_JOB_TOKEN": "ci-job-token",
        "GITLAB_API_KEY": "provider-write-key",
        "GITHUB_API_KEY": "provider-write-key",
    }

    env = scrub_environment(source, preserve_model_auth=True)

    assert set(env) == {
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
        "KIMI_API_KEY", "OPENAI_API_KEY", "RG_FINAL_CREDENTIAL_FREE",
    }


def test_sandbox_removes_generic_secrets_urls_git_hooks_and_model_auth():
    source = {
        "PATH": "/usr/bin",
        "CI_PROJECT_NAME": "github-looking-public-name",
        "CI_COMMIT_AUTHOR": "Public Author <author@example.test>",
        "CI_REPOSITORY_URL": "https://oauth2:write-token@gitlab.example/group/repo.git",
        "DATABASE_URL": "postgres://review:password@db.example/review",
        "PUBLIC_PROJECT_URL": "https://gitlab.example/group/public-repo",
        "CUSTOM_TOKEN": "generic-token",
        "DATABASE_PASSWORD": "generic-password",
        "TLS_PRIVATE_KEY": "private-key",
        "SESSION_COOKIE": "cookie",
        "OPENAI_API_KEY": "selector-only",
        "GIT_TRACE": "/tmp/trace",
        "GIT_TRACE2_EVENT": "/tmp/trace-event",
        "GIT_DIR": "/tmp/source/.git",
        "GIT_WORK_TREE": "/tmp/source",
        "GIT_INDEX_FILE": "/tmp/index",
        "GIT_PREFIX": "subdir",
        "GIT_COMMON_DIR": "/tmp/common.git",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "url.https://token@github.com/.insteadOf",
        "GIT_CONFIG_VALUE_0": "https://github.com/",
        "GIT_CONFIG_PARAMETERS": "'credential.helper=!exfiltrate'",
        "OCR_TEST_SELECTOR_CMD": "selector-only-command",
        "KIMCHI_BIN": "selector-only-binary",
    }

    env = scrub_environment(source)

    assert env == {
        "PATH": "/usr/bin",
        "CI_PROJECT_NAME": "github-looking-public-name",
        "CI_COMMIT_AUTHOR": "Public Author <author@example.test>",
        "PUBLIC_PROJECT_URL": "https://gitlab.example/group/public-repo",
        "RG_FINAL_CREDENTIAL_FREE": "1",
    }


def test_empty_and_malformed_environment_entries_fail_closed():
    assert scrub_environment({}) == {"RG_FINAL_CREDENTIAL_FREE": "1"}
    env = scrub_environment({
        "SAFE": "value",
        "NON_STRING_VALUE": 42,
        "NUL_VALUE": "bad\0value",
        "NUL\0KEY": "bad",
        "BAD=KEY": "bad",
        42: "bad-key",
    }, preserve_model_auth=True)
    assert env == {"SAFE": "value", "RG_FINAL_CREDENTIAL_FREE": "1"}


def _standalone_install(tmp_path, *, include_helper=True):
    install = tmp_path / "installed"
    work = tmp_path / "review-clone"
    install.mkdir()
    work.mkdir()
    (work / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    shutil.copyfile(ROOT / "examples" / "test-check" / "test_check", install / "test_check")
    (install / "test_check").chmod(0o755)
    if include_helper:
        shutil.copyfile(ROOT / "scripts" / "rg_final_env.py", install / "rg_final_env.py")
    capture = tmp_path / "subject-environment.json"
    selection = {
        "existing_tests": [],
        "probes": [{
            "name": "capture-environment",
            "language": "python",
            "source": (
                "import json, os\n"
                f"with open({str(capture)!r}, 'w', encoding='utf-8') as fh:\n"
                "    json.dump(sorted(k for k in os.environ if k in {"
                "'GENERIC_API_KEY', 'CI_REPOSITORY_URL', 'GIT_TRACE2_EVENT', "
                "'OPENAI_API_KEY', 'GITHUB_TOKEN'}), fh)\n"
            ),
            "run_cmd": f'"{sys.executable}" {{probe_file}}',
            "path": "app.py",
        }],
    }
    selector = tmp_path / "selector.py"
    selector.write_text(
        "import json, sys\n"
        "json.load(sys.stdin)\n"
        f"sys.stdout.write({json.dumps(selection)!r})\n",
        encoding="utf-8",
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "OCR_TEST_SELECTOR_CMD": f'"{sys.executable}" "{selector}"',
        "GENERIC_API_KEY": "must-not-cross",
        "CI_REPOSITORY_URL": "https://oauth2:token@gitlab.example/group/repo.git",
        "GIT_TRACE2_EVENT": "/tmp/trace",
        "OPENAI_API_KEY": "selector-only",
        "GITHUB_TOKEN": "provider-write-token",
    }
    return install / "test_check", work, capture, env


def test_standalone_executor_and_sibling_policy_install_in_clean_directories(tmp_path):
    executor, work, capture, env = _standalone_install(tmp_path)

    process = subprocess.run(
        [str(executor)], cwd=str(work), env=env, input='["app.py"]', text=True,
        capture_output=True, timeout=30, check=False,
    )

    assert process.returncode == 0, process.stderr
    assert json.loads(capture.read_text(encoding="utf-8")) == []
    response = json.loads(process.stdout)
    assert response["findings"] == []
    assert "ephemeral probe was the only evidence" in response["warnings"][0]["message"]


def test_standalone_executor_without_policy_helper_fails_closed(tmp_path):
    executor, work, capture, env = _standalone_install(tmp_path, include_helper=False)

    process = subprocess.run(
        [str(executor)], cwd=str(work), env=env, input='["app.py"]', text=True,
        capture_output=True, timeout=30, check=False,
    )

    assert process.returncode != 0
    assert "rg_final_env.py" in process.stderr
    assert not capture.exists(), "no selector, repository test, or probe may run"


def test_none_uses_process_environment_without_mutating_it(monkeypatch):
    monkeypatch.setenv("CUSTOM_SECRET", "must-be-removed")
    monkeypatch.setenv("OPENAI_API_KEY", "model-key")
    original = dict(os.environ)
    env = scrub_environment(preserve_model_auth=True)
    assert env["OPENAI_API_KEY"] == "model-key"
    assert "CUSTOM_SECRET" not in env
    assert os.environ == original
