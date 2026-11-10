"""Fixture M1 -- remount/filter-policy coupling (review-gate v2, t12).

The known bug shape (spec problem statement #1): the diff touches only
``web/src/editor/Mount.tsx`` -- React slots swapped, the remount-pinning
``key`` removed -- while the bug's second half lives in the UNTOUCHED
``web/src/history/filterPolicy.tsx``, which swallows undo updates whose tag
set lacks ``history-merge``. A diff-scoped gate cannot link the two; the v2
event index pairs every ``{tag: 'history-merge'}`` producer (Lexical-shaped)
with every ``tags.has('history-merge')`` consumer.

Both sides asserted here:

* OLD (parametrized absence): the pre-v2 tree has no ``ocr_index`` module at
  all (git show of main fails) -- the old gate had no machinery that could
  link an event producer to an untouched filter.
* v2: ``ocr_index.query_event_couples`` links the producer (changed file)
  to the consumer (untouched file), and the collectors' evidence bundle
  carries the couple for the spotter's demand.

External behavior, on a real git repo built per test by
tests/fixtures/miss_categories/builders.py.
"""
import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "fixtures", "miss_categories")
for _p in (_SCRIPTS, _FIXTURES):
    sys.path.insert(0, _p)

import ocr_collectors  # noqa: E402
import ocr_index  # noqa: E402
import builders  # noqa: E402


def _m1(tmp_path):
    return builders.build_m1(tmp_path / "work")


# ---------------------------------------------------------------------------
# OLD side: pre-v2 machinery has nothing that links producer and filter
# ---------------------------------------------------------------------------

class TestOldPathMisses:
    def test_no_event_index_existed_pre_v2(self):
        """git show of the pre-v2 tree: ocr_index.py (and the collectors it
        feeds) did not exist on main. Absence, parametrized."""
        assert builders.load_main_module("ocr_index") is None
        assert builders.load_main_module("ocr_collectors") is None

    def test_diff_scoped_search_never_touches_the_filter(self, tmp_path):
        """Even main's ocr_impact, asked for callers of the changed file's
        symbols under the gate's REAL filters, surfaces nothing about the
        event filter: it lives in an untouched file that calls no changed
        symbol. The old path had no way to pull it in."""
        main_impact = builders.load_main_module("ocr_impact")
        assert main_impact is not None  # ocr_impact predates v2
        info = _m1(tmp_path)
        run = main_impact.git_runner(info["root"])
        old_text, _ = _texts(info)
        got = main_impact.changed_symbols(info["mount"], old_text, M1_TIP_TEXT)
        symbols = got["symbols"]
        assert symbols  # the remount change registers as code changes
        refs = main_impact.find_references(
            run, info["tip"], symbols,
            is_allowed=builders.review_path_filter)
        paths = {s["path"] for s in refs["sites"]}
        assert info["filter"] not in paths  # old machinery: filter invisible


# ---------------------------------------------------------------------------
# v2 side: the event index couples the diff's producer to the untouched
# filter, and the collectors bundle carries it
# ---------------------------------------------------------------------------

def _texts(info):
    """(base text, tip text) of the mounting file, straight from git."""
    base = builders.git(["show", "%s~1:%s" % (info["tip"], info["mount"])],
                        info["root"])
    return base, M1_TIP_TEXT


M1_TIP_TEXT = builders.M1_MOUNT_TIP


class TestV2Catches:
    def test_index_builds_and_finds_both_utterances(self, tmp_path):
        info = _m1(tmp_path)
        built = ocr_index.ensure_built(info["root"])
        assert built["built"] is True or built["indexed"] > 0
        events = ocr_index.query_event_couples(info["root"], [info["event"]])
        evt = events["events"][info["event"]]
        producers = {p["path"] for p in evt["producers"]}
        consumers = {c["path"] for c in evt["consumers"]}
        assert info["mount"] in producers      # {tag: 'history-merge'}
        assert info["filter"] in consumers     # tags.has('history-merge')

    def test_query_event_couples_links_diff_file_to_untouched_file(self, tmp_path):
        info = _m1(tmp_path)
        ocr_index.ensure_built(info["root"])
        result = ocr_index.query_event_couples(info["root"], [info["event"]])
        couples = result["couples"]
        assert couples, result
        assert any(c["producer"]["path"] == info["mount"] and
                   c["consumer"]["path"] == info["filter"] for c in couples), couples
        # The coupling half a diff reviewer can never see: the consumer file
        # is not among this push's changed files.
        assert info["filter"] not in info["changed_files"]

    def test_consumer_is_review_eligible_but_invisible_to_the_diff(self, tmp_path):
        """The filter file would still pass the gate's REVIEW allowlist -- it
        is simply not in the diff. Both filters would let it in, so the
        miss is diff-scoping, not filtering."""
        info = _m1(tmp_path)
        assert builders.review_path_filter(info["filter"])
        assert builders.context_path_filter(info["filter"])

    def test_collectors_bundle_carries_the_couple(self, tmp_path):
        """The spotter demands the coupling; the collectors deliver it as
        evidence the reviewer can cite."""
        info = _m1(tmp_path)
        ocr_index.ensure_built(info["root"])
        result = ocr_collectors.collect(
            info["root"], info["spotter_demands"], info["changed_files"])
        assert result["warnings"] == [], result["warnings"]
        entries = [e for e in result["bundle"]["entries"]
                   if e["kind"] == "event_couples"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["status"] == "ok"
        ev = entry["evidence"]
        assert ev["event"] == info["event"]
        assert any(p["path"] == info["mount"] for p in ev["producers"])
        assert any(c["path"] == info["filter"] for c in ev["consumers"])
        assert any(c["producer"]["path"] == info["mount"] and
                   c["consumer"]["path"] == info["filter"]
                   for c in ev["couples"])
        assert ocr_collectors.validate_bundle(result["bundle"]) == {
            "ok": True, "errors": []}

    def test_wrong_event_name_stays_unresolved(self, tmp_path):
        """Sanity for the fixture itself: a sibling tag no file produces is
        unresolved, not coupled."""
        info = _m1(tmp_path)
        ocr_index.ensure_built(info["root"])
        result = ocr_collectors.collect(
            info["root"], builders.demands(event_couples=["history-undo"]),
            info["changed_files"])
        assert result["bundle"]["entries"][0]["status"] == "unresolved"

    def test_remount_semantics_actually_changed_in_the_fixture(self, tmp_path):
        """Fixture self-check: the diff removes the key and swaps the slots
        (a non-diff on Mount.tsx would make every assertion above vacuous)."""
        info = _m1(tmp_path)
        diff = builders.git(["diff", "%s..%s" % (info["tip"] + "~1", info["tip"]),
                             "--", info["mount"]], info["root"])
        assert '-    <section key="editor-shell">' in diff
        assert "+    <section>" in diff
        # slot swap: the A slot moved (removed above B, added below it)
        assert "-      <SlotA />" in diff and "+      <SlotA />" in diff
