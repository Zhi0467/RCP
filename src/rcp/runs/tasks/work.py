from __future__ import annotations

import asyncio
import hashlib
import json
import posixpath
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rcp.agents import (
    AgentEvent,
    AgentLauncher,
    PromptFactory,
    parse_agent_patch_json,
    prepare_agent_patch,
    validate_agent_patch_shape,
    validate_work_patch,
)
from rcp.agents.command_mailbox import (
    StagedCommandMailbox,
)
from rcp.agents.prompts import (
    CHAT_MASTER_CONTEXT_VERSION,
    invoked_package_pointers,
)
from rcp.agents.write_scope import ProjectWriteScope
from rcp.artifacts import AgentArtifactDescriptor
from rcp.attachments import ChatAttachmentStore
from rcp.background import AgentTaskExecution
from rcp.config import AgentSurface
from rcp.conversation_worktrees import conversation_worktree_context
from rcp.core.authority import AgentProfile
from rcp.core.models import Patch
from rcp.core.operations import CreateProposalsOperation
from rcp.history import ReplayHalted
from rcp.limits import (
    PATCH_CORRECTION_MAX_ROUNDS,
    PATCH_SELF_CHECK_TIMEOUT_SECONDS,
)
from rcp.runs.chat import (
    _append_chat_exchange,
    _append_chat_graph_receipt,
    _chat_read_dirs,
    _chat_stage_name,
    _ChatPatchInputs,
    _clear_stale_turn_handoffs,
    _commit_chat_prompt_state,
    _discover_chat_artifacts,
    _logical_chat_turn_operation_id,
    _prepare_chat_prompt_state,
    _prepare_local_artifact_directory,
    _prepare_local_chat_workspace,
    _project_write_scope,
    _read_chat_patch,
    _read_watch_request,
    _record_applied_graph_revision,
    _record_artifact_discovery_receipt,
    _record_chat_context_receipt,
    _stage_chat_patch_inputs,
    _validated_local_chat_resume_stage,
    _validated_remote_chat_resume_stage,
    finalize_artifact_revision,
    stage_artifact_context,
)
from rcp.runs.experiment_loop import (
    StagedExperimentWatcherResource,
    read_experiment_watcher_outputs,
    stage_chat_experiment_watcher_resources,
)
from rcp.runs.patch_validator import (
    PatchValidationBudget,
    PatchValidationResult,
    serve_patch_validation_mailbox,
    stage_patch_validation_mailbox,
)
from rcp.runs.recorded_settlement import (
    absorb_recorded_events,
    attach_retained_stage,
    provider_turn_request,
    retained_artifact_directory,
)
from rcp.runs.recorded_turn import (
    RecordedProviderTurn,
    decode_recorded_turn,
)
from rcp.runs.shared import (
    _parent_task_contract_path,
    _pinned_to_profile,
    _protected_run_stage_roots,
    _ProviderOutcome,
    _record_agent_launch_receipt,
    _retry_deliverable_is_unchanged,
    _sse,
    _stage_context_paths,
    _stage_json_task_input,
    _stage_task_contract,
    _stage_task_input,
    _swept_stage_root,
    _task_token,
)
from rcp.runs.tasks.compute_commands import WorkComputeCommands
from rcp.runs.tasks.experiment_watcher_maintenance import (
    _process_experiment_watcher_maintenance,
)
from rcp.runs.tasks.result_views import (
    ResultViewSnapshot,
    _finalize_result_view_turn,
    _preflight_result_view_revision,
    _prepare_result_view_turn,
    _PreparedResultView,
    _record_result_view_rejection,
    _roll_result_view_retention,
)
from rcp.runs.tasks.work_turn_runtime import (
    WorkFinalizationContext,
    WorkTurn,
    _AppliedWorkTurn,
    _ComposedWorkPrompt,
    _CorrectionPatchRead,
    _DeliverableFailure,
    _DeliverableRead,
    _DeliverableStep,
    _PreparedWorkPatch,
    _ResolvedWorkExecution,
    _RetryDeliverableBaseline,
    _SettledWorkDeliverables,
    _StagedWorkInputs,
    _WorkValidatorMailboxLifecycle,
    apply_work_patch,
    read_correction_patch,
    settle_graph_repair_patch,
    start_work_validator_mailbox,
    validate_work_patch_live,
)
from rcp.runs.tasks.work_turn_runtime import (
    clears_stale_turn_handoffs as _clears_stale_turn_handoffs,
)
from rcp.runs.tasks.work_turn_runtime import (
    close_work_validator_mailbox as _close_work_validator_mailbox,
)
from rcp.runs.tasks.work_turn_runtime import (
    stream_turn_agent_events as _stream_turn_agent_events,
)
from rcp.service import GraphUpdateResult, ProjectService, RunRequest
from rcp.skill_registry import SkillSelection
from rcp.skills.staging import skill_bundle_label, stage_skill_selection
from rcp.storage import ExperimentWatcherResourceRecord, ResultViewRecord, WatcherContinuation
from rcp.transport import RemoteRunStage, RunLockCancelled, StateUnavailable
from rcp.watchers import (
    WatcherBinding,
    WatcherInitialCheckError,
    arm_watchers,
    parse_watch_json,
)

# Auto-research shares the same patch-failure value while its orchestration remains separate.
_WorkPatchFailure = _DeliverableFailure

WORK_FINALIZATION_CONTEXT_ROLE = "work_finalization_context"
_WORK_PRIMARY_ANSWER_ROLE = "work_primary_answer"


