"""Linked-ticket fetch (Claude port of the Kimchi variant's t17 tests):
title + description only, read-only, fail-open.

For the keys discovery selected (ocr_tickets), the supervisor fetches the
ticket's title and description -- never comments -- read-only through the
maintainer's existing gh/glab sign-in (GitHub: issue, PR fallback; GitLab:
glab api against the current repo's project). Results are cached 24h in the
gate's data dir. Any failure (CLI missing, offline, timeout, non-zero exit,
unexpected JSON) is a silent skip with a human-readable note; a review never
fails on ticket fetching. This variant ships NO Jira backend (its review
worker hard-blocks MCP): the jira slot is a clean unsupported-tracker miss
note.

The CLI boundary is mocked everywhere: no network, no gh/glab needed.
"""
import json
import os
import sys

import pytest  # noqa: F401  (fixtures used via function args)

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")

# review-gate.py is hyphenated; the async-suite shim already loads it.
sys.path.insert(0, _HERE)
from test_async_gate import review_gate  # noqa: E402
import ocr_ticket_fetch  # noqa: E402


# --- helpers -----------------------------------------------------------------

def _settings(tickets=True, order=("github", "gitlab", "jira")):
    return {"intent": {"tickets": tickets, "tracker_order": list(order),
                       "ticket": None, "messages": True},
            "jira": {}, "notes": []}


def _discovered(keys_roles, extras=(), notes=()):
    return {"selected": [{"key": key, "source": source, "role": role}
                         for key, source, role in keys_roles],
            "extras": list(extras), "notes": list(notes)}


def _discovered_issue(key="#45"):
    return _discovered([(key, "branch", "direct")])


def _stub(routes):
    """A CLI-boundary stub. routes: list of (substring-of-argv, (stdout, note)).
    An unmatched call misses cleanly (the real failure modes all look like
    this to the fetcher). Records every argv for assertions."""
    calls = []

    def run(argv, cwd, timeout):
        calls.append(list(argv))
        glued = " ".join(str(a) for a in argv)
        for match, result in routes:
            if match in glued:
                return result
        return None, "no such resource (simulated non-zero exit)"

    run.calls = calls
    return run


ISSUE_JSON = json.dumps({"title": "Crash on empty input",
                         "body": "Reproduce: run with --empty.\nIt must not crash."})
ISSUE_BRO_FILE_JSON = json.dumps({"title": "Crash on empty input", "body": None})
PR_JSON = json.dumps({"title": "Fix the crash", "body": "Implements the retry."})
GLAB_JSON = json.dumps({"title": "Crash on empty input",
                        "description": "Reproduce from the issue body."})


# --- off by default -----------------------------------------------------------

def test_off_in_settings_fetches_nothing_and_never_touches_the_cli():
    runner = _stub([])
    result = ocr_ticket_fetch.fetch_tickets(_settings(tickets=False),
                                           _discovered_issue(), runner=runner)
    assert result == {"tickets": [], "notes": []}
    assert runner.calls == []


def test_empty_selection_fetches_nothing():
    result = ocr_ticket_fetch.fetch_tickets(_settings(), _discovered([]))
    assert result == {"tickets": [], "notes": []}


# --- github success path -------------------------------------------------------

def test_github_issue_success_carries_title_and_body_with_provenance(tmp_path):
    runner = _stub([("gh issue view", (ISSUE_JSON, None))])
    result = ocr_ticket_fetch.fetch_tickets(
        _settings(), _discovered_issue(), repo_root=str(tmp_path),
        runner=runner, cache_dir=tmp_path / "cache")
    assert runner.calls == [["gh", "issue", "view", "45", "--json", "title,body"]]
    assert result["notes"] == []
    (ticket,) = result["tickets"]
    assert ticket["key"] == "#45"
    assert ticket["tracker"] == "github"
    assert ticket["source"] == "branch"
    assert ticket["role"] == "direct"
    assert ticket["title"] == "Crash on empty input"
    assert ticket["body"] == "Reproduce: run with --empty.\nIt must not crash."
    assert ticket["truncated"] is False


