from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress
from pathlib import Path, PurePosixPath

from rcp.agents import AgentEvent, AgentLauncher, PromptFactory
from rcp.background import AgentTaskExecution
from rcp.config import AgentSurface
from rcp.history import ReplayHalted
from rcp.runs.chat import (
    _append_chat_exchange,
    _chat_read_dirs,
    _chat_stage_name,
    _clear_stale_patch,
    _discover_chat_artifacts,
    _logical_chat_turn_operation_id,
    _prepare_local_artifact_directory,
    _read_chat_patch,
    _record_artifact_discovery_receipt,
    _record_chat_context_receipt,
    _validated_local_chat_resume_stage,
    _validated_remote_chat_resume_stage,
)
from rcp.runs.shared import (
    _parent_task_contract_path,
    _pinned_to_profile,
    _ProviderOutcome,
    _record_agent_launch_receipt,
    _sse,
    _stage_context_paths,
    _stage_json_task_input,
    _stage_task_contract,
    _stage_task_input,
    _stream_agent_events,
    _swept_stage_root,
    _task_token,
)
from rcp.service import ProjectService, RunRequest
from rcp.skills.staging import stage_skill_selection
from rcp.transport import RemoteRunStage, StateUnavailable


async def stream_discuss_run(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    execution: AgentTaskExecution | None = None,
) -> AsyncIterator[str]:
    """Run one Discuss turn over graph, node, request, and repository context."""
    continuation = execution.continuation if execution is not None else "fresh"
    reusing_checkpoint = bool(execution is not None and execution.reuses_native_checkpoint)
    resuming = continuation == "resume"
    retrying = continuation == "retry"
    retry_attempt = continuation in {"retry", "handoff"}
    surface: AgentSurface = "project_chat" if request.chat_scope == "project" else "node_chat"
    try:
        profile = service.resolve_agent_profile(
            surface,
            provider=request.provider,
            model=request.model,
            reasoning=request.reasoning,
            run_on=request.run_on,
        )
    except ValueError as exc:
        yield _sse(AgentEvent(event="error", text=str(exc)))
        return
    request = _pinned_to_profile(request, profile)
    local_stage: Path | None = None
    execution_machine = service.manifest.machine_map[profile.run_on]
    execution_host = execution_machine.host
    provider_binary = execution_machine.provider_paths.get(profile.provider)
    remote_stage: RemoteRunStage | None = None
    artifact_scope_id: str | None = None
    artifact_directory: Path | PurePosixPath | None = None
    outcome = _ProviderOutcome(session_id=request.session_id)
    try:
        try:
            context = service.assemble_chat(request)
            _record_chat_context_receipt(execution, context, surface=surface)
            # One scratch folder per conversation, not per turn. Resuming a native
            # session means resuming it in the directory it was given — Claude keys
            # its sessions by that directory — so every turn of a chat, local or
            # remote, reuses the same folder and _sweep_stale_stages ages it out.
            stage_name = _chat_stage_name(service, request, execution)
            if execution_host:
                if reusing_checkpoint:
                    stage_root = _validated_remote_chat_resume_stage(
                        execution, execution_host, stage_name
                    )
                    remote_stage = RemoteRunStage(execution_host).attach(stage_root)
                else:
                    remote_stage = RemoteRunStage(execution_host).open(stage_name, reuse=True)
                assert remote_stage.root is not None
                if execution is not None:
                    execution.checkpoint_stage(execution_host, str(remote_stage.root))
                if not reusing_checkpoint or retrying:
                    context = context.model_copy(
                        update=_stage_context_paths(
                            context, service, remote_stage, execution_machine.alias
                        )
                    )
                workspace = Path(str(remote_stage.workspace))
            else:
                stage_root = _swept_stage_root(data_dir)
                expected_stage = stage_root / stage_name
                if reusing_checkpoint:
                    local_stage = _validated_local_chat_resume_stage(execution, expected_stage)
                else:
                    local_stage = expected_stage
                    local_stage.mkdir(parents=True, exist_ok=True)
                if execution is not None:
                    execution.checkpoint_stage("", str(local_stage))
                workspace = local_stage
            if not reusing_checkpoint:
                # A reused folder must not hand this turn the previous turn's patch.
                _clear_stale_patch(workspace, remote_stage)
            artifact_scope_id = (
                _logical_chat_turn_operation_id(execution.store, execution.operation_id)
                if execution is not None and resuming
                else execution.operation_id
                if execution is not None
                else str(uuid.uuid4())
            )
            if remote_stage is not None:
                artifact_directory = remote_stage.prepare_artifact_directory(
                    artifact_scope_id, reuse=resuming
                )
            else:
                assert local_stage is not None
                artifact_directory = _prepare_local_artifact_directory(
                    local_stage, artifact_scope_id, reuse=resuming
                )

            token = _task_token(execution)
            skill_pointers = stage_skill_selection(
                service.resolve_skill_selection(request),
                local_stage=local_stage,
                remote_stage=remote_stage,
                label=f"rcp-skills-{token}",
            )
            read_dirs = _chat_read_dirs(
                context,
                remote_stage,
                service,
                execution_machine.alias,
            )
            if reusing_checkpoint and not request.session_id:
                raise ValueError(
                    "The continued chat has no native agent session; retry it from a clean "
                    "attempt instead."
                )
            if resuming:
                assert execution is not None
                original_contract_path = _parent_task_contract_path(
                    execution, local_stage, remote_stage
                )
                contract = PromptFactory.continuation_task_contract(
                    original_contract_path=original_contract_path,
                    mode="resume",
                    patch_path=None,
                )
                contract_path, prompt = _stage_task_contract(
                    local_stage,
                    remote_stage,
                    f"task-{token}-resume.md",
                    contract,
                    execution=execution,
                    role="discuss_resume",
                )
            else:
                assert request.message is not None
                human_request_path = _stage_task_input(
                    local_stage,
                    remote_stage,
                    f"task-{token}-human-request.txt",
                    request.message,
                )
                retry_diagnostics_path = (
                    _stage_json_task_input(
                        local_stage,
                        remote_stage,
                        f"task-{token}-retry-diagnostics.json",
                        {"prior_attempt_diagnostics": list(execution.retry_feedback)},
                    )
                    if execution is not None and (execution.retry_feedback or retry_attempt)
                    else None
                )
                contract = PromptFactory.discuss_task_contract(
                    project_name=context.project_name,
                    ontology_path=f"{context.graph_path}#ontology",
                    graph_path=context.graph_path,
                    research_path=context.research_md_path,
                    focused_node_id=str(context.node["id"]) if context.node else None,
                    repositories=[
                        {"alias": item.alias, "host": item.host, "path": item.path}
                        for item in context.repositories
                    ],
                    introduction_path=context.introduction_path,
                    human_request_path=human_request_path,
                    artifact_path=str(artifact_directory),
                    retry_diagnostics_path=retry_diagnostics_path,
                    skill_pointers=skill_pointers,
                )
                current_contract_path, current_prompt = _stage_task_contract(
                    local_stage,
                    remote_stage,
                    f"task-{token}-{'base' if retry_attempt else 'initial'}.md",
                    contract,
                    execution=execution,
                    role="discuss_retry_base" if retry_attempt else "discuss",
                )
                if retrying:
                    assert execution is not None
                    assert retry_diagnostics_path is not None
                    original_contract_path = _parent_task_contract_path(
                        execution, local_stage, remote_stage
                    )
                    retry_contract = PromptFactory.continuation_task_contract(
                        original_contract_path=original_contract_path,
                        current_contract_path=current_contract_path,
                        diagnostics_path=retry_diagnostics_path,
                        mode="retry",
                    )
                    contract_path, prompt = _stage_task_contract(
                        local_stage,
                        remote_stage,
                        f"task-{token}-retry.md",
                        retry_contract,
                        execution=execution,
                        role="discuss_retry",
                    )
                else:
                    contract_path, prompt = current_contract_path, current_prompt
        except (OSError, ReplayHalted, StateUnavailable, ValueError) as exc:
            yield _sse(AgentEvent(event="error", text=str(exc)))
            return

        _record_agent_launch_receipt(
            execution,
            request,
            prompt=prompt,
            contract_path=contract_path,
            remote=bool(execution_host),
            resumed=reusing_checkpoint,
            continuation=continuation,
            extra={
                "surface": surface,
                "mode": "discuss",
                "capability": "discuss",
                "network_access": True,
                "launch_kind": "retry" if retry_attempt else "resume" if resuming else "initial",
                "write_directory_count": 0,
            },
        )
        try:
            async with aclosing(
                _stream_agent_events(
                    launcher,
                    request,
                    prompt,
                    workspace=workspace,
                    session_id=request.session_id,
                    read_dirs=read_dirs,
                    write_dirs=[],
                    execution_host=execution_host,
                    execution=execution,
                    remote_stage=remote_stage,
                    capability="discuss",
                    outcome=outcome,
                    binary=provider_binary,
                )
            ) as stream:
                async for frame in stream:
                    yield frame
        except Exception:
            # Provider launch/runtime exceptions are terminal and Background will
            # offer Retry. Cancellation and process shutdown use BaseException
            # paths and retain the reusable native-session stage for Resume.
            outcome.failed = True
            raise

        # Only a labelled final assistant message is the reply. A provider that
        # emitted none has not answered, and promoting its last trace would show
        # reasoning or tool output to the human as if it were the answer.
        answer = "\n\n".join(item.strip() for item in outcome.answers if item.strip()).strip()
        if not outcome.completed:
            if outcome.failed or outcome.paused:
                return
            outcome.failed = True
            yield _sse(AgentEvent(event="error", text=f"{request.provider} produced no result."))
            return
        if not answer:
            yield _sse(
                AgentEvent(
                    event="error",
                    text=f"{request.provider} finished without answering.",
                )
            )
            return

        assert artifact_scope_id is not None
        assert artifact_directory is not None
        try:
            artifacts = _discover_chat_artifacts(
                execution,
                artifact_scope_id,
                Path(str(artifact_directory)),
                remote_stage,
            )
        except Exception as exc:
            # Preview attachments are optional. Even a programming or storage
            # error in this branch must not take down a labelled chat answer.
            with suppress(Exception):
                _record_artifact_discovery_receipt(
                    execution,
                    attached=0,
                    candidates=0,
                    ignored={"unexpected_error": 1},
                    detail=str(exc),
                )
            artifacts = []
        yield _sse(AgentEvent(event="answer", text=answer))
        for artifact in artifacts:
            yield _sse(AgentEvent(event="artifact", artifact=artifact))

        # Authority to change the graph rides on the human's request. An agent
        # cannot grant it to itself by writing the file, so a stray patch is kept
        # as a receipt and discarded.
        if execution is not None:
            try:
                patch_text = _read_chat_patch(workspace, remote_stage)
            except (OSError, StateUnavailable, ValueError) as exc:
                execution.store.record_agent_task_receipt(
                    execution.operation_id,
                    "discuss_patch_discarded",
                    {
                        "reason": "unreadable",
                        "detail": f"The agent wrote a patch file that could not be read: {exc}"[
                            :400
                        ],
                    },
                    tier="diagnostic",
                )
                execution.store.record_agent_task_event(
                    execution.operation_id,
                    "Discuss wrote an unreadable patch.json; RCP discarded it without "
                    "changing the graph.",
                    level="warning",
                )
            else:
                if patch_text is not None:
                    execution.store.record_agent_task_patch_output(
                        execution.operation_id, patch_text
                    )
                    execution.store.record_agent_task_event(
                        execution.operation_id,
                        "Discuss has no graph authority, so the patch the agent wrote was "
                        "discarded. Switch to Work for a deliberate graph update.",
                        level="warning",
                    )
                    execution.store.record_agent_task_receipt(
                        execution.operation_id,
                        "discuss_patch_discarded",
                        {
                            "reason": "no_graph_authority",
                            "byte_length": len(patch_text.encode("utf-8")),
                        },
                        tier="diagnostic",
                    )

        try:
            _append_chat_exchange(
                service,
                request,
                answer,
                outcome.session_id,
                None,
                execution=execution,
            )
        except (OSError, StateUnavailable, ValueError) as exc:
            if execution is not None:
                execution.store.record_agent_task_event(
                    execution.operation_id,
                    f"The reply was delivered but could not be written to the chat "
                    f"transcript: {exc}",
                    level="warning",
                )
        yield _sse(AgentEvent(event="done"))
    finally:
        # There is no per-turn source cleanup; the reusable native-session stage
        # remains available to the normal stage sweeper.
        pass
