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


def test_docs_only_push_now_runs_the_checker_when_a_duty_is_on(tmp_path):
    """deliberate behavior change (SPEC-14): a docs-only push with an
    enabled quality duty reviews no CODE, but the documentation checker
    still spawns on the docs_quality payload -- the docs changer pays for
    the docs check. A finding from a class that needs docs_context (the
    fallback manifest carries none) cannot appear, so the push lands. The
    no-spawn behavior survives only with all duties off (the off-row test)."""
    work = _repo(tmp_path, {"app.py": _APP_V1, "README.md": "# project\n"})
    tip = _commit(work, {"README.md": "# project\n\nrewritten docs section\n"})
    env = _env(tmp_path, "t.trace",
               STUB_DOCS_FINDING=json.dumps({"content": _CONTRADICTION}))
    decision, reason = _push(work, env)
    assert decision == "pass", reason
    payload = _docs_quality(tmp_path)
    assert payload is not None, "docs-only push with deslop on spawns the checker"
    st = _state(work, tip)
    assert st.get("state") == "done" and st.get("verdict") == "pass"


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


# ---------------------------------------------------------------------------
# SPEC-14.3: documentation-quality duties -- persona duty sections plus the
# review skill's spawn condition and docs_quality payload contract.
#
# The payload contract below is what the gate plumbing (SPEC-14.4) must
# produce: it is pinned here textually on BOTH sides (the review SKILL.md the
# orchestrator follows and the docs-checker persona that consumes the block)
# so the two documents cannot drift apart. Through the stub worker, only the
# enable-matrix off-row is observable gate-side in this ticket: every built-
# in duty disabled in the repo's checks config => the manifest can never
# carry a docs_quality key, and without the key the skill's spawn condition
# cannot fire a quality check.

_PERSONA = Path(_SCRIPTS).parent / "agents" / "docs-checker.md"
_SKILL = Path(_SCRIPTS).parent / "skills" / "review" / "SKILL.md"

# The payload contract (SPEC-14 working doc, chunk 3): exact key/duty names.
_DUTY_NAMES = ("docs-deslop", "adr-wait-what", "adr-integrity")
_DOCS_QUALITY_PAYLOAD_KEYS = ("docs_quality", "enabled_duties", "options",
                              "files", "evidence")


def _persona_body():
    return _PERSONA.read_text(encoding="utf-8")


def _skill_body():
    return _SKILL.read_text(encoding="utf-8")


def test_persona_documents_all_six_duty_classes_with_the_gating_flag():
    """Six classes: the three legacy classes untouched in text, plus the
    three quality classes, each gated on its flag in
    docs_quality.enabled_duties."""
    body = _persona_body()
    # Legacy classes keep their anchors (text untouched by SPEC-14.3).
    assert "missing decision record" in body
    assert "stale_docs" in body
    # The three quality duties, each named and each gated on the manifest flag.
    for duty in _DUTY_NAMES:
        assert duty in body, duty
        gated = f"enabled_duties[\"{duty}\"]"
        assert gated in body, f"{duty} must be gated on docs_quality.{gated}"
    assert "docs_quality" in body


def test_persona_deslop_cluster_rule_is_a_hard_output_rule():
    """The deslop evidence rule as an OUTPUT contract: every deslop finding
    carries a tells array (phrase + line); the finding needs 3+ co-located
    tells or document_wide: true, anything weaker is a note, and the gate
    demotes nonconforming findings -- matching what ocr_docsquality.py
    --validate enforces gate-side."""
    body = _persona_body()
    for anchor in ("tells", '"phrase"', '"line"', "co-located",
                   "document_wide: true", "HARD OUTPUT RULE"):
        assert anchor in body, anchor
    assert "3" in body  # the cluster threshold
    # The demotion is stated as gate-enforced, so drifting above the bar is
    # pointless and below it wasted.
    assert "demote" in body.lower()


def test_persona_deslop_score_is_a_summary_field_never_a_gate():
    """Story 13: the 5-dimension score is telemetry signal only."""
    body = _persona_body()
    for dim in ("Concreteness", "Order", "Literalness", "Address", "Density"):
        assert dim in body, dim
    assert "score" in body
    assert "never a gate" in body or "never blocks" in body.lower()


def test_persona_wait_what_structure_contract():
    """Stories 14-15: the repo's own sections/template first; the fallback
    superset exactly (Status, Date, Context, Decision, Consequences);
    Context-before-Decision unconditional."""
    body = _persona_body()
    assert "Status, Date, Context, Decision, Consequences" in body
    for anchor in ("sections", "template", "fallback"):
        assert anchor in body, anchor
    assert "Context" in body and "Decision" in body
    assert "unconditional" in body


def test_persona_wait_what_ste_is_judged_from_scan_stats_not_regex():
    """Story 16: STE readability judged from deterministic scan stats; the
    persona must state plainly it never issues regex verdicts on prose."""
    body = _persona_body()
    for anchor in ("sentence", "acronym", "passive", "sentence_cap"):
        assert anchor in body, anchor
    assert "regex verdict" in body


def test_persona_wait_what_glossary_variants_against_corpus_glossaries():
    """Story 17: unapproved variants of canonical glossary terms flagged,
    against docs_context.corpus.glossaries; no glossary, no check."""
    body = _persona_body()
    assert "glossaries" in body
    assert "canonical" in body
    assert "corpus" in body


def test_persona_integrity_verifies_every_claim_class_with_evidence():
    """Story 18-20: files via the provided existence evidence (no re-walk),
    functions/modules/config keys by reading the named code, cited ADRs and
    documents read and never mischaracterized; capped at claims that would
    carry a finding, with the read breadth recorded."""
    body = _persona_body()
    for anchor in ("missing_paths", "unresolved_adr_refs", "dangling_links",
                   "functions, modules, and config keys", "cited",
                   "mischaracter", "reads"):
        assert anchor in body, anchor


def test_persona_integrity_misquotation_keeps_the_two_quote_discipline():
    """A mischaracterization finding quotes BOTH sides verbatim -- the citing
    ADR's claim and the cited document's actual words -- the same discipline
    the contradiction class carries."""
    body = _persona_body()
    assert "BOTH sides verbatim" in body
    for anchor in ("stale_passage", "existing_code"):
        assert anchor in body, anchor  # the quote fields stay in the contract


def test_persona_quality_findings_anchor_at_the_documentation_file():
    """Story 23: a documented exception to the code-anchoring rule, for the
    quality duties only -- the legacy anchoring sentence must survive."""
    body = _persona_body()
    assert "Anchor it at the **code file**" in body  # the legacy rule, intact
    assert "exception" in body
    assert "documentation file" in body


def test_skill_spawn_condition_gains_the_docs_quality_block():
    """The docs-checker step fires when the manifest has docs_context OR a
    docs_quality block, and is skipped only when it has neither."""
    skill = _skill_body()
    assert "only when the manifest has `docs_context` or `docs_quality`" in skill
    assert "neither `docs_context` nor `docs_quality`" in skill


def test_skill_specs_the_docs_quality_payload_contract():
    """The exact payload contract the gate plumbing (SPEC-14.4) must produce:
    doc-format diffs only, per-duty evidence, enabled-section flags."""
    skill = _skill_body()
    for key in _DOCS_QUALITY_PAYLOAD_KEYS:
        assert f"`{key}`" in skill or f'"{key}"' in skill, key
    # Per-duty evidence mapping: which evidence class feeds which duty.
    for anchor in ("enabled_duties", "deslop", "STE", "existence"):
        assert anchor in skill, anchor


