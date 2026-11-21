"""Review base when the target branch was merged in, and the target-branch fetch.

A 1000-line branch with 2000 lines of main merged into it must be reviewed as
about 1000 lines (spec stories 1-7):
  - on the open-MR path the gate first fetches the MR's target branch (only
    that one, bounded, fail-open) so a stale local `origin/<target>` does not
    widen the range;
  - on the fallback path the newest of chained boundaries is the base.

Real git (bare origin + clones), a stub `glab` on a private PATH, the gate run
in-process through the path a push takes (resolve_review_base with the pushed
branch name). The facts a user can see are asserted: the range, the changed
lines, stderr, the explain text and the run-record facts.
"""
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from test_range_explain import (  # noqa: E402,F401
    _glab_mr, _git, _git_mode, _env, _github_ify, _line, _print_range, _shim_path,
    _write_gh_shim, _serve, _mr, gitlab, rg, repo)


def _lines_file(work, name, n):
    (work / name).write_text("".join("%s line %d\n" % (name, i) for i in range(n)))


def _other_clone(work, tmp_path):
    remote = _git(["config", "remote.origin.url"], cwd=work)
    other = tmp_path / "other"
    _git(["clone", "-q", remote, str(other)], cwd=tmp_path)
    _git(["config", "user.email", "o@example.com"], cwd=other)
    _git(["config", "user.name", "o"], cwd=other)
    return other


def _land_on_remote_main(work, tmp_path, lines=2000):
    """Another developer lands `lines` lines on the remote's main."""
    other = _other_clone(work, tmp_path)
    _lines_file(other, "landed.txt", lines)
    _git(["add", "."], cwd=other)
    _git(["commit", "-q", "-m", "landed"], cwd=other)
    _git(["push", "-q", "origin", "main"], cwd=other)
    return _git(["rev-parse", "HEAD"], cwd=other)


def _merged_in_branch(work, tmp_path, stale):
    """feat = 1000 own lines, then main (2000 landed lines) merged in.
    Returns (fork, landed, tip). stale=True rewinds the local origin/main to the
    fork afterwards, as if the developer merged from the remote but never ran
    a plain fetch of their own."""
    fork = _git(["rev-parse", "HEAD"], cwd=work)
    _git(["switch", "-q", "-c", "feat"], cwd=work)
    _lines_file(work, "mine.txt", 1000)
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "mine"], cwd=work)
    landed = _land_on_remote_main(work, tmp_path)
    _git(["fetch", "-q", "origin", "main"], cwd=work)
    _git(["merge", "-q", "--no-ff", "-m", "merge main", "origin/main"], cwd=work)
    if stale:
        _git(["update-ref", "refs/remotes/origin/main", fork], cwd=work)
    return fork, landed, _git(["rev-parse", "HEAD"], cwd=work)


def _lines(work, base, tip):
    return rg._mr_diff_facts(str(work), base, tip)[1]


def _decide(work, tip):
    return rg.resolve_review_base("origin", tip, None, str(work), branch="feat")


# --- ticket 02: the newest chained boundary is the base ------------------------------

def test_main_merged_in_with_fresh_refs_reviews_only_the_branch_without_a_forge(gitlab, tmp_path):  # noqa: F811
    work, _canned = gitlab  # no open MR: the fallback path decides
    fork, landed, tip = _merged_in_branch(work, tmp_path, stale=False)
    rb = _decide(work, tip)
    assert rb.rule == "boundary-chain", rb
    assert rb.base_sha == landed
    assert "chain" in rb.reason and "merged into this branch" in rb.reason
    assert _lines(work, rb.base_sha, tip) == 1000   # old: fork..tip carried 3000
    assert rb.commits == 2                           # the branch's commit and the merge


