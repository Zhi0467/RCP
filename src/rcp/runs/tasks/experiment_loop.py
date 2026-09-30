from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing, suppress
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict

from rcp.agents import (
    AgentEvent,
    AgentLauncher,
    parse_agent_patch_json,
    prepare_agent_patch,
    validate_agent_patch_shape,
    validate_work_patch,
)
from rcp.agents.command_mailbox import (
    StagedCommandMailbox,
)
from rcp.agents.continuation_prompt import (
    LaunchPhase,
    MasterRef,
    changed_since_master,
    classify,
    compose,
    master_key,
)
from rcp.agents.experiment_loop_prompt import (
    EXPERIMENT_LOOP_POLICY_VERSION,
    experiment_loop_continuation_contract,
    experiment_loop_patch_correction_contract,
    experiment_loop_task_contract,
    experiment_loop_turn_message,
    experiment_loop_wake_message,
    experiment_loop_watcher_correction_contract,
)
from rcp.agents.graph_rules import graph_rules
from rcp.agents.prompts import (
    _invoked_package_section,
    invoked_package_pointers,
    invoked_provider_skill_section,
)
from rcp.attachments import ChatAttachmentStore
from rcp.background import AgentTaskExecution
from rcp.compute_jobs.job_managers import JOB_MANAGERS
from rcp.config import AgentSurface
from rcp.core.authority import AgentProfile
from rcp.core.models import ExperimentDecisionPin
from rcp.history import ReplayHalted
from rcp.limits import (
    EXPERIMENT_LOOP_WATCH_CORRECTION_MAX_ROUNDS,
    PATCH_CORRECTION_MAX_ROUNDS,
    PATCH_SELF_CHECK_TIMEOUT_SECONDS,
)
from rcp.runs.chat import (
    _append_chat_graph_receipt,
    _chat_read_dirs,
    _chat_stage_name,
    _ChatPatchInputs,
    _clear_stale_turn_handoffs,
    _discover_chat_artifacts,
    _logical_chat_turn_operation_id,
    _prepare_local_artifact_directory,
    _prepare_local_chat_workspace,
    _project_write_scope,
    _read_chat_patch,
    _read_watch_request,
    _record_artifact_discovery_receipt,
    _record_chat_context_receipt,
    _stage_chat_patch_inputs,
    _validated_local_chat_resume_stage,
    _validated_remote_chat_resume_stage,
)
from rcp.runs.experiment_admission import experiment_start_message
from rcp.runs.experiment_loop import (
    commit_experiment_episode_binding,
    commit_experiment_episode_handoff,
    experiment_episode_context_values,
    experiment_exit_problem,
    experiment_graph_result_summary,
    experiment_loop_ending_signal,
    experiment_loop_semantic_ending,
    experiment_observer_watcher_id,
    prepare_experiment_episode_context_candidate,
    prepare_experiment_watcher_records,
    root_experiment_loop_operation_id,
    stage_experiment_loop_context,
    validate_experiment_completion,
)
from rcp.runs.patch_validator import (
    PatchValidationBudget,
    PatchValidationResult,
    serve_patch_validation_mailbox,
    stage_patch_validation_mailbox,
)
from rcp.runs.recorded_settlement import (
    absorb_recorded_events,
    provider_turn_request,
    refuse_recorded_session_mismatch,
)
from rcp.runs.recorded_settlement import (
    note_stage_unreachable as _note_stage_unreachable,
)
from rcp.runs.recorded_turn import RecordedProviderTurn, decode_recorded_turn
from rcp.runs.session_master import (
    SESSION_MASTER_KEY_ROLE,
    SESSION_MASTER_ROLE,
    continuation_session_master,
    record_inline_prompt,
    record_session_master,
    recorded_master_values,
    session_master_label,
    stage_session_master,
)
from rcp.runs.shared import (
    _pinned_to_profile,
    _protected_run_stage_roots,
    _ProviderOutcome,
    _record_agent_launch_receipt,
    _retry_deliverable_is_unchanged,
    _sse,
    _stage_context_paths,
    _stage_json_task_input,
    _stage_or_reuse_task_input,
    _stage_task_contract,
    _swept_stage_root,
    _task_token,
    note_link_lost_before_provider,
)
from rcp.runs.tasks.compute_commands import WorkComputeCommands
from rcp.runs.tasks.episode_report import report_rebootstrap_pending
from rcp.runs.tasks.work import (
    _WORK_PRIMARY_ANSWER_ROLE,
    WorkTurn,
    _AppliedWorkTurn,
    _bounded_graph_messages,
    _capture_retry_deliverable_baseline,
    _clears_stale_turn_handoffs,
    _ComposedWorkPrompt,
    _DeliverableFailure,
    _DeliverableRead,
    _DeliverableStep,
    _finalize_work_turn,
    _lead_with_retained_answer,
    _load_work_finalization_context,
    _record_work_finalization_context,
    _record_work_graph_rejection,
    _record_work_lock_lost,
    _record_work_lock_wait,
    _recorded_retry_deliverable_baseline,
    _rejected_graph_update_for_repair,
    _resolve_work_execution,
    _ResolvedWorkExecution,
    _retained_primary_answer,
    _RetryDeliverableBaseline,
    _SettledWorkDeliverables,
    _stage_retry_diagnostics,
    _StagedWorkInputs,
    _watcher_continuation,
    _work_execution_instructions,
    _work_finalization_context,
    _work_graph_repairable,
    _work_patch_proposal_ids,
    _WorkValidatorMailboxLifecycle,
    _write_recorded_patch,
)
from rcp.runs.tasks.work_turn_runtime import (
    WorkFinalizationContext,
    _PreparedWorkPatch,
    apply_work_patch,
    read_correction_patch,
    settle_graph_repair_patch,
    start_work_validator_mailbox,
    validate_work_patch_live,
)
from rcp.runs.tasks.work_turn_runtime import (
    close_work_validator_mailbox as _close_work_validator_mailbox,
)
from rcp.runs.tasks.work_turn_runtime import (
    stream_turn_agent_events as _stream_turn_agent_events,
)
from rcp.service import GraphUpdateResult, ProjectService, RunRequest
from rcp.skills.staging import skill_bundle_label, stage_skill_selection
from rcp.storage import EpisodeRecord
from rcp.transport import RemoteRunStage, RunLockCancelled, StateUnavailable
from rcp.watchers import (
    WatcherBinding,
    WatcherInitialCheckError,
    parse_experiment_watch_json,
    validate_graph_conditions,
    validate_watch_specs,
)

EXPERIMENT_LOOP_FINALIZATION_CONTEXT_ROLE = "experiment_loop_finalization_context"
EXPERIMENT_LOOP_EPISODE_CONTEXT_ROLE = "experiment_loop_episode_context"


