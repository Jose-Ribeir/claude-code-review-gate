"""Tests for scripts/ocr_memory.py — the dismissed-finding memory store (t08).

Covers the unit seam the ticket names: dismiss()/match() semantics, the
gate-side demotion wiring (_apply_memory_demotion), finding-id resolution
against the recorded verdicts log (_dismiss_finding), and the two structural
guards: nothing but the dismiss path writes the store, and nothing learns
from --no-verify.
"""
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")
sys.path.insert(0, _SCRIPTS)

import ocr_memory as om  # noqa: E402

_spec = importlib.util.spec_from_file_location("review_gate_mem", _GATE)
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)

# A code line long and specific enough to survive signature_line()'s filters
# (>= 20 normalized chars, not a bare keyword).
SIG = "if user.is_service_account:  # quota check skipped for service accounts"
CONTENT = "quota check is missing for service accounts"


def _finding(**over):
    f = {
        "path": "app/x.py",
        "start_line": 3,
        "end_line": 5,
        "severity": "high",
        "confidence": 0.95,
        "category": "correctness",
        "content": CONTENT,
        "existing_code": SIG,
    }
    f.update(over)
    return f


def _records(repo_root):
    path = om.store_path(repo_root)
    try:
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except OSError:
        return []


# ---------------------------------------------------------------------------
# dismiss()
# ---------------------------------------------------------------------------

def test_dismiss_appends_one_record(tmp_path):
    rec = om.dismiss(str(tmp_path), _finding())
    assert isinstance(rec, dict)
    path = om.store_path(str(tmp_path))
    assert path == os.path.join(str(tmp_path), ".ocr", "memory.jsonl")
    lines = open(path, encoding="utf-8").read().splitlines()
    assert len(lines) == 1
    stored = json.loads(lines[0])
    assert stored["path"] == "app/x.py"
    assert stored["glob"] == "app/x.py"  # default glob is the exact path
    assert stored["category"] == "correctness"
    assert stored["signature"] == " ".join(SIG.split())  # whitespace-collapsed
    assert stored["at"] and stored["ts"] > 0


def test_dismiss_is_append_only(tmp_path):
    om.dismiss(str(tmp_path), _finding())
    om.dismiss(str(tmp_path), _finding(path="lib/y.py", category="security"))
    recs = _records(str(tmp_path))
    assert len(recs) == 2
    assert recs[0]["path"] == "app/x.py" and recs[1]["path"] == "lib/y.py"


def test_dismiss_honours_glob_override(tmp_path):
    rec = om.dismiss(str(tmp_path), _finding(), glob="app/*.py")
    assert rec["glob"] == "app/*.py"
    assert _records(str(tmp_path))[0]["glob"] == "app/*.py"


def test_dismiss_never_raises_on_broken_root(tmp_path):
    # A root whose .ocr directory cannot exist must not raise; the record is
    # still returned annotated so the caller can say what happened.
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    rec = om.dismiss(str(blocker), _finding())
    assert isinstance(rec, dict)
    assert rec.get("error")


# ---------------------------------------------------------------------------
# match()
# ---------------------------------------------------------------------------

def test_match_hit(tmp_path):
    om.dismiss(str(tmp_path), _finding())
    hit = om.match(str(tmp_path), _finding())
    assert hit is not None
    assert hit["path"] == "app/x.py"


def test_match_no_store_returns_none(tmp_path):
    assert om.match(str(tmp_path), _finding()) is None


def test_match_requires_category(tmp_path):
    om.dismiss(str(tmp_path), _finding())
    assert om.match(str(tmp_path), _finding(category="security")) is None


def test_match_honours_glob(tmp_path):
    om.dismiss(str(tmp_path), _finding(), glob="app/*.py")
    assert om.match(str(tmp_path), _finding(path="app/other.py")) is not None
    assert om.match(str(tmp_path), _finding(path="lib/other.py")) is None


