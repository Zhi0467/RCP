"""Transactional display invalidation owned by the operational tables.

Scopes are project ids, or the empty string for space-wide data. Entity keys
are empty for inventories, raw ids for details, and ``member:<id>`` for a
member's display preferences. Triggers also cover deletion and retention.
"""

from __future__ import annotations

import sqlite3
import uuid

from rcp.storage.resource_deadlines import migrate_resource_deadlines

# Explicit owners, rather than a naming convention that silently acquires new
# tables. Related entity tables are resolved through their retained parent.
# Migrations that rebuild an owner table or add a column touched only by their
# own writes must reinstall its triggers: UPDATE comparisons capture the schema
# at installation time.
_PROJECT_TABLES = {
    "agent_usage": ("usage",),
    "watchers": ("watchers", "episodes", "graph"),
    "compute_jobs": ("watchers", "episodes"),
    "graph_watcher_reconciliation": ("watchers",),
    "chat_display": ("chats",),
    "chat_browser_preferences": ("chats",),
    "chat_session_contexts": ("chats",),
    # The ref inventory marks branches with open questions.
    "questions": ("questions", "refs"),
    "notification_preferences": ("settings",),
    "consolidation_schedules": ("consolidation",),
    "consolidation_runs": ("consolidation",),
    "consolidation_apply_receipts": ("consolidation",),
    "digest_events": ("digest",),
    "digest_heads": ("digest",),
    "digest_marks": ("digest",),
    "operational_lessons": ("lessons",),
    "lesson_command_receipts": ("lessons",),
    "episodes": ("episodes", "graph", "refs", "watchers"),
    "auto_research_child_admissions": ("episodes",),
    "auto_research_child_experiments": ("episodes",),
    "auto_research_child_work": ("episodes",),
    "episode_isolations": ("episodes", "refs", "terminals"),
    # The digest reads branch archival to suppress archived attention.
    "episode_isolation_states": ("episodes", "refs", "graph", "digest", "terminals"),
    "branch_names": ("refs", "graph", "digest", "chats", "artifacts"),
    "branch_name_reservations": ("refs", "graph", "digest", "chats", "artifacts"),
    "conversation_worktrees": ("refs", "chats", "terminals"),
    "artifacts": ("artifacts", "tasks.status", "tasks.progress"),
    # migrate_episode_report_deletion adds a digest bump for removal tombstones.
    "artifact_imports": ("artifacts",),
    "artifact_revision_candidates": ("artifacts",),
    "compute_backend_probes": ("settings", "terminals"),
    "project_members": ("members",),
    "project_invitations": ("members",),
    "paper_drafts": ("paper",),
    "writing_sessions": ("paper",),
    "browser_owners": ("chats",),
    "client_requests": ("chats", "episodes"),
}
# migrate_episode_report_deletion reinstalls episode_reports with graph in every mode.
_EPISODE_TABLES = (
    "episode_archives",
    "episode_invocations",
    "episode_report_attempts",
    "episode_reports",
    "episode_wrapups",
    "experiment_episode_state",
    "auto_research_episodes",
    "auto_research_apply_results",
    "auto_research_command_files",
    "auto_research_finish_receipts",
    "auto_research_inbox_receipts",
    "auto_research_invocations",
    "auto_research_lifecycle_notices",
    "auto_research_messages",
    "auto_research_recoveries",
)
_TASK_TABLES = (
    "graph_run_events",
    "graph_run_receipts",
    "graph_run_outputs",
    "graph_run_contracts",
    "browser_turn_status",
)
_GLOBAL_TABLES = {
    "notification_devices": ("settings", "members"),
    "provider_login_states": ("settings",),
    "provider_readiness_snapshots": ("settings",),
    "provider_skill_inventories": ("settings",),
    "space_machines": ("settings", "members", "terminals"),
    "space_identity": ("members",),
    "space_users": ("members", "watchers"),
    "team_invitations": ("members",),
}