class _StoredExperimentLoopEpisodeContext(BaseModel):
    """The episode facts this loop turn will commit, written before it launches.

    A loop turn settles against the episode its launch read, not the episode a
    later reconnect finds. Re-reading these after a disconnect would let a turn
    commit a baseline, or continue a session, belonging to a different pass.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = 1
    episode_context_baseline: dict[str, object] | None
    experiment_control_snapshot: dict[str, object] | None
    wake_native_session_id: str | None


def _record_experiment_loop_episode_context(
    turn: WorkTurn,
    prompt_context: _WorkPromptContext,
) -> None:
    execution = turn.execution
    if execution is None:
        return
    stored = _StoredExperimentLoopEpisodeContext(
        episode_context_baseline=prompt_context.episode_context_baseline,
        experiment_control_snapshot=prompt_context.experiment_control_snapshot,
        wake_native_session_id=(
            prompt_context.wake_episode.native_session_id
            if prompt_context.wake_episode is not None
            else None
        ),
    )
    content = stored.model_dump_json()
    execution.store.record_agent_task_contract(
        execution.operation_id,
        EXPERIMENT_LOOP_EPISODE_CONTEXT_ROLE,
        content,
        hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


def _load_experiment_loop_episode_context(
    execution: AgentTaskExecution,
) -> _StoredExperimentLoopEpisodeContext:
    content = execution.store.agent_task_contract(
        execution.operation_id, EXPERIMENT_LOOP_EPISODE_CONTEXT_ROLE
    )
    if content is None:
        raise ValueError("The Experiment-loop turn has no retained episode context.")
    try:
        return _StoredExperimentLoopEpisodeContext.model_validate_json(content)
    except ValueError as exc:
        raise ValueError("The retained Experiment-loop episode context is invalid.") from exc


@dataclass(frozen=True)
class _WorkPromptContext:
    episode_context_baseline: dict[str, object] | None
    experiment_control_snapshot: dict[str, object] | None
    wake_episode: EpisodeRecord | None
    loop_control_path: str | None
    watcher_state_path: str | None
    provider_switch_recovery: bool
    ontology_sha256: str


@dataclass(frozen=True)
class _ComposedExperimentPrompt(_ComposedWorkPrompt):
    #: The values this launch left the session holding: its master's, updated by the
    #: changes it sent. A correction in the same operation sends only what differs.
    values: dict[str, object] | None = None


@dataclass
class _SettledExperimentDeliverables(_SettledWorkDeliverables):
    loop_watch_empty: bool = False
    loop_watch_text: str | None = None
    pending_loop_handoff: tuple[object, ...] | None = None


def _work_patch_source_operation_id(
    execution: AgentTaskExecution | None,
) -> str | None:
    if execution is None:
        return None
    if execution.continuation != "graph_repair":
        return root_experiment_loop_operation_id(execution)
    return execution.operation_id


def _experiment_loop_retry_handoff_authorized(
    turn: WorkFinalizationContext,
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


async def _stage_work_turn(
    service: ProjectService,
    resolved: _ResolvedWorkExecution,
    data_dir: Path,
    execution: AgentTaskExecution | None,
) -> tuple[WorkTurn, _StagedWorkInputs]:
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
            control_node_id=request.control_node_id,
            control_decision_bundle=request.control_decision_bundle,
        )
        write_dirs = [Path(item) for item in write_scope.repository_roots]
        experiment_resources = []
        experiment_resource_pointers = []
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
            compute_commands=compute_commands,
            outcome=outcome,
        )
        return turn, _StagedWorkInputs(
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
    current_context = experiment_episode_context_values(
        ontology_extensions=turn.context.ontology_extensions,
        ontology=ontology,
        repositories=staged.repositories,
        skill_pointers=staged.skill_pointers,
    )
    episode_context_baseline = prepare_experiment_episode_context_candidate(
        turn.execution, current_context
    )
    wake_episode: EpisodeRecord | None = None
    if turn.waking:
        if not turn.request.control_episode_id or turn.request.control_invocation is None:
            raise ValueError("Experiment-loop wake is missing its episode invocation.")
        wake_episode = turn.execution.store.experiment_episode(turn.request.control_episode_id)
        if wake_episode is None or not wake_episode.session_bound:
            raise ValueError("Experiment-loop wake has no committed episode session to continue.")
        if (
            wake_episode.native_session_id != turn.request.session_id
            or wake_episode.stage_host != turn.execution.stage_host
            or wake_episode.stage_root != turn.execution.stage_root
        ):
            raise ValueError(
                "Experiment-loop wake does not match its committed native session and exact stage."
            )
        if wake_episode.last_turn_invocation != turn.request.control_invocation - 1:
            raise ValueError(
                "Experiment-loop wake does not immediately follow the episode's last "
                "successful turn."
            )
        if not wake_episode.last_graph_result:
            raise ValueError("Experiment-loop wake cannot confirm the preceding graph handoff.")
    if turn.reusing_checkpoint and not turn.request.session_id:
        raise ValueError(
            "The continued Work turn has no native agent session; retry it from a clean attempt "
            "instead."
        )
    return _WorkPromptContext(
        episode_context_baseline=episode_context_baseline,
        experiment_control_snapshot=experiment_control_snapshot,
        wake_episode=wake_episode,
        loop_control_path=loop_control_path,
        watcher_state_path=watcher_state_path,
        provider_switch_recovery=provider_switch_recovery,
        ontology_sha256=current_context["ontology"]["sha256"],
    )


_EXPERIMENT_MASTER_LABEL = "experiment-master"
# The placeholder argv the master's launch-helper instruction names.
_LAUNCH_EXAMPLE_ARGS = (
    "launch",
    "--key",
    "<idempotency-key>",
    "--cwd",
    "<working-directory>",
    "--",
    "<argv...>",
)


def _handoff_values(turn: WorkTurn) -> dict[str, dict[str, object]]:
    """The master's values that a Patch or watcher repair in this session also names."""

    values: dict[str, dict[str, object]] = {
        "paths": {
            "patch": turn.patch_inputs.patch_path,
            "watch": turn.patch_inputs.watch_path,
            "patch_schema": turn.patch_inputs.schema_path,
        },
        "commands": {"validate": turn.patch_inputs.validator_command},
    }
    if turn.write_scope is not None:
        values["write_scope"] = {
            "writable": list(turn.write_scope.writable_roots),
            "denied": list(turn.write_scope.protected_write_paths),
        }
    return values


def _changed_handoff_values(
    master: MasterRef, current: dict[str, dict[str, object]]
) -> dict[str, object] | None:
    """What a repair's handoff values change from its master, among the ones it names.

    A repair stages no loop inputs, so the master's other values are neither
    compared nor reported as removed.
    """

    if master.values is None:
        return changed_since_master(master, dict(current))
    named: dict[str, object] = {}
    for group, group_values in current.items():
        recorded = master.values.get(group)
        if isinstance(recorded, dict):
            named[group] = (
                recorded
                if group == "write_scope"
                else {key: recorded[key] for key in group_values if key in recorded}
            )
    return changed_since_master(replace(master, values=named), dict(current))


def _experiment_values(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
    prepared: _WorkPromptContext,
) -> dict[str, object]:
    """Every value the Experiment master states that can differ per attempt or over time.

    The master is rendered from these same inputs, so a continuation that compares its
    current values with the master's sends exactly what changed.
    """

    values = _handoff_values(turn)
    values["paths"].update(
        graph=turn.context.graph_path,
        research=turn.context.research_md_path,
        loop_control=prepared.loop_control_path,
        watcher_state=prepared.watcher_state_path,
        artifacts=str(staged.artifact_directory),
    )
    compute = turn.compute_commands
    execution: dict[str, object] = {"watcher_host": turn.execution_host}
    if compute is not None:
        values["commands"]["launch"] = turn.patch_inputs.validator_staged.client_command(
            *_LAUNCH_EXAMPLE_ARGS
        )
        machine = turn.service.manifest.machine_map[compute.write_scope.execution_machine]
        manager = JOB_MANAGERS.get(machine.compute.job_manager) if machine.compute else None
        backend = compute.helper_backend
        execution["helper_owner"] = backend.id if backend is not None else None
        execution["job_manager_rules"] = manager.instructions if manager is not None else None
    return {
        **values,
        "execution": execution,
        "repositories": {
            item["alias"]: {"host": item["host"], "path": item["path"]}
            for item in staged.repositories
        },
        "skills": {
            str(item["id"]): {"version": item.get("version"), "path": item.get("path")}
            for item in staged.skill_pointers
        },
        "ontology_sha256": prepared.ontology_sha256,
    }