def test_skill_quality_findings_merge_into_the_same_stream():
    """Quality findings merge into the findings array and flow through
    section 3a/3b/4 unchanged; doc anchoring is named the documented
    exception."""
    skill = _skill_body()
    assert "docs_quality" in skill
    assert "documented exception" in skill


def test_skill_and_persona_carry_the_same_quality_contract():
    """Cross-file consistency: every payload key and duty name the skill
    specs, the persona consumes -- the skill is pasted with the persona into
    one spawn, so they cannot disagree."""
    skill = _skill_body()
    persona = _persona_body()
    for name in list(_DUTY_NAMES) + list(_DOCS_QUALITY_PAYLOAD_KEYS):
        assert name in skill, f"skill must spec {name}"
        assert name in persona, f"persona must consume {name}"


def _write_checks_config(work, checks):
    """The per-repo checks override, uncommitted (rg_config reads the file)."""
    p = work / ".ocr" / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"checks": checks}), encoding="utf-8")


_ALL_DUTIES_OFF = {n: {"enabled": False} for n in _DUTY_NAMES}


def test_all_quality_duties_off_means_no_docs_quality_key_end_to_end(tmp_path):
    """Enable matrix, off-row, through the real hook: checks config with every
    built-in duty disabled, a markdown file in the push -- the manifest can
    carry no docs_quality key, so the skill's quality spawn condition cannot
    fire. (This must stay true after SPEC-14.4 wires the payload.)"""
    work = _repo(tmp_path, {"app.py": _APP_V1, "README.md": "# project\n"})
    _write_docs_summary(work)
    _write_checks_config(work, _ALL_DUTIES_OFF)
    _commit(work, {"app.py": _APP_V2,
                   "README.md": "# project\n\nrewritten docs section\n"})
    decision, reason = _push(work, _env(tmp_path, "t.trace",
                                        STUB_DOCS_SUMMARY=DOCS_SUMMARY))
    assert decision == "pass", reason
    manifest = _review_manifest(tmp_path, "t.trace")
    assert "docs_context" in manifest, "the legacy docs context is untouched"
    assert "docs_quality" not in manifest, manifest


def test_all_quality_duties_off_docs_only_push_stays_skipped(tmp_path):
    """Off-row for a docs-only push: with the duties disabled the push keeps
    today's behavior -- no manifest, no spawn, no cost."""
    work = _repo(tmp_path, {"app.py": _APP_V1, "README.md": "# project\n"})
    _write_docs_summary(work)
    _write_checks_config(work, _ALL_DUTIES_OFF)
    tip = _commit(work, {"README.md": "# project\n\nrewritten docs section\n"})
    env = _env(tmp_path, "t.trace",
               STUB_DOCS_FINDING=json.dumps({"content": _CONTRADICTION}))
    decision, reason = _push(work, env)
    assert decision == "pass", reason
    for c in _calls(tmp_path, "t.trace"):
        assert "docs_quality" not in (c.get("manifest") or {})
    st = _state(work, tip)
    assert st.get("state") == "done" and st.get("verdict") in ("skip", "skipped", "pass")


# ---------------------------------------------------------------------------
# SPEC-14.4: gate plumbing -- doc-diff trigger, the docs_quality manifest
# payload, the docs-only fallback, doc-anchored finding exemption, and
# command checks. Scenarios drive the real hook with the stub worker; the
# assertions pin external behavior (did the checker spawn, what the payload
# carried, did the push block) -- never internals.

import ocr_telemetry as _ot  # noqa: E402


def _docs_quality(tmp_path, trace="t.trace"):
    """The docs_quality block of this run's review manifest, or None."""
    return (_review_manifest(tmp_path, trace) or {}).get("docs_quality")


def _tele_run_records(work):
    """The runs' telemetry records (kind-less entries are the _TELE runs)."""
    common = review_gate._git_common_dir(str(work))
    return [r for r in _ot.load(common, days=1)]


def _quality_spec(path, duty, **over):
    """A quality-duty checker finding (the persona's classes 4-6 shape)."""
    spec = {
        "docs_quality_duty": duty,
        "path": path, "start_line": 1, "end_line": 6,
        "severity": "high", "confidence": 0.9,
        "category": "documentation",
        "existing_code": "## A robust, seamless solution",
        "content": duty + ": the prose reads as generated slurry",
    }
    spec.update(over)
    return spec


_GUIDE = "# Guide\n\nHow the project fits together.\n"
_ADR_RECORD_1 = "# ADR 0001: use git for reviews\n\nStatus: accepted\n\nContext: pushes.\n\nDecision: gate.\n"
_ADR_RECORD_2 = "# ADR 0002: verdicts in Python\n\nStatus: accepted\n\nContext: models drift.\n\nDecision: compute.\n"


# --- the doc-diff trigger: enable-mode x file-kind matrix --------------------

def test_deslop_on_docs_only_push_spawns_checker_with_quality_payload(tmp_path):
    """Docs-only push, deslop on (the default): the fallback path builds a
    manifest carrying docs_quality, so the review session spawns the checker
    and the quality duties run -- today this push built NO manifest at all."""
    work = _repo(tmp_path, {"README.md": "# project\n"})
    tip = _commit(work, {"README.md": "# project\n\nrewritten docs section\n"})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    calls = _calls(tmp_path, "t.trace")
    spawned = [c for c in calls if c.get("paths_file") and not c.get("resolve_file")]
    assert spawned, "a docs-only push with an enabled duty must spawn the checker"
    payload = _docs_quality(tmp_path)
    assert payload is not None, "the manifest carries docs_quality"
    assert payload["enabled_duties"] == {
        "docs-deslop": True, "adr-wait-what": False, "adr-integrity": False}
    files = payload["files"]
    assert [f["path"] for f in files] == ["README.md"]
    assert any("diff" in f or "diff_file" in f for f in files)
    deslop_ev = payload["evidence"]["docs-deslop"]
    assert sorted(deslop_ev) == ["README.md"]
    for key in ("catalog_hits", "throat_clearing", "em_dash_count",
                "template_skeletons"):
        assert key in deslop_ev["README.md"], key
    assert payload["options"]["docs-deslop"] == {}
    st = _state(work, tip)
    assert st.get("state") == "done" and st.get("verdict") == "pass"


def test_code_only_push_carries_no_docs_quality_key(tmp_path):
    """File-kind matrix, code-only row: nothing matching the duties' globs,
    so no docs_quality block and no quality cost."""
    work = _repo(tmp_path, {"app.py": _APP_V1})
    _write_docs_summary(work)
    tip = _commit(work, {"app.py": _APP_V2})
    decision, reason = _push(work, _env(tmp_path, "t.trace",
                                        STUB_DOCS_SUMMARY=DOCS_SUMMARY))
    assert decision == "pass", reason
    assert _docs_quality(tmp_path) is None
    st = _state(work, tip)
    assert st.get("verdict") == "pass"


def test_planned_lane_mixed_push_carries_docs_quality_doc_files_only(tmp_path):
    """Planned lane (a real review plan exists): the docs_quality block rides
    the same manifest; its files hold the doc-format entries ONLY, never code."""
    work = _repo(tmp_path, {"app.py": _APP_V1, "README.md": "# project\n"})
    _write_docs_summary(work)
    tip = _commit(work, {"app.py": _APP_V2, "README.md": "# project\n\nmore\n"})
    decision, reason = _push(work, _env(tmp_path, "t.trace",
                                        STUB_DOCS_SUMMARY=DOCS_SUMMARY))
    assert decision == "pass", reason
    payload = _docs_quality(tmp_path)
    assert payload is not None, "mixed push: the quality payload rides the manifest"
    assert [f["path"] for f in payload["files"]] == ["README.md"]
    assert "app.py" not in [f["path"] for f in payload["files"]]
    st = _state(work, tip)
    assert st.get("verdict") == "pass"


