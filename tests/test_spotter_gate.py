"""Gate plumbing for the context spotter (review-gate v2, t04).

The spotter's model is configurable with OCR_SPOTTER_MODEL
(default deepseek-v4-flash). The gate carries the configured value into every
review manifest (the headless review session's allowlist has no env-reading
Bash, so the manifest is how the skill learns which model to spawn the spotter
subagent with), and, because the model changes the review criteria, puts the
env var into the review fingerprint so cached ledger records invalidate.
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
_spec = importlib.util.spec_from_file_location("review_gate_sg", _GATE)
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


# ---------------------------------------------------------------------------
# Module-level plumbing
# ---------------------------------------------------------------------------

class TestModelPlumbing:
    def test_spotter_model_defaults_to_fast_open_model(self, monkeypatch):
        monkeypatch.delenv("OCR_SPOTTER_MODEL", raising=False)
        assert review_gate._read_spotter_model() == "deepseek-v4-flash"

    def test_spotter_model_from_env(self, monkeypatch):
        monkeypatch.setenv("OCR_SPOTTER_MODEL", "some-fast-model")
        assert review_gate._read_spotter_model() == "some-fast-model"

    def test_spotter_model_blank_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("OCR_SPOTTER_MODEL", "  ")
        assert review_gate._read_spotter_model() == "deepseek-v4-flash"

    def test_fingerprint_env_vars_include_spotter_model(self):
        assert "OCR_SPOTTER_MODEL" in review_gate._FINGERPRINT_ENV_VARS


class TestFingerprint:
    def test_spotter_model_change_invalidates_cached_records(self, tmp_path, monkeypatch):
        """A review cached under one spotter model must not be trusted under
        another: the model steers the demands, the demands steer the review."""
        work = _repo(tmp_path, {"lib.py": "def f():\n    return 1\n"})
        tip = _commit(work, {"lib.py": "def f():\n    return 2\n"})
        monkeypatch.delenv("OCR_SPOTTER_MODEL", raising=False)
        fp_default = review_gate._compute_fingerprint(str(work), tip)
        monkeypatch.setenv("OCR_SPOTTER_MODEL", "some-fast-model")
        fp_other = review_gate._compute_fingerprint(str(work), tip)
        assert fp_default != fp_other
        parts = review_gate._fingerprint_parts(str(work), tip)
        assert "env:OCR_SPOTTER_MODEL" in parts


# ---------------------------------------------------------------------------
# End to end: the configured model reaches the review manifest
# ---------------------------------------------------------------------------

class TestManifestPlumbing:
    def test_manifest_carries_spotter_model(self, tmp_path):
        work = _repo(tmp_path, {"lib.py": "def f():\n    return 1\n"})
        tip = _commit(work, {"lib.py": "def f():\n    return 2\n"})
        decision, reason = _push(work, _env(tmp_path))
        assert decision == "pass", reason
        assert _state(work, tip).get("state") == "done"
        manifest = _review_manifest(tmp_path)
        assert manifest["spotter_model"] == "deepseek-v4-flash"

    def test_manifest_carries_configured_spotter_model(self, tmp_path):
        work = _repo(tmp_path, {"lib.py": "def f():\n    return 1\n"})
        tip = _commit(work, {"lib.py": "def f():\n    return 2\n"})
        decision, reason = _push(work, _env(tmp_path, OCR_SPOTTER_MODEL="some-fast-model"))
        assert decision == "pass", reason
        assert _state(work, tip).get("state") == "done"
        manifest = _review_manifest(tmp_path)
        assert manifest["spotter_model"] == "some-fast-model"

    def test_golden_argv_path_carries_no_manifest_change(self, tmp_path):
        """OCR_PRECOMPUTED_DIFFS=0 with no extras keeps the byte-identical 0.7.0
        command line: the spotter var must not force a manifest by itself."""
        work = _repo(tmp_path, {"lib.py": "def f():\n    return 1\n"})
        _commit(work, {"lib.py": "def f():\n    return 2\n"})
        decision, reason = _push(work, _env(tmp_path, OCR_PRECOMPUTED_DIFFS="0",
                                            OCR_SPOTTER_MODEL="some-fast-model"))
        assert decision == "pass", reason
        trace = tmp_path / "stub.trace"
        calls = [json.loads(x) for x in trace.read_text().splitlines() if x.strip()]
        assert calls and all(c.get("paths_file") is None for c in calls)
