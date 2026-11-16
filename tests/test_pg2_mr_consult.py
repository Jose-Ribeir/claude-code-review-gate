"""MR/PR context consult (pg2): read-only open-MR discovery + consistency findings.

The module under test (scripts/rg_mr_consult.py) answers, for a push to a
branch: is there an open MR/PR for this branch on this remote's forge, what
does it declare, and does the pushed diff contradict the declaration? All
lookups go through the maintainer's local gh/glab sign-in, cached 24h under
the gate state dir (OCR_STATE_DIR), positives AND negatives. Every failure
(CLI missing, offline, timeout, non-zero exit, unparseable JSON, unsupported
forge) degrades to "no context" (None / empty), never raises.

CLI boundary: REAL shim executables on a private PATH (the test_forge.py /
test_rg_ticket_fetch.py pattern) -- canned JSON files, scripted exit codes,
and an argv log so the tests assert exactly which CLI calls happened. The
conftest autouse fixture gives a private OCR_STATE_DIR per test; the cache
TTL tests inject `now` instead of sleeping.
"""
import json
import os
import subprocess
import sys

import pytest  # noqa: F401  (fixtures used via function args)

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, _SCRIPTS)

from conftest import minimal_path, posix_path, write_fake_cli  # noqa: E402

import rg_mr_consult  # noqa: E402
from rg_mr_consult import MrContext  # noqa: E402


GH_URL = "https://github.com/acme/proj.git"
GH_SSH_URL = "git@github.com:acme/proj.git"
GLAB_URL = "https://gitlab.com/acme/proj.git"
GLAB_SSH_URL = "git@gitlab.example.com:acme/proj.git"
BRANCH = "feature-x"

HEAD = "c3fe250b" + "0" * 32
TIP = "1" * 40

GH_PR = {"number": 42, "title": "Add connector titles ADR",
         "body": "docs-only: a markdown file.",
         "labels": [{"name": "documentation"}],
         "headRefOid": HEAD, "baseRefName": "main",
         "url": "https://github.com/acme/proj/pull/42"}

GLAB_MR = {"iid": 7, "title": "Add connector titles ADR",
           "description": "docs-only: a markdown file.",
           "labels": ["documentation"], "sha": HEAD,
           "target_branch": "main",
           "web_url": "https://gitlab.com/acme/proj/-/merge_requests/7"}


# --- the shim-CLI seam -------------------------------------------------------

def _action_lines(action):
    kind = action[0]
    if kind == "file":                       # ("file", key)
        return ['    key="%s"' % action[1], "    ;;"]
    if kind == "arg":                        # ("arg", prefix, positional_1based)
        return ["    shift %d" % (action[2] - 1),
                '    key="%s_$1"' % action[1], "    ;;"]
    if kind == "iid":                        # iid embedded in the api path arg
        return ["    iid=$(printf '%s' \"$glued\" | "
                "sed -n 's/.*merge_requests\\/\\([0-9][0-9]*\\)\\/changes.*/\\1/p')",
                '    key="%s_$iid"' % action[1], "    ;;"]
    if kind == "sleep":                      # ("sleep", seconds)
        return ["    sleep %s" % action[1], "    exit 0", "    ;;"]
    raise AssertionError("unknown shim action %r" % (action,))


