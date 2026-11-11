"""Tests for scripts/ocr_demands.py — the context-spotter demand contract.

The spotter (agents/context-spotter.md) reads the trimmed diff and emits
*demands only*: what context the change structurally requires, never findings.
The gate validates that output strictly and fails closed (ADR 0001: no
improvisation rescue): any unrecognized shape degrades to T0 behavior (review
proceeds without demands) plus a warning. Finding-shaped content — the one
enumerated exception — is dropped with a warning before validation.
"""
import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
sys.path.insert(0, _SCRIPTS)

import ocr_demands as od  # noqa: E402

EMPTY = {"symbol_callers": [], "event_couples": [], "doc_refs": [],
         "conventions": [], "prose_pointers": [], "risk": {"score": 0, "reasons": []}}


def _validate(payload):
    import json
    return od.validate_demands(json.dumps(payload))


# ---------------------------------------------------------------------------
# Fail-closed on unrecognized shapes (ADR 0001: no improvisation rescue)
# ---------------------------------------------------------------------------

class TestFailClosed:
    def _invalid(self, text_or_obj):
        import json
        text = text_or_obj if isinstance(text_or_obj, str) else json.dumps(text_or_obj)
        result = od.validate_demands(text)
        assert result["demands"] is None, result
        assert result["warnings"], "every degradation path must warn"
        return result["warnings"]

    def test_non_json_text(self):
        warnings = self._invalid("I could not classify this diff.")
        assert any("not valid JSON" in w for w in warnings)

    def test_trailing_garbage_after_json(self):
        import json
        warnings = self._invalid(json.dumps(EMPTY) + " trailing")
        assert any("not valid JSON" in w for w in warnings)

    def test_bare_string(self):
        self._invalid('"symbol_callers"')

    def test_bare_number(self):
        self._invalid("42")

    def test_null(self):
        self._invalid("null")

    def test_top_level_array_of_strings(self):
        warnings = self._invalid(["a", "b"])
        assert any("must be a JSON object" in w for w in warnings)

    def test_missing_demand_key(self):
        payload = {k: v for k, v in EMPTY.items() if k != "doc_refs"}
        warnings = self._invalid(payload)
        assert any("missing" in w and "doc_refs" in w for w in warnings)

    def test_missing_risk(self):
        payload = {k: v for k, v in EMPTY.items() if k != "risk"}
        warnings = self._invalid(payload)
        assert any("missing" in w and "risk" in w for w in warnings)

    def test_unknown_top_level_key(self):
        payload = dict(EMPTY, notes="I also checked the build")
        warnings = self._invalid(payload)
        assert any("unknown key" in w and "notes" in w for w in warnings)

    def test_demand_field_not_an_array(self):
        self._invalid(dict(EMPTY, symbol_callers="helper"))

    def test_entry_null(self):
        self._invalid(dict(EMPTY, doc_refs=[None]))

    def test_entry_number(self):
        self._invalid(dict(EMPTY, symbol_callers=[42]))

    def test_entry_empty_string(self):
        self._invalid(dict(EMPTY, symbol_callers=["  "]))

    def test_entry_array(self):
        self._invalid(dict(EMPTY, symbol_callers=[["a"]]))

    def test_entry_object_with_nested_object(self):
        self._invalid(dict(EMPTY, doc_refs=[{"doc": {"path": "x.md"}}]))

    def test_entry_object_with_nested_array_of_arrays(self):
        self._invalid(dict(EMPTY, doc_refs=[{"paths": [["a"]]}]))

    def test_risk_not_an_object(self):
        self._invalid(dict(EMPTY, risk="low"))

    def test_risk_extra_key(self):
        self._invalid(dict(EMPTY, risk={"score": 1, "reasons": [], "lane": "T2"}))

    def test_risk_missing_score(self):
        self._invalid(dict(EMPTY, risk={"reasons": []}))

    def test_risk_string_score(self):
        self._invalid(dict(EMPTY, risk={"score": "high", "reasons": []}))

    def test_risk_boolean_score(self):
        """True is an int subclass in Python; the contract must exclude it."""
        self._invalid(dict(EMPTY, risk={"score": True, "reasons": []}))

    def test_risk_negative_score(self):
        self._invalid(dict(EMPTY, risk={"score": -1, "reasons": []}))

    def test_risk_reasons_not_strings(self):
        self._invalid(dict(EMPTY, risk={"score": 1, "reasons": [1, 2]}))

    def test_risk_reasons_not_a_list(self):
        self._invalid(dict(EMPTY, risk={"score": 1, "reasons": "auth"}))

