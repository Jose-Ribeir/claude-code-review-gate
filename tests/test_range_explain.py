"""Explaining the review range: which base a push was reviewed against, and WHY.

`--print-range` takes the path a real push takes (current branch, merge-request
lookup, target-branch freshness) and prints the base, the rule that chose it,
the commit and changed-line counts, the forge command it ran and its result.
It reviews nothing and writes nothing. A real hook push records the same facts
in its state file and in the debug log.

Real git (bare origin + clone), a stub `glab`/`gh` on a private PATH, the real
gate. Only the forge URL is pinned: `git remote get-url` expands insteadOf, so
the GitLab identity the lookup sees is fed through rg_store.remote_url while
git itself still talks to the local bare repo.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")

sys.path.insert(0, _SCRIPTS)
import rg_mr_consult  # noqa: E402
import rg_store  # noqa: E402
from conftest import _git_sh, minimal_path, posix_path, write_fake_cli  # noqa: E402
from test_pg5_orchestrator import (  # noqa: E402,F401
    _env, _feat_tip, _git, _git_mode, _github_ify, _line, _mr, _serve, _trace,
    _write_gh_shim, repo)

_spec = importlib.util.spec_from_file_location("review_gate_range_explain", _GATE)
rg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rg)

GL_URL = "https://gitlab.com/acme/proj.git"


# --- a stub glab ----------------------------------------------------------------

def _shim_path(*front):
    """minimal_path plus, on Windows, the dir of git's sh.exe: the sh shims call
    cat/sleep, which git's own dir lacks (that dir holds no forge or reviewer CLI)."""
    sh = _git_sh() if sys.platform == "win32" else None
    return minimal_path(*front, *([os.path.dirname(sh)] if sh else []))


def _glab(tmp_path):
    """A fake `glab` on its own PATH dir. Returns (bindir, canned): write
    canned/out (stdout), canned/rc (exit code), canned/sleep (seconds)."""
    bindir = tmp_path / "bin-glab"
    canned = tmp_path / "canned-glab"
    canned.mkdir()
    body = "\n".join([
        "#!/bin/sh",
        'dir="%s"' % posix_path(canned),
        'echo "glab $*" >> "$dir/calls.log"',
        '[ -f "$dir/sleep" ] && sleep "$(cat "$dir/sleep")"',
        "rc=0",
        '[ -f "$dir/rc" ] && rc=$(cat "$dir/rc")',
        'if [ "$rc" != "0" ]; then echo "glab: 401 Unauthorized" >&2; exit "$rc"; fi',
        'cat "$dir/out"',
        "exit 0",
    ]) + "\n"
    write_fake_cli(bindir, "glab", body)
    return bindir, canned


def _glab_mr(tip, target="main", iid=7):
    return json.dumps([{"iid": iid, "title": "T", "description": "d", "labels": [],
                        "sha": tip, "target_branch": target,
                        "web_url": "https://gitlab.com/acme/proj/-/merge_requests/%d" % iid}])


@pytest.fixture
def gitlab(repo, tmp_path, monkeypatch):  # noqa: F811
    """The repo's origin is GitLab as far as the MR lookup can tell, the gate
    runs in-process, and PATH holds only git plus the stub glab."""
    bindir, canned = _glab(tmp_path)
    monkeypatch.setattr(rg_store, "remote_url", lambda root, name: GL_URL if name else "")
    monkeypatch.setenv("PATH", _shim_path(str(bindir)))
    monkeypatch.setenv("OCR_REVIEWER_CMD", "%s -c \"raise SystemExit(99)\"" % sys.executable)
    monkeypatch.delenv("OCR_BASE_V1", raising=False)
    (canned / "out").write_text("[]")
    return repo, canned


def _print_range(work, capsys):
    rc = rg._print_range(str(work))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


# --- print-range follows the path a push takes -----------------------------------

def test_print_range_with_an_open_mr_shows_the_mr_base_rule_and_counts(gitlab, capsys):
    work, canned = gitlab
    main = _git(["rev-parse", "HEAD"], cwd=work)
    tip = _feat_tip(work, ("f1.txt", "f2.txt"))
    (canned / "out").write_text(_glab_mr(tip))
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err
    assert out.strip() == "%s..%s" % (main, tip)          # stdout contract unchanged
    assert "rule:    mr-target" in err, err
    assert "open MR/PR target branch" in err, err            # the reason, not only the rule
    assert "commits: 2" in err and "changed lines: 2" in err, err
    assert "base:    " + main in err, err
    assert "MR target branch: main" in err, err
    assert "forge command: glab api projects/acme%2Fproj/merge_requests" in err, err
    assert "open MR/PR !7 for feat targets main" in err, err


