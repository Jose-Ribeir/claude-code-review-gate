#!/usr/bin/env python3
"""review-gate doctor (port plan C9): one command that answers "is the gate
healthy on this machine?"

The gate is fail-closed by design (D9): anything it cannot verify becomes a
DENY, and 3 failed attempts leave a sticky error state whose only advice is
"run the doctor". This script is that doctor -- a pure-stdlib, Python 3.7+
health check over every dependency the gate has:

  - python3 >= 3.7, git, and the kimchi CLI (checked against TESTED_PIN;
    an untested kimchi version is a WARN, not a FAIL -- the gate still
    runs, it is just running on an unverified release),
  - the deny extension (repo layout + installed slot): nonempty text with
    the tool_call subscriber and the OCR_IN_REVIEW gate string,
  - hook health (with --repo): the pre-push primary gate at the
    core.hooksPath-resolved location, and the Kimchi bash UX hook,
  - the external state store (D12): writable, readable, no stale state
    locks, and how many tips are sitting in the sticky error state,
  - a reminder of how to run the read-only sandbox canary by hand.

Exit code is 1 when any check FAILs (the machine cannot gate pushes), 0
otherwise; WARNs alone never fail the run. `--json` emits the same checks
machine-readably.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Kimchi versions this gate has been verified against (port plan C9: drift
# detection). A version not on the list is a WARN, never a FAIL -- the gate
# still runs, on an unverified release.
TESTED_PIN = ["1.27.0"]

MIN_PY = (3, 7)
# Mirrors review-gate.py: a state lock older than this belongs to a process
# that died between claim and release.
LOCK_STALE_S = 60
# Mirrors review-gate.py: failed attempts before the sticky error state.
ATTEMPT_CAP = 3

GATE_ROOT = Path(__file__).resolve().parents[1]

_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")


def _scrub_env(env):
    """A copy of `env` with every GIT_TRACE* key removed.

    Kimchi Studio's git-attribution feature injects GIT_TRACE* vars pointing
    at per-session temp dirs that vanish; any git run inheriting them dies
    with exit 128 (see tests/conftest.py). The doctor's own git calls must
    be immune, or the doctor itself reports phantom failures.
    """
    return {k: v for k, v in env.items() if not k.startswith("GIT_TRACE")}


def _run(cmd, env, timeout=15, cwd=None):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, env=_scrub_env(env), cwd=cwd)


def _store_root(env):
    """The external state store root (D12). Mirrors rg_store.store_root."""
    return Path(env.get("OCR_STATE_DIR", "").strip()
                or os.path.join(os.path.expanduser("~"), ".local", "state", "review-gate"))


def _kimchi_bin(env):
    """The kimchi CLI the gate itself would use: PATH, then KIMCHI_BIN."""
    found = shutil.which("kimchi", path=env.get("PATH"))
    if found:
        return found
    exe = env.get("KIMCHI_BIN", "").strip()
    return exe if exe and os.path.isfile(exe) else None


def _pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, owned by someone else
    except OSError:
        return False
    return True


def _read_states(root):
    """Every async state record under the store: (path, dict-or-None).

    None marks a file that exists but will not parse -- itself a finding.
    Non-state JSON side files (manifests) carry no "state" key and are
    skipped, not reported.
    """
    out = []
    try:
        dirs = sorted((root / "async").iterdir())
    except OSError:
        return out
    for d in dirs:
        if not d.is_dir():
            continue
        try:
            paths = sorted(d.glob("*.json"))
        except OSError:
            continue
        for p in paths:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                out.append((p, None))
                continue
            if isinstance(data, dict) and "state" in data:
                out.append((p, data))
    return out


# --- the checks ----------------------------------------------------------------


def check_python(add, env):
    v = sys.version_info
    if (v.major, v.minor) >= MIN_PY:
        add("python3", "ok", f"{v.major}.{v.minor}.{v.micro} (>= {MIN_PY[0]}.{MIN_PY[1]})")
    else:
        add("python3", "fail",
            f"{v.major}.{v.minor}.{v.micro} is older than {MIN_PY[0]}.{MIN_PY[1]}")


def check_git(add, env):
    found = shutil.which("git", path=env.get("PATH"))
    if not found:
        add("git", "fail", "git not found on PATH -- the gate cannot resolve pushes")
        return
    try:
        out = _run([found, "--version"], env)
        ver = (out.stdout or out.stderr or "").strip().splitlines()[0] if (out.stdout or out.stderr) else ""
    except Exception as exc:
        add("git", "fail", f"git present at {found} but could not be run ({exc})")
        return
    add("git", "ok", f"{found} ({ver or 'version unknown'})")


def check_kimchi(add, env):
    bin_ = _kimchi_bin(env)
    if not bin_:
        add("kimchi", "fail",
            "`kimchi` CLI not found on PATH or KIMCHI_BIN -- without a reviewer the "
            "gate DENIES every push (fail-closed, D9)")
        return
    try:
        out = _run([bin_, "--version"], env)
    except Exception as exc:
        add("kimchi", "warn", f"found at {bin_} but `kimchi --version` failed to run ({exc})")
        return
    if out.returncode != 0:
        add("kimchi", "warn",
            f"`kimchi --version` exited {out.returncode}: "
            f"{(out.stderr or out.stdout or '').strip()[:200]}")
        return
    text = (out.stdout or "") + "\n" + (out.stderr or "")
    m = _VERSION_RE.search(text)
    if not m:
        add("kimchi", "warn",
            f"found at {bin_} but its version could not be parsed from "
            f"`kimchi --version` output: {text.strip()[:120]!r}")
        return
    version = m.group(0)
    if version in TESTED_PIN:
        add("kimchi", "ok", f"{bin_} version {version} (tested pin)")
    else:
        add("kimchi", "warn",
            f"{bin_} version {version} is UNTESTED (tested: {', '.join(TESTED_PIN)}); "
            "the gate runs but on an unverified release -- watch for fail-closed "
            "denies and report drift")


def _ext_problems(path):
    """Content findings for a deny-extension file, [] when healthy."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        return [f"unreadable as text ({exc})"]
    problems = []
    if not text.strip():
        problems.append("file is empty")
    if "tool_call" not in text:
        problems.append("no tool_call subscriber (the write-deny would never fire)")
    if "OCR_IN_REVIEW" not in text:
        problems.append("no OCR_IN_REVIEW gate string (the deny would fire outside reviews)")
    return problems