# ---------------------------------------------------------------------------
# Finding-shaped-output guard (demands-only contract)
# ---------------------------------------------------------------------------

FINDING = {"path": "app/x.py", "start_line": 1, "end_line": 2, "severity": "high",
           "confidence": 0.9, "category": "correctness", "content": "stub finding"}


class TestFindingShapedGuard:
    def test_bare_array_of_findings_degrades_to_t0(self):
        result = _validate([FINDING, dict(FINDING, path="app/y.py")])
        assert result["demands"] is None
        assert any("finding-shaped" in w and "dropped" in w for w in result["warnings"])

    def test_single_finding_object_degrades_to_t0(self):
        """A lone finding object instead of demands: dropped, warned, T0."""
        result = _validate(FINDING)
        assert result["demands"] is None
        assert any("finding-shaped" in w for w in result["warnings"])

    def test_top_level_findings_array_dropped_but_demands_kept(self):
        """The common reviewer-trained failure: demands plus a findings array.
        The findings are dropped with a warning; the rest stands."""
        payload = dict(EMPTY, symbol_callers=["helper"], findings=[FINDING])
        result = _validate(payload)
        assert result["demands"] == dict(EMPTY, symbol_callers=["helper"])
        assert "findings" not in result["demands"]
        assert any("1 finding-shaped" in w and "dropped" in w for w in result["warnings"])

    def test_top_level_findings_empty_array_dropped(self):
        payload = dict(EMPTY, findings=[])
        result = _validate(payload)
        assert result["demands"] == EMPTY
        assert result["warnings"], "the demands-only violation must still be visible"

    def test_top_level_findings_non_array_fails_closed(self):
        """'findings' that is not an array is an unrecognized shape, not the
        enumerated rescue: fail closed."""
        result = _validate(dict(EMPTY, findings="none"))
        assert result["demands"] is None
        assert any("unknown key" in w and "findings" in w for w in result["warnings"])

    def test_finding_like_entry_inside_demand_list_dropped(self):
        payload = dict(EMPTY, symbol_callers=[FINDING, "helper"])
        result = _validate(payload)
        assert result["demands"] == dict(EMPTY, symbol_callers=["helper"])
        assert any("finding-shaped" in w and "dropped" in w for w in result["warnings"])

    def test_finding_like_entry_with_nested_values_is_dropped_not_fatal(self):
        """A finding-shaped entry carries whatever keys the model invented; the
        guard drops it before entry-shape validation gets a vote."""
        mutant = dict(FINDING, detail={"nested": True})
        payload = dict(EMPTY, symbol_callers=[mutant, "helper"])
        result = _validate(payload)
        assert result["demands"] == dict(EMPTY, symbol_callers=["helper"])
        assert result["warnings"]

    def test_only_finding_like_entries_leave_empty_list(self):
        payload = dict(EMPTY, prose_pointers=[FINDING])
        result = _validate(payload)
        assert result["demands"] == EMPTY
        assert result["warnings"]

    def test_fence_prose_around_json_fails_closed(self):
        """Output wrapped in markdown fences / prose is not parseable JSON:
        no rescue heuristics (ADR 0001)."""
        import json
        text = "Sure! Here are my findings:\n```json\n" + json.dumps(EMPTY) + "\n```"
        result = od.validate_demands(text)
        assert result["demands"] is None
        assert any("not valid JSON" in w for w in result["warnings"])

