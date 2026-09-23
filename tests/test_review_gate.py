"""Unit tests for review-gate.py's _extract_json balanced-brace parser."""
import importlib.util
import json
import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
_HERE = os.path.dirname(os.path.abspath(__file__))
_STUB_PATH = os.path.join(_HERE, "stub_reviewer.py")
sys.path.insert(0, _SCRIPTS)  # so review-gate.py's own `from ocr_verdict import ...` resolves

_spec = importlib.util.spec_from_file_location("review_gate", os.path.join(_SCRIPTS, "review-gate.py"))
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)

_extract_json = review_gate._extract_json
_format_reasons = review_gate._format_reasons
compute_verdict = review_gate.compute_verdict


def _fake_popen(seen, stdout='{"findings": []}', stderr="", returncode=0):
    """subprocess.Popen stand-in for _run_review's tests.

    Captures the constructor's positional cmd (as seen["cmd"]) and every
    keyword argument (merged into `seen`) so tests can assert on encoding,
    creationflags, stdin, etc. -- the same thing the old `_fake_run` did for
    subprocess.run, before _run_review moved to Popen so OCR_DEBUG could log a
    line the instant the child is spawned.
    """

    class _FakeProc:
        pid = 4242

        def __init__(self, cmd, **kw):
            seen["cmd"] = cmd
            seen.update(kw)
            self.returncode = returncode

        def communicate(self, timeout=None):
            return stdout, stderr

        def kill(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    return _FakeProc


def test_whole_string_json():
    assert _extract_json('{"findings": []}') == {"findings": []}


def test_fenced_json_block():
    text = 'Here is the verdict:\n```json\n{"findings": [{"severity": "low"}]}\n```\n'
    assert _extract_json(text) == {"findings": [{"severity": "low"}]}


def test_trailing_prose_with_stray_brace_after_json():
    # The bug this parser fixes: a naive find("{")..rfind("}") span would grab
    # the LAST '}' in the whole text -- including the one in the parenthetical
    # below -- and fail to parse. A balanced scan must stop at the object's own
    # matching brace and ignore everything after it.
    text = 'Review complete - verdict pass {"findings": []} (no blocking issues found}'
    assert _extract_json(text) == {"findings": []}


def test_skips_unrelated_json_object_without_findings_key():
    # An earlier JSON-looking value quoted from the reviewed diff (e.g. a config
    # fixture) must not win over the real verdict that follows it.
    text = 'Example fixture: {"severity": "high"}\nActual verdict: {"findings": [{"severity": "high"}]}'
    assert _extract_json(text) == {"findings": [{"severity": "high"}]}


def test_last_findings_object_wins_when_multiple_present():
    text = '{"findings": [{"id": 1}]}\nWait, corrected: {"findings": [{"id": 2}]}'
    assert _extract_json(text) == {"findings": [{"id": 2}]}


def test_no_json_present_returns_none():
    assert _extract_json("Review complete, verdict pass, nothing to report.") is None


def test_empty_string_returns_none():
    assert _extract_json("") is None


def test_malformed_braces_return_none():
    assert _extract_json("{not: valid json at all") is None


def test_format_reasons_full_finding():
    result = {
        "findings": [
            {"severity": "high", "path": "a.py", "start_line": 3, "end_line": 3, "content": "bug"}
        ]
    }
    assert _format_reasons(result) == "  [high] a.py:3 - bug"


def test_format_reasons_missing_fields_flagged_not_blank():
    # A finding can be syntactically valid JSON yet still miss the fields it
    # needs to be actionable (the reviewer skipped them under output-length
    # pressure). The line must say so, not silently print "path:? - " with
    # nothing after the dash, which reads as display truncation rather than a
    # defect in the review itself.
    result = {"findings": [{"severity": "high", "path": "a.py"}]}
    line = _format_reasons(result)
    assert line.startswith("  [high] a.py:? - ")
    assert "reviewer omitted" in line


def test_complete_high_confidence_finding_blocks():
    result = {
        "findings": [
            {
                "severity": "high",
                "confidence": 0.9,
                "path": "a.py",
                "start_line": 3,
                "end_line": 3,
                "content": "real bug",
            }
        ]
    }
    assert compute_verdict(result) == "block"


def test_incomplete_finding_cannot_block_even_at_high_confidence():
    # The exact shape observed in production: severity/confidence/path present
    # (enough to pass the old block check) but content/lines missing -- nothing
    # a human could act on. compute_verdict is the auditable, model-independent
    # decision point (see its own module docstring), so this must be enforced
    # here rather than trusted to the reviewing LLM to self-police.
    result = {
        "findings": [
            {"severity": "high", "confidence": 0.9, "path": "a.py"}
        ]
    }
    assert compute_verdict(result) != "block"


def test_incomplete_finding_still_counts_as_warn():
    # Not actionable enough to block, but still a real signal -- must not be
    # thrown away entirely, only downgraded below the blocking threshold.
    result = {
        "findings": [
            {"severity": "high", "confidence": 0.9, "path": "a.py"}
        ]
    }
    assert compute_verdict(result) == "warn"


def test_finding_with_explicit_null_content_cannot_block():
    # f.get("content", "") only uses the "" default when the key is absent;
    # a JSON null makes it return None, and str(None) is the non-empty string
    # "None" -- a bare truthy check on that would wrongly call it actionable.
    result = {
        "findings": [
            {
                "severity": "high",
                "confidence": 0.9,
                "path": "a.py",
                "start_line": 5,
                "end_line": 6,
                "content": None,
            }
        ]
    }
    assert compute_verdict(result) != "block"


def test_incomplete_finding_with_zero_line_numbers_cannot_block():
    result = {
        "findings": [
            {
                "severity": "high",
                "confidence": 0.9,
                "path": "a.py",
                "start_line": 0,
                "end_line": 0,
                "content": "bug",
            }
        ]
    }
    assert compute_verdict(result) != "block"


# --- marker reaping -----------------------------------------------------------
# Markers are written per reviewed HEAD sha so the paired adapter (Claude Code
# hook vs global git hook) can skip re-reviewing the same push. They were never
# removed, so one file accumulated in the git dir per passing push, forever.

import time  # noqa: E402

_reap_markers = review_gate._reap_markers
_marker_path = review_gate._marker_path
MARKER_PREFIX = review_gate.MARKER_PREFIX
MARKER_TTL = review_gate.MARKER_TTL


def _aged_marker(git_dir, sha, age_seconds):
    path = _marker_path(str(git_dir), sha)
    path.write_text("x", encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def test_reap_removes_expired_markers(tmp_path):
    old = _aged_marker(tmp_path, "a" * 40, MARKER_TTL + 60)
    _reap_markers(str(tmp_path))
    assert not old.exists()


def test_reap_keeps_unexpired_markers_for_other_shas(tmp_path):
    # A fresh marker for another sha is still load-bearing: the paired adapter
    # may be mid-push against a different HEAD.
    fresh = _aged_marker(tmp_path, "b" * 40, 10)
    _reap_markers(str(tmp_path))
    assert fresh.exists()


def test_reap_never_removes_the_marker_just_written(tmp_path):
    # Guards the keep= contract even if the new marker's mtime looks expired
    # (clock skew, or a filesystem with coarse timestamps).
    current = _aged_marker(tmp_path, "c" * 40, MARKER_TTL + 60)
    _reap_markers(str(tmp_path), keep=current)
    assert current.exists()


def test_reap_collects_the_pre_0_3_legacy_markers(tmp_path):
    # Nothing writes scr-reviewed-* any more, but the sweep only globbed the
    # prefixes in use, so every one the old per-commit gate ever wrote is still
    # in .git -- 516 of them in one real repo. Same mtime rule as the rest.
    old = tmp_path / ("scr-reviewed-" + "a" * 40)
    old.write_text("x", encoding="utf-8")
    stamp = time.time() - (MARKER_TTL + 60)
    os.utime(old, (stamp, stamp))
    fresh = tmp_path / ("scr-reviewed-" + "b" * 40)
    fresh.write_text("x", encoding="utf-8")

    _reap_markers(str(tmp_path))
    assert not old.exists()
    assert fresh.exists()  # mtime rule, not a blanket delete


def test_reap_collects_abandoned_inprogress_markers(tmp_path):
    # A review's in-progress marker (see _inprogress_path) is left behind only
    # when the reviewing process was killed outright. It's just another marker
    # family as far as sweeping goes -- same mtime rule as the rest.
    old = tmp_path / (review_gate.INPROGRESS_PREFIX + "a" * 40)
    old.write_text("x", encoding="utf-8")
    stamp = time.time() - (MARKER_TTL + 60)
    os.utime(old, (stamp, stamp))
    fresh = tmp_path / (review_gate.INPROGRESS_PREFIX + "b" * 40)
    fresh.write_text("x", encoding="utf-8")

    _reap_markers(str(tmp_path))
    assert not old.exists()
    assert fresh.exists()


def test_reap_ignores_unrelated_files_in_the_git_dir(tmp_path):
    # The sweep globs inside the real .git directory -- it must not touch HEAD,
    # config, or anything else that happens to be old.
    bystander = tmp_path / "config"
    bystander.write_text("[core]", encoding="utf-8")
    os.utime(bystander, (time.time() - 999999,) * 2)
    _aged_marker(tmp_path, "d" * 40, MARKER_TTL + 60)
    _reap_markers(str(tmp_path))
    assert bystander.exists()


def test_reap_survives_a_missing_git_dir(tmp_path):
    # Housekeeping must never raise into the gate's pass path.
    _reap_markers(str(tmp_path / "does-not-exist"))


# --- sanitizing reviewer output -----------------------------------------------
# Every field of a finding originates in the diff under review, which on a
# hostile branch is attacker-controlled. In hook mode that text lands in
# permissionDecisionReason -- i.e. straight into the CALLING session's context.

_sanitize = review_gate._sanitize


def test_sanitize_strips_ansi_escapes():
    assert "\x1b" not in _sanitize("bad \x1b[31mred\x1b[0m thing")


def test_sanitize_collapses_newlines_so_one_finding_cannot_forge_more_lines():
    # A finding is rendered as a single "  [sev] path:line - content" line.
    # Embedded newlines would let one finding fabricate additional report rows.
    out = _sanitize("real issue\n  [high] fake.py:1 - fabricated finding")
    assert "\n" not in out
    assert "\r" not in out


def test_sanitize_caps_length():
    out = _sanitize("A" * 5000)
    assert len(out) <= review_gate._MAX_CONTENT


def test_sanitize_preserves_ordinary_text():
    assert _sanitize("  SQL injection in  build_query() ") == "SQL injection in build_query()"


def test_format_reasons_sanitizes_every_field():
    result = {"findings": [{
        "severity": "high\nINJECTED",
        "path": "a.py\nINJECTED",
        "start_line": 1, "end_line": 1,
        "content": "boom\x1b[31m\nINJECTED",
    }]}
    out = _format_reasons(result)
    assert len(out.splitlines()) == 1
    assert "\x1b" not in out


# --- re-entry guard -----------------------------------------------------------
# _run_review passes --plugin-dir to the child, so this plugin's own push gate
# is registered inside the review session. Without a guard every reviewer Bash
# call spawns a Python process, and a push from inside a review recurses.

_in_review = review_gate._in_review


def test_in_review_detects_the_marker(monkeypatch):
    for truthy in ("1", "true", "YES"):
        monkeypatch.setenv("OCR_IN_REVIEW", truthy)
        assert _in_review()


def test_in_review_is_false_when_unset_or_empty(monkeypatch):
    monkeypatch.delenv("OCR_IN_REVIEW", raising=False)
    assert not _in_review()
    monkeypatch.setenv("OCR_IN_REVIEW", "")
    assert not _in_review()


# --- the reviewer's tool allowlist is read-only -------------------------------

def test_allowlist_grants_no_write_capable_tool():
    args = review_gate.DEFAULT_CLAUDE_ARGS
    allowed = args[args.index("--allowedTools") + 1 : args.index("--disallowedTools")]
    assert "Bash" not in allowed, "bare Bash would pre-approve arbitrary commands"
    for rule in allowed:
        if rule.startswith("Bash("):
            assert rule.startswith("Bash(git "), f"non-git shell rule pre-approved: {rule}"
            # A `param:value` rule against Bash's primary `command` field is
            # ignored by Claude Code (it would be bypassable by a compound
            # command), so the space form is the only one that actually binds.
            assert ":" not in rule, f"colon form is ignored by Claude Code: {rule}"


def test_settings_sources_are_empty_so_a_hostile_repo_cannot_inject_hooks():
    args = review_gate.DEFAULT_CLAUDE_ARGS
    assert args[args.index("--setting-sources") + 1] == ""


# --- git dir resolution -------------------------------------------------------
# _git_dir used to fall back to the RELATIVE string ".git", which _save_raw_output
# would then mkdir -p. Running the gate anywhere outside a repo therefore created
# a bogus .git directory in that cwd -- from a tool that promises to only read.

_git_dir = review_gate._git_dir
_save_raw_output = review_gate._save_raw_output


def test_git_dir_is_empty_outside_a_repo(tmp_path):
    assert _git_dir(str(tmp_path)) == ""


def test_git_dir_is_absolute_inside_a_repo():
    # --absolute-git-dir, not --git-dir: the latter answers a bare ".git" when
    # cwd is the repo root, which callers would resolve against the WRONG cwd
    # (in hook mode the process inherits Claude Code's cwd, not the repo's).
    repo = os.path.dirname(_SCRIPTS)
    got = _git_dir(repo)
    assert got and os.path.isabs(got)


def test_save_raw_output_creates_no_stray_git_dir(tmp_path):
    _save_raw_output("", "some reviewer output")
    assert not (tmp_path / ".git").exists()
    assert not os.path.exists(os.path.join(os.getcwd(), ".git")) or os.path.isdir(".git")


def test_bytecode_writing_is_disabled():
    # Otherwise every push drops .pyc files into the versioned plugin snapshot,
    # which the plugin manager treats as immutable and sync-local-install diffs.
    assert review_gate.sys.dont_write_bytecode is True


# --- the shipped payload ------------------------------------------------------
# PAYLOAD in sync-local-install.py is an ALLOWLIST, so a newly added component
# directory ships only if someone remembers to list it. commands/ was added in
# 0.3.0 and initially was not, which would have quietly shipped a plugin whose
# /review-gate:doctor did not exist.

import importlib.util as _ilu  # noqa: E402

_sync_spec = _ilu.spec_from_file_location(
    "sync_local_install", os.path.join(_SCRIPTS, "sync-local-install.py")
)
_sync = _ilu.module_from_spec(_sync_spec)
_sync_spec.loader.exec_module(_sync)

_REPO = os.path.dirname(_SCRIPTS)
# Directories Claude Code discovers by convention. If one exists in the repo it
# must be in the payload, or the installed plugin silently lacks that feature.
_COMPONENT_DIRS = ["agents", "commands", "skills", "hooks", ".claude-plugin"]


def test_payload_ships_every_component_directory_that_exists():
    missing = [
        d for d in _COMPONENT_DIRS
        if os.path.isdir(os.path.join(_REPO, d)) and d not in _sync.PAYLOAD
    ]
    assert not missing, f"component dirs missing from PAYLOAD: {missing}"


def test_payload_entries_all_exist():
    absent = [n for n in _sync.PAYLOAD if not os.path.exists(os.path.join(_REPO, n))]
    assert not absent, f"PAYLOAD lists paths that do not exist: {absent}"


def test_payload_ships_the_runtime_scripts_and_the_compat_shim():
    assert "scripts" in _sync.PAYLOAD
    assert "bin" in _sync.PAYLOAD, "the pre-0.3.0 compat shim must still ship"


# --------------------------------------------------------------------------
# Findings persistence. review-gate-last-output.json is overwritten by every
# run and markers used to hold only an epoch float, so a warn/pass verdict's
# findings -- the ones that never stop a push -- were unrecoverable as soon as
# the next review started. These tests pin the "kept forever" contract.
# --------------------------------------------------------------------------
_record_review = review_gate._record_review
_read_history = review_gate._read_history
_findings_log_path = review_gate._findings_log_path
_archive_raw_output = review_gate._archive_raw_output
_prune_history = review_gate._prune_history
_history_dir = review_gate._history_dir
_save_raw_output = review_gate._save_raw_output
_write_marker = review_gate._write_marker
_read_marker = review_gate._read_marker
_prior_findings_note = review_gate._prior_findings_note

_FINDING = {
    "severity": "medium",
    "path": "app/svc.py",
    "start_line": 12,
    "end_line": 12,
    "content": "unchecked index",
    "confidence": 0.6,
}


def _rec(tmp_path, verdict="warn", findings=(_FINDING,), **kw):
    return _record_review(
        str(tmp_path), kw.get("head", "abc1234"), kw.get("branch", "feat/x"),
        kw.get("mode", "git"), verdict, kw.get("advisory", False),
        kw.get("blocked", False), {"findings": list(findings)}, kw.get("raw", "snap.json"),
    )


def test_record_keeps_the_full_finding(tmp_path):
    _rec(tmp_path)
    entry = _read_history(str(tmp_path))[0]
    assert entry["findings"] == [_FINDING]
    assert entry["verdict"] == "warn" and entry["finding_count"] == 1
    assert entry["head"] == "abc1234" and entry["branch"] == "feat/x"
    assert entry["raw"] == review_gate.HISTORY_DIR + "/snap.json"


def test_non_blocking_findings_survive_the_next_review(tmp_path):
    # The whole point: run 2 must not erase run 1, the way last-output.json does.
    _rec(tmp_path, verdict="warn")
    _rec(tmp_path, verdict="pass", findings=())
    entries = _read_history(str(tmp_path))
    assert [e["verdict"] for e in entries] == ["warn", "pass"]
    assert entries[0]["findings"] == [_FINDING]


def test_a_clean_pass_is_still_recorded(tmp_path):
    _rec(tmp_path, verdict="pass", findings=())
    entry = _read_history(str(tmp_path))[0]
    assert entry["finding_count"] == 0 and entry["truncated"] is False


def test_blocked_reviews_are_recorded_too(tmp_path):
    _rec(tmp_path, verdict="block", blocked=True)
    assert _read_history(str(tmp_path))[0]["blocked"] is True


def test_history_limit_returns_the_newest_entries(tmp_path):
    for i in range(5):
        _rec(tmp_path, head="sha%d" % i)
    assert [e["head"] for e in _read_history(str(tmp_path), 2)] == ["sha3", "sha4"]
    assert len(_read_history(str(tmp_path), 0)) == 5


def test_a_corrupt_line_does_not_hide_the_intact_ones(tmp_path):
    _rec(tmp_path, head="good1")
    with _findings_log_path(str(tmp_path)).open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")
    _rec(tmp_path, head="good2")
    assert [e["head"] for e in _read_history(str(tmp_path))] == ["good1", "good2"]


def test_oversized_record_sheds_findings_but_stays_parseable(tmp_path):
    huge = dict(_FINDING, content="x" * 40000)
    _rec(tmp_path, findings=[huge] * 40)
    entry = _read_history(str(tmp_path))[0]
    assert entry["truncated"] is True
    assert entry["finding_count"] == 40  # the count is never falsified
    assert len(entry["findings"]) < 40
    assert len(_findings_log_path(str(tmp_path)).read_text(encoding="utf-8")) <= review_gate._MAX_LOG_LINE + 2


def test_record_outside_a_repo_writes_nothing(tmp_path):
    # git_dir is "" outside a repo; the log must not be created in the cwd.
    assert _record_review("", "sha", "b", "git", "pass", False, False, {"findings": []}, "") is None
    assert not list(tmp_path.iterdir())


def test_read_history_on_a_missing_log_is_empty_not_an_error(tmp_path):
    assert _read_history(str(tmp_path)) == []


def test_raw_output_is_archived_alongside_the_overwritten_copy(tmp_path):
    name = _save_raw_output(str(tmp_path), "RAW", "abcdef1234")
    assert name.endswith("-abcdef1.json")
    assert (tmp_path / "review-gate-last-output.json").read_text(encoding="utf-8") == "RAW"
    assert (_history_dir(str(tmp_path)) / name).read_text(encoding="utf-8") == "RAW"


def test_two_archives_in_the_same_second_do_not_collide(tmp_path):
    a = _archive_raw_output(str(tmp_path), "first", "abcdef1")
    b = _archive_raw_output(str(tmp_path), "second", "abcdef1")
    assert a != b
    assert (_history_dir(str(tmp_path)) / a).read_text(encoding="utf-8") == "first"


def test_archive_never_clobbers_an_existing_snapshot(tmp_path):
    # The name is claimed with O_CREAT|O_EXCL, not exists()-then-write: a name
    # already on disk must be skipped, never overwritten.
    d = _history_dir(str(tmp_path))
    d.mkdir()
    taken = _archive_raw_output(str(tmp_path), "first", "abcdef1")
    (d / taken.replace(".json", "-2.json")).write_text("squatter", encoding="utf-8")

    third = _archive_raw_output(str(tmp_path), "third", "abcdef1")
    assert third not in (taken, taken.replace(".json", "-2.json"))
    assert (d / taken).read_text(encoding="utf-8") == "first"
    assert (d / taken.replace(".json", "-2.json")).read_text(encoding="utf-8") == "squatter"
    assert (d / third).read_text(encoding="utf-8") == "third"


def test_archive_loses_the_race_without_losing_the_other_snapshot(tmp_path, monkeypatch):
    # Simulate the interleaving directly: the first O_EXCL create loses to a
    # concurrent adapter that just took the name. The retry must move on rather
    # than overwrite, which is what the old exists()-then-write could not do.
    d = _history_dir(str(tmp_path))
    d.mkdir()
    real_open = os.open
    state = {"raced": False}

    def _racing_open(path, flags, *a, **kw):
        if not state["raced"] and (flags & os.O_EXCL):
            state["raced"] = True
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("other adapter")  # the loser's target, now taken
            raise FileExistsError(path)
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(os, "open", _racing_open)
    name = _archive_raw_output(str(tmp_path), "mine", "abcdef1")
    monkeypatch.undo()

    assert name.endswith("-2.json")
    assert (d / name).read_text(encoding="utf-8") == "mine"
    assert (d / name.replace("-2.json", ".json")).read_text(encoding="utf-8") == "other adapter"


def test_archived_snapshot_is_not_executable(tmp_path):
    # os.open defaults to 0o777; a data file must not come out executable, and
    # must match the io-layer siblings (_save_raw_output, the findings log).
    if os.name != "posix":
        return  # mode bits are not meaningful on Windows
    name = _archive_raw_output(str(tmp_path), "raw", "abcdef1")
    _rec(tmp_path)
    snapshot = (_history_dir(str(tmp_path)) / name).stat().st_mode & 0o777
    sibling = _findings_log_path(str(tmp_path)).stat().st_mode & 0o777
    assert snapshot & 0o111 == 0
    assert snapshot == sibling


def test_prune_keeps_only_the_newest_snapshots(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_HISTORY_LIMIT", "2")
    d = _history_dir(str(tmp_path))
    d.mkdir()
    for i in range(5):
        f = d / ("snap%d.json" % i)
        f.write_text("x", encoding="utf-8")
        os.utime(f, (1000 + i, 1000 + i))
    _prune_history(d)
    assert sorted(p.name for p in d.glob("*.json")) == ["snap3.json", "snap4.json"]


def test_prune_limit_zero_keeps_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("OCR_HISTORY_LIMIT", "0")
    d = _history_dir(str(tmp_path))
    d.mkdir()
    for i in range(4):
        (d / ("snap%d.json" % i)).write_text("x", encoding="utf-8")
    _prune_history(d)
    assert len(list(d.glob("*.json"))) == 4


def test_rotation_never_touches_the_findings_log(tmp_path, monkeypatch):
    # Snapshots rotate; the findings themselves must not. The log lives beside
    # HISTORY_DIR, not inside it -- this pins that layout.
    monkeypatch.setenv("OCR_HISTORY_LIMIT", "1")
    _rec(tmp_path)
    for i in range(3):
        _archive_raw_output(str(tmp_path), "raw%d" % i, "sha%d" % i)
    assert _findings_log_path(str(tmp_path)).exists()
    assert len(_read_history(str(tmp_path))) == 1


def test_marker_carries_the_findings_of_the_run_that_wrote_it(tmp_path):
    marker = tmp_path / "m"
    _write_marker(marker, "abc", "warn", False, "  [medium] a.py:1 - boom")
    prior = _read_marker(marker)
    assert prior["verdict"] == "warn" and "boom" in prior["reasons"]
    assert "already reviewed at this HEAD" in _prior_findings_note(prior)
    assert "boom" in _prior_findings_note(prior)


def test_legacy_epoch_marker_is_read_as_no_findings(tmp_path):
    marker = tmp_path / "m"
    marker.write_text("1756300000.0", encoding="utf-8")
    assert _read_marker(marker) == {}
    assert _prior_findings_note({}) == ""


def test_unreadable_marker_never_raises(tmp_path):
    assert _read_marker(tmp_path / "does-not-exist") == {}


def test_prior_note_sanitizes_marker_contents(tmp_path):
    # The marker sits in .git; treat its text as untrusted on the way back out.
    note = _prior_findings_note({"verdict": "warn", "reasons": "  [low] a.py:1 - x\x1b[31mred"})
    assert "\x1b" not in note and chr(27) not in note


def test_format_reasons_limit_zero_returns_every_finding():
    findings = [dict(_FINDING, start_line=i, end_line=i) for i in range(30)]
    assert len(_format_reasons({"findings": findings}, limit=0).splitlines()) == 30
    assert len(_format_reasons({"findings": findings}).splitlines()) == 20


# --------------------------------------------------------------------------
# End-to-end wiring of a NON-BLOCKING review: the case that used to leave no
# trace at all. Nothing here runs `claude`; _run_review is stubbed so the test
# exercises the gate's own bookkeeping and exit paths.
# --------------------------------------------------------------------------
import io as _io  # noqa: E402

_WARN_RESULT = {"findings": [dict(_FINDING, content="unchecked index")]}


def _isolate_gate_data(monkeypatch, tmp_path):
    """Point the plugin's scratch dir at tmp_path.

    Load-bearing, not hygiene: parked reports live there, and without this a
    test would read -- and delete -- the pending notes of whatever real session
    happens to be running on the machine executing the suite.
    """
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "gate-data"))


def _pending_files(tmp_path):
    d = tmp_path / "gate-data"
    return sorted(p.name for p in d.glob("pending-*")) if d.is_dir() else []


def _stub_gate(monkeypatch, tmp_path, result=_WARN_RESULT, calls=None):
    """A gate whose review always returns `result`, anchored on tmp_path.

    The two seams are _gate_repo (which repo is being pushed, and whether that
    is even knowable) and _drop_breadcrumb (how delivery later finds out).
    Everything else about the gate is exercised for real.
    """
    monkeypatch.setattr(review_gate, "_write_gate_pointer", lambda: None)
    monkeypatch.setattr(review_gate, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(review_gate, "_gate_repo", lambda payload: (str(tmp_path), False))
    monkeypatch.setattr(review_gate, "_drop_breadcrumb", lambda session_id, repo: None)
    _isolate_gate_data(monkeypatch, tmp_path)
    monkeypatch.setattr(review_gate, "_git_dir", lambda repo_root=None: str(tmp_path))
    monkeypatch.setattr(review_gate, "_git_common_dir", lambda repo_root=None: str(tmp_path))
    monkeypatch.setattr(review_gate, "_head_sha", lambda repo_root=None: "a" * 40)
    monkeypatch.setattr(review_gate, "_branch", lambda repo_root=None: "feat/x")
    monkeypatch.setattr(review_gate, "_has_unpushed_commits", lambda repo_root=None, push_range=None: True)
    # What the push sends: resolved from the command by _hook_target for real;
    # here pinned so the stub gate needs no repository.
    monkeypatch.setattr(review_gate, "_hook_target", lambda repo_root, cmd: (
        "review", {"tip": "a" * 40, "branch": "feat/x", "base": "b" * 40,
                   "range": "b" * 40 + ".." + "a" * 40, "remote": "origin", "dst": "feat/x"}))
    # The stubbed reviewer reads nothing, so "the worktree" is the repo itself.
    monkeypatch.setattr(review_gate, "_make_worktree", lambda repo_root, tip, run_id: repo_root)
    monkeypatch.delenv("OCR_IN_REVIEW", raising=False)
    monkeypatch.delenv("OCR_FORCE_REVIEW", raising=False)
    _inline_supervisor(monkeypatch)

    def _fake_review(repo_root, mode, git_dir=None, head_sha="", push_range=""):
        if calls is not None:
            calls.append(head_sha)
        raw_name = review_gate._save_raw_output(git_dir, json.dumps(result), head_sha)
        return result, True, raw_name

    monkeypatch.setattr(review_gate, "_run_review", _fake_review)


def _inline_supervisor(monkeypatch):
    """Run the detached supervisor synchronously, in this process.

    Production spawns `--mode supervise` as a detached child so the hook can
    return inside the host's wall (see ASYNC_DIR). In-process it sees the same
    monkeypatched _run_review the test installed, and the hook's join finds
    the terminal state on its first poll.
    """
    def _spawn(state_path, run_id, repo_root):
        review_gate._supervise(state_path, run_id)
        return os.getpid()

    monkeypatch.setattr(review_gate, "_spawn_supervisor", _spawn)


def _run_post(monkeypatch, tmp_path, command="git push", session_id="s1", shadowed=False,
              head="a" * 40, omit_session=False):
    """Drive --mode post. Note what is NOT stubbed: any command parsing.

    Delivery reads the breadcrumb the gate left; it never looks at the command
    to work out which repo was pushed. That is the point of the breadcrumb.
    """
    body = {"tool_input": {"command": command}}
    if not omit_session:
        body["session_id"] = session_id
    monkeypatch.setattr(sys, "stdin", _io.StringIO(json.dumps(body)))
    monkeypatch.setattr(review_gate, "_read_breadcrumb", lambda sid: str(tmp_path))
    monkeypatch.setattr(review_gate, "_git_dir", lambda repo_root=None: str(tmp_path))
    monkeypatch.setattr(review_gate, "_head_sha", lambda repo_root=None: head)
    monkeypatch.setattr(review_gate, "_hookspath_shadowed", lambda repo_root: shadowed)
    monkeypatch.setattr(review_gate, "_repo_root", lambda: str(tmp_path))
    _isolate_gate_data(monkeypatch, tmp_path)
    monkeypatch.delenv("OCR_IN_REVIEW", raising=False)
    assert review_gate._mode_post(["review-gate.py", "--mode", "post"]) == 0


def _run_hook(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _io.StringIO('{"tool_input": {"command": "git push"}}'))
    try:
        review_gate._main_inner(["review-gate.py", "--mode", "hook"], "hook")
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("_main_inner did not exit")


def test_passing_review_persists_its_findings_and_reports_them(tmp_path, monkeypatch, capsys):
    _stub_gate(monkeypatch, tmp_path)
    _run_hook(monkeypatch)

    payload = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert payload["permissionDecision"] == "allow"
    # The findings are attached to the allow decision. NOTE: this reaches the
    # hook's own record (visible UI-side, useful when debugging) and NOT the
    # model -- verified 2026-09, an allow produces no `hook_additional_context`
    # companion. Model delivery is --mode post; see the tests at the end of
    # this file. This assertion pins the debugging aid, not a delivery channel.
    assert "unchecked index" in payload["permissionDecisionReason"]

    entry = _read_history(str(tmp_path))[0]
    assert entry["verdict"] == "warn" and entry["blocked"] is False
    assert entry["findings"][0]["content"] == "unchecked index"
    assert (tmp_path / entry["raw"]).exists()


def test_the_paired_adapters_short_circuit_replays_instead_of_silencing(tmp_path, monkeypatch, capsys):
    calls = []
    _stub_gate(monkeypatch, tmp_path, calls=calls)
    _run_hook(monkeypatch)
    capsys.readouterr()

    # Second adapter, same HEAD, marker still fresh: no second review...
    _run_hook(monkeypatch)
    payload = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert calls == ["a" * 40]
    # ...but the first run's findings are shown again rather than swallowed.
    assert "already reviewed at this HEAD" in payload["permissionDecisionReason"]
    assert "unchecked index" in payload["permissionDecisionReason"]


# --- the review runs elsewhere: state file, supervisor, inline join -----------
# The desktop app kills a CLI that is silent for ~16 min, and a PreToolUse
# hook is silent for as long as it runs. So the review runs under a detached
# supervisor and the hook only JOINS it, up to a budget; past the budget it
# denies with "still running" and a retry joins the same review. These tests
# drive that state machine with the supervisor run in-process.

def _state_of(tmp_path, tip="a" * 40):
    return review_gate._read_state(review_gate._state_path(str(tmp_path), tip)) or {}


def _budget_exhausted(monkeypatch):
    """Make the inline budget already spent, so a running review is not
    waited for. Measured from process start, so the clock is moved back."""
    monkeypatch.setattr(review_gate, "_HOOK_T0", time.time() - 10_000)


def test_the_verdict_is_recorded_in_the_state_file(tmp_path, monkeypatch, capsys):
    _stub_gate(monkeypatch, tmp_path)
    _run_hook(monkeypatch)
    st = _state_of(tmp_path)
    assert st["state"] == "done" and st["verdict"] == "warn" and st["blocked"] is False
    assert "unchecked index" in st["reasons"]
    assert st["run_id"] and st["supervisor_pid"] == os.getpid()


def test_a_blocked_verdict_is_replayed_without_a_second_review(tmp_path, monkeypatch, capsys):
    calls = []
    blocked = {"findings": [dict(_FINDING, severity="high", confidence=0.95, content="sql injection")]}
    _stub_gate(monkeypatch, tmp_path, result=blocked, calls=calls)
    _run_hook(monkeypatch)
    first = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert first["permissionDecision"] == "deny" and "sql injection" in first["permissionDecisionReason"]
    # No pass marker for a block: an older git hook reads that marker as pass.
    assert not review_gate._marker_path(str(tmp_path), "a" * 40).exists()

    # Retry within the TTL, same tip: answered from the state file, no review.
    st = _state_of(tmp_path)
    st["done_ts"] = time.time() - 120  # long enough ago to read as a replay
    review_gate._write_state(review_gate._state_path(str(tmp_path), "a" * 40), st)
    _run_hook(monkeypatch)
    second = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert calls == ["a" * 40]
    assert second["permissionDecision"] == "deny"
    assert "review recorded" in second["permissionDecisionReason"]
    assert "sql injection" in second["permissionDecisionReason"]


def test_force_review_ignores_a_recorded_verdict(tmp_path, monkeypatch, capsys):
    calls = []
    _stub_gate(monkeypatch, tmp_path, calls=calls)
    _run_hook(monkeypatch)
    capsys.readouterr()
    monkeypatch.setenv("OCR_FORCE_REVIEW", "1")
    _run_hook(monkeypatch)
    assert calls == ["a" * 40, "a" * 40]


def test_a_running_review_past_the_budget_is_denied_not_allowed(tmp_path, monkeypatch, capsys):
    calls = []
    _stub_gate(monkeypatch, tmp_path, calls=calls)
    _budget_exhausted(monkeypatch)
    # Another process's review, alive: fresh heartbeat.
    path = review_gate._state_path(str(tmp_path), "a" * 40)
    review_gate._write_state(path, {
        "state": "running", "run_id": "other", "tip": "a" * 40, "branch": "feat/x",
        "started_ts": time.time() - 700, "heartbeat_ts": time.time(), "supervisor_pid": 0,
    })
    _run_hook(monkeypatch)
    payload = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert payload["permissionDecision"] == "deny"
    assert "still running" in payload["permissionDecisionReason"]
    assert "re-run this exact `git push`" in payload["permissionDecisionReason"]
    assert calls == []  # joined, never restarted
    assert _state_of(tmp_path)["run_id"] == "other"
    # ...and a note is parked so --mode post can announce the verdict later.
    notes = [json.loads((tmp_path / "gate-data" / n).read_text()) for n in _pending_files(tmp_path)]
    assert [n["kind"] for n in notes] == ["async"]


def test_a_review_whose_supervisor_went_silent_is_restarted(tmp_path, monkeypatch, capsys):
    calls = []
    _stub_gate(monkeypatch, tmp_path, calls=calls)
    path = review_gate._state_path(str(tmp_path), "a" * 40)
    review_gate._write_state(path, {
        "state": "running", "run_id": "dead", "tip": "a" * 40, "branch": "feat/x",
        "started_ts": time.time() - 900, "heartbeat_ts": time.time() - 600, "supervisor_pid": 0,
    })
    _run_hook(monkeypatch)
    payload = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert payload["permissionDecision"] == "allow"
    assert calls == ["a" * 40]
    st = _state_of(tmp_path)
    assert st["state"] == "done" and st["run_id"] != "dead"


def test_a_failed_review_is_retried_once_then_denied_with_its_reason(tmp_path, monkeypatch, capsys):
    calls = []
    _stub_gate(monkeypatch, tmp_path, calls=calls)

    def _raise(*a, **kw):
        raise review_gate.ReviewGateError("claude exited 1 without running the review")

    monkeypatch.setattr(review_gate, "_run_review", _raise)
    _run_hook(monkeypatch)
    first = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert first["permissionDecision"] == "deny"
    assert "could not complete" in first["permissionDecisionReason"]
    assert _state_of(tmp_path)["attempts"] == 1

    _run_hook(monkeypatch)  # automatic retry, fails again
    assert _state_of(tmp_path)["attempts"] == 2
    second = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert "will not be retried automatically" in second["permissionDecisionReason"]

    monkeypatch.setattr(review_gate, "_run_review",
                        lambda *a, **kw: (_WARN_RESULT, True, ""))
    _run_hook(monkeypatch)  # at the cap: no third attempt inside the TTL
    third = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert third["permissionDecision"] == "deny"


def test_a_stale_run_id_fences_the_old_supervisor(tmp_path, monkeypatch):
    # The supervisor re-reads its state file; if a newer run has claimed the
    # tip, it must not write a verdict over the newer run's state.
    _stub_gate(monkeypatch, tmp_path)
    path = review_gate._state_path(str(tmp_path), "a" * 40)
    review_gate._write_state(path, {"state": "claimed", "run_id": "old", "tip": "a" * 40,
                                    "repo_root": str(tmp_path), "git_dir": str(tmp_path)})
    orig = review_gate._run_review

    def _supersede(*a, **kw):
        st = review_gate._read_state(path)
        st["run_id"] = "new"
        review_gate._write_state(path, st)
        return orig(*a, **kw)

    monkeypatch.setattr(review_gate, "_run_review", _supersede)
    assert review_gate._supervise(str(path), "old") == 0
    assert _state_of(tmp_path)["run_id"] == "new"
    assert _state_of(tmp_path).get("state") != "done"


def test_post_announces_an_async_verdict_once(tmp_path, monkeypatch, capsys):
    _stub_gate(monkeypatch, tmp_path)
    _seed_record(tmp_path)
    path = review_gate._state_path(str(tmp_path), "a" * 40)
    review_gate._write_state(path, {"state": "done", "run_id": "r", "tip": "a" * 40,
                                    "branch": "feat/x", "verdict": "warn", "blocked": False,
                                    "done_ts": time.time()})
    review_gate._park_pending("s1", str(tmp_path), "a" * 40, kind="async",
                              extra={"state": str(path)})
    _run_post(monkeypatch, tmp_path, command="ls")
    ctx = _post_context(capsys)
    assert "has finished (verdict: warn)" in ctx and "unchecked index" in ctx
    assert _pending_files(tmp_path) == []
    _run_post(monkeypatch, tmp_path, command="ls")
    assert _post_context(capsys) == ""


def test_post_reminds_about_a_running_review_at_most_every_five_minutes(tmp_path, monkeypatch, capsys):
    _stub_gate(monkeypatch, tmp_path)
    path = review_gate._state_path(str(tmp_path), "a" * 40)
    review_gate._write_state(path, {"state": "running", "run_id": "r", "tip": "a" * 40,
                                    "branch": "feat/x", "started_ts": time.time() - 200,
                                    "heartbeat_ts": time.time()})
    review_gate._park_pending("s1", str(tmp_path), "a" * 40, kind="async",
                              extra={"state": str(path)})
    _run_post(monkeypatch, tmp_path, command="ls")
    assert "still running" in _post_context(capsys)
    _run_post(monkeypatch, tmp_path, command="ls")
    assert _post_context(capsys) == ""  # rate-limited
    assert len(_pending_files(tmp_path)) == 1  # kept until the verdict lands


def test_a_clean_run_records_a_pass_and_says_nothing(tmp_path, monkeypatch, capsys):
    _stub_gate(monkeypatch, tmp_path, result={"findings": []})
    _run_hook(monkeypatch)
    payload = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert payload["permissionDecision"] == "allow"
    assert "permissionDecisionReason" not in payload  # no findings, no noise
    assert _read_history(str(tmp_path))[0]["verdict"] == "pass"


def test_history_command_prints_the_recorded_findings(tmp_path, monkeypatch, capsys):
    _stub_gate(monkeypatch, tmp_path)
    _run_hook(monkeypatch)
    capsys.readouterr()

    assert review_gate._print_history(["review-gate.py", "--history"]) == 0
    out = capsys.readouterr().out
    assert "unchecked index" in out and "feat/x" in out and "warn" in out


# --- --mode post: the only channel that reaches the model ---------------------
# Verified 2026-09 against Claude Code transcripts: a PreToolUse
# permissionDecisionReason on an ALLOW produces no `hook_additional_context`
# record, so it never reaches the model; a PostToolUse additionalContext does.
# These tests pin the behaviour that discovery forced.

def _seed_record(tmp_path, verdict="warn", advisory=False, blocked=False, findings=None,
                 head="a" * 40, count=None):
    result = {"findings": findings if findings is not None else _WARN_RESULT["findings"]}
    review_gate._record_review(
        str(tmp_path), head, "feat/x", "hook", verdict, advisory, blocked, result, ""
    )
    if count is not None:
        # Simulate _record_review shedding findings to fit _MAX_LOG_LINE: the
        # entry keeps the true count but carries fewer (or zero) findings.
        log = review_gate._findings_log_path(str(tmp_path))
        lines = log.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[-1])
        entry["finding_count"] = count
        entry["truncated"] = True
        lines[-1] = json.dumps(entry)
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _post_context(capsys):
    """The additionalContext this run emitted, or "" when it stayed silent."""
    out = capsys.readouterr().out.strip()
    if not out:
        return ""
    payload = json.loads(out)["hookSpecificOutput"]
    assert payload["hookEventName"] == "PostToolUse"
    return payload["additionalContext"]


def test_post_delivers_a_warn_records_findings(tmp_path, monkeypatch, capsys):
    _seed_record(tmp_path)
    _run_post(monkeypatch, tmp_path)
    ctx = _post_context(capsys)
    assert "unchecked index" in ctx and "verdict: warn" in ctx


def test_post_stays_silent_the_second_time_in_one_session(tmp_path, monkeypatch, capsys):
    _seed_record(tmp_path)
    _run_post(monkeypatch, tmp_path)
    assert _post_context(capsys)
    # Same session, same HEAD, same record: already in this context.
    _run_post(monkeypatch, tmp_path)
    assert _post_context(capsys) == ""


def test_post_redelivers_to_a_new_session(tmp_path, monkeypatch, capsys):
    _seed_record(tmp_path)
    _run_post(monkeypatch, tmp_path, session_id="s1")
    assert _post_context(capsys)
    # A different session has a fresh context that genuinely lacks the findings.
    _run_post(monkeypatch, tmp_path, session_id="s2")
    assert "unchecked index" in _post_context(capsys)


def test_post_redelivers_when_the_same_head_is_reviewed_again(tmp_path, monkeypatch, capsys):
    _seed_record(tmp_path)
    _run_post(monkeypatch, tmp_path)
    capsys.readouterr()
    time.sleep(0.01)
    _seed_record(tmp_path)  # a second review of the same HEAD -> new record ts
    _run_post(monkeypatch, tmp_path)
    assert "unchecked index" in _post_context(capsys)


def test_post_reports_a_clean_pass_in_one_line(tmp_path, monkeypatch, capsys):
    # Silence used to be the answer here, and it read as "no review happened" --
    # so the model went and checked the log every time, which is the chore this
    # whole mode exists to remove. One line, and nothing to go and read.
    _seed_record(tmp_path, verdict="pass", findings=[])
    _run_post(monkeypatch, tmp_path)
    ctx = _post_context(capsys)
    assert ctx == "review-gate: pass - no findings."


def test_post_says_a_clean_pass_only_once_per_session(tmp_path, monkeypatch, capsys):
    _seed_record(tmp_path, verdict="pass", findings=[])
    _run_post(monkeypatch, tmp_path)
    assert _post_context(capsys)
    _run_post(monkeypatch, tmp_path)
    assert _post_context(capsys) == ""


def test_post_is_silent_when_no_review_was_recorded_for_this_head(tmp_path, monkeypatch, capsys):
    _seed_record(tmp_path, head="b" * 40)
    _run_post(monkeypatch, tmp_path, head="a" * 40)
    assert _post_context(capsys) == ""


def test_post_is_silent_for_a_non_push_with_nothing_parked(tmp_path, monkeypatch, capsys):
    # A non-push call now looks for an undelivered review instead of returning
    # immediately. With nothing parked it must still say nothing at all -- this
    # is the hot path, running on every Bash call the session makes.
    _seed_record(tmp_path)
    _run_post(monkeypatch, tmp_path, command="git status")
    assert _post_context(capsys) == ""


def test_post_shouts_about_an_advisory_downgraded_block(tmp_path, monkeypatch, capsys):
    # The highest-stakes invisible case: a block-level finding that let the push
    # through because blocking is off. Nothing else stops it.
    _seed_record(tmp_path, verdict="block", advisory=True, blocked=False)
    ctx = (_run_post(monkeypatch, tmp_path), _post_context(capsys))[1]
    assert "BLOCK-level findings, NOT enforced (advisory mode)" in ctx


def test_post_never_renders_an_empty_findings_block_when_the_log_shed_them(
    tmp_path, monkeypatch, capsys
):
    _seed_record(tmp_path, findings=[], count=7)
    _run_post(monkeypatch, tmp_path)
    ctx = _post_context(capsys)
    assert "7 finding(s)" in ctx and "7 more finding(s) not recorded" in ctx
    assert "--history 1" in ctx


def test_post_works_when_the_payload_carries_no_session_id(tmp_path, monkeypatch, capsys):
    _seed_record(tmp_path)
    _run_post(monkeypatch, tmp_path, omit_session=True)
    assert "unchecked index" in _post_context(capsys)


def test_post_warns_once_per_session_that_the_git_adapter_is_shadowed(
    tmp_path, monkeypatch, capsys
):
    _seed_record(tmp_path)
    _run_post(monkeypatch, tmp_path, shadowed=True)
    assert "core.hooksPath" in _post_context(capsys)
    # Static per-repo fact: repeating it every push trains the reader to skip it.
    time.sleep(0.01)
    _seed_record(tmp_path)
    _run_post(monkeypatch, tmp_path, shadowed=True)
    ctx = _post_context(capsys)
    assert "unchecked index" in ctx and "core.hooksPath" not in ctx


def test_post_output_is_pure_ascii(tmp_path, monkeypatch, capsys):
    # This text reaches a Windows terminal via git's stderr in --mode git, where
    # a stray em-dash renders as a replacement character.
    _seed_record(tmp_path, verdict="block", advisory=True)
    _run_post(monkeypatch, tmp_path, shadowed=True)
    _post_context(capsys).encode("ascii")


def test_post_is_inert_inside_the_headless_review_session(tmp_path, monkeypatch, capsys):
    # The plugin is loaded into the review session via --plugin-dir, so this
    # hook is registered there too and would fire on every Bash call it makes.
    _seed_record(tmp_path)
    monkeypatch.setenv("OCR_IN_REVIEW", "1")
    monkeypatch.setattr(sys, "stdin", _io.StringIO('{"tool_input": {"command": "git push"}}'))
    try:
        review_gate.main(["review-gate.py", "--mode", "post"])
    except SystemExit as exc:
        assert exc.code == 0
    assert capsys.readouterr().out.strip() == ""


# --- core.hooksPath shadowing -------------------------------------------------
# A repo-local core.hooksPath overrides the global one, silently taking the git
# adapter out of the chain. install-git-hook.sh sets the GLOBAL value, so this
# is a config-induced fail-open that nothing else announces.

def _shadow_repo(tmp_path, monkeypatch, local, glob_="/g/review-gate/hooks"):
    def _fake_git(args, cwd=None):
        if args[:4] == ["config", "--local", "--get", "core.hooksPath"]:
            return (local, 0) if local else ("", 1)
        if args[:4] == ["config", "--global", "--get", "core.hooksPath"]:
            return (glob_, 0) if glob_ else ("", 1)
        return "", 1
    monkeypatch.setattr(review_gate, "_git", _fake_git)
    return review_gate._hookspath_shadowed(str(tmp_path))


def test_no_local_hookspath_is_not_shadowed(tmp_path, monkeypatch):
    assert _shadow_repo(tmp_path, monkeypatch, local="") is False


def test_no_global_hook_means_there_is_nothing_to_shadow(tmp_path, monkeypatch):
    assert _shadow_repo(tmp_path, monkeypatch, local="/repo/hooks", glob_="") is False


def test_a_repo_local_hookspath_shadows_the_git_adapter(tmp_path, monkeypatch):
    assert _shadow_repo(tmp_path, monkeypatch, local=str(tmp_path / "hooks")) is True


def test_a_hook_that_actually_chains_into_us_is_not_shadowing(tmp_path, monkeypatch):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pre-push").write_text(
        '#!/bin/sh\nexec python "$HOME/x/review-gate.py" --mode git\n', encoding="utf-8"
    )
    assert _shadow_repo(tmp_path, monkeypatch, local=str(hooks)) is False


