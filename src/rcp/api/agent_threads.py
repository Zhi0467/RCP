"""Member-owned thread index and controls, sharing the existing chat owners."""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Literal, Self, cast
from urllib.parse import quote

from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from pydantic import UUID4, BaseModel, ConfigDict, Field, model_validator

from rcp.api.chats import (
    AttachmentStoreDependency,
    CatalogDependency,
    ChatBrowserPreference,
    ChatReadBody,
    ChatTitleBody,
    IdentityDependency,
    StoreDependency,
    _artifact_comments,
    _delete_browser_if_still_off,
    add_attachment,
    mark_chat_read,
    remove_attachment,
    rename_chat,
    set_chat_browser,
    stamp_shown_at,
    transcript_tasks,
)
from rcp.api.dependencies import project_write_admission
from rcp.api.index import (
    DisplayCacheDependency,
    ExperimentOperationLockDependency,
    SpaceRunMode,
    episode_title,
    space_run_entries,
)
from rcp.api.questions import _answer_continues, _newest_episode, _serialize, _serialize_all
from rcp.api.tasks import (
    BackgroundTasksDependency,
    RetryAgentTaskRequest,
    SteerAgentTaskRequest,
    _agent_artifact_descriptor,
    _agent_task_response,
    _artifact_viewer_response,
    _reject_history_only_control,
    content_agent_artifact,
    download_agent_artifact,
    released_unless_started,
    require_attachment_pair,
)
from rcp.artifact_comments import COMMENTABLE_MEDIA_TYPES, supports_comments
from rcp.artifact_views import ViewerPresentation
from rcp.attachments import ChatAttachmentStore, ChatAttachmentUpload, ProjectReferenceSelector
from rcp.config import load_manifest
from rcp.core.models import AuthorizedHuman
from rcp.history import ReplayHalted
from rcp.limits import (
    AGENT_THREAD_QUEUE_MAX_CHARS,
    CHAT_ATTACHMENT_MAX_COUNT,
    SPACE_RUNNING_MAX_ROWS,
)
from rcp.projects import ProjectCatalog
from rcp.providers import ProviderId
from rcp.repository_preview import repository_for_path
from rcp.runs.agent_names import DEFAULT_AGENT_NAMES
from rcp.runs.agent_thread_commands import sent_thread_agent, store_thread_pick
from rcp.runs.agent_thread_queue import queued_turn_operation_id, release_queued_files
from rcp.runs.agent_threads import (
    episode_watching,
    task_owner,
    thread_projection,
    thread_projections,
    thread_scope_id,
)
from rcp.runs.approval_cards import ProposalCardView, draws_proposal_cards, proposal_cards
from rcp.runs.artifact_edit_admission import artifact_comment_message
from rcp.runs.space_chat import SpaceThreadOwner, admit_space_invocations, space_transcript_lock
from rcp.runs.steering import begin_chat_steer, finish_chat_steer
from rcp.runs.turn_attachments import claim_turn_attachments
from rcp.service import (
    ArtifactContextRequest,
    ChatTranscript,
    RunRequest,
    read_space_chat_transcript,
)
from rcp.service_connections import ConnectionError
from rcp.storage import AgentTaskRecord, AppStore
from rcp.storage.agent_threads import (
    AgentThreadQueueFull,
    AgentThreadQueueRecord,
    AgentThreadRecord,
    private_artifact_owner,
    thread_binds_task,
)
from rcp.storage.chat_names import ChatNameError, chat_titles
from rcp.storage.notifications import question_recipients
from rcp.transport.state import StateUnavailable

router = APIRouter()
logger = logging.getLogger(__name__)


def owned_thread(store: AppStore, thread_id: str, user_id: str) -> AgentThreadRecord:
    thread = store.agent_thread(thread_id)
    if thread is None or thread.user_id != user_id:
        raise HTTPException(status_code=404, detail="Thread not found")
    if thread.project_id and not store.is_project_member(thread.project_id, user_id):
        raise HTTPException(status_code=404, detail="Thread not found")
    return thread


def owned_space_thread(store: AppStore, thread_id: str, user_id: str) -> AgentThreadRecord:
    thread = owned_thread(store, thread_id, user_id)
    if thread.kind != "space" or thread.closed_at is not None:
        raise HTTPException(status_code=404, detail="Space thread not found")
    return thread


