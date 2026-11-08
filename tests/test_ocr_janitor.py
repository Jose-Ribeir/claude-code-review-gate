"""Tests for scripts/ocr_janitor.py (C9): the store's GC.

Seeds a simulated 10-run accumulation (clones, state entries, pending
markers, an oversized findings log, verdict records) and asserts the
janitor reclaims it -- while honoring the invariants that make it safe:
live-verdict preservation, "if unsure, keep", and no auto-clearing of
sticky-error states.
"""
import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ocr_janitor.py"

_spec = importlib.util.spec_from_file_location("ocr_janitor", SCRIPT)
ocr_janitor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ocr_janitor)

NOW = time.time() - 40 * 86400  # the janitor clock: everything seeded is 40 days old
OLD = NOW - 40 * 86400          # verdict timestamps old enough to prune
DAY = 86400.0


def _git_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE")}


def _make_repo(tmp_path, name="live"):
    repo = tmp_path / name
    repo.mkdir()
    env = _git_env()
    r = subprocess.run(["git", "-C", str(repo), "init", "-q"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    for cmd in (["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo)] + cmd, capture_output=True, env=env)
    (repo / "f.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "one"],
                   capture_output=True, env=env)
    tip = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True, env=env).stdout.strip()
    return repo, tip


class Store:
    """A seeded external store: the layout rg_store.py creates."""

    def __init__(self, tmp_path):
        self.root = tmp_path / "store"
        self.clones = self.root / "clones"
        self.clones.mkdir(parents=True)
        (self.root / "data").mkdir(parents=True)
        (self.root / "verdicts").mkdir(parents=True)
        self.key = self.root / "async" / "0123456789abcdef"
        self.key.mkdir(parents=True)

    def state(self, run_id, **fields):
        tip = str(fields.get("tip") or "b" * 40)
        p = self.key / f"{str(fields.get('base', 'aaaa'))[:16]}-{tip}.json"
        p.write_text(json.dumps(dict(run_id=run_id, **fields)), encoding="utf-8")
        return p

    def clone(self, tip, run_id):
        d = self.clones / f"{str(tip)[:12]}-{run_id}"
        d.mkdir()
        (d / ".git").mkdir()
        return d

    def pending(self, repo, head="deadbeef"):
        p = self.root / "data" / f"pending-{head}"
        p.write_text(json.dumps({"session": "s", "repo": str(repo),
                                 "head": head, "kind": "review"}), encoding="utf-8")
        return p

    def pushed_repo(self, repo, digest="cafe"):
        p = self.root / "data" / f"pushed-repo-{digest}"
        p.write_text(str(repo), encoding="utf-8")
        return p

    def verdict(self, tip, ts, remote="r", **extra):
        rec = {"remote": remote, "tip": str(tip), "base": "", "verdict": "pass",
               "blocked": False, "reasons": "", "commits": [str(tip)],
               "capped": False, "note": "", "ts": ts,
               "protocol_version": 1}
        rec.update(extra)
        p = self.root / "verdicts" / f"abc-{str(tip)[:8]}-{len(list(self.root.glob('verdicts/*.json')))}.json"
        p.write_text(json.dumps(rec), encoding="utf-8")
        return p


def _janitor(store, days=7, verdict_days=30, log_bytes=10 * 1024 * 1024,
             dry=False, now=NOW):
    env = dict(_git_env())
    env["OCR_STATE_DIR"] = str(store.root)
    return ocr_janitor.janitor(env=env, days=days, verdict_days=verdict_days,
                               log_bytes=log_bytes, dry=dry, now=now)


# --- (a) clones -----------------------------------------------------------------

def test_ten_run_accumulation_reclaimed(tmp_path):
    store = Store(tmp_path)
    for i in range(10):
        store.state(f"run{i:02d}", tip=f"{i:040x}", state="done",
                    done_ts=NOW - 20 * DAY, repo_root=str(tmp_path),
                    git_dir=str(tmp_path))
        store.clone(f"{i:040x}", f"run{i:02d}")
    lines, summary = _janitor(store)
    assert summary["clone"] == 10
    assert not list(store.clones.iterdir())


def test_orphan_clone_removed_when_state_entry_gone(tmp_path):
    store = Store(tmp_path)
    store.clone("a" * 40, "runZZ")  # no state entry anywhere
    lines, _ = _janitor(store)
    assert not (store.clones / f"{'a' * 12}-runZZ").exists()


def test_running_and_recent_clones_kept(tmp_path):
    store = Store(tmp_path)
    store.state("runR", tip="b" * 40, state="running", repo_root=str(tmp_path))
    running = store.clone("b" * 40, "runR")
    store.state("runF", tip="c" * 40, state="done", done_ts=NOW - DAY)  # fresh
    fresh = store.clone("c" * 40, "runF")
    _janitor(store)
    assert running.is_dir() and fresh.is_dir()


# --- (b) markers ------------------------------------------------------------------

def test_markers_for_gone_repos_removed_live_kept(tmp_path):
    store = Store(tmp_path)
    live_repo, _tip = _make_repo(tmp_path)
    gone = store.pending(tmp_path / "vanished", head="gone1")
    gone_pushed = store.pushed_repo(tmp_path / "vanished2")
    keep = store.pending(live_repo, head="keep1")
    keep_pushed = store.pushed_repo(live_repo, digest="beef")
    lines, summary = _janitor(store)
    assert summary["marker"] == 2
    assert not gone.exists() and not gone_pushed.exists()
    assert keep.exists() and keep_pushed.exists()


def test_unreadable_pending_marker_removed(tmp_path):
    store = Store(tmp_path)
    bad = store.root / "data" / "pending-badbad"
    bad.write_text("{not json", encoding="utf-8")
    _janitor(store)
    assert not bad.exists()


# --- (c) findings-log rotation ------------------------------------------------------

def test_findings_log_rotated_to_whole_lines(tmp_path):
    store = Store(tmp_path)
    git_dir = tmp_path / "gitdir"
    git_dir.mkdir()
    store.state("runL", tip="d" * 40, state="done", done_ts=NOW,
                repo_root=str(tmp_path), git_dir=str(git_dir))
    log = git_dir / "review-gate-findings.jsonl"

    lines_out = [json.dumps({"n": i, "pad": "x" * 500}) + "\n" for i in range(40)]
    log.write_text("".join(lines_out), encoding="utf-8")
    full = log.read_bytes()
    lines, summary = _janitor(store, log_bytes=4000)
    assert summary["log"] == 1
    trimmed = log.read_bytes()
    assert len(trimmed) <= 4000 < len(full)
    assert trimmed.endswith(b"\n")
    kept = [json.loads(ln) for ln in trimmed.decode().splitlines()]
    assert kept == [json.loads(ln) for ln in lines_out[-len(kept):]]  # newest kept
    assert kept[0]["n"] > 0  # cut at a line boundary, not mid-line


def test_small_findings_log_untouched(tmp_path):
    store = Store(tmp_path)
    git_dir = tmp_path / "gitdir"
    git_dir.mkdir()
    store.state("runS", tip="e" * 40, state="done", done_ts=NOW,
                repo_root=str(tmp_path), git_dir=str(git_dir))
    log = git_dir / "review-gate-findings.jsonl"
    log.write_text('{"a": 1}\n', encoding="utf-8")
    lines, summary = _janitor(store, log_bytes=10 * 1024 * 1024)
    assert summary["log"] == 0
    assert log.read_text(encoding="utf-8") == '{"a": 1}\n'


# --- (d) verdicts --------------------------------------------------------------------

def test_old_verdict_for_gone_repo_pruned(tmp_path):
    store = Store(tmp_path)
    store.state("runV", tip="1" * 40, state="done", done_ts=NOW,
                repo_root=str(tmp_path / "gone-repo"))
    v = store.verdict("1" * 40, OLD)
    lines, _ = _janitor(store)
    assert not v.exists()
    assert any("pruned verdict" in ln for ln in lines)


def test_live_verdict_preserved(tmp_path):
    store = Store(tmp_path)
    repo, tip = _make_repo(tmp_path)
    store.state("runW", tip=tip, state="done", done_ts=NOW,
                repo_root=str(repo), git_dir=str(repo / ".git"))
    v = store.verdict(tip, OLD)
    lines, _ = _janitor(store)
    assert v.exists()
    assert any("kept verdict (live)" in ln for ln in lines)


def test_unreachable_tip_in_live_repo_pruned(tmp_path):
    # A commit on a side branch that is then deleted: the repo is alive but
    # no ref contains the tip any more.
    store = Store(tmp_path)
    repo, tip = _make_repo(tmp_path)
    env = _git_env()
    main = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"],
                          capture_output=True, text=True, env=env).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "side"],
                   capture_output=True, env=env)
    (repo / "g.txt").write_text("two\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "two"],
                   capture_output=True, env=env)
    side_tip = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, env=env).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", main],
                   capture_output=True, env=env)
    subprocess.run(["git", "-C", str(repo), "branch", "-qD", "side"],
                   capture_output=True, env=env)
    store.state("runX", tip=side_tip, state="done", done_ts=NOW,
                repo_root=str(repo), git_dir=str(repo / ".git"))
    v = store.verdict(side_tip, OLD)
    lines, _ = _janitor(store)
    assert not v.exists()


