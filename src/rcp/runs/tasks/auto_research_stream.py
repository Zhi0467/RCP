from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, aclosing, asynccontextmanager, suppress
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypeVar, cast

from pydantic import BaseModel, ConfigDict

from rcp.agents import (
    AgentEvent,
    AgentLauncher,
    ChatContext,
    ContextAssembler,
    agent_output_schema,
)
from rcp.agents.auto_research_commands import auto_research_allowed_verbs
from rcp.agents.auto_research_prompt import (
    AUTO_RESEARCH_POLICY_VERSION,
    auto_research_branch_read_pointers,
    auto_research_continuation_parts,
    auto_research_orchestrator_continuation_contract,
    auto_research_orchestrator_task_contract,
    auto_research_patch_correction_parts,
    auto_research_prompt_values,
    auto_research_worker_task_contract,
)
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
    ValidateCommandRequest,
)
from rcp.agents.continuation_prompt import (
    LaunchPhase,
    MasterRef,
    changed_since_master,
    classify,
    compose,
    graph_head_delta,
    master_key,
)
from rcp.agents.graph_read_scope import GraphReadScope
from rcp.agents.group_context import GroupContext, build_group_context
from rcp.agents.loop_overlap import render_auto_research_overlap, render_loop_overlap
from rcp.agents.prompts import render_group_context
from rcp.agents.write_scope import ProjectWriteScope
from rcp.background import AgentTaskExecution
from rcp.core.research_md import render_research_md
from rcp.core.transition_models import GraphHeadRef
from rcp.limits import PATCH_CORRECTION_MAX_ROUNDS
from rcp.loop_status import other_branch_auto_research, other_branch_loops
from rcp.providers import classify_terminal_error
from rcp.providers.browser_grant import BrowserGrant, browser_prompt_line
from rcp.runs.agent_names import resolve_agent_name_state
from rcp.runs.auto_research import (
    AutoResearchCommandContext,
    AutoResearchCommandDispatcher,
    AutoResearchCommandEffectResult,
    AutoResearchCommandInvalid,
    AutoResearchRunRequest,
)
from rcp.runs.auto_research_admission import (
    ChildSeatProfileChoices,
    auto_research_child_seat_profile_choices,
)
from rcp.runs.auto_research_lifecycle import (
    AUTO_RESEARCH_LIFECYCLE_HANDOFF_FILE,
    auto_research_lifecycle_delivery,
    stage_auto_research_lifecycle_delivery,
)
from rcp.runs.auto_research_mail import (
    AUTO_RESEARCH_MAIL_HANDOFF_FILE,
    auto_research_mail_delivery,
    stage_auto_research_mail_delivery,
)
from rcp.runs.auto_research_questions import (
    auto_research_question_snapshot,
    mark_auto_research_question_snapshot_delivered,
)
from rcp.runs.browser_lifecycle import browser_turn
from rcp.runs.chat import (
    _chat_read_dirs,
    _clear_stale_turn_handoffs,
    _project_write_scope,
    _read_chat_patch,
)
from rcp.runs.lessons import stage_operational_context_pointer
from rcp.runs.patch_validator import command_rejection_recorder
from rcp.runs.recorded_settlement import (
    absorb_recorded_events,
    attach_retained_stage,
    provider_turn_request,
    refuse_recorded_session_mismatch,
    write_recorded_patch,
)
from rcp.runs.recorded_turn import RecordedProviderTurn, decode_recorded_turn
from rcp.runs.session_master import (
    commit_prepared_graph_observation,
    continuation_session_master,
    merge_prepared_graph_observation_delta,
    prepare_graph_observation,
    record_inline_prompt,
    record_session_master,
)
from rcp.runs.shared import (
    _existing_exact_patch_digest,
    _parent_task_contract_path,
    _prepare_hidden_read_scope,
    _protected_run_stage_roots,
    _ProviderOutcome,
    _record_agent_launch_receipt,
    _retry_deliverable_is_unchanged,
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
from rcp.runs.tasks.work import (
    _apply_work_patch,
    _bounded_graph_messages,
    _CorrectionPatchRead,
    _read_correction_patch,
    _record_work_graph_rejection,
    _WorkPatchFailure,
)
from rcp.runs.tasks.work_turn_runtime import (
    WorkValidatorMailboxLifecycle,
    failed_graph_update,
    load_work_mailbox_context,
    restore_work_validator_mailbox,
    start_work_validator_mailbox,
)
from rcp.service import GraphUpdateResult, ProjectService, RunRequest
from rcp.skills.staging import skill_bundle_label, stage_skill_selection
from rcp.storage import (
    AgentTaskRecord,
    AutoResearchActorBinding,
    AutoResearchLifecycleNoticeRecord,
    AutoResearchMessageRecord,
)
from rcp.storage.lessons import lesson_command_actions
from rcp.transport import (
    RemoteRunStage,
    RunLockCancelled,
    RunStageMailbox,
    StateUnavailable,
    repository_access,
)
from rcp.transport.run_stage import remote_stage_name

_SAME_ALLOCATION_RECOVERY = frozenset({"resume", "retry"})
_HANDOFFS_CLEARED_RECEIPT = "auto_research_worker_handoffs_cleared"
_WORKER_CONTINUATIONS = frozenset(
    {
        "fresh",
        "resume",
        "retry",
        "watcher_wake",
        "graph_condition_wake",
        "message_wake",
        "auto_research_continuation",
    }
)
_ORCHESTRATOR_CONTINUATIONS = _WORKER_CONTINUATIONS | {"lifecycle_wake"}
# The node an actor launch is when it hands the provider a saved session. Only a requeued
# fresh allocation carries one; it continues rather than restarting the session.
_LAUNCH_PHASES: dict[str, Literal["wake", "recovery"]] = {
    "fresh": "recovery",
    "resume": "recovery",
    "retry": "recovery",
    "watcher_wake": "wake",
    "graph_condition_wake": "wake",
    "message_wake": "wake",
    "lifecycle_wake": "wake",
    "auto_research_continuation": "wake",
}


_LOGGER = logging.getLogger(__name__)
_Item = TypeVar("_Item")


@dataclass(frozen=True)
class _CanonicalWorkerTurn:
    task: AgentTaskRecord
    request: AutoResearchRunRequest
    binding: AutoResearchActorBinding
    allocation_operation_id: str
    recovering_allocation: bool


@dataclass(frozen=True)
class _CanonicalOrchestratorTurn:
    task: AgentTaskRecord
    request: AutoResearchRunRequest
    binding: AutoResearchActorBinding
    allocation_operation_id: str
    recovering_allocation: bool
    clean_session_retry: bool


@dataclass(frozen=True)
class _WorkerStage:
    local: Path | None
    remote: RemoteRunStage | None
    workspace: Path
    execution_host: str
    provider_binary: str | None


@dataclass(frozen=True)
class _PatchSettlement:
    graph_update: GraphUpdateResult | None
    frames: tuple[str, ...] = ()
    had_patch: bool = False
    # A correction pass lost its link; the turn now waits for its recorded result.
    awaiting_remote_result: bool = False


AUTO_RESEARCH_FINALIZATION_ROLE = "auto_research_finalization"
# A correction has its own journal, and its prose is not the turn's answer.
_AUTO_RESEARCH_PRIMARY_ANSWER_ROLE = "auto_research_primary_answer"


class _AutoResearchFinalization(BaseModel):
    """The launch facts a recorded pass needs that durable rows do not hold."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage_host: str
    stage_root: str
    workspace: str
    write_scope: ProjectWriteScope
    retry_patch_digest: str | None


@dataclass(frozen=True)
class _Correction:
    """What only a live correction round needs; recorded settlement passes none."""

    launcher: AgentLauncher
    master: MasterRef
    values: dict[str, object]
    read_dirs: list[Path]
    write_dirs: list[Path]
    provider_binary: str | None
    staged_head: GraphHeadRef | None
    group_context: GroupContext | None
    command_dispatcher: AutoResearchCommandDispatcher


async def stream_auto_research_orchestrator_run(
    service: ProjectService,
    launcher: AgentLauncher,
    request: AutoResearchRunRequest,
    data_dir: Path,
    execution: AgentTaskExecution,
    *,
    command_dispatcher: AutoResearchCommandDispatcher,
) -> AsyncIterator[str]:
    """Run one paid turn of the sole project-owned auto_research orchestrator."""

    browser_stack = AsyncExitStack()
    try:
        turn = _canonical_orchestrator_turn(execution, request)
        if command_dispatcher.store is not execution.store:
            raise ValueError(
                "auto_research orchestrator stream and command dispatcher must share one store"
            )
        stage = _open_orchestrator_stage(service, data_dir, execution, turn)
        hidden_read_scope = await _prepare_hidden_read_scope(
            service,
            turn.request,
            workspace=stage.workspace,
            remote_stage=stage.remote,
            execution=execution,
            capability="orchestrate",
            execution_host=stage.execution_host,
            data_dir=data_dir,
            local_stage=stage.local,
        )
        browser_grant = await browser_stack.enter_async_context(
            browser_turn(
                turn.request,
                workspace=stage.workspace,
                execution_host=stage.execution_host,
                execution=execution,
                remote_stage=stage.remote,
                capability="orchestrate",
                hidden_read_scope=hidden_read_scope,
            )
        )
        context = _orchestrator_launch_context(service, turn.request, stage)
        context.group_context = build_group_context(
            service, execution.store, turn.request.run_on or ""
        )
        graph_read_scope = GraphReadScope.resolve(
            turn.task.project_id, service.history, context.group_context, store=service.paper.store
        )
        # The one command surface this turn's prompt renders and its dispatcher enforces.
        lesson_actions = lesson_command_actions(execution.store, turn.task.operation_id)
        allowed_verbs = auto_research_allowed_verbs(
            "orchestrator",
            can_self_name=bool(resolve_agent_name_state(execution).get("can_self_name")),
            graph_read_scope=graph_read_scope,
            bulletin=execution.store.bulletin_enabled_for_task(turn.task.operation_id),
            lesson_actions=lesson_actions,
        )
        command_dispatcher = _bind_actor_dispatcher(
            command_dispatcher, service, turn.request, stage, graph_read_scope, allowed_verbs
        )
        _prepare_turn_handoffs(execution, turn, stage)

        messages_path = _stage_claimed_mail(execution, turn, stage)
        lifecycle_path = _stage_claimed_lifecycle(execution, turn, stage)
        token = _task_token(execution)
        schema = (
            json.dumps(
                agent_output_schema(profile="orchestrator"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        schema_digest = hashlib.sha256(schema.encode("utf-8")).hexdigest()[:16]
        schema_path = _stage_or_reuse_task_input(
            stage.local,
            stage.remote,
            f"auto_research-orchestrator-patch-schema-{schema_digest}.json",
            schema,
        )
        patch_path = str(stage.workspace / "patch.json")
        # The orchestrator gets the project's enabled packages on the same terms as any
        # other Work agent: resolved from Settings at launch, never from the task receipt.
        orchestrator_selection = service.resolve_skill_selection(turn.request)
        orchestrator_skill_pointers = stage_skill_selection(
            orchestrator_selection,
            local_stage=stage.local,
            remote_stage=stage.remote,
            label=skill_bundle_label(orchestrator_selection),
            reuse_existing=True,
        )
        staged_head = prepare_graph_observation(
            execution,
            service,
            owner_id=_actor_seen_owner(turn),
            target=graph_read_scope.target,
            revision=context.graph_revision,
            native_session_id=turn.binding.native_session_id,
        )
        expected_turn_id = f"{execution.operation_id}:orchestrator"
        staged_commands = stage_command_mailbox(
            local_stage=stage.local,
            remote_stage=stage.remote,
            episode_id=turn.request.episode_id,
            task_id=execution.operation_id,
            turn_id=expected_turn_id,
        )
        outcome = _ProviderOutcome(session_id=turn.binding.native_session_id)
        async with _actor_mailbox(
            staged_commands,
            execution=execution,
            outcome=outcome,
            serve=partial(
                _serve_auto_research_commands,
                staged_commands,
                execution=execution,
                episode_id=turn.request.episode_id,
                dispatcher=command_dispatcher,
                service=service,
                graph_read_scope=graph_read_scope,
                expected_turn_id=expected_turn_id,
            ),
            resume_context=_actor_mailbox_context(
                expected_turn_id, graph_read_scope, allowed_verbs
            ),
        ):
            write_scope = _project_write_scope(
                context,
                service,
                turn.request.run_on or "",
                local_stage=stage.local,
                workspace=stage.workspace,
                remote_stage=stage.remote,
                data_dir=data_dir,
                execution=execution,
                capability="orchestrate",
                episode_request=turn.request,
            )
            contract_path, prompt, master, values = _orchestrator_prompt(
                service,
                execution,
                turn,
                context=context,
                local_stage=stage.local,
                remote_stage=stage.remote,
                token=token,
                patch_path=patch_path,
                schema_path=schema_path,
                command_client=staged_commands.client_command(),
                allowed_verbs=allowed_verbs,
                lesson_actions=lesson_actions,
                graph_read_scope=graph_read_scope,
                staged_head=staged_head,
                messages_path=messages_path,
                lifecycle_path=lifecycle_path,
                skill_pointers=orchestrator_skill_pointers,
                write_scope=write_scope,
                browser_grant=browser_grant,
                child_profiles=auto_research_child_seat_profile_choices(service),
            )
            read_dirs = _chat_read_dirs(
                context,
                stage.local,
                stage.remote,
                service,
                turn.request.run_on or "",
            )
            if context.group_context is not None:
                read_dirs.extend(context.group_context.read_dirs())
            write_dirs = [Path(item) for item in write_scope.repository_roots]
            retry_patch_digest = (
                _existing_exact_patch_digest(stage.workspace, stage.remote)
                if execution.continuation == "retry"
                else None
            )
            _record_auto_research_finalization(execution, stage, write_scope, retry_patch_digest)
            _record_agent_launch_receipt(
                execution,
                cast(RunRequest, turn.request),
                prompt=prompt,
                contract_path=contract_path,
                remote=bool(stage.execution_host),
                resumed=turn.binding.native_session_id is not None,
                write_scope=write_scope,
                continuation=execution.continuation,
                extra={
                    "surface": "auto_research",
                    "role": "orchestrator",
                    "profile": "orchestrator",
                    "actor_operation_id": turn.binding.actor_operation_id,
                    "allocation_operation_id": turn.allocation_operation_id,
                    "capability": "orchestrate",
                    "network_access": True,
                    "write_directory_count": len(write_dirs),
                },
            )
            async with aclosing(
                _stream_agent_events(
                    launcher,
                    cast(RunRequest, turn.request),
                    prompt,
                    service=service,
                    hidden_read_scope=hidden_read_scope,
                    workspace=stage.workspace,
                    session_id=turn.binding.native_session_id,
                    read_dirs=read_dirs,
                    write_dirs=write_dirs,
                    write_scope=write_scope,
                    execution_host=stage.execution_host,
                    execution=execution,
                    remote_stage=stage.remote,
                    capability="orchestrate",
                    browser_grant=browser_grant,
                    outcome=outcome,
                    binary=stage.provider_binary,
                    invocation_gate=staged_commands.invocation_gate,
                )
            ) as stream:
                async for frame in stream:
                    yield frame
        if outcome.remote_result_pending:
            return
        async for frame in _settle_actor_turn(
            service,
            execution,
            turn,
            stage,
            outcome,
            write_scope=write_scope,
            retry_patch_digest=retry_patch_digest,
            correction=_Correction(
                launcher=launcher,
                master=master,
                values=values,
                read_dirs=read_dirs,
                write_dirs=write_dirs,
                provider_binary=stage.provider_binary,
                staged_head=staged_head,
                group_context=context.group_context,
                command_dispatcher=command_dispatcher,
            ),
        ):
            yield frame
    except (KeyError, OSError, StateUnavailable, ValueError) as exc:
        yield _sse(AgentEvent(event="error", text=str(exc)))
    finally:
        await browser_stack.aclose()


def _ordered_orchestrator_graph_updates(
    execution: AgentTaskExecution,
) -> list[GraphUpdateResult]:
    """Rebuild this turn's in-turn Apply history from its durable commit ledger."""

    graph_updates: list[GraphUpdateResult] = []
    for record in execution.store.auto_research_apply_results(execution.operation_id):
        effect_result = record.result.get("result")
        if not isinstance(effect_result, dict):
            continue
        raw_graph_update = effect_result.get("graph_update")
        if raw_graph_update is None:
            continue
        try:
            graph_updates.append(GraphUpdateResult.model_validate(raw_graph_update))
        except (TypeError, ValueError):
            execution.store.record_agent_task_event(
                execution.operation_id,
                f"Ignored an invalid durable Apply result: {record.apply_id}.",
                level="warning",
            )
    return graph_updates


async def stream_auto_research_worker_run(
    service: ProjectService,
    launcher: AgentLauncher,
    request: AutoResearchRunRequest,
    data_dir: Path,
    execution: AgentTaskExecution,
    *,
    command_dispatcher: AutoResearchCommandDispatcher,
) -> AsyncIterator[str]:
    """Run one ordinary auto_research worker on its canonical actor-owned stage."""

    browser_stack = AsyncExitStack()
    try:
        turn = _canonical_worker_turn(execution, request)
        if command_dispatcher.store is not execution.store:
            raise ValueError(
                "auto_research worker stream and command dispatcher must share one store"
            )
        stage = _open_worker_stage(service, data_dir, execution, turn)
        hidden_read_scope = await _prepare_hidden_read_scope(
            service,
            turn.request,
            workspace=stage.workspace,
            remote_stage=stage.remote,
            execution=execution,
            capability="work_auto",
            execution_host=stage.execution_host,
            data_dir=data_dir,
            local_stage=stage.local,
        )
        browser_grant = await browser_stack.enter_async_context(
            browser_turn(
                turn.request,
                workspace=stage.workspace,
                execution_host=stage.execution_host,
                execution=execution,
                remote_stage=stage.remote,
                capability="work_auto",
                hidden_read_scope=hidden_read_scope,
            )
        )
        context = _auto_research_context(service, turn.request, stage)
        context.group_context = build_group_context(
            service, execution.store, turn.request.run_on or ""
        )
        _prepare_turn_handoffs(execution, turn, stage)

        messages_path = _stage_claimed_mail(execution, turn, stage)
        token = _task_token(execution)
        schema = (
            json.dumps(agent_output_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        schema_digest = hashlib.sha256(schema.encode("utf-8")).hexdigest()[:16]
        schema_path = _stage_or_reuse_task_input(
            stage.local,
            stage.remote,
            f"auto_research-patch-schema-{schema_digest}.json",
            schema,
        )
        graph_read_scope = GraphReadScope.resolve(
            turn.task.project_id, service.history, context.group_context, store=service.paper.store
        )
        # The one command surface this turn's prompt renders and its dispatcher enforces.
        lesson_actions = lesson_command_actions(execution.store, turn.task.operation_id)
        allowed_verbs = auto_research_allowed_verbs(
            "worker",
            graph_read_scope=graph_read_scope,
            bulletin=execution.store.bulletin_enabled_for_task(turn.task.operation_id),
            lesson_actions=lesson_actions,
        )
        command_dispatcher = _bind_actor_dispatcher(
            command_dispatcher, service, turn.request, stage, graph_read_scope, allowed_verbs
        )
        staged_head = prepare_graph_observation(
            execution,
            service,
            owner_id=_actor_seen_owner(turn),
            target=graph_read_scope.target,
            revision=context.graph_revision,
            native_session_id=turn.binding.native_session_id,
        )
        patch_path = str(stage.workspace / "patch.json")
        staged_commands = stage_command_mailbox(
            local_stage=stage.local,
            remote_stage=stage.remote,
            episode_id=turn.request.episode_id,
            task_id=execution.operation_id,
            turn_id=f"{execution.operation_id}:worker",
        )
        outcome = _ProviderOutcome(session_id=turn.binding.native_session_id)
        async with _actor_mailbox(
            staged_commands,
            execution=execution,
            outcome=outcome,
            serve=partial(
                _serve_auto_research_commands,
                staged_commands,
                execution=execution,
                episode_id=turn.request.episode_id,
                dispatcher=command_dispatcher,
                service=service,
                graph_read_scope=graph_read_scope,
                expected_turn_id=f"{execution.operation_id}:worker",
            ),
            resume_context=_actor_mailbox_context(
                f"{execution.operation_id}:worker", graph_read_scope, allowed_verbs
            ),
        ):
            write_scope = _project_write_scope(
                context,
                service,
                turn.request.run_on or "",
                local_stage=stage.local,
                workspace=stage.workspace,
                remote_stage=stage.remote,
                data_dir=data_dir,
                execution=execution,
                capability="work_auto",
                episode_request=turn.request,
            )
            contract_path, prompt, master, values = _worker_prompt(
                service,
                execution,
                turn,
                context=context,
                local_stage=stage.local,
                remote_stage=stage.remote,
                token=token,
                patch_path=patch_path,
                schema_path=schema_path,
                command_client=staged_commands.client_command(),
                allowed_verbs=allowed_verbs,
                lesson_actions=lesson_actions,
                graph_read_scope=graph_read_scope,
                staged_head=staged_head,
                reply_key=_worker_reply_key(turn),
                messages_path=messages_path,
                write_scope=write_scope,
                browser_grant=browser_grant,
            )
            read_dirs = _chat_read_dirs(
                context,
                stage.local,
                stage.remote,
                service,
                turn.request.run_on or "",
            )
            if context.group_context is not None:
                read_dirs.extend(context.group_context.read_dirs())
            write_dirs = [Path(item) for item in write_scope.repository_roots]
            retry_patch_digest = (
                _existing_exact_patch_digest(stage.workspace, stage.remote)
                if execution.continuation == "retry"
                else None
            )
            _record_auto_research_finalization(execution, stage, write_scope, retry_patch_digest)
            _record_agent_launch_receipt(
                execution,
                cast(RunRequest, turn.request),
                prompt=prompt,
                contract_path=contract_path,
                remote=bool(stage.execution_host),
                resumed=turn.binding.native_session_id is not None,
                write_scope=write_scope,
                continuation=execution.continuation,
                extra={
                    "surface": "auto_research",
                    "role": "worker",
                    "profile": "ordinary",
                    "actor_operation_id": turn.binding.actor_operation_id,
                    "allocation_operation_id": turn.allocation_operation_id,
                    "control_node_id": turn.binding.control_node_id,
                    "capability": "work_auto",
                    "network_access": True,
                    "write_directory_count": len(write_dirs),
                },
            )

            async with aclosing(
                _stream_agent_events(
                    launcher,
                    cast(RunRequest, turn.request),
                    prompt,
                    service=service,
                    hidden_read_scope=hidden_read_scope,
                    workspace=stage.workspace,
                    session_id=turn.binding.native_session_id,
                    read_dirs=read_dirs,
                    write_dirs=write_dirs,
                    write_scope=write_scope,
                    execution_host=stage.execution_host,
                    execution=execution,
                    remote_stage=stage.remote,
                    capability="work_auto",
                    browser_grant=browser_grant,
                    outcome=outcome,
                    binary=stage.provider_binary,
                    invocation_gate=staged_commands.invocation_gate,
                )
            ) as stream:
                async for frame in stream:
                    yield frame
        if outcome.remote_result_pending:
            return
        async for frame in _settle_actor_turn(
            service,
            execution,
            turn,
            stage,
            outcome,
            write_scope=write_scope,
            retry_patch_digest=retry_patch_digest,
            correction=_Correction(
                launcher=launcher,
                master=master,
                values=values,
                read_dirs=read_dirs,
                write_dirs=write_dirs,
                provider_binary=stage.provider_binary,
                staged_head=staged_head,
                group_context=context.group_context,
                command_dispatcher=command_dispatcher,
            ),
        ):
            yield frame
    except (KeyError, OSError, StateUnavailable, ValueError) as exc:
        yield _sse(AgentEvent(event="error", text=str(exc)))
    finally:
        await browser_stack.aclose()


def _canonical_worker_turn(
    execution: AgentTaskExecution,
    supplied: AutoResearchRunRequest,
) -> _CanonicalWorkerTurn:
    if execution.continuation not in _WORKER_CONTINUATIONS:
        raise ValueError("AutoResearch worker continuation is not supported by this stream.")
    task = execution.store.agent_task(execution.operation_id)
    if task is None or task.kind != "auto_research" or task.episode_id is None:
        raise ValueError("AutoResearch worker execution requires one durable auto_research task.")
    durable = AutoResearchRunRequest.model_validate(task.request)
    if durable != supplied:
        raise ValueError("AutoResearch worker launch request differs from its durable task record.")
    binding = execution.store.auto_research_actor_binding(execution.operation_id)
    if execution.store.agent_task_profile(execution.operation_id) != "ordinary":
        raise ValueError("AutoResearch worker requires the canonical ordinary semantic profile.")
    if binding.role != "worker" or durable.role != "worker":
        raise ValueError("This stream executes ordinary auto_research workers only.")
    if (
        binding.episode_id != task.episode_id
        or durable.episode_id != task.episode_id
        or durable.actor_operation_id != binding.actor_operation_id
        or durable.control_node_id != binding.control_node_id
        or binding.current_operation_id != execution.operation_id
    ):
        raise ValueError(
            "AutoResearch worker role, actor, or seat conflicts with its durable binding."
        )
    if (execution.stage_host or "") != (binding.stage_host or ""):
        raise ValueError(
            "AutoResearch worker execution host conflicts with its durable actor binding."
        )
    if execution.stage_root != binding.stage_root:
        raise ValueError("AutoResearch worker stage conflicts with its durable actor binding.")
    cause = execution.store.agent_task_continuation_cause(execution.operation_id)
    if cause != execution.continuation:
        raise ValueError(
            "AutoResearch worker continuation conflicts with its durable launch cause."
        )
    recovering = execution.continuation in _SAME_ALLOCATION_RECOVERY
    if execution.continuation != "fresh" and (
        not binding.native_session_id
        or not binding.stage_root
        or durable.session_id != binding.native_session_id
    ):
        raise ValueError("AutoResearch worker recovery requires its exact saved session and stage.")
    if execution.continuation == "fresh" and binding.actor_operation_id != execution.operation_id:
        raise ValueError("A fresh auto_research worker cannot continue another actor.")
    allocation_operation_id = _paid_allocation_operation_id(execution, task)
    return _CanonicalWorkerTurn(
        task=task,
        request=durable.model_copy(update={"session_id": binding.native_session_id}),
        binding=binding,
        allocation_operation_id=allocation_operation_id,
        recovering_allocation=recovering,
    )


def _canonical_orchestrator_turn(
    execution: AgentTaskExecution,
    supplied: AutoResearchRunRequest,
) -> _CanonicalOrchestratorTurn:
    if execution.continuation not in _ORCHESTRATOR_CONTINUATIONS:
        raise ValueError("AutoResearch orchestrator continuation is not supported by this stream.")
    task = execution.store.agent_task(execution.operation_id)
    if task is None or task.kind != "auto_research" or task.episode_id is None:
        raise ValueError(
            "AutoResearch orchestrator execution requires one durable auto_research task."
        )
    durable = AutoResearchRunRequest.model_validate(task.request)
    if durable != supplied:
        raise ValueError(
            "AutoResearch orchestrator launch request differs from its durable task record."
        )
    binding = execution.store.auto_research_actor_binding(execution.operation_id)
    if execution.store.agent_task_profile(execution.operation_id) != "orchestrator":
        raise ValueError("AutoResearch orchestrator requires its sole elevated semantic profile.")
    if binding.role != "orchestrator" or durable.role != "orchestrator":
        raise ValueError("This stream executes the auto_research orchestrator only.")
    if (
        binding.episode_id != task.episode_id
        or durable.episode_id != task.episode_id
        or durable.actor_operation_id != binding.actor_operation_id
        or durable.control_node_id is not None
        or binding.control_node_id is not None
        or binding.current_operation_id != execution.operation_id
    ):
        raise ValueError(
            "AutoResearch orchestrator role, actor, or scope conflicts with its durable binding."
        )
    if (execution.stage_host or "") != (binding.stage_host or ""):
        raise ValueError(
            "AutoResearch orchestrator execution host conflicts with its durable actor binding."
        )
    if execution.stage_root != binding.stage_root:
        raise ValueError(
            "AutoResearch orchestrator stage conflicts with its durable actor binding."
        )
    cause = execution.store.agent_task_continuation_cause(execution.operation_id)
    if cause != execution.continuation:
        raise ValueError(
            "AutoResearch orchestrator continuation conflicts with its durable launch cause."
        )
    recovering = execution.continuation in _SAME_ALLOCATION_RECOVERY
    clean_retry = _is_authorized_clean_orchestrator_retry(
        execution,
        task=task,
        request=durable,
        binding=binding,
    )
    if (
        execution.continuation != "fresh"
        and not clean_retry
        and (
            not binding.native_session_id
            or not binding.stage_root
            or durable.session_id != binding.native_session_id
        )
    ):
        raise ValueError(
            "AutoResearch orchestrator continuation requires its exact session and stage."
        )
    if execution.continuation == "fresh" and (
        binding.actor_operation_id != execution.operation_id or task.parent_operation_id is not None
    ):
        raise ValueError("A fresh auto_research orchestrator must be the sole root actor.")
    allocation_operation_id = _paid_allocation_operation_id(execution, task)
    return _CanonicalOrchestratorTurn(
        task=task,
        request=durable.model_copy(update={"session_id": binding.native_session_id}),
        binding=binding,
        allocation_operation_id=allocation_operation_id,
        recovering_allocation=recovering,
        clean_session_retry=clean_retry,
    )


def _is_authorized_clean_orchestrator_retry(
    execution: AgentTaskExecution,
    *,
    task: AgentTaskRecord,
    request: AutoResearchRunRequest,
    binding: AutoResearchActorBinding,
) -> bool:
    """Recognize the storage-authorized same-allocation clean-session retry."""

    if (
        execution.continuation != "retry"
        or task.parent_operation_id is None
        or task.native_session_id is not None
        or request.session_id is not None
        or binding.native_session_id is not None
    ):
        return False
    parent = execution.store.agent_task(task.parent_operation_id)
    if (
        parent is None
        or parent.episode_id != task.episode_id
        or parent.kind != "auto_research"
        or parent.status not in {"paused", "interrupted", "failed"}
        or task.attempt != parent.attempt + 1
    ):
        return False
    parent_request = AutoResearchRunRequest.model_validate(parent.request)
    if (
        parent_request.role != "orchestrator"
        or (parent_request.actor_operation_id or parent.operation_id) != binding.actor_operation_id
        or (parent.stage_host or "") != (binding.stage_host or "")
        or parent.stage_root != binding.stage_root
    ):
        return False
    if not parent.native_session_id:
        return True
    # A human who changed the provider, model, or reasoning retired the session
    # the old binding owns: no provider can resume another's, and the same
    # reasoning that made the recovery worth starting makes resuming wrong.
    if any(
        getattr(parent_request, field) != getattr(request, field)
        for field in ("provider", "model", "reasoning")
    ):
        return True
    receipts = execution.store.agent_task_receipts(parent.operation_id)
    session_limit = any(
        receipt.category == "provider_terminal_error"
        and receipt.payload.get("classification") == "session_limit"
        for receipt in receipts
    ) or (bool(parent.error) and classify_terminal_error(parent.error or "") == "session_limit")
    continuation_unavailable = any(
        receipt.category == "continuation_context_unavailable"
        and receipt.payload.get("retry_required") is True
        for receipt in receipts
    )
    if not session_limit and not continuation_unavailable:
        return False

    # A clean retry deliberately leaves the new task's session NULL so the
    # provider can bind its replacement with the existing checkpoint CAS. Walk
    # only this same-allocation recovery chain to prove that the actor previously
    # held the native session/stage pair being retired.
    prior = parent
    seen: set[str] = set()
    while True:
        if prior.operation_id in seen:
            return False
        seen.add(prior.operation_id)
        prior_request = AutoResearchRunRequest.model_validate(prior.request)
        if (
            prior.episode_id != task.episode_id
            or prior.kind != "auto_research"
            or prior_request.role != "orchestrator"
            or (prior_request.actor_operation_id or prior.operation_id)
            != binding.actor_operation_id
            or (prior.stage_host or "") != (binding.stage_host or "")
            or prior.stage_root != binding.stage_root
        ):
            return False
        if prior.native_session_id and prior.stage_root:
            return True
        if (
            prior.parent_operation_id is None
            or execution.store.agent_task_continuation_cause(prior.operation_id)
            not in _SAME_ALLOCATION_RECOVERY
        ):
            return False
        ancestor = execution.store.agent_task(prior.parent_operation_id)
        if ancestor is None:
            return False
        prior = ancestor


def _paid_allocation_operation_id(
    execution: AgentTaskExecution,
    task: AgentTaskRecord,
) -> str:
    return _allocation_lineage(execution, task)[-1]


def _allocation_lineage(
    execution: AgentTaskExecution,
    task: AgentTaskRecord,
) -> list[str]:
    """The task and each recovery attempt back to its paid allocation, which is last."""
    current = task
    lineage = [task.operation_id]
    seen: set[str] = set()
    while execution.store.agent_task_continuation_cause(current.operation_id) in {
        "resume",
        "retry",
    }:
        if current.operation_id in seen or current.parent_operation_id is None:
            raise ValueError("AutoResearch actor recovery lost its paid allocation lineage.")
        seen.add(current.operation_id)
        parent = execution.store.agent_task(current.parent_operation_id)
        if parent is None or parent.episode_id != task.episode_id or parent.kind != "auto_research":
            raise ValueError("AutoResearch actor recovery crossed its paid allocation lineage.")
        current = parent
        lineage.append(current.operation_id)
    return lineage


def _worker_stage_name(project_id: str, actor_operation_id: str) -> str:
    project = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:16]
    actor = hashlib.sha256(actor_operation_id.encode("utf-8")).hexdigest()[:16]
    return f"auto_research-worker-{project}-{actor}"


def _orchestrator_stage_name(project_id: str, actor_operation_id: str) -> str:
    project = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:16]
    actor = hashlib.sha256(actor_operation_id.encode("utf-8")).hexdigest()[:16]
    return f"auto_research-orchestrator-{project}-{actor}"


def _open_worker_stage(
    service: ProjectService,
    data_dir: Path,
    execution: AgentTaskExecution,
    turn: _CanonicalWorkerTurn,
) -> _WorkerStage:
    return _open_auto_research_actor_stage(
        service,
        data_dir,
        execution,
        turn,
        stage_name=_worker_stage_name(turn.task.project_id, turn.binding.actor_operation_id),
        actor_label="worker",
    )


def _orchestrator_stage_actor_id(
    execution: AgentTaskExecution,
    turn: _CanonicalOrchestratorTurn,
) -> str:
    """The actor whose stage this orchestrator turn reopens.

    A continuation episode resumes the chain root's native session inside the
    stage that session was opened in, so its stage is named after the root
    orchestrator rather than the continuation's own root task.
    """

    episode = execution.store.episode(turn.task.episode_id or "")
    if episode is None or episode.continues_episode_id is None:
        return turn.binding.actor_operation_id
    owner_id = execution.store.branch_owner(
        episode.project_id, episode.graph_target.branch_id or ""
    )
    root = execution.store.episode(owner_id) if owner_id else None
    if root is None or root.root_operation_id is None:
        raise ValueError("The continued Auto-research episode lost its chain root.")
    return root.root_operation_id


def _open_orchestrator_stage(
    service: ProjectService,
    data_dir: Path,
    execution: AgentTaskExecution,
    turn: _CanonicalOrchestratorTurn,
) -> _WorkerStage:
    return _open_auto_research_actor_stage(
        service,
        data_dir,
        execution,
        turn,
        stage_name=_orchestrator_stage_name(
            turn.task.project_id,
            _orchestrator_stage_actor_id(execution, turn),
        ),
        actor_label="orchestrator",
        allow_new_stage=turn.clean_session_retry,
    )


def _open_auto_research_actor_stage(
    service: ProjectService,
    data_dir: Path,
    execution: AgentTaskExecution,
    turn: _CanonicalWorkerTurn | _CanonicalOrchestratorTurn,
    *,
    stage_name: str,
    actor_label: Literal["worker", "orchestrator"],
    allow_new_stage: bool = False,
) -> _WorkerStage:
    request = turn.request
    if (
        not request.provider
        or request.model is None
        or request.reasoning is None
        or not request.run_on
    ):
        raise ValueError(f"AutoResearch {actor_label} has no exact pinned provider profile.")
    machine = service.manifest.machine_map.get(request.run_on)
    if machine is None:
        raise ValueError(f"unknown auto_research {actor_label} execution machine: {request.run_on}")
    if turn.binding.stage_root is not None:
        if machine.host:
            # Accepts the new `~/.rcp/stages` root, whose home the host checks on
            # attach, and a saved legacy `/tmp` root that a later release removes.
            if (
                turn.binding.stage_host != machine.host
                or remote_stage_name(turn.binding.stage_root) != stage_name
            ):
                raise ValueError(
                    f"AutoResearch {actor_label} saved remote stage has a different actor binding."
                )
            remote = RemoteRunStage(machine.host).attach(turn.binding.stage_root)
            return _WorkerStage(
                local=None,
                remote=remote,
                workspace=Path(str(remote.workspace)),
                execution_host=machine.host,
                provider_binary=service.launcher.provider_binary(request.provider, machine.host),
            )
        expected_local = _swept_stage_root(data_dir, store=execution.store) / stage_name
        saved = Path(turn.binding.stage_root)
        if (
            turn.binding.stage_host is not None
            or saved.absolute() != expected_local.absolute()
            or saved.is_symlink()
            or not saved.is_dir()
        ):
            raise ValueError(
                f"AutoResearch {actor_label} saved local stage has a different actor binding."
            )
        return _WorkerStage(
            local=saved,
            remote=None,
            workspace=saved,
            execution_host="",
            provider_binary=service.launcher.provider_binary(request.provider, machine.host),
        )

    if turn.binding.native_session_id is not None or (
        execution.continuation != "fresh" and not allow_new_stage
    ):
        raise ValueError(
            f"AutoResearch {actor_label} continuation cannot start a fresh execution stage."
        )
    if machine.host:
        remote = RemoteRunStage(machine.host).open(
            stage_name,
            reuse=True,
            protected_roots=_protected_run_stage_roots(execution.store, machine.host),
        )
        assert remote.root is not None
        execution.checkpoint_stage(machine.host, str(remote.root))
        return _WorkerStage(
            local=None,
            remote=remote,
            workspace=Path(str(remote.workspace)),
            execution_host=machine.host,
            provider_binary=service.launcher.provider_binary(request.provider, machine.host),
        )
    local = _swept_stage_root(data_dir, store=execution.store) / stage_name
    if os.path.lexists(local):
        if local.is_symlink() or not local.is_dir():
            raise ValueError(f"AutoResearch {actor_label} local stage is unsafe.")
    else:
        local.mkdir(mode=0o700, parents=True)
    execution.checkpoint_stage("", str(local))
    return _WorkerStage(
        local=local,
        remote=None,
        workspace=local,
        execution_host="",
        provider_binary=service.launcher.provider_binary(request.provider, machine.host),
    )


def _auto_research_context(
    service: ProjectService,
    request: AutoResearchRunRequest,
    stage: _WorkerStage,
) -> ChatContext:
    state = service.history.state()
    selected = request.run_truth_scope or service.manifest.agent.default_run_truth_scope
    access = {
        alias: repository_access(
            service.manifest.repository_map[alias],
            service.manifest.machine_map[service.manifest.repository_map[alias].machine],
        )
        for alias in selected
        if alias in service.manifest.repository_map
    }
    context = ContextAssembler(service.manifest, graph_root=service.history.root).chat_context(
        state,
        graph_target=service.history.graph_target,
        node_id=None,
        run_truth_scope=request.run_truth_scope,
        repository_access=access,
    )
    state_machine = service.manifest.base_machine
    if state_machine != request.run_on:
        repositories = []
        for item in context.repositories:
            if item.machine == request.run_on:
                repositories.append(item.model_copy(update={"host": ""}))
            elif item.host:
                repositories.append(item)
            else:
                raise StateUnavailable(
                    f"Repository {item.alias!r} has no SSH host reachable from auto_research "
                    f"execution machine {request.run_on!r}."
                )
        graph = (
            json.dumps(
                state.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        research = render_research_md(state)
        graph_digest = hashlib.sha256(graph.encode("utf-8")).hexdigest()[:16]
        research_digest = hashlib.sha256(research.encode("utf-8")).hexdigest()[:16]
        updates = {
            "repositories": repositories,
            "graph_path": _stage_or_reuse_task_input(
                stage.local,
                stage.remote,
                f"auto_research-graph-r{state.revision}-{graph_digest}.json",
                graph,
            ),
            "research_md_path": _stage_or_reuse_task_input(
                stage.local,
                stage.remote,
                f"auto_research-research-r{state.revision}-{research_digest}.md",
                research,
            ),
        }
        context = context.model_copy(update=updates)
    elif stage.remote is not None:
        context = context.model_copy(
            update=_stage_context_paths(context, service, stage.remote, request.run_on or "")
        )
    return context


def _orchestrator_launch_context(
    service: ProjectService, request: AutoResearchRunRequest, stage: _WorkerStage
) -> ChatContext:
    context = _auto_research_context(service, request, stage)
    return context.model_copy(
        update=stage_branch_read_context(context, service, stage.local, stage.remote)
    )


def _refreshed_orchestrator_state_paths(
    service: ProjectService,
    request: AutoResearchRunRequest,
    stage: _WorkerStage,
) -> tuple[int, str, str]:
    context = _auto_research_context(service, request, stage)
    return context.graph_revision, context.graph_path, context.research_md_path


def _claimed_messages(
    execution: AgentTaskExecution,
    turn: _CanonicalWorkerTurn | _CanonicalOrchestratorTurn,
) -> list[AutoResearchMessageRecord]:
    # Includes mail any attempt of this allocation harvested mid-turn, since a
    # retry, often in a clean native session, has not seen it; mail an attempt
    # cleared was acknowledged unread and stays unread. The handoff copies carry
    # the allocation's claim; the durable records keep the attempt that took them.
    lineage = set(_allocation_lineage(execution, turn.task))
    cleared = {
        message_id
        for receipt in execution.store.auto_research_inbox_receipts(turn.request.episode_id)
        if receipt.mode == "clear"
        for message_id in receipt.message_ids
    }
    return [
        message.model_copy(update={"delivery_operation_id": turn.allocation_operation_id})
        for message in execution.store.auto_research_messages(turn.request.episode_id)
        if message.delivery_operation_id in lineage and message.message_id not in cleared
    ]


def _claimed_notices(
    execution: AgentTaskExecution,
    turn: _CanonicalOrchestratorTurn,
) -> list[AutoResearchLifecycleNoticeRecord]:
    # As with mail: includes notices any attempt harvested, re-stamped as this
    # allocation's claim. A Clear receipt keeps no notice bodies.
    lineage = set(_allocation_lineage(execution, turn.task))
    claimed = {
        notice.notice_id: notice
        for notice in execution.store.auto_research_lifecycle_delivery(turn.allocation_operation_id)
    }
    for receipt in execution.store.auto_research_inbox_receipts(turn.request.episode_id):
        if receipt.mode == "harvest":
            for notice in receipt.notices:
                if notice.acknowledged_operation_id in lineage:
                    claimed.setdefault(
                        notice.notice_id,
                        notice.model_copy(
                            update={
                                "delivery_operation_id": turn.allocation_operation_id,
                                "delivered_at": notice.acknowledged_at,
                            }
                        ),
                    )
    return sorted(claimed.values(), key=lambda notice: (notice.created_at, notice.notice_id))


def _fit_handoff(
    items: list[_Item], build: Callable[[list[_Item]], object], *, kind: str, allocation: str
) -> list[_Item]:
    """Keep the oldest items one handoff can carry; say how many a recovery dropped.

    Wake claims and harvests are each bounded, but their union across attempts
    may not be. Anything else wrong with an item still raises.
    """
    kept: list[_Item] = []
    for item in items:
        try:
            build([*kept, item])
        except ValueError:
            if not kept:
                raise
            break
        kept.append(item)
    if len(kept) < len(items):
        _LOGGER.warning(
            "Auto-research allocation %s restaged %d of %d claimed %s; the rest exceed "
            "one handoff.",
            allocation,
            len(kept),
            len(items),
            kind,
        )
    return kept


def _prepare_turn_handoffs(
    execution: AgentTaskExecution,
    turn: _CanonicalWorkerTurn | _CanonicalOrchestratorTurn,
    stage: _WorkerStage,
) -> None:
    cleared = execution.store.auto_research_handoffs_cleared(turn.allocation_operation_id)
    if turn.recovering_allocation and cleared:
        return
    _clear_stale_turn_handoffs(stage.workspace, stage.remote)
    execution.store.mark_auto_research_handoffs_cleared(turn.allocation_operation_id)
    execution.store.record_agent_task_receipt(
        turn.allocation_operation_id,
        _HANDOFFS_CLEARED_RECEIPT,
        {
            "version": 1,
            "files": [
                "patch.json",
                "watch.json",
                AUTO_RESEARCH_MAIL_HANDOFF_FILE,
                AUTO_RESEARCH_LIFECYCLE_HANDOFF_FILE,
            ],
        },
    )


def _stage_claimed_mail(
    execution: AgentTaskExecution,
    turn: _CanonicalWorkerTurn | _CanonicalOrchestratorTurn,
    stage: _WorkerStage,
) -> str | None:
    messages = _claimed_messages(execution, turn)
    mailbox = RunStageMailbox.for_stage(local_stage=stage.local, remote_stage=stage.remote)
    if messages:
        recipient = messages[0].recipient_task_id
        if recipient != turn.binding.actor_operation_id:
            raise ValueError("Claimed auto_research mail targets another auto_research actor.")

        def build(batch: list[AutoResearchMessageRecord]):
            return auto_research_mail_delivery(
                episode_id=turn.request.episode_id,
                recipient_task_id=recipient,
                delivery_operation_id=turn.allocation_operation_id,
                messages=batch,
            )

        delivery = build(
            _fit_handoff(
                messages, build, kind="mail messages", allocation=turn.allocation_operation_id
            )
        )
        # The durable claim is the record; a recovery restages it rather than
        # trusting or refusing whatever copy an earlier attempt left behind.
        stage_auto_research_mail_delivery(mailbox, delivery)
        return str(stage.workspace / AUTO_RESEARCH_MAIL_HANDOFF_FILE)
    mailbox.remove(AUTO_RESEARCH_MAIL_HANDOFF_FILE)
    if turn.request.wake_cause == "message" and not turn.recovering_allocation:
        raise ValueError("AutoResearch message wake has no mail claimed by this paid allocation.")
    return None


def _stage_claimed_lifecycle(
    execution: AgentTaskExecution,
    turn: _CanonicalOrchestratorTurn,
    stage: _WorkerStage,
) -> str | None:
    notices = _claimed_notices(execution, turn)
    mailbox = RunStageMailbox.for_stage(local_stage=stage.local, remote_stage=stage.remote)
    if notices:
        if turn.binding.role != "orchestrator":
            raise ValueError("Lifecycle notices may wake only the Auto-research orchestrator.")

        def build(batch: list[AutoResearchLifecycleNoticeRecord]):
            return auto_research_lifecycle_delivery(
                episode_id=turn.request.episode_id,
                recipient_task_id=turn.binding.actor_operation_id,
                delivery_operation_id=turn.allocation_operation_id,
                notices=batch,
            )

        delivery = build(
            _fit_handoff(
                notices,
                build,
                kind="lifecycle notices",
                allocation=turn.allocation_operation_id,
            )
        )
        stage_auto_research_lifecycle_delivery(mailbox, delivery)
        return str(stage.workspace / AUTO_RESEARCH_LIFECYCLE_HANDOFF_FILE)
    mailbox.remove(AUTO_RESEARCH_LIFECYCLE_HANDOFF_FILE)
    if turn.request.wake_cause == "lifecycle":
        raise ValueError(
            "AutoResearch lifecycle wake has no lifecycle facts claimed by this paid allocation."
        )
    return None


def _worker_reply_key(turn: _CanonicalWorkerTurn) -> str:
    digest = hashlib.sha256(
        (
            "auto_research-worker-reply\0"
            + turn.request.episode_id
            + "\0"
            + turn.allocation_operation_id
        ).encode("utf-8")
    ).hexdigest()
    return f"worker-reply-{digest[:32]}"


def _retry_diagnostics_path(
    execution: AgentTaskExecution,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    token: str,
) -> str | None:
    if execution.continuation != "retry":
        return None
    return _stage_json_task_input(
        local_stage,
        remote_stage,
        f"task-{token}-retry-diagnostics.json",
        {"prior_attempt_diagnostics": list(execution.retry_feedback)},
    )


def _actor_seen_owner(turn: _CanonicalWorkerTurn | _CanonicalOrchestratorTurn) -> str:
    kind = "auto-root" if isinstance(turn, _CanonicalOrchestratorTurn) else "auto-child"
    return f"{kind}:{turn.binding.actor_operation_id}"


def _actor_launch_prompt(
    execution: AgentTaskExecution,
    *,
    browser_grant: BrowserGrant,
    session_id: str | None,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    label: str,
    role: str,
    master_label_prefix: str,
    render_master: Callable[[], str],
    start_contract: Callable[[], str],
    continuation_parts: Callable[[], list[str]],
    values: dict[str, object],
    ontology_extensions: bool,
    staged_head: GraphHeadRef | None = None,
    report_pending: Callable[[str], bool] = lambda _session_id: False,
    fresh_parts: tuple[str, ...] = (),
) -> tuple[str, str, MasterRef]:
    """Stage one actor launch; return its contract path, prompt, and the session's master.

    A session start sends its full contract by forced read and records it as the session's
    master with its values. A continuation sends only its current parts and the values that
    changed since that master inline, with a pointer to it, or a bootstrap when the session
    holds none under the current key. The returned master is the one the session now holds.
    """

    # A launch that never asked for the browser has nothing to state or revoke; its values
    # still record the status, so a change from a granted launch travels as a change.
    browser_line = (
        "" if browser_grant.status == "not_requested" else browser_prompt_line(browser_grant)
    )

    def with_browser(text: str) -> str:
        return f"{text}\n\n{browser_line}" if browser_line else text

    values["browser"] = browser_grant.model_dump(
        include={"status", "reason_code", "session_name", "invocation_dir"}
    )
    fresh_parts = (
        *fresh_parts,
        stage_operational_context_pointer(
            execution,
            local_stage,
            remote_stage,
        ),
    )
    key = master_key(AUTO_RESEARCH_POLICY_VERSION, ontology_extensions=ontology_extensions)
    node = classify(
        LaunchPhase(session_id=session_id, phase=_LAUNCH_PHASES[execution.continuation])
    )
    if node == "session_start":
        contract = with_browser(start_contract())
        contract_path, prompt = _stage_task_contract(
            local_stage, remote_stage, label, contract, execution=execution, role=role
        )
        prompt = "\n\n".join([prompt, *fresh_parts])
        record_session_master(execution.store, execution.operation_id, contract, key, values)
        return contract_path, prompt, MasterRef(path=contract_path, bootstrap=False, values=values)
    assert session_id is not None
    parts = [*continuation_parts(), *fresh_parts, browser_line]
    after_report = report_pending(session_id)
    master = continuation_session_master(
        execution,
        local_stage=local_stage,
        remote_stage=remote_stage,
        native_session_id=session_id,
        label_prefix=master_label_prefix,
        key=key,
        render=lambda: with_browser(render_master()),
        values=values,
        force_bootstrap=after_report,
    )
    # The report's restriction is the newest instruction this session holds; the prompt
    # retires it and sends the agent back to its operational master.
    held = replace(master, after_report=True) if after_report else master
    delta = changed_since_master(master, values) or {}
    if staged_head is not None:
        delta = merge_prepared_graph_observation_delta(delta, execution, session_id)
    prompt = compose(node, parts=parts, master=held, delta=delta)
    contract_path = record_inline_prompt(
        execution,
        local_stage=local_stage,
        remote_stage=remote_stage,
        label=label,
        role=role,
        prompt=prompt,
    )
    return contract_path, prompt, MasterRef(path=master.path, bootstrap=False, values=master.values)


def _orchestrator_prompt(
    service: ProjectService,
    execution: AgentTaskExecution,
    turn: _CanonicalOrchestratorTurn,
    *,
    context: ChatContext,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    token: str,
    patch_path: str,
    schema_path: str,
    command_client: str,
    allowed_verbs: tuple[CommandVerb, ...],
    lesson_actions: tuple[str, ...] = (),
    graph_read_scope: GraphReadScope | None = None,
    staged_head: GraphHeadRef | None = None,
    messages_path: str | None,
    lifecycle_path: str | None,
    skill_pointers: list[dict[str, object]],
    write_scope: ProjectWriteScope,
    browser_grant: BrowserGrant,
    child_profiles: ChildSeatProfileChoices,
) -> tuple[str, str, MasterRef, dict[str, object]]:
    repositories = [
        {"alias": item.alias, "host": item.host, "path": item.path} for item in context.repositories
    ]
    compute_profiles = service.machine_prompt_profiles(turn.request.run_on or "")
    shared_repositories = [
        {"alias": item.alias, "host": item.host, "path": item.path}
        for item in context.shared_repositories
    ]
    loops = other_branch_loops(
        execution.store, turn.task.project_id, graph_target=turn.task.graph_target
    )
    other_runs = other_branch_auto_research(
        execution.store, turn.task.project_id, graph_target=turn.task.graph_target
    )
    values = auto_research_prompt_values(
        graph_path=context.graph_path,
        research_path=context.research_md_path,
        repositories=repositories,
        patch_path=patch_path,
        output_schema_path=schema_path,
        command_client=command_client,
        write_scope=write_scope,
        group_context=context.group_context,
        skill_pointers=skill_pointers,
        allowed_verbs=allowed_verbs,
        child_profiles=child_profiles,
        compute_profiles=compute_profiles,
    )

    if graph_read_scope is not None:
        values["branches"] = graph_read_scope.branch_values()

    snapshot, dismissal_ids = auto_research_question_snapshot(
        execution.store,
        project_id=turn.task.project_id,
        episode_id=turn.task.episode_id,
        operation_id=turn.allocation_operation_id,
    )
    snapshot_digest = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()[:16]
    snapshot_path = _stage_or_reuse_task_input(
        local_stage, remote_stage, f"task-{token}-questions-{snapshot_digest}.json", snapshot + "\n"
    )
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "question_snapshot",
        {"path": snapshot_path, "dismissal_ids": dismissal_ids},
    )
    question_part = f"Read the fresh question snapshot: `{snapshot_path}`. Answers are human mail, not authority."

    def render_master() -> str:
        instruction_path = None
        if turn.request.instruction:
            instruction_digest = hashlib.sha256(
                turn.request.instruction.encode("utf-8")
            ).hexdigest()[:16]
            instruction_path = _stage_or_reuse_task_input(
                local_stage,
                remote_stage,
                f"auto_research-starting-instruction-{instruction_digest}.txt",
                turn.request.instruction + "\n",
            )
        return auto_research_orchestrator_task_contract(
            project_name=context.project_name,
            graph_path=context.graph_path,
            research_path=context.research_md_path,
            repositories=repositories,
            patch_path=patch_path,
            output_schema_path=schema_path,
            command_client=command_client,
            allowed_verbs=allowed_verbs,
            lesson_actions=lesson_actions,
            instruction_path=instruction_path,
            messages_path=messages_path,
            lifecycle_path=lifecycle_path,
            skill_pointers=skill_pointers,
            write_scope=write_scope,
            ontology_extensions=context.ontology_extensions,
            graph_read_scope=graph_read_scope,
            child_profiles=child_profiles,
            compute_profiles=compute_profiles,
        )

    def start_contract() -> str:
        # Without a saved stage the failed allocation never reached its first provider
        # launch, so its clean retry needs the full base contract rather than a reference
        # to a contract it never got.
        if execution.continuation == "fresh" or turn.binding.stage_root is None:
            return render_master()
        return auto_research_orchestrator_continuation_contract(
            original_contract_path=_parent_task_contract_path(
                execution,
                local_stage,
                remote_stage,
            ),
            mode="retry",
            graph_path=context.graph_path,
            research_path=context.research_md_path,
            repositories=repositories,
            patch_path=patch_path,
            output_schema_path=schema_path,
            command_client=command_client,
            allowed_verbs=allowed_verbs,
            lesson_actions=lesson_actions,
            messages_path=messages_path,
            lifecycle_path=lifecycle_path,
            retry_diagnostics_path=_retry_diagnostics_path(
                execution, local_stage, remote_stage, token
            ),
            skill_pointers=skill_pointers,
            write_scope=write_scope,
            ontology_extensions=context.ontology_extensions,
            graph_read_scope=graph_read_scope,
        )

    def continuation_parts() -> list[str]:
        return auto_research_continuation_parts(
            actor="orchestrator",
            cause=execution.continuation,
            messages_path=messages_path,
            lifecycle_path=lifecycle_path,
            retry_diagnostics_path=_retry_diagnostics_path(
                execution, local_stage, remote_stage, token
            ),
        )

    contract_path, prompt, master = _actor_launch_prompt(
        execution,
        browser_grant=browser_grant,
        values=values,
        staged_head=staged_head,
        ontology_extensions=context.ontology_extensions,
        session_id=turn.binding.native_session_id,
        local_stage=local_stage,
        remote_stage=remote_stage,
        label=f"task-{token}-auto_research-orchestrator.md",
        role=(
            "auto_research_orchestrator"
            if execution.continuation == "fresh"
            else f"auto_research_orchestrator_{execution.continuation}"
        ),
        master_label_prefix="auto_research-orchestrator-master",
        render_master=render_master,
        start_contract=start_contract,
        continuation_parts=continuation_parts,
        fresh_parts=(
            render_group_context(context.group_context),
            question_part,
            auto_research_branch_read_pointers(
                context.parent_graph_path,
                shared_repositories,
                repositories,
                branch_name=context.branch_name,
                branch_parent=context.branch_parent,
            ),
            render_loop_overlap(loops),
            render_auto_research_overlap(other_runs),
        ),
        report_pending=lambda session_id: execution.store.episode_report_rebootstrap_pending(
            turn.task.project_id,
            native_session_id=session_id,
            stage_host=turn.binding.stage_host,
            stage_root=turn.binding.stage_root or "",
        ),
    )
    return contract_path, prompt, master, values


def _worker_prompt(
    service: ProjectService,
    execution: AgentTaskExecution,
    turn: _CanonicalWorkerTurn,
    *,
    context: ChatContext,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    token: str,
    patch_path: str,
    schema_path: str,
    command_client: str,
    allowed_verbs: tuple[CommandVerb, ...],
    lesson_actions: tuple[str, ...] = (),
    graph_read_scope: GraphReadScope | None = None,
    staged_head: GraphHeadRef | None = None,
    reply_key: str,
    messages_path: str | None,
    write_scope: ProjectWriteScope,
    browser_grant: BrowserGrant,
) -> tuple[str, str, MasterRef, dict[str, object]]:
    actor = execution.store.agent_task(turn.binding.actor_operation_id)
    if actor is None:
        raise ValueError("AutoResearch worker origin task is missing.")
    actor_request = AutoResearchRunRequest.model_validate(actor.request)
    if not actor_request.instruction:
        raise ValueError("AutoResearch worker origin has no durable instruction.")
    instruction_digest = hashlib.sha256(actor_request.instruction.encode("utf-8")).hexdigest()[:16]
    instruction_path = _stage_or_reuse_task_input(
        local_stage,
        remote_stage,
        f"auto_research-worker-instruction-{instruction_digest}.txt",
        actor_request.instruction + "\n",
    )
    node = service.history.state().nodes.get(turn.binding.control_node_id or "")
    if node is None:
        raise ValueError("AutoResearch worker seat is no longer in the graph.")
    repositories = [
        {"alias": item.alias, "host": item.host, "path": item.path} for item in context.repositories
    ]
    compute_profiles = service.machine_prompt_profiles(turn.request.run_on or "")
    values = auto_research_prompt_values(
        graph_path=context.graph_path,
        research_path=context.research_md_path,
        repositories=repositories,
        patch_path=patch_path,
        output_schema_path=schema_path,
        command_client=command_client,
        write_scope=write_scope,
        group_context=context.group_context,
        reply_key=reply_key,
        allowed_verbs=allowed_verbs,
        compute_profiles=compute_profiles,
    )

    if graph_read_scope is not None:
        values["branches"] = graph_read_scope.branch_values()

    def render_master() -> str:
        return auto_research_worker_task_contract(
            project_name=context.project_name,
            seat_node_type=node.extension_type or node.type,
            seat_node_id=node.id,
            seat_difficulty=json.dumps(node.model_dump(mode="json"), ensure_ascii=False, indent=2),
            instruction_path=instruction_path,
            graph_path=context.graph_path,
            research_path=context.research_md_path,
            repositories=repositories,
            patch_path=patch_path,
            output_schema_path=schema_path,
            command_client=command_client,
            allowed_verbs=allowed_verbs,
            lesson_actions=lesson_actions,
            reply_key=reply_key,
            messages_path=messages_path,
            write_scope=write_scope,
            ontology_extensions=context.ontology_extensions,
            graph_read_scope=graph_read_scope,
            compute_profiles=compute_profiles,
        )

    def continuation_parts() -> list[str]:
        return auto_research_continuation_parts(
            actor="worker",
            cause=execution.continuation,
            messages_path=messages_path,
            retry_diagnostics_path=_retry_diagnostics_path(
                execution, local_stage, remote_stage, token
            ),
        )

    contract_path, prompt, master = _actor_launch_prompt(
        execution,
        browser_grant=browser_grant,
        values=values,
        staged_head=staged_head,
        ontology_extensions=context.ontology_extensions,
        session_id=turn.binding.native_session_id,
        local_stage=local_stage,
        remote_stage=remote_stage,
        label=f"task-{token}-auto_research-worker.md",
        role=(
            "auto_research_worker"
            if execution.continuation == "fresh"
            else f"auto_research_worker_{execution.continuation}"
        ),
        master_label_prefix="auto_research-worker-master",
        render_master=render_master,
        start_contract=render_master,
        continuation_parts=continuation_parts,
        fresh_parts=(render_group_context(context.group_context),),
    )
    return contract_path, prompt, master, values


async def _serve_auto_research_commands(
    staged: StagedCommandMailbox,
    *,
    execution: AgentTaskExecution,
    episode_id: str,
    dispatcher: AutoResearchCommandDispatcher,
    stop: asyncio.Event | threading.Event,
    expected_turn_id: str,
    service: ProjectService | None = None,
    graph_read_scope: GraphReadScope | None = None,
    **mailbox_state: Any,
) -> None:
    async def handle(
        request: CommandRequest,
        identity: CommandTurnIdentity,
    ) -> CommandResponse:
        if (
            identity.episode_id != episode_id
            or identity.task_id != execution.operation_id
            or identity.turn_id != expected_turn_id
        ):
            return CommandResponse(
                request_id=request.request_id,
                status="invalid",
                message="AutoResearch command credential does not match this actor turn.",
            )
        if graph_read_scope is not None and request.verb in graph_read_scope.verbs:
            from rcp.runs.graph_history_queries import handle_graph_history_command

            assert service is not None
            response = await asyncio.to_thread(
                handle_graph_history_command,
                service,
                graph_read_scope,
                request,
                resolve_project=service.resolve_project_service,
            )
            # Graph reads are commands too, so they carry the pending-inbox note.
            return await asyncio.to_thread(
                dispatcher.with_inbox_pending, execution.operation_id, request, response
            )
        return await asyncio.to_thread(dispatcher.dispatch, execution.operation_id, request)

    await serve_command_mailbox(
        staged=staged,
        handler=handle,
        stop=stop,
        invocation_gate=staged.invocation_gate,
        record_rejection=command_rejection_recorder(execution),
        **mailbox_state,
    )


class _ValidateOnlyAutoResearchCommandDispatcher(AutoResearchCommandDispatcher):
    """Keep dispatcher ledger semantics while denying every correction side effect."""

    def dispatch(self, operation_id: str, request: CommandRequest) -> CommandResponse:
        if self.graph_read_scope is not None and request.verb in self.graph_read_scope.verbs:
            return super().dispatch(operation_id, request)
        if not isinstance(request, ValidateCommandRequest):
            context = self._context(operation_id)
            invocation = self.store.start_agent_command(
                operation_id=operation_id,
                command_id=self._unused_command_id(request.request_id),
                episode_id=context.episode.episode_id,
                verb=request.verb,
                idempotency_key=None,
                payload={
                    "request_id": request.request_id,
                    "arguments": request.arguments.model_dump(mode="json"),
                    "supplied_idempotency_key": request.idempotency_key,
                    "denied_by": "auto_research_patch_correction_validate_only",
                },
            )
            return self._finish(
                invocation.command_id,
                request.request_id,
                AutoResearchCommandEffectResult(
                    status="invalid",
                    message=(
                        "AutoResearch graph-correction credentials authorize Patch validation only."
                    ),
                ),
            )
        return super().dispatch(operation_id, request)

    def _execute(
        self,
        context: AutoResearchCommandContext,
        request: CommandRequest,
        *,
        planned_worker_id: str | None,
        planned_message_id: str | None,
        planned_watcher_id: str | None,
        planned_apply_id: str | None,
        planned_resume_operation_id: str | None,
        planned_episode_effect_id: str | None,
        planned_inbox_effect_id: str | None,
        planned_finish_effect_id: str | None,
    ) -> AutoResearchCommandEffectResult:
        if not isinstance(request, ValidateCommandRequest):
            raise AutoResearchCommandInvalid(
                "AutoResearch graph-correction credentials authorize Patch validation only."
            )
        return super()._execute(
            context,
            request,
            planned_worker_id=planned_worker_id,
            planned_message_id=planned_message_id,
            planned_watcher_id=planned_watcher_id,
            planned_apply_id=planned_apply_id,
            planned_resume_operation_id=planned_resume_operation_id,
            planned_episode_effect_id=planned_episode_effect_id,
            planned_inbox_effect_id=planned_inbox_effect_id,
            planned_finish_effect_id=planned_finish_effect_id,
        )


@asynccontextmanager
async def _worker_mailbox_lifecycle(
    staged: StagedCommandMailbox,
    *,
    execution: AgentTaskExecution,
    start: Callable[[asyncio.Event], Awaitable[None]],
) -> AsyncIterator[None]:
    """Own one staged mailbox from server setup through fail-closed cleanup."""

    stop: asyncio.Event | None = None
    task: asyncio.Task[None] | None = None
    primary_error: BaseException | None = None
    try:
        stop = asyncio.Event()
        task = asyncio.create_task(start(stop))
        yield
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        await _close_worker_mailbox(
            staged,
            stop=stop,
            task=task,
            execution=execution,
            primary_error=primary_error,
        )


async def _wait_for_owned_task(
    task: asyncio.Task[None],
) -> tuple[BaseException | None, asyncio.CancelledError | None]:
    """Wait without allowing caller cancellation to abandon an owned task."""

    caller_cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if caller_cancelled is None:
                caller_cancelled = exc
        except BaseException:
            break
    try:
        task.result()
    except BaseException as exc:
        return exc, caller_cancelled
    return None, caller_cancelled


async def _close_worker_mailbox(
    staged: StagedCommandMailbox,
    *,
    stop: asyncio.Event | None,
    task: asyncio.Task[None] | None,
    execution: AgentTaskExecution,
    primary_error: BaseException | None = None,
) -> None:
    if stop is not None:
        stop.set()

    serve_error: BaseException | None = None
    caller_cancelled: asyncio.CancelledError | None = None
    if task is not None:
        serve_error, caller_cancelled = await _wait_for_owned_task(task)

    cleanup_task = asyncio.create_task(asyncio.to_thread(staged.cleanup))
    cleanup_error, cleanup_cancelled = await _wait_for_owned_task(cleanup_task)
    if caller_cancelled is None:
        caller_cancelled = cleanup_cancelled

    def warning(message: str) -> None:
        with suppress(Exception):
            execution.store.record_agent_task_event(
                execution.operation_id,
                message,
                level="warning",
            )

    expected_errors = (OSError, StateUnavailable, ValueError)
    if primary_error is not None:
        if serve_error is not None:
            warning(f"AutoResearch command mailbox became unavailable: {serve_error}")
        if cleanup_error is not None:
            warning(f"AutoResearch command mailbox cleanup failed: {cleanup_error}")
        return

    if caller_cancelled is not None:
        if serve_error is not None and not isinstance(serve_error, asyncio.CancelledError):
            warning(f"AutoResearch command mailbox became unavailable: {serve_error}")
        if cleanup_error is not None and not isinstance(cleanup_error, asyncio.CancelledError):
            warning(f"AutoResearch command mailbox cleanup failed: {cleanup_error}")
        raise caller_cancelled

    if serve_error is not None:
        if isinstance(serve_error, expected_errors):
            warning(f"AutoResearch command mailbox became unavailable: {serve_error}")
        else:
            if cleanup_error is not None:
                warning(f"AutoResearch command mailbox cleanup failed: {cleanup_error}")
            raise serve_error
    if cleanup_error is not None:
        if isinstance(cleanup_error, expected_errors):
            warning(f"AutoResearch command mailbox cleanup failed: {cleanup_error}")
        else:
            raise cleanup_error


async def _settle_worker_patch(
    service: ProjectService,
    execution: AgentTaskExecution,
    turn: _CanonicalWorkerTurn | _CanonicalOrchestratorTurn,
    stage: _WorkerStage,
    *,
    write_scope: ProjectWriteScope,
    native_session_id: str,
    retry_patch_digest: str | None,
    correction: _Correction | None,
    failed_correction: str | None = None,
    _actor_role: Literal["worker", "orchestrator"] = "worker",
    _profile: Literal["ordinary", "orchestrator"] = "ordinary",
    _capability: Literal["work_auto", "orchestrate"] = "work_auto",
) -> _PatchSettlement:
    if failed_correction is not None:
        # A recorded correction that failed ends the rounds as a live one does.
        rejected = failed_graph_update(
            _WorkPatchFailure(failed_correction, correctable=True),
            bounded_messages=_bounded_graph_messages,
            correction_rounds=PATCH_CORRECTION_MAX_ROUNDS,
        )
        _record_work_graph_rejection(execution, rejected)
        return _PatchSettlement(rejected, (), had_patch=True)
    try:
        patch_text = _read_chat_patch(stage.workspace, stage.remote)
    except (OSError, StateUnavailable, ValueError) as exc:
        if correction is None and isinstance(exc, StateUnavailable):
            # Only a recorded pass settles without a correction owner. It has
            # applied nothing yet, so its settlement waits out the outage.
            raise
        patch_text = None
        failure: _WorkPatchFailure | None = _WorkPatchFailure(
            f"The auto_research {_actor_role} wrote a patch file that could not be read: {exc}",
            correctable=False,
        )
    else:
        failure = None
    if _retry_deliverable_is_unchanged(
        execution,
        filename="patch.json",
        predecessor_digest=retry_patch_digest,
        current_text=patch_text,
    ):
        patch_text = None
    had_patch = patch_text is not None or failure is not None
    if patch_text is None and failure is None:
        return _PatchSettlement(GraphUpdateResult(status="none"))

    correction_rounds = 0
    correction_frames: list[str] = []
    staged_head = correction.staged_head if correction is not None else None
    while True:
        if patch_text is not None:
            source_effect_id: str | None = None
            recovered_apply = False
            patch_digest = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
            if _profile == "orchestrator":
                source_effect_id, recovered_apply = _orchestrator_final_source_effect_id(
                    service,
                    execution,
                    patch_digest=patch_digest,
                )
            try:
                result, failure = _apply_work_patch(
                    service,
                    execution,
                    patch_text,
                    run_truth_scope=turn.request.run_truth_scope
                    or service.manifest.agent.default_run_truth_scope,
                    profile=_profile,
                    source_effect_id=source_effect_id,
                )
                if correction is None and failure is not None and failure.commit_status == "absent":
                    # Canonical state was unreachable before anything published:
                    # a recorded pass retries rather than spending its result.
                    raise StateUnavailable(failure.message)
                if result is not None and recovered_apply:
                    command_files = RunStageMailbox.for_stage(
                        local_stage=stage.local, remote_stage=stage.remote
                    )
                    if not command_files.remove_if_sha256("patch.json", patch_digest):
                        raise ValueError(
                            "The retained in-turn Apply Patch changed before final settlement."
                        )
            except RunLockCancelled:
                correction_frames.append(
                    _sse(
                        AgentEvent(
                            event="paused",
                            text=(
                                f"Paused while waiting for canonical state. The auto_research "
                                f"{_actor_role} "
                                "answer and retained patch are preserved."
                            ),
                        )
                    )
                )
                return _PatchSettlement(None, tuple(correction_frames), had_patch=had_patch)
            if result is not None:
                return _PatchSettlement(
                    result.model_copy(update={"correction_rounds": correction_rounds}),
                    tuple(correction_frames),
                    had_patch=had_patch,
                )
        assert failure is not None
        if (
            not failure.correctable
            or correction is None
            or correction_rounds >= PATCH_CORRECTION_MAX_ROUNDS
            or not native_session_id
        ):
            rejected = failed_graph_update(
                failure,
                bounded_messages=_bounded_graph_messages,
                correction_rounds=correction_rounds,
            )
            _record_work_graph_rejection(execution, rejected)
            return _PatchSettlement(rejected, tuple(correction_frames), had_patch=had_patch)

        correction_rounds += 1
        execution.store.record_agent_task_receipt(
            execution.operation_id,
            "patch_correction_requested",
            {"round": correction_rounds, "problem": failure.message[:400]},
            tier="diagnostic",
        )
        execution.store.update_agent_task_message(
            execution.operation_id,
            f"Correcting auto_research {_actor_role} graph reflection.",
            phase="correcting",
            event=True,
        )
        token = _task_token(execution)
        diagnostics_path = _stage_json_task_input(
            stage.local,
            stage.remote,
            f"task-{token}-auto_research-work-correction-{correction_rounds}.json",
            {"kind": "work", "problem": failure.message},
        )
        context = _auto_research_context(service, turn.request, stage)
        # The launch's group, which the reused read_dirs were built from.
        context.group_context = correction.group_context
        graph_read_scope = GraphReadScope.resolve(
            turn.task.project_id, service.history, context.group_context, store=service.paper.store
        )
        correction_head = prepare_graph_observation(
            execution,
            service,
            owner_id=_actor_seen_owner(turn),
            target=graph_read_scope.target,
            revision=context.graph_revision,
            native_session_id=native_session_id,
        )
        correction_mailbox = stage_command_mailbox(
            local_stage=stage.local,
            remote_stage=stage.remote,
            episode_id=turn.request.episode_id,
            task_id=execution.operation_id,
            turn_id=(
                f"{execution.operation_id}:{_actor_role}-patch-correction:{correction_rounds}"
            ),
        )
        correction_outcome = _ProviderOutcome(session_id=native_session_id)
        async with _actor_mailbox(
            correction_mailbox,
            execution=execution,
            outcome=correction_outcome,
            # Built inside the mailbox's lifetime, so a setup failure still closes it.
            serve=lambda mailbox=correction_mailbox, round_number=correction_rounds, scope=graph_read_scope, **state: (
                _serve_auto_research_commands(
                    mailbox,
                    execution=execution,
                    episode_id=turn.request.episode_id,
                    dispatcher=_ValidateOnlyAutoResearchCommandDispatcher(
                        correction.command_dispatcher.store,
                        correction.command_dispatcher.effects,
                        service=service,
                        graph_read_scope=scope,
                    ),
                    service=service,
                    graph_read_scope=scope,
                    expected_turn_id=(
                        f"{execution.operation_id}:{_actor_role}-patch-correction:{round_number}"
                    ),
                    **state,
                )
            ),
            resume_context=_actor_mailbox_context(
                f"{execution.operation_id}:{_actor_role}-patch-correction:{correction_rounds}",
                graph_read_scope,
                None,
            ),
        ):
            # This operation's own launch staged or restored the session's master moments ago,
            # so the correction points to it rather than looking up a settled record.
            correction_grant = BrowserGrant()
            correction_values = {
                **correction.values,
                "graph": context.graph_path,
                "research": context.research_md_path,
                "command_prefix": correction_mailbox.client_command(),
                "browser": correction_grant.model_dump(
                    include={"status", "reason_code", "session_name", "invocation_dir"}
                ),
            }
            correction_prompt = compose(
                classify(LaunchPhase(session_id=native_session_id, phase="correction")),
                parts=[
                    *auto_research_patch_correction_parts(
                        actor=_actor_role,
                        diagnostics_path=diagnostics_path,
                        graph_read_scope=graph_read_scope,
                        command_client=correction_mailbox.client_command(),
                    ),
                    render_group_context(context.group_context),
                    stage_operational_context_pointer(
                        execution,
                        stage.local,
                        stage.remote,
                    ),
                    browser_prompt_line(correction_grant),
                ],
                master=correction.master,
                delta={
                    **(changed_since_master(correction.master, correction_values) or {}),
                    **graph_head_delta(staged_head, correction_head),
                },
            )
            staged_head = correction_head
            correction_path = record_inline_prompt(
                execution,
                local_stage=stage.local,
                remote_stage=stage.remote,
                label=f"task-{token}-auto_research-work-correction-{correction_rounds}.md",
                role=f"auto_research_{_actor_role}_patch_correction_{correction_rounds}",
                prompt=correction_prompt,
            )
            _record_agent_launch_receipt(
                execution,
                cast(RunRequest, turn.request),
                prompt=correction_prompt,
                contract_path=correction_path,
                remote=bool(stage.execution_host),
                resumed=True,
                write_scope=write_scope,
                continuation="graph_correction",
                extra={
                    "surface": "auto_research",
                    "role": _actor_role,
                    "profile": _profile,
                    "capability": _capability,
                    "network_access": True,
                    "launch_kind": "graph_correction",
                    "correction_round": correction_rounds,
                    "write_directory_count": len(correction.write_dirs),
                    "repeat_operational_work": False,
                },
            )
            correction_error: str | None = None
            async with aclosing(
                _stream_agent_events(
                    correction.launcher,
                    cast(RunRequest, turn.request),
                    correction_prompt,
                    service=service,
                    workspace=stage.workspace,
                    session_id=native_session_id,
                    read_dirs=correction.read_dirs,
                    write_dirs=correction.write_dirs,
                    write_scope=write_scope,
                    execution_host=stage.execution_host,
                    execution=execution,
                    remote_stage=stage.remote,
                    capability=_capability,
                    browser_grant=correction_grant,
                    outcome=correction_outcome,
                    binary=correction.provider_binary,
                    invocation_gate=correction_mailbox.invocation_gate,
                    required_session_id=native_session_id,
                )
            ) as stream:
                async for frame in stream:
                    event = AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
                    if event.event == "error":
                        correction_error = (
                            event.text or f"AutoResearch {_actor_role} Patch correction failed."
                        )
                    else:
                        correction_frames.append(frame)
        if correction_outcome.remote_result_pending:
            return _PatchSettlement(
                None, tuple(correction_frames), had_patch=had_patch, awaiting_remote_result=True
            )
        if correction_outcome.paused:
            return _PatchSettlement(None, tuple(correction_frames), had_patch=had_patch)
        if (
            correction_error is not None
            or correction_outcome.failed
            or not correction_outcome.completed
            or correction_outcome.session_id != native_session_id
        ):
            failure = _WorkPatchFailure(
                correction_error
                or (
                    f"AutoResearch {_actor_role} Patch correction did not complete in its saved "
                    "session."
                ),
                correctable=True,
                change_summary=failure.change_summary,
                proposal_ids=failure.proposal_ids,
            )
            patch_text = None
            correction_rounds = PATCH_CORRECTION_MAX_ROUNDS
            continue
        corrected: _CorrectionPatchRead = _read_correction_patch(
            stage.workspace,
            stage.remote,
        )
        if corrected.problem == "unreadable":
            failure = _WorkPatchFailure(
                f"The corrected patch could not be read: {corrected.detail}",
                correctable=True,
                change_summary=failure.change_summary,
                proposal_ids=failure.proposal_ids,
            )
            patch_text = None
        elif corrected.problem == "missing":
            failure = _WorkPatchFailure(
                "The correction completed without writing patch.json.",
                correctable=True,
                change_summary=failure.change_summary,
                proposal_ids=failure.proposal_ids,
            )
            patch_text = None
        else:
            assert corrected.text is not None
            patch_text = corrected.text


def _orchestrator_final_source_effect_id(
    service: ProjectService,
    execution: AgentTaskExecution,
    *,
    patch_digest: str,
) -> tuple[str, bool]:
    """Reuse an in-turn Apply commit when its exact handoff survives to settlement."""

    matches = [
        patch
        for patch in service.history.load_patches()
        if patch.admission == "accepted"
        and patch.kind == "work"
        and patch.source_operation_id == execution.operation_id
        and patch.source_effect_sha256 == patch_digest
    ]
    if len(matches) > 1:
        raise ValueError(
            "The retained orchestrator Patch matches multiple canonical in-turn Apply commits."
        )
    if matches:
        source_effect_id = matches[0].source_effect_id
        if source_effect_id is None:
            raise ValueError(
                "The retained orchestrator Patch matches a canonical commit without an effect id."
            )
        return source_effect_id, True
    return (
        str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "rcp:auto-research-final-apply:" + execution.operation_id,
            )
        ),
        False,
    )


