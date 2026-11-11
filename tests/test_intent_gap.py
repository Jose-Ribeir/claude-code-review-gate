"""intent_gap: a contracted finding family for intent-vs-diff mismatches (t14).

Schema (spec, Intent findings & the verdict contract):

    {"type": "intent_gap",
     "claim": "<verbatim message line(s)>",
     "against": {"path": "...", "lines": "...", "evidence": "<code/quote>"},
     "severity": "blocking|warning",
     "confidence": 0.0,
     "self_check": {"fix_safe": true, "rationale": "..."}}

Verdict rules pinned here:
  - a self-checked `blocking` intent finding (claimed work entirely absent
    from the changes) takes the blocking path;
  - `warning` intent findings warn, never block -- not even at top confidence;
  - an intent finding whose self-check fails (no quoted claim, no quoted
    contradicting code, fix-safety not shown, or a severity outside
    blocking|warning) is dropped gate-side;
  - the asymmetry invariant: defect findings stand regardless of any intent
    claim (intent adds findings, never suppresses them);
  - `intent_gap` is a FIRST-CLASS contract family (ADR 0001): a verdict
    carrying one validates as-is and is never routed through the improvised-
    family rescue, while genuinely unrecognized improvised shapes keep
    failing closed.
The report text must distinguish intent findings from defect findings.
"""
import importlib.util
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SCRIPTS = os.path.join(_ROOT, "scripts")

sys.path.insert(0, _SCRIPTS)
import ocr_verdict  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "review_gate_intent_gap", os.path.join(_SCRIPTS, "review-gate.py"))
rg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rg)


# --------------------------------------------------------------------------
# Fixtures: intent findings and defect findings over the same change.
# --------------------------------------------------------------------------

def _intent(severity="warning", claim="fixes the retry backoff overflow",
            against=None, fix_safe=True, rationale="code shows no backoff change",
            confidence=0.9):
    return {
        "type": "intent_gap",
        "claim": claim,
        "against": against if against is not None else {
            "path": "retry.py", "lines": "42-50",
            "evidence": "def retry(): time.sleep(1)  # unchanged backoff",
        },
        "severity": severity,
        "confidence": confidence,
        "self_check": {"fix_safe": fix_safe, "rationale": rationale},
    }


def _defect(severity="high", confidence=0.9):
    return {
        "path": "retry.py", "start_line": 42, "end_line": 45,
        "severity": severity, "confidence": confidence,
        "category": "correctness",
        "content": "unhandled TimeoutError escapes the retry loop",
    }


def _verdict(findings, verdict="pass"):
    return {"status": "success", "verdict": verdict, "findings": findings,
            "summary": {"files_reviewed": 1, "findings": len(findings),
                        "high": 0, "medium": 0, "low": 0}}


# --------------------------------------------------------------------------
# Verdict computation: blocking intent finding blocks; warning never does.
# --------------------------------------------------------------------------

def test_blocking_intent_finding_blocks():
    assert ocr_verdict.compute_verdict([_intent(severity="blocking")]) == "block"


def test_blocking_intent_finding_blocks_inside_a_findings_wrapper():
    data = {"findings": [_intent(severity="blocking")], "status": "success"}
    assert ocr_verdict.compute_verdict(data) == "block"


def test_warning_intent_finding_warns_but_never_blocks():
    assert ocr_verdict.compute_verdict([_intent(severity="warning")]) == "warn"


def test_warning_intent_finding_never_blocks_at_full_confidence():
    f = _intent(severity="warning", confidence=1.0)
    assert ocr_verdict.compute_verdict([f]) == "warn"


def test_no_intent_findings_passes():
    assert ocr_verdict.compute_verdict([]) == "pass"


# --------------------------------------------------------------------------
# Gate-side self-check: failures drop the finding entirely.
# --------------------------------------------------------------------------

def _self_check_failures():
    no_claim = _intent(severity="blocking", claim="")
    no_code_quote = _intent(severity="blocking",
                            against={"path": "retry.py", "lines": "42",
                                     "evidence": " "})
    fix_unsafe = _intent(severity="blocking", fix_safe=False)
    no_rationale = _intent(severity="blocking", rationale="")
    no_self_check = _intent(severity="blocking")
    del no_self_check["self_check"]
    bad_severity = _intent(severity="high")  # severities are blocking|warning
    return [no_claim, no_code_quote, fix_unsafe, no_rationale, no_self_check,
            bad_severity]


def test_self_check_failing_intent_finding_is_dropped_for_verdict():
    for bad in _self_check_failures():
        assert ocr_verdict.compute_verdict([bad]) == "pass", bad