def _experiment_master(
    execution: AgentTaskExecution,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    *,
    session_id: str,
    episode_id: str | None,
    ontology_extensions: bool,
    render: Callable[[], str] | None,
    values: dict[str, object] | None = None,
) -> MasterRef:
    """The master contract this continuing episode session holds, staged for a pointer.

    A session with no settled master record keeps the exact start contract its own
    episode lineage sent it, recorded now under the current key with the values that
    start recorded. Otherwise the shared lookup points to the recorded master, or
    bootstraps a freshly rendered one from ``values``. Without a renderer, the recorded
    master is kept even under an older key, and the session's next operational launch
    re-opens the current one.
    """

    key = master_key(EXPERIMENT_LOOP_POLICY_VERSION, ontology_extensions=ontology_extensions)
    record = execution.store.agent_task(execution.operation_id)
    if record is None or not record.stage_root:
        raise ValueError("An Experiment-loop continuation has no saved stage for its master.")
    recorded = execution.store.latest_session_master(record.project_id, session_id)
    if recorded is None and episode_id:
        started = _session_start_contract(
            execution,
            episode_id=episode_id,
            session_id=session_id,
            stage_identity=(record.stage_host or "", record.stage_root),
            key=key,
        )
        if started is not None:
            content, started_values = started
            record_session_master(
                execution.store, execution.operation_id, content, key, started_values
            )
            path = _stage_or_reuse_task_input(
                local_stage,
                remote_stage,
                session_master_label(_EXPERIMENT_MASTER_LABEL, content),
                content,
            )
            return MasterRef(path=path, bootstrap=False, values=started_values)
    if render is None:
        if recorded is None:
            raise ValueError(
                "The native session has no recoverable master contract; retry the "
                "Experiment turn from a clean attempt instead."
            )
        operation_id, digest, _ = recorded
        content = execution.store.agent_task_contract(operation_id, SESSION_MASTER_ROLE) or ""
        path = stage_session_master(
            execution.store,
            local_stage=local_stage,
            remote_stage=remote_stage,
            operation_id=operation_id,
            sha256=digest,
            path=session_master_label(_EXPERIMENT_MASTER_LABEL, content),
        )
        return MasterRef(
            path=path, bootstrap=False, values=recorded_master_values(execution.store, operation_id)
        )
    return continuation_session_master(
        execution,
        local_stage=local_stage,
        remote_stage=remote_stage,
        native_session_id=session_id,
        label_prefix=_EXPERIMENT_MASTER_LABEL,
        key=key,
        render=render,
        values=values,
    )


def _stale_master_graph_rules(
    execution: AgentTaskExecution, session_id: str, *, ontology_extensions: bool
) -> list[str]:
    """The current graph rules, when the master a repair keeps was rendered under another key.

    A repair stages no loop inputs to render a new master, so the rules its Patch must
    follow travel inline instead.
    """

    record = execution.store.agent_task(execution.operation_id)
    recorded = (
        execution.store.latest_session_master(record.project_id, session_id)
        if record is not None
        else None
    )
    key = master_key(EXPERIMENT_LOOP_POLICY_VERSION, ontology_extensions=ontology_extensions)
    if recorded is not None and recorded[2] == key:
        return []
    return [graph_rules(edits=True, ontology_extensions=ontology_extensions)]


def _session_start_contract(
    execution: AgentTaskExecution,
    *,
    episode_id: str,
    session_id: str,
    stage_identity: tuple[str, str],
    key: str,
) -> tuple[str, dict[str, object] | None] | None:
    """The exact full contract that started this session on this stage, if retained.

    The search follows the episode back through the episodes it continues, because an
    Add-turns episode runs on the session an earlier episode started. A start recorded
    under another master key is not reused; the caller renders a new master instead.
    The values the start recorded beside it come back with it. Only a succeeded start
    counts: one that failed or paused may never have delivered its contract.
    """

    episode_ids: list[str] = []
    current: str | None = episode_id
    while current is not None and current not in episode_ids:
        episode_ids.append(current)
        source = execution.store.episode(current)
        current = source.continues_episode_id if source is not None else None
    tasks = [
        task
        for lineage_id in episode_ids
        for task in reversed(execution.store.episode_tasks(lineage_id))
        if task.status == "succeeded" and (task.stage_host or "", task.stage_root) == stage_identity
    ]
    for candidate_session_id in (session_id, None):
        for task in tasks:
            if task.native_session_id != candidate_session_id:
                continue
            for durable in reversed(execution.store.agent_task_contracts(task.operation_id)):
                if durable.role not in {"work", "work_retry_base"}:
                    continue
                digest = hashlib.sha256(durable.content.encode("utf-8")).hexdigest()
                if digest != durable.sha256:
                    raise ValueError("The Experiment-loop session contract is corrupt.")
                started_key = execution.store.agent_task_contract(
                    task.operation_id, SESSION_MASTER_KEY_ROLE
                )
                if started_key not in {None, key}:
                    return None
                return durable.content, recorded_master_values(execution.store, task.operation_id)
    return None


def _report_rebootstrap_pending(turn: WorkTurn) -> bool:
    assert turn.execution is not None and turn.request.session_id
    return report_rebootstrap_pending(turn.execution, turn.request.session_id)


def _record_continuation_prompt(
    turn: WorkTurn,
    phase: LaunchPhase,
    parts: list[str],
    master: MasterRef,
    *,
    delta: dict[str, object] | None,
    report_ended: bool,
    label: str,
    role: str,
) -> tuple[str, str]:
    """Compose one continuation inline, record it for recovery, and return its path and text.

    After an episode report on this session, the master is re-opened instead of pointed to,
    because the report's restriction is the newest instruction the session holds.
    """

    if report_ended:
        master = replace(master, after_report=True)
    prompt = compose(classify(phase), parts=parts, master=master, delta=delta)
    contract_path = record_inline_prompt(
        turn.execution,
        local_stage=turn.local_stage,
        remote_stage=turn.remote_stage,
        label=label,
        role=role,
        prompt=prompt,
    )
    return contract_path, prompt


def _turn_invocations(turn: WorkTurn, staged: _StagedWorkInputs) -> list[str]:
    """This turn's invoked packages and provider skills, which never enter the master."""

    return [
        section
        for section in (
            _invoked_package_section(
                invoked_package_pointers(
                    staged.skill_pointers,
                    workflow_ids=turn.request.invoked_workflow_ids,
                    skill_ids=turn.request.invoked_skill_ids,
                )
            ).strip(),
            invoked_provider_skill_section(turn.request.resolved_provider_skills).strip(),
        )
        if section
    ]


def _continued_master(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
    prepared: _WorkPromptContext,
    values: dict[str, object],
) -> MasterRef:
    assert turn.execution is not None and turn.request.session_id
    return _experiment_master(
        turn.execution,
        turn.local_stage,
        turn.remote_stage,
        session_id=turn.request.session_id,
        episode_id=turn.request.control_episode_id,
        ontology_extensions=turn.context.ontology_extensions,
        render=lambda: _experiment_start_contract(
            turn,
            staged,
            prepared,
            human_request=_episode_objective(turn),
            invoked=False,
        ),
        values=values,
    )


def _episode_objective(turn: WorkTurn) -> str:
    """The human objective the episode started from, for a master rendered mid-episode."""

    assert turn.execution is not None and turn.request.control_node_id
    for task in turn.execution.store.episode_tasks(turn.request.control_episode_id or ""):
        message = task.request.get("message")
        if task.request.get("trigger") != "watcher" and isinstance(message, str):
            return experiment_start_message(message, turn.request.control_node_id)
    return experiment_start_message(turn.request.message, turn.request.control_node_id)


