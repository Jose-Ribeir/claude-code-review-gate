"""Review-speed 16: the speed and failure fixes working together, end to end.

One sequence through the real gate (`--mode hook`, a detached supervisor) behind
the fake backend CLIs (tests/fake_backend.py), which delegate to the stub reviewer
(tests/stub_reviewer.py):

  1. a normal claude-harness push on the fast lane (an `.ocr/rule.json` whose risk
     globs the change does not touch -> no deep escalation); the inline wait is
     long enough that the FIRST hook call returns the verdict (no "still running"
     deny); exactly one session (no formatter session); the manifest carries the
     docs-checker-in-parallel flag; the spotter is skipped on the small diff
     (OCR_SPOTTER_SKIP=1);
  2. a first kimchi push probes the sandbox canary once;
  3. a second kimchi push uses the cached canary (no second probe);
  4. a kimchi push whose worker fails: the deny carries the worker's own error.

Assertions on the session count and order come from FAKE_TRACE (every launch of
the fake CLIs, canary probes included). Each push is a fresh repo sharing one state
dir (conftest gives each test its own), because the fake on Windows only sees the
first review's multi-line prompt per repo (see fake_backend.py).
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts"))
import fake_backend  # noqa: E402
from test_async_gate import _git, _hook, _wait_state  # noqa: E402
from test_claude_profile import _backend_env, _canaries, _launches, _push  # noqa: E402

_RULE = json.dumps({"risk_globs": ["payments/**"]})
_CANARY_PREFIX = "Sandbox self-check"
_SUMMARY_PREFIX = "Summarize this repository's documentation"


def _make_repo(tmp_path, name):
    """A repo with a pushed base (holding an .ocr/rule.json whose risk globs the
    change below does not match) and one small commit on top, not yet pushed."""
    base = tmp_path / name
    base.mkdir()
    remote, work = base / "origin.git", base / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=base)
    _git(["init", "-b", "main", str(work)], cwd=base)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        _git(["config", k, v], cwd=work)
    hooks = base / "no-hooks"
    hooks.mkdir()
    _git(["config", "core.hooksPath", str(hooks)], cwd=work)
    (work / "a.txt").write_text("one\n")
    (work / "README.md").write_text("# project\n")      # a docs corpus for the checker
    (work / "app").mkdir()
    (work / "app" / "x.py").write_text("x = 1\n")
    (work / ".ocr").mkdir()
    (work / ".ocr" / "rule.json").write_text(_RULE, encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    (work / "app" / "x.py").write_text("x = 2\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "feat"], cwd=work)
    return work, _git(["rev-parse", "HEAD"], cwd=work)


def _stub_calls(tmp_path):
    path = tmp_path / "stub.trace"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _sequence(tmp_path):
    """FAKE_TRACE as an ordered list of (flavor, "canary" | "docs-summary" | "review").
    The docs summary is the gate's own one-off generation for a repo without one;
    it is not a formatter or a second review."""
    def kind(prompt):
        if prompt.startswith(_CANARY_PREFIX):
            return "canary"
        return "docs-summary" if prompt.startswith(_SUMMARY_PREFIX) else "review"
    return [(t["flavor"], kind(t["prompt"]))
            for t in fake_backend.read_trace(tmp_path / "fake.trace")]


def _reviews(tmp_path):
    return [t for t in fake_backend.read_trace(tmp_path / "fake.trace")
            if not t["prompt"].startswith((_CANARY_PREFIX, _SUMMARY_PREFIX))]


def test_the_speed_and_failure_fixes_work_together(tmp_path):
    common = {"OCR_HARNESS": "claude", "OCR_SPOTTER_SKIP": "1", "OCR_INLINE_BUDGET": 90}

    # 1. claude push: fast lane, verdict in the first call, one session, no formatter.
    work, tip = _make_repo(tmp_path, "one")
    env = _backend_env(tmp_path, OCR_BACKEND="claude", STUB_RAW="1", STUB_SLEEP=3, **common)
    decision, reason, elapsed, _ = _hook(work, "git push origin main", env)
    assert decision == "pass", reason
    assert "still running" not in reason
    assert elapsed >= 2, "the stub takes ~3 s, so the verdict was not awaited"
    assert _wait_state(work, tip, {"done"})["verdict"] == "pass"
    assert _sequence(tmp_path) == [("claude", "docs-summary"), ("claude", "review")]
    launches = _reviews(tmp_path)                             # one review, no formatter
    assert len(launches) == 1
    assert launches[0]["prompt"].endswith(" --json")
    assert "formatter" not in launches[0]["prompt"].lower()

    calls = [c for c in _stub_calls(tmp_path) if c.get("manifest")]
    assert len(calls) == 1
    manifest = calls[0]["manifest"]
    assert manifest.get("deep_review") is not True            # fast lane
    assert manifest["docs_checker_parallel"] is True          # checker beside the reviewer
    assert manifest["spotter_skipped"] is True                # small diff, skip enabled
    assert "small change" in manifest["spotter_skip_reason"]

    # 2. first kimchi push probes the canary once, then reviews.
    work2, _ = _make_repo(tmp_path, "two")
    env = _backend_env(tmp_path, OCR_BACKEND="kimchi", STUB_RAW="1", **common)
    assert _push(work2, env)[0] == "pass"
    assert len(_canaries(tmp_path)) == 1

    # 3. second kimchi push: cached canary, still reviewed.
    work3, _ = _make_repo(tmp_path, "three")
    assert _push(work3, env)[0] == "pass"
    assert len(_canaries(tmp_path)) == 1                      # no second probe
    assert len(_reviews(tmp_path)) == 3                       # one review session per push
    assert _sequence(tmp_path) == [
        ("claude", "docs-summary"), ("claude", "review"),
        ("kimchi", "canary"), ("kimchi", "docs-summary"), ("kimchi", "review"),
        ("kimchi", "docs-summary"), ("kimchi", "review")]

    # 4. a scripted worker failure is reported with the worker's own error.
    work4, _ = _make_repo(tmp_path, "four")
    env = _backend_env(tmp_path, OCR_BACKEND="kimchi", FAKE_SCENARIO="auth", **common)
    decision, reason = _push(work4, env)
    assert decision == "deny"
    assert "OAuth session expired" in reason, reason
    assert len(_canaries(tmp_path)) == 1                      # still the one cached probe
    seq = _sequence(tmp_path)
    assert seq[:7] == [
        ("claude", "docs-summary"), ("claude", "review"),
        ("kimchi", "canary"), ("kimchi", "docs-summary"), ("kimchi", "review"),
        ("kimchi", "docs-summary"), ("kimchi", "review")]
    assert seq[7:] and all(flavor == "kimchi" and kind != "canary" for flavor, kind in seq[7:])
