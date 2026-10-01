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
