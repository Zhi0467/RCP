from __future__ import annotations

import sqlite3
import sys
from contextlib import closing, nullcontext
from pathlib import Path
from typing import Literal, NamedTuple

from tests.lifecycles.engine import RESTART, Action, Clock, Goal, Memory, Safety, explore

Light = Literal["red", "green", "amber"]


class View(NamedTuple):
    light: str
    presses: int


def _write(connection: sqlite3.Connection, light: str) -> None:
    connection.execute("UPDATE light SET value = ?", (light,))


def _unused(connection: sqlite3.Connection) -> None:
    connection.execute("DELETE FROM light")


class Toy:
    """A light that turns green when pressed; ``presses`` lives only in memory."""

    name = "toy"
    bound = 3
    horizon = 1
    covers = {Light: ("light", None)}
    tables = frozenset({"light"})
    writers = (_write, _unused)
    unreached_values = {"amber": "no action makes amber"}
    unreached_writers = {_unused: "no action deletes"}

    def __init__(self, *, rules=(), in_view: bool = True) -> None:
        self.rules = rules
        self.in_view = in_view

    def seams(self):
        return nullcontext()

    def databases(self, root: Path) -> list[Path]:
        return [root / "toy.sqlite3"]

    def start(self, root: Path, clock: Clock):
        with closing(sqlite3.connect(root / "toy.sqlite3")) as connection:
            connection.execute("CREATE TABLE light(value TEXT)")
            connection.execute("INSERT INTO light VALUES ('red')")
            connection.commit()
        return self.boot(root, clock)

    def boot(self, root: Path, clock: Clock):
        return {"db": sqlite3.connect(root / "toy.sqlite3"), "presses": 0}

    def close(self, world) -> None:
        world["db"].close()

    def actions(self, view: View) -> list[Action]:
        return [Action("press"), Action(RESTART)]

    def apply(self, world, action: Action) -> None:
        _write(world["db"], "green")
        world["db"].commit()
        world["presses"] = min(world["presses"] + 1, 1)

    def restart(self, world) -> None:
        pass

    def view(self, world) -> View:
        light = world["db"].execute("SELECT value FROM light").fetchone()[0]
        return View(light, world["presses"] if self.in_view else 0)

    def save_memory(self, world) -> Memory:
        return Memory(world["presses"], None)

    def restore_memory(self, world, memory: Memory) -> None:
        world["presses"] = memory.engine or 0


def _green(view: View) -> bool:
    return view.light == "green"


def _never(_world) -> bool:
    return False


def _nothing(_world) -> list[str]:
    return []


def _explore(tmp_path: Path, machine: Toy):
    root = tmp_path / "tree"
    root.mkdir()
    return explore(machine, root, tmp_path / "saves")


def test_a_complete_machine_has_no_gaps(tmp_path: Path) -> None:
    result = _explore(tmp_path, Toy(rules=(Goal(_green, "toy"),)))

    assert result.failures == []
    assert result.stuck() == []
    assert result.gaps() == []


def test_gaps_name_vacuous_rules_and_stale_exceptions(tmp_path: Path) -> None:
    machine = Toy(rules=(Safety(_nothing, _never, "toy"),))
    machine.unreached_values = {"amber": "x", "green": "stale"}
    machine.writers = (_write, _unused, Toy.view)
    result = _explore(tmp_path, machine)

    gaps = result.gaps()
    assert "rule _nothing never applies" in gaps
    assert "status 'green' is reached but listed as unreached" in gaps
    if sys.version_info >= (3, 12):
        assert "writer Toy.view never runs" in gaps


def test_memory_left_out_of_the_view_fails_the_merge_check(tmp_path: Path) -> None:
    result = _explore(tmp_path, Toy(in_view=False))

    assert any("merge" in message for _, message in result.failures)


def test_a_goal_beyond_the_bound_is_a_gap(tmp_path: Path) -> None:
    machine = Toy(rules=(Goal(_green, "toy"),))
    machine.horizon = machine.bound + 1

    assert (
        "goal _green is never checked from an unreached state" in _explore(tmp_path, machine).gaps()
    )
