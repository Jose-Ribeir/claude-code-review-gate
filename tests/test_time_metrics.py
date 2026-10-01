"""0.9.6 time metrics: per call, per phase, per chunk -- counts and timings only.

Unit tests cover the stream-json parser, the log line formatter (the privacy
rule), log rotation and the report; the end-to-end tests run the real hook and
detached supervisor against the stub reviewer in its `STUB_STREAM` mode, which
answers as `claude --output-format stream-json` does (tool events carrying a
path and code, one Agent call, a `result` event).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_async_gate as ag  # noqa: E402  (helpers: _big_repo, _chunk_env, _hook, ...)
from test_async_gate import review_gate  # noqa: E402

import ocr_telemetry  # noqa: E402

# Text the stub puts in tool inputs and file names the repo uses: none of it may
# reach the metrics.
_FORBIDDEN = ("/secret", "SECRET_CODE_TOKEN", "hunter2", "mod0.py", "mod1.py", "app.py")


def _ts(offset, base=1_700_000_000.0):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(base + int(offset))) + (
        ".%03dZ" % int((offset % 1) * 1000))


def _ev(kind, offset, *blocks, **extra):
    msg = {"role": "assistant" if kind == "assistant" else "user", "content": list(blocks)}
    return json.dumps(dict(type=kind, timestamp=_ts(offset), message=msg, **extra))


def _use(name, tid, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def _res(tid):
    return {"type": "tool_result", "tool_use_id": tid, "content": "x"}


# --- stream_stats --------------------------------------------------------------

def test_stream_stats_reads_result_tools_and_agent_wall_time():
    base = 1_700_000_000.0
    lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        _ev("assistant", 2.0, _use("Read", "t1", file_path="/p/a.py")),
        _ev("user", 2.5, _res("t1")),
        _ev("assistant", 3.0, _use("Agent", "t2", prompt="x" * 1000)),
        _ev("user", 13.0, _res("t2")),
        json.dumps({"type": "result", "num_turns": 7, "duration_ms": 20000,
                    "duration_api_ms": 15000, "total_cost_usd": 0.1234}),
    ]
    st = ocr_telemetry.stream_stats("\n".join(lines), started_at=base - 1)
    assert st["turns"] == 7 and st["api_s"] == 15.0 and st["cost_usd"] == 0.1234
    assert st["first_event_s"] == 3.0          # first timestamped event minus process start
    assert st["tools"]["Read"][0] == 1 and st["tools"]["Agent"][0] == 1
    assert st["agent_calls"] == 1 and st["agent_input_bytes"] >= 1000
    assert st["agent_wall_s"] == 10.0          # tool_use -> tool_result of the Agent call
    assert st["orchestrator_s"] == 10.0        # the call's 20 s minus those 10
    assert st["events"] == 6


def test_stream_stats_counts_overlapping_agent_calls_once():
    lines = [
        _ev("assistant", 0, _use("Agent", "a", prompt="p"), _use("Task", "b", prompt="p")),
        _ev("user", 6, _res("a")),
        _ev("user", 8, _res("b")),
        json.dumps({"type": "result", "num_turns": 2, "duration_ms": 10000}),
    ]
    st = ocr_telemetry.stream_stats("\n".join(lines))
    assert st["agent_wall_s"] == 8.0 and st["agent_calls"] == 2


@pytest.mark.parametrize("text", ["", None, "not json at all", '{"status": "success"}',
                                  '{broken\n{"type": \n', "[1, 2, 3]"])
def test_stream_stats_never_raises_on_other_output(text):
    assert ocr_telemetry.stream_stats(text) == {}


def test_a_timeout_keeps_the_partial_stream_and_never_changes_the_error(monkeypatch):
    """Cut off mid-review: the stats of what ran are kept for the telemetry, and
    the call still fails exactly as before."""
    partial = "\n".join([
        _ev("assistant", 0, _use("Agent", "a", prompt="p")),
        _ev("user", 4, _res("a")),
    ])

    class _Proc:
        pid = 4242
        returncode = None

        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def communicate(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("claude", timeout)
            return partial, ""

        def kill(self):
            pass

    monkeypatch.setattr(review_gate.subprocess, "Popen", _Proc)
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    monkeypatch.delenv("OCR_REVIEWER_CMD", raising=False)
    review_gate._TELE.clear()
    with pytest.raises(review_gate.ReviewGateError) as ei:
        review_gate._run_review(".", "hook", None, "abc", "", timeout=1)
    assert ei.value.is_timeout
    entry = review_gate._TELE["calls"][-1]
    assert entry["outcome"] == "timeout" and entry["agent_wall_s"] == 4.0
    review_gate._TELE.clear()


# --- the privacy rule ------------------------------------------------------------

def test_metric_line_replaces_anything_that_is_not_a_number_or_a_short_token():
    line = ocr_telemetry.metric_line(
        "chunk", index=3, s=1.5, outcome="ok", ok=True,
        path="J:\\code\\app.py", code="SECRET = 'x'", sentence="a b", empty="",
        tools={"Read": 2, "Agent": [1, 99], "/etc/passwd": 1}, nothing=None)
    assert line.startswith("ts=") and "event=chunk" in line
    assert "index=3" in line and "s=1.5" in line and "outcome=ok" in line and "ok=1" in line
    assert "path=?" in line and "code=?" in line and "sentence=?" in line and "empty=?" in line
    assert "tools=Read:2,Agent:1,?:1" in line
    assert "nothing" not in line
    for bad in ("app.py", "SECRET", "passwd", "J:"):
        assert bad not in line


def test_metric_log_lines_cannot_carry_a_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    review_gate._metric_log("phase", name="/home/me/secret.py", s=1.0)
    text = (tmp_path / "review-gate-debug.log").read_text(encoding="utf-8")
    assert "name=?" in text and "secret" not in text and "/home" not in text


# --- rotation ----------------------------------------------------------------------

def test_rotating_append_shifts_files_and_drops_the_oldest(tmp_path):
    log = tmp_path / "x.log"
    for i in range(12):
        ocr_telemetry.rotating_append(log, f"line-{i:02d}-" + "x" * 40, max_bytes=100, keep=2)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["x.log", "x.log.1", "x.log.2"]
    kept = [ln for n in names for ln in (tmp_path / n).read_text().splitlines()]
    assert "line-11-" + "x" * 40 in kept          # the newest survives
    assert not any(ln.startswith("line-00-") for ln in kept)   # the oldest rotated out


def test_the_debug_log_rotates_by_size(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    monkeypatch.setattr(review_gate, "_DEBUG_LOG_BYTES", 300)
    for i in range(40):
        review_gate._metric_log("phase", name="plan", s=i)
    files = sorted(p.name for p in tmp_path.glob("review-gate-debug.log*"))
    assert "review-gate-debug.log.1" in files
    assert len(files) <= review_gate._DEBUG_LOG_KEEP + 1


def test_the_log_is_on_without_ocr_debug(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    monkeypatch.delenv("OCR_DEBUG", raising=False)
    review_gate._metric_log("phase", name="plan", s=0.5)
    assert "event=phase" in (tmp_path / "review-gate-debug.log").read_text(encoding="utf-8")


# --- phases, chunks, still-running -----------------------------------------------

def test_mark_phase_names_the_time_since_the_previous_mark(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    review_gate._TELE.clear()
    review_gate._PHASE_CLOCK["t"] = time.monotonic() - 2.0
    review_gate._mark_phase("plan", files=3)
    review_gate._mark_phase("impact")
    ph = review_gate._TELE["phases"]
    assert [p["name"] for p in ph] == ["plan", "impact"]
    assert 1.9 <= ph[0]["seconds"] < 10 and ph[0]["files"] == 3 and ph[1]["seconds"] < 1
    review_gate._TELE.clear()


def test_still_running_shows_the_average_chunk_time():
    st = {"tip": "a" * 40, "branch": "b", "chunks_done": 2, "chunks_total": 10,
          "chunk_avg_s": 372.4, "started_ts": time.time()}
    assert "chunk 3/10, avg 6m12s per chunk" in review_gate._still_running_reason(st, 60, "hook")
    st.pop("chunk_avg_s")
    assert "avg" not in review_gate._still_running_reason(st, 60, "hook")


# --- the report -----------------------------------------------------------------------

def _rec(**kw):
    base = {"ts": time.time(), "verdict": "pass", "seconds": 100.0}
    base.update(kw)
    return base


def test_report_shows_time_metrics():
    recs = [_rec(
        phases=[{"name": "plan", "seconds": 5.0, "files": 4}, {"name": "review", "seconds": 80.0}],
        chunks=[{"index": 0, "of": 2, "files": 2, "lines": 300, "outcome": "ok", "seconds": 40.0},
                {"index": 1, "of": 2, "files": 1, "lines": 100, "outcome": "timeout",
                 "seconds": 90.0}],
        calls=[{"kind": "review", "seconds": 40.0, "outcome": "ok", "turns": 11, "cost_usd": 0.5,
                "agent_input_bytes": 20480, "agent_wall_s": 30.0, "orchestrator_s": 10.0,
                "first_event_s": 2.5, "api_s": 33.0}]) for _ in range(3)]
    text = ocr_telemetry.report(recs)
    assert "Model call time" in text
    assert "orchestrator vs reviewer agent: 30s (25%) vs 90s (75%)" in text
    assert "Run phases" in text and "plan" in text and "review" in text
    assert "Chunks: 6" in text and "ok=3" in text and "timeout=3" in text
    rows = [ln for ln in text.splitlines() if ln.strip().startswith("review ") and "11.0" in ln]
    assert rows and "20.0" in rows[0]                 # agent-input KB


def test_report_copes_with_records_from_before_0_9_6():
    old = [_rec(calls=[{"kind": "review", "seconds": 12.0, "outcome": "ok"}])]
    text = ocr_telemetry.report(old)
    assert "Model calls" in text and "Model call time" not in text and "Run phases" not in text


# --- end to end ---------------------------------------------------------------------------

def _debug_log_text(tmp_path):
    return "\n".join(p.read_text(encoding="utf-8")
                     for p in sorted((tmp_path / "gate-data").glob("review-gate-debug.log*")))


def _tele_record(work):
    common = review_gate._git_common_dir(str(work))
    logs = sorted((Path(common) / "review-gate-telemetry").glob("*.jsonl"))
    return json.loads(logs[-1].read_text(encoding="utf-8").splitlines()[-1])


def test_a_chunked_run_logs_phases_chunks_and_calls_without_paths_or_code(tmp_path):
    repo = ag._big_repo(tmp_path, n_files=4)
    tip = ag._git(["rev-parse", "HEAD"], cwd=repo)
    env = ag._chunk_env(tmp_path, STUB_STREAM=4, STUB_STREAM_AGENT_S=0.3)

    decision, reason, _, _ = ag._hook(repo, "git push origin main", env, timeout=120)
    assert decision == "allow", reason
    st = ag._wait_state(repo, tip, {"done"})
    assert st["verdict"] == "pass"

    rec = _tele_record(repo)
    phases = [p["name"] for p in rec["phases"]]
    assert phases == ["worktree", "plan", "priors", "impact", "diffs", "review", "finish"], phases
    assert all(p["seconds"] >= 0 for p in rec["phases"])
    chunks = rec["chunks"]
    assert [(c["index"], c["of"], c["files"], c["outcome"]) for c in chunks] == [
        (i, 4, 1, "ok") for i in range(4)]
    assert all(c["lines"] == 1 and c["seconds"] > 0.2 for c in chunks)

    calls = [c for c in rec["calls"] if c["kind"] == "review"]
    assert len(calls) == 4
    for c in calls:
        assert c["turns"] == 6 and c["api_s"] == 1.23 and c["cost_usd"] == 0.0421
        assert {k: v[0] for k, v in c["tools"].items()} == {"Read": 2, "Bash": 2, "Agent": 1}
        assert c["agent_input_bytes"] > 50 and c["tools"]["Agent"][1] == c["agent_input_bytes"]
        assert 0.25 <= c["agent_wall_s"] < c["seconds"] and c["orchestrator_s"] >= 0
        assert c["first_event_s"] >= 0

    # The state file carries the average, for "chunk 3/10, avg ... per chunk".
    assert st.get("chunk_avg_s", 0) > 0.2

    # One line per phase, per chunk and per call, plus the run -- in the always-on log.
    log = _debug_log_text(tmp_path)
    kinds = [ln.split("event=")[1].split()[0] for ln in log.splitlines() if "event=" in ln]
    assert kinds.count("phase") == 7 and kinds.count("chunk") == 4
    assert kinds.count("call") == 4 and kinds.count("run") == 1
    assert "tools=Read:2,Bash:2,Agent:1" in log

    # Privacy: nothing from the tool inputs or the repo's paths in the log, or in
    # the new metric keys of the telemetry line.
    blob = json.dumps({"phases": rec["phases"], "chunks": chunks,
                       "calls": [{k: v for k, v in c.items() if k != "manifest_bytes"}
                                 for c in rec["calls"]]})
    for needle in _FORBIDDEN:
        assert needle not in log, needle
        assert needle not in blob, needle

    out = subprocess.run([sys.executable, ag._GATE, "--telemetry-report"], cwd=str(repo),
                         capture_output=True, text=True, timeout=60, env=env)
    assert out.returncode == 0, out.stderr
    assert "Model call time" in out.stdout and "Run phases" in out.stdout
    assert "Chunks: 4" in out.stdout and "orchestrator vs reviewer agent" in out.stdout


def test_a_long_stream_is_fully_drained_and_parsed(tmp_path):
    """Thousands of tool events (well past any pipe buffer) before the result:
    the review still completes and every event is counted."""
    work = ag._big_repo(tmp_path, n_files=1)
    tip = ag._git(["rev-parse", "HEAD"], cwd=work)
    env = ag._env(tmp_path, STUB_STREAM=3000, STUB_STREAM_AGENT_S=0.1)
    decision, reason, _, _ = ag._hook(work, "git push origin main", env, timeout=120)
    assert decision == "allow", reason
    ag._wait_state(work, tip, {"done"})
    call = _tele_record(work)["calls"][0]
    assert call["outcome"] == "ok" and call["turns"] == 3002
    assert call["tools"]["Read"][0] == 1500 and call["tools"]["Bash"][0] == 1500


def test_metrics_change_no_verdict(tmp_path):
    """The same push with and without a stream-json reply gets the same verdict."""
    verdicts = []
    for i, extra in enumerate(({}, {"STUB_STREAM": 3})):
        sub = tmp_path / f"r{i}"
        sub.mkdir()
        work = ag._big_repo(sub, n_files=2)
        tip = ag._git(["rev-parse", "HEAD"], cwd=work)
        env = ag._env(sub, STUB_VERDICT="warn", **extra)
        ag._hook(work, "git push origin main", env, timeout=120)
        st = ag._wait_state(work, tip, {"done"})
        verdicts.append((st["verdict"], st["finding_count"]))
    assert verdicts[0] == verdicts[1] == ("warn", 1)
