"""Orchestrator wiring (push-gate ticket pg5): config consumption, the MR-context
consult, and MR-target base resolution.

The gate subprocess (run exactly as the installed pre-push hook runs it:
`--mode git`, ref updates on stdin, `--remote <name>`) now:

- reads the user-level run config (rg_config.load_config): review.depth re-scopes
  the review prompt, review.timeoutSeconds caps the reviewer call (timeout is a
  fail-closed block with the OCR_FAIL_OPEN bypass route), review.failOpen feeds
  pg4's audited bypass plumbing; config warnings print as advisories.
- consults rg_mr_consult for the pushed branch: consistency findings
  (MR-CONSISTENCY:) block the push as objective scope contradictions, BEFORE any
  review runs and even for a tip an earlier verdict already covered (the
  retroactive-discover/"first seen" path for web-created MRs); consult
  degradation (no CLIs on PATH) is only an advisory.
- resolves the review base against the open MR's target branch before the
  legacy fallbacks, so a stacked piece branch reviews only its own scope.

Same seams as pg4 (fixture repos, stub reviewer via OCR_REVIEWER_CMD, stub
invocation counting through STUB_TRACE) plus pg2's Shim-CLI pattern: a fake `gh`
on a private PATH serving canned MR JSON. Nothing is monkeypatched.
"""
import json
import os
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_STUB = os.path.join(_HERE, "stub_reviewer.py")

sys.path.insert(0, _SCRIPTS)
import rg_store  # noqa: E402
from conftest import minimal_path, posix_path, scrub_git_trace, write_fake_cli  # noqa: E402

ZERO = "0" * 40
GH_URL = "https://github.com/acme/proj.git"


def _git(args, cwd, check=True, env=None):
    return subprocess.run(["git"] + args, cwd=str(cwd), env=env, capture_output=True,
                          text=True, check=check).stdout.strip()


def _config(work):
    _git(["config", "user.email", "t@example.com"], cwd=work)
    _git(["config", "user.name", "t"], cwd=work)
    _git(["config", "commit.gpgsign", "false"], cwd=work)


@pytest.fixture
def repo(tmp_path):
    """A clone with a pushed main and a bare origin (the push destination)."""
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    _config(work)
    # A repo-local hooksPath shadows the developer's own global pre-push hook.
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    _git(["config", "core.hooksPath", str(hooks)], cwd=work)
    (work / "a.txt").write_text("one\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    return work


def _commit(work, name, text=None):
    (work / name).write_text((text or name) + "\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", f"add {name}"], cwd=work)
    return _git(["rev-parse", "HEAD"], cwd=work)


def _feat_tip(repo, files=("f1.txt",)):
    """A feat branch with the given code files; return its tip."""
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    tip = None
    for name in files:
        tip = _commit(repo, name, "code " + name)
    return tip


def _line(tip, branch="feat"):
    return f"refs/heads/{branch} {tip} refs/heads/{branch} {ZERO}"


def _env(tmp_path, **kw):
    env = scrub_git_trace(dict(os.environ))
    for k in ("OCR_IN_REVIEW", "OCR_FAIL_OPEN", "OCR_ADVISORY", "OCR_FORCE_REVIEW",
              "OCR_LEGACY_RANGE", "OCR_INLINE_BUDGET", "OCR_COVERAGE_V1",
              "OCR_BASE_V1", "OCR_TIMEOUT", "OCR_REVIEW_DEPTH",
              "OCR_REVIEW_TIMEOUT_SECONDS", "OCR_CONFIG_FILE",
              "STUB_SLEEP", "STUB_VERDICT", "STUB_TRACE"):
        env.pop(k, None)
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "gate-data")
    env["OCR_REVIEWER_CMD"] = f'"{sys.executable}" "{_STUB}"'
    env["STUB_TRACE"] = str(tmp_path / "stub.trace")
    env["OCR_INLINE_BUDGET"] = "30"
    env.update({k: str(v) for k, v in kw.items()})
    return env


