"""Read-only SPEC-16 operator views generated from the final-review SQLite store.

The contract and acceptance registry in SQLite are canonical. This module only
selects and renders those records; it does not maintain a second document or
change any review, acceptance, or publication result.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from rg_final_store import FinalStore, StoreError


COMMANDS = frozenset(("contract", "acceptance", "final-report"))
VIEW_ERROR_PREFIX = "review-gate views"


class ViewError(Exception):
    """A requested view has no trustworthy, non-empty selection."""


def _json_text(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def _identity(store):
    identity = store.store_identity()
    return {
        "store": identity,
        "store_instance_id": identity["store_instance_id"],
    }


def _selection(version):
    return "historical" if version is not None else "current"


def _contract(args, store):
    document = store.contract_view(args.version)
    if not document:
        selected = args.version if args.version is not None else "current"
        raise ViewError(f"contract version {selected!r} is not installed")
    payload = {
        "schema": "final-review-contract-view/v1",
        **_identity(store),
        "selection": _selection(args.version),
        "contract": document,
    }
    if args.json:
        sys.stdout.write(_json_text(payload))
        return 0
    lines = [
        "# Final review contract",
        "",
        f"Store: {store.store_instance_id}",
        f"Version: {document['version']}",
        f"Implementation: {document['implementation_version']}",
        f"Selection: {payload['selection']}",
        "",
        "## Rules",
        "",
    ]
    for rule in document["rules"]:
        lines.extend((
            f"### {rule['rule_key']} — {rule['title']}",
            "",
            rule["body"],
            "",
        ))
    lines.extend(("## Decision log", ""))
    for decision in document["decision_log"]:
        lines.extend((
            f"### {decision['decision_key']} — {decision['title']}",
            "",
            decision["body"],
            "",
        ))
    sys.stdout.write("\n".join(lines).rstrip() + "\n")
    return 0


def _scenario_lines(scenario):
    lines = [
        f"### {scenario['scenario_id']}",
        "",
        f"Version: {scenario['version']}",
        f"Title: {scenario['title']}",
        f"Behavior: {scenario['behavior']}",
        f"Expected state: {scenario['expected_state']}",
        f"Dispatch key: {scenario['dispatch_key']}",
        "Required evidence:",
    ]
    lines.extend(f"- {key}" for key in scenario["required_evidence"])
    for key, heading in (
        ("cleanup_assertions", "Cleanup assertions:"),
        ("warning_assertions", "Warning assertions:"),
        ("trust_assertions", "Trust assertions:"),
    ):
        lines.append(heading)
        lines.extend(f"- {item}" for item in scenario[key])
    lines.append("")
    return lines


def _acceptance_list(args, store):
    registry = store.acceptance_view(args.version)
    if not registry:
        selected = args.version if args.version is not None else "current"
        raise ViewError(f"acceptance version {selected!r} is not installed")
    payload = {
        "schema": "final-review-acceptance-list/v1",
        **_identity(store),
        "selection": _selection(args.version),
        "acceptance_version": registry["version"],
        "implementation_version": registry["implementation_version"],
        "scenarios": registry["scenarios"],
    }
    if args.json:
        sys.stdout.write(_json_text(payload))
        return 0
    lines = [
        "# Final review acceptance registry",
        "",
        f"Store: {store.store_instance_id}",
        f"Acceptance version: {registry['version']}",
        f"Implementation: {registry['implementation_version']}",
        f"Selection: {payload['selection']}",
        f"Scenarios: {len(registry['scenarios'])}",
        "",
    ]
    for scenario in registry["scenarios"]:
        lines.extend(_scenario_lines(scenario))
    sys.stdout.write("\n".join(lines).rstrip() + "\n")
    return 0


def _acceptance_filters(args):
    return {
        "scenario_id": getattr(args, "scenario_id", None),
        "version": getattr(args, "version", None),
        "review_run_id": getattr(args, "review_run_id", None),
    }


def _acceptance_history(store, filters):
    return store.acceptance_history(
        scenario_id=filters["scenario_id"],
        version=filters["version"],
        review_run_id=filters["review_run_id"],
    )


def _no_acceptance_attempts(filters):
    selected = ", ".join(
        f"{key}={value}" for key, value in filters.items() if value is not None
    ) or "unfiltered history"
    return ViewError(f"no acceptance attempts found for {selected}")


def _acceptance_status(args, store):
    if bool(args.scenario_id) == bool(args.acceptance_run_id):
        raise ViewError(
            "acceptance status requires exactly one scenario_id or --acceptance-run-id"
        )
    if args.acceptance_run_id:
        attempt = store.acceptance_run(args.acceptance_run_id)
        filters = {"acceptance_run_id": args.acceptance_run_id}
        if args.version is not None and attempt["version"] != args.version:
            raise ViewError(
                f"acceptance run {args.acceptance_run_id} is not at version {args.version}"
            )
        if args.review_run_id is not None and attempt["review_run_id"] != args.review_run_id:
            raise ViewError(
                f"acceptance run {args.acceptance_run_id} does not belong to review run "
                f"{args.review_run_id}"
            )
    else:
        filters = _acceptance_filters(args)
        attempts = _acceptance_history(store, filters)
        if not attempts:
            raise _no_acceptance_attempts(filters)
        attempt = attempts[-1]
    payload = {
        "schema": "final-review-acceptance-status/v1",
        **_identity(store),
        "filters": filters,
        "acceptance_version": attempt["version"],
        "acceptance_run": attempt,
    }
    if args.json:
        sys.stdout.write(_json_text(payload))
        return 0
    sys.stdout.write(_acceptance_attempt_text(store, attempt, heading="Acceptance status"))
    return 0


def _acceptance_attempt_text(store, attempt, heading="Acceptance attempt"):
    result = attempt["result"]
    lines = [
        f"# {heading}",
        "",
        f"Store: {store.store_instance_id}",
        f"Acceptance run: {attempt['acceptance_run_id']}",
        f"Scenario: {attempt['scenario_id']}",
        f"Acceptance version: {attempt['version']}",
        f"Review run: {attempt['review_run_id'] or 'not associated'}",
        f"State: {attempt['state']}",
        f"Created: {attempt['created_at']}",
        f"Completed: {attempt['completed_at'] or 'not completed'}",
        f"Error: {attempt['error'] or 'none'}",
        "",
        "## Expected scenario",
        "",
        f"Title: {attempt['scenario']['title']}",
        f"Behavior: {attempt['scenario']['behavior']}",
        f"Expected state: {attempt['scenario']['expected_state']}",
        "Expected evidence:",
    ]
    lines.extend(f"- {key}" for key in attempt["scenario"]["required_evidence"])
    lines.extend(("", "## Result", ""))
    if result is None:
        lines.append("No result recorded.")
    else:
        lines.extend((
            f"Result: {result['state']}",
            f"Result ID: {result['result_id']}",
            f"Recorded: {result['recorded_at']}",
            f"Result error: {result['error'] or 'none'}",
            "Result evidence:",
            _json_text(result["evidence"]).rstrip(),
            "Result details:",
            _json_text(result["details"]).rstrip(),
        ))
    return "\n".join(lines) + "\n"


def _acceptance_history_command(args, store):
    filters = _acceptance_filters(args)
    attempts = _acceptance_history(store, filters)
    if not attempts:
        raise _no_acceptance_attempts(filters)
    payload = {
        "schema": "final-review-acceptance-history/v1",
        **_identity(store),
        "filters": filters,
        "acceptance_runs": attempts,
    }
    if args.json:
        sys.stdout.write(_json_text(payload))
        return 0
    chunks = []
    for attempt in attempts:
        chunks.append(_acceptance_attempt_text(store, attempt).rstrip())
    sys.stdout.write("\n\n".join(chunks) + "\n")
    return 0


def _run_summary(manifest):
    run = manifest["run"]
    return {
        "run_id": run["run_id"],
        "run_seq": run["run_seq"],
        "stack_key": run["stack_key"],
        "contract_version": run["contract_version"],
        "acceptance_version": run["acceptance_version"],
        "state": run["state"],
        "started_at": run["started_at"],
        "completed_at": run["completed_at"],
        "superseded_by_run_id": run["superseded_by_run_id"],
        "provider": manifest["stack"]["provider"],
        "target_project": manifest["stack"]["target_project"],
        "root_target_ref": manifest["stack"]["root_target_ref"],
        "revisions": manifest["revisions"],
        "units": [{
            "unit_id": unit["unit_id"],
            "unit_key": unit["unit_key"],
            "kind": unit["kind"],
            "item_position": unit["item_position"],
            "base_sha": unit["base_sha"],
            "head_sha": unit["head_sha"],
            "state": unit["state"],
        } for unit in manifest["units"]],
    }


def _report_list(args, store):
    runs = [_run_summary(manifest) for manifest in store.run_history(args.stack)]
    if not runs:
        scope = f" for stack {args.stack}" if args.stack is not None else ""
        raise ViewError(f"no final-review runs found{scope}")
    payload = {
        "schema": "final-review-run-list/v1",
        **_identity(store),
        "filters": {"stack_key": args.stack},
        "runs": runs,
    }
    if args.json:
        sys.stdout.write(_json_text(payload))
        return 0
    lines = [
        "# Final review runs",
        "",
        f"Store: {store.store_instance_id}",
        f"Runs: {len(runs)}",
        "",
    ]
    for run in runs:
        lines.extend((
            f"## Run {run['run_id']}",
            "",
            f"Sequence: {run['run_seq']}",
            f"State: {run['state']}",
            f"Stack: {run['stack_key']}",
            f"Contract: {run['contract_version']}",
            f"Acceptance: {run['acceptance_version'] or 'not recorded'}",
            f"Superseded by: {run['superseded_by_run_id'] or 'not superseded'}",
            f"Root target SHA: {run['revisions']['root_target_sha']}",
            "Pinned items:",
        ))
        for item in run["revisions"]["items"]:
            lines.append(
                f"- {item['position']}. {item['item_key']}: "
                f"{item['source_project']} {item['source_ref']}@{item['source_sha']} -> "
                f"{item['target_project']} {item['target_ref']}@{item['target_sha']}"
            )
        lines.append("Units:")
        for unit in run["units"]:
            lines.append(
                f"- {unit['unit_id']} {unit['unit_key']} ({unit['kind']}): "
                f"{unit['state']}; base={unit['base_sha']}; head={unit['head_sha']}"
            )
        lines.append("")
    sys.stdout.write("\n".join(lines).rstrip() + "\n")
    return 0


def _report_show(args, store):
    if args.json:
        sys.stdout.write(_json_text(store.machine_report(args.run_id, args.unit)))
    else:
        sys.stdout.write(store.human_report(args.run_id, args.unit))
    return 0


def _report_export(args, store):
    if args.format == "json":
        content = _json_text(store.machine_report(args.run_id, args.unit))
    else:
        content = store.human_report(args.run_id, args.unit)
    if args.output == "-":
        sys.stdout.write(content)
        return 0
    output = Path(args.output).expanduser()
    if output.absolute() == store.path:
        raise ViewError("export output must not overwrite the final-review store")
    output.write_text(content, encoding="utf-8")
    sys.stdout.write(
        f"Exported run {args.run_id} from store {store.store_instance_id} to {output}\n"
    )
    return 0


def _leaf(subparsers, name, help_text, handler, *, json_option=True):
    parser = subparsers.add_parser(name, help=help_text)
    parser.add_argument("--store", help="final-review SQLite path (default: external store root)")
    if json_option:
        parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.set_defaults(handler=handler)
    return parser


def build_parser():
    parser = argparse.ArgumentParser(
        prog="review-gate", description="Inspect versioned final-review SQLite records.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    contract_parser = commands.add_parser("contract", help="show the versioned review contract")
    contract_leaves = contract_parser.add_subparsers(dest="action", required=True)
    show_contract = _leaf(contract_leaves, "show", "show current or historical rules", _contract)
    show_contract.add_argument("--version", help="explicit historical contract version")

    acceptance_parser = commands.add_parser(
        "acceptance", help="inspect registry definitions, status, and retained results",
    )
    acceptance_leaves = acceptance_parser.add_subparsers(dest="action", required=True)
    list_acceptance = _leaf(
        acceptance_leaves, "list", "list scenarios and expected evidence", _acceptance_list,
    )
    list_acceptance.add_argument("--version", help="explicit historical acceptance version")
    for name, handler in (
        ("status", _acceptance_status), ("history", _acceptance_history_command),
    ):
        leaf = _leaf(acceptance_leaves, name, f"show acceptance {name}", handler)
        leaf.add_argument("scenario_id", nargs="?", help="stable scenario identifier")
        leaf.add_argument("--version", help="filter to an exact acceptance version")
        leaf.add_argument("--review-run-id", help="filter attempts by final-review run")
        if name == "status":
            leaf.add_argument("--acceptance-run-id", help="select one exact acceptance attempt")

    report_parser = commands.add_parser(
        "final-report", help="list, show, or export auditable run and unit reports",
    )
    report_leaves = report_parser.add_subparsers(dest="action", required=True)
    list_reports = _leaf(report_leaves, "list", "list runs and exact identities", _report_list)
    list_reports.add_argument("--stack", help="filter by exact stack key")
    show_report = _leaf(report_leaves, "show", "show a run or one unit", _report_show)
    show_report.add_argument("run_id", help="exact final-review run identifier")
    show_report.add_argument("--unit", help="exact unit identifier within the run")
    export_report = _leaf(
        report_leaves, "export", "export a run or one unit", _report_export,
        json_option=False,
    )
    export_report.add_argument("run_id", help="exact final-review run identifier")
    export_report.add_argument("--unit", help="exact unit identifier within the run")
    export_report.add_argument(
        "--format", choices=("json", "markdown"), default="json",
        help="export representation (default: json)",
    )
    export_report.add_argument(
        "--output", default="-", help="output file, or - for stdout (default: -)",
    )
    return parser


def main(argv=None):
    """Dispatch one standalone operator view and return its process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) > 1 and args[1] in COMMANDS and args[0] not in COMMANDS:
        args = args[1:]
    parser = build_parser()
    try:
        parsed = parser.parse_args(args)
        with FinalStore(parsed.store) as store:
            return parsed.handler(parsed, store)
    except (ViewError, StoreError, ValueError, OSError, sqlite3.Error) as exc:
        sys.stderr.write(f"{VIEW_ERROR_PREFIX}: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
