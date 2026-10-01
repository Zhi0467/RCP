"""Nonblocking question handling, called only after a concrete owner authorizes ask."""

from __future__ import annotations

from rcp.agents.command_protocol import AskCommandRequest, AskResult, CommandResponse
from rcp.storage import AppStore
from rcp.storage.question_models import QuestionArgumentConflict, QuestionOrigin


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