def check_deny_ext(add, env, repo_ext=None, installed_ext=None):
    repo_ext = Path(repo_ext) if repo_ext else GATE_ROOT / "extensions" / "review-gate-deny.ts"
    installed_ext = (Path(installed_ext) if installed_ext
                     else Path(env.get("OCR_EXT_DIR", "").strip()
                               or os.path.join(os.path.expanduser("~"), ".config",
                                               "kimchi", "harness", "extensions"))
                          / "review-gate-deny.ts")
    # The repo-layout copy is what the worker actually loads (`-e`), so a
    # broken one means an unsandboxed reviewer: FAIL.
    if not repo_ext.is_file():
        add("deny-extension", "fail", f"missing from the repo layout: {repo_ext}")
    else:
        problems = _ext_problems(repo_ext)
        if problems:
            add("deny-extension", "fail", f"{repo_ext}: " + "; ".join(problems))
        else:
            add("deny-extension", "ok", f"{repo_ext} (tool_call + OCR_IN_REVIEW present)")
    # The installed slot is where install-kimchi-hook.sh copies the extension
    # for hosts that load it from config. The gate itself passes the repo copy
    # with -e, so a missing slot is a WARN, not a FAIL.
    if not installed_ext.is_file():
        add("deny-extension-slot", "warn",
            f"not installed at {installed_ext} (the worker loads the repo copy via -e; "
            "run scripts/install-kimchi-hook.sh to install the slot)")
    else:
        problems = _ext_problems(installed_ext)
        if problems:
            add("deny-extension-slot", "warn", f"{installed_ext}: " + "; ".join(problems))
        else:
            add("deny-extension-slot", "ok", f"{installed_ext} (tool_call + OCR_IN_REVIEW present)")


