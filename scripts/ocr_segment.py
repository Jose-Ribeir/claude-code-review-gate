#!/usr/bin/env python3
#
# Stable review units for big files (review-gate 0.11.0, "Part S").
#
# A file too big to be reviewed as one diff is cut into UNITS -- functions,
# classes, methods, heading sections, or content-defined regions -- whose
# identity is the hash of their own text. Two versions of a file are then
# compared unit by unit: a unit whose text is on both sides is never reviewed
# again, whatever happened around it (an insertion above it, a move), and only
# the units that really changed are handed to the reviewer.
#
# Pure functions over text: no git, no filesystem, no model. Every input comes
# from an untrusted branch, so nothing here may raise on odd input -- the entry
# points catch and return a "cannot segment" result, and the caller then falls
# back to reviewing the file whole.
#
# Units TILE the file: every non-blank line belongs to exactly one unit (the
# blank lines between units belong to none). That is what lets the gate check,
# fail-closed, that every changed line of a diff lies in a unit it knows about
# (check_coverage).
#
import ast
import bisect
import difflib
import hashlib
import os
import re
from collections import defaultdict, deque

import ocr_impact

# Region sizes of the content-defined cut (lines). A region is never shorter
# than MIN (except a file's tail, which joins the region before it) and never
# much longer than MAX.
REGION_MIN = 24
REGION_MAX = 120
# A cut is a starting line whose hash is the smallest within this many lines.
_CUT_WINDOW = 24
# A class longer than this is split into its methods (Python only; in the other
# languages a class stays one unit).
CLASS_SPLIT_LINES = 150
# More units than this and the file is not segmented (the pairing is quadratic).
MAX_UNITS = 6000

_MARKDOWN_EXTS = (".md", ".markdown")
_LEADING_COMMENT = ("#", "//", "/*", "*", "*/", "@", "///", "--")


def split_lines(text):
    """Lines of `text` as git counts them: split on "\\n" only (str.splitlines
    would also split on form feeds and NEL, shifting every line number after
    them), and no phantom empty last line after a final newline."""
    if not text:
        return []
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    return parts


def norm_line(line):
    """The comparison form of a line: no trailing whitespace, no CR."""
    return line.rstrip()


def text_hash(parent, kind, lines):
    """blake2b, full 256 bits, of the normalised text (CRLF = LF, trailing
    whitespace ignored) under its parent's qualname and kind: a method moved
    into another class has another hash."""
    h = hashlib.blake2b(digest_size=32)
    h.update((parent or "").encode("utf-8", "replace"))
    h.update(b"\0")
    h.update((kind or "").encode("utf-8", "replace"))
    h.update(b"\0")
    for ln in lines:
        h.update(norm_line(ln).encode("utf-8", "replace"))
        h.update(b"\n")
    return h.hexdigest()


def path_hash(path):
    """A hash of the tip path, for ledger keys (the path is part of a unit's
    cache identity: moving a function to another file is a change)."""
    return hashlib.blake2b((path or "").encode("utf-8", "replace"), digest_size=16).hexdigest()


def language(path):
    """The language family used for segmentation and in cache keys."""
    ext = os.path.splitext(path or "")[1].lower()
    if ext in _MARKDOWN_EXTS:
        return "markdown"
    return ocr_impact._EXT_LANG.get(ext) or "text"


def _blank(line):
    return not line.strip()


# --- building units ---------------------------------------------------------------

def _mk(lines, start, end, kind, name="", qualname="", parent="", ident="cut",
        sig="", provides=None):
    return {
        "start": start, "end": end, "kind": kind, "name": name,
        "qualname": qualname or name, "parent": parent, "ident": ident,
        "sig": sig, "provides": list(provides or ([name] if name else [])),
        "hash": text_hash(parent, kind, lines[start - 1:end]),
        "nlines": end - start + 1,
    }


def _line_hash(line):
    return int.from_bytes(hashlib.blake2b(norm_line(line).encode("utf-8", "replace"),
                                          digest_size=4).digest(), "big")


