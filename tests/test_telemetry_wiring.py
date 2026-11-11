"""Per-tier telemetry wiring (review-gate v2): record_tier_call's production
call sites.

ocr_telemetry.record_tier_call existed since t12 with no production caller.
The four v2 tiers each record at their real invocation seam:

* tier1 / tier2 -- review-gate.py's own spawn of the review session
  (_run_review, told its lane): the model is the tier's configured one, the
  duration is measured, and cost/tokens come from the CLI's stream-json when
  it reports them. The stub-reviewer e2e seam drives both lanes.
* collectors -- the ocr_collectors.py CLI (the manifest/CLI wrapper seam the
  skill already invokes): no model exists, so the record carries the run's
  duration and a chars//4 estimate of the evidence bytes delivered.
* spotter -- the ocr_demands.py CLI: the spotter is spawned inside the review
  session, so its output's mandatory passage through the validator is the one
  deterministic seam that sees it. It records the configured model and a
  chars//4 estimate of the output bytes (duration is unknowable there).
"""
import importlib.util
import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_STUB = os.path.join(_HERE, "stub_reviewer.py")

sys.path.insert(0, _SCRIPTS)
from hook_output import parse_pretooluse  # noqa: E402
import ocr_telemetry as ot  # noqa: E402

_spec = importlib.util.spec_from_file_location("review_gate_tw", _GATE)
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def _repo(tmp_path, files):
    tmp_path.mkdir(parents=True, exist_ok=True)
    remote, work = tmp_path / "origin.git", tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    for k, v in (("user.email", "t@t.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(["config", k, v], cwd=work)
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    _git(["config", "core.hooksPath", str(hooks)], cwd=work)
    for name, content in files.items():
        p = work / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    return work


def _commit(work, files):
    for name, content in files.items():
        p = work / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "c"], cwd=work)
    return _git(["rev-parse", "HEAD"], cwd=work)


def _env(tmp_path, **kw):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OCR_", "STUB_"))}
    # Kimchi port (D12): the gate and this test must share ONE external
    # state store; conftest points it at a per-test temp dir.
    env["OCR_STATE_DIR"] = os.environ["OCR_STATE_DIR"]
    env.update({"CLAUDE_PLUGIN_DATA": str(tmp_path / "gate-data"),
                "OCR_REVIEWER_CMD": f'"{sys.executable}" "{_STUB}"',
                "STUB_TRACE": str(tmp_path / "stub.trace"), "OCR_INLINE_BUDGET": "30"})
    env.update({k: str(v) for k, v in kw.items()})
    return env


def _push(work, env):
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash", "cwd": str(work),
                          "tool_input": {"command": "git push origin main"}})
    proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                          capture_output=True, text=True, cwd=str(work), env=env, timeout=90)
    assert proc.returncode == 0, proc.stderr
    return parse_pretooluse(proc.stdout)


def _state(work, tip, timeout=60):
    common = review_gate._git_common_dir(str(work))
    path = review_gate._state_path(common, tip)
    deadline = time.monotonic() + timeout
    while True:
        st = review_gate._read_state(path) or {}
        if st.get("state") in ("done", "failed") or time.monotonic() > deadline:
            return st
        time.sleep(0.2)


def _tier_records(work):
    """The push's tier_call records, read from the real telemetry log."""
    common = review_gate._git_common_dir(str(work))
    return [r for r in ot.load(common, days=1) if r.get("kind") == "tier_call"]


# ---------------------------------------------------------------------------
# _run_review's tier seam: the fast/deep review call records its own tier
# ---------------------------------------------------------------------------

