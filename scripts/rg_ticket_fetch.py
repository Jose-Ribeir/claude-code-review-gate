"""Linked-ticket fetch (t17): title + description only, read-only, fail-open.

For the keys discovery selected (rg_tickets.discover_ticket_keys), the
supervisor fetches each ticket's title and description -- NEVER comments,
never any write operation -- through the maintainer's existing local CLI
sign-in:

    github : gh issue view <N> --json title,body
             (falls back to gh pr view <N> when the key is #N and the issue
             lookup fails)
    gitlab : glab api projects/:fullpath/issues/<N>
             (glab substitutes :fullpath from the current repo's remote)
    jira   : a seam only -- the real backend is t18's side-fetch worker; the
             slot exists so tracker_order's "jira" entry yields a clean,
             human-readable miss note instead of an unknown-tracker fall-out
             (networkless supervisors keep their strict no-fetch posture).

Which backends are tried for a key, and in which order, comes from the
settings' intent.tracker_order (default github, gitlab, jira); a backend is
only tried for key shapes it can resolve (#N for gh/glab, Jira-style keys
for jira). Successful fetches are cached in the gate's state directory
(store_root()/tickets/, same conventions as rg_store) for 24 hours, keyed by
tracker+key: a hit avoids re-fetching, an expired or corrupt entry refetches,
and a miss is never cached (a transient VPN outage must be retried on the
next push).

Fail-open ALWAYS: CLI missing, network down, timeout (a few seconds),
non-zero exit, unexpected JSON, one backend raised -- everything is a silent
skip with a human-readable note in the result; this module NEVER raises for
those, and even an internal exception becomes a note, because a review must
proceed on commit messages alone.

Result shape (pure data, JSON-friendly, for the manifest extras seam):

    {"tickets": [{"key", "tracker", "source", "role", "title", "body",
                  "truncated"}],
     "notes":   [...]}

Descriptions are kept small: past DESCRIPTION_MAX_CHARS the body is hard-
truncated with a visible marker.
"""
import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

import rg_store

# 24h inherits the spec's "retries and re-pushes review instantly"; the few
# seconds of fetch timeout bounds how long a dead VPN can stall a push.
TICKET_TTL = 24 * 3600
FETCH_TIMEOUT = 5
DESCRIPTION_MAX_CHARS = 4000
_TRIM_SUFFIX = ("\n[... description truncated at "
                f"{DESCRIPTION_MAX_CHARS} characters ...]")

_JIRA_KEY = re.compile(r"[A-Z][A-Z0-9]+-\d+\Z")


# --- the CLI boundary ----------------------------------------------------------

