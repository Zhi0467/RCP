from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from rcp.limits import AGENT_TASK_LIST_FINISHED_CHAT_SECONDS, HIDDEN_READ_READINESS_TTL_SECONDS
from rcp.storage import AppStore
from rcp.storage.question_activity import QuestionActivity
from rcp.storage.resource_deadlines import record_question_activity


@pytest.mark.parametrize("family", ["task", "artifact", "consolidation", "readiness"])
def test_deadlines_follow_owner_transactions_and_cross_without_writes(tmp_path, family):
    store = AppStore(tmp_path / "app.sqlite3")
    now = datetime.now(UTC)
    stamp = now.isoformat()
    if family == "task":
        table, resource, offset = (
            "graph_runs",
            "tasks.status",
            AGENT_TASK_LIST_FINISHED_CHAT_SECONDS,
        )
        fields = dict(
            operation_id="task",
            project_id="p",
            kind="project_chat",
            status="succeeded",
            request_json='{"chat_id":"chat"}',
            created_at=stamp,
            updated_at=stamp,
            finished_at=stamp,
            status_message="",
            visible=1,
        )
    elif family == "artifact":
        table, resource, offset = "artifacts", "artifacts", 0
        fields = dict(
            artifact_id="artifact", project_id="p", metadata=json.dumps({"expires_at": stamp})
        )
    elif family == "consolidation":
        table, resource, offset = "consolidation_schedules", "consolidation", 0
        fields = dict(
            project_id="p",
            authorization_id="auth",
            local_time="12:00",
            timezone="UTC",
            authorized_by_json="{}",
            authorized_at=stamp,
            expires_at=stamp,
            next_due_at=stamp,
        )
    else:
        table, resource, offset = (
            "provider_readiness_snapshots",
            "settings",
            HIDDEN_READ_READINESS_TTL_SECONDS,
        )
        fields = dict(
            provider="codex",
            host="",
            binary="codex",
            version="v",
            readiness_json="{}",
            probed_at=stamp,
        )
    sql = f"INSERT INTO {table} ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})"
    with store.connection() as connection:
        connection.execute("SAVEPOINT owner_write")
        connection.execute(sql, tuple(fields.values()))
        assert connection.execute("SELECT COUNT(*) FROM resource_deadlines").fetchone()[0] > 0
        connection.execute("ROLLBACK TO owner_write")
        assert connection.execute("SELECT COUNT(*) FROM resource_deadlines").fetchone()[0] == 0
        connection.execute(sql, tuple(fields.values()))
    with store.connection() as connection:
        deadline = connection.execute(
            "SELECT deadline FROM resource_deadlines WHERE resource=?", (resource,)
        ).fetchone()[0]
        assert deadline == pytest.approx(now.timestamp() + offset, abs=0.001)
        query = (
            "SELECT MAX(deadline) FROM resource_deadlines "
            "WHERE scope=? AND resource=? AND entity='' AND deadline<=?"
        )
        scope = "" if family == "readiness" else "p"
        assert connection.execute(query, (scope, resource, deadline - 1)).fetchone()[0] is None
        assert connection.execute(query, (scope, resource, deadline + 1)).fetchone()[0] == deadline
        connection.execute(f"DELETE FROM {table}")
        assert connection.execute("SELECT COUNT(*) FROM resource_deadlines").fetchone()[0] == 0


def test_question_activity_publishes_pending_and_forget_without_changing_attempts():
    changes = []
    clock = [0.0]
    activity = QuestionActivity(clock=lambda: clock[0], changed=lambda *args: changes.append(args))
    assert activity.pending("q", "call") == 1
    assert activity.pending("q", "call") == 1
    assert activity.is_waiting("q")
    activity.forget("q")
    assert not activity.is_waiting("q")
    assert changes == [("q", True), ("q", True), ("q", False)]


def test_question_pending_deadline_and_counter_share_callback_transaction(tmp_path):
    store = AppStore(tmp_path / "app.sqlite3")
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO questions(question_id,project_id,owner_kind,owner_id,key,origin_json,"
            "argument_hash,question,choices_json,multiple,state,created_at) "
            "VALUES('q','p','chat','c','key','{}','hash','Question?','[]',0,'pending',?)",
            (store.now(),),
        )
        before = connection.execute(
            "SELECT generation FROM resource_versions WHERE scope='p' AND resource='questions'"
        ).fetchone()[0]
    store.question_activity.pending("q", "call")
    with store.connection() as connection:
        assert (
            connection.execute(
                "SELECT generation FROM resource_versions WHERE scope='p' AND resource='questions'"
            ).fetchone()[0]
            > before
        )
        deadline = connection.execute(
            "SELECT deadline FROM resource_deadlines WHERE resource='questions'"
        ).fetchone()[0]
        assert deadline > datetime.now(UTC).timestamp()
        assert (
            connection.execute(
                "SELECT MAX(deadline) FROM resource_deadlines WHERE scope='p' AND resource='questions' "
                "AND entity='' AND deadline<=?",
                (deadline + 1,),
            ).fetchone()[0]
            == deadline
        )
    with store.connection() as connection:
        generation = connection.execute(
            "SELECT generation FROM resource_versions WHERE scope='p' AND resource='questions'"
        ).fetchone()[0]
        record_question_activity(connection, "q", True, now=deadline - 1)
        assert (
            connection.execute(
                "SELECT generation FROM resource_versions WHERE scope='p' AND resource='questions'"
            ).fetchone()[0]
            == generation
        )
        renewed = connection.execute(
            "SELECT deadline FROM resource_deadlines WHERE resource='questions'"
        ).fetchone()[0]
        assert renewed > deadline
        record_question_activity(connection, "q", True, now=renewed)
        assert (
            connection.execute(
                "SELECT generation FROM resource_versions WHERE scope='p' AND resource='questions'"
            ).fetchone()[0]
            == generation + 1
        )
    store.question_activity.forget("q")
    with store.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM resource_deadlines WHERE resource='questions'"
            ).fetchone()[0]
            == 0
        )


def test_question_activity_publication_failure_preserves_visible_liveness(caplog):
    def changed(question_id: str, waiting: bool) -> None:
        # Publication can read liveness: SQLite work does not hold its lock.
        assert activity.is_waiting(question_id) == waiting
        raise sqlite3.OperationalError("database is locked")

    activity = QuestionActivity(changed=changed)
    assert activity.pending("q", "call") == 1
    assert activity.is_waiting("q")
    assert activity.pending("q", "call") == 1
    activity.forget("q")
    assert not activity.is_waiting("q")
    assert len(caplog.records) == 3
    assert all(
        record.exc_info is not None and record.exc_info[0] is sqlite3.OperationalError
        for record in caplog.records
    )


def test_slow_activity_publication_does_not_extend_liveness_past_its_deadline():
    from rcp.limits import ASK_WAITING_FRESH_SECONDS

    clock = [0.0]

    def delayed_publication(*_):
        clock[0] += ASK_WAITING_FRESH_SECONDS

    activity = QuestionActivity(clock=lambda: clock[0], changed=delayed_publication)
    activity.pending("q", "call")
    assert not activity.is_waiting("q")