def test_adr_duty_flag_follows_the_adr_globs(tmp_path):
    """adr-wait-what enabled: an ADR under docs/adr flips its flag and gets
    STE evidence; the deslop flag stays true for the same doc file."""
    work = _repo(tmp_path, {"docs/adr/0001-verdicts.md": _ADR_RECORD_1})
    _write_checks_config(work, {"adr-wait-what": {"enabled": True}})
    _commit(work, {"docs/adr/0001-verdicts.md": _ADR_RECORD_1 + "\nMore context.\n"})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    payload = _docs_quality(tmp_path)
    assert payload is not None
    assert payload["enabled_duties"]["adr-wait-what"] is True
    assert payload["enabled_duties"]["adr-integrity"] is False
    ste = payload["evidence"]["adr-wait-what"]["docs/adr/0001-verdicts.md"]
    for key in ("sentence_length", "acronyms", "passive_voice"):
        assert key in ste, key


def test_adr_duty_stays_off_for_non_adr_markdown(tmp_path):
    """The same enabled ADR duty does not fire on a non-ADR markdown file;
    the deslop default still does."""
    work = _repo(tmp_path, {"docs/guide.md": _GUIDE})
    _write_checks_config(work, {"adr-wait-what": {"enabled": True},
                                "adr-integrity": {"enabled": True}})
    _commit(work, {"docs/guide.md": _GUIDE + "\nMore prose.\n"})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    payload = _docs_quality(tmp_path)
    assert payload is not None
    assert payload["enabled_duties"]["adr-wait-what"] is False
    assert payload["enabled_duties"]["adr-integrity"] is False
    assert "adr-wait-what" not in payload["evidence"]


def test_deep_lane_manifest_carries_the_quality_payload(tmp_path):
    """The deep lane feeds the checker too: a risky-path push escalates to
    the deep manifest, and the docs_quality block is injected there."""
    work = _repo(tmp_path, {"app/x.py": "x = 1\n", "README.md": "# project\n"})
    tip = _commit(work, {
        "app/x.py": "x = 2\n",
        "README.md": "# project\n\nrewritten docs section\n",
        ".ocr/rule.json": json.dumps({"risk_globs": ["app/**"],
                                      "risk_threshold": 9}),
    })
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    st = _state(work, tip)
    assert st.get("tier") == "deep", "the risky push escalated"
    manifest = _review_manifest(tmp_path, "t.trace")
    payload = manifest.get("docs_quality")
    assert payload is not None, "deep manifest carries docs_quality"
    assert payload["enabled_duties"]["docs-deslop"] is True
    assert [f["path"] for f in payload["files"]] == ["README.md"]


# --- doc-anchored findings: the exemption ------------------------------------

def test_deslop_cluster_finding_at_a_doc_path_blocks_end_to_end(tmp_path):
    """Story 23's exemption, proven: a docs-deslop finding anchored at the
    DOCUMENTATION file (never a review target) survives every findings
    filter, reaches compute_verdict, and blocks -- the denial quotes it."""
    work = _repo(tmp_path, {"README.md": "# project\n"})
    tip = _commit(work, {"README.md": "# project\n\nmore docs\n"})
    spec = _quality_spec("README.md", "docs-deslop", tells=[
        {"phrase": "robust", "line": 2},
        {"phrase": "seamless", "line": 3},
        {"phrase": "—", "line": 4},
    ])
    decision, reason = _push(work, _env(tmp_path, "t.trace",
                                        STUB_DOCS_FINDING=json.dumps(spec)))
    assert decision == "deny", reason
    assert "the prose reads as generated slurry" in reason, reason
    assert "README.md" in reason, reason
    st = _state(work, tip)
    assert st.get("verdict") == "block", st


def test_deslop_finding_below_the_cluster_rule_is_demoted_and_never_blocks(tmp_path):
    """The gate-side demotion pass: a docs-deslop finding with a single tell
    and no document_wide flag is demoted to note severity by the helper's
    --validate, so it can never block even at high claimed severity."""
    work = _repo(tmp_path, {"README.md": "# project\n"})
    tip = _commit(work, {"README.md": "# project\n\nmore docs\n"})
    spec = _quality_spec("README.md", "docs-deslop",
                         tells=[{"phrase": "delve", "line": 3}])
    decision, reason = _push(work, _env(tmp_path, "t.trace",
                                        STUB_DOCS_FINDING=json.dumps(spec)))
    assert decision == "pass", reason
    st = _state(work, tip)
    assert st.get("verdict") == "pass", st
    # Demoted gate-side: the content stays visible in the verdict record,
    # only as a note -- a confidence-0.9 high that reached compute_verdict
    # UNALTERED would have blocked.
    assert "[note]" in st.get("reasons", ""), st.get("reasons")


def test_document_wide_deslop_finding_survives_the_cluster_rule(tmp_path):
    """document_wide: true is the cluster rule's other path to blocking."""
    work = _repo(tmp_path, {"README.md": "# project\n"})
    _commit(work, {"README.md": "# project\n\nmore docs\n"})
    spec = _quality_spec("README.md", "docs-deslop", document_wide=True,
                         tells=[{"phrase": "delve", "line": 3}])
    decision, reason = _push(work, _env(tmp_path, "t.trace",
                                        STUB_DOCS_FINDING=json.dumps(spec)))
    assert decision == "deny", reason


def test_integrity_finding_without_tells_is_never_demoted(tmp_path):
    """The cluster rule is the DESLOP contract: an adr-integrity finding
    (which carries `reads`, not `tells`) must not be demoted by the
    --validate pass and blocks at its own severity."""
    work = _repo(tmp_path, {"docs/adr/0001-verdicts.md": _ADR_RECORD_1})
    _write_checks_config(work, {"adr-integrity": {"enabled": True}})
    tip = _commit(work, {"docs/adr/0001-verdicts.md": _ADR_RECORD_1 + "\nMore.\n"})
    spec = _quality_spec("docs/adr/0001-verdicts.md", "adr-integrity",
                         reads=["scripts/review-gate.py"])
    decision, reason = _push(work, _env(tmp_path, "t.trace",
                                        STUB_DOCS_FINDING=json.dumps(spec)))
    assert decision == "deny", reason
    st = _state(work, tip)
    assert st.get("verdict") == "block", st


# --- built-in duty modes (story 2) -------------------------------------------

def test_warn_mode_deslop_cluster_never_blocks(tmp_path):
    """Story 2: mode: warn is honored by the built-in duties, not only by
    command checks -- the same 3-tell cluster that BLOCKS under the default
    block mode is clamped below the block bar (severity at most medium,
    confidence at most 0.5), so the push lands with a warn. Mode only ever
    downgrades."""
    work = _repo(tmp_path, {"README.md": "# project\n"})
    _write_checks_config(work, {"docs-deslop": {"mode": "warn"}})
    tip = _commit(work, {"README.md": "# project\n\nmore docs\n"})
    spec = _quality_spec("README.md", "docs-deslop", tells=[
        {"phrase": "robust", "line": 2},
        {"phrase": "seamless", "line": 3},
        {"phrase": "--", "line": 4},
    ])
    decision, reason = _push(work, _env(tmp_path, "t.trace",
                                        STUB_DOCS_FINDING=json.dumps(spec)))
    assert decision == "pass", reason
    st = _state(work, tip)
    assert st.get("verdict") == "warn", st
    assert "the prose reads as generated slurry" in st.get("reasons", "")


