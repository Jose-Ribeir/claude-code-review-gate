#!/usr/bin/env python3
#
# Dismissed-finding memory for review-gate (v2).
#
# The contract (adapted from the review pipeline of open-code-review (ocr):
# https://github.com/alibaba/open-code-review, Apache License 2.0 -- see NOTICE
# for full attribution) is the notion of acting on findings only after a
# falsification pass; the dismissal store itself is an addition of this
# project.
#
# A local, untracked, append-only JSONL store at <repo>/.ocr/memory.jsonl
# records the findings a developer dismissed through the dismiss command.
# Matching a future finding against it uses the same signature_line()
# normalization ocr_impact uses for sibling search, plus the finding's
# category and a path glob. A matched finding is demoted by the gate to low
# severity with a "previously dismissed" note; the gate logs every demotion
# as a warning. Nothing is learned implicitly: this module has one writer
# (dismiss) and push bypasses never teach it.
#
# Everything here is fail-soft: the store is advisory, so no error in it may
# ever break a review, a push, or a dismissal attempt.
import fnmatch
import hashlib
import json
import os
import time

from ocr_impact import signature_line

SCHEMA_VERSION = 1
STORE_REL = os.path.join(".ocr", "memory.jsonl")
_SUMMARY_LIMIT = 200


def store_path(repo_root):
    """Absolute path of this repository's dismissal store."""
    return os.path.join(str(repo_root), STORE_REL)


def _code_key(text):
    """sha256 of the whitespace-collapsed code -- a whole-string identity for
    snippets too short to yield a signature_line() line."""
    return hashlib.sha256(" ".join(str(text or "").split()).encode("utf-8")).hexdigest()


def _signature_of(finding):
    # existing_code only: the signature identifies the defect's CODE, so a
    # reworded description of the same snippet still matches and a differently
    # worded different snippet does not. Snippets too small for a signature
    # line compare by whole-code key instead (see _record_matches); findings
    # with no code at all fall back to their normalized content there.
    return signature_line(str(finding.get("existing_code") or ""))


def dismiss(repo_root, finding, glob=None, context=None):
    """Record `finding` as dismissed. Returns the record dict, always.

    Appends one JSON line to `<repo_root>/.ocr/memory.jsonl`, creating .ocr/
    as needed, and best-effort adds the store to .git/info/exclude so the
    per-developer memory stays untracked. The default path glob is the
    finding's own path. `context` may carry extra provenance keys from the
    recorded verdict (verdict/head/branch). Never raises: a store that cannot
    be written is reported on the record's "error" key instead.
    """
    finding = finding if isinstance(finding, dict) else {}
    path = str(finding.get("path") or "")
    existing = str(finding.get("existing_code") or "")
    record = {
        "v": SCHEMA_VERSION,
        "id": str(finding.get("id") or ""),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ts": time.time(),
        "path": path,
        "glob": str(glob or path),
        "category": str(finding.get("category") or ""),
        "signature": _signature_of(finding),
        "code_key": _code_key(existing or finding.get("content")),
        "summary": " ".join(str(finding.get("content") or "").split())[:_SUMMARY_LIMIT],
    }
    for key, val in (context or {}).items():
        if isinstance(val, (str, int, float, bool)) and key not in record:
            record[key] = val
    try:
        repo = str(repo_root)
        path_on_disk = store_path(repo)
        os.makedirs(os.path.dirname(path_on_disk), exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with open(path_on_disk, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        _keep_untracked(repo)
    except Exception as exc:  # noqa: BLE001 -- the store must never break the caller
        record["error"] = f"{type(exc).__name__}: {exc}"
    return record


def _keep_untracked(repo_root):
    """Best-effort: keep .ocr/memory.jsonl out of `git status` via the local
    exclude file. Missing git dirs and unwritable files are quietly fine."""
    try:
        exclude = os.path.join(str(repo_root), ".git", "info", "exclude")
        if not os.path.isfile(exclude):
            return  # worktrees point .git elsewhere; the store is untracked anyway
        with open(exclude, "r", encoding="utf-8", errors="replace") as fh:
            body = fh.read()
        entry = ".ocr/memory.jsonl"
        if entry not in body.splitlines():
            with open(exclude, "a", encoding="utf-8") as fh:
                if body and not body.endswith("\n"):
                    fh.write("\n")
                fh.write(entry + "\n")
    except Exception:
        pass


def load(repo_root):
    """All dismissal records, oldest first. Corrupt or partial lines (a killed
    process may have half-written the tail) are skipped, never fatal."""
    records = []
    try:
        with open(store_path(repo_root), "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    records.append(obj)
    except OSError:
        pass
    except Exception:
        pass
    return records


def _category(finding):
    return str(finding.get("category") or "").strip().lower()


def _record_matches(record, finding):
    category = _category(finding)
    if not category or category != str(record.get("category") or "").strip().lower():
        return False
    path = str(finding.get("path") or "").replace("\\", "/")
    pattern = str(record.get("glob") or record.get("path") or "").replace("\\", "/")
    if not path or not pattern or not fnmatch.fnmatchcase(path, pattern):
        return False
    rec_sig = str(record.get("signature") or "")
    new_sig = _signature_of(finding)
    if rec_sig and new_sig:
        return rec_sig == new_sig
    if not rec_sig and not new_sig:
        # Both snippets too small for a signature line: compare whole-code identity.
        key = _code_key(finding.get("existing_code") or finding.get("content"))
        return bool(key) and key == str(record.get("code_key") or "")
    return False


def match_records(records, finding):
    """The latest (append-only => most recent is most authoritative) record
    matching `finding`, or None."""
    if not isinstance(finding, dict):
        return None
    for record in reversed(list(records or [])):
        try:
            if _record_matches(record, finding):
                return record
        except Exception:
            continue
    return None


def match(repo_root, finding):
    """The latest dismissal matching `finding`, or None. Never raises."""
    try:
        return match_records(load(repo_root), finding)
    except Exception:
        return None
