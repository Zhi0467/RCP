from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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
from rcp.providers import ProviderUsage
from rcp.skill_registry import SkillReference


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

_EXPERIMENT_EPISODE_CONTEXT_CANDIDATE_ROLE = "experiment_episode_context_candidate"
_MISSING_EXPERIMENT_EPISODE_CONTEXT_DIAGNOSTIC = (
    "This Experiment-loop turn cannot be resumed or retried because its pre-migration "
    "root has no retained episode context candidate. Use Stop loop and press Run to start "
    "a fresh episode."
)


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


class AgentTaskContractRecord(BaseModel):
    operation_id: str
    role: str
    created_at: str
    sha256: str
    content: str


class ChatSessionContextRecord(BaseModel):
    """Durable RCP context baseline bound to one native provider session."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    execution_machine: str = Field(min_length=1)
    native_session_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    kind: Literal["node_chat", "project_chat"]
    chat_id: str = Field(min_length=1)
    node_id: str | None = None
    protocol_version: int = Field(ge=1)
    snapshot_json: str
    snapshot_sha256: str = Field(min_length=1)
    committed_operation_id: str = Field(min_length=1)
    created_at: str
    updated_at: str


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


class ExperimentEpisodeRecord(BaseModel):
    """One bounded episode's native-session binding and graceful-stop intent.

    The binding is what an automatic watcher wake resumes. It is committed only
    by a mechanically successful joint handoff, so a failed first invocation
    never leaves a session an automatic wake would try to continue. A graph-only
    rejection is still a truthful accepted operational handoff.
    """

    model_config = ConfigDict(extra="forbid")

    episode_id: str
    project_id: str
    control_node_id: str
    provider: str | None = None
    execution_machine: str | None = None
    execution_host: str = ""
    native_session_id: str | None = None
    stage_host: str | None = None
    stage_root: str | None = None
    chat_id: str | None = None
    last_turn_operation_id: str | None = None
    last_turn_invocation: int | None = Field(default=None, ge=1)
    last_graph_result: str | None = None
    last_watcher_ids: list[str] = Field(default_factory=list)
    context_baseline: dict[str, object] = Field(default_factory=dict)
    session_diagnostic: str | None = None
    stop_requested_at: str | None = None
    stop_settled_at: str | None = None
    created_at: str
    updated_at: str

    @property
    def session_bound(self) -> bool:
        """Whether an automatic wake has a complete binding to resume."""

        return bool(
            self.native_session_id
            and self.provider
            and self.execution_machine
            and self.stage_root
            and self.chat_id
        )


class ExperimentLoopRuntime(BaseModel):
    """Operational state of the newest bounded episode for one Experiment."""

    episode_id: str | None = None
    invocations_used: int = Field(default=0, ge=0)
    invocation_ceiling: int | None = Field(default=None, ge=1)
    control_revision: int | None = Field(default=None, ge=0)
    active: bool = False
    paused: bool = False
    task_active: bool = False
    detached_work_active: bool = False
    watcher_degraded: bool = False
    watcher_completion_pending: bool = False
    episode_exited: bool = False
    decision_bundle: list[dict[str, object]] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    stop_requested: bool = False
    stop_settled: bool = False
    session_bound: bool = False
    session_diagnostic: str | None = None
    provider: str | None = None
    model: str | None = None
    reasoning: str | None = None
    run_on: str | None = None
    execution_host: str | None = None
    run_truth_scope: list[str] | None = None
    chat_id: str | None = None
    current_operation_id: str | None = None
    current_status: str | None = None
    current_phase: str | None = None
    current_status_message: str | None = None
    current_last_activity_at: str | None = None
    current_invocation: int | None = Field(default=None, ge=1)


AgentUsageCountReason = Literal["counted", "duplicate", "invalid"]


class AgentUsageRecord(BaseModel):
    usage_id: str
    project_id: str
    operation_id: str
    task_kind: AgentTaskKind
    provider: str
    model: str | None = None
    provider_profile: str
    provider_event_type: str
    dedupe_key: str
    counted: bool
    count_reason: AgentUsageCountReason
    created_at: str
    processed_input_tokens: int = Field(ge=0)
    generated_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    cache_write_input_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)
    reported_input_tokens: int | None = Field(default=None, ge=0)
    reported_output_tokens: int | None = Field(default=None, ge=0)
    reported_total_tokens: int | None = Field(default=None, ge=0)
    provider_fields: dict[str, object] = Field(default_factory=dict)


class AgentUsageCell(BaseModel):
    task_kind: AgentTaskKind
    provider: str
    processed_input_tokens: int = 0
    generated_tokens: int = 0
    cached_input_tokens: int = 0
    counted_records: int = 0


class AgentUsageMetric(BaseModel):
    total_tokens: int = 0
    cached_tokens: int = 0
    cache_share: float = 0.0
    block_percent: float = 5.0
    block_tokens: float = 0.0
    cells: list[AgentUsageCell] = Field(default_factory=list)


class AgentUsageSnapshot(BaseModel):
    project_id: str
    input_processed: AgentUsageMetric
    generated: AgentUsageMetric
    counted_records: int = 0
    excluded_records: int = 0
    records: list[AgentUsageRecord] = Field(default_factory=list)


WatcherStatus = Literal["active", "degraded", "completed", "stopped"]


class WatcherClaimConflict(ValueError):
    """A watcher delivery already won the atomic claim."""


class WatcherContinuation(BaseModel):
    """RCP-bound policy needed to create a fresh Work wake."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str | None = None
    reasoning: str | None = None
    run_on: str
    run_truth_scope: list[str] | None = None
    patch_kind: Literal["work", "experiment_loop"] = "work"
    control_node_id: str | None = None
    control_revision: int | None = Field(default=None, ge=0)
    control_episode_id: str | None = None
    control_invocation: int | None = Field(default=None, ge=1)
    control_invocation_ceiling: int | None = Field(default=None, ge=1)
    control_decision_bundle: list[dict[str, object]] = Field(default_factory=list)
    control_completion_criteria: list[str] = Field(default_factory=list)
    workflow_ids: list[str] = Field(default_factory=list)
    skill_ids: list[str] = Field(default_factory=list)
    invoked_workflow_ids: list[str] = Field(default_factory=list)
    invoked_skill_ids: list[str] = Field(default_factory=list)
    resolved_skill_packages: list[SkillReference] = Field(default_factory=list)


class WatcherRecord(BaseModel):
    """Durable operational watcher, separate from graph and provider attempts."""

    model_config = ConfigDict(extra="forbid")

    watcher_id: str
    project_id: str
    origin_operation_id: str
    origin_task_kind: Literal["node_chat", "project_chat"]
    chat_id: str
    node_id: str | None = None
    execution_host: str = ""
    check_command: str
    log_path: str
    cwd: str
    continuation: WatcherContinuation
    status: WatcherStatus = "active"
    created_at: str
    last_checked_at: str | None = None
    last_exit_code: int | None = None
    last_error: str | None = None
    completed_at: str | None = None
    notified: bool = False
    notification_operation_id: str | None = None


_EXPERIMENT_EPISODE_PINNED_FIELDS = (
    "provider",
    "model",
    "reasoning",
    "run_on",
    "run_truth_scope",
    "chat_id",
    "control_node_id",
    "control_revision",
    "control_episode_id",
    "control_invocation_ceiling",
    "control_decision_bundle",
    "control_completion_criteria",
)