def _regions(lines, a, b, parent=""):
    """Content-defined regions of lines a..b (1-based, inclusive).

    A cut goes BEFORE a line that starts something (it follows a blank line, or
    sits at column 0) and whose hash is the smallest among the starting lines
    within _CUT_WINDOW lines on either side. Whether a line is a cut depends only
    on the lines around it, so an edit moves the cuts within a window of it and
    every other region keeps its text -- and its hash. Two cuts are always more
    than the window apart (the smaller of two close ones wins), which is the
    region minimum; a stretch with no cut for REGION_MAX lines is cut at its
    next starting line regardless."""
    while a <= b and _blank(lines[a - 1]):
        a += 1
    while b >= a and _blank(lines[b - 1]):
        b -= 1
    if a > b:
        return []
    cand = {}
    for i in range(a + 1, b + 1):
        line = lines[i - 1]
        if not _blank(line) and (_blank(lines[i - 2]) or not line[:1].isspace()):
            cand[i] = (_line_hash(line), i)
    pos = sorted(cand)
    cutset = set()
    for i in pos:
        lo = bisect.bisect_left(pos, i - _CUT_WINDOW)
        hi = bisect.bisect_right(pos, i + _CUT_WINDOW)
        if cand[i] == min(cand[x] for x in pos[lo:hi]):
            cutset.add(i)
    bounds, last = [a], a
    for i in range(a + 1, b + 1):
        if i in cutset or (i - last >= REGION_MAX and i in cand) or i - last >= 2 * REGION_MAX:
            bounds.append(i)
            last = i
    spans = [(s0, (bounds[n + 1] - 1) if n + 1 < len(bounds) else b)
             for n, s0 in enumerate(bounds)]
    # a short tail joins the region before it
    if len(spans) > 1 and spans[-1][1] - spans[-1][0] + 1 < REGION_MIN:
        last_span = spans.pop()
        spans[-1] = (spans[-1][0], last_span[1])
    out = []
    for s0, e0 in spans:
        while s0 <= e0 and _blank(lines[s0 - 1]):
            s0 += 1
        while e0 >= s0 and _blank(lines[e0 - 1]):
            e0 -= 1
        if s0 <= e0:
            out.append(_mk(lines, s0, e0, "region", parent=parent, ident="cut",
                           sig=lines[s0 - 1].strip()[:200]))
    return out


def _is_leading(line):
    s = line.strip()
    return bool(s) and s.startswith(_LEADING_COMMENT)


def _tile(lines, spans, lo, hi, gap_parent="", leading=True):
    """The units of lines lo..hi: `spans` (named definitions, as dicts with
    start/end/kind/name/..., sorted, disjoint, inside lo..hi) plus content-defined
    regions for everything between them. A definition takes the comment and
    decorator lines directly above it."""
    out, prev_end = [], lo - 1
    for s in sorted(spans, key=lambda x: x["start"]):
        start, end = s["start"], s["end"]
        while end > start and _blank(lines[end - 1]):
            end -= 1
        if leading:
            while start - 1 > prev_end and start - 1 >= lo and _is_leading(lines[start - 2]):
                start -= 1
        if start <= prev_end:
            continue            # overlapping span: drop it, its lines stay with the one before
        out += _regions(lines, prev_end + 1, start - 1, gap_parent)
        kw = {k: v for k, v in s.items() if k not in ("start", "end", "children", "split")}
        if s.get("split"):
            # an oversized class: its methods are units, the rest regions
            out += _tile(lines, s["children"], start, end, gap_parent=s["qualname"],
                         leading=leading)
        else:
            out.append(_mk(lines, start, end, **kw))
        prev_end = end
    out += _regions(lines, prev_end + 1, hi, gap_parent)
    return out


def _python_units(text, lines, class_split):
    tree = ast.parse(text)

    def top(node):
        s = node.lineno
        for d in getattr(node, "decorator_list", ()):
            s = min(s, d.lineno)
        return s

    def sig(node):
        return lines[node.lineno - 1].strip()[:200] if node.lineno <= len(lines) else ""

    def func(node, parent=""):
        q = f"{parent}.{node.name}" if parent else node.name
        return {"start": top(node), "end": node.end_lineno, "kind": "method" if parent else "function",
                "name": node.name, "qualname": q, "parent": parent, "ident": "ast",
                "sig": sig(node), "provides": [node.name]}

    spans = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spans.append(func(node))
        elif isinstance(node, ast.ClassDef):
            methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            start, end = top(node), node.end_lineno
            if end - start + 1 > class_split and methods:
                spans.append({"start": start, "end": end, "split": True, "qualname": node.name,
                              "children": [func(m, node.name) for m in methods]})
            else:
                spans.append({"start": start, "end": end, "kind": "class", "name": node.name,
                              "qualname": node.name, "parent": "", "ident": "ast",
                              "sig": sig(node),
                              "provides": [node.name] + [m.name for m in methods]})
    return _tile(lines, spans, 1, len(lines))


