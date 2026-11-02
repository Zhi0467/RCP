from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path

from rcp.storage import AppStore
from rcp.storage.models import ProjectRecord
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


def test_restore_detaches_schedules_cards_handoffs_and_uncaptured_memory(tmp_path: Path) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    now = store.now()
    store.upsert_project(_project("remote-project", remote=True))
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO agent_schedules(schedule_id,thread_id,created_operation_id,idempotency_key,cron,timezone,note,state,created_at) VALUES('s','thread','op','key','0 9 * * *','UTC','note','active',?)",
            (now,),
        )
        connection.execute(
            "INSERT INTO approval_cards(question_id,action_json,pinned_json,pinned_sha256,expires_at,request_id) VALUES('q','{}','{}','hash','2099-01-01T00:00:00+00:00','request')"
        )
        for handoff_id, delivered in (("pending", None), ("settled", "target-turn")):
            connection.execute(
                "INSERT INTO agent_handoffs(handoff_id,source_thread_id,source_operation_id,idempotency_key,target_thread_id,message,created_at,target_operation_id,settled_at) VALUES(?,'source','op',?,'target','message',?,?,?)",
                (handoff_id, handoff_id, now, delivered, delivered and now),
            )
        for project_id in ("captured", "uncaptured", "remote-project"):
            connection.execute("INSERT INTO memory_migrations VALUES (?,?,0)", (project_id, now))
    store.detach_restored_lifecycle(
        diagnostic="Restored",
        confirmed_by="Operator",
        detached_at=now,
        restored_memory_projects={"captured", "remote-project"},
    )
    with store.connection() as connection:
        assert tuple(
            connection.execute("SELECT state,disabled_reason FROM agent_schedules").fetchone()
        ) == ("disabled", "restore")
        assert connection.execute("SELECT expires_at FROM approval_cards").fetchone()[0] == now
        # Nothing remains for startup to launch or wake on the restored host.
        assert not connection.execute(
            "SELECT 1 FROM agent_handoffs WHERE target_operation_id IS NULL "
            "OR notified_operation_id IS NULL"
        ).fetchall()
        assert [row[0] for row in connection.execute("SELECT handoff_id FROM agent_handoffs")] == [
            "settled"
        ]
        assert [
            row[0] for row in connection.execute("SELECT project_id FROM memory_migrations")
        ] == ["captured"]


def _project(project_id: str, *, remote: bool = False) -> ProjectRecord:
    return ProjectRecord(
        project_id=project_id,
        locator=f"/tmp/{project_id}/research.yaml",
        name=project_id,
        state_location=f"host:/{project_id}/.research"
        if remote
        else f"/tmp/{project_id}/.research",
        state_remote=remote,
        added_at="2026-07-31T00:00:00+00:00",
    )


def test_project_deletion_clears_agent_state_and_reregistration_names_a_fresh_thread(
    tmp_path: Path,
) -> None:
    store = AppStore(tmp_path / "rcp.sqlite3")
    store.upsert_project(_project("project"))
    space, thread = store.ensure_agent_threads("member", ["project"], default_names=("Pip",))
    store.create_agent_handoff(
        source_thread_id=space.thread_id,
        source_operation_id="source-turn",
        idempotency_key="key",
        target_thread_id=thread.thread_id,
        message="Please check",
    )
    now = store.now()
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO agent_schedules(schedule_id,thread_id,created_operation_id,idempotency_key,cron,timezone,note,state,created_at) VALUES('s',?,'op','key','0 9 * * *','UTC','note','active',?)",
            (thread.thread_id, now),
        )
        connection.execute(
            "INSERT INTO questions(question_id,project_id,owner_kind,owner_id,key,origin_json,"
            "argument_hash,question,choices_json,multiple,state,created_at) "
            "VALUES('q','project','chat',?,'key','{}','hash','Question?','[]',0,'pending',?)",
            (thread.thread_id, now),
        )
        connection.execute(
            "INSERT INTO approval_cards(question_id,action_json,pinned_json,pinned_sha256,expires_at,request_id) VALUES('q','{}','{}','hash','2099-01-01T00:00:00+00:00','request')"
        )
        connection.execute(
            "INSERT INTO space_turn_project_bindings VALUES('turn','project','{}','scope','{}')"
        )
    store.delete_project_records("project")
    with store.connection() as connection:
        for table in (
            "agent_handoffs",
            "agent_schedules",
            "approval_cards",
            "space_turn_project_bindings",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    store.upsert_project(_project("project"))
    reopened = store.ensure_agent_threads("member", ["project"], default_names=("Pip",))[1]
    assert reopened.thread_id != thread.thread_id
    with store.connection() as connection:
        assert connection.execute(
            "SELECT title FROM chat_display WHERE chat_id=?", (reopened.thread_id,)
        ).fetchone()[0]


def test_project_identity_migration_keeps_one_named_thread(tmp_path: Path) -> None:
    import uuid

    store = AppStore(tmp_path / "rcp.sqlite3")
    store.upsert_project(_project("legacy"))
    _, thread = store.ensure_agent_threads("member", ["legacy"], default_names=("Pip",))
    canonical = str(uuid.uuid4())
    title = store.chat_name("legacy", thread.thread_id, "")
    store.migrate_project_identity("legacy", canonical, store.space_id)
    threads = store.ensure_agent_threads("member", [canonical], default_names=("Pip",))
    assert [row.thread_id for row in threads if row.kind == "project"] == [thread.thread_id]
    with store.connection() as connection:
        rows = connection.execute(
            "SELECT project_id, title FROM chat_display WHERE chat_id=?", (thread.thread_id,)
        ).fetchall()
    assert [tuple(row) for row in rows] == [(canonical, title)]


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


def test_thread_watermark_moves_on_turn_status_not_progress(tmp_path: Path) -> None:
    import json

    store = AppStore(tmp_path / "rcp.sqlite3")
    _, thread = store.ensure_agent_threads("member", ["project"], default_names=("Pip",))
    now = store.now()

    def generation() -> int:
        with store.connection() as connection:
            return connection.execute(
                "SELECT generation FROM resource_versions WHERE resource='agent_threads' "
                "AND entity='member:member'"
            ).fetchone()[0]

    with store.connection() as connection:
        connection.execute(
            "INSERT INTO graph_runs(operation_id,project_id,kind,status,request_json,created_at,updated_at,status_message) VALUES('op','project','project_chat','running',?,?,?,'Started')",
            (json.dumps({"chat_id": thread.thread_id}), now, now),
        )
    started = generation()
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET status_message='Reading', updated_at=? WHERE operation_id='op'",
            (store.now(),),
        )
    assert generation() == started
    with store.connection() as connection:
        connection.execute(
            "UPDATE graph_runs SET status='succeeded', finished_at=? WHERE operation_id='op'",
            (store.now(),),
        )
    assert generation() > started
