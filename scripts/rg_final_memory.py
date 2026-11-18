"""SPEC-16 final-finding continuity, revision freshness, and waiver isolation.

The functions in this module are the orchestrator-facing contract.  Finding
history is durably implemented by :class:`rg_final_store.FinalStore`; this
module deliberately accepts that public store seam rather than maintaining a
second memory.  Provider access is read-only and injected, so tests and later
orchestration can supply scripted revision providers.

A finding is never retired because a later reviewer omitted it. Resolution
requires independently inspected current-tree proof plus an exact successful
test executed by the supervisor in the same run; falsification requires a
clear rationale and independently inspected or executed evidence. Existing
pre-push waiver ledgers are accepted only as detached operator context.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess

RERUN_CONTEXT_SCHEMA = "final-review-rerun-context/v1"
FRESHNESS_SCHEMA = "final-review-freshness/v1"
CLOSURE_VERIFICATION_SCHEMA = "final-review-closure-verification/v1"
_SUCCESSFUL_TEST_STATES = frozenset(("pass", "passed", "success", "succeeded"))
_CLOSURE_TEST_KINDS = frozenset(("required", "existing_test", "ephemeral_test"))
CARRY_FORWARD_INSTRUCTION = (
    "Explicitly carry this unresolved finding into the new run and revalidate it. "
    "Do not treat omission as resolution. Retirement requires changed code/evidence "
    "plus positive revalidation, or an explicit falsification with rationale."
)

_COORDINATES = (
    ("source.project", ("source", "project_id")),
    ("source.branch", ("source", "branch")),
    ("source.sha", ("source", "sha")),
    ("target.project", ("target", "project_id")),
    ("target.branch", ("target", "branch")),
    ("target.sha", ("target", "sha")),
    ("target.base_sha", ("target", "base_sha")),
)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)


def _digest(value):
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def normalize_anchors(anchors=(), finding=None):
    """Return detached, deterministic content/evidence anchors.

    Existing anchor strings and evidence fingerprints are preserved verbatim.
    A finding without stored anchors receives a derived content anchor for
    continuity only; this does not add anchors to its historical store row.
    """
    finding = finding or {}
    normalized = []
    for supplied in anchors or ():
        if not isinstance(supplied, dict):
            raise ValueError("finding anchors must be mappings")
        anchor_type = str(supplied.get("anchor_type") or "").strip()
        anchor = str(supplied.get("anchor") or "").strip()
        if not anchor_type or not anchor:
            raise ValueError("finding anchors require anchor_type and anchor")
        normalized.append({
            "anchor_type": anchor_type,
            "anchor": anchor,
            "evidence_fingerprint": str(supplied.get("evidence_fingerprint") or ""),
            "details": copy.deepcopy(supplied.get("details") or {}),
        })
    if not any(item["anchor_type"] == "content" for item in normalized):
        identity = {
            "finding_key": finding.get("finding_key") or finding.get("id") or "",
            "content": finding.get("content") or "",
            "lens": finding.get("lens") or "",
            "path": finding.get("path") or "",
        }
        normalized.insert(0, {
            "anchor_type": "content", "anchor": _digest(identity),
            "evidence_fingerprint": "",
            "details": {"path": finding.get("path"), "line": finding.get("line")},
        })
    return normalized


def finding_content_anchor(finding=None, anchors=()):
    """The stable same-defect identity used within one stack and unit scope."""
    normalized = normalize_anchors(anchors, finding)
    return next(item["anchor"] for item in normalized if item["anchor_type"] == "content")


def finding_evidence_anchor(anchors=(), evidence=None, finding=None):
    """Fingerprint all code/evidence coordinates, independently of content identity."""
    normalized = normalize_anchors(anchors, finding)
    coordinates = [{
        "anchor_type": item["anchor_type"],
        "anchor": item["anchor"],
        "evidence_fingerprint": item["evidence_fingerprint"],
    } for item in normalized]
    if not anchors and evidence is not None:
        coordinates.append({"evidence": evidence})
    return _digest(coordinates)


def _unwrap_revision(entry):
    if not isinstance(entry, dict):
        raise ValueError("revision entries must be mappings")
    wrapped = entry.get("pinned", entry.get("item"))
    if isinstance(wrapped, dict):
        item = copy.deepcopy(wrapped)
        if "ancestors" in entry and "ancestors" not in item:
            item["ancestors"] = copy.deepcopy(entry["ancestors"])
        return item
    return copy.deepcopy(entry)


def _change(changes, field, expected, current, item_key=None):
    change = {"field": field, "expected": expected, "current": current}
    if item_key is not None:
        change["item_key"] = item_key
    changes.append(change)


def _compare_item(pinned, current, *, prefix="", item_key=None, changes, errors):
    for field, (side, coordinate) in _COORDINATES:
        expected = pinned.get(side, {}).get(coordinate)
        actual = current.get(side, {}).get(coordinate)
        if expected is None or expected == "":
            errors.append({
                "code": "incomplete-revision-pin", "item_key": item_key,
                "field": prefix + field,
                "message": f"pinned {prefix + field} is unavailable",
            })
            continue
        if actual is None or actual == "":
            errors.append({
                "code": "incomplete-current-revision", "item_key": item_key,
                "field": prefix + field,
                "message": f"current {prefix + field} is unavailable",
            })
            continue
        if expected != actual:
            _change(changes, prefix + field, expected, actual, item_key)

    expected_ancestors = pinned.get("ancestors")
    current_ancestors = current.get("ancestors")
    if expected_ancestors is None and current_ancestors is None:
        return
    expected_ancestors = list(expected_ancestors or ())
    current_ancestors = list(current_ancestors or ())
    if len(expected_ancestors) != len(current_ancestors):
        _change(
            changes, prefix + "ancestors",
            [_ancestor_key(item) for item in expected_ancestors],
            [_ancestor_key(item) for item in current_ancestors], item_key,
        )
    for index, (expected, actual) in enumerate(zip(expected_ancestors, current_ancestors)):
        _compare_item(
            _unwrap_revision(expected), _unwrap_revision(actual),
            prefix=f"{prefix}ancestors[{index}].",
            item_key=_ancestor_key(expected), changes=changes, errors=errors,
        )


def _ancestor_key(item):
    if not isinstance(item, dict):
        return str(item)
    wrapped = item.get("pinned", item.get("item"))
    if isinstance(wrapped, dict):
        item = wrapped
    return item.get("item_key") or item.get("id") or item.get("url") or "unknown"


def _assessment(pinned, current, changes, errors):
    state = "stale" if changes else "incomplete" if errors else "fresh"
    return {
        "schema": FRESHNESS_SCHEMA,
        "state": state,
        "fresh": state == "fresh",
        "changes": changes,
        "errors": errors,
        "pinned": pinned,
        "current": current,
        "restart_required": state == "stale",
        "automatic_restart": False,
        "publication_pass_allowed": state == "fresh",
        "restart": {"required": state == "stale", "automatic": False},
    }


def compare_revision_tuples(pinned, current):
    """Compare complete source/target/base and ancestor revision identities.

    Either side may be one normalized provider item or a sequence of items (or
    store wrappers containing ``pinned``/``item``). Movement is stale evidence;
    unavailable coordinates are incomplete evidence. Neither outcome permits
    an official pass, and this function never starts another run.
    """
    many = isinstance(pinned, (list, tuple)) or isinstance(current, (list, tuple))
    expected_items = list(pinned) if isinstance(pinned, (list, tuple)) else [pinned]
    current_items = list(current) if isinstance(current, (list, tuple)) else [current]
    changes = []
    errors = []
    if len(expected_items) != len(current_items):
        _change(
            changes, "items", [_ancestor_key(item) for item in expected_items],
            [_ancestor_key(item) for item in current_items],
        )
    for index, (expected, current_item) in enumerate(zip(expected_items, current_items)):
        expected_raw = _unwrap_revision(expected)
        current_raw = _unwrap_revision(current_item)
        item_key = (expected.get("item_key") if isinstance(expected, dict) else None) or \
            _ancestor_key(expected_raw)
        prefix = f"items[{index}]." if many and len(expected_items) > 1 else ""
        _compare_item(
            expected_raw, current_raw, prefix=prefix, item_key=item_key,
            changes=changes, errors=errors,
        )
    pinned_view = {str(index): _unwrap_revision(item) for index, item in enumerate(expected_items)}
    current_view = {
        str(index): (_unwrap_revision(item) if item is not None else None)
        for index, item in enumerate(current_items)
    }
    return _assessment(pinned_view, current_view, changes, errors)


def _reference(entry, position, references):
    if isinstance(references, dict):
        key = entry.get("item_key") or _ancestor_key(entry)
        if key in references:
            return references[key]
    elif isinstance(references, (list, tuple)) and position < len(references):
        return references[position]
    elif isinstance(references, str) and position == 0:
        return references
    if isinstance(entry, dict):
        return entry.get("reference") or entry.get("url") or _unwrap_revision(entry).get("url")
    return None


def refresh_freshness(provider, pinned, *, references=None, repo_root=None):
    """Refetch every pinned item through ``provider.read`` and compare all tuples.

    Provider failures are returned as incomplete evidence rather than being
    mistaken for freshness. No provider mutation, retry loop, or run creation
    is performed.
    """
    pinned_items = list(pinned) if isinstance(pinned, (list, tuple)) else [pinned]
    current = []
    errors = []
    for position, entry in enumerate(pinned_items):
        reference = _reference(entry, position, references)
        item_key = (entry.get("item_key") if isinstance(entry, dict) else None) or \
            _ancestor_key(entry)
        if not reference:
            errors.append({
                "code": "missing-provider-reference", "item_key": item_key,
                "message": "no provider reference is pinned for freshness re-fetch",
            })
            current.append(None)
            continue
        try:
            current.append(provider.read(reference, repo_root=repo_root))
        except Exception as exc:
            errors.append({
                "code": "provider-read-failed", "item_key": item_key,
                "message": str(exc),
            })
            current.append(None)
    comparable_current = [
        item if item is not None else _unwrap_revision(pinned_items[index])
        for index, item in enumerate(current)
    ]
    result = compare_revision_tuples(pinned_items, comparable_current)
    # A failed read is incomplete evidence, not proof that all fields stayed
    # unchanged. Keep successful coordinates visible without comparing the
    # failed item against itself as a fresh claim.
    if len(pinned_items) == 1 and current[0] is None:
        result["changes"] = []
        result["current"] = {"0": None}
    result["errors"] = [*errors, *result["errors"]]
    result["fresh"] = not result["changes"] and not result["errors"]
    result["state"] = "stale" if result["changes"] else "incomplete" if result["errors"] else "fresh"
    result["restart_required"] = result["state"] == "stale"
    result["restart"] = {"required": result["restart_required"], "automatic": False}
    result["publication_pass_allowed"] = result["fresh"]
    return result


def refresh_run_freshness(store, run_id, provider, *, references=None, repo_root=None):
    """Refetch and durably record freshness for an existing run, never a restart."""
    pinned = store.pinned_revisions(run_id)
    result = refresh_freshness(
        provider, pinned, references=references, repo_root=repo_root)
    return store.record_freshness(run_id, result)


def rerun_context(store, run_id):
    """Return outstanding same-store findings that the new run must address."""
    return store.rerun_context(run_id)


def carry_forward(store, run_id, unit_id, thread_id, *, finding_id=None,
                  anchors=None, evidence=None):
    """Explicitly attach a prior thread to a current finding observation."""
    return store.carry_finding(
        run_id, unit_id, thread_id, finding_id=finding_id,
        anchors=anchors, evidence=evidence,
    )


def resolve_finding(store, run_id, thread_id, *, anchors, resolution_evidence,
                    revalidation):
    """Close a thread only after changed evidence and positive revalidation."""
    return store.resolve_finding(
        run_id, thread_id, anchors=anchors, resolution_evidence=resolution_evidence,
        revalidation=revalidation,
    )


def falsify_finding(store, run_id, thread_id, rationale, *, evidence=None):
    """Close a thread as falsified without requiring a code/evidence change."""
    return store.falsify_finding(run_id, thread_id, rationale, evidence=evidence)


def finding_memory_for_unit(prior, unit_key):
    """Restrict inherited context so one unit cannot close another unit's thread."""
    selected = copy.deepcopy(prior or {})
    selected["findings"] = [
        finding for finding in selected.get("findings", [])
        if finding.get("unit_key") == unit_key
    ]
    selected["required"] = bool(selected["findings"])
    return selected


