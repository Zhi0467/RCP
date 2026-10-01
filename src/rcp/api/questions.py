"""Human question resolutions are input, never authority changes."""

from __future__ import annotations

import logging
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
from rcp.api.episodes import _episode_for_http
from rcp.api.identity import IdentityAccess
from rcp.core.models import AuthorizedHuman
from rcp.projects import ProjectCatalog
from rcp.runs.auto_research_delivery import deliver_pending_auto_research_mail
from rcp.runs.auto_research_questions import record_auto_research_question_answer
from rcp.storage import AppStore
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


class DismissQuestionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class QuestionResponse(BaseModel):
    question_id: str
    owner_kind: Literal["chat", "episode"]
    owner_id: str
    operation_id: str
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
    created_at: str


def _can_answer(question: QuestionRecord) -> bool:
    return question.state == "pending" and not question.withdrawn_readonly


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
        state=state,
        can_answer=_can_answer(question),
    )


@router.get("/api/projects/{project_id}/chats/{chat_id}/questions")
def chat_questions(
    project_id: str, chat_id: str, *, store: StoreDependency, catalog: CatalogDependency
) -> list[QuestionResponse]:
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
    identity_access: IdentityDependency,
) -> QuestionResponse:
    human = identity_access.require_patch_capable_identity(request)
    with project_write_admission(project_id, request):
        previous = _question_for_http(store, project_id, question_id)
        try:
            question = store.answer_question(
                question_id, answer=body.answer, choices=body.choices, resolved_by=human
            )
        except QuestionStateConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # The admission lock serializes the transition check with its commit.
        # Owners recover delivery from durable resolutions if this process dies.
        if _can_answer(previous):
            try:
                episode = (
                    store.episode(question.origin.owner_id)
                    if question.origin.owner_kind == "episode"
                    else None
                )
                if episode is not None and episode.mode == "auto_research":
                    mail = record_auto_research_question_answer(store, question_id)
                    if mail is not None:
                        deliver_pending_auto_research_mail(
                            request.app.state.background_tasks,
                            episode_id=mail.episode_id,
                            recipient_task_id=mail.recipient_task_id,
                        )
                else:
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
    identity_access: IdentityDependency,
) -> QuestionResponse:
    human = identity_access.require_patch_capable_identity(request)
    with project_write_admission(project_id, request):
        _question_for_http(store, project_id, question_id)
        try:
            question = store.dismiss_question(question_id, resolved_by=human)
        except QuestionStateConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _serialize(store, question)