def _experiment_pinned_value(request: dict[str, object], field: str) -> object:
    value = request.get(field)
    if field == "run_truth_scope" and isinstance(value, list):
        return sorted({str(item) for item in value})
    return value


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
                CREATE TABLE IF NOT EXISTS chat_session_contexts (
                    provider TEXT NOT NULL,
                    execution_machine TEXT NOT NULL,
                    native_session_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    node_id TEXT,
                    protocol_version INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    committed_operation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(provider, execution_machine, native_session_id)
                );
                CREATE INDEX IF NOT EXISTS chat_session_contexts_project
                    ON chat_session_contexts(project_id);
                CREATE INDEX IF NOT EXISTS chat_session_contexts_native_session
                    ON chat_session_contexts(native_session_id);
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
                CREATE TABLE IF NOT EXISTS agent_usage (
                    usage_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    task_kind TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT,
                    provider_profile TEXT NOT NULL,
                    provider_event_type TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    counted INTEGER NOT NULL,
                    count_reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    processed_input_tokens INTEGER NOT NULL,
                    generated_tokens INTEGER NOT NULL,
                    cached_input_tokens INTEGER NOT NULL,
                    cache_creation_input_tokens INTEGER NOT NULL,
                    cache_write_input_tokens INTEGER NOT NULL,
                    reasoning_output_tokens INTEGER NOT NULL,
                    reported_input_tokens INTEGER,
                    reported_output_tokens INTEGER,
                    reported_total_tokens INTEGER,
                    provider_fields_json TEXT NOT NULL,
                    FOREIGN KEY(operation_id) REFERENCES graph_runs(operation_id)
                );
                CREATE INDEX IF NOT EXISTS agent_usage_project
                    ON agent_usage(project_id, created_at DESC);
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
                CREATE TABLE IF NOT EXISTS watchers (
                    watcher_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    origin_operation_id TEXT NOT NULL,
                    origin_task_kind TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    node_id TEXT,
                    execution_host TEXT NOT NULL,
                    check_command TEXT NOT NULL,
                    log_path TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    continuation_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_checked_at TEXT,
                    last_exit_code INTEGER,
                    last_error TEXT,
                    completed_at TEXT,
                    notified INTEGER NOT NULL DEFAULT 0,
                    notification_operation_id TEXT
                );
                CREATE INDEX IF NOT EXISTS watchers_project
                    ON watchers(project_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS watchers_pollable
                    ON watchers(status, created_at);
                CREATE INDEX IF NOT EXISTS watchers_delivery
                    ON watchers(project_id, origin_operation_id, notified, completed_at);
                CREATE TABLE IF NOT EXISTS experiment_episodes (
                    episode_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    control_node_id TEXT NOT NULL,
                    provider TEXT,
                    execution_machine TEXT,
                    execution_host TEXT NOT NULL DEFAULT '',
                    native_session_id TEXT,
                    stage_host TEXT,
                    stage_root TEXT,
                    chat_id TEXT,
                    last_turn_operation_id TEXT,
                    last_turn_invocation INTEGER,
                    last_graph_result TEXT,
                    last_watcher_ids_json TEXT NOT NULL DEFAULT '[]',
                    context_baseline_json TEXT NOT NULL DEFAULT '{}',
                    session_diagnostic TEXT,
                    stop_requested_at TEXT,
                    stop_settled_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS experiment_episodes_control
                    ON experiment_episodes(project_id, control_node_id, created_at DESC);
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
            self._ensure_column(connection, "graph_runs", "phase", "TEXT NOT NULL DEFAULT 'queued'")
            self._ensure_column(connection, "graph_runs", "last_activity_at", "TEXT")
            self._ensure_column(connection, "graph_runs", "result_json", "TEXT")
            connection.execute("DROP INDEX IF EXISTS graph_runs_active_project")
            connection.execute("DROP INDEX IF EXISTS agent_tasks_active_project")

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
            if (
                connection.execute(
                    "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
                ).fetchone()
                is None
            ):
                raise KeyError(project_id)
            if (
                connection.execute(
                    """
                SELECT 1 FROM graph_runs
                WHERE project_id = ? AND status IN ('queued', 'running', 'pausing')
                LIMIT 1
                """,
                    (project_id,),
                ).fetchone()
                is not None
            ):
                raise ValueError("Pause the active agent task before deleting this project.")
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
                if (
                    connection.execute(
                        "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
                    ).fetchone()
                    is None
                ):
                    raise KeyError(project_id)
                if (
                    connection.execute(
                        """
                    SELECT 1 FROM graph_runs
                    WHERE project_id = ? AND status IN ('queued', 'running', 'pausing')
                    LIMIT 1
                    """,
                        (project_id,),
                    ).fetchone()
                    is not None
                ):
                    raise ValueError("Pause the active agent task before deleting this project.")

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
                    "chat_session_contexts": connection.execute(
                        "DELETE FROM chat_session_contexts WHERE project_id = ?", (project_id,)
                    ).rowcount,
                    "watchers": connection.execute(
                        "DELETE FROM watchers WHERE project_id = ?", (project_id,)
                    ).rowcount,
                    "experiment_episodes": connection.execute(
                        "DELETE FROM experiment_episodes WHERE project_id = ?", (project_id,)
                    ).rowcount,
                }
                connection.execute("DELETE FROM agent_usage WHERE project_id = ?", (project_id,))
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
                "UPDATE chat_session_contexts SET project_id = ? WHERE project_id = ?",
                (project_id, legacy_id),
            )
            connection.execute(
                "UPDATE graph_runs SET project_id = ? WHERE project_id = ?",
                (project_id, legacy_id),
            )
            connection.execute(
                "UPDATE watchers SET project_id = ? WHERE project_id = ?",
                (project_id, legacy_id),
            )
            connection.execute(
                "UPDATE experiment_episodes SET project_id = ? WHERE project_id = ?",
                (project_id, legacy_id),
            )

    def create_agent_task(self, record: AgentTaskRecord) -> AgentTaskRecord:
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if self._has_active_chat_overlap(connection, record):
                    raise ValueError("Another task is already active in this conversation.")
                self._insert_agent_task(connection, record)
        except sqlite3.IntegrityError as exc:
            raise ValueError("Could not create the agent task.") from exc
        stored = self.agent_task(record.operation_id)
        assert stored is not None
        return stored

    def _insert_agent_task(
        self,
        connection: sqlite3.Connection,
        record: AgentTaskRecord,
    ) -> None:
        self._validate_experiment_task_insert(connection, record)
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

    @staticmethod
    def _validate_experiment_task_insert(
        connection: sqlite3.Connection,
        record: AgentTaskRecord,
    ) -> None:
        request = record.request
        if request.get("patch_kind") != "experiment_loop":
            return

        recovery_binding_keys = (*_EXPERIMENT_EPISODE_PINNED_FIELDS, "control_invocation")
        node_id = request.get("control_node_id")
        control_revision = request.get("control_revision")
        episode_id = request.get("control_episode_id")
        invocation = request.get("control_invocation")
        ceiling = request.get("control_invocation_ceiling")
        decision_bundle = request.get("control_decision_bundle")
        completion_criteria = request.get("control_completion_criteria")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("A bounded experiment-loop task must name its control node.")
        if not isinstance(control_revision, int) or isinstance(control_revision, bool):
            raise ValueError("A bounded experiment-loop task must pin its control revision.")
        if not isinstance(decision_bundle, list):
            raise ValueError("A bounded experiment-loop task must pin its governing decisions.")
        if not isinstance(completion_criteria, list) or any(
            not isinstance(item, str) for item in completion_criteria
        ):
            raise ValueError("A bounded experiment-loop task must pin its completion criteria.")
        if not isinstance(episode_id, str):
            raise ValueError("A bounded experiment-loop task must name a valid episode id.")
        try:
            uuid.UUID(episode_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "A bounded experiment-loop task must name a valid episode id."
            ) from exc
        if not isinstance(invocation, int) or isinstance(invocation, bool) or invocation < 1:
            raise ValueError("A bounded experiment-loop task must name its invocation number.")
        if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling < 1:
            raise ValueError("A bounded experiment-loop task must pin its invocation ceiling.")
        if invocation > ceiling:
            raise ValueError("The experiment-loop invocation exceeds its pinned ceiling.")

        if record.parent_operation_id:
            parent = connection.execute(
                """
                SELECT project_id, kind, status, attempt, request_json, result_json
                FROM graph_runs WHERE operation_id = ?
                """,
                (record.parent_operation_id,),
            ).fetchone()
            if parent is None:
                raise ValueError("An experiment-loop recovery task must have its parent task.")
            if parent["project_id"] != record.project_id or parent["kind"] != record.kind:
                raise ValueError("An experiment-loop recovery task must preserve its task scope.")
            parent_request = json.loads(parent["request_json"])
            if any(
                _experiment_pinned_value(parent_request, key)
                != _experiment_pinned_value(request, key)
                for key in recovery_binding_keys
            ):
                raise ValueError(
                    "An experiment-loop recovery task must preserve its control binding and "
                    "pinned configuration."
                )
            parent_result = json.loads(parent["result_json"]) if parent["result_json"] else None
            graph_update = (
                parent_result.get("graph_update") if isinstance(parent_result, dict) else None
            )
            patch_only_repair = (
                request.get("message") is None
                and parent["status"] == "succeeded"
                and isinstance(graph_update, dict)
                and graph_update.get("status") == "rejected"
                and graph_update.get("repairable") is False
            )
            if not patch_only_repair:
                AppStore._validate_experiment_recovery_claim(
                    connection,
                    record,
                    parent,
                    parent_request,
                )
            else:
                AppStore._validate_current_experiment_graph_repair(
                    connection,
                    project_id=record.project_id,
                    control_node_id=node_id,
                    episode_id=episode_id,
                    invocation=invocation,
                    operation_id=record.parent_operation_id,
                )
            return

        trigger = request.get("trigger")
        if trigger not in {"experiment_run", "watcher"}:
            raise ValueError("A root experiment-loop task must be a Run or watcher invocation.")
        rows = connection.execute(
            """
            SELECT request_json FROM graph_runs
            WHERE project_id = ? AND parent_operation_id IS NULL
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
              AND json_extract(request_json, '$.control_episode_id') = ?
            """,
            (record.project_id, node_id, episode_id),
        ).fetchall()
        prior = [json.loads(row["request_json"]) for row in rows]
        if any(
            _experiment_pinned_value(item, key) != _experiment_pinned_value(request, key)
            for item in prior
            for key in _EXPERIMENT_EPISODE_PINNED_FIELDS
        ):
            raise ValueError("An experiment-loop episode cannot change its pinned configuration.")
        expected = max((int(item["control_invocation"]) for item in prior), default=0) + 1
        if invocation != expected:
            raise ValueError(
                f"Experiment-loop invocation {invocation} is out of sequence; expected {expected}."
            )
        if invocation == 1 and prior:
            raise ValueError("An experiment-loop episode may have only one first invocation.")
        if trigger == "experiment_run" and invocation != 1:
            raise ValueError("A human Run must start at experiment-loop invocation 1.")
        if trigger == "watcher" and not prior:
            raise ValueError("An automatic watcher wake requires an existing loop episode.")
        if trigger == "watcher":
            AppStore._validate_experiment_wake_binding(connection, record)

    @staticmethod
    def _validate_experiment_wake_binding(
        connection: sqlite3.Connection,
        record: AgentTaskRecord,
    ) -> None:
        """Prove the saved native session before an automatic wake spends budget."""

        request = record.request
        episode_id = request.get("control_episode_id")
        session_id = request.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("An automatic Experiment wake requires its episode session id.")
        if record.native_session_id != session_id or not record.stage_root:
            raise ValueError(
                "An automatic Experiment wake requires its exact saved session and stage."
            )
        episode = connection.execute(
            "SELECT * FROM experiment_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if episode is None or episode["stop_requested_at"] is not None:
            raise ValueError("The automatic Experiment wake has no active episode binding.")
        expected = {
            "project_id": record.project_id,
            "control_node_id": request.get("control_node_id"),
            "provider": request.get("provider"),
            "execution_machine": request.get("run_on"),
            "native_session_id": session_id,
            "stage_host": record.stage_host or "",
            "stage_root": record.stage_root,
            "chat_id": request.get("chat_id"),
        }
        actual = {
            "project_id": episode["project_id"],
            "control_node_id": episode["control_node_id"],
            "provider": episode["provider"],
            "execution_machine": episode["execution_machine"],
            "native_session_id": episode["native_session_id"],
            "stage_host": episode["stage_host"] or "",
            "stage_root": episode["stage_root"],
            "chat_id": episode["chat_id"],
        }
        mismatched = sorted(key for key, value in expected.items() if actual[key] != value)
        if (episode["execution_host"] or "") != (record.stage_host or ""):
            mismatched.append("execution_host")
        if mismatched:
            raise ValueError(
                "The automatic Experiment wake no longer matches its episode binding: "
                + ", ".join(sorted(set(mismatched)))
            )

    @staticmethod
    def _validate_experiment_recovery_claim(
        connection: sqlite3.Connection,
        record: AgentTaskRecord,
        parent: sqlite3.Row,
        parent_request: dict[str, object],
    ) -> None:
        abandoned = connection.execute(
            """
            SELECT 1 FROM graph_run_receipts
            WHERE operation_id = ? AND category = 'experiment_recovery_abandoned'
            LIMIT 1
            """,
            (record.parent_operation_id,),
        ).fetchone()
        if abandoned is not None:
            raise ValueError("Stop loop already abandoned recovery of this Experiment task.")
        if parent["status"] not in {"paused", "interrupted", "failed"}:
            raise ValueError("Only the latest unresolved loop task can be resumed or retried.")
        if record.attempt != int(parent["attempt"]) + 1:
            raise ValueError("A loop recovery task must advance its provider-attempt lineage.")
        child = connection.execute(
            "SELECT 1 FROM graph_runs WHERE parent_operation_id = ? LIMIT 1",
            (record.parent_operation_id,),
        ).fetchone()
        if child is not None:
            raise ValueError("This loop task already has a recovery child.")
        newest_root = connection.execute(
            """
            SELECT request_json FROM graph_runs
            WHERE project_id = ? AND parent_operation_id IS NULL
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (record.project_id, parent_request["control_node_id"]),
        ).fetchone()
        if newest_root is None:
            raise ValueError("The loop episode root is no longer available.")
        newest_request = json.loads(newest_root["request_json"])
        if newest_request.get("control_episode_id") != parent_request.get(
            "control_episode_id"
        ) or newest_request.get("control_invocation") != parent_request.get("control_invocation"):
            raise ValueError("Only the newest loop episode and invocation can be recovered.")
        newer_attempt = connection.execute(
            """
            SELECT 1 FROM graph_runs
            WHERE project_id = ?
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
              AND json_extract(request_json, '$.control_episode_id') = ?
              AND json_extract(request_json, '$.control_invocation') = ?
              AND attempt > ?
            LIMIT 1
            """,
            (
                record.project_id,
                parent_request["control_node_id"],
                parent_request["control_episode_id"],
                parent_request["control_invocation"],
                parent["attempt"],
            ),
        ).fetchone()
        if newer_attempt is not None:
            raise ValueError("Only the latest unresolved loop task can be recovered.")

    @staticmethod
    def _validate_current_experiment_graph_repair(
        connection: sqlite3.Connection,
        *,
        project_id: str,
        control_node_id: str,
        episode_id: str,
        invocation: int,
        operation_id: str,
    ) -> None:
        """Keep patch-only repair on the newest episode, invocation, and attempt."""

        newest_root = connection.execute(
            """
            SELECT json_extract(request_json, '$.control_episode_id') AS episode_id
            FROM graph_runs
            WHERE project_id = ? AND parent_operation_id IS NULL
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (project_id, control_node_id),
        ).fetchone()
        if newest_root is None or newest_root["episode_id"] != episode_id:
            raise ValueError("Only the newest Experiment episode can repair its graph update.")
        stopped = connection.execute(
            "SELECT stop_requested_at FROM experiment_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if stopped is not None and stopped["stop_requested_at"] is not None:
            raise ValueError("A stopped Experiment episode cannot repair an old graph update.")
        latest = connection.execute(
            """
            SELECT operation_id,
                   json_extract(request_json, '$.control_invocation') AS invocation
            FROM graph_runs
            WHERE project_id = ?
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
              AND json_extract(request_json, '$.control_episode_id') = ?
            ORDER BY CAST(json_extract(request_json, '$.control_invocation') AS INTEGER) DESC,
                     attempt DESC, created_at DESC, rowid DESC
            LIMIT 1
            """,
            (project_id, control_node_id, episode_id),
        ).fetchone()
        if (
            latest is None
            or latest["invocation"] != invocation
            or latest["operation_id"] != operation_id
        ):
            raise ValueError(
                "Only the newest Experiment invocation and task attempt can repair its graph "
                "update."
            )

    @staticmethod
    def _has_active_chat_overlap(
        connection: sqlite3.Connection,
        record: AgentTaskRecord,
    ) -> bool:
        if record.kind not in {"node_chat", "project_chat"}:
            return False
        chat_id = record.request.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id:
            return False
        active = connection.execute(
            """
            SELECT 1 FROM graph_runs
            WHERE project_id = ? AND kind = ?
              AND json_extract(request_json, '$.chat_id') = ?
              AND status IN ('queued', 'running', 'pausing')
            LIMIT 1
            """,
            (record.project_id, record.kind, chat_id),
        ).fetchone()
        return active is not None

    def create_watchers(self, records: list[WatcherRecord]) -> list[WatcherRecord]:
        """Insert one validated watch list atomically."""

        self._validate_watch_list(records)
        watcher_ids = [record.watcher_id for record in records]
        with self.connection() as connection:
            for record in records:
                self._insert_watcher(connection, record)
        stored: list[WatcherRecord] = []
        for watcher_id in watcher_ids:
            record = self.watcher(watcher_id)
            assert record is not None
            stored.append(record)
        return stored

    def persist_experiment_watchers_idempotently(
        self,
        records: list[WatcherRecord],
    ) -> list[WatcherRecord]:
        """Persist one loop handoff atomically with the episode's graceful stop.

        Deterministic watcher ids make Retry and crash recovery safe. The same
        ``BEGIN IMMEDIATE`` boundary used by Stop loop ensures either the handoff
        lands first and Stop terminalizes it, or the handoff sees stop intent and
        is born stopped. No pollable row can be created after a persisted stop.
        """

        self._validate_watch_list(records)
        continuation = records[0].continuation
        if continuation.patch_kind != "experiment_loop":
            raise ValueError("idempotent Experiment persistence requires loop watchers")
        episode_id = continuation.control_episode_id
        assert episode_id is not None
        watcher_ids = [record.watcher_id for record in records]
        placeholders = ",".join("?" for _ in watcher_ids)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            episode = connection.execute(
                "SELECT project_id, control_node_id, stop_requested_at "
                "FROM experiment_episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            if episode is not None and (
                episode["project_id"] != records[0].project_id
                or episode["control_node_id"] != continuation.control_node_id
            ):
                raise ValueError("This watcher handoff belongs to a different Experiment episode.")
            stopped = episode is not None and episode["stop_requested_at"] is not None
            existing_rows = connection.execute(
                f"SELECT * FROM watchers WHERE watcher_id IN ({placeholders})",
                watcher_ids,
            ).fetchall()
            existing_by_id = {
                str(row["watcher_id"]): self._watcher_record(row) for row in existing_rows
            }
            for desired in records:
                existing = existing_by_id.get(desired.watcher_id)
                if existing is not None:
                    self._validate_idempotent_watcher(existing, desired)
                    if stopped and (existing.status != "stopped" or not existing.notified):
                        connection.execute(
                            "UPDATE watchers SET status = 'stopped', notified = 1 "
                            "WHERE watcher_id = ?",
                            (desired.watcher_id,),
                        )
                    continue
                persisted = (
                    desired.model_copy(update={"status": "stopped", "notified": True})
                    if stopped
                    else desired
                )
                self._insert_watcher(connection, persisted)
            stored_rows = connection.execute(
                f"SELECT * FROM watchers WHERE watcher_id IN ({placeholders})",
                watcher_ids,
            ).fetchall()
            stored_by_id = {
                str(row["watcher_id"]): self._watcher_record(row) for row in stored_rows
            }
        return [stored_by_id[watcher_id] for watcher_id in watcher_ids]

    @staticmethod
    def _validate_watch_list(records: list[WatcherRecord]) -> None:
        if not records:
            raise ValueError("a watch list must contain at least one watcher")
        watcher_ids = [record.watcher_id for record in records]
        if len(watcher_ids) != len(set(watcher_ids)):
            raise ValueError("a watch list cannot repeat a watcher id")
        bindings = {
            (
                record.project_id,
                record.origin_operation_id,
                record.origin_task_kind,
                record.chat_id,
                record.node_id,
                record.execution_host,
                record.continuation.model_dump_json(),
            )
            for record in records
        }
        if len(bindings) != 1:
            raise ValueError("one watch list must share one RCP-bound continuation context")
        continuation = records[0].continuation
        if continuation.patch_kind != "experiment_loop":
            return
        if not all(
            (
                continuation.control_node_id,
                continuation.control_episode_id,
                continuation.control_invocation,
                continuation.control_invocation_ceiling,
            )
        ):
            raise ValueError("an experiment-loop watcher must preserve its control binding")
        assert continuation.control_invocation is not None
        assert continuation.control_invocation_ceiling is not None
        if continuation.control_invocation > continuation.control_invocation_ceiling:
            raise ValueError("an experiment-loop watcher invocation exceeds its pinned ceiling")

    @staticmethod
    def _validate_idempotent_watcher(
        existing: WatcherRecord,
        desired: WatcherRecord,
    ) -> None:
        immutable_fields = (
            "project_id",
            "origin_operation_id",
            "origin_task_kind",
            "chat_id",
            "node_id",
            "execution_host",
            "check_command",
            "log_path",
            "cwd",
            "continuation",
        )
        if any(getattr(existing, field) != getattr(desired, field) for field in immutable_fields):
            raise ValueError("Experiment-loop watcher identity conflicts with stored state.")

    @staticmethod
    def _insert_watcher(connection: sqlite3.Connection, record: WatcherRecord) -> None:
        connection.execute(
            """
            INSERT INTO watchers (
                watcher_id, project_id, origin_operation_id, origin_task_kind,
                chat_id, node_id, execution_host, check_command, log_path, cwd,
                continuation_json, status, created_at, last_checked_at,
                last_exit_code, last_error, completed_at, notified,
                notification_operation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.watcher_id,
                record.project_id,
                record.origin_operation_id,
                record.origin_task_kind,
                record.chat_id,
                record.node_id,
                record.execution_host,
                record.check_command,
                record.log_path,
                record.cwd,
                record.continuation.model_dump_json(),
                record.status,
                record.created_at,
                record.last_checked_at,
                record.last_exit_code,
                record.last_error,
                record.completed_at,
                int(record.notified),
                record.notification_operation_id,
            ),
        )

    def watcher(self, watcher_id: str) -> WatcherRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM watchers WHERE watcher_id = ?", (watcher_id,)
            ).fetchone()
        return self._watcher_record(row) if row is not None else None

    def watchers(
        self,
        project_id: str,
        *,
        chat_id: str | None = None,
    ) -> list[WatcherRecord]:
        query = "SELECT * FROM watchers WHERE project_id = ?"
        parameters: list[object] = [project_id]
        if chat_id is not None:
            query += " AND chat_id = ?"
            parameters.append(chat_id)
        query += " ORDER BY created_at DESC, watcher_id"
        with self.connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._watcher_record(row) for row in rows]

    def pollable_watchers(self) -> list[WatcherRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM watchers
                WHERE status IN ('active', 'degraded')
                ORDER BY created_at, watcher_id
                """
            ).fetchall()
            records = [self._watcher_record(row) for row in rows]
            stopping_contexts: dict[
                tuple[str, str],
                tuple[dict[str, object], ExperimentEpisodeRecord] | None,
            ] = {}
            return [
                record
                for record in records
                if not self._watcher_suppressed_by_current_stop(
                    connection,
                    record,
                    stopping_contexts,
                )
            ]

    def stop_watchers(self, project_id: str, watcher_ids: list[str]) -> list[WatcherRecord]:
        """Release watchers the human has given up on.

        A stopped watcher leaves the polling set and can never wake a turn. RCP
        never decides this for itself — a check that cannot answer is reported,
        not interpreted.
        """

        ids = list(dict.fromkeys(watcher_ids))
        if not ids:
            raise ValueError("stopping watchers requires at least one watcher id")
        placeholders = ",".join("?" for _ in ids)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"""
                SELECT watcher_id, project_id, status, notified, notification_operation_id
                FROM watchers
                WHERE watcher_id IN ({placeholders})
                """,
                ids,
            ).fetchall()
            if {str(row["watcher_id"]) for row in rows} != set(ids) or {
                str(row["project_id"]) for row in rows
            } != {project_id}:
                missing = next(
                    (
                        watcher_id
                        for watcher_id in ids
                        if watcher_id not in {str(row["watcher_id"]) for row in rows}
                    ),
                    ids[0],
                )
                raise KeyError(missing)
            if any(row["notification_operation_id"] is not None for row in rows):
                raise WatcherClaimConflict("A watcher update was already claimed for delivery.")
            invalid = [
                str(row["watcher_id"])
                for row in rows
                if row["status"] not in {"active", "degraded", "completed", "stopped"}
                or (bool(row["notified"]) and row["status"] != "stopped")
            ]
            if invalid:
                raise ValueError(f"Watchers cannot be stopped: {', '.join(sorted(invalid))}.")
            connection.execute(
                f"""
                UPDATE watchers
                SET status = 'stopped', notified = 1
                WHERE project_id = ? AND watcher_id IN ({placeholders})
                  AND status IN ('active', 'degraded', 'completed')
                  AND notification_operation_id IS NULL
                """,
                (project_id, *ids),
            )
        stopped: list[WatcherRecord] = []
        for watcher_id in ids:
            record = self.watcher(watcher_id)
            assert record is not None
            stopped.append(record)
        return stopped

    def experiment_watcher_ids(self, project_id: str, control_node_id: str) -> list[str]:
        """Live watchers armed by a bounded loop on one experiment."""

        return [
            record.watcher_id
            for record in self.watchers(project_id)
            if (
                record.status in {"active", "degraded"}
                or (record.status == "completed" and not record.notified)
            )
            and record.continuation.control_node_id == control_node_id
        ]

    def experiment_episode(self, episode_id: str) -> ExperimentEpisodeRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM experiment_episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
        return self._experiment_episode_record(row) if row is not None else None

    def experiment_episode_recovery_context_problem(self, operation_id: str) -> str | None:
        """Explain why this task lineage cannot retain its episode context on recovery."""

        with self.connection() as connection:
            return self._experiment_episode_recovery_context_problem(connection, operation_id)

    @staticmethod
    def _experiment_episode_recovery_context_problem(
        connection: sqlite3.Connection,
        operation_id: str,
    ) -> str | None:
        """Validate the immutable candidate on an Experiment invocation's lineage root."""

        current_id = operation_id
        seen: set[str] = set()
        while True:
            if current_id in seen:
                return (
                    "This Experiment-loop turn cannot be resumed or retried because its task "
                    "lineage contains a cycle. Use Stop loop and press Run to start a fresh "
                    "episode."
                )
            seen.add(current_id)
            row = connection.execute(
                "SELECT parent_operation_id FROM graph_runs WHERE operation_id = ?",
                (current_id,),
            ).fetchone()
            if row is None:
                return (
                    "This Experiment-loop turn cannot be resumed or retried because its task "
                    "lineage is incomplete. Use Stop loop and press Run to start a fresh episode."
                )
            parent_id = row["parent_operation_id"]
            if parent_id is None:
                break
            current_id = str(parent_id)

        contract = connection.execute(
            """
            SELECT content FROM graph_run_contracts
            WHERE operation_id = ? AND role = ?
            """,
            (current_id, _EXPERIMENT_EPISODE_CONTEXT_CANDIDATE_ROLE),
        ).fetchone()
        if contract is None:
            return _MISSING_EXPERIMENT_EPISODE_CONTEXT_DIAGNOSTIC
        try:
            candidate = json.loads(contract["content"])
        except (json.JSONDecodeError, TypeError):
            candidate = None
        if not isinstance(candidate, dict):
            return (
                "This Experiment-loop turn cannot be resumed or retried because its retained "
                "episode context candidate is invalid. Use Stop loop and press Run to start a "
                "fresh episode."
            )
        return None

    def previous_experiment_episode(
        self,
        project_id: str,
        control_node_id: str,
        episode_id: str,
    ) -> ExperimentEpisodeRecord | None:
        """Return the episode immediately before this one for the same Experiment.

        Ordering comes from the root invocations, not the episode table, because
        an episode only gets a row once it binds a session or receives a stop.
        """

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT json_extract(request_json, '$.control_episode_id') AS episode_id
                FROM graph_runs
                WHERE project_id = ? AND parent_operation_id IS NULL
                  AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
                  AND json_extract(request_json, '$.control_node_id') = ?
                ORDER BY created_at DESC, rowid DESC
                """,
                (project_id, control_node_id),
            ).fetchall()
        ordered: list[str] = []
        for row in rows:
            value = row["episode_id"]
            if isinstance(value, str) and value not in ordered:
                ordered.append(value)
        if episode_id not in ordered:
            return None
        position = ordered.index(episode_id) + 1
        if position >= len(ordered):
            return None
        return self.experiment_episode(ordered[position])

    def commit_experiment_episode_turn(
        self,
        *,
        episode_id: str,
        project_id: str,
        control_node_id: str,
        provider: str,
        execution_machine: str,
        execution_host: str,
        native_session_id: str,
        stage_host: str | None,
        stage_root: str,
        chat_id: str,
        operation_id: str,
        invocation: int,
        graph_result: str,
        watcher_ids: list[str],
        context_baseline: dict[str, object],
    ) -> ExperimentEpisodeRecord:
        """Bind this episode to the session a later automatic wake resumes.

        Only a mechanically successful joint handoff commits, so a wake never
        tries to continue a session that never established one, and the context
        baseline can only move forward with an accepted operational turn. A
        graph-only rejection is retained as that turn's truthful result.
        """

        if not native_session_id or not stage_root:
            raise ValueError("An episode binding requires a native session and its exact stage.")
        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO experiment_episodes (
                    episode_id, project_id, control_node_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(episode_id) DO NOTHING
                """,
                (episode_id, project_id, control_node_id, now, now),
            )
            existing = connection.execute(
                "SELECT * FROM experiment_episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            if (
                existing is None
                or existing["project_id"] != project_id
                or existing["control_node_id"] != control_node_id
            ):
                raise ValueError("This episode id belongs to a different Experiment.")
            if existing["native_session_id"] is not None:
                immutable = {
                    "provider": provider,
                    "execution_machine": execution_machine,
                    "execution_host": execution_host,
                    "native_session_id": native_session_id,
                    "stage_host": stage_host or "",
                    "stage_root": stage_root,
                    "chat_id": chat_id,
                }
                conflicts = sorted(
                    field for field, value in immutable.items() if (existing[field] or "") != value
                )
                if conflicts:
                    raise ValueError(
                        "An Experiment episode cannot change its native-session binding: "
                        + ", ".join(conflicts)
                    )
            connection.execute(
                """
                UPDATE experiment_episodes
                SET provider = ?, execution_machine = ?, execution_host = ?,
                    native_session_id = ?, stage_host = ?, stage_root = ?, chat_id = ?,
                    last_turn_operation_id = ?, last_turn_invocation = ?,
                    last_graph_result = ?, last_watcher_ids_json = ?,
                    context_baseline_json = ?, session_diagnostic = NULL, updated_at = ?
                WHERE episode_id = ?
                """,
                (
                    provider,
                    execution_machine,
                    execution_host,
                    native_session_id,
                    stage_host,
                    stage_root,
                    chat_id,
                    operation_id,
                    invocation,
                    graph_result,
                    json.dumps(list(watcher_ids), separators=(",", ":")),
                    json.dumps(context_baseline, sort_keys=True, separators=(",", ":")),
                    now,
                    episode_id,
                ),
            )
        stored = self.experiment_episode(episode_id)
        assert stored is not None
        return stored

    def record_experiment_episode_diagnostic(
        self,
        *,
        episode_id: str,
        project_id: str,
        control_node_id: str,
        diagnostic: str | None,
    ) -> None:
        """Persist why an automatic wake could not use this episode's session.

        The row is created on demand: the episode whose very first turn never
        bound a session is exactly the one that most needs a diagnostic, and it
        has nothing else to write a row for it.
        """

        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO experiment_episodes (
                    episode_id, project_id, control_node_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(episode_id) DO NOTHING
                """,
                (episode_id, project_id, control_node_id, now, now),
            )
            connection.execute(
                "UPDATE experiment_episodes SET session_diagnostic = ?, updated_at = ? "
                "WHERE episode_id = ? AND project_id = ? AND control_node_id = ?",
                (diagnostic, now, episode_id, project_id, control_node_id),
            )

    def request_experiment_loop_stop(
        self,
        project_id: str,
        control_node_id: str,
    ) -> ExperimentEpisodeRecord | None:
        """Persist a durable stop for the newest episode before any new claim can win.

        The intent is written under the same write lock a watcher claim takes, so
        a claim that committed first becomes the current turn and anything later
        finds the loop already stopped.
        """

        now = self.now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            episode_id = self._newest_experiment_episode_id(connection, project_id, control_node_id)
            if episode_id is None:
                return None
            connection.execute(
                """
                INSERT INTO experiment_episodes (
                    episode_id, project_id, control_node_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(episode_id) DO NOTHING
                """,
                (episode_id, project_id, control_node_id, now, now),
            )
            connection.execute(
                """
                UPDATE experiment_episodes
                SET stop_requested_at = COALESCE(stop_requested_at, ?), updated_at = ?
                WHERE episode_id = ?
                """,
                (now, now, episode_id),
            )
            self._settle_experiment_loop_stop(connection, project_id, control_node_id, episode_id)
        return self.experiment_episode(episode_id)

    def settle_experiment_loop_stop(
        self,
        project_id: str,
        control_node_id: str,
    ) -> ExperimentEpisodeRecord | None:
        """Reconcile a persisted stop once its authorized turn is no longer live."""

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            episode_id = self._newest_experiment_episode_id(connection, project_id, control_node_id)
            if episode_id is None:
                return None
            self._settle_experiment_loop_stop(connection, project_id, control_node_id, episode_id)
        return self.experiment_episode(episode_id)

    def _settle_experiment_loop_stop(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        control_node_id: str,
        episode_id: str,
    ) -> bool:
        """Terminalize this episode's observers once its authorized turn is resolved.

        "Resolved" is the same predicate the runtime calls `task_active`, not just
        "not running": a turn that paused or failed is still the authorized turn
        the human may Resume, so the loop keeps reading Stopping until it reaches
        a terminal state. A claimed watcher keeps its notification provenance,
        but becomes stopped once the task it woke has finished successfully.
        """

        requested = connection.execute(
            "SELECT * FROM experiment_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if requested is None or requested["stop_requested_at"] is None:
            return False
        # A superseded attempt does not count: only the newest attempt of each
        # invocation is the turn the human can still act on, which is exactly what
        # `experiment_loop_runtime` reports as `task_active`.
        unresolved = connection.execute(
            """
            SELECT task.operation_id, task.status FROM graph_runs AS task
            WHERE task.project_id = ?
              AND json_extract(task.request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(task.request_json, '$.control_node_id') = ?
              AND json_extract(task.request_json, '$.control_episode_id') = ?
              AND task.status IN ('queued', 'running', 'pausing', 'paused', 'failed', 'interrupted')
              AND NOT EXISTS (
                  SELECT 1 FROM graph_runs AS child
                  WHERE child.parent_operation_id = task.operation_id
              )
            """,
            (project_id, control_node_id, episode_id),
        ).fetchall()
        if unresolved:
            diagnostic = requested["session_diagnostic"]
            if not diagnostic:
                diagnostic = next(
                    (
                        problem
                        for row in unresolved
                        if (
                            problem := self._experiment_episode_recovery_context_problem(
                                connection,
                                str(row["operation_id"]),
                            )
                        )
                    ),
                    None,
                )
                if diagnostic:
                    now = self.now()
                    connection.execute(
                        "UPDATE experiment_episodes SET session_diagnostic = ?, updated_at = ? "
                        "WHERE episode_id = ?",
                        (diagnostic, now, episode_id),
                    )
            abandonable = bool(diagnostic) and all(
                row["status"] in {"paused", "failed", "interrupted"} for row in unresolved
            )
            if not abandonable:
                return False
            now = self.now()
            for row in unresolved:
                already_abandoned = connection.execute(
                    """
                    SELECT 1 FROM graph_run_receipts
                    WHERE operation_id = ? AND category = 'experiment_recovery_abandoned'
                    LIMIT 1
                    """,
                    (row["operation_id"],),
                ).fetchone()
                if already_abandoned is not None:
                    continue
                detail = (
                    "Stop loop abandoned recovery of this terminal task because its saved "
                    "episode session cannot be continued. The task and all history remain "
                    "inspectable."
                )
                self._insert_agent_task_receipt(
                    connection,
                    str(row["operation_id"]),
                    "experiment_recovery_abandoned",
                    self._bounded_receipt_payload({"episode_id": episode_id, "reason": diagnostic}),
                    tier="summary",
                    created_at=now,
                )
                self._insert_agent_task_event(
                    connection,
                    str(row["operation_id"]),
                    detail,
                    level="warning",
                    created_at=now,
                )
        root_request = self._experiment_episode_root_request(
            connection,
            project_id,
            control_node_id,
            episode_id,
        )
        episode = self._experiment_episode_record(requested)
        watcher_rows = connection.execute(
            """
            SELECT * FROM watchers
            WHERE project_id = ?
              AND json_extract(continuation_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(continuation_json, '$.control_node_id') = ?
              AND status IN ('active', 'degraded', 'completed')
            """,
            (project_id, control_node_id),
        ).fetchall()
        watcher_ids = {
            record.watcher_id
            for record in (self._watcher_record(row) for row in watcher_rows)
            if root_request is not None
            and self._experiment_watcher_matches_current(record, root_request, episode)
        }
        claimed_rows = connection.execute(
            """
            SELECT watcher_id FROM watchers
            WHERE project_id = ?
              AND notification_operation_id IN (
                  SELECT operation_id FROM graph_runs
                  WHERE project_id = ?
                    AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
                    AND json_extract(request_json, '$.control_node_id') = ?
                    AND json_extract(request_json, '$.control_episode_id') = ?
              )
            """,
            (project_id, project_id, control_node_id, episode_id),
        ).fetchall()
        watcher_ids.update(str(row["watcher_id"]) for row in claimed_rows)
        if watcher_ids:
            placeholders = ",".join("?" for _ in watcher_ids)
            connection.execute(
                f"UPDATE watchers SET status = 'stopped', notified = 1 "
                f"WHERE watcher_id IN ({placeholders})",
                sorted(watcher_ids),
            )
        if requested["stop_settled_at"] is None:
            now = self.now()
            connection.execute(
                "UPDATE experiment_episodes SET stop_settled_at = ?, updated_at = ? "
                "WHERE episode_id = ?",
                (now, now, episode_id),
            )
        return True

    def settle_ready_experiment_loop_stops(self) -> int:
        """Reconcile every durable stop that no longer has a recoverable turn."""

        settled = 0
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT episode_id, project_id, control_node_id
                FROM experiment_episodes
                WHERE stop_requested_at IS NOT NULL AND stop_settled_at IS NULL
                ORDER BY created_at, episode_id
                """
            ).fetchall()
            for row in rows:
                if self._settle_experiment_loop_stop(
                    connection,
                    str(row["project_id"]),
                    str(row["control_node_id"]),
                    str(row["episode_id"]),
                ):
                    settled += 1
        return settled

    @staticmethod
    def _newest_experiment_episode_id(
        connection: sqlite3.Connection,
        project_id: str,
        control_node_id: str,
    ) -> str | None:
        row = connection.execute(
            """
            SELECT json_extract(request_json, '$.control_episode_id') AS episode_id
            FROM graph_runs
            WHERE project_id = ? AND parent_operation_id IS NULL
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (project_id, control_node_id),
        ).fetchone()
        if row is None or not isinstance(row["episode_id"], str):
            return None
        return row["episode_id"]

    def experiment_loop_runtime(
        self,
        project_id: str,
        control_node_id: str,
    ) -> ExperimentLoopRuntime:
        """Derive the newest episode from root invocations and its watcher ledger."""

        return self.experiment_loop_runtimes(project_id, [control_node_id])[control_node_id]

    def experiment_loop_runtimes(
        self,
        project_id: str,
        control_node_ids: Iterable[str],
    ) -> dict[str, ExperimentLoopRuntime]:
        """Derive several Experiment runtimes from one project-scoped projection."""

        requested = tuple(dict.fromkeys(control_node_ids))
        if not requested:
            return {}
        projected = self._project_experiment_loop_runtimes(project_id, set(requested))
        return {
            control_node_id: projected.get(control_node_id, ExperimentLoopRuntime())
            for control_node_id in requested
        }

    def _project_experiment_loop_runtimes(
        self,
        project_id: str,
        requested: set[str] | None,
    ) -> dict[str, ExperimentLoopRuntime]:
        """Load loop ledgers in four bounded reads and group them in memory."""

        with self.connection() as connection:
            task_rows = connection.execute(
                """
                SELECT operation_id, parent_operation_id, status, attempt, request_json,
                       created_at, phase, status_message, last_activity_at,
                       rowid AS storage_rowid
                FROM graph_runs
                WHERE project_id = ?
                  AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
                """,
                (project_id,),
            ).fetchall()
            receipt_rows = connection.execute(
                """
                SELECT receipt.operation_id, receipt.category
                FROM graph_run_receipts AS receipt
                JOIN graph_runs AS task ON task.operation_id = receipt.operation_id
                WHERE task.project_id = ?
                  AND json_extract(task.request_json, '$.patch_kind') = 'experiment_loop'
                  AND receipt.category IN (
                      'experiment_loop_exit', 'experiment_recovery_abandoned'
                  )
                """,
                (project_id,),
            ).fetchall()
            watcher_rows = connection.execute(
                """
                SELECT * FROM watchers
                WHERE project_id = ?
                  AND json_extract(continuation_json, '$.patch_kind') = 'experiment_loop'
                  AND (status IN ('active', 'degraded')
                       OR (status = 'completed' AND notified = 0))
                """,
                (project_id,),
            ).fetchall()
            episode_rows = connection.execute(
                """
                SELECT * FROM experiment_episodes
                WHERE project_id = ?
                """,
                (project_id,),
            ).fetchall()

        tasks_by_control: dict[
            str,
            list[tuple[sqlite3.Row, dict[str, object]]],
        ] = {}
        for row in task_rows:
            request = json.loads(row["request_json"])
            control_node_id = request.get("control_node_id")
            if not isinstance(control_node_id, str) or not control_node_id:
                continue
            if requested is not None and control_node_id not in requested:
                continue
            tasks_by_control.setdefault(control_node_id, []).append((row, request))

        watchers_by_control: dict[str, list[WatcherRecord]] = {}
        for row in watcher_rows:
            record = self._watcher_record(row)
            control_node_id = record.continuation.control_node_id
            if not control_node_id:
                continue
            if requested is not None and control_node_id not in requested:
                continue
            watchers_by_control.setdefault(control_node_id, []).append(record)

        receipt_categories: dict[str, set[str]] = {}
        for row in receipt_rows:
            receipt_categories.setdefault(str(row["operation_id"]), set()).add(str(row["category"]))
        episodes = {
            str(row["episode_id"]): self._experiment_episode_record(row) for row in episode_rows
        }
        control_node_ids = (
            set(tasks_by_control) | set(watchers_by_control) if requested is None else requested
        )
        return {
            control_node_id: self._derive_experiment_loop_runtime(
                tasks_by_control.get(control_node_id, []),
                watchers_by_control.get(control_node_id, []),
                receipt_categories,
                episodes,
            )
            for control_node_id in control_node_ids
        }

    @classmethod
    def _derive_experiment_loop_runtime(
        cls,
        task_entries: list[tuple[sqlite3.Row, dict[str, object]]],
        watchers: list[WatcherRecord],
        receipt_categories: dict[str, set[str]],
        episodes: dict[str, ExperimentEpisodeRecord],
    ) -> ExperimentLoopRuntime:
        """Purely derive one runtime from an already-loaded project ledger."""

        root_entries = [entry for entry in task_entries if entry[0]["parent_operation_id"] is None]
        if not root_entries:
            return ExperimentLoopRuntime()
        _, root_request = max(
            root_entries,
            key=lambda entry: (entry[0]["created_at"], entry[0]["storage_rowid"]),
        )
        episode_id = root_request.get("control_episode_id")
        if not isinstance(episode_id, str):
            raise ValueError("Stored experiment-loop root is missing its episode id.")
        try:
            uuid.UUID(episode_id)
        except ValueError as exc:
            raise ValueError("Stored experiment-loop root has an invalid episode id.") from exc

        episode_entries = [
            entry for entry in task_entries if entry[1].get("control_episode_id") == episode_id
        ]
        episode_entries.sort(
            key=lambda entry: (
                entry[0]["attempt"],
                entry[0]["created_at"],
                entry[0]["storage_rowid"],
            ),
            reverse=True,
        )
        episode = episodes.get(episode_id)
        compatible_watchers = [
            record
            for record in watchers
            if cls._experiment_watcher_matches_current(record, root_request, episode)
        ]
        latest_by_invocation: dict[
            int,
            tuple[sqlite3.Row, dict[str, object]],
        ] = {}
        for row, request in episode_entries:
            invocation = request.get("control_invocation")
            if isinstance(invocation, int) and invocation not in latest_by_invocation:
                latest_by_invocation[invocation] = (row, request)
        ceiling = root_request.get("control_invocation_ceiling")
        if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling < 1:
            raise ValueError("Stored experiment-loop root is missing its pinned ceiling.")
        if not latest_by_invocation or min(latest_by_invocation) < 1:
            raise ValueError("Stored experiment-loop root is missing its invocation number.")
        invocations_used = max(latest_by_invocation)
        if set(latest_by_invocation) != set(range(1, invocations_used + 1)):
            raise ValueError("Stored experiment-loop root invocations are out of sequence.")
        if invocations_used > ceiling:
            raise ValueError("Stored experiment-loop root exceeds its pinned ceiling.")
        unresolved = any(
            row["status"] in {"queued", "running", "pausing", "paused", "failed", "interrupted"}
            and "experiment_recovery_abandoned"
            not in receipt_categories.get(str(row["operation_id"]), set())
            for row, _request in latest_by_invocation.values()
        )
        detached_work_active = any(
            record.status in {"active", "degraded"} for record in compatible_watchers
        )
        watcher_degraded = any(record.status == "degraded" for record in compatible_watchers)
        watcher_completion_pending = any(
            record.status == "completed" and not record.notified for record in compatible_watchers
        )
        has_watcher = detached_work_active or watcher_completion_pending
        episode_exited = any(
            "experiment_loop_exit" in receipt_categories.get(str(row["operation_id"]), set())
            for row, _request in episode_entries
        )
        at_ceiling = invocations_used >= ceiling
        pins = root_request.get("control_decision_bundle")
        if not isinstance(pins, list):
            raise ValueError("Stored experiment-loop root is missing its pinned decision bundle.")
        control_revision = root_request.get("control_revision")
        if not isinstance(control_revision, int) or isinstance(control_revision, bool):
            raise ValueError("Stored experiment-loop root is missing its control revision.")
        completion_criteria = root_request.get("control_completion_criteria")
        if not isinstance(completion_criteria, list) or any(
            not isinstance(item, str) for item in completion_criteria
        ):
            raise ValueError("Stored experiment-loop root is missing its completion criteria.")
        current_row, current_request = max(
            episode_entries,
            key=lambda entry: (entry[0]["created_at"], entry[0]["storage_rowid"]),
        )
        current_invocation = current_request.get("control_invocation")
        return ExperimentLoopRuntime(
            episode_id=episode_id,
            invocations_used=invocations_used,
            invocation_ceiling=ceiling,
            control_revision=control_revision,
            task_active=unresolved,
            detached_work_active=detached_work_active,
            watcher_degraded=watcher_degraded,
            watcher_completion_pending=watcher_completion_pending,
            episode_exited=episode_exited,
            active=unresolved
            or (
                has_watcher
                and not at_ceiling
                and not episode_exited
                and not (episode is not None and episode.stop_requested_at is not None)
            ),
            paused=has_watcher
            and at_ceiling
            and not unresolved
            and not episode_exited
            and not (episode is not None and episode.stop_requested_at is not None),
            decision_bundle=pins,
            completion_criteria=completion_criteria,
            stop_requested=episode is not None and episode.stop_requested_at is not None,
            stop_settled=episode is not None and episode.stop_settled_at is not None,
            session_bound=episode is not None and episode.session_bound,
            session_diagnostic=episode.session_diagnostic if episode else None,
            provider=_optional_str(root_request.get("provider")),
            model=(root_request["model"] if isinstance(root_request.get("model"), str) else None),
            reasoning=_optional_str(root_request.get("reasoning")),
            run_on=_optional_str(root_request.get("run_on")),
            execution_host=episode.execution_host if episode else None,
            run_truth_scope=(
                [str(item) for item in root_request["run_truth_scope"]]
                if isinstance(root_request.get("run_truth_scope"), list)
                else None
            ),
            chat_id=_optional_str(root_request.get("chat_id")),
            current_operation_id=current_row["operation_id"],
            current_status=current_row["status"],
            current_phase=current_row["phase"],
            current_status_message=current_row["status_message"],
            current_last_activity_at=current_row["last_activity_at"],
            current_invocation=(
                current_invocation if isinstance(current_invocation, int) else None
            ),
        )

    @staticmethod
    def _experiment_watcher_matches_current(
        record: WatcherRecord,
        root_request: dict[str, object],
        episode: ExperimentEpisodeRecord | None,
    ) -> bool:
        """Whether watcher provenance can automatically wake the current episode.

        Model, reasoning, and package pointers deliberately are not selectors:
        the current episode owns those values. Human reauthorization uses the
        frozen full continuation through a separate grouping path.
        """

        continuation = record.continuation
        expected_scope = _experiment_pinned_value(root_request, "run_truth_scope")
        actual_scope = _experiment_pinned_value(
            continuation.model_dump(mode="json"),
            "run_truth_scope",
        )
        episode_matches = episode is None or (
            record.project_id == episode.project_id
            and episode.control_node_id == root_request.get("control_node_id")
            and (episode.chat_id is None or episode.chat_id == record.chat_id)
            and (
                episode.execution_machine is None
                or (
                    episode.execution_machine == continuation.run_on
                    and record.execution_host == episode.execution_host
                )
            )
        )
        return (
            continuation.patch_kind == "experiment_loop"
            and continuation.provider == root_request.get("provider")
            and continuation.run_on == root_request.get("run_on")
            and continuation.control_node_id == root_request.get("control_node_id")
            and record.chat_id == root_request.get("chat_id")
            and record.node_id == root_request.get("node_id")
            and expected_scope == actual_scope
            and episode_matches
        )

    @staticmethod
    def _experiment_episode_root_request(
        connection: sqlite3.Connection,
        project_id: str,
        control_node_id: str,
        episode_id: str,
    ) -> dict[str, object] | None:
        row = connection.execute(
            """
            SELECT request_json FROM graph_runs
            WHERE project_id = ? AND parent_operation_id IS NULL
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
              AND json_extract(request_json, '$.control_episode_id') = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (project_id, control_node_id, episode_id),
        ).fetchone()
        return json.loads(row["request_json"]) if row is not None else None

    @classmethod
    def _watcher_suppressed_by_current_stop(
        cls,
        connection: sqlite3.Connection,
        record: WatcherRecord,
        cache: dict[
            tuple[str, str],
            tuple[dict[str, object], ExperimentEpisodeRecord] | None,
        ],
    ) -> bool:
        continuation = record.continuation
        control_node_id = continuation.control_node_id
        if continuation.patch_kind != "experiment_loop" or not control_node_id:
            return False
        key = (record.project_id, control_node_id)
        if key not in cache:
            root = connection.execute(
                """
                SELECT request_json FROM graph_runs
                WHERE project_id = ? AND parent_operation_id IS NULL
                  AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
                  AND json_extract(request_json, '$.control_node_id') = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                key,
            ).fetchone()
            context = None
            if root is not None:
                root_request = json.loads(root["request_json"])
                episode_id = root_request.get("control_episode_id")
                episode_row = (
                    connection.execute(
                        "SELECT * FROM experiment_episodes WHERE episode_id = ?",
                        (episode_id,),
                    ).fetchone()
                    if isinstance(episode_id, str)
                    else None
                )
                if episode_row is not None and episode_row["stop_requested_at"] is not None:
                    context = (root_request, cls._experiment_episode_record(episode_row))
            cache[key] = context
        context = cache[key]
        return context is not None and cls._experiment_watcher_matches_current(
            record,
            context[0],
            context[1],
        )

    def experiment_watcher_compatible_with_episode(
        self,
        watcher_id: str,
        episode_id: str,
    ) -> bool:
        """Whether a stopped observer belonged to that episode operationally.

        Watcher origin remains immutable provenance. This derived relation lets
        a fresh post-stop Run stage compatible adopted observers as history even
        when an older invocation or episode originally armed them.
        """

        with self.connection() as connection:
            watcher_row = connection.execute(
                "SELECT * FROM watchers WHERE watcher_id = ?",
                (watcher_id,),
            ).fetchone()
            episode_row = connection.execute(
                "SELECT * FROM experiment_episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            if watcher_row is None or episode_row is None:
                return False
            record = self._watcher_record(watcher_row)
            episode = self._experiment_episode_record(episode_row)
            root_request = self._experiment_episode_root_request(
                connection,
                episode.project_id,
                episode.control_node_id,
                episode_id,
            )
        return root_request is not None and self._experiment_watcher_matches_current(
            record,
            root_request,
            episode,
        )

    def active_experiment_control_ids(self, project_id: str) -> set[str]:
        """Return Experiments whose newest operational episode is still live."""

        return {
            control_node_id
            for control_node_id, runtime in self._project_experiment_loop_runtimes(
                project_id, None
            ).items()
            if runtime.active
        }

    def record_watcher_check(
        self,
        watcher_id: str,
        *,
        status: WatcherStatus,
        exit_code: int | None,
        error: str | None,
        checked_at: str | None = None,
    ) -> WatcherRecord:
        if status == "degraded" and not error:
            raise ValueError("a degraded watcher requires a check error")
        if status != "degraded":
            error = None
        timestamp = checked_at or self.now()
        completed_at = timestamp if status == "completed" else None
        with self.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE watchers
                SET status = ?, last_checked_at = ?, last_exit_code = ?, last_error = ?,
                    completed_at = CASE
                        WHEN ? = 'completed' THEN COALESCE(completed_at, ?)
                        ELSE completed_at
                    END
                WHERE watcher_id = ? AND status IN ('active', 'degraded')
                """,
                (
                    status,
                    timestamp,
                    exit_code,
                    error,
                    status,
                    completed_at,
                    watcher_id,
                ),
            )
            if cursor.rowcount == 0:
                existing = connection.execute(
                    "SELECT 1 FROM watchers WHERE watcher_id = ?", (watcher_id,)
                ).fetchone()
                if existing is None:
                    raise KeyError(watcher_id)
        stored = self.watcher(watcher_id)
        assert stored is not None
        return stored

    def completed_watcher_groups(self) -> list[list[WatcherRecord]]:
        """Group completed watchers by conversation and compatible continuation policy."""

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM watchers
                WHERE status = 'completed' AND notified = 0
                ORDER BY completed_at, created_at, watcher_id
                """
            ).fetchall()
            records = [self._watcher_record(row) for row in rows]
            stopping_contexts: dict[
                tuple[str, str],
                tuple[dict[str, object], ExperimentEpisodeRecord] | None,
            ] = {}
            records = [
                record
                for record in records
                if not self._watcher_suppressed_by_current_stop(
                    connection,
                    record,
                    stopping_contexts,
                )
            ]
        groups: dict[tuple[object, ...], list[WatcherRecord]] = {}
        for record in records:
            key = (
                record.project_id,
                record.origin_task_kind,
                record.chat_id,
                record.node_id,
                record.execution_host,
                self._automatic_watcher_delivery_policy(record.continuation),
            )
            groups.setdefault(key, []).append(record)
        return list(groups.values())

    def completed_experiment_watcher_group(
        self,
        project_id: str,
        control_node_id: str,
    ) -> list[WatcherRecord] | None:
        """Return the oldest frozen group a human may reauthorize.

        Unlike automatic delivery, human reauthorization preserves the full
        watcher configuration, including model, reasoning, and package pointers.
        """

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM watchers
                WHERE project_id = ? AND status = 'completed' AND notified = 0
                  AND json_extract(continuation_json, '$.patch_kind') = 'experiment_loop'
                  AND json_extract(continuation_json, '$.control_node_id') = ?
                ORDER BY completed_at, created_at, watcher_id
                """,
                (project_id, control_node_id),
            ).fetchall()
        groups: dict[tuple[object, ...], list[WatcherRecord]] = {}
        for row in rows:
            record = self._watcher_record(row)
            key = (
                record.origin_task_kind,
                record.chat_id,
                record.node_id,
                record.execution_host,
                self._watcher_delivery_policy(record.continuation),
            )
            groups.setdefault(key, []).append(record)
        return next(iter(groups.values()), None)

    def create_watcher_notification_task(
        self,
        record: AgentTaskRecord,
        watcher_ids: list[str],
    ) -> AgentTaskRecord | None:
        """Queue a wake and mark its completed watchers notified in one transaction.

        A live task in the same conversation wins its slot. In that case no
        watcher row changes, and the completed group can be retried later.
        """

        ids = list(dict.fromkeys(watcher_ids))
        if not ids or len(ids) != len(watcher_ids):
            raise ValueError("a watcher notification requires unique watcher ids")
        if record.status != "queued":
            raise ValueError("a watcher notification task must be queued")
        requested_ids = record.request.get("watcher_ids")
        if (
            not isinstance(requested_ids, list)
            or any(not isinstance(item, str) for item in requested_ids)
            or len(requested_ids) != len(set(requested_ids))
            or set(requested_ids) != set(ids)
        ):
            raise ValueError("the watcher notification request must name exactly its watcher ids")
        placeholders = ",".join("?" for _ in ids)
        try:
            with self.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    f"""
                    SELECT *
                    FROM watchers
                    WHERE watcher_id IN ({placeholders})
                        AND status = 'completed' AND notified = 0
                    """,
                    ids,
                ).fetchall()
                if {str(row["watcher_id"]) for row in rows} != set(ids):
                    raise ValueError("watchers are missing, incomplete, or already notified")
                watchers = [self._watcher_record(row) for row in rows]
                if {item.project_id for item in watchers} != {record.project_id}:
                    raise ValueError("watchers and notification task belong to different projects")
                bindings = {
                    (
                        item.origin_task_kind,
                        item.chat_id,
                        item.node_id,
                        item.execution_host,
                        (
                            self._automatic_watcher_delivery_policy(item.continuation)
                            if record.request.get("trigger") == "watcher"
                            else self._watcher_delivery_policy(item.continuation)
                        ),
                    )
                    for item in watchers
                }
                if len(bindings) != 1:
                    raise ValueError("one notification cannot merge incompatible watch lists")
                self._validate_watcher_notification_scope(connection, record, watchers)
                if self._experiment_wake_is_stopped(connection, record):
                    return None
                if self._has_active_chat_overlap(connection, record):
                    return None
                self._insert_agent_task(connection, record)
                cursor = connection.execute(
                    f"""
                    UPDATE watchers
                    SET notified = 1, notification_operation_id = ?
                    WHERE watcher_id IN ({placeholders})
                        AND status = 'completed' AND notified = 0
                    """,
                    [record.operation_id, *ids],
                )
                if cursor.rowcount != len(ids):
                    raise RuntimeError("watcher notification changed during its transaction")
        except sqlite3.IntegrityError as exc:
            raise ValueError("Could not queue the watcher notification task.") from exc
        stored = self.agent_task(record.operation_id)
        assert stored is not None
        return stored

    @staticmethod
    def _experiment_wake_is_stopped(
        connection: sqlite3.Connection,
        record: AgentTaskRecord,
    ) -> bool:
        """Refuse an automatic wake whose episode already carries a stop request.

        The check runs inside the claim's own write transaction, so a claim either
        commits before the stop or finds it — there is no window where both win.
        """

        request = record.request
        if request.get("patch_kind") != "experiment_loop" or request.get("trigger") != "watcher":
            return False
        episode_id = request.get("control_episode_id")
        if not isinstance(episode_id, str):
            return False
        row = connection.execute(
            "SELECT stop_requested_at FROM experiment_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        return row is not None and row["stop_requested_at"] is not None

    @staticmethod
    def _watcher_delivery_policy(continuation: WatcherContinuation) -> str:
        policy = continuation.model_dump(mode="json")
        if continuation.patch_kind == "experiment_loop" and policy.get("model") is None:
            # Legacy Experiment watchers stored the provider-default sentinel
            # as null. It is immutable policy, equivalent to today's "".
            policy["model"] = ""
        for field in (
            "control_revision",
            "control_episode_id",
            "control_invocation",
            "control_invocation_ceiling",
            "control_decision_bundle",
            "control_completion_criteria",
        ):
            policy.pop(field, None)
        return json.dumps(policy, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _automatic_watcher_delivery_policy(continuation: WatcherContinuation) -> str:
        """Policy key for poller-driven delivery; generic Work stays unchanged."""

        if continuation.patch_kind != "experiment_loop":
            return AppStore._watcher_delivery_policy(continuation)
        policy = {
            "provider": continuation.provider,
            "run_on": continuation.run_on,
            "run_truth_scope": sorted(set(continuation.run_truth_scope or [])),
            "patch_kind": continuation.patch_kind,
            "control_node_id": continuation.control_node_id,
        }
        return json.dumps(policy, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _validate_watcher_notification_scope(
        connection: sqlite3.Connection,
        record: AgentTaskRecord,
        watchers: list[WatcherRecord],
    ) -> None:
        first = watchers[0]
        continuation = first.continuation
        request = record.request
        expected = {
            "kind": first.origin_task_kind,
            "chat_id": first.chat_id,
            "node_id": first.node_id,
        }
        actual = {
            "kind": record.kind,
            "chat_id": request.get("chat_id"),
            "node_id": request.get("node_id"),
        }
        mismatched = sorted(key for key, value in expected.items() if actual[key] != value)
        if mismatched:
            raise ValueError(
                f"watcher notification changed immutable scope: {', '.join(mismatched)}"
            )
        request_continuation_data = {
            key: request[key] for key in WatcherContinuation.model_fields if key in request
        }
        for nullable_list in ("workflow_ids", "skill_ids", "resolved_skill_packages"):
            if request_continuation_data.get(nullable_list) is None:
                request_continuation_data[nullable_list] = []
        request_continuation = WatcherContinuation.model_validate(request_continuation_data)
        trigger = request.get("trigger")
        request_policy = (
            AppStore._automatic_watcher_delivery_policy(request_continuation)
            if trigger == "watcher" and continuation.patch_kind == "experiment_loop"
            else AppStore._watcher_delivery_policy(request_continuation)
        )
        continuation_policy = (
            AppStore._automatic_watcher_delivery_policy(continuation)
            if trigger == "watcher" and continuation.patch_kind == "experiment_loop"
            else AppStore._watcher_delivery_policy(continuation)
        )
        if request_policy != continuation_policy:
            raise ValueError("watcher notification changed its immutable delivery policy")
        if continuation.patch_kind != "experiment_loop":
            if trigger != "watcher":
                raise ValueError("a generic watcher notification must use the watcher trigger")
            return
        invocation = request.get("control_invocation")
        episode_id = request.get("control_episode_id")
        if trigger == "watcher":
            if not isinstance(invocation, int) or invocation < 2:
                raise ValueError("an automatic Experiment wake must continue an existing episode")
            newest = connection.execute(
                """
                SELECT request_json FROM graph_runs
                WHERE project_id = ? AND parent_operation_id IS NULL
                  AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
                  AND json_extract(request_json, '$.control_node_id') = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (record.project_id, continuation.control_node_id),
            ).fetchone()
            newest_request = json.loads(newest["request_json"]) if newest is not None else None
            if newest_request is None or newest_request.get("control_episode_id") != episode_id:
                raise ValueError("an automatic Experiment wake must use the newest episode")
            episode_row = connection.execute(
                "SELECT * FROM experiment_episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            episode = (
                AppStore._experiment_episode_record(episode_row)
                if episode_row is not None
                else None
            )
            if any(
                not AppStore._experiment_watcher_matches_current(item, newest_request, episode)
                for item in watchers
            ):
                raise ValueError(
                    "completed watchers are incompatible with the current Experiment episode"
                )
            return
        if trigger != "experiment_run" or invocation != 1:
            raise ValueError("a human Experiment watcher claim must start a new episode")
        previous = connection.execute(
            """
            SELECT request_json FROM graph_runs
            WHERE project_id = ? AND parent_operation_id IS NULL
              AND json_extract(request_json, '$.patch_kind') = 'experiment_loop'
              AND json_extract(request_json, '$.control_node_id') = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (record.project_id, continuation.control_node_id),
        ).fetchone()
        if previous is None:
            raise ValueError("a human watcher claim requires a prior Experiment episode")
        previous_request = json.loads(previous["request_json"])
        if previous_request.get("control_episode_id") == episode_id:
            raise ValueError("a human watcher claim must authorize a fresh episode")

    def agent_task(self, operation_id: str) -> AgentTaskRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT graph_runs.*,
                       EXISTS (
                           SELECT 1 FROM graph_run_receipts AS receipt
                           WHERE receipt.operation_id = graph_runs.operation_id
                             AND receipt.category = 'experiment_recovery_abandoned'
                       ) AS recovery_abandoned
                FROM graph_runs WHERE operation_id = ?
                """,
                (operation_id,),
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
            if not eligible:
                raise ValueError(
                    "This task has no repairable graph update. Start a new Work turn instead."
                )
            if request.get("patch_kind") == "experiment_loop":
                control_node_id = request.get("control_node_id")
                episode_id = request.get("control_episode_id")
                invocation = request.get("control_invocation")
                if (
                    not isinstance(control_node_id, str)
                    or not isinstance(episode_id, str)
                    or not isinstance(invocation, int)
                ):
                    raise ValueError("The Experiment graph repair lost its control binding.")
                self._validate_current_experiment_graph_repair(
                    connection,
                    project_id=data["project_id"],
                    control_node_id=control_node_id,
                    episode_id=episode_id,
                    invocation=invocation,
                    operation_id=operation_id,
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
                SELECT graph_runs.*,
                       EXISTS (
                           SELECT 1 FROM graph_run_receipts AS receipt
                           WHERE receipt.operation_id = graph_runs.operation_id
                             AND receipt.category = 'experiment_recovery_abandoned'
                       ) AS recovery_abandoned
                FROM graph_runs
                WHERE project_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (project_id, max(1, min(limit, AGENT_TASK_LIST_MAX_LIMIT))),
            ).fetchall()
        return [self._agent_task_record(row) for row in rows]

    def has_active_chat_task(
        self,
        project_id: str,
        kind: Literal["node_chat", "project_chat"],
        chat_id: str,
    ) -> bool:
        """Return whether one exact chat already owns an active task."""

        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM graph_runs
                WHERE project_id = ? AND kind = ?
                  AND json_extract(request_json, '$.chat_id') = ?
                  AND status IN ('queued', 'running', 'pausing')
                LIMIT 1
                """,
                (project_id, kind, chat_id),
            ).fetchone()
        return row is not None

    def has_chat_native_session_origin(
        self,
        project_id: str,
        kind: Literal["node_chat", "project_chat"],
        chat_id: str,
        node_id: str | None,
        provider: str,
        execution_machine: str,
        native_session_id: str,
    ) -> bool:
        """Prove that RCP previously observed this session on the exact chat binding."""

        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM graph_runs
                WHERE project_id = ? AND kind = ?
                  AND json_extract(request_json, '$.chat_id') = ?
                  AND json_extract(request_json, '$.node_id') IS ?
                  AND json_extract(request_json, '$.provider') = ?
                  AND json_extract(request_json, '$.run_on') = ?
                  AND native_session_id = ?
                LIMIT 1
                """,
                (
                    project_id,
                    kind,
                    chat_id,
                    node_id,
                    provider,
                    execution_machine,
                    native_session_id,
                ),
            ).fetchone()
        return row is not None

    def chat_session_context(
        self,
        provider: str,
        execution_machine: str,
        native_session_id: str,
    ) -> ChatSessionContextRecord | None:
        """Read the durable baseline for one exact native provider session."""

        with self.connection() as connection:
            row = self._chat_session_context_row(
                connection,
                provider,
                execution_machine,
                native_session_id,
            )
        return self._chat_session_context_record(row) if row is not None else None

    def validate_chat_session_context_binding(
        self,
        provider: str,
        execution_machine: str,
        native_session_id: str,
        *,
        project_id: str,
        kind: Literal["node_chat", "project_chat"],
        chat_id: str,
        node_id: str | None,
    ) -> ChatSessionContextRecord | None:
        """Return an existing baseline only when its complete binding matches."""

        with self.connection() as connection:
            row = self._chat_session_context_row(
                connection,
                provider,
                execution_machine,
                native_session_id,
            )
            if row is None:
                return None
            self._validate_chat_session_context_binding(
                row,
                project_id=project_id,
                kind=kind,
                chat_id=chat_id,
                node_id=node_id,
            )
        return self._chat_session_context_record(row)

    def commit_chat_session_context(
        self,
        *,
        provider: str,
        execution_machine: str,
        native_session_id: str,
        project_id: str,
        kind: Literal["node_chat", "project_chat"],
        chat_id: str,
        node_id: str | None,
        protocol_version: int,
        snapshot_json: str,
        snapshot_sha256: str,
        committed_operation_id: str,
        expected_snapshot_sha256: str | None,
    ) -> ChatSessionContextRecord:
        """CAS one session baseline, inserting only when no prior digest is expected."""

        now = self.now()
        ChatSessionContextRecord.model_validate(
            {
                "provider": provider,
                "execution_machine": execution_machine,
                "native_session_id": native_session_id,
                "project_id": project_id,
                "kind": kind,
                "chat_id": chat_id,
                "node_id": node_id,
                "protocol_version": protocol_version,
                "snapshot_json": snapshot_json,
                "snapshot_sha256": snapshot_sha256,
                "committed_operation_id": committed_operation_id,
                "created_at": now,
                "updated_at": now,
            }
        )
        try:
            json.loads(snapshot_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Chat session context snapshot must be valid JSON.") from exc
        actual_sha256 = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        if snapshot_sha256 != actual_sha256:
            raise ValueError("Chat session context snapshot SHA-256 does not match its JSON.")

        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._chat_session_context_row(
                    connection,
                    provider,
                    execution_machine,
                    native_session_id,
                )
                if row is None:
                    if expected_snapshot_sha256 is not None:
                        raise ValueError(
                            "Chat session context compare-and-swap failed: prior baseline is missing."
                        )
                    connection.execute(
                        """
                        INSERT INTO chat_session_contexts (
                            provider, execution_machine, native_session_id,
                            project_id, kind, chat_id, node_id, protocol_version,
                            snapshot_json, snapshot_sha256, committed_operation_id,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            provider,
                            execution_machine,
                            native_session_id,
                            project_id,
                            kind,
                            chat_id,
                            node_id,
                            protocol_version,
                            snapshot_json,
                            snapshot_sha256,
                            committed_operation_id,
                            now,
                            now,
                        ),
                    )
                else:
                    self._validate_chat_session_context_binding(
                        row,
                        project_id=project_id,
                        kind=kind,
                        chat_id=chat_id,
                        node_id=node_id,
                    )
                    if expected_snapshot_sha256 != row["snapshot_sha256"]:
                        raise ValueError(
                            "Chat session context compare-and-swap failed: prior digest changed."
                        )
                    changed = connection.execute(
                        """
                        UPDATE chat_session_contexts
                        SET protocol_version = ?, snapshot_json = ?, snapshot_sha256 = ?,
                            committed_operation_id = ?, updated_at = ?
                        WHERE provider = ? AND execution_machine = ? AND native_session_id = ?
                          AND snapshot_sha256 = ?
                        """,
                        (
                            protocol_version,
                            snapshot_json,
                            snapshot_sha256,
                            committed_operation_id,
                            now,
                            provider,
                            execution_machine,
                            native_session_id,
                            expected_snapshot_sha256,
                        ),
                    ).rowcount
                    if changed != 1:
                        raise ValueError(
                            "Chat session context compare-and-swap failed: prior digest changed."
                        )
            except Exception:
                connection.rollback()
                raise

        stored = self.chat_session_context(provider, execution_machine, native_session_id)
        assert stored is not None
        return stored

    def record_agent_usage(self, operation_id: str, usage: ProviderUsage) -> AgentUsageRecord:
        """Persist one provider usage report and mark duplicate reports excluded."""

        task = self.agent_task(operation_id)
        if task is None:
            raise ValueError(f"Cannot attribute provider usage to unknown task {operation_id!r}")
        usage_id = str(uuid.uuid4())
        now = self.now()
        with self.connection() as connection:
            duplicate = connection.execute(
                """
                SELECT 1 FROM agent_usage
                WHERE operation_id = ? AND provider_profile = ? AND dedupe_key = ?
                    AND counted = 1
                LIMIT 1
                """,
                (operation_id, usage.provider_profile, usage.dedupe_key),
            ).fetchone()
            counted = duplicate is None
            count_reason: AgentUsageCountReason = "counted" if counted else "duplicate"
            connection.execute(
                """
                INSERT INTO agent_usage (
                    usage_id, project_id, operation_id, provider, model,
                    task_kind, provider_profile, provider_event_type, dedupe_key, counted,
                    count_reason, created_at, processed_input_tokens,
                    generated_tokens, cached_input_tokens,
                    cache_creation_input_tokens, cache_write_input_tokens,
                    reasoning_output_tokens, reported_input_tokens,
                    reported_output_tokens, reported_total_tokens,
                    provider_fields_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    usage_id,
                    task.project_id,
                    operation_id,
                    task.request.get("provider") or "unknown",
                    task.request.get("model"),
                    task.kind,
                    usage.provider_profile,
                    usage.provider_event_type,
                    usage.dedupe_key,
                    int(counted),
                    count_reason,
                    now,
                    usage.processed_input_tokens,
                    usage.generated_tokens,
                    usage.cached_input_tokens,
                    usage.cache_creation_input_tokens,
                    usage.cache_write_input_tokens,
                    usage.reasoning_output_tokens,
                    usage.reported_input_tokens,
                    usage.reported_output_tokens,
                    usage.reported_total_tokens,
                    json.dumps(usage.provider_fields, separators=(",", ":")),
                ),
            )
        record = self.agent_usage_record(usage_id)
        assert record is not None
        return record

    def agent_usage_record(self, usage_id: str) -> AgentUsageRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM agent_usage WHERE usage_id = ?", (usage_id,)
            ).fetchone()
        return self._agent_usage_record(row) if row else None

    def agent_usage(self, project_id: str) -> list[AgentUsageRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_usage
                WHERE project_id = ?
                ORDER BY created_at ASC, usage_id ASC
                """,
                (project_id,),
            ).fetchall()
        return [self._agent_usage_record(row) for row in rows]

    def agent_usage_snapshot(self, project_id: str) -> AgentUsageSnapshot:
        records = self.agent_usage(project_id)
        counted = [record for record in records if record.counted]
        # Input reports describe the full context of one request. For a resumed
        # native session, later reports supersede earlier context sizes; generated
        # output is newly produced content and remains additive.
        latest_input_by_session: dict[tuple[str, str], AgentUsageRecord] = {}
        input_cells: dict[tuple[AgentTaskKind, str], AgentUsageCell] = {}
        generated_cells: dict[tuple[AgentTaskKind, str], AgentUsageCell] = {}
        for record in counted:
            task = self.agent_task(record.operation_id)
            if task is None:
                continue
            native_session_id = task.native_session_id or task.request.get("session_id")
            session_key = (
                (record.provider, native_session_id)
                if isinstance(native_session_id, str) and native_session_id
                else (record.provider, f"usage:{record.usage_id}")
            )
            previous = latest_input_by_session.get(session_key)
            if previous is None or (record.created_at, record.usage_id) > (
                previous.created_at,
                previous.usage_id,
            ):
                latest_input_by_session[session_key] = record

            key = (task.kind, record.provider)
            generated_cell = generated_cells.setdefault(
                key,
                AgentUsageCell(task_kind=task.kind, provider=record.provider),
            )
            generated_cell.generated_tokens += record.generated_tokens
            generated_cell.counted_records += 1

        for record in latest_input_by_session.values():
            task = self.agent_task(record.operation_id)
            if task is None:
                continue
            key = (task.kind, record.provider)
            input_cell = input_cells.setdefault(
                key,
                AgentUsageCell(task_kind=task.kind, provider=record.provider),
            )
            input_cell.processed_input_tokens += record.processed_input_tokens
            input_cell.cached_input_tokens += record.cached_input_tokens
            input_cell.counted_records += 1

        input_total = sum(cell.processed_input_tokens for cell in input_cells.values())
        generated_total = sum(cell.generated_tokens for cell in generated_cells.values())
        cached_total = sum(cell.cached_input_tokens for cell in input_cells.values())
        return AgentUsageSnapshot(
            project_id=project_id,
            input_processed=AgentUsageMetric(
                total_tokens=input_total,
                cached_tokens=cached_total,
                cache_share=cached_total / input_total if input_total else 0.0,
                block_tokens=input_total / 20 if input_total else 0.0,
                cells=sorted(
                    input_cells.values(),
                    key=lambda cell: (cell.task_kind, cell.provider),
                ),
            ),
            generated=AgentUsageMetric(
                total_tokens=generated_total,
                block_tokens=generated_total / 20 if generated_total else 0.0,
                cells=sorted(
                    generated_cells.values(),
                    key=lambda cell: (cell.task_kind, cell.provider),
                ),
            ),
            counted_records=len(counted),
            excluded_records=len(records) - len(counted),
            records=records,
        )

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
                    AND NOT EXISTS (
                        SELECT 1
                        FROM graph_run_receipts AS receipt
                        WHERE receipt.operation_id = paused.operation_id
                          AND receipt.category = 'experiment_recovery_abandoned'
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

    def agent_task_continuation_cause(self, operation_id: str) -> str | None:
        """Return the durable launch cause for one task attempt.

        Recovery must preserve patch-only graph-repair semantics instead of
        inferring a full Work turn from the request shape alone.
        """

        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM graph_run_receipts
                WHERE operation_id = ? AND category = 'operation_created'
                ORDER BY receipt_id ASC
                LIMIT 1
                """,
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        cause = payload.get("continuation_cause") if isinstance(payload, dict) else None
        return cause if isinstance(cause, str) and cause else None

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

    def agent_task_contracts(self, operation_id: str) -> list[AgentTaskContractRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT operation_id, role, created_at, sha256, content
                FROM graph_run_contracts
                WHERE operation_id = ?
                ORDER BY rowid
                """,
                (operation_id,),
            ).fetchall()
        return [AgentTaskContractRecord.model_validate(dict(row)) for row in rows]

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
                        raw_change_summary[:32] if isinstance(raw_change_summary, list) else []
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

    def pause_agent_task(
        self,
        operation_id: str,
        *,
        detail: str | None = None,
        result: dict[str, object] | None = None,
    ) -> None:
        now = self.now()
        detail = (
            detail or "Paused. Resume from the saved agent session, or retry from the beginning."
        )
        result_json = self._bounded_result_json(result) if result is not None else None
        with self.connection() as connection:
            connection.execute(
                """
                UPDATE graph_runs
                SET status = 'paused', updated_at = ?, finished_at = ?,
                    last_activity_at = ?, phase = 'paused', status_message = ?, error = NULL,
                    result_json = COALESCE(?, result_json)
                WHERE operation_id = ? AND status IN ('queued', 'running', 'pausing')
                """,
                (now, now, now, detail, result_json, operation_id),
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
        graph_rejected = isinstance(graph_update, dict) and graph_update.get("status") == "rejected"
        status_message = (
            "Completed; graph update rejected." if graph_rejected else "Agent task completed."
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
    def _chat_session_context_row(
        connection: sqlite3.Connection,
        provider: str,
        execution_machine: str,
        native_session_id: str,
    ) -> sqlite3.Row | None:
        rows = connection.execute(
            "SELECT * FROM chat_session_contexts WHERE native_session_id = ?",
            (native_session_id,),
        ).fetchall()
        conflicts = [
            row
            for row in rows
            if row["provider"] != provider or row["execution_machine"] != execution_machine
        ]
        if conflicts:
            raise ValueError(
                "Chat session context provider or execution-machine conflict for native session."
            )
        return next(
            (
                row
                for row in rows
                if row["provider"] == provider and row["execution_machine"] == execution_machine
            ),
            None,
        )

    @staticmethod
    def _validate_chat_session_context_binding(
        row: sqlite3.Row,
        *,
        project_id: str,
        kind: Literal["node_chat", "project_chat"],
        chat_id: str,
        node_id: str | None,
    ) -> None:
        expected = {
            "project_id": project_id,
            "kind": kind,
            "chat_id": chat_id,
            "node_id": node_id,
        }
        conflicts = [name for name, value in expected.items() if row[name] != value]
        if conflicts:
            raise ValueError(
                "Chat session context immutable binding conflict: " + ", ".join(conflicts)
            )

    @staticmethod
    def _chat_session_context_record(row: sqlite3.Row) -> ChatSessionContextRecord:
        return ChatSessionContextRecord.model_validate(dict(row))

    @staticmethod
    def _project_record(row: sqlite3.Row) -> ProjectRecord:
        data = dict(row)
        data["state_remote"] = bool(data["state_remote"])
        if data["reachable"] is not None:
            data["reachable"] = bool(data["reachable"])
        return ProjectRecord.model_validate(data)

    @staticmethod
    def _watcher_record(row: sqlite3.Row) -> WatcherRecord:
        data = dict(row)
        data["continuation"] = json.loads(data.pop("continuation_json"))
        data["notified"] = bool(data["notified"])
        return WatcherRecord.model_validate(data)

    @staticmethod
    def _experiment_episode_record(row: sqlite3.Row) -> ExperimentEpisodeRecord:
        data = dict(row)
        data["last_watcher_ids"] = json.loads(data.pop("last_watcher_ids_json"))
        data["context_baseline"] = json.loads(data.pop("context_baseline_json"))
        return ExperimentEpisodeRecord.model_validate(data)

    def _agent_task_record(self, row: sqlite3.Row) -> AgentTaskRecord:
        data = dict(row)
        recovery_abandoned = bool(data.pop("recovery_abandoned", False))
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
            and not recovery_abandoned
        )
        data["can_retry"] = (
            status in {"paused", "interrupted", "failed"} and not active and not recovery_abandoned
        )
        return AgentTaskRecord.model_validate(data)

    @staticmethod
    def _agent_usage_record(row: sqlite3.Row) -> AgentUsageRecord:
        data = dict(row)
        data["counted"] = bool(data["counted"])
        data["provider_fields"] = json.loads(data.pop("provider_fields_json"))
        return AgentUsageRecord.model_validate(data)

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


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
