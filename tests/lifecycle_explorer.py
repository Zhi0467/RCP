"""Bounded exhaustive explorer of the handoff and transport-retry machine.

A ``World`` is one real backend over a copy of a prepared data tree: the store,
the engine and ``dispatch_thread_handoffs`` run as in production, with only the
seams of ``tests/test_agent_thread_commands.py`` replaced. No worker thread runs.
A worker is the engine's own registry entry, and its two halves are separate
actions: the turn's verdict commits, then ``_task_settled`` decides the
automatic retry and the worker leaves. Retry timers land in ``World.timers``
and fire only as an action, advancing the world's clock by their delay.

``explore`` searches every action sequence up to its bound and merges states
with equal views. Each state is saved as its file tree, its database and the
engine's in-memory worker registry, and each action runs on a fresh backend
loaded from its parent's save. A restart is the one action that drops the
registry and the timers and runs ``recover_at_startup``. Views name each turn
by its lineage: ``S0`` for the source Space turn, ``T0`` for the delivered
project turn and ``W0`` for the Space wake, with ``T1`` and ``W1`` for their
continuations.
"""

from __future__ import annotations

import shutil
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from rcp.limits import AGENT_TRANSPORT_RETRY_BACKOFF_SECONDS

Action = tuple[str, ...]
Trace = tuple[Action, ...]
View = tuple

_TERMINAL = frozenset({"succeeded", "failed", "interrupted", "paused"})
_SETTLE_OUTCOMES = ("succeeded", "transport_lost", "other")
_ENVIRONMENT = frozenset({"settle", "finish", "fire", "restart"})
_DATABASE = "rcp.sqlite3"


@dataclass(frozen=True)
class Template:
    """The prepared tree every world starts from, and the names it needs."""

    root: Path
    snapshot: Path
    manifest_path: Path
    data_dir: Path
    handoff_id: str
    source_operation_id: str
    space_thread_id: str
    member: Any
    start: datetime


@dataclass(frozen=True)
class Saved:
    """One state: its tree on disk and what the engine held in memory."""

    path: Path
    clock: datetime
    timers: tuple[tuple[str, int, float], ...]
    workers: frozenset[str]
    decided: frozenset[str]
    settled_on: str | None


def build_template(manifest_path: Path, root: Path, snapshot: Path) -> Template:
    """Seed the handoff fixture once over the tree at ``root`` and copy it to ``snapshot``."""

    from rcp.service import RunRequest
    from tests.helpers import authorized_human, create_named_app
    from tests.test_storage import _task_record

    data_dir = root / "data"
    app = create_named_app(str(manifest_path), data_dir=data_dir)
    store = app.state.background_tasks.store
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
            operation_id="source",
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
        source_operation_id="source",
        target_thread_id=target.thread_id,
        idempotency_key="explorer",
        message="Please check",
    )
    store.close()
    shutil.copytree(root, snapshot)
    return Template(
        root=root,
        snapshot=snapshot,
        manifest_path=manifest_path,
        data_dir=data_dir,
        handoff_id=handoff.handoff_id,
        source_operation_id="source",
        space_thread_id=source.thread_id,
        member=actor,
        start=datetime.now(UTC) + timedelta(seconds=1),
    )


@contextmanager
def explorer_seams() -> Iterator[None]:
    """The module-level seam, held for a whole exploration."""

    with mock.patch("rcp.background.seconds_until_automatic_launch", lambda: 0):
        yield


