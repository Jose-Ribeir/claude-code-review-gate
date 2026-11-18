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

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from dataclasses import dataclass
import os
import threading
import time

import ocr_verdict


WORKER_RESULT_SCHEMA = "final-review-worker-result/v1"
CROSS_ITEM_CONTRACTS = "cross-item-contracts"
INTEGRATION_BEHAVIOR = "integration-behavior"
CAPABILITY_LABELS = frozenset((CROSS_ITEM_CONTRACTS, INTEGRATION_BEHAVIOR))
DEFAULT_LENS_WORKERS = 4
MAX_LENS_WORKERS = 4
_LENS_START_INTERVAL_SECONDS = 0.05


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

    def __init__(self, message, *, details=None):
        super().__init__(message)
        self.details = deepcopy(details) if isinstance(details, dict) else {}


def _received_status(value):
    """Bound an untrusted status diagnostic without serializing nested content."""
    if value is not None and not isinstance(value, (str, int, float, bool)):
        return f"<{type(value).__name__}>"
    text = str(value)
    return "".join(char if char.isprintable() else " " for char in text)[:128]


def _error_details(error):
    details = getattr(error, "details", {})
    return deepcopy(details) if isinstance(details, dict) else {}


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
        raise LensResultError(
            f"{spec.name} worker result has the wrong schema",
            details=({"received_status": _received_status(result.get("status"))}
                     if isinstance(result, dict) and "status" in result else {}),
        )
    if result.get("status") not in ("success", "completed", "completed_with_warnings"):
        received = _received_status(result.get("status"))
        raise LensResultError(
            f"{spec.name} worker did not complete successfully (status={received!r})",
            details={"received_status": received},
        )
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
    details = {"error": message, **_error_details(error), "required": True}
    warning = _normal_warning({
        "code": "required-lens-failed",
        "message": f"Required {spec.name} lens did not complete: {message}",
        "details": details,
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


def _clamped_lens_workers(value):
    try:
        count = int(str(value).strip()) if value is not None and str(value).strip() else 4
    except (TypeError, ValueError):
        count = DEFAULT_LENS_WORKERS
    return min(MAX_LENS_WORKERS, max(1, count))


def lens_worker_limit(environ=None):
    """Resolve RG_FINAL_LENS_WORKERS, defaulting/clamping to one through four."""
    source = os.environ if environ is None else environ
    return _clamped_lens_workers(source.get("RG_FINAL_LENS_WORKERS", ""))


def _run_bounded_lenses(base_request, worker, specs, *, normalize_result,
                        failed_result, skipped_result=None, max_workers=None, environ=None):
    """Map detached raw requests to outcomes with bounded in-flight workers.

    Futures retain their specification index, so completion order cannot swap a
    raw response onto a different lens. A stopped outcome prevents new requests
    only when the execution layer supplies ``skipped_result``; every already
    running safe peer is joined before this function returns any outcomes.
    """
    specs = tuple(specs)
    if not specs:
        return ()
    limit = (lens_worker_limit(environ) if max_workers is None
             else _clamped_lens_workers(max_workers))
    limit = min(limit, len(specs))
    outcomes = [None] * len(specs)
    futures = {}
    next_index = 0
    stopping = False
    started = [threading.Event() for _spec in specs]

    def invoke(index, spec):
        if index:
            # Preserve deterministic request/startup order without waiting for a
            # response. The short hand-off also keeps external worker audit logs
            # in dispatch order; model execution itself remains fully concurrent.
            started[index - 1].wait()
            time.sleep(_LENS_START_INTERVAL_SECONDS)
        started[index].set()
        return worker(build_lens_request(base_request, spec))

    with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="final-lens") as executor:
        def fill_slots():
            nonlocal next_index
            # With stop handling, the first response is a sentinel: an immediate
            # unsafe STOP launches no additional work. An ordinary/failed sentinel
            # never suppresses the remaining independent evidence.
            slot_limit = (1 if skipped_result is not None and outcomes[0] is None
                          else limit)
            while not stopping and next_index < len(specs) and len(futures) < slot_limit:
                index = next_index
                spec = specs[index]
                future = executor.submit(invoke, index, spec)
                futures[future] = (index, spec)
                next_index += 1

        fill_slots()
        while futures:
            done, _pending = wait(tuple(futures), return_when=FIRST_COMPLETED)
            # Stable processing also makes simultaneous validated STOP selection
            # deterministic; outcome ordering never depends on thread timing.
            for future in sorted(done, key=lambda item: futures[item][0]):
                index, spec = futures.pop(future)
                try:
                    outcome = normalize_result(future.result(), spec)
                except Exception as exc:
                    outcome = failed_result(spec, exc)
                outcomes[index] = outcome
                if outcome.state == "stopped" and skipped_result is not None:
                    stopping = True
            fill_slots()

    if stopping:
        for index, outcome in enumerate(outcomes):
            if outcome is None:
                outcomes[index] = skipped_result(specs[index])
    return tuple(outcomes)


def run_independent_lenses(base_request, worker, specs=REQUIRED_LENSES, *,
                           max_workers=None, environ=None):
    """Run every applicable lens and aggregate only after all have returned.

    ``worker`` is the standalone orchestrator/worker boundary: it receives one
    detached request and returns one raw result. Any ordinary worker exception
    becomes an incomplete required outcome, never a warning-only completion,
    and does not short-circuit other independent evidence gathering. At most
    four requests run concurrently; no provider or checkout work is parallelized.
    """
    outcomes = _run_bounded_lenses(
        base_request, worker, specs, normalize_result=normalize_worker_result,
        failed_result=_failed_outcome, max_workers=max_workers, environ=environ,
    )
    return aggregate_lenses(outcomes)
