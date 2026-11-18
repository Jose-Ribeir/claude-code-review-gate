"""Validated base-first graphs for explicit final whole-stack review."""
import re
import shlex

from rg_final_provider import (
    ProviderError,
    list_open_references,
    provider_name_for_references,
    revision_tuple,
    select_provider,
)


GRAPH_SCHEMA = "final-review-graph/v1"
COMMAND_PREFIX = ("review-gate.py", "final-review")


class GraphValidationError(ProviderError, ValueError):
    """An explicit reference list cannot be reviewed as one linear graph."""

    def __init__(self, message, *, code="invalid-graph", details=None):
        self.code = code
        self.details = details or {}
        super().__init__(message)


def normalize_base_most_references(references):
    """Normalize explicit references without adding, deduplicating, or sorting them."""
    if isinstance(references, str) or not isinstance(references, (list, tuple)):
        raise GraphValidationError("final-review references must be an explicit base-first list",
                                   code="invalid-reference-list")
    normalized = []
    for reference in references:
        if not isinstance(reference, str) or not reference.strip():
            raise GraphValidationError("final-review references must be non-empty strings",
                                       code="invalid-reference")
        reference = reference.strip()
        if "://" in reference:
            reference = reference.rstrip("/")
        normalized.append(reference)
    if not normalized:
        raise GraphValidationError("final-review requires at least one reference",
                                   code="empty-reference-list")
    return normalized


class DuplicateReferenceError(GraphValidationError):
    """Two explicit references resolve to the same provider item."""


class GraphOrderError(GraphValidationError):
    """The explicit base-first list disagrees with provider branch edges."""

    def __init__(self, expected, actual):
        self.expected = list(expected)
        self.actual = list(actual)
        super().__init__(
            f"explicit order {self.actual!r} is not provider base-first order {self.expected!r}",
            code="inconsistent-order",
            details={"expected": self.expected, "actual": self.actual},
        )


class GraphGapError(GraphValidationError):
    """Supplied items do not form one connected provider-edge stack."""


class GraphCycleError(GraphValidationError):
    """Source/target branches form a cycle instead of a rooted chain."""


class MultipleTargetError(GraphValidationError):
    """Items address more than one target project."""


class RootTargetError(GraphValidationError):
    """Disconnected items imply multiple independent root targets."""


class BranchPointError(GraphValidationError):
    """A parent or child has more than one possible linear continuation."""


class ProseDependencyError(GraphValidationError):
    """A description claims a dependency absent from provider branch edges."""


class GraphOmissionError(GraphValidationError):
    """Provider discovery found open ancestors or descendants not supplied."""

    def __init__(self, ordered_items, supplied_ids):
        supplied_positions = [
            index for index, item in enumerate(ordered_items) if item["id"] in supplied_ids
        ]
        last_supplied = max(supplied_positions)
        omitted = [item for item in ordered_items if item["id"] not in supplied_ids]
        self.omitted_items = omitted
        self.omitted_ancestors = [
            item["id"] for index, item in enumerate(ordered_items)
            if item["id"] not in supplied_ids and index < last_supplied
        ]
        self.omitted_descendants = [
            item["id"] for index, item in enumerate(ordered_items)
            if item["id"] not in supplied_ids and index > last_supplied
        ]
        command = _command([item["url"] for item in ordered_items])
        self.corrected_argv = tuple(command["argv"])
        self.corrected_command = command["text"]
        super().__init__(
            "open stack items were omitted; rerun the complete corrected command: "
            + self.corrected_command,
            code="omitted-items",
            details={
                "omitted_ancestors": self.omitted_ancestors,
                "omitted_descendants": self.omitted_descendants,
                "corrected_command": self.corrected_command,
                "corrected_argv": list(self.corrected_argv),
            },
        )


def _command(references):
    argv = [*COMMAND_PREFIX, *references]
    return {"argv": argv, "text": shlex.join(argv)}


def _all_open_items(supplied, open_references, provider, repo_root):
    items = {item["id"]: item for item in supplied}
    known_references = {item["url"] for item in supplied}
    for reference in open_references:
        if reference in known_references:
            continue
        item = provider.read(reference, repo_root=repo_root)
        items[item["id"]] = item
        known_references.add(reference)
    return list(items.values())


_PROSE_DEPENDENCY = re.compile(
    r"\b(?:depends?\s+on|blocked\s+by|stacked\s+on)\b", re.IGNORECASE)


