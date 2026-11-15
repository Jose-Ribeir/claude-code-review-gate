"""Fixture M4 -- dangling report pointer (review-gate v2, t12).

The known bug shape (spec problem statement #4): a string in the diff
references a report produced by a sibling merge request. Nothing in the
repo, the artifact registry, sibling branch tips, or the forge produces
``out/weekly-2026-W48.md`` -- the pointer dangles. Resolution needs
repo-wide and MR-level knowledge a diff-scoped gate never had.

Both sides asserted here:

* OLD (parametrized absence): pre-v2 there is no ``ocr_forge`` at all -- no
  registry tier, no grep tier, no branch tier, no forge tier.
* v2: the index surfaces the pointer utterance, the resolver walks every
  applicable tier and returns ``unresolved`` (registry -> grep, with the
  pointer's own utterance site excluded -> branch tips -> stubbed forge
  returning zero hits), and the collectors' bundle surfaces the utterance
  with its resolution -- the reviewer's medium finding material.
  A registry entry rehabilitating the same pointer flips it to resolved.

The forge CLI is stubbed (a tiny shell script printing ``[]`` with rc 0) so
the "searched, nothing found" path is deterministic; offline degradation is
covered in tests/test_fixture_degradation.py.
"""
import os
import shutil
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "fixtures", "miss_categories")
for _p in (_SCRIPTS, _FIXTURES):
    sys.path.insert(0, _p)

import ocr_collectors  # noqa: E402
import ocr_forge  # noqa: E402
import ocr_index  # noqa: E402
import builders  # noqa: E402


def _m4(tmp_path):
    info = builders.build_m4(tmp_path / "work")
    # Repoint the local-path origin at a GitLab host: a local-path origin is
    # an unsupported host, so the forge tier would be skipped (and must not
    # count as searched); this fixture wants the tier to genuinely engage.
    builders.git(["remote", "set-url", "origin",
                  "https://gitlab.example.invalid/dept/weekly.git"],
                 info["root"])
    return info


def _stub_glab_env(tmp_path, monkeypatch):
    """A forge that runs and finds nothing: PATH = real git dir + a fake
    `glab` printing `[]` with rc 0 (the fixture's origin is repointed at a
    gitlab host in _m4, so autodetection picks glab)."""
    bin_dir = builders.make_forge_cli(tmp_path / "bin", "glab", "[]", rc=0)
    git_dir = os.path.dirname(shutil.which("git"))
    monkeypatch.setenv("PATH", os.pathsep.join([bin_dir, git_dir]))
    return bin_dir


def _resolve(info):
    resolver = ocr_forge.make_pointer_resolver(info["root"])
    return resolver(info["target"])


class TestFixtureSelfCheck:
    def test_the_diff_introduces_the_pointer(self, tmp_path):
        info = _m4(tmp_path)
        diff = builders.git(["diff", "%s..%s" % (info["tip"] + "~1", info["tip"]),
                             "--", info["src"]], info["root"])
        assert '+%s = "%s"' % ("WEEKLY_NOTE", info["target"]) in diff

    def test_target_produced_nowhere_in_the_repo(self, tmp_path):
        info = _m4(tmp_path)
        assert not os.path.exists(os.path.join(info["root"], info["target"]))
        hits = builders.git(["grep", "-l", "-F", "--full-name",
                             "weekly-2026-W48.md", "HEAD"], info["root"])
        # only the utterance itself (git prefixes hits with the tree-ish)
        assert hits.strip() == "HEAD:" + info["src"]

    def test_origin_is_not_github_so_glab_is_the_forge(self, tmp_path):
        """Deterministic CLI choice for the stub: _m4 repoints origin at a
        gitlab host so autodetection selects glab and the tier runs."""
        info = _m4(tmp_path)
        url = builders.git(["remote", "get-url", "origin"], info["root"]).strip()
        assert url and "github.com" not in url


class TestOldPathMisses:
    def test_no_pointer_machinery_existed_pre_v2(self):
        """Parametrized absence: git show of the pre-v2 tree finds neither
        the forge resolver nor the index that feeds it."""
        assert builders.load_main_module("ocr_forge") is None
        assert builders.load_main_module("ocr_index") is None


class TestV2Catches:
    def test_index_surfaces_the_utterance(self, tmp_path):
        info = _m4(tmp_path)
        ocr_index.ensure_built(info["root"])
        result = ocr_index.query_pointer_utterances(info["root"])
        utterances = [(p["path"], p["target"]) for p in result["pointers"]]
        assert (info["src"], info["target"]) in utterances

    def test_resolver_walks_all_tiers_to_unresolved(self, tmp_path, monkeypatch):
        """Registry: no .ocr/artifacts.json. Grep: the only hit is the
        utterance itself (excluded). Branches: no sibling refs. Forge:
        stubbed CLI runs, returns zero hits. Every tier ran; nothing
        produced the report -> unresolved, via none."""
        info = _m4(tmp_path)
        ocr_index.ensure_built(info["root"])
        _stub_glab_env(tmp_path, monkeypatch)
        resolution = _resolve(info)
        assert resolution["status"] == "unresolved"
        assert resolution["via"] == "none"
        assert resolution["searched"] == ["registry", "grep", "branch", "forge"]

    def test_bundle_surfaces_the_unresolved_pointer(self, tmp_path, monkeypatch):
        """The collectors hand the reviewer the utterance AND its
        resolution stubs the reviewer may cite as medium."""
        info = _m4(tmp_path)
        ocr_index.ensure_built(info["root"])
        _stub_glab_env(tmp_path, monkeypatch)
        resolver = ocr_forge.make_pointer_resolver(info["root"])
        result = ocr_collectors.collect(
            info["root"], info["spotter_demands"], info["changed_files"],
            pointer_resolver=resolver)
        entries = [e for e in result["bundle"]["entries"]
                   if e["kind"] == "prose_pointers"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["status"] == "ok"
        pointer = entry["evidence"]["pointers"][0]
        assert pointer["path"] == info["src"]
        assert pointer["target"] == info["target"]
        assert pointer["resolution"]["status"] == "unresolved"
        assert ocr_collectors.validate_bundle(result["bundle"]) == {
            "ok": True, "errors": []}

    def test_registry_rehabilitates_the_pointer(self, tmp_path):
        """A committed .ocr/artifacts.json entry naming the report resolves
        at the first tier -- the only difference between dangling and
        fine is the registry, so the unresolved assertion above is
        load-bearing."""
        info = _m4(tmp_path)
        ocr_index.ensure_built(info["root"])
        builders.commit(info["root"], {".ocr/artifacts.json": info["registry"]})
        resolution = _resolve(info)
        assert resolution["status"] == "resolved"
        assert resolution["via"] == "registry"
        assert resolution["produced_by"] == "ci/reporting-pipeline"
        assert resolution.get("mr") == "sibling-mr-42"

    def test_grep_tier_producing_hit_would_resolve(self, tmp_path, monkeypatch):
        """Second contrast: when ANY producer mentions the target, tier 2
        resolves -- so 'unresolved' names a real absence, not a stub
        artifact."""
        info = _m4(tmp_path)
        ocr_index.ensure_built(info["root"])
        _stub_glab_env(tmp_path, monkeypatch)
        builders.commit(info["root"], {"ci/reporting.py": (
            'WEEKLY_TEMPLATE = "weekly-2026-W48"\n'
            'OUT_PATH = "out/weekly-2026-W48.md"\n')})
        resolution = _resolve(info)
        assert resolution["status"] == "resolved"
        assert resolution["via"] == "grep"
        assert any(h["path"] == "ci/reporting.py" for h in resolution["hits"])
