"""docs-gate 01 end to end: documentation context, checker persona, blocking.

When the repo has a repo-local, gitignored documentation-context artifact at
`.ocr/docs-summary.md`, the gate injects it into the review manifest as
untrusted reference material; the documentation-checker pass consumes it and
its findings flow through the existing verdict machinery (D15 blocking,
quoted in the push's denial message). Absent artifact: behavior is identical
to today. These tests drive the real hook with the stub reviewer
(tests/stub_reviewer.py), asserting on what reached the manifest and what
blocked -- never on internals.
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
_spec = importlib.util.spec_from_file_location("review_gate_dg", _GATE)
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)

DOCS_SUMMARY = (
    "# Documentation summary\n"
    "\n"
    "Decision D1 (binding): verdict computation lives in Python; the model's\n"
    "own verdict word is never trusted.\n"
)
_DOCS_REL = ".ocr/docs-summary.md"


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


def _freshness_marker(work):
    """The docs-gate 05 freshness marker matching the current tip's doc blobs,
    so the hand-placed artifact reads as freshly generated (never regenerated
    under this test's own push)."""
    out = _git(["ls-tree", "-r", "-z", "HEAD"], cwd=work)
    docs = {}
    for entry in out.split("\0"):
        if "\t" not in entry:
            continue
        meta, path = entry.split("\t", 1)
        parts = meta.split()
        base = path.rsplit("/", 1)[-1].lower()
        if len(parts) >= 3 and base.endswith(".md") and not base.startswith(
                ("changelog", "changes", "history")):
            docs[path] = parts[2]
    marker = json.dumps({"version": 1, "tip": _git(["rev-parse", "HEAD"], cwd=work),
                         "docs": docs}, sort_keys=True)
    return "<!-- docs-gate:freshness " + marker + " -->\n\n"


def _write_docs_summary(work):
    """The repo-local cache artifact: present in the tree, never committed."""
    p = work / _DOCS_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_freshness_marker(work) + DOCS_SUMMARY, encoding="utf-8")


def _commit(work, files, amend=False):
    _write(work, files)
    _git(["add", "."], cwd=work)
    _git(["commit", "-q"] + (["--amend", "--no-edit"] if amend else ["-m", "c"]), cwd=work)
    return _git(["rev-parse", "HEAD"], cwd=work)


