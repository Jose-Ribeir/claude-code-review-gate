"""Gate run-config surface (pg1).

The user-level config file (default ~/.config/review-gate/config.json,
overridable via OCR_CONFIG_FILE) plus three env overrides resolve into a
single frozen read:

  rg_config.load_config() -> GateConfig(depth, timeout_seconds, fail_open,
                                        warnings)

Keys: review.depth (full|structural|large-diff-scoped, default full),
review.timeoutSeconds (int >= 0, 0 = no cap, default 0), review.failOpen
(bool, default false). Precedence, key by key: env var > config file >
built-in default. Missing files degrade to defaults silently; malformed
files and invalid values degrade to defaults WITH a warning (per key).
"""
import dataclasses
import json
import os
import sys
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")

sys.path.insert(0, _SCRIPTS)
import rg_config  # noqa: E402

_ENV_VARS = ("OCR_CONFIG_FILE", "OCR_REVIEW_DEPTH",
             "OCR_REVIEW_TIMEOUT_SECONDS", "OCR_FAIL_OPEN")


@pytest.fixture(autouse=True)
def _private_config_file(monkeypatch, tmp_path):
    """Keep every test off the developer's real config: point OCR_CONFIG_FILE
    at a not-yet-written tmp path and clear the override env vars."""
    for key in _ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    cfg = tmp_path / "cfg" / "config.json"
    monkeypatch.setenv("OCR_CONFIG_FILE", str(cfg))
    return cfg


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# --- file location -----------------------------------------------------------

def test_default_config_path_is_xdg_style_user_config(monkeypatch):
    monkeypatch.delenv("OCR_CONFIG_FILE")
    assert rg_config.user_config_path() == (
        Path.home() / ".config" / "review-gate" / "config.json"
    )


def test_ocr_config_file_overrides_the_location(_private_config_file):
    assert rg_config.user_config_path() == _private_config_file


# --- defaults ----------------------------------------------------------------

def test_missing_config_directory_gives_defaults_silently(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_CONFIG_FILE",
                       str(tmp_path / "no" / "such" / "dir" / "config.json"))
    cfg = rg_config.load_config()
    assert cfg.depth == "full"
    assert cfg.timeout_seconds == 0
    assert cfg.fail_open is False
    assert cfg.warnings == []


def test_missing_config_file_gives_defaults_silently(_private_config_file):
    cfg = rg_config.load_config()
    assert cfg.depth == "full"
    assert cfg.timeout_seconds == 0
    assert cfg.fail_open is False
    assert cfg.warnings == []


def test_result_is_a_frozen_dataclass(_private_config_file):
    cfg = rg_config.load_config()
    assert dataclasses.is_dataclass(cfg)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.depth = "structural"
    assert isinstance(cfg.warnings, list)


# --- the config file supplies values -----------------------------------------

def test_config_file_supplies_all_keys(_private_config_file):
    _write(_private_config_file, {
        "review": {"depth": "structural", "timeoutSeconds": 120,
                   "failOpen": True},
    })
    cfg = rg_config.load_config()
    assert cfg.depth == "structural"
    assert cfg.timeout_seconds == 120
    assert cfg.fail_open is True
    assert cfg.warnings == []


def test_partial_file_keeps_defaults_for_unset_keys(_private_config_file):
    _write(_private_config_file, {"review": {"depth": "large-diff-scoped"}})
    cfg = rg_config.load_config()
    assert cfg.depth == "large-diff-scoped"
    assert cfg.timeout_seconds == 0
    assert cfg.fail_open is False
    assert cfg.warnings == []


def test_zero_timeout_means_no_cap_and_is_valid(_private_config_file):
    _write(_private_config_file, {"review": {"timeoutSeconds": 0}})
    cfg = rg_config.load_config()
    assert cfg.timeout_seconds == 0
    assert cfg.warnings == []


def test_unrelated_namespaces_are_ignored_without_noise(_private_config_file):
    _write(_private_config_file, {"intent": {"tickets": True},
                                  "future": {"x": 1}})
    cfg = rg_config.load_config()
    assert cfg.depth == "full"
    assert cfg.warnings == []


# --- precedence: env > file > default ----------------------------------------

def test_env_wins_over_file_wins_over_default_for_depth(
        _private_config_file, monkeypatch):
    monkeypatch.setenv("OCR_REVIEW_DEPTH", "large-diff-scoped")
    _write(_private_config_file, {"review": {"depth": "structural"}})
    assert rg_config.load_config().depth == "large-diff-scoped"


def test_env_wins_over_file_for_timeout(_private_config_file, monkeypatch):
    monkeypatch.setenv("OCR_REVIEW_TIMEOUT_SECONDS", "45")
    _write(_private_config_file, {"review": {"timeoutSeconds": 120}})
    assert rg_config.load_config().timeout_seconds == 45


def test_env_wins_over_file_for_fail_open(_private_config_file, monkeypatch):
    monkeypatch.setenv("OCR_FAIL_OPEN", "0")
    _write(_private_config_file, {"review": {"failOpen": True}})
    assert rg_config.load_config().fail_open is False


def test_file_wins_over_default_when_env_absent_for_each_key(
        _private_config_file):
    _write(_private_config_file, {
        "review": {"depth": "structural", "timeoutSeconds": 5,
                   "failOpen": True},
    })
    cfg = rg_config.load_config()
    assert (cfg.depth, cfg.timeout_seconds, cfg.fail_open) == \
        ("structural", 5, True)


# --- the pre-existing OCR_FAIL_OPEN=1 convention ------------------------------

def test_ocr_fail_open_one_keeps_working(_private_config_file, monkeypatch):
    monkeypatch.setenv("OCR_FAIL_OPEN", "1")
    assert rg_config.load_config().fail_open is True


