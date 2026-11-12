#!/usr/bin/env python3
#
# Pointer resolution + forge lookup (t09): where does a prose-pointer target
# come from?
#
# The t06 event index surfaces pointer utterances (string literals naming
# report/doc-like artifacts); the t05 collectors hand each target to the
# ``pointer_resolver`` seam. This module builds that resolver. Resolution
# tiers, in order (spec: "Pointer resolution and forge lookup"):
#
#   1. committed registry ``.ocr/artifacts.json`` -- a project-maintained map
#      ``{name: {produced_by, path, mr?}}`` (see docs/pointer-artifact-registry.md);
#   2. repo-wide ``git grep`` for the target's basename (working tree: the
#      producer need not be committed). Hits that are the target's OWN
#      utterance sites (per the t06 index) are excluded: a dangling pointer
#      is one spoken somewhere but produced/registered nowhere, and grepping
#      the utterance back would mask exactly that;
#   3. sibling branch tips -- ``git grep`` on local branch refs, bounded to
#      MAX_BRANCHES branches (a renamed artifact living on a sibling MR);
#   4. live forge call -- ``gh`` or ``glab`` autodetected from the origin
#      remote (github.com vs gitlab.com / self-hosted GitLab), with a HARD
#      3-second subprocess timeout, permitted in the blocking lane as an
#      accepted, aggressively-bounded stall risk. Missing CLI, missing
#      remote, auth failures and offline all degrade to tiers 1-3 with a
#      warning; negatives are cached in-process and on disk
#      (.ocr/index/forge_negcache.json, bounded) so a degraded forge costs
#      the next calls nothing.
#
# Resolution dict contract (consumed by the collectors bundle; the reviewer
# cites ``unresolved`` as a medium finding -- this module never fabricates
# findings):
#
#   {"status": "resolved" | "unresolved" | "unavailable",
#    "via": "registry" | "grep" | "branch" | "forge" | "none",
#    "warning": <degradation note, if any>} + tier evidence:
#   registry -> produced_by, path, mr?      grep -> hits [{path, line}...]
#   branch   -> branches [...]              forge -> hits [...]
#
#   "resolved"   -- a tier located the artifact; ``via`` names which one.
#   "unresolved" -- every tier that could run ran and nothing matched
#                   (the reviewer's medium-finding case).
#   "unavailable"-- the forge tier could not execute (no remote/CLI/auth/
#                   timeout) so coverage is local-only; a warning says why.
#
# Fail-soft: the returned resolver never raises; internal failures degrade to
# "unavailable" with a warning. A review must never break on pointer lookup.
#
import json
import os
import re
import shutil
import subprocess
import time

import ocr_impact
import ocr_index

FORGE_TIMEOUT = 3.0          # hard per-call subprocess bound (spec)
MAX_GREP_HITS = 10
MAX_BRANCHES = 8
MAX_FORGE_HITS = 5
MAX_NEGCACHE = 200           # persisted negative-cache bound
NEGCACHE_TTL = 86400.0       # seconds; older negatives are retried

_REGISTRY_REL = ".ocr/artifacts.json"
_NEGCACHE_REL = os.path.join(".ocr", "index", "forge_negcache.json")


