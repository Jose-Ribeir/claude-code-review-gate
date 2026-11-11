"""Programmatic builders for the four miss-category fixture repos (t12).

Each ``build_mN(root_dir)`` creates a REAL git repo -- base commit plus tip
commit -- under the caller-supplied temp dir and returns a small dict with
what the tests need (paths, changed files, demands, stub knobs). Nothing
binary lives on disk; the fixture sources are the constants below.

Also the shared harness:

* :func:`load_main_module` execs a ``scripts/<name>.py`` from the PROJECT's
  pre-v2 base branch (``git show main:...``; the Kimchi port's ``main``,
  91744e9, is the same pre-v2 tree as upstream's: single-filter
  ``find_references(is_allowed=...)``, ``per_symbol=5``, and none of the v2
  modules), so a test can run the pre-v2 module side by side with the v2 one
  against the same fixture. ``None`` means the module did not exist pre-v2 --
  the parametrized-abstraction half of the "old misses -> v2 catches"
  assertion for the new v2 modules.
* :func:`review_path_filter` / :func:`context_path_filter` expose the gate's
  own t01 allowlist split (imported from the real review-gate.py, not
  reimplemented here).
* :func:`push_fixture` runs a real ``git push`` through ``review-gate.py
  --mode hook`` with ``tests/stub_reviewer.py`` as the reviewer
  (``OCR_REVIEWER_CMD``), mirroring tests/test_deep_gate.py. Kimchi port
  (D12): the gate's state store is EXTERNAL -- ``gate_env`` propagates the
  per-test ``OCR_STATE_DIR`` conftest sets, so the hook-mode subprocess and
  the test poll the same store.

Python 3.9; tests import this module after putting
``tests/fixtures/miss_categories`` on ``sys.path``.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_TESTS = os.path.dirname(os.path.dirname(_HERE))
_ROOT = os.path.dirname(_TESTS)
_SCRIPTS = os.path.join(_ROOT, "scripts")
for _p in (_SCRIPTS, _TESTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_spec = importlib.util.spec_from_file_location(
    "review_gate_fixtures", os.path.join(_SCRIPTS, "review-gate.py"))
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)

from hook_output import parse_pretooluse  # noqa: E402

_STUB = os.path.join(_TESTS, "stub_reviewer.py")


# ---------------------------------------------------------------------------
# Harness: git, repo construction, main-branch module loading, gate push
# ---------------------------------------------------------------------------

def git(args, cwd, check=True, env=None):
    proc = subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=env)
    if check and proc.returncode != 0:
        raise AssertionError("git %s failed: %s%s" % (args, proc.stdout, proc.stderr))
    return proc.stdout


def _write_files(root, files):
    for relpath, content in files.items():
        full = os.path.join(str(root), relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)


def init_repo(root, files, *, with_origin=True):
    """A fresh repo: initial branch main, base commit of ``files``, and (by
    default) a bare sibling pushed as origin (forge/e2e paths need one)."""
    root = str(root)
    os.makedirs(root, exist_ok=True)
    git(["init", "-b", "main", "."], cwd=root)
    for k, v in (("user.email", "t@t.com"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        git(["config", k, v], cwd=root)
    stray = os.path.join(root, "no-hooks")
    os.makedirs(stray, exist_ok=True)
    git(["config", "core.hooksPath", stray], cwd=root)
    _write_files(root, files)
    git(["add", "-A"], cwd=root)
    git(["commit", "-q", "-m", "base"], cwd=root)
    origin = None
    if with_origin:
        origin = os.path.join(os.path.dirname(root), "origin.git")
        git(["init", "--bare", "-b", "main", origin], cwd=root)
        git(["remote", "add", "origin", origin], cwd=root)
        git(["push", "-q", "-u", "origin", "main"], cwd=root)
    return {"root": root, "origin": origin}


def commit(root, files, message="c"):
    """Write files, commit as a new tip, return the tip sha."""
    _write_files(root, files)
    git(["add", "-A"], cwd=root)
    git(["commit", "-q", "-m", message], cwd=root)
    return git(["rev-parse", "HEAD"], cwd=root).strip()


def load_main_module(name):
    """Exec ``scripts/<name>.py`` from the project's `main` branch as a fresh
    module. Returns None when the module did not exist pre-v2.

    Used for the old-misses half of the fixture assertions: run the actual
    pre-v2 machinery (or assert its absence) against the same fixture state.
    """
    proc = subprocess.run(["git", "-C", _ROOT, "show", "main:scripts/%s.py" % name],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    spec = importlib.util.spec_from_loader("main_%s" % name, loader=None,
                                           origin="main:scripts/%s.py" % name)
    module = importlib.util.module_from_spec(spec)
    exec(compile(proc.stdout, "main:scripts/%s.py" % name, "exec"),
         module.__dict__)
    return module


def review_path_filter(path):
    """The gate's REVIEW allowlist (t01): which changed files get reviewed."""
    return review_gate._is_allowed_path(path)


