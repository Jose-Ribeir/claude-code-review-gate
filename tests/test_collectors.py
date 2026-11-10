"""Tests for scripts/ocr_collectors.py — deterministic evidence collectors (t05).

The collectors map each validated context-spotter demand kind to evidence in a
bounded bundle delivered to the reviewer beside impact_sites (spec: "Evidence
bundle contract"). Hard requirements covered here:

* every demand resolves to evidence or an explicit ``unresolved`` entry the
  reviewer may cite as medium;
* unavailable sources (index not built, a raising collector) degrade to
  ``unavailable`` entries plus a warning, never an error;
* the bundle is capped at 20k chars, degrading largest-entry-first with
  warnings, never dropping an entry entirely;
* ADR 0001: the bundle shape is validated gate-side before reaching the
  reviewer (``validate_bundle``), and ``collect`` self-validates so a broken
  bundle can never leave the module.

Fixtures are real tiny git repos in tmp_path (symbol_callers searches via
git grep/show) and a real event index under .ocr/index/.
"""
import json
import os
import subprocess
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)

import ocr_collectors as oc  # noqa: E402
import ocr_index  # noqa: E402

EMPTY = {"symbol_callers": [], "event_couples": [], "doc_refs": [],
         "conventions": [], "prose_pointers": [], "risk": {"score": 0, "reasons": []}}


def _demands(**overrides):
    d = dict(EMPTY)
    d.update(overrides)
    return d


def _collect(repo_root, demands, changed_files=(), **kwargs):
    return oc.collect(str(repo_root), demands, list(changed_files), **kwargs)


def _assert_valid_bundle(bundle):
    """ADR 0001: collect() output must always pass the gate-side validator."""
    result = oc.validate_bundle(bundle)
    assert result == {"ok": True, "errors": []}, result


# ---------------------------------------------------------------------------
# Helpers: tiny real git repos
# ---------------------------------------------------------------------------

def _git_raw(args, cwd):
    result = subprocess.run(
        ["git"] + args, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    return result.stdout, result.returncode


def _init_repo(tmp_path):
    cwd = str(tmp_path)
    for cmd in (["init"], ["config", "user.email", "test@test.com"],
                ["config", "user.name", "Tester"], ["config", "commit.gpgsign", "false"]):
        _git_raw(cmd, cwd)
    return cwd


def _commit(cwd, files, message="test commit"):
    for relpath, content in files.items():
        full = os.path.join(cwd, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
    _git_raw(["add", "-A"], cwd)
    out, rc = _git_raw(["commit", "-m", message], cwd)
    assert rc == 0, out


# ===========================================================================
# Bundle skeleton, empty input, validation (ADR 0001)
# ===========================================================================

class TestBundleSkeleton:
    def test_empty_demands_yield_empty_bundle(self, tmp_path):
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"a.py": "x = 1\n"})
        result = _collect(tmp_path, _demands(), ["a.py"])
        bundle = result["bundle"]
        assert bundle["version"] == 1
        assert bundle["entries"] == []
        assert bundle["truncated"] is False
        assert isinstance(bundle["total_chars"], int)
        assert result["warnings"] == []
        _assert_valid_bundle(bundle)

    def test_none_demands_yield_empty_bundle(self, tmp_path):
        """T0 fallback (no spotter demands) must not crash the collectors."""
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"a.py": "x = 1\n"})
        result = _collect(tmp_path, None, ["a.py"])
        assert result["bundle"]["entries"] == []
        _assert_valid_bundle(result["bundle"])

    def test_demand_keys_missing_are_treated_as_empty(self, tmp_path):
        result = _collect(tmp_path, {"risk": {"score": 0, "reasons": []}}, [])
        assert result["bundle"]["entries"] == []
        _assert_valid_bundle(result["bundle"])


# ===========================================================================
# conventions / doc_refs -> t03 resolver (in-process import, not subprocess)
# ===========================================================================