def _brace_units(text, lines, lang):
    defs = ocr_impact._regex_defs(text, lang)
    defs.sort(key=lambda d: (d["line"], -(d.get("end_line") or d["line"])))
    n = len(lines)
    spans, cur_end = [], 0
    for d in defs:
        s, e = d["line"], min(d.get("end_line") or d["line"], n)
        if s > n or e < s:
            continue
        if s <= cur_end:                       # nested in the unit before: it only provides a name
            if spans and d.get("name"):
                spans[-1]["provides"].append(d["name"])
            continue
        spans.append({"start": s, "end": e, "kind": d.get("kind") or "function",
                      "name": d["name"], "qualname": d.get("qualname") or d["name"],
                      "parent": "", "ident": "heur", "sig": (d.get("sig") or "")[:200],
                      "provides": [d["name"]]})
        cur_end = e
    return _tile(lines, spans, 1, n)


def _markdown_units(lines):
    heads, fence = [], False
    for i, ln in enumerate(lines, 1):
        if ln.lstrip().startswith(("```", "~~~")):
            fence = not fence
        elif not fence and re.match(r"^#{1,6}\s+\S", ln):
            heads.append(i)
    spans = []
    for idx, h in enumerate(heads):
        end = heads[idx + 1] - 1 if idx + 1 < len(heads) else len(lines)
        title = lines[h - 1].lstrip("# ").strip()
        spans.append({"start": h, "end": end, "kind": "section", "name": "", "qualname": title,
                      "parent": "", "ident": "heur", "sig": lines[h - 1].strip()[:200],
                      "provides": []})
    return _tile(lines, spans, 1, len(lines), leading=False)


def segment(path, text, class_split=None):
    """Cut `text` into units: {"lang", "method", "units", "lines"}. Never raises:
    anything that cannot be understood (a Python syntax error, a pathological
    file) is cut into content-defined regions instead, which is always possible."""
    lines = split_lines(text)
    lang = language(path)
    cs = CLASS_SPLIT_LINES if class_split is None else class_split
    units, method = None, "cut"
    try:
        if lang == "python":
            units, method = _python_units(text, lines, cs), "ast"
        elif lang == "markdown":
            units, method = _markdown_units(lines), "markdown"
        elif lang in ("js", "ts", "go", "rust", "shell", "powershell", "jvm", "ruby", "php"):
            units, method = _brace_units(text, lines, lang), "brace"
    except Exception:
        units = None
    if units is None:
        try:
            units, method = _regions(lines, 1, len(lines)), "cut"
        except Exception:
            units = []
    for i, u in enumerate(units):
        u["idx"] = i
    return {"lang": lang, "method": method, "units": units, "lines": lines}


# --- pairing ----------------------------------------------------------------------

def _similar(a_lines, b_lines):
    a = [norm_line(x) for x in a_lines]
    b = [norm_line(x) for x in b_lines]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sm.quick_ratio() if max(len(a), len(b)) > 1500 else sm.ratio()


