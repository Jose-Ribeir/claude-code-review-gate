"""Safe stage completion and supervisor-owned structured stops for SPEC-16.

Workers may request a stop, but only this trusted execution layer validates the
request and the review/stack supervisor maps it to concrete units. An ordinary
finding never cancels another useful stage. A validated stop retains its
original critical finding and turns every affected, unrun mandatory stage into
explicit skipped evidence rather than a shortened pass.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import re

import ocr_verdict
from rg_final_dynamic import record_dynamic_result
from rg_final_lenses import (
    LensOutcome,
    REQUIRED_LENSES,
    _error_details,
    _run_bounded_lenses,
    aggregate_lenses,
    normalize_worker_result,
)


EXECUTION_SCHEMA = "final-review-execution/v1"
STOP_SCHEMA = "final-review-stop/v1"
STOP_CATEGORIES = frozenset(("unsafe", "invalid", "untrustworthy"))
STOP_CONFIDENCE = 0.9
_TEST_STAGES = (("required_tests", "required verification"),
                ("dynamic_tests", "dynamic checks"))
_REASON_CODE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class StopRequestError(ValueError):
    """A worker's requested STOP has no objective stopping contract."""


@dataclass
class UnitExecution:
    """Detached execution products, aggregated only after useful work finishes."""

    aggregation: object
    required: dict | None = None
    dynamic: dict | None = None
    stop: dict | None = None
    rejected_stops: list = field(default_factory=list)
    skipped_work: list = field(default_factory=list)

    @property
    def finding_verdict(self):
        # Incomplete static evidence never turns partial findings into a clean
        # or blocking aggregate. Stop findings remain visible independently.
        if not self.aggregation.complete or self.stop is not None:
            return None
        findings = list(self.aggregation.findings)
        for result in (self.required, self.dynamic):
            findings.extend(deepcopy((result or {}).get("findings") or []))
        verdict = ocr_verdict.compute_verdict({
            "findings": findings,
            "unreviewed": list(self.aggregation.unreviewed),
        })
        if (self.required or {}).get("blocking") is True and \
                self.required.get("status") == "block":
            # Reproduced required evidence is complete and independently blocking;
            # unlike a model finding it needs no source-line location to act on it.
            return "block"
        return verdict

    @property
    def checks_complete(self):
        required_ok = self.required is None or self.required.get("status") not in (
            None, "incomplete", "STOP", "stop",
        )
        dynamic_ok = self.dynamic is None or self.dynamic.get("state") not in (None, "error")
        return (self.aggregation.complete and self.stop is None
                and required_ok and dynamic_ok)

    def as_dict(self):
        return {
            "schema": EXECUTION_SCHEMA,
            "complete": self.checks_complete,
            "finding_verdict": self.finding_verdict,
            "static_lenses_complete": self.aggregation.complete,
            "required_verification": deepcopy(self.required),
            "dynamic_checks": deepcopy(self.dynamic),
            "stop": deepcopy(self.stop),
            "rejected_stops": deepcopy(self.rejected_stops),
            "skipped_work": deepcopy(self.skipped_work),
        }


def _stop_envelope(result):
    if not isinstance(result, dict):
        return None
    nested = result.get("stop")
    status = str(result.get("status") or "").strip().upper()
    if nested is None and status != "STOP":
        return None
    if nested is None:
        nested = {key: deepcopy(value) for key, value in result.items()
                  if key not in ("schema", "status", "summary", "findings", "warnings")}
    if not isinstance(nested, dict):
        raise StopRequestError("STOP payload must be an object")
    return nested


def _reason_parts(payload):
    reason = payload.get("reason")
    if isinstance(reason, dict):
        code = reason.get("code") or payload.get("reason_code") or payload.get("code")
        text = reason.get("message") or reason.get("content") or payload.get("message")
    else:
        code = payload.get("reason_code") or payload.get("code")
        text = reason or payload.get("message")
    return str(code or "").strip(), str(text or "").strip()