def test_merely_mentioning_review_gate_in_a_comment_does_not_count_as_chaining(
    tmp_path, monkeypatch
):
    # Regression: the repo that prompted this check has a pre-push whose
    # comments discuss review-gate at length precisely to explain that it does
    # NOT invoke it. A bare substring match read that as "chained" and hid the
    # exact fail-open this function exists to report.
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pre-push").write_text(
        "#!/bin/sh\n"
        "# NOTE: this used to also run a second review pass on top of the\n"
        "# review-gate plugin that already reviews every commit. Removed --\n"
        "# do not re-add without checking whether review-gate is still enabled.\n"
        "run_tests\n",
        encoding="utf-8",
    )
    assert _shadow_repo(tmp_path, monkeypatch, local=str(hooks)) is True


# --- which repository was actually pushed -------------------------------------
# Regression: --mode post resolved the repo from the process cwd, which in a
# hook is the SESSION's directory. Claude Code routinely pushes as
# `cd <repo> && git push`, so a live smoke test delivered a different
# repository's month-old findings as though they described the push that had
# just completed -- worse than silence, because it reads as a real report.

_cd_targets = review_gate._cd_targets


def test_cd_target_is_taken_from_the_command():
    assert _cd_targets("cd /a/b && git push origin main") == ["/a/b"]