def context_path_filter(path):
    """The gate's CONTEXT allowlist (t01): which repo files the caller /
    reference / pointer machinery may search. Spec/test files qualify here."""
    return review_gate._is_context_allowed_path(path)


def gate_env(tmp_path, **overrides):
    """Environment for an out-of-process gate run with the stub reviewer.

    Kimchi D12 adaptation: ``OCR_STATE_DIR`` is propagated explicitly -- the
    strip of inherited ``OCR_*`` below must not drop it, or the hook-mode
    subprocess would write into the developer's real external store and the
    test would poll a private one (mirrors tests/test_deep_gate.py's _env).
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OCR_", "STUB_"))}
    env["OCR_STATE_DIR"] = os.environ["OCR_STATE_DIR"]
    env.update({"CLAUDE_PLUGIN_DATA": str(os.path.join(str(tmp_path), "gate-data")),
                "OCR_REVIEWER_CMD": '"%s" "%s"' % (sys.executable, _STUB),
                "STUB_TRACE": str(os.path.join(str(tmp_path), "stub.trace")),
                "OCR_INLINE_BUDGET": "30"})
    env.update({k: str(v) for k, v in overrides.items()})
    return env


def push_fixture(work, env):
    """One `git push origin main` through the gate hook; returns the
    (decision, reason) hook verdict (Kimchi hook protocol via hook_output)."""
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash",
                          "cwd": str(work),
                          "tool_input": {"command": "git push origin main"}})
    proc = subprocess.run(
        [sys.executable, os.path.join(_SCRIPTS, "review-gate.py"), "--mode", "hook"],
        input=payload, capture_output=True, text=True, cwd=str(work),
        env=env, timeout=90)
    assert proc.returncode == 0, proc.stderr
    return parse_pretooluse(proc.stdout)


def await_run_state(work, tip, timeout=60):
    """The review supervisor's end state for ``tip`` (done/failed), polling."""
    common = review_gate._git_common_dir(str(work))
    path = review_gate._state_path(common, tip)
    deadline = time.monotonic() + timeout
    while True:
        st = review_gate._read_state(path) or {}
        if st.get("state") in ("done", "failed") or time.monotonic() > deadline:
            return st
        time.sleep(0.2)


