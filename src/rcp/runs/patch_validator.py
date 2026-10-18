from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from rcp.agents.command_mailbox import (
    CommandHandler,
    CommandRejectionRecorder,
    CommandTurnIdentity,
    StagedCommandMailbox,
    prepare_command_mailbox,
    serve_command_mailbox,
    stage_command_mailbox,
)
from rcp.agents.command_protocol import (
    CommandRequest,
    CommandResponse,
    MergeAnswersCommandRequest,
    ValidateCommandRequest,
    staged_command_client_source,
)
from rcp.background import AgentTaskExecution
from rcp.history import RevisionConflict
from rcp.limits import (
    COMMAND_CLIENT_WAIT_SECONDS,
    COMPUTE_COMMAND_TIMEOUT_SECONDS,
    PATCH_SELF_CHECK_MAX_COUNT,
    PATCH_SELF_CHECK_POLL_SECONDS,
)
from rcp.runs import merge_patch
from rcp.runs.merge_contract import (
    MergeAnswers,
    MergeAnswersRejected,
    MergeCheckReceipt,
    MergeHiddenClash,
    MergePlan,
    MergeRefused,
)
from rcp.transport import RemoteRunStage, RunStageMailbox, StateUnavailable

if TYPE_CHECKING:
    from rcp.history import BranchHistoryManager, HistoryManager

# Compatibility export for callers that assert which tested source was staged.
# The executable itself lives in one stdlib-only module; there is no handwritten copy.
VALIDATOR_CLIENT_SOURCE = staged_command_client_source()


class PatchValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["valid", "invalid", "unavailable"]
    messages: list[str] = Field(default_factory=list)
    live_revision: int | None = Field(default=None, ge=0)
    candidate_revision: int | None = Field(default=None, ge=0)


@dataclass
class PatchValidationBudget:
    count: int = 0
    requests: dict[str, int] = field(default_factory=dict)

    def reserve(self, request_id: str) -> int:
        if request_id not in self.requests:
            self.count += 1
            self.requests[request_id] = self.count
        return self.requests[request_id]


