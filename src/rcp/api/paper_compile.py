"""Paper preview routes; application composition owns compiler and engine lifetime."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from rcp.api.dependencies import (
    get_identity_access,
    require_project_membership,
    require_project_write_admission,
)
from rcp.paper.compile import CompileResult, PaperCompiler
from rcp.paper.engines import EngineStore

router = APIRouter(dependencies=[Depends(require_project_membership)])
assets_router = APIRouter()


def get_paper_compiler(request: Request) -> PaperCompiler:
    compiler = getattr(request.app.state, "paper_compiler", None)
    if not isinstance(compiler, PaperCompiler):
        raise HTTPException(status_code=503, detail="Paper compiler is unavailable")
    return compiler


def get_paper_engines(request: Request) -> EngineStore:
    engines = getattr(request.app.state, "paper_engines", None)
    if not isinstance(engines, EngineStore):
        raise HTTPException(status_code=503, detail="Paper engines are unavailable")
    return engines


class PaperCompileRequest(BaseModel):
    engine: Literal["latex"]
    file_id: str = Field(min_length=1)
    revision: int | None = Field(default=None, ge=0)


@router.post("/api/projects/{project_id}/paper/compile", response_model=CompileResult)
async def compile_paper(
    body: PaperCompileRequest,
    canonical_project_id: Annotated[str, Depends(require_project_membership)],
    compiler: Annotated[PaperCompiler, Depends(get_paper_compiler)],
) -> CompileResult:
    try:
        return await compiler.compile(canonical_project_id, body.file_id, body.revision)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Paper file or revision not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/projects/{project_id}/paper/compile/{compile_id}/pdf")
def get_compiled_pdf(
    compile_id: str,
    canonical_project_id: Annotated[str, Depends(require_project_membership)],
    compiler: Annotated[PaperCompiler, Depends(get_paper_compiler)],
) -> Response:
    try:
        data = compiler.pdf(canonical_project_id, compile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Compiled PDF not found") from exc
    return Response(
        data,
        media_type="application/pdf",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'inline; filename="paper.pdf"',
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/api/projects/{project_id}/paper/engines")
def get_engines(engines: Annotated[EngineStore, Depends(get_paper_engines)]):
    return {
        "latex": engines.status("latex"),
        "typst": engines.status("typst"),
        "errors": {name: engines.error(name) for name in ("latex", "typst")},
    }


@router.post(
    "/api/projects/{project_id}/paper/engines/{name}/install",
    dependencies=[Depends(require_project_write_admission)],
    status_code=202,
)
async def install_engine(
    name: Literal["latex", "typst"],
    engines: Annotated[EngineStore, Depends(get_paper_engines)],
):
    try:
        engines.start_install(name)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"name": name, "status": engines.status(name), "error": engines.error(name)}


@assets_router.get("/api/paper-assets/typst/{asset}")
def get_typst_asset(
    asset: str,
    request: Request,
    engines: Annotated[EngineStore, Depends(get_paper_engines)],
) -> FileResponse:
    get_identity_access(request).acting_user(request)
    try:
        path = engines.asset(asset)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Typst asset unavailable") from exc
    media_type = {
        ".js": "text/javascript",
        ".wasm": "application/wasm",
        ".ttf": "font/ttf",
        ".otf": "font/otf",
    }.get(path.suffix, "application/octet-stream")
    return FileResponse(
        path,
        media_type=media_type,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )
