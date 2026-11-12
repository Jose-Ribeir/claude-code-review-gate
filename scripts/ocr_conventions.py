"""Two-layer convention-doc resolver for the review gate.

Given the repo root and the changed file list, produce the convention text that
applies to each changed file, merged from two layers:

1. Explicit mappings: ``conventions: [{doc, applyTo}]`` entries in the nearest
   ``.ocr/rule.json`` (walked up from the changed file, capped at the repo
   root). ``applyTo`` is a list of repo-relative globs defaulting to
   ``["**/*"]`` -- a doc applies repo-wide unless scoped otherwise. Doc paths
   resolve relative to the config root (the directory holding ``.ocr/``).
2. Walk-up ancestor docs: the nearest ``AGENTS.md`` or ``.ocr/conventions.md``
   at or above the changed file's directory (nearest ancestor wins). When both
   exist in the same directory, both apply, ``AGENTS.md`` first.

Merge rule: both layers are included, explicit mappings first, with each doc
rendered once per file. Budgets: each doc is capped at ``MAX_DOC_LINES`` lines
and the combined text per file at ``MAX_TOTAL_LINES`` lines; overflow is
truncated and each truncation (or drop) is recorded in the ``warnings`` array.

Fail-soft: the public entry points never raise; on any internal error they
return empty results plus a warning describing the failure.

CLI::

    python3 scripts/ocr_conventions.py --repo-root PATH [--max-doc-lines N]
        [--max-total-lines N] FILE [FILE ...]

Prints ``{"files": {path: text, ...}, "warnings": [...]}`` as JSON on stdout.
"""
import argparse
import json
import os
import re
import sys

MAX_DOC_LINES = 800
MAX_TOTAL_LINES = 4000

_DEFAULT_APPLY_TO = ["**/*"]
_ANCESTOR_DOC_RELPATHS = ("AGENTS.md", os.path.join(".ocr", "conventions.md"))


def _norm_rel(path):
    """Normalise a path to forward-slash relative form for matching.

    Both separators are normalised so Windows-style inputs work on POSIX
    hosts and vice versa. A leading ``./`` is stripped, exactly once.
    """
    path = path.replace("\\", "/")
    if path.startswith("./"):
        path = path[2:]
    return path


