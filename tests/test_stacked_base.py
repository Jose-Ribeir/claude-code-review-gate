"""Review base selection for stacked and forked branches (0.13.0).

Real git throughout: a bare origin and a working clone, with real refs. Git
mode goes through `_range_for_refs` / `resolve_review_base` (what the pre-push
adapter feeds them), hook mode through `_hook_target` (what the PreToolUse
adapter reads from the command).

Every test asserts something the pre-0.13.0 selection got wrong, so each one
fails under OCR_BASE_V1=1:
  - a new ref used the literal `origin/main` as a tree base, so whatever main
    gained after the fork showed up reversed (phantom deletions) and every
    unmerged parent of a stack was re-reviewed;
  - a non-fast-forward remote sha was used raw as the tree base;
  - the state cache was keyed on the tip alone.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")
_STUB = os.path.join(_HERE, "stub_reviewer.py")

sys.path.insert(0, _SCRIPTS)
from hook_output import parse_pretooluse  # noqa: E402
_spec = importlib.util.spec_from_file_location("review_gate_stacked", _GATE)
rg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rg)

ZERO = "0" * 40
SHA40 = re.compile(r"[0-9a-f]{40}")


def _git(args, cwd, check=True):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                          check=check).stdout.strip()


def _config(work):
    _git(["config", "user.email", "t@example.com"], cwd=work)
    _git(["config", "user.name", "t"], cwd=work)
    _git(["config", "commit.gpgsign", "false"], cwd=work)


@pytest.fixture
def repo(tmp_path):
    """A clone with two files on a pushed main (`a.txt`, `chat.txt`) and a bare origin."""
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
    (work / "chat.txt").write_text("chat\n")
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


def _sha(work, rev):
    return _git(["rev-parse", rev], cwd=work)


def _split(rng):
    base, _, tip = rng.partition("..")
    return base, tip


def _diff_names(work, rng, *flags):
    out = _git(["diff", "--name-status", *flags, rng], cwd=work)
    return {line.split("\t", 1)[1]: line.split("\t", 1)[0] for line in out.splitlines() if line}


def _advance_main(work, name="voice.txt"):
    """Land a commit on main AFTER a branch was cut, as another PR merging would."""
    _git(["switch", "-q", "main"], cwd=work)
    sha = _commit(work, name)
    _git(["push", "-q", "origin", "main"], cwd=work)
    return sha


# --- git mode ------------------------------------------------------------------

def test_new_ref_cut_from_moved_main_has_no_phantom_deletions(repo):
    fork = _sha(repo, "HEAD")
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    tip = _commit(repo, "feat.txt")
    _advance_main(repo, "voice.txt")  # lands on main after the fork

    rng = rg._range_for_refs([(tip, ZERO)], str(repo), "origin")
    base, rtip = _split(rng)
    assert rtip == tip
    assert base == fork  # the merge-base, not the moved origin/main
    names = _diff_names(repo, rng)
    assert names == {"feat.txt": "A"}  # voice.txt is main's, not this push's
    assert _diff_names(repo, rng, "--diff-filter=D") == {}


def test_stacked_child_reviews_only_its_own_commits_git_mode(repo):
    _git(["switch", "-q", "-c", "pr-a"], cwd=repo)
    a_tip = _commit(repo, "a1.txt")
    _git(["push", "-q", "-u", "origin", "pr-a"], cwd=repo)  # the parent is published
    _git(["switch", "-q", "-c", "pr-b"], cwd=repo)
    b_tip = _commit(repo, "b1.txt")

    rng = rg._range_for_refs([(b_tip, ZERO)], str(repo), "origin")
    assert rng == f"{a_tip}..{b_tip}"  # old: origin/main..tip re-reviewed a1.txt
    assert _diff_names(repo, rng) == {"b1.txt": "A"}
    rb = rg.resolve_review_base("origin", b_tip, None, str(repo))
    assert rb.base_sha == a_tip and rb.commits == 1 and rb.has_commits


def test_stacked_child_reviews_only_its_own_commits_hook_mode(repo):
    _git(["switch", "-q", "-c", "pr-a"], cwd=repo)
    a_tip = _commit(repo, "a1.txt")
    _git(["push", "-q", "-u", "origin", "pr-a"], cwd=repo)
    _git(["switch", "-q", "-c", "pr-b"], cwd=repo)
    b_tip = _commit(repo, "b1.txt")

    decision, info = rg._hook_target(str(repo), "git push -u origin pr-b")
    assert decision == "review", info
    assert info["tip"] == b_tip
    assert info["base"] == a_tip  # old: merge-base with origin/main, i.e. the fork point
    assert info["range"] == f"{a_tip}..{b_tip}"
    assert _diff_names(repo, info["range"]) == {"b1.txt": "A"}


def test_main_merged_into_child_over_reviews_but_never_deletes(repo):
    fork = _sha(repo, "HEAD")
    _git(["switch", "-q", "-c", "child"], cwd=repo)
    _commit(repo, "k1.txt")
    m1 = _advance_main(repo, "voice.txt")
    _git(["switch", "-q", "child"], cwd=repo)
    _git(["merge", "-q", "--no-ff", "-m", "merge main", "main"], cwd=repo)
    tip = _sha(repo, "HEAD")

    rng = rg._range_for_refs([(tip, ZERO)], str(repo), "origin")
    base, _ = _split(rng)
    # Two boundaries (the fork point and main's tip): the octopus merge-base.
    assert base == fork
    names = _diff_names(repo, rng)
    assert "k1.txt" in names
    assert names.get("voice.txt") == "A"  # over-review: main's commit rides along...
    assert not [n for n, st in names.items() if st == "D"]  # ...but nothing is "deleted"
    assert subprocess.run(["git", "merge-base", "--is-ancestor", base, tip], cwd=repo).returncode == 0
    assert m1 != base


def test_squash_merged_parent_falls_back_to_merge_base(repo):
    fork = _sha(repo, "HEAD")
    _git(["switch", "-q", "-c", "pr-a"], cwd=repo)
    _commit(repo, "a1.txt")
    _git(["push", "-q", "-u", "origin", "pr-a"], cwd=repo)
    _git(["switch", "-q", "-c", "pr-b"], cwd=repo)
    b_tip = _commit(repo, "b1.txt")
    # pr-a is squash-merged: main gets a NEW commit with the same content, and
    # the remote branch is deleted and pruned, so nothing on the remote holds a1.
    _git(["switch", "-q", "main"], cwd=repo)
    (repo / "a1.txt").write_text("a1.txt\n")
    _git(["add", "."], cwd=repo)
    _git(["commit", "-q", "-m", "squash of pr-a"], cwd=repo)
    _git(["push", "-q", "origin", "main"], cwd=repo)
    _git(["update-ref", "-d", "refs/remotes/origin/pr-a"], cwd=repo)

    rng = rg._range_for_refs([(b_tip, ZERO)], str(repo), "origin")
    base, _ = _split(rng)
    assert base == fork  # the merge-base; old code used the literal `origin/main`
    rb = rg.resolve_review_base("origin", b_tip, None, str(repo))
    assert rb.base_sha == fork and rb.commits == 2  # a1 + b1: over-review, never a silent skip
    assert _diff_names(repo, rng, "--diff-filter=D") == {}


def test_non_ff_remote_sha_is_a_set_exclusion_not_a_tree_base(repo):
    fork = _sha(repo, "HEAD")
    _git(["switch", "-q", "-c", "theirs"], cwd=repo)
    theirs = _commit(repo, "theirs.txt")  # a commit the remote has but this branch lacks
    _git(["switch", "-q", "main"], cwd=repo)
    _git(["switch", "-q", "-c", "mine"], cwd=repo)
    tip = _commit(repo, "mine.txt")
    _git(["branch", "-D", "theirs"], cwd=repo)  # the object stays (still in the reflog)

    rng = rg._range_for_refs([(tip, theirs)], str(repo), "origin")
    base, _ = _split(rng)
    assert base != theirs  # old: the raw remote sha, whose tree diff reverts theirs.txt
    assert base == fork
    assert _diff_names(repo, rng) == {"mine.txt": "A"}
    assert _diff_names(repo, rng, "--diff-filter=D") == {}


def test_shallow_clone_without_boundary_reviews_whole_tree(repo, tmp_path):
    clone = tmp_path / "shallow"
    _git(["clone", "-q", "--depth", "1", (tmp_path / "origin.git").as_uri(), str(clone)],
         cwd=tmp_path)
    _config(clone)
    for ref in _git(["for-each-ref", "--format=%(refname)", "refs/remotes"], cwd=clone).split():
        _git(["update-ref", "-d", ref], cwd=clone)
    tip = _commit(clone, "new.txt")

    rng = rg._range_for_refs([(tip, ZERO)], str(clone), "origin")
    # Old: no origin/* to name a base -> "" -> the push was waved through unreviewed.
    assert rng == f"{rg._EMPTY_TREE}..{tip}"
    rb = rg.resolve_review_base("origin", tip, None, str(clone))
    assert rb.base_sha == rg._EMPTY_TREE and rb.commits == 2 and rb.has_commits


def test_missing_remote_sha_uses_remote_refs_not_origin_main(repo):
    _git(["switch", "-q", "-c", "pr-a"], cwd=repo)
    a_tip = _commit(repo, "a1.txt")
    _git(["push", "-q", "-u", "origin", "pr-a"], cwd=repo)
    _git(["switch", "-q", "-c", "pr-b"], cwd=repo)
    b_tip = _commit(repo, "b1.txt")
    unknown = "de" * 20  # what the remote reported; this client has never seen it

    rng = rg._range_for_refs([(b_tip, unknown)], str(repo), "origin")
    assert rng == f"{a_tip}..{b_tip}"  # old: origin/main..tip, re-reviewing a1.txt
    assert not rng.startswith("origin")


def test_base_is_always_a_sha(repo):
    """Every selection path yields a 40-hex base (or the empty tree) that is an ancestor of the tip."""
    fork = _sha(repo, "HEAD")
    main_tip = _commit(repo, "m1.txt")  # ahead of origin/main: a fast-forward
    _git(["switch", "-q", "-c", "feat", fork], cwd=repo)
    feat_tip = _commit(repo, "f1.txt")  # diverged from main_tip: a non-ff for any remote sha
    cases = {
        "fast-forward": (main_tip, fork),
        "new ref": (feat_tip, ZERO),
        "missing remote sha": (feat_tip, "ab" * 20),
        "non-ff remote sha": (feat_tip, main_tip),
    }
    for label, (tip, remote_sha) in cases.items():
        rng = rg._range_for_refs([(tip, remote_sha)], str(repo), "origin")
        base, rtip = _split(rng)
        assert rtip == tip, label
        assert SHA40.fullmatch(base) or base == rg._EMPTY_TREE, f"{label}: {base!r}"
        rc = subprocess.run(["git", "merge-base", "--is-ancestor", base, tip], cwd=repo).returncode
        assert rc == 0, f"{label}: base is not an ancestor of the tip"
        rb = rg.resolve_review_base("origin", tip, remote_sha, str(repo))
        assert rb.base_sha == base, label


# --- the cache key -------------------------------------------------------------

def _env(tmp_path):
    env = dict(os.environ)
    for k in ("OCR_IN_REVIEW", "OCR_FAIL_OPEN", "OCR_ADVISORY", "OCR_FORCE_REVIEW",
              "OCR_LEGACY_RANGE", "OCR_INLINE_BUDGET", "STUB_SLEEP", "STUB_VERDICT", "STUB_TRACE"):
        env.pop(k, None)
    env["CLAUDE_PLUGIN_DATA"] = str(tmp_path / "gate-data")
    env["OCR_REVIEWER_CMD"] = f'"{sys.executable}" "{_STUB}"'
    env["STUB_TRACE"] = str(tmp_path / "stub.trace")
    env["OCR_INLINE_BUDGET"] = "30"
    return env


def _hook(work, cmd, env):
    payload = json.dumps({"session_id": "s1", "tool_name": "Bash",
                          "cwd": str(work), "tool_input": {"command": cmd}})
    proc = subprocess.run([sys.executable, _GATE, "--mode", "hook"], input=payload,
                          capture_output=True, text=True, cwd=str(work), env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return parse_pretooluse(proc.stdout)


def _trace(tmp_path):
    p = tmp_path / "stub.trace"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_done_marker_is_not_replayed_under_a_different_base(repo, tmp_path):
    # Path level: the same tip under two bases is two reviews.
    common = rg._git_common_dir(str(repo))
    git_dir = rg._git_dir(str(repo))
    tip, base_a, base_b = "c" * 40, "a" * 40, "b" * 40
    assert rg._state_path(common, tip, base_a) != rg._state_path(common, tip, base_b)
    assert rg._marker_path(git_dir, tip, base_a) != rg._marker_path(git_dir, tip, base_b)
    assert rg._marker_path(git_dir, tip, base_a) != rg._marker_path(git_dir, tip)

    # End to end: review pr-b against its published parent, then drop the parent's
    # tracking ref (a prune after the squash merge). The base moves to the fork
    # point, so the same tip must be reviewed again, not answered from `done`.
    _git(["switch", "-q", "-c", "pr-a"], cwd=repo)
    a_tip = _commit(repo, "a1.txt")
    _git(["push", "-q", "-u", "origin", "pr-a"], cwd=repo)
    _git(["switch", "-q", "-c", "pr-b"], cwd=repo)
    b_tip = _commit(repo, "b1.txt")
    env = _env(tmp_path)

    decision, reason = _hook(repo, "git push -u origin pr-b", env)
    assert decision == "pass", reason
    (first,) = _trace(tmp_path)
    assert first["range"] == f"{a_tip}..{b_tip}"
    state_a = rg._state_path(common, b_tip, a_tip)
    deadline = time.monotonic() + 60
    while (rg._read_state(state_a) or {}).get("state") != "done" and time.monotonic() < deadline:
        time.sleep(0.2)
    assert (rg._read_state(state_a) or {}).get("state") == "done"

    _git(["update-ref", "-d", "refs/remotes/origin/pr-a"], cwd=repo)
    decision, reason = _hook(repo, "git push -u origin pr-b", env)
    assert decision == "pass", reason
    traces = _trace(tmp_path)
    assert len(traces) == 2, "the done verdict of the narrower range was replayed"
    assert traces[1]["range"].endswith(f"..{b_tip}") and not traces[1]["range"].startswith(a_tip)


# --- fail-open paths in push range selection (B-W1-01..03) -------------------------

def _git_mode(work, lines, env, *extra):
    """Run `--mode git` the way scripts/pre-push does: ref updates on stdin."""
    proc = subprocess.run([sys.executable, _GATE, "--mode", "git", *extra],
                          input="".join(line + "\n" for line in lines), capture_output=True,
                          text=True, cwd=str(work), env=env, timeout=120)
    return proc


def test_failed_git_log_of_the_pushed_range_means_carries_commits(repo):
    # B-W1-01: HEAD is level with its upstream, so the HEAD-based check says
    # "nothing to review" -- but the pushed range is unevaluable (git log
    # errors), and unknown must mean "carries commits", never fall through.
    assert rg._has_unpushed_commits(str(repo), "") is False
    bad_range = f"{'e' * 40}..{_sha(repo, 'HEAD')}"  # base object does not exist
    assert rg._has_unpushed_commits(str(repo), bad_range) is True


def test_git_log_timeout_means_carries_commits(repo, monkeypatch):
    real = rg._git

    def flaky(args, cwd=None, *a, **kw):
        if args and args[0] == "log":
            return "", 1  # what _git returns on a timeout
        return real(args, cwd=cwd, *a, **kw)

    monkeypatch.setattr(rg, "_git", flaky)
    assert rg._has_unpushed_commits(str(repo), f"{_sha(repo, 'HEAD')}..{_sha(repo, 'HEAD')}") is True


def test_tags_only_push_of_unpushed_commits_is_reviewed(repo, tmp_path):
    # B-W1-02: `git push origin v1` where v1 sits on an unpushed commit. Only
    # the tag ref is on stdin; HEAD (main) is level with origin/main.
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    tip = _commit(repo, "feat.txt")
    _git(["tag", "v1", tip], cwd=repo)
    _git(["switch", "-q", "main"], cwd=repo)
    env = _env(tmp_path)

    proc = _git_mode(repo, [f"refs/tags/v1 {tip} refs/tags/v1 {ZERO}"], env)
    assert proc.returncode == 0, proc.stderr
    (call,) = _trace(tmp_path)  # old: waved through, the reviewer never ran
    assert call["range"].endswith(f"..{tip}")


def test_review_ref_push_of_unpushed_commits_is_reviewed(repo, tmp_path):
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    tip = _commit(repo, "feat.txt")
    _git(["switch", "-q", "main"], cwd=repo)
    env = _env(tmp_path)

    proc = _git_mode(repo, [f"refs/heads/feat {tip} refs/review/x {ZERO}"], env)
    assert proc.returncode == 0, proc.stderr
    (call,) = _trace(tmp_path)
    assert call["range"].endswith(f"..{tip}")


def test_follow_tags_does_not_make_a_branch_push_multi_ref(repo):
    # The original reason non-branch refs were filtered: a tag on a commit the
    # pushed branch already carries adds nothing to review.
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    tip = _commit(repo, "feat.txt")
    _git(["tag", "-a", "v1", "-m", "v1", tip], cwd=repo)  # annotated: sha is the tag object
    tag_obj = _sha(repo, "refs/tags/v1")
    branches, others = rg._read_push_updates_from(
        [f"refs/heads/feat {tip} refs/heads/feat {ZERO}",
         f"refs/tags/v1 {tag_obj} refs/tags/v1 {ZERO}"])
    rng = rg._range_for_refs(branches, str(repo), "origin", extra=others)
    assert rng not in ("", rg._MULTI_REF)
    assert rng.endswith(f"..{tip}")


def test_tag_on_other_unpushed_commits_beside_a_branch_is_refused(repo):
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    tip = _commit(repo, "feat.txt")
    _git(["switch", "-q", "main"], cwd=repo)
    other = _commit(repo, "other.txt")
    _git(["tag", "v1", other], cwd=repo)
    branches, others = rg._read_push_updates_from(
        [f"refs/heads/feat {tip} refs/heads/feat {ZERO}",
         f"refs/tags/v1 {other} refs/tags/v1 {ZERO}"])
    assert rg._range_for_refs(branches, str(repo), "origin", extra=others) == rg._MULTI_REF


def test_git_mode_excludes_the_pushed_remote_not_the_upstream(repo, tmp_path):
    # B-W1-03: the branch tracks origin; the push goes to `fork`. A commit that
    # only origin holds must not count as already reviewed for fork.
    fork = tmp_path / "fork.git"
    _git(["init", "--bare", "-b", "main", str(fork)], cwd=tmp_path)
    _git(["remote", "add", "fork", str(fork)], cwd=repo)
    _git(["push", "-q", "fork", "main"], cwd=repo)
    base = _sha(repo, "HEAD")
    _git(["switch", "-q", "-c", "other"], cwd=repo)
    x = _commit(repo, "x.txt")
    _git(["push", "-q", "origin", "other"], cwd=repo)  # origin/other holds x; fork does not
    _git(["switch", "-q", "-c", "feature"], cwd=repo)
    tip = _commit(repo, "feature.txt")
    _git(["branch", "-q", "--set-upstream-to=origin/other"], cwd=repo)
    env = _env(tmp_path)
    line = f"refs/heads/feature {tip} refs/heads/feature {ZERO}"  # a new ref on fork

    proc = _git_mode(repo, [line], env, "--remote", "fork")
    assert proc.returncode == 0, proc.stderr
    (call,) = _trace(tmp_path)
    assert call["range"] == f"{base}..{tip}"  # x rides along: fork does not have it
    assert x != base


def test_git_mode_without_a_remote_name_keeps_the_upstream_remote(repo, tmp_path):
    # No name forwarded (an old pre-push copy, or a push to a bare URL): the
    # upstream's remote scopes --remotes=, as before.
    base = _sha(repo, "HEAD")
    _git(["switch", "-q", "-c", "other"], cwd=repo)
    x = _commit(repo, "x.txt")
    _git(["push", "-q", "-u", "origin", "other"], cwd=repo)
    _git(["switch", "-q", "-c", "feature"], cwd=repo)
    tip = _commit(repo, "feature.txt")
    _git(["branch", "-q", "--set-upstream-to=origin/other"], cwd=repo)
    env = _env(tmp_path)
    line = f"refs/heads/feature {tip} refs/heads/feature {ZERO}"

    for extra in ((), ("--remote", "no-such-remote"), ("--remote", "https://example.invalid/r.git")):
        proc = _git_mode(repo, [line], env, *extra)
        assert proc.returncode == 0, proc.stderr
    assert {c["range"] for c in _trace(tmp_path)} == {f"{x}..{tip}"} and base != x
