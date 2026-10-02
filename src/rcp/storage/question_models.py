"""Durable human questions and their original, server-resolved authority binding."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rcp.core.models import AuthorizedHuman
from rcp.core.transition_models import GraphTargetRef
from rcp.limits import (
    ASK_ANSWER_MAX_LENGTH,
    ASK_CHOICE_MAX_COUNT,
    ASK_CHOICE_MAX_LENGTH,
    ASK_QUESTION_MAX_LENGTH,
)
from rcp.providers import ProviderId


class QuestionArgumentConflict(ValueError):
    """An owner reused a question key with different arguments."""


class QuestionStateConflict(ValueError):
    """A resolution conflicts with the question's durable state."""


class QuestionOrigin(BaseModel):
    """Captured once from server records; human input can never replace it.

    ``owner_id`` is the stable chat or episode id. ``operation_id`` identifies
    the asking turn. Episode continuations retain this original provenance.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    owner_kind: Literal["chat", "episode"]
    project_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    provider: ProviderId
    native_session_id: str = Field(min_length=1)
    stage_root: str = Field(min_length=1)
    stage_host: str | None = None
    capability: str = Field(min_length=1)
    write_scope_fingerprint: str = Field(min_length=1)
    graph_target: GraphTargetRef


class QuestionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question_id: str
    origin: QuestionOrigin
    key: str = Field(min_length=1, max_length=200)
    argument_hash: str
    question: str = Field(min_length=1, max_length=ASK_QUESTION_MAX_LENGTH)
    choices: list[Annotated[str, Field(min_length=1, max_length=ASK_CHOICE_MAX_LENGTH)]] = Field(
        default_factory=list, max_length=ASK_CHOICE_MAX_COUNT
    )
    multiple: bool = False
    state: Literal["pending", "answered", "dismissed"] = "pending"
    answer: str | None = Field(default=None, max_length=ASK_ANSWER_MAX_LENGTH)
    chosen_choices: list[str] = Field(default_factory=list)
    resolved_by: AuthorizedHuman | None = None
    resolved_at: str | None = None
    answer_revision: int = 0
    answer_projected_revision: int = 0
    client_receipt_revision: int | None = None
    client_receipt_operation_id: str | None = None
    client_receipt_request_id: str | None = None
    client_receipt_at: str | None = None
    followup_operation_id: str | None = None
    followup_claimed_at: str | None = None
    dismissal_delivered_at: str | None = None
    withdrawn_readonly: bool = False
    created_at: str

    @model_validator(mode="after")
    def validate_question(self) -> QuestionRecord:
        if not self.question.strip() or any(not choice.strip() for choice in self.choices):
            raise ValueError("question and choices must not be blank")
        if len(set(self.choices)) != len(self.choices):
            raise ValueError("question choices must be distinct")
        if self.multiple and not self.choices:
            raise ValueError("multiple requires choices")
        return self
