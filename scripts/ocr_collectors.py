#!/usr/bin/env python3
#
# Deterministic evidence collectors for review-gate v2 (t05): spotter demands
# in, bounded evidence bundle out.
#
# The context spotter (agents/context-spotter.md) emits *demands* validated by
# scripts/ocr_demands.py. This module maps each demand kind to gathered
# evidence, deterministically and fail-soft:
#
#   symbol_callers  -> ocr_impact.find_references under the CONTEXT allowlist
#                      (t01 split: spec/test files are searched), one
#                      per-symbol bundle per demand: name, sites with
#                      snippets, ref_counts.
#   conventions     -> ocr_conventions.resolve_conventions (t03 resolver,
#                      in-process import -- never a subprocess from the gate).
#   doc_refs        -> the same t03 resolver; a demanded doc that no resolved
#                      file carries is an explicit ``unresolved`` entry.
#   event_couples   -> ocr_index.query_event_couples; an unavailable index
#                      yields explicit ``unavailable`` entries + one warning,
#                      never an error (t06's index is optional).
#   prose_pointers  -> ocr_index.query_pointer_utterances filtered to the
#                      demanded names, each with a resolution STUB
#                      ({"status": "deferred"} -- real resolution is t09's
#                      ``pointer_resolver``; the seam is a callable parameter
#                      of ``collect``, so t09 slots in without touching this
#                      API).
#
# Bundle contract (spec "Evidence bundle contract"): every demand resolves to
# evidence (status "ok") or an explicit ``unresolved`` entry the reviewer may
# cite as a medium-severity finding. The bundle is capped at
# MAX_BUNDLE_CHARS, degrading largest-entry-first with warnings and never
# dropping an entry entirely (mirrors the diff-truncation discipline).
# ADR 0001: the bundle shape is validated before it may reach the reviewer
# prompt -- ``collect`` self-validates, and the gate must call
# ``validate_bundle`` on any bundle it did not build itself.
#
# Fail-soft: every public entry point never raises. A collector that raises
# degrades its demands to ``unavailable`` entries plus a warning; a malformed
# demands object yields an empty bundle. Nothing here can crash a review.
#
# CLI::
#
#     python3 scripts/ocr_collectors.py --repo-root PATH \
#         [--changed-file F ...] [--tip REF] [--max-chars N] < demands.json
#     python3 scripts/ocr_collectors.py --validate-bundle < bundle.json
#
# The first form prints {"bundle": ..., "warnings": [...]} and always exits 0;
# stdin may be either the demands object or the validator wrapper
# {"demands": ..., "warnings": [...]}. The second form prints
# {"ok": ..., "errors": [...]} (exit 1 when invalid).
#
import argparse
import json
import os
import re
import sys

import ocr_conventions
import ocr_impact
import ocr_index
from ocr_demands import DEMAND_KEYS

BUNDLE_VERSION = 1
MAX_BUNDLE_CHARS = 20000

_ENTRY_STATUSES = ("ok", "unresolved", "unavailable")

# Per-collector search caps; the *bundle* char cap below is the outer bound.
_MAX_SITES = 300

# Pointer-resolution stub: t09 implements real resolution (artifact registry,
# repo search, branch tips, forge lookup) and passes it as ``pointer_resolver``.
def _deferred_resolution(target):
    return {"status": "deferred", "reason": "pointer resolution lands in t09"}


# ---------------------------------------------------------------------------
# Default context filter (standalone / CLI use)
# ---------------------------------------------------------------------------

# Mirrors review-gate.py::_is_context_allowed_path in spirit (t01 allowlist
# split): test-family dirs ARE searchable -- a caller in a spec file is
# exactly where impact analysis must look. review-gate passes its own
# authoritative filter; this default only serves standalone/CLI use.
_DEFAULT_SKIP_DIRS = frozenset({
    ".git", ".idea", ".vscode", ".next", ".ocr",
    "vendor", "node_modules", "dist", "build", "out", "target",
    "__generated__", "__pycache__",
})
_EXCLUDED_SUFFIX_RE = re.compile(r"(\.min\.js|\.generated\.[^/\\]+)$", re.IGNORECASE)
_EXCLUDED_FILES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "go.sum",
    "cargo.lock", "poetry.lock", "composer.lock",
})