class _StoredResultViewFinalization(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["create", "revise"]
    view_id: str
    prompt_path: str
    origin_operation_id: str | None = None
    record: ResultViewRecord | None = None
    before_name: str | None = None
    before_size: int | None = Field(default=None, ge=0)
    before_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class _StoredExperimentFinalizationResource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    resource: ExperimentWatcherResourceRecord
    watcher_state_path: str
    watch_path: str


class _StoredWorkFinalizationContext(BaseModel):
    """Immutable launch snapshot consumed only after the provider has stopped."""

    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = 1
    request: RunRequest
    stage_host: str
    stage_root: str
    workspace: str
    run_truth_scope: list[str]
    write_scope: ProjectWriteScope
    artifact_scope_id: str
    artifact_directory: str
    result_view: _StoredResultViewFinalization | None = None
    experiment_resources: list[_StoredExperimentFinalizationResource]
    skill_selection: SkillSelection
    compute_commands: bool


def _read_correction_patch(
    workspace: Path,
    remote_stage: RemoteRunStage | None,
) -> _CorrectionPatchRead:
    return read_correction_patch(lambda: _read_chat_patch(workspace, remote_stage))


def _work_patch_source_operation_id(
    execution: AgentTaskExecution | None,
) -> str | None:
    if execution is None:
        return None
    return execution.operation_id


def _prepare_work_chat_prompt(
    execution: AgentTaskExecution | None,
    request: RunRequest,
    *,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    artifact_path: str,
    master_context: str,
    stable_values: dict[str, object],
    skill_pointers: list[dict[str, object]],
    attachment_pointers: list[dict[str, object]],
    result_view: _PreparedResultView | None,
    write_scope: ProjectWriteScope,
    execution_instructions: str,
) -> tuple[str, str]:
    """Prepare the provisional session baseline behind one Work-local seam."""

    if request.message is None:
        raise ValueError("An ordinary Work turn requires a human message.")
    bootstrap_path, context_delta, retained_master_path = _prepare_chat_prompt_state(
        execution,
        request,
        local_stage=local_stage,
        remote_stage=remote_stage,
        master_context=master_context,
        contract_key=f"chat-master-v{CHAT_MASTER_CONTEXT_VERSION}",
        values=stable_values,
    )
    execution_instructions_path = _stage_task_input(
        local_stage,
        remote_stage,
        f"task-{_task_token(execution)}-execution.md",
        execution_instructions,
    )
    prompt = PromptFactory.work_turn_prompt(
        artifact_path=artifact_path,
        human_message=request.message,
        master_context_path=retained_master_path,
        bootstrap_master_context=bootstrap_path is not None,
        context_delta=context_delta,
        invoked_skill_pointers=invoked_package_pointers(
            skill_pointers,
            workflow_ids=request.invoked_workflow_ids,
            skill_ids=request.invoked_skill_ids,
        ),
        invoked_provider_skills=request.resolved_provider_skills,
        attachments=attachment_pointers,
        result_view_action=result_view.action if result_view is not None else None,
        result_view_path=result_view.prompt_path if result_view is not None else None,
        write_scope=write_scope,
        execution_instructions_path=posixpath.relpath(
            execution_instructions_path, write_scope.workspace_root
        ),
    )
    return prompt, retained_master_path


def _work_execution_instructions(turn: WorkTurn) -> str:
    if turn.compute_commands is None:
        return ""
    command = turn.patch_inputs.validator_staged.client_command(
        "launch", "--key", "<idempotency-key>", "--cwd", "<working-directory>", "--", "<argv...>"
    )
    return turn.compute_commands.execution_instructions(command)


def _resolve_work_execution(
    service: ProjectService,
    request: RunRequest,
    execution: AgentTaskExecution | None,
) -> _ResolvedWorkExecution:
    surface: AgentSurface = "project_chat" if request.chat_scope == "project" else "node_chat"
    profile = service.resolve_agent_profile(
        surface,
        provider=request.provider,
        model=request.model,
        reasoning=request.reasoning,
        run_on=request.run_on,
    )
    request = _pinned_to_profile(request, profile)
    revision_preflight = _preflight_result_view_revision(request, execution)
    execution_machine = service.manifest.machine_map[profile.run_on]
    return _ResolvedWorkExecution(
        request=request,
        execution_machine_alias=execution_machine.alias,
        execution_host=execution_machine.host,
        provider_binary=execution_machine.provider_paths.get(profile.provider),
        revision_preflight=revision_preflight,
    )


def _stored_result_view(
    prepared: _PreparedResultView | None,
) -> _StoredResultViewFinalization | None:
    if prepared is None:
        return None
    before = prepared.before
    return _StoredResultViewFinalization(
        action=prepared.action,
        view_id=prepared.view_id,
        prompt_path=prepared.prompt_path,
        origin_operation_id=prepared.origin_operation_id,
        record=prepared.record,
        before_name=before.name if before is not None else None,
        before_size=before.size if before is not None else None,
        before_sha256=before.sha256 if before is not None else None,
    )


def _prepared_result_view(
    stored: _StoredResultViewFinalization | None,
) -> _PreparedResultView | None:
    if stored is None:
        return None
    before_values = (stored.before_name, stored.before_size, stored.before_sha256)
    if any(value is not None for value in before_values) and not all(
        value is not None for value in before_values
    ):
        raise ValueError("The retained result-view finalization snapshot is incomplete.")
    before = (
        ResultViewSnapshot(
            name=stored.before_name,
            size=stored.before_size,
            sha256=stored.before_sha256,
            # Finalization compares identity, size and digest. The launch-time
            # bytes remain in the result-view store and need not be duplicated
            # in this immutable task contract.
            data=b"",
        )
        if stored.before_name is not None
        and stored.before_size is not None
        and stored.before_sha256 is not None
        else None
    )
    return _PreparedResultView(
        action=stored.action,
        view_id=stored.view_id,
        prompt_path=stored.prompt_path,
        origin_operation_id=stored.origin_operation_id,
        record=stored.record,
        before=before,
    )


def _work_finalization_context(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
) -> WorkFinalizationContext:
    return WorkFinalizationContext(
        service=turn.service,
        request=turn.request,
        execution=turn.execution,
        run_truth_scope=list(turn.context.run_truth_scope),
        workspace=turn.workspace,
        local_stage=turn.local_stage,
        remote_stage=turn.remote_stage,
        execution_host=turn.execution_host,
        write_scope=turn.write_scope,
        outcome=turn.outcome,
        artifact_scope_id=staged.artifact_scope_id,
        artifact_directory=staged.artifact_directory,
        prepared_result_view=staged.prepared_result_view,
        experiment_resources=list(staged.experiment_resources),
        skill_selection=staged.skill_selection,
        compute_commands=turn.compute_commands,
        answer=turn.answer,
    )


def _record_work_finalization_context(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
) -> None:
    execution = turn.execution
    if execution is None:
        return
    if execution.stage_root is None:
        raise ValueError("A durable Work finalization context requires its exact task stage.")
    stored = _StoredWorkFinalizationContext(
        request=turn.request,
        stage_host=execution.stage_host or "",
        stage_root=execution.stage_root,
        workspace=str(turn.workspace),
        run_truth_scope=list(turn.context.run_truth_scope),
        write_scope=turn.write_scope,
        artifact_scope_id=staged.artifact_scope_id,
        artifact_directory=str(staged.artifact_directory),
        result_view=_stored_result_view(staged.prepared_result_view),
        experiment_resources=[
            _StoredExperimentFinalizationResource(
                resource=item.resource,
                watcher_state_path=item.watcher_state_path,
                watch_path=item.watch_path,
            )
            for item in staged.experiment_resources
        ],
        skill_selection=staged.skill_selection,
        compute_commands=turn.compute_commands is not None,
    )
    content = stored.model_dump_json()
    execution.store.record_agent_task_contract(
        execution.operation_id,
        WORK_FINALIZATION_CONTEXT_ROLE,
        content,
        hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


def _load_work_finalization_context(
    service: ProjectService,
    request: RunRequest,
    execution: AgentTaskExecution,
) -> WorkFinalizationContext:
    """Reopen finalization without rerunning any launch preparation."""

    content = execution.store.agent_task_contract(
        execution.operation_id,
        WORK_FINALIZATION_CONTEXT_ROLE,
    )
    if content is None:
        raise ValueError("The Work turn has no retained finalization context.")
    try:
        stored = _StoredWorkFinalizationContext.model_validate_json(content)
    except ValueError as exc:
        raise ValueError("The retained Work finalization context is invalid.") from exc
    task = execution.store.agent_task(execution.operation_id)
    if task is None or task.request != request.model_dump(mode="json"):
        raise ValueError("The Work finalization request does not match its durable task.")
    if execution.write_scope_fingerprint != stored.write_scope.fingerprint:
        raise ValueError("The retained Work finalization write scope changed after launch.")
    local_stage, remote_stage, workspace = attach_retained_stage(
        execution,
        owner="Work",
        stage_host=stored.stage_host,
        stage_root=stored.stage_root,
        workspace=stored.workspace,
    )
    # Work alone binds a write scope, so only Work can check that the scope it
    # enforced names the workspace this turn actually ran in.
    if stored.write_scope.workspace_root != stored.workspace:
        raise ValueError("The retained Work workspace changed after launch.")
    artifact_directory = retained_artifact_directory(
        workspace,
        stored.artifact_scope_id,
        stored.artifact_directory,
        owner="Work",
    )

    experiment_resources = [
        StagedExperimentWatcherResource(
            resource=item.resource,
            watcher_state_path=item.watcher_state_path,
            watch_path=item.watch_path,
        )
        for item in stored.experiment_resources
    ]
    compute_commands = (
        WorkComputeCommands(
            execution,
            service.manifest,
            stored.write_scope,
            remote_stage,
            stored.request.control_episode_id,
        )
        if stored.compute_commands
        else None
    )
    return WorkFinalizationContext(
        service=service,
        request=stored.request,
        execution=execution,
        run_truth_scope=list(stored.run_truth_scope),
        workspace=workspace,
        local_stage=local_stage,
        remote_stage=remote_stage,
        execution_host=stored.stage_host,
        write_scope=stored.write_scope,
        outcome=_ProviderOutcome(session_id=stored.request.session_id),
        artifact_scope_id=stored.artifact_scope_id,
        artifact_directory=artifact_directory,
        prepared_result_view=_prepared_result_view(stored.result_view),
        experiment_resources=experiment_resources,
        skill_selection=stored.skill_selection,
        compute_commands=compute_commands,
    )


async def _stage_work_turn(
    service: ProjectService,
    resolved: _ResolvedWorkExecution,
    data_dir: Path,
    execution: AgentTaskExecution | None,
) -> tuple[WorkTurn, _StagedWorkInputs]:
    """Build the launch context for one Work provider pass."""

    request = resolved.request
    continuation = execution.continuation if execution is not None else "fresh"
    clear_stale_handoffs = _clears_stale_turn_handoffs(continuation)
    resuming = continuation == "resume"
    local_stage: Path | None = None
    remote_stage: RemoteRunStage | None = None
    patch_inputs: _ChatPatchInputs | None = None
    validator_lifecycle: _WorkValidatorMailboxLifecycle | None = None
    validator_budget = PatchValidationBudget()
    outcome = _ProviderOutcome(session_id=request.session_id)
    try:
        context = service.assemble_chat(request)
        if execution is not None:
            task = execution.store.agent_task(execution.operation_id)
            if task is None:
                raise ValueError("The conversation task binding is unavailable.")
            context = conversation_worktree_context(
                service,
                execution.store,
                task.project_id,
                request,
                context,
                resuming_integration=execution.continuation in {"resume", "retry", "handoff"},
            )
        surface: AgentSurface = "project_chat" if request.chat_scope == "project" else "node_chat"
        _record_chat_context_receipt(execution, context, surface=surface)
        stage_name = _chat_stage_name(service, request, execution)
        saved_stage = execution is not None and execution.stage_root is not None
        if resolved.execution_host:
            if saved_stage:
                stage_root = _validated_remote_chat_resume_stage(
                    execution,
                    resolved.execution_host,
                    stage_name,
                )
                remote_stage = RemoteRunStage(resolved.execution_host).attach(stage_root)
            else:
                remote_stage = RemoteRunStage(resolved.execution_host).open(
                    stage_name,
                    reuse=True,
                    protected_roots=_protected_run_stage_roots(
                        execution.store if execution is not None else None,
                        resolved.execution_host,
                    ),
                )
            assert remote_stage.root is not None
            if execution is not None:
                execution.checkpoint_stage(resolved.execution_host, str(remote_stage.root))
            context = context.model_copy(
                update=_stage_context_paths(
                    context,
                    service,
                    remote_stage,
                    resolved.execution_machine_alias,
                )
            )
            workspace = Path(str(remote_stage.workspace))
        else:
            stage_root = _swept_stage_root(
                data_dir,
                store=execution.store if execution is not None else None,
            )
            expected_stage = stage_root / stage_name
            if saved_stage:
                local_stage = _validated_local_chat_resume_stage(execution, expected_stage)
            else:
                local_stage = expected_stage
                local_stage.mkdir(parents=True, exist_ok=True)
            workspace = _prepare_local_chat_workspace(
                local_stage,
                execution=execution,
                saved_stage=saved_stage,
            )
            if execution is not None:
                execution.checkpoint_stage("", str(local_stage))
        _roll_result_view_retention(request, execution, local_stage, remote_stage)
        token = _task_token(execution)
        patch_inputs = _stage_chat_patch_inputs(
            local_stage,
            remote_stage,
            workspace=workspace,
            stage_name=stage_name,
            task_id=execution.operation_id if execution is not None else token,
            turn_id=f"{token}:work",
            broker=True,
            episode_id=request.control_episode_id,
        )
        if clear_stale_handoffs:
            _clear_stale_turn_handoffs(workspace, remote_stage)
        artifact_scope_id = (
            _logical_chat_turn_operation_id(execution.store, execution.operation_id)
            if execution is not None and resuming
            else execution.operation_id
            if execution is not None
            else str(uuid.uuid4())
        )
        prepared_result_view = _prepare_result_view_turn(
            request,
            execution,
            workspace if remote_stage is None else None,
            remote_stage,
            focused_node=context.node,
            logical_operation_id=artifact_scope_id,
            revision_preflight=resolved.revision_preflight,
        )
        if remote_stage is not None:
            artifact_directory: Path | PurePosixPath = remote_stage.prepare_artifact_directory(
                artifact_scope_id,
                reuse=resuming,
            )
        else:
            assert local_stage is not None
            artifact_directory = _prepare_local_artifact_directory(
                workspace,
                artifact_scope_id,
                reuse=resuming,
            )
        artifact_context = stage_artifact_context(
            service,
            request,
            execution,
            local_stage=local_stage,
            remote_stage=remote_stage,
            artifact_path=str(artifact_directory),
        )
        read_dirs = _chat_read_dirs(
            context,
            local_stage,
            remote_stage,
            service,
            resolved.execution_machine_alias,
        )
        write_scope = _project_write_scope(
            context,
            service,
            resolved.execution_machine_alias,
            local_stage=local_stage,
            workspace=workspace,
            remote_stage=remote_stage,
            data_dir=data_dir,
            execution=execution,
            capability="work_auto",
            additional_protected_write_paths=(
                list(artifact_context.protected_write_paths)
                if artifact_context is not None
                else None
            ),
        )
        compute_commands = (
            WorkComputeCommands(
                execution, service.manifest, write_scope, remote_stage, request.control_episode_id
            )
            if execution is not None
            else None
        )
        validator_lifecycle = _start_work_validator_mailbox(
            service,
            patch_inputs.validator_staged,
            execution=execution,
            budget=validator_budget,
            compute_commands=compute_commands,
            run_truth_scope=context.run_truth_scope,
        )
        write_dirs = [Path(item) for item in write_scope.repository_roots]
        experiment_resources = await stage_chat_experiment_watcher_resources(
            request,
            execution,
            local_stage,
            remote_stage,
            workspace=workspace,
            token=token,
            clear_stale=clear_stale_handoffs,
        )
        experiment_resource_pointers = [item.prompt_value() for item in experiment_resources]
        skill_selection = service.resolve_skill_selection(request)
        skill_pointers = stage_skill_selection(
            skill_selection,
            local_stage=local_stage,
            remote_stage=remote_stage,
            label=skill_bundle_label(skill_selection),
            reuse_existing=True,
        )
        if bool(request.attachment_batch_id) != bool(request.attachments):
            raise ValueError("The chat task has incomplete attachment batch metadata.")
        attachment_pointers = (
            ChatAttachmentStore(data_dir / "chat-attachments").stage(
                request.attachment_batch_id,
                request.attachments,
                local_stage=local_stage,
                remote_stage=remote_stage,
            )
            if request.attachment_batch_id
            else []
        )
        if artifact_context is not None:
            attachment_pointers.append(artifact_context.pointer)
        read_dirs.extend(
            path
            for path in dict.fromkeys(
                Path(str(item["path"])).parent for item in attachment_pointers
            )
            if path not in read_dirs
        )
        repositories = [
            {"alias": item.alias, "host": item.host, "path": item.path}
            for item in context.repositories
        ]
        turn = WorkTurn(
            service=service,
            request=request,
            execution=execution,
            context=context,
            workspace=workspace,
            local_stage=local_stage,
            remote_stage=remote_stage,
            execution_host=resolved.execution_host,
            provider_binary=resolved.provider_binary,
            read_dirs=read_dirs,
            write_dirs=write_dirs,
            write_scope=write_scope,
            patch_inputs=patch_inputs,
            validator_lifecycle=validator_lifecycle,
            validator_budget=validator_budget,
            compute_commands=compute_commands,
            outcome=outcome,
        )
        staged = _StagedWorkInputs(
            token=token,
            artifact_scope_id=artifact_scope_id,
            artifact_directory=artifact_directory,
            prepared_result_view=prepared_result_view,
            experiment_resources=experiment_resources,
            experiment_resource_pointers=experiment_resource_pointers,
            skill_selection=skill_selection,
            skill_pointers=skill_pointers,
            attachment_pointers=attachment_pointers,
            repositories=repositories,
        )
        return turn, staged
    except BaseException as exc:
        if validator_lifecycle is not None:
            await validator_lifecycle.close(primary_error=exc)
        elif patch_inputs is not None and not patch_inputs.validator_staged.credential.expired:
            await _close_work_validator_mailbox(
                patch_inputs.validator_staged,
                stop=None,
                task=None,
                execution=execution,
                primary_error=exc,
            )
        raise


async def _prepare_work_prompt_context(
    turn: WorkTurn,
    _staged: _StagedWorkInputs,
) -> None:
    if turn.reusing_checkpoint and not turn.request.session_id:
        raise ValueError(
            "The continued Work turn has no native agent session; retry it from a clean attempt "
            "instead."
        )


def _stage_retry_diagnostics(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
) -> str | None:
    if turn.execution is None or not (turn.execution.retry_feedback or turn.retry_attempt):
        return None
    return _stage_json_task_input(
        turn.local_stage,
        turn.remote_stage,
        f"task-{staged.token}-retry-diagnostics.json",
        {"prior_attempt_diagnostics": list(turn.execution.retry_feedback)},
    )


def _compose_resume_prompt(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
) -> _ComposedWorkPrompt:
    assert turn.execution is not None
    original_contract_path = _parent_task_contract_path(
        turn.execution,
        turn.local_stage,
        turn.remote_stage,
    )
    current_contract_path = _stage_work_contract(turn, staged)
    contract = PromptFactory.continuation_task_contract(
        original_contract_path=original_contract_path,
        mode="resume",
        turn_mode="work",
        write_scope=turn.write_scope,
        current_contract_path=current_contract_path,
        patch_path=turn.patch_inputs.patch_path,
        watch_path=turn.patch_inputs.watch_path,
        validator_command=turn.patch_inputs.validator_command,
        execution_instructions=_work_execution_instructions(turn),
        invoked_skill_pointers=invoked_package_pointers(
            staged.skill_pointers,
            workflow_ids=turn.request.invoked_workflow_ids,
            skill_ids=turn.request.invoked_skill_ids,
        ),
        invoked_provider_skills=turn.request.resolved_provider_skills,
        result_view_action=(
            staged.prepared_result_view.action if staged.prepared_result_view is not None else None
        ),
        result_view_path=(
            staged.prepared_result_view.prompt_path
            if staged.prepared_result_view is not None
            else None
        ),
    )
    contract_path, prompt = _stage_task_contract(
        turn.local_stage,
        turn.remote_stage,
        f"task-{staged.token}-resume.md",
        contract,
        execution=turn.execution,
        role="work_resume",
    )
    return _ComposedWorkPrompt(
        contract_path=contract_path,
        prompt=prompt,
        base_contract_path=current_contract_path,
    )


def _stage_work_contract(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
    *,
    retry_diagnostics_path: str | None = None,
) -> str:
    """Stage current guidance without resetting the native session's assignment."""

    assert turn.request.message is not None
    focused_node_id = str(turn.context.node["id"]) if turn.context.node else None
    compute_profiles = turn.service.compute_prompt_profiles(turn.request.resolved_compute_context)
    human_request_path = _stage_task_input(
        turn.local_stage,
        turn.remote_stage,
        f"task-{staged.token}-human-request.txt",
        turn.request.message,
    )
    contract = PromptFactory.work_task_contract(
        execution_instructions=_work_execution_instructions(turn),
        project_name=turn.context.project_name,
        ontology_path=f"{turn.context.graph_path}#ontology",
        ontology_extensions=turn.context.ontology_extensions,
        graph_path=turn.context.graph_path,
        research_path=turn.context.research_md_path,
        focused_node_id=focused_node_id,
        repositories=staged.repositories,
        introduction_path=turn.context.introduction_path,
        human_request_path=human_request_path,
        patch_path=turn.patch_inputs.patch_path,
        artifact_path=str(staged.artifact_directory),
        output_schema_path=turn.patch_inputs.schema_path,
        retry_diagnostics_path=retry_diagnostics_path,
        watch_path=turn.patch_inputs.watch_path,
        execution_host=turn.execution_host,
        experiment_watcher_resources=staged.experiment_resource_pointers,
        validator_command=turn.patch_inputs.validator_command,
        write_scope=turn.write_scope,
        skill_pointers=staged.skill_pointers,
        invoked_skill_pointers=invoked_package_pointers(
            staged.skill_pointers,
            workflow_ids=turn.request.invoked_workflow_ids,
            skill_ids=turn.request.invoked_skill_ids,
        ),
        invoked_provider_skills=turn.request.resolved_provider_skills,
        attachments=staged.attachment_pointers,
        compute_connections=compute_profiles,
    )
    contract_path, _ = _stage_task_contract(
        turn.local_stage,
        turn.remote_stage,
        f"task-{staged.token}-{'base' if turn.retry_attempt or turn.reusing_checkpoint else 'initial'}.md",
        contract,
        execution=turn.execution,
        role="work_retry_base" if turn.retry_attempt else "work",
    )
    return contract_path


def _compose_fresh_prompt(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
    *,
    retry_diagnostics_path: str | None = None,
) -> _ComposedWorkPrompt:
    assert turn.request.message is not None
    focused_node_id = str(turn.context.node["id"]) if turn.context.node else None
    compute_profiles = turn.service.compute_prompt_profiles(turn.request.resolved_compute_context)
    if not turn.uses_master_protocol:
        contract_path = _stage_work_contract(
            turn, staged, retry_diagnostics_path=retry_diagnostics_path
        )
        return _ComposedWorkPrompt(
            contract_path=contract_path,
            prompt=PromptFactory.launch_prompt(contract_path),
            base_contract_path=contract_path,
        )

    master_context = PromptFactory.chat_master_context(
        project_name=turn.context.project_name,
        ontology_path=f"{turn.context.graph_path}#ontology",
        ontology_extensions=turn.context.ontology_extensions,
        graph_path=turn.context.graph_path,
        research_path=turn.context.research_md_path,
        graph_revision=turn.context.graph_revision,
        focused_node_id=focused_node_id,
        focused_node=turn.context.node,
        focused_relations=[item.model_dump(mode="json") for item in turn.context.relations],
        repositories=staged.repositories,
        introduction_path=turn.context.introduction_path,
        patch_path=turn.patch_inputs.patch_path,
        workspace_path=str(turn.workspace),
        output_schema_path=turn.patch_inputs.schema_path,
        validator_command=turn.patch_inputs.validator_command,
        watch_path=turn.patch_inputs.watch_path,
        execution_host=turn.execution_host,
        experiment_watcher_resources=staged.experiment_resource_pointers,
        skill_pointers=staged.skill_pointers,
        compute_connections=compute_profiles,
    )
    stable_prompt_values: dict[str, object] = {
        "project": {"name": turn.context.project_name},
        "settings": {
            "provider": turn.request.provider,
            "model": turn.request.model,
            "reasoning": turn.request.reasoning,
            "run_on": turn.request.run_on,
        },
        "current": {
            "ontology_path": f"{turn.context.graph_path}#ontology",
            "graph_revision": turn.context.graph_revision,
            "graph_path": turn.context.graph_path,
            "research_path": turn.context.research_md_path,
            "focused_node_id": focused_node_id,
            "introduction_path": turn.context.introduction_path,
            "experiment_watcher_resources": staged.experiment_resource_pointers,
        },
        "repositories": staged.repositories,
        "compute": {"active": compute_profiles},
        "skills": {"pointers": staged.skill_pointers},
        "patch": {
            "path": turn.patch_inputs.patch_path,
            "watch_path": turn.patch_inputs.watch_path,
            "schema_path": turn.patch_inputs.schema_path,
            "validator_command": turn.patch_inputs.validator_command,
            "validator_mailbox_id": turn.patch_inputs.validator_mailbox_id,
        },
        "workspace": {"path": str(turn.workspace)},
    }
    prompt, retained_master_path = _prepare_work_chat_prompt(
        turn.execution,
        turn.request,
        execution_instructions=_work_execution_instructions(turn),
        local_stage=turn.local_stage,
        remote_stage=turn.remote_stage,
        artifact_path=str(staged.artifact_directory),
        master_context=master_context,
        stable_values=stable_prompt_values,
        skill_pointers=staged.skill_pointers,
        attachment_pointers=staged.attachment_pointers,
        result_view=staged.prepared_result_view,
        write_scope=turn.write_scope,
    )
    return _ComposedWorkPrompt(
        contract_path=retained_master_path,
        prompt=prompt,
        base_contract_path=retained_master_path,
    )


def _compose_retry_prompt(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
) -> _ComposedWorkPrompt:
    assert turn.execution is not None
    retry_diagnostics_path = _stage_retry_diagnostics(turn, staged)
    current_contract_path = _stage_work_contract(
        turn, staged, retry_diagnostics_path=retry_diagnostics_path
    )
    result_view_handoff = bool(
        turn.continuation == "handoff" and staged.prepared_result_view is not None
    )
    original_contract_path = (
        current_contract_path
        if result_view_handoff
        else _parent_task_contract_path(turn.execution, turn.local_stage, turn.remote_stage)
    )
    retry_contract = PromptFactory.continuation_task_contract(
        original_contract_path=original_contract_path,
        current_contract_path=current_contract_path,
        turn_mode="work",
        write_scope=turn.write_scope,
        diagnostics_path=retry_diagnostics_path,
        patch_path=turn.patch_inputs.patch_path,
        watch_path=turn.patch_inputs.watch_path,
        mode="retry",
        validator_command=turn.patch_inputs.validator_command,
        execution_instructions=_work_execution_instructions(turn),
        invoked_skill_pointers=invoked_package_pointers(
            staged.skill_pointers,
            workflow_ids=turn.request.invoked_workflow_ids,
            skill_ids=turn.request.invoked_skill_ids,
        ),
        invoked_provider_skills=turn.request.resolved_provider_skills,
        result_view_action=(
            staged.prepared_result_view.action if staged.prepared_result_view is not None else None
        ),
        result_view_path=(
            staged.prepared_result_view.prompt_path
            if staged.prepared_result_view is not None
            else None
        ),
    )
    contract_path, prompt = _stage_task_contract(
        turn.local_stage,
        turn.remote_stage,
        f"task-{staged.token}-retry.md",
        retry_contract,
        execution=turn.execution,
        role="work_retry",
    )
    return _ComposedWorkPrompt(
        contract_path=contract_path,
        prompt=prompt,
        base_contract_path=current_contract_path,
    )


def _capture_retry_deliverable_baseline(turn: WorkTurn) -> _RetryDeliverableBaseline:
    if not turn.retrying:
        return _RetryDeliverableBaseline(None, None, {})
    assert turn.execution is not None
    predecessor_patch = _read_chat_patch(turn.workspace, turn.remote_stage)
    predecessor_watch = _read_watch_request(turn.workspace, turn.remote_stage)
    experiment_watch_digests = {
        name: hashlib.sha256(text.encode("utf-8")).hexdigest()
        for name, text in read_experiment_watcher_outputs(
            turn.workspace,
            turn.remote_stage,
        ).items()
    }
    patch_digest = (
        hashlib.sha256(predecessor_patch.encode("utf-8")).hexdigest()
        if predecessor_patch is not None
        else None
    )
    watch_digest = (
        hashlib.sha256(predecessor_watch.encode("utf-8")).hexdigest()
        if predecessor_watch is not None
        else None
    )
    turn.execution.store.record_agent_task_receipt(
        turn.execution.operation_id,
        "retry_deliverable_baseline",
        {
            "patch_sha256": patch_digest,
            "watch_sha256": watch_digest,
            "experiment_watch_sha256": experiment_watch_digests,
        },
        tier="diagnostic",
    )
    return _RetryDeliverableBaseline(
        patch_digest=patch_digest,
        watch_digest=watch_digest,
        experiment_watch_digests=experiment_watch_digests,
    )


def _read_initial_patch_deliverable(
    turn: WorkFinalizationContext,
    predecessor_digest: str | None,
    settled: _SettledWorkDeliverables,
) -> _DeliverableRead:
    try:
        text = _read_chat_patch(turn.workspace, turn.remote_stage)
    except (OSError, StateUnavailable, ValueError) as exc:
        text = None
        failure = _DeliverableFailure(
            f"The agent wrote a patch file that could not be read: {exc}",
            correctable=False,
        )
    else:
        failure = None
    if _retry_deliverable_is_unchanged(
        turn.execution,
        filename="patch.json",
        predecessor_digest=predecessor_digest,
        current_text=text,
    ):
        text = None
    if text is None and failure is None:
        settled.graph_update = GraphUpdateResult(status="none")
    return _DeliverableRead(text=text, failure=failure)


def _read_initial_watch_deliverable(
    turn: WorkFinalizationContext,
    predecessor_digest: str | None,
) -> _DeliverableRead:
    try:
        text = _read_watch_request(turn.workspace, turn.remote_stage)
    except (OSError, StateUnavailable, ValueError) as exc:
        text = None
        failure = _DeliverableFailure(
            f"The watcher request could not be read: {exc}",
            correctable=isinstance(exc, ValueError),
        )
    else:
        failure = None
    if _retry_deliverable_is_unchanged(
        turn.execution,
        filename="watch.json",
        predecessor_digest=predecessor_digest,
        current_text=text,
    ):
        text = None
    return _DeliverableRead(text=text, failure=failure)


async def _validate_patch_deliverable(
    turn: WorkFinalizationContext,
    patch_text: str,
    correction_rounds: int,
    settled: _SettledWorkDeliverables,
) -> _DeliverableStep:
    try:
        result, failure = _apply_work_patch(
            turn.service,
            turn.execution,
            patch_text,
            run_truth_scope=turn.run_truth_scope,
        )
    except RunLockCancelled:
        return _DeliverableStep(
            frames=(
                _sse(
                    AgentEvent(
                        event="paused",
                        text=(
                            "Paused while waiting for canonical state. The operational answer "
                            "and retained patch are preserved."
                        ),
                    )
                ),
            ),
            stop=True,
        )
    if result is not None:
        settled.graph_update = result.model_copy(update={"correction_rounds": correction_rounds})
        return _DeliverableStep()
    assert failure is not None
    return _DeliverableStep(failure=failure)


def _watcher_continuation(
    turn: WorkFinalizationContext,
) -> WatcherContinuation:
    request_values = turn.request.model_dump(mode="json")
    values = {
        name: request_values[name]
        for name in WatcherContinuation.model_fields
        if name in request_values
    }
    values.update(
        provider=turn.request.provider or "",
        run_on=turn.request.run_on or "",
        run_truth_scope=turn.run_truth_scope,
        workflow_ids=turn.skill_selection.workflow_ids,
        skill_ids=turn.skill_selection.skill_ids,
        resolved_skill_packages=turn.skill_selection.resolved_skill_packages,
    )
    return WatcherContinuation.model_validate(values)


def _patch_correction_contract(
    turn: WorkTurn,
    composed: _ComposedWorkPrompt,
    diagnostics_path: str,
    validator_command: str,
) -> str:
    return PromptFactory.continuation_task_contract(
        original_contract_path=composed.base_contract_path,
        mode="work_patch_correction",
        turn_mode="work",
        write_scope=turn.write_scope,
        output_schema_path=turn.patch_inputs.schema_path,
        patch_path=turn.patch_inputs.patch_path,
        diagnostics_path=diagnostics_path,
        validator_command=validator_command,
    )


def _read_corrected_patch_deliverable(
    turn: WorkTurn,
    previous: _DeliverableFailure,
) -> _DeliverableRead:
    corrected = _read_correction_patch(
        turn.workspace,
        turn.remote_stage,
    )
    if corrected.problem == "unreadable":
        message = f"The corrected patch could not be read: {corrected.detail}"
    elif corrected.problem == "missing":
        message = "The correction completed without writing patch.json."
    else:
        assert corrected.text is not None
        return _DeliverableRead(text=corrected.text)
    return _DeliverableRead(
        text=None,
        failure=_DeliverableFailure(
            message,
            correctable=True,
            change_summary=previous.change_summary,
            proposal_ids=previous.proposal_ids,
        ),
    )


def _read_corrected_watch_deliverable(turn: WorkTurn) -> _DeliverableRead:
    try:
        corrected_watch = _read_watch_request(turn.workspace, turn.remote_stage)
    except (OSError, StateUnavailable, ValueError) as exc:
        return _DeliverableRead(
            text=None,
            failure=_DeliverableFailure(
                f"The corrected watcher request could not be read: {exc}",
                correctable=True,
            ),
        )
    if corrected_watch is None:
        return _DeliverableRead(
            text=None,
            failure=_DeliverableFailure(
                "The correction completed without writing watch.json.",
                correctable=True,
            ),
        )
    # Correction validates the resulting Patch/watch handoff, not an output
    # delta. An empty handoff may already be correct while only patch.json changes.
    return _DeliverableRead(text=corrected_watch)


def _reject_patch_deliverable(
    turn: WorkFinalizationContext,
    settled: _SettledWorkDeliverables,
    failure: _DeliverableFailure,
    correction_rounds: int,
) -> _DeliverableStep:
    repairable = _work_graph_repairable(
        turn.execution,
        settled.native_session_id,
        failure,
    )
    settled.graph_update = GraphUpdateResult(
        status="rejected",
        change_summary=list(failure.change_summary),
        proposal_ids=list(failure.proposal_ids),
        validation_messages=_bounded_graph_messages(failure.message),
        correction_rounds=correction_rounds,
        repairable=repairable,
    )
    if turn.execution is None or not turn.execution.store.agent_task_has_receipt(
        turn.execution.operation_id, "work_graph_update_rejected"
    ):
        _record_work_graph_rejection(turn.execution, settled.graph_update)
    return _DeliverableStep()


async def _validate_watch_deliverable(
    turn: WorkFinalizationContext,
    watch_text: str,
    correction_rounds: int,
    settled: _SettledWorkDeliverables,
) -> _DeliverableStep:
    try:
        if turn.execution is None:
            raise ValueError("Watcher arming requires a durable originating operation.")
        origin_task = turn.execution.store.agent_task(turn.execution.operation_id)
        if origin_task is None:
            raise ValueError("The originating Work operation is no longer available.")
        ordinary_handoff = parse_watch_json(watch_text)
        if turn.compute_commands is not None:
            await asyncio.to_thread(
                turn.compute_commands.validate_handoff,
                {item.check_command for item in ordinary_handoff.external},
            )
        specs = ordinary_handoff.external
        graph_conditions = ordinary_handoff.graph
        child_route = turn.execution.store.auto_research_child_work_for_operation(
            turn.execution.operation_id
        )
        binding = WatcherBinding(
            project_id=origin_task.project_id,
            origin_operation_id=turn.execution.operation_id,
            origin_task_kind=turn.surface,
            chat_id=turn.request.chat_id or "",
            node_id=turn.request.node_id,
            episode_id=origin_task.episode_id,
            worker_id=child_route.worker_id if child_route is not None else None,
            graph_target=origin_task.graph_target,
            execution_host=turn.execution_host,
            continuation=_watcher_continuation(turn),
        )
        graph_state = (
            await asyncio.to_thread(turn.service.history.state) if graph_conditions else None
        )
        if graph_conditions:
            turn.execution.armed_graph_watchers = True
        watcher_ids = [
            str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"rcp-work-watcher:{turn.execution.operation_id}:{index}",
                )
            )
            for index in range(len(specs) + len(graph_conditions))
        ]
        armed = await asyncio.to_thread(
            arm_watchers,
            turn.execution.store,
            specs,
            binding,
            graph_conditions=graph_conditions,
            state=graph_state,
            watcher_ids=watcher_ids,
        )
    except WatcherInitialCheckError as exc:
        return _DeliverableStep(failure=_DeliverableFailure(str(exc), correctable=True))
    except ValueError as exc:
        return _DeliverableStep(failure=_DeliverableFailure(str(exc), correctable=True))
    except (OSError, ReplayHalted, StateUnavailable) as exc:
        return _DeliverableStep(failure=_DeliverableFailure(str(exc), correctable=False))

    if not turn.execution.store.agent_task_has_receipt(
        turn.execution.operation_id, "watchers_armed"
    ):
        turn.execution.store.record_agent_task_receipt(
            turn.execution.operation_id,
            "watchers_armed",
            {
                "watcher_ids": [item.watcher_id for item in armed],
                "count": len(armed),
                "correction_rounds": correction_rounds,
            },
        )
    settled.watch_correction_rounds = correction_rounds
    return _DeliverableStep()


