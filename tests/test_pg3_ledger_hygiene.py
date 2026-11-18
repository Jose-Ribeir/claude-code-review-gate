"""Ledger hygiene (push-gate ticket pg3): TTL + lazy prune, delete-ref
invalidation, and audited bypass records -- all inside scripts/rg_store.py.

Every failure inside the hygiene helpers degrades to a warning, never an
exception into the gate path (the store is an audit aid, not a block).
These tests use the private OCR_STATE_DIR the conftest pins per test, so
nothing here touches the developer's real ~/.local/state/review-gate.
"""
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, _SCRIPTS)

import rg_store  # noqa: E402

DAY = 24 * 3600
FEAT = "refs/heads/feat"
OTHER = "refs/heads/other"
GITAT = "git@example.com:org/repo.git"
GITAT_FORK = "git@example.com:me/repo-fork.git"

TIP_A = "a" * 40
TIP_B = "b" * 40
TIP_C = "c" * 40


# --- helpers -------------------------------------------------------------------

def _verdicts_dir():
    return rg_store.store_root() / "verdicts"


def _record_path(remote, tip):
    return rg_store.verdict_path(remote, tip)


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_record(remote, tip, branch=FEAT, recorded_at=None, ts=None):
    """A verdict record on disk, optionally with an aged/missing recorded_at."""
    path = _record_path(remote, tip)
    assert rg_store.record_verdict(
        remote, tip, base="d" * 40, verdict="pass", blocked=False,
        reasons="", commits=[tip], branch=branch,
        coverage={"tip_tree": "e" * 40, "patch_ids": [], "sha_only": []},
    )
    data = _load(path)
    if recorded_at is None:
        data.pop("recorded_at", None)
    else:
        data["recorded_at"] = recorded_at
    if ts is not None:
        data["ts"] = ts
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _make_clone(name, age_seconds):
    d = rg_store.clones_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    old = time.time() - age_seconds
    os.utime(d, (old, old))
    return d


# --- TTL + lazy prune ------------------------------------------------------------

def test_ttl_constant_is_ninety_days():
    assert rg_store.VERDICT_TTL_SECONDS == 90 * 24 * 3600


def test_record_verdict_stamps_recorded_at():
    path = _record_path(GITAT, TIP_A)
    assert rg_store.record_verdict(GITAT, TIP_A, base="", verdict="pass",
                                   blocked=False, reasons="", commits=[TIP_A])
    rec = _load(path)
    assert isinstance(rec.get("recorded_at"), (int, float))
    assert abs(rec["recorded_at"] - time.time()) < 60


def test_prune_removes_verdicts_older_than_ttl():
    aged = _write_record(GITAT, TIP_A, recorded_at=time.time() - 91 * DAY)
    fresh = _write_record(GITAT, TIP_B, recorded_at=time.time() - 1 * DAY)
    counts = rg_store.prune()
    assert counts["verdicts_removed"] == 1
    assert not aged.exists()
    assert fresh.exists()


def test_prune_keeps_unstamped_legacy_entries_and_stamps_them():
    legacy = _write_record(GITAT, TIP_A, recorded_at=None, ts=time.time() - 400 * DAY)
    counts = rg_store.prune()
    assert counts["verdicts_removed"] == 0
    assert legacy.exists()
    assert abs(_load(legacy)["recorded_at"] - time.time()) < 60


def test_prune_removes_clones_older_than_seven_days():
    old = _make_clone("oldclone-r1", 8 * DAY)
    young = _make_clone("youngclone-r2", 1 * DAY)
    counts = rg_store.prune()
    assert counts["clones_removed"] == 1
    assert not old.exists()
    assert young.exists()


def test_maybe_prune_runs_at_most_once_per_day(monkeypatch):
    marker = rg_store.store_root() / ".last-prune"

    aged1 = _write_record(GITAT, TIP_A, recorded_at=time.time() - 100 * DAY)
    first = rg_store.maybe_prune()
    assert first is not None and first["verdicts_removed"] == 1
    assert not aged1.exists()
    assert marker.is_file()

    # second call within the day: no run at all (marker honored)
    aged2 = _write_record(GITAT, TIP_B, recorded_at=time.time() - 100 * DAY)
    second = rg_store.maybe_prune()
    assert second is None
    assert aged2.exists()

    # day elapsed: the marker is stale, prune runs again
    old = time.time() - (DAY + 60)
    os.utime(marker, (old, old))
    marker.write_text(str(old), encoding="utf-8")
    third = rg_store.maybe_prune()
    assert third is not None and third["verdicts_removed"] == 1
    assert not aged2.exists()


# --- delete-ref invalidation ------------------------------------------------------

def test_invalidate_ref_deletes_only_the_named_remote_and_ref():
    target = _write_record(GITAT, TIP_A, branch=FEAT)
    extra_tip_same_ref = _write_record(GITAT, TIP_B, branch=FEAT)
    same_remote_other_ref = _write_record(GITAT, TIP_C, branch=OTHER)
    other_remote_same_ref = _write_record(GITAT_FORK, TIP_A, branch=FEAT)

    removed = rg_store.invalidate_ref(GITAT, FEAT)
    assert removed == 2
    assert not target.exists()
    assert not extra_tip_same_ref.exists()
    assert same_remote_other_ref.exists()
    assert other_remote_same_ref.exists()


def test_invalidate_ref_on_unknown_ref_is_zero():
    _write_record(GITAT, TIP_A, branch=FEAT)
    assert rg_store.invalidate_ref(GITAT, "refs/heads/never-pushed") == 0


# --- audited bypass records ---------------------------------------------------------

def test_record_bypass_appends_valid_jsonl():
    rg_store.record_bypass(GITAT, FEAT, TIP_A, "OCR_FAIL_OPEN", "deadline pressure")
    rg_store.record_bypass(GITAT, OTHER, TIP_B, "no-verify", "hotfix")
    path = rg_store.store_root() / "bypasses.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["remote"] == GITAT and first["ref"] == FEAT
    assert first["tip"] == TIP_A
    assert first["mechanism"] == "OCR_FAIL_OPEN"
    assert first["reason"] == "deadline pressure"
    assert isinstance(first["ts"], (int, float))
    assert second["mechanism"] == "no-verify" and second["ref"] == OTHER


# --- failure degradation: warnings, never exceptions --------------------------------

def test_corrupt_verdict_record_yields_warning_not_exception(capsys):
    _write_record(GITAT, TIP_A, branch=FEAT)
    bad = _record_path(GITAT, TIP_B)
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not json", encoding="utf-8")

    counts = rg_store.prune()          # must not raise
    assert counts["verdicts_removed"] == 0
    assert bad.exists()                # never laundered away, still DENYING
    assert rg_store.invalidate_ref(GITAT, FEAT) == 1
    assert bad.exists()                # corrupt record skipped, not crashed
    err = capsys.readouterr().err
    assert "warning" in err.lower()


def test_bypass_write_failure_warns_instead_of_raising(capsys):
    # a directory where a file must be: appending bypasses.jsonl fails
    bad = rg_store.store_root() / "bypasses.jsonl"
    bad.mkdir()
    rg_store.record_bypass(GITAT, FEAT, TIP_A, "no-verify", "y")  # no raise
    err = capsys.readouterr().err
    assert "warning" in err.lower() and "bypass" in err.lower()
    bad.rmdir()