class TestRunReviewTierSeam:
    def _fake_once(self, monkeypatch, stats=None):
        def fake(*_a, **_kw):
            review_gate._last_call_stats().update(stats or {})
            return {"status": "success", "findings": []}, True, "raw.json"
        monkeypatch.setattr(review_gate, "_run_review_once", fake)

    def test_tier1_call_records_configured_model_and_cli_cost(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OCR_TELEMETRY", "1")
        self._fake_once(monkeypatch, {"cost_usd": 0.0421})
        result, ran, _raw = review_gate._run_review(
            "repo", "git", tier=ot.TIER_REVIEW, common_dir=str(tmp_path))
        assert ran is True and result["status"] == "success"
        recs = [r for r in ot.load(str(tmp_path), days=1) if r.get("kind") == "tier_call"]
        assert [r["tier"] for r in recs] == ["tier1"]
        rec = recs[0]
        assert rec["model"] == review_gate._TIER1_MODEL
        assert rec["cost_usd"] == 0.0421
        assert rec["seconds"] is not None

    def test_tier2_call_records_the_deep_bucket(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OCR_TELEMETRY", "1")
        self._fake_once(monkeypatch, {"cost_usd": 0.5})
        review_gate._run_review("repo", "git", tier=ot.TIER_DEEP, common_dir=str(tmp_path))
        recs = [r for r in ot.load(str(tmp_path), days=1) if r.get("kind") == "tier_call"]
        assert [r["tier"] for r in recs] == ["tier2"]
        assert recs[0]["model"] == review_gate._TIER2_MODEL
        assert recs[0]["cost_usd"] == 0.5

    def test_untiered_calls_record_nothing(self, tmp_path, monkeypatch):
        """Resolver/recheck calls run through _run_review too, but they are not
        one of the four v2 tiers and must not appear in the per-tier report."""
        monkeypatch.setenv("OCR_TELEMETRY", "1")
        self._fake_once(monkeypatch, {"cost_usd": 0.01})
        review_gate._run_review("repo", "git")
        review_gate._run_review("repo", "git", resolve_file="priors.json")
        recs = [r for r in ot.load(str(tmp_path), days=1) if r.get("kind") == "tier_call"]
        assert recs == []


# ---------------------------------------------------------------------------
# End to end: a fast-lane push records the tier-1 cost
# ---------------------------------------------------------------------------

class TestFastLaneE2E:
    def test_a_fast_lane_push_records_the_tier1_cost(self, tmp_path):
        work = _repo(tmp_path, {"lib.py": "def f():\n    return 1\n"})
        _commit(work, {"lib.py": "def f():\n    return 2\n"})
        decision, reason = _push(work, _env(tmp_path, STUB_STREAM="2"))
        assert decision == "pass", reason
        tier1 = [r for r in _tier_records(work) if r["tier"] == "tier1"]
        assert len(tier1) == 1
        assert tier1[0]["model"] == "kimi-k3"
        # The stub's stream-json result event carries total_cost_usd 0.0421.
        assert tier1[0]["cost_usd"] == 0.0421
        assert tier1[0]["seconds"] is not None


# ---------------------------------------------------------------------------
# End to end: a risky push's deep pass records the tier-2 cost instead
# ---------------------------------------------------------------------------

class TestDeepLaneE2E:
    def test_a_risky_push_records_tier2_and_not_tier1(self, tmp_path):
        work = _repo(tmp_path, {"a.txt": "one\n"})
        tip = _commit(work, {"db/migrations/001_users.py": "x = 1\n"})
        decision, reason = _push(work, _env(tmp_path, STUB_STREAM="2"))
        assert decision == "pass", reason
        st = _state(work, tip)
        assert st["state"] == "done" and st["tier"] == "deep"
        recs = _tier_records(work)
        tier2 = [r for r in recs if r["tier"] == "tier2"]
        assert len(tier2) == 1
        assert tier2[0]["model"] == "kimi-k3"
        assert tier2[0]["cost_usd"] == 0.0421
        # The deep pass replaces the tier-1 review: no tier1 record lands.
        assert [r for r in recs if r["tier"] == "tier1"] == []


# ---------------------------------------------------------------------------
# The collectors CLI records its run (deterministic: duration + estimate)
# ---------------------------------------------------------------------------

class TestCollectorsCli:
    def _run_cli(self, repo, stdin_text, extra_env=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OCR_", "STUB_"))}
        env["OCR_TELEMETRY"] = "1"
        env.update(extra_env or {})
        return subprocess.run([sys.executable, os.path.join(_SCRIPTS, "ocr_collectors.py"),
                               "--repo-root", str(repo)],
                              input=stdin_text, capture_output=True, text=True, env=env)

    def test_the_collect_run_records_duration_and_estimate(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init", "-b", "main", "."], cwd=repo)
        proc = self._run_cli(repo, "{}")
        assert proc.returncode == 0, proc.stderr
        bundle = json.loads(proc.stdout)["bundle"]
        common = _git(["rev-parse", "--git-common-dir"], cwd=repo)
        common = os.path.normpath(os.path.join(str(repo), common))
        recs = [r for r in ot.load(common, days=1) if r.get("kind") == "tier_call"]
        assert len(recs) == 1
        rec = recs[0]
        assert rec["tier"] == "collectors"
        assert rec["model"] == ""  # deterministic tier: no model
        assert rec["token_estimate"] == ot.estimate_tokens(bundle["total_chars"])
        assert rec["seconds"] is not None

    def test_telemetry_off_records_nothing(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init", "-b", "main", "."], cwd=repo)
        proc = self._run_cli(repo, "{}", {"OCR_TELEMETRY": "0"})
        assert proc.returncode == 0, proc.stderr
        assert ot.load(str(repo / ".git"), days=1) == []


# ---------------------------------------------------------------------------
# The demands validator records the spotter's call (output bytes estimate)
# ---------------------------------------------------------------------------

_VALID_DEMANDS = json.dumps({
    "symbol_callers": ["get_settings"],
    "event_couples": [],
    "doc_refs": [],
    "conventions": [],
    "prose_pointers": [],
    "risk": {"score": 3, "reasons": ["touches settings"]},
})


class TestDemandsCli:
    def _run_cli(self, repo, stdin_text, extra_env=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OCR_", "STUB_"))}
        env["OCR_TELEMETRY"] = "1"
        env.update(extra_env or {})
        return subprocess.run([sys.executable, os.path.join(_SCRIPTS, "ocr_demands.py")],
                              input=stdin_text, capture_output=True, text=True,
                              env=env, cwd=str(repo))

    def test_spotter_output_passage_records_model_and_output_estimate(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init", "-b", "main", "."], cwd=repo)
        proc = self._run_cli(repo, _VALID_DEMANDS)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)["demands"] is not None
        recs = [r for r in ot.load(str(repo / ".git"), days=1) if r.get("kind") == "tier_call"]
        assert len(recs) == 1
        rec = recs[0]
        assert rec["tier"] == "spotter"
        assert rec["model"] == "deepseek-v4-flash"
        assert rec["token_estimate"] == len(_VALID_DEMANDS) // 4

    def test_spotter_model_comes_from_config(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init", "-b", "main", "."], cwd=repo)
        proc = self._run_cli(repo, _VALID_DEMANDS,
                             {"OCR_SPOTTER_MODEL": "some-fast-model"})
        assert proc.returncode == 0, proc.stderr
        recs = [r for r in ot.load(str(repo / ".git"), days=1) if r.get("kind") == "tier_call"]
        assert recs[0]["model"] == "some-fast-model"

    def test_malformed_spotter_output_still_records_its_estimate(self, tmp_path):
        """The spotter call happened whether or not its output validates."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init", "-b", "main", "."], cwd=repo)
        proc = self._run_cli(repo, "not json at all")
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)["demands"] is None
        recs = [r for r in ot.load(str(repo / ".git"), days=1) if r.get("kind") == "tier_call"]
        assert len(recs) == 1 and recs[0]["tier"] == "spotter"
