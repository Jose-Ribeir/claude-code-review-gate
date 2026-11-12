"""Regression tests for t23: review-gate.py high + medium defect-tier fixes.

Each test asserts the POST-fix behavior and fails against the pre-fix code.
Items (findings ref .kimchi/docs/cycle3-findings-299.txt):
  1  unparseable hook stdin fails CLOSED (was _pass_through)
  2  _guard_reviewer_command detects the short `-o` form of --output
  3  _GIT_CMD's command positions include `(`, `{`, backtick, `$(`
  4  _push_c_dirs honors ALL -C options, in order
  5  _find_kimi (and ocr_doctor._kimchi_bin) resolve KIMCHI_BIN env-first
  6  float(confidence) guarded in the merger and the normalizer
  7  .ocr/config advisory requires an explicit "blocking": false
  8  timeout escalation names the env var of the knob that fired
  9  the deep-lane manifest honors the rename contract
 10  _async_note's started_ts/claimed_ts conversion never raises
"""
import importlib.util
import io
import json
import os
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)  # so review-gate.py's own `from ocr_verdict import ...` resolves

_spec = importlib.util.spec_from_file_location(
    "review_gate", os.path.join(_SCRIPTS, "review-gate.py"))
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)

_spec_d = importlib.util.spec_from_file_location(
    "ocr_doctor", os.path.join(_SCRIPTS, "ocr_doctor.py"))
ocr_doctor = importlib.util.module_from_spec(_spec_d)
_spec_d.loader.exec_module(ocr_doctor)


# --- item 1: unparseable hook stdin fails closed ----------------------------

def test_unparseable_hook_stdin_fails_closed(capsys):
    with pytest.raises(SystemExit) as exc:
        review_gate._refuse_unparseable_stdin("hook")
    assert exc.value.code == 0  # the hook protocol exits 0; decision rides stdout
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"
    assert "OCR_FAIL_OPEN" in out["reason"]


def test_unparseable_hook_stdin_fail_open_escape(capsys, monkeypatch):
    monkeypatch.setenv("OCR_FAIL_OPEN", "1")
    monkeypatch.setattr(review_gate, "_log_gate_skipped", lambda *a, **k: None)
    with pytest.raises(SystemExit) as exc:
        review_gate._refuse_unparseable_stdin("hook")
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out or "{}") == {}


def test_hook_main_denies_unparseable_stdin(monkeypatch, capsys):
    """The old path caught the parse failure and landed on _pass_through()."""
    monkeypatch.chdir(os.environ.get("HOME", "/"))
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))
    monkeypatch.setattr(review_gate, "_write_gate_pointer", lambda: None)
    monkeypatch.setattr(review_gate, "_log_gate_skipped", lambda *a, **k: None)
    monkeypatch.delenv("OCR_FAIL_OPEN", raising=False)
    with pytest.raises(SystemExit):
        review_gate.main(["review-gate.py", "--mode", "hook"])
    out = capsys.readouterr().out
    payload = json.loads(out or "{}")
    assert payload.get("decision") == "block"
    assert "OCR_FAIL_OPEN" in payload.get("reason", "")


# --- item 2: the guard denies the short -o form of --output -----------------

def test_guard_denies_short_output_option():
    g = review_gate._guard_reviewer_command
    assert g("git log -o /tmp/x")
    assert g("git format-patch -o ../../x")
    assert g("git diff --output=/tmp/out HEAD")
    assert "-o" in g("git log -o /tmp/x")
    # no false positives on ordinary reads
    assert g("git log --oneline") == ""
    assert g("git diff HEAD~1") == ""


# --- item 3: _GIT_CMD command-position prefix -------------------------------

def test_pre_push_git_commands_finds_subshell_backtick_and_dollar_paren():
    pre = review_gate._pre_push_git_commands
    assert pre("(git commit -m x) && git push origin main") == ["commit"]
    assert pre("$(git commit -m x); git push origin main") == ["commit"]
    assert pre("`git commit -m x`; git push origin main") == ["commit"]
    assert pre("{ git reset --hard HEAD~1; } && git push origin main") == ["reset"]
    # read-only preludes stay allowed
    assert pre("git status && git push origin main") == []


# --- item 4: _push_c_dirs honors every -C option, in order ------------------

def test_push_c_dirs_returns_every_C_option_in_order():
    d = review_gate._push_c_dirs
    assert d("git -C /repoA -C sub push") == ["/repoA", "sub"]
    assert d("git -C /repoA -C /repoB push origin main") == ["/repoA", "/repoB"]
    assert d('git -C "/a b" push') == ["/a b"]
    assert d("git push origin main") == []
    # a -C on a DIFFERENT git subcommand is not the push's final cd
    assert d("git -C /repo status; git push origin main") == []


# --- item 5: KIMCHI_BIN overrides PATH --------------------------------------

def _shim(tmp_path):
    exe = tmp_path / "kimchi-shim"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    return str(exe)


