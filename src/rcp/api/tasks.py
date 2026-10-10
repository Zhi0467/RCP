from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Mapping
from contextlib import nullcontext
from pathlib import Path
from typing import Annotated, Literal, cast
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import UUID4, BaseModel, ConfigDict, Field, field_validator

from rcp.api.dependencies import (
    get_artifact_mutation_locks,
    get_attachment_store,
    get_background_tasks,
    get_catalog,
    get_experiment_admission,
    get_graph_service,
    get_identity_access,
    get_project_service,
    get_store,
    get_watcher_delivery,
    project_write_admission,
    require_project_membership,
    require_project_write_admission,
    require_registered_project,
)
from rcp.api.graph_changes import require_graph_edit_admission
from rcp.api.identity import IdentityAccess
from rcp.api.task_requests import _resolved_auto_research_request, _resolved_graph_request
from rcp.artifact_comments import comment_panel, selection_frame_addon, supports_comments
from rcp.artifact_views import artifact_content, artifact_viewer_document
from rcp.artifacts import (
    AgentArtifactDescriptor,
    ArtifactView,
    artifact_view,
    classify_artifact_bytes,
)
from rcp.artifacts import (
    artifact_id as scoped_artifact_id,
)
from rcp.attachments import ChatAttachmentStore
from rcp.background import AgentTaskRequest, BackgroundAgentTasks
from rcp.conversation_worktrees import conversation_worktree_recovery_admission
from rcp.keyed_locks import ExperimentAdmission, KeyedLocks
from rcp.limits import STEERING_MESSAGE_MAX_CHARS
from rcp.project_references import resolve_project_references
from rcp.projects import ProjectCatalog
from rcp.runs.auto_research import AutoResearchRunRequest
from rcp.runs.chat import (
    _logical_chat_turn_operation_id,
    artifact_omissions,
)
from rcp.runs.chat_admission import admit_fresh_chat_turn
from rcp.runs.steering import (
    begin_chat_steer,
    chat_steer_action_label,
    chat_steering_state,
    chat_steering_visible,
    finish_chat_steer,
)
from rcp.runs.task_policy import load_stored_request, task_graph_capable
from rcp.runs.tasks.coach import _resolved_coach_request
from rcp.runs.tasks.work_apply_again import apply_again_refusal, apply_work_graph_update_again
from rcp.service import ChatMessage, CoachRequest, ProjectService, RunRequest
from rcp.skill_registry import SkillSelection
from rcp.storage import (
    AgentTaskAdmissionConflict,
    AgentTaskKind,
    AgentTaskReceiptRecord,
    AgentTaskRecord,
    AppStore,
)
from rcp.storage.client_requests import ClientRequestConflict
from rcp.transport import StateUnavailable
from rcp.watchers import WatcherDelivery

router = APIRouter(dependencies=[Depends(require_project_membership)])

CatalogDependency = Annotated[ProjectCatalog, Depends(get_catalog)]
IdentityDependency = Annotated[IdentityAccess, Depends(get_identity_access)]
StoreDependency = Annotated[AppStore, Depends(get_store)]
AttachmentStoreDependency = Annotated[ChatAttachmentStore, Depends(get_attachment_store)]
BackgroundTasksDependency = Annotated[BackgroundAgentTasks, Depends(get_background_tasks)]
ExperimentAdmissionDependency = Annotated[
    ExperimentAdmission,
    Depends(get_experiment_admission),
]
WatcherDeliveryDependency = Annotated[WatcherDelivery, Depends(get_watcher_delivery)]


ArtifactMutationLocksDependency = Annotated[KeyedLocks, Depends(get_artifact_mutation_locks)]


class RetryAgentTaskRequest(BaseModel):
    provider: str | None = None
    model: str | None = None
    reasoning: str | None = None
    run_on: str | None = None


class SteerAgentTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: uuid.UUID
    attempt: int = Field(ge=1, strict=True)
    expected_turn_id: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=STEERING_MESSAGE_MAX_CHARS)

    @field_validator("message")
    @classmethod
    def require_nonblank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A steering message must not be blank.")
        return value


class AgentArtifactResponse(AgentArtifactDescriptor):
    """One stored descriptor plus backend-owned availability decisions."""

    view: ArtifactView
    available: bool
    unavailable_reason: str | None
    can_open: bool
    can_download: bool
    can_keep: bool
    can_discuss: bool


