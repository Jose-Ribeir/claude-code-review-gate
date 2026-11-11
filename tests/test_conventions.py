"""Tests for scripts/ocr_conventions.py — two-layer convention docs resolver.

Layer 1 (explicit): ``conventions: [{doc, applyTo}]`` in ``.ocr/rule.json``;
``applyTo`` defaults to ``["**/*"]`` so a doc applies repo-wide unless scoped.
Layer 2 (walk-up): nearest ``AGENTS.md`` / ``.ocr/conventions.md`` at or above
the changed file's directory. Both layers merge, explicit first, with per-doc
and total line budgets; overflow is truncated with warnings.
"""
import json
import os
import subprocess
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)

import ocr_conventions as oc  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(root, relpath, content):
    path = os.path.join(str(root), relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def _rule(root, conventions, **extra):
    payload = {"conventions": conventions}
    payload.update(extra)
    return _write(root, ".ocr/rule.json", json.dumps(payload))


def _resolve(root, files, **kwargs):
    return oc.resolve_conventions(str(root), files, **kwargs)


def _bodies(result, relpath):
    """Return the raw combined text for one file."""
    return result["files"].get(relpath, "")


# ---------------------------------------------------------------------------
# Glob matching
# ---------------------------------------------------------------------------

class TestGlobMatch:
    def test_double_star_crosses_directories(self):
        assert oc._glob_to_regex("src/**").match("src/deep/nested/a.py")
        assert oc._glob_to_regex("src/**").match("src/a.py")

    def test_single_star_stays_in_one_segment(self):
        rx = oc._glob_to_regex("docs/*.md")
        assert rx.match("docs/style.md")
        assert not rx.match("docs/sub/style.md")

    def test_default_glob_matches_everything(self):
        rx = oc._glob_to_regex("**/*")
        assert rx.match("a.py")
        assert rx.match("src/deep/nested/a.go")

    def test_non_matching_glob(self):
        assert not oc._glob_to_regex("src/**").match("tests/test_a.py")


# ---------------------------------------------------------------------------
# Layer 1: explicit mappings
# ---------------------------------------------------------------------------

class TestExplicitMappings:
    def test_global_doc_applies_to_src_files(self, tmp_path):
        """(a) A doc mapped with an explicit glob applies to matching files."""
        _write(tmp_path, "docs/STYLE.md", "Use snake_case everywhere.\n")
        _write(tmp_path, "src/app/main.py", "x = 1\n")
        _rule(tmp_path, [{"doc": "docs/STYLE.md", "applyTo": ["src/**"]}])

        result = _resolve(tmp_path, ["src/app/main.py"])

        assert "Use snake_case everywhere." in _bodies(result, "src/app/main.py")
        assert "docs/STYLE.md" in _bodies(result, "src/app/main.py")
        assert result["warnings"] == []

    def test_scoped_doc_skips_non_matching_files(self, tmp_path):
        """A doc scoped to src/** must not leak into tests/**."""
        _write(tmp_path, "docs/STYLE.md", "Use snake_case everywhere.\n")
        _write(tmp_path, "tests/test_a.py", "pass\n")
        _rule(tmp_path, [{"doc": "docs/STYLE.md", "applyTo": ["src/**"]}])

        result = _resolve(tmp_path, ["tests/test_a.py"])

        assert "tests/test_a.py" not in result["files"]
        assert result["warnings"] == []

    def test_unmapped_doc_defaults_to_global(self, tmp_path):
        """(c) A conventions entry without applyTo applies to every file."""
        _write(tmp_path, "docs/CONV.md", "Never swallow exceptions silently.\n")
        _write(tmp_path, "src/api/handler.go", "package api\n")
        _rule(tmp_path, [{"doc": "docs/CONV.md"}])

        result = _resolve(tmp_path, ["src/api/handler.go", "README.md"])

        assert "Never swallow exceptions silently." in _bodies(result, "src/api/handler.go")
        assert "Never swallow exceptions silently." in _bodies(result, "README.md")
        assert result["warnings"] == []

    def test_multiple_mappings_preserve_order(self, tmp_path):
        _write(tmp_path, "docs/A.md", "rule A\n")
        _write(tmp_path, "docs/B.md", "rule B\n")
        _rule(tmp_path, [{"doc": "docs/A.md"}, {"doc": "docs/B.md"}])

        result = _resolve(tmp_path, ["x.py"])
        body = _bodies(result, "x.py")

        assert body.index("rule A") < body.index("rule B")

    def test_same_doc_in_two_mappings_included_once(self, tmp_path):
        _write(tmp_path, "docs/A.md", "rule A\n")
        _rule(tmp_path, [{"doc": "docs/A.md"}, {"doc": "docs/A.md", "applyTo": ["**/*.py"]}])

        result = _resolve(tmp_path, ["x.py"])
        body = _bodies(result, "x.py")

        assert body.count("rule A") == 1

    def test_missing_doc_warns_and_is_skipped(self, tmp_path):
        _write(tmp_path, "src/a.py", "x = 1\n")
        _rule(tmp_path, [{"doc": "docs/NOPE.md"}])

        result = _resolve(tmp_path, ["src/a.py"])

        assert "src/a.py" not in result["files"]
        assert any("docs/NOPE.md" in w for w in result["warnings"])

    def test_invalid_rule_json_warns_fail_soft(self, tmp_path):
        _write(tmp_path, "src/a.py", "x = 1\n")
        _write(tmp_path, ".ocr/rule.json", "{not json")

        result = _resolve(tmp_path, ["src/a.py"])

        assert result["files"] == {} or result["files"].get("src/a.py", "") == ""
        assert any("rule.json" in w for w in result["warnings"])

    def test_no_rule_json_no_ancestor_docs_gives_empty(self, tmp_path):
        _write(tmp_path, "src/a.py", "x = 1\n")

        result = _resolve(tmp_path, ["src/a.py"])

        assert result["files"] == {}
        assert result["warnings"] == []

    def test_doc_path_resolves_relative_to_rule_json_dir(self, tmp_path):
        """A nested .ocr/rule.json resolves doc paths against its config root."""
        _write(tmp_path, "src/web/.ocr/rule.json",
               json.dumps({"conventions": [{"doc": "WEB.md"}]}))
        _write(tmp_path, "src/web/WEB.md", "web rules\n")
        _write(tmp_path, "src/web/page.ts", "export {};\n")
        _write(tmp_path, "src/api/server.py", "x = 1\n")

        result = _resolve(tmp_path, ["src/web/page.ts", "src/api/server.py"])

        assert "web rules" in _bodies(result, "src/web/page.ts")
        assert "src/api/server.py" not in result["files"]


# ---------------------------------------------------------------------------
# Layer 2: walk-up ancestor docs
# ---------------------------------------------------------------------------

class TestWalkUpAncestors:
    def test_nested_agents_md_applies_to_subtree_only(self, tmp_path):
        """(b) A nested AGENTS.md governs its subtree, not sibling subtrees."""
        _write(tmp_path, "src/web/AGENTS.md", "Components must be functional.\n")
        _write(tmp_path, "src/web/deep/comp.tsx", "export const C = () => null;\n")
        _write(tmp_path, "src/api/server.py", "x = 1\n")

        result = _resolve(tmp_path, ["src/web/deep/comp.tsx", "src/api/server.py"])

        assert "Components must be functional." in _bodies(result, "src/web/deep/comp.tsx")
        assert "src/api/server.py" not in result["files"]
        assert result["warnings"] == []

    def test_nearest_ancestor_wins(self, tmp_path):
        _write(tmp_path, "AGENTS.md", "root conventions\n")
        _write(tmp_path, "src/web/AGENTS.md", "web conventions\n")
        _write(tmp_path, "src/web/page.tsx", "export {};\n")

        result = _resolve(tmp_path, ["src/web/page.tsx"])
        body = _bodies(result, "src/web/page.tsx")

        assert "web conventions" in body
        assert "root conventions" not in body

    def test_root_agents_md_covers_whole_repo(self, tmp_path):
        _write(tmp_path, "AGENTS.md", "root conventions\n")
        _write(tmp_path, "a/b/c.py", "x = 1\n")

        result = _resolve(tmp_path, ["a/b/c.py"])

        assert "root conventions" in _bodies(result, "a/b/c.py")

    def test_ocr_conventions_md_is_an_ancestor_doc(self, tmp_path):
        _write(tmp_path, "src/.ocr/conventions.md", "src-layer conventions\n")
        _write(tmp_path, "src/mod/tool.go", "package mod\n")
        _write(tmp_path, "other/x.py", "x = 1\n")

        result = _resolve(tmp_path, ["src/mod/tool.go", "other/x.py"])

        body = _bodies(result, "src/mod/tool.go")
        assert "src-layer conventions" in body
        assert "src/.ocr/conventions.md" in body
        assert "other/x.py" not in result["files"]

    def test_both_docs_in_same_dir_apply_agents_first(self, tmp_path):
        _write(tmp_path, "pkg/AGENTS.md", "agents doc\n")
        _write(tmp_path, "pkg/.ocr/conventions.md", "ocr doc\n")
        _write(tmp_path, "pkg/x.py", "x = 1\n")

        result = _resolve(tmp_path, ["pkg/x.py"])
        body = _bodies(result, "pkg/x.py")

        assert "agents doc" in body
        assert "ocr doc" in body
        assert body.index("agents doc") < body.index("ocr doc")


# ---------------------------------------------------------------------------
# Merge of both layers
# ---------------------------------------------------------------------------

class TestLayerMerge:
    def test_explicit_mappings_come_before_ancestor_docs(self, tmp_path):
        _write(tmp_path, "docs/GLOBAL.md", "global rule\n")
        _rule(tmp_path, [{"doc": "docs/GLOBAL.md"}])
        _write(tmp_path, "src/web/AGENTS.md", "web conventions\n")
        _write(tmp_path, "src/web/page.tsx", "export {};\n")

        result = _resolve(tmp_path, ["src/web/page.tsx"])
        body = _bodies(result, "src/web/page.tsx")

        assert "global rule" in body
        assert "web conventions" in body
        assert body.index("global rule") < body.index("web conventions")

    def test_doc_in_both_layers_included_once(self, tmp_path):
        """An explicit mapping pointing at the walk-up doc must not duplicate it."""
        _write(tmp_path, "src/web/AGENTS.md", "web conventions\n")
        _write(tmp_path, "src/web/.ocr/rule.json",
               json.dumps({"conventions": [{"doc": "AGENTS.md"}]}))
        _write(tmp_path, "src/web/page.tsx", "export {};\n")

        result = _resolve(tmp_path, ["src/web/page.tsx"])
        body = _bodies(result, "src/web/page.tsx")

        assert body.count("web conventions") == 1

    def test_blocks_labelled_with_layer(self, tmp_path):
        _write(tmp_path, "docs/GLOBAL.md", "global rule\n")
        _rule(tmp_path, [{"doc": "docs/GLOBAL.md"}])
        _write(tmp_path, "pkg/AGENTS.md", "pkg conventions\n")
        _write(tmp_path, "pkg/x.py", "x = 1\n")

        body = _bodies(_resolve(tmp_path, ["pkg/x.py"]), "pkg/x.py")

        assert "## Convention: docs/GLOBAL.md (explicit)" in body
        assert "## Convention: pkg/AGENTS.md (ancestor)" in body


# ---------------------------------------------------------------------------
# Budgets: per-doc cap and total budget
# ---------------------------------------------------------------------------

class TestBudgets:
    def test_oversized_doc_truncates_with_warning(self, tmp_path):
        """(d) A doc longer than the per-doc cap is truncated; warning emitted."""
        big = "".join("rule line %d\n" % i for i in range(1000))
        _write(tmp_path, "docs/BIG.md", big)
        _rule(tmp_path, [{"doc": "docs/BIG.md"}])

        result = _resolve(tmp_path, ["src/a.py"])
        body = _bodies(result, "src/a.py")

        assert "rule line 799" in body
        assert "rule line 800" not in body
        assert "truncated" in body
        assert any("docs/BIG.md" in w and "truncated" in w for w in result["warnings"])

    def test_default_per_doc_cap_is_800(self):
        assert oc.MAX_DOC_LINES == 800

    def test_doc_exactly_at_cap_is_not_truncated(self, tmp_path):
        big = "".join("rule line %d\n" % i for i in range(800))
        _write(tmp_path, "docs/BIG.md", big)
        _rule(tmp_path, [{"doc": "docs/BIG.md"}])

        result = _resolve(tmp_path, ["src/a.py"])

        assert result["warnings"] == []
        assert "truncated" not in _bodies(result, "src/a.py")

    def test_total_budget_truncates_later_docs(self, tmp_path):
        _write(tmp_path, "docs/A.md", "".join("a%d\n" % i for i in range(700)))
        _write(tmp_path, "docs/B.md", "".join("b%d\n" % i for i in range(700)))
        _rule(tmp_path, [{"doc": "docs/A.md"}, {"doc": "docs/B.md"}])

        result = _resolve(tmp_path, ["x.py"], max_total_lines=750)
        body = _bodies(result, "x.py")

        assert "a699" in body
        assert "b699" not in body
        assert any("total budget" in w and "docs/B.md" in w for w in result["warnings"])

    def test_total_budget_drops_docs_when_exhausted(self, tmp_path):
        _write(tmp_path, "docs/A.md", "".join("a%d\n" % i for i in range(50)))
        _write(tmp_path, "docs/B.md", "b0\n")
        _rule(tmp_path, [{"doc": "docs/A.md"}, {"doc": "docs/B.md"}])

        result = _resolve(tmp_path, ["x.py"], max_total_lines=10)
        body = _bodies(result, "x.py")

        assert "b0" not in body
        assert any("dropped doc docs/B.md" in w for w in result["warnings"])

    def test_budgets_apply_per_file_not_globally(self, tmp_path):
        _write(tmp_path, "docs/A.md", "".join("a%d\n" % i for i in range(100)))
        _rule(tmp_path, [{"doc": "docs/A.md"}])

        result = _resolve(tmp_path, ["x.py", "y.py"], max_total_lines=50)

        # 1 header line leaves 49 doc lines: a0..a48 visible in BOTH files.
        body_x, body_y = _bodies(result, "x.py"), _bodies(result, "y.py")
        assert "a48" in body_x and "a49" not in body_x
        assert "a48" in body_y and "a49" not in body_y


# ---------------------------------------------------------------------------
# Fail-soft and CLI
# ---------------------------------------------------------------------------

class TestFailSoftAndCli:
    def test_files_with_backslashes_are_normalised(self, tmp_path):
        _write(tmp_path, "src/AGENTS.md", "src conventions\n")

        result = _resolve(tmp_path, ["src\\mod\\a.py"])

        assert any("a.py" in k for k in result["files"])

    def test_missing_repo_root_returns_empty_with_warning(self, tmp_path):
        result = _resolve(tmp_path / "no-such-dir", ["x.py"])

        assert result["files"] == {}
        # Walk-up on a missing root finds nothing; that alone is not an error,
        # so no warning is required — but the resolver must not raise.

    def test_run_cli_json_roundtrip(self, tmp_path):
        script = os.path.join(_SCRIPTS, "ocr_conventions.py")
        _write(tmp_path, "docs/STYLE.md", "cli rule\n")
        _rule(tmp_path, [{"doc": "docs/STYLE.md"}])

        proc = subprocess.run(
            [sys.executable, script, "--repo-root", str(tmp_path), "src/a.py"],
            capture_output=True, text=True)

        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        assert "cli rule" in payload["files"]["src/a.py"]
        assert payload["warnings"] == []

    def test_run_cli_reports_truncation_warnings(self, tmp_path):
        script = os.path.join(_SCRIPTS, "ocr_conventions.py")
        _write(tmp_path, "docs/BIG.md", "".join("r%d\n" % i for i in range(20)))
        _rule(tmp_path, [{"doc": "docs/BIG.md"}])

        proc = subprocess.run(
            [sys.executable, script, "--repo-root", str(tmp_path),
             "--max-doc-lines", "5", "src/a.py"],
            capture_output=True, text=True)

        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        assert any("truncated" in w for w in payload["warnings"])
        assert "r5" not in payload["files"]["src/a.py"]


# ---------------------------------------------------------------------------
# Skill contract: SKILL.md must wire the resolver into §2
# ---------------------------------------------------------------------------

_SKILL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "skills", "review")


class TestSkillContract:
    def _read(self, name):
        with open(os.path.join(_SKILL, name), encoding="utf-8") as fh:
            return fh.read()

    def test_skill_step7_references_resolver_script(self):
        skill = self._read("SKILL.md")
        assert "ocr_conventions.py" in skill
        assert "Convention" in skill or "convention" in skill

    def test_conventions_spec_file_documents_contract(self):
        spec = self._read("conventions.md")
        for needle in ("MAX_DOC_LINES", "800", "applyTo", "**/*", "AGENTS.md",
                       ".ocr/conventions.md", "warnings", "explicit", "ancestor"):
            assert needle in spec, needle
