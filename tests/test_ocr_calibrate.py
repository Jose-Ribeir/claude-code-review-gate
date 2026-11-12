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


def _mk_git_repo(path, commits):
    """A scratch git checkout standing in for the upstream review source."""
    path.mkdir(parents=True, exist_ok=True)

    def g(*args):
        subprocess.run(["git"] + list(args), cwd=str(path), check=True,
                       capture_output=True,
                       env={k: v for k, v in os.environ.items()
                            if not k.startswith("GIT_TRACE")})
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "t")
    g("config", "commit.gpgsign", "false")
    for i in range(commits):
        (path / f"f{i}.txt").write_text(f"{i}\n")
        g("add", ".")
        g("commit", "-q", "-m", f"commit {i}")
    return path


@pytest.fixture
def _restore_upstream(monkeypatch):
    """_retarget_upstream retargets module globals; snapshot + restore them
    around each test."""
    monkeypatch.setattr(cal, "UPSTREAM", cal.UPSTREAM)
    monkeypatch.setitem(cal.REMOTES, "upstream", cal.REMOTES["upstream"])


def test_missing_upstream_fails_with_a_clean_ap_error(tmp_path, monkeypatch,
                                                      capsys, _restore_upstream):
    """No OCR_CALIB_UPSTREAM and the fallback path does not exist: the run
    must die BEFORE rows are built with argparse's error (SystemExit 2) and a
    message naming the env var -- never a raw RuntimeError from git."""
    monkeypatch.delenv("OCR_CALIB_UPSTREAM", raising=False)
    monkeypatch.setattr(cal, "UPSTREAM_FALLBACK", tmp_path / "no-such-checkout")
    with pytest.raises(SystemExit) as exc:
        cal.main(["--fp-upstream", "3", "--plan", "--md", str(tmp_path / "m.md")])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "OCR_CALIB_UPSTREAM" in err
    assert "no-such-checkout" in err


def test_upstream_that_is_not_a_git_repo_fails_cleanly(tmp_path, monkeypatch,
                                                       capsys, _restore_upstream):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("OCR_CALIB_UPSTREAM", str(plain))
    with pytest.raises(SystemExit) as exc:
        cal.main(["--fp-upstream", "2", "--plan", "--md", str(tmp_path / "m.md")])
    assert exc.value.code == 2
    assert "not a git repository" in capsys.readouterr().err


def test_upstream_env_var_points_at_a_temp_repo(tmp_path, monkeypatch,
                                                capsys, _restore_upstream):
    """Happy path: OCR_CALIB_UPSTREAM names a real checkout; its ranges plan
    out and REMOTES is retargeted at the resolved path."""
    repo = _mk_git_repo(tmp_path / "upstream-checkout", commits=4)
    monkeypatch.setenv("OCR_CALIB_UPSTREAM", str(repo))
    rc = cal.main(["--fp-upstream", "2", "--plan", "--md", str(tmp_path / "m.md")])
    assert rc == 0
    todo = [ln for ln in capsys.readouterr().out.splitlines()
            if ln.split()[0] == "TODO" and ln.split()[1].startswith("upstream-")]
    assert len(todo) == 2
    assert cal.REMOTES["upstream"] == repo
    assert cal.UPSTREAM == repo
