"""A Paper turn's change list and one file's diff, readable by whoever reads its chat."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request

from rcp.api.dependencies import _api_services, require_project_membership
from rcp.api.paper_live import get_paper_live
from rcp.paper.turns import PaperTurn, PaperTurnFile, turn_file, turn_rows, turn_versions

router = APIRouter()
_member = Depends(require_project_membership)
_PREFIX = "/api/projects/{project_id}/paper/turns/{operation_id}"


def _turn(request: Request, project_id: str, operation_id: str) -> tuple[str, str]:
    services = _api_services(request)
    user = services.identity_access.acting_user(request)
    if (
        services.store.comment_target_route(
            user.user_id, "reply", operation_id, project_id=project_id
        )
        is None
    ):
        raise HTTPException(404, "Paper turn not found")
    try:
        return turn_versions(services.store, operation_id)
    except KeyError as exc:
        raise HTTPException(404, "Paper turn not found") from exc


@router.get(_PREFIX, response_model=PaperTurn)
async def paper_turn(operation_id: str, request: Request, project_id: str = _member):
    before, after = await asyncio.to_thread(_turn, request, project_id, operation_id)
    live = get_paper_live(request)
    files = await asyncio.to_thread(live.turn_files, project_id, operation_id)
    store = _api_services(request).store
    rows = await asyncio.to_thread(turn_rows, store, project_id, before, files)
    return PaperTurn(before_version_id=before, after_version_id=after, files=rows)


@router.get(_PREFIX + "/files/{path:path}", response_model=PaperTurnFile)
async def paper_turn_file(
    operation_id: str, path: str, request: Request, project_id: str = _member
):
    before, _after = await asyncio.to_thread(_turn, request, project_id, operation_id)
    live = get_paper_live(request)
    files = await asyncio.to_thread(live.turn_files, project_id, operation_id)
    row = next((row for row in files if row["path"] == path), None)
    if row is None:
        raise HTTPException(404, "File is not in this Paper turn")
    store = _api_services(request).store
    file = await asyncio.to_thread(turn_file, store, project_id, before, row)
    if file.clash:
        source = await live.source(project_id)
        entry = next((entry for entry in source.entries if entry.path == path), None)
        if entry is not None and entry.kind == "text":
            file.current = (await asyncio.to_thread(source.read, entry)).decode()
    return file
