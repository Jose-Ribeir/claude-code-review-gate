#!/usr/bin/env python3
"""Quality guard: prove a speed change did not make the gate weaker.

Settled decision (docs/specs/pre-push-review-covers-only-what-changed.md,
"Quality guard"): there is no time target, so every speed change must be
checkable. A change that loses a finding is reverted even if it is faster.

Three commands, one outcome format (``quality-guard-outcome/v1``, JSON):

``catch-rate``
    Runs the four planted-defect fixtures (tests/fixtures/miss_categories,
    M1..M4) through the REAL gate (``review-gate.py --mode hook``) with the
    stub reviewer and records, per category, whether the planted defect came
    back as a finding. Cheap, no model, runs in CI (tests/test_quality_guard.py).
    The ``scripted`` reviewer reports a defect on every planted location it is
    shown (and on every impact site for M3), so it guards the pipeline -- file
    selection, context hand-off, finding surfacing. The ``weakened`` reviewer
    reports nothing: the check must FAIL for it (proof the guard works).
    Model judgment is the real-model run's job, not this one's.

``real-model``
    Runs the gate on three fixed inputs and records findings and wall time with
    a split into model turns, lenses, tests and probes: ``fix-after-block`` (a
    2-line fix pushed after a blocked push), ``mr-1000`` (a 1000-line merge
    request) and ``recorded-range`` (``1dacf08..673b61d``, about 10,000 lines,
    built from this repo's history; fails clearly if it is missing). It spends
    real model money: it states the expected cost and asks for confirmation
    first. By hand, once per phase. ``--stub`` swaps in the stub reviewer
    (no cost, no confirmation) to prove the plumbing.

``compare BEFORE.json AFTER.json``
    Diffs two outcomes of the same kind and flags every finding (real-model) or
    category (catch-rate) the later one lost, with the time delta. Exit 1 if
    anything was lost.

Two gate versions are exercised by repeating ``--gate-ref`` (a git ref of this
repo, extracted with ``git archive``) or ``--gate-root`` (a checkout): the
first is "before", the last is "after"; the outcomes are compared at the end.

Exit codes: 0 fine, 1 a category/finding was lost or a catch check failed,
2 bad input or environment (missing range, no confirmation, bad file).

Python 3.9 compatible.
"""
import argparse
import io
import json
import math
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "quality-guard-outcome/v1"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "miss_categories"

CATEGORIES = ("m1", "m2", "m3", "m4")
REVIEWERS = ("scripted", "weakened")
REAL_INPUTS = ("fix-after-block", "mr-1000", "recorded-range")
RECORDED_RANGE = "1dacf08..673b61d"

# Rough planning figures for the confirmation prompt, NOT measurements: one
# review call costs about FIXED, plus PER_1K per thousand changed lines. Replace
# them with the baseline numbers (ticket 08) via --cost-fixed / --cost-per-1k.
DEFAULT_COST_FIXED_USD = 0.40
DEFAULT_COST_PER_1K_LINES_USD = 0.60
CHUNK_LINES = 3000  # the spec's first-guess chunk size, used only for the estimate


