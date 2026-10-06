"""The machines one space runs on, and the extra paths granted on each."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Mapping

from rcp.storage.mixin_base import StoreMixinBase
from rcp.storage.models import SpaceMachineRecord


class SpaceMachineStoreMixin(StoreMixinBase):
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

    def ensure_space_machines(self, machines: Iterable[tuple[str, str, str]]) -> list[str]:
        """Insert a card for each host the space lacks; return hosts whose account conflicts.

        Idempotent: an existing card for the host keeps its name, account, and
        writable paths, even when a manifest leaves the account empty. When
        manifests name two accounts for one host, the card's account is cleared
        rather than one being kept at random; an empty account checks nothing.
        """

        machines = list(machines)
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
            conflicts = []
            for host, os_account, _name in machines:
                cleared = connection.execute(
                    """
                    UPDATE space_machines SET os_account = '', updated_at = ?
                    WHERE host = ? AND ? != '' AND os_account NOT IN ('', ?)
                    """,
                    (now, host, os_account, os_account),
                ).rowcount
                if cleared:
                    conflicts.append(host)
        return conflicts

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
            raise ValueError("this space already has a machine for that host") from exc
        return self.space_machine(machine_id)

    def update_space_machine(
        self,
        machine_id: str,
        *,
        name: str | None = None,
        writable_paths: list[str] | None = None,
        hidden_folders: list[str] | None = None,
        provider_autocompact: Mapping[str, str | None] | None = None,
        provider_shell_timeout: Mapping[str, str | None] | None = None,
    ) -> SpaceMachineRecord:
        # Write only the fields given, so a rename and a path edit racing on one
        # card both land. Provider settings merge per provider (None removes one), so
        # two providers saved at once both land too.
        with self.connection() as connection:
            updated = connection.execute(
                """
                UPDATE space_machines
                SET name = COALESCE(?, name),
                    writable_paths_json = COALESCE(?, writable_paths_json),
                    hidden_folders_json = COALESCE(?, hidden_folders_json),
                    provider_autocompact_json = COALESCE(
                        json_patch(provider_autocompact_json, ?), provider_autocompact_json
                    ),
                    provider_shell_timeout_json = COALESCE(
                        json_patch(provider_shell_timeout_json, ?), provider_shell_timeout_json
                    ),
                    updated_at = ?
                WHERE machine_id = ?
                """,
                (
                    name,
                    None if writable_paths is None else json.dumps(writable_paths),
                    None if hidden_folders is None else json.dumps(hidden_folders),
                    None if provider_autocompact is None else json.dumps(provider_autocompact),
                    None if provider_shell_timeout is None else json.dumps(provider_shell_timeout),
                    self.now(),
                    machine_id,
                ),
            ).rowcount
        if not updated:
            raise KeyError(machine_id)
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
    values["hidden_folders"] = json.loads(values.pop("hidden_folders_json"))
    values["provider_autocompact"] = json.loads(values.pop("provider_autocompact_json"))
    values["provider_shell_timeout"] = json.loads(values.pop("provider_shell_timeout_json"))
    return SpaceMachineRecord(**values)