@router.get("/api/agent-threads")
def agent_threads(request: Request, *, store: StoreDependency, identity_access: IdentityDependency):
    user = identity_access.acting_user(request)
    project_ids = store.member_project_ids(user.user_id)
    threads = store.ensure_agent_threads(
        user.user_id, sorted(project_ids), default_names=DEFAULT_AGENT_NAMES
    )
    for thread in threads:
        if thread.project_id and thread.project_id not in project_ids and not thread.closed_at:
            store.close_agent_thread(thread.thread_id, reason="access_lost")
    rows = thread_projections(
        store,
        [
            thread
            for thread in threads
            if thread.project_id is None or thread.project_id in project_ids
        ],
    )
    rows.sort(key=lambda row: str(row["last_activity_at"] or ""), reverse=True)
    rows.sort(key=lambda row: row["kind"] != "space")
    return {"threads": rows}


def _card_threads(store: AppStore, user_id: str, thread_id: str | None) -> list[AgentThreadRecord]:
    """The member's open, shown project threads, or the one named, that carry cards."""
    if thread_id is not None:
        threads = [owned_thread(store, thread_id, user_id)]
    else:
        projects = store.member_project_ids(user_id)
        threads = [
            thread for thread in store.agent_threads(user_id) if thread.project_id in projects
        ]
    return [thread for thread in threads if draws_proposal_cards(store, thread)]


@router.get("/api/agent-threads/cards")
def agent_thread_cards(
    request: Request,
    thread_id: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
):
    """Open approval cards of the member's project threads: Proposal cards drawn from each
    project's local display snapshot, and asked cards from the question list. A member-wide
    read, Pip's dock, also lists the cards the Space thread asked. Question relays
    span the member's projects for Space and only the named Project Agent's project.

    A member-wide read skips a project whose snapshot cannot be read; a named thread's
    unreadable project is a 503.
    """
    user_id = identity_access.acting_user(request).user_id
    by_project: dict[str, list[AgentThreadRecord]] = defaultdict(list)
    for thread in _card_threads(store, user_id, thread_id):
        assert thread.project_id is not None
        by_project[thread.project_id].append(thread)
    drawn: list[ProposalCardView] = []
    asked = []
    for project_id, threads in by_project.items():
        project = store.project(project_id)
        if project is None:
            continue
        try:
            state = catalog.open_display(project_id).history.state()
        except (StateUnavailable, ReplayHalted, OSError, KeyError) as exc:
            if thread_id is not None:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            logger.warning("Cards skipped project %s: %s", project_id, exc)
        else:
            drawn.extend(proposal_cards(store, project, state, threads))
        asked.extend(_asked_cards(store, project_id, threads, user_id))
    drawn.sort(key=lambda card: card.turn_created_at, reverse=True)
    if thread_id is not None:
        asked.sort(key=lambda row: row["created_at"], reverse=True)
        return {
            "proposal_cards": drawn,
            "asked_cards": asked,
            "questions": _dock_questions(
                store, catalog, user_id, by_project, excluded_chat_id=thread_id
            ),
        }
    space = [
        thread
        for thread in store.agent_threads(user_id)
        if thread.kind == "space" and thread.closed_at is None
    ]
    asked.extend(_asked_cards(store, store.space_id, space, user_id))
    asked.sort(key=lambda row: row["created_at"], reverse=True)
    return {
        "proposal_cards": drawn,
        "asked_cards": asked,
        "questions": _dock_questions(store, catalog, user_id, store.member_project_ids(user_id)),
    }


def _dock_questions(
    store: AppStore,
    catalog: ProjectCatalog,
    user_id: str,
    project_ids: Iterable[str],
    *,
    excluded_chat_id: str | None = None,
) -> list[dict[str, Any]]:
    """Answerable card-less questions within the admitted project scope, excluding questions
    already shown inline in the named thread."""
    rows = []
    for project_id in sorted(project_ids):
        project = store.project(project_id)
        if project is None:
            continue
        pending = store.list_questions(project_id=project_id, open_only=True)
        cards = store.approval_cards([question.question_id for question in pending])
        with store.connection() as connection:
            recipient_of = [
                question
                for question in pending
                if question.question_id not in cards
                and not (
                    question.origin.owner_kind == "chat"
                    and question.origin.owner_id == excluded_chat_id
                )
                and user_id in question_recipients(connection, question)
            ]
        asked = [
            question
            for question in recipient_of
            if question.origin.owner_kind != "chat"
            or store.chat_turn_refusal(project_id, question.origin.owner_id) is None
        ]
        with store.connection() as connection:
            chats = chat_titles(
                connection,
                project_id,
                [item.origin.owner_id for item in asked if item.origin.owner_kind == "chat"],
            )
        for question in asked:
            episode = _newest_episode(store, question)
            row = _serialize(store, question, continues=_answer_continues(store, question, episode))
            rows.append(
                {
                    **row.model_dump(mode="json"),
                    "project_id": project_id,
                    "project_name": project.name,
                    "source_label": (
                        chats.get(question.origin.owner_id)
                        if question.origin.owner_kind == "chat"
                        else episode_title(catalog, episode)
                        if episode is not None
                        else None
                    ),
                }
            )
    rows.sort(key=lambda row: row["created_at"], reverse=True)
    return rows


