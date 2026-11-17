"""Vertical SPEC-16 final-status publication and same-store ordering tests.

Every provider write is a scripted in-memory double. No test invokes glab,
opens a network connection, or mutates a real provider.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qs

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rg_final_memory as memory  # noqa: E402
from rg_final_publish import STATUS_CONTEXT, FinalPublisher  # noqa: E402
from rg_final_store import FinalStore  # noqa: E402


SOURCE = "a" * 40
TARGET = "b" * 40
BASE = "c" * 40
REPORT_URL = "https://reports.example.test/final/run-2/individual-1"


def provider_item(number=123, source_sha=SOURCE, target_sha=TARGET):
    return {
        "provider": "gitlab",
        "id": f"gitlab:111!{number}",
        "iid": number,
        "url": f"https://gitlab.example.test/group/project/-/merge_requests/{number}",
        "title": f"Pinned change {number}",
        "source": {
            "project_id": 222,
            "project_path": "fork/project",
            "branch": f"feature-{number}",
            "sha": source_sha,
        },
        "target": {
            "project_id": 111,
            "project_path": "group/project",
            "branch": "main",
            "sha": target_sha,
            "base_sha": BASE,
        },
    }


def stack(*items):
    items = items or (provider_item(),)
    return {
        "provider": "gitlab",
        "target_project": "group/project",
        "root_target_ref": "main",
        "root_target_sha": items[0]["target"]["sha"],
        "items": [
            {
                "item_key": str(item["iid"]),
                "reference": item["url"],
                "source_project": item["source"]["project_path"],
                "source_ref": item["source"]["branch"],
                "source_sha": item["source"]["sha"],
                "target_project": item["target"]["project_path"],
                "target_ref": item["target"]["branch"],
                "target_sha": item["target"]["sha"],
                "base_sha": item["target"]["base_sha"],
                "provider_data": {
                    "provider": "gitlab",
                    "id": item["id"],
                    "iid": item["iid"],
                    "url": item["url"],
                    "source": {"project_id": item["source"]["project_id"]},
                    "target": {"project_id": item["target"]["project_id"]},
                },
            }
            for item in items
        ],
    }


class ScriptedGlab:
    """A write transport which records argv and returns scripted responses."""

    def __init__(self, *responses):
        self.responses = list(responses) or [(json.dumps({"id": 9001}), None)]
        self.calls = []

    def __call__(self, argv, cwd, timeout):
        self.calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout})
        response = self.responses.pop(0) if self.responses else (json.dumps({"id": 9001}), None)
        if isinstance(response, BaseException):
            raise response
        return response

    @property
    def fields(self):
        fields = {}
        for argument in self.calls[-1]["argv"]:
            if argument.startswith("state=") or argument.startswith("context=") or \
                    argument.startswith("description=") or argument.startswith("target_url="):
                key, value = argument.split("=", 1)
                fields[key] = value
        return fields


@pytest.fixture
def store(tmp_path):
    with FinalStore(tmp_path / "final.sqlite3") as opened:
        yield opened


def begin(store, *items):
    return store.start_run(stack(*items))


def mark_scope_complete(store, identity):
    for unit_key, unit_id in identity["unit_ids"].items():
        store.record_coverage(unit_id, "repository", "complete", "Immutable full tree")
        store.record_coverage(unit_id, "mandatory_lenses", "complete", "All lenses completed")
        store.record_coverage(unit_id, "required_tests", "absent", "No declared suite")
        store.record_coverage(unit_id, "dynamic_tests", "not_applicable", "No applicable probe")
        if unit_key == "combined":
            store.record_coverage(unit_id, "composition", "complete", "Ordered composition")


def record_freshness(store, identity, state="fresh"):
    pinned = store.pinned_revisions(identity["run_id"])
    assessment = memory.compare_revision_tuples(pinned, pinned)
    if state != "fresh":
        assessment["state"] = state
        assessment["fresh"] = False
        if state == "stale":
            assessment["changes"] = [{
                "field": "source.sha", "expected": SOURCE, "current": "d" * 40,
            }]
        else:
            assessment["errors"] = [{"code": "provider-read-failed", "message": "unavailable"}]
    return store.record_freshness(identity["run_id"], assessment)


def finish(store, identity, state="pass", *, trusted=True):
    for unit_id in identity["unit_ids"].values():
        store.set_unit_state(unit_id, state)
    return store.finish_run(
        identity["run_id"], state,
        trust={
            "local_only": not trusted,
            "enforced": trusted,
            "official": trusted,
            "trusted_orchestrator": trusted,
        },
    )


def ready_run(store, *, state="pass", trusted=True, continued=True, fresh=True,
              complete=True, items=()):
    if continued:
        # The first run establishes same-store continuity. The reviewed run has
        # identical SHAs, proving ordering never relies on revision movement.
        begin(store, *items)
    identity = begin(store, *items)
    if complete:
        mark_scope_complete(store, identity)
    finish(store, identity, state, trusted=trusted)
    if fresh:
        record_freshness(store, identity)
    return identity


def publish_individual(store, transport, identity, **kwargs):
    publisher = FinalPublisher(
        store, runner=transport, trusted_orchestrator=True,
    )
    return publisher.publish_individual(
        identity["run_id"], identity["unit_ids"]["individual:1"], **kwargs,
    )


def test_official_individual_publication_uses_dedicated_gitlab_status_and_identity(
        store, tmp_path):
    identity = ready_run(store)
    transport = ScriptedGlab((json.dumps({
        "id": 812, "status": "success", "name": STATUS_CONTEXT, "sha": SOURCE,
    }), None))

    result = publish_individual(
        store, transport, identity, report_url=REPORT_URL,
    )

    assert result["status"] == "published"
    assert result["provider_state"] == "success"
    assert result["local_only"] is False
    assert result["enforced"] is True
    assert result["official"] is True
    assert result["official_green"] is True
    assert result["error"] == ""
    assert result["decision"]["can_publish_pass"] is True
    assert result["decision"]["finding_verdict"] == "pass"
    assert result["identity"] == {
        "store_instance_id": store.store_instance_id,
        "run_id": identity["run_id"],
        "run_seq": 2,
        "stack_key": identity["stack_key"],
        "contract_version": identity["contract_version"],
        "unit_id": identity["unit_ids"]["individual:1"],
        "unit_key": "individual:1",
        "item_key": "123",
        "source_project": 222,
        "source_ref": "feature-123",
        "head_sha": SOURCE,
        "target_project": 111,
        "target_ref": "main",
        "target_sha": TARGET,
        "base_sha": BASE,
    }
    assert result["report"]["findings"] == 0
    assert result["report"]["completed_units"] == 2
    assert result["report"]["total_units"] == 2
    assert result["report"]["freshness"] == "fresh"

    assert len(transport.calls) == 1
    call = transport.calls[0]
    argv = call["argv"]
    assert argv[:2] == ["glab", "api"]
    assert "--method" in argv and argv[argv.index("--method") + 1] == "POST"
    assert f"projects/222/statuses/{SOURCE}" in argv
    assert call["cwd"] is None
    assert 0 < call["timeout"] <= 30
    fields = transport.fields
    assert fields["state"] == "success"
    assert fields["context"] == STATUS_CONTEXT == "review-gate/final-review"
    assert fields["target_url"] == REPORT_URL
    assert len(fields["description"]) <= 255
    for expected in (
        identity["run_id"], identity["unit_ids"]["individual:1"],
        store.store_instance_id, SOURCE, "individual:1", "findings=0", "stops=0",
    ):
        assert expected in fields["description"]

    attempts = store.machine_report(identity["run_id"])["publications"]
    assert len(attempts) == 1
    assert attempts[0]["publication_id"] == result["publication_id"]
    assert attempts[0]["unit_id"] == identity["unit_ids"]["individual:1"]
    assert attempts[0]["item_key"] == "123"
    assert attempts[0]["status"] == "published"
    assert attempts[0]["status_context"] == STATUS_CONTEXT
    assert attempts[0]["details"]["identity"] == result["identity"]
    assert attempts[0]["details"]["provider_status_id"] == 812
    assert attempts[0]["details"]["report"] == result["report"]


def test_combined_decision_is_publishable_per_participating_item(store):
    first = provider_item(123)
    second = provider_item(124, source_sha="e" * 40, target_sha=SOURCE)
    identity = ready_run(store, items=(first, second))
    transport = ScriptedGlab((json.dumps({"id": 813, "status": "success"}), None))
    publisher = FinalPublisher(store, runner=transport, trusted_orchestrator=True)

    result = publisher.publish_combined(
        identity["run_id"], item_key="124", report_url=REPORT_URL,
    )

    assert result["official_green"] is True
    assert result["identity"]["unit_id"] == identity["unit_ids"]["combined"]
    assert result["identity"]["unit_key"] == "combined"
    assert result["identity"]["item_key"] == "124"
    assert result["identity"]["head_sha"] == "e" * 40
    assert transport.fields["state"] == "success"
    assert "unit=combined/" + identity["unit_ids"]["combined"] in transport.fields["description"]
    assert f"projects/222/statuses/{'e' * 40}" in transport.calls[0]["argv"]


@pytest.mark.parametrize("state", ["findings", "stale", "incomplete"])
def test_completed_non_pass_final_states_can_never_create_green(store, state):
    identity = ready_run(store, state=state)
    if state == "stale":
        record_freshness(store, identity, "stale")
    transport = ScriptedGlab((json.dumps({"id": 814, "status": "failed"}), None))

    result = publish_individual(store, transport, identity)

    assert result["decision"]["state"] == state
    assert result["decision"]["can_publish_pass"] is False
    assert result["provider_state"] == "failed"
    assert result["official_green"] is False
    assert transport.fields["state"] == "failed"
    assert transport.fields["state"] != "success"


@pytest.mark.parametrize("mode", ["stop", "missing-lens-evidence", "missing-freshness"])
def test_incomplete_evidence_or_stop_cannot_create_green(store, mode):
    identity = ready_run(
        store, complete=mode != "missing-lens-evidence", fresh=mode != "missing-freshness",
    )
    if mode == "stop":
        unit_id = identity["unit_ids"]["individual:1"]
        store.record_stop(
            unit_id, "Dependent verification is unsafe",
            evidence={"reason_code": "unsafe-sandbox"}, affected_unit_ids=[unit_id],
            skipped_work=[{"kind": "lens", "name": "security"}],
        )
    transport = ScriptedGlab((json.dumps({"id": 815, "status": "failed"}), None))

    result = publish_individual(store, transport, identity)

    assert result["decision"]["can_publish_pass"] is False
    assert result["decision"]["scope"]["complete"] is False
    assert result["provider_state"] == "failed"
    assert result["official_green"] is False
    assert transport.fields["state"] == "failed"


def test_independent_pass_cannot_emit_official_green(store):
    identity = ready_run(store, continued=False)
    transport = ScriptedGlab((json.dumps({"id": 816, "status": "failed"}), None))

    result = publish_individual(store, transport, identity)

    assert result["decision"]["trust"]["independent"] is True
    assert "independent-run" in result["decision"]["green_blockers"]
    assert result["provider_state"] == "failed"
    assert result["official_green"] is False
    assert transport.fields["state"] == "failed"


def test_local_prepush_waiver_is_context_only_at_publication_seam(store):
    identity = ready_run(store, state="findings")
    finding_id = store.record_finding(
        identity["unit_ids"]["individual:1"], "save-contract",
        "Save drops its argument", "high", 0.99,
        path="widget.py", line=7,
    )
    transport = ScriptedGlab((json.dumps({"id": 817, "status": "failed"}), None))

    result = publish_individual(
        store, transport, identity,
        local_waivers=[{"finding_id": finding_id, "note": "local pre-push acceptance"}],
    )

    assert result["decision"]["local_waivers_applied"] is False
    assert result["decision"]["suppressed_finding_ids"] == []
    assert result["decision"]["local_waiver_context"][0]["official_suppression"] is False
    assert result["decision"]["local_waiver_context"][0]["matched_finding_ids"] == [finding_id]
    assert result["decision"]["finding_verdict"] == "block"
    assert result["provider_state"] == "failed"
    assert transport.fields["state"] == "failed"


@pytest.mark.parametrize("response", [
    (None, "403 Forbidden: insufficient scope to create commit statuses"),
    RuntimeError("glab transport unavailable"),
    ("not-json", None),
], ids=["permission", "exception", "invalid-response"])
def test_permission_and_transport_failures_preserve_local_unenforced_evidence(
        store, response):
    identity = ready_run(store)
    transport = ScriptedGlab(response)

    result = publish_individual(store, transport, identity)

    assert result["status"] == "local_only"
    assert result["local_only"] is True
    assert result["enforced"] is False
    assert result["official"] is False
    assert result["official_green"] is False
    assert result["provider_state"] is None
    assert result["error"]
    report = store.machine_report(identity["run_id"])
    assert report["run"]["state"] == "pass"
    assert report["trust"]["local_only"] is True
    assert report["trust"]["enforced"] is False
    assert report["trust"]["official"] is False
    assert "publication" in report["trust"]["degraded_reason"]
    assert len(report["publications"]) == 1
    assert report["publications"][0]["status"] == "local_only"
    assert report["publications"][0]["error"] == result["error"]
    assert report["publications"][0]["details"]["official_green"] is False
    assert report["units"][0]["coverage"]


def test_untrusted_caller_cannot_access_provider_write_path(store):
    identity = ready_run(store)
    transport = ScriptedGlab()
    publisher = FinalPublisher(store, runner=transport, trusted_orchestrator=False)

    result = publisher.publish_individual(
        identity["run_id"], identity["unit_ids"]["individual:1"],
    )

    assert transport.calls == []
    assert result["status"] == "local_only"
    assert result["local_only"] is True
    assert result["enforced"] is False
    assert result["official_green"] is False
    assert "trusted orchestrator" in result["error"]
    assert "untrusted-caller" in result["decision"]["status_blockers"]


def test_local_only_trust_never_attempts_a_provider_write(store):
    identity = ready_run(store, trusted=False)
    transport = ScriptedGlab()

    result = publish_individual(store, transport, identity)

    assert transport.calls == []
    assert result["status"] == "local_only"
    assert result["error"]
    assert result["official_green"] is False
    assert store.machine_report(identity["run_id"])["publications"][0]["status"] == "local_only"


def test_late_older_identical_sha_run_cannot_replace_newer_outcome(store):
    old = ready_run(store, continued=False)
    # Starting a newer run over exactly the same source/target/base tuple is
    # sufficient to supersede the slow old worker. No SHA change is required.
    new = ready_run(store, continued=False)
    old_transport = ScriptedGlab()
    new_transport = ScriptedGlab((json.dumps({"id": 818, "status": "success"}), None))

    old_result = publish_individual(store, old_transport, old)
    new_result = publish_individual(store, new_transport, new)

    assert old["run_seq"] == 1 and new["run_seq"] == 2
    assert old["stack_key"] == new["stack_key"]
    assert store.is_superseded(old["run_id"]) is True
    assert old_result["status"] == "superseded"
    assert old_result["local_only"] is True
    assert old_result["enforced"] is False
    assert old_result["official_green"] is False
    assert old_transport.calls == []
    assert new_result["provider_state"] == "success"
    assert len(new_transport.calls) == 1
    assert new_transport.fields["state"] == "success"
    old_attempts = store.machine_report(old["run_id"])["publications"]
    assert [(row["status"], row["details"]["reason"]) for row in old_attempts] == [
        ("superseded", "run-superseded"),
    ]
    assert len(store.machine_report(new["run_id"])["publications"]) == 1


def test_inflight_old_status_finishes_before_newer_identical_sha_run_can_start(store):
    old = ready_run(store)
    # Open/migrate the second connection before the old writer guard begins.
    second_connection = FinalStore(store.path)
    transport_started = threading.Event()
    release_transport = threading.Event()
    newer_started = threading.Event()
    provider_writes = []
    old_result = []
    new_identity = []
    errors = []

    def slow_old_transport(argv, cwd, timeout):
        provider_writes.append(old["run_id"])
        transport_started.set()
        if not release_transport.wait(5):
            raise RuntimeError("test did not release the old transport")
        return json.dumps({"id": 820, "status": "success"}), None

    def publish_old():
        try:
            publisher = FinalPublisher(
                store, runner=slow_old_transport, trusted_orchestrator=True,
            )
            old_result.append(publisher.publish_individual(
                old["run_id"], old["unit_ids"]["individual:1"],
            ))
        except Exception as exc:  # pragma: no cover - thread assertion channel
            errors.append(exc)

    old_thread = threading.Thread(target=publish_old)
    old_thread.start()
    assert transport_started.wait(2)

    with second_connection:
        def start_newer():
            try:
                new_identity.append(begin(second_connection))
                newer_started.set()
            except Exception as exc:  # pragma: no cover - thread assertion channel
                errors.append(exc)

        new_thread = threading.Thread(target=start_newer)
        new_thread.start()
        # The newer BEGIN IMMEDIATE waits behind the old guarded provider I/O;
        # it cannot become visible and then be followed by an old status write.
        assert not newer_started.wait(0.05)
        release_transport.set()
        old_thread.join(timeout=5)
        new_thread.join(timeout=5)
        assert not old_thread.is_alive()
        assert not new_thread.is_alive()
        assert not errors
        assert newer_started.is_set()
        assert old_result[0]["official_green"] is True

        new = new_identity[0]
        assert new["run_seq"] == old["run_seq"] + 1
        assert store.is_superseded(old["run_id"]) is True
        mark_scope_complete(second_connection, new)
        finish(second_connection, new)
        record_freshness(second_connection, new)

        def new_transport(argv, cwd, timeout):
            provider_writes.append(new["run_id"])
            return json.dumps({"id": 821, "status": "success"}), None

        new_publisher = FinalPublisher(
            second_connection, runner=new_transport, trusted_orchestrator=True,
        )
        new_result = new_publisher.publish_individual(
            new["run_id"], new["unit_ids"]["individual:1"],
        )
        assert new_result["official_green"] is True

    assert provider_writes == [old["run_id"], new["run_id"]]


def test_unfinished_run_is_audited_without_a_provider_write(store):
    identity = begin(store)
    mark_scope_complete(store, identity)
    record_freshness(store, identity)
    transport = ScriptedGlab()

    result = publish_individual(store, transport, identity)

    assert transport.calls == []
    assert result["decision"]["run_completed"] is False
    assert result["status"] == "local_only"
    assert result["official_green"] is False
    assert "run-not-completed" in result["decision"]["status_blockers"]


def test_every_retry_attempt_remains_in_publication_audit_history(store):
    identity = ready_run(store)
    transport = ScriptedGlab(
        (None, "403 Forbidden"),
        (json.dumps({"id": 819, "status": "success"}), None),
    )

    failed = publish_individual(store, transport, identity)
    # A trusted supervisor may explicitly re-establish authorization after the
    # credential problem is fixed; the prior failed attempt remains durable.
    store.finish_run(
        identity["run_id"], "pass",
        trust={"local_only": False, "enforced": True, "official": True},
    )
    succeeded = publish_individual(store, transport, identity)

    attempts = store.machine_report(identity["run_id"])["publications"]
    assert failed["status"] == "local_only"
    assert succeeded["status"] == "published"
    assert [row["status"] for row in attempts] == ["local_only", "published"]
    assert len({row["publication_id"] for row in attempts}) == 2
    assert attempts[0]["error"] == "403 Forbidden"
    assert attempts[1]["details"]["provider_status_id"] == 819
    assert store.is_superseded(identity["run_id"]) is False


def test_all_provider_arguments_are_least_privilege_and_contain_no_credentials(
        store, monkeypatch):
    identity = ready_run(store)
    transport = ScriptedGlab()
    monkeypatch.setenv("GLAB_TOKEN", "must-not-be-copied-to-status")
    monkeypatch.setenv("GITLAB_WRITE_TOKEN", "also-secret")

    result = publish_individual(store, transport, identity)

    assert result["official_green"] is True
    argv = transport.calls[0]["argv"]
    assert argv[:2] == ["glab", "api"]
    assert argv[argv.index("--method") + 1] == "POST"
    assert len([arg for arg in argv if arg.startswith("projects/")]) == 1
    assert not any("merge_requests" in arg or "notes" in arg for arg in argv)
    serialized = json.dumps({"argv": argv, "result": result, "transport": transport.calls})
    assert "must-not-be-copied-to-status" not in serialized
    assert "also-secret" not in serialized
    # This assertion also catches accidental form encoding of identity fields.
    assert not parse_qs(serialized).get("GLAB_TOKEN")
