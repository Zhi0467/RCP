from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

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
    CommandTurnIdentity,
    StagedCommandMailbox,
    serve_command_mailbox,
    stage_command_mailbox,
)
from rcp.agents.command_protocol import (
    CommandRequest,
    CommandResponse,
    MessageCommandRequest,
    ValidateCommandRequest,
)
from rcp.agents.context import ChatContext
from rcp.agents.experiment_loop_prompt import (
    experiment_loop_continuation_contract,
    experiment_loop_patch_correction_contract,
    experiment_loop_task_contract,
    experiment_loop_wake_message,
    experiment_loop_watcher_correction_contract,
    experiment_watcher_maintenance_correction_contract,
)
from rcp.agents.prompts import (
    CHAT_MASTER_CONTEXT_VERSION,
    invoked_package_pointers,
    invoked_provider_skill_section,
)
from rcp.agents.write_scope import ProjectWriteScope
from rcp.attachments import ChatAttachmentStore
from rcp.background import AgentTaskContinuation, AgentTaskExecution
from rcp.config import AgentSurface
from rcp.core.authority import AgentProfile
from rcp.core.models import ExperimentDecisionPin, Patch
from rcp.core.operations import CreateProposalsOperation
from rcp.history import PatchRejected, ReplayHalted
from rcp.limits import (
    AUTO_RESEARCH_MAIL_MAX_BYTES,
    PATCH_CORRECTION_MAX_ROUNDS,
    PATCH_SELF_CHECK_MAX_COUNT,
    PATCH_SELF_CHECK_POLL_SECONDS,
    PATCH_SELF_CHECK_TIMEOUT_SECONDS,
)
from rcp.runs.auto_research_mail import (
    AUTO_RESEARCH_MAIL_HANDOFF_FILE,
    auto_research_mail_delivery,
    parse_auto_research_mail_delivery,
    stage_auto_research_mail_delivery,
)
from rcp.runs.chat import (
    _append_chat_exchange,
    _append_chat_graph_receipt,
    _chat_context_delta,
    _chat_read_dirs,
    _chat_stage_name,
    _ChatPatchInputs,
    _clear_stale_turn_handoffs,
    _commit_chat_prompt_state,
    _discover_chat_artifacts,
    _logical_chat_turn_operation_id,
    _prepare_chat_prompt_state,
    _prepare_local_artifact_directory,
    _project_write_scope,
    _read_chat_patch,
    _read_watch_request,
    _record_applied_graph_revision,
    _record_artifact_discovery_receipt,
    _record_chat_context_receipt,
    _stage_chat_patch_inputs,
    _validated_local_chat_resume_stage,
    _validated_remote_chat_resume_stage,
)
from rcp.runs.experiment_loop import (
    StagedExperimentWatcherResource,
    commit_experiment_episode_binding,
    commit_experiment_episode_handoff,
    experiment_episode_context_values,
    experiment_exit_problem,
    experiment_graph_result_summary,
    experiment_loop_ending_signal,
    experiment_loop_semantic_ending,
    experiment_watcher_output_name,
    persist_experiment_watchers_idempotently,
    prepare_experiment_episode_context_candidate,
    prepare_experiment_watcher_records,
    read_experiment_watcher_outputs,
    root_experiment_loop_operation_id,
    stage_chat_experiment_watcher_resources,
    stage_experiment_loop_context,
    validate_experiment_completion,
)
from rcp.runs.patch_validator import (
    PatchValidationBudget,
    PatchValidationResult,
    serve_patch_validation_mailbox,
    stage_patch_validation_mailbox,
)
from rcp.runs.shared import (
    _existing_patch_digest,
    _parent_task_contract_path,
    _pinned_to_profile,
    _ProviderOutcome,
    _record_agent_launch_receipt,
    _record_patch_applied_receipt,
    _record_patch_receipt,
    _sse,
    _stage_context_paths,
    _stage_json_task_input,
    _stage_task_contract,
    _stage_task_input,
    _stream_agent_events,
    _swept_stage_root,
    _task_token,
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
from rcp.service import GraphUpdateResult, ProjectService, RunRequest
from rcp.skill_registry import SkillSelection
from rcp.skills.staging import skill_bundle_label, stage_skill_selection
from rcp.storage import (
    AgentCommandInvocationRecord,
    AutoResearchChildWorkRecord,
    AutoResearchMessageRecord,
    EpisodeRecord,
    ResultViewRecord,
    WatcherContinuation,
)
from rcp.transport import RemoteRunStage, RunLockCancelled, RunStageMailbox, StateUnavailable
from rcp.watchers import (
    WatcherBinding,
    WatcherInitialCheckError,
    arm_watchers,
    parse_experiment_watch_json,
    parse_watch_json,
    validate_graph_conditions,
    validate_watch_specs,
)


@dataclass(frozen=True)
class _DeliverableFailure:
    message: str
    correctable: bool
    change_summary: tuple[str, ...] = ()
    proposal_ids: tuple[str, ...] = ()


# Auto-research shares the same patch-failure value while its orchestration remains separate.
_WorkPatchFailure = _DeliverableFailure


@dataclass(frozen=True)
class _PreparedWorkPatch:
    patch: Patch
    change_summary: tuple[str, ...]
    proposal_ids: tuple[str, ...]


@dataclass(frozen=True)
class _CorrectionPatchRead:
    text: str | None
    problem: Literal["unreadable", "missing", "unchanged"] | None = None
    detail: str | None = None


@dataclass
class _WorkValidatorMailboxLifecycle:
    staged: StagedCommandMailbox
    execution: AgentTaskExecution | None
    stop: asyncio.Event
    task: asyncio.Task[None]
    closed: bool = False

    async def close(self, *, primary_error: BaseException | None = None) -> None:
        if self.closed:
            return
        self.closed = True
        await _close_work_validator_mailbox(
            self.staged,
            stop=self.stop,
            task=self.task,
            execution=self.execution,
            primary_error=primary_error,
        )


@dataclass
class WorkTurn:
    """Cross-phase state carried by one operational Work turn."""

    service: ProjectService
    request: RunRequest
    execution: AgentTaskExecution | None
    context: ChatContext
    workspace: Path
    local_stage: Path | None
    remote_stage: RemoteRunStage | None
    execution_host: str
    provider_binary: str | None
    read_dirs: list[Path]
    write_dirs: list[Path]
    write_scope: ProjectWriteScope
    patch_inputs: _ChatPatchInputs
    validator_lifecycle: _WorkValidatorMailboxLifecycle
    validator_budget: PatchValidationBudget
    outcome: _ProviderOutcome
    answer: str | None = None

    @property
    def continuation(self) -> AgentTaskContinuation:
        return self.execution.continuation if self.execution is not None else "fresh"

    @property
    def surface(self) -> AgentSurface:
        return "project_chat" if self.request.chat_scope == "project" else "node_chat"

    @property
    def reusing_checkpoint(self) -> bool:
        return bool(self.execution is not None and self.execution.reuses_native_checkpoint)

    @property
    def resuming(self) -> bool:
        return self.continuation == "resume"

    @property
    def retrying(self) -> bool:
        return self.continuation == "retry"

    @property
    def waking(self) -> bool:
        return self.continuation == "watcher_wake"

    @property
    def message_waking(self) -> bool:
        return self.continuation == "message_wake"

    @property
    def auto_research_child(self) -> AutoResearchChildWorkRecord | None:
        if self.execution is None:
            return None
        return self.execution.store.auto_research_child_work_for_operation(
            self.execution.operation_id
        )

    @property
    def retry_attempt(self) -> bool:
        return self.continuation in {"retry", "handoff"}

    @property
    def uses_master_protocol(self) -> bool:
        return (
            self.request.trigger in {"human", "orchestrator"}
            and self.request.patch_kind == "work"
            and not self.retry_attempt
        )


async def _stream_turn_agent_events(
    turn: WorkTurn,
    launcher: AgentLauncher,
    prompt: str,
    *,
    session_id: str | None,
    outcome: _ProviderOutcome,
    validator_staged: StagedCommandMailbox | None = None,
    validator_lifecycle: _WorkValidatorMailboxLifecycle | None = None,
) -> AsyncIterator[str]:
    """Stream one Work provider continuation from the turn's staged execution context."""

    async with aclosing(
        _stream_work_agent_events(
            turn.service,
            launcher,
            turn.request,
            prompt,
            workspace=turn.workspace,
            session_id=session_id,
            read_dirs=turn.read_dirs,
            write_dirs=turn.write_dirs,
            write_scope=turn.write_scope,
            execution_host=turn.execution_host,
            execution=turn.execution,
            remote_stage=turn.remote_stage,
            capability="work_auto",
            outcome=outcome,
            binary=turn.provider_binary,
            validator_staged=validator_staged or turn.patch_inputs.validator_staged,
            validator_lifecycle=(
                validator_lifecycle if validator_staged is not None else turn.validator_lifecycle
            ),
            validator_budget=turn.validator_budget,
            run_truth_scope=turn.context.run_truth_scope,
            patch_kind=turn.request.patch_kind,
            control_node_id=turn.request.control_node_id,
            control_decision_bundle=turn.request.control_decision_bundle,
        )
    ) as stream:
        async for frame in stream:
            yield frame


@dataclass(frozen=True)
class _ResolvedWorkExecution:
    request: RunRequest
    execution_machine_alias: str
    execution_host: str
    provider_binary: str | None
    revision_preflight: tuple[ResultViewRecord, ResultViewSnapshot] | None


@dataclass(frozen=True)
class _StagedWorkInputs:
    token: str
    artifact_scope_id: str
    artifact_directory: Path | PurePosixPath
    prepared_result_view: _PreparedResultView | None
    experiment_resources: list[StagedExperimentWatcherResource]
    experiment_resource_pointers: list[dict[str, object]]
    skill_selection: SkillSelection
    skill_pointers: list[dict[str, object]]
    attachment_pointers: list[dict[str, object]]
    repositories: list[dict[str, object]]
    auto_research_mail_path: str | None = None


@dataclass(frozen=True)
class _WorkPromptContext:
    episode_context_baseline: dict[str, object] | None
    experiment_control_snapshot: dict[str, object] | None
    wake_episode: EpisodeRecord | None
    context_replacement: dict[str, object] | None
    loop_control_path: str | None
    watcher_state_path: str | None
    provider_switch_recovery: bool


@dataclass(frozen=True)
class _ComposedWorkPrompt:
    contract_path: str
    prompt: str
    base_contract_path: str


@dataclass(frozen=True)
class _RetryDeliverableBaseline:
    patch_digest: str | None
    watch_digest: str | None
    experiment_watch_digests: dict[str, str]


@dataclass
class _SettledWorkDeliverables:
    native_session_id: str | None
    graph_update: GraphUpdateResult = field(
        default_factory=lambda: GraphUpdateResult(status="none")
    )
    watch_correction_rounds: int = 0
    loop_watch_empty: bool = False
    loop_watch_text: str | None = None
    pending_loop_handoff: tuple[object, ...] | None = None
    stop: bool = False


@dataclass
class _AppliedWorkTurn:
    graph_update: GraphUpdateResult
    native_session_id: str | None
    stop: bool = False


@dataclass(frozen=True)
class _DeliverableRead:
    text: str | None
    failure: _DeliverableFailure | None = None


@dataclass(frozen=True)
class _DeliverableStep:
    failure: _DeliverableFailure | None = None
    frames: tuple[str, ...] = ()
    stop: bool = False


def _read_correction_patch(
    workspace: Path,
    remote_stage: RemoteRunStage | None,
    *,
    pre_launch_digest: str | None,
) -> _CorrectionPatchRead:
    """Classify one correction round's patch output without applying policy."""

    try:
        corrected = _read_chat_patch(workspace, remote_stage)
    except (OSError, StateUnavailable, ValueError) as exc:
        return _CorrectionPatchRead(text=None, problem="unreadable", detail=str(exc))
    if corrected is None:
        return _CorrectionPatchRead(text=None, problem="missing")
    if (
        pre_launch_digest is not None
        and hashlib.sha256(corrected.encode("utf-8")).hexdigest() == pre_launch_digest
    ):
        return _CorrectionPatchRead(text=None, problem="unchanged")
    return _CorrectionPatchRead(text=corrected)


def _work_patch_source_operation_id(
    execution: AgentTaskExecution | None,
    patch_kind: Literal["work", "experiment_loop"],
) -> str | None:
    if execution is None:
        return None
    if patch_kind == "experiment_loop" and execution.continuation != "graph_repair":
        return root_experiment_loop_operation_id(execution)
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
    prompt = PromptFactory.work_turn_prompt(
        artifact_path=artifact_path,
        human_message=request.message,
        master_context_path=bootstrap_path,
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
    )
    return prompt, retained_master_path


def _retry_deliverable_is_unchanged(
    execution: AgentTaskExecution | None,
    *,
    filename: str,
    predecessor_digest: str | None,
    current_text: str | None,
    allow_unchanged: bool = False,
) -> bool:
    """Record whether a reused Retry stage still contains its predecessor's output."""

    if execution is None or execution.continuation != "retry":
        return False
    current_digest = (
        hashlib.sha256(current_text.encode("utf-8")).hexdigest()
        if current_text is not None
        else None
    )
    unchanged = predecessor_digest is not None and current_digest == predecessor_digest
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "retry_deliverable_comparison",
        {
            "filename": filename,
            "predecessor_sha256": predecessor_digest,
            "retry_sha256": current_digest,
            "unchanged": unchanged,
            "consumed": current_text is not None and (not unchanged or allow_unchanged),
        },
        tier="diagnostic",
    )
    return unchanged and not allow_unchanged


