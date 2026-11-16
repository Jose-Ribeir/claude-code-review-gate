"""The claude launch path, end to end, next to the kimchi one.

Seam (docs/specs/unified-review-gate.md, Testing Decisions): the push gate
entry point. Each test builds a fixture repo, runs the gate exactly as a
harness hook does (`review-gate.py --mode hook`, a real detached supervisor),
and puts a FAKE backend executable named `claude` and/or `kimchi` on PATH
(tests/fake_backend.py) that returns scripted reviewer output. Assertions are
about observable behaviour: allow / block / warn, the verdict record, which
backend binary was launched and how, and what the denial tells the agent.
The same scenarios run against both profiles wherever the behaviour is shared.

A few in-process tests pin the launch description itself (CLI discovery
order, argv, env scrub, model pins) where the end-to-end trace cannot see it.
"""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, _HERE)
sys.path.insert(0, _SCRIPTS)
import fake_backend  # noqa: E402
from test_async_gate import (_commit, _env as _async_env, _hook, _verdict_record,  # noqa: E402,F401
                             _wait_state, repo)

_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_spec = importlib.util.spec_from_file_location("review_gate_claude_profile", _GATE)
rg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rg)

PROFILES = ("claude", "kimchi")
_CLI_NAMES = ("claude", "kimchi")

# Env that selects or configures a backend. Scrubbed from every scenario so
# the developer's own harness (this suite runs inside Claude Code) never leaks
# into a result; a scenario then sets exactly the signals it is about.
_BACKEND_VARS = ("OCR_BACKEND", "OCR_HARNESS", "OCR_REVIEWER_CMD", "OCR_STUB_WORKER",
                 "OCR_CLAUDE_BIN", "OCR_CLAUDE_ARGS", "OCR_CLAUDE_EXTRA_ARGS",
                 "OCR_KIMI_ARGS", "OCR_KIMI_EXTRA_ARGS", "OCR_SKIP_CANARY",
                 "OCR_MODEL", "OCR_CLAUDE_MODEL", "OCR_UNSET_ENV",
                 "CLAUDECODE", "CLAUDE_CODE_EXECPATH", "KIMCHI_BIN")


def _path_without_backends(path):
    """PATH entries that hold no claude/kimchi launcher: the machine running the
    suite may well have the real ones installed."""
    keep = []
    for d in (path or "").split(os.pathsep):
        if not d:
            continue
        names = {n + ext for n in _CLI_NAMES for ext in ("", ".exe", ".cmd", ".bat")}
        try:
            present = names & set(os.listdir(d))
        except OSError:
            present = set()
        if not present:
            keep.append(d)
    return keep


def _backend_env(tmp_path, clis=_CLI_NAMES, **extra):
    """Hook env with the fake backends in `clis` as the ONLY claude/kimchi on
    PATH, no stub worker, no inherited harness signals, and no bundled-claude
    fallback location. `extra` are set last (None removes)."""
    env = _async_env(tmp_path)
    env.pop("OCR_STUB_WORKER", None)
    for k in list(env):
        if (k in _BACKEND_VARS or k.startswith(("CLAUDE_CODE_", "KIMCHI_", "PI_", "FAKE_"))):
            env.pop(k)
    shim = tmp_path / "shim"
    for name in clis:
        fake_backend.install(shim, name)
    env["PATH"] = os.pathsep.join([str(shim)] + _path_without_backends(env.get("PATH", "")))
    home = tmp_path / "nohome"
    home.mkdir(exist_ok=True)
    for k in ("LOCALAPPDATA", "USERPROFILE", "HOME"):
        env[k] = str(home)
    env["OCR_INLINE_BUDGET"] = "90"
    env["FAKE_TRACE"] = str(tmp_path / "fake.trace")
    for k, v in extra.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = str(v)
    return env


def _launches(tmp_path):
    """Real review launches the fakes saw (the kimchi canary probe excluded)."""
    return [t for t in fake_backend.read_trace(tmp_path / "fake.trace")
            if not t["prompt"].startswith("Sandbox self-check")]


