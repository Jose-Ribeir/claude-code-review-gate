"""Canary cache (review-speed 09): a passed kimchi sandbox canary is remembered.

The gate is driven end to end behind the fake kimchi CLI (tests/fake_backend.py);
the canary launches the fake records in FAKE_TRACE are counted. Every scenario
pushes from two repos that share one state dir (conftest gives each test its own),
because the fake on Windows only sees the first review's multi-line prompt per repo.
The deny extension's content cannot be varied under the real gate (it is the
plugin's own file), so that key part is covered in process with the real cache
functions and a temp extension file.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts"))
import fake_backend  # noqa: E402
import rg_settings  # noqa: E402
from test_async_gate import _git  # noqa: E402
from test_claude_profile import _backend_env, _canaries, _launches, _push, rg  # noqa: E402

FAST = "kimchi-dev/kimi-k3.1-pro"
ALT = "kimchi-dev/glm-5.3"


def _make_repo(tmp_path, name):
    base = tmp_path / name
    base.mkdir()
    remote, work = base / "origin.git", base / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=base)
    _git(["init", "-b", "main", str(work)], cwd=base)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        _git(["config", k, v], cwd=work)
    hooks = base / "no-hooks"
    hooks.mkdir()
    _git(["config", "core.hooksPath", str(hooks)], cwd=work)
    (work / "a.txt").write_text("one\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    (work / "feat.txt").write_text("x\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "feat"], cwd=work)
    return work


def _push_new_repo(tmp_path, name, **env):
    work = _make_repo(tmp_path, name)
    return _push(work, _backend_env(tmp_path, OCR_BACKEND="kimchi", **env))


def _cache_files():
    return sorted((Path(os.environ["OCR_STATE_DIR"]) / "canary-cache").glob("*.json"))


def _write_settings(data):
    path = Path(os.environ["OCR_SETTINGS_FILE"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_the_second_push_launches_no_canary(tmp_path):
    assert _push_new_repo(tmp_path, "one")[0] == "pass"
    assert len(_canaries(tmp_path)) == 1
    assert len(_cache_files()) == 1
    assert _push_new_repo(tmp_path, "two")[0] == "pass"
    assert len(_canaries(tmp_path)) == 1          # no second probe
    assert len(_launches(tmp_path)) == 2          # both pushes were still reviewed


def test_a_changed_cli_version_reruns_the_canary(tmp_path):
    assert _push_new_repo(tmp_path, "one")[0] == "pass"
    assert _push_new_repo(tmp_path, "two", FAKE_VERSION="kimchi 9.9.9")[0] == "pass"
    assert len(_canaries(tmp_path)) == 2
    assert len(_cache_files()) == 2


def test_a_changed_canary_model_reruns_the_canary(tmp_path):
    assert _push_new_repo(tmp_path, "one")[0] == "pass"
    _write_settings({"agents": {"canary": {"kimchi": {"model": ALT}}}})
    assert _push_new_repo(tmp_path, "two")[0] == "pass"
    canaries = _canaries(tmp_path)
    assert len(canaries) == 2
    argv = canaries[1]["argv"]
    assert argv[argv.index("--model") + 1] == ALT


def test_a_failed_canary_is_denied_and_never_cached(tmp_path):
    decision, reason = _push_new_repo(tmp_path, "one", FAKE_CANARY_LEAK="1")
    assert decision == "deny"
    assert "sandbox" in reason.lower()
    assert len(_canaries(tmp_path)) == 1
    assert _cache_files() == []
    # The next push probes again (nothing was remembered) and now passes.
    assert _push_new_repo(tmp_path, "two")[0] == "pass"
    assert len(_canaries(tmp_path)) == 2
    assert len(_cache_files()) == 1


def test_a_cached_pass_does_not_excuse_a_later_leak_after_a_key_change(tmp_path):
    assert _push_new_repo(tmp_path, "one")[0] == "pass"
    decision, _reason = _push_new_repo(tmp_path, "two", FAKE_VERSION="kimchi 9.9.9",
                                       FAKE_CANARY_LEAK="1")
    assert decision == "deny"
    assert len(_canaries(tmp_path)) == 2


def _age_every_entry(seconds):
    for path in _cache_files():
        data = json.loads(path.read_text(encoding="utf-8"))
        data["passed_at"] -= seconds
        path.write_text(json.dumps(data), encoding="utf-8")


def test_an_entry_older_than_seven_days_reruns_the_canary(tmp_path):
    assert _push_new_repo(tmp_path, "one")[0] == "pass"
    _age_every_entry(8 * 86400)
    assert _push_new_repo(tmp_path, "two")[0] == "pass"
    assert len(_canaries(tmp_path)) == 2
    # ...and the re-run refreshed the entry.
    assert _push_new_repo(tmp_path, "three")[0] == "pass"
    assert len(_canaries(tmp_path)) == 2


def test_the_lifetime_comes_from_the_ttl_setting(tmp_path):
    _write_settings({"behavior": {"canary_cache": {"ttl_days": 1}}})
    assert _push_new_repo(tmp_path, "one")[0] == "pass"
    _age_every_entry(2 * 86400)
    assert _push_new_repo(tmp_path, "two")[0] == "pass"
    assert len(_canaries(tmp_path)) == 2


def test_a_corrupt_cache_file_is_a_miss(tmp_path):
    assert _push_new_repo(tmp_path, "one")[0] == "pass"
    for path in _cache_files():
        path.write_text('{"v": 1, "passed": tr', encoding="utf-8")      # partial write
    assert _push_new_repo(tmp_path, "two")[0] == "pass"
    assert len(_canaries(tmp_path)) == 2
    assert json.loads(_cache_files()[0].read_text(encoding="utf-8"))["passed"] is True


def test_a_future_dated_entry_is_a_miss(tmp_path):
    assert _push_new_repo(tmp_path, "one")[0] == "pass"
    _age_every_entry(-30 * 86400)
    assert _push_new_repo(tmp_path, "two")[0] == "pass"
    assert len(_canaries(tmp_path)) == 2


def test_the_setting_switches_the_cache_off(tmp_path):
    _write_settings({"behavior": {"canary_cache": {"enabled": False}}})
    assert _push_new_repo(tmp_path, "one")[0] == "pass"
    assert _push_new_repo(tmp_path, "two")[0] == "pass"
    assert len(_canaries(tmp_path)) == 2
    assert _cache_files() == []


def test_the_environment_switches_the_cache_off(tmp_path):
    assert _push_new_repo(tmp_path, "one", OCR_CANARY_CACHE="0")[0] == "pass"
    assert _push_new_repo(tmp_path, "two", OCR_CANARY_CACHE="0")[0] == "pass"
    assert len(_canaries(tmp_path)) == 2
    assert _cache_files() == []


def test_the_environment_wins_over_the_setting(tmp_path):
    _write_settings({"behavior": {"canary_cache": {"enabled": False}}})
    assert _push_new_repo(tmp_path, "one", OCR_CANARY_CACHE="1")[0] == "pass"
    assert _push_new_repo(tmp_path, "two", OCR_CANARY_CACHE="1")[0] == "pass"
    assert len(_canaries(tmp_path)) == 1


# --- the extension hash, in process --------------------------------------------------

@pytest.fixture
def inproc(tmp_path, monkeypatch):
    """The cache functions with the canary probe counted, a fake CLI whose
    `--version` is controllable and a temp deny extension."""
    ext = tmp_path / "deny.ts"
    ext.write_text("export default 1;\n", encoding="utf-8")
    monkeypatch.setattr(rg, "_DENY_EXT", str(ext))
    shim = fake_backend.install(tmp_path / "shim", "kimchi")
    monkeypatch.delenv("OCR_CANARY_CACHE", raising=False)
    monkeypatch.delenv("FAKE_VERSION", raising=False)
    probes = []
    monkeypatch.setattr(rg, "_canary_probe", lambda *a: probes.append(a))
    env = dict(os.environ)

    def run(model=FAST):
        rg._canary_checked(shim, ["--model", model], env, model)

    return run, probes, ext


def test_a_changed_extension_reruns_the_canary(inproc):
    run, probes, ext = inproc
    run()
    run()
    assert len(probes) == 1
    ext.write_text("export default 2;\n", encoding="utf-8")
    run()
    assert len(probes) == 2
    run()
    assert len(probes) == 2


def test_an_unreadable_extension_is_never_cached(inproc):
    run, probes, ext = inproc
    ext.unlink()
    run()
    run()
    assert len(probes) == 2
    assert _cache_files() == []


def test_a_failing_probe_leaves_nothing_cached(inproc, monkeypatch):
    run, probes, _ext = inproc

    def boom(*a):
        raise rg.ReviewGateError("sandbox did not engage")

    monkeypatch.setattr(rg, "_canary_probe", boom)
    with pytest.raises(rg.ReviewGateError):
        run()
    assert _cache_files() == []


def test_the_defaults_are_on_for_seven_days():
    cfg = rg_settings.load(None)
    assert cfg.behavior("canary_cache.enabled").value is True
    assert cfg.behavior("canary_cache.ttl_days").value == 7
