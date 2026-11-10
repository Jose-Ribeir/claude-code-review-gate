"""Tests for scripts/ocr_index.py — the event-graph index (t06).

Fixtures are plain directories (the index does not need a git repo; it is a
tree scan governed by a context-allowed filter). Storage under .ocr/index/ is
runtime state and must never be committed.
"""
import importlib.util
import json
import os
import sys


_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)

import ocr_index  # noqa: E402

_spec = importlib.util.spec_from_file_location("review_gate", os.path.join(_SCRIPTS, "review-gate.py"))
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)

# ---------------------------------------------------------------------------
# Fixture repo
# ---------------------------------------------------------------------------

_PRODUCER_TSX = """import {useLexicalComposerContext} from '@lexical/react/LexicalComposerContext';

const AUDIT_DOC = "out/undo-audit-report.md";
const configUrl = './package.json';

export function ResurrectPlugin() {
  const [editor] = useLexicalComposerContext();
  const resurrect = (entryId: string) => {
    editor.update(() => {
      restoreEntry(entryId);
    }, {tag: 'resurrect-entry'});
  };
  return resurrect;
}
"""

_FILTER_TS = """import type {LexicalEditor} from 'lexical';

export function registerUndoFilter(editor: LexicalEditor): () => void {
  return editor.registerUpdateListener(({tags}) => {
    if (tags.has('resurrect-entry')) {
      return;
    }
    pruneUndoEntries();
  });
}
"""

_REPORT_BUILDER_PY = '''"""Builds the weekly report."""
import events


def build_report(month):
    """Render the report and announce it."""
    path = render(month)
    events.emit('report-ready', path)
    return path


def render(month):
    return "reports/" + month + ".md"
'''

_MAILER_PY = '''"""Mails the report when it is ready."""
import events
from report_builder import build_report


def arm():
    events.on('report-ready', send_copy)


def send_copy(path):
    message = render_message(build_report("2026-11"), path)
    return message


def render_message(a, b):
    return a + b
'''

_TEST_HELPER_PY = """def helper_fn():
    return 1
"""

_VENDORED_TS = "export function vendoredHelper() { return 1; }\n"
_DEP_JS = "module.exports = { emit: (n) => this.bus.emit('dep-event', n) };\n"


def _write(root, relpath, content):
    full = os.path.join(str(root), relpath)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)


def _fixture_repo(tmp_path):
    root = str(tmp_path)
    _write(root, "src/resurrectPlugin.tsx", _PRODUCER_TSX)
    _write(root, "src/undoFilter.ts", _FILTER_TS)
    _write(root, "app/report_builder.py", _REPORT_BUILDER_PY)
    _write(root, "app/mailer.py", _MAILER_PY)
    _write(root, "tests/test_helper.py", _TEST_HELPER_PY)
    _write(root, "vendor/lib/vendored.ts", _VENDORED_TS)
    _write(root, "node_modules/dep/index.js", _DEP_JS)
    return root


def _couple_pairs(result, event):
    return [
        (c["producer"]["path"], c["consumer"]["path"])
        for c in result["couples"]
        if c["event"] == event
    ]


# ===========================================================================
# Lazy build and storage
# ===========================================================================