def test_warn_mode_holds_under_a_lowered_block_bar(tmp_path):
    """Gate fix: the warn clamp follows the OPERATOR's block bar. With
    OCR_BLOCK_SEVERITY=medium the old clamp (severity capped at medium,
    confidence capped at 0.5) left this cluster AT the bar and the push
    blocked -- medium blocks at any confidence once the bar is lowered. The
    clamp now lands strictly below the effective bars, so warn mode warns
    and never blocks whatever the operator lowered the bars to."""
    work = _repo(tmp_path, {"README.md": "# project\n"})
    _write_checks_config(work, {"docs-deslop": {"mode": "warn"}})
    tip = _commit(work, {"README.md": "# project\n\nmore docs\n"})
    spec = _quality_spec("README.md", "docs-deslop", tells=[
        {"phrase": "robust", "line": 2},
        {"phrase": "seamless", "line": 3},
        {"phrase": "--", "line": 4},
    ])
    decision, reason = _push(work, _env(tmp_path, "t.trace",
                                        OCR_BLOCK_SEVERITY="medium",
                                        STUB_DOCS_FINDING=json.dumps(spec)))
    assert decision == "pass", reason
    st = _state(work, tip)
    assert st.get("verdict") == "warn", st


def test_warn_mode_adr_integrity_finding_never_blocks(tmp_path):
    """The clamp keys on the resolved per-duty mode for every quality duty,
    not only on the tell-carrying deslop class."""
    work = _repo(tmp_path, {"docs/adr/0001-verdicts.md": _ADR_RECORD_1})
    _write_checks_config(work, {"adr-integrity": {"enabled": True, "mode": "warn"}})
    tip = _commit(work, {"docs/adr/0001-verdicts.md": _ADR_RECORD_1 + "\nMore.\n"})
    spec = _quality_spec("docs/adr/0001-verdicts.md", "adr-integrity",
                         reads=["scripts/review-gate.py"])
    decision, reason = _push(work, _env(tmp_path, "t.trace",
                                        STUB_DOCS_FINDING=json.dumps(spec)))
    assert decision == "pass", reason
    st = _state(work, tip)
    assert st.get("verdict") == "warn", st


# --- the deterministic pre-pass notes (story 12) -----------------------------

_SLOPPY = ("# Notes\n\nLet's delve into the robust platform.\n"
           "This PR leverages the new module.\n")


def test_deterministic_scan_hits_emit_note_findings(tmp_path):
    """Story 12: the deterministic pre-pass hits the checker consumes as
    evidence also flow to the report -- as note-severity findings, tagged
    deterministic, one per file. A note ranks below every blocking tier in
    compute_verdict, so a note-only review PASSES (never even warns)."""
    work = _repo(tmp_path, {"README.md": "# project\n"})
    tip = _commit(work, {"README.md": "# project\n\n" + _SLOPPY})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    st = _state(work, tip)
    assert st.get("verdict") == "pass", st
    reasons = st.get("reasons", "")
    assert "[note]" in reasons, reasons
    assert "README.md" in reasons, reasons
    records = _tele_run_records(work)
    tele_findings = [f for r in records for f in (r.get("findings") or [])]
    notes = [f for f in tele_findings
             if f.get("severity") == "note" and f.get("path") == "README.md"]
    assert len(notes) == 1, "one aggregated note per file, not one per hit"


def test_deterministic_notes_respect_the_enabled_switch(tmp_path):
    """Only for enabled duties: with docs-deslop off, the same slop leaves
    zero trace -- no notes, no cost."""
    work = _repo(tmp_path, {"README.md": "# project\n"})
    _write_checks_config(work, {"docs-deslop": {"enabled": False}})
    tip = _commit(work, {"README.md": "# project\n\n" + _SLOPPY})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    st = _state(work, tip)
    assert st.get("verdict") == "pass", st
    assert "[note]" not in st.get("reasons", ""), st.get("reasons")


def test_scan_notes_aggregate_one_note_per_file(tmp_path):
    """Unit seam: two sloppy files plus a clean one, fed as diff entries --
    one capped-tells note per sloppy file, tagged, note-severity."""
    _write(tmp_path, {
        "docs/a.md": _SLOPPY,
        "docs/b.md": "Let's delve into the robust module.\n",
        "docs/c.md": "A plain sentence.\n",
    })
    entries = [{"path": p, "status": "M"}
               for p in ("docs/a.md", "docs/b.md", "docs/c.md")]
    notes, warnings = review_gate._docs_quality_scan_notes(
        str(tmp_path), str(tmp_path), None, None, entries=entries)
    assert warnings == []
    assert [n["path"] for n in notes] == ["docs/a.md", "docs/b.md"]
    for n in notes:
        assert n["severity"] == "note"
        assert n["docs_quality_duty"] == "docs-deslop"
        assert n["deterministic"] is True
        assert n["tells"], "the note carries the aggregated tells"
        assert len(n["tells"]) <= review_gate._DETERMINISTIC_NOTE_TELLS_CAP
    a = notes[0]
    assert any(t.get("phrase") == "This PR" for t in a["tells"]), a["tells"]
    assert any("delve" in str(t.get("phrase")) for t in a["tells"]), a["tells"]


# --- command checks (story 4) ------------------------------------------------

_CHECK_BLOCK_SH = (
    "#!/bin/sh\n"
    "files=\"$(tr -d '[]\" ' </dev/stdin)\"\n"
    "printf '{\"findings\": [{\"path\": \"app.py\", \"start_line\": 1, "
    "\"end_line\": 1, \"severity\": \"high\", \"confidence\": 0.95, "
    "\"category\": \"custom\", \"content\": \"dsl-check tripped on: %s\"}]}' \"$files\"\n"
)


def _repo_with_check(tmp_path, check_body, mode=None, globs=("**/*.py",)):
    """A repo carrying an executable repo-relative command check, with the
    check declared in .ocr/config.json (uncommitted override)."""
    work = _repo(tmp_path, {"app.py": _APP_V1})
    p = work / "scripts" / "dslcheck.sh"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(check_body, encoding="utf-8")
    p.chmod(0o755)
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "add check"], cwd=work)
    declaration = {"command": "scripts/dslcheck.sh", "globs": list(globs)}
    if mode:
        declaration["mode"] = mode
    _write_checks_config(work, {"dsl-check": declaration})
    return work


def test_command_check_findings_block_and_are_tagged(tmp_path):
    """A registered command check runs on its matching changed files
    (repo-relative executable, cwd the repo root, changed-file list as stdin
    JSON); its findings join the stream, tagged with the check name, and a
    block-mode finding blocks -- default mode is block."""
    work = _repo_with_check(tmp_path, _CHECK_BLOCK_SH)
    tip = _commit(work, {"app.py": _APP_V2})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "deny", reason
    assert "dsl-check tripped" in reason, reason
    assert "app.py" in reason, "the check saw the matching changed-file list"
    st = _state(work, tip)
    assert st.get("verdict") == "block", st
    records = _tele_run_records(work)
    assert any(r.get("command_checks") for r in records), records


def test_command_check_warn_mode_never_blocks(tmp_path):
    """mode: warn downgrades, never upgrades: the same blocking-shaped
    finding is clamped below the block bar, so the push lands with a warn."""
    work = _repo_with_check(tmp_path, _CHECK_BLOCK_SH, mode="warn")
    tip = _commit(work, {"app.py": _APP_V2})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    st = _state(work, tip)
    assert st.get("verdict") == "warn", st
    assert "dsl-check tripped" in st.get("reasons", "")