def test_cd_target_handles_quoted_paths_with_spaces():
    assert _cd_targets('cd "/a b/c" && git push') == ["/a b/c"]
    assert _cd_targets("cd '/a b/c' && git push") == ["/a b/c"]


def test_the_last_cd_before_the_push_wins():
    # Callers try these in reverse, so the one in effect at push time is last.
    assert _cd_targets("cd /a && cd /b && git push")[-1] == "/b"


def test_cd_after_the_push_is_not_a_target():
    # Only what ran BEFORE the push can have determined where it happened.
    assert _cd_targets("git push && cd /elsewhere") == []


def test_a_command_with_no_cd_has_no_targets():
    assert _cd_targets("git push origin main") == []
    assert _cd_targets("") == []


def test_env_prefixed_push_still_resolves(tmp_path):
    # The shape the user actually reported this bug with.
    cmd = 'cd J:/x && PYTEST_XDIST_AUTO_NUM_WORKERS=4 git push origin main 2>&1 | tail -14'
    assert _cd_targets(cmd) == ["J:/x"]


# --- capture encoding ---------------------------------------------------------
# Regression: both subprocess calls used text=True with no encoding=, so output
# was decoded with locale.getpreferredencoding() -- cp1252 on a default Windows
# box. The reviewer emits UTF-8, so an em-dash in a finding arrived as "a€""
# and was then stored that way in the findings log, the raw snapshot, and the
# context injected into the session. Caught in a live smoke test, not by review.

