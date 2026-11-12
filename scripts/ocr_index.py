#!/usr/bin/env python3
#
# Deterministic event-graph index for review-gate v2 (t06).
#
# A lazy, incrementally refreshed index of the target repo with three edge
# kinds:
#   1. symbol defs/refs  — definitions extracted per file (reusing
#      ocr_impact._ast_defs / ocr_impact._regex_defs; tree-sitter for TS/JS
#      when importable) plus an identifier-occurrence index so callers can be
#      found without a git grep per symbol.
#   2. event couplings   — emit/dispatch/on/subscribe call patterns and
#      framework tag strings (e.g. Lexical's editor.update(..., {tag: 'x'})
#      producer paired with a consumer's tags.has('x') filter — the
#      undo/resurrection miss class). Pattern sets below are a documented
#      STARTER SET; extend _EVENT_PATTERNS to cover more frameworks.
#   3. pointer utterances — string literals referencing report/doc-like names
#      (*.md, *.pdf, *.json). Kept conservative on purpose: better to miss a
#      pointer than to drown collectors in string noise.
#
# Storage: <repo>/.ocr/index/index.json — UNTRACKED runtime state. Do not
# commit it, add it to nothing; it is rebuilt lazily on first push and
# refreshed incrementally from each push's changed-file list. The gate wires
# this in review-gate.py::_refresh_index.
#
# Tree-sitter is optional: if `tree_sitter` + a TS/JS grammar are importable
# they give precise TS/JS extraction; otherwise the documented-weaker regex
# extraction is used and capability_warnings() names what is missing
# (spec "Event-coupling index" / user story 19). Set OCR_INDEX_TREE_SITTER=0
# to force the fallback.
#
# Python 3.9+, no dependencies beyond the stdlib and ocr_impact.
#
# Public contract (mirrors ocr_impact): every public function is fail-soft,
# returns a JSON-serializable dict with a "warnings" list, and never raises.
#
import bisect
import hashlib
import json
import os
import re
from typing import Callable, Dict, List, Optional

import ocr_impact as _impact

_INDEX_VERSION = 1
_INDEX_DIR = os.path.join(".ocr", "index")
_INDEX_FILE = "index.json"

# Extensions this index extracts edges from. Targets of pointer utterances
# (.md/.pdf/.json) are NOT indexed as sources — they are not code.
_INDEX_EXTS = frozenset({".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"})

# Caps — every size is bounded so a monstrous repo degrades with a warning
# instead of a hang.
_MAX_FILE_BYTES = 1_000_000
_MAX_FILES = 20_000
_MAX_IDENTS_PER_FILE = 400
_MAX_LINES_PER_IDENT = 20
_MAX_QUERY_RESULTS = 200

# Always skipped while walking, regardless of the caller's is_allowed filter:
# VCS internals, our own state dir, and dependency caches (a custom filter
# like review-gate's is not required to know about .ocr).
_WALK_SKIP_DIRS = frozenset({".git", ".ocr", "node_modules", "__pycache__"})

# Default context filter (used when the caller does not pass is_allowed, e.g.
# standalone use). Mirrors review-gate.py::_is_context_allowed_path in
# spirit: test-family dirs ARE indexed (a caller in a spec file is exactly
# where coupling must look), vendored/generated/dependency trees are not.
# review-gate passes its own _is_context_allowed_path, which is authoritative.
_DEFAULT_SKIP_DIRS = frozenset({
    ".git", ".idea", ".vscode", ".next", ".ocr",
    "vendor", "node_modules", "dist", "build", "out", "target",
    "__generated__", "__pycache__",
})
_EXCLUDED_SUFFIX_RE = re.compile(r"(\.min\.js|\.generated\.[^/\\]+)$", re.IGNORECASE)

