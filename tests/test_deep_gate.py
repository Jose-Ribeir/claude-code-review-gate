"""Risk gate + deep-reviewer lane (review-gate v2, t10).

Tier 2 is the slow lane: a specialist deep-reviewer pass, run under the
existing async supervisor ("deny the first push, join the recorded verdict on
retry"), for pushes whose changed paths hit a deterministic risk glob or
whose spotter risk score crosses the configured threshold. The gate claims
the tier BEFORE spawning the supervisor, so deny-first-push + retry-join
covers the deep pass exactly like a slow fast-lane review.

The deep pass reuses the existing findings contract and the unchanged
code-filter / compute_verdict pipeline (ADR 0001: no new verdict authority):
a deep finding blocks iff severity high, confidence >= 0.7, and the falsify
pass cannot disprove it -- exactly the fast lane's bar.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_STUB = os.path.join(_HERE, "stub_reviewer.py")

sys.path.insert(0, _SCRIPTS)
from hook_output import parse_pretooluse  # noqa: E402
_spec = importlib.util.spec_from_file_location("review_gate_t10", _GATE)
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


def _push(work, env, timeout=120):
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash", "cwd": str(work),
                          "tool_input": {"command": "git push origin main"}})
    proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                          capture_output=True, text=True, cwd=str(work), env=env,
                          timeout=timeout)
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


def _review_calls(tmp_path):
    trace = tmp_path / "stub.trace"
    if not trace.exists():
        return []
    return [json.loads(x) for x in trace.read_text().splitlines() if x.strip()]


def _review_manifest(tmp_path):
    return next((c.get("manifest") or {}) for c in _review_calls(tmp_path)
                if not c.get("resolve_file"))


def _meta(tmp_path, files):
    """A repo whose push changes exactly these files, plus the meta dict the
    push hook would build for it (see review-gate.py's hook path)."""
    work = _repo(tmp_path, {"a.txt": "one\n"})
    tip = _commit(work, files)
    base = _git(["rev-parse", "origin/main"], cwd=work)
    return work, tip, {
        "tip": tip, "branch": "main", "base": base,
        "range": base + ".." + tip, "repo_root": str(work),
        "git_dir": str(work / ".git"), "commit_count": "1", "push_ref": "",
    }


# ---------------------------------------------------------------------------
# Tier-2 model plumbing (mirrors the OCR_TIER1_MODEL pattern from t07)
# ---------------------------------------------------------------------------

class TestTier2ModelPlumbing:
    def test_tier2_model_defaults_to_flagship_open_model(self, monkeypatch):
        monkeypatch.delenv("OCR_TIER2_MODEL", raising=False)
        assert review_gate._read_tier2_model() == "kimi-k3"

    def test_tier2_model_from_env(self, monkeypatch):
        monkeypatch.setenv("OCR_TIER2_MODEL", "some-stronger-model")
        assert review_gate._read_tier2_model() == "some-stronger-model"

    def test_tier2_model_blank_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("OCR_TIER2_MODEL", "  ")
        assert review_gate._read_tier2_model() == "kimi-k3"

    def test_tier2_model_whitespace_trimmed(self, monkeypatch):
        monkeypatch.setenv("OCR_TIER2_MODEL", " some-stronger-model\n")
        assert review_gate._read_tier2_model() == "some-stronger-model"

    def test_fingerprint_env_vars_include_tier2_model(self):
        assert "OCR_TIER2_MODEL" in review_gate._FINGERPRINT_ENV_VARS

    def test_fingerprint_agents_include_deep_reviewer(self):
        assert "deep-reviewer.md" in review_gate._FINGERPRINT_AGENTS


class TestTier2ProviderGuard:
    @pytest.mark.parametrize("model", ["claude-opus-4-5", "gpt-5"])
    def test_blocked_provider_rejected_naming_var_and_value(self, monkeypatch, model):
        monkeypatch.setenv("OCR_TIER2_MODEL", model)
        with pytest.raises(review_gate.ReviewGateError) as ei:
            review_gate._check_provider_guard()
        msg = str(ei.value)
        assert "OCR_TIER2_MODEL" in msg
        assert model in msg

    def test_default_passes(self, monkeypatch):
        monkeypatch.delenv("OCR_TIER2_MODEL", raising=False)
        review_gate._check_provider_guard()  # must not raise


# ---------------------------------------------------------------------------
# Deep-pass wall-clock budget
# ---------------------------------------------------------------------------

class TestDeepTimeout:
    def test_default_is_ten_minutes(self, monkeypatch):
        monkeypatch.delenv("OCR_DEEP_TIMEOUT", raising=False)
        assert review_gate._read_deep_timeout() == 600

    def test_env_value_honoured(self, monkeypatch):
        monkeypatch.setenv("OCR_DEEP_TIMEOUT", "900")
        assert review_gate._read_deep_timeout() == 900

    def test_env_value_clamped_at_one_minute(self, monkeypatch):
        monkeypatch.setenv("OCR_DEEP_TIMEOUT", "5")
        assert review_gate._read_deep_timeout() == 60

    def test_non_numeric_falls_back(self, monkeypatch):
        monkeypatch.setenv("OCR_DEEP_TIMEOUT", "soonish")
        assert review_gate._read_deep_timeout() == 600


# ---------------------------------------------------------------------------
# Risk glob matching
# ---------------------------------------------------------------------------

class TestRiskGlobMatch:
    @pytest.mark.parametrize("path", [
        "db/migrations/001_add_users.py",
        "migrations/001_add_users.sql",
        "src/server/auth/oauth.py",
        "auth/session.py",
        "payments/stripe.py",
        "core/concurrency/locks.py",
        "api/v1/endpoints.py",
        "src/api/routes.py",
    ])
    def test_starter_set_hits(self, path):
        assert any(review_gate._risk_glob_match(path, g)
                   for g in review_gate._DEFAULT_RISK_GLOBS), path

    @pytest.mark.parametrize("path", [
        "app/x.py",
        "lib/helpers.py",
        "docs/README.md",
        "src/server/routes.py",
        "src/authetication_notes.py",  # substring is not a segment
    ])
    def test_benign_paths_do_not_hit(self, path):
        assert not any(review_gate._risk_glob_match(path, g)
                       for g in review_gate._DEFAULT_RISK_GLOBS), path

    def test_leading_double_star_matches_repo_root_too(self):
        assert review_gate._risk_glob_match("auth/login.py", "**/auth/**")
        assert review_gate._risk_glob_match("src/auth/login.py", "**/auth/**")

    def test_non_matching_glob(self):
        assert not review_gate._risk_glob_match("app/x.py", "db/**")


# ---------------------------------------------------------------------------
# Escalation evaluation (gate-side, before claim)
# ---------------------------------------------------------------------------

class TestDeepEscalation:
    def test_risky_path_escalates_with_reasons(self, tmp_path):
        work, tip, meta = _meta(tmp_path, {"db/migrations/001_users.py": "x = 1\n"})
        deep, reasons = review_gate._deep_escalation(meta)
        assert deep is True
        assert reasons and any("db/migrations/001_users.py" in r for r in reasons)

    def test_benign_push_stays_fast(self, tmp_path):
        work, tip, meta = _meta(tmp_path, {"app/x.py": "x = 1\n"})
        deep, reasons = review_gate._deep_escalation(meta)
        assert deep is False
        assert reasons == []

    def test_rule_json_risk_globs_extend_the_starter_set(self, tmp_path):
        work, tip, meta = _meta(tmp_path, {"app/x.py": "x = 1\n"})
        ocr = Path(work) / ".ocr"
        ocr.mkdir()
        (ocr / "rule.json").write_text(json.dumps({"risk_globs": ["app/**"]}))
        deep, reasons = review_gate._deep_escalation(meta)
        assert deep is True
        assert any("app/x.py" in r for r in reasons)

    def test_malformed_rule_json_degrades_to_defaults(self, tmp_path):
        work, tip, meta = _meta(tmp_path, {"db/migrations/001_users.py": "x = 1\n"})
        ocr = Path(work) / ".ocr"
        ocr.mkdir()
        (ocr / "rule.json").write_text("{not json")
        deep, reasons = review_gate._deep_escalation(meta)
        assert deep is True  # the starter set still matches

    def test_noise_files_alone_do_not_escalate(self, tmp_path):
        """Paths the review-allowlist excludes are not review content: a
        risky-glob match on one of them must not claim a deep pass."""
        work, tip, meta = _meta(tmp_path, {"tests/test_auth_flow.py": "x = 1\n"})
        deep, reasons = review_gate._deep_escalation(meta)
        assert deep is False
        assert reasons == []


# ---------------------------------------------------------------------------
# End to end: the deep lane rides the existing async supervisor
# ---------------------------------------------------------------------------

class TestDeepLaneE2E:
    def test_risky_push_denies_first_then_retry_joins_the_deep_verdict(self, tmp_path):
        """Accept-when scenario: a risky-path push claims a deep pass BEFORE
        spawning the supervisor; the first push is denied while it runs, the
        retry joins the recorded verdict -- no second review."""
        work = _repo(tmp_path, {"a.txt": "one\n"})
        tip = _commit(work, {"db/migrations/001_users.py": "x = 1\n"})
        env = _env(tmp_path, STUB_SLEEP=35, STUB_VERDICT="block")
        decision, reason = _push(work, env, timeout=90)
        assert decision == "deny" and "still running" in reason
        # The lane was recorded at claim time, before the supervisor did any
        # work -- the deny/retry-join semantics cover the whole deep pass.
        common = review_gate._git_common_dir(str(work))
        st = review_gate._read_state(review_gate._state_path(common, tip)) or {}
        assert st.get("tier") == "deep"
        # Kimchi port: hook mode is fire-and-forget (the verdict is reported by a
        # LATER call), so poll the deep pass done -- the retry then joins its
        # recorded verdict instead of seeing "still running".
        st = _state(work, tip)
        assert st["state"] == "done" and st["tier"] == "deep"
        decision, reason = _push(work, env, timeout=90)  # retry joins the same run
        assert decision == "deny", decision
        assert "stub high finding" in reason
        calls = _review_calls(tmp_path)
        assert len(calls) == 1  # one deep pass; no fast pass plus a deep pass
        manifest = _review_manifest(tmp_path)
        assert manifest["deep_review"] is True
        assert manifest["tier2_model"] == "kimi-k3"
        assert manifest["spotter_model"]  # the deep pass still runs the spotter for demands
        assert "db/migrations/001_users.py" in manifest["paths"]
        assert manifest.get("risk_threshold") == 7
        assert any("db/migrations/001_users.py" in r
                   for r in manifest.get("deep_reasons", []))

    def test_benign_push_stays_in_the_fast_lane(self, tmp_path):
        work = _repo(tmp_path, {"a.txt": "one\n"})
        tip = _commit(work, {"app/x.py": "x = 1\n"})
        decision, reason = _push(work, _env(tmp_path, STUB_VERDICT="block"))
        assert decision == "deny" and "stub high finding" in reason
        st = _state(work, tip)
        assert st["state"] == "done" and st.get("tier") == "fast"
        manifest = _review_manifest(tmp_path)
        assert manifest.get("deep_review") is not True
        assert manifest.get("tier1_model") == "kimi-k3"

    def test_rule_json_escalation_and_threshold_reach_the_manifest(self, tmp_path):
        work = _repo(tmp_path, {"a.txt": "one\n"})
        tip = _commit(work, {
            "app/x.py": "x = 1\n",
            ".ocr/rule.json": json.dumps({"risk_globs": ["app/**"],
                                          "risk_threshold": 9}),
        })
        decision, reason = _push(work, _env(tmp_path))
        assert decision == "pass", reason
        st = _state(work, tip)
        assert st.get("tier") == "deep"
        manifest = _review_manifest(tmp_path)
        assert manifest["deep_review"] is True
        assert manifest["risk_threshold"] == 9

    def test_a_falsified_deep_finding_does_not_block(self, tmp_path):
        """The falsify pass runs unchanged downstream of the deep reviewer:
        output whose only accusation could not survive it contains no
        blocking finding and the push passes."""
        work = _repo(tmp_path, {"a.txt": "one\n"})
        tip = _commit(work, {"payments/stripe.py": "x = 1\n"})
        # Kimchi port (blocking bar, ocr_verdict D15): upstream never blocks on
        # a medium, but kimchi blocks medium at confidence >= 0.7 -- so the
        # non-blocking warning here is a medium at LOW confidence instead of
        # the upstream STUB_VERDICT=warn stub finding (which would block).
        findings = {"payments/stripe.py": {"severity": "medium",
                                           "content": "speculative issue",
                                           "confidence": 0.4}}
        decision, reason = _push(work, _env(tmp_path, STUB_FINDINGS_FOR=json.dumps(findings)))
        assert decision == "pass", reason
        st = _state(work, tip)
        assert st["state"] == "done" and st["tier"] == "deep"
        assert st.get("verdict") == "warn"

    def test_a_surviving_high_low_confidence_deep_finding_blocks_under_d15(self, tmp_path):
        """Escalation swaps the reviewer, never the bar: the deep lane keeps
        compute_verdict as the only verdict authority. DIVERGES from the claude
        variant: upstream's bar needs high + confidence >= 0.7, so this push
        PASSES there; kimchi's D15 bar blocks ANY high severity regardless of
        confidence, so the same finding blocks here (t12 parity item)."""
        work = _repo(tmp_path, {"a.txt": "one\n"})
        tip = _commit(work, {"auth/oauth.py": "x = 1\n"})
        findings = {"auth/oauth.py": {"severity": "high", "content": "speculative race",
                                      "confidence": 0.4}}
        decision, reason = _push(work, _env(tmp_path, STUB_FINDINGS_FOR=json.dumps(findings)))
        assert decision == "deny", reason
        st = _state(work, tip)
        assert st["state"] == "done" and st["tier"] == "deep"
        assert st.get("verdict") == "block"