def test_the_reviewer_subprocess_decodes_as_utf8(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(review_gate.subprocess, "Popen", _fake_popen(seen))
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    review_gate._run_review(str(tmp_path), "hook", str(tmp_path), "a" * 40)
    assert seen.get("encoding") == "utf-8"
    assert seen.get("errors") == "replace"


# --- reviewer process isolation on Windows -------------------------------------
# The reviewer used to inherit the parent's stdin and share its console/process
# group by default (no creationflags/startupinfo were ever set). Isolating it is
# a best-effort mitigation for a session crash whose root cause is unconfirmed
# -- see _WIN_FLAGS -- so these tests pin the mechanism, not a fixed root cause.

def test_reviewer_gets_no_shared_stdin(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(review_gate.subprocess, "Popen", _fake_popen(seen))
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    review_gate._run_review(str(tmp_path), "hook", str(tmp_path), "a" * 40)
    assert seen.get("stdin") is review_gate.subprocess.DEVNULL


def test_reviewer_is_isolated_from_the_console_on_windows(monkeypatch, tmp_path):
    # _WIN_FLAGS itself is computed once at import time from whichever
    # constants this interpreter's subprocess module actually has (0 for both
    # on a non-Windows CI runner, since the Windows-only constants don't exist
    # there at all) -- so this pins the branch (win32 uses _WIN_FLAGS, not a
    # hardcoded value), not the flag bits themselves.
    seen = {}
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(review_gate.subprocess, "Popen", _fake_popen(seen))
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    review_gate._run_review(str(tmp_path), "hook", str(tmp_path), "a" * 40)
    assert seen.get("creationflags") == review_gate._WIN_FLAGS


def test_reviewer_gets_no_special_flags_off_windows(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(review_gate.subprocess, "Popen", _fake_popen(seen))
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    review_gate._run_review(str(tmp_path), "hook", str(tmp_path), "a" * 40)
    assert seen.get("creationflags") == 0


def test_a_cancelled_review_kills_the_child_instead_of_orphaning_it(monkeypatch, tmp_path):
    # A rewrite that only handled TimeoutExpired would leave `claude.exe`
    # running (and burning tokens) whenever this hook is cancelled or errors
    # out for any other reason -- including KeyboardInterrupt, hence the
    # generic exception case below, not just a timeout.
    killed = []

    class _HangingProc:
        pid = 4242
        returncode = None

        def __init__(self, cmd, **kw):
            pass

        def communicate(self, timeout=None):
            raise KeyboardInterrupt()

        def kill(self):
            killed.append(True)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(review_gate.subprocess, "Popen", _HangingProc)
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    try:
        review_gate._run_review(str(tmp_path), "hook", str(tmp_path), "a" * 40)
        raise AssertionError("expected KeyboardInterrupt to propagate")
    except KeyboardInterrupt:
        pass
    assert killed == [True]


def test_a_timed_out_review_kills_the_child_and_fails_closed(monkeypatch, tmp_path):
    class _StuckProc:
        pid = 4242
        returncode = None
        killed = False

        def __init__(self, cmd, **kw):
            pass

        def communicate(self, timeout=None):
            if not self.killed:
                raise review_gate.subprocess.TimeoutExpired(cmd="claude", timeout=timeout)
            return "", ""

        def kill(self):
            type(self).killed = True

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(review_gate.subprocess, "Popen", _StuckProc)
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    try:
        review_gate._run_review(str(tmp_path), "hook", str(tmp_path), "a" * 40)
        raise AssertionError("expected ReviewGateError")
    except review_gate.ReviewGateError as exc:
        assert "timed out" in str(exc)
    assert _StuckProc.killed is True


# --- OCR_UNSET_ENV / the default session-bridge scrub --------------------------
# A live OCR_DEBUG smoke test against a real Claude Code Desktop session (see
# _SESSION_BRIDGE_ENV's comment) confirmed these names are genuinely inherited
# by the reviewer -- no longer a guess -- so they're scrubbed by default.

def _clear_session_bridge_env(monkeypatch):
    """Only OCR_UNSET_ENV itself is under test here -- make sure none of the
    real vars this default list names happen to be set on the machine running
    the suite, or a default-scrub assertion could pass/fail for the wrong
    reason."""
    monkeypatch.delenv("OCR_UNSET_ENV", raising=False)
    for name in review_gate._SESSION_BRIDGE_ENV:
        monkeypatch.delenv(name, raising=False)


def test_unset_env_default_scrubs_exactly_the_session_bridge_bundle(monkeypatch, tmp_path):
    seen = {}
    _clear_session_bridge_env(monkeypatch)
    for name in review_gate._SESSION_BRIDGE_ENV:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("SOME_CLAUDE_MARKER", "1")  # not in the bundle -- must survive
    monkeypatch.setattr(review_gate.subprocess, "Popen", _fake_popen(seen))
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    review_gate._run_review(str(tmp_path), "hook", str(tmp_path), "a" * 40)
    for name in review_gate._SESSION_BRIDGE_ENV:
        assert name not in seen["env"]
    assert seen["env"]["SOME_CLAUDE_MARKER"] == "1"


def test_unset_env_none_disables_the_default_scrub(monkeypatch, tmp_path):
    seen = {}
    _clear_session_bridge_env(monkeypatch)
    monkeypatch.setenv("OCR_UNSET_ENV", "none")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "x")
    monkeypatch.setattr(review_gate.subprocess, "Popen", _fake_popen(seen))
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    review_gate._run_review(str(tmp_path), "hook", str(tmp_path), "a" * 40)
    assert seen["env"]["CLAUDE_CODE_SESSION_ID"] == "x"


def test_unset_env_none_is_case_insensitive(monkeypatch, tmp_path):
    seen = {}
    _clear_session_bridge_env(monkeypatch)
    monkeypatch.setenv("OCR_UNSET_ENV", "None")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "x")
    monkeypatch.setattr(review_gate.subprocess, "Popen", _fake_popen(seen))
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    review_gate._run_review(str(tmp_path), "hook", str(tmp_path), "a" * 40)
    assert seen["env"]["CLAUDE_CODE_SESSION_ID"] == "x"


def test_unset_env_explicit_list_replaces_the_default_rather_than_adding_to_it(
    monkeypatch, tmp_path
):
    seen = {}
    _clear_session_bridge_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "x")  # in the default bundle
    monkeypatch.setenv("SOME_CLAUDE_MARKER", "1")
    monkeypatch.setenv("OCR_UNSET_ENV", "SOME_CLAUDE_MARKER")
    monkeypatch.setattr(review_gate.subprocess, "Popen", _fake_popen(seen))
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    review_gate._run_review(str(tmp_path), "hook", str(tmp_path), "a" * 40)
    assert "SOME_CLAUDE_MARKER" not in seen["env"]
    # An explicit list REPLACES the default -- it does not add to it, since a
    # partial scrub of the bundle is its own untested state.
    assert seen["env"]["CLAUDE_CODE_SESSION_ID"] == "x"


def test_unset_env_accepts_semicolon_and_whitespace_separators(monkeypatch, tmp_path):
    seen = {}
    _clear_session_bridge_env(monkeypatch)
    monkeypatch.setenv("A_VAR", "1")
    monkeypatch.setenv("B_VAR", "1")
    monkeypatch.setenv("OCR_UNSET_ENV", "A_VAR; B_VAR")
    monkeypatch.setattr(review_gate.subprocess, "Popen", _fake_popen(seen))
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    review_gate._run_review(str(tmp_path), "hook", str(tmp_path), "a" * 40)
    assert "A_VAR" not in seen["env"]
    assert "B_VAR" not in seen["env"]


def test_unset_env_names_are_matched_case_insensitively_on_windows(monkeypatch, tmp_path):
    # os.environ's keys are already upper-cased by CPython on Windows
    # regardless of how the variable was actually set, so a lowercase name in
    # OCR_UNSET_ENV must still find it there.
    seen = {}
    _clear_session_bridge_env(monkeypatch)
    monkeypatch.setattr(review_gate.os, "name", "nt")
    monkeypatch.setenv("SOME_CLAUDE_MARKER", "1")
    monkeypatch.setenv("OCR_UNSET_ENV", "some_claude_marker")
    monkeypatch.setattr(review_gate.subprocess, "Popen", _fake_popen(seen))
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    review_gate._run_review(str(tmp_path), "hook", str(tmp_path), "a" * 40)
    assert "SOME_CLAUDE_MARKER" not in seen["env"]


