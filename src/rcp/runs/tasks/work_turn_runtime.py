from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from rcp.agents import AgentEvent, AgentLauncher
from rcp.agents.command_mailbox import StagedCommandMailbox
from rcp.agents.context import ChatContext
from rcp.agents.write_scope import ProjectWriteScope
from rcp.background import AgentTaskContinuation, AgentTaskExecution
from rcp.config import AgentSurface
from rcp.core.models import Patch
from rcp.history import PatchRejected, ReplayHalted
from rcp.runs.chat import _ChatPatchInputs
from rcp.runs.experiment_loop import StagedExperimentWatcherResource
from rcp.runs.patch_validator import (
    PatchValidationBudget,
    PatchValidationResult,
    serve_patch_validation_mailbox,
)
from rcp.runs.shared import (
    _ProviderOutcome,
    _record_patch_applied_receipt,
    _record_patch_receipt,
    _sse,
    _stream_agent_events,
)
from rcp.runs.tasks.result_views import ResultViewSnapshot, _PreparedResultView
from rcp.service import GraphUpdateResult, ProjectService, RunRequest
from rcp.skill_registry import SkillSelection
from rcp.storage import ResultViewRecord
from rcp.transport import RemoteRunStage, RunLockCancelled, StateUnavailable


@dataclass(frozen=True)
class PreparedWorkPatch:
    patch: Patch
    change_summary: tuple[str, ...]
    proposal_ids: tuple[str, ...]


@dataclass(frozen=True)
class DeliverableFailure:
    message: str
    correctable: bool
    change_summary: tuple[str, ...] = ()
    proposal_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CorrectionPatchRead:
    text: str | None
    problem: Literal["unreadable", "missing", "unchanged"] | None = None
    detail: str | None = None


@dataclass
class WorkValidatorMailboxLifecycle:
    staged: StagedCommandMailbox
    execution: AgentTaskExecution | None
    stop: asyncio.Event
    task: asyncio.Task[None]
    closed: bool = False

    async def close(self, *, primary_error: BaseException | None = None) -> None:
        if self.closed:
            return
        self.closed = True
        await close_work_validator_mailbox(
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
    validator_lifecycle: WorkValidatorMailboxLifecycle
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
    def retry_attempt(self) -> bool:
        return self.continuation in {"retry", "handoff"}

    @property
    def uses_master_protocol(self) -> bool:
        return (
            self.request.trigger in {"human", "orchestrator"}
            and self.request.patch_kind == "work"
            and not self.retry_attempt
        )


@dataclass(frozen=True)
class ResolvedWorkExecution:
    request: RunRequest
    execution_machine_alias: str
    execution_host: str
    provider_binary: str | None
    revision_preflight: tuple[ResultViewRecord, ResultViewSnapshot] | None


@dataclass(frozen=True)
class StagedWorkInputs:
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


@dataclass(frozen=True)
class ComposedWorkPrompt:
    contract_path: str
    prompt: str
    base_contract_path: str


@dataclass(frozen=True)
class RetryDeliverableBaseline:
    patch_digest: str | None
    watch_digest: str | None
    experiment_watch_digests: dict[str, str]


@dataclass
class SettledWorkDeliverables:
    native_session_id: str | None
    graph_update: GraphUpdateResult = field(
        default_factory=lambda: GraphUpdateResult(status="none")
    )
    watch_correction_rounds: int = 0
    stop: bool = False


@dataclass
class AppliedWorkTurn:
    graph_update: GraphUpdateResult
    native_session_id: str | None
    stop: bool = False


@dataclass(frozen=True)
class DeliverableRead:
    text: str | None
    failure: DeliverableFailure | None = None


@dataclass(frozen=True)
class DeliverableStep:
    failure: DeliverableFailure | None = None
    frames: tuple[str, ...] = ()
    stop: bool = False


@dataclass(frozen=True)
class GraphRepairPatchResult:
    patch_text: str | None = None
    graph_update: GraphUpdateResult | None = None
    frames: tuple[str, ...] = ()


_NEW_LOGICAL_WORK_TURN_CONTINUATIONS = frozenset(
    {
        "fresh",
        "handoff",
        "watcher_wake",
        "graph_condition_wake",
        "message_wake",
        "lifecycle_wake",
    }
)
_SAME_LOGICAL_WORK_TURN_CONTINUATIONS = frozenset({"resume", "retry", "graph_repair"})


def clears_stale_turn_handoffs(continuation: AgentTaskContinuation) -> bool:
    if continuation in _NEW_LOGICAL_WORK_TURN_CONTINUATIONS:
        return True
    if continuation in _SAME_LOGICAL_WORK_TURN_CONTINUATIONS:
        return False
    raise ValueError(f"Unsupported Work continuation: {continuation}")


async def stream_turn_agent_events(
    turn: WorkTurn,
    launcher: AgentLauncher,
    prompt: str,
    *,
    session_id: str | None,
    required_session_id: str | None = None,
    outcome: _ProviderOutcome,
    validator_staged: StagedCommandMailbox | None = None,
    validator_lifecycle: WorkValidatorMailboxLifecycle | None = None,
) -> AsyncIterator[str]:
    """Stream one provider continuation from a staged Work execution context."""

    async with aclosing(
        stream_work_agent_events(
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
            outcome=outcome,
            binary=turn.provider_binary,
            validator_staged=validator_staged or turn.patch_inputs.validator_staged,
            validator_lifecycle=(
                validator_lifecycle if validator_staged is not None else turn.validator_lifecycle
            ),
            required_session_id=required_session_id,
        )
    ) as stream:
        async for frame in stream:
            yield frame


async def stream_work_agent_events(
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
    outcome: _ProviderOutcome,
    binary: str | None,
    validator_staged: StagedCommandMailbox,
    validator_lifecycle: WorkValidatorMailboxLifecycle,
    required_session_id: str | None = None,
) -> AsyncIterator[str]:
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
                capability="work_auto",
                outcome=outcome,
                binary=binary,
                invocation_gate=validator_staged.invocation_gate,
                required_session_id=required_session_id,
            )
        ) as stream:
            async for frame in stream:
                yield frame
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        await validator_lifecycle.close(primary_error=primary_error)


