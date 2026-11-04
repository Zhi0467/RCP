"""The per-project half of the machine identity upgrade: alias-keyed rows name cards.

Migration 77 rekeyed every host-keyed row and only renamed the columns that
held a project's machine alias, because an alias means something only in its
own manifest. Once `rcp.projects.upgrade_project_machine_ids` has read that
manifest, this rewrites the project's rows through `machine_id_for_alias`.

Rewriting is idempotent: a value that already names a card stays, so a run
interrupted before the manifest or the pending flag was written repeats safely.
On a key collision the newest row is kept.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Collection, Mapping
from typing import TYPE_CHECKING, Any

from rcp.machine_upgrade import machine_id_for_alias, upgraded_worktree_binding

if TYPE_CHECKING:
    from rcp.storage import AppStore

_OPEN_REQUEST = """
SELECT 1 FROM project_transfer_requests WHERE project_id = ? AND phase != 'completed'
UNION ALL
SELECT 1 FROM project_provisioning_requests
WHERE proposed_project_id = ? AND status NOT IN ('completed', 'cancelled')
LIMIT 1
"""

_RUN_ON_DOCUMENTS = (
    ("graph_runs", "request_json", "$.run_on"),
    ("watchers", "continuation_json", "$.run_on"),
    ("auto_research_child_experiments", "request_json", "$.run_on"),
    ("chat_forks", "record_json", "$.origin.run_on"),
)


def project_machine_upgrade_blocked(store: AppStore, project_id: str) -> bool:
    """Whether a transfer or provisioning request is still open for the project."""

    with store.connection() as connection:
        return connection.execute(_OPEN_REQUEST, (project_id, project_id)).fetchone() is not None


def rekey_project_machines(
    store: AppStore, project_id: str, machine_ids: Mapping[str, str]
) -> None:
    """Name cards in every row that held one of the project's machine aliases."""

    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            card_ids = {
                str(row[0]) for row in connection.execute("SELECT machine_id FROM space_machines")
            }

            def mapped(value: str) -> str:
                return machine_id_for_alias(value, machine_ids, card_ids)

            _rekey_column(
                connection,
                "chat_session_contexts",
                "machine_id",
                project_id,
                mapped,
                key=("provider", "native_session_id"),
                newest="updated_at",
            )
            _rekey_column(connection, "writing_sessions", "machine_id", project_id, mapped)
            _rekey_column(
                connection,
                "compute_backend_probes",
                "machine_id",
                project_id,
                mapped,
                key=("route",),
                newest="probed_at",
            )
            _rekey_column(connection, "result_views", "run_on", project_id, mapped)
            _rekey_column(
                connection,
                "agent_threads",
                "agent_run_on",
                project_id,
                mapped,
                where="kind = 'project'",
            )
            for table, column, path in _RUN_ON_DOCUMENTS:
                _rekey_document(connection, table, column, path, project_id, mapped)
            _rekey_bindings(
                connection,
                "conversation_worktrees",
                project_id,
                lambda binding: upgraded_worktree_binding(binding, machine_ids, card_ids),
            )
            _rekey_bindings(
                connection,
                "episode_isolations",
                project_id,
                lambda isolation: _upgraded_isolation(isolation, machine_ids, card_ids),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def finish_project_machine_ids(store: AppStore, project_id: str) -> None:
    """The project's manifest and rows name cards; it may open and recover again."""

    with store.connection() as connection:
        connection.execute(
            "UPDATE projects SET machine_ids_pending = 0 WHERE project_id = ?", (project_id,)
        )


def _rekey_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    project_id: str,
    mapped: Callable[[str], str],
    *,
    key: tuple[str, ...] = (),
    newest: str = "rowid",
    where: str = "1",
) -> None:
    """Map one column for the project's rows; rows that collide on `key` keep the newest."""

    rows = connection.execute(
        f'SELECT rowid AS row_id, "{column}" AS value, {newest} AS newest'
        + "".join(f', "{name}"' for name in key)
        + f' FROM "{table}" WHERE project_id = ? AND "{column}" IS NOT NULL AND ({where})',
        (project_id,),
    ).fetchall()
    groups: dict[tuple[object, ...], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        groups[(*(row[name] for name in key), mapped(str(row["value"])))].append(row)
    updates: list[tuple[str, int]] = []
    for (*_, target), group in groups.items():
        if key:
            group.sort(key=lambda row: (str(row["newest"] or ""), int(row["row_id"])), reverse=True)
            connection.executemany(
                f'DELETE FROM "{table}" WHERE rowid = ?', [(row["row_id"],) for row in group[1:]]
            )
            group = group[:1]
        updates.extend(
            (str(target), int(row["row_id"])) for row in group if str(row["value"]) != target
        )
    connection.executemany(
        f'UPDATE "{table}" SET "{column}" = char(0) || "{column}" WHERE rowid = ?',
        [(row_id,) for _, row_id in updates],
    )
    connection.executemany(f'UPDATE "{table}" SET "{column}" = ? WHERE rowid = ?', updates)


def _rekey_document(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    path: str,
    project_id: str,
    mapped: Callable[[str], str],
) -> None:
    rows = connection.execute(
        f'SELECT rowid AS row_id, json_extract("{column}", ?) AS value FROM "{table}" '
        f"WHERE project_id = ? AND json_type(\"{column}\", ?) = 'text'",
        (path, project_id, path),
    ).fetchall()
    connection.executemany(
        f'UPDATE "{table}" SET "{column}" = json_set("{column}", ?, ?) WHERE rowid = ?',
        [
            (path, mapped(str(row["value"])), row["row_id"])
            for row in rows
            if mapped(str(row["value"])) != row["value"]
        ],
    )


def _rekey_bindings(
    connection: sqlite3.Connection,
    table: str,
    project_id: str,
    upgraded: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    rows = connection.execute(
        f'SELECT rowid AS row_id, binding_json FROM "{table}" WHERE project_id = ?',
        (project_id,),
    ).fetchall()
    for row in rows:
        binding = json.loads(row["binding_json"])
        rewritten = upgraded(binding)
        if rewritten != binding:
            connection.execute(
                f'UPDATE "{table}" SET binding_json = ? WHERE rowid = ?',
                (json.dumps(rewritten, separators=(",", ":")), row["row_id"]),
            )


def _upgraded_isolation(
    isolation: dict[str, Any], machine_ids: Mapping[str, str], card_ids: Collection[str]
) -> dict[str, Any]:
    worktree = isolation.get("worktree")
    if not isinstance(worktree, dict):
        return isolation
    return {**isolation, "worktree": upgraded_worktree_binding(worktree, machine_ids, card_ids)}