def test_claude_code_execpath_survives_the_default_scrub(monkeypatch, tmp_path):
    # Read directly by _find_claude, not a session-identity variable -- must
    # never be in the default scrub bundle.
    seen = {}
    _clear_session_bridge_env(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_EXECPATH", "/path/to/claude")
    monkeypatch.setattr(review_gate.subprocess, "Popen", _fake_popen(seen))
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    review_gate._run_review(str(tmp_path), "hook", str(tmp_path), "a" * 40)
    assert seen["env"]["CLAUDE_CODE_EXECPATH"] == "/path/to/claude"


def test_ocr_debug_writes_a_spawn_and_completion_breadcrumb(monkeypatch, tmp_path):
    monkeypatch.setenv("OCR_DEBUG", "1")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "gate-data"))
    monkeypatch.setattr(review_gate.subprocess, "Popen", _fake_popen({}))
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    review_gate._run_review(str(tmp_path), "hook", str(tmp_path), "a" * 40)
    log = tmp_path / "gate-data" / "review-gate-debug.log"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("start ") for line in lines)
    assert any("scrubbed=" in line for line in lines)
    assert any(line.startswith("end ") and "outcome=rc0" in line for line in lines)


def test_ocr_debug_logs_safe_values_from_the_original_env_even_when_scrubbed(
    monkeypatch, tmp_path
):
    # Regression: _DEBUG_SAFE_VALUES and _SESSION_BRIDGE_ENV overlap by design
    # (CLAUDE_CODE_ENTRYPOINT, CLAUDE_CODE_CHILD_SESSION,
    # CLAUDE_AGENT_SDK_VERSION are in both) -- reading the allowlisted values
    # from child_env AFTER the default scrub silently logged {} for exactly
    # the names it exists to surface. Caught by a live smoke test, not review.
    _clear_session_bridge_env(monkeypatch)
    monkeypatch.setenv("OCR_DEBUG", "1")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "gate-data"))
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-desktop")
    monkeypatch.setattr(review_gate.subprocess, "Popen", _fake_popen({}))
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    review_gate._run_review(str(tmp_path), "hook", str(tmp_path), "a" * 40)
    log = (tmp_path / "gate-data" / "review-gate-debug.log").read_text(encoding="utf-8")
    assert "'CLAUDE_CODE_ENTRYPOINT': 'claude-desktop'" in log


def test_ocr_debug_off_by_default_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("OCR_DEBUG", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "gate-data"))
    monkeypatch.setattr(review_gate.subprocess, "Popen", _fake_popen({}))
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    review_gate._run_review(str(tmp_path), "hook", str(tmp_path), "a" * 40)
    assert not (tmp_path / "gate-data" / "review-gate-debug.log").exists()


def test_git_subprocess_decodes_as_utf8(monkeypatch):
    seen = {}

    class _Proc:
        returncode = 0
        stdout = "main"
        stderr = ""

    def _fake_run(cmd, **kw):
        seen.update(kw)
        return _Proc()

    monkeypatch.setattr(review_gate.subprocess, "run", _fake_run)
    review_gate._git(["rev-parse", "HEAD"])
    assert seen.get("encoding") == "utf-8"
    assert seen.get("errors") == "replace"


def test_post_ignores_a_record_too_old_to_describe_this_push(tmp_path, monkeypatch, capsys):
    _seed_record(tmp_path)
    log = review_gate._findings_log_path(str(tmp_path))
    lines = log.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[-1])
    entry["ts"] = time.time() - (MARKER_TTL + 60)
    lines[-1] = json.dumps(entry)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    _run_post(monkeypatch, tmp_path)
    assert _post_context(capsys) == ""


def test_post_still_delivers_a_record_written_moments_ago(tmp_path, monkeypatch, capsys):
    _seed_record(tmp_path)
    _run_post(monkeypatch, tmp_path)
    assert "unchecked index" in _post_context(capsys)


def test_post_ignores_a_record_with_an_unusable_timestamp(tmp_path, monkeypatch, capsys):
    _seed_record(tmp_path)
    log = review_gate._findings_log_path(str(tmp_path))
    lines = log.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[-1])
    entry["ts"] = "not-a-timestamp"
    lines[-1] = json.dumps(entry)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    _run_post(monkeypatch, tmp_path)
    assert _post_context(capsys) == ""


# --------------------------------------------------------------------------
# Delivery must survive the push FAILING.
#
# PostToolUse does not fire for a tool call that exited non-zero, so hanging
# the report off the push itself lost every finding from a rejected push. The
# gate parks a note at review time; any later tool call cashes it in. Observed
# in the wild: two reviewed pushes, one finding each, both with a
# scr-push-reviewed marker and no scr-post-delivered companion.
# --------------------------------------------------------------------------


def _park(monkeypatch, tmp_path, session="s1", repo=None, head="a" * 40):
    """Park a note as the gate would. Isolates the scratch dir FIRST -- without
    that this writes into the real one on the machine running the suite."""
    _isolate_gate_data(monkeypatch, tmp_path)
    review_gate._park_pending(session, repo if repo is not None else str(tmp_path), head)


def test_gate_parks_a_report_before_the_push_runs(tmp_path, monkeypatch, capsys):
    _stub_gate(monkeypatch, tmp_path)
    _run_hook(monkeypatch)
    capsys.readouterr()
    assert _pending_files(tmp_path), "a review the push may never report must be parked"


def test_a_failed_push_still_reports_on_the_next_command(tmp_path, monkeypatch, capsys):
    _seed_record(tmp_path)
    _park(monkeypatch, tmp_path)
    # The push blew up, so no PostToolUse fired for it. The next command does.
    _run_post(monkeypatch, tmp_path, command="git status")
    assert "unchecked index" in _post_context(capsys)


def test_a_deferred_report_names_the_repo_it_describes(tmp_path, monkeypatch, capsys):
    # It arrives detached from the push that earned it, and one session spans
    # several repos in a conversation -- so "which repo is this about?" cannot
    # be answered by "wherever you are now".
    _seed_record(tmp_path)
    _park(monkeypatch, tmp_path)
    _run_post(monkeypatch, tmp_path, command="git status")
    ctx = _post_context(capsys)
    assert str(tmp_path) in ctx and "deferred report" in ctx


def test_a_flushed_report_is_not_delivered_twice(tmp_path, monkeypatch, capsys):
    _seed_record(tmp_path)
    _park(monkeypatch, tmp_path)
    _run_post(monkeypatch, tmp_path, command="git status")
    assert _post_context(capsys)
    assert _pending_files(tmp_path) == []
    _run_post(monkeypatch, tmp_path, command="ls")
    assert _post_context(capsys) == ""


def test_a_spent_note_is_cleared_even_when_there_was_nothing_to_say(tmp_path, monkeypatch, capsys):
    # Otherwise the same dead question is re-asked on every tool call for an
    # hour, spawning a Python process each time.
    _seed_record(tmp_path, verdict="pass", findings=[])
    _run_post(monkeypatch, tmp_path)  # the push reports the pass and spends the note
    capsys.readouterr()
    _park(monkeypatch, tmp_path)
    _run_post(monkeypatch, tmp_path, command="git status")
    assert _post_context(capsys) == ""  # already delivered to this session
    assert _pending_files(tmp_path) == []


def test_a_successful_push_clears_its_own_note(tmp_path, monkeypatch, capsys):
    _seed_record(tmp_path)
    _park(monkeypatch, tmp_path)
    _run_post(monkeypatch, tmp_path, command="git push")
    assert _post_context(capsys)
    assert _pending_files(tmp_path) == []


def test_a_push_does_not_clear_another_sessions_note_for_the_same_repo(
    tmp_path, monkeypatch, capsys
):
    # Two sessions pushing one repo. Clearing the other's note on the way past
    # would leave it holding a review nothing will ever report -- exactly the
    # hole the note exists to close.
    _seed_record(tmp_path)
    _park(monkeypatch, tmp_path, session="s2")
    _run_post(monkeypatch, tmp_path, command="git push", session_id="s1")
    assert _post_context(capsys)
    assert _pending_files(tmp_path), "s2's note must survive s1's push"


def test_one_sessions_parked_report_is_not_flushed_into_another(tmp_path, monkeypatch, capsys):
    # The failure this guards: two concurrent sessions in different repos, and
    # one gets told about the other's findings as though they were its own.
    _seed_record(tmp_path)
    _park(monkeypatch, tmp_path, session="s1")
    _run_post(monkeypatch, tmp_path, command="git status", session_id="s2")
    assert _post_context(capsys) == ""
    assert _pending_files(tmp_path), "s1's note must survive for s1"
    _run_post(monkeypatch, tmp_path, command="git status", session_id="s1")
    assert "unchecked index" in _post_context(capsys)


def test_a_terminal_pushs_report_goes_to_whoever_is_in_that_repo(tmp_path, monkeypatch, capsys):
    # The git adapter has no session id, so its note is addressed to nobody --
    # it belongs to whichever session is actually sitting in that repository.
    _seed_record(tmp_path)
    _park(monkeypatch, tmp_path, session="")
    _run_post(monkeypatch, tmp_path, command="git status", session_id="whoever")
    assert "unchecked index" in _post_context(capsys)


def test_a_sessionless_note_from_another_repo_is_not_flushed_here(tmp_path, monkeypatch, capsys):
    _seed_record(tmp_path)
    _park(monkeypatch, tmp_path, session="", repo=str(tmp_path / "elsewhere"))
    _run_post(monkeypatch, tmp_path, command="git status")
    assert _post_context(capsys) == ""


def test_a_note_older_than_any_push_it_could_describe_is_swept(tmp_path, monkeypatch, capsys):
    _seed_record(tmp_path)
    _park(monkeypatch, tmp_path)
    stale = time.time() - review_gate.MARKER_TTL - 60
    for p in (tmp_path / "gate-data").glob("pending-*"):
        os.utime(p, (stale, stale))
    _run_post(monkeypatch, tmp_path, command="git status")
    assert _post_context(capsys) == ""
    assert _pending_files(tmp_path) == []


def test_an_unreadable_note_is_discarded_rather_than_crashing(tmp_path, monkeypatch, capsys):
    _seed_record(tmp_path)
    data = tmp_path / "gate-data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "pending-garbage").write_text("{not json", encoding="utf-8")
    _run_post(monkeypatch, tmp_path, command="git status")
    assert _post_context(capsys) == ""
    assert _pending_files(tmp_path) == []


def test_flush_output_is_pure_ascii(tmp_path, monkeypatch, capsys):
    _seed_record(tmp_path)
    _park(monkeypatch, tmp_path)
    _run_post(monkeypatch, tmp_path, command="git status")
    _post_context(capsys).encode("ascii")


# --- the gate must look at the repo being PUSHED ------------------------------
# Found by smoke test, not review: in hook mode repo_root came from the process
# cwd -- the directory Claude Code was launched from. Claude routinely pushes as
# `cd <repo> && git push`, so the gate inspected the SESSION's repo, found
# nothing unpushed, and allowed a push it had never reviewed. Silent, and total
# in any repo where the global git hook is absent or shadowed.

def _gate_hook(monkeypatch, command, **patches):
    seen = {}
    monkeypatch.setattr(review_gate, "_write_gate_pointer", lambda: None)
    monkeypatch.delenv("OCR_IN_REVIEW", raising=False)
    for name, value in patches.items():
        monkeypatch.setattr(review_gate, name, value)

    def _unpushed(repo_root=None, push_range=None):
        seen["unpushed_root"] = repo_root
        return True

    def _review(repo_root, mode, git_dir=None, head_sha="", push_range=""):
        seen["review_root"] = repo_root
        return {"findings": []}, True, ""

    monkeypatch.setattr(review_gate, "_has_unpushed_commits", _unpushed)
    monkeypatch.setattr(review_gate, "_run_review", _review)
    monkeypatch.setattr(sys, "stdin", _io.StringIO(json.dumps({"tool_input": {"command": command}})))
    try:
        review_gate._main_inner(["review-gate.py", "--mode", "hook"], "hook")
    except SystemExit:
        pass
    return seen


def test_a_real_push_is_recognised_behind_env_prefixes_and_flags():
    assert review_gate._looks_like_real_push("git push origin main") is True
    assert review_gate._looks_like_real_push("cd /x && git push") is True
    assert review_gate._looks_like_real_push("PYTEST_WORKERS=4 git push origin main") is True
    assert review_gate._looks_like_real_push("git -c foo=bar push") is True


# --- multi-line commands and relative cd targets ------------------------------
# All three found by the gate reviewing its own change (verdict warn, three
# high findings against 94b69df). A Bash tool call is routinely multi-line, and
# the separator alternation had no newline case while re.finditer is not
# re.MULTILINE -- so `cd repo\ngit push` produced no cd target at all, and
# _looks_like_real_push returned False for a genuine push, silently disabling
# the unknown-target block it gates. Relative targets were tested with
# os.path.isdir against the HOOK PROCESS's cwd, which is the very confusion
# this code path exists to correct.

def test_cd_is_found_when_it_sits_on_its_own_line():
    assert _cd_targets("cd /a/b\ngit push origin main") == ["/a/b"]
    assert _cd_targets("echo start\ncd /a/b\ngit push") == ["/a/b"]


def test_a_push_on_its_own_line_is_still_a_real_push():
    assert review_gate._looks_like_real_push("cd /repo\ngit push") is True
    assert review_gate._looks_like_real_push("set -e\nPYTEST_WORKERS=4 git push origin main") is True
_PUSH = "git" + " push"  # not spelled literally: this file is read by the gate


def test_a_real_push_after_a_heredoc_still_counts():
    # Only the BODY is dropped; commands that follow the terminator remain.
    cmd = f"cat > x <<'EOF'\nhello\nEOF\ncd /srv/app && {_PUSH}"
    assert review_gate._looks_like_real_push(cmd) is True
    assert _cd_targets(cmd) == ["/srv/app"]


# --- ...but stripping must never eat real commands ----------------------------
# The gate blocked the release of the heredoc handling above (high): _HEREDOC
# matched `<<WORD` anywhere on a line with no quoting awareness, and when no
# terminator existed it discarded everything to the end of the command. That
# could swallow a genuine cd and a genuine push, leaving the parser blind --
# a way to silently defeat the gate, introduced by a fix meant to protect it.

def test_two_angle_brackets_as_data_do_not_open_a_heredoc():
    # A real opener ENDS its line; this one is mid-line, inside a string.
    cmd = f'echo "a <<EOF b"\ncd /repo\n{_PUSH}'
    assert _cd_targets(cmd) == ["/repo"]
    assert review_gate._looks_like_real_push(cmd) is True


def test_a_quoted_cd_target_survives_masking():
    # The exception that makes blanket masking unsafe: this quoted span IS the
    # hop we are trying to follow.
    cmd = f'cd "/path with spaces" && {_PUSH}'
    assert _cd_targets(cmd) == ["/path with spaces"]


def test_a_real_push_alongside_a_quoted_message_still_parses():
    cmd = f'cd /repo && git commit -m "msg" && {_PUSH}'
    assert _cd_targets(cmd) == ["/repo"]
    assert review_gate._looks_like_real_push(cmd) is True


# --- which repo is being pushed, and whether that is knowable ----------------
# This replaced ~300 lines of shell parsing. The parser tried to make every
# exotic command WORK and produced eleven repair commits, several of them
# fail-opens the gate itself caught. In a fail-closed tool the answer to an
# ambiguous command is not a better parser: it is to refuse and say so.

_gate_repo = review_gate._gate_repo


def _payload(cmd, cwd):
    return {"tool_input": {"command": cmd}, "cwd": str(cwd)}


def _repo_at(monkeypatch, *real_dirs):
    """git resolves only the given directories; everything else is not a repo."""
    real = {os.path.normcase(os.path.abspath(str(d))) for d in real_dirs}

    def _fake_git(args, cwd=None):
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            if cwd and os.path.normcase(os.path.abspath(str(cwd))) in real:
                return str(cwd), 0
        return "", 1

    monkeypatch.setattr(review_gate, "_git", _fake_git)


def test_no_cd_uses_the_session_directory(tmp_path, monkeypatch):
    _repo_at(monkeypatch, tmp_path)
    assert _gate_repo(_payload("git push origin main", tmp_path)) == (str(tmp_path), False)


def test_a_literal_cd_wins_over_the_session_directory(tmp_path, monkeypatch):
    pushed = tmp_path / "pushed"
    pushed.mkdir()
    _repo_at(monkeypatch, pushed)
    root, ambiguous = _gate_repo(_payload("cd " + str(pushed) + " && git push", tmp_path))
    assert (root, ambiguous) == (str(pushed), False)


def test_a_relative_cd_is_anchored_to_the_session_directory(tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    _repo_at(monkeypatch, tmp_path / "repo")
    root, ambiguous = _gate_repo(_payload("cd repo && git push", tmp_path))
    assert ambiguous is False
    assert os.path.normcase(root) == os.path.normcase(str(tmp_path / "repo"))


def test_a_chained_relative_cd_folds_onto_the_previous_hop(tmp_path, monkeypatch):
    (tmp_path / "repo" / "sub").mkdir(parents=True)
    _repo_at(monkeypatch, tmp_path / "repo" / "sub")
    root, ambiguous = _gate_repo(_payload("cd repo && cd sub && git push", tmp_path))
    assert ambiguous is False
    assert os.path.normcase(root) == os.path.normcase(str(tmp_path / "repo" / "sub"))


def test_an_unexpanded_variable_is_ambiguous(tmp_path, monkeypatch):
    _repo_at(monkeypatch, tmp_path)
    assert _gate_repo(_payload('cd "$T" && git push', tmp_path)) == ("", True)


def test_a_command_substitution_is_ambiguous(tmp_path, monkeypatch):
    _repo_at(monkeypatch, tmp_path)
    assert _gate_repo(_payload("cd $(git rev-parse --show-toplevel) && git push", tmp_path)) == ("", True)


def test_cd_dash_is_ambiguous(tmp_path, monkeypatch):
    _repo_at(monkeypatch, tmp_path)
    assert _gate_repo(_payload("cd - && git push", tmp_path)) == ("", True)


def test_a_cd_into_something_that_is_not_a_directory_is_ambiguous(tmp_path, monkeypatch):
    _repo_at(monkeypatch, tmp_path)
    assert _gate_repo(_payload("cd nope && git push", tmp_path)) == ("", True)


def test_a_directory_that_is_not_a_repo_is_ambiguous(tmp_path, monkeypatch):
    (tmp_path / "plain").mkdir()
    _repo_at(monkeypatch, tmp_path)  # tmp_path resolves, tmp_path/plain does not
    assert _gate_repo(_payload("cd plain && git push", tmp_path)) == ("", True)


def test_ambiguity_is_deliberately_blunter_than_the_old_parser(tmp_path, monkeypatch):
    # A heredoc or quoted argument that merely CONTAINS a cd chain now reads as
    # ambiguous and blocks. The old parser tried to tell code from data and got
    # it wrong repeatedly; blocking is visible and bypassable, misrouting is
    # neither. This test exists so the trade is a decision, not an accident.
    _repo_at(monkeypatch, tmp_path)
    cmd = 'git commit -m "cd nowhere && git push" && git push'
    assert _gate_repo(_payload(cmd, tmp_path)) == ("", True)


# --- the breadcrumb: delivery does no command parsing at all -----------------
# The gate already had to resolve the pushed repo in order to review it, so it
# writes that down and --mode post reads it. Delivery re-deriving it by parsing
# the command again was duplicated fragility with its own failure modes.

def test_the_breadcrumb_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    review_gate._drop_breadcrumb("sess-1", str(tmp_path))
    assert review_gate._read_breadcrumb("sess-1") == str(tmp_path)


def test_breadcrumbs_are_per_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    review_gate._drop_breadcrumb("sess-1", str(tmp_path))
    assert review_gate._read_breadcrumb("sess-2") == ""


def test_a_missing_breadcrumb_reads_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    assert review_gate._read_breadcrumb("never-written") == ""


def test_a_stale_breadcrumb_is_ignored(tmp_path, monkeypatch):
    # It describes some earlier push, not this one. Better to fall back to the
    # process cwd than to report against a repo we have since left.
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    review_gate._drop_breadcrumb("sess-1", str(tmp_path))
    p = review_gate._breadcrumb_path("sess-1")
    stamp = time.time() - (MARKER_TTL + 60)
    os.utime(p, (stamp, stamp))
    assert review_gate._read_breadcrumb("sess-1") == ""


def test_a_breadcrumb_pointing_at_a_vanished_directory_reads_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "data"))
    review_gate._drop_breadcrumb("sess-1", str(tmp_path / "gone"))
    assert review_gate._read_breadcrumb("sess-1") == ""


