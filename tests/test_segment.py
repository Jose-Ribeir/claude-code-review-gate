"""Part S (0.11.0), the segmenter: units with content identity, pairing, coverage.

Pure tests over scripts/ocr_segment.py -- no git, no gate, no reviewer.
"""
import difflib
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import ocr_segment as S  # noqa: E402


def _func(i, n=20):
    body = "".join(f"    y{j} = x + {j}\n" for j in range(n))
    return f"def f{i}(x):\n{body}    return y0 + {i}\n"


def _module(n_funcs=10, n=20):
    return "import os\n\n\n" + "\n\n".join(_func(i, n) for i in range(n_funcs)) + "\n"


def _changed_sets(base, tip):
    sm = difflib.SequenceMatcher(None, S.split_lines(base), S.split_lines(tip), autojunk=False)
    tc, bc = set(), set()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            bc.update(range(i1 + 1, i2 + 1))
            tc.update(range(j1 + 1, j2 + 1))
    return tc, bc


def _plan(path, base, tip, **kw):
    b, t = S.segment(path, base, **kw), S.segment(path, tip, **kw)
    pairing = S.pair_units(b["units"], t["units"], b["lines"], t["lines"])
    changes = S.changes_of(pairing, b["units"], t["units"])
    tc, bc = _changed_sets(base, tip)
    gaps = S.check_coverage(b["units"], t["units"], tc, bc, len(b["lines"]), len(t["lines"]),
                            b["lines"], t["lines"])
    return b, t, pairing, changes, gaps


def _names(changes):
    return sorted((c["kind"], (c["tip"] or c["base"])["qualname"]) for c in changes)


# --- units tile the file, with a stable identity ------------------------------------------

def test_python_units_are_the_functions_and_the_regions_between_them():
    seg = S.segment("m.py", _module(3))
    assert seg["method"] == "ast"
    kinds = [(u["kind"], u["qualname"]) for u in seg["units"]]
    assert kinds == [("region", ""), ("function", "f0"), ("function", "f1"), ("function", "f2")]
    # no unit overlaps another, and every non-blank line is in one
    covered = set()
    for u in seg["units"]:
        assert not covered & set(range(u["start"], u["end"] + 1))
        covered |= set(range(u["start"], u["end"] + 1))
    assert {i for i, ln in enumerate(seg["lines"], 1) if ln.strip()} <= covered


def test_decorators_and_leading_comments_belong_to_the_unit_after_them():
    src = "x = 1\n\n# explains f\n@decorator\n@other(1)\ndef f():\n    return 1\n"
    units = S.segment("m.py", src)["units"]
    f = next(u for u in units if u["qualname"] == "f")
    assert (f["start"], f["end"]) == (3, 7)


def test_crlf_and_lf_give_identical_hashes_and_trailing_whitespace_is_ignored():
    lf = _module(4)
    crlf = lf.replace("\n", "\r\n")
    spaced = lf.replace("\n", "  \n")
    a = [u["hash"] for u in S.segment("m.py", lf)["units"]]
    # the gate reads blobs with universal newlines; the segmenter must not care either way
    b = [u["hash"] for u in S.segment("m.py", crlf)["units"]]
    c = [u["hash"] for u in S.segment("m.py", spaced)["units"]]
    assert len(a) == len(b) == len(c) > 1 and a == b == c
    assert len(a[0]) == 64                      # blake2b, the full 256 bits


def test_a_syntax_error_falls_back_to_content_defined_regions():
    seg = S.segment("m.py", "def broken(:\n" + "x = 1\n" * 100)
    assert seg["method"] == "cut" and seg["units"]
    assert all(u["kind"] == "region" for u in seg["units"])


def test_an_oversized_python_class_is_split_into_methods_with_the_class_as_parent():
    methods = "\n".join(f"    def m{i}(self):\n" + "".join(f"        a{j} = {j}\n" for j in range(10))
                        for i in range(30))
    src = f"class Big:\n    attr = 1\n\n{methods}\n\ndef after():\n    return 1\n"
    units = S.segment("m.py", src, class_split=100)["units"]
    ms = [u for u in units if u["kind"] == "method"]
    assert len(ms) == 30 and all(u["parent"] == "Big" for u in ms)
    assert ms[0]["qualname"] == "Big.m0"
    # a small class stays one unit
    small = S.segment("m.py", "class C:\n    def m(self):\n        return 1\n")["units"]
    assert [u["kind"] for u in small] == ["class"] and "m" in small[0]["provides"]


