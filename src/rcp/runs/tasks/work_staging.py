"""One staging path for every lifecycle that launches a Work provider turn.

Chat Work, the Experiment loop, and Auto-research child Work open or attach
the same stage, issue the same write scope, and build the same ``WorkTurn``.
What differs between them is policy, and each owner passes its concrete
policy in: which mailbox validates its Patch, which handoffs a new turn
clears, and which extra inputs it stages. Nothing here asks which lifecycle
is calling.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeVar

from rcp.agents.context import ChatContext
from rcp.attachments import ChatAttachmentStore
from rcp.background import AgentTaskExecution
from rcp.config import AgentSurface
from rcp.runs.chat import (
    _chat_read_dirs,
    _chat_stage_name,
    _ChatPatchInputs,
    _logical_chat_turn_operation_id,
    _prepare_local_artifact_directory,
    _prepare_local_chat_workspace,
    _project_write_scope,
    _record_chat_context_receipt,
    _stage_chat_patch_inputs,
    _validated_local_chat_resume_stage,
    _validated_remote_chat_resume_stage,
)
from rcp.runs.experiment_loop import StagedExperimentWatcherResource
from rcp.runs.patch_validator import PatchValidationBudget
from rcp.runs.shared import (
    _protected_run_stage_roots,
    _ProviderOutcome,
    _stage_context_paths,
    _swept_stage_root,
    _task_token,
)
from rcp.runs.tasks.compute_commands import WorkComputeCommands
from rcp.runs.tasks.work_turn_runtime import (
    ResolvedWorkExecution,
    StagedWorkInputs,
    WorkTurn,
    WorkValidatorMailboxLifecycle,
    close_work_validator_mailbox,
)
from rcp.service import ProjectService, RunRequest
from rcp.skill_registry import SkillSelection
from rcp.skills.staging import skill_bundle_label, stage_skill_selection
from rcp.transport import RemoteRunStage

_HandoffT = TypeVar("_HandoffT")


@dataclass(frozen=True)
class WorkStageLayout:
    """Where one Work turn's stage lives once it is open or attached."""

    request: RunRequest
    context: ChatContext
    local_stage: Path | None
    remote_stage: RemoteRunStage | None
    workspace: Path
    stage_name: str
    token: str


@dataclass(frozen=True)
class StagedArtifactAccess:
    """Artifact inputs an owner staged: paths the turn must not write, and its pointer."""

    protected_write_paths: list[str]
    pointer: dict[str, object] | None


