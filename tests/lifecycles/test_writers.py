"""Keep every lifecycle's writer list complete.

Each table that holds lifecycle state has one owner: an explorer machine or a
catalog entry that lists it in ``tables``. Every function the scan of
``tests/lifecycles/writers.py`` finds writing an owned table appears in its
owner's ``writers`` or in its owner's ``unreached_writers`` with a reason, so a
new code path that changes a lifecycle fails here until someone lists it.

``PENDING_OWNERSHIP`` holds the tables whose owner has not landed yet; the last
build step empties it and the test then requires every written table to be
owned or listed in ``NOT_LIFECYCLE``, and every writer of an interpolated table
to be declared by an owner or listed in ``UNKNOWN_TABLE``.
"""

from __future__ import annotations

import inspect
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import rcp
from tests.lifecycles.handoff import Handoff
from tests.lifecycles.writers import UNKNOWN, scan, scan_source, schema_tables

OWNERS = (Handoff(),)
PENDING_OWNERSHIP = True
NOT_LIFECYCLE: dict[str, str] = {}
UNKNOWN_TABLE: dict[str, str] = {}


def _name(writer: Callable[..., Any]) -> str:
    writer = inspect.unwrap(writer)
    return f"{writer.__module__}:{writer.__qualname__}"


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


def test_each_table_has_at_most_one_owner() -> None:
    owners: dict[str, list[str]] = defaultdict(list)
    for owner in OWNERS:
        for table in owner.tables:
            owners[table].append(owner.name)
    assert {table: names for table, names in owners.items() if len(names) > 1} == {}


def test_every_writer_of_an_owned_table_is_listed(written: dict[str, set[str]]) -> None:
    missing = {}
    for owner in OWNERS:
        listed = {_name(writer) for writer in (*owner.writers, *owner.unreached_writers)}
        for table in owner.tables:
            unlisted = written.get(table, set()) - listed
            if unlisted:
                missing[f"{owner.name}:{table}"] = sorted(unlisted)
    assert missing == {}


def test_every_written_table_has_an_owner(written: dict[str, set[str]]) -> None:
    if PENDING_OWNERSHIP:
        pytest.skip("owners for the remaining tables land with their explorers")
    owned = {table for owner in OWNERS for table in owner.tables}
    assert sorted(set(written) - owned - set(NOT_LIFECYCLE) - {UNKNOWN}) == []
    declared = {
        _name(writer) for owner in OWNERS for writer in (*owner.writers, *owner.unreached_writers)
    }
    assert sorted(written.get(UNKNOWN, set()) - declared - set(UNKNOWN_TABLE)) == []
