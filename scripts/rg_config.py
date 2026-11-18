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
                      ticket); a command check also takes "parallel"
                      (default False -- its executor may run concurrently
                      against its own tip clone; SPEC-15.1), "required"
                      (default False -- opt in to final-review required
                      verification without adding a pre-push duty), and
                      "timeout" (seconds, default 60 -- its answer budget). Command
                      checks may define new check names; other unknown names
                      are typos and are ignored.

Missing files degrade to defaults silently -- that is the normal state for
most installs. Unreadable/invalid files, and invalid VALUES for known keys,
degrade to defaults WITH A NOTE in "notes" (a note is for a human reading a
report or a doctor run; the gate itself never fails on config).

Second surface (pg1): the user-level RUN configuration the gate consults
when deciding how a push is reviewed:

    load_config() -> GateConfig(depth, timeout_seconds, fail_open, warnings)

File: ~/.config/review-gate/config.json (override with OCR_CONFIG_FILE).
Shape: {"review": {"depth": ..., "timeoutSeconds": ..., "failOpen": ...}}.
Keys: review.depth ("full"|"structural"|"large-diff-scoped", default
"full"), review.timeoutSeconds (int >= 0, 0 = no wall-clock cap, default 0),
review.failOpen (bool, default false -- the gate is fail-CLOSED by default).
Precedence, key by key: env var > config file > built-in default; an invalid
value at a higher level is warned about and the next level is consulted, so
one bad key never masks a good one beneath it.

Environment switches this module reads (switches are decisions; ADR 0006
cites this list):

    OCR_CONFIG_FILE             (pg1) override for the run-config file
                                location; tests and hermetic installs point
                                it at a private path.
    OCR_REVIEW_DEPTH            (pg1) override for review.depth.
    OCR_REVIEW_TIMEOUT_SECONDS  (pg1) override for review.timeoutSeconds.
    OCR_FAIL_OPEN               (pre-dates pg1: the gate's documented
                                one-shot escape hatch -- "1"/"true"/"yes"
                                per review-gate.py) overrides
                                review.failOpen.
    (OCR_STATE_DIR is read by rg_store, whose store_root() locates the
    settings surface above.)

