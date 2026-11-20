"""Migration 93 folds per-check watcher rows into watchers with checks, once."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from rcp.storage import AppStore
from rcp.storage.resource_versions import _bump, _install
from tests.storage_helpers import OLD_WATCHERS_TABLE

_T0 = "2026-11-01T00:00:00+00:00"
_T1 = "2026-11-01T01:00:00+00:00"
_T2 = "2026-11-01T02:00:00+00:00"
_MAIN = '{"kind":"main","branch_id":null}'


def _old_row(watcher_id: str, **fields: object) -> dict[str, object]:
    return {
        "watcher_id": watcher_id,
        "project_id": "project",
        "origin_operation_id": "chat-turn",
        "origin_task_kind": "node_chat",
        "chat_id": "node-chat",
        "machine_id": "gpu",
        "check_command": "squeue -j 7",
        "log_path": "/tmp/job.log",
        "cwd": "/tmp",
        "continuation_json": '{"provider":"codex","run_on":"gpu"}',
        "status": "active",
        "created_at": _T0,
        **fields,
    }


_ROWS = [
    _old_row("lone", next_check_at=_T1, purpose="Training run"),
    _old_row("g1", group_id="grp", group_label="Sweep", status="completed", completed_at=_T1),
    _old_row("g2", group_id="grp", group_label="Sweep", status="completed", completed_at=_T2),
    _old_row(
        "p1",
        group_id="part",
        group_label="Pair",
        status="stopped",
        stopped_by="agent",
        stopped_at=_T1,
    ),
    _old_row("p2", group_id="part", group_label="Pair", consecutive_error_count=2),
    _old_row(
        "human",
        status="stopped",
        stopped_by="human",
        stop_reason="No longer needed.",
        stopped_at=_T1,
    ),
    _old_row(
        "claimed",
        status="completed",
        completed_at=_T1,
        notified=1,
        notification_operation_id="wake-turn",
    ),
    _old_row("acked", status="completed", completed_at=_T1, notified=1),
    _old_row(
        "graph",
        check_command="",
        log_path="",
        cwd="",
        graph_condition_json=json.dumps({"node_id": "exp/a", "status_in": ["completed"]}),
        armed_revision=4,
    ),
    _old_row(
        "orch",
        origin_operation_id="orch-turn",
        origin_task_kind="auto_research",
        chat_id="orch-turn",
        episode_id="ar-next",
    ),
]


def _legacy_store(path: Path) -> None:
    AppStore(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=OFF")
    with connection:
        connection.execute("DROP TABLE watcher_checks")
        connection.execute("DROP TABLE watchers")
        connection.execute(OLD_WATCHERS_TABLE)
        connection.execute("CREATE INDEX watchers_project ON watchers(project_id, created_at DESC)")
        _install(connection, "watchers", [_bump("ROW.project_id", "watchers")])
        for episode_id, parent, status in (
            ("ar-root", None, "completed"),
            ("ar-next", "ar-root", "running"),
        ):
            connection.execute(
                "INSERT INTO episodes (episode_id, project_id, mode, status, invocation_ceiling, "
                "continues_episode_id, created_at, updated_at) "
                "VALUES (?, 'project', 'auto_research', ?, 3, ?, ?, ?)",
                (episode_id, status, parent, _T0, _T0),
            )
        for operation_id, kind, episode_id in (
            ("chat-turn", "node_chat", None),
            ("orch-turn", "auto_research", "ar-next"),
        ):
            connection.execute(
                "INSERT INTO graph_runs (operation_id, project_id, episode_id, kind, status, "
                "request_json, created_at, updated_at, status_message) "
                "VALUES (?, 'project', ?, ?, 'succeeded', '{}', ?, ?, '')",
                (operation_id, episode_id, kind, _T0, _T0),
            )
        connection.execute(
            "INSERT INTO auto_research_invocations (episode_id, operation_id, "
            "allocation_operation_id, role, actor_operation_id, created_at) "
            "VALUES ('ar-next', 'orch-turn', 'orch-turn', 'orchestrator', 'orch-turn', ?)",
            (_T0,),
        )
        for row in _ROWS:
            connection.execute(
                f"INSERT INTO watchers ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                tuple(row.values()),
            )
        connection.execute("DELETE FROM storage_schema_migrations WHERE migration_version = 93")
    connection.close()


def test_old_watcher_rows_fold_into_watchers_with_checks_once(tmp_path: Path) -> None:
    path = tmp_path / "data"
    _legacy_store(path)

    for _ in range(2):
        store = AppStore(path)
        watchers = {watcher.watcher_id: watcher for watcher in store.project_watchers("project")}
        assert set(watchers) == {
            "lone",
            "grp",
            "part",
            "human",
            "claimed",
            "acked",
            "graph",
            "orch",
        }

        lone = watchers["lone"]
        assert lone.open and lone.state == "watching" and lone.purpose == "Training run"
        assert [check.check_id for check in lone.checks] == ["lone"]
        assert lone.checks[0].next_check_at == _T1
        assert lone.checks[0].machine_id == "gpu"

        group = watchers["grp"]
        assert [check.check_id for check in group.checks] == ["g1", "g2"]
        assert group.purpose == "Sweep" and group.ready

        part = watchers["part"]
        assert part.open and part.cancelled_at is None
        removed, live = part.checks
        assert removed.removed_at == _T1
        assert live.removed_at is None and live.consecutive_error_count == 2
        assert part.state == "check_failing"

        human = watchers["human"]
        assert (human.cancelled_by, human.cancel_reason, human.cancelled_at) == (
            "human",
            "No longer needed.",
            _T1,
        )
        assert human.state == "cancelled"

        claimed = watchers["claimed"]
        assert claimed.wake_turn_id == "wake-turn" and claimed.delivery_closed_at is None
        assert watchers["acked"].delivery_closed_at == _T1 and not watchers["acked"].open

        graph = watchers["graph"].checks[0]
        assert graph.definition == "graph" and graph.armed_head == 4
        assert graph.check_command is None and graph.condition is not None
        assert graph.condition.node_id == "exp/a"
        assert graph.graph_target is not None and graph.graph_target.kind == "main"

        assert watchers["orch"].agent_id == "ar-root"
        assert watchers["orch"].origin_turn_id == "orch-turn"

        with store.connection() as connection:
            ledger = connection.execute(
                "SELECT COUNT(*) FROM storage_schema_migrations WHERE migration_version = 93"
            ).fetchone()[0]
            leftovers = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = '_watchers_v1'"
            ).fetchone()[0]
        assert (ledger, leftovers) == (1, 0)