def _run_cli(argv, cwd, timeout):
    """Run one read-only tracker CLI lookup; return (stdout, note).

    stdout None with a note is the ONLY failure representation: the CLI
    missing from PATH, a timeout, a non-zero exit, or the process not being
    runnable at all all look the same to the caller. Never raises.
    """
    try:
        proc = subprocess.run(argv, cwd=cwd or None, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"{argv[0]} timed out after {timeout}s"
    except FileNotFoundError:
        return None, f"{argv[0]} not found on PATH"
    except OSError as exc:
        return None, f"{argv[0]} could not run ({exc})"
    if proc.returncode != 0:
        first_err = (proc.stderr or "").strip().splitlines()
        detail = f" ({first_err[0][:160]})" if first_err else ""
        return None, (f"{' '.join(str(a) for a in argv)} exited "
                      f"{proc.returncode}{detail}")
    return proc.stdout, None


def _json_object(raw):
    """Parse CLI-JSON output into a dict, else None (never parses to a note:
    the caller supplies the human-readable miss text)."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --- backends ------------------------------------------------------------------

def _ticket_payload(title, body):
    return {"title": title if isinstance(title, str) and title else "",
            "body": body if isinstance(body, str) else ""}


def _gh_backend(key, repo_root, runner):
    """GitHub: issue first, PR fallback when the issue lookup fails."""
    number = key[1:]
    out, note = runner(["gh", "issue", "view", number, "--json", "title,body"],
                       repo_root, FETCH_TIMEOUT)
    misses = []
    if out is not None:
        data = _json_object(out)
        if data is not None:
            return _ticket_payload(data.get("title"), data.get("body")), None
        misses.append(f"gh issue view #{number} returned unrecognised JSON")
    else:
        misses.append(note)
    # PR fallback: a #N key can be a PR number (spec's deterministic fallback).
    out, note = runner(["gh", "pr", "view", number, "--json", "title,body"],
                       repo_root, FETCH_TIMEOUT)
    if out is not None:
        data = _json_object(out)
        if data is not None:
            return _ticket_payload(data.get("title"), data.get("body")), None
        misses.append(f"gh pr view #{number} returned unrecognised JSON")
    else:
        misses.append(note)
    return None, "; ".join(m for m in misses if m)


def _glab_backend(key, repo_root, runner):
    """GitLab: the current repo project's issue via glab api (:fullpath is
    substituted by glab from the repo's remote). description -> body."""
    number = key[1:]
    out, note = runner(["glab", "api", f"projects/:fullpath/issues/{number}"],
                       repo_root, FETCH_TIMEOUT)
    if out is None:
        return None, note
    data = _json_object(out)
    if data is None:
        return None, f"glab api issue #{number} returned unrecognised JSON"
    return _ticket_payload(data.get("title"), data.get("description")), None


def _jira_backend(key, repo_root, runner):
    """The jira seam: ticket fetching for Jira is t18's side-fetch worker
    (the review worker keeps its strict no-MCP/no-network posture). The slot
    sits here so a "jira" entry in tracker_order produces a clean miss note
    rather than an unknown-tracker surprise."""
    return None, (f"jira fetch for {key} skipped: the jira backend is t18's "
                  "side-fetch worker, not this supervisor fetch")


_BACKENDS = {"github": _gh_backend, "gitlab": _glab_backend, "jira": _jira_backend}


def _applies(tracker, key):
    """Whether a tracker backend can resolve this key shape at all. gh/glab
    only know repo issue/PR numbers; jira only project keys."""
    if key.startswith("#"):
        return tracker in ("github", "gitlab")
    return tracker == "jira" and _JIRA_KEY.match(key) is not None


# --- the 24h cache --------------------------------------------------------------

def _cache_path(cache_dir, tracker, key):
    name = f"{quote(tracker, safe='')}--{quote(key, safe='')}.json"
    return Path(cache_dir) / name


def _cache_read(cache_dir, tracker, key, now):
    """The cached {title, body} when fresh, else None. Corrupt/odd entries
    and expired TTLs are plain misses -- the next fetch rewrites them."""
    try:
        data = json.loads(_cache_path(cache_dir, tracker, key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    fetched_at = data.get("fetched_at")
    if not isinstance(fetched_at, (int, float)) or now - fetched_at >= TICKET_TTL:
        return None
    if not isinstance(data.get("title"), str) or not isinstance(data.get("body"), str):
        return None
    return {"title": data["title"], "body": data["body"]}


def _cache_write(cache_dir, tracker, key, payload, now):
    """Best-effort success cache; a failure to write is silently fine (the
    next push simply refetches). Misses are never cached: a transient outage
    must be retried."""
    try:
        path = _cache_path(cache_dir, tracker, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_at": now,
                                    "title": payload["title"],
                                    "body": payload["body"]},
                                   ensure_ascii=False),
                        encoding="utf-8")
    except OSError:
        pass


# --- the seam -------------------------------------------------------------------

def _fetch_one(key, order, repo_root, runner, cache_dir, now):
    """(tracker, payload) on success, else (None, note). Tries applicable
    backends in tracker_order; cache is consulted per tracker+key before the
    CLI is invoked, and refreshed on success."""
    applicable = [t for t in order if _applies(t, key)]
    if not applicable:
        return None, (f"no tracker in tracker_order ({', '.join(order)}) "
                      f"can resolve key {key}")
    misses = []
    for tracker in applicable:
        cached = _cache_read(cache_dir, tracker, key, now)
        if cached is not None:
            return tracker, cached
        backend = _BACKENDS[tracker]
        try:
            payload, note = backend(key, repo_root, runner)
        except Exception as exc:  # the CLI boundary itself must never escape
            payload, note = None, f"{tracker} fetch for {key} failed ({exc})"
        if payload is not None:
            _cache_write(cache_dir, tracker, key, payload, now)
            return tracker, payload
        if note:
            misses.append(f"{tracker}: {note}")
    return None, "; ".join(misses) or "no tracker could fetch it"


def _trim(body):
    """Hard-truncate a giant ticket description with a visible marker."""
    truncated = len(body) > DESCRIPTION_MAX_CHARS
    if truncated:
        body = body[:DESCRIPTION_MAX_CHARS].rstrip() + _TRIM_SUFFIX
    return body, truncated


def fetch_tickets(settings, discovered, repo_root=None, runner=None,
                  cache_dir=None, now=None):
    """Fetch title+description for the discovery-selected keys.

    settings is the single settings read; when intent.tickets is off (the
    default) this returns an empty result without touching the CLI. runner
    is the CLI boundary (default _run_cli): (argv, cwd, timeout) ->
    (stdout, note). cache_dir defaults to store_root()/tickets. now is
    injectable for TTL tests. NEVER raises: any exception becomes a note.
    """
    empty = {"tickets": [], "notes": []}
    try:
        if (not settings or not settings.get("intent", {}).get("tickets")
                or not discovered):
            return dict(empty)
        selected = discovered.get("selected") or ()
        if not selected:
            return dict(empty)
        runner = runner or _run_cli
        cache_dir = cache_dir if cache_dir is not None else rg_store.store_root() / "tickets"
        now = time.time() if now is None else now
        order = settings["intent"].get("tracker_order") or list(_BACKENDS)
        tickets, notes = [], []
        for sel in selected:
            if not isinstance(sel, dict) or not sel.get("key"):
                continue
            key = sel["key"]
            tracker, note_or_none = _fetch_one_safe(
                key, order, repo_root, runner, cache_dir, now)
            if tracker is None:
                notes.append(f"{key}: {note_or_none}")
                continue
            body, truncated = _trim(note_or_none["body"])
            tickets.append({"key": key, "tracker": tracker,
                            "source": sel.get("source"), "role": sel.get("role"),
                            "title": note_or_none["title"], "body": body,
                            "truncated": truncated})
        return {"tickets": tickets, "notes": notes}
    except Exception as exc:  # fail-open invariant: a note, never an exception
        return {"tickets": [], "notes": [f"ticket fetching skipped: {exc}"]}


def _fetch_one_safe(key, order, repo_root, runner, cache_dir, now):
    """_fetch_one with its own fail-open guard so one tracker bug cannot
    sink the whole enrichment."""
    try:
        return _fetch_one(key, order, repo_root, runner, cache_dir, now)
    except Exception as exc:
        return None, f"fetch failed ({exc})"