def _watch_correction_contract(
    turn: WorkTurn,
    composed: _ComposedWorkPrompt,
    diagnostics_path: str,
    watcher_diagnostic: str,
) -> str:
    return PromptFactory.continuation_task_contract(
        original_contract_path=composed.base_contract_path,
        mode="watch_correction",
        diagnostics_path=diagnostics_path,
        watcher_diagnostic=watcher_diagnostic,
        watch_path=turn.patch_inputs.watch_path,
    )


def _reject_watch_deliverable(
    turn: WorkFinalizationContext,
    settled: _SettledWorkDeliverables,
    failure: _DeliverableFailure,
    correction_rounds: int,
    *,
    required_handoff: bool = False,
) -> _DeliverableStep:
    if turn.execution is not None and not turn.execution.store.agent_task_has_receipt(
        turn.execution.operation_id, "watcher_handoff_rejected"
    ):
        turn.execution.store.record_agent_task_receipt(
            turn.execution.operation_id,
            "watcher_handoff_rejected",
            {
                "problem": failure.message[:1600],
                "correction_rounds": correction_rounds,
            },
            tier="diagnostic",
        )
        turn.execution.store.record_agent_task_event(
            turn.execution.operation_id,
            f"Watcher handoff was not armed: {failure.message}",
            level="warning",
        )
    settled.watch_correction_rounds = correction_rounds
    if required_handoff:
        return _DeliverableStep(
            frames=(
                _sse(
                    AgentEvent(
                        event="error", text=f"Compute watcher handoff failed: {failure.message}"
                    )
                ),
            ),
            stop=True,
        )
    return _DeliverableStep()


