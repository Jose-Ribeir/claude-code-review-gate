"""Gate settings surface (Claude port of the Kimchi variant's t15 tests).

A global config file under the gate's data dir plus an optional per-repo
override file (.ocr/config.json) resolve into a single settings read API:

  ocr_intent_config.load_settings(repo_root=None) -> {"intent": {...}, "jira": {...},
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
import ocr_intent_config  # noqa: E402


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# --- the global config file location (under the gate's store root) ----------

def test_global_config_lives_under_the_gate_data_dir():
    """The global config sits beside the gate's other data-dir files; the
    conftest's private CLAUDE_PLUGIN_DATA keeps tests off the real one."""
    assert ocr_intent_config.global_config_path().parent == \
        ocr_intent_config.data_dir()
    from pathlib import Path
    assert ocr_intent_config.data_dir() == Path(os.environ["CLAUDE_PLUGIN_DATA"])


def test_repo_config_is_the_per_repo_ocr_config(tmp_path):
    assert ocr_intent_config.repo_config_path(tmp_path) == tmp_path / ".ocr" / "config.json"


# --- defaults (no files at all) ---------------------------------------------

def test_no_files_gives_defaults_and_no_notes(tmp_path):
    settings = ocr_intent_config.load_settings(tmp_path)  # repo without .ocr/
    assert settings["intent"]["messages"] is True   # commit messages: ON by default
    assert settings["intent"]["tickets"] is False   # ticket fetching: OFF by default
    assert set(settings["intent"]["tracker_order"]) == {"github", "gitlab", "jira"}
    assert settings["jira"] == {}
    assert settings["notes"] == []


def test_no_repo_root_still_gives_defaults():
    settings = ocr_intent_config.load_settings()
    assert settings["intent"]["messages"] is True
    assert settings["intent"]["tickets"] is False
    assert settings["notes"] == []


# --- global file only ----------------------------------------------------------

def test_global_file_applies(tmp_path):
    _write(ocr_intent_config.global_config_path(), {
        "intent": {"tickets": True, "tracker_order": ["jira", "github"]},
        "jira": {"server": "https://jira.example.com", "project": "PROJ"},
    })
    settings = ocr_intent_config.load_settings()
    assert settings["intent"]["tickets"] is True
    assert settings["intent"]["messages"] is True  # untouched key keeps its default
    assert settings["intent"]["tracker_order"] == ["jira", "github"]
    assert settings["jira"]["server"] == "https://jira.example.com"
    assert settings["jira"]["project"] == "PROJ"
    assert settings["notes"] == []


# --- global + per-repo override precedence --------------------------------------

def test_repo_keys_win_over_global_key_by_key(tmp_path):
    _write(ocr_intent_config.global_config_path(), {
        "intent": {"tickets": False, "messages": False,
                   "tracker_order": ["github"]},
        "jira": {"server": "https://global.example.com"},
    })
    _write(tmp_path / ".ocr" / "config.json", {
        "blocking": True,  # pre-existing key used by the gate; ignored here
        "intent": {"tickets": True},  # only this key overridden per repo
    })
    settings = ocr_intent_config.load_settings(tmp_path)
    # overridden key: repo wins
    assert settings["intent"]["tickets"] is True
    # un-overridden keys: global still applies
    assert settings["intent"]["messages"] is False
    assert settings["intent"]["tracker_order"] == ["github"]
    assert settings["jira"]["server"] == "https://global.example.com"
    assert settings["notes"] == []


def test_repo_jira_keys_win_over_global_jira_keys(tmp_path):
    _write(ocr_intent_config.global_config_path(), {
        "jira": {"server": "https://global.example.com", "project": "GLOB"},
    })
    _write(tmp_path / ".ocr" / "config.json", {
        "jira": {"project": "LOCAL"},
    })
    settings = ocr_intent_config.load_settings(tmp_path)
    assert settings["jira"]["project"] == "LOCAL"
    assert settings["jira"]["server"] == "https://global.example.com"


def test_repo_only_without_global_applies(tmp_path):
    _write(tmp_path / ".ocr" / "config.json", {"intent": {"messages": False}})
    settings = ocr_intent_config.load_settings(tmp_path)
    assert settings["intent"]["messages"] is False
    assert settings["intent"]["tickets"] is False  # default


# --- invalid files degrade to defaults with a note -------------------------------

def test_invalid_global_json_degrades_with_a_note():
    p = ocr_intent_config.global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    settings = ocr_intent_config.load_settings()
    assert settings["intent"]["messages"] is True
    assert settings["intent"]["tickets"] is False
    assert any("config" in n.lower() or "global" in n.lower() for n in settings["notes"])


def test_invalid_repo_json_keeps_global_and_notes_it(tmp_path):
    _write(ocr_intent_config.global_config_path(), {"intent": {"tickets": True}})
    bad = tmp_path / ".ocr" / "config.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not json at all", encoding="utf-8")
    settings = ocr_intent_config.load_settings(tmp_path)
    assert settings["intent"]["tickets"] is True  # global still applies
    assert settings["notes"]  # and the broken repo file was noted


def test_repo_config_directory_is_ignored(tmp_path):
    """An .ocr/ dir with no config.json is simply absent, not an error."""
    (tmp_path / ".ocr").mkdir()
    settings = ocr_intent_config.load_settings(tmp_path)
    assert settings["notes"] == []


# --- invalid VALUES degrade per-key, with a note ---------------------------------

def test_wrong_type_for_a_switch_degrades_that_key_only(tmp_path):
    _write(ocr_intent_config.global_config_path(), {
        "intent": {"tickets": "banana", "messages": False},
    })
    settings = ocr_intent_config.load_settings()
    assert settings["intent"]["tickets"] is False  # default, noted
    assert settings["intent"]["messages"] is False  # the valid sibling applies
    assert settings["notes"]


def test_on_off_strings_are_accepted_switches():
    _write(ocr_intent_config.global_config_path(), {
        "intent": {"tickets": "on", "messages": "off"},
    })
    settings = ocr_intent_config.load_settings()
    assert settings["intent"]["tickets"] is True
    assert settings["intent"]["messages"] is False
    assert settings["notes"] == []


def test_tracker_order_must_be_a_list():
    _write(ocr_intent_config.global_config_path(), {"intent": {"tracker_order": "github"}})
    settings = ocr_intent_config.load_settings()
    assert set(settings["intent"]["tracker_order"]) == {"github", "gitlab", "jira"}
    assert settings["notes"]


def test_tracker_order_dedupes_and_drops_unknown_trackers():
    _write(ocr_intent_config.global_config_path(), {
        "intent": {"tracker_order": ["jira", "jira", "bitbucket", "github"]},
    })
    settings = ocr_intent_config.load_settings()
    assert settings["intent"]["tracker_order"] == ["jira", "github"]
    assert any("bitbucket" in n for n in settings["notes"])


def test_jira_block_must_be_a_dict():
    _write(ocr_intent_config.global_config_path(), {"jira": "https://jira.example.com"})
    settings = ocr_intent_config.load_settings()
    assert settings["jira"] == {}
    assert settings["notes"]


def test_top_level_config_that_is_not_an_object_degrades():
    p = ocr_intent_config.global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    settings = ocr_intent_config.load_settings()
    assert settings["intent"]["messages"] is True
    assert settings["notes"]


def test_unknown_top_level_keys_are_ignored_without_noise():
    _write(ocr_intent_config.global_config_path(), {"future_feature": {"x": 1}})
    settings = ocr_intent_config.load_settings()
    assert settings["intent"]["messages"] is True
    assert settings["notes"] == []