def validate_stop(result, *, stage="execution"):
    """Return a detached trusted stop or raise :class:`StopRequestError`.

    A subjective severity claim is insufficient. The payload needs a stable
    reason code, one of the three dependency-invalidation categories,
    structured positive evidence, and a complete high-confidence critical
    finding which independently passes the normal actionable-finding gate.
    """
    payload = _stop_envelope(result)
    if payload is None:
        return None
    code, reason = _reason_parts(payload)
    category = str(payload.get("category") or payload.get("kind") or "").strip().lower()
    evidence = payload.get("evidence")
    finding = deepcopy(payload.get("finding"))
    if not _REASON_CODE.fullmatch(code) or not reason:
        raise StopRequestError("STOP requires a machine-readable reason code and message")
    if category not in STOP_CATEGORIES:
        raise StopRequestError("STOP category must be unsafe, invalid, or untrustworthy")
    if not isinstance(evidence, dict) or not any(
            value not in (None, "", [], {}) for value in evidence.values()):
        raise StopRequestError("STOP requires non-empty structured evidence")
    if not isinstance(finding, dict):
        raise StopRequestError("STOP requires its original critical finding")
    try:
        confidence = float(finding.get("confidence", payload.get("confidence", 0.0)))
    except (TypeError, ValueError) as exc:
        raise StopRequestError("STOP finding confidence must be numeric") from exc
    finding["confidence"] = confidence
    finding["severity"] = ocr_verdict.norm_severity(finding.get("severity"))
    if confidence < STOP_CONFIDENCE or finding["severity"] != "high":
        raise StopRequestError("STOP requires a high-confidence critical finding")
    if ocr_verdict.compute_verdict({"findings": [finding]}) != "block":
        raise StopRequestError("STOP finding is not complete and actionable")
    return {
        "schema": STOP_SCHEMA,
        "stage": str(stage),
        "reason_code": code,
        "reason": reason,
        "category": category,
        "evidence": deepcopy(evidence),
        "finding": finding,
        # A worker's scope label is diagnostic only. The supervisor supplies
        # affected_unit_ids after mapping its own unit/dependency graph.
        "requested_scope": deepcopy(payload.get("scope")),
    }


def _rejected_result(result, error):
    """Downgrade an invalid stop request into ordinary, visible worker evidence."""
    raw = deepcopy(result) if isinstance(result, dict) else {}
    try:
        payload = _stop_envelope(raw) or {}
    except StopRequestError:
        payload = {}
    finding = payload.get("finding")
    findings = list(raw.get("findings") or [])
    if isinstance(finding, dict):
        findings.append(finding)
    warnings = list(raw.get("warnings") or [])
    warnings.append({
        "code": "structured-stop-rejected",
        "message": f"Structured STOP was not accepted: {error}",
        "scope": "execution",
        "details": {"error": str(error), "required_continuation": True},
    })
    raw.update({"status": "success", "findings": findings, "warnings": warnings})
    raw.setdefault("unreviewed", [])
    raw.setdefault("summary", "Worker completed with a rejected STOP request")
    return raw


def _failed_lens(spec, error):
    message = str(error)
    details = {"error": message, **_error_details(error),
               "required": True, "lens": spec.name}
    warning = {
        "code": "required-lens-failed",
        "message": f"Required {spec.name} lens did not complete: {message}",
        "scope": spec.name,
        "details": details,
        "lens": spec.name,
    }
    return LensOutcome(name=spec.name, state="incomplete", summary=message,
                       warnings=(warning,), capabilities=tuple(spec.capabilities), error=message)


def _stop_lens(spec, stop, *, skipped):
    state = "skipped" if skipped else "stopped"
    message = ("Skipped after dependent critical stop" if skipped else stop["reason"])
    warning = {
        "code": "mandatory-work-skipped" if skipped else "structured-stop",
        "message": message,
        "scope": spec.name,
        "details": {"stop": deepcopy(stop), "lens": spec.name, "required": True},
        "lens": spec.name,
    }
    return LensOutcome(name=spec.name, state=state, summary=message,
                       warnings=(warning,), capabilities=tuple(spec.capabilities),
                       error=stop["reason_code"])


def _skip(stop, kind, name, unit_id=None, completed=False):
    return {
        "unit_id": unit_id,
        "kind": kind,
        "name": name,
        "details": {
            "status": "skipped",
            "reason_code": stop["reason_code"],
            "reason": stop["reason"],
            "category": stop["category"],
            "evidence": deepcopy(stop["evidence"]),
            "source_stage": stop["stage"],
            "completed_before_stop": completed,
        },
    }


