"""Indexed time boundaries maintained beside the records that own them."""

from __future__ import annotations

import sqlite3
import time

from rcp.limits import (
    AGENT_TASK_LIST_FINISHED_CHAT_SECONDS,
    ASK_WAITING_FRESH_SECONDS,
    HIDDEN_READ_READINESS_TTL_SECONDS,
)


def _install_deadline(
    connection: sqlite3.Connection,
    *,
    table: str,
    resource: str,
    owner: str,
    deadline: str,
    scope: str = "row.project_id",
    predicate: str = "1",
) -> None:
    """Backfill and maintain one deadline per owner/resource, including deletes."""

    def insert(alias: str, source: str = "") -> str:
        return (
            "INSERT OR REPLACE INTO resource_deadlines(scope,resource,entity,owner,deadline) "
            f"SELECT {scope}, '{resource}', '', '{table}:' || {owner}, {deadline} "
            f"{source} WHERE ({predicate}) AND ({deadline}) IS NOT NULL;"
        ).replace("row.", alias + ".")

    remove = (
        "DELETE FROM resource_deadlines "
        f"WHERE scope={scope} AND resource='{resource}' AND entity='' "
        f"AND owner='{table}:' || {owner};"
    ).replace("row.", "OLD.")
    key = f"resource_deadline_{table}_{resource.replace('.', '_')}"
    for event, body in (
        ("INSERT", insert("NEW")),
        ("UPDATE", remove + insert("NEW")),
        ("DELETE", remove),
    ):
        connection.execute(
            f"CREATE TRIGGER {key}_{event.lower()} AFTER {event} ON {table} BEGIN {body} END"
        )
    connection.execute(insert("row", f"FROM {table} AS row"))


def migrate_resource_deadlines(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE resource_deadlines (scope TEXT NOT NULL, resource TEXT NOT NULL, "
        "entity TEXT NOT NULL, owner TEXT NOT NULL, deadline REAL NOT NULL, "
        "PRIMARY KEY(scope,resource,entity,owner))"
    )
    connection.execute(
        "CREATE INDEX resource_deadlines_lookup "
        "ON resource_deadlines(scope,resource,entity,deadline)"
    )
    # julianday keeps fractional seconds and accepts the same ISO timestamps as
    # the owners. Elapsed rows remain indexed until the owner changes/deletes.
    for resource in ("tasks.status", "tasks.progress"):
        _install_deadline(
            connection,
            table="graph_runs",
            resource=resource,
            owner="row.operation_id",
            deadline="(julianday(row.finished_at)-2440587.5)*86400.0 + "
            f"{AGENT_TASK_LIST_FINISHED_CHAT_SECONDS}",
            predicate="row.visible=1 AND row.history_only=0 AND "
            "(row.kind IN ('node_chat','project_chat') OR (row.kind='auto_research' "
            "AND json_extract(row.request_json,'$.role')='orchestrator'))",
        )
    _install_deadline(
        connection,
        table="artifacts",
        resource="artifacts",
        owner="row.artifact_id",
        deadline="(julianday(json_extract(row.metadata,'$.expires_at'))-2440587.5)*86400.0",
        predicate="json_extract(row.metadata,'$.kept_at') IS NULL",
    )
    _install_deadline(
        connection,
        table="consolidation_schedules",
        resource="consolidation",
        owner="row.project_id",
        deadline="(julianday(row.expires_at)-2440587.5)*86400.0",
    )
    _install_deadline(
        connection,
        table="provider_readiness_snapshots",
        resource="settings",
        owner="json_array(row.provider,row.host,row.binary)",
        scope="''",
        deadline="(julianday(row.probed_at)-2440587.5)*86400.0 + "
        f"{HIDDEN_READ_READINESS_TTL_SECONDS}",
    )
    connection.execute(
        "CREATE TRIGGER resource_deadline_questions_delete AFTER DELETE ON questions "
        "BEGIN DELETE FROM resource_deadlines WHERE scope=OLD.project_id "
        "AND resource='questions' AND entity='' AND owner='question_activity:' || OLD.question_id; END"
    )


def record_question_activity(
    connection: sqlite3.Connection, question_id: str, waiting: bool, *, now: float | None = None
) -> None:
    """Publish process-local liveness and its wall-clock boundary atomically."""
    row = connection.execute(
        "SELECT project_id FROM questions WHERE question_id=?", (question_id,)
    ).fetchone()
    if row is None:
        return
    project_id = row[0]
    connection.execute(
        "INSERT INTO resource_versions(scope,resource,entity,generation) VALUES(?,'questions','',1) "
        "ON CONFLICT(scope,resource,entity) DO UPDATE SET generation=generation+1",
        (project_id,),
    )
    if waiting:
        connection.execute(
            "INSERT OR REPLACE INTO resource_deadlines(scope,resource,entity,owner,deadline) "
            "VALUES(?,'questions','',?,?)",
            (
                project_id,
                f"question_activity:{question_id}",
                (time.time() if now is None else now) + ASK_WAITING_FRESH_SECONDS,
            ),
        )
    else:
        connection.execute(
            "DELETE FROM resource_deadlines WHERE scope=? AND resource='questions' "
            "AND entity='' AND owner=?",
            (project_id, f"question_activity:{question_id}"),
        )
