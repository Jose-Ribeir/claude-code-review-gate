"""Unit tests for review-gate.py's _extract_json balanced-brace parser."""
import importlib.util
import os
import sys

_BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
sys.path.insert(0, _BIN)  # so review-gate.py's own `from ocr_verdict import ...` resolves

_spec = importlib.util.spec_from_file_location("review_gate", os.path.join(_BIN, "review-gate.py"))
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)

_extract_json = review_gate._extract_json
_format_reasons = review_gate._format_reasons
compute_verdict = review_gate.compute_verdict


def test_whole_string_json():
    assert _extract_json('{"findings": []}') == {"findings": []}


def test_fenced_json_block():
    text = 'Here is the verdict:\n```json\n{"findings": [{"severity": "low"}]}\n```\n'
    assert _extract_json(text) == {"findings": [{"severity": "low"}]}


def test_trailing_prose_with_stray_brace_after_json():
    # The bug this parser fixes: a naive find("{")..rfind("}") span would grab
    # the LAST '}' in the whole text -- including the one in the parenthetical
    # below -- and fail to parse. A balanced scan must stop at the object's own
    # matching brace and ignore everything after it.
    text = 'Review complete - verdict pass {"findings": []} (no blocking issues found}'
    assert _extract_json(text) == {"findings": []}


def test_skips_unrelated_json_object_without_findings_key():
    # An earlier JSON-looking value quoted from the reviewed diff (e.g. a config
    # fixture) must not win over the real verdict that follows it.
    text = 'Example fixture: {"severity": "high"}\nActual verdict: {"findings": [{"severity": "high"}]}'
    assert _extract_json(text) == {"findings": [{"severity": "high"}]}


def test_last_findings_object_wins_when_multiple_present():
    text = '{"findings": [{"id": 1}]}\nWait, corrected: {"findings": [{"id": 2}]}'
    assert _extract_json(text) == {"findings": [{"id": 2}]}


def test_no_json_present_returns_none():
    assert _extract_json("Review complete, verdict pass, nothing to report.") is None


def test_empty_string_returns_none():
    assert _extract_json("") is None


def test_malformed_braces_return_none():
    assert _extract_json("{not: valid json at all") is None


def test_format_reasons_full_finding():
    result = {
        "findings": [
            {"severity": "high", "path": "a.py", "start_line": 3, "end_line": 3, "content": "bug"}
        ]
    }
    assert _format_reasons(result) == "  [high] a.py:3 - bug"


def test_format_reasons_missing_fields_flagged_not_blank():
    # A finding can be syntactically valid JSON yet still miss the fields it
    # needs to be actionable (the reviewer skipped them under output-length
    # pressure). The line must say so, not silently print "path:? - " with
    # nothing after the dash, which reads as display truncation rather than a
    # defect in the review itself.
    result = {"findings": [{"severity": "high", "path": "a.py"}]}
    line = _format_reasons(result)
    assert line.startswith("  [high] a.py:? - ")
    assert "reviewer omitted" in line


def test_complete_high_confidence_finding_blocks():
    result = {
        "findings": [
            {
                "severity": "high",
                "confidence": 0.9,
                "path": "a.py",
                "start_line": 3,
                "end_line": 3,
                "content": "real bug",
            }
        ]
    }
    assert compute_verdict(result) == "block"


def test_incomplete_finding_cannot_block_even_at_high_confidence():
    # The exact shape observed in production: severity/confidence/path present
    # (enough to pass the old block check) but content/lines missing -- nothing
    # a human could act on. compute_verdict is the auditable, model-independent
    # decision point (see its own module docstring), so this must be enforced
    # here rather than trusted to the reviewing LLM to self-police.
    result = {
        "findings": [
            {"severity": "high", "confidence": 0.9, "path": "a.py"}
        ]
    }
    assert compute_verdict(result) != "block"


def test_incomplete_finding_still_counts_as_warn():
    # Not actionable enough to block, but still a real signal -- must not be
    # thrown away entirely, only downgraded below the blocking threshold.
    result = {
        "findings": [
            {"severity": "high", "confidence": 0.9, "path": "a.py"}
        ]
    }
    assert compute_verdict(result) == "warn"


def test_finding_with_explicit_null_content_cannot_block():
    # f.get("content", "") only uses the "" default when the key is absent;
    # a JSON null makes it return None, and str(None) is the non-empty string
    # "None" -- a bare truthy check on that would wrongly call it actionable.
    result = {
        "findings": [
            {
                "severity": "high",
                "confidence": 0.9,
                "path": "a.py",
                "start_line": 5,
                "end_line": 6,
                "content": None,
            }
        ]
    }
    assert compute_verdict(result) != "block"


def test_incomplete_finding_with_zero_line_numbers_cannot_block():
    result = {
        "findings": [
            {
                "severity": "high",
                "confidence": 0.9,
                "path": "a.py",
                "start_line": 0,
                "end_line": 0,
                "content": "bug",
            }
        ]
    }
    assert compute_verdict(result) != "block"


# --- marker reaping -----------------------------------------------------------
# Markers are written per reviewed HEAD sha so the paired adapter (Claude Code
# hook vs global git hook) can skip re-reviewing the same push. They were never
# removed, so one file accumulated in the git dir per passing push, forever.

import time  # noqa: E402

_reap_markers = review_gate._reap_markers
_marker_path = review_gate._marker_path
MARKER_PREFIX = review_gate.MARKER_PREFIX
MARKER_TTL = review_gate.MARKER_TTL


def _write_marker(git_dir, sha, age_seconds):
    path = _marker_path(str(git_dir), sha)
    path.write_text("x", encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def test_reap_removes_expired_markers(tmp_path):
    old = _write_marker(tmp_path, "a" * 40, MARKER_TTL + 60)
    _reap_markers(str(tmp_path))
    assert not old.exists()


def test_reap_keeps_unexpired_markers_for_other_shas(tmp_path):
    # A fresh marker for another sha is still load-bearing: the paired adapter
    # may be mid-push against a different HEAD.
    fresh = _write_marker(tmp_path, "b" * 40, 10)
    _reap_markers(str(tmp_path))
    assert fresh.exists()


def test_reap_never_removes_the_marker_just_written(tmp_path):
    # Guards the keep= contract even if the new marker's mtime looks expired
    # (clock skew, or a filesystem with coarse timestamps).
    current = _write_marker(tmp_path, "c" * 40, MARKER_TTL + 60)
    _reap_markers(str(tmp_path), keep=current)
    assert current.exists()


def test_reap_ignores_unrelated_files_in_the_git_dir(tmp_path):
    # The sweep globs inside the real .git directory -- it must not touch HEAD,
    # config, or anything else that happens to be old.
    bystander = tmp_path / "config"
    bystander.write_text("[core]", encoding="utf-8")
    os.utime(bystander, (time.time() - 999999,) * 2)
    _write_marker(tmp_path, "d" * 40, MARKER_TTL + 60)
    _reap_markers(str(tmp_path))
    assert bystander.exists()


def test_reap_survives_a_missing_git_dir(tmp_path):
    # Housekeeping must never raise into the gate's pass path.
    _reap_markers(str(tmp_path / "does-not-exist"))
