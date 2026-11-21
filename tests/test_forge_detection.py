"""Which forge a remote is: the setting, then the hosts glab/gh are logged in
to, then the host-name guess.

Through the real gate's git mode (`--print-range` path, in-process) with a stub
`glab` on a private PATH, and through the settings command and the doctor as a
user runs them (subprocesses). Only the forge URL is pinned for the gate: it is
fed through rg_store.remote_url so git itself never contacts the host.
"""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")

sys.path.insert(0, _SCRIPTS)
import rg_mr_consult  # noqa: E402
import rg_store  # noqa: E402
from conftest import minimal_path, posix_path, write_fake_cli  # noqa: E402
from test_pg5_orchestrator import _feat_tip, _git, repo  # noqa: E402,F401
from test_range_explain import _glab_mr, _shim_path  # noqa: E402

_spec = importlib.util.spec_from_file_location("review_gate_forge_detection", _GATE)
rg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rg)

PLAIN_URL = "https://git.corp.example/acme/proj.git"        # no "gitlab" in the name
GUESS_URL = "https://gitlab.corp.example/acme/proj.git"      # the name says gitlab
AUTH_OUT = ("git.corp.example\n  v Logged in to git.corp.example as dev (config)\n"
            "  v Git operations for git.corp.example configured to use ssh protocol.\n")


def _stub_glab(tmp_path):
    """A fake `glab`: `auth status` prints canned/auth (to stderr, as glab does)
    and exits with canned/auth_rc; anything else is an API call answered from
    canned/out. Every call is logged to canned/calls.log."""
    bindir = tmp_path / "bin-glab"
    canned = tmp_path / "canned-glab"
    canned.mkdir()
    body = "\n".join([
        "#!/bin/sh",
        'dir="%s"' % posix_path(canned),
        'echo "glab $*" >> "$dir/calls.log"',
        'if [ "$1 $2" = "auth status" ]; then',
        '  [ -f "$dir/auth" ] && cat "$dir/auth" >&2',
        '  rc=0; [ -f "$dir/auth_rc" ] && rc=$(cat "$dir/auth_rc")',
        '  exit "$rc"',
        "fi",
        'cat "$dir/out"',
        "exit 0",
    ]) + "\n"
    write_fake_cli(bindir, "glab", body)
    (canned / "out").write_text("[]")
    return bindir, canned


@pytest.fixture
def forge(repo, tmp_path, monkeypatch):  # noqa: F811
    """The gate runs in-process against the repo; origin's URL is whatever
    `forge.url` says; PATH holds git plus the stub glab only."""
    bindir, canned = _stub_glab(tmp_path)
    holder = {"url": PLAIN_URL}
    monkeypatch.setattr(rg_store, "remote_url", lambda root, name: holder["url"] if name else "")
    monkeypatch.setenv("PATH", _shim_path(str(bindir)))
    monkeypatch.setenv("OCR_REVIEWER_CMD", "%s -c \"raise SystemExit(99)\"" % sys.executable)
    monkeypatch.delenv("OCR_BASE_V1", raising=False)
    monkeypatch.chdir(repo)
    rg_mr_consult._login_cache.clear()
    holder["repo"], holder["canned"], holder["bindir"] = repo, canned, bindir
    yield holder
    rg_mr_consult._login_cache.clear()


def _settings(tmp_path, **forge_block):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"forge": forge_block}), encoding="utf-8")
    return path


def _print_range(work, capsys):
    rc = rg._print_range(str(work))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


# --- the gate's git mode ----------------------------------------------------------

def test_a_plain_named_host_is_recognised_when_glab_is_logged_in_to_it(forge, capsys):
    work, canned = forge["repo"], forge["canned"]
    main = _git(["rev-parse", "HEAD"], cwd=work)
    tip = _feat_tip(work)
    (canned / "auth").write_text(AUTH_OUT)
    (canned / "out").write_text(_glab_mr(tip))
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err
    assert out.strip() == "%s..%s" % (main, tip)
    assert "rule:    mr-target" in err and "open MR/PR !7 for feat targets main" in err, err
    assert "forge command: GITLAB_HOST=git.corp.example glab api" in err, err   # the right instance


