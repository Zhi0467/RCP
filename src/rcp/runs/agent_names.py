"""Task-bound conversation naming and its resolved launch context."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from rcp.agents.command_protocol import CommandResponse, NameCommandRequest
from rcp.agents.prompts import AGENT_NAME_INSTRUCTION
from rcp.agents.staged_command_client import name_usage
from rcp.bulletin import bulletin_actor_id
from rcp.storage import AppStore
from rcp.storage.chat_names import ChatNameError
from rcp.transport.workspace_mailbox import RunStageMailbox

if TYPE_CHECKING:
    from rcp.background import AgentTaskExecution
    from rcp.transport.run_stage import RemoteRunStage


def handle_name(store: AppStore, operation_id: str, request: NameCommandRequest) -> CommandResponse:
    try:
        result = store.self_name_chat_for_task(operation_id, request.arguments.name)
    except ChatNameError as exc:
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message=str(exc),
            result={"code": exc.code},
        )
    return CommandResponse(request_id=request.request_id, status="ok", result=result)


def name_command_usage(client: str) -> str:
    return f"- `{client} {name_usage()}`\n- {AGENT_NAME_INSTRUCTION}"


def resolve_agent_name_state(execution: AgentTaskExecution | None) -> dict[str, object]:
    """Resolve once for this launch so grants and staged context share one snapshot."""
    from rcp.runs.consolidation import is_consolidation

    if execution is None or is_consolidation(execution):
        return {}
    if execution.agent_name_state is None:
        execution.agent_name_state = task_agent_name_state(execution.store, execution.operation_id)
    return execution.agent_name_state


def task_agent_name_state(store: AppStore, operation_id: str) -> dict[str, object]:
    with store.connection() as connection:
        row = connection.execute(
            "SELECT * FROM graph_runs WHERE operation_id=?", (operation_id,)
        ).fetchone()
        actor = bulletin_actor_id(connection, row) if row else None
        if row is None or actor is None:
            return {}
        chat_id = actor.split(":", 1)[1]
        if (
            connection.execute(
                "SELECT 1 FROM chat_display WHERE project_id=? AND chat_id=?",
                (row["project_id"], chat_id),
            ).fetchone()
            is None
        ):
            return {}
    return store.agent_name_state(row["project_id"], chat_id)


def stage_agent_name_pointer(
    execution: AgentTaskExecution | None,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
) -> str:
    state = resolve_agent_name_state(execution)
    if not state.get("can_self_name"):
        return ""
    if remote_stage is not None:
        path = remote_stage.workspace / "agent-name.json"
        mailbox = RunStageMailbox.for_stage(local_stage=None, remote_stage=remote_stage)
    else:
        if local_stage is None:
            raise ValueError("The agent name snapshot requires a launch stage.")
        workspace = local_stage / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        path = workspace / "agent-name.json"
        mailbox = RunStageMailbox.for_stage(local_stage=workspace, remote_stage=None)
    mailbox.write_text("agent-name.json", json.dumps(state, ensure_ascii=True))
    return f"Your agent name and naming permission: `{path}`."
