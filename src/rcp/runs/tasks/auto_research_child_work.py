from __future__ import annotations

import asyncio
import shlex
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import replace
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Literal, get_args

from rcp.agents import AgentEvent, AgentLauncher, PromptFactory
from rcp.agents.command_mailbox import (
    CommandTurnIdentity,
    StagedCommandMailbox,
    serve_command_mailbox,
    stage_command_mailbox,
)
from rcp.agents.command_protocol import (
    CommandRequest,
    CommandResponse,
    CommandVerb,
    MessageCommandRequest,
    ValidateCommandRequest,
)
from rcp.agents.continuation_prompt import LaunchPhase, classify
from rcp.agents.prompts import (
    COMMAND_CLIENT,
    _invoked_package_section,
    invoked_package_pointers,
    invoked_provider_skill_section,
)
from rcp.attachments import ChatAttachmentStore
from rcp.background import AgentTaskContinuation, AgentTaskExecution
from rcp.history import ReplayHalted
from rcp.limits import (
    AUTO_RESEARCH_MAIL_MAX_BYTES,
    COMPUTE_COMMAND_TIMEOUT_SECONDS,
    PATCH_SELF_CHECK_MAX_COUNT,
    PATCH_SELF_CHECK_POLL_SECONDS,
)
from rcp.runs.auto_research_mail import (
    AUTO_RESEARCH_MAIL_HANDOFF_FILE,
    auto_research_mail_delivery,
    parse_auto_research_mail_delivery,
    stage_auto_research_mail_delivery,
)
from rcp.runs.chat import (
    _chat_read_dirs,
    _chat_stage_name,
    _ChatPatchInputs,
    _clear_stale_turn_handoffs,
    _logical_chat_turn_operation_id,
    _prepare_local_artifact_directory,
    _prepare_local_chat_workspace,
    _project_write_scope,
    _record_chat_context_receipt,
    _stage_chat_patch_inputs,
    _validated_local_chat_resume_stage,
    _validated_remote_chat_resume_stage,
)
from rcp.runs.patch_validator import (
    PatchValidationBudget,
    PatchValidationResult,
    command_rejection_recorder,
    command_transport_recorder,
)
from rcp.runs.recorded_turn import RecordedProviderTurn
from rcp.runs.session_master import record_inline_prompt
from rcp.runs.shared import (
    _parent_task_contract_path,
    _protected_run_stage_roots,
    _ProviderOutcome,
    _sse,
    _stage_context_paths,
    _stage_task_contract,
    _swept_stage_root,
    _task_token,
    note_link_lost_before_provider,
    retry_original_contract_path,
)
from rcp.runs.tasks.compute_commands import WorkComputeCommands
from rcp.runs.tasks.experiment_watcher_maintenance import _process_experiment_watcher_maintenance
from rcp.runs.tasks.work import (
    PATCH_CORRECTION_MAX_ROUNDS,
    WorkFinalizationContext,
    WorkTurn,
    _capture_retry_deliverable_baseline,
    _close_work_validator_mailbox,
    _compose_work_recovery_prompt,
    _ComposedWorkPrompt,
    _finalize_work_artifacts,
    _finalize_work_turn,
    _launch_and_stream_work_turn,
    _maintenance_continuation,
    _prepare_work_chat_prompt,
    _record_work_finalization_context,
    _recorded_retry_deliverable_baseline,
    _resolve_work_execution,
    _ResolvedWorkExecution,
    _resume_work_compute_commands,
    _RetryDeliverableBaseline,
    _settle_patch_deliverable,
    _settle_watch_deliverable,
    _SettledWorkDeliverables,
    _stage_retry_diagnostics,
    _stage_work_contract,
    _StagedWorkInputs,
    _stream_work_graph_repair,
    _validate_work_patch_live,
    _work_chat_master_context,
    _work_continuation,
    _work_contract_text,
    _work_execution_instructions,
    _work_finalization_context,
    _work_mailbox_context,
    _work_prompt_values,
    _WorkMailboxContext,
    _WorkValidatorMailboxLifecycle,
    open_recorded_work_turn,
)
from rcp.runs.tasks.work_turn_runtime import (
    load_work_mailbox_context,
    restore_work_validator_mailbox,
    start_work_validator_mailbox,
)
from rcp.service import ProjectService, RunRequest
from rcp.skills.staging import skill_bundle_label, stage_skill_selection
from rcp.storage import (
    AgentCommandInvocationRecord,
    AutoResearchChildWorkRecord,
    AutoResearchMessageRecord,
)
from rcp.transport import RemoteRunStage, RunStageMailbox, StateUnavailable

_CHILD_WORK_HANDOFFS_CLEARED_RECEIPT = "auto_research_child_work_handoffs_cleared"


def _prepare_auto_research_child_work_handoffs(
    execution: AgentTaskExecution,
    *,
    local_stage: Path | None,
    workspace: Path,
    remote_stage: RemoteRunStage | None,
) -> None:
    """Clear a new child turn once while preserving an interrupted turn's outputs."""

    if any(
        receipt.category == _CHILD_WORK_HANDOFFS_CLEARED_RECEIPT
        for receipt in execution.store.agent_task_receipts(execution.operation_id)
    ):
        return
    _clear_stale_turn_handoffs(workspace, remote_stage)
    if local_stage is not None:
        RunStageMailbox.for_stage(
            local_stage=local_stage / "inputs",
            remote_stage=None,
        ).remove(AUTO_RESEARCH_MAIL_HANDOFF_FILE)
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        _CHILD_WORK_HANDOFFS_CLEARED_RECEIPT,
        {
            "version": 1,
            "files": [
                "patch.json",
                "watch.json",
                "inputs/messages.json" if local_stage is not None else "messages.json",
                "lifecycle.json",
            ],
        },
    )


