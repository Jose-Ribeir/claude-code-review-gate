#!/usr/bin/env python3
#
# Deterministic verdict parser for review-gate.
#
# The verdict contract (the block/warn/pass tiers and the notion of acting on
# findings only after a falsification pass) is adapted from the review pipeline
# of open-code-review (ocr): https://github.com/alibaba/open-code-review
# Apache License, Version 2.0. The thresholding and the severity/confidence
# fields it operates on are additions of this project (ocr has no severity field).
# See the repository NOTICE file for full attribution.
#
# Reads the review JSON (an output object with a "findings" array, OR a bare
# findings array) on stdin or from a file argument, and prints exactly one of:
#   block | warn | pass
# The decision is intentionally simple and model-independent so that what blocks
# a commit is auditable code, not an LLM's discretion.
#
# Tunables (env):
#   OCR_BLOCK_SEVERITY    severity that can block        (default: high)
#   OCR_BLOCK_CONFIDENCE   min confidence to block        (default: 0.7)
import json
import os
import sys

_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}


def _block_threshold():
    sev = os.environ.get("OCR_BLOCK_SEVERITY", "high").strip().lower()
    try:
        conf = float(os.environ.get("OCR_BLOCK_CONFIDENCE", "0.7"))
    except ValueError:
        conf = 0.7
    return _SEVERITY_RANK.get(sev, 2), conf


def _findings(data):
    if isinstance(data, dict):
        return data.get("findings", []) or []
    if isinstance(data, list):
        return data
    return []


def _is_actionable(f):
    """A finding with no description or no valid location is not something a
    human can act on -- most likely the reviewer dropped required fields
    while generating the final JSON (seen in practice: severity/path present,
    content/start_line/end_line missing), not a deliberate signal. Treat it
    the same way the hallucination check (see the review skill's step 3a)
    treats an unverifiable existing_code snippet: too low-credibility to
    block on by itself, but still worth a warning -- it still counts toward
    `has_warn` below, and _format_reasons/the raw-output log still surface it
    for a human to look at.
    """
    # f.get("content", "") only falls back to "" when the key is absent -- an
    # explicit JSON null ("content": null) makes it return None, and str(None)
    # is the non-empty string "None", which would pass a bare truthy check.
    if not str(f.get("content") or "").strip():
        return False
    for key in ("start_line", "end_line"):
        try:
            if int(f.get(key)) <= 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def compute_verdict(data):
    """Return 'block' | 'warn' | 'pass' for a parsed review result."""
    min_rank, min_conf = _block_threshold()
    findings = _findings(data)
    has_warn = False
    for f in findings:
        if not isinstance(f, dict):
            continue
        rank = _SEVERITY_RANK.get(str(f.get("severity", "")).lower(), -1)
        try:
            conf = float(f.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        if rank >= min_rank and conf >= min_conf and _is_actionable(f):
            return "block"
        if rank >= _SEVERITY_RANK["medium"]:
            has_warn = True
    return "warn" if has_warn else "pass"


def main(argv):
    raw = ""
    if len(argv) > 1 and argv[1] not in ("-", ""):
        with open(argv[1], "r", encoding="utf-8") as fh:
            raw = fh.read()
    else:
        raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Unparseable input is not a basis to block anything — fail open.
        print("pass")
        return 0
    print(compute_verdict(data))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
