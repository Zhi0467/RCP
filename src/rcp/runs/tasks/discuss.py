from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator
from contextlib import AsyncExitStack, aclosing, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rcp.agents import AgentEvent, AgentLauncher, PromptFactory
from rcp.agents.continuation_prompt import (
    LaunchPhase,
    MasterRef,
    changed_since_master,
    classify,
    compose,
    master_key,
)
from rcp.agents.prompts import (
    DISCUSS_POLICY_VERSION,
    _command_client_rule,
    chat_master_contract_key,
    invoked_package_pointers,
    live_ask_contract,
    render_chat_read_context,
)
from rcp.attachments import ChatAttachmentStore
from rcp.background import AgentTaskExecution
from rcp.config import AgentSurface
from rcp.conversation_worktrees import conversation_worktree_context
from rcp.history import ReplayHalted
from rcp.limits import COMMAND_CLIENT_WAIT_SECONDS, ask_hold_seconds
from rcp.providers.browser_grant import BrowserGrant, browser_prompt_line
from rcp.runs.browser_lifecycle import browser_turn
from rcp.runs.chat import (
    _append_chat_exchange,
    _chat_read_dirs,
    _chat_stage_name,
    _clear_stale_turn_handoffs,
    _commit_chat_prompt_state,
    _discover_chat_artifacts,
    _logical_chat_turn_operation_id,
    _prepare_chat_prompt_state,
    _prepare_local_artifact_directory,
    _prepare_local_chat_workspace,
    _read_chat_patch,
    _record_artifact_discovery_receipt,
    _record_chat_context_receipt,
    _retained_chat_patch_values,
    _stage_chat_patch_inputs,
    _stage_chat_turn_contract,
    _validated_local_chat_resume_stage,
    _validated_remote_chat_resume_stage,
    chat_continuation_master,
    chat_master_label,
    chat_master_owner,
    chat_prompt_values,
    finalize_artifact_edit,
    prepare_artifact_edit_directory,
    retained_work_values,
    stage_artifact_context,
)
from rcp.runs.experiment_loop import stage_chat_experiment_watcher_resources, stage_chat_loop_status
from rcp.runs.lessons import stage_lessons_pointer
from rcp.runs.patch_validator import cleanup_patch_validation_mailbox
from rcp.runs.question_snapshots import (
    QuestionSnapshot,
    question_snapshot,
    question_snapshot_part,
    record_question_snapshot_sent,
)
from rcp.runs.questions import (
    WorkCommandHandler,
    discuss_command_handler,
    require_question_origin_binding,
)
from rcp.runs.recorded_settlement import (
    absorb_recorded_events,
    attach_retained_stage,
    provider_turn_request,
    refuse_recorded_session_mismatch,
    retained_artifact_directory,
    write_recorded_patch,
)
from rcp.runs.recorded_settlement import (
    note_stage_unreachable as _note_stage_unreachable,
)
from rcp.runs.recorded_turn import RecordedProviderTurn, decode_recorded_turn
from rcp.runs.session_master import (
    record_inline_prompt,
    record_session_master,
    stage_session_master,
)
from rcp.runs.shared import (
    _pinned_to_profile,
    _prepare_hidden_read_scope,
    _protected_run_stage_roots,
    _ProviderOutcome,
    _record_agent_launch_receipt,
    _sse,
    _stage_context_paths,
    _stage_json_task_input,
    _stage_or_reuse_task_input,
    _stage_task_contract,
    _stream_agent_events,
    _swept_stage_root,
    _task_token,
    stage_branch_read_context,
)
from rcp.runs.tasks.work_turn_runtime import (
    WorkValidatorMailboxLifecycle,
    load_work_mailbox_context,
    restore_work_validator_mailbox,
    start_work_validator_mailbox,
)
from rcp.service import ProjectService, RunRequest
from rcp.skills.staging import skill_bundle_label, stage_skill_selection
from rcp.transport import RemoteRunStage, StateUnavailable


def _discuss_execution_instructions(
    handler: WorkCommandHandler, wait_seconds: float = COMMAND_CLIENT_WAIT_SECONDS
) -> str:
    if "ask" not in handler.allowed_verbs:
        return ""
    return "Only `ask` is available in Discuss.\n" + live_ask_contract(wait_seconds)