def test_match_uses_signature_line_normalization(tmp_path):
    """The same defect seen with different indentation/spacing still matches:
    signature_line collapses whitespace and picks the normalized line."""
    om.dismiss(str(tmp_path), _finding())
    spaced = "  if   user.is_service_account:    # quota check skipped for service accounts\n"
    assert om.match(str(tmp_path), _finding(existing_code=spaced)) is not None
    # A different code line is not the same defect.
    other = "if user.is_admin:  # admin bypass audited elsewhere"
    assert om.match(str(tmp_path), _finding(existing_code=other)) is None


def test_match_fallback_when_no_signature_line(tmp_path):
    """Code with no line >= 20 chars has no signature: match falls back to the
    normalized full-code key so tiny snippets can still be dismissed."""
    tiny = "pass"
    om.dismiss(str(tmp_path), _finding(existing_code=tiny))
    spaced_tiny = "  pass   \n"
    assert om.match(str(tmp_path), _finding(existing_code=spaced_tiny)) is not None
    assert om.match(str(tmp_path), _finding(existing_code="continue")) is None


def test_match_latest_dismissal_wins(tmp_path):
    om.dismiss(str(tmp_path), _finding())
    second = om.dismiss(str(tmp_path), _finding())
    hit = om.match(str(tmp_path), _finding())
    assert hit["ts"] == pytest_ts(second) or hit["ts"] == second["ts"]
    assert hit["id"] == second["id"]


def pytest_ts(rec):
    return rec["ts"]


