"""A waiver keeps matching the same TEST failure across runs.

A test finding quotes the run's output, and that output carries run times
("1 failed in 0.41s"). Before the fix the waiver identity hashed those times,
so every run produced a new id and the waiver never matched twice. The gate now
strips run-dependent tokens (times, durations, timestamps) from the evidence
before fingerprinting it (ADR 0007 addendum "Run-dependent tokens").

Driven through the gate's git mode (`--mode git`, the pre-push path) against a
stub test executor -- a repo-committed command check whose stdout is scripted
per push through STUB_CHECK_OUT -- and the `waive` operator command. Nothing
here calls the fingerprint function: what is checked is the finding id the
record carries and whether the waiver suppresses the finding.
"""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
_GATE = os.path.join(_SCRIPTS, "review-gate.py")

sys.path.insert(0, _SCRIPTS)
from test_pg5_orchestrator import (  # noqa: E402,F401
    _env, _git, _git_mode, _line, repo)

_spec = importlib.util.spec_from_file_location("review_gate_waiver_evidence", _GATE)
rg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rg)

_APP = ("def compute_pricing_totals_for_the_cart_and_apply_discounts(c):\n"
        "    return 1\n" + "".join(f"# filler {i}\n" for i in range(1, 31)))

_STUB_PY = ("import os, sys\n"
            "sys.stdin.read()\n"
            "sys.stdout.write(os.environ.get('STUB_CHECK_OUT', '{\"findings\": []}'))\n")

_FAIL_A = ("FAILED tests/test_pricing.py::test_total - assert total == 12 got 7\n")
_SUMMARY = "=========== 1 failed in {t} ===========\n"


def _out(content, evidence_source="existing_test"):
    finding = {"path": "app.py", "start_line": 1, "end_line": 1,
               "severity": "high", "confidence": 0.9, "category": "test",
               "content": content}
    if evidence_source:
        finding["evidence_source"] = evidence_source
    return json.dumps({"findings": [finding]})


@pytest.fixture
def work(repo, tmp_path):  # noqa: F811
    """A feature branch holding the app file, the stub test executor and the
    check config that wires it to *.py changes."""
    _git(["switch", "-q", "-c", "feat"], cwd=repo)
    (repo / "checks").mkdir()
    (repo / "checks" / "stub.py").write_text(_STUB_PY, encoding="utf-8")
    if sys.platform == "win32":
        # CreateProcess runs .cmd directly; an sh shebang it cannot run.
        wrapper, body = "run.cmd", f'@"{sys.executable}" "%~dp0stub.py"\r\n'
    else:
        wrapper, body = "run", f'#!/bin/sh\nexec "{sys.executable}" "$(dirname "$0")/stub.py"\n'
    (repo / "checks" / wrapper).write_text(body, encoding="utf-8", newline="")
    (repo / "checks" / wrapper).chmod(0o755)
    (repo / ".ocr").mkdir()
    (repo / ".ocr" / "config.json").write_text(json.dumps(
        {"checks": {"pricing": {"command": f"checks/{wrapper}",
                                "globs": ["**/*.py"]}}}), encoding="utf-8")
    (repo / "app.py").write_text(_APP, encoding="utf-8")
    _git(["add", "."], cwd=repo)
    _git(["commit", "-q", "-m", "app, stub executor, check config"], cwd=repo)
    return repo


_counter = [0]


