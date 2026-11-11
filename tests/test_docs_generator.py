"""docs-gate 05: docs summary generator + git-based freshness tracker.

Replaces ticket 01's static-artifact assumption: the gate itself generates
`.ocr/docs-summary.md` through one headless worker call (the same stub-worker
mechanism as the review worker, driven here by STUB_DOCS_SUMMARY* knobs), and
decides freshness from the git blob hashes of the tracked docs -- never file
timestamps. A checked push regenerates inline (the doc-changer's push pays the
one model call); a docs-only push only records staleness; a generation failure
fails closed. Nothing generated ever becomes a tracked file.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_STUB = os.path.join(_HERE, "stub_reviewer.py")

sys.path.insert(0, _SCRIPTS)
from hook_output import parse_pretooluse  # noqa: E402
_spec = importlib.util.spec_from_file_location("review_gate_dg05", _GATE)
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)

_DOCS_REL = ".ocr/docs-summary.md"
_STUB_SUMMARY = (
    "# Documentation summary\n"
    "\n"
    "Stub-generated summary of the repository documentation.\n")

_APP_V1 = "def verdict():\n    return 'model-says-pass'\n"
_APP_V2 = "def verdict():\n    return trust_model()\n\n\ndef trust_model():\n    return True\n"
_APP_V3 = "def verdict():\n    return trust_model() * 2\n\n\ndef trust_model():\n    return True\n"


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def _repo(tmp_path, files):
    tmp_path.mkdir(parents=True, exist_ok=True)
    remote, work = tmp_path / "origin.git", tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    for k, v in (("user.email", "t@t.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(["config", k, v], cwd=work)
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    _git(["config", "core.hooksPath", str(hooks)], cwd=work)
    _write(work, files)
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    return work


def _write(work, files):
    for name, content in files.items():
        p = work / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _commit(work, files, amend=False):
    _write(work, files)
    _git(["add", "."], cwd=work)
    _git(["commit", "-q"] + (["--amend", "--no-edit"] if amend else ["-m", "c"]), cwd=work)
    return _git(["rev-parse", "HEAD"], cwd=work)


def _freshness_marker(work, docs):
    """A freshness marker naming `docs` (the source blobs at generation time),
    in the exact format the gate's tracker reads."""
    marker = json.dumps(
        {"version": 1, "tip": _git(["rev-parse", "HEAD"], cwd=work), "docs": docs},
        sort_keys=True)
    return "<!-- docs-gate:freshness " + marker + " -->\n\n"


def _write_stale_summary(work):
    """An artifact whose marker no longer matches the tip's docs."""
    p = work / _DOCS_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_freshness_marker(work, {"OUTDATED.md": "0" * 40})
                 + "old summary body\n", encoding="utf-8")


def _env(tmp_path, trace, **kw):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OCR_", "STUB_", "GIT_TRACE"))}
    env.update({"CLAUDE_PLUGIN_DATA": str(tmp_path / "gate-data"),
                "OCR_STATE_DIR": os.environ["OCR_STATE_DIR"],
                "OCR_REVIEWER_CMD": f'"{sys.executable}" "{_STUB}"',
                "STUB_TRACE": str(tmp_path / trace), "OCR_INLINE_BUDGET": "30"})
    env.update({k: str(v) for k, v in kw.items()})
    return env


def _push(work, env, cmd="git push origin main"):
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash", "cwd": str(work),
                          "tool_input": {"command": cmd}})
    proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                          capture_output=True, text=True, cwd=str(work), env=env, timeout=90)
    assert proc.returncode == 0, proc.stderr
    return parse_pretooluse(proc.stdout)


def _state(work, tip, timeout=60):
    common = review_gate._git_common_dir(str(work))
    path = review_gate._state_path(common, tip)
    deadline = time.monotonic() + timeout
    while True:
        st = review_gate._read_state(path) or {}
        if st.get("state") in ("done", "failed") or time.monotonic() > deadline:
            return st
        time.sleep(0.2)


def _calls(tmp_path, trace):
    p = tmp_path / trace
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def _gen_calls(tmp_path, trace):
    """The worker invocations that generated the docs summary (docs-gate 05)."""
    return [c for c in _calls(tmp_path, trace) if c.get("docs_summary")]


