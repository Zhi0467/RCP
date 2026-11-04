"""Private thread identities, durable handoffs, queued messages and Space-turn project bindings."""

from __future__ import annotations

import sqlite3
import uuid
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel

from rcp.storage.artifact_models import Artifact
from rcp.storage.chat_names import normalize_chat_name
from rcp.storage.mixin_base import StoreMixinBase
from rcp.storage.models import AgentTaskRecord
from rcp.storage.resource_versions import (
    install_agent_thread_version,
    install_agent_thread_versions,
)

if TYPE_CHECKING:
    from rcp.storage import AppStore


def private_artifact_owner(connection: sqlite3.Connection, artifact: Artifact) -> str | None:
    """Resolve saved output privacy from its chat or producing task, including old rows."""
    row = connection.execute(
        "SELECT user_id FROM agent_threads WHERE thread_id = ? OR thread_id IN ("
        "SELECT json_extract(request_json, '$.chat_id') FROM graph_runs "
        "WHERE operation_id IN (?, ?)) LIMIT 1",
        (
            artifact.chat_id,
            artifact.origin_operation_id,
            artifact.supplier_id if artifact.supplier == "turn" else None,
        ),
    ).fetchone()
    return str(row[0]) if row is not None else None


class AgentThreadRecord(BaseModel):
    thread_id: str
    user_id: str
    kind: Literal["space", "project"]
    project_id: str | None = None
    created_at: str
    closed_at: str | None = None
    closed_reason: str | None = None
    agent_provider: str | None = None
    agent_model: str | None = None
    agent_reasoning: str | None = None
    agent_run_on: str | None = None

    @property
    def agent(self) -> dict[str, str | None] | None:
        """The member's stored agent pick; None until one is made."""
        if self.agent_provider is None:
            return None
        return {
            "provider": self.agent_provider,
            "model": self.agent_model,
            "reasoning": self.agent_reasoning,
            "run_on": self.agent_run_on,
        }


def thread_binds_task(thread: AgentThreadRecord, task: AgentTaskRecord, space_id: str) -> bool:
    """The task is a turn of this thread: its kind and scope, authorized by its member."""
    return (
        task.request.get("chat_id") == thread.thread_id
        and task.kind == ("space_chat" if thread.kind == "space" else "project_chat")
        and task.project_id == (thread.project_id or space_id)
        and task.authorized_by is not None
        and task.authorized_by.user_id == thread.user_id
    )


def bound_thread(store: AppStore, task: AgentTaskRecord) -> AgentThreadRecord | None:
    """The member thread this task is a turn of; callers add their own conditions."""
    chat_id = task.request.get("chat_id")
    thread = store.agent_thread(chat_id) if isinstance(chat_id, str) else None
    return thread if thread and thread_binds_task(thread, task, store.space_id) else None


class AgentHandoffRecord(BaseModel):
    handoff_id: str
    source_thread_id: str
    source_operation_id: str
    idempotency_key: str
    target_thread_id: str
    message: str
    created_at: str
    target_operation_id: str | None = None
    settled_at: str | None = None
    notified_operation_id: str | None = None
    #: Why a handoff settled without delivery; None for a delivered turn.
    refusal_reason: str | None = None


class AgentThreadQueueRecord(BaseModel):
    queue_id: str
    thread_id: str
    user_id: str
    idempotency_key: str
    message: str
    attachment_set_id: str | None = None
    attachment_client_id: str | None = None
    attachment_count: int = 0
    created_at: str
    operation_id: str | None = None
    refusal_reason: str | None = None
    deferred_at: str | None = None


class AgentThreadQueueFull(Exception):
    """The thread already holds its limit of unstarted queued messages."""


def _unused_name(names: tuple[str, ...], used: set[str]) -> str:
    """The first unused seeded name, then numbered suffixes."""
    suffix = 1
    while True:
        candidates = [name if suffix == 1 else f"{name} {suffix}" for name in names]
        if name := next((candidate for candidate in candidates if candidate not in used), None):
            return normalize_chat_name(name)
        suffix += 1


