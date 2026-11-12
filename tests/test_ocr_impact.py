"""Regression tests for cycle3 findings against scripts/ocr_impact.py (t25):

1. git_runner must scrub GIT_TRACE* env vars exactly like ocr_janitor /
   ocr_forge (a git inheriting them dies with exit 128 — see conftest.py).
2. git grep rc>1 (real error) must surface in the warnings list, not be
   silently degraded to a clean empty result like rc==1 (no match).
3. PowerShell Verb-Noun names (Get-Foo, Invoke-Build) must survive
   _changed_symbols_inner and reach the find_references grep.
"""
import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)

import ocr_impact as oi  # noqa: E402


def _stat_out(stdout="", rc=0):
    class _R:
        pass
    r = _R()
    r.stdout = stdout
    r.returncode = rc
    return r


def _stub_run(out="", rc=0, capture=None):
    def run(args):
        if capture is not None:
            capture.append(list(args))
        return out, rc
    return run


def _symbol(name, defined_in="pkg/mod.py"):
    return {
        "name": name, "qualname": name, "kind": "function",
        "change": "body", "defined_in": defined_in, "line": 5,
        "old_signature": "def %s():" % name, "new_signature": "def %s():" % name,
        "renamed_to": "", "lines_changed": 1,
    }


# ---------------------------------------------------------------------------
# item 1: git_runner scrubs GIT_TRACE* like its siblings
# ---------------------------------------------------------------------------

def test_git_runner_scrubs_git_trace_env(monkeypatch):
    monkeypatch.setenv("GIT_TRACE", "/tmp/dead-trace")
    monkeypatch.setenv("GIT_TRACE2_EVENT", "/tmp/dead-event")
    monkeypatch.setenv("GIT_SSH_COMMAND", "true")
    captured = {}

    def fake_run(cmd, **kw):
        captured["env"] = kw["env"]
        return _stat_out("", 0)

    monkeypatch.setattr(oi.subprocess, "run", fake_run)
    run = oi.git_runner("/tmp")
    out, rc = run(["status"])
    assert rc == 0 and out == ""
    leaked = [k for k in captured["env"] if k.startswith("GIT_TRACE")]
    assert leaked == []
    # unrelated git config survives the scrub
    assert captured["env"]["GIT_SSH_COMMAND"] == "true"
    # pathspec-magic guard is kept
    assert captured["env"]["GIT_LITERAL_PATHSPECS"] == "1"
    # the caller's process env itself was not mutated
    assert os.environ.get("GIT_TRACE") == "/tmp/dead-trace"


def test_scrub_policy_aligned_across_runners(monkeypatch):
    """impact, forge and janitor must apply the IDENTICAL GIT_TRACE* policy."""
    import ocr_forge
    import ocr_janitor

    env = {"GIT_TRACE_REFS": "a", "GIT_TRACE2_EVENT": "b",
           "GIT_CONFIG_COUNT": "0", "PATH": "/usr/bin"}
    janitor_kept = set(ocr_janitor._scrub_env(env))
    assert janitor_kept == {"GIT_CONFIG_COUNT", "PATH"}

    monkeypatch.setenv("GIT_TRACE_REFS", "a")
    monkeypatch.setenv("GIT_TRACE2_EVENT", "b")
    captured = []

    def fake_run(cmd, **kw):
        captured.append(kw["env"])
        return _stat_out("", 0)

    monkeypatch.setattr(oi.subprocess, "run", fake_run)
    monkeypatch.setattr(ocr_forge.subprocess, "run", fake_run)
    oi.git_runner("/tmp")(["status"])
    ocr_forge._git_runner("/tmp")(["status"])
    impact_env, forge_env = captured
    for e in (impact_env, forge_env):
        assert not [k for k in e if k.startswith("GIT_TRACE")]
        assert e["GIT_LITERAL_PATHSPECS"] == "1"
    assert set(impact_env) == set(forge_env)


# ---------------------------------------------------------------------------
# item 2: rc>1 (git failure) is not "no match" — it must warn
# ---------------------------------------------------------------------------

def test_find_references_git_failure_surfaces_warning():
    out = oi.find_references(_stub_run("", 128), "HEAD", [_symbol("helper_fn")])
    assert out["sites"] == []
    assert out["warnings"], \
        "git failure (rc=128) must produce a non-empty warnings list"


def test_find_references_no_match_rc1_is_quiet():
    out = oi.find_references(_stub_run("", 1), "HEAD", [_symbol("helper_fn")])
    assert out["sites"] == []
    assert out["warnings"] == []


def test_find_siblings_git_failure_surfaces_warning():
    finding = {"id": "f1", "path": "a.py", "start_line": 1, "end_line": 3,
               "severity": "medium",
               "existing_code": 'user = db.execute(f"SELECT * FROM users WHERE id={uid}")'}
    out = oi.find_siblings(_stub_run("", 128), "HEAD", finding, push_paths={"a.py"})
    assert out["siblings"] == []
    assert out["warnings"], \
        "git failure must warn, not look like a clean no-siblings result"


def test_find_siblings_no_match_rc1_is_quiet():
    finding = {"id": "f1", "path": "a.py", "start_line": 1, "end_line": 3,
               "severity": "medium",
               "existing_code": 'user = db.execute(f"SELECT * FROM users WHERE id={uid}")'}
    out = oi.find_siblings(_stub_run("", 1), "HEAD", finding, push_paths={"a.py"})
    assert out["siblings"] == []
    assert out["warnings"] == []


# ---------------------------------------------------------------------------
# item 3 (and 4): PowerShell dashed names survive to the impact analysis
# ---------------------------------------------------------------------------

_PS1_OLD = (
    "function Get-Foo {\n"
    "    param($x)\n"
    "    Write-Output ($x + 1)\n"
    "}\n"
    "function Invoke-Build {\n"
    "    Write-Output 'one'\n"
    "}\n"
)
_PS1_NEW = (
    "function Get-Foo {\n"
    "    param($x)\n"
    "    Write-Output ($x + 2)\n"
    "}\n"
    "function Invoke-Build {\n"
    "    param($target)\n"
    "    Write-Output 'one'\n"
    "}\n"
)


def test_powershell_dashed_names_survive_changed_symbols():
    out = oi.changed_symbols("build.ps1", _PS1_OLD, _PS1_NEW)
    assert out["supported"]
    by_name = {s["name"]: s for s in out["symbols"]}
    assert "Get-Foo" in by_name
    assert "Invoke-Build" in by_name
    assert by_name["Get-Foo"]["change"] == "body"
    # Adding a param is a body change in PowerShell: the sig line is the
    # `function Name {` header and is unchanged in both texts.
    assert by_name["Invoke-Build"]["change"] == "body"


def test_powershell_dashed_names_passed_to_git_grep():
    captured = []
    syms = oi.changed_symbols("build.ps1", _PS1_OLD, _PS1_NEW)["symbols"]
    assert any(s["name"] == "Get-Foo" for s in syms)
    oi.find_references(_stub_run("", 1, capture=captured), "HEAD", syms)
    grep_names = {args[i + 1] for args in captured if "grep" in args
                  for i, a in enumerate(args) if a == "-e"}
    assert "Get-Foo" in grep_names
    assert "Invoke-Build" in grep_names


def test_non_powershell_dashed_names_still_rejected():
    # The powershell exception must not leak into other languages.
    text = "const myFn = (a) => a + 1;\n"
    out = oi.changed_symbols("mod.js", text, text.replace("+ 1", "- 1"))
    assert out["supported"]
    for s in out["symbols"]:
        assert "-" not in s["name"]
