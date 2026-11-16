"""MR/PR context consult (pg2): read-only open-MR discovery + consistency checks.

Detector for the incident class "docs-only MR carrying an entire
implementation": for the branch being pushed, find the open MR/PR on the
push remote's forge, read what it DECLARES (labels, description prose,
recorded head), and cross-check that declaration against the computed diff.

All forge access is read-only and goes through the maintainer's existing
local CLI sign-in (same posture as rg_ticket_fetch / ocr_forge):

    github : gh pr list  --repo <owner/repo> --state open --head <branch>
                       --json number,title,body,labels,headRefOid,baseRefName,url
             gh pr view  <N> --repo <owner/repo> --json files   (overlap tier)
             gh pr list  --repo <owner/repo> --state open --limit 30
                       --json number,url                        (overlap tier)
    gitlab : glab api projects/:id/merge_requests?state=opened&source_branch=...
             glab api projects/:id/merge_requests?state=opened&per_page=30
             glab api projects/:id/merge_requests/<iid>/changes

The provider is resolved from the REMOTE URL (HTTPS or scp-style SSH, github
or gitlab hosts incl. self-hosted GitLab), never from the local repo config.

Caching: positive AND negative ("no open MR for this branch") results are
cached 24h under the gate state dir (rg_store.store_root()/mr-consult,
redirected by OCR_STATE_DIR in tests), keyed by (remote_url, branch). A fresh
cache hit costs no CLI call; an expired or corrupt entry refetches; transient
FAILURES are never cached (the next push retries).

Fail-open ALWAYS: unsupported forge, CLI missing, timeout (MR_TIMEOUT <= 8s),
non-zero exit, unparseable JSON, one entry raised -- everything degrades to
"no context" (None / empty). This module NEVER raises for those; consult
failure must be indistinguishable from "no MR context" to the caller
(spec: user story 25 -- infra failure is never a verdict and never a block).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import quote

import rg_store

MR_TTL = 24 * 3600
MR_TIMEOUT = 5          # per-CLI-call bound (<= the spec's 8s ceiling)
MAX_LIST_MRS = 30       # bound for the cross-MR overlap tier's list call

FINDING_PREFIX = "MR-CONSISTENCY:"


@dataclass
class MrContext:
    """What the forge declares about the open MR/PR for a branch."""
    iid: int
    title: str
    description: str
    labels: list = field(default_factory=list)
    head_sha: str = ""
    target_branch: str = ""
    web_url: str = ""


# --- provider resolution -------------------------------------------------------

_HTTP_RE = re.compile(r"^(?:https?|ssh|git)://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$")
_SCP_RE = re.compile(r"^(?:[^@/ ]+@)?([^:/]+):(.+)$")


def _provider(remote_url):
    """(provider, host, slug) for a remote URL, or None when unsupported.

    HTTPS/SSH GitHub -> ("github", host, "owner/repo"); gitlab.com and
    self-hosted GitLab (host contains 'gitlab') -> ("gitlab", ...); any other
    host has no supported CLI and is simply "no context".
    """
    url = str(remote_url or "").strip()
    if not url:
        return None
    m = _HTTP_RE.match(url) or _SCP_RE.match(url)
    if not m:
        return None
    host, path = m.group(1), m.group(2)
    if path.endswith(".git"):
        path = path[:-4]
    slug = path.strip("/")
    if not slug:
        return None
    host = host.lower()
    if "github" in host:
        return "github", host, slug
    if "gitlab" in host:
        return "gitlab", host, slug
    return None


# --- the CLI boundary -----------------------------------------------------------

def _sh_on_windows():
    """A POSIX sh for running .sh shims on Windows (test seam), else None."""
    for name in ("sh", "bash"):
        found = shutil.which(name)
        if found:
            return found
    git = shutil.which("git")
    if git:
        root = os.path.dirname(os.path.dirname(git))
        for rel in (("usr", "bin", "sh.exe"), ("..", "usr", "bin", "sh.exe")):
            cand = os.path.normpath(os.path.join(root, *rel))
            if os.path.isfile(cand):
                return cand
    return None


def _resolve_spawn(argv):
    """(list argv, None) or (None, note). Resolves the CLI on PATH; on
    Windows a `name.cmd` marker with a sibling `name.sh` (the conftest shim
    layout) runs through Git for Windows' sh, since CreateProcess cannot
    execute a bare shim name and cmd.exe would mangle api-path arguments."""
    exe = shutil.which(argv[0])
    if exe is None:
        return None, f"{argv[0]} not found on PATH"
    if exe.lower().endswith(".cmd") and os.path.isfile(exe[:-4] + ".sh"):
        sh = _sh_on_windows()
        if sh is not None:
            return [sh, exe[:-4] + ".sh"] + list(argv[1:]), None
    return [exe] + list(argv[1:]), None


def _run_cli(argv, env_extra=None, timeout=MR_TIMEOUT):
    """Run one read-only forge CLI lookup; return (stdout, note).

    stdout None with a note is the ONLY failure representation: CLI missing,
    timeout, non-zero exit, spawn failure all look the same. Never raises.
    """
    spawn, note = _resolve_spawn(argv)
    if spawn is None:
        return None, note
    env = dict(os.environ)
    env.update(env_extra or {})
    try:
        proc = subprocess.run(spawn, capture_output=True, text=True,
                              timeout=timeout, env=env)
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


def _json_list(raw):
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, list) else None


# --- live lookup -----------------------------------------------------------------

def _list_cmd(provider, slug, branch):
    if provider == "github":
        return ["gh", "pr", "list", "--repo", slug, "--state", "open",
                "--head", str(branch), "--limit", "1", "--json",
                "number,title,body,labels,headRefOid,baseRefName,url"]
    enc = quote(slug, safe="")
    return ["glab", "api",
            f"projects/{enc}/merge_requests?state=opened"
            f"&source_branch={quote(str(branch), safe='')}"]


def _ctx_from_gh(item):
    labels = [str(lab.get("name")) for lab in item.get("labels") or []
              if isinstance(lab, dict) and lab.get("name")]
    return MrContext(iid=int(item.get("number") or 0),
                     title=str(item.get("title") or ""),
                     description=str(item.get("body") or ""),
                     labels=labels,
                     head_sha=str(item.get("headRefOid") or ""),
                     target_branch=str(item.get("baseRefName") or ""),
                     web_url=str(item.get("url") or ""))


def _ctx_from_glab(item):
    labels = [str(lab) for lab in item.get("labels") or []]
    return MrContext(iid=int(item.get("iid") or 0),
                     title=str(item.get("title") or ""),
                     description=str(item.get("description") or ""),
                     labels=labels,
                     head_sha=str(item.get("sha") or ""),
                     target_branch=str(item.get("target_branch") or ""),
                     web_url=str(item.get("web_url") or ""))


def _live_lookup(remote_url, branch, runner, timeout):
    """(MrContext | None, note | None). (None, None) = searched, no open MR
    (cacheable negative); (None, note) = degraded, never cached."""
    parsed = _provider(remote_url)
    if parsed is None:
        return None, f"remote {remote_url!r} is neither GitHub nor GitLab"
    provider, host, slug = parsed
    env_extra = {}
    if provider == "gitlab" and host != "gitlab.com":
        env_extra["GITLAB_HOST"] = host
    out, note = runner(_list_cmd(provider, slug, branch),
                       env_extra=env_extra, timeout=timeout)
    if out is None:
        return None, note
    items = _json_list(out)
    if items is None:
        return None, f"{provider} open-MR lookup returned unrecognised JSON"
    if not items or not isinstance(items[0], dict):
        return None, None  # searched: no open MR for this branch
    build = _ctx_from_gh if provider == "github" else _ctx_from_glab
    try:
        return build(items[0]), None
    except Exception as exc:
        return None, f"{provider} open-MR payload was unusable ({exc})"


# --- the 24h cache (positives AND negatives) ---------------------------------------

def _cache_root(cache_dir=None):
    return Path(cache_dir) if cache_dir is not None \
        else rg_store.store_root() / "mr-consult"


def _cache_path(cache_dir, remote_url, branch):
    digest = hashlib.sha256(
        f"{remote_url}\x00{branch}".encode("utf-8", "replace")).hexdigest()[:24]
    return Path(cache_dir) / f"{digest}.json"


def _read_entry(path):
    """The raw cache entry dict, or None when absent/corrupt. TTL-neutral:
    find_open_mr applies the TTL, first_seen cares only about the baseline."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("kind") not in ("mr", "none"):
        return None
    if not isinstance(data.get("fetched_at"), (int, float)):
        return None
    if data["kind"] == "mr" and not isinstance(data.get("mr"), dict):
        return None
    return data


