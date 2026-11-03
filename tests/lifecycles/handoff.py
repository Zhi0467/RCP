"""The handoff and transport-retry machine.

A world is one real backend over the explorer's tree: the store, the engine and
``dispatch_thread_handoffs`` run as in production. ``boot`` replaces only the
seams ``tests/test_agent_thread_commands.py`` replaces: ``store.now`` reads the
explorer clock, admission and remote reconciliation do nothing, ``_spawn_record``
registers a worker without starting a thread, and ``_schedule_transport_retry``
records a timer instead of sleeping. A worker's two halves are separate
actions: the turn's verdict commits (``settle``), then ``_task_settled`` decides
the automatic retry and the worker leaves (``finish``). A timer fires only as
an action, advancing the clock by its delay. Every action is followed by a
dispatch.

Views name each turn by lineage: ``S0`` for the source Space turn, ``T0`` for
the delivered project turn and ``W0`` for the Space wake, with ``T1`` and ``W1``
for their continuations.

The delivered turn carries one attachment that expires 30 minutes after the
start instant. The handoff builder has no attachment path, so ``start`` writes
the descriptor into the delivered turn's request, standing in for a member's
upload. ``expire`` moves the clock past it, after which the turn can no longer
be retried and a promised transport reattempt stands down. Expiry changes only
what a final turn offers, so ``expire`` is offered only once the newest
delivered turn is final; expiring earlier reaches the same states. ``seams`` points the
``datetime`` that ``rcp.storage.rows`` reads at the explorer clock.

The goal counts restart as allowed: recovering a crashed worker is restart's
job, and a handoff whose worker died must settle once the backend comes back.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from rcp.limits import AGENT_TRANSPORT_RETRY_BACKOFF_SECONDS
from tests.lifecycles.engine import RESTART, START, Action, Clock, Goal, Memory, Safety, Step

_TERMINAL = frozenset({"succeeded", "failed", "interrupted", "paused"})
_SETTLE_OUTCOMES = ("succeeded", "transport_lost", "other")
_SOURCE = "source"
_EXPIRY = START + timedelta(minutes=30)


@dataclass
class World:
    """One backend over the tree, plus the timers its engine armed."""

    app: Any
    clock: Clock
    names: Names
    timers: list[tuple[str, int, float]] = field(default_factory=list)
    settled_on: str | None = None

    @property
    def background(self):
        return self.app.state.background_tasks

    @property
    def store(self):
        return self.background.store

    def handoff(self):
        handoff = self.store.agent_handoff(self.names.handoff_id)
        assert handoff is not None
        return handoff

    def lineage(self) -> dict[str, str]:
        """Each operation id's positional name."""

        handoff = self.handoff()
        roots = {
            _SOURCE: "S",
            handoff.target_operation_id: "T",
            handoff.notified_operation_id: "W",
        }
        with self.store.connection() as connection:
            rows = connection.execute(
                "SELECT operation_id,parent_operation_id FROM graph_runs"
            ).fetchall()
        parents = {row[0]: row[1] for row in rows}
        names: dict[str, str] = {}
        for operation_id in parents:
            depth, cursor = 0, operation_id
            while cursor not in roots and parents.get(cursor) is not None:
                depth, cursor = depth + 1, parents[cursor]
            if cursor is None or cursor not in roots:
                raise AssertionError(f"task {operation_id} is outside the handoff's lineage")
            names[operation_id] = f"{roots[cursor]}{depth}"
        return names

    def newest_delivered(self):
        """The delivered turn's newest continuation, the one settlement evaluates."""

        names = self.lineage()
        delivered = [op for op, name in names.items() if name.startswith("T")]
        if not delivered:
            return None
        return self.store.agent_task(max(delivered, key=lambda op: int(names[op][1:])))

    def space_busy(self) -> bool:
        from rcp.runs.agent_thread_commands import _thread_busy

        thread = self.store.agent_thread(self.names.space_thread_id)
        return thread is not None and _thread_busy(self.store, thread)

    def retry_pending(self, operation_id: str) -> bool:
        """The engine still holds a worker that has not decided its retry."""

        background = self.background
        return operation_id in background._workers and operation_id not in background._retry_decided


@dataclass(frozen=True)
class Names:
    """The ids ``start`` seeded."""

    handoff_id: str
    space_thread_id: str
    member: Any