def test_dropping_a_breadcrumb_never_raises(tmp_path, monkeypatch):
    # Bookkeeping must not be able to break a push.
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "f" / "data"))
    (tmp_path / "f").write_text("not a directory", encoding="utf-8")
    review_gate._drop_breadcrumb("sess-1", str(tmp_path))  # must not raise


def test_a_mention_of_a_push_does_not_truncate_the_cd_scan(tmp_path, monkeypatch):
    # The scan is bounded at the push COMMAND, not the first occurrence of the
    # words. Splitting on the substring loses a real cd that comes after a mere
    # mention -- the command below would fall back to the session directory and
    # review the wrong repo silently, which is the fail-open this resolution
    # exists to close. Caught by the gate on its own release.
    (tmp_path / "real").mkdir()
    _repo_at(monkeypatch, tmp_path / "real")
    cmd = 'echo "remember to git push later" && cd real && git push'
    root, ambiguous = _gate_repo(_payload(cmd, tmp_path))
    assert ambiguous is False
    assert os.path.normcase(root) == os.path.normcase(str(tmp_path / "real"))


def test_an_absolute_hop_re_anchors_after_an_unresolvable_one(tmp_path, monkeypatch):
    # `cd "$OLDPWD" && cd /srv/repo && git push` is legitimate: the absolute
    # hop fully determines where we end up regardless of what came before, so
    # the chain IS knowable. Returning ambiguous on the first unresolvable hop
    # contradicted the docstring and would have blocked it.
    real = tmp_path / "srv"
    real.mkdir()
    _repo_at(monkeypatch, real)
    cmd = 'cd "$OLDPWD" && cd ' + str(real) + ' && git push'
    root, ambiguous = _gate_repo(_payload(cmd, tmp_path))
    assert ambiguous is False
    assert os.path.normcase(root) == os.path.normcase(str(real))


def test_a_relative_hop_after_an_unresolvable_one_stays_ambiguous(tmp_path, monkeypatch):
    # Nothing to join it onto. Guessing here is what produced a confident
    # answer about the wrong repository in the first place.
    _repo_at(monkeypatch, tmp_path)
    assert _gate_repo(_payload('cd "$T" && cd sub && git push', tmp_path)) == ("", True)


# --- Git Bash spells Windows drives as /j/..., and this hook is not Git Bash --
# Claude's Bash tool on Windows is Git Bash, so `pwd` says /j/codigo/repo and
# Claude pushes as `cd /j/codigo/repo && git push`. The hook runs under native
# Python, where that path is `j/codigo/repo` off the root of the CURRENT drive: isabs()
# is True, isdir() is False, and a real repository was denied as ambiguous.

import pytest  # noqa: E402


def test_an_msys_drive_path_is_translated_to_the_native_spelling(monkeypatch):
    monkeypatch.setattr(review_gate.os, "name", "nt")
    native = review_gate._native_path
    assert native("/j/codigo/thyra-ai") == r"J:\codigo\thyra-ai"
    assert native("/cygdrive/c/Users/me/repo") == r"C:\Users\me\repo"
    assert native("/j") == "J:\\"
    # Not a drive spelling: left alone for the resolver to judge as before.
    assert native("/usr/local/repo") == "/usr/local/repo"
    assert native("C:/codigo/repo") == "C:/codigo/repo"
    assert native("repo") == "repo"


def test_an_msys_drive_path_is_left_alone_off_windows(monkeypatch):
    # POSIX has no drives; /j/repo is simply a directory named j.
    monkeypatch.setattr(review_gate.os, "name", "posix")
    assert review_gate._native_path("/j/codigo/repo") == "/j/codigo/repo"


@pytest.mark.skipif(os.name != "nt", reason="needs a real drive letter to spell")
def test_a_cd_into_a_git_bash_drive_path_resolves_the_repo(tmp_path, monkeypatch):
    real = tmp_path / "pushed"
    real.mkdir()
    _repo_at(monkeypatch, real)
    drive, rest = os.path.splitdrive(str(real))
    msys = "/" + drive[0].lower() + rest.replace("\\", "/")
    root, ambiguous = _gate_repo(_payload("cd " + msys + " && git push", tmp_path))
    assert ambiguous is False
    assert os.path.normcase(root) == os.path.normcase(str(real))


# --- heredoc bodies are data, and that one distinction earned its way back ---
# The simplification removed all command parsing, and within minutes a
# `git commit` whose MESSAGE discussed a cd chain and a push was denied as an
# ambiguous push. In this repo, whose commit messages routinely quote commands,
# that is not an edge case.
#
# A heredoc body is data the command WRITES; the shell never runs it, so
# parsing it as code is simply wrong. That is decidable, which is why this one
# transformation came back while quoted-argument and `bash -c` guessing did
# not: those were guesses at intent, and they kept guessing wrong.

def test_a_commit_message_written_via_heredoc_does_not_read_as_a_push(tmp_path, monkeypatch):
    _repo_at(monkeypatch, tmp_path)
    cmd = (
        "git commit -F - <<'MSG'\n"
        "explains that cd \"$OLDPWD\" && cd /srv/repo && git push was denied\n"
        "MSG\n"
        "git log --oneline -1"
    )
    assert review_gate._looks_like_real_push(cmd) is False
    assert _gate_repo(_payload(cmd, tmp_path)) == (str(tmp_path), False)


def test_an_unterminated_heredoc_strips_nothing(tmp_path):
    # Stripping to end-of-command would delete the real commands after it.
    cmd = "cat > x <<EOF\nbody\ncd /repo\ngit push"
    assert review_gate._strip_heredocs(cmd) == cmd


def test_an_opener_followed_by_a_pipe_is_still_an_opener(tmp_path):
    cmd = "python - <<'PY' 2>&1 | head -5\ncd /elsewhere && git push\nPY\necho done"
    assert review_gate._looks_like_real_push(cmd) is False


# --- what is being pushed, and where ----------------------------------------
# The branch:main bypass. `_has_unpushed_commits` asked "is HEAD ahead of its
# own upstream?" when the question is "what is the remote about to gain?".
# Those diverge the moment you push to a ref that is not your upstream:
# `git push origin mybranch:main`, with mybranch already pushed, reports zero
# unpushed commits -- so the gate allowed five commits onto main having
# reviewed none of them. Observed in a real repo.
#
# Git hands a pre-push hook the answer on stdin and the gate used to discard it.

_ZERO = "0" * 40


def _stdin(monkeypatch, text):
    monkeypatch.setattr(sys, "stdin", _io.StringIO(text))


def test_push_refs_are_read_from_stdin(monkeypatch):
    _stdin(monkeypatch, "refs/heads/x " + "a" * 40 + " refs/heads/main " + "b" * 40 + "\n")
    assert review_gate._read_push_refs() == [("a" * 40, "b" * 40)]


def test_a_branch_deletion_carries_nothing_to_review(monkeypatch):
    _stdin(monkeypatch, "(delete) " + _ZERO + " refs/heads/main " + "b" * 40 + "\n")
    assert review_gate._read_push_refs() == []


def test_malformed_and_empty_stdin_fall_back_rather_than_fail(monkeypatch):
    _stdin(monkeypatch, "nonsense\n\n")
    assert review_gate._read_push_refs() == []
    _stdin(monkeypatch, "")
    assert review_gate._read_push_refs() == []


def test_the_range_is_what_the_remote_gains(monkeypatch):
    # Not @{u}..HEAD -- the remote sha of the ref actually being written.
    monkeypatch.setattr(
        review_gate, "_git",
        lambda args, cwd=None: ("commit1", 0) if args[:1] in (["log"], ["cat-file"]) else ("", 1),
    )
    rng = review_gate._range_for_refs([("a" * 40, "b" * 40)])
    assert rng == "b" * 40 + ".." + "a" * 40


def test_a_range_with_no_commits_is_not_offered(monkeypatch):
    monkeypatch.setattr(
        review_gate, "_git",
        lambda args, cwd=None: ("", 0) if args[:1] in (["log"], ["cat-file"]) else ("", 1),
    )
    assert review_gate._range_for_refs([("a" * 40, "b" * 40)]) == ""


def test_a_brand_new_remote_ref_falls_back_to_the_default_branch(monkeypatch):
    seen = {}

    def _fake_git(args, cwd=None):
        if args[:2] == ["rev-parse", "--verify"]:
            return ("origin/main", 0) if args[-1] == "origin/main" else ("", 1)
        if args[:1] == ["log"]:
            seen["range"] = args[1]
            return "commit1", 0
        return "", 1

    monkeypatch.setattr(review_gate, "_git", _fake_git)
    rng = review_gate._range_for_refs([("a" * 40, _ZERO)])
    assert rng == "origin/main.." + "a" * 40
    assert seen["range"] == rng


def test_the_push_range_decides_whether_there_is_anything_to_review(monkeypatch):
    # THE bypass, in one assertion. The branch is level with its upstream, so
    # the old question answers "nothing unpushed" -- but the range being sent
    # to main carries commits, so there is plenty to review.
    def _fake_git(args, cwd=None):
        if args[:1] != ["log"]:
            return "", 1
        rng = args[1]
        if rng.startswith("@{u}"):
            return "", 0          # level with upstream
        return "deadbee some commit", 0   # but the push carries commits

    monkeypatch.setattr(review_gate, "_git", _fake_git)
    assert review_gate._has_unpushed_commits("/repo") is False
    assert review_gate._has_unpushed_commits("/repo", push_range="aaa..bbb") is True


def test_without_refs_the_old_upstream_heuristic_still_applies(monkeypatch):
    # Hook mode runs BEFORE git, so no refs exist yet and this is all there is.
    monkeypatch.setattr(
        review_gate, "_git",
        lambda args, cwd=None: ("commit1", 0) if args[:1] == ["log"] else ("", 1),
    )
    assert review_gate._has_unpushed_commits("/repo") is True


def test_the_review_is_told_the_range(monkeypatch, tmp_path):
    # Detecting the right thing is only half of it: without passing the range
    # on, the skill re-derives @{u}..HEAD and reviews nothing.
    seen = {}
    monkeypatch.setattr(review_gate.subprocess, "Popen", _fake_popen(seen))
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    review_gate._run_review(str(tmp_path), "git", str(tmp_path), "a" * 40, "aaa..bbb")
    prompt = seen["cmd"][2]
    assert "--range aaa..bbb" in prompt
    assert "--unpushed" not in prompt


