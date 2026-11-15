"""Demand-contract validator for the review gate's context spotter.

The context spotter (agents/context-spotter.md) reads the trimmed diff and
emits a *demands* object -- what context the change structurally requires:

    {"symbol_callers": [], "event_couples": [], "doc_refs": [],
     "conventions": [], "prose_pointers": [],
     "risk": {"score": 0, "reasons": []}}

It must never emit findings. This module is the strict, fail-closed gate-side
validator for that contract (ADR 0001 discipline: a fixed shape, no
improvisation rescue; loosening is an ADR-level decision, not code):

* anything that does not match the contract exactly -> ``demands`` is None and
  the review degrades to T0 (proceeds without demands) with a warning;
* the one enumerated exception is *finding-shaped* content (``findings`` keys,
  objects with finding fields, a bare array of finding-like objects): it is
  dropped with a warning -- the spotter is demands-only -- and what remains is
  then validated as usual.

Fail-soft: the public entry point never raises; any internal error degrades to
T0 with a warning.

CLI::

    python3 scripts/ocr_demands.py                 # validate JSON on stdin
    python3 scripts/ocr_demands.py --dump-schema   # print the JSON schema

Prints ``{"demands": <object or null>, "warnings": [...]}`` on stdout and
always exits 0: the demands contract can only *diminish* context, never block
or crash the pipeline.
"""
import json
import os
import sys

import ocr_telemetry

# The demand-list keys, in contract order. schemas/demands.schema.json is
# GENERATED from this module (DEMANDS_SCHEMA below); do not edit it by hand.
DEMAND_KEYS = ("symbol_callers", "event_couples", "doc_refs", "conventions",
               "prose_pointers")
RISK_KEYS = ("score", "reasons")

# A demand entry is a short string ("get_settings", "docs/STYLE.md") or a
# small descriptive object whose values are scalars or one level of scalar
# lists. Anything deeper or any other type violates the contract.
_SCALAR_TYPES = (str, int, float, bool)


def _is_scalar(value):
    return value is None or isinstance(value, _SCALAR_TYPES)


def is_finding_like(obj):
    """True for an object shaped like a reviewer finding rather than a demand.

    Findings carry severity + content (and usually path/line/confidence); a
    demand never does. Used by the finding-shaped-output guard.
    """
    return (isinstance(obj, dict)
            and isinstance(obj.get("severity"), str)
            and isinstance(obj.get("content"), str))


def _valid_demand_list(value, label):
    """Validate one demand-list field; returns (ok, errors-as-warning-text)."""
    if not isinstance(value, list):
        return False, "%s must be an array" % label
    for i, entry in enumerate(value):
        if isinstance(entry, str):
            if entry.strip():
                continue
            return False, "%s[%d] is an empty string" % (label, i)
        if isinstance(entry, dict):
            for k, v in entry.items():
                if _is_scalar(v):
                    continue
                if isinstance(v, list) and all(_is_scalar(x) for x in v):
                    continue
                return False, "%s[%d].%s is not a scalar or a scalar list" % (label, i, k)
            continue
        return False, "%s[%d] is not a string or an object" % (label, i)
    return True, ""


def _valid_risk(value):
    """Validate the risk field; returns (ok, reason)."""
    if not isinstance(value, dict):
        return False, "risk must be an object"
    extra = set(value) - set(RISK_KEYS)
    missing = set(RISK_KEYS) - set(value)
    if extra or missing:
        return False, "risk must have exactly the keys score and reasons"
    score, reasons = value["score"], value["reasons"]
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return False, "risk.score must be a number"
    if score < 0:
        return False, "risk.score must be non-negative"
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        return False, "risk.reasons must be an array of strings"
    return True, ""


