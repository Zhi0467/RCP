"""The private agent schedule machine.

A world is one ``AppStore`` over the explorer's tree with the real
``BackgroundAgentTasks`` and ``ConsolidationPoller`` on it; no app. ``start``
runs ``build_schedule_store`` (the builder of ``tests/test_agent_schedules.py``)
and has the member's turn add one schedule, ``M``: every minute, two fires.

``boot`` applies these instance patches: ``store.now`` reads the explorer clock;
``admit_provider_task`` admits, as the schedule tests' autouse fixture does;
``_schedule_remote_reconciliation`` does nothing; ``launch_admitted`` validates
the queued request as ``tests.helpers.fake_launch_admitted`` does and then
records a worker without starting a thread. ``seams`` sets the consolidation
module's ``seconds_until_automatic_launch`` to zero, so a tick never cools down.

A tick moves the clock and runs ``poll_once``, which advances due schedules and
dispatches wakes as production does. A worker's turn runs only as ``settle``.
A restart runs ``recover_at_startup``, the startup call ``api/app.py`` makes for
queued wakes; the next tick relaunches them. Restart is offered only while a
worker exists, since otherwise it only moves the clock a second.

Views name turns by lineage: ``W0``, ``W1`` for schedule wakes in creation
order. Times are seconds from now. ``fence`` writes the source transfer row a
project move leaves while it is under way, as
``test_schedule_wake_rechecks_transfer_fence`` does; driving a real transfer is
the transfer catalog entry's job.

Bound 3 runs within the budget only without the member's Cancel, Enable and
composer sends, which no rule reads: the waiting wake keeps the thread busy, so
a send is not needed to hold a note. The schedule tests cover Cancel and Enable.
A schedule with no next fire is offered only the one-minute tick; longer ticks
reach the same rows and differ only in the age of a waiting note.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple, cast
from unittest import mock

from rcp.consolidation import ConsolidationPoller
from rcp.limits import AGENT_SCHEDULE_MIN_GAP_MINUTES, AGENT_SCHEDULES_MAX_ACTIVE
from rcp.runs.agent_schedules import _dispatch_thread
from rcp.server_ops.members import _remove_member_agent_data
from rcp.storage import AppStore
from rcp.storage.agent_schedules import (
    AgentSchedule,
    AgentSchedulesStoreMixin,
    migrate_agent_schedule_pending_fires,
)
from rcp.storage.agent_threads import AgentThreadStoreMixin
from rcp.storage.projects import ProjectStoreMixin
from rcp.storage.restore_detachment import RestoreDetachmentStoreMixin
from tests.lifecycles.engine import RESTART, Action, Clock, Memory, Safety, Step

THREAD = "thread"
PROJECT = "project"
ORIGIN = "origin"
COUNT = 2
_TICKS = (1, 15, 60)
_ACTIVE_TURN = frozenset({"queued", "running", "pausing"})
_ScheduleState = AgentSchedule.model_fields["state"].annotation


def build_schedule_store(path: Path, now: Callable[[], str] | None = None) -> AppStore:
    """A store with one project, its owner, a succeeded member turn and their private thread."""

    from rcp.core.models import AuthorizedHuman
    from rcp.service import RunRequest, resolve_dispatch_authority
    from tests.test_storage import _project, _task_record

    store = AppStore(path)
    if now is not None:
        store.now = now
    store.upsert_project(_project(PROJECT))
    owner = store.local_owner
    assert owner is not None
    store.seat_project_member(PROJECT, owner.user_id, seated_by=owner.user_id)
    request = RunRequest(chat_id=THREAD, chat_scope="project", mode="work", message="Check daily")
    task = _task_record(
        store.now(),
        operation_id=ORIGIN,
        kind="project_chat",
        status="succeeded",
        request=request.model_dump(mode="json"),
        authorized_by=AuthorizedHuman(
            space_id=store.space_id, user_id=owner.user_id, display_name="Test Member"
        ),
        dispatch_authority=resolve_dispatch_authority("project_chat", request),
    )
    store.create_agent_task(task)
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO agent_threads (thread_id,user_id,kind,project_id,created_at) VALUES (?,?,'project','project',?)",
            (THREAD, owner.user_id, store.now()),
        )
    return store


def schedule_command(key: str, **arguments):
    """A keyed ``schedule`` command as the agent's CLI sends it."""

    from rcp.agents.command_protocol import ScheduleCommandRequest

    return ScheduleCommandRequest.model_validate(
        dict(
            mailbox_id="b" * 32,
            credential="c" * 64,
            request_id="a" * 32,
            verb="schedule",
            idempotency_key=key,
            arguments=arguments,
        )
    )


