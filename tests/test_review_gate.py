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
    monkeypatch.setattr(review_gate, "_has_unpushed_commits", lambda: True)
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
    # The findings ride back to the calling session instead of vanishing into
    # a stderr stream Claude Code does not surface on an allow.
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
