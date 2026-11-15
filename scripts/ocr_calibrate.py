#!/usr/bin/env python3
"""C5 calibration harness: the real-model review matrix for the review-gate.

Drives pushes through the REAL pipeline -- bash hook entry, detached
supervisor, reviewer sandbox clone, external verdict store -- against scratch
clones of the seeded fixture repos (tests/fixtures/repos/*.git) and of the
upstream claude-code-review-gate history. No `git push` is ever executed: the
harness feeds synthetic PreToolUse payloads to `review-gate.py --mode hook`,
exactly as the async-gate tests do, then polls the verdict store until the
supervisor records its verdict for (remote, tip).

Isolation contract -- the real user config is NEVER written:
  - OCR_STATE_DIR   -> <root>/state            (verdicts, clones, async state)
  - KIMCHI_HOOKS_DIR, OCR_EXT_DIR -> <root>/kimchi-hooks|kimchi-ext
                       (where install-kimchi-hook.sh would otherwise write)
  - scratch clones  -> <root>/scratch/<source>/
Default root: /tmp/ocr-calib.

Every push appends one row to docs/calibration.md (range, source, expected,
verdict, findings with severity/confidence/headline, wall seconds). The run is
resumable: ranges whose row id is already in the matrix are skipped, and the
verdict store itself short-circuits pushes already reviewed.

Environment:
  OCR_CALIB_UPSTREAM   path to a local checkout of claude-code-review-gate,
                       the review source behind --fp-upstream and
                       --source upstream. REQUIRED for those commands:
                       there is no built-in default (a developer-machine
                       path must not be committed). The run fails with a
                       clear error before building any rows when it is
                       unset, missing, or not a git repo.

Usage:
  python3 scripts/ocr_calibrate.py --stub            # plumbing proof (no model)
  python3 scripts/ocr_calibrate.py --fn              # the 6 seeded-bug pushes
  python3 scripts/ocr_calibrate.py --fp              # clean fixture pushes
  python3 scripts/ocr_calibrate.py --fp-upstream [N] # N upstream single-commit
  python3 scripts/ocr_calibrate.py --plan            # what is left, no pushes
  python3 scripts/ocr_calibrate.py --source py-app --base <sha> --tip <sha> \
      --expect block --label "ad hoc range"
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import namedtuple
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "scripts" / "review-gate.py"
INSTALLER = REPO_ROOT / "scripts" / "install-kimchi-hook.sh"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "repos"
# Upstream review source: retargeted from OCR_CALIB_UPSTREAM in main()
# after validation (--fp-upstream / --source upstream only) -- see
# _retarget_upstream. None until then; there is deliberately NO built-in
# fallback path, so nothing developer-machine specific is committed.
UPSTREAM = None

# remote per source: the bare fixture (or upstream checkout) the scratch clone
# is made from. Its path IS the verdict-store identity of the push remote.
REMOTES = {
    "py-app": FIXTURES / "py-app.git",
    "electron-ts": FIXTURES / "electron-ts.git",
    "clean-ts": FIXTURES / "clean-ts.git",
    "upstream": UPSTREAM,
    # Dogfooding: the gate's own repo as a review source.
    "port": REPO_ROOT,
}

# One row per seeded bug (short shas; resolved to full at run time). The
# electron-ts history is one commit per bug, oldest first; py-app carries the
# SQLi and the missing-sender bug as its two newest commits on main.
FN_MATRIX = [
    ("py-app", "ddbb912", "48a4c0c", "block", "SQLi: get_user f-string SQL"),
    ("py-app", "48a4c0c", "1199231", "block", "missing-sender: on_command"),
    ("electron-ts", "6220061", "f8bc3db", "block",
     "nodeIntegration:true + contextIsolation:false"),
    ("electron-ts", "f8bc3db", "45c5827", "block", "orphan channel-x"),
    ("electron-ts", "45c5827", "948118d", "block", "diag:open no sender validation"),
    ("electron-ts", "948118d", "3cc8a7b", "block",
     "formatBytes->formatSize rename break"),
]

# Clean pushes: every one of these must PASS (a block is a false positive and
# is recorded verbatim). py-app's clean-variant is the branch off ddbb912 that
# lands both fixes safely in one commit.
FP_FIXTURE_MATRIX = [
    ("clean-ts", "8fcc2a1", "07cc15c", "pass", "clean: cart totals on stats helpers"),
    ("clean-ts", "07cc15c", "0544617", "pass", "clean: report builder wiring"),
    ("py-app", "a5ccc09", "ddbb912", "pass", "clean: summary reporting"),
    ("py-app", "ddbb912", "ccc0e8b", "pass",
     "clean-variant: safe user lookup + sender-checked commands"),
    ("electron-ts", "271c078", "6220061", "pass", "clean: status line for report panel"),
]

Row = namedtuple("Row", "rid source base tip expected label")

_MD_HEADER = """# Review-gate calibration matrix (C5)

