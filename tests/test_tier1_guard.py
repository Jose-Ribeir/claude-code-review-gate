"""Tier-1 reviewer wiring and the provider guard (review-gate v2, t07).

The tier-1 (fast-lane) reviewer's model is configurable with OCR_TIER1_MODEL
(default kimi-k3). The gate carries the configured value into every review
manifest (`tier1_model`) the same way it carries the spotter model -- the
headless review session's allowlist cannot read the env var -- and, because
the model changes the review criteria, puts the env var into the review
fingerprint so cached ledger records invalidate.

The provider guard is a startup validator: after config/env resolution and
BEFORE any review work it rejects configured review models whose provider
(anthropic, openai) would violate the subscription account-lock terms
(spec story 18). A misconfiguration fails loudly with the offending variable
and value named, and no reviewer process is ever spawned.
"""
import importlib.util
import json
import os
import subprocess
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_STUB = os.path.join(_HERE, "stub_reviewer.py")

sys.path.insert(0, _SCRIPTS)
from hook_output import parse_pretooluse  # noqa: E402
_spec = importlib.util.spec_from_file_location("review_gate_t7", _GATE)
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


def _review_manifest(tmp_path):
    trace = tmp_path / "stub.trace"
    calls = [json.loads(x) for x in trace.read_text().splitlines() if x.strip()]
    return next((c.get("manifest") or {}) for c in calls if not c.get("resolve_file"))


def _review_calls(tmp_path):
    trace = tmp_path / "stub.trace"
    if not trace.exists():
        return []
    return [json.loads(x) for x in trace.read_text().splitlines() if x.strip()]


# ---------------------------------------------------------------------------
# Tier-1 model plumbing (mirrors the OCR_SPOTTER_MODEL pattern)
# ---------------------------------------------------------------------------

class TestTier1ModelPlumbing:
    def test_tier1_model_defaults_to_flagship_open_model(self, monkeypatch):
        monkeypatch.delenv("OCR_TIER1_MODEL", raising=False)
        assert review_gate._read_tier1_model() == "kimi-k3"

    def test_tier1_model_from_env(self, monkeypatch):
        monkeypatch.setenv("OCR_TIER1_MODEL", "some-strong-model")
        assert review_gate._read_tier1_model() == "some-strong-model"

    def test_tier1_model_blank_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("OCR_TIER1_MODEL", "  ")
        assert review_gate._read_tier1_model() == "kimi-k3"

    def test_tier1_model_whitespace_trimmed(self, monkeypatch):
        monkeypatch.setenv("OCR_TIER1_MODEL", "  some-strong-model \n")
        assert review_gate._read_tier1_model() == "some-strong-model"

    def test_fingerprint_env_vars_include_tier1_model(self):
        assert "OCR_TIER1_MODEL" in review_gate._FINGERPRINT_ENV_VARS


class TestTier1Fingerprint:
    def test_tier1_model_change_invalidates_cached_records(self, tmp_path, monkeypatch):
        """A review cached under one reviewer model must not be trusted under
        another: the reviewer is the criteria, full stop."""
        work = _repo(tmp_path, {"lib.py": "def f():\n    return 1\n"})
        tip = _commit(work, {"lib.py": "def f():\n    return 2\n"})
        monkeypatch.delenv("OCR_TIER1_MODEL", raising=False)
        fp_default = review_gate._compute_fingerprint(str(work), tip)
        monkeypatch.setenv("OCR_TIER1_MODEL", "some-strong-model")
        fp_other = review_gate._compute_fingerprint(str(work), tip)
        assert fp_default != fp_other
        parts = review_gate._fingerprint_parts(str(work), tip)
        assert "env:OCR_TIER1_MODEL" in parts


# ---------------------------------------------------------------------------
# Provider guard: classification
# ---------------------------------------------------------------------------

class TestModelProvider:
    @pytest.mark.parametrize("model", [
        "kimi-k3", "glm-5.3", "deepseek-v4-flash",
        "qwen3-coder", "llama-4", "mistral-large", "some-unknown-model",
    ])
    def test_open_and_unknown_models_are_not_blocked(self, model):
        assert review_gate._model_provider(model) not in ("anthropic", "openai")

    @pytest.mark.parametrize("model", [
        "claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-3-5",
        "claude-3-5-sonnet", "CLAUDE-OPUS-4-5", "Sonnet", "haiku", "opus",
    ])
    def test_anthropic_models_classify(self, model):
        assert review_gate._model_provider(model) == "anthropic"

    @pytest.mark.parametrize("model", [
        "gpt-5", "gpt-4o", "GPT-5", "chatgpt-4o", "o1-preview", "o3-mini", "o4-mini",
    ])
    def test_openai_models_classify(self, model):
        assert review_gate._model_provider(model) == "openai"

    def test_blank_and_none_classify_as_unknown(self):
        assert review_gate._model_provider("") not in ("anthropic", "openai")