def test_without_a_range_the_prompt_is_unchanged(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(review_gate.subprocess, "Popen", _fake_popen(seen))
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    review_gate._run_review(str(tmp_path), "hook", str(tmp_path), "a" * 40)
    prompt = seen["cmd"][2]
    assert "--unpushed" in prompt
    assert "--range" not in prompt


def test_a_tag_ref_is_not_counted_as_a_branch_update(monkeypatch):
    # `git push --follow-tags` carries tag refs alongside the branch. Their
    # commits are already covered by it, so counting them would turn ordinary
    # pushes into multi-ref ones for no gain.
    _stdin(monkeypatch,
           "refs/tags/v1 " + "a" * 40 + " refs/tags/v1 " + _ZERO + "\n"
           "refs/heads/x " + "c" * 40 + " refs/heads/main " + "d" * 40 + "\n")
    assert review_gate._read_push_refs() == [("c" * 40, "d" * 40)]


def test_two_branches_gaining_commits_is_refused_not_half_reviewed(monkeypatch):
    # No single `A..B` expresses two branches, and reviewing one would leave
    # the other unreviewed -- the exact fail-open this code closes.
    monkeypatch.setattr(
        review_gate, "_git",
        lambda args, cwd=None: ("commit1", 0) if args[:1] in (["log"], ["cat-file"]) else ("", 1),
    )
    rng = review_gate._range_for_refs([("a" * 40, "b" * 40), ("c" * 40, "d" * 40)])
    assert rng == review_gate._MULTI_REF


def test_a_second_ref_carrying_nothing_does_not_trigger_the_refusal(monkeypatch):
    def _fake_git(args, cwd=None):
        if args[:1] == ["cat-file"]:
            return "", 0
        if args[:1] != ["log"]:
            return "", 1
        return ("commit1", 0) if args[1].startswith("b" * 40) else ("", 0)

    monkeypatch.setattr(review_gate, "_git", _fake_git)
    rng = review_gate._range_for_refs([("a" * 40, "b" * 40), ("c" * 40, "d" * 40)])
    assert rng == "b" * 40 + ".." + "a" * 40


def test_the_multi_ref_sentinel_is_never_used_as_a_revision_range(monkeypatch):
    seen = []

    def _fake_git(args, cwd=None):
        seen.append(args)
        return "commit1", 0

    monkeypatch.setattr(review_gate, "_git", _fake_git)
    review_gate._has_unpushed_commits("/repo", push_range=review_gate._MULTI_REF)
    assert all(review_gate._MULTI_REF not in a for args in seen for a in args)


def test_a_remote_sha_the_client_does_not_have_falls_back_rather_than_skipping(monkeypatch):
    # The remote sha comes from the REMOTE during negotiation; the client only
    # needs the objects it must send, so that commit may be absent locally.
    # `git log <missing>..<local>` then fails, and treating that as "no
    # commits" would skip the review exactly when we are least sure.
    def _fake_git(args, cwd=None):
        if args[:1] == ["cat-file"]:
            return "", 1                     # remote tip not present locally
        if args[:2] == ["rev-parse", "--verify"]:
            return ("origin/main", 0) if args[-1] == "origin/main" else ("", 1)
        if args[:1] == ["log"]:
            return "commit1", 0
        return "", 1

    monkeypatch.setattr(review_gate, "_git", _fake_git)
    assert review_gate._range_for_refs([("a" * 40, "b" * 40)]) == "origin/main.." + "a" * 40


def test_an_unevaluable_range_is_assumed_to_carry_commits(monkeypatch):
    # Over-reviewing costs a review; under-reviewing costs the gate.
    def _fake_git(args, cwd=None):
        if args[:1] == ["cat-file"]:
            return "", 0
        if args[:1] == ["log"]:
            return "", 128                   # unknown revision
        return "", 1

    monkeypatch.setattr(review_gate, "_git", _fake_git)
    assert review_gate._range_for_refs([("a" * 40, "b" * 40)]) == "b" * 40 + ".." + "a" * 40


# --- what the push command sends (0.6.0) --------------------------------------
# Hook mode used to review the checked-out HEAD whatever the command named.
# _parse_push reads the command; everything it cannot read literally is
# refused rather than guessed. Shell plumbing around the push is not an
# argument to it: Claude's habitual form is `git push origin main 2>&1`.

_parse_push = review_gate._parse_push


def test_parse_push_reads_remote_and_branch():
    t = _parse_push("git push -u origin feat/x")
    assert (t["kind"], t["remote"], t["src"], t["dst"], t["set_upstream"]) == ("branch", "origin", "feat/x", "", True)
    t = _parse_push("git push origin HEAD:refs/heads/x")
    assert (t["src"], t["dst"]) == ("HEAD", "refs/heads/x")
    assert _parse_push("git push origin +main")["src"] == "main"


def test_parse_push_ignores_shell_redirections_around_the_push():
    for cmd in ("git push origin main 2>&1", "git push origin main 2>&1 | tail -5",
                "git push origin main > push.log 2>&1", "git push origin main 2> /dev/null",
                "git push origin main && echo done", "cd /repo && git push origin main 2>&1; echo rc=$?"):
        t = _parse_push(cmd)
        assert t["kind"] == "branch", (cmd, t)
        assert (t["remote"], t["src"]) == ("origin", "main"), (cmd, t)


def test_parse_push_refuses_what_it_cannot_read():
    assert _parse_push("git push --frobnicate origin main")["kind"] == "unparseable"
    assert _parse_push("git push origin $BRANCH")["kind"] == "unparseable"
    assert _parse_push("git push --no-verify origin main")["kind"] == "no_verify"
    assert _parse_push("git push origin a b")["kind"] == "multi"
    assert _parse_push("git push --all")["kind"] == "multi"
    assert _parse_push("git push origin main; git push origin dev")["kind"] == "multi_push"


def test_parse_push_knows_the_harmless_shapes():
    assert _parse_push("git push -n origin main")["kind"] == "dry_run"
    assert _parse_push("git push origin :gone")["kind"] == "delete"
    assert _parse_push("git push --delete origin gone")["kind"] == "delete"
    assert _parse_push("git push --tags")["kind"] == "tags"
    # value-taking options do not swallow the remote
    t = _parse_push("git push -o ci.skip --force-with-lease=main:abc origin main")
    assert (t["remote"], t["src"]) == ("origin", "main")
    assert _parse_push("git push --repo=upstream main")["remote"] == "upstream"


def test_only_read_only_git_may_precede_a_push():
    pre = review_gate._pre_push_git_commands
    assert pre("git status && git push origin main") == []
    assert pre("git branch --show-current && git push origin main") == []
    assert pre("git switch x && git push origin x") == ["switch"]
    assert pre("git commit -m x && git push origin main") == ["commit"]
    assert pre("git stash pop; git push origin main") == ["stash"]


def test_git_C_is_a_final_cd_for_the_resolver():
    assert review_gate._push_c_dir("git -C /repo push origin main") == "/repo"
    assert review_gate._push_c_dir('git -C "/a b" push') == "/a b"
    assert review_gate._looks_like_real_push('git -C "/a b" push') is True
    assert review_gate._push_c_dir("git push origin main") == ""


# --- inside the review: the write guard ----------------------------------------

def test_guard_refuses_output_and_escaping_redirections_only():
    g = review_gate._guard_reviewer_command
    assert g("git diff HEAD~1") == ""
    assert g("git diff HEAD~1 > .review_hunks.txt") == ""
    assert g("git diff HEAD~1 2>&1 | head -50") == ""
    assert g("git diff > /dev/null") == ""
    assert g("git diff --output=x.txt HEAD~1") != ""
    assert g("git log -1 --format=x --output out.txt") != ""
    assert g("git diff > .git/scr-push-reviewed-abc") != ""
    assert g("git diff > ../outside.txt") != ""
    assert g("git diff > /abs/path.txt") != ""
    assert g("git diff > C:/abs/path.txt") != ""
    assert g("git diff > ~/x") != ""
    assert g("git diff > $HOME/x") != ""


# --- findings from the gate reviewing its own 0.6.0 push ----------------------

def test_guard_catches_abbreviated_output_options():
    g = review_gate._guard_reviewer_command
    for cmd in ("git diff --outp=x.txt HEAD~1", "git diff --out x.txt HEAD~1", "git log --o=x"):
        assert g(cmd) != "", cmd
    assert g("git log --oneline -5") == ""  # a different option, allowed


def test_tags_alongside_a_refspec_are_still_checked(monkeypatch):
    calls = []

    def _fake_git(args, cwd=None):
        calls.append(args)
        if args[:2] == ["rev-list", "--tags"]:
            return "deadbeef", 0  # a tagged commit the remote lacks
        return "", 1

    monkeypatch.setattr(review_gate, "_git", _fake_git)
    decision, info = review_gate._hook_target("/repo", "git push origin main --tags")
    assert decision == "deny" and "--tags" in info["why"]


def test_post_routes_a_dash_C_push_to_delivery(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(review_gate, "_flush_pending", lambda sid: seen.setdefault("flushed", True) and 0)
    monkeypatch.setattr(review_gate, "_read_breadcrumb", lambda sid: "")
    monkeypatch.setattr(review_gate, "_repo_root", lambda: "")
    monkeypatch.setattr(sys, "stdin", _io.StringIO(json.dumps(
        {"session_id": "s1", "tool_input": {"command": "git -C /repo push origin main"}})))
    assert review_gate._mode_post(["x", "--mode", "post"]) == 0
    assert "flushed" not in seen  # taken as a push, not as "some other command"


def test_state_lock_releases_only_its_own_lock(tmp_path):
    state = tmp_path / "tip.json"
    lock = review_gate._StateLock(state)
    with lock:
        # A waiter that judged us stale took the lock over.
        (tmp_path / "tip.json.lock").write_text("someone-else", encoding="utf-8")
    assert (tmp_path / "tip.json.lock").read_text(encoding="utf-8") == "someone-else"
    (tmp_path / "tip.json.lock").unlink()
    with review_gate._StateLock(state):
        pass
    assert not (tmp_path / "tip.json.lock").exists()


def test_the_retry_that_delivers_a_verdict_drops_its_async_note(tmp_path, monkeypatch, capsys):
    _stub_gate(monkeypatch, tmp_path)
    review_gate._park_pending("s1", str(tmp_path), "a" * 40, kind="async", extra={"state": "x"})
    assert _pending_files(tmp_path) != []
    monkeypatch.setattr(sys, "stdin", _io.StringIO(json.dumps(
        {"session_id": "s1", "tool_input": {"command": "git push"}})))
    try:
        review_gate._main_inner(["review-gate.py", "--mode", "hook"], "hook")
    except SystemExit:
        pass
    notes = [json.loads((tmp_path / "gate-data" / n).read_text()) for n in _pending_files(tmp_path)]
    assert [n["kind"] for n in notes] == ["review"]  # the async note is gone


def test_guard_judges_tokens_not_quoted_text():
    g = review_gate._guard_reviewer_command
    assert g('git log --grep="needs --output flag" --oneline') == ""
    assert g("git log -1 --format='%s' --output x") != ""


def test_guard_falls_back_to_the_raw_text_when_the_command_cannot_be_tokenised():
    g = review_gate._guard_reviewer_command
    # An unterminated quote defeats shlex; the guard must still refuse the
    # write rather than wave it through -- the fallback is the raw match.
    assert g("git log --format='unterminated --output=x") != ""
    assert g("git log --format='unterminated --oneline") == ""


# ---------------------------------------------------------------------------
# Chunking helpers (0.7.0)
# ---------------------------------------------------------------------------

_is_allowed_path = review_gate._is_allowed_path
_collect_diff_entries = review_gate._collect_diff_entries
_plan_chunks = review_gate._plan_chunks
_group_into_chunks = review_gate._group_into_chunks
_merge_chunk_results = review_gate._merge_chunk_results
_merge_near_dup_findings = review_gate._merge_near_dup_findings
_findings_similar = review_gate._findings_similar
_validate_chunk_cache = review_gate._validate_chunk_cache
_read_chunk_cache = review_gate._read_chunk_cache
_write_chunk_cache = review_gate._write_chunk_cache
_chunk_id = review_gate._chunk_id
_check_limit = review_gate._check_limit
_parse_resets_at = review_gate._parse_resets_at
_reap_async = review_gate._reap_async
_async_dir = review_gate._async_dir
_gate_data_dir = review_gate._gate_data_dir
STALE_S = review_gate.STALE_S
MARKER_TTL = review_gate.MARKER_TTL


# --- allowlist parity ---------------------------------------------------------
# _ALLOWED_EXTS must stay in sync with skills/review/allowlist.md.

def _parse_allowlist_exts():
    """Extract the set of extensions from allowlist.md's fenced block."""
    p = os.path.join(os.path.dirname(_SCRIPTS), "skills", "review", "allowlist.md")
    text = open(p, encoding="utf-8").read()
    # The extensions live in a ```...``` block after "## Allowed source extensions"
    in_block = False
    exts = set()
    for line in text.splitlines():
        if line.strip().startswith("```") and not in_block:
            in_block = True
            continue
        if line.strip().startswith("```") and in_block:
            break
        if in_block:
            for tok in line.split():
                if tok.startswith("."):
                    exts.add(tok.lower())
    return exts


def test_allowed_exts_parity_with_allowlist_md():
    md_exts = _parse_allowlist_exts()
    py_exts = frozenset(e.lower() for e in review_gate._ALLOWED_EXTS)
    assert py_exts == md_exts, (
        f"_ALLOWED_EXTS and allowlist.md are out of sync.\n"
        f"  in code only: {sorted(py_exts - md_exts)}\n"
        f"  in markdown only: {sorted(md_exts - py_exts)}"
    )


# --- _plan_chunks: threshold and limits ---------------------------------------

def _make_entries(n, ext=".py", lines_each=10):
    return [
        {"path": f"src/f{i}{ext}", "old_path": "", "status": "M",
         "old_oid": "0" * 40, "new_oid": "1" * 40, "lines": lines_each}
        for i in range(n)
    ]


def test_plan_chunks_returns_none_below_threshold(monkeypatch):
    entries = _make_entries(5)
    monkeypatch.setattr(review_gate, "_collect_diff_entries",
                        lambda root, base, tip: (entries, []))
    monkeypatch.setattr(review_gate, "_CHUNK_THRESHOLD", 15)
    chunks, warns = _plan_chunks(".", "abc", "def")
    assert chunks is None


def test_plan_chunks_returns_chunks_above_threshold(monkeypatch):
    entries = _make_entries(20)
    monkeypatch.setattr(review_gate, "_collect_diff_entries",
                        lambda root, base, tip: (entries, []))
    monkeypatch.setattr(review_gate, "_CHUNK_THRESHOLD", 15)
    monkeypatch.setattr(review_gate, "_CHUNK_FILES", 8)
    monkeypatch.setattr(review_gate, "_CHUNK_LINES", 1200)
    chunks, warns = _plan_chunks(".", "abc", "def")
    assert chunks is not None
    assert sum(len(c) for c in chunks) == 20


def test_plan_chunks_skips_control_char_paths(monkeypatch):
    # A path containing a control character should be skipped with a warning.
    entries = _make_entries(20)  # 20 normal entries above threshold
    entries.append({
        "path": "bad\x01path.py", "old_path": "", "status": "M",
        "old_oid": "0" * 40, "new_oid": "1" * 40, "lines": 10,
    })
    raw_with_ctrl = (
        ":100644 100644 " + "0" * 40 + " " + "1" * 40 + " M\tbad\x01path.py\n"
        + "".join(f":100644 100644 {'0'*40} {'1'*40} M\tsrc/f{i}.py\n" for i in range(20))
    )
    # Call _collect_diff_entries directly (mocked); the ctrl check is in there.
    # We verify warnings are emitted.
    def _fake_diff(args, cwd=None):
        if args[:2] == ["diff", "--raw"]:
            return raw_with_ctrl, 0
        return "", 0
    monkeypatch.setattr(review_gate, "_git", _fake_diff)
    monkeypatch.setattr(review_gate, "_CHUNK_THRESHOLD", 15)
    ents, warns = _collect_diff_entries(".", "abc", "def")
    bad = [w for w in warns if "control" in w]
    assert bad, "expected a warning about the control-char path"
    assert all("bad\x01path.py" not in e["path"] for e in ents)


def test_plan_chunks_empty_base_uses_empty_tree(monkeypatch):
    # When base is "" or None, plan_chunks should fall back to _EMPTY_TREE.
    calls = []
    def _fake_collect(root, base, tip):
        calls.append(base)
        return [], []
    monkeypatch.setattr(review_gate, "_collect_diff_entries", _fake_collect)
    _plan_chunks(".", "", "abc")
    assert calls and calls[0] == review_gate._EMPTY_TREE


# --- ≤ threshold: argv unchanged ----------------------------------------------

def test_run_review_argv_unchanged_below_threshold(monkeypatch, tmp_path):
    """Below the chunk threshold, _run_review is called without --paths-file."""
    seen = {}
    monkeypatch.setattr(review_gate.subprocess, "Popen", _fake_popen(seen))
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    review_gate._run_review(str(tmp_path), "hook", str(tmp_path), "a" * 40,
                            "b" * 40 + ".." + "a" * 40)
    cmd_str = " ".join(str(c) for c in seen["cmd"])
    assert "--paths-file" not in cmd_str


def test_run_review_argv_includes_paths_file_when_given(monkeypatch, tmp_path):
    """When paths_file is given, --paths-file appears in the prompt (non-stub) cmd."""
    seen = {}
    pf = str(tmp_path / "manifest.json")
    (tmp_path / "manifest.json").write_text('{"paths": []}', encoding="utf-8")
    rng = "b" * 40 + ".." + "a" * 40
    monkeypatch.setattr(review_gate.subprocess, "Popen",
                        _fake_popen(seen, stdout='{"findings": []}'))
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    review_gate._run_review(str(tmp_path), "hook", str(tmp_path), "a" * 40,
                            rng, paths_file=pf)
    # In the non-stub path, cmd is [claude, "-p", "<prompt --paths-file ...>", ...]
    # The paths-file reference is embedded in the prompt string (cmd[2]).
    cmd_str = " ".join(str(c) for c in seen["cmd"])
    assert "--paths-file" in cmd_str


# --- near-duplicate merge -----------------------------------------------------

_BASE_FINDING = {
    "path": "app/x.py", "severity": "high", "start_line": 10, "end_line": 12,
    "confidence": 0.8, "content": "sql injection risk in build_query",
    "category": "security",
}


def test_near_dup_merge_keeps_higher_confidence():
    low = dict(_BASE_FINDING, confidence=0.7)
    high = dict(_BASE_FINDING, confidence=0.9)
    result = _merge_near_dup_findings([low, high])
    assert len(result) == 1
    assert result[0]["confidence"] == 0.9


def test_near_dup_merge_preserves_distinct_findings():
    f1 = dict(_BASE_FINDING, path="a.py")
    f2 = dict(_BASE_FINDING, path="b.py")
    result = _merge_near_dup_findings([f1, f2])
    assert len(result) == 2


def test_near_dup_different_lines_are_kept_separately():
    f1 = dict(_BASE_FINDING, start_line=10, end_line=12)
    f2 = dict(_BASE_FINDING, start_line=50, end_line=52)  # no overlap
    result = _merge_near_dup_findings([f1, f2])
    assert len(result) == 2


# --- merge_chunk_results: block propagates ------------------------------------

def test_merge_chunk_results_block_propagates():
    # _merge_chunk_results uses the skill's status vocabulary; verdict is
    # computed from findings by compute_verdict at a higher level.
    r1 = {"status": "success", "findings": [], "warnings": []}
    r2 = {"status": "completed_with_errors",
          "findings": [dict(_BASE_FINDING)], "warnings": []}
    r3 = {"status": "success", "findings": [], "warnings": []}
    merged = _merge_chunk_results([r1, r2, r3])
    assert merged["status"] == "completed_with_errors"
    assert len(merged["findings"]) == 1
    # compute_verdict on the merged result gives block (high + confidence ≥ 0.7).
    assert compute_verdict(merged) == "block"


def test_merge_chunk_results_combines_findings_from_all_chunks():
    f = lambda p: dict(_BASE_FINDING, path=p, start_line=1, end_line=2)
    r1 = {"status": "completed_with_warnings", "findings": [f("a.py")], "warnings": []}
    r2 = {"status": "completed_with_warnings", "findings": [f("b.py")], "warnings": []}
    merged = _merge_chunk_results([r1, r2])
    paths = {fn["path"] for fn in merged["findings"]}
    assert paths == {"a.py", "b.py"}


def test_merge_chunk_results_includes_planner_warnings():
    r = {"status": "pass", "verdict": "pass", "findings": [], "warnings": []}
    merged = _merge_chunk_results([r], planner_warnings=["file ceiling: 5 files skipped"])
    assert merged["warnings"] == [{"file": None, "message": "file ceiling: 5 files skipped"}]


# --- chunk cache: validate, read, write, corrupt ------------------------------

def _fake_entries():
    return [{"path": "app/x.py", "old_path": "", "status": "M",
             "old_oid": "a" * 40, "new_oid": "b" * 40, "lines": 20}]


def test_validate_chunk_cache_accepts_valid_cache(monkeypatch):
    entries = _fake_entries()
    result = {"status": "pass", "verdict": "pass", "findings": [], "warnings": []}
    cache = {
        "chunk_id": _chunk_id(entries),
        "entries": entries,
        "result": result,
        "computed_at_tip": "c" * 40,
    }
    # No findings cite any path, so blob-oid check passes trivially.
    monkeypatch.setattr(review_gate, "_blob_oids_at", lambda root, tip, paths: {})
    assert _validate_chunk_cache(cache, ".", "d" * 40) is True


def test_validate_chunk_cache_rejects_stale_finding(monkeypatch):
    # A finding on a file whose blob changed since the chunk was computed must
    # be rejected — a stale block-level finding would permanently deny the push.
    entries = _fake_entries()
    result = {
        "status": "block", "verdict": "block",
        "findings": [dict(_BASE_FINDING, path="app/x.py")],
        "warnings": [],
    }
    # computed_at_tip = "c"*40, current tip = "e"*40 — different, so check runs.
    cache = {
        "chunk_id": _chunk_id(entries),
        "entries": entries,
        "result": result,
        "computed_at_tip": "c" * 40,
    }
    # The function calls _blob_oids_at twice: once for the new tip and once for
    # computed_at_tip. Return different blobs so the comparison fails.
    def _different_blobs(root, tip, paths):
        if tip == "c" * 40:
            return {"app/x.py": "b" * 40}   # old blob
        return {"app/x.py": "d" * 40}        # new (changed) blob

    monkeypatch.setattr(review_gate, "_blob_oids_at", _different_blobs)
    assert _validate_chunk_cache(cache, ".", "e" * 40) is False


def test_corrupt_cache_treated_as_miss(tmp_path):
    d = tmp_path / "review-gate-async" / "chunks"
    d.mkdir(parents=True)
    entries = _fake_entries()
    cid = _chunk_id(entries)
    (d / f"{cid[:24]}.json").write_text("{broken", encoding="utf-8")
    result = _read_chunk_cache(str(tmp_path), cid)
    assert result is None


def test_write_then_read_chunk_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(review_gate, "_blob_oids_at", lambda root, tip, paths: {})
    entries = _fake_entries()
    result = {"status": "pass", "verdict": "pass", "findings": [], "warnings": []}
    _write_chunk_cache(str(tmp_path), _chunk_id(entries), entries, result, "c" * 40)
    cid = _chunk_id(entries)
    cached = _read_chunk_cache(str(tmp_path), cid)
    assert cached is not None
    assert cached["result"] == result
    assert _validate_chunk_cache(cached, ".", "d" * 40) is True


# --- limit detection ----------------------------------------------------------

def test_check_limit_detects_session_limit():
    hit, resets = _check_limit(
        "You've hit your session limit · resets 3:20pm (Europe/Lisbon)"
    )
    assert hit is True


def test_check_limit_detects_usage_limit_reached():
    hit, _ = _check_limit("usage limit reached, please wait")
    assert hit is True


def test_check_limit_detects_rate_limit():
    hit, _ = _check_limit("Claude API rate limit exceeded")
    assert hit is True


def test_check_limit_false_for_normal_output():
    hit, _ = _check_limit('{"status": "success", "verdict": "pass", "findings": []}')
    assert hit is False


def test_parse_resets_at_parses_lisbon_time():
    text = "You've hit your session limit · resets 3:20pm (Europe/Lisbon)"
    epoch = _parse_resets_at(text)
    # Should return a future-ish timestamp (within 24 h).
    assert epoch is not None
    assert isinstance(epoch, float)
    assert epoch > time.time() - 86400  # must be within the last 24h or future


def test_parse_resets_at_returns_none_on_garbage():
    assert _parse_resets_at("hit your limit") is None
    assert _parse_resets_at("") is None


# --- _reap_async: chunks TTL and live-worktree protection ---------------------

def test_reap_async_keeps_fresh_chunk_files(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "gate-data"))
    chunks_dir = _async_dir(str(tmp_path)) / "chunks"
    chunks_dir.mkdir(parents=True)
    fresh = chunks_dir / "abc123.json"
    fresh.write_text("{}", encoding="utf-8")
    # fresh mtime → should survive
    _reap_async(str(tmp_path))
    assert fresh.exists()


def test_reap_async_removes_old_chunk_files(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "gate-data"))
    chunks_dir = _async_dir(str(tmp_path)) / "chunks"
    chunks_dir.mkdir(parents=True)
    old = chunks_dir / "oldchunk.json"
    old.write_text("{}", encoding="utf-8")
    # Age it beyond _CHECKPOINT_TTL.
    os.utime(old, (time.time() - review_gate._CHECKPOINT_TTL - 60,) * 2)
    _reap_async(str(tmp_path))
    assert not old.exists()


