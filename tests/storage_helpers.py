from __future__ import annotations

import sqlite3

from rcp.storage import AppStore


def downgrade_resource_watermarks(connection: sqlite3.Connection) -> None:
    """Reconstruct older schema eras without retaining the later owner triggers.

    The ledger write comes first so a read-only store refuses before any DDL.
    """
    connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version>=49")
    for table in (
        "agent_threads",
        "agent_schedules",
        "approval_cards",
        "agent_handoffs",
        "agent_thread_queue",
        "paper_coach_steers",
        "space_turn_project_bindings",
        "memory_migrations",
        "memory_deletions",
    ):
        connection.execute(f'DROP TABLE IF EXISTS "{table}"')
    connection.execute("DROP TABLE IF EXISTS episode_merge_request_receipts")
    connection.execute("DROP TABLE IF EXISTS evidence_flag_carries")
    from rcp.server_ops.backup_paper import PAPER_PROJECT_TABLES, PAPER_SHARED_TABLES

    for table in (*reversed(PAPER_PROJECT_TABLES), *PAPER_SHARED_TABLES):
        connection.execute(f'DROP TABLE IF EXISTS "{table}"')
    triggers = connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='trigger' AND name LIKE 'resource_%'"
    ).fetchall()
    for (name,) in triggers:
        connection.execute(f'DROP TRIGGER "{name}"')
    if any(
        row[1] == "state_machine_id" for row in connection.execute("PRAGMA table_info(projects)")
    ):
        connection.execute("ALTER TABLE projects DROP COLUMN state_machine_id")
        connection.execute("ALTER TABLE projects DROP COLUMN machine_ids_pending")
        connection.execute("ALTER TABLE projects RENAME COLUMN state_path TO state_location")
        connection.execute(
            "ALTER TABLE projects ADD COLUMN state_remote INTEGER NOT NULL DEFAULT 0"
        )
    if any(
        row[1] == "origin_flag_id"
        for row in connection.execute("PRAGMA table_info(evidence_flags)")
    ):
        connection.execute("ALTER TABLE evidence_flags DROP COLUMN origin_flag_id")
    if any(row[1] == "login_id" for row in connection.execute("PRAGMA table_info(graph_runs)")):
        connection.execute("ALTER TABLE graph_runs DROP COLUMN login_id")
        connection.execute("DROP TABLE provider_login_assignments")
        connection.execute("DROP TABLE provider_login_settings")
        connection.execute("ALTER TABLE provider_login_states RENAME TO new_provider_login_states")
        AppStore._migrate_provider_login_states(connection)
        connection.execute(
            "INSERT INTO provider_login_states SELECT provider,COALESCE((SELECT host FROM space_machines m WHERE m.machine_id=n.machine_id),''),state,generation,detail,source,changed_at,changed_by FROM new_provider_login_states n WHERE login_id=''"
        )
        connection.execute("DROP TABLE new_provider_login_states")
        connection.execute("DROP TABLE provider_readiness_snapshots")
        AppStore._migrate_provider_readiness_snapshots(connection)
    if any(
        row[1] == "target" for row in connection.execute("PRAGMA table_info(consolidation_runs)")
    ):
        from rcp.storage.consolidation import migrate_consolidation

        columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(consolidation_runs)")
            if row[1] != "target"
        ]
        names = ",".join(columns)
        connection.execute(
            f"CREATE TEMP TABLE old_consolidation_runs AS SELECT {names} FROM consolidation_runs WHERE target='main'"
        )
        connection.execute("DROP TABLE consolidation_runs")
        migrate_consolidation(connection)
        connection.execute(
            f"INSERT INTO consolidation_runs ({names}) SELECT * FROM old_consolidation_runs"
        )
        connection.execute("DROP TABLE old_consolidation_runs")
        connection.execute("ALTER TABLE consolidation_schedules ADD COLUMN covered_head INTEGER")
        connection.execute(
            "UPDATE consolidation_schedules SET covered_head=(SELECT covered_head FROM consolidation_targets t WHERE t.project_id=consolidation_schedules.project_id AND t.target='main')"
        )
        connection.execute("DROP TABLE consolidation_targets")
        connection.execute("DROP INDEX evidence_flags_live")
        connection.execute("ALTER TABLE evidence_flags DROP COLUMN target")
        connection.execute(
            "CREATE UNIQUE INDEX evidence_flags_live ON evidence_flags(project_id,evidence_id,kind) WHERE state IN ('open','addressing')"
        )
    if any(row[1] == "self_named" for row in connection.execute("PRAGMA table_info(chat_display)")):
        connection.execute("ALTER TABLE chat_display DROP COLUMN self_named")
    connection.execute("DROP TABLE IF EXISTS research_view_pins")
    connection.execute("DROP TABLE IF EXISTS graph_branches")
    connection.execute("DROP TABLE IF EXISTS bulletin_posts")
    connection.execute("DROP TABLE IF EXISTS bulletin_command_receipts")
    if any(
        row[1] == "bulletin_enabled" for row in connection.execute("PRAGMA table_info(episodes)")
    ):
        connection.execute("ALTER TABLE episodes DROP COLUMN bulletin_enabled")
    connection.execute("DROP INDEX IF EXISTS projects_group")
    connection.execute("DROP TABLE IF EXISTS project_groups")
    for table in ("projects", "project_provisioning_requests"):
        if any(row[1] == "group_id" for row in connection.execute(f"PRAGMA table_info({table})")):
            connection.execute(f"ALTER TABLE {table} DROP COLUMN group_id")
    if any(
        row[1] == "deleted_at" for row in connection.execute("PRAGMA table_info(episode_reports)")
    ):
        connection.execute("ALTER TABLE episode_reports DROP COLUMN deleted_at")
    if any(
        row[1] == "agent_status_json" for row in connection.execute("PRAGMA table_info(graph_runs)")
    ):
        connection.execute("ALTER TABLE graph_runs DROP COLUMN agent_status_json")
    for table in ("resource_versions", "resource_deadlines", "resource_watermark_epoch"):
        connection.execute(f"DROP TABLE IF EXISTS {table}")


