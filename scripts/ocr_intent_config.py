"""Gate settings surface (Claude port of the Kimchi variant's rg_config).

Single read API for gate settings:

    load_settings(repo_root=None) -> {"intent": {...}, "jira": {},
                                      "notes": [...]}

Resolution order -- later wins, key by key:

    built-in defaults
      <- the GLOBAL config file (under the gate's data dir, the same root
         review-gate._gate_data_dir() derives: $CLAUDE_PLUGIN_DATA or
         ~/.claude/plugins/data/review-gate-local)
      <- a PER-REPO override file (<repo_root>/.ocr/config.json -- the file
         the gate already honors for `blocking`; only its "intent" and "jira"
         top-level keys are read here).

    intent.messages : commit-message injection into the review  (default ON)
    intent.tickets  : linked-ticket fetching                    (default OFF)
    intent.ticket   : explicit ticket-key override              (per-repo
                      only -- it names an issue for the repo it sits in, so
                      a global value is refused with a note)
    intent.tracker_order : tracker backends tried, in order
    jira            : Jira server coordinates block (e.g. {"server": ...,
                      "project": ...}); the key is reserved so configs stay
                      portable from the Kimchi variant, but the Claude
                      variant ships NO Jira backend -- its review worker
                      hard-blocks MCP. A "jira" entry in tracker_order yields
                      a clean unsupported-tracker note (ocr_ticket_fetch).

Missing files degrade to defaults silently -- that is the normal state for
most installs. Unreadable/invalid files, and invalid VALUES for known keys,
degrade to defaults WITH A NOTE in "notes" (a note is for a human reading a
report or a doctor run; the gate itself never fails on config).

This module reads config; it never writes it.
"""
import json
import os
from pathlib import Path

# The tracker backends a tracker_order may name. GitHub/GitLab are fetchable
# in this variant (ocr_ticket_fetch); "jira" stays a recognized name (so the
# Kimchi variant's default order parses without noise) but its backend is a
# clean unsupported-tracker miss note here.
_KNOWN_TRACKERS = ("github", "gitlab", "jira")
_ON_WORDS = {"1", "true", "yes", "on"}
_OFF_WORDS = {"0", "false", "no", "off"}


def _defaults():
    return {
        "intent": {
            "tickets": False,
            "messages": True,
            "ticket": None,
            "tracker_order": list(_KNOWN_TRACKERS),
        },
        "jira": {},
    }


def data_dir():
    """The gate's data dir, mirroring review-gate._gate_data_dir()
    (review-gate.py is hyphenated and does not import cleanly, so the path
    logic is replicated here -- $CLAUDE_PLUGIN_DATA, else
    $CLAUDE_CONFIG_DIR|'~/.claude'/plugins/data/review-gate-local). Tests
    redirect it through CLAUDE_PLUGIN_DATA exactly like the gate itself,
    keeping them off a developer's real config.
    """
    data = os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
    if not data:
        cfg = os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or os.path.join(
            os.path.expanduser("~"), ".claude"
        )
        data = os.path.join(cfg, "plugins", "data", "review-gate-local")
    return Path(data)


def global_config_path():
    """The global gate config file, under the gate's data dir: the place the
    gate keeps the rest of its supervisor-side files (the debug log, parked
    reports, review worktrees) that survives plugin upgrades.
    """
    return data_dir() / "config.json"


def repo_config_path(repo_root):
    """The per-repo override file: the .ocr/config.json the gate already uses."""
    return Path(repo_root) / ".ocr" / "config.json"


def load_settings(repo_root=None):
    """The resolved gate settings plus human-readable load notes.

    repo_root may be None (no per-repo override consulted; the pre-review
    stage does not always have a repo). The result is the single read API
    other stages call -- e.g. ticket fetching consults intent.tickets and
    skips entirely when it is off.
    """
    resolved = _defaults()
    notes = []
    _merge_file(resolved, notes, global_config_path(), "global")
    if repo_root:
        _merge_file(resolved, notes, repo_config_path(repo_root), "repo")
    return {"intent": resolved["intent"], "jira": resolved["jira"], "notes": notes}