class RunningRow(BaseModel):
    kind: SpaceRunMode | Literal["thread"]
    id: str
    project_id: str
    project_name: str
    title: str
    needs_you: bool
    label: str
    watching: int
    at: str | None
    link: str


class RunningList(BaseModel):
    rows: list[RunningRow]
    needs: int
    total: int


_RUNNING_THREAD_STATES = frozenset({"needs_you", "running", "queued", "paused", "failed"})
_THREAD_NEEDS_YOU_STATES = frozenset({"needs_you", "paused", "failed"})


@router.get("/api/agent-threads/running", response_model=RunningList)
def running_list(
    request: Request,
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    project_display_cache: DisplayCacheDependency,
    identity_access: IdentityDependency,
    experiment_operation_lock: ExperimentOperationLockDependency,
) -> RunningList:
    """What runs in the member's projects: live Experiment and Auto-research episodes, whoever
    started them, and the member's own project threads that work, wait, failed, or watch.
    Rows that need the member come first, each group newest first."""
    user_id = identity_access.acting_user(request).user_id
    episodes = [
        entry
        for entry in space_run_entries(
            user_id,
            catalog=catalog,
            project_display_cache=project_display_cache,
            store=store,
            experiment_operation_lock=experiment_operation_lock,
        )
        if entry.run_section != "completed" and not entry.archived
    ]
    watching = episode_watching(store, [entry.episode_id for entry in episodes])
    rows = [
        RunningRow(
            kind=entry.mode,
            id=entry.episode_id,
            project_id=entry.project_id,
            project_name=entry.project_name,
            title=entry.title,
            needs_you=entry.run_section == "actionable",
            label=entry.health_label,
            watching=watching.get(entry.episode_id, 0),
            at=entry.last_activity_at,
            link=f"#/projects/{quote(entry.project_id, safe='')}"
            f"/targets/{quote(entry.graph_target.key, safe='')}"
            f"/episode/{quote(entry.episode_id, safe='')}",
        )
        for entry in episodes
    ]
    for thread in thread_projections(store, _card_threads(store, user_id, None)):
        state = thread["live_state"]
        if state not in _RUNNING_THREAD_STATES and not thread["watching"]:
            continue
        rows.append(
            RunningRow(
                kind="thread",
                id=str(thread["thread_id"]),
                project_id=str(thread["project_id"]),
                project_name=str(thread["project_name"]),
                title=str(thread["agent_name"]),
                needs_you=state in _THREAD_NEEDS_YOU_STATES,
                label=str(thread["live_label"]),
                watching=cast(int, thread["watching"]),
                at=cast(str | None, thread["last_activity_at"]),
                link=f"#/agent/{quote(str(thread['thread_id']), safe='')}",
            )
        )
    rows.sort(key=lambda row: row.at or "", reverse=True)
    rows.sort(key=lambda row: not row.needs_you)
    return RunningList(
        rows=rows[:SPACE_RUNNING_MAX_ROWS],
        needs=sum(row.needs_you for row in rows),
        total=len(rows),
    )


def _asked_cards(
    store: AppStore, scope_id: str, threads: list[AgentThreadRecord], user_id: str
) -> list[dict[str, Any]]:
    """The answerable pending card questions of these threads under `scope_id`, each naming
    the project its action acts on, and only while the member still belongs to it."""
    asked = []
    for thread in threads:
        pending = [
            question
            for question in store.list_questions(
                project_id=scope_id, owner_kind="chat", owner_id=thread.thread_id
            )
            if question.state == "pending"
        ]
        for row in _serialize_all(store, pending):
            if row.card is None or not row.can_answer:
                continue
            project_id = row.card["action"]["project_id"]
            project = store.project(project_id)
            if project is not None and store.is_project_member(project_id, user_id):
                asked.append({**row.model_dump(mode="json"), "project_name": project.name})
    return asked


class SpaceReference(BaseModel):
    project_id: str


