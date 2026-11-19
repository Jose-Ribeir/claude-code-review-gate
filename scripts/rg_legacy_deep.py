"""Bounded deterministic decomposition for legacy native deep reviews.

The native executor remains the only session launcher and the global native
scheduler remains the only dispatcher. This module supplies legacy manifests,
run-scoped durable bookkeeping, and deterministic evidence validation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import time

from rg_native_executor import KimchiNativeHost, NativeExecutor, NativeTask
from rg_native_scheduler import NativeScheduler, SchedulerTask
from rg_run_lifecycle import RunDeadline


DEEP_FILE_TASK_LIMIT = 4
DEEP_JOURNAL_SCHEMA = "legacy-deep-native-journal/v1"
FILE_DUTIES = (
    "changed-file-contracts",
    "changed-call-chains",
)
GLOBAL_DUTIES = (
    "cross-file-call-chains",
    "cross-file-api-contracts",
    "global-invariants",
)


class DeepExecutionError(RuntimeError):
    def __init__(self, message, *, is_timeout=False):
        super().__init__(message)
        self.is_timeout = is_timeout


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class LegacyDeepStore:
    """The scheduler store contract persisted beside legacy async state.

    No final-review unit or run row is invented. Identities, events, receipts,
    and accepted artifacts are immutable and idempotent just like FinalStore's
    native methods, while legacy verdict state remains in its existing file.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._state = self._read()

    def _read(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema": DEEP_JOURNAL_SCHEMA, "native_tasks": []}
        if not isinstance(value, dict) or value.get("schema") != DEEP_JOURNAL_SCHEMA \
                or not isinstance(value.get("native_tasks"), list):
            raise DeepExecutionError("legacy deep native journal has an invalid schema")
        return value

    def _write(self):
        _atomic_json(self.path, self._state)

    def _find(self, identity):
        return next((task for task in self._state["native_tasks"]
                     if task["identity"]["run_id"] == identity["run_id"]
                     and task["identity"]["task_id"] == identity["task_id"]), None)

    def register_native_task(self, task):
        identity = task["identity"]
        with self._lock:
            existing = self._find(identity)
            if existing is not None:
                if existing["identity"] != identity or existing["transport"] != task["transport"]:
                    raise DeepExecutionError("registered legacy deep task identity is immutable")
                return existing
            existing = {
                "schema": "native-review-task/v1",
                "identity": identity,
                "transport": task["transport"],
                "events": [],
                "artifacts": [],
                "artifact_messages": [],
            }
            self._state["native_tasks"].append(existing)
            self._write()
            return existing

    def record_native_event(self, event):
        identity = event["identity"]
        with self._lock:
            task = self._find(identity)
            if task is None or task["identity"] != identity \
                    or task["transport"] != event["transport"]:
                raise DeepExecutionError("legacy deep event does not match its registered task")
            existing = next((old for old in task["events"]
                             if old["event_id"] == event["event_id"]), None)
            if existing is not None:
                if existing != event:
                    raise DeepExecutionError("legacy deep event identity has conflicting content")
                return existing
            task["events"].append(event)
            self._write()
            return event

    def commit_native_artifact(self, identity, message_id, *, artifact, received_at_unix):
        with self._lock:
            task = self._find(identity)
            if task is None or task["identity"] != identity \
                    or artifact["identity"] != identity \
                    or task["transport"] != artifact["transport"]:
                raise DeepExecutionError("legacy deep artifact identity does not match its task")
            existing = next((receipt for receipt in task["artifact_messages"]
                             if receipt["message_id"] == message_id), None)
            artifact_sha256 = _digest(artifact)
            if existing is not None:
                if existing["artifact_sha256"] != artifact_sha256:
                    raise DeepExecutionError("legacy deep artifact receipt has conflicting content")
                return existing
            if task["artifacts"]:
                raise DeepExecutionError("legacy deep task already has an accepted artifact")
            committed = {
                "message_id": message_id,
                "artifact_sha256": artifact_sha256,
                "artifact": artifact,
                "recorded_at": received_at_unix,
            }
            receipt = {
                "message_id": message_id,
                "accepted": True,
                "artifact_sha256": artifact_sha256,
                "recorded_at": received_at_unix,
            }
            event = {
                "schema": "native-review-event/v1",
                "event_id": "artifact-commit:" + message_id,
                "event_type": "completed",
                "identity": identity,
                "transport": artifact["transport"],
                "native_session_id": artifact["native_session_id"],
                "timing": artifact["timing"],
                "details": {
                    "artifact_sha256": artifact_sha256,
                    "message_id": message_id,
                },
            }
            task["artifacts"].append(committed)
            task["artifact_messages"].append(receipt)
            task["events"].append(event)
            self._write()
            return receipt


def _manifest_document(parent, paths, *, kind, task_id, duties, all_paths):
    remaining = [path for path in all_paths if path not in set(paths)]
    document = dict(parent)
    document["paths"] = list(paths)
    document["files"] = [
        file for file in parent.get("files", []) if file.get("path") in set(paths)
    ]
    document["renames"] = [
        rename for rename in parent.get("renames", []) if rename[-1] in set(paths)
    ]
    document["other_changed"] = remaining
    document["carried"] = []
    document["native_deep_task"] = {
        "task_id": task_id,
        "kind": kind,
        "duties": list(duties),
        "context_files": remaining,
    }
    document["manifest_sha256"] = _digest(document)
    return document


def build_deep_manifests(parent, directory, run_id, *, file_limit=DEEP_FILE_TASK_LIMIT):
    """Return deterministic bounded file/subsystem manifests plus global duties."""
    paths = list(parent.get("paths") or [])
    if len(paths) != len(set(paths)):
        raise DeepExecutionError("legacy deep input repeats a changed file")
    ordered = sorted(paths)
    groups = {}
    for path in ordered:
        subsystem = path.split("/", 1)[0] if "/" in path else "<root>"
        groups.setdefault(subsystem, []).append(path)
    assignments = []
    for subsystem in sorted(groups):
        grouped = groups[subsystem]
        for offset in range(0, len(grouped), file_limit):
            chunk = grouped[offset:offset + file_limit]
            assignments.append(("subsystem", subsystem, chunk))
    assignments.append(("global", "repository", []))
    manifests = []
    for kind, subsystem, chunk in assignments:
        identity_part = {"kind": kind, "subsystem": subsystem, "paths": chunk}
        task_id = f"{run_id}-deep-{kind}-{_digest(identity_part)[:16]}"
        duties = GLOBAL_DUTIES if kind == "global" else FILE_DUTIES + tuple(
            "file:" + path for path in chunk
        )
        manifest = _manifest_document(
            parent, chunk, kind=kind, task_id=task_id, duties=duties,
            all_paths=ordered,
        )
        path = Path(directory) / f"manifest-{task_id}.json"
        _atomic_json(path, manifest)
        manifests.append((manifest, path))
    return manifests


def _task_prompt(base_prompt, parent_path, manifest, manifest_path):
    prompt = base_prompt.replace(
        str(parent_path).replace("\\", "/"),
        str(manifest_path).replace("\\", "/"),
    )
    evidence = {
        "task_id": manifest["native_deep_task"]["task_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "reviewed_paths": manifest["paths"],
        "completed_duties": manifest["native_deep_task"]["duties"],
    }
    contract = (
        "\n\nNATIVE DEEP TASK CONTRACT — Review every assigned path and complete every "
        "assigned duty. `other_changed` is context for call-chain and integration checks; "
        "do not silently treat it as out of scope. Your otherwise unchanged legacy verdict "
        "JSON must additionally contain exactly one `native_deep_evidence` object with keys "
        "task_id, input_sha256, manifest_sha256, reviewed_paths, completed_duties. Copy these "
        "values exactly (input_sha256 is supplied below). Omitted or repeated coverage is "
        "rejected by the trusted supervisor.\n" + _canonical(evidence)
    )
    input_body = prompt + contract
    input_sha256 = hashlib.sha256(input_body.encode("utf-8")).hexdigest()
    return input_body + f'\n\n"input_sha256": "{input_sha256}"\n', input_sha256


def _validate_completion(definition, result, manifest):
    if result.state != "completed" or not result.eligible or result.artifact is None:
        raise DeepExecutionError(
            f"legacy deep task {definition.task.task_id} did not complete: "
            f"{result.error or result.state}"
        )
    if result.artifact.task != definition.task:
        raise DeepExecutionError("legacy deep artifact has a mismatched immutable input identity")
    evidence = result.artifact.payload.get("native_deep_evidence")
    expected = {
        "task_id": definition.task.task_id,
        "input_sha256": definition.task.input_sha256,
        "manifest_sha256": manifest["manifest_sha256"],
        "reviewed_paths": manifest["paths"],
        "completed_duties": manifest["native_deep_task"]["duties"],
    }
    if evidence != expected:
        raise DeepExecutionError(
            f"legacy deep task {definition.task.task_id} omitted, repeated, or misidentified evidence"
        )


def execute_deep_review(base_prompt, parent_manifest, parent_manifest_path, *, kimi,
                        repo_root, run_id, async_dir, head_sha, push_range, timeout,
                        model, child_env, started_at, result_parser, merge_results,
                        verdict, extensions, progress=None, on_start=None,
                        on_completion=None, on_expire=None):
    """Run bounded direct native tasks and join only their validated evidence."""
    parent_path = Path(parent_manifest_path)
    async_dir = Path(async_dir)
    journal_path = async_dir / f"native-{run_id}-deep-tasks.json"
    manifests = build_deep_manifests(parent_manifest, async_dir, run_id)
    store = LegacyDeepStore(journal_path)
    host = KimchiNativeHost.checked(kimi, environ=child_env, progress=progress)
    owner = {}
    finalized_event = threading.Event()

    def expired(details):
        if on_expire is not None:
            on_expire(details)
        owner["scheduler"].expire_run(run_id)

    deadline = RunDeadline(run_id, timeout, on_expire=expired)
    executor = NativeExecutor(
        host, deadline=deadline, store=store, result_parser=result_parser,
        diagnostic_environ=child_env,
    )
    scheduler = NativeScheduler(
        executor, on_start=on_start, on_completion=on_completion,
        on_finalized=lambda finalized_run: (
            finalized_event.set() if finalized_run == run_id else None
        ),
    )
    owner["scheduler"] = scheduler
    definitions = []
    for manifest, manifest_path in manifests:
            kind = manifest["native_deep_task"]["kind"]
            task_id = manifest["native_deep_task"]["task_id"]
            prompt, input_sha256 = _task_prompt(
                base_prompt, parent_path, manifest, manifest_path,
            )
            task = NativeTask(
                run_id=run_id,
                task_id=task_id,
                session_name="legacy-" + task_id,
                unit_id="legacy-deep:" + task_id,
                stage="legacy-deep",
                lens="deep-global" if kind == "global" else "deep-files",
                revision_tuple=(head_sha, push_range),
                input_sha256=input_sha256,
                backend="kimchi",
                model=model,
                deadline_at_unix=started_at + timeout,
                prompt=prompt,
                repo_root=Path(repo_root),
                artifact_path=async_dir / f"native-{task_id}.json",
                extensions=extensions,
            )
            if task.artifact_path.exists():
                raise DeepExecutionError(
                    f"legacy deep task {task_id} has an already committed artifact; "
                    "refusing to rewrite immutable evidence"
                )
            definition = SchedulerTask(task)
            definitions.append((definition, manifest))
    deadline.start()
    try:
        for definition, _manifest in definitions:
            scheduler.submit(definition)
        wait_deadline = time.monotonic() + timeout + 20
        while not scheduler.close_run(run_id):
            finalized_event.clear()
            if scheduler.close_run(run_id):
                break
            remaining = wait_deadline - time.monotonic()
            if remaining <= 0:
                scheduler.cancel_run(run_id, "run-deadline-exceeded")
                raise DeepExecutionError(
                    f"legacy deep tasks did not finish cleanup within {timeout}s",
                    is_timeout=True,
                )
            finalized_event.wait(remaining)
        snapshot = scheduler.snapshot(run_id)
        if snapshot["cancelled"] is not None:
            raise DeepExecutionError(
                "legacy deep native run was cancelled: " + snapshot["cancelled"],
                is_timeout=snapshot["cancelled"] == "run-deadline-exceeded",
            )
        validated = []
        for definition, manifest in definitions:
            result = snapshot["tasks"][definition.task.task_id]
            _validate_completion(definition, result, manifest)
            validated.append(result.artifact.payload)
        file_results = [
            (definition, manifest) for definition, manifest in definitions
            if manifest["native_deep_task"]["kind"] != "global"
        ]
        covered = [
            path for _definition, manifest in file_results
            for path in manifest["paths"]
        ]
        if len(covered) != len(set(covered)) or sorted(covered) != sorted(parent_manifest["paths"]):
            raise DeepExecutionError("legacy deep task manifests omit or repeat changed files")
        result = merge_results(validated)
        result["verdict"] = verdict(result)
        raw = _canonical([
            {
                "task_id": definition.task.task_id,
                "artifact": snapshot["tasks"][definition.task.task_id].artifact.as_dict(),
            }
            for definition, _manifest in definitions
        ])
        return result, raw
    finally:
        if not scheduler.snapshot(run_id)["finalized"]:
            scheduler.cancel_run(run_id, "legacy-deep-supervisor-exit")
        deadline.close()