def _write_shim(tmp_path, monkeypatch, name, routes):
    """A fake CLI named `name` as the ONLY CLI on PATH.

    routes: ordered (needle, action); the first argv string containing the
    needle wins. Canned stdout goes in canned/files/<key>, a scripted failure
    in canned/rc/<key> (write an exit code there to flip the route to
    failing), and every invocation appends to canned/calls.log.
    """
    bindir = tmp_path / ("bin-" + name)
    canned = tmp_path / ("canned-" + name)
    (canned / "files").mkdir(parents=True)
    (canned / "rc").mkdir()
    lines = ["#!/bin/sh",
             'dir="%s"' % posix_path(canned),
             'echo "%s $*" >> "$dir/calls.log"' % name,
             'glued=" $* "',
             'case "$glued" in']
    for needle, action in routes:
        lines.append('  *"%s"*)' % needle)
        lines.extend(_action_lines(action))
    lines.append('  *) echo "fake %s: unscripted call: $*" >&2; exit 3;;' % name)
    lines.append("esac")
    lines.extend([
        "rc=0",
        '[ -f "$dir/rc/$key" ] && rc=$(cat "$dir/rc/$key")',
        'if [ "$rc" != "0" ]; then',
        '  echo "fake %s: scripted failure rc=$rc" >&2' % name,
        '  exit "$rc"',
        "fi",
        'if [ ! -f "$dir/files/$key" ]; then',
        '  echo "fake %s: no canned output for $key" >&2' % name,
        "  exit 3",
        "fi",
        'cat "$dir/files/$key"',
        "exit 0",
    ])
    write_fake_cli(bindir, name, "\n".join(lines) + "\n")
    monkeypatch.setenv("PATH", minimal_path(str(bindir)))
    return canned


