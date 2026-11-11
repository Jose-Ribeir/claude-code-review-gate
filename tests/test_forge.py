"""Tests for scripts/ocr_forge.py -- pointer resolution + forge lookup (t09).

The resolver answers one question per prose-pointer target: where does this
artifact come from? Tiers, in order (spec: "Pointer resolution and forge
lookup"):

  1. committed registry .ocr/artifacts.json ({name: {produced_by, path, mr?}});
  2. repo-wide grep for the target name;
  3. sibling branch tips (bounded);
  4. live forge call (glab or gh by remote, hard 3s timeout, cached negatives).

Resolution dict contract: {"status": "resolved"|"unresolved"|"unavailable",
"via": "registry"|"grep"|"branch"|"forge"|"none", "warning": ...?} -- a
resolved pointer names where it resolved. Offline/unauthenticated/missing CLI
degrades to tiers 1-3 with a warning. A pointer that resolves by no tier and
appears in no registry is carried as ``unresolved`` in the bundle for the
reviewer to cite as a medium finding -- this module never fabricates findings.

Fixtures are real tiny git repos in tmp_path plus fake CLI executables (shell
scripts) so the timeout test exercises a REAL subprocess timeout.
"""
import json
import os
import subprocess
import sys
import time

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)

import ocr_forge  # noqa: E402

TARGET = "out/weekly-2026-11.md"


def _git_raw(args, cwd):
    result = subprocess.run(
        ["git"] + args, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    return result.stdout, result.returncode


def _init_repo(tmp_path):
    cwd = str(tmp_path)
    for cmd in (["init"], ["config", "user.email", "test@test.com"],
                ["config", "user.name", "Tester"], ["config", "commit.gpgsign", "false"]):
        _git_raw(cmd, cwd)
    return cwd


def _commit(cwd, files, message="test commit"):
    for relpath, content in files.items():
        full = os.path.join(cwd, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
    _git_raw(["add", "-A"], cwd)
    out, rc = _git_raw(["commit", "-m", message], cwd)
    assert rc == 0, out


def _current_branch(cwd):
    out, rc = _git_raw(["branch", "--show-current"], cwd)
    assert rc == 0
    return out.strip()


def _resolve(tmp_path, target=TARGET, **kwargs):
    return ocr_forge.make_pointer_resolver(str(tmp_path), **kwargs)(target)


# ===========================================================================
# Tier 1: committed registry .ocr/artifacts.json
# ===========================================================================

class TestRegistryTier:
    def test_registry_hit_resolves_with_producer(self, tmp_path):
        cwd = _init_repo(tmp_path)
        _commit(cwd, {
            ".ocr/artifacts.json": json.dumps({
                "weekly-2026-11.md": {"produced_by": "app/reports.py",
                                      "path": "out/weekly-2026-11.md",
                                      "mr": "!42"},
            }),
        })
        res = _resolve(tmp_path)
        assert res["status"] == "resolved"
        assert res["via"] == "registry"
        assert res["produced_by"] == "app/reports.py"
        assert res["path"] == "out/weekly-2026-11.md"
        assert res["mr"] == "!42"

    def test_registry_entry_without_mr(self, tmp_path):
        """The mr field is optional."""
        cwd = _init_repo(tmp_path)
        _commit(cwd, {".ocr/artifacts.json": json.dumps({
            "weekly-2026-11.md": {"produced_by": "gen.py",
                                  "path": "out/weekly-2026-11.md"}})})
        res = _resolve(tmp_path)
        assert res["status"] == "resolved"
        assert res["via"] == "registry"
        assert "mr" not in res

    def test_malformed_registry_degrades_with_warning(self, tmp_path):
        """A broken registry is a warning, never an error; later tiers still run."""
        cwd = _init_repo(tmp_path)
        _commit(cwd, {".ocr/artifacts.json": "{ not json"})
        res = _resolve(tmp_path)
        assert res["status"] != "resolved"
        assert res.get("warning")
        assert "registry" in res["warning"]

    def test_registry_must_not_match_unrelated_names(self, tmp_path):
        cwd = _init_repo(tmp_path)
        _commit(cwd, {".ocr/artifacts.json": json.dumps({
            "quarterly-2030-01.md": {"produced_by": "gen.py",
                                     "path": "out/quarterly-2030-01.md"}})})
        res = _resolve(tmp_path)
        assert res.get("via") != "registry"


# ===========================================================================
# Tier 2: repo-wide grep for the target name
# ===========================================================================

class TestGrepTier:
    def test_producer_file_found_by_grep(self, tmp_path):
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"app/gen_reports.py": 'TARGET = "weekly-2026-11.md"\n'})
        res = _resolve(tmp_path)
        assert res["status"] == "resolved"
        assert res["via"] == "grep"
        hit_paths = [h["path"] for h in res["hits"]]
        assert "app/gen_reports.py" in hit_paths

    def test_registry_beats_grep(self, tmp_path):
        """Tier order is registry first, even when grep would also hit."""
        cwd = _init_repo(tmp_path)
        _commit(cwd, {
            ".ocr/artifacts.json": json.dumps({
                "weekly-2026-11.md": {"produced_by": "app/gen_reports.py",
                                      "path": "out/weekly-2026-11.md"}}),
            "app/gen_reports.py": 'TARGET = "weekly-2026-11.md"\n',
        })
        res = _resolve(tmp_path)
        assert res["via"] == "registry"

    def test_grep_hits_are_bounded(self, tmp_path):
        """A name appearing in many files never floods the resolution."""
        cwd = _init_repo(tmp_path)
        files = {"dir/f%02d.txt" % i: "weekly-2026-11.md\n" for i in range(40)}
        _commit(cwd, files)
        res = _resolve(tmp_path)
        assert res["status"] == "resolved"
        assert res["via"] == "grep"
        assert len(res["hits"]) <= ocr_forge.MAX_GREP_HITS

    def test_untracked_file_matches_grep(self, tmp_path):
        """git grep searches the working tree: an uncommitted producer resolves."""
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"app.py": "x = 1\n"})
        with open(os.path.join(cwd, "notes.md"), "w", encoding="utf-8") as fh:
            fh.write("produces weekly-2026-11.md every Friday\n")
        _git_raw(["add", "notes.md"], cwd)
        res = _resolve(tmp_path)
        assert res["status"] == "resolved"
        assert res["via"] == "grep"