def migrate_agent_threads(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE agent_threads (
        thread_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('space', 'project')),
        project_id TEXT, created_at TEXT NOT NULL, closed_at TEXT, closed_reason TEXT,
        agent_provider TEXT, agent_model TEXT, agent_reasoning TEXT, agent_run_on TEXT,
        CHECK((kind='space' AND project_id IS NULL) OR
              (kind='project' AND project_id IS NOT NULL)))""")
    connection.execute(
        "CREATE UNIQUE INDEX agent_threads_space ON agent_threads(user_id) WHERE kind='space'"
    )
    connection.execute(
        "CREATE UNIQUE INDEX agent_threads_project ON agent_threads(user_id, project_id) WHERE kind='project'"
    )

    install_agent_thread_versions(connection)


def migrate_agent_handoffs(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE agent_handoffs (
        handoff_id TEXT PRIMARY KEY, source_thread_id TEXT NOT NULL,
        source_operation_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
        target_thread_id TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL,
        target_operation_id TEXT, settled_at TEXT, notified_operation_id TEXT,
        refusal_reason TEXT, message_projected_at TEXT, delivery_deferred_at TEXT,
        UNIQUE(source_operation_id, idempotency_key))""")
    # Partial indexes keep each dispatcher pass to the unsettled and unreported rows.
    connection.execute("""CREATE INDEX agent_handoffs_pending ON agent_handoffs(target_thread_id, created_at)
        WHERE target_operation_id IS NULL AND settled_at IS NULL""")
    connection.execute("""CREATE INDEX agent_handoffs_delivered ON agent_handoffs(target_operation_id)
        WHERE target_operation_id IS NOT NULL AND settled_at IS NULL""")
    connection.execute("""CREATE INDEX agent_handoffs_owed_wake ON agent_handoffs(settled_at, handoff_id)
        WHERE settled_at IS NOT NULL AND notified_operation_id IS NULL""")

    install_agent_thread_version(
        connection,
        "agent_handoffs",
        "SELECT user_id FROM agent_threads WHERE thread_id=ROW.target_thread_id",
    )


def migrate_agent_thread_queue(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE agent_thread_queue (
        queue_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, user_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL, message TEXT NOT NULL,
        attachment_set_id TEXT, attachment_client_id TEXT,
        attachment_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
        operation_id TEXT, refusal_reason TEXT, deferred_at TEXT,
        UNIQUE(thread_id, idempotency_key))""")
    connection.execute("""CREATE INDEX agent_thread_queue_pending ON agent_thread_queue(thread_id, created_at)
        WHERE operation_id IS NULL AND refusal_reason IS NULL""")
    install_agent_thread_version(connection, "agent_thread_queue", "ROW.user_id")


def migrate_space_turn_project_bindings(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE space_turn_project_bindings (
        operation_id TEXT NOT NULL, project_id TEXT NOT NULL,
        dispatch_authority_json TEXT NOT NULL, write_scope_fingerprint TEXT NOT NULL,
        graph_target_json TEXT NOT NULL, PRIMARY KEY(operation_id, project_id))""")


