"""Commit intent (pushed commit messages -> review payload).

The gate collects the pushed commits' messages (subjects + bodies + the files
each commit touched) in one `git log` call over the push range, and injects
them through the manifest-extras seam as `commit_intent`: verbatim for the
newest commits up to a budget, subjects-only beyond it with a visible trim
marker, and scoped per chunk by touched-file intersection -- a chunk whose
files no commit touched gets subjects-only background. Untrusted,
author-controlled claims; never instructions.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_async_gate as ag  # noqa: E402
from test_async_gate import review_gate  # noqa: E402


def _unit_repo(tmp_path):
    """Repo whose history is built commit by commit for the collection tests."""
    work = tmp_path / "unit"
    work.mkdir(parents=True)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    for k, v in (("user.email", "t@t.com"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        _git(["config", k, v], cwd=work)
    hooks = tmp_path / "no-hooks"
    hooks.mkdir(exist_ok=True)
    _git(["config", "core.hooksPath", str(hooks)], cwd=work)
    return work


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def _commit(work, files, subject, body=""):
    for name, content in files.items():
        p = work / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(["add", "."] , cwd=work)
    args = ["commit", "-q", "-m", subject] + (["-m", body] if body else [])
    _git(args, cwd=work)
    return _git(["rev-parse", "HEAD"], cwd=work)


def _subjects(extra):
    return [s["subject"] for s in extra["subject_only"]]


# --- collection (unit) --------------------------------------------------------

def test_collects_subjects_bodies_and_touched_files_verbatim_over_a_range(tmp_path):
    work = _unit_repo(tmp_path)
    base = _commit(work, {"a.py": "x = 1\n"}, "base commit", "the seed")
    _commit(work, {"a.py": "x = 2\n"}, "fix the overflow in the retry backoff",
            "The counter wrapped at 2**31.\nClamp it instead.")
    tip = _commit(work, {"b/util.py": "def u():\n    return 1\n"}, "add util helper")
    commits = review_gate._collect_commit_intent(str(work), f"{base}..{tip}")
    assert [c["subject"] for c in commits] == [
        "add util helper", "fix the overflow in the retry backoff"]  # newest first
    fix = commits[1]
    assert "The counter wrapped at 2**31." in fix["body"] and "Clamp it" in fix["body"]
    assert fix["files"] == ["a.py"] and commits[0]["files"] == ["b/util.py"]
    assert commits[0]["sha"] == tip and len(fix["sha"]) == 40


def test_collect_is_fail_open_on_an_unevaluable_range(tmp_path):
    work = _unit_repo(tmp_path)
    _commit(work, {"a.py": "x = 1\n"}, "only")
    assert review_gate._collect_commit_intent(str(work), "") is None
    assert review_gate._collect_commit_intent(str(work), "deadbeef..cafe0000") is None


# --- rendering / truncation (unit) --------------------------------------------

def _mk(n, body_chars=0, prefix="c"):
    """n synthetic commits, newest first (as _collect_commit_intent returns)."""
    out = []
    for i in range(n):
        out.append({"sha": f"{i:040x}", "subject": f"{prefix}{n - 1 - i} subject",
                    "body": "b" * body_chars, "files": [f"f{n - 1 - i}.py"]})
    return out


def test_newest_ten_verbatim_then_subjects_only_with_a_trim_marker():
    extra = review_gate._intent_extra(_mk(14), None, "A..B")
    assert len(extra["commits"]) == review_gate._INTENT_VERBATIM_COMMITS == 10
    assert extra["commit_count"] == 14 and extra["trimmed"] is True
    assert _subjects(extra) == [f"c{i} subject" for i in (3, 2, 1, 0)]
    assert "4" in extra["trim_notice"] and "subject" in extra["trim_notice"].lower()
    # Full entries carry the body and the touched files; tail entries do not.
    assert extra["commits"][0]["body"] == "" and extra["commits"][0]["files"]
    assert set(extra["subject_only"][0]) == {"sha", "subject"}


def test_token_budget_trims_before_the_commit_cap():
    # ~4000 token budget at 4 chars/token: three ~3000-char bodies overflow it.
    extra = review_gate._intent_extra(_mk(6, body_chars=3000), None, "A..B")
    assert 1 <= len(extra["commits"]) < 6 and extra["trimmed"] is True
    assert extra["commits"]  # at least the newest message is always verbatim
    assert len(extra["subject_only"]) == 6 - len(extra["commits"])


def test_newest_commit_over_the_budget_still_sets_a_trim_notice():
    # The newest message is verbatim even when it alone overflows the budget
    # (~4000 tokens at 4 chars/token): keeping it must still be VISIBLE --
    # spec story 3: truncation is never silent.
    extra = review_gate._intent_extra(_mk(1, body_chars=20000), None, "A..B")
    assert len(extra["commits"]) == 1 and "b" * 100 in extra["commits"][0]["body"]
    assert extra["subject_only"] == []
    assert extra["trim_notice"], "an over-budget newest commit must be marked"
    assert "exceed" in extra["trim_notice"]
    assert "kept anyway" in extra["trim_notice"] and "1" in extra["trim_notice"]


def test_no_intent_when_there_are_no_commits():
    assert review_gate._intent_extra([], None, "A..B") is None
    assert review_gate._intent_extra(None, None, "A..B") is None


# --- per-chunk scoping (unit) -------------------------------------------------

def _scoped_commits():
    return _mk_scoped([(["src/a.py"], "core change"), (["docs/b.md"], "docs change"),
                       (["src/c.py"], "oldest")])


def _mk_scoped(specs):
    """[(files, subject)] newest-first -> newest-first commit dicts."""
    return [{"sha": f"{i:040x}", "subject": subj, "body": f"body of {subj}",
             "files": list(files)}
            for i, (files, subj) in enumerate(specs)]


def test_a_chunk_sees_in_full_only_the_commits_that_touched_its_files():
    extra = review_gate._intent_extra(_scoped_commits(), {"src/a.py"}, "A..B")
    assert [c["subject"] for c in extra["commits"]] == ["core change"]
    assert extra["commits"][0]["body"] == "body of core change"
    # The rest stay around as subjects-only background, not silently dropped.
    assert sorted(_subjects(extra)) == ["docs change", "oldest"]
    assert extra["trimmed"] is False and extra["trim_notice"] == ""


def test_a_zero_intersection_chunk_gets_subjects_only_background():
    extra = review_gate._intent_extra(_scoped_commits(), {"unrelated/z.py"}, "A..B")
    assert extra["commits"] == []
    assert sorted(_subjects(extra)) == ["core change", "docs change", "oldest"]


def test_a_file_less_commit_counts_as_touching_every_chunk():
    # Merge commits under plain `git log --name-only` carry an empty files
    # list: without special-casing they would zero-intersect every chunk and
    # land subjects-only everywhere, even when inside the verbatim budget.
    commits = _mk_scoped([([], "merge branch 'feature'"), (["src/c.py"], "oldest")])
    extra = review_gate._intent_extra(commits, {"unrelated/z.py"}, "A..B")
    # The file-less commit is a verbatim candidate in EVERY chunk; the real
    # zero-intersection commit still degrades to subjects-only background.
    assert [c["subject"] for c in extra["commits"]] == ["merge branch 'feature'"]
    assert extra["commits"][0]["body"] == "body of merge branch 'feature'"
    assert _subjects(extra) == ["oldest"]


def test_single_context_scoping_is_a_no_op_over_the_full_set():
    extra = review_gate._intent_extra(_scoped_commits(), None, "A..B")
    assert [c["subject"] for c in extra["commits"]] == [
        "core change", "docs change", "oldest"]
    assert extra["subject_only"] == [] and extra["trimmed"] is False


# --- end to end (hook + stub reviewer, manifests from the trace) --------------

def _repo(tmp_path, commits):
    """Bare remote + work repo, one pushed base commit, then `commits` unpushed:
    [(files, subject, body)]."""
    remote, work = tmp_path / "origin.git", tmp_path / "work"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    _git(["init", "-b", "main", str(work)], cwd=tmp_path)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"),
                 ("commit.gpgsign", "false")):
        _git(["config", k, v], cwd=work)
    hooks = tmp_path / "no-hooks"
    hooks.mkdir(exist_ok=True)
    _git(["config", "core.hooksPath", str(hooks)], cwd=work)
    (work / "base.py").write_text("# base\n")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "base"], cwd=work)
    _git(["remote", "add", "origin", str(remote)], cwd=work)
    _git(["push", "-q", "-u", "origin", "main"], cwd=work)
    for files, subject, body in commits:
        _commit(work, files, subject, body)
    return work


def _reviews(tmp_path):
    return [c for c in ag._trace(tmp_path) if not c.get("resolve_file")]


def _push(work, env):
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    decision, reason, _, _ = ag._hook(work, "git push origin main", env)
    assert decision == "pass", reason
    ag._wait_state(work, tip, {"done"})


def test_a_multi_commit_push_carries_the_fenced_messages_in_its_manifest(tmp_path):
    work = _repo(tmp_path, [
        ({"src/a.py": "x = 1\n"}, "add the parser", "Recursive descent, no deps."),
        ({"src/b.py": "y = 2\n"}, "fix the overflow in the retry backoff",
         "The counter wrapped at 2**31. Clamp it instead."),
    ])
    _push(work, ag._env(tmp_path))
    (call,) = _reviews(tmp_path)
    intent = call["manifest"].get("commit_intent")
    assert intent, "single-context manifest must carry commit_intent"
    assert intent["commit_count"] == 2 and intent["trimmed"] is False
    by_subject = {c["subject"]: c for c in intent["commits"]}
    assert by_subject["fix the overflow in the retry backoff"]["body"] == \
        "The counter wrapped at 2**31. Clamp it instead."
    assert by_subject["fix the overflow in the retry backoff"]["files"] == ["src/b.py"]
    assert by_subject["add the parser"]["body"] == "Recursive descent, no deps."


def test_intent_messages_off_skips_collection_entirely(tmp_path):
    work = _repo(tmp_path, [
        ({"src/b.py": "y = 2\n"}, "fix the overflow", "body"),
    ])
    (work / ".ocr").mkdir()
    (work / ".ocr" / "config.json").write_text(
        json.dumps({"intent": {"messages": False}}))
    _push(work, ag._env(tmp_path))
    (call,) = _reviews(tmp_path)
    assert "commit_intent" not in call["manifest"], (
        "intent.messages off must leave the payload without commit_intent")


def test_a_single_commit_push_behaves_as_before_plus_its_message(tmp_path):
    work = _repo(tmp_path, [
        ({"mod0.py": "x = 1\n", "mod1.py": "y = 2\n"}, "add two modules", "both"),
    ])
    _push(work, ag._env(tmp_path))
    (call,) = _reviews(tmp_path)
    manifest = call["manifest"]
    intent = manifest["commit_intent"]
    assert [c["subject"] for c in intent["commits"]] == ["add two modules"]
    assert intent["commits"][0]["body"] == "both" and intent["trimmed"] is False
    # The rest of the manifest is untouched: the diffs still arrive as items.
    items = [i for i in manifest["items"] if i["kind"] == "file_diff"]
    assert sorted(i["path"] for i in items) == ["mod0.py", "mod1.py"]


def test_a_chunked_run_scopes_the_messages_to_each_chunks_files(tmp_path):
    work = _repo(tmp_path, [
        ({"mod0.py": "x = 1\n", "mod1.py": "x = 1\n"}, "core: add mod0 and mod1",
         "core work"),
        ({"mod2.py": "x = 2\n", "mod3.py": "x = 3\n"}, "docs: add mod2 and mod3",
         "docs work"),
    ])
    env = ag._chunk_env(tmp_path)          # 4 files -> 4 one-file chunks
    env["OCR_CHUNK_CONCURRENCY"] = "1"     # deterministic single slot
    _push(work, env)
    calls = _reviews(tmp_path)
    assert len(calls) == 4
    by_path = {}
    for c in calls:
        m = c["manifest"]
        (path,) = m["paths"]
        by_path[path] = m["commit_intent"]
    core = by_path["mod0.py"]
    assert [c["subject"] for c in core["commits"]] == ["core: add mod0 and mod1"]
    assert _subjects(core) == ["docs: add mod2 and mod3"]
    docs = by_path["mod3.py"]
    assert [c["subject"] for c in docs["commits"]] == ["docs: add mod2 and mod3"]
    assert _subjects(docs) == ["core: add mod0 and mod1"]
    # Both halves of the push are visible in full somewhere; bodies verbatim.
    assert core["commits"][0]["body"] == "core work"