# ---------------------------------------------------------------------------
# CLI: stdin -> stdout contract, always exit 0
# ---------------------------------------------------------------------------

class TestCLI:
    def _run(self, stdin, *args):
        import subprocess
        script = os.path.join(_SCRIPTS, "ocr_demands.py")
        proc = subprocess.run([sys.executable, script] + list(args), input=stdin,
                              capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr   # fail-soft: never crash the pipeline
        return proc

    def test_valid_input_round_trip(self):
        import json
        proc = self._run(json.dumps(dict(EMPTY, doc_refs=["docs/X.md"])))
        out = json.loads(proc.stdout)
        assert out["demands"]["doc_refs"] == ["docs/X.md"]
        assert out["warnings"] == []

    def test_garbage_input_degrades_with_warning(self):
        import json
        proc = self._run("not json at all")
        out = json.loads(proc.stdout)
        assert out["demands"] is None
        assert out["warnings"]

    def test_empty_stdin_degrades_with_warning(self):
        """A spotter that prints nothing must not wedge the gate."""
        import json
        out = json.loads(self._run("").stdout)
        assert out["demands"] is None and out["warnings"]

# ---------------------------------------------------------------------------
# JSON schema: single source (generated, cross-tested for agreement)
# ---------------------------------------------------------------------------

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "schemas", "demands.schema.json")


class TestSchemaSingleSource:
    def test_checked_in_schema_equals_generated(self):
        """schemas/demands.schema.json is generated from ocr_demands.DEMANDS_SCHEMA
        (python3 scripts/ocr_demands.py --dump-schema <path>); hand edits drift."""
        import json
        with open(_SCHEMA_PATH, encoding="utf-8") as fh:
            checked_in = json.load(fh)
        assert checked_in == od.DEMANDS_SCHEMA

    def test_schema_pins_the_contract_shape(self):
        schema = od.DEMANDS_SCHEMA
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(od.DEMAND_KEYS) | {"risk"}
        risk = schema["definitions"]["risk"]
        assert risk["additionalProperties"] is False
        assert risk["required"] == ["score", "reasons"]

    def test_schema_and_validator_agree_on_fixtures(self):
        """Run every fixture through both: the schema's required/additionalProperties
        discipline must mirror the validator's verdicts."""
        import json
        with open(_SCHEMA_PATH, encoding="utf-8") as fh:
            schema = json.load(fh)
        valid = [EMPTY, dict(EMPTY, symbol_callers=["x"]),
                 dict(EMPTY, doc_refs=[{"doc": "d.md", "paths": ["src"]}]),
                 dict(EMPTY, risk={"score": 9.5, "reasons": ["auth"]})]
        invalid = [dict(EMPTY, extra=True),
                   {k: v for k, v in EMPTY.items() if k != "risk"},
                   dict(EMPTY, risk={"score": "x", "reasons": []}),
                   dict(EMPTY, symbol_callers="nope")]
        for fixture in valid:
            assert _validate(fixture)["demands"] == fixture
            assert _schema_ok(schema, fixture), fixture
        for fixture in invalid:
            assert _validate(fixture)["demands"] is None
            assert not _schema_ok(schema, fixture), fixture


def _schema_ok(schema, obj):
    """A minimal draft-07 subset checker for exactly the demands schema:
    required, additionalProperties, object/array typed fields, minLength,
    minimum, anyOf. Keeps the cross-test dependency-free."""
    def check(node, value):
        if "$ref" in node:
            return check(schema["definitions"][node["$ref"].rsplit("/", 1)[1]], value)
        if "anyOf" in node:
            return any(check(alt, value) for alt in node["anyOf"])
        types = node.get("type")
        if types:
            if isinstance(types, str):
                types = [types]
            ok = any(
                (t == "object" and isinstance(value, dict))
                or (t == "array" and isinstance(value, list))
                or (t == "string" and isinstance(value, str))
                or (t == "number" and isinstance(value, (int, float))
                    and not isinstance(value, bool))
                or (t == "boolean" and isinstance(value, bool))
                or (t == "null" and value is None)
                for t in types)
            if not ok:
                return False
        if isinstance(value, dict):
            if node.get("required") and not all(k in value for k in node["required"]):
                return False
            props = node.get("properties", {})
            ap = node.get("additionalProperties", True)
            for k, v in value.items():
                if k in props:
                    if not check(props[k], v):
                        return False
                elif ap is False:
                    return False
                elif isinstance(ap, dict) and not check(ap, v):
                    return False
        if isinstance(value, list):
            items = node.get("items")
            if items and not all(check(items, v) for v in value):
                return False
        if isinstance(value, str) and "minLength" in node and len(value) < node["minLength"]:
            return False
        if isinstance(value, (int, float)) and not isinstance(value, bool) \
                and "minimum" in node and value < node["minimum"]:
            return False
        return True
    return check(schema, obj)

