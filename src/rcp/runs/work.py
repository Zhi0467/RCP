from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from rcp.agents import (
    AgentEvent,
    AgentLauncher,
    PromptFactory,
    agent_output_schema,
    normalize_agent_patch_bookkeeping,
    validate_agent_patch_shape,
    validate_work_patch,
)
from rcp.background import AgentTaskExecution
from rcp.config import AgentSurface
from rcp.core.models import Patch
from rcp.history import PatchRejected, ReplayHalted, RevisionConflict
from rcp.runs.chat import (
    _append_chat_exchange,
    _append_chat_graph_receipt,
    _chat_conversation_roots,
    _chat_native_checkpoint_available,
    _chat_read_dirs,
    _chat_stage_name,
    _cleanup_chat_conversation_projection,
    _clear_stale_patch,
    _discover_chat_artifacts,
    _first_chat_base_revision,
    _known_chat_session,
    _logical_chat_turn_operation_id,
    _prepare_local_artifact_directory,
    _project_chat_conversations,
    _read_chat_patch,
    _rebind_chat_conversations,
    _record_artifact_discovery_receipt,
    _record_chat_context_receipt,
    _saved_chat_conversation_projection,
    _validated_local_chat_resume_stage,
    _validated_remote_chat_resume_stage,
    _work_write_dirs,
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
from rcp.transport import RemoteRunStage, StateUnavailable

_MAX_CORRECTION_ROUNDS = 2


def _require_agent_patch_identity(patch: Patch, run_kind: str) -> None:
    if patch.author != "agent" or patch.kind != run_kind:
        raise ValueError(
            f"The {run_kind} agent must return an agent-authored {run_kind} patch; "
            "human approval patches can only be created by the RCP review UI."
        )


@dataclass(frozen=True)
class _WorkPatchFailure:
    message: str
    correctable: bool
    change_summary: tuple[str, ...] = ()
    proposal_ids: tuple[str, ...] = ()


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

    resuming = bool(execution is not None and execution.reuses_native_checkpoint)
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
    conversation_projection: Path | PurePosixPath | None = None
    artifact_scope_id: str | None = None
    artifact_directory: Path | PurePosixPath | None = None
    provider_started = False
    outcome = _ProviderOutcome(session_id=request.session_id)
    try:
        try:
            context = service.assemble_chat(request)
            base_revision = context.graph_revision
            if resuming:
                base_revision = _first_chat_base_revision(execution, base_revision)
            _record_chat_context_receipt(execution, context, surface=surface)
            if request.session_id and not resuming and not _known_chat_session(service, request):
                raise ValueError(
                    "That native session was not created by this chat. Start a new chat instead."
                )
            stage_name = _chat_stage_name(service, request, execution)
            if execution_host:
                if resuming:
                    stage_root = _validated_remote_chat_resume_stage(
                        execution, execution_host, stage_name
                    )
                    remote_stage = RemoteRunStage(execution_host).attach(stage_root)
                else:
                    remote_stage = RemoteRunStage(execution_host).open(stage_name, reuse=True)
                assert remote_stage.root is not None
                if execution is not None:
                    execution.checkpoint_stage(execution_host, str(remote_stage.root))
                if not resuming:
                    context = context.model_copy(
                        update=_stage_context_paths(
                            context, service, remote_stage, execution_machine.alias
                        )
                    )
                workspace = Path(str(remote_stage.workspace))
                patch_path = str(remote_stage.workspace / "patch.json")
            else:
                stage_root = _swept_stage_root(data_dir)
                expected_stage = stage_root / stage_name
                if resuming:
                    local_stage = _validated_local_chat_resume_stage(execution, expected_stage)
                else:
                    local_stage = expected_stage
                    local_stage.mkdir(parents=True, exist_ok=True)
                if execution is not None:
                    execution.checkpoint_stage("", str(local_stage))
                workspace = local_stage
                patch_path = str(local_stage / "patch.json")
            if not resuming:
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
            if resuming:
                conversation_projection = _saved_chat_conversation_projection(
                    local_stage, remote_stage
                )
                context = _rebind_chat_conversations(
                    context,
                    conversation_projection,
                    verify_local=remote_stage is None,
                )
            else:
                context, conversation_projection = _project_chat_conversations(
                    context, local_stage, remote_stage
                )

            read_dirs = _chat_read_dirs(
                context,
                remote_stage,
                service,
                execution_machine.alias,
                conversation_projection,
            )
            write_dirs = _work_write_dirs(
                context,
                service,
                execution_machine.alias,
                remote=remote_stage is not None,
            )
            token = _task_token(execution)
            if resuming:
                if not request.session_id:
                    raise ValueError(
                        "The interrupted Work turn has no native agent session; retry it instead."
                    )
                assert execution is not None
                original_contract_path = _parent_task_contract_path(
                    execution, local_stage, remote_stage
                )
                base_contract_path = original_contract_path
                contract = PromptFactory.continuation_task_contract(
                    original_contract_path=original_contract_path,
                    mode="resume",
                    patch_path=patch_path,
                )
                contract_path, prompt = _stage_task_contract(
                    local_stage,
                    remote_stage,
                    f"task-{token}-resume.md",
                    contract,
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
                    if execution is not None and execution.retry_feedback
                    else None
                )
                contract = PromptFactory.work_task_contract(
                    project_name=context.project_name,
                    ontology_path=f"{context.graph_path}#ontology",
                    graph_path=context.graph_path,
                    research_path=context.research_md_path,
                    focused_node_id=str(context.node["id"]) if context.node else None,
                    conversation_roots=_chat_conversation_roots(context),
                    conversations_unreachable=context.conversations_unreachable,
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
                )
                contract_path, prompt = _stage_task_contract(
                    local_stage,
                    remote_stage,
                    f"task-{token}-initial.md",
                    contract,
                    execution=execution,
                    role="work",
                )
                base_contract_path = contract_path
        except (OSError, ReplayHalted, StateUnavailable, ValueError) as exc:
            yield _sse(AgentEvent(event="error", text=str(exc)))
            return

        _record_agent_launch_receipt(
            execution,
            request,
            prompt=prompt,
            contract_path=contract_path,
            remote=bool(execution_host),
            resumed=resuming,
            continuation=execution.continuation if execution is not None else "fresh",
            extra={
                "surface": surface,
                "mode": "work",
                "capability": "work_auto",
                "network_access": True,
                "launch_kind": "resume" if resuming else "initial",
                "write_directory_count": len(write_dirs),
                "canonical_state_boundary": (
                    "prompt_only" if profile.provider == "claude" else "sandbox_enforced"
                ),
            },
        )
        provider_started = True
        try:
            async with aclosing(
                _stream_agent_events(
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
        if patch_text is None and failure is None:
            graph_update = GraphUpdateResult(status="none")
        else:
            while True:
                if patch_text is not None:
                    result, failure = _apply_work_patch(
                        service,
                        execution,
                        patch_text,
                        base_revision=base_revision,
                        run_truth_scope=context.run_truth_scope,
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
                    mode="patch_correction",
                    patch_path=patch_path,
                    diagnostics_path=diagnostics_path,
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
                        "capability": "scratch_patch",
                        "network_access": True,
                        "launch_kind": "graph_correction",
                        "correction_round": correction_rounds,
                        "write_directory_count": 0,
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
                        read_dirs=[],
                        write_dirs=[],
                        execution_host=execution_host,
                        execution=execution,
                        remote_stage=remote_stage,
                        capability="scratch_patch",
                        outcome=correction_outcome,
                        binary=provider_binary,
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
        retain_projection = (
            provider_started
            and not outcome.completed
            and not outcome.failed
            and _chat_native_checkpoint_available(execution, outcome.session_id)
        )
        if conversation_projection is not None and not retain_projection:
            _cleanup_chat_conversation_projection(local_stage, remote_stage, execution)


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
        stage_name = _chat_stage_name(service, request, execution)
        local_stage: Path | None = None
        remote_stage: RemoteRunStage | None = None
        if execution_host:
            stage_root = _validated_remote_chat_resume_stage(execution, execution_host, stage_name)
            remote_stage = RemoteRunStage(execution_host).attach(stage_root)
            workspace = Path(str(remote_stage.workspace))
            patch_path = str(remote_stage.workspace / "patch.json")
        else:
            expected_stage = _swept_stage_root(data_dir) / stage_name
            local_stage = _validated_local_chat_resume_stage(execution, expected_stage)
            workspace = local_stage
            patch_path = str(local_stage / "patch.json")
        current_revision = service.history.state().revision
        base_revision = _first_chat_base_revision(execution, current_revision)
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
        if current_revision != base_revision:
            graph_update = GraphUpdateResult(
                status="rejected",
                change_summary=previous.change_summary,
                proposal_ids=previous.proposal_ids,
                validation_messages=[
                    f"The graph moved from revision {base_revision} to {current_revision}; "
                    "start a new Work turn to reconcile it."
                ],
            )
            _append_chat_graph_receipt(
                service,
                request,
                request.session_id,
                graph_update,
                execution,
            )
            yield _sse(
                AgentEvent(
                    event="message",
                    text=json.dumps(
                        {"graph_update": graph_update.model_dump(mode="json")},
                        separators=(",", ":"),
                    ),
                )
            )
            yield _sse(AgentEvent(event="done"))
            return
        original_contract_path = _parent_task_contract_path(execution, local_stage, remote_stage)
        token = _task_token(execution)
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
            mode="patch_correction",
            patch_path=patch_path,
            diagnostics_path=diagnostics_path,
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
            "capability": "scratch_patch",
            "network_access": True,
            "launch_kind": "graph_repair",
            "write_directory_count": 0,
        },
    )
    outcome = _ProviderOutcome(session_id=request.session_id)
    async with aclosing(
        _stream_agent_events(
            launcher,
            request,
            prompt,
            workspace=workspace,
            session_id=request.session_id,
            read_dirs=[],
            write_dirs=[],
            execution_host=execution_host,
            execution=execution,
            remote_stage=remote_stage,
            capability="scratch_patch",
            outcome=outcome,
            binary=provider_binary,
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
        base_revision=base_revision,
        run_truth_scope=request.run_truth_scope or service.manifest.agent.default_run_truth_scope,
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


def _apply_work_patch(
    service: ProjectService,
    execution: AgentTaskExecution | None,
    patch_text: str,
    *,
    base_revision: int,
    run_truth_scope: list[str],
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
        patch, _ = service.parse_patch_output([patch_text])
        change_summary = tuple(patch.change_summary)
        proposal_ids = tuple(_work_patch_proposal_ids(patch))
        _record_patch_receipt(
            execution,
            patch,
            byte_length=len(patch_text.encode("utf-8")),
        )
        _require_agent_patch_identity(patch, "work")
        patch = normalize_agent_patch_bookkeeping(patch)
        validate_agent_patch_shape(patch)
        validate_work_patch(patch)
        if sorted(patch.run_truth_scope) != sorted(run_truth_scope):
            raise ValueError(
                "A Work patch must declare the run truth scope it was given "
                f"({sorted(run_truth_scope)}), not {sorted(patch.run_truth_scope)}."
            )
        if not patch.ops:
            return GraphUpdateResult(status="none"), None
        with service.history.workspace.run_lock():
            appended, result = service.history.append(
                patch,
                discard_on_reject=True,
                expected_revision=base_revision,
            )
    except RevisionConflict as exc:
        return None, _WorkPatchFailure(
            str(exc),
            correctable=False,
            change_summary=change_summary,
            proposal_ids=proposal_ids,
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
