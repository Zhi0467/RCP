"""Human question resolutions are input, never authority changes."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from rcp.api.dependencies import (
    get_catalog,
    get_identity_access,
    get_store,
    project_write_admission,
    require_project_membership,
    require_registered_project,
)
from rcp.api.episode_routes import continue_ended_episode
from rcp.api.episodes import _episode_for_http
from rcp.api.identity import IdentityAccess
from rcp.core.models import AuthorizedHuman
from rcp.projects import ProjectCatalog
from rcp.runs.auto_research_delivery import deliver_pending_auto_research_mail
from rcp.runs.auto_research_questions import record_auto_research_question_answer
from rcp.storage import AppStore
from rcp.storage.models import EpisodeRecord
from rcp.storage.question_models import QuestionRecord, QuestionStateConflict

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_project_membership)])
StoreDependency = Annotated[AppStore, Depends(get_store)]
CatalogDependency = Annotated[ProjectCatalog, Depends(get_catalog)]
IdentityDependency = Annotated[IdentityAccess, Depends(get_identity_access)]


class AnswerQuestionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    answer: str = ""
    choices: list[str] = Field(default_factory=list)
    # Required when the answer continues its ended episode; the request id makes a
    # retry return the continuation it already created.
    invocation_ceiling: int | None = Field(default=None, ge=1)
    request_id: str | None = Field(default=None, min_length=1, max_length=120)


class DismissQuestionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class QuestionResponse(BaseModel):
    question_id: str
    owner_kind: Literal["chat", "episode"]
    owner_id: str
    operation_id: str
    capability: str
    question: str
    choices: list[str]
    multiple: bool
    state: Literal["pending", "parked", "answered", "dismissed"]
    answer: str | None
    chosen_choices: list[str]
    resolved_by: AuthorizedHuman | None
    resolved_at: str | None
    withdrawn_readonly: bool
    can_answer: bool
    agent_waiting: bool
    # "turns": answering continues the ended episode and names its turns;
    # "ending": the episode is still settling, so its last card waits.
    answer_continues: Literal["turns", "ending"] | None
    created_at: str


_ENDED_EPISODE_STATUSES = frozenset({"completed", "failed", "needs_action", "stopped"})


def _can_answer(question: QuestionRecord) -> bool:
    return question.state == "pending" and not question.withdrawn_readonly


def _newest_episode(store: AppStore, question: QuestionRecord) -> EpisodeRecord | None:
    if question.origin.owner_kind != "episode":
        return None
    episode = store.episode(question.origin.owner_id)
    while episode is not None and (newer := store.episode_continuation(episode.episode_id)):
        episode = newer
    return episode


def _answer_continues(
    store: AppStore, question: QuestionRecord, episode: EpisodeRecord | None
) -> Literal["turns", "ending"] | None:
    """Whether answering this card adds turns to its ended episode.

    Only Stop withdraws an episode's cards. After any other ending the chain's
    last open card carries the turns, so every answer to one ending turn
    reaches the continuation's first turn together.
    """

    if episode is None or question.withdrawn_readonly:
        return None
    if episode.status in _ENDED_EPISODE_STATUSES:
        state: Literal["turns", "ending"] = "turns"
    elif episode.ending is not None:
        state = "ending"
    else:
        return None
    others_open = any(
        other.question_id != question.question_id and _can_answer(other)
        for other in store.episode_questions(question.origin.project_id, episode.episode_id)
    )
    return None if others_open else state


def _serialize(store: AppStore, question: QuestionRecord) -> QuestionResponse:
    task = store.agent_task(question.origin.operation_id)
    state = question.state
    if state == "pending" and (
        question.origin.capability == "orchestrate" or task is None or task.settled
    ):
        state = "parked"
    return QuestionResponse(
        **question.model_dump(include=set(QuestionResponse.model_fields) - {"state"}),
        owner_kind=question.origin.owner_kind,
        owner_id=question.origin.owner_id,
        operation_id=question.origin.operation_id,
        capability=question.origin.capability,
        state=state,
        can_answer=_can_answer(question),
        agent_waiting=(
            _can_answer(question) and store.question_activity.is_waiting(question.question_id)
        ),
        answer_continues=(
            _answer_continues(store, question, _newest_episode(store, question))
            if _can_answer(question)
            else None
        ),
    )


@router.get("/api/projects/{project_id}/chats/{chat_id}/questions")
def chat_questions(
    project_id: str, chat_id: str, *, store: StoreDependency, catalog: CatalogDependency
) -> list[QuestionResponse]:
    project_id = catalog.resolve_project_id(project_id)
    require_registered_project(catalog, project_id)
    return [
        _serialize(store, question)
        for question in store.list_questions(
            project_id=project_id, owner_kind="chat", owner_id=chat_id
        )
    ]


@router.get("/api/projects/{project_id}/episodes/{episode_id}/questions")
def episode_questions(
    project_id: str, episode_id: str, *, store: StoreDependency, catalog: CatalogDependency
) -> list[QuestionResponse]:
    project_id = catalog.resolve_project_id(project_id)
    _episode_for_http(store, catalog, project_id, episode_id)
    return [_serialize(store, item) for item in store.episode_questions(project_id, episode_id)]


def _question_for_http(store: AppStore, project_id: str, question_id: str) -> QuestionRecord:
    question = store.get_question(question_id)
    if question is None or question.origin.project_id != project_id:
        raise HTTPException(status_code=404, detail="Question not found")
    if question.withdrawn_readonly:
        raise HTTPException(status_code=409, detail="Question is withdrawn and read-only")
    return question


@router.post("/api/projects/{project_id}/questions/{question_id}/answer")
def answer_question(
    project_id: str,
    question_id: str,
    body: AnswerQuestionBody,
    request: Request,
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
) -> QuestionResponse:
    project_id = catalog.resolve_project_id(project_id)
    human = identity_access.require_patch_capable_identity(request)
    with project_write_admission(project_id, request):
        current = _question_for_http(store, project_id, question_id)
        newest = _newest_episode(store, current)
        continues = _answer_continues(store, current, newest)
        if current.resolved_at is not None and not (
            # A resolved card replays its answer. It adds turns only when the
            # retry names them and no continuation has begun since it was
            # answered, which is a continuation that failed to start.
            continues == "turns"
            and body.invocation_ceiling is not None
            and body.request_id is not None
            and newest is not None
            and datetime.fromisoformat(newest.created_at)
            <= datetime.fromisoformat(current.resolved_at)
        ):
            continues = None
        if continues == "ending":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "episode_still_ending",
                    "message": "The episode is still ending. Answer once it settles.",
                },
            )
        if continues == "turns" and (body.invocation_ceiling is None or body.request_id is None):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "question_continuation_required",
                    "message": "This episode has ended. Choose how many turns the answer adds.",
                },
            )
        try:
            question = store.answer_question(
                question_id, answer=body.answer, choices=body.choices, resolved_by=human
            )
        except QuestionStateConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if continues == "turns":
            assert newest is not None
            assert body.invocation_ceiling is not None and body.request_id is not None
            # The continuation's first turn claims every held answer of the
            # chain; any later Add turns does the same if this one fails.
            continue_ended_episode(
                newest,
                invocation_ceiling=body.invocation_ceiling,
                request_id=body.request_id,
                authorized_by=human,
                catalog=catalog,
                store=store,
                background_tasks=request.app.state.background_tasks,
            )
        # Retry delivery even when the answer was already committed: an earlier
        # owner call may have failed. Owner delivery is independently idempotent.
        reconcile = False
        try:
            episode = (
                store.episode(question.origin.owner_id)
                if question.origin.owner_kind == "episode"
                else None
            )
            if episode is not None and episode.mode == "auto_research":
                # A continuation already claimed this answer for its first turn.
                mail = (
                    None
                    if continues == "turns"
                    else record_auto_research_question_answer(store, question_id)
                )
                if mail is not None:
                    deliver_pending_auto_research_mail(
                        request.app.state.background_tasks,
                        episode_id=mail.episode_id,
                        recipient_task_id=mail.recipient_task_id,
                    )
            else:
                reconcile = True
        except Exception:
            logger.exception("Question %s committed; immediate delivery failed", question_id)
    if reconcile:
        # Outside the admission fence: a steered answer waits for the provider's
        # acknowledgment, as the steer route does.
        try:
            request.app.state.reconcile_question_answers(project_id)
        except Exception:
            logger.exception("Question %s committed; immediate delivery failed", question_id)
    return _serialize(store, store.get_question(question_id) or question)


@router.post("/api/projects/{project_id}/questions/{question_id}/dismiss")
def dismiss_question(
    project_id: str,
    question_id: str,
    body: DismissQuestionBody,
    request: Request,
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
) -> QuestionResponse:
    project_id = catalog.resolve_project_id(project_id)
    human = identity_access.require_patch_capable_identity(request)
    with project_write_admission(project_id, request):
        _question_for_http(store, project_id, question_id)
        try:
            question = store.dismiss_question(question_id, resolved_by=human)
        except QuestionStateConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _serialize(store, question)
