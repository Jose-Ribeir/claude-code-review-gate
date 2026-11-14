"""Backend selection precedence (unified gate: claude / kimchi profiles).

Pins the user-visible rules from docs/specs/unified-review-gate.md:
  OCR_BACKEND  >  repo .ocr/config.json "backend"  >  harness hint
  >  harness environment  >  the single CLI on PATH  >  default.
An explicit choice that cannot be honoured FAILS CLOSED (BackendError); it is
never swapped for another backend. Auto-detection only returns a backend that
can launch a review in this build, so an unfinished profile never changes how
a push is reviewed.

Behaviour only: the tests assert on the Selection / the raised error, not on
module internals.
"""
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts"))
import ocr_profile  # noqa: E402
from ocr_profile import BackendError, select_backend  # noqa: E402


def _no_cli(_name):
    return None


def _only(*present):
    return lambda name: f"/bin/{name}" if name in present else None


def _unready(monkeypatch, name):
    """Make profile `name` look half-built (launch_ready=False): the generic
    guard that keeps a profile without a launch path from reviewing a push.
    Every shipped profile is ready, so the guard is exercised on a patched
    registry."""
    patched = dict(ocr_profile.PROFILES)
    patched[name] = patched[name]._replace(launch_ready=False)
    monkeypatch.setattr(ocr_profile, "PROFILES", patched)


def _pin(tmp_path, value):
    cfg = tmp_path / ".ocr"
    cfg.mkdir()
    (cfg / "config.json").write_text(json.dumps({"backend": value}), encoding="utf-8")
    return tmp_path


def test_default_when_nothing_signals_a_backend():
    sel = select_backend(env={}, which=_no_cli)
    assert (sel.name, sel.reason) == ("kimchi", "default")


def test_env_override_wins_over_everything(tmp_path):
    repo = _pin(tmp_path, "kimchi")
    env = {"OCR_BACKEND": "kimchi", "OCR_HARNESS": "kimchi", "KIMCHI_BIN": "x"}
    sel = select_backend(env=env, repo_root=repo, which=_only("kimchi"))
    assert sel.name == "kimchi" and "OCR_BACKEND" in sel.reason


def test_env_override_is_case_and_space_insensitive():
    assert select_backend(env={"OCR_BACKEND": "  KimChi "}, which=_no_cli).name == "kimchi"


def test_repo_pin_used_when_no_env_override(tmp_path):
    sel = select_backend(env={}, repo_root=_pin(tmp_path, "kimchi"), which=_no_cli)
    assert (sel.name, sel.reason) == ("kimchi", "repo config pin")


def test_env_override_beats_repo_pin(tmp_path):
    repo = _pin(tmp_path, "claude")
    sel = select_backend(env={"OCR_BACKEND": "kimchi"}, repo_root=repo, which=_no_cli)
    assert (sel.name, sel.reason) == ("kimchi", "OCR_BACKEND override")


def test_env_override_beats_a_repo_pin_that_cannot_launch(tmp_path, monkeypatch):
    # With the pinned backend unlaunchable, the env override must still win
    # without ever consulting (and failing on) the pin.
    _unready(monkeypatch, "claude")
    sel = select_backend(env={"OCR_BACKEND": "kimchi"},
                         repo_root=_pin(tmp_path, "claude"), which=_no_cli)
    assert sel.name == "kimchi"


@pytest.mark.parametrize("bad", ["gemini", "claud", "kimchi2"])
def test_unknown_explicit_backend_fails_closed(bad):
    with pytest.raises(BackendError) as exc:
        select_backend(env={"OCR_BACKEND": bad}, which=_only("kimchi"))
    assert bad in str(exc.value) and "kimchi" in str(exc.value)


def test_unknown_repo_pin_fails_closed(tmp_path):
    with pytest.raises(BackendError):
        select_backend(env={}, repo_root=_pin(tmp_path, "gemini"), which=_only("kimchi"))


@pytest.mark.parametrize("name", ["claude", "kimchi"])
def test_explicit_backend_that_cannot_launch_fails_closed_not_swapped(tmp_path, monkeypatch, name):
    # A profile without a launch path must block when chosen explicitly (env or
    # repo pin), never silently review with the other backend instead.
    other = "kimchi" if name == "claude" else "claude"
    _unready(monkeypatch, name)
    with pytest.raises(BackendError) as exc:
        select_backend(env={"OCR_BACKEND": name}, which=_only("kimchi", "claude"))
    assert "not implemented" in str(exc.value) and other in str(exc.value)
    with pytest.raises(BackendError):
        select_backend(env={}, repo_root=_pin(tmp_path, name), which=_only("claude", "kimchi"))


