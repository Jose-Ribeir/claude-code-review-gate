"""The settings layer: one source for every review agent's model and thinking.

Two seams. The module API (scripts/rg_settings.py) is checked in-process for
precedence (env > project file > user file > default), validation and the
schema. The gate is checked end to end behind the fake backend CLI
(tests/fake_backend.py): the model a settings file pins must be the model the
launched worker actually receives, in the fake's recorded argv. An invalid file
is a clear refusal and no launch; the ADR 0005 provider guard also covers a
value that came from a settings file.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SCRIPTS = os.path.join(_ROOT, "scripts")
sys.path.insert(0, _HERE)
sys.path.insert(0, _SCRIPTS)
import rg_settings  # noqa: E402
from test_async_gate import _commit, repo  # noqa: E402,F401
from test_claude_profile import _backend_env, _launches, _push, rg  # noqa: E402

NO_ENV = {}      # an empty environment for the module API: no OCR_* override


def _write(path, doc):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _agent(agent, backend, model=None, thinking=None):
    entry = {}
    if model is not None:
        entry["model"] = model
    if thinking is not None:
        entry["thinking"] = thinking
    return {"agents": {agent: {backend: entry}}}


@pytest.fixture(autouse=True)
def _no_leaked_backend_selection(monkeypatch):
    """The gate remembers the backend a worker call last selected in a
    thread-local. In-process tests that run earlier in this thread leave it
    set (to claude), and `_tier_model` / `_agent_model` then resolve for that
    backend whatever OCR_BACKEND says. Start every test here with none."""
    monkeypatch.delattr(rg._backend_local, "selection", raising=False)


@pytest.fixture
def user_file(tmp_path):
    """The user-level settings file the autouse conftest fixture points at."""
    return Path(os.environ["OCR_SETTINGS_FILE"])


def _model_arg(launch):
    argv = launch["argv"]
    return argv[argv.index("--model") + 1]


# --- module API: precedence --------------------------------------------------

def test_default_models_are_todays_pins():
    d = rg_settings.default_model
    assert d("orchestrator", "kimchi") == "kimchi-dev/kimi-k3.1-pro"
    assert d("canary", "kimchi") == "kimchi-dev/kimi-k3.1-pro"
    assert d("spotter", "kimchi") == "deepseek-v4.1-flash-1017"
    assert d("code-reviewer", "kimchi") == "kimchi-dev/kimi-k3.1-pro"
    assert d("deep-reviewer", "kimchi") == "kimchi-dev/kimi-k3.1-pro"
    assert d("docs-checker", "kimchi") == "kimchi-dev/glm-5.3"
    assert d("code-filter", "kimchi") == "kimchi-dev/deepseek-v4.1-flash-1017"
    assert d("code-resolver", "kimchi") == "kimchi-dev/glm-5.3"
    assert d("docs-summary", "kimchi") == "kimchi-dev/glm-5.3"
    assert d("spotter", "claude") == "haiku"
    assert d("code-reviewer", "claude") == "sonnet"
    assert d("deep-reviewer", "claude") == "opus"
    assert d("orchestrator", "claude") == "sonnet"
    assert d("docs-summary", "claude") == "sonnet"


def test_no_file_resolves_to_the_default_with_default_source(tmp_path):
    got = rg_settings.load(tmp_path, NO_ENV).model("code-reviewer", "kimchi")
    assert (got.value, got.source) == ("kimchi-dev/kimi-k3.1-pro", "default")


def test_user_file_beats_the_default(tmp_path, user_file):
    _write(user_file, _agent("spotter", "kimchi", "glm-5.3"))
    got = rg_settings.load(tmp_path, NO_ENV).model("spotter", "kimchi")
    assert got.value == "glm-5.3" and got.source.startswith("user:")


def test_project_file_beats_user_file_and_env_beats_both(tmp_path, user_file):
    _write(user_file, _agent("spotter", "kimchi", "user-model"))
    _write(rg_settings.project_settings_path(tmp_path),
           _agent("spotter", "kimchi", "project-model"))
    got = rg_settings.load(tmp_path, NO_ENV).model("spotter", "kimchi")
    assert got.value == "project-model" and got.source.startswith("project:")
    got = rg_settings.load(tmp_path, {"OCR_SPOTTER_MODEL": "env-model"}).model("spotter", "kimchi")
    assert (got.value, got.source) == ("env-model", "env:OCR_SPOTTER_MODEL")


def test_a_pin_in_one_backend_section_does_not_touch_the_other(tmp_path, user_file):
    _write(user_file, _agent("code-reviewer", "claude", "opus"))
    s = rg_settings.load(tmp_path, NO_ENV)
    assert s.model("code-reviewer", "claude").value == "opus"
    assert s.model("code-reviewer", "kimchi").value == "kimchi-dev/kimi-k3.1-pro"


@pytest.mark.parametrize("env,expected", [
    ({}, "sonnet"),
    ({"OCR_MODEL": "haiku"}, "haiku"),
    ({"OCR_MODEL": "kimchi-dev/kimi-k3"}, "sonnet"),            # a kimchi id is ignored
    ({"OCR_MODEL": "haiku", "OCR_CLAUDE_MODEL": "opus"}, "opus"),
    ({"OCR_CLAUDE_MODEL": "  "}, "sonnet"),
])
def test_claude_orchestrator_env_layer_matches_the_legacy_reader(monkeypatch, tmp_path, env, expected):
    for k in ("OCR_MODEL", "OCR_CLAUDE_MODEL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert rg._read_claude_model() == expected
    assert rg_settings.load(tmp_path, dict(env)).model("orchestrator", "claude").value == expected


def test_claude_tier_ignores_a_kimchi_id_in_the_shared_variable(tmp_path):
    s = rg_settings.load(tmp_path, {"OCR_TIER1_MODEL": "kimi-k3"})
    assert s.model("code-reviewer", "claude").value == "sonnet"
    s = rg_settings.load(tmp_path, {"OCR_TIER1_MODEL": "claude-sonnet-4-5"})
    assert s.model("code-reviewer", "claude").value == "claude-sonnet-4-5"
    s = rg_settings.load(tmp_path, {"OCR_TIER1_MODEL": "kimi-k3", "OCR_CLAUDE_TIER1_MODEL": "opus"})
    assert s.model("code-reviewer", "claude").value == "opus"


# --- module API: validation, writing, schema -----------------------------------

@pytest.mark.parametrize("text", [
    "{not json",
    json.dumps({"agents": {"no-such-agent": {"claude": {"model": "x"}}}}),
    json.dumps({"agents": {"spotter": {"claude": {"thinking": "extreme"}}}}),
    json.dumps({"agents": {"spotter": {"claude": {"modle": "x"}}}}),
    json.dumps({"behavior": {"spotter_skip": {"max_files": -1}}}),
    json.dumps(["not", "an", "object"]),
])
def test_an_invalid_file_is_an_error_never_a_silent_fallback(tmp_path, user_file, text):
    user_file.write_text(text, encoding="utf-8")
    with pytest.raises(rg_settings.SettingsError) as exc:
        rg_settings.load(tmp_path, NO_ENV)
    assert str(user_file) in str(exc.value)


def test_set_value_round_trips_and_rejects_a_bad_value(tmp_path, user_file):
    key = "agents.spotter.kimchi.thinking"
    assert Path(rg_settings.set_value(key, "low")) == user_file
    assert rg_settings.load(tmp_path, NO_ENV).thinking("spotter", "kimchi").value == "low"
    with pytest.raises(rg_settings.SettingsError):
        rg_settings.set_value(key, "extreme")
    # the bad write left the good file alone
    assert rg_settings.load(tmp_path, NO_ENV).thinking("spotter", "kimchi").value == "low"
    p = rg_settings.set_value("agents.code-reviewer.claude.model", "opus", scope="project",
                              repo_root=tmp_path)
    assert Path(p) == rg_settings.project_settings_path(tmp_path)
    assert rg_settings.load(tmp_path, NO_ENV).model("code-reviewer", "claude").source.startswith("project:")


def test_behavior_defaults_are_the_documented_values(tmp_path):
    s = rg_settings.load(tmp_path, NO_ENV)
    assert s.behavior("inline_wait_seconds.hook").value == 300
    assert s.behavior("inline_wait_seconds.git").value == 300
    # Shipped OFF until the cross-file recall scenarios ran on a real kimchi.
    assert s.behavior("spotter_skip.enabled").value is False
    assert s.behavior("spotter_skip.max_files").value == 3
    assert s.behavior("spotter_skip.max_changed_lines").value == 60
    assert s.behavior("canary_cache.enabled").value is True
    assert s.behavior("canary_cache.ttl_days").value == 7
    assert s.behavior("docs_checker_parallel.enabled").value is True


def test_the_boolean_behavior_switches_follow_their_environment_variable(tmp_path):
    s = rg_settings.load(tmp_path, {"OCR_SPOTTER_SKIP": "1", "OCR_CANARY_CACHE": "off"})
    assert (s.behavior("spotter_skip.enabled").value,
            s.behavior("spotter_skip.enabled").source) == (True, "env:OCR_SPOTTER_SKIP")
    assert s.behavior("canary_cache.enabled").value is False
    junk = rg_settings.load(tmp_path, {"OCR_SPOTTER_SKIP": "maybe"})
    assert junk.behavior("spotter_skip.enabled").source == "default"


def test_every_key_resolves_with_a_source(tmp_path):
    entries = rg_settings.load(tmp_path, NO_ENV).entries()
    assert [k for k, _ in entries] == rg_settings.all_keys()
    assert all(r.source == "default" for _, r in entries)
    assert "agents.orchestrator.kimchi.model" in rg_settings.all_keys()


def test_the_shipped_schema_is_the_generated_one():
    shipped = json.loads(Path(_ROOT, "schemas", "settings.schema.json").read_text(encoding="utf-8"))
    assert shipped == rg_settings.settings_schema()


# --- the guard reads settings files --------------------------------------------

def test_kimchi_guard_flags_a_blocked_provider_from_a_file(tmp_path, user_file):
    _write(user_file, _agent("code-reviewer", "kimchi", "gpt-5"))
    bad = rg_settings.load(tmp_path, NO_ENV).kimchi_guard_violations()
    assert [(m, p) for _, m, p in bad] == [("gpt-5", "openai")]
    assert "agents.code-reviewer.kimchi.model" in bad[0][0]


def test_claude_section_may_name_an_anthropic_model(tmp_path, user_file):
    _write(user_file, _agent("code-reviewer", "claude", "claude-sonnet-4-5"))
    assert rg_settings.load(tmp_path, NO_ENV).kimchi_guard_violations() == []


# --- the gate behind the fake backend -------------------------------------------

def test_no_settings_file_launches_todays_models(repo, tmp_path):
    _commit(repo)
    assert _push(repo, _backend_env(tmp_path, OCR_BACKEND="claude"))[0] == "pass"
    assert _model_arg(_launches(tmp_path)[0]) == "sonnet"


def test_no_settings_file_launches_todays_kimchi_model(repo, tmp_path):
    _commit(repo)
    assert _push(repo, _backend_env(tmp_path, OCR_BACKEND="kimchi"))[0] == "pass"
    # the fast lane's session runs at its default thinking level, as a suffix
    assert _model_arg(_launches(tmp_path)[0]) == "kimchi-dev/kimi-k3.1-pro:high"


def test_user_file_model_changes_the_launched_claude_model(repo, tmp_path, user_file):
    _write(user_file, _agent("orchestrator", "claude", "opus"))
    _commit(repo)
    assert _push(repo, _backend_env(tmp_path, OCR_BACKEND="claude"))[0] == "pass"
    assert _model_arg(_launches(tmp_path)[0]) == "opus"


def test_user_file_model_changes_the_launched_kimchi_model(repo, tmp_path, user_file):
    _write(user_file, _agent("orchestrator", "kimchi", "kimchi-dev/glm-5.3"))
    _commit(repo)
    assert _push(repo, _backend_env(tmp_path, OCR_BACKEND="kimchi"))[0] == "pass"
    assert _model_arg(_launches(tmp_path)[0]) == "kimchi-dev/glm-5.3:high"


def test_project_file_beats_user_file_in_a_real_launch(repo, tmp_path, user_file):
    _write(user_file, _agent("orchestrator", "claude", "opus"))
    _write(rg_settings.project_settings_path(repo), _agent("orchestrator", "claude", "haiku"))
    _commit(repo)
    assert _push(repo, _backend_env(tmp_path, OCR_BACKEND="claude"))[0] == "pass"
    assert _model_arg(_launches(tmp_path)[0]) == "haiku"


def test_env_beats_both_files_in_a_real_launch(repo, tmp_path, user_file):
    _write(user_file, _agent("orchestrator", "claude", "opus"))
    _write(rg_settings.project_settings_path(repo), _agent("orchestrator", "claude", "haiku"))
    _commit(repo)
    env = _backend_env(tmp_path, OCR_BACKEND="claude", OCR_CLAUDE_MODEL="sonnet")
    assert _push(repo, env)[0] == "pass"
    assert _model_arg(_launches(tmp_path)[0]) == "sonnet"


def test_an_invalid_settings_file_denies_the_push_and_launches_nothing(repo, tmp_path, user_file):
    user_file.write_text('{"agents": {"orchestrator": {"claude": {"thinking": "extreme"}}}}',
                         encoding="utf-8")
    _commit(repo)
    decision, reason = _push(repo, _backend_env(tmp_path, OCR_BACKEND="claude"))
    assert decision == "deny"
    assert str(user_file) in reason and "thinking" in reason
    assert _launches(tmp_path) == []


@pytest.mark.parametrize("bad_model", ["gpt-5", "claude-sonnet-4-5"])
def test_guard_refuses_a_blocked_provider_pinned_in_a_file_on_kimchi(repo, tmp_path, user_file, bad_model):
    _write(user_file, _agent("code-reviewer", "kimchi", bad_model))
    _commit(repo)
    decision, reason = _push(repo, _backend_env(tmp_path, OCR_BACKEND="kimchi"))
    assert decision == "deny"
    assert "provider guard" in reason and bad_model in reason
    assert _launches(tmp_path) == []


# --- in-process: tier models and fingerprint -------------------------------------

def test_tier_model_reads_through_the_settings_files(repo, monkeypatch, user_file):
    monkeypatch.chdir(repo)
    for var in ("OCR_SPOTTER_MODEL", "OCR_TIER1_MODEL", "OCR_TIER2_MODEL", "OCR_CLAUDE_TIER1_MODEL",
                "OCR_BACKEND", "OCR_HARNESS", "CLAUDECODE", "CLAUDE_CODE_EXECPATH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OCR_BACKEND", "kimchi")
    rg._settings_root_cache.clear()
    assert rg._tier_model("tier1") == "kimchi-dev/kimi-k3.1-pro"
    _write(user_file, _agent("code-reviewer", "kimchi", "glm-5.3"))
    assert rg._tier_model("tier1") == "glm-5.3"
    monkeypatch.setenv("OCR_TIER1_MODEL", "deepseek-v4-flash")
    assert rg._tier_model("tier1") == "deepseek-v4-flash"


def test_the_fingerprint_changes_only_when_a_file_pins_a_value(repo, monkeypatch, user_file):
    monkeypatch.chdir(repo)
    monkeypatch.setenv("OCR_BACKEND", "kimchi")
    rg._settings_root_cache.clear()
    tip = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True,
                         text=True, check=True).stdout.strip()
    before = rg._fingerprint_parts(str(repo), tip)
    assert "settings:agents" not in before
    _write(user_file, _agent("code-reviewer", "kimchi", "glm-5.3"))
    after = rg._fingerprint_parts(str(repo), tip)
    assert "settings:agents" in after
    assert {k: v for k, v in after.items() if k != "settings:agents"} == before
    _write(user_file, _agent("code-reviewer", "kimchi", "kimi-k3"))
    assert rg._fingerprint_parts(str(repo), tip)["settings:agents"] != after["settings:agents"]


def test_the_guard_turns_a_settings_error_into_a_gate_error(repo, monkeypatch, user_file):
    monkeypatch.chdir(repo)
    monkeypatch.setenv("OCR_BACKEND", "kimchi")
    rg._settings_root_cache.clear()
    user_file.write_text("{broken", encoding="utf-8")
    with pytest.raises(rg.ReviewGateError) as exc:
        rg._check_provider_guard()
    assert "not valid JSON" in str(exc.value)
