"""Live check of `review-gate.py --mode hook` (the path a harness PreToolUse hook takes).

Per scenario: a fresh repo with one unpushed commit, then the hook is called with a
`git push` tool call (JSON on stdin, as the Claude Code / Kimchi adapters do) until it
stops saying "review running". The backend is chosen by OCR_HARNESS, exactly as the
adapters set it, with simulated claude AND kimchi both installed.
Usage: live_hook.py <unified checkout> <out.json>
"""
import json
import os
import subprocess
import sys
import tempfile
import time

CHECKOUT = os.path.abspath(sys.argv[1])
OUT = sys.argv[2]
GATE = os.path.join(CHECKOUT, "scripts", "review-gate.py")
sys.path.insert(0, os.path.join(CHECKOUT, "tests"))
import fake_backend  # noqa: E402

ROOT = tempfile.mkdtemp(prefix="crg-hook-")
_SCRUB = ("OCR_", "CLAUDE_CODE_", "KIMCHI_", "PI_CODING_AGENT", "STUB_", "FAKE_")


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def cli_free_path(extra):
    keep = []
    for d in os.environ["PATH"].split(os.pathsep):
        if d and not any(os.path.exists(os.path.join(d, n + e)) for n in ("claude", "kimchi")
                         for e in ("", ".cmd", ".exe", ".bat", ".ps1")):
            keep.append(d)
    return os.pathsep.join([extra] + keep)


def scenario(name, harness, expect, verdict=None, fake=None, host_in_reason=None, host_not_in_reason=None):
    base = tempfile.mkdtemp(prefix=name.split()[0] + "-", dir=ROOT)
    remote, work = os.path.join(base, "origin.git"), os.path.join(base, "work")
    git(base, "init", "-q", "--bare", "-b", "main", remote)
    git(base, "init", "-q", "-b", "main", work)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false"),
                 ("core.autocrlf", "false")):
        git(work, "config", k, v)
    # keep any globally installed review-gate hook out of this repo's way
    os.makedirs(os.path.join(base, "no-hooks"))
    git(work, "config", "core.hooksPath", os.path.join(base, "no-hooks").replace("\\", "/"))
    with open(os.path.join(work, "calc.py"), "w", newline="\n") as fh:
        fh.write("def avg(xs):\n    return sum(xs) / len(xs)\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "base")
    git(work, "remote", "add", "origin", remote)
    git(work, "push", "-q", "-u", "origin", "main")
    with open(os.path.join(work, "calc.py"), "a", newline="\n") as fh:
        fh.write("\n\ndef double(x):\n    return x * 2\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "add double")

    shims = os.path.join(base, "shims")
    for flavor in ("claude", "kimchi"):
        fake_backend.install(shims, flavor)
    env = {k: v for k, v in os.environ.items() if not k.startswith(_SCRUB) and k != "CLAUDECODE"}
    env.update(PATH=cli_free_path(shims), OCR_STATE_DIR=os.path.join(base, "state"),
               CLAUDE_PLUGIN_DATA=os.path.join(base, "plugin-data"), OCR_HARNESS=harness,
               FAKE_TRACE=os.path.join(base, "fake.trace"), GIT_TERMINAL_PROMPT="0")
    if verdict:
        env["STUB_VERDICT"] = verdict
    if fake:
        env["FAKE_SCENARIO"] = fake

    payload = json.dumps({"session_id": "live-" + name.split()[0], "tool_name": "bash", "cwd": work,
                          "input": {"command": "git push origin main"}})
    seq, t0 = [], time.monotonic()
    decision, reason = "?", ""
    while time.monotonic() - t0 < 240:
        p = subprocess.run([sys.executable, GATE, "--mode", "hook"], input=payload, capture_output=True,
                           text=True, cwd=work, env=env, timeout=120)
        out = (p.stdout or "").strip()
        if not out:
            decision, reason = "pass", ""
        else:
            obj = json.loads(out)
            assert obj.get("decision") != "allow", out
            decision, reason = ("deny" if obj.get("decision") == "block" else obj.get("decision")), obj.get("reason", "")
        seq.append(decision)
        running = decision == "deny" and any(s in reason.lower() for s in ("still running", "review started",
                                                                           "is running", "review of"))
        if p.returncode != 0 or not running:
            break
        time.sleep(3)
    tr = fake_backend.read_trace(os.path.join(base, "fake.trace"))
    flavors = sorted({t["flavor"] for t in tr})
    checks = [(f"final decision = {expect}", decision == expect)]
    if expect == "pass":
        checks.append(("hook output is silent (a hook never grants)", reason == ""))
    checks.append((f"only {harness} was launched", flavors == [harness]))
    if host_in_reason:
        checks.append((f"reason names {host_in_reason}", host_in_reason.lower() in reason.lower()))
    if host_not_in_reason:
        checks.append((f"reason does not name {host_not_in_reason}", host_not_in_reason.lower() not in reason.lower()))
    return {"name": name, "ok": all(c[1] for c in checks), "checks": checks, "decision": decision,
            "sequence": seq, "secs": round(time.monotonic() - t0, 1), "reason": reason[-900:], "flavors": flavors}


S = [
    dict(name="H1 hook, claude, pass", harness="claude", verdict="pass", expect="pass"),
    dict(name="H2 hook, claude, blocking finding", harness="claude", verdict="block", expect="deny"),
    dict(name="H3 hook, kimchi, pass", harness="kimchi", verdict="pass", expect="pass"),
    dict(name="H4 hook, kimchi, blocking finding", harness="kimchi", verdict="block", expect="deny"),
    dict(name="H5 hook, claude crashes -> hints name Claude Code", harness="claude", fake="crash",
         expect="deny", host_in_reason="Claude Code", host_not_in_reason="Kimchi"),
    dict(name="H6 hook, kimchi crashes -> hints name Kimchi", harness="kimchi", fake="crash",
         expect="deny", host_in_reason="Kimchi", host_not_in_reason="Claude Code"),
]
res = []
for sc in S:
    t = time.monotonic()
    try:
        r = scenario(**sc)
    except Exception as exc:  # noqa: BLE001
        r = {"name": sc["name"], "ok": False, "checks": [("scenario ran", False)],
             "reason": f"{type(exc).__name__}: {exc}", "sequence": []}
    res.append(r)
    print(f"[{'PASS' if r['ok'] else 'FAIL'}] {r['name']}  ({time.monotonic() - t:.0f}s)  seq={r.get('sequence')}", flush=True)
    for label, ok in r["checks"]:
        if not ok:
            print(f"        x {label}", flush=True)
    with open(OUT, "w") as fh:
        json.dump(res, fh, indent=1)
print(f"\n{sum(r['ok'] for r in res)}/{len(res)} hook scenarios passed")