class AgentThreadStoreMixin(StoreMixinBase):
    def bump_agent_threads_version(self, user_id: str) -> None:
        """A file-only transcript append (voice) moves no SQL row, so it bumps directly."""
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO resource_versions(scope,resource,entity,generation) "
                "VALUES('', 'agent_threads', ?, 1) ON CONFLICT(scope,resource,entity) "
                "DO UPDATE SET generation=generation+1",
                (f"member:{user_id}",),
            )

    def via_agent(self, operation_id: str | None) -> dict[str, str] | None:
        with self.connection() as connection:
            return agent_thread_attribution(connection, operation_id)

    def agent_thread(self, thread_id: str) -> AgentThreadRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM agent_threads WHERE thread_id=?", (thread_id,)
            ).fetchone()
        return AgentThreadRecord.model_validate(dict(row)) if row is not None else None

    def agent_threads(self, user_id: str) -> list[AgentThreadRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_threads WHERE user_id=? ORDER BY kind DESC, project_id",
                (user_id,),
            ).fetchall()
        return [AgentThreadRecord.model_validate(dict(row)) for row in rows]

    def private_chat_owner(self, chat_id: str) -> str | None:
        thread = self.agent_thread(chat_id)
        return thread.user_id if thread else None

    def private_chat_ids_except(self, user_id: str) -> set[str]:
        with self.connection() as connection:
            return {
                str(row[0])
                for row in connection.execute(
                    "SELECT thread_id FROM agent_threads WHERE user_id != ?", (user_id,)
                )
            }

    def ensure_agent_threads(
        self, user_id: str, project_ids: list[str], *, default_names: tuple[str, ...]
    ) -> list[AgentThreadRecord]:
        """Create each identity once; initialize name and browser grant atomically."""
        if not default_names:
            raise ValueError("default names must not be empty")
        names = tuple(normalize_chat_name(name) for name in default_names)
        # The common call finds every thread open and shown; it takes no write lock.
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT t.*, d.archived_at AS display_archived_at FROM agent_threads t "
                "LEFT JOIN chat_display d ON d.chat_id=t.thread_id AND d.project_id="
                "coalesce(t.project_id, (SELECT space_id FROM space_identity WHERE singleton=1)) "
                "WHERE t.user_id=? ORDER BY t.kind DESC, t.project_id",
                (user_id,),
            ).fetchall()
        shown = {
            row["project_id"]
            for row in rows
            if row["closed_at"] is None and row["display_archived_at"] is None
        }
        if {None, *project_ids} <= shown:
            return [AgentThreadRecord.model_validate(dict(row)) for row in rows]
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            space_id = str(
                connection.execute(
                    "SELECT space_id FROM space_identity WHERE singleton=1"
                ).fetchone()[0]
            )
            used = {
                str(row[0])
                for row in connection.execute(
                    "SELECT d.title FROM agent_threads t JOIN chat_display d ON d.chat_id=t.thread_id AND d.project_id=coalesce(t.project_id, ?) WHERE t.user_id=?",
                    (space_id, user_id),
                )
            }
            for project_id in [None, *sorted(set(project_ids))]:
                row = connection.execute(
                    "SELECT thread_id FROM agent_threads WHERE user_id=? AND project_id IS ?",
                    (user_id, project_id),
                ).fetchone()
                if row is not None:
                    connection.execute(
                        "UPDATE agent_threads SET closed_at=NULL, closed_reason=NULL WHERE thread_id=?",
                        (row[0],),
                    )
                    connection.execute(
                        "UPDATE chat_display SET archived_at=NULL, archived_user_id=NULL "
                        "WHERE project_id=? AND chat_id=?",
                        (project_id or space_id, row[0]),
                    )
                    continue
                name = _unused_name(names, used)
                used.add(name)
                thread_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO agent_threads(thread_id,user_id,kind,project_id,created_at) VALUES(?,?,?,?,?)",
                    (
                        thread_id,
                        user_id,
                        "project" if project_id is not None else "space",
                        project_id,
                        self.now(),
                    ),
                )
                scope_id = project_id or space_id
                connection.execute(
                    "INSERT INTO chat_display(project_id,chat_id,title) VALUES(?,?,?)",
                    (scope_id, thread_id, name),
                )
                connection.execute(
                    "INSERT INTO chat_browser_preferences(project_id,chat_id,browser_requested) VALUES(?,?,1)",
                    (scope_id, thread_id),
                )
        return self.agent_threads(user_id)

    def set_agent_thread_pick(
        self,
        thread_id: str,
        *,
        provider: str,
        model: str | None,
        reasoning: str | None,
        run_on: str | None,
    ) -> None:
        """Every later turn on the thread runs this pick: sends, Retry, wakes and handoffs."""
        with self.connection() as connection:
            connection.execute(
                "UPDATE agent_threads SET agent_provider=?, agent_model=?, agent_reasoning=?, "
                "agent_run_on=? WHERE thread_id=?",
                (provider, model, reasoning, run_on, thread_id),
            )

    def close_agent_thread(self, thread_id: str, *, reason: str) -> None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT project_id FROM agent_threads WHERE thread_id=?", (thread_id,)
            ).fetchone()
            if row is not None:
                store = cast("AppStore", self)
                store._stop_chat_watchers(connection, row[0] or store.space_id, thread_id)
            connection.execute(
                "UPDATE agent_threads SET closed_at=coalesce(closed_at,?),closed_reason=? WHERE thread_id=?",
                (self.now(), reason, thread_id),
            )
            connection.execute(
                "UPDATE agent_schedules SET state='disabled', disabled_reason='access_lost', "
                "pending_since=NULL, next_fire_at=NULL WHERE thread_id=? AND state='active'",
                (thread_id,),
            )

    def create_agent_handoff(
        self,
        *,
        source_thread_id: str,
        source_operation_id: str,
        idempotency_key: str,
        target_thread_id: str,
        message: str,
    ) -> AgentHandoffRecord:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owners = connection.execute(
                "SELECT 1 FROM agent_threads source JOIN agent_threads target ON source.user_id=target.user_id WHERE source.thread_id=? AND source.kind='space' AND target.thread_id=? AND target.kind='project' AND source.closed_at IS NULL AND target.closed_at IS NULL",
                (source_thread_id, target_thread_id),
            ).fetchone()
            if owners is None:
                raise ValueError("handoff must target the same member's open project thread")
            existing = connection.execute(
                "SELECT * FROM agent_handoffs WHERE source_operation_id=? AND idempotency_key=?",
                (source_operation_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                record = AgentHandoffRecord.model_validate(dict(existing))
                if (record.source_thread_id, record.target_thread_id, record.message) != (
                    source_thread_id,
                    target_thread_id,
                    message,
                ):
                    raise ValueError("handoff idempotency key reused with different arguments")
                return record
            record = AgentHandoffRecord(
                handoff_id=str(uuid.uuid4()),
                source_thread_id=source_thread_id,
                source_operation_id=source_operation_id,
                idempotency_key=idempotency_key,
                target_thread_id=target_thread_id,
                message=message,
                created_at=self.now(),
            )
            connection.execute(
                "INSERT INTO agent_handoffs(handoff_id,source_thread_id,source_operation_id,idempotency_key,target_thread_id,message,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    record.handoff_id,
                    source_thread_id,
                    source_operation_id,
                    idempotency_key,
                    target_thread_id,
                    message,
                    record.created_at,
                ),
            )
            return record

    def pending_agent_handoffs(
        self, target_thread_id: str | None = None
    ) -> list[AgentHandoffRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_handoffs WHERE target_operation_id IS NULL AND settled_at IS NULL AND (? IS NULL OR target_thread_id=?) ORDER BY created_at,handoff_id",
                (target_thread_id, target_thread_id),
            ).fetchall()
        return [AgentHandoffRecord.model_validate(dict(row)) for row in rows]

    def refuse_agent_handoff(self, handoff_id: str, reason: str) -> None:
        """A settled handoff without a target turn is a final delivery refusal."""
        with self.connection() as connection:
            connection.execute(
                "UPDATE agent_handoffs SET settled_at=?, refusal_reason=? "
                "WHERE handoff_id=? AND target_operation_id IS NULL AND settled_at IS NULL",
                (self.now(), reason, handoff_id),
            )

    def defer_agent_handoff_delivery(self, handoff_id: str) -> str:
        """Note a transiently refused delivery; return when deferral first began."""
        with self.connection() as connection:
            connection.execute(
                "UPDATE agent_handoffs SET delivery_deferred_at=coalesce(delivery_deferred_at,?) "
                "WHERE handoff_id=?",
                (self.now(), handoff_id),
            )
            row = connection.execute(
                "SELECT delivery_deferred_at FROM agent_handoffs WHERE handoff_id=?", (handoff_id,)
            ).fetchone()
        return str(row[0])

    def mark_agent_handoff_message_projected(self, handoff_id: str) -> None:
        """The request message reached the project transcript; later passes skip it."""
        with self.connection() as connection:
            connection.execute(
                "UPDATE agent_handoffs SET message_projected_at=coalesce(message_projected_at,?) "
                "WHERE handoff_id=?",
                (self.now(), handoff_id),
            )

    def agent_handoff_delivered_as(self, operation_id: str) -> AgentHandoffRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM agent_handoffs WHERE target_operation_id=?", (operation_id,)
            ).fetchone()
        return AgentHandoffRecord.model_validate(dict(row)) if row is not None else None

    def agent_handoff_for_key(
        self, source_operation_id: str, idempotency_key: str
    ) -> AgentHandoffRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM agent_handoffs WHERE source_operation_id=? AND idempotency_key=?",
                (source_operation_id, idempotency_key),
            ).fetchone()
        return AgentHandoffRecord.model_validate(dict(row)) if row is not None else None

    def agent_handoff(self, handoff_id: str) -> AgentHandoffRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM agent_handoffs WHERE handoff_id=?", (handoff_id,)
            ).fetchone()
        return AgentHandoffRecord.model_validate(dict(row)) if row is not None else None

    def queue_agent_thread_message(
        self,
        *,
        queue_id: str,
        thread_id: str,
        user_id: str,
        idempotency_key: str,
        message: str,
        attachment_set_id: str | None = None,
        attachment_client_id: str | None = None,
        attachment_count: int = 0,
    ) -> AgentThreadQueueRecord:
        """Store one member message for the thread's next free turn; a replayed key returns it."""
        from rcp.limits import AGENT_THREAD_QUEUE_MAX_ITEMS

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = _queue_row_for_key(connection, thread_id, idempotency_key)
            if existing is not None:
                if (existing.user_id, existing.message) != (user_id, message):
                    raise ValueError("Queue idempotency key reused with a different message")
                return existing
            waiting = connection.execute(
                "SELECT count(*) FROM agent_thread_queue WHERE thread_id=? AND operation_id IS NULL",
                (thread_id,),
            ).fetchone()[0]
            if waiting >= AGENT_THREAD_QUEUE_MAX_ITEMS:
                raise AgentThreadQueueFull(
                    f"This thread already has {AGENT_THREAD_QUEUE_MAX_ITEMS} queued messages"
                )
            record = AgentThreadQueueRecord(
                queue_id=queue_id,
                thread_id=thread_id,
                user_id=user_id,
                idempotency_key=idempotency_key,
                message=message,
                attachment_set_id=attachment_set_id,
                attachment_client_id=attachment_client_id,
                attachment_count=attachment_count,
                created_at=self.now(),
            )
            connection.execute(
                "INSERT INTO agent_thread_queue(queue_id,thread_id,user_id,idempotency_key,message,"
                "attachment_set_id,attachment_client_id,attachment_count,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    record.queue_id,
                    thread_id,
                    user_id,
                    idempotency_key,
                    message,
                    attachment_set_id,
                    attachment_client_id,
                    attachment_count,
                    record.created_at,
                ),
            )
            return record

    def agent_thread_queue_for_key(
        self, thread_id: str, idempotency_key: str
    ) -> AgentThreadQueueRecord | None:
        with self.connection() as connection:
            return _queue_row_for_key(connection, thread_id, idempotency_key)

    def agent_thread_queue_item(self, queue_id: str) -> AgentThreadQueueRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM agent_thread_queue WHERE queue_id=?", (queue_id,)
            ).fetchone()
        return AgentThreadQueueRecord.model_validate(dict(row)) if row is not None else None

    def agent_thread_queue(self, thread_id: str) -> list[AgentThreadQueueRecord]:
        """The thread's unstarted messages, waiting or refused, oldest first."""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_thread_queue WHERE thread_id=? AND operation_id IS NULL "
                "ORDER BY created_at,queue_id",
                (thread_id,),
            ).fetchall()
        return [AgentThreadQueueRecord.model_validate(dict(row)) for row in rows]

    def pending_agent_thread_queue(self) -> list[AgentThreadQueueRecord]:
        """Each thread's oldest message that is neither started nor refused."""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_thread_queue q WHERE operation_id IS NULL "
                "AND refusal_reason IS NULL AND NOT EXISTS (SELECT 1 FROM agent_thread_queue o "
                "WHERE o.thread_id=q.thread_id AND o.operation_id IS NULL "
                "AND o.refusal_reason IS NULL AND (o.created_at,o.queue_id) < (q.created_at,q.queue_id)) "
                "ORDER BY created_at,queue_id"
            ).fetchall()
        return [AgentThreadQueueRecord.model_validate(dict(row)) for row in rows]

    def delete_agent_thread_queue_item(self, queue_id: str) -> AgentThreadQueueRecord | None:
        """Remove an unstarted message; None when it is missing or its turn already started."""
        with self.connection() as connection:
            row = connection.execute(
                "DELETE FROM agent_thread_queue WHERE queue_id=? AND operation_id IS NULL "
                "RETURNING *",
                (queue_id,),
            ).fetchone()
        return AgentThreadQueueRecord.model_validate(dict(row)) if row is not None else None

    def refuse_agent_thread_queue_item(
        self, queue_id: str, reason: str
    ) -> AgentThreadQueueRecord | None:
        """Refuse a waiting message for good; None when it already started or was refused."""
        with self.connection() as connection:
            row = connection.execute(
                "UPDATE agent_thread_queue SET refusal_reason=? WHERE queue_id=? "
                "AND operation_id IS NULL AND refusal_reason IS NULL RETURNING *",
                (reason, queue_id),
            ).fetchone()
        return AgentThreadQueueRecord.model_validate(dict(row)) if row is not None else None

    def clear_agent_thread_queue_deferral(self, queue_id: str) -> None:
        """A busy thread is ordinary waiting, so the delivery timeout restarts when it frees."""
        with self.connection() as connection:
            connection.execute(
                "UPDATE agent_thread_queue SET deferred_at=NULL WHERE queue_id=? AND operation_id IS NULL",
                (queue_id,),
            )

    def defer_agent_thread_queue_item(self, queue_id: str) -> str:
        """Note a transiently refused start; return when deferral first began."""
        with self.connection() as connection:
            row = connection.execute(
                "UPDATE agent_thread_queue SET deferred_at=coalesce(deferred_at,?) "
                "WHERE queue_id=? RETURNING deferred_at",
                (self.now(), queue_id),
            ).fetchone()
        if row is None:
            raise KeyError(queue_id)
        return str(row[0])


