from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path

from rcp.storage import AppStore
from rcp.transfer.archive import inspect_transfer_table_inventory
from rcp.transfer.records import validate_transfer_table_policy


def test_old_writer_database_upgrades_without_changing_legacy_rows(tmp_path: Path) -> None:
    source = Path(__file__).parent / "fixtures/restore_schema/provider-logins-v0.4.35.sqlite3.gz"
    database = tmp_path / "rcp.sqlite3"
    database.write_bytes(gzip.decompress(source.read_bytes()))
    with sqlite3.connect(database) as connection:
        before = connection.execute(
            "SELECT operation_id, request_json, result_json FROM graph_runs"
        ).fetchall()
    store = AppStore(database)
    with store.connection() as connection:
        assert (
            connection.execute(
                "SELECT operation_id, request_json, result_json FROM graph_runs"
            ).fetchall()
            == before
        )
        assert connection.execute("SELECT count(*) FROM agent_threads").fetchone()[0] == 0
        inventory = inspect_transfer_table_inventory(connection)
    assert store.storage_schema_ledger_head() == 73
    assert not inventory.unclassified_tables
    validate_transfer_table_policy(inventory.project_linked_tables)
    store.close()
    assert AppStore(database).storage_schema_ledger_head() == 73


def test_lazy_thread_creation_seeds_names_and_browser_once(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    rows = store.ensure_agent_threads(
        "member", ["project-a", "project-b"], default_names=("Pip", "Puck")
    )
    assert len(rows) == 3
    assert len({row.thread_id for row in rows}) == 3
    assert (
        store.ensure_agent_threads(
            "member", ["project-a", "project-b"], default_names=("Pip", "Puck")
        )
        == rows
    )
    with store.connection() as connection:
        assert {row[0] for row in connection.execute("SELECT title FROM chat_display")} == {
            "Pip",
            "Puck",
            "Pip 2",
        }
        assert {
            row[0]
            for row in connection.execute("SELECT browser_requested FROM chat_browser_preferences")
        } == {1}
    assert not store.agent_threads("other")
    project = next(row for row in rows if row.project_id == "project-a")
    store.close_agent_thread(project.thread_id, reason="access_lost")
    closed = store.agent_thread(project.thread_id)
    assert closed is not None
    assert closed.closed_reason == "access_lost"
    reopened = store.ensure_agent_threads("member", ["project-a"], default_names=("Pip", "Puck"))
    assert next(row for row in reopened if row.project_id == "project-a").closed_at is None


def test_handoff_deduplication_preserves_pending_delivery(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    rows = store.ensure_agent_threads("member", ["project"], default_names=("Pip",))
    source = next(row for row in rows if row.kind == "space")
    target = next(row for row in rows if row.kind == "project")
    arguments = dict(
        source_thread_id=source.thread_id,
        source_operation_id="source-turn",
        idempotency_key="key",
        target_thread_id=target.thread_id,
        message="Please check",
    )
    first = store.create_agent_handoff(**arguments)
    assert store.create_agent_handoff(**arguments) == first
    assert store.pending_agent_handoffs(target.thread_id) == [first]
    store.mark_agent_handoff_delivered(first.handoff_id, "target-turn")
    assert store.pending_agent_handoffs(target.thread_id) == []
    assert store.create_agent_handoff(**arguments).target_operation_id == "target-turn"


def test_restore_disables_schedules_expires_cards_and_preserves_handoffs(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    now = store.now()
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO agent_schedules(schedule_id,thread_id,created_operation_id,idempotency_key,cron,timezone,note,state,created_at) VALUES('s','thread','op','key','0 9 * * *','UTC','note','active',?)",
            (now,),
        )
        connection.execute(
            "INSERT INTO approval_cards(question_id,action_json,pinned_json,pinned_sha256,expires_at,request_id) VALUES('q','{}','{}','hash','2099-01-01T00:00:00+00:00','request')"
        )
        connection.execute(
            "INSERT INTO agent_handoffs(handoff_id,source_thread_id,source_operation_id,idempotency_key,target_thread_id,message,created_at) VALUES('h','source','op','key','target','message',?)",
            (now,),
        )
    store.detach_restored_lifecycle(diagnostic="Restored", confirmed_by="Operator", detached_at=now)
    with store.connection() as connection:
        assert tuple(
            connection.execute("SELECT state,disabled_reason FROM agent_schedules").fetchone()
        ) == ("disabled", "restore")
        assert connection.execute("SELECT expires_at FROM approval_cards").fetchone()[0] == now
        assert (
            connection.execute("SELECT target_operation_id FROM agent_handoffs").fetchone()[0]
            is None
        )


def test_transfer_redacts_private_text_but_keeps_patch_provenance(tmp_path: Path) -> None:
    import json

    store = AppStore(tmp_path / "rcp.sqlite3")
    thread = next(
        row
        for row in store.ensure_agent_threads("member", ["project"], default_names=("Pip",))
        if row.kind == "project"
    )
    now = store.now()
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO graph_runs(operation_id,project_id,kind,status,request_json,result_json,created_at,updated_at,status_message,applied_revision) VALUES('op','project','project_chat','succeeded',?,?,?,?, 'Done',3)",
            (
                json.dumps({"chat_id": thread.thread_id, "message": "private request"}),
                json.dumps({"answer": "private answer", "trace_messages": ["private trace"]}),
                now,
                now,
            ),
        )
        connection.execute("UPDATE graph_runs SET finished_at=? WHERE operation_id='op'", (now,))
        records = store._transfer_tasks(connection, "project", ())
    assert len(records) == 1
    assert records[0].operation_id == "op"
    assert records[0].applied_revision == 3
    assert "private request" not in records[0].model_dump_json()
    assert "private answer" not in records[0].model_dump_json()
    assert "private trace" not in records[0].model_dump_json()
