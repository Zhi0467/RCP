from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Literal

from pydantic import BaseModel

from rcp.artifacts import AgentArtifactDescriptor
from rcp.limits import (
    AGENT_TASK_ESTIMATE_HISTORY_LIMIT,
    AGENT_TASK_ESTIMATE_SAMPLE_LIMIT,
    AGENT_TASK_EVENT_LIST_DEFAULT_LIMIT,
    AGENT_TASK_EVENT_LIST_MAX_LIMIT,
    AGENT_TASK_EVENT_RETENTION_COUNT,
    AGENT_TASK_LIST_DEFAULT_LIMIT,
    AGENT_TASK_LIST_MAX_LIMIT,
    AGENT_TASK_RECEIPT_LIST_LIMIT,
    AGENT_TASK_RECEIPT_MAX_BYTES,
    AGENT_TASK_RECEIPT_RETENTION_COUNTS,
    AGENT_TASK_RESULT_MAX_BYTES,
    CHAT_ARTIFACT_MAX_COUNT,
    PATCH_OUTPUT_RETENTION_DAYS,
    RUN_TRACE_RETENTION_DAYS,
    WRITING_SESSION_RETENTION_DAYS,
    WRITING_SESSIONS_PER_PROJECT,
)


class ProjectRecord(BaseModel):
    project_id: str
    locator: str
    name: str
    state_location: str
    state_remote: bool
    added_at: str
    last_opened_at: str | None = None
    revision: int | None = None
    primary_question: str | None = None
    attention_count: int = 0
    last_refresh_at: str | None = None
    reachable: bool | None = None
    error: str | None = None


class ProjectStageRecord(BaseModel):
    host: str
    root: str


AgentTaskKind = Literal[
    "seed",
    "refresh",
    "node_chat",
    "project_chat",
    "paper_coach",
]
AgentTaskStatus = Literal[
    "queued",
    "running",
    "pausing",
    "paused",
    "succeeded",
    "failed",
    "interrupted",
]
AgentTaskReceiptTier = Literal["summary", "diagnostic", "trace"]

class AgentTaskEventRecord(BaseModel):
    event_id: int
    operation_id: str
    created_at: str
    level: Literal["info", "warning", "error"]
    message: str


class AgentTaskReceiptRecord(BaseModel):
    receipt_id: int
    operation_id: str
    created_at: str
    tier: AgentTaskReceiptTier
    category: str
    payload: dict[str, object]


class AgentTaskRecord(BaseModel):
    operation_id: str
    project_id: str
    kind: AgentTaskKind
    status: AgentTaskStatus
    request: dict[str, object]
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    status_message: str
    error: str | None = None
    applied_revision: int | None = None
    result: dict[str, object] | None = None
    attempt: int = 1
    parent_operation_id: str | None = None
    native_session_id: str | None = None
    stage_host: str | None = None
    stage_root: str | None = None
    estimate_seconds: float = 300.0
    estimate_samples: int = 0
    phase: str = "queued"
    last_activity_at: str | None = None
    elapsed_seconds: float = 0.0
    progress: float = 0.0
    can_pause: bool = False
    can_resume: bool = False
    can_retry: bool = False


class AppStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS paper_drafts (
                    project_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    base_hash TEXT,
                    updated_at TEXT NOT NULL,
                    cursor_state TEXT
                );
                CREATE TABLE IF NOT EXISTS writing_sessions (
                    native_session_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    execution_machine TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    title TEXT,
                    model TEXT NOT NULL,
                    reasoning TEXT,
                    created_at TEXT NOT NULL,
                    last_resumed_at TEXT NOT NULL,
                    introduction_hash_examined TEXT NOT NULL,
                    graph_revision_examined INTEGER NOT NULL,
                    research_md_hash_examined TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS writing_sessions_project
                    ON writing_sessions(project_id, last_resumed_at DESC);
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    locator TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    state_location TEXT NOT NULL,
                    state_remote INTEGER NOT NULL,
                    added_at TEXT NOT NULL,
                    last_opened_at TEXT,
                    revision INTEGER,
                    primary_question TEXT,
                    attention_count INTEGER NOT NULL DEFAULT 0,
                    last_refresh_at TEXT,
                    reachable INTEGER,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS projects_recent
                    ON projects(last_opened_at DESC, added_at DESC);
                CREATE TABLE IF NOT EXISTS graph_runs (
                    operation_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    status_message TEXT NOT NULL,
                    error TEXT,
                    applied_revision INTEGER,
                    result_json TEXT,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    parent_operation_id TEXT,
                    native_session_id TEXT,
                    stage_host TEXT,
                    stage_root TEXT,
                    estimate_seconds REAL NOT NULL DEFAULT 300,
                    estimate_samples INTEGER NOT NULL DEFAULT 0,
                    phase TEXT NOT NULL DEFAULT 'queued',
                    last_activity_at TEXT
                );
                CREATE INDEX IF NOT EXISTS graph_runs_project
                    ON graph_runs(project_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS graph_run_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES graph_runs(operation_id)
                );
                CREATE INDEX IF NOT EXISTS graph_run_events_operation
                    ON graph_run_events(operation_id, event_id);
                CREATE TABLE IF NOT EXISTS graph_run_receipts (
                    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    category TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES graph_runs(operation_id)
                );
                CREATE INDEX IF NOT EXISTS graph_run_receipts_operation
                    ON graph_run_receipts(operation_id, receipt_id);
                CREATE TABLE IF NOT EXISTS graph_run_outputs (
                    operation_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    patch_json TEXT NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES graph_runs(operation_id)
                );
                CREATE TABLE IF NOT EXISTS graph_run_contracts (
                    operation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    content TEXT NOT NULL,
                    PRIMARY KEY(operation_id, role),
                    FOREIGN KEY(operation_id) REFERENCES graph_runs(operation_id)
                );
                """
            )
            # Existing v0.2 databases need additive migration before the index
            # can include the new transitional state.
            self._ensure_column(connection, "graph_runs", "attempt", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(connection, "graph_runs", "parent_operation_id", "TEXT")
            self._ensure_column(connection, "graph_runs", "native_session_id", "TEXT")
            self._ensure_column(connection, "graph_runs", "stage_host", "TEXT")
            self._ensure_column(connection, "graph_runs", "stage_root", "TEXT")
            self._ensure_column(
                connection, "graph_runs", "estimate_seconds", "REAL NOT NULL DEFAULT 300"
            )
            self._ensure_column(
                connection, "graph_runs", "estimate_samples", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(
                connection, "graph_runs", "phase", "TEXT NOT NULL DEFAULT 'queued'"
            )
            self._ensure_column(connection, "graph_runs", "last_activity_at", "TEXT")
            self._ensure_column(connection, "graph_runs", "result_json", "TEXT")
            connection.execute("DROP INDEX IF EXISTS graph_runs_active_project")
            connection.execute("DROP INDEX IF EXISTS agent_tasks_active_project")
            connection.execute(
                """
                CREATE UNIQUE INDEX agent_tasks_active_project
                ON graph_runs(project_id)
                WHERE status IN ('queued', 'running', 'pausing')
                """
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        name: str,
        definition: str,
    ) -> None:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def project_by_locator(self, locator: str) -> ProjectRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE locator = ?", (locator,)
            ).fetchone()
        return self._project_record(row) if row else None

    def project(self, project_id: str) -> ProjectRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        return self._project_record(row) if row else None

    def projects(self) -> list[ProjectRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM projects
                ORDER BY COALESCE(last_opened_at, added_at) DESC, name COLLATE NOCASE
                """
            ).fetchall()
        return [self._project_record(row) for row in rows]

    def project_deletion_stages(self, project_id: str) -> list[ProjectStageRecord]:
        """Return the saved scratch stages after proving deletion is currently safe."""
        with self.connection() as connection:
            if connection.execute(
                "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone() is None:
                raise KeyError(project_id)
            if connection.execute(
                """
                SELECT 1 FROM graph_runs
                WHERE project_id = ? AND status IN ('queued', 'running', 'pausing')
                LIMIT 1
                """,
                (project_id,),
            ).fetchone() is not None:
                raise ValueError(
                    "Pause the active agent task before deleting this project."
                )
            rows = connection.execute(
                """
                SELECT DISTINCT COALESCE(stage_host, '') AS host, stage_root AS root
                FROM graph_runs
                WHERE project_id = ? AND stage_root IS NOT NULL
                """,
                (project_id,),
            ).fetchall()
        return [ProjectStageRecord.model_validate(dict(row)) for row in rows]

    def delete_project_records(self, project_id: str) -> dict[str, int]:
        """Atomically delete every database row owned by one registration.

        The active-task check is repeated under a write lock so a task cannot be
        launched between the catalog's cleanup preflight and the database commit.
        """
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute(
                    "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
                ).fetchone() is None:
                    raise KeyError(project_id)
                if connection.execute(
                    """
                    SELECT 1 FROM graph_runs
                    WHERE project_id = ? AND status IN ('queued', 'running', 'pausing')
                    LIMIT 1
                    """,
                    (project_id,),
                ).fetchone() is not None:
                    raise ValueError(
                        "Pause the active agent task before deleting this project."
                    )

                operation_ids = connection.execute(
                    "SELECT operation_id FROM graph_runs WHERE project_id = ?",
                    (project_id,),
                ).fetchall()
                operation_count = len(operation_ids)
                counts = {
                    "paper_drafts": connection.execute(
                        "DELETE FROM paper_drafts WHERE project_id = ?", (project_id,)
                    ).rowcount,
                    "writing_sessions": connection.execute(
                        "DELETE FROM writing_sessions WHERE project_id = ?", (project_id,)
                    ).rowcount,
                }
                for table in (
                    "graph_run_outputs",
                    "graph_run_events",
                    "graph_run_receipts",
                    "graph_run_contracts",
                ):
                    counts[table] = connection.execute(
                        f"""
                        DELETE FROM {table}
                        WHERE operation_id IN (
                            SELECT operation_id FROM graph_runs WHERE project_id = ?
                        )
                        """,
                        (project_id,),
                    ).rowcount
                counts["graph_runs"] = connection.execute(
                    "DELETE FROM graph_runs WHERE project_id = ?", (project_id,)
                ).rowcount
                assert counts["graph_runs"] == operation_count
                counts["projects"] = connection.execute(
                    "DELETE FROM projects WHERE project_id = ?", (project_id,)
                ).rowcount
                if counts["projects"] != 1:
                    raise RuntimeError("Project registration disappeared during deletion")
            except Exception:
                connection.rollback()
                raise
        return counts

    def upsert_project(self, record: ProjectRecord) -> ProjectRecord:
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO projects (
                    project_id, locator, name, state_location, state_remote, added_at,
                    last_opened_at, revision, primary_question, attention_count,
                    last_refresh_at, reachable, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    locator = excluded.locator,
                    name = excluded.name,
                    state_location = excluded.state_location,
                    state_remote = excluded.state_remote
                """,
                (
                    record.project_id,
                    record.locator,
                    record.name,
                    record.state_location,
                    int(record.state_remote),
                    record.added_at,
                    record.last_opened_at,
                    record.revision,
                    record.primary_question,
                    record.attention_count,
                    record.last_refresh_at,
                    None if record.reachable is None else int(record.reachable),
                    record.error,
                ),
            )
        stored = self.project(record.project_id)
        assert stored is not None
        return stored

    def update_project_summary(
        self,
        project_id: str,
        *,
        revision: int,
        primary_question: str | None,
        attention_count: int,
        last_refresh_at: str | None,
        reachable: bool,
        error: str | None,
    ) -> ProjectRecord:
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE projects
                SET last_opened_at = ?, revision = ?, primary_question = ?,
                    attention_count = ?, last_refresh_at = ?, reachable = ?, error = ?
                WHERE project_id = ?
                """,
                (
                    self.now(),
                    revision,
                    primary_question,
                    attention_count,
                    last_refresh_at,
                    int(reachable),
                    error,
                    project_id,
                ),
            )
        stored = self.project(project_id)
        if stored is None:
            raise KeyError(project_id)
        return stored

    def migrate_legacy_project_data(self, legacy_id: str, project_id: str) -> None:
        if legacy_id == project_id:
            return
        with self.connection() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO paper_drafts (
                    project_id, content, base_hash, updated_at, cursor_state
                )
                SELECT ?, content, base_hash, updated_at, cursor_state
                FROM paper_drafts
                WHERE project_id = ?
                """,
                (project_id, legacy_id),
            )
            connection.execute(
                "UPDATE writing_sessions SET project_id = ? WHERE project_id = ?",
                (project_id, legacy_id),
            )
            connection.execute(
                "UPDATE graph_runs SET project_id = ? WHERE project_id = ?",
                (project_id, legacy_id),
            )

    def create_agent_task(self, record: AgentTaskRecord) -> AgentTaskRecord:
        try:
            with self.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO graph_runs (
                        operation_id, project_id, kind, status, request_json,
                        created_at, updated_at, started_at, finished_at,
                        status_message, error, applied_revision, result_json, attempt,
                        parent_operation_id, native_session_id, stage_host,
                        stage_root, estimate_seconds, estimate_samples, phase,
                        last_activity_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.operation_id,
                        record.project_id,
                        record.kind,
                        record.status,
                        json.dumps(record.request, separators=(",", ":")),
                        record.created_at,
                        record.updated_at,
                        record.started_at,
                        record.finished_at,
                        record.status_message,
                        record.error,
                        record.applied_revision,
                        self._bounded_result_json(record.result),
                        record.attempt,
                        record.parent_operation_id,
                        record.native_session_id,
                        record.stage_host,
                        record.stage_root,
                        record.estimate_seconds,
                        record.estimate_samples,
                        record.phase,
                        record.last_activity_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Another agent task is already running for this project.") from exc
        stored = self.agent_task(record.operation_id)
        assert stored is not None
        return stored

    def agent_task(self, operation_id: str) -> AgentTaskRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM graph_runs WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        return self._agent_task_record(row) if row else None

    def claim_agent_task_graph_repair(self, operation_id: str) -> AgentTaskRecord:
        """Atomically consume one rejected Work result's manual repair eligibility."""

        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM graph_runs WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            data = dict(row)
            request = json.loads(data["request_json"])
            result = json.loads(data["result_json"]) if data.get("result_json") else None
            graph_update = result.get("graph_update") if isinstance(result, dict) else None
            eligible = (
                data["status"] == "succeeded"
                and data["kind"] in {"node_chat", "project_chat"}
                and isinstance(request, dict)
                and request.get("mode") == "work"
                and bool(data.get("native_session_id"))
                and bool(data.get("stage_root"))
                and isinstance(graph_update, dict)
                and graph_update.get("status") == "rejected"
                and graph_update.get("repairable") is True
            )
            active = connection.execute(
                """
                SELECT 1 FROM graph_runs
                WHERE project_id = ? AND status IN ('queued', 'running', 'pausing')
                LIMIT 1
                """,
                (data["project_id"],),
            ).fetchone()
            if active is not None:
                raise ValueError(
                    "Another agent task is already running for this project."
                )
            if not eligible:
                raise ValueError(
                    "This task has no repairable graph update. Start a new Work turn instead."
                )
            assert isinstance(result, dict)
            assert isinstance(graph_update, dict)
            graph_update = {**graph_update, "repairable": False}
            claimed_result = {**result, "graph_update": graph_update}
            claimed_json = self._bounded_result_json(claimed_result)
            cursor = connection.execute(
                """
                UPDATE graph_runs
                SET result_json = ?, updated_at = ?
                WHERE operation_id = ? AND result_json = ?
                """,
                (claimed_json, self.now(), operation_id, data["result_json"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("This graph update repair was already claimed.")
        claimed = self.agent_task(operation_id)
        assert claimed is not None
        return claimed

    def restore_agent_task_graph_repair(self, operation_id: str) -> None:
        """Undo an unconsumed claim only when no repair child was created."""

        with self.connection() as connection:
            row = connection.execute(
                "SELECT result_json FROM graph_runs WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None or not row["result_json"]:
                return
            child = connection.execute(
                "SELECT 1 FROM graph_runs WHERE parent_operation_id = ? LIMIT 1",
                (operation_id,),
            ).fetchone()
            if child is not None:
                return
            result = json.loads(row["result_json"])
            graph_update = result.get("graph_update") if isinstance(result, dict) else None
            if (
                not isinstance(graph_update, dict)
                or graph_update.get("status") != "rejected"
                or graph_update.get("repairable") is not False
            ):
                return
            restored = {
                **result,
                "graph_update": {**graph_update, "repairable": True},
            }
            connection.execute(
                "UPDATE graph_runs SET result_json = ?, updated_at = ? WHERE operation_id = ?",
                (self._bounded_result_json(restored), self.now(), operation_id),
            )

    def agent_tasks(
        self, project_id: str, *, limit: int = AGENT_TASK_LIST_DEFAULT_LIMIT
    ) -> list[AgentTaskRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM graph_runs
                WHERE project_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (project_id, max(1, min(limit, AGENT_TASK_LIST_MAX_LIMIT))),
            ).fetchall()
        return [self._agent_task_record(row) for row in rows]

    def has_resumable_paused_chat_task(
        self,
        project_id: str,
        kind: Literal["node_chat", "project_chat"],
        chat_id: str,
    ) -> bool:
        """Whether this conversation has a paused attempt awaiting a decision.

        A Resume or Retry creates a child operation immediately. Once that child
        exists, the paused parent no longer blocks a later ordinary turn; if the
        child itself pauses, it is independently found by this query.
        """

        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM graph_runs AS paused
                WHERE paused.project_id = ?
                    AND paused.kind = ?
                    AND paused.status = 'paused'
                    AND paused.native_session_id IS NOT NULL
                    AND (paused.stage_host IS NULL OR paused.stage_host = ''
                         OR paused.stage_root IS NOT NULL)
                    AND json_extract(paused.request_json, '$.chat_id') = ?
                    AND NOT EXISTS (
                        SELECT 1
                        FROM graph_runs AS child
                        WHERE child.parent_operation_id = paused.operation_id
                    )
                LIMIT 1
                """,
                (project_id, kind, chat_id),
            ).fetchone()
        return row is not None

    def has_any_active_agent_task(self) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM graph_runs
                WHERE status IN ('queued', 'running', 'pausing')
                LIMIT 1
                """
            ).fetchone()
        return row is not None

    def agent_task_events(
        self, operation_id: str, *, limit: int = AGENT_TASK_EVENT_LIST_DEFAULT_LIMIT
    ) -> list[AgentTaskEventRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM graph_run_events
                WHERE operation_id = ?
                ORDER BY event_id ASC
                LIMIT ?
                """,
                (operation_id, max(1, min(limit, AGENT_TASK_EVENT_LIST_MAX_LIMIT))),
            ).fetchall()
        return [AgentTaskEventRecord.model_validate(dict(row)) for row in rows]

    def record_agent_task_event(
        self,
        operation_id: str,
        message: str,
        *,
        level: Literal["info", "warning", "error"] = "info",
    ) -> None:
        detail = " ".join(message.split())[:2000]
        if not detail:
            return
        with self.connection() as connection:
            self._insert_agent_task_event(
                connection,
                operation_id,
                detail,
                level=level,
                created_at=self.now(),
            )

    @staticmethod
    def _insert_agent_task_event(
        connection: sqlite3.Connection,
        operation_id: str,
        detail: str,
        *,
        level: Literal["info", "warning", "error"],
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO graph_run_events (operation_id, created_at, level, message)
            VALUES (?, ?, ?, ?)
            """,
            (operation_id, created_at, level, detail),
        )
        connection.execute(
            """
            DELETE FROM graph_run_events
            WHERE operation_id = ? AND event_id NOT IN (
                SELECT event_id FROM graph_run_events
                WHERE operation_id = ?
                ORDER BY event_id DESC
                LIMIT ?
            )
            """,
            (operation_id, operation_id, AGENT_TASK_EVENT_RETENTION_COUNT),
        )

    def agent_task_receipts(
        self, operation_id: str, *, limit: int = AGENT_TASK_RECEIPT_LIST_LIMIT
    ) -> list[AgentTaskReceiptRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM graph_run_receipts
                WHERE operation_id = ?
                ORDER BY receipt_id ASC
                LIMIT ?
                """,
                (operation_id, max(1, min(limit, AGENT_TASK_RECEIPT_LIST_LIMIT))),
            ).fetchall()
        receipts = []
        for row in rows:
            data = dict(row)
            data["payload"] = json.loads(data.pop("payload_json"))
            receipts.append(AgentTaskReceiptRecord.model_validate(data))
        return receipts

    def record_agent_task_receipt(
        self,
        operation_id: str,
        category: str,
        payload: dict[str, object],
        *,
        tier: AgentTaskReceiptTier = "summary",
    ) -> None:
        safe_category = " ".join(category.split())[:100]
        if not safe_category:
            return
        if tier not in AGENT_TASK_RECEIPT_RETENTION_COUNTS:
            raise ValueError(f"Unknown agent-task receipt tier: {tier}")
        payload_json = self._bounded_receipt_payload(payload)
        with self.connection() as connection:
            self._insert_agent_task_receipt(
                connection,
                operation_id,
                safe_category,
                payload_json,
                tier=tier,
                created_at=self.now(),
            )

    @staticmethod
    def _insert_agent_task_receipt(
        connection: sqlite3.Connection,
        operation_id: str,
        category: str,
        payload_json: str,
        *,
        tier: AgentTaskReceiptTier,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO graph_run_receipts (
                operation_id, created_at, tier, category, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (operation_id, created_at, tier, category, payload_json),
        )
        connection.execute(
            """
            DELETE FROM graph_run_receipts
            WHERE operation_id = ? AND tier = ? AND receipt_id NOT IN (
                SELECT receipt_id FROM graph_run_receipts
                WHERE operation_id = ? AND tier = ?
                ORDER BY receipt_id DESC
                LIMIT ?
            )
            """,
            (
                operation_id,
                tier,
                operation_id,
                tier,
                AGENT_TASK_RECEIPT_RETENTION_COUNTS[tier],
            ),
        )

    def record_agent_task_contract(
        self, operation_id: str, role: str, content: str, sha256: str
    ) -> None:
        """Persist immutable contract content outside bounded diagnostic receipts."""
        safe_role = " ".join(role.split())[:200]
        if not safe_role:
            raise ValueError("agent-task contract role is empty")
        with self.connection() as connection:
            existing = connection.execute(
                """
                SELECT sha256, content FROM graph_run_contracts
                WHERE operation_id = ? AND role = ?
                """,
                (operation_id, safe_role),
            ).fetchone()
            if existing is not None:
                if existing["sha256"] != sha256 or existing["content"] != content:
                    raise ValueError("immutable agent-task contract already differs")
                return
            connection.execute(
                """
                INSERT INTO graph_run_contracts (
                    operation_id, role, created_at, sha256, content
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (operation_id, safe_role, self.now(), sha256, content),
            )

    def agent_task_contract(self, operation_id: str, role: str) -> str | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT content FROM graph_run_contracts
                WHERE operation_id = ? AND role = ?
                """,
                (operation_id, role),
            ).fetchone()
        return str(row["content"]) if row is not None else None

    @staticmethod
    def _bounded_receipt_payload(payload: dict[str, object]) -> str:
        keys = [str(key)[:80] for key in list(payload)[:32]]
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError):
            return json.dumps(
                {
                    "omitted": True,
                    "reason": "payload_not_json_serializable",
                    "keys": keys,
                },
                separators=(",", ":"),
            )
        byte_length = len(encoded.encode("utf-8"))
        if byte_length <= AGENT_TASK_RECEIPT_MAX_BYTES:
            return encoded
        return json.dumps(
            {
                "omitted": True,
                "reason": "payload_exceeded_limit",
                "byte_length": byte_length,
                "keys": keys,
            },
            separators=(",", ":"),
        )

    @staticmethod
    def _bounded_result_json(result: dict[str, object] | None) -> str | None:
        if result is None:
            return None
        raw_artifacts = result.get("artifacts")
        artifacts: list[dict[str, object]] = []
        if isinstance(raw_artifacts, list):
            for raw_artifact in raw_artifacts[:CHAT_ARTIFACT_MAX_COUNT]:
                try:
                    descriptor = AgentArtifactDescriptor.model_validate(raw_artifact)
                except (TypeError, ValueError):
                    continue
                artifacts.append(descriptor.model_dump(mode="json"))
        payload: dict[str, object] = {"messages": []}
        if artifacts:
            payload["artifacts"] = artifacts
        raw_graph_update = result.get("graph_update")
        if isinstance(raw_graph_update, dict) and raw_graph_update.get("status") in {
            "none",
            "applied",
            "rejected",
        }:
            raw_change_summary = raw_graph_update.get("change_summary")
            raw_proposal_ids = raw_graph_update.get("proposal_ids")
            raw_validation_messages = raw_graph_update.get("validation_messages")
            payload["graph_update"] = {
                "status": raw_graph_update["status"],
                "applied_revision": (
                    raw_graph_update.get("applied_revision")
                    if isinstance(raw_graph_update.get("applied_revision"), int)
                    and not isinstance(raw_graph_update.get("applied_revision"), bool)
                    else None
                ),
                "change_summary": [
                    item[:1600]
                    for item in (
                        raw_change_summary[:32]
                        if isinstance(raw_change_summary, list)
                        else []
                    )
                    if isinstance(item, str)
                ],
                "proposal_ids": [
                    item[:400]
                    for item in (
                        raw_proposal_ids[:32] if isinstance(raw_proposal_ids, list) else []
                    )
                    if isinstance(item, str)
                ],
                "validation_messages": [
                    item[:1600]
                    for item in (
                        raw_validation_messages[:8]
                        if isinstance(raw_validation_messages, list)
                        else []
                    )
                    if isinstance(item, str)
                ],
                "correction_rounds": (
                    raw_graph_update.get("correction_rounds")
                    if isinstance(raw_graph_update.get("correction_rounds"), int)
                    and not isinstance(raw_graph_update.get("correction_rounds"), bool)
                    else 0
                ),
                "repairable": raw_graph_update.get("repairable") is True,
            }
        raw_messages = result.get("messages")
        messages = raw_messages if isinstance(raw_messages, list) else []
        bounded: list[str] = []
        for raw_message in messages[:32]:
            if not isinstance(raw_message, str):
                continue
            message = raw_message.strip()
            if not message:
                continue
            bounded.append(message[:16_000])
            payload["messages"] = bounded
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if len(encoded.encode("utf-8")) > AGENT_TASK_RESULT_MAX_BYTES:
                bounded.pop()
                break
        payload["messages"] = bounded
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def agent_task_patch_output(self, operation_id: str) -> str | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT patch_json FROM graph_run_outputs WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return str(row["patch_json"]) if row else None

    def record_agent_task_patch_output(self, operation_id: str, patch_json: str) -> None:
        if len(patch_json.encode("utf-8")) > 2_000_000:
            raise ValueError("direct patch output exceeds the 2 MB recovery limit")
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO graph_run_outputs (operation_id, created_at, patch_json)
                VALUES (?, ?, ?)
                ON CONFLICT(operation_id) DO UPDATE SET
                    created_at = excluded.created_at,
                    patch_json = excluded.patch_json
                """,
                (operation_id, self.now(), patch_json),
            )

    def agent_task_estimate(
        self,
        project_id: str,
        kind: AgentTaskKind,
        request: dict[str, object],
    ) -> tuple[float, int]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT request_json, started_at, finished_at
                FROM graph_runs
                WHERE project_id = ? AND kind = ? AND status = 'succeeded'
                    AND started_at IS NOT NULL AND finished_at IS NOT NULL
                ORDER BY finished_at DESC
                LIMIT ?
                """,
                (project_id, kind, AGENT_TASK_ESTIMATE_HISTORY_LIMIT),
            ).fetchall()
        durations: list[float] = []
        for row in rows:
            saved_request = json.loads(row["request_json"])
            if saved_request.get("provider") != request.get("provider"):
                continue
            if (saved_request.get("model") or "") != (request.get("model") or ""):
                continue
            try:
                started = datetime.fromisoformat(row["started_at"])
                finished = datetime.fromisoformat(row["finished_at"])
            except (TypeError, ValueError):
                continue
            duration = (finished - started).total_seconds()
            if duration > 0:
                durations.append(duration)
            if len(durations) == AGENT_TASK_ESTIMATE_SAMPLE_LIMIT:
                break
        if durations:
            return max(1.0, float(median(durations))), len(durations)
        return (600.0 if kind == "seed" else 300.0), 0

    def mark_agent_task_running(self, operation_id: str) -> None:
        now = self.now()
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE graph_runs
                SET status = 'running', started_at = ?, updated_at = ?,
                    last_activity_at = ?, phase = 'preparing',
                    status_message = 'Preparing agent task.'
                WHERE operation_id = ? AND status = 'queued'
                """,
                (now, now, now, operation_id),
            )
        self.record_agent_task_event(operation_id, "Preparing agent task.")

    def update_agent_task_message(
        self,
        operation_id: str,
        message: str,
        *,
        phase: str | None = None,
        event: bool = False,
    ) -> None:
        now = self.now()
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE graph_runs
                SET status_message = ?, updated_at = ?, last_activity_at = ?,
                    phase = COALESCE(?, phase)
                WHERE operation_id = ? AND status IN ('running', 'pausing')
                """,
                (message, now, now, phase, operation_id),
            )
        if event:
            self.record_agent_task_event(operation_id, message)

    def checkpoint_agent_task(
        self,
        operation_id: str,
        *,
        native_session_id: str | None = None,
        stage_host: str | None = None,
        stage_root: str | None = None,
    ) -> None:
        now = self.now()
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE graph_runs
                SET native_session_id = COALESCE(?, native_session_id),
                    stage_host = COALESCE(?, stage_host),
                    stage_root = COALESCE(?, stage_root),
                    updated_at = ?, last_activity_at = ?
                WHERE operation_id = ?
                """,
                (native_session_id, stage_host, stage_root, now, now, operation_id),
            )

    def clear_agent_task_stage(self, operation_id: str) -> None:
        now = self.now()
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE graph_runs
                SET stage_host = NULL, stage_root = NULL, updated_at = ?
                WHERE operation_id = ?
                """,
                (now, operation_id),
            )

    def request_agent_task_pause(
        self, operation_id: str, *, requested_by: Literal["human", "shutdown"] = "human"
    ) -> AgentTaskRecord:
        now = self.now()
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE graph_runs
                SET status = 'pausing', updated_at = ?, last_activity_at = ?,
                    phase = 'pausing', status_message = 'Pausing at the current checkpoint.'
                WHERE operation_id = ? AND status IN ('queued', 'running')
                """,
                (now, now, operation_id),
            )
        if cursor.rowcount == 0:
            raise ValueError("Only a queued or running operation can be paused.")
        self.record_agent_task_event(
            operation_id,
            (
                "Pause requested by the human."
                if requested_by == "human"
                else "Paused for RCP shutdown or reload."
            ),
        )
        record = self.agent_task(operation_id)
        assert record is not None
        return record

    def pause_agent_task(self, operation_id: str) -> None:
        now = self.now()
        detail = "Paused. Resume from the saved agent session, or retry from the beginning."
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE graph_runs
                SET status = 'paused', updated_at = ?, finished_at = ?,
                    last_activity_at = ?, phase = 'paused', status_message = ?, error = NULL
                WHERE operation_id = ? AND status IN ('queued', 'running', 'pausing')
                """,
                (now, now, now, detail, operation_id),
            )
            self._insert_agent_task_event(
                connection,
                operation_id,
                detail,
                level="warning",
                created_at=now,
            )
            self._insert_agent_task_receipt(
                connection,
                operation_id,
                "operation_paused",
                self._bounded_receipt_payload({"status": "paused"}),
                tier="summary",
                created_at=now,
            )

    def complete_agent_task(
        self,
        operation_id: str,
        *,
        applied_revision: int | None,
        result: dict[str, object],
    ) -> None:
        now = self.now()
        result_json = self._bounded_result_json(result)
        graph_update = result.get("graph_update")
        graph_rejected = (
            isinstance(graph_update, dict) and graph_update.get("status") == "rejected"
        )
        status_message = (
            "Completed; graph update rejected."
            if graph_rejected
            else "Agent task completed."
        )
        message = (
            f"Project graph updated to revision {applied_revision}."
            if applied_revision is not None
            else "Operational work completed, but its graph update was rejected."
            if graph_rejected
            else "Agent task completed."
        )
        payload: dict[str, object] = {"status": "succeeded"}
        if applied_revision is not None:
            payload["applied_revision"] = applied_revision
        if isinstance(graph_update, dict):
            payload["graph_update_status"] = str(graph_update.get("status") or "none")
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE graph_runs
                SET status = 'succeeded', updated_at = ?, finished_at = ?,
                    status_message = ?, error = NULL,
                    applied_revision = ?, result_json = ?,
                    phase = 'complete', last_activity_at = ?
                WHERE operation_id = ? AND status IN ('queued', 'running', 'pausing')
                """,
                (
                    now,
                    now,
                    status_message,
                    applied_revision,
                    result_json,
                    now,
                    operation_id,
                ),
            )
            if not graph_rejected:
                connection.execute(
                    "DELETE FROM graph_run_outputs WHERE operation_id = ?",
                    (operation_id,),
                )
            self._insert_agent_task_event(
                connection,
                operation_id,
                message,
                level="info",
                created_at=now,
            )
            self._insert_agent_task_receipt(
                connection,
                operation_id,
                "operation_completed",
                self._bounded_receipt_payload(payload),
                tier="summary",
                created_at=now,
            )

    def fail_agent_task(
        self,
        operation_id: str,
        error: str,
        *,
        status: Literal["failed", "interrupted"] = "failed",
        result: dict[str, object] | None = None,
    ) -> None:
        """Record a failure, keeping any output the task produced before it.

        A chat turn that answered and then had its graph change rejected has
        already earned its reply; failing must not throw that away.
        """
        now = self.now()
        detail = " ".join(error.split())[:2000] or "The background agent task failed."
        self.record_agent_task_event(operation_id, detail, level="error")
        self.record_agent_task_receipt(
            operation_id,
            "operation_failed",
            {"status": status, "error_length": len(detail)},
        )
        result_json = self._bounded_result_json(result) if result is not None else None
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE graph_runs
                SET status = ?, updated_at = ?, finished_at = ?,
                    status_message = ?, error = ?, phase = ?, last_activity_at = ?,
                    result_json = COALESCE(?, result_json)
                WHERE operation_id = ? AND status IN ('queued', 'running', 'pausing')
                """,
                (status, now, now, detail, detail, status, now, result_json, operation_id),
            )

    def interrupt_active_agent_tasks(self) -> None:
        now = self.now()
        detail = (
            "RCP restarted before this operation finished. Resume from its saved session "
            "when available, or retry from the beginning."
        )
        interrupted: list[str] = []
        with self.connection() as connection:
            interrupted = [
                row["operation_id"]
                for row in connection.execute(
                    "SELECT operation_id FROM graph_runs WHERE status IN ('queued', 'running', 'pausing')"
                ).fetchall()
            ]
            connection.execute(
                """
                UPDATE graph_runs
                SET status = 'interrupted', updated_at = ?, finished_at = ?,
                    status_message = ?, error = ?, phase = 'interrupted', last_activity_at = ?
                WHERE status IN ('queued', 'running', 'pausing')
                """,
                (now, now, detail, detail, now),
            )
        for operation_id in interrupted:
            self.record_agent_task_event(operation_id, detail, level="warning")
            self.record_agent_task_receipt(
                operation_id,
                "operation_interrupted",
                {"status": "interrupted", "reason": "process_restart"},
            )

    def prune_operational_storage(self, *, now: datetime | None = None) -> dict[str, int]:
        """Age out bulky run payloads. `graph_runs` rows are never deleted, so
        resume ancestry (invariant 10b) stays walkable for the life of a project."""

        current = now or datetime.now(UTC)
        inactive = """
            operation_id NOT IN (
                SELECT operation_id FROM graph_runs
                WHERE status IN ('queued', 'running', 'pausing')
            )
        """
        patch_cutoff = (current - timedelta(days=PATCH_OUTPUT_RETENTION_DAYS)).isoformat()
        trace_cutoff = (current - timedelta(days=RUN_TRACE_RETENTION_DAYS)).isoformat()
        with self.connection() as connection:
            outputs = connection.execute(
                f"DELETE FROM graph_run_outputs WHERE created_at < ? AND {inactive}",
                (patch_cutoff,),
            ).rowcount
            events = connection.execute(
                f"DELETE FROM graph_run_events WHERE created_at < ? AND {inactive}",
                (trace_cutoff,),
            ).rowcount
            # Summary receipts carry the resume freshness proof (`operation_created`,
            # `chat_context_assembled`); only the bulky lower tiers age out.
            receipts = connection.execute(
                f"""
                DELETE FROM graph_run_receipts
                WHERE created_at < ? AND tier IN ('diagnostic', 'trace') AND {inactive}
                """,
                (trace_cutoff,),
            ).rowcount

            writing_cutoff = current - timedelta(days=WRITING_SESSION_RETENTION_DAYS)
            writing_rows = connection.execute(
                """
                SELECT native_session_id, project_id, last_resumed_at
                FROM writing_sessions
                ORDER BY project_id, last_resumed_at DESC
                """
            ).fetchall()
            delete_writing: list[str] = []
            writing_by_project: dict[str, list[sqlite3.Row]] = {}
            for row in writing_rows:
                writing_by_project.setdefault(str(row["project_id"]), []).append(row)
            for rows in writing_by_project.values():
                for index, row in enumerate(rows):
                    resumed_at = self._parse_time(row["last_resumed_at"])
                    if (
                        index >= WRITING_SESSIONS_PER_PROJECT
                        and resumed_at is not None
                        and resumed_at < writing_cutoff
                    ):
                        delete_writing.append(str(row["native_session_id"]))
            for session_id in delete_writing:
                connection.execute(
                    "DELETE FROM writing_sessions WHERE native_session_id = ?", (session_id,)
                )

        return {
            "outputs": outputs,
            "events": events,
            "receipts": receipts,
            "writing_sessions": len(delete_writing),
        }

    @staticmethod
    def _project_record(row: sqlite3.Row) -> ProjectRecord:
        data = dict(row)
        data["state_remote"] = bool(data["state_remote"])
        if data["reachable"] is not None:
            data["reachable"] = bool(data["reachable"])
        return ProjectRecord.model_validate(data)

    def _agent_task_record(self, row: sqlite3.Row) -> AgentTaskRecord:
        data = dict(row)
        data["request"] = json.loads(data.pop("request_json"))
        result_json = data.pop("result_json", None)
        data["result"] = json.loads(result_json) if result_json else None
        status = data["status"]
        started = self._parse_time(data.get("started_at"))
        finished = self._parse_time(data.get("finished_at"))
        end = finished or datetime.now(UTC)
        elapsed = max(0.0, (end - started).total_seconds()) if started else 0.0
        estimate = max(1.0, float(data.get("estimate_seconds") or 300.0))
        if status == "succeeded":
            progress = 1.0
        elif not started:
            progress = 0.0
        elif elapsed <= estimate:
            progress = 0.85 * elapsed / estimate
        else:
            progress = 0.85 + 0.14 * (1.0 - math.exp(-(elapsed - estimate) / estimate))
        data["elapsed_seconds"] = round(elapsed, 1)
        data["progress"] = round(min(0.99, max(0.0, progress)), 4) if status != "succeeded" else 1.0
        active = status in {"queued", "running", "pausing"}
        stage_ready = not data.get("stage_host") or bool(data.get("stage_root"))
        data["can_pause"] = status in {"queued", "running"}
        data["can_resume"] = (
            status in {"paused", "interrupted"}
            and bool(data.get("native_session_id"))
            and stage_ready
        )
        data["can_retry"] = status in {"paused", "interrupted", "failed"} and not active
        return AgentTaskRecord.model_validate(data)

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def now() -> str:
        return datetime.now(UTC).isoformat()
