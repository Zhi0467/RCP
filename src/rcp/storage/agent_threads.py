"""Private thread identities, durable handoffs and Space-turn project bindings."""

from __future__ import annotations

import sqlite3
import uuid
from typing import Literal

from pydantic import BaseModel

from rcp.storage.artifact_models import Artifact
from rcp.storage.chat_names import normalize_chat_name
from rcp.storage.mixin_base import StoreMixinBase
from rcp.storage.models import AgentTaskRecord
from rcp.storage.resource_versions import (
    install_agent_thread_version,
    install_agent_thread_versions,
)


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


def migrate_agent_threads(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE agent_threads (
        thread_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('space', 'project')),
        project_id TEXT, created_at TEXT NOT NULL, closed_at TEXT, closed_reason TEXT,
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
        UNIQUE(source_operation_id, idempotency_key))""")
    connection.execute("""CREATE INDEX agent_handoffs_pending ON agent_handoffs(target_thread_id, created_at)
        WHERE target_operation_id IS NULL""")

    install_agent_thread_version(
        connection,
        "agent_handoffs",
        "SELECT user_id FROM agent_threads WHERE thread_id=ROW.target_thread_id",
    )


def migrate_space_turn_project_bindings(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE space_turn_project_bindings (
        operation_id TEXT NOT NULL, project_id TEXT NOT NULL,
        dispatch_authority_json TEXT NOT NULL, write_scope_fingerprint TEXT NOT NULL,
        graph_target_json TEXT NOT NULL, PRIMARY KEY(operation_id, project_id))""")


class AgentThreadStoreMixin(StoreMixinBase):
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
                    continue
                suffix = 1
                while True:
                    candidates = [name if suffix == 1 else f"{name} {suffix}" for name in names]
                    name = next(
                        (candidate for candidate in candidates if candidate not in used), None
                    )
                    if name is not None:
                        name = normalize_chat_name(name)
                        break
                    suffix += 1
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

    def close_agent_thread(self, thread_id: str, *, reason: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE agent_threads SET closed_at=coalesce(closed_at,?),closed_reason=? WHERE thread_id=?",
                (self.now(), reason, thread_id),
            )
            connection.execute(
                "UPDATE agent_schedules SET state='disabled',disabled_reason='access_lost' WHERE thread_id=? AND state='active'",
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
                "SELECT * FROM agent_handoffs WHERE target_operation_id IS NULL AND (? IS NULL OR target_thread_id=?) ORDER BY created_at,handoff_id",
                (target_thread_id, target_thread_id),
            ).fetchall()
        return [AgentHandoffRecord.model_validate(dict(row)) for row in rows]

    def mark_agent_handoff_delivered(self, handoff_id: str, operation_id: str) -> None:
        with self.connection() as connection:
            updated = connection.execute(
                "UPDATE agent_handoffs SET target_operation_id=? WHERE handoff_id=? AND (target_operation_id IS NULL OR target_operation_id=?)",
                (operation_id, handoff_id, operation_id),
            )
            if updated.rowcount != 1:
                raise ValueError("handoff already delivered or missing")


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
    row = connection.execute(
        "SELECT * FROM agent_handoffs WHERE handoff_id=?", (selected,)
    ).fetchone()
    if row is None:
        raise ValueError("Handoff not found")
    wake = handoff_wake_id is not None
    thread_id = row["source_thread_id" if wake else "target_thread_id"]
    thread = connection.execute(
        "SELECT * FROM agent_threads WHERE thread_id=?", (thread_id,)
    ).fetchone()
    if (
        thread is None
        or thread["closed_at"] is not None
        or record.authorized_by is None
        or record.authorized_by.user_id != thread["user_id"]
        or record.request.get("chat_id") != thread_id
        or record.request.get("mode") != "work"
        or record.kind != ("space_chat" if wake else "project_chat")
    ):
        raise ValueError("Handoff turn changed its member or thread binding")
    if not wake and (
        record.project_id != thread["project_id"]
        or record.request.get("source") != "handoff"
        or record.request.get("trigger", "human") != "human"
        or record.request.get("message") != row["message"]
    ):
        raise ValueError("Handoff turn changed its request")
    if wake and row["settled_at"] is None:
        raise ValueError("Handoff has not settled")
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
