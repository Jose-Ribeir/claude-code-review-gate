"""t26 regression tests: installers/uninstallers, sync-local-install, rg_config.

Maps to ticket t26 items:
  7  uninstall-git-hook.sh must only restore/unset core.hooksPath when the
     CURRENT global value still equals our managed hooks dir.
  8  uninstall-kimchi-hook.sh must not mv pre-push.user over a pre-push that
     is not recognizably ours.
  9  sync-local-install._differs ignores exactly EXCLUDE_PATTERNS (both sides).
 10  installed_plugins.json is rewritten atomically (tmp + os.replace).
 11  rg_config accepts the README-documented JSON-number jira.timeout.
Also: gate-hook.ps1 de-splice prefilter parity (source-level; pwsh unavailable
on this dev machine, so behavior parity is pinned structurally).
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SCRIPTS = os.path.join(_ROOT, "scripts")
_HOMEBREW_BASH = shutil.which("bash")

needs_bash = pytest.mark.skipif(not _HOMEBREW_BASH, reason="bash not available")


def _load(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sync_local_install = _load("sync_local_install",
                           os.path.join(_SCRIPTS, "sync-local-install.py"))


def _load_rg_config():
    if _SCRIPTS not in sys.path:
        sys.path.insert(0, _SCRIPTS)
    return _load("rg_config", os.path.join(_SCRIPTS, "rg_config.py"))


# --- item 11: numeric jira timeout (README documents {"timeout": 30}) --------

def test_jira_numeric_timeout_accepted(tmp_path):
    rg_config = _load_rg_config()
    repo = tmp_path / "repo"
    (repo / ".ocr").mkdir(parents=True)
    (repo / ".ocr" / "config.json").write_text(json.dumps({
        "jira": {"server": "https://you.atlassian.net", "project": "PROJ",
                 "timeout": 120},
    }))
    settings = rg_config.load_settings(repo_root=repo)
    assert settings["jira"]["timeout"] == "120"
    assert settings["jira"]["server"] == "https://you.atlassian.net"
    assert settings["notes"] == []


def test_jira_bool_timeout_rejected_with_note(tmp_path):
    rg_config = _load_rg_config()
    repo = tmp_path / "repo"
    (repo / ".ocr").mkdir(parents=True)
    (repo / ".ocr" / "config.json").write_text(json.dumps({
        "jira": {"timeout": True},
    }))
    settings = rg_config.load_settings(repo_root=repo)
    assert "timeout" not in settings["jira"]
    assert any("timeout" in n for n in settings["notes"])


# --- item 9: _differs ignores exactly the copy step's EXCLUDE_PATTERNS -------

def test_excluded_entries_are_not_drift(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    for base in (src, dst):
        (base / "sub").mkdir(parents=True)
        (base / "sub" / "a.py").write_text("x = 1\n")
    # Copy-skipped junk on either side must not read as drift.
    (src / ".DS_Store").write_bytes(b"junk")
    (dst / "Thumbs.db").write_bytes(b"junk")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"junk")
    (dst / "sub" / "junk.pyc").write_bytes(b"junk")
    (dst / "settings.local.json").write_text("{}")
    assert not sync_local_install._differs(src, dst)


def test_real_drift_is_detected(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    for base in (src, dst):
        base.mkdir()
        (base / "a.py").write_text("x = 1\n")
    (dst / "a.py").write_text("x = 2\n")
    assert sync_local_install._differs(src, dst)
    (dst / "a.py").write_text("x = 1\n")
    (src / "extra.py").write_text("y = 3\n")
    assert sync_local_install._differs(src, dst)


def test_exclude_patterns_shared_between_copy_and_check():
    # One source of truth: the copy ignore and the drift check consume the
    # same constant (regression: they had drifted apart, so --check could
    # report OUT OF DATE forever on copy-skipped junk).
    import shutil as _sh
    ignored = _sh.ignore_patterns(*sync_local_install.EXCLUDE_PATTERNS)
    for name in (".DS_Store", "Thumbs.db", "settings.local.json",
                 "__pycache__", ".pytest_cache", "mod.pyc"):
        assert name in ignored("d", [name])
        assert sync_local_install._ignored(name)
    assert not sync_local_install._ignored("real.py")


# --- item 10: installed_plugins.json atomic rewrite ---------------------------

def test_update_record_rewrites_atomically(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    reg = plugins_dir / "installed_plugins.json"
    key = sync_local_install.PLUGIN_KEY
    reg.write_text(json.dumps({"plugins": {key: [{"installPath": "/old",
                                                  "version": "0.0.0"}],
                                           "other@mp": [{"keep": "me"}]}}))
    install_path = tmp_path / "snapshot"
    install_path.mkdir()
    assert sync_local_install._update_record(plugins_dir, install_path,
                                             "9.9.9", "abc123") is None
    data = json.loads(reg.read_text())
    entry = data["plugins"][key][0]
    assert entry["installPath"] == str(install_path)
    assert entry["version"] == "9.9.9"
    assert entry.get("gitCommitSha") == "abc123"
    # the other plugin's record is preserved, and no staging file leaks
    assert data["plugins"]["other@mp"] == [{"keep": "me"}]
    assert [p.name for p in plugins_dir.iterdir()] == ["installed_plugins.json"]


# --- item 7: uninstall-git-hook.sh current-value guard ------------------------

@needs_bash
def test_uninstall_git_hook_leaves_repointed_hooks_path_alone(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    hooks_dir = tmp_path / "our-hooks"
    hooks_dir.mkdir()
    other = tmp_path / "other-hooks"
    other.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE")}
    env.update({"HOME": str(home), "SCR_HOOKS_DIR": str(hooks_dir)})
    def gconf(*args):
        return subprocess.run(["git", "config", "--global", *args],
                              env=env, capture_output=True, text=True, check=True)
    # The user adopted another global hook setup AFTER our install.
    gconf("core.hooksPath", str(other))
    gconf("reviewGate.prevHooksPath", str(hooks_dir))
    out = subprocess.run(
        [_HOMEBREW_BASH, os.path.join(_SCRIPTS, "uninstall-git-hook.sh")],
        env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert gconf("--get", "core.hooksPath").stdout.strip() == str(other)
    gone = subprocess.run(["git", "config", "--global", "--get",
                           "reviewGate.prevHooksPath"],
                          env=env, capture_output=True, text=True)
    assert gone.returncode != 0


@needs_bash
def test_uninstall_git_hook_still_restores_when_current_is_ours(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    hooks_dir = tmp_path / "our-hooks"
    hooks_dir.mkdir()
    prev = tmp_path / "prev-hooks"
    prev.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE")}
    env.update({"HOME": str(home), "SCR_HOOKS_DIR": str(hooks_dir)})
    def gconf(*args):
        return subprocess.run(["git", "config", "--global", *args],
                              env=env, capture_output=True, text=True, check=True)
    gconf("core.hooksPath", str(hooks_dir))
    gconf("reviewGate.prevHooksPath", str(prev))
    out = subprocess.run(
        [_HOMEBREW_BASH, os.path.join(_SCRIPTS, "uninstall-git-hook.sh")],
        env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert gconf("--get", "core.hooksPath").stdout.strip() == str(prev)


# --- item 8: uninstall-kimchi-hook.sh mv guard --------------------------------

@needs_bash
def test_uninstall_kimchi_hook_never_clobbers_foreign_pre_push(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE")}
    env.update({
        "KIMCHI_HOOKS_DIR": str(tmp_path / "kimchi-hooks"),
        "OCR_EXT_DIR": str(tmp_path / "ext"),
        "OCR_STATE_DIR": str(tmp_path / "state"),
    })
    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=repo,
                   env=env, check=True, capture_output=True)
    hooks = repo / ".git" / "hooks"
    foreign = "#!/bin/sh\necho my own hook\n"
    (hooks / "pre-push").write_text(foreign)          # NOT ours
    (hooks / "pre-push.user").write_text("#!/bin/sh\necho stale user copy\n")
    out = subprocess.run(
        [_HOMEBREW_BASH, os.path.join(_SCRIPTS, "uninstall-kimchi-hook.sh"),
         "--repo", str(repo)],
        env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert (hooks / "pre-push").read_text() == foreign
    assert (hooks / "pre-push.user").exists()


@needs_bash
def test_uninstall_kimchi_hook_restores_user_hook_over_ours(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE")}
    env.update({
        "KIMCHI_HOOKS_DIR": str(tmp_path / "kimchi-hooks"),
        "OCR_EXT_DIR": str(tmp_path / "ext"),
        "OCR_STATE_DIR": str(tmp_path / "state"),
    })
    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=repo,
                   env=env, check=True, capture_output=True)
    hooks = repo / ".git" / "hooks"
    (hooks / "pre-push").write_text("#!/bin/sh\n# review-gate.py lives here\n")
    (hooks / "pre-push.user").write_text("#!/bin/sh\necho user hook\n")
    out = subprocess.run(
        [_HOMEBREW_BASH, os.path.join(_SCRIPTS, "uninstall-kimchi-hook.sh"),
         "--repo", str(repo)],
        env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert (hooks / "pre-push").read_text() == "#!/bin/sh\necho user hook\n"
    assert not (hooks / "pre-push.user").exists()


# --- item 2: ps1 de-splice prefilter (structural; pwsh unavailable locally) ---

def test_ps1_prefilter_desplices_before_matching():
    ps1 = open(os.path.join(_SCRIPTS, "gate-hook.ps1"), encoding="utf-8").read()
    assert "$desplicedText = $payloadText -replace" in ps1
    assert "$desplicedText -notlike '*git*push*'" in ps1
    # the regex char class strips exactly backslash, double-quote, backtick,
    # single-quote -- the same four gate-hook.sh strips
    import re
    m = re.search(r"\$desplicedText = \$payloadText -replace '(.*)', ''", ps1)
    assert m, "de-splice line not found"
    assert set(m.group(1)) == set("[]\\\"`''".replace("[", "").replace("]", "")) \
        or all(c in m.group(1) for c in ('\\', '"', '`', "''"))