def test_reap_async_does_not_remove_fresh_chunk_with_marker_age(monkeypatch, tmp_path):
    # Chunks live longer than MARKER_TTL. A chunk file that's between MARKER_TTL
    # and CHECKPOINT_TTL old must survive the sweep (it's under the chunks/ dir).
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "gate-data"))
    chunks_dir = _async_dir(str(tmp_path)) / "chunks"
    chunks_dir.mkdir(parents=True)
    mid_age = chunks_dir / "mid.json"
    mid_age.write_text("{}", encoding="utf-8")
    # Age to just past MARKER_TTL (1 h) but well within CHECKPOINT_TTL (24 h).
    os.utime(mid_age, (time.time() - MARKER_TTL - 60,) * 2)
    _reap_async(str(tmp_path))
    assert mid_age.exists()


def test_attempts_increments_when_no_new_chunks_reviewed(monkeypatch, tmp_path):
    """Cached chunks must not count as 'progress': if a run completed NO new
    chunks (chunks_new == 0) but had cached ones, the attempt counter still
    increments.  This regression test guards against the pre-fix behaviour
    where cur_chunks_done (which included cached chunks) prevented incrementing.
    """
    # Stub: 4 entries above threshold, cached chunks 0-1 valid, chunk 2 always errors.
    entries = _make_entries(4)
    cached_result = {"status": "success", "findings": [], "warnings": []}

    # Pre-populate cache for chunks 0 and 1 (OCR_CHUNK_FILES=1 → 4 chunks of 1).
    # We exercise via _supervise via the monkeypatched _stub_gate pattern.
    # Instead, test _run_chunked directly with a _run_review that always errors.

    # Patch constants so 4 files → chunked, 1 file per chunk.
    monkeypatch.setattr(review_gate, "_CHUNK_THRESHOLD", 3)
    monkeypatch.setattr(review_gate, "_CHUNK_FILES", 1)
    monkeypatch.setattr(review_gate, "_CHUNK_LINES", 99999)
    monkeypatch.setattr(review_gate, "_RUN_BUDGET", 9999)

    # Mock _collect_diff_entries to return the 4 entries.
    monkeypatch.setattr(review_gate, "_collect_diff_entries",
                        lambda root, base, tip: (entries[:], []))
    # Mock _blob_oids_at so cache validation passes.
    monkeypatch.setattr(review_gate, "_blob_oids_at",
                        lambda root, tip, paths: {})
    # Mock _ocr_tree_oid.
    monkeypatch.setattr(review_gate, "_ocr_tree_oid", lambda root, tip: "")
    # Mock _git so git clean / checkout no-ops.
    monkeypatch.setattr(review_gate, "_git", lambda args, cwd=None: ("", 0))

    # Compute chunks.
    chunks, _ = review_gate._plan_chunks(".", "base", "tip")
    assert chunks and len(chunks) == 4

    # Pre-populate cache for chunks 0 and 1.
    common = str(tmp_path)
    for chunk_entries in chunks[:2]:
        cid = review_gate._chunk_id(chunk_entries, "")
        review_gate._write_chunk_cache(common, cid, chunk_entries,
                                       cached_result, "tip")

    # _run_review always errors for chunks 2 and 3.
    def _always_error(*a, **kw):
        raise review_gate.ReviewGateError("stub error for chunk")

    monkeypatch.setattr(review_gate, "_run_review", _always_error)

    # Write a state file for the run.
    import pathlib
    async_d = pathlib.Path(common) / review_gate.ASYNC_DIR
    async_d.mkdir(parents=True, exist_ok=True)
    state_path = str(async_d / "tip.json")
    run_id = "test-run-1"
    review_gate._write_state(state_path, {"state": "running", "run_id": run_id,
                                           "attempts": 0})
    fenced = {"hit": False}

    # Only cached chunks, then an error: no new progress.
    progress = {"new": 0}
    with pytest.raises(review_gate.ReviewGateError):
        review_gate._run_chunked(
            state_path, run_id, common, ".", "hook", "",
            "tip", "base..tip", chunks, [], fenced, progress,
        )
    assert progress["new"] == 0
    assert review_gate._read_state(state_path)["chunks_done"] == 2

    # Chunk 2 is reviewed fresh, chunk 3 errors: the new chunk must survive the raise.
    calls = {"n": 0}

    def _second_errors(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return cached_result, True, ""
        raise review_gate.ReviewGateError("stub error for chunk")

    monkeypatch.setattr(review_gate, "_run_review", _second_errors)
    progress = {"new": 0}
    with pytest.raises(review_gate.ReviewGateError):
        review_gate._run_chunked(
            state_path, run_id, common, ".", "hook", "",
            "tip", "base..tip", chunks, [], fenced, progress,
        )
    assert progress["new"] == 1


def test_reap_async_skips_live_worktree(monkeypatch, tmp_path):
    # A worktree whose state file has a fresh heartbeat must not be deleted.
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "gate-data"))
    async_d = _async_dir(str(tmp_path))
    async_d.mkdir(parents=True)
    wt = _gate_data_dir() / "worktrees" / "live-wt"
    wt.mkdir(parents=True)
    # Write a state file pointing at the live worktree with a fresh heartbeat.
    state = {"state": "running", "run_id": "r1", "tip": "a" * 40,
             "heartbeat_ts": time.time(), "worktree": str(wt)}
    (async_d / ("a" * 40 + ".json")).write_text(
        json.dumps(state), encoding="utf-8"
    )
    # Age the worktree directory itself (so it would normally be swept).
    os.utime(wt, (time.time() - MARKER_TTL - 60,) * 2)
    _reap_async(str(tmp_path))
    assert wt.is_dir(), "live worktree must not be deleted by reaper"
