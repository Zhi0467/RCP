"""Immutable origins for human-created conversations that fork a native session."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict

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
    native_session_id: str
    source_cwd: str
    source_host: str
    runtime_id: str


def migrate_chat_forks(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS chat_forks (project_id TEXT NOT NULL, chat_id TEXT NOT NULL, "
        "record_json TEXT NOT NULL, PRIMARY KEY (project_id, chat_id))"
    )


class ChatForkStoreMixin(StoreMixinBase):
    def chat_fork(self, project_id: str, chat_id: str) -> ChatForkRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT record_json FROM chat_forks WHERE project_id = ? AND chat_id = ?",
                (project_id, chat_id),
            ).fetchone()
        return ChatForkRecord.model_validate_json(row[0]) if row else None

    def create_chat_fork(
        self,
        project_id: str,
        source_chat_id: str,
        graph_target: GraphTargetRef,
        *,
        target_node_ids: set[str] | None = None,
    ) -> ChatForkRecord:
        """Capture the latest settled turn without launching or changing its session."""
        store = cast("AppStore", self)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source = connection.execute(
                "SELECT * FROM graph_runs WHERE project_id = ? "
                "AND kind IN ('node_chat', 'project_chat') "
                "AND json_extract(request_json, '$.chat_id') = ? "
                "AND (json_extract(request_json, '$.artifact_edit') IS NULL "
                "OR json_extract(request_json, '$.artifact_edit.origin_bound') = 0) "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (project_id, source_chat_id),
            ).fetchone()
            if source is None:
                raise ValueError("This chat has no native session to branch.")
            current = store._current_chat_session_row(
                connection, project_id, source["kind"], source_chat_id
            )
            if (
                current is None
                or current["operation_id"] != source["operation_id"]
                or current["status"] in {"queued", "running", "pausing", "paused"}
                or current["history_only"]
                or current["recovery_abandoned"]
                or store._dropped_chat_session_reason(current) is not None
                or not current["stage_root"]
            ):
                raise ValueError("The latest chat turn has no settled, usable native session.")
            request = json.loads(source["request_json"])
            if not request.get("provider") or not request.get("run_on"):
                raise ValueError("The source chat's provider and machine must be known.")
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
            layout = connection.execute(
                "SELECT 1 FROM graph_run_receipts AS receipt "
                "JOIN graph_runs AS run ON run.operation_id = receipt.operation_id "
                "WHERE run.project_id = ? AND json_extract(run.request_json, '$.chat_id') = ? "
                "AND receipt.category = 'chat_stage_layout' "
                "AND json_extract(receipt.payload_json, '$.stage_root') = ? "
                "AND json_extract(receipt.payload_json, '$.layout') = 'split-v1' LIMIT 1",
                (project_id, source_chat_id, source["stage_root"]),
            ).fetchone()
            cwd = PurePosixPath(source["stage_root"])
            if layout is not None:
                cwd /= "workspace"
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
                chat_id=str(uuid.uuid4()),
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
                native_session_id=source["native_session_id"],
                source_cwd=str(cwd),
                source_host=source["stage_host"] or "",
                runtime_id=source["runtime_id"],
            )
            connection.execute(
                "INSERT INTO chat_forks VALUES (?, ?, ?)",
                (project_id, record.chat_id, record.model_dump_json()),
            )
            initialize_chat_name(connection, project_id, record.chat_id, title)
        return record
