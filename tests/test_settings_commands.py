"""/review-gate:settings and the doctor's settings checks, run as a user runs
them: the CLI behind the command (scripts/rg_settings_cli.py) and the doctor
script (scripts/ocr_doctor.py) as subprocesses with OCR_SETTINGS_FILE and a
temporary project. Only printed output, exit codes and the files on disk are
asserted.
"""
import json
import os
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_CLI = os.path.join(_ROOT, "scripts", "rg_settings_cli.py")
_DOCTOR = os.path.join(_ROOT, "scripts", "ocr_doctor.py")


def _clean_env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OCR_", "GIT_TRACE", "KIMCHI_", "CLAUDE_CODE_", "PI_"))
           and k not in ("CLAUDECODE",)}
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env.update(HOME=str(home), USERPROFILE=str(home),
               OCR_SETTINGS_FILE=str(tmp_path / "user-settings.json"),
               OCR_STATE_DIR=str(tmp_path / "store"),
               OCR_EXT_DIR=str(tmp_path / "ext"))
    env.update(extra)
    return env


def _run(args, env, cwd, script=_CLI):
    return subprocess.run([sys.executable, script, *args], capture_output=True,
                          text=True, env=env, cwd=str(cwd), timeout=90)


@pytest.fixture
def project(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    subprocess.run(["git", "init", "-q", str(d)], check=True, capture_output=True,
                   env={k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE")})
    return d


@pytest.fixture
def env(tmp_path):
    return _clean_env(tmp_path)


def _user_file(env):
    return env["OCR_SETTINGS_FILE"]


def _write_user(env, doc):
    with open(_user_file(env), "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def _rows(stdout):
    """{key: (value, source)} from the `show` table."""
    rows = {}
    for line in stdout.splitlines():
        if " = " not in line or not line.rstrip().endswith("]"):
            continue
        key, rest = line.split(" = ", 1)
        value, source = rest.rsplit("[", 1)
        rows[key.strip()] = (value.strip(), source.rstrip("]").strip())
    return rows


# --- show ---------------------------------------------------------------------

def test_show_lists_every_agent_backend_and_behavior_knob_with_defaults(project, env):
    out = _run([], env, project)
    assert out.returncode == 0, out.stderr
    rows = _rows(out.stdout)
    for agent in ("spotter", "code-reviewer", "deep-reviewer", "docs-checker",
                  "code-filter", "code-resolver", "orchestrator", "canary"):
        for backend in ("claude", "kimchi"):
            assert "agents.%s.%s.model" % (agent, backend) in rows
            assert "agents.%s.%s.thinking" % (agent, backend) in rows
    assert rows["agents.spotter.kimchi.model"] == ("deepseek-v4.1-flash-1017", "default")
    assert rows["agents.code-reviewer.claude.model"] == ("sonnet", "default")
    assert rows["agents.code-reviewer.kimchi.thinking"] == ("high", "default")
    assert rows["agents.canary.claude.thinking"][0] == "(not passed)"
    assert rows["behavior.spotter_skip.max_files"] == ("3", "default")
    assert rows["behavior.canary_cache.enabled"] == ("true", "default")
    assert all(src == "default" for _, src in rows.values())


def test_show_reports_the_source_of_each_layer(project, env):
    _write_user(env, {"agents": {"spotter": {"kimchi": {"model": "user-spot"}},
                                 "code-reviewer": {"kimchi": {"model": "user-rev"}}}})
    proj_file = project / ".ocr" / "settings.json"
    proj_file.parent.mkdir()
    proj_file.write_text(json.dumps(
        {"agents": {"code-reviewer": {"kimchi": {"model": "proj-rev"}}}}), encoding="utf-8")
    env["OCR_SPOTTER_MODEL"] = "env-spot"
    out = _run([], env, project)
    assert out.returncode == 0, out.stderr
    rows = _rows(out.stdout)
    assert rows["agents.spotter.kimchi.model"] == ("env-spot", "env:OCR_SPOTTER_MODEL")
    value, source = rows["agents.code-reviewer.kimchi.model"]
    assert value == "proj-rev" and source.startswith("project:")
    assert source.endswith("settings.json")
    # An untouched entry still reports its default beside the file-sourced ones.
    assert rows["agents.deep-reviewer.kimchi.model"][1] == "default"


def test_show_takes_the_user_value_when_no_project_file_exists(project, env):
    _write_user(env, {"agents": {"spotter": {"kimchi": {"model": "user-spot"}}}})
    rows = _rows(_run([], env, project).stdout)
    value, source = rows["agents.spotter.kimchi.model"]
    assert value == "user-spot" and source.startswith("user:")


def test_show_with_an_invalid_file_is_a_clear_error_not_defaults(project, env):
    _write_user(env, {"agents": {"spotter": {"kimchi": {"modle": "x"}}}})
    out = _run([], env, project)
    assert out.returncode != 0
    assert "modle" in out.stderr and "user-settings.json" in out.stderr
    assert "agents.spotter.kimchi.model" not in out.stdout


# --- set ----------------------------------------------------------------------

def test_set_writes_the_user_file_and_show_reflects_it(project, env):
    out = _run(["set", "agents.spotter.kimchi.model", "my-spotter"], env, project)
    assert out.returncode == 0, out.stderr
    doc = json.load(open(_user_file(env), encoding="utf-8"))
    assert doc == {"agents": {"spotter": {"kimchi": {"model": "my-spotter"}}}}
    rows = _rows(_run([], env, project).stdout)
    value, source = rows["agents.spotter.kimchi.model"]
    assert value == "my-spotter" and source.startswith("user:")


def test_set_project_scope_writes_the_project_file_and_beats_the_user_file(project, env):
    assert _run(["set", "agents.spotter.kimchi.model", "from-user", "--user"], env,
                project).returncode == 0
    out = _run(["set", "agents.spotter.kimchi.model", "from-project", "--project"], env, project)
    assert out.returncode == 0, out.stderr
    doc = json.load(open(project / ".ocr" / "settings.json", encoding="utf-8"))
    assert doc["agents"]["spotter"]["kimchi"]["model"] == "from-project"
    value, source = _rows(_run([], env, project).stdout)["agents.spotter.kimchi.model"]
    assert value == "from-project" and source.startswith("project:")


def test_set_notes_when_an_environment_override_still_wins(project, env):
    env["OCR_SPOTTER_MODEL"] = "env-spot"
    out = _run(["set", "agents.spotter.kimchi.model", "my-spotter"], env, project)
    assert out.returncode == 0, out.stderr
    assert "env:OCR_SPOTTER_MODEL" in out.stdout and "env-spot" in out.stdout


def test_set_parses_typed_behavior_values(project, env):
    assert _run(["set", "behavior.spotter_skip.max_files", "5"], env, project).returncode == 0
    assert _run(["set", "behavior.canary_cache.enabled", "false"], env, project).returncode == 0
    rows = _rows(_run([], env, project).stdout)
    assert rows["behavior.spotter_skip.max_files"][0] == "5"
    assert rows["behavior.canary_cache.enabled"][0] == "false"


@pytest.mark.parametrize("key,value,needle", [
    ("agents.spotter.kimchi.model", "bad model!", "model id"),
    ("agents.spotter.kimchi.thinking", "ludicrous", "one of"),
    ("agents.nobody.kimchi.model", "x", "unknown settings key"),
    ("agents.spotter.gemini.model", "x", "unknown settings key"),
    ("behavior.spotter_skip.max_files", "many", "integer"),
    ("behavior.spotter_skip.max_files", "-3", "integer"),
    ("behavior.canary_cache.enabled", "maybe", "true or false"),
    ("agents.code-reviewer.kimchi.model", "gpt-5", "ADR 0005"),
    ("agents.code-reviewer.kimchi.model", "claude-sonnet-4-5", "ADR 0005"),
])
def test_set_rejects_invalid_input_and_writes_nothing(project, env, key, value, needle):
    out = _run(["set", key, value], env, project)
    assert out.returncode != 0
    assert needle in out.stderr
    assert not os.path.exists(_user_file(env))
    assert not (project / ".ocr").exists()


def test_set_accepts_an_anthropic_model_on_the_claude_backend(project, env):
    out = _run(["set", "agents.code-reviewer.claude.model", "claude-sonnet-4-5"], env, project)
    assert out.returncode == 0, out.stderr


def test_set_never_overwrites_an_invalid_file(project, env):
    bad = '{"agents": {"spotter": {"kimchi": {"model": 5}}}}'
    with open(_user_file(env), "w", encoding="utf-8") as fh:
        fh.write(bad)
    out = _run(["set", "agents.spotter.kimchi.model", "fine-model"], env, project)
    assert out.returncode != 0 and "invalid" in out.stderr
    assert open(_user_file(env), encoding="utf-8").read() == bad


def test_set_project_outside_a_repository_is_an_error(tmp_path, env):
    plain = tmp_path / "plain"
    plain.mkdir()
    env["GIT_CEILING_DIRECTORIES"] = str(tmp_path)
    out = _run(["set", "agents.spotter.kimchi.model", "x", "--project"], env, plain)
    assert out.returncode != 0 and "repository" in out.stderr


def test_set_warns_on_a_deprecated_id_with_its_retirement_date(project, env):
    out = _run(["set", "agents.code-reviewer.kimchi.model", "kimi-k3"], env, project)
    assert out.returncode == 0, out.stderr
    assert "deprecated" in out.stdout and "2026-12-17" in out.stdout
    assert "kimi-k3.1-pro" in out.stdout


# --- validate -----------------------------------------------------------------

def test_validate_passes_with_no_files_and_with_a_good_file(project, env):
    out = _run(["validate"], env, project)
    assert out.returncode == 0 and "absent" in out.stdout
    _write_user(env, {"agents": {"spotter": {"kimchi": {"model": "ok-model"}}}})
    out = _run(["validate"], env, project)
    assert out.returncode == 0 and "valid" in out.stdout


def test_validate_names_every_problem_in_a_bad_project_file(project, env):
    f = project / ".ocr" / "settings.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"agents": {"spotter": {"kimchi": {"thinking": "ludicrous"}}},
                             "extra": 1}), encoding="utf-8")
    out = _run(["validate"], env, project)
    assert out.returncode != 0
    assert "ludicrous" in out.stderr and "extra" in out.stderr


def test_validate_reports_unparseable_json(project, env):
    with open(_user_file(env), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    out = _run(["validate"], env, project)
    assert out.returncode != 0 and "not valid JSON" in out.stderr


# --- doctor -------------------------------------------------------------------

def _fake_kimchi(tmp_path, models=None, list_rc=0):
    """A `kimchi` that answers --version and --list-models (from `models`)."""
    shim = tmp_path / "kimchishim"
    shim.mkdir(exist_ok=True)
    script = shim / "fake_kimchi.py"
    script.write_text(
        "import sys\n"
        "a = sys.argv[1:]\n"
        "if a == ['--version']:\n"
        "    print('kimchi 1.34.3')\n"
        "elif a == ['--list-models']:\n"
        "    sys.stdout.write(%r)\n"
        "    sys.exit(%d)\n"
        "else:\n"
        "    sys.exit(9)\n" % ("\n".join(models or []) + "\n", list_rc), encoding="utf-8")
    if sys.platform == "win32":
        path = shim / "kimchi.cmd"
        path.write_text('@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, script), encoding="ascii")
    else:
        path = shim / "kimchi"
        path.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, script))
        path.chmod(0o755)
    return str(path)


def _doctor(env, project, **extra):
    env = dict(env, **extra)
    out = _run(["--json", "--repo", str(project)], env, project, script=_DOCTOR)
    assert out.stdout.strip(), out.stderr
    checks = {}
    for c in json.loads(out.stdout)["checks"]:
        checks.setdefault(c["name"], []).append(c)
    return checks, out


def _kimchi_env(tmp_path, env, **kw):
    return dict(env, OCR_BACKEND="kimchi", KIMCHI_BIN=_fake_kimchi(tmp_path, **kw))


def test_doctor_with_no_settings_file_reports_defaults_and_nothing_to_warn(tmp_path, project, env):
    checks, _ = _doctor(_kimchi_env(tmp_path, env), project)
    assert checks["settings-files"][0]["status"] == "ok"
    assert "no settings file" in checks["settings-files"][0]["detail"]
    assert checks["settings-guard"][0]["status"] == "ok"
    assert checks["settings-deprecated"][0]["status"] == "ok"
    assert "settings-models" not in checks


def test_doctor_fails_on_an_invalid_settings_file(tmp_path, project, env):
    _write_user(env, {"agents": {"spotter": {"kimchi": {"model": ""}}}})
    checks, out = _doctor(_kimchi_env(tmp_path, env), project)
    (c,) = checks["settings-files"]
    assert c["status"] == "fail" and "user-settings.json" in c["detail"]
    assert "agents.spotter.kimchi.model" in c["detail"]
    assert out.returncode == 1


@pytest.mark.parametrize("bad", ["gpt-5", "claude-sonnet-4-5", "anthropic/claude-opus-4"])
def test_doctor_fails_an_anthropic_or_openai_model_on_kimchi(tmp_path, project, env, bad):
    _write_user(env, {"agents": {"code-reviewer": {"kimchi": {"model": bad}}}})
    checks, out = _doctor(_kimchi_env(tmp_path, env), project)
    (c,) = checks["settings-guard"]
    assert c["status"] == "fail" and bad in c["detail"] and "ADR 0005" in c["detail"]
    assert out.returncode == 1


def test_doctor_guard_also_catches_a_project_file_pin_for_another_agent(tmp_path, project, env):
    f = project / ".ocr" / "settings.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"agents": {"docs-checker": {"kimchi": {"model": "gpt-5"}}}}),
                 encoding="utf-8")
    checks, _ = _doctor(_kimchi_env(tmp_path, env), project)
    assert checks["settings-guard"][0]["status"] == "fail"
    assert "docs-checker" in checks["settings-guard"][0]["detail"]