class View(NamedTuple):
    state: str
    schedule: tuple
    turns: tuple
    closed: bool
    fenced: bool
    restored: bool


@dataclass
class World:
    """One store, its engine and its poller, plus the workers the engine holds."""

    store: AppStore
    tasks: Any
    poller: ConsolidationPoller
    clock: Clock
    workers: set[str] = field(default_factory=set)
    last_action: str | None = None

    def wakes(self) -> dict[str, Any]:
        """Each schedule wake by lineage name."""

        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT operation_id FROM graph_runs "
                "WHERE json_extract(request_json, '$.trigger')='schedule' ORDER BY created_at"
            ).fetchall()
        return {f"W{index}": self.store.agent_task(row[0]) for index, row in enumerate(rows)}

    def names(self) -> dict[str, str]:
        return {task.operation_id: name for name, task in self.wakes().items()}

    def schedule(self) -> AgentSchedule:
        """The member's schedule ``M``; a wake's refused add never makes another."""

        return next(row for row in self.store.agent_schedules(THREAD) if row.note == "M")

    def thread_closed(self) -> bool:
        record = self.store.agent_thread(THREAD)
        return record is None or record.closed_at is not None

    def notes(self) -> list[dict]:
        """Every note every wake carried, parsed from its message."""

        return [
            note
            for task in self.wakes().values()
            for note in json.loads(task.request["message"].split("\n", 1)[1])
        ]