class TestEnsureBuilt:
    def test_lazy_build_creates_untracked_index_and_is_idempotent(self, tmp_path):
        root = _fixture_repo(tmp_path)
        first = ocr_index.ensure_built(root)
        assert first["built"] is True
        assert first["indexed"] == 5  # vendor + node_modules excluded
        index_path = os.path.join(root, ".ocr", "index", "index.json")
        assert os.path.isfile(index_path)
        on_disk = json.load(open(index_path, encoding="utf-8"))
        assert on_disk["version"] == ocr_index._INDEX_VERSION
        assert set(on_disk["files"]) == {
            "src/resurrectPlugin.tsx", "src/undoFilter.ts",
            "app/report_builder.py", "app/mailer.py", "tests/test_helper.py",
        }

        second = ocr_index.ensure_built(root)
        assert second["built"] is False
        assert second["indexed"] == 5

    def test_context_allowlist_default(self, tmp_path):
        """Test-family files ARE indexed (context filter); vendored and
        dependency trees are not."""
        root = _fixture_repo(tmp_path)
        ocr_index.ensure_built(root)
        got = ocr_index.query_symbol_refs(root, "helper_fn")
        assert [d["path"] for d in got["defs"]] == ["tests/test_helper.py"]
        assert ocr_index.query_symbol_refs(root, "vendoredHelper")["defs"] == []
        assert ocr_index.query_event_couples(root, ["dep-event"])["events"] == {}

    def test_custom_is_allowed_filter_is_respected(self, tmp_path):
        """A caller-supplied filter replaces the default one wholesale: the
        .tsx producer is excluded, vendor/ — which only the default filter
        knows about — now IS indexed."""
        root = _fixture_repo(tmp_path)
        built = ocr_index.ensure_built(root, is_allowed=lambda p: not p.endswith(".tsx"))
        assert built["indexed"] == 5  # vendored.ts in, resurrectPlugin.tsx out
        got = ocr_index.query_event_couples(root, ["resurrect-entry"])
        assert _couple_pairs(got, "resurrect-entry") == []  # producer filtered out
        assert ocr_index.query_symbol_refs(root, "vendoredHelper")["defs"]

    def test_queries_before_build_return_empty_with_warning(self, tmp_path):
        got = ocr_index.query_pointer_utterances(str(tmp_path))
        assert got["pointers"] == []
        assert any("not built" in w for w in got["warnings"])

    def test_everything_is_json_serializable(self, tmp_path):
        root = _fixture_repo(tmp_path)
        results = [
            ocr_index.ensure_built(root),
            ocr_index.refresh(root, ["src/undoFilter.ts"]),
            ocr_index.query_event_couples(root, ["resurrect-entry"]),
            ocr_index.query_pointer_utterances(root),
            ocr_index.query_symbol_refs(root, "build_report"),
        ]
        for r in results:
            json.dumps(r)  # must not raise


# ===========================================================================
# Edge kind 2: event couplings (the undo/resurrection miss class)
# ===========================================================================

class TestEventCouples:
    def test_lexical_style_tag_producer_is_linked_to_filter_consumer(self, tmp_path):
        root = _fixture_repo(tmp_path)
        ocr_index.ensure_built(root)
        got = ocr_index.query_event_couples(root, ["resurrect-entry"])
        assert ("src/resurrectPlugin.tsx", "src/undoFilter.ts") in _couple_pairs(
            got, "resurrect-entry")
        prods = got["events"]["resurrect-entry"]["producers"]
        cons = got["events"]["resurrect-entry"]["consumers"]
        assert prods[0]["direction"] == "emit"
        assert prods[0]["kind"] == "update-tag"
        assert cons[0]["direction"] == "listen"
        assert cons[0]["kind"] == "tag-filter"
        assert 1 <= prods[0]["line"] <= _PRODUCER_TSX.count("\n") + 1

    def test_python_emit_is_linked_to_on_consumer(self, tmp_path):
        root = _fixture_repo(tmp_path)
        ocr_index.ensure_built(root)
        got = ocr_index.query_event_couples(root, ["report-ready"])
        assert ("app/report_builder.py", "app/mailer.py") in _couple_pairs(
            got, "report-ready")

    def test_unreferenced_names_reported_missing(self, tmp_path):
        root = _fixture_repo(tmp_path)
        ocr_index.ensure_built(root)
        got = ocr_index.query_event_couples(root, ["no-such-event", "resurrect-entry"])
        assert got["missing"] == ["no-such-event"]


# ===========================================================================
# Edge kinds 1 and 3: symbol defs/refs and pointer utterances
# ===========================================================================

