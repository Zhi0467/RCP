from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from rcp.agents import (
    AgentEvent,
    AgentLauncher,
    PromptFactory,
    prepare_agent_patch,
    validate_agent_patch_shape,
    validate_work_patch,
)
from rcp.agents.experiment_loop_prompt import (
    experiment_loop_continuation_contract,
    experiment_loop_patch_correction_contract,
    experiment_loop_task_contract,
    experiment_loop_wake_message,
    experiment_loop_watcher_correction_contract,
)
from rcp.agents.prompts import CHAT_MASTER_CONTEXT_VERSION
from rcp.background import AgentTaskExecution
from rcp.config import AgentSurface
from rcp.core.models import ExperimentDecisionPin, Patch
from rcp.history import PatchRejected, ReplayHalted
from rcp.runs.chat import (
    _append_chat_exchange,
    _append_chat_graph_receipt,
    _chat_context_delta,
    _chat_read_dirs,
    _chat_stage_name,
    _clear_stale_patch,
    _clear_stale_watch,
    _commit_chat_prompt_state,
    _discover_chat_artifacts,
    _existing_watch_digest,
    _logical_chat_turn_operation_id,
    _prepare_chat_prompt_state,
    _prepare_local_artifact_directory,
    _read_chat_patch,
    _read_watch_request,
    _record_applied_graph_revision,
    _record_artifact_discovery_receipt,
    _record_chat_context_receipt,
    _stage_chat_patch_inputs,
    _validated_local_chat_resume_stage,
    _validated_remote_chat_resume_stage,
    _work_write_dirs,
)
from rcp.runs.experiment_loop import (
    commit_experiment_episode_binding,
    experiment_episode_context_values,
    experiment_graph_result_summary,
    patch_explicitly_exits,
    persist_experiment_watchers_idempotently,
    prepare_experiment_episode_context_candidate,
    root_experiment_loop_operation_id,
    stage_experiment_loop_context,
)
from rcp.runs.patch_validator import (
    PatchValidationBudget,
    PatchValidationResult,
    cleanup_patch_validation_mailbox,
    prepare_patch_validation_mailbox,
    serve_patch_validation_mailbox,
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
from rcp.service import GraphUpdateResult, ProjectService, RunRequest
from rcp.skills.staging import skill_bundle_label, stage_skill_selection
from rcp.storage import WatcherContinuation
from rcp.transport import RemoteRunStage, RunLockCancelled, StateUnavailable
from rcp.watchers import (
    WatcherBinding,
    WatcherInitialCheckError,
    arm_watchers,
    parse_watch_json,
    validate_watch_specs,
)

_MAX_CORRECTION_ROUNDS = 2


@dataclass(frozen=True)
class _WorkPatchFailure:
    message: str
    correctable: bool
    change_summary: tuple[str, ...] = ()
    proposal_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class _PreparedWorkPatch:
    patch: Patch
    change_summary: tuple[str, ...]
    proposal_ids: tuple[str, ...]


def _work_patch_source_operation_id(
    execution: AgentTaskExecution | None,
    patch_kind: Literal["work", "experiment_loop"],
) -> str | None:
    if execution is None:
        return None
    if patch_kind == "experiment_loop":
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
    )
    return prompt, retained_master_path