def _canaries(tmp_path):
    return [t for t in fake_backend.read_trace(tmp_path / "fake.trace")
            if t["prompt"].startswith("Sandbox self-check")]


def _push(repo_, env):
    """(decision, reason) of a push through the gate. The hook only waits a few
    seconds for a review (OCR_INLINE_BUDGET is clamped to 4 s), so a slower one
    answers "still running": wait for the run to end, then collect its verdict
    the way the agent would, by re-running the same push. A run that FAILED is
    reported from its state instead of re-pushing (a re-push would start
    another attempt)."""
    decision, reason = _hook(repo_, "git push origin main", env)[:2]
    if "is still running" not in reason:
        return decision, reason
    tip = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_), capture_output=True,
                         text=True, check=True).stdout.strip()
    st = _wait_state(repo_, tip, {"done", "failed"}, timeout=120)
    if st["state"] == "failed":
        return "deny", str(st.get("detail") or st.get("reason"))
    return _hook(repo_, "git push origin main", env)[:2]


# --- the happy path, both profiles ------------------------------------------

@pytest.mark.parametrize("profile", PROFILES)
def test_the_selected_backend_reviews_the_push_and_only_it(repo, tmp_path, profile):
    tip = _commit(repo)
    env = _backend_env(tmp_path, OCR_BACKEND=profile)
    decision, reason = _push(repo, env)
    assert decision == "pass", reason
    (launch,) = _launches(tmp_path)
    assert launch["flavor"] == profile
    assert _wait_state(repo, tip, {"done"})["verdict"] == "pass"
    assert _verdict_record(repo, tip)["verdict"] == "pass"
    # What was reviewed rode in the skill prompt, namespaced for the harness.
    skill = "/review-gate:review" if profile == "claude" else "/skill:review"
    assert launch["prompt"].startswith(f"{skill} --range ")
    assert launch["prompt"].endswith(tip + " --json")


def test_claude_is_launched_with_its_own_sandbox_flags_and_no_kimchi_wiring(repo, tmp_path):
    _commit(repo)
    env = _backend_env(tmp_path, OCR_BACKEND="claude", CLAUDE_CODE_SESSION_ID="s-123",
                       CLAUDE_CODE_ENTRYPOINT="cli")
    assert _push(repo, env)[0] == "pass"
    (launch,) = _launches(tmp_path)
    argv = launch["argv"]
    allowed = argv[argv.index("--allowedTools") + 1: argv.index("--disallowedTools")]
    # Read-only: git queries, the read tools, and EXACTLY the one helper
    # script (the conventions resolver) by literal command prefix -- nothing
    # else may be pre-approved.
    helper_rules = {f"Bash({c} *)" for c in rg._helper_cmds().values()}
    assert len(helper_rules) == 1
    assert "Read" in allowed and all(
        a.startswith("Bash(git ") or a in ("Read", "Grep", "Glob", "Task") or a in helper_rules
        for a in allowed)
    assert helper_rules <= set(allowed)
    denied = argv[argv.index("--disallowedTools") + 1: argv.index("--model")]
    assert {"Write", "Edit", "WebFetch"} <= set(denied)
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv and "--verbose" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "-e" not in argv and "--mode" not in argv          # kimchi-only wiring
    # The worker env: marked in-review, no inherited claude CLAUDE.md, and the
    # desktop host's session identity is not handed down.
    assert launch["env"].get("OCR_IN_REVIEW") == "1"
    assert launch["env"].get("CLAUDE_CODE_DISABLE_CLAUDE_MDS") == "1"
    assert "CLAUDE_CODE_SESSION_ID" not in launch["env"]
    assert "CLAUDE_CODE_ENTRYPOINT" not in launch["env"]


