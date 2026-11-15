"""Jira side-fetch worker (t18, Kimchi variant only).

Jira tickets cannot ride gh/glab: they are fetched through the desktop
app's EXISTING Atlassian login, so no token is ever stored in gate config
(spec: the Jira path rides the desktop login by design). The review
worker's strict no-MCP posture stays untouched -- its --mcp-config keeps
pointing at an empty config. Instead, when discovery selected a
Jira-style key (PROJ-123), this module performs ONE tiny separate
headless invocation:

    kimchi -p <minimal prompt> --no-session --mcp-config <temp file>

whose temp --mcp-config enables ONLY the persisted Atlassian connector
entry (`__kimchi-studio-connector__atlassian`) read verbatim from the
harness config on disk -- ~/.config/kimchi/harness/mcp.json, overridable
with OCR_JIRA_MCP_CONFIG (tests must never read a developer's real
connector config). That entry carries the MCP_REMOTE_CONFIG_DIR pointing
at the desktop OAuth cache, so the run reuses the app's sign-in. The
prompt's only job is the issue's summary + description -- NEVER comments
-- emitted in a contracted, parseable form:

    TITLE: <one-line summary>
    DESCRIPTION:
    <the plain-text description, possibly multi-line>

Coordinates and knobs come from the settings' "jira" block
(rg_config.load_settings): "server" (the Atlassian site URL, named in the
prompt), "project" (optional context), and "timeout" (seconds, parsed
charitably; an unmappable value falls back to SIDE_FETCH_TIMEOUT).

Fail-open ALWAYS: no config file, no Atlassian entry, unreadable/invalid
JSON, CLI missing, timeout (SIDE_FETCH_TIMEOUT seconds), non-zero exit,
unparseable output, auth failure, even a raising runner -- everything is
(None, note); this module NEVER raises. Misses are never cached here:
caching lives in rg_ticket_fetch (t17's shared 24h cache), and this is
only the backend that slot delegates to.
"""
import json
import os
import tempfile

# The persisted Studio connector entry key in the harness mcp.json -- this
# is the ONLY server the side-fetch --mcp-config may enable.
CONNECTOR_KEY = "__kimchi-studio-connector__atlassian"

# A headless spawn plus the mcp-remote handshake needs more than t17's
# CLI-boundary seconds, but a dead VPN or an expired login still bounds the
# stall on a push.
SIDE_FETCH_TIMEOUT = 30

_PROMPT = """Fetch the Jira issue {key} from {site} using the Atlassian MCP connector.
Return ONLY the issue's summary (title) and description. NEVER fetch
comments. Do not call any other tool. Do not create, edit, or transition
anything -- this is a strictly read-only lookup.

Reply with EXACTLY this format and nothing else around it:

TITLE: <the issue summary, one line>
DESCRIPTION:
<the issue description as plain text, or EMPTY when there is none>"""


def harness_mcp_config_path():
    """The persisted harness MCP config. OCR_JIRA_MCP_CONFIG overrides it
    (tests point at a fixture; the real default is the Kimchi harness's
    on-disk config, which carries the desktop Atlassian connector)."""
    override = os.environ.get("OCR_JIRA_MCP_CONFIG", "").strip()
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".config", "kimchi",
                        "harness", "mcp.json")


def _kimchi_bin():
    """The headless CLI for the side-fetch: KIMCHI_BIN, else `kimchi` on
    PATH -- the same convention the review worker uses."""
    return os.environ.get("KIMCHI_BIN", "").strip() or "kimchi"


def _connector_entry(config_path):
    """(entry, note): the persisted Atlassian connector entry read VERBATIM
    from the harness config, or None plus a human-readable note when the
    file or the entry is absent/unusable. Never raises."""
    try:
        with open(config_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None, (f"no persisted harness MCP config at {config_path} "
                      f"(the side-fetch reuses the desktop Atlassian "
                      "connector; connect it in the app first)")
    except (OSError, ValueError) as exc:
        return None, f"harness MCP config {config_path} unreadable ({exc})"
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    entry = servers.get(CONNECTOR_KEY) if isinstance(servers, dict) else None
    if not isinstance(entry, dict):
        return None, (f"no persisted Atlassian connector entry "
                      f"({CONNECTOR_KEY}) in {config_path}")
    return entry, None


def _parse_reply(raw):
    """(title, body) or None: the contracted TITLE:/DESCRIPTION: reply,
    or None when the worker's output cannot be mapped to it. A missing
    title is a miss too (an empty ticket title is not context worth
    injecting)."""
    if not isinstance(raw, str):
        return None
    lines = raw.splitlines()
    title = None
    marker = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if title is None and stripped.startswith("TITLE:"):
            title = stripped[len("TITLE:"):].strip()
        elif title is not None and (stripped == "DESCRIPTION:"
                                    or stripped.startswith("DESCRIPTION:")):
            marker = i
            break
    if not title or marker is None:
        return None
    body = "\n".join(lines[marker + 1:]).strip()
    return (title, "" if body in ("", "EMPTY") else body)


def _timeout(jira_settings):
    """SIDE_FETCH_TIMEOUT unless the jira block pins a positive number."""
    raw = (jira_settings or {}).get("timeout")
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return SIDE_FETCH_TIMEOUT
    return value if value > 0 else SIDE_FETCH_TIMEOUT


def _prompt(key, jira_settings):
    site = (jira_settings or {}).get("server", "").strip()
    if not site:
        site = "the connected Atlassian site"
    return _PROMPT.format(key=key, site=site)


def fetch_issue(key, jira_settings, repo_root, runner):
    """(payload, note): the issue's {"title", "body"} on success, else
    (None, human-readable note). This is the backend for rg_ticket_fetch's
    jira slot: by the time we run, settings allow tickets and the key is
    Jira-style; the remaining gates -- a connector config exists on disk
    and the persisted Atlassian entry is in it -- are checked here.

    runner is the same CLI boundary t17 uses: (argv, cwd, timeout) ->
    (stdout, note), which already collapses spawn failure / timeout /
    non-zero exit into (None, note). NEVER raises.
    """
    try:
        entry, note = _connector_entry(harness_mcp_config_path())
        if entry is None:
            return None, note
        # ONLY the Atlassian connector -- never the harness's other servers,
        # never the reviewer's zeroed config.
        side_config = {"mcpServers": {CONNECTOR_KEY: entry}}
        fd, tmp_path = tempfile.mkstemp(prefix="ocr-jira-mcp-",
                                        suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(side_config, fh)
            argv = [_kimchi_bin(), "-p", _prompt(key, jira_settings),
                    "--no-session", "--mcp-config", tmp_path]
            out, note = runner(argv, repo_root, _timeout(jira_settings))
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        if out is None:
            return None, note
        parsed = _parse_reply(out)
        if parsed is None:
            return None, (f"jira side-fetch for {key} returned output that "
                          "did not match the TITLE:/DESCRIPTION: contract")
        title, body = parsed
        return {"title": title, "body": body}, None
    except Exception as exc:  # fail-open invariant: a note, never an exception
        return None, f"jira side-fetch for {key} failed ({exc})"