def _agent_artifact_response(
    store: AppStore,
    record: AgentTaskRecord,
    descriptor: AgentArtifactDescriptor,
) -> AgentArtifactResponse:
    stored = store.artifact(descriptor.artifact_id)
    kept = stored is not None and stored.kept_at is not None
    if stored is not None:
        descriptor = descriptor.model_copy(update={"kept_at": stored.kept_at})
    available = stored is not None and (
        stored.expires_at is None
        or stored.expires_at > store.now()
        or descriptor.artifact_id
        in (store.protected_edit_artifact_ids() | store.legacy_artifact_import_ids())
    )
    unavailable_reason = (
        None
        if available
        else "Artifact bytes were not retained with this task history."
        if record.history_only
        else "Artifact bytes are no longer available."
    )
    if stored is None:
        status = store.artifact_import_status(descriptor.artifact_id)
        unavailable_reason = (
            status["reason"] if status and status["reason"] else "Artifact import is pending."
        )
    can_discuss = (
        available
        and supports_comments(descriptor.media_type)
        and isinstance(record.request.get("chat_id"), str)
        and not record.history_only
        and bool(record.native_session_id)
        and bool(record.stage_root)
    )
    return AgentArtifactResponse(
        **descriptor.model_dump(mode="python"),
        available=available,
        unavailable_reason=unavailable_reason,
        view=artifact_view(descriptor.media_type),
        can_open=available and artifact_view(descriptor.media_type) not in {"pdf", "file"},
        can_download=available,
        can_keep=available and not kept and not record.history_only,
        can_discuss=can_discuss,
    )


def _agent_artifact_response_json(response: AgentArtifactResponse) -> dict[str, object]:
    return response.model_dump(mode="json")


def _agent_task_response(
    store: AppStore,
    record: AgentTaskRecord,
    background_tasks: BackgroundAgentTasks,
    degradations: Mapping[str, str] | None = None,
    discoveries: Mapping[str, AgentTaskReceiptRecord] | None = None,
    chat_sessions: dict[tuple[str, str], str | None] | None = None,
) -> dict[str, object]:
    response = record.model_dump(mode="json")
    consolidation = store.consolidation_run_for_operation(record.operation_id) is not None
    if consolidation:
        response.update(can_resume=False, can_retry=False)
    if record.kind in {"node_chat", "project_chat"}:
        chat_id = record.request.get("chat_id")
        session_id = None
        if isinstance(chat_id, str):
            # A list shares one lookup per chat; a single task reads its own.
            cache = chat_sessions if chat_sessions is not None else {}
            key = (record.kind, chat_id)
            if key not in cache:
                cache[key] = _current_chat_session_id(store, record.project_id, *key)
            session_id = cache[key]
        response["current_chat_session_id"] = session_id
    steering = chat_steering_state(background_tasks, record)
    response.update(
        steer_visible=chat_steering_visible(store, record),
        can_steer=steering.can_steer,
        steer_action_label=chat_steer_action_label(record),
        steer_unavailable_reason=steering.reason,
        steer_turn_id=steering.turn_id,
        steer_count=background_tasks.steer_count(record.operation_id),
        # The provider ran without part of what the launch asked for. Exported
        # here so no surface has to read exit receipts to learn it.
        degradation=(degradations or {}).get(record.operation_id),
        can_apply_again=not consolidation and _can_apply_again(store, record),
    )
    result = response.get("result")
    stored_artifacts = record.result.get("artifacts") if record.result else None
    if not isinstance(result, dict):
        return response
    receipt = (discoveries or {}).get(record.operation_id)
    if receipt is not None:
        result["artifact_omissions"] = artifact_omissions(receipt)
    if record.history_only or consolidation:
        graph_update = result.get("graph_update")
        if isinstance(graph_update, dict):
            graph_update["repairable"] = False
        graph_updates = result.get("graph_updates")
        if isinstance(graph_updates, list):
            for update in graph_updates:
                if isinstance(update, dict):
                    update["repairable"] = False
    if not isinstance(stored_artifacts, list):
        return response
    projected: list[object] = []
    for raw in stored_artifacts:
        try:
            descriptor = AgentArtifactDescriptor.model_validate(raw)
        except (TypeError, ValueError):
            projected.append(raw)
            continue
        projected.append(
            _agent_artifact_response_json(_agent_artifact_response(store, record, descriptor))
        )
    result["artifacts"] = projected
    return response


def _current_chat_session_id(
    store: AppStore, project_id: str, kind: str, chat_id: str
) -> str | None:
    current = store.current_chat_session(project_id, kind, chat_id)
    return (
        current.native_session_id
        if current is not None and not current.history_only and current.stage_root
        else None
    )


def _can_apply_again(store: AppStore, record: AgentTaskRecord) -> bool:
    graph_update = record.result.get("graph_update") if record.result else None
    if not isinstance(graph_update, dict) or graph_update.get("status") != "unavailable":
        return False
    return apply_again_refusal(store, record) is None


