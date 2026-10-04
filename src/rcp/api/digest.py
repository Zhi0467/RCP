"""Member-owned digest reads and explicit acknowledgment."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from rcp.api.dependencies import (
    get_catalog,
    get_identity_access,
    get_store,
    require_project_membership,
    require_registered_project,
)
from rcp.api.identity import IdentityAccess
from rcp.digest import read_digest
from rcp.projects import ProjectCatalog
from rcp.storage import AppStore

router = APIRouter(dependencies=[Depends(require_project_membership)])
CatalogDependency = Annotated[ProjectCatalog, Depends(get_catalog)]
StoreDependency = Annotated[AppStore, Depends(get_store)]
IdentityDependency = Annotated[IdentityAccess, Depends(get_identity_access)]


class DigestMark(BaseModel):
    seq: int
    marked_at: str


class DigestAttention(BaseModel):
    kind: Literal["proposal", "decision", "question", "episode"]
    item_id: str
    title: str
    target: str
    deep_link: str | None
    created_at: str


class DigestChange(BaseModel):
    source_key: str
    source_kind: Literal[
        "consolidation", "episode", "member", "ingestion", "chat", "agent", "system", "unattributed"
    ]
    label: str
    edits: int
    node_ids: list[str]
    report_artifact_id: str | None
    deep_link: str | None


class DigestBranch(BaseModel):
    episode_id: str
    title: str
    edits: int
    deep_link: str | None


class DigestRun(BaseModel):
    kind: Literal[
        "episode_ended",
        "job_ended",
        "task_failed",
        "consolidation_report",
        "consolidation_failed",
        "episode_report",
    ]
    item_id: str
    title: str
    status: str
    deep_link: str | None
    created_at: str


class DigestResponse(BaseModel):
    cursor: int
    mark: DigestMark
    needs_you: list[DigestAttention]
    changed: list[DigestChange]
    branches: list[DigestBranch]
    ran: list[DigestRun]
    changed_node_ids: list[str]
    count: int


class CaughtUpBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: StrictInt = Field(ge=0)


class CaughtUpResponse(BaseModel):
    mark: DigestMark


@router.get("/api/projects/{project_id}/digest", response_model=DigestResponse)
def digest(
    project_id: str,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity: IdentityDependency,
) -> dict[str, object]:
    project_id = catalog.resolve_project_id(project_id)
    require_registered_project(catalog, project_id)
    try:
        return read_digest(store, catalog, project_id, identity.acting_user(request).user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc


# A member's acknowledgment is not project work; global maintenance still applies.
@router.post("/api/projects/{project_id}/digest/caught-up", response_model=CaughtUpResponse)
def caught_up(
    project_id: str,
    body: CaughtUpBody,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity: IdentityDependency,
) -> dict[str, object]:
    project_id = catalog.resolve_project_id(project_id)
    require_registered_project(catalog, project_id)
    try:
        mark = store.catch_up_digest(project_id, identity.acting_user(request).user_id, body.seq)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"mark": mark}