class TestConventions:
    def test_ancestor_doc_resolves_as_evidence(self, tmp_path):
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"AGENTS.md": "# House rules\nNever mutate globals.\n",
                      "src/app.py": "x = 1\n"})
        demands = _demands(conventions=["global-state discipline"])
        result = _collect(tmp_path, demands, ["src/app.py"])
        entries = result["bundle"]["entries"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["kind"] == "conventions"
        assert entry["status"] == "ok"
        assert entry["demands"] == ["global-state discipline"]
        assert "Never mutate globals." in entry["evidence"]["files"]["src/app.py"]
        _assert_valid_bundle(result["bundle"])

    def test_no_applicable_doc_is_unresolved(self, tmp_path):
        """Every demand resolves to evidence or an explicit unresolved entry."""
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"src/app.py": "x = 1\n"})
        demands = _demands(conventions=["global-state discipline"])
        result = _collect(tmp_path, demands, ["src/app.py"])
        entry = result["bundle"]["entries"][0]
        assert entry["status"] == "unresolved"
        assert entry["demands"] == ["global-state discipline"]
        assert "no convention" in entry["note"]
        _assert_valid_bundle(result["bundle"])

    def test_resolver_warnings_propagate(self, tmp_path):
        """A truncated doc's warning from the t03 resolver is copied through."""
        cwd = _init_repo(tmp_path)
        doc = "\n".join("rule %d" % i for i in range(900))  # over MAX_DOC_LINES=800
        _commit(cwd, {"AGENTS.md": doc + "\n", "src/app.py": "x = 1\n"})
        demands = _demands(conventions=["anything"])
        result = _collect(tmp_path, demands, ["src/app.py"])
        assert result["bundle"]["entries"][0]["status"] == "ok"
        assert any("conventions: doc" in w and "truncated" in w
                   for w in result["warnings"])
        _assert_valid_bundle(result["bundle"])


class TestDocRefs:
    def _repo_with_rule(self, tmp_path):
        cwd = _init_repo(tmp_path)
        _commit(cwd, {
            "docs/STYLE.md": "# Style\nTwo-space indent, always.\n",
            ".ocr/rule.json": json.dumps(
                {"conventions": [{"doc": "docs/STYLE.md", "applyTo": ["**/*"]}]}),
            "src/app.py": "x = 1\n",
        })
        return cwd

    def test_demanded_doc_is_served_with_text(self, tmp_path):
        self._repo_with_rule(tmp_path)
        demands = _demands(doc_refs=["docs/STYLE.md"])
        result = _collect(tmp_path, demands, ["src/app.py"])
        entry = result["bundle"]["entries"][0]
        assert entry["kind"] == "doc_refs"
        assert entry["status"] == "ok"
        assert entry["demands"] == ["docs/STYLE.md"]
        assert "Two-space indent" in entry["evidence"]["files"]["src/app.py"]
        assert entry["evidence"]["doc"] == "docs/STYLE.md"
        _assert_valid_bundle(result["bundle"])

    def test_demanded_doc_by_basename(self, tmp_path):
        """The spotter may name the doc loosely; basename matching serves it."""
        self._repo_with_rule(tmp_path)
        demands = _demands(doc_refs=["STYLE.md"])
        result = _collect(tmp_path, demands, ["src/app.py"])
        assert result["bundle"]["entries"][0]["status"] == "ok"

    def test_unmatched_doc_ref_is_unresolved(self, tmp_path):
        self._repo_with_rule(tmp_path)
        demands = _demands(doc_refs=["docs/NONEXISTENT.md"])
        result = _collect(tmp_path, demands, ["src/app.py"])
        entry = result["bundle"]["entries"][0]
        assert entry["status"] == "unresolved"
        assert "docs/NONEXISTENT.md" in entry["note"]
        _assert_valid_bundle(result["bundle"])

    def test_conventions_and_doc_refs_share_one_resolution(self, tmp_path):
        """Both kinds are served by a single t03 resolver pass per collect()."""
        self._repo_with_rule(tmp_path)
        demands = _demands(conventions=["style"], doc_refs=["docs/STYLE.md"])
        result = _collect(tmp_path, demands, ["src/app.py"])
        kinds = [e["kind"] for e in result["bundle"]["entries"]]
        assert kinds == ["doc_refs", "conventions"]  # DEMAND_KEYS order
        assert all(e["status"] == "ok" for e in result["bundle"]["entries"])
        _assert_valid_bundle(result["bundle"])


# ===========================================================================
# symbol_callers -> ocr_impact.find_references under the context allowlist
# ===========================================================================