# ---------------------------------------------------------------------------
# Provider guard: the startup validator
# ---------------------------------------------------------------------------

class TestProviderGuard:
    @pytest.mark.parametrize("model", ["kimi-k3", "glm-5.3", "deepseek-v4-flash"])
    def test_allowed_tier1_models_pass(self, monkeypatch, model):
        monkeypatch.setenv("OCR_TIER1_MODEL", model)
        review_gate._check_provider_guard()  # must not raise

    @pytest.mark.parametrize("model", ["kimi-k3", "glm-5.3", "deepseek-v4-flash"])
    def test_allowed_spotter_models_pass(self, monkeypatch, model):
        monkeypatch.setenv("OCR_SPOTTER_MODEL", model)
        review_gate._check_provider_guard()  # must not raise

    def test_defaults_pass(self, monkeypatch):
        monkeypatch.delenv("OCR_TIER1_MODEL", raising=False)
        monkeypatch.delenv("OCR_SPOTTER_MODEL", raising=False)
        review_gate._check_provider_guard()  # must not raise

    @pytest.mark.parametrize("model", ["claude-opus-4-5", "claude-sonnet-4-5", "sonnet"])
    def test_anthropic_tier1_rejected_naming_var_and_value(self, monkeypatch, model):
        monkeypatch.setenv("OCR_TIER1_MODEL", model)
        with pytest.raises(review_gate.ReviewGateError) as ei:
            review_gate._check_provider_guard()
        msg = str(ei.value)
        assert "OCR_TIER1_MODEL" in msg
        assert model in msg
        assert "anthropic" in msg

    @pytest.mark.parametrize("model", ["gpt-5", "gpt-4o", "o3-mini"])
    def test_openai_tier1_rejected_naming_var_and_value(self, monkeypatch, model):
        monkeypatch.setenv("OCR_TIER1_MODEL", model)
        with pytest.raises(review_gate.ReviewGateError) as ei:
            review_gate._check_provider_guard()
        msg = str(ei.value)
        assert "OCR_TIER1_MODEL" in msg
        assert model in msg
        assert "openai" in msg

    @pytest.mark.parametrize("model", ["claude-opus-4-5", "gpt-5"])
    def test_spotter_var_also_guarded(self, monkeypatch, model):
        monkeypatch.setenv("OCR_SPOTTER_MODEL", model)
        with pytest.raises(review_gate.ReviewGateError) as ei:
            review_gate._check_provider_guard()
        assert "OCR_SPOTTER_MODEL" in str(ei.value)

    def test_multiple_violations_reported_together(self, monkeypatch):
        monkeypatch.setenv("OCR_TIER1_MODEL", "claude-opus-4-5")
        monkeypatch.setenv("OCR_SPOTTER_MODEL", "gpt-5")
        with pytest.raises(review_gate.ReviewGateError) as ei:
            review_gate._check_provider_guard()
        msg = str(ei.value)
        assert "OCR_TIER1_MODEL" in msg and "OCR_SPOTTER_MODEL" in msg

    def test_violations_report_is_machine_checkable(self, monkeypatch):
        monkeypatch.setenv("OCR_TIER1_MODEL", "gpt-5")
        bad = review_gate._provider_guard_violations()
        assert bad == [("OCR_TIER1_MODEL", "gpt-5", "openai")]

    def test_no_violations_report_is_empty(self, monkeypatch):
        monkeypatch.delenv("OCR_TIER1_MODEL", raising=False)
        monkeypatch.delenv("OCR_SPOTTER_MODEL", raising=False)
        assert review_gate._provider_guard_violations() == []


# ---------------------------------------------------------------------------
# End to end: the configured model reaches the review manifest
# ---------------------------------------------------------------------------