def test_both_shipped_profiles_can_launch():
    assert all(p.launch_ready for p in ocr_profile.PROFILES.values())
    assert select_backend(env={"OCR_BACKEND": "claude"}, which=_no_cli).name == "claude"


def test_explicit_claude_choice_launches_from_the_repo_pin_too(tmp_path):
    sel = select_backend(env={}, repo_root=_pin(tmp_path, "claude"), which=_only("kimchi"))
    assert (sel.name, sel.reason) == ("claude", "repo config pin")


def test_harness_hint_selects_a_launchable_backend():
    for hint in ("kimchi", "claude"):
        sel = select_backend(env={"OCR_HARNESS": hint}, which=_no_cli)
        assert (sel.name, sel.reason) == (hint, "harness hint (OCR_HARNESS)")


def test_harness_hint_for_an_unready_or_unknown_backend_is_skipped_with_a_note(monkeypatch):
    _unready(monkeypatch, "claude")
    for hint in ("claude", "gemini"):
        sel = select_backend(env={"OCR_HARNESS": hint}, which=_no_cli)
        assert sel.name == "kimchi"
        assert any("OCR_HARNESS" in n for n in sel.notes)


def test_kimchi_environment_is_autodetected():
    sel = select_backend(env={"KIMCHI_BIN": "C:/k/kimchi.exe"}, which=_no_cli)
    assert sel.name == "kimchi" and "auto-detected" in sel.reason


def test_a_push_from_inside_claude_code_selects_claude():
    # The case that matters: CLAUDECODE set (a push fired from a Claude Code
    # session), with or without kimchi installed alongside.
    env = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}
    for present in (("claude",), ("kimchi", "claude"), ()):
        sel = select_backend(env=env, which=_only(*present))
        assert sel.name == "claude" and "auto-detected" in sel.reason


def test_an_explicit_kimchi_choice_beats_the_claude_code_environment():
    sel = select_backend(env={"CLAUDECODE": "1", "OCR_BACKEND": "kimchi"},
                         which=_only("kimchi", "claude"))
    assert (sel.name, sel.reason) == ("kimchi", "OCR_BACKEND override")


def test_claude_environment_does_not_hijack_selection_while_claude_cannot_launch(monkeypatch):
    # The same push while claude has no launch path: keep reviewing with
    # kimchi, with a note saying why claude was skipped.
    _unready(monkeypatch, "claude")
    env = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}
    sel = select_backend(env=env, which=_only("kimchi", "claude"))
    assert sel.name == "kimchi"
    assert any("claude" in n and "cannot launch" in n for n in sel.notes)


def test_single_launchable_cli_on_path_is_used():
    sel = select_backend(env={}, which=_only("kimchi"))
    assert (sel.name, sel.reason) == ("kimchi", "only the kimchi CLI is on PATH")


def test_the_claude_profile_is_autodetected():
    assert select_backend(env={"CLAUDECODE": "1"}, which=_no_cli).name == "claude"
    assert select_backend(env={"OCR_HARNESS": "claude"}, which=_no_cli).name == "claude"
    assert select_backend(env={}, which=_only("claude")).name == "claude"
    # Both harness signals at once is ambiguous: fall through, do not guess.
    both = select_backend(env={"CLAUDECODE": "1", "KIMCHI_BIN": "x"}, which=_no_cli)
    assert both.name == "kimchi" and any("more than one" in n for n in both.notes)
    # Both CLIs on PATH and no other signal: default, not a coin flip.
    assert select_backend(env={}, which=_only("claude", "kimchi")).reason == "default"
    # An explicit choice launches.
    assert select_backend(env={"OCR_BACKEND": "claude"}, which=_no_cli).name == "claude"


@pytest.mark.parametrize("body", ["{not json", "[]", '{"backend": ""}', '{"backend": 3}', "{}"])
def test_unusable_repo_config_is_a_note_not_a_pin(tmp_path, body):
    (tmp_path / ".ocr").mkdir()
    (tmp_path / ".ocr" / "config.json").write_text(body, encoding="utf-8")
    sel = select_backend(env={}, repo_root=tmp_path, which=_no_cli)
    assert sel.name == "kimchi" and sel.reason == "default"


def test_missing_repo_config_is_silent(tmp_path):
    sel = select_backend(env={}, repo_root=tmp_path, which=_no_cli)
    assert sel.notes == []


# --- wiring: the real gate function consults the selection -------------------

import importlib.util  # noqa: E402

_GATE = os.path.join(os.path.dirname(_HERE), "scripts", "review-gate.py")
_spec = importlib.util.spec_from_file_location("review_gate_backend_select", _GATE)
rg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rg)


def _launch(repo_root=None):
    return rg._worker_invocation("review", "worker missing", repo_root=repo_root)


def test_gate_blocks_on_an_unusable_explicit_backend(monkeypatch):
    for k in ("OCR_STUB_WORKER", "OCR_REVIEWER_CMD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OCR_BACKEND", "gemini")
    with pytest.raises(rg.ReviewGateError) as exc:
        _launch()
    assert "backend selection failed" in str(exc.value) and "gemini" in str(exc.value)


def test_gate_blocks_when_the_repo_pins_a_backend_that_cannot_launch(monkeypatch, tmp_path):
    for k in ("OCR_STUB_WORKER", "OCR_REVIEWER_CMD", "OCR_BACKEND"):
        monkeypatch.delenv(k, raising=False)
    _unready(monkeypatch, "claude")
    with pytest.raises(rg.ReviewGateError) as exc:
        _launch(repo_root=_pin(tmp_path, "claude"))
    assert "not implemented" in str(exc.value)


def test_gate_launches_the_backend_the_repo_pins(monkeypatch, tmp_path):
    for k in ("OCR_STUB_WORKER", "OCR_REVIEWER_CMD", "OCR_BACKEND"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(rg, "_find_claude", lambda: "/fake/claude")
    inv = _launch(repo_root=_pin(tmp_path, "claude"))
    assert inv.launch.name == "claude" and inv[0] == "/fake/claude"


def test_a_ready_profile_without_a_launch_description_blocks_instead_of_falling_back(
        monkeypatch):
    for k in ("OCR_STUB_WORKER", "OCR_REVIEWER_CMD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OCR_BACKEND", "claude")
    monkeypatch.setattr(rg, "_LAUNCHES", {"kimchi": rg._KIMCHI_LAUNCH})
    with pytest.raises(rg.ReviewGateError) as exc:
        _launch()
    assert "no launch description" in str(exc.value) and "claude" in str(exc.value)


def test_stub_workers_skip_backend_selection(monkeypatch):
    # The stub runs INSTEAD of any CLI, so even a nonsense OCR_BACKEND must
    # not stop it: the whole existing stub-driven suite relies on this.
    monkeypatch.setenv("OCR_STUB_WORKER", "1")
    monkeypatch.setenv("OCR_BACKEND", "gemini")
    kimi, stub, _args, _env, _scrubbed = _launch()
    assert stub


def test_docs_summary_worker_honours_the_repo_backend_pin(monkeypatch, tmp_path):
    # The docs-summary worker is the second _worker_invocation call site. It
    # used to be called without the repo root, so a repo's pin was silently
    # ignored there while the review worker blocked on it.
    for k in ("OCR_STUB_WORKER", "OCR_REVIEWER_CMD", "OCR_BACKEND"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(rg.ReviewGateError) as exc:
        rg._run_docs_summary_once(str(tmp_path), "t" * 40, ["docs/a.md"],
                                  repo_root=_pin(tmp_path, "gemini"))
    assert "backend selection failed" in str(exc.value) and "gemini" in str(exc.value)


def test_the_selection_breadcrumb_is_per_thread(monkeypatch, tmp_path):
    # Parallel chunks each resolve a selection and later read it back for the
    # debug log. With a shared global the second writer's selection was read by
    # both; both threads select BEFORE either reads (the barrier), so a shared
    # slot would make them agree. Per-thread slots keep them distinct.
    import threading
    for k in ("OCR_STUB_WORKER", "OCR_REVIEWER_CMD", "OCR_BACKEND"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(rg, "_find_kimi", lambda: None)   # stop right after selection
    # "plain" must fall to the default whatever this machine has installed.
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setattr(ocr_profile.shutil, "which", lambda *_a, **_k: None)
    pinned = _pin(tmp_path, "kimchi")
    barrier = threading.Barrier(2)
    seen, errors = {}, []

    def run(name, repo):
        try:
            try:
                rg._worker_invocation("review", "worker missing", repo_root=repo)
            except rg.ReviewGateError:
                pass          # expected: no worker installed; selection already stored
            barrier.wait(timeout=20)
            seen[name] = rg._backend_local.selection.reason
        except Exception as exc:            # surface thread failures in the assert
            errors.append(repr(exc))

    threads = [threading.Thread(target=run, args=("pinned", pinned)),
               threading.Thread(target=run, args=("plain", None))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    assert seen == {"pinned": "repo config pin", "plain": "default"}
