"""Kimchi model pins (ticket 02): one source of truth, no literals in prompts.

Two seams. The gate is driven end to end behind the fake backend CLI
(tests/fake_backend.py): what the fake records in FAKE_TRACE is the argv a real
kimchi would have received -- the session model, its thinking suffix and the
canary's model. The review manifest the stub saw (STUB_TRACE) carries the
persona pins the skill spawns subagents with. A source scan guards the skill
and agent prompts: they carry no model id literal, so a pin can only change in
the settings layer.
"""
import glob
import json
import os
import re
import sys
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
import rg_settings  # noqa: E402
from test_async_gate import _git, repo  # noqa: E402,F401
from test_claude_profile import _backend_env, _canaries, _launches, _push  # noqa: E402

FAST_MODEL = "kimchi-dev/kimi-k3.1-pro"
DEPRECATED = "kimchi-dev/kimi-k3"

# A known model id (or the kimchi provider prefix) in a prompt means a pin lives
# outside the settings layer. Word-bounded so `kimi-k3.1-pro` is also caught.
_MODEL_LITERAL = re.compile(
    r"kimi-k3|deepseek-v4|glm-5|minimax|nemotron|gemma|kimchi-dev/", re.IGNORECASE)


def _model_arg(launch):
    argv = launch["argv"]
    return argv[argv.index("--model") + 1]


def _commit_files(work, files):
    for name, content in files.items():
        p = work / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "risky change"], cwd=work)


def _review_manifest(tmp_path):
    trace = tmp_path / "stub.trace"
    calls = [json.loads(x) for x in trace.read_text().splitlines() if x.strip()]
    return next((c.get("manifest") or {}) for c in calls if c.get("manifest"))


# --- prompts carry no model id literal -------------------------------------------

def _prompt_files():
    files = [os.path.join(_ROOT, "skills", "review", "SKILL.md")]
    files += sorted(glob.glob(os.path.join(_ROOT, "agents", "*.md")))
    return files


def test_there_are_prompt_files_to_scan():
    names = {os.path.basename(f) for f in _prompt_files()}
    assert {"SKILL.md", "code-reviewer.md", "deep-reviewer.md"} <= names


@pytest.mark.parametrize("path", _prompt_files(), ids=os.path.basename)
def test_no_model_id_literal_in_the_skill_or_agent_prompts(path):
    hits = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        m = _MODEL_LITERAL.search(line)
        if m:
            hits.append("%s:%d: %r in %r" % (os.path.basename(path), n, m.group(0), line.strip()))
    assert not hits, "model id literals belong in the manifest/settings layer:\n" + "\n".join(hits)


# --- defaults ------------------------------------------------------------------------

def test_no_kimchi_default_is_the_deprecated_kimi_k3():
    for (agent, backend), model in rg_settings.DEFAULT_MODELS.items():
        if backend != "kimchi":
            continue
        bare = model.split("/", 1)[-1]
        assert bare != "kimi-k3", "%s still defaults to the deprecated kimi-k3" % agent
        assert not re.fullmatch(r"kimi-k3(:\w+)?", bare), agent


def test_the_default_spotter_is_an_id_kimchi_lists():
    assert rg_settings.default_model("spotter", "kimchi") == "deepseek-v4.1-flash-1017"
    assert rg_settings.default_model("code-filter", "kimchi").endswith("/deepseek-v4.1-flash-1017")


# --- the launch behind the fake backend ----------------------------------------------

def test_fast_lane_session_runs_the_new_model_at_high(repo, tmp_path):
    (repo / "feat.txt").write_text("x\n")
    _git(["add", "."], cwd=repo)
    _git(["commit", "-q", "-m", "feat"], cwd=repo)
    assert _push(repo, _backend_env(tmp_path, OCR_BACKEND="kimchi"))[0] == "pass"
    (launch,) = _launches(tmp_path)
    assert launch["flavor"] == "kimchi"
    assert _model_arg(launch) == FAST_MODEL + ":high"
    assert DEPRECATED + ":" not in " ".join(launch["argv"])