def _git_mode(work, lines, env, *extra):
    """Run `--mode git` the way scripts/pre-push does: ref updates on stdin."""
    return subprocess.run([sys.executable, _GATE, "--mode", "git", "--remote", "origin",
                           *extra],
                          input="".join(line + "\n" for line in lines),
                          capture_output=True, text=True, cwd=str(work), env=env,
                          timeout=180)


def _trace(tmp_path):
    p = tmp_path / "stub.trace"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _rid(work, remote="origin"):
    return rg_store.remote_url(str(work), remote)


def _bypasses():
    path = rg_store.store_root() / "bypasses.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _github_ify(repo):
    """Point origin's URL at a (never contacted -- the gate is read-only and the
    test seam never runs `git push`) GitHub slug, so the MR consult finds a
    supported forge to interrogate through the shim CLI."""
    _git(["remote", "set-url", "origin", GH_URL], cwd=repo)


# --- the shim-CLI seam (pg2's pattern) ------------------------------------------

def _action_lines(action):
    kind = action[0]
    if kind == "file":                       # ("file", key)
        return ['    key="%s"' % action[1], "    ;;"]
    return ("    iid=$(printf '%s' \"$glued\" | "
            "sed -n 's/.*merge_requests\\/\\([0-9][0-9]*\\)\\/changes.*/\\1/p')\n"
            '    key="%s_$iid"' % action[1]).splitlines() + ["    ;;"]


def _write_gh_shim(tmp_path):
    """A fake `gh` as the only forge CLI on PATH (canned files + argv log).

    Returns (bindir, canned). `_serve(canned, "list_head", payload)` supplies
    the open-PR list response; flip it between pushes for the first_seen path.
    """
    bindir = tmp_path / "bin-gh"
    canned = tmp_path / "canned-gh"
    (canned / "files").mkdir(parents=True)
    (canned / "rc").mkdir()
    routes = [("pr view", ("file", "view")),
              ("--head", ("file", "list_head")),
              ("pr list", ("file", "list_all"))]
    lines = ["#!/bin/sh",
             'dir="%s"' % posix_path(canned),
             'echo "gh $*" >> "$dir/calls.log"',
             'glued=" $* "',
             'case "$glued" in']
    for needle, action in routes:
        lines.append('  *"%s"*)' % needle)
        lines.extend(_action_lines(action))
    lines.append('  *) echo "fake gh: unscripted call: $*" >&2; exit 3;;')
    lines.append("esac")
    lines.extend([
        "rc=0",
        '[ -f "$dir/rc/$key" ] && rc=$(cat "$dir/rc/$key")',
        'if [ "$rc" != "0" ]; then',
        '  echo "fake gh: scripted failure rc=$rc" >&2',
        '  exit "$rc"',
        "fi",
        'if [ ! -f "$dir/files/$key" ]; then',
        '  echo "fake gh: no canned output for $key" >&2',
        "  exit 3",
        "fi",
        'cat "$dir/files/$key"',
        "exit 0",
    ])
    write_fake_cli(bindir, "gh", "\n".join(lines) + "\n")
    return bindir, canned


