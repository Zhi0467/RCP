"""Upgrade a pre-thread writer without rewriting its durable question data."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from rcp.core.transition_models import GraphTargetRef
from rcp.storage import AppStore, QuestionOrigin
from rcp.storage.base import AppStoreBase
from rcp.transfer.archive import TRANSFER_GLOBAL_TABLES
from rcp.transfer.records import TRANSFER_EXCLUDED_PROJECT_TABLES


def test_pre_thread_database_upgrades_without_rewriting_old_tables(tmp_path: Path, monkeypatch):
    path = tmp_path / "old.sqlite3"
    migrations = AppStoreBase._STORAGE_SCHEMA_MIGRATIONS
    run_migration = AppStoreBase._run_storage_schema_migration

    def old_migration(
        self: AppStoreBase,
        connection: sqlite3.Connection,
        *,
        version: int,
        name: str,
        migration: Callable[[sqlite3.Connection], None],
    ) -> None:
        if version < 80:
            run_migration(self, connection, version=version, name=name, migration=migration)

    with monkeypatch.context() as legacy:
        legacy.setattr(AppStore, "_baseline_storage_schema_cache", None)
        legacy.setattr(AppStore, "_baseline_storage_sql", ())
        legacy.setattr(AppStoreBase, "_run_storage_schema_migration", old_migration)
        legacy.setattr(
            AppStoreBase,
            "_STORAGE_SCHEMA_MIGRATIONS",
            tuple(item for item in migrations if item[0] < 80),
        )
        # The old writer's schema is generated without invoking new migration owners.
        old = AppStore(path)
        question = old.create_or_get_question(
            origin=QuestionOrigin(
                owner_kind="chat",
                project_id="project",
                owner_id="chat",
                operation_id="turn",
                provider="codex",
                native_session_id="session",
                stage_root="/stage",
                capability="discuss",
                graph_target=GraphTargetRef(kind="main"),
            ),
            key="old-question",
            question="Which experiment next?",
            choices=["A", "B"],
        )
        with old.connection() as connection:
            before = connection.execute("SELECT * FROM space_identity").fetchall()
        old.close()
    upgraded = AppStore(path)
    with upgraded.connection() as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"approval_cards", "agent_schedules", "agent_threads"} <= tables
        assert connection.execute("SELECT * FROM space_identity").fetchall() == before
        assert connection.execute("SELECT COUNT(*) FROM approval_cards").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM agent_schedules").fetchone()[0] == 0
    assert upgraded.get_question(question.question_id) == question
    upgraded.close()
    assert {"approval_cards", "agent_schedules"} <= TRANSFER_GLOBAL_TABLES
    assert "agent_threads" in TRANSFER_EXCLUDED_PROJECT_TABLES