def _linear_component(supplied, items):
    by_id = {item["id"]: item for item in items}
    by_source = {}
    by_target = {}
    for item in items:
        by_source.setdefault(item["source"]["branch"], []).append(item)
        by_target.setdefault(item["target"]["branch"], []).append(item)

    first = supplied[0]
    visited = {first["id"]}
    queue = [first]
    while queue:
        item = queue.pop(0)
        parents = by_source.get(item["target"]["branch"], [])
        children = by_target.get(item["source"]["branch"], [])
        if len(parents) > 1 or len(children) > 1:
            raise BranchPointError(
                f"{item['id']} has multiple provider parents or children; linear order is ambiguous",
                code="branch-point", details={"item_key": item["id"]},
            )
        for related in [*parents, *children]:
            if related["id"] not in visited:
                visited.add(related["id"])
                queue.append(related)

    unreachable = [item for item in supplied[1:] if item["id"] not in visited]
    if unreachable:
        if any(_PROSE_DEPENDENCY.search(item.get("description") or "")
               for item in unreachable):
            raise ProseDependencyError(
                "a prose-only dependency cannot replace provider source/target edges; "
                "fix the merge-request targets and rerun",
                code="prose-only-dependency",
                details={"item_keys": [item["id"] for item in unreachable]},
            )
        component = [by_id[key] for key in visited]
        root_branch = component[0]["target"]["branch"]
        independent_roots = {"main", "master", "release", "develop", root_branch}
        if any(item["target"]["branch"] in independent_roots for item in unreachable):
            raise RootTargetError(
                "supplied items have disconnected root targets; one linear stack is required",
                code="multiple-root-targets",
                details={"disconnected_items": [item["id"] for item in unreachable]},
            )
        raise GraphGapError(
            "supplied items are disconnected by a gap in provider source/target edges",
            code="graph-gap", details={"disconnected_items": [item["id"] for item in unreachable]},
        )

    roots = [by_id[key] for key in visited
             if not by_source.get(by_id[key]["target"]["branch"])]
    if not roots:
        raise GraphCycleError("provider source/target branches contain no root and form a cycle",
                              code="cycle")
    if len(roots) != 1:
        raise RootTargetError("provider graph has multiple root targets",
                              code="multiple-root-targets",
                              details={"roots": [item["id"] for item in roots]})

    ordered = []
    current = roots[0]
    ordered_ids = set()
    while True:
        if current["id"] in ordered_ids:
            raise GraphCycleError(f"provider cycle reaches {current['id']} twice", code="cycle")
        ordered.append(current)
        ordered_ids.add(current["id"])
        children = by_target.get(current["source"]["branch"], [])
        if not children:
            break
        current = children[0]
    if len(ordered) != len(visited):
        raise GraphCycleError("provider source/target branches contain a cycle", code="cycle")
    return ordered


def record_graph_observation(telemetry, references, *, graph=None, error=None):
    """Fail-soft graph counters; provider command/description text is excluded."""
    if telemetry is None:
        return
    try:
        supplied_count = (len(references)
                          if not isinstance(references, str) and references is not None else 1)
        provider = (graph or {}).get("provider") or provider_name_for_references(references)
        if error is None:
            telemetry.graph_discovered(
                provider=provider, status="validated", supplied_count=supplied_count,
                discovered_count=len((graph or {}).get("items") or ()),
            )
            return
        telemetry.graph_discovered(
            provider=provider, status="rejected", supplied_count=supplied_count,
            discovered_count=len(getattr(error, "actual", ()) or ()),
            omitted_ancestor_count=len(getattr(error, "omitted_ancestors", ()) or ()),
            omitted_descendant_count=len(getattr(error, "omitted_descendants", ()) or ()),
            error_code=str(getattr(error, "code", "graph-discovery-failed")),
        )
    except Exception:
        try:
            telemetry.record("observability-internal-warning", {})
        except Exception:
            pass


def discover_linear_graph(references, *, provider=None, repo_root=None, telemetry=None):
    """Return provider-pinned items, recording only bounded graph counters."""
    try:
        graph = _discover_linear_graph(references, provider=provider, repo_root=repo_root)
    except Exception as exc:
        record_graph_observation(telemetry, references, error=exc)
        raise
    record_graph_observation(telemetry, references, graph=graph)
    return graph


def _discover_linear_graph(references, *, provider=None, repo_root=None):
    """Return provider-pinned items in their explicit base-most-first order."""
    provider = select_provider(references, provider=provider, repo_root=repo_root)
    references = normalize_base_most_references(references)
    supplied = []
    supplied_ids = set()
    for reference in references:
        item = provider.read(reference, repo_root=repo_root)
        if item["id"] in supplied_ids:
            raise DuplicateReferenceError(
                f"duplicate final-review reference resolves to {item['id']}",
                code="duplicate-reference", details={"item_key": item["id"]},
            )
        supplied.append(item)
        supplied_ids.add(item["id"])
    not_open = [item["id"] for item in supplied if item.get("state") != "opened"]
    if not_open:
        raise GraphValidationError(
            "final-review graph discovery accepts only open items",
            code="item-not-open", details={"item_keys": not_open},
        )
    target_projects = {(item["target"]["project_id"], item["target"]["project_path"])
                       for item in supplied}
    if len(target_projects) != 1:
        raise MultipleTargetError(
            "one final-review invocation must have exactly one target project",
            code="multiple-target-projects",
            details={"target_projects": sorted(target_projects)},
        )
    open_references = list_open_references(provider, supplied[0], repo_root=repo_root)
    open_items = _all_open_items(supplied, open_references, provider, repo_root)
    discovered_projects = {(item["target"]["project_id"], item["target"]["project_path"])
                           for item in open_items}
    if len(discovered_projects) != 1:
        raise MultipleTargetError(
            "open-item discovery returned multiple target projects",
            code="multiple-target-projects",
            details={"target_projects": sorted(discovered_projects)},
        )
    items = _linear_component(supplied, open_items)
    if any(item["id"] not in supplied_ids for item in items):
        raise GraphOmissionError(items, supplied_ids)
    expected = [item["id"] for item in items]
    actual = [item["id"] for item in supplied]
    if actual != expected:
        raise GraphOrderError(expected, actual)
    canonical = [item["url"] for item in items]
    return {
        "schema": GRAPH_SCHEMA,
        "provider": items[0]["provider"],
        "target_project": {
            "project_id": items[0]["target"]["project_id"],
            "project_path": items[0]["target"]["project_path"],
        },
        "root_target": items[0]["target"],
        "items": items,
        "item_keys": [item["id"] for item in items],
        "references": canonical,
        "revisions": [
            {"item_key": item["id"], "revision_tuple": list(revision_tuple(item))}
            for item in items
        ],
        "command": _command(canonical),
    }
