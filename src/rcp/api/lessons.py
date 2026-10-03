from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from rcp.api.dependencies import (
    get_identity_access,
    get_store,
    require_project_membership,
    require_project_write_admission,
)
from rcp.api.identity import IdentityAccess
from rcp.limits import LESSON_TEXT_MAX_CHARS
from rcp.storage import AppStore
from rcp.storage.lessons import LessonError

router = APIRouter(dependencies=[Depends(require_project_membership)])
StoreDependency = Annotated[AppStore, Depends(get_store)]
IdentityDependency = Annotated[IdentityAccess, Depends(get_identity_access)]
ProjectDependency = Annotated[str, Depends(require_project_membership)]
WriteProjectDependency = Annotated[str, Depends(require_project_write_admission)]


class LessonText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=LESSON_TEXT_MAX_CHARS)


def _lesson_error(exc: LessonError) -> HTTPException:
    status = {
        "lesson_not_found": 404,
        "lesson_forbidden": 403,
        "lesson_text_invalid": 422,
    }.get(exc.code, 409)
    return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})


@router.get("/api/projects/{project_id}/lessons")
def list_lessons(*, project: ProjectDependency, store: StoreDependency) -> dict[str, object]:
    return {"lessons": store.list_lessons(project)}


@router.post("/api/projects/{project_id}/lessons")
def add_lesson(
    body: LessonText,
    request: Request,
    *,
    project: WriteProjectDependency,
    store: StoreDependency,
    identity: IdentityDependency,
) -> dict[str, object]:
    human = identity.require_patch_capable_identity(request)
    try:
        lesson = store.add_lesson(
            project, body.text, user_id=human.user_id, display_name=human.display_name
        )
    except LessonError as exc:
        raise _lesson_error(exc) from exc
    return {"lesson": lesson}


@router.patch("/api/projects/{project_id}/lessons/{lesson_id}")
def update_lesson(
    lesson_id: str,
    body: LessonText,
    request: Request,
    *,
    project: WriteProjectDependency,
    store: StoreDependency,
    identity: IdentityDependency,
) -> dict[str, object]:
    human = identity.require_patch_capable_identity(request)
    try:
        lesson = store.update_lesson(
            project,
            lesson_id,
            body.text,
            user_id=human.user_id,
            display_name=human.display_name,
        )
    except LessonError as exc:
        raise _lesson_error(exc) from exc
    return {"lesson": lesson}


@router.delete("/api/projects/{project_id}/lessons/{lesson_id}")
def delete_lesson(
    lesson_id: str,
    request: Request,
    *,
    project: WriteProjectDependency,
    store: StoreDependency,
    identity: IdentityDependency,
) -> dict[str, object]:
    human = identity.require_patch_capable_identity(request)
    try:
        store.delete_lesson(project, lesson_id, user_id=human.user_id)
    except LessonError as exc:
        raise _lesson_error(exc) from exc
    return {}