@router.get("/api/agent-threads/{thread_id}/references", response_model=SpaceReference)
def space_reference(
    thread_id: str,
    request: Request,
    path: Annotated[str | None, Query(min_length=1, max_length=4096)] = None,
    artifact_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
) -> SpaceReference:
    """The member project a Space reply's file path or saved artifact id belongs to.

    Only the member's own projects resolve, and another member's private artifact
    reads like a missing one. The caller then reads it through the project's routes.
    """

    user_id = identity_access.acting_user(request).user_id
    owned_space_thread(store, thread_id, user_id)
    if (path is None) == (artifact_id is None):
        raise HTTPException(status_code=422, detail="Name one path or one artifact id")
    projects = store.member_project_ids(user_id)
    if artifact_id is not None:
        artifact = store.artifact(artifact_id)
        readable = (
            store.readable_artifact(artifact.project_id, artifact_id)
            if artifact is not None and artifact.project_id in projects
            else None
        )
        if readable is not None:
            with store.connection() as connection:
                owner = private_artifact_owner(connection, readable)
            if owner in {None, user_id}:
                return SpaceReference(project_id=readable.project_id)
        raise HTTPException(
            status_code=404, detail="No saved artifact of your projects has this id"
        )
    assert path is not None
    matches = []
    for project_id in sorted(projects):
        record = store.project(project_id)
        if record is None:
            continue
        try:
            repository_for_path(load_manifest(record.locator), path)
        except (FileNotFoundError, OSError, ValueError):
            continue
        matches.append(project_id)
    if len(matches) != 1:
        raise HTTPException(
            status_code=404,
            detail="The path is in no repository of your projects"
            if not matches
            else "The path is in repositories of several of your projects",
        )
    return SpaceReference(project_id=matches[0])


@router.get("/api/agent-threads/{thread_id}/transcript", response_model=ChatTranscript)
def thread_transcript(
    thread_id: str,
    request: Request,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
):
    """The Space thread's transcript; a project thread's is its project chat's."""
    thread = owned_thread(store, thread_id, identity_access.acting_user(request).user_id)
    if thread.kind != "space":
        raise HTTPException(status_code=404, detail="Space thread not found")
    title = store.chat_name(store.space_id, thread_id, "")
    try:
        # Under the appenders' lock, so a read never sees a half-written record.
        with space_transcript_lock(store, thread_id):
            transcript = read_space_chat_transcript(
                store.path.parent, thread.user_id, thread_id, title=title
            )
    except ConnectionError as exc:
        raise HTTPException(exc.status, {"code": exc.code, "message": exc.message}) from None
    if transcript is not None:
        tasks = transcript_tasks(store, transcript.messages)
        for message in transcript.messages:
            task = tasks.get(message.operation_id or "")
            if message.role == "user" and task is not None and message.steering is None:
                message.artifact_comments = _artifact_comments(store, task.request)
        stamp_shown_at(store, transcript.messages, tasks)
        return transcript
    return ChatTranscript(
        chat_id=thread_id,
        kind="space_chat",
        node_id=None,
        title=title,
        updated_at=thread.created_at,
        message_count=0,
        last_message_preview="",
    )


@router.post("/api/agent-threads/{thread_id}/title")
def rename_thread(
    thread_id: str,
    body: ChatTitleBody,
    request: Request,
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
):
    thread = owned_thread(store, thread_id, identity_access.acting_user(request).user_id)
    if thread.project_id:
        with project_write_admission(thread.project_id, request):
            rename_chat(
                thread.project_id,
                thread_id,
                body,
                request,
                catalog=catalog,
                store=store,
                identity_access=identity_access,
            )
        return thread_projection(store, thread)
    try:
        store.set_chat_title(thread_scope_id(store, thread), thread_id, thread.user_id, body.title)
    except ChatNameError as exc:
        raise HTTPException(status_code=422, detail=exc.code) from exc
    return thread_projection(store, thread)


class ThreadAgentBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    provider: ProviderId
    model: str | None = Field(default=None, min_length=1, max_length=200)
    reasoning: str | None = Field(default=None, min_length=1, max_length=100)
    run_on: str | None = Field(default=None, min_length=1, max_length=200)


@router.post("/api/agent-threads/{thread_id}/agent")
def pick_thread_agent(
    thread_id: str,
    body: ThreadAgentBody,
    request: Request,
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
):
    """Store the thread's agent pick; every later turn on the thread runs it."""
    thread = owned_thread(store, thread_id, identity_access.acting_user(request).user_id)
    try:
        store_thread_pick(store, catalog, thread, **body.model_dump(), wait_for_sign_in=True)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return thread_projection(store, store.agent_thread(thread_id) or thread)