# ===========================================================================
# Tier 3: sibling branch tips (bounded)
# ===========================================================================

class TestBranchTier:
    def test_sibling_branch_tip_resolves(self, tmp_path):
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"app.py": "x = 1\n"})
        main = _current_branch(cwd)
        _git_raw(["checkout", "-b", "feature-reports"], cwd)
        _commit(cwd, {"app/reports.py": 'REPORT = "weekly-2026-11.md"\n'},
                "add report producer")
        _git_raw(["checkout", main], cwd)
        res = _resolve(tmp_path)
        assert res["status"] == "resolved"
        assert res["via"] == "branch"
        assert "feature-reports" in res["branches"]

    def test_grep_beats_branch(self, tmp_path):
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"Makefile": "weekly-2026-11.md: app.py\n"})
        res = _resolve(tmp_path)
        assert res["via"] == "grep"

    def test_branch_search_is_bounded(self, tmp_path):
        """Many sibling branches cannot stall resolution: the walk is capped."""
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"app.py": "x = 1\n"})
        main = _current_branch(cwd)
        for i in range(ocr_forge.MAX_BRANCHES + 6):
            _git_raw(["branch", "padding-%02d" % i], cwd)
        _git_raw(["checkout", "-b", "feature-reports"], cwd)
        _commit(cwd, {"app/reports.py": 'REPORT = "weekly-2026-11.md"\n'},
                "add report producer")
        _git_raw(["checkout", main], cwd)
        res = _resolve(tmp_path)
        assert res["status"] != "unavailable"  # the walk completed
        if res["status"] == "resolved":
            assert len(res["branches"]) <= ocr_forge.MAX_BRANCHES


import shutil  # noqa: E402


