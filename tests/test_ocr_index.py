"""Regression tests for cycle3 findings against scripts/ocr_index.py (t25):

5. Result-cap handling: when the cap fires, the outer per-file loop must
   stop too, and the cap warning appears exactly once (hot identifiers in
   many files used to append the identical warning once per file).
7. query_event_couples: the producer-level cap break used to append NO
   warning at all (only the inner consumer-loop break did).
"""
import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)

import ocr_index  # noqa: E402


def _write_fake_index(repo, files):
    ocr_index._write_index(str(repo), {
        "version": ocr_index._INDEX_VERSION,
        "tool": "regex",
        "files": files,
    })


def _rec(**kw):
    base = {"defs": [], "refs": {}, "events": [], "pointers": []}
    base.update(kw)
    return base


def test_ref_cap_breaks_outer_loop_and_warns_once(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr_index, "_MAX_QUERY_RESULTS", 3)
    _write_fake_index(tmp_path, {
        "a.py": _rec(refs={"hotName": [10, 20, 30]}),
        "b.py": _rec(refs={"hotName": [5]}),
        "c.py": _rec(refs={"hotName": [7]}),
    })
    out = ocr_index.query_symbol_refs(str(tmp_path), "hotName")
    assert len(out["refs"]) == 3
    cap_warnings = [w for w in out["warnings"] if "ref cap" in w]
    assert len(cap_warnings) == 1  # not one per remaining file


def test_pointer_cap_breaks_outer_loop_and_warns_once(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr_index, "_MAX_QUERY_RESULTS", 2)
    _write_fake_index(tmp_path, {
        "a.py": _rec(pointers=[{"target": "out/r1.md", "kind": "md", "line": 1},
                               {"target": "out/r2.md", "kind": "md", "line": 2}]),
        "b.py": _rec(pointers=[{"target": "out/r3.md", "kind": "md", "line": 1}]),
        "c.py": _rec(pointers=[{"target": "out/r4.md", "kind": "md", "line": 1}]),
    })
    out = ocr_index.query_pointer_utterances(str(tmp_path))
    assert len(out["pointers"]) == 2
    cap_warnings = [w for w in out["warnings"] if "pointer cap" in w]
    assert len(cap_warnings) == 1


def test_couple_cap_producer_break_warns_once(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr_index, "_MAX_QUERY_RESULTS", 2)
    # 'undo': 1 producer x 2 consumers = exactly the cap with no inner
    # overflow; 'redo' is cut by the producer-level break, which used to
    # warn nothing at all.
    _write_fake_index(tmp_path, {
        "p1.js": _rec(events=[
            {"name": "undo", "direction": "emit", "kind": "emit-call", "line": 1},
            {"name": "undo", "direction": "listen", "kind": "on-call", "line": 5},
            {"name": "undo", "direction": "listen", "kind": "on-call", "line": 6},
            {"name": "redo", "direction": "emit", "kind": "emit-call", "line": 10},
            {"name": "redo", "direction": "listen", "kind": "on-call", "line": 11},
        ]),
    })
    out = ocr_index.query_event_couples(str(tmp_path), ["undo", "redo"])
    cap_warnings = [w for w in out["warnings"] if "couple cap" in w]
    assert cap_warnings, "producer-level cap break must surface a warning"
    assert len(cap_warnings) == 1
    # the capped name still reports its producers/consumers
    assert out["events"]["redo"]["producers"]
    assert len(out["couples"]) == 2


def test_couple_cap_inner_break_warns_once_for_multiple_names(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr_index, "_MAX_QUERY_RESULTS", 2)
    _write_fake_index(tmp_path, {
        "p1.js": _rec(events=[
            {"name": "undo", "direction": "emit", "kind": "emit-call", "line": 1},
            {"name": "undo", "direction": "listen", "kind": "on-call", "line": 5},
            {"name": "undo", "direction": "listen", "kind": "on-call", "line": 6},
            {"name": "undo", "direction": "listen", "kind": "on-call", "line": 7},
        ]),
    })
    out = ocr_index.query_event_couples(str(tmp_path), ["undo"])
    cap_warnings = [w for w in out["warnings"] if "couple cap" in w]
    assert len(cap_warnings) == 1
    assert len(out["couples"]) == 2
