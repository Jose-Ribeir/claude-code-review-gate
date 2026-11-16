"""comment-gate: the deep-reviewer comment-consistency class (false_citation /
stale_tense / contract_divergence) is non-blocking by construction.

A finding tagged `comment_kind` claims a comment or docstring disagrees with
the code it annotates -- a low-severity nit, never a code defect. The "always
low" rule is enforced by ocr_verdict.comment_kind skip in compute_verdict
(the machine, not the prompt), so a tagged finding can never block at any
severity, any confidence, or any OCR_BLOCK_SEVERITY tuning. Findings that
arrive without their verbatim proof (existing_code: the quoted comment text;
evidence: the contradicting code or search) are still displayed -- nothing is
ever dropped -- but the GATE stamps them "unverified" in the report.

These unit tests pin the verdict cap, the tag contract (exact contracted
values only), the stamp, and isolation: untagged findings (D15) and the
docs-drift two-quote rule behave byte-identically to before the class existed.
"""
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")

sys.path.insert(0, _SCRIPTS)
import ocr_verdict  # noqa: E402

_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_spec = importlib.util.spec_from_file_location("review_gate_cg", _GATE)
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)

STAMP = "[unverified: no verbatim quotes attached]"


def _finding(**over):
    f = {
        "path": "scripts/foo.py",
        "start_line": 12,
        "end_line": 14,
        "severity": "low",
        "confidence": 0.8,
        "category": "maintainability",
        "content": "comment says the retry cap is 3; the code retries 5 times",
        "comment_kind": "contract_divergence",
        "existing_code": "# retries at most 3 times",
        "evidence": "for attempt in range(5):",
    }
    f.update(over)
    return f


# --- verdict cap ------------------------------------------------------------

def test_tagged_high_never_blocks():
    f = _finding(severity="high", confidence=1.0)
    assert ocr_verdict.compute_verdict({"findings": [f]}) == "pass"
    # control: the identical finding without the tag blocks (D15).
    f.pop("comment_kind")
    assert ocr_verdict.compute_verdict({"findings": [f]}) == "block"


def test_tagged_medium_at_high_confidence_never_blocks():
    f = _finding(severity="medium", confidence=0.95)
    assert ocr_verdict.compute_verdict({"findings": [f]}) == "pass"
    # control: untagged medium at the block bar blocks.
    f.pop("comment_kind")
    assert ocr_verdict.compute_verdict({"findings": [f]}) == "block"


def test_tagged_finding_never_even_warns():
    # A comment nit is exactly a low: displayed, but it does not lift the
    # review to the warn tier.
    assert ocr_verdict.compute_verdict({"findings": [_finding()]}) == "pass"


def test_cap_survives_low_block_severity_tuning(monkeypatch):
    # The cap is by construction, not by the D15 thresholds: even with the
    # blocking bar lowered to 'low', a tagged finding cannot block.
    monkeypatch.setenv("OCR_BLOCK_SEVERITY", "low")
    assert ocr_verdict.compute_verdict({"findings": [_finding()]}) == "pass"
    # control: the tuning is live -- an untagged low now blocks.
    f = _finding()
    f.pop("comment_kind")
    assert ocr_verdict.compute_verdict({"findings": [f]}) == "block"


# --- the tag contract -------------------------------------------------------

def test_tag_is_normalized():
    for tag in ("stale_tense", "Stale-Tense", " FALSE_CITATION ", "contract-divergence"):
        f = _finding(severity="high", comment_kind=tag)
        assert ocr_verdict.comment_kind(f) is not None
        assert ocr_verdict.compute_verdict({"findings": [f]}) == "pass"


def test_unknown_tag_is_no_tag():
    # An improvised or misspelt tag keeps the finding's full strictness --
    # the conservative direction: it can block.
    for tag in ("stale_citation", "drift", "stale_docs", "1", "true"):
        f = _finding(severity="high", confidence=1.0, comment_kind=tag)
        assert ocr_verdict.comment_kind(f) is None
        assert ocr_verdict.compute_verdict({"findings": [f]}) == "block"


# --- the unverified stamp ---------------------------------------------------

def _report(findings):
    return review_gate._format_reasons({"findings": findings})


def test_proven_finding_gets_no_stamp():
    out = _report([_finding()])
    assert STAMP not in out
    assert "scripts/foo.py:12-14" in out
    assert "retries 5 times" in out


def test_missing_either_quote_gets_stamped():
    for missing in ("existing_code", "evidence"):
        f = _finding()
        del f[missing]
        out = _report([f])
        assert STAMP in out, f"missing {missing} must stamp"
        assert "scripts/foo.py:12-14" in out  # still displayed, never dropped


def test_empty_and_null_quotes_stamp_without_crashing():
    f = _finding(existing_code=None, evidence="")
    out = _report([f])
    assert STAMP in out
    assert ocr_verdict.compute_verdict({"findings": [_finding(severity="high", existing_code=None, evidence=None)]}) == "pass"


# --- isolation ---------------------------------------------------------------

def test_untagged_findings_in_the_same_review_still_block():
    tagged = _finding(severity="high", confidence=1.0)
    blocker = {"path": "scripts/foo.py", "start_line": 40, "end_line": 41,
               "severity": "high", "confidence": 0.9, "category": "security",
               "content": "sql injection"}
    assert ocr_verdict.compute_verdict({"findings": [tagged, blocker]}) == "block"


def test_tagged_finding_with_missing_content_still_capped():
    # The dropped-field credibility rules (_is_actionable) keep applying;
    # the class cap removes any interaction worth worrying about.
    f = _finding(severity="high", confidence=1.0, content="")
    assert ocr_verdict.compute_verdict({"findings": [f]}) == "pass"


def test_docs_drift_strictness_unchanged():
    # docs-gate 04's two-quote rule keys off docs_kind, a different tag on a
    # different truth: a drift finding missing its passage still downgrades,
    # and comment_kind fields do not affect it.
    drift = {"path": "README.md", "start_line": 3, "end_line": 4,
             "severity": "high", "confidence": 0.95, "category": "documentation",
             "content": "install script renamed", "docs_kind": "stale_docs",
             "existing_code": "install-git-hook.sh"}
    assert ocr_verdict.compute_verdict({"findings": [drift]}) == "warn"
    drift["stale_passage"] = "run install.sh"
    assert ocr_verdict.compute_verdict({"findings": [drift]}) == "block"
