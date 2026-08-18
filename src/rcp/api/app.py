from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from urllib.parse import quote

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
from rcp.api.episodes import (
    EpisodeMessageBody,
    EpisodeResponse,
    ReauthorizeEpisodeBody,
    StartEpisodeBody,
    episode_for_project,
    serialize_episode,
    serialize_episodes,
)
from rcp.api.identity import TEAM_SESSION_COOKIE, IdentityAccess, TrustedPrincipalResolver
from rcp.api.paper import router as paper_router
from rcp.artifacts import (
    ARTIFACT_MEDIA_TYPES,
    AgentArtifactDescriptor,
    ResultViewDescriptor,
    descriptor_for,
    html_preview_document,
    read_local_regular_file,
    validate_artifact_bytes,
)
from rcp.attachments import ChatAttachmentStore
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
    BranchMergeReceipt,
    Experiment,
    ExperimentDecisionPin,
    GraphBranchMetadata,
    GraphBranchSummary,
    GraphState,
    normalize_display_name,
)
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.core.transitions import (
    ProjectTransitionProjection,
    current_project_projection,
    transition_trigger_manifest,
)
from rcp.history import PatchRejected, ReplayHalted, RevisionConflict
from rcp.keyed_locks import ExperimentAdmission, KeyedLocks
from rcp.limits import (
    CHAT_ARTIFACT_MAX_FILE_BYTES,
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
    AutoResearchStartRequest,
    settle_auto_research_stop,
)
from rcp.runs.auto_research_child_reconcile import (
    reconcile_pending_auto_research_child_admissions,
)
from rcp.runs.auto_research_delivery import (
    deliver_auto_research_watcher_group,
    deliver_pending_auto_research_lifecycle,
    deliver_pending_auto_research_mail,
    reconcile_pending_auto_research_lifecycle,
    reconcile_pending_auto_research_mail,
    record_auto_research_message,
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
from rcp.runs.chat import _logical_chat_turn_operation_id
from rcp.runs.coach import _resolved_coach_request, stream_coach
from rcp.runs.discuss import stream_discuss_run
from rcp.runs.episode_reconcile import EpisodeReconciler
from rcp.runs.episode_report import EpisodeReportRunRequest, stream_episode_report_run
from rcp.runs.experiment_admission import (
    experiment_start_message,
    fresh_experiment_run_request,
    resolve_experiment_node_work_request,
)
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
    ACTIVE_AGENT_TASK_STATUSES,
    SPACE_NAME_MAX_LENGTH,
    AgentTaskKind,
    AgentUsageSnapshot,
    AppStore,
    AutoResearchMessageRecord,
    EpisodeNotRunning,
    EpisodeRecord,
    ExperimentLoopRuntime,
    GraphWatcherRecord,
    ResultViewConflict,
    ResultViewRecord,
    StoredWatcherRecord,
    TeamAuthenticationError,
    WatcherClaimConflict,
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
    require_patch_capable_identity = identity_access.require_patch_capable_identity
    identity_payload = identity_access.identity_payload
    launcher = AcceptanceAgentLauncher() if acceptance_agent else AgentLauncher()
    agent_mode: Literal["acceptance", "provider"] = "acceptance" if acceptance_agent else "provider"
    provider_skills = ProviderSkillInventoryManager(store)
    catalog = ProjectCatalog(app_data, store, launcher, provider_skills)
    attachment_store = ChatAttachmentStore(app_data / "chat-attachments")
    services = ApiServices(
        store=store,
        catalog=catalog,
        identity_access=identity_access,
        attachment_store=attachment_store,
    )

    def ensure_auto_research_graph_target(episode: EpisodeRecord) -> None:
        if (
            episode.mode != "auto_research"
            or episode.graph_target.kind != "branch"
            or episode.graph_target.branch_id != episode.episode_id
            or episode.graph_base_head is None
            or episode.authorized_by is None
        ):
            raise ValueError("Auto-research reservation lost its exact graph branch identity.")
        service = _project_service(catalog, episode.project_id)
        service.history.create_auto_research_branch(
            GraphBranchMetadata(
                branch_id=episode.episode_id,
                episode_id=episode.episode_id,
                project_id=episode.project_id,
                base_head=episode.graph_base_head,
                head=GraphHeadRef(
                    target=episode.graph_target,
                    revision=episode.graph_base_head.revision,
                    transition_id=episode.graph_base_head.transition_id,
                ),
                created_at=episode.created_at,
                authorized_by=episode.authorized_by,
            )
        )

    def graph_branch_summaries(
        episodes: list[EpisodeRecord],
    ) -> dict[str, GraphBranchSummary]:
        grouped: dict[str, list[EpisodeRecord]] = {}
        for episode in episodes:
            if (
                episode.mode != "auto_research"
                or episode.graph_target.kind != "branch"
                or episode.graph_target.branch_id != episode.episode_id
            ):
                raise ValueError("only an Auto-research branch has a graph branch summary")
            grouped.setdefault(episode.project_id, []).append(episode)

        summaries: dict[str, GraphBranchSummary] = {}
        for project_id, project_episodes in grouped.items():
            service = _project_service(catalog, project_id)
            snapshots = service.history.branch_read_snapshots(
                [
                    (episode.episode_id, episode.episode_id, episode.project_id)
                    for episode in project_episodes
                ]
            )
            for episode in project_episodes:
                snapshot = snapshots[episode.episode_id]
                summaries[episode.episode_id] = (
                    missing_graph_branch_summary(episode)
                    if snapshot is None
                    else graph_branch_summary_from_snapshot(
                        episode,
                        snapshot.metadata,
                        list(snapshot.receipts),
                    )
                )
        return summaries

    def missing_graph_branch_summary(episode: EpisodeRecord) -> GraphBranchSummary:
        if episode.status not in {"queued", "failed"} or episode.graph_base_head is None:
            raise KeyError(episode.episode_id)
        root = (
            store.agent_task(episode.root_operation_id)
            if episode.root_operation_id is not None
            else None
        )
        return GraphBranchSummary(
            branch_id=episode.episode_id,
            episode_id=episode.episode_id,
            base_head=episode.graph_base_head,
            head=GraphHeadRef(
                target=episode.graph_target,
                revision=episode.graph_base_head.revision,
                transition_id=episode.graph_base_head.transition_id,
            ),
            merge_eligible=False,
            merge_state="failed" if episode.status == "failed" else "unmerged",
            merge_diagnostic=(
                root.error
                if root is not None and root.error
                else episode.ending_diagnostic
                if episode.status == "failed"
                else "Establishing the episode graph branch before provider launch."
            ),
        )

    def graph_branch_summary_from_snapshot(
        episode: EpisodeRecord,
        metadata: GraphBranchMetadata,
        receipts: list[BranchMergeReceipt],
    ) -> GraphBranchSummary:
        current_receipt = next(
            (item for item in reversed(receipts) if item.provenance.branch_head == metadata.head),
            None,
        )
        merge_tasks = [
            item
            for item in store.episode_tasks(episode.episode_id)
            if item.kind == "branch_merge"
            and item.project_id == episode.project_id
            and item.graph_target == episode.graph_target
        ]
        active_task = next(
            (item for item in reversed(merge_tasks) if item.status in ACTIVE_AGENT_TASK_STATUSES),
            None,
        )
        latest_task = merge_tasks[-1] if merge_tasks else None
        active_branch_writers = [
            item
            for item in store.graph_target_tasks(
                episode.project_id,
                episode.graph_target,
                include_hidden=True,
            )
            if item.kind != "branch_merge"
            and item.status in {*ACTIVE_AGENT_TASK_STATUSES, "paused"}
            and task_graph_capable(item.kind, item.request)
        ]
        if active_task is not None:
            merge_state: Literal["unmerged", "running", "merged", "needs_action", "failed"] = (
                "running"
            )
        elif current_receipt is not None:
            merge_state = "merged"
        elif latest_task is not None and latest_task.status in {"paused", "interrupted"}:
            merge_state = "needs_action"
        elif latest_task is not None and latest_task.status == "failed":
            merge_state = "failed"
        else:
            merge_state = "unmerged"
        diagnostic = (
            (latest_task.error or latest_task.status_message)
            if merge_state in {"needs_action", "failed"} and latest_task is not None
            else None
        )
        merge_eligible = (
            episode.ending is not None
            and metadata.head.revision > metadata.base_head.revision
            and store.auto_research_is_quiescent(episode.episode_id)
            and active_task is None
            and not active_branch_writers
            and current_receipt is None
        )
        return GraphBranchSummary(
            branch_id=metadata.branch_id,
            episode_id=metadata.episode_id,
            base_head=metadata.base_head,
            head=metadata.head,
            merge_eligible=merge_eligible,
            merge_state=merge_state,
            latest_successful_merge=receipts[-1] if receipts else None,
            active_merge_task_id=(active_task.operation_id if active_task is not None else None),
            merge_diagnostic=diagnostic,
        )

    def graph_branch_summary(episode: EpisodeRecord) -> GraphBranchSummary:
        return graph_branch_summaries([episode])[episode.episode_id]

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

    def project_transition_payload(
        project_id: str,
        projection: ProjectTransitionProjection,
        *,
        reconcile_operational: bool,
    ) -> dict[str, object]:
        """Combine one graph/head projection with the matching live run controls."""

        payload = projection.model_dump(mode="json")
        control_snapshot: dict[str, object] = {"graph": payload["graph"]}
        if reconcile_operational:
            control_snapshot = project_display_cache.complete_transition_control(
                project_id,
                control_snapshot,
            )
        else:
            state = projection.graph
            experiment_ids = [
                node.id for node in state.nodes.values() if isinstance(node, Experiment)
            ]
            runtimes = store.experiment_loop_runtimes(
                project_id,
                experiment_ids,
                graph_target=GraphTargetRef(),
            )
            controls: dict[str, object] = {}
            for experiment_id in experiment_ids:
                runtime = runtimes[experiment_id]
                control = _experiment_control_from_runtime(
                    state,
                    experiment_id,
                    runtime,
                ).model_dump(mode="json")
                episode = (
                    store.episode(runtime.episode_id) if runtime.episode_id is not None else None
                )
                control["episode"] = (
                    serialize_episode(
                        store,
                        project_id,
                        episode,
                        branch_summary=graph_branch_summary,
                    ).model_dump(mode="json")
                    if episode is not None and episode.mode == "experiment_loop"
                    else None
                )
                controls[experiment_id] = control
            control_snapshot["experiment_control"] = controls
        payload["experiment_control"] = control_snapshot["experiment_control"]
        return payload

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

    @projects_router.get("/api/projects/{project_id}/history")
    def history(project_id: str, from_revision: int = 1, to_revision: int | None = None):
        service = _project_service(catalog, project_id)
        return service.history.slice(from_revision, to_revision)

    @projects_router.get("/api/projects/{project_id}/history/summaries")
    def history_summaries(
        project_id: str,
        from_revision: int = 1,
        to_revision: int | None = None,
    ):
        service = _project_service(catalog, project_id)
        summaries = service.history.revision_summaries(from_revision, to_revision)
        episode_ids = {
            episode_id
            for summary in summaries
            if isinstance(episode_id := summary.get("episode_id"), str)
        }
        episodes = {
            episode_id: _history_episode_decoration(store, project_id, episode_id)
            for episode_id in episode_ids
        }
        return [
            {
                **summary,
                "episode": episodes.get(summary.get("episode_id")),
            }
            for summary in summaries
        ]

    @projects_router.get("/api/projects/{project_id}/sources")
    def sources(project_id: str, refresh: bool = False):
        service = _project_service(catalog, project_id)
        return service.index_snapshot(refresh=refresh).model_dump(mode="json")

    @projects_router.post("/api/projects/{project_id}/sync")
    def sync_graph(project_id: str, body: GraphSyncRequest, request: Request):
        authorized_by = require_patch_capable_identity(request)
        service = _project_service(catalog, project_id)
        try:
            if body.removed_node_ids:
                with experiment_operation_lock(project_id):
                    transition = service.sync_graph_transition(
                        body,
                        active_control_node_ids=store.active_experiment_control_ids(
                            project_id,
                            graph_target=GraphTargetRef(),
                        ),
                        authorized_by=authorized_by,
                    )
            else:
                transition = service.sync_graph_transition(
                    body,
                    active_control_node_ids=store.active_experiment_control_ids(
                        project_id,
                        graph_target=GraphTargetRef(),
                    ),
                    authorized_by=authorized_by,
                )
            if transition is None:
                current = service.history.current_materialization()
                head = service.history.head_ref(current)
                projection = current_project_projection(
                    current.state,
                    transition_id=head.transition_id,
                    target=head.target,
                )
            else:
                projection = transition.projection
            state = projection.graph
            evaluate_graph_wake_boundary(project_id, state, source="human Sync")
            payload = state.model_dump(mode="json")
            payload.update(
                project_transition_payload(
                    project_id,
                    projection,
                    reconcile_operational=True,
                )
            )
            return payload
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"Missing graph object: {exc.args[0]}"
            ) from exc
        except NodeEditConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PatchRejected:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @projects_router.get("/api/projects/{project_id}/transition-manifest")
    def graph_transition_manifest(project_id: str):
        _project_service(catalog, project_id)
        return transition_trigger_manifest().model_dump(mode="json")

    @projects_router.post("/api/projects/{project_id}/sync/preview")
    def preview_graph_sync(project_id: str, body: GraphSyncRequest, request: Request):
        authorized_by = require_patch_capable_identity(request)
        service = _project_service(catalog, project_id)
        try:
            prepared = service.preview_sync_graph(
                body,
                active_control_node_ids=store.active_experiment_control_ids(
                    project_id,
                    graph_target=GraphTargetRef(),
                ),
                authorized_by=authorized_by,
            )
            assert prepared.patch.transition is not None
            return {
                "projection": project_transition_payload(
                    project_id,
                    prepared.projection,
                    reconcile_operational=False,
                ),
                "transition": prepared.patch.transition.model_dump(mode="json"),
            }
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail=f"Missing graph object: {exc.args[0]}",
            ) from exc
        except RevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except NodeEditConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PatchRejected:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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

    @projects_router.post("/api/projects/{project_id}/tasks/{kind}", status_code=202)
    def start_agent_task(
        project_id: str,
        kind: AgentTaskKind,
        body: dict[str, object],
        http_request: Request,
    ) -> dict[str, object]:
        if kind in {"auto_research", "branch_merge", "episode_report"}:
            raise HTTPException(
                status_code=405,
                detail="Use the project episode endpoint for Auto-research and branch merge.",
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

    @projects_router.post(
        "/api/projects/{project_id}/experiments/{node_id:path}/run", status_code=202
    )
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
                runtime, control = _experiment_control(
                    store,
                    project_id,
                    state,
                    node_id,
                    graph_target=GraphTargetRef(),
                )
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
                    else store.completed_experiment_watcher_group(
                        project_id,
                        node_id,
                        graph_target=GraphTargetRef(),
                    )
                )
                if pending_group is not None:
                    experiment_request = experiment_watcher_delivery_request(
                        pending_group,
                        trigger="experiment_run",
                        episode_id=episode_id,
                        invocation=1,
                        invocation_ceiling=node.invocation_ceiling,
                        control_revision=state.revision,
                        decision_bundle=control.governing_decisions,
                        completion_criteria=list(node.completion_criteria),
                    )
                    experiment_request = experiment_request.model_copy(
                        update={
                            "run_truth_scope": supplied.run_truth_scope,
                            "chat_scope": "node",
                            "node_id": node_id,
                            "message": experiment_start_message(supplied.message, node_id),
                            "chat_id": supplied.chat_id,
                            "session_id": None,
                        }
                    )
                    experiment_request = resolve_experiment_node_work_request(
                        service, experiment_request
                    )
                    record = background_tasks.start_watcher_notification(
                        project_id,
                        "node_chat",
                        experiment_request,
                        [item.watcher_id for item in pending_group],
                        authorized_by=authorized_by,
                    )
                    if record is None:
                        raise ValueError(
                            "The pending watcher completion could not be claimed because its "
                            "conversation is active."
                        )
                    return record.model_dump(mode="json")
                experiment_request = fresh_experiment_run_request(
                    service,
                    supplied,
                    node=node,
                    state_revision=state.revision,
                    control=control,
                    episode_id=episode_id,
                )
                record = background_tasks.start(
                    project_id,
                    "node_chat",
                    experiment_request,
                    authorized_by=authorized_by,
                )
        except ValueError as exc:
            status = 409 if "already running" in str(exc) else 422
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return record.model_dump(mode="json")

    @projects_router.get("/api/projects/{project_id}/tasks")
    def agent_tasks(project_id: str) -> list[dict[str, object]]:
        _require_registered_project(catalog, project_id)
        return [record.model_dump(mode="json") for record in store.agent_tasks(project_id)]

    @projects_router.get("/api/projects/{project_id}/usage", response_model=AgentUsageSnapshot)
    def agent_usage(project_id: str) -> AgentUsageSnapshot:
        _require_registered_project(catalog, project_id)
        return store.agent_usage_snapshot(project_id)

    @projects_router.get("/api/projects/{project_id}/watchers")
    def project_watchers(project_id: str) -> list[dict[str, object]]:
        _require_registered_project(catalog, project_id)
        return [record.model_dump(mode="json") for record in store.watchers(project_id)]

    @projects_router.post("/api/projects/{project_id}/watchers/{watcher_id}/check")
    def check_watcher_now(project_id: str, watcher_id: str) -> dict[str, object]:
        _require_registered_project(catalog, project_id)
        try:
            watcher = watcher_poller.check_now(project_id, watcher_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Watcher not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return watcher.model_dump(mode="json")

    @projects_router.post("/api/projects/{project_id}/watchers/{watcher_id}/stop")
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

    @projects_router.post("/api/projects/{project_id}/experiments/{node_id:path}/watchers/stop")
    def stop_experiment_watchers(project_id: str, node_id: str) -> list[dict[str, object]]:
        """Reject the retired bulk watcher control in favor of graceful Stop loop."""

        _require_registered_project(catalog, project_id)
        raise HTTPException(
            status_code=409,
            detail="This control was retired. Use Stop loop for the current Experiment episode.",
        )

    def stop_bound_experiment_episode(
        project_id: str,
        episode: EpisodeRecord,
    ) -> ExperimentControlState:
        """Stop one exact current loop against the graph target it actually controls."""

        node_id = episode.control_node_id
        if episode.mode != "experiment_loop" or node_id is None:
            raise HTTPException(status_code=409, detail="This is not an Experiment-loop episode.")
        main_service = _project_service(catalog, project_id)
        try:
            target_service = (
                main_service
                if episode.graph_target.kind == "main"
                else main_service.for_graph_target(
                    episode.graph_target,
                    expected_episode_id=episode.graph_target.branch_id,
                )
            )
            materialization = target_service.history.current_materialization()
            state = materialization.state
            head = target_service.history.head_ref(materialization)
        except (KeyError, OSError, StateUnavailable, ValueError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if head.target != episode.graph_target:
            raise HTTPException(
                status_code=409,
                detail="The Experiment episode no longer resolves to its exact graph target.",
            )
        if not isinstance(state.nodes.get(node_id), Experiment):
            raise HTTPException(status_code=404, detail="Experiment not found")
        if episode.graph_target.kind == "branch":
            route = store.auto_research_child_experiment(episode.episode_id)
            if route is None or route.auto_research_episode_id != episode.graph_target.branch_id:
                raise HTTPException(
                    status_code=409,
                    detail="The branch Experiment lost its Auto-research parent binding.",
                )
        runtime = store.experiment_loop_runtime_for_target(
            project_id,
            node_id,
            episode.graph_target,
        )
        if runtime.episode_id != episode.episode_id:
            raise HTTPException(
                status_code=409,
                detail="Only the current exact Experiment episode can be stopped.",
            )
        try:
            store.request_experiment_loop_stop(
                project_id,
                node_id,
                episode_id=episode.episode_id,
                graph_target=episode.graph_target,
            )
        except EpisodeNotRunning as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _, control = _experiment_control_for_target(
            store,
            project_id,
            state,
            node_id,
            graph_target=episode.graph_target,
        )
        return control

    # Registered after `.../watchers/stop`: `{node_id:path}` is greedy, so this
    # route would otherwise swallow that one with a node id ending in
    # "/watchers".
    @projects_router.post("/api/projects/{project_id}/experiments/{node_id:path}/stop")
    def stop_experiment_loop(
        project_id: str,
        node_id: str,
        request: Request,
        episode_id: str | None = Query(default=None),
    ) -> dict[str, object]:
        """Finish the current turn, then disable automatic continuation.

        The stop is durable before this returns, so no unclaimed watcher can win
        a wake afterwards. It never cancels the live task, kills external work,
        deletes a watcher, or changes what the Experiment means, and calling it
        again changes nothing.
        """

        require_patch_capable_identity(request)
        with experiment_operation_lock(project_id):
            if episode_id is None:
                runtime = store.experiment_loop_runtime(
                    project_id,
                    node_id,
                )
                episode = (
                    store.episode(runtime.episode_id) if runtime.episode_id is not None else None
                )
                if episode is None or episode.project_id != project_id:
                    raise HTTPException(status_code=404, detail="Experiment episode not found")
                if episode.graph_target != GraphTargetRef():
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "This Experiment runs on an episode graph branch; select its exact "
                            "episode before stopping the loop."
                        ),
                    )
            else:
                episode = _episode_for_http(store, catalog, project_id, episode_id)
                if episode.control_node_id != node_id:
                    raise HTTPException(status_code=404, detail="Experiment episode not found")
            control = stop_bound_experiment_episode(project_id, episode)
        return control.model_dump(mode="json")

    @projects_router.get(
        "/api/projects/{project_id}/episodes",
        response_model=list[EpisodeResponse],
    )
    def episodes(
        project_id: str,
        mode: Literal["auto_research", "experiment_loop"] | None = None,
    ) -> list[EpisodeResponse]:
        _require_registered_project(catalog, project_id)
        return serialize_episodes(
            store,
            project_id,
            mode=mode,
            branch_summaries=graph_branch_summaries,
        )

    @projects_router.post(
        "/api/projects/{project_id}/episodes",
        response_model=EpisodeResponse,
        status_code=202,
    )
    def start_episode(
        project_id: str,
        body: StartEpisodeBody,
        request: Request,
    ) -> EpisodeResponse:
        authorized_by = require_patch_capable_identity(request)
        service = _project_service(catalog, project_id)
        try:
            start_request = _resolved_auto_research_start_request(service, body)
            service.history.require_writable()
            graph_base_head = service.history.head_ref()
            episode, _ = background_tasks.start_auto_research(
                project_id,
                start_request,
                authorized_by=authorized_by,
                graph_base_head=graph_base_head,
                ensure_graph_target=ensure_auto_research_graph_target,
            )
            return serialize_episode(
                store,
                project_id,
                episode,
                branch_summary=graph_branch_summary,
            )
        except ValueError as exc:
            live = any(
                episode.mode == "auto_research"
                and episode.status in {"queued", "running", "stopping", "wrapping_up"}
                for episode in store.episodes(project_id)
            )
            status = 409 if live else 422
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @projects_router.post(
        "/api/projects/{project_id}/episodes/{episode_id}/stop",
        response_model=EpisodeResponse,
    )
    def stop_episode(
        project_id: str,
        episode_id: str,
        request: Request,
    ) -> EpisodeResponse:
        require_patch_capable_identity(request)
        episode = _episode_for_http(store, catalog, project_id, episode_id)
        if episode.mode == "auto_research":
            try:
                background_tasks.stop_auto_research(episode.episode_id)
                settle_auto_research_stop(store, episode.episode_id)
            except EpisodeNotRunning as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        elif episode.mode == "experiment_loop":
            with experiment_operation_lock(project_id):
                stop_bound_experiment_episode(project_id, episode)
        else:
            raise HTTPException(status_code=409, detail="This episode cannot be stopped.")
        current = store.episode(episode.episode_id)
        if current is None:
            raise RuntimeError("The stopped episode could not be reloaded.")
        return serialize_episode(
            store,
            project_id,
            current,
            branch_summary=graph_branch_summary,
        )

    @projects_router.post(
        "/api/projects/{project_id}/episodes/{episode_id}/merge",
        response_model=EpisodeResponse,
        status_code=202,
    )
    def merge_episode_branch(
        project_id: str,
        episode_id: str,
        request: Request,
    ) -> EpisodeResponse:
        authorized_by = require_patch_capable_identity(request)
        episode = _episode_for_http(store, catalog, project_id, episode_id)
        if episode.mode != "auto_research" or episode.graph_target.kind != "branch":
            raise HTTPException(
                status_code=409,
                detail="Only an Auto-research graph branch can merge to main.",
            )
        service = _project_service(catalog, project_id)
        try:
            summary = graph_branch_summary(episode)
            if not summary.merge_eligible:
                raise ValueError(
                    "This graph branch is active, unchanged, already merged, or otherwise "
                    "not merge eligible."
                )
            service.history.require_writable()
            merge_request = _resolved_branch_merge_request(service, episode.episode_id)
            background_tasks.start_branch_merge(
                project_id,
                merge_request,
                authorized_by=authorized_by,
            )
            current = store.episode(episode.episode_id)
            if current is None:
                raise RuntimeError("The branch merge episode could not be reloaded.")
            return serialize_episode(
                store,
                project_id,
                current,
                branch_summary=graph_branch_summary,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @projects_router.post(
        "/api/projects/{project_id}/episodes/{episode_id}/reauthorize",
        response_model=EpisodeResponse,
        status_code=202,
    )
    def reauthorize_episode(
        project_id: str,
        episode_id: str,
        body: ReauthorizeEpisodeBody,
        request: Request,
    ) -> EpisodeResponse:
        authorized_by = require_patch_capable_identity(request)
        episode = _episode_for_http(store, catalog, project_id, episode_id)
        if not (
            episode.mode == "auto_research"
            and episode.status == "needs_action"
            and episode.ending == "exhausted"
            and episode.wrapup_state in {"ready", "failed", "legacy_unavailable"}
        ):
            raise HTTPException(
                status_code=409,
                detail="Only an exhausted, settled Auto-research episode can be reauthorized.",
            )
        state = store.auto_research_state(episode.episode_id)
        if state is None:
            raise HTTPException(status_code=409, detail="Auto-research state is unavailable.")
        service = _project_service(catalog, project_id)
        try:
            service.history.require_writable()
            start_request = _resolved_auto_research_start_request(
                service,
                StartEpisodeBody(
                    mode="auto_research",
                    invocation_ceiling=body.invocation_ceiling,
                    starting_instruction=state.starting_instruction,
                ),
            )
            graph_base_head = service.history.head_ref()
            fresh, _ = background_tasks.start_auto_research(
                project_id,
                start_request,
                authorized_by=authorized_by,
                graph_base_head=graph_base_head,
                ensure_graph_target=ensure_auto_research_graph_target,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return serialize_episode(
            store,
            project_id,
            fresh,
            branch_summary=graph_branch_summary,
        )

    @projects_router.get(
        "/api/projects/{project_id}/episodes/{episode_id}/messages",
        response_model=list[AutoResearchMessageRecord],
    )
    def episode_messages(
        project_id: str,
        episode_id: str,
    ) -> list[AutoResearchMessageRecord]:
        episode = _episode_for_http(store, catalog, project_id, episode_id)
        if episode.mode != "auto_research":
            raise HTTPException(status_code=409, detail="This episode has no Auto-research mail.")
        return store.auto_research_messages(episode.episode_id)

    @projects_router.post(
        "/api/projects/{project_id}/episodes/{episode_id}/messages",
        response_model=AutoResearchMessageRecord,
        status_code=201,
    )
    def send_episode_message(
        project_id: str,
        episode_id: str,
        body: EpisodeMessageBody,
        request: Request,
    ) -> AutoResearchMessageRecord:
        authorized_by = require_patch_capable_identity(request)
        episode = _episode_for_http(store, catalog, project_id, episode_id)
        if episode.mode != "auto_research":
            raise HTTPException(status_code=409, detail="This episode has no Auto-research mail.")
        if episode.status != "running" or episode.ending is not None:
            raise HTTPException(status_code=409, detail="Episode is not accepting new mail")
        if episode.root_operation_id is None:
            raise HTTPException(status_code=409, detail="Episode orchestrator is unavailable")
        try:
            saved = record_auto_research_message(
                store,
                episode_id=episode.episode_id,
                sender_role="human",
                sender_task_id=None,
                authorized_by=authorized_by,
                recipient_task_id=episode.root_operation_id,
                body=body.body,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            started = deliver_pending_auto_research_lifecycle(
                background_tasks,
                episode_id=episode.episode_id,
            )
            if started is None:
                deliver_pending_auto_research_mail(
                    background_tasks,
                    episode_id=episode.episode_id,
                    recipient_task_id=episode.root_operation_id,
                )
        except Exception as exc:
            logger.warning(
                "Could not deliver durable Auto-research message %s immediately: %s",
                saved.message_id,
                exc,
            )
        current = store.auto_research_message(saved.message_id)
        if current is None:
            raise RuntimeError("The durable episode message could not be reloaded.")
        return current

    @projects_router.get("/api/projects/{project_id}/episodes/{episode_id}/report/preview")
    @projects_router.head("/api/projects/{project_id}/episodes/{episode_id}/report/preview")
    def preview_episode_report(
        project_id: str,
        episode_id: str,
        request: Request,
    ) -> Response:
        episode = _episode_for_http(store, catalog, project_id, episode_id)
        report = None if episode.ending == "stopped" else store.episode_report(episode.episode_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Episode report not found")
        try:
            document, csp = html_preview_document(
                report.html.encode("utf-8"),
            )
        except (UnicodeError, ValueError) as exc:
            raise HTTPException(status_code=410, detail="Episode report unavailable") from exc
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

    @projects_router.get(
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

    @projects_router.get("/api/projects/{project_id}/result-views/{view_id}/preview")
    @projects_router.head("/api/projects/{project_id}/result-views/{view_id}/preview")
    async def preview_result_view(
        project_id: str,
        view_id: str,
        request: Request,
    ) -> Response:
        _require_registered_project(catalog, project_id)
        _, data = await asyncio.to_thread(
            _load_visible_result_view_bytes,
            store,
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

    @projects_router.post(
        "/api/projects/{project_id}/result-views/{view_id}/keep",
        response_model=ResultViewDescriptor,
    )
    def keep_result_view(project_id: str, view_id: str) -> ResultViewDescriptor:
        _require_registered_project(catalog, project_id)
        with result_view_keep_lock(view_id):
            record = _visible_result_view_record(store, project_id, view_id)
            if record.kept_filename is not None:
                return store.result_view_descriptor(record)
            if store.has_active_result_view_revision(record):
                raise HTTPException(
                    status_code=409,
                    detail="Wait for the active result view revision before keeping it.",
                )
            data = _read_result_view_bytes_for_http(store, record)
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

    @projects_router.get("/api/projects/{project_id}/tasks/{operation_id}")
    def agent_task(project_id: str, operation_id: str) -> dict[str, object]:
        _require_registered_project(catalog, project_id)
        record = store.agent_task(operation_id)
        if record is None or record.project_id != project_id or not record.visible:
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

    @projects_router.get(
        "/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/preview"
    )
    @projects_router.head(
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

    @projects_router.get(
        "/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/download"
    )
    @projects_router.head(
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

    @projects_router.post("/api/projects/{project_id}/tasks/{operation_id}/pause", status_code=202)
    def pause_agent_task(project_id: str, operation_id: str) -> dict[str, object]:
        _project_service(catalog, project_id)
        record = store.agent_task(operation_id)
        if record is None or record.project_id != project_id or not record.visible:
            raise HTTPException(status_code=404, detail="Agent task not found")
        try:
            return background_tasks.pause(operation_id).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @projects_router.post("/api/projects/{project_id}/tasks/{operation_id}/resume", status_code=202)
    def resume_agent_task(
        project_id: str,
        operation_id: str,
        request: Request,
    ) -> dict[str, object]:
        previous = store.agent_task(operation_id)
        if previous is None or previous.project_id != project_id or not previous.visible:
            raise HTTPException(status_code=404, detail="Agent task not found")
        if previous.kind == "branch_merge":
            raise HTTPException(
                status_code=409,
                detail="Dispatch a new Merge to main task from the episode detail.",
            )
        authorized_by = require_patch_capable_identity(request)
        service = _project_service(catalog, project_id)
        result_view_resume_lock: threading.Lock | None = None
        try:
            if previous.kind not in {"paper_coach", "auto_research"}:
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

    @projects_router.post(
        "/api/projects/{project_id}/tasks/{operation_id}/repair-graph-update",
        status_code=202,
    )
    def repair_agent_task_graph_update(
        project_id: str,
        operation_id: str,
        request: Request,
    ) -> dict[str, object]:
        previous = store.agent_task(operation_id)
        if previous is None or previous.project_id != project_id or not previous.visible:
            raise HTTPException(status_code=404, detail="Agent task not found")
        if previous.kind == "branch_merge":
            raise HTTPException(
                status_code=409,
                detail="Dispatch a new Merge to main task from the episode detail.",
            )
        authorized_by = (
            require_patch_capable_identity(request)
            if task_graph_capable(previous.kind, previous.request)
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

    @projects_router.post("/api/projects/{project_id}/tasks/{operation_id}/retry", status_code=202)
    def retry_agent_task(
        project_id: str,
        operation_id: str,
        request: Request,
        body: RetryAgentTaskRequest | None = None,
    ) -> dict[str, object]:
        previous = store.agent_task(operation_id)
        if previous is None or previous.project_id != project_id or not previous.visible:
            raise HTTPException(status_code=404, detail="Agent task not found")
        if previous.kind == "branch_merge":
            raise HTTPException(
                status_code=409,
                detail="Dispatch a new Merge to main task from the episode detail.",
            )
        authorized_by = require_patch_capable_identity(request)
        service = _project_service(catalog, project_id)
        result_view_retry_lock: threading.Lock | None = None
        try:
            overrides = body.model_dump(exclude_none=True) if body is not None else {}
            if previous.request.get("patch_kind") == "experiment_loop" and "run_on" in overrides:
                raise ValueError(
                    "Experiment-loop recovery cannot change its pinned execution machine."
                )
            if previous.kind == "auto_research":
                candidate = AutoResearchRunRequest.model_validate({**previous.request, **overrides})
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
                if previous.kind == "auto_research":
                    _require_auto_research_retry_target_ready(
                        service,
                        AutoResearchRunRequest.model_validate(candidate),
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

    app.include_router(projects_router)
    app.include_router(chats_router)
    app.include_router(paper_router)

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


def _load_visible_result_view_bytes(
    store: AppStore,
    project_id: str,
    view_id: str,
) -> tuple[ResultViewRecord, bytes]:
    record = _visible_result_view_record(store, project_id, view_id)
    return record, _read_result_view_bytes_for_http(store, record)


def _read_result_view_bytes_for_http(
    store: AppStore,
    record: ResultViewRecord,
) -> bytes:
    try:
        return store.result_view_bytes(
            record.view_id,
            expected_content_sha256=record.content_sha256,
        )
    except (FileNotFoundError, OSError, StateUnavailable) as exc:
        raise HTTPException(status_code=503, detail="Result view storage unavailable") from exc
    except (KeyError, ResultViewConflict, ValueError) as exc:
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
    *,
    graph_target: GraphTargetRef,
) -> tuple[ExperimentLoopRuntime, ExperimentControlState]:
    """Derive one Experiment's operational and semantic control state together.

    Deriving is also where a graceful stop is reconciled, so the same joint
    handoff settles identically after a restart without anyone replaying it.
    """

    runtime = store.experiment_loop_runtime(
        project_id,
        experiment_id,
        graph_target=graph_target,
    )
    if runtime.stop_requested and not runtime.stop_settled and not runtime.task_active:
        store.settle_experiment_loop_stop(
            project_id,
            experiment_id,
            episode_id=runtime.episode_id,
            graph_target=graph_target,
        )
        runtime = store.experiment_loop_runtime(
            project_id,
            experiment_id,
            graph_target=graph_target,
        )
    return runtime, _experiment_control_from_runtime(state, experiment_id, runtime)


def _experiment_control_for_target(
    store: AppStore,
    project_id: str,
    state: GraphState,
    experiment_id: str,
    *,
    graph_target: GraphTargetRef,
) -> tuple[ExperimentLoopRuntime, ExperimentControlState]:
    """Derive and reconcile one exact target-bound operational runtime."""

    runtime = store.experiment_loop_runtime_for_target(
        project_id,
        experiment_id,
        graph_target,
    )
    if runtime.stop_requested and not runtime.stop_settled and not runtime.task_active:
        store.settle_experiment_loop_stop(
            project_id,
            experiment_id,
            episode_id=runtime.episode_id,
            graph_target=graph_target,
        )
        runtime = store.experiment_loop_runtime_for_target(
            project_id,
            experiment_id,
            graph_target,
        )
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


def _history_episode_decoration(
    store: AppStore,
    project_id: str,
    episode_id: str,
) -> dict[str, object] | None:
    episode = store.episode(episode_id)
    if episode is None or episode.project_id != project_id:
        return None
    report = None if episode.ending == "stopped" else store.episode_report(episode_id)
    return {
        "mode": episode.mode,
        "status": episode.status,
        "ending": episode.ending,
        "wrapup_state": episode.wrapup_state,
        "report": (
            {
                "report_id": report.report_id,
                "ending": report.ending,
                "created_at": report.created_at,
            }
            if report is not None
            else None
        ),
    }


def _episode_for_http(
    store: AppStore,
    catalog: ProjectCatalog,
    project_id: str,
    episode_id: str,
) -> EpisodeRecord:
    _require_registered_project(catalog, project_id)
    try:
        return episode_for_project(store, project_id, episode_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Episode not found") from exc


def _resolved_auto_research_start_request(
    service: ProjectService,
    body: StartEpisodeBody,
) -> AutoResearchStartRequest:
    profile = service.resolve_agent_profile("orchestrator")
    request = AutoResearchStartRequest(
        invocation_ceiling=body.invocation_ceiling,
        starting_instruction=body.starting_instruction,
        provider=profile.provider,
        model=profile.model,
        reasoning=profile.reasoning,
        run_on=profile.run_on,
        run_truth_scope=list(service.manifest.agent.default_run_truth_scope),
    )
    resolved = service.resolve_skill_request(cast(RunRequest, request))
    if not isinstance(resolved, AutoResearchStartRequest):
        raise TypeError("Auto-research skill resolution changed the start request type.")
    return resolved


def _resolved_branch_merge_request(
    service: ProjectService,
    episode_id: str,
) -> BranchMergeRunRequest:
    profile = service.resolve_agent_profile("orchestrator")
    return BranchMergeRunRequest(
        episode_id=episode_id,
        provider=profile.provider,
        model=profile.model,
        reasoning=profile.reasoning,
        run_on=profile.run_on,
        run_truth_scope=sorted(set(service.manifest.agent.default_run_truth_scope)),
        chat_scope="project",
        mode="work",
        trigger="human",
        patch_kind="work",
    )


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


def _resolved_auto_research_request(
    service: ProjectService,
    request: AutoResearchRunRequest,
) -> AutoResearchRunRequest:
    if (
        request.provider is None
        or request.model is None
        or request.reasoning is None
        or request.run_on is None
        or request.run_truth_scope is None
    ):
        raise ValueError("Auto-research recovery requires its exact pinned execution profile.")
    profile_for(request.provider)
    if request.run_on not in service.manifest.machine_map:
        raise ValueError(f"unknown execution machine: {request.run_on}")
    skill_resolved = service.resolve_skill_request(cast(RunRequest, request))
    if not isinstance(skill_resolved, AutoResearchRunRequest):
        raise TypeError("Auto-research skill resolution changed the task request type.")
    return skill_resolved


def _require_auto_research_retry_target_ready(
    service: ProjectService,
    request: AutoResearchRunRequest,
) -> None:
    """Recheck the pinned provider target before Retry can allocate a child task."""

    if request.provider is None or request.run_on is None:
        raise ValueError("Auto-research Retry requires its pinned provider and execution machine.")
    machine = service.manifest.machine_map.get(request.run_on)
    if machine is None:
        raise ValueError(f"unknown execution machine: {request.run_on}")
    binary = machine.provider_paths.get(request.provider)
    readiness = service.launcher.readiness(
        request.provider,
        host=machine.host,
        binary=binary,
        refresh=True,
    )
    if readiness.installed and readiness.authenticated:
        return
    diagnostic = (
        readiness.reason or f"{request.provider} is not ready on {request.run_on}"
    ).strip()
    if diagnostic.endswith("."):
        diagnostic = diagnostic[:-1]
    raise ValueError(
        f"Auto-research Retry cannot start: {diagnostic}. The current task was left unchanged."
    )


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

    if kind == "auto_research":
        auto_research_request = AutoResearchRunRequest.model_validate(body)
        resolved_auto_research = _resolved_auto_research_request(
            service,
            auto_research_request,
        )
        return service.resolve_skill_selection(cast(RunRequest, resolved_auto_research))
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