_SETTINGS_PY = '''\
def get_settings(path=None):
    """Load settings, defaulting to the repo-local file."""
    return {"path": path}
'''

_APP_PY = '''\
from settings import get_settings


def main():
    return get_settings()
'''

_TEST_APP_PY = '''\
from settings import get_settings


def test_defaults():
    assert get_settings()
'''


class TestSymbolCallers:
    def _repo(self, tmp_path):
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"settings.py": _SETTINGS_PY, "app.py": _APP_PY,
                      "tests/test_app.py": _TEST_APP_PY})
        return cwd

    def test_callers_found_including_spec_files(self, tmp_path):
        """The t01 context filter keeps spec/test files in the caller search."""
        self._repo(tmp_path)
        demands = _demands(symbol_callers=["get_settings"])
        result = _collect(tmp_path, demands, ["settings.py"])
        entry = result["bundle"]["entries"][0]
        assert entry["kind"] == "symbol_callers"
        assert entry["status"] == "ok"
        ev = entry["evidence"]
        assert ev["name"] == "get_settings"
        paths = {s["path"] for s in ev["sites"]}
        assert "app.py" in paths
        assert "tests/test_app.py" in paths  # searchable, though unreviewable
        assert ev["ref_counts"] == {"get_settings": 2}
        assert all("snippet" in s for s in ev["sites"])
        _assert_valid_bundle(result["bundle"])

    def test_context_filter_governs_the_search(self, tmp_path):
        """A caller-supplied context filter (the gate's own t01 filter) is
        honored: filtered files are never searched for references."""
        self._repo(tmp_path)
        demands = _demands(symbol_callers=["get_settings"])
        result = _collect(tmp_path, demands, ["settings.py"],
                          is_context_allowed=lambda p: not p.startswith("tests/"))
        ev = result["bundle"]["entries"][0]["evidence"]
        assert {s["path"] for s in ev["sites"]} == {"app.py"}

    def test_object_demand_with_defined_in(self, tmp_path):
        self._repo(tmp_path)
        demands = _demands(symbol_callers=[{"name": "get_settings",
                                            "defined_in": "settings.py"}])
        result = _collect(tmp_path, demands, ["settings.py"])
        ev = result["bundle"]["entries"][0]["evidence"]
        assert ev["defined_in"] == "settings.py"
        assert ev["sites"], ev
        # The defining file itself is never reported as a caller.
        assert "settings.py" not in {s["path"] for s in ev["sites"]}

    def test_symbol_without_callers_is_unresolved(self, tmp_path):
        self._repo(tmp_path)
        demands = _demands(symbol_callers=["definitely_absent_symbol"])
        result = _collect(tmp_path, demands, ["settings.py"])
        entry = result["bundle"]["entries"][0]
        assert entry["status"] == "unresolved"
        assert "definitely_absent_symbol" in entry["note"]
        _assert_valid_bundle(result["bundle"])

    def test_definition_itself_not_a_caller(self, tmp_path):
        """The only occurrence of the name lives in its definition: unresolved."""
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"lonely.py": "def lonely_fn():\n    return 1\n"})
        demands = _demands(symbol_callers=["lonely_fn"])
        result = _collect(tmp_path, demands, ["lonely.py"])
        assert result["bundle"]["entries"][0]["status"] == "unresolved"


# ===========================================================================
# event_couples -> ocr_index.query_event_couples (unavailable never errors)
# ===========================================================================

_PRODUCER_PY = '''\
import events


def place(order):
    events.emit('order-placed', order)
'''

_CONSUMER_PY = '''\
import events


def arm():
    events.on('order-placed', notify)
'''

_POINTER_PY = '''\
REPORT = "out/weekly-2026-11.md"
OTHER = "docs/runbook.md"
'''


class TestEventCouples:
    def _repo(self, tmp_path, build=True):
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"app/producer.py": _PRODUCER_PY, "app/consumer.py": _CONSUMER_PY})
        if build:
            built = ocr_index.ensure_built(str(tmp_path))
            assert built["built"] is True, built
        return cwd

    def test_couple_evidence(self, tmp_path):
        self._repo(tmp_path)
        demands = _demands(event_couples=["order-placed"])
        result = _collect(tmp_path, demands, ["app/producer.py"])
        entry = result["bundle"]["entries"][0]
        assert entry["kind"] == "event_couples"
        assert entry["status"] == "ok"
        ev = entry["evidence"]
        assert ev["event"] == "order-placed"
        assert any(p["path"] == "app/producer.py" for p in ev["producers"])
        assert any(c["path"] == "app/consumer.py" for c in ev["consumers"])
        assert any(c["producer"]["path"] == "app/producer.py"
                   and c["consumer"]["path"] == "app/consumer.py"
                   for c in ev["couples"])
        _assert_valid_bundle(result["bundle"])

    def test_unknown_event_is_unresolved(self, tmp_path):
        self._repo(tmp_path)
        demands = _demands(event_couples=["no-such-event"])
        result = _collect(tmp_path, demands, ["app/producer.py"])
        entry = result["bundle"]["entries"][0]
        assert entry["status"] == "unresolved"
        assert "no-such-event" in entry["note"]
        _assert_valid_bundle(result["bundle"])

    def test_unavailable_index_is_not_an_error(self, tmp_path):
        """t06's index may not exist: explicit unavailable entries + warning."""
        self._repo(tmp_path, build=False)
        demands = _demands(event_couples=["order-placed", "order-shipped"])
        result = _collect(tmp_path, demands, ["app/producer.py"])
        entries = result["bundle"]["entries"]
        assert [e["status"] for e in entries] == ["unavailable", "unavailable"]
        assert len(result["warnings"]) == 1
        assert "index" in result["warnings"][0]
        _assert_valid_bundle(result["bundle"])


# ===========================================================================
# prose_pointers -> ocr_index pointer utterances + deferred resolution (t09)
# ===========================================================================

class TestProsePointers:
    def _repo(self, tmp_path, build=True):
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"app/reports.py": _POINTER_PY})
        if build:
            built = ocr_index.ensure_built(str(tmp_path))
            assert built["built"] is True, built
        return cwd

    def test_pointer_utterance_with_deferred_resolution(self, tmp_path):
        self._repo(tmp_path)
        demands = _demands(prose_pointers=["weekly-2026-11.md"])
        result = _collect(tmp_path, demands, ["app/reports.py"])
        entry = result["bundle"]["entries"][0]
        assert entry["status"] == "ok"
        pointers = entry["evidence"]["pointers"]
        assert [p["target"] for p in pointers] == ["out/weekly-2026-11.md"]
        # t09 stub: resolution is an explicit placeholder, never fabricated.
        assert pointers[0]["resolution"]["status"] == "deferred"
        _assert_valid_bundle(result["bundle"])

    def test_demand_without_extension_still_matches(self, tmp_path):
        self._repo(tmp_path)
        demands = _demands(prose_pointers=["weekly-2026-11"])
        result = _collect(tmp_path, demands, ["app/reports.py"])
        assert result["bundle"]["entries"][0]["status"] == "ok"

    def test_unmatched_pointer_is_unresolved(self, tmp_path):
        self._repo(tmp_path)
        demands = _demands(prose_pointers=["quarterly-2030-01.md"])
        result = _collect(tmp_path, demands, ["app/reports.py"])
        entry = result["bundle"]["entries"][0]
        assert entry["status"] == "unresolved"
        assert "quarterly-2030-01.md" in entry["note"]
        _assert_valid_bundle(result["bundle"])

    def test_unavailable_index_is_not_an_error(self, tmp_path):
        self._repo(tmp_path, build=False)
        demands = _demands(prose_pointers=["weekly-2026-11.md"])
        result = _collect(tmp_path, demands, ["app/reports.py"])
        assert result["bundle"]["entries"][0]["status"] == "unavailable"
        assert result["warnings"]
        _assert_valid_bundle(result["bundle"])

    def test_pointer_resolver_seam_for_t09(self, tmp_path):
        """t09 lands real resolution by passing pointer_resolver; collect's API
        and the rest of the bundle do not change."""
        self._repo(tmp_path)

        def resolver(target):
            return {"status": "resolved", "via": "registry", "path": target}

        demands = _demands(prose_pointers=["weekly-2026-11.md"])
        result = _collect(tmp_path, demands, ["app/reports.py"],
                          pointer_resolver=resolver)
        pointers = result["bundle"]["entries"][0]["evidence"]["pointers"]
        assert pointers[0]["resolution"] == {
            "status": "resolved", "via": "registry", "path": "out/weekly-2026-11.md"}
        _assert_valid_bundle(result["bundle"])


