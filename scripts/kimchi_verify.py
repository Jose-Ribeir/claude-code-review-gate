#!/usr/bin/env python3
"""Real-kimchi verification, run once on a machine that has kimchi installed.

Driven by scripts/kimchi-verify.sh (or .ps1). It checks the things that cannot
be tested without a real kimchi and writes ONE report file, ready to paste back:

  1. kimchi --version and the model list
  2. ocr_doctor and the effective settings for the kimchi backend
  3. every effective kimchi model pin is in kimchi's model list
  4. the reviewer model accepts the `:high` / `:max` thinking suffix
  5. a timed sample review: a tiny commit pushed through the real gate
     (OCR_BACKEND=kimchi, OCR_DEBUG=1) from a throwaway repo, against the
     "small push verdict < 60 s" target

Nothing here touches your repositories or your global git config: all git work
happens in a temp directory (repo-local config only) and the gate's state store
is a temp OCR_STATE_DIR. Every external call has a timeout.

Environment (all optional):
  KIMCHI_BIN                 the kimchi executable (default: `kimchi` on PATH)
  KIMCHI_VERIFY_REPORT       report path (default ./kimchi-verify-report.txt)
  KIMCHI_VERIFY_TARGET_S     small-push verdict target in seconds (default 60)
  KIMCHI_VERIFY_CALL_TIMEOUT per-call timeout for the suffix probes (default 120)
  KIMCHI_VERIFY_PUSH_TIMEOUT timeout for one `git push` (default 420)
  KIMCHI_VERIFY_KEEP=1       keep the temp directory (its path is in the report)

Exit status: 0 when every check passed, 1 otherwise.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

SMOKE_PROMPT = "Reply with the single word OK."
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_PIN_LABELS = {
    "orchestrator": "session/orchestrator",
    "code-reviewer": "tier1 (code-reviewer)",
    "deep-reviewer": "tier2 (deep-reviewer)",
    "canary": "canary",
    "spotter": "spotter",
    "docs-checker": "docs-checker",
    "code-filter": "code-filter",
    "code-resolver": "code-resolver",
}


class Report:
    def __init__(self):
        self.lines = []
        self.checks = []          # (name, ok, detail)

    def section(self, title):
        self.lines += ["", "=" * 72, title, "=" * 72]

    def text(self, body):
        self.lines.append(body if body.endswith("\n") or not body else body)

    def check(self, name, ok, detail="", fix=""):
        self.checks.append((name, bool(ok), detail))
        mark = "PASS" if ok else "FAIL"
        line = f"[{mark}] {name}" + (f": {detail}" if detail else "")
        self.lines.append(line)
        print(line, flush=True)
        if not ok and fix:
            self.lines.append(f"       next: {fix}")
            print(f"       next: {fix}", flush=True)

    @property
    def ok(self):
        return all(ok for _, ok, _ in self.checks)


def _clean(text):
    return _ANSI.sub("", text or "")


def _run(argv, timeout, env=None, cwd=None, stdin=None):
    """(rc, stdout, stderr, seconds). rc is None on timeout / could-not-run;
    stderr then carries the reason."""
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=env, cwd=cwd,
                              input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL)
        return proc.returncode, _clean(proc.stdout), _clean(proc.stderr), time.monotonic() - started
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return None, _clean(out), _clean(err) + f"\n[timed out after {timeout}s]", time.monotonic() - started
    except OSError as exc:
        return None, "", f"[could not run {argv[0]}: {exc}]", time.monotonic() - started


def _fmt(rc, out, err):
    return f"exit={rc}\n--- stdout ---\n{out.rstrip()}\n--- stderr ---\n{err.rstrip()}"


def _git(args, cwd, env, timeout=60):
    rc, out, err, _ = _run(["git"] + args, timeout, env=env, cwd=cwd)
    if rc != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {_fmt(rc, out, err)}")
    return out.strip()


def find_kimchi(env):
    cand = env.get("KIMCHI_BIN", "").strip()
    if cand:
        return cand if (os.path.isfile(cand) or shutil.which(cand)) else None
    return shutil.which("kimchi", path=env.get("PATH"))


def parse_model_ids(text):
    """Ids named in the model list, vendor prefix dropped (kimchi-dev/x == x)."""
    ids = set()
    for tok in re.split(r"[\s,|;\"'\[\]{}()]+", _clean(text)):
        tok = tok.strip().strip("*-•:").lower()
        if tok:
            ids.add(tok.rsplit("/", 1)[-1])
    return ids


def bare_id(model):
    """`kimchi-dev/kimi-k3.1-pro:high` -> `kimi-k3.1-pro`."""
    m = (model or "").strip().lower().rsplit("/", 1)[-1]
    return m.split(":", 1)[0]


def stream_text(stdout):
    """(assistant text, error text or "") from a kimchi `--mode json` stream."""
    text, error = "", ""
    for ln in stdout.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else ev
        if ev.get("errorMessage") or msg.get("errorMessage"):
            error = str(ev.get("errorMessage") or msg.get("errorMessage"))
        if msg.get("stopReason") == "error" and not error:
            error = "stopReason=error"
        if ev.get("type") == "result" and ev.get("is_error"):
            error = str(ev.get("result") or "is_error")
        content = msg.get("content")
        if isinstance(content, list):
            got = "".join(c.get("text", "") for c in content
                          if isinstance(c, dict) and c.get("type") == "text")
            if got.strip():
                text = got
        elif isinstance(content, str) and content.strip():
            text = content
        if ev.get("type") == "result" and isinstance(ev.get("result"), str) and ev["result"].strip() \
                and not ev.get("is_error"):
            text = ev["result"]
    return text.strip(), error


def effective_pins(show):
    """[(label, key, model)] for every kimchi model pin, from `show --json`."""
    out = []
    for row in show:
        m = re.match(r"agents\.([^.]+)\.kimchi\.model$", row.get("key", ""))
        if not m:
            continue
        agent = m.group(1)
        out.append((_PIN_LABELS.get(agent, agent), row["key"], str(row["value"])))
    return out


def main():
    env = dict(os.environ)
    env.pop("OCR_IN_REVIEW", None)
    env["OCR_BACKEND"] = "kimchi"
    env["OCR_DEBUG"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    for k in [k for k in env if k.startswith("GIT_") and k not in ("GIT_TERMINAL_PROMPT",)]:
        env.pop(k)
    env.setdefault("PYTHONUTF8", "1")
    report_path = os.path.abspath(env.get("KIMCHI_VERIFY_REPORT") or "kimchi-verify-report.txt")
    target = float(env.get("KIMCHI_VERIFY_TARGET_S") or 60)
    call_timeout = float(env.get("KIMCHI_VERIFY_CALL_TIMEOUT") or 120)
    push_timeout = float(env.get("KIMCHI_VERIFY_PUSH_TIMEOUT") or 420)
    rep = Report()
    tmp = tempfile.mkdtemp(prefix="kimchi-verify-")
    try:
        rep.lines.append("kimchi-verify report")
        rep.lines.append(f"date: {time.strftime('%Y-%m-%d %H:%M:%S %z')}")
        rep.lines.append(f"platform: {sys.platform}, python {sys.version.split()[0]}")
        rep.lines.append(f"temp dir: {tmp}" + ("" if env.get("KIMCHI_VERIFY_KEEP") == "1" else " (removed on exit)"))
        run(rep, env, tmp, target, call_timeout, push_timeout)
    except Exception as exc:       # the report must survive a bug in this script
        rep.check("verify script", False, f"unexpected error: {type(exc).__name__}: {exc}",
                  "paste this report back; it is a bug in scripts/kimchi_verify.py")
    finally:
        rep.section("SUMMARY")
        for name, ok, _ in rep.checks:
            rep.lines.append(f"{'PASS' if ok else 'FAIL'}  {name}")
        rep.lines.append("")
        rep.lines.append("RESULT: " + ("ALL CHECKS PASSED" if rep.ok else "FAILED (see the FAIL lines above)"))
        try:
            with open(report_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(rep.lines) + "\n")
            print(f"\nreport written: {report_path}", flush=True)
        except OSError as exc:
            print(f"\ncould not write the report {report_path}: {exc}", file=sys.stderr)
            print("\n".join(rep.lines))
        if env.get("KIMCHI_VERIFY_KEEP") != "1":
            shutil.rmtree(tmp, ignore_errors=True)
    print("RESULT: " + ("ALL CHECKS PASSED" if rep.ok else "FAILED"), flush=True)
    return 0 if rep.ok else 1


def run(rep, env, tmp, target, call_timeout, push_timeout):
    kimchi = find_kimchi(env)

    # --- 1. version + model list ------------------------------------------------
    rep.section("1. kimchi version and model list")
    listed = set()
    if not kimchi:
        rep.check("kimchi installed", False, "no `kimchi` on PATH (and KIMCHI_BIN unset or missing)",
                  "install kimchi or set KIMCHI_BIN=/path/to/kimchi, then re-run")
    else:
        rep.text(f"kimchi: {kimchi}")
        rc, out, err, _ = _run([kimchi, "--version"], 30, env=env)
        rep.text("$ kimchi --version\n" + _fmt(rc, out, err))
        rep.check("kimchi --version", rc == 0 and bool(out.strip()),
                  out.strip().splitlines()[0] if out.strip() else _fmt(rc, out, err),
                  "check that kimchi starts at all (`kimchi --version` in a terminal)")
        forms = [["--list-models"], ["models"], ["models", "list"]]
        got = False
        for form in forms:
            rc, out, err, _ = _run([kimchi] + form, 60, env=env)
            rep.text("$ kimchi " + " ".join(form) + "\n" + _fmt(rc, out, err))
            ids = parse_model_ids(out)
            if rc == 0 and ids:
                listed, got = ids, True
                rep.text(f"model ids parsed ({len(ids)}): " + ", ".join(sorted(ids)))
                break
        rep.check("kimchi model list", got,
                  f"{len(listed)} model id(s) via `kimchi {' '.join(form)}`" if got
                  else "none of `--list-models`, `models`, `models list` produced a model list",
                  "log in (`kimchi` then /login) and check `kimchi --help` for the list-models command; "
                  "paste the errors above")

    # --- 2. doctor + settings ---------------------------------------------------
    rep.section("2. effective settings and doctor (backend kimchi)")
    cli = os.path.join(_HERE, "rg_settings_cli.py")
    rc, out, err, _ = _run([sys.executable, cli, "show", "--json"], 60, env=env)
    show = []
    try:
        show = json.loads(out)
    except ValueError:
        pass
    shown_rc, shown_out, shown_err, _ = _run([sys.executable, cli, "show"], 60, env=env)
    rep.text("$ rg_settings_cli.py show\n" + _fmt(shown_rc, shown_out, shown_err))
    rep.check("effective settings", rc == 0 and bool(show),
              f"{len(show)} settings read" if show else _fmt(rc, out, err),
              "fix the settings file named above (python scripts/rg_settings_cli.py validate)")

    # a throwaway repo for the doctor (hook health) and the sample push
    work, hooks = _make_repo(rep, tmp, env)
    doctor = os.path.join(_HERE, "ocr_doctor.py")
    args = [sys.executable, doctor] + (["--repo", work] if work else [])
    rc, out, err, _ = _run(args, 120, env=env)
    rep.text("$ ocr_doctor.py" + (" --repo <temp repo>" if work else "") + "\n" + _fmt(rc, out, err))
    fails = [ln.strip() for ln in out.splitlines() if re.match(r"\s*\[?(fail|FAIL)", ln.strip())]
    rep.check("ocr_doctor", rc == 0, "doctor reported no failing checks" if rc == 0 else
              ("; ".join(fails) or f"exit {rc}: {(err or out).strip()[:300]}"),
              "fix each FAIL line in the doctor output above, then re-run")

    # --- 3. every pinned model is in the list ----------------------------------
    rep.section("3. model pins vs the model list")
    pins = effective_pins(show)
    if not pins:
        rep.check("model pins", False, "no kimchi model pins could be read from the settings",
                  "see check 'effective settings'")
    elif not listed:
        rep.check("model pins in model list", False,
                  "cannot compare: no model list (see check 'kimchi model list')",
                  "fix the model-list check first")
    else:
        missing = []
        for label, key, model in pins:
            ok = bare_id(model) in listed
            rep.text(f"  {'ok     ' if ok else 'MISSING'} {label:<24} {key} = {model}")
            if not ok:
                missing.append((label, key, model))
        for label, key, model in missing:
            rep.check(f"pin {label}", False,
                      f"model id `{bare_id(model)}` ({key} = {model}) is NOT in kimchi's model list",
                      f"python scripts/rg_settings_cli.py set {key} kimchi-dev/<an id from the list above> "
                      "(or ask for the default to be repinned)")
        if not missing:
            rep.check("model pins in model list", True, f"all {len(pins)} effective kimchi pins are listed")

    # --- 4. thinking suffix on the reviewer model ------------------------------
    rep.section("4. thinking suffix (reviewer model)")
    if not kimchi:
        rep.check("thinking suffix", False, "no kimchi to call", "install kimchi first")
    else:
        by_key = {key: model for _, key, model in pins}
        think = {r["key"]: r["value"] for r in show if r.get("key", "").endswith(".kimchi.thinking")}
        cr = by_key.get("agents.code-reviewer.kimchi.model", "kimchi-dev/kimi-k3.1-pro")
        dr = by_key.get("agents.deep-reviewer.kimchi.model", cr)
        probes = []
        for model, level in ((cr, "high"), (cr, "max"),
                             (dr, think.get("agents.deep-reviewer.kimchi.thinking", "max"))):
            if (model, level) not in probes:
                probes.append((model, level))
        for model, level in probes:
            spec = f"{model}:{level}"
            argv = [kimchi, "-p", SMOKE_PROMPT, "--mode", "json", "--no-session", "--approve",
                    "--model", spec]
            rc, out, err, secs = _run(argv, call_timeout, env=env, cwd=tmp)
            rep.text(f"$ kimchi -p '{SMOKE_PROMPT}' --mode json --no-session --approve --model {spec}"
                     f"   ({secs:.1f}s)\n" + _fmt(rc, out, err))
            text, error = stream_text(out)
            ok = rc == 0 and not error and bool(text)
            why = ""
            if not ok:
                why = (error or (err.strip().splitlines() or [""])[-1] or out.strip()[:200]
                       or "no reply").strip()
                why = f"exit {rc}: {why}"
            rep.check(f"thinking suffix `{spec}`", ok,
                      f"accepted, replied {text[:40]!r} in {secs:.1f}s" if ok else f"REJECTED or failed ({why})",
                      f"the `:{level}` suffix is not accepted for {model}: check `kimchi --help` for the "
                      f"thinking levels this version takes, then python scripts/rg_settings_cli.py set "
                      f"agents.<agent>.kimchi.thinking <level>")

    # --- 5. timed sample review -------------------------------------------------
    rep.section("5. timed sample review (real gate, OCR_BACKEND=kimchi, OCR_DEBUG=1)")
    if not kimchi:
        rep.check("sample review", False, "no kimchi to review with", "install kimchi first")
    elif not work:
        rep.check("sample review", False, "the temp repo could not be created (see above)",
                  "paste this report back")
    else:
        sample_review(rep, env, tmp, work, target, push_timeout)


def _make_repo(rep, tmp, env):
    """A temp repo + local bare remote + a gate hook in a repo-local hooks dir.
    Returns (work, hooks) or (None, None)."""
    try:
        remote = os.path.join(tmp, "origin.git")
        work = os.path.join(tmp, "work")
        hooks = os.path.join(tmp, "hooks")
        os.makedirs(hooks)
        _git(["init", "-q", "--bare", "-b", "main", remote], tmp, env)
        _git(["init", "-q", "-b", "main", work], tmp, env)
        for k, v in (("user.email", "verify@example.com"), ("user.name", "kimchi-verify"),
                     ("commit.gpgsign", "false"), ("core.hooksPath", hooks)):
            _git(["config", k, v], work, env)
        with open(os.path.join(work, "app.py"), "w", encoding="utf-8") as fh:
            fh.write("def add(a, b):\n    return a + b\n")
        _git(["add", "."], work, env)
        _git(["commit", "-q", "-m", "base"], work, env)
        _git(["remote", "add", "origin", remote], work, env)
        _git(["push", "-q", "--no-verify", "-u", "origin", "main"], work, env)
        hook = os.path.join(hooks, "pre-push")
        shutil.copyfile(os.path.join(_HERE, "pre-push"), hook)
        os.chmod(hook, 0o755)
        return work, hooks
    except Exception as exc:
        rep.text(f"could not build the temp repo: {exc}")
        return None, None


def log_rows(state_dir, events=("call", "run", "chunk", "phase")):
    """The debug-log rows of the named events, oldest first."""
    rows = []
    data = os.path.join(state_dir, "data")
    names = [f"review-gate-debug.log.{i}" for i in (3, 2, 1)] + ["review-gate-debug.log"]
    for name in names:
        try:
            with open(os.path.join(data, name), encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    m = re.search(r"\bevent=(\w+)", ln)
                    if m and m.group(1) in events:
                        rows.append(ln.rstrip())
        except OSError:
            continue
    return rows


def sample_review(rep, env, tmp, work, target, push_timeout):
    state = os.path.join(tmp, "state")
    penv = dict(env)
    penv["OCR_STATE_DIR"] = state
    penv["SCR_GATE_DIR"] = _HERE
    with open(os.path.join(work, "app.py"), "a", encoding="utf-8") as fh:
        fh.write("\n\ndef sub(a, b):\n    return a - b\n")
    _git(["add", "."], work, env)
    _git(["commit", "-q", "-m", "add sub"], work, env)

    attempts, final_rc, final_out = [], None, ""
    total0 = time.monotonic()
    for n in range(1, 4):
        rc, out, err, secs = _run(["git", "push", "origin", "main"], push_timeout, env=penv, cwd=work)
        attempts.append((n, rc, secs))
        final_rc, final_out = rc, _fmt(rc, out, err)
        rep.text(f"$ git push origin main   (attempt {n}, {secs:.1f}s)\n" + final_out)
        if rc == 0 or rc is None or "still running" not in (out + err).lower():
            break          # verdict (pass or a real block), a timeout, or an error: no join to wait for
    total = time.monotonic() - total0

    rep.text("")
    rep.text("per push attempt:")
    for n, rc, secs in attempts:
        rep.text(f"  attempt {n}: {secs:7.1f}s  exit={rc}")
    rep.text(f"  whole push (all attempts): {total:.1f}s   target: < {target:.0f}s")

    rows = log_rows(state)
    rep.text("")
    rep.text("debug-log rows (event=call|run|chunk|phase) -- elapsed per session is `s=`:")
    for r in rows:
        rep.text("  " + r)
    if not rows:
        rep.text("  (none: the gate never reached a review; see the push output above)")
    rep.text("")
    rep.text("per-session timings:")
    sessions = []
    for r in rows:
        if re.search(r"\bevent=call\b", r):
            kind = (re.search(r"\bkind=(\S+)", r) or [None, "?"])[1]
            secs = (re.search(r"\bs=([\d.]+)", r) or [None, "?"])[1]
            outc = (re.search(r"\boutcome=(\S+)", r) or [None, "?"])[1]
            sessions.append((kind, secs, outc))
            rep.text(f"  session {kind:<16} {secs:>8}s  outcome={outc}")
    if not sessions:
        rep.text("  (no event=call rows)")

    done = final_rc == 0
    rep.check("sample review verdict", done,
              f"the gate let the push through after {len(attempts)} attempt(s)" if done
              else f"the push did not pass (last exit {final_rc})",
              "read the push output above; if it says kimchi exited / could not parse / timed out, paste "
              "this report back (it holds the call/run rows); if it is a real finding the sample change "
              "was blocked, which is itself worth reporting")
    rep.check(f"timing: small push verdict < {target:.0f}s", done and total < target,
              f"{total:.1f}s" + (" (push did not pass)" if not done else ""),
              "the per-session timings above show where the time went (a first run includes the sandbox "
              "canary, cached for later runs; re-run once to see the warm time)")


if __name__ == "__main__":
    sys.exit(main())