def test_brace_language_units_are_outermost_definitions():
    src = ("import x from 'y';\n\nexport function alpha(a) {\n  return a;\n}\n\n"
           "class Box {\n  open() {\n    return 1;\n  }\n  close() {\n    return 2;\n  }\n}\n\n"
           "const beta = (b) => {\n  return b;\n};\n")
    seg = S.segment("m.ts", src)
    assert seg["method"] == "brace"
    named = [(u["kind"], u["name"]) for u in seg["units"] if u["kind"] != "region"]
    assert named == [("function", "alpha"), ("class", "Box"), ("function", "beta")]
    box = next(u for u in seg["units"] if u["name"] == "Box")
    assert {"Box", "open", "close"} <= set(box["provides"])   # a big class is ONE unit
    assert all(u["ident"] == "heur" for u in seg["units"] if u["kind"] != "region")


def test_markdown_units_are_heading_sections_and_fences_do_not_start_sections():
    src = "intro\n\n# A\ntext\n```\n# not a heading\n```\n\n## B\nmore\n"
    seg = S.segment("d.md", src)
    assert [(u["kind"], u["qualname"]) for u in seg["units"] if u["kind"] == "section"] == [
        ("section", "A"), ("section", "B")]


# --- pairing: what is unchanged, what is a change ------------------------------------------

def test_fifty_lines_inserted_into_function_one_change_exactly_one_unit():
    base = _module(10)
    tip = base.replace("def f0(x):\n", "def f0(x):\n" + "".join(f"    z{j} = {j}\n" for j in range(50)))
    b, t, pairing, changes, gaps = _plan("m.py", base, tip)
    assert _names(changes) == [("modified", "f0")] and gaps == []
    # the other nine are untouched, though every one of them moved down by 50 lines
    moved = {t["units"][ti]["qualname"]: t["units"][ti]["start"] - b["units"][bi]["start"]
             for bi, ti in pairing["unchanged"] if t["units"][ti]["kind"] == "function"}
    assert len(moved) == 9 and set(moved.values()) == {50}


def test_a_2000_line_region_file_with_one_insertion_changes_at_most_two_regions():
    rnd = random.Random(7)
    rows = [f"row {i} {rnd.random():.6f}" if i % 7 else "" for i in range(2000)]
    base = "\n".join(rows) + "\n"
    for pos in (1, 400, 999, 1500, 1999):
        tip = "\n".join(rows[:pos] + ["inserted one", "inserted two"] + rows[pos:]) + "\n"
        b, t, pairing, changes, gaps = _plan("data.sql", base, tip)
        assert len(b["units"]) > 10 and gaps == []
        assert 1 <= len(changes) <= 2, (pos, len(changes))
        assert all(u["kind"] == "region" for u in b["units"])


def test_moving_a_function_within_its_parent_changes_no_unit():
    base = _module(6)
    parts = base.split("\n\n\n", 1)[1].split("\n\n")
    moved = parts[:2] + [parts[3], parts[2]] + parts[4:]
    tip = "import os\n\n\n" + "\n\n".join(moved)
    b, t, pairing, changes, gaps = _plan("m.py", base, tip)
    assert changes == [] and gaps == []         # git calls it a change; the coverage check allows it


def test_moving_a_method_into_another_class_is_a_change_and_one_item():
    cls_a = "class A:\n" + "".join(f"    def a{i}(self):\n        return {i}\n\n" for i in range(40))
    cls_b = "class B:\n" + "".join(f"    def b{i}(self):\n        return {i}\n\n" for i in range(40))
    base = cls_a + "\n" + cls_b
    moved = "    def a5(self):\n        return 5\n\n"
    tip = base.replace(moved, "").replace("class B:\n", "class B:\n" + moved.replace("a5", "a5"))
    b, t, pairing, changes, gaps = _plan("m.py", base, tip, class_split=50)
    assert gaps == []
    names = _names(changes)
    # the method is one item (a modified pair A.a5 -> B.a5), not a removal plus an addition
    assert [n for n in names if "a5" in n[1]] == [("modified", "B.a5")], names
    assert not any(k == "removed" for k, _ in names), names


def test_a_heuristic_unit_that_only_changes_position_counts_as_changed():
    def fn(name):
        return f"function {name}(a) {{\n" + "".join(f"  a += {j};\n" for j in range(6)) + "  return a;\n}\n"
    base = "\n".join(fn(n) for n in ("one", "two", "three", "four"))
    tip = "\n".join(fn(n) for n in ("one", "three", "two", "four"))
    b, t, pairing, changes, gaps = _plan("m.js", base, tip)
    assert gaps == [] and changes, "a moved brace-language unit must be reviewed again"
    kinds = {c["kind"] for c in changes}
    assert kinds <= {"added", "removed"}                  # identical text: never a 'modified' pair


