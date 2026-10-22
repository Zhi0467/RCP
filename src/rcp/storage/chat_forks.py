"""Immutable origins for human-created conversations that fork a native session."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from rcp.core.transition_models import GraphTargetRef
from rcp.storage.chat_names import initialize_chat_name
from rcp.storage.mixin_base import StoreMixinBase

if TYPE_CHECKING:
    from rcp.storage import AppStore


class ChatForkOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_chat_id: str
    source_operation_id: str
    source_graph_target: GraphTargetRef
    source_title: str
    provider: str
    model: str | None
    reasoning: str | None
    run_on: str


class ChatForkRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    chat_id: str
    graph_target: GraphTargetRef
    kind: Literal["node_chat", "project_chat"]
    node_id: str | None
    created_at: str
    origin: ChatForkOrigin
    # Restore detaches the fork intent; the origin stays for the origin link.
    detached: bool = Field(default=False, exclude_if=lambda value: not value)


# The source turn's launch profile a fork's first turn keeps.
FORK_PROFILE_FIELDS = ("provider", "model", "reasoning", "run_on")


def migrate_chat_forks(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS chat_forks (project_id TEXT NOT NULL, chat_id TEXT NOT NULL, "
        "record_json TEXT NOT NULL, PRIMARY KEY (project_id, chat_id))"
    )


class ChatForkStoreMixin(StoreMixinBase):
    @staticmethod
    def _latest_chat_turn_id(
        connection: sqlite3.Connection, project_id: str, chat_id: str
    ) -> str | None:
        latest = connection.execute(
            "SELECT operation_id FROM graph_runs WHERE project_id = ? "
            "AND kind IN ('node_chat', 'project_chat') "
            "AND json_extract(request_json, '$.chat_id') = ? "
            "AND (json_extract(request_json, '$.artifact_edit') IS NULL "
            "OR json_extract(request_json, '$.artifact_edit.origin_bound') = 0) "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (project_id, chat_id),
        ).fetchone()
        return latest[0] if latest else None

    def _chat_fork_source(
        self, connection: sqlite3.Connection, project_id: str, source_chat_id: str
    ) -> sqlite3.Row:
        """The chat's latest turn, which must hold a settled, usable native session."""
        return cast("AppStore", self)._usable_chat_fork_source(
            connection, self._latest_chat_turn_id(connection, project_id, source_chat_id)
        )

    def _chat_fork_record(
        self, connection: sqlite3.Connection, project_id: str, chat_id: str
    ) -> ChatForkRecord | None:
        row = connection.execute(
            "SELECT record_json FROM chat_forks WHERE project_id = ? AND chat_id = ?",
            (project_id, chat_id),
        ).fetchone()
        return None if row is None else ChatForkRecord.model_validate_json(row[0])

    def _chat_fork_refusal(
        self, connection: sqlite3.Connection, fork: ChatForkRecord, carried: dict[str, object]
    ) -> str | None:
        """Why a later attempt may not fork the source its first message recorded.

        Forking the source's live session again would import turns the source
        admitted after the branched chat's first message, so any newer source
        turn, or a source session that is gone, refuses the fork.
        """

        project_id, source_chat_id = fork.project_id, fork.origin.source_chat_id
        if self._latest_chat_turn_id(connection, project_id, source_chat_id) != carried.get(
            "fork_source_operation_id"
        ):
            return "chat_fork_source_moved"
        try:
            self._chat_fork_source(connection, project_id, source_chat_id)
        except ValueError:
            return "chat_fork_source_unavailable"
        return None

    @staticmethod
    def detach_chat_forks_for_restore(connection: sqlite3.Connection) -> None:
        """A restored branched chat that has not forked yet starts a fresh session."""
        connection.execute(
            "UPDATE chat_forks SET record_json = json_set(record_json, '$.detached', json('true'))"
        )

    def require_chat_fork_source(self, project_id: str, source_chat_id: str) -> None:
        """Refuse a branch before anything, such as a new graph branch, is created for it."""
        with self.connection() as connection:
            self._chat_fork_source(connection, project_id, source_chat_id)

    def _pending_chat_fork(
        self, connection: sqlite3.Connection, project_id: str, chat_id: str
    ) -> tuple[ChatForkRecord, sqlite3.Row] | None:
        """A branched chat's origin and the source turn its first turn forks.

        The fork takes the source chat's latest state when the first message is
        sent; nothing reads the source again after that turn is admitted.
        """
        fork = self._chat_fork_record(connection, project_id, chat_id)
        if fork is None or fork.detached:
            return None
        return fork, self._chat_fork_source(connection, project_id, fork.origin.source_chat_id)

    def pending_chat_fork_profile(self, project_id: str, chat_id: str) -> dict[str, object] | None:
        """The launch profile and scope a branched chat's first turn must use."""
        with self.connection() as connection:
            if connection.execute(
                "SELECT 1 FROM graph_runs WHERE project_id = ? "
                "AND json_extract(request_json, '$.chat_id') = ? LIMIT 1",
                (project_id, chat_id),
            ).fetchone():
                return None
            pending = self._pending_chat_fork(connection, project_id, chat_id)
        if pending is None:
            return None
        fork, source = pending
        request = json.loads(source["request_json"])
        return {
            **{field: request.get(field) for field in FORK_PROFILE_FIELDS},
            "node_id": fork.node_id,
            "chat_scope": "node" if fork.kind == "node_chat" else "project",
        }

    def create_chat_fork(
        self,
        project_id: str,
        source_chat_id: str,
        graph_target: GraphTargetRef,
        request_id: str,
        *,
        target_node_ids: set[str] | None = None,
    ) -> ChatForkRecord:
        """Capture the latest settled turn without launching or changing its session.

        The request id names the new chat, so a replayed request returns that chat.
        """
        chat_id = str(uuid.uuid5(uuid.UUID(source_chat_id), request_id))
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute(
                "SELECT record_json FROM chat_forks WHERE project_id = ? AND chat_id = ?",
                (project_id, chat_id),
            ).fetchone()
            if replay is not None:
                record = ChatForkRecord.model_validate_json(replay[0])
                if record.graph_target != graph_target:
                    raise ValueError("This branch request already chose another graph target.")
                return record
            source = self._chat_fork_source(connection, project_id, source_chat_id)
            request = json.loads(source["request_json"])
            source_target = GraphTargetRef.model_validate_json(source["graph_target_json"])
            if graph_target.kind == "branch":
                branch = connection.execute(
                    "SELECT archived FROM graph_branches WHERE project_id = ? AND branch_id = ?",
                    (project_id, graph_target.branch_id),
                ).fetchone()
                if branch is None or branch["archived"]:
                    raise ValueError("Choose an existing, non-archived graph branch.")
            title_row = connection.execute(
                "SELECT title FROM chat_display WHERE project_id = ? AND chat_id = ?",
                (project_id, source_chat_id),
            ).fetchone()
            title = title_row[0] if title_row and title_row[0] else "Chat"
            node_id = request.get("node_id")
            kind = source["kind"]
            if (
                node_id is not None
                and target_node_ids is not None
                and node_id not in target_node_ids
            ):
                kind, node_id = "project_chat", None
            record = ChatForkRecord(
                project_id=project_id,
                chat_id=chat_id,
                graph_target=graph_target,
                kind=kind,
                node_id=node_id,
                created_at=self.now(),
                origin=ChatForkOrigin(
                    source_chat_id=source_chat_id,
                    source_operation_id=source["operation_id"],
                    source_graph_target=source_target,
                    source_title=title,
                    provider=request["provider"],
                    model=request.get("model"),
                    reasoning=request.get("reasoning"),
                    run_on=request["run_on"],
                ),
            )
            connection.execute(
                "INSERT INTO chat_forks VALUES (?, ?, ?)",
                (project_id, record.chat_id, record.model_dump_json()),
            )
            initialize_chat_name(connection, project_id, record.chat_id, title)
        return record
