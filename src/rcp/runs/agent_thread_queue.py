"""Messages a member queued on a project thread, each started later as their own turn."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import TYPE_CHECKING

from rcp.attachments import ChatAttachmentStore
from rcp.core.models import AuthorizedHuman
from rcp.limits import (
    AGENT_HANDOFF_DELIVERY_TIMEOUT_SECONDS,
    AGENT_THREAD_QUEUE_MAX_CHARS,
    AGENT_THREAD_QUEUE_RETRY_SECONDS,
)
from rcp.projects import ProjectCatalog
from rcp.runs.agent_thread_commands import _TRANSIENT_DELIVERY, _thread_busy, _thread_request
from rcp.runs.turn_attachments import claim_turn_attachments
from rcp.storage import AppStore
from rcp.storage.agent_threads import AgentThreadQueueRecord, AgentThreadRecord

if TYPE_CHECKING:
    from rcp.background import BackgroundAgentTasks

logger = logging.getLogger(__name__)


def queued_turn_operation_id(queue_id: str) -> str:
    """The one task id a queued message can start as, so every drain names the same turn."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"rcp:agent-thread-queue:{queue_id}"))


def queue_attachment_store(catalog: ProjectCatalog) -> ChatAttachmentStore:
    return ChatAttachmentStore(catalog.data_dir / "chat-attachments")


def release_queued_files(
    attachment_store: ChatAttachmentStore, row: AgentThreadQueueRecord
) -> None:
    """Return a removed or refused message's uploads to its set and drop its reference copies."""
    if row.attachment_set_id is None:
        return
    try:
        attachment_store.release(row.attachment_set_id, queued_turn_operation_id(row.queue_id))
    except (ValueError, OSError) as exc:
        logger.warning("Queued message %s kept its files: %s", row.queue_id, exc)


def dispatch_thread_queue(
    store: AppStore,
    catalog: ProjectCatalog,
    background: BackgroundAgentTasks,
    *,
    project_admission: Callable[[str], AbstractContextManager[object]],
) -> list[str]:
    """Start each free thread's oldest queued message; one bad row never blocks the others.

    A deferred message arms one later pass, so it starts or times out without another event.
    """
    admitted: list[str] = []
    for row in store.pending_agent_thread_queue():
        try:
            if operation_id := admit_queued_message(
                store, catalog, background, row, project_admission
            ):
                admitted.append(operation_id)
        except Exception:
            logger.exception("Queued message %s could not be deferred", row.queue_id)
    if any(row.deferred_at is not None for row in store.pending_agent_thread_queue()):
        background.schedule_thread_queue_dispatch(
            AGENT_THREAD_QUEUE_RETRY_SECONDS,
            lambda: dispatch_thread_queue(
                store, catalog, background, project_admission=project_admission
            ),
        )
    return admitted


def admit_queued_message(
    store: AppStore,
    catalog: ProjectCatalog,
    background: BackgroundAgentTasks,
    row: AgentThreadQueueRecord,
    project_admission: Callable[[str], AbstractContextManager[object]],
) -> str | None:
    """Start the message as the member's own turn, refuse it for good, or leave it waiting.

    An unexpected failure defers it like a transient one, so the delivery timeout refuses it.
    """
    try:
        return _admit_queued_message(store, catalog, background, row, project_admission)
    except Exception as exc:
        logger.exception("Queued message %s admission failed", row.queue_id)
        _defer(store, catalog, row, type(exc).__name__)
        return None


def _admit_queued_message(
    store: AppStore,
    catalog: ProjectCatalog,
    background: BackgroundAgentTasks,
    row: AgentThreadQueueRecord,
    project_admission: Callable[[str], AbstractContextManager[object]],
) -> str | None:
    thread = store.agent_thread(row.thread_id)
    ready = _queue_ready(store, thread, row)
    if isinstance(ready, str):
        _refuse(store, catalog, row, ready)
        return None
    if ready is None:
        if row.deferred_at is not None:
            store.clear_agent_thread_queue_deferral(row.queue_id)
        return None
    assert thread is not None
    try:
        return _start_queued_turn(store, catalog, background, row, thread, ready, project_admission)
    except _TRANSIENT_DELIVERY as exc:
        _defer(store, catalog, row, str(exc))
    except (ValueError, KeyError) as exc:
        if _thread_busy(store, thread):
            logger.warning("Queued message %s waits: another turn took the thread", row.queue_id)
        else:
            _refuse(store, catalog, row, str(exc) or type(exc).__name__)
    return None


