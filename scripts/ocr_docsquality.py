"""Deterministic documentation-quality evidence + finding-contract validator.

House-pattern CLI (mirrors ocr_demands.py): JSON in -> JSON out, fail-soft,
telemetry, generated schema. Two modes (SPEC-14 stories 11, 12, 13, 19):

* Evidence mode: for a list of repo-relative changed doc files, compute
  existence facts and prose surface stats against the review tree. The tree is
  a checkout at the tip, so filesystem reads ARE at-tip reads -- no git calls.

    - missing_paths: inline-code and markdown-link path mentions that do not
      exist on disk, each {"path", "line", "kind"} (kind is "markdown_link" or
      "inline_code");
    - unresolved_adr_refs: ADR number/slug mentions ("ADR 0001", "adr/0001-x")
      resolving to no file under the discovered records root (probed in
      _RECORD_ROOTS order, same conventions as review-gate.py);
    - dangling_links: relative markdown links (http(s)/mailto ignored) whose
      target path does not exist (a subset of missing_paths, link-kind only);
    - deslop: catalog_hits from scripts/deslop_catalog.json (phrase + line +
      category + optional replacement), throat_clearing openers, em_dash
      count, and template_skeletons (Summary/Key Changes/Testing/Notes-style
      bold-first section skeletons);
    - ste: sentence_length outliers (words > cap, default 25), an acronym
      first-use table (defined vs never-defined: a definition is the acronym
      adjacent to an expansion, quoted or in parentheses), and a
      passive_voice estimate (be-verb + past participle, count + samples).

* Validate mode (--validate, findings JSON on stdin): enforce the deslop
  cluster rule on code-reviewer findings. Every input field is preserved
  verbatim, except: a finding WITHOUT document_wide: true whose tells array
  carries fewer than 3 co-located entries is demoted -- severity becomes
  "note", and "demoted": true plus a "demotion_reason" are added. A tell is
  co-located only when its line is a number inside the finding's
  start_line..end_line span: the contract is (phrase + line), so a tell
  without a line never counts, and a finding without numeric line bounds
  never satisfies co-location (only document_wide: true saves it).

Fail-soft: public entry points never raise; garbage input degrades to empty
results plus a warning. Telemetry is best-effort (collectors tier).

CLI::

    python3 scripts/ocr_docsquality.py --repo-root PATH [--sentence-cap N] FILE [FILE ...]
    python3 scripts/ocr_docsquality.py --validate < findings.json
    python3 scripts/ocr_docsquality.py --dump-schema [PATH]

schemas/docsquality.schema.json is GENERATED from this module
(DOCQUALITY_SCHEMA below); do not edit it by hand.

Refreshing scripts/deslop_catalog.json: re-derive it from the deslop skill's
references/phrases.md (bullet phrases with "(use: ...)" replacements and
Avoid/Use-instead tables) and references/tropes.md (quoted tell phrases in
each trope's description), one entry per unique phrase, then re-run this
file's test suite.
"""
import json
import os
import re
import sys

import ocr_telemetry

# Records-root probe order: identical to review-gate.py::_RECORD_ROOTS.
_RECORD_ROOTS = ("docs/adr", "docs/adrs", "docs/decisions", "docs/decision-records",
                 "adr")

SENTENCE_CAP_DEFAULT = 25
PASSIVE_SAMPLE_LIMIT = 5

_CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "deslop_catalog.json")

# Throat-clearing openers (fixed list per SPEC-14.2 ticket): exact-case,
# sentence-position phrases that announce instead of saying.
_THROAT_CLEARING = ("This PR", "This change", "This document", "Note that",
                    "It's worth noting")
_THROAT_CLEARING_RE = re.compile(
    r"(?:^|(?<=[.!?]\s))(" + "|".join(re.escape(p) for p in _THROAT_CLEARING) + r")\b")

# Template-skeleton section titles (PR-description boilerplate).
_SKELETON_TITLES = ("summary", "key changes", "changes", "testing", "test plan",
                    "notes", "overview", "description")
_SKELETON_TITLES_RE = "|".join(re.escape(t) for t in _SKELETON_TITLES)
_SKELETON_HEADING_RE = re.compile(r"^#{1,6}\s*(" + _SKELETON_TITLES_RE + r")\s*:?\s*$",
                                  re.IGNORECASE)
