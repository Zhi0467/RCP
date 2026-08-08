from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing, asynccontextmanager, contextmanager, suppress
from pathlib import Path
from typing import Literal, cast
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from rcp import __version__
from rcp.agents import AcceptanceAgentLauncher, AgentLauncher, ProviderReadiness
from rcp.artifacts import (
    ARTIFACT_MEDIA_TYPES,
    AgentArtifactDescriptor,
    descriptor_for,
    html_preview_document,
    read_local_regular_file,
    validate_artifact_bytes,
)
from rcp.background import (
    AgentTaskExecution,
    AgentTaskRequest,
    BackgroundAgentTasks,
)
from rcp.config import AgentSurface
from rcp.control import (
    ExperimentControlState,
    ExperimentOperationalState,
    ExperimentSessionBinding,
    admit_experiment_watcher_invocation,
    derive_experiment_control_state,
)
from rcp.core.models import Experiment, ExperimentDecisionPin, GraphState
from rcp.history import PatchRejected, ReplayHalted
from rcp.limits import (
    CHAT_ARTIFACT_MAX_FILE_BYTES,
    CHAT_PAGE_DEFAULT_LIMIT,
    CHAT_PAGE_MAX_LIMIT,
)
from rcp.projects import ProjectCatalog
from rcp.provider_skills import ProviderSkillInventoryManager
from rcp.providers import PROVIDER_IDS, profile_for
from rcp.runs.chat import _logical_chat_turn_operation_id
from rcp.runs.coach import _resolved_coach_request, stream_coach
from rcp.runs.discuss import stream_discuss_run
from rcp.runs.experiment_loop import experiment_watcher_delivery_request, preflight_episode_wake
from rcp.runs.graph import stream_graph_run
from rcp.runs.shared import _sweep_stale_stages
from rcp.runs.work import stream_work_run
from rcp.server_runtime import ServerMetadata, data_dir_identity, remove_server_metadata
from rcp.service import (
    ChatSummaryPage,
    ChatTranscript,
    CoachRequest,
    GraphSyncRequest,
    NodeEditConflict,
    ProjectService,
    ProjectSettingsRequest,
    RunRequest,
)
from rcp.setup import ProjectSetupManager, ProjectSetupRequest
from rcp.skill_registry import SkillKind, SkillSelection, official_registry
from rcp.sources import (
    REMOTE_SOURCE_CACHE_LIMITS,
    SESSION_SLICE_CACHE_LIMITS,
    RebuildableCache,
)
from rcp.storage import (
    AgentTaskKind,
    AgentUsageSnapshot,
    AppStore,
    ExperimentLoopRuntime,
    WatcherClaimConflict,
    WatcherRecord,
)
from rcp.transport import RemoteRunStage, StateUnavailable
from rcp.watchers import WatcherPoller
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
    acceptance_agent: bool = False,
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
    launcher = AcceptanceAgentLauncher() if acceptance_agent else AgentLauncher()
    agent_mode: Literal["acceptance", "provider"] = "acceptance" if acceptance_agent else "provider"
    provider_skills = ProviderSkillInventoryManager(store)
    catalog = ProjectCatalog(app_data, store, launcher, provider_skills)
    setup = ProjectSetupManager(app_data, catalog, launcher)
    default_record = catalog.register(manifest_path) if manifest_path else None
    default_project_id = default_record.project_id if default_record else None
    default_project_name = default_record.name if default_record else None
    default_state_host = (
        catalog.state_host(default_project_id) if default_project_id is not None else ""
    )
    default_service = (
        _LazyProjectService(catalog, default_project_id) if default_project_id is not None else None
    )
    experiment_operation_locks: dict[str, threading.RLock] = {}
    experiment_operation_locks_guard = threading.Lock()

    def experiment_operation_lock(project_id: str) -> threading.RLock:
        with experiment_operation_locks_guard:
            return experiment_operation_locks.setdefault(project_id, threading.RLock())

    @contextmanager
    def experiment_admission(
        project_id: str,
        service: ProjectService,
        request: RunRequest | CoachRequest | dict[str, object],
    ) -> Iterator[None]:
        control_node_id = _experiment_control_node_id(request)
        if control_node_id is None:
            yield
            return
        with experiment_operation_lock(project_id):
            if not isinstance(service.history.state().nodes.get(control_node_id), Experiment):
                raise ValueError(
                    f"Experiment {control_node_id} no longer exists; it cannot be continued."
                )
            yield

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
                stream_coach(
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
            if request.mode == "work":
                async with aclosing(
                    stream_work_run(
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
                stream_discuss_run(
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
            stream_graph_run(
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

    def deliver_watcher_group(group: list[WatcherRecord]) -> None:
        if not group:
            return
        first = group[0]
        continuation = first.continuation
        watcher_ids = [item.watcher_id for item in group]
        service = _project_service(catalog, first.project_id)
        if continuation.patch_kind == "experiment_loop":
            control_node_id = continuation.control_node_id
            if control_node_id is None:
                raise ValueError("An Experiment watcher is missing its control node.")
            with experiment_operation_lock(first.project_id):
                state = service.history.state()
                if not isinstance(state.nodes.get(control_node_id), Experiment):
                    store.stop_watchers(first.project_id, watcher_ids)
                    return
                runtime = store.experiment_loop_runtime(first.project_id, control_node_id)
                episode = (
                    store.experiment_episode(runtime.episode_id)
                    if runtime.episode_id is not None
                    else None
                )
                if episode is None:
                    preflight = preflight_episode_wake(runtime, None, group)
                    if runtime.episode_id is not None:
                        store.record_experiment_episode_diagnostic(
                            episode_id=runtime.episode_id,
                            project_id=first.project_id,
                            control_node_id=control_node_id,
                            diagnostic=preflight.diagnostic,
                        )
                    return
                current_machine = (
                    service.manifest.machine_map.get(runtime.run_on)
                    if runtime.run_on is not None
                    else None
                )
                current_host = current_machine.host if current_machine is not None else None
                binding_host = episode.execution_host if episode is not None else None
                group_hosts = {item.execution_host for item in group}
                if (
                    current_host is None
                    or binding_host != current_host
                    or (episode.stage_host or "") != current_host
                    or group_hosts != {current_host}
                ):
                    if runtime.episode_id is not None:
                        store.record_experiment_episode_diagnostic(
                            episode_id=runtime.episode_id,
                            project_id=first.project_id,
                            control_node_id=control_node_id,
                            diagnostic=(
                                "The Experiment episode's saved execution host or stage host "
                                "no longer matches the current project manifest. Stop the loop "
                                "and start a new Run after confirming the execution target."
                            ),
                        )
                    return
                # The episode session is proved before the claim and before the
                # budget spend, so an unusable binding never costs an invocation
                # and never quietly becomes a fresh session.
                preflight = preflight_episode_wake(runtime, episode, group)
                if preflight.readiness == "unavailable":
                    if runtime.episode_id is not None:
                        store.record_experiment_episode_diagnostic(
                            episode_id=runtime.episode_id,
                            project_id=first.project_id,
                            control_node_id=control_node_id,
                            diagnostic=preflight.diagnostic,
                        )
                    return
                if preflight.readiness != "ready":
                    # Transient unreachability and an incompatible group both
                    # leave the completion pending and visible for a later pass.
                    return
                if runtime.session_diagnostic is not None and runtime.episode_id is not None:
                    store.record_experiment_episode_diagnostic(
                        episode_id=runtime.episode_id,
                        project_id=first.project_id,
                        control_node_id=control_node_id,
                        diagnostic=None,
                    )
                pins = [
                    ExperimentDecisionPin.model_validate(item) for item in runtime.decision_bundle
                ]
                admission = admit_experiment_watcher_invocation(
                    state,
                    control_node_id,
                    episode_id=runtime.episode_id,
                    invocations_used=runtime.invocations_used,
                    invocation_ceiling=runtime.invocation_ceiling,
                    decision_bundle=pins,
                    task_active=runtime.task_active,
                    episode_exited=runtime.episode_exited,
                    stop_requested=runtime.stop_requested,
                )
                if admission is None:
                    return
                if runtime.control_revision is None:
                    raise ValueError("An Experiment watcher is missing its control revision.")
                request = experiment_watcher_delivery_request(
                    group,
                    trigger="watcher",
                    episode_id=admission.episode_id,
                    invocation=admission.invocation,
                    invocation_ceiling=admission.invocation_ceiling,
                    control_revision=runtime.control_revision,
                    decision_bundle=admission.decision_bundle,
                    completion_criteria=runtime.completion_criteria,
                    session_id=preflight.session_id,
                )
                request = request.model_copy(
                    update={
                        "provider": runtime.provider,
                        "model": runtime.model,
                        "reasoning": runtime.reasoning,
                        "run_on": runtime.run_on,
                        "run_truth_scope": runtime.run_truth_scope,
                        "chat_id": runtime.chat_id,
                        "session_id": preflight.session_id,
                    }
                )
                background_tasks.start_watcher_notification(
                    first.project_id,
                    first.origin_task_kind,
                    request,
                    watcher_ids,
                    episode_stage_host=preflight.stage_host,
                    episode_stage_root=preflight.stage_root,
                )
            return

        request = _generic_watcher_delivery_request(group)
        with experiment_admission(first.project_id, service, request):
            background_tasks.start_watcher_notification(
                first.project_id,
                first.origin_task_kind,
                request,
                watcher_ids,
            )

    watcher_poller = WatcherPoller(store, on_completed=deliver_watcher_group)

    async def warm_provider_capabilities() -> None:
        try:
            targets = await asyncio.to_thread(catalog.provider_targets)

            def mark_refreshing() -> None:
                for provider, host, binary in targets:
                    provider_skills.mark_refreshing(provider, host, binary)

            def probe(provider: str, host: str, binary: str | None) -> None:
                try:
                    readiness = launcher.readiness(provider, host=host, binary=binary)
                except Exception as exc:
                    # An unexpected readiness exception still has to finish
                    # this startup generation. Feeding the diagnostic through
                    # the inventory manager preserves any last-good skills as
                    # stale instead of leaving the target stuck refreshing.
                    readiness = ProviderReadiness(
                        provider=provider,
                        installed=False,
                        authenticated=False,
                        binary_path=binary,
                        path_state="unreachable" if host else "missing",
                        reason=str(exc),
                    )
                    provider_skills.refresh(provider, host, binary, readiness)
                    raise
                provider_skills.refresh(provider, host, binary, readiness)

            # Mark the whole startup inventory before beginning any provider
            # process so the UI never mistakes a prior process's cache for a
            # completed refresh in this one. All SQLite and CLI/SSH work stays
            # off the event loop.
            await asyncio.to_thread(mark_refreshing)

            results = await asyncio.gather(
                *(
                    asyncio.to_thread(probe, provider, host, binary)
                    for provider, host, binary in targets
                ),
                return_exceptions=True,
            )
            for target, result in zip(targets, results, strict=True):
                if isinstance(result, Exception):
                    logger.warning("Could not warm provider capability %s: %s", target, result)
        except Exception as exc:
            # Warming is an optimization; readiness remains authoritative and
            # can retry an individual capability when explicitly requested.
            logger.warning("Could not warm provider capabilities: %s", exc)

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
            startup_maintenance.append(asyncio.create_task(warm_provider_capabilities()))
            if default_state_host:
                startup_maintenance.append(asyncio.create_task(sweep_remote_run_stages()))
            watcher_poller.start()
            yield
        finally:
            for task in startup_maintenance:
                task.cancel()
            for task in startup_maintenance:
                with suppress(asyncio.CancelledError):
                    await task
            watcher_poller.stop()
            background_tasks.shutdown()
            # A PyInstaller one-file backend runs under a bootloader supervisor
            # whose signal exit can skip the CLI context manager's ``finally``.
            # Source reload workers share metadata owned by the outer supervisor,
            # so they must leave it in place across worker restarts.
            if getattr(sys, "frozen", False):
                remove_server_metadata(app_data, instance_id=identity.instance_id)

    app = FastAPI(title="RCP", version=__version__, lifespan=lifespan)
    app.state.catalog = catalog
    app.state.provider_skills = provider_skills
    app.state.setup = setup
    app.state.default_project_id = default_project_id
    app.state.service = default_service
    app.state.data_dir = app_data
    app.state.background_tasks = background_tasks
    app.state.watcher_poller = watcher_poller
    app.state.instance_metadata = identity
    app.state.launcher = launcher
    app.state.agent_mode = agent_mode
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
        status = 409 if any(item.code == "stale-node-edit" for item in exc.report.messages) else 422
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
            "agent_mode": agent_mode,
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

    def attach_experiment_control(project_id: str, snapshot: dict[str, object]) -> None:
        """Replace the graph-only control map with live operational state.

        `ProjectService` has no task store, so every snapshot it builds carries a
        default operational block. Any route that hands a snapshot to the client
        must overwrite it here, or a Settings save would blank the Experiment
        lifecycle the human is watching in Runs.
        """

        state = GraphState.model_validate(snapshot["graph"])
        experiment_ids = [node.id for node in state.nodes.values() if node.type == "experiment"]
        runtimes = store.experiment_loop_runtimes(project_id, experiment_ids)
        settle_ids = [
            experiment_id
            for experiment_id, runtime in runtimes.items()
            if runtime.stop_requested and not runtime.stop_settled and not runtime.task_active
        ]
        for experiment_id in settle_ids:
            store.settle_experiment_loop_stop(project_id, experiment_id)
        if settle_ids:
            runtimes.update(store.experiment_loop_runtimes(project_id, settle_ids))
        snapshot["experiment_control"] = {
            experiment_id: _experiment_control_from_runtime(
                state, experiment_id, runtimes[experiment_id]
            ).model_dump(mode="json")
            for experiment_id in experiment_ids
        }

    @app.get("/api/projects/{project_id}")
    def project(project_id: str) -> dict[str, object]:
        try:
            _, snapshot = catalog.open_snapshot(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        snapshot["id"] = project_id
        attach_experiment_control(project_id, snapshot)
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

    @app.get("/api/projects/{project_id}/revision")
    def project_revision(project_id: str) -> dict[str, int]:
        service = _project_service(catalog, project_id)
        return {"revision": service.history.current_accepted_revision()}

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
        attach_experiment_control(project_id, snapshot)
        return snapshot

    @app.post("/api/projects/{project_id}/machines/{machine_alias}/providers/{provider}/resolve")
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
            attach_experiment_control(project_id, project)
        return result

    @app.get("/api/projects/{project_id}/history")
    def history(project_id: str, from_revision: int = 1, to_revision: int | None = None):
        service = _project_service(catalog, project_id)
        return service.history.slice(from_revision, to_revision)

    @app.get("/api/projects/{project_id}/history/summaries")
    def history_summaries(
        project_id: str,
        from_revision: int = 1,
        to_revision: int | None = None,
    ):
        service = _project_service(catalog, project_id)
        return service.history.revision_summaries(from_revision, to_revision)

    @app.get("/api/projects/{project_id}/sources")
    def sources(project_id: str, refresh: bool = False):
        service = _project_service(catalog, project_id)
        return service.index_snapshot(refresh=refresh).model_dump(mode="json")

    @app.post("/api/projects/{project_id}/sync")
    def sync_graph(project_id: str, body: GraphSyncRequest):
        service = _project_service(catalog, project_id)
        try:
            if body.removed_node_ids:
                with experiment_operation_lock(project_id):
                    return service.sync_graph(
                        body,
                        active_control_node_ids=store.active_experiment_control_ids(project_id),
                    ).model_dump(mode="json")
            return service.sync_graph(
                body,
                active_control_node_ids=store.active_experiment_control_ids(project_id),
            ).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"Missing graph object: {exc.args[0]}"
            ) from exc
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

    @app.post("/api/projects/{project_id}/experiments/{node_id:path}/run", status_code=202)
    def run_experiment(
        project_id: str,
        node_id: str,
        body: dict[str, object],
    ) -> dict[str, object]:
        service = _project_service(catalog, project_id)
        try:
            with experiment_operation_lock(project_id):
                state = service.history.state()
                node = state.nodes.get(node_id)
                if not isinstance(node, Experiment):
                    raise HTTPException(status_code=404, detail="Experiment not found")
                runtime, control = _experiment_control(store, project_id, state, node_id)
                if not control.ready:
                    raise HTTPException(status_code=409, detail=" ".join(control.reasons))
                supplied = RunRequest.model_validate(body)
                if not supplied.chat_id:
                    raise ValueError("Run requires a chat_id")
                uuid.UUID(supplied.chat_id)
                episode_id = str(uuid.uuid4())
                pending_group = (
                    None
                    if runtime.stop_requested and runtime.stop_settled
                    else store.completed_experiment_watcher_group(project_id, node_id)
                )
                if pending_group is not None:
                    request = experiment_watcher_delivery_request(
                        pending_group,
                        trigger="experiment_run",
                        episode_id=episode_id,
                        invocation=1,
                        invocation_ceiling=node.invocation_ceiling,
                        control_revision=state.revision,
                        decision_bundle=control.governing_decisions,
                        completion_criteria=list(node.completion_criteria),
                    )
                    request = _resolved_graph_request(
                        service,
                        pending_group[0].origin_task_kind,
                        request,
                    )
                    record = background_tasks.start_watcher_notification(
                        project_id,
                        pending_group[0].origin_task_kind,
                        request,
                        [item.watcher_id for item in pending_group],
                    )
                    if record is None:
                        raise ValueError(
                            "The pending watcher completion could not be claimed because its "
                            "conversation is active."
                        )
                    return record.model_dump(mode="json")
                profile = service.resolve_agent_profile("node_chat")
                request = supplied.model_copy(
                    update={
                        "provider": profile.provider,
                        "model": profile.model,
                        "reasoning": profile.reasoning,
                        "run_on": profile.run_on,
                        "chat_scope": "node",
                        "node_id": node_id,
                        "message": f"Begin a bounded Experiment-loop episode for {node_id}.",
                        "session_id": None,
                        "mode": "work",
                        "trigger": "experiment_run",
                        "patch_kind": "experiment_loop",
                        "control_node_id": node_id,
                        "control_revision": state.revision,
                        "control_episode_id": episode_id,
                        "control_invocation": 1,
                        "control_invocation_ceiling": node.invocation_ceiling,
                        "control_decision_bundle": control.governing_decisions,
                        "control_completion_criteria": list(node.completion_criteria),
                        "watcher_ids": [],
                    }
                )
                request = _resolved_graph_request(service, "node_chat", request)
                record = background_tasks.start(project_id, "node_chat", request)
        except ValueError as exc:
            status = 409 if "already running" in str(exc) else 422
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return record.model_dump(mode="json")

    @app.get("/api/projects/{project_id}/tasks")
    def agent_tasks(project_id: str) -> list[dict[str, object]]:
        _require_registered_project(catalog, project_id)
        return [record.model_dump(mode="json") for record in store.agent_tasks(project_id)]

    @app.get("/api/projects/{project_id}/usage", response_model=AgentUsageSnapshot)
    def agent_usage(project_id: str) -> AgentUsageSnapshot:
        _require_registered_project(catalog, project_id)
        return store.agent_usage_snapshot(project_id)

    @app.get("/api/projects/{project_id}/watchers")
    def project_watchers(project_id: str) -> list[dict[str, object]]:
        _require_registered_project(catalog, project_id)
        return [record.model_dump(mode="json") for record in store.watchers(project_id)]

    @app.post("/api/projects/{project_id}/watchers/{watcher_id}/stop")
    def stop_watcher(project_id: str, watcher_id: str) -> dict[str, object]:
        _require_registered_project(catalog, project_id)
        watcher = store.watcher(watcher_id)
        if watcher is None or watcher.project_id != project_id:
            raise HTTPException(status_code=404, detail="Watcher not found")
        if watcher.continuation.patch_kind == "experiment_loop":
            raise HTTPException(
                status_code=409,
                detail="Use Stop loop to stop an Experiment loop and its watchers gracefully.",
            )
        try:
            stopped = store.stop_watchers(project_id, [watcher_id])
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Watcher not found") from exc
        except WatcherClaimConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return stopped[0].model_dump(mode="json")

    @app.post("/api/projects/{project_id}/experiments/{node_id:path}/watchers/stop")
    def stop_experiment_watchers(project_id: str, node_id: str) -> list[dict[str, object]]:
        """Reject the retired bulk watcher control in favor of graceful Stop loop."""

        _require_registered_project(catalog, project_id)
        raise HTTPException(
            status_code=409,
            detail="This control was retired. Use Stop loop for the current Experiment episode.",
        )

    # Registered after `.../watchers/stop`: `{node_id:path}` is greedy, so this
    # route would otherwise swallow that one with a node id ending in
    # "/watchers".
    @app.post("/api/projects/{project_id}/experiments/{node_id:path}/stop")
    def stop_experiment_loop(project_id: str, node_id: str) -> dict[str, object]:
        """Finish the current turn, then disable automatic continuation.

        The stop is durable before this returns, so no unclaimed watcher can win
        a wake afterwards. It never cancels the live task, kills external work,
        deletes a watcher, or changes what the Experiment means, and calling it
        again changes nothing.
        """

        service = _project_service(catalog, project_id)
        with experiment_operation_lock(project_id):
            state = service.history.state()
            if not isinstance(state.nodes.get(node_id), Experiment):
                raise HTTPException(status_code=404, detail="Experiment not found")
            store.request_experiment_loop_stop(project_id, node_id)
            _, control = _experiment_control(store, project_id, state, node_id)
        return control.model_dump(mode="json")

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
            receipt.model_dump(mode="json") for receipt in store.agent_task_receipts(operation_id)
        ]
        detail["contracts"] = [
            contract.model_dump(mode="json")
            for contract in store.agent_task_contracts(operation_id)
        ]
        return detail

    @app.get("/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/preview")
    @app.head("/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/preview")
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

    @app.get("/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/download")
    @app.head("/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/download")
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
            with experiment_admission(project_id, service, previous.request):
                skills = _validate_stored_task_request(service, previous.kind, previous.request)
                return background_tasks.resume(operation_id, skills=skills).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/projects/{project_id}/tasks/{operation_id}/repair-graph-update",
        status_code=202,
    )
    def repair_agent_task_graph_update(project_id: str, operation_id: str) -> dict[str, object]:
        service = _project_service(catalog, project_id)
        previous = store.agent_task(operation_id)
        if previous is None or previous.project_id != project_id:
            raise HTTPException(status_code=404, detail="Agent task not found")
        try:
            with experiment_admission(project_id, service, previous.request):
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
            if previous.request.get("patch_kind") == "experiment_loop" and overrides:
                raise ValueError(
                    "Experiment-loop Retry cannot override its pinned provider configuration."
                )
            request_type = CoachRequest if previous.kind == "paper_coach" else RunRequest
            candidate = request_type.model_validate(
                {**previous.request, **overrides, "session_id": None}
            )
            with experiment_admission(project_id, service, candidate):
                skills = _validate_stored_task_request(
                    service,
                    previous.kind,
                    candidate.model_dump(mode="json"),
                )
                return background_tasks.retry(operation_id, skills=skills, **overrides).model_dump(
                    mode="json"
                )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/skills/{kind}/{package_id}")
    def read_skill_package(kind: str, package_id: str) -> dict[str, object]:
        """The official package's own text, for the read-only Settings inspector."""

        if kind not in {"skill", "workflow"}:
            raise HTTPException(status_code=404, detail="Package not found")
        registry = official_registry()
        try:
            package = registry.package(cast(SkillKind, kind), package_id)
            body = registry.package_body(cast(SkillKind, kind), package_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {**package.catalog_entry(), "body": body}

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
            stage = RemoteRunStage(record.stage_host).attach_artifact_source(record.stage_root)
            data = stage.read_artifact_bytes(
                scope_id,
                descriptor.name,
                max_bytes=CHAT_ARTIFACT_MAX_FILE_BYTES,
            )
        else:
            directory = Path(record.stage_root) / "turns" / scope_id / "artifacts"
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


def _generic_watcher_delivery_request(group: list[WatcherRecord]) -> RunRequest:
    first = group[0]
    continuation = first.continuation
    if continuation.patch_kind != "work":
        raise ValueError("A generic watcher cannot carry Experiment-loop authority.")
    watcher_ids = [item.watcher_id for item in group]
    details = "\n".join(f"- watcher `{item.watcher_id}`: `{item.log_path}`" for item in group)
    return RunRequest(
        provider=continuation.provider,
        model=continuation.model,
        reasoning=continuation.reasoning,
        run_on=continuation.run_on,
        run_truth_scope=continuation.run_truth_scope,
        chat_scope="node" if first.origin_task_kind == "node_chat" else "project",
        node_id=first.node_id,
        message=(
            "RCP watcher update: the following external work is no longer present in its "
            f"system. Inspect the named logs and continue the Work turn.\n{details}"
        ),
        chat_id=first.chat_id,
        session_id=None,
        mode="work",
        trigger="watcher",
        patch_kind="work",
        workflow_ids=continuation.workflow_ids,
        skill_ids=continuation.skill_ids,
        invoked_workflow_ids=[],
        invoked_skill_ids=[],
        resolved_skill_packages=continuation.resolved_skill_packages,
        watcher_ids=watcher_ids,
    )


def _project_service(catalog: ProjectCatalog, project_id: str) -> ProjectService:
    try:
        return catalog.open(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _experiment_control(
    store: AppStore,
    project_id: str,
    state: GraphState,
    experiment_id: str,
) -> tuple[ExperimentLoopRuntime, ExperimentControlState]:
    """Derive one Experiment's operational and semantic control state together.

    Deriving is also where a graceful stop is reconciled, so the same joint
    handoff settles identically after a restart without anyone replaying it.
    """

    runtime = store.experiment_loop_runtime(project_id, experiment_id)
    if runtime.stop_requested and not runtime.stop_settled and not runtime.task_active:
        store.settle_experiment_loop_stop(project_id, experiment_id)
        runtime = store.experiment_loop_runtime(project_id, experiment_id)
    return runtime, _experiment_control_from_runtime(state, experiment_id, runtime)


def _experiment_control_from_runtime(
    state: GraphState,
    experiment_id: str,
    runtime: ExperimentLoopRuntime,
) -> ExperimentControlState:
    """Combine graph authority with one already-projected operational runtime."""

    pins = [ExperimentDecisionPin.model_validate(item) for item in runtime.decision_bundle]
    return derive_experiment_control_state(
        state,
        experiment_id,
        {experiment_id} if runtime.active else set(),
        episode_id=runtime.episode_id,
        invocations_used=runtime.invocations_used,
        invocation_ceiling=runtime.invocation_ceiling,
        paused=runtime.paused,
        detached_work_active=runtime.detached_work_active,
        episode_decision_bundle=pins if runtime.episode_id is not None else None,
        operational=_experiment_operational_state(runtime),
    )


def _experiment_operational_state(runtime: ExperimentLoopRuntime) -> ExperimentOperationalState:
    """Project the loop runtime onto the operational block Runs reads.

    The native session id itself stays in the backend; whether one is bound is
    the only part of it the human needs.
    """

    return ExperimentOperationalState(
        task_active=runtime.task_active,
        detached_work_active=runtime.detached_work_active,
        watcher_degraded=runtime.watcher_degraded,
        watcher_completion_pending=runtime.watcher_completion_pending,
        episode_exited=runtime.episode_exited,
        stop_requested=runtime.stop_requested,
        stop_settled=runtime.stop_settled,
        chat_id=runtime.chat_id,
        current_operation_id=runtime.current_operation_id,
        current_status=runtime.current_status,
        current_phase=runtime.current_phase,
        current_status_message=runtime.current_status_message,
        current_last_activity_at=runtime.current_last_activity_at,
        current_invocation=runtime.current_invocation,
        session=ExperimentSessionBinding(
            provider=runtime.provider,
            model=runtime.model,
            reasoning=runtime.reasoning,
            run_on=runtime.run_on,
            execution_host=runtime.execution_host,
            run_truth_scope=runtime.run_truth_scope,
            native_session_bound=runtime.session_bound,
            diagnostic=runtime.session_diagnostic,
        ),
    )


def _experiment_control_node_id(
    request: RunRequest | CoachRequest | dict[str, object],
) -> str | None:
    if isinstance(request, CoachRequest):
        return None
    if isinstance(request, RunRequest):
        patch_kind = request.patch_kind
        node_id = request.control_node_id
    else:
        patch_kind = request.get("patch_kind")
        node_id = request.get("control_node_id")
    if patch_kind != "experiment_loop":
        return None
    if not isinstance(node_id, str) or not node_id:
        raise ValueError("A bounded experiment-loop task must name its control node.")
    return node_id


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

    request = RunRequest.model_validate(body).model_copy(
        update={
            "trigger": "human",
            "patch_kind": "work",
            "control_node_id": None,
            "control_revision": None,
            "control_episode_id": None,
            "control_invocation": None,
            "control_invocation_ceiling": None,
            "control_decision_bundle": [],
            "control_completion_criteria": [],
            "watcher_ids": [],
        }
    )
    if kind in {"seed", "refresh"}:
        service.history.require_writable()
        if request.session_id:
            raise ValueError(
                "Seed and refresh sessions can only be resumed from an RCP background "
                "task checkpoint."
            )
        return _resolved_graph_request(service, kind, request)

    chat_scope: Literal["node", "project"] = "node" if kind == "node_chat" else "project"
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
    return request


def _validate_stored_task_request(
    service: ProjectService,
    kind: AgentTaskKind,
    body: dict[str, object],
) -> SkillSelection:
    """Validate a stored request and return the selection this attempt would get."""

    if kind == "paper_coach":
        resolved_coach = _resolved_coach_request(service, CoachRequest.model_validate(body))
        return service.resolve_skill_selection(resolved_coach)
    request = RunRequest.model_validate(body)
    if kind in {"seed", "refresh"}:
        service.history.require_writable()
    resolved_run = _resolved_graph_request(service, kind, request)
    return service.resolve_skill_selection(resolved_run)


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
    resolved = request.model_copy(
        update={
            "provider": profile.provider,
            # An empty string is the explicit provider-default sentinel. Once a
            # request is resolved it must not collapse back to None, which means
            # "inherit the current surface setting" on a later continuation.
            "model": profile.model,
            "reasoning": profile.reasoning,
            "run_on": profile.run_on,
            "run_truth_scope": list(
                request.run_truth_scope or service.manifest.agent.default_run_truth_scope
            ),
        }
    )
    result = service.resolve_skill_request(resolved)
    assert isinstance(result, RunRequest)
    return result


def default_data_dir() -> Path:
    override = os.environ.get("RCP_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "research-control-panel"