def _git_runner(repo_root):
    """run(args) -> (stdout, rc); never raises (mirrors ocr_impact.git_runner).

    Kimchi's per-session GIT_TRACE* vars are scrubbed (see conftest.py):
    they point at temp dirs that vanish, and any git inheriting them dies
    with exit 128."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE")}
    env["GIT_LITERAL_PATHSPECS"] = "1"

    def run(args):
        try:
            result = subprocess.run(
                ["git"] + args, cwd=repo_root, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30, env=env)
            return result.stdout, result.returncode
        except Exception:
            return "", 1

    return run


# ---------------------------------------------------------------------------
# Target-name matching (mirrors the collectors' loose pointer match)
# ---------------------------------------------------------------------------

def _name_matches(query, candidate):
    """Basename/stem match either way (shared with the collectors' loose
    pointer match; single implementation lives in ocr_impact.name_matches)."""
    return ocr_impact.name_matches(query, candidate)


def _basename(target):
    return str(target or "").replace("\\", "/").rsplit("/", 1)[-1].strip()


# ---------------------------------------------------------------------------
# Tier 1: committed registry .ocr/artifacts.json
# ---------------------------------------------------------------------------

def _registry_lookup(repo_root, target, warnings):
    full = os.path.join(repo_root, _REGISTRY_REL.replace("/", os.sep))
    try:
        with open(full, "r", encoding="utf-8") as fh:
            registry = json.load(fh)
    except FileNotFoundError:
        return None
    except (ValueError, OSError) as exc:
        warnings.append("forge: artifact registry %s unreadable (%s); tier skipped"
                        % (_REGISTRY_REL, exc))
        return None
    if not isinstance(registry, dict):
        warnings.append("forge: artifact registry %s is not an object; tier skipped"
                        % _REGISTRY_REL)
        return None
    matches = []
    for name, meta in registry.items():
        if not _name_matches(target, name):
            continue
        if isinstance(meta, dict):
            matches.append((name, meta))
    if not matches:
        return None
    name, meta = matches[0]
    result = {"status": "resolved", "via": "registry",
              "produced_by": str(meta.get("produced_by", "")),
              "path": str(meta.get("path", name))}
    if meta.get("mr"):
        result["mr"] = meta["mr"]
    return result


# ---------------------------------------------------------------------------
# Tier 2: repo-wide grep (working tree)
# ---------------------------------------------------------------------------

_GREP_LINE_RE = re.compile(r"^(?P<path>.*?):(?P<line>\d+):(.*)$")


def _utterance_paths(repo_root, target, warnings):
    """Working-tree files where THIS target is uttered as a pointer (t06
    index). Grep hits there are the pointer itself, not production evidence;
    excluding them is what lets a dangling pointer stay 'unresolved'."""
    try:
        if not ocr_index.is_available(repo_root):
            return set()
        result = ocr_index.query_pointer_utterances(repo_root)
        return {str(p.get("path") or "").replace("\\", "/")
                for p in result.get("pointers") or []
                if _name_matches(target, p.get("target") or "")}
    except Exception as exc:
        warnings.append("forge: pointer-utterance lookup failed (%s); grep "
                        "tier may cite the pointer's own site" % exc)
        return set()


def _grep_lookup(run, target, warnings, excluded=None):
    name = _basename(target)
    if not name:
        return None
    out, rc = run(["grep", "-n", "-F", "--no-color", "-m", "1",
                   "--full-name", name])
    if rc not in (0, 1) and out == "":
        warnings.append("forge: repo grep failed (rc=%d); tier skipped" % rc)
        return None
    hits = []
    for line in out.splitlines():
        m = _GREP_LINE_RE.match(line)
        if not m:
            continue
        path = m.group("path")
        if excluded and path.replace("\\", "/") in excluded:
            continue
        hits.append({"path": path, "line": int(m.group("line"))})
        if len(hits) >= MAX_GREP_HITS:
            break
    if not hits:
        return None
    return {"status": "resolved", "via": "grep", "hits": hits,
            "truncated": len(hits) >= MAX_GREP_HITS}


# ---------------------------------------------------------------------------
# Tier 3: sibling branch tips (bounded)
# ---------------------------------------------------------------------------

def _branch_lookup(run, target, warnings):
    name = _basename(target)
    if not name:
        return None
    out, rc = run(["for-each-ref", "--format=%(refname:short)", "refs/heads/"])
    if rc != 0:
        warnings.append("forge: local branch listing failed (rc=%d); tier skipped" % rc)
        return None
    head_out, _ = run(["branch", "--show-current"])
    current = head_out.strip()
    branches = sorted(b for b in out.splitlines() if b.strip() and b.strip() != current)
    found = []
    for branch in branches[:MAX_BRANCHES]:
        gout, grc = run(["grep", "-l", "-F", "--full-name", name, branch])
        if grc == 0 and gout.strip():
            found.append(branch.strip())
    if not found:
        return None
    return {"status": "resolved", "via": "branch", "branches": found[:MAX_BRANCHES]}


# ---------------------------------------------------------------------------
# Negative cache: in-process dict + persisted .ocr/index/forge_negcache.json
# ---------------------------------------------------------------------------

def _load_negcache(repo_root):
    """Load persisted negatives; stale entries are dropped on write-back."""
    cache = {}
    full = os.path.join(repo_root, _NEGCACHE_REL)
    try:
        with open(full, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for key, entry in data.items():
                if isinstance(entry, dict) and isinstance(entry.get("t"), (int, float)):
                    cache[key] = {"t": entry["t"], "result": str(entry.get("result"))}
    except Exception:
        pass  # corrupt/unreadable cache is a cold cache, never an error
    return cache


def _save_negcache(repo_root, cache):
    """Persist the negative cache, TTL-pruned and bounded to MAX_NEGCACHE
    (newest kept). Failure is silent: the cache is an optimization only."""
    if not cache:
        return
    now = time.time()
    fresh = {k: v for k, v in cache.items()
             if now - v.get("t", 0) < NEGCACHE_TTL}
    if len(fresh) > MAX_NEGCACHE:
        fresh = dict(sorted(fresh.items(), key=lambda kv: -kv[1]["t"])[:MAX_NEGCACHE])
    full = os.path.join(repo_root, _NEGCACHE_REL)
    tmp = full + ".tmp"
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(fresh, fh, sort_keys=True)
        os.replace(tmp, full)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Tier 4: live forge call (gh / glab by remote, hard timeout, cached negatives)
# ---------------------------------------------------------------------------

def _forge_cli(run):
    """Autodetect the forge CLI from the origin remote: github.com -> gh;
    gitlab.com and self-hosted GitLab (host contains 'gitlab') -> glab. Any
    other host (Bitbucket, Codeberg, ...) has no supported CLI: return
    (None, url) so the caller skips the forge tier with a warning."""
    url, rc = run(["remote", "get-url", "origin"])
    url = url.strip()
    if rc != 0 or not url:
        return None, None
    if "github.com" in url:
        return "gh", url
    if "gitlab" in url:
        return "glab", url
    return None, url


def _forge_cmd(cli, name):
    if cli == "gh":
        return ["gh", "search", "code", name, "--limit", str(MAX_FORGE_HITS),
                "--json", "path,repository"]
    from urllib.parse import quote
    return ["glab", "api", "search?scope=blobs&search=%s" % quote(name, safe="")]


def _forge_hits(stdout):
    """Extract path hits from forge JSON output (a list of objects carrying
    'path' / 'filename' keys, as `gh search code --json` and `glab api
    .../search?scope=blobs` both emit)."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return "unparseable"
    if not isinstance(data, list):
        return []
    hits = []
    for item in data:
        if isinstance(item, dict):
            path = item.get("path") or item.get("filename")
            if isinstance(path, str) and path:
                hits.append(path)
        if len(hits) >= MAX_FORGE_HITS:
            break
    return hits