def agent_thread_attribution(
    connection: sqlite3.Connection, operation_id: str | None
) -> dict[str, str] | None:
    """Project provenance names a thread without exposing its private contents."""
    if operation_id is None:
        return None
    row = connection.execute(
        """WITH RECURSIVE origins(operation_id, parent_operation_id, chat_id) AS (
            SELECT operation_id, coalesce(parent_operation_id, json_extract(request_json, '$.started_from_operation_id')), json_extract(request_json, '$.chat_id')
            FROM graph_runs WHERE operation_id=?
            UNION
            SELECT t.operation_id, coalesce(t.parent_operation_id, json_extract(t.request_json, '$.started_from_operation_id')), json_extract(t.request_json, '$.chat_id')
            FROM graph_runs t JOIN origins o ON t.operation_id=o.parent_operation_id
        )
        SELECT a.thread_id, d.title FROM origins o
        JOIN agent_threads a ON a.thread_id=o.chat_id
        JOIN chat_display d ON d.chat_id=a.thread_id
          AND d.project_id=coalesce(a.project_id, (SELECT space_id FROM space_identity WHERE singleton=1))
        LIMIT 1""",
        (operation_id,),
    ).fetchone()
    return {"thread_id": str(row[0]), "name": str(row[1])} if row is not None else None