def test_the_canary_launches_the_canary_model_with_no_thinking_suffix(repo, tmp_path):
    (repo / "feat.txt").write_text("x\n")
    _git(["add", "."], cwd=repo)
    _git(["commit", "-q", "-m", "feat"], cwd=repo)
    assert _push(repo, _backend_env(tmp_path, OCR_BACKEND="kimchi"))[0] == "pass"
    canaries = _canaries(tmp_path)
    assert canaries, "the kimchi canary never ran"
    assert _model_arg(canaries[0]) == FAST_MODEL


def test_a_settings_file_thinking_override_changes_the_suffix(repo, tmp_path):
    user_file = Path(os.environ["OCR_SETTINGS_FILE"])
    user_file.parent.mkdir(parents=True, exist_ok=True)
    user_file.write_text(json.dumps(
        {"agents": {"code-reviewer": {"kimchi": {"thinking": "low"}}}}), encoding="utf-8")
    (repo / "feat.txt").write_text("x\n")
    _git(["add", "."], cwd=repo)
    _git(["commit", "-q", "-m", "feat"], cwd=repo)
    assert _push(repo, _backend_env(tmp_path, OCR_BACKEND="kimchi"))[0] == "pass"
    assert _model_arg(_launches(tmp_path)[0]) == FAST_MODEL + ":low"


def test_an_ocr_kimi_args_override_is_left_untouched(repo, tmp_path):
    (repo / "feat.txt").write_text("x\n")
    _git(["add", "."], cwd=repo)
    _git(["commit", "-q", "-m", "feat"], cwd=repo)
    env = _backend_env(tmp_path, OCR_BACKEND="kimchi",
                       OCR_KIMI_ARGS="--no-session --model custom-model")
    assert _push(repo, env)[0] == "pass"
    assert _model_arg(_launches(tmp_path)[0]) == "custom-model"


@pytest.mark.parametrize("transport", [None, "native"], ids=["legacy-transport", "native-transport"])
def test_the_deep_lane_runs_at_max(repo, tmp_path, transport):
    _commit_files(repo, {"db/migrations/001_users.py": "x = 1\n"})
    env = _backend_env(tmp_path, OCR_BACKEND="kimchi", OCR_DEEP_TRANSPORT=transport)
    assert _push(repo, env)[0] == "pass"
    (launch,) = _launches(tmp_path)          # one deep pass, no fast pass besides
    assert _model_arg(launch) == FAST_MODEL + ":max"
    if transport == "native":
        assert launch["argv"][:2] == ["--mode", "rpc"]
    else:
        assert "-p" in launch["argv"]


# --- the manifests carry the pins ------------------------------------------------------

def test_the_fast_manifest_carries_every_persona_pin(repo, tmp_path):
    _commit_files(repo, {"lib.py": "def f():\n    return 2\n"})   # reviewable: gets a manifest
    assert _push(repo, _backend_env(tmp_path, OCR_BACKEND="kimchi"))[0] == "pass"
    m = _review_manifest(tmp_path)
    assert m["spotter_model"] == "deepseek-v4.1-flash-1017"
    assert m["tier1_model"] == FAST_MODEL and m["tier1_thinking"] == "high"
    assert m["tier2_model"] == FAST_MODEL and m["tier2_thinking"] == "max"
    assert m["docs_checker_model"] == "kimchi-dev/glm-5.3"
    assert m["filter_model"] == "kimchi-dev/deepseek-v4.1-flash-1017"
    assert m["resolver_model"] == "kimchi-dev/glm-5.3"
    for key, value in m.items():
        if key.endswith("_model"):
            assert not re.fullmatch(r"(kimchi-dev/)?kimi-k3", str(value)), key


def test_the_deep_manifest_carries_the_persona_pins(repo, tmp_path):
    _commit_files(repo, {"db/migrations/001_users.py": "x = 1\n"})
    assert _push(repo, _backend_env(tmp_path, OCR_BACKEND="kimchi"))[0] == "pass"
    m = _review_manifest(tmp_path)
    assert m["deep_review"] is True
    assert m["spotter_model"] == "deepseek-v4.1-flash-1017"
    assert m["tier2_model"] == FAST_MODEL and m["tier2_thinking"] == "max"
    assert m["filter_model"] == "kimchi-dev/deepseek-v4.1-flash-1017"
