"""Fixture M2 -- convention-doc violation (review-gate v2, t12).

The known bug shape (spec problem statement #2): ``docs/agents/comments.md``
carries the house rule "a comment fits in 72 characters"; the push adds an
over-long comment to ``src/app.py``. The gate's rubric has no copy of the
doc -- docs are not code -- so the old gate never sees the rule it would
have to enforce. The doc also lives OUTSIDE the changed file's ancestor
chain (docs/agents/ is not above src/), exercising the explicit-mapping
layer with its whole-repo default scope (spec user story 4).

Both sides asserted here:

* OLD (parametrized absence): pre-v2 has no ``ocr_conventions`` module at
  all -- git show of main fails -- and the gate's review filter does not
  even admit the .md as a reviewable file.
* v2: ``ocr_conventions.resolve_conventions`` returns the doc for the
  changed file, and the collectors' evidence bundle includes the matched
  convention; a scoped-out doc and an over-budget doc degrade with the
  documented warnings.
"""
import json
import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "fixtures", "miss_categories")
for _p in (_SCRIPTS, _FIXTURES):
    sys.path.insert(0, _p)

import ocr_collectors  # noqa: E402
import ocr_conventions  # noqa: E402
import builders  # noqa: E402


def _m2(tmp_path):
    return builders.build_m2(tmp_path / "work")


# ---------------------------------------------------------------------------
# Fixture self-checks
# ---------------------------------------------------------------------------

class TestFixtureSelfCheck:
    def test_the_diff_carries_the_violating_comment(self, tmp_path):
        """Without an over-long comment in the diff every assertion here is
        vacuous: verify the fixture's shape (line > 72 chars, added)."""
        info = _m2(tmp_path)
        diff = builders.git(["diff", "%s..%s" % (info["tip"] + "~1", info["tip"]),
                             "--", info["src"]], info["root"])
        added = [l[1:].lstrip() for l in diff.splitlines()
                 if l.startswith("+") and not l.startswith("+++")
                 and l[1:].lstrip().startswith("#")]
        assert added, diff
        assert any(len(line) > info["violating_column"] for line in added)

    def test_doc_is_not_an_ancestor_of_the_changed_file(self, tmp_path):
        """docs/agents/ shared no path prefix with src/: the walk-up ancestor
        layer alone cannot serve this doc (explicit mapping required)."""
        info = _m2(tmp_path)
        assert not os.path.dirname(info["src"]).startswith(
            os.path.dirname(info["doc"]))
        found = ocr_conventions._find_ancestor_docs(info["root"], info["src"])
        assert found == []


# ---------------------------------------------------------------------------
# OLD side: the old gate never sees the doc
# ---------------------------------------------------------------------------

class TestOldPathMisses:
    def test_no_convention_resolver_existed_pre_v2(self):
        """git show of the pre-v2 tree: no machinery resolved convention docs
        for changed files (parametrized absence)."""
        assert builders.load_main_module("ocr_conventions") is None

    def test_markdown_docs_are_not_reviewable_files(self, tmp_path):
        """The old single allowlist never admitted the doc as a review
        target, and the diff contains only src/app.py: the rule text had no
        way into the review."""
        info = _m2(tmp_path)
        assert not builders.review_path_filter(info["doc"])
        assert info["changed_files"] == [info["src"]]


# ---------------------------------------------------------------------------
# v2 side: the resolver returns the doc for the changed file, and the
# collectors bundle carries the matched convention
# ---------------------------------------------------------------------------

class TestV2Catches:
    def test_resolver_returns_the_doc_for_the_changed_file(self, tmp_path):
        info = _m2(tmp_path)
        result = ocr_conventions.resolve_conventions(info["root"],
                                                     info["changed_files"])
        text = result["files"][info["src"]]
        assert "## Convention: docs/agents/comments.md (explicit)" in text
        assert "72 characters" in text
        assert result["warnings"] == []

    def test_default_scope_is_repo_wide(self, tmp_path):
        """The fixture's rule.json has NO applyTo: a doc in a docs folder
        applies to code by default (spec user story 4 -- the directory-
        scoping trap eliminated by construction)."""
        info = _m2(tmp_path)
        with open(os.path.join(info["root"], ".ocr", "rule.json"),
                  encoding="utf-8") as fh:
            rule = json.load(fh)
        assert "applyTo" not in rule["conventions"][0]
        result = ocr_conventions.resolve_conventions(info["root"],
                                                     info["changed_files"])
        assert info["src"] in result["files"]

    def test_scoped_out_doc_is_silently_not_applied(self, tmp_path):
        """Contrast: scoped to docs/**, the same doc must NOT reach src/
        (proves the default-scope assertion above is load-bearing)."""
        info = _m2(tmp_path)
        with open(os.path.join(info["root"], ".ocr", "rule.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"conventions": [{"doc": info["doc"],
                                        "applyTo": ["docs/**"]}]}, fh)
        result = ocr_conventions.resolve_conventions(info["root"],
                                                     info["changed_files"])
        assert result["files"] == {}

    def test_collectors_bundle_includes_the_matched_convention(self, tmp_path):
        """conventions demand -> evidence keyed by changed file, carrying the
        house rule the reviewer must enforce."""
        info = _m2(tmp_path)
        result = ocr_collectors.collect(info["root"], info["spotter_demands"],
                                        info["changed_files"])
        by_kind = {}
        for e in result["bundle"]["entries"]:
            by_kind.setdefault(e["kind"], []).append(e)
        conv = by_kind["conventions"][0]
        assert conv["status"] == "ok"
        files = conv["evidence"]["files"]
        assert "72 characters" in files[info["src"]]
        doc_ref = by_kind["doc_refs"][0]
        assert doc_ref["status"] == "ok"
        assert doc_ref["evidence"]["doc"] == info["doc"]
        block = doc_ref["evidence"]["files"][info["src"]]
        assert "Comment style for agents" in block
        assert ocr_collectors.validate_bundle(result["bundle"]) == {
            "ok": True, "errors": []}

    def test_unmatched_doc_ref_is_an_explicit_unresolved(self, tmp_path):
        """A demanded doc that does not exist becomes an explicit unresolved
        entry (the reviewer's medium), never silence."""
        info = _m2(tmp_path)
        result = ocr_collectors.collect(
            info["root"],
            builders.demands(doc_refs=["docs/agents/typography.md"]),
            info["changed_files"])
        entry = result["bundle"]["entries"][0]
        assert entry["status"] == "unresolved"
        assert "typography.md" in entry["note"]

    def test_over_budget_doc_truncates_with_visible_warning(self, tmp_path):
        """Spec user story 5: diluted protection is told, not discovered --
        the oversized doc is truncated and the warning reaches the bundle
        warnings."""
        info = _m2(tmp_path)
        long_doc = "\n".join("comment rule %d, spelled out" % i
                             for i in range(900)) + "\n"
        builders.commit(info["root"], {info["doc"]: long_doc})
        # The resolver itself marks the dilution in the rendered text...
        resolved = ocr_conventions.resolve_conventions(info["root"],
                                                       info["changed_files"])
        assert "[truncated:" in resolved["files"][info["src"]]
        # ...and the collectors surface the same warning with the bundle.
        result = ocr_collectors.collect(
            info["root"], builders.demands(conventions=["comment-length"]),
            info["changed_files"])
        entry = [e for e in result["bundle"]["entries"]
                 if e["kind"] == "conventions"][0]
        assert entry["status"] == "ok"
        assert any("conventions: doc" in w and "truncated" in w
                   for w in result["warnings"])
