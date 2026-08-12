from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from rcp.agents import (
    AgentEvent,
    AgentLauncher,
    ChatContext,
    ContextAssembler,
    agent_output_schema,
)
from rcp.agents.campaign_prompt import (
    campaign_orchestrator_continuation_contract,
    campaign_orchestrator_task_contract,
    campaign_report_task_contract,
    campaign_worker_continuation_contract,
    campaign_worker_task_contract,
)
from rcp.agents.command_mailbox import (
    CommandTurnIdentity,
    StagedCommandMailbox,
    serve_command_mailbox,
    stage_command_mailbox,
)
from rcp.agents.command_protocol import CommandRequest, CommandResponse, ValidateCommandRequest
from rcp.agents.prompts import PromptFactory
from rcp.background import AgentTaskExecution
from rcp.core.research_md import render_research_md
from rcp.limits import CHAT_ARTIFACT_MAX_FILE_BYTES
from rcp.providers import classify_terminal_error
from rcp.runs.campaign import (
    CAMPAIGN_REPORT_MAX_CORRECTION_ROUNDS,
    CampaignCommandContext,
    CampaignCommandDispatcher,
    CampaignCommandEffectResult,
    CampaignCommandInvalid,
    CampaignReportCorrectionRequired,
    CampaignRunRequest,
    campaign_report_correction,
    complete_campaign_report,
)
from rcp.runs.campaign_mail import (
    CAMPAIGN_MAIL_HANDOFF_FILE,
    CAMPAIGN_MAIL_MAX_BYTES,
    campaign_mail_delivery,
    parse_campaign_mail_delivery,
    stage_campaign_mail_delivery,
)
from rcp.runs.chat import (
    _chat_read_dirs,
    _clear_stale_turn_handoffs,
    _read_chat_patch,
    _work_write_dirs,
)
from rcp.runs.shared import (
    _existing_patch_digest,
    _parent_task_contract_path,
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
)
from rcp.runs.work import (
    _apply_work_patch,
    _bounded_graph_messages,
    _CorrectionPatchRead,
    _read_correction_patch,
    _record_work_graph_rejection,
    _retry_deliverable_is_unchanged,
    _WorkPatchFailure,
)
from rcp.service import GraphUpdateResult, ProjectService, RunRequest
from rcp.skill_registry import SkillSelection, official_registry
from rcp.skills.staging import skill_bundle_label, stage_skill_selection
from rcp.storage import (
    AgentTaskRecord,
    CampaignActorBinding,
    CampaignMessageRecord,
    CampaignRecord,
)
from rcp.transport import (
    RemoteRunStage,
    RunLockCancelled,
    RunStageMailbox,
    StateUnavailable,
    repository_access,
)

_MAX_CORRECTION_ROUNDS = 2
_SAME_ALLOCATION_RECOVERY = frozenset({"resume", "retry"})
_HANDOFFS_CLEARED_RECEIPT = "campaign_worker_handoffs_cleared"
_WORKER_CONTINUATIONS = frozenset(
    {
        "fresh",
        "resume",
        "retry",
        "watcher_wake",
        "graph_condition_wake",
        "message_wake",
        "campaign_continuation",
    }
)
_ORCHESTRATOR_CONTINUATIONS = _WORKER_CONTINUATIONS
_REPORT_CONTINUATIONS = frozenset({"campaign_continuation", "resume", "retry"})
_CAMPAIGN_REPORT_FILE = "campaign-report.html"
_REPORT_HISTORY_TASK_LIMIT = 64
_REPORT_HISTORY_EVENT_LIMIT = 256
_REPORT_HISTORY_MESSAGE_LIMIT = 128
_REPORT_HISTORY_PRIOR_REPORT_LIMIT = 8
_REPORT_HISTORY_MAX_BYTES = min(2 * 1024 * 1024, CHAT_ARTIFACT_MAX_FILE_BYTES)


@dataclass(frozen=True)
class _CanonicalWorkerTurn:
    task: AgentTaskRecord
    request: CampaignRunRequest
    binding: CampaignActorBinding
    allocation_operation_id: str
    recovering_allocation: bool


@dataclass(frozen=True)
class _CanonicalOrchestratorTurn:
    task: AgentTaskRecord
    request: CampaignRunRequest
    binding: CampaignActorBinding
    allocation_operation_id: str
    recovering_allocation: bool


@dataclass(frozen=True)
class _CanonicalReportTurn:
    task: AgentTaskRecord
    request: CampaignRunRequest
    campaign: CampaignRecord
    actor_operation_id: str
    native_session_id: str
    stage_host: str | None
    stage_root: str


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


async def stream_campaign_orchestrator_run(
    service: ProjectService,
    launcher: AgentLauncher,
    request: CampaignRunRequest,
    data_dir: Path,
    execution: AgentTaskExecution,
    *,
    command_dispatcher: CampaignCommandDispatcher,
) -> AsyncIterator[str]:
    """Run one paid turn of the sole project-owned campaign orchestrator."""

    try:
        turn = _canonical_orchestrator_turn(execution, request)
        if command_dispatcher.store is not execution.store:
            raise ValueError(
                "campaign orchestrator stream and command dispatcher must share one store"
            )
        stage = _open_orchestrator_stage(service, data_dir, execution, turn)
        context = _campaign_context(service, turn.request, stage)
        _prepare_orchestrator_handoffs(execution, turn, stage)

        messages_path = _stage_claimed_mail(execution, turn, stage)
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
            f"campaign-orchestrator-patch-schema-{schema_digest}.json",
            schema,
        )
        patch_path = str(stage.workspace / "patch.json")
        expected_turn_id = f"{execution.operation_id}:orchestrator"
        staged_commands = stage_command_mailbox(
            local_stage=stage.local,
            remote_stage=stage.remote,
            campaign_id=turn.request.campaign_id,
            task_id=execution.operation_id,
            turn_id=expected_turn_id,
        )
        async with _worker_mailbox_lifecycle(
            staged_commands,
            execution=execution,
            start=lambda stop: _serve_campaign_commands(
                staged_commands,
                execution=execution,
                turn=turn,
                dispatcher=command_dispatcher,
                stop=stop,
                expected_turn_id=expected_turn_id,
            ),
        ):
            validator_command = staged_commands.client_command("validate", patch_path)
            contract_path, prompt = _orchestrator_prompt(
                execution,
                turn,
                context=context,
                local_stage=stage.local,
                remote_stage=stage.remote,
                token=token,
                patch_path=patch_path,
                schema_path=schema_path,
                validator_command=validator_command,
                command_client=staged_commands.client_command(),
                messages_path=messages_path,
            )
            read_dirs = _chat_read_dirs(
                context,
                stage.remote,
                service,
                turn.request.run_on or "",
            )
            write_dirs = _work_write_dirs(
                context,
                service,
                turn.request.run_on or "",
                remote=stage.remote is not None,
            )
            retry_patch_digest = (
                _existing_patch_digest(stage.workspace, stage.remote)
                if execution.continuation == "retry"
                else None
            )
            _record_agent_launch_receipt(
                execution,
                cast(RunRequest, turn.request),
                prompt=prompt,
                contract_path=contract_path,
                remote=bool(stage.execution_host),
                resumed=turn.binding.native_session_id is not None,
                continuation=execution.continuation,
                extra={
                    "surface": "campaign",
                    "role": "orchestrator",
                    "profile": "orchestrator",
                    "actor_operation_id": turn.binding.actor_operation_id,
                    "allocation_operation_id": turn.allocation_operation_id,
                    "capability": "orchestrate",
                    "network_access": True,
                    "write_directory_count": len(write_dirs),
                    "canonical_state_boundary": "prompt_only",
                },
            )
            outcome = _ProviderOutcome(session_id=turn.binding.native_session_id)
            async with aclosing(
                _stream_agent_events(
                    launcher,
                    cast(RunRequest, turn.request),
                    prompt,
                    workspace=stage.workspace,
                    session_id=turn.binding.native_session_id,
                    read_dirs=read_dirs,
                    write_dirs=write_dirs,
                    execution_host=stage.execution_host,
                    execution=execution,
                    remote_stage=stage.remote,
                    capability="orchestrate",
                    outcome=outcome,
                    binary=stage.provider_binary,
                    invocation_gate=staged_commands.invocation_gate,
                )
            ) as stream:
                async for frame in stream:
                    yield frame
        if outcome.paused or outcome.failed:
            return
        if not outcome.completed:
            yield _sse(
                AgentEvent(
                    event="error",
                    text=f"{turn.request.provider} produced no campaign orchestrator result.",
                )
            )
            return
        if not outcome.session_id:
            yield _sse(
                AgentEvent(
                    event="error", text="Campaign orchestrator returned no native session id."
                )
            )
            return
        if (
            turn.binding.native_session_id is not None
            and outcome.session_id != turn.binding.native_session_id
        ):
            yield _sse(
                AgentEvent(
                    event="error",
                    text="Campaign orchestrator continuation changed its canonical native session.",
                )
            )
            return
        answer = "\n\n".join(item.strip() for item in outcome.answers if item.strip())
        if not answer:
            yield _sse(
                AgentEvent(event="error", text="Campaign orchestrator finished without an answer.")
            )
            return
        yield _sse(AgentEvent(event="answer", text=answer))

        settlement = await _settle_orchestrator_patch(
            service,
            launcher,
            execution,
            turn,
            stage,
            contract_path=contract_path,
            patch_path=patch_path,
            schema_path=schema_path,
            read_dirs=read_dirs,
            write_dirs=write_dirs,
            provider_binary=stage.provider_binary,
            native_session_id=outcome.session_id,
            retry_patch_digest=retry_patch_digest,
            command_dispatcher=command_dispatcher,
        )
        for frame in settlement.frames:
            yield frame
        graph_update = settlement.graph_update
        if graph_update is None:
            return
        payload: dict[str, object] = {"graph_update": graph_update.model_dump(mode="json")}
        if graph_update.applied_revision is not None:
            payload["applied_revision"] = graph_update.applied_revision
        yield _sse(AgentEvent(event="message", text=json.dumps(payload, separators=(",", ":"))))
        yield _sse(AgentEvent(event="done"))
    except (KeyError, OSError, StateUnavailable, ValueError) as exc:
        yield _sse(AgentEvent(event="error", text=str(exc)))