@pytest.mark.parametrize("profile", PROFILES)
def test_the_deny_extension_canary_runs_for_kimchi_only(repo, tmp_path, profile):
    # Claude enforces its sandbox from --allowedTools/--disallowedTools, so
    # there is no extension to prove loaded; kimchi's -p sessions run yolo and
    # the canary is the only detector, so it must still fire (once).
    _commit(repo)
    assert _push(repo, _backend_env(tmp_path, OCR_BACKEND=profile))[0] == "pass"
    if profile == "kimchi":
        assert len(_canaries(tmp_path)) == 1
        assert "-e" in _launches(tmp_path)[0]["argv"]
    else:
        assert _canaries(tmp_path) == []


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("verdict,decision,needle", [
    ("block", "deny", "stub high finding"),     # a high finding blocks
    ("warn", "pass", None),                      # a medium one is advisory only
])
def test_findings_block_or_warn_through_either_backend(repo, tmp_path, profile, verdict,
                                                       decision, needle):
    tip = _commit(repo)
    got, reason = _push(repo, _backend_env(tmp_path, OCR_BACKEND=profile, STUB_VERDICT=verdict))
    assert got == decision, reason
    if needle:
        assert needle in reason
    assert _verdict_record(repo, tip)["verdict"] == verdict


def test_claude_single_json_result_envelope_is_understood(repo, tmp_path):
    # `claude -p --output-format json` prints ONE result object, not a stream.
    tip = _commit(repo)
    env = _backend_env(tmp_path, OCR_BACKEND="claude", FAKE_ENVELOPE="json",
                       STUB_VERDICT="block")
    decision, reason = _push(repo, env)
    assert decision == "deny" and "stub high finding" in reason
    assert _verdict_record(repo, tip)["verdict"] == "block"


# --- fail closed, both profiles ---------------------------------------------

@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("scenario,extra", [
    ("garbage", {}),
    ("rawtext", {}),
    ("crash", {}),
    ("auth", {"FAKE_AUTH_RC": "1"}),
    ("auth", {"FAKE_AUTH_RC": "0"}),
], ids=["garbage-stream", "raw-text", "crash-rc3", "auth-rc1", "auth-rc0"])
def test_unusable_backend_output_blocks_and_records_no_verdict(repo, tmp_path, profile,
                                                               scenario, extra):
    tip = _commit(repo)
    env = _backend_env(tmp_path, OCR_BACKEND=profile, FAKE_SCENARIO=scenario, **extra)
    decision, _reason = _push(repo, env)
    assert decision == "deny"
    st = _wait_state(repo, tip, {"failed"})
    assert st["state"] == "failed"
    assert _verdict_record(repo, tip) is None


@pytest.mark.parametrize("profile", PROFILES)
def test_expired_credentials_say_so_and_never_read_as_a_review_problem(repo, tmp_path, profile):
    tip = _commit(repo)
    env = _backend_env(tmp_path, OCR_BACKEND=profile, FAKE_SCENARIO="auth")
    assert _push(repo, env)[0] == "deny"
    detail = str(_wait_state(repo, tip, {"failed"}).get("detail"))
    assert "CREDENTIALS failure" in detail and "/login" in detail
    # The closing note names the harness whose session was refreshed.
    assert ("Claude Code Desktop" in detail) == (profile == "claude")
    assert ("Kimchi Studio" in detail) == (profile == "kimchi")


def test_a_usage_limit_blocks_with_the_limit_message(repo, tmp_path):
    tip = _commit(repo)
    env = _backend_env(tmp_path, OCR_BACKEND="claude", STUB_VERDICT="limit")
    assert _push(repo, env)[0] == "deny"
    assert "limit" in str(_wait_state(repo, tip, {"failed", "done"})).lower()
    assert _verdict_record(repo, tip) is None


@pytest.mark.parametrize("profile", PROFILES)
def test_a_missing_backend_cli_blocks_naming_that_backend(repo, tmp_path, profile):
    tip = _commit(repo)
    other = "kimchi" if profile == "claude" else "claude"
    env = _backend_env(tmp_path, clis=(other,), OCR_BACKEND=profile)
    decision, _ = _push(repo, env)
    assert decision == "deny"
    st = _wait_state(repo, tip, {"failed"})
    detail = str(st.get("detail"))
    assert f"`{profile}` CLI not found" in detail
    assert f"`{other}`" not in detail                 # never blames (or runs) the other one
    assert _launches(tmp_path) == []                  # and never substituted it
    assert _verdict_record(repo, tip) is None