def default_is_context_allowed(path):
    """Context-filter fallback; review-gate's _is_context_allowed_path governs
    when wired in (t07)."""
    norm = str(path).replace("\\", "/")
    parts = norm.split("/")
    if parts[-1].lower() in _EXCLUDED_FILES:
        return False
    if _EXCLUDED_SUFFIX_RE.search(parts[-1]):
        return False
    if any(p.lower() in _DEFAULT_SKIP_DIRS for p in parts[:-1]):
        return False
    return True


# ---------------------------------------------------------------------------
# Demand-entry helpers
# ---------------------------------------------------------------------------

def _demand_name(entry, *keys):
    """Best-effort short name for a demand entry (string or small object)."""
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        for key in keys:
            v = entry.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in entry.values():
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _entry(kind, demands, status, evidence=None, note=None):
    if isinstance(demands, list):
        served = list(demands)
    else:
        served = [demands]
    return {"id": "", "kind": kind, "demands": served, "status": status,
            "evidence": evidence, "note": note}


def _unavailable_entries(kind, demands, note):
    return [_entry(kind, d, "unavailable", note=note) for d in demands]


# ---------------------------------------------------------------------------
# Cap enforcement: largest-entry-first degradation with warnings
# ---------------------------------------------------------------------------

_TRUNC_SUFFIX = "\n[cut %d chars: bundle budget]"
_DROPPED_NOTE = "%d item(s) dropped: bundle budget"


def _json_chars(obj):
    return len(json.dumps(obj, ensure_ascii=False))