def _experiment_loop_retry_handoff_authorized(
    turn: WorkTurn,
    watch_text: str | None,
) -> bool:
    """Authorize one exact retained loop watcher handoff from an ancestor receipt."""

    execution = turn.execution
    if (
        execution is None
        or execution.continuation != "retry"
        or turn.request.patch_kind != "experiment_loop"
        or watch_text is None
        or not turn.request.control_episode_id
        or turn.request.control_invocation is None
    ):
        return False
    try:
        root_id = root_experiment_loop_operation_id(execution)
        patch_text = _read_chat_patch(turn.workspace, turn.remote_stage)
    except (OSError, StateUnavailable, ValueError):
        return False
    patch_digest = (
        hashlib.sha256(patch_text.encode("utf-8")).hexdigest() if patch_text is not None else None
    )
    watch_digest = hashlib.sha256(watch_text.encode("utf-8")).hexdigest()
    current = execution.store.agent_task(execution.operation_id)
    if current is None:
        return False

    matched = False
    ancestor_id = current.parent_operation_id
    seen = {current.operation_id}
    while ancestor_id is not None:
        if ancestor_id in seen:
            return False
        seen.add(ancestor_id)
        ancestor = execution.store.agent_task(ancestor_id)
        if ancestor is None:
            return False
        if ancestor.request.get("patch_kind") == "experiment_loop":
            for receipt in execution.store.agent_task_receipts(ancestor.operation_id):
                if receipt.category != "experiment_loop_handoff_prepared":
                    continue
                payload = receipt.payload
                if (
                    payload.get("root_operation_id") == root_id
                    and payload.get("episode_id") == turn.request.control_episode_id
                    and payload.get("invocation") == turn.request.control_invocation
                    and payload.get("patch_sha256") == patch_digest
                    and payload.get("watch_sha256") == watch_digest
                ):
                    matched = True
                    break
        ancestor_id = ancestor.parent_operation_id
    return matched


def _experiment_maintenance_binding(
    execution: AgentTaskExecution,
    staged: StagedExperimentWatcherResource,
) -> WatcherBinding:
    """Bind maintenance authority from the durable actor and staged node resource."""

    task = execution.store.agent_task(execution.operation_id)
    if task is None:
        raise ValueError("Experiment watcher maintenance actor is no longer available.")
    if task.kind not in {"node_chat", "project_chat"}:
        raise ValueError("Experiment watcher maintenance requires a durable chat actor.")
    chat_id = task.request.get("chat_id")
    if not isinstance(chat_id, str) or not chat_id:
        raise ValueError("Experiment watcher maintenance actor has no durable conversation.")
    resource = staged.resource
    return WatcherBinding(
        project_id=task.project_id,
        origin_operation_id=execution.operation_id,
        origin_task_kind=task.kind,
        chat_id=chat_id,
        node_id=resource.control_node_id,
        episode_id=resource.continuation.control_episode_id,
        graph_target=task.graph_target,
        execution_host=resource.execution_host,
        continuation=resource.continuation,
    )


async def _process_experiment_watcher_maintenance(
    *,
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    execution: AgentTaskExecution | None,
    staged_resources: list[StagedExperimentWatcherResource],
    workspace: Path,
    remote_stage: RemoteRunStage | None,
    local_stage: Path | None,
    base_contract_path: str,
    token: str,
    native_session_id: str | None,
    read_dirs: list[Path | PurePosixPath],
    write_dirs: list[Path | PurePosixPath],
    write_scope: ProjectWriteScope,
    execution_host: str,
    provider_binary: str | None,
    retry_output_digests: dict[str, str],
) -> tuple[list[str], str | None, bool]:
    """Admit, validate, and atomically persist each physical Experiment watcher file."""

    if execution is None:
        return [], native_session_id, False
    frames: list[str] = []
    staged_by_name = {
        experiment_watcher_output_name(
            item.resource.control_node_id,
            item.resource.graph_target,
        ): item
        for item in staged_resources
    }
    try:
        outputs = read_experiment_watcher_outputs(workspace, remote_stage)
    except (OSError, StateUnavailable, ValueError) as exc:
        execution.store.record_agent_task_event(
            execution.operation_id,
            f"Experiment watcher maintenance output could not be inspected: {exc}",
            level="warning",
        )
        return frames, native_session_id, False

    for name, initial_text in sorted(outputs.items()):
        # A Retry reuses the conversation's folder without clearing it, so a
        # previous attempt's maintenance file is still sitting there. Applying it
        # would commit that attempt's handoff under this attempt's authorization
        # (invariant 10c), so an unchanged survivor counts as nothing written.
        if _retry_deliverable_is_unchanged(
            execution,
            filename=name,
            predecessor_digest=retry_output_digests.get(name),
            current_text=initial_text,
        ):
            continue
        staged = staged_by_name.get(name)
        if staged is None:
            problem = (
                "Experiment watcher maintenance permission denied: the physical output path was "
                "not staged for this actor's resolved resource scope."
            )
            execution.store.record_agent_task_receipt(
                execution.operation_id,
                "experiment_watcher_maintenance_rejected",
                {"path": str(workspace / name), "problem": problem},
                tier="diagnostic",
            )
            execution.store.record_agent_task_event(
                execution.operation_id,
                problem,
                level="warning",
            )
            continue

        text = initial_text
        correction_round = 0
        target_digest = hashlib.sha256(staged.resource.control_node_id.encode("utf-8")).hexdigest()[
            :16
        ]

        def reject_maintenance(
            problem: str,
            target: StagedExperimentWatcherResource,
            rounds: int,
        ) -> None:
            execution.store.record_agent_task_receipt(
                execution.operation_id,
                "experiment_watcher_maintenance_rejected",
                {
                    "control_node_id": target.resource.control_node_id,
                    "episode_id": target.resource.episode_id,
                    "path": target.watch_path,
                    "problem": problem[:1600],
                    "correction_rounds": rounds,
                },
                tier="diagnostic",
            )
            execution.store.record_agent_task_event(
                execution.operation_id,
                f"Experiment watcher maintenance was not applied: {problem}",
                level="warning",
            )

        while True:
            binding = _experiment_maintenance_binding(execution, staged)
            try:
                execution.store.admit_experiment_watcher_maintenance(binding)
            except ValueError as exc:
                problem = str(exc)
                correctable = False
            else:
                try:
                    handoff = parse_experiment_watch_json(text)
                    graph_state = (
                        await asyncio.to_thread(service.history.state)
                        if handoff.graph_conditions
                        else None
                    )
                    graph_armed_revision = graph_state.revision if graph_state is not None else None
                    if graph_state is not None:
                        await asyncio.to_thread(
                            validate_graph_conditions,
                            handoff.graph_conditions,
                            graph_state,
                        )
                    check_results = (
                        await asyncio.to_thread(
                            validate_watch_specs,
                            handoff.observers,
                            staged.resource.execution_host,
                        )
                        if handoff.observers
                        else []
                    )
                except (WatcherInitialCheckError, ValueError) as exc:
                    problem = str(exc)
                    correctable = True
                except (OSError, ReplayHalted, StateUnavailable) as exc:
                    problem = str(exc)
                    correctable = False
                else:
                    try:
                        fresh_graph_state = (
                            await asyncio.to_thread(service.history.state)
                            if handoff.graph_conditions
                            else None
                        )
                        if handoff.graph_conditions:
                            # Mark the settlement before the insert attempt. If
                            # cancellation lands after SQLite commits but before
                            # this await resumes, ordered reconciliation must
                            # still catch canonical movement after validation.
                            execution.armed_graph_watchers = True
                        armed = await asyncio.to_thread(
                            persist_experiment_watchers_idempotently,
                            execution,
                            handoff.observers,
                            check_results,
                            binding,
                            handoff.stops,
                            graph_conditions=handoff.graph_conditions,
                            graph_state=fresh_graph_state,
                            armed_revision=graph_armed_revision,
                            expected_watcher_snapshot_token=(
                                staged.resource.watcher_snapshot_token
                            ),
                        )
                    except (OSError, ReplayHalted, StateUnavailable, ValueError) as exc:
                        problem = str(exc)
                        correctable = False
                    else:
                        execution.store.record_agent_task_receipt(
                            execution.operation_id,
                            "experiment_watchers_maintained",
                            {
                                "control_node_id": staged.resource.control_node_id,
                                "episode_id": staged.resource.episode_id,
                                "watcher_ids": [item.watcher_id for item in armed],
                                "stopped_watcher_ids": [
                                    item.stop_watcher_id for item in handoff.stops
                                ],
                                "correction_rounds": correction_round,
                            },
                        )
                        break

            if (
                not correctable
                or correction_round >= PATCH_CORRECTION_MAX_ROUNDS
                or not native_session_id
            ):
                reject_maintenance(problem, staged, correction_round)
                break

            correction_round += 1
            diagnostics_path = _stage_json_task_input(
                local_stage,
                remote_stage,
                f"task-{token}-experiment-watch-correction-{target_digest}-{correction_round}.json",
                {
                    "control_node_id": staged.resource.control_node_id,
                    "problem": problem,
                },
            )
            correction_contract = experiment_watcher_maintenance_correction_contract(
                original_contract_path=base_contract_path,
                diagnostics_path=diagnostics_path,
                watch_path=staged.watch_path,
            )
            correction_path, correction_prompt = _stage_task_contract(
                local_stage,
                remote_stage,
                f"task-{token}-experiment-watch-correction-{target_digest}-{correction_round}.md",
                correction_contract,
                execution=execution,
                role=f"experiment_watch_correction_{target_digest}_{correction_round}",
            )
            before_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            _record_agent_launch_receipt(
                execution,
                request,
                prompt=correction_prompt,
                contract_path=correction_path,
                remote=bool(execution_host),
                resumed=True,
                write_scope=write_scope,
                continuation="watch_correction",
                extra={
                    "surface": binding.origin_task_kind,
                    "mode": "work",
                    "capability": "work_auto",
                    "network_access": True,
                    "launch_kind": "experiment_watch_correction",
                    "correction_round": correction_round,
                    "control_node_id": staged.resource.control_node_id,
                    "write_directory_count": len(write_dirs),
                    "canonical_state_boundary": "prompt_only",
                },
            )
            correction_outcome = _ProviderOutcome(session_id=native_session_id)
            correction_error: str | None = None
            async with aclosing(
                _stream_agent_events(
                    launcher,
                    request,
                    correction_prompt,
                    workspace=workspace,
                    session_id=native_session_id,
                    read_dirs=read_dirs,
                    write_dirs=write_dirs,
                    write_scope=write_scope,
                    execution_host=execution_host,
                    execution=execution,
                    remote_stage=remote_stage,
                    capability="work_auto",
                    outcome=correction_outcome,
                    binary=provider_binary,
                )
            ) as stream:
                async for frame in stream:
                    event = AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
                    if event.event == "error":
                        correction_error = event.text or "Watcher maintenance correction failed."
                    elif event.event not in {"answer", "done"}:
                        frames.append(frame)
            native_session_id = correction_outcome.session_id or native_session_id
            if correction_outcome.paused:
                return frames, native_session_id, True
            if correction_error or not correction_outcome.completed:
                problem = correction_error or (
                    f"{request.provider} produced no watcher maintenance correction result."
                )
                reject_maintenance(problem, staged, correction_round)
                break
            try:
                corrected_outputs = read_experiment_watcher_outputs(workspace, remote_stage)
            except (OSError, StateUnavailable, ValueError) as exc:
                problem = f"The corrected watcher maintenance output could not be read: {exc}"
                reject_maintenance(problem, staged, correction_round)
                break
            corrected = corrected_outputs.get(name)
            if corrected is None:
                problem = "The correction completed without rewriting the Experiment watcher file."
                reject_maintenance(problem, staged, correction_round)
                break
            if hashlib.sha256(corrected.encode("utf-8")).hexdigest() == before_digest:
                problem = (
                    f"{problem} The correction left the Experiment watcher file byte-identical."
                )
                reject_maintenance(problem, staged, correction_round)
                break
            text = corrected

    return frames, native_session_id, False


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


_CHILD_WORK_HANDOFFS_CLEARED_RECEIPT = "auto_research_child_work_handoffs_cleared"