class Handoff:
    name = "handoff"
    bound = 4
    horizon = 3

    def __init__(self) -> None:
        self.names: Names | None = None
        self.manifest_path: Path | None = None
        self.clock = Clock()

    @contextmanager
    def seams(self) -> Iterator[None]:
        machine = self

        class ExplorerDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return machine.clock.now.astimezone(tz)

        with (
            mock.patch("rcp.background.seconds_until_automatic_launch", lambda: 0),
            mock.patch("rcp.storage.rows.datetime", ExplorerDatetime),
        ):
            yield

    def databases(self, root: Path) -> list[Path]:
        return [root / "data" / "rcp.sqlite3"]

    def start(self, root: Path, clock: Clock) -> World:
        from rcp.service import RunRequest
        from tests.helpers import authorized_human, create_named_app, write_test_manifest
        from tests.test_storage import _task_record

        manifest = write_test_manifest(root)
        app = create_named_app(str(manifest.path), data_dir=root / "data")
        store = app.state.background_tasks.store
        store.now = clock.iso
        actor = authorized_human(store)
        project_id = app.state.default_project_id
        threads = store.ensure_agent_threads(actor.user_id, [project_id], default_names=("Pip",))
        source = next(row for row in threads if row.kind == "space")
        target = next(row for row in threads if row.kind == "project")
        for thread in (source, target):
            store.set_agent_thread_pick(
                thread.thread_id, provider="claude", model=None, reasoning=None, run_on=None
            )
        store.create_agent_task(
            _task_record(
                store.now(),
                operation_id=_SOURCE,
                project_id=store.space_id,
                kind="space_chat",
                status="succeeded",
                authorized_by=actor,
                request=RunRequest(
                    provider="claude",
                    chat_scope="project",
                    chat_id=source.thread_id,
                    mode="work",
                    message="Delegate",
                ).model_dump(mode="json"),
            )
        )
        handoff = store.create_agent_handoff(
            source_thread_id=source.thread_id,
            source_operation_id=_SOURCE,
            target_thread_id=target.thread_id,
            idempotency_key="explorer",
            message="Please check",
        )
        store.close()
        self.names = Names(handoff.handoff_id, source.thread_id, actor)
        self.manifest_path = manifest.path
        world = self.boot(root, clock)
        self._dispatch(world)
        delivered = world.handoff().target_operation_id
        attachment = {
            "attachment_id": "upload",
            "name": "notes.txt",
            "media_type": "text/plain",
            "size": 5,
            "expires_at": _EXPIRY.isoformat(),
        }
        with world.store.connection() as connection:
            connection.execute(
                "UPDATE graph_runs SET request_json = json_set(request_json, '$.attachments', "
                "json(?)) WHERE operation_id = ?",
                (json.dumps([attachment]), delivered),
            )
        return world

    def boot(self, root: Path, clock: Clock) -> World:
        from tests.helpers import create_named_app

        assert self.names is not None and self.manifest_path is not None
        app = create_named_app(str(self.manifest_path), data_dir=root / "data")
        world = World(app, clock, self.names)
        self.clock = clock
        background = world.background
        store = background.store
        store.now = clock.iso
        background.admit_provider_task = lambda *args, **kwargs: None
        background._schedule_remote_reconciliation = lambda **kwargs: None
        background.on_task_settled = None

        def spawn(record, request, **kwargs):
            current = store.agent_task(record.operation_id) or record
            if current.status == "queued":
                with background._controls_lock:
                    background._workers[current.operation_id] = threading.current_thread()
            return current

        def schedule(operation_id: str, *, attempt: int, delay: float | None = None) -> None:
            wait = AGENT_TRANSPORT_RETRY_BACKOFF_SECONDS[attempt] if delay is None else delay
            world.timers.append((operation_id, attempt, wait))

        background._spawn_record = spawn
        background._schedule_transport_retry = schedule
        return world

    def close(self, world: World) -> None:
        world.store.close()

    def save_memory(self, world: World) -> Memory:
        names = world.lineage()
        background = world.background
        engine = (
            tuple(sorted((names[op], attempt, delay) for op, attempt, delay in world.timers)),
            frozenset(names[op] for op in background._workers),
            frozenset(names[op] for op in background._retry_decided),
        )
        return Memory(engine, world.settled_on)

    def restore_memory(self, world: World, memory: Memory) -> None:
        world.settled_on = memory.environment
        if memory.engine is None:
            return
        timers, workers, decided = memory.engine
        operation = {name: op for op, name in world.lineage().items()}
        world.timers = [(operation[name], attempt, delay) for name, attempt, delay in timers]
        background = world.background
        with background._controls_lock:
            for name in workers:
                background._workers[operation[name]] = threading.current_thread()
            background._retry_decided.update(operation[name] for name in decided)

    def actions(self, view) -> list[Action]:
        _handoff, turns, _busy, timers, expired = view
        found: list[Action] = []
        for name, status, _kind, _retry, _resume, _receipts, (worker, _decided) in turns:
            if worker and status in {"queued", "running"}:
                found.extend(Action("settle", (name, outcome)) for outcome in _SETTLE_OUTCOMES)
            elif worker and status in _TERMINAL:
                found.append(Action("finish", (name,)))
        found.extend(
            dict.fromkeys(Action("fire", (name, str(attempt))) for name, attempt, _ in timers)
        )
        newest = _newest_delivered(view)
        if newest is not None and newest[3]:
            found.append(Action("retry", (newest[0],), environment=False))
        if not expired and newest is not None and newest[1] in _TERMINAL:
            found.append(Action("expire"))
        found.append(Action(RESTART))
        return found

    def apply(self, world: World, action: Action) -> None:
        names = world.lineage()
        operation = {name: op for op, name in names.items()}
        if action.name == "settle":
            _settle(world, operation[action.args[0]], action.args[1])
        elif action.name == "finish":
            _finish(world, operation[action.args[0]])
        elif action.name == "fire":
            timer = next(
                timer for timer in world.timers if (names[timer[0]], str(timer[1])) == action.args
            )
            world.timers.remove(timer)
            world.clock.advance(timer[2])
            world.background._run_transport_retry(timer[0], attempt=timer[1])
        elif action.name == "expire":
            world.clock.advance((_EXPIRY - world.clock.now).total_seconds() + 1)
        elif action.name == "retry":
            world.background.retry(operation[action.args[0]], authorized_by=world.names.member)
        else:
            raise ValueError(f"unknown action {action}")
        self._dispatch(world)

    def restart(self, world: World) -> None:
        world.background.recover_at_startup()
        self._dispatch(world)

    def _dispatch(self, world: World) -> None:
        from rcp.runs.agent_thread_commands import dispatch_thread_handoffs

        dispatch_thread_handoffs(
            world.store,
            world.app.state.catalog,
            world.background,
            project_admission=lambda _: nullcontext(),
        )
        if world.settled_on is None and world.handoff().settled_at is not None:
            newest = world.newest_delivered()
            world.settled_on = world.lineage()[newest.operation_id] if newest else ""

    def view(self, world: World):
        handoff = world.handoff()
        names = world.lineage()
        background = world.background
        turns = []
        for operation_id, name in sorted(names.items(), key=lambda item: item[1]):
            task = world.store.agent_task(operation_id)
            assert task is not None
            receipts = tuple(
                sorted(
                    receipt.category
                    for receipt in world.store.agent_task_receipts(operation_id)
                    if receipt.category.startswith("transport_auto_retry")
                )
            )
            worker = (
                operation_id in background._workers,
                operation_id in background._retry_decided,
            )
            turns.append(
                (
                    name,
                    task.status,
                    task.failure_kind,
                    task.can_retry,
                    task.can_resume,
                    receipts,
                    worker,
                )
            )
        timers = tuple(sorted((names[op], attempt, delay) for op, attempt, delay in world.timers))
        return (
            (
                handoff.target_operation_id is not None,
                handoff.settled_at is not None,
                handoff.notified_operation_id is not None,
                handoff.refusal_reason,
                world.settled_on,
            ),
            tuple(turns),
            world.space_busy(),
            timers,
            world.clock.now >= _EXPIRY,
        )

    @property
    def rules(self):
        return RULES