def test_print_range_without_an_mr_names_the_fallback_rule(gitlab, capsys):
    work, canned = gitlab
    _feat_tip(work)
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err
    assert "rule:    boundary" in err and "commits: 1" in err, err
    assert "lists no open MR/PR for branch feat" in err, err
    assert "forge command: glab api" in err, err            # the command that was run


def test_a_fast_forward_says_the_forge_was_not_needed(gitlab, capsys):
    work, canned = gitlab
    _feat_tip(work)
    _git(["push", "-q", "-u", "origin", "feat"], cwd=work)
    _git(["commit", "-q", "--allow-empty", "-m", "more"], cwd=work)
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err
    assert "rule:    fast-forward" in err, err
    assert "MR lookup not needed" in err, err


# --- stale target ref ---------------------------------------------------------------

def _advance_remote_main(work, tmp_path):
    """Land a commit on the remote's main that `work` has not fetched."""
    remote = _git(["config", "remote.origin.url"], cwd=work)
    other = tmp_path / "other"
    _git(["clone", "-q", remote, str(other)], cwd=tmp_path)
    _git(["config", "user.email", "o@example.com"], cwd=other)
    _git(["config", "user.name", "o"], cwd=other)
    (other / "landed.txt").write_text("landed\n")
    _git(["add", "."], cwd=other)
    _git(["commit", "-q", "-m", "landed"], cwd=other)
    _git(["push", "-q", "origin", "main"], cwd=other)
    return _git(["rev-parse", "HEAD"], cwd=other)


def test_a_target_ref_behind_the_remote_is_warned_about(gitlab, tmp_path, capsys):
    work, canned = gitlab
    tip = _feat_tip(work)
    (canned / "out").write_text(_glab_mr(tip))
    remote_main = _advance_remote_main(work, tmp_path)
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err
    assert "STALE" in err and "git fetch origin main" in err, err
    assert remote_main[:8] in err, err


def test_a_current_target_ref_is_not_warned_about(gitlab, capsys):
    work, canned = gitlab
    tip = _feat_tip(work)
    (canned / "out").write_text(_glab_mr(tip))
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err
    assert "STALE" not in err, err
    assert "up to date with the remote" in err, err


# --- one reason line per skipped or failed lookup ------------------------------------

def _lookup_case(name, gitlab, monkeypatch, tmp_path):
    work, canned = gitlab
    if name == "no-cli":
        monkeypatch.setenv("PATH", minimal_path())
    elif name == "cli-failed":
        (canned / "rc").write_text("1")
    elif name == "timeout":
        (canned / "sleep").write_text("5")
        monkeypatch.setattr(rg_mr_consult, "MR_TIMEOUT", 1)
    elif name == "empty-list":
        (canned / "out").write_text("[]")
    elif name == "bad-json":
        (canned / "out").write_text("this is not json")
    elif name == "unrecognised-host":
        monkeypatch.setattr(rg_store, "remote_url",
                            lambda root, n: "https://code.example.org/acme/proj.git")


_EXPECTED = {
    "no-cli": "MR lookup skipped: glab not found on PATH",
    "cli-failed": "MR lookup failed: glab api",
    "timeout": "MR lookup failed: glab timed out after 1s",
    "empty-list": "the forge lists no open MR/PR for branch feat",
    "bad-json": "MR lookup failed: gitlab open-MR lookup returned unrecognised JSON",
    "unrecognised-host": "MR lookup skipped: remote 'https://code.example.org/acme/proj.git' "
                         "is neither GitHub nor GitLab",
}


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_each_lookup_skip_or_failure_prints_its_own_reason_line(
        name, gitlab, monkeypatch, tmp_path, capsys):
    work, _canned = gitlab
    _feat_tip(work)
    _lookup_case(name, gitlab, monkeypatch, tmp_path)
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err                                      # fail-open: still a range
    assert err.count(_EXPECTED[name]) == 1, err
    assert "rule:    boundary" in err, err                   # the fallback still answers
    if name == "cli-failed":
        assert "exited 1" in err and "401 Unauthorized" in err, err


# --- read-only --------------------------------------------------------------------------

def _snapshot(work):
    state = os.environ["OCR_STATE_DIR"], os.environ["CLAUDE_PLUGIN_DATA"]
    files = {}
    for root_dir in state:
        for dirpath, _dirs, names in os.walk(root_dir):
            for n in names:
                p = os.path.join(dirpath, n)
                files[p] = os.path.getmtime(p)
    return {
        "refs": _git(["for-each-ref"], cwd=work),
        "status": _git(["status", "--porcelain"], cwd=work),
        "git_dir": sorted(os.listdir(_git(["rev-parse", "--absolute-git-dir"], cwd=work))),
        "state": files,
    }