def resume_discuss_command_mailbox(
    service: Callable[[], ProjectService], execution: AgentTaskExecution
) -> WorkValidatorMailboxLifecycle | None:
    del service
    saved = load_work_mailbox_context(execution)
    if saved is None:
        return None
    handler = discuss_command_handler(execution)
    if saved.get("ask_allowed") is not True:
        handler = WorkCommandHandler(execution, None, frozenset())
    return restore_work_validator_mailbox(
        execution,
        command_handler=handler,
        validate=None,
    )


def _prepare_discuss_chat_prompt(
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
    ontology_extensions: bool,
    browser_grant: BrowserGrant,
    question_part: str = "",
    read_context: str = "",
) -> tuple[str, str]:
    """Prepare the session baseline behind one Discuss-local seam."""

    if request.message is None:
        raise ValueError("An ordinary Discuss turn requires a human message.")
    if request.artifact_edit is not None and request.artifact_edit.master_operation_id is not None:
        edit = request.artifact_edit
        assert (
            execution is not None
            and edit.master_sha256 is not None
            and edit.master_path is not None
        )
        path = stage_session_master(
            execution.store,
            local_stage=local_stage,
            remote_stage=remote_stage,
            operation_id=edit.master_operation_id,
            sha256=edit.master_sha256,
            path=edit.master_path,
        )
        node, master = "human_turn", MasterRef(path=path, bootstrap=False)
        context_delta = {
            "browser": browser_prompt_line(browser_grant),
            "discuss": {
                "execution_instructions": ["No RCP commands are available for this artifact edit."]
            },
        }
    else:
        node, master, context_delta = _prepare_chat_prompt_state(
            execution,
            request,
            local_stage=local_stage,
            remote_stage=remote_stage,
            master_context=master_context,
            contract_key=chat_master_contract_key(
                ontology_extensions=ontology_extensions, owner=chat_master_owner(execution)
            ),
            values={**stable_values, "browser": browser_prompt_line(browser_grant)},
        )
    prompt = PromptFactory.discuss_turn_prompt(
        artifact_path=artifact_path,
        human_message=request.message,
        node=node,
        master=master,
        lessons_pointer=stage_lessons_pointer(execution, local_stage, remote_stage),
        context_delta=context_delta,
        read_context=read_context,
        invoked_skill_pointers=invoked_package_pointers(
            skill_pointers,
            workflow_ids=request.invoked_workflow_ids,
            skill_ids=request.invoked_skill_ids,
        ),
        invoked_provider_skills=request.resolved_provider_skills,
        attachments=attachment_pointers,
    )
    if node == "session_start" and browser_grant.status != "not_requested":
        prompt += "\n\n" + browser_prompt_line(browser_grant)
    if question_part:
        prompt += "\n\n" + question_part
    return prompt, _stage_chat_turn_contract(execution, local_stage, remote_stage, prompt)


DISCUSS_FINALIZATION_CONTEXT_ROLE = "discuss_finalization_context"


class _StoredDiscussFinalizationContext(BaseModel):
    """Immutable launch snapshot consumed only after the provider has stopped."""

    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = 1
    request: RunRequest
    stage_host: str
    stage_root: str
    workspace: str
    artifact_scope_id: str
    artifact_directory: str
    # The question snapshot the prompt carried, so a recovered turn retires its dismissals.
    question_snapshot_text: str | None = None
    question_dismissal_ids: tuple[str, ...] = ()


@dataclass
class DiscussFinalizationContext:
    """The launch-time facts Discuss needs once its provider has stopped.

    Discuss holds no graph authority, no write scope and no watchers, so this is
    the whole of it: where the turn ran, and where its outputs were bounded. A
    recovered turn settles from these rather than from whatever the stage happens
    to hold when recovery arrives.
    """

    service: ProjectService
    request: RunRequest
    execution: AgentTaskExecution | None
    workspace: Path
    remote_stage: RemoteRunStage | None
    artifact_scope_id: str
    artifact_directory: Path | PurePosixPath
    outcome: _ProviderOutcome
    question_snapshot: QuestionSnapshot | None = None