# Identifier occurrences not worth indexing for ref lookup.
_STOPWORDS = frozenset({
    # python
    "and", "as", "assert", "async", "await", "break", "class", "continue",
    "def", "del", "elif", "else", "except", "finally", "for", "from",
    "global", "if", "import", "in", "is", "lambda", "nonlocal", "not",
    "or", "pass", "raise", "return", "try", "while", "with", "yield",
    "true", "false", "none", "self",
    # js/ts
    "var", "let", "const", "function", "new", "delete", "typeof",
    "instanceof", "of", "case", "default", "switch", "do", "this",
    "super", "extends", "static", "get", "set", "export", "void",
    "null", "undefined", "interface", "type", "enum", "implements",
})
_IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")

# ---------------------------------------------------------------------------
# Edge-kind pattern sets (documented starter set — extend here)
# ---------------------------------------------------------------------------

# (kind, direction, compiled regex with exactly one capture group = event name)
_EVENT_PATTERNS = {
    "js_ts": [
        ("emit-call", "emit",
         re.compile(r"\.(?:emit|dispatch|publish)\(\s*['\"]([^'\"]+)")),
        ("custom-event", "emit",
         re.compile(r"new\s+CustomEvent\(\s*['\"]([^'\"]+)")),
        # Lexical-style update tags: editor.update(fn, {tag: 'x'})
        ("update-tag", "emit",
         re.compile(r"\btag\s*:\s*['\"]([^'\"]+)")),
        ("on-call", "listen",
         re.compile(r"\.(?:on|once|subscribe|addEventListener|addListener)"
                    r"\(\s*['\"]([^'\"]+)")),
        # Lexical-style filter consumers: if (!tags.has('x')) return
        ("tag-filter", "listen",
         re.compile(r"\btags\.has\(\s*['\"]([^'\"]+)")),
    ],
    "python": [
        ("emit-call", "emit",
         re.compile(r"\.(?:emit|dispatch|publish|send)\(\s*['\"]([^'\"]+)")),
        ("on-call", "listen",
         re.compile(r"\.(?:on|subscribe|connect|listen|add_listener)"
                    r"\(\s*['\"]([^'\"]+)")),
    ],
}

_STRING_RES = (
    re.compile(r'"([^"\n]{4,300})"'),
    re.compile(r"'([^'\n]{4,300})'"),
    re.compile(r"`([^`\n]{4,300})`"),
)
_POINTER_EXTS = {".md": "md", ".pdf": "pdf", ".json": "json"}
# Config files that end in a pointer extension but are not report references.
_POINTER_DENY_BASENAMES = frozenset({
    "package.json", "package-lock.json", "tsconfig.json", "jsconfig.json",
    "composer.json", "composer.lock",
})


# ---------------------------------------------------------------------------
# Optional tree-sitter
# ---------------------------------------------------------------------------

def _import_tree_sitter():
    """Import tree_sitter + a TS/JS grammar, or return None when unavailable."""
    try:
        import tree_sitter  # noqa: F401
    except Exception:
        return None
    grammar = None
    for modname in ("tree_sitter_typescript", "tree_sitter_javascript"):
        try:
            grammar = __import__(modname)
            break
        except Exception:
            continue
    if grammar is None:
        return None
    return {"tree_sitter": tree_sitter, "grammar": grammar}


_TS = _import_tree_sitter()


def using_tree_sitter() -> bool:
    """True when tree-sitter extraction is active for TS/JS."""
    if _TS is None:
        return False
    return os.environ.get("OCR_INDEX_TREE_SITTER", "1").strip().lower() not in (
        "0", "false", "no")


def capability_warnings() -> List[str]:
    """One-line warning naming the precision lost to the regex fallback."""
    if using_tree_sitter():
        return []
    return [
        "ocr_index: tree-sitter unavailable; TS/JS extraction uses the "
        "documented-weaker regex fallback (install tree-sitter and a "
        "TypeScript/JavaScript grammar for precise extraction, or set "
        "OCR_INDEX_TREE_SITTER=1 after installing)"
    ]