def _reject_history_only_control(record: AgentTaskRecord) -> None:
    if record.history_only:
        raise HTTPException(
            status_code=409,
            detail="This task is retained as history and cannot be controlled or continued.",
        )


@router.post(
    "/api/projects/{project_id}/tasks/{kind}",
    status_code=202,
    dependencies=[Depends(require_project_write_admission)],
)
def start_agent_task(
    project_id: str,
    kind: AgentTaskKind,
    body: dict[str, object],
    http_request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
    attachment_store: AttachmentStoreDependency,
    background_tasks: BackgroundTasksDependency,
    idempotency_key: Annotated[UUID4 | None, Header()] = None,
    branch_id: str | None = None,
) -> dict[str, object]:
    if body.get("references") and kind not in {"node_chat", "project_chat"}:
        raise HTTPException(status_code=422, detail="Project references require an ordinary chat.")
    if kind in {"auto_research", "branch_merge", "episode_report", "artifact_edit"}:
        raise HTTPException(
            status_code=405,
            detail="Use the project episode endpoint for Auto-research and branch merge.",
        )
    authorized_by = identity_access.require_patch_capable_identity(http_request)
    if idempotency_key is not None:
        project_id = catalog.resolve_project_id(project_id)
    try:
        with store.client_request_admission(
            project_id,
            authorized_by.user_id,
            str(idempotency_key) if idempotency_key is not None else None,
            f"tasks/{kind}",
            result_kind="operation",
        ) as existing:
            if existing is not None:
                record = store.agent_task(existing.operation_id or "")
                assert record is not None
                return _agent_task_response(store, record, background_tasks)
            service = get_graph_service(catalog, project_id, branch_id)
            if branch_id is not None and kind not in {"node_chat", "project_chat"}:
                raise HTTPException(
                    status_code=422,
                    detail="Only ordinary conversations can target a graph branch here.",
                )
            chat_admission_lock = None
            try:
                request = _validated_task_request(service, kind, body)
                if task_graph_capable(kind, request) and not (
                    isinstance(request, RunRequest) and request.artifact_context is not None
                ):
                    require_graph_edit_admission(
                        store, catalog.resolve_project_id(project_id), service.history.graph_target
                    )
                if isinstance(request, RunRequest):
                    request = _admit_artifact_context_request(
                        store,
                        service,
                        project_id,
                        kind,
                        request,
                    )

                if isinstance(request, RunRequest) and request.artifact_edit is not None:
                    kind = (
                        "artifact_edit"
                        if request.artifact_edit.launch_kind == "revoking"
                        else ("project_chat" if request.chat_scope == "project" else "node_chat")
                    )
                if kind in {"node_chat", "project_chat"}:
                    assert isinstance(request, RunRequest)
                if (
                    kind in {"node_chat", "project_chat"}
                    and isinstance(request, RunRequest)
                    and request.artifact_edit is None
                ):
                    assert request.chat_id is not None
                    chat_admission_lock = admit_fresh_chat_turn(service, store, project_id, request)
                    request = chat_admission_lock.__enter__()
                operation_id = (
                    request.artifact_edit.operation_id
                    if isinstance(request, RunRequest) and request.artifact_edit is not None
                    else str(uuid.uuid4())
                )
                claimed_set: tuple[str, str] | None = None
                if kind in {"node_chat", "project_chat"}:
                    assert isinstance(request, RunRequest)
                    supplied = (request.attachment_set_id, request.attachment_client_id)
                    if any(supplied) and not all(supplied):
                        raise ValueError(
                            "Chat attachments require both attachment_set_id and attachment_client_id."
                        )
                    if request.attachment_set_id or request.references:
                        assert request.chat_id is not None
                        claimed = attachment_store.claim(
                            project_id=project_id,
                            chat_id=request.chat_id,
                            client_id=request.attachment_client_id,
                            attachment_set_id=request.attachment_set_id,
                            operation_id=operation_id,
                            reference_files=resolve_project_references(
                                store, catalog, project_id, request.references
                            ),
                        )
                        claimed_set = (claimed.attachment_batch_id, operation_id)
                        request = request.model_copy(
                            update={
                                "attachment_set_id": None,
                                "attachment_client_id": None,
                                "attachment_batch_id": claimed.attachment_batch_id,
                                "attachments": claimed.attachments,
                                "references": [],
                            }
                        )
                try:
                    record = background_tasks.start(
                        project_id,
                        kind,
                        request,
                        operation_id=operation_id,
                        authorized_by=authorized_by,
                        graph_target=service.history.graph_target,
                    )
                except BaseException:
                    if claimed_set is not None and store.agent_task(operation_id) is None:
                        attachment_store.release(*claimed_set)
                    raise
            except AgentTaskAdmissionConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except ClientRequestConflict:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            finally:
                if chat_admission_lock is not None:
                    chat_admission_lock.__exit__(None, None, None)
            # A task admitted a moment ago has not run, so it can carry no note yet.
            return _agent_task_response(store, record, background_tasks)
    except ClientRequestConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/projects/{project_id}/client-requests/{key}")