# ===========================================================================
# Fail-soft: a raising collector degrades to unavailable, never crashes
# ===========================================================================

class TestFailSoft:
    def test_raising_reference_search_degrades_with_warning(self, tmp_path, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("git gone")
        monkeypatch.setattr(oc.ocr_impact, "find_references", _boom)
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"settings.py": _SETTINGS_PY, "app.py": _APP_PY})
        demands = _demands(symbol_callers=["get_settings","other"],
                           conventions=["style"])
        result = _collect(tmp_path, demands, ["settings.py"])
        entries = result["bundle"]["entries"]
        sc = [e for e in entries if e["kind"] == "symbol_callers"]
        assert [e["status"] for e in sc] == ["unavailable", "unavailable"]
        assert any("symbol_callers collector failed" in w for w in result["warnings"])
        # Other kinds still collected -- one failure never poisons the bundle.
        assert [e["kind"] for e in entries if e["kind"] == "conventions"]
        _assert_valid_bundle(result["bundle"])

    def test_raising_index_query_degrades_with_warning(self, tmp_path, monkeypatch):
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"app/producer.py": _PRODUCER_PY})
        assert ocr_index.ensure_built(str(tmp_path))["built"]

        def _boom(*args, **kwargs):
            raise RuntimeError("disk on fire")
        monkeypatch.setattr(oc.ocr_index, "query_event_couples", _boom)
        demands = _demands(event_couples=["order-placed"])
        result = _collect(tmp_path, demands, ["app/producer.py"])
        entry = result["bundle"]["entries"][0]
        assert entry["status"] == "unavailable"
        assert any("event_couples collector failed" in w for w in result["warnings"])
        _assert_valid_bundle(result["bundle"])

    def test_raising_pointer_resolver_falls_back_to_deferred(self, tmp_path, monkeypatch):
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"app/reports.py": _POINTER_PY})
        assert ocr_index.ensure_built(str(tmp_path))["built"]

        def _boom(target):
            raise RuntimeError("forge unreachable")
        demands = _demands(prose_pointers=["weekly-2026-11.md"])
        result = _collect(tmp_path, demands, ["app/reports.py"],
                          pointer_resolver=_boom)
        entry = result["bundle"]["entries"][0]
        assert entry["status"] == "ok"
        assert entry["evidence"]["pointers"][0]["resolution"]["status"] == "deferred"
        assert any("pointer resolver failed" in w for w in result["warnings"])
        _assert_valid_bundle(result["bundle"])

    def test_collect_never_raises_on_garbage_repo(self, tmp_path):
        result = _collect(tmp_path, _demands(symbol_callers=["get_settings"],
                                             doc_refs=["docs/X.md"]), ["gone.py"])
        _assert_valid_bundle(result["bundle"])


# ===========================================================================
# validate_bundle -- ADR 0001 fail-closed shape gate
# ===========================================================================

_OK_ENTRY = {"id": "conventions[0]", "kind": "conventions",
             "demands": ["style"], "status": "ok",
             "evidence": {"files": {"a.py": "text"}}, "note": None}


def _valid_bundle():
    return {"version": 1, "entries": [_OK_ENTRY], "truncated": False,
            "total_chars": 100}