def _path_with_only_git(tmp_path, monkeypatch, fake_clis=()):
    """Restrict PATH to git plus named fake CLI executables, so 'CLI missing'
    is genuinely testable (the host machine may have gh/glab installed)."""
    bindir = tmp_path / "onlybin"
    bindir.mkdir(exist_ok=True)
    git = shutil.which("git")
    link = bindir / "git"
    if not link.exists():
        os.symlink(git, str(link))
    for name, body in fake_clis:
        script = bindir / name
        script.write_text(body, encoding="utf-8")
        script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    return bindir


def _hit_script(trace, path_hit):
    return ("#!/bin/sh\necho \"$@\" >> \"%s\"\n"
            "printf '[{\"path\": \"%s\"}]\\n'\n" % (trace, path_hit))


# ===========================================================================
# Tier 4: live forge call (gh / glab autodetect, hard timeout, negcache)
# ===========================================================================

class TestForgeTier:
    def _repo_no_match(self, tmp_path):
        """A repo where tiers 1-3 cannot match the target."""
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"app.py": "x = 1\n"})
        return cwd

    def test_github_remote_uses_gh(self, tmp_path, monkeypatch):
        cwd = self._repo_no_match(tmp_path)
        _git_raw(["remote", "add", "origin", "git@github.com:org/repo.git"], cwd)
        trace = tmp_path / "gh.trace"
        _path_with_only_git(tmp_path, monkeypatch,
                            [("gh", _hit_script(str(trace), TARGET))])
        res = _resolve(tmp_path)
        assert res["status"] == "resolved"
        assert res["via"] == "forge"
        assert res["forge"] == "gh"
        assert res["hits"] == [TARGET]
        assert "search" in trace.read_text()

    def test_non_github_remote_uses_glab(self, tmp_path, monkeypatch):
        """gitlab.com and self-hosted GitLab both select glab."""
        cwd = self._repo_no_match(tmp_path)
        _git_raw(["remote", "add", "origin", "git@gitlab.example.com:org/repo.git"], cwd)
        glab_trace = tmp_path / "glab.trace"
        _path_with_only_git(tmp_path, monkeypatch, [
            ("glab", _hit_script(str(glab_trace), TARGET)),
            ("gh", "#!/bin/sh\nexit 3\n"),  # wrong pick would fail
        ])
        res = _resolve(tmp_path)
        assert res["status"] == "resolved"
        assert res["via"] == "forge"
        assert res["forge"] == "glab"
        assert "search" in glab_trace.read_text()

    def test_forge_miss_is_unresolved_and_names_every_tier(self, tmp_path, monkeypatch):
        """Every tier ran; nothing matched: the reviewer's medium-finding case."""
        cwd = self._repo_no_match(tmp_path)
        _git_raw(["remote", "add", "origin", "git@gitlab.com:org/repo.git"], cwd)
        _path_with_only_git(tmp_path, monkeypatch,
                            [("glab", "#!/bin/sh\nprintf '[]\\n'\n")])
        res = _resolve(tmp_path)
        assert res["status"] == "unresolved"
        assert res["via"] == "none"
        assert res["searched"] == ["registry", "grep", "branch", "forge"]

    def test_missing_cli_degrades_with_warning(self, tmp_path, monkeypatch):
        cwd = self._repo_no_match(tmp_path)
        _git_raw(["remote", "add", "origin", "git@github.com:org/repo.git"], cwd)
        _path_with_only_git(tmp_path, monkeypatch)  # no gh/glab on PATH
        res = _resolve(tmp_path)
        assert res["status"] == "unavailable"
        assert res["via"] == "none"
        assert res["searched"] == ["registry", "grep", "branch"]
        assert "gh" in res["warning"] and "degraded" in res["warning"]

    def test_no_remote_degrades_with_warning(self, tmp_path):
        self._repo_no_match(tmp_path)
        res = _resolve(tmp_path)
        assert res["status"] == "unavailable"
        assert res.get("warning")
        assert "remote" in res["warning"]

    def test_auth_failure_degrades_with_warning(self, tmp_path, monkeypatch):
        cwd = self._repo_no_match(tmp_path)
        _git_raw(["remote", "add", "origin", "git@github.com:org/repo.git"], cwd)
        _path_with_only_git(tmp_path, monkeypatch, [("gh", "#!/bin/sh\nexit 4\n")])
        res = _resolve(tmp_path)
        assert res["status"] == "unavailable"
        assert "rc=4" in res["warning"]

    def test_hung_cli_respects_hard_timeout(self, tmp_path, monkeypatch):
        """The 3s bound is real: a sleeping CLI is killed, wall time < 5s."""
        cwd = self._repo_no_match(tmp_path)
        _git_raw(["remote", "add", "origin", "git@github.com:org/repo.git"], cwd)
        _path_with_only_git(tmp_path, monkeypatch,
                            [("gh", "#!/bin/sh\n/bin/sleep 30\n")])
        assert ocr_forge.FORGE_TIMEOUT <= 3.0
        start = time.monotonic()
        res = _resolve(tmp_path)  # default timeout: the spec's 3s
        elapsed = time.monotonic() - start
        assert elapsed < 5.0, "resolution took %.2fs -- timeout not enforced" % elapsed
        assert res["status"] == "unavailable"
        assert "timeout" in res["warning"]

    def test_local_hit_never_waits_for_the_forge(self, tmp_path, monkeypatch):
        """Tier precedence: a registry hit returns without any forge call."""
        cwd = _init_repo(tmp_path)
        _commit(cwd, {".ocr/artifacts.json": json.dumps({
            "weekly-2026-11.md": {"produced_by": "gen.py",
                                  "path": "out/weekly-2026-11.md"}})})
        _git_raw(["remote", "add", "origin", "git@github.com:org/repo.git"], cwd)
        _path_with_only_git(tmp_path, monkeypatch,
                            [("gh", "#!/bin/sh\n/bin/sleep 30\n")])
        start = time.monotonic()
        res = _resolve(tmp_path)
        assert time.monotonic() - start < 5.0
        assert res["status"] == "resolved"
        assert res["via"] == "registry"


