"""Tests for scripts/ocr_impact.py — uses real tiny git repos in tmp_path."""
import os
import subprocess
import sys
import textwrap

# Make sure the scripts directory is importable.
_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)

import ocr_impact as oi  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git_raw(args, cwd):
    """Run git in cwd and return (stdout, rc) without stripping."""
    result = subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout, result.returncode


def _init_repo(tmp_path):
    """Create a minimal git repo and return the cwd string."""
    cwd = str(tmp_path)
    for cmd in [
        ["init"],
        ["config", "user.email", "test@test.com"],
        ["config", "user.name", "Tester"],
        ["config", "commit.gpgsign", "false"],
    ]:
        _git_raw(cmd, cwd)
    return cwd


def _commit(cwd, files: dict, message="test commit"):
    """Write files dict {relpath: content} and commit."""
    for relpath, content in files.items():
        full = os.path.join(cwd, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
    _git_raw(["add", "-A"], cwd)
    _git_raw(["commit", "-m", message], cwd)
    out, _ = _git_raw(["rev-parse", "HEAD"], cwd)
    return out.strip()


# ===========================================================================
# changed_symbols — Python
# ===========================================================================

class TestChangedSymbolsPython:
    def test_body_change(self):
        old = textwrap.dedent("""\
            def foo(x):
                return x + 1
        """)
        new = textwrap.dedent("""\
            def foo(x):
                return x + 2
        """)
        r = oi.changed_symbols("mod.py", old, new)
        assert r["supported"] is True
        names = [s["name"] for s in r["symbols"]]
        assert "foo" in names
        foo = next(s for s in r["symbols"] if s["name"] == "foo")
        assert foo["change"] == "body"

    def test_deletion_only_change_finds_via_old_text(self):
        """Removing an if-guard has no new-side changed lines; must still find the function."""
        old = textwrap.dedent("""\
            def guarded(x):
                if x is None:
                    return None
                return x * 2
        """)
        new = textwrap.dedent("""\
            def guarded(x):
                return x * 2
        """)
        r = oi.changed_symbols("mod.py", old, new)
        names = [s["name"] for s in r["symbols"]]
        assert "guarded" in names
        g = next(s for s in r["symbols"] if s["name"] == "guarded")
        assert g["change"] in ("body", "signature")

    def test_signature_change(self):
        old = "def bar(x):\n    pass\n"
        new = "def bar(x, y=None):\n    pass\n"
        r = oi.changed_symbols("pkg/mod.py", old, new)
        bar = next((s for s in r["symbols"] if s["name"] == "bar"), None)
        assert bar is not None
        assert bar["change"] == "signature"
        assert bar["old_signature"] != bar["new_signature"]

    def test_removal(self):
        old = "def gone():\n    pass\n\ndef kept():\n    pass\n"
        new = "def kept():\n    pass\n"
        r = oi.changed_symbols("m.py", old, new)
        gone = next((s for s in r["symbols"] if s["name"] == "gone"), None)
        assert gone is not None
        assert gone["change"] in ("removed", "renamed")

    def test_rename_detection(self):
        old = "def old_name(x, y):\n    return x + y\n"
        new = "def new_name(x, y):\n    return x + y\n"
        r = oi.changed_symbols("m.py", old, new)
        old_sym = next((s for s in r["symbols"] if s["name"] == "old_name"), None)
        assert old_sym is not None
        assert old_sym["change"] == "renamed"
        assert old_sym["renamed_to"] == "new_name"

    def test_nested_function_maps_to_outer(self):
        old = textwrap.dedent("""\
            def outer(x):
                def inner():
                    return 1
                return inner()
        """)
        new = textwrap.dedent("""\
            def outer(x):
                def inner():
                    return 99
                return inner()
        """)
        r = oi.changed_symbols("m.py", old, new)
        # The change is inside inner() which is nested inside outer()
        # Should map to outer (the outermost)
        names = [s["name"] for s in r["symbols"]]
        assert "outer" in names

    def test_method_qualname(self):
        old = textwrap.dedent("""\
            class MyClass:
                def my_method(self):
                    return 1
        """)
        new = textwrap.dedent("""\
            class MyClass:
                def my_method(self):
                    return 2
        """)
        r = oi.changed_symbols("m.py", old, new)
        methods = [s for s in r["symbols"] if s.get("kind") == "method"]
        assert any(s["qualname"] == "MyClass.my_method" for s in methods)

    def test_module_level_change(self):
        old = "X = 1\n\ndef foo():\n    pass\n"
        new = "X = 2\n\ndef foo():\n    pass\n"
        r = oi.changed_symbols("pkg/mod.py", old, new)
        mod_syms = [s for s in r["symbols"] if s.get("kind") == "module"]
        assert mod_syms, f"expected module symbol, got {r['symbols']}"
        assert mod_syms[0]["change"] == "module"

    def test_syntax_error_falls_back_to_regex(self):
        # Deliberately invalid Python — must not raise
        old = "def foo(:\n    pass\n"
        new = "def foo(:\n    pass\n    x = 1\n"
        r = oi.changed_symbols("m.py", old, new)
        # Should not raise; may or may not find symbols, but warnings non-empty
        assert isinstance(r["warnings"], list)
        assert isinstance(r["symbols"], list)

    def test_added_file(self):
        new = "def brand_new():\n    return 42\n"
        r = oi.changed_symbols("m.py", None, new)
        added = [s for s in r["symbols"] if s["change"] == "added"]
        assert any(s["name"] == "brand_new" for s in added)

    def test_deleted_file(self):
        old = "def will_be_gone():\n    pass\n"
        r = oi.changed_symbols("m.py", old, None)
        names = [s["name"] for s in r["symbols"]]
        assert "will_be_gone" in names


# ===========================================================================
# changed_symbols — JS/TS
# ===========================================================================

class TestChangedSymbolsJS:
    def test_exported_function_body_change(self):
        old = "export function greet(name) {\n  return 'Hello ' + name;\n}\n"
        new = "export function greet(name) {\n  return `Hello ${name}`;\n}\n"
        r = oi.changed_symbols("utils.js", old, new)
        assert r["supported"] is True
        sym = next((s for s in r["symbols"] if s["name"] == "greet"), None)
        assert sym is not None
        assert sym["change"] in ("body", "signature")

    def test_arrow_const(self):
        old = "export const add = (a, b) => a + b;\n"
        new = "export const add = (a, b) => a - b;\n"
        r = oi.changed_symbols("math.ts", old, new)
        sym = next((s for s in r["symbols"] if s["name"] == "add"), None)
        assert sym is not None

    def test_class_method(self):
        old = textwrap.dedent("""\
            class Calc {
              multiply(a, b) {
                return a * b;
              }
            }
        """)
        new = textwrap.dedent("""\
            class Calc {
              multiply(a, b) {
                return a * b * 1;
              }
            }
        """)
        r = oi.changed_symbols("calc.js", old, new)
        sym = next((s for s in r["symbols"] if s["name"] == "multiply"), None)
        assert sym is not None
        assert "Calc" in sym.get("qualname", "")

    def test_object_literal_method(self):
        old = textwrap.dedent("""\
            const api = {
              fetch(url) {
                return null;
              }
            };
        """)
        new = textwrap.dedent("""\
            const api = {
              fetch(url) {
                return Promise.resolve();
              }
            };
        """)
        r = oi.changed_symbols("api.js", old, new)
        sym = next((s for s in r["symbols"] if s["name"] == "fetch"), None)
        assert sym is not None


# ===========================================================================
# changed_symbols — Go / Rust / Shell / unsupported
# ===========================================================================

class TestChangedSymbolsOtherLangs:
    def test_go_method_receiver_qualname(self):
        old = textwrap.dedent("""\
            func (s *Server) Start(port int) error {
                return nil
            }
        """)
        new = textwrap.dedent("""\
            func (s *Server) Start(port int) error {
                return fmt.Errorf("failed")
            }
        """)
        r = oi.changed_symbols("server.go", old, new)
        sym = next((s for s in r["symbols"] if s["name"] == "Start"), None)
        assert sym is not None
        assert "Server" in sym.get("qualname", "")

    def test_rust_impl_method(self):
        old = textwrap.dedent("""\
            impl MyStruct {
                pub fn process(&self) -> i32 {
                    0
                }
            }
        """)
        new = textwrap.dedent("""\
            impl MyStruct {
                pub fn process(&self) -> i32 {
                    1
                }
            }
        """)
        r = oi.changed_symbols("lib.rs", old, new)
        sym = next((s for s in r["symbols"] if s["name"] == "process"), None)
        assert sym is not None
        assert "MyStruct" in sym.get("qualname", "")

    def test_shell_function(self):
        old = "setup() {\n  echo old\n}\n"
        new = "setup() {\n  echo new\n}\n"
        r = oi.changed_symbols("install.sh", old, new)
        sym = next((s for s in r["symbols"] if s["name"] == "setup"), None)
        assert sym is not None

    def test_unsupported_extension_returns_supported_false(self):
        r = oi.changed_symbols("data.bin", "old content", "new content")
        assert r["supported"] is False
        assert r["symbols"] == []


# ===========================================================================
# find_references
# ===========================================================================

class TestFindReferences:
    def setup_repo(self, tmp_path):
        cwd = _init_repo(tmp_path)
        tip = _commit(cwd, {
            "pkg/__init__.py": "",
            "pkg/utils.py": "def helper():\n    return 1\n",
            "pkg/caller.py": "from pkg.utils import helper\n\ndef run():\n    return helper()\n",
            "other/consumer.py": "import pkg.utils\n\nx = pkg.utils.helper()\n",
            "unrelated.py": "def unrelated():\n    pass\n",
        })
        return cwd, tip

    def test_tier1_importer_found(self, tmp_path):
        cwd, tip = self.setup_repo(tmp_path)
        run = oi.git_runner(cwd)
        symbols = [{
            "name": "helper",
            "qualname": "helper",
            "kind": "function",
            "change": "body",
            "defined_in": "pkg/utils.py",
            "line": 1,
            "old_signature": "def helper():",
            "new_signature": "def helper():",
            "renamed_to": "",
            "lines_changed": 1,
        }]
        result = oi.find_references(run, tip, symbols)
        paths = [s["path"] for s in result["sites"]]
        # At least one caller should be found
        assert any("caller" in p or "consumer" in p for p in paths), \
            f"Expected caller or consumer in sites, got {paths}"

    def test_tier2_global_hit(self, tmp_path):
        cwd = _init_repo(tmp_path)
        tip = _commit(cwd, {
            "a/func.py": "def special_func():\n    return 42\n",
            "b/usage.py": "# uses special_func somewhere\nspecial_func()\n",
        })
        run = oi.git_runner(cwd)
        symbols = [{
            "name": "special_func",
            "qualname": "special_func",
            "kind": "function",
            "change": "body",
            "defined_in": "a/func.py",
            "line": 1,
            "old_signature": "",
            "new_signature": "",
            "renamed_to": "",
            "lines_changed": 1,
        }]
        result = oi.find_references(run, tip, symbols)
        paths = [s["path"] for s in result["sites"]]
        assert any("usage" in p for p in paths), f"Expected usage.py in sites, got {paths}"

    def test_defining_file_excluded(self, tmp_path):
        cwd, tip = self.setup_repo(tmp_path)
        run = oi.git_runner(cwd)
        symbols = [{
            "name": "helper",
            "qualname": "helper",
            "kind": "function",
            "change": "body",
            "defined_in": "pkg/utils.py",
            "line": 1,
            "old_signature": "",
            "new_signature": "",
            "renamed_to": "",
            "lines_changed": 1,
        }]
        result = oi.find_references(run, tip, symbols)
        # The defining file must not appear in sites
        assert not any(s["path"] == "pkg/utils.py" for s in result["sites"]), \
            "Defining file must be excluded from sites"

    def test_is_allowed_filter(self, tmp_path):
        cwd, tip = self.setup_repo(tmp_path)
        run = oi.git_runner(cwd)
        symbols = [{
            "name": "helper",
            "qualname": "helper",
            "kind": "function",
            "change": "body",
            "defined_in": "pkg/utils.py",
            "line": 1,
            "old_signature": "",
            "new_signature": "",
            "renamed_to": "",
            "lines_changed": 1,
        }]
        # Only allow files under 'other/'
        result = oi.find_references(run, tip, symbols,
                                    is_allowed=lambda p: p.startswith("other/"))
        for s in result["sites"]:
            assert s["path"].startswith("other/"), f"Non-other path leaked: {s['path']}"

    def test_path_containing_space(self, tmp_path):
        cwd = _init_repo(tmp_path)
        tip = _commit(cwd, {
            "lib/my func.py": "def spaced():\n    return 0\n",
            "caller.py": "from lib.my_func import spaced\nspaced()\n",
        })
        run = oi.git_runner(cwd)
        symbols = [{
            "name": "spaced",
            "qualname": "spaced",
            "kind": "function",
            "change": "body",
            "defined_in": "lib/my func.py",
            "line": 1,
            "old_signature": "",
            "new_signature": "",
            "renamed_to": "",
            "lines_changed": 1,
        }]
        # Should not raise even with a space in the path
        result = oi.find_references(run, tip, symbols)
        assert isinstance(result["sites"], list)

    def test_invalid_name_skipped(self, tmp_path):
        cwd = _init_repo(tmp_path)
        tip = _commit(cwd, {"a.py": "x = 1\n"})
        run = oi.git_runner(cwd)
        symbols = [{
            "name": "a;rm -rf",  # invalid identifier
            "qualname": "a;rm -rf",
            "kind": "function",
            "change": "body",
            "defined_in": "a.py",
            "line": 1,
            "old_signature": "",
            "new_signature": "",
            "renamed_to": "",
            "lines_changed": 1,
        }]
        result = oi.find_references(run, tip, symbols)
        # Must not raise; bad name is skipped
        assert isinstance(result["sites"], list)

    def test_ref_counts(self, tmp_path):
        cwd = _init_repo(tmp_path)
        tip = _commit(cwd, {
            "util.py": "def target():\n    pass\n",
            "a.py": "from util import target\ntarget()\n",
            "b.py": "from util import target\ntarget()\n",
        })
        run = oi.git_runner(cwd)
        symbols = [{
            "name": "target",
            "qualname": "target",
            "kind": "function",
            "change": "body",
            "defined_in": "util.py",
            "line": 1,
            "old_signature": "",
            "new_signature": "",
            "renamed_to": "",
            "lines_changed": 1,
        }]
        result = oi.find_references(run, tip, symbols)
        # ref_counts["target"] should be >= 2 (a.py and b.py)
        assert result["ref_counts"].get("target", 0) >= 2

    def test_returns_neutral_on_run_failure(self):
        def bad_run(args):
            return "", 1
        symbols = [{
            "name": "foo",
            "qualname": "foo",
            "kind": "function",
            "change": "body",
            "defined_in": "a.py",
            "line": 1,
            "old_signature": "",
            "new_signature": "",
            "renamed_to": "",
            "lines_changed": 1,
        }]
        result = oi.find_references(bad_run, "HEAD", symbols)
        assert result["sites"] == []
        assert isinstance(result["ref_counts"], dict)


# ===========================================================================
# find_references / find_siblings — is_context_allowed split (t01)
# ===========================================================================

def _helper_symbol(defined_in="pkg/utils.py"):
    return {
        "name": "helper",
        "qualname": "helper",
        "kind": "function",
        "change": "body",
        "defined_in": defined_in,
        "line": 1,
        "old_signature": "def helper():",
        "new_signature": "def helper():",
        "renamed_to": "",
        "lines_changed": 1,
    }


def _review_filter(path):
    """Mimics review-gate's review filter: test paths are NOT review targets."""
    norm = path.replace("\\", "/")
    return not ("tests/" in norm or ".spec." in norm or ".test." in norm
                or "/test_" in norm or "_test." in norm)


def _context_filter(path):
    """Context filter: test paths ARE searchable for callers and siblings."""
    return True


class TestContextFilterFindReferences:
    def setup_repo(self, tmp_path):
        cwd = _init_repo(tmp_path)
        tip = _commit(cwd, {
            "pkg/__init__.py": "",
            "pkg/utils.py": "def helper():\n    return 1\n",
            "tests/__init__.py": "",
            "tests/test_utils.py": (
                "from pkg.utils import helper\n\n"
                "def test_helper():\n    assert helper() == 1\n"
            ),
            "src/caller.py": "from pkg.utils import helper\n\nx = helper()\n",
        })
        return cwd, tip

    def test_spec_path_caller_collected_with_context_filter(self, tmp_path):
        cwd, tip = self.setup_repo(tmp_path)
        result = oi.find_references(oi.git_runner(cwd), tip, [_helper_symbol()],
                                    is_allowed=_review_filter,
                                    is_context_allowed=_context_filter)
        paths = {s["path"] for s in result["sites"]}
        assert "tests/test_utils.py" in paths, f"spec-path caller missing: {paths}"

    def test_context_filter_defaults_to_is_allowed(self, tmp_path):
        # Back-compat: with no context filter, is_allowed still gates everything.
        cwd, tip = self.setup_repo(tmp_path)
        result = oi.find_references(oi.git_runner(cwd), tip, [_helper_symbol()],
                                    is_allowed=_review_filter)
        paths = {s["path"] for s in result["sites"]}
        assert "tests/test_utils.py" not in paths, f"back-compat broken: {paths}"
        assert "src/caller.py" in paths, f"normal caller missing: {paths}"

    def test_ts_spec_caller_collected_with_context_filter(self, tmp_path):
        cwd = _init_repo(tmp_path)
        tip = _commit(cwd, {
            "app/util.ts": "export function render_widget() {\n  return 1;\n}\n",
            "tests/util.spec.ts": (
                "describe(\"util\", () => {\n"
                "  it(\"renders\", () => { render_widget(); });\n"
                "});\n"
            ),
        })
        syms = [dict(_helper_symbol("app/util.ts"), name="render_widget",
                     qualname="render_widget", old_signature="", new_signature="")]
        result = oi.find_references(oi.git_runner(cwd), tip, syms,
                                    is_allowed=_review_filter,
                                    is_context_allowed=_context_filter)
        paths = {s["path"] for s in result["sites"]}
        assert "tests/util.spec.ts" in paths, f"ts spec caller missing: {paths}"


class TestContextFilterFindSiblings:
    def setup_repo(self, tmp_path):
        cwd = _init_repo(tmp_path)
        pattern = "    result = dangerous_call(user_input, validate=False)"
        tip = _commit(cwd, {
            "a/vuln.py": f"def process(user_input):\n{pattern}\n    return result\n",
            "tests/test_handle.py": f"def test_handle(x):\n{pattern}\n    return x\n",
        })
        return cwd, tip, pattern.strip()

    def _finding(self, pattern):
        return {
            "id": "f1",
            "path": "a/vuln.py",
            "start_line": 2,
            "end_line": 2,
            "existing_code": f"    {pattern}",
            "severity": "high",
            "content": "Dangerous call without validation",
        }

    def test_spec_path_sibling_collected_with_context_filter(self, tmp_path):
        cwd, tip, pattern = self.setup_repo(tmp_path)
        result = oi.find_siblings(oi.git_runner(cwd), tip, self._finding(pattern),
                                  push_paths=set(), is_allowed=_review_filter,
                                  is_context_allowed=_context_filter)
        paths = {s["path"] for s in result["siblings"]}
        assert "tests/test_handle.py" in paths, f"spec-path sibling missing: {paths}"

    def test_context_filter_defaults_to_is_allowed(self, tmp_path):
        cwd, tip, pattern = self.setup_repo(tmp_path)
        result = oi.find_siblings(oi.git_runner(cwd), tip, self._finding(pattern),
                                  push_paths=set(), is_allowed=_review_filter)
        paths = {s["path"] for s in result["siblings"]}
        assert "tests/test_handle.py" not in paths, f"back-compat broken: {paths}"


# ===========================================================================
# build_bundle
# ===========================================================================

class TestBuildBundle:
    def _make_sites(self, names, per=3, paths=None):
        sites = []
        for i, name in enumerate(names):
            for j in range(per):
                p = (paths[i] if paths else f"file{i}.py")
                sites.append({
                    "id": f"id_{name}_{j}",
                    "path": p if not paths else f"caller_{j}.py",
                    "line": j + 10,
                    "name": name,
                    "tier": 1,
                    "text": f"    {name}(arg{j})",
                })
        return sites

    def test_round_robin_no_symbol_starved(self, tmp_path):
        """A symbol with 100 callers must not starve a symbol with 2 callers."""
        cwd = _init_repo(tmp_path)
        tip = _commit(cwd, {
            "a.py": "x = 1\n",
            "b.py": "y = 2\n",
        })
        run = oi.git_runner(cwd)

        def make_sym(name, change="body"):
            return {
                "name": name, "qualname": name, "kind": "function",
                "change": change, "defined_in": "a.py",
                "line": 1, "old_signature": "", "new_signature": "",
                "renamed_to": "", "lines_changed": 1,
            }

        syms = [make_sym("rare"), make_sym("common")]
        ref_counts = {"rare": 2, "common": 100}

        # 10 sites for rare, 100 for common
        sites = []
        for i in range(10):
            sites.append({"id": f"r{i}", "path": f"r{i}.py", "line": 1,
                          "name": "rare", "tier": 1, "text": "rare()"})
        for i in range(100):
            sites.append({"id": f"c{i}", "path": f"c{i}.py", "line": 1,
                          "name": "common", "tier": 1, "text": "common()"})

        result = oi.build_bundle(run, tip, syms, sites, ref_counts,
                                  max_sites=12, per_symbol=5, max_bytes=999999)
        names_in = {s["name"] for s in result["sites"]}
        assert "rare" in names_in, "rare symbol was starved"
        assert "common" in names_in

    def test_fewest_refs_ranked_first(self, tmp_path):
        cwd = _init_repo(tmp_path)
        tip = _commit(cwd, {"x.py": "pass\n"})
        run = oi.git_runner(cwd)

        syms = [
            {"name": "high_ref", "qualname": "high_ref", "kind": "function",
             "change": "body", "defined_in": "x.py", "line": 1,
             "old_signature": "", "new_signature": "", "renamed_to": "", "lines_changed": 1},
            {"name": "low_ref", "qualname": "low_ref", "kind": "function",
             "change": "body", "defined_in": "x.py", "line": 2,
             "old_signature": "", "new_signature": "", "renamed_to": "", "lines_changed": 1},
        ]
        ref_counts = {"high_ref": 400, "low_ref": 2}
        sites = [
            {"id": "s1", "path": "a.py", "line": 1, "name": "low_ref", "tier": 1, "text": "low_ref()"},
            {"id": "s2", "path": "b.py", "line": 1, "name": "high_ref", "tier": 1, "text": "high_ref()"},
        ]
        result = oi.build_bundle(run, tip, syms, sites, ref_counts,
                                  max_sites=1, per_symbol=1, max_bytes=999999)
        # With max_sites=1, low_ref (fewer refs) should get the first slot
        assert len(result["sites"]) >= 1
        assert result["sites"][0]["name"] == "low_ref"

    def test_max_bytes_sets_truncated(self, tmp_path):
        cwd = _init_repo(tmp_path)
        big_line = "x" * 300 + "\n"
        tip = _commit(cwd, {
            "target.py": "def fn():\n    pass\n",
            "big_caller.py": big_line * 50,
        })
        run = oi.git_runner(cwd)
        sym = {"name": "fn", "qualname": "fn", "kind": "function", "change": "body",
               "defined_in": "target.py", "line": 1,
               "old_signature": "", "new_signature": "", "renamed_to": "", "lines_changed": 1}
        sites = [{"id": f"s{i}", "path": "big_caller.py", "line": i + 1,
                  "name": "fn", "tier": 1, "text": "fn()"}
                 for i in range(20)]
        result = oi.build_bundle(run, tip, [sym], sites, {"fn": 20},
                                  max_bytes=100, per_symbol=20, max_sites=20)
        assert result["truncated"] is True

    def test_max_sites_cap_and_dropped_symbols(self, tmp_path):
        cwd = _init_repo(tmp_path)
        tip = _commit(cwd, {"x.py": "pass\n"})
        run = oi.git_runner(cwd)
        syms = [
            {"name": f"fn{i}", "qualname": f"fn{i}", "kind": "function",
             "change": "body", "defined_in": "x.py", "line": i + 1,
             "old_signature": "", "new_signature": "", "renamed_to": "", "lines_changed": 1}
            for i in range(10)
        ]
        sites = [
            {"id": f"s{i}", "path": f"c{i}.py", "line": 1, "name": f"fn{i}",
             "tier": 1, "text": f"fn{i}()"}
            for i in range(10)
        ]
        ref_counts = {f"fn{i}": 1 for i in range(10)}
        result = oi.build_bundle(run, tip, syms, sites, ref_counts,
                                  max_symbols=3, max_sites=3, per_symbol=1, max_bytes=999999)
        assert len(result["symbols"]) <= 3
        assert result["truncated"] is True

    def test_line_truncation(self, tmp_path):
        """A 20 KB single line must be truncated to line_cap chars."""
        cwd = _init_repo(tmp_path)
        huge_line = "a" * 20000
        tip = _commit(cwd, {
            "big.py": f"def fn():\n    x = '{huge_line}'\n",
            "caller.py": "fn()\n",
        })
        run = oi.git_runner(cwd)
        sym = {"name": "fn", "qualname": "fn", "kind": "function", "change": "body",
               "defined_in": "big.py", "line": 1,
               "old_signature": "", "new_signature": "", "renamed_to": "", "lines_changed": 1}
        sites = [{"id": "s1", "path": "caller.py", "line": 1,
                  "name": "fn", "tier": 1, "text": "fn()"}]
        result = oi.build_bundle(run, tip, [sym], sites, {"fn": 1},
                                  max_bytes=999999, per_symbol=5, max_sites=5, line_cap=200)
        for site in result["sites"]:
            for snip_line in site["snippet"].splitlines():
                # Strip the line number prefix "N: " before measuring
                content = snip_line.split(": ", 1)[-1] if ": " in snip_line else snip_line
                assert len(content) <= 201, f"Line not truncated: {len(content)}"

    def test_include_exclude_paths(self, tmp_path):
        cwd = _init_repo(tmp_path)
        tip = _commit(cwd, {"x.py": "pass\n"})
        run = oi.git_runner(cwd)
        sym_a = {"name": "fn_a", "qualname": "fn_a", "kind": "function",
                 "change": "body", "defined_in": "chunk_a.py", "line": 1,
                 "old_signature": "", "new_signature": "", "renamed_to": "", "lines_changed": 1}
        sym_b = {"name": "fn_b", "qualname": "fn_b", "kind": "function",
                 "change": "body", "defined_in": "chunk_b.py", "line": 1,
                 "old_signature": "", "new_signature": "", "renamed_to": "", "lines_changed": 1}
        sites = [
            {"id": "sa", "path": "outside.py", "line": 1, "name": "fn_a", "tier": 1, "text": "fn_a()"},
            {"id": "sb", "path": "chunk_b.py", "line": 1, "name": "fn_a", "tier": 1, "text": "fn_a()"},
        ]
        # Only include chunk_a, exclude chunk_a sites (reviewer sees diff)
        result = oi.build_bundle(run, tip, [sym_a, sym_b], sites, {"fn_a": 1, "fn_b": 0},
                                  include_paths={"chunk_a.py"},
                                  exclude_paths=("chunk_a.py",),
                                  max_bytes=999999, per_symbol=5, max_sites=5)
        # sym_b not included (not in include_paths)
        assert not any(s["name"] == "fn_b" for s in result["symbols"])
        # chunk_a.py excluded from sites
        assert not any(s["path"] == "chunk_a.py" for s in result["sites"])

    def test_no_exception_on_run_failure(self):
        def bad_run(args):
            raise RuntimeError("boom")
        sym = {"name": "fn", "qualname": "fn", "kind": "function", "change": "body",
               "defined_in": "a.py", "line": 1,
               "old_signature": "", "new_signature": "", "renamed_to": "", "lines_changed": 1}
        result = oi.build_bundle(bad_run, "HEAD", [sym], [], {"fn": 0})
        assert isinstance(result, dict)


# ===========================================================================
# signature_line and find_siblings
# ===========================================================================

class TestSignatureLine:
    def test_returns_longest_non_boring_line(self):
        code = textwrap.dedent("""\
            if condition:
                result = some_function_call(arg1, arg2, important_param=True)
                return result
        """)
        sig = oi.signature_line(code)
        assert "some_function_call" in sig

    def test_rejects_short_lines(self):
        sig = oi.signature_line("x = 1\n")
        assert sig == ""

    def test_rejects_boring_return(self):
        sig = oi.signature_line("return None\n")
        assert sig == ""

    def test_keeps_keyword_led_lines_with_real_code(self):
        # The defect line itself often starts with a keyword.
        assert oi.signature_line("return eval(req.args['expr'])") == "return eval(req.args['expr'])"
        assert oi.signature_line("raise ValueError(f'bad token {tok}')") != ""

    def test_rejects_empty(self):
        assert oi.signature_line("") == ""
        assert oi.signature_line(None) == ""  # type: ignore

    def test_rejects_only_punctuation(self):
        sig = oi.signature_line("} else {\n")
        assert sig == ""


class TestFindSiblings:
    def setup_repo(self, tmp_path):
        cwd = _init_repo(tmp_path)
        pattern = "    result = dangerous_call(user_input, validate=False)"
        tip = _commit(cwd, {
            "a/vuln.py": f"def process(user_input):\n{pattern}\n    return result\n",
            "b/other.py": f"def handle(x):\n{pattern}\n    return x\n",
            "push_file.py": f"def new_func():\n{pattern}\n    pass\n",
        })
        return cwd, tip, pattern.strip()

    def test_sibling_found_in_non_push_file(self, tmp_path):
        cwd, tip, pattern = self.setup_repo(tmp_path)
        run = oi.git_runner(cwd)
        finding = {
            "id": "f1",
            "path": "a/vuln.py",
            "start_line": 2,
            "end_line": 2,
            "existing_code": f"    {pattern}",
            "severity": "high",
            "content": "Dangerous call without validation",
        }
        result = oi.find_siblings(run, tip, finding,
                                   push_paths={"push_file.py"})
        paths = [s["path"] for s in result["siblings"]]
        assert any("other" in p for p in paths), f"Expected other.py, got {paths}"

    def test_in_push_siblings_come_first(self, tmp_path):
        cwd, tip, pattern = self.setup_repo(tmp_path)
        run = oi.git_runner(cwd)
        finding = {
            "id": "f1",
            "path": "a/vuln.py",
            "start_line": 2,
            "end_line": 2,
            "existing_code": f"    {pattern}",
            "severity": "high",
            "content": "Dangerous call",
        }
        result = oi.find_siblings(run, tip, finding,
                                   push_paths={"push_file.py"})
        if len(result["siblings"]) > 1:
            # In-push entries should come before non-push entries
            first_in_push = next((i for i, s in enumerate(result["siblings"]) if s["in_push"]), None)
            first_not_in_push = next((i for i, s in enumerate(result["siblings"]) if not s["in_push"]), None)
            if first_in_push is not None and first_not_in_push is not None:
                assert first_in_push < first_not_in_push

    def test_own_location_excluded(self, tmp_path):
        cwd, tip, pattern = self.setup_repo(tmp_path)
        run = oi.git_runner(cwd)
        finding = {
            "id": "f1",
            "path": "a/vuln.py",
            "start_line": 2,
            "end_line": 2,
            "existing_code": f"    {pattern}",
            "severity": "high",
            "content": "Issue",
        }
        result = oi.find_siblings(run, tip, finding, push_paths=set())
        for s in result["siblings"]:
            if s["path"] == "a/vuln.py":
                assert not (2 - 3 <= s["line"] <= 2 + 3), \
                    "Own location within exclusion window must be excluded"

    def test_empty_signature_line_returns_no_siblings(self, tmp_path):
        cwd = _init_repo(tmp_path)
        tip = _commit(cwd, {"x.py": "x = 1\n"})
        run = oi.git_runner(cwd)
        finding = {
            "id": "f1",
            "path": "x.py",
            "start_line": 1,
            "end_line": 1,
            "existing_code": "return None",  # boring — signature_line returns ""
            "severity": "low",
            "content": "Issue",
        }
        result = oi.find_siblings(run, tip, finding, push_paths=set())
        assert result["siblings"] == []

    def test_no_exception_on_run_failure(self):
        def bad_run(args):
            return "", 1
        finding = {
            "id": "f1",
            "path": "x.py",
            "start_line": 1,
            "end_line": 1,
            "existing_code": "    result = some_important_function_call(x, y, z=True)",
            "severity": "high",
            "content": "Issue",
        }
        result = oi.find_siblings(bad_run, "HEAD", finding, push_paths=set())
        assert result["siblings"] == []
        assert isinstance(result["warnings"], list)


# ===========================================================================
# git_runner
# ===========================================================================

class TestGitRunner:
    def test_preserves_leading_whitespace(self, tmp_path):
        cwd = _init_repo(tmp_path)
        _commit(cwd, {"f.py": "    indented = True\n"})
        run = oi.git_runner(cwd)
        out, rc = run(["show", "HEAD:f.py"])
        # Leading spaces must be preserved
        assert "    indented" in out

    def test_returns_empty_and_rc1_on_error(self, tmp_path):
        run = oi.git_runner(str(tmp_path))
        out, rc = run(["rev-parse", "HEAD"])
        # In a non-repo or bad state, must not raise
        assert isinstance(out, str)
        assert isinstance(rc, int)

    def test_never_raises_on_invalid_args(self, tmp_path):
        run = oi.git_runner(str(tmp_path))
        out, rc = run(["this-command-does-not-exist"])
        assert isinstance(out, str)
        assert rc != 0


# ---------------------------------------------------------------------------
# 0.9.1 regressions
# ---------------------------------------------------------------------------

class TestRegexBlockEnd:
    """A block ends at the first line back at its own indentation. Scanning on
    to the next definition credited top-level code to the function above."""

    def _defs(self, text, lang):
        return {d["name"]: d for d in oi._regex_defs(text, lang)}

    def test_top_level_code_after_a_function_is_not_inside_it(self):
        text = textwrap.dedent("""\
            function a() {
              return 1;
            }
            const LIMIT = 10;
            doSetup(LIMIT);
            function b() {
              return 2;
            }
        """)
        d = self._defs(text, "js")
        assert d["a"]["end_line"] == 3  # the closing brace, not line 5
        old, new = text, text.replace("doSetup(LIMIT)", "doSetup(LIMIT * 2)")
        r = oi.changed_symbols("app.js", old, new)
        assert "a" not in {s["name"] for s in r["symbols"]}

    def test_closing_keyword_belongs_to_the_block(self):
        text = "def greet\n  puts 'hi'\nend\nputs 'top'\n"
        assert self._defs(text, "ruby")["greet"]["end_line"] == 3
        sh = "setup() {\n  echo hi\n}\necho top\n"
        assert self._defs(sh, "shell")["setup"]["end_line"] == 3

    def test_allman_brace_and_wrapped_params_stay_in_the_header(self):
        allman = "function a()\n{\n  return 1;\n}\nx = 1;\n"
        assert self._defs(allman, "js")["a"]["end_line"] == 4
        go = "func Run(\n\tx int,\n) error {\n\treturn nil\n}\nvar y = 1\n"
        assert self._defs(go, "go")["Run"]["end_line"] == 5


class TestSameNamedSymbols:
    """Two changed symbols sharing a name are different symbols: separate
    pools, cursors and quotas, and a caller an importer search tied to one of
    them is never handed to the other."""

    def _sym(self, name, defined_in, qual=None):
        return {"name": name, "qualname": qual or name, "kind": "function",
                "change": "body", "defined_in": defined_in, "line": 1,
                "old_signature": "", "new_signature": "", "renamed_to": "",
                "lines_changed": 1}

    def test_each_definition_gets_its_own_quota_and_its_own_callers(self, tmp_path):
        cwd = _init_repo(tmp_path)
        tip = _commit(cwd, {"x.py": "pass\n"})
        run = oi.git_runner(cwd)
        syms = [self._sym("run", "a.py"), self._sym("run", "b.py")]
        sites = ([{"id": f"a{i}", "path": f"ua{i}.py", "line": 1, "name": "run",
                   "tier": 1, "text": "run()", "via": ["a.py"]} for i in range(5)]
                 + [{"id": f"b{i}", "path": f"ub{i}.py", "line": 1, "name": "run",
                     "tier": 1, "text": "run()", "via": ["b.py"]} for i in range(5)])
        result = oi.build_bundle(run, tip, syms, sites, {"run": 10},
                                 per_symbol=3, max_sites=40, max_bytes=999999)
        by_def = {}
        for s in result["sites"]:
            by_def.setdefault(s["defined_in"], set()).add(s["id"][0])
        # Each definition got its own 3, drawn only from its own importers.
        assert by_def == {"a.py": {"a"}, "b.py": {"b"}}
        assert len(result["sites"]) == 6
        assert [s["sites_included"] for s in result["symbols"]] == [3, 3]
        assert not any(s.get("ambiguous") for s in result["sites"])

    def test_a_shared_untied_site_goes_out_once_and_is_marked_ambiguous(self, tmp_path):
        cwd = _init_repo(tmp_path)
        tip = _commit(cwd, {"x.py": "pass\n"})
        run = oi.git_runner(cwd)
        syms = [self._sym("run", "a.py"), self._sym("run", "b.py")]
        sites = [{"id": "g1", "path": "u.py", "line": 3, "name": "run", "tier": 2,
                  "text": "run()", "via": []}]
        result = oi.build_bundle(run, tip, syms, sites, {"run": 1}, max_bytes=999999)
        assert [s["id"] for s in result["sites"]] == ["g1"]
        assert result["sites"][0]["ambiguous"] is True

    def test_find_references_records_which_definition_an_importer_belongs_to(self, tmp_path):
        cwd = _init_repo(tmp_path)
        tip = _commit(cwd, {
            "pkg/a.py": "def run():\n    pass\n",
            "pkg/b.py": "def run():\n    pass\n",
            "use_a.py": "from pkg.a import run\nrun()\n",
            "use_b.py": "from pkg.b import run\nrun()\n",
        })
        run_git = oi.git_runner(cwd)
        syms = [self._sym("run", "pkg/a.py"), self._sym("run", "pkg/b.py")]
        out = oi.find_references(run_git, tip, syms)
        via = {(s["path"], s["line"]): s["via"] for s in out["sites"]}
        assert via[("use_a.py", 2)] == ["pkg/a.py"]
        assert via[("use_b.py", 2)] == ["pkg/b.py"]
        # Neither defining file is reported as a caller of the other's `run`.
        assert not {"pkg/a.py", "pkg/b.py"} & {s["path"] for s in out["sites"]}


# ===========================================================================
# Optional-param signature classification (t02 characterization)
# ===========================================================================

class TestOptionalParamClassification:
    """Adding an *optional* parameter to an existing def changes the contract,
    so it must classify as "signature" (caller-collecting) whether the def
    header is written on one line or wrapped across several."""

    def _change_of(self, old, new, name="shared_helper"):
        r = oi.changed_symbols("pkg/utils.py", old, new)
        sym = next((s for s in r["symbols"] if s["name"] == name), None)
        assert sym is not None, f"symbol {name} not found in {r['symbols']}"
        return sym["change"]

    def test_single_line_def_optional_param_is_signature(self):
        old = "def shared_helper(x):\n    return x\n"
        new = "def shared_helper(x, strict=False):\n    return x\n"
        assert self._change_of(old, new) == "signature"

    def test_multiline_def_optional_param_is_signature(self):
        # New param lands on a CONTINUATION line; the first physical def line
        # is unchanged, but the full signature still changed.
        old = "def shared_helper(\n    x,\n):\n    return x\n"
        new = "def shared_helper(\n    x,\n    strict=False,\n):\n    return x\n"
        assert self._change_of(old, new) == "signature"

    def test_multiline_def_body_only_change_stays_body(self):
        old = "def shared_helper(\n    x,\n):\n    return x\n"
        new = "def shared_helper(\n    x,\n):\n    return str(x)\n"
        assert self._change_of(old, new) == "body"

    def test_multiline_method_optional_param_is_signature(self):
        old = (
            "class C:\n"
            "    def render(\n"
            "        self,\n"
            "        theme,\n"
            "    ):\n"
            "        return theme\n"
        )
        new = (
            "class C:\n"
            "    def render(\n"
            "        self,\n"
            "        theme,\n"
            "        dark=False,\n"
            "    ):\n"
            "        return theme\n"
        )
        assert self._change_of(old, new, name="render") == "signature"


# ===========================================================================
# Bundle capacity: shared helpers with many callers (t02)
# ===========================================================================

class TestBuildBundleCaps:
    """Default caps must collect all callers of a shared helper: an 11-caller
    helper needs per_symbol >= 11 with headroom (>= 12), and max_sites must
    stay coherent when several symbols each bring their full caller set."""

    def _sym(self, name, defined_in="pkg/utils.py"):
        return {
            "name": name, "qualname": name, "kind": "function",
            "change": "signature", "defined_in": defined_in,
            "line": 1, "old_signature": "", "new_signature": "",
            "renamed_to": "", "lines_changed": 1,
        }

    def _sites(self, name, specs):
        # specs: list of (path, line) call-site locations.
        return [{"id": f"{name}_{i}", "path": p, "line": ln, "name": name,
                 "tier": 1, "text": f"result = {name}()"}
                for i, (p, ln) in enumerate(specs)]

    def _repo_with(self, tmp_path, files):
        cwd = _init_repo(tmp_path)
        tip = _commit(cwd, files)
        return oi.git_runner(cwd), tip

    def test_default_per_symbol_collects_twelve_callers(self, tmp_path):
        # 12 callers in 12 distinct files: one per-file slot each, so the
        # binding constraint is per_symbol, which must be >= 12.
        files = {f"callers/c{i}.py": "result = helper()\n" for i in range(12)}
        run, tip = self._repo_with(tmp_path, files)
        specs = [(f"callers/c{i}.py", 1) for i in range(12)]
        result = oi.build_bundle(run, tip, [self._sym("helper")],
                                 self._sites("helper", specs), {"helper": 12})
        got = {(s["path"], s["line"]) for s in result["sites"]}
        assert got == set(specs), f"expected 12 caller sites, got {len(got)}"

    def test_two_per_file_cap_keeps_snippet_diversity(self, tmp_path):
        # Characterization of the deliberate per-(symbol, file) cap: 3 caller
        # lines in ONE file still yield at most 2 sites, so the bundle shows
        # diverse files rather than every line of one caller.
        run, tip = self._repo_with(tmp_path, {
            "one_caller.py": "a = helper()\nb = helper()\nc = helper()\n",
        })
        specs = [("one_caller.py", 1), ("one_caller.py", 2), ("one_caller.py", 3)]
        result = oi.build_bundle(run, tip, [self._sym("helper")],
                                 self._sites("helper", specs), {"helper": 3})
        assert len(result["sites"]) == 2

    def test_default_max_sites_fits_several_fully_covered_symbols(self, tmp_path):
        # 4 symbols x 12 callers in distinct files = 48 sites; the default
        # max_sites must not strand any of them (it must exceed one full
        # per-symbol budget by a sane multiple).
        names = [f"fn{i}" for i in range(4)]
        files = {}
        syms = []
        all_sites = []
        ref_counts = {}
        for s_i, name in enumerate(names):
            files[f"pkg/{name}.py"] = f"def {name}():\n    pass\n"
            syms.append(self._sym(name, defined_in=f"pkg/{name}.py"))
            specs = []
            for i in range(12):
                path = f"callers/{name}_c{i}.py"
                files[path] = f"result = {name}()\n"
                specs.append((path, 1))
            all_sites += self._sites(name, specs)
            ref_counts[name] = 12
        run, tip = self._repo_with(tmp_path, files)
        result = oi.build_bundle(run, tip, syms, all_sites, ref_counts)
        assert result["counts"]["sites_included"] == 48, \
            f"expected 48 sites, got {result['counts']['sites_included']}"


# ===========================================================================
# t02 acceptance: optional param on a shared helper, 11 callers in 8 spec files
# ===========================================================================

class TestElevenCallersAcceptance:
    """End-to-end over the ocr_impact pipeline: adding an optional parameter
    to a multi-line-wrapped helper def must classify as "signature" and the
    default caps must collect every one of its 11 spec-file caller lines."""

    _HELPER_OLD = (
        "def helper(\n"
        "    x,\n"
        "):\n"
        "    return x\n"
    )
    _HELPER_NEW = (
        "def helper(\n"
        "    x,\n"
        "    strict=False,\n"
        "):\n"
        "    return x if x is not None else (0 if strict else None)\n"
    )

    def _caller_file(self, tag, n_calls):
        lines = ["import pkg.utils", ""]
        for i in range(1, n_calls + 1):
            lines += [
                f"def test_{tag}_{i}():",
                f"    assert pkg.utils.helper({i}) == {i}",
                "",
            ]
        return "\n".join(lines)

    def test_signature_change_collects_all_eleven_callers(self, tmp_path):
        cwd = _init_repo(tmp_path)
        files = {
            "pkg/__init__.py": "",
            "pkg/utils.py": self._HELPER_NEW,
        }
        # 11 caller lines over 8 spec files: 3 files x 2 calls + 5 files x 1.
        expected_sites = set()
        for idx in range(8):
            tag = chr(ord("a") + idx)
            n_calls = 2 if idx < 3 else 1
            path = f"tests/test_{tag}.py"
            files[path] = self._caller_file(tag, n_calls)
            for ln, text in enumerate(files[path].splitlines(), start=1):
                if "helper(" in text:
                    expected_sites.add((path, ln))
        assert len(expected_sites) == 11  # guard the fixture itself
        tip = _commit(cwd, files)
        run = oi.git_runner(cwd)

        # 1. The signature change classification
        got = oi.changed_symbols("pkg/utils.py", self._HELPER_OLD, self._HELPER_NEW)
        helper = next((s for s in got["symbols"] if s["name"] == "helper"), None)
        assert helper is not None
        assert helper["change"] == "signature", \
            "multi-line wrapped def with new optional param must be 'signature'"

        # 2. Reference search with the production wiring (review filter +
        #    context filter), then bundling under DEFAULT caps.
        refs = oi.find_references(run, tip, got["symbols"],
                                  is_allowed=_review_filter,
                                  is_context_allowed=_context_filter)
        bundle = oi.build_bundle(run, tip, got["symbols"], refs["sites"],
                                 refs["ref_counts"])

        caller_sites = {(s["path"], s["line"]) for s in bundle["sites"]
                        if s["path"].startswith("tests/")}
        assert caller_sites == expected_sites, \
            f"missing callers: {sorted(expected_sites - caller_sites)}"
