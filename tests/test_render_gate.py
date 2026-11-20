"""Fewer LLM turns (review-speed 12): the gate renders what a formatter session used to.

A `--json` review session now prints its RAW results (findings, warnings, a few
status inputs). The gate assembles the contract object around them -- status,
summary, verdict (compute_verdict stays the only verdict authority) -- and runs
the skill's old step 3a quote check itself. These tests drive the real gate behind
the fake kimchi CLI (tests/fake_backend.py -> tests/stub_reviewer.py, whose
STUB_RAW knob answers with raw results instead of a contract object) and assert
only what an operator or the agent can observe: allow/deny, the denial text, the
launches the fake saw. The pure render helpers get plain unit tests.
"""
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
import ocr_render  # noqa: E402
from test_async_gate import _commit, _git, _wait_state, repo  # noqa: E402,F401
from test_claude_profile import _backend_env, _launches, _push, rg  # noqa: E402

_PATH = "feat.txt"
_QUOTE_IN_FILE = "present in the file"


def _finding_env(tmp_path, existing_code, raw="1", severity="medium", confidence=0.9):
    return _backend_env(
        tmp_path, OCR_BACKEND="kimchi", STUB_RAW=raw,
        STUB_FINDINGS_FOR=json.dumps({_PATH: {
            "severity": severity, "confidence": confidence,
            "content": "scripted raw finding", "existing_code": existing_code}}))


def _push_finding(repo, tmp_path, existing_code, **kw):
    """(decision, reason, state, recorded findings) of one pushed finding."""
    tip = _commit(repo, name="feat", msg=_QUOTE_IN_FILE)
    decision, reason = _push(repo, _finding_env(tmp_path, existing_code, **kw))
    st = _wait_state(repo, tip, {"done"})
    with open(st["record"], encoding="utf-8") as fh:
        findings = json.loads(fh.read().splitlines()[-1])["findings"]
    return decision, reason, st, findings


# --- the formatter session is gone --------------------------------------------

def test_a_kimchi_review_launches_exactly_one_session_and_no_formatter(repo, tmp_path):
    _commit(repo)
    env = _backend_env(tmp_path, OCR_BACKEND="kimchi", STUB_RAW="1")
    decision, reason = _push(repo, env)
    assert decision == "pass", reason
    launches = _launches(tmp_path)
    assert len(launches) == 1                       # was reviewer + formatter session
    assert "formatter" not in launches[0]["prompt"].lower()
    assert launches[0]["prompt"].endswith(" --json")


# --- raw shapes the formatter used to absorb ------------------------------------

@pytest.mark.parametrize("raw", ["1", "array", "wrapped", "commas"])
def test_every_raw_shape_renders_and_the_gate_decides_the_verdict(repo, tmp_path, raw):
    decision, reason, st, findings = _push_finding(repo, tmp_path, _QUOTE_IN_FILE, raw=raw)
    assert decision == "deny", reason               # medium at 0.9 confidence blocks
    assert st["verdict"] == "block" and "scripted raw finding" in reason
    assert [f["confidence"] for f in findings] == [0.9]


@pytest.mark.parametrize("raw", ["1", "wrapped", "commas"])
def test_a_clean_raw_answer_passes_in_every_shape(repo, tmp_path, raw):
    _commit(repo)
    decision, reason = _push(repo, _backend_env(
        tmp_path, OCR_BACKEND="kimchi", STUB_RAW=raw))
    assert decision == "pass", reason


def test_garbage_output_still_fails_closed(repo, tmp_path):
    _commit(repo)
    decision, reason = _push(repo, _backend_env(
        tmp_path, OCR_BACKEND="kimchi", FAKE_SCENARIO="garbage"))
    assert decision == "deny"


# --- the quote check (skill step 3a), now in Python ------------------------------

def test_a_quote_missing_from_the_file_downgrades_the_finding(repo, tmp_path):
    decision, reason, st, findings = _push_finding(
        repo, tmp_path, "this text is nowhere in the change")
    # confidence 0.9 - 0.3 = 0.6: below the 0.7 bar, so the medium finding warns
    assert decision == "pass", reason
    assert st["verdict"] == "warn"
    assert [f["confidence"] for f in findings] == [0.6]


def test_the_downgrade_records_a_warning_naming_the_file(repo):
    tip = _commit(repo, name="feat", msg=_QUOTE_IN_FILE)
    base = _git(["rev-parse", "origin/main"], cwd=repo)
    result = {"findings": [
        {"path": _PATH, "existing_code": "not in the change", "confidence": 0.9, "severity": "medium"},
        {"path": _PATH, "existing_code": _QUOTE_IN_FILE, "confidence": 0.9, "severity": "medium"}],
        "warnings": [{"file": None, "message": "earlier"}]}
    out = rg._apply_quote_check(str(repo), tip, base + ".." + tip, result)
    assert [f["confidence"] for f in out["findings"]] == [0.6, 0.9]
    assert out["warnings"] == [
        {"file": None, "message": "earlier"},
        {"file": _PATH, "message": "existing_code not found in diff or file — confidence downgraded"}]


def test_a_quote_present_in_the_file_changes_nothing(repo, tmp_path):
    decision, reason, st, findings = _push_finding(repo, tmp_path, _QUOTE_IN_FILE)
    assert decision == "deny", reason
    assert st["verdict"] == "block" and [f["confidence"] for f in findings] == [0.9]


