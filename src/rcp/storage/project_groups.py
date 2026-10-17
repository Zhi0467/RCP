"""Project groups: a space-local container for related projects.

A group is operational SQLite state only. It never enters canonical history,
so replay, Patch logs and project identity are unaffected; a project belongs to
at most one group through ``projects.group_id``. A member sees a group when they
belong to one of its live projects, or created it while it is still empty.
"""

from __future__ import annotations

import sqlite3
import uuid

from rcp.limits import PROJECT_GROUP_NAME_MAX_CHARS
from rcp.storage.mixin_base import StoreMixinBase
from rcp.storage.models import ProjectGroupRecord


class ProjectGroupError(ValueError):
    def __init__(self, code: str):
        self.code = code
        messages = {
            "project_group_not_found": "This project group no longer exists.",
            "project_group_not_empty": "Move this group's projects out before deleting it.",
            "project_group_name_invalid": (
                f"Enter a group name of at most {PROJECT_GROUP_NAME_MAX_CHARS} characters."
            ),
        }
        super().__init__(messages[code])


def migrate_project_groups(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS project_groups (group_id TEXT PRIMARY KEY, "
        "name TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL)"
    )
    columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
    if "group_id" not in columns:
        connection.execute("ALTER TABLE projects ADD COLUMN group_id TEXT")
    connection.execute("CREATE INDEX IF NOT EXISTS projects_group ON projects(group_id)")


_VISIBLE = """
    EXISTS (
        SELECT 1 FROM projects AS project
        JOIN project_members AS member ON member.project_id = project.project_id
        WHERE project.group_id = project_groups.group_id
          AND project.retired_at IS NULL AND member.user_id = :user_id
    )
    OR (
        project_groups.created_by = :user_id
        AND NOT EXISTS (
            SELECT 1 FROM projects AS project
            WHERE project.group_id = project_groups.group_id AND project.retired_at IS NULL
        )
    )
"""


def _clean_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned or len(cleaned) > PROJECT_GROUP_NAME_MAX_CHARS:
        raise ProjectGroupError("project_group_name_invalid")
    return cleaned


def _record(row: sqlite3.Row) -> ProjectGroupRecord:
    return ProjectGroupRecord(
        group_id=row["group_id"],
        name=row["name"],
        created_at=row["created_at"],
        created_by=row["created_by"],
    )


class ProjectGroupStoreMixin(StoreMixinBase):
    def project_groups(self, user_id: str) -> list[ProjectGroupRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM project_groups WHERE {_VISIBLE} "
                "ORDER BY name COLLATE NOCASE, group_id",
                {"user_id": user_id},
            ).fetchall()
        return [_record(row) for row in rows]

    def _visible_group(
        self, connection: sqlite3.Connection, group_id: str, user_id: str
    ) -> ProjectGroupRecord:
        row = connection.execute(
            f"SELECT * FROM project_groups WHERE group_id = :group_id AND ({_VISIBLE})",
            {"group_id": group_id, "user_id": user_id},
        ).fetchone()
        if row is None:
            raise ProjectGroupError("project_group_not_found")
        return _record(row)

    def create_project_group(self, name: str, *, user_id: str) -> ProjectGroupRecord:
        record = ProjectGroupRecord(
            group_id=str(uuid.uuid4()),
            name=_clean_name(name),
            created_at=self.now(),
            created_by=user_id,
        )
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO project_groups (group_id, name, created_at, created_by) "
                "VALUES (?, ?, ?, ?)",
                (record.group_id, record.name, record.created_at, record.created_by),
            )
        return record

    def rename_project_group(self, group_id: str, name: str, *, user_id: str) -> ProjectGroupRecord:
        cleaned = _clean_name(name)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = self._visible_group(connection, group_id, user_id)
            connection.execute(
                "UPDATE project_groups SET name = ? WHERE group_id = ?", (cleaned, group_id)
            )
        return record.model_copy(update={"name": cleaned})

    def delete_project_group(self, group_id: str, *, user_id: str) -> None:
        """Delete an empty group; a group still holding a live project is refused."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._visible_group(connection, group_id, user_id)
            if connection.execute(
                "SELECT 1 FROM projects WHERE group_id = ? AND retired_at IS NULL", (group_id,)
            ).fetchone():
                raise ProjectGroupError("project_group_not_empty")
            connection.execute(
                "UPDATE projects SET group_id = NULL WHERE group_id = ?", (group_id,)
            )
            connection.execute("DELETE FROM project_groups WHERE group_id = ?", (group_id,))

    def set_project_group(self, project_id: str, group_id: str | None, *, user_id: str) -> None:
        """Move one project into a group the member can see, or out of any group."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if group_id is not None:
                self._visible_group(connection, group_id, user_id)
            connection.execute(
                "UPDATE projects SET group_id = ? WHERE project_id = ? AND retired_at IS NULL",
                (group_id, project_id),
            )

    def project_group_project_ids(self, project_id: str) -> list[str]:
        """Every live project sharing this project's group, itself included, by name."""
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT sibling.project_id FROM projects AS own
                JOIN projects AS sibling ON sibling.group_id = own.group_id
                WHERE own.project_id = ? AND sibling.retired_at IS NULL
                ORDER BY sibling.name COLLATE NOCASE, sibling.project_id
                """,
                (project_id,),
            ).fetchall()
        return [row["project_id"] for row in rows] or [project_id]