This module reads config; it never writes it.
"""
import json
import os
from dataclasses import dataclass, field
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
# run is the execution ticket's call (SPEC-14.4). A command check also
# declares its execution posture (SPEC-15.1): "parallel" says its executor
# may run concurrently against its own tip clone (scheduling lands in
# SPEC-15.3), and "timeout" pins its answer budget -- 60 s, the budget the
# gate already gives every command check (_COMMAND_CHECK_TIMEOUT_S).
_COMMAND_CHECK_DEFAULTS = {"enabled": True, "mode": "block", "globs": [],
                            "parallel": False, "required": False, "timeout": 60}


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
    if key in ("parallel", "required"):
        ok, parsed = _as_switch(value)
        if ok:
            return True, parsed
        notes.append(f"{note}invalid checks.{name}.{key} value {value!r}; "
                     "current value kept")
        return False, None
    if key == "timeout":
        # Command-check answer budget in seconds (SPEC-15.1). Numeric and
        # positive; a positive value under one second is floored to one the
        # same way the env-int timeouts floor (max(floor, val)) -- a budget
        # under a second is a disallow, which no positive config value means.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            notes.append(f"{note}checks.{name}.timeout must be a number of "
                         "seconds; current value kept")
            return False, None
        if value <= 0:
            notes.append(f"{note}checks.{name}.timeout must be a positive "
                         "number of seconds; current value kept")
            return False, None
        return True, max(1, value)
    if key == "command":
        if isinstance(value, str):
            return True, value
        notes.append(f"{note}checks.{name}.command must be a string naming a "
                     "repo-relative executable; current value kept")
        return False, None
    notes.append(f"{note}unknown checks.{name} key \"{key}\" ignored")
    return False, None


def _as_switch(value):
    """(ok, bool). Accepts booleans, 0/1, and on/off-style words. A null
    (explicit JSON null for review.failOpen) is an invalid value, not a
    missing one -- the caller distinguishes with _MISSING."""
    if value is None:
        return False, None
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


# --- the user-level run configuration (pg1) ------------------------------------

REVIEW_DEPTHS = ("full", "structural", "large-diff-scoped")

_MISSING = object()  # a key/env var that is absent -- distinct from a null value


@dataclass(frozen=True)
class GateConfig:
    """The resolved run configuration for one gate invocation.

    warnings holds human-readable advisories about degraded config (bad
    JSON, invalid values) for the gate to print; nothing here ever raises
    on bad input -- the gate's posture is set by fail_open, not by config
    file health."""
    depth: str
    timeout_seconds: int
    fail_open: bool
    warnings: list = field(default_factory=list)


def user_config_path():
    """The run-config file: ~/.config/review-gate/config.json unless
    OCR_CONFIG_FILE redirects it (pathlib only -- no POSIX assumptions)."""
    override = os.environ.get("OCR_CONFIG_FILE", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".config" / "review-gate" / "config.json"


def load_config():
    """The resolved run configuration plus advisory warnings.

    Precedence, key by key: env var > config file > built-in default.
    Missing files and absent keys are silent; unreadable files, malformed
    JSON, and invalid values add a warning and fall to the next level (or
    the default).
    """
    path = user_config_path()
    warnings = []
    review = _read_review_block(path, warnings)
    return GateConfig(
        depth=_resolve(warnings, "review.depth", "full", _as_depth,
                       ("env OCR_REVIEW_DEPTH", _env("OCR_REVIEW_DEPTH")),
                       (f"user config {path}", review.get("depth", _MISSING))),
        timeout_seconds=_resolve(warnings, "review.timeoutSeconds", 0, _as_timeout,
                                 ("env OCR_REVIEW_TIMEOUT_SECONDS",
                                  _env("OCR_REVIEW_TIMEOUT_SECONDS")),
                                 (f"user config {path}",
                                  review.get("timeoutSeconds", _MISSING))),
        fail_open=_resolve(warnings, "review.failOpen", False, _as_switch,
                           ("env OCR_FAIL_OPEN", _env("OCR_FAIL_OPEN")),
                           (f"user config {path}",
                            review.get("failOpen", _MISSING))),
        warnings=warnings,
    )


def _env(name):
    """An env override's value, or _MISSING when unset/blank (blank is unset)."""
    value = os.environ.get(name, "").strip()
    return value if value else _MISSING


def _resolve(warnings, key, default, parse, *levels):
    """First valid value across (label, raw) precedence levels; invalid
    values warn and fall through; default when nothing valid appears. An
    explicit JSON null is an invalid value (warned), not an absent key."""
    for label, raw in levels:
        if raw is _MISSING:
            continue
        ok, value = parse(raw)
        if ok:
            return value
        warnings.append(f"{label}: invalid {key} value {raw!r}; ignored")
    return default


def _read_review_block(path, warnings):
    """The "review" object of the run-config file, or {} when absent or
    unusable (with a warning unless the file simply does not exist)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        warnings.append(f"user config {path} unreadable ({exc}); "
                        "defaults used for its values")
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        warnings.append(f"user config {path} is not valid JSON; "
                        "defaults used for its values")
        return {}
    if not isinstance(data, dict):
        warnings.append(f"user config {path} is not a JSON object; "
                        "defaults used for its values")
        return {}
    review = data.get("review")
    if review is None:
        return {}
    if not isinstance(review, dict):
        warnings.append(f'user config {path}: "review" must be an object; '
                        "its values ignored")
        return {}
    return review


def _as_depth(value):
    """(ok, depth). Depths are exact strings; no case-folding."""
    if isinstance(value, str) and value in REVIEW_DEPTHS:
        return True, value
    return False, None


def _as_timeout(value):
    """(ok, seconds). ints >= 0 from the file (booleans refused); integer
    strings from the env. 0 means no cap."""
    if isinstance(value, bool):
        return False, None
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            return False, None
    if isinstance(value, int) and value >= 0:
        return True, value
    return False, None
