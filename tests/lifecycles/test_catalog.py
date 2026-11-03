"""Keep the lifecycle catalog complete against the writer scan.

Every table the scan finds written belongs to an explorer, to exactly one
catalog entry, or to ``NOT_LIFECYCLE``; every writer of an entry's tables is in
that entry's ``writers``; and every writer of an interpolated table is
declared by an entry or listed in ``UNKNOWN_TABLE``.
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
from tests.lifecycles.writers import UNKNOWN, scan, schema_tables

EXPLORER_TABLES = frozenset(
    {
        "agent_handoffs",
        "graph_runs",
        "graph_run_receipts",
        "episodes",
        "experiment_episode_state",
        "watchers",
        "episode_wrapups",
        "episode_report_attempts",
        "episode_reports",
        "agent_schedules",
        "consolidation_runs",
        "consolidation_schedules",
        "consolidation_targets",
        "consolidation_apply_receipts",
        "approval_cards",
        "questions",
    }
)


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


def test_every_writer_of_an_entry_table_is_listed(written: dict[str, set[str]]) -> None:
    missing = {}
    for entry in ENTRIES:
        listed = {_name(writer) for writer in entry.writers}
        for table in entry.tables:
            unlisted = written.get(table, set()) - listed
            if unlisted:
                missing[f"{entry.name}:{table}"] = sorted(unlisted)
    assert missing == {}


def test_each_table_has_one_place() -> None:
    places: dict[str, list[str]] = defaultdict(list)
    for entry in ENTRIES:
        for table in entry.tables:
            places[table].append(entry.name)
    for table in NOT_LIFECYCLE:
        places[table].append("NOT_LIFECYCLE")
    for table in EXPLORER_TABLES:
        places[table].append("explorer")
    assert {table: names for table, names in places.items() if len(names) > 1} == {}


def test_every_written_table_is_owned_or_not_lifecycle(written: dict[str, set[str]]) -> None:
    owned = {table for entry in ENTRIES for table in entry.tables}
    placed = owned | set(NOT_LIFECYCLE) | EXPLORER_TABLES
    assert sorted(set(written) - placed - {UNKNOWN}) == []
    assert sorted((owned | set(NOT_LIFECYCLE)) - set(written)) == []


def test_every_interpolated_writer_is_declared(written: dict[str, set[str]]) -> None:
    declared = {_name(writer) for entry in ENTRIES for writer in entry.writers}
    interpolated = written.get(UNKNOWN, set())
    assert sorted(interpolated - declared - set(UNKNOWN_TABLE)) == []
    assert sorted(set(UNKNOWN_TABLE) - (interpolated - declared)) == []