def _ts_ast_defs(text: str, path: str) -> List[Dict]:
    """Tree-sitter based TS/JS definitions. Raises on any failure — the
    caller falls back to regex per file."""
    ts = _TS["tree_sitter"]
    grammar = _TS["grammar"]
    lang_obj = None
    for attr in ("language_typescript", "language_tsx", "language"):
        fn = getattr(grammar, attr, None)
        if fn is not None:
            lang_obj = fn()
            break
    if lang_obj is None:
        raise RuntimeError("no grammar language() entry point")
    language = ts.Language(lang_obj)
    parser = ts.Parser(language)
    tree = parser.parse(text.encode("utf-8"))
    defs = []
    wanted = {
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
    }
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        ntype = wanted.get(node.type)
        name_node = None
        if ntype is not None:
            name_node = node.child_by_field_name("name")
        elif node.type == "variable_declarator":
            name_node = node.child_by_field_name("name")
        if name_node is not None:
            kind = ntype or "function"
            name = name_node.text.decode("utf-8", errors="replace")
            if _impact._valid_ident(name):
                defs.append({
                    "name": name,
                    "qualname": name,
                    "kind": kind,
                    "line": name_node.start_point[0] + 1,
                })
        stack.extend(reversed(node.children))
    return defs


# ---------------------------------------------------------------------------
# Default context filter
# ---------------------------------------------------------------------------

def _default_is_allowed(path: str) -> bool:
    """Context-filter fallback for standalone use; review-gate passes its own
    _is_context_allowed_path, which governs when wired in."""
    norm = path.replace("\\", "/")
    parts = norm.split("/")
    if any(p.lower() in _DEFAULT_SKIP_DIRS for p in parts[:-1]):
        return False
    if _EXCLUDED_SUFFIX_RE.search(parts[-1]):
        return False
    return True


# ---------------------------------------------------------------------------
# Per-file extraction
# ---------------------------------------------------------------------------

def _defs_for(text: str, relpath: str, lang: str) -> List[Dict]:
    if lang == "python":
        defs = _impact._ast_defs(text, relpath)
        if not defs and text.strip():
            defs = _impact._regex_defs(text, lang)
    else:
        defs = []
        if using_tree_sitter():
            try:
                defs = _ts_ast_defs(text, relpath)
            except Exception:
                defs = []
        if not defs:
            defs = _impact._regex_defs(text, lang)
    out = []
    for d in defs:
        name = d.get("name")
        if not name or not _impact._valid_ident(name):
            continue
        out.append({
            "name": name,
            "qualname": d.get("qualname") or name,
            "kind": d.get("kind") or "unknown",
            "line": d.get("line") or 0,
        })
    return out


def _refs_for(text: str) -> Dict[str, List[int]]:
    starts = [0]
    for m in re.finditer("\n", text):
        starts.append(m.end())
    refs: Dict[str, List[int]] = {}
    truncated_overflow = False
    for m in _IDENT_RE.finditer(text):
        name = m.group(0)
        if len(name) < 3 or name.lower() in _STOPWORDS:
            continue
        entry = refs.get(name)
        if entry is None:
            if len(refs) >= _MAX_IDENTS_PER_FILE:
                truncated_overflow = True
                continue
            entry = refs[name] = []
        line = bisect.bisect_right(starts, m.start())
        if len(entry) < _MAX_LINES_PER_IDENT and (not entry or entry[-1] != line):
            entry.append(line)
    if truncated_overflow:
        refs["__truncated__"] = [1]
    return refs


def _events_for(text: str, lang: str) -> List[Dict]:
    group = "python" if lang == "python" else "js_ts"
    events = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for kind, direction, pat in _EVENT_PATTERNS[group]:
            for m in pat.finditer(line):
                events.append({
                    "name": m.group(1),
                    "direction": direction,
                    "kind": kind,
                    "line": lineno,
                })
    return events


def _pointer_target(value: str) -> Optional[str]:
    value = value.strip()
    if not value or any(ch.isspace() for ch in value):
        return None
    base = value.replace("\\", "/").rsplit("/", 1)[-1]
    if base.startswith(".") or len(base.split(".")[0]) < 1:
        return None
    if base.lower() in _POINTER_DENY_BASENAMES:
        return None
    ext = os.path.splitext(base)[1].lower()
    if ext not in _POINTER_EXTS:
        return None
    return value