def _compose_continuation(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
    prepared: _WorkPromptContext,
    phase: Literal["turn", "wake", "recovery"],
    parts: list[str],
    *,
    label: str,
    role: str,
) -> _ComposedExperimentPrompt:
    """Send this continuation's own parts, then the values that changed from its master."""

    values = _experiment_values(turn, staged, prepared)
    master = _continued_master(turn, staged, prepared, values)
    contract_path, prompt = _record_continuation_prompt(
        turn,
        LaunchPhase(session_id=turn.request.session_id, phase=phase),
        parts,
        master,
        delta=changed_since_master(master, values),
        report_ended=_report_rebootstrap_pending(turn),
        label=label,
        role=role,
    )
    return _ComposedExperimentPrompt(
        contract_path=contract_path,
        prompt=prompt,
        base_contract_path=master.path,
        values=values,
    )


def _compose_recovery_prompt(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
    prepared: _WorkPromptContext,
    mode: Literal["resume", "retry"],
) -> _ComposedExperimentPrompt:
    assert turn.execution is not None
    retry_diagnostics_path = _stage_retry_diagnostics(turn, staged) if mode == "retry" else None
    if not prepared.loop_control_path or (mode == "retry" and not retry_diagnostics_path):
        raise ValueError(f"Experiment-loop {mode.title()} is missing fresh control or diagnostics.")
    parts = experiment_loop_continuation_contract(
        mode=mode, diagnostics_path=retry_diagnostics_path
    )
    return _compose_continuation(
        turn,
        staged,
        prepared,
        "recovery",
        [*parts, *_turn_invocations(turn, staged)],
        label=f"task-{staged.token}-{mode}.md",
        role="work_resume" if mode == "resume" else "work_retry",
    )


def _compose_wake_prompt(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
    prepared: _WorkPromptContext,
) -> _ComposedExperimentPrompt:
    if (
        prepared.wake_episode is None
        or not turn.request.control_node_id
        or turn.request.control_invocation is None
        or turn.request.control_invocation_ceiling is None
        or not prepared.loop_control_path
        or not prepared.watcher_state_path
    ):
        raise ValueError("Experiment-loop wake inputs are incomplete after staging.")
    parts = experiment_loop_wake_message(
        focused_experiment_id=turn.request.control_node_id,
        invocation=turn.request.control_invocation,
        invocation_ceiling=turn.request.control_invocation_ceiling,
        previous_graph_result=prepared.wake_episode.last_graph_result or "",
        previous_watcher_ids=prepared.wake_episode.last_watcher_ids,
        delivered_watcher_ids=turn.request.watcher_ids,
    )
    return _compose_continuation(
        turn,
        staged,
        prepared,
        "wake",
        [*parts, *_turn_invocations(turn, staged)],
        label=f"task-{staged.token}-watcher-wake.md",
        role="experiment_loop_wake",
    )


def _compose_human_turn_prompt(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
    prepared: _WorkPromptContext,
    *,
    retry_diagnostics_path: str | None,
) -> _ComposedExperimentPrompt:
    """Continue an ended episode's session with the human's new turns, never its full contract."""

    assert turn.request.message is not None
    if (
        not turn.request.control_node_id
        or turn.request.control_invocation is None
        or turn.request.control_invocation_ceiling is None
        or not prepared.loop_control_path
        or not prepared.watcher_state_path
    ):
        raise ValueError("Experiment-loop turn inputs are incomplete after staging.")
    opening, human_message = experiment_loop_turn_message(
        focused_experiment_id=turn.request.control_node_id,
        invocation=turn.request.control_invocation,
        invocation_ceiling=turn.request.control_invocation_ceiling,
        human_message=turn.request.message,
        diagnostics_path=retry_diagnostics_path,
    )
    return _compose_continuation(
        turn,
        staged,
        prepared,
        "turn",
        [opening, *_turn_invocations(turn, staged), human_message],
        label=f"task-{staged.token}-turn.md",
        role="experiment_loop_turn",
    )


def _experiment_start_contract(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
    prepared: _WorkPromptContext,
    *,
    human_request: str,
    invoked: bool,
    retry_diagnostics_path: str | None = None,
) -> str:
    """Render the full contract a session starts from, with this turn's inputs."""

    if (
        not turn.request.control_node_id
        or not prepared.loop_control_path
        or not prepared.watcher_state_path
    ):
        raise ValueError("Experiment-loop contract inputs are incomplete after staging.")
    human_request_path = _stage_or_reuse_task_input(
        turn.local_stage,
        turn.remote_stage,
        f"task-{staged.token}-human-request.txt",
        human_request,
    )
    contract = experiment_loop_task_contract(
        execution_instructions=_work_execution_instructions(turn),
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
        invoked_skill_pointers=(
            invoked_package_pointers(
                staged.skill_pointers,
                workflow_ids=turn.request.invoked_workflow_ids,
                skill_ids=turn.request.invoked_skill_ids,
            )
            if invoked
            else None
        ),
    )
    if invoked:
        contract += invoked_provider_skill_section(turn.request.resolved_provider_skills)
    return contract


def _compose_fresh_prompt(
    turn: WorkTurn,
    staged: _StagedWorkInputs,
    prepared: _WorkPromptContext,
    *,
    retry_diagnostics_path: str | None = None,
) -> _ComposedExperimentPrompt:
    assert turn.request.message is not None
    if turn.request.session_id is not None:
        return _compose_human_turn_prompt(
            turn, staged, prepared, retry_diagnostics_path=retry_diagnostics_path
        )
    contract = _experiment_start_contract(
        turn,
        staged,
        prepared,
        human_request=turn.request.message,
        invoked=True,
        retry_diagnostics_path=retry_diagnostics_path,
    )
    contract_path, prompt = _stage_task_contract(
        turn.local_stage,
        turn.remote_stage,
        f"task-{staged.token}-{'base' if turn.retry_attempt else 'initial'}.md",
        contract,
        execution=turn.execution,
        role="work_retry_base" if turn.retry_attempt else "work",
    )
    values = _experiment_values(turn, staged, prepared)
    if turn.execution is not None:
        record_session_master(
            turn.execution.store,
            turn.execution.operation_id,
            contract,
            master_key(
                EXPERIMENT_LOOP_POLICY_VERSION,
                ontology_extensions=turn.context.ontology_extensions,
            ),
            values,
        )
    return _ComposedExperimentPrompt(
        contract_path=contract_path,
        prompt=prompt,
        base_contract_path=contract_path,
        values=values,
    )


def _read_initial_patch_deliverable(
    turn: WorkFinalizationContext,
    predecessor_digest: str | None,
    settled: _SettledExperimentDeliverables,
) -> _DeliverableRead:
    try:
        text = _read_chat_patch(turn.workspace, turn.remote_stage)
    except (OSError, StateUnavailable, ValueError) as exc:
        _note_stage_unreachable(turn.execution, exc)
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
    if text is not None and turn.execution is not None:
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
    # Loop graph admission is a joint Patch/watch handoff. Nothing in the
    # pre-handoff path may validate-correct-and-apply it.
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
        _note_stage_unreachable(turn.execution, exc)
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
    if text is None and failure is None:
        failure = _DeliverableFailure(
            "Experiment-loop work must write watch.json as an object with external and graph "
            "lists; leave both lists empty only after confirming nothing remains to watch.",
            correctable=True,
        )
    return _DeliverableRead(text=text, failure=failure)


def _read_corrected_watch_deliverable(turn: WorkFinalizationContext) -> _DeliverableRead:
    try:
        corrected_watch = _read_watch_request(turn.workspace, turn.remote_stage)
    except (OSError, StateUnavailable, ValueError) as exc:
        _note_stage_unreachable(turn.execution, exc)
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


