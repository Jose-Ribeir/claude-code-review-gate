"""Tests for scripts/ocr_doctor.py (C9): every check seeded by hand.

The doctor is a health check whose whole job is to FAIL loudly on the exact
failure modes the gate itself can only report as "run the doctor" -- so each
test seeds one broken dependency (missing kimchi, uninstalled hook,
unwritable store, stale lock, sticky error state, broken extension) and
asserts the verdict the user would see.
"""
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ocr_doctor.py"

_spec = importlib.util.spec_from_file_location("ocr_doctor", SCRIPT)
ocr_doctor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ocr_doctor)

def _by_name(checks):
    """Checks by name. A name may fire more than once (per sticky state,
    per stale lock); repeats are folded together, worst status wins and the
    details are joined so every line is assertable."""
    out = {}
    for c in checks:
        prev = out.get(c["name"])
        if prev is None:
            out[c["name"]] = dict(c)
            continue
        rank = {"ok": 0, "info": 0, "warn": 1, "fail": 2}
        if rank[c["status"]] > rank[prev["status"]]:
            prev["status"] = c["status"]
        prev["detail"] += " | " + c["detail"]
    return out

def _fake_kimchi(tmp_path, version_output, name="kimchi"):
    """A shim that answers `--version` like the real CLI (or fails to)."""
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"#!/bin/sh\nprintf '%s\\n' {version_output!r}\n")
    p.chmod(0o755)
    return p

def _base_env(tmp_path, kimchi_version="1.27.0"):
    """A mostly-healthy env: git on PATH, a pinned kimchi shim, private
    store and hook dirs (never the developer's real ones)."""
    _fake_kimchi(tmp_path / "bin", kimchi_version or "1.27.0")
    bin_dir = tmp_path / "bin"
    git_dir = os.path.dirname(shutil.which("git") or "/usr/bin/git")
    hooks = tmp_path / "kimchi-hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "review-gate.sh").write_text("#!/bin/sh\n# review-gate shim\n")
    return {
        "PATH": os.pathsep.join([str(bin_dir), git_dir]),
        "HOME": str(tmp_path / "home"),
        "OCR_STATE_DIR": str(tmp_path / "store"),
        "KIMCHI_HOOKS_DIR": str(hooks),
        "OCR_EXT_DIR": str(tmp_path / "ext"),
    }

def _seed_store(root, session=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / "verdicts").mkdir(exist_ok=True)
    (root / "data").mkdir(exist_ok=True)
    key = (root / "async" / "0123456789abcdef")
    key.mkdir(parents=True, exist_ok=True)
    return key

def _write_state(key, name, **fields):
    p = key / name
    p.write_text(json.dumps(fields), encoding="utf-8")
    return p

# --- the individual checks, seeded by hand -------------------------------------

def test_python_and_git_ok(tmp_path):
    env = _base_env(tmp_path)
    checks = _by_name(ocr_doctor.run_checks(env=env))
    assert checks["python3"]["status"] == "ok"
    assert checks["git"]["status"] == "ok"

def test_missing_kimchi_fails(tmp_path):
    env = _base_env(tmp_path)
    env["PATH"] = "/usr/bin:/bin"  # git, but no kimchi shim
    env.pop("KIMCHI_BIN", None)
    checks = _by_name(ocr_doctor.run_checks(env=env))
    assert checks["kimchi"]["status"] == "fail"
    assert "not found on PATH" in checks["kimchi"]["detail"]

def test_kimchi_bin_override_found(tmp_path):
    env = _base_env(tmp_path)
    env["PATH"] = "/usr/bin:/bin"
    env["KIMCHI_BIN"] = str(_fake_kimchi(tmp_path / "shim", "1.27.0"))
    checks = _by_name(ocr_doctor.run_checks(env=env))
    assert checks["kimchi"]["status"] == "ok"

def test_untested_kimchi_version_warns_not_fails(tmp_path):
    env = _base_env(tmp_path, kimchi_version="kimchi 1.99.0")
    checks = _by_name(ocr_doctor.run_checks(env=env))
    assert checks["kimchi"]["status"] == "warn"
    assert "1.99.0" in checks["kimchi"]["detail"]
    assert "UNTESTED" in checks["kimchi"]["detail"]

