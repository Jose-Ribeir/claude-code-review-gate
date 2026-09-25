#!/usr/bin/env python3
#
# Deterministic impact analysis for review-gate.
#
# Given what a push changed, find the functions it changed and where they are
# called elsewhere, so the reviewer can be shown those call sites.  Also a
# "sibling sweep": find other places with the same buggy code as a finding.
#
# Everything this module reads (file contents, names, paths) comes from an
# untrusted branch: we never shell out in a way that could be injected, we
# always pass git args as a list, put `--` before pathspecs, use -F fixed-string
# with -e per pattern, validate symbol names, and cap every size.
#
# Public contract: every exported function is best-effort.  On any exception it
# returns an empty/neutral result plus a non-empty warnings list and never raises
# — the caller keeps working exactly as before if this module fails.
#
import ast
import difflib
import hashlib
import os
import re
import subprocess
import sys
from typing import Callable, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Identifier safety
# ---------------------------------------------------------------------------

# Used to validate symbol names before they are passed to git grep -F -e.
# Anything that doesn't look like an identifier is silently skipped.
_IDENT_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


def _uses_name(name, text):
    """`name` as a whole identifier in `text` -- git grep -w found the line,
    but with several names per search a substring test would credit `get`
    with every `get_user` hit."""
    return re.search(r"(?<![A-Za-z0-9_$])" + re.escape(name) + r"(?![A-Za-z0-9_$])",
                     text) is not None


def _valid_ident(name: str) -> bool:
    return bool(_IDENT_RE.match(name))


# ---------------------------------------------------------------------------
# 1. git_runner
# ---------------------------------------------------------------------------

