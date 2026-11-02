from __future__ import annotations

import threading
import uuid
from concurrent.futures import Future
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from rcp.background import BackgroundAgentTasks
from rcp.limits import PROVIDER_STEER_ACK_TIMEOUT_SECONDS
from rcp.providers import ProviderRuntime, ProviderSteeringState, ProviderSteerReceipt, profile_for
from rcp.runs.chat import (
    _append_chat_records,
    _chat_path,
    question_answer_message_id,
    question_answer_text,
)
from rcp.runs.space_chat import SpaceThreadOwner, append_space_records, space_transcript_lock
from rcp.runs.task_policy import load_stored_request
from rcp.service import (
    ChatMessage,
    ProjectService,
    RunRequest,
    SteeringReceipt,
    _fold_chat_receipts,
    _StoredChatRecord,
    canonical_space_chat_path,
)
from rcp.storage import AgentTaskRecord, AppStore
from rcp.storage.question_models import QuestionRecord
from rcp.storage.questions import question_receiver_binds


def chat_steering_visible(store: AppStore, record: AgentTaskRecord) -> bool:
    """Steering belongs to human conversations, never episode or routed work."""
    return (
        record.kind in {"node_chat", "project_chat", "space_chat"}
        and record.request.get("trigger", "human") == "human"
        and record.episode_id is None
        and store.auto_research_child_work_for_operation(record.operation_id) is None
    )


def _recorded_runtime(record: AgentTaskRecord) -> ProviderRuntime | None:
    try:
        return profile_for(str(record.request.get("provider"))).runtime(record.runtime_id)
    except ValueError:
        return None


def chat_steer_action_label(record: AgentTaskRecord) -> str:
    runtime = _recorded_runtime(record)
    return (runtime.steer_action_label or "") if runtime is not None else ""


def chat_steering_state(
    background: BackgroundAgentTasks, record: AgentTaskRecord
) -> ProviderSteeringState:
    if not chat_steering_visible(background.store, record):
        return ProviderSteeringState(False, "Only human conversation turns can be steered.", None)
    if record.history_only or record.status != "running":
        return ProviderSteeringState(False, "This task attempt is not running.", None)
    runtime = _recorded_runtime(record)
    if runtime is None:
        return ProviderSteeringState(False, "The recorded provider runtime is unavailable.", None)
    if not runtime.supports_steering:
        return ProviderSteeringState(
            False, f"{record.runtime_label} does not support live steering.", None
        )
    control = background.live_control(record.operation_id)
    if control is None:
        return ProviderSteeringState(False, "This task attempt has no live provider process.", None)
    return control.steering_state()


def _receipt(attempt: int, turn_id: str, status: str, reason: str | None) -> SteeringReceipt:
    return SteeringReceipt(
        attempt=attempt,
        turn_id=turn_id,
        status=status,
        label={"delivered": "Delivered", "refused": "Refused", "unknown": "Delivery unknown"}[
            status
        ],
        reason=reason,
    )


def _message(record: _StoredChatRecord) -> ChatMessage:
    return ChatMessage(
        message_id=record.uuid,
        operation_id=record.operation_id,
        question_id=record.question_id,
        role=record.role,
        text=record.text,
        timestamp=record.timestamp,
        native_session_id=record.native_session_id,
        provider=record.provider,
        model=record.model,
        reasoning=record.reasoning,
        execution_machine=record.execution_machine,
        mode=record.mode,
        trigger=record.trigger,
        source=record.source,
        steering=record.steering,
    )


def _chat_records(path: Path) -> list[_StoredChatRecord]:
    if not path.exists():
        return []
    return _fold_chat_receipts(
        [
            _StoredChatRecord.model_validate_json(line)
            for line in path.read_text().split("\n")
            if line
        ]
    )


def _steering_path(service: ProjectService | SpaceThreadOwner, request: RunRequest) -> Path:
    if isinstance(service, SpaceThreadOwner):
        return canonical_space_chat_path(
            service.catalog.data_dir, service.thread.user_id, service.thread_id
        )
    return _chat_path(service, request)


def _steering_transaction(
    service: ProjectService | SpaceThreadOwner,
) -> AbstractContextManager[object]:
    if isinstance(service, SpaceThreadOwner):
        return space_transcript_lock(service.store, service.thread_id)
    return service.history.workspace.transaction()


