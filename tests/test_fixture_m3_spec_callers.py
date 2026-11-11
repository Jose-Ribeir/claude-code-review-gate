"""Fixture M3 -- stranded spec callers (review-gate v2, t12).

The known bug shape (spec problem statement #3): an optional parameter is
added to a shared helper, stranding eleven existing callers. Two
mechanisms conspired to hide it:

1. the caller search applied the *review* allowlist, and spec files are
   excluded from review -- the callers were found and thrown away (root
   cause fixed by t01's allowlist split);
2. the helper's ``def`` wraps over several lines; pre-t02 only the first
   line of the header compared, so the change classified as a mere "body"
   change instead of "signature".

Both sides asserted here, against main's actual ``ocr_impact`` loaded by
git show and the gate's own import of the t01 filters:

* OLD: ``find_references(is_allowed=<review filter>)`` finds the production
  caller and ZERO of the 11 spec callers; main's module classifies the
  multi-line signature change as "body" and its per-symbol bundle cap (5)
  would strand callers even when found.
* v2: ``find_references(is_context_allowed=<context filter>)`` collects all
  11 spec callers in all 8 spec files; the change classifies as
  "signature"; the raised caps fit the whole caller set.
"""
import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "fixtures", "miss_categories")
for _p in (_SCRIPTS, _FIXTURES):
    sys.path.insert(0, _p)

import ocr_collectors  # noqa: E402
import ocr_impact  # noqa: E402
import builders  # noqa: E402


def _m3(tmp_path):
    return builders.build_m3(tmp_path / "work")


def _symbols(impact, info):
    """changed_symbols for the helper's base->tip diff."""
    got = impact.changed_symbols(info["helper"], info["helper_base"],
                                 info["helper_tip"])
    assert got["supported"] and got["symbols"], got
    return got["symbols"]


def _spec_call_sites(sites, symbol):
    """Sites that are actual CALLS (`build_report(`) in spec files."""
    return [s for s in sites
            if s["path"].startswith("tests/")
            and (symbol + "(") in s["text"]]


class TestFixtureSelfCheck:
    def test_eleven_callers_across_eight_spec_files(self, tmp_path):
        info = _m3(tmp_path)
        assert len(info["spec_files"]) == 8
        total = 0
        for rel in info["spec_files"]:
            with open(os.path.join(info["root"], rel), encoding="utf-8") as fh:
                total += sum(1 for line in fh if info["symbol"] + "(" in line)
        assert total == info["spec_callers"] == 11
        # Fixture invariant for the cap assertions: import lines do not
        # name the symbol, so name-grep hits == call sites.
        for rel in info["spec_files"] + [info["prod_caller"]]:
            with open(os.path.join(info["root"], rel), encoding="utf-8") as fh:
                for line in fh:
                    if "import" in line:
                        assert info["symbol"] not in line

    def test_spec_files_are_review_excluded_but_context_allowed(self, tmp_path):
        """The t01 allowlist split is exercised: every fixture spec file is
        excluded from review and admissible to the context search."""
        info = _m3(tmp_path)
        for rel in info["spec_files"]:
            assert not builders.review_path_filter(rel), rel
            assert builders.context_path_filter(rel), rel
        assert builders.review_path_filter(info["helper"])

    def test_helper_def_wraps_multiple_lines_and_grew_a_default(self, tmp_path):
        """The t02 shape: a wrapped signature, changed on a continuation
        line only (line 1 of the header is identical base->tip)."""
        info = _m3(tmp_path)
        old_lines = info["helper_base"].splitlines()
        new_lines = info["helper_tip"].splitlines()
        idx = old_lines.index("def build_report(")
        assert old_lines[idx] == new_lines[idx]      # identical first line
        assert old_lines[idx + 3].strip() == "include_totals=True):"
        assert new_lines[idx + 3].strip() == "include_totals=True,"
        assert new_lines[idx + 4].strip() == 'currency="EUR"):'  # new optional param


# ---------------------------------------------------------------------------
# OLD side: review filter + old classification + old caps
# ---------------------------------------------------------------------------

