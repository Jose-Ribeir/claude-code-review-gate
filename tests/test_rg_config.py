"""Gate settings surface (t15).

A global config file under the gate's store root plus an optional per-repo
override file (.ocr/config.json) resolve into a single settings read API:

  rg_config.load_settings(repo_root=None) -> {"intent": {...}, "jira": {...},
                                              "notes": [...]}

Defaults: intent.messages ON, intent.tickets OFF, a tracker order, and a Jira
coordinates block. Per-repo keys win over global, key by key. Missing files
degrade to defaults; invalid files/values degrade to defaults WITH A NOTE.
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")

sys.path.insert(0, _SCRIPTS)
import rg_config  # noqa: E402


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# --- the global config file location (under the gate's store root) ----------

def test_global_config_lives_under_the_state_store_root():
    import rg_store
    assert rg_config.global_config_path().parent == rg_store.store_root()


def test_repo_config_is_the_per_repo_ocr_config(tmp_path):
    assert rg_config.repo_config_path(tmp_path) == tmp_path / ".ocr" / "config.json"


# --- defaults (no files at all) ---------------------------------------------

def test_no_files_gives_defaults_and_no_notes(tmp_path):
    settings = rg_config.load_settings(tmp_path)  # repo without .ocr/
    assert settings["intent"]["messages"] is True   # commit messages: ON by default
    assert settings["intent"]["tickets"] is False   # ticket fetching: OFF by default
    assert set(settings["intent"]["tracker_order"]) == {"github", "gitlab", "jira"}
    assert settings["jira"] == {}
    assert settings["notes"] == []


def test_no_repo_root_still_gives_defaults():
    settings = rg_config.load_settings()
    assert settings["intent"]["messages"] is True
    assert settings["intent"]["tickets"] is False
    assert settings["notes"] == []


# --- global file only ----------------------------------------------------------

def test_global_file_applies(tmp_path):
    _write(rg_config.global_config_path(), {
        "intent": {"tickets": True, "tracker_order": ["jira", "github"]},
        "jira": {"server": "https://jira.example.com", "project": "PROJ"},
    })
    settings = rg_config.load_settings()
    assert settings["intent"]["tickets"] is True
    assert settings["intent"]["messages"] is True  # untouched key keeps its default
    assert settings["intent"]["tracker_order"] == ["jira", "github"]
    assert settings["jira"]["server"] == "https://jira.example.com"
    assert settings["jira"]["project"] == "PROJ"
    assert settings["notes"] == []


# --- global + per-repo override precedence --------------------------------------

def test_repo_keys_win_over_global_key_by_key(tmp_path):
    _write(rg_config.global_config_path(), {
        "intent": {"tickets": False, "messages": False,
                   "tracker_order": ["github"]},
        "jira": {"server": "https://global.example.com"},
    })
    _write(tmp_path / ".ocr" / "config.json", {
        "blocking": True,  # pre-existing key used by the gate; ignored here
        "intent": {"tickets": True},  # only this key overridden per repo
    })
    settings = rg_config.load_settings(tmp_path)
    # overridden key: repo wins
    assert settings["intent"]["tickets"] is True
    # un-overridden keys: global still applies
    assert settings["intent"]["messages"] is False
    assert settings["intent"]["tracker_order"] == ["github"]
    assert settings["jira"]["server"] == "https://global.example.com"
    assert settings["notes"] == []


def test_repo_jira_keys_win_over_global_jira_keys(tmp_path):
    _write(rg_config.global_config_path(), {
        "jira": {"server": "https://global.example.com", "project": "GLOB"},
    })
    _write(tmp_path / ".ocr" / "config.json", {
        "jira": {"project": "LOCAL"},
    })
    settings = rg_config.load_settings(tmp_path)
    assert settings["jira"]["project"] == "LOCAL"
    assert settings["jira"]["server"] == "https://global.example.com"


def test_repo_only_without_global_applies(tmp_path):
    _write(tmp_path / ".ocr" / "config.json", {"intent": {"messages": False}})
    settings = rg_config.load_settings(tmp_path)
    assert settings["intent"]["messages"] is False
    assert settings["intent"]["tickets"] is False  # default


# --- invalid files degrade to defaults with a note -------------------------------

def test_invalid_global_json_degrades_with_a_note():
    p = rg_config.global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    settings = rg_config.load_settings()
    assert settings["intent"]["messages"] is True
    assert settings["intent"]["tickets"] is False
    assert any("config" in n.lower() or "global" in n.lower() for n in settings["notes"])


def test_invalid_repo_json_keeps_global_and_notes_it(tmp_path):
    _write(rg_config.global_config_path(), {"intent": {"tickets": True}})
    bad = tmp_path / ".ocr" / "config.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not json at all", encoding="utf-8")
    settings = rg_config.load_settings(tmp_path)
    assert settings["intent"]["tickets"] is True  # global still applies
    assert settings["notes"]  # and the broken repo file was noted


def test_repo_config_directory_is_ignored(tmp_path):
    """An .ocr/ dir with no config.json is simply absent, not an error."""
    (tmp_path / ".ocr").mkdir()
    settings = rg_config.load_settings(tmp_path)
    assert settings["notes"] == []


# --- invalid VALUES degrade per-key, with a note ---------------------------------

def test_wrong_type_for_a_switch_degrades_that_key_only(tmp_path):
    _write(rg_config.global_config_path(), {
        "intent": {"tickets": "banana", "messages": False},
    })
    settings = rg_config.load_settings()
    assert settings["intent"]["tickets"] is False  # default, noted
    assert settings["intent"]["messages"] is False  # the valid sibling applies
    assert settings["notes"]


def test_on_off_strings_are_accepted_switches():
    _write(rg_config.global_config_path(), {
        "intent": {"tickets": "on", "messages": "off"},
    })
    settings = rg_config.load_settings()
    assert settings["intent"]["tickets"] is True
    assert settings["intent"]["messages"] is False
    assert settings["notes"] == []


def test_tracker_order_must_be_a_list():
    _write(rg_config.global_config_path(), {"intent": {"tracker_order": "github"}})
    settings = rg_config.load_settings()
    assert set(settings["intent"]["tracker_order"]) == {"github", "gitlab", "jira"}
    assert settings["notes"]


def test_tracker_order_dedupes_and_drops_unknown_trackers():
    _write(rg_config.global_config_path(), {
        "intent": {"tracker_order": ["jira", "jira", "bitbucket", "github"]},
    })
    settings = rg_config.load_settings()
    assert settings["intent"]["tracker_order"] == ["jira", "github"]
    assert any("bitbucket" in n for n in settings["notes"])


def test_jira_block_must_be_a_dict():
    _write(rg_config.global_config_path(), {"jira": "https://jira.example.com"})
    settings = rg_config.load_settings()
    assert settings["jira"] == {}
    assert settings["notes"]


def test_top_level_config_that_is_not_an_object_degrades():
    p = rg_config.global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    settings = rg_config.load_settings()
    assert settings["intent"]["messages"] is True
    assert settings["notes"]


def test_unknown_top_level_keys_are_ignored_without_noise():
    _write(rg_config.global_config_path(), {"future_feature": {"x": 1}})
    settings = rg_config.load_settings()
    assert settings["intent"]["messages"] is True
    assert settings["notes"] == []


# --- the checks surface (SPEC-14.1) --------------------------------------------

# Built-in checks: docs-deslop on by default, the ADR duties off (they
# presuppose ADR conventions a repo may not follow); everything blocks.


def test_checks_defaults_resolve(tmp_path):
    settings = rg_config.load_settings(tmp_path)
    checks = settings["checks"]
    assert checks["docs-deslop"]["enabled"] is True
    assert checks["adr-wait-what"]["enabled"] is False
    assert checks["adr-integrity"]["enabled"] is False
    for name in ("docs-deslop", "adr-wait-what", "adr-integrity"):
        assert checks[name]["mode"] == "block"
    assert checks["docs-deslop"]["globs"] == ["**/*.md"]
    assert checks["adr-wait-what"]["globs"] == ["docs/adr/**/*.md", "**/adr/*.md"]
    assert checks["adr-integrity"]["globs"] == ["docs/adr/**/*.md", "**/adr/*.md"]
    assert settings["notes"] == []


def test_checks_settles_alongside_intent_and_jira():
    settings = rg_config.load_settings()
    assert {"intent", "jira", "checks", "notes"} <= set(settings)


def test_checks_key_values_are_not_shared_between_calls(tmp_path):
    first = rg_config.load_settings()["checks"]
    first["docs-deslop"]["globs"].append("mutated.md")
    first["docs-deslop"]["enabled"] = "mutated"
    fresh = rg_config.load_settings(tmp_path)
    assert fresh["checks"]["docs-deslop"]["globs"] == ["**/*.md"]
    assert fresh["checks"]["docs-deslop"]["enabled"] is True


def test_checks_global_file_applies(tmp_path):
    _write(rg_config.global_config_path(), {
        "checks": {
            "docs-deslop": {"enabled": "off", "mode": "warn"},
            "adr-wait-what": {"enabled": True},
        },
    })
    checks = rg_config.load_settings()["checks"]
    assert checks["docs-deslop"]["enabled"] is False
    assert checks["docs-deslop"]["mode"] == "warn"
    assert checks["docs-deslop"]["globs"] == ["**/*.md"]  # untouched key keeps default
    assert checks["adr-wait-what"]["enabled"] is True
    assert checks["adr-wait-what"]["mode"] == "block"      # untouched key keeps default
    checks_notes = rg_config.load_settings()["notes"]
    assert checks_notes == []


def test_checks_repo_keys_win_over_global_key_by_key(tmp_path):
    _write(rg_config.global_config_path(), {
        "checks": {"docs-deslop": {"enabled": False, "mode": "warn",
                                   "globs": ["global/*.md"]}},
    })
    _write(tmp_path / ".ocr" / "config.json", {
        "checks": {"docs-deslop": {"enabled": True}},  # only this key overridden per repo
    })
    checks = rg_config.load_settings(tmp_path)["checks"]
    assert checks["docs-deslop"]["enabled"] is True       # repo wins
    assert checks["docs-deslop"]["mode"] == "warn"        # global still applies
    assert checks["docs-deslop"]["globs"] == ["global/*.md"]


def test_checks_invalid_repo_json_keeps_global_and_notes_it(tmp_path):
    _write(rg_config.global_config_path(), {
        "checks": {"adr-integrity": {"enabled": True}},
    })
    bad = tmp_path / ".ocr" / "config.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("nope", encoding="utf-8")
    settings = rg_config.load_settings(tmp_path)
    assert settings["checks"]["adr-integrity"]["enabled"] is True
    assert settings["notes"]


def test_checks_block_must_be_an_object():
    _write(rg_config.global_config_path(), {"checks": ["docs-deslop"]})
    settings = rg_config.load_settings()
    assert settings["checks"]["docs-deslop"]["enabled"] is True  # defaults kept
    assert settings["notes"]


def test_unknown_check_without_a_command_is_noted_and_ignored():
    _write(rg_config.global_config_path(), {
        "checks": {"docs-desolp": {"enabled": False}},  # a typo, not a check
    })
    settings = rg_config.load_settings()
    assert "docs-desolp" not in settings["checks"]
    assert any("docs-desolp" in n for n in settings["notes"])
    assert settings["checks"]["docs-deslop"]["enabled"] is True  # default kept


def test_a_command_check_registers_as_a_user_check():
    _write(rg_config.global_config_path(), {
        "checks": {"my-lint": {"command": "tools/lint.sh", "globs": ["**/*.py"],
                               "mode": "warn"}},
    })
    settings = rg_config.load_settings()
    check = settings["checks"]["my-lint"]
    assert check["command"] == "tools/lint.sh"
    assert check["globs"] == ["**/*.py"]
    assert check["mode"] == "warn"
    assert check["enabled"] is True  # a declared check defaults to on
    assert settings["notes"] == []


def test_a_command_check_without_globs_is_noted_as_never_running():
    """A declared command check with no globs matches no changed file, so it
    silently never runs. Fail-soft AND observable (ADR 0006): the check still
    registers with the command-check defaults, and load_settings leaves one
    note naming the dead check."""
    _write(rg_config.global_config_path(), {
        "checks": {"my-lint": {"command": "tools/lint.sh"}},
    })
    settings = rg_config.load_settings()
    check = settings["checks"]["my-lint"]
    assert check["command"] == "tools/lint.sh"
    assert check["globs"] == []
    assert check["mode"] == "block"
    assert check["enabled"] is True
    assert any("my-lint" in n and "will never run" in n
               for n in settings["notes"]), settings["notes"]


def test_an_unknown_check_with_an_invalid_command_is_noted_and_ignored():
    _write(rg_config.global_config_path(), {
        "checks": {"weird": {"command": ["tools/lint.sh"]}},
    })
    settings = rg_config.load_settings()
    assert "weird" not in settings["checks"]
    assert settings["notes"]


def test_command_on_a_builtin_must_be_a_string():
    _write(rg_config.global_config_path(), {
        "checks": {"docs-deslop": {"command": 42}},
    })
    settings = rg_config.load_settings()
    assert "command" not in settings["checks"]["docs-deslop"]
    assert settings["notes"]


def test_checks_invalid_enabled_value_is_noted_and_kept():
    _write(rg_config.global_config_path(), {
        "checks": {"docs-deslop": {"enabled": "banana", "mode": "warn"}},
    })
    settings = rg_config.load_settings()
    assert settings["checks"]["docs-deslop"]["enabled"] is True  # default kept
    assert settings["checks"]["docs-deslop"]["mode"] == "warn"    # valid sibling applies
    assert settings["notes"]


def test_checks_mode_must_be_block_or_warn():
    _write(rg_config.global_config_path(), {
        "checks": {"docs-deslop": {"mode": "scream"}},
    })
    settings = rg_config.load_settings()
    assert settings["checks"]["docs-deslop"]["mode"] == "block"
    assert settings["notes"]


def test_checks_globs_must_be_a_list_of_strings():
    _write(rg_config.global_config_path(), {
        "checks": {"docs-deslop": {"globs": "**/*.md"}},
    })
    settings = rg_config.load_settings()
    assert settings["checks"]["docs-deslop"]["globs"] == ["**/*.md"]  # kept
    assert settings["notes"]


def test_checks_globs_drop_non_string_entries_with_a_note():
    _write(rg_config.global_config_path(), {
        "checks": {"docs-deslop": {"globs": ["docs/*.md", 7, None]}},
    })
    settings = rg_config.load_settings()
    assert settings["checks"]["docs-deslop"]["globs"] == ["docs/*.md"]
    assert settings["notes"]


def test_checks_options_pass_through_when_plausibly_typed():
    _write(rg_config.global_config_path(), {
        "checks": {"adr-wait-what": {
            "enabled": True,
            "sections": ["Status", "Date", "Context", "Decision", "Consequences"],
            "template": "docs/adr/template.md",
            "sentence_cap": 25,
            "sweep": True,
        }},
    })
    settings = rg_config.load_settings()
    check = settings["checks"]["adr-wait-what"]
    assert check["sections"] == ["Status", "Date", "Context", "Decision", "Consequences"]
    assert check["template"] == "docs/adr/template.md"
    assert check["sentence_cap"] == 25
    assert check["sweep"] is True
    assert settings["notes"] == []


def test_checks_invalid_options_are_noted_and_kept():
    _write(rg_config.global_config_path(), {
        "checks": {"adr-integrity": {
            "enabled": True,
            "sections": "not a list",
            "template": 7,
            "sentence_cap": "25",
            "sweep": "banana",
        }},
    })
    settings = rg_config.load_settings()
    check = settings["checks"]["adr-integrity"]
    assert check["enabled"] is True  # the valid sibling applies
    assert "sections" not in check
    assert "template" not in check
    assert "sentence_cap" not in check
    assert "sweep" not in check
    assert len(settings["notes"]) == 4


# --- the v2 stubs (SPEC-14 story 24) -----------------------------------------

# commit_messages and code_comments are the v2 deslop surfaces: recognized
# keys shipping disabled and inert -- declaring one must not earn the
# unknown-check typo note, and nothing executes for them in v1.

def test_v2_stubs_ship_disabled_and_inert(tmp_path):
    settings = rg_config.load_settings(tmp_path)
    checks = settings["checks"]
    for stub in ("commit_messages", "code_comments"):
        assert checks[stub]["enabled"] is False
        assert checks[stub]["mode"] == "block"
        assert checks[stub]["globs"] == []
    assert settings["notes"] == []


def test_declaring_a_v2_stub_earns_no_typo_note(tmp_path):
    _write(rg_config.repo_config_path(tmp_path), {
        "checks": {"commit_messages": {"enabled": True},
                   "code_comments": {"globs": ["**/*.py"],
                                     "sentence_cap": 30}}})
    settings = rg_config.load_settings(tmp_path)
    checks = settings["checks"]
    assert checks["commit_messages"]["enabled"] is True
    assert checks["code_comments"]["globs"] == ["**/*.py"]
    assert checks["code_comments"]["sentence_cap"] == 30
    assert settings["notes"] == []


def test_a_v2_stub_typo_is_still_a_typo(tmp_path):
    _write(rg_config.repo_config_path(tmp_path), {
        "checks": {"commit_message": {"enabled": True}}})
    settings = rg_config.load_settings(tmp_path)
    assert "commit_message" not in settings["checks"]
    assert any("unknown check" in n for n in settings["notes"])


def test_unknown_keys_inside_a_check_are_noted_and_ignored():
    _write(rg_config.global_config_path(), {
        "checks": {"docs-deslop": {"goblin": "gremlin"}},
    })
    settings = rg_config.load_settings()
    assert "goblin" not in settings["checks"]["docs-deslop"]
    assert any("goblin" in n for n in settings["notes"])