def _closure_reference(closure, prior_by_id):
    finding_id = str(
        closure.get("finding_id") or closure.get("prior_finding_id") or "").strip()
    content_anchor = str(
        closure.get("content_anchor") or closure.get("prior_content_anchor") or "").strip()
    if not finding_id or not content_anchor:
        raise ValueError("finding closure requires a prior finding ID and content anchor")
    prior = prior_by_id.get(finding_id)
    if prior is None:
        raise ValueError("finding closure does not identify an inherited finding for this unit")
    if content_anchor != prior.get("content_anchor"):
        raise ValueError("finding closure content anchor does not match its prior finding")
    thread_id = closure.get("thread_id") or closure.get("memory_thread_id")
    if thread_id is not None and str(thread_id) != str(prior.get("thread_id")):
        raise ValueError("finding closure thread does not match its prior finding")
    return prior


def _recorded_evidence(value):
    return bool(value.strip()) if isinstance(value, str) else bool(value)


def _closure_coordinates(context):
    if not isinstance(context, dict):
        raise ValueError("closure requires trusted materialization context")
    unit_key = str(context.get("unit_key") or "")
    head_sha = str(context.get("head_sha") or "")
    if not unit_key or len(head_sha) != 40 or any(
            character not in "0123456789abcdefABCDEF" for character in head_sha):
        raise ValueError("closure context requires the current unit and head SHA")
    return unit_key, head_sha