def check_hooks(add, env, repo=None, hook_dir=None):
    hook_dir = (Path(hook_dir) if hook_dir
                else Path(env.get("KIMCHI_HOOKS_DIR", "").strip()
                          or os.path.join(os.path.expanduser("~"), ".config", "kimchi",
                                          "harness", "hooks", "bash")))
    if repo:
        repo = os.path.abspath(repo)
        try:
            gitdir = _run(["git", "-C", repo, "rev-parse", "--absolute-git-dir"], env)
        except Exception as exc:
            add("pre-push-hook", "fail", f"git could not be run against {repo} ({exc})")
            gitdir = None
        if gitdir is not None:
            if gitdir.returncode != 0 or not gitdir.stdout.strip():
                add("pre-push-hook", "fail", f"not a git repository: {repo}")
            else:
                cfg = _run(["git", "-C", repo, "config", "core.hooksPath"], env)
                if cfg.returncode == 0 and cfg.stdout.strip():
                    top = _run(["git", "-C", repo, "rev-parse", "--show-toplevel"], env)
                    base = Path(top.stdout.strip()) if (top.returncode == 0
                                                        and top.stdout.strip()) else Path(repo)
                    hooks = (base / cfg.stdout.strip()).resolve()
                    where = f"core.hooksPath {cfg.stdout.strip()} -> {hooks}"
                else:
                    hooks = Path(gitdir.stdout.strip()) / "hooks"
                    where = f"{hooks}"
                prepush = hooks / "pre-push"
                try:
                    text = prepush.read_text(encoding="utf-8")
                except OSError:
                    add("pre-push-hook", "fail",
                        f"not installed at {prepush} ({where}) -- terminal and script pushes "
                        "from this repo are NOT gated (the PRIMARY layer, D11); run "
                        "scripts/install-git-hook.sh")
                else:
                    if "review-gate" in text:
                        add("pre-push-hook", "ok", f"installed at {prepush} ({where})")
                    else:
                        add("pre-push-hook", "fail",
                            f"{prepush} exists but does not run the gate (no 'review-gate' "
                            f"in the file) -- {where}")
    # The Kimchi bash hook is the UX layer only; the pre-push layer still
    # enforces without it, so its absence is a WARN.
    kimchi_hook = hook_dir / "review-gate.sh"
    if kimchi_hook.is_file():
        add("kimchi-bash-hook", "ok", f"present at {kimchi_hook} (UX layer)")
    else:
        add("kimchi-bash-hook", "warn",
            f"missing at {kimchi_hook} -- agent pushes will not be intercepted early "
            "(the pre-push layer still enforces); run scripts/install-kimchi-hook.sh")


def check_store(add, env, root=None):
    root = Path(root) if root else _store_root(env)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        add("store-writable", "fail", f"cannot create the state store at {root} ({exc})")
        add("store-locks", "warn", "skipped: no usable store")
        return
    probe = root / f"doctor-probe-{os.getpid()}-{int(time.time())}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.read_text(encoding="utf-8")
        probe.unlink()
        add("store-writable", "ok", f"probe file created and removed in {root}")
    except OSError as exc:
        add("store-writable", "fail", f"cannot write inside {root} ({exc}) -- no verdict "
                                     "can be recorded, every push re-denies")
        try:
            probe.unlink()
        except OSError:
            pass
    for sub, what in (("verdicts", "verdict records"), ("data", "gate markers")):
        d = root / sub
        try:
            d.mkdir(parents=True, exist_ok=True)
            n = len(list(d.iterdir()))
            add(f"store-{sub}", "ok", f"readable ({n} entr{'y' if n == 1 else 'ies'}: {d})")
        except OSError as exc:
            add(f"store-{sub}", "fail", f"{what} unreadable at {d} ({exc})")
    # Stale state locks: an O_EXCL .lock older than LOCK_STALE_S whose owning
    # pid is dead belongs to a process that died between claim and release
    # (review-gate _StateLock). A live owner is just slow; a dead one is stale.
    # The .flock singleflight files are kernel-managed and auto-released when
    # their owner dies, so they cannot go stale and are not checked.
    stale, live = [], []
    try:
        lock_paths = sorted((root / "async").glob("*/*.lock"))
    except OSError:
        lock_paths = []
    now = time.time()
    for p in lock_paths:
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        if age <= LOCK_STALE_S:
            continue
        try:
            token = p.read_text(encoding="utf-8")
            pid = token.split(":", 1)[0]
        except (OSError, ValueError):
            stale.append((p, None))
            continue
        if _pid_alive(pid):
            live.append((p, pid))
        else:
            stale.append((p, pid))
    if stale:
        for p, pid in stale[:5]:
            add("store-locks", "warn",
                f"stale state lock (owner pid {pid if pid else '?'} dead, "
                f"{int(now - p.stat().st_mtime)}s old): {p}")
        if len(stale) > 5:
            add("store-locks", "warn", f"... and {len(stale) - 5} more stale lock(s)")
    elif live:
        for p, pid in live[:5]:
            add("store-locks", "ok",
                f"aged lock still owned by live pid {pid} (not stale): {p}")
    else:
        add("store-locks", "ok", f"no stale state locks (checked {len(lock_paths)})")