class Schedules:
    name = "schedules"
    bound = 3
    horizon = 2
    covers = {_ScheduleState: ("state", None)}
    tables = frozenset({"agent_schedules"})
    writers = (
        AgentSchedulesStoreMixin.add_agent_schedule,
        AgentSchedulesStoreMixin.cancel_agent_schedule,
        AgentSchedulesStoreMixin.pause_agent_schedule,
        AgentSchedulesStoreMixin.reactivate_agent_schedule,
        AgentSchedulesStoreMixin.defer_agent_schedules,
        AgentSchedulesStoreMixin.advance_agent_schedules,
        AgentSchedulesStoreMixin.claim_agent_schedule_wake,
        ConsolidationPoller._advance_agent_schedules,
        _dispatch_thread,
        AgentThreadStoreMixin.close_agent_thread,
        RestoreDetachmentStoreMixin.detach_restored_lifecycle,
        _remove_member_agent_data,
        ProjectStoreMixin.delete_project_records,
    )
    unreached_values = {"cancelled": "Cancel is not modeled, as the module docstring says"}
    unreached_writers = {
        AgentSchedulesStoreMixin.add_agent_schedule: (
            "start adds the one schedule; a wake's every-minute add is refused before the store"
        ),
        AgentSchedulesStoreMixin.cancel_agent_schedule: (
            "Cancel is not modeled, as the module docstring says"
        ),
        migrate_agent_schedule_pending_fires: (
            "a migration runs when a store opens, never inside an action"
        ),
        _remove_member_agent_data: (
            "member removal deletes the member's schedules in one statement; no state follows"
        ),
        ProjectStoreMixin.delete_project_records: (
            "project deletion deletes the thread's schedules; the deletion catalog entry owns it"
        ),
    }

    def __init__(self) -> None:
        self.clock = Clock()

    @contextmanager
    def seams(self) -> Iterator[None]:
        with mock.patch("rcp.consolidation.seconds_until_automatic_launch", lambda: 0):
            yield

    def databases(self, root: Path) -> list[Path]:
        return [root / "state.sqlite3"]

    def start(self, root: Path, clock: Clock) -> World:
        from rcp.runs.agent_schedules import handle_schedule

        store = build_schedule_store(root / "state.sqlite3", clock.iso)
        added = handle_schedule(
            store,
            ORIGIN,
            schedule_command("M", action="add", every="1m", count=COUNT, timezone="UTC", note="M"),
        )
        assert added.status == "ok", added.message
        store.close()
        return self.boot(root, clock)

    def boot(self, root: Path, clock: Clock) -> World:
        from rcp.background import BackgroundAgentTasks

        self.clock = clock
        store = AppStore(root / "state.sqlite3")
        store.now = clock.iso

        async def stream(*_args):
            raise AssertionError("a schedule wake never starts a provider here")
            yield ""

        tasks = BackgroundAgentTasks(store, stream)
        poller = ConsolidationPoller(store, tasks, service_for=_no_service, clock=clock.iso)
        world = World(store, tasks, poller, clock)
        tasks.admit_provider_task = lambda *args, **kwargs: None
        tasks._schedule_remote_reconciliation = lambda **kwargs: None

        def launch_admitted(operation_id: str):
            record = store.agent_task(operation_id)
            if record is not None and record.status == "queued":
                tasks._launchable_request(record)
                world.workers.add(operation_id)
            return store.agent_task(operation_id)

        tasks.launch_admitted = launch_admitted
        return world

    def close(self, world: World) -> None:
        world.store.close()

    def save_memory(self, world: World) -> Memory:
        names = world.names()
        return Memory(frozenset(names[op] for op in world.workers), None)

    def restore_memory(self, world: World, memory: Memory) -> None:
        if memory.engine is None:
            return
        operation = {name: op for op, name in world.names().items()}
        world.workers = {operation[name] for name in memory.engine}

    def actions(self, view: View) -> list[Action]:
        state, schedule, turns, closed, fenced, restored = view
        ticks = _TICKS if schedule[1] is not None else _TICKS[:1]
        found = [Action("tick", (str(minutes),)) for minutes in ticks]
        for name, status, worker in turns:
            if worker and status == "queued":
                found.append(Action("settle", (name,)))
                found.append(Action("add", (name,), environment=False))
        if not closed:
            if state == "active":
                found.append(Action("pause", environment=False))
            if state == "paused":
                found.append(Action("resume", environment=False))
            found.append(Action("close"))
        if not fenced and state == "active":
            found.append(Action("fence"))
        if not restored and not closed:
            found.append(Action("restore"))
        if any(worker for _name, _status, worker in turns):
            found.append(Action(RESTART))
        return found

    def apply(self, world: World, action: Action) -> None:
        store = world.store
        world.last_action = action.name
        if action.name == "tick":
            world.clock.advance(int(action.args[0]) * 60)
            world.poller.poll_once()
        elif action.name == "settle":
            _settle(world, world.wakes()[action.args[0]].operation_id)
        elif action.name == "add":
            _add_from_wake(world, world.wakes()[action.args[0]].operation_id)
        elif action.name in {"pause", "resume"}:
            _member_control(world, action.name)
        elif action.name == "close":
            store.close_agent_thread(THREAD, reason="access_lost")
        elif action.name == "fence":
            _fence(store)
        elif action.name == "restore":
            store.detach_restored_lifecycle(diagnostic="explorer copy", confirmed_by="explorer")
        else:
            raise ValueError(f"unknown action {action}")

    def restart(self, world: World) -> None:
        world.tasks.recover_at_startup()

    def view(self, world: World) -> View:
        schedule = world.schedule()
        now = world.clock.now
        return View(
            schedule.state,
            (
                schedule.disabled_reason,
                _seconds(schedule.next_fire_at, now),
                _seconds(schedule.pending_since, now),
                schedule.fires_left,
                schedule.deferred_reason is not None,
            ),
            tuple(
                (name, task.status, task.operation_id in world.workers)
                for name, task in world.wakes().items()
            ),
            world.thread_closed(),
            _fenced(world.store),
            _restored(world.store),
        )

    @property
    def rules(self):
        return RULES


def _no_service(project_id: str):
    raise AssertionError(f"consolidation is not part of the schedule machine: {project_id}")


def _seconds(value: str | None, now: datetime) -> int | None:
    return None if value is None else int((datetime.fromisoformat(value) - now).total_seconds())


def _settle(world: World, operation_id: str) -> None:
    """The worker's wake runs and succeeds, then the worker leaves."""

    world.store.mark_agent_task_running(operation_id)
    world.store.complete_agent_task(
        operation_id,
        applied_revision=None,
        result={"messages": ["Done"], "turn_outcome": {"status": "done"}},
    )
    world.workers.discard(operation_id)