def _entry_context(entry):
    """MrContext from a 'mr' entry, validating every field type; None when
    the entry is corrupt (a corrupt positive is a miss, never a crash)."""
    mr = entry.get("mr") or {}
    try:
        return MrContext(iid=int(mr.get("iid") or 0),
                         title=str(mr.get("title") or ""),
                         description=str(mr.get("description") or ""),
                         labels=[str(lab) for lab in mr.get("labels") or []],
                         head_sha=str(mr.get("head_sha") or ""),
                         target_branch=str(mr.get("target_branch") or ""),
                         web_url=str(mr.get("web_url") or ""))
    except (TypeError, ValueError):
        return None


def _write_entry(path, now, ctx):
    """Best-effort cache write; a failure is silently fine (next push
    refetches). ctx None writes the negative sentinel."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"fetched_at": now,
                  "kind": "mr" if ctx is not None else "none",
                  "mr": asdict(ctx) if ctx is not None else None}
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    except (OSError, TypeError):
        pass


# --- the public seam -----------------------------------------------------------------

def find_open_mr(remote_url, branch, *, runner=None, cache_dir=None, now=None,
                 timeout=None):
    """The open MR/PR whose source branch is `branch`, or None.

    None means "no context": no MR, unsupported forge, missing CLI, offline,
    timeout -- all indistinguishable by design. 24h-cached in both directions
    (hits and negatives). Never raises.
    """
    try:
        runner = runner or _run_cli
        now = time.time() if now is None else now
        timeout = MR_TIMEOUT if timeout is None else timeout
        path = _cache_path(_cache_root(cache_dir), remote_url, branch)
        entry = _read_entry(path)
        if entry is not None and now - entry["fetched_at"] < MR_TTL:
            if entry["kind"] == "none":
                return None
            ctx = _entry_context(entry)
            if ctx is not None:
                return ctx
            # corrupt positive: fall through to a refetch
        ctx, note = _live_lookup(remote_url, branch, runner, timeout)
        if note is not None:
            return None
        _write_entry(path, now, ctx)
        return ctx
    except Exception:
        return None


def target_branch(remote_url, branch, *, runner=None, cache_dir=None, now=None,
                  timeout=None):
    """The MR's target branch for the base resolver, or None (no context)."""
    ctx = find_open_mr(remote_url, branch, runner=runner, cache_dir=cache_dir,
                       now=now, timeout=timeout)
    if ctx is None or not ctx.target_branch:
        return None
    return ctx.target_branch


