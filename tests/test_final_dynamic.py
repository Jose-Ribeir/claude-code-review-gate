"""Vertical SPEC-16 dynamic-check tests through the public final-review seam.

Every selection is scripted.  The reference SPEC-15 executor and temporary real
Git repositories provide the execution path; no test calls a model/provider or
uses a source/review/required-test checkout as a probe scratch directory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rg_final_dynamic as dynamic  # noqa: E402
from rg_final_store import FinalStore  # noqa: E402


DSL_OK = "def verdict():\n    return 'ok'\n"
DSL_BROKEN = "def verdict():\n    return None\n"
DSL_TEST = (
    "import os, sys\n"
    "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
    "from dsl import verdict\n"
    "assert verdict() == 'ok', 'regression: verdict broken'\n"
)


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        check=True, timeout=30,
    ).stdout.strip()


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _repository(tmp_path, *, broken=True):
    """Return a real repo and disjoint pinned target/feature revisions."""
    repo = tmp_path / "source"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    for key, value in (
        ("user.name", "Final Dynamic Test"),
        ("user.email", "dynamic@example.test"),
        ("commit.gpgsign", "false"),
    ):
        _git(repo, "config", key, value)
    (repo / "dsl.py").write_text(DSL_OK, encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_dsl.py").write_text(DSL_TEST, encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "target.txt").write_text("target only\n", encoding="utf-8")
    target = _commit(repo, "target")
    _git(repo, "checkout", "-q", "-b", "feature", base)
    (repo / "dsl.py").write_text(DSL_BROKEN if broken else DSL_OK, encoding="utf-8")
    if not broken:
        (repo / "feature.txt").write_text("feature change without a regression\n",
                                          encoding="utf-8")
    head = _commit(repo, "feature")
    _git(repo, "checkout", "-q", "main")
    return repo, target, head


def _selector(tmp_path, selection):
    """A one-shot selector double that records its cwd and credential view."""
    directory = tmp_path / "selector"
    directory.mkdir(exist_ok=True)
    script = directory / "selector.py"
    script.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "here = Path(__file__).resolve().parent\n"
        "manifest = json.load(sys.stdin)\n"
        "assert isinstance(manifest, list)\n"
        "with (here / 'runs.jsonl').open('a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps({'cwd': os.getcwd(), 'manifest': manifest, "
        "'publication_secret': os.environ.get('PUBLICATION_SECRET'), "
        "'gitlab_token': os.environ.get('GITLAB_TOKEN'), "
        "'model_credential': os.environ.get('OPENAI_API_KEY')}) + '\\n')\n"
        "selection = (here / 'selection.json').read_text(encoding='utf-8')\n"
        "sys.stdout.write(selection)\n",
        encoding="utf-8",
    )
    (directory / "selection.json").write_text(
        selection if isinstance(selection, str) else json.dumps(selection),
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


def _selector_runs(tmp_path):
    path = tmp_path / "selector" / "runs.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _environment(tmp_path, selection, **extra):
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("OCR_", "GITLAB_", "GITHUB_", "GLAB_", "GH_"))
        and key not in {"PUBLICATION_SECRET", "FLAKE_STATE"}
    }
    env.update({
        "OCR_STATE_DIR": str(tmp_path / "external-store"),
        "OCR_TEST_SELECTOR_CMD": _selector(tmp_path, selection),
        "PUBLICATION_SECRET": "must-not-reach-tests",
        "GITLAB_TOKEN": "must-not-reach-selector-or-tests",
        "OPENAI_API_KEY": "selector-only-model-credential",
    })
    env.update({key: str(value) for key, value in extra.items()})
    return env


def _run(repo, target, head, env, **kwargs):
    return dynamic.run_dynamic_checks(
        repo, target, head, environ=env, timeout=kwargs.pop("timeout", 30), **kwargs
    )


def _snapshot(repo):
    return {
        str(path.relative_to(repo)): path.read_bytes()
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    }


def _warning(result, code):
    return [item for item in result["warnings"] if item["code"] == code]


def test_public_seam_reproduces_existing_failure_in_its_own_clean_clone(
        tmp_path, monkeypatch):
    """A final unit explicitly invoking dynamic checks gets SPEC-15 evidence.

    The selected test runs twice, its verbatim failure becomes existing_test
    evidence, and that compact result can be persisted beside required tests in
    an existing FinalStore without changing this stage's pre-push behavior.
    """
    monkeypatch.setenv("OCR_STATE_DIR", str(tmp_path / "external-store"))
    repo, target, head = _repository(tmp_path)
    review_root = tmp_path / "review-root"
    required_root = tmp_path / "test-required-root"
    _git(tmp_path, "clone", "-q", str(repo), str(review_root))
    _git(tmp_path, "clone", "-q", str(repo), str(required_root))
    before = _snapshot(repo)
    env = _environment(tmp_path, {
        "existing_tests": ["tests/test_dsl.py"],
        "probes": [{
            "name": "must-not-run",
            "language": "python",
            "source": "raise AssertionError('probe should be discarded')\n",
            "run_cmd": f'"{sys.executable}" {{probe_file}}',
        }],
    })

    result = _run(repo, target, head, env, unit_key="individual:1")

    assert result["schema"] == dynamic.RESULT_SCHEMA
    assert result["unit_key"] == "individual:1"
    assert result["state"] == "findings"
    assert result["complete"] is True
    (finding,) = result["findings"]
    assert finding["evidence_source"] == "existing_test"
    assert finding["lens"] == "dynamic"
    assert finding["path"] == "tests/test_dsl.py"
    assert "regression: verdict broken" in finding["content"]
    assert finding["content"].startswith("reproduced failure of existing test")
    assert result["warnings"] == []
    assert result["telemetry"]["selector_attempts"] == 1
    assert result["telemetry"]["tests_selected"] == 1
    assert result["telemetry"]["probes_authored"] == 0
    assert result["telemetry"]["runs"] == 2
    assert result["telemetry"]["reproduce_results"]["fail"] == 1
    assert result["telemetry"]["executor_attempts"] == 1
    assert result["coverage"]["status"] == "existing_tests"
    assert result["revisions"] == {
        "target_sha": target,
        "head_sha": head,
        "merge_base_sha": _git(repo, "merge-base", target, head),
    }
    (selector_run,) = _selector_runs(tmp_path)
    scratch = Path(selector_run["cwd"])
    assert scratch not in {repo, review_root, required_root}
    assert not scratch.exists(), "the dedicated dynamic clone was cleaned"
    assert [item["path"] for item in selector_run["manifest"]] == ["dsl.py"]
    assert selector_run["gitlab_token"] is None
    assert selector_run["publication_secret"] is None
    assert _snapshot(repo) == before

    with FinalStore(tmp_path / "final.sqlite3") as store:
        identity = store.start_run({
            "provider": "gitlab",
            "target_project": "group/project",
            "root_target_ref": "main",
            "root_target_sha": target,
            "items": [{
                "item_key": "7",
                "source_project": "group/project",
                "source_ref": "feature",
                "source_sha": head,
                "target_project": "group/project",
                "target_ref": "main",
                "target_sha": target,
            }],
        })
        unit_id = identity["unit_ids"]["individual:1"]
        ids = dynamic.record_dynamic_result(store, unit_id, result)
        assert len(ids["finding_ids"]) == 1
        assert len(ids["test_ids"]) == 1
        unit = store.manifest(identity["run_id"], unit_id)["units"][0]
        assert unit["findings"][0]["lens"] == "dynamic"
        assert unit["findings"][0]["content"] == finding["content"]
        assert unit["findings"][0]["evidence"]["finding"]["evidence_source"] == \
            "existing_test"
        assert unit["tests"][0]["kind"] == "existing_test"
        assert unit["tests"][0]["output"] == finding["content"]
        assert unit["tests"][0]["evidence_fingerprint"]
        assert [item["kind"] for item in unit["coverage"]] == ["dynamic_tests"]
        assert unit["coverage"][0]["details"]["telemetry"]["runs"] == 2


def test_ephemeral_failure_is_clean_credential_free_and_visibly_uncovered(
        tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_STATE_DIR", str(tmp_path / "external-store"))
    repo, target, head = _repository(tmp_path)
    source_sentinel = "EPHEMERAL_SOURCE_NEVER_PERSIST_OR_REPORT"
    source = (
        "import os, sys\n"
        f"# {source_sentinel}\n"
        "leaked = [key for key in ('PUBLICATION_SECRET', 'GITLAB_TOKEN', "
        "'OPENAI_API_KEY') if os.environ.get(key)]\n"
        "if leaked:\n"
        "    print('credential leak: ' + ','.join(leaked))\n"
        "    sys.exit(9)\n"
        "print('simulated-race-detected: unsynchronized counter')\n"
        "sys.exit(3)\n"
    )
    env = _environment(tmp_path, {
        "existing_tests": [],
        "probes": [{
            "name": "race",
            "language": "python",
            "source": source,
            "run_cmd": f'"{sys.executable}" {{probe_file}}',
            "path": "dsl.py",
        }],
    })

    result = _run(repo, target, head, env)

    assert result["state"] == "findings"
    (finding,) = result["findings"]
    assert finding["evidence_source"] == "ephemeral_test"
    assert "simulated-race-detected: unsynchronized counter" in finding["content"]
    assert "credential leak" not in finding["content"]
    assert source_sentinel not in json.dumps(result)
    assert _warning(result, "dynamic-coverage")
    assert "ephemeral probe was the only evidence" in \
        _warning(result, "dynamic-coverage")[0]["message"]
    assert result["coverage"]["status"] == "ephemeral_only"
    assert result["telemetry"]["probes_authored"] == 1
    assert result["telemetry"]["runs"] == 2
    assert not (repo / dynamic.PROBE_ROOT).exists()
    scratch = Path(_selector_runs(tmp_path)[0]["cwd"])
    assert not scratch.exists()
    assert source_sentinel not in "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in repo.rglob("*") if path.is_file() and ".git" not in path.parts
    )


def test_fail_once_pass_once_is_a_visible_flake_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_STATE_DIR", str(tmp_path / "external-store"))
    repo, target, head = _repository(tmp_path, broken=False)
    flaky = (
        "import os, sys\n"
        "state = os.environ['FLAKE_STATE']\n"
        "if not os.path.exists(state):\n"
        "    open(state, 'w').close()\n"
        "    print('first run fails')\n"
        "    sys.exit(1)\n"
        "def test_stable_on_re_run():\n"
        "    assert True\n"
    )
    # The flaky file must be in the pinned feature tree, not just the caller.
    _git(repo, "checkout", "-q", "feature")
    (repo / "tests" / "test_flake.py").write_text(flaky, encoding="utf-8")
    head = _commit(repo, "add flaky regression test")
    _git(repo, "checkout", "-q", "main")
    env = _environment(
        tmp_path, {"existing_tests": ["tests/test_flake.py"], "probes": []},
        FLAKE_STATE=tmp_path / "flake-state",
    )

    result = _run(repo, target, head, env)

    assert result["findings"] == []
    assert result["state"] == "degraded"
    (warning,) = _warning(result, "dynamic-flake")
    assert "failed once and passed on the re-run" in warning["message"]
    assert result["telemetry"]["runs"] == 2
    assert result["telemetry"]["reproduce_results"]["flake"] == 1
    assert (tmp_path / "flake-state").exists()


@pytest.mark.parametrize("selection, code", [
    ({"existing_tests": [], "probes": []}, "dynamic-coverage"),
    ("not json and never retried", "dynamic-selector"),
])
def test_coverage_and_selector_gaps_are_warnings_with_one_attempt(
        tmp_path, monkeypatch, selection, code):
    monkeypatch.setenv("OCR_STATE_DIR", str(tmp_path / "external-store"))
    repo, target, head = _repository(tmp_path)
    env = _environment(tmp_path, selection)

    result = _run(repo, target, head, env)

    assert result["findings"] == []
    assert result["state"] == "degraded"
    assert len(_warning(result, code)) == 1
    assert result["telemetry"]["selector_attempts"] == 1
    assert result["telemetry"]["runs"] == 0
    assert len(_selector_runs(tmp_path)) == 1
    if code == "dynamic-coverage":
        assert result["coverage"]["status"] == "uncovered"
        assert "no existing tests, no probes" in _warning(result, code)[0]["message"]
    else:
        assert result["coverage"]["status"] == "unknown"
        assert "selector failed; no tests selected" in _warning(result, code)[0]["message"]


def test_unbuildable_probe_is_a_warning_not_fabricated_verification(
        tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_STATE_DIR", str(tmp_path / "external-store"))
    repo, target, head = _repository(tmp_path)
    env = _environment(tmp_path, {
        "existing_tests": [],
        "probes": [{
            "name": "broken",
            "language": "python",
            "source": "def broken(:\n    pass\n",
            "run_cmd": f'"{sys.executable}" {{probe_file}}',
            "path": "dsl.py",
        }],
    })

    result = _run(repo, target, head, env)

    assert result["findings"] == []
    assert result["state"] == "degraded"
    assert _warning(result, "dynamic-coverage")
    assert any("failed to build or run" in item["message"]
               for item in _warning(result, "dynamic-probe"))
    assert result["telemetry"]["runs"] == 1
    assert result["telemetry"]["reproduce_results"]["build"] == 1


def test_outer_executor_timeout_warns_once_and_cleans_clone(tmp_path, monkeypatch):
    """The wrapper adds no retry around the executor's internal rerun policy."""
    monkeypatch.setenv("OCR_STATE_DIR", str(tmp_path / "external-store"))
    repo, target, head = _repository(tmp_path)
    script = tmp_path / "hung_executor.py"
    calls = tmp_path / "executor-calls.jsonl"
    script.write_text(
        "import json, os, sys, time\n"
        f"with open({str(calls)!r}, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps({'cwd': os.getcwd(), "
        "'secret': os.environ.get('PUBLICATION_SECRET')}) + '\\n')\n"
        "json.load(sys.stdin)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    env = _environment(tmp_path, {"existing_tests": [], "probes": []})

    began = time.monotonic()
    result = _run(repo, target, head, env,
                  executor=[sys.executable, str(script)], timeout=0.15)

    assert time.monotonic() - began < 10
    assert result["state"] == "degraded"
    assert result["complete"] is False
    assert result["findings"] == []
    (warning,) = _warning(result, "dynamic-timeout")
    assert "dynamic executor timed out" in warning["message"]
    assert result["telemetry"]["executor_attempts"] == 1
    assert result["telemetry"]["executor_timeouts"] == 1
    lines = calls.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    invocation = json.loads(lines[0])
    assert invocation["secret"] is None
    assert not Path(invocation["cwd"]).exists()
    assert _selector_runs(tmp_path) == [], "a scripted executor replaces, not wraps, selection"


@pytest.mark.parametrize("stdout, returncode, code", [
    ("not-json", 0, "dynamic-executor-result"),
    (json.dumps({"findings": "bad", "warnings": []}), 0, "dynamic-executor-result"),
    ("executor exploded", 9, "dynamic-executor-exit"),
])
def test_invalid_or_failed_executor_response_degrades_without_retry(
        tmp_path, monkeypatch, stdout, returncode, code):
    monkeypatch.setenv("OCR_STATE_DIR", str(tmp_path / "external-store"))
    repo, target, head = _repository(tmp_path)
    script = tmp_path / "scripted_executor.py"
    count = tmp_path / "executor-count"
    script.write_text(
        "import os, sys\n"
        f"with open({str(count)!r}, 'a', encoding='utf-8') as fh: fh.write('run\\n')\n"
        "sys.stdin.read()\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.exit({returncode})\n",
        encoding="utf-8",
    )
    env = _environment(tmp_path, {"existing_tests": [], "probes": []})

    result = _run(repo, target, head, env,
                  executor=[sys.executable, str(script)])

    assert result["state"] == "degraded"
    assert result["complete"] is False
    assert result["findings"] == []
    assert len(_warning(result, code)) == 1
    assert result["telemetry"]["executor_attempts"] == 1
    assert count.read_text(encoding="utf-8") == "run\n"
    assert _selector_runs(tmp_path) == []


def test_empty_unit_does_not_invoke_selector_or_executor(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_STATE_DIR", str(tmp_path / "external-store"))
    repo, target, _head = _repository(tmp_path)
    env = _environment(tmp_path, {"existing_tests": ["tests/test_dsl.py"], "probes": []})

    result = _run(repo, target, target, env)

    assert result["state"] == "not_applicable"
    assert result["complete"] is True
    assert result["findings"] == []
    assert result["warnings"] == []
    assert result["coverage"]["status"] == "not_applicable"
    assert result["telemetry"]["executor_attempts"] == 0
    assert "selector_attempts" not in result["telemetry"]
    assert _selector_runs(tmp_path) == []


def test_invalid_explicit_paths_fail_soft_before_executor(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_STATE_DIR", str(tmp_path / "external-store"))
    repo, target, head = _repository(tmp_path)
    env = _environment(tmp_path, {"existing_tests": [], "probes": []})

    result = _run(repo, target, head, env, changed_paths=["../outside.py"])

    assert result["state"] == "degraded"
    assert result["complete"] is False
    assert _warning(result, "dynamic-paths")
    assert result["telemetry"]["executor_attempts"] == 0
    assert _selector_runs(tmp_path) == []