def _retry_deliverable_is_unchanged(
    execution: AgentTaskExecution | None,
    *,
    filename: str,
    predecessor_digest: str | None,
    current_text: str | None,
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
            "consumed": current_text is not None and not unchanged,
        },
        tier="diagnostic",
    )
    return unchanged


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

    continuation = execution.continuation if execution is not None else "fresh"
    reusing_checkpoint = bool(execution is not None and execution.reuses_native_checkpoint)
    resuming = continuation == "resume"
    retrying = continuation == "retry"
    # An Experiment-loop watcher wake resumes the episode's native session, but it
    # is a new turn at the next invocation -- never task Resume, never a retry, and
    # never a rebuilt master contract.
    waking = continuation == "watcher_wake"
    retry_attempt = continuation in {"retry", "handoff"}
    uses_master_protocol = (
        request.trigger == "human" and request.patch_kind == "work" and not retry_attempt
    )
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
    validator_budget = PatchValidationBudget()
    try:
        try:
            context = service.assemble_chat(request)
            _record_chat_context_receipt(execution, context, surface=surface)
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
            patch_inputs = _stage_chat_patch_inputs(
                local_stage,
                remote_stage,
                workspace=workspace,
                stage_name=stage_name,
            )
            patch_path = patch_inputs.patch_path
            watch_path = patch_inputs.watch_path
            schema_path = patch_inputs.schema_path
            validator_command = patch_inputs.validator_command
            validator_mailbox_id = patch_inputs.validator_mailbox_id
            if not reusing_checkpoint or waking:
                _clear_stale_patch(workspace, remote_stage)
                _clear_stale_watch(workspace, remote_stage)
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
            read_dirs = _chat_read_dirs(
                context,
                remote_stage,
                service,
                execution_machine.alias,
            )
            write_dirs = _work_write_dirs(
                context,
                service,
                execution_machine.alias,
                remote=remote_stage is not None,
            )
            token = _task_token(execution)
            skill_selection = service.resolve_skill_selection(request)
            skill_pointers = stage_skill_selection(
                skill_selection,
                local_stage=local_stage,
                remote_stage=remote_stage,
                label=skill_bundle_label(skill_selection),
                reuse_existing=True,
            )
            repositories = [
                {"alias": item.alias, "host": item.host, "path": item.path}
                for item in context.repositories
            ]
            episode_context_baseline: dict[str, object] | None = None
            wake_episode = None
            context_replacement: dict[str, object] | None = None
            loop_control_path: str | None = None
            watcher_state_path: str | None = None
            if request.patch_kind == "experiment_loop":
                control_node = context.node
                if (
                    control_node is None
                    or control_node.get("id") != request.control_node_id
                    or control_node.get("type") != "experiment"
                ):
                    raise ValueError("Experiment-loop work no longer resolves to its Experiment.")
                loop_control_path, watcher_state_path = await stage_experiment_loop_context(
                    service,
                    request,
                    execution,
                    local_stage,
                    remote_stage,
                    token=token,
                    continuation=continuation,
                )
                assert execution is not None
                ontology = service.history.state().ontology.model_dump(mode="json")
                episode_context_baseline = prepare_experiment_episode_context_candidate(
                    execution,
                    experiment_episode_context_values(
                        ontology_extensions=context.ontology_extensions,
                        ontology=ontology,
                        repositories=repositories,
                        skill_pointers=skill_pointers,
                    ),
                )
                if waking:
                    if not request.control_episode_id or request.control_invocation is None:
                        raise ValueError("Experiment-loop wake is missing its episode invocation.")
                    wake_episode = execution.store.experiment_episode(request.control_episode_id)
                    if wake_episode is None or not wake_episode.session_bound:
                        raise ValueError(
                            "Experiment-loop wake has no committed episode session to continue."
                        )
                    if (
                        wake_episode.native_session_id != request.session_id
                        or wake_episode.stage_host != execution.stage_host
                        or wake_episode.stage_root != execution.stage_root
                    ):
                        raise ValueError(
                            "Experiment-loop wake does not match its committed native session and "
                            "exact stage."
                        )
                    if wake_episode.last_turn_invocation != request.control_invocation - 1:
                        raise ValueError(
                            "Experiment-loop wake does not immediately follow the episode's last "
                            "successful turn."
                        )
                    if not wake_episode.last_graph_result:
                        raise ValueError(
                            "Experiment-loop wake cannot confirm the preceding graph handoff."
                        )
                    context_replacement = _chat_context_delta(
                        wake_episode.context_baseline,
                        episode_context_baseline,
                    )
            if reusing_checkpoint and not request.session_id:
                raise ValueError(
                    "The continued Work turn has no native agent session; retry it from a clean "
                    "attempt instead."
                )
            if resuming:
                assert execution is not None
                original_contract_path = _parent_task_contract_path(
                    execution, local_stage, remote_stage
                )
                base_contract_path = original_contract_path
                if request.patch_kind == "experiment_loop":
                    if not loop_control_path:
                        raise ValueError("Experiment-loop Resume is missing fresh loop control.")
                    contract = experiment_loop_continuation_contract(
                        original_contract_path=original_contract_path,
                        mode="resume",
                        loop_control_path=loop_control_path,
                        patch_path=patch_path,
                        watch_path=watch_path,
                        output_schema_path=schema_path,
                        validator_command=validator_command,
                    )
                else:
                    contract = PromptFactory.continuation_task_contract(
                        original_contract_path=original_contract_path,
                        mode="resume",
                        patch_path=patch_path,
                        validator_command=validator_command,
                    )
                contract_path, prompt = _stage_task_contract(
                    local_stage,
                    remote_stage,
                    f"task-{token}-resume.md",
                    contract,
                    execution=execution,
                    role="work_resume",
                )
            elif waking:
                if (
                    wake_episode is None
                    or not request.control_node_id
                    or request.control_invocation is None
                    or request.control_invocation_ceiling is None
                    or not loop_control_path
                    or not watcher_state_path
                ):
                    raise ValueError("Experiment-loop wake inputs are incomplete after staging.")
                contract = experiment_loop_wake_message(
                    focused_experiment_id=request.control_node_id,
                    invocation=request.control_invocation,
                    invocation_ceiling=request.control_invocation_ceiling,
                    previous_graph_result=wake_episode.last_graph_result or "",
                    previous_watcher_ids=wake_episode.last_watcher_ids,
                    delivered_watcher_ids=request.watcher_ids,
                    loop_control_path=loop_control_path,
                    watcher_state_path=watcher_state_path,
                    graph_path=context.graph_path,
                    research_path=context.research_md_path,
                    patch_path=patch_path,
                    watch_path=watch_path,
                    output_schema_path=schema_path,
                    validator_command=validator_command,
                    context_replacement=context_replacement,
                )
                contract_path, prompt = _stage_task_contract(
                    local_stage,
                    remote_stage,
                    f"task-{token}-watcher-wake.md",
                    contract,
                    execution=execution,
                    role="experiment_loop_wake",
                )
                base_contract_path = contract_path
            else:
                assert request.message is not None
                focused_node_id = str(context.node["id"]) if context.node else None
                retry_diagnostics_path = (
                    _stage_json_task_input(
                        local_stage,
                        remote_stage,
                        f"task-{token}-retry-diagnostics.json",
                        {"prior_attempt_diagnostics": list(execution.retry_feedback)},
                    )
                    if execution is not None
                    and not uses_master_protocol
                    and (execution.retry_feedback or retry_attempt)
                    else None
                )
                # A retry that still holds its native session already has the contract in the
                # conversation; it gets a follow-up naming what changed, not a rebuilt contract.
                resumed_retry = retrying and reusing_checkpoint
                loop_retry = request.patch_kind == "experiment_loop" and retrying
                explicit_contract = (
                    not uses_master_protocol and not resumed_retry and not loop_retry
                )
                current_contract_path = None
                current_prompt = None
                if explicit_contract:
                    human_request_path = _stage_task_input(
                        local_stage,
                        remote_stage,
                        f"task-{token}-human-request.txt",
                        request.message,
                    )
                    if request.patch_kind == "experiment_loop":
                        if (
                            not request.control_node_id
                            or not loop_control_path
                            or not watcher_state_path
                        ):
                            raise ValueError(
                                "Experiment-loop contract inputs are incomplete after staging."
                            )
                        contract = experiment_loop_task_contract(
                            project_name=context.project_name,
                            ontology_path=f"{context.graph_path}#ontology",
                            ontology_extensions=context.ontology_extensions,
                            graph_path=context.graph_path,
                            research_path=context.research_md_path,
                            focused_experiment_id=request.control_node_id,
                            repositories=repositories,
                            introduction_path=context.introduction_path,
                            human_request_path=human_request_path,
                            loop_control_path=loop_control_path,
                            watcher_state_path=watcher_state_path,
                            patch_path=patch_path,
                            watch_path=watch_path,
                            artifact_path=str(artifact_directory),
                            output_schema_path=schema_path,
                            validator_command=validator_command,
                            skill_pointers=skill_pointers,
                        )
                    else:
                        contract = PromptFactory.work_task_contract(
                            project_name=context.project_name,
                            ontology_path=f"{context.graph_path}#ontology",
                            ontology_extensions=context.ontology_extensions,
                            graph_path=context.graph_path,
                            research_path=context.research_md_path,
                            focused_node_id=focused_node_id,
                            repositories=repositories,
                            introduction_path=context.introduction_path,
                            human_request_path=human_request_path,
                            patch_path=patch_path,
                            artifact_path=str(artifact_directory),
                            output_schema_path=schema_path,
                            retry_diagnostics_path=retry_diagnostics_path,
                            watch_path=watch_path,
                            validator_command=validator_command,
                            skill_pointers=skill_pointers,
                        )
                    current_contract_path, current_prompt = _stage_task_contract(
                        local_stage,
                        remote_stage,
                        f"task-{token}-{'base' if retry_attempt else 'initial'}.md",
                        contract,
                        execution=execution,
                        role="work_retry_base" if retry_attempt else "work",
                    )
                    base_contract_path = current_contract_path
                elif uses_master_protocol:
                    master_context = PromptFactory.chat_master_context(
                        project_name=context.project_name,
                        ontology_path=f"{context.graph_path}#ontology",
                        ontology_extensions=context.ontology_extensions,
                        graph_path=context.graph_path,
                        research_path=context.research_md_path,
                        graph_revision=context.graph_revision,
                        focused_node_id=focused_node_id,
                        focused_node=context.node,
                        focused_relations=[
                            item.model_dump(mode="json") for item in context.relations
                        ],
                        repositories=repositories,
                        introduction_path=context.introduction_path,
                        patch_path=patch_path,
                        workspace_path=str(workspace),
                        output_schema_path=schema_path,
                        validator_command=validator_command,
                        watch_path=watch_path,
                        skill_pointers=skill_pointers,
                    )
                    stable_prompt_values: dict[str, object] = {
                        "project": {"name": context.project_name},
                        "settings": {
                            "provider": request.provider,
                            "model": request.model,
                            "reasoning": request.reasoning,
                            "run_on": request.run_on,
                        },
                        "current": {
                            "ontology_path": f"{context.graph_path}#ontology",
                            "graph_revision": context.graph_revision,
                            "graph_path": context.graph_path,
                            "research_path": context.research_md_path,
                            "focused_node_id": focused_node_id,
                            "introduction_path": context.introduction_path,
                        },
                        "repositories": repositories,
                        "skills": {"pointers": skill_pointers},
                        "patch": {
                            "path": patch_path,
                            "watch_path": watch_path,
                            "schema_path": schema_path,
                            "validator_command": validator_command,
                            "validator_mailbox_id": validator_mailbox_id,
                        },
                        "workspace": {"path": str(workspace)},
                    }
                    prompt, retained_master_path = _prepare_work_chat_prompt(
                        execution,
                        request,
                        local_stage=local_stage,
                        remote_stage=remote_stage,
                        artifact_path=str(artifact_directory),
                        master_context=master_context,
                        stable_values=stable_prompt_values,
                    )
                    contract_path = retained_master_path
                    base_contract_path = retained_master_path

                if retrying:
                    assert execution is not None
                    original_contract_path = _parent_task_contract_path(
                        execution, local_stage, remote_stage
                    )
                    if resumed_retry:
                        base_contract_path = original_contract_path
                    if request.patch_kind == "experiment_loop":
                        if not loop_control_path or not retry_diagnostics_path:
                            raise ValueError(
                                "Experiment-loop Retry is missing fresh control or diagnostics."
                            )
                        retry_contract = experiment_loop_continuation_contract(
                            original_contract_path=original_contract_path,
                            mode="retry",
                            loop_control_path=loop_control_path,
                            patch_path=patch_path,
                            watch_path=watch_path,
                            output_schema_path=schema_path,
                            validator_command=validator_command,
                            diagnostics_path=retry_diagnostics_path,
                        )
                    else:
                        retry_contract = PromptFactory.continuation_task_contract(
                            original_contract_path=original_contract_path,
                            current_contract_path=current_contract_path,
                            diagnostics_path=retry_diagnostics_path,
                            patch_path=patch_path,
                            watch_path=watch_path,
                            mode="retry",
                            validator_command=validator_command,
                            output_schema_path=schema_path if resumed_retry else None,
                            skill_pointers=skill_pointers if resumed_retry else None,
                        )
                    contract_path, prompt = _stage_task_contract(
                        local_stage,
                        remote_stage,
                        f"task-{token}-retry.md",
                        retry_contract,
                        execution=execution,
                        role="work_retry",
                    )
                elif explicit_contract:
                    contract_path, prompt = current_contract_path, current_prompt

            retry_patch_digest: str | None = None
            retry_watch_digest: str | None = None
            if retrying:
                assert execution is not None
                predecessor_patch = _read_chat_patch(workspace, remote_stage)
                predecessor_watch = _read_watch_request(workspace, remote_stage)
                retry_patch_digest = (
                    hashlib.sha256(predecessor_patch.encode("utf-8")).hexdigest()
                    if predecessor_patch is not None
                    else None
                )
                retry_watch_digest = (
                    hashlib.sha256(predecessor_watch.encode("utf-8")).hexdigest()
                    if predecessor_watch is not None
                    else None
                )
                execution.store.record_agent_task_receipt(
                    execution.operation_id,
                    "retry_deliverable_baseline",
                    {
                        "patch_sha256": retry_patch_digest,
                        "watch_sha256": retry_watch_digest,
                    },
                    tier="diagnostic",
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
                "mode": "work",
                "capability": "work_auto",
                "network_access": True,
                "launch_kind": (
                    "retry"
                    if retry_attempt
                    else "resume"
                    if resuming
                    else "watcher_wake"
                    if waking
                    else "initial"
                ),
                "write_directory_count": len(write_dirs),
                "canonical_state_boundary": "prompt_only",
            },
        )
        try:
            async with aclosing(
                _stream_work_agent_events(
                    service,
                    launcher,
                    request,
                    prompt,
                    workspace=workspace,
                    session_id=request.session_id,
                    read_dirs=read_dirs,
                    write_dirs=write_dirs,
                    execution_host=execution_host,
                    execution=execution,
                    remote_stage=remote_stage,
                    capability="work_auto",
                    outcome=outcome,
                    binary=provider_binary,
                    mailbox_id=validator_mailbox_id,
                    validator_budget=validator_budget,
                    run_truth_scope=context.run_truth_scope,
                    patch_kind=request.patch_kind,
                    control_node_id=request.control_node_id,
                    control_decision_bundle=request.control_decision_bundle,
                )
            ) as stream:
                async for frame in stream:
                    yield frame
        except Exception:
            outcome.failed = True
            raise

        answer = "\n\n".join(item.strip() for item in outcome.answers if item.strip()).strip()
        if not outcome.completed:
            if outcome.failed or outcome.paused:
                return
            outcome.failed = True
            yield _sse(AgentEvent(event="error", text=f"{request.provider} produced no result."))
            return
        if not answer:
            yield _sse(
                AgentEvent(event="error", text=f"{request.provider} finished without answering.")
            )
            return
        if waking and (
            wake_episode is None or outcome.session_id != wake_episode.native_session_id
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

        if uses_master_protocol:
            _commit_chat_prompt_state(execution, request, outcome.session_id)

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

        graph_update: GraphUpdateResult
        correction_rounds = 0
        native_session_id = outcome.session_id
        try:
            patch_text = _read_chat_patch(workspace, remote_stage)
        except (OSError, StateUnavailable, ValueError) as exc:
            patch_text = None
            failure = _WorkPatchFailure(
                f"The agent wrote a patch file that could not be read: {exc}",
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
        deferred_loop_patch = request.patch_kind == "experiment_loop" and patch_text is not None
        if deferred_loop_patch:
            assert patch_text is not None
            if execution is not None:
                execution.store.record_agent_task_patch_output(execution.operation_id, patch_text)
                execution.store.record_agent_task_receipt(
                    execution.operation_id,
                    "patch_retained",
                    {
                        "byte_length": len(patch_text.encode("utf-8")),
                        "file_name": "patch.json",
                    },
                    tier="diagnostic",
                )
            patch_text = None
        if request.patch_kind == "experiment_loop":
            # Loop graph admission is a joint Patch/watch handoff. Nothing in
            # the generic pre-handoff path may validate-correct-and-apply it.
            failure = None
        if patch_text is None and failure is None:
            graph_update = GraphUpdateResult(status="none")
        else:
            while True:
                if patch_text is not None:
                    try:
                        result, failure = _apply_work_patch(
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
                                text=(
                                    "Paused while waiting for canonical state. The operational "
                                    "answer and retained patch are preserved."
                                ),
                            )
                        )
                        return
                    if result is not None:
                        graph_update = result.model_copy(
                            update={"correction_rounds": correction_rounds}
                        )
                        break
                assert failure is not None
                if (
                    not failure.correctable
                    or correction_rounds >= _MAX_CORRECTION_ROUNDS
                    or not native_session_id
                ):
                    repairable = _work_graph_repairable(
                        execution,
                        native_session_id,
                        failure,
                    )
                    graph_update = GraphUpdateResult(
                        status="rejected",
                        change_summary=list(failure.change_summary),
                        proposal_ids=list(failure.proposal_ids),
                        validation_messages=_bounded_graph_messages(failure.message),
                        correction_rounds=correction_rounds,
                        repairable=repairable,
                    )
                    _record_work_graph_rejection(execution, graph_update)
                    break

                correction_rounds += 1
                if execution is not None:
                    execution.store.record_agent_task_receipt(
                        execution.operation_id,
                        "patch_correction_requested",
                        {"round": correction_rounds, "problem": failure.message[:400]},
                        tier="diagnostic",
                    )
                    execution.store.update_agent_task_message(
                        execution.operation_id,
                        "Correcting graph update.",
                        phase="correcting",
                        event=True,
                    )
                diagnostics_path = _stage_json_task_input(
                    local_stage,
                    remote_stage,
                    f"task-{token}-work-correction-{correction_rounds}.json",
                    {"kind": "work", "problem": failure.message},
                )
                correction_contract = PromptFactory.continuation_task_contract(
                    original_contract_path=base_contract_path,
                    mode="work_patch_correction",
                    patch_path=patch_path,
                    diagnostics_path=diagnostics_path,
                    validator_command=validator_command,
                )
                correction_path, correction_prompt = _stage_task_contract(
                    local_stage,
                    remote_stage,
                    f"task-{token}-work-correction-{correction_rounds}.md",
                    correction_contract,
                    execution=execution,
                    role=f"work_patch_correction_{correction_rounds}",
                )
                pre_launch_digest = _existing_patch_digest(workspace, remote_stage)
                _record_agent_launch_receipt(
                    execution,
                    request,
                    prompt=correction_prompt,
                    contract_path=correction_path,
                    remote=bool(execution_host),
                    resumed=True,
                    continuation="graph_correction",
                    extra={
                        "surface": surface,
                        "mode": "work",
                        "capability": "work_auto",
                        "network_access": True,
                        "launch_kind": "graph_correction",
                        "correction_round": correction_rounds,
                        "write_directory_count": len(write_dirs),
                        "canonical_state_boundary": "prompt_only",
                    },
                )
                correction_outcome = _ProviderOutcome(session_id=native_session_id)
                correction_error: str | None = None
                async with aclosing(
                    _stream_work_agent_events(
                        service,
                        launcher,
                        request,
                        correction_prompt,
                        workspace=workspace,
                        session_id=native_session_id,
                        read_dirs=read_dirs,
                        write_dirs=write_dirs,
                        execution_host=execution_host,
                        execution=execution,
                        remote_stage=remote_stage,
                        capability="work_auto",
                        outcome=correction_outcome,
                        binary=provider_binary,
                        mailbox_id=validator_mailbox_id,
                        validator_budget=validator_budget,
                        run_truth_scope=context.run_truth_scope,
                        patch_kind=request.patch_kind,
                        control_node_id=request.control_node_id,
                        control_decision_bundle=request.control_decision_bundle,
                    )
                ) as stream:
                    async for frame in stream:
                        event = AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
                        if event.event == "error":
                            correction_error = event.text or "Patch correction failed."
                            continue
                        yield frame
                native_session_id = correction_outcome.session_id or native_session_id
                if correction_outcome.paused:
                    return
                if correction_error or not correction_outcome.completed:
                    detail = (
                        correction_error or f"{request.provider} produced no correction result."
                    )
                    failure = _WorkPatchFailure(
                        detail,
                        correctable=True,
                        change_summary=failure.change_summary,
                        proposal_ids=failure.proposal_ids,
                    )
                    patch_text = None
                    correction_rounds = _MAX_CORRECTION_ROUNDS
                    continue
                try:
                    corrected = _read_chat_patch(workspace, remote_stage)
                except (OSError, StateUnavailable, ValueError) as exc:
                    corrected = None
                    failure = _WorkPatchFailure(
                        f"The corrected patch could not be read: {exc}",
                        correctable=True,
                        change_summary=failure.change_summary,
                        proposal_ids=failure.proposal_ids,
                    )
                if corrected is None:
                    failure = _WorkPatchFailure(
                        "The correction completed without writing patch.json.",
                        correctable=True,
                        change_summary=failure.change_summary,
                        proposal_ids=failure.proposal_ids,
                    )
                    patch_text = None
                    continue
                if (
                    pre_launch_digest is not None
                    and hashlib.sha256(corrected.encode("utf-8")).hexdigest() == pre_launch_digest
                ):
                    failure = _WorkPatchFailure(
                        f"{failure.message} The correction left patch.json byte-identical; "
                        "rewrite it with the required changes.",
                        correctable=True,
                        change_summary=failure.change_summary,
                        proposal_ids=failure.proposal_ids,
                    )
                    # Revalidating it would only reproduce the original
                    # diagnostic and drop the one detail this round adds: that
                    # the agent never rewrote the file.
                    patch_text = None
                    continue
                patch_text = corrected

        watch_correction_rounds = 0
        max_watch_corrections = 1 if request.patch_kind == "experiment_loop" else 2
        loop_watch_empty = False
        pending_loop_handoff = None
        accepted_loop_watcher_ids: list[str] = []
        try:
            watch_text = _read_watch_request(workspace, remote_stage)
        except ValueError as exc:
            watch_text = None
            watch_problem = f"The watcher request could not be read: {exc}"
            watch_correctable = True
        except (OSError, StateUnavailable) as exc:
            watch_text = None
            watch_problem = f"The watcher request could not be read: {exc}"
            watch_correctable = False
        else:
            watch_problem = None
            watch_correctable = True
        if _retry_deliverable_is_unchanged(
            execution,
            filename="watch.json",
            predecessor_digest=retry_watch_digest,
            current_text=watch_text,
        ):
            watch_text = None
        if request.patch_kind == "experiment_loop" and watch_text is None and watch_problem is None:
            watch_problem = (
                "Experiment-loop work must write watch.json: use a non-empty watcher list for "
                "detached work, or [] only after confirming none remains."
            )

        while watch_text is not None or watch_problem is not None:
            if watch_text is not None:
                try:
                    if execution is None:
                        raise ValueError("Watcher arming requires a durable originating operation.")
                    origin_task = execution.store.agent_task(execution.operation_id)
                    if origin_task is None:
                        raise ValueError("The originating Work operation is no longer available.")
                    raw_watch = json.loads(watch_text)
                    if request.patch_kind == "experiment_loop" and raw_watch == []:
                        exit_patch_text = _read_chat_patch(workspace, remote_stage)
                        if not request.control_node_id or not patch_explicitly_exits(
                            exit_patch_text, request.control_node_id
                        ):
                            raise ValueError(
                                "An empty Experiment-loop watch.json requires patch.json to "
                                "explicitly record success, a Proposal, or a same-Patch Blocker."
                            )
                    specs = (
                        []
                        if request.patch_kind == "experiment_loop" and raw_watch == []
                        else parse_watch_json(watch_text)
                    )
                    binding = WatcherBinding(
                        project_id=origin_task.project_id,
                        origin_operation_id=(
                            root_experiment_loop_operation_id(execution)
                            if request.patch_kind == "experiment_loop"
                            else execution.operation_id
                        ),
                        origin_task_kind=surface,
                        chat_id=request.chat_id or "",
                        node_id=request.node_id,
                        execution_host=execution_host,
                        continuation=WatcherContinuation(
                            provider=request.provider or "",
                            model=request.model,
                            reasoning=request.reasoning,
                            run_on=request.run_on or "",
                            run_truth_scope=context.run_truth_scope,
                            patch_kind=request.patch_kind,
                            control_node_id=request.control_node_id,
                            control_revision=request.control_revision,
                            control_episode_id=request.control_episode_id,
                            control_invocation=request.control_invocation,
                            control_invocation_ceiling=request.control_invocation_ceiling,
                            control_decision_bundle=[
                                item.model_dump(mode="json")
                                for item in request.control_decision_bundle
                            ],
                            control_completion_criteria=request.control_completion_criteria,
                            workflow_ids=skill_selection.workflow_ids,
                            skill_ids=skill_selection.skill_ids,
                            invoked_workflow_ids=request.invoked_workflow_ids,
                            invoked_skill_ids=request.invoked_skill_ids,
                            resolved_skill_packages=skill_selection.resolved_skill_packages,
                        ),
                    )
                    if specs and request.patch_kind == "experiment_loop":
                        check_results = await asyncio.to_thread(
                            validate_watch_specs,
                            specs,
                            execution_host,
                        )
                        pending_loop_handoff = (specs, check_results, binding)
                        armed = []
                    else:
                        armed = (
                            await asyncio.to_thread(
                                arm_watchers,
                                execution.store,
                                specs,
                                binding,
                            )
                            if specs
                            else []
                        )
                except WatcherInitialCheckError as exc:
                    watch_problem = str(exc)
                    watch_correctable = True
                except ValueError as exc:
                    watch_problem = str(exc)
                    watch_correctable = True
                except (OSError, StateUnavailable) as exc:
                    watch_problem = str(exc)
                    watch_correctable = False
                else:
                    loop_watch_empty = request.patch_kind == "experiment_loop" and not specs
                    if request.patch_kind != "experiment_loop":
                        execution.store.record_agent_task_receipt(
                            execution.operation_id,
                            "watchers_armed",
                            {
                                "watcher_ids": [item.watcher_id for item in armed],
                                "count": len(armed),
                                "correction_rounds": watch_correction_rounds,
                            },
                        )
                    watch_problem = None
                    break

            if watch_problem is None:
                break
            if (
                not watch_correctable
                or watch_correction_rounds >= max_watch_corrections
                or not native_session_id
            ):
                if execution is not None:
                    execution.store.record_agent_task_receipt(
                        execution.operation_id,
                        "watcher_handoff_rejected",
                        {
                            "problem": watch_problem[:1600],
                            "correction_rounds": watch_correction_rounds,
                        },
                        tier="diagnostic",
                    )
                    execution.store.record_agent_task_event(
                        execution.operation_id,
                        f"Watcher handoff was not armed: {watch_problem}",
                        level="warning",
                    )
                if request.patch_kind == "experiment_loop":
                    yield _sse(
                        AgentEvent(
                            event="error",
                            text=f"Experiment-loop watcher handoff failed: {watch_problem}",
                        )
                    )
                    return
                break

            watch_correction_rounds += 1
            assert execution is not None
            execution.store.record_agent_task_receipt(
                execution.operation_id,
                "watcher_correction_requested",
                {"round": watch_correction_rounds, "problem": watch_problem[:400]},
                tier="diagnostic",
            )
            execution.store.update_agent_task_message(
                execution.operation_id,
                "Correcting watcher handoff.",
                phase="correcting",
                event=True,
            )
            diagnostics_path = _stage_json_task_input(
                local_stage,
                remote_stage,
                f"task-{token}-watch-correction-{watch_correction_rounds}.json",
                {"problem": watch_problem},
            )
            correction_contract = (
                experiment_loop_watcher_correction_contract(
                    original_contract_path=base_contract_path,
                    diagnostics_path=diagnostics_path,
                    watch_path=watch_path,
                    patch_path=patch_path,
                    output_schema_path=schema_path,
                    validator_command=validator_command,
                )
                if request.patch_kind == "experiment_loop"
                else PromptFactory.continuation_task_contract(
                    original_contract_path=base_contract_path,
                    mode="watch_correction",
                    diagnostics_path=diagnostics_path,
                    watch_path=watch_path,
                )
            )
            correction_path, correction_prompt = _stage_task_contract(
                local_stage,
                remote_stage,
                f"task-{token}-watch-correction-{watch_correction_rounds}.md",
                correction_contract,
                execution=execution,
                role=f"watch_correction_{watch_correction_rounds}",
            )
            pre_launch_digest = _existing_watch_digest(workspace, remote_stage)
            _record_agent_launch_receipt(
                execution,
                request,
                prompt=correction_prompt,
                contract_path=correction_path,
                remote=bool(execution_host),
                resumed=True,
                continuation="watch_correction",
                extra={
                    "surface": surface,
                    "mode": "work",
                    "capability": "work_auto",
                    "network_access": True,
                    "launch_kind": "watch_correction",
                    "correction_round": watch_correction_rounds,
                    "write_directory_count": len(write_dirs),
                    "canonical_state_boundary": "prompt_only",
                },
            )
            correction_outcome = _ProviderOutcome(session_id=native_session_id)
            correction_error: str | None = None
            correction_stream = (
                _stream_work_agent_events(
                    service,
                    launcher,
                    request,
                    correction_prompt,
                    workspace=workspace,
                    session_id=native_session_id,
                    read_dirs=read_dirs,
                    write_dirs=write_dirs,
                    execution_host=execution_host,
                    execution=execution,
                    remote_stage=remote_stage,
                    capability="work_auto",
                    outcome=correction_outcome,
                    binary=provider_binary,
                    mailbox_id=validator_mailbox_id,
                    validator_budget=validator_budget,
                    run_truth_scope=context.run_truth_scope,
                    patch_kind=request.patch_kind,
                    control_node_id=request.control_node_id,
                    control_decision_bundle=request.control_decision_bundle,
                )
                if request.patch_kind == "experiment_loop"
                else _stream_agent_events(
                    launcher,
                    request,
                    correction_prompt,
                    workspace=workspace,
                    session_id=native_session_id,
                    read_dirs=read_dirs,
                    write_dirs=write_dirs,
                    execution_host=execution_host,
                    execution=execution,
                    remote_stage=remote_stage,
                    capability="work_auto",
                    outcome=correction_outcome,
                    binary=provider_binary,
                )
            )
            async with aclosing(correction_stream) as stream:
                async for frame in stream:
                    event = AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
                    if event.event == "error":
                        correction_error = event.text or "Watcher correction failed."
                    elif event.event not in {"answer", "done"}:
                        yield frame
            native_session_id = correction_outcome.session_id or native_session_id
            if correction_outcome.paused:
                return
            if correction_error or not correction_outcome.completed:
                watch_problem = correction_error or (
                    f"{request.provider} produced no watcher correction result."
                )
                watch_text = None
                watch_correction_rounds = max_watch_corrections
                continue
            try:
                corrected = _read_watch_request(workspace, remote_stage)
            except (OSError, StateUnavailable, ValueError) as exc:
                corrected = None
                watch_problem = f"The corrected watcher request could not be read: {exc}"
            if corrected is None:
                watch_problem = "The correction completed without writing watch.json."
                watch_text = None
                continue
            if (
                pre_launch_digest is not None
                and hashlib.sha256(corrected.encode("utf-8")).hexdigest() == pre_launch_digest
            ):
                watch_problem = (
                    f"{watch_problem} The correction left watch.json byte-identical; rewrite it "
                    "with the required changes."
                )
                watch_text = None
                continue
            watch_text = corrected
            watch_problem = None

        if request.patch_kind == "experiment_loop":
            try:
                final_patch_text = _read_chat_patch(workspace, remote_stage)
            except (OSError, StateUnavailable, ValueError) as exc:
                yield _sse(
                    AgentEvent(
                        event="error",
                        text=f"The final Experiment-loop patch could not be read: {exc}",
                    )
                )
                return
            if final_patch_text is None:
                graph_update = GraphUpdateResult(status="none")
            else:
                loop_patch_correction_rounds = 0
                while True:
                    if loop_watch_empty and (
                        not request.control_node_id
                        or not patch_explicitly_exits(final_patch_text, request.control_node_id)
                    ):
                        final_failure = _WorkPatchFailure(
                            "An empty watch.json requires this Patch to retain an explicit success, "
                            "Proposal, or same-Patch Blocker.",
                            correctable=True,
                        )
                        final_result = None
                    else:
                        try:
                            final_result, final_failure = _apply_work_patch(
                                service,
                                execution,
                                final_patch_text,
                                run_truth_scope=context.run_truth_scope,
                                patch_kind=request.patch_kind,
                                control_node_id=request.control_node_id,
                                control_decision_bundle=request.control_decision_bundle,
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
                            return
                    if final_result is not None:
                        graph_update = final_result.model_copy(
                            update={"correction_rounds": loop_patch_correction_rounds}
                        )
                        break
                    assert final_failure is not None
                    if (
                        not final_failure.correctable
                        or loop_patch_correction_rounds >= _MAX_CORRECTION_ROUNDS
                        or not native_session_id
                    ):
                        if loop_watch_empty:
                            yield _sse(
                                AgentEvent(
                                    event="error",
                                    text=(
                                        "Experiment-loop Patch could not be validated after its "
                                        f"watcher handoff: {final_failure.message}"
                                    ),
                                )
                            )
                            return
                        repairable = _work_graph_repairable(
                            execution,
                            native_session_id,
                            final_failure,
                        )
                        graph_update = GraphUpdateResult(
                            status="rejected",
                            change_summary=list(final_failure.change_summary),
                            proposal_ids=list(final_failure.proposal_ids),
                            validation_messages=_bounded_graph_messages(final_failure.message),
                            correction_rounds=loop_patch_correction_rounds,
                            repairable=repairable,
                        )
                        _record_work_graph_rejection(execution, graph_update)
                        break

                    loop_patch_correction_rounds += 1
                    assert execution is not None
                    execution.store.record_agent_task_receipt(
                        execution.operation_id,
                        "patch_correction_requested",
                        {
                            "round": loop_patch_correction_rounds,
                            "problem": final_failure.message[:400],
                        },
                        tier="diagnostic",
                    )
                    diagnostics_path = _stage_json_task_input(
                        local_stage,
                        remote_stage,
                        f"task-{token}-loop-patch-correction-{loop_patch_correction_rounds}.json",
                        {"kind": "experiment_loop", "problem": final_failure.message},
                    )
                    correction_contract = experiment_loop_patch_correction_contract(
                        original_contract_path=base_contract_path,
                        diagnostics_path=diagnostics_path,
                        patch_path=patch_path,
                        watch_path=watch_path,
                        validator_command=validator_command,
                    )
                    correction_path, correction_prompt = _stage_task_contract(
                        local_stage,
                        remote_stage,
                        f"task-{token}-loop-patch-correction-{loop_patch_correction_rounds}.md",
                        correction_contract,
                        execution=execution,
                        role=f"experiment_loop_patch_correction_{loop_patch_correction_rounds}",
                    )
                    pre_launch_digest = _existing_patch_digest(workspace, remote_stage)
                    _record_agent_launch_receipt(
                        execution,
                        request,
                        prompt=correction_prompt,
                        contract_path=correction_path,
                        remote=bool(execution_host),
                        resumed=True,
                        continuation="graph_correction",
                        extra={
                            "surface": surface,
                            "mode": "work",
                            "capability": "work_auto",
                            "network_access": True,
                            "launch_kind": "graph_correction",
                            "correction_round": loop_patch_correction_rounds,
                            "write_directory_count": len(write_dirs),
                            "canonical_state_boundary": "prompt_only",
                        },
                    )
                    correction_outcome = _ProviderOutcome(session_id=native_session_id)
                    async with aclosing(
                        _stream_work_agent_events(
                            service,
                            launcher,
                            request,
                            correction_prompt,
                            workspace=workspace,
                            session_id=native_session_id,
                            read_dirs=read_dirs,
                            write_dirs=write_dirs,
                            execution_host=execution_host,
                            execution=execution,
                            remote_stage=remote_stage,
                            capability="work_auto",
                            outcome=correction_outcome,
                            binary=provider_binary,
                            mailbox_id=validator_mailbox_id,
                            validator_budget=validator_budget,
                            run_truth_scope=context.run_truth_scope,
                            patch_kind=request.patch_kind,
                            control_node_id=request.control_node_id,
                            control_decision_bundle=request.control_decision_bundle,
                        )
                    ) as stream:
                        async for frame in stream:
                            yield frame
                    native_session_id = correction_outcome.session_id or native_session_id
                    if correction_outcome.paused:
                        return
                    if not correction_outcome.completed:
                        final_failure = _WorkPatchFailure(
                            f"{request.provider} produced no Patch correction result.",
                            correctable=True,
                        )
                        loop_patch_correction_rounds = _MAX_CORRECTION_ROUNDS
                        continue
                    corrected = _read_chat_patch(workspace, remote_stage)
                    if corrected is None or (
                        pre_launch_digest is not None
                        and hashlib.sha256(corrected.encode("utf-8")).hexdigest()
                        == pre_launch_digest
                    ):
                        final_failure = _WorkPatchFailure(
                            "The loop Patch correction did not rewrite patch.json.",
                            correctable=True,
                        )
                        final_patch_text = ""
                        continue
                    final_patch_text = corrected

            if pending_loop_handoff is not None:
                assert execution is not None
                specs, check_results, binding = pending_loop_handoff
                try:
                    armed = await asyncio.to_thread(
                        persist_experiment_watchers_idempotently,
                        execution,
                        specs,
                        check_results,
                        binding,
                    )
                except ValueError as exc:
                    yield _sse(
                        AgentEvent(
                            event="error",
                            text=f"Experiment-loop watcher persistence failed: {exc}",
                        )
                    )
                    return
                execution.store.record_agent_task_receipt(
                    root_experiment_loop_operation_id(execution),
                    "watchers_armed",
                    {
                        "watcher_ids": [item.watcher_id for item in armed],
                        "count": len(armed),
                        "correction_rounds": watch_correction_rounds,
                    },
                )
                accepted_loop_watcher_ids = [item.watcher_id for item in armed]
            if (
                execution is not None
                and request.control_node_id
                and graph_update.status == "applied"
                and patch_explicitly_exits(final_patch_text, request.control_node_id)
            ):
                execution.store.record_agent_task_receipt(
                    root_experiment_loop_operation_id(execution),
                    "experiment_loop_exit",
                    {
                        "control_node_id": request.control_node_id,
                        "episode_id": request.control_episode_id,
                        "invocation": request.control_invocation,
                        "applied_revision": graph_update.applied_revision,
                    },
                )

            if execution is None or episode_context_baseline is None:
                raise ValueError("Experiment-loop handoff lost its durable episode context.")
            commit_experiment_episode_binding(
                execution,
                request,
                native_session_id=native_session_id,
                execution_host=execution_host,
                stage_host=execution.stage_host,
                stage_root=execution.stage_root,
                graph_result=experiment_graph_result_summary(graph_update),
                watcher_ids=accepted_loop_watcher_ids,
                context_baseline=episode_context_baseline,
            )

        if uses_master_protocol:
            try:
                _record_applied_graph_revision(
                    execution,
                    request,
                    outcome.session_id,
                    graph_update.applied_revision,
                )
            except ValueError as exc:
                if execution is not None:
                    execution.store.record_agent_task_event(
                        execution.operation_id,
                        "This turn's own revision could not be absorbed into the session "
                        f"baseline; the next turn may re-announce it: {exc}",
                        level="warning",
                    )
        try:
            _append_chat_exchange(
                service,
                request,
                answer,
                outcome.session_id,
                graph_update.applied_revision,
                graph_update=graph_update,
                execution=execution,
            )
        except (OSError, StateUnavailable, ValueError) as exc:
            if execution is not None:
                execution.store.record_agent_task_event(
                    execution.operation_id,
                    f"The reply was delivered but could not be written to the chat transcript: {exc}",
                    level="warning",
                )
        payload: dict[str, object] = {
            "graph_update": graph_update.model_dump(mode="json"),
        }
        if graph_update.applied_revision is not None:
            payload["applied_revision"] = graph_update.applied_revision
        yield _sse(AgentEvent(event="message", text=json.dumps(payload, separators=(",", ":"))))
        yield _sse(AgentEvent(event="done"))

    finally:
        # There is no per-turn source cleanup; the reusable native-session stage
        # remains available to the normal stage sweeper.
        pass


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
        patch_inputs = _stage_chat_patch_inputs(
            local_stage,
            remote_stage,
            workspace=workspace,
            stage_name=stage_name,
        )
        patch_path = patch_inputs.patch_path
        read_dirs = _chat_read_dirs(
            context,
            remote_stage,
            service,
            execution_machine.alias,
        )
        write_dirs = _work_write_dirs(
            context,
            service,
            execution_machine.alias,
            remote=remote_stage is not None,
        )
        previous = _rejected_graph_update_for_repair(execution)
        original_contract_path = _parent_task_contract_path(execution, local_stage, remote_stage)
        token = _task_token(execution)
        validator_mailbox_id = patch_inputs.validator_mailbox_id
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
    except (OSError, ReplayHalted, StateUnavailable, ValueError) as exc:
        yield _sse(AgentEvent(event="error", text=str(exc)))
        return

    _record_agent_launch_receipt(
        execution,
        request,
        prompt=prompt,
        contract_path=contract_path,
        remote=bool(execution_host),
        resumed=True,
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
    outcome = _ProviderOutcome(session_id=request.session_id)
    async with aclosing(
        _stream_work_agent_events(
            service,
            launcher,
            request,
            prompt,
            workspace=workspace,
            session_id=request.session_id,
            read_dirs=read_dirs,
            write_dirs=write_dirs,
            execution_host=execution_host,
            execution=execution,
            remote_stage=remote_stage,
            capability="work_auto",
            outcome=outcome,
            binary=provider_binary,
            mailbox_id=validator_mailbox_id,
            validator_budget=PatchValidationBudget(),
            run_truth_scope=context.run_truth_scope,
            patch_kind=request.patch_kind,
            control_node_id=request.control_node_id,
            control_decision_bundle=request.control_decision_bundle,
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
    elif (
        request.patch_kind == "experiment_loop"
        and request.control_node_id
        and patch_explicitly_exits(patch_text, request.control_node_id)
    ):
        execution.store.record_agent_task_receipt(
            root_experiment_loop_operation_id(execution),
            "experiment_loop_exit",
            {
                "control_node_id": request.control_node_id,
                "episode_id": request.control_episode_id,
                "invocation": request.control_invocation,
                "applied_revision": graph_update.applied_revision,
            },
        )
    if request.patch_kind == "experiment_loop":
        if not request.control_episode_id:
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
    execution_host: str,
    execution: AgentTaskExecution | None,
    remote_stage: RemoteRunStage | None,
    capability: Literal["work_auto"],
    outcome: _ProviderOutcome,
    binary: str | None,
    mailbox_id: str,
    validator_budget: PatchValidationBudget,
    run_truth_scope: list[str],
    patch_kind: Literal["work", "experiment_loop"],
    control_node_id: str | None,
    control_decision_bundle: list[ExperimentDecisionPin],
) -> AsyncIterator[str]:
    await asyncio.to_thread(
        prepare_patch_validation_mailbox,
        mailbox_id=mailbox_id,
        workspace=workspace,
        remote_stage=remote_stage,
    )
    stop = asyncio.Event()
    mailbox = asyncio.create_task(
        serve_patch_validation_mailbox(
            mailbox_id=mailbox_id,
            workspace=workspace,
            remote_stage=remote_stage,
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
            budget=validator_budget,
        )
    )
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
                execution_host=execution_host,
                execution=execution,
                remote_stage=remote_stage,
                capability=capability,
                outcome=outcome,
                binary=binary,
            )
        ) as stream:
            async for frame in stream:
                yield frame
    finally:
        stop.set()
        try:
            await mailbox
        finally:
            await asyncio.to_thread(
                cleanup_patch_validation_mailbox,
                mailbox_id=mailbox_id,
                workspace=workspace,
                remote_stage=remote_stage,
                execution=execution,
            )


def _prepare_work_patch_candidate(
    service: ProjectService,
    patch_text: str,
    *,
    run_truth_scope: list[str],
    patch_kind: Literal["work", "experiment_loop"],
    control_node_id: str | None,
    control_decision_bundle: list[ExperimentDecisionPin] | None,
    source_operation_id: str | None = None,
) -> _PreparedWorkPatch:
    draft, _ = service.parse_patch_output([patch_text])
    validate_agent_patch_shape(draft)
    patch = prepare_agent_patch(
        draft,
        kind=patch_kind,
        run_truth_scope=run_truth_scope,
        source_operation_id=source_operation_id,
    )
    if patch_kind == "experiment_loop":
        patch = patch.model_copy(
            update={
                "experiment_control_node_id": control_node_id,
                "experiment_decision_bundle": list(control_decision_bundle or ()),
            }
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
    patch_kind: Literal["work", "experiment_loop"],
    control_node_id: str | None,
    control_decision_bundle: list[ExperimentDecisionPin] | None,
    source_operation_id: str | None = None,
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
) -> tuple[GraphUpdateResult | None, _WorkPatchFailure | None]:
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
    source_operation_id = _work_patch_source_operation_id(execution, patch_kind)
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
            if patch_kind == "experiment_loop" and source_operation_id:
                matches = [
                    item
                    for item in service.history.load_patches()
                    if item.source_operation_id == source_operation_id
                    and item.admission == "accepted"
                ]
                if len(matches) > 1:
                    raise ValueError(
                        "Experiment-loop invocation has multiple canonical Patch commits."
                    )
                if matches:
                    canonical_patch = matches[0]
                    if (
                        canonical_patch.kind != "experiment_loop"
                        or canonical_patch.experiment_control_node_id != control_node_id
                    ):
                        raise ValueError(
                            "Experiment-loop invocation source is bound to a different canonical "
                            "Patch."
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
        return None, _WorkPatchFailure(
            detail,
            correctable=True,
            change_summary=change_summary,
            proposal_ids=proposal_ids,
        )
    except (ReplayHalted, StateUnavailable) as exc:
        return None, _WorkPatchFailure(
            str(exc),
            correctable=False,
            change_summary=change_summary,
            proposal_ids=proposal_ids,
        )
    except ValueError as exc:
        return None, _WorkPatchFailure(
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
        if operation.get("op") != "create_proposals":
            continue
        proposals = operation.get("proposals")
        if not isinstance(proposals, list):
            continue
        for proposal in proposals:
            if isinstance(proposal, dict) and isinstance(proposal.get("id"), str):
                proposal_ids.append(proposal["id"])
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
    failure: _WorkPatchFailure,
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
