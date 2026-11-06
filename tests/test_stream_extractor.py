"""C2: the pi-mono --mode json stream extractor (port spec section 5).

Kimchi's headless `--mode json` output is pi-mono NDJSON: session, agent_start,
turn_start, message_start/update/end, turn_end, agent_end, agent_settled
events (see the captured real stream in tests/fixtures/json-mode-stream.jsonl).
The verdict JSON arrives in the text blocks of message_end (and is repeated by
turn_end). The extractor scans every such event; the text-mode _extract_json
stays as the fallback and must agree on the verdict object.
"""
import importlib.util
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, _SCRIPTS)
_spec = importlib.util.spec_from_file_location("review_gate_sj", os.path.join(_SCRIPTS, "review-gate.py"))
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)

_FIXTURE = os.path.join(_HERE, "fixtures", "json-mode-stream.jsonl")

VERDICT = {"status": "success", "verdict": "block",
           "findings": [{"path": "app/x.py", "start_line": 1, "end_line": 2,
                         "severity": "high", "confidence": 0.9,
                         "category": "correctness", "content": "boom"}]}


def _event(ev_type, **extra):
    return json.dumps({"type": ev_type, **extra})


def _turn(text, ev_type="message_end"):
    """One assistant turn carrying `text` (a thinking block rides along)."""
    return _event(ev_type, message={"role": "assistant", "content": [
        {"type": "thinking", "thinking": "internal reasoning"},
        {"type": "text", "text": text},
    ]})


def _stream(*lines):
    return "\n".join(lines) + "\n"


def test_the_captured_real_stream_parses_and_counts_one_turn():
    text = open(_FIXTURE, encoding="utf-8").read()
    # The capture was `reply with exactly: OK` -- no verdict in it.
    assert review_gate._extract_from_stream_json(text) is None
    assert review_gate._num_turns_from_stream_json(text) == 1


def test_num_turns_counts_turn_start_events():
    s = _stream(*([_event("turn_start") for _ in range(3)]
                  + [_event("message_start") for _ in range(2)]))
    assert review_gate._num_turns_from_stream_json(s) == 3
    # No JSON events at all (text-mode output): unknown, not zero.
    assert review_gate._num_turns_from_stream_json("plain text output\n") is None


def test_verdict_in_a_message_end_text_block_is_extracted():
    s = _stream(
        _event("session"),
        _event("agent_start"),
        _event("turn_start"),
        _event("message_start"),
        _turn(f"Here is my review:\n```json\n{json.dumps(VERDICT)}\n```\nDone."),
        _event("turn_end", message={"role": "assistant", "content": [
            {"type": "text", "text": "Here is my review:"}]}),
        _event("agent_end"),
        _event("agent_settled"),
    )
    assert review_gate._extract_from_stream_json(s) == VERDICT


def test_verdict_then_a_stray_ack_turn_still_yields_the_verdict():
    s = _stream(
        _event("turn_start"),
        _turn(f"```json\n{json.dumps(VERDICT)}\n```"),
        _event("turn_end", message={"role": "assistant", "content": [
            {"type": "text", "text": f"```json\n{json.dumps(VERDICT)}\n```"}]}),
        # A later notification ack: text, no verdict JSON.
        _event("turn_start"),
        _turn("OK, noted."),
        _event("turn_end", message={"role": "assistant", "content": [
            {"type": "text", "text": "OK, noted."}]}),
    )
    assert review_gate._extract_from_stream_json(s) == VERDICT


def test_no_verdict_anywhere_is_none_so_the_caller_denies():
    s = _stream(
        _event("turn_start"),
        _turn("I could not review this."),
        # Unparseable/garbage lines between events are skipped, not fatal.
        "not json at all {{{",
        "",
        _event("turn_end", message={"role": "assistant", "content": [
            {"type": "text", "text": "I could not review this."}]}),
    )
    assert review_gate._extract_from_stream_json(s) is None


def test_text_mode_and_stream_mode_extract_the_same_verdict_object():
    """The extractor's contract: same verdict object from both paths (the
    plan's C2 acceptance clause)."""
    raw = f"Review complete.\n{json.dumps(VERDICT)}\n(see above)"
    wrapped = _stream(
        _event("turn_start"),
        _turn(f"Review complete.\n{json.dumps(VERDICT)}\n(see above)"),
    )
    assert review_gate._extract_json(raw) == VERDICT
    assert review_gate._extract_from_stream_json(wrapped) == review_gate._extract_json(raw)