class TestManifestPlumbing:
    def test_manifest_carries_tier1_model(self, tmp_path):
        work = _repo(tmp_path, {"lib.py": "def f():\n    return 1\n"})
        tip = _commit(work, {"lib.py": "def f():\n    return 2\n"})
        decision, reason = _push(work, _env(tmp_path))
        assert decision == "pass", reason
        assert _state(work, tip).get("state") == "done"
        manifest = _review_manifest(tmp_path)
        assert manifest["tier1_model"] == "kimi-k3"

    def test_manifest_carries_configured_tier1_model(self, tmp_path):
        work = _repo(tmp_path, {"lib.py": "def f():\n    return 1\n"})
        tip = _commit(work, {"lib.py": "def f():\n    return 2\n"})
        decision, reason = _push(work, _env(tmp_path, OCR_TIER1_MODEL="some-strong-model"))
        assert decision == "pass", reason
        assert _state(work, tip).get("state") == "done"
        manifest = _review_manifest(tmp_path)
        assert manifest["tier1_model"] == "some-strong-model"

    def test_golden_argv_path_carries_no_manifest_change(self, tmp_path):
        """OCR_PRECOMPUTED_DIFFS=0 keeps the byte-identical pre-v2 command
        line: the tier1 var must not force a manifest by itself."""
        work = _repo(tmp_path, {"lib.py": "def f():\n    return 1\n"})
        _commit(work, {"lib.py": "def f():\n    return 2\n"})
        # commit_intent (intent.messages, default on) is itself manifest content.
        (work / ".ocr").mkdir()
        (work / ".ocr" / "config.json").write_text(json.dumps({"intent": {"messages": False}}))
        decision, reason = _push(work, _env(tmp_path, OCR_PRECOMPUTED_DIFFS="0",
                                            OCR_TIER1_MODEL="some-strong-model"))
        assert decision == "pass", reason
        calls = _review_calls(tmp_path)
        assert calls and all(c.get("paths_file") is None for c in calls)


# ---------------------------------------------------------------------------
# End to end: the provider guard aborts BEFORE any review work
# ---------------------------------------------------------------------------

class TestProviderGuardE2E:
    def test_blocked_tier1_model_denies_push_and_spawns_no_reviewer(self, tmp_path):
        work = _repo(tmp_path, {"lib.py": "def f():\n    return 1\n"})
        tip = _commit(work, {"lib.py": "def f():\n    return 2\n"})
        decision, reason = _push(work, _env(tmp_path, OCR_TIER1_MODEL="claude-opus-4-5"))
        assert decision == "deny", decision
        assert "OCR_TIER1_MODEL" in reason and "claude-opus-4-5" in reason
        # No review state, no reviewer process: nothing flowed to any model.
        assert _review_calls(tmp_path) == []
        common = review_gate._git_common_dir(str(work))
        assert not review_gate._read_state(review_gate._state_path(common, tip))

    def test_blocked_spotter_model_denies_push(self, tmp_path):
        work = _repo(tmp_path, {"lib.py": "def f():\n    return 1\n"})
        _commit(work, {"lib.py": "def f():\n    return 2\n"})
        decision, reason = _push(work, _env(tmp_path, OCR_SPOTTER_MODEL="gpt-5"))
        assert decision == "deny", decision
        assert "OCR_SPOTTER_MODEL" in reason and "gpt-5" in reason
        assert _review_calls(tmp_path) == []

    def test_allowed_models_still_pass(self, tmp_path):
        work = _repo(tmp_path, {"lib.py": "def f():\n    return 1\n"})
        tip = _commit(work, {"lib.py": "def f():\n    return 2\n"})
        decision, reason = _push(work, _env(tmp_path, OCR_TIER1_MODEL="glm-5.3",
                                            OCR_SPOTTER_MODEL="deepseek-v4-flash"))
        assert decision == "pass", reason
        assert _state(work, tip).get("state") == "done"

    def test_guard_does_not_block_non_push_commands(self, tmp_path):
        """The guard vets push-time config, not ambient shell commands: a
        misconfigured var must never deny a read-only git call."""
        work = _repo(tmp_path, {"lib.py": "def f():\n    return 1\n"})
        _commit(work, {"lib.py": "def f():\n    return 2\n"})
        env = _env(tmp_path, OCR_TIER1_MODEL="claude-opus-4-5")
        payload = json.dumps({"session_id": "s1", "tool_name": "Bash", "cwd": str(work),
                              "tool_input": {"command": "git status"}})
        proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                              capture_output=True, text=True, cwd=str(work), env=env,
                              timeout=30)
        assert proc.returncode == 0, proc.stderr
        decision, _ = parse_pretooluse(proc.stdout)
        assert decision in ("pass", None), proc.stdout
