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


# --- docs-gate 02: architectural-change flags + missing-record judgment -----
#
# Five deterministic detectors run over the push's changed entries and ride
# the manifest as `arch_flags` (push-wide, {flags: [{family, path, detail}],
# records_root}). A flag never blocks alone: blocking requires flags AND the
# checker's judgment that a decision record is needed and absent
# (STUB_MISSING_RECORD in the stub plays that judgment -- see the persona's
# missing-record class in agents/docs-checker.md).

_RECORD_V1 = "# 0001: verdict computation\n\nBinding: verdicts are computed in Python.\n"
_REQS_GAIN = "flask==3.0\nrequests==2.31\n"
_API_V2 = (
    "def verdict():\n    return 'ok'\n\n\n"
    "@app.route('/v2/ping')\ndef ping():\n    return 'pong'\n"
)


def _write_records_dir(work):
    """A decision-records directory at the conventional location, committed."""
    p = work / "docs" / "adr" / "0001-verdict-computation.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_RECORD_V1, encoding="utf-8")


def _flag_families(manifest):
    arch = (manifest or {}).get("arch_flags") or {}
    return {(f["family"], f["path"]) for f in arch.get("flags") or []}


def test_flag_dependency_manifest_gain_fires_and_flag_alone_never_blocks(tmp_path):
    """A push gaining a requirement carries arch_flags naming that family/path;
    with a calm checker (model does not agree) the push still passes: flags
    without model agreement never block."""
    work = _repo(tmp_path, {"app.py": _APP_V1, "requirements.txt": "flask==3.0\n"})
    _write_docs_summary(work)
    _write_records_dir(work)
    _commit(work, {"app.py": _APP_V2, "requirements.txt": _REQS_GAIN})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    manifest = _review_manifest(tmp_path, "t.trace")
    fams = _flag_families(manifest)
    assert fams == {("dependency_manifest", "requirements.txt")}, fams
    assert manifest["arch_flags"]["records_root"] == "docs/adr"


def test_dependency_manifest_without_a_gain_does_not_fire(tmp_path):
    """Removing an entry is not 'gains an entry': no added lines, no flag."""
    work = _repo(tmp_path, {"app.py": _APP_V1, "requirements.txt": _REQS_GAIN})
    _write_docs_summary(work)
    _write_records_dir(work)
    _commit(work, {"app.py": _APP_V2, "requirements.txt": "flask==3.0\n"})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    manifest = _review_manifest(tmp_path, "t.trace")
    assert "arch_flags" not in (manifest or {})


def test_flag_new_top_level_module(tmp_path):
    work = _repo(tmp_path, {"app.py": _APP_V1})
    _write_docs_summary(work)
    _write_records_dir(work)
    _commit(work, {"app.py": _APP_V2,
                   "billing/__init__.py": '\"\"\"Billing domain.\"\"\"\n'})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    fams = _flag_families(_review_manifest(tmp_path, "t.trace"))
    assert fams == {("new_top_level_module", "billing/__init__.py")}, fams


def test_no_new_top_level_module_when_directory_exists_at_base(tmp_path):
    work = _repo(tmp_path, {"app.py": _APP_V1,
                            "billing/__init__.py": '\"\"\"Billing.\"\"\"\n'})
    _write_docs_summary(work)
    _write_records_dir(work)
    _commit(work, {"app.py": _APP_V2, "billing/invoices.py": "def total():\n    return 0\n"})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    assert "arch_flags" not in (_review_manifest(tmp_path, "t.trace") or {})


def test_flag_public_api_surface(tmp_path):
    work = _repo(tmp_path, {"app.py": _APP_V1})
    _write_docs_summary(work)
    _write_records_dir(work)
    _commit(work, {"app.py": _API_V2})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    fams = _flag_families(_review_manifest(tmp_path, "t.trace"))
    assert fams == {("public_api", "app.py")}, fams


def test_flag_config_env_cli_surface(tmp_path):
    work = _repo(tmp_path, {"app.py": _APP_V1})
    _write_docs_summary(work)
    _write_records_dir(work)
    _commit(work, {"app.py": _APP_V2, ".env.example": "FEATURE_X=1\n"})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    fams = _flag_families(_review_manifest(tmp_path, "t.trace"))
    assert fams == {("config_surface", ".env.example")}, fams