class TestSymbolsAndPointers:
    def test_symbol_refs_cross_language_and_def_site_excluded(self, tmp_path):
        root = _fixture_repo(tmp_path)
        ocr_index.ensure_built(root)
        got = ocr_index.query_symbol_refs(root, "build_report")
        assert got["defs"] == [
            {"path": "app/report_builder.py", "line": 5, "kind": "function",
             "qualname": "build_report"}
        ]
        ref_paths = {(r["path"], r["line"]) for r in got["refs"]}
        assert ("app/report_builder.py", 5) not in ref_paths  # the def site itself
        assert ("app/mailer.py", 3) in ref_paths      # import
        assert ("app/mailer.py", 11) in ref_paths     # call

    def test_pointer_utterances_find_report_strings(self, tmp_path):
        root = _fixture_repo(tmp_path)
        ocr_index.ensure_built(root)
        got = ocr_index.query_pointer_utterances(root)
        targets = {(p["path"], p["target"], p["kind"]) for p in got["pointers"]}
        assert ("src/resurrectPlugin.tsx", "out/undo-audit-report.md", "md") in targets
        assert ("app/report_builder.py", "reports/", "md") not in targets
        # Config basenames are not report references: './package.json' is denied.
        assert all("package.json" not in t for _, t, _ in targets)
        # Python string concat pieces do not count as report pointers.
        assert all(not t.endswith("+ month +") for _, t, _ in targets)


# ===========================================================================
# Tree-sitter fallback
# ===========================================================================

class TestTreeSitterFallback:
    def test_regex_fallback_without_tree_sitter_emits_warning(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ocr_index, "_TS", None)
        assert ocr_index.using_tree_sitter() is False
        warnings = ocr_index.capability_warnings()
        assert len(warnings) == 1
        assert "tree-sitter" in warnings[0]

        root = _fixture_repo(tmp_path)
        built = ocr_index.ensure_built(root)
        assert built["tool"] == "regex"
        assert any("tree-sitter" in w for w in built["warnings"])
        # The documented-weaker path still links the undo/resurrection couple.
        got = ocr_index.query_event_couples(root, ["resurrect-entry"])
        assert ("src/resurrectPlugin.tsx", "src/undoFilter.ts") in _couple_pairs(
            got, "resurrect-entry")
        # TS defs are still extracted via regex.
        assert ocr_index.query_symbol_refs(root, "ResurrectPlugin")["defs"]

    def test_capability_warnings_empty_when_tree_sitter_available(self, monkeypatch):
        monkeypatch.setattr(ocr_index, "_TS", {"parser": object()})
        assert ocr_index.using_tree_sitter() is True
        assert ocr_index.capability_warnings() == []

    def test_env_kill_switch_forces_regex(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OCR_INDEX_TREE_SITTER", "0")
        assert ocr_index.using_tree_sitter() is False
        assert any("tree-sitter" in w for w in ocr_index.capability_warnings())


# ===========================================================================
# Incremental refresh
# ===========================================================================

class TestRefresh:
    def test_refresh_touches_only_changed_files(self, tmp_path):
        root = _fixture_repo(tmp_path)
        ocr_index.ensure_built(root)
        before = json.load(open(os.path.join(root, ".ocr", "index", "index.json"),
                                encoding="utf-8"))["files"]
        producer_before = dict(before["src/resurrectPlugin.tsx"])
        mailer_before = dict(before["app/mailer.py"])

        _write(root, "src/undoFilter.ts", _FILTER_TS + """
export function registerTagLogger(editor: LexicalEditor): () => void {
  return editor.registerUpdateListener(({tags}) => {
    if (tags.has('tag-log')) {
      console.log(tags);
    }
  });
}
""")
        got = ocr_index.refresh(root, ["src/undoFilter.ts"])
        assert got["refreshed"] == ["src/undoFilter.ts"]
        assert got["removed"] == []

        after = json.load(open(os.path.join(root, ".ocr", "index", "index.json"),
                               encoding="utf-8"))["files"]
        # Untouched files' extracted records are byte-for-byte the same.
        assert after["src/resurrectPlugin.tsx"] == producer_before
        assert after["app/mailer.py"] == mailer_before
        # ...and the changed file gained its new edge.
        couple = ocr_index.query_event_couples(root, ["tag-log"])
        cons = couple["events"]["tag-log"]["consumers"]
        assert [c["path"] for c in cons] == ["src/undoFilter.ts"]

    def test_refresh_reindexes_a_producers_new_tag(self, tmp_path):
        root = _fixture_repo(tmp_path)
        ocr_index.ensure_built(root)
        _write(root, "src/resurrectPlugin.tsx", _PRODUCER_TSX.replace(
            "{tag: 'resurrect-entry'}", "{tag: 'resurrect-entry-v2'}"))
        got = ocr_index.refresh(root, ["src/resurrectPlugin.tsx"])
        assert got["refreshed"] == ["src/resurrectPlugin.tsx"]
        old = ocr_index.query_event_couples(root, ["resurrect-entry"])
        assert _couple_pairs(old, "resurrect-entry") == []
        new = ocr_index.query_event_couples(root, ["resurrect-entry-v2"])
        assert ("src/resurrectPlugin.tsx", "src/undoFilter.ts") not in _couple_pairs(
            new, "resurrect-entry-v2")
        producers = new["events"]["resurrect-entry-v2"]["producers"]
        assert [p["path"] for p in producers] == ["src/resurrectPlugin.tsx"]

    def test_refresh_removes_deleted_files_and_their_edges(self, tmp_path):
        root = _fixture_repo(tmp_path)
        ocr_index.ensure_built(root)
        os.unlink(os.path.join(root, "app", "mailer.py"))
        got = ocr_index.refresh(root, ["app/mailer.py"])
        assert got["removed"] == ["app/mailer.py"]
        refs = ocr_index.query_symbol_refs(root, "build_report")
        assert all(r["path"] != "app/mailer.py" for r in refs["refs"])
        couples = ocr_index.query_event_couples(root, ["report-ready"])
        assert _couple_pairs(couples, "report-ready") == []

    def test_refresh_builds_lazily_when_index_is_missing(self, tmp_path):
        root = _fixture_repo(tmp_path)
        got = ocr_index.refresh(root, ["src/undoFilter.ts"])
        assert got.get("built") is True
        assert os.path.isfile(os.path.join(root, ".ocr", "index", "index.json"))


# ===========================================================================
# review-gate wiring
# ===========================================================================

class TestReviewGateWiring:
    def test_refresh_index_builds_and_reports_warnings(self, tmp_path, monkeypatch):
        for k in list(os.environ):
            if k.startswith("OCR_"):
                monkeypatch.delenv(k)
        monkeypatch.setattr(ocr_index, "_TS", None)
        root = _fixture_repo(tmp_path)
        warnings = review_gate._refresh_index(root, {"src/resurrectPlugin.tsx"})
        assert isinstance(warnings, list)
        assert any("tree-sitter" in w for w in warnings)
        assert os.path.isfile(os.path.join(root, ".ocr", "index", "index.json"))
        # A second call refreshes instead of rebuilding, and stays quiet.
        warnings2 = review_gate._refresh_index(root, {"src/resurrectPlugin.tsx"})
        assert not any("tree-sitter" in w for w in warnings2)

    def test_refresh_index_disabled_returns_no_warnings(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OCR_INDEX", "0")
        root = _fixture_repo(tmp_path)
        assert review_gate._refresh_index(root, set()) == []
        assert not os.path.exists(os.path.join(root, ".ocr"))

    def test_refresh_index_failure_is_fail_soft(self, tmp_path, monkeypatch):
        for k in list(os.environ):
            if k.startswith("OCR_"):
                monkeypatch.delenv(k)
        root = _fixture_repo(tmp_path)

        def _boom(*args, **kwargs):
            raise RuntimeError("disk exploded")

        monkeypatch.setattr(ocr_index, "ensure_built", _boom)
        warnings = review_gate._refresh_index(root, set())
        assert len(warnings) == 1
        assert "disk exploded" in warnings[0]