def test_doctor_does_not_fail_an_anthropic_model_on_the_claude_backend(tmp_path, project, env):
    _write_user(env, {"agents": {"code-reviewer": {"claude": {"model": "claude-sonnet-4-5"}}}})
    env = dict(env, OCR_BACKEND="claude")
    checks, _ = _doctor(env, project)
    assert checks["settings-guard"][0]["status"] == "ok"


def test_doctor_warns_on_deprecated_ids_with_retirement_dates(tmp_path, project, env):
    _write_user(env, {"agents": {"code-reviewer": {"kimchi": {"model": "kimi-k3"}},
                                 "docs-checker": {"kimchi": {"model": "deepseek-v4.1-flash"}}}})
    checks, out = _doctor(_kimchi_env(tmp_path, env), project)
    (c,) = checks["settings-deprecated"]
    assert c["status"] == "warn"
    assert "2026-12-17" in c["detail"] and "kimi-k3.1-pro" in c["detail"]
    assert "2026-12-02" in c["detail"] and "deepseek-v4.1-flash-1017" in c["detail"]


def test_doctor_does_not_flag_the_replacement_ids(tmp_path, project, env):
    _write_user(env, {"agents": {"code-reviewer": {"kimchi": {"model": "kimi-k3.1-pro"}},
                                 "docs-checker": {"kimchi": {"model": "kimchi-dev/deepseek-v4.1-flash-1017"}}}})
    checks, _ = _doctor(_kimchi_env(tmp_path, env), project)
    assert checks["settings-deprecated"][0]["status"] == "ok"