def test_self_check_failing_intent_finding_is_dropped_for_the_report():
    """Gate-side normalization: merge drops self-check-failures so they
    surface neither in the verdict nor in the report text."""
    bad = _self_check_failures()
    good = _intent(severity="warning")
    merged = rg._merge_chunk_results([_verdict(bad + [good])])
    remaining = [f for f in merged["findings"]
                 if str(f.get("type")) == "intent_gap"]
    assert remaining == [good]


def test_defect_findings_are_not_dropped_by_the_intent_filter():
    merged = rg._merge_chunk_results(
        [_verdict([_defect()] + _self_check_failures())])
    defect = [f for f in merged["findings"] if f.get("content")]
    assert len(defect) == 1
    assert defect[0]["severity"] == "high"


# --------------------------------------------------------------------------
# Asymmetry invariant: intent can add findings, never remove a defect.
# --------------------------------------------------------------------------

def test_defect_finding_blocks_despite_a_contradicting_intent_claim():
    """A message claiming the behaviour is intended does not launder a defect."""
    intent = _intent(severity="warning",
                     claim="the thrown TimeoutError is intentional")
    assert ocr_verdict.compute_verdict([intent, _defect()]) == "block"


def test_blocking_intent_finding_alongside_a_defect_still_blocks_both_ways():
    data = [_intent(severity="blocking"), _defect(severity="low")]
    assert ocr_verdict.compute_verdict(data) == "block"


# --------------------------------------------------------------------------
# Contract: intent_gap is first-class, not an improvised shape (ADR 0001).
# --------------------------------------------------------------------------

def test_verdict_with_intent_gap_validates_as_contract_not_rescue():
    """A verdict carrying intent_gap is a FIRST-CLASS valid verdict: the
    Claude variant's extractor accepts it straight (it has no near-miss
    rescue path to be routed through)."""
    obj = _verdict([_intent()])
    assert rg._is_valid_verdict(obj)
    assert rg._extract_json(json.dumps(obj)) == obj


def test_merge_preserves_intent_gap_shape_unmangled():
    """The Claude variant has no findings normalizer (ADR 0001's near-miss
    rescue is Kimchi-only); the merge passes an intent_gap finding through
    untouched -- no content-harvesting, no severity aliasing."""
    raw = {"type": "intent_gap", "claim": "fixes the overflow",
           "against": {"path": "a.py", "lines": "1", "evidence": "x"},
           "severity": "blocking", "confidence": 0.9,
           "self_check": {"fix_safe": True, "rationale": "r"}}
    merged = rg._merge_chunk_results([_verdict([raw])])
    intent = [f for f in merged["findings"] if f.get("type") == "intent_gap"]
    assert intent == [raw]
    assert "content" not in intent[0]


def test_unrecognized_improvised_shape_still_rejected():
    assert not rg._is_valid_verdict({"some_axis": {"count": 3}})


# --------------------------------------------------------------------------
# Report text: intent findings are visually distinct from defect findings.
# --------------------------------------------------------------------------

def test_report_marks_intent_findings_and_quotes_the_claim():
    result = _verdict([_intent(severity="blocking")])
    text = rg._format_reasons(result)
    assert "intent" in text.lower()
    assert "fixes the retry backoff overflow" in text


def test_report_distinguishes_intent_from_defect_findings():
    result = _verdict([_intent(severity="warning"), _defect()])
    text = rg._format_reasons(result)
    lines = {ln.strip(): ln for ln in text.splitlines() if ln.strip()}
    intent_lines = [ln for ln in lines if "intent" in ln.lower()]
    defect_lines = [ln for ln in lines if "unhandled TimeoutError" in ln]
    assert intent_lines, text
    assert defect_lines, text
    # The defect line is NOT labelled as an intent finding.
    assert all("intent" not in ln.lower() for ln in defect_lines)


# --------------------------------------------------------------------------
# Contract bits in the reviewer-facing documents (family name + rules).
# --------------------------------------------------------------------------

def test_reviewer_persona_contracts_the_intent_gap_family():
    text = open(os.path.join(_ROOT, "agents", "code-reviewer.md"),
                encoding="utf-8").read()
    assert '"intent_gap"' in text
    for key in ('"claim"', '"against"', '"severity"', '"self_check"'):
        assert key in text
    assert "blocking" in text and "warning" in text


def test_skill_states_the_source_precedence():
    text = open(os.path.join(_ROOT, "skills", "review", "SKILL.md"),
                encoding="utf-8").read()
    assert "intent_gap" in text
    # Source precedence is stated inside the intent-context section:
    # manual requirement text outranks ticket text outranks commit messages.
    section = text[text.find("intent context"):]
    assert section.find("requirement_background") < section.find("ticket")
    assert section.find("ticket") < section.find("commit messages")