def _push(work, tmp_path, out, **env_kw):
    """One pre-push run (git mode) over a NEW commit (a far-field edit of the
    app file, so the check's globs match and the tip is new) with the stub
    executor answering `out`. Returns the completed process."""
    _counter[0] += 1
    app = (work / "app.py").read_text(encoding="utf-8")
    (work / "app.py").write_text(app + f"# edit {_counter[0]}\n", encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", f"edit {_counter[0]}"], cwd=work)
    tip = _git(["rev-parse", "HEAD"], cwd=work)
    env = _env(tmp_path, OCR_HARNESS="claude", STUB_CHECK_OUT=out, **env_kw)
    proc = _git_mode(work, [_line(tip)], env)
    proc.env = env
    return proc


def _gate(work, env, *args):
    return subprocess.run([sys.executable, _GATE, *args], capture_output=True,
                          text=True, cwd=str(work), env=env, timeout=60)


def _record(work):
    entries = rg._read_history(rg._git_dir(str(work)), 1)
    assert entries, "no recorded review"
    return entries[-1]


def _only(findings):
    findings = [f for f in findings if isinstance(f, dict)]
    assert len(findings) == 1, findings
    return findings[0]


def _id(f):
    return str(f.get("id") or "").strip() or rg._finding_id(f)[:12]


def _blocked(work, tmp_path, out, **env_kw):
    """A push that blocks on exactly one fresh finding; returns (finding, env)."""
    proc = _push(work, tmp_path, out, **env_kw)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    rec = _record(work)
    assert rec["verdict"] == "block" and not rec.get("waived"), rec
    return _only(rec["findings"]), proc.env


def _waive(work, env, finding):
    proc = _gate(work, env, "waive", _id(finding))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "waived [" in proc.stdout, proc.stdout + proc.stderr


# --- the seven behaviours ------------------------------------------------------------

def test_same_failure_with_a_different_run_time_keeps_its_id_and_its_waiver(work, tmp_path):
    first, env = _blocked(work, tmp_path, _out(_FAIL_A + _SUMMARY.format(t="0.41s")))
    _waive(work, env, first)

    proc = _push(work, tmp_path, _out(_FAIL_A + _SUMMARY.format(t="3.97s")))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rec = _record(work)
    assert rec["verdict"] == "pass" and not rec["findings"], rec
    waived = _only(rec["waived"])
    assert _id(waived) == _id(first), "the same failure got a new id on the next run"
    # The run time is the only thing that differs; the record still shows it.
    assert "3.97s" in waived["content"] and "0.41s" in first["content"]


def test_timestamps_and_elapsed_fields_do_not_change_the_waiver_match(work, tmp_path):
    ev = ("2026-11-21T10:11:12.345Z ERROR t_pricing assert total == 12 got 7\n"
          '{"elapsed": 0.41, "passed": 0, "failed": 1}\n')
    first, env = _blocked(work, tmp_path, _out(ev))
    _waive(work, env, first)
    later = ev.replace("2026-11-21T10:11:12.345Z", "2026-11-22T23:59:01.002Z") \
              .replace('"elapsed": 0.41', '"elapsed": 12.5')
    proc = _push(work, tmp_path, _out(later))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _id(_only(_record(work)["waived"])) == _id(first)


def test_two_different_failures_get_different_ids(work, tmp_path):
    first, env = _blocked(work, tmp_path, _out(_FAIL_A + _SUMMARY.format(t="0.41s")))
    _waive(work, env, first)

    other = ("FAILED tests/test_pricing.py::test_tax - assert tax == 0.2 got 0.0\n"
             + _SUMMARY.format(t="0.41s"))
    second, _ = _blocked(work, tmp_path, _out(other))   # blocks: the waiver did not leak
    assert _id(second) != _id(first)


def test_a_changed_assertion_message_is_a_new_id_and_blocks_fresh(work, tmp_path):
    first, env = _blocked(work, tmp_path, _out(_FAIL_A + _SUMMARY.format(t="0.41s")))
    _waive(work, env, first)

    # Same test, same time, but the failing VALUES differ (digits that are part of
    # the failure are never stripped): a changed finding.
    changed = _FAIL_A.replace("got 7", "got 8") + _SUMMARY.format(t="0.41s")
    second, _ = _blocked(work, tmp_path, _out(changed))
    assert _id(second) != _id(first)
    # ... and so is a different line number in the traceback.
    third, _ = _blocked(work, tmp_path, _out(
        "tests/test_pricing.py:41: AssertionError\n" + _SUMMARY.format(t="0.41s")))
    fourth, _ = _blocked(work, tmp_path, _out(
        "tests/test_pricing.py:42: AssertionError\n" + _SUMMARY.format(t="0.41s")))
    assert _id(third) != _id(fourth)


def test_a_finding_with_no_evidence_stays_unwaivable(work, tmp_path):
    # Evidence that is nothing but a run-dependent token is no evidence either.
    for content in ("", "   ", "2026-11-21T10:11:12Z"):
        proc = _push(work, tmp_path, _out(content))
        assert proc.returncode in (0, 1), proc.stdout + proc.stderr
        rec = _record(work)
        finding = _only(rec["findings"])           # reported, never suppressed
        res = _gate(work, proc.env, "waive", _id(finding))
        assert "no computable content anchor" in res.stdout + res.stderr,             (content, res.stdout, res.stderr)
        assert rg.rg_store.read_waivers(rg._git_common_dir(str(work))) == [], content
        _push(work, tmp_path, _out(content))
        rec = _record(work)
        assert _only(rec["findings"]) and not rec.get("waived"), content


def test_reviewer_findings_keep_their_current_ids(work, tmp_path):
    """A finding with no test evidence hashes its content verbatim, run-time
    text and all: its id is the one the gate has always computed."""
    sig = "if user.is_service_account:  # quota check skipped in 0.41s"
    content = "quota check is missing for service accounts, slow in 0.41s"
    (work / "lib.py").write_text(f"def handle(user):\n    {sig}\n    pass\n", encoding="utf-8")
    _git(["add", "."], cwd=work)
    _git(["commit", "-q", "-m", "reviewer target"], cwd=work)
    scripted = json.dumps({"lib.py": {"severity": "high", "content": content,
                                      "existing_code": sig}})
    first, _ = _blocked(work, tmp_path, '{"findings": []}', STUB_FINDINGS_FOR=scripted)
    expected = hashlib.sha256("\x00".join(["lib.py", sig, content]).encode()).hexdigest()
    assert first["path"] == "lib.py"
    assert _id(first) in (expected[:12], expected), (first.get("id"), expected)


def test_waive_list_marks_a_waiver_that_no_longer_matches(work, tmp_path):
    first, env = _blocked(work, tmp_path, _out(_FAIL_A + _SUMMARY.format(t="0.41s")))
    _waive(work, env, first)
    # A waiver granted under the old fingerprint rule: an anchor no finding has now.
    common = rg._git_common_dir(str(work))
    ledger = rg.rg_store.read_waivers(common)
    ledger.append({"sid": "0123456789abcdef", "finding_id": "oldwaiver01",
                   "waived_at": time.time()})
    rg.rg_store.write_waivers(common, ledger)
    # The last recorded verdict must hold the live finding: a blocked push.
    second, env = _blocked(work, tmp_path, _out("FAILED test_other - assert 1 == 2\n"))

    proc = _gate(work, env, "waive", "--list")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = {ln.split()[1]: ln for ln in proc.stdout.splitlines()
             if ln.startswith("  ") and len(ln.split()) > 2}
    assert "NO LONGER MATCHES" in lines["oldwaiver01"], proc.stdout
    assert "re-grant" in proc.stdout
    assert "no longer match" in proc.stdout.splitlines()[-1]
    # The first waiver's failure is not in the last verdict either: it is listed
    # and marked too ("matches nothing in the last recorded verdict").
    assert "NO LONGER MATCHES" in lines[_id(first)], proc.stdout


def test_waive_list_does_not_mark_a_waiver_that_still_matches(work, tmp_path):
    first, env = _blocked(work, tmp_path, _out(_FAIL_A + _SUMMARY.format(t="0.41s")))
    _waive(work, env, first)
    proc = _push(work, tmp_path, _out(_FAIL_A + _SUMMARY.format(t="9.99s")))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = _gate(work, proc.env, "waive", "--list").stdout
    assert _id(first) in out and "NO LONGER MATCHES" not in out, out