def _serve(canned, key, payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (canned / "files" / key).write_text(text, encoding="utf-8")


def _flip_to_failure(canned, key, code=1):
    (canned / "rc" / key).write_text(str(code), encoding="ascii")


def _clear_failure(canned, key):
    try:
        (canned / "rc" / key).unlink()
    except FileNotFoundError:
        pass


def _calls(canned):
    log = canned / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _gh_routes():
    return [("pr view", ("arg", "view", 3)),
            ("--head", ("file", "list_head")),
            ("pr list", ("file", "list_all"))]


def _glab_routes():
    return [("/changes", ("iid", "changes")),
            ("source_branch", ("file", "mr_branch")),
            ("per_page", ("file", "mr_all"))]


# --- provider resolution + the happy path -------------------------------------

def test_github_https_remote_finds_open_pr(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_head", [GH_PR])
    ctx = rg_mr_consult.find_open_mr(GH_URL, BRANCH)
    assert ctx == MrContext(iid=42, title="Add connector titles ADR",
                            description="docs-only: a markdown file.",
                            labels=["documentation"], head_sha=HEAD,
                            target_branch="main",
                            web_url="https://github.com/acme/proj/pull/42")
    (call,) = _calls(canned)
    assert "pr list" in call and "--head" in call and BRANCH in call


def test_github_scp_ssh_remote_finds_open_pr(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_head", [GH_PR])
    ctx = rg_mr_consult.find_open_mr(GH_SSH_URL, BRANCH)
    assert ctx is not None and ctx.iid == 42


def test_gitlab_https_remote_finds_open_mr(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "glab", _glab_routes())
    _serve(canned, "mr_branch", [GLAB_MR])
    ctx = rg_mr_consult.find_open_mr(GLAB_URL, BRANCH)
    assert ctx == MrContext(iid=7, title="Add connector titles ADR",
                            description="docs-only: a markdown file.",
                            labels=["documentation"], head_sha=HEAD,
                            target_branch="main",
                            web_url="https://gitlab.com/acme/proj/-/merge_requests/7")
    (call,) = _calls(canned)
    assert call.startswith("glab api ") and "source_branch=%s" % BRANCH in call


def test_selfhosted_gitlab_scp_remote_uses_glab(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "glab", _glab_routes())
    _serve(canned, "mr_branch", [GLAB_MR])
    ctx = rg_mr_consult.find_open_mr(GLAB_SSH_URL, BRANCH)
    assert ctx is not None and ctx.iid == 7
    assert _calls(canned), "self-hosted GitLab must still resolve to glab"


def test_unsupported_forge_is_no_context_without_a_cli_call(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    assert rg_mr_consult.find_open_mr("https://bitbucket.org/acme/proj.git",
                                      BRANCH) is None
    assert _calls(canned) == []


def test_no_open_mr_returns_none_and_the_negative_is_cached(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_head", [])
    assert rg_mr_consult.find_open_mr(GH_URL, BRANCH) is None
    assert rg_mr_consult.find_open_mr(GH_URL, BRANCH) is None
    assert len(_calls(canned)) == 1, "a cached negative must not re-hit the CLI"


# --- failures degrade to "no context", never raise ----------------------------

def test_missing_cli_is_no_context_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", minimal_path(str(tmp_path / "empty-bin")))
    assert rg_mr_consult.find_open_mr(GH_URL, BRANCH) is None
    assert rg_mr_consult.find_open_mr(GLAB_URL, BRANCH) is None


def test_cli_error_exit_is_no_context_and_not_cached(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_head", [GH_PR])
    _flip_to_failure(canned, "list_head", code=4)
    assert rg_mr_consult.find_open_mr(GH_URL, BRANCH) is None
    _clear_failure(canned, "list_head")
    assert rg_mr_consult.find_open_mr(GH_URL, BRANCH).iid == 42
    assert len(_calls(canned)) == 2, "a transient failure must be retried"


def test_timeout_is_no_context(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", [("--head", ("sleep", 5))])
    assert rg_mr_consult.find_open_mr(GH_URL, BRANCH, timeout=0.3) is None
    assert _calls(canned), "the shim was reached; it just never answered in time"


def test_unparseable_json_is_no_context(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_head", "<html>rate limited</html>")
    assert rg_mr_consult.find_open_mr(GH_URL, BRANCH) is None


def test_cli_timeout_bound_is_small():
    assert rg_mr_consult.MR_TIMEOUT <= 8


# --- the 24h cache (positive entries) ------------------------------------------

def test_positive_result_is_cached_within_the_ttl(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_head", [GH_PR])
    first = rg_mr_consult.find_open_mr(GH_URL, BRANCH, now=1000.0)
    second = rg_mr_consult.find_open_mr(GH_URL, BRANCH,
                                        now=1000.0 + rg_mr_consult.MR_TTL - 60)
    assert first == second
    assert len(_calls(canned)) == 1


def test_expired_positive_entry_refetches(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_head", [GH_PR])
    rg_mr_consult.find_open_mr(GH_URL, BRANCH, now=1000.0)
    ctx = rg_mr_consult.find_open_mr(GH_URL, BRANCH,
                                     now=1000.0 + rg_mr_consult.MR_TTL + 60)
    assert ctx is not None and ctx.iid == 42
    assert len(_calls(canned)) == 2


def test_corrupt_cache_file_refetches(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_head", [GH_PR])
    assert rg_mr_consult.find_open_mr(GH_URL, BRANCH) is not None
    state_dir = os.environ["OCR_STATE_DIR"]
    cache_files = []
    for root, _dirs, files in os.walk(state_dir):
        cache_files.extend(os.path.join(root, f) for f in files
                           if f.endswith(".json"))
    assert cache_files, "setup: the lookup must have written a cache entry"
    for cache in cache_files:
        with open(cache, "w", encoding="utf-8") as fh:
            fh.write("{not json")
    assert rg_mr_consult.find_open_mr(GH_URL, BRANCH) is not None
    assert len(_calls(canned)) == 2


def test_expired_negative_entry_refetches(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_head", [])
    assert rg_mr_consult.find_open_mr(GH_URL, BRANCH, now=1000.0) is None
    _serve(canned, "list_head", [GH_PR])
    ctx = rg_mr_consult.find_open_mr(GH_URL, BRANCH,
                                     now=1000.0 + rg_mr_consult.MR_TTL + 60)
    assert ctx is not None and ctx.iid == 42
    assert len(_calls(canned)) == 2


def test_a_raising_cli_boundary_never_escapes(tmp_path, monkeypatch):
    def explode(argv, **kw):
        raise RuntimeError("simulated caller bug")

    assert rg_mr_consult.find_open_mr(GH_URL, BRANCH, runner=explode) is None
    assert rg_mr_consult.target_branch(GH_URL, BRANCH, runner=explode) is None
    assert rg_mr_consult.first_seen(GH_URL, BRANCH, runner=explode) is False
    assert rg_mr_consult.cross_mr_path_overlap(GH_URL, ["a.py"], 1,
                                               runner=explode) == {}


def test_find_open_mr_does_not_cache_a_degraded_lookup(tmp_path, monkeypatch):
    # Transient degradation (offline) must not be remembered as "no MR".
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _flip_to_failure(canned, "list_head", code=1)
    assert rg_mr_consult.find_open_mr(GH_URL, BRANCH) is None
    _clear_failure(canned, "list_head")
    _serve(canned, "list_head", [GH_PR])
    assert rg_mr_consult.target_branch(GH_URL, BRANCH) == "main"


def test_cache_is_keyed_by_remote_and_branch(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_head", [])
    assert rg_mr_consult.find_open_mr(GH_URL, BRANCH) is None
    assert rg_mr_consult.find_open_mr(GH_URL, "other-branch") is None
    assert rg_mr_consult.find_open_mr(GH_SSH_URL, BRANCH) is None
    assert len(_calls(canned)) == 3  # each (remote, branch) pair fetched


# --- target_branch / first_seen ------------------------------------------------

def test_target_branch_from_live_lookup(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "glab", _glab_routes())
    _serve(canned, "mr_branch", [GLAB_MR])
    assert rg_mr_consult.target_branch(GLAB_URL, BRANCH) == "main"


def test_target_branch_from_the_cache_without_new_cli_calls(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "glab", _glab_routes())
    _serve(canned, "mr_branch", [GLAB_MR])
    assert rg_mr_consult.target_branch(GLAB_URL, BRANCH) == "main"
    assert rg_mr_consult.target_branch(GLAB_URL, BRANCH) == "main"
    assert len(_calls(canned)) == 1


def test_target_branch_is_none_without_an_mr(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_head", [])
    assert rg_mr_consult.target_branch(GH_URL, BRANCH) is None


def test_first_seen_true_when_a_prior_negative_now_finds_an_mr(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_head", [])
    assert rg_mr_consult.find_open_mr(GH_URL, BRANCH) is None  # seed the negative
    _serve(canned, "list_head", [GH_PR])  # the MR is created on the web later
    assert rg_mr_consult.first_seen(GH_URL, BRANCH) is True
    # ...and the fresh positive replaced the negative in the cache.
    rg_mr_consult.find_open_mr(GH_URL, BRANCH)
    assert len(_calls(canned)) == 2


def test_first_seen_false_without_a_prior_negative(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_head", [GH_PR])
    # No baseline: "never looked" is not "first seen", only "first looked".
    assert rg_mr_consult.first_seen(GH_URL, BRANCH) is False
    assert len(_calls(canned)) == 1


def test_first_seen_false_while_still_absent(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_head", [])
    assert rg_mr_consult.find_open_mr(GH_URL, BRANCH) is None
    assert rg_mr_consult.first_seen(GH_URL, BRANCH) is False


def test_first_seen_false_for_an_already_known_positive(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_head", [GH_PR])
    assert rg_mr_consult.find_open_mr(GH_URL, BRANCH) is not None
    calls_before = len(_calls(canned))
    assert rg_mr_consult.first_seen(GH_URL, BRANCH) is False
    assert len(_calls(canned)) == calls_before, "a known MR needs no lookup"


def test_first_seen_degrades_to_false_on_cli_error(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_head", [])
    assert rg_mr_consult.find_open_mr(GH_URL, BRANCH) is None
    _flip_to_failure(canned, "list_head")
    assert rg_mr_consult.first_seen(GH_URL, BRANCH) is False


# --- consistency findings (pure; each of the three checks + clean) --------------

def _ctx(**over):
    base = dict(iid=7, title="Change", description="", labels=[],
                head_sha=HEAD, target_branch="main",
                web_url="https://gitlab.com/acme/proj/-/merge_requests/7")
    base.update(over)
    return MrContext(**base)


def _words_count_as_docs_only_claim(description):
    return _ctx(description=description)


def test_no_claims_and_matching_head_is_clean():
    ctx = _ctx()
    assert rg_mr_consult.consistency_findings(
        ctx, ["app/x.py", "docs/adr.md"], 120, HEAD) == []


def test_docs_only_label_vs_code_paths_is_a_finding():
    ctx = _ctx(labels=["documentation"])
    findings = rg_mr_consult.consistency_findings(
        ctx, ["docs/adr.md", "app/impl.py", "lib/core.py"], 300, HEAD)
    (finding,) = findings
    assert finding.startswith("MR-CONSISTENCY:")
    assert "app/impl.py" in finding and "lib/core.py" in finding
    assert "docs/adr.md" not in finding, "docs paths do not contradict"
    assert "docs/**" in finding, "the finding must state the classification rule"


def test_docs_only_description_phrases_vs_code_paths():
    for phrase in ("docs-only", "documentation only", "a markdown file"):
        ctx = _words_count_as_docs_only_claim("This MR is %s." % phrase)
        findings = rg_mr_consult.consistency_findings(
            ctx, ["scripts/rg_mr_consult.py"], 400, HEAD)
        assert len(findings) == 1, phrase
        assert findings[0].startswith("MR-CONSISTENCY:")


def test_docs_only_claim_with_only_docs_paths_is_clean():
    ctx = _ctx(labels=["documentation"], description="docs-only")
    assert rg_mr_consult.consistency_findings(
        ctx, ["docs/a.md", "docs/adr/b.md", "README.md"], 60, HEAD) == []


def test_claimed_file_count_mismatch_is_a_finding():
    ctx = _ctx(description="This MR changes 1 file.")
    findings = rg_mr_consult.consistency_findings(
        ctx, ["a.py", "b.py", "c.py"], 90, HEAD)
    assert len(findings) == 1
    assert "1 file" in findings[0] and "3" in findings[0]
    assert findings[0].startswith("MR-CONSISTENCY:")


def test_claimed_file_count_word_form_mismatch():
    ctx = _ctx(title="Small fix (one file)")
    findings = rg_mr_consult.consistency_findings(
        ctx, ["a.py", "b.py"], 20, HEAD)
    assert len(findings) == 1 and findings[0].startswith("MR-CONSISTENCY:")


def test_claimed_file_count_matching_actual_is_clean():
    ctx = _ctx(description="This MR changes 2 files.")
    assert rg_mr_consult.consistency_findings(
        ctx, ["a.py", "b.py"], 20, HEAD) == []


def test_claimed_line_count_mismatch_is_a_finding():
    ctx = _ctx(description="Adds +40 lines of documentation.")
    findings = rg_mr_consult.consistency_findings(
        ctx, ["docs/a.md", "app/b.py"], 13913, HEAD)
    assert any(f.startswith("MR-CONSISTENCY:") and "40" in f and "13913" in f
               for f in findings)


def test_claimed_line_count_matching_actual_is_clean():
    ctx = _ctx(description="Adds 120 lines.")
    assert rg_mr_consult.consistency_findings(
        ctx, ["a.py"], 120, HEAD) == []


def test_head_mismatch_is_a_finding():
    ctx = _ctx()  # recorded head HEAD
    findings = rg_mr_consult.consistency_findings(ctx, ["a.py"], 10, TIP)
    assert len(findings) == 1
    assert findings[0].startswith("MR-CONSISTENCY:")
    assert HEAD[:12] in findings[0] and TIP[:12] in findings[0]


def test_head_match_adds_no_finding():
    ctx = _ctx()
    assert rg_mr_consult.consistency_findings(ctx, ["a.py"], 10, HEAD) == []


# --- recorded-head posture: rewritten history blocks, normal iteration advises ---

def test_head_mismatch_with_ancestor_recorded_head_is_advisory_only():
    """Story 13 asks for VISIBILITY, not a veto on iteration: when the MR
    page's recorded head is an ancestor of the pushed tip (the MR simply trails
    the branch, normal review-new-commits flow) the mismatch is advisory, never
    a block. The advisory prefix keeps it out of the blocking finding set."""
    ctx = _ctx()  # recorded head HEAD, pushed tip TIP
    findings = rg_mr_consult.consistency_findings(
        ctx, ["a.py"], 10, TIP, head_ancestor=True)
    assert len(findings) == 1
    assert findings[0].startswith(rg_mr_consult.ADVISORY_PREFIX)
    assert not findings[0].startswith(rg_mr_consult.FINDING_PREFIX)
    assert "ancestor" in findings[0]


def test_head_mismatch_with_rewritten_or_unknown_head_stays_blocking():
    """head_ancestor=False (rewritten history, or ancestry unknowable -- the
    safe direction) keeps the mismatch a blocking MR-CONSISTENCY finding."""
    ctx = _ctx()
    for head_ancestor in (False, None):
        findings = rg_mr_consult.consistency_findings(
            ctx, ["a.py"], 10, TIP, head_ancestor=head_ancestor)
        assert len(findings) == 1
        assert findings[0].startswith(rg_mr_consult.FINDING_PREFIX)
        assert not findings[0].startswith(rg_mr_consult.ADVISORY_PREFIX)


def _git_repo_with_divergence(tmp_path):
    """first -> second, then a divergent rewrite from first."""
    repo = tmp_path / "repo"

    def g(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "t")
    g("config", "commit.gpgsign", "false")
    (repo / "a.txt").write_text("one\n")
    g("add", ".")
    g("commit", "-q", "-m", "base")
    first = g("rev-parse", "HEAD")
    (repo / "b.txt").write_text("two\n")
    g("add", ".")
    g("commit", "-q", "-m", "second")
    second = g("rev-parse", "HEAD")
    g("reset", "-q", "--hard", first)
    (repo / "c.txt").write_text("three\n")
    g("add", ".")
    g("commit", "-q", "-m", "rewritten")
    divergent = g("rev-parse", "HEAD")
    return repo, first, second, divergent


def test_head_is_ancestor_true_for_normal_iteration(tmp_path):
    repo, first, second, _ = _git_repo_with_divergence(tmp_path)
    assert rg_mr_consult.head_is_ancestor(first, second, repo_root=str(repo)) \
        is True


def test_head_is_ancestor_false_for_rewritten_history(tmp_path):
    repo, _, second, divergent = _git_repo_with_divergence(tmp_path)
    assert rg_mr_consult.head_is_ancestor(second, divergent,
                                          repo_root=str(repo)) is False


def test_head_is_ancestor_degrades_to_false_on_bad_input(tmp_path):
    repo, _, second, _ = _git_repo_with_divergence(tmp_path)
    assert rg_mr_consult.head_is_ancestor("0" * 40, second,
                                          repo_root=str(repo)) is False
    assert rg_mr_consult.head_is_ancestor("", second, repo_root=str(repo)) \
        is False
    assert rg_mr_consult.head_is_ancestor(second, second,
                                          repo_root=str(repo)) is False
    assert rg_mr_consult.head_is_ancestor(
        second, second, repo_root=str(tmp_path / "not-a-repo")) is False


def test_findings_never_raise_on_degenerate_input():
    ctx = _ctx(labels=[], description="", title="")
    assert rg_mr_consult.consistency_findings(ctx, [], 0, "") == []
    # A docs-claiming MR with an empty diff still cannot contradict itself.
    assert rg_mr_consult.consistency_findings(
        _ctx(labels=["documentation"]), [], 0, "") == []


def test_every_finding_is_a_prefixed_one_liner():
    ctx = _ctx(labels=["documentation"], description="changes 1 file",
               head_sha=HEAD)
    findings = rg_mr_consult.consistency_findings(
        ctx, ["a.py", "b.py"], 999, TIP)
    assert len(findings) == 3
    for finding in findings:
        assert finding.startswith("MR-CONSISTENCY:")
        assert "\n" not in finding


# --- cross-MR path overlap (the large-diff tier) --------------------------------

GH_ALL_LIST = [
    {"number": 45, "url": "https://github.com/acme/proj/pull/45"},
    {"number": 46, "url": "https://github.com/acme/proj/pull/46"},
]
GH_FILES_45 = {"files": [{"path": "app/impl.py"}, {"path": "docs/x.md"}]}
GH_FILES_46 = {"files": [{"path": "lib/other.py"}]}


def test_cross_overlap_github_intersects_other_mrs_paths(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_all", GH_ALL_LIST)
    _serve(canned, "view_45", GH_FILES_45)
    _serve(canned, "view_46", GH_FILES_46)
    overlap = rg_mr_consult.cross_mr_path_overlap(
        GH_URL, ["app/impl.py", "lib/mine.py"], exclude_iid=99)
    assert overlap == {"https://github.com/acme/proj/pull/45": ["app/impl.py"]}


def test_cross_overlap_excludes_the_pushed_mrs_own_iid(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_all", GH_ALL_LIST)
    _serve(canned, "view_46", GH_FILES_46)
    overlap = rg_mr_consult.cross_mr_path_overlap(
        GH_URL, ["app/impl.py"], exclude_iid=45)
    assert overlap == {}
    assert not any("pr view 45" in c for c in _calls(canned)), \
        "the pushed MR's own file list must not be fetched"


GLAB_ALL_LIST = [
    {"iid": 7, "web_url": "https://gitlab.com/acme/proj/-/merge_requests/7"},
    {"iid": 8, "web_url": "https://gitlab.com/acme/proj/-/merge_requests/8"},
]
GLAB_CHANGES_8 = {"changes": [{"new_path": "scripts/rg_store.py",
                               "old_path": "scripts/rg_store.py"},
                              {"new_path": "docs/adr.md",
                               "old_path": "docs/adr.md"}]}


def test_cross_overlap_gitlab_uses_the_changes_endpoint(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "glab", _glab_routes())
    _serve(canned, "mr_all", GLAB_ALL_LIST)
    _serve(canned, "changes_8", GLAB_CHANGES_8)
    overlap = rg_mr_consult.cross_mr_path_overlap(
        GLAB_URL, ["scripts/rg_store.py", "app/mine.py"], exclude_iid=7)
    assert overlap == {"https://gitlab.com/acme/proj/-/merge_requests/8":
                       ["scripts/rg_store.py"]}


def test_cross_overlap_no_intersection_is_empty(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_all", GH_ALL_LIST)
    _serve(canned, "view_45", GH_FILES_45)
    _serve(canned, "view_46", GH_FILES_46)
    assert rg_mr_consult.cross_mr_path_overlap(
        GH_URL, ["unrelated/only.py"], exclude_iid=1) == {}


def test_cross_overlap_degrades_to_empty_on_cli_failure(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_all", GH_ALL_LIST)
    _flip_to_failure(canned, "list_all")
    assert rg_mr_consult.cross_mr_path_overlap(
        GH_URL, ["app/impl.py"], exclude_iid=1) == {}


def test_cross_overlap_skips_an_mr_whose_file_fetch_fails(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_all", GH_ALL_LIST)
    _serve(canned, "view_45", GH_FILES_45)
    _flip_to_failure(canned, "view_45")
    _serve(canned, "view_46", GH_FILES_46)
    overlap = rg_mr_consult.cross_mr_path_overlap(
        GH_URL, ["app/impl.py", "lib/other.py"], exclude_iid=1)
    # 45 could not be fetched (skipped, not fatal); 46 overlaps on lib/other.py.
    assert overlap == {"https://github.com/acme/proj/pull/46": ["lib/other.py"]}


def test_cross_overlap_empty_path_list_never_fetches(tmp_path, monkeypatch):
    canned = _write_shim(tmp_path, monkeypatch, "gh", _gh_routes())
    _serve(canned, "list_all", GH_ALL_LIST)
    assert rg_mr_consult.cross_mr_path_overlap(GH_URL, [], exclude_iid=45) == {}
    assert not any("pr view" in c for c in _calls(canned)), \
        "nothing to intersect: no file lists needed"


def test_cross_overlap_list_times_out_to_empty(tmp_path, monkeypatch):
    _write_shim(tmp_path, monkeypatch, "gh", [("pr list", ("sleep", 5))])
    assert rg_mr_consult.cross_mr_path_overlap(
        GH_URL, ["app/impl.py"], exclude_iid=1, timeout=0.3) == {}
