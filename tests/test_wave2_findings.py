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


# --- wave-3: _streamed_diff_bytes failure/timeout parity with _git -----------

def test_streamed_diff_bytes_times_out_on_a_hung_diff(monkeypatch):
    import threading as _th
    import time as _time

    class _Hung:
        def __init__(self):
            self.killed = _th.Event()
            self.stdout = self
            self.returncode = None

        def read(self, _n):
            self.killed.wait(30)
            return b""

        def close(self):
            pass

        def kill(self):
            self.killed.set()

        def wait(self):
            self.returncode = -9

    proc = _Hung()
    monkeypatch.setattr(rg, "subprocess_run_popen", lambda *a, **k: proc)
    monkeypatch.setattr(rg, "_STREAMED_DIFF_TIMEOUT_S", 1)
    started = _time.monotonic()
    assert rg._streamed_diff_bytes("a" * 40, "b" * 40, "/x") == 0
    assert _time.monotonic() - started < 20
    assert proc.killed.is_set()


def test_streamed_diff_bytes_returns_zero_on_a_midstream_oserror(monkeypatch):
    class _Broken:
        def __init__(self):
            self.stdout = self
            self.returncode = 0
            self._n = 0

        def read(self, _n):
            self._n += 1
            if self._n == 1:
                return b"x" * 100
            raise OSError("broken pipe")

        def close(self):
            pass

        def kill(self):
            pass

        def wait(self):
            self.returncode = 0

    monkeypatch.setattr(rg, "subprocess_run_popen", lambda *a, **k: _Broken())
    assert rg._streamed_diff_bytes("a" * 40, "b" * 40, "/x") == 0


def test_subprocess_run_popen_forwards_create_no_window(monkeypatch):
    captured = {}

    class _P:
        stdout = None

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return _P()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    rg.subprocess_run_popen(["git", "diff"], cwd="/tmp")
    assert captured.get("creationflags") == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert captured.get("stderr") == subprocess.DEVNULL


# --- wave-3: parked-note tmp zombies (sweep + single retry) ------------------

def test_atomic_write_json_note_retries_past_a_same_name_zombie(tmp_path):
    zombie = tmp_path / (f".tmp-{os.getpid()}-note-x")
    zombie.write_text("zombie", encoding="utf-8")
    rg._atomic_write_json_note(tmp_path, "note-x", '{"a": 1}')
    assert json.loads((tmp_path / "note-x").read_text(encoding="utf-8")) == {"a": 1}
    assert not zombie.exists()  # the retry unlinked it


def test_pending_entries_sweeps_stale_tmp_zombies(tmp_path, monkeypatch):
    import time as _time
    monkeypatch.setattr(rg, "_gate_data_dir", lambda: tmp_path)
    stale = tmp_path / (f".tmp-{os.getpid()}-{rg.PENDING_PREFIX}dead")
    stale.write_text("{}", encoding="utf-8")
    old = _time.time() - rg.MARKER_TTL - 60
    os.utime(stale, (old, old))
    fresh = tmp_path / ".tmp-999999-live"
    fresh.write_text("x", encoding="utf-8")
    rg._pending_entries()
    assert not stale.exists(), "stale .tmp-* zombie must be swept"
    assert fresh.exists(), "a fresh in-flight tmp must NOT be swept"


def test_park_pending_survives_a_same_name_tmp_zombie(tmp_path, monkeypatch):
    monkeypatch.setattr(rg, "_gate_data_dir", lambda: tmp_path)
    name = rg.PENDING_PREFIX + rg._marker_digest("s1", "/repo", "h" * 40, "review")
    (tmp_path / (f".tmp-{os.getpid()}-{name}")).write_text("zombie", encoding="utf-8")
    rg._park_pending("s1", "/repo", "h" * 40)
    entries = rg._pending_entries()
    assert len(entries) == 1  # the note was still delivered, exactly once
    assert entries[0][1]["head"] == "h" * 40
    assert not (tmp_path / (f".tmp-{os.getpid()}-{name}")).exists()


# --- wave-3: _async_dir store-unavailable warning fires once per process -----

def test_async_dir_store_warning_fires_once_per_process(monkeypatch, capsys, tmp_path):
    def _down(common_dir):
        raise RuntimeError("store down")

    monkeypatch.setattr(rg.rg_store, "async_dir", _down)
    rg._WARNED_ONCE.discard("external-store")
    d1 = rg._async_dir(str(tmp_path / "c1"))
    d2 = rg._async_dir(str(tmp_path / "c2"))
    assert d1 == tmp_path / "c1" / rg.ASYNC_DIR
    assert d2 == tmp_path / "c2" / rg.ASYNC_DIR
    err = capsys.readouterr().err
    assert err.count("external state store") == 1, err


# --- wave-3: _state_path glob/stat race hardening ----------------------------

def test_state_path_skips_a_candidate_vanishing_between_glob_and_stat(monkeypatch, tmp_path):
    from pathlib import Path as _Path

    tip = "f" * 40
    a = tmp_path / f"aaaa-{tip}.json"
    b = tmp_path / f"bbbb-{tip}.json"
    a.write_text("{}", encoding="utf-8")
    b.write_text("{}", encoding="utf-8")
    os.utime(a, (1000, 1000))  # older
    os.utime(b, (2000, 2000))  # newest: would win without the race
    monkeypatch.setattr(rg, "_async_dir", lambda common_dir: tmp_path)
    real_stat = _Path.stat

    def flaky(self, *args, **kwargs):
        if self == b:
            raise OSError("vanished between glob and stat")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(_Path, "stat", flaky)
    # The vanished newest candidate is skipped; the survivor is still
    # selected by the (mtime, str(path)) ordering instead of degrading to
    # the default path.
    assert rg._state_path("any", tip, base=None) == a


def test_state_path_tie_breaks_on_mtime_then_path(monkeypatch, tmp_path):
    tip = "e" * 40
    a = tmp_path / f"aaaa-{tip}.json"
    b = tmp_path / f"bbbb-{tip}.json"
    a.write_text("{}", encoding="utf-8")
    b.write_text("{}", encoding="utf-8")
    os.utime(a, (1500, 1500))
    os.utime(b, (1500, 1500))  # same mtime: str(path) decides, newest last
    monkeypatch.setattr(rg, "_async_dir", lambda common_dir: tmp_path)
    assert rg._state_path("any", tip, base=None) == b


# --- wave-3: _arch_flags reuses an already-fetched entry list ----------------

def test_arch_flags_with_entries_never_reenumerates_the_diff(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("_collect_diff_entries must not run when entries are given")

    monkeypatch.setattr(rg, "_collect_diff_entries", _boom)
    entries = [{"path": "package.json", "status": "M", "old_path": None}]
    # _added_lines_by_path still needs git, which fails softly against this
    # non-repo root -- the point here is only that the enumeration is NOT
    # repeated.
    out = rg._arch_flags("/nonexistent", "", "t" * 40, entries=entries)
    assert out is None or isinstance(out, dict)
    assert rg._arch_flags("/nonexistent", "", "t" * 40, entries=[]) is None
