"""Spotter skip (review-speed 13): small and docs-only pushes skip the spotter.

The context spotter runs inside the review skill, so the observable through the
real gate is the review manifest the stub reviewer received (STUB_TRACE): a skip
is `spotter_skipped: true` plus a `spotter_skip_reason`, and the skill launches
no spotter on it. The skip is OFF by default (behavior.spotter_skip.enabled,
OCR_SPOTTER_SKIP wins over the settings files); risk-glob escalation to the deep
lane is decided before the skip and must survive it.
"""
import json
import os
import sys
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from test_deep_gate import (  # noqa: E402
    _commit, _env, _push, _repo, _review_calls, _review_manifest, _state)

_ROOT = os.path.dirname(_HERE)


def _settings(tmp_path, **behavior):
    """Write a settings file with the given behavior block; returns the env
    entry that points the gate at it (the harness env scrubs OCR_*)."""
    path = tmp_path / "skip-settings.json"
    path.write_text(json.dumps({"behavior": behavior}), encoding="utf-8")
    return {"OCR_SETTINGS_FILE": str(path)}


def _py(n_lines):
    return "".join("v%d = %d\n" % (i, i) for i in range(n_lines))


def _manifest_of(tmp_path, files, base_files=None, **env_kw):
    """Push a commit of `files` over a base repo and return (decision, manifest)."""
    work = _repo(tmp_path, base_files or {"a.txt": "one\n"})
    tip = _commit(work, files)
    decision, reason = _push(work, _env(tmp_path, **env_kw))
    assert decision in ("pass", "deny"), reason
    if "still running" in reason:
        _state(work, tip)
    assert _review_calls(tmp_path), "the stub reviewer never ran"
    return _review_manifest(tmp_path)


ON = {"enabled": True}


def _skipped(manifest):
    return manifest.get("spotter_skipped") is True


# --- default off -----------------------------------------------------------------

def test_disabled_by_default_a_tiny_push_runs_the_spotter(tmp_path):
    m = _manifest_of(tmp_path, {"app/x.py": "x = 1\n"})
    assert "spotter_skipped" not in m and "spotter_skip_reason" not in m
    assert m["spotter_model"]


def test_disabled_by_default_a_docs_only_push_is_unchanged(tmp_path):
    work = _repo(tmp_path, {"a.txt": "one\n"})
    _commit(work, {"README.md": "# hi\n"})
    _push(work, _env(tmp_path))
    for call in _review_calls(tmp_path):
        assert "spotter_skipped" not in (call.get("manifest") or {})


# --- enabled -----------------------------------------------------------------------

def test_enabled_by_env_a_small_push_skips_the_spotter(tmp_path):
    m = _manifest_of(tmp_path, {"app/x.py": "x = 1\n"}, OCR_SPOTTER_SKIP="1")
    assert _skipped(m)
    assert "small change" in m["spotter_skip_reason"]


def test_enabled_by_settings_file_a_small_push_skips_the_spotter(tmp_path):
    m = _manifest_of(tmp_path, {"app/x.py": "x = 1\n"},
                     **_settings(tmp_path, spotter_skip=ON))
    assert _skipped(m)
    assert m["spotter_skip_reason"]


def test_a_docs_only_push_skips_the_spotter(tmp_path):
    """The docs-only branch writes a manifest only to carry the skip."""
    work = _repo(tmp_path, {"a.txt": "one\n"})
    _commit(work, {"README.md": "# hi\n"})
    decision, reason = _push(work, _env(tmp_path, OCR_SPOTTER_SKIP="1"))
    assert decision == "pass", reason
    manifests = [c.get("manifest") for c in _review_calls(tmp_path) if c.get("manifest")]
    assert manifests, "the docs-only skip manifest never reached the reviewer"
    m = manifests[0]
    assert m["spotter_skipped"] is True
    assert m["spotter_skip_reason"] == "docs-only"
    assert not m.get("paths")


def test_the_limits_are_inclusive_three_files_and_sixty_lines_skip(tmp_path):
    m = _manifest_of(
        tmp_path,
        {"app/a.py": _py(20), "app/b.py": _py(20), "app/c.py": _py(20)},
        OCR_SPOTTER_SKIP="1")
    assert _skipped(m), m.get("spotter_skip_reason")


def test_a_fourth_file_runs_the_spotter(tmp_path):
    m = _manifest_of(
        tmp_path,
        {"app/a.py": "a = 1\n", "app/b.py": "b = 1\n", "app/c.py": "c = 1\n",
         "app/d.py": "d = 1\n"},
        OCR_SPOTTER_SKIP="1")
    assert not _skipped(m)
    assert "spotter_skip_reason" not in m


def test_one_line_over_the_line_limit_runs_the_spotter(tmp_path):
    m = _manifest_of(tmp_path, {"app/a.py": _py(61)}, OCR_SPOTTER_SKIP="1")
    assert not _skipped(m)


def test_exactly_the_line_limit_still_skips(tmp_path):
    m = _manifest_of(tmp_path, {"app/a.py": _py(60)}, OCR_SPOTTER_SKIP="1")
    assert _skipped(m), m.get("spotter_skip_reason")