def _stage_auto_research_child_work_mail(
    execution: AgentTaskExecution,
    route: AutoResearchChildWorkRecord,
    *,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    continuation: AgentTaskContinuation,
) -> str | None:
    """Stage only the batch durably claimed by this exact ordinary Work turn."""

    mailbox = RunStageMailbox.for_stage(
        local_stage=local_stage / "inputs" if local_stage is not None else None,
        remote_stage=remote_stage,
    )
    delivery_operation_id = _auto_research_child_mail_allocation_id(
        execution,
        route,
        continuation=continuation,
    )
    if delivery_operation_id is None:
        return None
    claimed = [
        message
        for message in execution.store.auto_research_messages(route.episode_id)
        if message.delivery_operation_id == delivery_operation_id
    ]
    if not claimed:
        raise ValueError(
            "Auto-research child Work message wake has no mail claimed by this allocation."
        )
    delivery = auto_research_mail_delivery(
        episode_id=route.episode_id,
        recipient_task_id=route.worker_id,
        delivery_operation_id=delivery_operation_id,
        messages=claimed,
    )
    if AUTO_RESEARCH_MAIL_HANDOFF_FILE in mailbox.entry_names():
        retained = parse_auto_research_mail_delivery(
            mailbox.read_text(
                AUTO_RESEARCH_MAIL_HANDOFF_FILE,
                max_bytes=AUTO_RESEARCH_MAIL_MAX_BYTES,
            )
        )
        if retained != delivery:
            raise ValueError("Retained child Work mail differs from its durable claimed batch.")
    else:
        stage_auto_research_mail_delivery(mailbox, delivery)
    return str(mailbox.workspace / AUTO_RESEARCH_MAIL_HANDOFF_FILE)


def _auto_research_child_mail_allocation_id(
    execution: AgentTaskExecution,
    route: AutoResearchChildWorkRecord,
    *,
    continuation: AgentTaskContinuation,
) -> str | None:
    """Resolve the paid message allocation through exact same-session recovery attempts."""

    if continuation == "message_wake":
        return execution.operation_id
    if continuation not in {"resume", "retry"}:
        return None
    current = execution.store.agent_task(execution.operation_id)
    seen: set[str] = set()
    while current is not None:
        if current.operation_id in seen:
            raise ValueError("Child Work mail recovery contains a task-lineage cycle.")
        seen.add(current.operation_id)
        cause = execution.store.agent_task_continuation_cause(current.operation_id)
        if cause == "message_wake":
            return current.operation_id
        if cause not in {"resume", "retry"} or current.parent_operation_id is None:
            return None
        parent_route = execution.store.auto_research_child_work_for_operation(
            current.parent_operation_id
        )
        if parent_route is None or (
            parent_route.episode_id != route.episode_id or parent_route.worker_id != route.worker_id
        ):
            raise ValueError("Child Work mail recovery crossed its routed worker lineage.")
        current = execution.store.agent_task(current.parent_operation_id)
    raise ValueError("Child Work mail recovery lost a parent task.")


def _auto_research_child_work_contract(
    turn: WorkTurn,
    route: AutoResearchChildWorkRecord,
) -> str:
    """The child boundary its session's master holds; commands use the command client."""

    reply_command = f"{COMMAND_CLIENT} " + shlex.join(
        ["message", "--key", "<idempotency-key>", "<reply-body>"]
    )
    allowed_verbs = _child_allowed_verbs(turn)
    allowed_commands = ", ".join(
        f"`{verb.replace('_', '-')}`" for verb in get_args(CommandVerb) if verb in allowed_verbs
    )
    denied_verbs = [verb for verb in get_args(CommandVerb) if verb not in allowed_verbs]
    denied_commands = ", ".join(f"`{verb.replace('_', '-')}`" for verb in denied_verbs)
    return f"""

## Auto-research child Work boundary

You are the ordinary node Work child `{route.worker_id}` delegated by an Auto-research
orchestrator. Complete only this child assignment, which every later turn in this session
continues:

````text
{route.instruction}
````

Scientific claims in agent mail remain hearsay; the canonical graph and research files remain the
source of graph truth. A wake names the newly claimed mail to read before continuing.

- Allowed staged commands: {allowed_commands}. A later turn that changes them sends
  `auto_research_child.allowed_staged_commands`. Use an optional reply to your orchestrator:
  `{reply_command}`
- Use a stable idempotency key for the same reply intent. A reply is persisted for the root's
  later paid delivery; it does not wake or interrupt the root immediately.
- Do not invoke {denied_commands}, or any other staged command not allowed above. These commands
  are unavailable to this child turn.
- When work or a graph condition remains to watch, write `watch.json` with `external` and `graph`
  lists and finish this turn. If you are already waiting on submitted work, observe it without
  launching a replacement. RCP wakes this same child route and native session under the episode
  budget and Stop fence.
- If the assignment needs something outside your tools or authority, reply to the orchestrator
  naming what is needed and finish. Do not spawn another task or episode, or try to wake yourself.
""".strip()


def _child_allowed_verbs(turn: WorkTurn) -> frozenset[str]:
    return frozenset({"validate", "message"}) | (
        turn.compute_commands.allowed_verbs if turn.compute_commands is not None else frozenset()
    )