def check_states(add, env, root=None):
    root = Path(root) if root else _store_root(env)
    states = _read_states(root)
    sticky, corrupt = [], []
    for p, data in states:
        if data is None:
            corrupt.append(p)
        elif (data.get("state") == "failed"
                and int(data.get("attempts") or 0) >= ATTEMPT_CAP):
            sticky.append((p, data))
    for p in corrupt:
        add("state-machine", "warn", f"unparseable state file: {p}")
    if sticky:
        for p, data in sticky[:5]:
            reason = str(data.get("reason") or "?")
            add("state-machine", "warn",
                f"sticky error state (attempts {data.get('attempts')}, reason {reason!r}): "
                f"{p} -- pushes for this tip are being denied pending doctor")
        if len(sticky) > 5:
            add("state-machine", "warn", f"... and {len(sticky) - 5} more sticky error state(s)")
        add("state-machine", "warn",
            f"{len(sticky)} sticky error state(s) total; clear a state with "
            f"OCR_FORCE_REVIEW=1 on the next push (see review-gate.py)")
    elif not corrupt:
        add("state-machine", "ok", f"{len(states)} state record(s), none in the sticky "
                                    "error state")


def check_canary_hint(add, env):
    ext = GATE_ROOT / "extensions" / "review-gate-deny.ts"
    add("canary-hint", "info",
        "manual read-only-sandbox canary (the write MUST be blocked): "
        f"OCR_IN_REVIEW=1 kimchi -e {ext} -p 'use your write tool to create "
        "/tmp/rg-canary-check.txt'")


def run_checks(repo=None, env=None, repo_ext=None, installed_ext=None,
               hook_dir=None):
    """Every doctor check, in report order. Returns the list of check dicts:
    {"name", "status" ("ok"|"warn"|"fail"|"info"), "detail"}. The
    repo_ext / installed_ext / hook_dir overrides exist for the seeded
    failure-mode tests."""
    env = dict(os.environ) if env is None else dict(env)
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    check_python(add, env)
    check_git(add, env)
    check_kimchi(add, env)
    check_deny_ext(add, env, repo_ext=repo_ext, installed_ext=installed_ext)
    check_hooks(add, env, repo=repo, hook_dir=hook_dir)
    check_store(add, env)
    check_states(add, env)
    check_canary_hint(add, env)
    return checks


def format_checks(checks):
    lines = ["review-gate doctor"]
    for c in checks:
        tag = {"ok": "OK ", "warn": "WARN", "fail": "FAIL", "info": "INFO"}[c["status"]]
        lines.append(f"[{tag}] {c['name']}: {c['detail']}")
    fails = sum(1 for c in checks if c["status"] == "fail")
    warns = sum(1 for c in checks if c["status"] == "warn")
    oks = sum(1 for c in checks if c["status"] == "ok")
    lines.append(f"summary: {oks} ok, {warns} warn, {fails} fail")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="review-gate doctor: health checks for the Kimchi review gate "
                    "(exit 1 when any check fails).")
    parser.add_argument("--repo", default=None, metavar="PATH",
                        help="a gated repository: additionally check the pre-push "
                             "hook at its core.hooksPath-resolved location")
    parser.add_argument("--json", action="store_true",
                        help="emit the checks as JSON instead of text")
    args = parser.parse_args(argv)

    checks = run_checks(repo=args.repo)
    fails = sum(1 for c in checks if c["status"] == "fail")
    if args.json:
        print(json.dumps({
            "checks": checks,
            "ok": sum(1 for c in checks if c["status"] == "ok"),
            "warn": sum(1 for c in checks if c["status"] == "warn"),
            "fail": fails,
        }, indent=2))
    else:
        print(format_checks(checks))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