@router.post("/api/agent-threads/{thread_id}/read")
def read_thread(
    thread_id: str,
    body: ChatReadBody,
    request: Request,
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
):
    thread = owned_thread(store, thread_id, identity_access.acting_user(request).user_id)
    if thread.project_id:
        mark_chat_read(
            thread.project_id,
            thread_id,
            body,
            request,
            catalog=catalog,
            store=store,
            identity_access=identity_access,
        )
    else:
        store.mark_chat_read(
            thread_scope_id(store, thread), thread_id, thread.user_id, body.read_through
        )
    return thread_projection(store, thread)


@router.put("/api/agent-threads/{thread_id}/browser", response_model=ChatBrowserPreference)
def thread_browser(
    thread_id: str,
    body: ChatBrowserPreference,
    request: Request,
    background_tasks: BackgroundTasks,
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
):
    thread = owned_thread(store, thread_id, identity_access.acting_user(request).user_id)
    if thread.project_id:
        with project_write_admission(thread.project_id, request):
            return set_chat_browser(
                thread.project_id, thread_id, body, background_tasks, catalog=catalog, store=store
            )
    scope = thread_scope_id(store, thread)
    store.set_chat_browser_requested(scope, thread_id, browser_requested=body.browser_requested)
    if not body.browser_requested:
        background_tasks.add_task(_delete_browser_if_still_off, store, scope, thread_id)
    return body


@router.post("/api/agent-threads/{thread_id}/attachments", response_model=ChatAttachmentUpload)
def upload_space_attachment(
    thread_id: str,
    request: Request,
    file: Annotated[UploadFile, File()],
    client_id: Annotated[str, Form()],
    attachment_set_id: Annotated[str | None, Form()] = None,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
    attachment_store: AttachmentStoreDependency,
) -> ChatAttachmentUpload:
    owned_space_thread(store, thread_id, identity_access.acting_user(request).user_id)
    return add_attachment(
        attachment_store, store.space_id, thread_id, file, client_id, attachment_set_id
    )


@router.delete("/api/agent-threads/{thread_id}/attachments/{attachment_id}")
def remove_space_attachment(
    thread_id: str,
    attachment_id: str,
    client_id: str,
    attachment_set_id: str,
    request: Request,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
    attachment_store: AttachmentStoreDependency,
) -> dict[str, bool]:
    owned_space_thread(store, thread_id, identity_access.acting_user(request).user_id)
    return remove_attachment(
        attachment_store, store.space_id, thread_id, attachment_id, client_id, attachment_set_id
    )


def owned_project_thread(store: AppStore, thread_id: str, user_id: str) -> AgentThreadRecord:
    thread = owned_thread(store, thread_id, user_id)
    if thread.kind != "project" or thread.project_id is None:
        raise HTTPException(status_code=404, detail="Project thread not found")
    return thread


class QueuedMessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    message: str = Field(default="", max_length=AGENT_THREAD_QUEUE_MAX_CHARS)
    attachment_set_id: str | None = Field(default=None, min_length=1)
    attachment_client_id: str | None = Field(default=None, min_length=1)
    references: list[ProjectReferenceSelector] = Field(
        default_factory=list, max_length=CHAT_ATTACHMENT_MAX_COUNT
    )

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        require_attachment_pair(self.attachment_set_id, self.attachment_client_id)
        if not (self.message.strip() or self.attachment_set_id or self.references):
            raise ValueError("A queued message needs text or files")
        return self

    def request_sha256(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


@router.post("/api/agent-threads/{thread_id}/queue", status_code=202)
def queue_thread_message(
    thread_id: str,
    body: QueuedMessageBody,
    request: Request,
    idempotency_key: Annotated[UUID4, Header()],
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
    attachment_store: AttachmentStoreDependency,
):
    """Keep the member's message until the thread is free, then run one drain pass for it."""
    authorized = identity_access.require_patch_capable_identity(request)
    thread = owned_project_thread(store, thread_id, authorized.user_id)
    assert thread.project_id is not None
    key = str(idempotency_key)
    with project_write_admission(thread.project_id, request) as project_id:
        if refusal := store.chat_turn_refusal(project_id, thread_id):
            raise HTTPException(status_code=409, detail=refusal)
        queued = store.agent_thread_queue_for_key(thread_id, key) or _queue_new_message(
            store, catalog, attachment_store, thread, project_id, body, key
        )
    if queued.request_sha256 != body.request_sha256():
        raise HTTPException(status_code=422, detail="The key was used for another message")
    request.app.state.reconcile_thread_handoffs()
    current = store.agent_thread_queue_item(queued.queue_id)
    return {
        "queue_id": queued.queue_id,
        "operation_id": current.operation_id if current is not None else None,
    }


def _queue_new_message(
    store: AppStore,
    catalog: ProjectCatalog,
    attachment_store: ChatAttachmentStore,
    thread: AgentThreadRecord,
    project_id: str,
    body: QueuedMessageBody,
    key: str,
) -> AgentThreadQueueRecord:
    """Claim the files for the queued turn's own id, then store the row, or release them."""
    queue_id = str(uuid.uuid4())
    operation_id = queued_turn_operation_id(queue_id)
    client_id = body.attachment_client_id or (str(uuid.uuid4()) if body.references else None)
    try:
        claimed, batch = claim_turn_attachments(
            store,
            catalog,
            attachment_store,
            project_id,
            RunRequest(
                chat_id=thread.thread_id,
                chat_scope="project",
                attachment_set_id=body.attachment_set_id,
                attachment_client_id=client_id,
                references=body.references,
            ),
            operation_id=operation_id,
            viewer_id=thread.user_id,
        )
    except ValueError as exc:
        if (raced := store.agent_thread_queue_for_key(thread.thread_id, key)) is not None:
            return raced
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        queued = store.queue_agent_thread_message(
            queue_id=queue_id,
            thread_id=thread.thread_id,
            user_id=thread.user_id,
            idempotency_key=key,
            message=body.message,
            attachment_set_id=batch,
            attachment_client_id=client_id,
            attachment_count=len(claimed.attachments),
            request_sha256=body.request_sha256(),
        )
    except (AgentThreadQueueFull, ValueError) as exc:
        if batch is not None:
            attachment_store.release(batch, operation_id)
        status = 409 if isinstance(exc, AgentThreadQueueFull) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    if queued.queue_id != queue_id and batch is not None:
        attachment_store.release(batch, operation_id)
    return queued


@router.delete("/api/agent-threads/{thread_id}/queue/{queue_id}")
def remove_queued_message(
    thread_id: str,
    queue_id: str,
    request: Request,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
    attachment_store: AttachmentStoreDependency,
):
    """Remove the member's unstarted or refused message; a started one stays."""
    thread = owned_project_thread(store, thread_id, identity_access.acting_user(request).user_id)
    row = store.agent_thread_queue_item(queue_id)
    if row is None or row.thread_id != thread.thread_id or row.removed_at is not None:
        raise HTTPException(status_code=404, detail="Queued message not found")
    removed = store.remove_agent_thread_queue_item(queue_id)
    if removed is None:
        raise HTTPException(status_code=409, detail="This message already started")
    if removed.refusal_reason is None:
        release_queued_files(attachment_store, removed)
    return {"queue_id": queue_id}


@router.get("/api/agent-threads/{thread_id}/queue")
def thread_queue(
    thread_id: str,
    request: Request,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
):
    """What waits for the thread: ready handoffs and the member's messages, by readiness."""
    thread = owned_project_thread(store, thread_id, identity_access.acting_user(request).user_id)
    items: list[dict[str, object]] = []
    for handoff in store.pending_agent_handoffs(thread_id):
        source = store.agent_task(handoff.source_operation_id)
        if source is None or source.status != "succeeded":
            continue
        items.append(
            {
                "kind": "handoff",
                "handoff_id": handoff.handoff_id,
                "message": handoff.message,
                "source_agent_name": store.chat_name(store.space_id, handoff.source_thread_id, ""),
                "ready_at": source.finished_at or handoff.created_at,
            }
        )
    items.extend(
        {
            "kind": "message",
            "queue_id": row.queue_id,
            "message": row.message,
            "created_at": row.created_at,
            "refusal_reason": row.refusal_reason,
            "attachment_count": row.attachment_count,
        }
        for row in store.agent_thread_queue(thread.thread_id)
    )
    items.sort(key=lambda item: str(item.get("ready_at") or item.get("created_at")))
    return {"items": items}


class SpaceTurnBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    message: str = ""
    provider: ProviderId | None = None
    model: str | None = None
    reasoning: str | None = None
    timezone: str | None = None
    attachment_set_id: str | None = Field(default=None, min_length=1)
    attachment_client_id: str | None = Field(default=None, min_length=1)
    artifact_context: ArtifactContextRequest | None = None
    invoked_workflow_ids: list[str] = Field(default_factory=list)
    invoked_skill_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_attachment_pair(self) -> Self:
        require_attachment_pair(self.attachment_set_id, self.attachment_client_id)
        return self

    @model_validator(mode="after")
    def validate_message(self) -> Self:
        if not self.message and self.artifact_context is None:
            raise ValueError("A Space turn needs a message or an artifact comment")
        return self


def _space_artifact_comment(
    store: AppStore, thread: AgentThreadRecord, body: SpaceTurnBody
) -> tuple[ArtifactContextRequest | None, str]:
    """Admit comments on an artifact one of this thread's own turns produced."""
    context = body.artifact_context
    if context is None:
        return None, body.message
    try:
        if context.source != "task":
            raise ValueError("A Space comment names a turn's artifact.")
        task = _space_task(store, thread, context.operation_id)
        descriptor = _agent_artifact_descriptor(task, context.artifact_id)
        if not supports_comments(descriptor.media_type):
            raise ValueError("The artifact type does not support comments.")
        if context.selections and descriptor.media_type not in COMMENTABLE_MEDIA_TYPES:
            raise ValueError("This artifact supports a comment without selections.")
        message = artifact_comment_message(body.message, context)
    except (HTTPException, ValueError) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        raise HTTPException(status_code=422, detail=detail) from exc
    return context, message


def _space_task(store: AppStore, thread: AgentThreadRecord, operation_id: str) -> AgentTaskRecord:
    task = store.agent_task(operation_id)
    if (
        thread.kind != "space"
        or task is None
        or not task.visible
        or not thread_binds_task(thread, task, store.space_id)
    ):
        raise HTTPException(status_code=404, detail="Agent task not found")
    return task


@router.post("/api/agent-threads/{thread_id}/turns", status_code=202)
def space_turn(
    thread_id: str,
    body: SpaceTurnBody,
    request: Request,
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
    attachment_store: AttachmentStoreDependency,
    idempotency_key: Annotated[UUID4 | None, Header()] = None,
):
    authorized = identity_access.require_patch_capable_identity(request)
    thread = owned_space_thread(store, thread_id, authorized.user_id)
    artifact_context, message = _space_artifact_comment(store, thread, body)
    try:
        with store.client_request_admission(
            store.space_id,
            authorized.user_id,
            str(idempotency_key) if idempotency_key is not None else None,
            f"agent-threads/{thread_id}/turns",
        ) as existing:
            if existing is not None:
                return {"operation_id": existing.operation_id}
            workflows, skills = admit_space_invocations(
                body.invoked_workflow_ids, body.invoked_skill_ids
            )
            turn = RunRequest.model_validate(
                {
                    **sent_thread_agent(store, thread, body.model_dump()),
                    "invoked_workflow_ids": workflows,
                    "invoked_skill_ids": skills,
                    "chat_id": thread_id,
                    "chat_scope": "project",
                    "mode": "work",
                    "message": message,
                    "timezone": body.timezone,
                    "artifact_context": artifact_context,
                    "browser_requested": store.chat_browser_requested(store.space_id, thread_id),
                }
            )
            operation_id = str(uuid.uuid4())
            claimed = None
            if body.attachment_set_id is not None:
                claimed = attachment_store.claim(
                    project_id=store.space_id,
                    chat_id=thread_id,
                    client_id=body.attachment_client_id,
                    attachment_set_id=body.attachment_set_id,
                    operation_id=operation_id,
                )
                turn = turn.model_copy(
                    update={
                        "attachment_batch_id": claimed.attachment_batch_id,
                        "attachments": claimed.attachments,
                    }
                )
            batch = claimed.attachment_batch_id if claimed is not None else None
            with released_unless_started(store, attachment_store, batch, operation_id):
                task = background_tasks.start(
                    store.space_id,
                    "space_chat",
                    turn,
                    operation_id=operation_id,
                    authorized_by=authorized,
                )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"operation_id": task.operation_id}


