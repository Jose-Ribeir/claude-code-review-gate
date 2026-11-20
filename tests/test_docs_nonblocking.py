"""review-speed 10 + 11: the docs summary never blocks alone; the docs-checker
runs beside the reviewer.

Everything goes through the real gate (hook mode) behind the stub worker
(tests/stub_reviewer.py). The stub plays the reviewer AND the checker's
findings inside one worker session, and the docs-checker itself is a subagent
the review skill spawns, so the gate-level tests see the contract that drives
it: the manifest the worker receives (the diff-based docs_context, the
`docs_checker_parallel` field), the verdict, and the order/overlap of the
summary-generation session against the review session. The skill's spawn
order is pinned against SKILL.md's text.
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import test_docs_generator as dg  # noqa: E402  (same repo/stub plumbing)

_SKILL = os.path.join(os.path.dirname(_HERE), "skills", "review", "SKILL.md")
_DOCS_REL = dg._DOCS_REL
_UNAVAILABLE = "docs summary unavailable"


def _setup(tmp_path, files=None):
    work = dg._repo(tmp_path, files or {"app.py": dg._APP_V1, "README.md": "# project\n"})
    dg._commit(work, {"app.py": dg._APP_V2})
    return work


def _state(work):
    return dg._state(work, dg._git(["rev-parse", "HEAD"], cwd=work))


def _manifest(tmp_path, trace="t.trace"):
    return dg._review_manifest(tmp_path, trace)


# An unknown harness is treated as kimchi, whose inline wait never exceeds 4 s;
# the slower scenarios here (a 6 s generation, two pushes with a parallel
# checker) name the claude harness so OCR_INLINE_BUDGET (30 s) applies.
_CLAUDE = {"OCR_HARNESS": "claude"}


def _settings_file(tmp_path, parallel):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(
        {"behavior": {"docs_checker_parallel": {"enabled": parallel}}}), encoding="utf-8")
    return p


# --- ticket 10: the summary regeneration never blocks alone -----------------

def test_failing_summary_step_does_not_block_and_warns(tmp_path):
    work = _setup(tmp_path)
    decision, reason = dg._push(work, dg._env(
        tmp_path, "t.trace", STUB_DOCS_SUMMARY_VERDICT="fail"))
    assert decision == "pass", reason
    st = _state(work)
    assert st.get("verdict") == "pass" and not st.get("blocked"), st
    assert _UNAVAILABLE in st.get("reasons", ""), st.get("reasons")
    assert "stub: docs summary generation failed" in st["reasons"]
    ctx = _manifest(tmp_path).get("docs_context")
    assert ctx and ctx.get("summary_unavailable") is True, ctx
    assert ctx["content"] == "" and ctx.get("corpus") is not None
    assert not (work / _DOCS_REL).exists(), "a failed generation leaves no artifact"
    assert (st.get("docs_summary") or {}).get("regen_failed"), st


def test_real_docs_finding_still_blocks_without_a_summary(tmp_path):
    work = _setup(tmp_path)
    finding = "contradicts recorded Decision D1: verdict computation lives in Python"
    decision, reason = dg._push(work, dg._env(
        tmp_path, "t.trace", STUB_DOCS_SUMMARY_VERDICT="fail",
        STUB_DOCS_FINDING=json.dumps({"content": finding})))
    assert decision == "deny", reason
    assert finding in reason
    assert _UNAVAILABLE in reason, "the warning rides the blocking verdict too"


def test_the_next_push_retries_the_failed_regeneration(tmp_path):
    work = _setup(tmp_path)
    decision, reason = dg._push(work, dg._env(
        tmp_path, "t1.trace", STUB_DOCS_SUMMARY_VERDICT="fail"))
    assert decision == "pass", reason
    dg._commit(work, {"app.py": dg._APP_V3})
    decision, reason = dg._push(work, dg._env(tmp_path, "t2.trace"))
    assert decision == "pass", reason
    assert len(dg._gen_calls(tmp_path, "t2.trace")) == 1
    assert "Stub-generated summary" in (work / _DOCS_REL).read_text(encoding="utf-8")


def test_successful_regeneration_behaves_as_before(tmp_path):
    work = _setup(tmp_path)
    decision, reason = dg._push(work, dg._env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    st = _state(work)
    assert _UNAVAILABLE not in st.get("reasons", "")
    ctx = _manifest(tmp_path)["docs_context"]
    assert "summary_unavailable" not in ctx
    assert ctx["content"].strip() == dg._STUB_SUMMARY.strip()
    assert (st.get("docs_summary") or {}).get("status") == "fresh"
    assert (work / _DOCS_REL).read_text(encoding="utf-8").startswith("<!-- docs-gate:freshness ")


def test_a_slow_summary_does_not_hold_the_review(tmp_path):
    """The generation session runs beside the review preparation: a summary
    that outlasts the grace is not waited for. The review session starts long
    before the generation session ends, the verdict warns, and the summary
    still lands (fresh) for the next push."""
    work = _setup(tmp_path)
    decision, reason = dg._push(work, dg._env(
        tmp_path, "t.trace", STUB_DOCS_SUMMARY_SLEEP=6, OCR_DOCS_SUMMARY_WAIT=0.5,
        **_CLAUDE))
    assert decision == "pass", reason
    calls = dg._calls(tmp_path, "t.trace")
    gen = next(c for c in calls if c.get("docs_summary"))
    review = next(c for c in calls if c.get("manifest") and not c.get("docs_summary"))
    assert review["ts"] - gen["ts"] < 5, \
        "the review started while the generation (6 s) was still running"
    st = _state(work)
    assert _UNAVAILABLE in st.get("reasons", "")
    assert review["manifest"]["docs_context"].get("summary_unavailable") is True
    assert "Stub-generated summary" in (work / _DOCS_REL).read_text(encoding="utf-8"), \
        "the late summary is written for the next push"
    assert (st.get("docs_summary") or {}).get("status") == "fresh"


def test_the_failure_denial_is_no_longer_fail_closed(tmp_path):
    work = _setup(tmp_path)
    decision, reason = dg._push(work, dg._env(
        tmp_path, "t.trace", STUB_DOCS_SUMMARY_VERDICT="fail"))
    assert decision == "pass" and "--no-verify" not in reason


# --- ticket 11: the docs-checker beside the reviewer ------------------------

def test_manifest_tells_the_skill_to_run_the_checker_in_parallel_by_default(tmp_path):
    work = _setup(tmp_path)
    decision, reason = dg._push(work, dg._env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    assert _manifest(tmp_path).get("docs_checker_parallel") is True


def test_the_switch_restores_the_old_order(tmp_path):
    work = _setup(tmp_path)
    env = dg._env(tmp_path, "t.trace",
                  OCR_SETTINGS_FILE=_settings_file(tmp_path, False))
    decision, reason = dg._push(work, env)
    assert decision == "pass", reason
    assert _manifest(tmp_path).get("docs_checker_parallel") is False


def test_findings_from_the_docs_checker_still_merge_and_block_in_both_modes(tmp_path):
    work = _setup(tmp_path)
    finding = "contradicts recorded Decision D1: verdict computation lives in Python"
    for parallel, trace in ((True, "a.trace"), (False, "b.trace")):
        env = dg._env(tmp_path, trace,
                      OCR_SETTINGS_FILE=_settings_file(tmp_path, parallel),
                      STUB_DOCS_FINDING=json.dumps({"content": finding}),
                      **_CLAUDE,
                      STUB_FINDINGS_FOR=json.dumps(
                          {"app.py": {"severity": "low", "content": "a code nit"}}))
        decision, reason = dg._push(work, env)
        assert decision == "deny", (parallel, reason)
        assert finding in reason
        assert _manifest(tmp_path, trace).get("docs_checker_parallel") is parallel
        if parallel:
            # a fresh tip for the second round so the verdict is recomputed
            dg._commit(work, {"app.py": dg._APP_V3})


def test_no_docs_context_means_no_docs_checker_inputs(tmp_path):
    """A repo whose only document is a changelog has no corpus: no docs
    context, no docs_quality payload, and no parallel field -- nothing for a
    checker to be spawned for."""
    work = _setup(tmp_path, {"app.py": dg._APP_V1, "CHANGELOG.md": "# Changelog\n"})
    decision, reason = dg._push(work, dg._env(tmp_path, "t.trace"))
    assert decision == "pass", reason
    manifest = _manifest(tmp_path)
    assert "docs_context" not in manifest
    assert "docs_quality" not in manifest
    assert "docs_checker_parallel" not in manifest
    assert dg._gen_calls(tmp_path, "t.trace") == []


# --- the review skill: when the checker is spawned ---------------------------

def _skill_section():
    text = open(_SKILL, encoding="utf-8").read()
    start = text.index("**Documentation check (only when the manifest has")
    return text[start:text.index("## 3a. Hallucination check")]


def test_skill_spawns_the_checker_in_the_reviewers_message_unless_switched_off():
    sec = " ".join(_skill_section().split())
    assert "manifest.docs_checker_parallel" in sec
    assert "in the same message as the reviewer" in sec
    assert "Only when `docs_checker_parallel` is `false`" in sec
    assert "after the reviewer's findings are parsed and before §3a" in sec
    # the old unconditional order sentence is gone
    assert "After the reviewer's findings are parsed and before §3a, spawn" not in sec
    assert "docs_context.summary_unavailable" in sec


def test_checker_persona_knows_the_unavailable_summary():
    persona = open(os.path.join(os.path.dirname(_HERE), "agents", "docs-checker.md"),
                   encoding="utf-8").read()
    assert "summary_unavailable" in persona
