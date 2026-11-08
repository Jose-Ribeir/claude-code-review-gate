"""Tests for scripts/ocr_calibrate.py (C5): the calibration harness.

The stub run exercises the whole harness against the REAL gate pieces (hook
subprocess, detached supervisor, verdict store, scratch clone, installer with
redirected config dirs) with the stub reviewer, so it needs no network or
model. The matrix-definition tests pin the seeded-fixture rows against the
committed fixture repos so a fixture rewrite cannot silently move the matrix.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import ocr_calibrate as cal  # noqa: E402

_SCRATCH_ROOT = os.environ.get("OCR_CAL_TEST_ROOT", "")


def _git_ok(cwd, *args):
    return subprocess.run(["git"] + list(args), cwd=str(cwd), capture_output=True,
                          text=True, env={k: v for k, v in os.environ.items()
                                          if not k.startswith("GIT_TRACE")}
                          ).returncode == 0


def test_fn_matrix_resolves_to_real_ancestor_ranges():
    rows = cal.fn_rows()
    assert len(rows) == 6
    for row in rows:
        assert row.expected == "block"
        assert len(row.tip) == 40 and len(row.base) == 40
        remote = cal.REMOTES[row.source]
        assert _git_ok(remote, "cat-file", "-e", row.tip)
        assert _git_ok(remote, "merge-base", "--is-ancestor", row.base, row.tip), row.rid


def test_fp_fixture_matrix_resolves_and_expects_pass():
    rows = cal.fp_fixture_rows()
    assert len(rows) == 5
    assert all(r.expected == "pass" for r in rows)
    for row in rows:
        remote = cal.REMOTES[row.source]
        assert _git_ok(remote, "merge-base", "--is-ancestor", row.base, row.tip), row.rid


def _head(cwd):
    return subprocess.run(["git", "-C", str(cwd), "rev-parse", "main"],
                           capture_output=True, text=True).stdout.strip()


def test_upstream_rows_are_single_commit_ranges_newest_first():
    rows = cal.upstream_rows(4)
    assert len(rows) == 4
    # newest first: each row's tip is a descendant of the next row's tip
    for a, b in zip(rows, rows[1:]):
        assert _git_ok(cal.REMOTES[a.source], "merge-base", "--is-ancestor", b.tip, a.tip)
    for row in rows:
        assert row.expected == "pass"
        assert _git_ok(cal.REMOTES[row.source], "merge-base", "--is-ancestor",
                       row.base, row.tip), row.rid
        one = subprocess.run(["git", "-C", str(cal.REMOTES[row.source]), "rev-list",
                              "--count", row.base + ".." + row.tip],
                             capture_output=True, text=True).stdout.strip()
        assert one == "1", row.rid



def test_recorded_ids_reads_the_matrix(tmp_path):
    md = tmp_path / "cal.md"
    md.write_text(cal._MD_HEADER + "| py-app-ddbb912-48a4c0c | x |\n", encoding="utf-8")
    assert cal._recorded_ids(md) == {"py-app-ddbb912-48a4c0c"}


def test_stub_push_records_one_resumable_row(tmp_path):
    md = tmp_path / "cal.md"
    argv = ["--stub", "--root", str(tmp_path / "root"), "--md", str(md)]
    assert cal.main(argv) == 0
    text = md.read_text(encoding="utf-8")
    rows = [ln for ln in text.splitlines() if ln.startswith("| stub-py-app-")]
    assert len(rows) == 1
    assert "| pass |" in rows[0] and "| no |" in rows[0]  # verdict + not blocked
    assert "[stub worker]" in rows[0]
    # resumable: the recorded row is skipped, nothing new is appended
    assert cal.main(argv) == 0
    assert len([ln for ln in md.read_text(encoding="utf-8").splitlines()
                if ln.startswith("| stub-py-app-")]) == 1


@pytest.mark.skipif(not _SCRATCH_ROOT, reason="needs OCR_CAL_TEST_ROOT (stub e2e)")
def test_stub_push_against_prebuilt_scratch(tmp_path):
    cal.main(["--stub", "--root", _SCRATCH_ROOT, "--md", str(tmp_path / "cal.md")])
