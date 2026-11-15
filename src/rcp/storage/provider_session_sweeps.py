"""Which provider sessions RCP started may be swept, and which it already swept.

A session is held while any live record names it; otherwise it is eligible once
its newest `graph_runs` row is older than the retention window. A swept id is
marked before its provider deletes it, so no later turn resumes it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel

from rcp.limits import PROVIDER_SESSION_RETENTION_DAYS, PROVIDER_SESSION_SWEEP_MAX_ATTEMPTS
from rcp.storage.mixin_base import StoreMixinBase

if TYPE_CHECKING:
    from rcp.storage import AppStore


class ProviderSessionSweepTarget(BaseModel):
    provider: str
    machine_id: str
    session_id: str
    marked: bool
    cwd: str | None = None


def migrate_provider_session_sweeps(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS provider_session_sweeps (
        provider TEXT NOT NULL, execution_machine TEXT NOT NULL,
        native_session_id TEXT NOT NULL, marked_at TEXT NOT NULL,
        deleted_at TEXT, last_error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
        abandoned_at TEXT,
        PRIMARY KEY (provider, execution_machine, native_session_id))""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS provider_session_sweeps_session "
        "ON provider_session_sweeps(native_session_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS graph_runs_native_session ON graph_runs(native_session_id)"
    )
    connection.execute("""CREATE TABLE IF NOT EXISTS provider_session_sweep_state (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1), restored_at TEXT)""")


_OPEN_EPISODES = """
    SELECT episode_id FROM episodes
    WHERE status IN ('queued','running','stopping','wrapping_up','needs_action')
       OR wrapup_state IN ('pending','running')"""

_NAMING = """
    naming(session_id, operation_id) AS (
        SELECT native_session_id, operation_id FROM graph_runs
        WHERE native_session_id IS NOT NULL AND native_session_id != ''
        UNION
        SELECT json_extract(request_json, '$.fork_session_id'), operation_id FROM graph_runs
        WHERE json_extract(request_json, '$.fork_session_id') IS NOT NULL
    )"""

_HELD = f"""
    held(session_id) AS (
        SELECT naming.session_id FROM naming JOIN graph_runs AS run USING (operation_id)
        WHERE run.status IN ('queued','running','pausing','paused')
           OR run.episode_id IN ({_OPEN_EPISODES})
        UNION
        SELECT source.native_session_id FROM chat_forks AS fork
        JOIN graph_runs AS source
          ON source.operation_id = json_extract(fork.record_json, '$.origin.source_operation_id')
        WHERE COALESCE(json_extract(fork.record_json, '$.detached'), 0) = 0
          AND NOT EXISTS (
              SELECT 1 FROM graph_runs AS turn
              WHERE turn.project_id = fork.project_id
                AND json_extract(turn.request_json, '$.chat_id') = fork.chat_id
                AND turn.native_session_id IS NOT NULL AND turn.native_session_id != '')
        UNION
        SELECT json_extract(origin_json, '$.native_session_id') FROM questions
        WHERE state = 'pending'
           OR (state = 'answered' AND owner_kind = 'chat' AND withdrawn_readonly = 0
               AND client_receipt_revision IS NULL AND followup_operation_id IS NULL)
        UNION
        SELECT run.native_session_id FROM watchers AS watcher
        JOIN graph_runs AS run ON run.operation_id = watcher.origin_operation_id
        WHERE watcher.status IN ('active','degraded')
        UNION
        SELECT native_session_id FROM writing_sessions
        UNION
        SELECT run.native_session_id FROM paper_change_groups AS change_group
        JOIN graph_runs AS run ON run.operation_id = change_group.task_id
        WHERE change_group.state IN ('pending','outdated')
        UNION
        SELECT native_session_id FROM experiment_episode_state
        WHERE episode_id IN ({_OPEN_EPISODES})
        UNION
        SELECT native_session_id FROM episode_wrapups
        WHERE episode_id IN ({_OPEN_EPISODES}) OR state IN ('pending','running')
    )"""

_ELIGIBLE = f"""
    WITH {_NAMING}, {_HELD},
    aged(session_id, last_used) AS (
        SELECT naming.session_id, MAX(run.updated_at) FROM naming
        JOIN graph_runs AS run USING (operation_id) GROUP BY naming.session_id
    ),
    latest AS (
        SELECT run.*, aged.last_used, ROW_NUMBER() OVER (
            PARTITION BY run.native_session_id ORDER BY run.created_at DESC, run.rowid DESC
        ) AS position
        FROM graph_runs AS run JOIN aged ON aged.session_id = run.native_session_id
    )
    SELECT latest.native_session_id AS session_id,
           json_extract(latest.request_json, '$.provider') AS provider,
           latest.stage_machine_id AS machine_id, latest.project_id, latest.kind,
           json_extract(latest.request_json, '$.chat_id') AS chat_id, latest.stage_root
    FROM latest
    WHERE latest.position = 1
      AND latest.stage_machine_id IS NOT NULL AND latest.stage_machine_id != ''
      AND json_extract(latest.request_json, '$.provider') IS NOT NULL
      AND latest.last_used < :cutoff
      AND (:restored_at IS NULL OR latest.last_used > :restored_at)
      AND NOT EXISTS (
          SELECT 1 FROM held WHERE held.session_id = latest.native_session_id)
      AND NOT EXISTS (
          SELECT 1 FROM provider_session_sweeps AS sweep
          WHERE sweep.native_session_id = latest.native_session_id)
      AND (:selected IS NULL OR latest.native_session_id IN (SELECT value FROM json_each(:selected)))
    ORDER BY latest.last_used ASC
