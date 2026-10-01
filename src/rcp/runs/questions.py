"""Nonblocking question handling, called only after a concrete owner authorizes ask."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rcp.agents.command_mailbox import CommandTurnIdentity
from rcp.agents.command_protocol import (
    AskCommandRequest,
    AskResult,
    CommandRequest,
    CommandResponse,
)
from rcp.storage import AppStore
from rcp.storage.question_models import QuestionArgumentConflict, QuestionOrigin

if TYPE_CHECKING:
    from rcp.background import AgentTaskExecution
    from rcp.runs.tasks.compute_commands import WorkComputeCommands


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
    result = AskResult(
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


def work_question_origin(execution: AgentTaskExecution) -> QuestionOrigin:
    """Resolve after the provider session checkpoint, never from command arguments."""
    if not work_ask_authorized(execution):
        raise ValueError("This turn does not authorize human questions.")
    task = execution.store.agent_task(execution.operation_id)
    assert task is not None and task.dispatch_authority is not None
    authority = task.dispatch_authority
    episode_id = task.episode_id if authority.scope.patch_kind == "experiment_loop" else None
    if not task.native_session_id or not task.stage_root or not task.write_scope_fingerprint:
        raise ValueError("The asking turn has no complete native session and write binding yet.")
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


def record_work_question_receipts(execution: AgentTaskExecution) -> None:
    """Successful full settlement following an answered response is our receipt rule.

    A handler response alone is never receipt. Persist offered revisions so recovery
    can apply the same rule, but only a successful settled task confirms them. Failed,
    paused, or disconnected turns deliberately retain follow-up eligibility. This
    favors retained input when transport consumption cannot be established.
    """
    record_settled_question_receipts(execution.store, execution.operation_id)


def record_settled_question_receipts(store: AppStore, operation_id: str) -> None:
    """Replay the same receipt rule after a crash between settlement and callback."""
    task = store.agent_task(operation_id)
    if task is None or task.status != "succeeded":
        return
    for receipt in store.agent_task_receipts(operation_id):
        if receipt.category != "question_answer_offered":
            continue
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
    """Questions compose beside compute, never in compute's child-facing allowlist."""

    execution: AgentTaskExecution | None
    compute_commands: WorkComputeCommands | None
    allowed_verbs: frozenset[str]

    def __call__(self, request: CommandRequest, identity: CommandTurnIdentity) -> CommandResponse:
        def refuse(message: str) -> CommandResponse:
            return CommandResponse(request_id=request.request_id, status="invalid", message=message)

        if request.verb not in self.allowed_verbs:
            return refuse("This Work owner does not authorize that command.")
        if isinstance(request, AskCommandRequest):
            execution = self.execution
            if (
                execution is None
                or identity.authority != "broker"
                or identity.task_id != execution.operation_id
            ):
                return refuse("Questions require this Work turn's broker authority.")
            try:
                origin = work_question_origin(execution)
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
                offered = execution.store.agent_task_receipts(execution.operation_id)
                if any(
                    item.category == "question_answer_offered"
                    and item.payload.get("question_id") == question.question_id
                    and item.payload.get("answer_revision") == question.answer_revision
                    for item in offered
                ):
                    return response
                execution.store.record_agent_task_receipt(
                    execution.operation_id,
                    "question_answer_offered",
                    {
                        "question_id": question.question_id,
                        "answer_revision": question.answer_revision,
                        "request_id": request.request_id,
                    },
                    tier="diagnostic",
                )
            return response
        if self.compute_commands is not None:
            return self.compute_commands(request, identity)
        return refuse("This Work owner does not authorize that command.")


def work_command_handler(
    execution: AgentTaskExecution | None, compute_commands: WorkComputeCommands | None
) -> WorkCommandHandler:
    verbs = {"validate"}
    if compute_commands is not None:
        verbs.update(compute_commands.allowed_verbs)
    if work_ask_authorized(execution):
        verbs.add("ask")
    return WorkCommandHandler(execution, compute_commands, frozenset(verbs))