def test_the_claude_missing_message_says_where_the_cli_is_looked_for(repo, tmp_path):
    tip = _commit(repo)
    _push(repo, _backend_env(tmp_path, clis=(), OCR_BACKEND="claude"))
    detail = str(_wait_state(repo, tip, {"failed"}).get("detail"))
    assert "OCR_CLAUDE_BIN" in detail and "CLAUDE_CODE_EXECPATH" in detail
    assert "KIMCHI_BIN" not in detail


def test_with_no_signal_and_no_cli_the_default_backend_is_kimchi_and_blocks(repo, tmp_path):
    tip = _commit(repo)
    _push(repo, _backend_env(tmp_path, clis=()))
    detail = str(_wait_state(repo, tip, {"failed"}).get("detail"))
    assert "`kimchi` CLI not found" in detail and "KIMCHI_BIN" in detail


# --- selection, observed through which fake got launched --------------------

def _which_backend(repo_, tmp_path, **env_kw):
    env = _backend_env(tmp_path, **env_kw)
    assert _push(repo_, env)[0] == "pass"
    (launch,) = _launches(tmp_path)
    return launch["flavor"]


def test_a_push_from_inside_claude_code_selects_claude_when_its_cli_is_present(repo, tmp_path):
    _commit(repo)
    assert _which_backend(repo, tmp_path, CLAUDECODE="1") == "claude"


def test_the_env_override_beats_the_claude_code_environment(repo, tmp_path):
    _commit(repo)
    assert _which_backend(repo, tmp_path, CLAUDECODE="1", OCR_BACKEND="kimchi") == "kimchi"


@pytest.mark.parametrize("hint,sniffed", [("claude", {}), ("kimchi", {"CLAUDECODE": "1"})])
def test_the_harness_hint_set_by_a_hook_adapter_picks_the_backend(repo, tmp_path, hint, sniffed):
    _commit(repo)
    assert _which_backend(repo, tmp_path, OCR_HARNESS=hint, **sniffed) == hint


def test_both_harness_signals_at_once_fall_to_the_default_not_a_guess(repo, tmp_path):
    _commit(repo)
    assert _which_backend(repo, tmp_path, CLAUDECODE="1", PI_CODING_AGENT_DIR="x") == "kimchi"


def test_a_lone_claude_cli_is_used_when_nothing_else_says_otherwise(repo, tmp_path):
    _commit(repo)
    assert _which_backend(repo, tmp_path, clis=("claude",)) == "claude"


def test_a_repo_pin_selects_the_backend(repo, tmp_path):
    cfg = repo / ".ocr"
    cfg.mkdir()
    (cfg / "config.json").write_text(json.dumps({"backend": "claude"}), encoding="utf-8")
    _commit(repo)
    assert _which_backend(repo, tmp_path) == "claude"


def test_an_unknown_explicit_backend_blocks_and_runs_nothing(repo, tmp_path):
    tip = _commit(repo)
    _push(repo, _backend_env(tmp_path, OCR_BACKEND="gemini"))
    detail = str(_wait_state(repo, tip, {"failed"}).get("detail"))
    assert "backend selection failed" in detail and "gemini" in detail
    assert _launches(tmp_path) == []


# --- the launch description, in process -------------------------------------

@pytest.fixture
def clean(monkeypatch):
    for k in _BACKEND_VARS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setitem(rg._canary_state, "probed", False)
    return monkeypatch