def test_verdict_with_no_recorded_repo_kept_if_unsure(tmp_path):
    store = Store(tmp_path)
    v = store.verdict("2" * 40, OLD)  # no state entry records this tip
    lines, _ = _janitor(store)
    assert v.exists()
    assert any("kept verdict (unsure)" in ln for ln in lines)


def test_young_verdict_never_pruned(tmp_path):
    store = Store(tmp_path)
    store.state("runY", tip="3" * 40, state="done", done_ts=NOW,
                repo_root=str(tmp_path / "gone"))
    v = store.verdict("3" * 40, NOW)  # young despite the old state
    lines, _ = _janitor(store, verdict_days=30)
    assert v.exists()


# --- (e) sticky errors ------------------------------------------------------------

def test_sticky_error_reported_not_cleared(tmp_path):
    store = Store(tmp_path)
    p = store.state("runE", tip="4" * 40, state="failed", attempts=3,
                    reason="spawn", failed_ts=NOW - 10 * DAY)
    lines, summary = _janitor(store)
    assert summary["sticky"] == 1
    assert any("sticky error state" in ln and str(p) in ln for ln in lines)
    assert p.exists()  # never auto-cleared
    assert "OCR_FORCE_REVIEW" in "".join(lines)


def test_sticky_error_younger_than_days_not_reported(tmp_path):
    store = Store(tmp_path)
    store.state("runE2", tip="5" * 40, state="failed", attempts=3,
                reason="spawn", failed_ts=NOW - DAY)
    lines, summary = _janitor(store)
    assert summary["sticky"] == 0


