from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from rcp.artifacts import AgentArtifactDescriptor
from rcp.limits import ARTIFACT_IMPORT_MAX_RETRY_SECONDS, ARTIFACT_IMPORT_RETRY_SECONDS
from rcp.storage.models import AgentTaskRecord


def migrate_artifact_imports(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE artifact_imports (artifact_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
        "state TEXT NOT NULL, attempts INTEGER NOT NULL, next_attempt_at TEXT, reason TEXT)"
    )
    connection.execute(
        "CREATE INDEX artifact_imports_project ON artifact_imports(project_id, next_attempt_at)"
    )


class ArtifactImportStoreMixin:
    def artifact_import_status(self, artifact_id: str) -> dict | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_imports WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        return dict(row) if row else None

    def artifact_import_tasks(self, project_id: str, *, limit: int) -> list[AgentTaskRecord]:
        """Read all history, including hidden turns, but only tasks with due missing outputs."""
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT r.* FROM graph_runs r WHERE r.project_id = ? AND EXISTS (
                    SELECT 1 FROM json_each(r.result_json, '$.artifacts') d
                    LEFT JOIN artifacts a ON a.artifact_id = CASE WHEN d.type = 'object'
                        THEN json_extract(d.value, '$.artifact_id') END
                    LEFT JOIN artifact_imports i
                        ON i.artifact_id = CASE WHEN d.type = 'object'
                        THEN json_extract(d.value, '$.artifact_id') END
                    WHERE CASE WHEN d.type = 'object'
                        THEN json_type(d.value, '$.artifact_id') = 'text' ELSE 0 END
                    AND (a.artifact_id IS NULL OR i.state = 'unavailable') AND (
                        i.artifact_id IS NULL OR
                        (i.state = 'unavailable' AND i.next_attempt_at <= ?)
                    )
                ) ORDER BY EXISTS (
                    SELECT 1 FROM json_each(r.result_json, '$.artifacts') kept
                    WHERE CASE WHEN kept.type = 'object'
                        THEN json_extract(kept.value, '$.kept_filename') IS NOT NULL ELSE 0 END
                ) DESC, r.history_only, r.created_at DESC, r.operation_id LIMIT ?
                """,
                (project_id, self.now(), limit),
            ).fetchall()
        return [self._agent_task_record(row) for row in rows]

    def record_artifact_import_failure(
        self, project_id: str, artifact_id: str, reason: str, *, permanent: bool
    ) -> None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT attempts FROM artifact_imports WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            attempts = (previous[0] if previous else 0) + 1
            delay = min(
                ARTIFACT_IMPORT_MAX_RETRY_SECONDS,
                ARTIFACT_IMPORT_RETRY_SECONDS * 2 ** min(attempts - 1, 20),
            )
            retry_at = (
                None
                if permanent
                else (datetime.fromisoformat(self.now()) + timedelta(seconds=delay)).isoformat()
            )
            connection.execute(
                "INSERT OR REPLACE INTO artifact_imports VALUES (?, ?, ?, ?, ?, ?)",
                (
                    artifact_id,
                    project_id,
                    "missing" if permanent else "unavailable",
                    attempts,
                    retry_at,
                    reason,
                ),
            )

    def complete_artifact_import(
        self,
        project_id: str,
        descriptor: AgentArtifactDescriptor,
        *,
        candidate_operation_id: str | None = None,
    ) -> None:
        """Commit the retry receipt and candidate's ordinary turn attachment together."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if candidate_operation_id is not None:
                row = connection.execute(
                    "SELECT result_json FROM graph_runs WHERE operation_id = ? AND project_id = ?",
                    (candidate_operation_id, project_id),
                ).fetchone()
                if row is None:
                    raise ValueError("artifact candidate's turn is unavailable")
                result = json.loads(row[0]) if row[0] else {}
                artifacts = result.setdefault("artifacts", [])
                if not any(item.get("artifact_id") == descriptor.artifact_id for item in artifacts):
                    artifacts.append(descriptor.model_dump(mode="json"))
                    connection.execute(
                        "UPDATE graph_runs SET result_json = ? WHERE operation_id = ?",
                        (self._bounded_result_json(result), candidate_operation_id),
                    )
            connection.execute(
                "INSERT OR REPLACE INTO artifact_imports VALUES (?, ?, 'imported', 0, NULL, NULL)",
                (descriptor.artifact_id, project_id),
            )

    def artifact_import_retry_delay(self, project_id: str) -> float | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT MIN(next_attempt_at) FROM artifact_imports "
                "WHERE project_id = ? AND state = 'unavailable'",
                (project_id,),
            ).fetchone()
        return (
            max(
                0,
                (
                    datetime.fromisoformat(row[0]) - datetime.fromisoformat(self.now())
                ).total_seconds(),
            )
            if row[0]
            else None
        )