@router.get("/api/agent-threads/{thread_id}/tasks/{operation_id}")
def space_task(
    thread_id: str,
    operation_id: str,
    request: Request,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
):
    thread = owned_thread(store, thread_id, identity_access.acting_user(request).user_id)
    task = _space_task(store, thread, operation_id)
    detail = _agent_task_response(store, task, background_tasks)
    detail["events"] = [
        event.model_dump(mode="json") for event in store.agent_task_events(operation_id)
    ]
    return detail


@router.post("/api/agent-threads/{thread_id}/tasks/{operation_id}/pause", status_code=202)
def pause_space_task(
    thread_id: str,
    operation_id: str,
    request: Request,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
):
    thread = owned_thread(store, thread_id, identity_access.acting_user(request).user_id)
    _reject_history_only_control(_space_task(store, thread, operation_id))
    try:
        return background_tasks.pause(operation_id).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/agent-threads/{thread_id}/tasks/{operation_id}/resume", status_code=202)
def resume_space_task(
    thread_id: str,
    operation_id: str,
    request: Request,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
    idempotency_key: Annotated[UUID4 | None, Header()] = None,
):
    authorized = identity_access.require_patch_capable_identity(request)
    thread = owned_thread(store, thread_id, authorized.user_id)
    _reject_history_only_control(_space_task(store, thread, operation_id))
    with _space_control_admission(store, authorized, idempotency_key, operation_id, "resume") as (
        existing
    ):
        return existing or background_tasks.resume(
            operation_id, authorized_by=authorized
        ).model_dump(mode="json")