def test_separate_line_boundaries_keep_the_merge_base_across_them(gitlab, tmp_path):  # noqa: F811
    work, _canned = gitlab
    fork = _git(["rev-parse", "HEAD"], cwd=work)
    _git(["switch", "-q", "-c", "pr-a"], cwd=work)
    _lines_file(work, "a1.txt", 10)
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "a1"], cwd=work)
    _git(["push", "-q", "-u", "origin", "pr-a"], cwd=work)  # the parent is published
    _git(["switch", "-q", "-c", "feat"], cwd=work)
    _lines_file(work, "mine.txt", 100)
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "mine"], cwd=work)
    landed = _land_on_remote_main(work, tmp_path, lines=200)  # main moves on separately
    _git(["fetch", "-q", "origin", "main"], cwd=work)
    _git(["merge", "-q", "--no-ff", "-m", "merge main", "origin/main"], cwd=work)
    tip = _git(["rev-parse", "HEAD"], cwd=work)

    rb = _decide(work, tip)
    # pr-a and main are two lines of history: the merge-base across them, never
    # the newest of the two (documented limit: stacked parents over-review).
    assert rb.rule == "boundary", rb
    assert rb.base_sha == fork and landed != fork


# --- ticket 03: the target branch is fetched before the merge-base --------------------

def test_stale_target_ref_is_fetched_so_the_range_is_only_the_branch(gitlab, tmp_path, capsys):  # noqa: F811
    work, canned = gitlab
    fork, landed, tip = _merged_in_branch(work, tmp_path, stale=True)
    (canned / "out").write_text(_glab_mr(tip))
    assert _git(["rev-parse", "refs/remotes/origin/main"], cwd=work) == fork   # stale going in

    rb = _decide(work, tip)
    assert rb.rule == "mr-target", rb
    assert rb.base_sha == landed                      # old: the stale ref's merge-base, the fork
    assert _lines(work, rb.base_sha, tip) == 1000     # old: 3000
    assert _git(["rev-parse", "refs/remotes/origin/main"], cwd=work) == landed
    facts = rg._base_facts(str(work), tip, rb.base_sha)
    assert facts["target_fetch_state"] == "fetched"
    assert "fetched origin/main" in facts["target_fetch_line"], facts
    assert fork[:8] in facts["target_fetch_line"] and landed[:8] in facts["target_fetch_line"]
    assert facts["target_ref_state"] == "fresh"
    assert "WARNING" not in capsys.readouterr().err


def test_a_current_target_ref_is_fetched_without_a_change(gitlab, tmp_path):  # noqa: F811
    work, canned = gitlab
    fork, landed, tip = _merged_in_branch(work, tmp_path, stale=False)
    (canned / "out").write_text(_glab_mr(tip))
    rb = _decide(work, tip)
    assert rb.rule == "mr-target" and rb.base_sha == landed
    assert _lines(work, rb.base_sha, tip) == 1000
    facts = rg._base_facts(str(work), tip, rb.base_sha)
    assert facts["target_fetch_state"] == "unchanged"
    assert "already up to date" in facts["target_fetch_line"]


def test_a_failing_fetch_warns_falls_back_to_the_local_ref_and_the_push_continues(
        gitlab, tmp_path, capsys):  # noqa: F811
    work, canned = gitlab
    fork, landed, tip = _merged_in_branch(work, tmp_path, stale=True)
    (canned / "out").write_text(_glab_mr(tip))
    _git(["remote", "set-url", "origin", str(tmp_path / "gone.git")], cwd=work)  # unreachable

    rb = _decide(work, tip)
    err = capsys.readouterr().err
    assert rb.rule == "mr-target" and rb.base_sha == fork   # the stale local ref, as before
    assert "WARNING: could not fetch origin/main" in err and "may be stale" in err, err
    facts = rg._base_facts(str(work), tip, rb.base_sha)
    assert facts["target_fetch_state"] == "failed"
    assert facts["target_ref_state"] == "unknown"
    assert "could not fetch origin/main" in facts["target_ref_line"]
    assert _git(["rev-parse", "refs/remotes/origin/main"], cwd=work) == fork   # untouched