def _add_from_wake(world: World, operation_id: str) -> None:
    """The wake's agent asks for a schedule more often than a wake may."""

    from rcp.runs.agent_schedules import handle_schedule

    handle_schedule(
        world.store,
        operation_id,
        schedule_command("often", action="add", every="1m", timezone="UTC", note="Often"),
    )


def _member_control(world: World, verb: str) -> None:
    """The member's pause or resume route."""

    from rcp.api import agent_schedules as routes

    store = world.store
    owner = store.local_owner
    assert owner is not None
    identity = SimpleNamespace(acting_user=lambda _: owner)
    identity.require_patch_capable_identity = identity.acting_user
    route = {"pause": routes.pause_schedule, "resume": routes.resume_schedule_route}[verb]
    route(
        THREAD,
        world.schedule().schedule_id,
        cast(Any, None),
        store=store,
        identity_access=cast(Any, identity),
    )


def _fence(store: AppStore) -> None:
    now = store.now()
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO project_transfer_requests (request_id,side,phase,project_id,"
            "source_space_id,target_space_id,record_json,revision,created_at,updated_at) "
            "VALUES ('fenced','source','source_fenced',?,'source','target','{}',0,?,?)",
            (PROJECT, now, now),
        )


def _fenced(store: AppStore) -> bool:
    with store.connection() as connection:
        row = connection.execute("SELECT 1 FROM project_transfer_requests LIMIT 1").fetchone()
    return row is not None


def _restored(store: AppStore) -> bool:
    with store.connection() as connection:
        row = connection.execute(
            "SELECT 1 FROM graph_runs WHERE operation_id=? AND history_only=1", (ORIGIN,)
        ).fetchone()
    return row is not None


def _active(view: View) -> bool:
    return any(status in _ACTIVE_TURN for _name, status, _worker in view.turns)


def _new_wake(before: View, world: World) -> bool:
    return len(world.wakes()) > len(before.turns)


def _always(*_args) -> bool:
    return True


def _has_wakes(world: World) -> bool:
    return bool(world.wakes())


def _ticked(before: View, world: World) -> bool:
    return world.last_action == "tick"


def _ticked_with_a_note(before: View, world: World) -> bool:
    return _ticked(before, world) and before.schedule[2] is not None


def _restoring(before: View, world: World) -> bool:
    return world.last_action == "restore"


def paused_and_disabled_hold_no_note(world: World) -> list[str]:
    """A paused or disabled schedule holds no waiting note."""

    schedule = world.schedule()
    if schedule.state in {"paused", "disabled"} and schedule.pending_since is not None:
        return [f"{schedule.state} schedule keeps a pending note"]
    return []


def disabled_holds_no_next_fire(world: World) -> list[str]:
    """A disabled schedule holds no next fire, so Enable picks one from now."""

    schedule = world.schedule()
    if schedule.state == "disabled" and schedule.next_fire_at is not None:
        return [f"schedule disabled for {schedule.disabled_reason} keeps its next fire"]
    return []


def one_active_wake_per_thread(world: World) -> list[str]:
    """At most one wake of the thread is queued, running or pausing."""

    active = [name for name, task in world.wakes().items() if task.status in _ACTIVE_TURN]
    return [f"{active} are active together"] if len(active) > 1 else []


def closed_thread_holds_no_active_schedule(world: World) -> list[str]:
    """A closed thread has no active schedule."""

    if world.thread_closed() and world.schedule().state == "active":
        return ["active schedule on a closed thread"]
    return []


def at_most_twenty_active(world: World) -> list[str]:
    """A thread holds at most AGENT_SCHEDULES_MAX_ACTIVE active schedules."""

    active = sum(row.state == "active" for row in world.store.agent_schedules(THREAD))
    return [f"{active} active schedules"] if active > AGENT_SCHEDULES_MAX_ACTIVE else []


def fires_are_reported_within_count(world: World) -> list[str]:
    """Missed fires count against --count, so wakes report no more fires than it granted."""

    schedule = world.schedule()
    assert schedule.fires_left is not None
    reported = sum(note["missed_fires"] + 1 for note in world.notes())
    if reported > COUNT - schedule.fires_left:
        return [f"wakes report {reported} fires of {COUNT - schedule.fires_left} fired"]
    return []