@router.post("/api/agent-threads/{thread_id}/tasks/{operation_id}/retry", status_code=202)
def retry_space_task(
    thread_id: str,
    operation_id: str,
    request: Request,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
    body: RetryAgentTaskRequest | None = None,
    idempotency_key: Annotated[UUID4 | None, Header()] = None,
):
    authorized = identity_access.require_patch_capable_identity(request)
    thread = owned_thread(store, thread_id, authorized.user_id)
    _reject_history_only_control(_space_task(store, thread, operation_id))
    # A Space thread always runs on the backend machine, so a retry's run_on is not a choice.
    overrides = body.model_dump(exclude_none=True, exclude={"run_on"}) if body else {}
    with _space_control_admission(store, authorized, idempotency_key, operation_id, "retry") as (
        existing
    ):
        return existing or background_tasks.retry(
            operation_id, authorized_by=authorized, **overrides
        ).model_dump(mode="json")


@contextmanager
def _space_control_admission(
    store: AppStore,
    authorized: AuthorizedHuman,
    idempotency_key: UUID4 | None,
    operation_id: str,
    action: str,
) -> Iterator[dict[str, object] | None]:
    """Admit one Retry or Resume per key; a repeat gets the task the first one started."""
    try:
        with store.client_request_admission(
            store.space_id,
            authorized.user_id,
            str(idempotency_key) if idempotency_key is not None else None,
            f"tasks/{operation_id}/{action}",
        ) as admitted:
            record = store.agent_task(admitted.operation_id or "") if admitted else None
            yield record.model_dump(mode="json") if record else None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/agent-threads/{thread_id}/tasks/{operation_id}/steer")
def steer_space_task(
    thread_id: str,
    operation_id: str,
    body: SteerAgentTaskRequest,
    request: Request,
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
):
    authorized = identity_access.require_patch_capable_identity(request)
    thread = owned_thread(store, thread_id, authorized.user_id)
    task = _space_task(store, thread, operation_id)
    owner = task_owner(store, task, catalog=catalog)
    if not isinstance(owner, SpaceThreadOwner):
        raise HTTPException(status_code=404, detail="Space thread not found")
    try:
        delivery = begin_chat_steer(
            owner,
            background_tasks,
            task,
            message_id=str(body.message_id),
            attempt=body.attempt,
            expected_turn_id=body.expected_turn_id,
            text=body.message,
        )
        return finish_chat_steer(owner, background_tasks, task, delivery)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.api_route(
    "/api/agent-threads/{thread_id}/tasks/{operation_id}/artifacts/{artifact_id}/{action}",
    methods=["GET", "HEAD"],
)
async def space_artifact(
    thread_id: str,
    operation_id: str,
    artifact_id: str,
    action: Literal["content", "download", "viewer"],
    request: Request,
    presentation: ViewerPresentation = "panel",
    theme: str | None = None,
    color_mode: str | None = None,
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
):
    user = identity_access.acting_user(request)
    thread = owned_thread(store, thread_id, user.user_id)
    _space_task(store, thread, operation_id)
    if action == "viewer":
        return await _artifact_viewer_response(
            store.space_id,
            operation_id,
            artifact_id,
            catalog=catalog,
            store=store,
            head=request.method == "HEAD",
            presentation=presentation,
        )
    if action == "content":
        return await content_agent_artifact(
            store.space_id,
            operation_id,
            artifact_id,
            request,
            presentation=presentation,
            theme=theme,
            color_mode=color_mode,
            catalog=catalog,
            store=store,
        )
    return await download_agent_artifact(
        store.space_id,
        operation_id,
        artifact_id,
        request,
        catalog=catalog,
        store=store,
    )
