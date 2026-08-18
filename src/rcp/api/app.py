from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager, suppress
from functools import partial
from pathlib import Path
from typing import Literal, cast

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rcp import __version__
from rcp.agents import AcceptanceAgentLauncher, AgentLauncher, ProviderReadiness
from rcp.agents.command_protocol import SpawnArguments
from rcp.api.chats import router as chats_router
from rcp.api.dependencies import (
    ApiServices,
    require_project_membership,
)
from rcp.api.dependencies import (
    get_project_service as _project_service,
)
from rcp.api.dependencies import require_registered_project as _require_registered_project
from rcp.api.episode_branches import (
    ensure_auto_research_graph_target as _ensure_auto_research_graph_target,
)
from rcp.api.episode_branches import (
    graph_branch_summary as _graph_branch_summary,
)
from rcp.api.episode_routes import router as episode_router
from rcp.api.episodes import (
    _episode_for_http,
    serialize_episode,
)
from rcp.api.experiment_controls import _experiment_control_from_runtime
from rcp.api.experiments import router as experiments_router
from rcp.api.history import router as history_router
from rcp.api.identity import TEAM_SESSION_COOKIE, IdentityAccess, TrustedPrincipalResolver
from rcp.api.paper import router as paper_router
from rcp.api.result_views import router as result_views_router
from rcp.api.sync import router as sync_router
from rcp.api.task_requests import _resolved_graph_request
from rcp.api.tasks import router as tasks_router
from rcp.api.watchers import router as watchers_router
from rcp.attachments import ChatAttachmentStore
from rcp.background import (
    AgentTaskExecution,
    AgentTaskRequest,
    BackgroundAgentTasks,
)
from rcp.config import load_manifest
from rcp.control import admit_experiment_watcher_invocation
from rcp.core.models import (
    DISPLAY_NAME_MAX_LENGTH,
    Experiment,
    GraphState,
    normalize_display_name,
)
from rcp.core.transition_models import GraphHeadRef
from rcp.history import PatchRejected, ReplayHalted
from rcp.keyed_locks import ExperimentAdmission, KeyedLocks
from rcp.limits import (
    TEAM_ENROLLMENT_CODE_MAX_LENGTH,
    TEAM_MEMBER_TOKEN_MAX_LENGTH,
    TEAM_PUBLIC_AUTH_REQUEST_MAX_BYTES,
)
from rcp.projects import ProjectCatalog, ProjectDisplayCache
from rcp.provider_skills import ProviderSkillInventoryManager
from rcp.providers import PROVIDER_IDS, profile_for
from rcp.repository_preview import (
    REPOSITORY_PREVIEW_CSP,
    load_repository_source_for_path,
    repository_source_document,
)
from rcp.runs.auto_research import (
    AutoResearchCommandContext,
    AutoResearchCommandDispatcher,
    AutoResearchCommandEffectResult,
    AutoResearchRunRequest,
)
from rcp.runs.auto_research_child_reconcile import (
    reconcile_pending_auto_research_child_admissions,
)
from rcp.runs.auto_research_delivery import (
    deliver_auto_research_watcher_group,
    reconcile_pending_auto_research_lifecycle,
    reconcile_pending_auto_research_mail,
)
from rcp.runs.auto_research_effects import auto_research_command_effects
from rcp.runs.auto_research_experiments import AutoResearchExperimentCoordinator
from rcp.runs.auto_research_recovery import reconcile_orphaned_auto_research_failures
from rcp.runs.auto_research_stream import (
    stream_auto_research_orchestrator_run,
    stream_auto_research_worker_run,
)
from rcp.runs.branch_merge_request import BranchMergeRunRequest
from rcp.runs.branch_merge_task import stream_branch_merge_task
from rcp.runs.coach import stream_coach
from rcp.runs.discuss import stream_discuss_run
from rcp.runs.episode_reconcile import EpisodeReconciler
from rcp.runs.episode_report import EpisodeReportRunRequest, stream_episode_report_run
from rcp.runs.experiment_loop import (
    experiment_watcher_delivery_request,
    preflight_episode_wake,
)
from rcp.runs.graph import stream_graph_run
from rcp.runs.membership_fence import fence_episodes_for_departed_member
from rcp.runs.shared import _sweep_stale_stages
from rcp.runs.task_policy import task_experiment_episode_id, task_graph_capable
from rcp.runs.transition_event_reconciliation import reconcile_accepted_graph_boundaries
from rcp.runs.work import _apply_work_patch, _validate_work_patch_live, stream_work_run
from rcp.server_runtime import ServerMetadata, data_dir_identity, remove_server_metadata
from rcp.service import (
    CoachRequest,
    ProjectService,
    ProjectSettingsRequest,
    RunRequest,
)
from rcp.setup import ProjectSetupManager, ProjectSetupRequest
from rcp.skill_registry import SkillKind, official_registry
from rcp.sources import (
    REMOTE_SOURCE_CACHE_LIMITS,
    SESSION_SLICE_CACHE_LIMITS,
    RebuildableCache,
    discover_project_cache_roots,
    legacy_shared_cache_roots,
)
from rcp.storage import (
    SPACE_NAME_MAX_LENGTH,
    AgentTaskKind,
    AgentUsageSnapshot,
    AppStore,
    EpisodeRecord,
    ExperimentLoopRuntime,
    GraphWatcherRecord,
    StoredWatcherRecord,
    TeamAuthenticationError,
    normalize_space_name,
)
from rcp.transport import RemoteRunStage, StateUnavailable
from rcp.watchers import (
    GraphWatcherRetryRegistry,
    WatcherDelivery,
    WatcherPoller,
    WatcherRetryWorker,
    ready_graph_watcher_groups,
)
from rcp.web_assets import web_dist_path

logger = logging.getLogger(__name__)


