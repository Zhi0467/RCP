from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import aclosing, asynccontextmanager, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, cast
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rcp import __version__
from rcp.agents import AcceptanceAgentLauncher, AgentLauncher, ProviderReadiness
from rcp.agents.command_protocol import SpawnArguments
from rcp.api.campaigns import (
    CampaignMessageBody,
    CampaignResponse,
    ReauthorizeCampaignBody,
    StartCampaignBody,
    campaign_for_project,
    serialize_campaign,
    serialize_campaigns,
)
from rcp.artifacts import (
    ARTIFACT_MEDIA_TYPES,
    AgentArtifactDescriptor,
    ResultViewDescriptor,
    descriptor_for,
    html_preview_document,
    read_local_regular_file,
    validate_artifact_bytes,
)
from rcp.attachments import ChatAttachmentStore, ChatAttachmentUpload
from rcp.background import (
    AgentTaskExecution,
    AgentTaskRequest,
    BackgroundAgentTasks,
)
from rcp.config import AgentSurface, load_manifest
from rcp.control import (
    ExperimentControlState,
    ExperimentOperationalState,
    ExperimentSessionBinding,
    admit_experiment_watcher_invocation,
    derive_experiment_control_state,
)
from rcp.core.models import (
    DISPLAY_NAME_MAX_LENGTH,
    AuthorizedHuman,
    Experiment,
    ExperimentDecisionPin,
    GraphState,
    normalize_display_name,
)
from rcp.history import PatchRejected, ReplayHalted
from rcp.limits import (
    CHAT_ARTIFACT_MAX_FILE_BYTES,
    CHAT_PAGE_DEFAULT_LIMIT,
    CHAT_PAGE_MAX_LIMIT,
    REMOTE_STATE_HEAD_PROBE_INTERVAL_SECONDS,
    TEAM_ENROLLMENT_CODE_MAX_LENGTH,
    TEAM_MEMBER_TOKEN_MAX_LENGTH,
    TEAM_PUBLIC_AUTH_REQUEST_MAX_BYTES,
    TEAM_SESSION_IDLE_DAYS,
    WATCHER_POLL_INTERVAL_SECONDS,
)
from rcp.paper import PaperSnapshot
from rcp.projects import ProjectCatalog
from rcp.provider_skills import ProviderSkillInventoryManager
from rcp.providers import PROVIDER_IDS, profile_for
from rcp.repository_preview import (
    REPOSITORY_PREVIEW_CSP,
    load_repository_source_for_path,
    repository_source_document,
)
from rcp.runs.campaign import (
    CampaignCommandContext,
    CampaignCommandDispatcher,
    CampaignCommandEffectResult,
    CampaignRunRequest,
    CampaignStartRequest,
)
from rcp.runs.campaign_delivery import (
    deliver_campaign_watcher_group,
    deliver_pending_campaign_mail,
    reconcile_pending_campaign_mail,
    record_campaign_message,
)
from rcp.runs.campaign_effects import campaign_command_effects
from rcp.runs.campaign_recovery import (
    reconcile_campaign_task_settlement,
    reconcile_due_campaign_recoveries,
    reconcile_orphaned_campaign_failures,
    schedule_report_reconciliation,
)
from rcp.runs.campaign_stream import (
    stream_campaign_orchestrator_run,
    stream_campaign_report_run,
    stream_campaign_worker_run,
)
from rcp.runs.chat import _logical_chat_turn_operation_id
from rcp.runs.coach import _resolved_coach_request, stream_coach
from rcp.runs.discuss import stream_discuss_run
from rcp.runs.experiment_loop import experiment_watcher_delivery_request, preflight_episode_wake
from rcp.runs.graph import stream_graph_run
from rcp.runs.result_views import read_local_result_view_bytes
from rcp.runs.shared import _sweep_stale_stages
from rcp.runs.work import _validate_work_patch_live, stream_work_run
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
    discover_project_cache_roots,
    legacy_shared_cache_roots,
)
from rcp.storage import (
    SPACE_NAME_MAX_LENGTH,
    AgentTaskKind,
    AgentUsageSnapshot,
    AppStore,
    CampaignMessageRecord,
    CampaignNotRunning,
    CampaignRecord,
    ExperimentLoopRuntime,
    GraphWatcherRecord,
    ResultViewConflict,
    ResultViewRecord,
    SpaceUserRecord,
    StoredWatcherRecord,
    TeamAuthenticationError,
    WatcherClaimConflict,
    normalize_space_name,
)
from rcp.transport import RemoteRunStage, StateUnavailable
from rcp.watchers import (
    WatcherPoller,
    WatcherRetryGeneration,
    WatcherRetryWorker,
    evaluate_graph_watchers,
    ready_graph_watcher_groups,
)
from rcp.web_assets import web_dist_path

logger = logging.getLogger(__name__)

TEAM_SESSION_COOKIE = "__Host-rcp_session"
TEAM_SESSION_COOKIE_MAX_AGE = TEAM_SESSION_IDLE_DAYS * 24 * 60 * 60


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


class PaperSaveRequest(BaseModel):
    content: str
    base_hash: str | None = None


class ProjectRegisterRequest(BaseModel):
    locator: str


class RetryAgentTaskRequest(BaseModel):
    provider: str | None = None
    model: str | None = None
    reasoning: str | None = None
    run_on: str | None = None


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