def wakes_are_captured_work_turns(world: World) -> list[str]:
    """Every schedule wake is a Work turn on the thread, authorized by its member."""

    owner = world.store.local_owner
    assert owner is not None
    return [
        f"{name} is not a captured Work turn of the member"
        for name, task in world.wakes().items()
        if task.request.get("mode") != "work"
        or task.request.get("chat_id") != THREAD
        or task.authorized_by is None
        or task.authorized_by.user_id != owner.user_id
        or task.dispatch_authority is None
    ]


def closed_thread_fails_queued_wakes(before: View, world: World) -> list[str]:
    """A tick settles a claimed wake whose thread closed before launch as failed."""

    if not world.thread_closed():
        return []
    return [
        f"{name} stays queued on a closed thread"
        for name, task in world.wakes().items()
        if task.status == "queued"
    ]


def wake_claims_its_notes_atomically(before: View, world: World) -> list[str]:
    """A new wake consumes the waiting note, and never joins an active turn or a paused schedule."""

    found = []
    if world.schedule().pending_since is not None:
        found.append("a note stays pending after the wake that claimed it")
    if _active(before):
        found.append("a wake was claimed while a thread turn was active")
    if before.state == "paused":
        found.append("a paused schedule fired")
    return found


def wake_adds_keep_the_gap(before: View, world: World) -> list[str]:
    """A wake's schedule fires at least AGENT_SCHEDULE_MIN_GAP_MINUTES apart."""

    wakes = {task.operation_id for task in world.wakes().values()}
    return [
        f"a wake added a schedule every {row.every_minutes} minutes"
        for row in world.store.agent_schedules(THREAD)
        if row.created_operation_id in wakes
        and row.every_minutes is not None
        and row.every_minutes < AGENT_SCHEDULE_MIN_GAP_MINUTES
    ]


def waiting_note_says_why(before: View, world: World) -> list[str]:
    """A tick claims a waiting note, keeps it for an active turn, or records why it was deferred.

    Losing access disables the schedule and drops the note instead.
    """

    schedule = world.schedule()
    if (
        schedule.pending_since is None
        or schedule.state not in {"active", "done"}
        or world.thread_closed()
        or _active(before)
        or _new_wake(before, world)
    ):
        return []
    if schedule.deferred_reason is None:
        return ["a waiting note was neither claimed nor deferred with a reason"]
    return []


def tick_keeps_or_claims_the_note(before: View, world: World) -> list[str]:
    """A tick never drops a waiting note without a wake, unless access is lost."""

    schedule = world.schedule()
    if (
        schedule.pending_since is None
        and not _new_wake(before, world)
        and schedule.state != "disabled"
    ):
        return ["a tick dropped a waiting note without a wake"]
    return []


def restore_disables_and_clears_notes(before: View, world: World) -> list[str]:
    """Restore leaves no schedule active and no wake pending."""

    found = []
    for row in world.store.agent_schedules(THREAD):
        if row.state == "active":
            found.append("an active schedule survives restore")
        if row.pending_since is not None:
            found.append("a pending note survives restore")
    return found


_SPEC = "consolidation spec: Private agent schedules"

RULES = (
    Safety(paused_and_disabled_hold_no_note, _always, _SPEC),
    Safety(disabled_holds_no_next_fire, _always, "code guard at _dispatch_thread"),
    Safety(one_active_wake_per_thread, _always, _SPEC),
    Safety(closed_thread_holds_no_active_schedule, _always, _SPEC),
    Safety(at_most_twenty_active, _always, _SPEC),
    Safety(fires_are_reported_within_count, _has_wakes, _SPEC),
    Safety(wakes_are_captured_work_turns, _has_wakes, "invariant 10b"),
    Step(closed_thread_fails_queued_wakes, _ticked, _SPEC),
    Step(wake_claims_its_notes_atomically, _new_wake, _SPEC),
    Step(wake_adds_keep_the_gap, _always, _SPEC),
    Step(waiting_note_says_why, _ticked, _SPEC),
    Step(tick_keeps_or_claims_the_note, _ticked_with_a_note, _SPEC),
    Step(restore_disables_and_clears_notes, _restoring, _SPEC),
)
