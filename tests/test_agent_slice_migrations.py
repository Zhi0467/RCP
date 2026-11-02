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
        if version < 69:
            run_migration(self, connection, version=version, name=name, migration=migration)

    with monkeypatch.context() as legacy:
        legacy.setattr(AppStore, "_baseline_storage_schema_cache", None)
        legacy.setattr(AppStore, "_baseline_storage_sql", ())
        legacy.setattr(AppStoreBase, "_run_storage_schema_migration", old_migration)
        legacy.setattr(
            AppStoreBase,
            "_STORAGE_SCHEMA_MIGRATIONS",
            tuple(item for item in migrations if item[0] < 69),
        )
        # The old writer's schema is generated without invoking new migration owners.
        old = AppStore(path)
        origin = QuestionOrigin(
            owner_kind="chat",
            project_id="project",
            owner_id="chat",
            operation_id="turn",
            provider="codex",
            native_session_id="session",
            stage_root="/stage",
            capability="discuss",
            graph_target=GraphTargetRef(kind="main"),
        )
        with old.connection() as connection:
            # Seed the old shape directly: current question creation joins threads.
            connection.execute(
                """INSERT INTO questions
                (question_id,project_id,owner_kind,owner_id,key,origin_json,argument_hash,
                 question,choices_json,multiple,state,created_at)
                VALUES ('old-question','project','chat','chat','old-key',?,'old-hash',
                        'Which experiment next?','["A","B"]',0,'pending',?)""",
                (origin.model_dump_json(), old.now()),
            )
            before = connection.execute("SELECT * FROM space_identity").fetchall()
        question = old.get_question("old-question")
        assert question is not None
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