_SKELETON_BOLD_RE = re.compile(r"^\*\*(" + _SKELETON_TITLES_RE + r")\*\*",
                               re.IGNORECASE)

# Words ending in "ed" that are not past participles.
_NON_PARTICIPLES = frozenset({"red", "bed"})
_BE_VERB = ("is|are|was|were|be|been|being|am|gets?|got|getting")
_PASSIVE_RE = re.compile(r"\b(?:" + _BE_VERB + r")(?:\s+\w+ly)?\s+"
                         r"(\w+(?:ed|en))\b", re.IGNORECASE)

_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{2,}\b")
_MD_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_ADR_NUMBER_RE = re.compile(r"\bADR[\s-]#?\s*0*(\d{1,5})\b", re.IGNORECASE)
# A cited ADR by number+slug ("ADR 0006", "adr/0006-custom-checks"). The
# lookahead excludes ISO dates (2024-01-15, 2026-11), which would otherwise
# flood unresolved_adr_refs with record numbers that can never exist.
_ADR_SLUG_RE = re.compile(r"\b(\d{4})-(?!\d{2}(?:-\d{2})?\b)[a-z0-9][a-z0-9-]*")
_ADR_RECORD_NUM_RE = re.compile(r"^(\d+)")

# A path mention: word chars plus . / -, either carrying a "/" or ending in a
# known doc/code extension. Keeps `os.path.join` and `foo(bar)` out.
_PATHLIKE_RE = re.compile(r"^(?:\.?/?|~?/?)[\w][\w./-]*$")
_PATH_EXT_RE = re.compile(r"\.(md|markdown|txt|rst|adoc|py|js|jsx|ts|tsx|json|"
                          r"ya?ml|toml|cfg|ini|sh|bash|go|rb|rs|c|h|cpp|hpp|"
                          r"java|kt|swift|css|html)$", re.IGNORECASE)

_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _norm_rel(path):
    """Forward-slash relative form, one leading ``./`` stripped."""
    path = str(path).replace("\\", "/")
    if path.startswith("./"):
        path = path[2:]
    return path


def _prose_lines(text):
    """Line dicts for text outside fenced code blocks.

    Returns [{"n": 1-based line number, "text": line}] in document order.
    """
    out, in_fence = [], False
    for n, line in enumerate(str(text).splitlines(), 1):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append({"n": n, "text": line})
    return out


# ---------------------------------------------------------------------------
# Deslop catalog
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\\\[[^\]]*\\\]")

_catalog_cache = None


def _phrase_regex(phrase):
    """Compile one catalog phrase to a case-insensitive regex.

    ``[...]`` placeholders match one non-space token; word boundaries are
    anchored where the phrase starts/ends with a word character.
    """
    esc = re.escape(phrase)
    esc = _PLACEHOLDER_RE.sub(r"\\S+", esc)
    if re.match(r"[\w']", phrase):
        esc = r"\b" + esc
    if re.match(r"\w", phrase[-1]):
        esc = esc + r"\b"
    try:
        return re.compile(esc, re.IGNORECASE)
    except re.error:
        return None


def load_catalog(path=None):
    """Load deslop_catalog.json, each entry with a precompiled regex.

    Returns [] on any problem -- fail soft like everything else here.
    """
    global _catalog_cache
    if path is None:
        if _catalog_cache is not None:
            return _catalog_cache
        path = _CATALOG_PATH
    entries = []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        for item in data.get("entries") or []:
            if not isinstance(item, dict) or not isinstance(item.get("phrase"), str):
                continue
            rx = _phrase_regex(item["phrase"])
            if rx is None:
                continue
            entry = {"phrase": item["phrase"],
                     "category": str(item.get("category", "")),
                     "regex": rx}
            if isinstance(item.get("replacement"), str) and item["replacement"].strip():
                entry["replacement"] = item["replacement"]
            entries.append(entry)
    except (OSError, ValueError):
        entries = []
    if path == _CATALOG_PATH:
        _catalog_cache = entries
    return entries