def test_print_range_changes_no_state_and_makes_no_review_call(gitlab, tmp_path, capsys, monkeypatch):
    work, canned = gitlab
    tip = _feat_tip(work)
    (canned / "out").write_text(_glab_mr(tip))
    trace = tmp_path / "reviewer.trace"
    monkeypatch.setenv("STUB_TRACE", str(trace))
    before = _snapshot(work)
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err
    assert "rule:    mr-target" in err, err
    assert _snapshot(work) == before
    assert not trace.exists(), "print-range called the reviewer"
    assert not (rg_store.store_root() / "mr-consult").exists(), "print-range wrote the lookup cache"


# --- the run record and the debug log of a real push ----------------------------------------

def test_a_real_push_records_the_reason_and_counts(repo, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("OCR_STATE_DIR", str(tmp_path / "state"))   # gate and test share one store
    bindir, canned = _write_gh_shim(tmp_path)
    main = _git(["rev-parse", "HEAD"], cwd=repo)
    tip = _feat_tip(repo, ("f1.txt", "f2.txt"))
    _github_ify(repo)
    _serve(canned, "list_head", _mr(tip))
    env = _env(tmp_path, PATH=_shim_path(str(bindir)))
    proc = _git_mode(repo, [_line(tip)], env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(_trace(tmp_path)) == 1

    st = rg._read_state(rg._state_path(rg._git_common_dir(str(repo)), tip, main))
    assert st is not None, "no state record under the MR-target base"
    assert st["base_rule"] == "mr-target"
    assert "open MR/PR target branch" in st["base_reason"]
    assert st["base_commits"] == 2 and st["base_lines"] == 2
    assert st["mr_lookup_status"] == "mr-found" and st["mr_target"] == "main"
    assert "gh pr list" in st["mr_lookup_command"]
    assert st["target_ref_state"] in ("fresh", "stale", "unknown")

    log = tmp_path / "state" / "data" / "review-gate-debug.log"
    deadline = time.monotonic() + 10
    while not log.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    text = log.read_text(encoding="utf-8")
    line = next((ln for ln in text.splitlines() if " range tip=" in ln or ln.startswith("range tip=")), "")
    assert "base_rule='mr-target'" in line and "base_commits=2" in line \
        and "base_lines=2" in line and "mr_lookup_status='mr-found'" in line, text


def test_a_real_push_without_an_mr_records_the_fallback(repo, tmp_path):  # noqa: F811
    """GitHub origin, no CLI: the push behaves as before and still records why."""
    tip = _feat_tip(repo)
    _github_ify(repo)
    proc = _git_mode(repo, [_line(tip)], _env(tmp_path, PATH=minimal_path()))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "no MR context" in proc.stdout + proc.stderr
    base = _git(["rev-parse", "main"], cwd=repo)
    st = rg._read_state(rg._state_path(rg._git_common_dir(str(repo)), tip, base))
    assert st["base_rule"] == "boundary" and st["base_commits"] == 1
    assert st["mr_lookup_status"] == "no-cli"
    assert "not found on PATH" in st["mr_lookup_line"]


def test_print_range_cli_keeps_stdout_a_bare_range(repo, tmp_path):  # noqa: F811
    """The skill reads stdout: it stays exactly `<base>..<tip>`; the explanation is stderr."""
    tip = _feat_tip(repo)
    env = _env(tmp_path, PATH=minimal_path())
    proc = subprocess.run([sys.executable, _GATE, "--print-range"], capture_output=True,
                          text=True, cwd=str(repo), env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    base = _git(["rev-parse", "main"], cwd=repo)
    assert proc.stdout.strip() == "%s..%s" % (base, tip)
    assert "rule:    " in proc.stderr and "changed lines: 1" in proc.stderr, proc.stderr


def test_base_v1_keeps_the_pre_0_13_range_and_never_asks_the_forge(gitlab, capsys, monkeypatch):
    work, canned = gitlab
    _feat_tip(work)
    monkeypatch.setenv("OCR_BASE_V1", "1")
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err
    base = _git(["rev-parse", "main"], cwd=work)
    assert out.strip().startswith(base + "..")
    assert not (canned / "calls.log").exists(), "OCR_BASE_V1 must not run the forge command"


def test_a_real_push_under_base_v1_still_passes(repo, tmp_path):  # noqa: F811
    tip = _feat_tip(repo)
    proc = _git_mode(repo, [_line(tip)], _env(tmp_path, PATH=minimal_path(), OCR_BASE_V1="1"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