def stub_calls(tmp_path):
    """One dict per stub reviewer invocation (the stub's trace)."""
    trace = os.path.join(str(tmp_path), "stub.trace")
    with open(trace, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def review_manifests(tmp_path):
    """The review manifests the stub reviewer saw (resolver calls skipped)."""
    return [c["manifest"] for c in stub_calls(tmp_path)
            if c.get("manifest") and not c.get("resolve_file")]


def make_forge_cli(bin_dir, cli, stdout_text, rc=0):
    """A fake ``glab``/``gh`` CLI on ``bin_dir``: prints ``stdout_text`` and
    exits ``rc``. Returns a PATH dir list to prepend (the dir holding it)."""
    os.makedirs(str(bin_dir), exist_ok=True)
    script = os.path.join(str(bin_dir), cli)
    with open(script, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nprintf '%%s' '%s'\nexit %d\n"
                 % (stdout_text.replace("'", "'\\''"), rc))
    os.chmod(script, 0o755)
    return str(bin_dir)


# ---------------------------------------------------------------------------
# Demands template (mirrors tests/test_collectors.py)
# ---------------------------------------------------------------------------

EMPTY_DEMANDS = {"symbol_callers": [], "event_couples": [], "doc_refs": [],
                 "conventions": [], "prose_pointers": [],
                 "risk": {"score": 0, "reasons": []}}


def demands(**overrides):
    d = json.loads(json.dumps(EMPTY_DEMANDS))
    d.update(overrides)
    return d


# ---------------------------------------------------------------------------
# Fixture M1 -- remount/filter-policy coupling (Lexical-style slot swap + an
# untouched event filter)
# ---------------------------------------------------------------------------
#
# The diff touches ONLY web/src/editor/Mount.tsx (slots swapped, the
# remount-pinning `key` removed, plus an unchanged-by-accident producer of
# the 'history-merge' update tag). The load-bearing second half lives in the
# untouched web/src/history/filterPolicy.tsx, which filters undo updates on
# exactly that tag.

M1_EVENT = "history-merge"
M1_MOUNT = "web/src/editor/Mount.tsx"
M1_FILTER = "web/src/history/filterPolicy.tsx"

_M1_SLOTS = {
    "web/src/editor/slots/SlotA.tsx": ("export function SlotA() {\n"
                                       "  return <div className=\"slot-a\">A</div>;\n"
                                       "}\n"),
    "web/src/editor/slots/SlotB.tsx": ("export function SlotB() {\n"
                                       "  return <div className=\"slot-b\">B</div>;\n"
                                       "}\n"),
}

M1_MOUNT_BASE = """\
import { SlotA } from './slots/SlotA';
import { SlotB } from './slots/SlotB';

// Editor shell: remount semantics matter for history updates.
export function Mount() {
  return (
    <section key="editor-shell">
      <SlotA />
      <SlotB />
    </section>
  );
}

// Flush pending history changes under the merge tag.
export function flushHistoryMerge(editor) {
  editor.update(() => mergeBatches(), {tag: 'history-merge'});
}

function mergeBatches() {
  return undefined;
}
"""

M1_MOUNT_TIP = """\
import { SlotA } from './slots/SlotA';
import { SlotB } from './slots/SlotB';

// Editor shell: remount semantics matter for history updates.
export function Mount() {
  return (
    <section>
      <SlotB />
      <SlotA />
    </section>
  );
}

// Flush pending history changes under the merge tag.
export function flushHistoryMerge(editor) {
  editor.update(() => mergeBatches(), {tag: 'history-merge'});
}

function mergeBatches() {
  return undefined;
}
"""

M1_FILTER_FILE = """\
// Filter policy for undo updates (untouched by the remount change).
// Runs on every editor update; the tag set decides what gets swallowed.
export function filterUndoUpdates(tags) {
  if (!tags.has('history-merge')) return;
  swallowResurrectedEntries();
}

function swallowResurrectedEntries() {
  return undefined;
}
"""


def build_m1(root):
    """Remount/filter-policy fixture. Tip changes ONLY the mounting file."""
    files = dict(_M1_SLOTS)
    files.update({M1_MOUNT: M1_MOUNT_BASE, M1_FILTER: M1_FILTER_FILE})
    info = init_repo(root, files)
    tip = commit(info["root"], {M1_MOUNT: M1_MOUNT_TIP})
    info.update({"tip": tip, "event": M1_EVENT, "mount": M1_MOUNT,
                 "filter": M1_FILTER, "changed_files": [M1_MOUNT],
                 "spotter_demands": demands(event_couples=[M1_EVENT])})
    return info


# ---------------------------------------------------------------------------
# Fixture M2 -- convention-doc violation (docs/agents doc + violating comment)
# ---------------------------------------------------------------------------

M2_DOC = "docs/agents/comments.md"
M2_SRC = "src/app.py"

M2_DOC_TEXT = """\
# Comment style for agents

House rule: a comment fits in 72 characters per line, one or two sentences.
Over-long prose comments must be summarised or moved to documentation.
"""

M2_RULE_JSON = json.dumps({
    "conventions": [{"doc": M2_DOC}]  # no applyTo: default scope is repo-wide
}, indent=2) + "\n"

M2_SRC_BASE = """\
def render_banner(user):
    return f"Hello {user}"
"""

M2_SRC_TIP = """\
def render_banner(user):
    # This comment narrates the whole history of the banner pipeline in endless detail, far past any sane line length
    return f"Hello {user}"
"""


def build_m2(root):
    """Convention-doc fixture. Tip adds an over-long comment to src/app.py;
    the rule lives in docs/agents/comments.md, NOT an ancestor of src/."""
    info = init_repo(root, {M2_DOC: M2_DOC_TEXT, ".ocr/rule.json": M2_RULE_JSON,
                            M2_SRC: M2_SRC_BASE})
    tip = commit(info["root"], {M2_SRC: M2_SRC_TIP})
    info.update({"tip": tip, "doc": M2_DOC, "src": M2_SRC,
                 "changed_files": [M2_SRC], "violating_column": 72,
                 "spotter_demands": demands(conventions=["comment-length rule"],
                                            doc_refs=[M2_DOC])})
    return info


# ---------------------------------------------------------------------------
# Fixture M3 -- stranded spec callers (shared helper default + 11 spec
# callers across 8 spec files)
# ---------------------------------------------------------------------------

M3_HELPER = "shared/formatters.py"
M3_SYMBOL = "build_report"

M3_HELPER_BASE = '''\
"""Report formatting helpers shared across the pipeline."""


def _render_row(row):
    return "%s: %s" % (row["name"], row["amount"])


def _render_totals(rows):
    return "total: %s" % sum(r["amount"] for r in rows)


def build_report(
        title,
        rows,
        include_totals=True):
    """Render a department report from row records."""
    parts = ["# %s" % title]
    parts += [_render_row(r) for r in rows]
    if include_totals:
        parts.append(_render_totals(rows))
    return "\\n".join(parts)
'''

M3_HELPER_TIP = '''\
"""Report formatting helpers shared across the pipeline."""


def _render_row(row):
    return "%s: %s" % (row["name"], row["amount"])


def _render_totals(rows):
    return "total: %s" % sum(r["amount"] for r in rows)


def build_report(
        title,
        rows,
        include_totals=True,
        currency="EUR"):
    """Render a department report from row records."""
    parts = ["# %s" % title]
    parts += [_render_row(r) for r in rows]
    if include_totals:
        parts.append(_render_totals(rows))
    parts.append("currency: %s" % currency)
    return "\\n".join(parts)
'''

# (filename, number of build_report() call lines) -- 11 callers, 8 files.
M3_SPEC_PLAN = [("spec_report_backend.py", 2), ("spec_report_billing.py", 2),
                ("spec_report_archive.py", 2), ("spec_report_ops.py", 1),
                ("spec_report_hr.py", 1), ("spec_report_sales.py", 1),
                ("spec_report_audit.py", 1), ("spec_report_weekly.py", 1)]
M3_SPEC_CALLERS = sum(n for _, n in M3_SPEC_PLAN)  # 11
M3_SPEC_FILES = ["tests/%s" % name for name, _ in M3_SPEC_PLAN]

_M3_PROD_CALLER = "src/run_pipeline.py"


def _m3_spec_text(stem, n_calls):
    # `from shared import formatters` + dotted calls: the import line does
    # not name the symbol, so every grep hit of it is a real call site (the
    # bundle's per-file/per-symbol caps then fit the whole caller set).
    body = ["from shared import formatters", ""]
    for i in range(1, n_calls + 1):
        body += ["",
                 "def test_%s_report_%d():" % (stem, i),
                 "    out = formatters.build_report(\"Ops %d\", [{\"name\": \"a\", \"amount\": %d}])" % (i, i),
                 "    assert out"]
    return "\n".join(body) + "\n"


M3_PROD_CALLER_TEXT = '''\
from shared import formatters


def nightly():
    return formatters.build_report("Nightly", [{"name": "n", "amount": 1}])
'''


def build_m3(root):
    """Stranded-caller fixture. Tip widens the helper's multi-line signature
    by one optional parameter; 11 call sites live in 8 review-excluded spec
    files (plus one production caller for contrast)."""
    files = {M3_HELPER: M3_HELPER_BASE, _M3_PROD_CALLER: M3_PROD_CALLER_TEXT}
    for name, n_calls in M3_SPEC_PLAN:
        files["tests/%s" % name] = _m3_spec_text(name[:-3], n_calls)
    info = init_repo(root, files)
    tip = commit(info["root"], {M3_HELPER: M3_HELPER_TIP})
    info.update({"tip": tip, "helper": M3_HELPER, "symbol": M3_SYMBOL,
                 "helper_base": M3_HELPER_BASE, "helper_tip": M3_HELPER_TIP,
                 "prod_caller": _M3_PROD_CALLER, "spec_files": M3_SPEC_FILES,
                 "spec_callers": M3_SPEC_CALLERS,
                 "changed_files": [M3_HELPER],
                 "spotter_demands": demands(symbol_callers=[
                     {"name": M3_SYMBOL, "defined_in": M3_HELPER}])})
    return info


# ---------------------------------------------------------------------------
# Fixture M4 -- dangling report pointer (diff introduces a reference to a
# report that exists nowhere: not in the repo, the registry, branch tips, or
# the forge)
# ---------------------------------------------------------------------------

M4_SRC = "reports/export.py"
M4_TARGET = "out/weekly-2026-W48.md"

M4_SRC_BASE = '''\
"""Weekly export job."""


def export_weekly(rows):
    """Write the weekly export."""
    return list(rows)
'''

M4_SRC_TIP = '''\
"""Weekly export job."""

# Companion artifact produced by the sibling MR's reporting pipeline; the
# dashboard links it from the weekly page.
WEEKLY_NOTE = "out/weekly-2026-W48.md"


def export_weekly(rows):
    """Write the weekly export."""
    return list(rows)
'''

M4_REGISTRY = json.dumps({
    "weekly-2026-W48.md": {"produced_by": "ci/reporting-pipeline",
                           "path": M4_TARGET, "mr": "sibling-mr-42"},
}, indent=2) + "\n"


def build_m4(root):
    """Dangling-pointer fixture. Tip adds a string referencing a report name
    that nothing produces. An origin remote exists so forge autodetection
    yields glab, to be stubbed (or hidden) per test."""
    info = init_repo(root, {M4_SRC: M4_SRC_BASE})
    tip = commit(info["root"], {M4_SRC: M4_SRC_TIP})
    info.update({"tip": tip, "src": M4_SRC, "target": M4_TARGET,
                 "registry": M4_REGISTRY, "changed_files": [M4_SRC],
                 "spotter_demands": demands(
                     prose_pointers=[M4_TARGET.rsplit("/", 1)[-1]])})
    return info
