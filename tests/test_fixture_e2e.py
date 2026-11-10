"""Fixture e2e harness -- each miss-category fixture pushed through the REAL
gate (review-gate.py --mode hook) with tests/stub_reviewer.py standing in for
the reviewer via OCR_REVIEWER_CMD (same seam as tests/test_deep_gate.py).

KIMCHI VARIANT notes (t12 parity):
* the gate's async state and verdict store are EXTERNAL (D12): the hook-mode
  subprocess shares the per-test OCR_STATE_DIR conftest sets; hook answers
  use the Kimchi protocol ({"decision": "block", "reason": ...}) parsed by
  tests/hook_output.py;
* verdict semantics differ (D15): this variant blocks high AND medium at
  confidence >= 0.7, not only high. No conflict here -- the stub is scripted
  to STUB_VERDICT=pass (no findings) on every e2e push, so both variants
  would answer "pass".

Where the pipeline-level path matters, these assert that a real push:

* completes with the supervisor's state machine in "done";
* lazily builds/refreshes the .ocr event index with the GATE'S OWN context
  filter (so M1's coupling and M4's pointer utterance are indexed on push);
* puts the context-filter impact search's callers -- INCLUDING the spec
  files -- into the reviewer manifest (M3: the stranded-caller miss,
  caught end to end before any model reads anything).

LLM-graded judgment is stubbed (STUB_VERDICT=pass); what is being proven is
the deterministic context pipeline that feeds it.
"""
import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "fixtures", "miss_categories")
for _p in (_SCRIPTS, _FIXTURES):
    sys.path.insert(0, _p)

import glob
import json

import builders  # noqa: E402


def _push_fixture(info, tmp_path):
    """Run one real push through the gate; return (decision, reason)."""
    env = builders.gate_env(tmp_path)
    decision, reason = builders.push_fixture(info["root"], env)
    st = builders.await_run_state(info["root"], info["tip"])
    assert st.get("state") == "done", st
    assert st.get("verdict") == "pass", st
    return decision, reason


def _telemetry_records(info):
    """The persisted run records of the fixture repo's gate log.

    The Kimchi store is external, but the run log still lands under the
    repo's git common dir (the supervisor recovers it via common_dir_of).
    """
    common = builders.review_gate._git_common_dir(info["root"])
    records = []
    for path in sorted(glob.glob(os.path.join(common, "review-gate-telemetry",
                                              "*.jsonl"))):
        with open(path, encoding="utf-8") as fh:
            records += [json.loads(line) for line in fh if line.strip()]
    return records


class TestM1PushBuildsTheEventIndex:
    def test_gate_index_machinery_ran_on_push(self, tmp_path):
        """The gate's own _refresh_index ran during the push: the run
        record carries this build's tree-sitter capability warning (the
        fixture is .tsx and the grammar is absent here), proving the index
        was built on the reviewed tip with the gate's context filter. The
        coupling itself is asserted by the deterministic fixture test
        (tests/test_fixture_m1_remount.py)."""
        info = builders.build_m1(tmp_path / "work")
        decision, reason = _push_fixture(info, tmp_path)
        assert decision == "pass", reason
        records = _telemetry_records(info)
        assert len(records) == 1
        warnings = records[0].get("index_warnings") or []
        assert any("tree-sitter unavailable" in w for w in warnings), warnings

    def test_stub_reviewed_the_changed_file(self, tmp_path):
        info = builders.build_m1(tmp_path / "work")
        _push_fixture(info, tmp_path)
        manifests = builders.review_manifests(tmp_path)
        assert manifests
        reviewed = {p for m in manifests for p in (m.get("paths") or [])}
        assert info["mount"] in reviewed
        assert info["filter"] not in reviewed  # untouched: never a diff target


class TestM2PushReview:
    def test_gate_completes_and_reviews_the_violating_file(self, tmp_path):
        info = builders.build_m2(tmp_path / "work")
        decision, reason = _push_fixture(info, tmp_path)
        assert decision == "pass", reason
        manifests = builders.review_manifests(tmp_path)
        reviewed = {p for m in manifests for p in (m.get("paths") or [])}
        assert info["src"] in reviewed
        # The doc is never a review target even on a v2 push -- it reaches
        # the reviewer only through the convention resolver (fixture test).
        assert info["doc"] not in reviewed


class TestM3PushImpactManifest:
    def test_reviewer_manifest_carries_all_eleven_spec_callers(self, tmp_path):
        """Pipeline-level proof of the t01 fix: the gate ran its impact
        search under the CONTEXT filter and the reviewer was handed the
        spec callers the pre-v2 gate threw away."""
        info = builders.build_m3(tmp_path / "work")
        decision, reason = _push_fixture(info, tmp_path)
        assert decision == "pass", reason
        manifests = [m for m in builders.review_manifests(tmp_path)
                     if (m.get("impact") or {}).get("sites")]
        assert manifests, "no reviewer manifest carried impact sites"
        impact = manifests[0]["impact"]
        sites = impact["sites"]
        spec_calls = [s for s in sites
                      if s["path"].startswith("tests/")]
        assert len(spec_calls) == info["spec_callers"] == 11
        assert {s["path"] for s in spec_calls} == set(info["spec_files"])
        symbol_names = {s.get("name") for s in (impact.get("symbols") or [])}
        assert info["symbol"] in symbol_names

    def test_reviewed_paths_exclude_the_spec_callers(self, tmp_path):
        """The context/review split, as the gate applied it: the callers are
        context, never review targets."""
        info = builders.build_m3(tmp_path / "work")
        _push_fixture(info, tmp_path)
        reviewed = {p for m in builders.review_manifests(tmp_path)
                    for p in (m.get("paths") or [])}
        assert info["helper"] in reviewed
        assert not any(p.startswith("tests/") for p in reviewed)


class TestM4PushIndexesThePointer:
    def test_pointer_text_reaches_the_reviewer_input(self, tmp_path):
        """The pushed diff -- containing the dangling pointer string --
        reached the reviewer verbatim (stub-traced item files), and the run
        record proves the pipeline completed over it."""
        info = builders.build_m4(tmp_path / "work")
        decision, reason = _push_fixture(info, tmp_path)
        assert decision == "pass", reason
        seen = ""
        for call in builders.stub_calls(tmp_path):
            seen += "\n".join(str(v) for v in (call.get("item_files") or {}).values())
        assert info["target"] in seen
        records = _telemetry_records(info)
        assert len(records) == 1 and records[0]["verdict"] == "pass"