# --- dry run -----------------------------------------------------------------------

def test_dry_run_touches_nothing(tmp_path):
    store = Store(tmp_path)
    for i in range(3):
        store.state(f"dr{i}", tip=f"{i:040x}", state="done",
                    done_ts=NOW - 20 * DAY, repo_root=str(tmp_path / "gone"),
                    git_dir=str(tmp_path))
        store.clone(f"{i:040x}", f"dr{i}")
    marker = store.pending(tmp_path / "vanished", head="dry1")
    v = store.verdict("6" * 40, OLD)
    lines, summary = _janitor(store, dry=True)
    assert summary["clone"] == 3 and summary["marker"] >= 1
    assert all(ln.startswith("[dry-run] ") or ln.startswith("janitor:") for ln in lines
               if "clone" in ln or "marker" in ln)
    assert len(list(store.clones.iterdir())) == 3
    assert marker.exists() and v.exists()


# --- CLI ---------------------------------------------------------------------------

def test_cli_runs_and_reports(tmp_path):
    store = Store(tmp_path)
    store.clone("f" * 40, "cliRun")
    env = _git_env()
    env["OCR_STATE_DIR"] = str(store.root)
    r = subprocess.run([os.fspath(__import__("sys").executable), str(SCRIPT)],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "janitor:" in r.stdout
    assert not (store.clones / f"{'f' * 12}-cliRun").exists()