def current_source_anchor(source):
    """Construct the only source anchor accepted by the closure store API."""
    identity = {
        "path": source["path"], "start_line": source["start_line"],
        "end_line": source["end_line"], "body_sha256": source["body_sha256"],
        "file_sha256": source["file_sha256"], "unit_key": source["unit_key"],
        "head_sha": source["head_sha"],
    }
    return {
        "anchor_type": "current_source", "anchor": _digest(identity),
        "evidence_fingerprint": source["file_sha256"],
        "details": copy.deepcopy(source),
    }


def _source_proof(evidence, prior_finding, context):
    """Independently bind a claimed excerpt to the live trusted materialization."""
    unit_key, head_sha = _closure_coordinates(context)
    if not isinstance(evidence, dict) or not isinstance(evidence.get("source"), dict):
        raise ValueError("closure requires current-tree source evidence")
    supplied = evidence["source"]
    if supplied.get("unit_key") != unit_key:
        raise ValueError("closure source unit does not match the current unit")
    if supplied.get("head_sha") != head_sha:
        raise ValueError("closure source SHA does not match the current materialization")
    path = supplied.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("closure source requires a path")
    relative = PurePosixPath(path)
    if relative.is_absolute() or str(relative) != path or ".." in relative.parts:
        raise ValueError("closure source path must be a normalized repository-relative path")
    original_paths = {prior_finding.get("path")}
    original_paths.update(
        (anchor.get("details") or {}).get("path")
        for anchor in prior_finding.get("anchors") or ()
    )
    if path not in original_paths - {None, ""}:
        raise ValueError("closure source must inspect the original finding path")
    start = supplied.get("start_line")
    end = supplied.get("end_line")
    if (not isinstance(start, int) or isinstance(start, bool) or
            not isinstance(end, int) or isinstance(end, bool) or start < 1 or end < start):
        raise ValueError("closure source requires a positive line range")
    body = supplied.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("closure source requires a non-empty exact body")
    try:
        root = Path(context.get("path") or "").resolve(strict=True)
        candidate = (root / Path(*relative.parts)).resolve(strict=True)
        candidate.relative_to(root)
        if not root.is_dir() or not candidate.is_file():
            raise ValueError("closure source is not a materialized regular file")
        current = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
            text=True, timeout=30, check=False,
        )
        if current.returncode or current.stdout.strip() != head_sha:
            raise ValueError("materialized HEAD does not match the closure source SHA")
        pinned = subprocess.run(
            ["git", "--no-replace-objects", "show", f"{head_sha}:{path}"],
            cwd=root, capture_output=True, timeout=30, check=False,
        )
        if pinned.returncode:
            raise ValueError("closure source is absent from the pinned current tree")
        data = pinned.stdout
        actual_lines = data.decode("utf-8").splitlines()
        actual_body = "\n".join(actual_lines[start - 1:end])
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"closure source could not be inspected: {exc}") from exc
    if end > len(actual_lines) or actual_body != body:
        raise ValueError("closure source body does not match the current tree")
    source = {
        "path": path, "start_line": start, "end_line": end, "body": actual_body,
        "body_sha256": hashlib.sha256(actual_body.encode("utf-8")).hexdigest(),
        "file_sha256": hashlib.sha256(data).hexdigest(),
        "unit_key": unit_key, "head_sha": head_sha,
    }
    return {
        "claim": str(evidence.get("claim") or "").strip(),
        "source": source,
        "verification": {
            "schema": CLOSURE_VERIFICATION_SCHEMA,
            "independent_tree_inspection": True,
            "unit_key": unit_key, "head_sha": head_sha,
        },
    }