def _forge_lookup(repo_root, run, target, timeout, negcache, warnings):
    """Return a resolved dict, the string 'skipped' (unsupported host: the
    tier never ran), the string 'unavailable' (degraded), or None (searched,
    nothing found). Appends degradation reasons to warnings."""
    name = _basename(target)
    cli, url = _forge_cli(run)
    if cli is None:
        if url:
            # Unsupported host: no tier engaged at all, so the walk simply
            # ends without an answer -- 'unresolved' (an absence), not
            # 'unavailable' (a tier tool down). Callers treat both as
            # local-only coverage; the warning says why. The tier did not
            # run, so it must not be reported as searched.
            warnings.append("forge: origin remote %s is neither GitHub nor "
                            "GitLab; forge tier skipped, local tiers only"
                            % url)
            return "skipped"
        warnings.append("forge: no origin remote configured; forge tier "
                        "skipped, local tiers only")
        return "unavailable"
    key = "%s:%s" % (cli, name)
    cached = negcache.get(key)
    if cached is not None and time.time() - cached["t"] < NEGCACHE_TTL:
        if cached["result"] == "miss":
            return None  # known miss, no new call
        warnings.append("forge: %s lookup previously failed (%s, cached); "
                        "local tiers only" % (cli, cached["result"]))
        return "unavailable"
    if shutil.which(cli) is None:
        warnings.append("forge: %s CLI not found; degraded to local tiers "
                        "(registry/grep/branch only)" % cli)
        return "unavailable"

    def remember(result):
        negcache[key] = {"t": time.time(), "result": result}
        _save_negcache(repo_root, negcache)

    try:
        proc = subprocess.run(_forge_cmd(cli, name), cwd=repo_root,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        remember("timeout")
        warnings.append("forge: %s lookup exceeded %.1fs hard timeout; degraded "
                        "to local tiers (registry/grep/branch only)"
                        % (cli, float(timeout)))
        return "unavailable"
    except Exception as exc:
        remember("spawn-failed")
        warnings.append("forge: %s lookup could not start (%s); degraded to "
                        "local tiers" % (cli, exc))
        return "unavailable"
    if proc.returncode != 0:
        remember("rc=%d" % proc.returncode)
        warnings.append("forge: %s lookup failed (rc=%d -- offline or "
                        "unauthenticated); degraded to local tiers"
                        % (cli, proc.returncode))
        return "unavailable"
    hits = _forge_hits(proc.stdout)
    if hits == "unparseable":
        warnings.append("forge: %s lookup returned unparseable output; degraded "
                        "to local tiers" % cli)
        return "unavailable"
    if not hits:
        remember("miss")
        return None
    return {"status": "resolved", "via": "forge", "forge": cli, "hits": hits}


# ---------------------------------------------------------------------------
# Public seam: build the collectors' pointer_resolver callable
# ---------------------------------------------------------------------------

def make_pointer_resolver(repo_root, *, timeout=FORGE_TIMEOUT):
    """Return resolver(target) -> resolution dict. Never raises.

    The same resolver instance shares one in-process negative cache across
    calls, so a degraded forge is paid for once per collect run.
    """
    repo_root = os.path.abspath(str(repo_root))
    warnings = []
    run = _git_runner(repo_root)
    negcache = _load_negcache(repo_root)
    utterance_cache = {}

    def resolve(target):
        target = str(target or "").strip()
        if not target:
            return {"status": "unavailable", "via": "none",
                    "warning": "forge: empty pointer target"}
        try:
            if target not in utterance_cache:
                utterance_cache[target] = _utterance_paths(repo_root, target,
                                                           warnings)
            for tier in (_registry_lookup(repo_root, target, warnings),
                         _grep_lookup(run, target, warnings,
                                      excluded=utterance_cache[target]),
                         _branch_lookup(run, target, warnings)):
                if tier is not None:
                    if warnings:
                        tier["warning"] = "; ".join(warnings[-3:])
                    return tier
            forge = _forge_lookup(repo_root, run, target, timeout,
                                  negcache, warnings)
            if isinstance(forge, dict):
                if warnings and "warning" not in forge:
                    forge["warning"] = "; ".join(warnings[-3:])
                return forge
            searched = ["registry", "grep", "branch"]
            if forge == "skipped":
                # Unsupported host: the tier never ran, so it is not counted
                # as searched -- only the local tiers were.
                result = {"status": "unresolved", "via": "none",
                          "searched": searched}
            elif forge is None:
                searched.append("forge")  # searched (or a known cached miss)
                result = {"status": "unresolved", "via": "none",
                          "searched": searched}
            else:
                result = {"status": "unavailable", "via": "none",
                          "searched": searched}
            if warnings:
                result["warning"] = "; ".join(warnings[-3:])
            return result
        except Exception as exc:  # fail-soft: never break the review
            return {"status": "unavailable", "via": "none",
                    "warning": "forge: resolver failed internally: %s" % exc}

    return resolve