def _scan_deslop(prose, catalog):
    """Deslop evidence for one file's prose lines."""
    catalog_hits, throat, skeletons = [], [], []
    em_dash_count = 0
    for ln in prose:
        text = ln["text"]
        em_dash_count += text.count("—")
        for entry in catalog:
            if entry["regex"].search(text):
                hit = {"phrase": entry["phrase"], "category": entry["category"],
                       "line": ln["n"]}
                if "replacement" in entry:
                    hit["replacement"] = entry["replacement"]
                catalog_hits.append(hit)
        m = _THROAT_CLEARING_RE.search(text)
        if m:
            throat.append({"phrase": m.group(1), "line": ln["n"]})
        m = _SKELETON_HEADING_RE.match(text) or _SKELETON_BOLD_RE.match(text)
        if m:
            skeletons.append({"heading": m.group(1), "line": ln["n"]})
    catalog_hits.sort(key=lambda h: (h["line"], h["phrase"].lower()))
    return {"catalog_hits": catalog_hits,
            "throat_clearing": throat,
            "em_dash_count": em_dash_count,
            "template_skeletons": skeletons}


# ---------------------------------------------------------------------------
# Path references + ADR references + dangling links
# ---------------------------------------------------------------------------

def _collect_path_mentions(repo_root, prose, doc_rel=""):
    """Path mentions with existence checked against the review tree.

    Returns (missing_paths, dangling_links); entry dicts {"path", "line",
    "kind"} with kind "markdown_link" or "inline_code". Relative markdown
    links resolve against the DOCUMENT's directory (standard markdown-
    viewer semantics), repo-root-absolute links ("/docs/x") against the
    repository root; inline-code path mentions resolve against the repo
    root (they name repo paths by convention).
    """
    seen, missing_paths, dangling_links = set(), [], []
    for ln in prose:
        text, n = ln["text"], ln["n"]
        mentions = []
        for m in _MD_LINK_RE.finditer(text):
            if text[m.start():m.start() + 1] == "!":
                continue  # image embed, not a doc link
            target = m.group(1).strip()
            if re.match(r"(?i)^(https?:|mailto:)", target):
                continue
            path, _, _anchor = target.partition("#")
            path = path.split("?", 1)[0].strip()
            if path.startswith("/"):
                # GitHub convention: leading "/" means repo root.
                resolved = path.lstrip("/")
            else:
                # Standard markdown: resolve against the document's
                # directory; normpath collapses ".." components.
                resolved = os.path.normpath(
                    os.path.join(os.path.dirname(doc_rel), path))
            path = resolved.replace(os.sep, "/")
            if not path or path.startswith(".."):
                # Anything escaping the repo is a dangling external ref.
                path = path or target
            if not path:
                continue
            mentions.append((path, n, "markdown_link"))
        for m in _INLINE_CODE_RE.finditer(text):
            code = m.group(1).strip()
            if not code:
                continue
            path = _norm_rel(code)
            if not _PATHLIKE_RE.match(path):
                continue
            if "/" not in path and not _PATH_EXT_RE.search(path):
                continue
            mentions.append((path, n, "inline_code"))
        for path, line, kind in mentions:
            key = (path, line, kind)
            if key in seen:
                continue
            seen.add(key)
            if os.path.exists(os.path.join(repo_root, path)):
                continue
            entry = {"path": path, "line": line, "kind": kind}
            missing_paths.append(entry)
            if kind == "markdown_link":
                dangling_links.append(dict(entry))
    missing_paths.sort(key=lambda e: (e["line"], e["path"]))
    dangling_links.sort(key=lambda e: (e["line"], e["path"]))
    return missing_paths, dangling_links


def _records_root(repo_root):
    """The repo's decision-records directory at its conventional location."""
    for rel in _RECORD_ROOTS:
        if os.path.isdir(os.path.join(repo_root, rel)):
            return rel
    return None


def _adr_numbers_under(repo_root, records_root):
    """Leading record numbers of files directly under the records root."""
    numbers = set()
    try:
        for name in os.listdir(os.path.join(repo_root, records_root)):
            m = _ADR_RECORD_NUM_RE.match(name)
            if m:
                numbers.add(int(m.group(1)))
    except OSError:
        pass
    return numbers