_MACHINE_IDENTITY_RENAMES = (
    ("chat_session_contexts", "machine_id", "execution_machine", False),
    ("writing_sessions", "machine_id", "execution_machine", False),
    ("compute_backend_probes", "machine_id", "execution_machine", False),
    ("compute_jobs", "machine_id", "execution_host", True),
    ("watchers", "machine_id", "execution_host", True),
    ("browser_owners", "machine_id", "execution_host", True),
    ("graph_runs", "stage_machine_id", "stage_host", True),
    ("artifact_revision_candidates", "stage_machine_id", "stage_host", True),
    ("episode_wrapups", "machine_id", "execution_host", True),
    ("episode_wrapups", "stage_machine_id", "stage_host", True),
    ("experiment_episode_state", "machine_id", "execution_host", True),
    ("experiment_episode_state", "stage_machine_id", "stage_host", True),
    ("provider_readiness_snapshots", "machine_id", "host", True),
    ("provider_skill_inventories", "machine_id", "host", True),
)
_MACHINE_IDENTITY_DROPS = (
    ("compute_jobs", "execution_machine TEXT NOT NULL DEFAULT ''"),
    ("episode_wrapups", "run_on TEXT"),
    ("experiment_episode_state", "execution_machine TEXT"),
)


def downgrade_machine_identity(connection: sqlite3.Connection) -> None:
    """Undo migration 77's column renames, naming each card's host again."""

    for table, new, old, card_keyed in _MACHINE_IDENTITY_RENAMES:
        if not any(row[1] == new for row in connection.execute(f"PRAGMA table_info({table})")):
            continue
        connection.execute(f'ALTER TABLE "{table}" RENAME COLUMN "{new}" TO "{old}"')
        if card_keyed:
            connection.execute(
                f'UPDATE "{table}" SET "{old}" = COALESCE('
                f'(SELECT host FROM space_machines m WHERE m.machine_id = "{table}"."{old}"), '
                f'"{old}")'
            )
    for table, column in _MACHINE_IDENTITY_DROPS:
        name = column.split()[0]
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if columns and name not in columns:
            connection.execute(f'ALTER TABLE "{table}" ADD COLUMN {column}')
    _restore_legacy_table_shapes(connection, {entry[0] for entry in _MACHINE_IDENTITY_RENAMES})


