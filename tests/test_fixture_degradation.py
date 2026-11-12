"""Degradation-path fixture variants (review-gate v2, t12).

Spec user stories 19/20/26/27: every degradation path emits a DISTINCT
warning and behaves as documented -- silent protection loss is structurally
impossible. Each variant below reuses a miss-category fixture and asserts
its documented warning:

* no tree-sitter -> the regex fallback's capability warning; the M1
  event coupling still extracted (documented-weaker, not absent);
* offline forge -> pointer resolution degrades to the local tiers with a
  "CLI not found" / "no origin remote" warning, status ``unavailable``;
* malformed spotter output -> demands fail closed to T0 (None) with a
  warning, findings-shaped content dropped, and the collectors proceed
  with an empty bundle (today's review behavior);
* oversized evidence bundle -> largest-entry-first truncation warnings,
  no entry dropped entirely, total within budget.
"""
import json
import os
import shutil
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "fixtures", "miss_categories")
for _p in (_SCRIPTS, _FIXTURES):
    sys.path.insert(0, _p)

import ocr_collectors  # noqa: E402
import ocr_demands  # noqa: E402
import ocr_forge  # noqa: E402
import ocr_index  # noqa: E402
import builders  # noqa: E402


# ---------------------------------------------------------------------------
# No tree-sitter: documented-weaker regex extraction, with a warning
# ---------------------------------------------------------------------------

class TestNoTreeSitter:
    def test_capability_warning_names_the_lost_precision(self, tmp_path,
                                                         monkeypatch):
        monkeypatch.setenv("OCR_INDEX_TREE_SITTER", "0")
        info = builders.build_m1(tmp_path / "work")
        built = ocr_index.ensure_built(info["root"])
        assert built["built"] is True
        assert built["tool"] == "regex"
        assert any("tree-sitter unavailable" in w and "regex" in w
                   for w in built["warnings"]), built["warnings"]

    def test_regex_fallback_still_extracts_the_coupling(self, tmp_path,
                                                        monkeypatch):
        """Degraded means weaker, not silent-empty: the starter patterns are
        regex-native, so the M1 couple survives the fallback."""
        monkeypatch.setenv("OCR_INDEX_TREE_SITTER", "0")
        info = builders.build_m1(tmp_path / "work")
        ocr_index.ensure_built(info["root"])
        couples = ocr_index.query_event_couples(
            info["root"], [info["event"]])["couples"]
        assert any(c["producer"]["path"] == info["mount"] and
                   c["consumer"]["path"] == info["filter"] for c in couples)

    def test_capability_warnings_function_lists_the_fallback(self, monkeypatch):
        monkeypatch.setenv("OCR_INDEX_TREE_SITTER", "0")
        warnings = ocr_index.capability_warnings()
        assert len(warnings) == 1
        assert "tree-sitter" in warnings[0] and "OCR_INDEX_TREE_SITTER" in warnings[0]


# ---------------------------------------------------------------------------
# Offline forge: local tiers only, with a warning
# ---------------------------------------------------------------------------

def _isolated_path(tmp_path, monkeypatch):
    """PATH holding git's real directory and NOTHING ELSE (no glab/gh)."""
    git_dir = os.path.dirname(shutil.which("git"))
    monkeypatch.setenv("PATH", git_dir)


def _gitlab_origin(info):
    """Repoint m4's local-path origin at a gitlab-hosting URL. The forge
    tier autodetects ``glab`` from a host containing 'gitlab'; any other
    host now skips the tier outright (findings-#10 fix), so the CLI-missing
    and CLI-fails degradation paths this class exists to check need an
    origin that still claws the tier back to ``glab``."""
    builders.git(["remote", "set-url", "origin",
                  "https://gitlab.example.invalid/dept/weekly.git"],
                 info["root"])


class TestOfflineForge:
    def test_missing_forge_cli_degrades_to_local_tiers(self, tmp_path, monkeypatch):
        info = builders.build_m4(tmp_path / "work")
        _gitlab_origin(info)
        ocr_index.ensure_built(info["root"])
        _isolated_path(tmp_path, monkeypatch)
        resolution = ocr_forge.make_pointer_resolver(info["root"])(info["target"])
        assert resolution["status"] == "unavailable"
        assert resolution["via"] == "none"
        assert resolution["searched"] == ["registry", "grep", "branch"]
        assert "glab CLI not found" in resolution["warning"]
        assert "degraded to local tiers" in resolution["warning"]

    def test_degraded_forge_still_reaches_the_bundle_as_unavailable(
            self, tmp_path, monkeypatch):
        info = builders.build_m4(tmp_path / "work")
        _gitlab_origin(info)
        ocr_index.ensure_built(info["root"])
        _isolated_path(tmp_path, monkeypatch)
        resolver = ocr_forge.make_pointer_resolver(info["root"])
        result = ocr_collectors.collect(
            info["root"], info["spotter_demands"], info["changed_files"],
            pointer_resolver=resolver)
        pointer = result["bundle"]["entries"][0]["evidence"]["pointers"][0]
        assert pointer["resolution"]["status"] == "unavailable"
        assert "CLI not found" in (pointer["resolution"].get("warning") or "")

    def test_no_origin_remote_degrades_with_its_own_warning(self, tmp_path):
        info = builders.build_m4(tmp_path / "work")
        ocr_index.ensure_built(info["root"])
        builders.git(["remote", "remove", "origin"], info["root"])
        resolution = ocr_forge.make_pointer_resolver(info["root"])(info["target"])
        assert resolution["status"] == "unavailable"
        assert "no origin remote" in resolution["warning"]

    def test_local_tiers_still_resolve_when_they_can(self, tmp_path, monkeypatch):
        """Offline never degrades what's locally knowable (spec story 8):
        the registry tier resolves fine without any forge."""
        info = builders.build_m4(tmp_path / "work")
        ocr_index.ensure_built(info["root"])
        builders.commit(info["root"], {".ocr/artifacts.json": info["registry"]})
        _isolated_path(tmp_path, monkeypatch)
        resolution = ocr_forge.make_pointer_resolver(info["root"])(info["target"])
        assert resolution["status"] == "resolved"
        assert resolution["via"] == "registry"

    def test_failing_forge_cli_is_cached_as_negative(self, tmp_path, monkeypatch):
        """An unauthneticated-forge answer (rc=1) degrades AND is cached:
        the second call pays nothing (spec: cached negative results)."""
        info = builders.build_m4(tmp_path / "work")
        _gitlab_origin(info)
        ocr_index.ensure_built(info["root"])
        bin_dir = builders.make_forge_cli(tmp_path / "bin", "glab", "error", rc=1)
        git_dir = os.path.dirname(shutil.which("git"))
        monkeypatch.setenv("PATH", os.pathsep.join([bin_dir, git_dir]))
        resolver = ocr_forge.make_pointer_resolver(info["root"])
        first = resolver(info["target"])
        assert first["status"] == "unavailable"
        assert "offline or unauthenticated" in first["warning"]
        second = resolver(info["target"])
        assert second["status"] == "unavailable"
        assert "previously failed" in second["warning"]


