"""Kimchi helper allowlist: the review session may run the review-gate's own
conventions / demands / collector scripts and nothing else in python.

Seam: the gate's own `_helper_cmds_field()` under OCR_BACKEND=kimchi (the field
the review manifest carries, read in-process: the golden single-context push
writes no manifest file); the exact helper commands it publishes
(`manifest.helper_cmds`) are then put through the REAL deny extension
(extensions/review-gate-deny.ts, loaded by tests/kimchi_deny_driver.mjs the way
`kimchi -e` loads it) alongside the commands that must stay denied. Assertions
are the extension's allow/deny decisions only. Needs node >= 24 (skipped when
absent).
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from test_claude_profile import clean, rg, tiers  # noqa: E402,F401

_DRIVER = os.path.join(_HERE, "kimchi_deny_driver.mjs")
_NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(_NODE is None, reason="node (>= 24) not installed")


def _decisions(commands):
    proc = subprocess.run([_NODE, _DRIVER], input=json.dumps(commands), capture_output=True,
                          text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert len(out) == len(commands)
    return dict(zip(commands, out))


@pytest.fixture
def helper_cmds(tiers):
    """manifest.helper_cmds the gate publishes for a kimchi review."""
    tiers.setenv("OCR_BACKEND", "kimchi")
    return rg._helper_cmds_field()["helper_cmds"]


def test_the_manifest_publishes_the_three_exact_helper_commands(helper_cmds):
    assert set(helper_cmds) == {"conventions", "demands", "collectors"}
    for key, script in (("conventions", "ocr_conventions.py"), ("demands", "ocr_demands.py"),
                        ("collectors", "ocr_collectors.py")):
        assert helper_cmds[key].split()[0] in ("python", "python3")
        assert helper_cmds[key].rstrip('"').endswith("/scripts/" + script)


def test_the_exact_helper_commands_pass_the_deny_extension(helper_cmds):
    conv, dem, col = helper_cmds["conventions"], helper_cmds["demands"], helper_cmds["collectors"]
    body = '{"demands": {"risk": {"score": 1}}}'
    allowed = [
        f"{conv} --repo-root . src/a.py src/b.py",
        f'{conv} --repo-root "/some where/repo" --max-doc-lines 50 --max-total-lines 200 a.py',
        f"{col} --repo-root . --changed-file a.py --changed-file b.py",
        f"{col} --repo-root . --tip HEAD --max-chars 4000 --changed-file a.py",
        f"{col} --validate-bundle",
        dem,
        f"{dem} 2>&1",
        f"{dem} <<'DEMANDS_EOF'\n{body}\nDEMANDS_EOF",
        f"{col} --repo-root . --changed-file a.py <<'EOF'\n{body}\nEOF\n",
    ]
    got = _decisions(allowed)
    assert [c for c, d in got.items() if d["blocked"]] == []


def test_arbitrary_python_and_other_scripts_stay_denied(helper_cmds):
    conv, dem = helper_cmds["conventions"], helper_cmds["demands"]
    root = conv.split()[-1].strip('"').rsplit("/scripts/", 1)[0]
    denied = [
        "python -c 'print(1)'",
        'python3 -c "import os; os.system(\'id\')"',
        "python3 -m http.server",
        "python3 -I -c 'print(1)'",
        "python3 /tmp/evil.py",
        "python3 evil.py",
        f"python3 {root}/scripts/review-gate.py --print-range",
        f"python3 {root}/scripts/rg_settings_cli.py",
        f"python3 {root}/scripts/ocr_forge.py",
        "py -c 'print(1)'",
        "bash -c 'id'",
    ]
    got = _decisions(denied)
    assert [c for c, d in got.items() if not d["blocked"]] == []
    assert "arbitrary code" in got[denied[0]]["reason"]


def test_a_crafted_path_or_argument_on_an_allowlisted_command_is_denied(helper_cmds):
    conv, dem, col = helper_cmds["conventions"], helper_cmds["demands"], helper_cmds["collectors"]
    conv_bare = conv.replace('"', "")          # the script path, unquoted (no spaces in CI roots)
    denied = [
        # a path that only starts with / resolves to / climbs out of the helper path
        conv_bare + ".bak --repo-root .",
        conv_bare + "x --repo-root .",
        conv_bare + "c --repo-root .",
        conv_bare + "/../ocr_conventions.py --repo-root .",
        conv_bare.replace("/scripts/", "/scripts/../scripts/") + " --repo-root .",
        conv_bare.replace("ocr_conventions.py", "ocr_forge.py"),
        # the demands validator takes no arguments (--dump-schema PATH writes a file)
        dem + " --dump-schema /tmp/out.json",
        dem + " --dump-schema",
        dem + " extra",
        # flags outside each helper's grammar, abbreviations, =-forms, `--`
        conv + " --repo-root . -c 'x'",
        conv + " --repo-root=. a.py",
        conv + " --repo-roo . a.py",
        conv + " --repo-root -x a.py",
        conv + " --max-doc-lines abc a.py",
        conv + " --repo-root . -- -rf",
        conv + " --repo-root",
        col + " --dump-schema /tmp/out.json",
        col + " --repo-root . --changed-file",
        col + " --repo-root . stray-positional",
        # chaining, substitution, redirection, env and interpreter-flag tricks
        conv + " --repo-root . ; python3 -c 'x'",
        conv + " --repo-root . && rm -rf /",
        conv + " --repo-root . | tee out.txt",
        conv + " --repo-root . > out.txt",
        conv + " --repo-root $(id)",
        conv + " --repo-root `id`",
        "PYTHONPATH=/tmp " + conv + " --repo-root .",
        conv.replace("python", "python -I", 1) + " --repo-root .",
        conv.replace("python3 ", "python3 -c 'x' ", 1).replace("python ", "python -c 'x' ", 1),
        # heredocs: only a quoted one on an exact helper, ending the command
        f"{dem} <<EOF\nx\nEOF",
        f"{dem} <<-'EOF'\nx\nEOF",
        f"{dem} <<'EOF'\nx\nEOF\nrm -rf /",
        f"{dem} <<'EOF'\nx\nEOF\nrm -rf /\nEOF",
        f"{dem} <<'EOF'\nx",
        f"{dem} --dump-schema /tmp/x <<'EOF'\nx\nEOF",
        "python3 -c 'import os' <<'EOF'\nx\nEOF",
        f"python3 {conv_bare.rsplit('/', 1)[0]}/review-gate.py <<'EOF'\nx\nEOF",
        f"{dem} <<'A' <<'B'\nx\nA\nB",
    ]
    got = _decisions(denied)
    assert [c for c, d in got.items() if not d["blocked"]] == []


def test_the_rest_of_the_sandbox_is_unchanged(helper_cmds):
    got = _decisions(["git log --oneline -3", "cat a.py", "curl http://x", "node -e 1",
                      "git push", "rm -rf /", "echo hi > f"])
    assert [c for c, d in got.items() if d["blocked"]] == [
        "curl http://x", "node -e 1", "git push", "rm -rf /", "echo hi > f"]
