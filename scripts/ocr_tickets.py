"""Ticket key discovery (Claude port of the Kimchi variant's rg_tickets).

Discovery only -- no fetching, no network, no model (ocr_ticket_fetch
fetches). Given the resolved gate settings (ocr_intent_config.load_settings
is the single settings read), the current branch name, and the pushed
commits' messages, produce an ordered, provenance-labelled list of issue
keys:

    1. explicit per-repo override key (intent.ticket in the repo's
       .ocr/config.json -- the file ocr_intent_config already loads) --
       direct; when valid it wins outright over the branch name
    2. key(s) in the branch name                       -- direct
    3. key(s) in commit message text                   -- background

At most CAP keys are selected for fetching; the rest are kept as an
ID-only list ("extras"). A review never fails on discovery: any oddity is
a human-readable note and discovery continues with what it has.

Result shape (pure data, JSON-friendly, for the fetch stage and the
manifest extras seam):

    {"selected": [{"key": "PROJ-123", "source": "branch",
                   "role": "direct"}, ...],
     "extras":   ["#45", ...],
     "notes":    [...]}

When intent.tickets is off (the default), discovery returns nothing.
"""
import re

CAP = 3  # settled: fetch at most 3 tickets; the rest are listed by ID only


# Realistic key shapes:
#   Jira-style   PROJ-123 -- an UPPERCASE project tag, so "utf-8"/"json-2"
#                  never match; nothing word-like may be glued on either side
#   issue-style  #123     -- the GitHub/GitLab form, kept verbatim with '#'
_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:([A-Z][A-Z0-9]+-\d+)|#(\d+))")


def extract_keys(text):
    """Ordered, deduped issue keys found in free text (branch name or
    commit message). First appearance wins; later repeats are skipped."""
    keys = []
    for match in _KEY_RE.finditer(text or ""):
        key = match.group(1) or f"#{match.group(2)}"
        if key not in keys:
            keys.append(key)
    return keys


def discover_ticket_keys(settings, branch=None, commit_messages=None, cap=CAP):
    """The ordered, provenance-labelled ticket keys for one push-review.

    settings is the single settings read (ocr_intent_config.load_settings
    output). branch is the current branch name; commit_messages the pushed
    commits' messages (subjects + bodies) in push order. Both are plain text
    the caller already has -- discovery stays pure.

    Returns {"selected": [{key, source, role}, ...], "extras": [key, ...],
    "notes": [...]}. Selected holds at most cap keys in precedence order
    (override, then branch, then commits); everything beyond the cap is in
    extras as bare key IDs. When intent.tickets is off, all lists are
    empty.
    """
    if not settings or not settings.get("intent", {}).get("tickets"):
        return {"selected": [], "extras": [], "notes": []}
    notes = []
    ordered = []  # (key, source, role) in precedence order, deduped by key

    def _add(key, source, role):
        if key not in [k for k, _, _ in ordered]:
            ordered.append((key, source, role))

    override = settings["intent"].get("ticket")
    if override is not None:
        direct = extract_keys(override)
        if direct:
            _add(direct[0], "override", "direct")  # the override wins
        else:
            notes.append(f"override ticket {override!r} is not a recognisable "
                         "issue key; ignored")
            override = None  # fall through to the branch name
    if override is None:
        for key in extract_keys(branch):
            _add(key, "branch", "direct")
    for message in commit_messages or ():
        for key in extract_keys(message):
            _add(key, "commit", "background")

    selected = [{"key": key, "source": source, "role": role}
                for key, source, role in ordered[:cap]]
    extras = [key for key, _, _ in ordered[cap:]]
    return {"selected": selected, "extras": extras, "notes": notes}