def test_match_skips_corrupt_lines(tmp_path):
    om.dismiss(str(tmp_path), _finding())
    path = om.store_path(str(tmp_path))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{not json\n")
        fh.write("\n")
        fh.write(json.dumps(["a list, not a record"]) + "\n")
    assert om.match(str(tmp_path), _finding()) is not None


def test_match_never_raises_on_nonsense(tmp_path):
    assert om.match(str(tmp_path / "missing"), _finding()) is None
    assert om.match(str(tmp_path), {"path": None}) is None
    assert om.match(str(tmp_path), "not a finding") is None


# ---------------------------------------------------------------------------
# Demotion wiring (gate side, T1/verdict assembly)
# ---------------------------------------------------------------------------

def _result(findings):
    return {"status": "success", "verdict": "block", "findings": findings,
            "warnings": []}


def test_demotion_lowers_severity_and_annotates(tmp_path):
    om.dismiss(str(tmp_path), _finding())
    original = _finding()
    result = review_gate._apply_memory_demotion(str(tmp_path), _result([original]))
    demoted = result["findings"][0]
    assert demoted["severity"] == "low"
    assert demoted["content"].startswith("previously dismissed")
    assert CONTENT in demoted["content"]  # original text preserved after prefix
    assert original["severity"] == "high"  # input finding object untouched
    assert len(result["warnings"]) == 1
    w = result["warnings"][0]
    msg = w["message"] if isinstance(w, dict) else str(w)
    assert "dismiss" in msg.lower()
    assert "app/x.py" in msg


def test_demotion_leaves_unmatched_findings_alone(tmp_path):
    om.dismiss(str(tmp_path), _finding())
    other = _finding(path="lib/z.py")
    result = review_gate._apply_memory_demotion(str(tmp_path), _result([other]))
    assert result["findings"][0]["severity"] == "high"
    assert result["warnings"] == []


def test_demotion_flips_block_to_pass(tmp_path):
    from ocr_verdict import compute_verdict
    om.dismiss(str(tmp_path), _finding())
    result = _result([_finding()])
    assert compute_verdict(result) == "block"
    demoted = review_gate._apply_memory_demotion(str(tmp_path), result)
    assert compute_verdict(demoted) == "pass"


def test_demotion_does_not_write_the_store(tmp_path):
    om.dismiss(str(tmp_path), _finding())
    path = om.store_path(str(tmp_path))
    before = open(path, "rb").read()
    review_gate._apply_memory_demotion(str(tmp_path), _result([_finding()]))
    assert open(path, "rb").read() == before


def test_demotion_without_store_adds_no_files(tmp_path):
    result = review_gate._apply_memory_demotion(str(tmp_path), _result([_finding()]))
    assert result["findings"][0]["severity"] == "high"
    assert not os.path.exists(os.path.join(str(tmp_path), ".ocr"))


def test_demotion_skips_already_low_and_non_dicts(tmp_path):
    om.dismiss(str(tmp_path), _finding())
    result = review_gate._apply_memory_demotion(
        str(tmp_path), _result([_finding(severity="low"), "junk", _finding(path="no/match.py")]))
    low = result["findings"][0]
    assert low["severity"] == "low"
    assert not low["content"].startswith("previously dismissed")
    assert result["warnings"] == []


def test_demotion_never_raises(tmp_path):
    result = review_gate._apply_memory_demotion(
        str(tmp_path / "nope"), {"findings": [{"path": None, "severity": 5}]})
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Dismissal resolution: /review-gate:dismiss <finding-id>
# ---------------------------------------------------------------------------

def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def _init_repo(tmp_path):
    for cmd in (["init", "-b", "main"], ["config", "user.email", "t@t.com"],
                ["config", "user.name", "t"], ["config", "commit.gpgsign", "false"]):
        _git(cmd, str(tmp_path))
    (tmp_path / "x.txt").write_text("hi", encoding="utf-8")
    _git(["add", "."], str(tmp_path))
    _git(["commit", "-q", "-m", "base"], str(tmp_path))


def _write_log(git_dir, entries):
    import pathlib
    path = pathlib.Path(git_dir) / review_gate.FINDINGS_LOG
    with open(path, "a", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return str(path)


def _log_entry(findings, **over):
    e = {"ts": 1.0, "at": "2026-11-10T00:00:00Z", "head": "abc",
         "branch": "main", "mode": "hook", "verdict": "block",
         "advisory": False, "blocked": True, "finding_count": len(findings),
         "findings": findings, "truncated": False, "raw": ""}
    e.update(over)
    return e


def test_dismiss_resolves_finding_id_from_last_verdict(tmp_path, monkeypatch, capsys):
    """The finding id the user passes is resolved against the findings the
    gate recorded for its last verdict (the findings log), then dismissed."""
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = _finding()
    _write_log(str(tmp_path / ".git"),
               [_log_entry([_finding(path="other.py")]), _log_entry([target])])
    fid = review_gate._finding_id(target)
    rc = review_gate._dismiss_finding(["--dismiss", fid])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "dismissed" in out.lower()
    recs = _records(str(tmp_path))
    assert len(recs) == 1
    assert recs[0]["id"] == fid
    assert recs[0]["path"] == "app/x.py"


def test_dismiss_resolves_finding_s_own_id_field(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = _finding(id="reviewer-given-id-123")
    _write_log(str(tmp_path / ".git"), [_log_entry([target])])
    rc = review_gate._dismiss_finding(["--dismiss", "reviewer-given-id-123"])
    assert rc == 0
    assert _records(str(tmp_path))[0]["id"] == "reviewer-given-id-123"


def test_dismiss_resolves_unique_prefix(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = _finding()
    _write_log(str(tmp_path / ".git"), [_log_entry([target])])
    fid = review_gate._finding_id(target)
    rc = review_gate._dismiss_finding(["--dismiss", fid[:12]])
    assert rc == 0


def test_dismiss_keeps_recorded_context(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = _finding()
    _write_log(str(tmp_path / ".git"),
               [_log_entry([target], verdict="warn", head="f00daa", branch="dev")])
    fid = review_gate._finding_id(target)
    assert review_gate._dismiss_finding(["--dismiss", fid]) == 0
    rec = _records(str(tmp_path))[0]
    assert rec["verdict"] == "warn"
    assert rec["head"] == "f00daa" and rec["branch"] == "dev"


def test_dismiss_glob_override(tmp_path, monkeypatch):
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = _finding()
    _write_log(str(tmp_path / ".git"), [_log_entry([target])])
    fid = review_gate._finding_id(target)
    rc = review_gate._dismiss_finding(["--dismiss", fid, "--glob", "app/*.py"])
    assert rc == 0
    assert _records(str(tmp_path))[0]["glob"] == "app/*.py"


def test_dismiss_unknown_id_refuses_and_hints(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    _write_log(str(tmp_path / ".git"), [_log_entry([_finding()])])
    rc = review_gate._dismiss_finding(["--dismiss", "deadbeef" * 8])
    out = capsys.readouterr().out
    assert rc == 1
    # Refusal must help the user pick a real id.
    assert review_gate._finding_id(_finding()) in out
    assert not os.path.exists(om.store_path(str(tmp_path)))


def test_dismiss_searches_only_last_verdict(tmp_path, monkeypatch, capsys):
    """Only the most recent recorded verdict is a dismissal source: an older
    entry's finding ids are not resolvable."""
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    old = _finding()
    _write_log(str(tmp_path / ".git"),
               [_log_entry([old]), _log_entry([_finding(path="fresh.py")])])
    rc = review_gate._dismiss_finding(["--dismiss", review_gate._finding_id(old)])
    assert rc == 1
    capsys.readouterr()  # drain
    assert not os.path.exists(om.store_path(str(tmp_path)))


def test_dismiss_no_history_refuses(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = review_gate._dismiss_finding(["--dismiss", review_gate._finding_id(_finding())])
    assert rc == 1
    capsys.readouterr()
    assert not os.path.exists(om.store_path(str(tmp_path)))


def test_dismiss_mode_dispatches_from_main(tmp_path, monkeypatch, capsys):
    """--dismiss is handled like --history: a standalone query/writer that does
    not spawn a review or need a hook payload."""
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = _finding()
    _write_log(str(tmp_path / ".git"), [_log_entry([target])])
    fid = review_gate._finding_id(target)
    with monkeypatch.context() as m:
        m.setattr(sys, "argv", ["review-gate.py"])
        try:
            review_gate.main(["review-gate.py", "--dismiss", fid])
        except SystemExit as exc:
            assert exc.code == 0
        else:
            raise AssertionError("main() must sys.exit after --dismiss")
    assert len(_records(str(tmp_path))) == 1


# ---------------------------------------------------------------------------
# Structural guards
# ---------------------------------------------------------------------------

def test_only_the_dismiss_path_writes_the_store():
    """ocr_memory.dismiss is the single store writer module-wide; review-gate
    calls it from exactly one place (the --dismiss resolution path)."""
    import inspect
    src = open(_GATE, encoding="utf-8").read()
    call_sites = re.findall(r"ocr_memory\.dismiss", src)
    assert len(call_sites) == 1
    dismiss_src = inspect.getsource(review_gate._dismiss_finding)
    assert "ocr_memory.dismiss" in dismiss_src
    demotion_src = inspect.getsource(review_gate._apply_memory_demotion)
    assert "dismiss(" not in demotion_src  # the read path only matches


def test_nothing_learns_from_no_verify():
    """Bypasses never teach: no memory/demotion/dismissal code path may know
    about --no-verify."""
    import inspect
    mem_src = open(os.path.join(_SCRIPTS, "ocr_memory.py"), encoding="utf-8").read()
    assert "no-verify" not in mem_src
    for fn in (review_gate._apply_memory_demotion, review_gate._dismiss_finding):
        assert "no-verify" not in inspect.getsource(fn)
        assert "no_verify" not in inspect.getsource(fn)


def test_reviewer_guard_still_forbids_ocr_writes():
    """The review session's Bash guard already refuses writes into .ocr/ — the
    memory store included. A regression there would let the model teach the
    store, bypassing the dismiss command."""
    assert ".ocr" in review_gate._FORBIDDEN_COMPONENTS


def test_store_line_is_self_describing(tmp_path):
    """One example of the on-disk record, pinned so format changes are loud."""
    om.dismiss(str(tmp_path), _finding(id="abc123"), glob="app/*.py")
    rec = _records(str(tmp_path))[0]
    expected_keys = {"v", "id", "at", "ts", "path", "glob", "category",
                     "signature", "code_key", "summary"}
    assert expected_keys <= set(rec)
    assert rec["v"] == 1
    # code_key is the sha256 of the whitespace-collapsed existing_code.
    key = hashlib.sha256(" ".join(SIG.split()).encode("utf-8")).hexdigest()
    assert rec["code_key"] == key
