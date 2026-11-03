"""Bounded exhaustive exploration of one stored lifecycle against real code.

A machine module describes one lifecycle: how to seed its start state (``start``
returns the booted world in it), how to boot a backend over a data tree, which actions a state offers, how each action
runs, and what the state looks like. ``explore`` searches every action order up
to the machine's bound. States merge when their views are equal.

Every step restores the parent's whole tree (each SQLite database through the
backup API, since the store runs in WAL mode), boots a fresh backend over it and
puts back the parent's ``Memory``. A fresh boot keeps every in-process cache
true to the files. ``Memory`` splits what the product holds in process
(``engine``: workers, timers, decided retries) from what the explorer holds
(``environment``). Both name tasks by lineage, never by operation id, because ids
are random and two paths to one view create different ids.

A restart is the one action the engine runs itself: it advances the clock by a
second, drops the engine memory, boots, and calls the machine's ``restart``,
which runs the startup reconciliation ``api/app.py`` runs for its tables.

Two checks keep the save from drifting from the code. The restore check replays
the first explored edge of each action name continuously in one backend and
compares the view with the restored one. The merge check compares the memory of
two paths that reach one view.

Rules are ``Safety`` (a state), ``Step`` (an edge) and ``Goal`` (liveness: from
every state at depth at most bound minus horizon, some run of ``allowed``
actions within the horizon reaches it). Each rule's docstring states it.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

START = datetime(2026, 1, 15, 12, tzinfo=UTC)
RESTART = "restart"


class Clock:
    """The one clock an exploration's stores and seams read."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def iso(self) -> str:
        return self.now.isoformat()

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass(frozen=True)
class Action:
    """One atomic step; ``environment`` steps happen without a member."""

    name: str
    args: tuple[str, ...] = ()
    environment: bool = True

    def __str__(self) -> str:
        return " ".join((self.name, *self.args))


Trace = tuple[Action, ...]


@dataclass(frozen=True)
class Memory:
    """What a state holds outside its files, named by lineage."""

    engine: Any
    environment: Any


@dataclass(frozen=True)
class Safety:
    """A state rule: ``check`` returns one message per violation."""

    check: Callable[[Any], list[str]]
    applies: Callable[[Any], bool]
    source: str

    @property
    def name(self) -> str:
        return self.check.__name__


@dataclass(frozen=True)
class Step:
    """An edge rule over the parent's view and the child world."""

    check: Callable[[Any, Any], list[str]]
    applies: Callable[[Any, Any], bool]
    source: str

    @property
    def name(self) -> str:
        return self.check.__name__


@dataclass(frozen=True)
class Goal:
    """A liveness rule; ``allowed`` defaults to environment actions but restart."""

    reached: Callable[[Any], bool]
    source: str
    allowed: Callable[[Action], bool] | None = None

    @property
    def name(self) -> str:
        return self.reached.__name__

    def permits(self, action: Action) -> bool:
        if self.allowed is not None:
            return self.allowed(action)
        return action.environment and action.name != RESTART


Rule = Safety | Step | Goal


class Machine(Protocol):
    name: str
    bound: int
    horizon: int
    rules: tuple[Rule, ...]

    def seams(self) -> AbstractContextManager[None]: ...
    def start(self, root: Path, clock: Clock) -> Any: ...
    def boot(self, root: Path, clock: Clock) -> Any: ...
    def close(self, world: Any) -> None: ...
    def databases(self, root: Path) -> Iterable[Path]: ...
    def actions(self, view: Any) -> list[Action]: ...
    def apply(self, world: Any, action: Action) -> None: ...
    def restart(self, world: Any) -> None: ...
    def view(self, world: Any) -> Any: ...
    def save_memory(self, world: Any) -> Memory: ...
    def restore_memory(self, world: Any, memory: Memory) -> None: ...


@dataclass(frozen=True)
class Saved:
    """One state: its tree on disk, the clock and its memory."""

    path: Path
    clock: datetime
    memory: Memory


@dataclass
class Exploration:
    """What a bounded search found."""

    machine: Machine
    depth: dict[Any, int] = field(default_factory=dict)
    edges: dict[Any, list[tuple[Action, Any]]] = field(default_factory=dict)
    traces: dict[Any, Trace] = field(default_factory=dict)
    memory: dict[Any, Memory] = field(default_factory=dict)
    failures: list[tuple[Trace, str]] = field(default_factory=list)

    @property
    def states(self) -> int:
        return len(self.depth)

    def stuck(self) -> list[tuple[Trace, str]]:
        """States within a goal's horizon from which no allowed run reaches it."""

        machine = self.machine
        found = []
        for goal in (rule for rule in machine.rules if isinstance(rule, Goal)):
            for view, depth in self.depth.items():
                if depth > machine.bound - machine.horizon:
                    continue
                frontier = {view}
                for _ in range(machine.horizon):
                    if any(goal.reached(state) for state in frontier):
                        break
                    frontier = {
                        child
                        for state in frontier
                        for action, child in self.edges.get(state, [])
                        if goal.permits(action)
                    }
                if not any(goal.reached(state) for state in frontier):
                    found.append((self.traces[view], f"{goal.name} is never reached"))
        return found


