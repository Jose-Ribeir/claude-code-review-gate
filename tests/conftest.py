"""Shared pytest setup.

The gate writes an always-on, rotating metrics log (review-gate-debug.log) under
$CLAUDE_PLUGIN_DATA. A test that runs the gate without pointing that at its own
temp dir would write into the developer's real ~/.claude, so every test starts
with a private default; tests that need a specific dir set it again.
"""
import pytest


@pytest.fixture(autouse=True)
def _private_plugin_data(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path_factory.mktemp("plugin-data")))


@pytest.fixture
def gate_env(monkeypatch, tmp_path):
    """Environment for an in-process supervisor run against the stub reviewer
    (tests/stub_reviewer.py): a private data dir, no inherited OCR_*/STUB_* settings.
    Returns the monkeypatch, so a test can also lower the gate module's limits."""
    import os
    import sys
    stub = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stub_reviewer.py")
    for k in list(os.environ):
        if k.startswith(("OCR_", "STUB_")):
            monkeypatch.delenv(k)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "gate-data"))
    monkeypatch.setenv("OCR_REVIEWER_CMD", f'"{sys.executable}" "{stub}"')
    monkeypatch.setenv("STUB_TRACE", str(tmp_path / "stub.trace"))
    monkeypatch.setenv("OCR_INLINE_BUDGET", "30")
    return monkeypatch