class TestValidateBundle:
    def test_valid_bundle(self):
        assert oc.validate_bundle(_valid_bundle()) == {"ok": True, "errors": []}

    def test_valid_unresolved_and_unavailable(self):
        bundle = _valid_bundle()
        bundle["entries"] = [
            dict(_OK_ENTRY, status="unresolved", evidence=None, note="n"),
            dict(_OK_ENTRY, id="event_couples[0]", kind="event_couples",
                 status="unavailable", evidence=None, note="idx"),
        ]
        assert oc.validate_bundle(bundle)["ok"]

    def _invalid(self, bundle):
        result = oc.validate_bundle(bundle)
        assert result["ok"] is False and result["errors"], result
        return result["errors"]

    def test_not_an_object(self):
        self._invalid(["not", "a", "bundle"])
        self._invalid(None)

    def test_wrong_version(self):
        self._invalid(dict(_valid_bundle(), version=99))
        self._invalid(dict(_valid_bundle(), version="1"))

    def test_entries_not_a_list(self):
        self._invalid(dict(_valid_bundle(), entries={}))

    def test_missing_id(self):
        entry = {k: v for k, v in _OK_ENTRY.items() if k != "id"}
        self._invalid(dict(_valid_bundle(), entries=[entry]))

    def test_unknown_kind(self):
        self._invalid(dict(_valid_bundle(), entries=[dict(_OK_ENTRY, kind="findings")]))

    def test_bad_status(self):
        self._invalid(dict(_valid_bundle(), entries=[dict(_OK_ENTRY, status="maybe")]))

    def test_ok_without_evidence(self):
        self._invalid(dict(_valid_bundle(), entries=[dict(_OK_ENTRY, evidence=None)]))

    def test_evidence_not_an_object(self):
        self._invalid(dict(_valid_bundle(), entries=[dict(_OK_ENTRY, evidence=[1])]))

    def test_empty_demands(self):
        self._invalid(dict(_valid_bundle(), entries=[dict(_OK_ENTRY, demands=[])]))

    def test_bad_note_and_flags(self):
        self._invalid(dict(_valid_bundle(), entries=[dict(_OK_ENTRY, note=1)]))
        self._invalid(dict(_valid_bundle(), truncated="yes"))
        self._invalid(dict(_valid_bundle(), total_chars="100"))


# ===========================================================================
# Bundle budget: 20k chars, largest-entry-first, entries never omitted
# ===========================================================================

class TestBundleCap:
    def _big_doc_repo(self, tmp_path, docs):
        cwd = _init_repo(tmp_path)
        files = {"src/app.py": "x = 1\n",
                 ".ocr/rule.json": json.dumps({"conventions": [
                     {"doc": path, "applyTo": ["**/*"]} for path in docs]})}
        files.update(docs)
        _commit(cwd, files)
        return cwd

    def test_oversize_bundle_is_truncated_largest_first(self, tmp_path):
        # ~25k chars of convention text: over the 20k bundle cap.
        big = ("Convention text, verbose and long-winded. " * 600)
        self._big_doc_repo(tmp_path, {"docs/BIG.md": big})
        demands = _demands(conventions=["style"])
        result = _collect(tmp_path, demands, ["src/app.py"])
        bundle = result["bundle"]
        assert bundle["truncated"] is True
        assert bundle["total_chars"] <= oc.MAX_BUNDLE_CHARS
        entry = bundle["entries"][0]
        # The entry survives degradation -- never omitted, marked truncated.
        assert entry["kind"] == "conventions"
        assert entry["status"] == "ok"
        assert entry.get("truncated") is True
        text = entry["evidence"]["files"]["src/app.py"]
        assert "cut" in text or len(text) < len(big)
        assert any("largest-entry-first" in w for w in result["warnings"])
        _assert_valid_bundle(bundle)

    def test_larger_entry_degrades_before_smaller(self, tmp_path):
        big = "B" * 23000
        small = "S" * 900
        self._big_doc_repo(tmp_path, {"docs/BIG.md": big, "docs/SMALL.md": small})
        demands = _demands(doc_refs=["docs/BIG.md", "docs/SMALL.md"])
        result = _collect(tmp_path, demands, ["src/app.py"])
        bundle = result["bundle"]
        assert bundle["total_chars"] <= oc.MAX_BUNDLE_CHARS
        assert {e["status"] for e in bundle["entries"]} == {"ok"}
        assert len(bundle["entries"]) == 2  # never dropped, only cut
        big_entry = next(e for e in bundle["entries"] if e["evidence"]["doc"] == "docs/BIG.md")
        small_entry = next(e for e in bundle["entries"]
                           if e["evidence"]["doc"] == "docs/SMALL.md")
        assert big_entry.get("truncated") is True
        assert not small_entry.get("truncated")
        assert "S" * 900 in small_entry["evidence"]["files"]["src/app.py"]
        _assert_valid_bundle(bundle)