def _record_discuss_finalization_context(context: DiscussFinalizationContext) -> None:
    """Write down what settling this turn needs, before the link can take it."""

    execution = context.execution
    if execution is None:
        return
    if execution.stage_root is None:
        raise ValueError("A durable Discuss finalization context requires its exact task stage.")
    stored = _StoredDiscussFinalizationContext(
        request=context.request,
        stage_host=execution.stage_host or "",
        stage_root=execution.stage_root,
        workspace=str(context.workspace),
        artifact_scope_id=context.artifact_scope_id,
        artifact_directory=str(context.artifact_directory),
        question_snapshot_text=context.question_snapshot.text
        if context.question_snapshot is not None
        else None,
        question_dismissal_ids=context.question_snapshot.dismissal_ids
        if context.question_snapshot is not None
        else (),
    )
    content = stored.model_dump_json()
    execution.store.record_agent_task_contract(
        execution.operation_id,
        DISCUSS_FINALIZATION_CONTEXT_ROLE,
        content,
        hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


def _load_discuss_finalization_context(
    service: ProjectService,
    request: RunRequest,
    execution: AgentTaskExecution,
) -> DiscussFinalizationContext:
    """Reopen finalization without rerunning any launch preparation."""

    content = execution.store.agent_task_contract(
        execution.operation_id,
        DISCUSS_FINALIZATION_CONTEXT_ROLE,
    )
    if content is None:
        raise ValueError("The Discuss turn has no retained finalization context.")
    try:
        stored = _StoredDiscussFinalizationContext.model_validate_json(content)
    except ValueError as exc:
        raise ValueError("The retained Discuss finalization context is invalid.") from exc
    task = execution.store.agent_task(execution.operation_id)
    if task is None or task.request != request.model_dump(mode="json"):
        raise ValueError("The Discuss finalization request does not match its durable task.")
    _local_stage, remote_stage, workspace = attach_retained_stage(
        execution,
        owner="Discuss",
        stage_host=stored.stage_host,
        stage_root=stored.stage_root,
        workspace=stored.workspace,
    )
    return DiscussFinalizationContext(
        service=service,
        request=stored.request,
        execution=execution,
        workspace=workspace,
        remote_stage=remote_stage,
        artifact_scope_id=stored.artifact_scope_id,
        artifact_directory=retained_artifact_directory(
            workspace,
            stored.artifact_scope_id,
            stored.artifact_directory,
            owner="Discuss",
        ),
        outcome=_ProviderOutcome(session_id=stored.request.session_id),
        question_snapshot=QuestionSnapshot(
            text=stored.question_snapshot_text, dismissal_ids=stored.question_dismissal_ids
        )
        if stored.question_snapshot_text is not None
        else None,
    )


def _warn_discuss_patch_discarded(execution: AgentTaskExecution, message: str) -> None:
    """Say this once for this task, however many times recovery reaches it.

    The receipt that guards the discard is written last, so a crash before it
    replays the whole discard. Its own message is what keeps this warning from
    being told twice; nothing else about a warning is unique.
    """

    if any(
        item.message == message
        for item in execution.store.agent_task_events(execution.operation_id)
    ):
        return
    execution.store.record_agent_task_event(
        execution.operation_id,
        message,
        level="warning",
    )


def _discard_discuss_patch(
    context: DiscussFinalizationContext,
    *,
    recorded: RecordedProviderTurn | None = None,
) -> None:
    """Keep a stray patch as evidence, never as a graph change.

    Authority to change the graph rides on the human's request. An agent cannot
    grant it to itself by writing the file. Recorded settlement can reach this
    twice, and can also die partway through it, so the receipt is written last
    and everything before it is safe to repeat: the patch text upserts and the
    warning knows whether it has already been told.

    A recorded pass hands over the Patch the host proved, so that settlement
    reads no stage at all here. Going back for a second look would let a host
    that went quiet turn this turn's evidence into a permanent `unreadable`.
    """

    execution = context.execution
    if execution is None or execution.store.agent_task_has_receipt(
        execution.operation_id, "discuss_patch_discarded"
    ):
        return
    if recorded is not None:
        _retain_discarded_discuss_patch(execution, recorded.patch)
        return
    try:
        patch_text = _read_chat_patch(context.workspace, context.remote_stage)
    except (OSError, StateUnavailable, ValueError) as exc:
        _note_stage_unreachable(execution, exc)
        _warn_discuss_patch_discarded(
            execution,
            "Discuss wrote an unreadable patch.json; RCP discarded it without changing the graph.",
        )
        execution.store.record_agent_task_receipt(
            execution.operation_id,
            "discuss_patch_discarded",
            {
                "reason": "unreadable",
                "detail": f"The agent wrote a patch file that could not be read: {exc}"[:400],
            },
            tier="diagnostic",
        )
        return
    _retain_discarded_discuss_patch(execution, patch_text)


def _retain_discarded_discuss_patch(
    execution: AgentTaskExecution,
    patch_text: str | None,
) -> None:
    """Write down the patch this turn produced and why it changes nothing."""

    if patch_text is None:
        return
    execution.store.record_agent_task_patch_output(execution.operation_id, patch_text)
    _warn_discuss_patch_discarded(
        execution,
        "Discuss has no graph authority, so the patch the agent wrote was "
        "discarded. Switch to Work for a deliberate graph update.",
    )
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "discuss_patch_discarded",
        {"reason": "no_graph_authority", "byte_length": len(patch_text.encode("utf-8"))},
        tier="diagnostic",
    )