def _actual_test(test, kind):
    if not isinstance(test, dict):
        return None
    name = str(test.get("name") or "")
    state = str(test.get("state") or "").strip().lower()
    fingerprint = str(test.get("evidence_fingerprint") or "")
    if (kind not in _CLOSURE_TEST_KINDS or not name or state not in _SUCCESSFUL_TEST_STATES
            or not fingerprint or "output" not in test):
        return None
    return {
        "name": name, "kind": kind, "state": state,
        "output": str(test.get("output") or ""),
        "evidence_fingerprint": fingerprint,
    }


def _executed_tests(execution, unit_key, head_sha):
    """Read only supervisor execution products, never worker revalidation claims."""
    if execution is None:
        return []
    tests = []
    required = execution.required or {}
    if str(required.get("commit") or "") == head_sha:
        tests.extend(
            actual for suite in required.get("suites") or ()
            if (actual := _actual_test(suite, "required")) is not None
        )
    dynamic = execution.dynamic or {}
    revisions = dynamic.get("revisions") or {}
    if (dynamic.get("schema") == "final-review-dynamic-result/v1"
            and dynamic.get("unit_key") == unit_key
            and revisions.get("head_sha") == head_sha):
        for test in dynamic.get("tests") or ():
            kind = str(test.get("kind") or "") if isinstance(test, dict) else ""
            actual = _actual_test(test, kind)
            if actual is not None:
                tests.append(actual)
    return tests


