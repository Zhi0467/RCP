"""Durable private-thread schedules; a wake consumes its pending notes atomically."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel

from rcp.limits import AGENT_SCHEDULES_MAX_ACTIVE
from rcp.storage.membership import live_project_member
from rcp.storage.mixin_base import StoreMixinBase
from rcp.storage.models import AgentTaskRecord
from rcp.storage.resource_versions import install_agent_thread_version

if TYPE_CHECKING:
    from rcp.storage import AppStore


class AgentSchedule(BaseModel):
    schedule_id: str
    thread_id: str
    created_operation_id: str
    idempotency_key: str
    cron: str | None = None
    at_local: str | None = None
    timezone: str
    note: str
    state: Literal["active", "cancelled", "disabled", "done"]
    disabled_reason: str | None = None
    next_fire_at: str | None = None
    pending_since: str | None = None
    last_fired_at: str | None = None
    created_at: str
    # Why a due wake could not start yet; it retries each poll and clears on launch.
    deferred_reason: str | None = None


def migrate_agent_schedules(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS agent_schedules (
        schedule_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
        created_operation_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
        cron TEXT, at_local TEXT, timezone TEXT NOT NULL, note TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('active','cancelled','disabled','done')),
        disabled_reason TEXT, next_fire_at TEXT, pending_since TEXT,
        last_fired_at TEXT, created_at TEXT NOT NULL, deferred_reason TEXT,
        UNIQUE(created_operation_id, idempotency_key),
        CHECK ((cron IS NULL) != (at_local IS NULL))
    )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS agent_schedules_due ON agent_schedules(state, next_fire_at)"
    )
    # The schedule poller re-reads queued wakes each minute without a project.
    connection.execute(
        "CREATE INDEX IF NOT EXISTS graph_runs_queued_trigger "
        "ON graph_runs(json_extract(request_json, '$.trigger')) WHERE status='queued'"
    )

    install_agent_thread_version(
        connection,
        "agent_schedules",
        "SELECT user_id FROM agent_threads WHERE thread_id=ROW.thread_id",
    )


class AgentSchedulesStoreMixin(StoreMixinBase):
    def agent_schedules(self, thread_id: str | None = None) -> list[AgentSchedule]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_schedules"
                + (" WHERE thread_id=?" if thread_id else "")
                + " ORDER BY created_at, schedule_id",
                (thread_id,) if thread_id else (),
            ).fetchall()
        return [AgentSchedule.model_validate(dict(row)) for row in rows]

    def add_agent_schedule(
        self,
        *,
        thread_id: str,
        operation_id: str,
        key: str,
        cron: str | None,
        at: str | None,
        timezone: str,
        note: str,
        next_fire_at: str,
    ) -> AgentSchedule:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM agent_schedules WHERE created_operation_id=? AND idempotency_key=?",
                (operation_id, key),
            ).fetchone()
            if existing is not None:
                record = AgentSchedule.model_validate(dict(existing))
                if (
                    record.thread_id,
                    record.cron,
                    record.at_local,
                    record.timezone,
                    record.note,
                ) != (thread_id, cron, at, timezone, note):
                    raise ValueError("schedule_key_conflict")
                return record
            count = connection.execute(
                "SELECT COUNT(*) FROM agent_schedules WHERE thread_id=? AND state='active'",
                (thread_id,),
            ).fetchone()[0]
            if count >= AGENT_SCHEDULES_MAX_ACTIVE:
                raise ValueError("schedule_limit")
            schedule = AgentSchedule(
                schedule_id=str(uuid.uuid4()),
                thread_id=thread_id,
                created_operation_id=operation_id,
                idempotency_key=key,
                cron=cron,
                at_local=at,
                timezone=timezone,
                note=note,
                state="active",
                next_fire_at=next_fire_at,
                created_at=self.now(),
            )
            values = schedule.model_dump()
            connection.execute(
                f"INSERT INTO agent_schedules ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                tuple(values.values()),
            )
            return schedule

    def cancel_agent_schedule(self, thread_id: str, schedule_id: str) -> None:
        with self.connection() as connection:
            changed = connection.execute(
                "UPDATE agent_schedules SET state='cancelled', pending_since=NULL, next_fire_at=NULL WHERE thread_id=? AND schedule_id=?",
                (thread_id, schedule_id),
            ).rowcount
            if not changed:
                raise KeyError(schedule_id)

    def enable_agent_schedule(
        self, thread_id: str, schedule_id: str, next_fire_at: str
    ) -> AgentSchedule:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                "SELECT COUNT(*) FROM agent_schedules WHERE thread_id=? AND state='active'",
                (thread_id,),
            ).fetchone()[0]
            if count >= AGENT_SCHEDULES_MAX_ACTIVE:
                raise ValueError("schedule_limit")
            changed = connection.execute(
                "UPDATE agent_schedules SET state='active', disabled_reason=NULL, next_fire_at=?, pending_since=NULL WHERE thread_id=? AND schedule_id=? AND state='disabled'",
                (next_fire_at, thread_id, schedule_id),
            ).rowcount
            if not changed:
                raise ValueError("schedule_not_disabled")
            row = connection.execute(
                "SELECT * FROM agent_schedules WHERE schedule_id=?", (schedule_id,)
            ).fetchone()
            return AgentSchedule.model_validate(dict(row))

    def defer_agent_schedules(self, schedules: list[AgentSchedule], reason: str) -> None:
        """Keep the notes pending and say why the wake has not started."""
        with self.connection() as connection:
            connection.executemany(
                "UPDATE agent_schedules SET deferred_reason=? "
                "WHERE schedule_id=? AND pending_since IS NOT NULL",
                [(reason[:500], schedule.schedule_id) for schedule in schedules],
            )

    def advance_agent_schedules(self, now: datetime) -> list[AgentSchedule]:
        from rcp.consolidation import next_occurrence

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM agent_schedules WHERE state='active' AND next_fire_at<=?",
                (now.isoformat(),),
            ).fetchall()
            for row in rows:
                due = row["next_fire_at"]
                following = (
                    next_occurrence(row["cron"], row["timezone"], now).isoformat()
                    if row["cron"]
                    else None
                )
                connection.execute(
                    "UPDATE agent_schedules SET pending_since=COALESCE(pending_since, ?), next_fire_at=?, state=? WHERE schedule_id=?",
                    (due, following, "active" if following else "done", row["schedule_id"]),
                )
            pending = connection.execute(
                "SELECT * FROM agent_schedules WHERE pending_since IS NOT NULL AND state IN ('active','done') ORDER BY created_at, schedule_id"
            ).fetchall()
            return [AgentSchedule.model_validate(dict(row)) for row in pending]

    def claim_agent_schedule_wake(
        self, schedules: list[AgentSchedule], task: AgentTaskRecord
    ) -> bool:
        if (
            not schedules
            or task.request.get("chat_id") != schedules[0].thread_id
            or task.status != "queued"
            or task.request.get("trigger") != "schedule"
        ):
            raise ValueError("schedule_thread_mismatch")
        store = cast("AppStore", self)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            thread = self._wake_thread(connection, schedules[0].thread_id, task)
            if self._thread_holds_wake(connection, thread, task) or not _notes_unchanged(
                connection, schedules
            ):
                return False
            store._insert_agent_task(connection, task, continuation_cause="message_wake")
            for schedule in schedules:
                connection.execute(
                    "UPDATE agent_schedules SET pending_since=NULL, deferred_reason=NULL, "
                    "last_fired_at=? WHERE schedule_id=?",
                    (task.created_at, schedule.schedule_id),
                )
            return True

    def _wake_thread(
        self, connection: sqlite3.Connection, thread_id: str, task: AgentTaskRecord
    ) -> sqlite3.Row:
        """The open thread, its live member, and a project that accepts work."""
        store = cast("AppStore", self)
        thread = connection.execute(
            "SELECT * FROM agent_threads WHERE thread_id=? AND closed_at IS NULL", (thread_id,)
        ).fetchone()
        if (
            thread is None
            or task.authorized_by is None
            or thread["user_id"] != task.authorized_by.user_id
        ):
            raise ValueError("schedule_thread_unavailable")
        if task.project_id != (thread["project_id"] or store.space_id):
            raise ValueError("schedule_thread_mismatch")
        store._require_active_space_user_from_connection(connection, thread["user_id"])
        if thread["project_id"]:
            if not live_project_member(connection, thread["project_id"], thread["user_id"]):
                raise ValueError("schedule_thread_unavailable")
            store._require_project_accepts_new_work(connection, thread["project_id"])
        return thread

    def _thread_holds_wake(
        self, connection: sqlite3.Connection, thread: sqlite3.Row, task: AgentTaskRecord
    ) -> bool:
        busy = connection.execute(
            "SELECT 1 FROM graph_runs WHERE json_extract(request_json, '$.chat_id')=? "
            "AND status IN ('queued','running','pausing') LIMIT 1",
            (thread["thread_id"],),
        ).fetchone()
        return busy is not None or cast("AppStore", self)._has_resumable_paused_chat_task(
            connection, task.project_id, task.kind, thread["thread_id"]
        )


def _notes_unchanged(connection: sqlite3.Connection, schedules: list[AgentSchedule]) -> bool:
    """Every observed note is still pending exactly as the wake message describes it."""
    for schedule in schedules:
        row = connection.execute(
            "SELECT * FROM agent_schedules WHERE schedule_id=?", (schedule.schedule_id,)
        ).fetchone()
        if (
            row is None
            or row["thread_id"] != schedules[0].thread_id
            or (row["pending_since"], row["next_fire_at"], row["state"])
            != (schedule.pending_since, schedule.next_fire_at, schedule.state)
        ):
            return False
    return True