def test_flag_data_shape(tmp_path):
    work = _repo(tmp_path, {"app.py": _APP_V1})
    _write_docs_summary(work)
    _write_records_dir(work)
    _commit(work, {"app.py": _APP_V2,
                   "migrations/0034_users.sql":
                       "ALTER TABLE users ADD COLUMN email TEXT;\n"})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    fams = _flag_families(_review_manifest(tmp_path, "t.trace"))
    assert fams == {("data_shape", "migrations/0034_users.sql")}, fams


def test_clean_push_has_no_arch_flags_key(tmp_path):
    work = _repo(tmp_path, {"app.py": _APP_V1})
    _write_docs_summary(work)
    _write_records_dir(work)
    _commit(work, {"app.py": _APP_V2})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    manifest = _review_manifest(tmp_path, "t.trace")
    assert "arch_flags" not in (manifest or {})


def test_missing_record_blocks_and_the_message_names_the_flags(tmp_path):
    """Flag fired AND the checker's judgment agrees (the repo HAS a decision-
    records directory): the blocking class flows through the verdict machinery
    and the denial message distinguishes the finding kind and names the flag(s)."""
    work = _repo(tmp_path, {"app.py": _APP_V1, "requirements.txt": "flask==3.0\n"})
    _write_docs_summary(work)
    _write_records_dir(work)
    tip = _commit(work, {"app.py": _APP_V2, "requirements.txt": _REQS_GAIN})
    env = _env(tmp_path, "t.trace", STUB_MISSING_RECORD="1")
    decision, reason = _push(work, env)
    assert decision == "deny", reason
    assert "missing decision record" in reason, reason          # the finding kind
    assert "dependency_manifest" in reason, reason              # names the firing flag(s)
    manifest = _review_manifest(tmp_path, "t.trace")
    assert (manifest.get("arch_flags") or {}).get("flags"), manifest
    st = _state(work, tip)
    assert st.get("verdict") == "block", st


def test_repo_without_records_directory_warns_but_never_blocks(tmp_path):
    """No decision-records directory at tip: the same flag+agreement yields the
    warning class only -- visible in the verdict, never a block."""
    work = _repo(tmp_path, {"app.py": _APP_V1, "requirements.txt": "flask==3.0\n"})
    _write_docs_summary(work)
    tip = _commit(work, {"app.py": _APP_V2, "requirements.txt": _REQS_GAIN})
    env = _env(tmp_path, "t.trace", STUB_MISSING_RECORD="1")
    decision, reason = _push(work, env)
    assert decision == "pass", reason
    manifest = _review_manifest(tmp_path, "t.trace")
    assert manifest["arch_flags"]["flags"], manifest
    assert manifest["arch_flags"]["records_root"] is None
    st = _state(work, tip)
    assert st.get("verdict") == "warn", st


def _record_push_1(tmp_path):
    """The shared first act: a flagged push, blocked for the missing record."""
    work = _repo(tmp_path, {"app.py": _APP_V1, "requirements.txt": "flask==3.0\n"})
    _write_docs_summary(work)
    _write_records_dir(work)
    tip1 = _commit(work, {"app.py": _APP_V2, "requirements.txt": _REQS_GAIN})
    decision, reason = _push(work, _env(tmp_path, "t1.trace", STUB_MISSING_RECORD="1"))
    assert decision == "deny", reason
    assert "missing decision record" in reason
    return work, tip1


def test_fix_referencing_the_record_in_a_code_touch_resolves(tmp_path):
    """The fix-recognition convention: the record alone is not enough; the fix
    is recognized when a code file the fix touches references it (the resolver
    sees the prior doc finding, evidence on the added side of an allowlisted
    file's diff) and the re-push passes."""
    work, _ = _record_push_1(tmp_path)
    _commit(work, {
        "app.py": _APP_V2 + "\n# decision: docs/adr/0002-requests.md\n",
        "docs/adr/0002-requests.md": "# 0002\nWe depend on requests for HTTP.\n",
    })
    resolve = json.dumps({"*": {"status": "resolved",
                                "evidence_path": "app.py",
                                "evidence_quote": "# decision: docs/adr/0002-requests.md"}})
    decision, reason = _push(work, _env(tmp_path, "t2.trace", STUB_RESOLVE=resolve))
    assert decision == "pass", reason
    resolvers = [c for c in _calls(tmp_path, "t2.trace") if c.get("resolve_file")]
    assert len(resolvers) == 1, "the prior doc finding must reach the resolver once"
    priors = json.dumps(resolvers[0].get("resolve_manifest") or {})
    assert '"documentation"' in priors, priors


