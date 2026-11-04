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
    assert store.storage_schema_ledger_head() == AppStore.storage_schema_registry_head()
    assert not inventory.unclassified_tables
    validate_transfer_table_policy(inventory.project_linked_tables)
    store.close()
    assert (
        AppStore(database).storage_schema_ledger_head() == AppStore.storage_schema_registry_head()
    )


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
    store.set_chat_archived("project-a", project.thread_id, "member", archived=True)
    reopened = store.ensure_agent_threads("member", ["project-a"], default_names=("Pip", "Puck"))
    assert not store.chat_archived("project-a", project.thread_id)
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
    store.refuse_agent_handoff(first.handoff_id, "thread_closed")
    store.refuse_agent_handoff(first.handoff_id, "later")
    assert store.pending_agent_handoffs(target.thread_id) == []
    assert store.create_agent_handoff(**arguments).refusal_reason == "thread_closed"


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
        for queue_id, started in (("waiting", None), ("started", "queued-turn")):
            connection.execute(
                "INSERT INTO agent_thread_queue(queue_id,thread_id,user_id,idempotency_key,message,request_sha256,created_at,operation_id) VALUES(?,'target','member',?,'message','sha',?,?)",
                (queue_id, queue_id, now, started),
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
        assert [
            row[0] for row in connection.execute("SELECT queue_id FROM agent_thread_queue")
        ] == ["started"]


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
    store.queue_agent_thread_message(
        queue_id="queued",
        thread_id=thread.thread_id,
        user_id="member",
        idempotency_key="key",
        message="Next",
        request_sha256="sha",
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
    assert store.delete_project_records("project")["agent_thread_queue"] == 1
    with store.connection() as connection:
        for table in (
            "agent_handoffs",
            "agent_thread_queue",
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


def test_thread_list_reads_a_fixed_number_of_statements_and_writes_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    from contextlib import contextmanager

    from rcp.runs.agent_threads import thread_projections
    from tests.test_storage import _task_record

    store = AppStore(tmp_path / "rcp.sqlite3")
    project_ids = [f"project-{index}" for index in range(6)]
    for project_id in project_ids:
        store.upsert_project(_project(project_id))
    connection = store.connection

    def listing(user_id: str, visible: list[str]) -> tuple[list[dict], list[str]]:
        statements: list[str] = []

        @contextmanager
        def traced():
            with connection() as opened:
                opened.set_trace_callback(statements.append)
                yield opened

        monkeypatch.setattr(store, "connection", traced)
        threads = store.ensure_agent_threads(user_id, visible, default_names=("Pip",))
        rows = thread_projections(store, threads)
        monkeypatch.setattr(store, "connection", connection)
        return rows, statements

    _, created = listing("few", project_ids[:1])
    assert any("BEGIN IMMEDIATE" in statement for statement in created)
    threads = {
        row.project_id: row
        for row in store.ensure_agent_threads("many", project_ids, default_names=("Pip",))
    }
    store.create_agent_task(
        _task_record(
            store.now(),
            operation_id="turn",
            project_id="project-1",
            kind="project_chat",
            request={"chat_id": threads["project-1"].thread_id},
        )
    )
    with store.connection() as opened:
        opened.execute(
            "INSERT INTO questions(question_id,project_id,owner_kind,owner_id,key,origin_json,"
            "argument_hash,question,choices_json,multiple,state,created_at) "
            "VALUES('q','project-0','chat',?,'key','{}','hash','Question?','[]',0,'pending',?)",
            (threads["project-0"].thread_id, store.now()),
        )
    _, few = listing("few", project_ids[:1])
    rows, many = listing("many", project_ids)
    assert len(rows) == len(project_ids) + 1
    assert len(many) == len(few)
    assert not any(
        statement.lstrip().upper().startswith(("BEGIN IMMEDIATE", "INSERT", "UPDATE"))
        for statement in many
    )
    by_project = {row["project_id"]: row for row in rows}
    assert by_project["project-0"]["live_state"] == "needs_you"
    assert by_project["project-1"]["live_state"] == "queued"
    assert by_project["project-1"]["latest_operation_id"] == "turn"
    assert by_project["project-2"]["live_state"] == "idle"
    assert by_project["project-2"]["project_name"] == "project-2"
    assert by_project[None]["project_name"] is None
    assert all(row["agent_name"] and row["browser_requested"] for row in rows)


def _queue_world(tmp_path: Path):
    from tests.helpers import authorized_human

    store = AppStore(tmp_path / "rcp.sqlite3")
    actor = authorized_human(store)
    thread = next(
        row
        for row in store.ensure_agent_threads(actor.user_id, ["project"], default_names=("Pip",))
        if row.kind == "project"
    )
    return store, actor, thread


def _queued(store: AppStore, thread, key: str, message: str):
    return store.queue_agent_thread_message(
        queue_id=key + "-row",
        thread_id=thread.thread_id,
        user_id=thread.user_id,
        idempotency_key=key,
        message=message,
        request_sha256=message,
    )


def _queued_turn(store: AppStore, actor, thread, row, operation_id: str):
    from rcp.service import RunRequest
    from tests.test_storage import _task_record

    return _task_record(
        store.now(),
        operation_id=operation_id,
        kind="project_chat",
        authorized_by=actor,
        request=RunRequest(
            chat_scope="project", chat_id=thread.thread_id, mode="work", message=row.message
        ).model_dump(mode="json"),
    )


def test_queue_insert_is_idempotent_and_refuses_a_reused_key(tmp_path: Path) -> None:
    import pytest

    from rcp.limits import AGENT_THREAD_QUEUE_MAX_ITEMS
    from rcp.storage.agent_threads import AgentThreadQueueFull

    store, _actor, thread = _queue_world(tmp_path)
    first = _queued(store, thread, "key", "Next, check the logs")
    assert _queued(store, thread, "key", "Next, check the logs") == first
    with pytest.raises(ValueError):
        _queued(store, thread, "key", "Something else")
    assert store.agent_thread_queue(thread.thread_id) == [first]
    for index in range(1, AGENT_THREAD_QUEUE_MAX_ITEMS):
        _queued(store, thread, f"key-{index}", "More")
    with pytest.raises(AgentThreadQueueFull):
        _queued(store, thread, "one-too-many", "More")


def test_queued_turn_claim_is_fifo_once_and_never_for_a_removed_row(tmp_path: Path) -> None:
    import threading

    import pytest

    store, actor, thread = _queue_world(tmp_path)
    older = _queued(store, thread, "older", "First")
    newer = _queued(store, thread, "newer", "Second")
    assert [row.queue_id for row in store.pending_agent_thread_queue()] == [older.queue_id]
    with pytest.raises(ValueError):
        store.create_agent_task(
            _queued_turn(store, actor, thread, newer, "newer-turn"), queue_id=newer.queue_id
        )
    turn = _queued_turn(store, actor, thread, older, "older-turn")
    results: list[str] = []
    barrier = threading.Barrier(2)

    def drain() -> None:
        barrier.wait()
        results.append(store.create_agent_task(turn, queue_id=older.queue_id).operation_id)

    workers = [threading.Thread(target=drain) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert results == ["older-turn", "older-turn"]
    assert [task.operation_id for task in store.agent_tasks("project")] == ["older-turn"]
    with pytest.raises(ValueError):
        store.create_agent_task(
            turn.model_copy(update={"operation_id": "another"}), queue_id=older.queue_id
        )
    assert store.remove_agent_thread_queue_item(older.queue_id) is None
    removed = store.remove_agent_thread_queue_item(newer.queue_id)
    assert removed is not None and removed.removed_at is not None
    assert removed.model_copy(update={"removed_at": None}) == newer
    store.complete_agent_task("older-turn", applied_revision=None, result={})
    with pytest.raises(ValueError):
        store.create_agent_task(
            _queued_turn(store, actor, thread, newer, "newer-turn"), queue_id=newer.queue_id
        )
    assert store.agent_task("newer-turn") is None


def test_refused_queue_row_never_starts_and_stops_blocking(tmp_path: Path) -> None:
    import pytest

    store, actor, thread = _queue_world(tmp_path)
    refused = _queued(store, thread, "refused", "First")
    waiting = _queued(store, thread, "waiting", "Second")
    assert store.refuse_agent_thread_queue_item(refused.queue_id, "thread_closed") is not None
    assert store.refuse_agent_thread_queue_item(refused.queue_id, "later") is None
    with pytest.raises(ValueError):
        store.create_agent_task(
            _queued_turn(store, actor, thread, refused, "refused-turn"),
            queue_id=refused.queue_id,
        )
    assert [row.queue_id for row in store.pending_agent_thread_queue()] == [waiting.queue_id]
    listed = store.agent_thread_queue(thread.thread_id)
    assert [(row.queue_id, row.refusal_reason) for row in listed] == [
        (refused.queue_id, "thread_closed"),
        (waiting.queue_id, None),
    ]
    deferred = store.defer_agent_thread_queue_item(waiting.queue_id)
    assert store.defer_agent_thread_queue_item(waiting.queue_id) == deferred
