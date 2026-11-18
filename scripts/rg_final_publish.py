"""Trusted, least-privilege GitLab/GitHub commit statuses for final review.

This module is the only final-review provider-write seam. Review workers,
test executors, and untrusted callers must not instantiate an authorized
publisher. ``trusted_orchestrator=True`` is an explicit supervisor capability;
without it the public API records local-only evidence and never invokes its
transport. Production uses the existing ``run_cli`` runner, while tests inject
scripted ``(argv, cwd, timeout) -> (stdout, note)`` doubles.

Each invocation makes at most one canonical commit-status POST for one
source/fork commit. No token is read or copied by this module. The caller must
provision only the provider permission needed to create commit statuses on
participating projects. Provider failures degrade trust and leave all durable
review evidence available locally.
"""
from __future__ import annotations

import json
from urllib.parse import quote, urlsplit

from rg_final_provider import FETCH_TIMEOUT, run_cli
from rg_final_store import PUBLICATION_STATUS_CONTEXT


STATUS_CONTEXT = PUBLICATION_STATUS_CONTEXT
PUBLICATION_SCHEMA = "final-review-publication/v1"


class FinalPublisher:
    """Narrow C14 contract for individual and combined final decisions.

    The store owns verdict, evidence, freshness, trust, and same-store ordering
    authority. This class only renders that decision, performs the explicitly
    authorized single status write, and records its outcome. Constructing it
    with the default trust setting is safe: an orchestrator capability is
    required before even an injected transport is called.
    """

    def __init__(self, store, *, runner=None, trusted_orchestrator=False,
                 repo_root=None, timeout=FETCH_TIMEOUT):
        self.store = store
        self.runner = runner or run_cli
        self.trusted_orchestrator = bool(trusted_orchestrator)
        self.repo_root = repo_root
        self.timeout = float(timeout)
        if self.timeout <= 0 or self.timeout > 30:
            raise ValueError("publication timeout must be greater than 0 and at most 30 seconds")

    def publish_individual(self, run_id, unit_id, *, report_url=None, local_waivers=()):
        """Publish one item's individual unit and whole-scope gate."""
        return self.publish(
            run_id, unit_id, report_url=report_url, local_waivers=local_waivers,
        )

    def publish_combined(self, run_id, *, item_key, report_url=None, local_waivers=()):
        """Publish the combined decision for one participating item's head.

        C14 calls this once per participating item. Requiring ``item_key``
        prevents silently applying a combined outcome to only the stack head
        or to an item the caller did not intend to update.
        """
        units = self.store.machine_report(run_id)["units"]
        combined = [unit for unit in units if unit["kind"] == "combined"]
        if len(combined) != 1:
            raise ValueError("run must contain exactly one combined review unit")
        return self.publish(
            run_id, combined[0]["unit_id"], item_key=str(item_key),
            report_url=report_url, local_waivers=local_waivers,
        )

    def publish(self, run_id, unit_id, *, item_key=None, report_url=None, local_waivers=()):
        """Finalize and audit one provider status attempt without raising I/O errors."""
        report_url = self._report_url(report_url)
        with self.store.publication_guard(run_id, unit_id, item_key) as attempt:
            decision = self.store.publication_decision(
                run_id, unit_id=unit_id, item_key=item_key, local_waivers=local_waivers,
                trusted_orchestrator=self.trusted_orchestrator,
            )
            provider = self._provider(decision)
            provider_state = ("success" if decision["can_publish_pass"] else
                              "failure" if provider == "github" else "failed")
            description = self._description(decision)

            if "run-superseded" in decision["status_blockers"]:
                return self._finalize(
                    attempt, decision, status="superseded", provider_state=None,
                    description=description, report_url=report_url,
                    reason="run-superseded",
                    error="A newer same-store run supersedes this publication",
                )
            if decision["status_blockers"]:
                reason = decision["status_blockers"][0]
                error = self._blocker_message(reason, decision["status_blockers"])
                return self._finalize(
                    attempt, decision, status="local_only", provider_state=None,
                    description=description, report_url=report_url,
                    reason=reason, error=error,
                )

            response, error = self._request(decision, provider_state, description, report_url)
            if error:
                reason = "publication-provider-write-failed"
                return self._finalize(
                    attempt, decision, status="local_only", provider_state=None,
                    description=description, report_url=report_url,
                    reason=reason, error=error,
                )
            return self._finalize(
                attempt, decision, status="published", provider_state=provider_state,
                description=description, report_url=report_url,
                reason="provider-status-recorded", response=response,
            )

    @staticmethod
    def _report_url(value):
        if value is None or value == "":
            return None
        value = str(value).strip()
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("publication report_url must be an absolute HTTP(S) URL")
        if len(value) > 2048:
            raise ValueError("publication report_url is too long")
        return value

    @staticmethod
    def _description(decision):
        identity = decision["identity"]
        report = decision["report"]
        # Full run/store/unit identity and exact head fit a conservative
        # 255-character limit. The HTTP(S) report link carries verbose detail.
        description = (
            f"{decision['publication_state']} store={identity['store_instance_id']} "
            f"run={identity['run_id']}#{identity['run_seq']} "
            f"unit={identity['unit_key']}/{identity['unit_id']} "
            f"head={identity['head_sha']} findings={report['findings']} "
            f"lenses={report['lenses']} tests={report['tests']} "
            f"warnings={report['warnings']} stops={report['stops']}"
        )
        if len(description) > 255:
            # Unit keys are normally individual:N or combined. A custom long
            # key remains fully durable in identity/audit; only its display is
            # shortened, never any UUID or revision coordinate.
            unit_key = str(identity["unit_key"])
            excess = len(description) - 255
            shortened = unit_key[:max(1, len(unit_key) - excess - 1)] + "…"
            description = description.replace(
                f"unit={unit_key}/", f"unit={shortened}/", 1,
            )
        return description[:255]

    @staticmethod
    def _blocker_message(reason, blockers):
        messages = {
            "untrusted-caller": (
                "Only the trusted orchestrator may write final-review provider statuses"
            ),
            "run-not-completed": "The final-review run has not completed",
            "freshness-not-assessed": "The pinned revisions have no freshness assessment",
            "independent-run": "An independent store cannot publish final-review statuses",
            "trust-local-only": "The final-review trust decision is local-only",
            "trust-not-enforced": "The final-review trust decision is unenforced",
            "trust-not-official": "The final-review trust decision is not official",
            "publication-target-required": "The status destination item is not available",
            "unit-required": "A final-review unit is required for publication",
            "unsupported-unit-kind": "Only individual and combined units can publish",
            "unsupported-provider": "The final-review provider does not support statuses",
        }
        detail = messages.get(reason, f"Publication is ineligible: {reason}")
        return detail + " (" + ", ".join(blockers) + ")"

    @staticmethod
    def _provider(decision):
        return decision["transport"]["provider"]

    def _request(self, decision, provider_state, description, report_url):
        if self._provider(decision) == "github":
            return self._github_request(decision, provider_state, description, report_url)
        return self._gitlab_request(decision, provider_state, description, report_url)

    def _gitlab_request(self, decision, provider_state, description, report_url):
        identity = decision["identity"]
        project = quote(str(identity["source_project"]), safe="")
        head = quote(str(identity["head_sha"]), safe="")
        endpoint = f"projects/{project}/statuses/{head}"
        argv = ["glab", "api"]
        reference = decision["transport"].get("reference")
        host = urlsplit(reference).netloc if reference else None
        if host:
            argv.extend(["--hostname", host])
        argv.extend(["--method", "POST", endpoint])
        fields = [
            ("state", provider_state),
            ("context", STATUS_CONTEXT),
            ("description", description),
        ]
        if report_url:
            fields.append(("target_url", report_url))
        for key, value in fields:
            argv.extend(["--raw-field", f"{key}={value}"])
        try:
            out, note = self.runner(argv, self.repo_root, self.timeout)
        except Exception as exc:
            return None, f"GitLab status transport failed: {str(exc)[:500]}"
        if note:
            return None, str(note)[:500]
        if out is None:
            return None, "GitLab status write returned no response"
        try:
            response = json.loads(out)
        except (TypeError, ValueError):
            return None, "GitLab status write returned invalid JSON"
        if not isinstance(response, dict) or not response.get("id"):
            return None, "GitLab status write returned no commit-status identity"
        if response.get("status") not in (None, provider_state):
            return None, "GitLab response state disagrees with the final-review status"
        if response.get("sha") not in (None, identity["head_sha"]):
            return None, "GitLab response SHA disagrees with the pinned final-review head"
        if response.get("name") not in (None, STATUS_CONTEXT) or \
                response.get("context") not in (None, STATUS_CONTEXT):
            return None, "GitLab response used the wrong final-review status context"
        return response, ""

    def _github_request(self, decision, provider_state, description, report_url):
        identity = decision["identity"]
        try:
            pin = next(
                row["pinned"] for row in self.store.pinned_revisions(decision["run_id"])
                if row["item_key"] == identity["item_key"]
            )
            source = pin["source"]
            if source["project_id"] != identity["source_project"]:
                raise ValueError("source project identity changed")
            project = str(source["project_path"])
        except (KeyError, StopIteration, TypeError, ValueError) as exc:
            return None, f"GitHub status source repository could not be pinned: {exc}"
        parts = project.split("/")
        if len(parts) != 2 or any(part in ("", ".", "..") for part in parts):
            return None, "GitHub status source repository is not OWNER/REPO"
        head = quote(str(identity["head_sha"]), safe="")
        endpoint = f"repos/{quote(project, safe='/')}/statuses/{head}"
        host = urlsplit(decision["transport"]["reference"]).netloc.lower()
        argv = ["gh", "api", "--hostname", host,
                "--method", "POST", endpoint]
        fields = [
            ("state", provider_state),
            ("context", STATUS_CONTEXT),
            ("description", description),
        ]
        if report_url:
            fields.append(("target_url", report_url))
        for key, value in fields:
            argv.extend(["--raw-field", f"{key}={value}"])
        try:
            out, note = self.runner(argv, self.repo_root, self.timeout)
        except Exception as exc:
            return None, f"GitHub status transport failed: {str(exc)[:500]}"
        if note:
            return None, str(note)[:500]
        if out is None:
            return None, "GitHub status write returned no response"
        try:
            response = json.loads(out)
        except (TypeError, ValueError):
            return None, "GitHub status write returned invalid JSON"
        if not isinstance(response, dict) or not response.get("id"):
            return None, "GitHub status write returned no commit-status identity"
        if response.get("state") not in (None, provider_state):
            return None, "GitHub response state disagrees with the final-review status"
        if response.get("sha") not in (None, identity["head_sha"]):
            return None, "GitHub response SHA disagrees with the pinned final-review head"
        if response.get("context") not in (None, STATUS_CONTEXT):
            return None, "GitHub response used the wrong final-review status context"
        return response, ""

    def _finalize(self, attempt, decision, *, status, provider_state, description,
                  report_url, reason, error="", response=None):
        published = status == "published"
        official_green = bool(published and provider_state == "success")
        if not published:
            decision["trust"] = attempt.degrade_trust(error or reason)
        provider_status_id = (response or {}).get("id")
        details = {
            "reason": reason,
            "provider_state": provider_state,
            "official_green": official_green,
            "identity": decision["identity"],
            "report": decision["report"],
            "description": description,
            "target_url": report_url,
            "provider_status_id": provider_status_id,
        }
        attempt.record(status, STATUS_CONTEXT, details, error)
        self.store.record_publication_observability(
            decision["run_id"], provider=self._provider(decision), status=status,
            reason=reason, local_only=not published, enforced=published,
            official_green=official_green,
        )
        return {
            "schema": PUBLICATION_SCHEMA,
            "publication_id": attempt.publication_id,
            "run_id": decision["run_id"],
            "unit_id": decision["unit_id"],
            "item_key": decision["item_key"],
            "status": status,
            "status_context": STATUS_CONTEXT,
            "provider_state": provider_state,
            "description": description,
            "target_url": report_url,
            "local_only": not published,
            "enforced": published,
            "official": published,
            "official_green": official_green,
            "error": error,
            "identity": decision["identity"],
            "report": decision["report"],
            "decision": decision,
            "response": response,
        }
