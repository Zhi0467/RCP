from __future__ import annotations

import sqlite3

from rcp.policies.project import is_member


def live_project_member(connection: sqlite3.Connection, project_id: str, user_id: str) -> bool:
    """Load one seat's membership facts in the caller's transaction and judge them."""

    row = connection.execute(
        "SELECT project.retired_at, user.removal_started_at "
        "FROM project_members AS member "
        "JOIN projects AS project ON project.project_id = member.project_id "
        "JOIN space_users AS user ON user.user_id = member.user_id "
        "WHERE member.project_id = ? AND member.user_id = ?",
        (project_id, user_id),
    ).fetchone()
    return row is not None and is_member(
        project_retired=row["retired_at"] is not None,
        # A schema CHECK makes removed_at imply removal_started_at.
        member_removing=row["removal_started_at"] is not None,
    )