def client_request(
    project_id: str,
    key: UUID4,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
) -> dict[str, str]:
    member = identity_access.acting_user(request)
    record = store.client_request(catalog.resolve_project_id(project_id), str(key))
    if record is None or record.user_id != member.user_id:
        raise HTTPException(status_code=404, detail="Client request not found")
    if record.operation_id is not None:
        found = {"route": record.route, "operation_id": record.operation_id}
        # An Experiment start admits its first turn; Resume watches the whole episode.
        task = store.agent_task(record.operation_id)
        if task is not None and task.episode_id is not None:
            found["episode_id"] = task.episode_id
        return found
    assert record.episode_id is not None
    return {"route": record.route, "episode_id": record.episode_id}


@router.get("/api/projects/{project_id}/tasks")
def agent_tasks(
    project_id: str,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    background_tasks: BackgroundTasksDependency,
    branch_id: str | None = None,
) -> list[dict[str, object]]:
    require_registered_project(catalog, project_id)
    target = (
        get_graph_service(catalog, project_id, branch_id, initialize=False).history.graph_target
        if branch_id is not None
        else None
    )
    records = store.agent_tasks(project_id, graph_target=target)
    degradations = store.agent_task_degradations([record.operation_id for record in records])
    discoveries = store.agent_task_artifact_discoveries([record.operation_id for record in records])
    chat_sessions: dict[tuple[str, str], str | None] = {}
    return [
        _agent_task_response(
            store, record, background_tasks, degradations, discoveries, chat_sessions
        )
        for record in records
    ]


@router.get("/api/projects/{project_id}/tasks/{operation_id}")
def agent_task(
    project_id: str,
    operation_id: str,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    background_tasks: BackgroundTasksDependency,
) -> dict[str, object]:
    require_registered_project(catalog, project_id)
    record = store.agent_task(operation_id)
    if record is None or record.project_id != project_id or not record.visible:
        raise HTTPException(status_code=404, detail="Agent task not found")
    detail = _agent_task_response(
        store,
        record,
        background_tasks,
        store.agent_task_degradations([operation_id]),
        store.agent_task_artifact_discoveries([operation_id]),
    )
    detail["events"] = [
        event.model_dump(mode="json") for event in store.agent_task_events(operation_id)
    ]
    detail["debug_receipts"] = [
        receipt.model_dump(mode="json") for receipt in store.agent_task_receipts(operation_id)
    ]
    detail["contracts"] = [
        contract.model_dump(mode="json") for contract in store.agent_task_contracts(operation_id)
    ]
    return detail


@router.post("/api/projects/{project_id}/tasks/{operation_id}/steer")
def steer_agent_task(
    project_id: str,
    operation_id: str,
    body: SteerAgentTaskRequest,
    http_request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
) -> ChatMessage:
    identity_access.require_patch_capable_identity(http_request)
    record = store.agent_task(operation_id)
    if record is None or record.project_id != project_id or not record.visible:
        raise HTTPException(status_code=404, detail="Agent task not found")
    service = get_graph_service(catalog, project_id, record.graph_target.branch_id)
    try:
        with project_write_admission(project_id, http_request):
            delivery = begin_chat_steer(
                service,
                background_tasks,
                record,
                message_id=str(body.message_id),
                attempt=body.attempt,
                expected_turn_id=body.expected_turn_id,
                text=body.message,
            )
        return finish_chat_steer(service, record, delivery)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, StateUnavailable) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/content")
@router.head("/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/content")
async def content_agent_artifact(
    project_id: str,
    operation_id: str,
    artifact_id: str,
    request: Request,
    version_id: str | None = None,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
) -> Response:
    require_registered_project(catalog, project_id)
    descriptor, data = await asyncio.to_thread(
        _load_agent_artifact,
        store,
        project_id,
        operation_id,
        artifact_id,
        "open",
    )
    try:
        if version_id is not None:
            data = store.read_artifact_bytes(artifact_id, version_id)
        document, media_type, csp = artifact_content(
            descriptor.name,
            descriptor.media_type,
            data,
            frame_addon=selection_frame_addon() if descriptor.media_type == "text/html" else None,
        )
    except (OSError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=410, detail="Preview unavailable") from exc
    return Response(
        b"" if request.method == "HEAD" else document,
        media_type=media_type,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": csp,
        },
    )


