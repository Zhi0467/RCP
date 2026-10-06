from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from rcp.core.graph_targets import graph_target_json
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.storage import AppStore, WatcherContinuation, WatcherRecord

from .test_episode_storage import _episode, _operational_task


def _legacy_database(path: Path) -> AppStore:
    store = AppStore(path)
    episode = _episode(store, "live-main", ceiling=3)
    _, _, task = store.create_episode_with_invocation(
        episode, _operational_task(store, "live-task", episode_id=episode.episode_id)
    )
    store.create_watchers(
        [
            WatcherRecord(
                watcher_id="live-watcher",
                project_id="project",
                origin_operation_id=task.operation_id,
                origin_task_kind="node_chat",
                chat_id="node-chat",
                node_id="experiment-node",
                episode_id=episode.episode_id,
                execution_host="",
                check_command="true",
                log_path="/tmp/loop.log",
                cwd="/tmp",
                continuation=WatcherContinuation(provider="codex", run_on="local"),
                created_at=store.now(),
            )
        ]
    )
    store.create_episode(_episode(store, "parent", mode="auto_research"))
    with store.connection() as connection:
        connection.execute("DROP INDEX episodes_one_live_experiment_target")
        connection.execute(
            "CREATE UNIQUE INDEX episodes_one_live_experiment_control "
            "ON episodes(project_id, control_node_id) WHERE mode = 'experiment_loop' "
            "AND status IN ('queued', 'running', 'stopping', 'wrapping_up')"
        )
        connection.execute(
            "CREATE UNIQUE INDEX auto_research_pending_experiment_per_node "
            "ON auto_research_child_experiments(project_id, control_node_id) "
            "WHERE state = 'pending'"
        )
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version = 42")
        connection.execute(
            "UPDATE episodes SET stop_requested_at = ?, stop_initiated_by = 'legacy-actor' "
            "WHERE episode_id = 'live-main'",
            (store.now(),),
        )
        connection.execute(
            "INSERT INTO auto_research_child_experiments "
            "(child_episode_id, auto_research_episode_id, project_id, control_node_id, "
            "state, replaces_episode_id, request_json, parent_operation_id, created_at, updated_at) "
            "VALUES ('never-launched', 'parent', 'project', 'experiment-node', 'pending', "
            "'live-main', '{}', 'live-task', ?, ?)",
            (store.now(), store.now()),
        )
        connection.execute(
            "INSERT INTO graph_watcher_reconciliation VALUES ('project', 'main', ?, 2, NULL, ?)",
            ('{ "branch_id": null, "kind": "main" }', store.now()),
        )
        for table in ("episodes", "graph_runs", "watchers"):
            connection.execute(
                f"UPDATE {table} SET graph_target_json = ? "
                "WHERE json_extract(graph_target_json, '$.kind') = 'main'",
                ('{ "branch_id": null, "kind": "main" }',),
            )
    return store


def test_per_target_upgrade_rehearses_and_preserves_live_history(tmp_path: Path):
    path = tmp_path / "legacy.sqlite3"
    previous = _legacy_database(path)
    episode = previous.episode("live-main")
    task = previous.agent_task("live-task")
    watcher = previous.watcher("live-watcher")
    budget = previous.episode_budget_meter("live-main")
    previous.close()
    readonly = AppStore.open_read_only_snapshot(path)
    assert readonly.check_storage_schema_migrations() == (
        41,
        42,
        ("per_target_experiment_loops_v1",),
    )
    readonly.close()
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute("SELECT state FROM auto_research_child_experiments").fetchone()[0]
            == "pending"
        )
    upgraded = AppStore(path)
    assert upgraded.episode("live-main") == episode
    assert upgraded.agent_task("live-task") == task
    assert upgraded.watcher("live-watcher") == watcher
    assert upgraded.episode_budget_meter("live-main") == budget
    route = upgraded.auto_research_child_experiment("never-launched")
    assert route is not None and route.state == "cancelled"
    assert route.replaces_episode_id == "live-main" and route.terminal_diagnostic
    assert upgraded.episode("never-launched") is None
    with upgraded.connection() as connection:
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert "episodes_one_live_experiment_control" not in indexes
        assert "auto_research_pending_experiment_per_node" not in indexes
        for table in ("episodes", "graph_runs", "watchers", "graph_watcher_reconciliation"):
            for row in connection.execute(f"SELECT graph_target_json FROM {table}"):
                assert row[0] == graph_target_json(GraphTargetRef.model_validate_json(row[0]))
        fresh = AppStore(tmp_path / "fresh.sqlite3")
        with fresh.connection() as fresh_connection:
            assert upgraded._storage_schema(connection) == fresh._storage_schema(fresh_connection)
    target = GraphTargetRef(kind="branch", branch_id="parent")
    upgraded.create_episode(
        _episode(upgraded, "branch-loop").model_copy(
            update={"graph_target": target, "graph_base_head": GraphHeadRef(revision=0)}
        )
    )
    with pytest.raises(ValueError, match="live parent"):
        upgraded.create_episode(_episode(upgraded, "main-duplicate"))
    upgraded.close()
    reopened = AppStore(path)
    assert reopened.episode("branch-loop") is not None
    assert reopened.auto_research_child_experiment("never-launched") == route


def test_per_target_migration_failure_rolls_back_and_reopens(tmp_path: Path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    store = _legacy_database(path)
    store.close()
    migrate = AppStore._migrate_per_target_experiment_loops

    def fail_after_migration(self, connection):
        migrate(self, connection)
        raise RuntimeError("injected migration failure")

    with monkeypatch.context() as patch:
        patch.setattr(AppStore, "_migrate_per_target_experiment_loops", fail_after_migration)
        with pytest.raises(RuntimeError, match="injected migration failure"):
            AppStore(path)
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT MAX(migration_version) FROM storage_schema_migrations"
            ).fetchone()[0]
            == 41
        )
        assert (
            connection.execute("SELECT state FROM auto_research_child_experiments").fetchone()[0]
            == "pending"
        )
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'episodes_one_live_experiment_control'"
        ).fetchone()
        target = connection.execute(
            "SELECT graph_target_json FROM episodes WHERE episode_id = 'live-main'"
        ).fetchone()[0]
        assert target != graph_target_json(GraphTargetRef())
        assert json.loads(target) == GraphTargetRef().model_dump()
    reopened = AppStore(path)
    assert reopened.storage_schema_ledger_head() == 42