def test_find_claude_order_is_path_then_env_overrides_then_nothing(clean, tmp_path):
    exe_a, exe_b = tmp_path / "a-claude", tmp_path / "b-claude"
    exe_a.write_text("x")
    exe_b.write_text("x")
    clean.setattr(rg.shutil, "which", lambda name: "/on/path/claude")
    clean.setenv("OCR_CLAUDE_BIN", str(exe_a))
    assert rg._find_claude() == "/on/path/claude"            # PATH wins
    clean.setattr(rg.shutil, "which", lambda name: None)
    clean.setenv("CLAUDE_CODE_EXECPATH", str(exe_b))
    assert rg._find_claude() == str(exe_a)                   # OCR_CLAUDE_BIN before the host's own
    clean.delenv("OCR_CLAUDE_BIN")
    assert rg._find_claude() == str(exe_b)
    clean.delenv("CLAUDE_CODE_EXECPATH")
    clean.setenv("LOCALAPPDATA", str(tmp_path / "none"))
    clean.setenv("USERPROFILE", str(tmp_path / "none"))
    clean.setenv("HOME", str(tmp_path / "none"))
    assert rg._find_claude() is None


def test_find_claude_falls_back_to_the_newest_desktop_bundled_exe(clean, tmp_path):
    root = tmp_path / "Packages" / "Claude_abc" / "LocalCache" / "Roaming" / "Claude" / "claude-code"
    old, new = root / "1.0.0" / "claude.exe", root / "2.0.0" / "claude.exe"
    for p, mtime in ((old, 1000), (new, 2000)):
        p.parent.mkdir(parents=True)
        p.write_text("x")
        os.utime(p, (mtime, mtime))
    clean.setattr(rg.shutil, "which", lambda name: None)
    clean.setenv("LOCALAPPDATA", str(tmp_path))
    clean.setenv("USERPROFILE", str(tmp_path / "none"))
    assert rg._find_claude() == str(new)


def test_the_claude_launch_is_selected_by_name_and_skips_the_canary(clean, monkeypatch):
    monkeypatch.setenv("OCR_BACKEND", "claude")
    monkeypatch.setattr(rg, "_find_claude", lambda: "/fake/claude")
    monkeypatch.setattr(rg, "_canary_probe",
                        lambda *a: pytest.fail("the claude launch must not run the deny-extension canary"))
    inv = rg._worker_invocation("review", "missing")
    assert inv.launch.name == "claude" and inv[0] == "/fake/claude"
    assert rg._canary_state["probed"] is False


def test_claude_extra_args_append_and_the_args_override_replaces_wholesale(clean, monkeypatch):
    monkeypatch.setenv("OCR_BACKEND", "claude")
    monkeypatch.setattr(rg, "_find_claude", lambda: "/fake/claude")
    monkeypatch.setenv("OCR_CLAUDE_EXTRA_ARGS", "--max-turns 7")
    args = rg._worker_invocation("review", "missing")[2]
    # The base argv, plus the two helper-script rules, then the caller's extras.
    assert set(rg.CLAUDE_WORKER_ARGS) <= set(args) and args[-2:] == ["--max-turns", "7"]
    assert args[:-2] == rg._claude_review_args()[:-2]
    monkeypatch.setenv("OCR_CLAUDE_ARGS", "--print --bare")
    assert rg._worker_invocation("review", "missing")[2] == ["--print", "--bare"]
    # ...and the kimchi variables mean nothing to the claude launch.
    monkeypatch.delenv("OCR_CLAUDE_ARGS")
    monkeypatch.delenv("OCR_CLAUDE_EXTRA_ARGS")
    monkeypatch.setenv("OCR_KIMI_ARGS", "--mode json")
    assert rg._worker_invocation("review", "missing")[2] == rg._claude_review_args()