@router.get("/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/preview")
@router.head("/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/preview")
async def preview_agent_artifact(
    project_id: str,
    operation_id: str,
    artifact_id: str,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
) -> Response:
    """Keep the old desktop route on the unified shell after source updates."""

    # Retained clients also used this URL as the src for small inline images.
    # A browser image request is distinguishable from a viewer navigation by
    # its Accept header, so keep that bounded compatibility without restoring
    # the old raw-preview entrance for ordinary navigation.
    record = store.agent_task(operation_id)
    image_request = (
        record is not None
        and record.project_id == project_id
        and artifact_view(_agent_artifact_descriptor(record, artifact_id).media_type) == "image"
    )
    if image_request and "image/" in request.headers.get("accept", "").casefold():
        return await content_agent_artifact(
            project_id,
            operation_id,
            artifact_id,
            request,
            catalog=catalog,
            store=store,
        )
    return await _artifact_viewer_response(
        project_id,
        operation_id,
        artifact_id,
        catalog=catalog,
        store=store,
        head=request.method == "HEAD",
    )


@router.get("/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/download")
@router.head("/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/download")
async def download_agent_artifact(
    project_id: str,
    operation_id: str,
    artifact_id: str,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
) -> Response:
    require_registered_project(catalog, project_id)
    descriptor, data = await asyncio.to_thread(
        _load_agent_artifact,
        store,
        project_id,
        operation_id,
        artifact_id,
        "download",
    )
    suffix = Path(descriptor.name).suffix.casefold()
    fallback = f"artifact{suffix}" if re.fullmatch(r"\.[a-z0-9]{1,16}", suffix) else "artifact"
    disposition = (
        f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(descriptor.name, safe='')}"
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


@router.get("/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/viewer")
async def view_agent_artifact(
    project_id: str,
    operation_id: str,
    artifact_id: str,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
) -> Response:
    return await _artifact_viewer_response(
        project_id,
        operation_id,
        artifact_id,
        catalog=catalog,
        store=store,
    )


