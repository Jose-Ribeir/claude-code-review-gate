"""Verdict records carry intent extras (t15).

Every review record can store the intent context actually used -- sources
present, the intent text, counts, fetch notes -- as an `intent` extra dict the
store tolerates at any protocol version (the intent work itself bumped
nothing; records are protocol_version 2 since restack-a's content-keyed
coverage). Records written before this feature (no `intent` key) must still
load.
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")

sys.path.insert(0, _SCRIPTS)
import rg_store  # noqa: E402


_INTENT = {
    "sources": ["ticket", "commit_messages"],
    "text": "PROJ-123: Fix the retry backoff overflow\ncommits: 2 messages",
    "counts": {"tickets": 1, "messages": 2, "trimmed": 0},
    "notes": ["github fetch ok", "1 ticket key left unfetched (cap 3)"],
}


def test_record_with_intent_round_trips_through_load():
    rg_store.record_verdict("origin", "a" * 40, "b" * 40, "pass", False,
                            "ok", ["c1", "c2"],
                            coverage={"tip_tree": "c" * 40,
                                      "patch_ids": ["pid-2", "pid-1"],
                                      "sha_only": ["d" * 40]},
                            branch="feat", intent=_INTENT)
    loaded = rg_store.load_verdict("origin", "a" * 40)
    assert loaded is not None
    assert loaded["intent"] == _INTENT
    # The core record fields are untouched by the extra.
    assert loaded["verdict"] == "pass"
    assert loaded["commits"] == ["c1", "c2"]
    # The v2 record shape (restack-a): the coverage the caller passed rides
    # the record (patch-ids deduped and sorted), as does the branch.
    assert loaded["protocol_version"] == 2
    assert loaded["tip_tree"] == "c" * 40
    assert loaded["patch_ids"] == ["pid-1", "pid-2"]
    assert loaded["sha_only"] == ["d" * 40]
    assert loaded["branch"] == "feat"


def test_record_without_intent_loads_and_has_no_intent_key():
    """New writer, no intent passed: the key is simply absent (extra-optional)."""
    rg_store.record_verdict("origin", "a" * 40, "b" * 40, "block", True,
                            "bad", ["c1"])
    loaded = rg_store.load_verdict("origin", "a" * 40)
    assert loaded is not None
    assert "intent" not in loaded
    assert loaded.get("intent") is None


def test_old_record_without_intent_still_loads():
    """A record written before the feature existed (no `intent` key) loads."""
    old = {
        "remote": "origin", "tip": "a" * 40, "base": "b" * 40,
        "verdict": "pass", "blocked": False, "reasons": "",
        "commits": ["c1"], "capped": False, "note": "",
        "ts": 1700000000.0, "protocol_version": 1,
    }
    path = rg_store.verdict_path("origin", "a" * 40)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(old), encoding="utf-8")
    loaded = rg_store.load_verdict("origin", "a" * 40)
    assert loaded is not None
    assert loaded.get("intent") is None
    assert loaded["verdict"] == "pass"


def test_intent_extra_adds_no_protocol_bump_of_its_own():
    """The record's protocol version is the store's own (v2 since restack-a's
    content keying); the intent extra is tolerated at whatever version the
    record carries and adds no field of its own beyond `intent`."""
    rg_store.record_verdict("origin", "a" * 40, "b" * 40, "pass", False, "",
                            ["c1"], intent=_INTENT)
    with_intent = json.loads(
        rg_store.verdict_path("origin", "a" * 40).read_text(encoding="utf-8"))
    rg_store.record_verdict("origin", "c" * 40, "b" * 40, "pass", False, "",
                            ["c1"])
    without = json.loads(
        rg_store.verdict_path("origin", "c" * 40).read_text(encoding="utf-8"))
    assert with_intent["protocol_version"] == 2
    assert with_intent["protocol_version"] == without["protocol_version"]
    assert set(with_intent) - set(without) == {"intent"}


def test_intent_none_is_the_same_as_not_passing_it():
    rg_store.record_verdict("origin", "a" * 40, "b" * 40, "pass", False, "",
                            ["c1"], intent=None)
    loaded = rg_store.load_verdict("origin", "a" * 40)
    assert "intent" not in loaded


def test_intent_non_dict_degrades_to_a_text_wrapper():
    """A stray non-dict value is kept as text, never dropped silently."""
    rg_store.record_verdict("origin", "a" * 40, "b" * 40, "pass", False, "",
                            ["c1"], intent="loose intent note")
    loaded = rg_store.load_verdict("origin", "a" * 40)
    assert loaded["intent"] == {"text": "loose intent note"}


def test_intent_non_json_values_are_coerced_like_any_record_value():
    """The writer never fails on odd extra values (default=str, as the store
    already does for every field)."""
    class Odd:
        def __str__(self):
            return "odd-value"
    rg_store.record_verdict("origin", "a" * 40, "b" * 40, "pass", False, "",
                            ["c1"], intent={"text": Odd()})
    loaded = rg_store.load_verdict("origin", "a" * 40)
    assert loaded["intent"]["text"] == "odd-value"


def test_intent_is_a_copy_not_the_callers_dict():
    intent = {"text": "mine"}
    rg_store.record_verdict("origin", "a" * 40, "b" * 40, "pass", False, "",
                            ["c1"], intent=intent)
    intent["text"] = "mutated after the fact"
    loaded = rg_store.load_verdict("origin", "a" * 40)
    assert loaded["intent"]["text"] == "mine"