@pytest.mark.parametrize("word", ["1", "true", "yes", "TRUE", "Yes"])
def test_ocr_fail_open_truthy_words(_private_config_file, monkeypatch, word):
    monkeypatch.setenv("OCR_FAIL_OPEN", word)
    assert rg_config.load_config().fail_open is True


@pytest.mark.parametrize("word", ["0", "false", "no"])
def test_ocr_fail_open_falsy_words(_private_config_file, monkeypatch, word):
    _write(_private_config_file, {"review": {"failOpen": True}})
    monkeypatch.setenv("OCR_FAIL_OPEN", word)
    assert rg_config.load_config().fail_open is False


# --- malformed files degrade with a warning, never crash ----------------------

def test_malformed_json_gives_defaults_with_a_warning(_private_config_file):
    _private_config_file.parent.mkdir(parents=True)
    _private_config_file.write_text("{not json", encoding="utf-8")
    cfg = rg_config.load_config()
    assert cfg.depth == "full"
    assert cfg.timeout_seconds == 0
    assert cfg.fail_open is False
    assert cfg.warnings and any("json" in w.lower() for w in cfg.warnings)


def test_non_object_top_level_gives_defaults_with_a_warning(
        _private_config_file):
    _write(_private_config_file, ["not", "a", "dict"])
    cfg = rg_config.load_config()
    assert cfg.depth == "full"
    assert cfg.warnings


def test_review_block_that_is_not_an_object_degrades(_private_config_file):
    _write(_private_config_file, {"review": "structural"})
    cfg = rg_config.load_config()
    assert cfg.depth == "full"
    assert cfg.warnings


def test_config_path_pointing_at_a_directory_degrades(_private_config_file,
                                                      monkeypatch):
    monkeypatch.setenv("OCR_CONFIG_FILE", str(_private_config_file.parent))
    _private_config_file.parent.mkdir(parents=True)
    cfg = rg_config.load_config()
    assert cfg.depth == "full"
    assert cfg.fail_open is False
    assert cfg.warnings


# --- invalid values: warning + default for that key only ----------------------

def test_invalid_depth_in_file_warns_and_defaults_that_key_only(
        _private_config_file):
    _write(_private_config_file, {
        "review": {"depth": "quick", "timeoutSeconds": 30},
    })
    cfg = rg_config.load_config()
    assert cfg.depth == "full"         # the invalid key
    assert cfg.timeout_seconds == 30   # the valid sibling still applies
    assert any("depth" in w and "quick" in w for w in cfg.warnings)


@pytest.mark.parametrize("bad", ["quick", "FULL-DEPTH", "", "full ", 3, None])
def test_invalid_depth_values_in_file(_private_config_file, bad):
    _write(_private_config_file, {"review": {"depth": bad}})
    cfg = rg_config.load_config()
    assert cfg.depth == "full"
    assert cfg.warnings


@pytest.mark.parametrize("bad", [-1, "fast", 1.5, True, [30]])
def test_invalid_timeout_in_file_warns_and_defaults(_private_config_file, bad):
    _write(_private_config_file, {"review": {"timeoutSeconds": bad}})
    cfg = rg_config.load_config()
    assert cfg.timeout_seconds == 0
    assert cfg.warnings


@pytest.mark.parametrize("bad", ["banana", 7, [True]])
def test_invalid_fail_open_in_file_warns_and_defaults(
        _private_config_file, bad):
    _write(_private_config_file, {"review": {"failOpen": bad}})
    cfg = rg_config.load_config()
    assert cfg.fail_open is False
    assert cfg.warnings


# --- invalid env values: warn, fall to the next precedence level --------------

def test_invalid_env_depth_falls_back_to_the_file(_private_config_file,
                                                  monkeypatch):
    monkeypatch.setenv("OCR_REVIEW_DEPTH", "turbo")
    _write(_private_config_file, {"review": {"depth": "structural"}})
    cfg = rg_config.load_config()
    assert cfg.depth == "structural"
    assert cfg.warnings


def test_invalid_env_depth_with_no_file_falls_back_to_default(
        _private_config_file, monkeypatch):
    monkeypatch.setenv("OCR_REVIEW_DEPTH", "turbo")
    cfg = rg_config.load_config()
    assert cfg.depth == "full"
    assert cfg.warnings


@pytest.mark.parametrize("bad", ["banana", "-5", "1.5"])
def test_invalid_env_timeout_falls_back(_private_config_file, monkeypatch,
                                        bad):
    monkeypatch.setenv("OCR_REVIEW_TIMEOUT_SECONDS", bad)
    _write(_private_config_file, {"review": {"timeoutSeconds": 9}})
    cfg = rg_config.load_config()
    assert cfg.timeout_seconds == 9
    assert cfg.warnings


def test_invalid_env_fail_open_falls_back(_private_config_file, monkeypatch):
    monkeypatch.setenv("OCR_FAIL_OPEN", "banana")
    _write(_private_config_file, {"review": {"failOpen": True}})
    cfg = rg_config.load_config()
    assert cfg.fail_open is True
    assert cfg.warnings


def test_empty_env_values_are_treated_as_unset(_private_config_file,
                                               monkeypatch):
    _write(_private_config_file, {
        "review": {"depth": "structural", "timeoutSeconds": 7,
                   "failOpen": True},
    })
    for key in ("OCR_REVIEW_DEPTH", "OCR_REVIEW_TIMEOUT_SECONDS",
                "OCR_FAIL_OPEN"):
        monkeypatch.setenv(key, "  ")
    cfg = rg_config.load_config()
    assert (cfg.depth, cfg.timeout_seconds, cfg.fail_open) == \
        ("structural", 7, True)
    assert cfg.warnings == []
