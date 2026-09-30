"""A scratch-only revoking edit in an operational native session."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing
from pathlib import Path
from typing import TYPE_CHECKING

from rcp.agents import AgentEvent, AgentLauncher
from rcp.agents.continuation_prompt import compose
from rcp.agents.prompts import _attachment_items
from rcp.runs.chat import (
    _append_chat_exchange,
    _clear_stale_turn_handoffs,
    _discover_chat_artifacts,
    _local_chat_workspace_for_task,
    finalize_artifact_edit,
    prepare_artifact_edit_directory,
    stage_artifact_context,
)
from rcp.runs.recorded_settlement import absorb_recorded_events, provider_turn_request
from rcp.runs.recorded_turn import RecordedProviderTurn, decode_recorded_turn
from rcp.runs.session_master import record_inline_prompt
from rcp.runs.shared import (
    _ProviderOutcome,
    _record_agent_launch_receipt,
    _sse,
    _stream_agent_events,
    _swept_stage_root,
)
from rcp.service import ArtifactEditAdmission, ProjectService, RunRequest
from rcp.transport import RemoteRunStage

if TYPE_CHECKING:
    from rcp.background import AgentTaskExecution

ARTIFACT_EDIT_FINALIZATION_ROLE = "artifact_edit_finalization"


def _local_edit_workspace(execution, edit, local: Path, session_id: str | None) -> Path:
    """Follow durable edit origins to the owner of this exact local stage layout."""

    current = execution.store.agent_task(execution.operation_id)
    if current is None:
        raise ValueError("The artifact edit task is unavailable.")
    origin_id = edit.origin_operation_id
    seen = {execution.operation_id}
    while True:
        if origin_id in seen:
            raise ValueError("The artifact edit origin lineage contains a cycle.")
        seen.add(origin_id)
        origin = execution.store.agent_task(origin_id)
        if (
            origin is None
            or origin.history_only
            or origin.project_id != current.project_id
            or origin.stage_host
            or origin.stage_root != str(local)
            or origin.native_session_id != session_id
        ):
            raise ValueError(
                "The artifact edit origin no longer matches its saved session and stage."
            )
        if origin.kind in {"node_chat", "project_chat"}:
            return _local_chat_workspace_for_task(execution.store, origin)
        if origin.kind != "artifact_edit":
            return local
        prior = ArtifactEditAdmission.model_validate(origin.request.get("artifact_edit"))
        if prior.fresh_session:
            return local
        if prior.stage_host or prior.stage_root != str(local):
            raise ValueError("The prior artifact edit changed its saved stage.")
        origin_id = prior.origin_operation_id


def _open_stage(service, request, data_dir, execution):
    edit = request.artifact_edit
    assert edit is not None
    machine = service.manifest.machine_map[request.run_on]
    root = execution.stage_root
    if root and (execution.stage_host or "") != machine.host:
        raise ValueError("The edit's saved stage differs from its execution host.")
    if machine.host:
        remote = RemoteRunStage(machine.host)
        if root:
            remote.attach(root)
        elif edit.fresh_session:
            remote.open("artifact-edit-" + edit.operation_id, reuse=False)
        else:
            raise ValueError("The edit's exact stage is unavailable.")
        execution.checkpoint_stage(machine.host, str(remote.root))
        return None, remote, Path(str(remote.workspace)), machine
    if root:
        local = Path(root)
        if not local.is_absolute() or local.is_symlink() or not local.is_dir():
            raise ValueError("The edit's saved stage is unavailable or unsafe.")
    elif edit.fresh_session:
        local = _swept_stage_root(data_dir, store=execution.store) / (
            "artifact-edit-" + edit.operation_id
        )
        local.mkdir(parents=True, exist_ok=True)
    else:
        raise ValueError("The edit's exact stage is unavailable.")
    execution.checkpoint_stage("", str(local))
    workspace = local
    if not edit.fresh_session:
        workspace = _local_edit_workspace(execution, edit, local, request.session_id)
        if workspace.is_symlink() or not workspace.is_dir():
            raise ValueError("The edit's recorded workspace is unavailable or unsafe.")
    return local, None, workspace, machine


def _settle(service, request, execution, workspace, remote, directory, outcome) -> Iterator[str]:
    if not outcome.completed or outcome.failed or outcome.paused:
        return
    answer = "\n\n".join(part.strip() for part in outcome.answers if part.strip())
    if not answer:
        yield _sse(AgentEvent(event="error", text="The artifact edit finished without answering."))
        return
    edit = request.artifact_edit
    assert edit is not None
    artifacts = _discover_chat_artifacts(execution, edit.staged_scope_id, directory, remote)
    artifacts = finalize_artifact_edit(
        request,
        execution,
        artifact_scope_id=edit.staged_scope_id,
        artifact_directory=directory,
        remote_stage=remote,
        artifacts=artifacts,
    )
    if edit.reply_episode_id is None:
        _append_chat_exchange(
            service, request, answer, outcome.session_id, None, execution=execution
        )
    # The orchestrator thread reads the delivered human mail and this task's labelled answer.
    yield _sse(AgentEvent(event="answer", text=answer))
    for artifact in artifacts:
        yield _sse(AgentEvent(event="artifact", artifact=artifact))
    yield _sse(AgentEvent(event="done"))


async def stream_artifact_edit_run(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    execution: AgentTaskExecution,
) -> AsyncIterator[str]:
    edit = request.artifact_edit
    if edit is None or edit.launch_kind != "revoking" or request.mode != "discuss":
        raise ValueError("A revoking edit requires its admitted file-only request.")
    local, remote, workspace, machine = _open_stage(service, request, data_dir, execution)
    _clear_stale_turn_handoffs(workspace, remote)
    directory = prepare_artifact_edit_directory(request, execution, workspace, remote)
    attachment = stage_artifact_context(
        service,
        request,
        execution,
        local_stage=local,
        remote_stage=remote,
        artifact_path=str(directory),
    )
    assert attachment is not None
    prompt = compose(
        "report",
        master=None,
        delta=None,
        parts=[
            "Edit the attached file in place according to the human's comment. Keep its name. "
            "You have writable scratch only. Do not change project files, the graph, watchers, "
            "or episode controls. Reply to the human when finished.",
            _attachment_items([attachment.pointer]),
            request.message or "",
        ],
    )
    contract_path = record_inline_prompt(
        execution,
        local_stage=local,
        remote_stage=remote,
        label="artifact-edit-" + execution.operation_id + ".md",
        role="artifact_edit_prompt",
        prompt=prompt,
    )
    if remote:
        remote.finalize_inputs()
    content = request.model_dump_json()
    execution.store.record_agent_task_contract(
        execution.operation_id,
        ARTIFACT_EDIT_FINALIZATION_ROLE,
        content,
        hashlib.sha256(content.encode()).hexdigest(),
    )
    _record_agent_launch_receipt(
        execution,
        request,
        prompt=prompt,
        contract_path=contract_path,
        remote=bool(remote),
        resumed=bool(request.session_id),
        continuation=execution.continuation,
        extra={"capability": "discuss", "graph_authority": "none"},
    )
    outcome = _ProviderOutcome(session_id=request.session_id)
    async with aclosing(
        _stream_agent_events(
            launcher,
            request,
            prompt,
            workspace=workspace,
            session_id=request.session_id,
            read_dirs=[],
            write_dirs=[],
            write_scope=None,
            execution_host=machine.host,
            execution=execution,
            remote_stage=remote,
            capability="discuss",
            outcome=outcome,
            binary=machine.provider_paths.get(request.provider),
            supervise_remote=bool(remote),
            required_session_id=request.session_id,
        )
    ) as stream:
        async for frame in stream:
            yield frame
    if not outcome.remote_result_pending:
        for frame in _settle(
            service, request, execution, workspace, remote, Path(str(directory)), outcome
        ):
            yield frame


async def finalize_recorded_artifact_edit_result(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    execution: AgentTaskExecution,
    recorded: RecordedProviderTurn,
) -> AsyncIterator[str]:
    del launcher
    content = execution.store.agent_task_contract(
        execution.operation_id, ARTIFACT_EDIT_FINALIZATION_ROLE
    )
    if content is None:
        raise ValueError("The edit's finalization binding is unavailable.")
    admitted = RunRequest.model_validate_json(content)
    if admitted != request:
        raise ValueError("The edit's finalization binding changed.")
    _, remote, workspace, _ = _open_stage(service, request, data_dir, execution)
    assert request.artifact_edit is not None
    directory = workspace / "turns" / request.artifact_edit.staged_scope_id / "artifacts"
    outcome = _ProviderOutcome(session_id=request.session_id)
    verdict = decode_recorded_turn(recorded, provider_turn_request(workspace, recorded))
    for frame in absorb_recorded_events(outcome, verdict):
        yield frame
    for frame in _settle(service, request, execution, workspace, remote, directory, outcome):
        yield frame