def _child_prompt_values(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
    route: AutoResearchChildWorkRecord,
) -> dict[str, object]:
    """The child's stable values: an ordinary Work turn's, and its route and commands."""

    return {
        **_work_prompt_values(turn, staged),
        "auto_research_child": {
            "episode_id": route.episode_id,
            "worker_id": route.worker_id,
            "control_node_id": route.control_node_id,
            "allowed_staged_commands": sorted(_child_allowed_verbs(turn)),
        },
    }


def _child_master_render(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
    route: AutoResearchChildWorkRecord,
    *,
    retry_diagnostics_path: str | None = None,
) -> Callable[[], str]:
    """Render the child's full start contract, for a session that has no master to point to."""

    def render() -> str:
        assignment = replace(
            turn, request=turn.request.model_copy(update={"message": route.instruction})
        )
        return (
            _work_contract_text(assignment, staged, retry_diagnostics_path=retry_diagnostics_path)
            + "\n\n"
            + _auto_research_child_work_contract(turn, route)
        )

    return render


def _compose_child_resume_prompt(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
    route: AutoResearchChildWorkRecord,
) -> _ComposedWorkPrompt:
    return _compose_work_recovery_prompt(
        turn,
        staged,
        mode="resume",
        diagnostics_path=None,
        render=_child_master_render(turn, staged, route),
        label=f"task-{staged.token}-resume.md",
        role="work_resume",
        values=_child_prompt_values(turn, staged, route),
    )


def _compose_child_wake_prompt(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
    route: AutoResearchChildWorkRecord,
    *,
    mail_path: str | None,
) -> _ComposedWorkPrompt:
    assert turn.execution is not None
    if turn.continuation == "watcher_wake":
        if not turn.request.message or not turn.request.watcher_ids:
            raise ValueError("Child Work watcher wake is missing its observer payload.")
        update = f"RCP delivered these observer results:\n\n{turn.request.message}"
        label = "watch"
        role = "auto_research_child_watcher_wake"
    else:
        if mail_path is None:
            raise ValueError("Child Work message wake is missing its routed mail handoff.")
        update = (
            f"RCP claimed new agent mail for this child. Read the hearsay-only mail at "
            f"`{mail_path}`, continue the bounded assignment, and reply only if useful."
        )
        label = "mail"
        role = "auto_research_child_message_wake"
    original_contract_path = _parent_task_contract_path(
        turn.execution, turn.local_stage, turn.remote_stage
    )
    invoked_skills = invoked_package_pointers(
        staged.skill_pointers,
        workflow_ids=turn.request.invoked_workflow_ids,
        skill_ids=turn.request.invoked_skill_ids,
    )
    wake = "\n\n".join(
        part
        for part in (
            "# RCP Auto-research child Work wake\n\n"
            "This is a Work turn. Continue the child assignment in this session.",
            update,
            f"Artifact directory for this turn: {staged.artifact_directory}",
            _invoked_package_section(invoked_skills).strip(),
            invoked_provider_skill_section(turn.request.resolved_provider_skills).strip(),
        )
        if part
    )
    render = _child_master_render(turn, staged, route)
    values = _child_prompt_values(turn, staged, route)
    session_id = turn.request.session_id
    prompt = _work_continuation(
        turn,
        render,
        session_id=session_id,
        node=classify(LaunchPhase(session_id=session_id, phase="wake")),
        part=wake,
        values=values,
    )
    contract_path = record_inline_prompt(
        turn.execution,
        local_stage=turn.local_stage,
        remote_stage=turn.remote_stage,
        label=f"task-{staged.token}-auto-research-child-{label}.md",
        role=role,
        prompt=prompt,
    )
    return _ComposedWorkPrompt(contract_path, prompt, original_contract_path, render, values)