def _settle_discuss_outcome(
    context: DiscussFinalizationContext,
    *,
    recorded: RecordedProviderTurn | None = None,
) -> Iterator[str]:
    """Turn one finished Discuss result into this task's durable output.

    The one door a Discuss result goes through, whether its provider streamed to
    this process or finished on a host that outlived the connection.
    """

    request = context.request
    execution = context.execution
    outcome = context.outcome
    # Only a labelled final assistant message is the reply. A provider that
    # emitted none has not answered, and promoting its last trace would show
    # reasoning or tool output to the human as if it were the answer.
    answer = "\n\n".join(item.strip() for item in outcome.answers if item.strip()).strip()
    if request.artifact_edit is not None and (outcome.failed or outcome.paused):
        return
    if not outcome.completed:
        if outcome.failed or outcome.paused:
            return
        outcome.failed = True
        yield _sse(AgentEvent(event="error", text=f"{request.provider} produced no result."))
        return
    if context.question_snapshot is not None and execution is not None:
        # Completion proves the prompt, and the dismissals in it, reached the provider.
        record_question_snapshot_sent(
            execution.store, context.question_snapshot, operation_id=execution.operation_id
        )
    if not answer:
        yield _sse(
            AgentEvent(event="error", text=f"{request.provider} finished without answering.")
        )
        return

    _commit_chat_prompt_state(execution, request, outcome.session_id)

    try:
        artifacts = _discover_chat_artifacts(
            execution,
            context.artifact_scope_id,
            Path(str(context.artifact_directory)),
            context.remote_stage,
            service=context.service,
        )
    except Exception as exc:
        # Preview attachments are optional. Even a programming or storage
        # error in this branch must not take down a labelled chat answer.
        with suppress(Exception):
            _record_artifact_discovery_receipt(
                execution,
                attached=0,
                candidates=0,
                ignored={"discovery_unavailable": 1},
                detail=str(exc),
            )
        artifacts = []
    artifacts = finalize_artifact_edit(
        request,
        execution,
        artifact_scope_id=context.artifact_scope_id,
        artifact_directory=Path(str(context.artifact_directory)),
        remote_stage=context.remote_stage,
        artifacts=artifacts,
        service=context.service,
    )
    yield _sse(AgentEvent(event="answer", text=answer))
    for artifact in artifacts:
        yield _sse(AgentEvent(event="artifact", artifact=artifact))

    _discard_discuss_patch(context, recorded=recorded)

    try:
        _append_chat_exchange(
            context.service,
            request,
            answer,
            outcome.session_id,
            None,
            execution=execution,
        )
    except (OSError, StateUnavailable, ValueError) as exc:
        _note_stage_unreachable(execution, exc)
        if execution is not None:
            execution.store.record_agent_task_event(
                execution.operation_id,
                f"The reply was delivered but could not be written to the chat transcript: {exc}",
                level="warning",
            )
    yield _sse(AgentEvent(event="done"))


