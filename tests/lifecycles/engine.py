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

``Exploration.gaps`` checks the checks. Every value of each ``covers`` status
type appears in some view or sits in ``unreached_values`` with a reason. Every
declared writer runs inside an action (never while seeding or booting) or sits
in ``unreached_writers`` with a reason; calls are counted with
``sys.monitoring``, which exists only from Python 3.12, so the 3.11 run skips
this one check and the 3.12 run of the same CI enforces it. Every Safety and
Step rule applied somewhere, and every Goal was checked from a state that had
not reached it. A listed exception that no longer holds is a gap too.
"""

from __future__ import annotations

import inspect
import shutil
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, get_args

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
    covers: Mapping[Any, tuple[str, int | None]]
    tables: frozenset[str]
    writers: tuple[Callable[..., Any], ...]
    unreached_values: Mapping[str, str]
    unreached_writers: Mapping[Callable[..., Any], str]

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
    applied: Counter[str] = field(default_factory=Counter)
    calls: Counter[Callable[..., Any]] | None = None

    @property
    def states(self) -> int:
        return len(self.depth)

    def gaps(self) -> list[str]:
        """Coverage the exploration lacks, and listed exceptions that no longer hold."""

        machine = self.machine
        found: list[str] = []
        for status, (name, column) in machine.covers.items():
            seen = set()
            for view in self.depth:
                value = getattr(view, name)
                seen.update([value] if column is None else (row[column] for row in value))
            for value in get_args(status):
                if value not in seen and value not in machine.unreached_values:
                    found.append(f"status {value!r} is never reached")
                if value in seen and value in machine.unreached_values:
                    found.append(f"status {value!r} is reached but listed as unreached")
        if self.calls is not None:
            for writer in machine.writers:
                called = self.calls[writer] > 0
                if not called and writer not in machine.unreached_writers:
                    found.append(f"writer {writer.__qualname__} never runs")
                if called and writer in machine.unreached_writers:
                    found.append(f"writer {writer.__qualname__} runs but is listed as unreached")
        for rule in machine.rules:
            if isinstance(rule, Goal):
                if not self._goal_checked(rule):
                    found.append(f"goal {rule.name} is never checked from an unreached state")
            elif self.applied[rule.name] == 0:
                found.append(f"rule {rule.name} never applies")
        return found

    def _goal_checked(self, goal: Goal) -> bool:
        limit = self.machine.bound - self.machine.horizon
        return any(depth <= limit and not goal.reached(view) for view, depth in self.depth.items())

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

    def __init__(
        self, machine: Machine, root: Path, saves: Path, calls: _Calls | None = None
    ) -> None:
        self.machine = machine
        self.root = root
        self.saves = saves
        self.calls = calls

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
            with self._counted():
                machine.restart(world)
        else:
            machine.restore_memory(world, saved.memory)
        return world, clock

    def step(self, saved: Saved, action: Action) -> tuple[Any, Clock]:
        if action.name == RESTART:
            return self.restore(saved, restart=True)
        world, clock = self.restore(saved)
        with self._counted():
            self.machine.apply(world, action)
        return world, clock

    def _counted(self) -> AbstractContextManager[None]:
        return self.calls.counted() if self.calls is not None else _nothing()

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


def _violations(
    machine: Machine, world: Any, before: Any | None, applied: Counter[str] | None = None
) -> list[str]:
    found: list[str] = []
    for rule in machine.rules:
        if isinstance(rule, Safety) and rule.applies(world):
            found.extend(f"{rule.name}: {message}" for message in rule.check(world))
        elif isinstance(rule, Step) and before is not None and rule.applies(before, world):
            found.extend(f"{rule.name}: {message}" for message in rule.check(before, world))
        else:
            continue
        if applied is not None:
            applied[rule.name] += 1
    return found


class _Calls:
    """Counts calls to each writer while ``counting`` is set (Python 3.12+)."""

    _TOOL = 5

    def __init__(self, writers: Iterable[Callable[..., Any]]) -> None:
        self.writers = {inspect.unwrap(writer).__code__: writer for writer in writers}
        self.counts: Counter[Callable[..., Any]] = Counter()
        self.counting = False

    def _start(self, code: Any, _offset: int) -> None:
        if self.counting:
            self.counts[self.writers[code]] += 1

    @contextmanager
    def watching(self) -> Iterator[None]:
        monitoring = sys.monitoring
        start = monitoring.events.PY_START
        monitoring.use_tool_id(self._TOOL, "lifecycle explorers")
        monitoring.register_callback(self._TOOL, start, self._start)
        for code in self.writers:
            monitoring.set_local_events(self._TOOL, code, start)
        try:
            yield
        finally:
            for code in self.writers:
                monitoring.set_local_events(self._TOOL, code, 0)
            monitoring.register_callback(self._TOOL, start, None)
            monitoring.free_tool_id(self._TOOL)

    @contextmanager
    def counted(self) -> Iterator[None]:
        self.counting = True
        try:
            yield
        finally:
            self.counting = False


@contextmanager
def _nothing() -> Iterator[None]:
    yield


def _watched(calls: _Calls | None) -> AbstractContextManager[None]:
    return calls.watching() if calls is not None else _nothing()


def witness(machine: Machine, root: Path, saves: Path, trace: Trace) -> set[str]:
    """The rules that report along ``trace``, each step restored as in ``explore``."""

    explorer = Explorer(machine, root, saves)
    with machine.seams():
        clock = Clock()
        world = machine.start(root, clock)
        before = machine.view(world)
        reported = {message.split(":")[0] for message in _violations(machine, world, None)}
        saved = explorer.save(world, clock, "w0")
        machine.close(world)
        for index, action in enumerate(trace, 1):
            world, clock = explorer.step(saved, action)
            reported |= {message.split(":")[0] for message in _violations(machine, world, before)}
            before = machine.view(world)
            saved = explorer.save(world, clock, f"w{index}")
            machine.close(world)
    return reported


def explore(machine: Machine, root: Path, saves: Path) -> Exploration:
    """Search every action order of at most ``machine.bound`` steps."""

    result = Exploration(machine)
    calls = _Calls(machine.writers) if hasattr(sys, "monitoring") else None
    explorer = Explorer(machine, root, saves, calls)
    with machine.seams(), _watched(calls):
        clock = Clock()
        world = machine.start(root, clock)
        start = machine.view(world)
        result.depth[start] = 0
        result.traces[start] = ()
        result.failures.extend(
            ((), message) for message in _violations(machine, world, None, result.applied)
        )
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
                        (path, message)
                        for message in _violations(machine, world, view, result.applied)
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
    result.calls = calls.counts if calls is not None else None
    return result
