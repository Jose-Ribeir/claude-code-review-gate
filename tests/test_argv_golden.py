"""0.7.0 argv golden fixture.

_run_review builds a specific argv for a ≤15-file first push (no --paths-file).
This test captures that structure so scenarios 13 and 20 can verify 0.8.0 does not
add extra arguments in those cases.

Normalisation (documented):
  - Claude executable path  → '<claude>'
  - Plugin root (_PLUGIN_ROOT) → '<plugin-dir>'
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
    effective_model = model if model is not None else review_gate._MODEL
    prompt = review_gate.PROMPT_RANGE.format(rng=push_range)
    # Rebuild DEFAULT_CLAUDE_ARGS with the requested model if overridden.
    if model is not None and model != review_gate._MODEL:
        args = list(review_gate.DEFAULT_CLAUDE_ARGS)
        i = args.index("--model")
        args[i + 1] = model
    else:
        args = list(review_gate.DEFAULT_CLAUDE_ARGS)
    # The real claude path (non-stub) — normalised to '<claude>'.
    cmd = ["<real-claude>", "-p", prompt] + args
    # Normalise: plugin root and range in prompt.
    plugin_root = os.path.normcase(os.path.abspath(review_gate._PLUGIN_ROOT))
    normalized = []
    for a in cmd:
        # Only substitute if the token is non-empty AND matches the plugin root.
        if a and (os.path.normcase(a) == plugin_root
                  or os.path.normcase(os.path.abspath(a)) == plugin_root):
            a = "<plugin-dir>"
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
    """The golden argv uses the default model (sonnet)."""
    fixture = _load_fixture()
    idx = fixture.index("--model")
    assert fixture[idx + 1] == "sonnet"
