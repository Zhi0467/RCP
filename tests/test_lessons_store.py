from __future__ import annotations

import pytest

from rcp.storage import ProjectRecord

from .test_work_questions import work_execution


@pytest.fixture
def store(tmp_path):
    execution, human = work_execution(tmp_path)
    store = execution.store
    owner = store.local_owner
    assert owner is not None
    store.upsert_project(
        ProjectRecord(
            project_id="project",
            locator="local",
            name="Project",
            state_location="local",
            state_remote=False,
            added_at=store.now(),
        )
    )
    store.seat_project_member("project", owner.user_id)
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET authorized_user_id=? WHERE operation_id='turn'", (owner.user_id,)
        )
    return store


def consolidate(store):
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO consolidation_runs(run_id,project_id,occurrence_date,operation_id,"
            "authorization_id,authorized_by_json,input_head,created_at) "
            "VALUES ('run','project','2026-10-03','turn','authorization','{}',0,'now')"
        )


def test_legacy_lessons_are_read_only_migration_source(store):
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO operational_lessons VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "old",
                "project",
                "Preserved note",
                "before",
                "before",
                "agent",
                "old-turn",
                None,
                None,
                0,
            ),
        )
    assert store.list_lessons("project")[0]["text"] == "Preserved note"
    assert store.list_lessons("other") == []
    for removed in ("add_lesson", "update_lesson", "delete_lesson", "execute_lesson_command"):
        assert not hasattr(store, removed)


def test_legacy_lessons_and_receipts_survive_restore_and_alias_then_delete(store):
    from uuid import uuid4

    from rcp.transfer import TRANSFER_EXCLUDED_PROJECT_TABLES

    from .test_memory_files import old_lesson

    old_lesson(store, "retained")
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO lesson_command_receipts VALUES ('project','turn','add','key','digest','{}')"
        )
    original = store.list_lessons("project")
    tables = {"operational_lessons", "lesson_command_receipts"}
    assert tables <= TRANSFER_EXCLUDED_PROJECT_TABLES
    store.detach_restored_lifecycle(diagnostic="Restore test", confirmed_by="Human")
    assert store.list_lessons("project") == original
    canonical = str(uuid4())
    store.migrate_project_identity("project", canonical, str(uuid4()))
    assert store.list_lessons(canonical) == original
    with store.connection() as connection:
        for table in tables:
            assert connection.execute(f"SELECT project_id FROM {table}").fetchone()[0] == canonical
    counts = store.delete_project_records(canonical)
    for table in tables:
        assert counts[table] == 1
        with store.connection() as connection:
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
