"""Durable private-thread schedules; a wake consumes its pending notes atomically."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta
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
    state: Literal["active", "paused", "cancelled", "disabled", "done"]
    disabled_reason: str | None = None
    next_fire_at: str | None = None
    pending_since: str | None = None
    last_fired_at: str | None = None
    created_at: str
    # Why a due wake could not start yet; it retries each poll and clears on launch.
    deferred_reason: str | None = None
    # A repeat is `cron` or `every_minutes`; null bounds mean unbounded.
    every_minutes: int | None = None
    start_local: str | None = None
    until_local: str | None = None
    fires_left: int | None = None


def local_instant(local: str, timezone: str) -> datetime:
    """A local `YYYY-MM-DDTHH:MM` in `timezone`, by consolidation's fold and gap rules."""
    from rcp.consolidation import occurrence_at

    parsed = datetime.fromisoformat(local)
    return occurrence_at(parsed.date(), parsed.strftime("%H:%M"), timezone)


def repeat_fire(
    *,
    cron: str | None,
    every_minutes: int | None,
    timezone: str,
    after: datetime,
    anchor: datetime | None,
) -> datetime:
    """The first repeat fire strictly after `after`, never before `anchor`.

    An interval keeps the phase of `anchor`, or starts one interval after `after`.
    """
    from rcp.consolidation import next_occurrence

    if every_minutes is not None:
        step = timedelta(minutes=every_minutes)
        if anchor is None:
            return after + step
        if anchor > after:
            return anchor
        return anchor + step * ((after - anchor) // step + 1)
    assert cron is not None
    if anchor is not None and anchor > after:
        # Cron matches whole minutes, so the anchor's own minute still matches.
        after = anchor - timedelta(seconds=1)
    return next_occurrence(cron, timezone, after)


def within_limits(schedule: AgentSchedule, fire: datetime, fires_left: int | None) -> bool:
    """Whether `fire` may still run under --count and --until."""
    if fires_left is not None and fires_left <= 0:
        return False
    return schedule.until_local is None or fire <= local_instant(
        schedule.until_local, schedule.timezone
    )


def resume_fire(schedule: AgentSchedule, now: datetime) -> datetime | None:
    """The next fire after `now`, skipping fires missed while off; None when none is left."""
    if schedule.at_local is not None:
        due = local_instant(schedule.at_local, schedule.timezone)
        return due if due > now else None
    if schedule.every_minutes is not None and schedule.next_fire_at is not None:
        anchor = datetime.fromisoformat(schedule.next_fire_at)
    elif schedule.start_local is not None:
        anchor = local_instant(schedule.start_local, schedule.timezone)
    else:
        anchor = None
    fire = repeat_fire(
        cron=schedule.cron,
        every_minutes=schedule.every_minutes,
        timezone=schedule.timezone,
        after=now,
        anchor=anchor,
    )
    return fire if within_limits(schedule, fire, schedule.fires_left) else None


def after_fire(schedule: AgentSchedule, now: datetime) -> tuple[str | None, int | None]:
    """The fire following a due one, and the fires left; None retires the schedule."""
    from rcp.consolidation import cron_occurrence_count

    if schedule.at_local is not None:
        return None, schedule.fires_left
    assert schedule.next_fire_at is not None
    due = datetime.fromisoformat(schedule.next_fire_at)
    following = repeat_fire(
        cron=schedule.cron,
        every_minutes=schedule.every_minutes,
        timezone=schedule.timezone,
        after=now,
        anchor=due if schedule.every_minutes is not None else None,
    )
    left = schedule.fires_left
    if left is not None:
        # Fires missed during downtime count; one combined wake reports them.
        if schedule.every_minutes is not None:
            fired = (following - due) // timedelta(minutes=schedule.every_minutes)
        else:
            assert schedule.cron is not None
            fired = cron_occurrence_count(schedule.cron, schedule.timezone, due, now)
        left = max(left - fired, 0)
    return (following.isoformat() if within_limits(schedule, following, left) else None), left


def migrate_agent_schedules(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS agent_schedules (
        schedule_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
        created_operation_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
        cron TEXT, at_local TEXT, timezone TEXT NOT NULL, note TEXT NOT NULL,
        state TEXT NOT NULL
            CHECK(state IN ('active','paused','cancelled','disabled','done')),
        disabled_reason TEXT, next_fire_at TEXT, pending_since TEXT,
        last_fired_at TEXT, created_at TEXT NOT NULL, deferred_reason TEXT,
        every_minutes INTEGER CHECK(every_minutes >= 1), start_local TEXT, until_local TEXT,
        fires_left INTEGER CHECK(fires_left >= 0),
        UNIQUE(created_operation_id, idempotency_key),
        CHECK ((cron IS NOT NULL) + (every_minutes IS NOT NULL) + (at_local IS NOT NULL) = 1)
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
        every_minutes: int | None = None,
        start: str | None = None,
        until: str | None = None,
        count: int | None = None,
    ) -> AgentSchedule:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM agent_schedules WHERE created_operation_id=? AND idempotency_key=?",
                (operation_id, key),
            ).fetchone()
            if existing is not None:
                record = AgentSchedule.model_validate(dict(existing))
                # The fires left change as it fires, so --count is not compared.
                if (
                    record.thread_id,
                    record.cron,
                    record.every_minutes,
                    record.at_local,
                    record.start_local,
                    record.until_local,
                    record.timezone,
                    record.note,
                ) != (thread_id, cron, every_minutes, at, start, until, timezone, note):
                    raise ValueError("schedule_key_conflict")
                return record
            active = connection.execute(
                "SELECT COUNT(*) FROM agent_schedules WHERE thread_id=? AND state='active'",
                (thread_id,),
            ).fetchone()[0]
            if active >= AGENT_SCHEDULES_MAX_ACTIVE:
                raise ValueError("schedule_limit")
            schedule = AgentSchedule(
                schedule_id=str(uuid.uuid4()),
                thread_id=thread_id,
                created_operation_id=operation_id,
                idempotency_key=key,
                cron=cron,
                every_minutes=every_minutes,
                at_local=at,
                start_local=start,
                until_local=until,
                fires_left=count,
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

    def pause_agent_schedule(self, thread_id: str, schedule_id: str) -> AgentSchedule:
        """A paused schedule keeps its next fire and limits; its waiting note is dropped."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT state FROM agent_schedules WHERE thread_id=? AND schedule_id=?",
                (thread_id, schedule_id),
            ).fetchone()
            if state is None:
                raise KeyError(schedule_id)
            if state[0] not in ("active", "paused"):
                raise ValueError("schedule_not_active")
            connection.execute(
                "UPDATE agent_schedules SET state='paused', pending_since=NULL, "
                "deferred_reason=NULL WHERE schedule_id=?",
                (schedule_id,),
            )
            row = connection.execute(
                "SELECT * FROM agent_schedules WHERE schedule_id=?", (schedule_id,)
            ).fetchone()
            return AgentSchedule.model_validate(dict(row))

    def reactivate_agent_schedule(
        self,
        thread_id: str,
        schedule_id: str,
        next_fire_at: str | None,
        *,
        was: Literal["disabled", "paused"],
    ) -> AgentSchedule:
        """Switch a disabled or paused schedule back on; with no fire left it is done."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                "SELECT COUNT(*) FROM agent_schedules WHERE thread_id=? AND state='active'",
                (thread_id,),
            ).fetchone()[0]
            if next_fire_at is not None and count >= AGENT_SCHEDULES_MAX_ACTIVE:
                raise ValueError("schedule_limit")
            changed = connection.execute(
                "UPDATE agent_schedules SET state=?, disabled_reason=NULL, next_fire_at=?, "
                "pending_since=NULL, deferred_reason=NULL "
                "WHERE thread_id=? AND schedule_id=? AND state=?",
                (
                    "done" if next_fire_at is None else "active",
                    next_fire_at,
                    thread_id,
                    schedule_id,
                    was,
                ),
            ).rowcount
            if not changed:
                raise ValueError(f"schedule_not_{was}")
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
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM agent_schedules WHERE state='active' AND next_fire_at<=?",
                (now.isoformat(),),
            ).fetchall()
            for row in rows:
                schedule = AgentSchedule.model_validate(dict(row))
                following, fires_left = after_fire(schedule, now)
                connection.execute(
                    "UPDATE agent_schedules SET pending_since=COALESCE(pending_since, ?), "
                    "next_fire_at=?, fires_left=?, state=? WHERE schedule_id=?",
                    (
                        schedule.next_fire_at,
                        following,
                        fires_left,
                        "active" if following else "done",
                        schedule.schedule_id,
                    ),
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