def test_a_failing_glab_login_still_lists_the_healthy_hosts(forge, capsys):
    """glab exits non-zero when any configured host fails, yet lists the rest."""
    work, canned = forge["repo"], forge["canned"]
    tip = _feat_tip(work)
    (canned / "auth").write_text("gitlab.old.example\n  x gitlab.old.example: API call failed\n"
                                 + AUTH_OUT)
    (canned / "auth_rc").write_text("1")
    (canned / "out").write_text(_glab_mr(tip))
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err
    assert "open MR/PR !7 for feat targets main" in err, err


def test_a_plain_host_nobody_recognises_prints_the_reason_line(forge, capsys):
    work = forge["repo"]
    _feat_tip(work)
    (forge["canned"] / "auth").write_text("other.example\n  v Logged in to other.example as dev\n")
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err                                       # fail-open: still a range
    assert ("MR lookup skipped: remote %r is neither GitHub nor GitLab" % PLAIN_URL) in err, err
    assert "set forge.name" in err, err                      # and how to fix it
    assert "rule:    boundary" in err, err


def test_the_name_guess_still_works_with_no_setting_and_no_login(forge, capsys):
    work, canned = forge["repo"], forge["canned"]
    forge["url"] = GUESS_URL
    tip = _feat_tip(work)
    (canned / "out").write_text(_glab_mr(tip))                # glab has no `auth` output at all
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err
    assert "open MR/PR !7 for feat targets main" in err, err


def test_a_setting_recognises_a_host_nothing_else_does(forge, tmp_path, monkeypatch, capsys):
    work, canned = forge["repo"], forge["canned"]
    tip = _feat_tip(work)
    (canned / "out").write_text(_glab_mr(tip))                # glab is not logged in to anything
    monkeypatch.setenv("OCR_SETTINGS_FILE", str(_settings(tmp_path, name="gitlab")))
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err
    assert "open MR/PR !7 for feat targets main" in err, err


def test_a_setting_wins_over_the_name_guess(forge, tmp_path, monkeypatch, capsys):
    """The name says gitlab, the setting says github: gh is asked (and is not
    installed here), glab is never asked for the merge request."""
    work, canned = forge["repo"], forge["canned"]
    forge["url"] = GUESS_URL
    _feat_tip(work)
    monkeypatch.setenv("OCR_SETTINGS_FILE", str(_settings(tmp_path, name="github")))
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err
    assert "MR lookup skipped: gh not found on PATH" in err, err
    assert "glab" not in (canned / "calls.log").read_text() if (canned / "calls.log").exists() else True


def test_a_setting_wins_over_a_logged_in_host(forge, tmp_path, monkeypatch, capsys):
    work, canned = forge["repo"], forge["canned"]
    _feat_tip(work)
    (canned / "auth").write_text(AUTH_OUT)                    # glab is logged in to the host
    monkeypatch.setenv("OCR_SETTINGS_FILE", str(_settings(tmp_path, name="github")))
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err
    assert "MR lookup skipped: gh not found on PATH" in err, err
    assert "api projects" not in (canned / "calls.log").read_text() if (canned / "calls.log").exists() else True


def test_a_setting_for_another_host_does_not_apply(forge, tmp_path, monkeypatch, capsys):
    work, canned = forge["repo"], forge["canned"]
    tip = _feat_tip(work)
    (canned / "auth").write_text(AUTH_OUT)
    (canned / "out").write_text(_glab_mr(tip))
    monkeypatch.setenv("OCR_SETTINGS_FILE",
                       str(_settings(tmp_path, name="github", host="elsewhere.example")))
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err
    assert "open MR/PR !7 for feat targets main" in err, err


def test_a_project_setting_in_the_repo_is_read(forge, capsys):
    work, canned = forge["repo"], forge["canned"]
    tip = _feat_tip(work)
    (canned / "out").write_text(_glab_mr(tip))
    (work / ".ocr").mkdir(exist_ok=True)
    (work / ".ocr" / "settings.json").write_text(
        json.dumps({"forge": {"name": "gitlab", "host": "git.corp.example"}}), encoding="utf-8")
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err
    assert "open MR/PR !7 for feat targets main" in err, err


def test_print_range_says_which_forge_was_chosen_and_why(forge, capsys):
    work, canned = forge["repo"], forge["canned"]
    tip = _feat_tip(work)
    (canned / "auth").write_text(AUTH_OUT)
    (canned / "out").write_text(_glab_mr(tip))
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err
    assert "  forge: gitlab [logged-in host: glab is logged in to git.corp.example]" in err, err