@dataclass
class MergeAnswersCheck:
    """Turn-bound dry preparation; successful checks bind the exact file bytes.

    Serve this handler with validate=None and budget=None. Merge answers have no
    Patch-check budget, and cannot invoke the ordinary Patch validator.
    """

    plan: MergePlan
    history: HistoryManager | BranchHistoryManager
    task_id: str
    turn_id: str
    execution: AgentTaskExecution | None = None
    receipts: list[MergeCheckReceipt] = field(default_factory=list, init=False)
    hidden_clash: MergeHiddenClash | None = field(default=None, init=False)
    refusal: MergeRefused | None = field(default=None, init=False)
    unavailable: str | None = field(default=None, init=False)
    destination_moved: bool = field(default=False, init=False)
    diagnostic: str | None = field(default=None, init=False)
    internal_error: str | None = field(default=None, init=False)
    latest_answers: MergeAnswers | None = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _generation: int = field(default=0, init=False, repr=False)
    _pending_checks: int = field(default=0, init=False, repr=False)

    def set_plan(self, plan: MergePlan, *, turn_id: str) -> None:
        with self._lock:
            self.plan = plan
            self.turn_id = turn_id
            self._generation += 1
            self._pending_checks = 0
            self.receipts.clear()
            self.hidden_clash = None
            self.refusal = None
            self.unavailable = None
            self.destination_moved = False
            self.diagnostic = None
            self.internal_error = None
            self.latest_answers = None

    def _receipt(self, content: bytes) -> MergeCheckReceipt:
        return MergeCheckReceipt(
            task_id=self.task_id,
            turn_id=self.turn_id,
            plan_id=self.plan.plan_id,
            answers_digest=hashlib.sha256(content).hexdigest(),
            source_head=self.plan.source_head,
            destination_head=self.plan.destination_head,
            preparation_contract=self.plan.preparation_contract,
        )

    def require_receipt(self, content: bytes) -> MergeCheckReceipt:
        with self._lock:
            if self.internal_error:
                raise ValueError(self.internal_error)
            if self._pending_checks:
                raise StateUnavailable(
                    "The merge answers check is still running; its result is unavailable."
                )
            expected = self._receipt(content)
            if expected in self.receipts:
                return expected
            if self.refusal is not None:
                raise self.refusal
            if self.hidden_clash is not None:
                raise self.hidden_clash
            if self.unavailable:
                raise StateUnavailable(self.unavailable)
            raise ValueError(
                "Collected answers do not match a successful check for this task, turn, and plan."
                + (f" {self.diagnostic}" if self.diagnostic else "")
            )

    def __call__(self, request: CommandRequest, identity: CommandTurnIdentity) -> CommandResponse:
        with self._lock:
            plan, turn_id, generation = self.plan, self.turn_id, self._generation
            if (
                not isinstance(request, MergeAnswersCommandRequest)
                or identity.task_id != self.task_id
                or identity.turn_id != turn_id
                or plan.merge_task_id != self.task_id
            ):
                return CommandResponse(
                    request_id=request.request_id,
                    status="invalid",
                    message="This command requires the current merge answers check turn.",
                )
            self._pending_checks += 1
        try:
            return self._check(request, plan, turn_id, generation)
        except Exception as exc:
            with self._lock:
                if self._generation != generation:
                    return CommandResponse(
                        request_id=request.request_id,
                        status="unavailable",
                        message="The merge plan or turn changed during this check; check again.",
                    )
                self.internal_error = (
                    f"Merge answers check failed internally ({type(exc).__name__}): {exc}"
                )
                self.diagnostic = self.internal_error
                return CommandResponse(
                    request_id=request.request_id,
                    status="invalid",
                    message=self.internal_error[:2_000],
                    result={"outcome": "internal_error"},
                )
        finally:
            with self._lock:
                if self._generation == generation:
                    self._pending_checks -= 1

    def _check(
        self,
        request: MergeAnswersCommandRequest,
        plan: MergePlan,
        turn_id: str,
        generation: int,
    ) -> CommandResponse:
        failure = None
        answers = None
        try:
            answers = MergeAnswers.model_validate_json(request.arguments.answers)
            merge_patch.prepare_merge_candidate(plan, answers, self.history)
        except (
            ValidationError,
            MergeAnswersRejected,
            MergeHiddenClash,
            MergeRefused,
            RevisionConflict,
            OSError,
            StateUnavailable,
        ) as exc:
            failure = exc
        with self._lock:
            if self._generation != generation or self.plan is not plan or self.turn_id != turn_id:
                return CommandResponse(
                    request_id=request.request_id,
                    status="unavailable",
                    message="The merge plan or turn changed during this check; check again.",
                )
            self.unavailable = None
            self.destination_moved = isinstance(failure, RevisionConflict)
            self.diagnostic = None
            if isinstance(failure, (OSError, StateUnavailable, RevisionConflict)):
                self.unavailable = str(failure) or "Merge answers preparation is unavailable."
                return CommandResponse(
                    request_id=request.request_id,
                    status="unavailable",
                    message=self.unavailable[:2_000],
                )
            if failure is not None:
                self.diagnostic = str(failure)
                result: dict[str, object] = {}
                if isinstance(failure, MergeHiddenClash):
                    result["outcome"] = failure.outcome
                    self.hidden_clash = failure
                    result["promotion"] = failure.promotion.model_dump(mode="json")
                elif isinstance(failure, MergeRefused):
                    result["outcome"] = failure.outcome
                    self.refusal = failure
                else:
                    result["outcome"] = MergeAnswersRejected.outcome
                if isinstance(failure, (MergeAnswersRejected, MergeRefused)):
                    result["diagnostics"] = [
                        item.model_dump(mode="json") for item in failure.diagnostics
                    ]
                return CommandResponse(
                    request_id=request.request_id,
                    status="invalid",
                    message=self.diagnostic[:2_000] or "Merge answers were rejected.",
                    result=result,
                )
            receipt = self._receipt(request.arguments.answers.encode("utf-8"))
            if self.execution is not None:
                try:
                    self.execution.store.record_agent_task_receipt(
                        self.execution.operation_id,
                        "merge_answers_check",
                        receipt.model_dump(mode="json"),
                        tier="diagnostic",
                    )
                except (OSError, StateUnavailable) as exc:
                    self.unavailable = str(exc) or "Merge check receipt could not be recorded."
                    return CommandResponse(
                        request_id=request.request_id,
                        status="unavailable",
                        message=self.unavailable[:2_000],
                    )
            self.receipts.append(receipt)
            self.hidden_clash = None
            self.refusal = None
            assert answers is not None
            self.latest_answers = answers.model_copy(deep=True)
            return CommandResponse(
                request_id=request.request_id,
                status="ok",
                result={"receipt": receipt.model_dump(mode="json")},
            )


def stage_patch_validation_mailbox(
    *,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    local_input_stage: Path | None = None,
    task_id: str,
    turn_id: str,
    timeout_seconds: float,
    authority: Literal["validate_only", "broker"] = "validate_only",
    episode_id: str | None = None,
    ask_wait_seconds: float = COMMAND_CLIENT_WAIT_SECONDS,
    shell_timeout_seconds: int | None = None,
) -> StagedCommandMailbox:
    """Stage the command client with the authority selected by its concrete owner."""

    if authority == "broker":
        # Broker turns also serve compute launches, which outlast a Patch check.
        timeout_seconds = max(timeout_seconds, COMPUTE_COMMAND_TIMEOUT_SECONDS)
    return stage_command_mailbox(
        local_stage=local_stage,
        remote_stage=remote_stage,
        local_input_stage=local_input_stage,
        episode_id=episode_id,
        ask_wait_seconds=ask_wait_seconds,
        shell_timeout_seconds=shell_timeout_seconds,
        authority=authority,
        task_id=task_id,
        turn_id=turn_id,
        timeout_seconds=timeout_seconds,
    )