def claim_handoff_turn(
    connection: sqlite3.Connection,
    record: AgentTaskRecord,
    *,
    handoff_id: str | None,
    handoff_wake_id: str | None,
) -> bool:
    """Bind delivery or its wake inside the transaction inserting the exact task."""
    if handoff_id is not None and handoff_wake_id is not None:
        raise ValueError("A turn cannot both deliver and report a handoff")
    selected = handoff_id or handoff_wake_id
    if selected is None:
        return False
    wake = handoff_wake_id is not None
    row = _require_handoff_turn(connection, selected, record, wake=wake)
    column = "notified_operation_id" if wake else "target_operation_id"
    previous = row[column]
    if previous is not None:
        if previous != record.operation_id:
            raise ValueError("Handoff was already claimed")
        return True
    connection.execute(
        f"UPDATE agent_handoffs SET {column}=? WHERE handoff_id=?", (record.operation_id, selected)
    )
    return False


def _queue_row_for_key(
    connection: sqlite3.Connection, thread_id: str, idempotency_key: str
) -> AgentThreadQueueRecord | None:
    row = connection.execute(
        "SELECT * FROM agent_thread_queue WHERE thread_id=? AND idempotency_key=?",
        (thread_id, idempotency_key),
    ).fetchone()
    return AgentThreadQueueRecord.model_validate(dict(row)) if row is not None else None