def _settle(world: World, operation_id: str, outcome: str) -> None:
    store = world.store
    task = store.agent_task(operation_id)
    if task is not None and task.status == "queued":
        store.mark_agent_task_running(operation_id)
    if outcome == "succeeded":
        store.complete_agent_task(
            operation_id,
            applied_revision=None,
            result={"messages": ["Checked"], "turn_outcome": {"status": "done"}},
        )
    else:
        lost = outcome == "transport_lost"
        store.fail_agent_task(
            operation_id,
            "link lost" if lost else "provider error",
            failure_kind="transport_lost" if lost else None,
        )


def _finish(world: World, operation_id: str) -> None:
    from rcp.agents import AgentProcessControl
    from rcp.background import AgentTaskExecution

    background = world.background
    record = background.store.agent_task(operation_id)
    assert record is not None
    execution = AgentTaskExecution(
        operation_id=operation_id, store=background.store, control=AgentProcessControl()
    )
    background._task_settled(record, background._request_from_record(record), execution)
    background._forget_control(operation_id)


def _newest_delivered(view):
    delivered = [turn for turn in view[1] if turn[0].startswith("T")]
    return max(delivered, key=lambda turn: int(turn[0][1:])) if delivered else None


def _always(*_args) -> bool:
    return True


def _has_delivered(world: World) -> bool:
    return world.newest_delivered() is not None