def prepare_patch_validation_mailbox(
    *,
    mailbox_id: str,
    workspace: Path,
    remote_stage: RemoteRunStage | None,
) -> None:
    """Compatibility hook for the Work call site while its serialized edit is pending."""

    if len(mailbox_id) != 32 or any(
        character not in "0123456789abcdef" for character in mailbox_id
    ):
        raise ValueError("validator mailbox id is malformed")
    mailbox = RunStageMailbox.for_stage(
        local_stage=workspace if remote_stage is None else None,
        remote_stage=remote_stage,
    )
    prepare_command_mailbox(mailbox=mailbox)


async def serve_patch_validation_mailbox(
    *,
    staged: StagedCommandMailbox,
    execution: AgentTaskExecution | None,
    validate: Callable[[str], PatchValidationResult] | None,
    stop: asyncio.Event | threading.Event,
    budget: PatchValidationBudget | None,
    command_handler: CommandHandler | None = None,
    responses: dict[str, CommandResponse] | None = None,
    terminal: dict[str, str] | None = None,
    checkpoint: Callable[[], None] | None = None,
    suspend: threading.Event | None = None,
) -> None:
    """Serve owner commands and optional bounded Patch checks over one mailbox."""
    if validate is not None and budget is None:
        raise ValueError("Patch validation requires a self-check budget")

    async def handle(
        request: CommandRequest,
        _identity: CommandTurnIdentity,
    ) -> CommandResponse:
        if validate is None or not isinstance(request, ValidateCommandRequest):
            if command_handler is not None:
                return await asyncio.to_thread(command_handler, request, _identity)
            return CommandResponse(
                request_id=request.request_id,
                status="invalid",
                message="This validator credential authorizes Patch validation only.",
            )
        assert budget is not None
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
        _record_self_check(execution, count, result)
        return _command_response(request.request_id, result)

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
        _record_mailbox_unavailable(execution, str(exc))


def command_transport_recorder(execution: AgentTaskExecution | None):
    """One event per outage boundary, never one event per failed poll."""
    if execution is None:
        return None

    def record(status: str, message: str) -> None:
        execution.store.record_agent_task_event(
            execution.operation_id,
            f"Command mailbox {status}: {' '.join(message.split())[:400]}",
            level="info" if status == "recovered" else "warning",
        )

    return record


def command_rejection_recorder(
    execution: AgentTaskExecution | None,
) -> CommandRejectionRecorder | None:
    """Record command refusals the handler never saw in this task's history."""

    if execution is None:
        return None

    def record(status: str, message: str) -> None:
        execution.store.record_agent_task_event(
            execution.operation_id,
            f"RCP command {status} before dispatch: {' '.join(message.split())[:400]}",
            level="warning",
        )

    return record


def cleanup_patch_validation_mailbox(
    *,
    staged: StagedCommandMailbox,
    execution: AgentTaskExecution | None,
) -> None:
    """Remove unified command receipts while keeping cleanup failure diagnostic-only."""

    try:
        staged.cleanup()
    except (OSError, StateUnavailable, ValueError) as exc:
        _record_mailbox_unavailable(execution, f"mailbox cleanup failed: {exc}")


def _command_response(request_id: str, result: PatchValidationResult) -> CommandResponse:
    status = {
        "valid": "ok",
        "invalid": "invalid",
        "unavailable": "unavailable",
    }[result.status]
    diagnostic = " ".join(message.strip() for message in result.messages if message.strip())
    if status != "ok" and not diagnostic:
        diagnostic = f"Patch validation was {result.status}."
    return CommandResponse(
        request_id=request_id,
        status=status,
        message=diagnostic[:2_000] or None,
        result=result.model_dump(mode="json"),
    )


def _record_self_check(
    execution: AgentTaskExecution | None,
    count: int,
    result: PatchValidationResult,
) -> None:
    if execution is None:
        return
    execution.store.record_agent_task_event(
        execution.operation_id,
        (
            f"Patch self-check {count}/{PATCH_SELF_CHECK_MAX_COUNT}: "
            f"{result.status}"
            + (
                f" against live graph revision {result.live_revision}"
                if result.live_revision is not None
                else ""
            )
            + "."
        ),
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


def _record_mailbox_unavailable(execution: AgentTaskExecution | None, detail: str) -> None:
    if execution is None:
        return
    execution.store.record_agent_task_event(
        execution.operation_id,
        f"Patch validator became unavailable: {' '.join(detail.split())[:400]}",
        level="warning",
    )