def test_doctor_warns_on_a_deprecated_env_override(tmp_path, project, env):
    checks, _ = _doctor(_kimchi_env(tmp_path, env, ), project, OCR_TIER1_MODEL="kimi-k3")
    (c,) = checks["settings-deprecated"]
    assert c["status"] == "warn" and "OCR_TIER1_MODEL" in c["detail"]


def test_doctor_flags_a_pinned_id_missing_from_the_kimchi_model_list(tmp_path, project, env):
    _write_user(env, {"agents": {"spotter": {"kimchi": {"model": "kimchi-dev/not-a-model"}},
                                 "code-reviewer": {"kimchi": {"model": "kimi-k3.1-pro"}}}})
    kenv = _kimchi_env(tmp_path, env, models=["kimi-k3.1-pro", "glm-5.3"])
    checks, _ = _doctor(kenv, project)
    (c,) = checks["settings-models"]
    assert c["status"] == "warn"
    assert "not-a-model" in c["detail"] and "kimi-k3.1-pro" not in c["detail"]


def test_doctor_accepts_pins_present_in_the_model_list(tmp_path, project, env):
    _write_user(env, {"agents": {"spotter": {"kimchi": {"model": "kimchi-dev/glm-5.3"}}}})
    kenv = _kimchi_env(tmp_path, env, models=["glm-5.3", "kimi-k3.1-pro"])
    checks, _ = _doctor(kenv, project)
    assert checks["settings-models"][0]["status"] == "ok"


