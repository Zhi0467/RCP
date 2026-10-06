from __future__ import annotations

import sqlite3

from rcp.storage import AppStore


def downgrade_artifacts(
    connection: sqlite3.Connection, *, report_html: dict[str, str] | None = None
) -> None:
    """Restore pre-artifact storage before a test replays older migrations."""
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
        connection.execute(
            "DELETE FROM storage_schema_migrations WHERE migration_name IN "
            "('machine_hidden_folders_v1', 'machine_provider_autocompact_v1')"
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
    connection.execute(
        "DELETE FROM storage_schema_migrations WHERE migration_version IN (30, 31, 32)"
    )