def _test_proof(claim, store, unit_id, execution, context):
    """Match an exact claim to this run's executed and already-persisted test."""
    unit_key, head_sha = _closure_coordinates(context)
    if not isinstance(claim, dict):
        raise ValueError("positive closure requires exact revalidation evidence")
    if claim.get("unit_key") != unit_key or claim.get("head_sha") != head_sha:
        raise ValueError("revalidation unit or SHA does not match the current unit")
    name = str(claim.get("test") or "")
    expected = {
        "name": name, "state": str(claim.get("state") or "").strip().lower(),
        "output": claim.get("output"),
        "evidence_fingerprint": claim.get("evidence_fingerprint"),
    }
    if not name or "output" not in claim or not claim.get("evidence_fingerprint"):
        raise ValueError("revalidation requires exact test name, state, output, and fingerprint")
    executed = [
        test for test in _executed_tests(execution, unit_key, head_sha)
        if all(test[field] == expected[field] for field in expected)
    ]
    if claim.get("kind") is not None:
        executed = [test for test in executed if test["kind"] == claim["kind"]]
    if len(executed) != 1:
        raise ValueError("revalidation does not uniquely match an independently executed test")
    actual = executed[0]
    persisted = [
        test for test in store.test_evidence_for_unit(unit_id)
        if all(test[field] == actual[field] for field in actual)
    ]
    if len(persisted) != 1:
        raise ValueError("revalidation test was not recorded before closure finalization")
    return {
        "test": actual["name"], "test_id": persisted[0]["test_id"],
        "kind": actual["kind"], "state": actual["state"], "status": "passed",
        "output": actual["output"],
        "evidence_fingerprint": actual["evidence_fingerprint"],
        "unit_key": unit_key, "head_sha": head_sha,
        "verification": {
            "schema": CLOSURE_VERIFICATION_SCHEMA,
            "independently_executed": True,
            "unit_key": unit_key, "head_sha": head_sha,
        },
    }


def _closure_anchors(prior_finding, source):
    original = next((anchor for anchor in prior_finding.get("anchors") or ()
                     if anchor.get("anchor_type") == "content"
                     and anchor.get("anchor") == prior_finding["content_anchor"]), None)
    if original is None:
        original = {
            "anchor_type": "content", "anchor": prior_finding["content_anchor"],
            "evidence_fingerprint": "",
            "details": {"path": prior_finding.get("path"),
                        "line": prior_finding.get("line")},
        }
    return [copy.deepcopy(original), current_source_anchor(source)]


