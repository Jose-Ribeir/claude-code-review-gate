"""Jira side-fetch worker (t18, Kimchi variant only).

A Jira key discovered by rg_tickets is fetched by ONE tiny separate
headless invocation -- NOT the review worker, whose strict no-MCP posture
is untouched: a fresh `kimchi -p` run whose --mcp-config enables ONLY the
persisted desktop Atlassian connector entry from
~/.config/kimchi/harness/mcp.json (overridable for tests with
OCR_JIRA_MCP_CONFIG). The run reuses the app's existing OAuth login, asks
for the issue's title + description (NEVER comments), and parses a
contracted TITLE:/DESCRIPTION: reply. The result is cached in t17's shared
24h cache in the exact same shape.

Fail-open everywhere: no connector config on disk, missing connector
entry, spawn failure, timeout, unparseable output, auth failure, even a
raising runner -- everything is a silent miss with a human-readable note;
the review proceeds on commit messages alone.

The headless invocation boundary (the runner) is mocked everywhere: no
real MCP, no network, no kimchi.
"""
import json
import os
import sys

import pytest  # noqa: F401  (fixtures used via function args)

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, _SCRIPTS)

# review-gate.py is hyphenated; the async-suite shim already loads it.
sys.path.insert(0, _HERE)
from test_async_gate import review_gate  # noqa: E402
import rg_jira_fetch  # noqa: E402
import rg_ticket_fetch  # noqa: E402


# --- fixtures / helpers ---------------------------------------------------------

CONNECTOR_KEY = rg_jira_fetch.CONNECTOR_KEY

_HARNESS = {
    "mcpServers": {
        CONNECTOR_KEY: {
            "command": "/Applications/Kimchi Studio.app/node",
            "args": ["/npx-cli.js", "-y", "mcp-remote@0.15.1",
                     "https://mcp.atlassian.com/v1/mcp/authv2"],
            "env": {"MCP_REMOTE_CONFIG_DIR": "/oauth/cache"},
        },
        "__kimchi-studio-connector__slack": {"command": "/slack"},
        "castai": {"url": "https://mcp.cast.ai/mcp"},
    },
}


def _harness_config(tmp_path, servers=None):
    """The persisted harness mcp.json on disk; returns its path."""
    path = tmp_path / "harness_mcp.json"
    blob = dict(_HARNESS)
    blob["mcpServers"] = servers if servers is not None else dict(_HARNESS["mcpServers"])
    path.write_text(json.dumps(blob), encoding="utf-8")
    return path


def _settings(tickets=True, order=("github", "gitlab", "jira"), jira=None):
    return {"intent": {"tickets": tickets, "tracker_order": list(order),
                       "ticket": None, "messages": True},
            "jira": jira if jira is not None else {"server": "https://co.atlassian.net",
                                                   "project": "PROJ"},
            "notes": []}


def _discovered(keys_roles, extras=(), notes=()):
    return {"selected": [{"key": key, "source": source, "role": role}
                         for key, source, role in keys_roles],
            "extras": list(extras), "notes": list(notes)}


def _jira_key(key="PROJ-123"):
    return _discovered([(key, "branch", "direct")])


WORKER_OUT = "TITLE: Crash on empty input\nDESCRIPTION:\nReproduce with --empty.\nIt must not crash."


def _runner_stub(out=WORKER_OUT, note=None):
    """A headless-invocation stub: (argv, cwd, timeout) -> (stdout, note).
    Records every call; the call's --mcp-config file is read into the record
    (it is a temp file deleted after the call returns)."""
    calls = []

    def run(argv, cwd, timeout):
        record = {"argv": list(argv), "timeout": timeout}
        if "--mcp-config" in argv:
            path = argv[argv.index("--mcp-config") + 1]
            with open(path, encoding="utf-8") as fh:
                record["mcp_config"] = json.load(fh)
            record["mcp_config_path"] = path
        if "-p" in argv:
            record["prompt"] = argv[argv.index("-p") + 1]
        calls.append(record)
        if isinstance(out, Exception):
            raise out
        return out, note

    run.calls = calls
    return run


def _fetch(tmp_path, runner, settings=None, discovered=None, now=1000.0):
    return rg_ticket_fetch.fetch_tickets(
        settings or _settings(),
        discovered if discovered is not None else _jira_key(),
        repo_root=str(tmp_path), runner=runner,
        cache_dir=tmp_path / "cache", now=now)


# --- the enabled path ------------------------------------------------------------