# ===========================================================================
# End-to-end: one fixture with evidence + unresolved + truncation warning
# ===========================================================================

# ===========================================================================
# CLI: demands JSON on stdin, bundle on stdout, always exit 0; --validate-bundle
# ===========================================================================

_COLLECTORS = os.path.join(_SCRIPTS, "ocr_collectors.py")


def _run_cli(args, stdin_text):
    proc = subprocess.run(
        [sys.executable, _COLLECTORS] + args,
        input=stdin_text, capture_output=True, text=True)
    return proc


class TestCLI:
    def test_collect_from_stdin_accepts_validator_wrapper(self, tmp_path):
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"settings.py": _SETTINGS_PY, "app.py": _APP_PY})
        wrapper = json.dumps({"demands": _demands(symbol_callers=["get_settings"]),
                              "warnings": []})
        proc = _run_cli(["--repo-root", str(tmp_path), "--changed-file", "settings.py"],
                        wrapper)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        entry = out["bundle"]["entries"][0]
        assert entry["kind"] == "symbol_callers" and entry["status"] == "ok"
        assert oc.validate_bundle(out["bundle"])["ok"]

    def test_collect_from_stdin_accepts_bare_demands(self, tmp_path):
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"a.py": "x = 1\n"})
        proc = _run_cli(["--repo-root", str(tmp_path), "--changed-file", "a.py"],
                        json.dumps(_demands()))
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)["bundle"]["entries"] == []

    def test_garbage_stdin_still_exits_zero_with_warning(self, tmp_path):
        proc = _run_cli(["--repo-root", str(tmp_path)], "not json at all")
        assert proc.returncode == 0
        out = json.loads(proc.stdout)
        assert out["bundle"]["entries"] == []
        assert out["warnings"]

    def test_validate_bundle_mode(self, tmp_path):
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"a.py": "x = 1\n"})
        out = oc.collect(str(tmp_path), _demands(), ["a.py"])
        proc = _run_cli(["--validate-bundle"], json.dumps(out["bundle"]))
        assert proc.returncode == 0
        assert json.loads(proc.stdout) == {"ok": True, "errors": []}
        bad = _run_cli(["--validate-bundle"], json.dumps({"version": 99}))
        assert bad.returncode == 1
        assert json.loads(bad.stdout)["ok"] is False


class TestEndToEnd:
    def test_bundle_carries_all_contract_shapes(self, tmp_path):
        big = ("House rule, spelled out at length. " * 650)
        cwd = _init_repo(tmp_path)
        _commit(cwd, {
            "AGENTS.md": big,
            "settings.py": _SETTINGS_PY,
            "app.py": _APP_PY,
            ".ocr/rule.json": json.dumps(
                {"conventions": [{"doc": "docs/STYLE.md", "applyTo": ["**/*"]}]}),
            "docs/STYLE.md": "# Style\nTwo-space indent, always.\n",
        })
        demands = _demands(
            symbol_callers=["get_settings"],
            doc_refs=["docs/STYLE.md", "docs/MISSING.md"],
            conventions=["house style"],
        )
        result = _collect(tmp_path, demands, ["settings.py"])
        bundle = result["bundle"]
        by_kind = {}
        for e in bundle["entries"]:
            by_kind.setdefault(e["kind"], []).append(e)
        # Evidence: per-symbol caller bundle with snippets.
        sc = by_kind["symbol_callers"][0]
        assert sc["status"] == "ok"
        assert any("get_settings()" in s["snippet"] for s in sc["evidence"]["sites"])
        # Evidence: demanded doc resolved through the t03 resolver.
        assert by_kind["doc_refs"][0]["status"] == "ok"
        # Explicit unresolved entry, citable as medium by the reviewer.
        missing = by_kind["doc_refs"][1]
        assert missing["status"] == "unresolved"
        assert "docs/MISSING.md" in missing["note"]
        # Truncation: the oversized conventions evidence was cut with a warning.
        conv = by_kind["conventions"][0]
        assert conv["status"] == "ok"
        assert conv.get("truncated") is True
        assert any("truncated" in w for w in result["warnings"])
        assert bundle["total_chars"] <= oc.MAX_BUNDLE_CHARS
        _assert_valid_bundle(bundle)
