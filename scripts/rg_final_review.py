"""Standalone and stack orchestration for explicit SPEC-16 final review.

:func:`run_final_review` performs complete one-item individual+combined review:
context/intent acquisition, independent lenses, required/dynamic tests, memory,
freshness, and explicitly authorized publication. The earlier execution-only
seam remains available as :func:`execute_final_review` for existing callers.

A worker receives one request file, can inspect the complete repository and
merge-base diff, and returns a result object on stdout. Model verdict fields
are informational: ``ocr_verdict.compute_verdict`` alone decides finding-based
verdicts from normalized findings.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from contextlib import contextmanager
import os
from copy import deepcopy
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import ocr_profile
from rg_final_compose import compose_stack
from rg_final_context import collect_review_context
from rg_final_graph import discover_linear_graph, record_graph_observation
from rg_final_gitlab import GitLabProvider
from rg_final_dynamic import run_dynamic_checks
from rg_final_env import scrub_environment
from rg_final_memory import (
    carry_forward,
    finding_memory_for_unit,
    process_finding_closures,
    refresh_run_freshness,
    rerun_context,
)
from rg_final_publish import FinalPublisher
from rg_final_execution import (
    execute_unit,
    record_execution_tests,
    skipped_execution,
)
from rg_final_verification import run_required_verification
from rg_final_lenses import (
    CROSS_ITEM_CONTRACTS,
    INTEGRATION_BEHAVIOR,
    REQUIRED_LENS_NAMES,
    REQUIRED_LENSES,
    _sanitize_lens_text,
    build_lens_request,
    failed_lens_aggregation,
    lens_method_record,
)
from rg_final_materialize import materialize_review
from rg_final_provider import RevisionChangedError, revision_tuple, select_provider
from rg_final_store import FinalStore
from rg_final_telemetry import FinalReviewTelemetry


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
EXECUTION_STAGE = "S16-04B"
EXECUTION_MISSING_STAGES = ["composition", "provider_publication"]
EXECUTION_STACK_MISSING_STAGES = ["provider_publication"]
COMPLETE_STAGE = "S16-07"


def _selected_provider(reference, provider, repo_root):
    """Dispatch by ref while retaining the patchable legacy GitLab factory."""
    return select_provider(
        reference, provider=provider, repo_root=repo_root,
        gitlab_factory=lambda: GitLabProvider(),
    )


def _discover_observed(discover, references, *, provider, repo_root, telemetry):
    """Use the instrumented graph seam without breaking legacy injected callables."""
    try:
        supports_telemetry = "telemetry" in inspect.signature(discover).parameters
    except (TypeError, ValueError):
        supports_telemetry = True
    if supports_telemetry:
        return discover(
            references, provider=provider, repo_root=repo_root, telemetry=telemetry,
        )
    graph = discover(references, provider=provider, repo_root=repo_root)
    record_graph_observation(telemetry, references, graph=graph)
    return graph


def _read_standalone_observed(provider, reference, repo_root):
    """Bind the one-item graph observation to the collector adopted by its run."""
    telemetry = FinalReviewTelemetry()
    try:
        item = provider.read(reference, repo_root=repo_root)
    except Exception as exc:
        record_graph_observation(telemetry, [reference], error=exc)
        raise
    record_graph_observation(
        telemetry, [reference], graph={"provider": item["provider"], "items": [item]},
    )
    return item, telemetry


class WorkerError(RuntimeError):
    """A lens worker did not produce a trustworthy completed result."""

    def __init__(self, message, *, details=None):
        super().__init__(message)
        self.details = deepcopy(details) if isinstance(details, dict) else {}


_TASKKILL_TIMEOUT = 5.0
_REAP_TIMEOUT = 2.0
_PIPE_CLOSE_TIMEOUT = 1.0


def _process_cleanup(method):
    return {
        "status": "incomplete",
        "termination_method": method,
        "direct_child_reaped": False,
        "tree_termination_verified": False,
        "pipe_drain_completed": False,
        "errors": [],
    }


def _kill_direct_child(process, cleanup):
    try:
        if process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass
    except OSError as exc:
        cleanup["errors"].append(f"direct-child-kill:{type(exc).__name__}")


def _kill_owned_process(process):
    """Start bounded termination of the exact owned child/tree and report proof."""
    if os.name == "posix":
        cleanup = _process_cleanup("process-group")
        cleanup["tree_termination_verified"] = True
        try:
            # start_new_session=True makes the child PID its process-group ID.
            # The explicit inequality prevents an accidental kill of the caller.
            if process.pid == os.getpgrp():
                cleanup["tree_termination_verified"] = False
                cleanup["termination_method"] = "direct-child"
                cleanup["errors"].append("owned-process-group-unavailable")
                _kill_direct_child(process, cleanup)
                return cleanup
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            cleanup["tree_termination_verified"] = False
            cleanup["termination_method"] = "process-group+direct-child"
            cleanup["errors"].append(f"process-group-kill:{type(exc).__name__}")
            _kill_direct_child(process, cleanup)
        return cleanup
    if os.name == "nt":
        cleanup = _process_cleanup("taskkill-tree+direct-child")
        try:
            # /T includes descendants; invoke it before reaping the direct child.
            completed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL, capture_output=True, check=False,
                timeout=_TASKKILL_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            cleanup["errors"].append("taskkill-timeout")
        except OSError as exc:
            cleanup["errors"].append(f"taskkill-invocation:{type(exc).__name__}")
        else:
            cleanup["taskkill_returncode"] = completed.returncode
            if completed.returncode == 0:
                # This is taskkill evidence, not a Job Object guarantee.  A failed
                # drain below retracts it because inherited handles may belong to
                # surviving descendants.
                cleanup["tree_termination_verified"] = True
            else:
                cleanup["errors"].append(f"taskkill-exit:{completed.returncode}")
        _kill_direct_child(process, cleanup)
        return cleanup
    cleanup = _process_cleanup("direct-child")
    _kill_direct_child(process, cleanup)
    return cleanup


def _close_owned_pipes(process):
    """Close abandoned inherited pipes without letting close/join hang cleanup."""
    pipes = [pipe for pipe in (process.stdout, process.stderr) if pipe is not None]
    failures = []

    def close(pipe):
        try:
            pipe.close()
        except (OSError, ValueError) as exc:
            failures.append(type(exc).__name__)

    threads = [threading.Thread(target=close, args=(pipe,), daemon=True) for pipe in pipes]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + _PIPE_CLOSE_TIMEOUT
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return not failures and all(not thread.is_alive() for thread in threads)


def _bounded_reap(process, cleanup, partial):
    """Reap the direct child and retain output, bounding inherited-pipe EOF waits."""
    stdout, stderr = partial
    try:
        stdout, stderr = process.communicate(timeout=_REAP_TIMEOUT)
        cleanup["pipe_drain_completed"] = True
    except subprocess.TimeoutExpired as exc:
        cleanup["errors"].append("pipe-drain-timeout")
        if exc.output is not None:
            stdout = exc.output
        if exc.stderr is not None:
            stderr = exc.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        cleanup["errors"].append(f"pipe-drain:{type(exc).__name__}")
    if not cleanup["pipe_drain_completed"]:
        try:
            process.wait(timeout=_REAP_TIMEOUT)
        except subprocess.TimeoutExpired:
            cleanup["errors"].append("direct-child-wait-timeout")
        except OSError as exc:
            cleanup["errors"].append(f"direct-child-wait:{type(exc).__name__}")
        if not _close_owned_pipes(process):
            cleanup["errors"].append("pipe-close-incomplete")
    cleanup["direct_child_reaped"] = getattr(process, "returncode", None) is not None
    if not cleanup["direct_child_reaped"] or not cleanup["pipe_drain_completed"]:
        cleanup["tree_termination_verified"] = False
    cleanup["complete"] = bool(
        cleanup["direct_child_reaped"] and cleanup["tree_termination_verified"]
        and cleanup["pipe_drain_completed"]
    )
    cleanup["status"] = "complete" if cleanup["complete"] else "incomplete"
    return stdout, stderr


def run_owned_process(command, *, cwd, env, timeout):
    """Run a captured subprocess and bound every post-deadline cleanup wait.

    Timeout re-raises ``TimeoutExpired`` with obtainable partial output,
    ``returncode`` and structured ``cleanup`` evidence. POSIX owns a separate
    process group. Windows records taskkill evidence conservatively; direct-child
    fallback never implies that descendants were terminated.
    """
    options = {"start_new_session": True} if os.name == "posix" else {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(
        command, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", **options,
    )
    try:
        stdout, stderr = process.communicate(timeout=float(timeout))
    except subprocess.TimeoutExpired as exc:
        cleanup = _kill_owned_process(process)
        stdout, stderr = _bounded_reap(process, cleanup, (exc.output, exc.stderr))
        timeout_error = subprocess.TimeoutExpired(
            command, timeout, output=stdout, stderr=stderr,
        )
        timeout_error.returncode = getattr(process, "returncode", None)
        timeout_error.cleanup = cleanup
        timeout_error.tree_termination_verified = cleanup["tree_termination_verified"]
        raise timeout_error from exc
    except BaseException:
        cleanup = _kill_owned_process(process)
        _bounded_reap(process, cleanup, (None, None))
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


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
            "base_sha": target.get("base_sha"),
            "reference": item.get("url"),
            "ancestors": list(item.get("ancestors") or ()),
            "provider_data": _provider_data(item),
        }],
    }


def worker_environment(environ=None):
    """Return a worker environment without provider-write or unrelated secrets.

    Model-backend authentication remains available, but provider credentials,
    askpass helpers, generic tokens and unrelated secret material do not cross
    the trusted-supervisor/untrusted-worker boundary.
    """
    return scrub_environment(environ, preserve_model_auth=True)


def _worker_prompt(request, request_path):
    return (
        f"Perform one independent {request['lens']} static review. Read the JSON request at "
        f"{request_path}. Its repository.path is a complete immutable checkout and its "
        "diff is the complete merge-base patch. Iteratively inspect unchanged code, "
        "docs, callers and tests as needed. Your assigned evidence duty is: "
        f"{request['review_duty']} Do not use or wait for another lens's conclusions. "
        f"Return only one JSON object with \"schema\":\"{WORKER_RESULT_SCHEMA}\" and "
        "\"status\":\"success\" plus summary, findings, warnings, and unreviewed fields. "
        "The ordinary status enum is exactly \"success\" | \"completed\" | "
        "\"completed_with_warnings\": success and completed both mean the assigned review "
        "completed successfully; completed_with_warnings means the assigned review completed "
        "with explicit warnings or unreviewed limitations to preserve. These three are the "
        "only successful completion values; do not invent another status spelling. summary "
        "must be evidence-specific, naming relevant inspected repository files or the precise "
        "basis for a no-finding conclusion; empty text, ellipses, and angle-bracket templates "
        "are rejected and make the required lens incomplete. findings and warnings must be "
        "arrays, and unreviewed must be an array when present. Optionally disclose actual "
        "inspection with sources_read entries containing repository-relative path and concise "
        "rationale, and evidence_references entries containing repository-relative path, "
        "concise location (for example a line range), and rationale. Disclose only what you "
        "actually inspected; these lists support audit but are not treated as independently "
        "verified tool-call or memory-closure proof. Never include host-absolute paths, home "
        "paths, credentials, source dumps, or other lenses' responses. Do not prescribe a "
        "finding merely to populate these fields. Do not trust your own verdict field to "
        "determine the gate result. "
        "If finding_memory contains prior findings, omission never resolves them. Explicit "
        "closure may use finding_resolutions entries with finding_id, content_anchor, "
        "resolution_evidence.source (path, start_line, end_line, exact body, unit_key, head_sha) "
        "and revalidation (exact test name, state, output, evidence_fingerprint, unit_key, "
        "head_sha) copied from an actually executed required/existing/ephemeral test. A "
        "status/revalidated declaration alone is ignored. finding_falsifications require "
        "finding_id, content_anchor, a specific rationale, and evidence.source or "
        "evidence.revalidation with the same independent-proof contract. Only close a named "
        "prior finding when current pinned-tree and executed evidence supports that conclusion."
    )


def _default_worker_command(request_path, repo_root, environ, request):
    selection = ocr_profile.select_backend(env=environ, repo_root=repo_root)
    executable = shutil.which(selection.name, path=environ.get("PATH"))
    if not executable:
        raise WorkerError(f"selected review backend {selection.name!r} is not on PATH")
    prompt = _worker_prompt(request, request_path)
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


WORKER_COMPLETION_STATUSES = ("success", "completed", "completed_with_warnings")
_STDOUT_DIAGNOSTIC_LIMIT = 512
# Credential names are commonly underscore-delimited (``access_token`` and
# ``client_secret``). Start at the sensitive component rather than using ``\b``
# before it: the
# prefix (``access_``, ``client_``, and so on) may safely remain in diagnostics.
# This also avoids scanning an arbitrary key prefix on attacker-sized output.
_SENSITIVE_KEY = (
    r"(?:api[_-]?key|token|secret|password|credential|authorization|auth)"
    r"[A-Za-z0-9_.-]*"
)
_SENSITIVE_DIAGNOSTIC = re.compile(
    rf"(?i)({_SENSITIVE_KEY}\s*[\"']?\s*[:=]\s*)"
    r"(?!(?:basic|bearer)\s+\[redacted\])"
    r"(?:(\\?[\"'])(.*?)\2|([^\s,;\"'\}\]&]+))",
    re.DOTALL,
)
_AUTHORIZATION_DIAGNOSTIC = re.compile(
    r"(?i)(?<![A-Za-z0-9_])([A-Za-z0-9_-]{0,128}authorization"
    r"[\"']?\s*[:=]\s*[\"']?)(basic|bearer)(\s+)([^\s,\"'\}\]]+)",
)
_BEARER_DIAGNOSTIC = re.compile(r"(?i)(\bbearer\s+)[^\s,\"']+")
_URI_CREDENTIAL_DIAGNOSTIC = re.compile(
    r"([A-Za-z][A-Za-z0-9+.-]{0,31}://)[^\s/?#@]+@"
)


def _redact_sensitive_assignment(match):
    quote = match.group(2) or ""
    return match.group(1) + quote + "[redacted]" + quote


def _sanitize_worker_diagnostic(value, environ):
    """Redact complete credential values first, then return a bounded safe tail."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value or "")
    sensitive_parts = (
        "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "COOKIE", "PRIVATE_KEY",
        "API_KEY", "ASKPASS", "AUTHORIZATION", "AUTH",
    )
    known = []
    for key, value in dict(environ).items():
        if len(str(value)) >= 4 and any(part in str(key).upper()
                                       for part in sensitive_parts):
            known.append(str(value))
    for secret in sorted(set(known), key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    text = _URI_CREDENTIAL_DIAGNOSTIC.sub(r"\1[redacted]@", text)
    text = _AUTHORIZATION_DIAGNOSTIC.sub(r"\1\2\3[redacted]", text)
    text = _BEARER_DIAGNOSTIC.sub(r"\1[redacted]", text)
    text = _SENSITIVE_DIAGNOSTIC.sub(_redact_sensitive_assignment, text)
    text = "".join(
        char if char.isprintable() or char in "\n\t" else " " for char in text
    )
    return text[-_STDOUT_DIAGNOSTIC_LIMIT:]


def _worker_diagnostics(source, *, reason, status=None, return_code=None, stdout=None,
                        cleanup=None):
    received = _sanitize_worker_diagnostic(status, source)[-128:] if status is not None else None
    details = {
        "reason": reason,
        "received_status": received,
        "return_code": return_code,
        "stdout_tail": _sanitize_worker_diagnostic(stdout, source),
        "expected_status": list(WORKER_COMPLETION_STATUSES),
    }
    if cleanup is not None:
        details["cleanup"] = deepcopy(cleanup)
    return details


@contextmanager
def _worker_request_directory(process_cleanup):
    directory = tempfile.mkdtemp(prefix="review-gate-final-request-")
    try:
        yield directory
    finally:
        cleanup = process_cleanup.get("process")
        if cleanup is None or cleanup.get("complete", False):
            shutil.rmtree(directory, ignore_errors=True)
        # Otherwise a surviving descendant may still have the request open. Keep
        # the directory rather than presenting recursive deletion as tree cleanup.


def invoke_worker(request, *, command=None, repo_root=None, environ=None, timeout=900):
    """Execute one credential-isolated worker and decode its contracted result."""
    source = os.environ if environ is None else environ
    process_cleanup = {}
    with _worker_request_directory(process_cleanup) as directory:
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
            process = run_owned_process(
                command, cwd=request["repository"]["path"],
                env=worker_environment(source), timeout=float(timeout),
            )
        except subprocess.TimeoutExpired as exc:
            process_cleanup["process"] = getattr(exc, "cleanup", None)
            details = _worker_diagnostics(
                source, reason="timeout", return_code=getattr(exc, "returncode", None),
                stdout=exc.output, cleanup=process_cleanup["process"],
            )
            raise WorkerError(
                f"{request['lens']} worker timed out after {float(timeout):g}s",
                details=details,
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            details = _worker_diagnostics(source, reason="invocation-error")
            raise WorkerError(
                f"{request['lens']} worker could not run: {type(exc).__name__}: {exc}",
                details=details,
            ) from exc
        result = _object_from_text(process.stdout)
        if process.returncode:
            details = _worker_diagnostics(
                source, reason="exit-status", status=(result or {}).get("status"),
                return_code=process.returncode, stdout=process.stdout,
            )
            raise WorkerError(
                f"{request['lens']} worker exited {process.returncode}", details=details,
            )
        if result is None:
            details = _worker_diagnostics(
                source, reason="invalid-json", return_code=process.returncode,
                stdout=process.stdout,
            )
            raise WorkerError(
                f"{request['lens']} worker returned no valid contracted JSON result",
                details=details,
            )
        status = result.get("status")
        if status not in WORKER_COMPLETION_STATUSES and str(status).upper() != "STOP":
            details = _worker_diagnostics(
                source, reason="invalid-status", status=status,
                return_code=process.returncode, stdout=process.stdout,
            )
            raise WorkerError(
                f"{request['lens']} worker returned unsupported completion status "
                f"{details['received_status']!r}",
                details=details,
            )
        # This is a transport diagnostic, not evidence that the model actually
        # called tools. Normalization uses it for rejected-summary auditing and
        # persists only its bounded/sanitized allowlisted subset.
        result["_worker_diagnostics"] = _worker_diagnostics(
            source, reason="response-received", return_code=process.returncode,
            stdout=process.stdout,
        )
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


def _record_pending(store, unit_id, *, include_tests=True):
    pending = [
        ("composition", "Combined and prospective composition are reserved for later stages"),
        ("provider_publication", "No official provider status is emitted by this stage"),
    ]
    if include_tests:
        pending[:0] = [
            ("required_tests", "Repository-required verification is reserved for a later stage"),
            ("dynamic_tests", "Dynamic checks are reserved for a later stage"),
        ]
    for kind, summary in pending:
        store.record_coverage(unit_id, kind, "not_implemented", summary,
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
    finding_id = store.record_finding(
        unit_id, key, str(finding.get("content") or ""),
        str(finding.get("severity") or ""), str(finding.get("confidence", 0.0)),
        lens=lens, path=finding.get("path"), line=_line(finding),
        anchors=anchors, evidence={"worker": raw, "lens": lens},
    )
    return {"finding_id": finding_id, "content_anchor": anchors[0]["anchor"]}


def _stored_worker_evidence(outcome):
    """Project normalized worker diagnostics into a bounded report-only shape."""
    supplied = outcome.worker_evidence if isinstance(outcome.worker_evidence, dict) else {}
    details = next((warning.get("details", {}) for warning in outcome.warnings
                    if warning.get("code") == "required-lens-failed"), {})
    details = details if isinstance(details, dict) else {}
    received_status = supplied.get("received_status") or details.get("received_status") or ""
    received_summary = supplied.get("received_summary") or outcome.summary
    diagnostics = dict(supplied.get("diagnostics") or {})
    for key in ("reason", "return_code", "stdout_tail"):
        if key not in diagnostics and details.get(key) is not None:
            diagnostics[key] = details[key]
    clean_diagnostics = {}
    if diagnostics.get("reason"):
        clean_diagnostics["reason"] = _sanitize_lens_text(diagnostics["reason"], 80)
    if isinstance(diagnostics.get("return_code"), int) \
            and not isinstance(diagnostics.get("return_code"), bool):
        clean_diagnostics["return_code"] = diagnostics["return_code"]
    if diagnostics.get("stdout_tail"):
        safe_tail = _sanitize_worker_diagnostic(diagnostics["stdout_tail"], {})
        clean_diagnostics["stdout_tail"] = _sanitize_lens_text(safe_tail, 512)
    disclosure = supplied.get("disclosure") or (
        "worker-result-rejected" if outcome.state == "incomplete" else "worker-not-returned")
    return {
        "received_status": _sanitize_lens_text(received_status, 128),
        "received_summary": _sanitize_lens_text(received_summary, 1000),
        "sources_read": deepcopy(supplied.get("sources_read") or ()),
        "evidence_references": deepcopy(supplied.get("evidence_references") or ()),
        "rejected": deepcopy(supplied.get("rejected") or ()),
        "diagnostics": clean_diagnostics,
        "disclosure": disclosure,
        "verification": "worker-declared-not-independently-verified",
        "tool_calls_verified": False,
    }


def _record_lens_aggregation(store, unit_id, aggregation, materialized=None, request=None):
    """Persist every lens completion and every normalized warning/findings stream."""
    common = {
        "request_schema": WORKER_REQUEST_SCHEMA,
        "result_schema": WORKER_RESULT_SCHEMA,
        "repository_access": "full_tree",
        "unit_id": unit_id,
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
    recorded_findings = []
    for outcome in aggregation.outcomes:
        method = lens_method_record(request, outcome.name)
        method["unit_id"] = unit_id
        evidence = {
            **common,
            "summary": outcome.summary,
            "finding_verdict": aggregation.finding_verdict if aggregation.complete else None,
            "finding_count": len(outcome.findings),
            "warning_count": len(outcome.warnings),
            "unreviewed": list(outcome.unreviewed),
            "capabilities": list(outcome.capabilities),
            "independent_context": True,
            "error": outcome.error,
            "method": method,
            "worker_evidence": _stored_worker_evidence(outcome),
        }
        store.record_lens(unit_id, outcome.name, outcome.state, outcome.summary, evidence)
        store.record_coverage(unit_id, f"lens:{outcome.name}", outcome.state,
                              outcome.summary, {"evidence": evidence})
        for finding in outcome.findings:
            recorded_findings.append(_record_finding(
                store, unit_id, finding, _finding_key(finding, used),
                materialized.diff if materialized is not None else ""))
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
    return recorded_findings


def _record_execution_evidence(store, unit_id, execution, diff="", affected_units=None,
                               *, record_tests=True):
    """Persist stage outputs and a supervisor-mapped stop in one hand-off."""
    if record_tests:
        record_execution_tests(store, unit_id, execution)
    for rejected in execution.rejected_stops if record_tests else ():
        store.record_warning(unit_id, "structured-stop-rejected", rejected["error"],
                             rejected["stage"], rejected)
    stop_id = None
    if execution.stop is not None:
        stop = execution.stop
        finding = deepcopy(stop["finding"])
        finding["lens"] = stop["stage"]
        finding.setdefault("category", "critical-stop")
        _record_finding(store, unit_id, finding,
                        "stop-" + stop["reason_code"], diff)
        affected = list(affected_units or [unit_id])
        skipped = []
        for item in execution.skipped_work:
            detached = deepcopy(item)
            detached["unit_id"] = detached.get("unit_id") or unit_id
            skipped.append(detached)
        stop_id = store.record_stop(
            unit_id, f"{stop['reason_code']}: {stop['reason']}", evidence=stop,
            affected_unit_ids=affected, skipped_work=skipped,
        )
        for affected_id in affected:
            store.record_coverage(
                affected_id, "critical_stop", "incomplete",
                stop["reason"], {"stop_id": stop_id, "source_unit_id": unit_id,
                                 "reason_code": stop["reason_code"]},
            )
    return stop_id


def _execution_result_fields(execution):
    if execution is None:
        return {"execution_complete": None, "stops": [], "rejected_stops": [],
                "skipped_work": []}
    stop_id = getattr(execution, "stop_id", None)
    stops = [] if execution.stop is None else [{**deepcopy(execution.stop), "stop_id": stop_id}]
    return {
        "execution_complete": execution.checks_complete,
        "stops": stops,
        "rejected_stops": deepcopy(execution.rejected_stops),
        "skipped_work": deepcopy(execution.skipped_work),
    }


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
    if completion_marker is None:
        stage = orchestration.get("stage", STAGE)
        marker = (f"\nStage: {stage}\n" + (
            "Completion: INCOMPLETE - individual static lenses and applicable tests ran; "
            "composition and provider publication remain pending.\n"
            if stage == EXECUTION_STAGE else
            "Completion: INCOMPLETE - this stage runs every individual static lens independently. "
            "Tests, composition and provider publication are not implemented.\n"
        ))
    else:
        marker = completion_marker
    human = human.replace("\n", marker, 1)
    human_path.write_text(human, encoding="utf-8")
    try:
        os.chmod(human_path, 0o600)
    except OSError:
        pass
    return str(machine_path), str(human_path)


def _run_static_final_review(reference, *, repo_root=None, provider=None, store_path=None,
                             worker=None, environ=None, execute_checks=False,
                             required_runner=None, dynamic_runner=None, dynamic_kwargs=None):
    """Review one provider-pinned ref and return the versioned operator result.

    ``worker`` is an optional argv prefix (``{request}`` placeholder supported)
    for scripted or alternate workers.  The default uses ``RG_FINAL_WORKER`` or
    the selected normal review backend.  No provider mutation method is called.
    """
    repo_root = str(repo_root or os.getcwd())
    provider = _selected_provider(reference, provider, repo_root)
    if isinstance(reference, (list, tuple)):
        return run_final_stack(
            reference, repo_root=repo_root, provider=provider, store_path=store_path,
            worker=worker, environ=environ,
        )
    environ = os.environ if environ is None else environ
    stage = EXECUTION_STAGE if execute_checks else STAGE
    missing_stages = EXECUTION_MISSING_STAGES if execute_checks else MISSING_STAGES
    item, telemetry = _read_standalone_observed(provider, reference, repo_root)
    revisions = list(revision_tuple(item))
    source = item["source"]
    target = item["target"]
    with FinalStore(store_path) as store:
        identity = store.start_run(
            _stack(item), acceptance_version=store.resolve_acceptance_version(), metadata={
                "stage": stage,
                "complete": False,
                "missing_stages": missing_stages,
                "standalone": True,
            }, telemetry=telemetry,
        )
        run_id = identity["run_id"]
        unit_id = identity["unit_ids"]["individual:1"]
        combined_id = identity["unit_ids"]["combined"]
        _record_pending(store, unit_id, include_tests=not execute_checks)
        _record_pending(store, combined_id)
        store.record_coverage(
            combined_id, "mandatory_lenses", "not_implemented",
            "Combined and stack execution are reserved for a later layer",
        )
        finding_verdict = None
        merge_base = None
        state = "incomplete"
        aggregation = None
        execution = None
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

                # Ordinary findings and stage errors never short-circuit useful
                # independent evidence. Only a supervisor-validated STOP can
                # suppress dependent work, with explicit skipped evidence.
                execution = execute_unit(
                    request, isolated_worker, unit_id=unit_id,
                    required_runner=((required_runner or run_required_verification)
                                     if execute_checks else None),
                    dynamic_runner=((dynamic_runner or run_dynamic_checks)
                                    if execute_checks else None),
                    dynamic_kwargs=dynamic_kwargs, environ=environ,
                )
                aggregation = execution.aggregation
                finding_verdict = (execution.finding_verdict if execute_checks
                                   else aggregation.finding_verdict)
                _record_lens_aggregation(
                    store, unit_id, aggregation, materialized, request=request)
                execution.stop_id = _record_execution_evidence(
                    store, unit_id, execution, materialized.diff)
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
        if execution is not None and execution.stop is not None:
            unit_state = "incomplete"
            state = "incomplete"
            finding_verdict = None
        summary = ("A validated critical stop skipped dependent work; original evidence is retained"
                   if execution is not None and execution.stop is not None else
                   "Static-lens findings block this incomplete standalone review"
                   if unit_state == "findings" else
                   "Independent static lenses and applicable tests completed; composition is pending"
                   if static_lenses_complete and execute_checks else
                   "Independent static lenses completed; verification and composition are pending"
                   if static_lenses_complete else
                   "A required static lens is incomplete; no final pass is available")
        store.set_unit_state(unit_id, unit_state, summary)
        store.set_unit_state(combined_id, "incomplete",
                             "Combined review is not implemented in this stage")
        trust = store.finish_run(run_id, state, trust={
            "local_only": True, "enforced": False, "official": False,
            "stage_incomplete": True, "missing_stages": missing_stages,
            "static_lenses_complete": static_lenses_complete,
            "stopped": bool(execution and execution.stop),
            "reason": "C12 preserves safe completion and scoped stops; composition/publication remain"
            if execute_checks else
            "C5 performs independent static lenses but never verifies, composes or publishes",
        })
        if state == "stale":
            exit_code = 3
        elif finding_verdict == "block":
            exit_code = 1
        else:
            exit_code = 2
        result = {
            "schema": RESULT_SCHEMA,
            "stage": stage,
            "complete": False,
            "missing_stages": missing_stages,
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
            **_execution_result_fields(execution),
            "required_verification": deepcopy(execution.required) if execution else None,
            "dynamic_checks": deepcopy(execution.dynamic) if execution else None,
        }
        machine_path, human_path = _write_reports(store, run_id, {
            "schema": RESULT_SCHEMA, "stage": stage, "complete": False,
            "missing_stages": missing_stages, "state": state, "exit_code": exit_code,
            "finding_verdict": finding_verdict, "unit_id": unit_id,
            "static_lenses_complete": static_lenses_complete,
            **_execution_result_fields(execution),
        })
        result["machine_report_path"] = machine_path
        result["human_report_path"] = human_path
        return result


def _record_standalone_context(store, unit_ids, context):
    """Persist context and intent acquisition without inventing requirements."""
    completeness = context.get("completeness") or {}
    context_complete = (
        context.get("contract") == "final-review-context/v1"
        and context.get("review_mode") == "complete-repository"
        and completeness.get("complete") is True
        and bool(context.get("search_roots"))
    )
    intent = context.get("intent")
    intent_attempted = isinstance(intent, dict) and all(
        key in intent for key in ("description", "tickets", "references", "warnings"))
    intent = intent if isinstance(intent, dict) else {}
    description = intent.get("description") or {}
    has_description = bool(str(description.get("text") or "").strip())
    tickets = intent.get("tickets") or []
    references = intent.get("references") or []
    listed_only = intent.get("listed_only") or []
    intent_warnings = [str(warning) for warning in intent.get("warnings") or ()]
    has_intent = has_description or bool(tickets) or bool(references) or bool(listed_only)
    intent_complete = intent_attempted
    if intent_attempted and not has_intent and not intent_warnings:
        intent_warnings.append(
            "No provider description, linked ticket, or intent reference was available")
    for unit_id in unit_ids:
        store.record_coverage(
            unit_id, "review_context", "complete" if context_complete else "incomplete",
            "Complete pinned-tree context and required content were acquired"
            if context_complete else "Complete pinned-tree context is unavailable",
            {"contract": context.get("contract"),
             "completeness": deepcopy(completeness)},
        )
        store.record_coverage(
            unit_id, "intent", "complete" if intent_complete else "incomplete",
            ("Provider/linked intent acquisition completed"
             if has_intent else
             "Intent acquisition was attempted; no description, ticket, or link was available")
            if intent_complete else "Mandatory intent acquisition did not complete",
            {"description_available": has_description,
             "ticket_count": len(tickets), "reference_count": len(references) + len(listed_only),
             "attempted": intent_attempted, "available": has_intent,
             "warnings": intent_warnings},
        )
        for warning in context.get("warnings") or ():
            if isinstance(warning, dict):
                store.record_warning(
                    unit_id, warning.get("code", "review-context"),
                    warning.get("message", "Review context warning"),
                    warning.get("scope", "review-context"),
                    {key: deepcopy(value) for key, value in warning.items()
                     if key not in ("code", "message", "scope")},
                )
            else:
                store.record_warning(unit_id, "review-context", str(warning), "review-context")
        for warning in intent_warnings:
            store.record_warning(unit_id, "intent-acquisition", warning, "intent")
    return context_complete, intent_complete


def _complete_checks(execution):
    """Require mandatory evidence while retaining best-effort dynamic warnings."""
    if execution is None or not execution.aggregation.complete or execution.stop is not None:
        return False
    required = execution.required or {}
    dynamic = execution.dynamic
    required_done = required.get("status") in ("pass", "absent", "block")
    if not isinstance(dynamic, dict) or dynamic.get("schema") != \
            "final-review-dynamic-result/v1":
        return False
    if not all(isinstance(dynamic.get(key), list)
               for key in ("findings", "warnings", "tests")):
        return False
    if not isinstance(dynamic.get("coverage"), dict) or \
            not isinstance(dynamic.get("telemetry"), dict):
        return False
    state = dynamic.get("state")
    if state not in ("passed", "not_applicable", "findings", "degraded", "error"):
        return False
    coverage_status = dynamic["coverage"].get("status")
    if not coverage_status:
        return False
    degraded = (
        state in ("degraded", "error")
        or (dynamic.get("complete") is not True and state != "findings")
        or coverage_status in ("unknown", "uncovered", "incomplete", "error")
    )
    # Dynamic limitations are compatible with completion only when their
    # warning and telemetry are persisted. They never fabricate a suite/probe
    # pass; reproduced findings still flow to the sole verdict authority.
    dynamic_done = not degraded or bool(dynamic["warnings"])
    return required_done and dynamic_done


def _carry_observed_findings(store, run_id, unit_id, prior, observations):
    """Turn same-defect current observations into explicit memory carries."""
    threads = {finding["content_anchor"]: finding for finding in prior.get("findings", [])}
    for observed in observations:
        pending = threads.get(observed["content_anchor"])
        if pending is None:
            continue
        try:
            carry_forward(
                store, run_id, unit_id, pending["thread_id"],
                finding_id=observed["finding_id"],
                evidence={"revalidation": "current independent lens observed the same defect"},
            )
        except Exception as exc:
            # A failed carry remains pending and blocks finalization.
            store.record_warning(
                unit_id, "finding-carry-forward", str(exc), "finding-memory",
                {"thread_id": pending["thread_id"], "finding_id": observed["finding_id"]},
            )


def _process_unit_finding_closures(store, run_id, unit_id, unit_key, unit_prior,
                                   execution, materialized=None):
    if execution is None:
        return []
    context = None
    if materialized is not None:
        context = {
            "path": str(materialized.path), "head_sha": materialized.head_sha,
            "unit_key": unit_key,
        }
    return process_finding_closures(
        store, run_id, unit_id, unit_prior, execution.aggregation.finding_closures,
        verification_context=context, execution=execution)


def run_final_review(reference, *, repo_root=None, provider=None, store_path=None,
                     worker=None, environ=None, execute_checks=False,
                     required_runner=None, dynamic_runner=None, dynamic_kwargs=None,
                     context_kwargs=None, publish=False, publication_runner=None,
                     report_url=None, local_waivers=(), current_root_sha=None,
                     native_host=None):
    """Run one complete standalone individual+combined final review.

    Every explicit invocation first validates the provider's actual open-item
    graph. A single reference is standalone only when that graph is isolated;
    omitted relatives fail with the graph's complete corrected command.
    Freshness, finding memory, and publication remain store-owned authorities.
    ``publish=True`` explicitly authorizes the trusted status seam; permission
    failures preserve complete local-only evidence. Multiple references use
    the same provider-selected complete stack lifecycle as the operator CLI.
    """
    repo_root = str(repo_root or os.getcwd())
    provider = _selected_provider(reference, provider, repo_root)
    environ = os.environ if environ is None else environ
    transport = str(environ.get("RG_FINAL_REVIEW_TRANSPORT", "")).strip().lower()
    if transport not in ("", "legacy", "native"):
        raise ValueError(f"unsupported final-review transport {transport!r}")
    if isinstance(reference, (list, tuple)) and len(reference) == 1:
        reference = reference[0]
    if isinstance(reference, (list, tuple)):
        return run_final_stack(
            reference, repo_root=repo_root, provider=provider, store_path=store_path,
            worker=worker, environ=environ, execute_checks=execute_checks,
            required_runner=required_runner, dynamic_runner=dynamic_runner,
            dynamic_kwargs=dynamic_kwargs, current_root_sha=current_root_sha,
            context_kwargs=context_kwargs, publish=publish,
            publication_runner=publication_runner, report_url=report_url,
            local_waivers=local_waivers, complete=True, native_host=native_host,
        )
    standalone_dynamic_kwargs = dict(dynamic_kwargs or {})
    configured_executor = environ.get("RG_FINAL_DYNAMIC_EXECUTOR", "").strip()
    if configured_executor:
        standalone_dynamic_kwargs.setdefault("executor", shlex.split(configured_executor))
    standalone_dynamic_kwargs.setdefault("environ", environ)
    telemetry = FinalReviewTelemetry()
    graph = _discover_observed(
        discover_linear_graph, [reference], provider=provider, repo_root=repo_root,
        telemetry=telemetry,
    )
    # Graph discovery raises GraphOmissionError for every related open item;
    # a successful one-reference discovery is therefore genuinely isolated.
    item = graph["items"][0]
    revisions = list(revision_tuple(item))
    source = item["source"]
    target = item["target"]
    with FinalStore(store_path) as store:
        identity = store.start_run(
            _stack(item), acceptance_version=store.resolve_acceptance_version(), metadata={
                "stage": COMPLETE_STAGE, "complete": True, "missing_stages": [],
                "standalone": True,
            }, telemetry=telemetry,
        )
        run_id = identity["run_id"]
        unit_id = identity["unit_ids"]["individual:1"]
        combined_id = identity["unit_ids"]["combined"]
        unit_ids = (unit_id, combined_id)
        prior = rerun_context(store, run_id)
        executions = {unit_id: None, combined_id: None}
        aggregations = {}
        closure_outcomes = {}
        context_complete = intent_complete = False
        merge_base = None
        native_execution = None
        try:
            source_repository = source.get("http_url") or source["url"]
            target_repository = target.get("http_url") or target["url"]
            with materialize_review(
                source_repository, target["sha"], source["sha"],
                target_repository=(target_repository
                                   if target_repository != source_repository else None),
                require_lfs=True,
                lfs_irrelevance=(context_kwargs or {}).get("binary_irrelevance"),
            ) as materialized:
                merge_base = materialized.merge_base_sha
                options = dict(context_kwargs or {})
                options.pop("binary_irrelevance", None)
                review_context = collect_review_context(
                    materialized, description=item.get("description"),
                    linked_intent=item.get("linked_intent"),
                    commit_messages=item.get("commit_messages", ()),
                    branch=source.get("branch"),
                    binary_irrelevance=(context_kwargs or {}).get("binary_irrelevance"),
                    **options,
                )
                required_context = {
                    "contract", "review_mode", "search_roots", "diff", "documents",
                    "files", "intent", "completeness", "warnings",
                }
                if not isinstance(review_context, dict) or \
                        not required_context <= set(review_context):
                    raise ValueError("review context did not satisfy final-review-context/v1")
                context_complete, intent_complete = _record_standalone_context(
                    store, unit_ids, review_context)
                for current_id in unit_ids:
                    store.record_coverage(
                        current_id, "repository", "complete",
                        "Complete immutable checkout available",
                        {"submodules": list(materialized.submodules),
                         "lfs_files": list(materialized.lfs_files),
                         "lfs_complete": materialized.lfs_complete},
                    )
                    store.record_coverage(
                        current_id, "diff", "complete",
                        "Complete binary-capable merge-base diff",
                        {"merge_base_sha": merge_base, "sha256": _sha256(materialized.diff)},
                    )
                store.record_coverage(
                    unit_id, "composition", "not_applicable",
                    "The individual unit uses its provider-pinned tree",
                )
                store.record_coverage(
                    combined_id, "composition", "complete",
                    "A one-item combined unit uses the same immutable pinned tree",
                    {"standalone": True, "source_unit_id": unit_id},
                )

                requests = {}
                unit_priors = {}

                def enrich_request(lens_request, unit_key):
                    if unit_key == "combined":
                        lens_request["capabilities"] = list(dict.fromkeys(
                            [*lens_request.get("capabilities", ()),
                             CROSS_ITEM_CONTRACTS, INTEGRATION_BEHAVIOR]))
                        lens_request["review_duty"] += (
                            " Review cross-item contracts and actual integration behavior."
                        )
                    return lens_request

                for current_id, unit_key in ((unit_id, "individual:1"), (combined_id, "combined")):
                    unit_priors[current_id] = finding_memory_for_unit(prior, unit_key)
                    public_item = _public_item(item)
                    if unit_key == "combined":
                        public_item.update({
                            "id": "combined", "title": "Standalone combined review",
                            "description": "Review the complete one-item integration behavior.",
                        })
                    requests[current_id] = {
                        "schema": WORKER_REQUEST_SCHEMA, "stage": COMPLETE_STAGE,
                        "run_id": run_id, "unit_id": current_id, "unit_key": unit_key,
                        "store_instance_id": identity["store_instance_id"],
                        "contract_version": identity["contract_version"],
                        "revision_tuple": revisions, "item": public_item,
                        "repository": {
                            "path": str(materialized.path), "access": "full_tree",
                            "head_sha": materialized.head_sha,
                            "target_sha": materialized.target_sha,
                            "merge_base_sha": materialized.merge_base_sha,
                            "submodules": list(materialized.submodules),
                            "lfs_files": list(materialized.lfs_files),
                            "lfs_complete": materialized.lfs_complete,
                        },
                        "diff": materialized.diff,
                        "review_context": deepcopy(review_context),
                        "finding_memory": unit_priors[current_id],
                    }

                transport = str(environ.get("RG_FINAL_REVIEW_TRANSPORT", "")).strip().lower()
                if transport not in ("", "legacy", "native"):
                    raise ValueError(f"unsupported final-review transport {transport!r}")
                if transport == "native":
                    from rg_final_native import NativeStandaloneExecution
                    from rg_native_executor import KimchiNativeHost

                    host = native_host or KimchiNativeHost.checked(environ=environ)
                    native_execution = NativeStandaloneExecution(
                        store, host, environ=environ, run_id=run_id,
                        prompt_factory=_worker_prompt,
                        required_runner=required_runner or run_required_verification,
                        dynamic_runner=dynamic_runner or run_dynamic_checks,
                        dynamic_kwargs=standalone_dynamic_kwargs,
                    )
                    native_execution.prepare([
                        enrich_request(build_lens_request(requests[current_id], spec), unit_key)
                        for current_id, unit_key in (
                            (unit_id, "individual:1"), (combined_id, "combined"))
                        for spec in REQUIRED_LENSES
                    ])

                for current_id, unit_key in ((unit_id, "individual:1"), (combined_id, "combined")):
                    if executions[unit_id] is not None and executions[unit_id].stop is not None:
                        executions[current_id] = skipped_execution(
                            executions[unit_id].stop, unit_id=current_id, run_tests=True)
                        continue
                    unit_prior = unit_priors[current_id]
                    public_item = _public_item(item)
                    if unit_key == "combined":
                        public_item.update({
                            "id": "combined", "title": "Standalone combined review",
                            "description": "Review the complete one-item integration behavior.",
                        })
                    request = {
                        "schema": WORKER_REQUEST_SCHEMA, "stage": COMPLETE_STAGE,
                        "run_id": run_id, "unit_id": current_id, "unit_key": unit_key,
                        "store_instance_id": identity["store_instance_id"],
                        "contract_version": identity["contract_version"],
                        "revision_tuple": revisions, "item": public_item,
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

                    def isolated_worker(lens_request, unit_key=unit_key):
                        lens_request = enrich_request(lens_request, unit_key)
                        if native_execution is not None:
                            return native_execution.worker(lens_request)
                        return invoke_worker(
                            lens_request, command=worker, repo_root=repo_root,
                            environ=environ,
                            timeout=float(environ.get("RG_FINAL_WORKER_TIMEOUT", "900")),
                        )

                    executions[current_id] = execute_unit(
                        request, isolated_worker, unit_id=current_id,
                        required_runner=(
                            native_execution.required_runner(request)
                            if native_execution is not None
                            else required_runner or run_required_verification
                        ),
                        dynamic_runner=(
                            native_execution.dynamic_runner(request)
                            if native_execution is not None
                            else dynamic_runner or run_dynamic_checks
                        ),
                        dynamic_kwargs=standalone_dynamic_kwargs, environ=environ,
                    )
                    aggregations[current_id] = executions[current_id].aggregation
                    observations = _record_lens_aggregation(
                        store, current_id, executions[current_id].aggregation, materialized,
                        request=request)
                    _carry_observed_findings(
                        store, run_id, current_id, unit_prior, observations)
                    if native_execution is not None:
                        record_execution_tests(store, current_id, executions[current_id])
                        for rejected in executions[current_id].rejected_stops:
                            store.record_warning(
                                current_id, "structured-stop-rejected", rejected["error"],
                                rejected["stage"], rejected,
                            )

                if native_execution is not None:
                    native_execution.close()

                individual_execution = executions[unit_id]
                combined_execution = executions[combined_id]
                if individual_execution.stop is not None:
                    individual_execution.skipped_work.extend(deepcopy(
                        combined_execution.skipped_work))
                    individual_execution.stop_id = _record_execution_evidence(
                        store, unit_id, individual_execution, materialized.diff,
                        affected_units=unit_ids, record_tests=native_execution is None)
                    record_execution_tests(store, combined_id, combined_execution)
                    aggregations[combined_id] = combined_execution.aggregation
                    _record_lens_aggregation(
                        store, combined_id, combined_execution.aggregation, materialized,
                        request=request)
                else:
                    _record_execution_evidence(
                        store, unit_id, individual_execution, materialized.diff,
                        record_tests=native_execution is None)
                    combined_execution.stop_id = _record_execution_evidence(
                        store, combined_id, combined_execution, materialized.diff,
                        record_tests=native_execution is None)
                # Claims were collected before tests, but cannot finalize until
                # the independently executed evidence exists in this same run.
                for current_id, unit_key in ((unit_id, "individual:1"),
                                             (combined_id, "combined")):
                    closure_outcomes[unit_key] = _process_unit_finding_closures(
                        store, run_id, current_id, unit_key,
                        finding_memory_for_unit(prior, unit_key),
                        executions[current_id], materialized,
                    )
        except Exception as exc:
            if native_execution is not None:
                native_execution.abort()
            for current_id in unit_ids:
                if current_id not in aggregations:
                    aggregations[current_id] = failed_lens_aggregation(exc)
                    _record_lens_aggregation(store, current_id, aggregations[current_id])
                store.record_coverage(
                    current_id, "repository", "incomplete",
                    "Immutable full-tree review did not complete", {"error": str(exc)})
                store.record_warning(current_id, "materialization", str(exc), "repository")
                if executions[current_id] is None:
                    for kind in ("required_tests", "dynamic_tests"):
                        store.record_coverage(
                            current_id, kind, "incomplete",
                            "Mandatory test evidence did not execute", {"error": str(exc)})
            store.record_coverage(
                combined_id, "composition", "incomplete",
                "No complete standalone combined review is available", {"error": str(exc)})

        freshness = None
        try:
            freshness = refresh_run_freshness(
                store, run_id, provider, references=[reference], repo_root=repo_root)
        except Exception as exc:
            for current_id in unit_ids:
                store.record_warning(current_id, "freshness-check", str(exc), "freshness")

        for current_id, unit_key in ((unit_id, "individual:1"), (combined_id, "combined")):
            if unit_key not in closure_outcomes:
                closure_outcomes[unit_key] = _process_unit_finding_closures(
                    store, run_id, current_id, unit_key,
                    finding_memory_for_unit(prior, unit_key), executions[current_id],
                )

        execution_list = [executions[unit_id], executions[combined_id]]
        stopped = any(execution and execution.stop for execution in execution_list)
        lenses_complete = len(aggregations) == 2 and all(
            aggregation.complete for aggregation in aggregations.values())
        finding_verdict = (store.publication_decision(
            run_id, unit_id=unit_id, local_waivers=local_waivers,
        )["finding_verdict"] if lenses_complete and not stopped else None)
        checks_complete = (
            context_complete and intent_complete and not stopped
            and all(_complete_checks(execution) for execution in execution_list)
        )
        memory_status = store.memory_status(run_id)
        fresh = bool(freshness and freshness.get("fresh"))
        complete = bool(checks_complete and fresh and memory_status["complete_for_finalization"])
        if freshness and freshness["state"] == "stale":
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

        if fresh:
            for current_id, execution in ((unit_id, executions[unit_id]),
                                          (combined_id, executions[combined_id])):
                unit_verdict = execution.finding_verdict if execution else None
                unit_complete = bool(
                    complete and execution and execution.stop is None
                    and _complete_checks(execution))
                unit_state = (
                    "findings" if unit_complete and unit_verdict == "block"
                    else "pass" if unit_complete and unit_verdict == "pass" and state in (
                        "pass", "findings") else "incomplete"
                )
                store.set_unit_state(
                    current_id, unit_state,
                    "Complete review evidence is available" if unit_complete
                    else "Mandatory review, memory, or test evidence is incomplete",
                )

        publication_permitted = bool(
            publish and not identity["trust"].get("independent"))
        trust = store.finish_run(run_id, state, trust={
            "local_only": not publication_permitted,
            "enforced": publication_permitted,
            "official": publication_permitted,
            "trusted_orchestrator": bool(publish),
            "stage_incomplete": False, "missing_stages": [],
            "static_lenses_complete": all(
                aggregation.complete for aggregation in aggregations.values()),
            "stopped": stopped,
            "reason": "Complete standalone evidence; publication remains explicitly guarded",
        })
        publication = None
        if publish:
            # Store authority distinguishes an enforced adverse status from a
            # green one; incomplete/stale outcomes never bypass green gates.
            publisher = FinalPublisher(
                store, runner=publication_runner, trusted_orchestrator=True,
                repo_root=repo_root,
            )
            publication = publisher.publish_individual(
                run_id, unit_id, report_url=report_url, local_waivers=local_waivers)
            trust = store.machine_report(run_id)["trust"]

        static_complete = lenses_complete
        exit_code = (3 if state == "stale" else 1 if state == "findings"
                     else 0 if state == "pass" else 2)
        report_units = store.machine_report(run_id)["units"]
        unit_outputs = []
        for current_id, unit_key in ((unit_id, "individual:1"), (combined_id, "combined")):
            execution = executions[current_id]
            aggregation = aggregations.get(current_id)
            unit_outputs.append({
                "unit_id": current_id, "unit_key": unit_key,
                "kind": "individual" if current_id == unit_id else "combined",
                "state": next(unit["state"] for unit in report_units
                              if unit["unit_id"] == current_id),
                "static_lenses_complete": bool(aggregation and aggregation.complete),
                "lenses": [{"name": outcome.name, "state": outcome.state,
                            "summary": outcome.summary,
                            "capabilities": list(outcome.capabilities)}
                           for outcome in (aggregation.outcomes if aggregation else ())],
                "required_verification": deepcopy(execution.required) if execution else None,
                "dynamic_checks": deepcopy(execution.dynamic) if execution else None,
                "finding_closures": deepcopy(closure_outcomes[unit_key]),
                **_execution_result_fields(execution),
            })
        result = {
            "schema": RESULT_SCHEMA, "stage": COMPLETE_STAGE,
            "complete": complete, "missing_stages": [], "state": state,
            "exit_code": exit_code, "finding_verdict": finding_verdict,
            "static_lenses_complete": static_complete,
            "context_complete": context_complete, "intent_complete": intent_complete,
            "lenses": unit_outputs[0]["lenses"], "units": unit_outputs,
            "store_instance_id": identity["store_instance_id"],
            "store_path": str(store.path), "run_id": run_id,
            "run_seq": identity["run_seq"], "stack_key": identity["stack_key"],
            "unit_id": unit_id, "unit_key": "individual:1",
            "combined_unit_id": combined_id,
            "contract_version": identity["contract_version"],
            "acceptance_version": identity["acceptance_version"],
            "revision_tuple": revisions,
            "revisions": {"source_sha": source["sha"], "target_sha": target["sha"],
                          "provider_base_sha": target["base_sha"],
                          "merge_base_sha": merge_base},
            "trust": trust, "freshness": deepcopy(freshness),
            "finding_memory": deepcopy(store.memory_status(run_id)),
            "finding_closures": [deepcopy(outcome) for outcomes in closure_outcomes.values()
                                 for outcome in outcomes],
            "execution_complete": checks_complete,
            "stops": [stop for output in unit_outputs for stop in output["stops"]],
            "rejected_stops": [entry for output in unit_outputs
                               for entry in output["rejected_stops"]],
            "skipped_work": [entry for output in unit_outputs
                             for entry in output["skipped_work"]],
            "required_verification": deepcopy(executions[unit_id].required)
            if executions[unit_id] else None,
            "dynamic_checks": deepcopy(executions[unit_id].dynamic)
            if executions[unit_id] else None,
            "publication": publication,
        }
        marker = (f"\nStage: {COMPLETE_STAGE}\n" + (
            "Completion: COMPLETE - final state and provider trust are reported separately.\n"
            if complete else
            "Completion: INCOMPLETE - mandatory evidence, freshness, memory, or a stop prevents "
            "a complete final result.\n"))
        machine_path, human_path = _write_reports(store, run_id, {
            "schema": RESULT_SCHEMA, "stage": COMPLETE_STAGE, "complete": complete,
            "missing_stages": [], "state": state, "exit_code": exit_code,
            "finding_verdict": finding_verdict, "unit_id": unit_id,
            "combined_unit_id": combined_id, "static_lenses_complete": static_complete,
            "context_complete": context_complete, "intent_complete": intent_complete,
            "publication": publication,
        }, completion_marker=marker)
        result["machine_report_path"] = machine_path
        result["human_report_path"] = human_path
        return result


def execute_final_review(reference, **kwargs):
    """Retain the safe C12 execution-only seam used by prior integrations."""
    kwargs["execute_checks"] = True
    return _run_static_final_review(reference, **kwargs)


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
                    target_sha, head_sha, worker, environ, repo_root,
                    execute_checks=False, required_runner=None, dynamic_runner=None,
                    dynamic_kwargs=None):
    """Materialize and run every mandatory lens, returning detached unit output."""
    capabilities = () if composed is None else (
        (CROSS_ITEM_CONTRACTS, INTEGRATION_BEHAVIOR) if unit_key == "combined"
        else (INTEGRATION_BEHAVIOR,)
    )
    aggregation = None
    execution = None
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

            execution = execute_unit(
                request, isolated_worker, unit_id=unit_id,
                required_runner=((required_runner or run_required_verification)
                                 if execute_checks else None),
                dynamic_runner=((dynamic_runner or run_dynamic_checks)
                                if execute_checks else None),
                dynamic_kwargs=dynamic_kwargs, environ=environ,
            )
            aggregation = execution.aggregation
            _record_lens_aggregation(
                store, unit_id, aggregation, materialized, request=request)
            record_execution_tests(store, unit_id, execution)
            for rejected in execution.rejected_stops:
                store.record_warning(unit_id, "structured-stop-rejected", rejected["error"],
                                     rejected["stage"], rejected)
            # Stack STOP persistence is deferred until dependent units have IDs.
    except Exception as exc:
        if aggregation is None:
            aggregation = failed_lens_aggregation(exc)
            _record_lens_aggregation(store, unit_id, aggregation)
        store.record_coverage(unit_id, "repository", "incomplete",
                              "Immutable full-tree review did not complete", {"error": str(exc)})
        store.record_warning(unit_id, "materialization", str(exc), "repository")
    verdict = ((execution.finding_verdict if execute_checks else aggregation.finding_verdict)
               if aggregation is not None else None)
    state = "findings" if verdict == "block" and not (execution and execution.stop) else "incomplete"
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
        "_execution": execution,
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
        "_execution": None,
    }


def _stop_dependents(unit_key):
    """Supervisor graph mapping; worker-provided scopes are never consulted."""
    if unit_key.startswith("individual:"):
        position = int(unit_key.split(":", 1)[1])
        descendants = {f"individual:{number}" for number in range(position + 1, 1000)}
        return descendants | {"combined", "prospective"}
    if unit_key == "combined":
        return {"prospective"}
    return set()


def _record_skipped_stack_unit(store, graph, unit_key, unit_id, stop, execute_checks):
    execution = skipped_execution(stop, unit_id=unit_id, run_tests=execute_checks)
    _record_lens_aggregation(store, unit_id, execution.aggregation)
    record_execution_tests(store, unit_id, execution)
    # Preserve the dependent's complete skip ledger locally as well as in the
    # origin stop's affected-unit list. This keeps per-unit reports auditable.
    store.record_stop(
        unit_id, f"Dependent work skipped after {stop['reason_code']}",
        evidence={"dependent_skip": True, "original_stop": stop},
        affected_unit_ids=[unit_id], skipped_work=execution.skipped_work,
    )
    store.record_coverage(unit_id, "repository", "skipped",
                          "Dependent immutable tree was not used after a critical stop",
                          {"reason_code": stop["reason_code"], "evidence": stop["evidence"]})
    store.set_unit_state(unit_id, "incomplete", "Dependent work skipped after critical stop")
    return {
        "unit_id": unit_id, "unit_key": unit_key,
        "kind": "individual" if unit_key.startswith("individual:") else unit_key,
        "state": "incomplete", "finding_verdict": None,
        "static_lenses_complete": False, "base_sha": "",
        "head_sha": "", "merge_base_sha": None, "repository_path": "",
        "composition_complete": False, "composition_errors": [], "lenses": [],
        "inherited_stop": True, "_execution": execution,
    }


def _execute_composed_unit(store, identity, graph, unit_key, unit_id, composed,
                           worker, environ, repo_root, execute_checks=False,
                           required_runner=None, dynamic_runner=None, dynamic_kwargs=None):
    _record_stack_composition(store, unit_id, composed)
    if not composed["complete"]:
        return _incomplete_composed_unit(
            store, identity, graph, unit_key, unit_id, composed)
    return _run_stack_unit(
        store, identity, graph, unit_key, unit_id, composed=composed,
        source_repository=composed["repository"]["source_repository"],
        target_sha=composed["target_sha"], head_sha=composed["head_sha"],
        worker=worker, environ=environ, repo_root=repo_root,
        execute_checks=execute_checks, required_runner=required_runner,
        dynamic_runner=dynamic_runner, dynamic_kwargs=dynamic_kwargs,
    )


def _write_stack_unit_report(store, run_id, unit, links, *, stage=STACK_STAGE,
                             missing_stages=STACK_MISSING_STAGES):
    directory = store.path.parent / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", unit["unit_key"])
    machine_path = directory / f"{run_id}-{stem}.json"
    human_path = directory / f"{run_id}-{stem}.md"
    report = store.machine_report(run_id, unit["unit_id"])
    report["orchestration"] = {
        "stage": stage, "complete": False, "missing_stages": list(missing_stages), **links}
    temporary = machine_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True,
                                    ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, machine_path)
    human = store.human_report(run_id, unit["unit_id"])
    marker = (f"\nStage: {stage}\nCompletion: INCOMPLETE - " + (
        "per-unit static/required/dynamic execution evidence is recorded; official publication "
        "remains pending.\n" if stage == EXECUTION_STAGE else
        "required/dynamic tests and official publication remain pending.\n"
    ))
    human_path.write_text(human.replace("\n", marker, 1), encoding="utf-8")
    for path in (machine_path, human_path):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    unit["machine_report_path"] = str(machine_path)
    unit["human_report_path"] = str(human_path)


def run_final_stack(references, *, repo_root=None, provider=None, store_path=None,
                    worker=None, environ=None, current_root_sha=None, execute_checks=False,
                    required_runner=None, dynamic_runner=None, dynamic_kwargs=None,
                    context_kwargs=None, publish=False, publication_runner=None,
                    report_url=None, local_waivers=(), complete=False, native_host=None):
    """Validate and review a complete base-first stack through composition.

    The returned units and per-unit report paths are the C14 publication
    hand-off. Dynamic/required tests and official provider statuses deliberately
    remain pending, so a clean static result still exits incomplete.
    """
    if complete:
        from rg_final_stack import run_final_stack as run_complete_stack
        return run_complete_stack(
            references, repo_root=repo_root, provider=provider, store_path=store_path,
            worker=worker, environ=environ, current_root_sha=current_root_sha,
            required_runner=required_runner, dynamic_runner=dynamic_runner,
            dynamic_kwargs=dynamic_kwargs, context_kwargs=context_kwargs,
            publish=publish, publication_runner=publication_runner,
            report_url=report_url, local_waivers=local_waivers, native_host=native_host,
        )
    environ = os.environ if environ is None else environ
    transport = str(environ.get("RG_FINAL_REVIEW_TRANSPORT", "")).strip().lower()
    if transport not in ("", "legacy", "native"):
        raise ValueError(f"unsupported final-review transport {transport!r}")
    repo_root = str(repo_root or os.getcwd())
    provider = _selected_provider(references, provider, repo_root)
    stage = EXECUTION_STAGE if execute_checks else STACK_STAGE
    missing_stages = EXECUTION_STACK_MISSING_STAGES if execute_checks else STACK_MISSING_STAGES
    telemetry = FinalReviewTelemetry()
    graph = _discover_observed(
        discover_linear_graph, references, provider=provider, repo_root=repo_root,
        telemetry=telemetry,
    )
    items = graph["items"]
    with FinalStore(store_path) as store:
        identity = store.start_run(
            _graph_stack(graph), acceptance_version=store.resolve_acceptance_version(), metadata={
                "stage": stage, "complete": False,
                "missing_stages": missing_stages, "standalone": False,
                "graph": {"schema": graph["schema"], "item_keys": graph["item_keys"]},
            }, telemetry=telemetry,
        )
        run_id = identity["run_id"]
        combined_id = identity["unit_ids"]["combined"]
        unit_results = []
        stopped_dependents = {}
        for position, item in enumerate(items, 1):
            unit_id = identity["unit_ids"][f"individual:{position}"]
            if execute_checks:
                store.record_coverage(unit_id, "provider_publication", "not_implemented",
                                      "Official publication remains a trusted later stage")
            else:
                _record_stack_pending(store, unit_id)
        if execute_checks:
            store.record_coverage(combined_id, "provider_publication", "not_implemented",
                                  "Official publication remains a trusted later stage")
        else:
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
                unit_id = identity["unit_ids"][unit_key]
                if unit_key in stopped_dependents:
                    unit_results.append(_record_skipped_stack_unit(
                        store, graph, unit_key, unit_id, stopped_dependents[unit_key],
                        execute_checks))
                else:
                    unit_results.append(_run_stack_unit(
                        store, identity, graph, unit_key, unit_id,
                        item=item, source_repository=source_repository,
                        target_repository=target_repository, target_sha=target["sha"],
                        head_sha=source["sha"], worker=worker, environ=environ,
                        repo_root=repo_root, execute_checks=execute_checks,
                        required_runner=required_runner, dynamic_runner=dynamic_runner,
                        dynamic_kwargs=dynamic_kwargs,
                    ))
                execution = unit_results[-1]["_execution"]
                if execution and execution.stop and not unit_results[-1].get("inherited_stop"):
                    for dependent in _stop_dependents(unit_key):
                        stopped_dependents.setdefault(dependent, execution.stop)
            if "combined" in stopped_dependents:
                unit_results.append(_record_skipped_stack_unit(
                    store, graph, "combined", combined_id,
                    stopped_dependents["combined"], execute_checks))
            else:
                unit_results.append(_execute_composed_unit(
                    store, identity, graph, "combined", combined_id,
                    composition["combined"], worker, environ, repo_root,
                    execute_checks=execute_checks, required_runner=required_runner,
                    dynamic_runner=dynamic_runner, dynamic_kwargs=dynamic_kwargs))
            combined_execution = unit_results[-1]["_execution"]
            if combined_execution and combined_execution.stop and \
                    not unit_results[-1].get("inherited_stop"):
                for dependent in _stop_dependents("combined"):
                    stopped_dependents.setdefault(dependent, combined_execution.stop)
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
            if execute_checks:
                store.record_coverage(prospective_id, "provider_publication", "not_implemented",
                                      "Official publication remains a trusted later stage")
            else:
                _record_stack_pending(store, prospective_id)
            if "prospective" in stopped_dependents:
                unit_results.append(_record_skipped_stack_unit(
                    store, graph, "prospective", prospective_id,
                    stopped_dependents["prospective"], execute_checks))
            else:
                unit_results.append(_execute_composed_unit(
                    store, identity, graph, "prospective", prospective_id,
                    prospective, worker, environ, repo_root,
                    execute_checks=execute_checks, required_runner=required_runner,
                    dynamic_runner=dynamic_runner, dynamic_kwargs=dynamic_kwargs))

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

        # Only now are every origin and dependent ID known. Record each original
        # STOP once, with the supervisor-computed blast radius and all skips.
        origin_stops = []
        for origin in unit_results:
            execution = origin["_execution"]
            if not execution or not execution.stop or origin.get("inherited_stop"):
                continue
            affected = [unit for unit in unit_results if unit["unit_key"] == origin["unit_key"]
                        or unit["unit_key"] in _stop_dependents(origin["unit_key"])]
            for dependent in affected:
                if dependent is origin:
                    continue
                execution.skipped_work.extend(deepcopy(
                    dependent["_execution"].skipped_work
                    if dependent["_execution"] else skipped_execution(
                        execution.stop, unit_id=dependent["unit_id"],
                        run_tests=execute_checks).skipped_work))
            execution.stop_id = _record_execution_evidence(
                store, origin["unit_id"], execution,
                affected_units=[unit["unit_id"] for unit in affected], record_tests=False)
            origin_stops.append(origin)

        verdicts = [unit["finding_verdict"] for unit in unit_results]
        finding_verdict = (None if origin_stops else
                           "block" if "block" in verdicts
                           else "pass" if verdicts and all(v == "pass" for v in verdicts)
                           else None)
        static_complete = all(unit["static_lenses_complete"] for unit in unit_results)
        state = "stale" if stale else "incomplete"
        trust = store.finish_run(run_id, state, trust={
            "local_only": True, "enforced": False, "official": False,
            "stage_incomplete": True, "missing_stages": missing_stages,
            "static_lenses_complete": static_complete,
            "stopped": bool(origin_stops),
            "reason": "C12 executes safe evidence and scoped stops; official publication remains"
            if execute_checks else
            "C10 composes and reviews statically; tests and official publication remain",
        })
        exit_code = 3 if stale else 1 if finding_verdict == "block" else 2
        result = {
            "schema": RESULT_SCHEMA, "stage": stage, "complete": False,
            "missing_stages": missing_stages, "state": state, "exit_code": exit_code,
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
            "execution_complete": not origin_stops and all(
                not unit["_execution"] or unit["_execution"].checks_complete
                for unit in unit_results),
            "stops": [_execution_result_fields(unit["_execution"])["stops"][0]
                      for unit in origin_stops],
        }
        for unit in unit_results:
            execution = unit.pop("_execution")
            unit.update(_execution_result_fields(execution))
        marker = (f"\nStage: {stage}\nCompletion: INCOMPLETE - " + (
            "ordered per-unit static/required/dynamic execution evidence is recorded; "
            "official publication remains pending.\n" if execute_checks else
            "composition and all static lenses ran; required/dynamic tests and official "
            "publication remain pending.\n"
        ))
        machine_path, human_path = _write_reports(store, run_id, {
            "schema": RESULT_SCHEMA, "stage": stage, "complete": False,
            "missing_stages": missing_stages, "state": state,
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
            }, stage=stage, missing_stages=missing_stages)
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


def execute_final_stack(references, **kwargs):
    """Run full C12 per-unit execution, preserving safe independent stack work."""
    kwargs["execute_checks"] = True
    return run_final_stack(references, **kwargs)


def _parse_main_arguments(args):
    """Parse publication guards without mistaking any positional ref for a flag.

    SPEC-16's explicit operator command requests guarded publication by default
    for both standalone and complete-stack forms. ``--no-publish`` is the local-
    only escape hatch; ``--publish`` states the default explicitly.
    """
    publish = None
    references = []
    position = 0
    while position < len(args):
        argument = args[position]
        if argument == "--":
            references.extend(args[position + 1:])
            break
        if argument in ("--publish", "--no-publish"):
            selected = argument == "--publish"
            if publish is not None and publish != selected:
                raise ValueError("--publish and --no-publish are mutually exclusive")
            publish = selected
        elif argument.startswith("--"):
            raise ValueError(f"unknown final-review option: {argument}")
        else:
            references.append(argument)
        position += 1
    if not references:
        raise ValueError("requires at least one explicit ref")
    return references, True if publish is None else publish


def main(argv=None):
    """Run final review with guarded publication on by default for all ref counts."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "final-review":
        args = args[1:]
    elif len(args) > 1 and args[1] == "final-review":
        args = args[2:]
    if not args:
        sys.stderr.write("review-gate final-review requires at least one explicit ref.\n")
        return 2
    try:
        # Numeric shorthand and canonical URLs share provider selection, graph
        # validation, and the complete standalone/stack publication lifecycle.
        references, publish = _parse_main_arguments(args)
        result = run_final_review(
            references if len(references) > 1 else references[0], publish=publish)
    except Exception as exc:
        sys.stderr.write(f"review-gate final-review failed: {exc}\n")
        return 2
    sys.stdout.write(json.dumps(result, sort_keys=True, ensure_ascii=False) + "\n")
    completion = "complete" if result["complete"] else "incomplete"
    sys.stderr.write(
        f"review-gate final-review: {result['state']}; stage {result['stage']} is {completion}; "
        f"finding verdict: {result['finding_verdict'] or 'unavailable'}; "
        f"run {result['run_id']}\n"
    )
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