async def finalize_recorded_discuss_result(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    execution: AgentTaskExecution,
    recorded: RecordedProviderTurn,
) -> AsyncIterator[str]:
    """Settle a Discuss turn from the pass its host recorded.

    No launcher and no data directory: Discuss recovery reads a record and the
    facts its own launch retained, and starts nothing. It writes under the
    operation id that opened the pass, because it is the same turn.
    """

    del launcher, data_dir  # Recovery launches nothing and stages nothing.
    context = _load_discuss_finalization_context(service, request, execution)
    verdict = decode_recorded_turn(recorded, provider_turn_request(context.workspace, recorded))
    question = execution.store.question_for_followup(execution.operation_id)
    required_session = None
    if question is not None:
        require_question_origin_binding(
            execution,
            context.request,
            question.origin,
            execution_host=context.remote_stage.host if context.remote_stage else "",
            stage_root=execution.stage_root,
            write_scope_fingerprint=None,
        )
        required_session = question.origin.native_session_id
    refusal = refuse_recorded_session_mismatch(
        execution, context.outcome, verdict, required_session
    )
    if refusal:
        for frame in refusal:
            yield frame
        return
    # A discarded Patch is still this turn's evidence. The stage is mutable and
    # the record is not, so restore what the host proved before reading it.
    write_recorded_patch(context.workspace, context.remote_stage, recorded)
    for frame in absorb_recorded_events(context.outcome, verdict):
        yield frame
    for frame in _settle_discuss_outcome(context, recorded=recorded):
        yield frame