Real-model pushes through the full gate pipeline (hook -> supervisor ->
sandbox clone -> `kimchi -p "/skill:review --range A..B --json"` -> verdict
store), driven by `scripts/ocr_calibrate.py`. `stub` rows prove the plumbing
with the stub worker and no model. A `block` on a `pass` row is a false
positive; its findings are recorded verbatim.

| id | source | range | expected | verdict | blocked | decision | findings | wall_s | note |
|---|---|---|---|---|---|---|---|---|---|
"""


def _git(args, cwd, env=None, timeout=300):
    """Run git, scrubbing Kimchi's per-session GIT_TRACE* vars (they point at
    temp dirs that vanish, and any git inheriting them dies with exit 128)."""
    if env is None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE")}
    proc = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                          text=True, env=env, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {cwd}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _scrub_hook_env(base):
    """A hook env with no inherited gate/reviewer/session state (test pattern
    from tests/test_async_gate.py::_env). PATH, HOME and auth stay."""
    drop = ("OCR_", "STUB_", "KIMCHI_", "GIT_TRACE", "CLAUDE_")
    return {k: v for k, v in base.items() if not k.startswith(drop)}


class _Modules:
    """rg_store + review_gate loaded with a private OCR_STATE_DIR."""

    def __init__(self, state_dir):
        os.environ["OCR_STATE_DIR"] = str(state_dir)
        scripts = str(REPO_ROOT / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import rg_store  # noqa: E402
        spec = importlib_spec("review_gate_cal", GATE)
        review_gate = load_spec(spec)
        self.rg_store, self.review_gate = rg_store, review_gate


def importlib_spec(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, str(path))
    return spec


def load_spec(spec):
    import importlib.util
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _resolve(source, short):
    """A short sha in the source's remote, as a full 40-hex sha."""
    return _git(["rev-parse", "--verify", "--quiet", short + "^{commit}"],
                REMOTES[source])