async def _settle_patch_deliverable(
    turn: WorkFinalizationContext,
    launcher: AgentLauncher,
    predecessor_digest: str | None,
    settled: _SettledWorkDeliverables,
    *,
    launch_turn: WorkTurn | None = None,
    staged: _StagedWorkInputs | None = None,
    composed: _ComposedWorkPrompt | None = None,
    required_session_id: str | None = None,
    maximum_corrections: int = PATCH_CORRECTION_MAX_ROUNDS,
) -> AsyncIterator[str]:
    initial = _read_initial_patch_deliverable(
        turn,
        predecessor_digest,
        settled,
    )
    text = initial.text
    failure = initial.failure
    if text is None and failure is None:
        return

    correction_rounds = 0
    while True:
        if text is not None:
            step = await _validate_patch_deliverable(
                turn,
                text,
                correction_rounds,
                settled,
            )
            for frame in step.frames:
                yield frame
            if step.stop:
                settled.stop = True
                return
            failure = step.failure
            if failure is None:
                return
        assert failure is not None
        if (
            not failure.correctable
            or correction_rounds >= maximum_corrections
            or not settled.native_session_id
        ):
            step = _reject_patch_deliverable(turn, settled, failure, correction_rounds)
            for frame in step.frames:
                yield frame
            if step.stop:
                settled.stop = True
            return

        correction_rounds += 1
        if launch_turn is None or staged is None or composed is None:
            raise RuntimeError("A Work correction requires its live launch context.")
        if turn.execution is not None:
            turn.execution.store.record_agent_task_receipt(
                turn.execution.operation_id,
                "patch_correction_requested",
                {"round": correction_rounds, "problem": failure.message[:400]},
                tier="diagnostic",
            )
            turn.execution.store.update_agent_task_message(
                turn.execution.operation_id,
                "Correcting graph update.",
                phase="correcting",
                event=True,
            )
        diagnostics_path = _stage_json_task_input(
            launch_turn.local_stage,
            launch_turn.remote_stage,
            f"task-{staged.token}-work-correction-{correction_rounds}.json",
            {"kind": "work", "problem": failure.message},
        )
        correction_validator: StagedCommandMailbox | None = None
        correction_lifecycle: _WorkValidatorMailboxLifecycle | None = None
        try:
            correction_validator = stage_patch_validation_mailbox(
                authority="broker",
                episode_id=launch_turn.request.control_episode_id,
                local_stage=(launch_turn.workspace if launch_turn.remote_stage is None else None),
                remote_stage=launch_turn.remote_stage,
                local_input_stage=(
                    launch_turn.local_stage if launch_turn.remote_stage is None else None
                ),
                task_id=(
                    launch_turn.execution.operation_id
                    if launch_turn.execution is not None
                    else staged.token
                ),
                turn_id=f"{staged.token}:work-patch-correction:{correction_rounds}",
                timeout_seconds=PATCH_SELF_CHECK_TIMEOUT_SECONDS,
            )
            correction_lifecycle = _start_work_validator_mailbox(
                launch_turn.service,
                correction_validator,
                execution=launch_turn.execution,
                budget=launch_turn.validator_budget,
                compute_commands=launch_turn.compute_commands,
                run_truth_scope=turn.run_truth_scope,
            )
            validator_command = correction_validator.client_command(
                "validate",
                launch_turn.patch_inputs.patch_path,
            )
            correction_contract = _patch_correction_contract(
                launch_turn,
                composed,
                diagnostics_path,
                validator_command,
            )
            correction_path, correction_prompt = _stage_task_contract(
                launch_turn.local_stage,
                launch_turn.remote_stage,
                f"task-{staged.token}-work-correction-{correction_rounds}.md",
                correction_contract,
                execution=launch_turn.execution,
                role=f"work_patch_correction_{correction_rounds}",
            )
            _record_agent_launch_receipt(
                launch_turn.execution,
                launch_turn.request,
                prompt=correction_prompt,
                contract_path=correction_path,
                remote=bool(launch_turn.execution_host),
                resumed=True,
                write_scope=launch_turn.write_scope,
                continuation="graph_correction",
                extra={
                    "surface": launch_turn.surface,
                    "mode": "work",
                    "capability": "work_auto",
                    "network_access": True,
                    "launch_kind": "graph_correction",
                    "correction_round": correction_rounds,
                    "write_directory_count": len(launch_turn.write_dirs),
                    "canonical_state_boundary": "prompt_only",
                },
            )
            correction_outcome = _ProviderOutcome(session_id=settled.native_session_id)
            correction_error: str | None = None
            correction_stream = _stream_turn_agent_events(
                launch_turn,
                launcher,
                correction_prompt,
                session_id=settled.native_session_id,
                required_session_id=required_session_id,
                outcome=correction_outcome,
                validator_staged=correction_validator,
                validator_lifecycle=correction_lifecycle,
                supervise_remote=launch_turn.supervise_remote,
            )
        except BaseException as exc:
            if correction_lifecycle is not None:
                await correction_lifecycle.close(primary_error=exc)
            elif correction_validator is not None and not correction_validator.credential.expired:
                await _close_work_validator_mailbox(
                    correction_validator,
                    stop=None,
                    task=None,
                    execution=launch_turn.execution,
                    primary_error=exc,
                )
            raise
        async with aclosing(correction_stream) as stream:
            async for frame in stream:
                event = AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
                if event.event == "error":
                    correction_error = event.text or "Patch correction failed."
                    continue
                yield frame
        settled.native_session_id = correction_outcome.session_id or settled.native_session_id
        if correction_outcome.paused or correction_outcome.remote_result_pending:
            settled.stop = True
            return
        if correction_error or not correction_outcome.completed:
            detail = correction_error or (f"{turn.request.provider} produced no correction result.")
            failure = _DeliverableFailure(
                detail,
                correctable=True,
                change_summary=failure.change_summary,
                proposal_ids=failure.proposal_ids,
            )
            text = None
            correction_rounds = PATCH_CORRECTION_MAX_ROUNDS
            continue
        corrected = _read_corrected_patch_deliverable(
            launch_turn,
            failure,
        )
        text = corrected.text
        failure = corrected.failure