async def stage_work_turn(
    service: ProjectService,
    resolved: ResolvedWorkExecution,
    data_dir: Path,
    execution: AgentTaskExecution | None,
    *,
    refresh_remote_stage: bool,
    read_dirs_before_write_scope: bool,
    compute_episode_id: str | None,
    prepare_handoffs: Callable[[WorkStageLayout], _HandoffT],
    start_validator_mailbox: Callable[..., WorkValidatorMailboxLifecycle],
    bind_context: Callable[[ChatContext], ChatContext] | None = None,
    stage_patch_inputs: Callable[[WorkStageLayout], _ChatPatchInputs] | None = None,
    stage_artifacts: (
        Callable[[WorkStageLayout, Path | PurePosixPath], StagedArtifactAccess] | None
    ) = None,
    stage_experiment_resources: (
        Callable[[WorkStageLayout], Awaitable[list[StagedExperimentWatcherResource]]] | None
    ) = None,
    select_skills: Callable[[RunRequest], tuple[SkillSelection, RunRequest]] | None = None,
) -> tuple[WorkTurn, StagedWorkInputs, _HandoffT]:
    """Build the launch context for one Work provider pass.

    ``refresh_remote_stage`` rolls a remote stage's retention timestamp forward.
    ``read_dirs_before_write_scope`` reads repository roots before the write
    scope resolves episode isolation, which may move a repository pointer to
    its worktree. ``compute_episode_id`` is the episode compute commands bill.
    ``prepare_handoffs`` clears or stages the owner's handoff files and its
    result is returned third. ``start_validator_mailbox`` is called as
    ``(service, staged, *, execution, budget, compute_commands, run_truth_scope)``.
    Without ``stage_patch_inputs`` the turn gets one brokered Patch mailbox;
    without ``select_skills`` the request's own skill selection is staged.
    """

    request = resolved.request
    continuation = execution.continuation if execution is not None else "fresh"
    resuming = continuation == "resume"
    local_stage: Path | None = None
    remote_stage: RemoteRunStage | None = None
    patch_inputs: _ChatPatchInputs | None = None
    validator_lifecycle: WorkValidatorMailboxLifecycle | None = None
    validator_budget = PatchValidationBudget()
    outcome = _ProviderOutcome(session_id=request.session_id)
    try:
        context = service.assemble_chat(request)
        if bind_context is not None:
            context = bind_context(context)
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
            if refresh_remote_stage:
                remote_stage.touch()
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
        token = _task_token(execution)
        layout = WorkStageLayout(
            request=request,
            context=context,
            local_stage=local_stage,
            remote_stage=remote_stage,
            workspace=workspace,
            stage_name=stage_name,
            token=token,
        )
        patch_inputs = (
            stage_patch_inputs(layout)
            if stage_patch_inputs is not None
            else _stage_chat_patch_inputs(
                local_stage,
                remote_stage,
                workspace=workspace,
                stage_name=stage_name,
                task_id=execution.operation_id if execution is not None else token,
                turn_id=f"{token}:work",
                broker=True,
                episode_id=request.control_episode_id,
                ask_wait_seconds=resolved.ask_wait_seconds,
            )
        )
        handoff = prepare_handoffs(layout)
        artifact_scope_id = (
            _logical_chat_turn_operation_id(execution.store, execution.operation_id)
            if execution is not None and resuming
            else execution.operation_id
            if execution is not None
            else str(uuid.uuid4())
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
        artifacts = (
            stage_artifacts(layout, artifact_directory) if stage_artifacts is not None else None
        )

        def read_dirs_now() -> list[Path]:
            return _chat_read_dirs(
                context,
                local_stage,
                remote_stage,
                service,
                resolved.execution_machine_alias,
            )

        read_dirs = read_dirs_now() if read_dirs_before_write_scope else []
        write_scope = _project_write_scope(
            context,
            service,
            resolved.execution_machine_alias,
            episode_request=request,
            local_stage=local_stage,
            workspace=workspace,
            remote_stage=remote_stage,
            data_dir=data_dir,
            execution=execution,
            capability="work_auto",
            additional_protected_write_paths=(
                artifacts.protected_write_paths if artifacts is not None else None
            ),
        )
        if not read_dirs_before_write_scope:
            read_dirs = read_dirs_now()
        compute_commands = (
            WorkComputeCommands(
                execution, service.manifest, write_scope, remote_stage, compute_episode_id
            )
            if execution is not None
            else None
        )
        validator_lifecycle = start_validator_mailbox(
            service,
            patch_inputs.validator_staged,
            execution=execution,
            budget=validator_budget,
            compute_commands=compute_commands,
            run_truth_scope=context.run_truth_scope,
        )
        write_dirs = [Path(item) for item in write_scope.repository_roots]
        experiment_resources = (
            await stage_experiment_resources(layout)
            if stage_experiment_resources is not None
            else []
        )
        experiment_resource_pointers = [
            dict[str, object](item.prompt_value()) for item in experiment_resources
        ]
        if select_skills is not None:
            skill_selection, request = select_skills(request)
        else:
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
        if artifacts is not None and artifacts.pointer is not None:
            attachment_pointers.append(artifacts.pointer)
        read_dirs.extend(
            path
            for path in dict.fromkeys(
                Path(str(item["path"])).parent for item in attachment_pointers
            )
            if path not in read_dirs
        )
        repositories: list[dict[str, object]] = [
            {"alias": item.alias, "host": item.host, "path": item.path}
            for item in context.repositories
        ]
        turn = WorkTurn(
            data_dir=data_dir,
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
        staged = StagedWorkInputs(
            token=token,
            artifact_scope_id=artifact_scope_id,
            artifact_directory=artifact_directory,
            experiment_resources=experiment_resources,
            experiment_resource_pointers=experiment_resource_pointers,
            skill_selection=skill_selection,
            skill_pointers=skill_pointers,
            attachment_pointers=attachment_pointers,
            repositories=repositories,
        )
        return turn, staged, handoff
    except BaseException as exc:
        if validator_lifecycle is not None:
            await validator_lifecycle.close(primary_error=exc)
        elif patch_inputs is not None and not patch_inputs.validator_staged.credential.expired:
            await close_work_validator_mailbox(
                patch_inputs.validator_staged,
                stop=None,
                task=None,
                execution=execution,
                primary_error=exc,
            )
        raise
