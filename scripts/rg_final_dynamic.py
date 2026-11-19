"""Explicit, isolated SPEC-15 dynamic checks for one SPEC-16 review unit.

Calling :func:`run_dynamic_checks` is the opt-in.  Nothing in this module is a
pre-push hook or default repository duty.  A call materializes a fresh pinned
clone, invokes the existing reference selector/executor once in that clone,
and returns a compact JSON-compatible result.  Required-suite policy remains
independent: every dynamic setup, selector, build, flake, coverage, executor,
or timeout limitation is visible but never masquerades as required verification.

Only test output and normalized findings cross the executor boundary.  Probe
source, selector manifests, and scratch paths are deliberately absent from the
result.  :func:`record_dynamic_result` persists that result through FinalStore's
existing public methods, so individual and future combined units share one
storage contract without requiring a store-schema or orchestration change.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess

from rg_execution_ownership import run_owned_process
from rg_final_env import scrub_environment
from rg_final_materialize import materialize_review


RESULT_SCHEMA = "final-review-dynamic-result/v1"
DEFAULT_TIMEOUT_S = 600.0
DEFAULT_EXECUTOR = Path(__file__).resolve().parents[1] / "examples" / "test-check" / "test_check"
PROBE_ROOT = ".test-check-probes"
_EVIDENCE_SOURCES = frozenset(("existing_test", "ephemeral_test"))
def _sha256(text):
    return hashlib.sha256(str(text).encode("utf-8", "surrogateescape")).hexdigest()


def _warning(code, message, file=None, **details):
    return {
        "code": str(code),
        "message": str(message),
        "file": file if isinstance(file, str) else None,
        "scope": "dynamic",
        "details": details,
    }


def _base_result(unit_key, target_sha, head_sha):
    return {
        "schema": RESULT_SCHEMA,
        "unit_key": str(unit_key),
        "state": "degraded",
        "complete": False,
        "findings": [],
        "warnings": [],
        "tests": [],
        "coverage": {
            "status": "unknown",
            "summary": "Dynamic checks did not produce test evidence",
        },
        "telemetry": {
            "executor_attempts": 0,
            "changed_files": 0,
        },
        "revisions": {
            "target_sha": str(target_sha),
            "head_sha": str(head_sha),
            "merge_base_sha": None,
        },
    }


def _executor_environment(environ):
    """Give the selector only model auth, never publication credentials.

    The reference executor applies a second, stricter scrub before running
    repository tests or probes, removing model credentials as well.  Provider
    and write/publication material is removed before *either* process starts.
    """
    return scrub_environment(environ, preserve_model_auth=True)


def _changed_paths(repository, merge_base, head_sha):
    process = run_owned_process(
        ["git", "diff", "--name-only", "--no-ext-diff", "--no-textconv",
         "-z", merge_base, head_sha],
        cwd=str(repository), capture_output=True, timeout=30, check=False,
    )
    if process.returncode:
        detail = process.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"could not enumerate merge-base changes: {detail}")
    return [item.decode("utf-8", "surrogateescape")
            for item in process.stdout.split(b"\0") if item]


def _valid_paths(paths):
    if not isinstance(paths, (list, tuple)):
        return False
    for value in paths:
        if not isinstance(value, str) or not value or "\0" in value:
            return False
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "." == value:
            return False
    return True


def _normalize_paths(paths):
    return list(dict.fromkeys(paths))


def _reference_warning_code(message):
    lowered = message.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "dynamic-timeout"
    if "selector failed" in lowered:
        return "dynamic-selector"
    if "no permanent test coverage" in lowered:
        return "dynamic-coverage"
    if "flaky" in lowered or "failed once and passed" in lowered:
        return "dynamic-flake"
    if "failed to build or run" in lowered or "probe" in lowered:
        return "dynamic-probe"
    return "dynamic-test"


def _normalize_findings(raw, warnings):
    findings = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not isinstance(item.get("content"), str) \
                or not item["content"].strip():
            warnings.append(_warning(
                "dynamic-executor-result",
                f"dynamic executor finding {index + 1} was malformed and discarded",
            ))
            continue
        finding = dict(item)
        finding["lens"] = "dynamic"
        source = finding.get("evidence_source")
        if source not in _EVIDENCE_SOURCES:
            if source is not None:
                warnings.append(_warning(
                    "dynamic-executor-result",
                    "dynamic executor supplied an unknown evidence_source; the tag was discarded",
                    file=finding.get("path"),
                ))
            finding.pop("evidence_source", None)
        findings.append(finding)
    return findings


def _normalize_warnings(raw):
    warnings = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not isinstance(item.get("message"), str) \
                or not item["message"].strip():
            warnings.append(_warning(
                "dynamic-executor-result",
                f"dynamic executor warning {index + 1} was malformed and discarded",
            ))
            continue
        message = item["message"]
        warnings.append(_warning(
            _reference_warning_code(message), message, item.get("file"),
            source="reference_executor",
        ))
    return warnings


def _test_evidence(findings, unit_key):
    tests = []
    for finding in findings:
        source = finding.get("evidence_source")
        if source not in _EVIDENCE_SOURCES:
            continue
        content = finding["content"]
        path = finding.get("path") if isinstance(finding.get("path"), str) else "dynamic-check"
        tests.append({
            "name": path,
            "kind": source,
            "state": "reproduced_failure",
            "output": content,
            "evidence_fingerprint": _sha256(content),
            "details": {
                "unit_key": unit_key,
                "path": finding.get("path"),
                "start_line": finding.get("start_line"),
            },
        })
    return tests


def _coverage(telemetry, warnings, complete):
    selected = telemetry.get("tests_selected", 0)
    probes = telemetry.get("probes_authored", 0)
    codes = {item["code"] for item in warnings}
    if selected:
        status = "existing_tests"
        summary = f"Selector ran {selected} existing test file(s)"
    elif probes:
        status = "ephemeral_only"
        summary = "Only ephemeral dynamic probes supplied test evidence"
    elif "dynamic-coverage" in codes:
        status = "uncovered"
        summary = "The selector found no existing tests or probes"
    elif not complete or "dynamic-selector" in codes:
        status = "unknown"
        summary = "Dynamic test coverage could not be assessed"
    else:
        status = "not_applicable"
        summary = "No dynamic test subject was applicable"
    return {"status": status, "summary": summary}


def _finish(result, *, complete):
    result["complete"] = bool(complete)
    result["tests"] = _test_evidence(result["findings"], result["unit_key"])
    result["coverage"] = _coverage(
        result["telemetry"], result["warnings"], result["complete"])
    if result["findings"]:
        result["state"] = "findings"
    elif result["warnings"]:
        result["state"] = "degraded"
    else:
        result["state"] = "passed"
    return result


def _consume_response(result, process):
    """Accept one executor response; never retry a failed test or executor."""
    result["telemetry"]["executor_exit_code"] = process.returncode
    if process.returncode:
        detail = (process.stderr or process.stdout or "").strip()[:2000]
        result["warnings"].append(_warning(
            "dynamic-executor-exit",
            f"dynamic executor exited {process.returncode}; no dynamic evidence is assumed",
            output=detail,
        ))
        return _finish(result, complete=False)
    try:
        response = json.loads(process.stdout)
    except (TypeError, ValueError):
        result["warnings"].append(_warning(
            "dynamic-executor-result",
            "dynamic executor returned no valid JSON result; no dynamic evidence is assumed",
            output=(process.stdout or "")[:2000],
        ))
        return _finish(result, complete=False)
    if not isinstance(response, dict) \
            or not isinstance(response.get("findings"), list) \
            or not isinstance(response.get("warnings"), list):
        result["warnings"].append(_warning(
            "dynamic-executor-result",
            "dynamic executor result must contain findings and warnings arrays",
        ))
        return _finish(result, complete=False)

    telemetry = response.get("telemetry", {})
    if not isinstance(telemetry, dict):
        telemetry = {}
        result["warnings"].append(_warning(
            "dynamic-executor-result",
            "dynamic executor telemetry was malformed and ignored",
        ))
    own = result["telemetry"]
    result["telemetry"] = dict(telemetry)
    result["telemetry"].update(own)
    result["findings"] = _normalize_findings(response["findings"], result["warnings"])
    result["warnings"].extend(_normalize_warnings(response["warnings"]))
    return _finish(result, complete=True)


def _invoke_executor(executor, paths, cwd, environ, timeout, result):
    command = [str(executor)] if isinstance(executor, (str, os.PathLike)) else list(executor or [])
    if not command:
        command = [str(DEFAULT_EXECUTOR)]
    environment = _executor_environment(environ)
    environment["OCR_CHECK_TIMEOUT_S"] = str(timeout)
    result["telemetry"]["executor_attempts"] += 1
    try:
        process = run_owned_process(
            command, cwd=str(cwd), input=json.dumps(paths), env=environment,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        result["telemetry"]["executor_timeouts"] = \
            result["telemetry"].get("executor_timeouts", 0) + 1
        result["warnings"].append(_warning(
            "dynamic-timeout",
            f"dynamic executor timed out after {timeout:g}s; its evidence was discarded",
        ))
        return _finish(result, complete=False)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        result["warnings"].append(_warning(
            "dynamic-executor",
            f"dynamic executor could not run: {exc}",
        ))
        return _finish(result, complete=False)
    return _consume_response(result, process)


def run_dynamic_checks(repository, target_sha, head_sha, *, changed_paths=None,
                       target_repository=None, executor=None, environ=None,
                       timeout=DEFAULT_TIMEOUT_S, unit_key="individual"):
    """Run SPEC-15 dynamic evidence for one explicitly opted-in final unit.

    ``repository`` is read-only input, never the execution directory.  Target
    and head must be full immutable SHAs accepted by ``materialize_review``.
    Future composed units may supply their composed repository and optional
    repo-relative ``changed_paths``; otherwise paths come from the actual
    merge-base diff.  ``executor`` is an argv/path override for scripted tests,
    not a shell command.  Dynamic degradation is returned as warnings, never
    raised as a required-suite failure and never retried by this wrapper.
    """
    result = _base_result(unit_key, target_sha, head_sha)
    try:
        seconds = float(timeout)
        if seconds <= 0:
            raise ValueError("dynamic timeout must be positive")
    except (TypeError, ValueError) as exc:
        result["warnings"].append(_warning("dynamic-timeout", str(exc)))
        return _finish(result, complete=False)

    try:
        with materialize_review(
            repository, target_sha, head_sha, target_repository=target_repository
        ) as materialized:
            result["revisions"]["merge_base_sha"] = materialized.merge_base_sha
            paths = (_changed_paths(materialized.path, materialized.merge_base_sha,
                                    materialized.head_sha)
                     if changed_paths is None else list(changed_paths))
            if not _valid_paths(paths):
                result["warnings"].append(_warning(
                    "dynamic-paths",
                    "dynamic changed paths must be non-empty repository-relative paths without '..'",
                ))
                return _finish(result, complete=False)
            paths = _normalize_paths(paths)
            result["telemetry"]["changed_files"] = len(paths)
            if not paths:
                result["state"] = "not_applicable"
                result["complete"] = True
                result["coverage"] = {
                    "status": "not_applicable",
                    "summary": "The pinned unit has no changed paths",
                }
                return result
            return _invoke_executor(
                executor, paths, materialized.path, environ, seconds, result)
    except Exception as exc:  # dynamic setup is best-effort and always isolated
        result["warnings"].append(_warning(
            "dynamic-materialization",
            f"dynamic checks could not materialize or inspect the pinned unit: {exc}",
        ))
        return _finish(result, complete=False)


def record_dynamic_result(store, unit_id, result):
    """Persist a dynamic result using only FinalStore's existing public seam.

    The caller retains responsibility for verdict aggregation and unit state;
    this function records evidence only.  It works identically for individual
    and combined unit IDs and deliberately stores no probe source or clone path.
    """
    if not isinstance(result, dict) or result.get("schema") != RESULT_SCHEMA:
        raise ValueError("unsupported final-review dynamic result")
    ids = {"test_ids": [], "finding_ids": [], "warning_ids": [], "coverage_id": None}
    telemetry = dict(result.get("telemetry") or {})
    for test in result.get("tests") or []:
        ids["test_ids"].append(store.record_test(
            unit_id, test.get("name", "dynamic-check"), test.get("kind", "dynamic_test"),
            test.get("state", "unknown"), output=test.get("output", ""),
            evidence_fingerprint=test.get("evidence_fingerprint", ""),
            details=test.get("details") or {},
        ))
    used = set()
    for finding in result.get("findings") or []:
        content = str(finding.get("content") or "")
        fingerprint = _sha256(content)
        base = "dynamic-" + fingerprint[:24]
        key = base
        suffix = 2
        while key in used:
            key = f"{base}-{suffix}"
            suffix += 1
        used.add(key)
        ids["finding_ids"].append(store.record_finding(
            unit_id, key, content, str(finding.get("severity") or ""),
            str(finding.get("confidence", "")), lens="dynamic",
            path=finding.get("path"),
            line=finding.get("start_line") if isinstance(
                finding.get("start_line"), int) else None,
            anchors=[{
                "anchor_type": "dynamic_evidence",
                "anchor": "sha256:" + _sha256(json.dumps(
                    finding, sort_keys=True, ensure_ascii=False, default=str)),
                "evidence_fingerprint": fingerprint,
                "details": {"evidence_source": finding.get("evidence_source")},
            }],
            evidence={
                "schema": RESULT_SCHEMA,
                "finding": dict(finding),
                "telemetry": telemetry,
            },
        ))
    for warning in result.get("warnings") or []:
        details = dict(warning.get("details") or {})
        if warning.get("file") is not None:
            details["file"] = warning["file"]
        ids["warning_ids"].append(store.record_warning(
            unit_id, warning.get("code", "dynamic"), warning.get("message", ""),
            warning.get("scope", "dynamic"), details,
        ))
    coverage = result.get("coverage") or {}
    ids["coverage_id"] = store.record_coverage(
        unit_id, "dynamic_tests", coverage.get("status", "unknown"),
        coverage.get("summary", "Dynamic check result"),
        {
            "schema": RESULT_SCHEMA,
            "unit_key": result.get("unit_key"),
            "complete": bool(result.get("complete")),
            "state": result.get("state"),
            "telemetry": telemetry,
            "revisions": result.get("revisions") or {},
        },
    )
    return ids