async def _settle_watch_deliverable(
    turn: WorkFinalizationContext,
    launcher: AgentLauncher,
    predecessor_digest: str | None,
    settled: _SettledWorkDeliverables,
    *,
    launch_turn: WorkTurn | None = None,
    staged: _StagedWorkInputs | None = None,
    composed: _ComposedWorkPrompt | None = None,
    maximum_corrections: int = PATCH_CORRECTION_MAX_ROUNDS,
) -> AsyncIterator[str]:
    initial = _read_initial_watch_deliverable(turn, predecessor_digest)
    text = initial.text
    failure = initial.failure
    if text is None and failure is None:
        if turn.compute_commands is not None:
            try:
                await asyncio.to_thread(turn.compute_commands.validate_handoff, set())
            except ValueError as exc:
                failure = _DeliverableFailure(str(exc), correctable=True)
        if failure is None:
            return

    correction_rounds = 0
    while True:
        if text is not None:
            step = await _validate_watch_deliverable(
                turn,
                text,
                correction_rounds,
                settled,
            )
            for frame in step.frames:
                yield frame
            if step.stop:
                settled.stop = True
                return
            failure = step.failure
            if failure is None:
                return
        assert failure is not None
        if (
            not failure.correctable
            or correction_rounds >= maximum_corrections
            or not settled.native_session_id
        ):
            required_handoff = False
            if turn.compute_commands is not None:
                try:
                    await asyncio.to_thread(turn.compute_commands.validate_handoff, set())
                except ValueError:
                    required_handoff = True
            step = _reject_watch_deliverable(
                turn, settled, failure, correction_rounds, required_handoff=required_handoff
            )
            for frame in step.frames:
                yield frame
            if step.stop:
                settled.stop = True
            return

        correction_rounds += 1
        if launch_turn is None or staged is None or composed is None:
            raise RuntimeError("A watcher correction requires its live launch context.")
        assert turn.execution is not None
        turn.execution.store.record_agent_task_receipt(
            turn.execution.operation_id,
            "watcher_correction_requested",
            {"round": correction_rounds, "problem": failure.message[:400]},
            tier="diagnostic",
        )
        turn.execution.store.update_agent_task_message(
            turn.execution.operation_id,
            "Correcting watcher handoff.",
            phase="correcting",
            event=True,
        )
        correction_validator = stage_patch_validation_mailbox(
            local_stage=(launch_turn.workspace if launch_turn.remote_stage is None else None),
            remote_stage=launch_turn.remote_stage,
            local_input_stage=(
                launch_turn.local_stage if launch_turn.remote_stage is None else None
            ),
            task_id=turn.execution.operation_id,
            turn_id=f"{staged.token}:watch-correction:{correction_rounds}",
            timeout_seconds=PATCH_SELF_CHECK_TIMEOUT_SECONDS,
            authority="broker",
            episode_id=turn.request.control_episode_id,
        )
        correction_lifecycle = _start_work_validator_mailbox(
            launch_turn.service,
            correction_validator,
            execution=launch_turn.execution,
            budget=launch_turn.validator_budget,
            compute_commands=launch_turn.compute_commands,
            run_truth_scope=turn.run_truth_scope,
        )
        primary_error: BaseException | None = None
        try:
            diagnostics_path = _stage_json_task_input(
                launch_turn.local_stage,
                launch_turn.remote_stage,
                f"task-{staged.token}-watch-correction-{correction_rounds}.json",
                {
                    "problem": failure.message,
                    "validator_command": correction_validator.client_command(
                        "validate", launch_turn.patch_inputs.patch_path
                    ),
                },
            )
            correction_contract = _watch_correction_contract(
                launch_turn,
                composed,
                diagnostics_path,
                failure.message,
            )
            correction_path, correction_prompt = _stage_task_contract(
                launch_turn.local_stage,
                launch_turn.remote_stage,
                f"task-{staged.token}-watch-correction-{correction_rounds}.md",
                correction_contract,
                execution=launch_turn.execution,
                role=f"watch_correction_{correction_rounds}",
            )
            _record_agent_launch_receipt(
                launch_turn.execution,
                launch_turn.request,
                prompt=correction_prompt,
                contract_path=correction_path,
                remote=bool(launch_turn.execution_host),
                resumed=True,
                write_scope=launch_turn.write_scope,
                continuation="watch_correction",
                extra={
                    "surface": launch_turn.surface,
                    "mode": "work",
                    "capability": "work_auto",
                    "network_access": True,
                    "launch_kind": "watch_correction",
                    "correction_round": correction_rounds,
                    "write_directory_count": len(launch_turn.write_dirs),
                    "canonical_state_boundary": "prompt_only",
                },
            )
            correction_outcome = _ProviderOutcome(session_id=settled.native_session_id)
            correction_error: str | None = None
            correction_stream = _stream_turn_agent_events(
                launch_turn,
                launcher,
                correction_prompt,
                session_id=settled.native_session_id,
                outcome=correction_outcome,
                validator_staged=correction_validator,
                validator_lifecycle=correction_lifecycle,
                supervise_remote=launch_turn.supervise_remote,
            )
            async with aclosing(correction_stream) as stream:
                async for frame in stream:
                    event = AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
                    if event.event == "error":
                        correction_error = event.text or "Watcher correction failed."
                        continue
                    if event.event not in {"answer", "done"}:
                        yield frame
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            await correction_lifecycle.close(primary_error=primary_error)
        settled.native_session_id = correction_outcome.session_id or settled.native_session_id
        if correction_outcome.paused or correction_outcome.remote_result_pending:
            settled.stop = True
            return
        if correction_error or not correction_outcome.completed:
            detail = correction_error or (
                f"{turn.request.provider} produced no watcher correction result."
            )
            failure = _DeliverableFailure(
                detail,
                correctable=True,
                change_summary=failure.change_summary,
                proposal_ids=failure.proposal_ids,
            )
            text = None
            correction_rounds = maximum_corrections
            continue
        corrected = _read_corrected_watch_deliverable(launch_turn)
        text = corrected.text
        failure = corrected.failure