# ---------------------------------------------------------------------------
# Malformed spotter output: fail closed to T0, with a warning
# ---------------------------------------------------------------------------

class TestMalformedSpotterOutput:
    def test_non_json_output_falls_back_to_t0(self, tmp_path):
        info = builders.build_m1(tmp_path / "work")
        result = ocr_demands.validate_demands("The diff touches the slots...")
        assert result["demands"] is None
        assert any("not valid JSON" in w and "T0 fallback" in w
                   for w in result["warnings"])
        # ...and the gate proceeds with today's behavior: an empty bundle.
        bundle = ocr_collectors.collect(info["root"], result["demands"],
                                        info["changed_files"])
        assert bundle["bundle"]["entries"] == []
        assert ocr_collectors.validate_bundle(bundle["bundle"]) == {
            "ok": True, "errors": []}

    def test_unrecognized_shape_fails_closed(self):
        result = ocr_demands.validate_demands(json.dumps({"unknown_key": []}))
        assert result["demands"] is None
        assert any("unrecognized shape" in w for w in result["warnings"])

    def test_finding_shaped_output_is_dropped(self):
        """A spotter emitting FINDINGS instead of demands: the one
        enumerated exception drops them (demands-only contract) and the
        rest still falls back, never strengthened."""
        payload = json.dumps([{"severity": "high", "content": "block this"}])
        result = ocr_demands.validate_demands(payload)
        assert result["demands"] is None
        assert any("finding-shaped" in w and "dropped" in w
                   for w in result["warnings"])

    def test_embedded_findings_are_dropped_but_demands_survive(self):
        demands = dict(builders.demands(event_couples=["history-merge"]))
        demands["findings"] = [{"severity": "low", "content": "niggle"}]
        result = ocr_demands.validate_demands(json.dumps(demands))
        assert result["demands"] is not None
        assert result["demands"]["event_couples"] == ["history-merge"]
        assert any("1 finding-shaped object" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# Oversized bundle: largest-first truncation, warnings, nothing dropped
# ---------------------------------------------------------------------------

class TestOversizedBundle:
    def _heavy_repo(self, root):
        """Two convention docs; the BIG one alone blows a small budget."""
        big = "Verbose house rule, spelled at length. " * 400  # ~16k chars
        builders.init_repo(root, {
            "src/app.py": "x = 1\n",
            "docs/BIG.md": big + "\n",
            "docs/SMALL.md": "S" * 900 + "\n",
            ".ocr/rule.json": json.dumps({"conventions": [
                {"doc": "docs/BIG.md"}, {"doc": "docs/SMALL.md"}]}) + "\n",
        })
        return str(root)

    def test_oversized_bundle_truncates_with_warnings(self, tmp_path):
        root = self._heavy_repo(tmp_path / "work")
        demands = builders.demands(doc_refs=["docs/BIG.md", "docs/SMALL.md"])
        result = ocr_collectors.collect(root, demands, ["src/app.py"],
                                        max_chars=10_000)
        bundle = result["bundle"]
        assert bundle["truncated"] is True
        assert bundle["total_chars"] <= 10_000
        warning_lines = [w for w in result["warnings"]
                         if "largest-entry-first" in w]
        assert warning_lines, result["warnings"]
        # Every demand still has an entry -- degradation never drops one.
        assert len(bundle["entries"]) == 2
        assert all(e["status"] == "ok" for e in bundle["entries"])
        assert ocr_collectors.validate_bundle(bundle) == {"ok": True, "errors": []}

    def test_largest_entry_truncates_before_the_small_one(self, tmp_path):
        root = self._heavy_repo(tmp_path / "work")
        demands = builders.demands(doc_refs=["docs/BIG.md", "docs/SMALL.md"])
        result = ocr_collectors.collect(root, demands, ["src/app.py"],
                                        max_chars=10_000)
        entries = {e["evidence"]["doc"]: e
                   for e in result["bundle"]["entries"]}
        assert entries["docs/BIG.md"].get("truncated") is True
        assert not entries["docs/SMALL.md"].get("truncated")
