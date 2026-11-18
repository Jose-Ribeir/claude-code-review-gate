"""SPEC-16 final-finding continuity, revision freshness, and waiver isolation.

The functions in this module are the orchestrator-facing contract.  Finding
history is durably implemented by :class:`rg_final_store.FinalStore`; this
module deliberately accepts that public store seam rather than maintaining a
second memory.  Provider access is read-only and injected, so tests and later
orchestration can supply scripted revision providers.

A finding is never retired because a later reviewer omitted it.  Only an
explicit resolution with changed anchors and positive revalidation evidence,
or an explicit falsification with a rationale, closes its thread.  Existing
pre-push waiver ledgers are accepted only as detached operator context.
"""
from __future__ import annotations

import copy
import hashlib
import json

RERUN_CONTEXT_SCHEMA = "final-review-rerun-context/v1"
FRESHNESS_SCHEMA = "final-review-freshness/v1"
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


def process_finding_closures(store, run_id, unit_id, prior, closures):
    """Validate and persist explicit worker/provider closure claims.

    Only findings present in this unit's inherited ``rerun_context`` are
    eligible. Unknown IDs, current-run findings, anchor mismatches, and
    cross-unit requests become visible rejected claims and cannot erase any
    finding. Store APIs perform the final open-state, changed-evidence, and
    positive-revalidation checks before appending an immutable event.
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
                history = resolve_finding(
                    store, run_id, thread_id,
                    anchors=closure.get("anchors"),
                    resolution_evidence=closure.get("resolution_evidence"),
                    revalidation=closure.get("revalidation"),
                )
            elif closure_type == "falsified":
                evidence = closure.get("evidence")
                if not _recorded_evidence(evidence):
                    raise ValueError("finding falsification requires recorded evidence")
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
