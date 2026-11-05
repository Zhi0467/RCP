"""The machines one space runs on, and everything configured on each.

A card is the one owner of a machine's settings: compute, access hint, provider
executables, writable paths, hidden folders, and provider limits. A project only
names which cards it uses.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Mapping

from rcp.config import MachineComputeConfig
from rcp.machine_cards import BACKEND_MACHINE_ID
from rcp.storage.mixin_base import StoreMixinBase
from rcp.storage.models import SpaceMachineHostInUse, SpaceMachineRecord


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
        """The card for `host`; a space keeps one card per host route.

        Only creation and the legacy upgrade look a card up by host. Everything
        else names the card by id.
        """

        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM space_machines WHERE host = ? ORDER BY created_at, machine_id",
                (host,),
            ).fetchone()
        return _record(row) if row else None

    def backend_space_machine(self) -> SpaceMachineRecord:
        """The card for the backend's own machine, `BACKEND_MACHINE_ID`, the one with no host.

        A project on that machine usually made it. Otherwise it is made here with
        no account, which checks nothing, as a Space turn without a card did.
        """

        try:
            return self.space_machine(BACKEND_MACHINE_ID)
        except KeyError:
            self.ensure_space_machines([("", "", "Backend")])
            card = self.space_machine(BACKEND_MACHINE_ID)
        return card

    def provider_shell_timeout_seconds(self, provider: str, machine_id: str) -> int | None:
        """This machine's configured shell timeout for `provider`; None keeps its default."""

        card = (
            self.backend_space_machine()
            if machine_id == BACKEND_MACHINE_ID
            else self.space_machine(machine_id)
        )
        value = card.provider_shell_timeout.get(provider, "")
        return int(value) * 60 if value else None

    def machine_compute(self, machine_id: str) -> MachineComputeConfig | None:
        """This machine's compute settings, as compute jobs read them."""

        return self.space_machine(machine_id).compute_config()

    def provider_binary(self, provider: str, machine_id: str) -> str | None:
        """The executable recorded for `provider` on this machine; None lets RCP discover it."""

        return self.space_machine(machine_id).provider_paths.get(provider)

    def clear_machine_caches(self, machine_id: str) -> None:
        """Drop what was probed through the card's old route: readiness, skills, compute."""

        with self.connection() as connection:
            for table in (
                "provider_readiness_snapshots",
                "provider_skill_inventories",
                "compute_backend_probes",
            ):
                connection.execute(f"DELETE FROM {table} WHERE machine_id = ?", (machine_id,))

    def merge_legacy_machine_settings(
        self,
        host: str,
        *,
        job_manager: str | None,
        jobs_root: str,
        access_hint: str,
        provider_paths: Mapping[str, str],
    ) -> None:
        """Copy one manifest's retired machine settings into a card still pending import.

        Fill-only, so several projects naming one machine merge: Slurm turns on
        when any of them used it, and the first value wins for the rest.
        """

        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM space_machines WHERE host = ? AND legacy_settings_pending = 1",
                (host,),
            ).fetchone()
            if row is None:
                return
            card = _record(row)
            paths = {**provider_paths, **card.provider_paths}
            connection.execute(
                """
                UPDATE space_machines
                SET job_manager = ?, jobs_root = ?, access_hint = ?, provider_paths_json = ?
                WHERE machine_id = ?
                """,
                (
                    "slurm"
                    if job_manager == "slurm" and card.job_manager is None
                    else card.job_manager,
                    card.jobs_root or jobs_root,
                    card.access_hint or access_hint,
                    json.dumps(paths, sort_keys=True),
                    card.machine_id,
                ),
            )

    def decide_job_manager(self, machine_id: str, job_manager: str) -> None:
        """Record a detected job manager unless someone decided first."""

        with self.connection() as connection:
            connection.execute(
                "UPDATE space_machines SET job_manager = ?, updated_at = ? "
                "WHERE machine_id = ? AND job_manager IS NULL",
                (job_manager, self.now(), machine_id),
            )

    def settle_legacy_machine_settings(self, hosts: Iterable[str]) -> None:
        """Every manifest naming these machines was read; their cards own the settings now."""

        with self.connection() as connection:
            connection.executemany(
                "UPDATE space_machines SET legacy_settings_pending = 0 WHERE host = ?",
                [(host,) for host in set(hosts)],
            )

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
                    (_new_machine_id(host), name, host, os_account, now, now, host)
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
        machine_id = _new_machine_id(host)
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
            raise SpaceMachineHostInUse(host) from exc
        return self.space_machine(machine_id)

    def update_space_machine(
        self,
        machine_id: str,
        *,
        name: str | None = None,
        host: str | None = None,
        os_account: str | None = None,
        writable_paths: list[str] | None = None,
        hidden_folders: list[str] | None = None,
        provider_autocompact: Mapping[str, str | None] | None = None,
        provider_shell_timeout: Mapping[str, str | None] | None = None,
        job_manager: str | None = None,
        jobs_root: str | None = None,
        access_hint: str | None = None,
        provider_paths: Mapping[str, str | None] | None = None,
    ) -> SpaceMachineRecord:
        """Write only the fields given, so racing edits of different fields all land.

        Provider settings merge per provider (None removes one). A host another
        card already has raises SpaceMachineHostInUse. The caller validates the
        route and invalidates what the old route taught (`machine_cards`).
        """

        try:
            with self.connection() as connection:
                updated = connection.execute(
                    """
                UPDATE space_machines
                SET name = COALESCE(?, name),
                    host = COALESCE(?, host),
                    os_account = COALESCE(?, os_account),
                    writable_paths_json = COALESCE(?, writable_paths_json),
                    hidden_folders_json = COALESCE(?, hidden_folders_json),
                    provider_autocompact_json = COALESCE(
                        json_patch(provider_autocompact_json, ?), provider_autocompact_json
                    ),
                    provider_shell_timeout_json = COALESCE(
                        json_patch(provider_shell_timeout_json, ?), provider_shell_timeout_json
                    ),
                    job_manager = COALESCE(?, job_manager),
                    jobs_root = COALESCE(?, jobs_root),
                    access_hint = COALESCE(?, access_hint),
                    provider_paths_json = COALESCE(
                        json_patch(provider_paths_json, ?), provider_paths_json
                    ),
                    updated_at = ?
                WHERE machine_id = ?
                """,
                    (
                        name,
                        host,
                        os_account,
                        None if writable_paths is None else json.dumps(writable_paths),
                        None if hidden_folders is None else json.dumps(hidden_folders),
                        None if provider_autocompact is None else json.dumps(provider_autocompact),
                        None
                        if provider_shell_timeout is None
                        else json.dumps(provider_shell_timeout),
                        job_manager,
                        jobs_root,
                        access_hint,
                        None if provider_paths is None else json.dumps(provider_paths),
                        self.now(),
                        machine_id,
                    ),
                ).rowcount
        except sqlite3.IntegrityError as exc:
            raise SpaceMachineHostInUse(host or "") from exc
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


def _new_machine_id(host: str) -> str:
    return uuid.uuid4().hex if host else BACKEND_MACHINE_ID


def _record(row: sqlite3.Row) -> SpaceMachineRecord:
    values = dict(row)
    values["writable_paths"] = json.loads(values.pop("writable_paths_json"))
    values["hidden_folders"] = json.loads(values.pop("hidden_folders_json"))
    values["provider_autocompact"] = json.loads(values.pop("provider_autocompact_json"))
    values["provider_shell_timeout"] = json.loads(values.pop("provider_shell_timeout_json"))
    values["provider_paths"] = json.loads(values.pop("provider_paths_json"))
    values["legacy_settings_pending"] = bool(values["legacy_settings_pending"])
    return SpaceMachineRecord(**values)