def process_finding_closures(store, run_id, unit_id, prior, closures, *,
                             verification_context=None, execution=None):
    """Finalize claims only after independent tree/test corroboration.

    Worker anchors, status fields, reviewer confidence, and fingerprints are
    claims only. The supervisor reads the live materialization and matches a
    persisted test from this same unit/run before appending an immutable event.
    """
    prior_by_id = {
        str(finding.get("finding_id")): finding
        for finding in (prior or {}).get("findings", [])
    }
    outcomes = []
    for supplied in closures or ():
        closure = copy.deepcopy(supplied)
        reference = {}
        try:
            if not isinstance(closure, dict):
                raise ValueError("finding closure must be an object")
            reference = {
                "finding_id": closure.get("finding_id") or closure.get("prior_finding_id"),
                "content_anchor": closure.get("content_anchor")
                or closure.get("prior_content_anchor"),
            }
            prior_finding = _closure_reference(closure, prior_by_id)
            thread_id = prior_finding["thread_id"]
            closure_type = str(closure.get("closure_type") or "").strip().lower()
            if closure_type == "resolved":
                resolution = _source_proof(
                    closure.get("resolution_evidence"), prior_finding, verification_context)
                revalidation = _test_proof(
                    closure.get("revalidation"), store, unit_id, execution,
                    verification_context)
                history = resolve_finding(
                    store, run_id, thread_id,
                    anchors=_closure_anchors(prior_finding, resolution["source"]),
                    resolution_evidence=resolution, revalidation=revalidation,
                )
            elif closure_type == "falsified":
                supplied_evidence = closure.get("evidence")
                if not _recorded_evidence(supplied_evidence):
                    raise ValueError("finding falsification requires recorded evidence")
                evidence = {"claim": str(supplied_evidence.get("claim") or "")}
                if supplied_evidence.get("source") is not None:
                    evidence = _source_proof(
                        supplied_evidence, prior_finding, verification_context)
                if supplied_evidence.get("revalidation") is not None:
                    evidence["revalidation"] = _test_proof(
                        supplied_evidence["revalidation"], store, unit_id, execution,
                        verification_context)
                if "source" not in evidence and "revalidation" not in evidence:
                    raise ValueError(
                        "finding falsification requires verified tree or executed-test evidence")
                history = falsify_finding(
                    store, run_id, thread_id, closure.get("rationale"), evidence=evidence)
            else:
                raise ValueError("finding closure must be resolved or falsified")
            outcomes.append({
                "state": "recorded", "event_type": closure_type,
                "thread_id": thread_id, "finding_id": prior_finding["finding_id"],
                "content_anchor": prior_finding["content_anchor"],
                "lens": closure.get("lens"), "history": history,
            })
        except Exception as exc:
            store.record_warning(
                unit_id, "finding-closure-rejected", str(exc), "finding-memory",
                {**reference, "lens": closure.get("lens") if isinstance(closure, dict) else None},
            )
            outcomes.append({
                "state": "rejected", "reason": str(exc), **reference,
                "lens": closure.get("lens") if isinstance(closure, dict) else None,
            })
    return outcomes


def local_waiver_context(findings, local_waivers=()):
    """Describe local waiver matches without suppressing or editing findings."""
    context = []
    for supplied in local_waivers or ():
        if not isinstance(supplied, dict):
            supplied = {"note": str(supplied)}
        waiver = copy.deepcopy(supplied)
        matched = []
        for finding in findings:
            identifiers = {
                str(finding.get("finding_id") or ""), str(finding.get("id") or ""),
                str(finding.get("finding_key") or ""),
                str(finding.get("content_anchor") or ""),
            }
            anchors = finding.get("anchors") or ()
            identifiers.update(str(anchor.get("anchor") or "") for anchor in anchors)
            supplied_ids = {
                str(waiver.get(key) or "")
                for key in ("finding_id", "id", "finding_key", "anchor", "content_anchor")
            }
            if (identifiers & supplied_ids) - {""}:
                matched.append(finding["finding_id"])
        waiver.update({
            "scope": "machine-local",
            "official_suppression": False,
            "matched_finding_ids": matched,
        })
        context.append(waiver)
    return context