@dataclass
class World:
    """One backend over the template's tree, driven only by actions."""

    template: Template
    clock: datetime
    timers: list[tuple[str, int, float]] = field(default_factory=list)
    settled_on: str | None = None
    app: Any = None

    @classmethod
    def first(cls, template: Template) -> World:
        """The starting state: the template's tree after one dispatch."""

        shutil.rmtree(template.root)
        shutil.copytree(template.snapshot, template.root)
        world = cls(template, template.start)
        world._boot()
        world.dispatch()
        return world

    @classmethod
    def after(cls, template: Template, saved: Saved, action: Action) -> World:
        """The state ``action`` leads to from ``saved``, followed by a dispatch.

        A restart boots over the saved tree without its registry or timers and
        runs startup recovery; any other action runs on the engine as it was.
        """

        shutil.rmtree(template.root)
        shutil.copytree(saved.path, template.root)
        world = cls(template, saved.clock, settled_on=saved.settled_on)
        world._boot()
        background = world.background
        if action == ("restart",):
            background.recover_at_startup()
        else:
            world.timers = list(saved.timers)
            with background._controls_lock:
                for operation_id in saved.workers:
                    background._workers[operation_id] = threading.current_thread()
                background._retry_decided.update(saved.decided)
            world._act(action)
        world.dispatch()
        if world.settled_on is None and world.handoff().settled_at is not None:
            newest = world.newest_delivered()
            world.settled_on = world.lineage()[newest.operation_id] if newest else ""
        return world

    def save(self, path: Path) -> Saved:
        """Copy the tree and back the database up, so open handles cannot tear it."""

        data = self.template.data_dir
        shutil.copytree(self.template.root, path, ignore=shutil.ignore_patterns(f"{_DATABASE}*"))
        target = path / data.relative_to(self.template.root) / _DATABASE
        with self.store.connection() as source, closing(sqlite3.connect(target)) as copy:
            source.backup(copy)
        background = self.background
        return Saved(
            path=path,
            clock=self.clock,
            timers=tuple(self.timers),
            workers=frozenset(background._workers),
            decided=frozenset(background._retry_decided),
            settled_on=self.settled_on,
        )

    def close(self) -> None:
        if self.app is not None:
            self.store.close()

    @property
    def background(self):
        return self.app.state.background_tasks

    @property
    def store(self):
        return self.background.store

    def _boot(self) -> None:
        from tests.helpers import create_named_app

        self.app = create_named_app(
            str(self.template.manifest_path), data_dir=self.template.data_dir
        )
        background = self.background
        store = background.store
        store.now = lambda: self.clock.isoformat()
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
            self.timers.append((operation_id, attempt, wait))

        background._spawn_record = spawn
        background._schedule_transport_retry = schedule

    def dispatch(self) -> list[str]:
        from rcp.runs.agent_thread_commands import dispatch_thread_handoffs

        return dispatch_thread_handoffs(
            self.store,
            self.app.state.catalog,
            self.background,
            project_admission=lambda _: nullcontext(),
        )

    def handoff(self):
        handoff = self.store.agent_handoff(self.template.handoff_id)
        assert handoff is not None
        return handoff

    def lineage(self) -> dict[str, str]:
        """Each operation id's positional name."""

        handoff = self.handoff()
        roots = {
            self.template.source_operation_id: "S",
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

        thread = self.store.agent_thread(self.template.space_thread_id)
        return thread is not None and _thread_busy(self.store, thread)

    def view(self) -> View:
        handoff = self.handoff()
        names = self.lineage()
        background = self.background
        turns = []
        for operation_id, name in sorted(names.items(), key=lambda item: item[1]):
            task = self.store.agent_task(operation_id)
            assert task is not None
            receipts = tuple(
                sorted(
                    receipt.category
                    for receipt in self.store.agent_task_receipts(operation_id)
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
        timers = tuple(sorted((names[op], attempt, delay) for op, attempt, delay in self.timers))
        return (
            (
                handoff.target_operation_id is not None,
                handoff.settled_at is not None,
                handoff.notified_operation_id is not None,
                handoff.refusal_reason,
                self.settled_on,
            ),
            tuple(turns),
            self.space_busy(),
            timers,
        )

    def _act(self, action: Action) -> None:
        names = self.lineage()
        operation = {name: op for op, name in names.items()}
        kind = action[0]
        if kind == "settle":
            self._settle(operation[action[1]], action[2])
        elif kind == "finish":
            self._finish(operation[action[1]])
        elif kind == "fire":
            timer = next(
                timer
                for timer in self.timers
                if (names[timer[0]], str(timer[1])) == (action[1], action[2])
            )
            self.timers.remove(timer)
            self.clock += timedelta(seconds=timer[2])
            self.background._run_transport_retry(timer[0], attempt=timer[1])
        elif kind == "retry":
            self.background.retry(operation[action[1]], authorized_by=self.template.member)
        else:
            raise ValueError(f"unknown action {action}")

    def _settle(self, operation_id: str, outcome: str) -> None:
        store = self.store
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

    def _finish(self, operation_id: str) -> None:
        from rcp.agents import AgentProcessControl
        from rcp.background import AgentTaskExecution

        background = self.background
        record = background.store.agent_task(operation_id)
        assert record is not None
        execution = AgentTaskExecution(
            operation_id=operation_id, store=background.store, control=AgentProcessControl()
        )
        background._task_settled(record, background._request_from_record(record), execution)
        background._forget_control(operation_id)


def actions(view: View) -> list[Action]:
    """Every action the state offers, read from its view."""

    _handoff, turns, _busy, timers = view
    found: list[Action] = []
    for name, status, _kind, _retry, _resume, _receipts, (worker, _decided) in turns:
        if worker and status in {"queued", "running"}:
            found.extend(("settle", name, outcome) for outcome in _SETTLE_OUTCOMES)
        elif worker and status in _TERMINAL:
            found.append(("finish", name))
    found.extend(dict.fromkeys(("fire", name, str(attempt)) for name, attempt, _ in timers))
    newest = _newest_delivered(view)
    if newest is not None and newest[3]:
        found.append(("retry", newest[0]))
    found.append(("restart",))
    return found


def _newest_delivered(view: View):
    delivered = [turn for turn in view[1] if turn[0].startswith("T")]
    return max(delivered, key=lambda turn: int(turn[0][1:])) if delivered else None


def _goal(view: View) -> bool:
    """The handoff settled, or its newest delivered turn offers the member Retry."""

    newest = _newest_delivered(view)
    return view[0][1] or (newest is not None and newest[3])


def violations(world: World, before: View | None) -> list[str]:
    """Agreement and safety in the world's current state, entered from view ``before``."""

    from rcp.runs.agent_thread_commands import _handoff_turn_settled, _newest_continuation

    found: list[str] = []
    store, background = world.store, world.background
    names = world.lineage()
    owed = set(store.owed_transport_retry_operation_ids())
    for operation_id, name in names.items():
        task = store.agent_task(operation_id)
        if task is not None and background.transport_retry_owed(task) and operation_id not in owed:
            found.append(f"{name} is owed a retry the restart query misses")
    handoff = world.handoff()
    newest = world.newest_delivered()
    if newest is not None:
        name = names[newest.operation_id]
        timed = any(op == newest.operation_id for op, _, _ in world.timers)
        with store.connection() as connection:
            row = _newest_continuation(connection, newest.operation_id)
        if timed and _handoff_turn_settled(store, background, row):
            found.append(f"{name} reads settled with a retry timer pending")
        if timed and world.settled_on == name:
            found.append(f"handoff settled on {name} while its retry is pending")
        if (
            handoff.settled_at is None
            and newest.status in _TERMINAL
            and not timed
            and not background.retry_undecided(newest.operation_id)
        ):
            found.append(f"handoff unsettled with {name} final and no retry coming")
    with store.connection() as connection:
        roots = dict(
            connection.execute(
                "SELECT json_extract(request_json,'$.source'),count(*) FROM graph_runs "
                "WHERE parent_operation_id IS NULL GROUP BY 1"
            ).fetchall()
        )
    if roots.get("handoff", 0) > 1:
        found.append("more than one delivered turn")
    if roots.get("handoff_report", 0) > 1:
        found.append("more than one Space wake")
    if before is not None and not before[0][2] and handoff.notified_operation_id and before[2]:
        found.append("Space woken while its thread was busy")
    return found


@dataclass
class Exploration:
    """What a bounded search found."""

    bound: int
    depth: dict[View, int] = field(default_factory=dict)
    edges: dict[View, list[tuple[Action, View]]] = field(default_factory=dict)
    traces: dict[View, Trace] = field(default_factory=dict)
    failures: list[tuple[Trace, str]] = field(default_factory=list)

    @property
    def states(self) -> int:
        return len(self.depth)

    def stuck(self, horizon: int = 3) -> list[tuple[Trace, str]]:
        """States within the liveness horizon from which no environment run settles."""

        found = []
        for view, depth in self.depth.items():
            if depth > self.bound - horizon:
                continue
            frontier = {view}
            for _ in range(horizon):
                if any(_goal(state) for state in frontier):
                    break
                frontier = {
                    child
                    for state in frontier
                    for action, child in self.edges.get(state, [])
                    if action[0] in _ENVIRONMENT
                }
            if not any(_goal(state) for state in frontier):
                found.append((self.traces[view], "no environment run settles the handoff"))
        return found


def explore(template: Template, bound: int, saves: Path) -> Exploration:
    """Search every action sequence of at most ``bound`` steps, merging equal views."""

    result = Exploration(bound=bound)
    with explorer_seams():
        world = World.first(template)
        root = world.view()
        result.depth[root] = 0
        result.traces[root] = ()
        result.failures.extend(((), message) for message in violations(world, None))
        saved = {root: world.save(saves / "0")}
        world.close()
        frontier = [root]
        for depth in range(bound):
            following = []
            for view in frontier:
                trace = result.traces[view]
                children = []
                for action in actions(view):
                    try:
                        world = World.after(template, saved[view], action)
                    except Exception as exc:
                        result.failures.append(((*trace, action), f"raised {exc!r}"))
                        continue
                    child = world.view()
                    children.append((action, child))
                    if child not in result.depth:
                        result.depth[child] = depth + 1
                        result.traces[child] = (*trace, action)
                        result.failures.extend(
                            ((*trace, action), message) for message in violations(world, view)
                        )
                        following.append(child)
                        if depth + 1 < bound:
                            saved[child] = world.save(saves / str(len(result.depth)))
                    world.close()
                result.edges[view] = children
            frontier = following
    return result
