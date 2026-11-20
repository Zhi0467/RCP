"""Agent identity, its current session, and the migration that names old agents."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from rcp.storage import AppStore
from rcp.storage.agent_tasks import SESSION_TASK_KINDS
from rcp.storage.agents import backfill_agents
from rcp.storage.base import AppStoreBase
from tests.test_storage import _task_record

_AGENTS_MIGRATION = 93


def _legacy_scan(connection: sqlite3.Connection, project_id: str, kind: str, chat_id: str):
    """The newest-turn scan the session pointer replaced, kept as the oracle."""
    row = connection.execute(
        """
        SELECT operation_id FROM graph_runs
        WHERE project_id = ? AND kind = ?
          AND json_extract(request_json, '$.chat_id') = ?
          AND native_session_id IS NOT NULL AND native_session_id != ''
          AND (json_extract(request_json, '$.artifact_edit') IS NULL
               OR json_extract(request_json, '$.artifact_edit.origin_bound') = 0)
        ORDER BY created_at DESC, rowid DESC LIMIT 1
        """,
        (project_id, kind, chat_id),
    ).fetchone()
    return row[0] if row is not None else None


def _bound(store: AppStore, kind: str, chat_id: str, project_id: str = "project") -> str | None:
    with store.connection() as connection:
        row = store._current_chat_session_row(connection, project_id, kind, chat_id)
        assert (row["operation_id"] if row is not None else None) == _legacy_scan(
            connection, project_id, kind, chat_id
        )
    return row["operation_id"] if row is not None else None


def _turn(store: AppStore, operation_id: str, *, chat_id: str = "chat", **fields) -> None:
    request = {"chat_id": chat_id, "provider": "codex", "run_on": "laptop"}
    request.update(fields.pop("request", {}))
    store.create_agent_task(
        _task_record(
            store.now(),
            operation_id=operation_id,
            kind="project_chat",
            status="succeeded",
            status_message="succeeded",
            request=request,
            **fields,
        )
    )


def test_a_turn_names_its_agent_and_the_session_follows_the_newest_bound_turn(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    assert store.agent("chat") is None
    _turn(store, "first")
    agent = store.agent("chat")
    assert agent is not None
    assert (agent.project_id, agent.visibility, agent.entry, agent.owner_user_id) == (
        "project",
        "shared",
        "project",
        None,
    )
    assert store.agent_session("chat") is None and _bound(store, "project_chat", "chat") is None

    store.checkpoint_agent_task("first", native_session_id="session-a", stage_root="/stage/a")
    session = store.agent_session("chat")
    assert session is not None
    assert (session.provider, session.machine_id, session.native_session_id) == (
        "codex",
        "laptop",
        "session-a",
    )
    assert (session.stage_root, session.operation_id) == ("/stage/a", "first")

    _turn(store, "second", native_session_id="session-a", stage_root="/stage/a")
    assert _bound(store, "project_chat", "chat") == "second"
    assert store.agent_task_receipts_by_category("second", "agent_session_replaced") == []

    _turn(store, "edit", request={"artifact_edit": {"origin_bound": True}})
    store.checkpoint_agent_task("edit", native_session_id="session-edit", stage_root="/stage/e")
    assert _bound(store, "project_chat", "chat") == "second"

    _turn(store, "third", request={"provider": "claude"})
    assert _bound(store, "project_chat", "chat") == "second"
    store.checkpoint_agent_task("first", native_session_id="session-a", stage_root="/stage/a")
    assert _bound(store, "project_chat", "chat") == "second"
    store.checkpoint_agent_task("third", native_session_id="session-b", stage_root="/stage/b")
    assert _bound(store, "project_chat", "chat") == "third"
    replaced = store.agent_task_receipts_by_category("third", "agent_session_replaced")
    assert len(replaced) == 1
    assert replaced[0].payload["replaced_operation_id"] == "second"
    assert replaced[0].payload["replaced_provider"] == "codex"

    assert _bound(store, "node_chat", "chat") is None
    assert _bound(store, "project_chat", "other") is None
    store.close()


def _old_store(path: Path, monkeypatch: pytest.MonkeyPatch) -> AppStore:
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
        if version < _AGENTS_MIGRATION:
            run_migration(self, connection, version=version, name=name, migration=migration)

    monkeypatch.setattr(AppStore, "_baseline_storage_schema_cache", None)
    monkeypatch.setattr(AppStore, "_baseline_storage_sql", ())
    monkeypatch.setattr(AppStoreBase, "_run_storage_schema_migration", old_migration)
    monkeypatch.setattr(
        AppStoreBase,
        "_STORAGE_SCHEMA_MIGRATIONS",
        tuple(item for item in migrations if item[0] < _AGENTS_MIGRATION),
    )
    return AppStore(path)


def _old_run(
    connection: sqlite3.Connection,
    operation_id: str,
    *,
    project_id: str = "project",
    kind: str = "project_chat",
    created_at: str,
    session: str | None = None,
    episode_id: str | None = None,
    **request: object,
) -> None:
    connection.execute(
        "INSERT INTO graph_runs(operation_id,project_id,episode_id,kind,status,request_json,"
        "created_at,updated_at,status_message,native_session_id,stage_root) "
        "VALUES(?,?,?,?,'succeeded',?,?,?,'succeeded',?,?)",
        (
            operation_id,
            project_id,
            episode_id,
            kind,
            json.dumps({"provider": "codex", "run_on": "laptop", **request}),
            created_at,
            created_at,
            session,
            f"/stage/{operation_id}" if session else None,
        ),
    )


def _old_episode(
    connection: sqlite3.Connection, episode_id: str, mode: str, continues: str | None = None
) -> None:
    connection.execute(
        "INSERT INTO episodes(episode_id,project_id,mode,status,invocation_ceiling,created_at,"
        "updated_at,continues_episode_id) VALUES(?,'project',?,'completed',1,'2026-01-01',"
        "'2026-01-01',?)",
        (episode_id, mode, continues),
    )


def _snapshot(store: AppStore) -> dict[str, list[tuple]]:
    with store.connection() as connection:
        return {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("agents", "agent_sessions", "episodes")
        }


def test_older_database_upgrades_every_agent_kind_and_its_session(tmp_path, monkeypatch) -> None:
    path = tmp_path / "old.sqlite3"
    with monkeypatch.context() as legacy:
        old = _old_store(path, legacy)
        assert old.storage_schema_ledger_head() == _AGENTS_MIGRATION - 1
        space_id = old.space_id
        with old.connection() as connection:
            connection.executemany(
                "INSERT INTO agent_threads(thread_id,user_id,kind,project_id,created_at) "
                "VALUES(?,?,?,?,'2026-01-01')",
                [
                    ("space-thread", "member", "space", None),
                    ("thread", "member", "project", "project"),
                ],
            )
            connection.execute(
                "INSERT INTO chat_display(project_id,chat_id,title,archived_at) "
                "VALUES('project','worker','Worker name','2026-02-02')"
            )
            _old_run(connection, "s1", created_at="2026-01-01", session="a", chat_id="shared")
            _old_run(connection, "s2", created_at="2026-01-03", session="b", chat_id="shared")
            _old_run(connection, "s3", created_at="2026-01-04", chat_id="shared")
            _old_run(
                connection,
                "s4",
                created_at="2026-01-05",
                session="edit",
                chat_id="shared",
                artifact_edit={"origin_bound": True},
            )
            _old_run(connection, "t1", created_at="2026-01-02", session="t", chat_id="thread")
            _old_run(
                connection,
                "sp1",
                project_id=space_id,
                kind="space_chat",
                created_at="2026-01-02",
                session="sp",
                chat_id="space-thread",
            )
            for episode_id, mode, continues in (
                ("experiment", "experiment_loop", None),
                ("root", "auto_research", None),
                ("continued", "auto_research", "root"),
            ):
                _old_episode(connection, episode_id, mode, continues)
            connection.execute(
                "INSERT INTO experiment_episode_state(episode_id,chat_id,created_at,updated_at) "
                "VALUES('experiment','experiment-chat','2026-01-01','2026-01-01')"
            )
            connection.executemany(
                "INSERT INTO auto_research_episodes(episode_id,created_at,updated_at) "
                "VALUES(?,'2026-01-01','2026-01-01')",
                [("root",), ("continued",)],
            )
            _old_run(
                connection, "o1", kind="auto_research", created_at="2026-01-01", episode_id="root"
            )
            _old_run(
                connection,
                "w1",
                kind="auto_research",
                created_at="2026-01-02",
                session="w",
                episode_id="root",
                chat_id="worker",
            )
            connection.execute(
                "INSERT INTO auto_research_child_work(worker_id,episode_id,project_id,"
                "root_operation_id,current_operation_id,admitted_by_operation_id,instruction,"
                "instruction_sha256,created_at,updated_at) VALUES('worker','root','project','w1',"
                "'w1','o1','go','hash','2026-01-02','2026-01-02')"
            )
        old.close()

    upgraded = AppStore(path)
    placements = {
        agent_id: (found.project_id, found.visibility, found.entry, found.owner_user_id)
        for agent_id in (
            "space-thread",
            "thread",
            "shared",
            "experiment-chat",
            "worker",
            "root",
        )
        if (found := upgraded.agent(agent_id)) is not None
    }
    assert placements == {
        "space-thread": (None, "private", "single_entry", "member"),
        "thread": ("project", "private", "project", "member"),
        "shared": ("project", "shared", "project", None),
        "experiment-chat": ("project", "shared", "project", None),
        "worker": ("project", "shared", "project", None),
        "root": ("project", "shared", "project", None),
    }
    assert upgraded.agent("continued") is None
    worker = upgraded.agent("worker")
    assert worker is not None
    assert (worker.title, worker.archived_at, worker.created_at) == (
        "Worker name",
        "2026-02-02",
        "2026-01-02",
    )
    assert _bound(upgraded, "project_chat", "shared") == "s2"
    assert _bound(upgraded, "project_chat", "thread") == "t1"
    assert _bound(upgraded, "space_chat", "space-thread", space_id) == "sp1"
    assert upgraded.agent_session("worker") is None
    session = upgraded.agent_session("shared")
    assert session is not None
    assert (session.native_session_id, session.stage_root) == ("b", "/stage/s2")
    current = upgraded.current_chat_session("project", "project_chat", "shared")
    assert current is not None and current.native_session_id == "b"
    with upgraded.connection() as connection:
        assert dict(connection.execute("SELECT episode_id, agent_id FROM episodes").fetchall()) == {
            "experiment": "experiment-chat",
            "root": "root",
            "continued": "root",
        }
        before = _snapshot(upgraded)
        backfill_agents(connection, SESSION_TASK_KINDS)
    assert _snapshot(upgraded) == before

    _turn(upgraded, "s5", chat_id="shared", native_session_id="b", stage_root="/stage/s2")
    assert _bound(upgraded, "project_chat", "shared") == "s5"
    upgraded.close()


def test_a_member_may_hold_more_private_agents_but_one_default(tmp_path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    threads = store.ensure_agent_threads("member", ["project"], default_names=("Pip",))
    assert all(thread.is_default for thread in threads)
    default = next(thread for thread in threads if thread.project_id == "project")
    agent = store.agent(default.thread_id)
    assert agent is not None
    assert (agent.visibility, agent.entry, agent.owner_user_id) == ("private", "project", "member")
    extra = "INSERT INTO agent_threads(thread_id,user_id,kind,project_id,created_at,is_default) "
    with store.connection() as connection:
        connection.execute(extra + "VALUES('second','member','project','project','2020-01-01',0)")
    with pytest.raises(sqlite3.IntegrityError), store.connection() as connection:
        connection.execute(extra + "VALUES('third','member','project','project','2020-01-01',1)")
    again = store.ensure_agent_threads("member", ["project"], default_names=("Pip",))
    assert {thread.thread_id for thread in again if thread.is_default} == {
        thread.thread_id for thread in threads
    }
    assert [thread.thread_id for thread in again if not thread.is_default] == ["second"]
    store.close()