def _scan_adr_refs(repo_root, prose):
    """ADR number/slug mentions with no record under the records root."""
    records_root = _records_root(repo_root)
    numbers = _adr_numbers_under(repo_root, records_root) if records_root else set()
    unresolved = []
    for ln in prose:
        text, n = ln["text"], ln["n"]
        for m in _ADR_NUMBER_RE.finditer(text):
            num = int(m.group(1))
            if num not in numbers:
                unresolved.append({"ref": "ADR %04d" % num, "line": n})
        for m in _ADR_SLUG_RE.finditer(text):
            if int(m.group(1)) not in numbers:
                unresolved.append({"ref": m.group(0), "line": n})
    unresolved.sort(key=lambda e: (e["line"], e["ref"]))
    return unresolved


# ---------------------------------------------------------------------------
# STE surface stats
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"https?://\S+")
_WORD_RE = re.compile(r"[A-Za-z0-9]")


def _split_sentences(text):
    """Split a text run into sentences on [.!?] runs."""
    pieces = re.split(r"[.!?]+(?:[\"')\]]+)?\s+", text)
    return [p.strip() for p in pieces if p.strip()]


def _words(sentence):
    return [w for w in _WS_RE.split(sentence) if _WORD_RE.search(w)]


def _scan_ste(prose, sentence_cap):
    """Sentence-length outliers, attributed to the prose line they start on.

    Each prose line is split on its own (headings and list markers stripped,
    inline code collapsed to one word): a sentence spilling onto the next line
    is attributed to the line it starts on -- these are surface estimates for
    the agent to judge from, not verdicts.
    """
    over_cap = []
    # Paragraph assembly first (wrapped sentences count as one sentence):
    # contiguous non-blank, non-heading lines form one paragraph whose
    # starting line attributes all of its sentences.
    para_text, para_start = "", None
    paras = []
    for ln in prose:
        text, n = ln["text"].strip(), ln["n"]
        if not text or text.startswith("#"):
            if para_text:
                paras.append((para_start, para_text))
                para_text, para_start = "", None
            continue
        text = re.sub(r"^(?:[-*+]|\d+[.)])\s+", "", text)
        text = _INLINE_CODE_RE.sub("CODE", text)
        text = _URL_RE.sub("URL", text)
        if para_start is None:
            para_start = n
        para_text = (para_text + " " + text).strip()
    if para_text:
        paras.append((para_start, para_text))
    for n, para in paras:
        for sent in _split_sentences(para):
            n_words = len(_words(sent))
            if sentence_cap is not None and n_words > sentence_cap:
                over_cap.append({"line": n, "words": n_words})
    over_cap.sort(key=lambda e: (e["line"], e["words"]))
    return {"cap": sentence_cap, "over_cap": over_cap}


def _expansion_parens_re(acronym):
    k = re.escape(acronym)
    return [
        # Expansion followed by (K).
        re.compile(r"[A-Za-z][A-Za-z0-9 ,/'-]*\(\s*" + k + r"\s*\)"),
        # K (expansion) or K "expansion".
        re.compile(r"\b" + k + r"\b\s*\(\s*[A-Za-z][A-Za-z0-9 ,/'-]*[a-z]"
                   r"[A-Za-z0-9 ,/'-]*\s*\)"),
        re.compile(r"\b" + k + r"\b\s*[\"'][^\"']*[a-z][^\"']*[\"']"),
    ]


def _scan_acronyms(prose):
    """ALL-CAPS acronym (3+ letters) first-use table. A definition counts
    only when it appears at (or immediately after) the acronym's FIRST
    occurrence -- an acronym used bare for 20 lines and expanded on line 21
    is reported as undefined, the wait-what first-use rule."""
    found = {}
    for ln in prose:
        text = _INLINE_CODE_RE.sub(" ", ln["text"])
        for m in _ACRONYM_RE.finditer(text):
            found.setdefault(m.group(0), ln["n"])
    patterns = {k: _expansion_parens_re(k) for k in found}
    table = []
    for acr in sorted(found):
        first_line = found[acr]
        # Only the first occurrence's own line (and its immediate follow-up
        # lines, for wrapped definitions) can define it: later prose is out.
        first_use_text = "\n".join(
            ln["text"] for ln in prose if ln["n"] in (first_line, first_line + 1))
        first_use_text = _INLINE_CODE_RE.sub(" ", first_use_text)
        defined = any(rx.search(first_use_text) for rx in patterns[acr])
        table.append({"acronym": acr, "defined": defined,
                      "first_line": first_line})
    return table