def _walk_strings(node, path=()):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_strings(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_strings(v, path + (i,))


def _set_path(node, path, value, drop=False):
    for key in path[:-1]:
        node = node[key]
    if drop:
        del node[path[-1]]
    else:
        node[path[-1]] = value


def _walk_lists(node, path=()):
    if isinstance(node, list):
        yield path, node
        for i, v in enumerate(node):
            yield from _walk_lists(v, path + (i,))
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_lists(v, path + (k,))


def _shrink_evidence(entry, target_chars):
    """Cut one entry's evidence toward target_chars. Returns True on progress.

    Phase A truncates the longest string leaves (snippets, doc text); phase B
    drops trailing elements of the longest list leaves (sites, files map
    entries are dicts -- strings shrink them there). Never removes the entry
    itself; the caller marks it truncated and warns.
    """
    evidence = entry.get("evidence")
    if not evidence:
        return False
    initial = _json_chars(evidence)
    size = initial
    if size <= target_chars:
        return True
    # Phase A: truncate long string leaves, longest first.
    for _ in range(64):
        strings = sorted(
            ((p, s) for p, s in _walk_strings(evidence) if len(s) > 160),
            key=lambda t: -len(t[1]))
        if not strings:
            break
        path, text = strings[0]
        keep = max(160, len(text) // 4)
        _set_path(evidence, path,
                  text[:keep] + (_TRUNC_SUFFIX % (len(text) - keep)))
        size = _json_chars(evidence)
        if size <= target_chars:
            return True
    # Phase B: drop list elements from the longest lists, until it fits or
    # no list has anything left to give.
    for _ in range(64):
        lists = sorted(((p, lst) for p, lst in _walk_lists(evidence) if lst),
                       key=lambda t: -len(t[1]))
        if not lists:
            break
        path, lst = lists[0]
        lst.pop()
        dropped = entry.setdefault("_dropped", {})
        label = ".".join(str(p) for p in path) or "items"
        dropped[label] = dropped.get(label, 0) + 1
        size = _json_chars(evidence)
        if size <= target_chars:
            return True
    return size < initial


def _elide_evidence(entry):
    """Last resort: the entry stays, its evidence is replaced by a note."""
    had = _json_chars(entry.get("evidence"))
    entry["evidence"] = {"elided": True,
                         "note": "evidence elided: bundle budget exhausted "
                                 "(was %d chars)" % had}


def _enforce_cap(bundle, max_chars, warnings):
    """Degrade largest-entry-first until the entries fit max_chars.

    Mirrors the diff-truncation discipline: an entry is cut but never
    omitted; every cut warns.
    """
    entries = bundle["entries"]
    total = _json_chars(entries)
    warned = set()
    unshrinkable = set()
    while total > max_chars:
        candidates = [
            (entry.get("evidence") and _json_chars(entry["evidence"]) or 0, i, entry)
            for i, entry in enumerate(entries)
            if id(entry) not in unshrinkable and entry.get("evidence")]
        candidates = [c for c in candidates if c[0] > 0]
        if not candidates:
            # No evidence left to cut: shrink note text, then elide nothing.
            break
        size, _, target = max(candidates, key=lambda t: t[0])
        goal = max(600, size // 2)
        if _shrink_evidence(target, goal):
            pass
        else:
            _elide_evidence(target)
        target["truncated"] = True
        bundle["truncated"] = True
        if id(target) not in warned:
            warnings.append(
                "collectors: bundle over %d chars; entry '%s' (%s) truncated "
                "-- largest-entry-first degradation; evidence is partial"
                % (max_chars, target["id"], target["kind"]))
            warned.add(id(target))
        new_total = _json_chars(entries)
        if new_total >= total:
            unshrinkable.add(id(target))
            if len(unshrinkable) >= len(entries):
                break
        total = new_total
    bundle["total_chars"] = _json_chars(entries)


# ---------------------------------------------------------------------------
# ADR 0001: gate-side bundle validation
# ---------------------------------------------------------------------------

def _valid_demand_entry(value):
    """A demand entry is a non-empty string or a small flat object (same
    contract as ocr_demands' demand lists)."""
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, dict)


def validate_bundle(bundle):
    """Validate an evidence bundle for the reviewer prompt. Fail-closed.

    ADR 0001 discipline: a fixed shape, no improvisation rescue. The gate
    MUST check this on any bundle it did not build itself before attaching
    it to the reviewer prompt; a bundle that fails is dropped and the review
    proceeds without it (T0 behavior) plus a warning.

    Returns {"ok": bool, "errors": [...]}. Never raises.
    """
    errors = []
    try:
        if not isinstance(bundle, dict):
            return {"ok": False, "errors": ["bundle must be an object"]}
        if bundle.get("version") != BUNDLE_VERSION:
            errors.append("version must be %d" % BUNDLE_VERSION)
        entries = bundle.get("entries")
        if not isinstance(entries, list):
            errors.append("entries must be an array")
            entries = []
        for i, entry in enumerate(entries):
            label = "entries[%d]" % i
            if not isinstance(entry, dict):
                errors.append("%s must be an object" % label)
                continue
            if not isinstance(entry.get("id"), str) or not entry["id"]:
                errors.append("%s.id must be a non-empty string" % label)
            if entry.get("kind") not in DEMAND_KEYS:
                errors.append("%s.kind %r is not a demand kind" % (label, entry.get("kind")))
            demands = entry.get("demands")
            if not isinstance(demands, list) or not demands:
                errors.append("%s.demands must be a non-empty array" % label)
            elif not all(_valid_demand_entry(d) for d in demands):
                errors.append("%s.demands holds an invalid demand entry" % label)
            if entry.get("status") not in _ENTRY_STATUSES:
                errors.append("%s.status %r is not ok/unresolved/unavailable"
                              % (label, entry.get("status")))
            evidence = entry.get("evidence")
            if evidence is not None and not isinstance(evidence, dict):
                errors.append("%s.evidence must be an object or null" % label)
            if entry.get("status") == "ok" and evidence is None:
                errors.append("%s is ok but carries no evidence" % label)
            if entry.get("note") is not None and not isinstance(entry.get("note"), str):
                errors.append("%s.note must be a string or null" % label)
            if "truncated" in entry and not isinstance(entry["truncated"], bool):
                errors.append("%s.truncated must be a boolean" % label)
        if not isinstance(bundle.get("truncated"), bool):
            errors.append("truncated must be a boolean")
        if not isinstance(bundle.get("total_chars"), int):
            errors.append("total_chars must be an integer")
    except Exception as exc:  # fail-closed, never raise
        errors.append("validation failed internally: %s" % exc)
    return {"ok": not errors, "errors": errors}


# ---------------------------------------------------------------------------
# conventions / doc_refs -> the t03 resolver (imported, never subprocessed)
# ---------------------------------------------------------------------------

# The resolver's rendered blocks carry "## Convention: <label> (<layer>)"
# headers; labels are what doc_ref demands match against.
_DOC_HEADER_RE = re.compile(r"^## Convention: (?P<label>.+?) "
                            r"\((?P<layer>explicit|ancestor)\)$", re.MULTILINE)


def _doc_labels(text):
    return {m.group("label") for m in _DOC_HEADER_RE.finditer(text)}


def _doc_block(text, label):
    """Extract the rendered block of one doc from a file's merged text."""
    matches = list(_DOC_HEADER_RE.finditer(text))
    for i, m in enumerate(matches):
        if m.group("label") != label:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        return text[m.start():end].strip()
    return ""


def _match_doc_label(name, labels):
    """Match a demanded doc name against resolved doc labels, loosely: exact,
    basename, or suffix match so \"docs/STYLE.md\" and \"STYLE.md\" both serve
    the same resolved document."""
    norm = name.replace("\\", "/").strip("/").lower()
    if not norm:
        return None
    base = norm.rsplit("/", 1)[-1]
    for label in sorted(labels):
        lnorm = label.replace("\\", "/").lower()
        if lnorm == norm or lnorm.endswith("/" + norm) or norm.endswith("/" + lnorm):
            return label
        if lnorm.rsplit("/", 1)[-1] == base:
            return label
    return None


def _conventions_collector(*, demands, resolved, warnings, **_):
    files = resolved["files"]
    if files:
        return [_entry("conventions", demands, "ok", evidence={"files": files})]
    return [_entry("conventions", demands, "unresolved",
                   note="no convention docs apply to the changed files: "
                        "the demanded conventions could not be evidenced")]


def _doc_refs_collector(*, demands, resolved, warnings, **_):
    files = resolved["files"]
    labels = set()
    for text in files.values():
        labels.update(_doc_labels(text))
    entries = []
    for demand in demands:
        name = _demand_name(demand, "doc", "path", "ref", "file")
        label = _match_doc_label(name, labels) if name else None
        if label is not None:
            hits = {path: _doc_block(text, label) for path, text in files.items()
                    if label in _doc_labels(text)}
            entries.append(_entry("doc_refs", demand, "ok",
                                  evidence={"doc": label, "files": hits}))
        elif not files:
            entries.append(_entry("doc_refs", demand, "unresolved",
                                  note="no convention docs apply to the changed "
                                       "files; demanded doc '%s' could not be "
                                       "evidenced" % (name or demand)))
        else:
            entries.append(_entry("doc_refs", demand, "unresolved",
                                  note="no resolved convention doc matched "
                                       "demanded doc '%s'" % (name or demand)))
    return entries


# ---------------------------------------------------------------------------
# symbol_callers -> ocr_impact.find_references under the context allowlist
# ---------------------------------------------------------------------------

def _defs_in_changed_files(repo_root, changed_files):
    """{name: path} of symbols defined in the changed files, from the working
    tree. Lets a bare-name demand behave like a changed symbol: its
    definition is never reported as its own caller."""
    defs = {}
    for rel in changed_files:
        full = os.path.join(repo_root, rel.replace("/", os.sep))
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        ext = os.path.splitext(rel)[1].lower()
        lang = ocr_impact._EXT_LANG.get(ext)
        extracted = ocr_impact._ast_defs(text, rel) if lang == "python" else []
        if not extracted and text.strip() and lang:
            extracted = ocr_impact._regex_defs(text, lang)
        for d in extracted:
            name = d.get("name")
            if name and name not in defs:
                defs[name] = rel
    return defs


def _symbol_callers_collector(*, repo_root, demands, is_context_allowed,
                              tip, run, warnings, changed_files, **_):
    run = run or ocr_impact.git_runner(repo_root)
    local_defs = _defs_in_changed_files(repo_root, changed_files)
    parsed, symbols, seen = [], [], set()
    for demand in demands:
        name = _demand_name(demand, "name", "symbol", "function", "method")
        defined_in = ""
        if isinstance(demand, dict):
            defined_in = str(demand.get("defined_in") or demand.get("file") or "")
        if not defined_in:
            defined_in = local_defs.get(name, "")
        parsed.append((demand, name, defined_in))
        key = (name, defined_in)
        if name and ocr_impact._valid_ident(name) and key not in seen:
            seen.add(key)
            # A demanded symbol is searched with a searchable change class;
            # "signature" guarantees find_references takes it, and an empty
            # defined_in simply skips tier-1 importer discovery.
            symbols.append({"name": name, "qualname": name,
                            "defined_in": defined_in, "kind": "function",
                            "change": "signature", "lines_changed": 1})
    refs = ocr_impact.find_references(run, tip, symbols,
                                      is_context_allowed=is_context_allowed,
                                      max_sites=_MAX_SITES)
    warnings.extend(refs.get("warnings") or [])
    site_bundle = ocr_impact.build_bundle(run, tip, symbols,
                                          refs["sites"], refs["ref_counts"])
    sites_by_name = {}
    for site in site_bundle["sites"]:
        sites_by_name.setdefault(site["name"], []).append(site)
    dropped = set(site_bundle.get("dropped_symbols") or [])
    entries = []
    for demand, name, defined_in in parsed:
        if not name or not ocr_impact._valid_ident(name):
            entries.append(_entry("symbol_callers", demand, "unresolved",
                                  note="demanded symbol %r is not a searchable "
                                       "identifier" % (name or demand)))
            continue
        sites = sites_by_name.get(name, [])
        if not sites:
            entries.append(_entry("symbol_callers", demand, "unresolved",
                                  note="no references to '%s' found outside its "
                                       "definition under the context allowlist" % name))
            continue
        entries.append(_entry("symbol_callers", demand, "ok", evidence={
            "name": name,
            "defined_in": defined_in,
            "ref_counts": {name: refs["ref_counts"].get(name, 0)},
            "sites": sites,
            "truncated": bool(site_bundle.get("truncated")) or name in dropped,
        }))
    return entries


# ---------------------------------------------------------------------------
# event_couples / prose_pointers -> the t06 event index (optional, fail-soft)
# ---------------------------------------------------------------------------

def _index_unavailable(kind, demands, warnings):
    warnings.append("collectors: event index unavailable; %d %s demand(s) "
                    "recorded as unavailable (the index is optional; build it "
                    "with ocr_index.ensure_built)" % (len(demands), kind))
    return _unavailable_entries(
        kind, demands, "event index unavailable (not built, corrupt, or "
                       "version-mismatched); this demand could not be collected")


def _event_couples_collector(*, repo_root, demands, warnings, **_):
    if not ocr_index.is_available(repo_root):
        return _index_unavailable("event_couples", demands, warnings)
    names = [(d, _demand_name(d, "event", "name", "tag")) for d in demands]
    result = ocr_index.query_event_couples(repo_root, [n for _, n in names if n])
    warnings.extend(result.get("warnings") or [])
    couples_by_event = {}
    for couple in result["couples"]:
        couples_by_event.setdefault(couple["event"], []).append(couple)
    entries = []
    for demand, name in names:
        event = result["events"].get(name)
        if not event:
            entries.append(_entry("event_couples", demand, "unresolved",
                                  note="no producer or consumer utterances of "
                                       "'%s' in the event index" % (name or demand)))
            continue
        entries.append(_entry("event_couples", demand, "ok", evidence={
            "event": name,
            "producers": event["producers"],
            "consumers": event["consumers"],
            "couples": couples_by_event.get(name, []),
        }))
    return entries


def _pointer_matches(demand_name, target):
    """Loose name match: basename (extension-insensitive) substring either way,
    so 'weekly-2026-11', 'weekly-2026-11.md' and 'out/weekly-2026-11.md' all
    name the same utterance target."""
    norm = demand_name.replace("\\", "/").strip().lower()
    if not norm:
        return False
    dbase = norm.rsplit("/", 1)[-1]
    dstem = os.path.splitext(dbase)[0]
    tbase = target.replace("\\", "/").rsplit("/", 1)[-1].lower()
    tstem = os.path.splitext(tbase)[0]
    if not dstem or not tstem:
        return False
    return dbase in tbase or dstem in tstem or tstem in dstem


def _prose_pointers_collector(*, repo_root, demands, pointer_resolver, warnings, **_):
    if not ocr_index.is_available(repo_root):
        return _index_unavailable("prose_pointers", demands, warnings)
    resolve = pointer_resolver or _deferred_resolution
    result = ocr_index.query_pointer_utterances(repo_root)
    warnings.extend(result.get("warnings") or [])
    entries = []
    for demand in demands:
        name = _demand_name(demand, "target", "pointer", "ref", "path")
        matches = [p for p in result["pointers"]
                   if name and _pointer_matches(name, p.get("target") or "")]
        if not matches:
            entries.append(_entry("prose_pointers", demand, "unresolved",
                                  note="no pointer utterances matched '%s' in "
                                       "the event index" % (name or demand)))
            continue
        pointers = []
        for p in matches:
            try:
                resolution = resolve(p["target"])
            except Exception as exc:
                warnings.append("collectors: pointer resolver failed for %s (%s); "
                                "resolution deferred" % (p.get("target"), exc))
                resolution = _deferred_resolution(p["target"])
            pointers.append(dict(p, resolution=resolution))
        evidence = {"pointers": pointers}
        entries.append(_entry("prose_pointers", demand, "ok", evidence=evidence))
    return entries


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def collect(repo_root, demands, changed_files, is_context_allowed=None,
            *, tip="HEAD", run=None, max_chars=MAX_BUNDLE_CHARS,
            pointer_resolver=None):
    """Map validated spotter demands to a bounded evidence bundle. Fail-soft.

    ``demands`` is the validated demands object (or None when the spotter
    degraded to T0 -- an empty bundle is returned, no error).
    ``changed_files`` are the push's repo-relative paths (conventions/docs are
    resolved against them). ``is_context_allowed`` is the t01 context filter
    (review-gate passes ``_is_context_allowed_path``); when omitted, the
    standalone default above applies.

    ``pointer_resolver`` is the t09 seam: a callable ``target -> resolution
    dict`` for prose-pointer utterances. Deferred by default (t09 slots in by
    passing a real resolver; this signature does not change).

    Returns {"bundle": {...}, "warnings": [...]}. Never raises.
    """
    warnings = []
    try:
        repo_root = os.path.abspath(str(repo_root))
        changed_files = [str(p).replace("\\", "/") for p in (changed_files or [])]
        is_context_allowed = is_context_allowed or default_is_context_allowed
        entries = []

        if not isinstance(demands, dict):
            bundle = {"version": BUNDLE_VERSION, "entries": [],
                      "truncated": False, "total_chars": 0}
            return {"bundle": bundle, "warnings": warnings}

        # conventions and doc_refs both draw on a single t03 resolver pass
        # over the changed files (imported in-process, never subprocessed).
        resolved = {"files": {}, "warnings": []}
        if demands.get("conventions") or demands.get("doc_refs"):
            resolved = ocr_conventions.resolve_conventions(repo_root, changed_files)
            warnings.extend(resolved.get("warnings") or [])

        for kind in DEMAND_KEYS:
            demand_list = demands.get(kind) or []
            if not isinstance(demand_list, list) or not demand_list:
                continue
            collector = _KIND_COLLECTORS.get(kind)
            if collector is None:
                continue
            try:
                kind_entries = collector(
                    repo_root=repo_root, demands=demand_list,
                    changed_files=changed_files,
                    is_context_allowed=is_context_allowed,
                    tip=tip, run=run, pointer_resolver=pointer_resolver,
                    resolved=resolved, warnings=warnings)
                entries.extend(kind_entries)
            except Exception as exc:
                warnings.append("collectors: %s collector failed (%s); demand(s) "
                                "recorded as unavailable" % (kind, exc))
                entries.extend(_unavailable_entries(
                    kind, demand_list, "collector failed: %s" % exc))

        counters = {}
        for entry in entries:
            n = counters.get(entry["kind"], 0)
            counters[entry["kind"]] = n + 1
            entry["id"] = "%s[%d]" % (entry["kind"], n)

        bundle = {"version": BUNDLE_VERSION, "entries": entries,
                  "truncated": False, "total_chars": 0}
        _enforce_cap(bundle, max_chars, warnings)
        bundle["total_chars"] = _json_chars(bundle["entries"])

        # ADR 0001: the shape that leaves this module is always valid.
        check = validate_bundle(bundle)
        if not check["ok"]:
            warnings.append("collectors: internal bundle failed validation (%s); "
                            "review proceeds without it (T0 fallback)"
                            % "; ".join(check["errors"][:3]))
            bundle = {"version": BUNDLE_VERSION, "entries": [],
                      "truncated": False, "total_chars": 0}
        return {"bundle": bundle, "warnings": warnings}
    except Exception as exc:  # fail-soft: never break the review
        warnings.append("collectors: collect failed (%s); review proceeds "
                        "without an evidence bundle" % exc)
        bundle = {"version": BUNDLE_VERSION, "entries": [],
                  "truncated": False, "total_chars": 0}
        return {"bundle": bundle, "warnings": warnings}


_KIND_COLLECTORS = {
    "symbol_callers": _symbol_callers_collector,
    "event_couples": _event_couples_collector,
    "conventions": _conventions_collector,
    "doc_refs": _doc_refs_collector,
    "prose_pointers": _prose_pointers_collector,
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main(argv=None):
    parser = argparse.ArgumentParser(
        description="Map spotter demands to a bounded evidence bundle (stdin -> stdout).")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--changed-file", dest="changed_files", action="append",
                        default=[], help="repo-relative changed path (repeatable)")
    parser.add_argument("--tip", default="HEAD")
    parser.add_argument("--max-chars", type=int, default=MAX_BUNDLE_CHARS)
    parser.add_argument("--validate-bundle", action="store_true",
                        help="validate a bundle JSON on stdin instead of collecting")
    args = parser.parse_args(argv)
    if args.validate_bundle:
        try:
            result = validate_bundle(json.load(sys.stdin))
        except ValueError as exc:
            result = {"ok": False, "errors": ["not valid JSON: %s" % exc]}
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0 if result["ok"] else 1
    try:
        data = json.load(sys.stdin)
    except ValueError as exc:
        data = None
        pre_warn = ["collectors: demands on stdin were not valid JSON (%s); "
                    "empty bundle" % exc]
    else:
        pre_warn = []
    # Accept either the demands object or the validator's wrapper output.
    if isinstance(data, dict) and "demands" in data and \
            not any(k in data for k in DEMAND_KEYS):
        data = data["demands"]
    result = collect(args.repo_root, data, args.changed_files,
                     tip=args.tip, max_chars=args.max_chars)
    result["warnings"] = pre_warn + result["warnings"]
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
