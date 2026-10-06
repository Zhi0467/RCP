"""Nonblocking question handling, called only after a concrete owner authorizes ask."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.command_protocol import (
    ApplyCommandRequest,
    AskCommandRequest,
    AskResult,
    CommandRequest,
    CommandResponse,
    LessonCommandRequest,
)
from rcp.limits import ASK_MAX_ATTEMPTS
from rcp.runs.lesson_commands import handle_lesson
from rcp.storage import AppStore
from rcp.storage.question_models import QuestionArgumentConflict, QuestionOrigin

if TYPE_CHECKING:
    from rcp.background import AgentTaskExecution
    from rcp.runs.tasks.compute_commands import WorkComputeCommands
    from rcp.service import RunRequest


def handle_ask(
    store: AppStore,
    request: AskCommandRequest,
    origin: QuestionOrigin,
    *,
    parked: bool = False,
) -> CommandResponse:
    """Persist or read a question without granting authority or admitting follow-ups.

    The caller supplies a server-resolved origin after its own admission check.
    Repeated keys preserve the first origin even across turns. Parking only changes
    the outward pending state; resolution and confirmed client receipt are separate
    store operations. Producing this response does not prove the client received it.
    """
    try:
        question = store.create_or_get_question(
            origin=origin,
            key=request.idempotency_key,
            **request.arguments.model_dump(),
        )
    except QuestionArgumentConflict as exc:
        return CommandResponse(
            request_id=request.request_id,
            status="invalid",
            message=str(exc),
        )
    # A parked reply ends the wait at once, so it is neither an attempt nor a live wait.
    pending = question.state == "pending" and not parked
    if pending:
        attempt = store.question_activity.pending(question.question_id, request.call_id)
    else:
        attempt = None
        if question.state != "pending":
            store.question_activity.forget(question.question_id)
    result = AskResult(
        attempt=attempt,
        max_attempts=ASK_MAX_ATTEMPTS if pending else None,
        question_id=question.question_id,
        state="parked" if parked and question.state == "pending" else question.state,
        answer=question.answer if question.state == "answered" else None,
        choices=question.chosen_choices if question.state == "answered" else [],
    )
    return CommandResponse(
        request_id=request.request_id,
        status="ok",
        result=result.model_dump(exclude_none=True),
    )


def work_ask_authorized(execution: AgentTaskExecution | None) -> bool:
    """Concrete human Work owners only; neither capability nor episode id suffices."""
    if execution is None:
        return False
    from rcp.runs.consolidation import is_consolidation

    if is_consolidation(execution):
        return False
    store = execution.store
    task = store.agent_task(execution.operation_id)
    if task is None or task.kind not in {"node_chat", "project_chat"}:
        return False
    authority = task.dispatch_authority
    if (
        authority is None
        or authority.profile != "ordinary"
        or authority.task_contract != "work_auto"
        or task.authorized_by is None
        or store.auto_research_child_work_for_operation(task.operation_id) is not None
    ):
        return False
    if authority.scope.patch_kind == "experiment_loop":
        episode_id = task.episode_id
        if episode_id is None:
            return False
        current = store.episode(episode_id)
        if (
            current is None
            or current.status not in {"queued", "running"}
            or current.stop_requested_at is not None
            or current.ending is not None
        ):
            return False
        seen = set()
        while episode_id is not None and episode_id not in seen:
            seen.add(episode_id)
            episode = store.episode(episode_id)
            if (
                episode is None
                or episode.mode != "experiment_loop"
                or episode.authorized_by is None
                or store.auto_research_child_experiment(episode_id) is not None
            ):
                return False
            episode_id = episode.continues_episode_id
        return episode_id is None
    return authority.scope.patch_kind == "work" and bool(authority.scope.chat_id)


def discuss_ask_authorized(execution: AgentTaskExecution | None) -> bool:
    """Only human conversation Discuss turns may use the question broker."""
    if execution is None:
        return False
    task = execution.store.agent_task(execution.operation_id)
    if task is None or task.kind not in {"node_chat", "project_chat"}:
        return False
    authority = task.dispatch_authority
    return bool(
        authority is not None
        and authority.profile == "ordinary"
        and authority.task_contract == "discuss"
        and authority.scope.patch_kind is None
        and authority.scope.chat_id
        and task.request.get("mode") == "discuss"
        and task.request.get("artifact_edit") is None
        and task.authorized_by is not None
        and task.episode_id is None
        and task.write_scope_fingerprint is None
        and execution.store.auto_research_child_work_for_operation(task.operation_id) is None
    )


def discuss_command_handler(execution: AgentTaskExecution | None) -> WorkCommandHandler:
    """Discuss owns a single verb and never delegates to compute or graph handlers."""
    return WorkCommandHandler(
        execution, None, frozenset({"ask"}) if discuss_ask_authorized(execution) else frozenset()
    )


def question_origin(execution: AgentTaskExecution) -> QuestionOrigin:
    """Resolve after the provider session checkpoint, never from command arguments."""
    if not (work_ask_authorized(execution) or discuss_ask_authorized(execution)):
        raise ValueError("This turn does not authorize human questions.")
    task = execution.store.agent_task(execution.operation_id)
    assert task is not None and task.dispatch_authority is not None
    authority = task.dispatch_authority
    episode_id = task.episode_id if authority.scope.patch_kind == "experiment_loop" else None
    if (
        not task.native_session_id
        or not task.stage_root
        or (authority.task_contract != "discuss" and not task.write_scope_fingerprint)
    ):
        raise ValueError(
            "The asking turn has no complete native session and authority binding yet."
        )
    return QuestionOrigin(
        owner_kind="episode" if episode_id else "chat",
        project_id=task.project_id,
        owner_id=episode_id or authority.scope.chat_id,
        operation_id=task.operation_id,
        provider=task.request["provider"],
        native_session_id=task.native_session_id,
        stage_root=task.stage_root,
        stage_host=task.stage_host,
        capability=authority.task_contract,
        write_scope_fingerprint=task.write_scope_fingerprint,
        graph_target=task.graph_target,
    )


def require_question_origin_binding(
    execution: AgentTaskExecution,
    request: RunRequest,
    origin: QuestionOrigin,
    *,
    execution_host: str,
    stage_root: str | None,
    write_scope_fingerprint: str | None,
) -> None:
    """A follow-up may deliver its answer only under the asking turn's binding."""
    task = execution.store.agent_task(execution.operation_id)
    discuss = origin.capability == "discuss"
    if (
        task is None
        or task.dispatch_authority is None
        or task.dispatch_authority.task_contract != origin.capability
        or request.provider != origin.provider
        or task.request.get("provider") != origin.provider
        or request.session_id != origin.native_session_id
        or task.native_session_id not in {None, origin.native_session_id}
        or execution_host != (origin.stage_host or "")
        or stage_root != origin.stage_root
        or request.mode != ("discuss" if discuss else "work")
        or task.request.get("mode") != request.mode
        or task.graph_target != origin.graph_target
        or write_scope_fingerprint != origin.write_scope_fingerprint
        or (
            discuss
            and (
                task.write_scope_fingerprint is not None
                or task.dispatch_authority.scope.patch_kind is not None
            )
        )
        or (not discuss and not write_scope_fingerprint)
    ):
        raise ValueError("question_origin_binding_unavailable")