def validate_demands(text):
    """Validate spotter output; fail-closed. Never raises.

    Returns {"demands": object or None, "warnings": [...]}. ``demands`` is the
    validated object on success, or None to degrade to T0 (review without
    demands) when the output was malformed or used an unrecognized shape.
    Finding-shaped content is dropped with a warning before validation.
    """
    warnings = []
    try:
        data = json.loads(text) if isinstance(text, str) else text
    except ValueError as exc:
        warnings.append("spotter: output was not valid JSON (%s); "
                        "proceeding without demands (T0 fallback)" % exc)
        return {"demands": None, "warnings": warnings}

    # Finding-shaped-output guard (the enumerated exception): the spotter is
    # demands-only, so anything finding-like is dropped, never trusted.
    if isinstance(data, list):
        if data and all(is_finding_like(x) for x in data):
            warnings.append("spotter: output was an array of %d finding-shaped "
                            "object(s); dropped (demands-only contract); "
                            "proceeding without demands (T0 fallback)" % len(data))
            return {"demands": None, "warnings": warnings}
        warnings.append("spotter: output must be a JSON object, not an array; "
                        "proceeding without demands (T0 fallback)")
        return {"demands": None, "warnings": warnings}
    if is_finding_like(data):
        warnings.append("spotter: output was finding-shaped; dropped "
                        "(demands-only contract); proceeding without demands "
                        "(T0 fallback)")
        return {"demands": None, "warnings": warnings}
    if not isinstance(data, dict):
        warnings.append("spotter: output must be a JSON object; "
                        "proceeding without demands (T0 fallback)")
        return {"demands": None, "warnings": warnings}
    data = dict(data)
    if isinstance(data.get("findings"), list):
        warnings.append("spotter: %d finding-shaped object(s) dropped from "
                        "output (demands-only contract)" % len(data["findings"]))
        data.pop("findings")

    # Strict contract: exactly the five demand arrays plus risk. An
    # unrecognized shape fails closed; the gate must not improvise a rescue.
    keys = set(data)
    missing = [k for k in DEMAND_KEYS + ("risk",) if k not in keys]
    unknown = sorted(k for k in keys if k not in DEMAND_KEYS and k != "risk")
    if missing or unknown:
        bits = []
        if missing:
            bits.append("missing key(s) %s" % ", ".join(missing))
        if unknown:
            bits.append("unknown key(s) %s" % ", ".join(unknown))
        warnings.append("spotter: output has an unrecognized shape (%s); "
                        "proceeding without demands (T0 fallback)" % "; ".join(bits))
        return {"demands": None, "warnings": warnings}

    ok, why = _valid_risk(data["risk"])
    if not ok:
        warnings.append("spotter: %s; proceeding without demands "
                        "(T0 fallback)" % why)
        return {"demands": None, "warnings": warnings}

    dropped = 0
    for key in DEMAND_KEYS:
        if not isinstance(data[key], list):
            warnings.append("spotter: %s must be an array; proceeding without "
                            "demands (T0 fallback)" % key)
            return {"demands": None, "warnings": warnings}
        kept = [e for e in data[key] if not is_finding_like(e)]
        dropped += len(data[key]) - len(kept)
        data[key] = kept
        ok, why = _valid_demand_list(data[key], key)
        if not ok:
            warnings.append("spotter: %s; proceeding without demands "
                            "(T0 fallback)" % why)
            return {"demands": None, "warnings": warnings}
    if dropped:
        warnings.append("spotter: %d finding-shaped demand entr%s dropped "
                        "(demands-only contract)"
                        % (dropped, "y was" if dropped == 1 else "ies were"))
    return {"demands": data, "warnings": warnings}


def _schema():
    """The demands JSON schema (draft-07), mirroring validate_demands exactly."""
    scalar_or_list = {
        "anyOf": [
            {"type": ["string", "number", "boolean", "null"]},
            {"type": "array",
             "items": {"type": ["string", "number", "boolean", "null"]}},
        ],
    }
    demand_list = {
        "type": "array",
        "items": {
            "anyOf": [
                {"type": "string", "minLength": 1, "pattern": "\\S"},
                {"type": "object", "additionalProperties": scalar_or_list},
            ],
        },
    }
    props = {k: {"$ref": "#/definitions/demandList"} for k in DEMAND_KEYS}
    props["risk"] = {"$ref": "#/definitions/risk"}
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": "https://github.com/Jose-Ribeir/claude-code-review-gate/schemas/demands.schema.json",
        "title": "context-spotter demands",
        "description": "Output of the context-spotter agent: context demands only, never findings. "
                       "Validated gate-side, fail-closed (scripts/ocr_demands.py is the "
                       "single source; this schema is generated from it).",
        "type": "object",
        "required": list(DEMAND_KEYS) + ["risk"],
        "additionalProperties": False,
        "properties": props,
        "definitions": {
            "demandList": demand_list,
            "risk": {
                "type": "object",
                "required": list(RISK_KEYS),
                "additionalProperties": False,
                "properties": {
                    "score": {"type": "number", "minimum": 0},
                    "reasons": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    }


DEMANDS_SCHEMA = _schema()


def _record_spotter_telemetry(text):
    """The spotter tier's per-tier cost record (v2). The spotter subagent is
    spawned inside the review session, so its output's mandatory passage
    through this validator is the one deterministic seam that sees the call:
    record what's known -- the configured model (mirrors review-gate.py's
    OCR_SPOTTER_MODEL default) and a chars//4 estimate of the output bytes.
    The call's duration never crosses this seam. Best-effort, never raises.
    """
    try:
        common = ocr_telemetry.repo_common_dir(os.getcwd())
        if not common:
            return
        model = (os.environ.get("OCR_SPOTTER_MODEL", "deepseek-v4-flash").strip()
                 or "deepseek-v4-flash")
        ocr_telemetry.record_tier_call(
            common, ocr_telemetry.TIER_SPOTTER, model=model,
            token_estimate=ocr_telemetry.estimate_tokens(len(text or "")))
    except Exception:
        pass


def _main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description="Validate context-spotter demands JSON (stdin -> stdout).")
    parser.add_argument("--dump-schema", metavar="PATH", nargs="?", const="-",
                        help="print or write the demands JSON schema and exit")
    args = parser.parse_args(argv)
    if args.dump_schema is not None:
        text = json.dumps(DEMANDS_SCHEMA, indent=2) + "\n"
        if args.dump_schema == "-":
            sys.stdout.write(text)
        else:
            with open(args.dump_schema, "w", encoding="utf-8") as fh:
                fh.write(text)
        return 0
    text = sys.stdin.read()
    result = validate_demands(text)
    _record_spotter_telemetry(text)
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
