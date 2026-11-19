from __future__ import annotations

import pytest
from pydantic import ValidationError

from rcp.runs.auto_research_mail import AutoResearchMailMessage, EpisodeMailDelivery


def _message(**updates):
    return {
        "message_id": "mail-1",
        "episode_id": "episode-1",
        "sender_role": "worker",
        "sender_task_id": "child-turn",
        "recipient_kind": "chat",
        "recipient_chat_id": "parent-chat",
        "item_kind": "question",
        "source_ref": "question-1",
        "body": "Choose a dataset",
        "created_at": "2026-11-18T00:00:00Z",
        "delivered_at": "2026-11-18T00:00:01Z",
        "delivery_operation_id": "parent-turn",
        **updates,
    }


def test_chat_mail_preserves_source_and_recipient_without_task():
    message = AutoResearchMailMessage.model_validate(_message())
    delivery = EpisodeMailDelivery(
        recipient_kind="chat",
        recipient_id="parent-chat",
        delivery_operation_id="parent-turn",
        messages=[message],
    )
    assert EpisodeMailDelivery.model_validate_json(delivery.model_dump_json()) == delivery
    assert delivery.messages[0].source_ref == "question-1"
    assert delivery.messages[0].recipient_task_id is None


@pytest.mark.parametrize(
    "updates",
    [
        {"recipient_task_id": "wrong-task"},
        {"recipient_chat_id": None},
        {"recipient_kind": "episode"},
    ],
)
def test_mail_rejects_ambiguous_address(updates):
    with pytest.raises(ValidationError):
        AutoResearchMailMessage.model_validate(_message(**updates))


def test_chat_batch_rejects_other_chat_and_other_turn():
    for updates in ({"recipient_chat_id": "other"}, {"delivery_operation_id": "other"}):
        with pytest.raises(ValidationError):
            EpisodeMailDelivery(
                recipient_kind="chat",
                recipient_id="parent-chat",
                delivery_operation_id="parent-turn",
                messages=[AutoResearchMailMessage.model_validate(_message(**updates))],
            )


def test_old_staged_actor_mail_loads_with_defaults():
    payload = _message(recipient_task_id="actor")
    for key in ("recipient_kind", "recipient_chat_id", "item_kind", "source_ref"):
        del payload[key]
    message = AutoResearchMailMessage.model_validate(payload)
    assert message.recipient_kind == "actor"
    assert message.item_kind == "message"
