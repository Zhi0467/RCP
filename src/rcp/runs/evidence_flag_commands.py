"""Evidence flags belong only to a server-bound consolidation mailbox."""

from __future__ import annotations

from rcp.agents.command_protocol import CommandResponse, FlagCommandRequest
from rcp.core.project_types import project_type_of
from rcp.service import ProjectService
from rcp.storage import AppStore
from rcp.storage.evidence_flags import EvidenceFlagError


def handle_flag(
    service: ProjectService, store: AppStore, operation_id: str, request: FlagCommandRequest
) -> CommandResponse:
    task = store.agent_task(operation_id)
    if task is None:
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message="The flag command task is unavailable.",
            result={"code": "flag_task_unavailable"},
        )

    def validate_evidence() -> int:
        state = service.history.state()
        node = state.nodes.get(request.arguments.evidence_id)
        project_type = project_type_of(state)
        if (
            node is None
            or not project_type.is_outcome(node.type)
            or getattr(node, project_type.lifecycle_field(node.type)) == project_type.retired_value
        ):
            raise EvidenceFlagError("flag_evidence_invalid")
        return state.revision

    try:
        result = store.execute_evidence_flag_command(
            task.project_id,
            operation_id,
            key=request.idempotency_key,
            evidence_id=request.arguments.evidence_id,
            kind=request.arguments.kind,
            finding=request.arguments.finding,
            source=request.arguments.source,
            validate_evidence=validate_evidence,
        )
    except EvidenceFlagError as exc:
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message=str(exc),
            result={"code": exc.code},
        )
    except ValueError as exc:
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message=str(exc),
            result={"code": "flag_write_unavailable"},
        )
    return CommandResponse(request_id=request.request_id, status="ok", result=result)


def flag_command_usage(client: str) -> str:
    return (
        f"- `{client} flag --key <key> --evidence <evidence-id> "
        "--kind overclaim|underclaim|no_source --finding <text> [--source <path[:line]>]`"
    )