def _review_manifest(tmp_path, trace):
    return next((c.get("manifest") or {}) for c in _calls(tmp_path, trace)
                if not c.get("resolve_file") and not c.get("docs_summary"))


def test_missing_cache_builds_on_first_push_one_model_call(tmp_path):
    """Fresh clone, no artifact: the first checked push pays exactly one
    generation call, the artifact lands gitignored with a freshness marker,
    and the manifest carries the generated body."""
    work = _repo(tmp_path, {"app.py": _APP_V1, "README.md": "# project\n"})
    tip = _commit(work, {"app.py": _APP_V2})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    gens = _gen_calls(tmp_path, "t.trace")
    assert len(gens) == 1, gens
    artifact = (work / _DOCS_REL).read_text(encoding="utf-8")
    assert artifact.startswith("<!-- docs-gate:freshness ")
    assert "Stub-generated summary" in artifact
    ctx = _review_manifest(tmp_path, "t.trace").get("docs_context")
    assert ctx is not None
    assert ctx["content"].strip() == _STUB_SUMMARY.strip()
    st = _state(work, tip)
    assert st.get("verdict") == "pass"
    assert (st.get("docs_summary") or {}).get("status") == "fresh"


def test_stale_summary_regenerates_inline_before_the_check_runs(tmp_path):
    """Docs changed since the marker: regeneration happens inline during the
    push, BEFORE the review call, and the documentation check then runs
    against the new summary -- its finding still blocks, quoting itself."""
    work = _repo(tmp_path, {"app.py": _APP_V1, "README.md": "# project\n"})
    _write_stale_summary(work)
    _commit(work, {"app.py": _APP_V2})
    contradiction = ("contradicts recorded Decision D1: verdict computation "
                     "lives in Python (docs: README.md)")
    new_summary = "# Documentation summary\n\nDecision D1 (binding): verdict in Python.\n"
    env = _env(tmp_path, "t.trace",
               STUB_DOCS_SUMMARY=new_summary,
               STUB_DOCS_FINDING=json.dumps({"content": contradiction}))
    decision, reason = _push(work, env)
    assert decision == "deny", reason
    assert contradiction in reason
    calls = _calls(tmp_path, "t.trace")
    gens = [i for i, c in enumerate(calls) if c.get("docs_summary")]
    reviews = [i for i, c in enumerate(calls)
               if c.get("manifest") and not c.get("docs_summary")]
    assert gens and reviews and max(gens) < min(reviews), \
        "the summary must regenerate before the review worker sees a manifest"
    ctx = calls[reviews[0]]["manifest"].get("docs_context")
    assert ctx["content"].strip() == new_summary.strip()


def test_fresh_summary_skips_generation(tmp_path):
    """Marker hashes == tip hashes: no model call at all, context injected."""
    work = _repo(tmp_path, {"app.py": _APP_V1, "README.md": "# project\n"})
    docs = {"README.md": _git(["rev-parse", "HEAD:README.md"], cwd=work)}
    (work / _DOCS_REL).parent.mkdir(parents=True, exist_ok=True)
    (work / _DOCS_REL).write_text(
        _freshness_marker(work, docs) + "the fresh body\n", encoding="utf-8")
    _commit(work, {"app.py": _APP_V2})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    assert _gen_calls(tmp_path, "t.trace") == []
    ctx = _review_manifest(tmp_path, "t.trace").get("docs_context")
    assert ctx is not None and "the fresh body" in ctx["content"]


def test_freshness_is_decided_by_git_hashes_never_timestamps(tmp_path):
    """Touching the docs' and the artifact's mtimes must not fake staleness:
    content hashes unchanged means fresh, whatever the clock says."""
    work = _repo(tmp_path, {"app.py": _APP_V1, "README.md": "# project\n"})
    tip = _commit(work, {"app.py": _APP_V2})
    decision, reason = _push(work, _env(tmp_path, "t1.trace"))
    assert decision == "pass", reason
    assert len(_gen_calls(tmp_path, "t1.trace")) == 1
    old = 1_000_000_000  # epoch seconds, far in the past
    os.utime(work / "README.md", (old, old))
    os.utime(work / _DOCS_REL, (old, old))
    _commit(work, {"app.py": _APP_V3})
    decision, reason = _push(work, _env(tmp_path, "t2.trace"))
    assert decision == "pass", reason
    assert _gen_calls(tmp_path, "t2.trace") == [], \
        "mtime-only changes must not trigger a regeneration"
    st = _state(work, _git(["rev-parse", "HEAD"], cwd=work))
    assert st.get("verdict") == "pass"
    assert tip != _git(["rev-parse", "HEAD"], cwd=work)