def owed_retries_are_found(world: World) -> list[str]:
    """Every task owed a transport retry is one the restart query returns."""

    store, background = world.store, world.background
    owed = set(store.owed_transport_retry_operation_ids())
    return [
        f"{name} is owed a retry the restart query misses"
        for operation_id, name in world.lineage().items()
        if (task := store.agent_task(operation_id)) is not None
        and background.transport_retry_owed(task)
        and operation_id not in owed
    ]


def pending_retry_blocks_settlement(world: World) -> list[str]:
    """A delivered turn whose armed retry will still run neither reads settled nor settles the handoff.

    A timer whose turn can no longer be retried (its attachments expired) stands
    down when it fires, so it does not hold the handoff open.
    """

    from rcp.runs.agent_thread_commands import _handoff_turn_settled, _newest_continuation

    newest = world.newest_delivered()
    assert newest is not None
    name = world.lineage()[newest.operation_id]
    if not _retry_coming(world, newest):
        return []
    found = []
    with world.store.connection() as connection:
        row = _newest_continuation(connection, newest.operation_id)
    if _handoff_turn_settled(world.store, world.background, row):
        found.append(f"{name} reads settled with a retry timer pending")
    if world.settled_on == name:
        found.append(f"handoff settled on {name} while its retry is pending")
    return found


def _retry_coming(world: World, task) -> bool:
    """An armed timer for ``task`` whose reattempt is still owed."""

    timed = any(op == task.operation_id for op, _, _ in world.timers)
    return timed and world.background.transport_retry_owed(task)


def final_turn_settles_handoff(world: World) -> list[str]:
    """Once the delivered turn is final with no retry coming, the handoff is settled."""

    newest = world.newest_delivered()
    assert newest is not None
    if (
        world.handoff().settled_at is None
        and newest.status in _TERMINAL
        and not _retry_coming(world, newest)
        and not world.retry_pending(newest.operation_id)
    ):
        return [f"handoff unsettled with {world.lineage()[newest.operation_id]} final"]
    return []


def one_delivery_and_one_wake(world: World) -> list[str]:
    """A handoff delivers at most one turn and wakes the Space at most once."""

    with world.store.connection() as connection:
        roots = dict(
            connection.execute(
                "SELECT json_extract(request_json,'$.source'),count(*) FROM graph_runs "
                "WHERE parent_operation_id IS NULL GROUP BY 1"
            ).fetchall()
        )
    found = []
    if roots.get("handoff", 0) > 1:
        found.append("more than one delivered turn")
    if roots.get("handoff_report", 0) > 1:
        found.append("more than one Space wake")
    return found


def wake_waits_for_idle_space(before, world: World) -> list[str]:
    """The Space is woken only while its thread is idle."""

    if world.handoff().notified_operation_id and before[2]:
        return ["Space woken while its thread was busy"]
    return []


def _not_yet_woken(before, _world: World) -> bool:
    return not before[0][2]


def handoff_settles(view) -> bool:
    """The handoff settled, or its newest delivered turn offers the member Retry."""

    newest = _newest_delivered(view)
    return view[0][1] or (newest is not None and newest[3])


RULES = (
    Safety(owed_retries_are_found, _always, "code guard at owed_transport_retry_operation_ids"),
    Safety(pending_retry_blocks_settlement, _has_delivered, "code guard at _handoff_turn_settled"),
    Safety(final_turn_settles_handoff, _has_delivered, "conversations spec: Private agent threads"),
    Safety(one_delivery_and_one_wake, _always, "conversations spec: Private agent threads"),
    Step(wake_waits_for_idle_space, _not_yet_woken, "conversations spec: Private agent threads"),
    Goal(
        handoff_settles,
        "conversations spec: Private agent threads",
        allowed=lambda action: action.environment,
    ),
)
