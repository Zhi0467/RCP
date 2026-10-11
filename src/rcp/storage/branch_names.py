"""Project-wide branch labels and permanent name reservations."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime

from rcp.storage.mixin_base import StoreMixinBase
from rcp.storage.models import EpisodeRecord


class BranchNameInvalid(ValueError):
    """The requested branch label violates the public naming contract."""


class BranchNameTaken(ValueError):
    """Another branch owns or previously used this name."""


def validate_branch_name(name: str) -> str:
    if name == "main" or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}[a-z0-9]", name) is None:
        raise BranchNameInvalid("Use 2–32 lowercase letters, digits, or hyphens; main is reserved.")
    return name


def normalize_branch_name(name: object) -> str | None:
    if name is None:
        return None
    if not isinstance(name, str):
        raise BranchNameInvalid("A branch name must be text.")
    return None if name == "" else validate_branch_name(name)


def migrate_branch_names(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS branch_names (
        project_id TEXT NOT NULL, branch_id TEXT NOT NULL, name TEXT NOT NULL,
        PRIMARY KEY(project_id, branch_id), UNIQUE(project_id, name)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS branch_name_reservations (
        project_id TEXT NOT NULL, name TEXT NOT NULL, branch_id TEXT NOT NULL,
        PRIMARY KEY(project_id, name)
    )""")


def _fallback(mode: str, created_at: str, occupied: set[str]) -> str:
    created = datetime.fromisoformat(created_at).astimezone(UTC)
    month = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
    prefix = "research" if mode == "auto_research" else "experiment"
    base = f"{prefix}-{month[created.month - 1]}-{created.day:02d}"
    name, suffix = base, 2
    while name in occupied:
        name = f"{base}-{suffix}"
        suffix += 1
    return name


def resolve_branch_names(connection: sqlite3.Connection, project_id: str) -> dict[str, str]:
    """Resolve every root, including archived roots, in deterministic creation order."""
    rows = connection.execute(
        "SELECT episode_id, mode, created_at FROM episodes WHERE project_id = ? "
        "AND json_extract(graph_target_json, '$.kind') = 'branch' "
        "AND json_extract(graph_target_json, '$.branch_id') = episode_id",
        (project_id,),
    ).fetchall()
    stored = dict(
        connection.execute(
            "SELECT branch_id, name FROM branch_names WHERE project_id = ?", (project_id,)
        ).fetchall()
    )
    occupied = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM branch_name_reservations WHERE project_id = ?", (project_id,)
        )
    } | set(stored.values())
    names: dict[str, str] = {}
    for row in sorted(
        rows,
        key=lambda row: (
            datetime.fromisoformat(row["created_at"]).astimezone(UTC),
            row["episode_id"],
        ),
    ):
        branch_id = str(row["episode_id"])
        name = stored.get(branch_id)
        if name is None:
            name = _fallback(row["mode"], row["created_at"], occupied)
        names[branch_id] = name
        occupied.add(name)
    return names


def reserve_branch_name(
    connection: sqlite3.Connection, record: EpisodeRecord, requested: object = None
) -> None:
    """Called inside the episode admission transaction, before inserting its root."""
    if record.graph_target.kind != "branch" or record.graph_target.branch_id != record.episode_id:
        return
    name = normalize_branch_name(requested)
    resolved = resolve_branch_names(connection, record.project_id)
    occupied = set(resolved.values()) | {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM branch_name_reservations WHERE project_id = ?", (record.project_id,)
        )
    }
    if name is None:
        name = _fallback(record.mode, record.created_at, occupied)
    elif name in occupied:
        raise BranchNameTaken("That branch name is already taken.")
    connection.execute(
        "INSERT INTO branch_names VALUES (?, ?, ?)", (record.project_id, record.episode_id, name)
    )
    connection.execute(
        "INSERT INTO branch_name_reservations VALUES (?, ?, ?)",
        (record.project_id, name, record.episode_id),
    )


class BranchNameStoreMixin(StoreMixinBase):
    def branch_names(self, project_id: str) -> dict[str, str]:
        with self.connection() as connection:
            connection.execute("BEGIN")
            return resolve_branch_names(connection, project_id)

    def rename_branch(self, project_id: str, branch_id: str, name: str) -> str:
        name = validate_branch_name(name)
        if branch_id == "main":
            raise BranchNameInvalid("main cannot be renamed.")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            names = resolve_branch_names(connection, project_id)
            if branch_id not in names:
                raise KeyError(branch_id)
            if names[branch_id] == name:
                return name
            if (
                name in names.values()
                or connection.execute(
                    "SELECT 1 FROM branch_name_reservations WHERE project_id = ? AND name = ?",
                    (project_id, name),
                ).fetchone()
                is not None
            ):
                raise BranchNameTaken("That branch name is already taken.")
            # Retain the old computed name too: legacy neighbors must not shift on rename.
            connection.execute(
                "INSERT OR IGNORE INTO branch_name_reservations VALUES (?, ?, ?)",
                (project_id, names[branch_id], branch_id),
            )
            connection.execute(
                "INSERT INTO branch_name_reservations VALUES (?, ?, ?)",
                (project_id, name, branch_id),
            )
            connection.execute(
                "INSERT INTO branch_names VALUES (?, ?, ?) ON CONFLICT(project_id, branch_id) DO UPDATE SET name = excluded.name",
                (project_id, branch_id, name),
            )
        return name
