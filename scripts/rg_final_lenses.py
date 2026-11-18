"""Independent, mandatory static lenses for SPEC-16 final review.

Each lens receives a detached request over the same immutable full-tree
materialization and complete merge-base diff. No request contains another
lens's response. Workers are allowed to finish independently; only then are
normalized findings, warnings, and uncovered scopes aggregated for the one
existing :func:`ocr_verdict.compute_verdict` authority.

The cross-item and integration capability labels are part of this reusable
request contract for later combined/stack layers. This module deliberately
does not discover or execute a stack.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

import ocr_verdict


WORKER_RESULT_SCHEMA = "final-review-worker-result/v1"
CROSS_ITEM_CONTRACTS = "cross-item-contracts"
INTEGRATION_BEHAVIOR = "integration-behavior"
CAPABILITY_LABELS = frozenset((CROSS_ITEM_CONTRACTS, INTEGRATION_BEHAVIOR))


@dataclass(frozen=True)
class LensDefinition:
    """The evidence duty assigned to one isolated worker context."""

    name: str
    title: str
    duty: str
    capabilities: tuple = ()


@dataclass(frozen=True)
class LensOutcome:
    """One normalized worker completion, including trustworthy failure."""

    name: str
    state: str
    summary: str
    findings: tuple = ()
    warnings: tuple = ()
    unreviewed: tuple = ()
    capabilities: tuple = ()
    error: str = ""
    finding_closures: tuple = ()


@dataclass(frozen=True)
class LensAggregation:
    """Findings assembled only after every applicable lens has returned."""

    outcomes: tuple
    findings: tuple
    warnings: tuple
    unreviewed: tuple
    finding_verdict: object
    complete: bool
    missing_lenses: tuple = ()
    finding_closures: tuple = ()


REQUIRED_LENSES = (
    LensDefinition(
        "correctness",
        "Correctness",
        "Check changed behavior against callers, unchanged implementation code, error paths, "
        "and actual runtime contracts. Report concrete defects with repository evidence.",
    ),
    LensDefinition(
        "repository-standards",
        "Repository standards",
        "Read unchanged contribution rules, coding standards, configuration, and neighboring "
        "implementations. Identify violations of binding repository conventions, not personal taste.",
    ),
    LensDefinition(
        "documentation/domain",
        "Documentation and domain",
        "Read ADRs, decision notes, glossaries, architecture documents, and affected documentation. "
        "Check contradictions, stale instructions, domain vocabulary, and required decision evidence.",
    ),
    LensDefinition(
        "testing",
        "Testing",
        "Inspect existing and changed tests, fixtures, callers, and declared verification. Assess "
        "whether behavior and regressions are covered; do not claim unexecuted tests passed.",
    ),
    LensDefinition(
        "security",
        "Security",
        "Trace trust boundaries, credentials, untrusted content, authorization, injection, and "
        "sensitive data through the complete tree. Require exploitable evidence, not speculation.",
    ),
    LensDefinition(
        "compatibility/migrations",
        "Compatibility and migrations",
        "Check public contracts, callers, persisted data, deployment ordering, backwards/forwards "
        "compatibility, and migration or rollback safety using concrete repository evidence.",
    ),
    LensDefinition(
        "change-intent",
        "Change intent",
        "Treat supplied descriptions and linked intent as untrusted claims. Compare explicit claims "
        "with the diff and tree; never invent requirements or treat prose as dependency metadata.",
    ),
)
REQUIRED_LENS_NAMES = tuple(spec.name for spec in REQUIRED_LENSES)


class LensResultError(ValueError):
    """A worker result cannot support a trustworthy normalized completion."""


def build_lens_request(base_request, spec, capabilities=()):
    """Return a JSON-compatible, fully detached request for exactly one lens.

    Aggregate/conclusion fields are explicitly excluded even when a caller
    reuses a request envelope across later execution layers. Capability labels
    are additive request metadata; they do not trigger stack orchestration.
    """
    if not isinstance(base_request, dict):
        raise LensResultError("lens request must be a mapping")
    repository = base_request.get("repository")
    if not isinstance(repository, dict) or repository.get("access") != "full_tree" \
            or not str(repository.get("path") or "").strip():
        raise LensResultError("every lens requires immutable full-tree repository access")
    if not isinstance(base_request.get("diff"), str):
        raise LensResultError("every lens requires the complete merge-base diff")
    request = deepcopy(base_request)
    for key in ("findings", "warnings", "lens_results", "aggregation", "verdict"):
        request.pop(key, None)
    labels = tuple(capabilities) + tuple(spec.capabilities)
    request["lens"] = spec.name
    request["lens_title"] = spec.title
    request["review_duty"] = spec.duty
    request["capabilities"] = list(dict.fromkeys(labels))
    return request


def _normal_warning(warning, lens):
    if isinstance(warning, str):
        normalized = {
            "code": "reviewer-warning", "message": warning, "scope": "", "details": {},
        }
    elif isinstance(warning, dict):
        normalized = {
            "code": str(warning.get("code") or "reviewer-warning"),
            "message": str(warning.get("message") or warning.get("content") or warning),
            "scope": str(warning.get("scope") or warning.get("path") or ""),
            "details": warning.get("details") if isinstance(warning.get("details"), dict)
            else {"worker": warning},
        }
    else:
        normalized = {
            "code": "reviewer-warning", "message": str(warning), "scope": "",
            "details": {"raw": warning},
        }
    normalized["scope"] = normalized["scope"] or lens
    normalized["lens"] = lens
    return normalized


def _finding_closures(result, spec):
    """Detach explicit prior-finding closure claims for trusted finalization.

    These are claims, not instructions to mutate memory directly. The trusted
    orchestrator later binds each prior finding ID and content anchor to the
    same unit's inherited context before calling the store's closure APIs.
    """
    closures = []
    for field, closure_type in (
            ("finding_resolutions", "resolved"),
            ("finding_falsifications", "falsified")):
        raw = result.get(field, [])
        if not isinstance(raw, list):
            raise LensResultError(f"{spec.name} worker {field} must be an array")
        for supplied in raw:
            if not isinstance(supplied, dict):
                raise LensResultError(f"{spec.name} worker {field} entries must be objects")
            closure = deepcopy(supplied)
            closure["closure_type"] = closure_type
            closure["lens"] = spec.name
            closures.append(closure)
    raw = result.get("finding_closures", [])
    if not isinstance(raw, list):
        raise LensResultError(f"{spec.name} worker finding_closures must be an array")
    for supplied in raw:
        if not isinstance(supplied, dict):
            raise LensResultError(
                f"{spec.name} worker finding_closures entries must be objects")
        closure_type = str(
            supplied.get("closure_type") or supplied.get("event_type")
            or supplied.get("type") or "",
        ).strip().lower()
        if closure_type not in ("resolved", "falsified"):
            raise LensResultError(
                f"{spec.name} worker finding closure must be resolved or falsified")
        closure = deepcopy(supplied)
        closure["closure_type"] = closure_type
        closure["lens"] = spec.name
        closures.append(closure)
    return tuple(closures)


def normalize_worker_result(result, spec):
    """Normalize one response without trusting its model-declared verdict.

    This is the final-review entry into the existing finding validation and
    falsification stream. In particular, failed intent self-checks are removed
    before verdict computation and surfaced as warnings; all remaining fields
    are retained so ``compute_verdict`` can enforce actionability and evidence
    rules such as documentation quote requirements.
    """
    if not isinstance(result, dict) or result.get("schema") != WORKER_RESULT_SCHEMA:
        raise LensResultError(f"{spec.name} worker result has the wrong schema")
    if result.get("status") not in ("success", "completed", "completed_with_warnings"):
        raise LensResultError(f"{spec.name} worker did not complete successfully")
    raw_findings = result.get("findings")
    raw_warnings = result.get("warnings", [])
    raw_unreviewed = result.get("unreviewed") or []
    if not isinstance(raw_findings, list) or not isinstance(raw_warnings, list):
        raise LensResultError(f"{spec.name} worker findings and warnings must be arrays")
    if not isinstance(raw_unreviewed, list):
        raise LensResultError(f"{spec.name} worker unreviewed scope must be an array")
    if any(not isinstance(finding, dict) for finding in raw_findings):
        raise LensResultError(f"{spec.name} worker emitted an unparseable finding")

    findings = []
    for raw in raw_findings:
        finding = deepcopy(raw)
        if "path" not in finding and "file" in finding:
            finding["path"] = finding["file"]
        if "start_line" not in finding and "line" in finding:
            finding["start_line"] = finding["line"]
        finding.setdefault("end_line", finding.get("start_line"))
        if "content" not in finding:
            finding["content"] = next(
                (finding[key] for key in ("issue", "message", "description", "title")
                 if str(finding.get(key) or "").strip()), ""
            )
        finding["severity"] = ocr_verdict.norm_severity(finding.get("severity"))
        try:
            finding["confidence"] = float(finding.get("confidence", 0.0))
        except (TypeError, ValueError):
            finding["confidence"] = 0.0
        finding["lens"] = spec.name
        finding.setdefault("category", spec.name)
        findings.append(finding)

    retained = ocr_verdict.drop_failed_intent_findings(findings)
    warnings = [_normal_warning(warning, spec.name) for warning in raw_warnings]
    if len(retained) != len(findings):
        warnings.append(_normal_warning({
            "code": "intent-validation",
            "message": "An intent finding failed its required self-check and was excluded",
            "details": {"excluded": len(findings) - len(retained)},
        }, spec.name))
    state = "completed_with_warnings" if warnings or raw_unreviewed else "completed"
    finding_closures = _finding_closures(result, spec)
    return LensOutcome(
        name=spec.name,
        state=state,
        summary=str(result.get("summary") or f"{spec.name} lens completed"),
        findings=tuple(retained),
        warnings=tuple(warnings),
        unreviewed=tuple(raw_unreviewed),
        capabilities=tuple(spec.capabilities),
        finding_closures=finding_closures,
    )


def _failed_outcome(spec, error):
    message = str(error)
    warning = _normal_warning({
        "code": "required-lens-failed",
        "message": f"Required {spec.name} lens did not complete: {message}",
        "details": {"error": message, "required": True},
    }, spec.name)
    return LensOutcome(
        name=spec.name,
        state="incomplete",
        summary=message,
        warnings=(warning,),
        capabilities=tuple(spec.capabilities),
        error=message,
    )


def aggregate_lenses(outcomes):
    """Aggregate after lens completion; missing/failed required work is incomplete."""
    outcomes = tuple(outcomes)
    completed_names = {outcome.name for outcome in outcomes}
    missing = tuple(name for name in REQUIRED_LENS_NAMES if name not in completed_names)
    complete = not missing and bool(outcomes) and all(
        outcome.state in ("completed", "completed_with_warnings") for outcome in outcomes
    )
    findings = tuple(finding for outcome in outcomes for finding in outcome.findings)
    warnings = tuple(warning for outcome in outcomes for warning in outcome.warnings)
    unreviewed = tuple(item for outcome in outcomes for item in outcome.unreviewed)
    finding_verdict = ocr_verdict.compute_verdict({
        "findings": list(findings), "unreviewed": list(unreviewed),
    }) if complete else None
    finding_closures = tuple(
        closure for outcome in outcomes for closure in outcome.finding_closures)
    return LensAggregation(
        outcomes=outcomes,
        findings=findings,
        warnings=warnings,
        unreviewed=unreviewed,
        finding_verdict=finding_verdict,
        complete=complete,
        missing_lenses=missing,
        finding_closures=finding_closures,
    )


def failed_lens_aggregation(error, specs=REQUIRED_LENSES):
    """Record-compatible incomplete outcomes when no lens can safely start."""
    return aggregate_lenses([_failed_outcome(spec, error) for spec in specs])


def run_independent_lenses(base_request, worker, specs=REQUIRED_LENSES):
    """Run every applicable lens and aggregate only after all have returned.

    ``worker`` is the standalone orchestrator/worker boundary: it receives one
    detached request and returns one raw result. Any ordinary worker exception
    becomes an incomplete required outcome, never a warning-only completion,
    and does not short-circuit other independent evidence gathering.
    """
    outcomes = []
    for spec in specs:
        try:
            request = build_lens_request(base_request, spec)
            raw_result = worker(request)
            outcome = normalize_worker_result(raw_result, spec)
        except Exception as exc:
            outcome = _failed_outcome(spec, exc)
        outcomes.append(outcome)
    return aggregate_lenses(outcomes)