def _merge_file(resolved, notes, path, label):
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except OSError as exc:
        notes.append(f"{label} config {path} unreadable ({exc}); defaults used for its values")
        return
    try:
        data = json.loads(raw)
    except ValueError:
        notes.append(f"{label} config {path} is not valid JSON; defaults used for its values")
        return
    if not isinstance(data, dict):
        notes.append(f"{label} config {path} is not a JSON object; defaults used for its values")
        return
    _merge_intent(resolved["intent"], notes, path, label, data.get("intent"))
    _merge_jira(resolved["jira"], notes, path, label, data.get("jira"))


def _merge_intent(resolved, notes, path, label, intent):
    if intent is None:
        return
    if not isinstance(intent, dict):
        notes.append(f"{label} config {path}: \"intent\" must be an object; ignored")
        return
    for key in ("tickets", "messages"):
        if key not in intent:
            continue
        ok, value = _as_switch(intent[key])
        if ok:
            resolved[key] = value
        else:
            notes.append(f"{label} config {path}: invalid intent.{key} value "
                         f"{intent[key]!r}; current value kept")
    if "tracker_order" in intent:
        ok, value = _as_tracker_order(intent["tracker_order"], notes, path, label)
        if ok:
            resolved["tracker_order"] = value
    if "ticket" in intent:
        _merge_ticket(resolved, notes, path, label, intent["ticket"])


def _merge_ticket(resolved, notes, path, label, ticket):
    """An explicit ticket-key override. Per-repo by design: a global value
    would name one issue for every repo on the machine, so it is refused.
    Whether the value is a plausible key is discovery's call (ocr_tickets),
    not the settings read's."""
    if isinstance(ticket, str) and label == "repo":
        resolved["ticket"] = ticket
    elif isinstance(ticket, str):
        notes.append(f"{label} config {path}: intent.ticket is a per-repo "
                     "override; the global value is ignored")
    else:
        notes.append(f"{label} config {path}: invalid intent.ticket value "
                     f"{ticket!r}; current value kept")


def _merge_jira(resolved, notes, path, label, jira):
    if jira is None:
        return
    if not isinstance(jira, dict):
        notes.append(f"{label} config {path}: \"jira\" must be an object of "
                     "coordinates; ignored")
        return
    for key, value in jira.items():
        if isinstance(value, str):
            resolved[str(key)] = value
        else:
            notes.append(f"{label} config {path}: jira.{key} must be a string; "
                         "ignored")


def _as_switch(value):
    """(ok, bool). Accepts booleans, 0/1, and on/off-style words."""
    if isinstance(value, bool):
        return True, value
    if isinstance(value, int):
        return (True, bool(value)) if value in (0, 1) else (False, None)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _ON_WORDS:
            return True, True
        if word in _OFF_WORDS:
            return True, False
    return False, None


def _as_tracker_order(value, notes, path, label):
    """(ok, [tracker, ...]). Unknown trackers are dropped with a note; an
    order that is not a list, or names nothing known, is rejected wholesale."""
    if not isinstance(value, (list, tuple)):
        notes.append(f"{label} config {path}: intent.tracker_order must be a list; "
                     "current order kept")
        return False, None
    order, unknown = [], []
    for entry in value:
        name = str(entry).strip().lower() if isinstance(entry, str) else ""
        if name in _KNOWN_TRACKERS:
            if name not in order:
                order.append(name)
        elif name:
            unknown.append(name)
    if unknown:
        notes.append(f"{label} config {path}: unknown tracker(s) in "
                     f"intent.tracker_order ignored: {', '.join(sorted(set(unknown)))}")
    if not order:
        notes.append(f"{label} config {path}: intent.tracker_order names no known "
                     "tracker; current order kept")
        return False, None
    return True, order