def claim_queued_turn(
    connection: sqlite3.Connection, record: AgentTaskRecord, queue_id: str | None
) -> bool:
    """Bind a queued message to the exact task its transaction inserts.

    Returns True when this task already claimed it, so a replayed or racing
    drain reuses the task. A removed, refused or out-of-order message raises.
    """
    if queue_id is None:
        return False
    row = connection.execute(
        "SELECT * FROM agent_thread_queue WHERE queue_id=?", (queue_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Queued message not found")
    if row["refusal_reason"] is not None:
        raise ValueError("Queued message was refused")
    if row["operation_id"] is not None:
        if row["operation_id"] != record.operation_id:
            raise ValueError("Queued message was already claimed")
        return True
    _require_queued_turn(connection, row, record)
    older = connection.execute(
        "SELECT 1 FROM agent_thread_queue WHERE thread_id=? AND operation_id IS NULL "
        "AND refusal_reason IS NULL AND (created_at,queue_id) < (?,?) LIMIT 1",
        (row["thread_id"], row["created_at"], queue_id),
    ).fetchone()
    if older is not None:
        raise ValueError("An older queued message of this thread starts first")
    connection.execute(
        "UPDATE agent_thread_queue SET operation_id=? WHERE queue_id=?",
        (record.operation_id, queue_id),
    )
    return False


def _require_queued_turn(
    connection: sqlite3.Connection, row: sqlite3.Row, record: AgentTaskRecord
) -> None:
    """The turn is the member's own ordinary Work turn on the open thread, with this text and files."""
    thread = connection.execute(
        "SELECT * FROM agent_threads WHERE thread_id=? AND closed_at IS NULL", (row["thread_id"],)
    ).fetchone()
    space_id = connection.execute(
        "SELECT space_id FROM space_identity WHERE singleton=1"
    ).fetchone()[0]
    request = record.request
    if (
        thread is None
        or thread["kind"] != "project"
        or thread["user_id"] != row["user_id"]
        or not thread_binds_task(AgentThreadRecord.model_validate(dict(thread)), record, space_id)
        or request.get("mode") != "work"
        or request.get("trigger", "human") != "human"
        or request.get("source") is not None
        or request.get("message") != row["message"]
        or request.get("attachment_batch_id") != row["attachment_set_id"]
    ):
        raise ValueError("Queued turn changed its member, thread or request")


def _require_handoff_turn(
    connection: sqlite3.Connection, handoff_id: str, record: AgentTaskRecord, *, wake: bool
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM agent_handoffs WHERE handoff_id=?", (handoff_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Handoff not found")
    _require_handoff_thread(connection, row, record, wake=wake)
    if wake and row["settled_at"] is None:
        raise ValueError("Handoff has not settled")
    if not wake:
        _require_handoff_delivery(row, record)
    return row


def _require_handoff_thread(
    connection: sqlite3.Connection, row: sqlite3.Row, record: AgentTaskRecord, *, wake: bool
) -> None:
    """The turn runs on the open thread it names, authorized by that thread's member."""
    column, kind = ("source_thread_id", "space") if wake else ("target_thread_id", "project")
    thread = connection.execute(
        "SELECT * FROM agent_threads WHERE thread_id=? AND closed_at IS NULL", (row[column],)
    ).fetchone()
    space_id = connection.execute(
        "SELECT space_id FROM space_identity WHERE singleton=1"
    ).fetchone()[0]
    if (
        thread is None
        or thread["kind"] != kind
        or record.request.get("mode") != "work"
        or not thread_binds_task(AgentThreadRecord.model_validate(dict(thread)), record, space_id)
    ):
        raise ValueError("Handoff turn changed its member or thread binding")


def _require_handoff_delivery(row: sqlite3.Row, record: AgentTaskRecord) -> None:
    if row["settled_at"] is not None and row["target_operation_id"] is None:
        raise ValueError("Handoff was refused")
    if (
        record.request.get("source") != "handoff"
        or record.request.get("trigger", "human") != "human"
        or record.request.get("message") != row["message"]
    ):
        raise ValueError("Handoff turn changed its request")