class Explorer:
    """Runs one machine over a working tree, saving states under ``saves``."""

    def __init__(self, machine: Machine, root: Path, saves: Path) -> None:
        self.machine = machine
        self.root = root
        self.saves = saves

    def save(self, world: Any, clock: Clock, name: str) -> Saved:
        databases = list(self.machine.databases(self.root))
        names = {database.name for database in databases}
        path = self.saves / name
        shutil.copytree(
            self.root,
            path,
            ignore=lambda _dir, entries: [
                entry for entry in entries if entry.split("-", 1)[0] in names
            ],
        )
        for database in databases:
            target = path / database.relative_to(self.root)
            with (
                closing(sqlite3.connect(database)) as source,
                closing(sqlite3.connect(target)) as copy,
            ):
                source.backup(copy)
        return Saved(path, clock.now, self.machine.save_memory(world))

    def restore(self, saved: Saved, *, restart: bool = False) -> tuple[Any, Clock]:
        machine = self.machine
        shutil.rmtree(self.root)
        shutil.copytree(saved.path, self.root)
        clock = Clock(saved.clock)
        if restart:
            clock.advance(1)
        world = machine.boot(self.root, clock)
        if restart:
            machine.restore_memory(world, Memory(None, saved.memory.environment))
            machine.restart(world)
        else:
            machine.restore_memory(world, saved.memory)
        return world, clock

    def step(self, saved: Saved, action: Action) -> tuple[Any, Clock]:
        if action.name == RESTART:
            return self.restore(saved, restart=True)
        world, clock = self.restore(saved)
        self.machine.apply(world, action)
        return world, clock

    def replay(self, start: Saved, trace: Trace) -> Any:
        """Run ``trace`` from the start state in one backend, rebooting only on restart."""

        machine = self.machine
        world, clock = self.restore(start)
        for action in trace:
            if action.name == RESTART:
                environment = machine.save_memory(world).environment
                machine.close(world)
                clock.advance(1)
                world = machine.boot(self.root, clock)
                machine.restore_memory(world, Memory(None, environment))
                machine.restart(world)
            else:
                machine.apply(world, action)
        view = machine.view(world)
        machine.close(world)
        return view


def _violations(machine: Machine, world: Any, before: Any | None) -> list[str]:
    found: list[str] = []
    for rule in machine.rules:
        if isinstance(rule, Safety) and rule.applies(world):
            found.extend(f"{rule.name}: {message}" for message in rule.check(world))
        elif isinstance(rule, Step) and before is not None and rule.applies(before, world):
            found.extend(f"{rule.name}: {message}" for message in rule.check(before, world))
    return found


def explore(machine: Machine, root: Path, saves: Path) -> Exploration:
    """Search every action order of at most ``machine.bound`` steps."""

    result = Exploration(machine)
    explorer = Explorer(machine, root, saves)
    with machine.seams():
        clock = Clock()
        world = machine.start(root, clock)
        start = machine.view(world)
        result.depth[start] = 0
        result.traces[start] = ()
        result.failures.extend(((), message) for message in _violations(machine, world, None))
        saved = {start: explorer.save(world, clock, "0")}
        result.memory[start] = saved[start].memory
        machine.close(world)
        replayed: dict[str, tuple[Trace, Any]] = {}
        frontier = [start]
        for depth in range(machine.bound):
            following = []
            for view in frontier:
                trace = result.traces[view]
                children = []
                for action in machine.actions(view):
                    path = (*trace, action)
                    try:
                        world, clock = explorer.step(saved[view], action)
                    except Exception as exc:
                        result.failures.append((path, f"raised {exc!r}"))
                        continue
                    child = machine.view(world)
                    memory = machine.save_memory(world)
                    children.append((action, child))
                    result.failures.extend(
                        (path, message) for message in _violations(machine, world, view)
                    )
                    if child not in result.depth:
                        result.depth[child] = depth + 1
                        result.traces[child] = path
                        result.memory[child] = memory
                        following.append(child)
                        if depth + 1 < machine.bound:
                            saved[child] = explorer.save(world, clock, str(len(result.depth)))
                    elif result.memory[child] != memory:
                        result.failures.append((path, "merge: equal views hold different memory"))
                    replayed.setdefault(action.name, (path, child))
                    machine.close(world)
                result.edges[view] = children
            frontier = following
        for trace, view in replayed.values():
            if explorer.replay(saved[start], trace) != view:
                result.failures.append((trace, "restore: replay reaches a different view"))
    return result
