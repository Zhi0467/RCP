"""Keep every lifecycle's writer list complete.

Each table that holds lifecycle state has one owner: an explorer machine or a
catalog entry that lists it in ``tables``. Every function the scan of
``tests/lifecycles/writers.py`` finds writing an owned table appears in its
owner's ``writers`` or in its owner's ``unreached_writers`` with a reason, so a
new code path that changes a lifecycle fails here until someone lists it.
Every other written table is in the catalog's ``NOT_LIFECYCLE`` with a reason,
and every writer of an interpolated table is declared by an owner or listed in
``UNKNOWN_TABLE``. A listed exception that no longer holds fails too.

``PENDING_TABLES`` holds the tables of explorers that have not landed yet; the
last build step empties and deletes it.
"""

from __future__ import annotations

import inspect
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import rcp
from tests.lifecycles.catalog import ENTRIES, NOT_LIFECYCLE, UNKNOWN_TABLE
from tests.lifecycles.consolidation import Consolidation
from tests.lifecycles.handoff import Handoff
from tests.lifecycles.writers import UNKNOWN, scan, scan_source, schema_tables

OWNERS: tuple[Any, ...] = (Handoff(), Consolidation(), *ENTRIES)
PENDING_TABLES = frozenset(
    {
        "graph_runs",
        "graph_run_receipts",
        "episodes",
        "experiment_episode_state",
        "watchers",
        "episode_wrapups",
        "episode_report_attempts",
        "episode_reports",
        "agent_schedules",
        "approval_cards",
        "questions",
    }
)


def _name(writer: Callable[..., Any]) -> str:
    writer = inspect.unwrap(writer)
    return f"{writer.__module__}:{writer.__qualname__}"


def _listed(owner: Any) -> set[str]:
    return {_name(writer) for writer in (*owner.writers, *owner.unreached_writers)}


@pytest.fixture(scope="module")
def written(tmp_path_factory: pytest.TempPathFactory) -> dict[str, set[str]]:
    tables = schema_tables(tmp_path_factory.mktemp("schema") / "rcp.sqlite3")
    found: dict[str, set[str]] = defaultdict(set)
    for writer, table in scan(Path(rcp.__file__).parent, tables):
        found[table].add(writer)
    return found


def test_scan_reads_sql_in_literals_and_ignores_prose() -> None:
    source = '''
def a(c, x):
    c.execute(f"UPDATE graph_runs SET status = {x}")
def b(c):
    c.execute("INSERT OR REPLACE INTO episodes VALUES (?)")
def c(c, table):
    c.execute(f'UPDATE OR IGNORE {table} SET x = 1')
    c.execute(f'DELETE FROM "{table}"')
def d(c):
    c.execute("INSERT INTO watchers VALUES (1) ON CONFLICT (id) DO UPDATE SET x = 1")
class S:
    def e(self, store):
        store._transition_agent_task("op", "failed")
def f(c):
    """Update the row, then update graph data."""
    c.execute("SELECT * FROM graph_runs WHERE status = 'x'")
'''
    found = scan_source(source, "m", {"graph_runs", "episodes", "watchers"})

    assert found == {
        ("m:a", "graph_runs"),
        ("m:b", "episodes"),
        ("m:c", UNKNOWN),
        ("m:d", "watchers"),
        ("m:S.e", "graph_runs"),
    }


def test_each_table_has_one_place() -> None:
    places: dict[str, list[str]] = defaultdict(list)
    for owner in OWNERS:
        for table in owner.tables:
            places[table].append(owner.name)
    for table in NOT_LIFECYCLE:
        places[table].append("NOT_LIFECYCLE")
    for table in PENDING_TABLES:
        places[table].append("pending")
    assert {table: names for table, names in places.items() if len(names) > 1} == {}


def test_every_writer_of_an_owned_table_is_listed(written: dict[str, set[str]]) -> None:
    missing = {}
    for owner in OWNERS:
        for table in owner.tables:
            unlisted = written.get(table, set()) - _listed(owner)
            if unlisted:
                missing[f"{owner.name}:{table}"] = sorted(unlisted)
    assert missing == {}


def test_every_written_table_has_a_place(written: dict[str, set[str]]) -> None:
    owned = {table for owner in OWNERS for table in owner.tables}
    placed = owned | set(NOT_LIFECYCLE) | PENDING_TABLES
    assert sorted(set(written) - placed - {UNKNOWN}) == []
    assert sorted(set(NOT_LIFECYCLE) - set(written)) == []


def test_every_interpolated_writer_is_declared(written: dict[str, set[str]]) -> None:
    declared = set().union(*(_listed(owner) for owner in OWNERS))
    interpolated = written.get(UNKNOWN, set())
    assert sorted(interpolated - declared - set(UNKNOWN_TABLE)) == []
    assert sorted(set(UNKNOWN_TABLE) - (interpolated - declared)) == []