def test_happy_path_spawns_only_the_atlassian_connector_and_maps_fields(tmp_path,
                                                                        monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(_harness_config(tmp_path)))
    runner = _runner_stub()
    result = _fetch(tmp_path, runner)
    (ticket,) = result["tickets"]
    assert result["notes"] == []
    assert ticket["tracker"] == "jira"
    assert ticket["key"] == "PROJ-123"
    assert ticket["title"] == "Crash on empty input"
    assert ticket["body"] == "Reproduce with --empty.\nIt must not crash."
    assert ticket["truncated"] is False
    # One single headless invocation...
    (call,) = runner.calls
    assert call["argv"][0:1] != []
    assert "-p" in call["argv"]
    # ...whose --mcp-config enables ONLY the persisted Atlassian entry,
    # verbatim from the harness config (slack/castai are excluded).
    assert call["mcp_config"] == {
        "mcpServers": {CONNECTOR_KEY: _HARNESS["mcpServers"][CONNECTOR_KEY]}}


def test_prompt_names_the_issue_and_forbids_comments(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(_harness_config(tmp_path)))
    runner = _runner_stub()
    _fetch(tmp_path, runner)
    prompt = runner.calls[0]["prompt"]
    assert "PROJ-123" in prompt
    assert "https://co.atlassian.net" in prompt
    lowered = prompt.lower()
    assert "comment" in lowered and "never" in lowered  # never comments
    assert "TITLE:" in prompt and "DESCRIPTION:" in prompt


def test_the_temp_mcp_config_file_is_removed_afterwards(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(_harness_config(tmp_path)))
    runner = _runner_stub()
    _fetch(tmp_path, runner)
    assert not os.path.exists(runner.calls[0]["mcp_config_path"])


# --- the cache is t17's, in t17's shape -------------------------------------------

def _cache_file(cache_dir, key="PROJ-123"):
    return cache_dir / f"jira--{key}.json"


def test_success_writes_t17s_cache_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(_harness_config(tmp_path)))
    cache = tmp_path / "cache"
    _fetch(tmp_path, _runner_stub())
    data = json.loads(_cache_file(cache).read_text(encoding="utf-8"))
    assert set(data) == {"fetched_at", "title", "body"}
    assert data["fetched_at"] == 1000.0
    assert data["title"] == "Crash on empty input"
    assert data["body"] == "Reproduce with --empty.\nIt must not crash."


def test_cache_hit_skips_the_side_fetch_entirely(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(_harness_config(tmp_path)))
    assert len(_fetch(tmp_path, _runner_stub())["tickets"]) == 1
    second_runner = _runner_stub()
    second = _fetch(tmp_path, second_runner)
    assert second_runner.calls == []
    assert second["tickets"][0]["tracker"] == "jira"


def test_expired_cache_entry_refetches(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(_harness_config(tmp_path)))
    runner = _runner_stub()
    _fetch(tmp_path, runner, now=1000.0)
    _fetch(tmp_path, runner, now=1000.0 + rg_ticket_fetch.TICKET_TTL + 60)
    assert len(runner.calls) == 2


def test_misses_are_never_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(_harness_config(tmp_path)))
    cache = tmp_path / "cache"
    _fetch(tmp_path, _runner_stub(out=None, note="kimchi timed out after 30s"))
    assert not cache.exists() or not _cache_file(cache).exists()


# --- the gates: invoked ONLY when every condition holds ----------------------------

def test_no_connector_config_on_disk_is_a_silent_miss(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(tmp_path / "absent.json"))
    runner = _runner_stub()
    result = _fetch(tmp_path, runner)
    assert result["tickets"] == []
    assert runner.calls == []
    (note,) = result["notes"]
    assert "PROJ-123" in note and "jira" in note.lower()
    assert "connector" in note.lower() or "config" in note.lower()


def test_config_without_the_atlassian_entry_is_a_silent_miss(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG",
                       str(_harness_config(tmp_path, servers={"other": {}})))
    runner = _runner_stub()
    result = _fetch(tmp_path, runner)
    assert result["tickets"] == []
    assert runner.calls == []
    assert "atlassian" in result["notes"][0].lower()


def test_unreadable_or_non_json_config_is_a_silent_miss(tmp_path, monkeypatch):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(path))
    runner = _runner_stub()
    result = _fetch(tmp_path, runner)
    assert result["tickets"] == []
    assert runner.calls == []
    assert result["notes"]


def test_tickets_off_never_invokes_jira(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(_harness_config(tmp_path)))
    runner = _runner_stub()
    result = _fetch(tmp_path, runner, settings=_settings(tickets=False))
    assert result == {"tickets": [], "notes": []}
    assert runner.calls == []


# --- routing: Jira keys to jira, #N keys never -------------------------------------

def test_jira_style_key_routes_to_the_jira_backend_only(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(_harness_config(tmp_path)))
    runner = _runner_stub()
    result = _fetch(tmp_path, runner)
    assert result["tickets"][0]["tracker"] == "jira"
    argv0s = [" ".join(c["argv"]) for c in runner.calls]
    assert not any(a.startswith("gh ") or a.startswith("glab ") for a in argv0s)