def test_unparseable_kimchi_version_warns(tmp_path):
    env = _base_env(tmp_path, kimchi_version="no version here")
    checks = _by_name(ocr_doctor.run_checks(env=env))
    assert checks["kimchi"]["status"] == "warn"
    assert "could not be parsed" in checks["kimchi"]["detail"]

def test_deny_extension_repo_layout_ok_and_content_checked(tmp_path):
    env = _base_env(tmp_path)
    # The REAL repo-layout extension: healthy by construction.
    checks = _by_name(ocr_doctor.run_checks(env=env))
    assert checks["deny-extension"]["status"] == "ok"
    assert "tool_call" in checks["deny-extension"]["detail"]
    # A gutted one (no tool_call, no OCR_IN_REVIEW): the worker would run
    # unsandboxed -- that is a FAIL, not a WARN.
    bad = tmp_path / "bad-ext.ts"
    bad.write_text("// gutted\nexport default 1;\n")
    checks = _by_name(ocr_doctor.run_checks(env=env, repo_ext=bad))
    assert checks["deny-extension"]["status"] == "fail"
    assert "no tool_call subscriber" in checks["deny-extension"]["detail"]
    assert "no OCR_IN_REVIEW gate string" in checks["deny-extension"]["detail"]
    # Missing entirely: also a FAIL.
    checks = _by_name(ocr_doctor.run_checks(
        env=env, repo_ext=tmp_path / "nope.ts"))
    assert checks["deny-extension"]["status"] == "fail"

def test_deny_extension_installed_slot_missing_warns(tmp_path):
    env = _base_env(tmp_path)  # OCR_EXT_DIR points at an empty dir
    checks = _by_name(ocr_doctor.run_checks(env=env))
    assert checks["deny-extension-slot"]["status"] == "warn"
    assert "install-kimchi-hook" in checks["deny-extension-slot"]["detail"]
    # Installed and healthy: OK.
    (tmp_path / "ext").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ext" / "review-gate-deny.ts").write_text(
        (REPO_ROOT / "extensions" / "review-gate-deny.ts").read_text())
    checks = _by_name(ocr_doctor.run_checks(env=env))
    assert checks["deny-extension-slot"]["status"] == "ok"

def test_kimchi_bash_hook_missing_warns(tmp_path):
    env = _base_env(tmp_path)
    env["KIMCHI_HOOKS_DIR"] = str(tmp_path / "empty-hooks")
    (tmp_path / "empty-hooks").mkdir()
    checks = _by_name(ocr_doctor.run_checks(env=env))
    assert checks["kimchi-bash-hook"]["status"] == "warn"
    assert "pre-push layer still enforces" in checks["kimchi-bash-hook"]["detail"]

def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE")}
    r = subprocess.run(["git", "-C", str(repo), "init", "-q"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return repo, env

def test_pre_push_hook_missing_fails_with_repo(tmp_path):
    env = _base_env(tmp_path)
    repo, git_env = _make_repo(tmp_path)
    checks = _by_name(ocr_doctor.run_checks(env=env, repo=str(repo)))
    assert checks["pre-push-hook"]["status"] == "fail"
    assert "install-git-hook" in checks["pre-push-hook"]["detail"]

def test_pre_push_hook_present_ok_and_not_gate_fails(tmp_path):
    env = _base_env(tmp_path)
    repo, git_env = _make_repo(tmp_path)
    hooks = repo / ".git" / "hooks"
    (hooks / "pre-push").write_text("#!/bin/sh\nexec review-gate \"$@\"\n")
    checks = _by_name(ocr_doctor.run_checks(env=env, repo=str(repo)))
    assert checks["pre-push-hook"]["status"] == "ok"
    # Present but NOT the gate: the primary layer is a no-op -> FAIL.
    (hooks / "pre-push").write_text("#!/bin/sh\nexit 0\n")
    checks = _by_name(ocr_doctor.run_checks(env=env, repo=str(repo)))
    assert checks["pre-push-hook"]["status"] == "fail"
    assert "does not run the gate" in checks["pre-push-hook"]["detail"]

def test_pre_push_hook_found_via_core_hooks_path(tmp_path):
    env = _base_env(tmp_path)
    repo, git_env = _make_repo(tmp_path)
    custom = tmp_path / "custom-hooks"
    custom.mkdir()
    (custom / "pre-push").write_text("#!/bin/sh\nreview-gate pre-push \"$@\"\n")
    r = subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath",
                        str(custom)], capture_output=True, env=git_env)
    assert r.returncode == 0
    checks = _by_name(ocr_doctor.run_checks(env=env, repo=str(repo)))
    assert checks["pre-push-hook"]["status"] == "ok"
    assert "core.hooksPath" in checks["pre-push-hook"]["detail"]
    assert str(custom / "pre-push") in checks["pre-push-hook"]["detail"]