def test_a_rename_is_one_pair():
    base = _module(5)
    tip = base.replace("def f2(x):", "def renamed(x):").replace("return y0 + 2", "return y0 + 2  # r")
    b, t, pairing, changes, gaps = _plan("m.py", base, tip)
    assert gaps == [] and [c["kind"] for c in changes] == ["modified"]
    assert changes[0]["base"]["qualname"] == "f2" and changes[0]["tip"]["qualname"] == "renamed"


def test_added_and_removed_units_are_changes_and_a_removal_has_a_position():
    base = _module(4)
    tip = base.replace(_func(1) + "\n", "") + "\n" + _func(9)
    b, t, pairing, changes, gaps = _plan("m.py", base, tip)
    assert gaps == []
    assert ("removed", "f1") in _names(changes) and ("added", "f9") in _names(changes)
    removed = next(c for c in changes if c["kind"] == "removed")
    assert removed["tip"] is None and removed["after"] >= 0


def test_prepending_a_docstring_changes_no_unit():
    base = _module(6)
    tip = '"""' + "\n".join(f"line {i} of a long module docstring" for i in range(50)) + '\n"""\n' + base
    b, t, pairing, changes, gaps = _plan("m.py", base, tip)
    # the new docstring is code of its own (a region); every function is untouched
    assert changes and all((c["tip"] or c["base"])["kind"] == "region" for c in changes)
    assert not any((c["tip"] or c["base"])["kind"] == "function" for c in changes)


def test_duplicate_units_pair_by_count():
    dup = _func(0)
    base = "\n\n".join([dup, dup, dup, _func(1)]) + "\n"
    tip = "\n\n".join([dup, dup, _func(1)]) + "\n"
    b, t, pairing, changes, gaps = _plan("m.py", base, tip)
    assert gaps == [] and [c["kind"] for c in changes] == ["removed"]
    assert len(pairing["unchanged"]) == 3 and len(t["units"]) == 3


# --- coverage ---------------------------------------------------------------------------------

def test_coverage_passes_on_random_edits_and_fails_on_a_lost_unit():
    rnd = random.Random(3)
    base = _module(12, n=15)
    lines = S.split_lines(base)
    for _ in range(30):
        tl = list(lines)
        for _ in range(rnd.randint(1, 5)):
            i = rnd.randrange(len(tl))
            op = rnd.choice(("edit", "ins", "del"))
            if op == "edit":
                tl[i] = tl[i] + "  # edited"
            elif op == "ins":
                tl.insert(i, f"    extra = {rnd.randint(0, 9)}")
            elif len(tl) > 50:
                del tl[i]
        tip = "\n".join(tl) + "\n"
        b, t, pairing, changes, gaps = _plan("m.py", base, tip)
        assert gaps == [], gaps
    # a forced gap: drop a unit the diff touches
    tip = base.replace("y3 = x + 3", "y3 = x + 33")
    b, t, pairing, changes, gaps = _plan("m.py", base, tip)
    assert gaps == []
    tc, bc = _changed_sets(base, tip)
    holed = [u for u in t["units"] if not (u["start"] <= min(tc) <= u["end"])]
    assert S.check_coverage(b["units"], holed, tc, bc, len(b["lines"]), len(t["lines"]))
    bad = [dict(u, start=u["start"] + 1) if i == 1 else u for i, u in enumerate(t["units"])]
    bad[1]["end"] = bad[1]["start"] - 1                  # a unit that does not fit the file
    assert S.check_coverage(b["units"], bad, tc, bc, len(b["lines"]), len(t["lines"]))


def test_changed_lines_are_read_from_the_hunk_headers():
    diff = "@@ -3,2 +3,3 @@ x\n-a\n-b\n+c\n+d\n+e\n@@ -10 +11,0 @@\n-gone\n@@ -20,0 +21 @@\n+new\n"
    tip, base = S.parse_changed_lines(diff)
    assert tip == {3, 4, 5, 21} and base == {3, 4, 10}


# --- the unit diff ----------------------------------------------------------------------------

def test_a_unit_diff_has_absolute_line_numbers_in_both_files():
    base = _module(4)
    tip = base.replace("def f0(x):\n", "def f0(x):\n    # added\n").replace("y5 = x + 5\n", "y5 = x + 55\n", 1)
    b, t, pairing, changes, gaps = _plan("m.py", base, tip)
    (ch,) = changes
    (text,) = S.unit_diff_parts("m.py", "", ch, b["lines"], t["lines"], 500)
    assert text.splitlines()[0].startswith("# unit: f0 (function) lines")
    assert "--- a/m.py" in text and "+++ b/m.py" in text
    f0b, f0t = ch["base"], ch["tip"]
    assert f"@@ -{f0b['start']}," in text and f" +{f0t['start']}," in text
    assert "+    # added" in text and "+    y5 = x + 55" in text and "-    y5 = x + 5" in text


