"""The machines one space runs on, and the extra paths granted on each."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable

from rcp.storage.models import SpaceMachineRecord


class SpaceMachineStoreMixin:
    def space_machines(self) -> list[SpaceMachineRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM space_machines ORDER BY name, host, os_account"
            ).fetchall()
        return [_record(row) for row in rows]

    def space_machine(self, machine_id: str) -> SpaceMachineRecord:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM space_machines WHERE machine_id = ?", (machine_id,)
            ).fetchone()
        if row is None:
            raise KeyError(machine_id)
        return _record(row)

    def space_machine_for(self, host: str) -> SpaceMachineRecord | None:
        """The card for `host`; a space keeps one card per host route."""

        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM space_machines WHERE host = ? ORDER BY created_at, machine_id",
                (host,),
            ).fetchone()
        return _record(row) if row else None

    def ensure_space_machines(self, machines: Iterable[tuple[str, str, str]]) -> None:
        """Insert a card for each host the space lacks.

        Idempotent: an existing card for the host keeps its name, account, and
        writable paths, even when a manifest leaves the account empty.
        """

        now = self.now()
        with self.connection() as connection:
            connection.executemany(
                """
                INSERT INTO space_machines
                    (machine_id, name, host, os_account, writable_paths_json,
                     created_at, updated_at)
                SELECT ?, ?, ?, ?, '[]', ?, ?
                WHERE NOT EXISTS (SELECT 1 FROM space_machines WHERE host = ?)
                """,
                [
                    (uuid.uuid4().hex, name, host, os_account, now, now, host)
                    for host, os_account, name in machines
                ],
            )

    def create_space_machine(self, *, name: str, host: str, os_account: str) -> SpaceMachineRecord:
        now = self.now()
        machine_id = uuid.uuid4().hex
        try:
            with self.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO space_machines
                        (machine_id, name, host, os_account, writable_paths_json,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, '[]', ?, ?)
                    """,
                    (machine_id, name, host, os_account, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("this space already has a machine for that host and account") from exc
        return self.space_machine(machine_id)

    def update_space_machine(
        self,
        machine_id: str,
        *,
        name: str | None = None,
        writable_paths: list[str] | None = None,
    ) -> SpaceMachineRecord:
        current = self.space_machine(machine_id)
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE space_machines
                SET name = ?, writable_paths_json = ?, updated_at = ?
                WHERE machine_id = ?
                """,
                (
                    current.name if name is None else name,
                    json.dumps(
                        current.writable_paths if writable_paths is None else writable_paths
                    ),
                    self.now(),
                    machine_id,
                ),
            )
        return self.space_machine(machine_id)

    def delete_space_machine(self, machine_id: str) -> None:
        with self.connection() as connection:
            deleted = connection.execute(
                "DELETE FROM space_machines WHERE machine_id = ?", (machine_id,)
            ).rowcount
        if not deleted:
            raise KeyError(machine_id)


def _record(row: sqlite3.Row) -> SpaceMachineRecord:
    values = dict(row)
    values["writable_paths"] = json.loads(values.pop("writable_paths_json"))
    return SpaceMachineRecord(**values)
