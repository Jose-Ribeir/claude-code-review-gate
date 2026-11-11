"""Verdict records carry intent extras (Claude port of the Kimchi variant's
t15 store test).

Every review record can store the intent context actually used -- sources
present, the intent text, counts, fetch notes -- as an `intent` extra dict.
The Claude variant's verdict record is the append-only findings log entry
written by review-gate._record_review: schema-loose JSONL that tolerates the
extra key with no protocol change, and _read_history tolerates its absence
in older records.
"""
import importlib.util
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")

sys.path.insert(0, _SCRIPTS)
_spec = importlib.util.spec_from_file_location(
    "review_gate_verdict_intent", os.path.join(_SCRIPTS, "review-gate.py"))
rg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rg)


_INTENT = {
    "sources": ["ticket", "commit_messages"],
    "text": "PROJ-123: Fix the retry backoff overflow\ncommits: 2 messages",
    "counts": {"tickets": 1, "messages": 2, "trimmed": 0},
    "notes": ["github fetch ok", "1 ticket key left unfetched (cap 3)"],
}

_TIP = "a" * 40


def _record(git_dir, intent="__unset__"):
    kwargs = {} if intent == "__unset__" else {"intent": intent}
    path = rg._record_review(str(git_dir), _TIP, "main", "hook", "pass",
                             False, False, {"findings": [{"severity": "high"}]},
                             "snap.json", **kwargs)
    return path


def _entries(git_dir):
    return rg._read_history(str(git_dir), 0)


def _log_line(git_dir):
    path = rg._findings_log_path(str(git_dir))
    return json.loads(path.read_text(encoding="utf-8").splitlines()[-1])


def test_record_with_intent_round_trips_through_the_log(tmp_path):
    _record(tmp_path / "git-dir", intent=_INTENT)
    (entry,) = _entries(tmp_path / "git-dir")
    assert entry["intent"] == _INTENT
    # The core record fields are untouched by the extra.
    assert entry["verdict"] == "pass"
    assert entry["findings"] == [{"severity": "high"}]
    assert entry["head"] == _TIP


def test_record_without_intent_loads_and_has_no_intent_key(tmp_path):
    """New writer, no intent passed: the key is simply absent (extra-optional)."""
    _record(tmp_path / "git-dir")
    (entry,) = _entries(tmp_path / "git-dir")
    assert "intent" not in entry


def test_old_record_without_intent_still_loads(tmp_path):
    """A record written before this feature existed (no `intent` key) loads."""
    old = {
        "ts": 1700000000.0, "at": "2023-11-14T22:13:20Z", "head": _TIP,
        "branch": "main", "mode": "hook", "verdict": "pass",
        "advisory": False, "blocked": False, "finding_count": 0,
        "findings": [], "truncated": False, "raw": "",
    }
    git_dir = tmp_path / "git-dir"
    git_dir.mkdir()
    (git_dir / rg.FINDINGS_LOG).write_text(json.dumps(old) + "\n", encoding="utf-8")
    (entry,) = _entries(git_dir)
    assert entry.get("intent") is None
    assert entry["verdict"] == "pass"


def test_intent_none_is_the_same_as_not_passing_it(tmp_path):
    _record(tmp_path / "git-dir", intent=None)
    (entry,) = _entries(tmp_path / "git-dir")
    assert "intent" not in entry


def test_intent_non_dict_degrades_to_a_text_wrapper(tmp_path):
    """A stray non-dict value is kept as text, never dropped silently."""
    _record(tmp_path / "git-dir", intent="loose intent note")
    (entry,) = _entries(tmp_path / "git-dir")
    assert entry["intent"] == {"text": "loose intent note"}


def test_intent_non_json_values_are_coerced_like_any_record_value(tmp_path):
    """The writer never fails on odd extra values (default=str, as the log
    already does for every field)."""
    class Odd:
        def __str__(self):
            return "odd-value"
    _record(tmp_path / "git-dir", intent={"text": Odd()})
    (entry,) = _entries(tmp_path / "git-dir")
    assert entry["intent"]["text"] == "odd-value"


def test_intent_is_a_copy_not_the_callers_dict(tmp_path):
    intent = {"text": "mine"}
    _record(tmp_path / "git-dir", intent=intent)
    intent["text"] = "mutated after the fact"
    (entry,) = _entries(tmp_path / "git-dir")
    assert entry["intent"]["text"] == "mine"


def test_intent_extra_survives_within_the_line_cap(tmp_path):
    """The extras dict is part of the same capped log line: findings still
    shed first to fit, the intent extra rides along."""
    result = {"findings": [{"severity": "high", "content": "x" * 500}]
              * 30}
    git_dir = tmp_path / "git-dir"
    path = rg._record_review(str(git_dir), _TIP, "main", "hook", "pass",
                             False, False, result, intent=_INTENT)
    line = path.read_text(encoding="utf-8").splitlines()[-1]
    assert len(line) <= rg._MAX_LOG_LINE
    entry = json.loads(line)
    assert entry["intent"] == _INTENT