async def _apply_work_turn(
    turn: WorkFinalizationContext,
    launcher: AgentLauncher,
    retry_baseline: _RetryDeliverableBaseline,
    applied: _AppliedWorkTurn,
    *,
    launch_turn: WorkTurn | None = None,
    composed: _ComposedWorkPrompt | None = None,
    maximum_corrections: int = PATCH_CORRECTION_MAX_ROUNDS,
) -> AsyncIterator[str]:
    (
        maintenance_frames,
        native_session_id,
        maintenance_paused,
    ) = await _process_experiment_watcher_maintenance(
        service=turn.service,
        launcher=launcher,
        request=turn.request,
        execution=turn.execution,
        staged_resources=turn.experiment_resources,
        workspace=turn.workspace,
        remote_stage=turn.remote_stage,
        local_stage=turn.local_stage,
        base_contract_path=composed.base_contract_path if composed is not None else "",
        token=_task_token(turn.execution),
        native_session_id=applied.native_session_id,
        read_dirs=launch_turn.read_dirs if launch_turn is not None else [],
        write_dirs=launch_turn.write_dirs if launch_turn is not None else [],
        write_scope=turn.write_scope,
        execution_host=turn.execution_host,
        provider_binary=launch_turn.provider_binary if launch_turn is not None else None,
        retry_output_digests=retry_baseline.experiment_watch_digests,
        maximum_corrections=maximum_corrections,
        supervise_remote=launch_turn.supervise_remote if launch_turn is not None else False,
    )
    applied.native_session_id = native_session_id
    for frame in maintenance_frames:
        yield frame
    if maintenance_paused:
        applied.stop = True


def _finalize_work_turn(
    turn: WorkFinalizationContext,
    answer: str,
    graph_update: GraphUpdateResult,
) -> tuple[str, str]:
    if turn.uses_master_protocol:
        try:
            _record_applied_graph_revision(
                turn.execution,
                turn.request,
                turn.outcome.session_id,
                graph_update.applied_revision,
            )
        except ValueError as exc:
            if turn.execution is not None:
                turn.execution.store.record_agent_task_event(
                    turn.execution.operation_id,
                    "This turn's own revision could not be absorbed into the session "
                    f"baseline; the next turn may re-announce it: {exc}",
                    level="warning",
                )
    try:
        _append_chat_exchange(
            turn.service,
            turn.request,
            answer,
            turn.outcome.session_id,
            graph_update.applied_revision,
            graph_update=graph_update,
            execution=turn.execution,
        )
    except (OSError, StateUnavailable, ValueError) as exc:
        if turn.execution is not None:
            turn.execution.store.record_agent_task_event(
                turn.execution.operation_id,
                f"The reply was delivered but could not be written to the chat transcript: {exc}",
                level="warning",
            )
    payload: dict[str, object] = {
        "graph_update": graph_update.model_dump(mode="json"),
    }
    if graph_update.applied_revision is not None:
        payload["applied_revision"] = graph_update.applied_revision
    return (
        _sse(AgentEvent(event="message", text=json.dumps(payload, separators=(",", ":")))),
        _sse(AgentEvent(event="done")),
    )


async def _launch_and_stream_work_turn(
    turn: WorkTurn,
    finalization: WorkFinalizationContext,
    launcher: AgentLauncher,
    prompt: str,
    contract_path: str,
    staged: _StagedWorkInputs,
    _wake_episode: object | None,
    required_session_id: str | None = None,
    *,
    supervise_remote: bool = False,
) -> AsyncIterator[str]:
    turn.supervise_remote = supervise_remote
    try:
        _record_agent_launch_receipt(
            turn.execution,
            turn.request,
            prompt=prompt,
            contract_path=contract_path,
            remote=bool(turn.execution_host),
            resumed=turn.reusing_checkpoint,
            write_scope=turn.write_scope,
            continuation=turn.continuation,
            extra={
                "surface": turn.surface,
                "mode": "work",
                "capability": "work_auto",
                "network_access": True,
                "launch_kind": (
                    "retry"
                    if turn.retry_attempt
                    else "resume"
                    if turn.resuming
                    else "message_wake"
                    if turn.continuation == "message_wake"
                    else "watcher_wake"
                    if turn.waking
                    else "initial"
                ),
                "write_directory_count": len(turn.write_dirs),
                "canonical_state_boundary": "prompt_only",
            },
        )
    except BaseException as exc:
        await turn.validator_lifecycle.close(primary_error=exc)
        raise
    try:
        try:
            async with aclosing(
                _stream_turn_agent_events(
                    turn,
                    launcher,
                    prompt,
                    session_id=turn.request.session_id,
                    required_session_id=required_session_id,
                    outcome=turn.outcome,
                    supervise_remote=supervise_remote,
                )
            ) as stream:
                async for frame in stream:
                    yield frame
        except Exception:
            turn.outcome.failed = True
            raise

        if turn.outcome.remote_result_pending:
            return
        for frame in _settle_work_outcome(finalization):
            yield frame
    except BaseException as exc:
        if turn.execution is not None and staged.prepared_result_view is not None:
            _record_result_view_rejection(turn.execution, staged.prepared_result_view, str(exc))
        raise
    turn.answer = finalization.answer
    if finalization.answer is not None:
        yield _sse(AgentEvent(event="answer", text=finalization.answer))


def _settle_work_outcome(turn: WorkFinalizationContext) -> list[str]:
    """Read one finished provider outcome into the turn's own settled state.

    The first thing past the provider, and the first thing a recorded result has
    to reach too. Deciding whether the turn produced an answer, binding the
    session that produced it, and publishing the result-view revision are all
    facts about a turn that finished -- not about the link that carried it -- so
    a recovered turn that skipped them would reach `done` unbound and unpublished.

    `turn.answer` stays None when there is nothing to settle, which is how the
    caller knows the rest of the finalization is not owed.
    """

    retained_answer = (
        turn.execution.store.agent_task_contract(
            turn.execution.operation_id, _WORK_PRIMARY_ANSWER_ROLE
        )
        if turn.execution is not None
        else None
    )
    answer = (
        retained_answer
        or "\n\n".join(item.strip() for item in turn.outcome.answers if item.strip()).strip()
    )
    if not turn.outcome.completed:
        if turn.outcome.failed or turn.outcome.paused:
            return []
        turn.outcome.failed = True
        return [
            _sse(AgentEvent(event="error", text=f"{turn.request.provider} produced no result."))
        ]
    if not answer:
        return [
            _sse(
                AgentEvent(
                    event="error", text=f"{turn.request.provider} finished without answering."
                )
            )
        ]
    if turn.uses_master_protocol:
        _commit_chat_prompt_state(turn.execution, turn.request, turn.outcome.session_id)
    _finalize_result_view_turn(
        turn.request,
        turn.execution,
        turn.prepared_result_view,
        turn.workspace if turn.remote_stage is None else None,
        turn.remote_stage,
        native_session_id=turn.outcome.session_id,
    )
    turn.answer = answer
    if turn.execution is not None:
        store = turn.execution.store
        operation_id = turn.execution.operation_id
        if (
            store.agent_task_contract(operation_id, WORK_FINALIZATION_CONTEXT_ROLE) is not None
            and retained_answer is None
        ):
            # Corrections have their own journals, but their prose is not the
            # human reply. Retain the completed primary answer before any starts.
            store.record_agent_task_contract(
                operation_id,
                _WORK_PRIMARY_ANSWER_ROLE,
                answer,
                hashlib.sha256(answer.encode("utf-8")).hexdigest(),
            )
    return []


def _finalize_work_artifacts(
    turn: WorkFinalizationContext,
) -> list[AgentArtifactDescriptor]:
    """Discover outputs only after every provider correction turn has settled."""

    try:
        artifacts = _discover_chat_artifacts(
            turn.execution,
            turn.artifact_scope_id,
            Path(str(turn.artifact_directory)),
            turn.remote_stage,
        )
    except Exception as exc:
        with suppress(Exception):
            _record_artifact_discovery_receipt(
                turn.execution,
                attached=0,
                candidates=0,
                ignored={"unexpected_error": 1},
                detail=str(exc),
            )
        artifacts = []
    return finalize_artifact_revision(
        turn.request,
        turn.execution,
        artifact_scope_id=turn.artifact_scope_id,
        artifact_directory=Path(str(turn.artifact_directory)),
        remote_stage=turn.remote_stage,
        artifacts=artifacts,
    )


