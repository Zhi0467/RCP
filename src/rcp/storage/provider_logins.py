"""Durable provider authentication, selection clocks, and member preferences."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Literal
from uuid import uuid4

from rcp.limits import PROVIDER_LOGIN_DETAIL_MAX_CHARS, PROVIDER_LOGIN_LIMIT_HINT_SECONDS
from rcp.storage.mixin_base import StoreMixinBase
from rcp.storage.models import (
    MACHINE_LOGIN_ID,
    LoginKey,
    ProviderLoginAssignmentRecord,
    ProviderLoginSetting,
    ProviderLoginSettingRecord,
    ProviderLoginStateRecord,
    ProviderReadinessSnapshotRecord,
)

RESTORED_LOGIN_DETAIL = (
    "This account was restored from an archive. Verify the sign-in on this machine."
)
_KEY_WHERE = "provider = ? AND host = ? AND login_id = ?"


def migrate_per_member_provider_logins(connection: sqlite3.Connection) -> None:
    """Upgrade the old writer's machine rows without preserving readiness cache."""
    from rcp.providers import PROVIDER_IDS
    from rcp.storage.resource_deadlines import install_provider_readiness_deadline
    from rcp.storage.resource_versions import install_provider_login_versions

    columns = {row[1] for row in connection.execute("PRAGMA table_info(provider_login_states)")}
    if "login_id" not in columns:
        connection.execute("ALTER TABLE provider_login_states RENAME TO provider_login_states_old")
        connection.execute("""CREATE TABLE provider_login_states (
            provider TEXT NOT NULL, host TEXT NOT NULL, login_id TEXT NOT NULL DEFAULT '',
            member_id TEXT, added_by TEXT, added_at TEXT, draining_since TEXT, removed_at TEXT,
            state TEXT NOT NULL CHECK(state IN ('signed_in', 'signed_out')),
            generation INTEGER NOT NULL, detail TEXT, source TEXT,
            changed_at TEXT NOT NULL, changed_by TEXT,
            PRIMARY KEY(provider, host, login_id))""")
        connection.execute("""INSERT INTO provider_login_states
            (provider, host, state, generation, detail, source, changed_at, changed_by)
            SELECT provider, host, state, generation, detail, source, changed_at, changed_by
            FROM provider_login_states_old""")
        connection.execute("DROP TABLE provider_login_states_old")
    connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS provider_login_states_member
        ON provider_login_states(provider, host, member_id) WHERE member_id IS NOT NULL AND removed_at IS NULL""")
    connection.execute("""CREATE TABLE IF NOT EXISTS provider_login_assignments (
        provider TEXT NOT NULL, host TEXT NOT NULL, login_id TEXT NOT NULL,
        last_assigned_at TEXT, limited_at TEXT, PRIMARY KEY(provider, host, login_id))""")
    connection.execute("DROP TABLE IF EXISTS provider_readiness_snapshots")
    connection.execute("""CREATE TABLE provider_readiness_snapshots (
        provider TEXT NOT NULL, host TEXT NOT NULL, login_id TEXT NOT NULL DEFAULT '',
        binary TEXT NOT NULL, version TEXT NOT NULL, readiness_json TEXT NOT NULL,
        probed_at TEXT NOT NULL, PRIMARY KEY(provider, host, login_id, binary))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS provider_login_settings (
        user_id TEXT NOT NULL, provider TEXT NOT NULL, host TEXT NOT NULL,
        setting TEXT NOT NULL CHECK(setting IN ('own','own_then_shared','shared_then_own','shared')),
        changed_at TEXT NOT NULL, PRIMARY KEY(user_id, provider, host))""")
    task_columns = {row[1] for row in connection.execute("PRAGMA table_info(graph_runs)")}
    if "login_id" not in task_columns:
        connection.execute("ALTER TABLE graph_runs ADD COLUMN login_id TEXT")
    connection.execute("UPDATE graph_runs SET login_id = '' WHERE login_id IS NULL")
    for provider in PROVIDER_IDS:
        connection.execute(
            """INSERT OR IGNORE INTO provider_login_settings
            SELECT user_id, ?, '*', 'own_then_shared', strftime('%Y-%m-%dT%H:%M:%fZ','now')
            FROM space_users WHERE identity_kind = 'team_member' AND removal_started_at IS NULL""",
            (provider,),
        )
    for table in (
        "provider_login_states",
        "provider_readiness_snapshots",
        "provider_login_settings",
    ):
        for event in ("insert", "update", "delete"):
            connection.execute(f"DROP TRIGGER IF EXISTS resource_{table}_{event}")
    install_provider_login_versions(connection)
    for event in ("insert", "update", "delete"):
        connection.execute(
            f"DROP TRIGGER IF EXISTS resource_deadline_provider_readiness_snapshots_settings_{event}"
        )
    connection.execute(
        "DELETE FROM resource_deadlines WHERE owner LIKE 'provider_readiness_snapshots:%'"
    )
    install_provider_readiness_deadline(connection)


class ProviderLoginStoreMixin(StoreMixinBase):
    @contextmanager
    def _login_connection(
        self, connection: sqlite3.Connection | None
    ) -> Iterator[sqlite3.Connection]:
        if connection is not None:
            yield connection
        else:
            with self.connection() as opened:
                yield opened

    def provider_login_state(
        self, key: LoginKey, *, connection: sqlite3.Connection | None = None
    ) -> ProviderLoginStateRecord:
        with self._login_connection(connection) as db:
            row = db.execute(
                f"SELECT * FROM provider_login_states WHERE {_KEY_WHERE}", key
            ).fetchone()
        if row:
            return ProviderLoginStateRecord(**dict(row))
        if key.login_id != MACHINE_LOGIN_ID:
            raise ValueError("Unknown provider login.")
        return ProviderLoginStateRecord(
            provider=key.provider,
            host=key.host,
            login_id=key.login_id,
        )

    def queued_agent_task_ids(self, project_id: str) -> list[str]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT operation_id FROM graph_runs WHERE project_id = ? AND status = 'queued' ORDER BY created_at, operation_id",
                (project_id,),
            ).fetchall()
        return [str(row["operation_id"]) for row in rows]

    def provider_login_states(self) -> list[ProviderLoginStateRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM provider_login_states WHERE removed_at IS NULL ORDER BY provider, host, login_id"
            ).fetchall()
        return [ProviderLoginStateRecord(**dict(row)) for row in rows]

    def provider_logins(
        self, provider: str, host: str, *, connection: sqlite3.Connection | None = None
    ) -> list[ProviderLoginStateRecord]:
        with self._login_connection(connection) as db:
            rows = db.execute(
                "SELECT * FROM provider_login_states WHERE provider=? AND host=? AND removed_at IS NULL ORDER BY login_id",
                (provider, host),
            ).fetchall()
        result = [ProviderLoginStateRecord(**dict(row)) for row in rows]
        if not any(row.login_id == MACHINE_LOGIN_ID for row in result):
            result.insert(0, ProviderLoginStateRecord(provider=provider, host=host))
        return result

    def add_provider_login(
        self, provider: str, host: str, *, member_id: str | None = None, added_by: str
    ) -> ProviderLoginStateRecord:
        from rcp.providers import profile_for

        if not profile_for(provider).authentication.manages_login:
            raise ValueError("OpenCode only supports the machine sign-in.")
        record = ProviderLoginStateRecord(
            provider=provider,
            host=host,
            login_id=str(uuid4()),
            member_id=member_id,
            added_by=added_by,
            added_at=self.now(),
            state="signed_out",
            changed_at=self.now(),
        )
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if (
                member_id is not None
                and connection.execute(
                    "SELECT 1 FROM provider_login_states WHERE provider=? AND host=? AND member_id=? AND removed_at IS NULL",
                    (provider, host, member_id),
                ).fetchone()
            ):
                raise ValueError("This member already has an own login for this provider and host.")
            self._write_provider_login_state(connection, record)
        return record

    def mark_provider_login_failed(
        self,
        key: LoginKey,
        *,
        generation: int,
        detail: str,
        source: Literal["turn", "report", "probe"],
    ) -> ProviderLoginStateRecord:
        if source not in {"turn", "report", "probe"}:
            raise ValueError("unknown provider login failure source")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self.provider_login_state(key, connection=connection)
            if generation < current.generation or current.removed_at:
                return current
            record = current.model_copy(
                update={
                    "state": "signed_out",
                    "detail": " ".join(detail.split())[:PROVIDER_LOGIN_DETAIL_MAX_CHARS],
                    "source": source,
                    "changed_at": self.now(),
                    "changed_by": None,
                }
            )
            self._write_provider_login_state(connection, record)
            connection.execute(f"DELETE FROM provider_readiness_snapshots WHERE {_KEY_WHERE}", key)
        return record

    def mark_provider_login_verified(
        self, key: LoginKey, *, member_id: str, detail: str
    ) -> ProviderLoginStateRecord:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self.provider_login_state(key, connection=connection)
            if current.removed_at:
                raise ValueError("A removed provider login cannot be verified.")
            record = current.model_copy(
                update={
                    "state": "signed_in",
                    "generation": current.generation + 1,
                    "detail": " ".join(detail.split())[:PROVIDER_LOGIN_DETAIL_MAX_CHARS],
                    "source": "verify",
                    "changed_at": self.now(),
                    "changed_by": member_id,
                }
            )
            self._write_provider_login_state(connection, record)
            connection.execute(
                f"UPDATE provider_login_assignments SET limited_at=NULL WHERE {_KEY_WHERE}", key
            )
        return record

    def mark_provider_login_signed_out(
        self,
        key: LoginKey,
        *,
        member_id: str | None,
        source: Literal["sign_out", "restore"],
        detail: str,
    ) -> ProviderLoginStateRecord:
        if source not in {"sign_out", "restore"}:
            raise ValueError("unknown provider sign-out source")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self.provider_login_state(key, connection=connection)
            if current.removed_at:
                # Already fenced; its detail may be the pending-cleanup marker.
                return current
            record = current.model_copy(
                update={
                    "state": "signed_out",
                    "generation": current.generation + 1,
                    "detail": " ".join(detail.split())[:PROVIDER_LOGIN_DETAIL_MAX_CHARS],
                    "source": source,
                    "changed_at": self.now(),
                    "changed_by": member_id,
                }
            )
            self._write_provider_login_state(connection, record)
        return record

    def set_provider_login_draining(self, key: LoginKey) -> ProviderLoginStateRecord:
        if not key.login_id:
            raise ValueError("The machine sign-in cannot be removed.")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self.provider_login_state(key, connection=connection)
            if current.removed_at:
                raise ValueError("Provider login has already been removed.")
            record = current.model_copy(
                update={"draining_since": current.draining_since or self.now()}
            )
            self._write_provider_login_state(connection, record)
        return record

    def finish_provider_login_removal(
        self, key: LoginKey, *, member_id: str | None = None
    ) -> ProviderLoginStateRecord:
        if not key.login_id:
            raise ValueError("The machine sign-in cannot be removed.")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self.provider_login_state(key, connection=connection)
            if current.removed_at:
                return current
            record = current.model_copy(
                update={
                    "state": "signed_out",
                    "generation": current.generation + 1,
                    "removed_at": self.now(),
                    "changed_at": self.now(),
                    "changed_by": member_id,
                    "source": "sign_out",
                }
            )
            self._write_provider_login_state(connection, record)
            connection.execute(f"DELETE FROM provider_readiness_snapshots WHERE {_KEY_WHERE}", key)
        return record

    def provider_readiness_snapshot(
        self, key: LoginKey, binary: str
    ) -> ProviderReadinessSnapshotRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                f"SELECT * FROM provider_readiness_snapshots WHERE {_KEY_WHERE} AND binary=?",
                (*key, binary),
            ).fetchone()
        return ProviderReadinessSnapshotRecord(**dict(row)) if row else None

    def save_provider_readiness_snapshot(
        self, key: LoginKey, record: ProviderReadinessSnapshotRecord
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO provider_readiness_snapshots
                (provider,host,login_id,binary,version,readiness_json,probed_at) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(provider,host,login_id,binary) DO UPDATE SET version=excluded.version,
                readiness_json=excluded.readiness_json,probed_at=excluded.probed_at""",
                (*key, record.binary, record.version, record.readiness_json, record.probed_at),
            )

    def detach_provider_logins_for_restore(
        self, connection: sqlite3.Connection, *, now: str
    ) -> None:
        rows = connection.execute(
            "SELECT * FROM provider_login_states WHERE state='signed_in'"
        ).fetchall()
        for row in rows:
            current = ProviderLoginStateRecord(**dict(row))
            self._write_provider_login_state(
                connection,
                current.model_copy(
                    update={
                        "state": "signed_out",
                        "generation": current.generation + 1,
                        "detail": RESTORED_LOGIN_DETAIL,
                        "source": "restore",
                        "changed_at": now,
                        "changed_by": None,
                    }
                ),
            )
        connection.execute("DELETE FROM provider_readiness_snapshots")

    def delete_provider_readiness_snapshots(self, key: LoginKey) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                f"DELETE FROM provider_readiness_snapshots WHERE {_KEY_WHERE}", key
            )
        return cursor.rowcount

    def provider_login_setting(
        self,
        user_id: str,
        provider: str,
        host: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> ProviderLoginSettingRecord:
        with self._login_connection(connection) as db:
            row = db.execute(
                "SELECT * FROM provider_login_settings WHERE user_id=? AND provider=? AND host IN (?, '*') ORDER BY CASE WHEN host=? THEN 0 ELSE 1 END LIMIT 1",
                (user_id, provider, host, host),
            ).fetchone()
            if row:
                return ProviderLoginSettingRecord(**dict(row))
            kind = db.execute("SELECT space_kind FROM space_identity WHERE singleton=1").fetchone()[
                0
            ]
        return ProviderLoginSettingRecord(
            user_id=user_id,
            provider=provider,
            host=host,
            setting="own" if kind == "team" else "shared",
        )

    def set_provider_login_setting(
        self, user_id: str, provider: str, host: str, setting: ProviderLoginSetting
    ) -> ProviderLoginSettingRecord:
        if host == "*":
            raise ValueError("Only the migration may write fallback settings.")
        record = ProviderLoginSettingRecord(
            user_id=user_id, provider=provider, host=host, setting=setting, changed_at=self.now()
        )
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO provider_login_settings VALUES(?,?,?,?,?)
                ON CONFLICT(user_id,provider,host) DO UPDATE SET setting=excluded.setting,changed_at=excluded.changed_at""",
                tuple(record.model_dump().values()),
            )
        return record

    def provider_login_assignment(
        self, key: LoginKey, *, connection: sqlite3.Connection | None = None
    ) -> ProviderLoginAssignmentRecord:
        with self._login_connection(connection) as db:
            row = db.execute(
                f"SELECT * FROM provider_login_assignments WHERE {_KEY_WHERE}", key
            ).fetchone()
        return (
            ProviderLoginAssignmentRecord(**dict(row))
            if row
            else ProviderLoginAssignmentRecord(
                provider=key.provider, host=key.host, login_id=key.login_id
            )
        )

    def provider_login_limited(
        self,
        key: LoginKey,
        *,
        continued: bool = False,
        now: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        mark = self.provider_login_assignment(key, connection=connection).limited_at
        return mark is not None and (
            continued
            or (
                datetime.fromisoformat(now or self.now()) - datetime.fromisoformat(mark)
            ).total_seconds()
            < PROVIDER_LOGIN_LIMIT_HINT_SECONDS
        )

    def set_provider_login_limit(
        self, key: LoginKey, *, provider_started_at: str, limited: bool
    ) -> bool:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self.provider_login_assignment(key, connection=connection)
            if current.limited_at is not None and datetime.fromisoformat(
                provider_started_at
            ) <= datetime.fromisoformat(current.limited_at):
                return False
            connection.execute(
                """INSERT INTO provider_login_assignments(provider,host,login_id,limited_at) VALUES(?,?,?,?)
                ON CONFLICT(provider,host,login_id) DO UPDATE SET limited_at=excluded.limited_at""",
                (*key, self.now() if limited else None),
            )
        return True

    def record_provider_login_refusal(self, operation_id: str, key: LoginKey) -> None:
        """Retain the concrete refused binding without advancing the assignment clock."""
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE graph_runs SET login_id=? WHERE operation_id=?",
                (key.login_id, operation_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Unknown agent task.")

    def assign_provider_login(
        self, operation_id: str, select: Callable[[sqlite3.Connection], LoginKey | None]
    ) -> LoginKey | None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            key = select(connection)
            if key is None:
                return None
            cursor = connection.execute(
                "UPDATE graph_runs SET login_id=? WHERE operation_id=?",
                (key.login_id, operation_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Unknown agent task.")
            if (
                key.login_id == MACHINE_LOGIN_ID
                and not connection.execute(
                    f"SELECT 1 FROM provider_login_states WHERE {_KEY_WHERE}", key
                ).fetchone()
            ):
                self._write_provider_login_state(
                    connection, self.provider_login_state(key, connection=connection)
                )
            connection.execute(
                """INSERT INTO provider_login_assignments(provider,host,login_id,last_assigned_at) VALUES(?,?,?,?)
                ON CONFLICT(provider,host,login_id) DO UPDATE SET last_assigned_at=excluded.last_assigned_at""",
                (*key, self.now()),
            )
        return key

    @staticmethod
    def _write_provider_login_state(
        connection: sqlite3.Connection, record: ProviderLoginStateRecord
    ) -> None:
        fields = record.model_dump()
        columns = ", ".join(fields)
        updates = ", ".join(
            f"{name}=excluded.{name}"
            for name in fields
            if name not in {"provider", "host", "login_id"}
        )
        connection.execute(
            f"INSERT INTO provider_login_states ({columns}) VALUES ({', '.join('?' for _ in fields)}) ON CONFLICT(provider,host,login_id) DO UPDATE SET {updates}",
            tuple(fields.values()),
        )