def test_a_fetch_that_times_out_falls_back_and_warns(gitlab, tmp_path, capsys, monkeypatch):  # noqa: F811
    work, canned = gitlab
    fork, landed, tip = _merged_in_branch(work, tmp_path, stale=True)
    (canned / "out").write_text(_glab_mr(tip))
    real_run = subprocess.run
    seen = []

    def slow_fetch(cmd, *a, **kw):
        if isinstance(cmd, list) and "fetch" in cmd:
            seen.append(kw.get("timeout"))
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(rg.subprocess, "run", slow_fetch)
    rb = _decide(work, tip)
    err = capsys.readouterr().err
    assert seen == [10]                       # bounded: the 10 s limit
    assert rb.base_sha == fork                # the local ref
    assert "timed out after 10s" in err and "may be stale" in err, err


def test_only_the_target_branch_is_fetched(gitlab, tmp_path):  # noqa: F811
    work, canned = gitlab
    fork, landed, tip = _merged_in_branch(work, tmp_path, stale=True)
    (canned / "out").write_text(_glab_mr(tip))
    other = tmp_path / "other"   # the clone _land_on_remote_main made
    _git(["switch", "-q", "-c", "side"], cwd=other)
    _lines_file(other, "side.txt", 3)
    _git(["add", "."], cwd=other)
    _git(["commit", "-q", "-m", "side"], cwd=other)
    _git(["tag", "side-tag"], cwd=other)
    _git(["push", "-q", "origin", "side", "side-tag"], cwd=other)
    before = _git(["for-each-ref", "refs/remotes", "refs/tags"], cwd=work)

    _decide(work, tip)
    after = _git(["for-each-ref", "refs/remotes", "refs/tags"], cwd=work)
    changed = {ln for ln in after.splitlines()} ^ {ln for ln in before.splitlines()}
    assert {ln.split()[-1] for ln in changed} == {"refs/remotes/origin/main"}, changed
    assert "side" not in after and "side-tag" not in after


def test_no_merge_request_means_no_fetch(gitlab, tmp_path):  # noqa: F811
    work, canned = gitlab  # the forge lists no open MR
    fork, landed, tip = _merged_in_branch(work, tmp_path, stale=True)
    before = _git(["for-each-ref"], cwd=work)
    rb = _decide(work, tip)
    assert rb.rule == "boundary"
    assert _git(["for-each-ref"], cwd=work) == before      # nothing fetched
    assert "fetch" not in rb.detail
    assert rg._base_facts(str(work), tip, rb.base_sha)["target_fetch_state"] == "not-run"


def test_print_range_explains_the_fetch_it_did_not_make(gitlab, tmp_path, capsys):  # noqa: F811
    work, canned = gitlab
    fork, landed, tip = _merged_in_branch(work, tmp_path, stale=True)
    (canned / "out").write_text(_glab_mr(tip))
    rc, out, err = _print_range(work, capsys)
    assert rc == 0, err
    assert "target branch not fetched: --print-range changes nothing" in err, err
    assert "STALE" in err, err                                         # the existing advice
    assert _git(["rev-parse", "refs/remotes/origin/main"], cwd=work) == fork   # read-only


def test_a_real_push_records_the_fetch_outcome(repo, tmp_path, monkeypatch):  # noqa: F811
    """Git mode, GitHub origin whose URL no one can reach: the fetch fails, the
    push is not blocked, and the run record and stderr say so."""
    monkeypatch.setenv("OCR_STATE_DIR", str(tmp_path / "state"))
    bindir, canned = _write_gh_shim(tmp_path)
    main = _git(["rev-parse", "HEAD"], cwd=repo)
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    (repo / "f1.txt").write_text("code\n")
    _git(["add", "."], cwd=repo)
    _git(["commit", "-q", "-m", "f1"], cwd=repo)
    tip = _git(["rev-parse", "HEAD"], cwd=repo)
    _github_ify(repo)
    _serve(canned, "list_head", _mr(tip))
    env = _env(tmp_path, PATH=_shim_path(str(bindir)), GIT_ASKPASS="echo")
    proc = _git_mode(repo, [_line(tip)], env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "WARNING: could not fetch origin/main" in proc.stderr, proc.stderr
    st = rg._read_state(rg._state_path(rg._git_common_dir(str(repo)), tip, main))
    assert st["base_rule"] == "mr-target"
    assert st["target_fetch_state"] == "failed"
    assert "could not fetch origin/main" in st["target_fetch_line"]