def _serve(canned, key, payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (canned / "files" / key).write_text(text, encoding="utf-8")


def _mr(tip, *, body="Implements the connector titles change.",
        labels=("enhancement",), target="main", number=42):
    return [{"number": number, "title": "Add connector titles",
             "body": body,
             "labels": [{"name": lab} for lab in labels],
             "headRefOid": tip, "baseRefName": target,
             "url": "https://github.com/acme/proj/pull/%d" % number}]


# --- MR consistency findings block (stories 12-13) ---------------------------

def test_docs_only_claim_with_code_in_the_diff_is_blocked(repo, tmp_path):
    """(a) The incident shape: an MR declaring docs-only scope while the pushed
    diff carries code is blocked pre-review, the MR-CONSISTENCY prefix preserved
    into the deny reason; the reviewer never runs."""
    bindir, canned = _write_gh_shim(tmp_path)
    tip = _feat_tip(repo)
    _github_ify(repo)
    _serve(canned, "list_head",
           _mr(tip, body="docs-only: a markdown file.", labels=("documentation",)))
    proc = _git_mode(repo, [_line(tip)],
                     _env(tmp_path, PATH=minimal_path(str(bindir))))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "MR-CONSISTENCY:" in out, out
    assert "docs-only" in out, out
    assert "OCR_FAIL_OPEN" in out, out          # the Q-standard bypass route
    assert _trace(tmp_path) == [], "a scope contradiction was sent to the reviewer"


def test_claimed_file_count_vs_actual_is_blocked(repo, tmp_path):
    """(b) '1 file' declared, two pushed: an objective count contradiction."""
    bindir, canned = _write_gh_shim(tmp_path)
    tip = _feat_tip(repo, ("f1.txt", "f2.txt"))
    _github_ify(repo)
    _serve(canned, "list_head", _mr(tip, body="Touches 1 file.", labels=()))
    proc = _git_mode(repo, [_line(tip)],
                     _env(tmp_path, PATH=minimal_path(str(bindir))))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "MR-CONSISTENCY:" in out and "file" in out, out
    assert _trace(tmp_path) == []


def test_mr_recorded_head_vs_pushed_tip_mismatch_is_blocked(repo, tmp_path):
    """(c) The MR page's recorded head names a different commit than the tip
    being pushed: verdicts/comments there do not cover this push."""
    bindir, canned = _write_gh_shim(tmp_path)
    tip = _feat_tip(repo)
    _github_ify(repo)
    _serve(canned, "list_head", _mr("1" * 40))
    proc = _git_mode(repo, [_line(tip)],
                     _env(tmp_path, PATH=minimal_path(str(bindir))))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "MR-CONSISTENCY:" in out and "recorded head" in out, out
    assert _trace(tmp_path) == []


def test_clean_consistency_lets_the_review_run_and_the_push_pass(repo, tmp_path):
    """(d) Declaration and diff agree: the ordinary review path is untouched."""
    bindir, canned = _write_gh_shim(tmp_path)
    tip = _feat_tip(repo)
    _github_ify(repo)
    _serve(canned, "list_head", _mr(tip))
    proc = _git_mode(repo, [_line(tip)],
                     _env(tmp_path, PATH=minimal_path(str(bindir))))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "MR-CONSISTENCY:" not in out, out
    assert "no MR context" not in out, out
    assert len(_trace(tmp_path)) == 1, "a consistent MR never blocked the review"
    rec = rg_store.load_verdict(_rid(repo), tip)
    assert rec is not None and rec["verdict"] == "pass"


def test_consult_degrades_to_an_advisory_without_forge_clis(repo, tmp_path):
    """(e) No gh/glab on PATH: the push proceeds (consult degradation is never
    a block -- spec story 25), with a "no MR context" advisory on stderr."""
    tip = _feat_tip(repo)
    _github_ify(repo)
    proc = _git_mode(repo, [_line(tip)], _env(tmp_path, PATH=minimal_path()))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "no MR context" in out, out
    assert len(_trace(tmp_path)) == 1


# --- MR-target base resolution (story 9) ----------------------------------------

def test_mr_target_branch_resolves_the_review_base(repo, tmp_path):
    """(f) A stacked piece branch is reviewed against its MR's target branch
    (the parent piece), not against main: only its own commit is reviewed."""
    bindir, canned = _write_gh_shim(tmp_path)
    _git(["switch", "-q", "-c", "piece1"], cwd=repo)
    tip1 = _commit(repo, "p1.txt")
    _git(["push", "-q", "-u", "origin", "piece1"], cwd=repo)
    _git(["switch", "-q", "-c", "piece2"], cwd=repo)
    tip2 = _commit(repo, "p2.txt")
    _github_ify(repo)
    _serve(canned, "list_head", _mr(tip2, target="piece1"))
    proc = _git_mode(repo, [_line(tip2, "piece2")],
                     _env(tmp_path, PATH=minimal_path(str(bindir))))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    (call,) = _trace(tmp_path)
    assert call["range"] == f"{tip1}..{tip2}", call["range"]
    rec = rg_store.load_verdict(_rid(repo), tip2)
    assert rec is not None and rec["base"] == tip1


# --- run-config consumption (story 22-23) ---------------------------------------

def test_depth_and_timeout_config_are_honored(repo, tmp_path):
    """(g) review.depth is announced and re-scopes the review prompt;
    review.timeoutSeconds caps the reviewer call -- a review outliving it is a
    fail-closed block carrying the OCR_FAIL_OPEN bypass route."""
    cfg = tmp_path / "gate-config.json"
    cfg.write_text(json.dumps({"review": {"depth": "structural",
                                          "timeoutSeconds": 2}}))
    tip = _feat_tip(repo)
    env = _env(tmp_path, OCR_CONFIG_FILE=str(cfg), STUB_SLEEP="10")
    proc = _git_mode(repo, [_line(tip)], env)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out
    assert "timed out" in out.lower(), out
    assert "OCR_FAIL_OPEN" in out, out
    assert "structural" in out, out            # the depth advisory
    # And the depth switch stays a no-op for the default:
    tip2 = _commit(repo, "f2.txt")
    cfg.write_text(json.dumps({"review": {"depth": "full", "timeoutSeconds": 0}}))
    proc2 = _git_mode(repo, [_line(tip2)], _env(tmp_path, OCR_CONFIG_FILE=str(cfg)))
    out2 = proc2.stdout + proc2.stderr
    assert proc2.returncode == 0, out2
    assert "review depth" not in out2, out2


def test_config_fail_open_feeds_the_audited_bypass(repo, tmp_path):
    """review.failOpen=true takes pg4's audited escape exactly like the env
    switch: the blocking verdict yields, the bypass lands in the ledger under
    its own mechanism name, and the allow is announced loudly."""
    cfg = tmp_path / "gate-config.json"
    cfg.write_text(json.dumps({"review": {"failOpen": True}}))
    tip = _feat_tip(repo)
    env = _env(tmp_path, OCR_CONFIG_FILE=str(cfg), STUB_VERDICT="block")
    proc = _git_mode(repo, [_line(tip)], env)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "BYPASS" in proc.stderr.upper(), proc.stderr
    entries = _bypasses()
    assert entries and entries[-1]["mechanism"] == "review.failOpen", entries
    assert entries[-1]["tip"] == tip


# --- retroactive discovery of a web-created MR (story 11) -------------------------

def test_web_created_mr_is_announced_first_seen_on_the_next_push(repo, tmp_path):
    """(h) First push: no MR (negative cached). The MR is then created on the
    web (no local event). The next push discovers it: "first seen" announced,
    consistency runs against the existing tip's verdict, and a clean MR reuses
    the recorded verdict with NO new review."""
    bindir, canned = _write_gh_shim(tmp_path)
    tip = _feat_tip(repo)
    _github_ify(repo)
    env = _env(tmp_path, PATH=minimal_path(str(bindir)))
    _serve(canned, "list_head", [])
    proc1 = _git_mode(repo, [_line(tip)], env)
    assert proc1.returncode == 0, proc1.stderr
    assert len(_trace(tmp_path)) == 1
    # The MR page now exists (its recorded head IS the already-pushed tip).
    _serve(canned, "list_head", _mr(tip))
    proc2 = _git_mode(repo, [_line(tip)], env)
    out = proc2.stdout + proc2.stderr
    assert proc2.returncode == 0, out
    assert "first seen" in out, out
    assert "!42" in out, out
    assert len(_trace(tmp_path)) == 1, "an existing verdict was re-reviewed"