def test_claude_model_pins_per_role(clean, monkeypatch):
    # Review role: OCR_CLAUDE_MODEL, then an unprefixed OCR_MODEL (main's
    # variable), then sonnet. A vendor-prefixed OCR_MODEL is kimchi's
    # convention and must not reach `claude --model`.
    assert rg._read_claude_model() == "sonnet"
    monkeypatch.setenv("OCR_MODEL", "haiku")
    assert rg._read_claude_model() == "haiku"
    monkeypatch.setenv("OCR_MODEL", "kimchi-dev/kimi-k3")
    assert rg._read_claude_model() == "sonnet"
    monkeypatch.setenv("OCR_CLAUDE_MODEL", "opus")
    assert rg._read_claude_model() == "opus"
    # Docs-summary role: its own pin, else the review pin.
    assert rg._read_claude_model("OCR_CLAUDE_DOCS_SUMMARY_MODEL", "OCR_DOCS_SUMMARY_MODEL") == "opus"
    monkeypatch.setenv("OCR_CLAUDE_DOCS_SUMMARY_MODEL", "haiku")
    assert rg._read_claude_model("OCR_CLAUDE_DOCS_SUMMARY_MODEL", "OCR_DOCS_SUMMARY_MODEL") == "haiku"


def test_the_claude_env_scrub_drops_the_session_bridge_and_marks_the_worker(clean, monkeypatch):
    env = {n: "v" for n in rg._CLAUDE_SESSION_BRIDGE_ENV}
    env.update({"CLAUDECODE": "1", "CLAUDE_CODE_EXECPATH": "/x", "GIT_TRACE": "/gone"})
    removed = rg._CLAUDE_LAUNCH.scrub_env(env)
    assert set(rg._CLAUDE_SESSION_BRIDGE_ENV) <= set(removed) and "GIT_TRACE" in removed
    # CLAUDECODE ("this is a Claude Code session") goes with the bridge; the
    # exec path the gate itself reads stays.
    assert "CLAUDECODE" in rg._CLAUDE_SESSION_BRIDGE_ENV
    assert env == {"CLAUDE_CODE_EXECPATH": "/x", "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1"}
    # The same OCR_UNSET_ENV policy knob as the kimchi scrub.
    env = {n: "v" for n in rg._CLAUDE_SESSION_BRIDGE_ENV}
    monkeypatch.setenv("OCR_UNSET_ENV", "none")
    rg._CLAUDE_LAUNCH.scrub_env(env)
    assert set(rg._CLAUDE_SESSION_BRIDGE_ENV) <= set(env)


def test_kimchi_launch_is_unchanged_by_the_claude_profile(clean, monkeypatch):
    monkeypatch.setenv("OCR_BACKEND", "kimchi")
    monkeypatch.setattr(rg, "_find_kimi", lambda: "/fake/kimchi")
    monkeypatch.setitem(rg._canary_state, "probed", True)
    inv = rg._worker_invocation("review", "missing")
    assert inv.launch.name == "kimchi"
    assert inv[2] == rg.DEFAULT_KIMI_ARGS
    assert inv.launch.prompt == rg.PROMPT and inv.launch.prompt_range == rg.PROMPT_RANGE


def test_the_docs_summary_worker_runs_on_the_selected_backend(clean, monkeypatch, tmp_path):
    shim = tmp_path / "shim"
    fake_backend.install(shim, "claude")
    monkeypatch.setenv("PATH", os.pathsep.join([str(shim)] + _path_without_backends(os.environ["PATH"])))
    monkeypatch.setenv("OCR_BACKEND", "claude")
    monkeypatch.setenv("FAKE_TRACE", str(tmp_path / "fake.trace"))
    monkeypatch.setenv("STUB_DOCS_SUMMARY", "- keep the gate fail-closed (source: docs/a.md)")
    body = rg._run_docs_summary_once(str(tmp_path), "t" * 40, ["docs/a.md"], repo_root=str(tmp_path))
    assert body == "- keep the gate fail-closed (source: docs/a.md)"
    (launch,) = fake_backend.read_trace(tmp_path / "fake.trace")
    assert launch["flavor"] == "claude" and launch["prompt"].startswith("Summarize this repository")



# --- review findings on 936d1ee ----------------------------------------------