def _bump(scope: str, resource: str, entity: str = "''", predicate: str = "1") -> str:
    return (
        "INSERT INTO resource_versions(scope,resource,entity,generation) "
        f"SELECT {scope},'{resource}',COALESCE({entity},''),1 "
        f"WHERE ({scope}) IS NOT NULL AND ({predicate}) "
        "ON CONFLICT(scope,resource,entity) DO UPDATE SET generation=generation+1;"
    )


def _install(
    connection: sqlite3.Connection,
    table: str,
    statements: list[str],
    *,
    columns: tuple[str, ...] | None = None,
    suffix: str = "",
) -> None:
    """Use OLD before delete so parent resolution survives cascading deletion."""
    compared = columns or tuple(
        row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
    )
    changed = " OR ".join(f'OLD."{name}" IS NOT NEW."{name}"' for name in compared)
    for event, row, timing in (
        ("INSERT", "NEW", "AFTER"),
        ("UPDATE", "NEW", "AFTER"),
        ("DELETE", "OLD", "BEFORE"),
    ):
        body = " ".join(statement.replace("ROW.", f"{row}.") for statement in statements)
        if event == "UPDATE":
            # Invalidating the old scope too makes reassignment safe.
            body += " " + " ".join(statement.replace("ROW.", "OLD.") for statement in statements)
        condition = f"WHEN {changed}" if event == "UPDATE" else ""
        connection.execute(
            f"CREATE TRIGGER resource_{table}{suffix}_{event.lower()} {timing} {event} "
            f'ON "{table}" {condition} BEGIN {body} END'
        )


# Migrations 49 and 54 retain their original same-ID branch/chat SQL.
_TASK_CHAT = (
    "CASE WHEN ROW.kind='auto_research' AND json_extract(ROW.request_json,'$.role')='orchestrator' "
    "THEN json_extract(ROW.graph_target_json,'$.branch_id') "
    "ELSE json_extract(ROW.request_json,'$.chat_id') END"
)
_BRANCH_TASK_CHAT = (
    "CASE WHEN ROW.kind='auto_research' AND json_extract(ROW.request_json,'$.role')='orchestrator' "
    "THEN (SELECT owner_episode_id FROM graph_branches WHERE project_id=ROW.project_id "
    "AND branch_id=json_extract(ROW.graph_target_json,'$.branch_id')) "
    "ELSE json_extract(ROW.request_json,'$.chat_id') END"
)


def _install_task_status(connection: sqlite3.Connection, chat: str = _TASK_CHAT) -> None:
    _install(
        connection,
        "graph_runs",
        [
            _bump("ROW.project_id", "tasks.status", predicate="ROW.visible=1"),
            _bump(
                "ROW.project_id",
                "artifacts",
                predicate="json_extract(ROW.request_json,'$.artifact_edit') IS NOT NULL",
            ),
            _bump("ROW.project_id", "refs", predicate="ROW.visible=1"),
            _bump(
                "ROW.project_id",
                "graph",
                predicate="ROW.visible=1 AND (json_extract(ROW.request_json,'$.patch_kind')='experiment_loop' "
                "OR EXISTS(SELECT 1 FROM episodes WHERE episode_id=ROW.episode_id AND mode='experiment_loop'))",
            ),
            _bump("ROW.project_id", "terminals"),
            _bump("ROW.project_id", "episodes", predicate="ROW.episode_id IS NOT NULL"),
            _bump(
                "ROW.project_id",
                "chats",
                predicate=f"({chat}) IS NOT NULL",
            ),
        ],
        columns=("project_id", "operation_id", "status", "finished_at", "visible", "history_only"),
        suffix="_status",
    )


def _install_task_progress(connection: sqlite3.Connection, chat: str = _TASK_CHAT) -> None:
    """Any change to a task row, in any column it has now, moves its progress views."""
    _install(
        connection,
        "graph_runs",
        [
            _bump("ROW.project_id", "tasks.progress", predicate="ROW.visible=1"),
            _bump("ROW.project_id", "task", "ROW.operation_id"),
            _bump("ROW.project_id", "episode", "ROW.episode_id", "ROW.episode_id IS NOT NULL"),
            _bump("ROW.project_id", "chat", chat, f"({chat}) IS NOT NULL"),
        ],
    )