def _append_steering_records(
    service: ProjectService | SpaceThreadOwner,
    path: Path,
    records: list[dict[str, object]],
    *,
    reserve_prompt: bool = False,
) -> None:
    if isinstance(service, SpaceThreadOwner):
        records = [
            {key: value for key, value in record.items() if key not in {"nodeId", "graphTarget"}}
            for record in records
        ]
        append_space_records(service, records, reserve_prompt=reserve_prompt)
    else:
        _append_chat_records(service, path, records, reserve_prompt=reserve_prompt)


def _record_steer(
    service: ProjectService | SpaceThreadOwner,
    request: RunRequest,
    record: AgentTaskRecord,
    *,
    message_id: str,
    text: str,
    receipt: SteeringReceipt,
    question_id: str | None = None,
) -> _StoredChatRecord:
    """Append one steer to the transcript inside the caller's transaction, once.

    Reads the transcript for identity only: whether this steer or the turn's
    prompt is recorded.
    """
    path = _steering_path(service, request)
    records = _chat_records(path)
    existing = next((item for item in records if item.uuid == message_id), None)
    if existing is not None:
        return existing
    timestamp = datetime.now(UTC).isoformat()
    common = {
        "sessionId": request.chat_id,
        "nativeSessionId": record.native_session_id,
        "nodeId": request.node_id,
        "chatScope": request.chat_scope,
        "provider": request.provider,
        "model": request.model or "provider-default",
        "reasoning": request.reasoning,
        "executionMachine": request.run_on,
        "cwd": str(service.catalog.data_dir)
        if isinstance(service, SpaceThreadOwner)
        else str(service.manifest.research_dir.parent),
        "timestamp": timestamp,
        "operationId": record.operation_id,
        "mode": request.mode,
        "trigger": "human",
        **({"source": request.source} if request.source is not None else {}),
        "type": "user",
        "role": "user",
    }
    if isinstance(service, SpaceThreadOwner):
        common.update({"chatScope": "space", "privateOwner": service.thread.user_id})
        common.pop("nodeId", None)
    stored = _StoredChatRecord.model_validate(
        {
            **common,
            "uuid": message_id,
            "text": text,
            "steering": receipt.model_dump(),
            "questionId": question_id,
        }
    )
    # The normal exchange arrives only on completion. Reserve its prompt first
    # so a steer cannot become the conversation title or precede its own turn.
    if not any(
        item.operation_id == record.operation_id and item.role == "user" and item.steering is None
        for item in records
    ):
        _append_steering_records(
            service,
            path,
            [
                {
                    **common,
                    "uuid": str(uuid.uuid4()),
                    "text": request.message,
                    "timestamp": record.created_at,
                    "attachments": [item.model_dump(mode="json") for item in request.attachments],
                }
            ],
            reserve_prompt=True,
        )
    _append_steering_records(service, path, [stored.model_dump(mode="json", by_alias=True)])
    return stored


def begin_chat_steer(
    service: ProjectService | SpaceThreadOwner,
    background: BackgroundAgentTasks,
    record: AgentTaskRecord,
    *,
    message_id: str,
    attempt: int,
    expected_turn_id: str,
    text: str,
) -> tuple[_StoredChatRecord, Future[ProviderSteerReceipt] | None]:
    """Record once, address one live attempt, and never recover by re-sending."""
    if not chat_steering_visible(background.store, record):
        raise ValueError("Only human conversation turns can be steered.")
    request = load_stored_request(RunRequest, record.request, operation_id=record.operation_id)
    path = _steering_path(service, request)
    with _steering_transaction(service):
        records = _chat_records(path)
        existing = next((item for item in records if item.uuid == message_id), None)
        if existing is not None:
            if (
                existing.operation_id != record.operation_id
                or existing.text != text
                or existing.steering is None
                or existing.steering.attempt != attempt
                or existing.steering.turn_id != expected_turn_id
            ):
                raise ValueError(
                    "The steering message UUID was already used for a different request."
                )
            return existing, None
        current = background.store.agent_task(record.operation_id)
        assert current is not None
        state = chat_steering_state(background, current)
        refusal = (
            "The task attempt does not match."
            if current.attempt != attempt
            else state.reason
            if not state.can_steer
            else "The provider turn does not match."
            if state.turn_id != expected_turn_id
            else None
        )
        receipt = _receipt(
            attempt,
            expected_turn_id,
            "refused" if refusal else "unknown",
            refusal
            or "Provider acknowledgment has not been recorded; this message will not be resent.",
        )
        stored = _record_steer(
            service,
            request,
            current,
            message_id=message_id,
            text=text,
            receipt=receipt,
        )
    # Recorded before delivery, so other members see a queued steer too.
    background.bump_steer_revision(record.operation_id)
    if refusal:
        return stored, None

    control = background.live_control(record.operation_id)
    try:
        if control is not None:
            return stored, control.steer(expected_turn_id, message_id, text)
        result = ProviderSteerReceipt("refused", "The provider process completed before delivery.")
    except Exception:
        result = ProviderSteerReceipt(
            "unknown", "Provider acknowledgment was lost; this message will not be resent."
        )
    future: Future[ProviderSteerReceipt] = Future()
    future.set_result(result)
    return stored, future


