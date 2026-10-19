"""Canonical conversation names, initialized once by task admission and migration."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from rcp.limits import AGENT_NAME_MAX_CHARS


class ChatNameError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def normalize_chat_name(name: str | None) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise ChatNameError("chat_name_empty")
    if len(name) > AGENT_NAME_MAX_CHARS:
        raise ChatNameError("chat_name_too_long")
    return name


def initialize_chat_name(
    connection: sqlite3.Connection, project_id: str, chat_id: str, message: str
) -> None:
    # A Markdown heading names the task better than the prose under it.
    heading = re.search(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", message, re.MULTILINE)
    text = heading.group(1) if heading else message
    name = " ".join(text.split())[:AGENT_NAME_MAX_CHARS].rstrip() or "Chat"
    connection.execute(
        "INSERT INTO chat_display (project_id, chat_id, title) VALUES (?, ?, ?) "
        "ON CONFLICT(project_id, chat_id) DO UPDATE SET title = excluded.title "
        "WHERE chat_display.title IS NULL",
        (project_id, chat_id, name),
    )


def initialize_task_chat_name(
    connection: sqlite3.Connection,
    project_id: str,
    kind: str,
    request: dict[str, Any],
    branch_id: str | None,
) -> None:
    if kind == "auto_research" and request.get("role") == "orchestrator":
        row = connection.execute(
            "SELECT owner_episode_id, name FROM graph_branches "
            "WHERE project_id = ? AND branch_id = ?",
            (project_id, branch_id),
        ).fetchone()
        if row is not None and row["owner_episode_id"]:
            initialize_chat_name(connection, project_id, row["owner_episode_id"], row["name"])
    elif kind in {"node_chat", "project_chat"} and request.get("chat_id"):
        message = request.get("message") or ""
        if not message.strip():
            message = request.get("node_id") or "Chat"
        # Only the nightly scheduler launches with this trigger; ordinary chats may
        # merely have the consolidation workflow enabled.
        if request.get("trigger") == "schedule":
            message = "Graph consolidation"
        initialize_chat_name(connection, project_id, request["chat_id"], message)


def backfill_chat_names(connection: sqlite3.Connection, project_id: str | None = None) -> None:
    # Names stored under the earlier, longer caps are cut to the one agent-name cap.
    connection.execute(
        "UPDATE chat_display SET title = rtrim(substr(title, 1, ?)) "
        "WHERE length(title) > ? AND (? IS NULL OR project_id = ?)",
        (AGENT_NAME_MAX_CHARS, AGENT_NAME_MAX_CHARS, project_id, project_id),
    )
    for row in connection.execute(
        "SELECT b.project_id, b.owner_episode_id, b.name FROM graph_branches b "
        "JOIN episodes e ON e.episode_id = b.owner_episode_id "
        "WHERE e.mode = 'auto_research' AND (? IS NULL OR b.project_id = ?)",
        (project_id, project_id),
    ).fetchall():
        initialize_chat_name(connection, row["project_id"], row["owner_episode_id"], row["name"])
    for row in connection.execute(
        "SELECT project_id, kind, request_json, graph_target_json FROM graph_runs "
        "WHERE kind IN ('node_chat', 'project_chat', 'auto_research') "
        "AND (? IS NULL OR project_id = ?) ORDER BY created_at, operation_id",
        (project_id, project_id),
    ).fetchall():
        initialize_task_chat_name(
            connection,
            row["project_id"],
            row["kind"],
            json.loads(row["request_json"]),
            json.loads(row["graph_target_json"]).get("branch_id"),
        )
