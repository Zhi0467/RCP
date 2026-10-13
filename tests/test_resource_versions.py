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
    ("chat_display", "chats", "title", {"chat_id": "chat"}),
    ("provider_login_states", "settings", "detail", {"state": "signed_in"}),
    ("chat_reads", "chats", "read_through", {"user_id": "member"}),
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
    if any(row[1] == "project_id" for row in connection.execute(f"PRAGMA table_info({table})")):
        fields["project_id"] = "p"
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
            ("changed",),
        )
        assert _generation(connection, resource) > inserted
        connection.execute("ROLLBACK TO mutation")
        assert _generation(connection, resource) == inserted
        connection.execute(f"DELETE FROM {table}")
        assert _generation(connection, resource) > inserted


@pytest.mark.parametrize("experiment", [False, True])
def test_task_progress_never_moves_status_and_entities_are_project_scoped(
    tmp_path: Path, experiment: bool
) -> None:
    store = AppStore(tmp_path / "store.db")
    with store.connection() as connection:
        _insert(
            connection,
            "graph_runs",
            {
                **_CASES[0][3],
                "episode_id": "episode",
                "request_json": json.dumps(
                    {"chat_id": "chat", "patch_kind": "experiment_loop" if experiment else "work"}
                ),
            },
            store.now(),
        )
    status = store.agent_task_status_signal("p")
    with store.connection() as connection:
        inventories = {
            name: _generation(connection, name)
            for name in ("tasks.status", "chats", "episodes", "terminals", "refs", "graph")
        }
        progress = _generation(connection, "tasks.progress")
        connection.execute(
            "UPDATE graph_runs SET status_message='checkpoint' WHERE operation_id='task'"
        )
        connection.execute(
            "INSERT INTO graph_run_events(operation_id,created_at,level,message) VALUES('task',?,'info','progress')",
            (store.now(),),
        )
        assert _generation(connection, "tasks.progress") > progress
        assert {name: _generation(connection, name) for name in inventories} == inventories
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
        for name in ("tasks.status", "chats", "episodes", "terminals", "refs"):
            assert _generation(connection, name) > inventories[name]
        assert (_generation(connection, "graph") > inventories["graph"]) == experiment
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


# Tables outside subscribed display resources. Provisioning belongs to setup;
# notifications shown in Settings/membership are registered by their owners.
_EXEMPT_TABLES = {
    "_legacy_campaign_invocations_archive": "legacy",
    "_legacy_campaign_messages_archive": "legacy",
    "_legacy_campaign_recoveries_archive": "legacy",
    "_legacy_campaign_reports_archive": "legacy",
    "_legacy_campaigns_archive": "legacy",
    "notification_episode_observations": "delivery",
    "notification_graph_markers": "delivery",
    "notification_outbox": "delivery",
    "notification_phone_pairings": "pairing",
    "notification_project_baselines": "delivery",
    "notification_question_events": "delivery",
    "notification_vapid_key": "credential",
    "notification_web_push_subscriptions": "transport",
    "owner_credentials": "credential",
    "owner_sign_in_codes": "credential",
    "project_aliases": "identity",
    "project_provisioning_requests": "setup",
    "project_provisioning_step_receipts": "setup",
    "project_transfer_activations": "transfer",
    "project_transfer_import_configurations": "transfer",
    "project_transfer_imports": "transfer",
    "project_transfer_proofs": "transfer",
    "project_transfer_requests": "transfer",
    "project_transfer_restore_reentries": "transfer",
    "project_transfer_uploads": "transfer",
    "resource_deadlines": "watermarks",
    "resource_versions": "watermarks",
    "resource_watermark_epoch": "watermarks",
    "sqlite_sequence": "sqlite",
    "storage_schema_migrations": "migration",
    "team_bootstrap_codes": "credential",
    "team_device_pairings": "pairing",
    "team_member_tokens": "credential",
    "team_sessions": "authentication",
}


def test_current_schema_classifies_every_table_and_installs_all_owner_triggers(tmp_path):
    from rcp.storage.resource_versions import (
        _EPISODE_TABLES,
        _GLOBAL_TABLES,
        _PROJECT_TABLES,
        _TASK_TABLES,
    )

    owners = (
        set(_PROJECT_TABLES)
        | set(_GLOBAL_TABLES)
        | set(_EPISODE_TABLES)
        | set(_TASK_TABLES)
        | {
            "graph_runs",
            "projects",
            "chat_reads",
            "chat_pins",
            "auto_research_experiment_invocations",
            "auto_research_child_work_attempts",
            "artifact_versions",
            "artifact_operations",
        }
    )
    store = AppStore(tmp_path / "store.db")
    with store.connection() as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not owners & _EXEMPT_TABLES.keys()
        assert tables == owners | _EXEMPT_TABLES.keys()
        triggers = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT name, tbl_name FROM sqlite_master WHERE type='trigger'"
            )
        }
        for table in owners:
            for event in ("insert", "update", "delete"):
                assert triggers[f"resource_{table}_{event}"] == table
        for event in ("insert", "update", "delete"):
            assert triggers[f"resource_graph_runs_status_{event}"] == "graph_runs"


@pytest.mark.parametrize(
    "table,fields,changes_graph,changes_refs",
    [
        # The ref inventory marks branches that have open questions.
        ("questions", {"owner_kind": "chat", "state": "pending", "multiple": 0}, False, True),
        ("episode_invocations", {"invocation_number": 1, "operation_id": "task"}, False, False),
        ("episode_reports", {"allocation_operation_id": "task"}, True, False),
        ("experiment_episode_state", {}, True, False),
    ],
)
def test_graph_invalidation_tracks_experiment_control_inputs(
    tmp_path, table, fields, changes_graph, changes_refs
):
    store = AppStore(tmp_path / "store.db")
    with store.connection() as connection:
        _insert(
            connection,
            "episodes",
            {"mode": "experiment_loop", "invocation_ceiling": 1},
            store.now(),
        )
        _insert(connection, "graph_runs", _CASES[0][3], store.now())
        if table == "episode_reports":
            _insert(
                connection,
                "episode_report_attempts",
                {
                    "attempt_number": 1,
                    "allocation_operation_id": "task",
                },
                store.now(),
            )
        graph, refs = _generation(connection, "graph"), _generation(connection, "refs")
        _insert(connection, table, fields, store.now())
        assert (_generation(connection, "graph") > graph) == changes_graph
        assert (_generation(connection, "refs") > refs) == changes_refs