def skipped_execution(stop, *, unit_id=None, run_tests=True):
    """Build evidence for a dependent unit which the supervisor chose not to start."""
    aggregation = aggregate_lenses([
        _stop_lens(spec, stop, skipped=True) for spec in REQUIRED_LENSES
    ])
    skipped = [_skip(stop, "lens", spec.name, unit_id) for spec in REQUIRED_LENSES]
    if run_tests:
        skipped.extend(_skip(stop, "stage", kind, unit_id) for kind, _name in _TEST_STAGES)
    return UnitExecution(aggregation=aggregation, stop=deepcopy(stop), skipped_work=skipped)


def _run_test_stage(name, runner, call):
    try:
        raw = call()
        stop = validate_stop(raw, stage=name)
        return raw, stop, None
    except StopRequestError as exc:
        # The caller converts this to stage-appropriate ordinary evidence and
        # continues independent stages. No untrusted stop can widen its scope.
        return raw, None, exc
    except Exception as exc:
        return None, None, exc


def execute_unit(request, worker, *, required_runner=None, dynamic_runner=None,
                 unit_id=None, dynamic_kwargs=None, specs=REQUIRED_LENSES,
                 max_workers=None, environ=None):
    """Execute one materialized unit through safe static/test completion.

    ``required_runner`` and ``dynamic_runner`` use the existing
    ``run_required_verification`` and ``run_dynamic_checks`` signatures. Passing
    neither retains the earlier static-only stage contract. When supplied,
    both run after ordinary blocking findings; only a validated stop suppresses
    dependent work. Any worker-declared scope is deliberately ignored here.
    """
    from pathlib import Path

    stop = None
    stops_by_lens = {}
    rejected = []
    skipped = []

    def normalize(raw, spec):
        nonlocal stop
        try:
            stage_stop = validate_stop(raw, stage=f"lens:{spec.name}")
        except StopRequestError as exc:
            rejected.append({"stage": f"lens:{spec.name}", "error": str(exc)})
            raw = _rejected_result(raw, exc)
            stage_stop = None
        if stage_stop is not None:
            # Safe peers already in flight still return full, separately
            # attributable evidence. Ownership is selected only after every
            # peer joins; this provisional value lets the scheduler build skips.
            stops_by_lens[spec.name] = stage_stop
            if stop is None:
                stop = stage_stop
            return _stop_lens(spec, stage_stop, skipped=False)
        return normalize_worker_result(raw, spec)

    def skip_lens(spec):
        return _stop_lens(spec, stop, skipped=True)

    outcomes = _run_bounded_lenses(
        request, worker, specs, normalize_result=normalize,
        failed_result=_failed_lens, skipped_result=skip_lens,
        max_workers=max_workers, environ=environ,
    )
    # Completion timing must not choose the supervisor's STOP. All in-flight
    # peers have joined now, so select by the trusted declared lens order and
    # rewrite only pending-lens skip envelopes settled by an earlier STOP.
    stop = next((stops_by_lens[spec.name] for spec in specs
                 if spec.name in stops_by_lens), None)
    outcomes = tuple(
        _stop_lens(spec, stop, skipped=True) if outcome.state == "skipped" else outcome
        for spec, outcome in zip(specs, outcomes)
    )
    for spec, outcome in zip(specs, outcomes):
        if outcome.state == "stopped":
            owned_stop = outcome.warnings[0]["details"]["stop"]
            skipped.append(_skip(owned_stop, "stage", f"lens:{spec.name}", unit_id))
        elif outcome.state == "skipped":
            skipped.append(_skip(stop, "lens", spec.name, unit_id))
    aggregation = aggregate_lenses(outcomes)

    required = dynamic = None
    runners = (("required verification", "required_tests", required_runner),
               ("dynamic checks", "dynamic_tests", dynamic_runner))
    for name, kind, runner in runners:
        if runner is None:
            continue
        if stop is not None:
            skipped.append(_skip(stop, "stage", kind, unit_id))
            continue
        if kind == "required_tests":
            def call(runner=runner):
                return runner(Path(request["repository"]["path"]),
                              request["repository"]["head_sha"])
        else:
            options = dict(dynamic_kwargs or {})
            options.setdefault("unit_key", request.get("unit_key", "individual:1"))

            def call(runner=runner, options=options):
                return runner(Path(request["repository"]["path"]),
                              request["repository"]["target_sha"],
                              request["repository"]["head_sha"], **options)
        raw, stage_stop, error = _run_test_stage(name, runner, call)
        if stage_stop is not None:
            stop = stage_stop
            skipped.append(_skip(stop, "stage", kind, unit_id, completed=True))
            raw = None
        elif error is None:
            if kind == "dynamic_tests":
                dynamic = raw
            else:
                required = raw
        if error is not None:
            if isinstance(error, StopRequestError):
                rejected.append({"stage": name, "error": str(error)})
                raw = _rejected_result(raw, error)
                raw["schema"] = ("final-review-dynamic-result/v1" if kind == "dynamic_tests"
                                 else raw.get("schema", "final-review-required-result/v1"))
            if kind == "dynamic_tests":
                raw = raw if isinstance(raw, dict) else {
                    "schema": "final-review-dynamic-result/v1"}
                raw.setdefault("warnings", []).append({
                    "code": "dynamic-executor", "message": str(error),
                    "scope": "dynamic", "details": {"error": str(error)},
                })
                raw.setdefault("state", "error")
                raw.setdefault("complete", False)
                raw.setdefault("coverage", {"status": "incomplete", "summary": str(error)})
                raw.setdefault("findings", [])
                raw.setdefault("tests", [])
                raw.setdefault("telemetry", {})
                raw.setdefault("revisions", {})
                raw.setdefault("unit_key", request.get("unit_key", "individual:1"))
                dynamic = raw
            else:
                required = raw if isinstance(raw, dict) else {
                    "status": "incomplete", "blocking": False, "suites": [], "findings": []}
                required["status"] = "incomplete"
                required.setdefault("blocking", False)
                required.setdefault("suites", [])
                required.setdefault("findings", [])
                required.setdefault("warnings", []).append(str(error))
    return UnitExecution(aggregation=aggregation, required=required, dynamic=dynamic,
                         stop=stop, rejected_stops=rejected, skipped_work=skipped)


