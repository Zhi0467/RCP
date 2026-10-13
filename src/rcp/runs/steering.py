from __future__ import annotations

import uuid
from concurrent.futures import Future
from datetime import UTC, datetime

from rcp.background import BackgroundAgentTasks
from rcp.limits import PROVIDER_STEER_ACK_TIMEOUT_SECONDS
from rcp.providers import ProviderRuntime, ProviderSteeringState, ProviderSteerReceipt, profile_for
from rcp.runs.chat import (
    _append_chat_records,
    _chat_path,
    question_answer_message_id,
    question_answer_text,
)
from rcp.runs.task_policy import load_stored_request
from rcp.service import (
    ChatMessage,
    ProjectService,
    RunRequest,
    SteeringReceipt,
    _fold_chat_receipts,
    _StoredChatRecord,
)
from rcp.storage import AgentTaskRecord, AppStore
from rcp.storage.question_models import QuestionRecord
from rcp.storage.questions import question_receiver_binds


def chat_steering_visible(store: AppStore, record: AgentTaskRecord) -> bool:
    """Steering belongs to human conversations, never episode or routed work."""
    return (
        record.kind in {"node_chat", "project_chat"}
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
        active_compute_ids=record.active_compute_ids,
        steering=record.steering,
    )


def begin_chat_steer(
    service: ProjectService,
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
    path = _chat_path(service, request)
    with service.history.workspace.transaction():
        records = (
            _fold_chat_receipts(
                [
                    _StoredChatRecord.model_validate_json(line)
                    for line in path.read_text().split("\n")
                    if line
                ]
            )
            if path.exists()
            else []
        )
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
        timestamp = datetime.now(UTC).isoformat()
        common = {
            "sessionId": request.chat_id,
            "nativeSessionId": current.native_session_id,
            "nodeId": request.node_id,
            "chatScope": request.chat_scope,
            "provider": request.provider,
            "model": request.model or "provider-default",
            "reasoning": request.reasoning,
            "executionMachine": request.run_on,
            "cwd": str(service.manifest.research_dir.parent),
            "timestamp": timestamp,
            "operationId": record.operation_id,
            "mode": request.mode,
            "trigger": "human",
            "activeComputeIds": request.active_compute_ids,
            "type": "user",
            "role": "user",
        }
        stored = _StoredChatRecord.model_validate(
            {**common, "uuid": message_id, "text": text, "steering": receipt.model_dump()}
        )
        # The normal exchange arrives only on completion. Reserve its prompt first
        # so a steer cannot become the conversation title or precede its own turn.
        if not any(
            item.operation_id == record.operation_id
            and item.role == "user"
            and item.steering is None
            for item in records
        ):
            _append_chat_records(
                service,
                path,
                [
                    {
                        **common,
                        "uuid": str(uuid.uuid4()),
                        "text": request.message,
                        "timestamp": record.created_at,
                        "attachments": [
                            item.model_dump(mode="json") for item in request.attachments
                        ],
                    }
                ],
                reserve_prompt=True,
            )
        _append_chat_records(service, path, [stored.model_dump(mode="json", by_alias=True)])
    # Recorded before delivery, so other members see a queued steer too.
    background.bump_steer_revision(record.operation_id, record.project_id)
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
    service: ProjectService,
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
    with service.history.workspace.transaction():
        path = service.chat_path(
            stored.session_id, chat_scope=stored.chat_scope, node_id=stored.node_id
        )
        _append_chat_records(service, path, [stored.model_dump(mode="json", by_alias=True)])
    # The final receipt replaces the queued one other members may already show.
    background.bump_steer_revision(record.operation_id, record.project_id)
    return _message(stored)


def steer_question_answer(
    service: ProjectService,
    store: AppStore,
    background: BackgroundAgentTasks,
    question: QuestionRecord,
) -> bool:
    """Deliver a chat answer into the chat's running turn instead of queueing a new one.

    Only a turn bound exactly like the asker can receive it, so the receipt rule that
    retires the follow-up accepts it. A provider acknowledgment becomes that turn's
    answer acknowledgment; its successful settlement records receipt. Refused or
    unknown delivery leaves the answer eligible for its ordinary follow-up turn.
    Returns whether the answer was recorded in the transcript as a steer.
    """
    origin = question.origin
    if origin.owner_kind != "chat" or question.state != "answered":
        return False
    target = store.running_chat_task(origin.project_id, origin.owner_id)
    if (
        target is None
        or not question_receiver_binds(origin, target)
        # A live ask call returns the answer itself.
        or (
            target.operation_id == origin.operation_id
            and store.question_activity.is_waiting(question.question_id)
        )
    ):
        return False
    state = chat_steering_state(background, target)
    if not state.can_steer or state.turn_id is None:
        return False
    message_id = question_answer_message_id(question)
    message = finish_chat_steer(
        service,
        background,
        target,
        begin_chat_steer(
            service,
            background,
            target,
            message_id=message_id,
            attempt=target.attempt,
            expected_turn_id=state.turn_id,
            text=question_answer_text(question),
        ),
    )
    if message.steering is not None and message.steering.status == "delivered":
        store.record_agent_task_receipt(
            target.operation_id,
            "question_answer_acknowledged",
            {
                "question_id": question.question_id,
                "answer_revision": question.answer_revision,
                "request_id": message_id,
            },
            tier="diagnostic",
        )
    return True