def test_fetching_never_requests_comments(tmp_path):
    runner = _stub([("gh issue view", (ISSUE_JSON, None))])
    ocr_ticket_fetch.fetch_tickets(_settings(), _discovered_issue(),
                                  repo_root=str(tmp_path), runner=runner,
                                  cache_dir=tmp_path / "cache")
    for argv in runner.calls:
        assert "--comments" not in argv
        assert "-c" not in argv
        assert "--json" not in argv or argv[argv.index("--json") + 1] == "title,body"
        assert not any("notes" in a.split("/") for a in argv)  # gitlab notes endpoint


def test_null_body_becomes_empty_text_not_a_crash(tmp_path):
    runner = _stub([("gh issue view", (ISSUE_BRO_FILE_JSON, None))])
    result = ocr_ticket_fetch.fetch_tickets(
        _settings(), _discovered_issue(), repo_root=str(tmp_path),
        runner=runner, cache_dir=tmp_path / "cache")
    (ticket,) = result["tickets"]
    assert ticket["body"] == ""


# --- PR fallback ----------------------------------------------------------------

def test_pr_fallback_when_the_issue_lookup_fails(tmp_path):
    runner = _stub([("gh pr view", (PR_JSON, None))])
    result = ocr_ticket_fetch.fetch_tickets(
        _settings(), _discovered_issue(), repo_root=str(tmp_path),
        runner=runner, cache_dir=tmp_path / "cache")
    glued = [" ".join(a) for a in runner.calls]
    assert any(c.startswith("gh issue view 45") for c in glued)
    assert any(c.startswith("gh pr view 45") for c in glued)
    (ticket,) = result["tickets"]
    assert ticket["tracker"] == "github"
    assert ticket["title"] == "Fix the crash"


# --- gitlab backend ---------------------------------------------------------------

def test_gitlab_api_success_and_description_maps_to_body(tmp_path):
    # github misses (no route), gitlab answers.
    runner = _stub([("glab api", (GLAB_JSON, None))])
    result = ocr_ticket_fetch.fetch_tickets(
        _settings(), _discovered_issue(), repo_root=str(tmp_path),
        runner=runner, cache_dir=tmp_path / "cache")
    (ticket,) = result["tickets"]
    assert ticket["tracker"] == "gitlab"
    assert ticket["body"] == "Reproduce from the issue body."
    assert any(a[:2] == ["glab", "api"] and a[2] == "projects/:fullpath/issues/45"
               for a in runner.calls)


def test_tracker_order_decides_which_backend_answers_first(tmp_path):
    runner = _stub([("glab api", (GLAB_JSON, None)), ("gh issue view", (ISSUE_JSON, None))])
    result = ocr_ticket_fetch.fetch_tickets(
        _settings(order=("gitlab", "github")), _discovered_issue(),
        repo_root=str(tmp_path), runner=runner, cache_dir=tmp_path / "cache")
    (ticket,) = result["tickets"]
    assert ticket["tracker"] == "gitlab"
    assert runner.calls[0][0] == "glab"  # gitlab tried before github


# --- jira slot: no backend in this variant --------------------------------------

def test_jira_key_is_a_clean_unsupported_tracker_note(tmp_path):
    # The default tracker_order still lists "jira" (configs stay portable
    # from the Kimchi variant), but this variant's review worker hard-blocks
    # MCP so no Jira backend ships: a Jira-style key yields a clean miss note
    # that names the tracker and the key, never a crash, never a CLI call.
    runner = _stub([])
    result = ocr_ticket_fetch.fetch_tickets(
        _settings(), _discovered([("PROJ-123", "branch", "direct")]),
        repo_root=str(tmp_path), runner=runner, cache_dir=tmp_path / "cache")
    assert result["tickets"] == []
    assert runner.calls == []  # gh/glab only handle #N keys; jira never runs
    (note,) = result["notes"]
    assert "PROJ-123" in note and "jira" in note.lower()
    assert "unsupported" in note.lower()


def test_key_without_a_matching_backend_is_a_note_not_a_crash(tmp_path):
    runner = _stub([])
    result = ocr_ticket_fetch.fetch_tickets(
        _settings(order=("github",)), _discovered([("PROJ-123", "commit", "background")]),
        repo_root=str(tmp_path), runner=runner, cache_dir=tmp_path / "cache")
    assert result["tickets"] == []
    assert result["notes"] and "PROJ-123" in result["notes"][0]