def test_command_check_malformed_output_degrades_and_never_crashes(tmp_path):
    """Unparseable check output: the check's findings are discarded, a
    warning + telemetry record is kept, and the push lands -- never silently,
    never crashing the gate."""
    work = _repo_with_check(tmp_path, "#!/bin/sh\ncat >/dev/null\necho 'not json'\n")
    tip = _commit(work, {"app.py": _APP_V2})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    st = _state(work, tip)
    assert st.get("state") == "done" and st.get("verdict") == "pass", st
    records = [r for r in _tele_run_records(work) if r.get("command_checks")]
    assert records, "a telemetry record per command-check run"
    entry = records[-1]["command_checks"][0]
    assert entry.get("error"), entry


def test_command_check_nonzero_exit_degrades_and_never_crashes(tmp_path):
    """A failing check (nonzero exit) is the same degrade path: warn +
    telemetry, findings discarded, push lands."""
    work = _repo_with_check(tmp_path, "#!/bin/sh\ncat >/dev/null\nexit 3\n")
    tip = _commit(work, {"app.py": _APP_V2})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    st = _state(work, tip)
    assert st.get("verdict") == "pass", st
    records = [r for r in _tele_run_records(work) if r.get("command_checks")]
    assert records and records[-1]["command_checks"][0].get("error")


def test_command_check_does_not_run_without_matching_files(tmp_path):
    """Story 5: no push pays for a check it cannot trip. A check whose globs
    match nothing in the push is skipped entirely -- its (blocking) output
    can never appear."""
    work = _repo_with_check(tmp_path, _CHECK_BLOCK_SH, globs=["**/*.sql"])
    tip = _commit(work, {"app.py": _APP_V2})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    assert "dsl-check tripped" not in (_state(work, tip).get("reasons", ""))


def test_command_check_fails_soft_when_the_command_is_missing(tmp_path):
    """A declared command that does not exist on disk degrades to warn +
    telemetry, never a crash."""
    work = _repo(tmp_path, {"app.py": _APP_V1})
    _write_checks_config(work, {
        "ghost": {"command": "scripts/does-not-exist.sh", "globs": ["**/*.py"]}})
    tip = _commit(work, {"app.py": _APP_V2})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    st = _state(work, tip)
    assert st.get("verdict") == "pass", st
    records = [r for r in _tele_run_records(work) if r.get("command_checks")]
    assert records and records[-1]["command_checks"][0].get("error")


def test_command_check_absolute_path_is_rejected(tmp_path):
    """The contract is a REPO-RELATIVE executable: an absolute command path
    is rejected with a warning plus telemetry before anything runs -- the
    gate never executes a binary outside the reviewed repo."""
    work = _repo(tmp_path, {"app.py": _APP_V1})
    _write_checks_config(work, {
        "abs-check": {"command": "/bin/echo", "globs": ["**/*.py"]}})
    tip = _commit(work, {"app.py": _APP_V2})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    st = _state(work, tip)
    assert st.get("verdict") == "pass", st
    records = [r for r in _tele_run_records(work) if r.get("command_checks")]
    assert records, "a telemetry record for the skipped check"
    error = records[-1]["command_checks"][0].get("error", "")
    assert "repo-relative" in error, error


def test_command_check_dotdot_escape_is_rejected(tmp_path):
    """.. components are the same escape: a command pointing outside the
    repo tree is rejected before execution, same degrade path."""
    work = _repo(tmp_path, {"app.py": _APP_V1})
    _write_checks_config(work, {
        "escape": {"command": "../outside/run.sh", "globs": ["**/*.py"]}})
    tip = _commit(work, {"app.py": _APP_V2})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    st = _state(work, tip)
    assert st.get("verdict") == "pass", st
    records = [r for r in _tele_run_records(work) if r.get("command_checks")]
    assert records
    error = records[-1]["command_checks"][0].get("error", "")
    assert "repo-relative" in error, error


def test_command_check_config_load_failure_is_observed(monkeypatch, tmp_path, capsys):
    """ADR 0006 (fail-soft AND observable): a checks-config load failure
    inside the command-check pass must surface a warning and a telemetry
    record before returning -- today the try/except returns silently."""
    import rg_config

    def boom(_repo_root=None):
        raise RuntimeError("synthetic store failure")

    monkeypatch.setattr(rg_config, "load_settings", boom)
    review_gate._TELE.pop("command_checks", None)
    findings, warnings = review_gate._command_check_findings(
        str(tmp_path), str(tmp_path), None, None)
    assert findings == []
    assert any("checks config" in str(w.get("message", "")) for w in warnings), warnings
    tele = review_gate._TELE.get("command_checks") or []
    assert tele and "synthetic store failure" in str(tele[-1].get("error")), tele
    assert "synthetic store failure" in capsys.readouterr().err


# --- SPEC-15.4: warnings field + evidence_source (executor contract) --------

def _run_check(tmp_path, body):
    """Direct seam: run ONE repo-relative command check body against
    app.py and hand back the executor-contract triple
    (findings, warnings, error_note) -- no push, no reviewer. (The fourth
    element, the executor's telemetry object, is covered by the SPEC-15
    suites.)"""
    p = tmp_path / "scripts" / "dslcheck.sh"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    p.chmod(0o755)
    got = review_gate._run_one_command_check(
        str(tmp_path), "scripts/dslcheck.sh", ["app.py"])
    return got[0], got[1], got[2]


def _evidence_body(evidence_source=None, warnings=None):
    """A check body answering with a findings+warnings wrapper; the finding
    carries the given evidence_source and the wrapper the given warnings."""
    finding = {"path": "app.py", "start_line": 1, "end_line": 1,
               "severity": "high", "confidence": 0.95,
               "category": "custom", "content": "race check tripped"}
    if evidence_source is not None:
        finding["evidence_source"] = evidence_source
    payload = {"findings": [finding]}
    if warnings is not None:
        payload["warnings"] = warnings
    return ("#!/bin/sh\ncat >/dev/null\ncat <<'OCRJSON'\n"
            + json.dumps(payload) + "\nOCRJSON\n")


def test_command_check_findings_plus_warnings_wrapper_parses_both_arrays(tmp_path):
    """SPEC-15.4: a check may answer {"findings": [...], "warnings": [...]};
    both arrays parse -- the findings survive and the warnings surface in
    the same (findings, warnings) channel every other check degradation
    uses."""
    findings, warnings, note = _run_check(
        tmp_path, _evidence_body(warnings=["no permanent coverage for the "
                                           "race probe: tests/dsl.py missing"]))
    assert note is None
    assert [f["content"] for f in findings] == ["race check tripped"]
    assert len(warnings) == 1
    assert "no permanent coverage" in str(warnings[0].get("message"))


def test_command_check_bare_and_findings_only_shapes_unchanged(tmp_path):
    """Regression: the pre-15.4 shapes still work exactly as before -- a
    bare findings array and a findings-only wrapper carry no warnings."""
    bare = ("#!/bin/sh\ncat >/dev/null\n"
            "printf '[{\"path\": \"app.py\", \"start_line\": 1, \"end_line\": 1, "
            "\"severity\": \"high\", \"confidence\": 0.95, "
            "\"category\": \"custom\", \"content\": \"bare tripped\"}]'\n")
    findings, warnings, note = _run_check(tmp_path, bare)
    assert note is None and warnings == []
    assert [f["content"] for f in findings] == ["bare tripped"]
    findings, warnings, note = _run_check(tmp_path, _evidence_body())
    assert note is None and warnings == []
    assert [f["content"] for f in findings] == ["race check tripped"]