async def _validate_watch_deliverable(
    turn: WorkFinalizationContext,
    watch_text: str,
    correction_rounds: int,
    settled: _SettledExperimentDeliverables,
) -> _DeliverableStep:
    try:
        if turn.execution is None:
            raise ValueError("Watcher arming requires a durable originating operation.")
        origin_task = turn.execution.store.agent_task(turn.execution.operation_id)
        if origin_task is None:
            raise ValueError("The originating Experiment-loop operation is no longer available.")
        handoff = parse_experiment_watch_json(watch_text)
        if turn.compute_commands is not None:
            await asyncio.to_thread(
                turn.compute_commands.validate_handoff,
                {item.check_command for item in handoff.observers},
            )
        settled.loop_watch_text = watch_text
        if handoff.is_empty:
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
        binding = WatcherBinding(
            project_id=origin_task.project_id,
            origin_operation_id=root_experiment_loop_operation_id(turn.execution),
            origin_task_kind=turn.surface,
            chat_id=turn.request.chat_id or "",
            node_id=turn.request.node_id,
            episode_id=turn.request.control_episode_id,
            graph_target=origin_task.graph_target,
            execution_host=turn.execution_host,
            continuation=_watcher_continuation(turn),
        )
        if handoff.stops:
            turn.execution.store.validate_experiment_agent_watcher_stops(
                binding,
                handoff.stops,
            )
        turn.execution.store.validate_experiment_observer_duplicates(
            binding,
            handoff.observers,
            stops=handoff.stops,
            rearmed_watcher_ids=[
                experiment_observer_watcher_id(binding, index, spec)
                for index, spec in enumerate(handoff.observers)
            ],
        )
        graph_armed_revision = None
        if handoff.graph_conditions:
            graph_state = await asyncio.to_thread(turn.service.history.state)
            await asyncio.to_thread(
                validate_graph_conditions,
                handoff.graph_conditions,
                graph_state,
            )
            graph_armed_revision = graph_state.revision
        check_results = (
            await asyncio.to_thread(
                validate_watch_specs,
                handoff.observers,
                turn.execution_host,
            )
            if handoff.observers
            else []
        )
        settled.pending_loop_handoff = (
            handoff.observers,
            check_results,
            handoff.graph_conditions,
            graph_armed_revision,
            binding,
            handoff.stops,
        )
    except WatcherInitialCheckError as exc:
        return _DeliverableStep(failure=_DeliverableFailure(str(exc), correctable=True))
    except ValueError as exc:
        return _DeliverableStep(failure=_DeliverableFailure(str(exc), correctable=True))
    except (OSError, ReplayHalted, StateUnavailable) as exc:
        _note_stage_unreachable(turn.execution, exc)
        return _DeliverableStep(failure=_DeliverableFailure(str(exc), correctable=False))

    settled.loop_watch_empty = (
        not handoff.observers
        and not handoff.graph_conditions
        and (
            not handoff.stops
            or not turn.execution.store.experiment_handoff_has_live_watcher_after_stops(
                binding,
                [item.stop_watcher_id for item in handoff.stops],
            )
        )
    )
    settled.watch_correction_rounds = correction_rounds
    return _DeliverableStep()


def _correction_prompt(
    launch_turn: WorkTurn,
    composed: _ComposedExperimentPrompt,
    session_id: str | None,
    parts: list[str],
    *,
    validator_command: str,
    label: str,
    role: str,
) -> tuple[str, str]:
    """Correct inside this operation's session, pointing to the master its launch used.

    The provider already answered this operation's launch, so the master it opened or
    was pointed to is the one it holds; the correction never re-opens it. The session
    already holds this launch's values, so only the correction's own validator is new.
    """

    launched = composed.values or {}
    commands = launched.get("commands")
    current = {
        **launched,
        "commands": {
            **(commands if isinstance(commands, dict) else {}),
            "validate": validator_command,
        },
    }
    master = MasterRef(path=composed.base_contract_path, bootstrap=False, values=composed.values)
    return _record_continuation_prompt(
        launch_turn,
        LaunchPhase(session_id=session_id, phase="correction"),
        parts,
        master,
        delta=changed_since_master(master, current),
        report_ended=False,
        label=label,
        role=role,
    )


