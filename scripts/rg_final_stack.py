"""Complete GitLab individual, combined, and prospective final-stack review.

This is the C14 execution layer behind multi-reference ``run_final_review``.
The older direct stack entry points in :mod:`rg_final_review` remain available
for their static-only and execution-only compatibility contracts. Every unit
here receives complete context/intent, independent static lenses, required and
dynamic checks, finding memory, freshness, and explicit publication gating.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shlex

import rg_final_review as standalone
from rg_final_compose import compose_stack
from rg_final_context import collect_review_context
from rg_final_dynamic import run_dynamic_checks
from rg_final_execution import execute_unit, record_execution_tests, skipped_execution
from rg_final_gitlab import GitLabProvider
from rg_final_graph import discover_linear_graph
from rg_final_lenses import (
    CROSS_ITEM_CONTRACTS,
    INTEGRATION_BEHAVIOR,
    failed_lens_aggregation,
)
from rg_final_materialize import materialize_review
from rg_final_memory import refresh_run_freshness, rerun_context
from rg_final_publish import FinalPublisher
from rg_final_store import FinalStore
from rg_final_telemetry import FinalReviewTelemetry
from rg_final_verification import run_required_verification


def _store_stack(graph):
    """Retain every revision coordinate needed by final freshness checks."""
    root = graph["root_target"]
    return {
        "provider": graph["provider"],
        "target_project": root["project_path"],
        "root_target_ref": root["branch"],
        "root_target_sha": root["sha"],
        "items": [{
            "item_key": item["id"],
            "source_project": item["source"]["project_path"],
            "source_ref": item["source"]["branch"],
            "source_sha": item["source"]["sha"],
            "target_project": item["target"]["project_path"],
            "target_ref": item["target"]["branch"],
            "target_sha": item["target"]["sha"],
            "base_sha": item["target"].get("base_sha"),
            "reference": item["url"],
            "ancestors": deepcopy(item.get("ancestors") or ()),
            "provider_data": standalone._provider_data(item),
        } for item in graph["items"]],
    }


def _context_inputs(graph, item):
    if item is not None:
        return {
            "description": item.get("description"),
            "linked_intent": deepcopy(item.get("linked_intent") or ()),
            "commit_messages": deepcopy(item.get("commit_messages") or ()),
            "branch": item["source"].get("branch"),
        }
    descriptions = []
    linked = []
    messages = []
    for current in graph["items"]:
        title = str(current.get("title") or current["id"])
        description = str(current.get("description") or "").strip()
        descriptions.append(title + ("\n" + description if description else ""))
        linked.extend(deepcopy(current.get("linked_intent") or ()))
        messages.extend(deepcopy(current.get("commit_messages") or ()))
    return {
        "description": "\n\n".join(descriptions),
        "linked_intent": linked,
        "commit_messages": messages,
        "branch": graph["items"][-1]["source"].get("branch"),
    }


def _collect_context(materialized, graph, item, context_kwargs):
    options = dict(context_kwargs or {})
    binary_irrelevance = options.pop("binary_irrelevance", None)
    inputs = _context_inputs(graph, item)
    context = collect_review_context(
        materialized, **inputs, binary_irrelevance=binary_irrelevance, **options,
    )
    required = {
        "contract", "review_mode", "search_roots", "diff", "documents", "files",
        "intent", "completeness", "warnings",
    }
    if not isinstance(context, dict) or not required <= set(context):
        raise ValueError("review context did not satisfy final-review-context/v1")
    return context


def _record_missing_tests(store, unit_id, error):
    for kind in ("required_tests", "dynamic_tests"):
        store.record_coverage(
            unit_id, kind, "incomplete", "Mandatory test evidence did not execute",
            {"error": str(error)},
        )


def _unit_output(unit_id, unit_key, kind, execution, aggregation, *, context_complete=False,
                 intent_complete=False, repository_path="", base_sha="", head_sha="",
                 merge_base_sha=None, composition_complete=True, composition_errors=()):
    checks_complete = bool(
        context_complete and intent_complete and composition_complete
        and standalone._complete_checks(execution)
    )
    verdict = execution.finding_verdict if execution is not None else None
    return {
        "unit_id": unit_id,
        "unit_key": unit_key,
        "kind": kind,
        "state": "incomplete",
        "finding_verdict": verdict,
        "execution_complete": checks_complete,
        "static_lenses_complete": bool(aggregation and aggregation.complete),
        "context_complete": bool(context_complete),
        "intent_complete": bool(intent_complete),
        "base_sha": base_sha,
        "head_sha": head_sha,
        "merge_base_sha": merge_base_sha,
        "repository_path": repository_path,
        "composition_complete": composition_complete,
        "composition_errors": deepcopy(list(composition_errors)),
        "lenses": [{
            "name": outcome.name,
            "state": outcome.state,
            "summary": outcome.summary,
            "capabilities": list(outcome.capabilities),
        } for outcome in (aggregation.outcomes if aggregation else ())],
        "required_verification": deepcopy(execution.required) if execution else None,
        "dynamic_checks": deepcopy(execution.dynamic) if execution else None,
        "_execution": execution,
    }


def _run_unit(store, identity, graph, unit_key, unit_id, *, item=None, composed=None,
              source_repository, target_repository=None, target_sha, head_sha,
              worker, environ, repo_root, required_runner, dynamic_runner,
              dynamic_kwargs, context_kwargs, prior):
    """Execute one fully materialized unit without publishing or revalidation."""
    capabilities = () if item is not None else (CROSS_ITEM_CONTRACTS, INTEGRATION_BEHAVIOR)
    kind = "individual" if item is not None else unit_key
    aggregation = None
    execution = None
    lenses_recorded = False
    context_complete = intent_complete = False
    merge_base = None
    repository_path = ""
    composition_errors = [] if composed is None else composed["errors"]
    composition_complete = True if composed is None else bool(composed["complete"])
    try:
        with materialize_review(
            source_repository, target_sha, head_sha,
            target_repository=(target_repository if target_repository
                               and target_repository != source_repository else None),
            require_lfs=True,
            lfs_irrelevance=(context_kwargs or {}).get("binary_irrelevance"),
        ) as materialized:
            merge_base = materialized.merge_base_sha
            repository_path = str(materialized.path)
            review_context = _collect_context(materialized, graph, item, context_kwargs)
            context_complete, intent_complete = standalone._record_standalone_context(
                store, (unit_id,), review_context)
            store.record_coverage(
                unit_id, "repository", "complete", "Complete immutable checkout available",
                {"submodules": list(materialized.submodules),
                 "lfs_files": list(materialized.lfs_files),
                 "lfs_complete": materialized.lfs_complete},
            )
            store.record_coverage(
                unit_id, "diff", "complete", "Complete binary-capable merge-base diff",
                {"merge_base_sha": merge_base,
                 "sha256": standalone._sha256(materialized.diff)},
            )
            store.record_coverage(
                unit_id, "composition", "not_applicable" if item is not None else "complete",
                "The individual unit uses its provider-pinned tree" if item is not None
                else "The composition unit uses its verified immutable tree",
            )
            unit_prior = {
                **deepcopy(prior),
                "findings": [deepcopy(finding) for finding in prior["findings"]
                             if finding.get("unit_key") == unit_key],
            }
            unit_prior["required"] = bool(unit_prior["findings"])
            public_item = (standalone._public_item(item) if item is not None
                           else standalone._combined_item(graph, unit_key))
            request = {
                "schema": standalone.WORKER_REQUEST_SCHEMA,
                "stage": standalone.COMPLETE_STAGE,
                "run_id": identity["run_id"],
                "unit_id": unit_id,
                "unit_key": unit_key,
                "store_instance_id": identity["store_instance_id"],
                "contract_version": identity["contract_version"],
                "revision_tuple": (list(standalone.revision_tuple(item)) if item is not None
                                   else deepcopy(graph["revisions"])),
                "item": public_item,
                "repository": {
                    "path": str(materialized.path), "access": "full_tree",
                    "head_sha": materialized.head_sha,
                    "target_sha": materialized.target_sha,
                    "merge_base_sha": merge_base,
                    "submodules": list(materialized.submodules),
                    "lfs_files": list(materialized.lfs_files),
                    "lfs_complete": materialized.lfs_complete,
                },
                "diff": materialized.diff,
                "review_context": deepcopy(review_context),
                "finding_memory": unit_prior,
            }
            if composed is not None:
                request["composition"] = {
                    "schema": composed.get("schema", "final-review-composition-unit/v1"),
                    "kind": unit_key,
                    "item_keys": list(graph["item_keys"]),
                    "root_target_sha": graph["root_target"]["sha"],
                    "current_root_sha": composed["target_sha"],
                    "applied_item_keys": list(composed["applied_item_keys"]),
                    "evidence_only_commit": unit_key == "prospective",
                }

            def isolated_worker(lens_request):
                lens_request["capabilities"] = list(capabilities)
                if capabilities:
                    lens_request["review_duty"] += (
                        " Review cross-item contracts and actual integration behavior, not "
                        "just the sum of isolated changes."
                    )
                return standalone.invoke_worker(
                    lens_request, command=worker, repo_root=repo_root, environ=environ,
                    timeout=float(environ.get("RG_FINAL_WORKER_TIMEOUT", "900")),
                )

            execution = execute_unit(
                request, isolated_worker, unit_id=unit_id,
                required_runner=required_runner or run_required_verification,
                dynamic_runner=dynamic_runner or run_dynamic_checks,
                dynamic_kwargs=dynamic_kwargs,
            )
            aggregation = execution.aggregation
            observations = standalone._record_lens_aggregation(
                store, unit_id, aggregation, materialized)
            lenses_recorded = True
            standalone._carry_observed_findings(
                store, identity["run_id"], unit_id, unit_prior, observations)
            record_execution_tests(store, unit_id, execution)
    except Exception as exc:
        if not lenses_recorded:
            aggregation = aggregation or failed_lens_aggregation(exc)
            standalone._record_lens_aggregation(store, unit_id, aggregation)
            lenses_recorded = True
        store.record_coverage(
            unit_id, "repository", "incomplete",
            "Immutable full-tree review did not complete", {"error": str(exc)},
        )
        store.record_warning(unit_id, "materialization", str(exc), "repository")
        if execution is None:
            _record_missing_tests(store, unit_id, exc)
        if composed is not None:
            store.record_coverage(
                unit_id, "composition", "incomplete",
                "The composed unit did not produce complete review evidence",
                {"error": str(exc)},
            )
    output = _unit_output(
        unit_id, unit_key, kind, execution, aggregation,
        context_complete=context_complete, intent_complete=intent_complete,
        repository_path=repository_path, base_sha=target_sha, head_sha=head_sha,
        merge_base_sha=merge_base, composition_complete=composition_complete,
        composition_errors=composition_errors,
    )
    for lens in output["lenses"]:
        lens["capabilities"] = list(capabilities)
    return output


def _skip_unit(store, unit_key, unit_id, stop, *, kind, base_sha, head_sha,
               composition_complete):
    execution = skipped_execution(stop, unit_id=unit_id, run_tests=True)
    standalone._record_lens_aggregation(store, unit_id, execution.aggregation)
    record_execution_tests(store, unit_id, execution)
    store.record_stop(
        unit_id, f"Dependent work skipped after {stop['reason_code']}",
        evidence={"dependent_skip": True, "original_stop": stop},
        affected_unit_ids=[unit_id], skipped_work=execution.skipped_work,
    )
    for coverage in ("review_context", "intent", "repository", "diff", "composition"):
        store.record_coverage(
            unit_id, coverage, "skipped",
            "Dependent evidence was not acquired after a validated critical stop",
            {"reason_code": stop["reason_code"]},
        )
    output = _unit_output(
        unit_id, unit_key, kind, execution, execution.aggregation,
        repository_path="", base_sha=base_sha, head_sha=head_sha,
        composition_complete=composition_complete,
    )
    output["inherited_stop"] = True
    return output


def _incomplete_composition(store, identity, graph, unit_key, unit_id, composed):
    output = standalone._incomplete_composed_unit(
        store, identity, graph, unit_key, unit_id, composed)
    for coverage in ("review_context", "intent", "required_tests", "dynamic_tests"):
        store.record_coverage(
            unit_id, coverage, "incomplete",
            "Composition did not produce a tree on which mandatory evidence could run",
            {"errors": deepcopy(composed["errors"])},
        )
    output.update({
        "execution_complete": False, "context_complete": False, "intent_complete": False,
        "required_verification": None, "dynamic_checks": None,
    })
    output["composition_stop"] = True
    return output


def _execute_composed(store, identity, graph, unit_key, unit_id, composed, **kwargs):
    standalone._record_stack_composition(store, unit_id, composed)
    if not composed["complete"]:
        return _incomplete_composition(
            store, identity, graph, unit_key, unit_id, composed)
    return _run_unit(
        store, identity, graph, unit_key, unit_id, composed=composed,
        source_repository=composed["repository"]["source_repository"],
        target_sha=composed["target_sha"], head_sha=composed["head_sha"], **kwargs,
    )


def _record_origin_stops(store, run_id, units):
    origins = []
    for origin in units:
        execution = origin["_execution"]
        if execution is None or execution.stop is None or origin.get("inherited_stop"):
            continue
        affected = [unit for unit in units if unit["unit_key"] == origin["unit_key"]
                    or unit["unit_key"] in standalone._stop_dependents(origin["unit_key"])]
        for dependent in affected:
            if dependent is origin:
                continue
            execution.skipped_work.extend(deepcopy(
                dependent["_execution"].skipped_work if dependent["_execution"] else ()
            ))
        execution.stop_id = standalone._record_execution_evidence(
            store, origin["unit_id"], execution,
            affected_units=[unit["unit_id"] for unit in affected], record_tests=False,
        )
        origins.append(origin)
    return origins


def _report_paths(store, run_id, unit_key):
    directory = store.path.parent / "reports"
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", unit_key)
    return (str(directory / f"{run_id}-{stem}.json"),
            str(directory / f"{run_id}-{stem}.md"))


def _write_unit_report(store, run_id, unit, orchestration):
    machine_path = Path(unit["machine_report_path"])
    human_path = Path(unit["human_report_path"])
    machine_path.parent.mkdir(parents=True, exist_ok=True)
    report = store.machine_report(run_id, unit["unit_id"])
    report["orchestration"] = orchestration
    temporary = machine_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, machine_path)
    marker = (
        f"\nStage: {standalone.COMPLETE_STAGE}\n"
        + ("Completion: COMPLETE - final state and provider trust are reported separately.\n"
           if orchestration["complete"] else
           "Completion: INCOMPLETE - mandatory stack evidence, freshness, memory, or a stop "
           "prevents a complete final result.\n")
    )
    human = store.human_report(run_id, unit["unit_id"]).replace("\n", marker, 1)
    human_path.write_text(human, encoding="utf-8")
    try:
        os.chmod(human_path, 0o600)
    except OSError:
        pass


def _publication_decisions(store, run_id, individuals, local_waivers,
                           trusted_orchestrator):
    return {
        unit["unit_key"]: store.publication_decision(
            run_id, unit_id=unit["unit_id"], local_waivers=local_waivers,
            trusted_orchestrator=trusted_orchestrator)
        for unit in individuals
    }


def run_final_stack(references, *, repo_root=None, provider=None, store_path=None,
                    worker=None, environ=None, current_root_sha=None,
                    required_runner=None, dynamic_runner=None, dynamic_kwargs=None,
                    context_kwargs=None, publish=False, publication_runner=None,
                    report_url=None, local_waivers=()):
    """Run complete C14 stack orchestration and return detached public evidence.

    Publication is requested for every authoritative completed outcome. Store
    authority permits adverse statuses without relaxing the individual,
    combined, prospective, memory, freshness, and trust gates required for a
    whole-stack pass. The publisher only renders that decision and transports it.
    """
    environ = os.environ if environ is None else environ
    repo_root = str(repo_root or os.getcwd())
    provider = provider or GitLabProvider()
    options = dict(dynamic_kwargs or {})
    configured_executor = environ.get("RG_FINAL_DYNAMIC_EXECUTOR", "").strip()
    if configured_executor:
        options.setdefault("executor", shlex.split(configured_executor))
    options.setdefault("environ", environ)
    telemetry = FinalReviewTelemetry()
    graph = standalone._discover_observed(
        discover_linear_graph, references, provider=provider, repo_root=repo_root,
        telemetry=telemetry,
    )
    items = graph["items"]
    with FinalStore(store_path) as store:
        identity = store.start_run(_store_stack(graph), metadata={
            "stage": standalone.COMPLETE_STAGE, "complete": True, "missing_stages": [],
            "standalone": False,
            "graph": {"schema": graph["schema"], "item_keys": graph["item_keys"]},
        }, telemetry=telemetry)
        run_id = identity["run_id"]
        combined_id = identity["unit_ids"]["combined"]
        prior = rerun_context(store, run_id)
        units = []
        stopped_dependents = {}
        current_sha, current_error = standalone._current_stack_root(
            provider, graph, repo_root, current_root_sha)
        common = {
            "worker": worker, "environ": environ, "repo_root": repo_root,
            "required_runner": required_runner, "dynamic_runner": dynamic_runner,
            "dynamic_kwargs": options, "context_kwargs": context_kwargs, "prior": prior,
        }
        with compose_stack(graph, current_root_sha=current_sha) as composition:
            for position, item in enumerate(items, 1):
                unit_key = f"individual:{position}"
                unit_id = identity["unit_ids"][unit_key]
                if unit_key in stopped_dependents:
                    result = _skip_unit(
                        store, unit_key, unit_id, stopped_dependents[unit_key],
                        kind="individual", base_sha=item["target"]["sha"],
                        head_sha=item["source"]["sha"], composition_complete=True,
                    )
                else:
                    source = item["source"]
                    target = item["target"]
                    result = _run_unit(
                        store, identity, graph, unit_key, unit_id, item=item,
                        source_repository=source.get("http_url") or source["url"],
                        target_repository=(target.get("http_url") or target["url"]),
                        target_sha=target["sha"], head_sha=source["sha"], **common,
                    )
                units.append(result)
                execution = result["_execution"]
                if execution and execution.stop and not result.get("inherited_stop"):
                    for dependent in standalone._stop_dependents(unit_key):
                        stopped_dependents.setdefault(dependent, execution.stop)

            if "combined" in stopped_dependents:
                combined = composition["combined"]
                units.append(_skip_unit(
                    store, "combined", combined_id, stopped_dependents["combined"],
                    kind="combined", base_sha=combined["target_sha"],
                    head_sha=combined["head_sha"], composition_complete=combined["complete"],
                ))
            else:
                units.append(_execute_composed(
                    store, identity, graph, "combined", combined_id,
                    composition["combined"], **common))
            combined_execution = units[-1]["_execution"]
            if combined_execution and combined_execution.stop and \
                    not units[-1].get("inherited_stop"):
                stopped_dependents.setdefault("prospective", combined_execution.stop)

            prospective = composition["prospective"]
            prospective_id = store.add_unit(
                run_id, "prospective", "prospective", prospective["target_sha"],
                prospective["head_sha"], target_ref=graph["root_target"]["branch"],
                metadata={"schema": "final-review-composition-unit/v1",
                          "complete": prospective["complete"],
                          "head_sha_role": "composed" if prospective["complete"]
                          else "pinned_candidate_only", "errors": prospective["errors"]},
            )
            if "prospective" in stopped_dependents:
                units.append(_skip_unit(
                    store, "prospective", prospective_id, stopped_dependents["prospective"],
                    kind="prospective", base_sha=prospective["target_sha"],
                    head_sha=prospective["head_sha"], composition_complete=prospective["complete"],
                ))
            else:
                units.append(_execute_composed(
                    store, identity, graph, "prospective", prospective_id,
                    prospective, **common))

        prospective_output = next(unit for unit in units if unit["unit_key"] == "prospective")
        if current_error is None:
            store.record_coverage(
                prospective_id, "current_target", "complete",
                "Prospective integration uses the refreshed root target",
                {"current_root_sha": current_sha},
            )
        else:
            store.record_coverage(
                prospective_id, "current_target", "incomplete", current_error["message"],
                current_error,
            )
            store.record_warning(
                prospective_id, current_error["code"], current_error["message"],
                "prospective", current_error,
            )

        origin_stops = _record_origin_stops(store, run_id, units)
        has_stops = bool(origin_stops) or any(
            unit.get("inherited_stop") or unit.get("composition_stop") for unit in units)
        freshness = None
        try:
            freshness = refresh_run_freshness(
                store, run_id, provider, references=graph["references"], repo_root=repo_root)
        except Exception as exc:
            for unit in units:
                store.record_warning(unit["unit_id"], "freshness-check", str(exc), "freshness")

        static_complete = all(unit["static_lenses_complete"] for unit in units)
        checks_complete = bool(
            current_error is None and not has_stops
            and all(unit["execution_complete"] for unit in units)
        )
        memory_status = store.memory_status(run_id)
        fresh = bool(freshness and freshness.get("fresh"))
        complete = bool(checks_complete and fresh and memory_status["complete_for_finalization"])
        finding_verdict = None
        if static_complete and not has_stops:
            finding_verdict = store.publication_decision(
                run_id, unit_id=units[0]["unit_id"], local_waivers=local_waivers,
            )["finding_verdict"]
        if (freshness and freshness["state"] == "stale") or (
                current_error and current_error.get("code") == "root-target-moved"):
            state = "stale"
        elif not complete:
            state = "incomplete"
        elif finding_verdict == "block":
            state = "findings"
        elif finding_verdict == "pass" and memory_status["clear_allowed"]:
            state = "pass"
        else:
            state = "incomplete"
            complete = False

        if state == "stale":
            for unit in units:
                store.set_unit_state(
                    unit["unit_id"], "stale", "Pinned stack revisions moved; explicit restart is required")
        elif fresh:
            for unit in units:
                verdict = unit["finding_verdict"]
                unit_state = (
                    "findings" if complete and verdict == "block" else
                    "pass" if complete and verdict == "pass" and state in ("pass", "findings")
                    else "incomplete"
                )
                store.set_unit_state(
                    unit["unit_id"], unit_state,
                    "Complete review evidence is available" if complete
                    else "Mandatory stack review, memory, or test evidence is incomplete",
                )

        publication_permitted = bool(
            publish and not identity["trust"].get("independent"))
        store.finish_run(run_id, state, trust={
            "local_only": not publication_permitted,
            "enforced": publication_permitted,
            "official": publication_permitted,
            "trusted_orchestrator": bool(publish),
            "stage_incomplete": False,
            "missing_stages": [],
            "static_lenses_complete": static_complete,
            "stopped": has_stops,
            "reason": "Complete stack evidence; publication remains explicitly guarded",
        })

        individuals = [unit for unit in units if unit["kind"] == "individual"]
        publications = []
        if publication_permitted:
            publisher = FinalPublisher(
                store, runner=publication_runner, trusted_orchestrator=True,
                repo_root=repo_root,
            )
            for unit in individuals:
                publication = publisher.publish_individual(
                    run_id, unit["unit_id"], report_url=report_url,
                    local_waivers=local_waivers,
                )
                publications.append(publication)
                store.record_coverage(
                    unit["unit_id"], "provider_publication", publication["status"],
                    "C13 publication decision recorded for this stack item",
                    {"item_key": publication["item_key"],
                     "official_green": publication["official_green"],
                     "error": publication["error"]},
                )
        decisions = _publication_decisions(
            store, run_id, individuals, local_waivers,
            trusted_orchestrator=bool(publish),
        )
        attempted = {publication["unit_id"]: publication for publication in publications}
        for unit in individuals:
            if unit["unit_id"] not in attempted:
                decision = decisions[unit["unit_key"]]
                store.record_coverage(
                    unit["unit_id"], "provider_publication",
                    "not_requested" if not publish else "not_eligible",
                    "No provider status write was authorized for the whole-stack outcome",
                    {"status_blockers": decision["status_blockers"],
                     "green_blockers": decision["green_blockers"],
                     "official_green": False},
                )
        combined_state = "complete" if publications and all(
            publication["status"] == "published" for publication in publications) else (
                "local_only" if publications else "not_eligible")
        store.record_coverage(
            combined_id, "provider_publication", combined_state,
            "Every participating item requires its own C13 whole-scope publication decision",
            {"item_count": len(individuals), "attempt_count": len(publications)},
        )
        store.record_coverage(
            prospective_id, "provider_publication", "not_applicable",
            "Prospective evidence-only commits are never provider publication targets",
        )

        for unit in units:
            unit["state"] = next(
                row["state"] for row in store.machine_report(run_id)["units"]
                if row["unit_id"] == unit["unit_id"])
            machine, human = _report_paths(store, run_id, unit["unit_key"])
            unit["machine_report_path"] = machine
            unit["human_report_path"] = human
        combined = next(unit for unit in units if unit["kind"] == "combined")
        prospective_output = next(unit for unit in units if unit["kind"] == "prospective")
        combined["individual_unit_ids"] = [unit["unit_id"] for unit in individuals]
        combined["individual_reports"] = [{
            "unit_id": unit["unit_id"], "unit_key": unit["unit_key"],
            "machine_report_path": unit["machine_report_path"],
            "human_report_path": unit["human_report_path"],
        } for unit in individuals]
        for unit in units:
            unit["combined_report_path"] = combined["machine_report_path"]
            unit["prospective_report_path"] = prospective_output["machine_report_path"]
        prospective_output["individual_reports"] = deepcopy(combined["individual_reports"])

        item_outputs = []
        for position, unit in enumerate(individuals):
            publication = attempted.get(unit["unit_id"])
            decision = decisions[unit["unit_key"]]
            item_outputs.append({
                "item_key": items[position]["id"],
                "reference": items[position]["url"],
                "unit_id": unit["unit_id"],
                "unit_key": unit["unit_key"],
                "state": unit["state"],
                "final_state": state,
                "finding_verdict": unit["finding_verdict"],
                "official_green": bool(publication and publication["official_green"]),
                "green_allowed": decision["can_publish_pass"],
                "machine_report_path": unit["machine_report_path"],
                "human_report_path": unit["human_report_path"],
                "combined_report_path": combined["machine_report_path"],
                "publication": deepcopy(publication),
                "publication_decision": deepcopy(decision),
            })

        for unit in units:
            execution = unit.pop("_execution")
            unit.update(standalone._execution_result_fields(execution))
        exit_code = 3 if state == "stale" else 1 if state == "findings" else 0 if state == "pass" else 2
        trust = store.machine_report(run_id)["trust"]
        result = {
            "schema": standalone.RESULT_SCHEMA,
            "stage": standalone.COMPLETE_STAGE,
            "complete": complete,
            "missing_stages": [],
            "state": state,
            "exit_code": exit_code,
            "finding_verdict": finding_verdict,
            "execution_complete": checks_complete,
            "static_lenses_complete": static_complete,
            "context_complete": all(unit["context_complete"] for unit in units),
            "intent_complete": all(unit["intent_complete"] for unit in units),
            "store_instance_id": identity["store_instance_id"],
            "store_path": str(store.path),
            "run_id": run_id,
            "run_seq": identity["run_seq"],
            "stack_key": identity["stack_key"],
            "unit_id": combined_id,
            "unit_key": "combined",
            "combined_unit_id": combined_id,
            "prospective_unit_id": prospective_id,
            "contract_version": identity["contract_version"],
            "acceptance_version": identity["acceptance_version"],
            "graph": {"schema": graph["schema"], "references": graph["references"],
                      "item_keys": graph["item_keys"]},
            "item_keys": graph["item_keys"],
            "revisions": deepcopy(graph["revisions"]),
            "composition": deepcopy(composition),
            "units": units,
            "items": item_outputs,
            "trust": trust,
            "freshness": deepcopy(freshness),
            "finding_memory": deepcopy(store.memory_status(run_id)),
            "stops": [stop for unit in units for stop in unit["stops"]
                      if not unit.get("inherited_stop")],
            "rejected_stops": [entry for unit in units for entry in unit["rejected_stops"]],
            "skipped_work": [entry for unit in units for entry in unit["skipped_work"]],
            "publications": publications,
            "publication_decisions": deepcopy(decisions),
        }
        report_links = {
            "units": {unit["unit_key"]: {
                "unit_id": unit["unit_id"],
                "machine_report_path": unit["machine_report_path"],
                "human_report_path": unit["human_report_path"],
            } for unit in units},
            "individual_unit_ids": [unit["unit_id"] for unit in individuals],
            "combined_unit_id": combined_id,
            "prospective_unit_id": prospective_id,
        }
        marker = (
            f"\nStage: {standalone.COMPLETE_STAGE}\n"
            + ("Completion: COMPLETE - final state and provider trust are reported separately.\n"
               if complete else
               "Completion: INCOMPLETE - mandatory stack evidence, freshness, memory, or a stop "
               "prevents a complete final result.\n")
        )
        machine_path, human_path = standalone._write_reports(store, run_id, {
            "schema": standalone.RESULT_SCHEMA,
            "stage": standalone.COMPLETE_STAGE,
            "complete": complete,
            "missing_stages": [],
            "state": state,
            "exit_code": exit_code,
            "finding_verdict": finding_verdict,
            "combined_unit_id": combined_id,
            "prospective_unit_id": prospective_id,
            "static_lenses_complete": static_complete,
            "execution_complete": checks_complete,
            "report_links": report_links,
            "publication_decisions": deepcopy(decisions),
        }, completion_marker=marker)
        result["machine_report_path"] = machine_path
        result["human_report_path"] = human_path
        for unit in units:
            _write_unit_report(store, run_id, unit, {
                "schema": standalone.RESULT_SCHEMA,
                "stage": standalone.COMPLETE_STAGE,
                "complete": complete,
                "missing_stages": [],
                "state": state,
                "unit_key": unit["unit_key"],
                "unit_state": unit["state"],
                "finding_verdict": unit["finding_verdict"],
                "execution_complete": unit["execution_complete"],
                "run_machine_report_path": machine_path,
                "report_links": report_links,
                "publication_decision": decisions.get(unit["unit_key"]),
            })
        return result


__all__ = ["run_final_stack"]
