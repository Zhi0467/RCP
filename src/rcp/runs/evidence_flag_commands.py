"""Evidence flags belong only to a server-bound consolidation mailbox."""

from __future__ import annotations

from collections.abc import Callable

from rcp.agents.command_protocol import CommandResponse, FlagCommandRequest
from rcp.core.models import GraphState
from rcp.core.operations import CreateNodesOperation
from rcp.core.project_types import project_type_of
from rcp.limits import EVIDENCE_FLAG_FINDING_MAX_CHARS
from rcp.service import ProjectService
from rcp.storage import AppStore
from rcp.storage.evidence_flags import EvidenceFlagError
from rcp.transport import StateUnavailable

RCP_FLAG_KEY_PREFIX = "rcp:"
_ADDED_FINDING = "Consolidation added this Evidence."


def is_current_evidence(state: GraphState, node_id: str) -> bool:
    """Flags are raised and addressed only on Evidence that is present and not retired."""
    node = state.nodes.get(node_id)
    project_type = project_type_of(state)
    return (
        node is not None
        and project_type.is_outcome(node.type)
        and getattr(node, project_type.lifecycle_field(node.type)) != project_type.retired_value
    )


def handle_flag(
    service: ProjectService, store: AppStore, operation_id: str, request: FlagCommandRequest
) -> CommandResponse:
    task = store.agent_task(operation_id)
    if (request.idempotency_key or "").startswith(RCP_FLAG_KEY_PREFIX):
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message=f"Flag keys starting with {RCP_FLAG_KEY_PREFIX!r} are reserved for RCP.",
            result={"code": "flag_key_invalid"},
        )
    if task is None:
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message="The flag command task is unavailable.",
            result={"code": "flag_task_unavailable"},
        )

    def validate_evidence() -> int:
        state = service.for_graph_target(task.graph_target).history.state()
        if not is_current_evidence(state, request.arguments.evidence_id):
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
        "--kind overclaim|underclaim|no_source|unverified --finding <text> "
        "[--source <path[:line]>]`"
    )


def hold_consolidation_additions(
    service: Callable[[], ProjectService], store: AppStore, operation_id: str
) -> bool:
    """Hold every Evidence node this consolidation run's committed Patches created.

    RCP raises these, not the agent, so new Evidence never reaches the graph unseen.
    The created ids come from the target's canonical Patches bound to this operation,
    keyed Applies and the final ``patch.json`` alike, never a graph diff. The keyed
    flags replay, so rerunning after any apply or at run settlement recovers a hold
    an interrupted process missed. A failure is recorded on the task; the Patch
    stands. A run that never launched committed nothing, so its settlement needs no
    canonical read. Returns False when canonical state could not be read.
    """
    task = store.agent_task(operation_id)
    if task is None or not store.agent_task_has_receipt(operation_id, "agent_launch"):
        return True
    try:
        history = service().for_graph_target(task.graph_target).history
        created = [
            (node.id, (patch.summary.strip().splitlines() or [_ADDED_FINDING])[0])
            for patch in history.load_patches()
            if patch.source_operation_id == operation_id and patch.admission == "accepted"
            for operation in patch.ops
            if isinstance(operation, CreateNodesOperation)
            for node in operation.nodes
        ]
        if not created:
            return True
        state = history.state()
    except (OSError, StateUnavailable, ValueError) as exc:
        store.record_agent_task_event(
            operation_id,
            f"Added Evidence could not be held for review yet: {exc}",
            level="warning",
        )
        return False
    for node_id, finding in created:
        if not is_current_evidence(state, node_id):
            continue
        try:
            store.execute_evidence_flag_command(
                task.project_id,
                operation_id,
                key=f"{RCP_FLAG_KEY_PREFIX}consolidation_added:{node_id}",
                evidence_id=node_id,
                kind="consolidation_added",
                finding=finding[:EVIDENCE_FLAG_FINDING_MAX_CHARS],
                validate_evidence=lambda: state.revision,
            )
        except (EvidenceFlagError, ValueError) as exc:
            store.record_agent_task_event(
                operation_id,
                f"Evidence {node_id} was added but could not be held for review: {exc}",
                level="warning",
            )
    return True
