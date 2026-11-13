"""Wave-2 review-findings regression tests (the 7465d89 BLOCK wave).

Each test names the reviewed gate behavior it pins:
  * _advisory_says_nonblocking / _is_advisory must swallow json.loads'
    RecursionError on pathologically nested .ocr/config.json (the docstring
    promises advisory lookups never break the gate).
  * _park_pending publishes via a `.tmp-<pid>-<name>` file created 0o600 from
    the start (no create-to-chmod window) and _pending_entries never sweeps a
    `.tmp-*` zombie as a live parked note.
  * _diff_exceeds_caps measures the MODIFIED-file branch by streaming the
    exact blob diff, never by summing blob sizes, so a big-blob-small-diff
    file is not flagged truncated.
"""
import importlib.util
import json
import os
import stat
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SCRIPTS = os.path.join(_ROOT, "scripts")
sys.path.insert(0, _SCRIPTS)

_spec = importlib.util.spec_from_file_location("review_gate",
                                               os.path.join(_SCRIPTS, "review-gate.py"))
rg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rg)

_GIT = os.environ.get("GIT", "git")


def _git_ok():
    try:
        subprocess.run([_GIT, "--version"], capture_output=True, timeout=15)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


needs_git = pytest.mark.skipif(not _git_ok(), reason="git not available")


# --- RecursionError: pathological .ocr/config.json -----------------------------

def _deep_json(levels=1500):
    # 1500 nested objects: past json.loads' recursion ceiling (~1000), which
    # raises RecursionError -- NOT the (ValueError, TypeError) the net used to
    # catch. Must still be a silent "no advisory evidence", never a crash.
    return '{"blocking": false, "a":' * levels + "1" + "}" * levels


def test_advisory_parse_survives_pathological_nesting():
    txt = _deep_json()
    silent = rg._advisory_says_nonblocking(txt)
    assert silent in (True, False)  # the point: it returned, not raised


def test_is_advisory_never_raises_on_pathological_config(tmp_path, monkeypatch):
    monkeypatch.delenv("OCR_ADVISORY", raising=False)
    repo = tmp_path / "repo"
    (repo / ".ocr").mkdir(parents=True)
    (repo / ".ocr" / "config.json").write_text(_deep_json(), encoding="utf-8")
    result = rg._is_advisory(str(repo))
    assert result in (True, False)


def test_is_advisory_still_honors_a_legit_nonblocking_config(tmp_path, monkeypatch):
    # The pathological-input guard must not eat the real semantics: a genuine
    # {"blocking": false} config still downgrades the gate, and nested-garbage
    # configs still FAIL to (only strict evidence counts, ADR 0003b).
    monkeypatch.delenv("OCR_ADVISORY", raising=False)
    repo = tmp_path / "repo"
    (repo / ".ocr").mkdir(parents=True)
    (repo / ".ocr" / "config.json").write_text(json.dumps({"blocking": False}))
    assert rg._is_advisory(str(repo)) is True
    (repo / ".ocr" / "config.json").write_text("# blocking must stay false nearby\n")
    assert rg._is_advisory(str(repo)) is False


# --- parked notes: tmp naming + 0o600-from-creation ------------------------------

def _data_dir():
    data = rg._gate_data_dir()
    return data


def test_park_pending_tmp_names_never_match_pending_glob(tmp_path):
    # _pending_entries globs PENDING_PREFIX*; the publish tmp must NOT match,
    # or a kill between write and os.replace leaves a valid-JSON zombie that
    # reads as a live parked note (wave-2 finding).
    rg._park_pending("sess", "/repo", "head-sha")
    data = _data_dir()
    names = [p.name for p in data.iterdir()]
    assert not [n for n in names if n.startswith(".tmp-")]
    notes = [n for n in names if n.startswith(rg.PENDING_PREFIX)]
    assert len(notes) == 1
    # A leftover tmp (simulated kill before replace) must never be returned.
    zombie = data / f".tmp-{os.getpid()}-{notes[0]}"
    zombie.write_text(json.dumps({"session": "z", "repo": "/r", "head": "h",
                                  "kind": "review"}))
    entries = rg._pending_entries()
    assert len(entries) == 1
    for path, _info in entries:
        assert not path.name.startswith(".tmp-")
        assert path.name.startswith(rg.PENDING_PREFIX)


def test_park_pending_creates_note_0600_even_under_umask_022(monkeypatch):
    old_umask = os.umask(0o022)
    try:
        rg._park_pending("sess", "/repo", "head-sha-2")
    finally:
        os.umask(old_umask)
    data = _data_dir()
    notes = [p for p in data.iterdir() if p.name.startswith(rg.PENDING_PREFIX)]
    assert notes
    for p in notes:
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


# --- _diff_exceeds_caps: streamed exact diff on the modified branch -----------

def _mk_blob_repo(tmp_path):
    repo = tmp_path / "blobrepo"
    repo.mkdir()
    subprocess.run([_GIT, "init", "-q", "-b", "main", str(repo)], check=True,
                   capture_output=True)
    big = ("lorem ipsum dolor sit amet\n" * 1100)  # ~30 KiB blob
    (repo / "f.txt").write_text(big)
    oid_old = subprocess.run([_GIT, "hash-object", "-w", "f.txt"], cwd=str(repo),
                             capture_output=True, text=True, check=True).stdout.strip()
    (repo / "f.txt").write_text(big + "one small added line\n")
    oid_new = subprocess.run([_GIT, "hash-object", "-w", "f.txt"], cwd=str(repo),
                             capture_output=True, text=True, check=True).stdout.strip()
    return repo, oid_old, oid_new


@needs_git
def test_big_blob_small_diff_modified_file_is_not_flagged(tmp_path, monkeypatch):
    """Two ~30 KiB blobs (sum >> _TRUNC_MAX_BYTES) whose real diff is a few
    lines must NOT exceed caps: the modified branch streams the exact diff
    via subprocess_run_popen and never calls `git cat-file -s` blob sums."""
    repo, oid_old, oid_new = _mk_blob_repo(tmp_path)
    assert os.path.getsize(repo / "f.txt") > rg._TRUNC_MAX_BYTES  # each blob beats the cap
    git_calls = []
    real_git = rg._git

    def spy_git(args, cwd=""):
        git_calls.append(list(args))
        return real_git(args, cwd=cwd)

    popen_calls = []
    real_popen = rg.subprocess_run_popen

    def spy_popen(argv, cwd=""):
        popen_calls.append(list(argv))
        return real_popen(argv, cwd=cwd)

    monkeypatch.setattr(rg, "_git", spy_git)
    monkeypatch.setattr(rg, "subprocess_run_popen", spy_popen)

    item = {"entry": {"path": "f.txt", "old_oid": oid_old,
                      "new_oid": oid_new, "lines": 3}}
    assert rg._diff_exceeds_caps(item, review_root=str(repo)) is False
    assert not [a for a in git_calls if a[:2] == ["cat-file", "-s"]], git_calls
    assert ["git", "diff", "--no-ext-diff", oid_old, oid_new] in popen_calls