def start_work_validator_mailbox(
    staged: StagedCommandMailbox,
    *,
    execution: AgentTaskExecution | None,
    budget: PatchValidationBudget,
    validate: Callable[[str], PatchValidationResult],
    serve: Callable[..., Awaitable[None]] = serve_patch_validation_mailbox,
) -> WorkValidatorMailboxLifecycle:
    stop = asyncio.Event()
    try:
        task = asyncio.create_task(
            serve(
                staged=staged,
                execution=execution,
                validate=validate,
                stop=stop,
                budget=budget,
            )
        )
    except BaseException:
        with suppress(BaseException):
            staged.cleanup()
        raise
    return WorkValidatorMailboxLifecycle(
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


async def close_work_validator_mailbox(
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


def validate_work_patch_live(
    service: ProjectService,
    patch_text: str,
    *,
    prepare_candidate: Callable[[str], PreparedWorkPatch],
    bounded_messages: Callable[..., list[str]],
) -> PatchValidationResult:
    """Run shared live-history validation around an owner-prepared candidate."""

    try:
        candidate = prepare_candidate(patch_text)
        prepared, report, state = service.history.validate_candidate(candidate.patch)
    except (ReplayHalted, StateUnavailable, OSError) as exc:
        return PatchValidationResult(status="unavailable", messages=[str(exc)])
    except ValueError as exc:
        return PatchValidationResult(status="invalid", messages=[str(exc)])
    rejects = [item.message for item in report.messages if item.level == "reject"]
    if rejects:
        return PatchValidationResult(
            status="invalid",
            messages=bounded_messages(*rejects),
            live_revision=state.revision,
            candidate_revision=prepared.revision,
        )
    return PatchValidationResult(
        status="valid",
        messages=bounded_messages(*(item.message for item in report.flags)),
        live_revision=state.revision,
        candidate_revision=prepared.revision,
    )


def apply_work_patch(
    service: ProjectService,
    execution: AgentTaskExecution | None,
    patch_text: str,
    *,
    prepare_candidate: Callable[[str], PreparedWorkPatch],
    source_operation_id: str | None,
    source_effect_id: str | None,
    canonical_matches: Callable[[Patch, Patch], bool],
    canonical_binding_error: str,
    rejected_patch_error: str,
    proposal_ids_for_patch: Callable[[Patch], list[str]],
    bounded_messages: Callable[..., list[str]],
    record_lock_wait: Callable[[str, str], None] | None = None,
    record_lock_lost: Callable[[str, str], None] | None = None,
) -> tuple[GraphUpdateResult | None, DeliverableFailure | None]:
    """Validate and atomically commit one candidate prepared by its concrete owner."""

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
    canonical_patch: Patch | None = None
    try:
        candidate = prepare_candidate(patch_text)
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
        workspace = service.history.workspace
        with workspace.run_lock(
            on_wait=(
                (lambda message: record_lock_wait(message, workspace.location))
                if record_lock_wait is not None
                else None
            ),
            on_lost=(
                (lambda message: record_lock_lost(message, workspace.location))
                if record_lock_lost is not None
                else None
            ),
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
                    if not canonical_matches(canonical_patch, patch):
                        raise ValueError(canonical_binding_error)
                    result = service.history.current_materialization()
                    appended = canonical_patch
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
        detail = "; ".join(messages) or str(exc) or rejected_patch_error
        if execution is not None:
            execution.store.record_agent_task_receipt(
                execution.operation_id,
                "patch_rejected",
                {"messages": [item.model_dump(mode="json") for item in exc.report.messages[:16]]},
                tier="diagnostic",
            )
        return None, DeliverableFailure(
            detail,
            correctable=True,
            change_summary=change_summary,
            proposal_ids=proposal_ids,
        )
    except (ReplayHalted, StateUnavailable) as exc:
        return None, DeliverableFailure(
            str(exc),
            correctable=False,
            change_summary=change_summary,
            proposal_ids=proposal_ids,
        )
    except ValueError as exc:
        return None, DeliverableFailure(
            str(exc),
            correctable=True,
            change_summary=change_summary,
            proposal_ids=proposal_ids,
        )

    if canonical_patch is not None:
        change_summary = tuple(canonical_patch.change_summary)
        proposal_ids = tuple(proposal_ids_for_patch(canonical_patch))
    report = result.reports[appended.revision]
    _record_patch_applied_receipt(execution, result.state)
    return (
        GraphUpdateResult(
            status="applied",
            applied_revision=appended.revision,
            change_summary=list(change_summary),
            proposal_ids=list(proposal_ids),
            validation_messages=bounded_messages(*(item.message for item in report.flags)),
        ),
        None,
    )


def read_correction_patch(
    read_patch: Callable[[], str | None],
    *,
    pre_launch_digest: str | None,
) -> CorrectionPatchRead:
    """Classify one correction round's patch output without applying owner policy."""

    try:
        corrected = read_patch()
    except (OSError, StateUnavailable, ValueError) as exc:
        return CorrectionPatchRead(text=None, problem="unreadable", detail=str(exc))
    if corrected is None:
        return CorrectionPatchRead(text=None, problem="missing")
    if (
        pre_launch_digest is not None
        and hashlib.sha256(corrected.encode("utf-8")).hexdigest() == pre_launch_digest
    ):
        return CorrectionPatchRead(text=None, problem="unchanged")
    return CorrectionPatchRead(text=corrected)


def settle_graph_repair_patch(
    outcome: _ProviderOutcome,
    *,
    provider: str,
    pre_launch_digest: str | None,
    read_patch: Callable[[], str | None],
    apply_patch: Callable[[str], tuple[GraphUpdateResult | None, DeliverableFailure | None]],
    bounded_messages: Callable[..., list[str]],
    record_rejection: Callable[[GraphUpdateResult], None],
) -> GraphRepairPatchResult:
    """Read and apply a repaired Patch after its owner-specific correction prompt."""

    if not outcome.completed:
        if outcome.failed or outcome.paused:
            return GraphRepairPatchResult()
        return GraphRepairPatchResult(
            frames=(_sse(AgentEvent(event="error", text=f"{provider} produced no result.")),)
        )
    try:
        patch_text = read_patch()
    except (OSError, StateUnavailable, ValueError) as exc:
        return GraphRepairPatchResult(
            frames=(
                _sse(
                    AgentEvent(
                        event="error",
                        text=f"The repaired patch could not be read: {exc}",
                    )
                ),
            )
        )
    if patch_text is None:
        return GraphRepairPatchResult(
            frames=(_sse(AgentEvent(event="error", text="The repair did not write patch.json.")),)
        )
    if (
        pre_launch_digest is not None
        and hashlib.sha256(patch_text.encode("utf-8")).hexdigest() == pre_launch_digest
    ):
        return GraphRepairPatchResult(
            frames=(
                _sse(
                    AgentEvent(
                        event="error",
                        text="The repair left patch.json byte-identical to the rejected patch.",
                    )
                ),
            )
        )
    try:
        graph_update, failure = apply_patch(patch_text)
    except RunLockCancelled:
        return GraphRepairPatchResult(
            frames=(
                _sse(
                    AgentEvent(
                        event="paused",
                        text=(
                            "Paused while waiting for canonical state. The retained patch is "
                            "preserved."
                        ),
                    )
                ),
            )
        )
    if graph_update is None:
        assert failure is not None
        graph_update = GraphUpdateResult(
            status="rejected",
            change_summary=list(failure.change_summary),
            proposal_ids=list(failure.proposal_ids),
            validation_messages=bounded_messages(failure.message),
            correction_rounds=1,
        )
        record_rejection(graph_update)
    return GraphRepairPatchResult(patch_text=patch_text, graph_update=graph_update)


# The concrete owners historically exposed these private names to their focused
# helpers and tests. Keep that vocabulary while moving the shared values here.
_AppliedWorkTurn = AppliedWorkTurn
_ComposedWorkPrompt = ComposedWorkPrompt
_CorrectionPatchRead = CorrectionPatchRead
_DeliverableFailure = DeliverableFailure
_DeliverableRead = DeliverableRead
_DeliverableStep = DeliverableStep
_PreparedWorkPatch = PreparedWorkPatch
_ResolvedWorkExecution = ResolvedWorkExecution
_RetryDeliverableBaseline = RetryDeliverableBaseline
_SettledWorkDeliverables = SettledWorkDeliverables
_StagedWorkInputs = StagedWorkInputs
_WorkValidatorMailboxLifecycle = WorkValidatorMailboxLifecycle
