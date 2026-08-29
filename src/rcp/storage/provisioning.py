"""Durable, human-authorized team-project preparation state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import PurePosixPath

from pydantic import TypeAdapter

from rcp.core.models import AuthorizedHuman
from rcp.server_ops.models import MessageText, ServerStep
from rcp.storage.models import (
    ProjectProvisioningCancellationDisposition,
    ProjectProvisioningKind,
    ProjectProvisioningMachineIntent,
    ProjectProvisioningMachineRecord,
    ProjectProvisioningProviderCheckRecord,
    ProjectProvisioningProviderIntent,
    ProjectProvisioningRepositoryIntent,
    ProjectProvisioningRepositoryRecord,
    ProjectProvisioningRequestRecord,
    ProjectProvisioningStatus,
    ProjectProvisioningStepReceiptRecord,
    _canonical_uuid4,
)

_PROVISIONING_TRANSITIONS: dict[ProjectProvisioningStatus, frozenset[ProjectProvisioningStatus]] = {
    "waiting_for_server_setup": frozenset(
        {"setup_in_progress", "operator_action_needed", "cancelled"}
    ),
    "setup_in_progress": frozenset(
        {"setup_in_progress", "operator_action_needed", "ready_for_review", "cancelled"}
    ),
    "operator_action_needed": frozenset(
        {"setup_in_progress", "operator_action_needed", "cancelled"}
    ),
    "ready_for_review": frozenset({"setup_in_progress", "completed", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}
_MESSAGE_TEXT_ADAPTER = TypeAdapter(MessageText)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def project_provisioning_review_digest(record: ProjectProvisioningRequestRecord) -> str:
    """Bind the exact identity, placement, Git, and provider review payload."""

    payload = {
        "request_id": record.request_id,
        "kind": record.kind,
        "target_space_id": record.target_space_id,
        "authorized_by": record.authorized_by.model_dump(mode="json"),
        "proposed_project_id": record.proposed_project_id,
        "machines": [machine.model_dump(mode="json") for machine in record.machines],
        "repositories": [repository.model_dump(mode="json") for repository in record.repositories],
        "provider_checks": [check.model_dump(mode="json") for check in record.provider_checks],
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _verify_project_provisioning_review_digest(
    record: ProjectProvisioningRequestRecord,
) -> ProjectProvisioningRequestRecord:
    if record.status in {
        "ready_for_review",
        "completed",
    } and record.final_review_digest != project_provisioning_review_digest(record):
        raise ValueError("project provisioning final-review digest does not match its payload")
    return record


class ProjectProvisioningStoreMixin:
    """One transactional state machine for every team-project preparation."""

    def create_project_provisioning_request(
        self,
        *,
        kind: ProjectProvisioningKind,
        authorized_by: AuthorizedHuman,
        machines: list[ProjectProvisioningMachineIntent],
        repositories: list[ProjectProvisioningRepositoryIntent],
        provider_checks: list[ProjectProvisioningProviderIntent],
        source_project_id: str | None = None,
    ) -> ProjectProvisioningRequestRecord:
        """Reserve one project namespace without creating project authority."""

        if kind not in {"create_team_project", "incoming_transfer"}:
            raise ValueError("project provisioning kind is invalid")
        authorizer = AuthorizedHuman.model_validate(authorized_by.model_dump(mode="json"))
        machine_intents = [
            ProjectProvisioningMachineIntent.model_validate(machine.model_dump(mode="json"))
            for machine in machines
        ]
        repository_intents = [
            ProjectProvisioningRepositoryIntent.model_validate(repository.model_dump(mode="json"))
            for repository in repositories
        ]
        provider_intents = [
            ProjectProvisioningProviderIntent.model_validate(check.model_dump(mode="json"))
            for check in provider_checks
        ]
        if kind == "create_team_project":
            if source_project_id is not None:
                raise ValueError("new team-project provisioning cannot name a source project id")
            proposed_project_id = str(uuid.uuid4())
        else:
            if source_project_id is None:
                raise ValueError("incoming transfer provisioning requires the source project id")
            try:
                proposed_project_id = _canonical_uuid4(
                    source_project_id,
                    label="incoming transfer project identity",
                )
            except RuntimeError as exc:
                raise ValueError(str(exc)) from exc
        target_space_id = self.space_id
        request_id = str(uuid.uuid4())
        machine_records = [
            ProjectProvisioningMachineRecord(
                **machine.model_dump(mode="json"),
                resolved_central_root=None,
            )
            for machine in machine_intents
        ]
        machine_map = {machine.alias: machine for machine in machine_records}
        repository_records: list[ProjectProvisioningRepositoryRecord] = []
        for repository in repository_intents:
            machine = machine_map.get(repository.machine_alias)
            if machine is None:
                raise ValueError("provisioning repository names an unknown machine")
            intended_path = str(
                PurePosixPath(machine.central_root)
                / proposed_project_id
                / "repositories"
                / repository.alias
            )
            repository_records.append(
                ProjectProvisioningRepositoryRecord(
                    **repository.model_dump(mode="json"),
                    intended_path=intended_path,
                )
            )
        provider_records = [
            ProjectProvisioningProviderCheckRecord(**check.model_dump(mode="json"))
            for check in provider_intents
        ]
        now = self.now()
        record = ProjectProvisioningRequestRecord(
            request_id=request_id,
            kind=kind,
            status="waiting_for_server_setup",
            target_space_id=target_space_id,
            authorized_by=authorizer,
            proposed_project_id=proposed_project_id,
            machines=machine_records,
            repositories=repository_records,
            provider_checks=provider_records,
            revision=0,
            created_at=now,
            updated_at=now,
        )
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            identity = connection.execute(
                "SELECT space_id, space_kind FROM space_identity WHERE singleton = 1"
            ).fetchone()
            if (
                identity is None
                or identity["space_id"] != target_space_id
                or identity["space_kind"] != "team"
            ):
                raise ValueError("project provisioning requires this exact team space")
            if authorizer.space_id != target_space_id:
                raise ValueError("project provisioning authorizer belongs to another space")
            if (
                connection.execute(
                    "SELECT 1 FROM space_users WHERE user_id = ?",
                    (authorizer.user_id,),
                ).fetchone()
                is None
            ):
                raise ValueError("project provisioning authorizer is not a current space member")
            if (
                connection.execute(
                    "SELECT 1 FROM projects WHERE project_id = ?",
                    (proposed_project_id,),
                ).fetchone()
                is not None
            ):
                raise ValueError("the proposed project identity already exists in this space")
            try:
                self._insert_project_provisioning_request(connection, record)
            except sqlite3.IntegrityError as exc:
                raise ValueError("the proposed project identity is already reserved") from exc
        return record

    def project_provisioning_request(
        self,
        request_id: str,
    ) -> ProjectProvisioningRequestRecord | None:
        try:
            canonical_request_id = _canonical_uuid4(
                request_id,
                label="provisioning request identity",
            )
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM project_provisioning_requests WHERE request_id = ?",
                (canonical_request_id,),
            ).fetchone()
        return self._project_provisioning_record(row) if row is not None else None

    def project_provisioning_requests(
        self,
        *,
        status: ProjectProvisioningStatus | None = None,
    ) -> list[ProjectProvisioningRequestRecord]:
        if status is not None and status not in _PROVISIONING_TRANSITIONS:
            raise ValueError("project provisioning status is invalid")
        with self.connection() as connection:
            if status is None:
                rows = connection.execute(
                    """
                    SELECT * FROM project_provisioning_requests
                    ORDER BY created_at DESC, request_id
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM project_provisioning_requests
                    WHERE status = ?
                    ORDER BY created_at DESC, request_id
                    """,
                    (status,),
                ).fetchall()
        return [self._project_provisioning_record(row) for row in rows]

    def transition_project_provisioning_request(
        self,
        request_id: str,
        *,
        receipt_id: str,
        phase: str,
        expected_revision: int,
        expected_status: ProjectProvisioningStatus,
        to_status: ProjectProvisioningStatus,
        machines: list[ProjectProvisioningMachineRecord],
        repositories: list[ProjectProvisioningRepositoryRecord],
        provider_checks: list[ProjectProvisioningProviderCheckRecord],
        retryable_diagnostic: str | None = None,
        operator_action: ServerStep | None = None,
        cancellation_disposition: ProjectProvisioningCancellationDisposition | None = None,
    ) -> ProjectProvisioningRequestRecord:
        """Commit one named machine step or return its already-committed result."""

        try:
            canonical_request_id = _canonical_uuid4(
                request_id,
                label="provisioning request identity",
            )
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        if expected_revision < 0:
            raise ValueError("project provisioning expected revision cannot be negative")
        if (
            expected_status not in _PROVISIONING_TRANSITIONS
            or to_status not in _PROVISIONING_TRANSITIONS
        ):
            raise ValueError("project provisioning transition status is invalid")
        machine_records = [
            ProjectProvisioningMachineRecord.model_validate(machine.model_dump(mode="json"))
            for machine in machines
        ]
        repository_records = [
            ProjectProvisioningRepositoryRecord.model_validate(repository.model_dump(mode="json"))
            for repository in repositories
        ]
        provider_records = [
            ProjectProvisioningProviderCheckRecord.model_validate(check.model_dump(mode="json"))
            for check in provider_checks
        ]
        normalized_action = (
            None
            if operator_action is None
            else ServerStep.model_validate_json(operator_action.model_dump_json())
        )
        normalized_diagnostic = (
            None
            if retryable_diagnostic is None
            else _MESSAGE_TEXT_ADAPTER.validate_python(retryable_diagnostic)
        )
        transition_payload = {
            "request_id": canonical_request_id,
            "receipt_id": receipt_id,
            "phase": phase,
            "expected_revision": expected_revision,
            "expected_status": expected_status,
            "to_status": to_status,
            "machines": [machine.model_dump(mode="json") for machine in machine_records],
            "repositories": [
                repository.model_dump(mode="json") for repository in repository_records
            ],
            "provider_checks": [check.model_dump(mode="json") for check in provider_records],
            "retryable_diagnostic": normalized_diagnostic,
            "operator_action": (
                None if normalized_action is None else normalized_action.model_dump(mode="json")
            ),
            "cancellation_disposition": cancellation_disposition,
        }
        transition_sha256 = hashlib.sha256(
            _canonical_json(transition_payload).encode("utf-8")
        ).hexdigest()
        now = self.now()
        receipt_shape = ProjectProvisioningStepReceiptRecord(
            request_id=canonical_request_id,
            receipt_id=receipt_id,
            phase=phase,
            from_status=expected_status,
            to_status=to_status,
            transition_sha256=transition_sha256,
            resulting_revision=expected_revision + 1,
            created_at=now,
        )
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior_receipt_row = connection.execute(
                """
                SELECT * FROM project_provisioning_step_receipts
                WHERE request_id = ? AND receipt_id = ?
                """,
                (canonical_request_id, receipt_id),
            ).fetchone()
            if prior_receipt_row is not None:
                prior_receipt = self._project_provisioning_receipt(prior_receipt_row)
                if prior_receipt.transition_sha256 != transition_sha256:
                    raise ValueError("project provisioning receipt already names another step")
                current_row = connection.execute(
                    "SELECT * FROM project_provisioning_requests WHERE request_id = ?",
                    (canonical_request_id,),
                ).fetchone()
                if current_row is None:
                    raise RuntimeError("project provisioning receipt lost its request")
                return self._project_provisioning_record(current_row)
            current_row = connection.execute(
                "SELECT * FROM project_provisioning_requests WHERE request_id = ?",
                (canonical_request_id,),
            ).fetchone()
            if current_row is None:
                raise KeyError(canonical_request_id)
            current = self._project_provisioning_record(current_row)
            if current.revision != expected_revision or current.status != expected_status:
                raise ValueError("project provisioning request changed; reload it before retrying")
            if to_status not in _PROVISIONING_TRANSITIONS[current.status]:
                raise ValueError(
                    f"project provisioning cannot move from {current.status} to {to_status}"
                )
            setup_started_at = current.setup_started_at
            if (
                to_status
                in {
                    "setup_in_progress",
                    "operator_action_needed",
                    "ready_for_review",
                    "completed",
                }
                and setup_started_at is None
            ):
                setup_started_at = now
            ready_at = now if to_status == "ready_for_review" else None
            if to_status == "completed":
                ready_at = current.ready_at
                if (
                    machine_records != current.machines
                    or repository_records != current.repositories
                    or provider_records != current.provider_checks
                ):
                    raise ValueError("project completion cannot change the reviewed machine state")
            values = current.model_dump(mode="json")
            values.update(
                {
                    "status": to_status,
                    "machines": [machine.model_dump(mode="json") for machine in machine_records],
                    "repositories": [
                        repository.model_dump(mode="json") for repository in repository_records
                    ],
                    "provider_checks": [
                        check.model_dump(mode="json") for check in provider_records
                    ],
                    "retryable_diagnostic": normalized_diagnostic,
                    "operator_action": (
                        None
                        if normalized_action is None
                        else normalized_action.model_dump(mode="json")
                    ),
                    "final_review_digest": None,
                    "cancellation_disposition": cancellation_disposition,
                    "revision": expected_revision + 1,
                    "updated_at": now,
                    "setup_started_at": setup_started_at,
                    "ready_at": ready_at,
                    "completed_at": now if to_status == "completed" else None,
                    "cancelled_at": now if to_status == "cancelled" else None,
                }
            )
            if to_status in {"ready_for_review", "completed"}:
                values["final_review_digest"] = "0" * 64
                draft = ProjectProvisioningRequestRecord.model_validate_json(
                    _canonical_json(values)
                )
                values["final_review_digest"] = project_provisioning_review_digest(draft)
                if (
                    to_status == "completed"
                    and values["final_review_digest"] != current.final_review_digest
                ):
                    raise ValueError("project provisioning final review changed before completion")
            updated = _verify_project_provisioning_review_digest(
                ProjectProvisioningRequestRecord.model_validate_json(_canonical_json(values))
            )
            changed = connection.execute(
                """
                UPDATE project_provisioning_requests
                SET status = ?, machines_json = ?, repositories_json = ?,
                    provider_checks_json = ?, retryable_diagnostic = ?,
                    operator_action_json = ?, final_review_digest = ?,
                    cancellation_disposition = ?, revision = ?, updated_at = ?,
                    setup_started_at = ?, ready_at = ?, completed_at = ?, cancelled_at = ?
                WHERE request_id = ? AND revision = ? AND status = ?
                """,
                (
                    updated.status,
                    _canonical_json([item.model_dump(mode="json") for item in updated.machines]),
                    _canonical_json(
                        [item.model_dump(mode="json") for item in updated.repositories]
                    ),
                    _canonical_json(
                        [item.model_dump(mode="json") for item in updated.provider_checks]
                    ),
                    updated.retryable_diagnostic,
                    (
                        None
                        if updated.operator_action is None
                        else _canonical_json(updated.operator_action.model_dump(mode="json"))
                    ),
                    updated.final_review_digest,
                    updated.cancellation_disposition,
                    updated.revision,
                    updated.updated_at,
                    updated.setup_started_at,
                    updated.ready_at,
                    updated.completed_at,
                    updated.cancelled_at,
                    canonical_request_id,
                    expected_revision,
                    expected_status,
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeError("project provisioning transition lost its transaction guard")
            connection.execute(
                """
                INSERT INTO project_provisioning_step_receipts (
                    request_id, receipt_id, phase, from_status, to_status,
                    transition_sha256, resulting_revision, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_shape.request_id,
                    receipt_shape.receipt_id,
                    receipt_shape.phase,
                    receipt_shape.from_status,
                    receipt_shape.to_status,
                    receipt_shape.transition_sha256,
                    receipt_shape.resulting_revision,
                    receipt_shape.created_at,
                ),
            )
        return updated

    def project_provisioning_step_receipts(
        self,
        request_id: str,
    ) -> list[ProjectProvisioningStepReceiptRecord]:
        try:
            canonical_request_id = _canonical_uuid4(
                request_id,
                label="provisioning request identity",
            )
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM project_provisioning_step_receipts
                WHERE request_id = ?
                ORDER BY resulting_revision, receipt_id
                """,
                (canonical_request_id,),
            ).fetchall()
        return [self._project_provisioning_receipt(row) for row in rows]

    @staticmethod
    def _insert_project_provisioning_request(
        connection: sqlite3.Connection,
        record: ProjectProvisioningRequestRecord,
    ) -> None:
        connection.execute(
            """
            INSERT INTO project_provisioning_requests (
                request_id, kind, status, target_space_id, authorized_by_json,
                proposed_project_id, machines_json, repositories_json,
                provider_checks_json, retryable_diagnostic, operator_action_json,
                final_review_digest, cancellation_disposition, revision,
                created_at, updated_at, setup_started_at, ready_at,
                completed_at, cancelled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.request_id,
                record.kind,
                record.status,
                record.target_space_id,
                _canonical_json(record.authorized_by.model_dump(mode="json")),
                record.proposed_project_id,
                _canonical_json([item.model_dump(mode="json") for item in record.machines]),
                _canonical_json([item.model_dump(mode="json") for item in record.repositories]),
                _canonical_json([item.model_dump(mode="json") for item in record.provider_checks]),
                record.retryable_diagnostic,
                None,
                record.final_review_digest,
                record.cancellation_disposition,
                record.revision,
                record.created_at,
                record.updated_at,
                record.setup_started_at,
                record.ready_at,
                record.completed_at,
                record.cancelled_at,
            ),
        )

    @staticmethod
    def _project_provisioning_record(row: sqlite3.Row) -> ProjectProvisioningRequestRecord:
        try:
            return _verify_project_provisioning_review_digest(
                ProjectProvisioningRequestRecord.model_validate_json(
                    _canonical_json(
                        {
                            "request_id": row["request_id"],
                            "kind": row["kind"],
                            "status": row["status"],
                            "target_space_id": row["target_space_id"],
                            "authorized_by": json.loads(row["authorized_by_json"]),
                            "proposed_project_id": row["proposed_project_id"],
                            "machines": json.loads(row["machines_json"]),
                            "repositories": json.loads(row["repositories_json"]),
                            "provider_checks": json.loads(row["provider_checks_json"]),
                            "retryable_diagnostic": row["retryable_diagnostic"],
                            "operator_action": (
                                None
                                if row["operator_action_json"] is None
                                else json.loads(row["operator_action_json"])
                            ),
                            "final_review_digest": row["final_review_digest"],
                            "cancellation_disposition": row["cancellation_disposition"],
                            "revision": row["revision"],
                            "created_at": row["created_at"],
                            "updated_at": row["updated_at"],
                            "setup_started_at": row["setup_started_at"],
                            "ready_at": row["ready_at"],
                            "completed_at": row["completed_at"],
                            "cancelled_at": row["cancelled_at"],
                        }
                    )
                )
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError("stored project provisioning request is invalid") from exc

    @staticmethod
    def _project_provisioning_receipt(row: sqlite3.Row) -> ProjectProvisioningStepReceiptRecord:
        try:
            return ProjectProvisioningStepReceiptRecord.model_validate(dict(row))
        except ValueError as exc:
            raise RuntimeError("stored project provisioning receipt is invalid") from exc


__all__ = ["ProjectProvisioningStoreMixin", "project_provisioning_review_digest"]