def test_command_check_malformed_warnings_field_degrades_never_fails(tmp_path):
    """A malformed warnings field (not an array, or junk entries) degrades
    to a note and the findings survive -- never a failure, never a discard."""
    findings, warnings, note = _run_check(
        tmp_path, _evidence_body(warnings="oops, a string"))
    assert note is None, "a malformed warnings field is never a failure"
    assert [f["content"] for f in findings] == ["race check tripped"]
    assert any("warnings" in str(w.get("message")) for w in warnings), warnings
    findings, warnings, note = _run_check(
        tmp_path, _evidence_body(warnings=["ok", 7]))
    assert note is None
    assert [f["content"] for f in findings] == ["race check tripped"]
    messages = [str(w.get("message")) for w in warnings]
    assert any("ok" in m for m in messages), messages
    assert any("ignored" in m for m in messages), messages


def test_command_check_valid_evidence_source_flows_to_output_and_telemetry(tmp_path):
    """A valid evidence_source tag (existing_test) flows through the findings
    stream into the verdict output and telemetry, and the check's warnings
    surface alongside it (per-check warnings count included)."""
    work = _repo_with_check(
        tmp_path, _evidence_body(evidence_source="existing_test",
                                 warnings=["no permanent coverage yet"]))
    tip = _commit(work, {"app.py": _APP_V2})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "deny", reason
    assert "race check tripped" in reason, reason
    st = _state(work, tip)
    assert st.get("verdict") == "block", st
    records = [r for r in _tele_run_records(work) if r.get("command_checks")]
    assert records, "telemetry carries the command-check run"
    entry = records[-1]["command_checks"][0]
    assert entry.get("warnings") == 1, entry
    tagged = [f for f in records[-1].get("findings") or []
              if f.get("path") == "app.py"]
    assert tagged and tagged[0].get("evidence_source") == "existing_test", tagged
    run_warnings = [str(w.get("message")) for w in records[-1].get("warnings") or []]
    assert any("no permanent coverage" in m for m in run_warnings), run_warnings


def test_command_check_unknown_evidence_source_degrades_tag_not_finding(tmp_path):
    """An unknown evidence_source value degrades the tag with a note while
    the finding itself survives (still blocks); the tag is dropped from
    output and telemetry."""
    work = _repo_with_check(
        tmp_path, _evidence_body(evidence_source="gut_feeling"))
    tip = _commit(work, {"app.py": _APP_V2})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "deny", reason
    assert "race check tripped" in reason, "the finding survives the tag degrade"
    st = _state(work, tip)
    assert st.get("verdict") == "block", st
    records = [r for r in _tele_run_records(work) if r.get("command_checks")]
    assert records
    tagged = [f for f in records[-1].get("findings") or []
              if f.get("path") == "app.py"]
    assert tagged and not tagged[0].get("evidence_source"), tagged
    run_warnings = [str(w.get("message")) for w in records[-1].get("warnings") or []]
    assert any("evidence_source" in m for m in run_warnings), run_warnings


def test_command_check_ephemeral_pairing_warning_lands_in_warnings_not_findings(tmp_path):
    """Provenance pairing (SPEC-15.4): an ephemeral_test finding without the
    check's no-permanent-coverage warning produces a gate-side WARNING on
    the same channel as the check's own degradations -- never a finding
    (findings counts stay honest, the note is not itself waivable), never
    a verdict change, never fatal. With the coverage warning present, no
    pairing warning fires."""
    findings, warnings, note = _run_check(
        tmp_path, _evidence_body(evidence_source="ephemeral_test"))
    assert note is None
    # Only the real finding rides the findings stream.
    assert [f.get("content") for f in findings] == ["race check tripped"], findings
    msgs = [str(w.get("message")) for w in warnings]
    assert any("no-permanent-coverage" in m for m in msgs), msgs
    findings, warnings, note = _run_check(
        tmp_path, _evidence_body(evidence_source="ephemeral_test",
                                 warnings=["no permanent coverage for the probe"]))
    assert note is None
    assert not [w for w in warnings
                if "no-permanent-coverage" in str(w.get("message"))], warnings


def test_command_check_coverage_pairing_needs_a_negation_not_a_mention(tmp_path):
    """The pairing heuristic matches the no-permanent-coverage SEMANTIC only:
    a warning that states the lack ("missing test coverage") pairs, one that
    merely MENTIONS coverage ("coverage is 82%") does not -- the reference
    executor (SPEC-15.5) must word its warning as a stated lack."""
    findings, warnings, note = _run_check(
        tmp_path, _evidence_body(evidence_source="ephemeral_test",
                                 warnings=["coverage is 82% on this branch"]))
    assert note is None
    msgs = [str(w.get("message")) for w in warnings]
    assert any("no-permanent-coverage" in m for m in msgs), msgs
    findings, warnings, note = _run_check(
        tmp_path, _evidence_body(evidence_source="ephemeral_test",
                                 warnings=["missing test coverage for the probe"]))
    assert note is None
    assert not [w for w in warnings
                if "no-permanent-coverage" in str(w.get("message"))], warnings


def test_command_check_ephemeral_pairing_warning_reaches_telemetry(tmp_path):
    """The pairing warning rides the warnings channel end to end: a push whose
    ephemeral finding lacks the coverage warning lands the pairing message in
    telemetry warnings (never in the findings list, never a verdict change
    on its own)."""
    work = _repo_with_check(
        tmp_path, _evidence_body(evidence_source="ephemeral_test"))
    tip = _commit(work, {"app.py": _APP_V2})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "deny", reason  # the high finding blocks, not the warning
    records = [r for r in _tele_run_records(work) if r.get("command_checks")]
    assert records
    rec = records[-1]
    assert not [f for f in rec.get("findings") or []
                if f.get("severity") == "note"], rec.get("findings")
    run_warnings = [str(w.get("message")) for w in rec.get("warnings") or []]
    assert any("no-permanent-coverage" in m for m in run_warnings), run_warnings


def test_command_check_per_check_timeout_degrades_with_its_own_message(tmp_path):
    """SPEC-15.1: a check's `timeout` config is consumed -- a check with
    timeout: 1 whose command sleeps 2 degrades with the per-check timeout
    message ("timed out after 1s"), findings discarded, warning plus
    telemetry, never a stuck gate."""
    body = ("#!/bin/sh\ncat >/dev/null\nsleep 2\n"
            "printf '[]'\n")
    work = _repo_with_check(tmp_path, body)
    _write_checks_config(work, {
        "slow": {"command": "scripts/dslcheck.sh", "globs": ["**/*.py"],
                 "timeout": 1}})
    tip = _commit(work, {"app.py": _APP_V2})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    records = [r for r in _tele_run_records(work) if r.get("command_checks")]
    assert records
    error = str(records[-1]["command_checks"][0].get("error") or "")
    assert "timed out after 1s" in error, error
    assert "findings discarded" in error, error


# --- the sweep option (story 20) ----------------------------------------------

def test_sweep_true_expands_integrity_evidence_to_the_whole_records_root(tmp_path):
    """sweep: true on adr-integrity widens its evidence targets from the diff
    to every ADR under the records root; the diff-file list itself stays the
    change set, and the (non-sweeping) deslop evidence stays diff-scoped."""
    work = _repo(tmp_path, {"docs/adr/0001-first.md": _ADR_RECORD_1,
                            "docs/adr/0002-second.md": _ADR_RECORD_2})
    _write_checks_config(work, {"adr-integrity": {"enabled": True, "sweep": True}})
    _commit(work, {"docs/adr/0002-second.md": _ADR_RECORD_2 + "\nConsequences: ok.\n"})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    payload = _docs_quality(tmp_path)
    assert payload["enabled_duties"]["adr-integrity"] is True
    assert [f["path"] for f in payload["files"]] == ["docs/adr/0002-second.md"]
    integrity = payload["evidence"]["adr-integrity"]
    assert sorted(integrity) == ["docs/adr/0001-first.md",
                                 "docs/adr/0002-second.md"]
    for facts in integrity.values():
        for key in ("missing_paths", "dangling_links", "unresolved_adr_refs"):
            assert key in facts, key
    assert sorted(payload["evidence"]["docs-deslop"]) == ["docs/adr/0002-second.md"]