def pair_units(base_units, tip_units, base_lines=None, tip_lines=None):
    """Compare the units of two versions of a file.

    Returns {"unchanged": [(bi, ti)], "modified": [(bi, ti)], "added": [ti],
    "removed": [bi]} (indices into the two lists).

    Unchanged = the same hash on both sides: first as an order-preserving match
    (an insertion above shifts positions, not order), then -- for units whose
    identity is their definition (Python ast units: the qualname is in the hash)
    -- by multiset, so a function moved within its parent is no change. A
    heuristic unit (brace, markdown, content-cut) whose hash only matches at a
    different place is NOT a free move: it is a removal here and an addition
    there. The rest are paired by qualname, then by position inside the same gap
    between unchanged units; a pair that does not look alike is left a removal
    and an addition.
    """
    bh = [u["hash"] for u in base_units]
    th = [u["hash"] for u in tip_units]
    sm = difflib.SequenceMatcher(None, bh, th, autojunk=False)
    lcs = {}
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            lcs[blk.a + k] = blk.b + k
    matched = dict(lcs)
    left_b = [i for i in range(len(base_units)) if i not in matched]
    taken_t = set(matched.values())
    pool = defaultdict(deque)
    for ti, u in enumerate(tip_units):
        if ti not in taken_t and u["ident"] == "ast":
            pool[u["hash"]].append(ti)
    for bi in list(left_b):
        u = base_units[bi]
        if u["ident"] == "ast" and pool.get(u["hash"]):
            matched[bi] = pool[u["hash"]].popleft()
    unchanged = sorted(matched.items())
    taken_t = set(matched.values())
    left_b = [i for i in range(len(base_units)) if i not in matched]
    left_t = [i for i in range(len(tip_units)) if i not in taken_t]

    modified = []
    paired_b, paired_t = set(), set()

    def pair_by(keyfn):
        """Pair leftover units that share keyfn(unit) -- never two identical
        texts (a heuristic move stays a removal plus an addition)."""
        idx = defaultdict(deque)
        for ti in left_t:
            if ti not in paired_t:
                k = keyfn(tip_units[ti])
                if k is not None:
                    idx[k].append(ti)
        for bi in left_b:
            if bi in paired_b:
                continue
            k = keyfn(base_units[bi])
            dq = idx.get(k) if k is not None else None
            while dq:
                ti = dq.popleft()
                if tip_units[ti]["hash"] != base_units[bi]["hash"]:
                    modified.append((bi, ti))
                    paired_b.add(bi)
                    paired_t.add(ti)
                    break

    # by qualname, then by bare name (a method moved into another class)
    pair_by(lambda u: ("q", u["kind"] == "section", u["qualname"])
            if u["qualname"] and u["kind"] != "region" else None)
    pair_by(lambda u: ("n", u["name"]) if u["name"] and u["kind"] in ("function", "method", "class") else None)
    # by position inside the same gap between order-preserving matches
    lcs_b = sorted(lcs)
    lcs_t = sorted(lcs.values())

    def gap_b(i):
        return bisect.bisect_left(lcs_b, i)

    def gap_t(i):
        return bisect.bisect_left(lcs_t, i)

    rest_b = [i for i in left_b if i not in paired_b]
    rest_t = [i for i in left_t if i not in paired_t]
    groups_b, groups_t = defaultdict(list), defaultdict(list)
    for i in rest_b:
        groups_b[gap_b(i)].append(i)
    for i in rest_t:
        groups_t[gap_t(i)].append(i)
    for g, bis in groups_b.items():
        tis = list(groups_t.get(g, ()))
        for bi in bis:
            for ti in tis:
                if ti in paired_t or tip_units[ti]["hash"] == base_units[bi]["hash"]:
                    continue
                if base_units[bi]["kind"] != tip_units[ti]["kind"] and \
                        "region" in (base_units[bi]["kind"], tip_units[ti]["kind"]):
                    continue
                ok = True
                if base_lines is not None and tip_lines is not None:
                    b, t = base_units[bi], tip_units[ti]
                    ok = _similar(base_lines[b["start"] - 1:b["end"]],
                                  tip_lines[t["start"] - 1:t["end"]]) >= 0.35
                if ok:
                    modified.append((bi, ti))
                    paired_b.add(bi)
                    paired_t.add(ti)
                    break
    modified.sort(key=lambda p: p[1])
    return {
        "unchanged": unchanged,
        "modified": modified,
        "added": [i for i in left_t if i not in paired_t],
        "removed": [i for i in left_b if i not in paired_b],
    }


def changes_of(pairing, base_units, tip_units):
    """The units that need a review, in file order: [{"id", "kind", "base", "tip"}]
    with kind modified | added | removed and base/tip the unit dicts (or None).
    A removed unit is placed where it stood, relative to the tip's units, so a
    deletion item has a position to be reported at."""
    items = []
    for bi, ti in pairing["modified"]:
        items.append(("modified", base_units[bi], tip_units[ti], tip_units[ti]["start"]))
    for ti in pairing["added"]:
        items.append(("added", None, tip_units[ti], tip_units[ti]["start"]))
    # a deleted unit sits just after the tip unit its predecessor was matched to
    tip_of = {bi: ti for bi, ti in list(pairing["unchanged"]) + list(pairing["modified"])}
    for bi in pairing["removed"]:
        pos = 0
        for k in range(bi - 1, -1, -1):
            if k in tip_of:
                pos = tip_units[tip_of[k]]["end"]
                break
        items.append(("removed", base_units[bi], None, pos + 0.5))
    items.sort(key=lambda x: x[3])
    return [{"id": n, "kind": k, "base": b, "tip": t, "after": int(pos)}
            for n, (k, b, t, pos) in enumerate(items)]


