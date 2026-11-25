"""Member-owned thread index and controls, sharing the existing chat owners."""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable
from contextlib import nullcontext
from datetime import datetime
from typing import Annotated, Any, Literal, Self, cast
from urllib.parse import quote, urlencode

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from pydantic import UUID4, BaseModel, ConfigDict, Field, model_validator

from rcp.agents.agent_owner import resolve_agent
from rcp.api import tasks as task_routes
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
    chat,
    chat_browser,
    mark_chat_read,
    remove_attachment,
    rename_chat,
    set_chat_browser,
    stamp_shown_at,
    transcript_tasks,
)
from rcp.api.dependencies import (
    project_write_admission,
    require_agent_project_membership,
    require_project_membership,
    require_registered_project,
)
from rcp.api.episodes import serialize_episode
from rcp.api.index import (
    DisplayCacheDependency,
    ExperimentOperationLockDependency,
    SpaceRunMode,
    episode_title,
    space_run_entries,
)
from rcp.api.questions import (
    _answer_continues,
    _newest_episode,
    _require_question_owner,
    _serialize,
    _serialize_all,
)
from rcp.api.tasks import (
    BackgroundTasksDependency,
    _agent_artifact_descriptor,
    _artifact_viewer_response,
    content_agent_artifact,
    download_agent_artifact,
    released_unless_started,
    require_attachment_pair,
)
from rcp.artifact_comments import supports_comments
from rcp.artifact_views import ViewerPresentation
from rcp.attachments import ChatAttachmentStore, ChatAttachmentUpload, ProjectReferenceSelector
from rcp.comments.send import chat_turn_send
from rcp.config import load_manifest
from rcp.core.transition_models import GraphTargetRef
from rcp.history import ReplayHalted
from rcp.limits import (
    AGENT_THREAD_QUEUE_MAX_CHARS,
    AGENT_THREAD_QUEUE_MAX_INVOCATIONS,
    ARTIFACT_CONTEXT_MAX_SELECTIONS,
    CHAT_ATTACHMENT_MAX_COUNT,
    CHAT_DOCK_ENDED_EPISODE_WINDOW,
    SPACE_RUNNING_MAX_ROWS,
)
from rcp.projects import ProjectCatalog
from rcp.providers import ProviderId
from rcp.repository_preview import repository_for_path
from rcp.runs.agent_names import DEFAULT_AGENT_NAMES
from rcp.runs.agent_thread_commands import sent_thread_agent, store_thread_pick
from rcp.runs.agent_thread_queue import (
    admit_queued_comments,
    queued_turn_operation_id,
    release_queued_message,
)
from rcp.runs.agent_threads import (
    episode_watching,
    thread_projection,
    thread_projections,
    thread_scope_id,
)
from rcp.runs.approval_cards import ProposalCardView, draws_proposal_cards, proposal_cards
from rcp.runs.artifact_edit_admission import admit_space_artifact_comment
from rcp.runs.space_chat import admit_space_invocations, space_transcript_lock
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
    QueuedTurnPicks,
    private_artifact_owner,
    space_thread_task,
)
from rcp.storage.agents import AgentRecord
from rcp.storage.chat_names import ChatNameError, chat_titles
from rcp.storage.comments import CommentError
from rcp.storage.models import AgentTaskAdmissionConflict, EpisodeRecord, ProjectRecord
from rcp.storage.notifications import question_recipients
from rcp.storage.question_models import QuestionRecord
from rcp.transport.state import StateUnavailable

router = APIRouter()
_agent_member = Depends(require_agent_project_membership)
logger = logging.getLogger(__name__)


def owned_thread(store: AppStore, thread_id: str, user_id: str) -> AgentThreadRecord:
    thread = store.agent_thread(thread_id)
    if thread is None or thread.user_id != user_id:
        raise HTTPException(status_code=404, detail="Thread not found")
    if thread.project_id and not store.is_project_member(thread.project_id, user_id):
        raise HTTPException(status_code=404, detail="Thread not found")
    return thread