async def stream_campaign_report_run(
    service: ProjectService,
    launcher: AgentLauncher,
    request: CampaignRunRequest,
    data_dir: Path,
    execution: AgentTaskExecution,
) -> AsyncIterator[str]:
    """Spend the reserved unit as one report-only continuation of the orchestrator session."""

    try:
        turn = _canonical_report_turn(execution, request)
        stage = _open_report_stage(service, data_dir, execution, turn)
        selection = _campaign_report_skill_selection(turn.request)
        skill_pointers = stage_skill_selection(
            selection,
            local_stage=stage.local,
            remote_stage=stage.remote,
            label=skill_bundle_label(selection),
            reuse_existing=True,
        )
        if len(skill_pointers) != 1 or skill_pointers[0].get("id") != "campaign-report":
            raise ValueError("Campaign report staging did not resolve its one required skill.")
        report_skill_path = str(PurePosixPath(str(skill_pointers[0]["path"])) / "SKILL.md")
        (
            graph_path,
            research_path,
            campaign_record_path,
            campaign_history_path,
        ) = _stage_campaign_report_inputs(service, execution, turn, stage)
        report_output_path = str(stage.workspace / _CAMPAIGN_REPORT_FILE)
        mailbox = RunStageMailbox.for_stage(local_stage=stage.local, remote_stage=stage.remote)
        inputs_path = (
            stage.local / "inputs"
            if stage.local is not None
            else Path(str(stage.remote.root / "inputs"))
            if stage.remote is not None and stage.remote.root is not None
            else None
        )
        if inputs_path is None:
            raise ValueError("Campaign report stage has no exact immutable input directory.")
        diagnostic: str | None = None
        final_answer = "Campaign report written."

        for correction_round in range(CAMPAIGN_REPORT_MAX_CORRECTION_ROUNDS + 1):
            # The actor stage is reused across the campaign. Never accept an older ending's bytes
            # or a failed attempt's candidate as this invocation's report.
            mailbox.remove(_CAMPAIGN_REPORT_FILE)
            correction_diagnostic_path: str | None = None
            if diagnostic is not None:
                correction = campaign_report_correction(
                    execution.store,
                    execution.operation_id,
                    round=correction_round,
                    diagnostic=diagnostic,
                )
                if (
                    correction.native_session_id != turn.native_session_id
                    or (correction.stage_host or "") != (turn.stage_host or "")
                    or correction.stage_root != turn.stage_root
                ):
                    raise ValueError(
                        "Campaign report correction changed its saved native session or stage."
                    )
                correction_diagnostic_path = _stage_json_task_input(
                    stage.local,
                    stage.remote,
                    f"task-{_task_token(execution)}-campaign-report-correction-"
                    f"{correction_round}.json",
                    correction.model_dump(mode="json"),
                )

            contract = campaign_report_task_contract(
                project_name=service.manifest.name,
                ending=turn.request.ending or "failed",
                partial=turn.request.ending != "completed",
                graph_path=graph_path,
                research_path=research_path,
                campaign_record_path=campaign_record_path,
                campaign_history_path=campaign_history_path,
                report_skill_path=report_skill_path,
                report_output_path=report_output_path,
                correction_diagnostic_path=correction_diagnostic_path,
            )
            suffix = (
                "campaign-report"
                if correction_round == 0
                else f"campaign-report-correction-{correction_round}"
            )
            contract_path, prompt = _stage_task_contract(
                stage.local,
                stage.remote,
                f"task-{_task_token(execution)}-{suffix}.md",
                contract,
                execution=execution,
                role=suffix.replace("-", "_"),
            )
            _record_agent_launch_receipt(
                execution,
                cast(RunRequest, turn.request),
                prompt=prompt,
                contract_path=contract_path,
                remote=bool(stage.execution_host),
                resumed=True,
                continuation=(
                    execution.continuation
                    if correction_round == 0
                    else "campaign_report_correction"
                ),
                extra={
                    "surface": "campaign",
                    "role": "report",
                    "profile": "orchestrator",
                    "actor_operation_id": turn.actor_operation_id,
                    "allocation_operation_id": turn.task.operation_id,
                    "capability": "orchestrate",
                    "graph_authority": "none",
                    "report_output_path": report_output_path,
                    "report_skill": selection.resolved_skill_packages[0].model_dump(mode="json"),
                    "correction_round": correction_round,
                },
            )
            outcome = _ProviderOutcome(session_id=turn.native_session_id)
            async with aclosing(
                _stream_agent_events(
                    launcher,
                    cast(RunRequest, turn.request),
                    prompt,
                    workspace=stage.workspace,
                    session_id=turn.native_session_id,
                    read_dirs=[inputs_path],
                    write_dirs=[stage.workspace],
                    execution_host=stage.execution_host,
                    execution=execution,
                    remote_stage=stage.remote,
                    capability="orchestrate",
                    outcome=outcome,
                    binary=stage.provider_binary,
                )
            ) as stream:
                async for frame in stream:
                    yield frame
            if outcome.paused or outcome.failed:
                return
            if not outcome.completed:
                yield _sse(
                    AgentEvent(
                        event="error",
                        text=f"{turn.request.provider} produced no campaign report result.",
                    )
                )
                return
            if outcome.session_id != turn.native_session_id:
                yield _sse(
                    AgentEvent(
                        event="error",
                        text="Campaign report continuation changed the orchestrator native session.",
                    )
                )
                return
            answer = "\n\n".join(item.strip() for item in outcome.answers if item.strip())
            if answer:
                final_answer = answer

            try:
                candidate = _read_campaign_report_candidate(mailbox)
                ended, report = complete_campaign_report(
                    execution.store,
                    campaign_id=turn.campaign.campaign_id,
                    operation_id=turn.task.operation_id,
                    ending=turn.request.ending,
                    candidate=candidate,
                )
            except CampaignReportCorrectionRequired as exc:
                diagnostic = exc.diagnostic
                if correction_round >= CAMPAIGN_REPORT_MAX_CORRECTION_ROUNDS:
                    yield _sse(
                        AgentEvent(
                            event="error",
                            text=(
                                "Campaign report remained invalid after "
                                f"{CAMPAIGN_REPORT_MAX_CORRECTION_ROUNDS} in-session corrections: "
                                f"{diagnostic}"
                            ),
                        )
                    )
                    return
                execution.store.record_agent_task_event(
                    execution.operation_id,
                    f"Campaign report correction {correction_round + 1} required: {diagnostic}",
                    level="warning",
                )
                continue

            yield _sse(AgentEvent(event="answer", text=final_answer))
            yield _sse(
                AgentEvent(
                    event="message",
                    text=json.dumps(
                        {
                            "campaign_report": {
                                "report_id": report.report_id,
                                "ending": report.ending,
                                "sha256": report.sha256,
                                "campaign_status": ended.status,
                            }
                        },
                        separators=(",", ":"),
                    ),
                )
            )
            yield _sse(AgentEvent(event="done"))
            return
    except (KeyError, OSError, StateUnavailable, ValueError) as exc:
        yield _sse(AgentEvent(event="error", text=str(exc)))