def _pointers_for(text: str) -> List[Dict]:
    pointers = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for sre in _STRING_RES:
            for m in sre.finditer(line):
                target = _pointer_target(m.group(1))
                if target is None:
                    continue
                ext = os.path.splitext(target.replace("\\", "/")
                                       .rsplit("/", 1)[-1])[1].lower()
                pointers.append({
                    "target": target,
                    "kind": _POINTER_EXTS[ext],
                    "line": lineno,
                })
    return pointers


def _extract_file(full: str, relpath: str, warnings: List[str]) -> Optional[Dict]:
    try:
        size = os.path.getsize(full)
        if size > _MAX_FILE_BYTES:
            warnings.append(f"ocr_index: {relpath} over {_MAX_FILE_BYTES} bytes; skipped")
            return None
        with open(full, "rb") as f:
            data = f.read()
    except OSError as exc:
        warnings.append(f"ocr_index: cannot read {relpath}: {exc}")
        return None
    text = data.decode("utf-8", errors="replace")
    ext = os.path.splitext(relpath)[1].lower()
    lang = _impact._EXT_LANG.get(ext) or ("python" if ext == ".py" else "js")
    return {
        "size": size,
        "sha1": hashlib.sha1(data).hexdigest(),
        "defs": _defs_for(text, relpath, lang),
        "refs": _refs_for(text),
        "events": _events_for(text, lang),
        "pointers": _pointers_for(text),
    }


# ---------------------------------------------------------------------------
# Index persistence
# ---------------------------------------------------------------------------

def _index_path(repo_root: str) -> str:
    return os.path.join(repo_root, _INDEX_DIR, _INDEX_FILE)


def _load_index(repo_root: str):
    """Return (index dict or None, warnings). Never raises."""
    warnings = []
    path = _index_path(repo_root)
    if not os.path.isfile(path):
        return None, [f"ocr_index: index not built for {repo_root}"]
    try:
        with open(path, encoding="utf-8") as f:
            index = json.load(f)
    except (OSError, ValueError) as exc:
        warnings.append(f"ocr_index: corrupt index at {path}: {exc}")
        return None, warnings
    if index.get("version") != _INDEX_VERSION:
        warnings.append("ocr_index: index version mismatch; rebuild required")
        return None, warnings
    return index, warnings


def _write_index(repo_root: str, index: Dict) -> None:
    d = os.path.join(repo_root, _INDEX_DIR)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, _INDEX_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, sort_keys=True)
    os.replace(tmp, os.path.join(d, _INDEX_FILE))


def _iter_source_files(repo_root: str, is_allowed: Callable[[str], bool]):
    for dirpath, dirnames, filenames in os.walk(repo_root, followlinks=False):
        dirnames[:] = sorted(
            d for d in dirnames if d not in _WALK_SKIP_DIRS)
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() not in _INDEX_EXTS:
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, repo_root).replace(os.sep, "/")
            if is_allowed(rel):
                yield full, rel


# ---------------------------------------------------------------------------
# Public API — build / refresh
# ---------------------------------------------------------------------------