def finish_chat_steer(
    service: ProjectService | SpaceThreadOwner,
    background: BackgroundAgentTasks,
    record: AgentTaskRecord,
    delivery: tuple[_StoredChatRecord, Future[ProviderSteerReceipt] | None],
) -> ChatMessage:
    """Wait without project/canonical locks; a disconnect never triggers a resend."""
    stored, future = delivery
    if future is None:
        return _message(stored)
    assert stored.steering is not None
    try:
        result = future.result(timeout=PROVIDER_STEER_ACK_TIMEOUT_SECONDS)
    except TimeoutError:
        # Cancel only the receipt waiter, not the provider turn. The durable
        # reservation still prevents a retry from sending the message again.
        future.cancel()
        result = ProviderSteerReceipt(
            "unknown", "Provider acknowledgment timed out; this message will not be resent."
        )
    except Exception:
        result = ProviderSteerReceipt(
            "unknown", "Provider acknowledgment was lost; this message will not be resent."
        )
    receipt = _receipt(
        stored.steering.attempt, stored.steering.turn_id, result.status, result.reason
    )
    stored = stored.model_copy(update={"steering": receipt})
    with _steering_transaction(service):
        path = _steering_path(
            service,
            load_stored_request(RunRequest, record.request, operation_id=record.operation_id),
        )
        _append_steering_records(service, path, [stored.model_dump(mode="json", by_alias=True)])
    # The final receipt replaces the queued one other members may already show.
    background.bump_steer_revision(record.operation_id)
    return _message(stored)


_ANSWER_STEERS_LOCK = threading.Lock()
_ANSWER_STEERS_IN_FLIGHT: set[str] = set()


def steer_question_answer(
    service: ProjectService,
    store: AppStore,
    background: BackgroundAgentTasks,
    question: QuestionRecord,
) -> Literal["steered", "in_flight", "not_steered"]:
    """Deliver a chat answer into the chat's running turn instead of queueing a new one.

    Only a later turn bound exactly like the asker can receive it, so the receipt
    rule that retires the follow-up accepts it. Neither the asking turn nor any
    turn whose ask call is still waiting on the question is steered: that call
    returns the answer, and both would deliver it twice.
    The transcript records the answer only after the provider acknowledges it, as
    that turn's steer; refused or unknown delivery leaves the answer to its
    ordinary projection and follow-up turn, so the chat never shows a refusal for
    an answer that still arrives. While one steer awaits acknowledgment, other
    reconcilers see `in_flight` and must leave the answer alone.
    """
    origin = question.origin
    if origin.owner_kind != "chat" or question.state != "answered":
        return "not_steered"
    # Claimed before looking for a target: a reconciler that runs as the target
    # settles finds no running turn, and must not fall back mid-steer.
    with _ANSWER_STEERS_LOCK:
        if question.question_id in _ANSWER_STEERS_IN_FLIGHT:
            return "in_flight"
        _ANSWER_STEERS_IN_FLIGHT.add(question.question_id)
    try:
        # An ask call still polling this question, or just handing back its
        # answer, delivers it itself, whichever turn made it; a steer as well
        # would deliver it twice.
        if store.question_activity.ask_holds_answer(question.question_id):
            return "not_steered"
        target = store.running_chat_task(origin.project_id, origin.owner_id)
        if (
            target is None
            or target.operation_id == origin.operation_id
            or not question_receiver_binds(origin, target)
        ):
            return "not_steered"
        if (turn_id := _steered_turn(store, question, target)) is not None:
            # Delivered earlier, but its transcript record did not land: repair it.
            _record_answer_steer(service, background, question, target, turn_id)
            return "steered"
        state = chat_steering_state(background, target)
        control = background.live_control(target.operation_id)
        if not state.can_steer or state.turn_id is None or control is None:
            return "not_steered"
        return _steer_answer(service, store, background, question, target, state.turn_id, control)
    finally:
        with _ANSWER_STEERS_LOCK:
            _ANSWER_STEERS_IN_FLIGHT.discard(question.question_id)