def _retarget_upstream(ap):
    """Resolve the upstream review source and fail cleanly when unusable.

    The path comes from OCR_CALIB_UPSTREAM, REQUIRED -- the repo ships no
    built-in fallback, so every user (developer or CI) sets one. Failures
    surface here, BEFORE rows are built, as a clear ap.error -- never a raw
    RuntimeError from _git mid-run ('fatal: not a git repository').
    Retargets the module-level UPSTREAM / REMOTES["upstream"] on success so
    upstream_rows() and _resolve() see the resolved path.
    """
    global UPSTREAM
    raw = os.environ.get("OCR_CALIB_UPSTREAM", "").strip()
    hint = "set OCR_CALIB_UPSTREAM to a local checkout of claude-code-review-gate"
    if not raw:
        ap.error(f"no upstream source configured -- {hint}")
    path = Path(raw)
    if not path.is_dir():
        ap.error(f"upstream source not found: {path} -- {hint}")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE")}
    proc = subprocess.run(["git", "rev-parse", "--git-dir"], cwd=str(path),
                          capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        ap.error(f"upstream source is not a git repository: {path} -- {hint}")
    UPSTREAM = path
    REMOTES["upstream"] = path
    return path


def fn_rows():
    return [Row(f"{s}-{b}-{t}", s, _resolve(s, b), _resolve(s, t), e, lbl)
            for s, b, t, e, lbl in FN_MATRIX]


def fp_fixture_rows():
    return [Row(f"{s}-{b}-{t}", s, _resolve(s, b), _resolve(s, t), e, lbl)
            for s, b, t, e, lbl in FP_FIXTURE_MATRIX]


def upstream_rows(count):
    """The newest `count` non-merge upstream commits, one single-commit range
    each, newest first (the plan's FP walk over real-commit history)."""
    out = []
    for line in _git(["log", "--oneline", f"-{count + 10}"], UPSTREAM).splitlines():
        sha, subject = line.split(" ", 1)
        sha = _git(["rev-parse", sha], UPSTREAM).strip()  # --oneline abbreviates; the store keys FULL tips
        parents = _git(["rev-list", "--parents", "-n", "1", sha], UPSTREAM).split()
        if len(parents) > 2:  # a merge: its range is not one clean step
            continue
        base = parents[1] if len(parents) == 2 else ""
        if not base:
            continue
        out.append(Row(f"upstream-{base[:7]}-{sha[:7]}", "upstream", base, sha,
                        "pass", f"upstream: {subject.strip()}"))
        if len(out) >= count:
            break
    return out


def _scratch(source, opts):
    """The per-source scratch clone (created once, refs re-pointed per range).

    core.hooksPath is pointed at an inert dir BEFORE the installer runs, so
    the repo-level pre-push lands there (inert: the harness never pushes) and
    any developer-global gate can never fire on the scratch's own git calls.
    """
    root = Path(opts.root) / "scratch" / source
    work = root / "work"
    if (work / ".git").exists():
        return work
    root.mkdir(parents=True, exist_ok=True)
    _git(["clone", "--no-hardlinks", "--quiet", str(REMOTES[source]), "work"], root)
    no_hooks = root / "no-hooks"
    no_hooks.mkdir(exist_ok=True)
    _git(["config", "core.hooksPath", str(no_hooks)], work)
    env = _scrub_hook_env(os.environ)
    env.update({
        "OCR_STATE_DIR": str(opts.state_dir),
        "KIMCHI_HOOKS_DIR": str(Path(opts.root) / "kimchi-hooks"),
        "OCR_EXT_DIR": str(Path(opts.root) / "kimchi-ext"),
    })
    proc = subprocess.run(["bash", str(INSTALLER), "--repo", str(work)],
                          capture_output=True, text=True, env=env, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"installer failed: {proc.stdout}\n{proc.stderr}")
    return work


def _point_refs(work, base, tip):
    """Make `git push origin main` describe exactly base..tip: local main at
    the tip, the remote-tracking origin/main at the base (the push never
    happens, so that ref never moves)."""
    _git(["update-ref", "refs/remotes/origin/main", base], work)
    _git(["checkout", "-q", "-B", "main", tip], work)


def _hook_env(opts, state_dir, stub=False):
    env = _scrub_hook_env(os.environ)
    env.update({
        "OCR_STATE_DIR": str(state_dir),
        # Real reviews take minutes: never answer inline, go straight to the
        # detached supervisor so the hook returns at once.
        "OCR_INLINE_BUDGET": "4",
        "OCR_TIMEOUT": str(opts.timeout),
        "KIMCHI_HOOKS_DIR": str(Path(opts.root) / "kimchi-hooks"),
        "OCR_EXT_DIR": str(Path(opts.root) / "kimchi-ext"),
    })
    if stub:
        env["OCR_STUB_WORKER"] = "1"
    return env


def _push_hook(work, env, timeout=180):
    """One synthetic push: the PreToolUse payload the Kimchi bash hook feeds
    review-gate.py. Returns (decision, reason) -- "pass" is hook silence.

    PIPE-FREE by design (wedge fix, 2026-11-09): two --fp-upstream runs
    blocked forever in select() inside subprocess.communicate() after a
    row's review completed -- the hook spawns detached trees, and any
    survivor holding an inherited stdout pipe keeps communicate() waiting
    for an EOF that never comes, even past the timeout kill. Temp files
    cannot be held open that way: no pipes, no wedge."""
    payload = json.dumps({"session_id": "calibrate", "tool_name": "bash",
                          "cwd": str(work), "input": {"command": "git push origin main"}})
    import tempfile
    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        proc = subprocess.Popen([sys.executable, str(GATE), "--mode", "hook"],
                                stdin=subprocess.PIPE, stdout=out, stderr=err,
                                text=True, cwd=str(work), env=env)
        try:
            proc.communicate(payload, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=30)
            raise
        if proc.returncode != 0:
            err.seek(0)
            raise RuntimeError(f"hook exited {proc.returncode}: {err.read()[-2000:]}")
        out.seek(0)
        text = (out.read() or "").strip()
    if not text:
        return "pass", ""
    obj = json.loads(text)
    decision = obj.get("decision")
    if decision is None:
        return "pass", ""
    return ("deny" if decision == "block" else str(decision)), str(obj.get("reason", ""))


def _load_verdict(mods, url, tip):
    try:
        return mods.rg_store.load_verdict(url, tip)
    except Exception:
        return None  # corrupt record: the hook itself will fail closed on it


def _wait_terminal(mods, work, tip, base, deadline):
    """Poll the async state + verdict store until the supervisor is done.

    The computed state path is the primary read, but a path-hash mismatch
    (observed 2026-11-09: some rows' supervisors write under a different
    async/<hash>/ dir than this side computes) left the poll blind and each
    such row burned its full deadline. The glob fallback below finds ANY
    state file for this tip under the store's async tree, whatever dir it
    landed in."""
    common = mods.review_gate._git_common_dir(str(work))
    state_path = mods.review_gate._state_path(common, tip, base)
    url = mods.rg_store.remote_url(str(work), "origin")
    state = {}
    _hb = time.monotonic() + 60
    while time.monotonic() < deadline:
        if time.monotonic() > _hb:
            _hb = time.monotonic() + 60
            _log(f"wait: still polling {tip[:7]} state={state.get('state')!r}")
        state = mods.review_gate._read_state(state_path) or {}
        if state.get("state") in ("done", "failed"):
            break
        if _load_verdict(mods, url, tip) is not None:
            break
        # Blind-poll fallback: any state file for this tip, any async dir.
        try:
            import glob as _glob
            for cand in _glob.glob(str(mods.rg_store.store_root() / "async" / "*" / f"*-{tip}.json")):
                state = mods.review_gate._read_state(cand) or state
                if state.get("state") in ("done", "failed"):
                    break
        except Exception:
            pass
        if state.get("state") in ("done", "failed"):
            break
        time.sleep(2)
    return state


def _findings_for(work, tip):
    """The findings jsonl entry the supervisor appended for this tip (the log
    is append-only, one line per completed review; head names the tip)."""
    path = Path(work) / ".git" / "review-gate-findings.jsonl"
    entry = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("head") == tip:
                    entry = obj
    except OSError:
        return []
    if not entry:
        return []
    findings = entry.get("findings")
    return [f for f in findings if isinstance(f, dict)] if isinstance(findings, list) else []


def _findings_cell(findings):
    if not findings:
        return "(none)"
    parts = []
    for f in findings:
        sev = str(f.get("severity", "?"))
        conf = f.get("confidence", "?")
        where = f"{f.get('path', '?')}:{f.get('start_line', '?')}"
        head = str(f.get("content", "") or "")[:120].replace("|", "\\|")
        parts.append(f"{sev}/{conf} {where} {head}")
    cell = "; ".join(parts)
    return (cell[:500] + "...") if len(cell) > 500 else cell


def _recorded_ids(md_path):
    """Row ids already in the matrix table (the resume set)."""
    try:
        text = Path(md_path).read_text(encoding="utf-8")
    except OSError:
        return set()
    ids = set()
    for line in text.splitlines():
        m = re.match(r"^\|\s*([a-z0-9][a-z0-9._-]*)\s*\|", line)
        if m and m.group(1) not in ("id", "---"):
            ids.add(m.group(1))
    return ids


def _append_row(md_path, row, result):
    path = Path(md_path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_MD_HEADER, encoding="utf-8")
    b7, t7 = row.base[:7], row.tip[:7]
    verdict = (result["record"] or {}).get("verdict") or result["state"].get("verdict") or "none"
    blocked = "yes" if result["blocked"] else "no"
    cell = _findings_cell(result["findings"])
    line = (f"| {row.rid} | {row.source} | {b7}..{t7} | {row.expected} | {verdict} | "
            f"{blocked} | {result['decision']} | {cell} | {result['wall']:.1f} | "
            f"{row.label}{result['note']} |\n")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


def _log(msg):
    """Stage logging: the harness's story, flushable, for wedge diagnosis."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run_row(row, opts, mods, state_dir, stub=False):
    """Push one range through the real gate and record the matrix row."""
    # rg_store resolves its root from the env at CALL time, so pointing the
    # env here retargets every poll below (and the stub run gets a store no
    # real-model row can ever be satisfied from).
    os.environ["OCR_STATE_DIR"] = str(state_dir)
    _log(f"row {row.rid}: scratch...")
    work = _scratch(row.source, opts)
    _point_refs(work, row.base, row.tip)
    url = mods.rg_store.remote_url(str(work), "origin")
    pre = _load_verdict(mods, url, row.tip)
    _log(f"row {row.rid}: pre-verdict={'hit' if pre else 'miss'} url={url}")
    env = _hook_env(opts, state_dir, stub=stub)
    t0 = time.monotonic()
    d0, r0 = _push_hook(work, env)  # pushes the review; a real review answers async
    _log(f"row {row.rid}: push#1 decision={d0} reason={r0[:100]!r}")
    state = _wait_terminal(mods, work, row.tip, row.base,
                           time.monotonic() + opts.timeout + 240)
    _log(f"row {row.rid}: wait done state={state.get('state')} attempts={state.get('attempts')}")
    decision, reason = _push_hook(work, env)  # the verdict-backed decision
    _log(f"row {row.rid}: push#2 decision={decision} reason={reason[:100]!r}")
    wall = time.monotonic() - t0
    record = _load_verdict(mods, url, row.tip)
    findings = _findings_for(work, row.tip)
    blocked = bool((record or {}).get("blocked")) or decision == "deny"
    note = ""
    if stub:
        note = " [stub worker]"
    elif pre is not None:
        note = " [cached verdict]"
    if state.get("state") == "failed":
        note += f" [state=failed: {str(state.get('detail'))[:120]}]"
    if state.get("state") not in ("done", "failed") and record is None:
        note += " [timed out waiting for the supervisor]"
    result = {"record": record, "state": state, "decision": decision, "reason": reason,
              "findings": findings, "blocked": blocked, "wall": wall, "note": note}
    _append_row(opts.md, row, result)
    ok = (decision == "deny") if row.expected == "block" else (decision == "pass")
    print(f"[{'OK ' if ok else 'MISS'}] {row.rid}: expected {row.expected}, "
          f"decision {decision}, verdict {(record or {}).get('verdict')}, "
          f"{len(findings)} finding(s), {wall:.1f}s{note}")
    if not ok and reason:
        print(f"      reason: {reason[:300]}")
    return result


def _build_rows(args):
    rows = []
    if args.stub:
        s, b, t = "py-app", "a5ccc09", "ddbb912"
        rows.append(Row(f"stub-{s}-{b}-{t}", s, _resolve(s, b), _resolve(s, t),
                        "pass", "plumbing proof"))
    if args.fn:
        rows += fn_rows()
    if args.fp:
        rows += fp_fixture_rows()
    if args.fp_upstream is not None:
        rows += upstream_rows(args.fp_upstream)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="upstream source (--fp-upstream N, --source upstream): taken "
               "from OCR_CALIB_UPSTREAM (required; the repo ships no "
               "built-in default); it must exist and be a git repository, "
               "or the run fails before any rows are built.",
    )
    ap.add_argument("--stub", action="store_true", help="one stub-worker push")
    ap.add_argument("--fn", action="store_true", help="the 6 seeded-bug pushes")
    ap.add_argument("--fp", action="store_true", help="clean fixture pushes")
    ap.add_argument("--fp-upstream", type=int, default=None, metavar="N",
                    help="N upstream single-commit ranges, newest first")
    ap.add_argument("--source", help="ad-hoc range: the remote source")
    ap.add_argument("--base", help="ad-hoc range: base sha")
    ap.add_argument("--tip", help="ad-hoc range: tip sha")
    ap.add_argument("--expect", default="pass", choices=["pass", "block"],
                    help="ad-hoc range: expected decision")
    ap.add_argument("--label", default="ad hoc", help="ad-hoc range: note")
    ap.add_argument("--timeout", type=int, default=900,
                    help="per-push wall cap, passed to the gate as OCR_TIMEOUT")
    ap.add_argument("--root", default="/tmp/ocr-calib",
                    help="scratch/state root (default /tmp/ocr-calib)")
    ap.add_argument("--md", default=str(REPO_ROOT / "docs" / "calibration.md"),
                    help="the calibration matrix to append to")
    ap.add_argument("--plan", action="store_true",
                    help="print pending rows and exit (no pushes)")
    ap.add_argument("--clean-scratch", action="store_true",
                    help="remove the scratch root (kept state + matrix) on exit")
    args = ap.parse_args(argv)
    args.state_dir = str(Path(args.root) / "state")

    if args.fp_upstream is not None or args.source == "upstream":
        _retarget_upstream(ap)

    rows = _build_rows(args)
    if args.source:
        if not (args.base and args.tip):
            ap.error("--source needs --base and --tip")
        s = args.source
        rows.append(Row(f"{s}-{args.base[:7]}-{args.tip[:7]}", s,
                        _resolve(s, args.base), _resolve(s, args.tip),
                        args.expect, args.label))
    if args.plan:
        done = _recorded_ids(args.md)
        for r in rows:
            print(f"{'DONE ' if r.rid in done else 'TODO '} {r.rid} "
                  f"(expect {r.expected}) {r.label}")
        print(f"{len([r for r in rows if r.rid not in done])} pending / {len(rows)}")
        return 0
    if not rows:
        ap.error("nothing to do: pass --stub/--fn/--fp/--fp-upstream/--plan")

    mods = _Modules(args.state_dir)
    done = _recorded_ids(args.md)
    pending = [r for r in rows if r.rid not in done]
    print(f"{len(pending)} pending of {len(rows)} requested "
          f"(state: {args.state_dir}, matrix: {args.md})")
    exit_code = 0
    for row in pending:
        # the stub run gets its own store so its verdict can never satisfy a
        # real-model row for the same (remote, tip)
        state_dir = str(Path(args.root) / "state-stub") if row.rid.startswith("stub-") \
            else args.state_dir
        try:
            result = run_row(row, args, mods, state_dir, stub=row.rid.startswith("stub-"))
            ok = (result["decision"] == "deny") if row.expected == "block" \
                else (result["decision"] == "pass")
            if not ok:
                exit_code = 1
        except Exception as exc:  # one bad range must not kill the matrix
            exit_code = 1
            print(f"[FAIL] {row.rid}: {type(exc).__name__}: {exc}")
    if args.clean_scratch:
        import shutil
        shutil.rmtree(Path(args.root) / "scratch", ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