def _scan_passive(prose):
    """Be-verb + past participle estimate: count plus up to 5 sample lines."""
    count, samples = 0, []
    for ln in prose:
        text = _INLINE_CODE_RE.sub(" ", ln["text"])
        for m in _PASSIVE_RE.finditer(text):
            participle = m.group(1).lower()
            if participle in _NON_PARTICIPLES:
                continue
            count += 1
            if len(samples) < PASSIVE_SAMPLE_LIMIT:
                samples.append({"line": ln["n"], "text": ln["text"].strip()})
    return {"count": count, "samples": samples}


# ---------------------------------------------------------------------------
# Evidence mode
# ---------------------------------------------------------------------------

def _evidence_for_file(repo_root, rel, sentence_cap, catalog, warnings):
    path = os.path.join(repo_root, rel)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        warnings.append("docsquality: could not read %s (%s); skipped" % (rel, exc))
        return None
    prose = _prose_lines(text)
    missing_paths, dangling_links = _collect_path_mentions(repo_root, prose,
                                                           doc_rel=rel)
    return {
        "missing_paths": missing_paths,
        "unresolved_adr_refs": _scan_adr_refs(repo_root, prose),
        "dangling_links": dangling_links,
        "deslop": _scan_deslop(prose, catalog),
        "ste": {
            "sentence_length": _scan_ste(prose, sentence_cap),
            "acronyms": _scan_acronyms(prose),
            "passive_voice": _scan_passive(prose),
        },
    }


def compute_evidence(repo_root, files, *, sentence_cap=SENTENCE_CAP_DEFAULT):
    """Evidence for each changed doc file. Fail-soft: never raises.

    Returns {"evidence": {rel: {...}}, "warnings": [...]}; files that could
    not be read are omitted with a warning.
    """
    warnings = []
    try:
        repo_root = os.path.abspath(str(repo_root))
        cap = sentence_cap if isinstance(sentence_cap, int) and sentence_cap > 0 \
            else SENTENCE_CAP_DEFAULT
        catalog = load_catalog()
        evidence = {}
        for f in files or []:
            rel = _norm_rel(f)
            entry = _evidence_for_file(repo_root, rel, cap, catalog, warnings)
            if entry is not None:
                evidence[rel] = entry
    except Exception as exc:  # fail-soft: never break the review
        warnings.append("docsquality: evidence computation failed (%s)" % exc)
        evidence = {}
    return {"evidence": evidence, "warnings": warnings}


# ---------------------------------------------------------------------------
# Validate mode: the deslop cluster rule on findings
# ---------------------------------------------------------------------------

def _co_located_tells(finding):
    """How many of the finding's tells sit inside its own line span.

    The contract is (phrase + line): a tell counts only when it carries a
    numeric line inside the finding's numeric start_line..end_line span. A
    tell without a line never counts (a model must not dodge the cluster
    rule by omitting line references), and a finding without line bounds
    has no span to co-locate anything in -- only document_wide: true saves
    such a finding from demotion.
    """
    tells = finding.get("tells")
    if not isinstance(tells, list):
        return 0
    start, end = finding.get("start_line"), finding.get("end_line")
    if not (isinstance(start, (int, float)) and not isinstance(start, bool)
            and isinstance(end, (int, float)) and not isinstance(end, bool)):
        return 0
    bounds = (int(start), int(end))
    co_located = 0
    for tell in tells:
        if not isinstance(tell, dict):
            continue
        line = tell.get("line")
        if isinstance(line, (int, float)) and not isinstance(line, bool) \
                and bounds[0] <= int(line) <= bounds[1]:
            co_located += 1
    return co_located


