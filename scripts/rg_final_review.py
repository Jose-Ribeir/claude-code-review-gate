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
from rg_final_gitlab import GitLabProvider
from rg_final_lenses import (
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


def _write_reports(store, run_id, orchestration):
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
    marker = (
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


def main(argv=None):
    """Run the standalone ``final-review`` operator command."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "final-review":
        args = args[1:]
    elif len(args) > 1 and args[1] == "final-review":
        args = args[2:]
    if len(args) != 1:
        sys.stderr.write(
            "review-gate final-review requires exactly one ref; multiple refs "
            "are reserved for later stack orchestration.\n"
        )
        return 2
    try:
        result = run_final_review(args[0])
    except Exception as exc:
        sys.stderr.write(f"review-gate final-review failed: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, sort_keys=True, ensure_ascii=False) + "\n")
    sys.stderr.write(
        f"review-gate final-review: {result['state']}; stage {STAGE} is incomplete; "
        f"finding verdict: {result['finding_verdict'] or 'unavailable'}; "
        f"run {result['run_id']}\n"
    )
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
