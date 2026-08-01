from __future__ import annotations

import asyncio
import dataclasses
import fcntl
import hashlib
import json
import logging
import os
import posixpath
import re
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal, TypeVar
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from rcp import __version__
from rcp.agents import (
    AgentEvent,
    AgentLauncher,
    ChatContext,
    PromptFactory,
    RunContext,
    agent_output_schema,
    bounded_session_metadata,
    normalize_agent_patch_bookkeeping,
    normalize_processed_cursors,
    validate_agent_patch_shape,
    validate_session_evidence,
    validate_work_patch,
)
from rcp.agents.context import ConversationPointer, SessionPointer
from rcp.artifacts import (
    ARTIFACT_MEDIA_TYPES,
    AgentArtifactDescriptor,
    descriptor_for,
    html_preview_document,
    list_local_regular_files,
    read_local_regular_file,
    validate_artifact_bytes,
)
from rcp.background import (
    AgentTaskExecution,
    AgentTaskRequest,
    BackgroundAgentTasks,
)
from rcp.config import AgentSurface, AgentSurfaceConfig
from rcp.core.models import CoverageBoundary, GraphState, Patch
from rcp.history import PatchRejected, ReplayHalted, RevisionConflict
from rcp.limits import (
    CHAT_ARTIFACT_MAX_COUNT,
    CHAT_ARTIFACT_MAX_FILE_BYTES,
    CHAT_ARTIFACT_MAX_TOTAL_BYTES,
    CHAT_PAGE_DEFAULT_LIMIT,
    CHAT_PAGE_MAX_LIMIT,
    RUN_STAGE_RETENTION_DAYS,
)
from rcp.paper import PaperService, WritingSession
from rcp.projects import ProjectCatalog
from rcp.providers import (
    PROVIDER_IDS,
    AgentCapability,
    classify_terminal_error,
    profile_for,
)
from rcp.server_runtime import ServerMetadata, data_dir_identity, remove_server_metadata
from rcp.service import (
    ChatSummaryPage,
    ChatTranscript,
    CoachRequest,
    GraphSyncRequest,
    GraphUpdateResult,
    NodeEditConflict,
    ProjectService,
    ProjectSettingsRequest,
    RunRequest,
)
from rcp.setup import ProjectSetupManager, ProjectSetupRequest
from rcp.sources import (
    REMOTE_SOURCE_CACHE_LIMITS,
    SESSION_SLICE_CACHE_LIMITS,
    ConversationIndex,
    RebuildableCache,
)
from rcp.storage import AgentTaskKind, AgentTaskReceiptRecord, AgentTaskRecord, AppStore
from rcp.transport import RemoteRunStage, StateUnavailable
from rcp.web_assets import web_dist_path

logger = logging.getLogger(__name__)


class PaperSaveRequest(BaseModel):
    content: str
    base_hash: str | None = None


class ConflictRequest(BaseModel):
    strategy: Literal["use_canonical", "overwrite_canonical"]


class ProjectRegisterRequest(BaseModel):
    locator: str


class RetryAgentTaskRequest(BaseModel):
    provider: str | None = None
    model: str | None = None
    reasoning: str | None = None
    run_on: str | None = None


class _PreparedGraphContext(BaseModel):
    version: Literal[1] = 1
    project_id: str
    kind: Literal["seed", "refresh"]
    graph_revision: int
    run_truth_scope: list[str]
    execution_host: str
    source_snapshot_digest: str
    original_contract_path: str | None = None
    context: RunContext
    previous_coverage: CoverageBoundary


@dataclass(frozen=True)
class _GraphRetryState:
    lineage: tuple[AgentTaskRecord, ...]
    prepared: _PreparedGraphContext | None
    prepared_parent: AgentTaskRecord | None
    progress_parent: AgentTaskRecord | None
    progress: dict[str, object]
    transcript_sources: tuple[str, ...] = ()
    prior_progress_text: str | None = None
    retained_patch_text: str | None = None
    base_contract_content: str | None = None
    context_reason: str | None = None
    progress_reason: str | None = None


class _LazyProjectService:
    """Compatibility handle that opens the default project only when inspected."""

    def __init__(self, catalog: ProjectCatalog, project_id: str) -> None:
        object.__setattr__(self, "_catalog", catalog)
        object.__setattr__(self, "_project_id", project_id)

    def _resolve(self) -> ProjectService:
        return self._catalog.open(self._project_id)

    def __getattr__(self, name: str):
        return getattr(self._resolve(), name)

    def __setattr__(self, name: str, value) -> None:
        setattr(self._resolve(), name, value)