def test_documentation_only_fix_reblocks_with_the_options_message(tmp_path):
    """A fix that only adds the record (no code touch) can never resolve the
    finding: the re-push is denied again and the message states the two options
    plainly -- a referencing code touch, or a logged --no-verify."""
    work, _ = _record_push_1(tmp_path)
    _commit(work, {"docs/adr/0002-requests.md": "# 0002\nWe depend on requests for HTTP.\n"})
    env = _env(tmp_path, "t2.trace", STUB_MISSING_RECORD="1")
    decision, reason = _push(work, env)
    assert decision == "deny", reason
    assert ("documentation-only fix re-blocks once and proceeds via a logged "
            "git push --no-verify") in reason, reason


# ---------------------------------------------------------------------------
# docs-gate 03: contradiction check against the binding corpus.
#
# The gate discovers the repo's binding documents Python-side and delivers the
# pointers in docs_context.corpus; the checker reads the files itself and only
# binding sources veto. Contracts under test: contradiction findings quote both
# sides (decision text AND contradicting code), informal decision notes in
# prose documents are binding, READMEs/runbooks never veto, and a would-block
# contradiction dropped by the falsify pass does not block.

_ADR = (
    "# ADR 0001: verdict computation lives in Python\n"
    "\n"
    "Decision D1: the model's verdict word is never trusted; the verdict is\n"
    "computed in Python from the findings.\n")
_PROSE_NOTES = (
    "# Design notes\n"
    "\n"
    "Decision D3: the falsify pass always runs in its own fresh context,\n"
    "independent of the reviewer's reasoning.\n")
_GLOSSARY = "# Glossary\n\n**gate**: the pre-push review hook.\n"
_ARCHITECTURE = "# Architecture\n\nOne hook process, detached supervisor.\n"
_README = "# project\n\nThe verdict is whatever the model says.\n"
_DEC_D1 = "the model's verdict word is never trusted"
_CODE_QUOTE = "return trust_model()"


def _corpus(tmp_path, trace):
    ctx = (_review_manifest(tmp_path, trace).get("docs_context") or {})
    return ctx.get("corpus")


def _contradiction_spec(source_doc, decision, code):
    """A checker finding built by the persona contract: the decision text and
    the contradicting code are BOTH quoted (docs-gate 03)."""
    return {"source_doc": source_doc,
            "content": "contradicts recorded decision \"%s\" (%s): "
                       "the added code `%s` breaks it" % (decision, source_doc, code),
            "existing_code": code}


def test_corpus_pointers_classify_the_binding_docs_and_exclude_the_rest(tmp_path):
    """Formal records, informal notes in prose, glossaries, architecture docs
    are enumerated as binding pointers; READMEs/runbooks are absent (drift
    only, ticket 04); changelogs and generated docs are excluded entirely."""
    work = _repo(tmp_path, {"app.py": _APP_V1,
                            "docs/adr/0001-python-verdict.md": _ADR,
                            "docs/plans/design.md": _PROSE_NOTES,
                            "GLOSSARY.md": _GLOSSARY,
                            "docs/architecture/overview.md": _ARCHITECTURE,
                            "README.md": _README,
                            "CHANGELOG.md": "# Changelog\n",
                            "docs/generated/api.md": "# Generated API\n",
                            "runbooks/deploy.md": "# Deploy runbook\n"})
    _write_docs_summary(work)
    _commit(work, {"app.py": _APP_V2})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    corpus = _corpus(tmp_path, "t.trace")
    assert corpus, "the manifest must carry binding-corpus pointers"
    assert corpus["decision_records"] == ["docs/adr/0001-python-verdict.md"]
    assert corpus["decision_notes"] == ["docs/plans/design.md"]
    assert corpus["glossaries"] == ["GLOSSARY.md"]
    assert corpus["architecture"] == ["docs/architecture/overview.md"]
    flat = [p for v in corpus.values() for p in v]
    for excluded in ("README.md", "CHANGELOG.md", "docs/generated/api.md",
                     "runbooks/deploy.md"):
        assert excluded not in flat


def test_contradiction_blocks_quoting_both_the_decision_and_the_code(tmp_path):
    """A checker finding against a binding record blocks, and the push's
    denial quotes BOTH the decision text and the contradicting code."""
    work = _repo(tmp_path, {"app.py": _APP_V1,
                            "docs/adr/0001-python-verdict.md": _ADR})
    _write_docs_summary(work)
    _commit(work, {"app.py": _APP_V2})
    spec = _contradiction_spec("docs/adr/0001-python-verdict.md", _DEC_D1, _CODE_QUOTE)
    env = _env(tmp_path, "t.trace", STUB_DOCS_FINDING=json.dumps(spec))
    decision, reason = _push(work, env)
    assert decision == "deny", reason
    assert _DEC_D1 in reason, reason
    assert _CODE_QUOTE in reason, reason


def test_informal_decision_note_inside_a_prose_document_is_binding(tmp_path):
    """A D-numbered note recorded in prose (not a formal ADR) carries veto
    power: the corpus points at the file and a contradiction blocks."""
    work = _repo(tmp_path, {"app.py": _APP_V1,
                            "docs/plans/design.md": _PROSE_NOTES})
    _write_docs_summary(work)
    _commit(work, {"app.py": _APP_V2})
    spec = _contradiction_spec("docs/plans/design.md",
                               "falsify pass always runs in its own fresh context",
                               _CODE_QUOTE)
    env = _env(tmp_path, "t.trace", STUB_DOCS_FINDING=json.dumps(spec))
    decision, reason = _push(work, env)
    assert decision == "deny", reason
    assert _corpus(tmp_path, "t.trace")["decision_notes"] == ["docs/plans/design.md"]
    assert "Decision D3" not in reason  # the quote appears as the prose text
    assert "fresh context" in reason, reason


def test_readme_contradiction_never_blocks(tmp_path):
    """READMEs carry no veto: a finding sourced from a README is dropped by
    the checker (not in the binding corpus) and the push lands."""
    work = _repo(tmp_path, {"app.py": _APP_V1, "README.md": _README})
    _write_docs_summary(work)
    tip = _commit(work, {"app.py": _APP_V2})
    spec = _contradiction_spec("README.md", "the verdict is whatever the model says",
                               _CODE_QUOTE)
    env = _env(tmp_path, "t.trace", STUB_DOCS_FINDING=json.dumps(spec))
    decision, reason = _push(work, env)
    assert decision == "pass", reason
    corpus = _corpus(tmp_path, "t.trace")
    assert all("README.md" not in v for v in (corpus or {}).values())
    st = _state(work, tip)
    assert st.get("verdict") == "pass"


def test_docs_finding_without_both_quotes_does_not_block(tmp_path):
    """The persona contract: a contradiction finding missing either quote is
    not blocking -- the checker drops it rather than emit it."""
    work = _repo(tmp_path, {"app.py": _APP_V1,
                            "docs/adr/0001-python-verdict.md": _ADR})
    _write_docs_summary(work)
    _commit(work, {"app.py": _APP_V2})
    env = _env(tmp_path, "t.trace",
               STUB_DOCS_FINDING=json.dumps({
                   "source_doc": "docs/adr/0001-python-verdict.md",
                   "content": "maybe contradicts the ADR, no code quoted",
                   "existing_code": "",
               }))
    decision, reason = _push(work, env)
    assert decision == "pass", reason


