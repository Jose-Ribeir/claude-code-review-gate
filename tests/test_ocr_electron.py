"""Tests for scripts/ocr_electron.py (W5) against the seeded fixtures.

Runs the checker as a subprocess against clones of the bare fixture repos
(tests/fixtures/repos/electron-ts.git and py-app.git) — the same invocation
shape the review-gate supervisor will use. The electron-ts history is
(one commit per seeded bug, oldest first):

    HEAD~5 scaffold (clean)      HEAD~4 status line (clean)
    HEAD~3 settings window       -> nodeIntegration:true + contextIsolation:false
    HEAD~2 diagnostics panel     -> orphan channel "channel-x"
    HEAD~1 diag:open handler     -> no sender validation
    HEAD    rename formatBytes   -> stale callers in panel.ts / status.ts
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ocr_electron.py"
ELECTRON_BARE = REPO_ROOT / "tests" / "fixtures" / "repos" / "electron-ts.git"
PYAPP_BARE = REPO_ROOT / "tests" / "fixtures" / "repos" / "py-app.git"


def _clean_env():
    # Kimchi injects GIT_TRACE* pointing at temp dirs that vanish; any git
    # subprocess inheriting them dies with exit 128 (see tests/conftest.py).
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE")}


def _git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, env=_clean_env())
    assert r.returncode == 0, r.stderr
    return r.stdout


def _clone(bare, tmp_path, name):
    dest = tmp_path / name
    r = subprocess.run(["git", "clone", "--no-hardlinks", str(bare), str(dest)],
                       capture_output=True, text=True, env=_clean_env())
    assert r.returncode == 0, r.stderr
    return dest


def _check(repo, a, b):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--range", "%s..%s" % (a, b), str(repo)],
        capture_output=True, text=True, env=_clean_env(), timeout=180,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    return json.loads(r.stdout)


def _module():
    spec = importlib.util.spec_from_file_location("ocr_electron", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def electron_repo(tmp_path_factory):
    return _clone(ELECTRON_BARE, tmp_path_factory.mktemp("et"), "electron-ts")


@pytest.fixture(scope="module")
def pyapp_repo(tmp_path_factory):
    return _clone(PYAPP_BARE, tmp_path_factory.mktemp("pa"), "py-app")


def _make_repo(tmp_path, name="r"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("# t\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "-c",
         "commit.gpgsign=false", "commit", "-q", "-m", "base")
    return repo


def _commit(repo):
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "-c",
         "commit.gpgsign=false", "commit", "-q", "-m", "change")


# ---------------------------------------------------------------------------
# fixtures are what the tests think they are
# ---------------------------------------------------------------------------

def test_fixture_history_shape(electron_repo, pyapp_repo):
    assert len(_git(electron_repo, "log", "--oneline", "main").splitlines()) == 6
    assert len(_git(pyapp_repo, "log", "--oneline", "main").splitlines()) == 4
    _git(pyapp_repo, "rev-parse", "--verify", "origin/clean-variant")
    _git(pyapp_repo, "rev-parse", "--verify", "v0.1.0")


# ---------------------------------------------------------------------------
# electron-ts: per-bug ranges
# ---------------------------------------------------------------------------

def test_clean_push_zero_findings(electron_repo):
    out = _check(electron_repo, "HEAD~5", "HEAD~4")
    assert out["findings"] == []
    assert out["known_defects"] == []
    assert out["changed_files"] == ["src/renderer/status.ts"]


def test_nodeintegration_flagged(electron_repo):
    out = _check(electron_repo, "HEAD~4", "HEAD~3")
    prefs = [f for f in out["findings"] if f["kind"] == "webpref-misuse"]
    assert len(prefs) == 2  # nodeIntegration:true + contextIsolation:false
    assert {f["file"] for f in prefs} == {"src/main/settings-window.ts"}
    assert all(f["severity"] == "high" for f in prefs)
    # the pre-existing safe window (contextIsolation:true) is NOT flagged
    assert not any("index.ts" in f["file"] for f in out["findings"])


def test_orphan_channel_flagged(electron_repo):
    out = _check(electron_repo, "HEAD~3", "HEAD~2")
    orphans = [f for f in out["findings"] if f["kind"] == "orphan-channel"]
    assert len(orphans) == 1
    assert orphans[0]["file"] == "src/renderer/diagnostics.ts"
    assert orphans[0]["line"] == 9
    assert orphans[0]["severity"] == "high"
    assert "channel-x" in orphans[0]["detail"]
    # the channel index shows the imbalance the finding is derived from
    assert out["channels"]["channel-x"]["main"] == []
    assert len(out["channels"]["channel-x"]["renderer"]) == 1
    assert out["channels"]["channel-x"]["renderer"][0]["api"] == "ipcRenderer.send"


def test_missing_sender_validation_flagged(electron_repo):
    out = _check(electron_repo, "HEAD~2", "HEAD~1")
    sv = [f for f in out["findings"] if f["kind"] == "missing-sender-validation"]
    assert len(sv) == 1
    assert sv[0]["file"] == "src/main/ipc.ts"
    assert sv[0]["severity"] == "medium"
    assert "diag:open" in sv[0]["detail"]
    # the pre-existing, validated app:open handler is NOT flagged
    assert not any("app:open" in f["detail"] for f in out["findings"])


def test_rename_broken_caller_pair(electron_repo):
    # The rename commit itself only touches the renamed module (v1 is
    # regex-only); the stale callers surface as findings.
    out = _check(electron_repo, "HEAD~1", "HEAD")
    assert out["changed_files"] == ["src/main/util.ts"]
    stale = [f for f in out["findings"] if f["kind"] == "stale-export-reference"]
    assert {f["file"] for f in stale} == {"src/renderer/panel.ts", "src/renderer/status.ts"}
    assert all("formatBytes" in f["detail"] for f in stale)
    # Full push: the file pair (renamed module + broken caller) is present
    # in the output — changed set plus finding targets.
    full = _check(electron_repo, "HEAD~5", "HEAD")
    touched = set(full["changed_files"]) | {f["file"] for f in full["findings"]}
    assert {"src/main/util.ts", "src/renderer/panel.ts"} <= touched


def test_full_push_all_findings_and_index(electron_repo):
    out = _check(electron_repo, "HEAD~5", "HEAD")
    kinds = {f["kind"] for f in out["findings"]}
    assert kinds == {
        "webpref-misuse", "orphan-channel",
        "missing-sender-validation", "stale-export-reference",
    }
    # channel index pairs app:open both ways; channel-x is renderer-only
    assert out["channels"]["app:open"]["main"]
    assert out["channels"]["app:open"]["renderer"]
    assert out["channels"]["channel-x"]["main"] == []
    # known_defects mirrors the findings 1:1 as gate-injectable hints
    assert len(out["known_defects"]) == len(out["findings"]) == 6
    assert all(d.startswith("webpref-misuse@") or "@" in d for d in out["known_defects"])
    # finding shape contract for the gate
    for f in out["findings"]:
        assert set(f) == {"id", "severity", "file", "line", "kind", "detail"}
        assert f["line"] > 0


def test_bad_range_reports_error(electron_repo):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--range", "HEAD~9..HEAD", str(electron_repo)],
        capture_output=True, text=True, env=_clean_env(), timeout=60,
    )
    assert r.returncode == 1
    assert "error" in json.loads(r.stdout)


# ---------------------------------------------------------------------------
# py-app: the Electron checker has nothing to say about Python
# ---------------------------------------------------------------------------

def test_pyapp_push_is_silent_for_electron_checker(pyapp_repo):
    root = _git(pyapp_repo, "rev-list", "--max-parents=0", "HEAD").strip()
    out = _check(pyapp_repo, root, "HEAD")
    assert out["findings"] == []
    assert out["channels"] == {}
    assert out["changed_files"]  # a real push, just no Electron surface


def test_pyapp_clean_variant_is_clean(pyapp_repo):
    out = _check(pyapp_repo, "v0.1.0", "origin/clean-variant")
    assert out["findings"] == []
    assert out["known_defects"] == []


# ---------------------------------------------------------------------------
# test-file allowlist
# ---------------------------------------------------------------------------

def test_is_skipped_allowlist():
    mod = _module()
    for p in [
        "tests/renderer/panel.test.ts",
        "src/__tests__/ipc.spec.ts",
        "fixtures/electron-mock.ts",
        "test/old-style.ts",
        "src/panel.test.tsx",
        "a/b/c.spec.js",
        "node_modules/electron/index.js",
    ]:
        assert mod.is_skipped(p), p
    for p in [
        "src/renderer/panel.ts",
        "src/main/ipc.ts",
        "app/db.py",
        "src/testing.ts",  # word-boundary: "testing" is not a tests/ dir
        "src/testUtils.ts",
    ]:
        assert not mod.is_skipped(p), p


def test_allowlist_prevents_self_fp(tmp_path):
    repo = _make_repo(tmp_path)
    src = repo / "src" / "renderer"
    src.mkdir(parents=True)
    (src / "x.ts").write_text(
        'import { ipcRenderer } from "electron";\n'
        'ipcRenderer.send("real-orphan");\n'
    )
    tests = repo / "tests" / "renderer"
    tests.mkdir(parents=True)
    (tests / "x.spec.ts").write_text(
        'import { ipcRenderer } from "electron";\n'
        'ipcRenderer.send("spec-orphan");\n'
    )
    _commit(repo)
    out = _check(repo, "HEAD~1", "HEAD")
    orphans = [f for f in out["findings"] if f["kind"] == "orphan-channel"]
    assert len(orphans) == 1
    assert orphans[0]["file"] == "src/renderer/x.ts"
    assert "real-orphan" in orphans[0]["detail"]
    # the spec-file channel was never indexed
    assert "spec-orphan" not in out["channels"]


def test_removed_handler_orphans_surviving_renderer_call(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "src").mkdir()
    main = repo / "src" / "main.ts"
    main.write_text(
        'import { ipcMain } from "electron";\n'
        'ipcMain.on("gone", (_event, arg: string) => { console.log(arg); });\n'
    )
    rend = repo / "src" / "rend.ts"
    rend.write_text(
        'import { ipcRenderer } from "electron";\n'
        'ipcRenderer.send("gone", "hi");\n'
    )
    _commit(repo)
    main.write_text('import { ipcMain } from "electron";\n')
    _commit(repo)
    out = _check(repo, "HEAD~1", "HEAD")
    orphans = [f for f in out["findings"] if f["kind"] == "orphan-channel"]
    assert len(orphans) == 1
    assert orphans[0]["file"] == "src/rend.ts"
    assert "lost its ipcMain handler" in orphans[0]["detail"]


def test_handler_body_and_line_helpers():
    mod = _module()
    text = 'ipcMain.on("ch", handler);\nfunction f() { return 1; }\n'
    # bare callback reference: no visible body -> None (never a finding)
    assert mod._handler_body(text, text.index('"ch"') + 3) is None
    body = 'ipcMain.on("ch", (e) => { const f = e.senderFrame; });'
    assert mod._handler_body(body, body.index('"ch"') + 3) is not None
    assert mod._line_of("ab\ncd", 3) == 2