def _prepare_auto_research_child_work_handoffs(
    execution: AgentTaskExecution,
    *,
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
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        _CHILD_WORK_HANDOFFS_CLEARED_RECEIPT,
        {
            "version": 1,
            "files": ["patch.json", "watch.json", "messages.json", "lifecycle.json"],
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

    mailbox = RunStageMailbox.for_stage(local_stage=local_stage, remote_stage=remote_stage)
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


async def _stage_work_turn(
    service: ProjectService,
    resolved: _ResolvedWorkExecution,
    data_dir: Path,
    execution: AgentTaskExecution | None,
) -> tuple[WorkTurn, _StagedWorkInputs]:
    request = resolved.request
    continuation = execution.continuation if execution is not None else "fresh"
    reusing_checkpoint = bool(execution is not None and execution.reuses_native_checkpoint)
    resuming = continuation == "resume"
    waking = continuation == "watcher_wake"
    message_waking = continuation == "message_wake"
    local_stage: Path | None = None
    remote_stage: RemoteRunStage | None = None
    patch_inputs: _ChatPatchInputs | None = None
    validator_lifecycle: _WorkValidatorMailboxLifecycle | None = None
    validator_budget = PatchValidationBudget()
    outcome = _ProviderOutcome(session_id=request.session_id)
    try:
        context = service.assemble_chat(request)
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
            stage_root = _swept_stage_root(data_dir)
            expected_stage = stage_root / stage_name
            if saved_stage:
                local_stage = _validated_local_chat_resume_stage(execution, expected_stage)
            else:
                local_stage = expected_stage
                local_stage.mkdir(parents=True, exist_ok=True)
            if execution is not None:
                execution.checkpoint_stage("", str(local_stage))
            workspace = local_stage
        _roll_result_view_retention(request, execution, local_stage, remote_stage)
        token = _task_token(execution)
        patch_inputs = _stage_chat_patch_inputs(
            local_stage,
            remote_stage,
            workspace=workspace,
            stage_name=stage_name,
            task_id=execution.operation_id if execution is not None else token,
            turn_id=f"{token}:work",
        )
        child_route = (
            execution.store.auto_research_child_work_for_operation(execution.operation_id)
            if execution is not None
            else None
        )
        if child_route is not None:
            patch_inputs.validator_staged.cleanup()
            child_staged = stage_command_mailbox(
                local_stage=local_stage,
                remote_stage=remote_stage,
                episode_id=child_route.episode_id,
                task_id=execution.operation_id,
                turn_id=f"{token}:auto-research-child-work",
                timeout_seconds=PATCH_SELF_CHECK_TIMEOUT_SECONDS,
            )
            patch_inputs = _ChatPatchInputs(
                patch_path=patch_inputs.patch_path,
                watch_path=patch_inputs.watch_path,
                schema_path=patch_inputs.schema_path,
                validator_command=child_staged.client_command(
                    "validate",
                    patch_inputs.patch_path,
                ),
                validator_mailbox_id=child_staged.credential.mailbox_id,
                validator_staged=child_staged,
            )
        validator_lifecycle = _start_work_validator_mailbox(
            service,
            patch_inputs.validator_staged,
            execution=execution,
            budget=validator_budget,
            run_truth_scope=context.run_truth_scope,
            patch_kind=request.patch_kind,
            control_node_id=request.control_node_id,
            control_decision_bundle=request.control_decision_bundle,
            auto_research_child=child_route,
        )
        if child_route is not None and (not reusing_checkpoint or message_waking):
            assert execution is not None
            _prepare_auto_research_child_work_handoffs(
                execution,
                workspace=workspace,
                remote_stage=remote_stage,
            )
        elif not reusing_checkpoint or waking:
            _clear_stale_turn_handoffs(workspace, remote_stage)
        auto_research_mail_path = (
            _stage_auto_research_child_work_mail(
                execution,
                child_route,
                local_stage=local_stage,
                remote_stage=remote_stage,
                continuation=continuation,
            )
            if execution is not None and child_route is not None
            else None
        )
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
            local_stage,
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
                local_stage,
                artifact_scope_id,
                reuse=resuming,
            )
        read_dirs = _chat_read_dirs(
            context,
            remote_stage,
            service,
            resolved.execution_machine_alias,
        )
        write_scope = _project_write_scope(
            context,
            service,
            resolved.execution_machine_alias,
            workspace=workspace,
            remote_stage=remote_stage,
            data_dir=data_dir,
            execution=execution,
            capability="work_auto",
        )
        write_dirs = [Path(item) for item in write_scope.repository_roots]
        experiment_resources = (
            await stage_chat_experiment_watcher_resources(
                request,
                execution,
                local_stage,
                remote_stage,
                workspace=workspace,
                token=token,
                clear_stale=not reusing_checkpoint or waking,
            )
            if request.patch_kind == "work" and child_route is None
            else []
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
            outcome=outcome,
        )
        return turn, _StagedWorkInputs(
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
            auto_research_mail_path=auto_research_mail_path,
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


async def _prepare_work_prompt_context(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
) -> _WorkPromptContext:
    episode_context_baseline: dict[str, object] | None = None
    experiment_control_snapshot: dict[str, object] | None = None
    wake_episode: EpisodeRecord | None = None
    context_replacement: dict[str, object] | None = None
    loop_control_path: str | None = None
    watcher_state_path: str | None = None
    provider_switch_recovery = False
    if turn.request.patch_kind == "experiment_loop":
        control_node = turn.context.node
        if (
            control_node is None
            or control_node.get("id") != turn.request.control_node_id
            or control_node.get("type") != "experiment"
        ):
            raise ValueError("Experiment-loop work no longer resolves to its Experiment.")
        experiment_control_snapshot = dict(control_node)
        loop_control_path, watcher_state_path = await stage_experiment_loop_context(
            turn.service,
            turn.request,
            turn.execution,
            turn.local_stage,
            turn.remote_stage,
            token=staged.token,
            continuation=turn.continuation,
        )
        assert turn.execution is not None
        episode = (
            turn.execution.store.experiment_episode(turn.request.control_episode_id)
            if turn.request.control_episode_id
            else None
        )
        provider_switch_recovery = bool(
            turn.continuation == "handoff" and episode is not None and episode.session_bound
        )
        ontology = turn.service.history.state().ontology.model_dump(mode="json")
        episode_context_baseline = prepare_experiment_episode_context_candidate(
            turn.execution,
            experiment_episode_context_values(
                ontology_extensions=turn.context.ontology_extensions,
                ontology=ontology,
                repositories=staged.repositories,
                skill_pointers=staged.skill_pointers,
            ),
        )
        if turn.waking:
            if not turn.request.control_episode_id or turn.request.control_invocation is None:
                raise ValueError("Experiment-loop wake is missing its episode invocation.")
            wake_episode = turn.execution.store.experiment_episode(turn.request.control_episode_id)
            if wake_episode is None or not wake_episode.session_bound:
                raise ValueError(
                    "Experiment-loop wake has no committed episode session to continue."
                )
            if (
                wake_episode.native_session_id != turn.request.session_id
                or wake_episode.stage_host != turn.execution.stage_host
                or wake_episode.stage_root != turn.execution.stage_root
            ):
                raise ValueError(
                    "Experiment-loop wake does not match its committed native session and exact "
                    "stage."
                )
            if wake_episode.last_turn_invocation != turn.request.control_invocation - 1:
                raise ValueError(
                    "Experiment-loop wake does not immediately follow the episode's last "
                    "successful turn."
                )
            if not wake_episode.last_graph_result:
                raise ValueError("Experiment-loop wake cannot confirm the preceding graph handoff.")
            context_replacement = _chat_context_delta(
                wake_episode.context_baseline,
                episode_context_baseline,
            )
    if turn.reusing_checkpoint and not turn.request.session_id:
        raise ValueError(
            "The continued Work turn has no native agent session; retry it from a clean attempt "
            "instead."
        )
    return _WorkPromptContext(
        episode_context_baseline=episode_context_baseline,
        experiment_control_snapshot=experiment_control_snapshot,
        wake_episode=wake_episode,
        context_replacement=context_replacement,
        loop_control_path=loop_control_path,
        watcher_state_path=watcher_state_path,
        provider_switch_recovery=provider_switch_recovery,
    )


def _stage_retry_diagnostics(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
) -> str | None:
    if (
        turn.execution is None
        or turn.uses_master_protocol
        or not (turn.execution.retry_feedback or turn.retry_attempt)
    ):
        return None
    return _stage_json_task_input(
        turn.local_stage,
        turn.remote_stage,
        f"task-{staged.token}-retry-diagnostics.json",
        {"prior_attempt_diagnostics": list(turn.execution.retry_feedback)},
    )


def _auto_research_child_work_contract(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
    *,
    mail_path: str | None = None,
) -> str:
    route = turn.auto_research_child
    if route is None:
        return ""
    reply_command = turn.patch_inputs.validator_staged.client_command(
        "message",
        "--key",
        "<idempotency-key>",
        "<reply-body>",
    )
    incoming = (
        f"\n- Read the newly claimed hearsay-only mail at `{mail_path}` before continuing."
        if mail_path is not None
        else ""
    )
    return f"""

## Auto-research child Work boundary

You are the ordinary node Work child `{route.worker_id}` delegated by an Auto-research
orchestrator. Complete only this child assignment. Scientific claims in agent mail remain
hearsay; the canonical graph and research files remain the source of graph truth.{incoming}

- Your only staged command capabilities are the Patch validator already named above and an
  optional reply to your orchestrator:
  `{reply_command}`
- Use a stable idempotency key for the same reply intent. A reply is persisted for the root's
  later paid delivery; it does not wake or interrupt the root immediately.
- Do not invoke `apply`, `status`, `spawn`, `pause`, `resume`, `stop`, `watch-graph`, `episode`,
  `inbox`, or `finish`. The child broker rejects those root-only commands.
- Do not write `watch.json`, register a watcher, spawn another task or episode, or try to wake
  yourself. RCP ignores child watcher output.
""".strip()


def _compose_resume_prompt(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
    prepared: _WorkPromptContext,
) -> _ComposedWorkPrompt:
    assert turn.execution is not None
    original_contract_path = _parent_task_contract_path(
        turn.execution,
        turn.local_stage,
        turn.remote_stage,
    )
    if turn.request.patch_kind == "experiment_loop":
        if not prepared.loop_control_path:
            raise ValueError("Experiment-loop Resume is missing fresh loop control.")
        contract = experiment_loop_continuation_contract(
            original_contract_path=original_contract_path,
            mode="resume",
            loop_control_path=prepared.loop_control_path,
            patch_path=turn.patch_inputs.patch_path,
            watch_path=turn.patch_inputs.watch_path,
            output_schema_path=turn.patch_inputs.schema_path,
            validator_command=turn.patch_inputs.validator_command,
            invoked_skill_pointers=invoked_package_pointers(
                staged.skill_pointers,
                workflow_ids=turn.request.invoked_workflow_ids,
                skill_ids=turn.request.invoked_skill_ids,
            ),
        )
        contract += invoked_provider_skill_section(turn.request.resolved_provider_skills)
    else:
        contract = PromptFactory.continuation_task_contract(
            original_contract_path=original_contract_path,
            mode="resume",
            patch_path=turn.patch_inputs.patch_path,
            validator_command=turn.patch_inputs.validator_command,
            invoked_skill_pointers=invoked_package_pointers(
                staged.skill_pointers,
                workflow_ids=turn.request.invoked_workflow_ids,
                skill_ids=turn.request.invoked_skill_ids,
            ),
            invoked_provider_skills=turn.request.resolved_provider_skills,
            result_view_action=(
                staged.prepared_result_view.action
                if staged.prepared_result_view is not None
                else None
            ),
            result_view_path=(
                staged.prepared_result_view.prompt_path
                if staged.prepared_result_view is not None
                else None
            ),
        )
        child_contract = _auto_research_child_work_contract(
            turn,
            staged,
            mail_path=staged.auto_research_mail_path,
        )
        if child_contract:
            contract += "\n\n" + child_contract
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
        base_contract_path=original_contract_path,
    )


def _compose_auto_research_child_message_wake_prompt(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
) -> _ComposedWorkPrompt:
    assert turn.execution is not None
    if turn.auto_research_child is None or staged.auto_research_mail_path is None:
        raise ValueError("Child Work message wake is missing its routed mail handoff.")
    original_contract_path = _parent_task_contract_path(
        turn.execution,
        turn.local_stage,
        turn.remote_stage,
    )
    contract = f"""
Continue the exact Auto-research child Work assignment from `{original_contract_path}` in the
same native provider session. The newly claimed agent mail is staged separately from the task
contract. Read it, continue the bounded assignment, and reply only if useful.

{_auto_research_child_work_contract(turn, staged, mail_path=staged.auto_research_mail_path)}
""".strip()
    contract_path, prompt = _stage_task_contract(
        turn.local_stage,
        turn.remote_stage,
        f"task-{staged.token}-auto-research-child-mail.md",
        contract,
        execution=turn.execution,
        role="auto_research_child_message_wake",
    )
    return _ComposedWorkPrompt(
        contract_path=contract_path,
        prompt=prompt,
        base_contract_path=original_contract_path,
    )


def _compose_wake_prompt(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
    prepared: _WorkPromptContext,
) -> _ComposedWorkPrompt:
    if (
        prepared.wake_episode is None
        or not turn.request.control_node_id
        or turn.request.control_invocation is None
        or turn.request.control_invocation_ceiling is None
        or not prepared.loop_control_path
        or not prepared.watcher_state_path
    ):
        raise ValueError("Experiment-loop wake inputs are incomplete after staging.")
    contract = experiment_loop_wake_message(
        focused_experiment_id=turn.request.control_node_id,
        invocation=turn.request.control_invocation,
        invocation_ceiling=turn.request.control_invocation_ceiling,
        previous_graph_result=prepared.wake_episode.last_graph_result or "",
        previous_watcher_ids=prepared.wake_episode.last_watcher_ids,
        delivered_watcher_ids=turn.request.watcher_ids,
        loop_control_path=prepared.loop_control_path,
        watcher_state_path=prepared.watcher_state_path,
        graph_path=turn.context.graph_path,
        research_path=turn.context.research_md_path,
        patch_path=turn.patch_inputs.patch_path,
        watch_path=turn.patch_inputs.watch_path,
        output_schema_path=turn.patch_inputs.schema_path,
        validator_command=turn.patch_inputs.validator_command,
        execution_host=turn.execution_host,
        context_replacement=prepared.context_replacement,
        invoked_skill_pointers=invoked_package_pointers(
            staged.skill_pointers,
            workflow_ids=turn.request.invoked_workflow_ids,
            skill_ids=turn.request.invoked_skill_ids,
        ),
    )
    contract_path, prompt = _stage_task_contract(
        turn.local_stage,
        turn.remote_stage,
        f"task-{staged.token}-watcher-wake.md",
        contract,
        execution=turn.execution,
        role="experiment_loop_wake",
    )
    return _ComposedWorkPrompt(
        contract_path=contract_path,
        prompt=prompt,
        base_contract_path=contract_path,
    )


def _compose_fresh_prompt(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
    prepared: _WorkPromptContext,
    *,
    retry_diagnostics_path: str | None = None,
) -> _ComposedWorkPrompt:
    assert turn.request.message is not None
    focused_node_id = str(turn.context.node["id"]) if turn.context.node else None
    if not turn.uses_master_protocol:
        human_request_path = _stage_task_input(
            turn.local_stage,
            turn.remote_stage,
            f"task-{staged.token}-human-request.txt",
            turn.request.message,
        )
        if turn.request.patch_kind == "experiment_loop":
            if (
                not turn.request.control_node_id
                or not prepared.loop_control_path
                or not prepared.watcher_state_path
            ):
                raise ValueError("Experiment-loop contract inputs are incomplete after staging.")
            contract = experiment_loop_task_contract(
                project_name=turn.context.project_name,
                ontology_path=f"{turn.context.graph_path}#ontology",
                ontology_extensions=turn.context.ontology_extensions,
                graph_path=turn.context.graph_path,
                research_path=turn.context.research_md_path,
                focused_experiment_id=turn.request.control_node_id,
                repositories=staged.repositories,
                introduction_path=turn.context.introduction_path,
                human_request_path=human_request_path,
                loop_control_path=prepared.loop_control_path,
                watcher_state_path=prepared.watcher_state_path,
                patch_path=turn.patch_inputs.patch_path,
                watch_path=turn.patch_inputs.watch_path,
                artifact_path=str(staged.artifact_directory),
                output_schema_path=turn.patch_inputs.schema_path,
                validator_command=turn.patch_inputs.validator_command,
                write_scope=turn.write_scope,
                execution_host=turn.execution_host,
                recovery_diagnostics_path=(
                    retry_diagnostics_path if prepared.provider_switch_recovery else None
                ),
                skill_pointers=staged.skill_pointers,
                invoked_skill_pointers=invoked_package_pointers(
                    staged.skill_pointers,
                    workflow_ids=turn.request.invoked_workflow_ids,
                    skill_ids=turn.request.invoked_skill_ids,
                ),
            )
            contract += invoked_provider_skill_section(turn.request.resolved_provider_skills)
        else:
            contract = PromptFactory.work_task_contract(
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
            )
        contract_path, prompt = _stage_task_contract(
            turn.local_stage,
            turn.remote_stage,
            f"task-{staged.token}-{'base' if turn.retry_attempt else 'initial'}.md",
            contract,
            execution=turn.execution,
            role="work_retry_base" if turn.retry_attempt else "work",
        )
        return _ComposedWorkPrompt(
            contract_path=contract_path,
            prompt=prompt,
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
    )
    child_contract = _auto_research_child_work_contract(
        turn,
        staged,
        mail_path=staged.auto_research_mail_path,
    )
    if child_contract:
        master_context += "\n\n" + child_contract + "\n"
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
    child_route = turn.auto_research_child
    if child_route is not None:
        stable_prompt_values["auto_research_child"] = {
            "episode_id": child_route.episode_id,
            "worker_id": child_route.worker_id,
            "control_node_id": child_route.control_node_id,
            "allowed_staged_commands": ["validate", "message"],
        }
    prompt, retained_master_path = _prepare_work_chat_prompt(
        turn.execution,
        turn.request,
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
    prepared: _WorkPromptContext,
) -> _ComposedWorkPrompt:
    assert turn.execution is not None
    retry_diagnostics_path = _stage_retry_diagnostics(turn, staged)
    resumed_retry = turn.retrying and turn.reusing_checkpoint
    loop_retry = turn.request.patch_kind == "experiment_loop" and turn.retrying
    explicit_contract = not turn.uses_master_protocol and not resumed_retry and not loop_retry
    current: _ComposedWorkPrompt | None = None
    if explicit_contract:
        current = _compose_fresh_prompt(
            turn,
            staged,
            prepared,
            retry_diagnostics_path=retry_diagnostics_path,
        )
    result_view_handoff = bool(
        turn.continuation == "handoff" and staged.prepared_result_view is not None
    )
    if result_view_handoff:
        if current is None:
            raise ValueError("The result view create handoff lost its current Work contract.")
        original_contract_path = current.contract_path
        continuation_contract_path = None
    else:
        original_contract_path = _parent_task_contract_path(
            turn.execution,
            turn.local_stage,
            turn.remote_stage,
        )
        continuation_contract_path = current.contract_path if current is not None else None
    base_contract_path = (
        original_contract_path if resumed_retry or current is None else current.base_contract_path
    )
    if turn.request.patch_kind == "experiment_loop":
        if not prepared.loop_control_path or not retry_diagnostics_path:
            raise ValueError("Experiment-loop Retry is missing fresh control or diagnostics.")
        retry_contract = experiment_loop_continuation_contract(
            original_contract_path=original_contract_path,
            mode="retry",
            loop_control_path=prepared.loop_control_path,
            patch_path=turn.patch_inputs.patch_path,
            watch_path=turn.patch_inputs.watch_path,
            output_schema_path=turn.patch_inputs.schema_path,
            validator_command=turn.patch_inputs.validator_command,
            diagnostics_path=retry_diagnostics_path,
            invoked_skill_pointers=invoked_package_pointers(
                staged.skill_pointers,
                workflow_ids=turn.request.invoked_workflow_ids,
                skill_ids=turn.request.invoked_skill_ids,
            ),
        )
        retry_contract += invoked_provider_skill_section(turn.request.resolved_provider_skills)
    else:
        retry_contract = PromptFactory.continuation_task_contract(
            original_contract_path=original_contract_path,
            current_contract_path=continuation_contract_path,
            diagnostics_path=retry_diagnostics_path,
            patch_path=turn.patch_inputs.patch_path,
            watch_path=turn.patch_inputs.watch_path,
            mode="retry",
            validator_command=turn.patch_inputs.validator_command,
            output_schema_path=turn.patch_inputs.schema_path if resumed_retry else None,
            skill_pointers=staged.skill_pointers if resumed_retry else None,
            invoked_skill_pointers=invoked_package_pointers(
                staged.skill_pointers,
                workflow_ids=turn.request.invoked_workflow_ids,
                skill_ids=turn.request.invoked_skill_ids,
            ),
            invoked_provider_skills=turn.request.resolved_provider_skills,
            result_view_action=(
                staged.prepared_result_view.action
                if staged.prepared_result_view is not None
                else None
            ),
            result_view_path=(
                staged.prepared_result_view.prompt_path
                if staged.prepared_result_view is not None
                else None
            ),
        )
        child_contract = _auto_research_child_work_contract(
            turn,
            staged,
            mail_path=staged.auto_research_mail_path,
        )
        if child_contract:
            retry_contract += "\n\n" + child_contract
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
        base_contract_path=base_contract_path,
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
        },
        tier="diagnostic",
    )
    return _RetryDeliverableBaseline(
        patch_digest=patch_digest,
        watch_digest=watch_digest,
        experiment_watch_digests=experiment_watch_digests,
    )


