"""Standalone orchestration for explicit SPEC-16 final review.

This layer performs every mandatory independent static lens against the
individual immutable tree. Verification, composition, and provider publication
are later contract stages; successful workers therefore never become an
official final pass.

The stable hand-off for later layers is :func:`run_final_review` and the JSON
schemas below.  A worker receives one request file, can inspect the complete
repository and merge-base diff, and returns a result object on stdout.  Model
verdict fields are informational: ``ocr_verdict.compute_verdict`` alone decides
the finding-based verdict from normalized findings.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import ocr_profile
from rg_final_compose import compose_stack
from rg_final_graph import discover_linear_graph
from rg_final_gitlab import GitLabProvider
from rg_final_lenses import (
    CROSS_ITEM_CONTRACTS,
    INTEGRATION_BEHAVIOR,
    REQUIRED_LENS_NAMES,
    failed_lens_aggregation,
    run_independent_lenses,
)
from rg_final_materialize import materialize_review
from rg_final_provider import RevisionChangedError, revision_tuple
from rg_final_store import FinalStore


RESULT_SCHEMA = "final-review-result/v1"
WORKER_REQUEST_SCHEMA = "final-review-worker-request/v1"
WORKER_RESULT_SCHEMA = "final-review-worker-result/v1"
STAGE = "S16-02A"
MISSING_STAGES = [
    "required_and_dynamic_tests",
    "composition",
    "provider_publication",
]
STACK_STAGE = "S16-06B"
STACK_MISSING_STAGES = [
    "required_and_dynamic_tests",
    "provider_publication",
]


class WorkerError(RuntimeError):
    """A lens worker did not produce a trustworthy completed result."""


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


def _public_item(item):
    """Provider metadata safe to persist or disclose to an untrusted worker."""
    return {
        key: item.get(key)
        for key in ("provider", "id", "iid", "url", "title", "description", "state",
                    "author", "linked_intent")
        if key in item
    }


def _provider_data(item):
    data = _public_item(item)
    for side in ("source", "target"):
        data[side] = {
            key: item[side].get(key) for key in ("project_id", "project_path")
        }
    return data


def _stack(item):
    source = item["source"]
    target = item["target"]
    return {
        "provider": item["provider"],
        "target_project": target["project_path"],
        "root_target_ref": target["branch"],
        "root_target_sha": target["sha"],
        "items": [{
            "item_key": item["id"],
            "source_project": source["project_path"],
            "source_ref": source["branch"],
            "source_sha": source["sha"],
            "target_project": target["project_path"],
            "target_ref": target["branch"],
            "target_sha": target["sha"],
            "provider_data": _provider_data(item),
        }],
    }


def worker_environment(environ=None):
    """Return a worker environment without provider-write or unrelated secrets.

    Model-backend authentication remains available, but provider credentials,
    askpass helpers, generic tokens and unrelated secret material do not cross
    the trusted-supervisor/untrusted-worker boundary.
    """
    source = os.environ if environ is None else environ
    child = dict(source)
    model_auth = ("ANTHROPIC_", "CLAUDE_", "KIMI_", "OPENAI_", "PI_")
    sensitive = ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "COOKIE",
                 "PRIVATE_KEY", "ASKPASS", "AUTH")
    for key in list(child):
        upper = key.upper()
        provider = any(part in upper for part in ("GITLAB", "GITHUB", "GLAB_", "GH_"))
        generic_secret = any(part in upper for part in sensitive)
        if provider or (generic_secret and not upper.startswith(model_auth)):
            child.pop(key, None)
    for key in ("SSH_AUTH_SOCK", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_CONFIG_COUNT"):
        child.pop(key, None)
    child["RG_FINAL_CREDENTIAL_FREE"] = "1"
    return child


def _default_worker_command(request_path, repo_root, environ, request):
    selection = ocr_profile.select_backend(env=environ, repo_root=repo_root)
    executable = shutil.which(selection.name, path=environ.get("PATH"))
    if not executable:
        raise WorkerError(f"selected review backend {selection.name!r} is not on PATH")
    prompt = (
        f"Perform one independent {request['lens']} static review. Read the JSON request at "
        f"{request_path}. Its repository.path is a complete immutable checkout and its "
        "diff is the complete merge-base patch. Iteratively inspect unchanged code, "
        "docs, callers and tests as needed. Your assigned evidence duty is: "
        f"{request['review_duty']} Do not use or wait for another lens's conclusions. "
        f"Return only a {WORKER_RESULT_SCHEMA} JSON object with status, summary, findings "
        "and warnings; do not trust your own verdict field to determine the gate result."
    )
    if selection.name == "claude":
        args = [
            "--output-format", "stream-json", "--verbose", "--setting-sources", "",
            "--allowedTools", "Bash(git diff *)", "Bash(git show *)",
            "Bash(git status *)", "Read", "Grep", "Glob",
            "--disallowedTools", "Write", "Edit", "WebFetch", "WebSearch",
            "--mcp-config", '{"mcpServers":{}}', "--strict-mcp-config",
        ]
    else:
        args = ["--mode", "json", "--no-session", "--approve",
                "--mcp-config", '{"mcpServers":{}}']
    return [executable, "-p", prompt, *args]


def _object_from_text(text):
    """Extract a result object from plain, fenced, or stream-json worker text."""
    if not isinstance(text, str) or not text.strip():
        return None
    stripped = text.strip()
    try:
        value = json.loads(stripped)
    except ValueError:
        value = None
    if isinstance(value, dict) and value.get("schema") == WORKER_RESULT_SCHEMA:
        return value
    for block in re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL):
        found = _object_from_text(block)
        if found is not None:
            return found
    for line in stripped.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if isinstance(event.get("result"), str):
            found = _object_from_text(event["result"])
            if found is not None:
                return found
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content or []:
            if isinstance(block, dict) and block.get("type") == "text":
                found = _object_from_text(block.get("text", ""))
                if found is not None:
                    return found
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", stripped):
        try:
            value, _ = decoder.raw_decode(stripped[match.start():])
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("schema") == WORKER_RESULT_SCHEMA:
            return value
    return None


def invoke_worker(request, *, command=None, repo_root=None, environ=None, timeout=900):
    """Execute one credential-isolated worker and decode its contracted result."""
    source = os.environ if environ is None else environ
    with tempfile.TemporaryDirectory(prefix="review-gate-final-request-") as directory:
        request_path = Path(directory) / "request.json"
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(request_path, 0o600)
        except OSError:
            pass
        if command is None:
            configured = source.get("RG_FINAL_WORKER", "").strip()
            if configured:
                command = shlex.split(configured)
                replaced = [part.replace("{request}", str(request_path)) for part in command]
                command = replaced if replaced != command else [*command, str(request_path)]
            else:
                command = _default_worker_command(str(request_path), repo_root, source, request)
        elif any("{request}" in str(part) for part in command):
            command = [str(part).replace("{request}", str(request_path)) for part in command]
        else:
            command = [*(str(part) for part in command), str(request_path)]
        try:
            process = subprocess.run(
                command, cwd=str(request["repository"]["path"]),
                env=worker_environment(source), stdin=subprocess.DEVNULL,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=float(timeout), check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorkerError(f"{request['lens']} worker could not run: {exc}") from exc
    if process.returncode:
        raise WorkerError(f"{request['lens']} worker exited {process.returncode}")
    result = _object_from_text(process.stdout)
    if result is None:
        raise WorkerError(f"{request['lens']} worker returned no valid contracted JSON result")
    return result


def _finding_key(finding, used):
    base = str(finding.get("id") or finding.get("finding_key") or "").strip()
    if not base:
        base = str(finding.get("lens") or "static") + "-" + _sha256(json.dumps(
            finding, sort_keys=True, ensure_ascii=False, default=str))[:16]
    key = base
    suffix = 2
    while key in used:
        key = f"{base}-{suffix}"
        suffix += 1
    used.add(key)
    return key


def _line(finding):
    try:
        value = int(finding.get("start_line"))
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def _record_pending(store, unit_id):
    for kind, summary in (
        ("required_tests", "Repository-required verification is reserved for a later stage"),
        ("dynamic_tests", "Dynamic checks are reserved for a later stage"),
        ("composition", "Combined and prospective composition are reserved for later stages"),
        ("provider_publication", "No official provider status is emitted by this stage"),
    ):
        status = "incomplete" if kind == "mandatory_lenses" else "not_implemented"
        store.record_coverage(unit_id, kind, status, summary,
                              {"stage": STAGE, "complete": False})


def _record_finding(store, unit_id, finding, key, diff):
    raw = dict(finding)
    lens = str(finding.get("lens") or "static")
    evidence_text = str(finding.get("evidence") or finding.get("existing_code") or "")
    anchors = [{
        "anchor_type": "content",
        "anchor": "sha256:" + _sha256(json.dumps(
            raw, sort_keys=True, ensure_ascii=False, default=str)),
        "evidence_fingerprint": _sha256(diff + "\0" + evidence_text),
        "details": {"path": finding.get("path"), "line": _line(finding)},
    }]
    store.record_finding(
        unit_id, key, str(finding.get("content") or ""),
        str(finding.get("severity") or ""), str(finding.get("confidence", 0.0)),
        lens=lens, path=finding.get("path"), line=_line(finding),
        anchors=anchors, evidence={"worker": raw, "lens": lens},
    )


def _record_lens_aggregation(store, unit_id, aggregation, materialized=None):
    """Persist every lens completion and every normalized warning/findings stream."""
    common = {
        "request_schema": WORKER_REQUEST_SCHEMA,
        "result_schema": WORKER_RESULT_SCHEMA,
        "repository_access": "full_tree",
    }
    if materialized is not None:
        common.update({
            "merge_base_sha": materialized.merge_base_sha,
            "diff_bytes": len(materialized.diff.encode("utf-8", "surrogateescape")),
            "diff_sha256": _sha256(materialized.diff),
            "submodules": list(materialized.submodules),
            "lfs_files": list(materialized.lfs_files),
            "lfs_complete": materialized.lfs_complete,
        })
    used = set()
    for outcome in aggregation.outcomes:
        evidence = {
            **common,
            "summary": outcome.summary,
            "finding_verdict": aggregation.finding_verdict if aggregation.complete else None,
            "warning_count": len(outcome.warnings),
            "unreviewed": list(outcome.unreviewed),
            "capabilities": list(outcome.capabilities),
            "independent_context": True,
            "error": outcome.error,
        }
        store.record_lens(unit_id, outcome.name, outcome.state, outcome.summary, evidence)
        store.record_coverage(unit_id, f"lens:{outcome.name}", outcome.state,
                              outcome.summary, {"evidence": evidence})
        for finding in outcome.findings:
            _record_finding(store, unit_id, finding, _finding_key(finding, used),
                            materialized.diff if materialized is not None else "")
        for warning in outcome.warnings:
            details = dict(warning["details"])
            details["lens"] = warning["lens"]
            store.record_warning(unit_id, warning["code"], warning["message"],
                                 warning["scope"], details)
    state = "complete" if aggregation.complete else "incomplete"
    summary = ("Every mandatory independent static lens completed" if aggregation.complete else
               "One or more mandatory independent static lenses did not complete")
    store.record_coverage(unit_id, "mandatory_lenses", state, summary, {
        "complete": aggregation.complete,
        "missing_lenses": list(aggregation.missing_lenses),
        "incomplete_lenses": [outcome.name for outcome in aggregation.outcomes
                              if outcome.state == "incomplete"],
        "aggregate_after_completion": True,
    })


def _write_reports(store, run_id, orchestration, completion_marker=None):
    report = store.machine_report(run_id)
    report["orchestration"] = orchestration
    directory = store.path.parent / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    machine_path = directory / f"{run_id}.json"
    human_path = directory / f"{run_id}.md"
    temporary = machine_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, machine_path)
    human = store.human_report(run_id)
    marker = completion_marker or (
        f"\nStage: {STAGE}\n"
        "Completion: INCOMPLETE - this stage runs every individual static lens independently. "
        "Tests, composition and provider publication are not implemented.\n"
    )
    human = human.replace("\n", marker, 1)
    human_path.write_text(human, encoding="utf-8")
    try:
        os.chmod(human_path, 0o600)
    except OSError:
        pass
    return str(machine_path), str(human_path)


def run_final_review(reference, *, repo_root=None, provider=None, store_path=None,
                     worker=None, environ=None):
    """Review one provider-pinned ref and return the versioned operator result.

    ``worker`` is an optional argv prefix (``{request}`` placeholder supported)
    for scripted or alternate workers.  The default uses ``RG_FINAL_WORKER`` or
    the selected normal review backend.  No provider mutation method is called.
    """
    if isinstance(reference, (list, tuple)):
        return run_final_stack(
            reference, repo_root=repo_root, provider=provider, store_path=store_path,
            worker=worker, environ=environ,
        )
    environ = os.environ if environ is None else environ
    repo_root = str(repo_root or os.getcwd())
    provider = provider or GitLabProvider()
    item = provider.read(reference, repo_root=repo_root)
    revisions = list(revision_tuple(item))
    source = item["source"]
    target = item["target"]
    with FinalStore(store_path) as store:
        identity = store.start_run(_stack(item), metadata={
            "stage": STAGE,
            "complete": False,
            "missing_stages": MISSING_STAGES,
            "standalone": True,
        })
        run_id = identity["run_id"]
        unit_id = identity["unit_ids"]["individual:1"]
        combined_id = identity["unit_ids"]["combined"]
        _record_pending(store, unit_id)
        _record_pending(store, combined_id)
        store.record_coverage(
            combined_id, "mandatory_lenses", "not_implemented",
            "Combined and stack execution are reserved for a later layer",
        )
        finding_verdict = None
        merge_base = None
        state = "incomplete"
        aggregation = None
        lenses_recorded = False
        try:
            source_repository = source.get("http_url") or source["url"]
            target_repository = target.get("http_url") or target["url"]
            with materialize_review(
                source_repository, target["sha"], source["sha"],
                target_repository=(target_repository
                                   if target_repository != source_repository else None),
                require_lfs=True,
            ) as materialized:
                merge_base = materialized.merge_base_sha
                store.record_coverage(
                    unit_id, "repository", "complete", "Complete immutable checkout available",
                    {"submodules": list(materialized.submodules),
                     "lfs_files": list(materialized.lfs_files),
                     "lfs_complete": materialized.lfs_complete},
                )
                store.record_coverage(
                    unit_id, "diff", "complete", "Complete binary-capable merge-base diff",
                    {"merge_base_sha": merge_base, "sha256": _sha256(materialized.diff)},
                )
                request = {
                    "schema": WORKER_REQUEST_SCHEMA,
                    "stage": STAGE,
                    "run_id": run_id,
                    "unit_id": unit_id,
                    "store_instance_id": identity["store_instance_id"],
                    "contract_version": identity["contract_version"],
                    "revision_tuple": revisions,
                    "item": _public_item(item),
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
                }
                def isolated_worker(lens_request):
                    return invoke_worker(
                        lens_request, command=worker, repo_root=repo_root, environ=environ,
                        timeout=float(environ.get("RG_FINAL_WORKER_TIMEOUT", "900")),
                    )

                # This returns only after every required lens has completed or
                # failed. Blocking findings never short-circuit other evidence.
                aggregation = run_independent_lenses(request, isolated_worker)
                finding_verdict = aggregation.finding_verdict
                _record_lens_aggregation(store, unit_id, aggregation, materialized)
                lenses_recorded = True
                try:
                    provider.revalidate(item, repo_root=repo_root)
                except RevisionChangedError as exc:
                    state = "stale"
                    store.record_warning(unit_id, "revision-stale", str(exc), "freshness", {
                        "field": exc.field, "expected": exc.expected, "current": exc.current,
                    })
                except Exception as exc:  # freshness cannot be inferred as valid
                    store.record_warning(unit_id, "freshness-check", str(exc), "freshness")
        except Exception as exc:
            # Materialization and unexpected execution failures are never a
            # reduced-scope pass; the clone context has already been cleaned.
            if not lenses_recorded:
                aggregation = aggregation or failed_lens_aggregation(exc)
                _record_lens_aggregation(store, unit_id, aggregation)
                lenses_recorded = True
            store.record_coverage(unit_id, "repository", "incomplete",
                                  "Immutable full-tree review did not complete", {"error": str(exc)})
            store.record_warning(unit_id, "materialization", str(exc), "repository")
        if state == "stale":
            unit_state = "stale"
        elif finding_verdict == "block":
            unit_state = "findings"
        else:
            unit_state = "incomplete"
        static_lenses_complete = bool(aggregation and aggregation.complete)
        summary = ("Static-lens findings block this incomplete standalone review"
                   if unit_state == "findings" else
                   "Independent static lenses completed; verification and composition are pending"
                   if static_lenses_complete else
                   "A required static lens is incomplete; no final pass is available")
        store.set_unit_state(unit_id, unit_state, summary)
        store.set_unit_state(combined_id, "incomplete",
                             "Combined review is not implemented in this stage")
        trust = store.finish_run(run_id, state, trust={
            "local_only": True, "enforced": False, "official": False,
            "stage_incomplete": True, "missing_stages": MISSING_STAGES,
            "static_lenses_complete": static_lenses_complete,
            "reason": "C5 performs independent static lenses but never verifies, composes or publishes",
        })
        if state == "stale":
            exit_code = 3
        elif finding_verdict == "block":
            exit_code = 1
        else:
            exit_code = 2
        result = {
            "schema": RESULT_SCHEMA,
            "stage": STAGE,
            "complete": False,
            "missing_stages": MISSING_STAGES,
            "state": state,
            "exit_code": exit_code,
            "finding_verdict": finding_verdict,
            "static_lenses_complete": static_lenses_complete,
            "lenses": [{"name": outcome.name, "state": outcome.state,
                        "summary": outcome.summary, "capabilities": list(outcome.capabilities)}
                       for outcome in (aggregation.outcomes if aggregation else ())],
            "store_instance_id": identity["store_instance_id"],
            "store_path": str(store.path),
            "run_id": run_id,
            "run_seq": identity["run_seq"],
            "stack_key": identity["stack_key"],
            "unit_id": unit_id,
            "unit_key": "individual:1",
            "combined_unit_id": combined_id,
            "contract_version": identity["contract_version"],
            "acceptance_version": identity["acceptance_version"],
            "revision_tuple": revisions,
            "revisions": {
                "source_sha": source["sha"], "target_sha": target["sha"],
                "provider_base_sha": target["base_sha"], "merge_base_sha": merge_base,
            },
            "trust": trust,
        }
        machine_path, human_path = _write_reports(store, run_id, {
            "schema": RESULT_SCHEMA, "stage": STAGE, "complete": False,
            "missing_stages": MISSING_STAGES, "state": state, "exit_code": exit_code,
            "finding_verdict": finding_verdict, "unit_id": unit_id,
            "static_lenses_complete": static_lenses_complete,
        })
        result["machine_report_path"] = machine_path
        result["human_report_path"] = human_path
        return result


def _graph_stack(graph):
    """Translate validated provider metadata without changing unit identity."""
    items = graph["items"]
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
            "provider_data": _provider_data(item),
        } for item in items],
    }


def _record_stack_pending(store, unit_id):
    for kind, summary in (
        ("required_tests", "Repository-required verification is reserved for a later stage"),
        ("dynamic_tests", "Dynamic checks are reserved for a later stage"),
        ("provider_publication", "No official provider status is emitted by this stage"),
    ):
        store.record_coverage(unit_id, kind, "not_implemented", summary,
                              {"stage": STACK_STAGE, "complete": False})


def _current_stack_root(provider, graph, repo_root, explicit_sha):
    if explicit_sha is not None:
        return str(explicit_sha), None
    root = graph["root_target"]
    try:
        fresh = provider.read(graph["references"][0], repo_root=repo_root)
        current = fresh["target"]
        identity = ("project_id", "project_path", "branch")
        if any(current.get(key) != root.get(key) for key in identity):
            return root["sha"], {
                "code": "root-target-moved", "message": "The root target moved during discovery",
                "pinned": {key: root.get(key) for key in identity},
                "current": {key: current.get(key) for key in identity},
            }
        return current["sha"], None
    except Exception as exc:
        return root["sha"], {
            "code": "current-target-unavailable",
            "message": f"The current root target could not be refreshed: {exc}",
        }


def _combined_item(graph, unit_key):
    return {
        "provider": graph["provider"], "id": unit_key,
        "title": ("Ordered combined stack" if unit_key == "combined"
                  else "Prospective root-target integration"),
        "description": "Review cross-item contracts and integration behavior across the full diff.",
        "items": [_public_item(item) for item in graph["items"]],
    }


def _record_stack_composition(store, unit_id, composed):
    state = "complete" if composed["complete"] else "incomplete"
    store.record_coverage(unit_id, "composition", state,
                          "Ordered composition is reproducible" if composed["complete"]
                          else "Composition conflict or unreproducibility prevents review",
                          {"errors": composed["errors"],
                           "applied_item_keys": composed["applied_item_keys"]})


def _run_stack_unit(store, identity, graph, unit_key, unit_id, *, item=None,
                    composed=None, source_repository, target_repository=None,
                    target_sha, head_sha, worker, environ, repo_root):
    """Materialize and run every mandatory lens, returning detached unit output."""
    capabilities = () if composed is None else (
        (CROSS_ITEM_CONTRACTS, INTEGRATION_BEHAVIOR) if unit_key == "combined"
        else (INTEGRATION_BEHAVIOR,)
    )
    aggregation = None
    merge_base = None
    repository_path = ""
    try:
        with materialize_review(
            source_repository, target_sha, head_sha,
            target_repository=(target_repository if target_repository
                               and target_repository != source_repository else None),
            require_lfs=True,
        ) as materialized:
            merge_base = materialized.merge_base_sha
            repository_path = str(materialized.path)
            store.record_coverage(
                unit_id, "repository", "complete", "Complete immutable checkout available",
                {"submodules": list(materialized.submodules),
                 "lfs_files": list(materialized.lfs_files),
                 "lfs_complete": materialized.lfs_complete},
            )
            store.record_coverage(
                unit_id, "diff", "complete", "Complete binary-capable merge-base diff",
                {"merge_base_sha": merge_base, "sha256": _sha256(materialized.diff)},
            )
            request = {
                "schema": WORKER_REQUEST_SCHEMA, "stage": STACK_STAGE,
                "run_id": identity["run_id"], "unit_id": unit_id, "unit_key": unit_key,
                "store_instance_id": identity["store_instance_id"],
                "contract_version": identity["contract_version"],
                "revision_tuple": (list(revision_tuple(item)) if item is not None
                                   else graph["revisions"]),
                "item": (_public_item(item) if item is not None
                         else _combined_item(graph, unit_key)),
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
            }
            if composed is not None:
                request["composition"] = {
                    "schema": composed.get("schema", "final-review-composition-unit/v1"),
                    "kind": unit_key, "item_keys": graph["item_keys"],
                    "root_target_sha": graph["root_target"]["sha"],
                    "current_root_sha": composed["target_sha"],
                    "applied_item_keys": composed["applied_item_keys"],
                    "evidence_only_commit": unit_key == "prospective",
                }

            def isolated_worker(lens_request):
                lens_request["capabilities"] = list(capabilities)
                if capabilities:
                    lens_request["review_duty"] += (
                        " Review cross-item contracts and actual integration behavior, not "
                        "just the sum of isolated changes."
                    )
                return invoke_worker(
                    lens_request, command=worker, repo_root=repo_root, environ=environ,
                    timeout=float(environ.get("RG_FINAL_WORKER_TIMEOUT", "900")),
                )

            aggregation = run_independent_lenses(request, isolated_worker)
            _record_lens_aggregation(store, unit_id, aggregation, materialized)
    except Exception as exc:
        if aggregation is None:
            aggregation = failed_lens_aggregation(exc)
            _record_lens_aggregation(store, unit_id, aggregation)
        store.record_coverage(unit_id, "repository", "incomplete",
                              "Immutable full-tree review did not complete", {"error": str(exc)})
        store.record_warning(unit_id, "materialization", str(exc), "repository")
    verdict = aggregation.finding_verdict if aggregation is not None else None
    state = "findings" if verdict == "block" else "incomplete"
    complete = bool(aggregation and aggregation.complete)
    summary = ("Static-lens findings block this incomplete stack review" if verdict == "block"
               else "Independent static lenses completed; tests and publication are pending"
               if complete else "A required static lens is incomplete; no final pass is available")
    store.set_unit_state(unit_id, state, summary)
    return {
        "unit_id": unit_id, "unit_key": unit_key,
        "kind": ("individual" if item is not None else unit_key),
        "state": state, "finding_verdict": verdict,
        "static_lenses_complete": complete, "base_sha": target_sha, "head_sha": head_sha,
        "merge_base_sha": merge_base, "repository_path": repository_path,
        "composition_complete": True if composed is None else composed["complete"],
        "composition_errors": [] if composed is None else composed["errors"],
        "lenses": [{"name": outcome.name, "state": outcome.state,
                    "summary": outcome.summary, "capabilities": list(capabilities)}
                   for outcome in (aggregation.outcomes if aggregation else ())],
    }


def _incomplete_composed_unit(store, identity, graph, unit_key, unit_id, composed):
    message = "; ".join(error["message"] for error in composed["errors"]) or \
        "Composition did not produce a reviewable tree"
    aggregation = failed_lens_aggregation(message)
    _record_lens_aggregation(store, unit_id, aggregation)
    store.record_coverage(unit_id, "repository", "incomplete",
                          "No immutable composed tree is available")
    store.record_coverage(unit_id, "diff", "incomplete",
                          "No complete composed merge-base diff is available")
    store.record_warning(unit_id, "composition-incomplete", message, unit_key,
                         {"errors": composed["errors"]})
    store.record_stop(
        unit_id, message, evidence={"composition": composed},
        affected_unit_ids=[unit_id],
        skipped_work=[{"unit_id": unit_id, "kind": "lens", "name": name}
                      for name in REQUIRED_LENS_NAMES],
    )
    store.set_unit_state(unit_id, "incomplete", "Composition is incomplete; no official pass")
    return {
        "unit_id": unit_id, "unit_key": unit_key, "kind": unit_key,
        "state": "incomplete", "finding_verdict": None,
        "static_lenses_complete": False, "base_sha": composed["target_sha"],
        "head_sha": composed["head_sha"], "merge_base_sha": None,
        "repository_path": "", "composition_complete": False,
        "composition_errors": composed["errors"],
        "lenses": [{"name": outcome.name, "state": outcome.state,
                    "summary": outcome.summary, "capabilities": []}
                   for outcome in aggregation.outcomes],
    }


def _execute_composed_unit(store, identity, graph, unit_key, unit_id, composed,
                           worker, environ, repo_root):
    _record_stack_composition(store, unit_id, composed)
    if not composed["complete"]:
        return _incomplete_composed_unit(
            store, identity, graph, unit_key, unit_id, composed)
    return _run_stack_unit(
        store, identity, graph, unit_key, unit_id, composed=composed,
        source_repository=composed["repository"]["source_repository"],
        target_sha=composed["target_sha"], head_sha=composed["head_sha"],
        worker=worker, environ=environ, repo_root=repo_root,
    )


def _write_stack_unit_report(store, run_id, unit, links):
    directory = store.path.parent / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", unit["unit_key"])
    machine_path = directory / f"{run_id}-{stem}.json"
    human_path = directory / f"{run_id}-{stem}.md"
    report = store.machine_report(run_id, unit["unit_id"])
    report["orchestration"] = {"stage": STACK_STAGE, "complete": False, **links}
    temporary = machine_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True,
                                    ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, machine_path)
    human = store.human_report(run_id, unit["unit_id"])
    marker = (f"\nStage: {STACK_STAGE}\nCompletion: INCOMPLETE - required/dynamic tests "
              "and official publication remain pending.\n")
    human_path.write_text(human.replace("\n", marker, 1), encoding="utf-8")
    for path in (machine_path, human_path):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    unit["machine_report_path"] = str(machine_path)
    unit["human_report_path"] = str(human_path)


def run_final_stack(references, *, repo_root=None, provider=None, store_path=None,
                    worker=None, environ=None, current_root_sha=None):
    """Validate and review a complete base-first stack through composition.

    The returned units and per-unit report paths are the C14 publication
    hand-off. Dynamic/required tests and official provider statuses deliberately
    remain pending, so a clean static result still exits incomplete.
    """
    environ = os.environ if environ is None else environ
    repo_root = str(repo_root or os.getcwd())
    provider = provider or GitLabProvider()
    graph = discover_linear_graph(references, provider=provider, repo_root=repo_root)
    items = graph["items"]
    with FinalStore(store_path) as store:
        identity = store.start_run(_graph_stack(graph), metadata={
            "stage": STACK_STAGE, "complete": False,
            "missing_stages": STACK_MISSING_STAGES, "standalone": False,
            "graph": {"schema": graph["schema"], "item_keys": graph["item_keys"]},
        })
        run_id = identity["run_id"]
        combined_id = identity["unit_ids"]["combined"]
        unit_results = []
        for position, item in enumerate(items, 1):
            unit_id = identity["unit_ids"][f"individual:{position}"]
            _record_stack_pending(store, unit_id)
        _record_stack_pending(store, combined_id)
        current_sha, current_error = _current_stack_root(
            provider, graph, repo_root, current_root_sha)

        with compose_stack(graph, current_root_sha=current_sha) as composition:
            for position, item in enumerate(items, 1):
                unit_key = f"individual:{position}"
                source = item["source"]
                target = item["target"]
                source_repository = source.get("http_url") or source["url"]
                target_repository = target.get("http_url") or target["url"]
                unit_results.append(_run_stack_unit(
                    store, identity, graph, unit_key, identity["unit_ids"][unit_key],
                    item=item, source_repository=source_repository,
                    target_repository=target_repository, target_sha=target["sha"],
                    head_sha=source["sha"], worker=worker, environ=environ,
                    repo_root=repo_root,
                ))
            unit_results.append(_execute_composed_unit(
                store, identity, graph, "combined", combined_id,
                composition["combined"], worker, environ, repo_root))
            prospective = composition["prospective"]
            prospective_id = store.add_unit(
                run_id, "prospective", "prospective", prospective["target_sha"],
                prospective["head_sha"], target_ref=graph["root_target"]["branch"],
                metadata={"schema": "final-review-composition-unit/v1",
                          "complete": prospective["complete"],
                          "head_sha_role": "composed" if prospective["complete"]
                          else "pinned_candidate_only",
                          "errors": prospective["errors"]},
            )
            _record_stack_pending(store, prospective_id)
            unit_results.append(_execute_composed_unit(
                store, identity, graph, "prospective", prospective_id,
                prospective, worker, environ, repo_root))

        if current_error is not None:
            store.record_coverage(unit_results[-1]["unit_id"], "current_target", "incomplete",
                                  current_error["message"], current_error)
            store.record_warning(unit_results[-1]["unit_id"], current_error["code"],
                                 current_error["message"], "prospective", current_error)
        else:
            store.record_coverage(unit_results[-1]["unit_id"], "current_target", "complete",
                                  "Prospective integration uses the refreshed root target",
                                  {"current_root_sha": current_sha})

        stale = current_error is not None and current_error.get("code") == "root-target-moved"
        for position, item in enumerate(items, 1):
            unit_id = identity["unit_ids"][f"individual:{position}"]
            try:
                provider.revalidate(item, repo_root=repo_root)
            except RevisionChangedError as exc:
                stale = True
                store.record_warning(unit_id, "revision-stale", str(exc), "freshness", {
                    "field": exc.field, "expected": exc.expected, "current": exc.current,
                })
            except Exception as exc:
                store.record_warning(unit_id, "freshness-check", str(exc), "freshness")
        if stale:
            for unit in unit_results:
                unit["state"] = "stale"
                store.set_unit_state(unit["unit_id"], "stale",
                                     "Pinned stack revisions moved; explicit restart is required")

        verdicts = [unit["finding_verdict"] for unit in unit_results]
        finding_verdict = ("block" if "block" in verdicts
                           else "pass" if verdicts and all(v == "pass" for v in verdicts)
                           else None)
        static_complete = all(unit["static_lenses_complete"] for unit in unit_results)
        state = "stale" if stale else "incomplete"
        trust = store.finish_run(run_id, state, trust={
            "local_only": True, "enforced": False, "official": False,
            "stage_incomplete": True, "missing_stages": STACK_MISSING_STAGES,
            "static_lenses_complete": static_complete,
            "reason": "C10 composes and reviews statically; tests and official publication remain",
        })
        exit_code = 3 if stale else 1 if finding_verdict == "block" else 2
        result = {
            "schema": RESULT_SCHEMA, "stage": STACK_STAGE, "complete": False,
            "missing_stages": STACK_MISSING_STAGES, "state": state, "exit_code": exit_code,
            "finding_verdict": finding_verdict, "static_lenses_complete": static_complete,
            "store_instance_id": identity["store_instance_id"], "store_path": str(store.path),
            "run_id": run_id, "run_seq": identity["run_seq"],
            "stack_key": identity["stack_key"], "unit_id": combined_id,
            "unit_key": "combined", "combined_unit_id": combined_id,
            "prospective_unit_id": prospective_id,
            "contract_version": identity["contract_version"],
            "acceptance_version": identity["acceptance_version"],
            "graph": {"schema": graph["schema"], "references": graph["references"],
                      "item_keys": graph["item_keys"]},
            "item_keys": graph["item_keys"], "revisions": graph["revisions"],
            "composition": composition, "units": unit_results, "trust": trust,
        }
        marker = (f"\nStage: {STACK_STAGE}\nCompletion: INCOMPLETE - composition and all static "
                  "lenses ran; required/dynamic tests and official publication remain pending.\n")
        machine_path, human_path = _write_reports(store, run_id, {
            "schema": RESULT_SCHEMA, "stage": STACK_STAGE, "complete": False,
            "missing_stages": STACK_MISSING_STAGES, "state": state,
            "exit_code": exit_code, "finding_verdict": finding_verdict,
            "combined_unit_id": combined_id, "prospective_unit_id": prospective_id,
            "static_lenses_complete": static_complete,
        }, completion_marker=marker)
        result["machine_report_path"] = machine_path
        result["human_report_path"] = human_path
        for unit in unit_results:
            _write_stack_unit_report(store, run_id, unit, {
                "run_machine_report_path": machine_path,
                "combined_unit_id": combined_id,
                "individual_unit_ids": [item["unit_id"] for item in unit_results
                                        if item["kind"] == "individual"],
            })
        combined = next(unit for unit in unit_results if unit["kind"] == "combined")
        combined["individual_unit_ids"] = [unit["unit_id"] for unit in unit_results
                                           if unit["kind"] == "individual"]
        combined["individual_reports"] = [{
            "unit_id": unit["unit_id"], "unit_key": unit["unit_key"],
            "machine_report_path": unit["machine_report_path"],
            "human_report_path": unit["human_report_path"],
        } for unit in unit_results if unit["kind"] == "individual"]
        for unit in unit_results:
            unit["combined_report_path"] = combined["machine_report_path"]
        return result


def main(argv=None):
    """Run the standalone ``final-review`` operator command."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "final-review":
        args = args[1:]
    elif len(args) > 1 and args[1] == "final-review":
        args = args[2:]
    if not args:
        sys.stderr.write("review-gate final-review requires at least one explicit ref.\n")
        return 2
    if len(args) > 1 and not all(reference.startswith(("https://", "http://"))
                                 for reference in args):
        # Preserve the unambiguous legacy shorthand contract. Graph discovery
        # emits canonical provider URLs for corrected multi-item commands.
        sys.stderr.write(
            "review-gate final-review requires exactly one numeric ref; multiple numeric "
            "refs are reserved. Use canonical provider URLs for stack orchestration.\n"
        )
        return 2
    try:
        result = run_final_review(args[0] if len(args) == 1 else args)
    except Exception as exc:
        sys.stderr.write(f"review-gate final-review failed: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, sort_keys=True, ensure_ascii=False) + "\n")
    sys.stderr.write(
        f"review-gate final-review: {result['state']}; stage {result['stage']} is incomplete; "
        f"finding verdict: {result['finding_verdict'] or 'unavailable'}; "
        f"run {result['run_id']}\n"
    )
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
