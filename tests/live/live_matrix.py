"""Live end-to-end matrix for the unified gate.

Every scenario is a REAL `git push` from a fresh clone into a bare remote, with a
repo-local pre-push that execs the unified checkout's scripts/pre-push (the same
path the shared git hook install uses). Backends are:
  * real  -> the actual `claude` CLI on this machine
  * fake  -> tests/fake_backend.py, which stands in for `claude -p` / `kimchi -p`
             (kimchi is not installed here) and delegates content to the stub
             reviewer, so the gate's discovery, selection, argv, env scrub,
             canary, spawn and output parsing are the real code under test.
Usage: live_matrix.py <unified checkout> <out.json> [scenario-name-substring ...]
"""
import json
import os
import subprocess
import sys
import tempfile
import time

CHECKOUT = os.path.abspath(sys.argv[1])
OUT = sys.argv[2]
ONLY = sys.argv[3:]
SCRIPTS = os.path.join(CHECKOUT, "scripts")
sys.path.insert(0, os.path.join(CHECKOUT, "tests"))
import fake_backend  # noqa: E402

ROOT = tempfile.mkdtemp(prefix="crg-live-")
BUG = "def avg(xs):\n    return sum(xs) / len(xs)\n\n\ndef first(xs):\n    return xs[1]\n"
CLEAN = "def avg(xs):\n    return sum(xs) / len(xs)\n\n\ndef double(x):\n    return x * 2\n"

_SCRUB_PREFIXES = ("OCR_", "CLAUDE_CODE_", "KIMCHI_", "PI_CODING_AGENT", "STUB_", "FAKE_")
_SCRUB_NAMES = ("CLAUDECODE", "SCR_GATE_DIR", "SCR_BIN")


def git(cwd, *a, env=None, check=True):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True,
                          env=env, check=check)


def cli_free_path(extra_dirs=()):
    """PATH with every dir that holds a claude/kimchi launcher removed."""
    keep = []
    for d in os.environ["PATH"].split(os.pathsep):
        if not d:
            continue
        if any(os.path.exists(os.path.join(d, n + e)) for n in ("claude", "kimchi")
               for e in ("", ".cmd", ".exe", ".bat", ".ps1")):
            continue
        keep.append(d)
    return os.pathsep.join(list(extra_dirs) + keep)