def record_required_result(store, unit_id, result):
    """Persist existing required-verification output through FinalStore's API."""
    if result is None:
        return
    for suite in result.get("suites") or []:
        store.record_test(
            unit_id, suite.get("name", "required-suite"), "required",
            suite.get("state", "unknown"), output=suite.get("output", ""),
            evidence_fingerprint=suite.get("evidence_fingerprint", ""),
            details={key: deepcopy(suite.get(key)) for key in
                     ("command", "source", "attempts", "error")},
        )
    used = set()
    for finding in result.get("findings") or []:
        finding = deepcopy(finding)
        finding.setdefault("start_line", 1)
        finding.setdefault("end_line", finding["start_line"])
        key = "required-" + str(finding.get("name") or len(used) + 1)
        if key in used:
            key += f"-{len(used) + 1}"
        used.add(key)
        store.record_finding(
            unit_id, key, str(finding.get("content") or ""),
            ocr_verdict.norm_severity(finding.get("severity")), finding.get("confidence", 0.0),
            lens="required-verification", path=finding.get("path"),
            line=finding.get("start_line"),
            anchors=[{
                "anchor_type": "required_evidence",
                "anchor": str(finding.get("evidence_fingerprint") or key),
                "evidence_fingerprint": str(finding.get("evidence_fingerprint") or ""),
                "details": {"name": finding.get("name")},
            }], evidence=finding,
        )
    for warning in result.get("warnings") or []:
        if isinstance(warning, dict):
            store.record_warning(unit_id, warning.get("code", "required-verification"),
                                 warning.get("message", ""), warning.get("scope", "required"),
                                 warning.get("details") or {})
        else:
            store.record_warning(unit_id, "required-verification", str(warning), "required")
    store.record_coverage(
        unit_id, "required_tests", result.get("status", "incomplete"),
        "Declared required verification completed" if result.get("status") == "pass"
        else "Declared required verification was absent"
        if result.get("status") == "absent" else "Required verification did not produce a pass",
        {"blocking": bool(result.get("blocking")), "commit": result.get("commit")},
    )


def record_execution_tests(store, unit_id, execution):
    record_required_result(store, unit_id, execution.required)
    if execution.dynamic is not None:
        record_dynamic_result(store, unit_id, execution.dynamic)
    for skipped in execution.skipped_work:
        if skipped["kind"] != "stage" or skipped["name"] not in dict(_TEST_STAGES):
            continue
        status = "stopped" if skipped["details"].get("completed_before_stop") else "skipped"
        store.record_coverage(
            skipped.get("unit_id") or unit_id, skipped["name"], status,
            "Mandatory test stopped or skipped after a validated dependent STOP",
            skipped["details"],
        )