def _glob_to_regex(glob):
    """Translate a repo-relative glob into a compiled regex.

    ``**`` crosses directory boundaries, ``*`` and ``?`` stay within one
    path segment. A ``**/`` prefix also matches zero directories so
    ``**/*`` matches top-level files.
    """
    glob = _norm_rel(glob)
    out = []
    i = 0
    n = len(glob)
    while i < n:
        ch = glob[i]
        if ch == "*":
            if glob[i:i + 3] == "**/" :
                out.append("(?:.*/)?")
                i += 3
            elif glob[i:i + 2] == "**":
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _read_lines(path):
    """Read a file's lines; on failure return None (the caller warns)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    except OSError:
        return None


def _find_rule_json(repo_root, file_rel):
    """Nearest .ocr/rule.json at or above the changed file, capped at repo root.

    Returns the absolute path to the rule file, or None.
    """
    dir_abs = os.path.dirname(os.path.join(repo_root, file_rel))
    root_abs = os.path.abspath(repo_root)
    while True:
        candidate = os.path.join(dir_abs, ".ocr", "rule.json")
        if os.path.isfile(candidate):
            return candidate
        if os.path.abspath(dir_abs) == root_abs or not dir_abs.startswith(root_abs):
            return None
        dir_abs = os.path.dirname(dir_abs)


def _load_explicit_mappings(repo_root, file_rel, warnings):
    """Load [{doc_path_abs, doc_label, patterns}] for one file from rule.json.

    ``doc_label`` is the doc path relative to the repo root when possible,
    else the absolute path, used in output headers and warnings.
    """
    rule_path = _find_rule_json(repo_root, file_rel)
    if rule_path is None:
        return []
    try:
        with open(rule_path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        rel_rule = _norm_rel(os.path.relpath(rule_path, repo_root))
        warnings.append("conventions: could not parse %s (%s); explicit mappings skipped"
                        % (rel_rule, exc))
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("conventions")
    if not isinstance(entries, list):
        return []
    config_root = os.path.dirname(os.path.dirname(rule_path))
    mappings = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("doc"):
            continue
        doc_abs = os.path.normpath(os.path.join(config_root, entry["doc"]))
        try:
            doc_label = _norm_rel(os.path.relpath(doc_abs, repo_root))
        except ValueError:
            doc_label = doc_abs
        apply_to = entry.get("applyTo")
        if not isinstance(apply_to, list) or not apply_to:
            apply_to = _DEFAULT_APPLY_TO
        patterns = [_glob_to_regex(str(g)) for g in apply_to]
        mappings.append({"doc_abs": doc_abs, "doc_label": doc_label, "patterns": patterns})
    return mappings


def _find_ancestor_docs(repo_root, file_rel):
    """Nearest ancestor-dir docs for a changed file; [] when none exist.

    Walks from the file's directory up to the repo root (inclusive); the
    nearest directory containing ``AGENTS.md`` and/or ``.ocr/conventions.md``
    wins. Both docs in that directory apply, ``AGENTS.md`` first.
    """
    dir_abs = os.path.dirname(os.path.join(repo_root, file_rel))
    root_abs = os.path.abspath(repo_root)
    while True:
        found = [os.path.join(dir_abs, rel) for rel in _ANCESTOR_DOC_RELPATHS
                 if os.path.isfile(os.path.join(dir_abs, rel))]
        if found:
            return found
        if os.path.abspath(dir_abs) == root_abs or not dir_abs.startswith(root_abs):
            return []
        dir_abs = os.path.dirname(dir_abs)


def _collect_doc_entries(repo_root, file_rel, warnings):
    """Ordered [{doc_abs, doc_label, layer}] applying to one changed file.

    Explicit mappings first (in rule-file order), then the ancestor layer.
    Duplicate docs (same absolute path) are kept once, first occurrence wins.
    """
    entries = []
    seen = set()
    rel = _norm_rel(file_rel)
    for mapping in _load_explicit_mappings(repo_root, file_rel, warnings):
        if not any(rx.match(rel) for rx in mapping["patterns"]):
            continue
        if mapping["doc_abs"] in seen:
            continue
        seen.add(mapping["doc_abs"])
        entries.append({"doc_abs": mapping["doc_abs"], "doc_label": mapping["doc_label"],
                        "layer": "explicit"})
    for doc_abs in _find_ancestor_docs(repo_root, file_rel):
        if doc_abs in seen:
            continue
        seen.add(doc_abs)
        try:
            doc_label = _norm_rel(os.path.relpath(doc_abs, repo_root))
        except ValueError:
            doc_label = doc_abs
        entries.append({"doc_abs": doc_abs, "doc_label": doc_label, "layer": "ancestor"})
    return entries


def _truncate_doc(label, lines, max_doc_lines, warnings):
    """Apply the per-doc line cap; returns (lines, truncated_note or None)."""
    if max_doc_lines is None or len(lines) <= max_doc_lines:
        return lines, None
    note = "[truncated: showing first %d lines of %d]" % (max_doc_lines, len(lines))
    warnings.append("conventions: doc %s truncated to %d lines (per-doc cap; had %d)"
                    % (label, max_doc_lines, len(lines)))
    return lines[:max_doc_lines], note


def _render_file(entries, max_doc_lines, max_total_lines, warnings):
    """Render the merged convention text for one file; '' when nothing applies."""
    blocks = []
    used = 0
    for entry in entries:
        label = entry["doc_label"]
        lines = _read_lines(entry["doc_abs"])
        if lines is None:
            warnings.append("conventions: doc %s not found or unreadable; skipped" % label)
            continue
        lines, note = _truncate_doc(label, lines, max_doc_lines, warnings)
        block = "## Convention: %s (%s)\n%s" % (label, entry["layer"], "\n".join(lines))
        if note:
            block += "\n" + note
        block_lines = block.count("\n") + 1
        if max_total_lines is not None and used + block_lines > max_total_lines:
            remaining = max_total_lines - used
            if remaining <= 0:
                warnings.append(
                    "conventions: total budget of %d lines exceeded; dropped doc %s"
                    % (max_total_lines, label))
                continue
            block = "\n".join(block.splitlines()[:remaining])
            block += "\n[truncated: total conventions budget of %d lines reached]" % max_total_lines
            warnings.append(
                "conventions: doc %s truncated to fit total budget of %d lines"
                % (label, max_total_lines))
        blocks.append(block)
        used += block.count("\n") + 1
    return "\n\n".join(blocks)


def _resolve_conventions_inner(repo_root, files, max_doc_lines, max_total_lines, warnings):
    result = {}
    for file_rel in files:
        entries = _collect_doc_entries(repo_root, file_rel, warnings)
        text = _render_file(entries, max_doc_lines, max_total_lines, warnings)
        if text:
            result[file_rel] = text
    return result


def resolve_conventions(repo_root, files, *, max_doc_lines=MAX_DOC_LINES,
                        max_total_lines=MAX_TOTAL_LINES):
    """Resolve merged convention docs for each changed file. Fail-soft.

    Returns {"files": {path: text}, "warnings": [...]}; files with no
    applicable doc are omitted from "files".
    """
    warnings = []
    try:
        resolved = _resolve_conventions_inner(
            os.path.abspath(str(repo_root)), [_norm_rel(str(f)) for f in files],
            max_doc_lines, max_total_lines, warnings)
    except Exception as exc:  # fail-soft: never break the review
        warnings.append("conventions: resolver failed (%s); convention docs skipped" % exc)
        resolved = {}
    return {"files": resolved, "warnings": warnings}


def _main(argv=None):
    parser = argparse.ArgumentParser(
        description="Resolve merged convention docs per changed file (JSON on stdout).")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--max-doc-lines", type=int, default=MAX_DOC_LINES)
    parser.add_argument("--max-total-lines", type=int, default=MAX_TOTAL_LINES)
    parser.add_argument("files", nargs="*", help="changed file paths, relative to the repo root")
    args = parser.parse_args(argv)
    result = resolve_conventions(args.repo_root, args.files,
                                 max_doc_lines=args.max_doc_lines,
                                 max_total_lines=args.max_total_lines)
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