def _env(tmp_path, trace, **kw):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OCR_", "STUB_", "GIT_TRACE"))}
    env.update({"CLAUDE_PLUGIN_DATA": str(tmp_path / "gate-data"),
                # Kimchi port (plan D12): async run-state and verdicts live in
                # the EXTERNAL store under OCR_STATE_DIR -- keep the conftest's
                # private store on both sides (see test_impact_gate.py).
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


def _review_manifest(tmp_path, trace):
    return next((c.get("manifest") or {}) for c in _calls(tmp_path, trace)
                if not c.get("resolve_file") and not c.get("docs_summary"))


_APP_V1 = "def verdict():\n    return 'model-says-pass'\n"
_APP_V2 = "def verdict():\n    return trust_model()\n\n\ndef trust_model():\n    return True\n"

_CONTRADICTION = (
    "contradicts recorded Decision D1: verdict computation lives in Python, "
    "the model's verdict word is never trusted (docs: .ocr/docs-summary.md)")


def test_docs_finding_blocks_and_the_message_quotes_it(tmp_path):
    """Seam: stub checker returns a finding against the change -> the push is
    denied and the denial quotes the finding -- the existing verdict machinery
    (_normalize_verdict/_merge_chunk_results, D15) untouched."""
    work = _repo(tmp_path, {"app.py": _APP_V1})
    _write_docs_summary(work)
    tip = _commit(work, {"app.py": _APP_V2})
    env = _env(tmp_path, "t.trace",
               STUB_DOCS_FINDING=json.dumps({"content": _CONTRADICTION}))
    decision, reason = _push(work, env)
    assert decision == "deny", reason
    assert _CONTRADICTION in reason, reason
    st = _state(work, tip)
    assert st.get("verdict") == "block" and _CONTRADICTION in st.get("reasons", "")


def test_artifact_arrives_as_untrusted_context_never_as_a_review_target(tmp_path):
    """The artifact's content is injected under a manifest key labelled
    untrusted; document files stay out of the reviewable path list."""
    work = _repo(tmp_path, {"app.py": _APP_V1, "README.md": "# project\n"})
    _write_docs_summary(work)
    _commit(work, {"app.py": _APP_V2, "README.md": "# project\n\nmore docs\n"})
    # The push also changes the docs, so the summary is stale and regenerates
    # inline (docs-gate 05): the stub generator reproduces the same body.
    decision, reason = _push(work, _env(tmp_path, "t.trace",
                                        STUB_DOCS_SUMMARY=DOCS_SUMMARY))
    assert decision == "pass", reason
    manifest = _review_manifest(tmp_path, "t.trace")
    ctx = manifest.get("docs_context")
    assert ctx is not None, "docs_context must be injected when the artifact exists"
    assert ctx["content"] == DOCS_SUMMARY
    assert ctx["source"] == _DOCS_REL
    assert "untrusted" in (ctx.get("note") or "").lower()
    reviewed = set(manifest.get("paths") or [])
    assert ".md" not in [p.rsplit(".", 1)[-1] for p in reviewed], reviewed
    assert _DOCS_REL not in reviewed and "README.md" not in reviewed
    items = manifest.get("items") or []
    assert all(it.get("path") not in ("README.md", _DOCS_REL) for it in items)


def test_no_artifact_means_no_docs_context_and_no_checker_finding(tmp_path):
    """Absent artifact -> identical to today: no docs key, and a stubbed docs
    finding cannot appear because the checker never sees a context."""
    work = _repo(tmp_path, {"app.py": _APP_V1})
    tip = _commit(work, {"app.py": _APP_V2})
    env = _env(tmp_path, "t.trace",
               STUB_DOCS_FINDING=json.dumps({"content": _CONTRADICTION}))
    decision, reason = _push(work, env)
    assert decision == "pass", reason
    manifest = _review_manifest(tmp_path, "t.trace")
    assert "docs_context" not in manifest
    st = _state(work, tip)
    assert st.get("verdict") == "pass"


def test_docs_only_push_keeps_todays_skipped_behavior(tmp_path):
    """A push that changes only documents reviews no files: the worker runs
    without any manifest (the 0.7.0 path), the artifact changes nothing, and
    the push lands."""
    work = _repo(tmp_path, {"app.py": _APP_V1, "README.md": "# project\n"})
    _write_docs_summary(work)
    tip = _commit(work, {"README.md": "# project\n\nrewritten docs section\n"})
    env = _env(tmp_path, "t.trace",
               STUB_DOCS_FINDING=json.dumps({"content": _CONTRADICTION}))
    decision, reason = _push(work, env)
    assert decision == "pass", reason
    assert all(c.get("paths_file") is None for c in _calls(tmp_path, "t.trace"))
    st = _state(work, tip)
    assert st.get("state") == "done" and st.get("verdict") in ("skip", "skipped", "pass")


def test_checker_persona_is_fingerprinted(tmp_path):
    """The documentation checker decides which findings exist, so its persona
    is review criteria: hashed into the build fingerprint."""
    assert "docs-checker.md" in review_gate._FINGERPRINT_AGENTS
    persona = Path(_SCRIPTS).parent / "agents" / "docs-checker.md"
    assert persona.is_file(), "agents/docs-checker.md must exist"
    parts = review_gate._fingerprint_parts(str(tmp_path), "")
    digest = parts.get("agents/docs-checker.md")
    assert digest, "the checker's persona must hash into the fingerprint"
    assert digest == __import__("hashlib").sha256(persona.read_bytes()).hexdigest()[:16]
    # Editing the persona must change the fingerprint: covered with a throwaway
    # plugin copy in test_ledger_scenarios.py (agents/docs-checker.md is criteria).