def test_an_added_unit_is_all_plus_and_a_removed_one_all_minus():
    base = _module(2)
    tip = base + "\n" + _func(7, 3)
    b, t, pairing, changes, gaps = _plan("m.py", base, tip)
    (added,) = [c for c in changes if c["kind"] == "added"]
    (text,) = S.unit_diff_parts("m.py", "", added, b["lines"], t["lines"], 500)
    assert "--- /dev/null" in text and "ADDED" in text
    assert text.count("\n+def f7") == 1
    assert not [ln for ln in text.splitlines() if ln.startswith("-") and not ln.startswith("---")]
    b2, t2, pairing2, changes2, _ = _plan("m.py", tip, base)
    (removed,) = [c for c in changes2 if c["kind"] == "removed"]
    (text2,) = S.unit_diff_parts("m.py", "", removed, b2["lines"], t2["lines"], 500)
    assert "+++ /dev/null" in text2 and "REMOVED" in text2 and text2.count("\n-    ") >= 3


def test_a_monolithic_unit_is_split_into_parts_that_hold_every_changed_line_once():
    base = "def big(x):\n" + "".join(f"    v{j} = x + {j}\n" for j in range(1200)) + "    return x\n"
    tip = base.replace("\n", "\n", 1)
    tip = "def big(x):\n" + "".join(f"    v{j} = x - {j}\n" for j in range(1200)) + "    return x\n"
    b, t, pairing, changes, gaps = _plan("m.py", base, tip)
    (ch,) = changes
    parts = S.unit_diff_parts("m.py", "", ch, b["lines"], t["lines"], 300)
    assert len(parts) > 3
    plus = [ln for p in parts for ln in p.splitlines() if ln.startswith("+    v")]
    minus = [ln for p in parts for ln in p.splitlines() if ln.startswith("-    v")]
    assert len(plus) == 1200 and len(set(plus)) == 1200 and len(minus) == 1200
    for p in parts:
        assert len(p.splitlines()) <= 300 + 12
        assert "# part " in p


# --- call edges -------------------------------------------------------------------------------

def test_python_callers_come_from_the_ast_and_are_capped():
    src = ("def target(x):\n    return x\n\n"
           + "".join(f"def caller{i}():\n    return target({i})\n\n" for i in range(12))
           + "def other():\n    return 1\n")
    seg = S.segment("m.py", src)
    calls = S.unit_calls("python", src, seg["lines"], seg["units"])
    ti = next(i for i, u in enumerate(seg["units"]) if u["qualname"] == "target")
    got = S.callers_of("python", seg["units"], calls, [ti])[ti]
    assert len(got) == S.CALLERS_PYTHON == 8
    assert all(seg["units"][h["caller"]]["qualname"].startswith("caller") for h in got)


def test_regex_callers_skip_short_stoplisted_and_twice_defined_names_and_cap_at_four():
    def fn(name, call):
        return f"function {name}(a) {{\n  return {call}(a);\n}}\n\n"
    src = (fn("helper_one", "x") + fn("get", "x") + fn("dup", "x") + fn("dup", "x")
           + "".join(fn(f"user{i}", "helper_one") for i in range(7))
           + fn("a_get", "get") + fn("caller_dup", "dup") + fn("ab", "x") + fn("w", "ab"))
    seg = S.segment("m.js", src)
    calls = S.unit_calls("js", src, seg["lines"], seg["units"])
    idx = {u["name"]: i for i, u in enumerate(seg["units"]) if u["kind"] == "function"}
    got = S.callers_of("js", seg["units"], calls,
                       [idx["helper_one"], idx["get"], idx["dup"], idx["ab"]])
    assert len(got[idx["helper_one"]]) == S.CALLERS_REGEX == 4
    assert got[idx["get"]] == []        # stoplisted
    assert got[idx["dup"]] == []        # defined twice
    assert got[idx["ab"]] == []         # under four characters


def test_preamble_and_signature_index():
    seg = S.segment("m.py", _module(3))
    assert S.preamble_lines(seg["units"], seg["lines"]) == ["import os"]
    idx = S.signature_index(seg["units"], {2})
    assert idx[0].endswith("def f0(x):") and idx[1].endswith("[CHANGED]") and len(idx) == 3
