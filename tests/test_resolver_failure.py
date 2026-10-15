"""A resolver FAILURE (timeout, non-zero exit, unparseable output, crash) is its
own status, not an answer without evidence.

* the failed call is retried once with half the batch (each half its own call);
* ids still failing keep BLOCKING (fail closed), with a distinct "resolver
  unavailable" message instead of "unverified", and are not re-checked;
* a failed prior is never re-written as a fresh finding: the record keeps it
  with its original target_oid, so the next push re-judges it from there.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_resolver_robustness import (  # noqa: E402  (also puts scripts/ on sys.path)
    _FID, _amend, _blocked_on_x, _env, _git, _hook, _prior, _state, _trace,
    review_gate,
)
import ocr_telemetry  # noqa: E402

_RESOLVED = {"status": "resolved", "evidence_path": "x.py",
             "evidence_quote": "def good(): pass"}


def _resolve_calls(tmp_path, name):
    return [c for c in _trace(tmp_path, name) if c.get("resolve_file")]


def _records_for(work, path):
    """Every ledger record that names `path`, across the whole ledger."""
    common = review_gate._git_common_dir(str(work))
    out = []
    for f in sorted(review_gate._ledger_dir(common).rglob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and data.get("path") == path and "findings" in data:
            out.append(data)
    return out


# ---------------------------------------------------------------------------
# Unit: the status and the half-batch retry
# ---------------------------------------------------------------------------

def _fake_resolver(monkeypatch, tmp_path, script):
    """Replace the model call. `script(call_no, ids)` returns a resolutions map,
    or raises to simulate a failure. Returns the list of id-batches it saw."""
    calls = []

    def _fake_review(*a, **kw):
        with open(kw["resolve_file"], encoding="utf-8") as fh:
            ids = [p["id"] for p in json.load(fh)["resolve"]]
        calls.append(ids)
        return {"resolutions": script(len(calls), ids)}, True, ""

    monkeypatch.setattr(review_gate, "_run_review", _fake_review)
    (tmp_path / review_gate.ASYNC_DIR).mkdir(exist_ok=True)
    return calls


def _call(tmp_path, priors):
    items = [{"mode": "delta", "from_oid": "1" * 40,
              "entry": {"path": "x.py", "new_oid": "2" * 40}}]
    return review_gate._run_resolver(
        ".", "hook", "", "tip", "b..t", priors, items, str(tmp_path), "fp", "run1")


def _ok(ids):
    return {i: {"status": "still_present", "evidence_path": "", "evidence_quote": ""}
            for i in ids}


def test_a_failed_call_is_retried_with_half_the_batch_and_resolves_normally(
        monkeypatch, tmp_path):
    ids = [c * 64 for c in "abcd"]

    def script(n, batch):
        if n == 1:
            raise review_gate.ReviewGateError("resolver timed out")
        return {i: _RESOLVED for i in batch}

    calls = _fake_resolver(monkeypatch, tmp_path, script)
    res, warnings = _call(tmp_path, [_prior(i) for i in ids])
    assert [len(c) for c in calls] == [4, 2, 2]           # the halves partition the batch
    assert sorted(sum(calls[1:], [])) == sorted(ids)
    assert {i: res[i]["status"] for i in ids} == {i: "resolved" for i in ids}
    assert all(res[i]["evidence_quote"] == "def good(): pass" for i in ids)
    assert any("retried with half the batch" in w for w in warnings)


def test_only_the_half_that_fails_twice_is_marked_failed(monkeypatch, tmp_path):
    ids = [c * 64 for c in "abcd"]
    state = {}

    def script(n, batch):
        if n == 1:
            raise review_gate.ReviewGateError("exit 1")
        if n == 2:
            state["bad"] = set(batch)
            raise RuntimeError("took too long")      # not a ReviewGateError: a crash
        return _ok(batch)

    calls = _fake_resolver(monkeypatch, tmp_path, script)
    res, warnings = _call(tmp_path, [_prior(i) for i in ids])
    assert len(calls) == 3
    failed = {i for i in ids if res[i]["status"] == "failed"}
    assert failed == state["bad"] and len(failed) == 2
    assert all("took too long" in res[i]["reason"] for i in failed)
    assert all(res[i]["status"] == "still_present" for i in set(ids) - failed)
    assert any("took too long" in w for w in warnings)


def test_a_single_prior_that_fails_is_retried_once_then_failed(monkeypatch, tmp_path):
    def script(n, batch):
        raise review_gate.ReviewGateError("resolver exited 1")

    calls = _fake_resolver(monkeypatch, tmp_path, script)
    res, _ = _call(tmp_path, [_prior()])
    assert len(calls) == 2
    assert res["a" * 64]["status"] == "failed"
    assert res["a" * 64]["reason"] == "resolver exited 1"


def test_limit_and_budget_errors_reach_the_outer_handler_and_are_not_retried(
        monkeypatch, tmp_path):
    for exc in (review_gate.ReviewLimitError("usage limit", resets_at=123.0),
                review_gate.ReviewBudgetError("budget gone")):
        def script(n, batch, _exc=exc):
            raise _exc

        calls = _fake_resolver(monkeypatch, tmp_path, script)
        ids = [c * 64 for c in "abcd"]
        try:
            _call(tmp_path, [_prior(i) for i in ids])
        except type(exc) as got:
            assert got is exc
        else:
            raise AssertionError(f"{type(exc).__name__} was swallowed into a failed verdict")
        assert len(calls) == 1          # no half-batch retry against an exhausted limit


def test_a_resolver_answer_without_evidence_is_not_a_failure(monkeypatch, tmp_path):
    calls = _fake_resolver(monkeypatch, tmp_path, lambda n, batch: {batch[0]: True})
    res, _ = _call(tmp_path, [_prior()])
    assert len(calls) == 1                       # no retry
    assert res["a" * 64]["status"] == "still_present"


def test_judging_a_failed_status_never_decides_anything(tmp_path):
    failed = review_gate._failed("resolver exited 1")
    assert review_gate._judge_resolution(
        _prior(), failed, [], "", str(tmp_path), "tip") == "failed"


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

def test_resolver_failing_twice_blocks_with_a_distinct_message_and_does_not_pollute_records(
        tmp_path):
    work, blob1 = _blocked_on_x(tmp_path)
    tip2 = _amend(work, {"x.py": "x = 1\ndef good(): pass\n"})
    common = review_gate._git_common_dir(str(work))

    decision, reason = _hook(work, _env(tmp_path, "t2.trace", STUB_RESOLVE_VERDICT="fail"))
    assert _state(work, tip2).get("state") == "done"
    assert decision == "deny", reason
    assert "resolver unavailable" in reason and "unverified" not in reason, reason
    # One call, one retry of the half batch -- and no recheck on top.
    calls = _resolve_calls(tmp_path, "t2.trace")
    assert len(calls) == 2
    assert not any(c["resolve_manifest"].get("recheck") for c in calls)

    # Telemetry says the resolver failed for this prior.
    last = ocr_telemetry.load(common, 1)[-1]
    assert last["resolver"]["failed"] == 1
    assert list(last["resolver"]["judged"].values()) == ["failed"]
    assert last["resolver"]["failed_reason"]

    # Nothing was decided: no resolution written, and the prior is not a fresh
    # finding of the new blob -- every copy still points at the blob it was raised on.
    assert not list(review_gate._ledger_dir(common).rglob("resolutions/*.json"))
    carried = [f for r in _records_for(work, "x.py") for f in r["findings"]
               if f.get("id") == _FID]
    assert carried, "the prior must stay owed in the ledger"
    assert all(f.get("target_oid") == blob1 for f in carried), carried

    # The next push (resolver healthy again) re-judges it from the original blob.
    _amend(work, {"x.py": "x = 1\ndef good(): pass\ny = 2\n"})
    decision, reason = _hook(work, _env(tmp_path, "t3.trace",
                                        STUB_RESOLVE=json.dumps({_FID: _RESOLVED})))
    assert decision == "pass", reason
    spec = _resolve_calls(tmp_path, "t3.trace")[0]["resolve_manifest"]["prior_files"]
    assert spec and spec[0]["from_oid"] == blob1


def test_a_half_batch_retry_that_succeeds_resolves_end_to_end(tmp_path):
    work, _ = _blocked_on_x(tmp_path)
    tip2 = _amend(work, {"x.py": "x = 1\ndef good(): pass\n"})
    # The resolver runs before the review (siblings need its verdicts): call 1 is the
    # resolver (made to fail), call 2 its retry, call 3 the review.
    env = _env(tmp_path, "t2.trace", STUB_FAIL_ON_CALL=1,
               STUB_RESOLVE=json.dumps({_FID: _RESOLVED}))
    decision, reason = _hook(work, env)
    assert _state(work, tip2).get("state") == "done"
    assert decision == "pass", reason
    assert len(_resolve_calls(tmp_path, "t2.trace")) == 2


def test_a_carried_prior_without_a_target_keeps_the_blob_of_the_record_it_came_from():
    """Records from before target_oid was stamped: carrying them into the new
    file state must not make that new state the finding's origin."""
    old = {"path": "x.py", "severity": "high", "content": "c", "existing_code": "def bad(): pass"}
    item = {"entry": {"path": "x.py"}, "record": {"head_oid": "1" * 40, "findings": [old]}}
    out = review_gate._record_findings_for(item, {}, [], {"x.py"})
    assert [f["target_oid"] for f in out] == ["1" * 40]