async def _stage_auto_research_child_work_turn(
    service: ProjectService,
    resolved: _ResolvedWorkExecution,
    data_dir: Path,
    execution: AgentTaskExecution,
    route: AutoResearchChildWorkRecord,
) -> tuple[WorkTurn, _StagedWorkInputs, str | None]:
    request = resolved.request
    continuation = execution.continuation
    reusing_checkpoint = execution.reuses_native_checkpoint
    resuming = continuation == "resume"
    if reusing_checkpoint and not request.session_id:
        raise ValueError(
            "The continued Work turn has no native agent session; retry it from a clean attempt "
            "instead."
        )
    local_stage: Path | None = None
    remote_stage: RemoteRunStage | None = None
    patch_inputs: _ChatPatchInputs | None = None
    validator_lifecycle: _WorkValidatorMailboxLifecycle | None = None
    validator_budget = PatchValidationBudget()
    outcome = _ProviderOutcome(session_id=request.session_id)
    try:
        context = service.assemble_chat(request)
        surface = "project_chat" if request.chat_scope == "project" else "node_chat"
        _record_chat_context_receipt(execution, context, surface=surface)
        stage_name = _chat_stage_name(service, request, execution)
        saved_stage = execution.stage_root is not None
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
                        execution.store,
                        resolved.execution_host,
                    ),
                )
            assert remote_stage.root is not None
            remote_stage.touch()
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
            stage_root = _swept_stage_root(data_dir, store=execution.store)
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
            execution.checkpoint_stage("", str(local_stage))
        token = _task_token(execution)
        patch_inputs = _stage_chat_patch_inputs(
            local_stage,
            remote_stage,
            workspace=workspace,
            stage_name=stage_name,
            task_id=execution.operation_id,
            turn_id=f"{token}:work",
        )
        patch_inputs.validator_staged.cleanup()
        child_staged = stage_command_mailbox(
            local_stage=workspace if remote_stage is None else None,
            remote_stage=remote_stage,
            local_input_stage=local_stage if remote_stage is None else None,
            episode_id=route.episode_id,
            task_id=execution.operation_id,
            turn_id=f"{token}:auto-research-child-work",
            # The child serves compute launches too, which outlast a Patch check.
            timeout_seconds=COMPUTE_COMMAND_TIMEOUT_SECONDS,
        )
        patch_inputs = _ChatPatchInputs(
            patch_path=patch_inputs.patch_path,
            watch_path=patch_inputs.watch_path,
            schema_path=patch_inputs.schema_path,
            validator_command=child_staged.client_command("validate", patch_inputs.patch_path),
            validator_mailbox_id=child_staged.credential.mailbox_id,
            validator_staged=child_staged,
            command_client=child_staged.client_command(),
        )
        if not reusing_checkpoint or continuation in {"message_wake", "watcher_wake"}:
            _prepare_auto_research_child_work_handoffs(
                execution,
                local_stage=local_stage,
                workspace=workspace,
                remote_stage=remote_stage,
            )
        mail_path = _stage_auto_research_child_work_mail(
            execution,
            route,
            local_stage=local_stage,
            remote_stage=remote_stage,
            continuation=continuation,
        )
        artifact_scope_id = (
            _logical_chat_turn_operation_id(execution.store, execution.operation_id)
            if resuming
            else execution.operation_id
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
            episode_request=request,
        )
        read_dirs = _chat_read_dirs(
            context,
            local_stage,
            remote_stage,
            service,
            resolved.execution_machine_alias,
        )
        compute_commands = WorkComputeCommands(
            execution, service.manifest, write_scope, remote_stage, route.episode_id
        )
        validator_lifecycle = _start_auto_research_child_validator_mailbox(
            service,
            child_staged,
            execution=execution,
            route=route,
            budget=validator_budget,
            compute_commands=compute_commands,
            run_truth_scope=context.run_truth_scope,
        )
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
            write_dirs=[Path(item) for item in write_scope.repository_roots],
            write_scope=write_scope,
            patch_inputs=patch_inputs,
            validator_lifecycle=validator_lifecycle,
            validator_budget=validator_budget,
            compute_commands=compute_commands,
            outcome=outcome,
        )
        return (
            turn,
            _StagedWorkInputs(
                token=token,
                artifact_scope_id=artifact_scope_id,
                artifact_directory=artifact_directory,
                experiment_resources=[],
                experiment_resource_pointers=[],
                skill_selection=skill_selection,
                skill_pointers=skill_pointers,
                attachment_pointers=attachment_pointers,
                repositories=repositories,
            ),
            mail_path,
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
        raise


def _compose_child_fresh_prompt(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
    route: AutoResearchChildWorkRecord,
    *,
    retry_diagnostics_path: str | None = None,
) -> _ComposedWorkPrompt:
    assert turn.request.message is not None
    values = _child_prompt_values(turn, staged, route)
    render = _child_master_render(
        turn, staged, route, retry_diagnostics_path=retry_diagnostics_path
    )
    if not turn.uses_master_protocol:
        contract_path = _stage_work_contract(
            turn,
            staged,
            retry_diagnostics_path=retry_diagnostics_path,
            contract=render(),
            values=values,
        )
        return _ComposedWorkPrompt(
            contract_path,
            PromptFactory.launch_prompt(contract_path),
            contract_path,
            render,
            values,
        )

    prompt, contract_path = _prepare_work_chat_prompt(
        turn.execution,
        turn.request,
        launch_instructions=_work_execution_instructions(turn, COMMAND_CLIENT),
        local_stage=turn.local_stage,
        remote_stage=turn.remote_stage,
        artifact_path=str(staged.artifact_directory),
        master_context=_work_chat_master_context(
            turn,
            staged,
            auto_research_child_boundary=_auto_research_child_work_contract(turn, route),
        ),
        stable_values=values,
        skill_pointers=staged.skill_pointers,
        attachment_pointers=staged.attachment_pointers,
        ontology_extensions=turn.context.ontology_extensions,
    )
    return _ComposedWorkPrompt(contract_path, prompt, contract_path, render, values)


def _compose_child_retry_prompt(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
    route: AutoResearchChildWorkRecord,
    *,
    mail_path: str | None,
) -> _ComposedWorkPrompt:
    assert turn.execution is not None
    retry_diagnostics_path = _stage_retry_diagnostics(turn, staged)
    render = _child_master_render(
        turn, staged, route, retry_diagnostics_path=retry_diagnostics_path
    )
    values = _child_prompt_values(turn, staged, route)
    if turn.request.session_id is not None:
        return _compose_work_recovery_prompt(
            turn,
            staged,
            mode="retry",
            diagnostics_path=retry_diagnostics_path,
            render=render,
            label=f"task-{staged.token}-retry.md",
            role="work_retry",
            values=values,
        )
    # A handoff starts a new native session from the full current contract.
    current_contract_path = _stage_work_contract(
        turn,
        staged,
        retry_diagnostics_path=retry_diagnostics_path,
        contract=render(),
        values=values,
    )
    original_contract_path = retry_original_contract_path(
        turn.execution, turn.local_stage, turn.remote_stage, current_contract_path
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
        output_schema_path=turn.patch_inputs.schema_path,
        invoked_skill_pointers=invoked_package_pointers(
            staged.skill_pointers,
            workflow_ids=turn.request.invoked_workflow_ids,
            skill_ids=turn.request.invoked_skill_ids,
        ),
        invoked_provider_skills=turn.request.resolved_provider_skills,
    )
    if mail_path is not None:
        retry_contract += (
            f"\n\nRead the newly claimed hearsay-only mail at `{mail_path}` before continuing."
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
        render_master=render,
        prompt_values=values,
    )


def _compose_child_prompt(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
    route: AutoResearchChildWorkRecord,
    *,
    mail_path: str | None,
) -> _ComposedWorkPrompt:
    if turn.resuming:
        return _compose_child_resume_prompt(turn, staged, route)
    if turn.continuation in {"watcher_wake", "message_wake"}:
        return _compose_child_wake_prompt(turn, staged, route, mail_path=mail_path)
    if turn.retrying or turn.continuation == "handoff":
        return _compose_child_retry_prompt(turn, staged, route, mail_path=mail_path)
    return _compose_child_fresh_prompt(
        turn,
        staged,
        route,
        retry_diagnostics_path=_stage_retry_diagnostics(turn, staged),
    )


def _start_auto_research_child_validator_mailbox(
    service: ProjectService,
    staged: StagedCommandMailbox,
    *,
    execution: AgentTaskExecution,
    route: AutoResearchChildWorkRecord,
    budget: PatchValidationBudget,
    compute_commands: WorkComputeCommands,
    run_truth_scope: list[str],
) -> _WorkValidatorMailboxLifecycle:
    return start_work_validator_mailbox(
        staged,
        execution=execution,
        budget=budget,
        validate=lambda text: _validate_work_patch_live(
            service,
            text,
            run_truth_scope=run_truth_scope,
            source_operation_id=execution.operation_id,
        ),
        serve=partial(
            _serve_auto_research_child_work_mailbox,
            route=route,
            compute_commands=compute_commands,
        ),
        resume_context=_ChildMailboxContext(
            **_work_mailbox_context(run_truth_scope, compute_commands).model_dump(),
            route=route,
        ).model_dump(mode="json"),
    )


class _ChildMailboxContext(_WorkMailboxContext):
    route: AutoResearchChildWorkRecord


def resume_child_work_command_mailbox(
    service: Callable[[], ProjectService], execution: AgentTaskExecution
) -> _WorkValidatorMailboxLifecycle | None:
    saved = load_work_mailbox_context(execution)
    if saved is None:
        return None
    context = _ChildMailboxContext.model_validate(saved)
    route = execution.store.auto_research_child_work_for_operation(execution.operation_id)
    if route is None or (
        route.worker_id != context.route.worker_id
        or route.episode_id != context.route.episode_id
        or context.route.current_operation_id != execution.operation_id
    ):
        raise ValueError("The retained child Work mailbox route no longer names this turn.")
    compute_commands = _resume_work_compute_commands(execution, context)
    if compute_commands is None:
        raise ValueError("The retained child Work mailbox has no compute scope.")
    return restore_work_validator_mailbox(
        execution,
        validate=lambda text: _validate_work_patch_live(
            service(),
            text,
            run_truth_scope=context.run_truth_scope,
            source_operation_id=execution.operation_id,
        ),
        serve=partial(
            _serve_auto_research_child_work_mailbox,
            route=context.route,
            compute_commands=compute_commands,
        ),
    )


AUTO_RESEARCH_CHILD_FINALIZATION_CONTEXT_ROLE = "auto_research_child_finalization_context"


async def finalize_recorded_auto_research_child_work_result(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    execution: AgentTaskExecution,
    recorded: RecordedProviderTurn,
) -> AsyncIterator[str]:
    """Settle one child Work turn from the pass its host recorded.

    A lost connection is not a lost child. The result lands on the operation
    that opened the pass, under the parent episode's authority it already had,
    and no second child is admitted to go and fetch it.
    """

    del data_dir  # Recovery uses only launch-time facts retained by this task.
    finalization, opened = open_recorded_work_turn(
        service,
        request,
        execution,
        recorded,
        role=AUTO_RESEARCH_CHILD_FINALIZATION_CONTEXT_ROLE,
        owner="Auto-research child Work",
    )
    for frame in opened:
        yield frame
    if finalization.answer is None:
        return
    async with aclosing(
        settle_child_work_deliverables(
            finalization,
            launcher,
            _recorded_retry_deliverable_baseline(execution),
            maximum_corrections=0,
        )
    ) as stream:
        async for frame in stream:
            yield frame


async def settle_child_work_deliverables(
    finalization: WorkFinalizationContext,
    launcher: AgentLauncher,
    retry_baseline: _RetryDeliverableBaseline,
    *,
    launch_turn: WorkTurn | None = None,
    staged: _StagedWorkInputs | None = None,
    composed: _ComposedWorkPrompt | None = None,
    required_session_id: str | None = None,
    maximum_corrections: int = PATCH_CORRECTION_MAX_ROUNDS,
) -> AsyncIterator[str]:
    """Turn one finished child result into this task's durable output.

    The one door a child Work result goes through, whether its provider
    streamed to this process or finished on a host that outlived the link.
    Graph updates, watchers, and copied artifacts settle through the same
    durable output path for streamed, retried, and recovered turns.
    """

    settled = _SettledWorkDeliverables(native_session_id=finalization.outcome.session_id)
    async with aclosing(
        _settle_patch_deliverable(
            finalization,
            launcher,
            retry_baseline.patch_digest,
            settled,
            launch_turn=launch_turn,
            staged=staged,
            composed=composed,
            required_session_id=required_session_id,
            maximum_corrections=maximum_corrections,
        )
    ) as stream:
        async for frame in stream:
            yield frame
    if settled.stop:
        return

    (
        maintenance_frames,
        native_session_id,
        maintenance_paused,
    ) = await _process_experiment_watcher_maintenance(
        service=finalization.service,
        launcher=launcher,
        request=finalization.request,
        execution=finalization.execution,
        staged_resources=[],
        workspace=finalization.workspace,
        remote_stage=finalization.remote_stage,
        local_stage=finalization.local_stage,
        token=_task_token(finalization.execution),
        native_session_id=settled.native_session_id,
        read_dirs=launch_turn.read_dirs if launch_turn is not None else [],
        write_dirs=launch_turn.write_dirs if launch_turn is not None else [],
        write_scope=finalization.write_scope,
        execution_host=finalization.execution_host,
        provider_binary=launch_turn.provider_binary if launch_turn is not None else None,
        retry_output_digests=retry_baseline.experiment_watch_digests,
        maximum_corrections=maximum_corrections,
        supervise_remote=launch_turn.supervise_remote if launch_turn is not None else False,
        continuation=_maintenance_continuation(launch_turn, composed),
    )
    settled.native_session_id = native_session_id
    for frame in maintenance_frames:
        yield frame
    if maintenance_paused:
        return
    async with aclosing(
        _settle_watch_deliverable(
            finalization,
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
    for artifact in _finalize_work_artifacts(finalization):
        yield _sse(AgentEvent(event="artifact", artifact=artifact))

    final_turn = finalization
    if finalization.continuation == "message_wake" and finalization.request.message is None:
        final_turn = replace(
            finalization,
            request=finalization.request.model_copy(
                update={
                    "message": (
                        "RCP delivered a claimed Auto-research mail batch in the separate "
                        "messages.json handoff."
                    )
                }
            ),
        )
    for frame in _finalize_work_turn(final_turn, finalization.answer, settled.graph_update):
        yield frame


async def stream_auto_research_child_work_run(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    execution: AgentTaskExecution,
    *,
    route: AutoResearchChildWorkRecord,
) -> AsyncIterator[str]:
    """Run one durably admitted Auto-research child Work task."""

    if execution.continuation == "graph_repair":
        async with aclosing(
            _stream_work_graph_repair(
                service,
                launcher,
                request,
                data_dir,
                execution=execution,
                master_for=lambda turn, staged: (
                    _child_master_render(turn, staged, route),
                    _child_prompt_values(turn, staged, route),
                ),
            )
        ) as stream:
            async for frame in stream:
                yield frame
        return

    turn: WorkTurn | None = None
    try:
        resolved = _resolve_work_execution(service, request, execution)
        turn, staged, mail_path = await _stage_auto_research_child_work_turn(
            service,
            resolved,
            data_dir,
            execution,
            route,
        )
        composed = _compose_child_prompt(turn, staged, route, mail_path=mail_path)
        retry_baseline = _capture_retry_deliverable_baseline(turn)
    except BaseException as exc:
        if turn is not None:
            await turn.validator_lifecycle.close(primary_error=exc)
        note_link_lost_before_provider(execution, exc)
        if isinstance(exc, (OSError, ReplayHalted, StateUnavailable, ValueError)):
            yield _sse(AgentEvent(event="error", text=str(exc)))
            return
        raise

    assert turn is not None
    required_session_id = turn.request.session_id if execution.reuses_native_checkpoint else None
    finalization = _work_finalization_context(
        turn, staged, role=AUTO_RESEARCH_CHILD_FINALIZATION_CONTEXT_ROLE
    )
    if turn.execution_host:
        # Only a remote turn can outlive this connection, and only under this
        # owner's own role: a child settles differently from ordinary Work.
        _record_work_finalization_context(
            turn,
            staged,
            role=AUTO_RESEARCH_CHILD_FINALIZATION_CONTEXT_ROLE,
            required_session_id=required_session_id,
        )
    async with aclosing(
        _launch_and_stream_work_turn(
            turn,
            finalization,
            launcher,
            composed.prompt,
            composed.contract_path,
            staged,
            None,
            required_session_id=required_session_id,
            supervise_remote=bool(turn.execution_host),
        )
    ) as stream:
        async for frame in stream:
            yield frame
    if finalization.answer is None:
        return

    async with aclosing(
        settle_child_work_deliverables(
            finalization,
            launcher,
            retry_baseline,
            launch_turn=turn,
            staged=staged,
            composed=composed,
            required_session_id=required_session_id,
        )
    ) as stream:
        async for frame in stream:
            yield frame


def _child_reply_message_id(episode_id: str, idempotency_key: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"rcp:auto_research:{episode_id}:message:{idempotency_key}",
        )
    )


def _unused_child_command_id(execution: AgentTaskExecution, preferred: str) -> str:
    if execution.store.agent_command(preferred) is None:
        return preferred
    while True:
        candidate = uuid.uuid4().hex
        if execution.store.agent_command(candidate) is None:
            return candidate


def _child_reply_result(
    message: AutoResearchMessageRecord,
    *,
    disposition: Literal["created", "existing"],
) -> dict[str, object]:
    return {
        "message_id": message.message_id,
        "recipient_task_id": message.recipient_task_id,
        "delivery_operation_id": message.delivery_operation_id,
        "delivery": "started" if message.delivery_operation_id is not None else "pending",
        "graph_authority": "none",
        "epistemic_status": "hearsay",
        "disposition": disposition,
    }


def _finish_child_reply_command(
    execution: AgentTaskExecution,
    *,
    command_id: str,
    request_id: str,
    status: Literal["ok", "invalid", "unavailable"],
    message: str,
    result: dict[str, object] | None = None,
) -> CommandResponse:
    payload: dict[str, object] = {"result": result or {}, "diagnostic": message}
    try:
        stored = execution.store.finish_agent_command(
            command_id,
            status=status,
            payload=payload,
            message=message,
        )
    except ValueError:
        stored = execution.store.agent_command(command_id)
        if stored is None or stored.exited_at is None:
            raise
        return _recorded_child_reply_response(stored, request_id=request_id)
    return CommandResponse(
        request_id=request_id,
        status=stored.status or status,
        message=message,
        result=result or {},
    )


def _recorded_child_reply_response(
    invocation: AgentCommandInvocationRecord,
    *,
    request_id: str,
) -> CommandResponse:
    if invocation.status not in {"ok", "invalid", "unavailable"} or not isinstance(
        invocation.exit_payload, dict
    ):
        raise ValueError("Recorded child Work reply command exit is incomplete.")
    recorded_result = invocation.exit_payload.get("result")
    result = dict(recorded_result) if isinstance(recorded_result, dict) else {}
    diagnostic = invocation.exit_payload.get("diagnostic")
    message = diagnostic if isinstance(diagnostic, str) else None
    if invocation.status != "ok" and not message:
        message = "Recorded child Work reply command did not complete successfully."
    return CommandResponse(
        request_id=request_id,
        status=invocation.status,
        message=message,
        result=result,
    )


def _finish_child_reply_with_retry_attempt(
    execution: AgentTaskExecution,
    *,
    command_id: str,
    retry_command_id: str | None,
    request_id: str,
    status: Literal["ok", "invalid", "unavailable"],
    message: str,
    result: dict[str, object] | None = None,
) -> CommandResponse:
    response = _finish_child_reply_command(
        execution,
        command_id=command_id,
        request_id=request_id,
        status=status,
        message=message,
        result=result,
    )
    if retry_command_id is None:
        return response
    return _finish_child_reply_command(
        execution,
        command_id=retry_command_id,
        request_id=request_id,
        status=response.status,
        message=response.message or message,
        result=response.result,
    )


def _child_reply_matches(
    execution: AgentTaskExecution,
    route: AutoResearchChildWorkRecord,
    request: MessageCommandRequest,
    saved: AutoResearchMessageRecord,
) -> bool:
    episode = execution.store.episode(route.episode_id)
    sender_route = (
        execution.store.auto_research_child_work_for_operation(saved.sender_task_id)
        if saved.sender_task_id is not None
        else None
    )
    return bool(
        episode is not None
        and episode.root_operation_id is not None
        and sender_route is not None
        and sender_route.worker_id == route.worker_id
        and saved.episode_id == route.episode_id
        and saved.sender_role == "worker"
        and saved.authorized_by is None
        and saved.recipient_task_id == episode.root_operation_id
        and saved.control_node_id == route.control_node_id
        and saved.body == request.arguments.body
    )


def _dispatch_auto_research_child_reply(
    execution: AgentTaskExecution,
    route: AutoResearchChildWorkRecord,
    request: MessageCommandRequest,
) -> CommandResponse:
    """Persist one child-to-root reply without starting a concurrent root turn."""

    episode = execution.store.episode(route.episode_id)
    if episode is None or episode.mode != "auto_research" or episode.root_operation_id is None:
        return CommandResponse(
            request_id=request.request_id,
            status="unavailable",
            message="The Auto-research orchestrator recipient is unavailable.",
        )
    if request.arguments.recipient_task_id not in {None, episode.root_operation_id}:
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message="This child Work task may reply only to its Auto-research orchestrator.",
        )
    if request.idempotency_key is None:
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message="A child Work reply requires an idempotency key.",
        )
    start_payload = {
        "request_id": request.request_id,
        "arguments": request.arguments.model_dump(mode="json"),
        "planned_message_id": _child_reply_message_id(
            route.episode_id,
            request.idempotency_key,
        ),
    }
    prior = execution.store.agent_command_by_key(route.episode_id, request.idempotency_key)
    command_id = _unused_child_command_id(execution, request.request_id)
    retry_command_id: str | None = None
    if prior is None:
        try:
            invocation = execution.store.start_agent_command(
                operation_id=execution.operation_id,
                command_id=command_id,
                episode_id=route.episode_id,
                verb="message",
                idempotency_key=request.idempotency_key,
                payload=start_payload,
            )
        except ValueError:
            raced = execution.store.agent_command_by_key(
                route.episode_id,
                request.idempotency_key,
            )
            if raced is None:
                raise
            invocation = raced
        if invocation.command_id != command_id:
            prior = invocation
    if prior is not None:
        attempt = execution.store.start_agent_command(
            operation_id=execution.operation_id,
            command_id=command_id,
            episode_id=route.episode_id,
            verb="message",
            idempotency_key=None,
            payload={
                **start_payload,
                "idempotency_key": request.idempotency_key,
                "deduplicates_command_id": prior.command_id,
            },
        )
        prior_route = execution.store.auto_research_child_work_for_operation(prior.operation_id)
        if (
            prior.verb != "message"
            or prior.start_payload.get("arguments") != start_payload["arguments"]
            or prior.start_payload.get("planned_message_id") != start_payload["planned_message_id"]
            or prior_route is None
            or prior_route.worker_id != route.worker_id
        ):
            return _finish_child_reply_command(
                execution,
                command_id=attempt.command_id,
                request_id=request.request_id,
                status="invalid",
                message=(
                    "This idempotency key was already used by another actor or with different "
                    "reply arguments."
                ),
            )
        if prior.exited_at is not None:
            recorded = _recorded_child_reply_response(prior, request_id=request.request_id)
            return _finish_child_reply_command(
                execution,
                command_id=attempt.command_id,
                request_id=request.request_id,
                status=recorded.status,
                message=recorded.message or "The existing child Work reply was returned.",
                result=recorded.result,
            )
        invocation = prior
        retry_command_id = attempt.command_id

    planned_message_id = str(start_payload["planned_message_id"])
    saved = execution.store.auto_research_message(planned_message_id)
    disposition: Literal["created", "existing"] = "existing"
    if saved is None:
        saved = execution.store.record_auto_research_message(
            AutoResearchMessageRecord(
                message_id=planned_message_id,
                episode_id=route.episode_id,
                sender_role="worker",
                sender_task_id=execution.operation_id,
                recipient_task_id=episode.root_operation_id,
                control_node_id=route.control_node_id,
                body=request.arguments.body,
                created_at=execution.store.now(),
            )
        )
        disposition = "created"
    if not _child_reply_matches(execution, route, request, saved):
        return _finish_child_reply_with_retry_attempt(
            execution,
            command_id=invocation.command_id,
            retry_command_id=retry_command_id,
            request_id=request.request_id,
            status="unavailable",
            message="The durable child Work reply does not match this command intent.",
        )
    return _finish_child_reply_with_retry_attempt(
        execution,
        command_id=invocation.command_id,
        retry_command_id=retry_command_id,
        request_id=request.request_id,
        status="ok",
        message=(
            "Reply persisted for the Auto-research orchestrator's paid delivery."
            if saved.delivery_operation_id is not None
            else "Reply persisted for the Auto-research orchestrator's next paid delivery."
        ),
        result=_child_reply_result(saved, disposition=disposition),
    )


