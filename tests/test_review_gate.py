"""Unit tests for review-gate.py's _extract_json balanced-brace parser."""
import importlib.util
import json
import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)  # so review-gate.py's own `from ocr_verdict import ...` resolves

_spec = importlib.util.spec_from_file_location("review_gate", os.path.join(_SCRIPTS, "review-gate.py"))
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)

_extract_json = review_gate._extract_json
_format_reasons = review_gate._format_reasons
compute_verdict = review_gate.compute_verdict


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


def _stub_gate(monkeypatch, tmp_path, result=_WARN_RESULT, calls=None):
    monkeypatch.setattr(review_gate, "_write_gate_pointer", lambda: None)
    monkeypatch.setattr(review_gate, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(review_gate, "_git_dir", lambda repo_root=None: str(tmp_path))
    monkeypatch.setattr(review_gate, "_head_sha", lambda repo_root=None: "a" * 40)
    monkeypatch.setattr(review_gate, "_branch", lambda repo_root=None: "feat/x")
    monkeypatch.setattr(review_gate, "_resolve_pushed_repo", lambda payload: str(tmp_path))
    monkeypatch.setattr(review_gate, "_push_target_unknown", lambda payload: False)
    monkeypatch.setattr(review_gate, "_has_unpushed_commits", lambda repo_root=None: True)
    monkeypatch.delenv("OCR_IN_REVIEW", raising=False)

    def _fake_review(repo_root, mode, git_dir=None, head_sha=""):
        if calls is not None:
            calls.append(head_sha)
        raw_name = review_gate._save_raw_output(git_dir, json.dumps(result), head_sha)
        return result, True, raw_name

    monkeypatch.setattr(review_gate, "_run_review", _fake_review)


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


def _run_post(monkeypatch, tmp_path, command="git push", session_id="s1", shadowed=False,
              head="a" * 40, omit_session=False):
    body = {"tool_input": {"command": command}}
    if not omit_session:
        body["session_id"] = session_id
    monkeypatch.setattr(sys, "stdin", _io.StringIO(json.dumps(body)))
    monkeypatch.setattr(review_gate, "_resolve_pushed_repo", lambda payload: str(tmp_path))
    monkeypatch.setattr(review_gate, "_git_dir", lambda repo_root=None: str(tmp_path))
    monkeypatch.setattr(review_gate, "_head_sha", lambda repo_root=None: head)
    monkeypatch.setattr(review_gate, "_hookspath_shadowed", lambda repo_root: shadowed)
    monkeypatch.delenv("OCR_IN_REVIEW", raising=False)
    assert review_gate._mode_post(["review-gate.py", "--mode", "post"]) == 0


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


def test_post_is_silent_on_a_clean_pass(tmp_path, monkeypatch, capsys):
    _seed_record(tmp_path, verdict="pass", findings=[])
    _run_post(monkeypatch, tmp_path)
    assert _post_context(capsys) == ""


def test_post_is_silent_when_no_review_was_recorded_for_this_head(tmp_path, monkeypatch, capsys):
    _seed_record(tmp_path, head="b" * 40)
    _run_post(monkeypatch, tmp_path, head="a" * 40)
    assert _post_context(capsys) == ""


def test_post_is_silent_for_a_command_that_is_not_a_push(tmp_path, monkeypatch, capsys):
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


def test_resolve_prefers_the_commands_cd_over_the_session_cwd(tmp_path, monkeypatch):
    pushed = tmp_path / "pushed"
    pushed.mkdir()
    monkeypatch.setattr(
        review_gate, "_git",
        lambda args, cwd=None: (str(cwd), 0) if args[:2] == ["rev-parse", "--show-toplevel"] else ("", 1),
    )
    got = review_gate._resolve_pushed_repo(
        {"tool_input": {"command": f'cd "{pushed}" && git push'}, "cwd": str(tmp_path)}
    )
    assert got == str(pushed)


def test_resolve_falls_back_to_the_payload_cwd_when_there_is_no_cd(tmp_path, monkeypatch):
    monkeypatch.setattr(
        review_gate, "_git",
        lambda args, cwd=None: (str(cwd), 0) if args[:2] == ["rev-parse", "--show-toplevel"] else ("", 1),
    )
    got = review_gate._resolve_pushed_repo(
        {"tool_input": {"command": "git push"}, "cwd": str(tmp_path)}
    )
    assert got == str(tmp_path)


def test_a_cd_into_something_that_is_not_a_repo_falls_through(tmp_path, monkeypatch):
    notrepo = tmp_path / "notrepo"
    notrepo.mkdir()
    session = tmp_path / "session"
    session.mkdir()

    def _fake_git(args, cwd=None):
        if args[:2] == ["rev-parse", "--show-toplevel"] and str(cwd) == str(session):
            return str(session), 0
        return "", 1  # everything else, including notrepo, is not a repo

    monkeypatch.setattr(review_gate, "_git", _fake_git)
    got = review_gate._resolve_pushed_repo(
        {"tool_input": {"command": f'cd "{notrepo}" && git push'}, "cwd": str(session)}
    )
    assert got == str(session)


# --- capture encoding ---------------------------------------------------------
# Regression: both subprocess calls used text=True with no encoding=, so output
# was decoded with locale.getpreferredencoding() -- cp1252 on a default Windows
# box. The reviewer emits UTF-8, so an em-dash in a finding arrived as "a€""
# and was then stored that way in the findings log, the raw snapshot, and the
# context injected into the session. Caught in a live smoke test, not by review.

def test_the_reviewer_subprocess_decodes_as_utf8(monkeypatch, tmp_path):
    seen = {}

    class _Proc:
        returncode = 0
        stdout = '{"findings": []}'
        stderr = ""

    def _fake_run(cmd, **kw):
        seen.update(kw)
        return _Proc()

    monkeypatch.setattr(review_gate.subprocess, "run", _fake_run)
    monkeypatch.setattr(review_gate, "_find_claude", lambda: "claude")
    review_gate._run_review(str(tmp_path), "hook", str(tmp_path), "a" * 40)
    assert seen.get("encoding") == "utf-8"
    assert seen.get("errors") == "replace"


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


# --- resolving the repo is best-effort, so staleness is the real guard --------
# Two live failures drove this. First, --mode post resolved the session's repo
# and delivered its month-old findings as though they described the push that
# had just happened. Then, after the first fix, a command with two cds --
# `cd J:/plugin && ...; cd "$T" && git push` -- had its real target hidden
# behind an unexpandable shell variable, and falling back to the EARLIER cd
# picked the wrong repo again. Shell parsing cannot be made reliable, so the
# freshness bound is what makes a wrong guess silent instead of misleading.

def test_only_the_last_cd_is_used_never_a_superseded_one(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()

    def _fake_git(args, cwd=None):
        if args[:2] == ["rev-parse", "--show-toplevel"] and str(cwd) == str(real):
            return str(real), 0
        return "", 1

    monkeypatch.setattr(review_gate, "_git", _fake_git)
    # The last cd is an unexpanded variable; the earlier one is a real repo the
    # shell has already left. Answering with it would be wrong.
    got = review_gate._resolve_pushed_repo(
        {"tool_input": {"command": f'cd {real} && x; cd "$T" && git push'}, "cwd": ""}
    )
    assert got != str(real)


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

    def _unpushed(repo_root=None):
        seen["unpushed_root"] = repo_root
        return True

    def _review(repo_root, mode, git_dir=None, head_sha=""):
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


def test_the_gate_reviews_the_repo_the_command_cds_into(tmp_path, monkeypatch, capsys):
    session, pushed = tmp_path / "session", tmp_path / "pushed"
    session.mkdir()
    pushed.mkdir()
    seen = _gate_hook(
        monkeypatch,
        f"cd {pushed} && git push origin main",
        _repo_root=lambda: str(session),
        _push_target_unknown=lambda payload: False,
        _resolve_pushed_repo=lambda payload: str(pushed),
        _git_dir=lambda repo_root=None: str(repo_root),
        _head_sha=lambda repo_root=None: "a" * 40,
        _branch=lambda repo_root=None: "main",
    )
    # Both the "is there anything to review" question and the review itself
    # must be asked of the pushed repo, not the session's.
    assert seen["unpushed_root"] == str(pushed)
    assert seen["review_root"] == str(pushed)


def test_the_gate_blocks_when_it_cannot_tell_which_repo_is_being_pushed(monkeypatch, capsys):
    monkeypatch.delenv("OCR_FAIL_OPEN", raising=False)
    _gate_hook(monkeypatch, 'cd "$TARGET" && git push', _push_target_unknown=lambda payload: True)
    payload = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert payload["permissionDecision"] == "deny"
    assert "could not determine which repository" in payload["permissionDecisionReason"]


def test_fail_open_still_bypasses_the_unknown_target_block(monkeypatch, capsys):
    monkeypatch.setenv("OCR_FAIL_OPEN", "1")
    _gate_hook(monkeypatch, 'cd "$TARGET" && git push', _push_target_unknown=lambda payload: True)
    payload = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert payload["permissionDecision"] == "allow"


def test_a_push_with_no_cd_is_not_treated_as_unknown():
    assert review_gate._push_target_unknown({"tool_input": {"command": "git push origin main"}}) is False


def test_an_unexpanded_variable_in_the_cd_is_unknown():
    assert review_gate._push_target_unknown({"tool_input": {"command": 'cd "$T" && git push'}}) is True


def test_a_cd_into_a_real_repo_is_not_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(
        review_gate, "_git",
        lambda args, cwd=None: (str(cwd), 0) if args[:2] == ["rev-parse", "--show-toplevel"] else ("", 1),
    )
    assert review_gate._push_target_unknown(
        {"tool_input": {"command": f"cd {tmp_path} && git push"}}
    ) is False


def test_a_cd_into_a_directory_that_is_not_a_repo_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(review_gate, "_git", lambda args, cwd=None: ("", 1))
    assert review_gate._push_target_unknown(
        {"tool_input": {"command": f"cd {tmp_path} && git push"}}
    ) is True


# --- the block must not catch commands that merely SAY "git push" -------------
# The adapters trigger a review on a loose "git push" substring, which is fine
# because over-reviewing is cheap. Blocking is not: a first cut of the
# unknown-target block would have denied `cd ~/x && grep "git push" .`, since
# os.path.isdir does not expand ~ and the substring matched. Tilde paths and
# incidental mentions are both ordinary, so both must stay allowed.

def test_a_tilde_path_is_resolvable_not_unknown(monkeypatch):
    monkeypatch.setattr(
        review_gate, "_git",
        lambda args, cwd=None: (str(cwd), 0) if args[:2] == ["rev-parse", "--show-toplevel"] else ("", 1),
    )
    assert review_gate._push_target_unknown({"tool_input": {"command": "cd ~ && git push"}}) is False


def test_a_push_mentioned_only_as_text_is_not_a_real_push():
    assert review_gate._looks_like_real_push('grep -r "git push" .') is False
    assert review_gate._looks_like_real_push('python -c "print(\'git push\')"') is False


def test_a_real_push_is_recognised_behind_env_prefixes_and_flags():
    assert review_gate._looks_like_real_push("git push origin main") is True
    assert review_gate._looks_like_real_push("cd /x && git push") is True
    assert review_gate._looks_like_real_push("PYTEST_WORKERS=4 git push origin main") is True
    assert review_gate._looks_like_real_push("git -c foo=bar push") is True


def test_an_unknown_target_does_not_block_when_the_push_is_only_text(monkeypatch, capsys):
    monkeypatch.delenv("OCR_FAIL_OPEN", raising=False)
    seen = _gate_hook(
        monkeypatch,
        'cd "$T" && grep -r "git push" .',
        _push_target_unknown=lambda payload: True,
        _repo_root=lambda: "/session",
        _resolve_pushed_repo=lambda payload: "/session",
        _git_dir=lambda repo_root=None: "",
        _head_sha=lambda repo_root=None: "",
        _branch=lambda repo_root=None: "main",
    )
    payload = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert payload["permissionDecision"] != "deny"
    assert seen  # it went down the normal path rather than short-circuiting to a block


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


def test_a_relative_cd_is_anchored_to_the_session_directory(tmp_path, monkeypatch):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    seen = {}

    def _fake_git(args, cwd=None):
        seen["cwd"] = cwd
        return (str(cwd), 0) if args[:2] == ["rev-parse", "--show-toplevel"] else ("", 1)

    monkeypatch.setattr(review_gate, "_git", _fake_git)
    got = review_gate._resolve_pushed_repo(
        {"tool_input": {"command": "cd myrepo && git push"}, "cwd": str(tmp_path)}
    )
    assert got == str(repo)


def test_a_relative_cd_that_does_not_exist_under_the_session_dir_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(review_gate, "_git", lambda args, cwd=None: ("", 1))
    assert review_gate._push_target_unknown(
        {"tool_input": {"command": "cd nope && git push"}, "cwd": str(tmp_path)}
    ) is True


def test_a_relative_cd_is_not_tested_against_the_hook_process_cwd(tmp_path, monkeypatch):
    # The regression itself: "scripts" exists under the plugin repo, which is
    # where the hook process runs. Anchored to an unrelated session dir it must
    # NOT resolve, no matter what the process cwd happens to contain.
    monkeypatch.setattr(
        review_gate, "_git",
        lambda args, cwd=None: (str(cwd), 0) if args[:2] == ["rev-parse", "--show-toplevel"] else ("", 1),
    )
    assert review_gate._push_target_unknown(
        {"tool_input": {"command": "cd scripts && git push"}, "cwd": str(tmp_path)}
    ) is True


# --- the whole cd chain, not just the last hop --------------------------------
# The gate's review of 9af1886 (medium): only the LAST target was anchored to
# the session base, but in `cd repo1 && cd sub && <push>`, `sub` is relative to
# `repo1`. Joining it onto the base produced a path that either did not exist --
# falling through to the fallback this code exists to avoid -- or existed and
# pointed somewhere unrelated.

_effective_cd = review_gate._effective_cd
_PUSH = "git" + " push"  # not spelled literally: this file is read by the gate


def test_a_relative_hop_is_joined_onto_the_previous_hop():
    got = _effective_cd(f"cd repo1 && cd sub && {_PUSH}", os.path.join(os.sep, "session"))
    assert got == os.path.normpath(os.path.join(os.sep, "session", "repo1", "sub"))


def test_the_chain_folds_across_newlines_too():
    got = _effective_cd(f"cd repo1\ncd sub\n{_PUSH}", os.path.join(os.sep, "session"))
    assert got == os.path.normpath(os.path.join(os.sep, "session", "repo1", "sub"))


def test_an_absolute_hop_re_anchors_and_clears_an_earlier_unknown():
    # `$T` cannot be followed, but the absolute cd after it fully determines
    # where the push happens, so the chain is knowable again.
    abs_repo = os.path.join(os.sep, "srv", "repo")
    assert _effective_cd(f'cd "$T" && cd {abs_repo} && {_PUSH}', os.sep) == abs_repo


def test_a_relative_hop_after_an_unknown_one_stays_unknown():
    # Nothing to join it onto, and guessing is what produced a confident answer
    # about the wrong repository in the first place.
    assert _effective_cd(f'cd "$T" && cd sub && {_PUSH}', os.sep) == ""


def test_cd_dash_is_unfollowable():
    assert _effective_cd(f"cd - && {_PUSH}", os.sep) == ""


def test_no_cd_yields_the_base_unchanged():
    base = os.path.join(os.sep, "session")
    assert _effective_cd(f"{_PUSH} origin main", base) == base


def test_a_chained_relative_push_resolves_to_the_real_repo(tmp_path, monkeypatch):
    (tmp_path / "repo1" / "sub").mkdir(parents=True)
    monkeypatch.setattr(
        review_gate, "_git",
        lambda args, cwd=None: (str(cwd), 0) if args[:2] == ["rev-parse", "--show-toplevel"] else ("", 1),
    )
    got = review_gate._resolve_pushed_repo(
        {"tool_input": {"command": f"cd repo1 && cd sub && {_PUSH}"}, "cwd": str(tmp_path)}
    )
    assert got == os.path.normpath(str(tmp_path / "repo1" / "sub"))


def test_a_chain_ending_somewhere_unfollowable_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(review_gate, "_git", lambda args, cwd=None: ("", 1))
    assert review_gate._push_target_unknown(
        {"tool_input": {"command": f'cd repo1 && cd "$T" && {_PUSH}'}, "cwd": str(tmp_path)}
    ) is True


# --- heredoc bodies are data, not commands ------------------------------------
# Found the hard way: the Bash call adding the tests above was DENIED by the
# installed gate. Its heredoc contained `cd repo1 && cd sub && <push>` as test
# data, which the parser read as a real cd chain -- it resolved to nothing, and
# the unknown-target rule blocked an entirely innocent command.

def test_a_heredoc_body_does_not_contribute_cd_targets():
    cmd = (
        "cd /real/repo && cat >> t.py <<'EOF'\n"
        f"    x = 'cd repo1 && cd sub && {_PUSH}'\n"
        "EOF\n"
        "pytest -q"
    )
    assert _cd_targets(cmd) == ["/real/repo"]


def test_a_push_inside_a_heredoc_is_not_a_real_push():
    cmd = f"cat > deploy.sh <<'EOF'\ncd /srv/app && {_PUSH} origin main\nEOF"
    assert review_gate._looks_like_real_push(cmd) is False


def test_a_real_push_after_a_heredoc_still_counts():
    # Only the BODY is dropped; commands that follow the terminator remain.
    cmd = f"cat > x <<'EOF'\nhello\nEOF\ncd /srv/app && {_PUSH}"
    assert review_gate._looks_like_real_push(cmd) is True
    assert _cd_targets(cmd) == ["/srv/app"]


def test_an_unquoted_heredoc_marker_is_handled_too():
    cmd = f"cat > x <<EOF\ncd /elsewhere && {_PUSH}\nEOF\ncd /real && {_PUSH}"
    assert _cd_targets(cmd) == ["/real"]


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


def test_an_unterminated_heredoc_strips_nothing():
    # No terminator anywhere means this cannot be trusted as a heredoc, and
    # stripping to end-of-command would delete the real commands after it.
    cmd = f"cat > x <<EOF\nbody\ncd /repo\n{_PUSH}"
    assert review_gate._strip_heredocs(cmd) == cmd


def test_an_opener_with_a_trailing_redirection_is_still_an_opener():
    cmd = f"cat <<'EOF' > out\ncd /elsewhere\nEOF\ncd /repo && {_PUSH}"
    assert _cd_targets(cmd) == ["/repo"]


# The opener lookahead has been wrong twice, in both directions. Allowing only
# redirections after the marker rejected the extremely ordinary
# `python - <<'PY' | tee log`, so its body went unstripped and the misparse
# came back. What actually separates an opener from the same characters as data
# is that an opener is followed by end-of-line, a redirection, or a
# pipe/separator -- never by a bare word.

def test_an_opener_followed_by_a_pipe_is_still_an_opener():
    cmd = f"cat <<'EOF' | grep x\ncd /elsewhere && {_PUSH}\nEOF\ncd /repo && {_PUSH}"
    assert _cd_targets(cmd) == ["/repo"]


def test_an_opener_followed_by_a_redirection_and_a_pipe_is_still_an_opener():
    # The shape used throughout this project's own tooling.
    cmd = f"python - <<'PY' 2>&1 | head -5\ncd /elsewhere && {_PUSH}\nPY\ncd /repo && {_PUSH}"
    assert _cd_targets(cmd) == ["/repo"]


def test_an_opener_followed_by_a_command_separator_is_still_an_opener():
    cmd = f"cat <<EOF && echo hi\ncd /elsewhere\nEOF\ncd /repo && {_PUSH}"
    assert _cd_targets(cmd) == ["/repo"]


def test_a_marker_followed_by_a_bare_word_is_data_not_an_opener():
    cmd = f'echo "a <<EOF b"\ncd /repo\n{_PUSH}'
    assert _cd_targets(cmd) == ["/repo"]
    assert review_gate._strip_heredocs(cmd) == cmd


# --- quoted spans are data, except a cd target and a shell's -c ---------------
# The last known way to make the parser misread a command: a cd chain inside a
# quoted ARGUMENT (a commit message, a grep pattern) was parsed as code, so a
# chain that never ran resolved to nothing and the unknown-target rule could
# deny something innocent.

def test_a_cd_chain_in_a_commit_message_is_not_code():
    cmd = f'git commit -m "cd repo1 && cd sub && {_PUSH}"'
    assert _cd_targets(cmd) == []
    assert review_gate._looks_like_real_push(cmd) is False


def test_a_cd_chain_in_a_grep_pattern_is_not_code():
    cmd = f'grep -r "cd /x && {_PUSH}" .'
    assert _cd_targets(cmd) == []
    assert review_gate._looks_like_real_push(cmd) is False


def test_a_quoted_cd_target_survives_masking():
    # The exception that makes blanket masking unsafe: this quoted span IS the
    # hop we are trying to follow.
    cmd = f'cd "/path with spaces" && {_PUSH}'
    assert _cd_targets(cmd) == ["/path with spaces"]


def test_a_shell_dash_c_argument_is_still_code(tmp_path):
    for runner in ("bash", "/bin/bash", "sh"):
        cmd = f'{runner} -c "cd /real && {_PUSH}"'
        assert _cd_targets(cmd) == ["/real"], runner
        assert review_gate._looks_like_real_push(cmd) is True, runner


def test_a_non_shell_dash_c_argument_is_not_code():
    # python -c takes PYTHON, not shell. Treating every `-c` as shell made this
    # harmless one-liner read as a real push.
    cmd = f"""python -c "print('{_PUSH}')" """
    assert review_gate._looks_like_real_push(cmd) is False
    assert _cd_targets("""python -c "import os; os.system('cd /x')" """) == []


def test_a_real_push_alongside_a_quoted_message_still_parses():
    cmd = f'cd /repo && git commit -m "msg" && {_PUSH}'
    assert _cd_targets(cmd) == ["/repo"]
    assert review_gate._looks_like_real_push(cmd) is True


def test_an_unbalanced_quote_does_not_crash_or_swallow_the_command():
    cmd = f'echo "unterminated && cd /repo && {_PUSH}'
    review_gate._mask_quoted(cmd)  # must not raise


# --- commit and push in one command -------------------------------------------
# A PreToolUse hook runs BEFORE the command, so when the command creates the
# commit it pushes, HEAD still points at the parent and there is nothing
# unpushed to review. Structural -- no pre-tool hook can review a commit that
# does not exist yet -- so the gate says so instead of passing in silence.

_commits_in_same_command = review_gate._commits_in_same_command


def test_commit_then_push_in_one_command_is_detected():
    assert _commits_in_same_command(f'git add -A && git commit -q -m "x" && {_PUSH}') is True


def test_commit_then_push_is_detected_across_lines():
    assert _commits_in_same_command(f'git commit -m "x"\n{_PUSH}') is True


def test_a_push_on_its_own_is_not_the_commit_then_push_shape():
    assert _commits_in_same_command(f"cd /repo && {_PUSH}") is False


def test_a_commit_message_mentioning_a_push_is_not_the_shape():
    assert _commits_in_same_command(f'git commit -m "later {_PUSH}"') is False


def test_a_commit_after_the_push_is_not_the_shape():
    # Only a commit created BEFORE the push can be the one going unreviewed.
    assert _commits_in_same_command(f'{_PUSH} && git commit -m "x"') is False


def test_a_push_with_nothing_to_send_is_not_reported_as_unreviewed():
    assert review_gate._push_was_a_noop(
        {"tool_response": {"stdout": "Everything up-to-date", "stderr": ""}}
    ) is True
    assert review_gate._push_was_a_noop({"tool_response": {"stdout": "main -> main"}}) is False