# --- cache -----------------------------------------------------------------------

def _fresh_cache_hit(cache_dir, key):
    runner = _stub([("gh issue view", (ISSUE_JSON, None))])
    first = ocr_ticket_fetch.fetch_tickets(
        _settings(), _discovered_issue(key), runner=runner, cache_dir=cache_dir)
    assert len(first["tickets"]) == 1 and runner.calls, "setup: cache not warmed"


def test_cache_hit_avoids_a_refetch(tmp_path):
    cache = tmp_path / "cache"
    _fresh_cache_hit(cache, "#45")
    runner2 = _stub([])
    second = ocr_ticket_fetch.fetch_tickets(
        _settings(), _discovered_issue(), runner=runner2, cache_dir=cache)
    assert runner2.calls == []
    assert second["tickets"][0]["title"] == "Crash on empty input"


def test_expired_cache_entry_refetches(tmp_path):
    cache = tmp_path / "cache"
    runner = _stub([("gh issue view", (ISSUE_JSON, None))])
    first = ocr_ticket_fetch.fetch_tickets(
        _settings(), _discovered_issue(), runner=runner, cache_dir=cache,
        now=1000.0 + ocr_ticket_fetch.TICKET_TTL + 60)
    # Backdate then re-fetch later: a miss re-runs the CLI.
    backdated_calls = len(runner.calls)
    second = ocr_ticket_fetch.fetch_tickets(
        _settings(), _discovered_issue(), runner=runner, cache_dir=cache,
        now=1000.0 + 2 * ocr_ticket_fetch.TICKET_TTL + 120)
    assert first["tickets"][0]["title"] == second["tickets"][0]["title"]
    assert len(runner.calls) == backdated_calls * 2  # fetched once per window


def test_corrupt_cache_file_is_a_miss_not_a_crash(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "github--%2345.json").write_text("{not json", encoding="utf-8")
    runner = _stub([("gh issue view", (ISSUE_JSON, None))])
    result = ocr_ticket_fetch.fetch_tickets(
        _settings(), _discovered_issue(), runner=runner, cache_dir=cache)
    assert result["tickets"][0]["tracker"] == "github"
    assert len(runner.calls) == 1


def test_misses_are_not_cached(tmp_path):
    cache = tmp_path / "cache"
    runner1 = _stub([])  # everything misses
    ocr_ticket_fetch.fetch_tickets(_settings(), _discovered_issue(),
                                  runner=runner1, cache_dir=cache)
    runner2 = _stub([("gh issue view", (ISSUE_JSON, None))])
    second = ocr_ticket_fetch.fetch_tickets(
        _settings(), _discovered_issue(), runner=runner2, cache_dir=cache)
    assert runner2.calls, "a transient miss must be retried on the next push"
    assert second["tickets"]


# --- fail-open failure modes ------------------------------------------------------

def test_missing_cli_is_a_silent_skip_with_a_note(tmp_path):
    runner = _stub([("gh", (None, "gh not found on PATH")),
                    ("glab", (None, "glab not found on PATH"))])
    result = ocr_ticket_fetch.fetch_tickets(
        _settings(), _discovered_issue(), runner=runner, cache_dir=tmp_path / "cache")
    assert result["tickets"] == []
    (note,) = result["notes"]
    assert "#45" in note and "gh" in note


def test_timeout_is_a_silent_skip_with_a_note(tmp_path):
    runner = _stub([("gh", (None, "gh timed out after 5s")),
                    ("glab", (None, "glab timed out after 5s"))])
    result = ocr_ticket_fetch.fetch_tickets(
        _settings(), _discovered_issue(), runner=runner, cache_dir=tmp_path / "cache")
    assert result["tickets"] == []
    assert result["notes"] and "timed out" in result["notes"][0]


def test_unparseable_json_is_a_miss_with_a_note(tmp_path):
    runner = _stub([("gh issue view", ("<html>rate limited</html>", None)),
                    ("glab", ("<html>rate limited</html>", None))])
    result = ocr_ticket_fetch.fetch_tickets(
        _settings(), _discovered_issue(), runner=runner, cache_dir=tmp_path / "cache")
    assert result["tickets"] == []
    assert result["notes"]