def test_sweep_default_off_keeps_evidence_scoped_to_the_changed_adrs(tmp_path):
    """Default off: an unchanged ADR is never a target, so push cost does not
    grow with the corpus."""
    work = _repo(tmp_path, {"docs/adr/0001-first.md": _ADR_RECORD_1,
                            "docs/adr/0002-second.md": _ADR_RECORD_2})
    _write_checks_config(work, {"adr-integrity": {"enabled": True}})
    _commit(work, {"docs/adr/0002-second.md": _ADR_RECORD_2 + "\nConsequences: ok.\n"})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    payload = _docs_quality(tmp_path)
    assert sorted(payload["evidence"]["adr-integrity"]) == ["docs/adr/0002-second.md"]


def test_duty_options_pass_through_and_sentence_cap_reaches_the_evidence(tmp_path):
    """The repo's per-duty checks options travel in the payload, and the
    sentence cap reaches the STE scan."""
    work = _repo(tmp_path, {"docs/adr/0001-verdicts.md": _ADR_RECORD_1})
    _write_checks_config(work, {
        "adr-wait-what": {"enabled": True, "sentence_cap": 12,
                          "sections": ["Status", "Context", "Decision"]},
        "adr-integrity": {"enabled": True, "template": "docs/adr/template.md"},
    })
    _commit(work, {"docs/adr/0001-verdicts.md": _ADR_RECORD_1 + "\nMore.\n"})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    payload = _docs_quality(tmp_path)
    opts = payload["options"]
    assert opts["adr-wait-what"]["sections"] == ["Status", "Context", "Decision"]
    assert opts["adr-wait-what"]["sentence_cap"] == 12
    assert opts["adr-integrity"]["template"] == "docs/adr/template.md"
    ste = payload["evidence"]["adr-wait-what"]["docs/adr/0001-verdicts.md"]
    assert ste["sentence_length"]["cap"] == 12


# ---------------------------------------------------------------------------
# SPEC-14.6: guilty-until-proven-innocent -- a claim a changed ADR makes
# about PRESENT REALITY that the checker cannot verify is a BLOCKING
# adr-integrity finding, never a silent skip (house rule ADR 0001). Two
# persona-contract pins + the gate-side scenarios through the stub checker:
# the stub emits the finding the persona's rule demands (or emits nothing --
# the forward-looking row), and the gate behavior is what the tests pin.


