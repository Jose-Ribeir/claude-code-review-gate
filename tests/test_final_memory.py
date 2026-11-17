"""Vertical SPEC-16 finding-memory, freshness, and waiver-isolation tests.

Only public memory/store seams are used. Stores are real temporary SQLite
atabases and providers are scripted read-only doubles.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import rg_final_memory as memory  # noqa: E402
from rg_final_store import FinalStore  # noqa: E402


SOURCE = "a" * 40
TARGET = "b" * 40
BASE = "c" * 40
OLD_EVIDENCE = "d" * 64
NEW_EVIDENCE = "e" * 64


def provider_item(item_key="123", source_sha=SOURCE, target_sha=TARGET):
    number = int("".join(character for character in item_key if character.isdigit()) or "1")
    return {
        "provider": "gitlab",
        "id": f"gitlab:111!{number}",
        "iid": number,
        "url": f"https://gitlab.example.test/group/project/-/merge_requests/{number}",
        "source": {
            "project_id": 222,
            "project_path": "fork/project",
            "branch": f"feature-{number}",
            "sha": source_sha,
        },
        "target": {
            "project_id": 111,
            "project_path": "group/project",
            "branch": "main" if number == 123 else "feature-123",
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
                "ancestors": item.get("ancestors", []),
                "provider_data": {
                    "id": item["id"], "url": item["url"],
                    "source": {"project_id": item["source"]["project_id"]},
                    "target": {"project_id": item["target"]["project_id"]},
                },
            }
            for item in items
        ],
    }


def anchors(evidence=OLD_EVIDENCE):
    return [{
        "anchor_type": "content",
        "anchor": "sha256:stable-finding-content",
        "evidence_fingerprint": evidence,
        "details": {"path": "widget.py", "line": 17},
    }]


@pytest.fixture
def store(tmp_path):
    with FinalStore(tmp_path / "final.sqlite3") as opened:
        yield opened


def begin(store, *items, **kwargs):
    return store.start_run(stack(*items), **kwargs)


def record_open(store, identity, evidence=OLD_EVIDENCE):
    return store.record_finding(
        identity["unit_ids"]["individual:1"], "save-contract",
        "Save drops the supplied value", "high", "high",
        lens="correctness", path="widget.py", line=17,
        anchors=anchors(evidence), evidence={"test": "test_save"},
    )


def test_quiet_rerun_never_clears_an_unresolved_prior_finding(store):
    first = begin(store)
    original_id = record_open(store, first)
    store.finish_run(first["run_id"], "findings")
    original = store.manifest(first["run_id"])["units"][0]["findings"][0]

    second = begin(store, provider_item(source_sha="f" * 40))
    context = memory.rerun_context(store, second["run_id"])

    assert context["schema"] == "final-review-rerun-context/v1"
    assert context["store_instance_id"] == store.store_instance_id
    assert context["required"] is True
    assert len(context["findings"]) == 1
    pending = context["findings"][0]
    assert pending["finding_id"] == original_id
    assert pending["content_anchor"] == "sha256:stable-finding-content"
    assert pending["evidence_anchor"] != pending["content_anchor"]
    assert pending["instruction"] == memory.CARRY_FORWARD_INSTRUCTION

    # No new finding and no carry/resolution call: even a supervisor-supplied
    # pass cannot make the official publication contract green.
    store.finish_run(second["run_id"], "pass")
    decision = store.publication_decision(second["run_id"])
    assert decision["can_publish_pass"] is False
    assert decision["state"] == "findings"
    assert decision["memory"]["quiet_rerun_cleared"] is False
    assert decision["memory"]["pending_thread_ids"] == [pending["thread_id"]]
    assert [item["finding_id"] for item in decision["official_findings"]] == [original_id]
    assert decision["official_findings"][0]["inherited"] is True

    # The original finding and its original event remain untouched.
    assert store.manifest(first["run_id"])["units"][0]["findings"][0] == original
    history = store.finding_history(pending["thread_id"])
    assert history["events"][0]["event_type"] == "observed"
    assert history["events"][0]["finding"]["finding_id"] == original_id


def test_prior_finding_must_be_explicitly_carried_and_remains_blocking(store):
    first = begin(store)
    record_open(store, first)
    second = begin(store, provider_item(source_sha="f" * 40))
    pending = memory.rerun_context(store, second["run_id"])["findings"][0]
    wrong_content = anchors()
    wrong_content[0]["anchor"] = "sha256:a-different-defect"
    with pytest.raises(ValueError, match="content anchor"):
        memory.carry_forward(
            store, second["run_id"], second["unit_ids"]["individual:1"],
            pending["thread_id"], anchors=wrong_content,
        )

    carried_id = memory.carry_forward(
        store, second["run_id"], second["unit_ids"]["individual:1"],
        pending["thread_id"], evidence={"revalidation": "defect still reproduces"},
    )

    assert carried_id != pending["finding_id"]
    status = store.memory_status(second["run_id"])
    assert status["pending_thread_ids"] == []
    assert status["unresolved_thread_ids"] == [pending["thread_id"]]
    assert status["complete_for_finalization"] is True
    assert status["clear_allowed"] is False
    decision = store.publication_decision(second["run_id"])
    assert decision["state"] == "findings"
    assert decision["can_publish_pass"] is False
    current = decision["official_findings"][0]
    assert current["finding_id"] == carried_id
    assert current["inherited"] is False
    assert current["carried_into_run"] is True
    assert [event["event_type"] for event in store.finding_history(
        pending["thread_id"])["events"]] == ["observed", "carried"]


def test_positive_resolution_requires_changed_evidence_and_revalidation(store):
    first = begin(store)
    original_id = record_open(store, first)
    original = store.manifest(first["run_id"])["units"][0]["findings"][0]
    second = begin(store, provider_item(source_sha="f" * 40))
    pending = memory.rerun_context(store, second["run_id"])["findings"][0]
    memory.carry_forward(
        store, second["run_id"], second["unit_ids"]["individual:1"],
        pending["thread_id"],
    )

    with pytest.raises(ValueError, match="changed code or evidence"):
        memory.resolve_finding(
            store, second["run_id"], pending["thread_id"], anchors=anchors(),
            resolution_evidence={"test": "test_save now covers persistence"},
            revalidation={"status": "passed", "evidence_fingerprint": NEW_EVIDENCE},
        )
    with pytest.raises(ValueError, match="revalidation"):
        memory.resolve_finding(
            store, second["run_id"], pending["thread_id"], anchors=anchors(NEW_EVIDENCE),
            resolution_evidence={"test": "test_save now covers persistence"},
            revalidation={},
        )
    with pytest.raises(ValueError, match="successful revalidation"):
        memory.resolve_finding(
            store, second["run_id"], pending["thread_id"], anchors=anchors(NEW_EVIDENCE),
            resolution_evidence={"test": "test_save now covers persistence"},
            revalidation={"status": "failed", "evidence_fingerprint": NEW_EVIDENCE},
        )

    memory.resolve_finding(
        store, second["run_id"], pending["thread_id"], anchors=anchors(NEW_EVIDENCE),
        resolution_evidence={"test": "test_save now covers persistence"},
        revalidation={"status": "passed", "evidence_fingerprint": NEW_EVIDENCE},
    )

    status = store.memory_status(second["run_id"])
    assert status["unresolved_thread_ids"] == []
    assert status["pending_thread_ids"] == []
    assert status["clear_allowed"] is True
    assert store.publication_decision(second["run_id"])["official_findings"] == []
    assert store.manifest(first["run_id"])["units"][0]["findings"][0] == original
    history = store.finding_history(pending["thread_id"])
    assert [event["event_type"] for event in history["events"]] == [
        "observed", "carried", "resolved",
    ]
    assert history["events"][-1]["finding"]["finding_id"] == original_id or \
        history["events"][-1]["finding"] is not None
    assert history["events"][-1]["revalidation"]["status"] == "passed"
    assert history["events"][-1]["resolution_evidence"]["test"]
    assert history["events"][-1]["previous_evidence_anchor"] != \
        history["events"][-1]["evidence_anchor"]
    assert history["events"][0]["finding"]["finding_id"] == original_id


def test_falsification_is_explicit_rationaled_and_does_not_need_a_code_change(store):
    first = begin(store)
    record_open(store, first)
    second = begin(store)
    pending = memory.rerun_context(store, second["run_id"])["findings"][0]

    with pytest.raises(ValueError, match="rationale"):
        memory.falsify_finding(store, second["run_id"], pending["thread_id"], "  ")

    memory.falsify_finding(
        store, second["run_id"], pending["thread_id"],
        "The configured transaction wrapper persists before this method returns",
        evidence={"inspection": "widget.py:41-48"},
    )

    assert store.memory_status(second["run_id"])["clear_allowed"] is True
    event = store.finding_history(pending["thread_id"])["events"][-1]
    assert event["event_type"] == "falsified"
    assert event["rationale"].startswith("The configured transaction")
    assert event["previous_evidence_anchor"] == event["evidence_anchor"]


def test_a_resolved_defect_reopening_preserves_the_whole_thread(store):
    first = begin(store)
    record_open(store, first)
    second = begin(store, provider_item(source_sha="f" * 40))
    pending = memory.rerun_context(store, second["run_id"])["findings"][0]
    memory.resolve_finding(
        store, second["run_id"], pending["thread_id"], anchors=anchors(NEW_EVIDENCE),
        resolution_evidence={"fix": "assign the supplied value"},
        revalidation={"status": "passed", "test": "test_save"},
    )
    third = begin(store, provider_item(source_sha="9" * 40))

    reopened_id = record_open(store, third, evidence="8" * 64)

    context = memory.rerun_context(store, third["run_id"])
    # Reopening is current evidence, not a request to resurrect an already
    # closed prior thread behind the reviewer's back.
    assert context["findings"] == []
    assert store.publication_decision(third["run_id"])["official_findings"][0][
        "finding_id"] == reopened_id
    events = store.finding_history(pending["thread_id"])["events"]
    assert [event["event_type"] for event in events] == ["observed", "resolved", "reopened"]
    assert events[0]["finding"]["finding_id"] != events[-1]["finding"]["finding_id"]


def test_memory_is_scoped_to_store_stack_and_unit(tmp_path):
    with FinalStore(tmp_path / "one.sqlite3") as one, \
            FinalStore(tmp_path / "two.sqlite3") as two:
        first = begin(one)
        record_open(store=one, identity=first)
        unrelated_store = begin(two)
        assert memory.rerun_context(two, unrelated_store["run_id"])["findings"] == []
        assert unrelated_store["trust"]["independent"] is True

        other = provider_item("124", source_sha="7" * 40)
        other["target"]["branch"] = "main"
        other_stack = stack(other)
        other_stack["root_target_ref"] = "release"
        other_run = one.start_run(other_stack)
        assert memory.rerun_context(one, other_run["run_id"])["findings"] == []

        continued = begin(one, provider_item(source_sha="6" * 40))
        combined = continued["unit_ids"]["combined"]
        with pytest.raises(ValueError, match="unit scope"):
            memory.carry_forward(
                one, continued["run_id"], combined,
                memory.rerun_context(one, continued["run_id"])["findings"][0]["thread_id"],
            )


class ScriptedRevisionProvider:
    def __init__(self, items):
        self.items = [copy.deepcopy(item) for item in items]
        self.reads = []

    def read(self, reference, repo_root=None):
        self.reads.append((reference, repo_root))
        for item in self.items:
            if reference == item["url"]:
                return copy.deepcopy(item)
        raise AssertionError(f"unexpected provider read: {reference}")


def test_freshness_refetches_the_complete_revision_tuple_without_restarting(store, tmp_path):
    first_item = provider_item()
    ancestor = provider_item("124", source_sha="1" * 40, target_sha="2" * 40)
    first_item["ancestors"] = [ancestor]
    identity = begin(store, first_item)
    before = store.manifest(identity["run_id"])
    current = copy.deepcopy(first_item)
    current["source"]["sha"] = "3" * 40
    current["target"]["sha"] = "4" * 40
    current["target"]["branch"] = "release"
    current["target"]["base_sha"] = "5" * 40
    current["ancestors"][0]["source"]["sha"] = "6" * 40
    provider = ScriptedRevisionProvider([current])

    result = memory.refresh_run_freshness(
        store, identity["run_id"], provider, repo_root=tmp_path)

    assert provider.reads == [(first_item["url"], tmp_path)]
    assert result["schema"] == "final-review-freshness/v1"
    assert result["fresh"] is False
    assert result["state"] == "stale"
    assert result["restart_required"] is True
    assert result["automatic_restart"] is False
    changed = {change["field"] for change in result["changes"]}
    assert {
        "source.sha", "target.sha", "target.branch", "target.base_sha",
        "ancestors[0].source.sha",
    } <= changed
    assert all(change["expected"] and change["current"] for change in result["changes"])

    # Freshness records stale evidence against old SHAs; it never spawns a run
    # or mutates the original revision pin.
    assert len(store.run_history()) == 1
    assert store.manifest(identity["run_id"])["revisions"] == before["revisions"]
    assert store.manifest(identity["run_id"])["run"]["state"] == "stale"
    assert store.manifest(identity["run_id"])["freshness"]["check_id"] == result["check_id"]
    assert store.manifest(identity["run_id"])["freshness"]["changes"] == result["changes"]
    decision = store.publication_decision(identity["run_id"])
    assert decision["state"] == "stale"
    assert decision["can_publish_pass"] is False
    assert decision["restart_required"] is True
    assert decision["automatic_restart"] is False


@pytest.mark.parametrize("mutate", [
    lambda item: item["source"].update(sha="3" * 40),
    lambda item: item["target"].update(sha="4" * 40),
    lambda item: item["target"].update(branch="release"),
    lambda item: item["target"].update(project_id=999),
    lambda item: item["target"].update(base_sha="5" * 40),
])
def test_every_source_target_retarget_and_base_coordinate_is_freshness_sensitive(mutate):
    pinned = provider_item()
    current = copy.deepcopy(pinned)
    mutate(current)

    result = memory.compare_revision_tuples(pinned, current)

    assert result["fresh"] is False
    assert result["state"] == "stale"
    assert result["restart_required"] is True
    assert len(result["changes"]) == 1


def test_identical_full_revision_tuples_are_fresh(store):
    item = provider_item()
    identity = begin(store, item)
    provider = ScriptedRevisionProvider([item])

    result = memory.refresh_run_freshness(store, identity["run_id"], provider)

    assert result["fresh"] is True
    assert result["changes"] == []
    assert result["restart_required"] is False
    assert result["automatic_restart"] is False
    assert store.manifest(identity["run_id"])["run"]["state"] == "incomplete"


def test_added_removed_or_changed_ancestors_make_the_stack_stale():
    pinned = provider_item()
    ancestor = provider_item("124")
    pinned["ancestors"] = [ancestor]
    changed = copy.deepcopy(pinned)
    changed["ancestors"][0]["target"]["base_sha"] = "7" * 40
    assert memory.compare_revision_tuples(pinned, changed)["changes"][0]["field"] == \
        "ancestors[0].target.base_sha"
    removed = copy.deepcopy(pinned)
    removed["ancestors"] = []
    result = memory.compare_revision_tuples(pinned, removed)
    assert result["state"] == "stale"
    assert result["changes"][0]["field"] == "ancestors"


def test_provider_read_failure_is_incomplete_not_a_lucky_fresh_pass(store):
    class Unavailable:
        def read(self, reference, repo_root=None):
            raise RuntimeError("authenticated provider read failed")

    identity = begin(store)
    result = memory.refresh_run_freshness(store, identity["run_id"], Unavailable())

    assert result["fresh"] is False
    assert result["state"] == "incomplete"
    assert result["restart_required"] is False
    assert result["errors"][0]["item_key"] == "123"
    assert store.publication_decision(identity["run_id"])["can_publish_pass"] is False


def test_local_prepush_waivers_are_visible_but_never_official_suppression(store):
    identity = begin(store)
    finding_id = record_open(store, identity)
    store.finish_run(identity["run_id"], "findings")
    waivers = [{"finding_id": finding_id, "note": "accepted on this laptop"}]
    findings_before = copy.deepcopy(store.official_findings(identity["run_id"]))

    decision = store.publication_decision(identity["run_id"], local_waivers=waivers)

    assert decision["official_findings"] == findings_before
    assert decision["suppressed_finding_ids"] == []
    assert decision["local_waivers_applied"] is False
    assert decision["local_waiver_context"][0]["note"] == "accepted on this laptop"
    assert decision["local_waiver_context"][0]["official_suppression"] is False
    assert decision["local_waiver_context"][0]["matched_finding_ids"] == [finding_id]
    assert decision["state"] == "findings"
    assert decision["trust"]["waiver_isolation"] == "local-context-only"
    assert waivers == [{"finding_id": finding_id, "note": "accepted on this laptop"}]


def test_store_history_and_manifest_expose_append_only_memory_and_stale_evidence(store):
    item = provider_item()
    first = begin(store, item)
    record_open(store, first)
    second = begin(store, provider_item(source_sha="f" * 40))
    pending = memory.rerun_context(store, second["run_id"])["findings"][0]
    memory.carry_forward(
        store, second["run_id"], second["unit_ids"]["individual:1"], pending["thread_id"])
    changed = copy.deepcopy(item)
    changed["target"]["base_sha"] = "8" * 40
    memory.refresh_run_freshness(
        store, second["run_id"], ScriptedRevisionProvider([changed]))

    report = store.machine_report(second["run_id"])

    assert report["finding_memory"]["unresolved_thread_ids"] == [pending["thread_id"]]
    assert report["finding_memory"]["store_instance_id"] == store.store_instance_id
    assert report["freshness"]["state"] == "stale"
    assert report["freshness"]["pinned"]["0"]["source"]["sha"] == "f" * 40
    assert report["freshness"]["current"]["0"]["target"]["base_sha"] == "8" * 40
    assert json.loads(json.dumps(report))["finding_memory"]["quiet_rerun_cleared"] is False
    assert "Content and evidence anchors" in store.human_report(second["run_id"])