async def stream_campaign_worker_run(
    service: ProjectService,
    launcher: AgentLauncher,
    request: CampaignRunRequest,
    data_dir: Path,
    execution: AgentTaskExecution,
    *,
    command_dispatcher: CampaignCommandDispatcher,
) -> AsyncIterator[str]:
    """Run one ordinary campaign worker on its canonical actor-owned stage."""

    try:
        turn = _canonical_worker_turn(execution, request)
        if command_dispatcher.store is not execution.store:
            raise ValueError("campaign worker stream and command dispatcher must share one store")
        stage = _open_worker_stage(service, data_dir, execution, turn)
        context = _campaign_context(service, turn.request, stage)
        _prepare_worker_handoffs(execution, turn, stage)

        messages_path = _stage_claimed_mail(execution, turn, stage)
        token = _task_token(execution)
        schema = (
            json.dumps(agent_output_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        schema_digest = hashlib.sha256(schema.encode("utf-8")).hexdigest()[:16]
        schema_path = _stage_or_reuse_task_input(
            stage.local,
            stage.remote,
            f"campaign-patch-schema-{schema_digest}.json",
            schema,
        )
        patch_path = str(stage.workspace / "patch.json")
        staged_commands = stage_command_mailbox(
            local_stage=stage.local,
            remote_stage=stage.remote,
            campaign_id=turn.request.campaign_id,
            task_id=execution.operation_id,
            turn_id=f"{execution.operation_id}:worker",
        )
        async with _worker_mailbox_lifecycle(
            staged_commands,
            execution=execution,
            start=lambda stop: _serve_worker_commands(
                staged_commands,
                execution=execution,
                turn=turn,
                dispatcher=command_dispatcher,
                stop=stop,
                expected_turn_id=f"{execution.operation_id}:worker",
            ),
        ):
            validator_command = staged_commands.client_command("validate", patch_path)
            reply_command = staged_commands.client_command(
                "message",
                "--key",
                _worker_reply_key(turn),
            )
            contract_path, prompt = _worker_prompt(
                service,
                execution,
                turn,
                context=context,
                local_stage=stage.local,
                remote_stage=stage.remote,
                token=token,
                patch_path=patch_path,
                schema_path=schema_path,
                validator_command=validator_command,
                reply_command=reply_command,
                messages_path=messages_path,
            )
            read_dirs = _chat_read_dirs(
                context,
                stage.remote,
                service,
                turn.request.run_on or "",
            )
            write_dirs = _work_write_dirs(
                context,
                service,
                turn.request.run_on or "",
                remote=stage.remote is not None,
            )
            retry_patch_digest = (
                _existing_patch_digest(stage.workspace, stage.remote)
                if execution.continuation == "retry"
                else None
            )
            _record_agent_launch_receipt(
                execution,
                cast(RunRequest, turn.request),
                prompt=prompt,
                contract_path=contract_path,
                remote=bool(stage.execution_host),
                resumed=turn.binding.native_session_id is not None,
                continuation=execution.continuation,
                extra={
                    "surface": "campaign",
                    "role": "worker",
                    "profile": "ordinary",
                    "actor_operation_id": turn.binding.actor_operation_id,
                    "allocation_operation_id": turn.allocation_operation_id,
                    "control_node_id": turn.binding.control_node_id,
                    "capability": "work_auto",
                    "network_access": True,
                    "write_directory_count": len(write_dirs),
                    "canonical_state_boundary": "prompt_only",
                },
            )

            outcome = _ProviderOutcome(session_id=turn.binding.native_session_id)
            async with aclosing(
                _stream_agent_events(
                    launcher,
                    cast(RunRequest, turn.request),
                    prompt,
                    workspace=stage.workspace,
                    session_id=turn.binding.native_session_id,
                    read_dirs=read_dirs,
                    write_dirs=write_dirs,
                    execution_host=stage.execution_host,
                    execution=execution,
                    remote_stage=stage.remote,
                    capability="work_auto",
                    outcome=outcome,
                    binary=stage.provider_binary,
                    invocation_gate=staged_commands.invocation_gate,
                )
            ) as stream:
                async for frame in stream:
                    yield frame
        if outcome.paused or outcome.failed:
            return
        if not outcome.completed:
            yield _sse(
                AgentEvent(
                    event="error",
                    text=f"{turn.request.provider} produced no campaign worker result.",
                )
            )
            return
        if not outcome.session_id:
            yield _sse(
                AgentEvent(event="error", text="Campaign worker returned no native session id.")
            )
            return
        if (
            turn.binding.native_session_id is not None
            and outcome.session_id != turn.binding.native_session_id
        ):
            yield _sse(
                AgentEvent(
                    event="error",
                    text="Campaign worker continuation changed its canonical native session.",
                )
            )
            return
        answer = "\n\n".join(item.strip() for item in outcome.answers if item.strip())
        if not answer:
            yield _sse(
                AgentEvent(event="error", text="Campaign worker finished without an answer.")
            )
            return
        yield _sse(AgentEvent(event="answer", text=answer))

        settlement = await _settle_worker_patch(
            service,
            launcher,
            execution,
            turn,
            stage,
            contract_path=contract_path,
            patch_path=patch_path,
            schema_path=schema_path,
            read_dirs=read_dirs,
            write_dirs=write_dirs,
            provider_binary=stage.provider_binary,
            native_session_id=outcome.session_id,
            retry_patch_digest=retry_patch_digest,
            command_dispatcher=command_dispatcher,
        )
        for frame in settlement.frames:
            yield frame
        graph_update = settlement.graph_update
        if graph_update is None:
            return
        payload: dict[str, object] = {"graph_update": graph_update.model_dump(mode="json")}
        if graph_update.applied_revision is not None:
            payload["applied_revision"] = graph_update.applied_revision
        yield _sse(AgentEvent(event="message", text=json.dumps(payload, separators=(",", ":"))))
        yield _sse(AgentEvent(event="done"))
    except (KeyError, OSError, StateUnavailable, ValueError) as exc:
        yield _sse(AgentEvent(event="error", text=str(exc)))


def _canonical_worker_turn(
    execution: AgentTaskExecution,
    supplied: CampaignRunRequest,
) -> _CanonicalWorkerTurn:
    if execution.continuation not in _WORKER_CONTINUATIONS:
        raise ValueError("Campaign worker continuation is not supported by this stream.")
    task = execution.store.agent_task(execution.operation_id)
    if task is None or task.kind != "campaign" or task.campaign_id is None:
        raise ValueError("Campaign worker execution requires one durable campaign task.")
    durable = CampaignRunRequest.model_validate(task.request)
    if durable != supplied:
        raise ValueError("Campaign worker launch request differs from its durable task record.")
    binding = execution.store.campaign_actor_binding(execution.operation_id)
    if execution.store.agent_task_profile(execution.operation_id) != "ordinary":
        raise ValueError("Campaign worker requires the canonical ordinary semantic profile.")
    if binding.role != "worker" or durable.role != "worker":
        raise ValueError("This stream executes ordinary campaign workers only.")
    if (
        binding.campaign_id != task.campaign_id
        or durable.campaign_id != task.campaign_id
        or durable.actor_operation_id != binding.actor_operation_id
        or durable.control_node_id != binding.control_node_id
        or binding.current_operation_id != execution.operation_id
    ):
        raise ValueError("Campaign worker role, actor, or seat conflicts with its durable binding.")
    if (execution.stage_host or "") != (binding.stage_host or ""):
        raise ValueError("Campaign worker execution host conflicts with its durable actor binding.")
    if execution.stage_root != binding.stage_root:
        raise ValueError("Campaign worker stage conflicts with its durable actor binding.")
    cause = execution.store.agent_task_continuation_cause(execution.operation_id)
    if cause != execution.continuation:
        raise ValueError("Campaign worker continuation conflicts with its durable launch cause.")
    recovering = execution.continuation in _SAME_ALLOCATION_RECOVERY
    if execution.continuation != "fresh" and (
        not binding.native_session_id
        or not binding.stage_root
        or durable.session_id != binding.native_session_id
    ):
        raise ValueError("Campaign worker recovery requires its exact saved session and stage.")
    if execution.continuation == "fresh" and binding.actor_operation_id != execution.operation_id:
        raise ValueError("A fresh campaign worker cannot continue another actor.")
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
    supplied: CampaignRunRequest,
) -> _CanonicalOrchestratorTurn:
    if execution.continuation not in _ORCHESTRATOR_CONTINUATIONS:
        raise ValueError("Campaign orchestrator continuation is not supported by this stream.")
    task = execution.store.agent_task(execution.operation_id)
    if task is None or task.kind != "campaign" or task.campaign_id is None:
        raise ValueError("Campaign orchestrator execution requires one durable campaign task.")
    durable = CampaignRunRequest.model_validate(task.request)
    if durable != supplied:
        raise ValueError(
            "Campaign orchestrator launch request differs from its durable task record."
        )
    binding = execution.store.campaign_actor_binding(execution.operation_id)
    if execution.store.agent_task_profile(execution.operation_id) != "orchestrator":
        raise ValueError("Campaign orchestrator requires its sole elevated semantic profile.")
    if binding.role != "orchestrator" or durable.role != "orchestrator":
        raise ValueError("This stream executes the campaign orchestrator only.")
    if (
        binding.campaign_id != task.campaign_id
        or durable.campaign_id != task.campaign_id
        or durable.actor_operation_id != binding.actor_operation_id
        or durable.control_node_id is not None
        or binding.control_node_id is not None
        or binding.current_operation_id != execution.operation_id
    ):
        raise ValueError(
            "Campaign orchestrator role, actor, or scope conflicts with its durable binding."
        )
    if (execution.stage_host or "") != (binding.stage_host or ""):
        raise ValueError(
            "Campaign orchestrator execution host conflicts with its durable actor binding."
        )
    if execution.stage_root != binding.stage_root:
        raise ValueError("Campaign orchestrator stage conflicts with its durable actor binding.")
    cause = execution.store.agent_task_continuation_cause(execution.operation_id)
    if cause != execution.continuation:
        raise ValueError(
            "Campaign orchestrator continuation conflicts with its durable launch cause."
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
        raise ValueError("Campaign orchestrator continuation requires its exact session and stage.")
    if execution.continuation == "fresh" and (
        binding.actor_operation_id != execution.operation_id or task.parent_operation_id is not None
    ):
        raise ValueError("A fresh campaign orchestrator must be the sole root actor.")
    allocation_operation_id = _paid_allocation_operation_id(execution, task)
    return _CanonicalOrchestratorTurn(
        task=task,
        request=durable.model_copy(update={"session_id": binding.native_session_id}),
        binding=binding,
        allocation_operation_id=allocation_operation_id,
        recovering_allocation=recovering,
    )


def _is_authorized_clean_orchestrator_retry(
    execution: AgentTaskExecution,
    *,
    task: AgentTaskRecord,
    request: CampaignRunRequest,
    binding: CampaignActorBinding,
) -> bool:
    """Recognize the storage-authorized same-allocation clean-session retry."""

    if (
        execution.continuation != "retry"
        or task.parent_operation_id is None
        or task.native_session_id is not None
        or request.session_id is not None
        or binding.native_session_id is not None
        or not binding.stage_root
    ):
        return False
    parent = execution.store.agent_task(task.parent_operation_id)
    if (
        parent is None
        or parent.campaign_id != task.campaign_id
        or parent.kind != "campaign"
        or parent.status not in {"paused", "interrupted", "failed"}
        or task.attempt != parent.attempt + 1
    ):
        return False
    parent_request = CampaignRunRequest.model_validate(parent.request)
    if (
        parent_request.role != "orchestrator"
        or (parent_request.actor_operation_id or parent.operation_id) != binding.actor_operation_id
        or (parent.stage_host or "") != (binding.stage_host or "")
        or parent.stage_root != binding.stage_root
    ):
        return False
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
    # held the reportable session/stage pair being retired.
    prior = parent
    seen: set[str] = set()
    while True:
        if prior.operation_id in seen:
            return False
        seen.add(prior.operation_id)
        prior_request = CampaignRunRequest.model_validate(prior.request)
        if (
            prior.campaign_id != task.campaign_id
            or prior.kind != "campaign"
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


def _canonical_report_turn(
    execution: AgentTaskExecution,
    supplied: CampaignRunRequest,
) -> _CanonicalReportTurn:
    if execution.continuation not in _REPORT_CONTINUATIONS:
        raise ValueError("Campaign report continuation is not supported by this stream.")
    task = execution.store.agent_task(execution.operation_id)
    if task is None or task.kind != "campaign" or task.campaign_id is None:
        raise ValueError("Campaign report execution requires one durable campaign task.")
    durable = CampaignRunRequest.model_validate(task.request)
    if durable != supplied:
        raise ValueError("Campaign report launch request differs from its durable task record.")
    if execution.store.campaign_invocation_role(task.operation_id) != "report":
        raise ValueError("This stream executes the reserved campaign report allocation only.")
    campaign = execution.store.campaign(task.campaign_id)
    if campaign is None:
        raise ValueError("Campaign report has no durable campaign.")
    if (
        campaign.project_id != task.project_id
        or campaign.status != "wrapping_up"
        or campaign.ending is None
        or durable.role != "report"
        or durable.ending != campaign.ending
    ):
        raise ValueError("Campaign report does not match the active durable ending.")
    actor_operation_id = campaign.root_operation_id
    if actor_operation_id is None or durable.actor_operation_id != actor_operation_id:
        raise ValueError("Campaign report must continue the sole orchestrator actor.")
    if durable.control_node_id is not None or durable.wake_cause is not None or durable.watcher_ids:
        raise ValueError("Campaign report cannot carry worker seating or wake state.")
    if task.dispatch_authority is not None:
        raise ValueError("Campaign report cannot carry graph dispatch authority.")
    if (
        not durable.provider
        or durable.model is None
        or durable.reasoning is None
        or not durable.run_on
        or not durable.session_id
        or not task.native_session_id
        or not task.stage_root
    ):
        raise ValueError("Campaign report requires the orchestrator's exact saved launch binding.")
    if durable.session_id != task.native_session_id:
        raise ValueError("Campaign report request changed the saved orchestrator native session.")
    if (execution.stage_host or "") != (
        task.stage_host or ""
    ) or execution.stage_root != task.stage_root:
        raise ValueError("Campaign report execution changed the saved orchestrator stage.")
    cause = execution.store.agent_task_continuation_cause(execution.operation_id)
    if cause != execution.continuation:
        raise ValueError("Campaign report continuation conflicts with its durable launch cause.")
    if task.parent_operation_id is None:
        raise ValueError("Campaign report has no concluding orchestrator lineage.")
    parent = execution.store.agent_task(task.parent_operation_id)
    if parent is None or parent.campaign_id != campaign.campaign_id:
        raise ValueError("Campaign report parent is outside its campaign lineage.")
    parent_role = execution.store.campaign_invocation_role(parent.operation_id)
    parent_request = CampaignRunRequest.model_validate(parent.request)
    expected_parent_role = (
        "orchestrator" if execution.continuation == "campaign_continuation" else "report"
    )
    if (
        parent_role != expected_parent_role
        or parent_request.actor_operation_id != actor_operation_id
        or parent_request.provider != durable.provider
        or parent_request.model != durable.model
        or parent_request.reasoning != durable.reasoning
        or parent_request.run_on != durable.run_on
        or parent.native_session_id != durable.session_id
        or (parent.stage_host or "") != (task.stage_host or "")
        or parent.stage_root != task.stage_root
    ):
        raise ValueError(
            "Campaign report did not preserve the concluding orchestrator's provider, session, "
            "or exact stage."
        )
    return _CanonicalReportTurn(
        task=task,
        request=durable,
        campaign=campaign,
        actor_operation_id=actor_operation_id,
        native_session_id=durable.session_id,
        stage_host=task.stage_host,
        stage_root=task.stage_root,
    )


def _campaign_report_skill_selection(request: CampaignRunRequest) -> SkillSelection:
    reference = official_registry().package("skill", "campaign-report").reference()
    selection = SkillSelection(
        skill_ids=["campaign-report"],
        resolved_skill_packages=[reference],
    )
    if (
        request.workflow_ids not in (None, [])
        or request.skill_ids != ["campaign-report"]
        or request.invoked_workflow_ids
        or request.invoked_skill_ids != ["campaign-report"]
        or request.invoked_provider_skill_names
        or request.resolved_provider_skills
        or request.resolved_skill_packages != [reference]
    ):
        raise ValueError(
            "Campaign report requires exactly the current official campaign-report skill and no "
            "other invoked package."
        )
    return selection


def _open_report_stage(
    service: ProjectService,
    data_dir: Path,
    execution: AgentTaskExecution,
    turn: _CanonicalReportTurn,
) -> _WorkerStage:
    request = turn.request
    assert request.run_on is not None and request.provider is not None
    machine = service.manifest.machine_map.get(request.run_on)
    if machine is None:
        raise ValueError(f"unknown campaign report execution machine: {request.run_on}")
    stage_name = _orchestrator_stage_name(turn.task.project_id, turn.actor_operation_id)
    expected_remote = str(PurePosixPath("/tmp") / f"rcp-run.{stage_name}")
    if machine.host:
        if turn.stage_host != machine.host or turn.stage_root != expected_remote:
            raise ValueError("Campaign report changed the orchestrator's saved remote stage.")
        remote = RemoteRunStage(machine.host).attach(turn.stage_root)
        return _WorkerStage(
            local=None,
            remote=remote,
            workspace=Path(str(remote.workspace)),
            execution_host=machine.host,
            provider_binary=machine.provider_paths.get(request.provider),
        )

    expected_local = data_dir / "run-stage" / stage_name
    saved = Path(turn.stage_root)
    if (
        turn.stage_host is not None
        or saved.absolute() != expected_local.absolute()
        or saved.is_symlink()
        or not saved.is_dir()
    ):
        raise ValueError("Campaign report changed the orchestrator's saved local stage.")
    return _WorkerStage(
        local=saved,
        remote=None,
        workspace=saved,
        execution_host="",
        provider_binary=machine.provider_paths.get(request.provider),
    )


def _stage_campaign_report_inputs(
    service: ProjectService,
    execution: AgentTaskExecution,
    turn: _CanonicalReportTurn,
    stage: _WorkerStage,
) -> tuple[str, str, str, str]:
    state = service.history.state()
    graph = (
        json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    research = render_research_md(state)
    graph_digest = hashlib.sha256(graph.encode("utf-8")).hexdigest()[:16]
    research_digest = hashlib.sha256(research.encode("utf-8")).hexdigest()[:16]
    graph_path = _stage_or_reuse_task_input(
        stage.local,
        stage.remote,
        f"campaign-graph-r{state.revision}-{graph_digest}.json",
        graph,
    )
    research_path = _stage_or_reuse_task_input(
        stage.local,
        stage.remote,
        f"campaign-research-r{state.revision}-{research_digest}.md",
        research,
    )
    record_payload = {
        "campaign": turn.campaign.model_dump(mode="json"),
        "budget": execution.store.campaign_budget_meter(turn.campaign.campaign_id).model_dump(
            mode="json"
        ),
    }
    history_payload = _bounded_campaign_report_history(execution, turn)
    token = _task_token(execution)
    campaign_record_path = _stage_json_task_input(
        stage.local,
        stage.remote,
        f"task-{token}-campaign-record.json",
        record_payload,
    )
    campaign_history_path = _stage_json_task_input(
        stage.local,
        stage.remote,
        f"task-{token}-campaign-history.json",
        history_payload,
    )
    return graph_path, research_path, campaign_record_path, campaign_history_path


def _bounded_campaign_report_history(
    execution: AgentTaskExecution,
    turn: _CanonicalReportTurn,
) -> dict[str, object]:
    campaign_id = turn.campaign.campaign_id
    task_count, task_counts, role_counts, selected_tasks = (
        execution.store.campaign_report_task_history(
            campaign_id,
            limit=_REPORT_HISTORY_TASK_LIMIT,
        )
    )
    if not selected_tasks:
        raise ValueError("Campaign report has no durable task history.")

    def bounded_text(value: str | None, max_bytes: int) -> str | None:
        return _bounded_report_text(value, max_bytes)

    def bounded_json(value: object, max_bytes: int) -> str | None:
        if value is None:
            return None
        return bounded_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            max_bytes,
        )

    task_entries: list[dict[str, object]] = []
    for task in selected_tasks:
        request = CampaignRunRequest.model_validate(task.request)
        result = task.result or {}
        result_messages = result.get("messages")
        valid_result_messages = (
            [item for item in result_messages if isinstance(item, str)]
            if isinstance(result_messages, list)
            else []
        )
        task_entries.append(
            {
                "operation_id": task.operation_id,
                "parent_operation_id": task.parent_operation_id,
                "actor_operation_id": request.actor_operation_id,
                "role": execution.store.campaign_invocation_role(task.operation_id),
                "control_node_id": request.control_node_id,
                "wake_cause": request.wake_cause,
                "status": task.status,
                "attempt": task.attempt,
                "created_at": task.created_at,
                "finished_at": task.finished_at,
                "applied_revision": task.applied_revision,
                "instruction": bounded_text(request.instruction, 4_000),
                "status_message": bounded_text(task.status_message, 2_000),
                "error": bounded_text(task.error, 2_000),
                "result_messages": [
                    bounded_text(item, 2_000) for item in valid_result_messages[:2]
                ],
                "omitted_result_message_count": max(0, len(valid_result_messages) - 2),
                "graph_update": bounded_json(result.get("graph_update"), 4_000),
            }
        )

    event_count, events = execution.store.campaign_report_event_history(
        campaign_id,
        limit=_REPORT_HISTORY_EVENT_LIMIT,
    )
    event_entries = [
        {
            "event_id": event.event_id,
            "operation_id": event.operation_id,
            "created_at": event.created_at,
            "level": event.level,
            "kind": event.event_kind,
            "message": bounded_text(event.message, 1_000),
            "command_verb": event.command_verb,
            "command_phase": event.command_phase,
            "payload": bounded_json(event.payload, 1_000),
        }
        for event in events
    ]

    message_count, messages = execution.store.campaign_report_message_history(
        campaign_id,
        limit=_REPORT_HISTORY_MESSAGE_LIMIT,
    )
    message_entries = [
        {
            "message_id": message.message_id,
            "sender_role": message.sender_role,
            "sender_task_id": message.sender_task_id,
            "authorized_by": (
                message.authorized_by.model_dump(mode="json")
                if message.authorized_by is not None
                else None
            ),
            "recipient_task_id": message.recipient_task_id,
            "control_node_id": message.control_node_id,
            "body": bounded_text(message.body, 2_000),
            "created_at": message.created_at,
            "delivered_at": message.delivered_at,
        }
        for message in messages
    ]
    prior_report_count, reports = execution.store.campaign_report_prior_history(
        campaign_id,
        limit=_REPORT_HISTORY_PRIOR_REPORT_LIMIT,
    )
    report_entries = [
        {
            "report_id": report.report_id,
            "operation_id": report.operation_id,
            "ending": report.ending,
            "sha256": report.sha256,
            "created_at": report.created_at,
            "html_excerpt": bounded_text(report.html, 8_000),
        }
        for report in reports
    ]
    payload: dict[str, object] = {
        "summary": {
            "task_count": task_count,
            "included_task_count": len(selected_tasks),
            "omitted_task_count": task_count - len(selected_tasks),
            "status_counts": task_counts,
            "role_counts": role_counts,
            "event_count": event_count,
            "included_event_count": len(events),
            "omitted_event_count": event_count - len(events),
            "message_count": message_count,
            "included_message_count": len(messages),
            "omitted_message_count": message_count - len(messages),
            "prior_report_count": prior_report_count,
            "included_prior_report_count": len(reports),
            "omitted_prior_report_count": prior_report_count - len(reports),
            "truncated_field_count": 0,
            "byte_limit_omitted": {
                "tasks": 0,
                "events": 0,
                "messages": 0,
                "prior_reports": 0,
            },
        },
        "tasks": task_entries,
        "events": event_entries,
        "messages": message_entries,
        "prior_reports": report_entries,
    }
    _fit_campaign_report_history(payload)
    return payload


def _fit_campaign_report_history(payload: dict[str, object]) -> None:
    """Drop oldest bounded excerpts until the staged JSON is guaranteed to fit."""

    summary = payload["summary"]
    assert isinstance(summary, dict)
    byte_limit_omitted = summary["byte_limit_omitted"]
    assert isinstance(byte_limit_omitted, dict)

    def encoded_size() -> int:
        return len(
            (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            )
        )

    while True:
        summary["truncated_field_count"] = _truncated_report_field_count(payload)
        if encoded_size() <= _REPORT_HISTORY_MAX_BYTES:
            return
        removed = False
        for key, summary_key, preserve in (
            ("prior_reports", "prior_reports", 0),
            ("events", "events", 0),
            ("messages", "messages", 0),
            ("tasks", "tasks", 1),
        ):
            entries = payload[key]
            assert isinstance(entries, list)
            removable = len(entries) - preserve
            if removable <= 0:
                continue
            count = max(1, (removable + 1) // 2)
            del entries[preserve : preserve + count]
            byte_limit_omitted[summary_key] = int(byte_limit_omitted[summary_key]) + count
            included_key = f"included_{key[:-1]}_count"
            omitted_key = f"omitted_{key[:-1]}_count"
            if key == "prior_reports":
                included_key = "included_prior_report_count"
                omitted_key = "omitted_prior_report_count"
            summary[included_key] = int(summary[included_key]) - count
            summary[omitted_key] = int(summary[omitted_key]) + count
            removed = True
            summary["truncated_field_count"] = _truncated_report_field_count(payload)
            if encoded_size() <= _REPORT_HISTORY_MAX_BYTES:
                return
        if not removed:
            raise ValueError("Campaign report history metadata exceeds its staging byte limit.")


def _truncated_report_field_count(value: object) -> int:
    if isinstance(value, dict):
        return sum(_truncated_report_field_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_truncated_report_field_count(item) for item in value)
    return int(isinstance(value, str) and value.endswith("\n[truncated]"))


def _bounded_report_text(value: str | None, max_bytes: int) -> str | None:
    if value is None:
        return None
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore") + "\n[truncated]"


def _read_campaign_report_candidate(mailbox: RunStageMailbox) -> str | None:
    try:
        return mailbox.read_text(
            _CAMPAIGN_REPORT_FILE,
            max_bytes=CHAT_ARTIFACT_MAX_FILE_BYTES,
        )
    except FileNotFoundError:
        return None
    except ValueError as exc:
        raise CampaignReportCorrectionRequired(f"Campaign report is invalid: {exc}.") from exc


def _paid_allocation_operation_id(
    execution: AgentTaskExecution,
    task: AgentTaskRecord,
) -> str:
    current = task
    seen: set[str] = set()
    while execution.store.agent_task_continuation_cause(current.operation_id) in {
        "resume",
        "retry",
    }:
        if current.operation_id in seen or current.parent_operation_id is None:
            raise ValueError("Campaign actor recovery lost its paid allocation lineage.")
        seen.add(current.operation_id)
        parent = execution.store.agent_task(current.parent_operation_id)
        if parent is None or parent.campaign_id != task.campaign_id or parent.kind != "campaign":
            raise ValueError("Campaign actor recovery crossed its paid allocation lineage.")
        current = parent
    return current.operation_id


def _worker_stage_name(project_id: str, actor_operation_id: str) -> str:
    project = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:16]
    actor = hashlib.sha256(actor_operation_id.encode("utf-8")).hexdigest()[:16]
    return f"campaign-worker-{project}-{actor}"


def _orchestrator_stage_name(project_id: str, actor_operation_id: str) -> str:
    project = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:16]
    actor = hashlib.sha256(actor_operation_id.encode("utf-8")).hexdigest()[:16]
    return f"campaign-orchestrator-{project}-{actor}"


def _open_worker_stage(
    service: ProjectService,
    data_dir: Path,
    execution: AgentTaskExecution,
    turn: _CanonicalWorkerTurn,
) -> _WorkerStage:
    return _open_campaign_actor_stage(
        service,
        data_dir,
        execution,
        turn,
        stage_name=_worker_stage_name(turn.task.project_id, turn.binding.actor_operation_id),
        actor_label="worker",
    )


def _open_orchestrator_stage(
    service: ProjectService,
    data_dir: Path,
    execution: AgentTaskExecution,
    turn: _CanonicalOrchestratorTurn,
) -> _WorkerStage:
    return _open_campaign_actor_stage(
        service,
        data_dir,
        execution,
        turn,
        stage_name=_orchestrator_stage_name(
            turn.task.project_id,
            turn.binding.actor_operation_id,
        ),
        actor_label="orchestrator",
    )


def _open_campaign_actor_stage(
    service: ProjectService,
    data_dir: Path,
    execution: AgentTaskExecution,
    turn: _CanonicalWorkerTurn | _CanonicalOrchestratorTurn,
    *,
    stage_name: str,
    actor_label: Literal["worker", "orchestrator"],
) -> _WorkerStage:
    request = turn.request
    if (
        not request.provider
        or request.model is None
        or request.reasoning is None
        or not request.run_on
    ):
        raise ValueError(f"Campaign {actor_label} has no exact pinned provider profile.")
    machine = service.manifest.machine_map.get(request.run_on)
    if machine is None:
        raise ValueError(f"unknown campaign {actor_label} execution machine: {request.run_on}")
    expected_remote = str(PurePosixPath("/tmp") / f"rcp-run.{stage_name}")
    if turn.binding.stage_root is not None:
        if machine.host:
            if (
                turn.binding.stage_host != machine.host
                or turn.binding.stage_root != expected_remote
            ):
                raise ValueError(
                    f"Campaign {actor_label} saved remote stage has a different actor binding."
                )
            remote = RemoteRunStage(machine.host).attach(turn.binding.stage_root)
            return _WorkerStage(
                local=None,
                remote=remote,
                workspace=Path(str(remote.workspace)),
                execution_host=machine.host,
                provider_binary=machine.provider_paths.get(request.provider),
            )
        expected_local = _swept_stage_root(data_dir) / stage_name
        saved = Path(turn.binding.stage_root)
        if (
            turn.binding.stage_host is not None
            or saved.absolute() != expected_local.absolute()
            or saved.is_symlink()
            or not saved.is_dir()
        ):
            raise ValueError(
                f"Campaign {actor_label} saved local stage has a different actor binding."
            )
        return _WorkerStage(
            local=saved,
            remote=None,
            workspace=saved,
            execution_host="",
            provider_binary=machine.provider_paths.get(request.provider),
        )

    if turn.binding.native_session_id is not None or execution.continuation != "fresh":
        raise ValueError(
            f"Campaign {actor_label} continuation cannot start a fresh execution stage."
        )
    if machine.host:
        remote = RemoteRunStage(machine.host).open(stage_name, reuse=True)
        assert remote.root is not None
        execution.checkpoint_stage(machine.host, str(remote.root))
        return _WorkerStage(
            local=None,
            remote=remote,
            workspace=Path(str(remote.workspace)),
            execution_host=machine.host,
            provider_binary=machine.provider_paths.get(request.provider),
        )
    local = _swept_stage_root(data_dir) / stage_name
    if os.path.lexists(local):
        if local.is_symlink() or not local.is_dir():
            raise ValueError(f"Campaign {actor_label} local stage is unsafe.")
    else:
        local.mkdir(mode=0o700, parents=True)
    execution.checkpoint_stage("", str(local))
    return _WorkerStage(
        local=local,
        remote=None,
        workspace=local,
        execution_host="",
        provider_binary=machine.provider_paths.get(request.provider),
    )


def _campaign_context(
    service: ProjectService,
    request: CampaignRunRequest,
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
    context = ContextAssembler(service.manifest).chat_context(
        state,
        node_id=None,
        run_truth_scope=request.run_truth_scope,
        repository_access=access,
    )
    state_machine = service.manifest.repository_map[service.manifest.state.repository].machine
    if state_machine != request.run_on:
        repositories = []
        for item in context.repositories:
            if item.machine == request.run_on:
                repositories.append(item.model_copy(update={"host": ""}))
            elif item.host:
                repositories.append(item)
            else:
                raise StateUnavailable(
                    f"Repository {item.alias!r} has no SSH host reachable from campaign "
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
                f"campaign-graph-r{state.revision}-{graph_digest}.json",
                graph,
            ),
            "research_md_path": _stage_or_reuse_task_input(
                stage.local,
                stage.remote,
                f"campaign-research-r{state.revision}-{research_digest}.md",
                research,
            ),
        }
        context = context.model_copy(update=updates)
    elif stage.remote is not None:
        context = context.model_copy(
            update=_stage_context_paths(context, service, stage.remote, request.run_on or "")
        )
    return context


def _claimed_messages(
    execution: AgentTaskExecution,
    turn: _CanonicalWorkerTurn | _CanonicalOrchestratorTurn,
) -> list[CampaignMessageRecord]:
    return [
        message
        for message in execution.store.campaign_messages(turn.request.campaign_id)
        if message.delivery_operation_id == turn.allocation_operation_id
    ]


def _prepare_worker_handoffs(
    execution: AgentTaskExecution,
    turn: _CanonicalWorkerTurn,
    stage: _WorkerStage,
) -> None:
    cleared = execution.store.campaign_worker_handoffs_cleared(turn.allocation_operation_id)
    if turn.recovering_allocation and cleared:
        return
    _clear_stale_turn_handoffs(stage.workspace, stage.remote)
    execution.store.mark_campaign_worker_handoffs_cleared(turn.allocation_operation_id)
    execution.store.record_agent_task_receipt(
        turn.allocation_operation_id,
        _HANDOFFS_CLEARED_RECEIPT,
        {
            "version": 1,
            "files": ["patch.json", "watch.json", CAMPAIGN_MAIL_HANDOFF_FILE],
        },
    )


def _prepare_orchestrator_handoffs(
    execution: AgentTaskExecution,
    turn: _CanonicalOrchestratorTurn,
    stage: _WorkerStage,
) -> None:
    cleared = execution.store.campaign_handoffs_cleared(turn.allocation_operation_id)
    if turn.recovering_allocation and cleared:
        return
    _clear_stale_turn_handoffs(stage.workspace, stage.remote)
    execution.store.mark_campaign_handoffs_cleared(turn.allocation_operation_id)
    execution.store.record_agent_task_receipt(
        turn.allocation_operation_id,
        _HANDOFFS_CLEARED_RECEIPT,
        {
            "version": 1,
            "files": ["patch.json", "watch.json", CAMPAIGN_MAIL_HANDOFF_FILE],
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
            raise ValueError("Claimed campaign mail targets another campaign actor.")
        delivery = campaign_mail_delivery(
            campaign_id=turn.request.campaign_id,
            recipient_task_id=recipient,
            delivery_operation_id=turn.allocation_operation_id,
            messages=messages,
        )
        if turn.recovering_allocation and CAMPAIGN_MAIL_HANDOFF_FILE in mailbox.entry_names():
            retained = parse_campaign_mail_delivery(
                mailbox.read_text(
                    CAMPAIGN_MAIL_HANDOFF_FILE,
                    max_bytes=CAMPAIGN_MAIL_MAX_BYTES,
                )
            )
            if retained != delivery:
                raise ValueError("Retained campaign mail differs from its durable claimed batch.")
        else:
            stage_campaign_mail_delivery(mailbox, delivery)
        return str(stage.workspace / CAMPAIGN_MAIL_HANDOFF_FILE)
    mailbox.remove(CAMPAIGN_MAIL_HANDOFF_FILE)
    if turn.request.wake_cause == "message" and not turn.recovering_allocation:
        raise ValueError("Campaign message wake has no mail claimed by this paid allocation.")
    return None


def _worker_reply_key(turn: _CanonicalWorkerTurn) -> str:
    digest = hashlib.sha256(
        (
            "campaign-worker-reply\0"
            + turn.request.campaign_id
            + "\0"
            + turn.allocation_operation_id
        ).encode("utf-8")
    ).hexdigest()
    return f"worker-reply-{digest[:32]}"


def _orchestrator_prompt(
    execution: AgentTaskExecution,
    turn: _CanonicalOrchestratorTurn,
    *,
    context: ChatContext,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    token: str,
    patch_path: str,
    schema_path: str,
    validator_command: str,
    command_client: str,
    messages_path: str | None,
) -> tuple[str, str]:
    repositories = [
        {"alias": item.alias, "host": item.host, "path": item.path} for item in context.repositories
    ]
    if execution.continuation == "fresh":
        instruction_path = None
        if turn.request.instruction:
            instruction_digest = hashlib.sha256(
                turn.request.instruction.encode("utf-8")
            ).hexdigest()[:16]
            instruction_path = _stage_or_reuse_task_input(
                local_stage,
                remote_stage,
                f"campaign-starting-instruction-{instruction_digest}.txt",
                turn.request.instruction + "\n",
            )
        contract = campaign_orchestrator_task_contract(
            project_name=context.project_name,
            graph_path=context.graph_path,
            research_path=context.research_md_path,
            repositories=repositories,
            patch_path=patch_path,
            output_schema_path=schema_path,
            validator_command=validator_command,
            command_client=command_client,
            instruction_path=instruction_path,
            messages_path=messages_path,
        )
        role = "campaign_orchestrator"
    else:
        retry_diagnostics_path = (
            _stage_json_task_input(
                local_stage,
                remote_stage,
                f"task-{token}-retry-diagnostics.json",
                {"prior_attempt_diagnostics": list(execution.retry_feedback)},
            )
            if execution.continuation == "retry"
            else None
        )
        contract = campaign_orchestrator_continuation_contract(
            original_contract_path=_parent_task_contract_path(
                execution,
                local_stage,
                remote_stage,
            ),
            mode=(
                "resume"
                if execution.continuation == "resume"
                else "retry"
                if execution.continuation == "retry"
                else "continuation"
            ),
            graph_path=context.graph_path,
            research_path=context.research_md_path,
            repositories=repositories,
            patch_path=patch_path,
            output_schema_path=schema_path,
            validator_command=validator_command,
            command_client=command_client,
            messages_path=messages_path,
            retry_diagnostics_path=retry_diagnostics_path,
        )
        role = f"campaign_orchestrator_{execution.continuation}"
    return _stage_task_contract(
        local_stage,
        remote_stage,
        f"task-{token}-campaign-orchestrator.md",
        contract,
        execution=execution,
        role=role,
    )


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
    validator_command: str,
    reply_command: str,
    messages_path: str | None,
) -> tuple[str, str]:
    actor = execution.store.agent_task(turn.binding.actor_operation_id)
    if actor is None:
        raise ValueError("Campaign worker origin task is missing.")
    actor_request = CampaignRunRequest.model_validate(actor.request)
    if not actor_request.instruction:
        raise ValueError("Campaign worker origin has no durable instruction.")
    instruction_digest = hashlib.sha256(actor_request.instruction.encode("utf-8")).hexdigest()[:16]
    instruction_path = _stage_or_reuse_task_input(
        local_stage,
        remote_stage,
        f"campaign-worker-instruction-{instruction_digest}.txt",
        actor_request.instruction + "\n",
    )
    node = service.history.state().nodes.get(turn.binding.control_node_id or "")
    if node is None or node.type not in {"experiment", "blocker"}:
        raise ValueError("Campaign worker seat is no longer an Experiment or Blocker.")
    repositories = [
        {"alias": item.alias, "host": item.host, "path": item.path} for item in context.repositories
    ]
    if execution.continuation == "fresh":
        contract = campaign_worker_task_contract(
            project_name=context.project_name,
            seat_node_type="Experiment" if node.type == "experiment" else "Blocker",
            seat_node_id=node.id,
            seat_difficulty=json.dumps(node.model_dump(mode="json"), ensure_ascii=False, indent=2),
            instruction_path=instruction_path,
            graph_path=context.graph_path,
            research_path=context.research_md_path,
            repositories=repositories,
            patch_path=patch_path,
            output_schema_path=schema_path,
            validator_command=validator_command,
            reply_command=reply_command,
            messages_path=messages_path,
        )
        role = "campaign_worker"
    else:
        retry_diagnostics_path = (
            _stage_json_task_input(
                local_stage,
                remote_stage,
                f"task-{token}-retry-diagnostics.json",
                {"prior_attempt_diagnostics": list(execution.retry_feedback)},
            )
            if execution.continuation == "retry"
            else None
        )
        contract = campaign_worker_continuation_contract(
            original_contract_path=_parent_task_contract_path(execution, local_stage, remote_stage),
            mode=(
                "resume"
                if execution.continuation == "resume"
                else "retry"
                if execution.continuation == "retry"
                else "continuation"
            ),
            graph_path=context.graph_path,
            research_path=context.research_md_path,
            repositories=repositories,
            patch_path=patch_path,
            output_schema_path=schema_path,
            validator_command=validator_command,
            reply_command=reply_command,
            messages_path=messages_path,
            retry_diagnostics_path=retry_diagnostics_path,
        )
        role = f"campaign_worker_{execution.continuation}"
    return _stage_task_contract(
        local_stage,
        remote_stage,
        f"task-{token}-campaign-worker.md",
        contract,
        execution=execution,
        role=role,
    )


async def _serve_worker_commands(
    staged: StagedCommandMailbox,
    *,
    execution: AgentTaskExecution,
    turn: _CanonicalWorkerTurn,
    dispatcher: CampaignCommandDispatcher,
    stop: asyncio.Event,
    expected_turn_id: str,
) -> None:
    await _serve_campaign_commands(
        staged,
        execution=execution,
        turn=turn,
        dispatcher=dispatcher,
        stop=stop,
        expected_turn_id=expected_turn_id,
    )


async def _serve_campaign_commands(
    staged: StagedCommandMailbox,
    *,
    execution: AgentTaskExecution,
    turn: _CanonicalWorkerTurn | _CanonicalOrchestratorTurn,
    dispatcher: CampaignCommandDispatcher,
    stop: asyncio.Event,
    expected_turn_id: str,
) -> None:
    async def handle(
        request: CommandRequest,
        identity: CommandTurnIdentity,
    ) -> CommandResponse:
        if (
            identity.campaign_id != turn.request.campaign_id
            or identity.task_id != execution.operation_id
            or identity.turn_id != expected_turn_id
        ):
            return CommandResponse(
                request_id=request.request_id,
                status="invalid",
                message="Campaign command credential does not match this actor turn.",
            )
        return await asyncio.to_thread(dispatcher.dispatch, execution.operation_id, request)

    await serve_command_mailbox(
        staged=staged,
        handler=handle,
        stop=stop,
        invocation_gate=staged.invocation_gate,
    )


class _ValidateOnlyCampaignCommandDispatcher(CampaignCommandDispatcher):
    """Keep dispatcher ledger semantics while denying every correction side effect."""

    def dispatch(self, operation_id: str, request: CommandRequest) -> CommandResponse:
        if not isinstance(request, ValidateCommandRequest):
            context = self._context(operation_id)
            invocation = self.store.start_agent_command(
                operation_id=operation_id,
                command_id=self._unused_command_id(request.request_id),
                campaign_id=context.campaign.campaign_id,
                verb=request.verb,
                idempotency_key=None,
                payload={
                    "request_id": request.request_id,
                    "arguments": request.arguments.model_dump(mode="json"),
                    "supplied_idempotency_key": request.idempotency_key,
                    "denied_by": "campaign_patch_correction_validate_only",
                },
            )
            return self._finish(
                invocation.command_id,
                request.request_id,
                CampaignCommandEffectResult(
                    status="invalid",
                    message=(
                        "Campaign graph-correction credentials authorize Patch validation only."
                    ),
                ),
            )
        return super().dispatch(operation_id, request)

    def _execute(
        self,
        context: CampaignCommandContext,
        request: CommandRequest,
        *,
        planned_worker_id: str | None,
        planned_message_id: str | None,
        planned_watcher_id: str | None,
    ) -> CampaignCommandEffectResult:
        if not isinstance(request, ValidateCommandRequest):
            raise CampaignCommandInvalid(
                "Campaign graph-correction credentials authorize Patch validation only."
            )
        return super()._execute(
            context,
            request,
            planned_worker_id=planned_worker_id,
            planned_message_id=planned_message_id,
            planned_watcher_id=planned_watcher_id,
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
            warning(f"Campaign command mailbox became unavailable: {serve_error}")
        if cleanup_error is not None:
            warning(f"Campaign command mailbox cleanup failed: {cleanup_error}")
        return

    if caller_cancelled is not None:
        if serve_error is not None and not isinstance(serve_error, asyncio.CancelledError):
            warning(f"Campaign command mailbox became unavailable: {serve_error}")
        if cleanup_error is not None and not isinstance(cleanup_error, asyncio.CancelledError):
            warning(f"Campaign command mailbox cleanup failed: {cleanup_error}")
        raise caller_cancelled

    if serve_error is not None:
        if isinstance(serve_error, expected_errors):
            warning(f"Campaign command mailbox became unavailable: {serve_error}")
        else:
            if cleanup_error is not None:
                warning(f"Campaign command mailbox cleanup failed: {cleanup_error}")
            raise serve_error
    if cleanup_error is not None:
        if isinstance(cleanup_error, expected_errors):
            warning(f"Campaign command mailbox cleanup failed: {cleanup_error}")
        else:
            raise cleanup_error


async def _settle_worker_patch(
    service: ProjectService,
    launcher: AgentLauncher,
    execution: AgentTaskExecution,
    turn: _CanonicalWorkerTurn | _CanonicalOrchestratorTurn,
    stage: _WorkerStage,
    *,
    contract_path: str,
    patch_path: str,
    schema_path: str,
    read_dirs: list[Path],
    write_dirs: list[Path],
    provider_binary: str | None,
    native_session_id: str,
    retry_patch_digest: str | None,
    command_dispatcher: CampaignCommandDispatcher,
    _actor_role: Literal["worker", "orchestrator"] = "worker",
    _profile: Literal["ordinary", "orchestrator"] = "ordinary",
    _capability: Literal["work_auto", "orchestrate"] = "work_auto",
) -> _PatchSettlement:
    try:
        patch_text = _read_chat_patch(stage.workspace, stage.remote)
    except (OSError, StateUnavailable, ValueError) as exc:
        patch_text = None
        failure: _WorkPatchFailure | None = _WorkPatchFailure(
            f"The campaign {_actor_role} wrote a patch file that could not be read: {exc}",
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
        return _PatchSettlement(GraphUpdateResult(status="none"))

    correction_rounds = 0
    correction_frames: list[str] = []
    while True:
        if patch_text is not None:
            try:
                result, failure = _apply_work_patch(
                    service,
                    execution,
                    patch_text,
                    run_truth_scope=turn.request.run_truth_scope
                    or service.manifest.agent.default_run_truth_scope,
                    patch_kind="work",
                    profile=_profile,
                )
            except RunLockCancelled:
                correction_frames.append(
                    _sse(
                        AgentEvent(
                            event="paused",
                            text=(
                                f"Paused while waiting for canonical state. The campaign "
                                f"{_actor_role} "
                                "answer and retained patch are preserved."
                            ),
                        )
                    )
                )
                return _PatchSettlement(None, tuple(correction_frames))
            if result is not None:
                return _PatchSettlement(
                    result.model_copy(update={"correction_rounds": correction_rounds}),
                    tuple(correction_frames),
                )
        assert failure is not None
        if (
            not failure.correctable
            or correction_rounds >= _MAX_CORRECTION_ROUNDS
            or not native_session_id
        ):
            rejected = GraphUpdateResult(
                status="rejected",
                change_summary=list(failure.change_summary),
                proposal_ids=list(failure.proposal_ids),
                validation_messages=_bounded_graph_messages(failure.message),
                correction_rounds=correction_rounds,
                repairable=False,
            )
            _record_work_graph_rejection(execution, rejected)
            return _PatchSettlement(rejected, tuple(correction_frames))

        correction_rounds += 1
        execution.store.record_agent_task_receipt(
            execution.operation_id,
            "patch_correction_requested",
            {"round": correction_rounds, "problem": failure.message[:400]},
            tier="diagnostic",
        )
        execution.store.update_agent_task_message(
            execution.operation_id,
            f"Correcting campaign {_actor_role} graph reflection.",
            phase="correcting",
            event=True,
        )
        token = _task_token(execution)
        diagnostics_path = _stage_json_task_input(
            stage.local,
            stage.remote,
            f"task-{token}-campaign-work-correction-{correction_rounds}.json",
            {"kind": "work", "problem": failure.message},
        )
        correction_mailbox = stage_command_mailbox(
            local_stage=stage.local,
            remote_stage=stage.remote,
            campaign_id=turn.request.campaign_id,
            task_id=execution.operation_id,
            turn_id=(
                f"{execution.operation_id}:{_actor_role}-patch-correction:{correction_rounds}"
            ),
        )
        async with _worker_mailbox_lifecycle(
            correction_mailbox,
            execution=execution,
            start=lambda stop, mailbox=correction_mailbox, round_number=correction_rounds: (
                _serve_worker_commands if _actor_role == "worker" else _serve_campaign_commands
            )(
                mailbox,
                execution=execution,
                turn=turn,
                dispatcher=_ValidateOnlyCampaignCommandDispatcher(
                    command_dispatcher.store,
                    command_dispatcher.effects,
                ),
                stop=stop,
                expected_turn_id=(
                    f"{execution.operation_id}:{_actor_role}-patch-correction:{round_number}"
                ),
            ),
        ):
            correction_validator_command = correction_mailbox.client_command("validate", patch_path)
            correction_contract = PromptFactory.continuation_task_contract(
                original_contract_path=contract_path,
                mode="work_patch_correction",
                patch_path=patch_path,
                diagnostics_path=diagnostics_path,
                validator_command=correction_validator_command,
                output_schema_path=schema_path,
            )
            correction_path, correction_prompt = _stage_task_contract(
                stage.local,
                stage.remote,
                f"task-{token}-campaign-work-correction-{correction_rounds}.md",
                correction_contract,
                execution=execution,
                role=f"campaign_{_actor_role}_patch_correction_{correction_rounds}",
            )
            pre_launch_digest = _existing_patch_digest(stage.workspace, stage.remote)
            _record_agent_launch_receipt(
                execution,
                cast(RunRequest, turn.request),
                prompt=correction_prompt,
                contract_path=correction_path,
                remote=bool(stage.execution_host),
                resumed=True,
                continuation="graph_correction",
                extra={
                    "surface": "campaign",
                    "role": _actor_role,
                    "profile": _profile,
                    "capability": _capability,
                    "network_access": True,
                    "launch_kind": "graph_correction",
                    "correction_round": correction_rounds,
                    "write_directory_count": len(write_dirs),
                    "canonical_state_boundary": "prompt_only",
                    "repeat_operational_work": False,
                },
            )
            correction_outcome = _ProviderOutcome(session_id=native_session_id)
            correction_error: str | None = None
            async with aclosing(
                _stream_agent_events(
                    launcher,
                    cast(RunRequest, turn.request),
                    correction_prompt,
                    workspace=stage.workspace,
                    session_id=native_session_id,
                    read_dirs=read_dirs,
                    write_dirs=write_dirs,
                    execution_host=stage.execution_host,
                    execution=execution,
                    remote_stage=stage.remote,
                    capability=_capability,
                    outcome=correction_outcome,
                    binary=provider_binary,
                    invocation_gate=correction_mailbox.invocation_gate,
                )
            ) as stream:
                async for frame in stream:
                    event = AgentEvent.model_validate_json(frame.removeprefix("data: ").strip())
                    if event.event == "error":
                        correction_error = (
                            event.text or f"Campaign {_actor_role} Patch correction failed."
                        )
                    else:
                        correction_frames.append(frame)
        if correction_outcome.paused:
            return _PatchSettlement(None, tuple(correction_frames))
        if (
            correction_error is not None
            or correction_outcome.failed
            or not correction_outcome.completed
            or correction_outcome.session_id != native_session_id
        ):
            failure = _WorkPatchFailure(
                correction_error
                or (
                    f"Campaign {_actor_role} Patch correction did not complete in its saved "
                    "session."
                ),
                correctable=True,
                change_summary=failure.change_summary,
                proposal_ids=failure.proposal_ids,
            )
            patch_text = None
            correction_rounds = _MAX_CORRECTION_ROUNDS
            continue
        corrected: _CorrectionPatchRead = _read_correction_patch(
            stage.workspace,
            stage.remote,
            pre_launch_digest=pre_launch_digest,
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
        elif corrected.problem == "unchanged":
            failure = _WorkPatchFailure(
                f"{failure.message} The correction left patch.json byte-identical.",
                correctable=True,
                change_summary=failure.change_summary,
                proposal_ids=failure.proposal_ids,
            )
            patch_text = None
        else:
            assert corrected.text is not None
            patch_text = corrected.text


async def _settle_orchestrator_patch(
    service: ProjectService,
    launcher: AgentLauncher,
    execution: AgentTaskExecution,
    turn: _CanonicalOrchestratorTurn,
    stage: _WorkerStage,
    *,
    contract_path: str,
    patch_path: str,
    schema_path: str,
    read_dirs: list[Path],
    write_dirs: list[Path],
    provider_binary: str | None,
    native_session_id: str,
    retry_patch_digest: str | None,
    command_dispatcher: CampaignCommandDispatcher,
) -> _PatchSettlement:
    return await _settle_worker_patch(
        service,
        launcher,
        execution,
        turn,
        stage,
        contract_path=contract_path,
        patch_path=patch_path,
        schema_path=schema_path,
        read_dirs=read_dirs,
        write_dirs=write_dirs,
        provider_binary=provider_binary,
        native_session_id=native_session_id,
        retry_patch_digest=retry_patch_digest,
        command_dispatcher=command_dispatcher,
        _actor_role="orchestrator",
        _profile="orchestrator",
        _capability="orchestrate",
    )