def _findings_log_last(work):
    """The last verdict-history entry (append-only FINDINGS_LOG): its
    findings are stored verbatim post normalization/verdict-assembly, so
    this is where field-preservation through the seam is provable."""
    common = review_gate._git_common_dir(str(work))
    path = review_gate._findings_log_path(common)
    lines = [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert lines, "a completed review must leave a findings-log entry"
    return json.loads(lines[-1])


_UNVERIFIABLE_CLAIM = "The gate retries a failed review three times."


def _unverifiable_claim_spec():
    """A quality-duty checker finding in the persona's SPEC-14.6 unverifiable
    shape: doc-anchored at the ADR, duty adr-integrity, quoting the claim
    verbatim (existing_code), naming what could not be verified (content),
    listing the reads attempted, and carrying the verified-claim count."""
    return _quality_spec(
        "docs/adr/0001-verdicts.md", "adr-integrity",
        existing_code=_UNVERIFIABLE_CLAIM,
        content=("adr-integrity: the claim \"" + _UNVERIFIABLE_CLAIM + "\" "
                 "could not be verified -- no gate code or doc at the tip "
                 "settles the retry behavior; reads attempted: "
                 "scripts/review-gate.py, README.md. The push unblocks by "
                 "touching a code file that references the claim."),
        reads=["scripts/review-gate.py", "README.md"],
        verified_claims=2)


def test_persona_integrity_unverifiable_claim_is_a_blocking_finding():
    """The unverifiable-claim rule in the persona's duty 6 (the block is a
    persona-contract change; the gate only needs findings to pass through
    its existing seam): present-reality scoping, the quote/name/reads
    discipline, blocking posture, never silent, and the house-rule
    rationale pointing at the one verdict authority."""
    body = _persona_body()
    sec = body[body.index("ADR claim integrity"):]
    assert "present reality" in sec.lower()
    assert "unverifiable" in sec.lower()
    assert "guilty until proven innocent" in sec.lower()
    assert "BLOCKING" in sec
    assert "never silent" in sec.lower() or "silently" in sec.lower()
    assert "reads attempted" in sec.lower()
    assert "quotes the claim" in sec.lower() and "verbatim" in sec.lower()
    assert "verdict authority" in sec.lower()


def test_persona_integrity_present_reality_scoping_guard():
    """The scoping guard is explicit: forward-looking statements --
    intentions, predictions, expected consequences -- are NOT claims and
    never fail on unverifiability; they cannot go down with the rule."""
    body = _persona_body()
    sec = body[body.index("ADR claim integrity"):]
    assert "forward-looking" in sec.lower()
    assert "are NOT claims" in sec
    assert "never fail on unverifiability" in sec
    for anchor in ("intentions", "predictions", "consequences"):
        assert anchor in sec, anchor


def test_persona_integrity_records_the_verified_claim_count():
    """Verified-claim accounting: every integrity finding carries a
    `verified_claims` summary field -- the audit counterpart of the `reads`
    trail -- telemetry only, never a gate (the deslop `score` discipline)."""
    body = _persona_body()
    assert "`verified_claims`" in body
    assert "verified TRUE" in body
    # The telemetry-only posture rides beside it, worded as telemetry.
    assert "telemetry" in body
    for anchor in ("never a gate", "never block"):
        assert anchor in body.lower(), anchor


def test_unverifiable_claim_finding_blocks_end_to_end(tmp_path):
    """Story: an unverifiable-claim finding BLOCKS the push. The stub plays
    the checker emitting it against an ADR push with the duty enabled; the
    finding is doc-anchored, carries docs_quality_duty "adr-integrity", and
    its denial quotes the claim verbatim."""
    work = _repo(tmp_path, {"docs/adr/0001-verdicts.md": _ADR_RECORD_1})
    _write_checks_config(work, {"adr-integrity": {"enabled": True}})
    tip = _commit(work, {"docs/adr/0001-verdicts.md": _ADR_RECORD_1 + "\nMore.\n"})
    decision, reason = _push(
        work, _env(tmp_path, "t.trace",
                   STUB_DOCS_FINDING=json.dumps(_unverifiable_claim_spec())))
    assert decision == "deny", reason
    assert _UNVERIFIABLE_CLAIM in reason, "the denial names the blocked claim"
    st = _state(work, tip)
    assert st.get("verdict") == "block", st


def test_unverifiable_claim_finding_survives_verdict_assembly_unaltered(tmp_path):
    """The same finding passes the falsify/normalization seam unaltered:
    duty tag, claim quote, reads trail, and the verified_claims count all
    reach the verbatim findings record intact."""
    work = _repo(tmp_path, {"docs/adr/0001-verdicts.md": _ADR_RECORD_1})
    _write_checks_config(work, {"adr-integrity": {"enabled": True}})
    _commit(work, {"docs/adr/0001-verdicts.md": _ADR_RECORD_1 + "\nMore.\n"})
    decision, _ = _push(
        work, _env(tmp_path, "t.trace",
                   STUB_DOCS_FINDING=json.dumps(_unverifiable_claim_spec())))
    assert decision == "deny"
    entry = _findings_log_last(work)
    f = next(x for x in (entry.get("findings") or [])
             if x.get("docs_quality_duty") == "adr-integrity")
    assert f["path"] == "docs/adr/0001-verdicts.md"  # doc-anchored
    assert f["category"] == "documentation"
    assert f["existing_code"] == _UNVERIFIABLE_CLAIM  # the claim, verbatim
    assert f["reads"] == ["scripts/review-gate.py", "README.md"]
    assert f["verified_claims"] == 2
    assert not f.get("tells"), "not a deslop finding: no cluster-rule bar"


def test_warn_mode_clamp_applies_to_an_unverifiable_claim_finding(tmp_path):
    """The new block is still a quality-duty finding: a warn-mode integrity
    duty demotes it like any other quality finding (the clamp from the
    review fixes) -- the push lands with a warn, never blocked bypassing
    compute_verdict's rules."""
    work = _repo(tmp_path, {"docs/adr/0001-verdicts.md": _ADR_RECORD_1})
    _write_checks_config(work, {"adr-integrity": {"enabled": True, "mode": "warn"}})
    tip = _commit(work, {"docs/adr/0001-verdicts.md": _ADR_RECORD_1 + "\nMore.\n"})
    decision, _ = _push(
        work, _env(tmp_path, "t.trace",
                   STUB_DOCS_FINDING=json.dumps(_unverifiable_claim_spec())))
    assert decision == "pass"
    st = _state(work, tip)
    assert st.get("verdict") == "warn", st
    entry = _findings_log_last(work)
    f = next(x for x in (entry.get("findings") or [])
             if x.get("docs_quality_duty") == "adr-integrity")
    assert f["severity"] != "high" or float(f["confidence"]) <= 0.5, f


def test_forward_looking_statement_produces_no_integrity_finding(tmp_path):
    """The scope guard through the stub path: only findings the checker
    EMITS count. An ADR carrying only forward-looking claims pushes with
    the duty enabled and no STUB_DOCS_FINDING (the checker's honest output
    for a forward-looking-only change) -- the push lands clean, no
    adr-integrity finding anywhere, the duty still enabled in the payload."""
    work = _repo(tmp_path, {"docs/adr/0001-verdicts.md": _ADR_RECORD_1})
    _write_checks_config(work, {"adr-integrity": {"enabled": True}})
    forward_only = (
        _ADR_RECORD_1
        + "\nConsequences: this decision will make future pushes cleaner, "
          "and the gate is expected to stay self-hosting.\n")
    tip = _commit(work, {"docs/adr/0001-verdicts.md": forward_only})
    decision, reason = _push(work, _env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    st = _state(work, tip)
    assert st.get("verdict") == "pass", st
    payload = _docs_quality(tmp_path)
    assert payload["enabled_duties"]["adr-integrity"] is True
    entry = _findings_log_last(work)
    assert not any(x.get("docs_quality_duty") == "adr-integrity"
                   for x in (entry.get("findings") or []))


# --- W9: the telemetry field + the exported per-check budget (SPEC-15.4) ----

def _run_check_four(tmp_path, body, timeout=None):
    """Direct seam returning the full executor-contract quadruple
    (findings, warnings, error_note, telemetry)."""
    p = tmp_path / "scripts" / "dslcheck.sh"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    p.chmod(0o755)
    kw = {} if timeout is None else {"timeout": timeout}
    return review_gate._run_one_command_check(
        str(tmp_path), "scripts/dslcheck.sh", ["app.py"], **kw)


def test_per_check_budget_is_exported_without_losing_the_base_env(
        tmp_path, monkeypatch):
    """The resolved per-check timeout reaches the executor's environment as
    OCR_CHECK_TIMEOUT_S (string seconds) ON TOP of the gate's own
    environment -- a cooperative executor can clamp its phases to finish
    before the gate's kill."""
    monkeypatch.setenv("OCR_TEST_BASE_ENV_MARK", "yes")
    body = ("#!/bin/sh\n"
            "cat >/dev/null\n"
            "printf '{\"findings\": [{\"path\": \"app.py\", "
            "\"start_line\": 1, \"end_line\": 1, \"severity\": \"high\", "
            "\"confidence\": 0.95, \"category\": \"custom\", "
            "\"content\": \"%s %s\"}]}' "
            "\"$OCR_CHECK_TIMEOUT_S\" \"$OCR_TEST_BASE_ENV_MARK\"\n")
    findings, warnings, note, tele = _run_check_four(tmp_path, body, timeout=120)
    assert note is None, note
    assert [f["content"] for f in findings] == ["120 yes"], findings


def test_malformed_telemetry_field_degrades_to_a_note(tmp_path):
    """A malformed telemetry field (not an object) is a note, the findings
    survive, and nothing merges -- never a failure, never a discard."""
    payload = {"findings": [{"path": "app.py", "start_line": 1,
                             "end_line": 1, "severity": "high",
                             "confidence": 0.95, "category": "custom",
                             "content": "race check tripped"}],
               "telemetry": "oops, a string"}
    body = ("#!/bin/sh\ncat >/dev/null\ncat <<'OCRJSON'\n"
            + json.dumps(payload) + "\nOCRJSON\n")
    findings, warnings, note, tele = _run_check_four(tmp_path, body)
    assert note is None and tele == {}
    assert [f["content"] for f in findings] == ["race check tripped"]
    assert any("malformed telemetry field" in str(w.get("message"))
              for w in warnings), warnings


def test_malformed_telemetry_values_drop_only_the_bad_entries(tmp_path):
    """Non-scalar telemetry values drop their own entries with a note;
    the well-formed counts still merge."""
    payload = {"findings": [], "telemetry": {"runs": 2, "bad": [1, 2]}}
    body = ("#!/bin/sh\ncat >/dev/null\ncat <<'OCRJSON'\n"
            + json.dumps(payload) + "\nOCRJSON\n")
    findings, warnings, note, tele = _run_check_four(tmp_path, body)
    assert note is None and findings == []
    assert tele == {"runs": 2}, tele
    assert any("malformed telemetry values for bad" in str(w.get("message"))
               for w in warnings), warnings


def test_command_checks_telemetry_object_merges_into_the_check_entry(
        tmp_path, monkeypatch):
    """A well-formed telemetry object merges into THAT check's
    command_checks entry (the tele_sink path the parallel worker uses
    included): counts observed by the executor ride the gate's telemetry."""
    import rg_config

    payload = {"findings": [], "telemetry": {"tests_selected": 1,
                                             "selector_attempts": 1}}
    body = ("#!/bin/sh\ncat >/dev/null\ncat <<'OCRJSON'\n"
            + json.dumps(payload) + "\nOCRJSON\n")
    work = _repo_with_check(tmp_path, body)
    monkeypatch.setattr(rg_config, "load_settings",
                        lambda repo_root=None: {
                            "checks": {"dsl-check": {
                                "command": "scripts/dslcheck.sh",
                                "globs": ["**/*.py"]}}})
    review_gate._TELE.pop("command_checks", None)
    sink = []
    findings, warnings = review_gate._command_check_findings(
        str(work), str(work), None, None,
        entries=[{"path": "app.py", "status": "A"}], tele_sink=sink)
    assert findings == [] and warnings == []
    assert sink and sink[0]["name"] == "dsl-check", sink
    assert sink[0]["tests_selected"] == 1, sink
    assert sink[0]["selector_attempts"] == 1, sink