# ---------------------------------------------------------------------------
# Checked-in fixtures with known-expected demands
# ---------------------------------------------------------------------------

_FIXTURES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tests", "fixtures", "demands")


class TestFixtures:
    def _fixture(self, name):
        with open(os.path.join(_FIXTURES, name), encoding="utf-8") as fh:
            return fh.read()

    def test_empty_demands(self):
        import json
        result = od.validate_demands(self._fixture("empty.json"))
        assert result["demands"] == json.loads(self._fixture("empty.json"))
        assert result["warnings"] == []

    def test_caller_default_changed_demands(self):
        """The stranded-caller miss shape: a changed default on a shared helper
        must surface as a symbol_callers demand with a raised risk score."""
        import json
        result = od.validate_demands(self._fixture("caller_default_changed.json"))
        demands = result["demands"]
        assert demands is not None
        assert demands["symbol_callers"], demands
        assert demands["risk"]["score"] > 0
        assert result["warnings"] == []
        # Every fixture under this directory must also agree with the schema.
        with open(_SCHEMA_PATH, encoding="utf-8") as fh:
            assert _schema_ok(json.load(fh), demands)

    def test_malformed_prose_and_fences_degrade_to_t0(self):
        """A spotter that answers like a reviewer (prose + fenced findings):
        no rescue, T0 fallback with a warning."""
        result = od.validate_demands(self._fixture("malformed_prose.txt"))
        assert result["demands"] is None
        assert "T0" in result["warnings"][0]

# ---------------------------------------------------------------------------
# Valid demands
# ---------------------------------------------------------------------------

class TestValidDemands:
    def test_canonical_empty_demands(self):
        result = _validate(EMPTY)
        assert result["demands"] == EMPTY
        assert result["warnings"] == []

    def test_string_entries(self):
        payload = dict(EMPTY, symbol_callers=["helper", "parse_config"],
                       doc_refs=["docs/STYLE.md"])
        result = _validate(payload)
        assert result["demands"] == payload
        assert result["warnings"] == []

    def test_object_entries_with_scalar_and_list_values(self):
        payload = dict(EMPTY, event_couples=[
            {"producer": "emit('reload')", "consumers": ["useFilters", "useUndo"]},
            {"producer": "dispatch(tag)", "tag": "undo", "confidence": 0.9},
        ])
        result = _validate(payload)
        assert result["demands"] == payload
        assert result["warnings"] == []

    def test_risk_with_score_and_reasons(self):
        payload = dict(EMPTY, risk={"score": 7, "reasons": ["touches auth", "migration"]})
        result = _validate(payload)
        assert result["demands"] == payload
        assert result["warnings"] == []

    def test_float_risk_score(self):
        payload = dict(EMPTY, risk={"score": 4.5, "reasons": []})
        result = _validate(payload)
        assert result["demands"] == payload

    def test_key_order_and_whitespace_do_not_matter(self):
        text = '{ "risk": { "reasons": [], "score": 1 }, "prose_pointers": [],' \
               ' "doc_refs": [], "event_couples": [], "symbol_callers": [], "conventions": [] }'
        result = od.validate_demands(text)
        assert result["demands"] == dict(EMPTY, risk={"score": 1, "reasons": []})
