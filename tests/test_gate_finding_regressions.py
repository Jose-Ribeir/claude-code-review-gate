"""Regression tests for pre-push review-gate findings in the ocr_* modules.

Each test names the finding it pins:
  1. ocr_forge._git_runner must scrub inherited GIT_TRACE* (exit-128 class).
  2. ocr_index.refresh must not mangle root-level dotfiles via lstrip('./').
  4. ocr_impact._sig must ignore comment/blank lines under an unchanged def.
  5. ocr_demands schema must mirror validate_demands for whitespace-only
     strings (both reject them).
  7. ocr_doctor.check_store must carry the stale-lock age computed in the
     guarded loop instead of re-stat()ing in the report loop.
  10. ocr_forge: an unsupported-host origin skips the forge tier; the
      'searched' list must not claim the skipped tier ran.
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import time

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)

import ocr_demands as od  # noqa: E402
import ocr_doctor  # noqa: E402
import ocr_forge  # noqa: E402
import ocr_impact  # noqa: E402
import ocr_index  # noqa: E402


def _git(args, cwd):
    """Git for test setup, always with GIT_TRACE* scrubbed (conftest.py)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE")}
    subprocess.run(["git"] + args, cwd=cwd, check=True,
                   capture_output=True, text=True, env=env)


# --------------------------------------------------------------------------
# Finding 1: forge's git runner must not inherit dangling GIT_TRACE*
# --------------------------------------------------------------------------

def test_forge_survives_dangling_git_trace_performance(tmp_path, monkeypatch):
    cwd = str(tmp_path)
    for cmd in (["init"], ["config", "user.email", "t@t"], ["config", "user.name", "T"],
                ["config", "commit.gpgsign", "false"]):
        _git(cmd, cwd)
    (tmp_path / "gen_reports.py").write_text('TARGET = "weekly-2026-11.md"\n',
                                             encoding="utf-8")
    _git(["add", "-A"], cwd)
    _git(["commit", "-m", "seed"], cwd)
    # Kimchi session artifacts: a GIT_TRACE_PERFORMANCE path under a temp dir
    # that has already vanished. Any git inheriting it dies with exit 128.
    ghost = os.path.join(cwd, "vanished-session-tmpdir", "trace.txt")
    monkeypatch.setenv("GIT_TRACE_PERFORMANCE", ghost)
    res = ocr_forge.make_pointer_resolver(cwd)("weekly-2026-11.md")
    assert res["status"] == "resolved", res
    assert res["via"] == "grep"
    assert "gen_reports.py" in [h["path"] for h in res["hits"]]


# --------------------------------------------------------------------------
# Finding 2: refresh() lstrip("./") mangled root-level dotfiles
# --------------------------------------------------------------------------

def test_refresh_root_dotfile_not_mangled(tmp_path):
    root = str(tmp_path)
    (tmp_path / ".hidden.py").write_text("def keep_me():\n    return 1\n",
                                         encoding="utf-8")
    ocr_index.ensure_built(root)
    out = ocr_index.refresh(root, ["./.hidden.py"])
    # Pre-fix: lstrip('./') turned '.hidden.py' into 'hidden.py', the probe
    # missed, and the entry was neither refreshed nor removed.
    assert out["refreshed"] == [".hidden.py"]


# --------------------------------------------------------------------------
# Finding 4: comment/blank line under an unchanged def is a body change
# --------------------------------------------------------------------------

def test_comment_under_unchanged_def_is_body_change():
    old = "def foo(x):\n    return x\n"
    for new in ("def foo(x):\n    # explain the guard\n    return x\n",
                "def foo(x):\n\n    return x\n"):
        r = ocr_impact.changed_symbols("mod.py", old, new)
        foo = next(s for s in r["symbols"] if s["name"] == "foo")
        assert foo["change"] == "body", foo


# --------------------------------------------------------------------------
# Finding 5: generated schema mirrors validate_demands on whitespace-only
# --------------------------------------------------------------------------

def test_schema_rejects_whitespace_only_string_demands():
    schema = od.DEMANDS_SCHEMA
    str_alt = schema["definitions"]["demandList"]["items"]["anyOf"][0]
    assert str_alt["type"] == "string" and "pattern" in str_alt
    assert re.search(str_alt["pattern"], "write tests")
    assert not re.search(str_alt["pattern"], "   ")
    bad = {"symbol_callers": ["   "], "event_couples": [], "doc_refs": [],
           "conventions": [], "prose_pointers": [],
           "risk": {"score": 0, "reasons": []}}
    assert od.validate_demands(json.dumps(bad))["demands"] is None


# --------------------------------------------------------------------------
# Finding 10: unsupported-host origins skip the forge tier; it must not be
# reported as searched
# --------------------------------------------------------------------------

def test_unsupported_host_skips_forge_without_claiming_it_searched(tmp_path):
    cwd = str(tmp_path)
    for cmd in (["init"], ["config", "user.email", "t@t"], ["config", "user.name", "T"],
                ["config", "commit.gpgsign", "false"]):
        _git(cmd, cwd)
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(["add", "-A"], cwd)
    _git(["commit", "-m", "seed"], cwd)
    _git(["remote", "add", "origin", "git@bitbucket.example.com:org/repo.git"], cwd)
    res = ocr_forge.make_pointer_resolver(cwd)("out/weekly-no-such-report.md")
    # Status stays 'unresolved' (an absence, not a degradation)...
    assert res["status"] == "unresolved", res
    assert res["via"] == "none"
    # ...but the skipped forge tier never ran, so it is not 'searched'.
    assert res["searched"] == ["registry", "grep", "branch"], res
    assert "neither GitHub nor GitLab" in res["warning"]


# --------------------------------------------------------------------------
# Finding 7: stale-lock report must not re-stat a vanished lock
# --------------------------------------------------------------------------

def test_check_store_stale_lock_age_computed_once(tmp_path, monkeypatch):
    key = tmp_path / "async" / "key1"
    key.mkdir(parents=True)
    (tmp_path / "verdicts").mkdir()  # readability checks stay quiet
    lock = key / "deadbeefcafe0123456789.json.lock"
    lock.write_text("999999:abcdef0123456789", encoding="utf-8")
    old = time.time() - 7200
    os.utime(lock, (old, old))
    real_stat = pathlib.Path.stat
    stats = {}

    def flaky_stat(self, *args, **kwargs):
        if self == lock:
            stats["n"] = stats.get("n", 0) + 1
            if stats["n"] > 1:
                raise FileNotFoundError("lock vanished before the report")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "stat", flaky_stat)
    checks = []
    ocr_doctor.check_store(lambda name, status, detail: checks.append((name, status, detail)),
                           env={}, root=str(tmp_path))
    by_name = {n: (s, d) for n, s, d in checks}
    assert by_name["store-locks"][0] == "warn"
    assert "s old" in by_name["store-locks"][1]