def _record_auto_research_finalization(
    execution: AgentTaskExecution,
    stage: _WorkerStage,
    write_scope: ProjectWriteScope,
    retry_patch_digest: str | None,
) -> None:
    """Retain what a recorded pass of this turn needs, before it launches."""

    if execution.stage_root is None:
        raise ValueError("An Auto-research launch has no checkpointed stage.")
    content = _AutoResearchFinalization(
        stage_host=stage.execution_host,
        stage_root=execution.stage_root,
        workspace=str(stage.workspace),
        write_scope=write_scope,
        retry_patch_digest=retry_patch_digest,
    ).model_dump_json()
    execution.store.record_agent_task_contract(
        execution.operation_id,
        AUTO_RESEARCH_FINALIZATION_ROLE,
        content,
        hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


@asynccontextmanager
async def _actor_mailbox(
    staged: StagedCommandMailbox,
    *,
    execution: AgentTaskExecution,
    outcome: _ProviderOutcome,
    serve: Callable[..., Awaitable[None]],
    resume_context: dict[str, object],
) -> AsyncIterator[None]:
    """Serve one actor pass's commands; a remote pass keeps serving if its link drops."""

    if staged.mailbox.remote_stage is None:
        async with _worker_mailbox_lifecycle(
            staged, execution=execution, start=lambda stop: serve(stop=stop)
        ):
            yield
        return
    lifecycle = start_work_validator_mailbox(
        staged,
        execution=execution,
        budget=None,
        validate=None,
        serve=lambda stop, suspend, responses, terminal, checkpoint, **_: serve(
            stop=stop,
            suspend=suspend,
            responses=responses,
            terminal=terminal,
            checkpoint=checkpoint,
        ),
        resume_context=resume_context,
    )
    try:
        yield
    except BaseException as exc:
        await lifecycle.close(primary_error=exc)
        raise
    if outcome.remote_result_pending:
        lifecycle.detach()
    else:
        await lifecycle.close()


def _actor_mailbox_context(
    turn_id: str,
    graph_read_scope: GraphReadScope,
    allowed_verbs: tuple[CommandVerb, ...] | None,
) -> dict[str, object]:
    """What a relaunched RCP needs to serve this pass's mailbox again; no verbs is a correction."""

    return {
        "turn_id": turn_id,
        "graph_read_scope": graph_read_scope.model_dump(mode="json"),
        "allowed_verbs": list(allowed_verbs) if allowed_verbs is not None else None,
    }


def _bind_actor_dispatcher(
    dispatcher: AutoResearchCommandDispatcher,
    service: ProjectService,
    request: AutoResearchRunRequest,
    stage: _WorkerStage,
    graph_read_scope: GraphReadScope,
    allowed_verbs: tuple[CommandVerb, ...],
) -> AutoResearchCommandDispatcher:
    """Bind one actor pass's command surface, at launch and again after a relaunch."""

    command_files = RunStageMailbox.for_stage(local_stage=stage.local, remote_stage=stage.remote)
    if request.role != "orchestrator":
        return dispatcher.with_allowed_verbs(allowed_verbs, mailbox=command_files)
    return dispatcher.with_command_files(
        mailbox=command_files,
        service=service,
        graph_read_scope=graph_read_scope,
        allowed_verbs=allowed_verbs,
        refresher=lambda: _refreshed_orchestrator_state_paths(service, request, stage),
    )


def resume_auto_research_command_mailbox(
    service: Callable[[], ProjectService],
    execution: AgentTaskExecution,
    request: AutoResearchRunRequest,
    dispatcher: Callable[[ProjectService], AutoResearchCommandDispatcher],
) -> WorkValidatorMailboxLifecycle | None:
    """Serve a detached actor pass's mailbox again after RCP relaunches."""

    saved = load_work_mailbox_context(execution)
    if saved is None:
        return None
    retained = _retained_finalization(execution)
    if not retained.stage_host:
        _retained, stage = _retained_stage(execution)
    elif (execution.stage_host, execution.stage_root) != (
        retained.stage_host,
        retained.stage_root,
    ):
        raise ValueError("The detached Auto-research mailbox belongs to another stage.")
    else:
        # Rebuilt from the binding alone: the host may be unreachable, and the
        # restored mailbox waits out an outage with its own backoff.
        remote = RemoteRunStage(retained.stage_host)
        remote.root = PurePosixPath(retained.stage_root)
        stage = _WorkerStage(None, remote, Path(retained.workspace), retained.stage_host, None)
    turn_request = (
        _canonical_orchestrator_turn(execution, request)
        if request.role == "orchestrator"
        else _canonical_worker_turn(execution, request)
    ).request
    project = service()
    scope = GraphReadScope.model_validate(saved["graph_read_scope"])
    allowed_verbs = saved["allowed_verbs"]
    unbound = dispatcher(project)
    bound = (
        _ValidateOnlyAutoResearchCommandDispatcher(
            unbound.store, unbound.effects, service=project, graph_read_scope=scope
        )
        if allowed_verbs is None
        else _bind_actor_dispatcher(
            unbound, project, turn_request, stage, scope, tuple(cast(list, allowed_verbs))
        )
    )

    def serve(*, staged, stop, suspend, responses, terminal, checkpoint, **_):
        return _serve_auto_research_commands(
            staged,
            execution=execution,
            episode_id=turn_request.episode_id,
            dispatcher=bound,
            service=project,
            graph_read_scope=scope,
            expected_turn_id=str(saved["turn_id"]),
            stop=stop,
            suspend=suspend,
            responses=responses,
            terminal=terminal,
            checkpoint=checkpoint,
        )

    return restore_work_validator_mailbox(execution, validate=None, serve=serve)


def _retained_finalization(execution: AgentTaskExecution) -> _AutoResearchFinalization:
    content = execution.store.agent_task_contract(
        execution.operation_id, AUTO_RESEARCH_FINALIZATION_ROLE
    )
    if content is None:
        raise ValueError("The Auto-research finalization context is unavailable.")
    return _AutoResearchFinalization.model_validate_json(content)


def _retained_stage(
    execution: AgentTaskExecution,
) -> tuple[_AutoResearchFinalization, _WorkerStage]:
    retained = _retained_finalization(execution)
    local, remote, workspace = attach_retained_stage(
        execution,
        owner="Auto-research",
        stage_host=retained.stage_host,
        stage_root=retained.stage_root,
        workspace=retained.workspace,
    )
    stage = _WorkerStage(local, remote, workspace, retained.stage_host, None)
    return retained, stage


async def _settle_actor_turn(
    service: ProjectService,
    execution: AgentTaskExecution,
    turn: _CanonicalWorkerTurn | _CanonicalOrchestratorTurn,
    stage: _WorkerStage,
    outcome: _ProviderOutcome,
    *,
    write_scope: ProjectWriteScope,
    retry_patch_digest: str | None,
    correction: _Correction | None,
    answer: str | None = None,
    failed_correction: str | None = None,
) -> AsyncIterator[str]:
    """Settle one finished actor pass, watched live or read back from its host."""

    role = turn.binding.role
    if outcome.paused or outcome.failed:
        return
    if not outcome.completed:
        yield _error(f"{turn.request.provider} produced no auto_research {role} result.")
        return
    if not outcome.session_id:
        yield _error(f"AutoResearch {role} returned no native session id.")
        return
    if (
        turn.binding.native_session_id is not None
        and outcome.session_id != turn.binding.native_session_id
    ):
        yield _error(f"AutoResearch {role} continuation changed its canonical native session.")
        return
    if answer is None:
        answer = "\n\n".join(item.strip() for item in outcome.answers if item.strip())
    if not answer:
        yield _error(f"AutoResearch {role} finished without an answer.")
        return
    if role == "orchestrator":
        mark_auto_research_question_snapshot_delivered(execution.store, execution.operation_id)
    yield _sse(AgentEvent(event="answer", text=answer))
    execution.store.record_agent_task_contract(
        execution.operation_id,
        _AUTO_RESEARCH_PRIMARY_ANSWER_ROLE,
        answer,
        hashlib.sha256(answer.encode("utf-8")).hexdigest(),
    )
    orchestrator = role == "orchestrator"
    settlement = await _settle_worker_patch(
        service,
        execution,
        turn,
        stage,
        write_scope=write_scope,
        native_session_id=outcome.session_id,
        retry_patch_digest=retry_patch_digest,
        correction=correction,
        failed_correction=failed_correction,
        _actor_role=role,
        _profile="orchestrator" if orchestrator else "ordinary",
        _capability="orchestrate" if orchestrator else "work_auto",
    )
    for frame in settlement.frames:
        yield frame
    if settlement.awaiting_remote_result:
        return
    graph_update = settlement.graph_update
    if orchestrator:
        graph_updates = _ordered_orchestrator_graph_updates(execution)
        if graph_update is not None and (settlement.had_patch or not graph_updates):
            graph_updates.append(graph_update)
    else:
        graph_updates = [graph_update] if graph_update is not None else []
    if not graph_updates:
        return
    graph_update = graph_updates[-1]
    payload: dict[str, object] = {"graph_update": graph_update.model_dump(mode="json")}
    if orchestrator:
        payload["graph_updates"] = [item.model_dump(mode="json") for item in graph_updates]
    latest_applied_revision = next(
        (
            item.applied_revision
            for item in reversed(graph_updates)
            if item.applied_revision is not None
        ),
        None,
    )
    if latest_applied_revision is not None:
        payload["applied_revision"] = latest_applied_revision
    yield _sse(AgentEvent(event="message", text=json.dumps(payload, separators=(",", ":"))))
    if graph_update.status in {"applied", "none"}:
        commit_prepared_graph_observation(
            execution,
            service,
            native_session_id=outcome.session_id,
            applied_revision=latest_applied_revision,
        )
    yield _sse(AgentEvent(event="done"))


def _error(text: str) -> str:
    return _sse(AgentEvent(event="error", text=text))


async def finalize_recorded_auto_research_result(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    execution: AgentTaskExecution,
    recorded: RecordedProviderTurn,
) -> AsyncIterator[str]:
    """Settle an Auto-research pass from what its host recorded, on the same operation.

    Corrections are not relaunched. When the recorded pass was a correction, the
    turn's answer is the primary one retained before the correction started.
    """

    del launcher, data_dir
    supplied = cast(AutoResearchRunRequest, request)
    turn = (
        _canonical_orchestrator_turn(execution, supplied)
        if supplied.role == "orchestrator"
        else _canonical_worker_turn(execution, supplied)
    )
    retained, stage = _retained_stage(execution)
    outcome = _ProviderOutcome(session_id=turn.binding.native_session_id)
    verdict = decode_recorded_turn(
        recorded,
        provider_turn_request(
            stage.workspace,
            recorded,
            capability="orchestrate" if supplied.role == "orchestrator" else "work_auto",
            write_scope=retained.write_scope,
        ),
    )
    refused = refuse_recorded_session_mismatch(
        execution, outcome, verdict, turn.binding.native_session_id
    )
    for frame in refused:
        yield frame
    if refused:
        return
    write_recorded_patch(stage.workspace, stage.remote, recorded)
    primary_answer = execution.store.agent_task_contract(
        execution.operation_id, _AUTO_RESEARCH_PRIMARY_ANSWER_ROLE
    )
    # A correction follows the operation's first pass; settling that first pass
    # again after an outage keeps its own freshness baseline.
    starts = execution.store.agent_task_receipts_by_category(
        execution.operation_id, "remote_provider_started"
    )
    correction = bool(starts) and starts[0].payload.get("pid_file") != recorded.pid_file
    failed_correction: str | None = None
    for frame in absorb_recorded_events(outcome, verdict):
        event = AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
        if correction and primary_answer is not None and event.event == "error":
            # As live: a failed correction rejects the graph update, not the
            # operational work the primary pass already finished.
            failed_correction = failed_correction or event.text
            continue
        yield frame
    if correction and primary_answer is not None and not outcome.completed:
        failed_correction = (
            failed_correction or f"AutoResearch {supplied.role} Patch correction failed."
        )
        outcome.failed = False
        outcome.completed = True
    async for frame in _settle_actor_turn(
        service,
        execution,
        turn,
        stage,
        outcome,
        write_scope=retained.write_scope,
        retry_patch_digest=None if correction else retained.retry_patch_digest,
        correction=None,
        answer=primary_answer,
        failed_correction=failed_correction,
    ):
        yield frame