def git_runner(cwd: str) -> Callable[[List[str]], Tuple[str, int]]:
    """Return a callable run(args) -> (stdout, rc) that runs git with the given cwd.

    The callable never raises: exceptions become rc=1 with empty stdout.
    Leading whitespace on lines is preserved (snippets need indentation);
    only a single trailing newline is stripped.
    """
    # Paths passed as pathspecs come from the untrusted tree; a file named
    # ":(exclude)*" must be matched as a name, not parsed as pathspec magic.
    env = dict(os.environ, GIT_LITERAL_PATHSPECS="1")

    def run(args: List[str]) -> Tuple[str, int]:
        try:
            result = subprocess.run(
                ["git"] + args,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                env=env,
                # Called from the detached review supervisor, which has no
                # console: without this each git call opens its own window.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            # Strip only a single trailing newline so indentation is intact.
            stdout = result.stdout
            if stdout.endswith("\n"):
                stdout = stdout[:-1]
            return stdout, result.returncode
        except Exception:
            return "", 1

    return run


# ---------------------------------------------------------------------------
# Internal helpers shared by changed_symbols and find_references
# ---------------------------------------------------------------------------

def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


# ---------------------------------------------------------------------------
# 2. changed_symbols
# ---------------------------------------------------------------------------

# Supported extensions mapped to their language family.
_EXT_LANG = {
    ".py": "python", ".pyi": "python",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
    ".ts": "ts", ".tsx": "ts",
    ".go": "go",
    ".rs": "rust",
    ".sh": "shell", ".bash": "shell",
    ".ps1": "powershell", ".psm1": "powershell",
    ".java": "jvm", ".kt": "jvm", ".kts": "jvm", ".cs": "jvm",
    ".rb": "ruby",
    ".php": "php",
}

# Keywords that must not be mistaken for function names in brace languages.
_BRACE_KEYWORDS = frozenset({
    "if", "else", "for", "while", "switch", "catch", "try", "finally",
    "do", "return", "break", "continue", "throw", "new", "delete", "typeof",
    "instanceof", "in", "of", "case", "default", "yield", "await", "async",
})


def _norm_sig(sig: str) -> str:
    """Normalize a signature line: collapse whitespace."""
    return " ".join(sig.split())


def _dotted_module(path: str) -> str:
    """Convert a file path to a dotted Python module name (best-effort)."""
    # Remove extension
    p = re.sub(r"\.(py|pyi)$", "", path, flags=re.IGNORECASE)
    # Normalize slashes
    p = p.replace("\\", "/").lstrip("/")
    return p.replace("/", ".")


def _module_stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


# --- Python AST-based definition extraction ---

def _ast_defs(text: str, path: str) -> List[Dict]:
    """Return list of {name, qualname, kind, line, end_line, sig} from Python AST."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []

    lines = text.splitlines()

    def _sig(node):
        # First physical line of the definition
        lineno = node.lineno - 1  # 0-based
        if 0 <= lineno < len(lines):
            return lines[lineno]
        return ""

    defs = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            # Class itself
            defs.append({
                "name": node.name,
                "qualname": node.name,
                "kind": "class",
                "line": node.lineno,
                "end_line": node.end_lineno,
                "sig": _sig(node),
                "_class": node.name,
            })
            # Direct methods
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defs.append({
                        "name": item.name,
                        "qualname": f"{node.name}.{item.name}",
                        "kind": "method",
                        "line": item.lineno,
                        "end_line": item.end_lineno,
                        "sig": _sig(item),
                        "_class": node.name,
                    })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Only top-level functions (not nested inside another function/method)
            # We handle nesting by walking to the outermost below.
            pass

    # Collect top-level functions (those whose parent is the Module)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.append({
                "name": node.name,
                "qualname": node.name,
                "kind": "function",
                "line": node.lineno,
                "end_line": node.end_lineno,
                "sig": _sig(node),
                "_class": None,
            })
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            # Top-level name = lambda ...
            if isinstance(node, ast.Assign):
                if isinstance(node.value, ast.Lambda) and len(node.targets) == 1:
                    t = node.targets[0]
                    if isinstance(t, ast.Name):
                        defs.append({
                            "name": t.id,
                            "qualname": t.id,
                            "kind": "variable",
                            "line": node.lineno,
                            "end_line": node.end_lineno if hasattr(node, "end_lineno") else node.lineno,
                            "sig": _sig(node),
                            "_class": None,
                        })
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.value, ast.Lambda) and isinstance(node.target, ast.Name):
                    defs.append({
                        "name": node.target.id,
                        "qualname": node.target.id,
                        "kind": "variable",
                        "line": node.lineno,
                        "end_line": node.end_lineno if hasattr(node, "end_lineno") else node.lineno,
                        "sig": _sig(node),
                        "_class": None,
                    })

    return defs


def _find_enclosing_ast(defs: List[Dict], lineno: int) -> Optional[Dict]:
    """Given 1-based lineno, return the outermost enclosing callable def.

    For a line inside a method, prefer the method over its containing class
    (the class is not itself callable in the same sense).  For nested functions,
    return the outermost function/method (largest span among function/method
    candidates), per spec: "nested/local functions map to their outermost
    enclosing top-level function or method."
    """
    candidates = [d for d in defs if d["line"] <= lineno <= d["end_line"]]
    if not candidates:
        return None

    # Separate callable (function/method) candidates from structural (class) ones.
    fn_candidates = [d for d in candidates if d.get("kind") in ("function", "method", "variable")]
    if fn_candidates:
        # Outermost = largest span.
        fn_candidates.sort(key=lambda d: d["end_line"] - d["line"], reverse=True)
        return fn_candidates[0]

    # Only class/module-level candidates — pick outermost (largest span).
    candidates.sort(key=lambda d: d["end_line"] - d["line"], reverse=True)
    return candidates[0]


# --- Regex-based definition extraction for non-Python languages ---

def _regex_defs(text: str, lang: str) -> List[Dict]:
    """Return list of {name, qualname, kind, line, end_line, sig} via regex for lang."""
    lines = text.splitlines()
    n = len(lines)
    defs = []

    def add(name, qualname, kind, line_0, sig):
        if not _valid_ident(name):
            return
        defs.append({
            "name": name,
            "qualname": qualname,
            "kind": kind,
            "line": line_0 + 1,  # 1-based
            "end_line": None,     # resolved below
            "sig": sig,
            "_line0": line_0,
        })

    if lang in ("js", "ts"):
        # Track top-level class context and object-literal context for qualnames.
        # We do a simple linear scan; nesting is approximated by indentation.
        _cls_re = re.compile(
            r"^(export\s+)?(default\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)"
        )
        # function declarations
        _fn_re = re.compile(
            r"^(export\s+)?(default\s+)?(async\s+)?function\s*\*?\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\("
        )
        # const/let/var name = [async] function | arrow
        _arrow_re = re.compile(
            r"^(export\s+)?(const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*="
            r"\s*(async\s+)?(function[\s(*]|\(|[A-Za-z_$][A-Za-z0-9_$]*\s*=>)"
        )
        # class method: name(...) { — indented at least 2 spaces, not a keyword
        _method_re = re.compile(
            r"^\s{2,}(async\s+|static\s+|get\s+|set\s+)?"
            r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\([^)]*\)\s*\{"
        )
        # object literal method: name(...) { inside const X = {
        _objlit_re = re.compile(
            r"^(export\s+)?(const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*\{"
        )

        current_class = None
        current_objlit = None  # name of object literal
        current_class_indent = -1

        for i, line in enumerate(lines):
            stripped = line.lstrip()
            indent = len(line) - len(stripped)

            # Track class context
            m = _cls_re.match(stripped)
            if m:
                current_class = m.group(3)
                current_class_indent = indent
                add(current_class, current_class, "class", i, line.strip())
                current_objlit = None
                continue

            # Track object literal
            m = _objlit_re.match(stripped)
            if m:
                current_objlit = m.group(3)
                add(current_objlit, current_objlit, "variable", i, line.strip())
                continue

            # Function declaration
            m = _fn_re.match(stripped)
            if m:
                name = m.group(4)
                add(name, name, "function", i, line.strip())
                current_class = None
                current_objlit = None
                continue

            # Arrow / const
            m = _arrow_re.match(stripped)
            if m:
                name = m.group(3)
                add(name, name, "function", i, line.strip())
                continue

            # Class method
            if current_class and indent > current_class_indent:
                m = _method_re.match(line)
                if m:
                    name = m.group(2)
                    if name not in _BRACE_KEYWORDS:
                        qualname = f"{current_class}.{name}"
                        add(name, qualname, "method", i, line.strip())
                        continue

            # Object literal method
            if current_objlit and indent >= 2:
                m = re.match(
                    r"^\s{2,}([A-Za-z_$][A-Za-z0-9_$]*)\s*\([^)]*\)\s*\{",
                    line
                )
                if m:
                    name = m.group(1)
                    if name not in _BRACE_KEYWORDS:
                        add(name, f"{current_objlit}.{name}", "method", i, line.strip())
                        continue

    elif lang == "go":
        _fn_re = re.compile(r"^func\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
        _method_re = re.compile(
            r"^func\s+\(\s*\w+\s+\*?([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*([A-Za-z_][A-Za-z0-9_]*)\s*\("
        )
        _type_re = re.compile(
            r"^type\s+([A-Za-z_][A-Za-z0-9_]*)\s+(struct|interface)"
        )
        for i, line in enumerate(lines):
            m = _method_re.match(line)
            if m:
                recv, name = m.group(1), m.group(2)
                add(name, f"{recv}.{name}", "method", i, line.strip())
                continue
            m = _fn_re.match(line)
            if m:
                add(m.group(1), m.group(1), "function", i, line.strip())
                continue
            m = _type_re.match(line)
            if m:
                add(m.group(1), m.group(1), "class", i, line.strip())

    elif lang == "rust":
        _fn_re = re.compile(
            r"^\s*(pub(\(crate\))?\s+)?(async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*"
        )
        _impl_re = re.compile(r"^\s*impl(?:<[^>]*>)?\s+(\*?[A-Za-z_][A-Za-z0-9_]*)")
        _type_re = re.compile(
            r"^\s*(pub(\(crate\))?\s+)?(struct|enum|trait)\s+([A-Za-z_][A-Za-z0-9_]*)"
        )
        current_impl = None
        current_impl_indent = -1
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            m = _impl_re.match(line)
            if m:
                current_impl = m.group(1).lstrip("*")
                current_impl_indent = indent
                continue
            m = _fn_re.match(line)
            if m:
                name = m.group(4)
                if current_impl and indent > current_impl_indent:
                    add(name, f"{current_impl}.{name}", "method", i, line.strip())
                else:
                    add(name, name, "function", i, line.strip())
                continue
            m = _type_re.match(line)
            if m:
                add(m.group(4), m.group(4), "class", i, line.strip())

    elif lang == "shell":
        _fn_re = re.compile(
            r"^(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*\)"
        )
        for i, line in enumerate(lines):
            m = _fn_re.match(line.strip())
            if m:
                name = m.group(1)
                add(name, name, "function", i, line.strip())

    elif lang == "powershell":
        _fn_re = re.compile(r"^function\s+([A-Za-z_][A-Za-z0-9_\-]*)", re.IGNORECASE)
        for i, line in enumerate(lines):
            m = _fn_re.match(line.strip())
            if m:
                name = m.group(1)
                if _valid_ident(name.replace("-", "_")):
                    defs.append({
                        "name": name,
                        "qualname": name,
                        "kind": "function",
                        "line": i + 1,
                        "end_line": None,
                        "sig": line.strip(),
                        "_line0": i,
                    })

    elif lang == "jvm":
        _cls_re = re.compile(
            r"^\s*(public|private|protected|internal|abstract|sealed|open|data|enum|annotation)?\s*"
            r"(class|interface|enum|object)\s+([A-Za-z_][A-Za-z0-9_]*)"
        )
        _method_re = re.compile(
            r"^\s+(public|private|protected|internal|static|override|suspend|abstract|"
            r"synchronized|final|open|fun|virtual|\w+<[^>]*>|\w+)\s+"
            r"(?:(?:public|private|protected|internal|static|override|suspend|abstract|"
            r"synchronized|final|open|fun|virtual|\w+<[^>]*>|\w+)\s+)*"
            r"([A-Za-z_][A-Za-z0-9_]*)\s*\("
        )
        for i, line in enumerate(lines):
            m = _cls_re.match(line)
            if m:
                add(m.group(3), m.group(3), "class", i, line.strip())
                continue
            m = _method_re.match(line)
            if m:
                name = m.group(2) if m.group(2) else m.lastgroup
                # The last capture group in _method_re is the name
                name = m.groups()[-1]
                if name and _valid_ident(name):
                    add(name, name, "method", i, line.strip())

    elif lang == "ruby":
        _fn_re = re.compile(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_?!]*)")
        _cls_re = re.compile(r"^\s*(class|module)\s+([A-Za-z_][A-Za-z0-9_:]*)")
        for i, line in enumerate(lines):
            m = _fn_re.match(line)
            if m:
                name = m.group(1)
                # Ruby allows ? and ! in method names, strip for ident check
                base = name.rstrip("?!")
                if _valid_ident(base):
                    add(name, name, "function", i, line.strip())
                continue
            m = _cls_re.match(line)
            if m:
                name = m.group(2).split("::")[-1]
                if _valid_ident(name):
                    add(name, name, "class", i, line.strip())

    elif lang == "php":
        _fn_re = re.compile(r"^\s*function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
        _cls_re = re.compile(r"^\s*(class|interface|abstract\s+class|trait)\s+([A-Za-z_][A-Za-z0-9_]*)")
        for i, line in enumerate(lines):
            m = _fn_re.match(line)
            if m:
                add(m.group(1), m.group(1), "function", i, line.strip())
                continue
            m = _cls_re.match(line)
            if m:
                add(m.group(2), m.group(2), "class", i, line.strip())

    # Resolve end_line (1-based, inclusive) for all defs that don't have one:
    # the block ends at the first non-blank line at the same or shallower
    # indentation. A closing token there (`}`, `end`, `fi`, ...) belongs to the
    # block; anything else -- another definition, or top-level code -- does
    # not. Scanning on past top-level code would credit a change to that code
    # to the function above it.
    for idx, d in enumerate(defs):
        if d.get("end_line") is not None:
            continue
        line0 = d.get("_line0", d["line"] - 1)
        own_indent = len(lines[line0]) - len(lines[line0].lstrip()) if line0 < n else 0
        end = n  # default: EOF
        for j in range(line0 + 1, n):
            stripped = lines[j].strip()
            if not stripped:
                continue
            j_indent = len(lines[j]) - len(lines[j].lstrip())
            if j_indent > own_indent:
                continue
            if _is_header_continuation(stripped):
                continue  # `{` on its own line, or `) {` after wrapped params
            end = j + 1 if _is_block_close(stripped) else j
            break
        d["end_line"] = end

    # Remove internal helpers
    for d in defs:
        d.pop("_line0", None)
        d.pop("_class", None)

    return defs


_BLOCK_CLOSE_RE = re.compile(r"^(\}|\]|end\b|fi\b|esac\b|done\b)")


def _is_block_close(stripped):
    return bool(_BLOCK_CLOSE_RE.match(stripped))


def _is_header_continuation(stripped):
    """Still the definition's header: an Allman-style `{` on its own line, or
    the `) {` / `): T {` that closes a wrapped parameter list."""
    return stripped == "{" or (stripped[:1] in (")", "]") and stripped.endswith("{"))


def _find_enclosing_regex(defs: List[Dict], lineno: int) -> Optional[Dict]:
    """Given 1-based lineno, return the outermost enclosing callable def.

    Priority: method/function > variable (object-literal container) > class.
    For nested functions, return outermost (largest span) among method/function.
    """
    candidates = [d for d in defs if d["line"] <= lineno <= (d["end_line"] or lineno)]
    if not candidates:
        return None

    # Prefer function/method (actual callables) over variable (e.g. JS object literals).
    fn_candidates = [d for d in candidates if d.get("kind") in ("function", "method")]
    if fn_candidates:
        fn_candidates.sort(key=lambda d: (d["end_line"] or d["line"]) - d["line"], reverse=True)
        return fn_candidates[0]

    # Variable (lambda container in Python, or object-literal in JS) — take smallest span
    # so we pick the outermost one that still encloses the line.
    var_candidates = [d for d in candidates if d.get("kind") == "variable"]
    if var_candidates:
        var_candidates.sort(key=lambda d: (d["end_line"] or d["line"]) - d["line"])
        return var_candidates[0]

    candidates.sort(key=lambda d: (d["end_line"] or d["line"]) - d["line"], reverse=True)
    return candidates[0]


def _changed_line_sets(old_lines, new_lines):
    """Return (old_changed: set of 1-based lines, new_changed: set of 1-based lines)."""
    old_changed: Set[int] = set()
    new_changed: Set[int] = set()
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag in ("replace", "delete"):
            for k in range(i1, i2):
                old_changed.add(k + 1)  # 1-based
        if tag in ("replace", "insert"):
            for k in range(j1, j2):
                new_changed.add(k + 1)
    return old_changed, new_changed


def changed_symbols(path: str, old_text: Optional[str], new_text: Optional[str]) -> Dict:
    """Find symbols whose definitions changed between old_text and new_text.

    Returns {"symbols": [...], "supported": bool, "warnings": [...]}.
    Never raises.
    """
    warnings = []
    try:
        return _changed_symbols_inner(path, old_text, new_text, warnings)
    except Exception as exc:
        warnings.append(f"changed_symbols({path}): {exc}")
        return {"symbols": [], "supported": False, "warnings": warnings}


def _changed_symbols_inner(path, old_text, new_text, warnings):
    ext = os.path.splitext(path)[1].lower()
    lang = _EXT_LANG.get(ext)
    supported = lang is not None

    old_lines = old_text.splitlines() if old_text else []
    new_lines = new_text.splitlines() if new_text else []

    old_changed, new_changed = _changed_line_sets(old_lines, new_lines)

    if not old_changed and not new_changed:
        return {"symbols": [], "supported": supported, "warnings": warnings}

    if not supported:
        return {"symbols": [], "supported": False, "warnings": warnings}

    # Get definitions for both texts
    if lang == "python":
        old_defs = _ast_defs(old_text, path) if old_text else []
        new_defs = _ast_defs(new_text, path) if new_text else []
        # Fall back to regex if AST failed
        if not old_defs and old_text:
            old_defs = _regex_defs(old_text, lang)
            if old_defs:
                warnings.append(f"Python AST failed for old {path}, using regex fallback")
        if not new_defs and new_text:
            new_defs = _regex_defs(new_text, lang)
            if new_defs:
                warnings.append(f"Python AST failed for new {path}, using regex fallback")
        find_enc_old = _find_enclosing_ast
        find_enc_new = _find_enclosing_ast
    else:
        old_defs = _regex_defs(old_text, lang) if old_text else []
        new_defs = _regex_defs(new_text, lang) if new_text else []
        find_enc_old = _find_enclosing_regex
        find_enc_new = _find_enclosing_regex

    # Map changed lines to enclosing definitions
    touched: Set[Tuple[str, str]] = set()  # (name, qualname) pairs
    touched_via_old: Dict[Tuple[str, str], Dict] = {}
    touched_via_new: Dict[Tuple[str, str], Dict] = {}

    for lineno in old_changed:
        d = find_enc_old(old_defs, lineno)
        if d:
            k = (d["name"], d["qualname"])
            touched.add(k)
            touched_via_old[k] = d

    for lineno in new_changed:
        d = find_enc_new(new_defs, lineno)
        if d:
            k = (d["name"], d["qualname"])
            touched.add(k)
            touched_via_new[k] = d

    # Check for module-level changes (lines not inside any def)
    module_changed = False
    for lineno in old_changed:
        if find_enc_old(old_defs, lineno) is None:
            module_changed = True
            break
    if not module_changed:
        for lineno in new_changed:
            if find_enc_new(new_defs, lineno) is None:
                module_changed = True
                break

    # Build name -> def maps for classification
    old_by_name: Dict[str, Dict] = {d["name"]: d for d in old_defs}
    new_by_name: Dict[str, Dict] = {d["name"]: d for d in new_defs}
    old_by_qual: Dict[str, Dict] = {d["qualname"]: d for d in old_defs}
    new_by_qual: Dict[str, Dict] = {d["qualname"]: d for d in new_defs}

    results = []
    seen_keys: Set[Tuple[str, str]] = set()

    def _lines_changed_count(old_d, new_d):
        count = 0
        if old_d:
            for ln in old_changed:
                if old_d["line"] <= ln <= (old_d.get("end_line") or old_d["line"]):
                    count += 1
        if new_d:
            for ln in new_changed:
                if new_d["line"] <= ln <= (new_d.get("end_line") or new_d["line"]):
                    count += 1
        return max(count, 1)

    for (name, qualname) in touched:
        if (name, qualname) in seen_keys:
            continue
        seen_keys.add((name, qualname))

        if not _valid_ident(name):
            continue

        old_d = old_by_qual.get(qualname) or old_by_name.get(name)
        new_d = new_by_qual.get(qualname) or new_by_name.get(name)

        in_old = old_d is not None
        in_new = new_d is not None

        if in_old and not in_new:
            # Check for rename: a new-only def with same normalized param list
            old_sig_norm = _norm_sig(old_d["sig"])
            renamed_to = ""
            new_only = [d for d in new_defs if d["name"] not in old_by_name]
            for nd in new_only:
                if _norm_sig(nd["sig"]).split("(", 1)[-1] == old_sig_norm.split("(", 1)[-1]:
                    renamed_to = nd["name"]
                    break
            change = "renamed" if renamed_to else "removed"
            use_d = old_d
            old_sig = old_d["sig"]
            new_sig = ""
        elif not in_old and in_new:
            change = "added"
            use_d = new_d
            old_sig = ""
            new_sig = new_d["sig"]
            renamed_to = ""
        else:
            # In both
            old_sig_n = _norm_sig(old_d["sig"]) if old_d else ""
            new_sig_n = _norm_sig(new_d["sig"]) if new_d else ""
            if old_sig_n != new_sig_n:
                change = "signature"
            else:
                change = "body"
            use_d = new_d or old_d
            old_sig = old_d["sig"] if old_d else ""
            new_sig = new_d["sig"] if new_d else ""
            renamed_to = ""

        lc = _lines_changed_count(old_d, new_d)
        ref_line = (new_d or old_d)["line"]
        kind = use_d.get("kind", "function")

        entry = {
            "name": name,
            "qualname": qualname,
            "kind": kind,
            "change": change,
            "defined_in": path,
            "line": ref_line,
            "old_signature": old_sig,
            "new_signature": new_sig,
            "renamed_to": renamed_to,
            "lines_changed": lc,
        }
        results.append(entry)

    # Module-level entry
    if module_changed:
        stem = _module_stem(path)
        module_path = _dotted_module(path) if lang == "python" else re.sub(r"\.[^.]+$", "", path.replace("\\", "/"))
        results.append({
            "name": stem,
            "qualname": stem,
            "kind": "module",
            "change": "module",
            "defined_in": path,
            "line": 1,
            "old_signature": "",
            "new_signature": "",
            "renamed_to": "",
            "lines_changed": len(old_changed) + len(new_changed),
            "module": module_path,
        })

    return {"symbols": results, "supported": supported, "warnings": warnings}


# ---------------------------------------------------------------------------
# 3. find_references
# ---------------------------------------------------------------------------

# Per-language import pattern builders for tier-1 (proximity) search.

def _python_import_patterns(module_dotted: str, stem: str) -> List[str]:
    """Return fixed-string patterns that identify importers of a Python module."""
    parent = module_dotted.rsplit(".", 1)[0] if "." in module_dotted else ""
    patterns = [
        f"import {module_dotted}",
        f"from {module_dotted} import",
    ]
    if parent:
        patterns.append(f"from {parent} import {stem}")
    patterns.append(f"from .{stem} import")
    patterns.append(f"from . import {stem}")
    return patterns


def _js_import_patterns(path: str) -> List[str]:
    """Return fixed-string patterns for JS/TS importers of a given path."""
    stem = os.path.splitext(os.path.basename(path))[0]
    patterns = [
        f"/{stem}'",
        f'/{stem}"',
        f"/{stem}.js'",
        f'/{stem}.js"',
        f"/{stem}.ts'",
        f'/{stem}.ts"',
        f"/{stem}.jsx'",
        f'/{stem}.jsx"',
        f"/{stem}.tsx'",
        f'/{stem}.tsx"',
    ]
    return patterns


def _go_import_patterns(path: str) -> List[str]:
    """Return fixed-string patterns for Go importers (package dir path)."""
    # Use the directory portion
    d = os.path.dirname(path).replace("\\", "/")
    if not d:
        return []
    parts = d.split("/")
    # Use last 2 path components as a conservative suffix
    suffix = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    return [f'"{suffix}"']


def _parse_grep_line(line: str, tip: str) -> Optional[Tuple[str, int, str]]:
    """Parse a 'git grep --null -n' output line into (path, lineno, text).

    With --null and a tree-ish, git grep -n output format per match line is:
        <tip>:<path>\0<lineno>\0<text>
    (null separates path from lineno and lineno from text; lines end with \n)
    We use --null so paths containing ':' are parsed correctly.
    """
    # Split on null to get [path_part, lineno_part, text_part, ...]
    parts = line.split("\0")
    if len(parts) < 3:
        return None
    path_part, lineno_part = parts[0], parts[1]
    text = "\0".join(parts[2:])  # text itself may (rarely) contain nulls
    # path_part = "<tip>:<path>" — strip the tree-ish prefix
    colon = path_part.find(":")
    if colon == -1:
        return None
    path = path_part[colon + 1:]
    try:
        lineno = int(lineno_part)
    except ValueError:
        return None
    return path, lineno, text


def find_references(
    run: Callable,
    tip: str,
    symbols: List[Dict],
    *,
    is_allowed: Optional[Callable] = None,
    max_sites: int = 500,
) -> Dict:
    """Find call sites for the given symbols in the repository at tip.

    Returns {"sites": [...], "ref_counts": {name: int}, "warnings": [...]}.
    Never raises.
    """
    warnings = []
    try:
        return _find_references_inner(run, tip, symbols, is_allowed=is_allowed,
                                      max_sites=max_sites, warnings=warnings)
    except Exception as exc:
        warnings.append(f"find_references: {exc}")
        return {"sites": [], "ref_counts": {}, "warnings": warnings}


def _find_references_inner(run, tip, symbols, *, is_allowed, max_sites, warnings):
    # Filter: only search symbols with these change types
    searchable_changes = {"removed", "renamed", "signature", "body", "module"}
    to_search = [s for s in symbols if s.get("change") in searchable_changes]

    if not to_search:
        return {"sites": [], "ref_counts": {}, "warnings": warnings}

    # Group by defining file for tier-1 importer search
    by_file: Dict[str, List[Dict]] = {}
    for s in to_search:
        by_file.setdefault(s["defined_in"], []).append(s)

    # Collect sites: path -> {name -> set of linenos} for dedup
    found_sites: Dict[str, Dict[str, List[Tuple[int, str, int]]]] = {}
    # path -> name -> [(lineno, text, tier)]

    tier1_files: Set[str] = set()

    # (path, name, lineno) -> defining files whose importer search found it.
    # A name can be defined in several changed files; this is what lets the
    # bundle tell whose caller a tier-1 site is.
    found_via: Dict[Tuple[str, str, int], Set[str]] = {}

    def _add_site(path, name, lineno, text, tier, via=None):
        if via:
            found_via.setdefault((path, name, lineno), set()).add(via)
        found_sites.setdefault(path, {}).setdefault(name, [])
        # Avoid duplicates
        existing = found_sites[path][name]
        for (ln, _, _) in existing:
            if ln == lineno:
                return
        existing.append((lineno, text, tier))

    # Tier 1: importer discovery per defining file
    for def_file, syms in by_file.items():
        ext = os.path.splitext(def_file)[1].lower()
        lang = _EXT_LANG.get(ext)

        patterns: List[str] = []
        if lang == "python":
            module_dotted = _dotted_module(def_file)
            stem = _module_stem(def_file)
            patterns = _python_import_patterns(module_dotted, stem)
        elif lang in ("js", "ts"):
            patterns = _js_import_patterns(def_file)
        elif lang == "go":
            patterns = _go_import_patterns(def_file)
        else:
            patterns = []

        if not patterns:
            continue

        # Build args for git grep to find importer files
        args = ["grep", "-l", "--null", "-F"]
        for p in patterns:
            args += ["-e", p]
        args += [tip, "--"]
        out, rc = run(args)
        if rc != 0 or not out:
            continue

        # Parse importer file list.
        # With tree-ish + --null in -l mode, output is:
        #   "<tip>:<path>\0<tip>:<path>\0..."  (null-terminated entries)
        raw_paths = []
        for chunk in out.split("\0"):
            chunk = chunk.strip()
            if not chunk:
                continue
            # Strip tree-ish prefix "<tip>:" if present
            colon = chunk.find(":")
            if colon != -1:
                raw_paths.append(chunk[colon + 1:])
            else:
                raw_paths.append(chunk)

        if not raw_paths:
            continue

        # Filter importer files
        importer_files = [p for p in raw_paths if p and p != def_file]
        if is_allowed:
            importer_files = [p for p in importer_files if is_allowed(p)]

        if not importer_files:
            continue

        # Search for symbol names in those importer files
        names = [s["name"] for s in syms if _valid_ident(s.get("name", ""))]
        if not names:
            continue

        # Split into batches to avoid overly long command lines
        for batch in _batched(importer_files, 50):
            name_args = ["grep", "-n", "--null", "-w", "-F"]
            for nm in names:
                name_args += ["-e", nm]
            name_args += [tip, "--"] + batch
            out2, rc2 = run(name_args)
            if rc2 == 0 and out2:
                for line in out2.splitlines():
                    parsed = _parse_grep_line(line, tip)
                    if parsed:
                        fpath, lineno, text = parsed
                        if fpath == def_file:
                            continue
                        if is_allowed and not is_allowed(fpath):
                            continue
                        # Which name matched?
                        for nm in names:
                            if _uses_name(nm, text):
                                _add_site(fpath, nm, lineno, text, 1, via=def_file)
                        tier1_files.add(fpath)

    # Tier 2: global search for all names
    all_names = list({s["name"] for s in to_search if _valid_ident(s.get("name", ""))})
    if all_names:
        args2 = ["grep", "-n", "--null", "-w", "-F"]
        for nm in all_names:
            args2 += ["-e", nm]
        args2 += [tip, "--"]
        out3, rc3 = run(args2)
        if rc3 == 0 and out3:
            # Map name -> every changed file defining it, for exclusion
            def_files_by_name: Dict[str, Set[str]] = {}
            for s in to_search:
                def_files_by_name.setdefault(s["name"], set()).add(s["defined_in"])
            for line in out3.splitlines():
                parsed = _parse_grep_line(line, tip)
                if parsed:
                    fpath, lineno, text = parsed
                    if is_allowed and not is_allowed(fpath):
                        continue
                    for nm in all_names:
                        if _uses_name(nm, text):
                            # Exclude the defining file entirely
                            if fpath in def_files_by_name.get(nm, ()):
                                continue
                            # Tier 2 only if not already tier 1
                            tier = 1 if fpath in tier1_files else 2
                            _add_site(fpath, nm, lineno, text, tier)

    # Flatten into sites list, capped at max_sites
    sites = []
    ref_file_sets: Dict[str, Set[str]] = {}  # name -> set of files

    for fpath, name_dict in sorted(found_sites.items()):
        for name, entries in sorted(name_dict.items()):
            for (lineno, text, tier) in sorted(entries):
                if len(sites) >= max_sites:
                    warnings.append(f"find_references: hit max_sites={max_sites}, some sites omitted")
                    break
                site_id = _sha1(f"{fpath}:{lineno}:{name}")[:10]
                sites.append({
                    "id": site_id,
                    "path": fpath,
                    "line": lineno,
                    "name": name,
                    "tier": tier,
                    "text": text,
                    "via": sorted(found_via.get((fpath, name, lineno), ())),
                })
                ref_file_sets.setdefault(name, set()).add(fpath)
            else:
                continue
            break
        else:
            continue
        break

    ref_counts = {name: len(files) for name, files in ref_file_sets.items()}
    return {"sites": sites, "ref_counts": ref_counts, "warnings": warnings}


def _batched(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]


# ---------------------------------------------------------------------------
# 4. build_bundle
# ---------------------------------------------------------------------------

# Change tier for ranking: contract-breaking changes come first
_CHANGE_RANK = {"removed": 0, "renamed": 0, "signature": 0, "module": 1, "body": 2, "added": 3}


def build_bundle(
    run: Callable,
    tip: str,
    symbols: List[Dict],
    sites: List[Dict],
    ref_counts: Dict[str, int],
    *,
    include_paths: Optional[Set] = None,
    exclude_paths: Tuple = (),
    max_symbols: int = 30,
    per_symbol: int = 5,
    max_sites: int = 40,
    max_bytes: int = 12000,
    context: int = 6,
    line_cap: int = 200,
) -> Dict:
    """Assemble a compact, byte-capped bundle for reviewer context.

    Returns {"symbols": [...], "sites": [...], "dropped_symbols": [...],
             "truncated": bool, "counts": {...}}.
    Never raises.
    """
    try:
        return _build_bundle_inner(
            run, tip, symbols, sites, ref_counts,
            include_paths=include_paths, exclude_paths=exclude_paths,
            max_symbols=max_symbols, per_symbol=per_symbol,
            max_sites=max_sites, max_bytes=max_bytes,
            context=context, line_cap=line_cap,
        )
    except Exception as exc:
        return {
            "symbols": [], "sites": [], "dropped_symbols": [],
            "truncated": False,
            "counts": {"symbols_found": 0, "symbols_included": 0,
                       "sites_found": 0, "sites_included": 0, "bytes": 0},
            "_error": str(exc),
        }


def _build_bundle_inner(
    run, tip, symbols, sites, ref_counts, *,
    include_paths, exclude_paths, max_symbols, per_symbol,
    max_sites, max_bytes, context, line_cap,
):
    exclude_set = set(exclude_paths)

    # Filter symbols by include_paths if given
    filtered_syms = [s for s in symbols
                     if include_paths is None or s.get("defined_in") in include_paths]

    # Only non-added symbols are candidates for ref site inclusion
    # (added symbols have no callers to search for)
    ranked = [s for s in filtered_syms if s.get("change") != "added"]

    # Sort: change tier asc, then ref_count asc (fewest refs first — specific callers),
    # then lines_changed desc (bigger change more important among ties)
    def _sort_key(s):
        rc = ref_counts.get(s["name"], 0)
        cr = _CHANGE_RANK.get(s.get("change", "body"), 2)
        lc = s.get("lines_changed", 0)
        return (cr, rc, -lc)

    ranked.sort(key=_sort_key)

    # Keep max_symbols; rest -> dropped (only those with >0 sites)
    sites_by_sym: Dict[str, List[Dict]] = {}
    for site in sites:
        sites_by_sym.setdefault(site["name"], []).append(site)

    included_syms = ranked[:max_symbols]
    dropped_syms = [s["name"] for s in ranked[max_symbols:]
                    if sites_by_sym.get(s["name"])]
    truncated = len(dropped_syms) > 0

    # Everything per symbol is keyed by its DEFINITION, not its name: two
    # changed `__init__`s or `run`s are different symbols with their own pool,
    # cursor and quota. Sites are found by name, so a site is shared between
    # same-named symbols unless an importer search tied it to one of them.
    def _key(sym):
        return (sym.get("defined_in") or "", sym.get("qualname") or sym["name"])

    # Build site pools per symbol: its own importers' sites first, then the
    # other tier-1 and tier-2 sites of that name that are not tied to another
    # definition; exclude exclude_set; cap at 2 per (symbol, file)
    def _sym_sites(sym):
        name = sym["name"]
        mine = sym.get("defined_in") or ""
        raw = sites_by_sym.get(name, [])
        # Exclude paths, and callers an importer search tied to a different
        # definition of the same name.
        raw = [s for s in raw if s.get("path") not in exclude_set
               and (not s.get("via") or mine in s["via"])]
        t1 = [s for s in raw if s.get("via")]
        t2 = [s for s in raw if not s.get("via")]
        ordered = t1 + t2
        # Cap 2 per file
        file_count: Dict[str, int] = {}
        result = []
        for s in ordered:
            fp = s["path"]
            if file_count.get(fp, 0) < 2:
                result.append(s)
                file_count[fp] = file_count.get(fp, 0) + 1
        return result

    sym_pools = {_key(s): _sym_sites(s) for s in included_syms}
    name_count: Dict[str, int] = {}
    for s in included_syms:
        name_count[s["name"]] = name_count.get(s["name"], 0) + 1

    # Round-robin allocation
    # Blob cache: path -> list of lines
    blob_cache: Dict[str, Optional[List[str]]] = {}

    def _get_blob_lines(path: str) -> Optional[List[str]]:
        if path in blob_cache:
            return blob_cache[path]
        out, rc = run(["show", f"{tip}:{path}"])
        if rc != 0:
            blob_cache[path] = None
            return None
        # Check size: > 1MB?
        encoded = out.encode("utf-8", errors="replace")
        if len(encoded) > 1_000_000:
            blob_cache[path] = None
            return None
        lines = out.splitlines()
        # Reject minified: avg line length > 500
        if lines and sum(len(l) for l in lines) / len(lines) > 500:
            blob_cache[path] = None
            return None
        blob_cache[path] = lines
        return lines

    def _snippet(path: str, lineno: int) -> str:
        blob_lines = _get_blob_lines(path)
        if not blob_lines:
            return ""
        start = max(0, lineno - 1 - context)
        end = min(len(blob_lines), lineno + context)
        parts = []
        for i in range(start, end):
            raw_line = blob_lines[i]
            if len(raw_line) > line_cap:
                raw_line = raw_line[:line_cap] + "…"
            parts.append(f"{i+1}: {raw_line}")
        return "\n".join(parts)

    allocated_sites = []
    allocated_ids: Set[str] = set()  # a site shared by same-named symbols goes out once
    sym_given: Dict[Tuple[str, str], int] = {}
    total_bytes = 0
    sites_included = 0

    # Round-robin: iterate through symbols repeatedly until all pools exhausted
    # or limits hit.
    pointers: Dict[Tuple[str, str], int] = {_key(s): 0 for s in included_syms}
    active = list(included_syms)  # in ranked order for round-robin

    while active and sites_included < max_sites and total_bytes < max_bytes:
        still_active = []
        for sym in active:
            key = _key(sym)
            pool = sym_pools[key]
            idx = pointers[key]
            # Skip sites another same-named symbol already took.
            while idx < len(pool) and pool[idx].get("id") in allocated_ids:
                idx += 1
            pointers[key] = idx
            if idx >= len(pool):
                continue
            if sym_given.get(key, 0) >= per_symbol:
                continue
            site = pool[idx]
            pointers[key] = idx + 1

            if sites_included >= max_sites:
                truncated = True
                break
            snippet = _snippet(site["path"], site["line"])
            entry_bytes = len(snippet.encode("utf-8", errors="replace"))
            if total_bytes + entry_bytes > max_bytes:
                truncated = True
                break

            site_out = dict(site)
            site_out["snippet"] = snippet
            # Whose caller this is. A name defined in several changed files
            # without an importer tying the site to one of them is ambiguous,
            # and the reviewer is told so rather than guessing.
            site_out["defined_in"] = sym.get("defined_in") or ""
            if name_count.get(sym["name"], 0) > 1 and not site.get("via"):
                site_out["ambiguous"] = True
            allocated_sites.append(site_out)
            allocated_ids.add(site.get("id"))
            sym_given[key] = sym_given.get(key, 0) + 1
            total_bytes += entry_bytes
            sites_included += 1
            still_active.append(sym)

        if not still_active:
            break
        # Keep only symbols that still have room
        active = [s for s in still_active
                  if pointers[_key(s)] < len(sym_pools[_key(s)])
                  and sym_given.get(_key(s), 0) < per_symbol]

    # Build output symbol entries
    out_syms = []
    for sym in included_syms:
        entry = dict(sym)
        entry["ref_count"] = ref_counts.get(sym["name"], 0)
        entry["sites_included"] = sym_given.get(_key(sym), 0)
        out_syms.append(entry)

    return {
        "symbols": out_syms,
        "sites": allocated_sites,
        "dropped_symbols": dropped_syms,
        "truncated": truncated,
        "counts": {
            "symbols_found": len(filtered_syms),
            "symbols_included": len(out_syms),
            "sites_found": len(sites),
            "sites_included": sites_included,
            "bytes": total_bytes,
        },
    }


# ---------------------------------------------------------------------------
# 5. Sibling sweep
# ---------------------------------------------------------------------------

# Words that alone don't make a good search signature — pure punctuation/control flow.
_BORING_LINE_RE = re.compile(
    r"^[\s{}()\[\];,<>|&^%#@!~`'\"/\\:=.?+\-*]+$"
    # A bare keyword, optionally with a trivial literal (`return None`, `try:`).
    # Not a keyword followed by real code: `return eval(req.args['expr'])` is
    # often the most distinctive line a defect has.
    r"|^(return|pass|break|continue|else|elif|try|except|finally|do|end|fi|done|esac|"
    r"then|yield|raise|throw|null|nil|none|true|false|undefined|void)\b"
    r"[\s;:{}()]*(none|null|nil|true|false|undefined|0|\"\"|'')?[\s;:{}()]*$",
    re.IGNORECASE,
)


def signature_line(existing_code: str) -> str:
    """Return the most distinctive line from existing_code for grep-based sibling search.

    Returns "" if no suitable line is found.
    """
    if not existing_code:
        return ""
    lines = existing_code.splitlines()
    best = ""
    for line in lines:
        stripped = line.strip()
        normalized = " ".join(stripped.split())
        if len(normalized) < 20:
            continue
        if _BORING_LINE_RE.match(normalized):
            continue
        if len(normalized) > len(best):
            best = normalized
    return best


def find_siblings(
    run: Callable,
    tip: str,
    finding: Dict,
    *,
    push_paths: Set[str],
    is_allowed: Optional[Callable] = None,
    per_finding: int = 10,
) -> Dict:
    """Find files with the same code pattern as a given finding.

    Returns {"siblings": [...], "warnings": [...]}.  Never raises.
    """
    warnings = []
    try:
        return _find_siblings_inner(run, tip, finding, push_paths=push_paths,
                                    is_allowed=is_allowed, per_finding=per_finding,
                                    warnings=warnings)
    except Exception as exc:
        warnings.append(f"find_siblings: {exc}")
        return {"siblings": [], "warnings": warnings}


def _find_siblings_inner(run, tip, finding, *, push_paths, is_allowed, per_finding, warnings):
    existing_code = finding.get("existing_code", "") or ""
    sig = signature_line(existing_code)
    if not sig:
        return {"siblings": [], "warnings": warnings}

    finding_id = finding.get("id", "")
    finding_path = finding.get("path", "")
    start_line = finding.get("start_line", 0) or 0
    end_line = finding.get("end_line", 0) or 0
    severity = finding.get("severity", "")
    content = finding.get("content", "") or ""

    # git grep -n -F --null for the stripped signature line
    args = ["grep", "-n", "-F", "--null", "-e", sig, tip, "--"]
    out, rc = run(args)

    if rc != 0 and not out:
        return {"siblings": [], "warnings": warnings}

    in_push = []
    not_in_push = []

    for line in out.splitlines():
        parsed = _parse_grep_line(line, tip)
        if not parsed:
            continue
        fpath, lineno, text = parsed

        # Exclude hits inside the finding's own location
        if fpath == finding_path:
            if (start_line - 3) <= lineno <= (end_line + 3):
                continue

        if is_allowed and not is_allowed(fpath):
            continue

        sid = _sha1(f"{finding_id}:{fpath}:{lineno}")[:10]
        entry = {
            "sid": sid,
            "of": finding_id,
            "path": fpath,
            "line": lineno,
            "text": text,
            "in_push": fpath in push_paths,
            "severity": severity,
            "of_content": content[:300],
        }
        if fpath in push_paths:
            in_push.append(entry)
        else:
            not_in_push.append(entry)

    combined = (in_push + not_in_push)[:per_finding]
    return {"siblings": combined, "warnings": warnings}