class TestNegativeCache:
    NEGCACHE_REL = os.path.join(".ocr", "index", "forge_negcache.json")

    def _repo_failing_cli(self, tmp_path, monkeypatch):
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"app.py": "x = 1\n"})
        _git_raw(["remote", "add", "origin", "git@github.com:org/repo.git"], cwd)
        trace = tmp_path / "calls.trace"
        script = "#!/bin/sh\necho called >> \"%s\"\nexit 4\n" % trace
        _path_with_only_git(tmp_path, monkeypatch, [("gh", script)])
        return trace

    def test_repeated_calls_pay_for_the_forge_once(self, tmp_path, monkeypatch):
        trace = self._repo_failing_cli(tmp_path, monkeypatch)
        resolver = ocr_forge.make_pointer_resolver(str(tmp_path))
        first = resolver(TARGET)
        second = resolver(TARGET)
        assert first["status"] == "unavailable"
        assert second["status"] == "unavailable"
        assert len(trace.read_text().splitlines()) == 1
        assert "cached" in second["warning"]

    def test_negative_cache_persists_across_resolver_instances(self, tmp_path, monkeypatch):
        trace = self._repo_failing_cli(tmp_path, monkeypatch)
        assert ocr_forge.make_pointer_resolver(str(tmp_path))(TARGET)["status"] == "unavailable"
        # A whole new resolver (new in-process cache) must reuse the disk cache.
        res = ocr_forge.make_pointer_resolver(str(tmp_path))(TARGET)
        assert res["status"] == "unavailable"
        assert "cached" in res["warning"]
        assert len(trace.read_text().splitlines()) == 1

    def test_persisted_cache_is_bounded(self, tmp_path, monkeypatch):
        trace = self._repo_failing_cli(tmp_path, monkeypatch)
        full = tmp_path / self.NEGCACHE_REL
        full.parent.mkdir(parents=True)
        prefilled = {"gh:old-%04d.md" % i: {"t": time.time(), "result": "rc=4"}
                     for i in range(ocr_forge.MAX_NEGCACHE + 50)}
        full.write_text(json.dumps(prefilled), encoding="utf-8")
        ocr_forge.make_pointer_resolver(str(tmp_path))(TARGET)
        saved = json.loads(full.read_text(encoding="utf-8"))
        assert len(saved) <= ocr_forge.MAX_NEGCACHE
        assert trace.exists()  # the new lookup really ran, then pruned

    def test_stale_negative_is_retried(self, tmp_path, monkeypatch):
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"app.py": "x = 1\n"})
        _git_raw(["remote", "add", "origin", "git@github.com:org/repo.git"], cwd)
        full = tmp_path / self.NEGCACHE_REL
        full.parent.mkdir(parents=True)
        stale = time.time() - ocr_forge.NEGCACHE_TTL - 60
        full.write_text(json.dumps(
            {"gh:weekly-2026-11.md": {"t": stale, "result": "rc=4"}}),
            encoding="utf-8")
        _path_with_only_git(tmp_path, monkeypatch,
                            [("gh", _hit_script(str(tmp_path / "t"), TARGET))])
        res = _resolve(tmp_path)
        assert res["status"] == "resolved"
        assert res["via"] == "forge"


