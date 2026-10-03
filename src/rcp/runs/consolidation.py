"""Authority and keyed graph effects owned by a server-bound consolidation turn."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from rcp.agents.command_protocol import ApplyCommandRequest, CommandResponse
from rcp.limits import PATCH_SELF_CHECK_MAX_REQUEST_BYTES
from rcp.skill_registry import official_registry
from rcp.transport import StateUnavailable

if TYPE_CHECKING:
    from rcp.agents.command_mailbox import CommandTurnIdentity
    from rcp.background import AgentTaskExecution
    from rcp.service import ProjectService
    from rcp.storage import AgentTaskRecord, AppStore
    from rcp.transport.workspace_mailbox import RunStageMailbox


CONSOLIDATION_PRELAUNCH_ERRORS = frozenset(
    {
        "consolidation_authorization_revoked",
        "consolidation_authorization_expired",
        "consolidation_authorizer_mismatch",
        "consolidation_authorizer_departed",
        "consolidation_project_unavailable",
        "consolidation_launch_binding_invalid",
    }
)


def is_consolidation(execution: AgentTaskExecution | None) -> bool:
    return (
        execution is not None
        and execution.store.consolidation_run_for_operation(execution.operation_id) is not None
    )


def require_consolidation_launch(store: AppStore, task: AgentTaskRecord) -> None:
    """Check the captured authorization immediately before an unstarted launch."""
    run = store.consolidation_run_for_operation(task.operation_id)
    if run is None:
        return
    schedule = store.consolidation_schedule(task.project_id)
    if schedule is None or schedule.authorization_id != run.authorization_id:
        raise ValueError("consolidation_authorization_revoked")
    if datetime.fromisoformat(schedule.expires_at) <= datetime.fromisoformat(store.now()):
        raise ValueError("consolidation_authorization_expired")
    if task.authorized_by != run.authorized_by:
        raise ValueError("consolidation_authorizer_mismatch")
    if task.authorized_by is None or not store.is_project_member(
        task.project_id, task.authorized_by.user_id
    ):
        raise ValueError("consolidation_authorizer_departed")
    member = store.space_user(task.authorized_by.user_id)
    if member is None or member.removal_started_at is not None or member.removed_at is not None:
        raise ValueError("consolidation_authorizer_departed")
    try:
        store.require_project_accepts_new_work(task.project_id)
    except ValueError as exc:
        raise ValueError("consolidation_project_unavailable") from exc
    if (
        task.kind != "project_chat"
        or task.request.get("mode") != "work"
        or task.request.get("trigger") != "schedule"
        or task.graph_target.kind != "main"
        or task.native_session_id is not None
        or task.dispatch_authority is None
        or task.dispatch_authority.task_contract != "work_auto"
    ):
        raise ValueError("consolidation_launch_binding_invalid")


def consolidation_skill_selection():
    return official_registry().resolve(workflow_ids=["graph-consolidation"], skill_ids=[])


def source_effect_id(operation_id: str, key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"rcp:consolidation:apply:{operation_id}:{key}"))


def settlement_source_effect_id(
    service: ProjectService, execution: AgentTaskExecution, patch_text: str
) -> str:
    """Recover a consumed effect even when the process died before saving its result."""
    digest = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
    for patch in service.history.load_patches():
        if (
            patch.admission == "accepted"
            and patch.source_operation_id == execution.operation_id
            and patch.source_effect_sha256 == digest
            and patch.source_effect_id is not None
        ):
            return patch.source_effect_id
    for receipt in execution.store.list_consolidation_apply_receipts(execution.operation_id):
        if receipt["sha256"] == digest:
            return receipt["source_effect_id"]
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"rcp:consolidation:final:{execution.operation_id}:{digest}")
    )


def consolidation_apply_handler(
    service: Callable[[], ProjectService],
    execution: AgentTaskExecution,
    mailbox: RunStageMailbox,
    run_truth_scope: list[str],
):
    def apply(request: ApplyCommandRequest, identity: CommandTurnIdentity) -> CommandResponse:
        from rcp.runs.tasks.work import _apply_work_patch

        if (
            not is_consolidation(execution)
            or identity.authority != "broker"
            or identity.task_id != execution.operation_id
        ):
            return CommandResponse(
                request_id=request.request_id,
                status="invalid",
                message="consolidation_binding_required",
            )
        task = execution.store.agent_task(execution.operation_id)
        if (
            task is None
            or task.authorized_by is None
            or not execution.store.is_project_member(task.project_id, task.authorized_by.user_id)
        ):
            return CommandResponse(
                request_id=request.request_id,
                status="invalid",
                message="consolidation_authorizer_departed",
            )
        member = execution.store.space_user(task.authorized_by.user_id)
        if member is None or member.removal_started_at is not None or member.removed_at is not None:
            return CommandResponse(
                request_id=request.request_id,
                status="invalid",
                message="consolidation_authorizer_departed",
            )
        key = request.idempotency_key
        if not key:
            return CommandResponse(
                request_id=request.request_id,
                status="invalid",
                message="consolidation_apply_key_required",
            )
        previous = next(
            (
                item
                for item in execution.store.list_consolidation_apply_receipts(
                    execution.operation_id
                )
                if item["key"] == key
            ),
            None,
        )
        receipt = None
        try:
            try:
                text = mailbox.read_text("patch.json", max_bytes=PATCH_SELF_CHECK_MAX_REQUEST_BYTES)
            except FileNotFoundError:
                if previous is None or previous["result"] is None:
                    raise ValueError("consolidation_patch_missing") from None
                return CommandResponse(request_id=request.request_id, **previous["result"])
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            receipt = execution.store.reserve_consolidation_apply(
                execution.operation_id,
                key,
                digest,
                text,
                source_effect_id(execution.operation_id, key),
            )
            if receipt["result"] is not None:
                response = CommandResponse(request_id=request.request_id, **receipt["result"])
            else:
                result, failure = _apply_work_patch(
                    service(),
                    execution,
                    text,
                    run_truth_scope=run_truth_scope,
                    source_operation_id=execution.operation_id,
                    source_effect_id=receipt["source_effect_id"],
                )
                if result is None:
                    assert failure is not None
                    execution.store.record_consolidation_apply_failure(
                        execution.operation_id,
                        key,
                        "invalid" if failure.correctable else "unavailable",
                        failure.message,
                    )
                    return CommandResponse(
                        request_id=request.request_id,
                        status="invalid" if failure.correctable else "unavailable",
                        message=failure.message,
                    )
                response = CommandResponse(
                    request_id=request.request_id,
                    status="ok",
                    result={
                        "revision": result.applied_revision
                        if result.applied_revision is not None
                        else service().history.state().revision,
                        "disposition": "applied" if result.status == "applied" else "valid_empty",
                        "graph_update": result.model_dump(mode="json"),
                    },
                )
                execution.store.finish_consolidation_apply(
                    execution.operation_id,
                    key,
                    response.model_dump(mode="json", exclude={"request_id"}),
                )
            if response.status == "ok":
                mailbox.remove_if_sha256("patch.json", digest)
            return response
        except (OSError, StateUnavailable) as exc:
            if receipt is not None:
                execution.store.record_consolidation_apply_failure(
                    execution.operation_id, key, "unavailable", str(exc)
                )
            return CommandResponse(
                request_id=request.request_id, status="unavailable", message=str(exc)
            )
        except ValueError as exc:
            return CommandResponse(
                request_id=request.request_id, status="invalid", message=str(exc)
            )

    return apply