async def stream_discuss_run(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    execution: AgentTaskExecution | None = None,
) -> AsyncGenerator[str, None]:
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
    patch_inputs = None
    mailbox_lifecycle = None
    snapshot = None
    primary_error = None
    outcome = _ProviderOutcome(session_id=request.session_id)
    browser_stack = AsyncExitStack()
    try:
        try:
            question = (
                execution.store.question_for_followup(execution.operation_id)
                if execution is not None
                else None
            )
            required_session_id = request.session_id if request.artifact_edit else None
            if question is not None:
                assert execution is not None
                require_question_origin_binding(
                    execution,
                    request,
                    question.origin,
                    execution_host=execution_host,
                    stage_root=execution.stage_root,
                    write_scope_fingerprint=None,
                )
                required_session_id = question.origin.native_session_id
            context = service.assemble_chat(request)
            if execution is not None:
                task = execution.store.agent_task(execution.operation_id)
                if task is None:
                    raise ValueError("The conversation task binding is unavailable.")
                context = conversation_worktree_context(
                    service, execution.store, task.project_id, request, context
                )
            _record_chat_context_receipt(execution, context, surface=surface)
            # One scratch folder per conversation, not per turn. Resuming a native
            # session means resuming it in the directory it was given — Claude keys
            # its sessions by that directory — so every turn of a chat, local or
            # remote, reuses the same folder and _sweep_stale_stages ages it out.
            stage_name = _chat_stage_name(service, request, execution)
            saved_stage = execution is not None and execution.stage_root is not None
            if execution_host:
                if saved_stage:
                    stage_root = _validated_remote_chat_resume_stage(
                        execution, execution_host, stage_name
                    )
                    remote_stage = RemoteRunStage(execution_host).attach(stage_root)
                else:
                    remote_stage = RemoteRunStage(execution_host).open(
                        stage_name,
                        reuse=True,
                        protected_roots=_protected_run_stage_roots(
                            execution.store if execution is not None else None,
                            execution_host,
                        ),
                    )
                assert remote_stage.root is not None
                remote_stage.touch()
                if execution is not None:
                    execution.checkpoint_stage(execution_host, str(remote_stage.root))
                # Resumes rebuild context too; branch read pointers need execution-host paths.
                context = context.model_copy(
                    update=_stage_context_paths(
                        context, service, remote_stage, execution_machine.alias
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
            hidden_read_scope = await _prepare_hidden_read_scope(
                service,
                request,
                workspace=workspace,
                remote_stage=remote_stage,
                execution=execution,
                capability="discuss",
                execution_host=execution_host,
                data_dir=data_dir,
                local_stage=local_stage,
            )
            browser_grant = await browser_stack.enter_async_context(
                browser_turn(
                    request,
                    workspace=workspace,
                    execution_host=execution_host,
                    execution=execution,
                    remote_stage=remote_stage,
                    capability="discuss",
                    hidden_read_scope=hidden_read_scope,
                )
            )
            if not reusing_checkpoint:
                # A reused folder must not hand this turn any previous turn output.
                _clear_stale_turn_handoffs(workspace, remote_stage)
            artifact_scope_id = (
                request.artifact_edit.staged_scope_id
                if request.artifact_edit is not None
                else _logical_chat_turn_operation_id(execution.store, execution.operation_id)
                if execution is not None and resuming
                else execution.operation_id
                if execution is not None
                else str(uuid.uuid4())
            )
            if request.artifact_edit is not None:
                assert execution is not None
                artifact_directory = prepare_artifact_edit_directory(
                    request,
                    execution,
                    workspace,
                    remote_stage,
                )
            elif remote_stage is not None:
                artifact_directory = remote_stage.prepare_artifact_directory(
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

            context = context.model_copy(
                update=stage_branch_read_context(context, service, local_stage, remote_stage)
            )
            context = context.model_copy(
                update={
                    "loop_status": stage_chat_loop_status(
                        request,
                        execution,
                        local_stage,
                        remote_stage,
                        state=service.history.state(),
                        graph_target=service.history.graph_target,
                    )
                }
            )
            token = _task_token(execution)
            experiment_resources = await stage_chat_experiment_watcher_resources(
                request,
                execution,
                local_stage,
                remote_stage,
                workspace=workspace,
                token=token,
                clear_stale=not reusing_checkpoint,
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
            artifact_context_pointer = stage_artifact_context(
                service,
                request,
                execution,
                local_stage=local_stage,
                remote_stage=remote_stage,
                artifact_path=str(artifact_directory),
            )
            if artifact_context_pointer is not None:
                attachment_pointers.append(artifact_context_pointer.pointer)
            read_dirs = _chat_read_dirs(
                context,
                local_stage,
                remote_stage,
                service,
                execution_machine.alias,
            )
            read_dirs.extend(
                path
                for path in dict.fromkeys(
                    Path(str(item["path"])).parent for item in attachment_pointers
                )
                if path not in read_dirs
            )
            if reusing_checkpoint and not request.session_id:
                raise ValueError(
                    "The continued chat has no native agent session; retry it from a clean "
                    "attempt instead."
                )
            repositories = [
                {"alias": item.alias, "host": item.host, "path": item.path}
                for item in context.repositories
            ]
            compute_profiles = service.compute_prompt_profiles(request.resolved_compute_context)
            discuss_values = chat_prompt_values(
                context,
                request,
                repositories=repositories,
                compute_profiles=compute_profiles,
                skill_pointers=skill_pointers,
                experiment_watcher_resources=experiment_resource_pointers,
                workspace=str(workspace),
            )
            handler = discuss_command_handler(execution)
            if request.artifact_edit is not None:
                handler = WorkCommandHandler(execution, None, frozenset())
            eligible = "ask" in handler.allowed_verbs
            retained = _retained_chat_patch_values(
                execution, request, ontology_extensions=context.ontology_extensions
            )
            if eligible or (
                retained is None
                and (request.artifact_edit is not None or not (resuming or retry_attempt))
            ):
                shell_timeout = (
                    execution.store.provider_shell_timeout_seconds(profile.provider, execution_host)
                    if execution is not None
                    else None
                )
                patch_inputs = _stage_chat_patch_inputs(
                    local_stage,
                    remote_stage,
                    workspace=workspace,
                    stage_name=stage_name,
                    task_id=execution.operation_id if execution is not None else token,
                    turn_id=f"{token}:discuss",
                    broker=eligible,
                    ask_wait_seconds=ask_hold_seconds(shell_timeout),
                    shell_timeout_seconds=shell_timeout,
                )
                retained = {**(retained or {}), "patch": patch_inputs.prompt_values()}
            instructions = ""
            if eligible:
                assert patch_inputs is not None
                mailbox_lifecycle = start_work_validator_mailbox(
                    patch_inputs.validator_staged,
                    execution=execution,
                    validate=None,
                    budget=None,
                    command_handler=handler,
                    resume_context={"ask_allowed": "ask" in handler.allowed_verbs},
                )
                instructions = _discuss_execution_instructions(
                    handler, patch_inputs.validator_staged.ask_wait_seconds
                )
                discuss_values["patch"] = patch_inputs.prompt_values()
                assert execution is not None and request.chat_id is not None
                task = execution.store.agent_task(execution.operation_id)
                assert task is not None
                snapshot = question_snapshot(
                    execution.store,
                    project_id=task.project_id,
                    owner_kind="chat",
                    owner_ids=[request.chat_id],
                    operation_id=execution.operation_id,
                )
            question_part = (
                question_snapshot_part(snapshot, local_stage=local_stage, remote_stage=remote_stage)
                if snapshot is not None
                else ""
            )
            # Keep multiline prose in one structured overlay value.
            discuss_values["discuss"] = {"execution_instructions": instructions.splitlines()}
            if (resuming or retry_attempt) and request.artifact_edit is None:
                assert request.message is not None
                retry_diagnostics_path = (
                    _stage_json_task_input(
                        local_stage,
                        remote_stage,
                        f"task-{token}-retry-diagnostics.json",
                        {"prior_attempt_diagnostics": list(execution.retry_feedback)},
                    )
                    if execution is not None and retry_attempt
                    else None
                )

                def render_discuss_contract() -> str:
                    assert request.message is not None
                    human_request_path = _stage_or_reuse_task_input(
                        local_stage,
                        remote_stage,
                        f"task-{token}-human-request.txt",
                        request.message,
                    )
                    contract = PromptFactory.discuss_task_contract(
                        project_name=context.project_name,
                        ontology_path=f"{context.graph_path}#ontology",
                        ontology_extensions=context.ontology_extensions,
                        graph_path=context.graph_path,
                        research_path=context.research_md_path,
                        focused_node_id=str(context.node["id"]) if context.node else None,
                        repositories=repositories,
                        introduction_path=context.introduction_path,
                        human_request_path=human_request_path,
                        artifact_path=str(artifact_directory),
                        retry_diagnostics_path=retry_diagnostics_path,
                        experiment_watcher_resources=experiment_resource_pointers,
                        skill_pointers=skill_pointers,
                        invoked_skill_pointers=invoked_package_pointers(
                            skill_pointers,
                            workflow_ids=request.invoked_workflow_ids,
                            skill_ids=request.invoked_skill_ids,
                        ),
                        invoked_provider_skills=request.resolved_provider_skills,
                        attachments=attachment_pointers,
                        compute_connections=compute_profiles,
                        execution_instructions=(
                            _command_client_rule(patch_inputs.command_client) + "\n" + instructions
                            if patch_inputs is not None and instructions
                            else ""
                        ),
                    )
                    return contract + "\n\n" + browser_prompt_line(browser_grant)

                if resuming or retrying:
                    assert execution is not None and request.session_id is not None
                    recovery_mode: Literal["resume", "retry"] = "resume" if resuming else "retry"
                    node = classify(LaunchPhase(session_id=request.session_id, phase="recovery"))
                    master = chat_continuation_master(
                        execution,
                        request,
                        session_id=request.session_id,
                        local_stage=local_stage,
                        remote_stage=remote_stage,
                        policy_version=DISCUSS_POLICY_VERSION,
                        ontology_extensions=context.ontology_extensions,
                        render=render_discuss_contract,
                        values=discuss_values,
                    )
                    # Discuss changes no Work value, so it keeps those its session holds.
                    current = {**retained_work_values(master.values), **discuss_values}
                    continuation_contract = PromptFactory.inline_continuation(
                        mode=recovery_mode,
                        turn_mode="discuss",
                        diagnostics_path=retry_diagnostics_path,
                        artifact_path=str(artifact_directory),
                    )
                    prompt = compose(
                        node,
                        parts=[
                            continuation_contract,
                            render_chat_read_context(context),
                            stage_lessons_pointer(execution, local_stage, remote_stage),
                            question_part,
                        ],
                        master=master,
                        delta={
                            **(changed_since_master(master, current) or {}),
                            "browser": browser_prompt_line(browser_grant),
                        },
                    )
                    contract_path = record_inline_prompt(
                        execution,
                        local_stage=local_stage,
                        remote_stage=remote_stage,
                        label=f"task-{token}-{recovery_mode}.md",
                        role=f"discuss_{recovery_mode}",
                        prompt=prompt,
                    )
                else:
                    # A handoff starts a new native session from the full Discuss contract.
                    contract = render_discuss_contract()
                    if question_part:
                        contract += "\n\n" + question_part
                    contract_path, prompt = _stage_task_contract(
                        local_stage,
                        remote_stage,
                        f"task-{token}-base.md",
                        contract,
                        execution=execution,
                        role="discuss_retry_base",
                    )
                    prompt += "\n\n" + render_chat_read_context(context)
                    prompt += "\n\n" + stage_lessons_pointer(execution, local_stage, remote_stage)
                    if execution is not None:
                        record_session_master(
                            execution.store,
                            execution.operation_id,
                            contract,
                            master_key(
                                chat_master_label(DISCUSS_POLICY_VERSION, execution),
                                ontology_extensions=context.ontology_extensions,
                            ),
                            discuss_values,
                        )
            else:
                assert request.message is not None
                assert artifact_scope_id is not None
                assert retained is not None
                patch_values = retained["patch"]
                assert isinstance(patch_values, dict)
                stable_prompt_values = {**discuss_values, **retained}
                focused_node_id = str(context.node["id"]) if context.node else None
                master_context = PromptFactory.chat_master_context(
                    project_name=context.project_name,
                    ontology_path=f"{context.graph_path}#ontology",
                    ontology_extensions=context.ontology_extensions,
                    graph_path=context.graph_path,
                    research_path=context.research_md_path,
                    graph_revision=context.graph_revision,
                    focused_node_id=focused_node_id,
                    focused_node=context.node,
                    focused_relations=[item.model_dump(mode="json") for item in context.relations],
                    repositories=repositories,
                    introduction_path=context.introduction_path,
                    patch_path=patch_values["path"],
                    workspace_path=str(workspace),
                    output_schema_path=patch_values["schema_path"],
                    command_client=patch_values["command_client"],
                    watch_path=patch_values["watch_path"],
                    execution_host=execution_host,
                    experiment_watcher_resources=experiment_resource_pointers,
                    skill_pointers=skill_pointers,
                    compute_connections=compute_profiles,
                    discuss_execution_instructions=instructions,
                )
                prompt, contract_path = _prepare_discuss_chat_prompt(
                    execution,
                    request,
                    local_stage=local_stage,
                    remote_stage=remote_stage,
                    artifact_path=str(artifact_directory),
                    master_context=master_context,
                    stable_values=stable_prompt_values,
                    question_part=question_part,
                    read_context=render_chat_read_context(context),
                    browser_grant=browser_grant,
                    skill_pointers=skill_pointers,
                    attachment_pointers=attachment_pointers,
                    ontology_extensions=context.ontology_extensions,
                )
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
        assert artifact_scope_id is not None
        assert artifact_directory is not None
        settlement = DiscussFinalizationContext(
            service=service,
            request=request,
            execution=execution,
            workspace=workspace,
            remote_stage=remote_stage,
            artifact_scope_id=artifact_scope_id,
            artifact_directory=artifact_directory,
            outcome=outcome,
            question_snapshot=snapshot,
        )
        if execution_host:
            # Only a remote turn can outlive this connection, and only a turn
            # whose settling facts are already written down can be recovered.
            _record_discuss_finalization_context(settlement)
        try:
            async with aclosing(
                _stream_agent_events(
                    launcher,
                    request,
                    prompt,
                    service=service,
                    hidden_read_scope=hidden_read_scope,
                    workspace=workspace,
                    session_id=request.session_id,
                    required_session_id=required_session_id,
                    read_dirs=read_dirs,
                    write_dirs=[],
                    write_scope=None,
                    execution_host=execution_host,
                    execution=execution,
                    remote_stage=remote_stage,
                    capability="discuss",
                    shell_timeout_seconds=(
                        patch_inputs.validator_staged.shell_timeout_seconds if patch_inputs else ...
                    ),
                    invocation_gate=patch_inputs.validator_staged.invocation_gate
                    if patch_inputs
                    else None,
                    browser_grant=browser_grant,
                    outcome=outcome,
                    binary=provider_binary,
                    supervise_remote=bool(execution_host),
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

        if outcome.remote_result_pending:
            # The host has this turn now. Its original task waits, and the
            # reconciler settles it through the same door below.
            return
        if mailbox_lifecycle is not None:
            await mailbox_lifecycle.close()
        for frame in _settle_discuss_outcome(settlement):
            yield frame
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        # There is no per-turn source cleanup; the reusable native-session stage
        # remains available to the normal stage sweeper.
        try:
            if mailbox_lifecycle is not None:
                if outcome.remote_result_pending:
                    mailbox_lifecycle.detach()
                else:
                    await mailbox_lifecycle.close(primary_error=primary_error)
            elif patch_inputs is not None:
                await asyncio.to_thread(
                    cleanup_patch_validation_mailbox,
                    staged=patch_inputs.validator_staged,
                    execution=execution,
                )
        finally:
            await browser_stack.aclose()
