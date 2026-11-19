"""This repo's own tracked .ocr/rule.json must not force every push onto the
deep lane (review-speed ticket 03). `risk_globs` in the rule file EXTENDS the
default starter set, so the tracked file must not carry a match-everything
glob: ordinary source changes stay on the fast lane, and a path matching the
default risk globs still escalates.

The tracked rule file is copied into the base commit of a throwaway repo (so
it is rule configuration, not part of the pushed diff) and the real gate runs
behind the stub reviewer.
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from test_deep_gate import (  # noqa: E402
    _commit, _env, _push, _repo, _review_manifest, _state, review_gate,
)

_RULE = os.path.join(os.path.dirname(_HERE), ".ocr", "rule.json")


def _repo_with_repo_rule(tmp_path):
    with open(_RULE, encoding="utf-8") as f:
        rule_text = f.read()
    return _repo(tmp_path, {"a.txt": "one\n", ".ocr/rule.json": rule_text})


def test_tracked_rule_file_is_valid_and_matches_no_everything_glob():
    with open(_RULE, encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, dict)
    for glob in data.get("risk_globs") or []:
        assert not any(review_gate._risk_glob_match(p, glob)
                       for p in ("app/x.py", "scripts/review-gate.py", "README.md")), glob


def test_ordinary_source_change_is_not_escalated(tmp_path):
    work = _repo_with_repo_rule(tmp_path)
    tip = _commit(work, {"scripts/helper.py": "x = 1\n"})
    decision, reason = _push(work, _env(tmp_path, STUB_VERDICT="block"))
    assert decision == "deny" and "stub high finding" in reason
    st = _state(work, tip)
    assert st["state"] == "done" and st.get("tier") == "fast"
    manifest = _review_manifest(tmp_path)
    assert manifest.get("deep_review") is not True


def test_default_risk_glob_path_still_escalates(tmp_path):
    work = _repo_with_repo_rule(tmp_path)
    tip = _commit(work, {"db/migrations/001_users.py": "x = 1\n"})
    decision, reason = _push(work, _env(tmp_path))
    assert decision == "pass", reason
    st = _state(work, tip)
    assert st["state"] == "done" and st.get("tier") == "deep"
    manifest = _review_manifest(tmp_path)
    assert manifest["deep_review"] is True
    assert any("db/migrations/001_users.py" in r
               for r in manifest.get("deep_reasons", []))