def _restore_legacy_table_shapes(connection: sqlite3.Connection, tables: set[str]) -> None:
    """Rebuild each table whose shape migration 5 checks to its legacy statement."""

    legacy = {
        row[1]: row[3] for row in AppStore._legacy_storage_schema_cache or () if row[0] == "table"
    }
    actual = {row[1]: row[3] for row in AppStore._storage_schema(connection) if row[0] == "table"}
    for table in sorted(tables):
        if (
            table not in legacy
            or table not in actual
            or actual[table] == legacy[table]
            or table in AppStore._SCHEMA_NORMALIZED_TABLES
        ):
            continue
        indexes = [
            str(row[0])
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? "
                "AND sql IS NOT NULL",
                (table,),
            )
        ]
        columns = {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}
        connection.execute(f'ALTER TABLE "{table}" RENAME TO "{table}__legacy_shape"')
        connection.execute(legacy[table])
        names = ", ".join(
            f'"{row[1]}"'
            for row in connection.execute(f'PRAGMA table_info("{table}")')
            if row[1] in columns
        )
        connection.execute(
            f'INSERT INTO "{table}" ({names}) SELECT {names} FROM "{table}__legacy_shape"'
        )
        connection.execute(f'DROP TABLE "{table}__legacy_shape"')
        for index in indexes:
            connection.execute(index)


def downgrade_artifacts(
    connection: sqlite3.Connection, *, report_html: dict[str, str] | None = None
) -> None:
    """Restore pre-artifact storage before a test replays older migrations.

    Migration 46 rebuilt the provisioning table; this restores its pre-46 shape.
    """
    downgrade_resource_watermarks(connection)
    downgrade_machine_identity(connection)
    if not any(
        row[1] == "artifact_id" for row in connection.execute("PRAGMA table_info(episode_reports)")
    ):
        return
    if any(
        row[1] == "hidden_folders_json"
        for row in connection.execute("PRAGMA table_info(space_machines)")
    ):
        connection.execute("ALTER TABLE space_machines DROP COLUMN hidden_folders_json")
        connection.execute("ALTER TABLE space_machines DROP COLUMN provider_autocompact_json")
        connection.execute("ALTER TABLE space_machines DROP COLUMN provider_shell_timeout_json")
        for column in (
            "job_manager",
            "jobs_root",
            "access_hint",
            "provider_paths_json",
            "legacy_settings_pending",
        ):
            connection.execute(f"ALTER TABLE space_machines DROP COLUMN {column}")
        connection.execute(
            "DELETE FROM storage_schema_migrations WHERE migration_name IN "
            "('machine_hidden_folders_v1', 'machine_provider_autocompact_v1', "
            "'machine_provider_shell_timeout_v1', 'machine_settings_v1')"
        )
    connection.execute("ALTER TABLE episodes DROP COLUMN browser_requested")
    for table in ("chat_browser_preferences", "browser_owners", "browser_turn_status"):
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    connection.execute(
        "DELETE FROM storage_schema_migrations WHERE migration_name = 'browser_grants_v1'"
    )
    report_html = report_html or {}
    assert {row[0] for row in connection.execute("SELECT report_id FROM episode_reports")} == set(
        report_html
    )
    connection.execute("ALTER TABLE episode_reports ADD COLUMN html TEXT NOT NULL DEFAULT ''")
    connection.executemany(
        "UPDATE episode_reports SET html = ? WHERE report_id = ?",
        [(html, report_id) for report_id, html in report_html.items()],
    )
    connection.execute("ALTER TABLE episode_reports DROP COLUMN artifact_id")
    connection.execute("ALTER TABLE episode_reports DROP COLUMN artifact_version_id")
    for table in ("artifacts", "artifact_versions", "artifact_operations", "artifact_imports"):
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    for row in AppStore._legacy_storage_schema_cache:
        if row[0] == "table" and row[1] == "result_views":
            connection.execute(row[3])
    connection.execute("DROP TABLE project_provisioning_requests")
    legacy = sorted(
        (
            row
            for row in AppStore._legacy_storage_schema_cache
            if row[2] == "project_provisioning_requests" and row[3]
        ),
        key=lambda row: row[0] != "table",
    )
    for row in legacy:
        connection.execute(row[3])
    connection.execute(
        "DELETE FROM storage_schema_migrations "
        "WHERE migration_name = 'repository_provisioning_contracts_v1'"
    )
    connection.execute(
        "DELETE FROM storage_schema_migrations WHERE migration_version IN (30, 31, 32)"
    )
