"""Shared pytest setup (Kimchi port).

Every run must point the gate's EXTERNAL state store at a private temp dir:
the store lives at ~/.local/state/review-gate/ in production (D12 of the port
plan) and a test that runs the gate without the override would write into the
developer's real store. The plugin-data override is kept for the upstream
tests that still set it.
"""
import os

import pytest


def scrub_git_trace(env):
    """A copy of `env` with every GIT_TRACE* key removed.

    Kimchi Studio's git-attribution feature injects GIT_TRACE* env vars
    (GIT_TRACE_REFS, GIT_TRACE2_EVENT, ...) pointing at per-session temp
    dirs that are deleted when the session ends. Any git run inheriting
    them fails with exit 128 as soon as it tries to write its trace target
    (verified live: `git clone --no-hardlinks` in the supervisor dies this
    way). Every test subprocess that runs git must get a scrubbed env.
    """
    return {k: v for k, v in env.items() if not k.startswith("GIT_TRACE")}


@pytest.fixture(autouse=True)
def _private_state_store(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("OCR_STATE_DIR", str(tmp_path_factory.mktemp("rg-state")))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path_factory.mktemp("rg-data")))
    # The autouse half of the GIT_TRACE* scrub: the helpers below copy
    # os.environ, so scrubbing the live environment covers every subprocess
    # a test spawns; the helpers call scrub_git_trace() again explicitly.
    for k in [x for x in os.environ if x.startswith("GIT_TRACE")]:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def gate_env(monkeypatch, tmp_path):
    """Environment for an in-process supervisor run against the stub reviewer
    (tests/kimi_stub.py via OCR_STUB_WORKER=1): a private store, no inherited
    OCR_*/STUB_* settings. Returns the monkeypatch, so a test can also lower
    the gate module's limits."""
    import os
    for k in list(os.environ):
        if k.startswith(("OCR_", "STUB_")):
            monkeypatch.delenv(k)
    monkeypatch.setenv("OCR_STATE_DIR", str(tmp_path / "gate-state"))
    monkeypatch.setenv("OCR_STUB_WORKER", "1")
    monkeypatch.setenv("STUB_TRACE", str(tmp_path / "stub.trace"))
    monkeypatch.setenv("OCR_INLINE_BUDGET", "3")
    return monkeypatch