class TestOldPathMisses:
    def test_old_search_with_review_filter_collects_zero_spec_callers(self, tmp_path):
        """main's find_references has only ONE filter. Running the real old
        module with the gate's review allowlist reproduces the miss: the 11
        spec callers are collected and thrown away."""
        main_impact = builders.load_main_module("ocr_impact")
        info = _m3(tmp_path)
        run = main_impact.git_runner(info["root"])
        symbols = _symbols(main_impact, info)
        refs = main_impact.find_references(run, info["tip"], symbols,
                                           is_allowed=builders.review_path_filter)
        assert _spec_call_sites(refs["sites"], info["symbol"]) == []
        # The one caller the old gate saw: the production one.
        assert info["prod_caller"] in {s["path"] for s in refs["sites"]}

    def test_v2_search_with_review_filter_alone_still_misses(self, tmp_path):
        """Back-compat seam: find_references with only the single review
        filter behaves exactly as pre-v2 (the split is the fix)."""
        info = _m3(tmp_path)
        run = ocr_impact.git_runner(info["root"])
        symbols = _symbols(ocr_impact, info)
        refs = ocr_impact.find_references(run, info["tip"], symbols,
                                          is_allowed=builders.review_path_filter)
        assert _spec_call_sites(refs["sites"], info["symbol"]) == []

    def test_old_classification_of_wrapped_signature_is_body(self, tmp_path):
        """t02's second half: pre-fix, changing a wrapped signature on a
        continuation line compared as the identical first line -> "body"."""
        main_impact = builders.load_main_module("ocr_impact")
        info = _m3(tmp_path)
        syms = {s["name"]: s for s in _symbols(main_impact, info)}
        assert syms[info["symbol"]]["change"] == "body"

    def test_old_per_symbol_cap_strands_the_caller_set(self, tmp_path):
        """Even had old machinery seen them: the pre-v2 bundle cap held 5
        sites per symbol -- 11 callers cannot fit; v2's 12 covers it
        (build_bundle's docstring is explicit about the 11-caller case)."""
        main_impact = builders.load_main_module("ocr_impact")
        assert main_impact.build_bundle.__kwdefaults__["per_symbol"] == 5
        assert ocr_impact.build_bundle.__kwdefaults__["per_symbol"] >= 11


# ---------------------------------------------------------------------------
# v2 side: context filter + signature classification + raised caps
# ---------------------------------------------------------------------------

class TestV2Catches:
    def test_context_filter_search_collects_all_eleven(self, tmp_path):
        info = _m3(tmp_path)
        run = ocr_impact.git_runner(info["root"])
        symbols = _symbols(ocr_impact, info)
        refs = ocr_impact.find_references(
            run, info["tip"], symbols,
            is_allowed=builders.review_path_filter,
            is_context_allowed=builders.context_path_filter)
        calls = _spec_call_sites(refs["sites"], info["symbol"])
        assert len(calls) == info["spec_callers"] == 11
        assert {s["path"] for s in calls} == set(info["spec_files"])
        assert info["prod_caller"] in {s["path"] for s in refs["sites"]}

    def test_v2_classification_is_signature(self, tmp_path):
        info = _m3(tmp_path)
        syms = {s["name"]: s for s in _symbols(ocr_impact, info)}
        sym = syms[info["symbol"]]
        assert sym["change"] == "signature"
        assert "currency=\"EUR\"" in sym["new_signature"]
        assert sym["new_signature"] != sym["old_signature"]

    def test_collectors_bundle_surfaces_every_spec_caller(self, tmp_path):
        """The spotter's demand becomes per-symbol evidence: 11 call sites,
        8+1 referencing files, snippets attached."""
        info = _m3(tmp_path)
        result = ocr_collectors.collect(
            info["root"], info["spotter_demands"], info["changed_files"],
            is_context_allowed=builders.context_path_filter)
        entries = [e for e in result["bundle"]["entries"]
                   if e["kind"] == "symbol_callers"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["status"] == "ok"
        ev = entry["evidence"]
        sites = ev["sites"]
        spec_calls = [s for s in sites if s["path"].startswith("tests/")
                      and (info["symbol"] + "(") in (s.get("text") or "")]
        assert len(spec_calls) == 11, sites
        assert {s["path"] for s in spec_calls} == set(info["spec_files"])
        assert ev["ref_counts"][info["symbol"]] == 8 + 1
        assert all(s["snippet"] for s in spec_calls)
        assert ocr_collectors.validate_bundle(result["bundle"]) == {
            "ok": True, "errors": []}

    def test_collectors_with_review_filter_alone_strand_spec_callers(self, tmp_path):
        """Contrast at the collector seam: handed the pre-t01 single review
        filter as its context filter, the same collectors strand all 11
        spec callers -- the context filter wiring is what catches them."""
        info = _m3(tmp_path)
        result = ocr_collectors.collect(
            info["root"], info["spotter_demands"], info["changed_files"],
            is_context_allowed=builders.review_path_filter)
        ev = result["bundle"]["entries"][0]["evidence"]
        assert {s["path"] for s in ev["sites"]
                if s["path"].startswith("tests/")} == set()
        assert info["prod_caller"] in {s["path"] for s in ev["sites"]}