def test_a_high_finding_with_a_missing_quote_still_blocks(repo, tmp_path):
    decision, reason, st, findings = _push_finding(
        repo, tmp_path, "nowhere in the change", severity="high", confidence=0.95)
    assert decision == "deny", reason
    assert st["verdict"] == "block" and [f["confidence"] for f in findings] == [0.65]


def test_a_contract_object_answer_is_left_alone_by_the_quote_check(repo, tmp_path):
    # Not raw: the stub's default contract object. The gate renders nothing and
    # does not second-guess its findings, so the verdict and confidence stand.
    decision, reason, st, findings = _push_finding(
        repo, tmp_path, "nowhere in the change", raw="")
    assert decision == "deny", reason
    assert st["verdict"] == "block" and [f["confidence"] for f in findings] == [0.9]


# --- dedup stays model-side and threshold-gated ----------------------------------

def _skill():
    with open(os.path.join(_ROOT, "skills", "review", "SKILL.md"), encoding="utf-8") as fh:
        return fh.read()


def test_dedup_is_instructed_only_at_four_or_more_findings():
    skill = _skill()
    assert "## 4. Global dedup (only if ≥ 4 total findings)" in skill
    section = skill.split("## 4. Global dedup", 1)[1].split("\n## 5.", 1)[0]
    assert "same claim" in section
    for lower in ("≥ 2", "≥ 3", ">= 2", ">= 3"):
        assert lower not in section


def test_the_skill_prints_raw_results_and_has_no_formatter_session():
    skill = _skill()
    # the only mention left is the statement that there is none
    assert skill.lower().count("formatter") == skill.lower().count("no formatter")
    assert "status_inputs" in skill


# --- persona drift ----------------------------------------------------------------

def test_appendix_p1_equals_the_generated_copy_of_the_reviewer_agent():
    with open(os.path.join(_ROOT, "agents", "code-reviewer.md"), encoding="utf-8") as fh:
        body = ocr_render.reviewer_persona_body(fh.read())
    p1 = _skill().split("### P.1 ", 1)[1].split("### P.2 ", 1)[0]
    assert body.strip() in p1


# --- ocr_render units ----------------------------------------------------------------

def test_derive_status_rules():
    d = ocr_render.derive_status
    assert d({}) == "success"
    assert d({"warnings": [{"file": "a", "message": "m"}]}) == "completed_with_warnings"
    assert d({"status_inputs": {"subagent_errored": True}, "warnings": [1]}) == "completed_with_errors"
    assert d({"status_inputs": {"files_reviewable": 0}}) == "skipped"
    assert d({"status_inputs": {"files_reviewable": 0},
              "native_deep_evidence": {"task_id": "t"}}) == "success"
    assert d({"status_inputs": {"files_reviewable": 3}}) == "success"


def test_trailing_commas_are_removed_but_commas_inside_strings_are_kept():
    text = '{"a": [1, 2,], "b": "x,]", "c": {"d": 1,},}'
    assert json.loads(ocr_render.strip_trailing_commas(text)) == {
        "a": [1, 2], "b": "x,]", "c": {"d": 1}}
    assert ocr_render.loads_lenient(text)["b"] == "x,]"
    with pytest.raises(ValueError):
        ocr_render.loads_lenient("{not json,}")


def _located(x):
    return isinstance(x, dict) and bool(x.get("path"))


def test_unwrap_accepts_bare_arrays_and_one_wrapper_key_only():
    f = {"path": "a.py", "content": "c"}
    assert ocr_render.unwrap_findings_object([f], _located) == {"findings": [f]}
    assert ocr_render.unwrap_findings_object({"result": {"findings": [f]}}, _located) == {"findings": [f]}
    assert ocr_render.unwrap_findings_object({"findings": [f]}, _located) is None
    assert ocr_render.unwrap_findings_object([], _located) is None
    assert ocr_render.unwrap_findings_object([f, "x"], _located) is None
    assert ocr_render.unwrap_findings_object({"result": {"other": 1}}, _located) is None


def _check(finding, texts):
    return ocr_render.quote_check([finding], lambda p: texts)


def test_quote_check_downgrades_only_a_checkable_missing_quote():
    base = {"path": "a.py", "existing_code": "x = 1", "confidence": 0.9}
    (out,), warns = _check(dict(base), ["y = 2"])
    assert out["confidence"] == 0.6 and len(warns) == 1 and warns[0]["file"] == "a.py"
    (out,), warns = _check(dict(base), ["  x   =\n 1  "])          # whitespace-normalised
    assert out["confidence"] == 0.9 and not warns
    (out,), warns = _check(dict(base), [])                         # unreadable: no verdict on it
    assert out["confidence"] == 0.9 and not warns


@pytest.mark.parametrize("extra", [
    {"category": "documentation"}, {"type": "intent_gap"}, {"impact_site": "s1"},
    {"sibling_of": "x"}, {"dep_task": "t"}, {"docs_kind": "stale_docs"},
    {"comment_kind": "stale_tense"}, {"provenance": "pre_existing"},
])
def test_quote_check_skips_findings_anchored_elsewhere(extra):
    f = dict({"path": "a.py", "existing_code": "x = 1", "confidence": 0.9}, **extra)
    (out,), warns = _check(f, ["y = 2"])
    assert out == f and not warns