def accessible_agent(store: AppStore, agent_id: str, user_id: str) -> AgentRecord:
    agent = store.agent(agent_id)
    thread = store.agent_thread(agent_id)
    if (
        agent is None
        or (agent.owner_user_id is not None and agent.owner_user_id != user_id)
        or (agent.project_id and not store.is_project_member(agent.project_id, user_id))
        or (thread is not None and thread.closed_at is not None)
    ):
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


def owned_space_thread(store: AppStore, thread_id: str, user_id: str) -> AgentThreadRecord:
    thread = owned_thread(store, thread_id, user_id)
    if thread.project_id is not None or thread.closed_at is not None:
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
    project's local display snapshot, and asked cards from the question list. A named
    thread's read adds that chat's dock (`chat_dock`). A member-wide read, Pip's dock, also
    lists the cards the Space thread asked, and relays questions across the member's
    projects except those a member thread's dock already shows under its episodes.

    A member-wide read skips a project whose snapshot cannot be read; a named thread's
    unreadable project is a 503.
    """
    user_id = identity_access.acting_user(request).user_id
    card_threads = _card_threads(store, user_id, thread_id)
    by_project: dict[str, list[AgentThreadRecord]] = defaultdict(list)
    for thread in card_threads:
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
        if thread_id is None:
            asked.extend(
                _asked_cards(store, project_id, [thread.thread_id for thread in threads], user_id)
            )
    drawn.sort(key=lambda card: card.turn_created_at, reverse=True)
    if thread_id is not None:
        thread = card_threads[0] if card_threads else None
        if thread is None or thread.project_id is None or store.project(thread.project_id) is None:
            return {
                "proposal_cards": drawn,
                "asked_cards": [],
                "questions": [],
                "episodes": [],
                "next_expiry_at": None,
            }
        return {
            "proposal_cards": drawn,
            **chat_dock(
                store, catalog, user_id, thread.project_id, thread.thread_id, relay_project=True
            ),
        }
    space = [
        thread
        for thread in store.agent_threads(user_id)
        if thread.kind == "space" and thread.closed_at is None
    ]
    asked.extend(
        _asked_cards(store, store.space_id, [thread.thread_id for thread in space], user_id)
    )
    asked.sort(key=lambda row: row["created_at"], reverse=True)
    rows = []
    for project_id in sorted(store.member_project_ids(user_id)):
        project = store.project(project_id)
        if project is None:
            continue
        relayable = _relayable_questions(store, project_id, user_id)
        asks = any(question.origin.owner_kind == "episode" for question in relayable)
        shown = {
            question.question_id
            for thread in (by_project.get(project_id, []) if asks else [])
            for _episode, questions in _chat_children(
                store, project_id, thread.thread_id, relayable
            )
            for question in questions
        }
        rows.extend(
            _relayed_rows(
                store,
                catalog,
                project,
                [question for question in relayable if question.question_id not in shown],
            )
        )
    rows.sort(key=lambda row: row["created_at"], reverse=True)
    return {
        "proposal_cards": drawn,
        "asked_cards": asked,
        "questions": rows,
        "episodes": [],
        "next_expiry_at": None,
    }


@router.get(
    "/api/projects/{project_id}/chats/{chat_id}/cards",
    dependencies=[Depends(require_project_membership)],
)
def chat_cards(
    project_id: str,
    chat_id: str,
    request: Request,
    *,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
):
    """A shared or node chat's dock (`chat_dock`); it draws no Proposal cards."""
    project_id = catalog.resolve_project_id(project_id)
    require_registered_project(catalog, project_id)
    user_id = identity_access.acting_user(request).user_id
    _require_question_owner(store, chat_id, user_id)
    if store.project(project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return {
        "proposal_cards": [],
        **chat_dock(store, catalog, user_id, project_id, chat_id, relay_project=False),
    }


def chat_dock(
    store: AppStore,
    catalog: ProjectCatalog,
    user_id: str,
    project_id: str,
    chat_id: str,
    *,
    relay_project: bool,
) -> dict[str, Any]:
    """One chat's dock buckets, disjoint by question id: the chat's own answerable card
    questions, its direct child episodes each with their open relayed questions, and, for
    a Project Agent thread only, the project's other relayed questions."""
    project = store.project(project_id)
    assert project is not None
    relayable = _relayable_questions(store, project_id, user_id)
    children = _chat_children(store, project_id, chat_id, relayable)
    archive_states = store.episode_archive_states(project_id) if children else {}
    next_expiry = min(
        (
            datetime.fromisoformat(episode.ended_at or episode.updated_at)
            + CHAT_DOCK_ENDED_EPISODE_WINDOW
            for episode, _questions in children
            if episode.status in {"completed", "stopped", "failed"}
        ),
        default=None,
    )
    shown = {question.question_id for _episode, questions in children for question in questions}
    asked = _asked_cards(store, project_id, [chat_id], user_id)
    asked.sort(key=lambda row: row["created_at"], reverse=True)
    return {
        "asked_cards": asked,
        "next_expiry_at": next_expiry.isoformat() if next_expiry is not None else None,
        "episodes": [
            {
                "episode": serialize_episode(
                    store,
                    project_id,
                    episode,
                    include_graph_branch=False,
                    archive_state=archive_states[episode.episode_id],
                ).model_dump(mode="json"),
                "title": episode_title(catalog, episode),
                "link": _episode_link(project_id, episode.graph_target, episode.episode_id),
                "questions": _relayed_rows(store, catalog, project, questions),
            }
            for episode, questions in children
        ],
        "questions": _relayed_rows(
            store,
            catalog,
            project,
            [
                question
                for question in relayable
                if relay_project
                and question.question_id not in shown
                and not (
                    question.origin.owner_kind == "chat" and question.origin.owner_id == chat_id
                )
            ],
        ),
    }


def _episode_link(project_id: str, target: GraphTargetRef, episode_id: str) -> str:
    """The app hash that opens one episode on Runs."""
    return (
        f"#/projects/{quote(project_id, safe='')}"
        f"/targets/{quote(target.key, safe='')}/episode/{quote(episode_id, safe='')}"
    )


def _chat_children(
    store: AppStore, project_id: str, chat_id: str, relayable: list[QuestionRecord]
) -> list[tuple[EpisodeRecord, list[QuestionRecord]]]:
    """The chat's direct child episodes that are open or ended within the dock window, newest
    first, each with the relayable questions its continuation chain asked."""
    since = datetime.fromisoformat(store.now()) - CHAT_DOCK_ENDED_EPISODE_WINDOW
    children = store.chat_child_episodes(project_id, chat_id, ended_since=since.isoformat())
    if not children:
        return []
    owners = store.episodes_started_from_chats(
        list(
            {
                question.origin.owner_id
                for question in relayable
                if question.origin.owner_kind == "episode"
            }
        )
    )
    asked: dict[str, list[QuestionRecord]] = defaultdict(list)
    for question in relayable:
        if question.origin.owner_kind != "episode":
            continue
        owner = owners.get(question.origin.owner_id)
        if owner is None or owner.chat_id != chat_id:
            continue
        newest = _newest_episode(store, question)
        if newest is not None:
            asked[newest.episode_id].append(question)
    return [(episode, asked.get(episode.episode_id, [])) for episode in children]


def _relayable_questions(store: AppStore, project_id: str, user_id: str) -> list[QuestionRecord]:
    """The project's open card-less questions the member receives, except those of a chat
    that can no longer take a turn."""
    pending = store.list_questions(project_id=project_id, open_only=True)
    cards = store.approval_cards([question.question_id for question in pending])
    with store.connection() as connection:
        received = [
            question
            for question in pending
            if question.question_id not in cards
            and user_id in question_recipients(connection, question)
        ]
    return [
        question
        for question in received
        if question.origin.owner_kind != "chat"
        or store.chat_turn_refusal(project_id, question.origin.owner_id) is None
    ]


def _relayed_rows(
    store: AppStore, catalog: ProjectCatalog, project: ProjectRecord, asked: list[QuestionRecord]
) -> list[dict[str, Any]]:
    """Relayed question rows newest first, each naming its project and the chat or episode
    that asked."""
    with store.connection() as connection:
        chats = chat_titles(
            connection,
            project.project_id,
            [item.origin.owner_id for item in asked if item.origin.owner_kind == "chat"],
        )
    rows = []
    for question in asked:
        episode = _newest_episode(store, question)
        row = _serialize(store, question, continues=_answer_continues(store, question, episode))
        rows.append(
            {
                **row.model_dump(mode="json"),
                "project_id": project.project_id,
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
            link=_episode_link(entry.project_id, entry.graph_target, entry.episode_id),
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
    store: AppStore, scope_id: str, chat_ids: Iterable[str], user_id: str
) -> list[dict[str, Any]]:
    """The answerable pending card questions of these chats under `scope_id`, each naming
    the project its action acts on, and only while the member still belongs to it.

    A parked plan waits in its turn and the pane, never in a dock.
    """
    asked = []
    for chat_id in chat_ids:
        pending = [
            question
            for question in store.list_questions(
                project_id=scope_id, owner_kind="chat", owner_id=chat_id
            )
            if question.state == "pending"
        ]
        for row in _serialize_all(store, pending):
            if row.card is None or not row.can_answer or row.card["state"] == "parked":
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


@router.get(
    "/api/agent-threads/{thread_id}/transcript",
    response_model=ChatTranscript,
    dependencies=[_agent_member],
)
def thread_transcript(
    thread_id: str,
    request: Request,
    *,
    project_id: str | None = None,
    store: StoreDependency,
    identity_access: IdentityDependency,
    catalog: CatalogDependency,
    branch_id: str | None = None,
):
    """The Space thread's transcript; a project thread's is its project chat's."""
    owner = resolve_agent(
        store, thread_id, identity_access.acting_user(request).user_id, project_id
    )
    if owner.project_id is not None:
        return chat(owner.project_id, thread_id, catalog=catalog, store=store, branch_id=branch_id)
    thread = owned_space_thread(store, thread_id, identity_access.acting_user(request).user_id)
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


@router.post("/api/agent-threads/{thread_id}/title", dependencies=[_agent_member])
def rename_thread(
    thread_id: str,
    body: ChatTitleBody,
    request: Request,
    *,
    project_id: str | None = None,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
):
    owner = resolve_agent(
        store, thread_id, identity_access.acting_user(request).user_id, project_id
    )
    if owner.project_id is not None and owner.agent.visibility == "shared":
        with project_write_admission(owner.project_id, request):
            return rename_chat(
                owner.project_id,
                thread_id,
                body,
                request,
                catalog=catalog,
                store=store,
                identity_access=identity_access,
            )
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


_PickName = Annotated[str, Field(min_length=1, max_length=200)]
_PickReasoning = Annotated[str, Field(min_length=1, max_length=100)]


class ThreadAgentBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    provider: ProviderId
    model: _PickName | None = None
    reasoning: _PickReasoning | None = None
    run_on: _PickName | None = None


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
    """Store the pick for member, Retry, scheduled and handoff turns."""
    thread = owned_thread(store, thread_id, identity_access.acting_user(request).user_id)
    try:
        store_thread_pick(store, catalog, thread, **body.model_dump(), wait_for_sign_in=True)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return thread_projection(store, store.agent_thread(thread_id) or thread)


@router.post("/api/agent-threads/{thread_id}/read", dependencies=[_agent_member])
def read_thread(
    thread_id: str,
    body: ChatReadBody,
    request: Request,
    *,
    project_id: str | None = None,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
    branch_id: str | None = None,
    inventory: bool = False,
):
    owner = resolve_agent(
        store, thread_id, identity_access.acting_user(request).user_id, project_id
    )
    if owner.project_id is not None and owner.agent.visibility == "shared":
        return mark_chat_read(
            owner.project_id,
            thread_id,
            body,
            request,
            catalog=catalog,
            store=store,
            identity_access=identity_access,
            branch_id=branch_id,
            inventory=inventory,
        )
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


@router.get(
    "/api/agent-threads/{thread_id}/browser",
    response_model=ChatBrowserPreference,
    dependencies=[_agent_member],
)
def get_thread_browser(
    thread_id: str,
    request: Request,
    *,
    project_id: str | None = None,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
):
    owner = resolve_agent(
        store, thread_id, identity_access.acting_user(request).user_id, project_id
    )
    if owner.project_id is not None:
        return chat_browser(owner.project_id, thread_id, catalog=catalog, store=store)
    return ChatBrowserPreference(
        browser_requested=store.chat_browser_requested(owner.scope_id, thread_id)
    )


@router.put(
    "/api/agent-threads/{thread_id}/browser",
    response_model=ChatBrowserPreference,
    dependencies=[_agent_member],
)
def thread_browser(
    thread_id: str,
    body: ChatBrowserPreference,
    request: Request,
    background_tasks: BackgroundTasks,
    *,
    project_id: str | None = None,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
):
    owner = resolve_agent(
        store, thread_id, identity_access.acting_user(request).user_id, project_id
    )
    if owner.project_id:
        with project_write_admission(owner.project_id, request):
            return set_chat_browser(
                owner.project_id, thread_id, body, background_tasks, catalog=catalog, store=store
            )
    scope = owner.scope_id
    store.set_chat_browser_requested(scope, thread_id, browser_requested=body.browser_requested)
    if not body.browser_requested:
        background_tasks.add_task(_delete_browser_if_still_off, store, scope, thread_id)
    return body


@router.post(
    "/api/agent-threads/{thread_id}/attachments",
    response_model=ChatAttachmentUpload,
    dependencies=[_agent_member],
)
def upload_space_attachment(
    thread_id: str,
    request: Request,
    file: Annotated[UploadFile, File()],
    client_id: Annotated[str, Form()],
    attachment_set_id: Annotated[str | None, Form()] = None,
    *,
    project_id: str | None = None,
    store: StoreDependency,
    identity_access: IdentityDependency,
    attachment_store: AttachmentStoreDependency,
) -> ChatAttachmentUpload:
    owner = resolve_agent(
        store, thread_id, identity_access.acting_user(request).user_id, project_id
    )
    with owner.write_admission(request):
        return add_attachment(
            attachment_store, owner.scope_id, thread_id, file, client_id, attachment_set_id
        )


@router.delete(
    "/api/agent-threads/{thread_id}/attachments/{attachment_id}", dependencies=[_agent_member]
)
def remove_space_attachment(
    thread_id: str,
    attachment_id: str,
    client_id: str,
    attachment_set_id: str,
    request: Request,
    *,
    project_id: str | None = None,
    store: StoreDependency,
    identity_access: IdentityDependency,
    attachment_store: AttachmentStoreDependency,
) -> dict[str, bool]:
    owner = resolve_agent(
        store, thread_id, identity_access.acting_user(request).user_id, project_id
    )
    with owner.write_admission(request):
        return remove_attachment(
            attachment_store, owner.scope_id, thread_id, attachment_id, client_id, attachment_set_id
        )


def owned_project_thread(store: AppStore, thread_id: str, user_id: str) -> AgentThreadRecord:
    thread = owned_thread(store, thread_id, user_id)
    if thread.project_id is None:
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
    provider: ProviderId | None = Field(default=None, exclude_if=lambda value: value is None)
    model: _PickName | None = Field(default=None, exclude_if=lambda value: value is None)
    reasoning: _PickReasoning | None = Field(default=None, exclude_if=lambda value: value is None)
    invoked_workflow_ids: list[_PickName] = Field(
        default_factory=list,
        max_length=AGENT_THREAD_QUEUE_MAX_INVOCATIONS,
        exclude_if=lambda value: not value,
    )
    invoked_skill_ids: list[_PickName] = Field(
        default_factory=list,
        max_length=AGENT_THREAD_QUEUE_MAX_INVOCATIONS,
        exclude_if=lambda value: not value,
    )
    invoked_provider_skill_names: list[_PickName] = Field(
        default_factory=list,
        max_length=AGENT_THREAD_QUEUE_MAX_INVOCATIONS,
        exclude_if=lambda value: not value,
    )
    comment_thread_ids: list[str] = Field(
        default_factory=list,
        max_length=ARTIFACT_CONTEXT_MAX_SELECTIONS,
        exclude_if=lambda value: not value,
    )

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        require_attachment_pair(self.attachment_set_id, self.attachment_client_id)
        if not (
            self.message.strip()
            or self.attachment_set_id
            or self.references
            or self.comment_thread_ids
        ):
            raise ValueError("A queued message needs text, files, or comment threads")
        if self.provider is None and (self.model or self.reasoning):
            raise ValueError("A queued model or reasoning pick needs its provider")
        return self

    def request_sha256(self) -> str:
        """Unpicked fields stay out, so a body without picks hashes as it did before them."""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    def picks(self) -> QueuedTurnPicks:
        return QueuedTurnPicks.model_validate(
            self.model_dump(include=set(QueuedTurnPicks.model_fields))
        )


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
    thread = accessible_agent(store, thread_id, authorized.user_id)
    with (
        project_write_admission(thread.project_id, request)
        if thread.project_id
        else nullcontext(store.space_id)
    ) as project_id:
        queued = queue_message(
            store,
            catalog,
            attachment_store,
            thread,
            project_id,
            authorized.user_id,
            body,
            str(idempotency_key),
        )
    request.app.state.reconcile_thread_handoffs()
    current = store.agent_thread_queue_item(queued.queue_id)
    return {
        "queue_id": queued.queue_id,
        "operation_id": current.operation_id if current is not None else None,
    }


def queue_message(
    store: AppStore,
    catalog: ProjectCatalog,
    attachment_store: ChatAttachmentStore,
    thread: AgentRecord,
    project_id: str,
    user_id: str,
    body: QueuedMessageBody,
    key: str,
) -> AgentThreadQueueRecord:
    """The thread's queued message for `key`, stored now or by an earlier try of it."""
    if refusal := store.chat_turn_refusal(project_id, thread.agent_id):
        raise HTTPException(status_code=409, detail=refusal)
    if thread.project_id is None and body.invoked_provider_skill_names:
        raise HTTPException(status_code=422, detail="A Space turn takes no provider skills")
    queued = store.agent_thread_queue_for_key(thread.agent_id, key) or _queue_new_message(
        store, catalog, attachment_store, thread, project_id, user_id, body, key
    )
    if queued.user_id != user_id or queued.request_sha256 != body.request_sha256():
        raise HTTPException(status_code=422, detail="The key was used for another message")
    return queued


def _queue_new_message(
    store: AppStore,
    catalog: ProjectCatalog,
    attachment_store: ChatAttachmentStore,
    thread: AgentRecord,
    project_id: str,
    user_id: str,
    body: QueuedMessageBody,
    key: str,
) -> AgentThreadQueueRecord:
    """Claim the files for the queued turn's own id, then store the row, or release them."""
    queue_id = str(uuid.uuid4())
    operation_id = queued_turn_operation_id(queue_id)
    sent = None
    if body.comment_thread_ids:
        try:
            sent = admit_queued_comments(
                store, catalog, thread, user_id, body.comment_thread_ids, key, body.message
            ).sent
        except CommentError:
            raise
        except ValueError as exc:
            status = 409 if isinstance(exc, AgentTaskAdmissionConflict) else 422
            raise HTTPException(status_code=status, detail=str(exc)) from exc
    client_id = body.attachment_client_id or (str(uuid.uuid4()) if body.references else None)
    try:
        claimed, batch = claim_turn_attachments(
            store,
            catalog,
            attachment_store,
            project_id,
            RunRequest(
                chat_id=thread.agent_id,
                chat_scope="project",
                attachment_set_id=body.attachment_set_id,
                attachment_client_id=client_id,
                references=body.references,
            ),
            operation_id=operation_id,
            viewer_id=user_id,
        )
    except ValueError as exc:
        if (raced := store.agent_thread_queue_for_key(thread.agent_id, key)) is not None:
            return raced
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        queued = store.queue_agent_thread_message(
            queue_id=queue_id,
            thread_id=thread.agent_id,
            user_id=user_id,
            idempotency_key=key,
            message=body.message,
            attachment_set_id=batch,
            attachment_client_id=client_id,
            attachment_count=len(claimed.attachments),
            request_sha256=body.request_sha256(),
            picks=body.picks(),
            rendered_through=sent.rendered_through if sent is not None else None,
            sent_operation_id=operation_id,
        )
    except (AgentThreadQueueFull, ValueError) as exc:
        if batch is not None:
            attachment_store.release(batch, operation_id)
        if isinstance(exc, CommentError):
            raise
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
    user_id = identity_access.acting_user(request).user_id
    thread = accessible_agent(store, thread_id, user_id)
    row = store.agent_thread_queue_item(queue_id)
    if (
        row is None
        or row.thread_id != thread.agent_id
        or row.user_id != user_id
        or row.removed_at is not None
    ):
        raise HTTPException(status_code=404, detail="Queued message not found")
    removed = store.remove_agent_thread_queue_item(queue_id)
    if removed is None:
        raise HTTPException(status_code=409, detail="This message already started")
    if removed.refusal_reason is None:
        release_queued_message(store, attachment_store, removed)
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
    user_id = identity_access.acting_user(request).user_id
    thread = accessible_agent(store, thread_id, user_id)
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
        for row in store.agent_thread_queue(thread.agent_id)
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
    comment_thread_ids: list[str] = Field(
        default_factory=list, max_length=ARTIFACT_CONTEXT_MAX_SELECTIONS
    )
    invoked_workflow_ids: list[str] = Field(default_factory=list)
    invoked_skill_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_attachment_pair(self) -> Self:
        require_attachment_pair(self.attachment_set_id, self.attachment_client_id)
        return self

    @model_validator(mode="after")
    def validate_message(self) -> Self:
        if not self.message and not self.comment_thread_ids:
            raise ValueError("A Space turn needs a message or a comment")
        return self


def _space_artifact_comment(
    store: AppStore, thread: AgentThreadRecord, context: ArtifactContextRequest | None, text: str
) -> tuple[ArtifactContextRequest | None, str]:
    if context is None:
        return None, text
    try:
        return admit_space_artifact_comment(store, thread, context, text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _space_task(store: AppStore, thread: AgentThreadRecord, operation_id: str) -> AgentTaskRecord:
    task = space_thread_task(store, thread, operation_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Agent task not found")
    return task


@router.post("/api/agent-threads/{thread_id}/turns", status_code=202, dependencies=[_agent_member])
def space_turn(
    thread_id: str,
    body: dict[str, object],
    request: Request,
    *,
    project_id: str | None = None,
    store: StoreDependency,
    catalog: CatalogDependency,
    identity_access: IdentityDependency,
    background_tasks: BackgroundTasksDependency,
    attachment_store: AttachmentStoreDependency,
    idempotency_key: Annotated[UUID4 | None, Header()] = None,
    branch_id: str | None = None,
):
    authorized = identity_access.require_patch_capable_identity(request)
    owner = resolve_agent(store, thread_id, authorized.user_id, project_id)
    if owner.project_id is not None:
        if body.get("chat_id") not in (None, thread_id):
            raise HTTPException(status_code=422, detail="Turn belongs to another agent")
        try:
            known = store.chat_scope(owner.project_id, thread_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if known:
            body = {**body, "chat_scope": known[0], "node_id": known[1]}
        with project_write_admission(owner.project_id, request):
            return task_routes.start_agent_task(
                owner.project_id,
                owner.turn_kind(body),
                {**body, "chat_id": thread_id},
                request,
                catalog=catalog,
                store=store,
                identity_access=identity_access,
                attachment_store=attachment_store,
                background_tasks=background_tasks,
                idempotency_key=idempotency_key,
                branch_id=branch_id,
            )
    try:
        payload = SpaceTurnBody.model_validate(body)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    thread = owned_space_thread(store, thread_id, authorized.user_id)
    key = str(idempotency_key) if idempotency_key is not None else None
    sent = (
        chat_turn_send(
            store,
            store.space_id,
            authorized.user_id,
            payload.comment_thread_ids,
            key or "",
            message=payload.message,
            chat_id=thread_id,
            paper=False,
        )
        if payload.comment_thread_ids
        else None
    )
    try:
        with store.client_request_admission(
            store.space_id,
            authorized.user_id,
            key,
            f"agent-threads/{thread_id}/turns",
        ) as existing:
            if existing is not None:
                if sent is not None:
                    record = store.agent_task(existing.operation_id or "")
                    assert record is not None
                    sent.require_replay_of(record)
                    store.mark_threads_sent(
                        sent.rendered_through, record.operation_id, key or "", authorized.user_id
                    )
                return {"operation_id": existing.operation_id}
            context = sent.context if sent is not None else None
            with store.artifact_lock(context.artifact_id) if context is not None else nullcontext():
                artifact_context, message = _space_artifact_comment(
                    store, thread, context, sent.text if sent is not None else payload.message
                )
                workflows, skills = admit_space_invocations(
                    payload.invoked_workflow_ids, payload.invoked_skill_ids
                )
                turn = RunRequest.model_validate(
                    {
                        **sent_thread_agent(store, thread, payload.model_dump()),
                        "invoked_workflow_ids": workflows,
                        "invoked_skill_ids": skills,
                        "chat_id": thread_id,
                        "chat_scope": "project",
                        "mode": "work",
                        "message": message,
                        "timezone": payload.timezone,
                        "artifact_context": artifact_context,
                        "browser_requested": store.chat_browser_requested(
                            store.space_id, thread_id
                        ),
                    }
                )
                operation_id = str(uuid.uuid4())
                claimed = None
                if payload.attachment_set_id is not None:
                    claimed = attachment_store.claim(
                        project_id=store.space_id,
                        chat_id=thread_id,
                        client_id=payload.attachment_client_id,
                        attachment_set_id=payload.attachment_set_id,
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
                if sent is not None:
                    store.mark_threads_sent(
                        sent.rendered_through, task.operation_id, key or "", authorized.user_id
                    )
    except CommentError:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"operation_id": task.operation_id}


@router.get("/api/agent-threads/{thread_id}/artifacts/{artifact_id}/state")
def space_artifact_state(
    thread_id: str,
    artifact_id: str,
    request: Request,
    response: Response,
    *,
    store: StoreDependency,
    identity_access: IdentityDependency,
):
    thread = owned_thread(store, thread_id, identity_access.acting_user(request).user_id)
    with store.artifact_lock(artifact_id):
        artifact = store.artifact(artifact_id)
        if artifact is None or artifact.project_id != store.space_id:
            raise HTTPException(status_code=404, detail="Artifact not found")
        origin = _space_task(store, thread, artifact.origin_operation_id or "")
        descriptor = _agent_artifact_descriptor(origin, artifact_id)
        if store.readable_artifact(store.space_id, artifact_id) is None:
            raise HTTPException(status_code=410, detail="Artifact expired")
        versions = store.artifact_versions(artifact_id)
        current = next(
            version for version in versions if version.version_id == artifact.current_version
        )
    base = (
        f"/api/agent-threads/{quote(thread_id, safe='')}/tasks/{quote(origin.operation_id, safe='')}"
        f"/artifacts/{quote(artifact_id, safe='')}/viewer"
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "current_version": current.version_id,
        "version_number": versions.index(current) + 1,
        "viewer_url": base + "?" + urlencode({"version_id": current.version_id}),
        "can_comment": thread.closed_at is None and supports_comments(descriptor.media_type),
        "can_keep": False,
        "fresh_session_required": False,
        "live": None,
        "editing_operation_id": None,
    }


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
    version_id: str | None = None,
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
            version_id=version_id,
        )
    if action == "content":
        return await content_agent_artifact(
            store.space_id,
            operation_id,
            artifact_id,
            request,
            version_id=version_id,
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
