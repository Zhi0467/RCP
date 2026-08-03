from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
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
    agent_output_schema,
    prepare_agent_patch,
    validate_agent_patch_shape,
    validate_work_patch,
)
from rcp.background import AgentTaskExecution
from rcp.config import AgentSurface
from rcp.control import decision_drift
from rcp.core.models import ExperimentDecisionPin, Patch
from rcp.history import PatchRejected, ReplayHalted
from rcp.limits import WORK_PATCH_SELF_CHECK_TIMEOUT_SECONDS
from rcp.runs.chat import (
    _append_chat_exchange,
    _append_chat_graph_receipt,
    _chat_read_dirs,
    _chat_stage_name,
    _clear_stale_patch,
    _clear_stale_watch,
    _discover_chat_artifacts,
    _existing_watch_digest,
    _logical_chat_turn_operation_id,
    _prepare_local_artifact_directory,
    _read_chat_patch,
    _read_watch_request,
    _record_artifact_discovery_receipt,
    _record_chat_context_receipt,
    _validated_local_chat_resume_stage,
    _validated_remote_chat_resume_stage,
    _work_write_dirs,
)
from rcp.runs.patch_validator import (
    VALIDATOR_CLIENT_SOURCE,
    PatchValidationBudget,
    PatchValidationResult,
    cleanup_patch_validation_mailbox,
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
from rcp.storage import WatcherContinuation
from rcp.transport import RemoteRunStage, StateUnavailable
from rcp.watchers import (
    WatcherBinding,
    WatcherInitialCheckError,
    arm_watchers,
    parse_watch_json,
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
                if not reusing_checkpoint or retrying:
                    context = context.model_copy(
                        update=_stage_context_paths(
                            context, service, remote_stage, execution_machine.alias
                        )
                    )
                workspace = Path(str(remote_stage.workspace))
                patch_path = str(remote_stage.workspace / "patch.json")
                watch_path = str(remote_stage.workspace / "watch.json")
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
                patch_path = str(local_stage / "patch.json")
                watch_path = str(local_stage / "watch.json")
            if not reusing_checkpoint:
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
            validator_mailbox_id = uuid.uuid4().hex
            validator_client_path = _stage_task_input(
                local_stage,
                remote_stage,
                f"task-{token}-validator-client.py",
                VALIDATOR_CLIENT_SOURCE,
            )
            validator_command = shlex.join(
                [
                    "python3",
                    validator_client_path,
                    patch_path,
                    validator_mailbox_id,
                    str(WORK_PATCH_SELF_CHECK_TIMEOUT_SECONDS),
                    str(workspace),
                ]
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
            else:
                assert request.message is not None
                human_request_path = _stage_task_input(
                    local_stage,
                    remote_stage,
                    f"task-{token}-human-request.txt",
                    request.message,
                )
                schema_path = _stage_json_task_input(
                    local_stage,
                    remote_stage,
                    f"task-{token}-patch-schema.json",
                    agent_output_schema(),
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
                control_context_path = None
                if request.patch_kind == "experiment_loop":
                    if not request.control_node_id or request.control_revision is None:
                        raise ValueError("Experiment-loop work is missing its RCP control binding.")
                    control_node = context.node
                    if (
                        control_node is None
                        or control_node.get("id") != request.control_node_id
                        or control_node.get("type") != "experiment"
                    ):
                        raise ValueError(
                            "Experiment-loop work no longer resolves to its Experiment."
                        )
                    attempts = control_node.get("attempts", [])
                    attempt_ceiling = control_node.get("attempt_ceiling", 5)
                    # RCP computes the drift rather than leaving the turn to
                    # notice that its pins no longer match the graph. Reading
                    # canonical state can touch a remote repository, so it stays
                    # off the event loop.
                    drift = await asyncio.to_thread(
                        lambda: decision_drift(
                            service.history.state(),
                            request.control_decision_bundle,
                        )
                    )
                    control_context_path = _stage_json_task_input(
                        local_stage,
                        remote_stage,
                        f"task-{token}-experiment-control.json",
                        {
                            "experiment_id": request.control_node_id,
                            "pinned_graph_revision": request.control_revision,
                            "decision_bundle": [
                                item.model_dump(mode="json")
                                for item in request.control_decision_bundle
                            ],
                            "completion_criteria": request.control_completion_criteria,
                            "decision_drift": [item.model_dump(mode="json") for item in drift],
                            "attempts_used": len(attempts) if isinstance(attempts, list) else 0,
                            "attempt_ceiling": attempt_ceiling,
                            "at_ceiling": (
                                isinstance(attempt_ceiling, int)
                                and isinstance(attempts, list)
                                and len(attempts) >= attempt_ceiling
                            ),
                        },
                    )
                contract = PromptFactory.work_task_contract(
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
                    patch_path=patch_path,
                    artifact_path=str(artifact_directory),
                    output_schema_path=schema_path,
                    retry_diagnostics_path=retry_diagnostics_path,
                    watch_path=watch_path,
                    patch_kind=request.patch_kind,
                    control_context_path=control_context_path,
                    validator_command=validator_command,
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
                        patch_path=patch_path,
                        watch_path=watch_path,
                        mode="retry",
                        validator_command=validator_command,
                    )
                    contract_path, prompt = _stage_task_contract(
                        local_stage,
                        remote_stage,
                        f"task-{token}-retry.md",
                        retry_contract,
                        execution=execution,
                        role="work_retry",
                    )
                else:
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
                "launch_kind": "retry" if retry_attempt else "resume" if resuming else "initial",
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
        if patch_text is None and failure is None:
            graph_update = GraphUpdateResult(status="none")
        else:
            while True:
                if patch_text is not None:
                    result, failure = _apply_work_patch(
                        service,
                        execution,
                        patch_text,
                        run_truth_scope=context.run_truth_scope,
                        patch_kind=request.patch_kind,
                        control_node_id=request.control_node_id,
                        control_decision_bundle=request.control_decision_bundle,
                    )
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
        try:
            watch_text = _read_watch_request(workspace, remote_stage)
        except (OSError, StateUnavailable, ValueError) as exc:
            watch_text = None
            watch_problem: str | None = f"The watcher request could not be read: {exc}"
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

        while watch_text is not None or watch_problem is not None:
            if watch_text is not None:
                try:
                    if execution is None:
                        raise ValueError("Watcher arming requires a durable originating operation.")
                    origin_task = execution.store.agent_task(execution.operation_id)
                    if origin_task is None:
                        raise ValueError("The originating Work operation is no longer available.")
                    specs = parse_watch_json(watch_text)
                    binding = WatcherBinding(
                        project_id=origin_task.project_id,
                        origin_operation_id=execution.operation_id,
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
                            control_decision_bundle=[
                                item.model_dump(mode="json")
                                for item in request.control_decision_bundle
                            ],
                            control_completion_criteria=request.control_completion_criteria,
                        ),
                    )
                    armed = await asyncio.to_thread(
                        arm_watchers,
                        execution.store,
                        specs,
                        binding,
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
                or watch_correction_rounds >= _MAX_CORRECTION_ROUNDS
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
            correction_contract = PromptFactory.continuation_task_contract(
                original_contract_path=base_contract_path,
                mode="watch_correction",
                diagnostics_path=diagnostics_path,
                watch_path=watch_path,
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
            async with aclosing(
                _stream_agent_events(
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
            ) as stream:
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
                watch_correction_rounds = _MAX_CORRECTION_ROUNDS
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
            patch_path = str(remote_stage.workspace / "patch.json")
        else:
            expected_stage = _swept_stage_root(data_dir) / stage_name
            local_stage = _validated_local_chat_resume_stage(execution, expected_stage)
            workspace = local_stage
            patch_path = str(local_stage / "patch.json")
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
        parent = execution.store.agent_task(execution.operation_id)
        if parent is None or parent.parent_operation_id is None:
            raise ValueError("The graph repair has no rejected Work parent.")
        rejected = execution.store.agent_task(parent.parent_operation_id)
        raw_graph_update = (
            rejected.result.get("graph_update") if rejected and rejected.result else None
        )
        previous = GraphUpdateResult.model_validate(raw_graph_update)
        if previous.status != "rejected":
            raise ValueError("Only a rejected Work graph update can be repaired.")
        original_contract_path = _parent_task_contract_path(execution, local_stage, remote_stage)
        token = _task_token(execution)
        validator_mailbox_id = uuid.uuid4().hex
        validator_client_path = _stage_task_input(
            local_stage,
            remote_stage,
            f"task-{token}-validator-client.py",
            VALIDATOR_CLIENT_SOURCE,
        )
        validator_command = shlex.join(
            [
                "python3",
                validator_client_path,
                patch_path,
                validator_mailbox_id,
                str(WORK_PATCH_SELF_CHECK_TIMEOUT_SECONDS),
                str(workspace),
            ]
        )
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
    graph_update, failure = _apply_work_patch(
        service,
        execution,
        patch_text,
        run_truth_scope=context.run_truth_scope,
        patch_kind=request.patch_kind,
        control_node_id=request.control_node_id,
        control_decision_bundle=request.control_decision_bundle,
    )
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
) -> _PreparedWorkPatch:
    draft, _ = service.parse_patch_output([patch_text])
    validate_agent_patch_shape(draft)
    patch = prepare_agent_patch(
        draft,
        kind=patch_kind,
        run_truth_scope=run_truth_scope,
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
) -> PatchValidationResult:
    try:
        candidate = _prepare_work_patch_candidate(
            service,
            patch_text,
            run_truth_scope=run_truth_scope,
            patch_kind=patch_kind,
            control_node_id=control_node_id,
            control_decision_bundle=control_decision_bundle,
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
    try:
        candidate = _prepare_work_patch_candidate(
            service,
            patch_text,
            run_truth_scope=run_truth_scope,
            patch_kind=patch_kind,
            control_node_id=control_node_id,
            control_decision_bundle=control_decision_bundle,
        )
        patch = candidate.patch
        change_summary = candidate.change_summary
        proposal_ids = candidate.proposal_ids
        _record_patch_receipt(
            execution,
            patch,
            byte_length=len(patch_text.encode("utf-8")),
        )
        if not patch.ops:
            return GraphUpdateResult(status="none"), None
        with service.history.workspace.run_lock():
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

    report = result.reports[appended.revision]
    _record_patch_applied_receipt(execution, result.state)
    return (
        GraphUpdateResult(
            status="applied",
            applied_revision=result.state.revision,
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