def record_work_question_receipts(execution: AgentTaskExecution) -> None:
    """Client acknowledgement plus successful full settlement confirms receipt.

    The client echoes a token only after consuming the answered response. An offered
    response or successful task alone is insufficient. Missing acknowledgements,
    failed, paused, or disconnected turns retain follow-up eligibility; recovery
    applies the same rule from durable acknowledgement receipts.
    """
    record_settled_question_receipts(execution.store, execution.operation_id)


def record_settled_question_receipts(store: AppStore, operation_id: str) -> None:
    """Replay the same receipt rule after a crash between settlement and callback."""
    task = store.agent_task(operation_id)
    if task is None or task.status != "succeeded":
        return
    for receipt in store.agent_task_receipts_by_category(
        operation_id, "question_answer_acknowledged"
    ):
        payload = receipt.payload
        received = store.record_question_receipt(
            payload["question_id"],
            answer_revision=payload["answer_revision"],
            operation_id=operation_id,
            request_id=payload["request_id"],
        )
        if not received:
            store.record_question_continuation_receipt(
                payload["question_id"],
                answer_revision=payload["answer_revision"],
                operation_id=operation_id,
                request_id=payload["request_id"],
            )


def reconcile_question_receipt(store: AppStore, question_id: str) -> None:
    """Replay successful original or same-binding continuation receipt after restart."""
    for operation_id in store.question_receipt_candidate_operations(question_id):
        record_settled_question_receipts(store, operation_id)