def ensure_built(repo_root: str, is_allowed: Callable[[str], bool] = None) -> Dict:
    """Build the index on first use; cheap no-op afterwards.

    Returns {"built": bool, "indexed": int, "path": str, "tool": str,
    "warnings": [...]}. Lazy: an existing, version-matching index is left
    alone and returned as built=False.
    """
    warnings: List[str] = []
    try:
        repo_root = os.path.abspath(repo_root)
        existing, load_warnings = _load_index(repo_root)
        if existing is not None:
            return {
                "built": False,
                "indexed": len(existing.get("files") or {}),
                "path": _index_path(repo_root),
                "tool": "tree-sitter" if using_tree_sitter() else "regex",
                "warnings": [],
            }
        is_allowed = is_allowed or _default_is_allowed
        files: Dict[str, Dict] = {}
        capped = False
        for full, rel in _iter_source_files(repo_root, is_allowed):
            if len(files) >= _MAX_FILES:
                capped = True
                break
            record = _extract_file(full, rel, warnings)
            if record is not None:
                files[rel] = record
        if capped:
            warnings.append(
                f"ocr_index: file cap {_MAX_FILES} reached; index is partial")
        if not using_tree_sitter() and any(
                os.path.splitext(p)[1].lower() in (".js", ".jsx", ".mjs", ".cjs",
                                                   ".ts", ".tsx")
                for p in files):
            warnings += capability_warnings()
        index = {
            "version": _INDEX_VERSION,
            "tool": "tree-sitter" if using_tree_sitter() else "regex",
            "files": files,
        }
        _write_index(repo_root, index)
        return {
            "built": True,
            "indexed": len(files),
            "path": _index_path(repo_root),
            "tool": index["tool"],
            "warnings": warnings,
        }
    except Exception as exc:  # fail-soft contract
        warnings.append(f"ocr_index: ensure_built failed: {exc}")
        return {"built": False, "indexed": 0, "path": _index_path(repo_root),
                "tool": "unknown", "warnings": warnings}


def _norm_rel(path) -> str:
    """Repo-relative normalize: forward slashes, one leading './' removed.

    NOT str.lstrip('./'): that strips every leading '.' and '/', mangling
    root-level dotfiles ('.eslintrc.js' -> 'eslintrc.js')."""
    rel = str(path).replace("\\", "/")
    return rel[2:] if rel.startswith("./") else rel


def refresh(repo_root: str, changed_files, is_allowed: Callable[[str], bool] = None) -> Dict:
    """Incrementally re-extract the given repo-relative changed files.

    Files that vanished (or are no longer context-allowed / supported) have
    their edges dropped. If the index does not exist yet, it is built lazily
    instead ({"built": True}). Never raises.
    """
    warnings: List[str] = []
    try:
        repo_root = os.path.abspath(repo_root)
        index, load_warnings = _load_index(repo_root)
        if index is None:
            built = ensure_built(repo_root, is_allowed=is_allowed)
            return {
                "built": built.get("built", False),
                "refreshed": [],
                "removed": [],
                "warnings": list(built.get("warnings") or []) or load_warnings,
            }
        is_allowed = is_allowed or _default_is_allowed
        files = index.setdefault("files", {})
        refreshed, removed = [], []
        for rel in sorted({_norm_rel(p) for p in (changed_files or [])}):
            if not rel:
                continue
            full = os.path.join(repo_root, rel.replace("/", os.sep))
            ext = os.path.splitext(rel)[1].lower()
            if os.path.isfile(full) and ext in _INDEX_EXTS and is_allowed(rel):
                record = _extract_file(full, rel, warnings)
                if record is not None:
                    files[rel] = record
                    refreshed.append(rel)
                    continue
            if rel in files:
                del files[rel]
                removed.append(rel)
        _write_index(repo_root, index)
        return {"built": False, "refreshed": refreshed, "removed": removed,
                "warnings": warnings}
    except Exception as exc:
        warnings.append(f"ocr_index: refresh failed: {exc}")
        return {"built": False, "refreshed": [], "removed": [], "warnings": warnings}


# ---------------------------------------------------------------------------
# Public API — availability probe + queries (read-only; never trigger a build)
# ---------------------------------------------------------------------------

def is_available(repo_root: str) -> bool:
    """True when a version-matching index exists and parses (cheap probe).

    Callers (the t05 collectors) use this to turn "no index" into explicit
    ``unavailable`` evidence instead of silent empty query answers."""
    try:
        index, _ = _load_index(os.path.abspath(str(repo_root)))
        return index is not None
    except Exception:
        return False