def _reject_watch_deliverable(
    turn: WorkFinalizationContext,
    settled: _SettledExperimentDeliverables,
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


async def _settle_watch_deliverable(
    turn: WorkFinalizationContext,
    launcher: AgentLauncher,
    predecessor_digest: str | None,
    settled: _SettledExperimentDeliverables,
    *,
    launch_turn: WorkTurn | None = None,
    staged: _StagedWorkInputs | None = None,
    composed: _ComposedExperimentPrompt | None = None,
    maximum_corrections: int = EXPERIMENT_LOOP_WATCH_CORRECTION_MAX_ROUNDS,
) -> AsyncIterator[str]:
    initial = _read_initial_watch_deliverable(turn, predecessor_digest)
    text = initial.text
    failure = initial.failure
    settled.loop_watch_text = text
    if text is None and failure is None:
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
            step = _reject_watch_deliverable(turn, settled, failure, correction_rounds)
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
        diagnostics_path = _stage_json_task_input(
            turn.local_stage,
            turn.remote_stage,
            f"task-{staged.token}-watch-correction-{correction_rounds}.json",
            {"problem": failure.message},
        )
        correction_validator: StagedCommandMailbox | None = None
        correction_lifecycle: _WorkValidatorMailboxLifecycle | None = None
        try:
            validator_command = launch_turn.patch_inputs.validator_command
            correction_validator = stage_patch_validation_mailbox(
                authority="broker",
                episode_id=turn.request.control_episode_id,
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
                budget=launch_turn.validator_budget,
                compute_commands=turn.compute_commands,
                run_truth_scope=turn.run_truth_scope,
                control_node_id=turn.request.control_node_id,
                control_decision_bundle=turn.request.control_decision_bundle,
            )
            validator_command = correction_validator.client_command(
                "validate",
                launch_turn.patch_inputs.patch_path,
            )
            correction_path, correction_prompt = _correction_prompt(
                launch_turn,
                composed,
                settled.native_session_id,
                experiment_loop_watcher_correction_contract(diagnostics_path=diagnostics_path),
                validator_command=validator_command,
                label=f"task-{staged.token}-watch-correction-{correction_rounds}.md",
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
            correction_stream = _stream_turn_agent_events(
                launch_turn,
                launcher,
                correction_prompt,
                session_id=settled.native_session_id,
                required_session_id=settled.native_session_id,
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
        corrected = _read_corrected_watch_deliverable(turn)
        text = corrected.text
        failure = corrected.failure
        settled.loop_watch_text = text


async def _resettle_changed_watch_handoff(
    turn: WorkFinalizationContext,
    launcher: AgentLauncher,
    settled: _SettledExperimentDeliverables,
    applied: _AppliedWorkTurn,
    *,
    launch_turn: WorkTurn | None = None,
    staged: _StagedWorkInputs | None = None,
    composed: _ComposedExperimentPrompt | None = None,
    maximum_corrections: int = EXPERIMENT_LOOP_WATCH_CORRECTION_MAX_ROUNDS,
) -> AsyncIterator[str]:
    # A correction can change the observer declaration or launch more compute.
    # Preserve Patch-read diagnostics before checking that operational handoff.
    try:
        changed_watch = (
            _read_watch_request(turn.workspace, turn.remote_stage) != settled.loop_watch_text
        )
    except (OSError, StateUnavailable, ValueError) as exc:
        _note_stage_unreachable(turn.execution, exc)
        changed_watch = True
    if not changed_watch and turn.compute_commands is not None:
        observers = settled.pending_loop_handoff[0] if settled.pending_loop_handoff else []
        try:
            await asyncio.to_thread(
                turn.compute_commands.validate_handoff,
                {item.check_command for item in observers},
            )
        except ValueError:
            changed_watch = True
    if changed_watch:
        settled.native_session_id = applied.native_session_id
        async with aclosing(
            _settle_watch_deliverable(
                turn,
                launcher,
                None,
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
            applied.stop = True
            return
        applied.native_session_id = settled.native_session_id


async def _apply_experiment_loop_turn(
    turn: WorkFinalizationContext,
    launcher: AgentLauncher,
    settled: _SettledExperimentDeliverables,
    applied: _AppliedWorkTurn,
    *,
    episode_context_baseline: dict[str, object] | None,
    experiment_control_snapshot: dict[str, object] | None,
    launch_turn: WorkTurn | None = None,
    staged: _StagedWorkInputs | None = None,
    composed: _ComposedExperimentPrompt | None = None,
    maximum_corrections: int = PATCH_CORRECTION_MAX_ROUNDS,
    maximum_watch_corrections: int = EXPERIMENT_LOOP_WATCH_CORRECTION_MAX_ROUNDS,
) -> AsyncIterator[str]:
    try:
        final_patch_text = _read_chat_patch(turn.workspace, turn.remote_stage)
    except (OSError, StateUnavailable, ValueError) as exc:
        _note_stage_unreachable(turn.execution, exc)
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
                            run_truth_scope=turn.run_truth_scope,
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
                or loop_patch_correction_rounds >= maximum_corrections
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
            if launch_turn is None or staged is None or composed is None:
                raise RuntimeError("A loop Patch correction requires its live launch context.")
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
                authority="broker",
                episode_id=turn.request.control_episode_id,
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
                budget=launch_turn.validator_budget,
                compute_commands=turn.compute_commands,
                run_truth_scope=turn.run_truth_scope,
                control_node_id=turn.request.control_node_id,
                control_decision_bundle=turn.request.control_decision_bundle,
            )
            try:
                loop_validator_command = loop_validator.client_command(
                    "validate",
                    launch_turn.patch_inputs.patch_path,
                )
                correction_path, correction_prompt = _correction_prompt(
                    launch_turn,
                    composed,
                    applied.native_session_id,
                    experiment_loop_patch_correction_contract(diagnostics_path=diagnostics_path),
                    validator_command=loop_validator_command,
                    label=(
                        f"task-{staged.token}-loop-patch-correction-"
                        f"{loop_patch_correction_rounds}.md"
                    ),
                    role=(f"experiment_loop_patch_correction_{loop_patch_correction_rounds}"),
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
                    launch_turn,
                    launcher,
                    correction_prompt,
                    session_id=applied.native_session_id,
                    required_session_id=applied.native_session_id,
                    outcome=correction_outcome,
                    validator_staged=loop_validator,
                    validator_lifecycle=loop_validator_lifecycle,
                    supervise_remote=launch_turn.supervise_remote,
                )
            ) as stream:
                async for frame in stream:
                    yield frame
            applied.native_session_id = correction_outcome.session_id or applied.native_session_id
            if correction_outcome.paused or correction_outcome.remote_result_pending:
                applied.stop = True
                return
            if not correction_outcome.completed:
                final_failure = _DeliverableFailure(
                    f"{turn.request.provider} produced no Patch correction result.",
                    correctable=True,
                )
                final_patch_text = None
                loop_patch_correction_rounds = maximum_corrections
                continue
            corrected = read_correction_patch(
                lambda: _read_chat_patch(turn.workspace, turn.remote_stage),
            )
            if corrected.problem == "unreadable":
                final_failure = _DeliverableFailure(
                    f"The corrected loop Patch could not be read: {corrected.detail}",
                    correctable=True,
                )
                final_patch_text = None
                continue
            if corrected.problem == "missing":
                final_failure = _DeliverableFailure(
                    "The loop Patch correction did not leave patch.json.",
                    correctable=True,
                )
                final_patch_text = None
                continue
            assert corrected.text is not None
            final_patch_text = corrected.text
            async with aclosing(
                _resettle_changed_watch_handoff(
                    turn,
                    launcher,
                    settled,
                    applied,
                    launch_turn=launch_turn,
                    staged=staged,
                    composed=composed,
                    maximum_corrections=maximum_watch_corrections,
                )
            ) as stream:
                async for frame in stream:
                    yield frame
            if applied.stop:
                return
            # A nested watcher correction may rewrite patch.json; the next iteration
            # must validate and apply what is on disk, not the earlier correction.
            try:
                rewritten = _read_chat_patch(turn.workspace, turn.remote_stage)
            except (OSError, StateUnavailable, ValueError) as exc:
                _note_stage_unreachable(turn.execution, exc)
                rewritten = None
                detail = str(exc)
            else:
                detail = "patch.json is missing after the watcher correction."
            if rewritten is None:
                final_failure = _DeliverableFailure(
                    f"The corrected loop Patch could not be read: {detail}", correctable=True
                )
            final_patch_text = rewritten

    # Even rejected graph reflection must retain a complete operational handoff.
    async with aclosing(
        _resettle_changed_watch_handoff(
            turn,
            launcher,
            settled,
            applied,
            launch_turn=launch_turn,
            staged=staged,
            composed=composed,
            maximum_corrections=maximum_watch_corrections,
        )
    ) as stream:
        async for frame in stream:
            yield frame
    if applied.stop:
        return

    if (
        turn.execution is None
        or episode_context_baseline is None
        or experiment_control_snapshot is None
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
            continuation=_watcher_continuation(turn),
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
        _note_stage_unreachable(turn.execution, exc)
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
                control_snapshot=experiment_control_snapshot,
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
    invocation = turn.request.control_invocation
    if not _handoff_receipt_recorded(
        execution, execution.operation_id, "experiment_loop_handoff_prepared", invocation
    ):
        execution.store.record_agent_task_receipt(
            execution.operation_id,
            "experiment_loop_handoff_prepared",
            {
                "episode_id": turn.request.control_episode_id,
                "invocation": invocation,
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
            context_baseline=episode_context_baseline,
            stops=stop_requests,
            ending_signal=ending_signal,
        )
    except (OSError, ReplayHalted, StateUnavailable, ValueError) as exc:
        _note_stage_unreachable(turn.execution, exc)
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
    if not _handoff_receipt_recorded(execution, root_id, "watchers_armed", invocation):
        execution.store.record_agent_task_receipt(
            root_id,
            "watchers_armed",
            {
                # The root collects one of these per invocation, so it names
                # which one it is rather than only what it armed.
                "invocation": invocation,
                "watcher_ids": [item.watcher_id for item in armed],
                "stopped_watcher_ids": [item.stop_watcher_id for item in stop_requests],
                "count": len(armed),
                "correction_rounds": settled.watch_correction_rounds,
            },
        )


def _handoff_receipt_recorded(
    execution: AgentTaskExecution,
    operation_id: str,
    category: str,
    invocation: int | None,
) -> bool:
    """Whether this exact invocation already wrote this handoff receipt.

    The root operation collects one of these per invocation, so the category
    alone cannot tell a replay repeating one from a later turn adding its own.
    Recovery replays the whole settlement, and operational history is a product
    of this system rather than a log, so one invocation says this once.
    """

    return any(
        receipt.category == category and receipt.payload.get("invocation") == invocation
        for receipt in execution.store.agent_task_receipts(operation_id)
    )


def _settle_experiment_loop_outcome(
    turn: WorkFinalizationContext,
    *,
    wake_native_session_id: str | None,
) -> list[str]:
    """Read one finished loop pass into its answer, or say why there is none.

    Live and recorded delivery reach this verdict the same way, from the same
    events. An automatic wake is still held to the session its launch committed
    to, which a recorded pass carries forward rather than re-reading.
    """

    frames: list[str] = []
    # A correction of this turn is supervised too, so a recovered journal may be
    # the correction rather than the pass. Its prose is not the human reply.
    retained_answer = _retained_primary_answer(turn)
    answer = (
        retained_answer
        or "\n\n".join(item.strip() for item in turn.outcome.answers if item.strip()).strip()
    )
    if not turn.outcome.completed:
        if turn.outcome.failed or turn.outcome.paused:
            return frames
        turn.outcome.failed = True
        frames.append(
            _sse(AgentEvent(event="error", text=f"{turn.request.provider} produced no result."))
        )
        return frames
    if not answer:
        frames.append(
            _sse(
                AgentEvent(
                    event="error",
                    text=f"{turn.request.provider} finished without answering.",
                )
            )
        )
        return frames
    if turn.continuation == "watcher_wake" and (
        wake_native_session_id is None or turn.outcome.session_id != wake_native_session_id
    ):
        frames.append(
            _sse(
                AgentEvent(
                    event="error",
                    text=(
                        "The automatic Experiment wake did not continue its committed native "
                        "provider session. The watcher handoff was not accepted."
                    ),
                )
            )
        )
        return frames

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
    turn.answer = answer
    if (
        retained_answer is None
        and turn.execution is not None
        and turn.finalization_role is not None
        and turn.execution.store.agent_task_contract(
            turn.execution.operation_id, turn.finalization_role
        )
        is not None
    ):
        # Corrections have their own journals, but their prose is not the human
        # reply. Retain the completed primary answer before any starts.
        turn.execution.store.record_agent_task_contract(
            turn.execution.operation_id,
            _WORK_PRIMARY_ANSWER_ROLE,
            answer,
            hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        )
    frames.append(_sse(AgentEvent(event="answer", text=answer)))
    frames.extend(_sse(AgentEvent(event="artifact", artifact=item)) for item in artifacts)
    return frames


async def _launch_and_stream_work_turn(
    turn: WorkTurn,
    finalization: WorkFinalizationContext,
    launcher: AgentLauncher,
    prompt: str,
    contract_path: str,
    wake_episode: EpisodeRecord | None,
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
                    required_session_id=(
                        required_session_id
                        if required_session_id is not None
                        else _required_work_continuation_session_id(
                            turn.request,
                            turn.execution,
                            session_id=turn.request.session_id,
                        )
                    ),
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
            # The host has this turn now. Its original task waits, and the
            # reconciler settles it through the recorded door.
            return
        for frame in _settle_experiment_loop_outcome(
            finalization,
            wake_native_session_id=(
                wake_episode.native_session_id if wake_episode is not None else None
            ),
        ):
            yield frame
    except BaseException:
        raise
    turn.answer = finalization.answer


async def settle_experiment_loop_deliverables(
    turn: WorkFinalizationContext,
    launcher: AgentLauncher,
    retry_baseline: _RetryDeliverableBaseline,
    *,
    episode_context_baseline: dict[str, object] | None,
    experiment_control_snapshot: dict[str, object] | None,
    launch_turn: WorkTurn | None = None,
    staged: _StagedWorkInputs | None = None,
    composed: _ComposedExperimentPrompt | None = None,
    maximum_corrections: int = PATCH_CORRECTION_MAX_ROUNDS,
    maximum_watch_corrections: int = EXPERIMENT_LOOP_WATCH_CORRECTION_MAX_ROUNDS,
) -> AsyncIterator[str]:
    """Settle one finished loop turn, live or recorded, against its own task.

    Both deliveries reach the same joint Patch/watch admission and the same
    episode handoff. A recorded pass allows no correction round, because the
    provider that could answer one stopped when its connection did.
    """

    if turn.answer is None:
        return
    answer = turn.answer
    settled = _SettledExperimentDeliverables(native_session_id=turn.outcome.session_id)
    initial_patch = _read_initial_patch_deliverable(turn, retry_baseline.patch_digest, settled)
    if initial_patch.failure is not None:
        if turn.execution is not None:
            turn.execution.store.record_agent_task_receipt(
                turn.execution.operation_id,
                "experiment_unrecoverable_deliverable",
                {"diagnostic": initial_patch.failure.message},
                tier="summary",
            )
        yield _sse(AgentEvent(event="error", text=initial_patch.failure.message))
        return
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
            maximum_corrections=maximum_watch_corrections,
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
    apply_stream = _apply_experiment_loop_turn(
        turn,
        launcher,
        settled,
        applied,
        episode_context_baseline=episode_context_baseline,
        experiment_control_snapshot=experiment_control_snapshot,
        launch_turn=launch_turn,
        staged=staged,
        composed=composed,
        maximum_corrections=maximum_corrections,
        maximum_watch_corrections=maximum_watch_corrections,
    )
    async with aclosing(apply_stream) as stream:
        async for frame in stream:
            yield frame
    if applied.stop:
        return

    for frame in _finalize_work_turn(turn, answer, applied.graph_update):
        yield frame


async def stream_experiment_loop_task(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    execution: AgentTaskExecution | None = None,
) -> AsyncIterator[str]:
    """Run one already-admitted Experiment-loop invocation end to end."""

    if request.mode != "work" or request.patch_kind != "experiment_loop":
        yield _sse(
            AgentEvent(
                event="error",
                text="The Experiment-loop task owner received a non-Experiment Work request.",
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
        resuming = turn.resuming
        # An Experiment-loop watcher wake resumes the episode's native session, but it
        # is a new turn at the next invocation -- never task Resume, never a retry, and
        # never a rebuilt master contract.
        waking = turn.waking
        prompt_context = await _prepare_work_prompt_context(turn, staged)
        wake_episode = prompt_context.wake_episode
        if resuming:
            composed_prompt = _compose_recovery_prompt(turn, staged, prompt_context, "resume")
        elif waking:
            composed_prompt = _compose_wake_prompt(turn, staged, prompt_context)
        else:
            if turn.retrying:
                composed_prompt = _compose_recovery_prompt(turn, staged, prompt_context, "retry")
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
        note_link_lost_before_provider(execution, exc)
        if isinstance(exc, (OSError, ReplayHalted, StateUnavailable, ValueError)):
            yield _sse(AgentEvent(event="error", text=str(exc)))
            return
        raise

    assert turn is not None
    finalization = _work_finalization_context(
        turn, staged, role=EXPERIMENT_LOOP_FINALIZATION_CONTEXT_ROLE
    )
    # One value, enforced by the live stream and retained for recovery, so the
    # two cannot come to disagree about which session this turn may continue.
    required_session_id = _required_work_continuation_session_id(
        turn.request, turn.execution, session_id=turn.request.session_id
    )
    if turn.execution_host:
        # Only a remote turn can outlive this connection, and only a turn whose
        # settling facts are already written down can be recovered.
        _record_work_finalization_context(
            turn,
            staged,
            role=EXPERIMENT_LOOP_FINALIZATION_CONTEXT_ROLE,
            required_session_id=required_session_id,
        )
        _record_experiment_loop_episode_context(turn, prompt_context)
    async with aclosing(
        _launch_and_stream_work_turn(
            turn,
            finalization,
            launcher,
            prompt,
            contract_path,
            wake_episode,
            required_session_id=required_session_id,
            supervise_remote=bool(turn.execution_host),
        )
    ) as stream:
        async for frame in stream:
            yield frame
    if turn.outcome.remote_result_pending:
        return
    async with aclosing(
        settle_experiment_loop_deliverables(
            finalization,
            launcher,
            retry_baseline,
            episode_context_baseline=prompt_context.episode_context_baseline,
            experiment_control_snapshot=prompt_context.experiment_control_snapshot,
            launch_turn=turn,
            staged=staged,
            composed=composed_prompt,
        )
    ) as stream:
        async for frame in stream:
            yield frame


async def finalize_recorded_experiment_loop_result(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    execution: AgentTaskExecution,
    recorded: RecordedProviderTurn,
) -> AsyncIterator[str]:
    """Settle one Experiment-loop turn from the pass its host recorded.

    The episode this settles is the one the launch read, not the one a reconnect
    finds: its baseline, control snapshot and committed wake session were all
    written down before the provider started.
    """

    del data_dir  # Recovery uses only launch-time facts retained by this task.
    turn = _load_work_finalization_context(
        service,
        request,
        execution,
        role=EXPERIMENT_LOOP_FINALIZATION_CONTEXT_ROLE,
        owner="Experiment loop",
    )
    episode = _load_experiment_loop_episode_context(execution)
    verdict = decode_recorded_turn(recorded, provider_turn_request(turn.workspace, recorded))
    refusal = refuse_recorded_session_mismatch(
        execution, turn.outcome, verdict, turn.required_session_id
    )
    if refusal:
        # A wake or continuation that answered on another session arms nothing
        # and admits no Patch, so neither reaches the stage.
        for frame in refusal:
            yield frame
        return
    # The verified Patch replaces whatever the stage holds, before anything reads
    # the stage. Half of this turn's admission is that Patch, and a stage that
    # changed after the host finished would admit a different one.
    _write_recorded_patch(turn, recorded)
    frames = absorb_recorded_events(turn.outcome, verdict)
    frames.extend(
        _settle_experiment_loop_outcome(turn, wake_native_session_id=episode.wake_native_session_id)
    )
    _lead_with_retained_answer(turn, frames)
    for frame in frames:
        yield frame
    if turn.answer is None:
        return
    async with aclosing(
        settle_experiment_loop_deliverables(
            turn,
            launcher,
            _recorded_retry_deliverable_baseline(execution),
            episode_context_baseline=episode.episode_context_baseline,
            experiment_control_snapshot=episode.experiment_control_snapshot,
            maximum_corrections=0,
            maximum_watch_corrections=0,
        )
    ) as stream:
        async for frame in stream:
            yield frame


async def _stream_work_graph_repair(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    *,
    execution: AgentTaskExecution,
) -> AsyncIterator[str]:
    """Repair only a retained Experiment-loop Patch; never repeat the operational turn."""

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
            control_node_id=request.control_node_id,
            control_decision_bundle=request.control_decision_bundle,
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
            episode_request=request,
        )
        read_dirs = _chat_read_dirs(
            context,
            local_stage,
            remote_stage,
            service,
            execution_machine.alias,
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
        if not request.session_id:
            raise ValueError("The graph repair has no native session to continue.")
        master = _experiment_master(
            execution,
            local_stage,
            remote_stage,
            session_id=request.session_id,
            episode_id=request.control_episode_id,
            ontology_extensions=context.ontology_extensions,
            # A repair stages no loop inputs, so it renders no master of its own.
            render=None,
        )
        repair_values = _handoff_values(turn)
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
        contract_path, prompt = _record_continuation_prompt(
            turn,
            LaunchPhase(session_id=request.session_id, phase="recovery"),
            experiment_loop_patch_correction_contract(diagnostics_path=diagnostics_path)
            + _stale_master_graph_rules(
                execution, request.session_id, ontology_extensions=context.ontology_extensions
            ),
            master,
            delta=_changed_handoff_values(master, repair_values),
            report_ended=report_rebootstrap_pending(execution, request.session_id),
            label=f"task-{token}-manual-graph-repair.md",
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
        note_link_lost_before_provider(execution, exc)
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
            required_session_id=request.session_id,
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
            control_node_id=request.control_node_id,
            control_decision_bundle=request.control_decision_bundle,
        ),
        bounded_messages=_bounded_graph_messages,
        record_rejection=lambda update: _record_work_graph_rejection(execution, update),
    )
    for frame in repair.frames:
        yield frame
    if repair.graph_update is None or repair.patch_text is None:
        return
    graph_update = repair.graph_update
    patch_text = repair.patch_text
    if (
        not request.control_episode_id
        or not request.control_node_id
        or request.control_invocation is None
        or request.control_invocation_ceiling is None
    ):
        yield _sse(AgentEvent(event="error", text="The graph repair lost its Experiment episode."))
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


def _start_work_validator_mailbox(
    service: ProjectService,
    staged: StagedCommandMailbox,
    *,
    execution: AgentTaskExecution | None,
    budget: PatchValidationBudget,
    compute_commands: WorkComputeCommands | None = None,
    run_truth_scope: list[str],
    control_node_id: str | None,
    control_decision_bundle: list[ExperimentDecisionPin],
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
            control_node_id=control_node_id,
            control_decision_bundle=control_decision_bundle,
            source_operation_id=_work_patch_source_operation_id(execution),
        ),
    )


def _required_work_continuation_session_id(
    _request: RunRequest,
    execution: AgentTaskExecution | None,
    *,
    session_id: str | None,
) -> str | None:
    """Pin Experiment continuations to their saved native provider session."""

    if execution is None or not execution.reuses_native_checkpoint:
        return None
    return session_id


def _prepare_work_patch_candidate(
    service: ProjectService,
    patch_text: str,
    *,
    run_truth_scope: list[str],
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
        kind="experiment_loop",
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
    control_node_id: str | None,
    control_decision_bundle: list[ExperimentDecisionPin] | None,
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
            control_node_id=control_node_id,
            control_decision_bundle=control_decision_bundle,
            source_operation_id=source_operation_id,
            source_effect_id=source_effect_id,
            profile=profile,
        ),
        bounded_messages=_bounded_graph_messages,
    )


def _apply_work_patch(
    service: ProjectService,
    execution: AgentTaskExecution | None,
    patch_text: str,
    *,
    run_truth_scope: list[str],
    control_node_id: str | None = None,
    control_decision_bundle: list[ExperimentDecisionPin] | None = None,
    profile: AgentProfile = "ordinary",
    source_operation_id: str | None = None,
    source_effect_id: str | None = None,
) -> tuple[GraphUpdateResult | None, _DeliverableFailure | None]:
    """Validate and atomically apply one Experiment-loop Patch candidate."""

    source_operation_id = source_operation_id or _work_patch_source_operation_id(execution)
    return apply_work_patch(
        service,
        execution,
        patch_text,
        prepare_candidate=lambda text: _prepare_work_patch_candidate(
            service,
            text,
            run_truth_scope=run_truth_scope,
            control_node_id=control_node_id,
            control_decision_bundle=control_decision_bundle,
            source_operation_id=source_operation_id,
            source_effect_id=source_effect_id,
            profile=profile,
        ),
        source_operation_id=source_operation_id,
        source_effect_id=source_effect_id,
        canonical_matches=lambda canonical, candidate: (
            canonical.source_operation_id == source_operation_id
            and canonical.source_effect_sha256 == candidate.source_effect_sha256
            and canonical.kind == "experiment_loop"
            and canonical.experiment_control_node_id == control_node_id
        ),
        canonical_binding_error=(
            "Experiment-loop invocation source is bound to a different canonical Patch."
        ),
        rejected_patch_error="The graph rejected the Experiment-loop Patch.",
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
