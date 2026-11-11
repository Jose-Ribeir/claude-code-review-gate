"""Ticket key discovery (t16).

Discovery only -- no fetching here (t17 fetches). Given the resolved gate
settings (rg_config.load_settings), the current branch name, and the pushed
commits' messages, discovery produces an ordered, provenance-labelled list
of issue keys:

  precedence: per-repo override key (intent.ticket in .ocr/config.json)
              -> branch name        (direct)
              -> commit messages    (background)

At most 3 keys are selected for fetching; the rest are kept as an ID-only
list. When intent.tickets is off, discovery returns nothing.
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")

sys.path.insert(0, _SCRIPTS)
import rg_config  # noqa: E402
import rg_tickets  # noqa: E402


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# --- key extraction on realistic text (slice 1) ------------------------------

def test_extracts_jira_style_key_from_a_conventional_branch_name():
    assert rg_tickets.extract_keys("feature/PROJ-123-retry-backoff") == ["PROJ-123"]


def test_extracts_issue_style_key_from_a_conventional_branch_name():
    assert rg_tickets.extract_keys("bugfix/#45-crash-on-empty-input") == ["#45"]


def test_extracts_keys_from_commit_message_in_order_of_appearance():
    message = ("Fix overflow in retry backoff\n\n"
               "Closes #128. See PROJ-123 for the rollout plan.")
    assert rg_tickets.extract_keys(message) == ["#128", "PROJ-123"]


def test_extraction_dedupes_repeated_keys_keeping_first_position():
    text = "PROJ-123 landed earlier; PROJ-123 again here, plus #45 and #45"
    assert rg_tickets.extract_keys(text) == ["PROJ-123", "#45"]


def test_version_lookalike_tokens_are_not_keys():
    # case-sensitive project keys keep "utf-8", "json-2" etc. out
    assert rg_tickets.extract_keys("decode utf-8 and parse json-2 sanely") == []


def test_lowercase_lookalikes_are_not_keys():
    assert rg_tickets.extract_keys("feature/proj-123-retry-backoff") == []


def test_key_embedded_in_a_word_is_not_a_key():
    # a letter/glyph glued to the candidate means it is not a ticket reference
    assert rg_tickets.extract_keys("C#123 is a version tag") == []
    assert rg_tickets.extract_keys("xPROJ-123 is not a key") == []


def test_text_without_keys_extracts_nothing():
    assert rg_tickets.extract_keys("wip: tidy up imports") == []
    assert rg_tickets.extract_keys("") == []


# --- the per-repo override key surface (rg_config, slice 3) --------------------

def test_override_ticket_key_loads_through_the_settings_api(tmp_path):
    _write(tmp_path / ".ocr" / "config.json", {"intent": {"ticket": "PROJ-9"}})
    settings = rg_config.load_settings(tmp_path)
    assert settings["intent"]["ticket"] == "PROJ-9"
    assert settings["notes"] == []


def test_override_ticket_key_defaults_to_none():
    settings = rg_config.load_settings()
    assert settings["intent"]["ticket"] is None


def test_override_ticket_key_is_per_repo_only(tmp_path):
    # a global "ticket" would name an issue for every repo on the machine;
    # it is a per-repo override by design and the global value is refused
    _write(rg_config.global_config_path(), {"intent": {"ticket": "PROJ-9"}})
    settings = rg_config.load_settings(tmp_path)
    assert settings["intent"]["ticket"] is None
    assert settings["notes"] != []


def test_non_string_override_ticket_degrades_to_none_with_a_note(tmp_path):
    _write(tmp_path / ".ocr" / "config.json", {"intent": {"ticket": 123}})
    settings = rg_config.load_settings(tmp_path)
    assert settings["intent"]["ticket"] is None
    assert settings["notes"] != []


# --- discovery: precedence, labels, cap (slice 2) -----------------------------

def _settings(tmp_path, repo_intent=None):
    """Resolved settings with tickets switched on, per repo override keys."""
    intent = {"tickets": True}
    if repo_intent:
        intent.update(repo_intent)
    _write(tmp_path / ".ocr" / "config.json", {"intent": intent})
    return rg_config.load_settings(tmp_path)


def test_tickets_off_discovers_nothing_even_with_key_rich_inputs(tmp_path):
    settings = rg_config.load_settings(tmp_path)  # tickets default OFF
    result = rg_tickets.discover_ticket_keys(
        settings,
        branch="feature/PROJ-123-retry-backoff",
        commit_messages=["Fix the overflow\n\nCloses #128. See PROJ-7."],
    )
    assert result["selected"] == []
    assert result["extras"] == []


def test_branch_key_is_the_direct_target(tmp_path):
    result = rg_tickets.discover_ticket_keys(
        _settings(tmp_path),
        branch="feature/PROJ-123-retry-backoff",
        commit_messages=[],
    )
    assert result["selected"] == [
        {"key": "PROJ-123", "source": "branch", "role": "direct"}]
    assert result["extras"] == []


def test_commit_message_keys_are_background(tmp_path):
    result = rg_tickets.discover_ticket_keys(
        _settings(tmp_path),
        branch="feature/tidy-ups",
        commit_messages=[
            "Fix overflow in retry backoff\n\nCloses #128.",
            "Document the rollout\n\nRefs PROJ-7.",
        ],
    )
    assert result["selected"] == [
        {"key": "#128", "source": "commit", "role": "background"},
        {"key": "PROJ-7", "source": "commit", "role": "background"},
    ]


def test_per_repo_override_wins_over_the_branch_name(tmp_path):
    settings = _settings(tmp_path, {"ticket": "PROJ-9"})
    result = rg_tickets.discover_ticket_keys(
        settings,
        branch="feature/ABC-1-retry-backoff",
        commit_messages=["Refs PROJ-7."],
    )
    # the override is the direct target; the branch name is not consulted
    assert result["selected"][0] == {
        "key": "PROJ-9", "source": "override", "role": "direct"}
    assert all(k["key"] != "ABC-1" for k in result["selected"])
    # commit-derived keys still come along as background
    assert result["selected"][1] == {
        "key": "PROJ-7", "source": "commit", "role": "background"}


def test_a_branch_key_repeated_in_commits_stays_direct_and_unrepeated(tmp_path):
    result = rg_tickets.discover_ticket_keys(
        _settings(tmp_path),
        branch="feature/PROJ-123-retry-backoff",
        commit_messages=["Refs PROJ-123.", "Closes #128."],
    )
    keys = [k["key"] for k in result["selected"]]
    assert keys == ["PROJ-123", "#128"]
    assert result["selected"][0]["role"] == "direct"


def test_selection_is_capped_and_extras_are_ids_only(tmp_path):
    result = rg_tickets.discover_ticket_keys(
        _settings(tmp_path, {"ticket": "PROJ-1"}),
        commit_messages=[
            "Closes #2. Refs PROJ-3. See #4. Mention PROJ-5.",
        ],
    )
    assert [k["key"] for k in result["selected"]] == ["PROJ-1", "#2", "PROJ-3"]
    assert result["extras"] == ["#4", "PROJ-5"]


def test_no_keys_anywhere_discovers_nothing_quietly(tmp_path):
    result = rg_tickets.discover_ticket_keys(
        _settings(tmp_path), branch="feature/tidy-ups", commit_messages=["wip"])
    assert result["selected"] == []
    assert result["extras"] == []
    assert result["notes"] == []


def test_an_override_that_is_not_a_key_falls_back_to_the_branch(tmp_path):
    settings = _settings(tmp_path, {"ticket": "banana"})
    result = rg_tickets.discover_ticket_keys(
        settings, branch="feature/PROJ-123-retry-backoff", commit_messages=[])
    assert result["selected"] == [
        {"key": "PROJ-123", "source": "branch", "role": "direct"}]
    assert any("banana" in n for n in result["notes"])