def test_docs_only_push_records_staleness_without_regenerating(tmp_path):
    """A push that changes only docs runs no review: the tracker notes the
    staleness (state + no generation call), and the regeneration waits for
    the next push that runs checks -- which then pays the one call."""
    work = _repo(tmp_path, {"app.py": _APP_V1, "README.md": "# project\n"})
    tip = _commit(work, {"README.md": "# project\n\nrewritten docs\n"})
    decision, reason = _push(work, _env(tmp_path, "t1.trace"))
    assert decision == "pass", reason
    assert _gen_calls(tmp_path, "t1.trace") == [], \
        "a docs-only push never pays the generation call"
    st = _state(work, tip)
    rec = st.get("docs_summary") or {}
    # Docs changed since any usable marker: recorded as stale (artifact is
    # present but its hashes no longer match) or missing (no cache at all).
    assert rec.get("status") in ("stale", "missing"), st
    # The next push that runs checks regenerates first.
    _commit(work, {"app.py": _APP_V2})
    decision, reason = _push(work, _env(tmp_path, "t2.trace"))
    assert decision == "pass", reason
    assert len(_gen_calls(tmp_path, "t2.trace")) == 1


def test_generation_failure_fails_closed_with_a_documented_message(tmp_path):
    """The summary is needed and cannot be produced: the push is denied
    (never silently unchecked), and the denial itself documents the chosen
    fail-closed behavior and the recorded escape hatch."""
    work = _repo(tmp_path, {"app.py": _APP_V1, "README.md": "# project\n"})
    _commit(work, {"app.py": _APP_V2})
    env = _env(tmp_path, "t.trace", STUB_DOCS_SUMMARY_VERDICT="fail")
    decision, reason = _push(work, env)
    assert decision == "deny", reason
    assert "documentation summary" in reason
    assert "fail-closed" in reason
    assert "--no-verify" in reason
    assert not (work / _DOCS_REL).exists(), \
        "a failed generation must leave no half-written artifact"


def test_nothing_generated_ever_lands_in_tracked_files(tmp_path):
    """The gitignore guarantee: after generation the artifact is ignored,
    untracked, and absent from the index -- the repo accumulates no files."""
    work = _repo(tmp_path, {"app.py": _APP_V1, "README.md": "# project\n"})
    _commit(work, {"app.py": _APP_V2})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    assert (work / _DOCS_REL).exists()
    tracked = _git(["ls-files"], cwd=work).splitlines()
    assert not any(p.startswith(".ocr/") for p in tracked), tracked
    proc = subprocess.run(["git", "check-ignore", "-q", _DOCS_REL], cwd=work)
    assert proc.returncode == 0, "the artifact MUST be ignored"
    assert _DOCS_REL not in _git(["status", "--porcelain"], cwd=work)


def test_repo_with_only_a_changelog_is_no_docs_never_checked(tmp_path):
    """Changelogs are out of scope for all checks: a repo whose only doc is a
    CHANGELOG has no corpus -- no generation call, no docs_context, and a
    scripted docs finding cannot block."""
    work = _repo(tmp_path, {"app.py": _APP_V1, "CHANGELOG.md": "# Changelog\n"})
    _commit(work, {"app.py": _APP_V2})
    env = _env(tmp_path, "t.trace",
               STUB_DOCS_FINDING=json.dumps({"content": "must never appear"}))
    decision, reason = _push(work, env)
    assert decision == "pass", reason
    assert _gen_calls(tmp_path, "t.trace") == []
    assert "docs_context" not in _review_manifest(tmp_path, "t.trace")
