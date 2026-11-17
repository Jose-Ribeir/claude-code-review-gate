"""Gate settings surface (t15).

Single read API for gate settings:

    load_settings(repo_root=None) -> {"intent": {...}, "jira": {...},
                                      "checks": {...}, "notes": [...]}

Resolution order -- later wins, key by key:

    built-in defaults
      <- the GLOBAL config file (under the gate's external store root, the
         same root rg_store.store_root() derives: $OCR_STATE_DIR or
         ~/.local/state/review-gate)
      <- a PER-REPO override file (<repo_root>/.ocr/config.json -- the file
         the gate already honors for `blocking`; only its "intent", "jira",
         and "checks" top-level keys are read here).

    intent.messages : commit-message injection into the review  (default ON)
    intent.tickets  : linked-ticket fetching                    (default OFF)
    intent.ticket   : explicit ticket-key override              (per-repo
                      only -- it names an issue for the repo it sits in, so
                      a global value is refused with a note)
    intent.tracker_order : tracker backends tried, in order
    jira            : Jira server coordinates block (e.g. {"server": ...,
                      "project": ...}); the Kimchi variant's ticket backend
    checks          : the custom-checks surface (SPEC-14). Per check: an
                      "enabled" switch, a "mode" of "block" or "warn", a
                      "globs" list of repo-relative globs the check runs
                      over, and pass-through options ("sections",
                      "template", "sentence_cap", "sweep"). A "command"
                      string names a repo-relative executable (run in a later
                      ticket). Command checks may define new check names;
                      other unknown names are typos and are ignored.

Missing files degrade to defaults silently -- that is the normal state for
most installs. Unreadable/invalid files, and invalid VALUES for known keys,
degrade to defaults WITH A NOTE in "notes" (a note is for a human reading a
report or a doctor run; the gate itself never fails on config).

This module reads config; it never writes it.
"""
import json
from pathlib import Path

import rg_store

# The tracker backends a tracker_order may name (Jira ships in the Kimchi
# variant; the Claude variant's is GitHub/GitLab only).
_KNOWN_TRACKERS = ("github", "gitlab", "jira")
_ON_WORDS = {"1", "true", "yes", "on"}
_OFF_WORDS = {"0", "false", "no", "off"}

# The checks surface (SPEC-14). Built-in defaults: docs-deslop on (it applies
# to any repo with markdown), the ADR duties off (they presuppose ADR
# conventions a repo may not follow); everything blocks by default. Globs are
# repo-relative; the ADR duties watch the common ADR locations.
_ADR_GLOBS = ["docs/adr/**/*.md", "**/adr/*.md"]
_BUILT_IN_CHECKS = {
    "docs-deslop": {"enabled": True, "mode": "block", "globs": ["**/*.md"]},
    "adr-wait-what": {"enabled": False, "mode": "block",
                      "globs": list(_ADR_GLOBS)},
    "adr-integrity": {"enabled": False, "mode": "block",
                      "globs": list(_ADR_GLOBS)},
    # The v2 deslop stubs (story 24), recognized and INERT in v1 (markdown
    # only): declared disabled defaults so the toggle surface is ready for
    # v2 without behavior change. Naming one never earns the unknown-check
    # typo note; nothing in the gate reads or executes them in v1.
    "commit_messages": {"enabled": False, "mode": "block", "globs": []},
    "code_comments": {"enabled": False, "mode": "block", "globs": []},
}
# A user-declared command check defaults to on and empty-scoped: the
# declaration itself is the enabling act, and how an empty glob list scopes a
# run is the execution ticket's call (SPEC-14.4).
_COMMAND_CHECK_DEFAULTS = {"enabled": True, "mode": "block", "globs": []}


def _defaults():
    return {
        "intent": {
            "tickets": False,
            "messages": True,
            "ticket": None,
            "tracker_order": list(_KNOWN_TRACKERS),
        },
        "jira": {},
        # Deep-copied per call: the checks maps are mutable and a returned
        # map may be mutated by a caller.
        "checks": {name: dict(check) | {"globs": list(check["globs"])}
                   for name, check in _BUILT_IN_CHECKS.items()},
    }


def global_config_path():
    """The global gate config file, under the external state store root.

    The store root is where the gate keeps the rest of its supervisor-side
    files (verdicts/, clones/, data/, the gate-dir pointer) and is already
    redirected by OCR_STATE_DIR, which keeps tests off a developer's real
    config.
    """
    return rg_store.store_root() / "config.json"


def repo_config_path(repo_root):
    """The per-repo override file: the .ocr/config.json the gate already uses."""
    return Path(repo_root) / ".ocr" / "config.json"


def load_settings(repo_root=None):
    """The resolved gate settings plus human-readable load notes.

    repo_root may be None (no per-repo override consulted; the pre-review
    stage does not always have a repo). The result is the single read API
    other stages call -- e.g. intent collection consults intent.messages and
    skips entirely when it is off.
    """
    resolved = _defaults()
    notes = []
    _merge_file(resolved, notes, global_config_path(), "global")
    if repo_root:
        _merge_file(resolved, notes, repo_config_path(repo_root), "repo")
    _note_globless_command_checks(resolved["checks"], notes)
    return {"intent": resolved["intent"], "jira": resolved["jira"],
            "checks": resolved["checks"], "notes": notes}


