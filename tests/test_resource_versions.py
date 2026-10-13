from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from rcp.storage import AppStore

# Real owner rows with only the fields their schema requires. Every mutation
# exercises the same commit/rollback contract, including deletion/pruning.
_CASES = [
    (
        "graph_runs",
        "tasks.status",
        "status",
        {
            "operation_id": "task",
            "kind": "project_chat",
            "status": "running",
            "request_json": '{"chat_id":"chat"}',
            "status_message": "",
        },
    ),
    ("agent_usage", "usage", "count_reason", {"operation_id": "task", "counted": 1}),
    ("watchers", "watchers", "status", {}),
    ("compute_jobs", "watchers", "status", {"containment": "cooperative", "status": "running"}),
    ("chat_display", "chats", "title", {"chat_id": "chat"}),
    (
        "questions",
        "questions",
        "question",
        {"owner_kind": "chat", "state": "pending", "multiple": 0},
    ),
    ("consolidation_schedules", "consolidation", "local_time", {}),
    ("digest_heads", "digest", "patch_id", {}),
    (
        "operational_lessons",
        "lessons",
        "text",
        {"author_kind": "agent", "operation_id": "task", "human_owned": 0},
    ),
    ("episodes", "episodes", "status", {"mode": "auto_research", "invocation_ceiling": 1}),
    ("branch_names", "refs", "name", {}),
    ("conversation_worktrees", "refs", "binding_json", {}),
    ("artifacts", "artifacts", "metadata", {"metadata": "{}"}),
    ("compute_backend_probes", "settings", "probe_json", {"route": "scheduler"}),
    ("project_members", "members", "seated_by", {}),
    ("paper_drafts", "paper", "content", {}),
    ("writing_sessions", "paper", "title", {}),
    ("provider_login_states", "settings", "detail", {"state": "signed_in"}),
    ("provider_skill_inventories", "settings", "diagnostic", {}),
    ("provider_readiness_snapshots", "settings", "version", {}),
    ("chat_reads", "chats", "read_through", {"user_id": "member"}),
    ("chat_pins", "chats", "pinned_at", {"user_id": "member"}),
]


def _insert(connection: sqlite3.Connection, table: str, overrides: dict, now: str) -> None:
    fields = {}
    for row in connection.execute(f"PRAGMA table_info({table})"):
        name, kind, required, default, primary = row[1:6]
        if (required or primary) and default is None:
            fields[name] = (
                0
                if kind == "INTEGER"
                else "{}"
                if name.endswith("json")
                else now
                if name.endswith("_at")
                else name
            )
    fields["project_id"] = "p"
    if table.startswith("provider_"):
        fields.pop("project_id")
    fields.update(overrides)
    connection.execute(
        f"INSERT INTO {table}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
        tuple(fields.values()),
    )


def _generation(connection: sqlite3.Connection, resource: str) -> int:
    return connection.execute(
        "SELECT COALESCE(SUM(generation),0) FROM resource_versions WHERE resource=?", (resource,)
    ).fetchone()[0]


@pytest.mark.parametrize(("table", "resource", "column", "fields"), _CASES)
def test_owner_writes_commit_atomically_and_pruning_invalidates(
    tmp_path: Path, table: str, resource: str, column: str, fields: dict
) -> None:
    store = AppStore(tmp_path / "store.db")
    with store.connection() as connection:
        if table == "agent_usage":
            _insert(connection, "graph_runs", _CASES[0][3], store.now())
        initial = _generation(connection, resource)
    with store.connection() as connection:
        _insert(connection, table, fields, store.now())
        assert _generation(connection, resource) > initial
        with sqlite3.connect(store.path) as reader:
            assert _generation(reader, resource) == initial
    with store.connection() as connection:
        inserted = _generation(connection, resource)
        connection.execute("SAVEPOINT mutation")
        connection.execute(
            f"UPDATE {table} SET {column}=?",
            (
                "exited"
                if table == "compute_jobs"
                else json.dumps({"changed": True})
                if column.endswith("json") or column == "metadata"
                else "changed",
            ),
        )
        assert _generation(connection, resource) > inserted
        connection.execute("ROLLBACK TO mutation")
        assert _generation(connection, resource) == inserted
        connection.execute(f"DELETE FROM {table}")
        assert _generation(connection, resource) > inserted


def test_task_progress_never_moves_status_and_entities_are_project_scoped(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "store.db")
    with store.connection() as connection:
        _insert(connection, "graph_runs", _CASES[0][3], store.now())
    status = store.agent_task_status_signal("p")
    with store.connection() as connection:
        progress = _generation(connection, "tasks.progress")
        connection.execute(
            "UPDATE graph_runs SET status_message='checkpoint' WHERE operation_id='task'"
        )
        connection.execute(
            "INSERT INTO graph_run_events(operation_id,created_at,level,message) VALUES('task',?,'info','progress')",
            (store.now(),),
        )
        assert _generation(connection, "tasks.progress") > progress
        assert connection.execute(
            "SELECT generation FROM resource_versions WHERE scope='p' AND resource='task' AND entity='task'"
        ).fetchone()
        assert (
            connection.execute(
                "SELECT generation FROM resource_versions WHERE scope='other' AND resource='task' AND entity='task'"
            ).fetchone()
            is None
        )
    assert store.agent_task_status_signal("p") == status
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET status='succeeded',finished_at=? WHERE operation_id='task'",
            (store.now(),),
        )
    assert store.agent_task_status_signal("p") != status


def test_watermark_epoch_is_seeded_preserved_on_reopen_and_rotated_by_restore(
    tmp_path: Path,
) -> None:
    path = tmp_path / "store.db"
    store = AppStore(path)
    with store.connection() as connection:
        original = connection.execute("SELECT epoch FROM resource_watermark_epoch").fetchone()[0]
    store.close()
    store = AppStore(path)
    with store.connection() as connection:
        assert (
            connection.execute("SELECT epoch FROM resource_watermark_epoch").fetchone()[0]
            == original
        )
    store.detach_restored_lifecycle(diagnostic="test restore", confirmed_by="test member")
    with store.connection() as connection:
        assert (
            connection.execute("SELECT epoch FROM resource_watermark_epoch").fetchone()[0]
            != original
        )


def test_watermark_migration_backfills_existing_owner_deadlines(tmp_path: Path) -> None:
    path = tmp_path / "store.db"
    store = AppStore(path)
    with store.connection() as connection:
        _insert(connection, "graph_runs", {**_CASES[0][3], "finished_at": store.now()}, store.now())
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'resource_%'"
        ).fetchall()
        for trigger in triggers:
            connection.execute(f'DROP TRIGGER "{trigger[0]}"')
        for table in ("resource_versions", "resource_deadlines", "resource_watermark_epoch"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version=48")
    store.close()
    store = AppStore(path)
    with store.connection() as connection:
        assert connection.execute("SELECT epoch FROM resource_watermark_epoch").fetchone()[0]
        assert connection.execute(
            "SELECT deadline FROM resource_deadlines WHERE scope='p' AND resource='tasks.status'"
        ).fetchone()[0]
        connection.execute("UPDATE graph_runs SET status='succeeded'")
        assert _generation(connection, "tasks.status") > 0