async def finalize_work_result(
    turn: WorkFinalizationContext,
    launcher: AgentLauncher,
    retry_baseline: _RetryDeliverableBaseline,
    answer: str,
    *,
    launch_turn: WorkTurn | None = None,
    staged: _StagedWorkInputs | None = None,
    composed: _ComposedWorkPrompt | None = None,
    maximum_corrections: int = PATCH_CORRECTION_MAX_ROUNDS,
) -> AsyncIterator[str]:
    """Turn one finished provider result into this task's durable output.

    Everything past this door reads what the turn already produced; nothing past
    it decides what to ask a provider for. A result delivered over a live link
    and the same result read back off a journal come through here, so what the
    task durably records cannot depend on which link carried it.

    `maximum_corrections` is zero for a recorded result. Correcting a bad
    deliverable means asking the provider again, and the turn whose link dropped
    has nobody left to ask; the owner's existing rejection is the answer instead.
    """

    if maximum_corrections and (launch_turn is None or staged is None or composed is None):
        raise ValueError("Work corrections require a complete live launch context.")

    settled = _SettledWorkDeliverables(native_session_id=turn.outcome.session_id)
    async with aclosing(
        _settle_patch_deliverable(
            turn,
            launcher,
            retry_baseline.patch_digest,
            settled,
            launch_turn=launch_turn,
            staged=staged,
            composed=composed,
            maximum_corrections=maximum_corrections,
        )
    ) as stream:
        async for frame in stream:
            yield frame
    if settled.stop:
        return
    async with aclosing(
        _settle_watch_deliverable(
            turn,
            launcher,
            retry_baseline.watch_digest,
            settled,
            launch_turn=launch_turn,
            staged=staged,
            composed=composed,
            maximum_corrections=maximum_corrections,
        )
    ) as stream:
        async for frame in stream:
            yield frame
    if settled.stop:
        return
    applied = _AppliedWorkTurn(
        graph_update=settled.graph_update,
        native_session_id=settled.native_session_id,
    )
    apply_stream = _apply_work_turn(
        turn,
        launcher,
        retry_baseline,
        applied,
        launch_turn=launch_turn,
        composed=composed,
        maximum_corrections=maximum_corrections,
    )
    async with aclosing(apply_stream) as stream:
        async for frame in stream:
            yield frame
    if applied.stop:
        return

    for artifact in _finalize_work_artifacts(turn):
        yield _sse(AgentEvent(event="artifact", artifact=artifact))

    for frame in _finalize_work_turn(turn, answer, applied.graph_update):
        yield frame


async def stream_work_run(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    execution: AgentTaskExecution | None = None,
) -> AsyncIterator[str]:
    """Run one operational conversation turn with optional graph reflection."""

    if request.patch_kind == "experiment_loop":
        yield _sse(
            AgentEvent(
                event="error",
                text=("Experiment-loop requests must be dispatched to the Experiment task owner."),
            )
        )
        return

    if execution is not None and execution.continuation == "graph_repair":
        async with aclosing(
            _stream_work_graph_repair(
                service,
                launcher,
                request,
                data_dir,
                execution=execution,
            )
        ) as stream:
            async for frame in stream:
                yield frame
        return

    try:
        resolved = _resolve_work_execution(service, request, execution)
    except ValueError as exc:
        yield _sse(AgentEvent(event="error", text=str(exc)))
        return
    request = resolved.request
    turn: WorkTurn | None = None
    patch_inputs = None
    validator_lifecycle: _WorkValidatorMailboxLifecycle | None = None
    try:
        turn, staged = await _stage_work_turn(service, resolved, data_dir, execution)
        patch_inputs = turn.patch_inputs
        validator_lifecycle = turn.validator_lifecycle
        if turn.execution_host:
            _record_work_finalization_context(turn, staged)
        resuming = turn.resuming
        await _prepare_work_prompt_context(turn, staged)
        if resuming:
            composed_prompt = _compose_resume_prompt(turn, staged)
        else:
            result_view_handoff = bool(
                turn.continuation == "handoff" and staged.prepared_result_view is not None
            )
            if turn.retrying or result_view_handoff:
                composed_prompt = _compose_retry_prompt(turn, staged)
            else:
                retry_diagnostics_path = _stage_retry_diagnostics(turn, staged)
                composed_prompt = _compose_fresh_prompt(
                    turn,
                    staged,
                    retry_diagnostics_path=retry_diagnostics_path,
                )
        contract_path = composed_prompt.contract_path
        prompt = composed_prompt.prompt
        retry_baseline = _capture_retry_deliverable_baseline(turn)
    except BaseException as exc:
        if validator_lifecycle is not None:
            await validator_lifecycle.close(primary_error=exc)
        elif patch_inputs is not None and not patch_inputs.validator_staged.credential.expired:
            await _close_work_validator_mailbox(
                patch_inputs.validator_staged,
                stop=None,
                task=None,
                execution=execution,
                primary_error=exc,
            )
        if isinstance(exc, (OSError, ReplayHalted, StateUnavailable, ValueError)):
            yield _sse(AgentEvent(event="error", text=str(exc)))
            return
        raise

    assert turn is not None
    finalization = _work_finalization_context(turn, staged)
    async with aclosing(
        _launch_and_stream_work_turn(
            turn,
            finalization,
            launcher,
            prompt,
            contract_path,
            staged,
            None,
            supervise_remote=bool(turn.execution_host),
        )
    ) as stream:
        async for frame in stream:
            yield frame
    if finalization.answer is None:
        return

    async with aclosing(
        finalize_work_result(
            finalization,
            launcher,
            retry_baseline,
            finalization.answer,
            launch_turn=turn,
            staged=staged,
            composed=composed_prompt,
        )
    ) as stream:
        async for frame in stream:
            yield frame


def _rejected_graph_update_for_repair(execution: AgentTaskExecution) -> GraphUpdateResult:
    """Find the rejected Work result behind a graph-repair recovery chain."""

    record = execution.store.agent_task(execution.operation_id)
    seen: set[str] = set()
    while record is not None and record.parent_operation_id is not None:
        parent_id = record.parent_operation_id
        if parent_id in seen:
            break
        seen.add(parent_id)
        record = execution.store.agent_task(parent_id)
        raw_graph_update = record.result.get("graph_update") if record and record.result else None
        if isinstance(raw_graph_update, dict):
            try:
                graph_update = GraphUpdateResult.model_validate(raw_graph_update)
            except ValueError:
                pass
            else:
                if graph_update.status == "rejected":
                    return graph_update
    raise ValueError("The graph repair has no rejected Work ancestor.")


async def _stream_work_graph_repair(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    *,
    execution: AgentTaskExecution,
) -> AsyncIterator[str]:
    """Repair only a retained Work patch; never repeat the operational turn."""

    surface: AgentSurface = "project_chat" if request.chat_scope == "project" else "node_chat"
    patch_inputs = None
    validator_lifecycle: _WorkValidatorMailboxLifecycle | None = None
    validator_budget = PatchValidationBudget()
    try:
        profile = service.resolve_agent_profile(
            surface,
            provider=request.provider,
            model=request.model,
            reasoning=request.reasoning,
            run_on=request.run_on,
        )
        request = _pinned_to_profile(request, profile)
        execution_machine = service.manifest.machine_map[profile.run_on]
        execution_host = execution_machine.host
        provider_binary = execution_machine.provider_paths.get(profile.provider)
        context = service.assemble_chat(request)
        if execution is not None:
            task = execution.store.agent_task(execution.operation_id)
            if task is None:
                raise ValueError("The conversation task binding is unavailable.")
            context = conversation_worktree_context(
                service,
                execution.store,
                task.project_id,
                request,
                context,
                resuming_integration=execution.continuation in {"resume", "retry", "handoff"},
            )
        stage_name = _chat_stage_name(service, request, execution)
        local_stage: Path | None = None
        remote_stage: RemoteRunStage | None = None
        if execution_host:
            stage_root = _validated_remote_chat_resume_stage(execution, execution_host, stage_name)
            remote_stage = RemoteRunStage(execution_host).attach(stage_root)
            context = context.model_copy(
                update=_stage_context_paths(
                    context,
                    service,
                    remote_stage,
                    execution_machine.alias,
                )
            )
            workspace = Path(str(remote_stage.workspace))
        else:
            expected_stage = _swept_stage_root(data_dir, store=execution.store) / stage_name
            local_stage = _validated_local_chat_resume_stage(execution, expected_stage)
            workspace = _prepare_local_chat_workspace(
                local_stage,
                execution=execution,
                saved_stage=True,
            )
        token = _task_token(execution)
        patch_inputs = _stage_chat_patch_inputs(
            local_stage,
            remote_stage,
            workspace=workspace,
            stage_name=stage_name,
            task_id=execution.operation_id,
            turn_id=f"{token}:work-graph-repair",
            broker=True,
            episode_id=request.control_episode_id,
        )
        validator_lifecycle = _start_work_validator_mailbox(
            service,
            patch_inputs.validator_staged,
            execution=execution,
            budget=validator_budget,
            run_truth_scope=context.run_truth_scope,
        )
        patch_path = patch_inputs.patch_path
        read_dirs = _chat_read_dirs(
            context,
            local_stage,
            remote_stage,
            service,
            execution_machine.alias,
        )
        write_scope = _project_write_scope(
            context,
            service,
            execution_machine.alias,
            local_stage=local_stage,
            workspace=workspace,
            remote_stage=remote_stage,
            data_dir=data_dir,
            execution=execution,
            capability="work_auto",
        )
        write_dirs = [Path(item) for item in write_scope.repository_roots]
        assert validator_lifecycle is not None
        outcome = _ProviderOutcome(session_id=request.session_id)
        turn = WorkTurn(
            service=service,
            request=request,
            execution=execution,
            context=context,
            workspace=workspace,
            local_stage=local_stage,
            remote_stage=remote_stage,
            execution_host=execution_host,
            provider_binary=provider_binary,
            read_dirs=read_dirs,
            write_dirs=write_dirs,
            write_scope=write_scope,
            patch_inputs=patch_inputs,
            validator_lifecycle=validator_lifecycle,
            validator_budget=validator_budget,
            outcome=outcome,
        )
        previous = _rejected_graph_update_for_repair(execution)
        original_contract_path = _parent_task_contract_path(execution, local_stage, remote_stage)
        validator_command = patch_inputs.validator_command
        diagnostics_path = _stage_json_task_input(
            local_stage,
            remote_stage,
            f"task-{token}-manual-graph-repair.json",
            {
                "kind": "work",
                "problems": previous.validation_messages,
                "prior_correction_rounds": previous.correction_rounds,
            },
        )
        contract = PromptFactory.continuation_task_contract(
            original_contract_path=original_contract_path,
            mode="work_patch_correction",
            turn_mode="work",
            write_scope=write_scope,
            output_schema_path=patch_inputs.schema_path,
            patch_path=patch_path,
            diagnostics_path=diagnostics_path,
            validator_command=validator_command,
        )
        contract_path, prompt = _stage_task_contract(
            local_stage,
            remote_stage,
            f"task-{token}-manual-graph-repair.md",
            contract,
            execution=execution,
            role="work_patch_repair",
        )
    except BaseException as exc:
        if validator_lifecycle is not None:
            await validator_lifecycle.close(primary_error=exc)
        elif patch_inputs is not None and not patch_inputs.validator_staged.credential.expired:
            await _close_work_validator_mailbox(
                patch_inputs.validator_staged,
                stop=None,
                task=None,
                execution=execution,
                primary_error=exc,
            )
        if isinstance(exc, (OSError, ReplayHalted, StateUnavailable, ValueError)):
            yield _sse(AgentEvent(event="error", text=str(exc)))
            return
        raise

    assert validator_lifecycle is not None
    try:
        _record_agent_launch_receipt(
            execution,
            request,
            prompt=prompt,
            contract_path=contract_path,
            remote=bool(execution_host),
            resumed=True,
            write_scope=write_scope,
            continuation="graph_repair",
            extra={
                "surface": surface,
                "mode": "work",
                "capability": "work_auto",
                "network_access": True,
                "launch_kind": "graph_repair",
                "write_directory_count": len(write_dirs),
                "canonical_state_boundary": "prompt_only",
            },
        )
    except BaseException as exc:
        await validator_lifecycle.close(primary_error=exc)
        raise
    async with aclosing(
        _stream_turn_agent_events(
            turn,
            launcher,
            prompt,
            session_id=request.session_id,
            outcome=outcome,
        )
    ) as stream:
        async for frame in stream:
            yield frame
    repair = settle_graph_repair_patch(
        outcome,
        provider=request.provider,
        read_patch=lambda: _read_chat_patch(workspace, remote_stage),
        apply_patch=lambda text: _apply_work_patch(
            service,
            execution,
            text,
            run_truth_scope=context.run_truth_scope,
        ),
        bounded_messages=_bounded_graph_messages,
        record_rejection=lambda update: _record_work_graph_rejection(execution, update),
    )
    for frame in repair.frames:
        yield frame
    if repair.graph_update is None:
        return
    graph_update = repair.graph_update
    try:
        _append_chat_graph_receipt(
            service,
            request,
            outcome.session_id,
            graph_update,
            execution,
        )
    except (OSError, StateUnavailable, ValueError) as exc:
        execution.store.record_agent_task_event(
            execution.operation_id,
            f"The graph repair completed but its chat receipt could not be written: {exc}",
            level="warning",
        )
    payload: dict[str, object] = {
        "graph_update": graph_update.model_dump(mode="json"),
    }
    if graph_update.applied_revision is not None:
        payload["applied_revision"] = graph_update.applied_revision
    yield _sse(AgentEvent(event="message", text=json.dumps(payload, separators=(",", ":"))))
    yield _sse(AgentEvent(event="done"))