"""


_PENDING = """
    WITH latest AS (
        SELECT run.native_session_id, run.stage_machine_id, run.project_id, run.kind,
               run.stage_root, json_extract(run.request_json, '$.chat_id') AS chat_id,
               ROW_NUMBER() OVER (
                   PARTITION BY run.native_session_id ORDER BY run.created_at DESC, run.rowid DESC
               ) AS position
        FROM graph_runs AS run
        WHERE run.native_session_id IN (
            SELECT native_session_id FROM provider_session_sweeps
            WHERE deleted_at IS NULL AND abandoned_at IS NULL)
    )
    SELECT sweep.provider, sweep.execution_machine AS machine_id,
           sweep.native_session_id AS session_id, latest.project_id, latest.kind,
           latest.chat_id, latest.stage_root
    FROM provider_session_sweeps AS sweep
    LEFT JOIN latest
      ON latest.native_session_id = sweep.native_session_id AND latest.position = 1
    WHERE sweep.deleted_at IS NULL AND sweep.abandoned_at IS NULL
    ORDER BY sweep.marked_at, sweep.native_session_id
"""


class ProviderSessionSweepStoreMixin(StoreMixinBase):
    def provider_session_swept(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        with self.connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM provider_session_sweeps WHERE native_session_id = ?",
                    (session_id,),
                ).fetchone()
                is not None
            )

    def provider_session_sweep_targets(self, now: str) -> list[ProviderSessionSweepTarget]:
        """Marked sessions still to delete, then eligible unmarked ones, oldest first."""

        with self.connection() as connection:
            targets = [
                self._sweep_target(connection, row, marked=True)
                for row in connection.execute(_PENDING).fetchall()
            ]
            for row in self._eligible_sessions(connection, now, None):
                targets.append(self._sweep_target(connection, row, marked=False))
        return targets

    def mark_provider_sessions_swept(
        self, targets: list[ProviderSessionSweepTarget], now: str
    ) -> list[ProviderSessionSweepTarget]:
        """Mark the targets that are still eligible; a later turn then starts fresh."""

        if not targets:
            return []
        wanted = {(target.provider, target.machine_id, target.session_id) for target in targets}
        selected = json.dumps(sorted({target.session_id for target in targets}))
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            marked = [
                self._sweep_target(connection, row, marked=True)
                for row in self._eligible_sessions(connection, now, selected)
                if (row["provider"], row["machine_id"], row["session_id"]) in wanted
            ]
            connection.executemany(
                "INSERT INTO provider_session_sweeps "
                "(provider, execution_machine, native_session_id, marked_at) VALUES (?, ?, ?, ?)",
                [(item.provider, item.machine_id, item.session_id, now) for item in marked],
            )
        return marked

    def record_provider_session_sweep_results(
        self, results: list[tuple[ProviderSessionSweepTarget, str | None]], now: str
    ) -> list[ProviderSessionSweepTarget]:
        """Record each deletion and return the marks this call abandoned.

        Gone sets `deleted_at`. A failure keeps the mark for a later pass until
        `PROVIDER_SESSION_SWEEP_MAX_ATTEMPTS` failures abandon it; it stays swept.
        """

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "UPDATE provider_session_sweeps SET deleted_at = ?, last_error = ?, "
                "attempts = attempts + (? IS NOT NULL), "
                "abandoned_at = CASE WHEN ? IS NOT NULL AND attempts + 1 >= ? THEN ? END "
                "WHERE provider = ? AND execution_machine = ? AND native_session_id = ?",
                [
                    (
                        now if error is None else None,
                        error,
                        error,
                        error,
                        PROVIDER_SESSION_SWEEP_MAX_ATTEMPTS,
                        now,
                        target.provider,
                        target.machine_id,
                        target.session_id,
                    )
                    for target, error in results
                ],
            )
            return [
                target
                for target, error in results
                if error is not None
                and connection.execute(
                    "SELECT 1 FROM provider_session_sweeps WHERE provider = ? "
                    "AND execution_machine = ? AND native_session_id = ? AND abandoned_at = ?",
                    (target.provider, target.machine_id, target.session_id, now),
                ).fetchone()
                is not None
            ]

    def unmark_provider_sessions(self, targets: list[ProviderSessionSweepTarget]) -> None:
        """Drop the marks of sessions in use outside RCP; they may resume and age again."""

        with self.connection() as connection:
            connection.executemany(
                "DELETE FROM provider_session_sweeps "
                "WHERE provider = ? AND execution_machine = ? AND native_session_id = ?",
                [(target.provider, target.machine_id, target.session_id) for target in targets],
            )

    def abandon_provider_session_marks(
        self, machine_id: str, reason: str, now: str
    ) -> list[ProviderSessionSweepTarget]:
        """Settle every pending mark on a machine RCP can no longer reach by id."""

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "UPDATE provider_session_sweeps SET abandoned_at = ?, last_error = ? "
                "WHERE execution_machine = ? AND deleted_at IS NULL AND abandoned_at IS NULL "
                "RETURNING provider, execution_machine AS machine_id, "
                "native_session_id AS session_id",
                (now, reason, machine_id),
            ).fetchall()
        return [
            ProviderSessionSweepTarget(
                provider=row["provider"],
                machine_id=row["machine_id"],
                session_id=row["session_id"],
                marked=True,
            )
            for row in sorted(rows, key=lambda row: (row["provider"], row["session_id"]))
        ]

    @staticmethod
    def detach_provider_session_sweeps_for_restore(
        connection: sqlite3.Connection, *, now: str
    ) -> None:
        """A restored database may predate the hosts; only later use may age a session."""

        connection.execute(
            "INSERT INTO provider_session_sweep_state (singleton, restored_at) VALUES (1, ?) "
            "ON CONFLICT(singleton) DO UPDATE SET restored_at = excluded.restored_at",
            (now,),
        )

    @staticmethod
    def _eligible_sessions(
        connection: sqlite3.Connection, now: str, selected: str | None
    ) -> list[sqlite3.Row]:
        restored = connection.execute(
            "SELECT restored_at FROM provider_session_sweep_state WHERE singleton = 1"
        ).fetchone()
        cutoff = datetime.fromisoformat(now) - timedelta(days=PROVIDER_SESSION_RETENTION_DAYS)
        return connection.execute(
            _ELIGIBLE,
            {
                "cutoff": cutoff.isoformat(),
                "restored_at": None if restored is None else restored["restored_at"],
                "selected": selected,
            },
        ).fetchall()

    def _sweep_target(
        self, connection: sqlite3.Connection, row: sqlite3.Row, *, marked: bool
    ) -> ProviderSessionSweepTarget:
        return ProviderSessionSweepTarget(
            provider=row["provider"],
            machine_id=row["machine_id"],
            session_id=row["session_id"],
            marked=marked,
            cwd=self._chat_cwd(connection, row),
        )

    def _chat_cwd(self, connection: sqlite3.Connection, row: sqlite3.Row) -> str | None:
        if row["kind"] not in {"node_chat", "project_chat"} or not (
            row["stage_root"] and row["chat_id"] and row["machine_id"]
        ):
            return None
        return cast("AppStore", self)._chat_stage_workspace(
            connection,
            project_id=row["project_id"],
            kind=row["kind"],
            chat_id=row["chat_id"],
            stage_machine_id=row["machine_id"],
            stage_root=row["stage_root"],
        )