def create_app(
    manifest_path: str | None = None,
    data_dir: Path | None = None,
    *,
    instance_metadata: ServerMetadata | None = None,
) -> FastAPI:
    # macOS exposes /tmp through /private/tmp. Keep every cache and manifest
    # pointer in the same canonical spelling so relative canonical-state paths
    # do not fail after an otherwise successful remote chat.
    app_data = (data_dir or default_data_dir()).expanduser().resolve()
    identity = instance_metadata or ServerMetadata.create(
        app_data,
        host="127.0.0.1",
        port=8421,
        owner_kind="embedded",
    )
    if identity.data_dir_id != data_dir_identity(app_data):
        raise ValueError("Server metadata does not identify this RCP data directory.")
    store = AppStore(app_data / "rcp.sqlite3")
    launcher = AgentLauncher()
    catalog = ProjectCatalog(app_data, store, launcher)
    setup = ProjectSetupManager(app_data, catalog, launcher)
    default_record = catalog.register(manifest_path) if manifest_path else None
    default_project_id = default_record.project_id if default_record else None
    default_project_name = default_record.name if default_record else None
    default_state_host = (
        catalog.state_host(default_project_id) if default_project_id is not None else ""
    )
    default_service = (
        _LazyProjectService(catalog, default_project_id)
        if default_project_id is not None
        else None
    )

    async def background_task_stream(
        project_id: str,
        kind: AgentTaskKind,
        request: AgentTaskRequest,
        execution: AgentTaskExecution,
    ) -> AsyncIterator[str]:
        service = _project_service(catalog, project_id)
        if kind == "paper_coach":
            assert isinstance(request, CoachRequest)
            async with aclosing(
                _stream_coach(
                    service,
                    launcher,
                    service.paper,
                    request,
                    app_data,
                    execution=execution,
                )
            ) as stream:
                async for frame in stream:
                    yield frame
            return
        assert isinstance(request, RunRequest)
        if kind in {"node_chat", "project_chat"}:
            async with aclosing(
                _stream_chat_run(
                    service,
                    launcher,
                    request,
                    app_data,
                    execution=execution,
                )
            ) as stream:
                async for frame in stream:
                    yield frame
            return
        async with aclosing(
            _stream_graph_run(
                service,
                launcher,
                kind,
                request,
                app_data,
                execution=execution,
            )
        ) as stream:
            async for frame in stream:
                yield frame

    background_tasks = BackgroundAgentTasks(store, background_task_stream)

    async def warm_local_provider_capabilities() -> None:
        try:
            targets = await asyncio.to_thread(catalog.local_provider_targets)

            def probe(provider: str, binary: str | None) -> None:
                if binary is None:
                    launcher.readiness(provider)
                else:
                    launcher.readiness(provider, binary=binary)

            await asyncio.gather(
                *(asyncio.to_thread(probe, provider, binary) for provider, binary in targets)
            )
        except Exception as exc:
            # Warming is an optimization; the next explicit readiness request
            # remains authoritative and must still be able to retry.
            logger.warning("Could not warm local provider capabilities: %s", exc)

    async def sweep_remote_run_stages() -> None:
        try:
            await asyncio.to_thread(RemoteRunStage(default_state_host).sweep)
        except Exception as exc:
            # Stale scratch cleanup is best-effort and must not delay app
            # availability when the project's remote machine is unavailable.
            logger.warning("Could not sweep remote run stages: %s", exc)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        startup_maintenance: list[asyncio.Task[None]] = []
        try:
            store.prune_operational_storage()
            _sweep_stale_stages(app_data / "run-stage", now=time.time())
            RebuildableCache(
                app_data / "source-cache",
                REMOTE_SOURCE_CACHE_LIMITS,
                layout="files",
            ).sweep()
            RebuildableCache(
                app_data / "session-slices",
                SESSION_SLICE_CACHE_LIMITS,
                layout="directories",
            ).sweep()
            # Scheduling happens before the app becomes available, but the task
            # itself cannot run until control returns to the server after yield.
            startup_maintenance.append(
                asyncio.create_task(warm_local_provider_capabilities())
            )
            if default_state_host:
                startup_maintenance.append(asyncio.create_task(sweep_remote_run_stages()))
            yield
        finally:
            for task in startup_maintenance:
                task.cancel()
            for task in startup_maintenance:
                with suppress(asyncio.CancelledError):
                    await task
            background_tasks.shutdown()
            # A PyInstaller one-file backend runs under a bootloader supervisor
            # whose signal exit can skip the CLI context manager's ``finally``.
            # Source reload workers share metadata owned by the outer supervisor,
            # so they must leave it in place across worker restarts.
            if getattr(sys, "frozen", False):
                remove_server_metadata(app_data, instance_id=identity.instance_id)

    app = FastAPI(title="RCP", version=__version__, lifespan=lifespan)
    app.state.catalog = catalog
    app.state.setup = setup
    app.state.default_project_id = default_project_id
    app.state.service = default_service
    app.state.data_dir = app_data
    app.state.background_tasks = background_tasks
    app.state.instance_metadata = identity
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def require_current_instance(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            pinned_instance = request.headers.get("X-RCP-Instance-ID")
            if pinned_instance and pinned_instance != identity.instance_id:
                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": (
                            "RCP was replaced by another backend instance. Refresh before "
                            "making changes."
                        ),
                        "instance_id": identity.instance_id,
                    },
                )
        return await call_next(request)

    @app.exception_handler(PatchRejected)
    async def patch_rejected(_: Request, exc: PatchRejected) -> JSONResponse:
        status = (
            409
            if any(item.code == "stale-node-edit" for item in exc.report.messages)
            else 422
        )
        return JSONResponse(
            status_code=status,
            content={"detail": [item.model_dump(mode="json") for item in exc.report.messages]},
        )

    @app.exception_handler(StateUnavailable)
    async def state_unavailable(_: Request, exc: StateUnavailable) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(ReplayHalted)
    async def replay_halted(_: Request, exc: ReplayHalted) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "detail": str(exc),
                "failed_revision": exc.failed_revision,
                "coherent_revision": exc.coherent_revision,
                "code": exc.code,
            },
        )

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        with store.connection() as connection:
            active_agent_tasks = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM graph_runs
                    WHERE status IN ('queued', 'running', 'pausing')
                    """
                ).fetchone()[0]
            )
        payload: dict[str, object] = {
            "status": "ok",
            "version": __version__,
            "instance_id": identity.instance_id,
            "pid": identity.pid,
            "data_dir_id": identity.data_dir_id,
            "owner_kind": identity.owner_kind,
            "active_agent_tasks": active_agent_tasks,
            "projects": len(catalog.cards()),
        }
        if default_project_name is not None:
            payload["project"] = default_project_name
        return payload

    @app.get("/api/projects")
    def projects() -> list[dict[str, object]]:
        return catalog.cards()

    @app.get("/api/providers")
    def providers(refresh: bool = False) -> list[dict[str, object]]:
        """The registry probed on this machine, for surfaces with no project yet.

        Project setup picks agent defaults before any manifest exists, so it has
        no per-machine readiness to read. Remote hosts are reported by preflight.
        """
        return [
            launcher.readiness(provider, refresh=refresh).model_dump(mode="json")
            for provider in PROVIDER_IDS
        ]

    @app.post("/api/projects")
    def register_project(body: ProjectRegisterRequest) -> dict[str, object]:
        try:
            record = catalog.register(body.locator)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return catalog.card(record.project_id)

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str) -> dict[str, object]:
        try:
            return catalog.delete(project_id).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        except ValueError as exc:
            status = 409 if "active agent task" in str(exc) else 422
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        except (OSError, RuntimeError, StateUnavailable) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/project-setup/preflight")
    def preflight_project(body: ProjectSetupRequest) -> dict[str, object]:
        try:
            return setup.preflight(body).model_dump(mode="json")
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/project-setup/create")
    def create_project(body: ProjectSetupRequest) -> dict[str, object]:
        try:
            return setup.create(body)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/projects/{project_id}")
    def project(project_id: str) -> dict[str, object]:
        try:
            _, snapshot = catalog.open_snapshot(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        snapshot["id"] = project_id
        try:
            catalog.update_summary(project_id, snapshot)
            catalog.write_cached_snapshot(project_id, snapshot)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Could not update display snapshot for %s: %s", project_id, exc)
        return snapshot

    @app.get("/api/projects/{project_id}/cached")
    def cached_project(project_id: str) -> dict[str, object]:
        snapshot = catalog.cached_snapshot(project_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Cached project snapshot not found")
        return snapshot

    @app.get("/api/projects/{project_id}/readiness")
    def project_readiness(project_id: str, refresh: bool = False) -> dict[str, object]:
        try:
            return catalog.readiness_snapshot(project_id, refresh=refresh)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/projects/{project_id}/graph")
    def graph(project_id: str) -> dict[str, object]:
        return _project_service(catalog, project_id).graph_snapshot()

    @app.put("/api/projects/{project_id}/settings")
    def update_project_settings(
        project_id: str,
        body: ProjectSettingsRequest,
    ) -> dict[str, object]:
        try:
            snapshot = catalog.update_settings(project_id, body)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        snapshot["id"] = project_id
        return snapshot

    @app.post(
        "/api/projects/{project_id}/machines/{machine_alias}/providers/{provider}/resolve"
    )
    def resolve_project_provider_path(
        project_id: str,
        machine_alias: str,
        provider: str,
    ) -> dict[str, object]:
        try:
            profile_for(provider)
            result = catalog.resolve_provider_path(project_id, machine_alias, provider)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        project = result.get("project")
        if isinstance(project, dict):
            project["id"] = project_id
        return result

    @app.get("/api/projects/{project_id}/history")
    def history(project_id: str, from_revision: int = 1, to_revision: int | None = None):
        service = _project_service(catalog, project_id)
        return service.history.slice(from_revision, to_revision)

    @app.get("/api/projects/{project_id}/sources")
    def sources(project_id: str, refresh: bool = False):
        service = _project_service(catalog, project_id)
        return service.index_snapshot(refresh=refresh).model_dump(mode="json")

    @app.post("/api/projects/{project_id}/sync")
    def sync_graph(project_id: str, body: GraphSyncRequest):
        service = _project_service(catalog, project_id)
        try:
            return service.sync_graph(body).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Missing graph object: {exc.args[0]}") from exc
        except NodeEditConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/api/projects/{project_id}/caches")
    def clear_rebuildable_caches(project_id: str):
        service = _project_service(catalog, project_id)
        if store.has_any_active_agent_task():
            raise HTTPException(
                status_code=409,
                detail="Rebuildable caches cannot be cleared while an agent task is active.",
            )
        return service.clear_rebuildable_caches()

    @app.post("/api/projects/{project_id}/tasks/{kind}", status_code=202)
    def start_agent_task(
        project_id: str,
        kind: AgentTaskKind,
        body: dict[str, object],
    ) -> dict[str, object]:
        service = _project_service(catalog, project_id)
        try:
            request = _validated_task_request(service, kind, body)
            if kind in {"node_chat", "project_chat"}:
                assert isinstance(request, RunRequest)
                assert request.chat_id is not None
                if store.has_resumable_paused_chat_task(
                    project_id,
                    kind,
                    request.chat_id,
                ):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "This conversation has a paused turn. Resume or retry it before "
                            "starting a new turn."
                        ),
                    )
            record = background_tasks.start(project_id, kind, request)
        except ValueError as exc:
            status = 409 if "already running" in str(exc) else 422
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return record.model_dump(mode="json")

    @app.get("/api/projects/{project_id}/tasks")
    def agent_tasks(project_id: str) -> list[dict[str, object]]:
        _require_registered_project(catalog, project_id)
        return [record.model_dump(mode="json") for record in store.agent_tasks(project_id)]

    @app.get(
        "/api/projects/{project_id}/chats",
        response_model=ChatSummaryPage,
    )
    def chats(
        project_id: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(
            default=CHAT_PAGE_DEFAULT_LIMIT,
            ge=1,
            le=CHAT_PAGE_MAX_LIMIT,
        ),
    ) -> ChatSummaryPage:
        service = _project_service(catalog, project_id)
        return service.chat_summaries(offset=offset, limit=limit)

    @app.get(
        "/api/projects/{project_id}/chats/{chat_id}",
        response_model=ChatTranscript,
    )
    def chat(project_id: str, chat_id: str) -> ChatTranscript:
        service = _project_service(catalog, project_id)
        try:
            transcript = service.chat_transcript(chat_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if transcript is None:
            raise HTTPException(status_code=404, detail="Chat not found")
        return transcript

    @app.get("/api/projects/{project_id}/tasks/{operation_id}")
    def agent_task(project_id: str, operation_id: str) -> dict[str, object]:
        _require_registered_project(catalog, project_id)
        record = store.agent_task(operation_id)
        if record is None or record.project_id != project_id:
            raise HTTPException(status_code=404, detail="Agent task not found")
        detail = record.model_dump(mode="json")
        detail["events"] = [
            event.model_dump(mode="json") for event in store.agent_task_events(operation_id)
        ]
        detail["debug_receipts"] = [
            receipt.model_dump(mode="json")
            for receipt in store.agent_task_receipts(operation_id)
        ]
        return detail

    @app.get(
        "/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/preview"
    )
    @app.head(
        "/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/preview"
    )
    async def preview_agent_artifact(
        project_id: str,
        operation_id: str,
        artifact_id: str,
        request: Request,
    ) -> Response:
        descriptor, data = await asyncio.to_thread(
            _load_agent_artifact,
            store,
            project_id,
            operation_id,
            artifact_id,
        )
        headers = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if descriptor.media_type == "text/html":
            try:
                document, csp = html_preview_document(data)
            except Exception as exc:
                # Rendering is an optional preview boundary. A malformed document
                # or renderer defect makes only this attachment unavailable.
                raise HTTPException(status_code=410, detail="Preview unavailable") from exc
            headers["Content-Security-Policy"] = csp
            return Response(
                b"" if request.method == "HEAD" else document,
                media_type="text/html",
                headers=headers,
            )
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return Response(
            b"" if request.method == "HEAD" else data,
            media_type=descriptor.media_type,
            headers=headers,
        )

    @app.get(
        "/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/download"
    )
    @app.head(
        "/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/download"
    )
    async def download_agent_artifact(
        project_id: str,
        operation_id: str,
        artifact_id: str,
        request: Request,
    ) -> Response:
        descriptor, data = await asyncio.to_thread(
            _load_agent_artifact,
            store,
            project_id,
            operation_id,
            artifact_id,
        )
        suffix = Path(descriptor.name).suffix.casefold()
        fallback = f"artifact{suffix}" if suffix in ARTIFACT_MEDIA_TYPES else "artifact"
        disposition = (
            f'attachment; filename="{fallback}"; '
            f"filename*=UTF-8''{quote(descriptor.name, safe='')}"
        )
        return Response(
            b"" if request.method == "HEAD" else data,
            media_type=descriptor.media_type,
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": disposition,
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/projects/{project_id}/tasks/{operation_id}/pause", status_code=202)
    def pause_agent_task(project_id: str, operation_id: str) -> dict[str, object]:
        _project_service(catalog, project_id)
        record = store.agent_task(operation_id)
        if record is None or record.project_id != project_id:
            raise HTTPException(status_code=404, detail="Agent task not found")
        try:
            return background_tasks.pause(operation_id).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/projects/{project_id}/tasks/{operation_id}/resume", status_code=202)
    def resume_agent_task(project_id: str, operation_id: str) -> dict[str, object]:
        service = _project_service(catalog, project_id)
        previous = store.agent_task(operation_id)
        if previous is None or previous.project_id != project_id:
            raise HTTPException(status_code=404, detail="Agent task not found")
        try:
            _validate_stored_task_request(service, previous.kind, previous.request)
            return background_tasks.resume(operation_id).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/projects/{project_id}/tasks/{operation_id}/repair-graph-update",
        status_code=202,
    )
    def repair_agent_task_graph_update(
        project_id: str, operation_id: str
    ) -> dict[str, object]:
        service = _project_service(catalog, project_id)
        previous = store.agent_task(operation_id)
        if previous is None or previous.project_id != project_id:
            raise HTTPException(status_code=404, detail="Agent task not found")
        try:
            receipts = store.agent_task_receipts(operation_id)
            base_revision = _assembled_graph_revision(receipts, operation_id)
            current_revision = service.history.state().revision
            if current_revision != base_revision:
                raise ValueError(
                    f"The graph moved from revision {base_revision} to {current_revision}. "
                    "Start a new Work turn to reconcile it."
                )
            return background_tasks.repair_graph_update(operation_id).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Agent task not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/projects/{project_id}/tasks/{operation_id}/retry", status_code=202)
    def retry_agent_task(
        project_id: str,
        operation_id: str,
        body: RetryAgentTaskRequest | None = None,
    ) -> dict[str, object]:
        service = _project_service(catalog, project_id)
        previous = store.agent_task(operation_id)
        if previous is None or previous.project_id != project_id:
            raise HTTPException(status_code=404, detail="Agent task not found")
        try:
            overrides = body.model_dump(exclude_none=True) if body is not None else {}
            request_type = CoachRequest if previous.kind == "paper_coach" else RunRequest
            candidate = request_type.model_validate(
                {**previous.request, **overrides, "session_id": None}
            )
            _validate_stored_task_request(
                service, previous.kind, candidate.model_dump(mode="json")
            )
            return background_tasks.retry(operation_id, **overrides).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/projects/{project_id}/paper")
    def get_paper(project_id: str):
        paper = _project_service(catalog, project_id).paper
        return paper.snapshot().model_dump(mode="json")

    @app.post("/api/projects/{project_id}/paper/create")
    def create_paper(project_id: str):
        paper = _project_service(catalog, project_id).paper
        return paper.create().model_dump(mode="json")

    @app.put("/api/projects/{project_id}/paper")
    def save_paper(project_id: str, body: PaperSaveRequest):
        paper = _project_service(catalog, project_id).paper
        return paper.save(body.content, body.base_hash).model_dump(mode="json")

    @app.post("/api/projects/{project_id}/paper/conflict")
    def resolve_paper_conflict(project_id: str, body: ConflictRequest):
        paper = _project_service(catalog, project_id).paper
        return paper.resolve_conflict(body.strategy).model_dump(mode="json")

    @app.get("/api/projects/{project_id}/paper/sessions")
    def paper_sessions(project_id: str):
        paper = _project_service(catalog, project_id).paper
        return [item.model_dump(mode="json") for item in paper.sessions()]

    web_dist = web_dist_path()
    if web_dist.exists():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")

    return app


def _load_agent_artifact(
    store: AppStore,
    project_id: str,
    operation_id: str,
    artifact_id: str,
) -> tuple[AgentArtifactDescriptor, bytes]:
    """Resolve an attachment only through its persisted task descriptor and stage."""
    record = store.agent_task(operation_id)
    if (
        record is None
        or record.project_id != project_id
        or record.kind not in {"node_chat", "project_chat"}
    ):
        raise HTTPException(status_code=404, detail="Agent task not found")
    artifacts = record.result.get("artifacts") if record.result else None
    descriptor: AgentArtifactDescriptor | None = None
    if isinstance(artifacts, list):
        for raw in artifacts:
            try:
                candidate = AgentArtifactDescriptor.model_validate(raw)
            except (TypeError, ValueError):
                continue
            if candidate.artifact_id == artifact_id:
                descriptor = candidate
                break
    if descriptor is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if not record.stage_root:
        raise HTTPException(status_code=410, detail="Preview unavailable")
    try:
        scope_id = _logical_chat_turn_operation_id(store, record.operation_id)
        expected_descriptor = descriptor_for(scope_id, descriptor.name)
        if expected_descriptor != descriptor:
            raise ValueError("artifact descriptor does not match its task scope")
        if record.stage_host:
            stage = RemoteRunStage(record.stage_host).attach_artifact_source(
                record.stage_root
            )
            data = stage.read_artifact_bytes(
                scope_id,
                descriptor.name,
                max_bytes=CHAT_ARTIFACT_MAX_FILE_BYTES,
            )
        else:
            directory = (
                Path(record.stage_root) / "turns" / scope_id / "artifacts"
            )
            data = read_local_regular_file(
                directory,
                descriptor.name,
                max_bytes=CHAT_ARTIFACT_MAX_FILE_BYTES,
            )
        media_type = validate_artifact_bytes(descriptor.name, data)
        if media_type != descriptor.media_type:
            raise ValueError("artifact media type changed")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=410, detail="Preview unavailable") from exc
    except StateUnavailable as exc:
        raise HTTPException(status_code=503, detail="Preview unavailable") from exc
    except ValueError as exc:
        raise HTTPException(status_code=410, detail="Preview unavailable") from exc
    return descriptor, data


def _project_service(catalog: ProjectCatalog, project_id: str) -> ProjectService:
    try:
        return catalog.open(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _require_registered_project(catalog: ProjectCatalog, project_id: str) -> None:
    try:
        catalog.card(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc


def _validated_task_request(
    service: ProjectService,
    kind: AgentTaskKind,
    body: dict[str, object],
) -> AgentTaskRequest:
    if kind == "paper_coach":
        return _resolved_coach_request(service, CoachRequest.model_validate(body))

    request = RunRequest.model_validate(body)
    if kind in {"seed", "refresh"}:
        service.history.require_writable()
        if request.session_id:
            raise ValueError(
                "Seed and refresh sessions can only be resumed from an RCP background "
                "task checkpoint."
            )
        return _resolved_graph_request(service, kind, request)

    chat_scope: Literal["node", "project"] = (
        "node" if kind == "node_chat" else "project"
    )
    request = request.model_copy(
        update={
            "chat_scope": chat_scope,
            "node_id": request.node_id if chat_scope == "node" else None,
        }
    )
    if not request.message or not request.chat_id:
        raise ValueError("Chat requires a chat_id and message")
    if chat_scope == "node":
        if not request.node_id:
            raise ValueError("Node chat requires a node_id")
        if request.node_id not in service.history.state().nodes:
            raise HTTPException(status_code=404, detail="Node not found")
    try:
        uuid.UUID(request.chat_id)
    except ValueError as exc:
        raise ValueError("chat_id must be a UUID") from exc
    request = _resolved_graph_request(service, kind, request)
    if request.session_id and not _known_chat_session(service, request):
        raise ValueError(
            "That native session was not created by this chat. Start a new chat instead."
        )
    return request


def _validate_stored_task_request(
    service: ProjectService,
    kind: AgentTaskKind,
    body: dict[str, object],
) -> None:
    if kind == "paper_coach":
        request = CoachRequest.model_validate(body)
        service.resolve_agent_profile(
            "paper_coach",
            provider=request.provider,
            model=request.model,
            reasoning=request.reasoning,
            run_on=request.run_on,
        )
        return
    request = RunRequest.model_validate(body)
    if kind in {"seed", "refresh"}:
        service.history.require_writable()
    _resolved_graph_request(service, kind, request)


def _resolved_coach_request(
    service: ProjectService,
    request: CoachRequest,
) -> CoachRequest:
    existing = None
    if request.session_id:
        existing = next(
            (
                session
                for session in service.paper.sessions()
                if session.native_session_id == request.session_id
            ),
            None,
        )
        if existing is None:
            raise ValueError("That native session was not created by this Paper workspace.")
        if request.provider is not None and request.provider != existing.provider:
            raise ValueError("A resumed session cannot change provider.")
        if request.model is not None and (
            request.model or "provider-default"
        ) != existing.model:
            raise ValueError("A resumed session cannot change model.")
        if request.reasoning is not None and request.reasoning != existing.reasoning:
            raise ValueError("A resumed session cannot change reasoning.")
        if request.run_on is not None and request.run_on != existing.execution_machine:
            raise ValueError("A resumed session cannot change execution machine.")
    profile = service.resolve_agent_profile(
        "paper_coach",
        provider=request.provider or (existing.provider if existing else None),
        model=(
            request.model
            if request.model is not None
            else ("" if existing.model == "provider-default" else existing.model)
            if existing
            else None
        ),
        reasoning=request.reasoning or (existing.reasoning if existing else None),
        run_on=request.run_on or (existing.execution_machine if existing else None),
    )
    return request.model_copy(
        update={
            "provider": profile.provider,
            "model": profile.model or None,
            "reasoning": profile.reasoning,
            "run_on": profile.run_on,
        }
    )


def _resolved_graph_request(
    service: ProjectService,
    kind: AgentTaskKind,
    request: RunRequest,
) -> RunRequest:
    surface: AgentSurface = kind
    profile = service.resolve_agent_profile(
        surface,
        provider=request.provider,
        model=request.model,
        reasoning=request.reasoning,
        run_on=request.run_on,
    )
    return request.model_copy(
        update={
            "provider": profile.provider,
            "model": profile.model or None,
            "reasoning": profile.reasoning,
            "run_on": profile.run_on,
        }
    )


_MAX_CORRECTION_ROUNDS = 2
_STAGE_RETENTION_SECONDS = RUN_STAGE_RETENTION_DAYS * 24 * 3600
_MAX_PATCH_CANDIDATES = 8


class AgentOutputProblem(ValueError):
    """The agent finished but its patch file is missing or does not validate.

    These are the failures the recovery ladder can act on by talking to the same
    live session again. Agent-authored graph rejection follows the same ladder.
    """


def _looks_like_patch(text: str) -> bool:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(value, dict) and "ops" in value and "kind" in value


def _existing_patch_digest(
    workspace: Path,
    remote_stage: RemoteRunStage | None,
) -> str | None:
    """Fingerprint a patch already sitting in the stage before a launch.

    A continuation runs in the stage its earlier attempt was given, and
    invariant 9 keeps that attempt's `patch.json` on disk. Without this
    fingerprint a provider that writes nothing at all has its predecessor's
    file collected as this launch's deliverable.
    """
    try:
        text, _ = _collect_patch_text(workspace, remote_stage)
    except (AgentOutputProblem, OSError, StateUnavailable):
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _collect_patch_text(
    workspace: Path,
    remote_stage: RemoteRunStage | None,
) -> tuple[str, str]:
    """Recover the patch the agent wrote, whatever it chose to call the file.

    Rung 1 of the recovery ladder: a filename mismatch used to discard a whole
    run's work, so the entire scratch folder is searched rather than one path.
    """
    if remote_stage is not None:
        names = remote_stage.list_workspace_files()
        reader = remote_stage.read_text

        def read(name: str) -> str:
            return reader(remote_stage.workspace / name)
    else:
        names = sorted(item.name for item in workspace.iterdir() if item.is_file())

        def read(name: str) -> str:
            return (workspace / name).read_text(encoding="utf-8")

    ordered = sorted(
        (name for name in names if name.casefold().endswith(".json")),
        key=lambda name: (
            name != "patch.json",
            name.casefold() != "patch.json",
            name,
        ),
    )
    if not ordered:
        raise AgentOutputProblem(
            "The agent finished without writing any JSON file to its scratch folder."
        )
    matches: list[tuple[str, str]] = []
    for name in ordered[:_MAX_PATCH_CANDIDATES]:
        try:
            text = read(name)
        except (OSError, ValueError):
            continue
        if name == "patch.json" and _looks_like_patch(text):
            return text, name
        if _looks_like_patch(text):
            matches.append((text, name))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AgentOutputProblem(
            "The agent left more than one patch-shaped JSON file in its scratch folder: "
            + ", ".join(name for _, name in matches)
            + ". Write exactly one, named patch.json."
        )
    raise AgentOutputProblem(
        "The agent finished without writing a patch object to patch.json. "
        f"The scratch folder holds: {', '.join(ordered) or 'nothing'}."
    )


def _sweep_stale_stages(root: Path, *, now: float) -> None:
    """Age out retained scratch folders. Failed runs keep theirs until then."""
    if not root.is_dir():
        return
    for candidate in root.iterdir():
        if not candidate.is_dir():
            continue
        try:
            age = now - candidate.stat().st_mtime
        except OSError:
            continue
        if age > _STAGE_RETENTION_SECONDS:
            try:
                _remove_local_tree(candidate, root)
            except (OSError, ValueError):
                # Retention is best effort; a live run must not fail because an
                # unrelated expired stage could not be reclaimed.
                continue


def _remove_local_tree(target: Path, boundary: Path) -> None:
    """Remove one exact tree beneath ``boundary``, including read-only copies."""
    if target.parent != boundary:
        raise ValueError("local cleanup target is outside its exact stage boundary")
    if not os.path.lexists(target):
        return
    if target.is_symlink() or not target.is_dir():
        target.unlink()
    else:
        _make_local_tree_writable(target)
        shutil.rmtree(target)
    if os.path.lexists(target):
        raise OSError(f"local cleanup left {target} behind")


def _make_local_tree_writable(target: Path) -> None:
    if target.is_symlink():
        return
    target.chmod(0o700 if target.is_dir() else 0o600)
    if not target.is_dir():
        return
    for child in target.iterdir():
        _make_local_tree_writable(child)


def _swept_stage_root(data_dir: Path) -> Path:
    """The local scratch root, with expired folders reclaimed before it is used."""
    stage_root = data_dir / "run-stage"
    _sweep_stale_stages(stage_root, now=time.time())
    return stage_root


def _stage_task_input(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    label: str,
    content: str,
) -> str:
    """Create one immutable task input and return its execution-host path."""
    if (local_stage is None) == (remote_stage is None):
        raise ValueError("exactly one task stage must be selected")
    safe_label = _safe_stage_name(label)
    if safe_label != label:
        raise ValueError("task input label contains unsupported characters")
    if remote_stage is not None:
        with tempfile.TemporaryDirectory(prefix="rcp-task-input-") as temporary:
            source = Path(temporary) / safe_label
            source.write_text(content, encoding="utf-8")
            source.chmod(0o400)
            return remote_stage.put_file(source, safe_label)

    assert local_stage is not None
    inputs = local_stage / "inputs"
    inputs.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = inputs / safe_label
    if target.exists():
        raise ValueError(f"immutable task input already exists: {safe_label}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{safe_label}.", dir=inputs)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o400)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return str(target)


def _task_token(execution: AgentTaskExecution | None) -> str:
    return _safe_stage_name(execution.operation_id if execution is not None else uuid.uuid4().hex)


def _parent_task_contract_path(
    execution: AgentTaskExecution,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
) -> str:
    record = execution.store.agent_task(execution.operation_id)
    if record is None or record.parent_operation_id is None:
        raise ValueError("The resumed operation has no original task contract.")
    receipts = execution.store.agent_task_receipts(record.parent_operation_id)
    candidates = [
        receipt.payload.get("contract_path")
        for receipt in receipts
        if receipt.category == "agent_prompt"
    ]
    contract_path = next(
        (value for value in reversed(candidates) if isinstance(value, str) and value), None
    )
    if contract_path is None:
        raise ValueError("The resumed operation has no recorded original task contract.")
    if remote_stage is not None:
        assert remote_stage.root is not None
        if PurePosixPath(contract_path).parent != remote_stage.root / "inputs":
            raise ValueError("The resumed operation's task contract is outside its saved stage.")
    else:
        assert local_stage is not None
        if Path(contract_path).resolve().parent != (local_stage / "inputs").resolve():
            raise ValueError("The resumed operation's task contract is outside its saved stage.")
    return contract_path


_PREPARED_GRAPH_CONTEXT_FILE = "prepared-context.json"


def _source_snapshot_digest(
    index: ConversationIndex,
    run_truth_scope: list[str],
    *,
    exclude_native_session_id: str | None = None,
    exclude_provider: str | None = None,
    exclude_native_sessions: set[tuple[str, str]] | None = None,
) -> str:
    sessions = index.for_scope(run_truth_scope)
    excluded = set(exclude_native_sessions or set())
    if exclude_native_session_id and exclude_provider:
        excluded.add((exclude_provider, exclude_native_session_id))
    if excluded:
        changed = True
        while changed:
            changed = False
            for item in sessions:
                key = (item.provider, item.session_id)
                parent_key = (item.provider, item.parent_session_id or "")
                if key in excluded or parent_key not in excluded:
                    continue
                excluded.add(key)
                changed = True
    rows = [
        {
            "key": item.key,
            "provider": item.provider,
            "machine": item.source_machine,
            "last_uuid": item.last_uuid,
            "record_count": item.record_count,
            "first_timestamp": (
                item.first_timestamp.isoformat() if item.first_timestamp is not None else None
            ),
            "last_timestamp": (
                item.last_timestamp.isoformat() if item.last_timestamp is not None else None
            ),
        }
        for item in sorted(sessions, key=lambda value: value.key)
        if (item.provider, item.session_id) not in excluded
    ]
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_prepared_graph_context(parent: AgentTaskRecord) -> _PreparedGraphContext:
    if not parent.stage_root:
        raise ValueError("the prior attempt has no retained stage")
    if parent.stage_host:
        stage = RemoteRunStage(parent.stage_host).attach(parent.stage_root)
        assert stage.root is not None
        raw = stage.read_input_text(_PREPARED_GRAPH_CONTEXT_FILE)
    else:
        root = Path(parent.stage_root).resolve()
        path = (root / "inputs" / _PREPARED_GRAPH_CONTEXT_FILE).resolve()
        if path.parent != (root / "inputs").resolve() or not path.is_file():
            raise ValueError("the prior attempt has no prepared context metadata")
        raw = path.read_text(encoding="utf-8")
    return _PreparedGraphContext.model_validate_json(raw)


def _retry_lineage(execution: AgentTaskExecution | None) -> list[AgentTaskRecord]:
    if execution is None or execution.reuses_native_checkpoint:
        return []
    current = execution.store.agent_task(execution.operation_id)
    if current is None or current.parent_operation_id is None:
        return []
    lineage: list[AgentTaskRecord] = []
    seen = {current.operation_id}
    parent_id = current.parent_operation_id
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        parent = execution.store.agent_task(parent_id)
        if (
            parent is None
            or parent.project_id != current.project_id
            or parent.kind != current.kind
        ):
            break
        lineage.append(parent)
        parent_id = parent.parent_operation_id
    return lineage


def _continuation_graph_context(
    service: ProjectService,
    execution: AgentTaskExecution,
    *,
    kind: str,
    request: RunRequest,
    execution_host: str,
) -> _PreparedGraphContext:
    """Load the immutable context owned by a native-session continuation.

    Resume and same-provider correction continue a provider process in its
    original stage. Reassembling here would silently give that process a
    different graph and different evidence than the contract it is continuing.
    """
    record = execution.store.agent_task(execution.operation_id)
    if record is None:
        raise ValueError("The saved continuation task is unavailable. Retry this task.")
    try:
        prepared = _read_prepared_graph_context(record)
    except (OSError, StateUnavailable, ValueError) as exc:
        reason = " ".join(str(exc).split())[:400]
        execution.store.record_agent_task_receipt(
            execution.operation_id,
            "continuation_context_unavailable",
            {"reason": reason, "retry_required": True},
            tier="diagnostic",
        )
        raise ValueError(
            f"The saved prepared context is unavailable ({exc}). Retry this task."
        ) from exc
    expected_scope = sorted(
        request.run_truth_scope or service.manifest.agent.default_run_truth_scope
    )
    current_revision = int(service.graph_snapshot()["revision"])
    problems: list[str] = []
    if prepared.project_id != record.project_id:
        problems.append("project identity changed")
    if prepared.kind != kind:
        problems.append("task kind changed")
    if sorted(prepared.run_truth_scope) != expected_scope:
        problems.append("run truth scope changed")
    if prepared.execution_host != execution_host or record.stage_host != (
        execution_host or None
    ):
        problems.append("execution host changed")
    if prepared.graph_revision != current_revision:
        problems.append(
            f"graph revision moved from {prepared.graph_revision} to {current_revision}"
        )
    if problems:
        reason = "; ".join(problems)
        execution.store.record_agent_task_receipt(
            execution.operation_id,
            "continuation_context_unavailable",
            {"reason": reason, "retry_required": True},
            tier="diagnostic",
        )
        raise ValueError(f"The saved prepared context no longer matches ({reason}). Retry this task.")
    return prepared


def _native_session_paths(
    service: ProjectService,
    parent: AgentTaskRecord,
    *,
    execution_host: str,
) -> list[str]:
    if not parent.native_session_id or parent.stage_host != (execution_host or None):
        return []
    provider = str(parent.request.get("provider") or "")
    roots = profile_for(provider).session_roots(
        service.manifest.sources, remote=bool(execution_host)
    )
    if execution_host:
        return RemoteRunStage(execution_host).find_native_session_files(
            roots, parent.native_session_id
        )
    matches: list[str] = []
    for declared in roots:
        root = Path(declared).expanduser()
        if not root.is_dir():
            continue
        for candidate in root.rglob("*.jsonl"):
            if parent.native_session_id in candidate.stem and candidate.is_file():
                matches.append(str(candidate.resolve()))
                if len(matches) >= 8:
                    return sorted(set(matches))
    return sorted(set(matches))


def _legacy_base_contract(execution: AgentTaskExecution, record: AgentTaskRecord) -> str | None:
    stored = execution.store.agent_task_contract(record.operation_id, "base")
    if stored is not None:
        return stored
    paths = [
        receipt.payload.get("contract_path")
        for receipt in execution.store.agent_task_receipts(record.operation_id)
        if receipt.category == "agent_prompt"
        and receipt.payload.get("launch_kind") == "initial"
        and isinstance(receipt.payload.get("contract_path"), str)
    ]
    for value in paths:
        assert isinstance(value, str)
        try:
            if not record.stage_root:
                continue
            if record.stage_host:
                candidate = PurePosixPath(value)
                if candidate.parent != PurePosixPath(record.stage_root) / "inputs":
                    continue
                content = RemoteRunStage(record.stage_host).attach(
                    record.stage_root
                ).read_input_text(candidate.name)
            else:
                candidate = Path(value).resolve()
                if candidate.parent != (Path(record.stage_root) / "inputs").resolve():
                    continue
                content = candidate.read_text(encoding="utf-8")
            execution.store.record_agent_task_contract(
                record.operation_id,
                "base",
                content,
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
            return content
        except (OSError, StateUnavailable, ValueError):
            continue
    return None


def _read_prior_progress(
    execution: AgentTaskExecution, parent: AgentTaskRecord
) -> str | None:
    messages = (parent.result or {}).get("messages", [])
    if isinstance(messages, list) and messages:
        return "\n\n".join(str(item) for item in messages[:16])
    path = next(
        (
            receipt.payload.get("path")
            for receipt in reversed(execution.store.agent_task_receipts(parent.operation_id))
            if receipt.category == "provider_progress"
            and isinstance(receipt.payload.get("path"), str)
        ),
        None,
    )
    if not isinstance(path, str) or not parent.stage_root:
        return None
    try:
        if parent.stage_host:
            candidate = PurePosixPath(path)
            if candidate.parent != PurePosixPath(parent.stage_root) / "inputs":
                return None
            return RemoteRunStage(parent.stage_host).attach(parent.stage_root).read_input_text(
                candidate.name
            )
        candidate = Path(path).resolve()
        if candidate.parent != (Path(parent.stage_root) / "inputs").resolve():
            return None
        return candidate.read_text(encoding="utf-8")
    except (OSError, StateUnavailable, ValueError):
        return None


def _try_reuse_graph_context(
    service: ProjectService,
    execution: AgentTaskExecution | None,
    *,
    kind: str,
    request: RunRequest,
    execution_machine: str,
    execution_host: str,
) -> _GraphRetryState | None:
    lineage = _retry_lineage(execution)
    if not lineage or execution is None:
        return None
    expected_scope = sorted(
        request.run_truth_scope or service.manifest.agent.default_run_truth_scope
    )
    graph_revision = int(service.graph_snapshot()["revision"])
    index = service.index_snapshot(refresh=True, execution_machine=execution_machine)
    excluded_sessions = {
        (str(item.request.get("provider") or ""), item.native_session_id)
        for item in lineage
        if item.native_session_id
    }
    prepared = None
    prepared_parent = None
    context_errors: list[str] = []
    for candidate in lineage:
        try:
            value = _read_prepared_graph_context(candidate)
            if kind not in {"seed", "refresh"} or value.kind != kind:
                raise ValueError("task kind changed")
            if value.project_id != candidate.project_id:
                raise ValueError("project identity changed")
            if sorted(value.run_truth_scope) != expected_scope:
                raise ValueError("run truth scope changed")
            if value.execution_host != execution_host or candidate.stage_host != (
                execution_host or None
            ):
                raise ValueError("execution host changed")
            if value.graph_revision != graph_revision:
                raise ValueError("graph revision changed")
            if _source_snapshot_digest(
                index,
                value.run_truth_scope,
                exclude_native_sessions=excluded_sessions,
            ) != value.source_snapshot_digest:
                raise ValueError("source snapshot changed")
            prepared = value
            prepared_parent = candidate
            break
        except (OSError, StateUnavailable, ValueError) as exc:
            context_errors.append(f"attempt {candidate.attempt}: {exc}")

    progress_parent = None
    progress: dict[str, object] = {}
    transcript_sources: tuple[str, ...] = ()
    prior_progress_text = None
    retained_patch_text = None
    progress_errors: list[str] = []
    for candidate in lineage:
        try:
            transcript_sources = tuple(
                _native_session_paths(service, candidate, execution_host=execution_host)
            )
        except (OSError, StateUnavailable, ValueError) as exc:
            transcript_sources = ()
            progress_errors.append(f"attempt {candidate.attempt}: {exc}")
        prior_progress_text = _read_prior_progress(execution, candidate)
        retained_patch_text = execution.store.agent_task_patch_output(candidate.operation_id)
        if transcript_sources or prior_progress_text or retained_patch_text:
            progress_parent = candidate
            progress = {
                "prior_operation_id": candidate.operation_id,
                "prior_attempt": candidate.attempt,
                "prior_provider": candidate.request.get("provider"),
                "prior_error": candidate.error,
            }
            if candidate.native_session_id:
                progress["native_session_id"] = candidate.native_session_id
            break
        progress_errors.append(f"attempt {candidate.attempt}: no retained provider progress")

    base_contract_content = next(
        (
            content
            for candidate in reversed(lineage)
            if (content := _legacy_base_contract(execution, candidate)) is not None
        ),
        None,
    )
    return _GraphRetryState(
        lineage=tuple(lineage),
        prepared=prepared,
        prepared_parent=prepared_parent,
        progress_parent=progress_parent,
        progress=progress,
        transcript_sources=transcript_sources,
        prior_progress_text=prior_progress_text if progress_parent else None,
        retained_patch_text=retained_patch_text if progress_parent else None,
        base_contract_content=base_contract_content,
        context_reason="; ".join(context_errors)[:1200] if prepared is None else None,
        progress_reason="; ".join(progress_errors)[:1200] if progress_parent is None else None,
    )


def _record_context_reuse(
    execution: AgentTaskExecution | None,
    *,
    reused: bool,
    reason: str | None = None,
) -> None:
    if execution is None:
        return
    category = "context_reused" if reused else "context_reuse_unavailable"
    payload = {"reused": reused}
    if reason:
        payload["reason"] = " ".join(reason.split())[:400]
    execution.store.record_agent_task_receipt(
        execution.operation_id, category, payload, tier="diagnostic"
    )
    execution.store.record_agent_task_event(
        execution.operation_id,
        (
            "Reusing the prior attempt's prepared context."
            if reused
            else (
                "Prepared context could not be reused; rebuilding it. "
                f"Reason: {' '.join(reason.split())[:400]}"
                if reason
                else "Prepared context could not be reused; rebuilding it."
            )
        ),
        level="info" if reused else "warning",
    )


def _record_progress_handoff(
    execution: AgentTaskExecution | None,
    *,
    handed_off: bool,
    source: AgentTaskRecord | None = None,
    reason: str | None = None,
) -> None:
    if execution is None:
        return
    payload: dict[str, object] = {"handed_off": handed_off}
    if source is not None:
        payload.update(
            {
                "source_operation_id": source.operation_id,
                "source_attempt": source.attempt,
                "source_provider": source.request.get("provider"),
            }
        )
    if reason:
        payload["reason"] = " ".join(reason.split())[:400]
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "progress_handed_off" if handed_off else "progress_handoff_unavailable",
        payload,
        tier="diagnostic",
    )
    execution.store.record_agent_task_event(
        execution.operation_id,
        (
            f"Handing off provider progress from attempt {source.attempt}."
            if handed_off and source is not None
            else (
                "No prior provider progress was handed off. "
                f"Reason: {' '.join(reason.split())[:400]}"
                if reason
                else "No prior provider progress was handed off."
            )
        ),
        level="info" if handed_off else "warning",
    )


def _stage_prepared_graph_context(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    *,
    project_id: str,
    kind: str,
    graph_revision: int,
    execution_host: str,
    source_snapshot_digest: str,
    original_contract_path: str,
    context: RunContext,
    previous_coverage: CoverageBoundary,
) -> None:
    prepared = _PreparedGraphContext(
        project_id=project_id,
        kind=kind,
        graph_revision=graph_revision,
        run_truth_scope=context.run_truth_scope,
        execution_host=execution_host,
        source_snapshot_digest=source_snapshot_digest,
        original_contract_path=original_contract_path,
        context=context,
        previous_coverage=previous_coverage,
    )
    _stage_json_task_input(
        local_stage,
        remote_stage,
        _PREPARED_GRAPH_CONTEXT_FILE,
        prepared.model_dump(mode="json"),
    )


def _stage_authorized_session_keys(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    context: RunContext,
) -> str:
    return _stage_json_task_input(
        local_stage,
        remote_stage,
        "authorized-session-keys.json",
        [{"key": session.key, "path": session.path} for session in context.sessions],
    )


def _stage_json_task_input(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    label: str,
    value: object,
) -> str:
    return _stage_task_input(
        local_stage,
        remote_stage,
        label,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _project_native_transcripts(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    sources: tuple[str, ...],
    label: str,
) -> list[str]:
    if not sources:
        return []
    if remote_stage is not None:
        return remote_stage.project_host_files(list(sources), label)
    if local_stage is None:
        raise RuntimeError("local run stage is unavailable")
    inputs = local_stage / "inputs"
    target = inputs / _safe_stage_name(label)
    if target.exists():
        raise ValueError("immutable native transcript projection already exists")
    staged = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=inputs))
    try:
        projected: list[str] = []
        for index, value in enumerate(sources):
            source = Path(value).resolve()
            if not source.is_file():
                raise ValueError(f"native transcript is unavailable: {source}")
            destination = staged / f"{index:02d}.jsonl"
            # This must be a snapshot, not a hard link. A provider may keep
            # appending to its native transcript, and chmod on a hard link
            # would also mutate the provider-owned source inode.
            shutil.copy2(source, destination)
            destination.chmod(0o400)
            projected.append(str(target / destination.name))
        staged.chmod(0o500)
        os.replace(staged, target)
        return projected
    finally:
        if staged.exists():
            _remove_local_tree(staged, inputs)


def _stage_task_contract(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    label: str,
    content: str,
    *,
    execution: AgentTaskExecution | None = None,
    role: str | None = None,
) -> tuple[str, str]:
    if execution is not None:
        execution.store.record_agent_task_contract(
            execution.operation_id,
            role or label,
            content,
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )
    contract_path = _stage_task_input(local_stage, remote_stage, label, content)
    return contract_path, PromptFactory.launch_prompt(contract_path)


_RequestT = TypeVar("_RequestT", bound=BaseModel)


def _pinned_to_profile(request: _RequestT, profile: AgentSurfaceConfig) -> _RequestT:
    """Pin the resolved launch configuration onto the request the run will use."""
    return request.model_copy(
        update={
            "provider": profile.provider,
            "model": profile.model or None,
            "reasoning": profile.reasoning,
            "run_on": profile.run_on,
        }
    )


def _record_agent_launch_receipt(
    execution: AgentTaskExecution | None,
    request: RunRequest | CoachRequest,
    *,
    prompt: str,
    contract_path: str,
    remote: bool,
    resumed: bool,
    continuation: str | None = None,
    extra: dict[str, object],
) -> None:
    if execution is None:
        return
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "agent_launch",
        {
            "provider": request.provider,
            "run_on": request.run_on,
            "remote": remote,
            "resumed": resumed,
            **({"continuation_cause": continuation} if continuation is not None else {}),
            **extra,
        },
    )
    encoded = prompt.encode("utf-8")
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "agent_prompt",
        {
            "prompt": prompt,
            "contract_path": contract_path,
            "byte_length": len(encoded),
            "line_count": len(prompt.splitlines()),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "resumed": resumed,
            **({"continuation_cause": continuation} if continuation is not None else {}),
            **extra,
        },
        tier="diagnostic",
    )


@dataclass
class _ProviderOutcome:
    """What one pass of a provider stream leaves behind for its caller.

    `answers` collects only the provider's labelled final assistant messages; a
    `message` is a trace and is never promoted into it. What an answer is worth
    is the caller's decision — an ingest run leaves this list unread.
    """

    session_id: str | None = None
    completed: bool = False
    failed: bool = False
    paused: bool = False
    answers: list[str] = dataclasses.field(default_factory=list)
    trace_messages: list[str] = dataclasses.field(default_factory=list)
    exit_evidence: dict[str, object] | None = None
    exit_recorded: bool = False


async def _stream_agent_events(
    launcher: AgentLauncher,
    request: RunRequest,
    prompt: str,
    *,
    workspace: Path,
    session_id: str | None,
    read_dirs: list[Path],
    write_dirs: list[Path],
    execution_host: str,
    execution: AgentTaskExecution | None,
    remote_stage: RemoteRunStage | None,
    capability: AgentCapability,
    outcome: _ProviderOutcome,
    binary: str | None,
) -> AsyncIterator[str]:
    """Run one provider pass, recording its outcome and forwarding wire events.

    Terminal and labelled events are withheld from the wire: the caller decides
    what a completed run, an answer, or a trace is worth in its own protocol.
    """
    if remote_stage is not None:
        try:
            await asyncio.to_thread(remote_stage.finalize_inputs)
        except (OSError, StateUnavailable, ValueError) as exc:
            outcome.failed = True
            yield _sse(AgentEvent(event="error", text=str(exc)))
            return
    async with aclosing(
        launcher.stream(
            request.provider,
            prompt,
            cwd=workspace,
            model=request.model,
            reasoning=request.reasoning,
            session_id=session_id,
            read_dirs=read_dirs,
            write_dirs=write_dirs,
            host=execution_host,
            control=execution.control if execution is not None else None,
            remote_pid_file=(
                str(remote_stage.root / "agent.pid")
                if execution is not None and remote_stage is not None and remote_stage.root
                else None
            ),
            capability=capability,
            binary=binary,
        )
    ) as stream:
        async for event in stream:
            if event.event == "provider_exit":
                try:
                    evidence = json.loads(event.text)
                except (json.JSONDecodeError, TypeError, ValueError):
                    evidence = {"unparsed": event.text[:400]}
                outcome.exit_evidence = evidence if isinstance(evidence, dict) else {
                    "unparsed": event.text[:400]
                }
                _record_provider_exit(
                    execution,
                    outcome,
                    workspace=workspace,
                    remote_stage=remote_stage,
                )
                continue
            if event.event == "paused":
                outcome.paused = True
            if event.event == "session" and event.session_id:
                outcome.session_id = event.session_id
                if execution_host and execution is None:
                    continue
            if event.event == "answer":
                outcome.answers.append(event.text)
                continue
            if event.event == "message":
                if event.text.strip() and len(outcome.trace_messages) < 16:
                    outcome.trace_messages.append(event.text.strip()[:16_000])
                continue
            if event.event == "error":
                outcome.failed = True
            if event.event == "done":
                outcome.completed = True
                continue
            yield _sse(event)


def _record_provider_exit(
    execution: AgentTaskExecution | None,
    outcome: _ProviderOutcome,
    *,
    workspace: Path,
    remote_stage: RemoteRunStage | None,
) -> None:
    if execution is None or outcome.exit_evidence is None or outcome.exit_recorded:
        return
    payload = dict(outcome.exit_evidence)
    try:
        if remote_stage is not None:
            patch_exists = "patch.json" in remote_stage.list_workspace_files()
        else:
            patch_exists = (workspace / "patch.json").is_file()
        payload["patch_json_exists"] = patch_exists
    except (OSError, StateUnavailable, ValueError) as exc:
        payload["patch_json_exists"] = None
        payload["patch_check_error"] = " ".join(str(exc).split())[:400]
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "provider_exit",
        payload,
        tier="diagnostic",
    )
    outcome.exit_recorded = True


async def _stream_graph_run(
    service: ProjectService,
    launcher: AgentLauncher,
    kind: str,
    request: RunRequest,
    data_dir: Path,
    execution: AgentTaskExecution | None = None,
) -> AsyncIterator[str]:
    continuation = execution.continuation if execution is not None else "fresh"
    reuses_native_checkpoint = bool(
        execution is not None and execution.reuses_native_checkpoint
    )
    if request.session_id and not reuses_native_checkpoint:
        yield _sse(
            AgentEvent(
                event="error",
                text=(
                    "Seed and refresh sessions can only be resumed from an RCP background "
                    "task checkpoint."
                ),
            )
        )
        return
    surface: AgentSurface = "seed" if kind == "seed" else "refresh"
    try:
        profile = service.resolve_agent_profile(
            surface,
            provider=request.provider,
            model=request.model,
            reasoning=request.reasoning,
            run_on=request.run_on,
        )
    except ValueError as exc:
        yield _sse(AgentEvent(event="error", text=str(exc)))
        return
    request = _pinned_to_profile(request, profile)
    local_stage: Path | None = None
    execution_machine = service.manifest.machine_map[profile.run_on]
    execution_host = execution_machine.host
    provider_binary = execution_machine.provider_paths.get(profile.provider)
    remote_stage: RemoteRunStage | None = None
    run_lock = service.history.workspace.run_lock()
    run_lock_acquired = False
    cache_pin = None
    applied = False
    retry_state: _GraphRetryState | None = None
    source_snapshot_digest = ""
    graph_revision = 0
    try:
        try:
            run_lock.__enter__()
            run_lock_acquired = True
        except StateUnavailable as exc:
            yield _sse(AgentEvent(event="error", text=str(exc)))
            return
        try:
            continuation_prepared = (
                _continuation_graph_context(
                    service,
                    execution,
                    kind=kind,
                    request=request,
                    execution_host=execution_host,
                )
                if execution is not None and reuses_native_checkpoint
                else None
            )
            retry_state = (
                None
                if continuation_prepared is not None
                else _try_reuse_graph_context(
                    service,
                    execution,
                    kind=kind,
                    request=request,
                    execution_machine=execution_machine.alias,
                    execution_host=execution_host,
                )
            )
            if continuation_prepared is not None:
                context = continuation_prepared.context
                source_snapshot_digest = continuation_prepared.source_snapshot_digest
                graph_revision = continuation_prepared.graph_revision
                _record_context_reuse(execution, reused=True)
            elif retry_state is not None and retry_state.prepared is not None:
                context = retry_state.prepared.context
                source_snapshot_digest = retry_state.prepared.source_snapshot_digest
                graph_revision = retry_state.prepared.graph_revision
                _record_context_reuse(execution, reused=True)
            else:
                if retry_state is not None:
                    _record_context_reuse(
                        execution, reused=False, reason=retry_state.context_reason
                    )
                cache_pin = service.indexer.pin_rebuildable_scope()
                pin_artifact = cache_pin.__enter__()
                context = service.assemble_run(
                    request,
                    surface,
                    pin_artifact=pin_artifact,
                )
                _record_context_receipt(execution, context, surface=surface)
                _report_source_errors(execution, context.source_errors)
                graph_revision = context.graph_revision
                execution_record = (
                    execution.store.agent_task(execution.operation_id)
                    if execution is not None
                    else None
                )
                if execution_record is not None:
                    source_snapshot_digest = _source_snapshot_digest(
                        service.index_snapshot(
                            execution_machine=execution_machine.alias,
                            pin_artifact=pin_artifact,
                        ),
                        context.run_truth_scope,
                        exclude_native_sessions=(
                            {
                                (str(item.request.get("provider") or ""), item.native_session_id)
                                for item in retry_state.lineage
                                if item.native_session_id
                            }
                            if retry_state is not None
                            else None
                        ),
                    )
            previous_coverage = (
                continuation_prepared.previous_coverage
                if continuation_prepared is not None
                else retry_state.prepared.previous_coverage
                if retry_state is not None and retry_state.prepared is not None
                else CoverageBoundary.model_validate_json(
                    Path(context.coverage_path).read_text(encoding="utf-8")
                )
            )
            # One scratch folder per operation, reused by every rung of the recovery
            # ladder so a resumed native session still points at the directory it was
            # originally given. It is never deleted on failure; _sweep_stale_stages
            # ages it out instead.
            if execution_host:
                if request.session_id and not reuses_native_checkpoint:
                    raise ValueError(
                        "Remote native-session resume needs persistent run staging; "
                        "start this chat on the local execution machine."
                    )
                if reuses_native_checkpoint and execution is not None and execution.stage_root:
                    remote_stage = RemoteRunStage(execution_host).attach(execution.stage_root)
                elif reuses_native_checkpoint:
                    raise ValueError(
                        "The interrupted remote operation has no staging checkpoint; retry it."
                    )
                else:
                    remote_stage = RemoteRunStage(execution_host).open(
                        execution.operation_id if execution is not None else None
                    )
                    if execution is not None:
                        assert remote_stage.root is not None
                        execution.checkpoint_stage(execution_host, str(remote_stage.root))
                    if retry_state is None or retry_state.prepared is None:
                        context = _stage_graph_context(
                            context,
                            service,
                            remote_stage,
                            execution_machine.alias,
                        )
                workspace = Path(str(remote_stage.workspace))
                patch_path = str(remote_stage.workspace / "patch.json")
            else:
                stage_root = _swept_stage_root(data_dir)
                if reuses_native_checkpoint and execution is not None and execution.stage_root:
                    local_stage = Path(execution.stage_root).resolve()
                    if local_stage.parent != stage_root.resolve() or not local_stage.is_dir():
                        raise ValueError(
                            "The interrupted local operation has no valid staging checkpoint; "
                            "retry it instead."
                        )
                    context = _rebind_graph_conversations(
                        context, local_stage / "inputs" / "conversations"
                    )
                elif reuses_native_checkpoint:
                    raise ValueError(
                        "The interrupted local operation has no staging checkpoint; retry it."
                    )
                else:
                    name = execution.operation_id if execution is not None else uuid.uuid4().hex
                    local_stage = stage_root / _safe_stage_name(name)
                    local_stage.mkdir(parents=True, exist_ok=True)
                    if execution is not None:
                        execution.checkpoint_stage("", str(local_stage))
                    if retry_state is None or retry_state.prepared is None:
                        context = _stage_local_graph_conversations(context, local_stage)
                workspace = local_stage
                patch_path = str(local_stage / "patch.json")

            read_dirs = _agent_read_dirs(context, remote_stage, service, execution_machine.alias)
            if (
                retry_state is not None
                and retry_state.prepared is not None
                and retry_state.prepared_parent is not None
                and retry_state.prepared_parent.stage_root
            ):
                parent_inputs = (
                    PurePosixPath(retry_state.prepared_parent.stage_root) / "inputs"
                    if execution_host
                    else Path(retry_state.prepared_parent.stage_root) / "inputs"
                )
                read_dirs.append(Path(str(parent_inputs)))
                for conversation_root in _conversation_roots(context).values():
                    read_dirs.append(Path(conversation_root))
            token = _task_token(execution)
            if reuses_native_checkpoint:
                if not request.session_id:
                    raise ValueError(
                        "The interrupted operation has no native agent session; retry it instead."
                    )
                assert execution is not None
                original_contract_path = _parent_task_contract_path(
                    execution, local_stage, remote_stage
                )
                if continuation == "correction":
                    diagnostics_path = _stage_json_task_input(
                        local_stage,
                        remote_stage,
                        f"task-{token}-retry-correction.json",
                        {
                            "kind": kind,
                            "prior_attempt_diagnostics": list(execution.retry_feedback),
                            "retained_patch_path": patch_path,
                        },
                    )
                    contract = PromptFactory.continuation_task_contract(
                        original_contract_path=original_contract_path,
                        mode="patch_correction",
                        patch_path=patch_path,
                        diagnostics_path=diagnostics_path,
                    )
                    contract_path, prompt = _stage_task_contract(
                        local_stage,
                        remote_stage,
                        f"task-{token}-correction.md",
                        contract,
                        execution=execution,
                        role="correction",
                    )
                else:
                    # A literal native Resume already owns its immutable contract
                    # and saved stage. Its only new instruction is to continue.
                    contract_path = original_contract_path
                    prompt = "Continue the interrupted task."
                base_contract_path = contract_path
            else:
                base_contract_content = (
                    retry_state.base_contract_content
                    if retry_state is not None and retry_state.prepared is not None
                    else None
                )
                if base_contract_content is None:
                    schema_path = _stage_json_task_input(
                        local_stage,
                        remote_stage,
                        f"task-{token}-patch-schema.json",
                        agent_output_schema(),
                    )
                    human_request_path = (
                        _stage_task_input(
                            local_stage,
                            remote_stage,
                            f"task-{token}-human-request.txt",
                            request.message,
                        )
                        if request.message
                        else None
                    )
                    retry_diagnostics_path = (
                        _stage_json_task_input(
                            local_stage,
                            remote_stage,
                            f"task-{token}-retry-diagnostics.json",
                            {"prior_attempt_diagnostics": list(execution.retry_feedback)},
                        )
                        if execution is not None and execution.retry_feedback
                        else None
                    )
                    authorized_session_keys_path = _stage_authorized_session_keys(
                        local_stage,
                        remote_stage,
                        context,
                    )
                    base_contract_content = service.graph_task_contract(
                        kind,
                        project_name=context.project_name,
                        ontology_path=f"{context.graph_path}#ontology",
                        graph_path=context.graph_path,
                        research_path=context.research_md_path,
                        conversation_roots=_conversation_roots(context),
                        authorized_session_keys_path=authorized_session_keys_path,
                        cursor_path=str(
                            PurePosixPath(context.coverage_path).with_name("cursors.json")
                        ),
                        repositories=[
                            {"alias": item.alias, "host": item.host, "path": item.path}
                            for item in context.repositories
                        ],
                        patch_path=patch_path,
                        output_schema_path=schema_path,
                        human_request_path=human_request_path,
                        retry_diagnostics_path=retry_diagnostics_path,
                    )
                base_label = (
                    f"task-{token}-base.md"
                    if retry_state is not None
                    else f"task-{token}-initial.md"
                )
                base_contract_path, base_prompt = _stage_task_contract(
                    local_stage,
                    remote_stage,
                    base_label,
                    base_contract_content,
                    execution=execution,
                    role="base",
                )

                if retry_state is not None and retry_state.progress_parent is not None:
                    handoff = dict(retry_state.progress)
                    transcript_paths = _project_native_transcripts(
                        local_stage,
                        remote_stage,
                        retry_state.transcript_sources,
                        f"task-{token}-native-transcripts",
                    )
                    if transcript_paths:
                        handoff["native_transcript_paths"] = transcript_paths
                    if retry_state.prior_progress_text:
                        handoff["prior_progress_path"] = _stage_task_input(
                            local_stage,
                            remote_stage,
                            f"task-{token}-prior-progress.md",
                            retry_state.prior_progress_text,
                        )
                    if retry_state.retained_patch_text:
                        handoff["retained_patch_path"] = _stage_task_input(
                            local_stage,
                            remote_stage,
                            f"task-{token}-prior-patch.json",
                            retry_state.retained_patch_text,
                        )
                    handoff_path = _stage_json_task_input(
                        local_stage,
                        remote_stage,
                        f"task-{token}-handoff.json",
                        handoff,
                    )
                    contract = PromptFactory.retry_handoff_task_contract(
                        kind=kind,
                        handoff_path=handoff_path,
                        original_contract_path=base_contract_path,
                        patch_path=patch_path,
                    )
                    contract_path, prompt = _stage_task_contract(
                        local_stage,
                        remote_stage,
                        f"task-{token}-retry.md",
                        contract,
                        execution=execution,
                        role="retry",
                    )
                    _record_progress_handoff(
                        execution,
                        handed_off=True,
                        source=retry_state.progress_parent,
                    )
                else:
                    contract_path, prompt = base_contract_path, base_prompt
                    if retry_state is not None:
                        _record_progress_handoff(
                            execution,
                            handed_off=False,
                            reason=retry_state.progress_reason,
                        )
            if not reuses_native_checkpoint and execution is not None:
                execution_record = execution.store.agent_task(execution.operation_id)
                if execution_record is not None:
                    _stage_prepared_graph_context(
                        local_stage,
                        remote_stage,
                        project_id=execution_record.project_id,
                        kind=kind,
                        graph_revision=graph_revision,
                        execution_host=execution_host,
                        source_snapshot_digest=source_snapshot_digest,
                        original_contract_path=base_contract_path,
                        context=context,
                        previous_coverage=previous_coverage,
                    )
            base_contract_path = contract_path
        except (ReplayHalted, StateUnavailable, ValueError) as exc:
            yield _sse(AgentEvent(event="error", text=str(exc)))
            return

        native_session_id = request.session_id
        session_id = request.session_id if reuses_native_checkpoint else None
        rounds = 0
        last_problem = (
            execution.retry_feedback[0]
            if execution is not None
            and continuation == "correction"
            and execution.retry_feedback
            else None
        )
        while True:
            # A correction reuses its predecessor's stage, so the patch it is
            # meant to replace is still lying there. Remember it rather than
            # deleting it: invariant 9 says a failed run keeps its patch text.
            # Only a reused stage can hold one, so a first launch skips the probe
            # and its remote round-trip.
            correcting = bool(rounds) or continuation == "correction"
            pre_launch_patch_digest = (
                _existing_patch_digest(workspace, remote_stage)
                if reuses_native_checkpoint or rounds
                else None
            )
            _record_agent_launch_receipt(
                execution,
                request,
                prompt=prompt,
                contract_path=contract_path,
                remote=bool(execution_host),
                resumed=reuses_native_checkpoint,
                continuation=(
                    "correction" if rounds else continuation
                ),
                extra={
                    "surface": surface,
                    "capability": "scratch_patch",
                    "network_access": True,
                    "launch_kind": (
                        "correction"
                        if rounds
                        else continuation
                    ),
                    "correction_round": rounds,
                },
            )
            # An ingest run's deliverable is the patch file; its prose only confirms
            # it was written, so the collected answers go unread. `done` is held back
            # until the patch is applied so the wire order stays applied_revision,
            # then done.
            outcome = _ProviderOutcome(session_id=native_session_id)
            async with aclosing(
                _stream_agent_events(
                    launcher,
                    request,
                    prompt,
                    workspace=workspace,
                    session_id=session_id,
                    read_dirs=read_dirs,
                    write_dirs=[],
                    execution_host=execution_host,
                    execution=execution,
                    remote_stage=remote_stage,
                    capability="scratch_patch",
                    outcome=outcome,
                    binary=provider_binary,
                )
            ) as stream:
                async for frame in stream:
                    if execution is not None:
                        streamed = AgentEvent.model_validate_json(
                            frame.removeprefix("data: ").strip()
                        )
                        execution_record = execution.store.agent_task(
                            execution.operation_id
                        )
                        if streamed.event == "error" and execution_record is not None:
                            if outcome.trace_messages:
                                progress_path = _stage_task_input(
                                    local_stage,
                                    remote_stage,
                                    f"task-{token}-provider-progress.md",
                                    "\n\n".join(outcome.trace_messages),
                                )
                                try:
                                    if remote_stage is not None:
                                        await asyncio.to_thread(remote_stage.finalize_inputs)
                                except (OSError, StateUnavailable, ValueError) as exc:
                                    execution.store.record_agent_task_event(
                                        execution.operation_id,
                                        f"Provider progress could not be retained: {exc}",
                                        level="warning",
                                    )
                                else:
                                    execution.store.record_agent_task_receipt(
                                        execution.operation_id,
                                        "provider_progress",
                                        {"path": progress_path},
                                        tier="diagnostic",
                                    )
                            execution.store.record_agent_task_receipt(
                                execution.operation_id,
                                "provider_terminal_error",
                                {
                                    "provider": request.provider,
                                    "classification": classify_terminal_error(streamed.text),
                                },
                                tier="diagnostic",
                            )
                            execution.store.record_agent_task_receipt(
                                execution.operation_id,
                                "patch_collection_skipped",
                                {
                                    "reason": "provider_terminal_error",
                                    "patch_availability_evaluated": False,
                                },
                                tier="diagnostic",
                            )
                    yield frame
            _record_provider_exit(
                execution,
                outcome,
                workspace=workspace,
                remote_stage=remote_stage,
            )
            native_session_id = outcome.session_id
            if not outcome.completed:
                if outcome.failed or outcome.paused:
                    return
                yield _sse(
                    AgentEvent(event="error", text=f"{request.provider} produced no result.")
                )
                return

            if execution is not None:
                execution.store.update_agent_task_message(
                    execution.operation_id,
                    "Validating and applying the graph update.",
                    phase="applying",
                    event=True,
                )
            stale_patch = False
            try:
                patch_text, output_name = _collect_patch_text(workspace, remote_stage)
                unchanged = (
                    pre_launch_patch_digest is not None
                    and hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
                    == pre_launch_patch_digest
                )
                if unchanged and execution is not None:
                    execution.store.record_agent_task_receipt(
                        execution.operation_id,
                        "patch_predates_launch",
                        {"correction_round": rounds, "accepted": not correcting},
                        tier="diagnostic",
                    )
                if unchanged and correcting:
                    # Applying it would report a correction that never happened.
                    # The substantive diagnostic still leads: why the patch is
                    # unacceptable is what the human and the agent both need,
                    # and "you did not rewrite it" only explains this launch.
                    stale_patch = True
                    raise AgentOutputProblem(
                        (f"{last_problem} " if last_problem else "")
                        + "The patch file is byte-identical to the one this launch "
                        "was asked to correct, so no corrected patch was written. "
                        "Rewrite patch.json with the changes the diagnostic requires."
                    )
            except AgentOutputProblem as exc:
                problem = str(exc)
                if not stale_patch:
                    last_problem = problem
            else:
                if execution is not None:
                    # Persisted before validation: a patch that fails validation is
                    # still the run's work product and must survive the failure.
                    execution.store.record_agent_task_patch_output(
                        execution.operation_id, patch_text
                    )
                    execution.store.record_agent_task_receipt(
                        execution.operation_id,
                        "patch_retained",
                        {
                            "byte_length": len(patch_text.encode("utf-8")),
                            "file_name": output_name,
                        },
                        tier="diagnostic",
                    )
                    if output_name != "patch.json":
                        execution.store.record_agent_task_event(
                            execution.operation_id,
                            f"Recovered the patch from {output_name}.",
                            level="warning",
                        )
                try:
                    patch, _ = service.parse_patch_output([patch_text])
                    _record_patch_receipt(
                        execution,
                        patch,
                        byte_length=len(patch_text.encode("utf-8")),
                    )
                    _require_agent_patch_identity(patch, kind)
                    patch = normalize_agent_patch_bookkeeping(patch)
                    patch = normalize_processed_cursors(context, patch, previous_coverage)
                    validate_agent_patch_shape(patch)
                    validate_session_evidence(context, patch, previous_coverage)
                except ValueError as exc:
                    problem = str(exc)
                    last_problem = problem
                else:
                    try:
                        _appended, result = service.history.append(
                            patch,
                            discard_on_reject=True,
                        )
                    except PatchRejected as exc:
                        problem = str(exc)
                        last_problem = problem
                        if execution is not None:
                            execution.store.record_agent_task_receipt(
                                execution.operation_id,
                                "patch_rejected",
                                {
                                    "round": rounds,
                                    "messages": [
                                        item.model_dump(mode="json")
                                        for item in exc.report.messages
                                    ],
                                },
                                tier="diagnostic",
                            )
                    except (ReplayHalted, StateUnavailable) as exc:
                        yield _sse(AgentEvent(event="error", text=str(exc)))
                        return
                    else:
                        _record_patch_applied_receipt(execution, result.state)
                        applied = True
                        yield _sse(
                            AgentEvent(
                                event="message",
                                text=json.dumps(
                                    {"applied_revision": result.state.revision},
                                    separators=(",", ":"),
                                ),
                            )
                        )
                        yield _sse(AgentEvent(event="done"))
                        return

            # Rungs 2 and 3: hand the concrete problem back to the agent that is still
            # holding the analysis, rather than discarding the run and asking a human.
            if rounds >= _MAX_CORRECTION_ROUNDS or not native_session_id:
                yield _sse(AgentEvent(event="error", text=problem))
                return
            rounds += 1
            if execution is not None:
                execution.store.record_agent_task_receipt(
                    execution.operation_id,
                    "patch_correction_requested",
                    {"round": rounds, "problem": problem[:400]},
                    tier="diagnostic",
                )
                execution.store.record_agent_task_event(
                    execution.operation_id,
                    f"Asking the agent to correct its patch (round {rounds}).",
                    level="info",
                )
                execution.store.update_agent_task_message(
                    execution.operation_id,
                    "Asking the agent to correct its patch.",
                    phase="agent",
                    event=True,
                )
            diagnostics_path = _stage_json_task_input(
                local_stage,
                remote_stage,
                f"task-{token}-correction-{rounds}.json",
                {"kind": kind, "problem": problem},
            )
            correction_contract = PromptFactory.continuation_task_contract(
                original_contract_path=base_contract_path,
                mode="patch_correction",
                patch_path=patch_path,
                diagnostics_path=diagnostics_path,
            )
            contract_path, prompt = _stage_task_contract(
                local_stage,
                remote_stage,
                f"task-{token}-correction-{rounds}.md",
                correction_contract,
            )
            session_id = native_session_id
    finally:
        if cache_pin is not None:
            cache_pin.__exit__(None, None, None)
        if applied:
            if local_stage is not None:
                with suppress(OSError, ValueError):
                    _remove_local_tree(local_stage, local_stage.parent)
            if remote_stage is not None:
                remote_stage.close()
            if execution is not None and (local_stage is not None or remote_stage is not None):
                execution.store.clear_agent_task_stage(execution.operation_id)
        if run_lock_acquired:
            run_lock.__exit__(None, None, None)


async def _stream_chat_run(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    execution: AgentTaskExecution | None = None,
) -> AsyncIterator[str]:
    """Dispatch one conversation turn to its captured authority policy."""

    stream = _stream_work_run if request.mode == "work" else _stream_discuss_run
    async with aclosing(
        stream(service, launcher, request, data_dir, execution=execution)
    ) as events:
        async for frame in events:
            yield frame


@dataclass(frozen=True)
class _WorkPatchFailure:
    message: str
    correctable: bool
    change_summary: tuple[str, ...] = ()
    proposal_ids: tuple[str, ...] = ()


async def _stream_work_run(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    execution: AgentTaskExecution | None = None,
) -> AsyncIterator[str]:
    """Run one operational conversation turn with optional graph reflection."""

    if execution is not None and execution.continuation == "graph_repair":
        async with aclosing(
            _stream_work_graph_repair(
                service,
                launcher,
                request,
                data_dir,
                execution=execution,
            )
        ) as stream:
            async for frame in stream:
                yield frame
        return

    resuming = bool(execution is not None and execution.reuses_native_checkpoint)
    surface: AgentSurface = "project_chat" if request.chat_scope == "project" else "node_chat"
    try:
        profile = service.resolve_agent_profile(
            surface,
            provider=request.provider,
            model=request.model,
            reasoning=request.reasoning,
            run_on=request.run_on,
        )
    except ValueError as exc:
        yield _sse(AgentEvent(event="error", text=str(exc)))
        return
    request = _pinned_to_profile(request, profile)
    local_stage: Path | None = None
    execution_machine = service.manifest.machine_map[profile.run_on]
    execution_host = execution_machine.host
    provider_binary = execution_machine.provider_paths.get(profile.provider)
    remote_stage: RemoteRunStage | None = None
    conversation_projection: Path | PurePosixPath | None = None
    artifact_scope_id: str | None = None
    artifact_directory: Path | PurePosixPath | None = None
    provider_started = False
    outcome = _ProviderOutcome(session_id=request.session_id)
    try:
        try:
            context = service.assemble_chat(request)
            base_revision = context.graph_revision
            if resuming:
                base_revision = _first_chat_base_revision(execution, base_revision)
            _record_chat_context_receipt(execution, context, surface=surface)
            if request.session_id and not resuming and not _known_chat_session(service, request):
                raise ValueError(
                    "That native session was not created by this chat. Start a new chat instead."
                )
            stage_name = _chat_stage_name(service, request, execution)
            if execution_host:
                if resuming:
                    stage_root = _validated_remote_chat_resume_stage(
                        execution, execution_host, stage_name
                    )
                    remote_stage = RemoteRunStage(execution_host).attach(stage_root)
                else:
                    remote_stage = RemoteRunStage(execution_host).open(stage_name, reuse=True)
                assert remote_stage.root is not None
                if execution is not None:
                    execution.checkpoint_stage(execution_host, str(remote_stage.root))
                if not resuming:
                    context = context.model_copy(
                        update=_stage_context_paths(
                            context, service, remote_stage, execution_machine.alias
                        )
                    )
                workspace = Path(str(remote_stage.workspace))
                patch_path = str(remote_stage.workspace / "patch.json")
            else:
                stage_root = _swept_stage_root(data_dir)
                expected_stage = stage_root / stage_name
                if resuming:
                    local_stage = _validated_local_chat_resume_stage(execution, expected_stage)
                else:
                    local_stage = expected_stage
                    local_stage.mkdir(parents=True, exist_ok=True)
                if execution is not None:
                    execution.checkpoint_stage("", str(local_stage))
                workspace = local_stage
                patch_path = str(local_stage / "patch.json")
            if not resuming:
                _clear_stale_patch(workspace, remote_stage)
            artifact_scope_id = (
                _logical_chat_turn_operation_id(execution.store, execution.operation_id)
                if execution is not None and resuming
                else execution.operation_id
                if execution is not None
                else str(uuid.uuid4())
            )
            if remote_stage is not None:
                artifact_directory = remote_stage.prepare_artifact_directory(
                    artifact_scope_id, reuse=resuming
                )
            else:
                assert local_stage is not None
                artifact_directory = _prepare_local_artifact_directory(
                    local_stage, artifact_scope_id, reuse=resuming
                )
            if resuming:
                conversation_projection = _saved_chat_conversation_projection(
                    local_stage, remote_stage
                )
                context = _rebind_chat_conversations(
                    context,
                    conversation_projection,
                    verify_local=remote_stage is None,
                )
            else:
                context, conversation_projection = _project_chat_conversations(
                    context, local_stage, remote_stage
                )

            read_dirs = _chat_read_dirs(
                context,
                remote_stage,
                service,
                execution_machine.alias,
                conversation_projection,
            )
            write_dirs = _work_write_dirs(
                context,
                service,
                execution_machine.alias,
                remote=remote_stage is not None,
            )
            token = _task_token(execution)
            if resuming:
                if not request.session_id:
                    raise ValueError(
                        "The interrupted Work turn has no native agent session; retry it instead."
                    )
                assert execution is not None
                original_contract_path = _parent_task_contract_path(
                    execution, local_stage, remote_stage
                )
                base_contract_path = original_contract_path
                contract = PromptFactory.continuation_task_contract(
                    original_contract_path=original_contract_path,
                    mode="resume",
                    patch_path=patch_path,
                )
                contract_path, prompt = _stage_task_contract(
                    local_stage,
                    remote_stage,
                    f"task-{token}-resume.md",
                    contract,
                )
            else:
                assert request.message is not None
                human_request_path = _stage_task_input(
                    local_stage,
                    remote_stage,
                    f"task-{token}-human-request.txt",
                    request.message,
                )
                schema_path = _stage_json_task_input(
                    local_stage,
                    remote_stage,
                    f"task-{token}-patch-schema.json",
                    agent_output_schema(),
                )
                retry_diagnostics_path = (
                    _stage_json_task_input(
                        local_stage,
                        remote_stage,
                        f"task-{token}-retry-diagnostics.json",
                        {"prior_attempt_diagnostics": list(execution.retry_feedback)},
                    )
                    if execution is not None and execution.retry_feedback
                    else None
                )
                contract = PromptFactory.work_task_contract(
                    project_name=context.project_name,
                    ontology_path=f"{context.graph_path}#ontology",
                    graph_path=context.graph_path,
                    research_path=context.research_md_path,
                    focused_node_id=str(context.node["id"]) if context.node else None,
                    conversation_roots=_chat_conversation_roots(context),
                    conversations_unreachable=context.conversations_unreachable,
                    repositories=[
                        {"alias": item.alias, "host": item.host, "path": item.path}
                        for item in context.repositories
                    ],
                    introduction_path=context.introduction_path,
                    human_request_path=human_request_path,
                    patch_path=patch_path,
                    artifact_path=str(artifact_directory),
                    output_schema_path=schema_path,
                    retry_diagnostics_path=retry_diagnostics_path,
                )
                contract_path, prompt = _stage_task_contract(
                    local_stage,
                    remote_stage,
                    f"task-{token}-initial.md",
                    contract,
                    execution=execution,
                    role="work",
                )
                base_contract_path = contract_path
        except (OSError, ReplayHalted, StateUnavailable, ValueError) as exc:
            yield _sse(AgentEvent(event="error", text=str(exc)))
            return

        _record_agent_launch_receipt(
            execution,
            request,
            prompt=prompt,
            contract_path=contract_path,
            remote=bool(execution_host),
            resumed=resuming,
            continuation=execution.continuation if execution is not None else "fresh",
            extra={
                "surface": surface,
                "mode": "work",
                "capability": "work_auto",
                "network_access": True,
                "launch_kind": "resume" if resuming else "initial",
                "write_directory_count": len(write_dirs),
                "canonical_state_boundary": (
                    "prompt_only"
                    if profile.provider == "claude"
                    else "sandbox_enforced"
                ),
            },
        )
        provider_started = True
        try:
            async with aclosing(
                _stream_agent_events(
                    launcher,
                    request,
                    prompt,
                    workspace=workspace,
                    session_id=request.session_id,
                    read_dirs=read_dirs,
                    write_dirs=write_dirs,
                    execution_host=execution_host,
                    execution=execution,
                    remote_stage=remote_stage,
                    capability="work_auto",
                    outcome=outcome,
                    binary=provider_binary,
                )
            ) as stream:
                async for frame in stream:
                    yield frame
        except Exception:
            outcome.failed = True
            raise

        answer = "\n\n".join(item.strip() for item in outcome.answers if item.strip()).strip()
        if not outcome.completed:
            if outcome.failed or outcome.paused:
                return
            outcome.failed = True
            yield _sse(AgentEvent(event="error", text=f"{request.provider} produced no result."))
            return
        if not answer:
            yield _sse(
                AgentEvent(event="error", text=f"{request.provider} finished without answering.")
            )
            return

        assert artifact_scope_id is not None
        assert artifact_directory is not None
        try:
            artifacts = _discover_chat_artifacts(
                execution,
                artifact_scope_id,
                Path(str(artifact_directory)),
                remote_stage,
            )
        except Exception as exc:
            with suppress(Exception):
                _record_artifact_discovery_receipt(
                    execution,
                    attached=0,
                    candidates=0,
                    ignored={"unexpected_error": 1},
                    detail=str(exc),
                )
            artifacts = []
        yield _sse(AgentEvent(event="answer", text=answer))
        for artifact in artifacts:
            yield _sse(AgentEvent(event="artifact", artifact=artifact))

        graph_update: GraphUpdateResult
        correction_rounds = 0
        native_session_id = outcome.session_id
        try:
            patch_text = _read_chat_patch(workspace, remote_stage)
        except (OSError, StateUnavailable, ValueError) as exc:
            patch_text = None
            failure = _WorkPatchFailure(
                f"The agent wrote a patch file that could not be read: {exc}",
                correctable=False,
            )
        else:
            failure = None
        if patch_text is None and failure is None:
            graph_update = GraphUpdateResult(status="none")
        else:
            while True:
                if patch_text is not None:
                    result, failure = _apply_work_patch(
                        service,
                        execution,
                        patch_text,
                        base_revision=base_revision,
                        run_truth_scope=context.run_truth_scope,
                    )
                    if result is not None:
                        graph_update = result.model_copy(
                            update={"correction_rounds": correction_rounds}
                        )
                        break
                assert failure is not None
                if (
                    not failure.correctable
                    or correction_rounds >= _MAX_CORRECTION_ROUNDS
                    or not native_session_id
                ):
                    repairable = _work_graph_repairable(
                        execution,
                        native_session_id,
                        failure,
                    )
                    graph_update = GraphUpdateResult(
                        status="rejected",
                        change_summary=list(failure.change_summary),
                        proposal_ids=list(failure.proposal_ids),
                        validation_messages=_bounded_graph_messages(failure.message),
                        correction_rounds=correction_rounds,
                        repairable=repairable,
                    )
                    _record_work_graph_rejection(execution, graph_update)
                    break

                correction_rounds += 1
                if execution is not None:
                    execution.store.record_agent_task_receipt(
                        execution.operation_id,
                        "patch_correction_requested",
                        {"round": correction_rounds, "problem": failure.message[:400]},
                        tier="diagnostic",
                    )
                    execution.store.update_agent_task_message(
                        execution.operation_id,
                        "Correcting graph update.",
                        phase="correcting",
                        event=True,
                    )
                diagnostics_path = _stage_json_task_input(
                    local_stage,
                    remote_stage,
                    f"task-{token}-work-correction-{correction_rounds}.json",
                    {"kind": "work", "problem": failure.message},
                )
                correction_contract = PromptFactory.continuation_task_contract(
                    original_contract_path=base_contract_path,
                    mode="patch_correction",
                    patch_path=patch_path,
                    diagnostics_path=diagnostics_path,
                )
                correction_path, correction_prompt = _stage_task_contract(
                    local_stage,
                    remote_stage,
                    f"task-{token}-work-correction-{correction_rounds}.md",
                    correction_contract,
                    execution=execution,
                    role=f"work_patch_correction_{correction_rounds}",
                )
                pre_launch_digest = _existing_patch_digest(workspace, remote_stage)
                _record_agent_launch_receipt(
                    execution,
                    request,
                    prompt=correction_prompt,
                    contract_path=correction_path,
                    remote=bool(execution_host),
                    resumed=True,
                    continuation="graph_correction",
                    extra={
                        "surface": surface,
                        "mode": "work",
                        "capability": "scratch_patch",
                        "network_access": True,
                        "launch_kind": "graph_correction",
                        "correction_round": correction_rounds,
                        "write_directory_count": 0,
                    },
                )
                correction_outcome = _ProviderOutcome(session_id=native_session_id)
                correction_error: str | None = None
                async with aclosing(
                    _stream_agent_events(
                        launcher,
                        request,
                        correction_prompt,
                        workspace=workspace,
                        session_id=native_session_id,
                        read_dirs=[],
                        write_dirs=[],
                        execution_host=execution_host,
                        execution=execution,
                        remote_stage=remote_stage,
                        capability="scratch_patch",
                        outcome=correction_outcome,
                        binary=provider_binary,
                    )
                ) as stream:
                    async for frame in stream:
                        event = AgentEvent.model_validate_json(
                            frame.removeprefix("data: ").strip()
                        )
                        if event.event == "error":
                            correction_error = event.text or "Patch correction failed."
                            continue
                        yield frame
                native_session_id = correction_outcome.session_id or native_session_id
                if correction_outcome.paused:
                    return
                if correction_error or not correction_outcome.completed:
                    detail = correction_error or f"{request.provider} produced no correction result."
                    failure = _WorkPatchFailure(
                        detail,
                        correctable=True,
                        change_summary=failure.change_summary,
                        proposal_ids=failure.proposal_ids,
                    )
                    patch_text = None
                    correction_rounds = _MAX_CORRECTION_ROUNDS
                    continue
                try:
                    corrected = _read_chat_patch(workspace, remote_stage)
                except (OSError, StateUnavailable, ValueError) as exc:
                    corrected = None
                    failure = _WorkPatchFailure(
                        f"The corrected patch could not be read: {exc}",
                        correctable=True,
                        change_summary=failure.change_summary,
                        proposal_ids=failure.proposal_ids,
                    )
                if corrected is None:
                    failure = _WorkPatchFailure(
                        "The correction completed without writing patch.json.",
                        correctable=True,
                        change_summary=failure.change_summary,
                        proposal_ids=failure.proposal_ids,
                    )
                    patch_text = None
                    continue
                if (
                    pre_launch_digest is not None
                    and hashlib.sha256(corrected.encode("utf-8")).hexdigest()
                    == pre_launch_digest
                ):
                    failure = _WorkPatchFailure(
                        f"{failure.message} The correction left patch.json byte-identical; "
                        "rewrite it with the required changes.",
                        correctable=True,
                        change_summary=failure.change_summary,
                        proposal_ids=failure.proposal_ids,
                    )
                    # Revalidating it would only reproduce the original
                    # diagnostic and drop the one detail this round adds: that
                    # the agent never rewrote the file.
                    patch_text = None
                    continue
                patch_text = corrected

        try:
            _append_chat_exchange(
                service,
                request,
                answer,
                outcome.session_id,
                graph_update.applied_revision,
                graph_update=graph_update,
                execution=execution,
            )
        except (OSError, StateUnavailable, ValueError) as exc:
            if execution is not None:
                execution.store.record_agent_task_event(
                    execution.operation_id,
                    f"The reply was delivered but could not be written to the chat transcript: {exc}",
                    level="warning",
                )
        payload: dict[str, object] = {
            "graph_update": graph_update.model_dump(mode="json"),
        }
        if graph_update.applied_revision is not None:
            payload["applied_revision"] = graph_update.applied_revision
        yield _sse(
            AgentEvent(event="message", text=json.dumps(payload, separators=(",", ":")))
        )
        yield _sse(AgentEvent(event="done"))
    finally:
        retain_projection = (
            provider_started
            and not outcome.completed
            and not outcome.failed
            and _chat_native_checkpoint_available(execution, outcome.session_id)
        )
        if conversation_projection is not None and not retain_projection:
            _cleanup_chat_conversation_projection(local_stage, remote_stage, execution)


async def _stream_work_graph_repair(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    *,
    execution: AgentTaskExecution,
) -> AsyncIterator[str]:
    """Repair only a retained Work patch; never repeat the operational turn."""

    surface: AgentSurface = "project_chat" if request.chat_scope == "project" else "node_chat"
    try:
        profile = service.resolve_agent_profile(
            surface,
            provider=request.provider,
            model=request.model,
            reasoning=request.reasoning,
            run_on=request.run_on,
        )
        request = _pinned_to_profile(request, profile)
        execution_machine = service.manifest.machine_map[profile.run_on]
        execution_host = execution_machine.host
        provider_binary = execution_machine.provider_paths.get(profile.provider)
        stage_name = _chat_stage_name(service, request, execution)
        local_stage: Path | None = None
        remote_stage: RemoteRunStage | None = None
        if execution_host:
            stage_root = _validated_remote_chat_resume_stage(
                execution, execution_host, stage_name
            )
            remote_stage = RemoteRunStage(execution_host).attach(stage_root)
            workspace = Path(str(remote_stage.workspace))
            patch_path = str(remote_stage.workspace / "patch.json")
        else:
            expected_stage = _swept_stage_root(data_dir) / stage_name
            local_stage = _validated_local_chat_resume_stage(execution, expected_stage)
            workspace = local_stage
            patch_path = str(local_stage / "patch.json")
        current_revision = service.history.state().revision
        base_revision = _first_chat_base_revision(execution, current_revision)
        parent = execution.store.agent_task(execution.operation_id)
        if parent is None or parent.parent_operation_id is None:
            raise ValueError("The graph repair has no rejected Work parent.")
        rejected = execution.store.agent_task(parent.parent_operation_id)
        raw_graph_update = rejected.result.get("graph_update") if rejected and rejected.result else None
        previous = GraphUpdateResult.model_validate(raw_graph_update)
        if previous.status != "rejected":
            raise ValueError("Only a rejected Work graph update can be repaired.")
        if current_revision != base_revision:
            graph_update = GraphUpdateResult(
                status="rejected",
                change_summary=previous.change_summary,
                proposal_ids=previous.proposal_ids,
                validation_messages=[
                    f"The graph moved from revision {base_revision} to {current_revision}; "
                    "start a new Work turn to reconcile it."
                ],
            )
            _append_chat_graph_receipt(
                service,
                request,
                request.session_id,
                graph_update,
                execution,
            )
            yield _sse(
                AgentEvent(
                    event="message",
                    text=json.dumps(
                        {"graph_update": graph_update.model_dump(mode="json")},
                        separators=(",", ":"),
                    ),
                )
            )
            yield _sse(AgentEvent(event="done"))
            return
        original_contract_path = _parent_task_contract_path(
            execution, local_stage, remote_stage
        )
        token = _task_token(execution)
        diagnostics_path = _stage_json_task_input(
            local_stage,
            remote_stage,
            f"task-{token}-manual-graph-repair.json",
            {
                "kind": "work",
                "problems": previous.validation_messages,
                "prior_correction_rounds": previous.correction_rounds,
            },
        )
        contract = PromptFactory.continuation_task_contract(
            original_contract_path=original_contract_path,
            mode="patch_correction",
            patch_path=patch_path,
            diagnostics_path=diagnostics_path,
        )
        contract_path, prompt = _stage_task_contract(
            local_stage,
            remote_stage,
            f"task-{token}-manual-graph-repair.md",
            contract,
            execution=execution,
            role="work_patch_repair",
        )
        pre_launch_digest = _existing_patch_digest(workspace, remote_stage)
    except (OSError, ReplayHalted, StateUnavailable, ValueError) as exc:
        yield _sse(AgentEvent(event="error", text=str(exc)))
        return

    _record_agent_launch_receipt(
        execution,
        request,
        prompt=prompt,
        contract_path=contract_path,
        remote=bool(execution_host),
        resumed=True,
        continuation="graph_repair",
        extra={
            "surface": surface,
            "mode": "work",
            "capability": "scratch_patch",
            "network_access": True,
            "launch_kind": "graph_repair",
            "write_directory_count": 0,
        },
    )
    outcome = _ProviderOutcome(session_id=request.session_id)
    async with aclosing(
        _stream_agent_events(
            launcher,
            request,
            prompt,
            workspace=workspace,
            session_id=request.session_id,
            read_dirs=[],
            write_dirs=[],
            execution_host=execution_host,
            execution=execution,
            remote_stage=remote_stage,
            capability="scratch_patch",
            outcome=outcome,
            binary=provider_binary,
        )
    ) as stream:
        async for frame in stream:
            yield frame
    if not outcome.completed:
        if outcome.failed or outcome.paused:
            return
        yield _sse(AgentEvent(event="error", text=f"{request.provider} produced no result."))
        return
    try:
        patch_text = _read_chat_patch(workspace, remote_stage)
    except (OSError, StateUnavailable, ValueError) as exc:
        yield _sse(AgentEvent(event="error", text=f"The repaired patch could not be read: {exc}"))
        return
    if patch_text is None:
        yield _sse(AgentEvent(event="error", text="The repair did not write patch.json."))
        return
    if (
        pre_launch_digest is not None
        and hashlib.sha256(patch_text.encode("utf-8")).hexdigest() == pre_launch_digest
    ):
        yield _sse(
            AgentEvent(
                event="error",
                text="The repair left patch.json byte-identical to the rejected patch.",
            )
        )
        return
    graph_update, failure = _apply_work_patch(
        service,
        execution,
        patch_text,
        base_revision=base_revision,
        run_truth_scope=request.run_truth_scope
        or service.manifest.agent.default_run_truth_scope,
    )
    if graph_update is None:
        assert failure is not None
        graph_update = GraphUpdateResult(
            status="rejected",
            change_summary=list(failure.change_summary),
            proposal_ids=list(failure.proposal_ids),
            validation_messages=_bounded_graph_messages(failure.message),
            correction_rounds=1,
        )
        _record_work_graph_rejection(execution, graph_update)
    try:
        _append_chat_graph_receipt(
            service,
            request,
            outcome.session_id,
            graph_update,
            execution,
        )
    except (OSError, StateUnavailable, ValueError) as exc:
        execution.store.record_agent_task_event(
            execution.operation_id,
            f"The graph repair completed but its chat receipt could not be written: {exc}",
            level="warning",
        )
    payload: dict[str, object] = {
        "graph_update": graph_update.model_dump(mode="json"),
    }
    if graph_update.applied_revision is not None:
        payload["applied_revision"] = graph_update.applied_revision
    yield _sse(AgentEvent(event="message", text=json.dumps(payload, separators=(",", ":"))))
    yield _sse(AgentEvent(event="done"))


async def _stream_discuss_run(
    service: ProjectService,
    launcher: AgentLauncher,
    request: RunRequest,
    data_dir: Path,
    execution: AgentTaskExecution | None = None,
) -> AsyncIterator[str]:
    """One Discuss turn, plus the narrow legacy graph-only compatibility path.

    Deliberately not the ingest pipeline: no cursors, no evidence slices, no
    mandatory patch. New Discuss turns have neither a patch contract nor graph
    authority. A legacy request may retain its old optional graph-only authority,
    but never gains Work repository writes.
    """
    resuming = bool(execution is not None and execution.reuses_native_checkpoint)
    surface: AgentSurface = "project_chat" if request.chat_scope == "project" else "node_chat"
    try:
        profile = service.resolve_agent_profile(
            surface,
            provider=request.provider,
            model=request.model,
            reasoning=request.reasoning,
            run_on=request.run_on,
        )
    except ValueError as exc:
        yield _sse(AgentEvent(event="error", text=str(exc)))
        return
    request = _pinned_to_profile(request, profile)
    local_stage: Path | None = None
    execution_machine = service.manifest.machine_map[profile.run_on]
    execution_host = execution_machine.host
    provider_binary = execution_machine.provider_paths.get(profile.provider)
    remote_stage: RemoteRunStage | None = None
    conversation_projection: Path | PurePosixPath | None = None
    artifact_scope_id: str | None = None
    artifact_directory: Path | PurePosixPath | None = None
    provider_started = False
    outcome = _ProviderOutcome(session_id=request.session_id)
    try:
        try:
            context = service.assemble_chat(request)
            # A resumed turn re-assembles its context, so `graph_revision` is now
            # whatever the graph has moved to — but the agent is still finishing
            # reasoning it began at the original revision. That first revision is
            # the one its patch must be judged against, and the context receipt
            # from the interrupted attempt is where it survives.
            base_revision = context.graph_revision
            if resuming:
                base_revision = _first_chat_base_revision(execution, base_revision)
            _record_chat_context_receipt(execution, context, surface=surface)
            if request.session_id and not resuming and not _known_chat_session(service, request):
                raise ValueError(
                    "That native session was not created by this chat. Start a new chat instead."
                )
            # One scratch folder per conversation, not per turn. Resuming a native
            # session means resuming it in the directory it was given — Claude keys
            # its sessions by that directory — so every turn of a chat, local or
            # remote, reuses the same folder and _sweep_stale_stages ages it out.
            stage_name = _chat_stage_name(service, request, execution)
            if execution_host:
                if resuming:
                    stage_root = _validated_remote_chat_resume_stage(
                        execution, execution_host, stage_name
                    )
                    remote_stage = RemoteRunStage(execution_host).attach(stage_root)
                else:
                    remote_stage = RemoteRunStage(execution_host).open(stage_name, reuse=True)
                assert remote_stage.root is not None
                if execution is not None:
                    execution.checkpoint_stage(execution_host, str(remote_stage.root))
                if not resuming:
                    context = context.model_copy(
                        update=_stage_context_paths(
                            context, service, remote_stage, execution_machine.alias
                        )
                    )
                workspace = Path(str(remote_stage.workspace))
            else:
                stage_root = _swept_stage_root(data_dir)
                expected_stage = stage_root / stage_name
                if resuming:
                    local_stage = _validated_local_chat_resume_stage(
                        execution, expected_stage
                    )
                else:
                    local_stage = expected_stage
                    local_stage.mkdir(parents=True, exist_ok=True)
                if execution is not None:
                    execution.checkpoint_stage("", str(local_stage))
                workspace = local_stage
            if not resuming:
                # A reused folder must not hand this turn the previous turn's patch.
                _clear_stale_patch(workspace, remote_stage)
            artifact_scope_id = (
                _logical_chat_turn_operation_id(
                    execution.store, execution.operation_id
                )
                if execution is not None and resuming
                else execution.operation_id
                if execution is not None
                else str(uuid.uuid4())
            )
            if remote_stage is not None:
                artifact_directory = remote_stage.prepare_artifact_directory(
                    artifact_scope_id, reuse=resuming
                )
            else:
                assert local_stage is not None
                artifact_directory = _prepare_local_artifact_directory(
                    local_stage, artifact_scope_id, reuse=resuming
                )

            if resuming:
                conversation_projection = _saved_chat_conversation_projection(
                    local_stage, remote_stage
                )
                context = _rebind_chat_conversations(
                    context,
                    conversation_projection,
                    verify_local=remote_stage is None,
                )
            else:
                context, conversation_projection = _project_chat_conversations(
                    context, local_stage, remote_stage
                )

            read_dirs = _chat_read_dirs(
                context,
                remote_stage,
                service,
                execution_machine.alias,
                conversation_projection,
            )
            token = _task_token(execution)
            if resuming:
                if not request.session_id:
                    raise ValueError(
                        "The interrupted chat has no native agent session; retry it instead."
                    )
                assert execution is not None
                original_contract_path = _parent_task_contract_path(
                    execution, local_stage, remote_stage
                )
                contract = PromptFactory.continuation_task_contract(
                    original_contract_path=original_contract_path,
                    mode="resume",
                    patch_path=None,
                )
                contract_path, prompt = _stage_task_contract(
                    local_stage,
                    remote_stage,
                    f"task-{token}-resume.md",
                    contract,
                )
            else:
                assert request.message is not None
                human_request_path = _stage_task_input(
                    local_stage,
                    remote_stage,
                    f"task-{token}-human-request.txt",
                    request.message,
                )
                retry_diagnostics_path = (
                    _stage_json_task_input(
                        local_stage,
                        remote_stage,
                        f"task-{token}-retry-diagnostics.json",
                        {"prior_attempt_diagnostics": list(execution.retry_feedback)},
                    )
                    if execution is not None and execution.retry_feedback
                    else None
                )
                contract = PromptFactory.discuss_task_contract(
                    project_name=context.project_name,
                    ontology_path=f"{context.graph_path}#ontology",
                    graph_path=context.graph_path,
                    research_path=context.research_md_path,
                    focused_node_id=str(context.node["id"]) if context.node else None,
                    conversation_roots=_chat_conversation_roots(context),
                    conversations_unreachable=context.conversations_unreachable,
                    repositories=[
                        {"alias": item.alias, "host": item.host, "path": item.path}
                        for item in context.repositories
                    ],
                    introduction_path=context.introduction_path,
                    human_request_path=human_request_path,
                    artifact_path=str(artifact_directory),
                    retry_diagnostics_path=retry_diagnostics_path,
                )
                contract_path, prompt = _stage_task_contract(
                    local_stage,
                    remote_stage,
                    f"task-{token}-initial.md",
                    contract,
                )
        except (OSError, ReplayHalted, StateUnavailable, ValueError) as exc:
            yield _sse(AgentEvent(event="error", text=str(exc)))
            return

        _record_agent_launch_receipt(
            execution,
            request,
            prompt=prompt,
            contract_path=contract_path,
            remote=bool(execution_host),
            resumed=resuming,
            continuation=execution.continuation if execution is not None else "fresh",
            extra={
                "surface": surface,
                "mode": "discuss",
                "capability": "discuss",
                "network_access": True,
                "launch_kind": "resume" if resuming else "initial",
                "write_directory_count": 0,
            },
        )
        provider_started = True
        try:
            async with aclosing(
                _stream_agent_events(
                    launcher,
                    request,
                    prompt,
                    workspace=workspace,
                    session_id=request.session_id,
                    read_dirs=read_dirs,
                    write_dirs=[],
                    execution_host=execution_host,
                    execution=execution,
                    remote_stage=remote_stage,
                    capability="discuss",
                    outcome=outcome,
                    binary=provider_binary,
                )
            ) as stream:
                async for frame in stream:
                    yield frame
        except Exception:
            # Provider launch/runtime exceptions are terminal and Background will
            # offer Retry, which re-projects. Cancellation and process shutdown use
            # BaseException paths and retain the projection for a possible Resume.
            outcome.failed = True
            raise

        # Only a labelled final assistant message is the reply. A provider that
        # emitted none has not answered, and promoting its last trace would show
        # reasoning or tool output to the human as if it were the answer.
        answer = "\n\n".join(item.strip() for item in outcome.answers if item.strip()).strip()
        if not outcome.completed:
            if outcome.failed or outcome.paused:
                return
            outcome.failed = True
            yield _sse(AgentEvent(event="error", text=f"{request.provider} produced no result."))
            return
        if not answer:
            yield _sse(
                AgentEvent(
                    event="error",
                    text=f"{request.provider} finished without answering.",
                )
            )
            return

        assert artifact_scope_id is not None
        assert artifact_directory is not None
        try:
            artifacts = _discover_chat_artifacts(
                execution,
                artifact_scope_id,
                Path(str(artifact_directory)),
                remote_stage,
            )
        except Exception as exc:
            # Preview attachments are optional. Even a programming or storage
            # error in this branch must not take down a labelled chat answer.
            with suppress(Exception):
                _record_artifact_discovery_receipt(
                    execution,
                    attached=0,
                    candidates=0,
                    ignored={"unexpected_error": 1},
                    detail=str(exc),
                )
            artifacts = []
        yield _sse(AgentEvent(event="answer", text=answer))
        for artifact in artifacts:
            yield _sse(AgentEvent(event="artifact", artifact=artifact))

        # Authority to change the graph rides on the human's request. An agent
        # cannot grant it to itself by writing the file, so a stray patch is kept
        # as a receipt and discarded.
        if execution is not None:
            try:
                patch_text = _read_chat_patch(workspace, remote_stage)
            except (OSError, StateUnavailable, ValueError) as exc:
                execution.store.record_agent_task_receipt(
                    execution.operation_id,
                    "discuss_patch_discarded",
                    {
                        "reason": "unreadable",
                        "detail": f"The agent wrote a patch file that could not be read: {exc}"[
                            :400
                        ],
                    },
                    tier="diagnostic",
                )
                execution.store.record_agent_task_event(
                    execution.operation_id,
                    "Discuss wrote an unreadable patch.json; RCP discarded it without "
                    "changing the graph.",
                    level="warning",
                )
            else:
                if patch_text is not None:
                    execution.store.record_agent_task_patch_output(
                        execution.operation_id, patch_text
                    )
                    execution.store.record_agent_task_event(
                        execution.operation_id,
                        "Discuss has no graph authority, so the patch the agent wrote was "
                        "discarded. Switch to Work for a deliberate graph update.",
                        level="warning",
                    )
                    execution.store.record_agent_task_receipt(
                        execution.operation_id,
                        "discuss_patch_discarded",
                        {
                            "reason": "no_graph_authority",
                            "byte_length": len(patch_text.encode("utf-8")),
                        },
                        tier="diagnostic",
                    )

        try:
            _append_chat_exchange(
                service,
                request,
                answer,
                outcome.session_id,
                None,
                execution=execution,
            )
        except (OSError, StateUnavailable, ValueError) as exc:
            if execution is not None:
                execution.store.record_agent_task_event(
                    execution.operation_id,
                    f"The reply was delivered but could not be written to the chat "
                    f"transcript: {exc}",
                    level="warning",
                )
        yield _sse(AgentEvent(event="done"))
    finally:
        # Keep exact transcript copies only when this attempt can genuinely
        # Resume. A pause before the provider establishes any native checkpoint
        # is Retry-only, so retaining its potentially large projection just leaks
        # storage. The reusable native-session cwd itself always stays put.
        retain_projection = (
            provider_started
            and not outcome.completed
            and not outcome.failed
            and _chat_native_checkpoint_available(execution, outcome.session_id)
        )
        if conversation_projection is not None and not retain_projection:
            _cleanup_chat_conversation_projection(local_stage, remote_stage, execution)


def _clear_stale_patch(workspace: Path, remote_stage: RemoteRunStage | None) -> None:
    """Drop a previous turn's `patch.json` from a conversation's scratch folder.

    Fails the turn if it cannot: a scratch folder is reused across a conversation,
    so a survivor would be read as this turn's patch and applied under this turn's
    authorization.
    """
    if remote_stage is not None:
        remote_stage.remove_workspace_file("patch.json")
        return
    (workspace / "patch.json").unlink(missing_ok=True)


def _prepare_local_artifact_directory(
    stage: Path,
    scope_id: str,
    *,
    reuse: bool,
) -> Path:
    """Create an empty exact output boundary, or require it for Resume."""
    if _safe_stage_name(scope_id) != scope_id:
        raise ValueError("artifact scope contains unsupported characters")
    turns = stage / "turns"
    if os.path.lexists(turns) and (turns.is_symlink() or not turns.is_dir()):
        raise ValueError("artifact parent is unsafe")
    turns.mkdir(mode=0o700, exist_ok=True)
    scope = turns / scope_id
    target = scope / "artifacts"
    if reuse:
        if (
            scope.is_symlink()
            or not scope.is_dir()
            or target.is_symlink()
            or not target.is_dir()
        ):
            raise ValueError(
                "The saved artifact directory is unavailable; retry this chat turn instead."
            )
        return target
    _remove_local_tree(scope, turns)
    target.mkdir(parents=True, mode=0o700)
    return target


def _discover_chat_artifacts(
    execution: AgentTaskExecution | None,
    scope_id: str,
    directory: Path,
    remote_stage: RemoteRunStage | None,
) -> list[AgentArtifactDescriptor]:
    """Discover bounded attachments without making their validity part of chat success."""
    ignored: dict[str, int] = {}

    def ignore(reason: str) -> None:
        ignored[reason] = ignored.get(reason, 0) + 1

    try:
        candidates = (
            remote_stage.list_artifact_files(scope_id)
            if remote_stage is not None
            else list_local_regular_files(directory)
        )
    except (OSError, StateUnavailable, ValueError) as exc:
        _record_artifact_discovery_receipt(
            execution,
            attached=0,
            candidates=0,
            ignored={"discovery_unavailable": 1},
            detail=str(exc),
        )
        return []

    attached: list[AgentArtifactDescriptor] = []
    total_bytes = 0
    allowed_candidates = 0
    for name, advertised_size in sorted(candidates):
        if Path(name).suffix.casefold() not in ARTIFACT_MEDIA_TYPES:
            ignore("unsupported_type")
            continue
        if allowed_candidates >= CHAT_ARTIFACT_MAX_COUNT:
            ignore("count_limit")
            continue
        allowed_candidates += 1
        if advertised_size < 0 or advertised_size > CHAT_ARTIFACT_MAX_FILE_BYTES:
            ignore("file_size_limit")
            continue
        if total_bytes + advertised_size > CHAT_ARTIFACT_MAX_TOTAL_BYTES:
            ignore("total_size_limit")
            continue
        try:
            data = (
                remote_stage.read_artifact_bytes(
                    scope_id, name, max_bytes=CHAT_ARTIFACT_MAX_FILE_BYTES
                )
                if remote_stage is not None
                else read_local_regular_file(
                    directory, name, max_bytes=CHAT_ARTIFACT_MAX_FILE_BYTES
                )
            )
            if total_bytes + len(data) > CHAT_ARTIFACT_MAX_TOTAL_BYTES:
                ignore("total_size_limit")
                continue
            media_type = validate_artifact_bytes(name, data)
            descriptor = descriptor_for(scope_id, name)
            if descriptor.media_type != media_type:
                raise ValueError("artifact media type mismatch")
        except (FileNotFoundError, OSError, StateUnavailable, ValueError):
            ignore("invalid_or_unavailable")
            continue
        attached.append(descriptor)
        total_bytes += len(data)
    _record_artifact_discovery_receipt(
        execution,
        attached=len(attached),
        candidates=len(candidates),
        ignored=ignored,
    )
    return attached


def _record_artifact_discovery_receipt(
    execution: AgentTaskExecution | None,
    *,
    attached: int,
    candidates: int,
    ignored: dict[str, int],
    detail: str | None = None,
) -> None:
    if execution is None:
        return
    payload: dict[str, object] = {
        "candidate_count": candidates,
        "attached_count": attached,
        "ignored": ignored,
    }
    if detail:
        payload["detail"] = " ".join(detail.split())[:400]
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "artifact_discovery",
        payload,
        tier="diagnostic",
    )


def _read_chat_patch(workspace: Path, remote_stage: RemoteRunStage | None) -> str | None:
    """Read `patch.json` if the agent wrote one. Absence is the normal case.

    Unlike an ingest run, chat does not hunt the scratch folder for a stray JSON
    file — with no patch expected, that search would misread scratch work as a
    graph change. A file that exists but cannot be read raises: a written patch
    that silently reads as "no patch" is the one outcome nobody can see.
    """
    if remote_stage is not None:
        if "patch.json" not in remote_stage.list_workspace_files():
            return None
        return remote_stage.read_text(remote_stage.workspace / "patch.json")
    path = workspace / "patch.json"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def _apply_work_patch(
    service: ProjectService,
    execution: AgentTaskExecution | None,
    patch_text: str,
    *,
    base_revision: int,
    run_truth_scope: list[str],
) -> tuple[GraphUpdateResult | None, _WorkPatchFailure | None]:
    """Validate and atomically apply one Work patch candidate."""

    if execution is not None:
        execution.store.record_agent_task_patch_output(execution.operation_id, patch_text)
        execution.store.record_agent_task_receipt(
            execution.operation_id,
            "patch_retained",
            {"byte_length": len(patch_text.encode("utf-8")), "file_name": "patch.json"},
            tier="diagnostic",
        )
    change_summary: tuple[str, ...] = ()
    proposal_ids: tuple[str, ...] = ()
    try:
        patch, _ = service.parse_patch_output([patch_text])
        change_summary = tuple(patch.change_summary)
        proposal_ids = tuple(_work_patch_proposal_ids(patch))
        _record_patch_receipt(
            execution,
            patch,
            byte_length=len(patch_text.encode("utf-8")),
        )
        _require_agent_patch_identity(patch, "work")
        patch = normalize_agent_patch_bookkeeping(patch)
        validate_agent_patch_shape(patch)
        validate_work_patch(patch)
        if sorted(patch.run_truth_scope) != sorted(run_truth_scope):
            raise ValueError(
                "A Work patch must declare the run truth scope it was given "
                f"({sorted(run_truth_scope)}), not {sorted(patch.run_truth_scope)}."
            )
        if not patch.ops:
            return GraphUpdateResult(status="none"), None
        with service.history.workspace.run_lock():
            appended, result = service.history.append(
                patch,
                discard_on_reject=True,
                expected_revision=base_revision,
            )
    except RevisionConflict as exc:
        return None, _WorkPatchFailure(
            str(exc),
            correctable=False,
            change_summary=change_summary,
            proposal_ids=proposal_ids,
        )
    except PatchRejected as exc:
        messages = [item.message for item in exc.report.messages if item.level == "reject"]
        detail = "; ".join(messages) or str(exc) or "The graph rejected the Work patch."
        if execution is not None:
            execution.store.record_agent_task_receipt(
                execution.operation_id,
                "patch_rejected",
                {
                    "messages": [
                        item.model_dump(mode="json") for item in exc.report.messages[:16]
                    ]
                },
                tier="diagnostic",
            )
        return None, _WorkPatchFailure(
            detail,
            correctable=True,
            change_summary=change_summary,
            proposal_ids=proposal_ids,
        )
    except (ReplayHalted, StateUnavailable) as exc:
        return None, _WorkPatchFailure(
            str(exc),
            correctable=False,
            change_summary=change_summary,
            proposal_ids=proposal_ids,
        )
    except ValueError as exc:
        return None, _WorkPatchFailure(
            str(exc),
            correctable=True,
            change_summary=change_summary,
            proposal_ids=proposal_ids,
        )

    report = result.reports[appended.revision]
    _record_patch_applied_receipt(execution, result.state)
    return (
        GraphUpdateResult(
            status="applied",
            applied_revision=result.state.revision,
            change_summary=list(change_summary),
            proposal_ids=list(proposal_ids),
            validation_messages=_bounded_graph_messages(
                *(item.message for item in report.flags)
            ),
        ),
        None,
    )


def _work_patch_proposal_ids(patch: Patch) -> list[str]:
    proposal_ids: list[str] = []
    for operation in patch.ops:
        if operation.get("op") != "create_proposals":
            continue
        proposals = operation.get("proposals")
        if not isinstance(proposals, list):
            continue
        for proposal in proposals:
            if isinstance(proposal, dict) and isinstance(proposal.get("id"), str):
                proposal_ids.append(proposal["id"])
    return list(dict.fromkeys(proposal_ids))


def _bounded_graph_messages(*messages: str) -> list[str]:
    bounded: list[str] = []
    for raw in messages:
        detail = " ".join(raw.split())[:1600]
        if detail and detail not in bounded:
            bounded.append(detail)
        if len(bounded) == 8:
            break
    return bounded


def _work_graph_repairable(
    execution: AgentTaskExecution | None,
    native_session_id: str | None,
    failure: _WorkPatchFailure,
) -> bool:
    return bool(
        failure.correctable
        and native_session_id
        and execution is not None
        and execution.stage_root
    )


def _record_work_graph_rejection(
    execution: AgentTaskExecution | None,
    graph_update: GraphUpdateResult,
) -> None:
    if execution is None:
        return
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "work_graph_update_rejected",
        graph_update.model_dump(mode="json"),
    )
    detail = (
        graph_update.validation_messages[0]
        if graph_update.validation_messages
        else "The graph update was rejected."
    )
    execution.store.record_agent_task_event(
        execution.operation_id,
        f"Operational work completed, but the graph update was rejected: {detail}",
        level="warning",
    )


async def _stream_coach(
    service: ProjectService,
    launcher: AgentLauncher,
    paper: PaperService,
    request: CoachRequest,
    data_dir: Path,
    execution: AgentTaskExecution | None = None,
) -> AsyncIterator[str]:
    existing = None
    if request.session_id:
        existing = next(
            (item for item in paper.sessions() if item.native_session_id == request.session_id),
            None,
        )
    try:
        if not (
            execution is not None
            and execution.reuses_native_checkpoint
            and existing is None
        ):
            request = _resolved_coach_request(service, request)
        profile = service.resolve_agent_profile(
            "paper_coach",
            provider=request.provider,
            model=request.model,
            reasoning=request.reasoning,
            run_on=request.run_on,
        )
    except ValueError as exc:
        yield _sse(AgentEvent(event="error", text=str(exc)))
        return
    request = _pinned_to_profile(request, profile)
    execution_machine = service.manifest.machine_map[profile.run_on]
    execution_host = execution_machine.host
    provider_binary = execution_machine.provider_paths.get(profile.provider)
    if execution_host:
        yield _sse(
            AgentEvent(
                event="error",
                text=(
                    "Remote writing-coach sessions need persistent read-only staging for native "
                    "resume. Choose a local machine for this invocation."
                ),
            )
        )
        return
    stage_root = _swept_stage_root(data_dir)
    if execution is not None and execution.reuses_native_checkpoint:
        if not execution.stage_root:
            yield _sse(
                AgentEvent(
                    event="error",
                    text="The interrupted paper-coach task has no staging checkpoint; retry it.",
                )
            )
            return
        local_stage = Path(execution.stage_root).resolve()
        if local_stage.parent != stage_root.resolve() or not local_stage.is_dir():
            yield _sse(
                AgentEvent(
                    event="error",
                    text="The interrupted paper-coach staging checkpoint is unavailable; retry it.",
                )
            )
            return
    else:
        token = _task_token(execution)
        local_stage = stage_root / f"paper-{token}"
        local_stage.mkdir(parents=True, exist_ok=False)
        if execution is not None:
            execution.checkpoint_stage("", str(local_stage))
    snapshot = paper.snapshot()
    draft_override = None
    if snapshot.sync_state in {"unsynced", "conflict"}:
        draft_override = Path(
            _stage_task_input(
                local_stage,
                None,
                f"task-{_task_token(execution)}-paper-draft.md",
                snapshot.content,
            )
        )
    try:
        if execution is not None and execution.reuses_native_checkpoint:
            original_contract_path = _parent_task_contract_path(execution, local_stage, None)
            contract = PromptFactory.continuation_task_contract(
                original_contract_path=original_contract_path,
                mode="resume",
            )
            contract_path, prompt = _stage_task_contract(
                local_stage,
                None,
                f"task-{_task_token(execution)}-resume.md",
                contract,
            )
            read_dirs = [service.manifest.research_dir, local_stage / "inputs"]
        else:
            pointers, read_dirs = service.coach_context(request, draft_override)
            token = _task_token(execution)
            human_request_path = _stage_task_input(
                local_stage,
                None,
                f"task-{token}-human-request.txt",
                request.message,
            )
            retry_diagnostics_path = (
                _stage_json_task_input(
                    local_stage,
                    None,
                    f"task-{token}-retry-diagnostics.json",
                    {"prior_attempt_diagnostics": list(execution.retry_feedback)},
                )
                if execution is not None and execution.retry_feedback
                else None
            )
            raw_repositories = pointers["truth_repositories"]
            assert isinstance(raw_repositories, list)
            contract = PromptFactory.paper_coach_task_contract(
                introduction_path=str(pointers["introduction"]),
                graph_path=str(pointers["graph"]),
                research_path=str(pointers["research_md"]),
                repositories=[
                    {
                        "alias": str(item["alias"]),
                        "host": str(item["host"]),
                        "path": str(item["path"]),
                    }
                    for item in raw_repositories
                    if isinstance(item, dict)
                ],
                human_request_path=human_request_path,
                retry_diagnostics_path=retry_diagnostics_path,
            )
            contract_path, prompt = _stage_task_contract(
                local_stage,
                None,
                f"task-{token}-initial.md",
                contract,
            )
            read_dirs.extend([service.manifest.research_dir, local_stage / "inputs"])
    except (StateUnavailable, ValueError) as exc:
        yield _sse(AgentEvent(event="error", text=str(exc)))
        return
    model = (
        (None if existing.model == "provider-default" else existing.model)
        if existing
        else request.model
    )
    reasoning = existing.reasoning if existing else request.reasoning
    native_session_id = request.session_id
    completed = False
    provider_outcome = _ProviderOutcome(session_id=native_session_id)
    _record_agent_launch_receipt(
        execution,
        request,
        prompt=prompt,
        contract_path=contract_path,
        remote=False,
        resumed=bool(execution is not None and execution.reuses_native_checkpoint),
        continuation=execution.continuation if execution is not None else "fresh",
        extra={
            "surface": "paper_coach",
            "capability": "paper_readonly",
            "network_access": False,
            "launch_kind": (
                "resume"
                if execution is not None and execution.reuses_native_checkpoint
                else "initial"
            ),
        },
    )
    async with aclosing(
        launcher.stream(
            request.provider,
            prompt,
            cwd=service.manifest.research_dir,
            model=model,
            reasoning=reasoning,
            session_id=request.session_id,
            read_dirs=read_dirs,
            capability="paper_readonly",
            control=execution.control if execution is not None else None,
            binary=provider_binary,
        )
    ) as stream:
        async for event in stream:
            if event.event == "provider_exit":
                try:
                    evidence = json.loads(event.text)
                except (json.JSONDecodeError, TypeError, ValueError):
                    evidence = {"unparsed": event.text[:400]}
                provider_outcome.exit_evidence = (
                    evidence
                    if isinstance(evidence, dict)
                    else {"unparsed": event.text[:400]}
                )
                _record_provider_exit(
                    execution,
                    provider_outcome,
                    workspace=local_stage,
                    remote_stage=None,
                )
                continue
            if event.event == "session" and event.session_id:
                native_session_id = event.session_id
            if event.event == "done":
                completed = True
            yield _sse(event)
    if completed and native_session_id:
        intro_hash, graph_revision, research_hash = service.pointer_hashes()
        now = datetime.now(UTC)
        paper.record_session(
            WritingSession(
                provider=request.provider,
                native_session_id=native_session_id,
                execution_machine=profile.run_on,
                project_id=paper.project_id,
                title=existing.title if existing else request.message[:72],
                model=model or "provider-default",
                reasoning=reasoning,
                created_at=existing.created_at if existing else now,
                last_resumed_at=now,
                introduction_hash_examined=intro_hash,
                graph_revision_examined=graph_revision,
                research_md_hash_examined=research_hash,
            )
        )


def _record_context_receipt(
    execution: AgentTaskExecution | None,
    context: RunContext,
    *,
    surface: AgentSurface,
) -> None:
    if execution is None:
        return
    slice_hashes = {
        session.slice_sha256 for session in context.sessions if session.slice_sha256
    }
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "context_assembled",
        {
            "surface": surface,
            "repository_count": len(context.repositories),
            "session_count": len(context.sessions),
            "session_record_count": sum(
                session.slice_record_count for session in context.sessions
            ),
            "unique_slice_count": len(slice_hashes),
            "source_error_count": len(context.source_errors),
            "graph_revision": context.graph_revision,
        },
    )


def _record_chat_context_receipt(
    execution: AgentTaskExecution | None,
    context: ChatContext,
    *,
    surface: AgentSurface,
) -> None:
    if execution is None:
        return
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "chat_context_assembled",
        {
            "surface": surface,
            "repository_count": len(context.repositories),
            "relation_count": len(context.relations),
            "conversation_count": len(context.conversations),
            "conversations_truncated": context.conversations_truncated,
            "graph_revision": context.graph_revision,
            "node_id": context.node["id"] if context.node else None,
        },
    )


def _first_chat_base_revision(execution: AgentTaskExecution | None, fallback: int) -> int:
    """The graph revision the reasoning being resumed was actually written against.

    A resume is a *new* task whose parent holds the original attempt, so the
    revision has to be followed up the chain — reading only this attempt's
    receipts would find the revision it just re-assembled and wave the stale
    patch through. The walk stops at the first ancestor that was not itself a
    resume, because a retry starts fresh reasoning at its own revision.
    """
    if execution is None:
        return fallback
    store = execution.store
    operation_id: str | None = execution.operation_id
    seen: set[str] = set()
    first = True
    lineage_project: str | None = None
    lineage_kind: AgentTaskKind | None = None
    expected_attempt: int | None = None
    while operation_id is not None:
        if operation_id in seen:
            raise _resume_lineage_error("the task ancestry contains a cycle")
        seen.add(operation_id)
        record = store.agent_task(operation_id)
        if record is None:
            raise _resume_lineage_error(f"task {operation_id!r} is missing")
        if first:
            lineage_project = record.project_id
            lineage_kind = record.kind
        elif record.project_id != lineage_project or record.kind != lineage_kind:
            raise _resume_lineage_error(
                f"task {operation_id!r} crosses a project or task-kind boundary"
            )
        if expected_attempt is not None and record.attempt != expected_attempt:
            raise _resume_lineage_error(
                f"task {operation_id!r} has inconsistent attempt ancestry"
            )
        receipts = store.agent_task_receipts(operation_id)
        resumed = _attempt_was_resumed(receipts, record)
        if first and not resumed:
            raise _resume_lineage_error("the current task is not marked as a Resume")
        if not resumed:
            return _assembled_graph_revision(receipts, operation_id)
        assert record.parent_operation_id is not None
        expected_attempt = record.attempt - 1
        if expected_attempt < 1:
            raise _resume_lineage_error(
                f"task {record.operation_id!r} has an invalid attempt number"
            )
        operation_id = record.parent_operation_id
        first = False
    raise _resume_lineage_error("the task ancestry ended without an original attempt")


def _logical_chat_turn_operation_id(store: AppStore, operation_id: str) -> str:
    """Resume shares its original turn directory; Retry begins a fresh one."""
    seen: set[str] = set()
    current_id = operation_id
    project_id: str | None = None
    kind: AgentTaskKind | None = None
    while current_id not in seen:
        seen.add(current_id)
        record = store.agent_task(current_id)
        if record is None:
            raise ValueError("chat task provenance is missing")
        if project_id is None:
            project_id = record.project_id
            kind = record.kind
        elif record.project_id != project_id or record.kind != kind:
            raise ValueError("chat task provenance crosses a task boundary")
        resumed = _attempt_was_resumed(
            store.agent_task_receipts(current_id), record
        )
        if not resumed:
            return current_id
        if record.parent_operation_id is None:
            raise ValueError("resumed chat task has no parent")
        current_id = record.parent_operation_id
    raise ValueError("chat task provenance contains a cycle")


def _resume_lineage_error(detail: str) -> ValueError:
    return ValueError(
        "Cannot safely resume this chat because "
        f"{detail}. Retry the turn from the beginning instead."
    )


def _attempt_was_resumed(
    receipts: list[AgentTaskReceiptRecord], record: AgentTaskRecord
) -> bool:
    created = [receipt for receipt in receipts if receipt.category == "operation_created"]
    if len(created) != 1:
        raise _resume_lineage_error(
            f"task {record.operation_id!r} has no unique operation-created receipt"
        )
    payload = created[0].payload
    resumed = payload.get("resumed")
    has_parent = payload.get("has_parent")
    attempt = payload.get("attempt")
    if (
        not isinstance(resumed, bool)
        or not isinstance(has_parent, bool)
        or isinstance(attempt, bool)
        or not isinstance(attempt, int)
        or attempt != record.attempt
        or payload.get("kind") != record.kind
        or record.kind not in {"node_chat", "project_chat"}
    ):
        raise _resume_lineage_error(
            f"task {record.operation_id!r} has invalid operation-created provenance"
        )
    actual_has_parent = record.parent_operation_id is not None
    if has_parent != actual_has_parent or (resumed and not actual_has_parent):
        raise _resume_lineage_error(
            f"task {record.operation_id!r} has inconsistent parent provenance"
        )
    return resumed


def _assembled_graph_revision(
    receipts: list[AgentTaskReceiptRecord], operation_id: str
) -> int:
    assembled = [
        receipt for receipt in receipts if receipt.category == "chat_context_assembled"
    ]
    if not assembled:
        raise _resume_lineage_error(
            f"the original attempt {operation_id!r} has no assembled chat context"
        )
    revision = assembled[0].payload.get("graph_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise _resume_lineage_error(
            f"the original attempt {operation_id!r} has an invalid graph revision"
        )
    return revision


def _report_source_errors(
    execution: AgentTaskExecution | None,
    source_errors: list[str],
) -> None:
    """Raise degraded sources as run warnings so a dropped session is never silent."""

    if execution is None:
        return
    for detail in source_errors[:16]:
        execution.store.record_agent_task_event(
            execution.operation_id,
            f"Conversation source unavailable and excluded from this run: {detail}",
            level="warning",
        )


def _record_patch_receipt(
    execution: AgentTaskExecution | None,
    patch: Patch,
    *,
    byte_length: int,
) -> None:
    if execution is None:
        return
    known_operations = {
        "create_nodes",
        "update_nodes",
        "create_edges",
        "remove_edges",
        "supersede_nodes",
        "merge_nodes",
        "create_ambiguities",
        "resolve_ambiguities",
        "upsert_glossary",
        "set_coverage",
        "set_project_truth_scope",
        "create_proposals",
        "resolve_proposals",
    }
    operation_counts: dict[str, int] = {}
    created_node_count = 0
    created_edge_count = 0
    for operation in patch.ops:
        raw_kind = operation.get("op")
        operation_kind = (
            raw_kind
            if isinstance(raw_kind, str) and raw_kind in known_operations
            else "unknown"
        )
        operation_counts[operation_kind] = operation_counts.get(operation_kind, 0) + 1
        if operation_kind == "create_nodes" and isinstance(operation.get("nodes"), list):
            created_node_count += len(operation["nodes"])
        if operation_kind == "create_edges" and isinstance(operation.get("edges"), list):
            created_edge_count += len(operation["edges"])
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "patch_parsed",
        {
            "byte_length": byte_length,
            "kind": patch.kind,
            "author": patch.author,
            "operation_count": len(patch.ops),
            "operation_counts": operation_counts,
            "created_node_count": created_node_count,
            "created_edge_count": created_edge_count,
            "processed_cursor_count": len(patch.processed_cursors),
            "change_summary_count": len(patch.change_summary),
        },
        tier="diagnostic",
    )


def _record_patch_applied_receipt(
    execution: AgentTaskExecution | None,
    state: GraphState,
) -> None:
    if execution is None:
        return
    execution.store.record_agent_task_receipt(
        execution.operation_id,
        "patch_applied",
        {
            "revision": state.revision,
            "node_count": len(state.nodes),
            "edge_count": len(state.edges),
            "validation_message_count": len(state.validation_messages),
        },
    )


_STATE_PATH_FIELDS = (
    "graph_path",
    "research_md_path",
    "introduction_path",
    "glossary_path",
    "coverage_path",
)


def _safe_stage_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    if not name:
        raise ValueError("run stage name is empty")
    return name


def _chat_stage_name(
    service: ProjectService,
    request: RunRequest,
    execution: AgentTaskExecution | None,
) -> str:
    """Name a reusable chat workspace inside one stable project boundary."""
    if not request.chat_id:
        raise ValueError("Chat requires a chat_id")
    if execution is not None:
        task = execution.store.agent_task(execution.operation_id)
        if task is None or not task.project_id:
            raise ValueError(
                "Cannot identify this chat's project workspace; retry the turn from the "
                "beginning."
            )
        project_identity = f"task-project\0{task.project_id}"
    else:
        # Direct streams have no catalog task record. The canonical workspace
        # location is the stable project identity available at this boundary.
        project_identity = f"canonical-workspace\0{service.history.workspace.location}"
    project_key = hashlib.sha256(project_identity.encode()).hexdigest()[:16]
    return _safe_stage_name(f"chat-{project_key}-{request.chat_id}")


def _validated_remote_chat_resume_stage(
    execution: AgentTaskExecution | None,
    execution_host: str,
    stage_name: str,
) -> str:
    if execution is None or not execution.stage_root:
        raise ValueError(
            "Cannot safely resume this chat because its saved stage is missing. "
            "Retry the turn from the beginning."
        )
    if (execution.stage_host or "") != execution_host:
        raise ValueError(
            "Cannot safely resume this chat because its saved stage host does not match "
            "the execution machine. Retry the turn from the beginning."
        )
    expected = str(PurePosixPath("/tmp") / f"rcp-run.{stage_name}")
    if execution.stage_root != expected:
        raise ValueError(
            "Cannot safely resume this chat because its saved stage belongs to a different "
            "project or conversation. Retry the turn from the beginning."
        )
    return execution.stage_root


def _validated_local_chat_resume_stage(
    execution: AgentTaskExecution | None,
    expected: Path,
) -> Path:
    if execution is None or not execution.stage_root:
        raise ValueError(
            "Cannot safely resume this chat because its saved stage is missing. "
            "Retry the turn from the beginning."
        )
    if execution.stage_host:
        raise ValueError(
            "Cannot safely resume this chat because its saved stage host does not match "
            "the execution machine. Retry the turn from the beginning."
        )
    stored = Path(execution.stage_root)
    if (
        stored.absolute() != expected.absolute()
        or stored.is_symlink()
        or not stored.is_dir()
    ):
        raise ValueError(
            "Cannot safely resume this chat because its saved stage belongs to a different "
            "project or conversation, or is unavailable. Retry the turn from the beginning."
        )
    return stored


def _chat_native_checkpoint_available(
    execution: AgentTaskExecution | None,
    native_session_id: str | None,
) -> bool:
    if execution is None:
        return bool(native_session_id)
    task = execution.store.agent_task(execution.operation_id)
    return task is not None and bool(task.native_session_id)


def _agent_read_dirs(
    context: RunContext,
    remote_stage: RemoteRunStage | None,
    service: ProjectService,
    execution_machine: str,
) -> list[Path]:
    """Directories the agent may need to read from outside its scratch folder.

    Only Claude consumes these (as `--add-dir`); Codex reads are unrestricted in
    every sandbox mode. Repositories on another machine are deliberately absent —
    those are reached over ssh from the pointers in the prompt, never copied.
    """
    read_dirs = [
        Path(item.path)
        for item in context.repositories
        if item.machine == execution_machine
    ]
    if remote_stage is not None:
        # Derived from the manifest, not from the context: on a resumed run the
        # context still carries local paths because it is never re-staged.
        assert remote_stage.root is not None
        read_dirs.append(Path(str(remote_stage.root / "inputs")))
        state_repository = service.manifest.repository_map[service.manifest.state.repository]
        if state_repository.machine == execution_machine:
            state_root = Path(state_repository.path) / ".research"
            if str(state_root) not in {str(item) for item in read_dirs}:
                read_dirs.append(state_root)
        return read_dirs
    read_dirs = [item for item in read_dirs if item.exists()]
    read_dirs.append(service.manifest.research_dir)
    for root in _conversation_roots(context).values():
        candidate = Path(root)
        if candidate.is_dir():
            read_dirs.append(candidate)
    return read_dirs


def _chat_read_dirs(
    context: ChatContext,
    remote_stage: RemoteRunStage | None,
    service: ProjectService,
    execution_machine: str,
    conversation_projection: Path | PurePosixPath | None,
) -> list[Path]:
    """Provider-generic read roots outside the chat scratch folder."""
    read_dirs = [
        Path(item.path) for item in context.repositories if item.machine == execution_machine
    ]
    if conversation_projection is None:
        raise StateUnavailable(
            "The saved conversation projection is unavailable; retry this chat turn."
        )
    if remote_stage is not None:
        assert remote_stage.root is not None
        read_dirs.append(Path(str(remote_stage.root / "inputs")))
        state_repository = service.manifest.repository_map[service.manifest.state.repository]
        if state_repository.machine == execution_machine:
            state_root = Path(state_repository.path) / ".research"
            if str(state_root) not in {str(item) for item in read_dirs}:
                read_dirs.append(state_root)
        read_dirs.append(Path(str(conversation_projection)))
        return read_dirs
    read_dirs = [item for item in read_dirs if item.exists()]
    read_dirs.append(service.manifest.research_dir)
    projection = Path(conversation_projection)
    if not projection.is_dir():
        raise StateUnavailable(
            "The saved conversation projection is unavailable; retry this chat turn."
        )
    read_dirs.append(projection)
    return read_dirs


def _work_write_dirs(
    context: ChatContext,
    service: ProjectService,
    execution_machine: str,
    *,
    remote: bool,
) -> list[Path]:
    """Exact on-machine repository roots authorized by this Work turn."""

    pointers = [
        item
        for item in context.repositories
        if item.machine == execution_machine and not item.host
    ]
    state_repository = service.manifest.repository_map[service.manifest.state.repository]
    if state_repository.machine == execution_machine:
        canonical_root = PurePosixPath(posixpath.normpath(state_repository.path))
        canonical_research = canonical_root / ".research"
        for pointer in pointers:
            candidate = PurePosixPath(posixpath.normpath(pointer.path))
            if _overlaps_canonical_state(candidate, canonical_root, canonical_research):
                raise StateUnavailable(
                    f"Work repository root {pointer.path!r} overlaps canonical RCP state; "
                    "select the exact state repository root or a non-overlapping repository."
                )
        if not remote:
            resolved_root = Path(state_repository.path).resolve()
            resolved_research = service.manifest.research_dir.resolve()
            for pointer in pointers:
                resolved_candidate = Path(pointer.path).resolve()
                if _overlaps_canonical_state(
                    resolved_candidate,
                    resolved_root,
                    resolved_research,
                ):
                    raise StateUnavailable(
                        f"Work repository root {pointer.path!r} resolves across canonical RCP "
                        "state; select the exact state repository root or a non-overlapping "
                        "repository."
                    )
    roots = [Path(item.path) for item in pointers]
    if remote:
        return list(dict.fromkeys(roots))
    missing = [str(path) for path in roots if not path.is_dir()]
    if missing:
        raise StateUnavailable(
            "Work repository roots are unavailable on the execution machine: "
            f"{missing}"
        )
    return list(dict.fromkeys(roots))


def _overlaps_canonical_state(
    candidate: Path | PurePosixPath,
    canonical_root: Path | PurePosixPath,
    canonical_research: Path | PurePosixPath,
) -> bool:
    inside_research = (
        candidate == canonical_research or canonical_research in candidate.parents
    )
    ancestor_of_state = candidate != canonical_root and candidate in canonical_root.parents
    return inside_research or ancestor_of_state


def _project_chat_conversations(
    context: ChatContext,
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
) -> tuple[ChatContext, Path | PurePosixPath]:
    """Copy only authorized on-machine conversations into the chat stage."""
    on_machine = [pointer for pointer in context.conversations if not pointer.host]
    unavailable = context.conversations_unreachable + len(context.conversations) - len(on_machine)
    entries = [
        (pointer.path, _session_bundle_relative_path(pointer).as_posix())
        for pointer in on_machine
    ]
    if remote_stage is not None:
        projected_paths = remote_stage.replace_conversation_inputs(entries)
        projection = remote_stage.require_conversation_inputs()
    else:
        if local_stage is None:
            raise RuntimeError("local chat stage is unavailable")
        projection = _replace_local_conversation_inputs(local_stage, entries)
        projected_paths = [str(projection / relative) for _source, relative in entries]
    conversations = [
        pointer.model_copy(update={"path": path})
        for pointer, path in zip(on_machine, projected_paths, strict=True)
    ]
    return context.model_copy(
        update={
            "conversations": conversations,
            "conversations_unreachable": unavailable,
        }
    ), projection


def _replace_local_conversation_inputs(
    stage: Path, sources: list[tuple[str, str]]
) -> Path:
    """Replace ``inputs/conversations`` with real copies, failing before launch."""
    parent = stage / "inputs"
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / "conversations"
    staged = Path(tempfile.mkdtemp(prefix=".conversations-", dir=parent))
    try:
        for source_text, relative in sources:
            source = Path(source_text)
            if not source.is_file():
                raise StateUnavailable(f"Conversation input is unavailable: {source}")
            output = staged / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, output, follow_symlinks=True)
            output.chmod(0o400)
        for directory in sorted(
            (item for item in staged.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            directory.chmod(0o500)
        staged.chmod(0o500)
        _remove_local_tree(target, parent)
        os.replace(staged, target)
    except Exception:
        if os.path.lexists(staged):
            _remove_local_tree(staged, parent)
        raise
    return target


def _rebind_chat_conversations(
    context: ChatContext,
    projection: Path | PurePosixPath,
    *,
    verify_local: bool,
) -> ChatContext:
    available = [pointer for pointer in context.conversations if not pointer.host]
    rebound = [
        pointer.model_copy(
            update={"path": str(projection / _session_bundle_relative_path(pointer))}
        )
        for pointer in available
    ]
    if verify_local and any(not Path(pointer.path).is_file() for pointer in rebound):
        raise StateUnavailable(
            "The saved grouped conversation inputs are incomplete; retry this chat turn."
        )
    return context.model_copy(
        update={
            "conversations": rebound,
            "conversations_unreachable": (
                context.conversations_unreachable
                + len(context.conversations)
                - len(available)
            ),
        }
    )


def _chat_conversation_roots(context: ChatContext) -> dict[str, str]:
    roots: dict[str, str] = {}
    for pointer in context.conversations:
        path = PurePosixPath(pointer.path)
        root = str(path.parents[2])
        previous = roots.setdefault(pointer.provider, root)
        if previous != root:
            raise ValueError(f"provider {pointer.provider!r} has more than one visible root")
    return roots


def _saved_chat_conversation_projection(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
) -> Path | PurePosixPath:
    """Recover, but never refresh, the exact projection used by a resumed turn."""
    if remote_stage is not None:
        return remote_stage.require_conversation_inputs()
    if local_stage is None:
        raise RuntimeError("local chat stage is unavailable")
    projection = local_stage / "inputs" / "conversations"
    if not projection.is_dir():
        raise StateUnavailable(
            "The saved conversation projection is unavailable; retry this chat turn instead."
        )
    return projection


def _cleanup_chat_conversation_projection(
    local_stage: Path | None,
    remote_stage: RemoteRunStage | None,
    execution: AgentTaskExecution | None,
) -> None:
    """Best-effort terminal cleanup of only ``inputs/conversations``."""
    try:
        if remote_stage is not None:
            remote_stage.remove_conversation_inputs()
            return
        if local_stage is None:
            return
        inputs = local_stage / "inputs"
        _remove_local_tree(inputs / "conversations", inputs)
    except (OSError, StateUnavailable, ValueError) as exc:
        if execution is not None:
            execution.store.record_agent_task_event(
                execution.operation_id,
                f"Conversation projection cleanup could not reclaim its copies: {exc}",
                level="warning",
            )


def _stage_graph_context(
    context: RunContext,
    service: ProjectService,
    stage: RemoteRunStage,
    execution_machine: str,
) -> RunContext:
    """Give a remote agent paths it can actually open.

    RCP's materialized state is never copied when the canonical state repository
    already lives on the execution machine — the agent reads `.research/` there,
    which is the same bytes RCP validates against because the local tree is an
    rsync mirror of it and the run lock is held. Only conversation slices are
    staged, because they are RCP-derived artifacts that exist nowhere else.
    """
    updates = _stage_context_paths(context, service, stage, execution_machine)
    staged_sessions = []
    with tempfile.TemporaryDirectory(prefix="rcp-session-stage-") as bundle_root:
        bundle = Path(bundle_root)
        labels: list[Path] = []
        created: set[Path] = set()
        for session in context.sessions:
            path = Path(session.path)
            if not path.is_file():
                raise StateUnavailable(f"Conversation slice is unavailable: {session.path}")
            label = _session_bundle_relative_path(session)
            target = bundle / label
            if label not in created:
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(path, target)
                except OSError:
                    shutil.copy2(path, target)
                created.add(label)
            labels.append(label)
        remote_bundle = PurePosixPath(stage.put_directory(bundle, "conversations"))
        staged_sessions = [
            session.model_copy(update={"path": str(remote_bundle / label)})
            for session, label in zip(context.sessions, labels, strict=True)
        ]
    staged_context = context.model_copy(update=updates)
    inline, omitted = bounded_session_metadata(staged_sessions)
    return staged_context.model_copy(
        update={
            "sessions": staged_sessions,
            "sessions_inline": inline,
            "sessions_omitted": omitted,
            "session_routing_index": None,
        }
    )


def _stage_local_graph_conversations(context: RunContext, stage: Path) -> RunContext:
    """Project normalized slices into one reversible directory tree per provider."""
    inputs = stage / "inputs"
    inputs.mkdir(mode=0o700, parents=True, exist_ok=True)
    target_root = inputs / "conversations"
    if target_root.exists():
        raise ValueError("immutable graph conversation inputs already exist")
    staged_root = Path(tempfile.mkdtemp(prefix=".conversations-", dir=inputs))
    labels: list[Path] = []
    created: set[Path] = set()
    try:
        for session in context.sessions:
            source = Path(session.path)
            if not source.is_file():
                raise StateUnavailable(f"Conversation slice is unavailable: {session.path}")
            label = _session_bundle_relative_path(session)
            destination = staged_root / label
            if label not in created:
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(source, destination)
                except OSError:
                    shutil.copy2(source, destination)
                destination.chmod(0o400)
                created.add(label)
            labels.append(label)
        for directory in sorted(
            (item for item in staged_root.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            directory.chmod(0o500)
        staged_root.chmod(0o500)
        os.replace(staged_root, target_root)
    finally:
        if staged_root.exists():
            _remove_local_tree(staged_root, inputs)
    staged_sessions = [
        session.model_copy(update={"path": str(target_root / label)})
        for session, label in zip(context.sessions, labels, strict=True)
    ]
    inline, omitted = bounded_session_metadata(staged_sessions)
    return context.model_copy(
        update={
            "sessions": staged_sessions,
            "sessions_inline": inline,
            "sessions_omitted": omitted,
            "session_routing_index": None,
        }
    )


def _rebind_graph_conversations(context: RunContext, root: Path) -> RunContext:
    if not root.is_dir():
        raise StateUnavailable(
            "The saved grouped conversation inputs are unavailable; retry this operation."
        )
    staged_sessions = [
        session.model_copy(update={"path": str(root / _session_bundle_relative_path(session))})
        for session in context.sessions
    ]
    if any(not Path(session.path).is_file() for session in staged_sessions):
        raise StateUnavailable(
            "The saved grouped conversation inputs are incomplete; retry this operation."
        )
    inline, omitted = bounded_session_metadata(staged_sessions)
    return context.model_copy(
        update={
            "sessions": staged_sessions,
            "sessions_inline": inline,
            "sessions_omitted": omitted,
            "session_routing_index": None,
        }
    )


def _session_bundle_relative_path(
    session: SessionPointer | ConversationPointer,
) -> Path:
    key = session.key
    parts = key.split("/", 3)
    if len(parts) != 4:
        raise ValueError(f"conversation session key is not reversible: {key!r}")
    repository, machine, provider, session_id = parts
    if provider != session.provider:
        raise ValueError(f"conversation session provider does not match its key: {key!r}")
    return Path(
        quote(provider, safe=""),
        quote(repository, safe=""),
        quote(machine, safe=""),
        f"{quote(session_id, safe='')}.jsonl",
    )


def _conversation_roots(context: RunContext) -> dict[str, str]:
    roots: dict[str, str] = {}
    for session in context.sessions:
        path = PurePosixPath(session.path)
        if len(path.parents) < 3:
            raise ValueError(f"staged conversation path has no provider root: {path}")
        root = str(path.parents[2])
        previous = roots.setdefault(session.provider, root)
        if previous != root:
            raise ValueError(f"provider {session.provider!r} has more than one visible root")
    return roots


def _stage_context_paths(
    context: RunContext | ChatContext,
    service: ProjectService,
    stage: RemoteRunStage,
    execution_machine: str,
) -> dict[str, object]:
    """Repository and materialized-state pointers a remote agent can open."""

    repositories = []
    for repository in context.repositories:
        if repository.machine == execution_machine:
            repositories.append(repository.model_copy(update={"host": ""}))
            continue
        if not repository.host:
            raise StateUnavailable(
                f"Repository {repository.alias!r} is on {repository.machine!r}, which has no "
                f"SSH host reachable from execution machine {execution_machine!r}. Run the "
                "agent on that repository's machine or configure a reachable host."
            )
        repositories.append(repository)
    updates: dict[str, object] = {"repositories": repositories}
    state_repository = service.manifest.repository_map[service.manifest.state.repository]
    if state_repository.machine != execution_machine:
        raise StateUnavailable(
            "Graph-writing agents must run on the canonical state machine; "
            "cross-machine state staging is forbidden."
        )
    canonical = PurePosixPath(state_repository.path) / ".research"
    for field in _STATE_PATH_FIELDS:
        raw_path = getattr(context, field)
        if raw_path:
            updates[field] = str(canonical / Path(raw_path).name)
    updates["facts_dir"] = str(canonical / "facts")
    return updates


def _chat_path(service: ProjectService, request: RunRequest) -> Path:
    assert request.chat_id is not None
    return service.chat_path(
        request.chat_id,
        chat_scope=request.chat_scope,
        node_id=request.node_id,
    )


def _require_agent_patch_identity(patch: Patch, run_kind: str) -> None:
    if patch.author != "agent" or patch.kind != run_kind:
        raise ValueError(
            f"The {run_kind} agent must return an agent-authored {run_kind} patch; "
            "human approval patches can only be created by the RCP review UI."
        )


def _paper_snapshot_path(data_dir: Path, project_id: str) -> Path:
    exports = data_dir / "paper-snapshots"
    exports.mkdir(parents=True, exist_ok=True)
    safe_project_id = re.sub(r"[^A-Za-z0-9._-]+", "_", project_id).strip("._")
    return exports / f"{(safe_project_id or 'project')[:80]}-introduction.md"


def _known_chat_session(service: ProjectService, request: RunRequest) -> bool:
    if not request.session_id:
        return True
    path = _chat_path(service, request)
    if not path.is_file():
        return False
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if (
                record.get("nativeSessionId") == request.session_id
                and record.get("provider") == request.provider
                and record.get("nodeId") == request.node_id
                and record.get("chatScope", "node") == request.chat_scope
                and record.get("executionMachine") == request.run_on
                and record.get("model") == (request.model or "provider-default")
                and record.get("reasoning") == request.reasoning
            ):
                return True
    except (OSError, json.JSONDecodeError):
        return False
    return False


def _append_chat_exchange(
    service: ProjectService,
    request: RunRequest,
    answer: str,
    native_session_id: str | None,
    applied_revision: int | None,
    *,
    graph_update: GraphUpdateResult | None = None,
    execution: AgentTaskExecution | None = None,
) -> None:
    assert request.message is not None
    assert request.chat_id is not None
    with service.history.workspace.transaction():
        path = _chat_path(service, request)
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).isoformat()
        common = {
            "sessionId": request.chat_id,
            "nativeSessionId": native_session_id,
            "nodeId": request.node_id,
            "chatScope": request.chat_scope,
            "provider": request.provider,
            "model": request.model or "provider-default",
            "reasoning": request.reasoning,
            "executionMachine": request.run_on,
            "cwd": str(service.manifest.research_dir.parent),
            "timestamp": timestamp,
            "operationId": execution.operation_id if execution is not None else None,
            "mode": request.mode,
        }
        records = [
            {
                **common,
                "uuid": str(uuid.uuid4()),
                "type": "user",
                "role": "user",
                "text": request.message,
            },
            {
                **common,
                "uuid": str(uuid.uuid4()),
                "type": "assistant",
                "role": "assistant",
                "text": answer,
                "appliedRevision": applied_revision,
                "graphUpdate": (
                    graph_update.model_dump(mode="json") if graph_update is not None else None
                ),
            },
        ]
        lock_path = service.history.workspace.root / ".chat.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                with path.open("a", encoding="utf-8") as handle:
                    for record in records:
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        service.history.workspace.publish([path.relative_to(service.history.workspace.root)])
    # The transcript is itself an indexed app_chat source.
    service.invalidate_source_index()


def _append_chat_graph_receipt(
    service: ProjectService,
    request: RunRequest,
    native_session_id: str | None,
    graph_update: GraphUpdateResult,
    execution: AgentTaskExecution,
) -> None:
    """Append only a durable receipt for a manual patch repair continuation."""

    assert request.chat_id is not None
    with service.history.workspace.transaction():
        path = _chat_path(service, request)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "sessionId": request.chat_id,
            "nativeSessionId": native_session_id,
            "nodeId": request.node_id,
            "chatScope": request.chat_scope,
            "provider": request.provider,
            "model": request.model or "provider-default",
            "reasoning": request.reasoning,
            "executionMachine": request.run_on,
            "cwd": str(service.manifest.research_dir.parent),
            "timestamp": datetime.now(UTC).isoformat(),
            "operationId": execution.operation_id,
            "mode": "work",
            "uuid": str(uuid.uuid4()),
            "type": "assistant",
            "role": "assistant",
            "text": "",
            "appliedRevision": graph_update.applied_revision,
            "graphUpdate": graph_update.model_dump(mode="json"),
        }
        lock_path = service.history.workspace.root / ".chat.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        service.history.workspace.publish([path.relative_to(service.history.workspace.root)])
    service.invalidate_source_index()


def _sse(event: AgentEvent) -> str:
    return f"data: {event.model_dump_json()}\n\n"


def default_data_dir() -> Path:
    override = os.environ.get("RCP_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "research-control-panel"