# --- diffs and coverage -------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.M)


def parse_changed_lines(diff_text):
    """(tip lines added/changed, base lines deleted/changed), as sets of 1-based
    line numbers, from the hunk headers of a `git diff -U0`."""
    tip, base = set(), set()
    for m in _HUNK_RE.finditer(diff_text or ""):
        a, b = int(m.group(1)), int(m.group(2)) if m.group(2) is not None else 1
        c, d = int(m.group(3)), int(m.group(4)) if m.group(4) is not None else 1
        base.update(range(a, a + b))
        tip.update(range(c, c + d))
    return tip, base


def _locate(units, starts, line):
    i = bisect.bisect_right(starts, line) - 1
    if i >= 0 and units[i]["start"] <= line <= units[i]["end"]:
        return i
    return -1


def check_coverage(base_units, tip_units, tip_changed, base_changed, n_base, n_tip,
                   base_lines=None, tip_lines=None):
    """Fail-closed tripwire: [gap descriptions], empty when sound.

    The units of each side must tile it (sorted, disjoint, inside the file), and
    every changed line git reports (whitespace-only changes excluded by the
    caller's diff options; blank lines dropped here when the lines are given)
    must lie in some unit -- a changed
    unit, or an identical one that moved. A changed line in no unit is code the
    segmentation lost; the caller then reviews the whole file instead."""
    gaps = []
    for side, units, n in (("base", base_units, n_base), ("tip", tip_units, n_tip)):
        prev = 0
        for u in units:
            if not (1 <= u["start"] <= u["end"] <= n) or u["start"] <= prev:
                gaps.append(f"{side} units do not tile the file at lines {u['start']}-{u['end']}")
                break
            prev = u["end"]
    if gaps:
        return gaps
    if tip_lines is not None:                      # a blank line is in no unit, by design
        tip_changed = [ln for ln in tip_changed if 1 <= ln <= len(tip_lines) and tip_lines[ln - 1].strip()]
    if base_lines is not None:
        base_changed = [ln for ln in base_changed if 1 <= ln <= len(base_lines) and base_lines[ln - 1].strip()]
    ts = [u["start"] for u in tip_units]
    bs = [u["start"] for u in base_units]
    lost_t = sorted(ln for ln in tip_changed if _locate(tip_units, ts, ln) < 0)
    lost_b = sorted(ln for ln in base_changed if _locate(base_units, bs, ln) < 0)
    if lost_t:
        gaps.append(f"changed tip line(s) in no unit: {lost_t[:5]}")
    if lost_b:
        gaps.append(f"changed base line(s) in no unit: {lost_b[:5]}")
    return gaps


def max_line_len(lines, a, b):
    return max((len(x) for x in lines[a - 1:b]), default=0)


def _fmt_range(start, count):
    if count == 1:
        return f"{start}"
    if count == 0:
        start -= 1
    return f"{start},{count}"


def _split_opcodes(opcodes, limit):
    """Cut any change opcode longer than `limit` lines (either side) into pieces."""
    out = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal" or max(i2 - i1, j2 - j1) <= limit:
            out.append((tag, i1, i2, j1, j2))
            continue
        pieces = -(-max(i2 - i1, j2 - j1) // limit)
        for p in range(pieces):
            a1 = i1 + (i2 - i1) * p // pieces
            a2 = i1 + (i2 - i1) * (p + 1) // pieces
            b1 = j1 + (j2 - j1) * p // pieces
            b2 = j1 + (j2 - j1) * (p + 1) // pieces
            t = "replace" if (a2 > a1 and b2 > b1) else ("delete" if a2 > a1 else "insert")
            out.append((t, a1, a2, b1, b2))
    return out


def unit_diff_parts(path, old_path, change, base_lines, tip_lines, part_limit, context=3):
    """The unit's base -> tip diff as a list of texts (one per part), with
    ABSOLUTE line numbers in both files. `change` is an item of changes_of().

    A unit is never truncated. One that is too big for `part_limit` diff lines is
    cut into parts at hunk boundaries (a single huge hunk is cut inside); each
    part is a complete diff of its piece, so the parts together hold every
    changed line exactly once."""
    b, t = change["base"], change["tip"]
    bl = [x.rstrip("\r") for x in base_lines[b["start"] - 1:b["end"]]] if b else []
    tl = [x.rstrip("\r") for x in tip_lines[t["start"] - 1:t["end"]]] if t else []
    boff = (b["start"] - 1) if b else 0
    toff = (t["start"] - 1) if t else change.get("after", 0)
    sm = difflib.SequenceMatcher(None, [norm_line(x) for x in bl], [norm_line(x) for x in tl],
                                 autojunk=False)
    opcodes = _split_opcodes(sm.get_opcodes(), max(20, part_limit // 2))
    if not any(o[0] != "equal" for o in opcodes):
        # identical text (e.g. only blank lines inside moved): show it whole as a change
        opcodes = [("replace", 0, len(bl), 0, len(tl))] if (bl or tl) else []
    groups = _group(opcodes, context)

    def render(group):
        first, last = group[0], group[-1]
        bs, be, ts, te = first[1], last[2], first[3], last[4]
        head = f"@@ -{_fmt_range(boff + bs + 1, be - bs)} +{_fmt_range(toff + ts + 1, te - ts)} @@"
        body = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                body += [" " + x for x in bl[i1:i2]]
            else:
                body += ["-" + x for x in bl[i1:i2]] + ["+" + x for x in tl[j1:j2]]
        return [head] + body

    rendered = [render(sub) for g in groups for sub in _chop_group(g, part_limit)]
    parts, cur, size = [], [], 0
    for hunk in rendered:
        if cur and size + len(hunk) > part_limit:
            parts.append(cur)
            cur, size = [], 0
        cur += hunk
        size += len(hunk)
    if cur:
        parts.append(cur)
    old = old_path or path
    a_side = "/dev/null" if not b else f"a/{old}"
    b_side = "/dev/null" if not t else f"b/{path}"
    where = (f"lines {t['start']}-{t['end']}" if t else f"was lines {b['start']}-{b['end']}")
    label = (t or b).get("qualname") or (t or b).get("sig") or (t or b)["kind"]
    texts = []
    for n, body in enumerate(parts, 1):
        note = [f"# unit: {label} ({(t or b)['kind']}) {where} of {path}"
                + ("; REMOVED in this change" if not t else "")
                + ("; ADDED in this change" if not b else "")]
        if len(parts) > 1:
            note.append(f"# part {n} of {len(parts)} of this unit's diff; the other parts are "
                        "reviewed alongside, in this or another chunk")
        texts.append("\n".join(note + [f"diff --git a/{old} b/{path}", f"--- {a_side}",
                                       f"+++ {b_side}"] + body))
    return texts


def _chop_group(group, limit):
    """A hunk longer than `limit` rendered lines is cut between its opcodes."""
    out, cur, size = [], [], 0
    for op in group:
        n = (op[2] - op[1]) + (0 if op[0] == "equal" else op[4] - op[3])
        if cur and size + n > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(op)
        size += n
    if cur:
        out.append(cur)
    return out


def _group(opcodes, n):
    """difflib's grouped opcodes over an opcode list (so split opcodes are kept)."""
    codes = list(opcodes)
    if not codes:
        return []
    if codes[0][0] == "equal":
        tag, i1, i2, j1, j2 = codes[0]
        codes[0] = (tag, max(i1, i2 - n), i2, max(j1, j2 - n), j2)
    if codes[-1][0] == "equal":
        tag, i1, i2, j1, j2 = codes[-1]
        codes[-1] = (tag, i1, min(i2, i1 + n), j1, min(j2, j1 + n))
    nn = n + n
    groups, group = [], []
    for tag, i1, i2, j1, j2 in codes:
        if tag == "equal" and i2 - i1 > nn:
            group.append((tag, i1, min(i2, i1 + n), j1, min(j2, j1 + n)))
            groups.append(group)
            group = []
            i1, j1 = max(i1, i2 - n), max(j1, j2 - n)
        group.append((tag, i1, i2, j1, j2))
    if group and not (len(group) == 1 and group[0][0] == "equal"):
        groups.append(group)
    return groups


def render_numbered(lines, a, b):
    """Lines a..b (1-based) with their absolute numbers, for a context item."""
    return "\n".join(f"{i:>6} | {lines[i - 1].rstrip()}" for i in range(a, min(b, len(lines)) + 1))


# --- call edges ---------------------------------------------------------------------

# Names too common to say anything about a call (regex languages only).
_STOP = frozenset({
    "get", "set", "run", "data", "main", "init", "key", "value", "name", "args", "self",
    "this", "true", "false", "none", "null", "result", "error", "item", "list", "test",
    "config", "handle", "process", "update", "create", "delete", "render", "parse",
    "start", "stop", "open", "close", "read", "write", "send", "load", "save", "next",
    "call", "apply", "exec", "emit", "done", "size", "text", "type", "path", "file",
})
_TOKEN = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
CALLERS_PYTHON = 8
CALLERS_REGEX = 4


def _unit_at(units, starts, line):
    return _locate(units, starts, line)


def unit_calls(lang, text, lines, units):
    """{unit index: {called name: first line}} -- the names each unit calls.

    Python: from the ast (a call `f(...)`, `obj.f(...)`). Other languages: every
    identifier of the unit that is not on a comment line, a name-reference regex
    (the callers filter it by what is defined)."""
    starts = [u["start"] for u in units]
    out = defaultdict(dict)
    if lang == "python":
        try:
            tree = ast.parse(text)
        except Exception:
            return {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
                if not name:
                    continue
                ui = _unit_at(units, starts, node.lineno)
                if ui >= 0:
                    out[ui].setdefault(name, node.lineno)
        return dict(out)
    for ui, u in enumerate(units):
        for ln in range(u["start"], u["end"] + 1):
            s = lines[ln - 1].strip()
            if not s or s.startswith(("//", "#", "*", "/*", "--")):
                continue
            for tok in _TOKEN.findall(s):
                out[ui].setdefault(tok, ln)
    return dict(out)


def callers_of(lang, units, calls, targets):
    """{target unit index: [{"caller": ui, "name": n, "line": L}]} for the named
    units in `targets`: same-file units that call something the target defines.

    Capped per target (8 for Python, 4 elsewhere). In the regex languages a name
    under four characters, one on the stoplist, and one defined by two units
    are skipped: a mere reference to such a name proves too little."""
    regex = lang != "python"
    defined = defaultdict(set)
    for ui, u in enumerate(units):
        for nm in u.get("provides") or ():
            defined[nm].add(ui)
    out = {}
    for ti in targets:
        names = [nm for nm in (units[ti].get("provides") or ())
                 if not (nm.startswith("__") and nm.endswith("__"))]
        if regex:
            names = [nm for nm in names
                     if len(nm) >= 4 and nm.lower() not in _STOP and len(defined[nm]) == 1]
        hits = []
        for ui, called in calls.items():
            if ui == ti:
                continue
            for nm in names:
                if nm in called:
                    hits.append({"caller": ui, "name": nm, "line": called[nm]})
                    break
        hits.sort(key=lambda h: (h["line"], h["caller"]))
        out[ti] = hits[:CALLERS_REGEX if regex else CALLERS_PYTHON]
    return out


def preamble_lines(units, lines, limit=60):
    """The imports/preamble: the lines before the first definition, capped."""
    first = next((u["start"] for u in units if u["ident"] in ("ast", "heur") and u["kind"] != "region"),
                 len(lines) + 1)
    out = lines[:max(0, min(first - 1, limit))]
    while out and not out[-1].strip():
        out.pop()
    return out


def signature_index(units, changed_ids, limit=300):
    """One line per named unit: `L10-40  def foo(a, b)  [CHANGED]`."""
    out = []
    for ui, u in enumerate(units):
        if u["kind"] == "region" or not u.get("sig"):
            continue
        out.append(f"{u['start']}-{u['end']}  {u['sig']}" + ("  [CHANGED]" if ui in changed_ids else ""))
        if len(out) >= limit:
            out.append("... (index truncated)")
            break
    return out