def query_symbol_refs(repo_root: str, name: str) -> Dict:
    """Definitions and identifier-occurrence references for one symbol.

    {"name", "defs": [{path, line, kind, qualname}],
     "refs": [{path, line}], "warnings": [...]} — def sites excluded from refs.
    """
    warnings: List[str] = []
    try:
        index, warnings = _load_index(repo_root)
        defs_out, refs_out = [], []
        if index is not None and name:
            def_sites = set()
            for path in sorted(index.get("files") or {}):
                rec = index["files"][path]
                for d in rec.get("defs") or []:
                    if d.get("name") == name:
                        def_sites.add((path, d.get("line")))
                        defs_out.append({
                            "path": path,
                            "line": d.get("line"),
                            "kind": d.get("kind"),
                            "qualname": d.get("qualname"),
                        })
            for path in sorted(index.get("files") or {}):
                for line in (index["files"][path].get("refs") or {}).get(name, []):
                    if (path, line) in def_sites:
                        continue
                    if len(refs_out) >= _MAX_QUERY_RESULTS:
                        warnings.append(
                            f"ocr_index: ref cap {_MAX_QUERY_RESULTS} hit for {name}")
                        break
                    refs_out.append({"path": path, "line": line})
        return {"name": name, "defs": defs_out, "refs": refs_out,
                "warnings": warnings}
    except Exception as exc:
        warnings.append(f"ocr_index: query_symbol_refs failed: {exc}")
        return {"name": name, "defs": [], "refs": [], "warnings": warnings}


def query_event_couples(repo_root: str, names) -> Dict:
    """Producer↔consumer couples for the given event/tag names.

    {"events": {name: {"producers": [...], "consumers": [...]}},
     "couples": [{event, producer: {path, line, kind},
                  consumer: {path, line, kind}}],
     "missing": [names with no utterances], "warnings": [...]}
    """
    warnings: List[str] = []
    try:
        index, warnings = _load_index(repo_root)
        events_out: Dict[str, Dict] = {}
        couples, missing = [], []
        wanted = [n for n in (names or []) if n]
        if index is not None:
            files = index.get("files") or {}
            for name in wanted:
                producers, consumers = [], []
                for path in sorted(files):
                    for ev in files[path].get("events") or []:
                        if ev.get("name") != name:
                            continue
                        entry = {"path": path, "line": ev.get("line"),
                                 "kind": ev.get("kind"),
                                 "direction": ev.get("direction")}
                        if ev.get("direction") == "emit":
                            producers.append(entry)
                        else:
                            consumers.append(entry)
                if not producers and not consumers:
                    missing.append(name)
                    continue
                events_out[name] = {"producers": producers,
                                    "consumers": consumers}
                for p in producers:
                    if len(couples) >= _MAX_QUERY_RESULTS:
                        break
                    for c in consumers:
                        if len(couples) >= _MAX_QUERY_RESULTS:
                            warnings.append(
                                f"ocr_index: couple cap {_MAX_QUERY_RESULTS} hit")
                            break
                        couples.append({
                            "event": name,
                            "producer": {k: p[k] for k in ("path", "line", "kind")},
                            "consumer": {k: c[k] for k in ("path", "line", "kind")},
                        })
        return {"events": events_out, "couples": couples, "missing": missing,
                "warnings": warnings}
    except Exception as exc:
        warnings.append(f"ocr_index: query_event_couples failed: {exc}")
        return {"events": {}, "couples": [], "missing": [], "warnings": warnings}


def query_pointer_utterances(repo_root: str) -> Dict:
    """All string literals referencing report/doc-like names.

    {"pointers": [{path, line, target, kind}], "warnings": [...]}
    """
    warnings: List[str] = []
    try:
        index, warnings = _load_index(repo_root)
        pointers = []
        if index is not None:
            for path in sorted(index.get("files") or {}):
                for p in index["files"][path].get("pointers") or []:
                    if len(pointers) >= _MAX_QUERY_RESULTS:
                        warnings.append(
                            f"ocr_index: pointer cap {_MAX_QUERY_RESULTS} hit")
                        break
                    pointers.append({
                        "path": path,
                        "line": p.get("line"),
                        "target": p.get("target"),
                        "kind": p.get("kind"),
                    })
        return {"pointers": pointers, "warnings": warnings}
    except Exception as exc:
        warnings.append(f"ocr_index: query_pointer_utterances failed: {exc}")
        return {"pointers": [], "warnings": warnings}
