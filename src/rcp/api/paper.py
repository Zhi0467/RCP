"""Project Paper trees, immutable history, and bounded binary uploads."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import UploadFile
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rcp.api.dependencies import _api_services, get_project_service, require_project_membership
from rcp.api.paper_live import get_paper_live, paper_mutation
from rcp.limits import PAPER_MAX_UPLOAD_BYTES, PAPER_MULTIPART_OVERHEAD_BYTES
from rcp.paper import PaperOp

router = APIRouter()
_member = Depends(require_project_membership)
_PREFIX = "/api/projects/{project_id}/paper"


def is_paper_upload(method: str, path: str) -> bool:
    parts = path.split("/")
    return (
        len(parts) >= 6
        and parts[1:3] == ["api", "projects"]
        and parts[4] == "paper"
        and (
            (method == "POST" and len(parts) == 6 and parts[5] == "uploads")
            or (
                method == "PUT"
                and len(parts) == 8
                and parts[5] == "files"
                and parts[7] == "content"
            )
        )
    )


class PaperUploadBodyLimit:
    """Bound the complete multipart stream before Starlette spools file parts."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_paper_upload(scope["method"], scope["path"]):
            await self.app(scope, receive, send)
            return
        received = 0

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > PAPER_MAX_UPLOAD_BYTES + PAPER_MULTIPART_OVERHEAD_BYTES:
                raise HTTPException(413, "Paper upload exceeds its size limit")
            return message

        await self.app(scope, bounded_receive, send)


class CreateFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    kind: Literal["text"] = "text"


class RenameFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)


class NamedVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)


class RestoreVersion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_ids: list[str]


class ResolveHold(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keep: Literal["draft", "canonical"]


def _actor(request: Request) -> str:
    return _api_services(request).identity_access.acting_user(request).user_id


async def _apply(request: Request, project_id: str, op: PaperOp):
    live = get_paper_live(request)
    return await paper_mutation(
        request, project_id, lambda: live.apply(project_id, [op], _actor(request), {})
    )


@router.get(_PREFIX + "/tree")
async def paper_tree(request: Request, project_id: str = _member):
    return await get_paper_live(request).tree(project_id)


@router.post(_PREFIX + "/files")
async def create_file(body: CreateFile, request: Request, project_id: str = _member):
    return await _apply(request, project_id, PaperOp(kind="create", path=body.path))


@router.post(_PREFIX + "/files/{file_id}/rename")
async def rename_file(file_id: str, body: RenameFile, request: Request, project_id: str = _member):
    return await _apply(
        request, project_id, PaperOp(kind="rename", file_id=file_id, path=body.path)
    )


@router.delete(_PREFIX + "/files/{file_id}")
async def delete_file(file_id: str, request: Request, project_id: str = _member):
    return await _apply(request, project_id, PaperOp(kind="delete", file_id=file_id))


async def _upload(request: Request, *, new: bool) -> tuple[str | None, bytes]:
    async with request.form(
        max_files=1, max_fields=1, max_part_size=PAPER_MAX_UPLOAD_BYTES
    ) as form:
        upload = form.get("file")
        path = form.get("path")
        if not isinstance(upload, UploadFile) or (new and not isinstance(path, str)):
            raise HTTPException(422, "Upload requires a file and new files require a path")
        data = await upload.read(PAPER_MAX_UPLOAD_BYTES + 1)
        if len(data) > PAPER_MAX_UPLOAD_BYTES:
            raise HTTPException(413, "Paper upload exceeds its size limit")
        return path if isinstance(path, str) else None, data


@router.post(_PREFIX + "/uploads")
async def upload_file(request: Request, project_id: str = _member):
    path, data = await _upload(request, new=True)
    return await _apply(
        request, project_id, PaperOp(kind="create", path=path, file_kind="binary", data=data)
    )


@router.put(_PREFIX + "/files/{file_id}/content")
async def replace_binary(file_id: str, request: Request, project_id: str = _member):
    _, data = await _upload(request, new=False)
    return await _apply(request, project_id, PaperOp(kind="binary", file_id=file_id, data=data))


@router.get(_PREFIX + "/files/{file_id}/raw")
async def raw_file(
    file_id: str, request: Request, revision: int | None = None, project_id: str = _member
):
    live = get_paper_live(request)
    try:
        snapshot = await live.snapshot(project_id, revision)
        file = next(file for file in snapshot.files if file.file_id == file_id)
    except (KeyError, StopIteration) as exc:
        raise HTTPException(404, "Paper file or revision not found") from exc
    data = (
        (file.text or "").encode("utf-8")
        if file.kind == "text"
        else live.blob(file.blob_sha256 or "")
    )
    raster = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    media_type = raster.get(PurePosixPath(file.path).suffix.lower())
    disposition = "inline" if media_type else "attachment"
    return Response(
        data,
        media_type=media_type or "application/octet-stream",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(PurePosixPath(file.path).name)}",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )


@router.get(_PREFIX + "/history")
async def history(request: Request, project_id: str = _member):
    return await get_paper_live(request).history(project_id)


@router.post(_PREFIX + "/history")
async def create_version(body: NamedVersion, request: Request, project_id: str = _member):
    live = get_paper_live(request)
    return await paper_mutation(
        request, project_id, lambda: live.create_version(project_id, body.name, _actor(request))
    )


@router.get(_PREFIX + "/history/{version_id}")
async def version(version_id: str, request: Request, project_id: str = _member):
    try:
        return await get_paper_live(request).version(project_id, version_id)
    except KeyError as exc:
        raise HTTPException(404, "Paper version not found") from exc


@router.get(_PREFIX + "/history/{version_id}/files/{file_id}")
async def version_file(version_id: str, file_id: str, request: Request, project_id: str = _member):
    try:
        snapshot = await get_paper_live(request).version_snapshot(project_id, version_id)
        file = next(file for file in snapshot.files if file.file_id == file_id)
    except (KeyError, StopIteration) as exc:
        raise HTTPException(404, "Paper version file not found") from exc
    if file.kind != "text":
        raise HTTPException(422, "This version file is binary")
    return PlainTextResponse(file.text or "", headers={"X-Content-Type-Options": "nosniff"})


@router.post(_PREFIX + "/history/{version_id}/restore")
async def restore_version(
    version_id: str, body: RestoreVersion, request: Request, project_id: str = _member
):
    live = get_paper_live(request)
    return await paper_mutation(
        request,
        project_id,
        lambda: live.restore(project_id, version_id, body.file_ids, _actor(request)),
    )


@router.post(_PREFIX + "/hold/resolve")
async def resolve_hold(body: ResolveHold, request: Request, project_id: str = _member):
    live = get_paper_live(request)
    return await paper_mutation(
        request, project_id, lambda: live.resolve_hold(project_id, body.keep, _actor(request))
    )


@router.get(_PREFIX + "/sessions")
def paper_sessions(request: Request, project_id: str = _member):
    paper = get_project_service(_api_services(request).catalog, project_id).paper
    return [item.model_dump(mode="json") for item in paper.sessions()]