def _validate_one_finding(finding):
    """Apply the cluster rule to one finding; returns (finding, was_demoted).

    Every input field is preserved verbatim. A finding with fewer than 3
    co-located tells and no document_wide: true is demoted to note severity.
    """
    if not isinstance(finding, dict):
        return finding, False
    if finding.get("document_wide") is True:
        return dict(finding), False
    co_located = _co_located_tells(finding)
    if co_located >= 3:
        return dict(finding), False
    original_severity = finding.get("severity", "?")
    out = dict(finding)
    out["severity"] = "note"
    out["demoted"] = True
    out["demotion_reason"] = (
        "fewer than 3 co-located tells (%d) and not document_wide; demoted from "
        "%s severity to note (deslop cluster rule: a finding needs 3+ co-located "
        "tells or the same tell document-wide)" % (co_located, original_severity))
    return out, True


def validate_findings(text):
    """Validate docs-quality findings against the cluster rule. Never raises.

    Accepts a JSON array of findings or {"findings": [...]}. Returns
    {"findings": [...], "demoted": N, "warnings": [...]} -- every input field
    preserved verbatim except demoted findings (severity -> note, plus
    "demoted" and "demotion_reason"). Input that is not a findings array
    degrades to an empty list plus a warning.
    """
    warnings = []
    try:
        data = json.loads(text) if isinstance(text, str) else text
    except ValueError as exc:
        warnings.append("docsquality: input was not valid JSON (%s); "
                        "no findings validated" % exc)
        return {"findings": [], "demoted": 0, "warnings": warnings}
    if isinstance(data, dict):
        findings = data.get("findings")
        if not isinstance(findings, list):
            warnings.append("docsquality: input object's \"findings\" is not an "
                            "array; no findings validated")
            return {"findings": [], "demoted": 0, "warnings": warnings}
    elif isinstance(data, list):
        findings = data
    else:
        warnings.append("docsquality: input must be a findings array or an object "
                        "with a findings array; no findings validated")
        return {"findings": [], "demoted": 0, "warnings": warnings}
    out, demoted = [], 0
    try:
        for finding in findings:
            checked, was_demoted = _validate_one_finding(finding)
            out.append(checked)
            demoted += 1 if was_demoted else 0
    except Exception as exc:  # fail-soft
        warnings.append("docsquality: validation failed midway (%s); "
                        "partial results returned" % exc)
    return {"findings": out, "demoted": demoted, "warnings": warnings}


# ---------------------------------------------------------------------------
# Generated schema
# ---------------------------------------------------------------------------