def test_find_kimi_kimchi_bin_beats_path(tmp_path, monkeypatch):
    monkeypatch.setenv("KIMCHI_BIN", _shim(tmp_path))
    monkeypatch.setattr(review_gate.shutil, "which", lambda name: "/stale/on-path/kimchi")
    assert review_gate._find_kimi() == _shim(tmp_path)


def test_find_kimi_falls_back_to_path(tmp_path, monkeypatch):
    monkeypatch.delenv("KIMCHI_BIN", raising=False)
    monkeypatch.setattr(review_gate.shutil, "which", lambda name: "/ok/kimchi")
    assert review_gate._find_kimi() == "/ok/kimchi"
    monkeypatch.setattr(review_gate.shutil, "which", lambda name: None)
    assert review_gate._find_kimi() is None


def test_ocr_doctor_kimchi_bin_env_first(tmp_path):
    shim = _shim(tmp_path)
    assert ocr_doctor._kimchi_bin({"PATH": "/usr/bin:/bin", "KIMCHI_BIN": shim}) == shim


# --- item 6: confidence coercion never raises -------------------------------

def test_finding_confidence_never_raises():
    assert review_gate._finding_confidence({"confidence": "high"}) == 0.0
    assert review_gate._finding_confidence({"confidence": [0.5]}) == 0.0
    assert review_gate._finding_confidence({}) == 0.0
    assert review_gate._finding_confidence({"confidence": 0.9}) == 0.9


def test_merge_near_dup_survives_a_string_confidence():
    low = {"path": "a.py", "severity": "high", "content": "same defect", "confidence": "high"}
    high = {"path": "a.py", "severity": "high", "content": "same defect", "confidence": 0.7}
    out = review_gate._merge_near_dup_findings([low, high])
    assert len(out) == 1
    assert out[0]["confidence"] == 0.7


def test_normalize_verdict_coerces_a_string_confidence():
    obj = {"verdict": "block", "findings": [
        {"file": "a.py", "line": "1", "issue": "a defect", "confidence": "high"}]}
    shaped = review_gate._normalize_verdict(obj)
    assert shaped["findings"][0]["confidence"] == 0.0


# --- item 7: .ocr/config advisory needs an explicit "blocking": false -------

def _write_config(root, name, text):
    d = os.path.join(root, ".ocr")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "w", encoding="utf-8") as f:
        f.write(text)


def test_advisory_ignores_prose_mentioning_blocking_and_false(tmp_path):
    _write_config(str(tmp_path), "config",
                  "# blocking must stay ON here; do NOT set it to false\n")
    assert review_gate._is_advisory(str(tmp_path)) is False


def test_advisory_requires_explicit_blocking_false(tmp_path):
    _write_config(str(tmp_path), "config", '{"blocking": false}')
    assert review_gate._is_advisory(str(tmp_path)) is True
    _write_config(str(tmp_path), "config", '{"blocking": true, "note": "false negatives"}')
    assert review_gate._is_advisory(str(tmp_path)) is False
    _write_config(str(tmp_path), "config.json", '{"blocking": false}')
    assert review_gate._is_advisory(str(tmp_path)) is True


# --- item 8: timeout escalation names the knob that fired -------------------

def test_timeout_knob_names_the_per_call_override():
    assert review_gate._timeout_knob(None, review_gate.TIMEOUT) == "OCR_TIMEOUT"
    assert (review_gate._timeout_knob(review_gate._DEEP_TIMEOUT, review_gate._DEEP_TIMEOUT)
            == "OCR_DEEP_TIMEOUT")
    assert (review_gate._timeout_knob(review_gate._CHUNK_TIMEOUT, review_gate._CHUNK_TIMEOUT)
            == "OCR_CHUNK_TIMEOUT")
    assert review_gate._timeout_knob(12345, 12345) == "OCR_TIMEOUT"


# --- item 9: the deep manifest honors the rename contract --------------------

def test_deep_manifest_honors_the_rename_contract():
    m = review_gate._build_deep_manifest(["pkg/new.py"], ["reason"], 0.5,
                                         renames=[["pkg/old.py", "pkg/new.py"]])
    assert m["renames"] == [["pkg/old.py", "pkg/new.py"]]
    assert review_gate._build_deep_manifest(["a.py"], [], 0.5)["renames"] == []


# --- item 10: _async_note started_ts conversion never raises ----------------

def test_async_note_survives_a_non_numeric_started_ts(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps(
        {"state": "running", "started_ts": "not-a-number"}), encoding="utf-8")
    info = {"kind": "async", "repo": str(tmp_path), "head": "abc1234",
            "state": str(state), "notified_ts": 0}
    text = review_gate._async_note(str(tmp_path / "note.json"), info, "sid")
    assert "still running" in text


def test_async_note_survives_a_non_numeric_claimed_ts(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps(
        {"state": "claimed", "started_ts": None, "claimed_ts": "soon"}), encoding="utf-8")
    info = {"kind": "async", "repo": str(tmp_path), "head": "abc1234",
            "state": str(state), "notified_ts": 0}
    text = review_gate._async_note(str(tmp_path / "note.json"), info, "sid")
    assert "still running" in text