def repair_received_answer_steer(
    service: ProjectService,
    store: AppStore,
    background: BackgroundAgentTasks,
    question: QuestionRecord,
) -> bool:
    """Restore a steered answer's record after its receiving turn settled.

    A failed transcript write can outlive the turn; the settled turn's receipt
    still names the steer, so the chat keeps the answer on that turn.
    """
    receiver = (
        store.agent_task(question.client_receipt_operation_id)
        if question.client_receipt_operation_id
        else None
    )
    turn_id = _steered_turn(store, question, receiver) if receiver is not None else None
    if receiver is None or turn_id is None:
        return False
    _record_answer_steer(service, background, question, receiver, turn_id)
    return True


def _steered_turn(store: AppStore, question: QuestionRecord, target: AgentTaskRecord) -> str | None:
    """The provider turn an acknowledged steer of this answer reached, if any."""
    # Only this answer's own steer: an ask call that returned it has another request id.
    return next(
        (
            str(receipt.payload["turn_id"])
            for receipt in store.agent_task_receipts_by_category(
                target.operation_id, "question_answer_acknowledged"
            )
            if receipt.payload.get("question_id") == question.question_id
            and receipt.payload.get("answer_revision") == question.answer_revision
            and receipt.payload.get("request_id") == question_answer_message_id(question)
            and receipt.payload.get("turn_id")
        ),
        None,
    )


def _record_answer_steer(
    service: ProjectService,
    background: BackgroundAgentTasks,
    question: QuestionRecord,
    target: AgentTaskRecord,
    turn_id: str,
) -> None:
    """Project a delivered answer as the target turn's steer, once."""
    request = load_stored_request(RunRequest, target.request, operation_id=target.operation_id)
    with service.history.workspace.transaction():
        _record_steer(
            service,
            request,
            target,
            message_id=question_answer_message_id(question),
            text=question_answer_text(question),
            receipt=_receipt(target.attempt, turn_id, "delivered", None),
            question_id=question.question_id,
        )
    background.bump_steer_revision(target.operation_id)


def _steer_answer(
    service: ProjectService,
    store: AppStore,
    background: BackgroundAgentTasks,
    question: QuestionRecord,
    target: AgentTaskRecord,
    turn_id: str,
    control,
) -> Literal["steered", "not_steered"]:
    from rcp.runs.questions import reconcile_question_receipt

    message_id = question_answer_message_id(question)
    text = question_answer_text(question)
    try:
        delivered = (
            control.steer(turn_id, message_id, text)
            .result(timeout=PROVIDER_STEER_ACK_TIMEOUT_SECONDS)
            .status
            == "delivered"
        )
    except Exception:
        delivered = False
    if not delivered:
        return "not_steered"
    # Acknowledgment first: the provider consumed the answer, so a failed
    # transcript write below must never let the answer fall back to a follow-up.
    store.record_agent_task_receipt(
        target.operation_id,
        "question_answer_acknowledged",
        {
            "question_id": question.question_id,
            "answer_revision": question.answer_revision,
            "request_id": message_id,
            "turn_id": turn_id,
        },
        tier="diagnostic",
    )
    # The turn may have settled while the steer awaited acknowledgment; its
    # settlement then saw no receipt, so replay the receipt rule now, before
    # the fallible transcript write.
    reconcile_question_receipt(store, question.question_id)
    _record_answer_steer(service, background, question, target, turn_id)
    return "steered"