class TeamPublicAuthBodyLimit:
    """Bound unauthenticated credential requests before JSON parsing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") not in {
            "/api/team/enroll",
            "/api/team/session/exchange",
        }:
            await self.app(scope, receive, send)
            return

        messages: list[Message] = []
        total = 0
        more_body = True
        while more_body:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                more_body = False
                continue
            total += len(message.get("body", b""))
            if total > TEAM_PUBLIC_AUTH_REQUEST_MAX_BYTES:
                response = JSONResponse(
                    status_code=413,
                    content={
                        "detail": {
                            "code": "team_auth_request_too_large",
                            "message": "The team authentication request is too large.",
                        }
                    },
                )
                await response(scope, receive, send)
                return
            more_body = message.get("more_body", False)

        message_index = 0

        async def replay() -> Message:
            nonlocal message_index
            if message_index >= len(messages):
                return {"type": "http.request", "body": b"", "more_body": False}
            message = messages[message_index]
            message_index += 1
            return message

        await self.app(scope, replay, send)


class ProjectRegisterRequest(BaseModel):
    locator: str


class ProjectInviteRequest(BaseModel):
    """Who is being invited. The server derives the inviter from the session."""

    user_id: str


class SpaceIdentityUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(max_length=DISPLAY_NAME_MAX_LENGTH)

    @field_validator("display_name", mode="before")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        return normalize_display_name(value)


class TeamEnrollmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: str = Field(min_length=1, max_length=TEAM_ENROLLMENT_CODE_MAX_LENGTH)
    display_name: str = Field(max_length=DISPLAY_NAME_MAX_LENGTH)

    @field_validator("display_name", mode="before")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        return normalize_display_name(value)


class TeamSessionExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    token: str = Field(min_length=1, max_length=TEAM_MEMBER_TOKEN_MAX_LENGTH)


class TeamSpaceUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(max_length=SPACE_NAME_MAX_LENGTH)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return normalize_space_name(value)


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
    trusted_principal_resolver: TrustedPrincipalResolver | None = None,
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
    space_id = store.space_id
    space_kind = store.space_kind
    identity_access = IdentityAccess(
        store,
        space_id=space_id,
        space_kind=space_kind,
        trusted_principal_resolver=trusted_principal_resolver,
    )
    require_team_space = identity_access.require_team_space
    set_team_session_cookie = identity_access.set_team_session_cookie
    clear_team_session_cookie = identity_access.clear_team_session_cookie
    resolve_team_user = identity_access.resolve_team_user
    authenticating_team_session = identity_access.authenticating_team_session
    acting_user = identity_access.acting_user
    identity_payload = identity_access.identity_payload
    launcher = AcceptanceAgentLauncher() if acceptance_agent else AgentLauncher()
    agent_mode: Literal["acceptance", "provider"] = "acceptance" if acceptance_agent else "provider"
    provider_skills = ProviderSkillInventoryManager(store)
    catalog = ProjectCatalog(app_data, store, launcher, provider_skills)
    attachment_store = ChatAttachmentStore(app_data / "chat-attachments")

    ensure_auto_research_graph_target = partial(
        _ensure_auto_research_graph_target,
        catalog=catalog,
    )
    graph_branch_summary = partial(
        _graph_branch_summary,
        store=store,
        catalog=catalog,
    )

    project_display_cache = ProjectDisplayCache(
        store,
        catalog,
        serialize_episode=lambda project_id, episode: serialize_episode(
            store,
            project_id,
            episode,
            branch_summary=graph_branch_summary,
        ).model_dump(mode="json"),
        project_experiment_control=lambda state, experiment_id, runtime: (
            _experiment_control_from_runtime(state, experiment_id, runtime).model_dump(mode="json")
        ),
        logger=logger,
    )
    refresh_cached_project_after_stream = project_display_cache.refresh_cached_project_after_stream
    schedule_project_reconciliation = project_display_cache.schedule_project_reconciliation

    setup = ProjectSetupManager(app_data, catalog, launcher)
    default_record = (
        catalog.register(manifest_path, identity_action="adopted") if manifest_path else None
    )
    default_project_id = default_record.project_id if default_record else None
    default_project_name = default_record.name if default_record else None
    default_state_host = (
        catalog.state_host(default_project_id) if default_project_id is not None else ""
    )
    default_service = (
        _LazyProjectService(catalog, default_project_id) if default_project_id is not None else None
    )
    experiment_operation_lock = KeyedLocks(threading.RLock)
    result_view_keep_lock = KeyedLocks()
    experiment_admission = ExperimentAdmission(
        experiment_operation_lock,
        _experiment_control_node_id,
    )
    graph_watcher_retry = GraphWatcherRetryRegistry()

    async def background_task_stream(
        project_id: str,
        kind: AgentTaskKind,
        request: AgentTaskRequest,
        execution: AgentTaskExecution,
    ) -> AsyncIterator[str]:
        service = _project_service(catalog, project_id)
        task = store.agent_task(execution.operation_id)
        if task is None or task.project_id != project_id:
            raise ValueError("The agent stream lost its durable project task.")
        if task.graph_target.kind == "branch" and kind != "branch_merge":
            service = service.for_graph_target(
                task.graph_target,
                expected_episode_id=task.graph_target.branch_id,
            )
        if kind == "branch_merge":
            if not isinstance(request, BranchMergeRunRequest):
                raise TypeError("A branch merge task requires its pinned merge request.")
            episode = store.episode(request.episode_id)
            if episode is None or episode.project_id != project_id:
                raise ValueError("The branch merge task lost its Auto-research episode.")
            async with aclosing(
                stream_branch_merge_task(
                    service,
                    launcher,
                    request,
                    app_data,
                    episode=episode,
                    task=task,
                    execution=execution,
                )
            ) as stream:
                async for frame in stream:
                    yield frame
            return
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
        if kind == "episode_report":
            if not isinstance(request, EpisodeReportRunRequest):
                raise TypeError("An episode report task requires its frozen report request.")
            async with aclosing(
                stream_episode_report_run(service, launcher, request, execution)
            ) as stream:
                async for frame in stream:
                    yield frame
            return
        if kind == "auto_research":
            if not isinstance(request, AutoResearchRunRequest):
                raise TypeError("An Auto-research task requires its operational run request.")
            if request.run_on is None:
                raise ValueError("An Auto-research turn has no pinned execution machine.")
            execution_machine = service.manifest.machine_map.get(request.run_on)
            if execution_machine is None:
                raise ValueError(f"unknown execution machine: {request.run_on}")

            def validate_auto_research_patch(context, arguments) -> AutoResearchCommandEffectResult:
                validated = _validate_work_patch_live(
                    service,
                    arguments.patch,
                    run_truth_scope=list(context.request.run_truth_scope or ()),
                    patch_kind="work",
                    control_node_id=None,
                    control_decision_bundle=None,
                    source_operation_id=context.task.operation_id,
                    profile=(
                        "orchestrator" if context.request.role == "orchestrator" else "ordinary"
                    ),
                )
                result = validated.model_dump(mode="json")
                if validated.status == "valid":
                    return AutoResearchCommandEffectResult(result=result)
                diagnostic = (
                    validated.messages[0]
                    if validated.messages
                    else f"Auto-research Patch validation is {validated.status}."
                )
                return AutoResearchCommandEffectResult(
                    status=validated.status,
                    message=diagnostic,
                    result=result,
                )

            def apply_auto_research_patch(context, patch_text, source_effect_id):
                result, failure = _apply_work_patch(
                    service,
                    execution,
                    patch_text,
                    run_truth_scope=list(
                        context.request.run_truth_scope
                        or service.manifest.agent.default_run_truth_scope
                    ),
                    patch_kind="work",
                    profile="orchestrator",
                    source_operation_id=context.task.operation_id,
                    source_effect_id=source_effect_id,
                )
                if failure is not None:
                    return None, failure.message, failure.correctable
                return result, None, False

            effects = auto_research_command_effects(
                store=store,
                background=background_tasks,
                validate=validate_auto_research_patch,
                worker_request_factory=lambda context, arguments, instruction, worker_id: (
                    _auto_research_worker_request(
                        service,
                        context,
                        arguments,
                        instruction,
                        worker_id,
                    )
                ),
                graph_state=service.history.state,
                execution_host=execution_machine.host,
                apply_patch=apply_auto_research_patch,
                on_graph_applied=lambda: evaluate_graph_wake_boundary(
                    project_id,
                    None,
                    graph_target=task.graph_target,
                    source="Auto-research in-turn Apply",
                ),
                on_watcher_ready=lambda ready_project_id: evaluate_graph_wake_boundary(
                    ready_project_id,
                    None,
                    graph_target=task.graph_target,
                    source="Auto-research graph condition",
                ),
                experiment_coordinator=auto_research_experiment_coordinator,
            )
            dispatcher = AutoResearchCommandDispatcher(store, effects)
            stream_auto_research = (
                stream_auto_research_orchestrator_run
                if request.role == "orchestrator"
                else stream_auto_research_worker_run
            )
            async with aclosing(
                stream_auto_research(
                    service,
                    launcher,
                    request,
                    app_data,
                    execution,
                    command_dispatcher=dispatcher,
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

    background_tasks = BackgroundAgentTasks(
        store,
        background_task_stream,
        on_stream_closed=refresh_cached_project_after_stream,
    )
    auto_research_experiment_coordinator = AutoResearchExperimentCoordinator(
        store,
        background_tasks,
        project_service=lambda project_id, episode_id: _project_service(
            catalog,
            project_id,
        ).for_graph_target(
            _episode_for_http(store, catalog, project_id, episode_id).graph_target,
            expected_episode_id=episode_id,
        ),
        operation_lock=experiment_operation_lock,
    )

    def restart_child_worker_request(
        context: AutoResearchCommandContext,
        arguments: SpawnArguments,
        instruction: str,
        worker_id: str,
    ) -> RunRequest:
        service = _project_service(catalog, context.task.project_id).for_graph_target(
            context.episode.graph_target,
            expected_episode_id=context.episode.episode_id,
        )
        return _auto_research_worker_request(
            service,
            context,
            arguments,
            instruction,
            worker_id,
        )

    def restart_child_seat_node_type(
        project_id: str,
        episode_id: str,
        node_id: str,
    ) -> str | None:
        episode = store.episode(episode_id)
        if episode is None or episode.project_id != project_id:
            raise ValueError("Auto-research child admission lost its branch episode.")
        service = _project_service(catalog, project_id).for_graph_target(
            episode.graph_target,
            expected_episode_id=(
                episode.graph_target.branch_id if episode.graph_target.kind == "branch" else None
            ),
        )
        node = service.history.state().nodes.get(node_id)
        return node.type if node is not None else None

    # Reconciliation runs on the 5s watcher poll, so a stuck admission would log
    # a dozen identical warnings a minute. Keep only each active admission's last
    # reason: log transitions (including A -> B -> A) and forget settled entries.
    reported_child_deferrals: dict[str, tuple[str, str]] = {}
    reported_child_deferrals_lock = threading.Lock()

    def reconcile_auto_research_children(episode_id: str | None = None):
        reconciliation = reconcile_pending_auto_research_child_admissions(
            store,
            background_tasks,
            auto_research_experiment_coordinator,
            worker_request_factory=restart_child_worker_request,
            seat_node_type=restart_child_seat_node_type,
            episode_id=episode_id,
        )
        current_deferrals = {
            deferral.admission_id: (deferral.episode_id, deferral.reason)
            for deferral in reconciliation.deferrals
        }
        with reported_child_deferrals_lock:
            if episode_id is None:
                stale_ids = set(reported_child_deferrals) - set(current_deferrals)
            else:
                stale_ids = {
                    admission_id
                    for admission_id, (reported_episode_id, _reason) in (
                        reported_child_deferrals.items()
                    )
                    if reported_episode_id == episode_id and admission_id not in current_deferrals
                }
            for admission_id in stale_ids:
                reported_child_deferrals.pop(admission_id, None)
            changed_deferrals = [
                deferral
                for deferral in reconciliation.deferrals
                if reported_child_deferrals.get(deferral.admission_id)
                != (deferral.episode_id, deferral.reason)
            ]
            for deferral in changed_deferrals:
                reported_child_deferrals[deferral.admission_id] = (
                    deferral.episode_id,
                    deferral.reason,
                )
        for deferral in changed_deferrals:
            logger.warning(
                "Auto-research child admission %s (%s) in episode %s is still unreflected "
                "and blocks finish: %s",
                deferral.admission_id,
                deferral.child_kind,
                deferral.episode_id,
                deferral.reason,
            )
        return reconciliation

    episode_reconciler = EpisodeReconciler(store, background_tasks, logger=logger)
    reconcile_auto_research_wrapup = episode_reconciler.reconcile_auto_research_wrapup
    reconcile_auto_research_episode = episode_reconciler.reconcile_auto_research_episode
    reconcile_auto_research_task = episode_reconciler.reconcile_auto_research_task
    reconcile_auto_research_recovery_pass = episode_reconciler.reconcile_auto_research_recovery_pass
    reconcile_experiment_episode = episode_reconciler.reconcile_experiment_episode

    watcher_delivery = WatcherDelivery(
        store,
        retry=graph_watcher_retry,
        project_service=lambda project_id: _project_service(catalog, project_id),
        graph_project_service=lambda project_id, target: _project_service(
            catalog, project_id
        ).for_graph_target(
            target,
            expected_episode_id=(target.branch_id if target.kind == "branch" else None),
        ),
        generic_request=_generic_watcher_delivery_request,
        experiment_operation_lock=experiment_operation_lock,
        experiment_admission=experiment_admission,
        deliver_auto_research_group=lambda group: deliver_auto_research_watcher_group(
            background_tasks,
            group,
        ),
        preflight_episode_wake=preflight_episode_wake,
        admit_experiment_watcher_invocation=admit_experiment_watcher_invocation,
        experiment_watcher_request=experiment_watcher_delivery_request,
        start_watcher_notification=lambda *args, **kwargs: (
            background_tasks.start_watcher_notification(*args, **kwargs)
        ),
        state_unavailable=lambda exc: isinstance(exc, StateUnavailable),
        task_graph_capable=task_graph_capable,
        task_experiment_episode_id=task_experiment_episode_id,
        reconcile_experiment_episode=episode_reconciler.reconcile_experiment_episode,
        reconcile_graph_boundaries=reconcile_accepted_graph_boundaries,
        ready_graph_groups=lambda candidate_store, project_id: ready_graph_watcher_groups(
            candidate_store,
            project_id,
        ),
        logger=logger,
    )
    deliver_watcher_group = watcher_delivery.deliver_watcher_group
    evaluate_graph_wake_boundary = watcher_delivery.evaluate_graph_wake_boundary
    sweep_graph_conditions_at_startup = watcher_delivery.sweep_graph_conditions_at_startup
    retry_graph_wakes_after_poll = watcher_delivery.retry_graph_wakes_after_poll

    def after_task_settled(
        project_id: str,
        kind: AgentTaskKind,
        request: AgentTaskRequest,
        execution: AgentTaskExecution,
    ) -> None:
        watcher_delivery.evaluate_graph_conditions_after_task(
            project_id,
            kind,
            request,
            execution,
        )
        for episode in store.episodes(project_id):
            if episode.mode == "auto_research":
                reconcile_auto_research_children(episode.episode_id)
                auto_research_experiment_coordinator.reconcile(episode.episode_id)
                reconcile_pending_auto_research_lifecycle(
                    background_tasks,
                    episode_id=episode.episode_id,
                )
                reconcile_pending_auto_research_mail(
                    background_tasks,
                    episode_id=episode.episode_id,
                )

    background_tasks.on_task_settled = after_task_settled

    background_tasks.on_auto_research_task_settled = reconcile_auto_research_task
    background_tasks.on_auto_research_admission_exhausted = lambda episode: (
        reconcile_auto_research_episode(
            episode.episode_id,
            source="budget exhaustion",
            operation_id=episode.root_operation_id,
        )
    )

    graph_watcher_retry_worker = WatcherRetryWorker(retry_graph_wakes_after_poll)

    def after_watcher_poll() -> None:
        graph_watcher_retry_worker.signal()
        reconcile_auto_research_recovery_pass()
        auto_research_episode_ids: list[str] = []
        for project in store.projects():
            for episode in store.episodes(project.project_id):
                if episode.mode == "auto_research":
                    auto_research_episode_ids.append(episode.episode_id)
                    reconcile_auto_research_children(episode.episode_id)
                    auto_research_experiment_coordinator.reconcile(episode.episode_id)
                    reconcile_auto_research_episode(
                        episode.episode_id,
                        source="watcher poll",
                    )
                elif episode.mode == "experiment_loop":
                    reconcile_experiment_episode(
                        episode.episode_id,
                        source="watcher poll",
                    )
        for episode_id in auto_research_episode_ids:
            reconcile_pending_auto_research_lifecycle(
                background_tasks,
                episode_id=episode_id,
            )
            reconcile_pending_auto_research_mail(
                background_tasks,
                episode_id=episode_id,
            )

    watcher_poller = WatcherPoller(
        store,
        on_completed=deliver_watcher_group,
        on_poll_completed=after_watcher_poll,
    )
    services = ApiServices(
        store=store,
        catalog=catalog,
        identity_access=identity_access,
        attachment_store=attachment_store,
        watcher_poller=watcher_poller,
        result_view_keep_locks=result_view_keep_lock,
        project_display_cache=project_display_cache,
        watcher_delivery=watcher_delivery,
        experiment_operation_lock=experiment_operation_lock,
        background_tasks=background_tasks,
        experiment_admission=experiment_admission,
    )

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
            background_tasks.accept_watcher_notifications()
            store.prune_operational_storage()
            await asyncio.to_thread(
                background_tasks.reconcile_reserved_auto_research_roots,
                ensure_auto_research_graph_target,
            )
            await asyncio.to_thread(background_tasks.reconcile_committed_auto_research_dispatches)
            child_reconciliation = await asyncio.to_thread(
                reconcile_auto_research_children,
            )
            if child_reconciliation.cancelled:
                logger.warning(
                    "Cancelled %s unlaunchable Auto-research child admission(s) at startup.",
                    child_reconciliation.cancelled,
                )
            for project in store.projects():
                for episode in store.episodes(project.project_id):
                    if episode.mode == "auto_research":
                        await asyncio.to_thread(
                            auto_research_experiment_coordinator.reconcile,
                            episode.episode_id,
                        )
            orphaned_endings = await asyncio.to_thread(
                reconcile_orphaned_auto_research_failures,
                background_tasks,
            )
            for ending in orphaned_endings:
                await asyncio.to_thread(
                    reconcile_auto_research_wrapup,
                    ending,
                    source="startup failure recovery",
                )
            try:
                await asyncio.to_thread(
                    reconcile_pending_auto_research_lifecycle,
                    background_tasks,
                )
                await asyncio.to_thread(reconcile_pending_auto_research_mail, background_tasks)
            except Exception as exc:
                logger.warning(
                    "Could not reconcile pending Auto-research lifecycle or mail at startup: %s",
                    exc,
                )
            for project in store.projects():
                for episode in store.episodes(project.project_id):
                    if episode.mode == "auto_research":
                        await asyncio.to_thread(
                            reconcile_auto_research_episode,
                            episode.episode_id,
                            source="startup",
                        )
                    elif episode.mode == "experiment_loop":
                        await asyncio.to_thread(
                            reconcile_experiment_episode,
                            episode.episode_id,
                            source="startup",
                        )
            await asyncio.to_thread(reconcile_auto_research_recovery_pass)
            _sweep_stale_stages(app_data / "run-stage", now=time.time())
            attachment_store.sweep()
            cache_roots = [
                *discover_project_cache_roots(app_data),
                legacy_shared_cache_roots(app_data),
            ]
            for source_root, slice_root in cache_roots:
                RebuildableCache(
                    source_root,
                    REMOTE_SOURCE_CACHE_LIMITS,
                    layout="files",
                ).sweep()
                RebuildableCache(
                    slice_root,
                    SESSION_SLICE_CACHE_LIMITS,
                    layout="directories",
                ).sweep()
            # Scheduling happens before the app becomes available, but the task
            # itself cannot run until control returns to the server after yield.
            startup_maintenance.append(asyncio.create_task(warm_provider_capabilities()))
            if default_state_host:
                startup_maintenance.append(asyncio.create_task(sweep_remote_run_stages()))
            await asyncio.to_thread(sweep_graph_conditions_at_startup)
            graph_watcher_retry_worker.start()
            watcher_poller.start()
            yield
        finally:
            for task in startup_maintenance:
                task.cancel()
            for task in list(project_display_cache.reconciliation_tasks.values()):
                task.cancel()
            for task in startup_maintenance:
                with suppress(asyncio.CancelledError):
                    await task
            for task in list(project_display_cache.reconciliation_tasks.values()):
                with suppress(asyncio.CancelledError):
                    await task
            watcher_poller.stop()
            graph_watcher_retry_worker.stop()
            background_tasks.shutdown()
            # A PyInstaller one-file backend runs under a bootloader supervisor
            # whose signal exit can skip the CLI context manager's ``finally``.
            # Source reload workers share metadata owned by the outer supervisor,
            # so they must leave it in place across worker restarts.
            if getattr(sys, "frozen", False):
                remove_server_metadata(app_data, instance_id=identity.instance_id)

    app = FastAPI(title="RCP", version=__version__, lifespan=lifespan)
    app.state.services = services
    app.state.catalog = catalog
    app.state.provider_skills = provider_skills
    app.state.setup = setup
    app.state.default_project_id = default_project_id
    app.state.service = default_service
    app.state.data_dir = app_data
    app.state.background_tasks = background_tasks
    app.state.project_reconciliation_tasks = project_display_cache.reconciliation_tasks
    app.state.watcher_poller = watcher_poller
    app.state.graph_watcher_retry_worker = graph_watcher_retry_worker
    app.state.instance_metadata = identity
    app.state.space_id = space_id
    app.state.space_kind = space_kind
    app.state.launcher = launcher
    app.state.agent_mode = agent_mode
    if space_kind == "team":
        app.add_middleware(TeamPublicAuthBodyLimit)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def require_current_instance(request: Request, call_next):
        project_prefix = "/api/projects/"
        path = request.scope["path"]
        if path.startswith(project_prefix):
            project_id, separator, rest = path[len(project_prefix) :].partition("/")
            canonical_project_id = catalog.resolve_project_id(project_id)
            if canonical_project_id != project_id:
                canonical_path = f"{project_prefix}{canonical_project_id}"
                if separator:
                    canonical_path = f"{canonical_path}/{rest}"
                request.scope["path"] = canonical_path
                path = canonical_path

        public_team_api_paths = {
            "/api/health",
            "/api/team/enroll",
            "/api/team/session/exchange",
        }
        session_ending_paths = {
            "/api/team/session/logout",
            "/api/team/credential/rotate",
            "/api/team/credential/revoke",
        }
        if (
            space_kind == "team"
            and request.method != "OPTIONS"
            and (path == "/api" or path.startswith("/api/"))
            and path not in public_team_api_paths
        ):
            try:
                resolve_team_user(request)
            except HTTPException as exc:
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail},
                    headers=exc.headers,
                )
            if trusted_principal_resolver is None and request.method not in {
                "GET",
                "HEAD",
                "OPTIONS",
            }:
                origin = request.headers.get("origin")
                expected_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
                if origin is not None and origin.rstrip("/") != expected_origin.rstrip("/"):
                    return JSONResponse(
                        status_code=403,
                        content={
                            "detail": {
                                "code": "team_origin_invalid",
                                "message": "The request origin does not match this team server.",
                            }
                        },
                    )
                media_type = request.headers.get("content-type", "").partition(";")[0].strip()
                path_parts = path.split("/")
                attachment_upload = (
                    request.method == "POST"
                    and len(path_parts) == 7
                    and path_parts[1:3] == ["api", "projects"]
                    and path_parts[4] == "chats"
                    and path_parts[6] == "attachments"
                )
                allowed_media_type = media_type.lower() == "application/json" or (
                    attachment_upload and media_type.lower() == "multipart/form-data"
                )
                if not allowed_media_type:
                    return JSONResponse(
                        status_code=415,
                        content={
                            "detail": {
                                "code": "team_json_required",
                                "message": (
                                    "Authenticated team mutations require JSON, except for "
                                    "the bounded attachment upload."
                                ),
                            }
                        },
                    )
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            pinned_instance = request.headers.get("X-RCP-Instance-ID")
            if pinned_instance and pinned_instance != identity.instance_id:
                response = JSONResponse(
                    status_code=409,
                    content={
                        "detail": (
                            "RCP was replaced by another backend instance. Refresh before "
                            "making changes."
                        ),
                        "instance_id": identity.instance_id,
                    },
                )
                session = getattr(request.state, "team_session", None)
                if isinstance(session, str) and path not in session_ending_paths:
                    set_team_session_cookie(response, session)
                return response
        response = await call_next(request)
        session = getattr(request.state, "team_session", None)
        if isinstance(session, str) and path not in session_ending_paths:
            set_team_session_cookie(response, session)
        return response

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

    @app.exception_handler(TeamAuthenticationError)
    async def team_authentication_error(_: Request, exc: TeamAuthenticationError) -> JSONResponse:
        status_by_code = {
            "enrollment_code_invalid": 401,
            "team_token_invalid": 401,
            "enrollment_code_consumed": 409,
            "enrollment_code_expired": 410,
            "enrollment_code_locked": 429,
        }
        return JSONResponse(
            status_code=status_by_code.get(exc.code, 401),
            content={"detail": {"code": exc.code, "message": str(exc)}},
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
            "space_id": space_id,
            "space_kind": space_kind,
            "space_name": store.space_name,
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

    @app.get("/api/identity")
    def get_identity(request: Request) -> dict[str, object]:
        return identity_payload(acting_user(request))

    @app.patch("/api/identity")
    def update_identity(
        request: Request,
        body: SpaceIdentityUpdateRequest,
    ) -> dict[str, object]:
        current = acting_user(request)
        try:
            renamed = store.rename_space_user(current.user_id, body.display_name)
        except KeyError as exc:  # pragma: no cover - resolved and renamed in one local store
            raise HTTPException(
                status_code=403, detail="Acting identity is no longer valid."
            ) from exc
        return identity_payload(renamed)

    @app.post("/api/team/enroll")
    def enroll_team_member(body: TeamEnrollmentRequest) -> dict[str, object]:
        require_team_space()
        member, token = store.enroll_team_member(body.code, body.display_name)
        return {"identity": identity_payload(member), "token": token}

    @app.post("/api/team/session/exchange")
    def exchange_team_session(
        body: TeamSessionExchangeRequest,
        response: Response,
    ) -> dict[str, object]:
        require_team_space()
        session, member = store.create_team_session(body.token)
        set_team_session_cookie(response, session)
        return identity_payload(member)

    @app.post("/api/team/session/logout")
    def logout_team_session(request: Request, response: Response) -> dict[str, bool]:
        require_team_space()
        acting_user(request)
        store.delete_team_session(request.cookies.get(TEAM_SESSION_COOKIE))
        clear_team_session_cookie(response)
        return {"ok": True}

    @app.get("/api/team/invitations")
    def team_invitations(request: Request) -> list[dict[str, object]]:
        require_team_space()
        member = acting_user(request)
        return [
            invitation.model_dump(mode="json")
            for invitation in store.team_invitations(member.user_id)
        ]

    @app.post("/api/team/invitations")
    def create_team_invitation(request: Request) -> dict[str, object]:
        require_team_space()
        member = acting_user(request)
        invitation, code = store.create_team_invitation(member.user_id)
        space_name = store.space_name
        if space_name is None:  # pragma: no cover - named team initialization is required
            raise HTTPException(status_code=500, detail="Team space name is missing.")
        return {
            "invitation": invitation.model_dump(mode="json"),
            "code": code,
            "space_name": space_name,
        }

    @app.post("/api/team/credential/rotate")
    def rotate_team_credential(request: Request, response: Response) -> dict[str, str]:
        require_team_space()
        member = acting_user(request)
        token = store.rotate_team_token(
            member.user_id,
            authenticating_session=authenticating_team_session(request),
        )
        clear_team_session_cookie(response)
        return {"token": token}

    @app.post("/api/team/credential/revoke")
    def revoke_team_credential(request: Request, response: Response) -> dict[str, bool]:
        require_team_space()
        member = acting_user(request)
        store.revoke_team_token(
            member.user_id,
            authenticating_session=authenticating_team_session(request),
        )
        clear_team_session_cookie(response)
        return {"ok": True}

    @app.patch("/api/team/space")
    def update_team_space(
        request: Request,
        body: TeamSpaceUpdateRequest,
    ) -> dict[str, str]:
        require_team_space()
        acting_user(request)
        return {"space_name": store.rename_space(body.name)}

    # S101. Every project-scoped route hangs off this one router, so membership
    # is declared once instead of remembered 36 times. A route added outside it
    # is caught by test_project_membership's route enumeration, not by review.
    projects_router = APIRouter(dependencies=[Depends(require_project_membership)])
    # Exposed so the route-enumeration test can prove membership is attached,
    # rather than trusting that every project route was declared in one place.
    app.state.project_membership_dependency = require_project_membership

    @app.get("/api/projects")
    def projects(request: Request) -> list[dict[str, object]]:
        visible = store.member_project_ids(acting_user(request).user_id)
        return [card for card in catalog.cards() if card["id"] in visible]

    @app.get("/api/episodes")
    def experiment_episodes(
        request: Request,
        mode: Literal["experiment_loop"] = Query(...),
    ) -> list[dict[str, object]]:
        # An unfiltered answer would publish research and not just project names.
        # Start from durable loop parents rather than graph nodes: a branch may
        # create an Experiment that does not exist on main at all.
        visible = store.member_project_ids(acting_user(request).user_id)
        entries: list[dict[str, object]] = []
        for record in store.projects():
            if record.project_id not in visible:
                continue
            runtimes = store.project_experiment_loop_runtimes(record.project_id)
            if not runtimes:
                continue
            settle_ids = [
                experiment_id
                for experiment_id, runtime in runtimes.items()
                if runtime.stop_requested and not runtime.stop_settled and not runtime.task_active
            ]
            for experiment_id in settle_ids:
                runtime = runtimes[experiment_id]
                episode = (
                    store.episode(runtime.episode_id) if runtime.episode_id is not None else None
                )
                if episode is not None:
                    store.settle_experiment_loop_stop(
                        record.project_id,
                        experiment_id,
                        episode_id=episode.episode_id,
                        graph_target=episode.graph_target,
                    )
            if settle_ids:
                runtimes.update(store.experiment_loop_runtimes(record.project_id, settle_ids))

            current: list[tuple[EpisodeRecord, ExperimentLoopRuntime]] = []
            for control_node_id, runtime in runtimes.items():
                if runtime.episode_id is None:
                    continue
                episode = store.episode(runtime.episode_id)
                if (
                    episode is None
                    or episode.project_id != record.project_id
                    or episode.mode != mode
                    or episode.control_node_id != control_node_id
                ):
                    raise ValueError(
                        "Experiment runtime does not identify its exact durable episode."
                    )
                current.append((episode, runtime))
            current.sort(
                key=lambda item: (item[0].created_at, item[0].episode_id),
                reverse=True,
            )

            cache_status, cached = catalog.cached_snapshot_status(record.project_id)
            if cache_status == "invalid" or (
                cache_status == "missing" and record.revision is not None
            ):
                raise HTTPException(
                    status_code=503,
                    detail=f"Cached project snapshot is unavailable for {record.project_id}.",
                )
            reachable = _cached_project_reachable(cached)
            if record.reachable is False:
                reachable = False

            grouped: dict[str, list[tuple[EpisodeRecord, ExperimentLoopRuntime]]] = {}
            for episode, runtime in current:
                grouped.setdefault(episode.graph_target.key, []).append((episode, runtime))

            main_service: ProjectService | None = None
            for group in grouped.values():
                target = group[0][0].graph_target
                graph_head: GraphHeadRef | None
                if target.kind == "main":
                    # ProjectDisplayCache is deliberately a main-only display snapshot.
                    # Preserve its graph/runtime publication fence for ordinary loops;
                    # branch state is always read through its exact history service.
                    state = _cached_graph_state(cached)
                    if state is None:
                        continue
                    graph_head = None
                else:
                    if main_service is None:
                        main_service = _project_service(catalog, record.project_id)
                    try:
                        target_service = (
                            main_service
                            if target.kind == "main"
                            else main_service.for_graph_target(
                                target,
                                expected_episode_id=target.branch_id,
                            )
                        )
                        materialization = target_service.history.current_materialization()
                        state = materialization.state
                        graph_head = target_service.history.head_ref(materialization)
                    except (KeyError, OSError, StateUnavailable, ValueError) as exc:
                        raise HTTPException(status_code=503, detail=str(exc)) from exc
                    if graph_head.target != target:
                        raise ValueError(
                            "Experiment graph projection returned a different target head."
                        )

                for episode, runtime in group:
                    node_id = episode.control_node_id
                    assert node_id is not None
                    node = state.nodes.get(node_id)
                    if not isinstance(node, Experiment):
                        continue
                    route = store.auto_research_child_experiment(episode.episode_id)
                    parent_episode_id = (
                        route.auto_research_episode_id if route is not None else None
                    )
                    if target.kind == "branch" and parent_episode_id != target.branch_id:
                        raise ValueError(
                            "Branch-target Experiment lost its Auto-research parent identity."
                        )
                    serialized_episode = serialize_episode(
                        store,
                        record.project_id,
                        episode,
                        branch_summary=graph_branch_summary,
                    ).model_dump(mode="json")
                    control = _experiment_control_from_runtime(
                        state,
                        node.id,
                        runtime,
                    ).model_dump(mode="json")
                    control["episode"] = serialized_episode
                    entries.append(
                        {
                            "project_id": record.project_id,
                            "project_name": record.name,
                            "project_reachable": reachable,
                            "graph_target": target.model_dump(mode="json"),
                            "graph_head": (
                                graph_head.model_dump(mode="json")
                                if graph_head is not None
                                else None
                            ),
                            "parent_episode_id": parent_episode_id,
                            "node": node.model_dump(mode="json"),
                            "control": control,
                            "episode": serialized_episode,
                        }
                    )
        return entries

    @app.get("/api/space/users")
    def space_users(request: Request) -> list[dict[str, object]]:
        """Who is enrolled in this space, so Invite can offer them by name.

        Names are not unique, so the control resolves to the durable id.
        """

        acting_user(request)
        return [
            {"user_id": user.user_id, "display_name": user.display_name}
            for user in store.space_users()
        ]

    # S122. Deliberately *outside* the membership router: you are not a member
    # of the project you are being invited to, and Inbox lives inside the
    # project shell, which is unreachable before membership.
    @app.get("/api/project-invitations")
    def project_invitations_for_me(request: Request) -> list[dict[str, object]]:
        user = acting_user(request)
        names = {item.user_id: item.display_name for item in store.space_users()}
        entries = []
        for invitation in store.pending_project_invitations(user.user_id):
            record = store.project(invitation.project_id)
            if record is None:
                continue
            entries.append(
                {
                    "invitation_id": invitation.invitation_id,
                    "project_id": invitation.project_id,
                    "project_name": record.name,
                    "space_name": store.space_name,
                    "invited_by": invitation.invited_by,
                    "invited_by_name": names.get(invitation.invited_by),
                    "created_at": invitation.created_at,
                }
            )
        return entries

    @app.post("/api/project-invitations/{invitation_id}/{response}")
    def answer_project_invitation(
        invitation_id: str,
        response: Literal["accept", "decline"],
        request: Request,
    ) -> dict[str, object]:
        user = acting_user(request)
        try:
            answered = store.answer_project_invitation(
                invitation_id,
                invited_user_id=user.user_id,
                response="accepted" if response == "accept" else "declined",
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Invitation not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return answered.model_dump(mode="json")

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
    def register_project(body: ProjectRegisterRequest, request: Request) -> dict[str, object]:
        # Deliberately not require_patch_capable_identity: creating a project
        # does not demand a display name, and S01/S112/S116 rely on that.
        try:
            record = catalog.register(body.locator, seat_member=acting_user(request).user_id)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return catalog.card(record.project_id)

    @projects_router.delete("/api/projects/{project_id}")
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
    def create_project(body: ProjectSetupRequest, request: Request) -> dict[str, object]:
        try:
            return setup.create(body, seat_member=acting_user(request).user_id)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @projects_router.get("/api/projects/{project_id}")
    async def project(project_id: str) -> dict[str, object]:
        cached = project_display_cache.cached_project_snapshot(project_id)
        if cached is not None:
            return cached
        try:
            generation = catalog.reserve_cached_snapshot_generation(project_id)
            service, snapshot = await asyncio.to_thread(
                project_display_cache.open_snapshot, project_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        except (FileNotFoundError, OSError, ValueError, StateUnavailable) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            committed = catalog.commit_cached_snapshot(
                project_id,
                snapshot,
                generation=generation,
                patch_log_head=service.history.workspace.cached_patch_log_head(),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Could not update display snapshot for %s: %s", project_id, exc)
        else:
            if not committed:
                latest = project_display_cache.cached_project_snapshot(project_id)
                if latest is not None:
                    return latest
        return snapshot

    @projects_router.get("/api/projects/{project_id}/members")
    def project_members(project_id: str) -> list[dict[str, object]]:
        canonical = catalog.resolve_project_id(project_id)
        users = {user.user_id: user for user in store.space_users()}
        return [
            {
                "user_id": record.user_id,
                "display_name": (
                    users[record.user_id].display_name if record.user_id in users else None
                ),
                "seated_at": record.seated_at,
            }
            for record in store.project_members(canonical)
        ]

    @projects_router.post("/api/projects/{project_id}/invitations", status_code=201)
    def invite_project_member(
        project_id: str,
        body: ProjectInviteRequest,
        request: Request,
    ) -> dict[str, object]:
        canonical = catalog.resolve_project_id(project_id)
        # The server derives the inviter from the session; the body names only
        # who is being invited.
        inviter = acting_user(request)
        try:
            invitation = store.invite_to_project(
                canonical,
                body.user_id,
                invited_by=inviter.user_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return invitation.model_dump(mode="json")

    @projects_router.post("/api/projects/{project_id}/leave", status_code=204)
    def leave_project(project_id: str, request: Request) -> Response:
        canonical = catalog.resolve_project_id(project_id)
        leaving = acting_user(request)
        try:
            store.leave_project(canonical, leaving.user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        fence_episodes_for_departed_member(store, background_tasks, canonical, leaving.user_id)
        return Response(status_code=204)

    @projects_router.get("/api/projects/{project_id}/cached")
    def cached_project(project_id: str) -> dict[str, object]:
        snapshot = project_display_cache.cached_project_snapshot(project_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Cached project snapshot not found")
        return snapshot

    @projects_router.get("/api/projects/{project_id}/cached/revision")
    async def cached_project_revision(project_id: str) -> dict[str, object]:
        snapshot = project_display_cache.cached_project_snapshot(project_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Cached project snapshot not found")
        schedule_project_reconciliation(project_id)
        return {
            "revision": snapshot["revision"],
            "snapshot_freshness": snapshot["snapshot_freshness"],
            "last_remote_sync_at": snapshot["last_remote_sync_at"],
        }

    @projects_router.get("/api/projects/{project_id}/readiness")
    def project_readiness(project_id: str, refresh: bool = False) -> dict[str, object]:
        try:
            return catalog.readiness_snapshot(project_id, refresh=refresh)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @projects_router.get("/api/projects/{project_id}/graph")
    def graph(project_id: str) -> dict[str, object]:
        return _project_service(catalog, project_id).graph_snapshot()

    @projects_router.get("/api/projects/{project_id}/revision")
    def project_revision(project_id: str) -> dict[str, int]:
        service = _project_service(catalog, project_id)
        return {"revision": service.history.current_accepted_revision()}

    @projects_router.get("/api/projects/{project_id}/repositories/files/preview")
    @projects_router.head("/api/projects/{project_id}/repositories/files/preview")
    def preview_repository_file(
        project_id: str,
        request: Request,
        path: str = Query(min_length=1),
        line: int | None = Query(default=None, ge=1),
    ) -> Response:
        record = store.project(project_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Project not found")
        try:
            manifest = load_manifest(record.locator)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            source = load_repository_source_for_path(manifest, path)
            document = repository_source_document(source, line=line)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return Response(
            b"" if request.method == "HEAD" else document,
            media_type="text/html",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": REPOSITORY_PREVIEW_CSP,
                "X-Content-Type-Options": "nosniff",
            },
        )

    @projects_router.put("/api/projects/{project_id}/settings")
    def update_project_settings(
        project_id: str,
        body: ProjectSettingsRequest,
    ) -> dict[str, object]:
        try:
            snapshot = project_display_cache.update_settings(project_id, body)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return snapshot

    @projects_router.post(
        "/api/projects/{project_id}/machines/{machine_alias}/providers/{provider}/resolve"
    )
    def resolve_project_provider_path(
        project_id: str,
        machine_alias: str,
        provider: str,
    ) -> dict[str, object]:
        try:
            profile_for(provider)
            result = project_display_cache.resolve_provider_path(
                project_id,
                machine_alias,
                provider,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return result

    @projects_router.get("/api/projects/{project_id}/sources")
    def sources(project_id: str, refresh: bool = False):
        service = _project_service(catalog, project_id)
        return service.index_snapshot(refresh=refresh).model_dump(mode="json")

    @projects_router.delete("/api/projects/{project_id}/caches")
    def clear_rebuildable_caches(project_id: str):
        service = _project_service(catalog, project_id)
        if store.has_active_agent_task(project_id):
            raise HTTPException(
                status_code=409,
                detail="This project's cache cannot be cleared while its agent task is active.",
            )
        return service.clear_rebuildable_caches()

    @app.delete("/api/caches")
    def clear_all_rebuildable_caches(project_id: str) -> dict[str, object]:
        if store.has_any_active_agent_task():
            raise HTTPException(
                status_code=409,
                detail="All project caches cannot be cleared while any agent task is active.",
            )
        current_service = _project_service(catalog, project_id)

        project_roots = discover_project_cache_roots(app_data)
        for source_root, slice_root in project_roots:
            RebuildableCache(
                source_root,
                REMOTE_SOURCE_CACHE_LIMITS,
                layout="files",
            ).clear()
            RebuildableCache(
                slice_root,
                SESSION_SLICE_CACHE_LIMITS,
                layout="directories",
            ).clear()
        for record in store.projects():
            service = catalog.loaded_service(record.project_id)
            if service is not None:
                service.invalidate_source_index()

        legacy_source_root, legacy_slice_root = legacy_shared_cache_roots(app_data)
        RebuildableCache(
            legacy_source_root,
            REMOTE_SOURCE_CACHE_LIMITS,
            layout="files",
        ).clear()
        RebuildableCache(
            legacy_slice_root,
            SESSION_SLICE_CACHE_LIMITS,
            layout="directories",
        ).clear()
        return current_service.indexer.cache_metrics().model_dump(mode="json")

    @projects_router.get("/api/projects/{project_id}/usage", response_model=AgentUsageSnapshot)
    def agent_usage(project_id: str) -> AgentUsageSnapshot:
        _require_registered_project(catalog, project_id)
        return store.agent_usage_snapshot(project_id)

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

    app.include_router(projects_router)
    app.include_router(episode_router)
    app.include_router(experiments_router)
    app.include_router(chats_router)
    app.include_router(history_router)
    app.include_router(paper_router)
    app.include_router(result_views_router)
    app.include_router(sync_router)
    app.include_router(tasks_router)
    app.include_router(watchers_router)

    web_dist = web_dist_path()
    if web_dist.exists():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")

    return app


def _generic_watcher_delivery_request(group: list[StoredWatcherRecord]) -> RunRequest:
    first = group[0]
    continuation = first.continuation
    if continuation.patch_kind != "work":
        raise ValueError("A generic watcher cannot carry Experiment-loop authority.")
    watcher_ids = [item.watcher_id for item in group]
    details = "\n".join(
        (
            f"- graph condition `{item.watcher_id}`: `{item.condition.model_dump_json()}`"
            if isinstance(item, GraphWatcherRecord)
            else f"- external watcher `{item.watcher_id}`: `{item.log_path}`"
        )
        for item in group
    )
    return RunRequest(
        provider=continuation.provider,
        model=continuation.model,
        reasoning=continuation.reasoning,
        run_on=continuation.run_on,
        run_truth_scope=continuation.run_truth_scope,
        chat_scope="node" if first.origin_task_kind == "node_chat" else "project",
        node_id=first.node_id,
        message=(
            "RCP watcher update: the following external or canonical graph conditions are "
            f"ready. Inspect their current authoritative state and continue the Work turn.\n{details}"
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


def _cached_graph_state(snapshot: dict[str, object] | None) -> GraphState | None:
    if snapshot is None:
        return None
    try:
        return GraphState.model_validate(snapshot["graph"])
    except (KeyError, TypeError, ValueError):
        return None


def _cached_project_reachable(snapshot: dict[str, object] | None) -> bool | None:
    if snapshot is None:
        return None
    canonical = snapshot.get("canonical_state")
    if not isinstance(canonical, dict):
        return None
    reachable = canonical.get("reachable")
    return reachable if isinstance(reachable, bool) else None


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


def _auto_research_worker_request(
    service: ProjectService,
    _context: AutoResearchCommandContext,
    arguments: SpawnArguments,
    instruction: str,
    worker_id: str,
) -> RunRequest:
    """Resolve a spawned child through the current ordinary node-Work profile."""

    return _resolved_graph_request(
        service,
        "node_chat",
        RunRequest(
            chat_id=worker_id,
            chat_scope="node",
            node_id=arguments.seat_node_id,
            message=instruction,
            mode="work",
            trigger="orchestrator",
            patch_kind="work",
        ),
    )


def default_data_dir() -> Path:
    override = os.environ.get("RCP_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "research-control-panel"