def test_issue_style_key_never_reaches_the_jira_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(_harness_config(tmp_path)))
    runner = _runner_stub(out=None, note="gh timed out after 5s")
    result = _fetch(tmp_path, runner, discovered=_jira_key("#45"))
    assert result["tickets"] == []
    # Every attempt was gh/glab; no headless -p invocation at all.
    for call in runner.calls:
        assert "-p" not in call["argv"]


def test_jira_not_in_tracker_order_is_a_clean_note_without_a_spawn(tmp_path,
                                                                   monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(_harness_config(tmp_path)))
    runner = _runner_stub()
    result = _fetch(tmp_path, runner, settings=_settings(order=("github",)))
    assert result["tickets"] == []
    assert runner.calls == []
    assert result["notes"] and "PROJ-123" in result["notes"][0]


# --- every failure mode is a silent note ---------------------------------------------

def test_spawn_failure_is_a_silent_note(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(_harness_config(tmp_path)))
    runner = _runner_stub(out=None, note="kimchi not found on PATH")
    result = _fetch(tmp_path, runner)
    assert result["tickets"] == []
    assert runner.calls  # it was tried
    assert "PROJ-123" in result["notes"][0]
    assert "not found" in result["notes"][0]


def test_timeout_is_a_silent_note(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(_harness_config(tmp_path)))
    runner = _runner_stub(out=None, note="kimchi timed out after 30s")
    result = _fetch(tmp_path, runner)
    assert result["tickets"] == []
    assert "timed out" in result["notes"][0]


def test_unparseable_output_is_a_silent_note(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(_harness_config(tmp_path)))
    runner = _runner_stub(out="I could not reach Jira because the OAuth token expired.")
    result = _fetch(tmp_path, runner)
    assert result["tickets"] == []
    assert "PROJ-123" in result["notes"][0]


def test_output_with_title_but_no_description_marker_is_a_miss(tmp_path,
                                                               monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(_harness_config(tmp_path)))
    runner = _runner_stub(out="TITLE: something\nbut the rest is prose")
    result = _fetch(tmp_path, runner)
    assert result["tickets"] == []
    assert result["notes"]


def test_a_raising_runner_never_escapes(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(_harness_config(tmp_path)))

    def explode(argv, cwd, timeout):
        raise RuntimeError("simulated side-fetch bug")

    result = _fetch(tmp_path, explode)
    assert result["tickets"] == []
    assert result["notes"] and "PROJ-123" in result["notes"][0]


def test_auth_failure_exit_is_a_silent_note(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(_harness_config(tmp_path)))
    runner = _runner_stub(out=None, note="kimchi -p ... exited 1 (OAuth token "
                                         "expired, re-login in Kimchi Studio)")
    result = _fetch(tmp_path, runner)
    assert result["tickets"] == []
    assert result["notes"] and "PROJ-123" in result["notes"][0]


# --- jira settings knobs -----------------------------------------------------------

def test_jira_block_timeout_is_forwarded(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(_harness_config(tmp_path)))
    runner = _runner_stub()
    _fetch(tmp_path, runner,
           settings=_settings(jira={"server": "https://co.atlassian.net",
                                    "timeout": "12"}))
    assert runner.calls[0]["timeout"] == 12.0
    assert "https://co.atlassian.net" in runner.calls[0]["prompt"]


def test_unparseable_timeout_falls_back_to_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(_harness_config(tmp_path)))
    runner = _runner_stub()
    _fetch(tmp_path, runner,
           settings=_settings(jira={"server": "https://co.atlassian.net",
                                    "timeout": "soon"}))
    assert runner.calls[0]["timeout"] == rg_jira_fetch.SIDE_FETCH_TIMEOUT


def test_empty_jira_block_still_fetches_with_careful_defaults(tmp_path,
                                                              monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(_harness_config(tmp_path)))
    runner = _runner_stub()
    result = _fetch(tmp_path, runner, settings=_settings(jira={}))
    assert len(result["tickets"]) == 1


# --- the review worker's argv is untouched -------------------------------------------

def test_review_worker_argv_still_zeroes_mcp():
    args = review_gate.DEFAULT_KIMI_ARGS
    idx = args.index("--mcp-config")
    assert args[idx + 1] == '{"mcpServers":{}}'
    glued = " ".join(args)
    assert "atlassian" not in glued.lower()
    assert "--strict-mcp-config" not in args  # nothing new was added


def test_kimchi_bin_override_is_honoured_for_the_side_fetch(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_JIRA_MCP_CONFIG", str(_harness_config(tmp_path)))
    monkeypatch.setenv("KIMCHI_BIN", "/opt/bin/kimchi")
    runner = _runner_stub()
    _fetch(tmp_path, runner)
    assert runner.calls[0]["argv"][0] == "/opt/bin/kimchi"