def test_hook_mode_hints_name_the_selected_host_without_a_worker_having_run(clean, tmp_path):
    # `--mode hook` never calls _worker_invocation, so the thread has no recorded
    # selection; the hints must still say which host the user has to relaunch.
    clean.chdir(tmp_path)
    clean.delattr(rg._backend_local, "selection", raising=False)
    clean.setenv("OCR_BACKEND", "claude")
    for hint in (rg._bypass_hint("hook"), rg._downgrade_hint("hook")):
        assert "Claude Code" in hint and "Kimchi" not in hint
    clean.setenv("OCR_BACKEND", "kimchi")
    for hint in (rg._bypass_hint("hook"), rg._downgrade_hint("hook")):
        assert "Kimchi" in hint and "Claude Code" not in hint
    # Resolving a hint must not pre-empt the worker's own authoritative choice.
    assert getattr(rg._backend_local, "selection", None) is None


def test_a_hint_never_raises_when_the_backend_cannot_be_selected(clean, tmp_path):
    clean.chdir(tmp_path)
    clean.delattr(rg._backend_local, "selection", raising=False)
    clean.setenv("OCR_BACKEND", "gemini")
    assert "Kimchi" in rg._bypass_hint("hook")    # falls back; the worker call reports the error


def test_find_claude_ignores_whitespace_around_an_env_override_like_the_doctor(clean, tmp_path):
    exe = tmp_path / "claude-bin"
    exe.write_text("x")
    clean.setattr(rg.shutil, "which", lambda name: None)
    clean.setenv("OCR_CLAUDE_BIN", f"  {exe}\n")
    assert rg._find_claude() == str(exe)


def test_hook_mode_hints_find_the_repo_pin_from_a_subdirectory(clean, tmp_path):
    # The pin is read from <repo top>/.ocr/config.json with no upward search, and
    # a hook-mode cwd can be any directory inside the repo.
    repo = tmp_path / "repo"
    sub = repo / "pkg" / "deep"
    sub.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".ocr").mkdir()
    (repo / ".ocr" / "config.json").write_text(json.dumps({"backend": "claude"}))
    clean.chdir(sub)
    clean.delattr(rg._backend_local, "selection", raising=False)
    assert "Claude Code" in rg._bypass_hint("hook")


# --- post-merge hardening: helper rules, tier models, guard, env, telemetry -----

_TIER_VARS = ("OCR_SPOTTER_MODEL", "OCR_TIER1_MODEL", "OCR_TIER2_MODEL",
              "OCR_CLAUDE_SPOTTER_MODEL", "OCR_CLAUDE_TIER1_MODEL", "OCR_CLAUDE_TIER2_MODEL")


@pytest.fixture
def tiers(clean):
    for k in _TIER_VARS:
        clean.delenv(k, raising=False)
    clean.delattr(rg._backend_local, "selection", raising=False)
    return clean


def test_helper_rule_is_exactly_the_conventions_script_and_only_for_the_review_worker(clean):
    cmds = rg._helper_cmds()
    # The demands validator is deliberately absent: the Bash tool refuses the
    # JSON it would have to be given (checked against the real CLI).
    assert set(cmds) == {"conventions"}
    assert cmds["conventions"].endswith("/scripts/ocr_conventions.py")
    assert "\\" not in cmds["conventions"]       # an allowlist rule is a literal string
    review = rg._claude_review_args()
    allowed = review[review.index("--allowedTools") + 1: review.index("--disallowedTools")]
    assert {f"Bash({c} *)" for c in cmds.values()} <= set(allowed)
    docs = rg._claude_docs_summary_args()
    docs_allowed = docs[docs.index("--allowedTools") + 1: docs.index("--disallowedTools")]
    assert not any("ocr_" in a for a in docs_allowed)    # the docs worker never runs them


def test_helper_cmds_are_given_to_the_session_only_under_claude(tiers):
    tiers.setenv("OCR_BACKEND", "claude")
    assert set(rg._helper_cmds_field()["helper_cmds"]) == {"conventions"}
    tiers.setenv("OCR_BACKEND", "kimchi")
    assert rg._helper_cmds_field() == {}