async def _serve_auto_research_child_work_mailbox(
    *,
    staged: StagedCommandMailbox,
    execution: AgentTaskExecution,
    route: AutoResearchChildWorkRecord,
    stop: asyncio.Event | threading.Event,
    budget: PatchValidationBudget,
    compute_commands: WorkComputeCommands,
    validate: Callable[[str], PatchValidationResult],
    responses: dict[str, CommandResponse] | None = None,
    terminal: dict[str, str] | None = None,
    checkpoint: Callable[[], None] | None = None,
    suspend: threading.Event | None = None,
) -> None:
    async def handle(
        request: CommandRequest,
        identity: CommandTurnIdentity,
    ) -> CommandResponse:
        if identity.episode_id != route.episode_id or identity.task_id != execution.operation_id:
            return CommandResponse(
                request_id=request.request_id,
                status="invalid",
                message="This child Work command credential is bound to another turn.",
            )
        if isinstance(request, ValidateCommandRequest):
            count = budget.reserve(request.request_id)
            if checkpoint is not None:
                checkpoint()
            if count > PATCH_SELF_CHECK_MAX_COUNT:
                result = PatchValidationResult(
                    status="unavailable",
                    messages=["This task has reached its bounded RCP validator self-check limit."],
                )
            else:
                result = await asyncio.to_thread(validate, request.arguments.patch)
            execution.store.record_agent_task_event(
                execution.operation_id,
                f"Patch self-check {count}/{PATCH_SELF_CHECK_MAX_COUNT}: {result.status}.",
                level="info" if result.status == "valid" else "warning",
            )
            execution.store.record_agent_task_receipt(
                execution.operation_id,
                "patch_self_check",
                {
                    "count": count,
                    "limit": PATCH_SELF_CHECK_MAX_COUNT,
                    **result.model_dump(mode="json"),
                },
                tier="diagnostic",
            )
            status = {
                "valid": "ok",
                "invalid": "invalid",
                "unavailable": "unavailable",
            }[result.status]
            diagnostic = " ".join(message.strip() for message in result.messages if message.strip())
            return CommandResponse(
                request_id=request.request_id,
                status=status,
                message=(diagnostic[:2_000] or None)
                if status == "ok"
                else (diagnostic[:2_000] or f"Patch validation was {result.status}."),
                result=result.model_dump(mode="json"),
            )
        if isinstance(request, MessageCommandRequest):
            return _dispatch_auto_research_child_reply(execution, route, request)
        if request.verb in compute_commands.allowed_verbs:
            return await asyncio.to_thread(compute_commands, request, identity)
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message=("This command is unavailable to this child Work turn."),
        )

    try:
        await serve_command_mailbox(
            staged=staged,
            handler=handle,
            stop=stop,
            poll_seconds=(
                PATCH_SELF_CHECK_POLL_SECONDS if staged.mailbox.remote_stage is None else None
            ),
            invocation_gate=staged.invocation_gate,
            record_rejection=command_rejection_recorder(execution),
            record_transport=command_transport_recorder(execution),
            responses=responses,
            terminal=terminal,
            checkpoint=checkpoint,
            suspend=suspend,
        )
    except (OSError, StateUnavailable, ValueError) as exc:
        execution.store.record_agent_task_event(
            execution.operation_id,
            f"Child Work command broker became unavailable: {' '.join(str(exc).split())[:400]}",
            level="warning",
        )
