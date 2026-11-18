"""Public runtime integration for bounded SPEC-16 final-review observability."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rg_final_review  # noqa: E402
from rg_final_store import FinalStore  # noqa: E402
from rg_final_telemetry import MAX_EVENTS, MAX_SERIALIZED_BYTES  # noqa: E402
from test_final_complete_standalone import (  # noqa: E402
    Provider as StandaloneProvider,
    standalone as standalone,
    run as run_standalone,
    stop_result,
    worker_result,
)
from test_final_publication import (  # noqa: E402
    ScriptedGlab,
    publish_individual,
    ready_run,
    store as store,
)
from test_final_stack import Harness as StackHarness, Provider as StackProvider  # noqa: E402


def _events(report, kind=None):
    events = report["observability"]["events"]
    return [event for event in events if kind is None or event["kind"] == kind]


def _assert_bounded_sanitized_detached(store_path, run_id, report):
    observability = report["observability"]
    encoded = json.dumps(observability, sort_keys=True, separators=(",", ":"))
    assert len(encoded.encode("utf-8")) <= MAX_SERIALIZED_BYTES
    assert len(observability["events"]) <= MAX_EVENTS
    assert observability["schema"] == "final-review-telemetry/v1"
    assert observability == report["run"]["metadata"]["observability"]
    for forbidden in (
        "Correct the return value", "python -m unittest", "Implement stack item",
        "scripted required suite", "diff --git", "glpat-", "Bearer ", "PRIVATE KEY",
    ):
        assert forbidden not in encoded
    accepted = observability["counters"]["accepted"]
    observability["counters"]["accepted"] = -1
    if observability["events"]:
        observability["events"][0]["kind"] = "mutated"
    with FinalStore(store_path) as reopened:
        detached = reopened.machine_report(run_id)["observability"]
        assert detached["counters"]["accepted"] == accepted
        assert all(event["kind"] != "mutated" for event in detached["events"])


def test_real_standalone_persists_graph_run_lens_and_test_observability(standalone):
    store_path = standalone.tmp_path / "standalone-observability.sqlite3"

    output = run_standalone(standalone, StandaloneProvider(), store_path)

    assert output["state"] == "pass"
    with FinalStore(store_path) as final_store:
        report = final_store.machine_report(output["run_id"])
        assert report["freshness"]["state"] == "fresh"
        assert report["observability"]["counters"]["rejected"] == 0
        assert _events(report, "graph_discovery")[0]["status"] == "validated"
        assert _events(report, "store_identity")[0]["run_id"] == output["run_id"]
        assert {event["unit_key"] for event in _events(report, "unit_coverage")} == {
            "individual:1", "combined",
        }
        assert any(event["status"] == "pass" for event in _events(report, "unit_coverage"))
        assert len(_events(report, "lens_coverage")) == 14
        assert {event["execution"] for event in _events(report, "test_coverage")} == {
            "required", "dynamic",
        }
        machine_report = json.loads(Path(output["machine_report_path"]).read_text())
        assert machine_report["observability"] == report["observability"]
        _assert_bounded_sanitized_detached(store_path, output["run_id"], report)


def test_real_stack_persists_stale_freshness_and_bounded_run_snapshot(tmp_path):
    harness = StackHarness(tmp_path)
    original_root = harness.items[0]["target"]["sha"]

    class MovingRootProvider(StackProvider):
        def read(self, reference, repo_root=None):
            current = super().read(reference, repo_root=repo_root)
            if len(self.reads) > len(self.items) and current["target"]["branch"] == "main":
                current["target"] = deepcopy(current["target"])
                current["target"]["sha"] = harness.current_sha
                current["target"]["base_sha"] = harness.current_sha
            return current

    harness.provider = MovingRootProvider(harness.items)
    store_path = tmp_path / "stack-observability.sqlite3"

    output = harness.run(store_path)

    assert output["state"] == "stale"
    assert output["freshness"]["restart_required"] is True
    with FinalStore(store_path) as final_store:
        report = final_store.machine_report(output["run_id"])
        assert report["observability"]["counters"]["rejected"] == 0
        assert _events(report, "graph_discovery")[0]["supplied_count"] == 3
        assert _events(report, "store_identity")[0]["store_instance_id"] == \
            final_store.store_instance_id
        assert {event["unit_key"] for event in _events(report, "unit_coverage")} == {
            "individual:1", "individual:2", "individual:3", "combined", "prospective",
        }
        assert {event["lens"] for event in _events(report, "lens_coverage")} >= {
            "correctness", "testing", "documentation/domain", "security", "change-intent",
        }
        assert {event["execution"] for event in _events(report, "test_coverage")} == {
            "required", "dynamic",
        }
        stale = _events(report, "stale_outcome")
        assert len(stale) == 1
        assert stale[0]["changed_count"] == 2
        assert stale[0]["restart_required"] is True
        assert stale[0]["pass_publication_allowed"] is False
        assert json.loads(Path(output["machine_report_path"]).read_text())[
            "observability"] == report["observability"]
        assert _events(report, "graph_discovery")[0]["error_code"] == ""
        assert original_root != harness.current_sha
        _assert_bounded_sanitized_detached(store_path, output["run_id"], report)


def test_publication_failure_and_superseded_run_persist_degradation(store):
    old = ready_run(store, continued=False)
    new = ready_run(store, continued=False)

    old_result = publish_individual(store, ScriptedGlab(), old)
    failed = publish_individual(
        store, ScriptedGlab((None, "403 Forbidden: scripted status permission denied")), new,
    )

    assert old_result["status"] == "superseded"
    assert failed["status"] == "local_only"
    old_report = store.machine_report(old["run_id"])
    new_report = store.machine_report(new["run_id"])
    assert old_report["run"]["superseded_by_run_id"] == new["run_id"]
    assert any(event["superseded_by_run_id"] == new["run_id"]
               for event in _events(old_report, "run_supersession"))
    assert any(event["status"] == "superseded" and event["reason"] == "run-superseded"
               for event in _events(old_report, "publication_degradation"))
    degradation = _events(new_report, "publication_degradation")
    assert [(event["status"], event["reason"]) for event in degradation] == [
        ("local_only", "publication-provider-write-failed"),
    ]
    assert degradation[0]["local_only"] is True
    assert degradation[0]["enforced"] is degradation[0]["official_green"] is False
    assert "403 Forbidden" not in json.dumps(new_report["observability"])


@pytest.mark.parametrize("outcome", ["validated", "rejected"])
def test_real_validated_and_rejected_stops_are_visible(standalone, outcome):
    def stopping_worker(request, **kwargs):
        if request["unit_key"] == "individual:1" and request["lens"] == "correctness":
            if outcome == "validated":
                return stop_result()
            result = worker_result()
            result.update({"status": "STOP", "stop": {"reason_code": "invalid-stop"}})
            return result
        return worker_result()

    standalone.monkeypatch.setattr(rg_final_review, "invoke_worker", stopping_worker)
    store_path = standalone.tmp_path / f"{outcome}-stop-observability.sqlite3"

    output = run_standalone(standalone, StandaloneProvider(), store_path)

    with FinalStore(store_path) as final_store:
        report = final_store.machine_report(output["run_id"])
        stops = _events(report, "structured_stop")
        assert stops
        assert all(stop["outcome"] == outcome for stop in stops)
        if outcome == "validated":
            assert len(stops) == 1
        if outcome == "validated":
            assert stops[0]["reason_code"] == "unsafe-sandbox"
            assert stops[0]["category"] == "unsafe"
            assert stops[0]["affected_units"] == 2
            assert output["stops"]
        else:
            assert stops[0]["reason_code"] == "structured-stop-rejected"
            assert stops[0]["affected_units"] == stops[0]["skipped_stages"] == 0
            assert output["rejected_stops"]