def test_falsify_rejected_contradiction_does_not_block(tmp_path):
    """A would-block contradiction passes through the existing falsify pass
    (SKILL.md 3b); when the filter drops it, the push lands."""
    work = _repo(tmp_path, {"app.py": _APP_V1,
                            "docs/adr/0001-python-verdict.md": _ADR})
    _write_docs_summary(work)
    tip = _commit(work, {"app.py": _APP_V2})
    spec = _contradiction_spec("docs/adr/0001-python-verdict.md", _DEC_D1, _CODE_QUOTE)
    env = _env(tmp_path, "t.trace",
               STUB_DOCS_FINDING=json.dumps(spec),
               STUB_FILTER_DROP=json.dumps([_DEC_D1]))
    decision, reason = _push(work, env)
    assert decision == "pass", reason
    drops = [c["filter"]["dropped"] for c in _calls(tmp_path, "t.trace")
             if c.get("filter")]
    assert drops and any(_DEC_D1 in d for d in drops[0]), drops
    st = _state(work, tip)
    assert st.get("verdict") == "pass"
    assert _DEC_D1 not in st.get("reasons", "")


# ---------------------------------------------------------------------------
# docs-gate 04: documentation DRIFT -- code that stales an existing README,
# runbook, or reference doc. Drift findings carry docs_kind "stale_docs" and
# block only when they quote BOTH the exact stale passage (stale_passage) and
# the contradicting code (existing_code); missing either quote they are
# downgraded to warning and can never block. The blocking rule they ride is
# D15 itself (compute_verdict, ocr_verdict.py) -- byte-identical to the rule
# code findings use.
# ---------------------------------------------------------------------------

_README_DOC = ("# project\n\nBefore pushing, run `python app.py --self-check` "
               "and make sure it prints OK.\n")
_APP_WITH_CHECK = "def self_check():\n    return 'OK'\n"
_APP_WITHOUT_CHECK = "def main():\n    return 1\n"
_STALE_PASSAGE = "run `python app.py --self-check`"
_DRIFT_CODE = "def main():"
_DRIFT_CONTENT = (
    "README.md instructs to run `python app.py --self-check` before pushing, "
    "but the change removes the self-check entry point (app.py now has only "
    "def main():), so that documented instruction is now stale. Unblock by "
    "touching a code file that references the record (a pure docs fix "
    "re-blocks once, then proceed with a logged git push --no-verify).")


def _drift_finding(**over):
    """A contract-shaped drift finding; overrides pin one aspect per test."""
    f = {
        "path": "app.py", "start_line": 1, "end_line": 2,
        "severity": "medium", "confidence": 0.8,
        "category": "documentation", "evidence": "docs_context",
        "docs_kind": "stale_docs",
        "stale_passage": _STALE_PASSAGE, "doc_path": "README.md",
        "existing_code": _DRIFT_CODE, "content": _DRIFT_CONTENT,
    }
    f.update(over)
    return f


def test_fully_quoted_drift_finding_blocks():
    # Criterion: a solid drift finding blocks -- with both quotes present the
    # documentation class is as binding as any code finding.
    assert review_gate.compute_verdict({"findings": [_drift_finding()]}) == "block"


def test_drift_blocking_uses_the_same_d15_medium_branch_as_code_findings():
    # The drift class rides D15 byte-for-byte: medium severity blocks exactly
    # at OCR_BLOCK_CONFIDENCE (0.7) and warns below it -- no drift-specific
    # severity math.
    assert review_gate.compute_verdict(
        {"findings": [_drift_finding(confidence=0.7)]}) == "block"
    assert review_gate.compute_verdict(
        {"findings": [_drift_finding(confidence=0.69)]}) == "warn"


def test_drift_finding_without_the_stale_passage_quote_never_blocks():
    # Missing the verbatim stale passage entirely, or carrying an empty one:
    # even at high severity and 0.95 confidence (which WOULD block a code
    # finding) the finding is below the credibility bar -- warn, never block.
    missing = _drift_finding(severity="high", confidence=0.95)
    del missing["stale_passage"]
    assert review_gate.compute_verdict({"findings": [missing]}) == "warn"
    empty = _drift_finding(severity="high", confidence=0.95, stale_passage="  ")
    assert review_gate.compute_verdict({"findings": [empty]}) == "warn"