def build_repo(base, pin=None):
    remote, work = os.path.join(base, "origin.git"), os.path.join(base, "work")
    git(base, "init", "-q", "--bare", "-b", "main", remote)
    git(base, "init", "-q", "-b", "main", work)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"),
                 ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
        git(work, "config", k, v)
    with open(os.path.join(work, "calc.py"), "w", newline="\n") as fh:
        fh.write("def avg(xs):\n    return sum(xs) / len(xs)\n")
    if pin:
        os.makedirs(os.path.join(work, ".ocr"))
        with open(os.path.join(work, ".ocr", "config.json"), "w") as fh:
            fh.write(json.dumps({"backend": pin}))
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "base")
    git(work, "remote", "add", "origin", remote)
    git(work, "push", "-q", "-u", "origin", "main")        # no hook yet -> unreviewed baseline
    # Same layout install-git-hook.sh produces: a COPY of scripts/pre-push in its
    # own hooks dir, selected through core.hooksPath (set per repo here instead of
    # globally). It must not live at .git/hooks/pre-push: the script chains into
    # that file, and a copy of itself there would recurse.
    hooks = os.path.join(base, "hooks")
    os.makedirs(hooks)
    scripts_fwd = SCRIPTS.replace("\\", "/")
    with open(os.path.join(SCRIPTS, "pre-push"), encoding="utf-8", newline="") as fh:
        body = fh.read().replace("__SCR_DIR__", scripts_fwd).replace("__SCR_VERSION__", "live-matrix")
    hook = os.path.join(hooks, "pre-push")
    with open(hook, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    os.chmod(hook, 0o755)
    git(work, "config", "core.hooksPath", hooks.replace("\\", "/"))
    return remote, work


def commit(work, content, msg, name="calc.py"):
    with open(os.path.join(work, name), "w", newline="\n") as fh:
        fh.write(content)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", msg)


def run_scenario(sc):
    base = tempfile.mkdtemp(prefix=sc["name"].split()[0] + "-", dir=ROOT)
    remote, work = build_repo(base, pin=sc.get("pin"))
    shim_dir = os.path.join(base, "shims")
    trace = os.path.join(base, "fake.trace")
    for flavor in sc.get("shims", ()):
        fake_backend.install(shim_dir, flavor)

    env = {k: v for k, v in os.environ.items()
           if not k.startswith(_SCRUB_PREFIXES) and k not in _SCRUB_NAMES}
    env["OCR_STATE_DIR"] = os.path.join(base, "state")
    env["CLAUDE_PLUGIN_DATA"] = os.path.join(base, "plugin-data")
    env["OCR_DEBUG"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Pin the gate under test. Without it pre-push follows the gate-dir pointer to
    # whatever review-gate plugin is installed on this machine (an older gate).
    env["SCR_GATE_DIR"] = SCRIPTS.replace("\\", "/")
    if sc.get("shims"):
        env["FAKE_TRACE"] = trace
    if sc.get("path_mode") != "real":
        env["PATH"] = cli_free_path([shim_dir] if sc.get("shims") else [])
    if sc.get("no_cli_anywhere"):                         # also hide the Desktop-bundled exe
        empty = os.path.join(base, "empty-home")
        os.makedirs(empty, exist_ok=True)
        env.update(LOCALAPPDATA=empty, USERPROFILE=empty, HOME=empty)
    env.update(sc.get("env", {}))
    if "verdict" in sc:
        env["STUB_VERDICT"] = sc["verdict"]
    if sc.get("fake_scenario"):
        env["FAKE_SCENARIO"] = sc["fake_scenario"]

    n = sc.get("commits", 1)
    for i in range(n):
        last = i == n - 1
        commit(work, sc.get("content", CLEAN) if last else CLEAN + f"# {i}\n", f"change {i}",
               name="calc.py" if last else f"extra{i}.py")
    local = git(work, "rev-parse", "HEAD").stdout.strip()

    t0, out, rc, attempts = time.monotonic(), "", None, 0
    # The gate answers "still running, push again" at once instead of waiting, so
    # a slow (real) reviewer needs patient retries, up to 15 minutes.
    for attempts in range(1, 200):
        p = subprocess.run(["git", "push", "origin", "main"], cwd=work, env=env, capture_output=True,
                           text=True, errors="replace", timeout=900)
        out, rc = p.stdout + p.stderr, p.returncode
        if "still running" not in out or time.monotonic() - t0 > 900:
            break
        time.sleep(10)
    elapsed = time.monotonic() - t0
    remote_head = git(remote, "rev-parse", "main").stdout.strip()
    pushed = remote_head == local

    tr = fake_backend.read_trace(trace)
    review = [t for t in tr if t["prompt"].startswith(("/skill:review", "/review-gate:review"))]
    canary = [t for t in tr if t["prompt"].startswith("Sandbox self-check")]
    docs = [t for t in tr if t["prompt"].startswith("Summarize this repository")]
    dbg = ""
    for dp, _dn, fn in os.walk(env["OCR_STATE_DIR"]):
        for f in fn:
            if f.startswith("review-gate-debug.log"):
                with open(os.path.join(dp, f), encoding="utf-8", errors="replace") as fh:
                    dbg += fh.read()
    lines = dbg.splitlines()
    backends = [ln.split("backend=", 1)[1].split()[0] for ln in lines if ln.lstrip().startswith("backend=")
                or " backend=" in ln]
    reasons = [ln.split("reason=", 1)[1].split(" notes=")[0] for ln in lines if "reason=" in ln]
    launches = sum(1 for ln in lines if " start ts=" in ln or ln.startswith("start ts="))
    flavors = sorted({t["flavor"] for t in tr})

    checks = []

    def chk(label, ok):
        checks.append((label, bool(ok)))

    chk(f"push {'goes through' if sc['push_ok'] else 'is blocked'}", pushed == sc["push_ok"])
    if sc["push_ok"]:
        chk("push exit 0", rc == 0)
    else:
        chk("push exit non-zero and remote untouched", rc != 0 and not pushed)
    if "backend" in sc:
        chk(f"selected backend = {sc['backend']}", bool(backends) and set(backends) == {sc["backend"]})
    if sc.get("no_launch"):
        chk("no worker launched", launches == 0 and not tr)
    if "reviews" in sc:
        chk(f"exactly {sc['reviews']} review launch(es)", len(review) == sc["reviews"])
    if "flavors" in sc:
        chk(f"only {sc['flavors']} was launched", flavors == sc["flavors"])
    if "canary" in sc:
        chk(f"deny-extension canary ran {sc['canary']}x", len(canary) == sc["canary"])
    if "reviews_real" in sc:
        chk(f"{sc['reviews_real']} real worker launch(es)", launches == sc["reviews_real"])
    for s in sc.get("out_has", ()):
        chk(f"output mentions {s!r}", s.lower() in out.lower())
    for s in sc.get("out_lacks", ()):
        chk(f"output does not mention {s!r}", s.lower() not in out.lower())
    argv = sum([t["argv"] for t in review], [])
    for a in sc.get("argv_has", ()):
        chk(f"worker argv has {a}", a in argv)
    for a in sc.get("argv_lacks", ()):
        chk(f"worker argv lacks {a}", a not in argv)
    if sc.get("env_scrubbed"):
        seen = {}
        for t in review:
            seen.update(t["env"])
        for name in sc["env_scrubbed"]:
            chk(f"{name} not leaked to the worker", name not in seen)
        chk("OCR_IN_REVIEW=1 marks the worker",
            bool(review) and all(t["env"].get("OCR_IN_REVIEW") == "1" for t in review))

    return {"name": sc["name"], "ok": all(c[1] for c in checks), "checks": checks,
            "rc": rc, "pushed": pushed, "attempts": attempts, "secs": round(elapsed, 1),
            "backends": backends, "reasons": reasons[:1], "flavors": flavors,
            "reviews": len(review), "canary": len(canary), "docs": len(docs),
            "launches": launches, "out_tail": out[-1400:]}


FAKE2 = ("claude", "kimchi")
S = []
# ---- real claude -------------------------------------------------------------
S += [
    dict(name="L1 real claude, clean change", path_mode="real", env={"OCR_BACKEND": "claude"},
         content=CLEAN, push_ok=True, backend="claude", reviews_real=1),
    dict(name="L2 real claude, seeded bug", path_mode="real", env={"OCR_BACKEND": "claude"},
         content=BUG, push_ok=False, backend="claude", reviews_real=1, out_has=["first"]),
]
# ---- simulated kimchi --------------------------------------------------------
S += [
    dict(name="K1 sim kimchi, clean", shims=("kimchi",), env={"OCR_BACKEND": "kimchi"}, verdict="pass",
         push_ok=True, backend="kimchi", flavors=["kimchi"], reviews=1, canary=1,
         argv_has=["-e"]),
    dict(name="K2 sim kimchi, blocking finding", shims=("kimchi",), env={"OCR_BACKEND": "kimchi"},
         verdict="block", push_ok=False, backend="kimchi", flavors=["kimchi"], reviews=1),
    dict(name="K3 sim kimchi, only CLI on PATH, no config (auto)", shims=("kimchi",), verdict="pass",
         push_ok=True, backend="kimchi", flavors=["kimchi"], reviews=1),
    dict(name="K4 sim kimchi, 3 commits in one push", shims=("kimchi",), env={"OCR_BACKEND": "kimchi"},
         verdict="pass", commits=3, push_ok=True, backend="kimchi", reviews=1),
    dict(name="K5 sim kimchi returns prose, no verdict", shims=("kimchi",), env={"OCR_BACKEND": "kimchi"},
         fake_scenario="garbage", push_ok=False, backend="kimchi"),
    dict(name="K6 sim kimchi auth expired", shims=("kimchi",), env={"OCR_BACKEND": "kimchi"},
         fake_scenario="auth", push_ok=False, out_has=["login"]),
    dict(name="K7 sim kimchi crashes", shims=("kimchi",), env={"OCR_BACKEND": "kimchi"},
         fake_scenario="crash", push_ok=False),
]
# ---- simulated claude --------------------------------------------------------
S += [
    dict(name="C1 sim claude, clean", shims=("claude",), env={"OCR_BACKEND": "claude"}, verdict="pass",
         push_ok=True, backend="claude", flavors=["claude"], reviews=1, canary=0,
         argv_has=["--allowedTools", "--disallowedTools", "--strict-mcp-config", "--setting-sources",
                   "--plugin-dir", "stream-json"],
         argv_lacks=["-e"],
         env_scrubbed=["CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT"]),
    dict(name="C2 sim claude, blocking finding", shims=("claude",), env={"OCR_BACKEND": "claude"},
         verdict="block", push_ok=False, backend="claude", flavors=["claude"], reviews=1),
    dict(name="C3 sim claude, only CLI on PATH (auto)", shims=("claude",), verdict="pass",
         push_ok=True, backend="claude", flavors=["claude"], reviews=1),
    dict(name="C4 sim claude inside a Claude Code session env", shims=("claude",), verdict="pass",
         env={"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SESSION_ID": "sess"},
         push_ok=True, backend="claude", flavors=["claude"], reviews=1,
         env_scrubbed=["CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT"]),
    dict(name="C5 sim claude auth expired", shims=("claude",), env={"OCR_BACKEND": "claude"},
         fake_scenario="auth", push_ok=False, out_has=["login"]),
    dict(name="C6 sim claude returns prose, no verdict", shims=("claude",), env={"OCR_BACKEND": "claude"},
         fake_scenario="garbage", push_ok=False),
    dict(name="C7 sim claude crashes", shims=("claude",), env={"OCR_BACKEND": "claude"},
         fake_scenario="crash", push_ok=False),
]
# ---- selection with both installed ------------------------------------------
S += [
    dict(name="S1 both installed, no signal -> default kimchi, ONE review", shims=FAKE2, verdict="pass",
         push_ok=True, backend="kimchi", flavors=["kimchi"], reviews=1),
    dict(name="S2 both installed, Claude Code session -> claude", shims=FAKE2, verdict="pass",
         env={"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"},
         push_ok=True, backend="claude", flavors=["claude"], reviews=1),
    dict(name="S3 both installed, Kimchi session -> kimchi", shims=FAKE2, verdict="pass",
         env={"KIMCHI_SESSION_ID": "k1"},
         push_ok=True, backend="kimchi", flavors=["kimchi"], reviews=1),
    dict(name="S4 both installed, both session signals -> default kimchi (no guess)", shims=FAKE2,
         verdict="pass", env={"CLAUDECODE": "1", "KIMCHI_SESSION_ID": "k1"},
         push_ok=True, backend="kimchi", flavors=["kimchi"], reviews=1),
    dict(name="S5 both installed, repo pins claude", shims=FAKE2, verdict="pass", pin="claude",
         push_ok=True, backend="claude", flavors=["claude"], reviews=1),
    dict(name="S6 both installed, repo pins kimchi", shims=FAKE2, verdict="pass", pin="kimchi",
         env={"CLAUDECODE": "1"},
         push_ok=True, backend="kimchi", flavors=["kimchi"], reviews=1),
    dict(name="S7 env OCR_BACKEND beats the repo pin", shims=FAKE2, verdict="pass", pin="kimchi",
         env={"OCR_BACKEND": "claude"},
         push_ok=True, backend="claude", flavors=["claude"], reviews=1),
    dict(name="S8 harness hint (hook adapter) picks claude", shims=FAKE2, verdict="pass",
         env={"OCR_HARNESS": "claude"},
         push_ok=True, backend="claude", flavors=["claude"], reviews=1),
    dict(name="S9 both installed, block verdict still blocks via the chosen one", shims=FAKE2,
         verdict="block", env={"OCR_BACKEND": "claude"},
         push_ok=False, backend="claude", flavors=["claude"], reviews=1),
]
# ---- fail-closed -------------------------------------------------------------
S += [
    dict(name="F1 unknown backend name", shims=FAKE2, env={"OCR_BACKEND": "gemini"}, verdict="pass",
         push_ok=False, no_launch=True, out_has=["backend selection failed", "gemini"]),
    dict(name="F2 repo pins an unknown backend", shims=FAKE2, pin="gemini", verdict="pass",
         push_ok=False, no_launch=True, out_has=["backend selection failed"]),
    dict(name="F3 kimchi requested but not installed", env={"OCR_BACKEND": "kimchi"},
         no_cli_anywhere=True, push_ok=False, out_has=["kimchi"]),
    dict(name="F4 claude requested but not installed", env={"OCR_BACKEND": "claude"},
         no_cli_anywhere=True, push_ok=False, out_has=["claude"]),
    dict(name="F5 no CLI at all, nothing requested", no_cli_anywhere=True, push_ok=False),
]

results = []
for sc in S:
    if ONLY and not any(o.lower() in sc["name"].lower() for o in ONLY):
        continue
    t = time.monotonic()
    try:
        r = run_scenario(sc)
    except Exception as exc:  # noqa: BLE001
        r = {"name": sc["name"], "ok": False, "checks": [("scenario ran", False)],
             "out_tail": f"{type(exc).__name__}: {exc}"}
    results.append(r)
    mark = "PASS" if r["ok"] else "FAIL"
    print(f"[{mark}] {r['name']}  ({time.monotonic() - t:.0f}s)", flush=True)
    for label, ok in r["checks"]:
        if not ok:
            print(f"        x {label}", flush=True)
    with open(OUT, "w") as fh:
        json.dump(results, fh, indent=1)
print(f"\n{sum(r['ok'] for r in results)}/{len(results)} scenarios passed; workdir {ROOT}")