def _schema():
    line_ref = {"type": "object", "required": ["line"],
                "additionalProperties": True,
                "properties": {"line": {"type": "integer"}}}
    path_entry = {
        "type": "object",
        "required": ["path", "line", "kind"],
        "additionalProperties": False,
        "properties": {
            "path": {"type": "string"},
            "line": {"type": "integer"},
            "kind": {"enum": ["markdown_link", "inline_code"]},
        },
    }
    per_file = {
        "type": "object",
        "required": ["missing_paths", "unresolved_adr_refs", "dangling_links",
                     "deslop", "ste"],
        "additionalProperties": False,
        "properties": {
            "missing_paths": {"type": "array", "items": path_entry},
            "unresolved_adr_refs": {
                "type": "array",
                "items": {"type": "object", "required": ["ref", "line"],
                          "additionalProperties": False,
                          "properties": {"ref": {"type": "string"},
                                         "line": {"type": "integer"}}}},
            "dangling_links": {"type": "array", "items": path_entry},
            "deslop": {
                "type": "object",
                "required": ["catalog_hits", "throat_clearing", "em_dash_count",
                             "template_skeletons"],
                "additionalProperties": False,
                "properties": {
                    "catalog_hits": {
                        "type": "array",
                        "items": {"type": "object",
                                  "required": ["phrase", "category", "line"],
                                  "additionalProperties": False,
                                  "properties": {
                                      "phrase": {"type": "string"},
                                      "category": {"type": "string"},
                                      "line": {"type": "integer"},
                                      "replacement": {"type": "string"}}}},
                    "throat_clearing": {
                        "type": "array",
                        "items": {"type": "object",
                                  "required": ["phrase", "line"],
                                  "additionalProperties": False,
                                  "properties": {"phrase": {"type": "string"},
                                                 "line": {"type": "integer"}}}},
                    "em_dash_count": {"type": "integer", "minimum": 0},
                    "template_skeletons": {
                        "type": "array",
                        "items": {"type": "object",
                                  "required": ["heading", "line"],
                                  "additionalProperties": False,
                                  "properties": {"heading": {"type": "string"},
                                                 "line": {"type": "integer"}}}},
                },
            },
            "ste": {
                "type": "object",
                "required": ["sentence_length", "acronyms", "passive_voice"],
                "additionalProperties": False,
                "properties": {
                    "sentence_length": {
                        "type": "object",
                        "required": ["cap", "over_cap"],
                        "additionalProperties": False,
                        "properties": {
                            "cap": {"type": ["integer", "null"]},
                            "over_cap": {
                                "type": "array",
                                "items": {"type": "object",
                                          "required": ["line", "words"],
                                          "additionalProperties": False,
                                          "properties": {
                                              "line": {"type": "integer"},
                                              "words": {"type": "integer",
                                                        "minimum": 0}}}}}},
                    "acronyms": {
                        "type": "array",
                        "items": {"type": "object",
                                  "required": ["acronym", "defined",
                                               "first_line"],
                                  "additionalProperties": False,
                                  "properties": {
                                      "acronym": {"type": "string"},
                                      "defined": {"type": "boolean"},
                                      "first_line": {"type": "integer"}}}},
                    "passive_voice": {
                        "type": "object",
                        "required": ["count", "samples"],
                        "additionalProperties": False,
                        "properties": {
                            "count": {"type": "integer", "minimum": 0},
                            "samples": {"type": "array", "items": line_ref}}},
                },
            },
        },
    }
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "https://github.com/Jose-Ribeir/claude-code-review-gate/schemas/docsquality.schema.json",
        "title": "docs-quality evidence",
        "description": "Deterministic documentation-quality evidence emitted by "
                       "scripts/ocr_docsquality.py evidence mode (existence facts, "
                       "deslop scan, STE surface stats). This schema is generated "
                       "from that module; do not edit by hand.",
        "type": "object",
        "required": ["evidence", "warnings"],
        "additionalProperties": False,
        "properties": {
            "evidence": {"type": "object",
                         "additionalProperties": per_file},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
    }


DOCQUALITY_SCHEMA = _schema()


def _record_telemetry(repo_root, out_text):
    """Collectors-tier cost record for the deterministic evidence pass.

    Best-effort: a non-repo root (the common scratch fixture) writes nothing.
    """
    try:
        common = ocr_telemetry.repo_common_dir(str(repo_root or os.getcwd()))
        if not common:
            return
        ocr_telemetry.record_tier_call(
            common, ocr_telemetry.TIER_COLLECTORS, model="",
            token_estimate=ocr_telemetry.estimate_tokens(len(out_text or "")))
    except Exception:
        pass


def _main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description="Docs-quality evidence + finding-contract validator "
                    "(JSON in -> JSON out).")
    parser.add_argument("--repo-root", help="review tree root (evidence mode)")
    parser.add_argument("--sentence-cap", type=int, default=SENTENCE_CAP_DEFAULT,
                        help="max words per sentence before it is reported "
                             "(default %(default)s)")
    parser.add_argument("--validate", action="store_true",
                        help="validate findings JSON on stdin against the "
                             "deslop cluster rule")
    parser.add_argument("--dump-schema", metavar="PATH", nargs="?", const="-",
                        help="print or write the evidence JSON schema and exit")
    parser.add_argument("files", nargs="*",
                        help="repo-relative doc files (evidence mode)")
    args = parser.parse_args(argv)
    if args.dump_schema is not None:
        text = json.dumps(DOCQUALITY_SCHEMA, indent=2) + "\n"
        if args.dump_schema == "-":
            sys.stdout.write(text)
        else:
            with open(args.dump_schema, "w", encoding="utf-8") as fh:
                fh.write(text)
        return 0
    if args.validate:
        text = sys.stdin.read()
        result = validate_findings(text)
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        _record_telemetry(args.repo_root, text)
        return 0
    result = compute_evidence(args.repo_root or os.getcwd(), args.files,
                              sentence_cap=args.sentence_cap)
    out_text = json.dumps(result, ensure_ascii=False)
    sys.stdout.write(out_text + "\n")
    _record_telemetry(args.repo_root, out_text)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