import ocr_collectors as oc  # noqa: E402
import ocr_index  # noqa: E402

_POINTER_PY = 'REPORT = "out/weekly-2026-11.md"\n'

_EMPTY = {"symbol_callers": [], "event_couples": [], "doc_refs": [],
          "conventions": [], "prose_pointers": [], "risk": {"score": 0, "reasons": []}}


# ===========================================================================
# Bundle surfacing: unresolved pointers reach the reviewer, never findings
# ===========================================================================

class TestBundleSurfacing:
    def _repo_with_pointer(self, tmp_path):
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"app/reports.py": _POINTER_PY})
        built = ocr_index.ensure_built(str(tmp_path))
        assert built["built"] is True, built
        return cwd

    def test_unresolvable_pointer_is_carried_for_the_reviewer(self, tmp_path, monkeypatch):
        """Medium finding is the REVIEWER's job: the bundle carries
        ``unresolved`` verbatim; Python fabricates nothing."""
        self._repo_with_pointer(tmp_path)
        _git_raw(["remote", "add", "origin", "git@gitlab.com:org/repo.git"],
                 str(tmp_path))
        _path_with_only_git(tmp_path, monkeypatch,
                            [("glab", "#!/bin/sh\nprintf '[]\\n'\n")])
        demands = dict(_EMPTY, prose_pointers=["weekly-2026-11"])
        result = oc.collect(str(tmp_path), demands, ["app/reports.py"],
                            pointer_resolver=ocr_forge.make_pointer_resolver(str(tmp_path)))
        entry = result["bundle"]["entries"][0]
        assert entry["status"] == "ok"
        resolution = entry["evidence"]["pointers"][0]["resolution"]
        assert resolution["status"] == "unresolved"
        assert resolution["via"] == "none"
        assert entry["note"] is None  # no fabricated finding, no invented note
        check = oc.validate_bundle(result["bundle"])
        assert check == {"ok": True, "errors": []}, check

    def test_collectors_cli_wires_the_real_resolver(self, tmp_path):
        """The collectors call site passes ocr_forge's resolver, so a registry
        hit shows up in the printed bundle without any extra skill step."""
        self._repo_with_pointer(tmp_path)
        _commit(str(tmp_path), {".ocr/artifacts.json": json.dumps({
            "weekly-2026-11.md": {"produced_by": "app/reports.py",
                                  "path": "out/weekly-2026-11.md", "mr": "!7"}})})
        ocr_index.ensure_built(str(tmp_path))  # index picks up the new file
        demands = dict(_EMPTY, prose_pointers=["out/weekly-2026-11.md"])
        proc = subprocess.run(
            [sys.executable, os.path.join(_SCRIPTS, "ocr_collectors.py"),
             "--repo-root", str(tmp_path), "--changed-file", "app/reports.py"],
            input=json.dumps(demands), capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        resolution = out["bundle"]["entries"][0]["evidence"]["pointers"][0]["resolution"]
        assert resolution["status"] == "resolved"
        assert resolution["via"] == "registry"
        assert resolution["mr"] == "!7"
        check = oc.validate_bundle(out["bundle"])
        assert check == {"ok": True, "errors": []}, check