def test_doctor_is_unverified_when_the_model_list_is_unavailable(tmp_path, project, env):
    _write_user(env, {"agents": {"spotter": {"kimchi": {"model": "whatever-model"}}}})
    kenv = _kimchi_env(tmp_path, env, models=[], list_rc=2)
    checks, _ = _doctor(kenv, project)
    (c,) = checks["settings-models"]
    assert c["status"] == "info" and c["detail"].startswith("unverified")


def test_doctor_skips_the_model_list_cleanly_when_kimchi_is_not_installed(tmp_path, project, env):
    _write_user(env, {"agents": {"spotter": {"kimchi": {"model": "whatever-model"}}}})
    # claude backend selected, and no kimchi anywhere on PATH.
    shim = tmp_path / "clishim"
    shim.mkdir()
    keep = [d for d in env["PATH"].split(os.pathsep)
            if d and not any(os.path.isfile(os.path.join(d, "kimchi" + ext))
                             for ext in ("", ".exe", ".cmd", ".bat"))]
    cenv = dict(env, OCR_BACKEND="claude", PATH=os.pathsep.join(keep))
    checks, _ = _doctor(cenv, project)
    (c,) = checks["settings-models"]
    assert c["status"] == "info"
    assert "unverified: kimchi not installed" in c["detail"]


def test_doctor_settings_checks_do_not_write_any_file(tmp_path, project, env):
    _write_user(env, {"agents": {"spotter": {"kimchi": {"model": "kimi-k3"}}}})
    before = open(_user_file(env), encoding="utf-8").read()
    _doctor(_kimchi_env(tmp_path, env, models=["kimi-k3"]), project)
    assert open(_user_file(env), encoding="utf-8").read() == before
    assert not (project / ".ocr").exists()