def test_print_range_says_when_no_forge_is_recognised(forge, capsys):
    work = forge["repo"]
    _feat_tip(work)
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err
    assert "  forge: not recognised (git.corp.example is not a pinned host" in err, err


def _pin_forge_in_repo(work):
    (work / ".ocr").mkdir(exist_ok=True)
    (work / ".ocr" / "settings.json").write_text(
        json.dumps({"forge": {"name": "gitlab", "host": "git.corp.example"}}), encoding="utf-8")


def test_the_pushed_repos_setting_is_read_from_another_working_directory(
        forge, tmp_path, monkeypatch, capsys):
    """The gate is handed the repo root; the current directory (a git hook can
    run anywhere) must not decide which settings file is read."""
    work, canned = forge["repo"], forge["canned"]
    tip = _feat_tip(work)
    (canned / "out").write_text(_glab_mr(tip))
    _pin_forge_in_repo(work)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err
    assert "open MR/PR !7 for feat targets main" in err, err


def test_the_push_consult_reads_the_pushed_repos_setting_too(forge, tmp_path, monkeypatch):
    """_mr_consult (the pre-push consistency check) gets the same repo root."""
    work, canned = forge["repo"], forge["canned"]
    tip = _feat_tip(work)
    (canned / "out").write_text(_glab_mr(tip))
    _pin_forge_in_repo(work)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    ctx, _first = rg._mr_consult(PLAIN_URL, "feat", str(work))
    assert ctx is not None and ctx.iid == 7


def test_github_origin_behaves_as_before_without_asking_glab(forge, capsys):
    work, canned = forge["repo"], forge["canned"]
    forge["url"] = "https://github.com/acme/proj.git"
    _feat_tip(work)
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err
    assert "MR lookup skipped: gh not found on PATH" in err, err
    assert not (canned / "calls.log").exists(), "a github.com origin must not touch glab"


def test_a_hung_login_probe_is_bounded_and_fails_open(forge, monkeypatch, capsys):
    work = forge["repo"]
    _feat_tip(work)
    sleeper = forge["bindir"] / "glab.sh"
    sleeper.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(rg_mr_consult, "MR_TIMEOUT", 1)
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err
    assert "neither GitHub nor GitLab" in err, err


# --- the settings command and the doctor, as a user runs them -------------------------

def _env(tmp_path, bindir, settings=None):
    env = dict(os.environ)
    env["PATH"] = _shim_path(str(bindir))
    env["OCR_SETTINGS_FILE"] = str(settings or tmp_path / "no-such-settings.json")
    env.pop("OCR_BASE_V1", None)
    return env


def _run(script, args, env, cwd):
    return subprocess.run([sys.executable, os.path.join(_SCRIPTS, script)] + args,
                          capture_output=True, text=True, env=env, cwd=str(cwd), timeout=120)


@pytest.fixture
def cli_repo(repo, tmp_path):  # noqa: F811
    bindir, canned = _stub_glab(tmp_path)
    _git(["remote", "set-url", "origin", PLAIN_URL], cwd=repo)
    return repo, bindir, canned


def test_settings_show_names_the_logged_in_host_as_the_source(cli_repo, tmp_path):
    work, bindir, canned = cli_repo
    (canned / "auth").write_text(AUTH_OUT)
    res = _run("rg_settings_cli.py", ["show", "--repo", str(work)], _env(tmp_path, bindir), work)
    assert res.returncode == 0, res.stderr
    assert "effective forge for origin (%s): gitlab [logged-in host: glab is logged in to " \
           "git.corp.example]" % PLAIN_URL in res.stdout, res.stdout
    assert "forge.name" in res.stdout and "(not set: detected)" in res.stdout, res.stdout


def test_settings_show_names_the_setting_as_the_source(cli_repo, tmp_path):
    work, bindir, _canned = cli_repo
    env = _env(tmp_path, bindir)
    res = _run("rg_settings_cli.py", ["set", "forge.name", "gitlab"], env, work)
    assert res.returncode == 0, res.stderr
    res = _run("rg_settings_cli.py", ["show", "--repo", str(work)], env, work)
    assert res.returncode == 0, res.stderr
    assert "gitlab [setting: forge.name=gitlab in user:" in res.stdout, res.stdout
    assert "forge.name" in res.stdout and "= gitlab" in res.stdout, res.stdout