async def _artifact_viewer_response(
    project_id: str,
    operation_id: str,
    artifact_id: str,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    head: bool = False,
) -> Response:
    require_registered_project(catalog, project_id)
    descriptor, _ = await asyncio.to_thread(
        _load_agent_artifact,
        store,
        project_id,
        operation_id,
        artifact_id,
        "open",
    )
    content_url = (
        f"/api/projects/{quote(project_id, safe='')}/tasks/{quote(operation_id, safe='')}"
        f"/artifacts/{quote(artifact_id, safe='')}/content"
    )
    keep_url = (
        f"/api/projects/{quote(project_id, safe='')}/tasks/{quote(operation_id, safe='')}"
        f"/artifacts/{quote(artifact_id, safe='')}/keep"
        if descriptor.can_keep
        else None
    )
    panel = (
        comment_panel(
            {"projectId": project_id, "artifactId": artifact_id, "mediaType": descriptor.media_type}
        )
        if supports_comments(descriptor.media_type)
        else None
    )
    stored = store.artifact(artifact_id)
    live_url = None
    if stored is not None:
        content_url += "?" + urlencode({"version_id": stored.current_version})
        live_url = (
            f"/api/projects/{quote(project_id, safe='')}/artifacts/{quote(artifact_id, safe='')}"
            f"/versions/{quote(stored.current_version, safe='')}/live"
        )
    document, csp = artifact_viewer_document(
        descriptor,
        content_url=content_url,
        keep_url=keep_url,
        state="kept" if descriptor.is_kept() else "temporary",
        panel=panel,
        live_url=live_url,
    )
    return Response(
        b"" if head else document,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": csp,
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post(
    "/api/projects/{project_id}/tasks/{operation_id}/artifacts/{artifact_id}/keep",
    dependencies=[Depends(require_project_write_admission)],
)
def keep_agent_artifact(
    project_id: str,
    operation_id: str,
    artifact_id: str,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    artifact_mutation_locks: ArtifactMutationLocksDependency,
) -> dict[str, object]:
    require_registered_project(catalog, project_id)
    with artifact_mutation_locks(_artifact_mutation_key(project_id, operation_id, artifact_id)):
        descriptor, data = _load_agent_artifact(
            store,
            project_id,
            operation_id,
            artifact_id,
            "keep",
        )
        try:
            stored = store.keep_artifact(artifact_id)
            kept = store.mark_agent_artifact_kept(
                operation_id,
                artifact_id,
                kept_at=stored.kept_at,
            )
        except (FileNotFoundError, OSError, StateUnavailable, ValueError) as exc:
            raise HTTPException(status_code=503, detail="Artifact Keep unavailable") from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Artifact not found") from exc
    updated = store.agent_task(operation_id)
    assert updated is not None
    return _agent_artifact_response_json(_agent_artifact_response(store, updated, kept))


@router.post("/api/projects/{project_id}/tasks/{operation_id}/pause", status_code=202)
def pause_agent_task(
    project_id: str,
    operation_id: str,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    background_tasks: BackgroundTasksDependency,
) -> dict[str, object]:
    get_project_service(catalog, project_id)
    record = store.agent_task(operation_id)
    if record is None or record.project_id != project_id or not record.visible:
        raise HTTPException(status_code=404, detail="Agent task not found")
    _reject_history_only_control(record)
    try:
        return background_tasks.pause(operation_id).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/api/projects/{project_id}/tasks/{operation_id}/resume",
    status_code=202,
    dependencies=[Depends(require_project_write_admission)],
)
def resume_agent_task(
    project_id: str,
    operation_id: str,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
    experiment_admission: ExperimentAdmissionDependency,
) -> dict[str, object]:
    previous = store.agent_task(operation_id)
    if previous is None or previous.project_id != project_id or not previous.visible:
        raise HTTPException(status_code=404, detail="Agent task not found")
    _reject_history_only_control(previous)
    if previous.kind == "branch_merge":
        raise HTTPException(
            status_code=409,
            detail="Dispatch a new Merge to main task from the episode detail.",
        )
    authorized_by = identity_access.require_patch_capable_identity(request)
    service = get_graph_service(catalog, project_id, previous.graph_target.branch_id)
    try:
        experiment_admission.require_current(service, previous.request)
        skills = _validate_stored_task_request(service, previous.kind, previous.request)
        with _chat_recovery_admission(service, store, previous):
            return background_tasks.resume(
                operation_id,
                skills=skills,
                authorized_by=authorized_by,
            ).model_dump(mode="json")
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/api/projects/{project_id}/tasks/{operation_id}/repair-graph-update",
    status_code=202,
    dependencies=[Depends(require_project_write_admission)],
)
def repair_agent_task_graph_update(
    project_id: str,
    operation_id: str,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
    experiment_admission: ExperimentAdmissionDependency,
) -> dict[str, object]:
    previous = store.agent_task(operation_id)
    if previous is None or previous.project_id != project_id or not previous.visible:
        raise HTTPException(status_code=404, detail="Agent task not found")
    _reject_history_only_control(previous)
    if previous.kind == "branch_merge":
        raise HTTPException(
            status_code=409,
            detail="Dispatch a new Merge to main task from the episode detail.",
        )
    authorized_by = (
        identity_access.require_patch_capable_identity(request)
        if task_graph_capable(previous.kind, previous.request)
        else None
    )
    service = get_graph_service(catalog, project_id, previous.graph_target.branch_id)
    try:
        experiment_admission.require_current(service, previous.request)
        with _chat_recovery_admission(service, store, previous):
            return background_tasks.repair_graph_update(
                operation_id,
                authorized_by=authorized_by,
            ).model_dump(mode="json")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Agent task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/api/projects/{project_id}/tasks/{operation_id}/apply-graph-update-again",
    dependencies=[Depends(require_project_write_admission)],
)
def apply_agent_task_graph_update_again(
    project_id: str,
    operation_id: str,
    request: Request,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
    watcher_delivery: WatcherDeliveryDependency,
) -> dict[str, object]:
    """Re-apply a Work turn's retained Patch after canonical state was unreachable."""

    previous = store.agent_task(operation_id)
    if previous is None or previous.project_id != project_id or not previous.visible:
        raise HTTPException(status_code=404, detail="Agent task not found")
    _reject_history_only_control(previous)
    if not task_graph_capable(previous.kind, previous.request):
        raise HTTPException(status_code=409, detail="This task cannot change the graph.")
    authorized_by = identity_access.require_patch_capable_identity(request)
    service = get_graph_service(catalog, project_id, previous.graph_target.branch_id)
    target = service.history.graph_target
    require_graph_edit_admission(store, catalog.resolve_project_id(project_id), target)
    try:
        record = apply_work_graph_update_again(
            service,
            store,
            operation_id,
            authorized_by=authorized_by,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Agent task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    graph_update = record.result.get("graph_update") if record.result else None
    if isinstance(graph_update, dict) and graph_update.get("status") == "applied":
        watcher_delivery.evaluate_graph_wake_boundary(
            catalog.resolve_project_id(project_id),
            None,
            source="Apply again",
            graph_target=target,
        )
    return _agent_task_response(store, record, background_tasks)


@router.post(
    "/api/projects/{project_id}/tasks/{operation_id}/retry",
    status_code=202,
    dependencies=[Depends(require_project_write_admission)],
)
def retry_agent_task(
    project_id: str,
    operation_id: str,
    request: Request,
    body: RetryAgentTaskRequest | None = None,
    *,
    catalog: CatalogDependency,
    store: StoreDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
    experiment_admission: ExperimentAdmissionDependency,
) -> dict[str, object]:
    previous = store.agent_task(operation_id)
    if previous is None or previous.project_id != project_id or not previous.visible:
        raise HTTPException(status_code=404, detail="Agent task not found")
    _reject_history_only_control(previous)
    if previous.kind == "branch_merge":
        raise HTTPException(
            status_code=409,
            detail="Dispatch a new Merge to main task from the episode detail.",
        )
    authorized_by = identity_access.require_patch_capable_identity(request)
    service = get_graph_service(catalog, project_id, previous.graph_target.branch_id)
    try:
        overrides = body.model_dump(exclude_none=True) if body is not None else {}
        if previous.kind == "auto_research":
            candidate = load_stored_request(
                AutoResearchRunRequest,
                {**previous.request, **overrides},
                operation_id=previous.operation_id,
            )
        else:
            request_type = CoachRequest if previous.kind == "paper_coach" else RunRequest
            candidate = load_stored_request(
                request_type,
                {**previous.request, **overrides, "session_id": None},
                operation_id=previous.operation_id,
            )
        if isinstance(candidate, RunRequest) and candidate.artifact_edit is None:
            candidate = _admit_artifact_context_request(
                store,
                service,
                project_id,
                previous.kind,
                candidate,
            )
            if candidate.artifact_context is not None:
                overrides.update(
                    {
                        "provider": candidate.provider,
                        "model": candidate.model,
                        "reasoning": candidate.reasoning,
                        "run_on": candidate.run_on,
                    }
                )
        candidate_payload = candidate.model_dump(mode="json")
        experiment_admission.require_current(service, candidate_payload)
        skills = _validate_stored_task_request(
            service,
            previous.kind,
            candidate_payload,
        )
        if previous.kind == "auto_research":
            return background_tasks.retry_auto_research(
                operation_id,
                service=service,
                skills=skills,
                **overrides,
            ).model_dump(mode="json")
        with _chat_recovery_admission(service, store, previous, candidate):
            return background_tasks.retry(
                operation_id,
                skills=skills,
                authorized_by=authorized_by,
                **overrides,
            ).model_dump(mode="json")
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _chat_recovery_admission(
    service: ProjectService,
    store: AppStore,
    previous: AgentTaskRecord,
    candidate: RunRequest | CoachRequest | None = None,
):
    if previous.kind not in {"node_chat", "project_chat"}:
        return nullcontext()
    request = candidate or load_stored_request(
        RunRequest, previous.request, operation_id=previous.operation_id
    )
    assert isinstance(request, RunRequest)
    if request.artifact_edit is not None:
        return nullcontext()
    return conversation_worktree_recovery_admission(service, store, previous.project_id, request)


def _artifact_mutation_key(project_id: str, operation_id: str, artifact_id: str) -> str:
    return f"artifact:{project_id}:{operation_id}:{artifact_id}"


def _load_agent_artifact(
    store: AppStore,
    project_id: str,
    operation_id: str,
    artifact_id: str,
    action: Literal["open", "download", "keep"],
) -> tuple[AgentArtifactResponse, bytes]:
    """Resolve an attachment through its persisted task and RCP-owned bytes."""
    try:
        projected, data = _read_agent_artifact_bytes(
            store,
            project_id,
            operation_id,
            artifact_id,
            action,
        )
        if (
            projected.media_type != "application/octet-stream"
            and classify_artifact_bytes(projected.name, data) != projected.media_type
        ):
            raise ValueError("artifact media type changed")
    except (FileNotFoundError, KeyError) as exc:
        raise HTTPException(status_code=410, detail="Preview unavailable") from exc
    except StateUnavailable as exc:
        raise HTTPException(status_code=503, detail="Preview unavailable") from exc
    except ValueError as exc:
        raise HTTPException(status_code=410, detail="Preview unavailable") from exc
    return projected, data


def _read_agent_artifact_bytes(
    store: AppStore,
    project_id: str,
    operation_id: str,
    artifact_id: str,
    action: Literal["open", "download", "keep"],
) -> tuple[AgentArtifactResponse, bytes]:
    """Read bounded source bytes without applying viewer-specific media validation."""
    record = store.agent_task(operation_id)
    # An in-place edit lists the artifact it published, which its origin turn's scope owns.
    # A revoking edit runs as its own `artifact_edit` task whose other outputs share the
    # server-recorded staging scope.
    edit = record.request.get("artifact_edit") if record is not None else None
    edited = isinstance(edit, dict) and edit.get("artifact_id") == artifact_id
    if (
        record is None
        or record.project_id != project_id
        or not (
            record.kind in {"node_chat", "project_chat"}
            or (record.kind == "artifact_edit" and isinstance(edit, dict))
        )
    ):
        raise HTTPException(status_code=404, detail="Agent task not found")
    descriptor = _agent_artifact_descriptor(record, artifact_id)
    projected = _agent_artifact_response(store, record, descriptor)
    allowed = {
        "open": projected.can_open,
        "download": projected.can_download,
        "keep": projected.can_keep,
    }[action]
    if action == "open" and projected.view in {"pdf", "file"}:
        raise HTTPException(status_code=404, detail="Artifact has no viewer")
    if not allowed:
        raise HTTPException(
            status_code=410 if action in {"open", "download"} else 409,
            detail=projected.unavailable_reason or f"Artifact {action} unavailable",
        )
    # Mirrors the turn's staging: an edit binding fixes the scope, even across a Retry.
    scope_id = (
        edit["staged_scope_id"]
        if isinstance(edit, dict)
        else _logical_chat_turn_operation_id(store, record.operation_id)
    )
    if not edited and scoped_artifact_id(scope_id, descriptor.name) != descriptor.artifact_id:
        raise ValueError("artifact descriptor does not match its task scope")
    stored = store.artifact(artifact_id)
    if stored is None or stored.project_id != project_id:
        raise FileNotFoundError(descriptor.name)
    data = store.read_artifact_bytes(artifact_id)
    return projected, data


def _agent_artifact_descriptor(
    record: AgentTaskRecord,
    artifact_id: str,
) -> AgentArtifactDescriptor:
    artifacts = record.result.get("artifacts") if record.result else None
    if isinstance(artifacts, list):
        for raw in artifacts:
            try:
                descriptor = AgentArtifactDescriptor.model_validate(raw)
            except (TypeError, ValueError):
                continue
            if descriptor.artifact_id == artifact_id:
                return descriptor
    raise HTTPException(status_code=404, detail="Artifact not found")


def _admit_artifact_context_request(
    store: AppStore,
    service: ProjectService,
    project_id: str,
    kind: AgentTaskKind,
    request: RunRequest,
) -> RunRequest:
    if request.artifact_context is None:
        return request
    if kind not in {"node_chat", "project_chat"}:
        raise ValueError("Artifact comments require a conversation or artifact comment route.")
    from rcp.runs.artifact_edit_admission import admit_artifact_edit

    return admit_artifact_edit(store, service, project_id, request)


def _validated_task_request(
    service: ProjectService,
    kind: AgentTaskKind,
    body: dict[str, object],
) -> AgentTaskRequest:
    if "graph_target" in body or "branch_id" in body:
        raise ValueError("Select the graph target with the branch_id route parameter.")
    if body.get("references") and (
        kind not in {"node_chat", "project_chat"}
        or body.get("artifact_context") is not None
        or body.get("artifact_edit") is not None
    ):
        raise ValueError("Project references require an ordinary chat.")
    if kind == "paper_coach":
        return _resolved_coach_request(service, CoachRequest.model_validate(body))

    client_request = dict(body)
    # Resolved compute metadata is a server-owned admission snapshot. A client
    # may echo or forge this field, but it never participates in resolution.
    client_request.pop("artifact_edit", None)
    client_request.pop("resolved_compute_context", None)
    client_request.pop("worktree_integration_target", None)
    client_request.pop("attachments", None)
    request = RunRequest.model_validate(client_request).model_copy(
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
        if request.worktree or request.worktree_integration:
            raise ValueError("Only ordinary conversations can use worktrees.")
        if request.active_compute_ids:
            raise ValueError("Compute connections can be attached only to a chat turn.")
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
            "session_id": None,
        }
    )
    # Artifact comments are a turn's text by themselves; admission writes them in.
    commented = request.artifact_context is not None and request.artifact_context.selections
    if not request.chat_id or not (commented or (request.message and request.message.strip())):
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
    # Artifact-context admission resolves the chat's current execution profile.
    # Do not first resolve stale settings from the currently open client.
    if request.artifact_context is not None:
        return request
    return _resolved_graph_request(service, kind, request)


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
    if request.artifact_edit is not None:
        return None
    if kind in {"seed", "refresh"}:
        service.history.require_writable()
    resolved_run = _resolved_graph_request(
        service,
        kind,
        request,
        admit_compute_context=False,
    )
    return service.resolve_skill_selection(resolved_run)


__all__ = [
    "RetryAgentTaskRequest",
    "agent_task",
    "agent_tasks",
    "content_agent_artifact",
    "download_agent_artifact",
    "pause_agent_task",
    "preview_agent_artifact",
    "repair_agent_task_graph_update",
    "resume_agent_task",
    "retry_agent_task",
    "router",
    "start_agent_task",
    "view_agent_artifact",
]