class GuardError(Exception):
    """Bad input or environment: reported on stderr, exit code 2."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _git(args, cwd, check=True):
    proc = subprocess.run(["git"] + list(args), cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise GuardError("git %s failed in %s: %s" % (" ".join(args), cwd,
                                                      (proc.stderr or proc.stdout).strip()))
    return proc.stdout


def _rmtree(path):
    def _onerror(func, p, _exc):  # read-only git objects on Windows
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(str(path), onerror=_onerror)


def _load_builders():
    """The fixture builders (tests/fixtures/miss_categories/builders.py): the
    fixture repos always come from THIS tree, so before and after runs of two
    gate versions see identical inputs."""
    if not FIXTURES_DIR.is_dir():
        raise GuardError("fixture directory missing: %s" % FIXTURES_DIR)
    if str(FIXTURES_DIR) not in sys.path:
        sys.path.insert(0, str(FIXTURES_DIR))
    import builders  # noqa: E402
    return builders


# ---------------------------------------------------------------------------
# Gate versions
# ---------------------------------------------------------------------------

def resolve_gate(workdir, gate_ref=None, gate_root=None):
    """(gate_root Path, label). A ref is extracted with ``git archive`` into
    ``workdir`` (an unmodified snapshot, whatever the working tree holds)."""
    if gate_ref and gate_root:
        raise GuardError("give --gate-ref or --gate-root, not both")
    if gate_ref:
        proc = subprocess.run(["git", "archive", "--format=zip", gate_ref],
                              cwd=str(REPO_ROOT), capture_output=True)
        if proc.returncode != 0:
            raise GuardError("cannot read gate version %r from %s: %s" % (
                gate_ref, REPO_ROOT, proc.stderr.decode("utf-8", "replace").strip()))
        dest = Path(workdir) / ("gate-%s" % (len(list(Path(workdir).glob("gate-*"))) + 1))
        dest.mkdir(parents=True)
        with zipfile.ZipFile(io.BytesIO(proc.stdout)) as zf:
            zf.extractall(str(dest))
        root, label = dest, str(gate_ref)
    else:
        root = Path(gate_root) if gate_root else REPO_ROOT
        label = str(gate_root) if gate_root else "working-tree"
    if not (root / "scripts" / "review-gate.py").is_file():
        raise GuardError("no scripts/review-gate.py under gate root %s" % root)
    return root, label


def _base_env(stub, gate_root, state_dir, data_dir, timeout):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("STUB_") and not k.startswith("GIT_TRACE")}
    if stub:
        env = {k: v for k, v in env.items() if not k.startswith("OCR_")}
    else:
        # Switches that would change WHAT is measured, not how fast.
        for k in ("OCR_REVIEWER_CMD", "OCR_FAIL_OPEN", "OCR_ADVISORY", "OCR_STUB_WORKER"):
            env.pop(k, None)
    env["OCR_STATE_DIR"] = str(state_dir)
    env["CLAUDE_PLUGIN_DATA"] = str(data_dir)
    # Wait for the verdict inside the hook so wall time is what a developer feels.
    env["OCR_INLINE_BUDGET"] = str(int(timeout))
    env["OCR_TIMEOUT"] = str(int(timeout))
    if stub:
        stub_path = Path(gate_root) / "tests" / "stub_reviewer.py"
        if not stub_path.is_file():
            raise GuardError("gate version has no tests/stub_reviewer.py: %s" % stub_path)
        env["OCR_REVIEWER_CMD"] = '"%s" "%s"' % (sys.executable, stub_path)
    return env


def _hook_push(gate_root, work, env, timeout):
    """One synthetic ``git push origin main`` through the gate's hook mode.
    Returns (decision, reason); "pass" is hook silence. Pipe-free (temp files)
    so a detached survivor cannot wedge the read (same reason as ocr_calibrate)."""
    payload = json.dumps({"session_id": "quality-guard", "tool_name": "Bash",
                          "cwd": str(work),
                          "tool_input": {"command": "git push origin main"}})
    gate = Path(gate_root) / "scripts" / "review-gate.py"
    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        proc = subprocess.Popen([sys.executable, str(gate), "--mode", "hook"],
                                stdin=subprocess.PIPE, stdout=out, stderr=err,
                                text=True, cwd=str(work), env=env)
        try:
            proc.communicate(payload, timeout=timeout + 60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=30)
            raise GuardError("gate hook did not return within %ss" % (timeout + 60))
        if proc.returncode != 0:
            err.seek(0)
            raise GuardError("gate hook exited %s: %s" % (proc.returncode, err.read()[-1500:]))
        out.seek(0)
        text = (out.read() or "").strip()
    if not text:
        return "pass", ""
    obj = json.loads(text)
    decision = obj.get("decision")
    if decision is None:
        return "pass", ""
    return ("deny" if decision == "block" else str(decision)), str(obj.get("reason", ""))


def _telemetry_record(work, tip):
    """The gate's run record for ``tip`` (the newest one), or None."""
    common = _git(["rev-parse", "--git-common-dir"], work).strip()
    common = Path(common) if os.path.isabs(common) else Path(work) / common
    found = None
    for path in sorted((common / "review-gate-telemetry").glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("kind") != "tier_call" \
                    and rec.get("tip") == tip:
                found = rec
    return found


def _wait_record(work, tip, deadline):
    while True:
        rec = _telemetry_record(work, tip)
        if rec is not None or time.monotonic() > deadline:
            return rec
        time.sleep(0.5)


def time_split(record):
    """Where the time went, from one run record. The buckets are measured
    independently and can overlap (chunks and checks run in parallel); they are
    a guide to what to cut, not an additive breakdown of ``wall_s``.

    model   sum of reviewer/resolver/filter call seconds, and their turns
    lenses  final-review lens time (phases or ``lenses`` entries naming a lens)
    tests   command-check seconds (project tests/linters the gate runs)
    probes  deterministic context work: every phase that is not a model call
    """
    record = record or {}
    calls = [c for c in (record.get("calls") or []) if isinstance(c, dict)]
    phases = {}
    for ph in record.get("phases") or []:
        if isinstance(ph, dict) and ph.get("name"):
            phases[ph["name"]] = phases.get(ph["name"], 0.0) + float(ph.get("seconds") or 0)
    lens = sum(s for n, s in phases.items() if "lens" in n)
    lens += sum(float(x.get("seconds") or 0) for x in (record.get("lenses") or [])
                if isinstance(x, dict))
    model_phases = ("review", "resolve", "recheck", "finish", "failed")
    return {
        "model_s": round(sum(float(c.get("seconds") or 0) for c in calls), 2),
        "model_calls": len(calls),
        "model_turns": sum(int(c.get("turns") or 0) for c in calls),
        "lenses_s": round(lens, 2),
        "tests_s": round(sum(float(c.get("seconds") or 0)
                             for c in (record.get("command_checks") or [])
                             if isinstance(c, dict)), 2),
        "probes_s": round(sum(s for n, s in phases.items()
                              if n not in model_phases and "lens" not in n), 2),
    }


def _norm_finding(f):
    return {"path": str(f.get("path") or ""),
            "start_line": f.get("start_line"),
            "severity": str(f.get("severity") or ""),
            "category": str(f.get("category") or ""),
            "content": str(f.get("content") or "")[:240]}


def review_push(gate_root, work, tip, env, timeout):
    """Run one gate review of ``tip`` and measure it. Never raises on a missing
    record: that becomes an ``error`` the comparison treats as a failure."""
    t0 = time.monotonic()
    decision, reason = _hook_push(gate_root, work, env, timeout)
    record = _wait_record(work, tip, t0 + timeout)
    wall = round(time.monotonic() - t0, 2)
    case = {"decision": decision, "wall_s": wall, "findings": [], "split": {}}
    if record is None:
        case["error"] = "no gate run record for %s within %ss" % (tip[:10], timeout)
        return case
    case["verdict"] = record.get("verdict")
    case["findings"] = [_norm_finding(f) for f in (record.get("findings") or [])
                        if isinstance(f, dict)]
    case["split"] = time_split(record)
    return case


# ---------------------------------------------------------------------------
# catch-rate: the planted-defect fixtures with the stub reviewer
# ---------------------------------------------------------------------------

def _planted(category, info):
    """(paths a defect must be reported on, extra stub env) for one fixture."""
    if category == "m3":
        # The stranded callers are the planted defect: every spec file, not just
        # the helper. The stub reports each impact site it is handed.
        return list(info["spec_files"]), {"STUB_IMPACT_BREAK": "1"}
    key = {"m1": "mount", "m2": "src", "m4": "src"}[category]
    return [info[key]], {}


def _reviewer_env(reviewer, category, info):
    if reviewer == "weakened":
        return {"STUB_VERDICT": "pass"}
    _paths, extra = _planted(category, info)
    scripted = {p: {"severity": "high", "content": "planted defect (%s)" % category}
                for p in info["changed_files"]}
    env = {"STUB_FINDINGS_FOR": json.dumps(scripted), "STUB_VERDICT": "pass"}
    env.update(extra)
    return env


def run_catch_rate(gate_root=None, gate_ref=None, reviewer="scripted",
                   categories=None, label=None, timeout=120):
    """One catch-rate outcome for one gate version."""
    if reviewer not in REVIEWERS:
        raise GuardError("unknown reviewer %r (choose %s)" % (reviewer, ", ".join(REVIEWERS)))
    cats = list(categories or CATEGORIES)
    for c in cats:
        if c not in CATEGORIES:
            raise GuardError("unknown category %r (choose %s)" % (c, ", ".join(CATEGORIES)))
    builders = _load_builders()
    tmp = Path(tempfile.mkdtemp(prefix="rg-qg-"))
    try:
        root, auto_label = resolve_gate(tmp, gate_ref, gate_root)
        cases = {}
        for cat in cats:
            base = tmp / cat
            base.mkdir()
            info = getattr(builders, "build_" + cat)(str(base / "work"))
            env = _base_env(True, root, base / "state", base / "data", timeout)
            env.update(_reviewer_env(reviewer, cat, info))
            case = review_push(root, info["root"], info["tip"], env, timeout)
            required, _extra = _planted(cat, info)
            got = {f["path"] for f in case["findings"]}
            missing = [p for p in required if p not in got]
            case["required_paths"] = required
            case["missing_paths"] = missing
            case["caught"] = not missing and "error" not in case
            cases[cat] = case
        return {"schema": SCHEMA, "kind": "catch-rate", "label": label or auto_label,
                "reviewer": reviewer, "cases": cases}
    finally:
        _rmtree(tmp)


# ---------------------------------------------------------------------------
# real-model: three fixed inputs
# ---------------------------------------------------------------------------

_DB_BASE = '''\
"""User lookups."""


def find_user(conn, name):
    """Return the rows for one user name."""
    return conn.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchall()
'''

_DB_BAD = '''\
"""User lookups."""


def find_user(conn, name):
    """Return the rows for one user name."""
    query = "SELECT * FROM users WHERE name = '" + name + "'"
    return conn.execute(query).fetchall()
'''

_DB_FIXED = '''\
"""User lookups."""


def find_user(conn, name):
    """Return the rows for one user name."""
    query = "SELECT * FROM users WHERE name = ?"
    return conn.execute(query, (name,)).fetchall()
'''
_DB_PATH = "app/db.py"


def _module_text(index, functions=24):
    """~40 lines per function-block of plain arithmetic helpers: reviewable, no
    defect (the planted one lives in app/db.py)."""
    out = ['"""Generated helper module %d."""' % index, ""]
    for j in range(functions):
        out += ["", "def helper_%d_%d(values, scale=%d):" % (index, j, j + 1),
                '    """Scale and total the values (variant %d)."""' % j,
                "    total = 0",
                "    for value in values:",
                "        total += value * scale",
                "    return total", ""]
    return "\n".join(out) + "\n"


def _lines(work, base, tip):
    out = _git(["diff", "--shortstat", base, tip], work)
    nums = [int(tok) for tok in out.replace(",", " ").split() if tok.isdigit()]
    # "N files changed, I insertions(+), D deletions(-)": files, then ins/del
    return sum(nums[1:]) if len(nums) > 1 else 0


def _count_commits(work, base, tip):
    return int(_git(["rev-list", "--count", "%s..%s" % (base, tip)], work).strip() or 0)


def _step(work, label, base, tip, measured, planted=None):
    return {"label": label, "work": str(work), "base": base, "tip": tip,
            "lines": _lines(work, base, tip), "commits": _count_commits(work, base, tip),
            "measured": measured, "planted": planted or []}


def prepare_fix_after_block(builders, root):
    """A blocked push, then a 2-line fix. The fix push is the measured one; the
    blocked push runs first (it is what makes the second one "after a block")."""
    work = root / "fix-after-block" / "work"
    info = builders.init_repo(str(work), {_DB_PATH: _DB_BASE})
    base = builders.git(["rev-parse", "HEAD"], work).strip()
    bad = builders.commit(str(work), {_DB_PATH: _DB_BAD}, "add query lookup")
    # Two steps, one repo: the fix commit is added after the first review.
    first = _step(work, "blocked push (SQL built by string concatenation)", base, bad,
                  False, [_DB_PATH])
    return {"name": "fix-after-block", "work": str(work), "origin": info.get("origin"),
            "steps": [first], "after_first": ("fix", _DB_FIXED, base)}


def prepare_mr_1000(builders, root):
    work = root / "mr-1000" / "work"
    builders.init_repo(str(work), {"README.md": "# service\n"})
    base = builders.git(["rev-parse", "HEAD"], work).strip()
    files = {"app/mod_%02d.py" % i: _module_text(i) for i in range(5)}
    files[_DB_PATH] = _DB_BAD
    tip = builders.commit(str(work), files, "add the lookup service")
    return {"name": "mr-1000", "work": str(work), "steps": [
        _step(work, "1000-line merge request (one SQL-injection defect)", base, tip,
              True, [_DB_PATH])]}


def prepare_recorded_range(builders, root, repo, rng):
    """A clone of ``repo`` at the range tip, with origin/main pinned at the base."""
    try:
        base_s, tip_s = rng.split("..")
    except ValueError:
        raise GuardError("range must look like BASE..TIP, got %r" % rng)
    shas = []
    for sha in (base_s, tip_s):
        proc = subprocess.run(["git", "rev-parse", "--verify", "-q", sha + "^{commit}"],
                              cwd=str(repo), capture_output=True, text=True)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise GuardError(
                "the recorded range %s is not in the history of %s (commit %s is missing). "
                "It is built from the repo history: fetch the full history "
                "(`git fetch --unshallow` / `git fetch origin`) or pass --repo with a "
                "clone that has it." % (rng, repo, sha))
        shas.append(proc.stdout.strip())
    base, tip = shas
    work = root / "recorded-range" / "work"
    work.parent.mkdir(parents=True)
    _git(["clone", "-q", str(repo), str(work)], root)
    for k, v in (("user.email", "t@t.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(["config", k, v], work)
    stray = work.parent / "no-hooks"
    stray.mkdir()
    _git(["config", "core.hooksPath", str(stray)], work)
    _git(["update-ref", "refs/remotes/origin/main", base], work)
    _git(["checkout", "-q", "-B", "main", tip], work)
    return {"name": "recorded-range", "work": str(work), "steps": [
        _step(work, "recorded range %s" % rng, base, tip, True)]}


def prepare_inputs(names, root, repo=None, recorded_range=RECORDED_RANGE):
    """Build every input BEFORE anything is spent, so a missing range fails first."""
    builders = _load_builders()
    prepared = []
    for name in names:
        if name == "fix-after-block":
            prepared.append(prepare_fix_after_block(builders, root))
        elif name == "mr-1000":
            prepared.append(prepare_mr_1000(builders, root))
        elif name == "recorded-range":
            prepared.append(prepare_recorded_range(
                builders, root, Path(repo) if repo else REPO_ROOT, recorded_range))
        else:
            raise GuardError("unknown input %r (choose %s)" % (name, ", ".join(REAL_INPUTS)))
    return prepared


def estimate_cost(prepared, versions=1, fixed=DEFAULT_COST_FIXED_USD,
                  per_1k=DEFAULT_COST_PER_1K_LINES_USD):
    """(total_usd, per-input rows). One review per step; a step over the chunk
    size is several calls. The fix-after-block second push is ~2 lines."""
    rows = []
    for inp in prepared:
        usd = 0.0
        lines = 0
        for st in inp["steps"]:
            calls = max(1, math.ceil(st["lines"] / float(CHUNK_LINES)))
            usd += calls * fixed + per_1k * st["lines"] / 1000.0
            lines += st["lines"]
        if inp.get("after_first"):  # the 2-line fix review
            usd += fixed
            lines += 2
        rows.append({"input": inp["name"], "lines": lines, "usd": round(usd * versions, 2)})
    return round(sum(r["usd"] for r in rows), 2), rows


def _stub_env_for(inp):
    if inp["name"] in ("fix-after-block", "mr-1000"):
        return {"STUB_FINDINGS_FOR": json.dumps({
            _DB_PATH: {"severity": "high", "content": "SQL built from user input"}})}
    return {"STUB_VERDICT": "pass"}


def run_real_model(prepared, gate_root, label, stub=False, timeout=900, log=print):
    """Run every prepared input against one gate version -> one outcome."""
    cases = {}
    tmp = Path(tempfile.mkdtemp(prefix="rg-qg-env-"))
    try:
        for n, inp in enumerate(prepared):
            work = Path(inp["work"])
            env = _base_env(stub, gate_root, tmp / ("state-%d" % n),
                            tmp / ("data-%d" % n), timeout)
            if stub:
                env.update(_stub_env_for(inp))
            steps = list(inp["steps"])
            case = None
            for st in steps:
                log("  [%s] %s: %d lines, %d commits" % (inp["name"], st["label"],
                                                         st["lines"], st["commits"]))
                case = review_push(gate_root, work, st["tip"], env, timeout)
                case.update({"label": st["label"], "lines": st["lines"],
                             "commits": st["commits"]})
            after = inp.get("after_first")
            if after:  # the fix, committed on top of the blocked tip
                _kind, text, _base = after
                fix_tip = _commit(work, {_DB_PATH: text}, "parametrize the query")
                log("  [%s] 2-line fix after the block" % inp["name"])
                case = review_push(gate_root, work, fix_tip, env, timeout)
                case.update({"label": "2-line fix after the block", "lines": 2,
                             "commits": 1})
            cases[inp["name"]] = case
        return {"schema": SCHEMA, "kind": "real-model", "label": label,
                "stub": bool(stub), "cases": cases}
    finally:
        _rmtree(tmp)


def _commit(work, files, message):
    for rel, text in files.items():
        full = Path(work) / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding="utf-8")
    _git(["add"] + list(files), work)
    _git(["commit", "-q", "-m", message], work)
    return _git(["rev-parse", "HEAD"], work).strip()


# ---------------------------------------------------------------------------
# Comparison and reporting
# ---------------------------------------------------------------------------

def _finding_key(kind, f):
    """Identity of a finding across two runs. Catch-rate findings are scripted,
    so their line is stable; a real model words and anchors things differently
    each run, so a real-model finding is its file and category."""
    if kind == "catch-rate":
        return "%s:%s [%s]" % (f.get("path"), f.get("start_line"), f.get("category"))
    return "%s [%s]" % (f.get("path"), f.get("category"))


def _check_outcome(obj, where):
    if not isinstance(obj, dict) or obj.get("schema") != SCHEMA \
            or not isinstance(obj.get("cases"), dict):
        raise GuardError("%s is not a %s file" % (where, SCHEMA))
    return obj


def compare_outcomes(before, after):
    """What the later run lost, gained and cost relative to the earlier one."""
    _check_outcome(before, "before")
    _check_outcome(after, "after")
    kind = before.get("kind")
    if kind != after.get("kind"):
        raise GuardError("cannot compare a %s outcome with a %s outcome"
                         % (kind, after.get("kind")))
    result = {"kind": kind, "before": before.get("label"), "after": after.get("label"),
              "lost_cases": [], "lost_categories": [], "lost_findings": [],
              "gained_findings": [], "errors": [], "timing": {}}
    for name, b in before["cases"].items():
        a = after["cases"].get(name)
        if a is None:
            result["lost_cases"].append(name)
            continue
        if a.get("error"):
            result["errors"].append("%s: %s" % (name, a["error"]))
        if kind == "catch-rate" and b.get("caught") and not a.get("caught"):
            result["lost_categories"].append(name)
        bkeys = {_finding_key(kind, f): f for f in b.get("findings") or []}
        akeys = {_finding_key(kind, f): f for f in a.get("findings") or []}
        for key in sorted(set(bkeys) - set(akeys)):
            result["lost_findings"].append({"case": name, "finding": key,
                                            "content": bkeys[key].get("content", "")})
        for key in sorted(set(akeys) - set(bkeys)):
            result["gained_findings"].append({"case": name, "finding": key,
                                              "content": akeys[key].get("content", "")})
        timing = {"wall_s": [b.get("wall_s"), a.get("wall_s")]}
        for k in sorted(set(b.get("split") or {}) | set(a.get("split") or {})):
            timing[k] = [(b.get("split") or {}).get(k), (a.get("split") or {}).get(k)]
        result["timing"][name] = timing
    result["ok"] = not (result["lost_cases"] or result["lost_categories"]
                        or result["lost_findings"] or result["errors"])
    return result


def _fmt_delta(pair):
    b, a = pair
    if b is None or a is None:
        return "%s -> %s" % ("-" if b is None else b, "-" if a is None else a)
    d = round(a - b, 2)
    return "%s -> %s (%s%s)" % (b, a, "+" if d > 0 else "", d)


def format_comparison(cmp_):
    out = ["Comparison (%s): before=%s  after=%s" % (cmp_["kind"], cmp_["before"], cmp_["after"])]
    for name in cmp_["lost_cases"]:
        out.append("LOST CASE      %s: not present in the later run" % name)
    for name in cmp_["lost_categories"]:
        out.append("LOST CATEGORY  %s: caught before, no longer caught" % name)
    for item in cmp_["lost_findings"]:
        out.append("LOST FINDING   %s: %s  %s" % (item["case"], item["finding"], item["content"]))
    for err in cmp_["errors"]:
        out.append("UNMEASURED     %s" % err)
    for item in cmp_["gained_findings"]:
        out.append("new finding    %s: %s" % (item["case"], item["finding"]))
    out.append("Time (seconds, before -> after):")
    for name, timing in cmp_["timing"].items():
        out.append("  %s" % name)
        for k, pair in timing.items():
            out.append("    %-12s %s" % (k, _fmt_delta(pair)))
    out.append("RESULT: " + ("OK, nothing lost" if cmp_["ok"]
                             else "FAIL, the later run lost findings or could not be measured"))
    return "\n".join(out)


def format_outcome(outcome):
    out = ["Outcome (%s) label=%s%s" % (
        outcome["kind"], outcome.get("label"),
        "  reviewer=%s" % outcome["reviewer"] if outcome.get("reviewer") else "")]
    for name, case in outcome["cases"].items():
        head = name
        if outcome["kind"] == "catch-rate":
            head += "  " + ("CAUGHT" if case.get("caught") else "MISSED")
        out.append(head)
        if case.get("error"):
            out.append("  ERROR: %s" % case["error"])
        if case.get("missing_paths"):
            out.append("  missing findings on: %s" % ", ".join(case["missing_paths"]))
        for f in case.get("findings") or []:
            out.append("  [%s] %s:%s  %s" % (f["severity"], f["path"], f["start_line"],
                                             f["content"][:100]))
        if not case.get("findings"):
            out.append("  (no findings)")
        s = case.get("split") or {}
        out.append("  wall %ss | model %ss (%s calls, %s turns) | lenses %ss | tests %ss | "
                   "probes %ss" % (case.get("wall_s"), s.get("model_s", 0),
                                   s.get("model_calls", 0), s.get("model_turns", 0),
                                   s.get("lenses_s", 0), s.get("tests_s", 0),
                                   s.get("probes_s", 0)))
    return "\n".join(out)


def catch_failed(outcome):
    return [n for n, c in outcome["cases"].items() if not c.get("caught")]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _write_outcome(out_dir, outcome, n):
    if not out_dir:
        return
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(outcome.get("label")))
    path = out_dir / ("%s-%d-%s.json" % (outcome["kind"], n, safe))
    path.write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8")
    print("wrote %s" % path)


def _versions(args):
    """[(gate_ref, gate_root)] for the requested versions (default: the tree)."""
    refs = [(r, None) for r in (args.gate_ref or [])]
    if args.gate_root:
        refs.append((None, args.gate_root))
    return refs or [(None, None)]


def cmd_catch_rate(args):
    outcomes = []
    cats = args.categories.split(",") if args.categories else None
    for n, (ref, root) in enumerate(_versions(args), 1):
        outcome = run_catch_rate(gate_root=root, gate_ref=ref, reviewer=args.reviewer,
                                 categories=cats, timeout=args.timeout)
        print(format_outcome(outcome))
        _write_outcome(args.out_dir, outcome, n)
        outcomes.append(outcome)
    failed = catch_failed(outcomes[-1])
    if len(outcomes) > 1:
        cmp_ = compare_outcomes(outcomes[0], outcomes[-1])
        print(format_comparison(cmp_))
        return 0 if cmp_["ok"] else 1
    if failed:
        print("CATCH-RATE FAIL: not caught: %s" % ", ".join(failed))
        return 1
    print("CATCH-RATE OK: every planted defect was caught")
    return 0


def cmd_compare(args):
    try:
        before = json.loads(Path(args.before).read_text(encoding="utf-8"))
        after = json.loads(Path(args.after).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GuardError("cannot read outcome file: %s" % exc)
    cmp_ = compare_outcomes(before, after)
    print(format_comparison(cmp_))
    return 0 if cmp_["ok"] else 1


def confirm_cost(total, rows, versions, stub, assume_yes, ask=input, interactive=None):
    """State the expected cost; True only on an explicit yes."""
    print("Real-model run: %d gate version(s), inputs:" % versions)
    for r in rows:
        print("  %-16s %6d changed lines   about $%.2f" % (r["input"], r["lines"], r["usd"]))
    print("Expected cost: about $%.2f (planning figures, not measurements; see "
          "--cost-fixed / --cost-per-1k-lines)." % total)
    if stub:
        print("--stub: the stub reviewer replaces the model, no cost.")
        return True
    if assume_yes:
        print("--yes given: not asking.")
        return True
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        print("Refusing to spend model money without confirmation: run interactively or "
              "pass --yes.", file=sys.stderr)
        return False
    return str(ask("Proceed? [y/N] ")).strip().lower() in ("y", "yes")


def cmd_real_model(args):
    names = args.inputs.split(",") if args.inputs else list(REAL_INPUTS)
    versions = _versions(args)
    tmp = Path(tempfile.mkdtemp(prefix="rg-qg-real-"))
    try:
        prepared = prepare_inputs(names, tmp, repo=args.repo,
                                  recorded_range=args.recorded_range)
        total, rows = estimate_cost(prepared, versions=len(versions),
                                    fixed=args.cost_fixed, per_1k=args.cost_per_1k)
        if not confirm_cost(total, rows, len(versions), args.stub, args.yes):
            print("Aborted before spending anything.", file=sys.stderr)
            return 2
        outcomes = []
        gate_tmp = Path(tempfile.mkdtemp(prefix="rg-qg-gates-"))
        try:
            for n, (ref, root) in enumerate(versions, 1):
                gate_root, label = resolve_gate(gate_tmp, ref, root)
                if n > 1:  # each version starts from the same pristine inputs
                    _rmtree(tmp)
                    tmp = Path(tempfile.mkdtemp(prefix="rg-qg-real-"))
                    prepared = prepare_inputs(names, tmp, repo=args.repo,
                                              recorded_range=args.recorded_range)
                print("Running gate version %s" % label)
                outcome = run_real_model(prepared, gate_root, label, stub=args.stub,
                                         timeout=args.timeout)
                print(format_outcome(outcome))
                _write_outcome(args.out_dir, outcome, n)
                outcomes.append(outcome)
        finally:
            _rmtree(gate_tmp)
        if len(outcomes) > 1:
            cmp_ = compare_outcomes(outcomes[0], outcomes[-1])
            print(format_comparison(cmp_))
            return 0 if cmp_["ok"] else 1
        return 0
    finally:
        _rmtree(tmp)


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)

    def versions(p):
        p.add_argument("--gate-ref", action="append",
                       help="git ref of this repo to run as a gate version; repeat for "
                            "before and after")
        p.add_argument("--gate-root", help="a gate checkout to run (default: this tree)")
        p.add_argument("--out-dir", help="write each outcome as JSON into this directory")
        p.add_argument("--timeout", type=int, default=120, help="seconds per review")

    p = sub.add_parser("catch-rate", help="planted-defect fixtures with the stub reviewer")
    versions(p)
    p.add_argument("--reviewer", choices=REVIEWERS, default="scripted")
    p.add_argument("--categories", help="comma list of %s (default all)" % ",".join(CATEGORIES))
    p.set_defaults(func=cmd_catch_rate)

    p = sub.add_parser("real-model", help="three fixed inputs, findings and wall time")
    versions(p)
    p.set_defaults(timeout=900)
    p.add_argument("--inputs", help="comma list of %s (default all)" % ",".join(REAL_INPUTS))
    p.add_argument("--repo", help="repo holding the recorded range (default: this repo)")
    p.add_argument("--recorded-range", default=RECORDED_RANGE)
    p.add_argument("--stub", action="store_true", help="use the stub reviewer: no cost")
    p.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    p.add_argument("--cost-fixed", type=float, default=DEFAULT_COST_FIXED_USD)
    p.add_argument("--cost-per-1k", type=float, default=DEFAULT_COST_PER_1K_LINES_USD)
    p.set_defaults(func=cmd_real_model)

    p = sub.add_parser("compare", help="diff two outcome files; exit 1 if the later lost something")
    p.add_argument("before")
    p.add_argument("after")
    p.set_defaults(func=cmd_compare)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except GuardError as exc:
        print("quality-guard: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