def test_settings_show_names_the_guess_as_the_source(cli_repo, tmp_path):
    work, bindir, _canned = cli_repo
    _git(["remote", "set-url", "origin", GUESS_URL], cwd=work)
    res = _run("rg_settings_cli.py", ["show", "--repo", str(work)], _env(tmp_path, bindir), work)
    assert res.returncode == 0, res.stderr
    assert "gitlab [name guess: gitlab.corp.example contains 'gitlab']" in res.stdout, res.stdout


def test_settings_show_says_when_the_host_is_not_recognised(cli_repo, tmp_path):
    work, bindir, _canned = cli_repo
    res = _run("rg_settings_cli.py", ["show", "--repo", str(work)], _env(tmp_path, bindir), work)
    assert res.returncode == 0, res.stderr
    assert "effective forge for origin (%s): not recognised" % PLAIN_URL in res.stdout, res.stdout


def test_an_invalid_forge_value_is_refused_with_a_clear_error(cli_repo, tmp_path):
    work, bindir, _canned = cli_repo
    env = _env(tmp_path, bindir)
    res = _run("rg_settings_cli.py", ["set", "forge.name", "gitlba"], env, work)
    assert res.returncode == 1
    assert "forge.name: must be one of github, gitlab, got 'gitlba'" in res.stderr, res.stderr
    assert not (tmp_path / "no-such-settings.json").exists(), "nothing is written for an invalid value"
    res = _run("rg_settings_cli.py", ["set", "forge.host", "https://git.example/x"], env, work)
    assert res.returncode == 1 and "forge.host: must be a host name" in res.stderr, res.stderr


def test_an_invalid_forge_value_in_a_file_is_reported_by_validate(cli_repo, tmp_path):
    work, bindir, _canned = cli_repo
    bad = tmp_path / "bad-settings.json"
    bad.write_text(json.dumps({"forge": {"name": "bitbucket", "colour": "red"}}), encoding="utf-8")
    res = _run("rg_settings_cli.py", ["validate", "--repo", str(work)], _env(tmp_path, bindir, bad), work)
    assert res.returncode == 1
    assert "forge.name: must be one of github, gitlab, got 'bitbucket'" in res.stderr, res.stderr
    assert "forge.colour: unknown key" in res.stderr, res.stderr


def test_the_doctor_shows_the_effective_forge_and_its_source(cli_repo, tmp_path):
    work, bindir, canned = cli_repo
    (canned / "auth").write_text(AUTH_OUT)
    res = _run("ocr_doctor.py", ["--repo", str(work)], _env(tmp_path, bindir), work)
    line = [ln for ln in res.stdout.splitlines() if "forge:" in ln and ln.startswith("[")]
    assert line, res.stdout + res.stderr
    assert "gitlab [logged-in host: glab is logged in to git.corp.example]" in line[0], line


def test_the_schema_accepts_the_forge_block():
    import rg_settings
    assert rg_settings.validate_document({"forge": {"name": "github", "host": "ghe.example:8443"}}) == []
    props = rg_settings.settings_schema()["properties"]["forge"]["properties"]
    assert props["name"]["enum"] == ["github", "gitlab"]


def test_minimal_path_still_has_no_forge_cli():
    """The suite's own premise: a forge CLI is only ever the stub."""
    import shutil
    assert shutil.which("glab", path=minimal_path()) is None


def test_the_large_diff_overlap_tier_reads_the_pushed_repos_setting_too(forge, tmp_path, monkeypatch):
    """cross_mr_path_overlap takes the pushed repo's root: its forge setting
    names the host, so the open-MR list is fetched from another directory too."""
    work, canned = forge["repo"], forge["canned"]
    _pin_forge_in_repo(work)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    calls = canned / "calls.log"

    def api_calls():
        lines = calls.read_text().splitlines() if calls.exists() else []
        return [ln for ln in lines if " auth " not in ln]

    # no repo root: the host is unrecognised from here, so the tier stays blind
    assert rg_mr_consult.cross_mr_path_overlap(PLAIN_URL, ["a.py"], exclude_iid=1) == {}
    assert api_calls() == []
    rg_mr_consult._login_cache.clear()
    rg_mr_consult.cross_mr_path_overlap(PLAIN_URL, ["a.py"], exclude_iid=1,
                                        repo_root=str(work))
    assert api_calls(), "the pushed repo's forge setting was not applied"
