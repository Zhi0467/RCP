from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rcp.core.models import AuthorizedHuman
from rcp.limits import CAMPAIGN_MAIL_MAX_BYTES, CAMPAIGN_MAIL_MAX_MESSAGES
from rcp.storage import CampaignMessageRecord, CampaignMessageRole
from rcp.transport.workspace_mailbox import RunStageMailbox

CAMPAIGN_MAIL_PROTOCOL_VERSION = 1
CAMPAIGN_MAIL_HANDOFF_FILE = "messages.json"


def _campaign_mail_wire_size(delivery: BaseModel) -> int:
    return len((delivery.model_dump_json() + "\n").encode("utf-8"))


class CampaignMailMessage(BaseModel):
    """One durable message copied into a claimed delivery without reinterpretation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    message_id: str = Field(min_length=1)
    campaign_id: str = Field(min_length=1)
    sender_role: CampaignMessageRole
    sender_task_id: str | None = Field(default=None, min_length=1)
    authorized_by: AuthorizedHuman | None = None
    recipient_task_id: str = Field(min_length=1)
    control_node_id: str | None = Field(default=None, min_length=1)
    body: str = Field(min_length=1, max_length=16_000)
    created_at: str = Field(min_length=1)
    delivered_at: str = Field(min_length=1)
    delivery_operation_id: str = Field(min_length=1)

    @field_validator("body")
    @classmethod
    def body_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("campaign mail body must not be blank")
        return value

    @model_validator(mode="after")
    def sender_metadata_is_explicit(self) -> CampaignMailMessage:
        if self.sender_role == "human":
            if self.sender_task_id is not None:
                raise ValueError("human campaign mail cannot claim an agent task sender")
        else:
            if self.sender_task_id is None:
                raise ValueError("agent campaign mail must name its durable sender task")
            if self.authorized_by is not None:
                raise ValueError("agent campaign mail cannot claim a human sender snapshot")
        return self


class CampaignMailDelivery(BaseModel):
    """One coalesced, already-claimed inbound mail handoff for a provider turn."""

    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = CAMPAIGN_MAIL_PROTOCOL_VERSION
    kind: Literal["campaign_mail_delivery"] = "campaign_mail_delivery"
    campaign_id: str = Field(min_length=1)
    recipient_task_id: str = Field(min_length=1)
    delivery_operation_id: str = Field(min_length=1)
    graph_authority: Literal["none"] = "none"
    epistemic_status: Literal["hearsay"] = "hearsay"
    messages: list[CampaignMailMessage] = Field(min_length=1)

    @model_validator(mode="after")
    def messages_match_the_claimed_delivery(self) -> CampaignMailDelivery:
        if len(self.messages) > CAMPAIGN_MAIL_MAX_MESSAGES:
            raise ValueError(
                f"campaign mail delivery exceeds {CAMPAIGN_MAIL_MAX_MESSAGES} messages"
            )
        message_ids: set[str] = set()
        for message in self.messages:
            if message.message_id in message_ids:
                raise ValueError("campaign mail delivery contains a duplicate message")
            message_ids.add(message.message_id)
            if message.campaign_id != self.campaign_id:
                raise ValueError("campaign mail delivery crosses campaigns")
            if message.recipient_task_id != self.recipient_task_id:
                raise ValueError("campaign mail delivery crosses recipients")
            if message.delivery_operation_id != self.delivery_operation_id:
                raise ValueError("campaign mail delivery crosses claimed wake operations")
        if _campaign_mail_wire_size(self) > CAMPAIGN_MAIL_MAX_BYTES:
            raise ValueError(f"campaign mail delivery exceeds {CAMPAIGN_MAIL_MAX_BYTES} bytes")
        return self

    @property
    def message_ids(self) -> list[str]:
        return [message.message_id for message in self.messages]


def campaign_mail_claim_prefix(
    *,
    campaign_id: str,
    recipient_task_id: str,
    delivery_operation_id: str,
    delivered_at: str,
    messages: Sequence[CampaignMessageRecord],
) -> list[CampaignMessageRecord]:
    """Select the largest deterministic pending prefix inside the handoff domain."""

    selected: list[CampaignMessageRecord] = []
    projected: list[CampaignMailMessage] = []
    message_ids: set[str] = set()
    for message in messages:
        if len(selected) >= CAMPAIGN_MAIL_MAX_MESSAGES:
            break
        if message.message_id in message_ids:
            raise ValueError("campaign mail claim contains a duplicate message")
        if message.campaign_id != campaign_id:
            raise ValueError("campaign mail claim crosses campaigns")
        if message.recipient_task_id != recipient_task_id:
            raise ValueError("campaign mail claim crosses recipients")
        if message.delivered_at is not None or message.delivery_operation_id is not None:
            raise ValueError("campaign mail claim contains an already delivered message")
        copied = CampaignMailMessage.model_validate(
            {
                **message.model_dump(mode="python"),
                "delivered_at": delivered_at,
                "delivery_operation_id": delivery_operation_id,
            }
        )
        candidate = CampaignMailDelivery.model_construct(
            campaign_id=campaign_id,
            recipient_task_id=recipient_task_id,
            delivery_operation_id=delivery_operation_id,
            messages=[*projected, copied],
        )
        if _campaign_mail_wire_size(candidate) > CAMPAIGN_MAIL_MAX_BYTES:
            break
        selected.append(message)
        projected.append(copied)
        message_ids.add(message.message_id)
    return selected


def campaign_mail_delivery(
    *,
    campaign_id: str,
    recipient_task_id: str,
    delivery_operation_id: str,
    messages: Sequence[CampaignMessageRecord],
) -> CampaignMailDelivery:
    """Build an inbound handoff only from records claimed by this exact wake."""

    copied = [CampaignMailMessage.model_validate(message.model_dump()) for message in messages]
    return CampaignMailDelivery(
        campaign_id=campaign_id,
        recipient_task_id=recipient_task_id,
        delivery_operation_id=delivery_operation_id,
        messages=copied,
    )


def parse_campaign_mail_delivery(value: str | bytes) -> CampaignMailDelivery:
    """Strictly parse a complete versioned ``messages.json`` handoff."""

    encoded = value.encode("utf-8") if isinstance(value, str) else value
    if len(encoded) > CAMPAIGN_MAIL_MAX_BYTES:
        raise ValueError(f"campaign mail delivery exceeds {CAMPAIGN_MAIL_MAX_BYTES} bytes")
    return CampaignMailDelivery.model_validate_json(encoded)


def stage_campaign_mail_delivery(
    mailbox: RunStageMailbox,
    delivery: CampaignMailDelivery,
) -> None:
    """Atomically stage inbound mail after the owning turn clears stale handoffs."""

    validated = CampaignMailDelivery.model_validate(delivery.model_dump(mode="python"))
    mailbox.write_text(CAMPAIGN_MAIL_HANDOFF_FILE, validated.model_dump_json() + "\n")