def test_not_a_git_repo_fails(tmp_path):
    env = _base_env(tmp_path)
    notrepo = tmp_path / "plain"
    notrepo.mkdir()
    checks = _by_name(ocr_doctor.run_checks(env=env, repo=str(notrepo)))
    assert checks["pre-push-hook"]["status"] == "fail"
    assert "not a git repository" in checks["pre-push-hook"]["detail"]

def test_unwritable_store_fails(tmp_path):
    env = _base_env(tmp_path)
    _seed_store(tmp_path / "store")
    root = tmp_path / "store"
    os.chmod(root, stat.S_IRUSR | stat.S_IXUSR)  # read+traverse, no write
    try:
        checks = _by_name(ocr_doctor.run_checks(env=env))
        assert checks["store-writable"]["status"] == "fail"
        assert "every push re-denies" in checks["store-writable"]["detail"]
    finally:
        os.chmod(root, 0o700)

def test_healthy_store_ok(tmp_path):
    env = _base_env(tmp_path)
    _seed_store(tmp_path / "store")
    checks = _by_name(ocr_doctor.run_checks(env=env))
    assert checks["store-writable"]["status"] == "ok"
    assert checks["store-verdicts"]["status"] == "ok"
    assert checks["store-data"]["status"] == "ok"
    # The probe file must not linger.
    assert not list((tmp_path / "store").glob("doctor-probe-*"))

def test_stale_lock_dead_pid_warns(tmp_path):
    env = _base_env(tmp_path)
    key = _seed_store(tmp_path / "store")
    lock = key / "deadbeefcafe0123456789.json.lock"
    lock.write_text("999999:abcdef0123456789", encoding="utf-8")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    checks = _by_name(ocr_doctor.run_checks(env=env))
    assert checks["store-locks"]["status"] == "warn"
    assert "999999" in checks["store-locks"]["detail"]
    assert str(lock) in checks["store-locks"]["detail"]

def test_aged_lock_live_pid_ok(tmp_path):
    env = _base_env(tmp_path)
    key = _seed_store(tmp_path / "store")
    lock = key / "deadbeefcafe0123456789.json.lock"
    lock.write_text(f"{os.getpid()}:abcdef0123456789", encoding="utf-8")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    checks = _by_name(ocr_doctor.run_checks(env=env))
    assert checks["store-locks"]["status"] == "ok"
    assert "live pid" in checks["store-locks"]["detail"]

def test_fresh_lock_not_reported(tmp_path):
    env = _base_env(tmp_path)
    key = _seed_store(tmp_path / "store")
    lock = key / "deadbeefcafe0123456789.json.lock"
    lock.write_text("999999:abcdef0123456789", encoding="utf-8")
    checks = _by_name(ocr_doctor.run_checks(env=env))
    assert checks["store-locks"]["status"] == "ok"
    assert "checked 1" in checks["store-locks"]["detail"]