def _note_globless_command_checks(checks, notes):
    """One note per resolved command check with no globs. An empty glob list
    matches no changed file, so the check silently never runs -- a declared
    check must never be invisible (ADR 0006: fail-soft AND observable)."""
    for name, check in checks.items():
        if isinstance(check, dict) and isinstance(check.get("command"), str) \
                and check.get("enabled") and not check.get("globs"):
            notes.append(f"check \"{name}\" declares a command but no globs; "
                         "it matches no changed file and will never run")


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
    _merge_checks(resolved["checks"], notes, path, label, data.get("checks"))


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
    Whether the value is a plausible key is discovery's call (rg_tickets),
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
        if isinstance(value, bool):
            notes.append(f"{label} config {path}: jira.{key} must be a string "
                         "or a number; ignored")
        elif isinstance(value, str):
            resolved[str(key)] = value
        elif isinstance(value, (int, float)):
            # README documents a JSON-number form ("timeout": 30);
            # rg_jira_fetch._timeout parses numbers charitably, so accept
            # them here instead of dropping the documented shape.
            resolved[str(key)] = str(value)
        else:
            notes.append(f"{label} config {path}: jira.{key} must be a string "
                         "or a number; ignored")


def _merge_checks(resolved, notes, path, label, checks):
    if checks is None:
        return
    if not isinstance(checks, dict):
        notes.append(f"{label} config {path}: \"checks\" must be an object of "
                     "per-check objects; ignored")
        return
    for name, declaration in checks.items():
        _merge_check(resolved, notes, path, label, str(name), declaration)


def _merge_check(resolved, notes, path, label, name, declaration):
    if not isinstance(declaration, dict):
        notes.append(f"{label} config {path}: check \"{name}\" must be an "
                     "object; ignored")
        return
    if name not in resolved:
        # A new name is a user command check only when it names an executable
        # to run; without a "command" it reads as a typo of a built-in name
        # and is ignored (an unknown check can never break the gate).
        command = declaration.get("command")
        if isinstance(command, str):
            resolved[name] = dict(_COMMAND_CHECK_DEFAULTS,
                                  globs=list(_COMMAND_CHECK_DEFAULTS["globs"]))
        else:
            notes.append(f"{label} config {path}: unknown check \"{name}\" "
                         "(a user check must name a \"command\"); ignored")
            return
    _merge_check_entry(resolved[name], notes, path, label, name, declaration)


def _merge_check_entry(resolved, notes, path, label, name, declaration):
    for key, value in declaration.items():
        ok, parsed = _as_check_value(key, value, notes, path, label, name)
        if ok:
            resolved[key] = parsed


def _as_check_value(key, value, notes, path, label, name):
    """(ok, value) for one key of one check. Anything invalid degrades to a
    note and keeps the check's current value; an unknown key is noted and
    ignored. Command checks carry their options here too -- the same
    pass-through options a built-in may carry."""
    note = f"{label} config {path}: "
    if key == "enabled":
        ok, parsed = _as_switch(value)
        if ok:
            return True, parsed
        notes.append(f"{note}invalid checks.{name}.enabled value {value!r}; "
                     "current value kept")
        return False, None
    if key == "mode":
        if value in ("block", "warn"):
            return True, value
        notes.append(f"{note}checks.{name}.mode must be \"block\" or \"warn\"; "
                     "current value kept")
        return False, None
    if key == "globs":
        if not isinstance(value, list):
            notes.append(f"{note}checks.{name}.globs must be a list of "
                         "repo-relative globs; current value kept")
            return False, None
        globs = [g for g in value if isinstance(g, str)]
        if len(globs) != len(value):
            notes.append(f"{note}non-string entries in checks.{name}.globs "
                         "ignored")
        return True, globs
    if key == "sections":
        if isinstance(value, list) and all(isinstance(s, str) for s in value):
            return True, list(value)
        notes.append(f"{note}checks.{name}.sections must be a list of "
                     "strings; current value kept")
        return False, None
    if key == "template":
        if isinstance(value, str):
            return True, value
        notes.append(f"{note}checks.{name}.template must be a string; "
                     "current value kept")
        return False, None
    if key == "sentence_cap":
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return True, value
        notes.append(f"{note}checks.{name}.sentence_cap must be a positive "
                     "integer; current value kept")
        return False, None
    if key == "sweep":
        ok, parsed = _as_switch(value)
        if ok:
            return True, parsed
        notes.append(f"{note}invalid checks.{name}.sweep value {value!r}; "
                     "current value kept")
        return False, None
    if key == "command":
        if isinstance(value, str):
            return True, value
        notes.append(f"{note}checks.{name}.command must be a string naming a "
                     "repo-relative executable; current value kept")
        return False, None
    notes.append(f"{note}unknown checks.{name} key \"{key}\" ignored")
    return False, None


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