def test_a_raising_cli_boundary_never_escapes(tmp_path):
    def explode(argv, cwd, timeout):
        raise RuntimeError("simulated supervisor bug")

    result = ocr_ticket_fetch.fetch_tickets(
        _settings(), _discovered_issue(), runner=explode, cache_dir=tmp_path / "cache")
    assert result["tickets"] == []
    assert result["notes"] and "#45" in result["notes"][0]


# --- budget & shape ---------------------------------------------------------------

def test_giant_description_is_hard_truncated_with_a_visible_marker(tmp_path):
    giant = json.dumps({"title": "big", "body": "x" * (ocr_ticket_fetch.DESCRIPTION_MAX_CHARS + 500)})
    runner = _stub([("gh issue view", (giant, None))])
    result = ocr_ticket_fetch.fetch_tickets(
        _settings(), _discovered_issue(), runner=runner, cache_dir=tmp_path / "cache")
    (ticket,) = result["tickets"]
    assert ticket["truncated"] is True
    assert "truncated" in ticket["body"]
    assert len(ticket["body"]) < ocr_ticket_fetch.DESCRIPTION_MAX_CHARS + 200


def test_every_selected_key_is_attempted_in_discovery_order(tmp_path):
    runner = _stub([("gh issue view 45", (ISSUE_JSON, None)),
                    ("gh issue view 46", (PR_JSON, None))])
    result = ocr_ticket_fetch.fetch_tickets(
        _settings(), _discovered([("#45", "branch", "direct"),
                                  ("#46", "commit", "background")]),
        runner=runner, cache_dir=tmp_path / "cache")
    assert [t["key"] for t in result["tickets"]] == ["#45", "#46"]
    assert [t["role"] for t in result["tickets"]] == ["direct", "background"]


# --- review-gate wiring (manifest extras slot) ---------------------------------------



def _repo_with_tickets_on(tmp_path, key="#45"):
    cfg = tmp_path / ".ocr" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"intent": {"tickets": True, "ticket": key}}),
                   encoding="utf-8")
    return tmp_path


def test_collect_ticket_context_off_by_default_returns_none(tmp_path):
    assert review_gate._collect_ticket_context(str(tmp_path), "main", None) is None


def test_collect_ticket_context_assembles_the_extras_block(tmp_path, monkeypatch):
    repo = _repo_with_tickets_on(tmp_path)
    payload = {"tickets": [{"key": "#45", "tracker": "github", "title": "T",
                            "body": "B", "source": "override", "role": "direct",
                            "truncated": False}],
               "notes": []}
    monkeypatch.setattr(review_gate.ocr_ticket_fetch, "fetch_tickets",
                        lambda *a, **k: payload)
    block = review_gate._collect_ticket_context(
        str(repo), "main",
        [{"sha": "a" * 40, "subject": "fix", "body": "", "files": ["a.py"]}])
    assert block["tickets"] == payload["tickets"]
    assert block["listed_only"] == []
    assert block["notes"] == []


def test_collect_ticket_context_keeps_discovery_extras_and_notes(tmp_path, monkeypatch):
    repo = _repo_with_tickets_on(tmp_path)
    monkeypatch.setattr(review_gate.ocr_ticket_fetch, "fetch_tickets",
                        lambda settings, discovered, **kw: {
                            "tickets": [], "notes": ["#45: gh not found on PATH"]})
    monkeypatch.setattr(
        review_gate.ocr_tickets, "discover_ticket_keys",
        lambda *a, **k: {"selected": [{"key": "#45", "source": "override",
                                       "role": "direct"}],
                         "extras": ["PROJ-9"], "notes": ["override oddity"]})
    block = review_gate._collect_ticket_context(str(repo), "main", None)
    assert block["listed_only"] == ["PROJ-9"]
    assert "override oddity" in block["notes"]
    assert "#45: gh not found on PATH" in block["notes"]


def test_collect_ticket_context_never_raises(tmp_path, monkeypatch):
    repo = _repo_with_tickets_on(tmp_path)
    monkeypatch.setattr(review_gate.ocr_ticket_fetch, "fetch_tickets",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    block = review_gate._collect_ticket_context(str(repo), "main", None)
    assert block["tickets"] == []
    assert any("boom" in note for note in block["notes"])