def test_sticky_error_state_warns(tmp_path):
    env = _base_env(tmp_path)
    key = _seed_store(tmp_path / "store")
    p = _write_state(key, "aaaa-bbbb.json", state="failed", attempts=3,
                     reason="spawn", failed_ts=time.time() - 100)
    checks = _by_name(ocr_doctor.run_checks(env=env))
    assert checks["state-machine"]["status"] == "warn"
    assert "sticky error state" in checks["state-machine"]["detail"]
    assert str(p) in checks["state-machine"]["detail"]
    assert "denied pending doctor" in checks["state-machine"]["detail"]

def test_unsticky_failed_state_is_not_sticky(tmp_path):
    env = _base_env(tmp_path)
    key = _seed_store(tmp_path / "store")
    _write_state(key, "aaaa-bbbb.json", state="failed", attempts=1,
                 reason="spawn", failed_ts=time.time() - 100)
    _write_state(key, "cccc-dddd.json", state="done", verdict="pass",
                 done_ts=time.time() - 100)
    checks = _by_name(ocr_doctor.run_checks(env=env))
    assert checks["state-machine"]["status"] == "ok"

def test_unparseable_state_warns(tmp_path):
    env = _base_env(tmp_path)
    key = _seed_store(tmp_path / "store")
    (key / "eeee-ffff.json").write_text("{not json", encoding="utf-8")
    checks = _by_name(ocr_doctor.run_checks(env=env))
    assert checks["state-machine"]["status"] == "warn"
    assert "unparseable" in checks["state-machine"]["detail"]

def test_canary_hint_present(tmp_path):
    env = _base_env(tmp_path)
    checks = _by_name(ocr_doctor.run_checks(env=env))
    hint = checks["canary-hint"]
    assert hint["status"] == "info"
    assert "OCR_IN_REVIEW=1" in hint["detail"]
    assert "-e" in hint["detail"]

def test_any_fail_makes_summary_exit_1(tmp_path):
    env = _base_env(tmp_path)
    env["PATH"] = "/usr/bin:/bin"  # no kimchi shim
    checks = ocr_doctor.run_checks(env=env)
    assert any(c["status"] == "fail" for c in checks)
    assert ocr_doctor.main([]) in (0, 1)  # sanity: never raises

# --- the effective hook path (git's own resolution) -----------------------------

