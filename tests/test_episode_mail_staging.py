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


def test_the_child_rule_names_ask_only_for_a_chat_started_episode_that_may_ask():
    from rcp.agents.prompts import COMMAND_CLIENT, episode_mailbox_child_rule

    ask = f"`{COMMAND_CLIENT} ask`"
    assert ask in episode_mailbox_child_rule(("ask", "apply"), parent_chat=True)
    assert ask not in episode_mailbox_child_rule(("apply",), parent_chat=True)
    assert episode_mailbox_child_rule(("apply",), parent_chat=True)
    assert episode_mailbox_child_rule(("ask", "apply"), parent_chat=False) == ""


def test_an_ungranted_verb_refusal_names_the_verbs_this_turn_has():
    import uuid

    from rcp.agents.command_mailbox import CommandTurnIdentity
    from rcp.agents.command_protocol import EpisodeCommandRequest
    from rcp.runs.questions import WorkCommandHandler

    handler = WorkCommandHandler(None, None, frozenset({"ask", "bulletin"}))
    refused = handler(
        EpisodeCommandRequest.model_validate(
            {
                "mailbox_id": "a" * 32,
                "request_id": uuid.uuid4().hex,
                "credential": "b" * 64,
                "verb": "episode",
                "idempotency_key": "k",
                "arguments": {"action": "stop", "episode_id": "e"},
            }
        ),
        CommandTurnIdentity(episode_id=None, task_id="t", turn_id="turn", authority="broker"),
    )
    assert refused.status == "invalid"
    assert refused.result == {"code": "verb_not_granted", "allowed_verbs": ["ask", "bulletin"]}


def test_episode_mail_accepts_messages_from_its_continuation_chain_only():
    def delivery(episode_id):
        return EpisodeMailDelivery(
            recipient_kind="episode",
            recipient_id="continuation",
            chain_episode_ids=["continuation", "first"],
            delivery_operation_id="parent-turn",
            messages=[
                AutoResearchMailMessage.model_validate(
                    _message(
                        recipient_kind="episode",
                        recipient_chat_id=None,
                        episode_id=episode_id,
                    )
                )
            ],
        )

    assert delivery("first").messages[0].episode_id == "first"
    with pytest.raises(ValidationError):
        delivery("unrelated")