def test_drift_finding_without_the_code_quote_never_blocks():
    # Symmetric other half: the contradicting code quote is required too.
    f = _drift_finding(severity="high", confidence=0.95, existing_code="")
    assert review_gate.compute_verdict({"findings": [f]}) == "warn"


def test_non_drift_documentation_findings_are_not_quote_clamped():
    # The quoting rule scopes to docs_kind "stale_docs" only. Other
    # documentation-check classes (ticket 03's contradiction class, emitted
    # without docs_kind) keep their own strictness: a high finding blocks.
    f = _drift_finding(severity="high", confidence=0.95)
    del f["docs_kind"]
    del f["stale_passage"]
    assert review_gate.compute_verdict({"findings": [f]}) == "block"


def test_drift_finding_blocks_and_the_denial_quotes_passage_and_code(tmp_path):
    """Seam: stub checker reports a staling change with BOTH quotes -> the push
    is denied and the denial quotes the stale passage and the contradicting
    code, verbatim."""
    work = _repo(tmp_path, {"app.py": _APP_WITH_CHECK, "README.md": _README_DOC})
    _write_docs_summary(work)
    tip = _commit(work, {"app.py": _APP_WITHOUT_CHECK})
    env = _env(tmp_path, "t.trace", STUB_DOCS_FINDING=json.dumps({
        "docs_kind": "stale_docs", "severity": "medium", "confidence": 0.8,
        "stale_passage": _STALE_PASSAGE, "doc_path": "README.md",
        "existing_code": _DRIFT_CODE, "content": _DRIFT_CONTENT}))
    decision, reason = _push(work, env)
    assert decision == "deny", reason
    assert _STALE_PASSAGE in reason and _DRIFT_CODE in reason, reason
    st = _state(work, tip)
    assert st.get("verdict") == "block" and _STALE_PASSAGE in st.get("reasons", "")


def test_drift_finding_without_both_quotes_warns_only_end_to_end(tmp_path):
    """Same staling change, but the finding carries no stale_passage: even at
    high severity / 0.95 confidence the push lands (decision pass) and the
    verdict is warn -- downgraded, never blocking, still surfaced."""
    work = _repo(tmp_path, {"app.py": _APP_WITH_CHECK, "README.md": _README_DOC})
    _write_docs_summary(work)
    tip = _commit(work, {"app.py": _APP_WITHOUT_CHECK})
    env = _env(tmp_path, "t.trace", STUB_DOCS_FINDING=json.dumps({
        "docs_kind": "stale_docs", "severity": "high", "confidence": 0.95,
        "doc_path": "README.md",
        "existing_code": _DRIFT_CODE, "content": _DRIFT_CONTENT}))
    decision, reason = _push(work, env)
    assert decision == "pass", reason
    st = _state(work, tip)
    assert st.get("verdict") == "warn", st
    assert _DRIFT_CONTENT in st.get("reasons", ""), st.get("reasons")


def test_drift_medium_at_confidence_threshold_blocks_end_to_end(tmp_path):
    """D15 equivalence through the gate: a quoted drift finding at exactly the
    medium blocking threshold (0.7) denies the push, same as a code finding."""
    work = _repo(tmp_path, {"app.py": _APP_WITH_CHECK, "README.md": _README_DOC})
    _write_docs_summary(work)
    _commit(work, {"app.py": _APP_WITHOUT_CHECK})
    env = _env(tmp_path, "t.trace", STUB_DOCS_FINDING=json.dumps({
        "docs_kind": "stale_docs", "severity": "medium", "confidence": 0.7,
        "stale_passage": _STALE_PASSAGE, "doc_path": "README.md",
        "existing_code": _DRIFT_CODE, "content": _DRIFT_CONTENT}))
    decision, reason = _push(work, env)
    assert decision == "deny", reason


def test_persona_carries_the_drift_finding_contract():
    """The finding contract (not prompt wording): the persona must name the
    drift class marker, the two quote fields, and assert this class rides the
    same blocking rule -- what compute_verdict actually enforces."""
    body = (Path(_SCRIPTS).parent / "agents" / "docs-checker.md").read_text()
    assert "docs_kind" in body and "stale_docs" in body
    assert "stale_passage" in body
    for field in ("doc_path", "existing_code"):
        assert field in body, field