def _start_work_validator_mailbox(
    service: ProjectService,
    staged: StagedCommandMailbox,
    *,
    execution: AgentTaskExecution | None,
    budget: PatchValidationBudget,
    compute_commands: WorkComputeCommands | None = None,
    run_truth_scope: list[str],
) -> _WorkValidatorMailboxLifecycle:
    return start_work_validator_mailbox(
        staged,
        execution=execution,
        budget=budget,
        command_handler=compute_commands,
        serve=serve_patch_validation_mailbox,
        validate=lambda text: _validate_work_patch_live(
            service,
            text,
            run_truth_scope=run_truth_scope,
            source_operation_id=_work_patch_source_operation_id(execution),
        ),
    )


def _prepare_work_patch_candidate(
    service: ProjectService,
    patch_text: str,
    *,
    run_truth_scope: list[str],
    source_operation_id: str | None = None,
    source_effect_id: str | None = None,
    profile: AgentProfile = "ordinary",
) -> _PreparedWorkPatch:
    if profile == "ordinary":
        draft, _ = service.parse_patch_output([patch_text])
    else:
        draft = parse_agent_patch_json(patch_text, profile=profile)
    validate_agent_patch_shape(draft, profile=profile)
    patch = prepare_agent_patch(
        draft,
        kind="work",
        run_truth_scope=run_truth_scope,
        repository_paths=service.manifest.repository_paths,
        source_operation_id=source_operation_id,
        source_effect_id=source_effect_id,
        source_effect_sha256=(
            hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
            if source_effect_id is not None
            else None
        ),
        profile=profile,
    )
    validate_work_patch(patch)
    return _PreparedWorkPatch(
        patch=patch,
        change_summary=tuple(draft.change_summary),
        proposal_ids=tuple(_work_patch_proposal_ids(patch)),
    )


def _validate_work_patch_live(
    service: ProjectService,
    patch_text: str,
    *,
    run_truth_scope: list[str],
    source_operation_id: str | None = None,
    source_effect_id: str | None = None,
    profile: AgentProfile = "ordinary",
) -> PatchValidationResult:
    return validate_work_patch_live(
        service,
        patch_text,
        prepare_candidate=lambda text: _prepare_work_patch_candidate(
            service,
            text,
            run_truth_scope=run_truth_scope,
            source_operation_id=source_operation_id,
            source_effect_id=source_effect_id,
            profile=profile,
        ),
        bounded_messages=_bounded_graph_messages,
    )


def _record_work_lock_wait(
    execution: AgentTaskExecution,
    message: str,
    location: str,
) -> None:
    detail = f"{message} Location: {location}"
    execution.store.update_agent_task_message(
        execution.operation_id,
        detail,
        phase="waiting",
        event=True,
    )
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "canonical_state_lock_wait",
        {"location": location},
        tier="diagnostic",
    )


def _record_work_lock_lost(
    execution: AgentTaskExecution,
    message: str,
    location: str,
) -> None:
    detail = (
        f"{message} RCP will report the observed outcome of the retained Work patch without "
        f"repeating operational work. Location: {location}"
    )
    execution.store.update_agent_task_message(
        execution.operation_id,
        detail,
        phase="applying",
    )
    execution.store.record_agent_task_event(
        execution.operation_id,
        detail,
        level="warning",
    )
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "canonical_state_lock_lost",
        {"location": location},
        tier="diagnostic",
    )


def _apply_work_patch(
    service: ProjectService,
    execution: AgentTaskExecution | None,
    patch_text: str,
    *,
    run_truth_scope: list[str],
    profile: AgentProfile = "ordinary",
    source_operation_id: str | None = None,
    source_effect_id: str | None = None,
) -> tuple[GraphUpdateResult | None, _DeliverableFailure | None]:
    """Validate and atomically apply one Work patch candidate."""

    source_operation_id = source_operation_id or _work_patch_source_operation_id(execution)
    return apply_work_patch(
        service,
        execution,
        patch_text,
        prepare_candidate=lambda text: _prepare_work_patch_candidate(
            service,
            text,
            run_truth_scope=run_truth_scope,
            source_operation_id=source_operation_id,
            source_effect_id=source_effect_id,
            profile=profile,
        ),
        source_operation_id=source_operation_id,
        source_effect_id=source_effect_id,
        canonical_matches=lambda canonical, candidate: (
            canonical.source_operation_id == source_operation_id
            and canonical.source_effect_sha256 == candidate.source_effect_sha256
            and canonical.kind == "work"
        ),
        canonical_binding_error="Work invocation source is bound to a different canonical Patch.",
        rejected_patch_error="The graph rejected the Work patch.",
        proposal_ids_for_patch=_work_patch_proposal_ids,
        bounded_messages=_bounded_graph_messages,
        record_lock_wait=(
            (lambda message, location: _record_work_lock_wait(execution, message, location))
            if execution is not None
            else None
        ),
        record_lock_lost=(
            (lambda message, location: _record_work_lock_lost(execution, message, location))
            if execution is not None
            else None
        ),
    )


def _work_patch_proposal_ids(patch: Patch) -> list[str]:
    proposal_ids: list[str] = []
    for operation in patch.ops:
        if not isinstance(operation, CreateProposalsOperation):
            continue
        proposal_ids.extend(proposal.id for proposal in operation.proposals)
    return list(dict.fromkeys(proposal_ids))


def _bounded_graph_messages(*messages: str) -> list[str]:
    bounded: list[str] = []
    for raw in messages:
        detail = " ".join(raw.split())[:1600]
        if detail and detail not in bounded:
            bounded.append(detail)
        if len(bounded) == 8:
            break
    return bounded


def _work_graph_repairable(
    execution: AgentTaskExecution | None,
    native_session_id: str | None,
    failure: _DeliverableFailure,
) -> bool:
    return bool(
        failure.correctable and native_session_id and execution is not None and execution.stage_root
    )


def _record_work_graph_rejection(
    execution: AgentTaskExecution | None,
    graph_update: GraphUpdateResult,
) -> None:
    if execution is None:
        return
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "work_graph_update_rejected",
        graph_update.model_dump(mode="json"),
    )
    detail = (
        graph_update.validation_messages[0]
        if graph_update.validation_messages
        else "The graph update was rejected."
    )
    execution.store.record_agent_task_event(
        execution.operation_id,
        f"Operational work completed, but the graph update was rejected: {detail}",
        level="warning",
    )


def _recorded_retry_deliverable_baseline(
    execution: AgentTaskExecution,
) -> _RetryDeliverableBaseline:
    """The baseline this turn captured before it launched, read back.

    Recapturing it here would read the stage the provider has since written to,
    so the retry's own output would be its own predecessor -- identical digests,
    and settlement would drop the graph update and watcher request the retry was
    run to produce. A missing receipt yields no predecessor at all, which errs
    toward applying the deliverable rather than discarding it.
    """

    for receipt in reversed(execution.store.agent_task_receipts(execution.operation_id)):
        if receipt.category != "retry_deliverable_baseline":
            continue
        payload = receipt.payload
        digests = payload.get("experiment_watch_sha256")
        return _RetryDeliverableBaseline(
            patch_digest=payload.get("patch_sha256"),
            watch_digest=payload.get("watch_sha256"),
            experiment_watch_digests=digests if isinstance(digests, dict) else {},
        )
    return _RetryDeliverableBaseline(None, None, {})


async def finalize_recorded_work_result(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    execution: AgentTaskExecution,
    recorded: RecordedProviderTurn,
) -> AsyncIterator[str]:
    """Settle a Work turn from the pass its host recorded.

    The recorded value is the input, not a hint about where to look: its Patch is
    the digest-verified one, written into the stage before anything reads the
    stage, so what settles is what the host proved rather than whatever the
    directory happens to hold now. No provider is launched, no prompt composed,
    and no correction asked for -- and every write lands under this turn's own
    operation id, because it is the same turn.
    """

    del data_dir  # Recovery uses only launch-time facts retained by this task.
    turn = _load_work_finalization_context(service, request, execution)
    verdict = decode_recorded_turn(recorded, provider_turn_request(turn.workspace, recorded))
    # The verified Patch replaces whatever the stage holds. A stage is mutable
    # and this value is not, so settling from the record means settling from
    # the record.
    _write_recorded_patch(turn, recorded)
    for frame in absorb_recorded_events(turn.outcome, verdict):
        yield frame
    for frame in _settle_work_outcome(turn):
        yield frame
    if turn.answer is None:
        return
    async with aclosing(
        finalize_work_result(
            turn,
            launcher,
            _recorded_retry_deliverable_baseline(execution),
            turn.answer,
            maximum_corrections=0,
        )
    ) as stream:
        async for frame in stream:
            yield frame


def _write_recorded_patch(
    turn: WorkFinalizationContext,
    recorded: RecordedProviderTurn,
) -> None:
    target = "patch.json"
    if recorded.patch is None:
        if turn.remote_stage is not None:
            turn.remote_stage.remove_workspace_file(target)
        else:
            (turn.workspace / target).unlink(missing_ok=True)
        return
    if turn.remote_stage is not None:
        turn.remote_stage.write_workspace_text(target, recorded.patch)
    else:
        (turn.workspace / target).write_text(recorded.patch, encoding="utf-8")