def first_seen(remote_url, branch, *, runner=None, cache_dir=None, now=None,
               timeout=None):
    """True exactly when the cache holds a PRIOR NEGATIVE for (remote, branch)
    and a live lookup NOW finds an MR -- the retroactive discovery flag the
    caller announces as "MR first seen".

    An MR that was never looked up has no baseline (first lookup seeds the
    cache, returns False), and an already-known MR is not news. Degradation
    is False. Never raises.
    """
    try:
        runner = runner or _run_cli
        now = time.time() if now is None else now
        timeout = MR_TIMEOUT if timeout is None else timeout
        path = _cache_path(_cache_root(cache_dir), remote_url, branch)
        entry = _read_entry(path)
        if entry is not None and entry["kind"] == "mr":
            return False
        had_negative = entry is not None and entry["kind"] == "none"
        ctx, note = _live_lookup(remote_url, branch, runner, timeout)
        if note is not None:
            return False
        _write_entry(path, now, ctx)
        return had_negative and ctx is not None
    except Exception:
        return False


# --- consistency findings ------------------------------------------------------------

_DOCS_PHRASES = ("docs-only", "docs only", "documentation only",
                 "documentation-only", "only documentation", "only docs",
                 "a markdown file", "a single markdown file")

_NUMWORDS = {"a": 1, "an": 1, "single": 1, "one": 1, "two": 2, "three": 3,
             "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10}

_FILE_CLAIM_RE = re.compile(
    r"\b(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|single)"
    r"\s+(?:single\s+)?files?\b")
_LINE_CLAIM_RE = re.compile(r"\+?\s*(\d[\d,]*)\s+lines?\b")


def _is_docs_path(path):
    """Diff classification rule, stated plainly in every scope finding:
    paths under docs/** or ending in .md count as docs; all else is code."""
    p = str(path or "").replace("\\", "/").lstrip("./")
    return p == "docs" or p.startswith("docs/") or p.lower().endswith(".md")


def _docs_claim(ctx):
    """The basis for a docs-only declaration ('label "documentation"' or
    'description says "docs-only"'), or None."""
    for label in ctx.labels or []:
        low = str(label).strip().lower()
        if low == "documentation" or low.startswith("docs") or "documentation" in low:
            return f'label "{label}"'
    text = f"{ctx.title}\n{ctx.description}".lower()
    for phrase in _DOCS_PHRASES:
        if phrase in text:
            return f'description says "{phrase}"'
    return None


def _claimed_files(text):
    claimed = []
    for m in _FILE_CLAIM_RE.finditer(text):
        token = m.group(1)
        claimed.append(int(token) if token.isdigit() else _NUMWORDS[token])
    return min(claimed) if claimed else None


def _claimed_lines(text):
    claimed = [int(m.group(1).replace(",", ""))
               for m in _LINE_CLAIM_RE.finditer(text)]
    return min(claimed) if claimed else None


def consistency_findings(ctx, changed_paths, changed_lines, pushed_tip):
    """Actionable one-liners (each prefixed MR-CONSISTENCY:) comparing the
    MR's declaration to the pushed diff. Empty list = clean. Never raises.

    Checks: (1) declared docs-only scope vs a diff carrying non-docs paths;
    (2) claimed file/line counts vs actual; (3) the MR page's recorded head
    vs the tip being pushed (stale verdicts/comments cover the wrong SHA).
    """
    try:
        findings = []
        ref = f"MR {ctx.iid}" if ctx.iid else "the MR"
        paths = [str(p).replace("\\", "/") for p in (changed_paths or [])]

        basis = _docs_claim(ctx)
        if basis:
            contradicting = [p for p in paths if not _is_docs_path(p)]
            if contradicting:
                shown = contradicting[:5]
                extra = (f" (+{len(contradicting) - 5} more)"
                         if len(contradicting) > 5 else "")
                findings.append(
                    f"{FINDING_PREFIX} {ref} declares docs-only scope "
                    f"({basis}) but the diff touches {len(contradicting)} "
                    f"non-docs path(s): {', '.join(shown)}{extra} -- diff "
                    f"classification: paths under docs/** or ending in .md "
                    f"count as docs, everything else counts as code")

        text = f"{ctx.title}\n{ctx.description}".lower()
        claimed_files = _claimed_files(text)
        if claimed_files is not None and claimed_files != len(paths):
            findings.append(
                f"{FINDING_PREFIX} {ref} claims {claimed_files} file(s) but "
                f"the diff changes {len(paths)} file(s)")
        if isinstance(changed_lines, int) and changed_lines > 0:
            claimed_n = _claimed_lines(text)
            if claimed_n is not None and claimed_n != changed_lines:
                findings.append(
                    f"{FINDING_PREFIX} {ref} claims {claimed_n} changed "
                    f"line(s) but the diff changes {changed_lines} line(s)")

        head = (ctx.head_sha or "").strip().lower()
        tip = str(pushed_tip or "").strip().lower()
        if head and tip and head != tip:
            findings.append(
                f"{FINDING_PREFIX} {ref} recorded head {head[:12]} differs "
                f"from the pushed tip {tip[:12]} -- verdicts and comments on "
                f"the MR page do not cover the tip being pushed")
        return findings
    except Exception:
        return []


# --- cross-MR path overlap (large-diff tier, cheap, thresholded by the caller) -----

def _list_all_cmd(provider, slug):
    if provider == "github":
        return ["gh", "pr", "list", "--repo", slug, "--state", "open",
                "--limit", str(MAX_LIST_MRS), "--json", "number,url"]
    return ["glab", "api",
            f"projects/{quote(slug, safe='')}/merge_requests?state=opened"
            f"&per_page={MAX_LIST_MRS}"]


def _files_cmd(provider, slug, iid):
    if provider == "github":
        return ["gh", "pr", "view", str(iid), "--repo", slug, "--json", "files"]
    return ["glab", "api",
            f"projects/{quote(slug, safe='')}/merge_requests/{iid}/changes"]


def _mr_files(provider, raw):
    """Changed paths from a file-list payload, or None when unusable."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if provider == "github":
        files = data.get("files")
        if not isinstance(files, list):
            return None
        return [str(f["path"]) for f in files
                if isinstance(f, dict) and f.get("path")]
    changes = data.get("changes")
    if not isinstance(changes, list):
        return None
    return [str(c["new_path"]) for c in changes
            if isinstance(c, dict) and c.get("new_path")]


def cross_mr_path_overlap(remote_url, paths, exclude_iid=None, *, runner=None,
                          timeout=None):
    """{mr web_url: [overlapping paths]} for every OTHER open MR whose diff
    touches any of `paths` ("the whole series smuggled into one MR" is
    structurally visible here). The pushed MR's own iid is excluded (its file
    list is never fetched). Every CLI failure degrades to {} or a skipped MR.
    Never raises.
    """
    try:
        parsed = _provider(remote_url)
        if parsed is None:
            return {}
        provider, host, slug = parsed
        runner = runner or _run_cli
        timeout = MR_TIMEOUT if timeout is None else timeout
        wanted = {str(p).replace("\\", "/") for p in (paths or [])}
        if not wanted:
            return {}  # nothing to intersect: no fetches needed at all
        env_extra = {}
        if provider == "gitlab" and host != "gitlab.com":
            env_extra["GITLAB_HOST"] = host

        out, note = runner(_list_all_cmd(provider, slug),
                           env_extra=env_extra, timeout=timeout)
        if out is None:
            return {}
        items = _json_list(out)
        if items is None:
            return {}

        overlap = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            iid = item.get("number") if provider == "github" else item.get("iid")
            if iid is None:
                continue
            if exclude_iid is not None and str(iid) == str(exclude_iid):
                continue
            key = str(item.get("url") or item.get("web_url")
                      or f"{'#' if provider == 'github' else '!'}{iid}")
            out, note = runner(_files_cmd(provider, slug, iid),
                               env_extra=env_extra, timeout=timeout)
            if out is None:
                continue  # one MR's fetch failing never sinks the check
            files = _mr_files(provider, out)
            if files is None:
                continue
            shared = sorted(wanted & {f.replace("\\", "/") for f in files})
            if shared:
                overlap[key] = shared
        return overlap
    except Exception:
        return {}
