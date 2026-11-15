"""0.7.0 argv golden fixture.

_run_review builds a specific argv for a ≤15-file first push (no --paths-file).
This test captures that structure so scenarios 13 and 20 can verify 0.8.0 does not
add extra arguments in those cases.

Normalisation (documented):
  - Claude executable path  → '<claude>'
  - Plugin root (_PLUGIN_ROOT) → '<plugin-dir>'
  - Deny extension path (_DENY_EXT) → '<deny-ext>'
  - Push range in the prompt  → '<RANGE>'
"""
import importlib.util
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
sys.path.insert(0, _SCRIPTS)
_spec = importlib.util.spec_from_file_location("review_gate", os.path.join(_SCRIPTS, "review-gate.py"))
review_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(review_gate)

_FIXTURE = os.path.join(_HERE, "fixtures", "argv_golden.json")
_PLACEHOLDER_RANGE = "A" * 40 + ".." + "B" * 40


def _build_normalized_argv(push_range=_PLACEHOLDER_RANGE, model=None):
    """Build the non-stub argv _run_review would use and normalise machine parts."""
    prompt =review_gate.PROMPT_RANGE.format(rng=push_range)
    # Rebuild DEFAULT_CLAUDE_ARGS with the requested model if overridden.
    if model is not None and model != review_gate._MODEL:
        args = list(review_gate.DEFAULT_CLAUDE_ARGS)
        i = args.index("--model")
        args[i + 1] = model
    else:
        args = list(review_gate.DEFAULT_CLAUDE_ARGS)
    # The real claude path (non-stub) — normalised to '<claude>'.
    cmd = ["<real-claude>", "-p", prompt] + args
    # Normalise: plugin root, deny extension path, and range in prompt.
    plugin_root = os.path.normcase(os.path.abspath(review_gate._PLUGIN_ROOT))
    deny_ext = os.path.normcase(os.path.abspath(review_gate._DENY_EXT))
    normalized = []
    for a in cmd:
        # Only substitute if the token is non-empty AND matches the path.
        if a and (os.path.normcase(a) == plugin_root
                  or os.path.normcase(os.path.abspath(a)) == plugin_root):
            a = "<plugin-dir>"
        if a and os.path.normcase(os.path.abspath(a)) == deny_ext:
            a = "<deny-ext>"
        normalized.append(a.replace(push_range, "<RANGE>"))
    normalized[0] = "<claude>"
    return normalized


def _load_fixture():
    with open(_FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)["argv"]


def test_golden_fixture_is_current():
    """The committed fixture must match what _run_review actually builds today.

    If this test fails after a change to DEFAULT_CLAUDE_ARGS or PROMPT_RANGE,
    update tests/fixtures/argv_golden.json to reflect the new baseline.
    """
    built = _build_normalized_argv()
    fixture = _load_fixture()
    assert built == fixture, (
        "argv_golden.json is out of sync with DEFAULT_CLAUDE_ARGS / PROMPT_RANGE.\n"
        f"  built:   {built}\n"
        f"  fixture: {fixture}"
    )


def test_golden_has_no_paths_file():
    """The golden argv must NOT contain --paths-file — that is what makes it 'full'."""
    fixture = _load_fixture()
    assert "--paths-file" not in fixture


def test_golden_has_correct_model():
    """The golden argv uses the reviewer pin (kimchi-dev/kimi-k3, D16). A flash
    orchestration default was tried and reverted (2026-11-09): it broke the
    skill flow (verdict extraction failed on every review) and tripled wall
    time — see docs/review-latency-analysis.md."""
    fixture = _load_fixture()
    idx = fixture.index("--model")
    assert fixture[idx + 1] == "kimchi-dev/kimi-k3"


def test_golden_loads_the_deny_extension():
    """The -e deny extension is the worker's sandbox AND (DOCS-GATE-08) the
    carrier that puts this plugin's review skill on the resolution path."""
    fixture = _load_fixture()
    assert "-e" in fixture
    assert fixture[fixture.index("-e") + 1] == "<deny-ext>"


_DENY_EXT_SRC = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..",
    "extensions", "review-gate-deny.ts")


_REVIEW_SKILL = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..",
    "skills", "review", "SKILL.md")


def test_worker_skill_resolution_wiring():
    """DOCS-GATE-08: `kimchi -p "/skill:review ..."` must resolve to THIS
    plugin's review skill, not a foreign global one.

    Live finding (ticket 07): headless `-p` prompts are not slash-expanded
    unless the CLI knows a `skill:review` command, and the worker's skill
    catalogue holds only the user's global skills (config.json skillPaths),
    where a `code-review` skill shadowed the lookup -- the foreign skill
    answered instead, so the verdict contract and the docs-checker pass
    never ran. The native fix: the `-e` deny extension also answers
    `resources_discover` with the absolute path of this plugin's
    skills/review/SKILL.md (top-level skills become `skill:<name>`
    commands), resolving `/skill:review` to this file. Verified live:
    the child prompt arrives as `<skill name="review" location="...">`
    with this repository's SKILL.md inline."""
    with open(_DENY_EXT_SRC, encoding="utf-8") as fh:
        src = fh.read()
    # The extension carries a resources_discover handler ...
    assert 'pi.on("resources_discover"' in src
    # ... returning THIS plugin's review SKILL.md ...
    assert "skills/review/SKILL.md" in src
    # ... with the same inertness gate as the sandbox.
    rd = src[src.index('pi.on("resources_discover"'):]
    assert 'process.env.OCR_IN_REVIEW !== "1"' in rd[:600]


def test_review_skill_frontmatter_names_it_review():
    """skill:review resolves by the SKILL.md frontmatter `name`; pin it."""
    assert os.path.isfile(_REVIEW_SKILL)
    with open(_REVIEW_SKILL, encoding="utf-8") as fh:
        head = fh.read(400)
    assert "\nname: review\n" in head


def test_golden_approves_project_local_files():
    """A6: the worker argv carries --approve so skill discovery works in the
    untrusted --no-hardlinks clone (spelling verified against `kimchi --help`)."""
    fixture = _load_fixture()
    assert "--approve" in fixture
    assert "--no-approve" not in fixture
