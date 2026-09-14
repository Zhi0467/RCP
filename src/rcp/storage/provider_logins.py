"""Durable provider authentication facts for one execution account."""

from __future__ import annotations

from typing import Literal

from rcp.limits import PROVIDER_LOGIN_DETAIL_MAX_CHARS
from rcp.storage.models import ProviderLoginStateRecord, ProviderReadinessSnapshotRecord


class ProviderLoginStoreMixin:
    def provider_login_state(self, provider: str, host: str) -> ProviderLoginStateRecord:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM provider_login_states WHERE provider = ? AND host = ?",
                (provider, host),
            ).fetchone()
        return (
            ProviderLoginStateRecord(**dict(row))
            if row
            else ProviderLoginStateRecord(provider=provider, host=host)
        )

    def queued_agent_task_ids(self, project_id: str) -> list[str]:
        """Admitted tasks of one project that no dispatch has started yet."""

        with self.connection() as connection:
            rows = connection.execute(
                "SELECT operation_id FROM graph_runs WHERE project_id = ? AND status = 'queued' "
                "ORDER BY created_at, operation_id",
                (project_id,),
            ).fetchall()
        return [str(row["operation_id"]) for row in rows]

    def provider_login_states(self) -> list[ProviderLoginStateRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM provider_login_states ORDER BY provider, host"
            ).fetchall()
        return [ProviderLoginStateRecord(**dict(row)) for row in rows]

    def mark_provider_login_failed(
        self,
        provider: str,
        host: str,
        *,
        generation: int,
        detail: str,
        source: Literal["turn", "report", "probe"],
    ) -> ProviderLoginStateRecord:
        if source not in {"turn", "report", "probe"}:
            raise ValueError("unknown provider login failure source")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM provider_login_states WHERE provider = ? AND host = ?",
                (provider, host),
            ).fetchone()
            current = (
                ProviderLoginStateRecord(**dict(row))
                if row
                else ProviderLoginStateRecord(provider=provider, host=host)
            )
            if generation < current.generation:
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
            connection.execute(
                "DELETE FROM provider_readiness_snapshots WHERE provider = ? AND host = ?",
                (provider, host),
            )
        return record

    def mark_provider_login_verified(
        self,
        provider: str,
        host: str,
        *,
        member_id: str,
        detail: str,
    ) -> ProviderLoginStateRecord:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT generation FROM provider_login_states WHERE provider = ? AND host = ?",
                (provider, host),
            ).fetchone()
            record = ProviderLoginStateRecord(
                provider=provider,
                host=host,
                state="signed_in",
                generation=(row[0] if row else 0) + 1,
                detail=" ".join(detail.split())[:PROVIDER_LOGIN_DETAIL_MAX_CHARS],
                source="verify",
                changed_at=self.now(),
                changed_by=member_id,
            )
            self._write_provider_login_state(connection, record)
        return record

    def mark_provider_login_signed_out(
        self,
        provider: str,
        host: str,
        *,
        member_id: str | None,
        source: Literal["sign_out", "restore"],
        detail: str,
    ) -> ProviderLoginStateRecord:
        """A deliberate sign-out or a restore that lost the credential; fences like a failure."""

        if source not in {"sign_out", "restore"}:
            raise ValueError("unknown provider sign-out source")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT generation FROM provider_login_states WHERE provider = ? AND host = ?",
                (provider, host),
            ).fetchone()
            record = ProviderLoginStateRecord(
                provider=provider,
                host=host,
                state="signed_out",
                generation=(row[0] if row else 0) + 1,
                detail=" ".join(detail.split())[:PROVIDER_LOGIN_DETAIL_MAX_CHARS],
                source=source,
                changed_at=self.now(),
                changed_by=member_id,
            )
            self._write_provider_login_state(connection, record)
        return record

    def provider_readiness_snapshot(
        self, provider: str, host: str, binary: str
    ) -> ProviderReadinessSnapshotRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM provider_readiness_snapshots "
                "WHERE provider = ? AND host = ? AND binary = ?",
                (provider, host, binary),
            ).fetchone()
        return ProviderReadinessSnapshotRecord(**dict(row)) if row else None

    def save_provider_readiness_snapshot(self, record: ProviderReadinessSnapshotRecord) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO provider_readiness_snapshots
                   (provider, host, binary, version, readiness_json, probed_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(provider, host, binary) DO UPDATE SET
                   version=excluded.version, readiness_json=excluded.readiness_json,
                   probed_at=excluded.probed_at""",
                tuple(record.model_dump().values()),
            )

    def delete_provider_readiness_snapshots(self, provider: str, host: str) -> int:
        """Forget every probed answer for one account so the next readiness probes again."""

        with self.connection() as connection:
            cursor = connection.execute(
                "DELETE FROM provider_readiness_snapshots WHERE provider = ? AND host = ?",
                (provider, host),
            )
        return cursor.rowcount

    @staticmethod
    def _write_provider_login_state(connection, record: ProviderLoginStateRecord) -> None:
        connection.execute(
            """INSERT INTO provider_login_states
               (provider, host, state, generation, detail, source, changed_at, changed_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(provider, host) DO UPDATE SET
               state=excluded.state, generation=excluded.generation, detail=excluded.detail,
               source=excluded.source, changed_at=excluded.changed_at, changed_by=excluded.changed_by""",
            tuple(record.model_dump().values()),
        )