def _queue_ready(
    store: AppStore, thread: AgentThreadRecord | None, row: AgentThreadQueueRecord
) -> AuthorizedHuman | str | None:
    """The member to start as, a final refusal reason, or None while the thread is busy."""
    if (
        thread is None
        or thread.closed_at
        or thread.project_id is None
        or thread.user_id != row.user_id
    ):
        return "thread_closed"
    if refusal := store.chat_turn_refusal(thread.project_id, thread.thread_id):
        return refusal
    if not store.is_project_member(thread.project_id, row.user_id):
        return "not_project_member"
    member = store.space_user(row.user_id)
    if member is None or member.removed_at or member.removal_started_at:
        return "member_removed"
    if not (member.display_name or "").strip():
        return "display_name_blank"
    if _thread_busy(store, thread):
        return None
    return AuthorizedHuman(
        space_id=store.space_id, user_id=row.user_id, display_name=member.display_name or ""
    )


def _start_queued_turn(
    store: AppStore,
    catalog: ProjectCatalog,
    background: BackgroundAgentTasks,
    row: AgentThreadQueueRecord,
    thread: AgentThreadRecord,
    member: AuthorizedHuman,
    project_admission: Callable[[str], AbstractContextManager[object]],
) -> str:
    from rcp.api.task_requests import _resolved_graph_request
    from rcp.runs.chat_admission import admit_fresh_chat_turn

    project_id = thread.project_id or ""
    operation_id = queued_turn_operation_id(row.queue_id)
    with project_admission(project_id):
        service = catalog.open_execution(project_id)
        request = _thread_request(store, thread, row.message, wake=False).model_copy(
            update={"source": None}
        )
        request = _resolved_graph_request(service, "project_chat", request)
        with admit_fresh_chat_turn(service, store, project_id, request) as request:
            request, _ = claim_turn_attachments(
                store,
                catalog,
                queue_attachment_store(catalog),
                project_id,
                request.model_copy(
                    update={
                        "attachment_set_id": row.attachment_set_id,
                        "attachment_client_id": row.attachment_client_id,
                    }
                ),
                operation_id=operation_id,
                viewer_id=row.user_id,
            )
            return background.start(
                project_id,
                "project_chat",
                request,
                operation_id=operation_id,
                authorized_by=member,
                queue_id=row.queue_id,
            ).operation_id


def _defer(
    store: AppStore, catalog: ProjectCatalog, row: AgentThreadQueueRecord, detail: str
) -> None:
    """Keep the message for a later pass, refusing it once it has waited out the timeout."""
    since = datetime.fromisoformat(store.defer_agent_thread_queue_item(row.queue_id))
    waited = (datetime.fromisoformat(store.now()) - since).total_seconds()
    if waited >= AGENT_HANDOFF_DELIVERY_TIMEOUT_SECONDS:
        _refuse(store, catalog, row, f"delivery_timed_out: {detail}")
    else:
        logger.warning("Queued message %s start deferred: %s", row.queue_id, detail)


def _refuse(
    store: AppStore, catalog: ProjectCatalog, row: AgentThreadQueueRecord, reason: str
) -> None:
    """Refuse for good and release its files, including after a removal raced the claim."""
    refused = store.refuse_agent_thread_queue_item(
        row.queue_id, reason[:AGENT_THREAD_QUEUE_MAX_CHARS]
    )
    if refused is not None or store.agent_thread_queue_item(row.queue_id) is None:
        release_queued_files(queue_attachment_store(catalog), row)
