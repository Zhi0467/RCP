"""Project Paper trees, immutable history, and bounded binary uploads."""

from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import UploadFile
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rcp.api.dependencies import _api_services, get_project_service, require_project_membership
from rcp.api.paper_live import get_paper_live, paper_mutation
from rcp.artifact_comments import selection_frame_addon
from rcp.artifact_theme import artifact_color_mode, artifact_theme
from rcp.artifact_views import (
    InlineAppearance,
    ViewerPresentation,
    artifact_content,
    inline_viewer_document,
)
from rcp.limits import PAPER_MAX_UPLOAD_BYTES, PAPER_MULTIPART_OVERHEAD_BYTES
from rcp.paper import PaperOp
from rcp.paper.live import text_replacement
from rcp.paper.models import Receipt, file_format, paper_file_kind

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


class FolderPath(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)


class RenameFolder(FolderPath):
    to: str = Field(min_length=1)


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


async def _apply(request: Request, project_id: str, op: PaperOp, *, checkpoint: bool = False):
    live = get_paper_live(request)
    return await paper_mutation(
        request,
        project_id,
        lambda: live.apply(project_id, [op], _actor(request), {}, checkpoint=checkpoint),
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
    return await _apply(
        request, project_id, PaperOp(kind="delete", file_id=file_id), checkpoint=True
    )


async def _folder_ops(request: Request, project_id: str, folder: str, op) -> Receipt:
    """Apply one operation per file under `folder` as a single revision, or refuse.

    The files are read before admission, so the mutation re-checks that the folder still
    holds exactly those files at those paths; a concurrent move or addition refuses it."""
    live = get_paper_live(request)
    tree = await live.snapshot(project_id)
    files = tree.files_under(folder)
    if not files:
        raise HTTPException(404, "Paper folder not found")
    checks = [
        {"folder": folder, "file_ids": [file.file_id for file in files]},
        *({"file_id": file.file_id, "path": file.path} for file in files),
    ]
    return await paper_mutation(
        request,
        project_id,
        lambda: live.apply(
            project_id,
            [op(file) for file in files],
            _actor(request),
            {"checks": checks},
            checkpoint=True,
        ),
    )


@router.post(_PREFIX + "/folders/rename")
async def rename_folder(body: RenameFolder, request: Request, project_id: str = _member):
    return await _folder_ops(
        request,
        project_id,
        body.path,
        lambda file: PaperOp(
            kind="rename", file_id=file.file_id, path=body.to + file.path[len(body.path) :]
        ),
    )


@router.post(_PREFIX + "/folders/delete")
async def delete_folder(body: FolderPath, request: Request, project_id: str = _member):
    return await _folder_ops(
        request, project_id, body.path, lambda file: PaperOp(kind="delete", file_id=file.file_id)
    )


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
    kind = paper_file_kind(path or "", data)
    return await _apply(
        request,
        project_id,
        PaperOp(
            kind="create",
            path=path,
            file_kind=kind,
            text=data.decode("utf-8") if kind == "text" else "",
            data=data if kind == "binary" else None,
        ),
    )


@router.put(_PREFIX + "/files/{file_id}/content")
async def replace_content(file_id: str, request: Request, project_id: str = _member):
    _, data = await _upload(request, new=False)
    live = get_paper_live(request)
    tree = await live.snapshot(project_id)
    current = next((file for file in tree.files if file.file_id == file_id), None)
    if current is None:
        raise HTTPException(404, "Paper file not found")
    if current.kind == "binary":
        return await _apply(
            request, project_id, PaperOp(kind="binary", file_id=file_id, data=data), checkpoint=True
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(409, "Paper text files take UTF-8 uploads") from exc
    old = current.text or ""
    # The whole-text guard refuses an upload that would overwrite a concurrent edit.
    op = text_replacement(file_id, old, text)
    if op is None:
        return Receipt(revision=tree.revision)
    checks = [{"file_id": file_id, "kind": "text", "text": old}]
    return await paper_mutation(
        request,
        project_id,
        lambda: live.apply(project_id, [op], _actor(request), {"checks": checks}, checkpoint=True),
    )


@router.get(_PREFIX + "/files/{file_id}/raw")
async def raw_file(
    file_id: str, request: Request, revision: int | None = None, project_id: str = _member
):
    live = get_paper_live(request)
    try:
        file, data = await live.raw_file(project_id, file_id, revision)
    except KeyError as exc:
        raise HTTPException(404, "Paper file or revision not found") from exc
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


async def _html_file(request: Request, project_id: str, file_id: str, revision: int | None):
    try:
        file, data = await get_paper_live(request).raw_file(project_id, file_id, revision)
    except KeyError as exc:
        raise HTTPException(404, "Paper file or revision not found") from exc
    if file_format(file.path, file.kind) != "html":
        raise HTTPException(404, "Paper file has no HTML preview")
    return file, data


_PREVIEW_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


@router.get(_PREFIX + "/files/{file_id}/preview")
async def html_preview(
    file_id: str, request: Request, revision: int | None = None, project_id: str = _member
):
    """The inline artifact shell around a Paper HTML file; invariant 10e holds one frame in."""
    file, _ = await _html_file(request, project_id, file_id, revision)
    query = urlencode({"revision": revision} if revision is not None else {})
    content_url = (
        f"{_PREFIX.format(project_id=quote(project_id, safe=''))}"
        f"/files/{quote(file_id, safe='')}/preview/content" + (f"?{query}" if query else "")
    )
    document, csp = inline_viewer_document(
        PurePosixPath(file.path).name, "html", content_url=content_url, live=None, selectable=True
    )
    return Response(
        document,
        media_type="text/html",
        headers={**_PREVIEW_HEADERS, "Content-Security-Policy": csp},
    )


@router.get(_PREFIX + "/files/{file_id}/preview/content")
async def html_preview_content(
    file_id: str,
    request: Request,
    revision: int | None = None,
    presentation: ViewerPresentation = "panel",
    theme: str | None = None,
    color_mode: str | None = None,
    project_id: str = _member,
):
    file, data = await _html_file(request, project_id, file_id, revision)
    inline = (
        InlineAppearance(artifact_theme(theme), artifact_color_mode(color_mode))
        if presentation == "inline"
        else None
    )
    try:
        document, media_type, csp = artifact_content(
            file.path, "text/html", data, frame_addon=selection_frame_addon(), inline=inline
        )
    except ValueError as exc:
        raise HTTPException(404, "Paper file has no HTML preview") from exc
    return Response(
        document,
        media_type=media_type,
        headers={**_PREVIEW_HEADERS, "Content-Security-Policy": csp},
    )


@router.get(_PREFIX + "/folders/archive")
async def folder_archive(path: str, request: Request, project_id: str = _member):
    tree = await get_paper_live(request).snapshot(project_id)
    files = tree.files_under(path)
    if not files:
        raise HTTPException(404, "Paper folder not found")
    name = PurePosixPath(path).name

    def compress() -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in files:
                archive.writestr(
                    f"{name}/{file.path[len(path) + 1 :]}",
                    tree.file_bytes(file),
                )
        return buffer.getvalue()

    # Compressing a large tree must not stall the loop that serves live edits.
    return Response(
        await asyncio.to_thread(compress),
        media_type="application/zip",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}.zip",
        },
    )


@router.get(_PREFIX + "/history")
async def history(request: Request, project_id: str = _member):
    names = {user.user_id: user.display_name for user in _api_services(request).store.space_users()}
    return [
        row
        | {"actor_name": "automatic" if row["actor"] == "automatic" else names.get(row["actor"])}
        for row in await get_paper_live(request).history(project_id)
    ]


@router.post(_PREFIX + "/history")
async def create_version(body: NamedVersion, request: Request, project_id: str = _member):
    live = get_paper_live(request)
    return await paper_mutation(
        request, project_id, lambda: live.create_version(project_id, body.name, _actor(request))
    )


@router.delete(_PREFIX + "/history/{version_id}", status_code=204)
async def delete_version(version_id: str, request: Request, project_id: str = _member):
    live = get_paper_live(request)
    await paper_mutation(request, project_id, lambda: live.delete_version(project_id, version_id))


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