def test_tier_models_are_per_profile(tiers):
    tiers.setenv("OCR_BACKEND", "kimchi")
    assert [rg._tier_model(r) for r in ("spotter", "tier1", "tier2")] == [
        "deepseek-v4-flash", "kimi-k3", "kimi-k3"]
    tiers.setenv("OCR_BACKEND", "claude")
    assert [rg._tier_model(r) for r in ("spotter", "tier1", "tier2")] == ["haiku", "sonnet", "opus"]


def test_claude_tier_models_ignore_kimchi_ids_and_honour_claude_ones(tiers):
    tiers.setenv("OCR_BACKEND", "claude")
    tiers.setenv("OCR_TIER1_MODEL", "kimi-k3")               # not runnable by claude: ignored
    assert rg._tier_model("tier1") == "sonnet"
    tiers.setenv("OCR_TIER2_MODEL", " opus ")                 # shared var, an Anthropic model
    assert rg._tier_model("tier2") == "opus"
    tiers.setenv("OCR_CLAUDE_TIER1_MODEL", "claude-sonnet-4-5")   # role-specific wins
    assert rg._tier_model("tier1") == "claude-sonnet-4-5"


def test_the_provider_guard_still_blocks_anthropic_under_kimchi(tiers):
    tiers.setenv("OCR_BACKEND", "kimchi")
    tiers.setenv("OCR_TIER1_MODEL", "claude-sonnet-4-5")
    with pytest.raises(rg.ReviewGateError):
        rg._check_provider_guard()


def test_the_provider_guard_stands_down_under_claude(tiers):
    tiers.setenv("OCR_BACKEND", "claude")
    tiers.setenv("OCR_TIER1_MODEL", "claude-sonnet-4-5")
    rg._check_provider_guard()                                # must not raise


def test_telemetry_records_carry_the_backend_only_when_known():
    tele = rg.ocr_telemetry
    assert tele.tier_record("tier1", model="m", backend="claude")["backend"] == "claude"
    assert "backend" not in tele.tier_record("tier1", model="m")


def test_claudecode_and_the_internal_handoffs_do_not_reach_the_reviewer(clean):
    env = {"CLAUDECODE": "1", "OCR_HARNESS": "claude", rg.ocr_profile.SNIFFED_VAR: "claude", "KEEP": "1"}
    rg._scrub_claude_worker_env(env)
    assert "CLAUDECODE" not in env and env["KEEP"] == "1"
    clean.setenv("CLAUDECODE", "1")
    clean.setenv("OCR_HARNESS", "claude")
    sup = rg._supervisor_env()
    assert "CLAUDECODE" not in sup                            # scrubbed ...
    assert sup["OCR_HARNESS"] == "claude"                     # ... the hint survives for selection
    assert sup[rg.ocr_profile.SNIFFED_VAR] == "claude"        # and so does what was sniffed first


def test_jira_side_fetch_rides_the_selected_backend(clean, tmp_path):
    import rg_jira_fetch
    conf = tmp_path / "mcp.json"
    conf.write_text(json.dumps({"mcpServers": {
        "atlassian-remote": {"command": "npx"},
        "__kimchi-studio-connector__atlassian": {"command": "npx"}}}))
    clean.setenv("OCR_JIRA_MCP_CONFIG", str(conf))
    seen = []

    def runner(argv, cwd, timeout):
        seen.append(argv)
        return "TITLE: t\nDESCRIPTION:\nbody", None

    clean.setenv("OCR_BACKEND", "claude")
    payload, note = rg_jira_fetch.fetch_issue("PROJ-1", {}, str(tmp_path), runner)
    assert payload == {"title": "t", "body": "body"}, note
    argv = seen[-1]
    assert os.path.basename(argv[0]).lower().startswith("claude")
    assert "--strict-mcp-config" in argv and argv[argv.index("--allowedTools") + 1] == "mcp__atlassian-remote"
    clean.setenv("OCR_BACKEND", "kimchi")
    payload, note = rg_jira_fetch.fetch_issue("PROJ-1", {}, str(tmp_path), runner)
    assert payload == {"title": "t", "body": "body"}, note
    assert os.path.basename(seen[-1][0]).lower().startswith("kimchi") and "--no-session" in seen[-1]