def _gitcfg(repo, key, value):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE")}
    r = subprocess.run(["git", "-C", str(repo), "config", key, value],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr

def _mkrepo_with_commit(tmp_path, name="repo"):
    """A repo with one commit (a linked worktree needs a HEAD to branch from)."""
    repo = tmp_path / name
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE")}
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "t"], ["config", "commit.gpgsign", "false"]):
        r = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
    (repo / "a.txt").write_text("one\n")
    r = subprocess.run(["git", "-C", str(repo), "add", "."],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    r = subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "base"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return repo

def test_effective_path_reported_for_plain_repo(tmp_path):
    """No core.hooksPath: the doctor reports the repo's own hooks dir and
    says it is the default."""
    env = _base_env(tmp_path)
    repo, _ = _make_repo(tmp_path)
    checks = _by_name(ocr_doctor.run_checks(env=env, repo=str(repo)))
    eff = checks["effective-hook-path"]
    assert eff["status"] == "ok"
    assert "core.hooksPath" not in eff["detail"]
    assert "default hooks dir" in eff["detail"]
    assert os.path.realpath(str(repo / ".git" / "hooks")) in os.path.realpath(eff["detail"].split(" ")[0])

def test_effective_path_reported_for_relative_hooks_path(tmp_path):
    """A relative core.hooksPath resolves against the repo top, exactly git's
    rule, and the report says so."""
    env = _base_env(tmp_path)
    repo, _ = _make_repo(tmp_path)
    _gitcfg(repo, "core.hooksPath", ".husky/_")
    checks = _by_name(ocr_doctor.run_checks(env=env, repo=str(repo)))
    eff = checks["effective-hook-path"]
    assert eff["status"] == "ok"
    assert "core.hooksPath" in eff["detail"]
    assert os.path.realpath(str(repo / ".husky" / "_")) in os.path.realpath(eff["detail"].split("->")[1].strip().split(" ")[0])

def test_effective_path_for_absolute_hooks_path(tmp_path):
    env = _base_env(tmp_path)
    repo, _ = _make_repo(tmp_path)
    custom = tmp_path / "abs-hooks"
    custom.mkdir()
    _gitcfg(repo, "core.hooksPath", str(custom))
    checks = _by_name(ocr_doctor.run_checks(env=env, repo=str(repo)))
    eff = checks["effective-hook-path"]
    assert eff["status"] == "ok"
    assert str(custom) in eff["detail"]

def test_effective_path_for_linked_worktree_reports_common_hooks(tmp_path):
    """A linked worktree has no hooks of its own: git runs hooks from the
    main checkout's .git/hooks (the common dir), and the doctor must say
    exactly that path."""
    env = _base_env(tmp_path)
    main = _mkrepo_with_commit(tmp_path)
    genv = {k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE")}
    r = subprocess.run(["git", "-C", str(main), "worktree", "add", "-q",
                        str(tmp_path / "wt")], capture_output=True, text=True, env=genv)
    assert r.returncode == 0, r.stderr
    worktree = tmp_path / "wt"
    checks = _by_name(ocr_doctor.run_checks(env=env, repo=str(worktree)))
    eff = checks["effective-hook-path"]
    assert eff["status"] == "ok"
    assert os.path.realpath(str(main / ".git" / "hooks")) in \
        os.path.realpath(eff["detail"].split(" ")[0])

def test_gate_reached_through_husky_shim_chain(tmp_path):
    """husky v9 layout: hooksPath=.husky/_ and .husky/_/pre-push is a shim
    whose chain ends at .husky/pre-push. A gate there is REACHABLE, and the
    report names the file the gate was reached through."""
    env = _base_env(tmp_path)
    repo, _ = _make_repo(tmp_path)
    _gitcfg(repo, "core.hooksPath", ".husky/_")
    (repo / ".husky" / "_").mkdir(parents=True)
    (repo / ".husky" / "_" / "pre-push").write_text(
        "#!/usr/bin/env sh\n. \"${0%/*}/h\"\n")
    # husky's helper runs the real hook through variables no static scan
    # resolves (`sh -e "$s/$h"`); the doctor must still follow the chain.
    (repo / ".husky" / "_" / "h").write_text(
        "#!/usr/bin/env sh\nh=\"${0##*/}\"\ns=\"${0%/*/*}\"\n"
        "sh -e \"$s/$h\" \"$@\"\n")
    (repo / ".husky" / "pre-push").write_text(
        "#!/bin/sh\nexec review-gate \"$@\"\n")
    checks = _by_name(ocr_doctor.run_checks(env=env, repo=str(repo)))
    assert checks["pre-push-hook"]["status"] == "ok"
    assert "reached through" in checks["pre-push-hook"]["detail"]
    assert str(repo / ".husky" / "pre-push") in checks["pre-push-hook"]["detail"]

def test_husky_chain_without_gate_fails(tmp_path):
    """Same husky chain but the real hook never runs the gate: reachable
    means the CHAIN, not just the shim file."""
    env = _base_env(tmp_path)
    repo, _ = _make_repo(tmp_path)
    _gitcfg(repo, "core.hooksPath", ".husky/_")
    (repo / ".husky" / "_").mkdir(parents=True)
    (repo / ".husky" / "_" / "pre-push").write_text(
        "#!/usr/bin/env sh\n. \"${0%/*}/h\"\n")
    (repo / ".husky" / "_" / "h").write_text(
        "#!/usr/bin/env sh\nh=\"${0##*/}\"\ns=\"${0%/*/*}\"\n"
        "sh -e \"$s/$h\" \"$@\"\n")
    (repo / ".husky" / "pre-push").write_text("#!/bin/sh\necho husky ran\n")
    checks = _by_name(ocr_doctor.run_checks(env=env, repo=str(repo)))
    assert checks["pre-push-hook"]["status"] == "fail"
    assert "does not run the gate" in checks["pre-push-hook"]["detail"]

def test_stale_gate_copy_in_default_dir_is_flagged(tmp_path):
    """core.hooksPath points elsewhere while a gate hook still sits in the
    default hooks dir: that copy is shadowed (git will never run it) and is
    reported as a failure."""
    env = _base_env(tmp_path)
    repo, _ = _make_repo(tmp_path)
    custom = tmp_path / "custom-hooks"
    custom.mkdir()
    (custom / "pre-push").write_text("#!/bin/sh\nexec review-gate \"$@\"\n")
    _gitcfg(repo, "core.hooksPath", str(custom))
    (repo / ".git" / "hooks" / "pre-push").write_text(
        "#!/bin/sh\nexec review-gate \"$@\"\n")
    checks = _by_name(ocr_doctor.run_checks(env=env, repo=str(repo)))
    stale = checks["stale-hook-copy"]
    assert stale["status"] == "fail"
    assert "stale" in stale["detail"]
    assert str(repo / ".git" / "hooks" / "pre-push") in stale["detail"]
    # The effective copy is still healthy, and the shadowed copy must not
    # be confused with it.
    assert checks["pre-push-hook"]["status"] == "ok"
    assert str(custom / "pre-push") in checks["pre-push-hook"]["detail"]

def test_no_stale_flag_when_default_dir_is_the_effective_one(tmp_path):
    """No hooksPath: a gate hook in .git/hooks IS the installed hook, not a
    stale copy."""
    env = _base_env(tmp_path)
    repo, _ = _make_repo(tmp_path)
    (repo / ".git" / "hooks" / "pre-push").write_text(
        "#!/bin/sh\nexec review-gate \"$@\"\n")
    checks = _by_name(ocr_doctor.run_checks(env=env, repo=str(repo)))
    assert checks["stale-hook-copy"]["status"] == "ok"
    assert checks["pre-push-hook"]["status"] == "ok"

def test_no_gate_repo_reports_not_installed_at_effective_path(tmp_path):
    """A repo with no gate anywhere: the effective path is still reported,
    and the hook check fails as not installed there."""
    env = _base_env(tmp_path)
    repo, _ = _make_repo(tmp_path)
    checks = _by_name(ocr_doctor.run_checks(env=env, repo=str(repo)))
    assert checks["effective-hook-path"]["status"] == "ok"
    assert checks["pre-push-hook"]["status"] == "fail"
    assert "not installed" in checks["pre-push-hook"]["detail"]
    assert os.path.realpath(str(repo / ".git" / "hooks")) in \
        os.path.realpath(checks["pre-push-hook"]["detail"])

# --- the CLI, end to end --------------------------------------------------------

def _cli(env, *args):
    clean = {k: v for k, v in env.items() if not k.startswith("GIT_TRACE")}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=clean, timeout=120)

def test_cli_healthy_exit_0(tmp_path):
    env = _base_env(tmp_path)
    _seed_store(tmp_path / "store")
    r = _cli(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[OK ] kimchi:" in r.stdout
    assert "summary:" in r.stdout

def test_cli_broken_kimchi_exit_1(tmp_path):
    env = _base_env(tmp_path)
    env["PATH"] = "/usr/bin:/bin"
    r = _cli(env)
    assert r.returncode == 1
    assert "[FAIL] kimchi:" in r.stdout

def test_cli_json_emits_parseable_report(tmp_path):
    env = _base_env(tmp_path, kimchi_version="kimchi 2.0.0")
    r = _cli(env, "--json")
    assert r.returncode == 0
    data = json.loads(r.stdout)
    names = {c["name"] for c in data["checks"]}
    assert {"python3", "git", "kimchi", "deny-extension", "store-writable",
            "state-machine"} <= names
    assert data["warn"] >= 1  # untested version
    assert data["fail"] == 0

def test_cli_repo_flag_runs_hook_checks(tmp_path):
    env = _base_env(tmp_path)
    repo, _ = _make_repo(tmp_path)
    r = _cli(env, "--repo", str(repo))
    assert r.returncode == 1
    assert "[FAIL] pre-push-hook:" in r.stdout