# --- thresholds come from settings ---------------------------------------------------

def test_max_files_is_read_from_settings(tmp_path):
    files = {"app/a.py": "a = 1\n", "app/b.py": "b = 1\n"}
    m = _manifest_of(tmp_path, files,
                     **_settings(tmp_path, spotter_skip={"enabled": True, "max_files": 1}))
    assert not _skipped(m)


def test_a_raised_max_files_skips_a_bigger_push(tmp_path):
    files = {"app/%s.py" % n: "%s = 1\n" % n for n in "abcde"}
    m = _manifest_of(tmp_path, files,
                     **_settings(tmp_path, spotter_skip={"enabled": True, "max_files": 5}))
    assert _skipped(m), m.get("spotter_skip_reason")


def test_max_changed_lines_is_read_from_settings(tmp_path):
    m = _manifest_of(
        tmp_path, {"app/a.py": _py(10)},
        **_settings(tmp_path, spotter_skip={"enabled": True, "max_changed_lines": 5}))
    assert not _skipped(m)


def test_a_raised_max_changed_lines_skips_a_bigger_push(tmp_path):
    m = _manifest_of(
        tmp_path, {"app/a.py": _py(100)},
        **_settings(tmp_path, spotter_skip={"enabled": True, "max_changed_lines": 200}))
    assert _skipped(m), m.get("spotter_skip_reason")


# --- the kill switch -----------------------------------------------------------------

def test_env_off_beats_an_enabled_setting(tmp_path):
    m = _manifest_of(tmp_path, {"app/x.py": "x = 1\n"},
                     OCR_SPOTTER_SKIP="0", **_settings(tmp_path, spotter_skip=ON))
    assert not _skipped(m)


def test_env_on_beats_a_disabled_setting(tmp_path):
    m = _manifest_of(tmp_path, {"app/x.py": "x = 1\n"},
                     OCR_SPOTTER_SKIP="1",
                     **_settings(tmp_path, spotter_skip={"enabled": False}))
    assert _skipped(m)


def test_a_disabled_setting_keeps_the_spotter(tmp_path):
    m = _manifest_of(tmp_path, {"app/x.py": "x = 1\n"},
                     **_settings(tmp_path, spotter_skip={"enabled": False}))
    assert not _skipped(m)


# --- risk escalation survives the skip -----------------------------------------------

def test_a_risk_glob_push_still_escalates_to_the_deep_lane(tmp_path):
    work = _repo(tmp_path, {"a.txt": "one\n"})
    tip = _commit(work, {"db/migrations/001_users.py": "x = 1\n"})
    env = _env(tmp_path, OCR_SPOTTER_SKIP="1", STUB_SLEEP=0)
    _push(work, env, timeout=90)
    st = _state(work, tip)
    assert st.get("state") == "done" and st.get("tier") == "deep", st
    m = _review_manifest(tmp_path)
    assert m["deep_review"] is True
    assert "db/migrations/001_users.py" in m["paths"]
    assert m["spotter_skipped"] is True


def test_the_deep_lane_without_the_skip_has_no_skip_field(tmp_path):
    work = _repo(tmp_path, {"a.txt": "one\n"})
    tip = _commit(work, {"db/migrations/001_users.py": "x = 1\n"})
    _push(work, _env(tmp_path), timeout=90)
    _state(work, tip)
    m = _review_manifest(tmp_path)
    assert m["deep_review"] is True
    assert "spotter_skipped" not in m


# --- chunked pushes never skip -------------------------------------------------------

def test_a_chunked_push_never_skips(tmp_path):
    """Chunking forces a big push: a tiny chunk size makes even a 3-file push
    chunked, and no chunk manifest may carry the skip."""
    files = {"app/a.py": "a = 1\n", "app/b.py": "b = 1\n", "app/c.py": "c = 1\n"}
    work = _repo(tmp_path, {"a.txt": "one\n"})
    tip = _commit(work, files)
    env = _env(tmp_path, OCR_SPOTTER_SKIP="1", OCR_CHUNK_THRESHOLD="1", OCR_CHUNK_FILES="1")
    _push(work, env, timeout=120)
    _state(work, tip, timeout=90)
    manifests = [c.get("manifest") for c in _review_calls(tmp_path)
                 if c.get("manifest") and not c.get("resolve_file")]
    assert manifests
    assert any(m.get("chunk_index") is not None or m.get("chunks") for m in manifests) \
        or len(manifests) > 1, "the push was not chunked"
    for m in manifests:
        assert "spotter_skipped" not in m


# --- the skill ---------------------------------------------------------------------

def test_the_skill_skips_sections_2c_and_2d_on_the_flag():
    text = Path(_ROOT, "skills", "review", "SKILL.md").read_text(encoding="utf-8")
    c = text.index("## 2c. Context demands")
    d = text.index("## 2d. Evidence bundle")
    nxt = text.index("\n## ", d + 5)
    assert "spotter_skipped" in text[c:d]
    assert "spotter_skipped" in text[d:nxt]