TrustedPrincipalResolver = Callable[[Request, AppStore], SpaceUserRecord | str]


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
    launcher = AcceptanceAgentLauncher() if acceptance_agent else AgentLauncher()
    agent_mode: Literal["acceptance", "provider"] = "acceptance" if acceptance_agent else "provider"
    provider_skills = ProviderSkillInventoryManager(store)
    catalog = ProjectCatalog(app_data, store, launcher, provider_skills)
    setup = ProjectSetupManager(app_data, catalog, launcher)
    attachment_store = ChatAttachmentStore(app_data / "chat-attachments")
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
    experiment_operation_locks: dict[str, threading.RLock] = {}
    experiment_operation_locks_guard = threading.Lock()
    result_view_keep_locks: dict[str, threading.Lock] = {}
    result_view_keep_locks_guard = threading.Lock()
    graph_watcher_reconciliation_locks: dict[str, threading.Lock] = {}
    graph_watcher_reconciliation_locks_guard = threading.Lock()
    graph_watcher_retry_failures: dict[str, int] = {}
    graph_watcher_retry_passes: dict[str, int] = {}
    graph_watcher_retry_guard = threading.Lock()
    project_reconciliation_tasks: dict[str, asyncio.Task[None]] = {}
    project_probe_started_at: dict[str, float] = {}

    def experiment_operation_lock(project_id: str) -> threading.RLock:
        with experiment_operation_locks_guard:
            return experiment_operation_locks.setdefault(project_id, threading.RLock())

    def result_view_keep_lock(view_id: str) -> threading.Lock:
        with result_view_keep_locks_guard:
            return result_view_keep_locks.setdefault(view_id, threading.Lock())

    def graph_watcher_reconciliation_lock(project_id: str) -> threading.Lock:
        with graph_watcher_reconciliation_locks_guard:
            return graph_watcher_reconciliation_locks.setdefault(project_id, threading.Lock())

    def schedule_graph_watcher_reconciliation(project_id: str) -> None:
        """Retry transient reconciliation failures with capped poll-pass backoff."""

        max_passes = max(1, 60 // WATCHER_POLL_INTERVAL_SECONDS)
        with graph_watcher_retry_guard:
            failures = graph_watcher_retry_failures.get(project_id, 0) + 1
            graph_watcher_retry_failures[project_id] = failures
            graph_watcher_retry_passes[project_id] = min(
                2 ** min(failures - 1, 6),
                max_passes,
            )

    def clear_graph_watcher_reconciliation_retry(project_id: str) -> None:
        with graph_watcher_retry_guard:
            graph_watcher_retry_failures.pop(project_id, None)
            graph_watcher_retry_passes.pop(project_id, None)

    def due_graph_watcher_reconciliations() -> list[str]:
        due: list[str] = []
        with graph_watcher_retry_guard:
            for project_id, passes in list(graph_watcher_retry_passes.items()):
                if passes <= 1:
                    # Keep due work durable in process until its reconciliation
                    # explicitly succeeds or reaches a non-retryable outcome. A
                    # retry generation can be invalidated after this selection.
                    graph_watcher_retry_passes[project_id] = 0
                    due.append(project_id)
                else:
                    graph_watcher_retry_passes[project_id] = passes - 1
        return sorted(due)

    def retry_generation_is_current(
        generation: WatcherRetryGeneration | None,
    ) -> bool:
        return generation is None or generation.is_current()

    def run_for_retry_generation(
        generation: WatcherRetryGeneration | None,
        callback: Callable[[], None],
    ) -> bool:
        if generation is None:
            callback()
            return True
        return generation.run_if_current(callback)

    class _GraphWatcherReplayDegraded(RuntimeError):
        pass

    def require_team_space() -> None:
        if space_kind != "team":
            raise HTTPException(status_code=404, detail="Team authentication is unavailable.")

    def set_team_session_cookie(response: Response, session: str) -> None:
        response.set_cookie(
            TEAM_SESSION_COOKIE,
            session,
            max_age=TEAM_SESSION_COOKIE_MAX_AGE,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )

    def clear_team_session_cookie(response: Response) -> None:
        response.delete_cookie(
            TEAM_SESSION_COOKIE,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )

    def resolve_team_user(request: Request) -> SpaceUserRecord:
        cached = getattr(request.state, "team_member", None)
        if isinstance(cached, SpaceUserRecord):
            return cached

        if trusted_principal_resolver is None:
            session = request.cookies.get(TEAM_SESSION_COOKIE)
            member = store.resolve_team_session(session)
            if member is None:
                raise HTTPException(
                    status_code=401,
                    detail={
                        "code": "team_identity_required",
                        "message": "This team action requires a trusted authenticated member.",
                    },
                )
            request.state.team_session = session
        else:
            resolved = trusted_principal_resolver(request, store)
            user_id = resolved.user_id if isinstance(resolved, SpaceUserRecord) else resolved
            if not isinstance(user_id, str):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "team_identity_invalid",
                        "message": "The trusted team identity is invalid for this space.",
                    },
                )
            member = store.space_user(user_id)

        if member is None or member.identity_kind != "team_member":
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "team_identity_invalid",
                    "message": "The trusted team identity is invalid for this space.",
                },
            )
        request.state.team_member = member
        return member

    def authenticating_team_session(request: Request) -> str | None:
        if trusted_principal_resolver is not None:
            return None
        session = getattr(request.state, "team_session", None)
        if not isinstance(session, str):  # pragma: no cover - middleware resolves this first
            raise HTTPException(status_code=401, detail="The browser session is unavailable.")
        return session

    def acting_user(request: Request) -> SpaceUserRecord:
        if space_kind == "personal":
            owner = store.local_owner
            if owner is None:  # pragma: no cover - guarded by the storage invariant
                raise HTTPException(status_code=500, detail="Personal owner identity is missing.")
            return owner
        return resolve_team_user(request)

    def require_patch_capable_identity(request: Request) -> AuthorizedHuman:
        user = acting_user(request)
        if user.display_name is None or not user.display_name.strip():
            raise HTTPException(
                status_code=428,
                detail={
                    "code": "identity_name_required",
                    "message": (
                        "Choose an RCP display name before this action. The name will be "
                        "copied into permanent project history as a snapshot."
                    ),
                },
            )
        return AuthorizedHuman(
            space_id=space_id,
            user_id=user.user_id,
            display_name=user.display_name,
        )

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
        if kind == "campaign":
            if not isinstance(request, CampaignRunRequest):
                raise TypeError("A campaign task requires a campaign run request.")
            if request.role == "report":
                async with aclosing(
                    stream_campaign_report_run(
                        service,
                        launcher,
                        request,
                        app_data,
                        execution,
                    )
                ) as stream:
                    async for frame in stream:
                        yield frame
                return
            if request.run_on is None:
                raise ValueError("A campaign turn has no pinned execution machine.")
            execution_machine = service.manifest.machine_map.get(request.run_on)
            if execution_machine is None:
                raise ValueError(f"unknown execution machine: {request.run_on}")

            def validate_campaign_patch(context, arguments) -> CampaignCommandEffectResult:
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
                    return CampaignCommandEffectResult(result=result)
                diagnostic = (
                    validated.messages[0]
                    if validated.messages
                    else f"Campaign Patch validation is {validated.status}."
                )
                return CampaignCommandEffectResult(
                    status=validated.status,
                    message=diagnostic,
                    result=result,
                )

            effects = campaign_command_effects(
                store=store,
                background=background_tasks,
                validate=validate_campaign_patch,
                worker_request_factory=_campaign_worker_request,
                graph_state=service.history.state,
                execution_host=execution_machine.host,
                on_watcher_ready=lambda ready_project_id: evaluate_graph_wake_boundary(
                    ready_project_id,
                    None,
                    source="campaign graph condition",
                ),
            )
            dispatcher = CampaignCommandDispatcher(store, effects)
            stream_campaign = (
                stream_campaign_orchestrator_run
                if request.role == "orchestrator"
                else stream_campaign_worker_run
            )
            async with aclosing(
                stream_campaign(
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

    def refresh_cached_project_after_stream(
        project_id: str,
        kind: AgentTaskKind,
        request: AgentTaskRequest,
        execution: AgentTaskExecution,
    ) -> None:
        graph_capable = (
            isinstance(request, RunRequest)
            and (
                kind in {"seed", "refresh"}
                or (kind in {"node_chat", "project_chat"} and request.mode == "work")
            )
        ) or (
            kind == "campaign"
            and isinstance(request, CampaignRunRequest)
            and request.role in {"orchestrator", "worker"}
        )
        if not graph_capable:
            return
        try:
            service = catalog.loaded_service(project_id)
            if service is None:
                raise RuntimeError("The closed stream's project service is no longer loaded.")
            generation = catalog.reserve_cached_snapshot_generation(project_id)
            cache_status, cached = catalog.cached_snapshot_status(project_id)
            if cache_status == "missing":
                record = store.project(project_id)
                if record is None or record.revision is None:
                    return
                raise ValueError("The expected project display snapshot is missing.")
            if cached is None:
                raise ValueError("The existing project display snapshot is invalid.")
            state = service.history.materialize(write_outputs=False).state
            paper = PaperSnapshot.model_validate(cached["paper"])
            snapshot = service.project_snapshot(state=state, paper=paper)
            snapshot["id"] = project_id
            catalog.mark_snapshot_fresh(snapshot)
            attach_experiment_control(project_id, snapshot)
            catalog.commit_cached_snapshot(
                project_id,
                snapshot,
                generation=generation,
                patch_log_head=service.history.workspace.cached_patch_log_head(),
            )
        except Exception as exc:
            logger.warning(
                "Could not refresh display snapshot after task %s for project %s: %s",
                execution.operation_id,
                project_id,
                exc,
            )
            try:
                store.record_agent_task_receipt(
                    execution.operation_id,
                    "display_cache_refresh_failed",
                    {"exception_type": type(exc).__name__, "detail": str(exc)},
                    tier="diagnostic",
                )
            except Exception as receipt_exc:
                logger.warning(
                    "Could not record display cache refresh failure for task %s: %s",
                    execution.operation_id,
                    receipt_exc,
                )

    def evaluate_graph_conditions_after_task(
        project_id: str,
        kind: AgentTaskKind,
        request: AgentTaskRequest,
        execution: AgentTaskExecution,
    ) -> None:
        graph_capable = (
            isinstance(request, RunRequest)
            and (
                kind in {"seed", "refresh"}
                or (kind in {"node_chat", "project_chat"} and request.mode == "work")
            )
        ) or (
            kind == "campaign"
            and isinstance(request, CampaignRunRequest)
            and request.role in {"orchestrator", "worker"}
        )
        if not graph_capable:
            deliver_ready_graph_wake_groups(project_id, source="task settlement")
            return
        if execution.applied_revision is None and not execution.armed_graph_watchers:
            deliver_ready_graph_wake_groups(project_id, source="task settlement")
            return
        evaluate_graph_wake_boundary(
            project_id,
            execution.applied_graph_state,
            source="agent patch" if execution.applied_revision is not None else "watcher arming",
        )

    background_tasks = BackgroundAgentTasks(
        store,
        background_task_stream,
        on_stream_closed=refresh_cached_project_after_stream,
        on_task_settled=evaluate_graph_conditions_after_task,
    )

    def campaign_report_request(campaign: CampaignRecord) -> CampaignRunRequest:
        if campaign.ending is None:
            raise ValueError("A campaign report request requires a durable ending.")
        return CampaignRunRequest(
            campaign_id=campaign.campaign_id,
            role="report",
            ending=campaign.ending,
            instruction=(
                "Produce the campaign's concluding report from the staged official "
                "campaign-report skill."
            ),
        )

    def reconcile_campaign_report(
        campaign: CampaignRecord,
        *,
        source: str,
        operation_id: str | None = None,
    ) -> bool:
        try:
            report_task = background_tasks.reconcile_campaign_report(
                campaign.campaign_id,
                request_factory=campaign_report_request,
            )
            if report_task is not None:
                return True
            current = store.campaign(campaign.campaign_id)
            if current is None or current.status != "wrapping_up":
                return True
            return any(
                store.campaign_invocation_role(task.operation_id) == "report"
                and CampaignRunRequest.model_validate(task.request).ending == current.ending
                for task in store.campaign_tasks(campaign.campaign_id)
            )
        except Exception as exc:
            logger.warning(
                "Could not reconcile campaign report for %s after %s: %s",
                campaign.campaign_id,
                source,
                exc,
            )
            if operation_id is not None:
                with suppress(Exception):
                    store.record_agent_task_receipt(
                        operation_id,
                        "campaign_report_reconciliation_failed",
                        {
                            "campaign_id": campaign.campaign_id,
                            "source": source,
                            "exception_type": type(exc).__name__,
                            "detail": str(exc),
                        },
                        tier="diagnostic",
                    )
            with suppress(Exception):
                schedule_report_reconciliation(
                    background_tasks,
                    campaign,
                    diagnostic=str(exc),
                )
            return False

    def reconcile_campaign_reports_at_startup() -> None:
        for campaign in store.campaigns_awaiting_report():
            reconcile_campaign_report(campaign, source="startup")

    def fence_depleted_campaigns_at_startup() -> None:
        """Fence the one live campaign per project after crash recovery is reconstructed."""

        for project in store.projects():
            campaign = store.active_campaign(project.project_id)
            if campaign is None:
                continue
            store.fence_campaign_exhaustion_if_depleted(campaign.campaign_id)

    def reconcile_campaign_report_after_exhaustion(campaign: CampaignRecord) -> None:
        reconcile_campaign_report(
            campaign,
            source="budget exhaustion",
            operation_id=campaign.root_operation_id,
        )

    def deliver_mail_after_campaign_task(
        campaign: CampaignRecord,
        campaign_request: CampaignRunRequest,
        execution: AgentTaskExecution,
    ) -> None:
        campaign = reconcile_campaign_task_settlement(
            background_tasks,
            campaign,
            campaign_request,
            execution,
        )
        if campaign_request.role != "report":
            campaign = store.fence_campaign_exhaustion_if_depleted(campaign.campaign_id)
        settled_task = store.agent_task(execution.operation_id)
        if (
            settled_task is not None
            and settled_task.status in {"failed", "interrupted"}
            and campaign_request.role in {"orchestrator", "report"}
        ):
            recovery = store.campaign_control_recovery(
                campaign.campaign_id,
                settled_task.operation_id,
            )
            if recovery is not None and recovery.status in {"pending", "admitted"}:
                return
        try:
            reconcile_pending_campaign_mail(
                background_tasks,
                campaign_id=campaign.campaign_id,
            )
        except Exception as exc:
            logger.warning(
                "Could not deliver pending campaign mail after task %s: %s",
                execution.operation_id,
                exc,
            )
            with suppress(Exception):
                store.record_agent_task_receipt(
                    execution.operation_id,
                    "campaign_mail_delivery_retry_failed",
                    {"exception_type": type(exc).__name__, "detail": str(exc)},
                    tier="diagnostic",
                )
        reconcile_campaign_report(
            campaign,
            source=f"task {execution.operation_id} settlement",
            operation_id=execution.operation_id,
        )

    def reconcile_campaign_recovery_pass() -> None:
        reconcile_due_campaign_recoveries(
            background_tasks,
            reconcile_report=lambda campaign: reconcile_campaign_report(
                campaign,
                source="durable recovery retry",
                operation_id=campaign.root_operation_id,
            ),
        )

    background_tasks.on_campaign_task_settled = deliver_mail_after_campaign_task
    background_tasks.on_campaign_admission_exhausted = reconcile_campaign_report_after_exhaustion

    def deliver_watcher_group(
        group: list[StoredWatcherRecord],
        *,
        retry_generation: WatcherRetryGeneration | None = None,
    ) -> None:
        if not group:
            return
        if not retry_generation_is_current(retry_generation):
            return
        watcher_ids = [item.watcher_id for item in group]
        authorized_by, terminal_diagnostic = store.resolve_watcher_delivery_authorizer(watcher_ids)
        if not retry_generation_is_current(retry_generation):
            return
        if authorized_by is None:
            if terminal_diagnostic is not None:
                logger.warning(
                    "Watcher delivery terminalized for %s: %s",
                    watcher_ids,
                    terminal_diagnostic,
                )
            return
        first = group[0]
        continuation = first.continuation
        service = _project_service(catalog, first.project_id)
        if first.origin_task_kind == "campaign":
            started: list[str] = []

            def claim_campaign_wake() -> None:
                operation_id = deliver_campaign_watcher_group(background_tasks, group)
                if operation_id is not None:
                    started.append(operation_id)

            if retry_generation is not None:
                if not retry_generation.run_if_current(claim_campaign_wake):
                    return
            else:
                claim_campaign_wake()
            if started:
                logger.info(
                    "Campaign watcher group %s queued task %s.",
                    watcher_ids,
                    started[0],
                )
            return
        if continuation.patch_kind == "experiment_loop":
            control_node_id = continuation.control_node_id
            if control_node_id is None:
                raise ValueError("An Experiment watcher is missing its control node.")
            with experiment_operation_lock(first.project_id):
                state = service.history.state()
                if not retry_generation_is_current(retry_generation):
                    return
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
                if not retry_generation_is_current(retry_generation):
                    return
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
                # Watchers keep the maintenance turn as immutable creation
                # provenance. Delivery always resumes the live episode's node
                # chat and pinned policy instead.
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
                        "chat_scope": "node",
                        "node_id": control_node_id,
                        "chat_id": episode.chat_id,
                        "session_id": preflight.session_id,
                    }
                )

                background_tasks.start_watcher_notification(
                    first.project_id,
                    "node_chat",
                    request,
                    watcher_ids,
                    authorized_by=authorized_by,
                    episode_stage_host=preflight.stage_host,
                    episode_stage_root=preflight.stage_root,
                    admission_fence=(
                        retry_generation.run_if_current if retry_generation is not None else None
                    ),
                )
            return

        request = _generic_watcher_delivery_request(group)
        with experiment_admission(first.project_id, service, request):
            background_tasks.start_watcher_notification(
                first.project_id,
                first.origin_task_kind,
                request,
                watcher_ids,
                authorized_by=authorized_by,
                admission_fence=(
                    retry_generation.run_if_current if retry_generation is not None else None
                ),
            )

    def evaluate_graph_wake_boundary(
        project_id: str,
        _trigger_state: GraphState | None,
        *,
        source: str,
        retry_generation: WatcherRetryGeneration | None = None,
    ) -> None:
        """Reconcile canonical graph conditions without changing the trigger's verdict."""

        if not retry_generation_is_current(retry_generation):
            return
        try:
            with graph_watcher_reconciliation_lock(project_id):
                active_records = store.active_graph_watchers(project_id)
                if active_records:
                    service = _project_service(catalog, project_id)
                    replay, boundaries = service.history.accepted_boundary_states()
                    if not retry_generation_is_current(retry_generation):
                        return
                    if replay.state.replay_status != "complete":
                        raise _GraphWatcherReplayDegraded(
                            "canonical graph replay is degraded at revision "
                            f"{replay.state.revision}"
                        )

                    # Captured task/sync state is only an arrival signal. Every
                    # production entry point replays accepted boundaries in canonical
                    # order so reversed task settlement cannot invert terminal watcher
                    # outcomes. Legacy rows are based at the coherent head and never
                    # retroactively evaluated against earlier history.
                    evaluated_at = store.now()
                    for record in active_records:
                        if record.armed_revision is None:
                            initialized = run_for_retry_generation(
                                retry_generation,
                                lambda record=record: store.initialize_graph_watcher_baseline(
                                    record.watcher_id,
                                    armed_revision=replay.state.revision,
                                    evaluated_at=evaluated_at,
                                ),
                            )
                            if not initialized:
                                return
                    for boundary in boundaries:
                        evaluated = run_for_retry_generation(
                            retry_generation,
                            lambda boundary=boundary: evaluate_graph_watchers(
                                store,
                                project_id,
                                boundary,
                            ),
                        )
                        if not evaluated:
                            return
        except _GraphWatcherReplayDegraded as exc:
            if not run_for_retry_generation(
                retry_generation,
                lambda: clear_graph_watcher_reconciliation_retry(project_id),
            ):
                return
            logger.warning(
                "Could not reconcile graph conditions after %s for project %s: %s",
                source,
                project_id,
                exc,
            )
            deliver_ready_graph_wake_groups(
                project_id,
                source=f"{source} degraded graph replay",
                retry_generation=retry_generation,
            )
            return
        except (OSError, StateUnavailable) as exc:
            if not run_for_retry_generation(
                retry_generation,
                lambda: schedule_graph_watcher_reconciliation(project_id),
            ):
                return
            logger.warning(
                "Could not reconcile graph conditions after %s for project %s: %s",
                source,
                project_id,
                exc,
            )
            deliver_ready_graph_wake_groups(
                project_id,
                source=f"{source} graph evaluation failure",
                retry_generation=retry_generation,
            )
            return
        except Exception as exc:
            if not run_for_retry_generation(
                retry_generation,
                lambda: clear_graph_watcher_reconciliation_retry(project_id),
            ):
                return
            logger.warning(
                "Could not reconcile graph conditions after %s for project %s: %s",
                source,
                project_id,
                exc,
            )
            deliver_ready_graph_wake_groups(
                project_id,
                source=f"{source} graph evaluation failure",
                retry_generation=retry_generation,
            )
            return
        if not run_for_retry_generation(
            retry_generation,
            lambda: clear_graph_watcher_reconciliation_retry(project_id),
        ):
            return
        deliver_ready_graph_wake_groups(
            project_id,
            source=source,
            retry_generation=retry_generation,
        )

    def deliver_ready_graph_wake_groups(
        project_id: str,
        *,
        source: str,
        retry_generation: WatcherRetryGeneration | None = None,
    ) -> None:
        """Retry ready graph delivery without evaluating an active condition."""

        if not retry_generation_is_current(retry_generation):
            return
        try:
            groups = ready_graph_watcher_groups(store, project_id)
        except Exception as exc:
            logger.warning(
                "Could not read ready graph-condition completions after %s for project %s: %s",
                source,
                project_id,
                exc,
            )
            return
        for group in groups:
            if not retry_generation_is_current(retry_generation):
                return
            try:
                deliver_watcher_group(
                    group,
                    retry_generation=retry_generation,
                )
            except Exception as exc:
                logger.warning(
                    "Could not retry graph-condition completion %s for project %s: %s",
                    [item.watcher_id for item in group],
                    project_id,
                    exc,
                )

    def sweep_graph_conditions_at_startup() -> None:
        for project_id in store.graph_watcher_project_ids():
            evaluate_graph_wake_boundary(project_id, None, source="startup sweep")

    def retry_graph_wakes_after_poll(generation: WatcherRetryGeneration) -> None:
        due = due_graph_watcher_reconciliations()
        for project_id in due:
            if not generation.is_current():
                return
            evaluate_graph_wake_boundary(
                project_id,
                None,
                source="reconciliation retry",
                retry_generation=generation,
            )
        reconciled = set(due)
        for project_id in store.graph_watcher_project_ids():
            if not generation.is_current():
                return
            if project_id not in reconciled:
                deliver_ready_graph_wake_groups(
                    project_id,
                    source="watcher poll",
                    retry_generation=generation,
                )

    graph_watcher_retry_worker = WatcherRetryWorker(retry_graph_wakes_after_poll)

    def after_watcher_poll() -> None:
        graph_watcher_retry_worker.signal()
        reconcile_campaign_recovery_pass()

    watcher_poller = WatcherPoller(
        store,
        on_completed=deliver_watcher_group,
        on_poll_completed=after_watcher_poll,
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
            await asyncio.to_thread(reconcile_orphaned_campaign_failures, background_tasks)
            await asyncio.to_thread(fence_depleted_campaigns_at_startup)
            try:
                await asyncio.to_thread(reconcile_pending_campaign_mail, background_tasks)
            except Exception as exc:
                logger.warning("Could not reconcile pending campaign mail at startup: %s", exc)
            await asyncio.to_thread(reconcile_campaign_reports_at_startup)
            await asyncio.to_thread(reconcile_campaign_recovery_pass)
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
            for task in list(project_reconciliation_tasks.values()):
                task.cancel()
            for task in startup_maintenance:
                with suppress(asyncio.CancelledError):
                    await task
            for task in list(project_reconciliation_tasks.values()):
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
    app.state.catalog = catalog
    app.state.provider_skills = provider_skills
    app.state.setup = setup
    app.state.default_project_id = default_project_id
    app.state.service = default_service
    app.state.data_dir = app_data
    app.state.background_tasks = background_tasks
    app.state.project_reconciliation_tasks = project_reconciliation_tasks
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

    def identity_payload(user: SpaceUserRecord) -> dict[str, object]:
        return {
            "space_id": space_id,
            "space_kind": space_kind,
            "space_name": store.space_name,
            "user": user.model_dump(mode="json"),
        }

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

    @app.get("/api/projects")
    def projects() -> list[dict[str, object]]:
        return catalog.cards()

    @app.get("/api/experiment-loops")
    def experiment_loops() -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []
        for record in store.projects():
            cache_status, cached = catalog.cached_snapshot_status(record.project_id)
            if cache_status == "invalid" or (
                cache_status == "missing" and record.revision is not None
            ):
                raise HTTPException(
                    status_code=503,
                    detail=f"Cached project snapshot is unavailable for {record.project_id}.",
                )
            state = _cached_graph_state(cached)
            if state is None:
                continue
            reachable = _cached_project_reachable(cached)
            if record.reachable is False:
                reachable = False

            experiments = [node for node in state.nodes.values() if isinstance(node, Experiment)]
            experiment_ids = [node.id for node in experiments]
            runtimes = store.experiment_loop_runtimes(record.project_id, experiment_ids)
            settle_ids = [
                experiment_id
                for experiment_id, runtime in runtimes.items()
                if runtime.stop_requested and not runtime.stop_settled and not runtime.task_active
            ]
            for experiment_id in settle_ids:
                store.settle_experiment_loop_stop(record.project_id, experiment_id)
            if settle_ids:
                runtimes.update(store.experiment_loop_runtimes(record.project_id, settle_ids))
            for node in experiments:
                runtime = runtimes[node.id]
                if runtime.episode_id is None:
                    continue
                entries.append(
                    {
                        "project_id": record.project_id,
                        "project_name": record.name,
                        "project_reachable": reachable,
                        "node": node.model_dump(mode="json"),
                        "control": _experiment_control_from_runtime(
                            state, node.id, runtime
                        ).model_dump(mode="json"),
                    }
                )
        return entries

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

    async def reconcile_cached_project(project_id: str) -> None:
        try:
            head_status = await asyncio.to_thread(catalog.probe_remote_patch_log_head, project_id)
            if head_status == "unavailable":
                await asyncio.to_thread(
                    catalog.update_cached_snapshot_freshness,
                    project_id,
                    "stale",
                )
                return
            if head_status == "unchanged":
                await asyncio.to_thread(
                    catalog.update_cached_snapshot_freshness,
                    project_id,
                    "fresh",
                )
                return

            await asyncio.to_thread(
                catalog.update_cached_snapshot_freshness,
                project_id,
                "reconciling",
            )
            generation = await asyncio.to_thread(
                catalog.reserve_cached_snapshot_generation,
                project_id,
            )
            service, snapshot = await asyncio.to_thread(catalog.reconcile_snapshot, project_id)
            attach_experiment_control(project_id, snapshot)
            await asyncio.to_thread(
                catalog.commit_cached_snapshot,
                project_id,
                snapshot,
                generation=generation,
                patch_log_head=service.history.workspace.cached_patch_log_head(),
            )
        except KeyError:
            return
        except Exception as exc:
            logger.warning("Could not reconcile display snapshot for %s: %s", project_id, exc)
            with suppress(KeyError, OSError, TypeError, ValueError):
                await asyncio.to_thread(
                    catalog.update_cached_snapshot_freshness,
                    project_id,
                    "stale",
                )

    def schedule_project_reconciliation(project_id: str) -> None:
        task = project_reconciliation_tasks.get(project_id)
        if task is not None and not task.done():
            return
        now = time.monotonic()
        last_started = project_probe_started_at.get(project_id)
        if (
            last_started is not None
            and now - last_started < REMOTE_STATE_HEAD_PROBE_INTERVAL_SECONDS
        ):
            return
        project_probe_started_at[project_id] = now
        task = asyncio.create_task(reconcile_cached_project(project_id))
        project_reconciliation_tasks[project_id] = task

        def forget(completed: asyncio.Task[None]) -> None:
            if project_reconciliation_tasks.get(project_id) is completed:
                project_reconciliation_tasks.pop(project_id, None)

        task.add_done_callback(forget)

    @app.get("/api/projects/{project_id}")
    async def project(project_id: str) -> dict[str, object]:
        cached = catalog.cached_snapshot(project_id)
        if cached is not None:
            attach_experiment_control(project_id, cached)
            return cached
        try:
            generation = catalog.reserve_cached_snapshot_generation(project_id)
            service, snapshot = await asyncio.to_thread(catalog.open_snapshot, project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Project not found") from exc
        except (FileNotFoundError, OSError, ValueError, StateUnavailable) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        snapshot["id"] = project_id
        attach_experiment_control(project_id, snapshot)
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
                latest = catalog.cached_snapshot(project_id)
                if latest is not None:
                    attach_experiment_control(project_id, latest)
                    return latest
        return snapshot

    @app.get("/api/projects/{project_id}/cached")
    def cached_project(project_id: str) -> dict[str, object]:
        snapshot = catalog.cached_snapshot(project_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Cached project snapshot not found")
        attach_experiment_control(project_id, snapshot)
        return snapshot

    @app.get("/api/projects/{project_id}/cached/revision")
    async def cached_project_revision(project_id: str) -> dict[str, object]:
        snapshot = catalog.cached_snapshot(project_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Cached project snapshot not found")
        schedule_project_reconciliation(project_id)
        return {
            "revision": snapshot["revision"],
            "snapshot_freshness": snapshot["snapshot_freshness"],
            "last_remote_sync_at": snapshot["last_remote_sync_at"],
        }

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

    @app.get("/api/projects/{project_id}/repositories/files/preview")
    @app.head("/api/projects/{project_id}/repositories/files/preview")
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
    def sync_graph(project_id: str, body: GraphSyncRequest, request: Request):
        authorized_by = require_patch_capable_identity(request)
        service = _project_service(catalog, project_id)
        try:
            if body.removed_node_ids:
                with experiment_operation_lock(project_id):
                    state = service.sync_graph(
                        body,
                        active_control_node_ids=store.active_experiment_control_ids(project_id),
                        authorized_by=authorized_by,
                    )
            else:
                state = service.sync_graph(
                    body,
                    active_control_node_ids=store.active_experiment_control_ids(project_id),
                    authorized_by=authorized_by,
                )
            evaluate_graph_wake_boundary(project_id, state, source="human Sync")
            return state.model_dump(mode="json")
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

    @app.post(
        "/api/projects/{project_id}/chats/{chat_id}/attachments",
        response_model=ChatAttachmentUpload,
    )
    def upload_chat_attachment(
        project_id: str,
        chat_id: str,
        file: Annotated[UploadFile, File()],
        client_id: Annotated[str, Form()],
        attachment_set_id: Annotated[str | None, Form()] = None,
    ) -> ChatAttachmentUpload:
        _require_registered_project(catalog, project_id)
        try:
            return attachment_store.add(
                project_id=project_id,
                chat_id=chat_id,
                client_id=client_id,
                filename=file.filename or "",
                media_type=file.content_type,
                source=file.file,
                attachment_set_id=attachment_set_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            file.file.close()

    @app.delete(
        "/api/projects/{project_id}/chats/{chat_id}/attachments/{attachment_id}",
    )
    def remove_chat_attachment(
        project_id: str,
        chat_id: str,
        attachment_id: str,
        client_id: str,
        attachment_set_id: str,
    ) -> dict[str, bool]:
        _require_registered_project(catalog, project_id)
        try:
            attachment_store.remove(
                project_id=project_id,
                chat_id=chat_id,
                client_id=client_id,
                attachment_set_id=attachment_set_id,
                attachment_id=attachment_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"removed": True}

    @app.post("/api/projects/{project_id}/tasks/{kind}", status_code=202)
    def start_agent_task(
        project_id: str,
        kind: AgentTaskKind,
        body: dict[str, object],
        http_request: Request,
    ) -> dict[str, object]:
        if kind == "campaign":
            raise HTTPException(
                status_code=405,
                detail="Use the project campaign endpoint to start auto-research.",
            )
        authorized_by = require_patch_capable_identity(http_request)
        service = _project_service(catalog, project_id)
        admission_lock: threading.Lock | None = None
        result_view_stage_host: str | None = None
        result_view_stage_root: str | None = None
        try:
            request = _validated_task_request(service, kind, body)
            if isinstance(request, RunRequest):
                if request.result_view is not None and request.result_view.action == "revise":
                    admission_lock = result_view_keep_lock(request.result_view.view_id)
                    admission_lock.acquire()
                request = _admit_result_view_request(
                    store,
                    service,
                    project_id,
                    kind,
                    request,
                )
                if request.result_view is not None and request.result_view.action == "revise":
                    view = store.result_view(request.result_view.view_id)
                    if view is None:
                        raise ValueError("The result view is unavailable or expired.")
                    result_view_stage_host = view.stage_host or None
                    result_view_stage_root = view.stage_root
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
            operation_id = str(uuid.uuid4())
            claimed_set: tuple[str, str] | None = None
            if kind in {"node_chat", "project_chat"}:
                assert isinstance(request, RunRequest)
                supplied = (request.attachment_set_id, request.attachment_client_id)
                if any(supplied) and not all(supplied):
                    raise ValueError(
                        "Chat attachments require both attachment_set_id and attachment_client_id."
                    )
                if request.attachment_set_id and request.attachment_client_id:
                    assert request.chat_id is not None
                    claimed = attachment_store.claim(
                        project_id=project_id,
                        chat_id=request.chat_id,
                        client_id=request.attachment_client_id,
                        attachment_set_id=request.attachment_set_id,
                        operation_id=operation_id,
                    )
                    claimed_set = (claimed.attachment_batch_id, operation_id)
                    request = request.model_copy(
                        update={
                            "attachment_set_id": None,
                            "attachment_client_id": None,
                            "attachment_batch_id": claimed.attachment_batch_id,
                            "attachments": claimed.attachments,
                        }
                    )
            try:
                record = background_tasks.start(
                    project_id,
                    kind,
                    request,
                    operation_id=operation_id,
                    authorized_by=authorized_by,
                    stage_host=result_view_stage_host,
                    stage_root=result_view_stage_root,
                )
            except BaseException:
                if claimed_set is not None and store.agent_task(operation_id) is None:
                    attachment_store.release(*claimed_set)
                raise
        except ValueError as exc:
            status = 409 if "already running" in str(exc) else 422
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        finally:
            if admission_lock is not None:
                admission_lock.release()
        return record.model_dump(mode="json")

    @app.post("/api/projects/{project_id}/experiments/{node_id:path}/run", status_code=202)
    def run_experiment(
        project_id: str,
        node_id: str,
        body: dict[str, object],
        request: Request,
    ) -> dict[str, object]:
        authorized_by = require_patch_capable_identity(request)
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
                if supplied.result_view is not None:
                    raise ValueError("Result views require an ordinary node Work turn.")
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
                    profile = service.resolve_agent_profile("node_chat")
                    request = request.model_copy(
                        update={
                            "provider": profile.provider,
                            "model": profile.model,
                            "reasoning": profile.reasoning,
                            "run_on": profile.run_on,
                            "run_truth_scope": supplied.run_truth_scope,
                            "chat_scope": "node",
                            "node_id": node_id,
                            "chat_id": supplied.chat_id,
                            "session_id": None,
                        }
                    )
                    request = _resolved_graph_request(
                        service,
                        "node_chat",
                        request,
                    )
                    record = background_tasks.start_watcher_notification(
                        project_id,
                        "node_chat",
                        request,
                        [item.watcher_id for item in pending_group],
                        authorized_by=authorized_by,
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
                record = background_tasks.start(
                    project_id,
                    "node_chat",
                    request,
                    authorized_by=authorized_by,
                )
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

    @app.post("/api/projects/{project_id}/watchers/{watcher_id}/check")
    def check_watcher_now(project_id: str, watcher_id: str) -> dict[str, object]:
        _require_registered_project(catalog, project_id)
        try:
            watcher = watcher_poller.check_now(project_id, watcher_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Watcher not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return watcher.model_dump(mode="json")

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

    @app.get(
        "/api/projects/{project_id}/campaigns",
        response_model=list[CampaignResponse],
    )
    def campaigns(project_id: str) -> list[CampaignResponse]:
        _require_registered_project(catalog, project_id)
        return serialize_campaigns(store, project_id)

    @app.post(
        "/api/projects/{project_id}/campaigns",
        response_model=CampaignResponse,
        status_code=202,
    )
    def start_campaign(
        project_id: str,
        body: StartCampaignBody,
        request: Request,
    ) -> CampaignResponse:
        authorized_by = require_patch_capable_identity(request)
        service = _project_service(catalog, project_id)
        try:
            start_request = _resolved_campaign_start_request(service, body)
            service.history.require_writable()
            campaign, _ = background_tasks.start_campaign(
                project_id,
                start_request,
                authorized_by=authorized_by,
            )
            return serialize_campaign(store, project_id, campaign)
        except ValueError as exc:
            status = 409 if store.active_campaign(project_id) is not None else 422
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @app.post(
        "/api/projects/{project_id}/campaigns/{campaign_id}/stop",
        response_model=CampaignResponse,
    )
    def stop_campaign(
        project_id: str,
        campaign_id: str,
        request: Request,
    ) -> CampaignResponse:
        require_patch_capable_identity(request)
        campaign = _campaign_for_http(store, catalog, project_id, campaign_id)
        try:
            stopped = background_tasks.stop_campaign(campaign.campaign_id)
        except CampaignNotRunning as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        reconcile_campaign_report(
            stopped,
            source="Stop request",
            operation_id=stopped.root_operation_id,
        )
        current = store.campaign(stopped.campaign_id)
        if current is None:
            raise RuntimeError("The stopped campaign could not be reloaded.")
        return serialize_campaign(store, project_id, current)

    @app.post(
        "/api/projects/{project_id}/campaigns/{campaign_id}/reauthorize",
        response_model=CampaignResponse,
    )
    def reauthorize_campaign(
        project_id: str,
        campaign_id: str,
        body: ReauthorizeCampaignBody,
        request: Request,
    ) -> CampaignResponse:
        require_patch_capable_identity(request)
        campaign = _campaign_for_http(store, catalog, project_id, campaign_id)
        service = _project_service(catalog, project_id)
        try:
            reauthorized, _ = background_tasks.reauthorize_campaign(
                campaign.campaign_id,
                body.additional_invocations,
                request_preflight=lambda saved: _preflight_campaign_reauthorization(
                    service,
                    saved,
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            reconcile_pending_campaign_mail(
                background_tasks,
                campaign_id=campaign.campaign_id,
            )
        except Exception as exc:
            logger.warning(
                "Could not reconcile pending campaign mail after reauthorizing %s: %s",
                campaign.campaign_id,
                exc,
            )
        current = store.campaign(reauthorized.campaign_id)
        if current is None:
            raise RuntimeError("The reauthorized campaign could not be reloaded.")
        return serialize_campaign(store, project_id, current)

    @app.get(
        "/api/projects/{project_id}/campaigns/{campaign_id}/messages",
        response_model=list[CampaignMessageRecord],
    )
    def campaign_messages(
        project_id: str,
        campaign_id: str,
    ) -> list[CampaignMessageRecord]:
        campaign = _campaign_for_http(store, catalog, project_id, campaign_id)
        return store.campaign_messages(campaign.campaign_id)

    @app.post(
        "/api/projects/{project_id}/campaigns/{campaign_id}/messages",
        response_model=CampaignMessageRecord,
        status_code=201,
    )
    def send_campaign_message(
        project_id: str,
        campaign_id: str,
        body: CampaignMessageBody,
        request: Request,
    ) -> CampaignMessageRecord:
        authorized_by = require_patch_capable_identity(request)
        campaign = _campaign_for_http(store, catalog, project_id, campaign_id)
        if campaign.status in {"succeeded", "stopped", "failed"}:
            raise HTTPException(status_code=409, detail="Campaign has already ended")
        if campaign.status != "running" or campaign.ending is not None:
            raise HTTPException(status_code=409, detail="Campaign is not accepting new mail")
        if campaign.root_operation_id is None:
            raise HTTPException(status_code=409, detail="Campaign orchestrator is unavailable")
        try:
            saved = record_campaign_message(
                store,
                campaign_id=campaign.campaign_id,
                sender_role="human",
                sender_task_id=None,
                authorized_by=authorized_by,
                recipient_task_id=campaign.root_operation_id,
                body=body.body,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            deliver_pending_campaign_mail(
                background_tasks,
                campaign_id=campaign.campaign_id,
                recipient_task_id=campaign.root_operation_id,
            )
        except Exception as exc:
            logger.warning(
                "Could not deliver durable campaign message %s immediately: %s",
                saved.message_id,
                exc,
            )
        current = store.campaign_message(saved.message_id)
        if current is None:
            raise RuntimeError("The durable campaign message could not be reloaded.")
        return current

    @app.get("/api/projects/{project_id}/campaigns/{campaign_id}/reports/{report_id}/preview")
    @app.head("/api/projects/{project_id}/campaigns/{campaign_id}/reports/{report_id}/preview")
    def preview_campaign_report(
        project_id: str,
        campaign_id: str,
        report_id: str,
        request: Request,
    ) -> Response:
        campaign = _campaign_for_http(store, catalog, project_id, campaign_id)
        report = store.campaign_report(report_id)
        if report is None or report.campaign_id != campaign.campaign_id:
            raise HTTPException(status_code=404, detail="Campaign report not found")
        try:
            document, csp = html_preview_document(
                report.html.encode("utf-8"),
            )
        except (UnicodeError, ValueError) as exc:
            raise HTTPException(status_code=410, detail="Campaign report unavailable") from exc
        encoded = document.encode("utf-8")
        return Response(
            b"" if request.method == "HEAD" else encoded,
            media_type="text/html",
            headers={
                "Cache-Control": "no-store",
                "Content-Length": str(len(encoded)),
                "Content-Security-Policy": csp,
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get(
        "/api/projects/{project_id}/result-views",
        response_model=list[ResultViewDescriptor],
    )
    def result_views(
        project_id: str,
        experiment_id: str | None = None,
        chat_id: str | None = None,
    ) -> list[ResultViewDescriptor]:
        _require_registered_project(catalog, project_id)
        return store.list_result_view_descriptors(
            project_id,
            experiment_id=experiment_id,
            chat_id=chat_id,
        )

    @app.get("/api/projects/{project_id}/result-views/{view_id}/preview")
    @app.head("/api/projects/{project_id}/result-views/{view_id}/preview")
    async def preview_result_view(
        project_id: str,
        view_id: str,
        request: Request,
    ) -> Response:
        _require_registered_project(catalog, project_id)
        _, data = await asyncio.to_thread(
            _load_visible_result_view_bytes,
            store,
            catalog,
            project_id,
            view_id,
        )
        try:
            document, csp = html_preview_document(data, result_view_gestures=True)
        except (UnicodeError, ValueError) as exc:
            raise HTTPException(status_code=410, detail="Result view unavailable") from exc
        encoded = document.encode("utf-8")
        headers = {
            "Cache-Control": "no-store",
            "Content-Length": str(len(encoded)),
            "Content-Security-Policy": csp,
            "X-Content-Type-Options": "nosniff",
        }
        return Response(
            b"" if request.method == "HEAD" else encoded,
            media_type="text/html",
            headers=headers,
        )

    @app.post(
        "/api/projects/{project_id}/result-views/{view_id}/keep",
        response_model=ResultViewDescriptor,
    )
    def keep_result_view(project_id: str, view_id: str) -> ResultViewDescriptor:
        _require_registered_project(catalog, project_id)
        with result_view_keep_lock(view_id):
            record = _visible_result_view_record(store, project_id, view_id)
            if record.kept_filename is not None:
                return store.result_view_descriptor(record)
            if _has_active_result_view_revision(store, record):
                raise HTTPException(
                    status_code=409,
                    detail="Wait for the active result view revision before keeping it.",
                )
            data = _read_result_view_bytes_for_http(catalog, record)
            service = _project_service(catalog, project_id)
            project_name = catalog.card(project_id)["name"]
            if not isinstance(project_name, str):
                raise HTTPException(status_code=503, detail="Result view Keep unavailable")
            try:
                kept_filename = service.history.workspace.keep_result_view(
                    source_name=record.source_name,
                    project_name=project_name,
                    data=data,
                )
            except (FileNotFoundError, OSError, StateUnavailable, ValueError) as exc:
                raise HTTPException(
                    status_code=503,
                    detail="Result view Keep unavailable",
                ) from exc
            try:
                kept = store.mark_result_view_kept(
                    view_id,
                    expected_content_sha256=record.content_sha256,
                    kept_filename=kept_filename,
                    kept_at=store.now(),
                )
            except ResultViewConflict as exc:
                raise HTTPException(
                    status_code=409,
                    detail="The result view changed before Keep completed.",
                ) from exc
            return store.result_view_descriptor(kept)

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
    def resume_agent_task(
        project_id: str,
        operation_id: str,
        request: Request,
    ) -> dict[str, object]:
        previous = store.agent_task(operation_id)
        if previous is None or previous.project_id != project_id:
            raise HTTPException(status_code=404, detail="Agent task not found")
        authorized_by = (
            require_patch_capable_identity(request)
            if previous.kind != "campaign"
            or _task_is_patch_capable(previous.kind, previous.request)
            else None
        )
        service = _project_service(catalog, project_id)
        result_view_resume_lock: threading.Lock | None = None
        try:
            if previous.kind not in {"paper_coach", "campaign"}:
                stored_request = RunRequest.model_validate(previous.request)
                if (
                    stored_request.result_view is not None
                    and stored_request.result_view.action == "revise"
                ):
                    result_view_resume_lock = result_view_keep_lock(
                        stored_request.result_view.view_id
                    )
                    result_view_resume_lock.acquire()
                    _admit_result_view_request(
                        store,
                        service,
                        project_id,
                        previous.kind,
                        stored_request,
                    )
            with experiment_admission(project_id, service, previous.request):
                skills = _validate_stored_task_request(service, previous.kind, previous.request)
                return background_tasks.resume(
                    operation_id,
                    skills=skills,
                    authorized_by=authorized_by,
                ).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            if result_view_resume_lock is not None:
                result_view_resume_lock.release()

    @app.post(
        "/api/projects/{project_id}/tasks/{operation_id}/repair-graph-update",
        status_code=202,
    )
    def repair_agent_task_graph_update(
        project_id: str,
        operation_id: str,
        request: Request,
    ) -> dict[str, object]:
        previous = store.agent_task(operation_id)
        if previous is None or previous.project_id != project_id:
            raise HTTPException(status_code=404, detail="Agent task not found")
        authorized_by = (
            require_patch_capable_identity(request)
            if _task_is_patch_capable(previous.kind, previous.request)
            else None
        )
        service = _project_service(catalog, project_id)
        try:
            with experiment_admission(project_id, service, previous.request):
                return background_tasks.repair_graph_update(
                    operation_id,
                    authorized_by=authorized_by,
                ).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Agent task not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/projects/{project_id}/tasks/{operation_id}/retry", status_code=202)
    def retry_agent_task(
        project_id: str,
        operation_id: str,
        request: Request,
        body: RetryAgentTaskRequest | None = None,
    ) -> dict[str, object]:
        previous = store.agent_task(operation_id)
        if previous is None or previous.project_id != project_id:
            raise HTTPException(status_code=404, detail="Agent task not found")
        authorized_by = (
            require_patch_capable_identity(request)
            if previous.kind != "campaign"
            or _task_is_patch_capable(previous.kind, previous.request)
            else None
        )
        service = _project_service(catalog, project_id)
        result_view_retry_lock: threading.Lock | None = None
        try:
            overrides = body.model_dump(exclude_none=True) if body is not None else {}
            if previous.request.get("patch_kind") == "experiment_loop" and "run_on" in overrides:
                raise ValueError(
                    "Experiment-loop recovery cannot change its pinned execution machine."
                )
            if previous.kind == "campaign":
                candidate = CampaignRunRequest.model_validate({**previous.request, **overrides})
            else:
                request_type = CoachRequest if previous.kind == "paper_coach" else RunRequest
                candidate = request_type.model_validate(
                    {**previous.request, **overrides, "session_id": None}
                )
            if (
                isinstance(candidate, RunRequest)
                and candidate.result_view is not None
                and candidate.result_view.action == "revise"
            ):
                result_view_retry_lock = result_view_keep_lock(candidate.result_view.view_id)
                result_view_retry_lock.acquire()
                _admit_result_view_request(
                    store,
                    service,
                    project_id,
                    previous.kind,
                    candidate,
                )
            with experiment_admission(
                project_id,
                service,
                candidate.model_dump(mode="json"),
            ):
                skills = _validate_stored_task_request(
                    service,
                    previous.kind,
                    candidate.model_dump(mode="json"),
                )
                return background_tasks.retry(
                    operation_id,
                    skills=skills,
                    authorized_by=authorized_by,
                    **overrides,
                ).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            if result_view_retry_lock is not None:
                result_view_retry_lock.release()

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

    @app.get("/api/projects/{project_id}/paper/sessions")
    def paper_sessions(project_id: str):
        paper = _project_service(catalog, project_id).paper
        return [item.model_dump(mode="json") for item in paper.sessions()]

    web_dist = web_dist_path()
    if web_dist.exists():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")

    return app


def _visible_result_view_record(
    store: AppStore,
    project_id: str,
    view_id: str,
) -> ResultViewRecord:
    as_of = datetime.now(UTC)
    record = store.result_view_for_diagnostics(view_id)
    if record is None or record.project_id != project_id:
        raise HTTPException(status_code=404, detail="Result view not found")
    if record.kept_filename is None and store.result_view(view_id, as_of=as_of) is None:
        raise HTTPException(status_code=410, detail="Result view expired")
    return record


def _has_active_result_view_revision(store: AppStore, record: ResultViewRecord) -> bool:
    with store.connection() as connection:
        row = connection.execute(
            """
            SELECT 1 FROM graph_runs AS revision
            WHERE revision.project_id = ? AND revision.kind = 'node_chat'
              AND json_extract(revision.request_json, '$.chat_id') = ?
              AND json_extract(revision.request_json, '$.result_view.action') = 'revise'
              AND json_extract(revision.request_json, '$.result_view.view_id') = ?
              AND (
                revision.status IN ('queued', 'running', 'pausing')
                OR (
                  revision.status IN ('paused', 'interrupted')
                  AND revision.native_session_id IS NOT NULL
                  AND revision.stage_root IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM graph_runs AS child
                    WHERE child.parent_operation_id = revision.operation_id
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM graph_run_receipts AS receipt
                    WHERE receipt.operation_id = revision.operation_id
                      AND receipt.category = 'experiment_recovery_abandoned'
                  )
                )
              )
            LIMIT 1
            """,
            (record.project_id, record.chat_id, record.view_id),
        ).fetchone()
    return row is not None


def _load_visible_result_view_bytes(
    store: AppStore,
    catalog: ProjectCatalog,
    project_id: str,
    view_id: str,
) -> tuple[ResultViewRecord, bytes]:
    record = _visible_result_view_record(store, project_id, view_id)
    return record, _read_result_view_bytes_for_http(catalog, record)


def _read_result_view_bytes_for_http(
    catalog: ProjectCatalog,
    record: ResultViewRecord,
) -> bytes:
    try:
        if record.kept_filename is not None:
            service = _project_service(catalog, record.project_id)
            data = service.history.workspace.read_kept_result_view(
                record.kept_filename,
                max_bytes=record.size_bytes,
            )
        elif record.stage_host:
            stage = RemoteRunStage(record.stage_host).attach_artifact_source(record.stage_root)
            data = stage.read_result_view_bytes(
                record.view_id,
                record.source_name,
                max_bytes=record.size_bytes,
            )
        else:
            data = read_local_result_view_bytes(
                Path(record.stage_root),
                record.view_id,
                record.source_name,
                max_bytes=record.size_bytes,
            )
        if len(data) != record.size_bytes:
            raise ValueError("result view size changed")
        if hashlib.sha256(data).hexdigest() != record.content_sha256:
            raise ValueError("result view digest changed")
        if validate_artifact_bytes(record.source_name, data) != "text/html":
            raise ValueError("result view media type changed")
        return data
    except HTTPException as exc:
        if exc.status_code == 404:
            raise
        raise HTTPException(status_code=503, detail="Result view storage unavailable") from exc
    except (FileNotFoundError, OSError, StateUnavailable) as exc:
        raise HTTPException(status_code=503, detail="Result view storage unavailable") from exc
    except ValueError as exc:
        raise HTTPException(status_code=410, detail="Result view unavailable") from exc


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


def _project_service(catalog: ProjectCatalog, project_id: str) -> ProjectService:
    try:
        return catalog.open(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


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


def _task_is_patch_capable(
    kind: AgentTaskKind,
    request: AgentTaskRequest | dict[str, object],
) -> bool:
    if kind in {"seed", "refresh"}:
        return True
    if kind == "campaign":
        role = request.role if isinstance(request, CampaignRunRequest) else request.get("role")
        return role != "report"
    if kind not in {"node_chat", "project_chat"}:
        return False
    if isinstance(request, RunRequest):
        return request.mode == "work"
    return request.get("mode") == "work"


def _campaign_for_http(
    store: AppStore,
    catalog: ProjectCatalog,
    project_id: str,
    campaign_id: str,
) -> CampaignRecord:
    _require_registered_project(catalog, project_id)
    try:
        return campaign_for_project(store, project_id, campaign_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Campaign not found") from exc


def _resolved_campaign_start_request(
    service: ProjectService,
    body: StartCampaignBody,
) -> CampaignStartRequest:
    profile = service.resolve_agent_profile("orchestrator")
    request = CampaignStartRequest(
        invocation_ceiling=body.invocation_ceiling,
        starting_instruction=body.starting_instruction,
        provider=profile.provider,
        model=profile.model,
        reasoning=profile.reasoning,
        run_on=profile.run_on,
        run_truth_scope=list(service.manifest.agent.default_run_truth_scope),
    )
    resolved = service.resolve_skill_request(cast(RunRequest, request))
    if not isinstance(resolved, CampaignStartRequest):
        raise TypeError("Campaign skill resolution changed the start request type.")
    return resolved


def _campaign_worker_request(
    context: CampaignCommandContext,
    arguments: SpawnArguments,
) -> CampaignRunRequest:
    """Seat a fresh worker without re-reading mutable project Settings."""

    return CampaignRunRequest.model_validate(
        {
            **context.request.model_dump(mode="json"),
            "role": "worker",
            "actor_operation_id": None,
            "session_id": None,
            "instruction": arguments.instruction,
            "control_node_id": arguments.seat_node_id,
            "wake_cause": None,
            "watcher_ids": [],
            "ending": None,
        }
    )


def _resolved_campaign_request(
    service: ProjectService,
    request: CampaignRunRequest,
) -> CampaignRunRequest:
    if (
        request.provider is None
        or request.model is None
        or request.reasoning is None
        or request.run_on is None
        or request.run_truth_scope is None
    ):
        raise ValueError(
            "Campaign recovery requires its exact pinned orchestrator execution profile."
        )
    profile_for(request.provider)
    if request.run_on not in service.manifest.machine_map:
        raise ValueError(f"unknown execution machine: {request.run_on}")
    skill_resolved = service.resolve_skill_request(cast(RunRequest, request))
    if not isinstance(skill_resolved, CampaignRunRequest):
        raise TypeError("Campaign skill resolution changed the task request type.")
    return skill_resolved


def _preflight_campaign_reauthorization(
    service: ProjectService,
    request: CampaignRunRequest,
) -> CampaignRunRequest:
    resolved = _resolved_campaign_request(service, request)
    service.history.require_writable()
    return resolved


def _require_registered_project(catalog: ProjectCatalog, project_id: str) -> None:
    try:
        catalog.card(project_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc


def _admit_result_view_request(
    store: AppStore,
    service: ProjectService,
    project_id: str,
    kind: AgentTaskKind,
    request: RunRequest,
) -> RunRequest:
    intent = request.result_view
    if intent is None:
        return request
    if (
        kind != "node_chat"
        or request.chat_scope != "node"
        or request.mode != "work"
        or request.trigger != "human"
        or request.patch_kind != "work"
        or request.control_node_id is not None
        or request.watcher_ids
    ):
        raise ValueError("Result views require an ordinary node Work turn.")
    if request.node_id is None or not isinstance(
        service.history.state().nodes.get(request.node_id),
        Experiment,
    ):
        raise ValueError("Result views require an Experiment node.")
    if intent.action == "create":
        return request

    record = store.result_view(intent.view_id)
    if record is None or record.project_id != project_id:
        raise ValueError("The result view is unavailable or expired.")
    if record.kept_filename is not None:
        raise ValueError("A kept result view cannot be revised.")
    if record.experiment_id != request.node_id or record.chat_id != request.chat_id:
        raise ValueError("The result view does not belong to this Experiment conversation.")

    pinned = RunRequest.model_validate(
        {
            **request.model_dump(mode="python"),
            "provider": record.provider,
            "model": record.model,
            "reasoning": record.reasoning,
            "run_on": record.run_on,
            "session_id": record.native_session_id,
        }
    )
    return _resolved_graph_request(service, kind, pinned)


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
            "attachment_batch_id": None,
            "attachments": [],
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
    if not request.message or not request.message.strip() or not request.chat_id:
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
) -> SkillSelection | None:
    """Validate a stored request and return any package-selection refresh it needs."""

    if kind == "campaign":
        campaign_request = CampaignRunRequest.model_validate(body)
        if campaign_request.role == "report":
            return _validate_stored_campaign_report_request(service, campaign_request)
        resolved_campaign = _resolved_campaign_request(
            service,
            campaign_request,
        )
        return service.resolve_skill_selection(cast(RunRequest, resolved_campaign))
    if kind == "paper_coach":
        resolved_coach = _resolved_coach_request(service, CoachRequest.model_validate(body))
        return service.resolve_skill_selection(resolved_coach)
    request = RunRequest.model_validate(body)
    if kind in {"seed", "refresh"}:
        service.history.require_writable()
    resolved_run = _resolved_graph_request(service, kind, request)
    return service.resolve_skill_selection(resolved_run)


def _validate_stored_campaign_report_request(
    service: ProjectService,
    request: CampaignRunRequest,
) -> None:
    """Validate the report-only package receipt without consulting Settings defaults."""

    if (
        request.provider is None
        or request.model is None
        or request.reasoning is None
        or request.run_on is None
        or request.run_truth_scope is None
    ):
        raise ValueError(
            "Campaign recovery requires its exact pinned orchestrator execution profile."
        )
    profile_for(request.provider)
    if request.run_on not in service.manifest.machine_map:
        raise ValueError(f"unknown execution machine: {request.run_on}")

    expected = official_registry().resolve(
        workflow_ids=[],
        skill_ids=["campaign-report"],
    )
    if (
        request.workflow_ids != expected.workflow_ids
        or request.skill_ids != expected.skill_ids
        or request.invoked_workflow_ids
        or request.invoked_skill_ids != ["campaign-report"]
        or request.invoked_provider_skill_names
        or request.resolved_provider_skills
        or request.resolved_skill_packages != expected.resolved_skill_packages
    ):
        raise ValueError(
            "Campaign report recovery requires its exact stored official "
            "campaign-report package; the saved package is missing, malformed, or stale."
        )
    return None


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