def migrate_agent_live_status(connection: sqlite3.Connection) -> None:
    """A task's display-only live status; its progress triggers must compare the new column."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(graph_runs)")}
    # A pre-ledger table rebuilt from the current schema template already has it.
    if "agent_status_json" not in columns:
        connection.execute("ALTER TABLE graph_runs ADD COLUMN agent_status_json TEXT")
    for event in ("insert", "update", "delete"):
        connection.execute(f"DROP TRIGGER resource_graph_runs_{event}")
    _install_task_progress(connection)


def _human_mail_chat_bumps(
    scope: str,
    root: str = "(SELECT json_extract(graph_target_json,'$.branch_id') "
    "FROM episodes WHERE episode_id=ROW.episode_id)",
) -> list[str]:
    delivered = "ROW.sender_role='human' AND ROW.delivery_operation_id IS NOT NULL"
    return [_bump(scope, "chats", predicate=delivered), _bump(scope, "chat", root, delivered)]


def migrate_graph_branch_versions(connection: sqlite3.Connection) -> None:
    for table in ("graph_runs", "graph_runs_status", "auto_research_messages"):
        for event in ("insert", "update", "delete"):
            connection.execute(f"DROP TRIGGER resource_{table}_{event}")
    _install_task_status(connection, _BRANCH_TASK_CHAT)
    _install_task_progress(connection, _BRANCH_TASK_CHAT)
    scope = "(SELECT project_id FROM episodes WHERE episode_id=ROW.episode_id)"
    _install(
        connection,
        "auto_research_messages",
        [
            _bump(scope, "episodes"),
            _bump(scope, "episode", "ROW.episode_id"),
            *_human_mail_chat_bumps(
                scope,
                "(SELECT b.owner_episode_id FROM graph_branches b JOIN episodes e ON e.project_id=b.project_id "
                "AND b.branch_id=json_extract(e.graph_target_json,'$.branch_id') WHERE e.episode_id=ROW.episode_id)",
            ),
        ],
    )
    _install(
        connection,
        "graph_branches",
        [
            _bump("ROW.project_id", resource)
            for resource in ("episodes", "refs", "graph", "digest", "chats", "artifacts")
        ],
    )


def migrate_resource_versions(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE resource_versions(scope TEXT NOT NULL,resource TEXT NOT NULL,"
        "entity TEXT NOT NULL DEFAULT '',generation INTEGER NOT NULL,"
        "PRIMARY KEY(scope,resource,entity)) WITHOUT ROWID"
    )
    connection.execute(
        "CREATE TABLE resource_watermark_epoch(singleton INTEGER PRIMARY KEY CHECK(singleton=1), epoch TEXT NOT NULL)"
    )
    connection.execute("INSERT INTO resource_watermark_epoch VALUES(1,?)", (str(uuid.uuid4()),))
    for table, resources in _PROJECT_TABLES.items():
        statements = [_bump("ROW.project_id", resource) for resource in resources]
        columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
        if "chat_id" in columns:
            statements.append(
                _bump("ROW.project_id", "chat", "ROW.chat_id", "ROW.chat_id IS NOT NULL")
            )
        if "episode_id" in columns:
            statements.append(
                _bump("ROW.project_id", "episode", "ROW.episode_id", "ROW.episode_id IS NOT NULL")
            )
        for episode_column in ("owner_episode_id", "auto_research_episode_id", "child_episode_id"):
            if episode_column in columns:
                statements.append(_bump("ROW.project_id", "episode", f"ROW.{episode_column}"))
        if table == "questions":
            statements.extend(
                [
                    _bump("ROW.project_id", "episode", "ROW.owner_id", "ROW.owner_kind='episode'"),
                    _bump("ROW.project_id", "chat", "ROW.owner_id", "ROW.owner_kind='chat'"),
                ]
            )
        if table == "agent_usage":
            statements.append(_bump("ROW.project_id", "task", "ROW.operation_id"))
        if table == "artifacts":
            task = "json_extract(ROW.metadata,'$.origin_operation_id')"
            statements.append(_bump("ROW.project_id", "task", task, f"{task} IS NOT NULL"))
        _install(connection, table, statements)
    for table in ("chat_reads", "chat_pins"):
        statements = [_bump("ROW.project_id", "chats", "'member:' || ROW.user_id")]
        if table == "chat_reads":
            statements.append(_bump("ROW.project_id", "digest"))
        _install(connection, table, statements)
    for table, resources in _GLOBAL_TABLES.items():
        _install(connection, table, [_bump("''", resource) for resource in resources])
    # Graph completion embeds Experiment episode details, including archive and
    # wrapup health. Allocation and Auto-research ledgers are not graph inputs.
    for table in _EPISODE_TABLES:
        scope = "(SELECT project_id FROM episodes WHERE episode_id=ROW.episode_id)"
        _install(
            connection,
            table,
            [
                _bump(scope, "episodes"),
                *(
                    [
                        _bump(
                            scope,
                            "graph",
                            predicate="EXISTS(SELECT 1 FROM episodes "
                            "WHERE episode_id=ROW.episode_id AND mode='experiment_loop')",
                        )
                    ]
                    if table
                    in {
                        "episode_archives",
                        "episode_reports",
                        "episode_wrapups",
                        "experiment_episode_state",
                    }
                    else []
                ),
                _bump(scope, "episode", "ROW.episode_id"),
                *(_human_mail_chat_bumps(scope) if table == "auto_research_messages" else []),
            ],
        )
    for table, entity in (
        ("auto_research_experiment_invocations", "auto_research_episode_id"),
        ("auto_research_child_work_attempts", "worker_id"),
    ):
        scope = (
            f"(SELECT project_id FROM episodes WHERE episode_id=ROW.{entity})"
            if entity != "worker_id"
            else "(SELECT project_id FROM auto_research_child_work WHERE worker_id=ROW.worker_id)"
        )
        episode = (
            f"ROW.{entity}"
            if entity != "worker_id"
            else "(SELECT episode_id FROM auto_research_child_work WHERE worker_id=ROW.worker_id)"
        )
        _install(connection, table, [_bump(scope, "episodes"), _bump(scope, "episode", episode)])
    for table in ("artifact_versions", "artifact_operations"):
        _install(
            connection,
            table,
            [
                _bump(
                    "(SELECT project_id FROM artifacts WHERE artifact_id=ROW.artifact_id)",
                    "artifacts",
                )
            ],
        )
    for table in _TASK_TABLES:
        scope = "(SELECT project_id FROM graph_runs WHERE operation_id=ROW.operation_id)"
        visible = (
            "EXISTS(SELECT 1 FROM graph_runs WHERE operation_id=ROW.operation_id AND visible=1)"
        )
        episode = "(SELECT episode_id FROM graph_runs WHERE operation_id=ROW.operation_id)"
        chat = "(SELECT json_extract(request_json,'$.chat_id') FROM graph_runs WHERE operation_id=ROW.operation_id)"
        _install(
            connection,
            table,
            [
                _bump(scope, "task", "ROW.operation_id"),
                # Terminals show running work from launch receipts only, the ones
                # naming repository roots; progress receipts must not move them.
                *(
                    [
                        _bump(
                            scope,
                            "terminals",
                            predicate="CASE WHEN json_valid(ROW.payload_json) THEN "
                            "json_extract(ROW.payload_json,'$.canonical_repository_roots') "
                            "END IS NOT NULL",
                        )
                    ]
                    if table == "graph_run_receipts"
                    else []
                ),
                _bump(scope, "tasks.progress", predicate=visible),
                _bump(scope, "episode", episode, f"{episode} IS NOT NULL"),
                _bump(scope, "chat", chat, f"{chat} IS NOT NULL"),
            ],
        )
    _install_task_status(connection)
    _install_task_progress(connection)
    # Project metadata used by settings changes independently of canonical files.
    _install(
        connection,
        "projects",
        [_bump("ROW.project_id", "settings")],
        columns=("name", "state_location", "state_remote", "locator"),
    )
    migrate_resource_deadlines(connection)


def migrate_episode_report_deletion(connection: sqlite3.Connection) -> None:
    """A deleted report keeps its lineage row; every view of it must refresh."""
    connection.execute("ALTER TABLE episode_reports ADD COLUMN deleted_at TEXT")
    for table in ("episode_reports", "artifact_imports"):
        for event in ("insert", "update", "delete"):
            connection.execute(f"DROP TRIGGER resource_{table}_{event}")
    scope = "(SELECT project_id FROM episodes WHERE episode_id=ROW.episode_id)"
    # History subscribes to graph in every mode, not only Experiment.
    _install(
        connection,
        "episode_reports",
        [
            _bump(scope, "episodes"),
            _bump(scope, "graph"),
            _bump(scope, "episode", "ROW.episode_id"),
        ],
    )
    # The digest drops a report whose artifact a member deleted.
    _install(
        connection,
        "artifact_imports",
        [
            _bump("ROW.project_id", "artifacts"),
            _bump("ROW.project_id", "digest", predicate="ROW.state='deleted'"),
        ],
    )
    # A publishing report holds Delete; the wrap-up settling releases it.
    _install(
        connection,
        "episodes",
        [_bump("ROW.project_id", "artifacts")],
        columns=("wrapup_state",),
        suffix="_wrapup",
    )


def install_bulletin_resource_versions(connection: sqlite3.Connection) -> None:
    """Posts and their live roster refresh through the existing project polling loop."""
    roster_columns = {
        "chat_display": ("project_id", "chat_id", "title", "archived_at"),
        "bulletin_posts": (
            "project_id",
            "target_key",
            "operation_id",
            "created_at",
            "text",
            "mentions_json",
            "refs_json",
            "edited_at",
            "deleted_at",
        ),
        "graph_branches": ("project_id", "branch_id", "name", "archived"),
        "project_members": ("project_id", "user_id"),
        "questions": ("project_id", "origin_json", "state", "withdrawn_readonly"),
        "graph_runs": (
            "project_id",
            "episode_id",
            "kind",
            "graph_target_json",
            "request_json",
            "status",
            "visible",
            "created_at",
        ),
        "episodes": (
            "project_id",
            "episode_id",
            "mode",
            "control_node_id",
            "continues_episode_id",
            "status",
        ),
        "auto_research_child_work": (
            "project_id",
            "episode_id",
            "current_operation_id",
            "control_node_id",
            "instruction",
            "stop_requested_at",
        ),
        "watchers": (
            "project_id",
            "episode_id",
            "worker_id",
            "chat_id",
            "graph_target_json",
            "status",
            "notified",
            "notification_operation_id",
        ),
    }
    for table, columns in roster_columns.items():
        _install(
            connection,
            table,
            [_bump("ROW.project_id", "bulletin")],
            columns=columns,
            # Posts own their table's triggers; the roster inputs share other owners' tables.
            suffix="" if table == "bulletin_posts" else "_bulletin",
        )

    _install(
        connection,
        "space_users",
        [
            "INSERT INTO resource_versions(scope,resource,entity,generation) SELECT project_id,'bulletin','',1 FROM project_members WHERE user_id=ROW.user_id ON CONFLICT(scope,resource,entity) DO UPDATE SET generation=generation+1;"
        ],
        columns=("user_id", "display_name", "removal_started_at"),
        suffix="_bulletin",
    )


def migrate_chat_name_versions(connection: sqlite3.Connection) -> None:
    """Reinstall the chat owner after adding persistent self-name state."""
    for event in ("insert", "update", "delete"):
        connection.execute(f"DROP TRIGGER resource_chat_display_{event}")
    _install(
        connection,
        "chat_display",
        [
            _bump("ROW.project_id", "chats"),
            _bump("ROW.project_id", "chat", "ROW.chat_id", "ROW.chat_id IS NOT NULL"),
        ],
    )