def _read_initial_patch_deliverable(
    turn: WorkTurn,
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
    if turn.request.patch_kind == "experiment_loop" and text is not None:
        if turn.execution is not None:
            turn.execution.store.record_agent_task_patch_output(
                turn.execution.operation_id,
                text,
            )
            turn.execution.store.record_agent_task_receipt(
                turn.execution.operation_id,
                "patch_retained",
                {
                    "byte_length": len(text.encode("utf-8")),
                    "file_name": "patch.json",
                },
                tier="diagnostic",
            )
        text = None
    if turn.request.patch_kind == "experiment_loop":
        # Loop graph admission is a joint Patch/watch handoff. Nothing in the
        # generic pre-handoff path may validate-correct-and-apply it.
        failure = None
    if text is None and failure is None:
        settled.graph_update = GraphUpdateResult(status="none")
    return _DeliverableRead(text=text, failure=failure)


def _read_initial_watch_deliverable(
    turn: WorkTurn,
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
    allow_unchanged = _experiment_loop_retry_handoff_authorized(turn, text)
    if _retry_deliverable_is_unchanged(
        turn.execution,
        filename="watch.json",
        predecessor_digest=predecessor_digest,
        current_text=text,
        allow_unchanged=allow_unchanged,
    ):
        text = None
    if turn.request.patch_kind == "experiment_loop" and text is None and failure is None:
        failure = _DeliverableFailure(
            "Experiment-loop work must write watch.json as an object with external and graph "
            "lists; leave both lists empty only after confirming nothing remains to watch.",
            correctable=True,
        )
    return _DeliverableRead(text=text, failure=failure)


async def _validate_patch_deliverable(
    turn: WorkTurn,
    _staged: _StagedWorkInputs,
    patch_text: str,
    correction_rounds: int,
    settled: _SettledWorkDeliverables,
) -> _DeliverableStep:
    try:
        result, failure = _apply_work_patch(
            turn.service,
            turn.execution,
            patch_text,
            run_truth_scope=turn.context.run_truth_scope,
            patch_kind=turn.request.patch_kind,
            control_node_id=turn.request.control_node_id,
            control_decision_bundle=turn.request.control_decision_bundle,
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
    turn: WorkTurn,
    staged: _StagedWorkInputs,
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
        run_truth_scope=turn.context.run_truth_scope,
        workflow_ids=staged.skill_selection.workflow_ids,
        skill_ids=staged.skill_selection.skill_ids,
        resolved_skill_packages=staged.skill_selection.resolved_skill_packages,
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
        patch_path=turn.patch_inputs.patch_path,
        diagnostics_path=diagnostics_path,
        validator_command=validator_command,
    )


def _read_corrected_patch_deliverable(
    turn: WorkTurn,
    pre_launch_digest: str | None,
    previous: _DeliverableFailure,
) -> _DeliverableRead:
    corrected = _read_correction_patch(
        turn.workspace,
        turn.remote_stage,
        pre_launch_digest=pre_launch_digest,
    )
    if corrected.problem == "unreadable":
        message = f"The corrected patch could not be read: {corrected.detail}"
    elif corrected.problem == "missing":
        message = "The correction completed without writing patch.json."
    elif corrected.problem == "unchanged":
        message = (
            f"{previous.message} The correction left patch.json byte-identical; rewrite it "
            "with the required changes."
        )
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
    turn: WorkTurn,
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
    _record_work_graph_rejection(turn.execution, settled.graph_update)
    return _DeliverableStep()


async def _validate_watch_deliverable(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
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
        experiment_handoff = None
        ordinary_handoff = None
        if turn.request.patch_kind == "experiment_loop":
            experiment_handoff = parse_experiment_watch_json(watch_text)
            settled.loop_watch_text = watch_text
        else:
            ordinary_handoff = parse_watch_json(watch_text)
        if turn.request.patch_kind == "experiment_loop" and experiment_handoff.is_empty:
            exit_patch_text = _read_chat_patch(turn.workspace, turn.remote_stage)
            if (
                not turn.request.control_node_id
                or experiment_loop_semantic_ending(
                    exit_patch_text,
                    turn.request.control_node_id,
                )
                is None
            ):
                completion_problem = (
                    experiment_exit_problem(
                        exit_patch_text,
                        turn.request.control_node_id,
                    )
                    if turn.request.control_node_id
                    else None
                )
                raise ValueError(
                    completion_problem
                    or "An Experiment-loop watch.json with both lists empty requires "
                    "patch.json to explicitly record success, a Proposal, or a "
                    "same-Patch Blocker."
                )
        specs = (
            experiment_handoff.observers
            if experiment_handoff is not None
            else ordinary_handoff.external
        )
        graph_conditions = (
            experiment_handoff.graph_conditions
            if experiment_handoff is not None
            else ordinary_handoff.graph
        )
        stop_requests = experiment_handoff.stops if experiment_handoff is not None else []
        binding = WatcherBinding(
            project_id=origin_task.project_id,
            origin_operation_id=(
                root_experiment_loop_operation_id(turn.execution)
                if turn.request.patch_kind == "experiment_loop"
                else turn.execution.operation_id
            ),
            origin_task_kind=turn.surface,
            chat_id=turn.request.chat_id or "",
            node_id=turn.request.node_id,
            episode_id=(
                turn.request.control_episode_id
                if turn.request.patch_kind == "experiment_loop"
                else None
            ),
            graph_target=origin_task.graph_target,
            execution_host=turn.execution_host,
            continuation=_watcher_continuation(turn, staged),
        )
        if stop_requests:
            turn.execution.store.validate_experiment_agent_watcher_stops(
                binding,
                stop_requests,
            )
        if turn.request.patch_kind == "experiment_loop":
            graph_armed_revision = None
            if graph_conditions:
                graph_state = await asyncio.to_thread(turn.service.history.state)
                await asyncio.to_thread(
                    validate_graph_conditions,
                    graph_conditions,
                    graph_state,
                )
                graph_armed_revision = graph_state.revision
            check_results = (
                await asyncio.to_thread(
                    validate_watch_specs,
                    specs,
                    turn.execution_host,
                )
                if specs
                else []
            )
            settled.pending_loop_handoff = (
                specs,
                check_results,
                graph_conditions,
                graph_armed_revision,
                binding,
                stop_requests,
            )
            armed = []
        else:
            graph_state = (
                await asyncio.to_thread(turn.service.history.state) if graph_conditions else None
            )
            if graph_conditions:
                turn.execution.armed_graph_watchers = True
            armed = await asyncio.to_thread(
                arm_watchers,
                turn.execution.store,
                specs,
                binding,
                graph_conditions=graph_conditions,
                state=graph_state,
            )
    except WatcherInitialCheckError as exc:
        return _DeliverableStep(failure=_DeliverableFailure(str(exc), correctable=True))
    except ValueError as exc:
        return _DeliverableStep(failure=_DeliverableFailure(str(exc), correctable=True))
    except (OSError, ReplayHalted, StateUnavailable) as exc:
        return _DeliverableStep(failure=_DeliverableFailure(str(exc), correctable=False))

    settled.loop_watch_empty = (
        turn.request.patch_kind == "experiment_loop"
        and not specs
        and not graph_conditions
        and (
            not stop_requests
            or not turn.execution.store.experiment_handoff_has_live_watcher_after_stops(
                binding,
                [item.stop_watcher_id for item in stop_requests],
            )
        )
    )
    if turn.request.patch_kind != "experiment_loop":
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
    validator_command: str,
) -> str:
    if turn.request.patch_kind == "experiment_loop":
        return experiment_loop_watcher_correction_contract(
            original_contract_path=composed.base_contract_path,
            diagnostics_path=diagnostics_path,
            watch_path=turn.patch_inputs.watch_path,
            patch_path=turn.patch_inputs.patch_path,
            output_schema_path=turn.patch_inputs.schema_path,
            validator_command=validator_command,
        )
    return PromptFactory.continuation_task_contract(
        original_contract_path=composed.base_contract_path,
        mode="watch_correction",
        diagnostics_path=diagnostics_path,
        watch_path=turn.patch_inputs.watch_path,
    )


def _reject_watch_deliverable(
    turn: WorkTurn,
    settled: _SettledWorkDeliverables,
    failure: _DeliverableFailure,
    correction_rounds: int,
) -> _DeliverableStep:
    if turn.execution is not None:
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
    if turn.request.patch_kind != "experiment_loop":
        return _DeliverableStep()
    return _DeliverableStep(
        frames=(
            _sse(
                AgentEvent(
                    event="error",
                    text=f"Experiment-loop watcher handoff failed: {failure.message}",
                )
            ),
        ),
        stop=True,
    )


async def _settle_patch_deliverable(
    turn: WorkTurn,
    launcher: AgentLauncher,
    staged: _StagedWorkInputs,
    composed: _ComposedWorkPrompt,
    predecessor_digest: str | None,
    settled: _SettledWorkDeliverables,
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
                staged,
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
            or correction_rounds >= PATCH_CORRECTION_MAX_ROUNDS
            or not settled.native_session_id
        ):
            step = _reject_patch_deliverable(turn, settled, failure, correction_rounds)
            for frame in step.frames:
                yield frame
            if step.stop:
                settled.stop = True
            return

        correction_rounds += 1
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
            turn.local_stage,
            turn.remote_stage,
            f"task-{staged.token}-work-correction-{correction_rounds}.json",
            {"kind": "work", "problem": failure.message},
        )
        correction_validator: StagedCommandMailbox | None = None
        correction_lifecycle: _WorkValidatorMailboxLifecycle | None = None
        try:
            correction_validator = stage_patch_validation_mailbox(
                local_stage=turn.local_stage,
                remote_stage=turn.remote_stage,
                task_id=(
                    turn.execution.operation_id if turn.execution is not None else staged.token
                ),
                turn_id=f"{staged.token}:work-patch-correction:{correction_rounds}",
                timeout_seconds=PATCH_SELF_CHECK_TIMEOUT_SECONDS,
            )
            correction_lifecycle = _start_work_validator_mailbox(
                turn.service,
                correction_validator,
                execution=turn.execution,
                budget=turn.validator_budget,
                run_truth_scope=turn.context.run_truth_scope,
                patch_kind=turn.request.patch_kind,
                control_node_id=turn.request.control_node_id,
                control_decision_bundle=turn.request.control_decision_bundle,
            )
            validator_command = correction_validator.client_command(
                "validate",
                turn.patch_inputs.patch_path,
            )
            correction_contract = _patch_correction_contract(
                turn,
                composed,
                diagnostics_path,
                validator_command,
            )
            correction_path, correction_prompt = _stage_task_contract(
                turn.local_stage,
                turn.remote_stage,
                f"task-{staged.token}-work-correction-{correction_rounds}.md",
                correction_contract,
                execution=turn.execution,
                role=f"work_patch_correction_{correction_rounds}",
            )
            pre_launch_digest = _existing_patch_digest(turn.workspace, turn.remote_stage)
            _record_agent_launch_receipt(
                turn.execution,
                turn.request,
                prompt=correction_prompt,
                contract_path=correction_path,
                remote=bool(turn.execution_host),
                resumed=True,
                write_scope=turn.write_scope,
                continuation="graph_correction",
                extra={
                    "surface": turn.surface,
                    "mode": "work",
                    "capability": "work_auto",
                    "network_access": True,
                    "launch_kind": "graph_correction",
                    "correction_round": correction_rounds,
                    "write_directory_count": len(turn.write_dirs),
                    "canonical_state_boundary": "prompt_only",
                },
            )
            correction_outcome = _ProviderOutcome(session_id=settled.native_session_id)
            correction_error: str | None = None
            correction_stream = _stream_turn_agent_events(
                turn,
                launcher,
                correction_prompt,
                session_id=settled.native_session_id,
                outcome=correction_outcome,
                validator_staged=correction_validator,
                validator_lifecycle=correction_lifecycle,
            )
        except BaseException as exc:
            if correction_lifecycle is not None:
                await correction_lifecycle.close(primary_error=exc)
            elif correction_validator is not None and not correction_validator.credential.expired:
                await _close_work_validator_mailbox(
                    correction_validator,
                    stop=None,
                    task=None,
                    execution=turn.execution,
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
        if correction_outcome.paused:
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
            turn,
            pre_launch_digest,
            failure,
        )
        text = corrected.text
        failure = corrected.failure


async def _settle_watch_deliverable(
    turn: WorkTurn,
    launcher: AgentLauncher,
    staged: _StagedWorkInputs,
    composed: _ComposedWorkPrompt,
    predecessor_digest: str | None,
    settled: _SettledWorkDeliverables,
) -> AsyncIterator[str]:
    initial = _read_initial_watch_deliverable(turn, predecessor_digest)
    text = initial.text
    failure = initial.failure
    settled.loop_watch_text = text if turn.request.patch_kind == "experiment_loop" else None
    if text is None and failure is None:
        return

    maximum_corrections = (
        1 if turn.request.patch_kind == "experiment_loop" else PATCH_CORRECTION_MAX_ROUNDS
    )
    correction_rounds = 0
    while True:
        if text is not None:
            step = await _validate_watch_deliverable(
                turn,
                staged,
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
            step = _reject_watch_deliverable(turn, settled, failure, correction_rounds)
            for frame in step.frames:
                yield frame
            if step.stop:
                settled.stop = True
            return

        correction_rounds += 1
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
        diagnostics_path = _stage_json_task_input(
            turn.local_stage,
            turn.remote_stage,
            f"task-{staged.token}-watch-correction-{correction_rounds}.json",
            {"problem": failure.message},
        )
        correction_validator: StagedCommandMailbox | None = None
        correction_lifecycle: _WorkValidatorMailboxLifecycle | None = None
        try:
            validator_command = turn.patch_inputs.validator_command
            if turn.request.patch_kind == "experiment_loop":
                correction_validator = stage_patch_validation_mailbox(
                    local_stage=turn.local_stage,
                    remote_stage=turn.remote_stage,
                    task_id=turn.execution.operation_id,
                    turn_id=f"{staged.token}:watch-correction:{correction_rounds}",
                    timeout_seconds=PATCH_SELF_CHECK_TIMEOUT_SECONDS,
                )
                correction_lifecycle = _start_work_validator_mailbox(
                    turn.service,
                    correction_validator,
                    execution=turn.execution,
                    budget=turn.validator_budget,
                    run_truth_scope=turn.context.run_truth_scope,
                    patch_kind=turn.request.patch_kind,
                    control_node_id=turn.request.control_node_id,
                    control_decision_bundle=turn.request.control_decision_bundle,
                )
                validator_command = correction_validator.client_command(
                    "validate",
                    turn.patch_inputs.patch_path,
                )
            correction_contract = _watch_correction_contract(
                turn,
                composed,
                diagnostics_path,
                validator_command,
            )
            correction_path, correction_prompt = _stage_task_contract(
                turn.local_stage,
                turn.remote_stage,
                f"task-{staged.token}-watch-correction-{correction_rounds}.md",
                correction_contract,
                execution=turn.execution,
                role=f"watch_correction_{correction_rounds}",
            )
            _record_agent_launch_receipt(
                turn.execution,
                turn.request,
                prompt=correction_prompt,
                contract_path=correction_path,
                remote=bool(turn.execution_host),
                resumed=True,
                write_scope=turn.write_scope,
                continuation="watch_correction",
                extra={
                    "surface": turn.surface,
                    "mode": "work",
                    "capability": "work_auto",
                    "network_access": True,
                    "launch_kind": "watch_correction",
                    "correction_round": correction_rounds,
                    "write_directory_count": len(turn.write_dirs),
                    "canonical_state_boundary": "prompt_only",
                },
            )
            correction_outcome = _ProviderOutcome(session_id=settled.native_session_id)
            correction_error: str | None = None
            correction_stream = (
                _stream_turn_agent_events(
                    turn,
                    launcher,
                    correction_prompt,
                    session_id=settled.native_session_id,
                    outcome=correction_outcome,
                    validator_staged=correction_validator,
                    validator_lifecycle=correction_lifecycle,
                )
                if correction_validator is not None
                else _stream_agent_events(
                    launcher,
                    turn.request,
                    correction_prompt,
                    workspace=turn.workspace,
                    session_id=settled.native_session_id,
                    read_dirs=turn.read_dirs,
                    write_dirs=turn.write_dirs,
                    write_scope=turn.write_scope,
                    execution_host=turn.execution_host,
                    execution=turn.execution,
                    remote_stage=turn.remote_stage,
                    capability="work_auto",
                    outcome=correction_outcome,
                    binary=turn.provider_binary,
                )
            )
        except BaseException as exc:
            if correction_lifecycle is not None:
                await correction_lifecycle.close(primary_error=exc)
            elif correction_validator is not None and not correction_validator.credential.expired:
                await _close_work_validator_mailbox(
                    correction_validator,
                    stop=None,
                    task=None,
                    execution=turn.execution,
                    primary_error=exc,
                )
            raise
        async with aclosing(correction_stream) as stream:
            async for frame in stream:
                event = AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
                if event.event == "error":
                    correction_error = event.text or "Watcher correction failed."
                    continue
                if event.event not in {"answer", "done"}:
                    yield frame
        settled.native_session_id = correction_outcome.session_id or settled.native_session_id
        if correction_outcome.paused:
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
        corrected = _read_corrected_watch_deliverable(turn)
        text = corrected.text
        failure = corrected.failure
        if turn.request.patch_kind == "experiment_loop":
            settled.loop_watch_text = text


async def _apply_work_turn(
    turn: WorkTurn,
    launcher: AgentLauncher,
    staged: _StagedWorkInputs,
    composed: _ComposedWorkPrompt,
    retry_baseline: _RetryDeliverableBaseline,
    applied: _AppliedWorkTurn,
) -> AsyncIterator[str]:
    maintenance_resources = (
        [] if turn.auto_research_child is not None else staged.experiment_resources
    )
    (
        maintenance_frames,
        native_session_id,
        maintenance_paused,
    ) = await _process_experiment_watcher_maintenance(
        service=turn.service,
        launcher=launcher,
        request=turn.request,
        execution=turn.execution,
        staged_resources=maintenance_resources,
        workspace=turn.workspace,
        remote_stage=turn.remote_stage,
        local_stage=turn.local_stage,
        base_contract_path=composed.base_contract_path,
        token=staged.token,
        native_session_id=applied.native_session_id,
        read_dirs=turn.read_dirs,
        write_dirs=turn.write_dirs,
        write_scope=turn.write_scope,
        execution_host=turn.execution_host,
        provider_binary=turn.provider_binary,
        retry_output_digests=retry_baseline.experiment_watch_digests,
    )
    applied.native_session_id = native_session_id
    for frame in maintenance_frames:
        yield frame
    if maintenance_paused:
        applied.stop = True


async def _apply_experiment_loop_turn(
    turn: WorkTurn,
    launcher: AgentLauncher,
    staged: _StagedWorkInputs,
    composed: _ComposedWorkPrompt,
    prompt_context: _WorkPromptContext,
    settled: _SettledWorkDeliverables,
    applied: _AppliedWorkTurn,
) -> AsyncIterator[str]:
    try:
        final_patch_text = _read_chat_patch(turn.workspace, turn.remote_stage)
    except (OSError, StateUnavailable, ValueError) as exc:
        yield _sse(
            AgentEvent(
                event="error",
                text=f"The final Experiment-loop patch could not be read: {exc}",
            )
        )
        applied.stop = True
        return
    if final_patch_text is None:
        applied.graph_update = GraphUpdateResult(status="none")
    else:
        loop_patch_correction_rounds = 0
        while True:
            # A correction round that produced nothing new leaves this None so
            # its own diagnostic reaches the agent instead of being overwritten.
            if final_patch_text is not None:
                if settled.loop_watch_empty and (
                    not turn.request.control_node_id
                    or experiment_loop_semantic_ending(
                        final_patch_text,
                        turn.request.control_node_id,
                    )
                    is None
                ):
                    final_failure = _DeliverableFailure(
                        "A watch.json with both lists empty requires this Patch to retain an "
                        "explicit success, Proposal, or same-Patch Blocker.",
                        correctable=True,
                    )
                    final_result = None
                else:
                    try:
                        final_result, final_failure = _apply_work_patch(
                            turn.service,
                            turn.execution,
                            final_patch_text,
                            run_truth_scope=turn.context.run_truth_scope,
                            patch_kind=turn.request.patch_kind,
                            control_node_id=turn.request.control_node_id,
                            control_decision_bundle=(turn.request.control_decision_bundle),
                        )
                    except RunLockCancelled:
                        yield _sse(
                            AgentEvent(
                                event="paused",
                                text=(
                                    "Paused while waiting for canonical state. The operational "
                                    "answer and retained patch are preserved."
                                ),
                            )
                        )
                        applied.stop = True
                        return
                if final_result is not None:
                    applied.graph_update = final_result.model_copy(
                        update={"correction_rounds": loop_patch_correction_rounds}
                    )
                    break
            assert final_failure is not None
            if (
                not final_failure.correctable
                or loop_patch_correction_rounds >= PATCH_CORRECTION_MAX_ROUNDS
                or not applied.native_session_id
            ):
                if settled.loop_watch_empty:
                    yield _sse(
                        AgentEvent(
                            event="error",
                            text=(
                                "Experiment-loop Patch could not be validated after its watcher "
                                f"handoff: {final_failure.message}"
                            ),
                        )
                    )
                    applied.stop = True
                    return
                repairable = _work_graph_repairable(
                    turn.execution,
                    applied.native_session_id,
                    final_failure,
                )
                applied.graph_update = GraphUpdateResult(
                    status="rejected",
                    change_summary=list(final_failure.change_summary),
                    proposal_ids=list(final_failure.proposal_ids),
                    validation_messages=_bounded_graph_messages(final_failure.message),
                    correction_rounds=loop_patch_correction_rounds,
                    repairable=repairable,
                )
                _record_work_graph_rejection(turn.execution, applied.graph_update)
                break

            loop_patch_correction_rounds += 1
            assert turn.execution is not None
            turn.execution.store.record_agent_task_receipt(
                turn.execution.operation_id,
                "patch_correction_requested",
                {
                    "round": loop_patch_correction_rounds,
                    "problem": final_failure.message[:400],
                },
                tier="diagnostic",
            )
            diagnostics_path = _stage_json_task_input(
                turn.local_stage,
                turn.remote_stage,
                f"task-{staged.token}-loop-patch-correction-{loop_patch_correction_rounds}.json",
                {"kind": "experiment_loop", "problem": final_failure.message},
            )
            loop_validator = stage_patch_validation_mailbox(
                local_stage=turn.local_stage,
                remote_stage=turn.remote_stage,
                task_id=turn.execution.operation_id,
                turn_id=(f"{staged.token}:loop-patch-correction:{loop_patch_correction_rounds}"),
                timeout_seconds=PATCH_SELF_CHECK_TIMEOUT_SECONDS,
            )
            loop_validator_lifecycle = _start_work_validator_mailbox(
                turn.service,
                loop_validator,
                execution=turn.execution,
                budget=turn.validator_budget,
                run_truth_scope=turn.context.run_truth_scope,
                patch_kind=turn.request.patch_kind,
                control_node_id=turn.request.control_node_id,
                control_decision_bundle=turn.request.control_decision_bundle,
            )
            try:
                loop_validator_command = loop_validator.client_command(
                    "validate",
                    turn.patch_inputs.patch_path,
                )
                correction_contract = experiment_loop_patch_correction_contract(
                    original_contract_path=composed.base_contract_path,
                    diagnostics_path=diagnostics_path,
                    patch_path=turn.patch_inputs.patch_path,
                    watch_path=turn.patch_inputs.watch_path,
                    validator_command=loop_validator_command,
                )
                correction_path, correction_prompt = _stage_task_contract(
                    turn.local_stage,
                    turn.remote_stage,
                    f"task-{staged.token}-loop-patch-correction-{loop_patch_correction_rounds}.md",
                    correction_contract,
                    execution=turn.execution,
                    role=(f"experiment_loop_patch_correction_{loop_patch_correction_rounds}"),
                )
                pre_launch_digest = _existing_patch_digest(
                    turn.workspace,
                    turn.remote_stage,
                )
                _record_agent_launch_receipt(
                    turn.execution,
                    turn.request,
                    prompt=correction_prompt,
                    contract_path=correction_path,
                    remote=bool(turn.execution_host),
                    resumed=True,
                    write_scope=turn.write_scope,
                    continuation="graph_correction",
                    extra={
                        "surface": turn.surface,
                        "mode": "work",
                        "capability": "work_auto",
                        "network_access": True,
                        "launch_kind": "graph_correction",
                        "correction_round": loop_patch_correction_rounds,
                        "write_directory_count": len(turn.write_dirs),
                        "canonical_state_boundary": "prompt_only",
                    },
                )
                correction_outcome = _ProviderOutcome(session_id=applied.native_session_id)
            except BaseException as exc:
                await loop_validator_lifecycle.close(primary_error=exc)
                raise
            async with aclosing(
                _stream_turn_agent_events(
                    turn,
                    launcher,
                    correction_prompt,
                    session_id=applied.native_session_id,
                    outcome=correction_outcome,
                    validator_staged=loop_validator,
                    validator_lifecycle=loop_validator_lifecycle,
                )
            ) as stream:
                async for frame in stream:
                    yield frame
            applied.native_session_id = correction_outcome.session_id or applied.native_session_id
            if correction_outcome.paused:
                applied.stop = True
                return
            if not correction_outcome.completed:
                final_failure = _DeliverableFailure(
                    f"{turn.request.provider} produced no Patch correction result.",
                    correctable=True,
                )
                final_patch_text = None
                loop_patch_correction_rounds = PATCH_CORRECTION_MAX_ROUNDS
                continue
            corrected = _read_correction_patch(
                turn.workspace,
                turn.remote_stage,
                pre_launch_digest=pre_launch_digest,
            )
            if corrected.problem == "unreadable":
                final_failure = _DeliverableFailure(
                    f"The corrected loop Patch could not be read: {corrected.detail}",
                    correctable=True,
                )
                final_patch_text = None
                continue
            if corrected.problem in {"missing", "unchanged"}:
                final_failure = _DeliverableFailure(
                    "The loop Patch correction did not rewrite patch.json.",
                    correctable=True,
                )
                final_patch_text = None
                continue
            assert corrected.text is not None
            final_patch_text = corrected.text

    if (
        turn.execution is None
        or prompt_context.episode_context_baseline is None
        or prompt_context.experiment_control_snapshot is None
    ):
        raise ValueError("Experiment-loop handoff lost its durable episode context.")
    execution = turn.execution
    if settled.pending_loop_handoff is not None:
        (
            specs,
            check_results,
            graph_conditions,
            graph_armed_revision,
            binding,
            stop_requests,
        ) = settled.pending_loop_handoff
    else:
        origin_task = execution.store.agent_task(execution.operation_id)
        if origin_task is None:
            raise ValueError("The originating Experiment-loop operation is no longer available.")
        specs = []
        check_results = []
        graph_conditions = []
        graph_armed_revision = None
        stop_requests = []
        binding = WatcherBinding(
            project_id=origin_task.project_id,
            origin_operation_id=root_experiment_loop_operation_id(execution),
            origin_task_kind=turn.surface,
            chat_id=turn.request.chat_id or "",
            node_id=turn.request.node_id,
            episode_id=turn.request.control_episode_id,
            graph_target=origin_task.graph_target,
            execution_host=turn.execution_host,
            continuation=_watcher_continuation(turn, staged),
        )

    try:
        graph_state = (
            await asyncio.to_thread(turn.service.history.state) if graph_conditions else None
        )
        prepared = await asyncio.to_thread(
            prepare_experiment_watcher_records,
            execution,
            specs,
            check_results,
            binding,
            graph_conditions=graph_conditions,
            graph_state=graph_state,
            armed_revision=graph_armed_revision,
        )
        prepared_watcher_ids = [item.watcher_id for item in prepared]
        prepared_stopped_watcher_ids = [item.stop_watcher_id for item in stop_requests]
    except (OSError, ReplayHalted, StateUnavailable, ValueError) as exc:
        yield _sse(
            AgentEvent(
                event="error",
                text=f"Experiment-loop watcher handoff preparation failed: {exc}",
            )
        )
        applied.stop = True
        return

    accepted_loop_watcher_ids = prepared_watcher_ids
    accepted_loop_stopped_watcher_ids = prepared_stopped_watcher_ids
    ending_signal = None
    if applied.graph_update.status == "applied" and turn.request.control_node_id:
        semantic_ending = experiment_loop_semantic_ending(
            final_patch_text,
            turn.request.control_node_id,
        )
        if semantic_ending is not None:
            if (
                final_patch_text is None
                or not turn.request.control_episode_id
                or turn.request.control_invocation is None
                or turn.request.control_invocation_ceiling is None
            ):
                raise ValueError("Experiment-loop ending lost its compact episode receipt inputs.")
            ending_signal = experiment_loop_ending_signal(
                semantic_ending=semantic_ending,
                episode_id=turn.request.control_episode_id,
                control_node_id=turn.request.control_node_id,
                invocation=turn.request.control_invocation,
                invocation_ceiling=turn.request.control_invocation_ceiling,
                control_snapshot=prompt_context.experiment_control_snapshot,
                patch_text=final_patch_text,
                graph_update=applied.graph_update,
                watcher_ids=accepted_loop_watcher_ids,
                stopped_watcher_ids=accepted_loop_stopped_watcher_ids,
                decision_bundle=turn.request.control_decision_bundle,
            )
    patch_digest = (
        hashlib.sha256(final_patch_text.encode("utf-8")).hexdigest()
        if final_patch_text is not None
        else None
    )
    watch_text = settled.loop_watch_text
    watch_digest = (
        hashlib.sha256(watch_text.encode("utf-8")).hexdigest() if watch_text is not None else None
    )
    root_id = root_experiment_loop_operation_id(execution)
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "experiment_loop_handoff_prepared",
        {
            "episode_id": turn.request.control_episode_id,
            "invocation": turn.request.control_invocation,
            "root_operation_id": root_id,
            "patch_sha256": patch_digest,
            "watch_sha256": watch_digest,
            "graph_status": applied.graph_update.status,
            "applied_revision": applied.graph_update.applied_revision,
            "watcher_ids": prepared_watcher_ids,
            "requested_stop_ids": [item.stop_watcher_id for item in stop_requests],
        },
    )
    try:
        armed = await asyncio.to_thread(
            commit_experiment_episode_handoff,
            execution,
            turn.request,
            prepared,
            binding,
            native_session_id=applied.native_session_id,
            execution_host=turn.execution_host,
            stage_host=execution.stage_host,
            stage_root=execution.stage_root,
            graph_result=experiment_graph_result_summary(applied.graph_update),
            context_baseline=prompt_context.episode_context_baseline,
            stops=stop_requests,
            ending_signal=ending_signal,
        )
    except (OSError, ReplayHalted, StateUnavailable, ValueError) as exc:
        yield _sse(
            AgentEvent(
                event="error",
                text=f"Experiment-loop watcher handoff failed: {exc}",
            )
        )
        applied.stop = True
        return
    if graph_conditions:
        execution.armed_graph_watchers = True
    execution.store.record_agent_task_receipt(
        root_id,
        "watchers_armed",
        {
            "watcher_ids": [item.watcher_id for item in armed],
            "stopped_watcher_ids": [item.stop_watcher_id for item in stop_requests],
            "count": len(armed),
            "correction_rounds": settled.watch_correction_rounds,
        },
    )


def _finalize_work_turn(
    turn: WorkTurn,
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
    transcript_request = turn.request
    if (
        turn.message_waking
        and turn.auto_research_child is not None
        and transcript_request.message is None
    ):
        transcript_request = transcript_request.model_copy(
            update={
                "message": (
                    "RCP delivered a claimed Auto-research mail batch in the separate "
                    "messages.json handoff."
                )
            }
        )
    try:
        _append_chat_exchange(
            turn.service,
            transcript_request,
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
    launcher: AgentLauncher,
    prompt: str,
    contract_path: str,
    staged: _StagedWorkInputs,
    wake_episode: EpisodeRecord | None,
) -> AsyncIterator[str]:
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
                    if turn.message_waking
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
                    outcome=turn.outcome,
                )
            ) as stream:
                async for frame in stream:
                    yield frame
        except Exception:
            turn.outcome.failed = True
            raise

        answer = "\n\n".join(item.strip() for item in turn.outcome.answers if item.strip()).strip()
        if not turn.outcome.completed:
            if turn.outcome.failed or turn.outcome.paused:
                return
            turn.outcome.failed = True
            yield _sse(
                AgentEvent(event="error", text=f"{turn.request.provider} produced no result.")
            )
            return
        if not answer:
            yield _sse(
                AgentEvent(
                    event="error",
                    text=f"{turn.request.provider} finished without answering.",
                )
            )
            return
        if turn.waking and (
            wake_episode is None or turn.outcome.session_id != wake_episode.native_session_id
        ):
            yield _sse(
                AgentEvent(
                    event="error",
                    text=(
                        "The automatic Experiment wake did not continue its committed native "
                        "provider session. The watcher handoff was not accepted."
                    ),
                )
            )
            return

        if turn.uses_master_protocol:
            _commit_chat_prompt_state(turn.execution, turn.request, turn.outcome.session_id)

        try:
            artifacts = _discover_chat_artifacts(
                turn.execution,
                staged.artifact_scope_id,
                Path(str(staged.artifact_directory)),
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
        _finalize_result_view_turn(
            turn.request,
            turn.execution,
            staged.prepared_result_view,
            turn.local_stage,
            turn.remote_stage,
            native_session_id=turn.outcome.session_id,
        )
    except BaseException as exc:
        if turn.execution is not None and staged.prepared_result_view is not None:
            _record_result_view_rejection(turn.execution, staged.prepared_result_view, str(exc))
        raise
    turn.answer = answer
    yield _sse(AgentEvent(event="answer", text=answer))
    for artifact in artifacts:
        yield _sse(AgentEvent(event="artifact", artifact=artifact))


async def stream_work_run(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    execution: AgentTaskExecution | None = None,
) -> AsyncIterator[str]:
    """Run one operational conversation turn with optional graph reflection."""

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
        outcome = turn.outcome
        resuming = turn.resuming
        # An Experiment-loop watcher wake resumes the episode's native session, but it
        # is a new turn at the next invocation -- never task Resume, never a retry, and
        # never a rebuilt master contract.
        waking = turn.waking
        prompt_context = await _prepare_work_prompt_context(turn, staged)
        wake_episode = prompt_context.wake_episode
        if resuming:
            composed_prompt = _compose_resume_prompt(turn, staged, prompt_context)
        elif turn.message_waking and turn.auto_research_child is not None:
            composed_prompt = _compose_auto_research_child_message_wake_prompt(turn, staged)
        elif waking:
            composed_prompt = _compose_wake_prompt(turn, staged, prompt_context)
        else:
            result_view_handoff = bool(
                turn.continuation == "handoff" and staged.prepared_result_view is not None
            )
            if turn.retrying or result_view_handoff:
                composed_prompt = _compose_retry_prompt(turn, staged, prompt_context)
            else:
                retry_diagnostics_path = _stage_retry_diagnostics(turn, staged)
                composed_prompt = _compose_fresh_prompt(
                    turn,
                    staged,
                    prompt_context,
                    retry_diagnostics_path=retry_diagnostics_path,
                )
        contract_path = composed_prompt.contract_path
        prompt = composed_prompt.prompt
        retry_baseline = _capture_retry_deliverable_baseline(turn)
        retry_patch_digest = retry_baseline.patch_digest
        retry_watch_digest = retry_baseline.watch_digest
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
    async with aclosing(
        _launch_and_stream_work_turn(
            turn,
            launcher,
            prompt,
            contract_path,
            staged,
            wake_episode,
        )
    ) as stream:
        async for frame in stream:
            yield frame
    if turn.answer is None:
        return
    answer = turn.answer

    settled = _SettledWorkDeliverables(native_session_id=outcome.session_id)
    async with aclosing(
        _settle_patch_deliverable(
            turn,
            launcher,
            staged,
            composed_prompt,
            retry_patch_digest,
            settled,
        )
    ) as stream:
        async for frame in stream:
            yield frame
    if settled.stop:
        return
    if turn.auto_research_child is not None:
        child_mailbox = RunStageMailbox.for_stage(
            local_stage=turn.local_stage,
            remote_stage=turn.remote_stage,
        )
        child_mailbox.remove("watch.json")
        if turn.execution is not None:
            turn.execution.store.record_agent_task_receipt(
                turn.execution.operation_id,
                "auto_research_child_watcher_output_discarded",
                {
                    "watcher_authority": "none",
                    "reason": "ordinary Auto-research child Work cannot arm a watcher",
                },
                tier="diagnostic",
            )
    else:
        async with aclosing(
            _settle_watch_deliverable(
                turn,
                launcher,
                staged,
                composed_prompt,
                retry_watch_digest,
                settled,
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
    if request.patch_kind == "work":
        apply_stream = _apply_work_turn(
            turn,
            launcher,
            staged,
            composed_prompt,
            retry_baseline,
            applied,
        )
    else:
        apply_stream = _apply_experiment_loop_turn(
            turn,
            launcher,
            staged,
            composed_prompt,
            prompt_context,
            settled,
            applied,
        )
    async with aclosing(apply_stream) as stream:
        async for frame in stream:
            yield frame
    if applied.stop:
        return
    graph_update = applied.graph_update

    for frame in _finalize_work_turn(turn, answer, graph_update):
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
            expected_stage = _swept_stage_root(data_dir) / stage_name
            local_stage = _validated_local_chat_resume_stage(execution, expected_stage)
            workspace = local_stage
        token = _task_token(execution)
        patch_inputs = _stage_chat_patch_inputs(
            local_stage,
            remote_stage,
            workspace=workspace,
            stage_name=stage_name,
            task_id=execution.operation_id,
            turn_id=f"{token}:work-graph-repair",
        )
        validator_lifecycle = _start_work_validator_mailbox(
            service,
            patch_inputs.validator_staged,
            execution=execution,
            budget=validator_budget,
            run_truth_scope=context.run_truth_scope,
            patch_kind=request.patch_kind,
            control_node_id=request.control_node_id,
            control_decision_bundle=request.control_decision_bundle,
        )
        patch_path = patch_inputs.patch_path
        read_dirs = _chat_read_dirs(
            context,
            remote_stage,
            service,
            execution_machine.alias,
        )
        write_scope = _project_write_scope(
            context,
            service,
            execution_machine.alias,
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
        pre_launch_digest = _existing_patch_digest(workspace, remote_stage)
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
    if not outcome.completed:
        if outcome.failed or outcome.paused:
            return
        yield _sse(AgentEvent(event="error", text=f"{request.provider} produced no result."))
        return
    try:
        patch_text = _read_chat_patch(workspace, remote_stage)
    except (OSError, StateUnavailable, ValueError) as exc:
        yield _sse(AgentEvent(event="error", text=f"The repaired patch could not be read: {exc}"))
        return
    if patch_text is None:
        yield _sse(AgentEvent(event="error", text="The repair did not write patch.json."))
        return
    if (
        pre_launch_digest is not None
        and hashlib.sha256(patch_text.encode("utf-8")).hexdigest() == pre_launch_digest
    ):
        yield _sse(
            AgentEvent(
                event="error",
                text="The repair left patch.json byte-identical to the rejected patch.",
            )
        )
        return
    try:
        graph_update, failure = _apply_work_patch(
            service,
            execution,
            patch_text,
            run_truth_scope=context.run_truth_scope,
            patch_kind=request.patch_kind,
            control_node_id=request.control_node_id,
            control_decision_bundle=request.control_decision_bundle,
        )
    except RunLockCancelled:
        yield _sse(
            AgentEvent(
                event="paused",
                text="Paused while waiting for canonical state. The retained patch is preserved.",
            )
        )
        return
    if graph_update is None:
        assert failure is not None
        graph_update = GraphUpdateResult(
            status="rejected",
            change_summary=list(failure.change_summary),
            proposal_ids=list(failure.proposal_ids),
            validation_messages=_bounded_graph_messages(failure.message),
            correction_rounds=1,
        )
        _record_work_graph_rejection(execution, graph_update)
    if request.patch_kind == "experiment_loop":
        if (
            not request.control_episode_id
            or not request.control_node_id
            or request.control_invocation is None
            or request.control_invocation_ceiling is None
        ):
            yield _sse(
                AgentEvent(event="error", text="The graph repair lost its Experiment episode.")
            )
            return
        episode = execution.store.experiment_episode(request.control_episode_id)
        if episode is None or not episode.session_bound:
            yield _sse(
                AgentEvent(
                    event="error",
                    text="The graph repair has no bound Experiment episode to update.",
                )
            )
            return
        control_node = context.node
        if (
            not isinstance(control_node, dict)
            or control_node.get("id") != request.control_node_id
            or control_node.get("type") != "experiment"
        ):
            yield _sse(
                AgentEvent(
                    event="error",
                    text="The graph repair lost its Experiment control snapshot.",
                )
            )
            return
        ending_signal = None
        if graph_update.status == "applied":
            semantic_ending = experiment_loop_semantic_ending(
                patch_text,
                request.control_node_id,
            )
            if semantic_ending is not None:
                ending_signal = experiment_loop_ending_signal(
                    semantic_ending=semantic_ending,
                    episode_id=request.control_episode_id,
                    control_node_id=request.control_node_id,
                    invocation=request.control_invocation,
                    invocation_ceiling=request.control_invocation_ceiling,
                    control_snapshot=dict(control_node),
                    patch_text=patch_text,
                    graph_update=graph_update,
                    watcher_ids=episode.last_watcher_ids,
                    stopped_watcher_ids=[],
                    decision_bundle=request.control_decision_bundle,
                )
        try:
            commit_experiment_episode_binding(
                execution,
                request,
                native_session_id=outcome.session_id,
                execution_host=execution_host,
                stage_host=episode.stage_host,
                stage_root=episode.stage_root,
                graph_result=experiment_graph_result_summary(graph_update),
                watcher_ids=episode.last_watcher_ids,
                context_baseline=episode.context_baseline,
                ending_signal=ending_signal,
            )
        except ValueError as exc:
            yield _sse(
                AgentEvent(
                    event="error",
                    text=f"The graph repair could not update its Experiment handoff: {exc}",
                )
            )
            return
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
    service: ProjectService,
    *,
    staged: StagedCommandMailbox,
    execution: AgentTaskExecution,
    route: AutoResearchChildWorkRecord,
    stop: asyncio.Event,
    budget: PatchValidationBudget,
    run_truth_scope: list[str],
    patch_kind: Literal["work", "experiment_loop"],
    control_node_id: str | None,
    control_decision_bundle: list[ExperimentDecisionPin],
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
            budget.count += 1
            if budget.count > PATCH_SELF_CHECK_MAX_COUNT:
                result = PatchValidationResult(
                    status="unavailable",
                    messages=["This task has reached its bounded RCP validator self-check limit."],
                )
            else:
                result = await asyncio.to_thread(
                    _validate_work_patch_live,
                    service,
                    request.arguments.patch,
                    run_truth_scope=run_truth_scope,
                    patch_kind=patch_kind,
                    control_node_id=control_node_id,
                    control_decision_bundle=control_decision_bundle,
                    source_operation_id=execution.operation_id,
                )
            execution.store.record_agent_task_event(
                execution.operation_id,
                f"Patch self-check {budget.count}/{PATCH_SELF_CHECK_MAX_COUNT}: {result.status}.",
                level="info" if result.status == "valid" else "warning",
            )
            execution.store.record_agent_task_receipt(
                execution.operation_id,
                "patch_self_check",
                {
                    "count": budget.count,
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
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message=(
                "This child Work credential authorizes only Patch validation and a reply to its "
                "Auto-research orchestrator."
            ),
        )

    try:
        await serve_command_mailbox(
            staged=staged,
            handler=handle,
            stop=stop,
            poll_seconds=PATCH_SELF_CHECK_POLL_SECONDS,
            invocation_gate=staged.invocation_gate,
        )
    except (OSError, StateUnavailable, ValueError) as exc:
        execution.store.record_agent_task_event(
            execution.operation_id,
            f"Child Work command broker became unavailable: {' '.join(str(exc).split())[:400]}",
            level="warning",
        )


def _start_work_validator_mailbox(
    service: ProjectService,
    staged: StagedCommandMailbox,
    *,
    execution: AgentTaskExecution | None,
    budget: PatchValidationBudget,
    run_truth_scope: list[str],
    patch_kind: Literal["work", "experiment_loop"],
    control_node_id: str | None,
    control_decision_bundle: list[ExperimentDecisionPin],
    auto_research_child: AutoResearchChildWorkRecord | None = None,
) -> _WorkValidatorMailboxLifecycle:
    stop = asyncio.Event()
    try:
        if auto_research_child is not None:
            if execution is None:
                raise ValueError("Auto-research child Work command broker requires its task.")
            task = asyncio.create_task(
                _serve_auto_research_child_work_mailbox(
                    service,
                    staged=staged,
                    execution=execution,
                    route=auto_research_child,
                    stop=stop,
                    budget=budget,
                    run_truth_scope=run_truth_scope,
                    patch_kind=patch_kind,
                    control_node_id=control_node_id,
                    control_decision_bundle=control_decision_bundle,
                )
            )
        else:
            task = asyncio.create_task(
                serve_patch_validation_mailbox(
                    staged=staged,
                    execution=execution,
                    validate=lambda text: _validate_work_patch_live(
                        service,
                        text,
                        run_truth_scope=run_truth_scope,
                        patch_kind=patch_kind,
                        control_node_id=control_node_id,
                        control_decision_bundle=control_decision_bundle,
                        source_operation_id=_work_patch_source_operation_id(execution, patch_kind),
                    ),
                    stop=stop,
                    budget=budget,
                )
            )
    except BaseException:
        with suppress(BaseException):
            staged.cleanup()
        raise
    return _WorkValidatorMailboxLifecycle(
        staged=staged,
        execution=execution,
        stop=stop,
        task=task,
    )


async def _wait_for_work_validator_task(
    task: asyncio.Task[None],
) -> tuple[BaseException | None, asyncio.CancelledError | None]:
    """Wait without allowing caller cancellation to abandon an owned mailbox task."""

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


async def _close_work_validator_mailbox(
    staged: StagedCommandMailbox,
    *,
    stop: asyncio.Event | None,
    task: asyncio.Task[None] | None,
    execution: AgentTaskExecution | None,
    primary_error: BaseException | None = None,
) -> None:
    if stop is not None:
        stop.set()

    serve_error: BaseException | None = None
    caller_cancelled: asyncio.CancelledError | None = None
    if task is not None:
        serve_error, caller_cancelled = await _wait_for_work_validator_task(task)

    cleanup_task = asyncio.create_task(asyncio.to_thread(staged.cleanup))
    cleanup_error, cleanup_cancelled = await _wait_for_work_validator_task(cleanup_task)
    if caller_cancelled is None:
        caller_cancelled = cleanup_cancelled

    def warning(message: str) -> None:
        if execution is None:
            return
        with suppress(Exception):
            execution.store.record_agent_task_event(
                execution.operation_id,
                message,
                level="warning",
            )

    expected_errors = (OSError, StateUnavailable, ValueError)
    if primary_error is not None:
        if serve_error is not None and not isinstance(serve_error, asyncio.CancelledError):
            warning(f"Patch validator became unavailable: {serve_error}")
        if cleanup_error is not None and not isinstance(cleanup_error, asyncio.CancelledError):
            warning(f"Patch validator cleanup failed: {cleanup_error}")
        return

    if caller_cancelled is not None:
        if serve_error is not None and not isinstance(serve_error, asyncio.CancelledError):
            warning(f"Patch validator became unavailable: {serve_error}")
        if cleanup_error is not None and not isinstance(cleanup_error, asyncio.CancelledError):
            warning(f"Patch validator cleanup failed: {cleanup_error}")
        raise caller_cancelled

    if serve_error is not None:
        if isinstance(serve_error, expected_errors):
            warning(f"Patch validator became unavailable: {serve_error}")
        else:
            if cleanup_error is not None:
                warning(f"Patch validator cleanup failed: {cleanup_error}")
            raise serve_error
    if cleanup_error is not None:
        if isinstance(cleanup_error, expected_errors):
            warning(f"Patch validator cleanup failed: {cleanup_error}")
        else:
            raise cleanup_error


async def _stream_work_agent_events(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    prompt: str,
    *,
    workspace: Path,
    session_id: str | None,
    read_dirs: list[Path],
    write_dirs: list[Path],
    write_scope: ProjectWriteScope,
    execution_host: str,
    execution: AgentTaskExecution | None,
    remote_stage: RemoteRunStage | None,
    capability: Literal["work_auto"],
    outcome: _ProviderOutcome,
    binary: str | None,
    validator_staged: StagedCommandMailbox,
    validator_lifecycle: _WorkValidatorMailboxLifecycle | None = None,
    validator_budget: PatchValidationBudget,
    run_truth_scope: list[str],
    patch_kind: Literal["work", "experiment_loop"],
    control_node_id: str | None,
    control_decision_bundle: list[ExperimentDecisionPin],
) -> AsyncIterator[str]:
    lifecycle = validator_lifecycle or _start_work_validator_mailbox(
        service,
        validator_staged,
        execution=execution,
        budget=validator_budget,
        run_truth_scope=run_truth_scope,
        patch_kind=patch_kind,
        control_node_id=control_node_id,
        control_decision_bundle=control_decision_bundle,
    )
    primary_error: BaseException | None = None
    try:
        async with aclosing(
            _stream_agent_events(
                launcher,
                request,
                prompt,
                workspace=workspace,
                session_id=session_id,
                read_dirs=read_dirs,
                write_dirs=write_dirs,
                write_scope=write_scope,
                execution_host=execution_host,
                execution=execution,
                remote_stage=remote_stage,
                capability=capability,
                outcome=outcome,
                binary=binary,
                invocation_gate=validator_staged.invocation_gate,
                required_session_id=_required_work_continuation_session_id(
                    request,
                    execution,
                    session_id=session_id,
                ),
            )
        ) as stream:
            async for frame in stream:
                yield frame
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        await lifecycle.close(primary_error=primary_error)


def _required_work_continuation_session_id(
    request: RunRequest,
    execution: AgentTaskExecution | None,
    *,
    session_id: str | None,
) -> str | None:
    """Pin continuations whose operational result is valid only in the saved session."""

    if execution is None or not execution.reuses_native_checkpoint:
        return None
    child_work = execution.store.auto_research_child_work_for_operation(execution.operation_id)
    experiment_continuation = request.patch_kind == "experiment_loop"
    return session_id if child_work is not None or experiment_continuation else None


def _prepare_work_patch_candidate(
    service: ProjectService,
    patch_text: str,
    *,
    run_truth_scope: list[str],
    patch_kind: Literal["work", "experiment_loop"],
    control_node_id: str | None,
    control_decision_bundle: list[ExperimentDecisionPin] | None,
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
        kind=patch_kind,
        run_truth_scope=run_truth_scope,
        source_operation_id=source_operation_id,
        source_effect_id=source_effect_id,
        source_effect_sha256=(
            hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
            if source_effect_id is not None
            else None
        ),
        profile=profile,
    )
    if patch_kind == "experiment_loop":
        patch = patch.model_copy(
            update={
                "experiment_control_node_id": control_node_id,
                "experiment_decision_bundle": list(control_decision_bundle or ()),
            }
        )
        if not control_node_id:
            raise ValueError("Experiment-loop Patch validation requires its focused Experiment.")
        validate_experiment_completion(patch, control_node_id)
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
    patch_kind: Literal["work", "experiment_loop"],
    control_node_id: str | None,
    control_decision_bundle: list[ExperimentDecisionPin] | None,
    source_operation_id: str | None = None,
    source_effect_id: str | None = None,
    profile: AgentProfile = "ordinary",
) -> PatchValidationResult:
    try:
        candidate = _prepare_work_patch_candidate(
            service,
            patch_text,
            run_truth_scope=run_truth_scope,
            patch_kind=patch_kind,
            control_node_id=control_node_id,
            control_decision_bundle=control_decision_bundle,
            source_operation_id=source_operation_id,
            source_effect_id=source_effect_id,
            profile=profile,
        )
        prepared, report, state = service.history.validate_candidate(candidate.patch)
    except (ReplayHalted, StateUnavailable, OSError) as exc:
        return PatchValidationResult(status="unavailable", messages=[str(exc)])
    except ValueError as exc:
        return PatchValidationResult(status="invalid", messages=[str(exc)])
    rejects = [item.message for item in report.messages if item.level == "reject"]
    if rejects:
        return PatchValidationResult(
            status="invalid",
            messages=_bounded_graph_messages(*rejects),
            live_revision=state.revision,
            candidate_revision=prepared.revision,
        )
    return PatchValidationResult(
        status="valid",
        messages=_bounded_graph_messages(*(item.message for item in report.flags)),
        live_revision=state.revision,
        candidate_revision=prepared.revision,
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
    patch_kind: Literal["work", "experiment_loop"] = "work",
    control_node_id: str | None = None,
    control_decision_bundle: list[ExperimentDecisionPin] | None = None,
    profile: AgentProfile = "ordinary",
    source_operation_id: str | None = None,
    source_effect_id: str | None = None,
) -> tuple[GraphUpdateResult | None, _DeliverableFailure | None]:
    """Validate and atomically apply one Work patch candidate."""

    if execution is not None:
        execution.store.record_agent_task_patch_output(execution.operation_id, patch_text)
        execution.store.record_agent_task_receipt(
            execution.operation_id,
            "patch_retained",
            {"byte_length": len(patch_text.encode("utf-8")), "file_name": "patch.json"},
            tier="diagnostic",
        )
    change_summary: tuple[str, ...] = ()
    proposal_ids: tuple[str, ...] = ()
    source_operation_id = source_operation_id or _work_patch_source_operation_id(
        execution, patch_kind
    )
    canonical_patch: Patch | None = None
    try:
        candidate = _prepare_work_patch_candidate(
            service,
            patch_text,
            run_truth_scope=run_truth_scope,
            patch_kind=patch_kind,
            control_node_id=control_node_id,
            control_decision_bundle=control_decision_bundle,
            source_operation_id=source_operation_id,
            source_effect_id=source_effect_id,
            profile=profile,
        )
        patch = candidate.patch
        change_summary = candidate.change_summary
        proposal_ids = candidate.proposal_ids
        _record_patch_receipt(
            execution,
            patch,
            byte_length=len(patch_text.encode("utf-8")),
        )
        if not patch.ops and patch_kind != "experiment_loop":
            return GraphUpdateResult(status="none"), None
        workspace = service.history.workspace
        with workspace.run_lock(
            on_wait=(lambda message: _record_work_lock_wait(execution, message, workspace.location))
            if execution is not None
            else None,
            on_lost=(lambda message: _record_work_lock_lost(execution, message, workspace.location))
            if execution is not None
            else None,
            cancelled=(execution.control.pause_requested.is_set if execution is not None else None),
        ) as lease:
            lease.assert_owned()
            if source_operation_id:
                matches = [
                    item
                    for item in service.history.load_patches()
                    if (
                        item.source_effect_id == source_effect_id
                        if source_effect_id is not None
                        else item.source_operation_id == source_operation_id
                    )
                    and item.admission == "accepted"
                ]
                if len(matches) > 1:
                    raise ValueError("One agent effect has multiple canonical Patch commits.")
                if matches:
                    canonical_patch = matches[0]
                    if (
                        canonical_patch.source_operation_id != source_operation_id
                        or canonical_patch.source_effect_sha256 != patch.source_effect_sha256
                        or canonical_patch.kind != patch_kind
                        or (
                            patch_kind == "experiment_loop"
                            and canonical_patch.experiment_control_node_id != control_node_id
                        )
                    ):
                        raise ValueError(
                            "Work invocation source is bound to a different canonical Patch."
                        )
                    result = service.history.current_materialization()
                    appended = canonical_patch
                elif not patch.ops:
                    return GraphUpdateResult(status="none"), None
                else:
                    appended, result = service.history.append(
                        patch,
                        discard_on_reject=True,
                    )
            else:
                appended, result = service.history.append(
                    patch,
                    discard_on_reject=True,
                )
    except PatchRejected as exc:
        messages = [item.message for item in exc.report.messages if item.level == "reject"]
        detail = "; ".join(messages) or str(exc) or "The graph rejected the Work patch."
        if execution is not None:
            execution.store.record_agent_task_receipt(
                execution.operation_id,
                "patch_rejected",
                {"messages": [item.model_dump(mode="json") for item in exc.report.messages[:16]]},
                tier="diagnostic",
            )
        return None, _DeliverableFailure(
            detail,
            correctable=True,
            change_summary=change_summary,
            proposal_ids=proposal_ids,
        )
    except (ReplayHalted, StateUnavailable) as exc:
        return None, _DeliverableFailure(
            str(exc),
            correctable=False,
            change_summary=change_summary,
            proposal_ids=proposal_ids,
        )
    except ValueError as exc:
        return None, _DeliverableFailure(
            str(exc),
            correctable=True,
            change_summary=change_summary,
            proposal_ids=proposal_ids,
        )

    if canonical_patch is not None:
        change_summary = tuple(canonical_patch.change_summary)
        proposal_ids = tuple(_work_patch_proposal_ids(canonical_patch))
    report = result.reports[appended.revision]
    _record_patch_applied_receipt(execution, result.state)
    return (
        GraphUpdateResult(
            status="applied",
            applied_revision=appended.revision,
            change_summary=list(change_summary),
            proposal_ids=list(proposal_ids),
            validation_messages=_bounded_graph_messages(*(item.message for item in report.flags)),
        ),
        None,
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