@dataclass(frozen=True)
class WorkCommandHandler:
    """Dispatch one owner's resolved verbs; questions share no compute authority."""

    execution: AgentTaskExecution | None
    compute_commands: WorkComputeCommands | None
    allowed_verbs: frozenset[str]
    consolidation_apply: (
        Callable[[ApplyCommandRequest, CommandTurnIdentity], CommandResponse] | None
    ) = None

    def __call__(self, request: CommandRequest, identity: CommandTurnIdentity) -> CommandResponse:
        def refuse(message: str) -> CommandResponse:
            return CommandResponse(request_id=request.request_id, status="invalid", message=message)

        if request.verb not in self.allowed_verbs:
            return refuse("This turn does not authorize that command.")
        if isinstance(request, LessonCommandRequest):
            execution = self.execution
            if (
                execution is None
                or identity.authority != "broker"
                or identity.task_id != execution.operation_id
            ):
                return refuse("Lessons require this Work turn's broker authority.")
            return handle_lesson(execution.store, execution.operation_id, request)
        if isinstance(request, ApplyCommandRequest) and self.consolidation_apply is not None:
            return self.consolidation_apply(request, identity)
        if isinstance(request, AskCommandRequest):
            execution = self.execution
            if (
                execution is None
                or identity.authority != "broker"
                or identity.task_id != execution.operation_id
            ):
                return refuse("Questions require this turn's broker authority.")
            try:
                origin = question_origin(execution)
            except ValueError as exc:
                return refuse(str(exc))
            current_episode_id = origin.owner_id if origin.owner_kind == "episode" else None
            # Keys survive turns and episode continuation, but an answer never
            # crosses to a different native session or authority binding.
            owner_ids = (
                execution.store.experiment_question_owner_ids(origin.owner_id)
                if origin.owner_kind == "episode"
                else [origin.owner_id]
            )
            for owner_id in owner_ids:
                previous = next(
                    (
                        item
                        for item in execution.store.list_questions(
                            project_id=origin.project_id,
                            owner_kind=origin.owner_kind,
                            owner_id=owner_id,
                        )
                        if item.key == request.idempotency_key
                    ),
                    None,
                )
                if previous is None:
                    continue
                binding_fields = {
                    "provider",
                    "native_session_id",
                    "stage_root",
                    "stage_host",
                    "capability",
                    "write_scope_fingerprint",
                    "graph_target",
                }
                if previous.origin.model_dump(include=binding_fields) != origin.model_dump(
                    include=binding_fields
                ):
                    return refuse("question_origin_binding_unavailable")
                origin = previous.origin
                break
            response = handle_ask(execution.store, request, origin)
            if current_episode_id is not None:
                episode = execution.store.episode(current_episode_id)
                if (
                    episode is None
                    or episode.status not in {"queued", "running"}
                    or episode.stop_requested_at is not None
                    or episode.ending is not None
                ):
                    # Close the create-vs-Stop race: a fence before insertion did
                    # not see this card. A later fence withdraws it itself.
                    execution.store.set_episode_questions_withdrawn(
                        origin.project_id, origin.owner_id, withdrawn=True
                    )
                    return refuse("question_episode_ended")
            if response.status == "ok" and response.result.get("state") == "answered":
                question = execution.store.get_question(response.result["question_id"])
                assert question is not None
                offered = next(
                    (
                        item.payload
                        for item in execution.store.agent_task_receipts_by_category(
                            execution.operation_id, "question_answer_offered"
                        )
                        if item.payload.get("question_id") == question.question_id
                        and item.payload.get("answer_revision") == question.answer_revision
                        and item.payload.get("receipt_token")
                    ),
                    None,
                )
                if request.receipt_token is not None:
                    if offered is None or not secrets.compare_digest(
                        request.receipt_token, offered["receipt_token"]
                    ):
                        return refuse("question_receipt_mismatch")
                    if not any(
                        item.payload.get("receipt_token") == request.receipt_token
                        for item in execution.store.agent_task_receipts_by_category(
                            execution.operation_id, "question_answer_acknowledged"
                        )
                    ):
                        execution.store.record_agent_task_receipt(
                            execution.operation_id,
                            "question_answer_acknowledged",
                            offered,
                            tier="diagnostic",
                        )
                if offered is None:
                    offered = {
                        "question_id": question.question_id,
                        "answer_revision": question.answer_revision,
                        "request_id": request.request_id,
                        "receipt_token": secrets.token_hex(32),
                    }
                    execution.store.record_agent_task_receipt(
                        execution.operation_id,
                        "question_answer_offered",
                        offered,
                        tier="diagnostic",
                    )
                response.result["receipt_token"] = offered["receipt_token"]
            return response
        if self.compute_commands is not None:
            return self.compute_commands(request, identity)
        return refuse("This turn does not authorize that command.")


def work_command_handler(
    execution: AgentTaskExecution | None,
    compute_commands: WorkComputeCommands | None,
    *,
    consolidation_apply: Callable[[ApplyCommandRequest, CommandTurnIdentity], CommandResponse]
    | None = None,
) -> WorkCommandHandler:
    from rcp.runs.consolidation import is_consolidation

    if is_consolidation(execution):
        return WorkCommandHandler(
            execution, None, frozenset({"validate", "apply", "lesson"}), consolidation_apply
        )
    verbs = {"validate"}
    if compute_commands is not None:
        verbs.update(compute_commands.allowed_verbs)
    if execution is not None:
        task = execution.store.agent_task(execution.operation_id)
        if (
            task is not None
            and task.dispatch_authority is not None
            and task.dispatch_authority.task_contract == "work_auto"
        ):
            verbs.add("lesson")
    if work_ask_authorized(execution):
        verbs.add("ask")
    return WorkCommandHandler(execution, compute_commands, frozenset(verbs))
