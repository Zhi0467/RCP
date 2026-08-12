from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rcp.storage.models import (  # noqa: F401
    _EXPERIMENT_EPISODE_CONTEXT_CANDIDATE_ROLE,
    _EXPERIMENT_EPISODE_PINNED_FIELDS,
    _MISSING_EXPERIMENT_EPISODE_CONTEXT_DIAGNOSTIC,
    _PROJECT_ID_TABLES,
    ACTIVE_AGENT_TASK_STATUSES,
    SPACE_NAME_MAX_LENGTH,
    AgentCommandInvocationRecord,
    AgentTaskContractRecord,
    AgentTaskEventRecord,
    AgentTaskKind,
    AgentTaskReceiptRecord,
    AgentTaskReceiptTier,
    AgentTaskRecord,
    AgentTaskStatus,
    AgentUsageCell,
    AgentUsageCountReason,
    AgentUsageMetric,
    AgentUsageRecord,
    AgentUsageSnapshot,
    CampaignActorBinding,
    CampaignActorBusy,
    CampaignBudgetExhausted,
    CampaignBudgetMeter,
    CampaignEnding,
    CampaignInvocationRole,
    CampaignMessageRecord,
    CampaignMessageRole,
    CampaignNotRunning,
    CampaignRecord,
    CampaignRecoveryMode,
    CampaignRecoveryPurpose,
    CampaignRecoveryRecord,
    CampaignRecoveryStatus,
    CampaignReportRecord,
    CampaignStatus,
    ChatSessionContextRecord,
    ExperimentEpisodeRecord,
    ExperimentLoopRuntime,
    ExperimentWatcherResourceRecord,
    GraphCondition,
    GraphWatcherRecord,
    NodeStatusGraphCondition,
    ProjectRecord,
    ProjectStageRecord,
    ProposalResolvedGraphCondition,
    ProviderSkillInventoryRecord,
    ResultViewConflict,
    ResultViewRecord,
    SpaceKind,
    SpaceUserKind,
    SpaceUserRecord,
    StoredWatcherRecord,
    TeamAuthenticationError,
    TeamInvitationRecord,
    WatcherClaimConflict,
    WatcherContinuation,
    WatcherDeliveryRecord,
    WatcherRecord,
    WatcherStatus,
    WatcherStopRequest,
    _canonical_space_id,
    _canonical_uuid4,
    _discard_failed_team_initialization,
    _experiment_pinned_value,
    _new_enrollment_code,
    _new_member_token,
    _new_session_token,
    _optional_str,
    _parse_enrollment_code,
    _plain_html_name,
    _required_timestamp,
    _result_view_html_bytes,
    _result_view_is_visible,
    _result_view_reference_time,
    _sha256,
    _stored_space_kind,
    _validated_result_view_html,
    normalize_space_name,
    watcher_next_check_at,
)

if TYPE_CHECKING:
    from rcp.storage import AppStore


class AppStoreBase:
    """Connection ownership, schema initialization, and the clock every mixin shares."""

    def __init__(self, path: Path, *, space_kind: SpaceKind | None = None) -> None:
        if space_kind is not None and space_kind not in ("personal", "team"):
            raise ValueError("space kind must be 'personal' or 'team'")
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize(space_kind)

    @classmethod
    def initialize_team_space(cls, path: Path, name: str) -> tuple[AppStore, str]:
        store = cls.__new__(cls)
        store.path = path
        store.path.parent.mkdir(parents=True, exist_ok=True)
        initial_space_id = str(uuid.uuid4())
        try:
            bootstrap_code = store._initialize(
                "team",
                initial_space_id=initial_space_id,
                initial_space_name=normalize_space_name(name),
                issue_bootstrap=True,
                require_new=True,
            )
        except Exception:
            _discard_failed_team_initialization(path, initial_space_id)
            raise
        if bootstrap_code is None:  # pragma: no cover - guarded by issue_bootstrap
            raise RuntimeError("RCP team bootstrap code was not created.")
        return store, bootstrap_code

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(
        self,
        requested_space_kind: SpaceKind | None,
        *,
        initial_space_id: str | None = None,
        initial_space_name: str | None = None,
        issue_bootstrap: bool = False,
        require_new: bool = False,
    ) -> str | None:
        bootstrap_code: str | None = None
        recovering_team_initialization = False
        with self.connection() as connection:
            try:
                connection.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError:
                # A concurrent first opener may be changing the journal mode.
                # Waiting for a write boundary proves that transaction finished
                # before retrying the same required mode change.
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
                connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            identity_table_exists = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'space_identity'"
                ).fetchone()
                is not None
            )
            if require_new and identity_table_exists:
                identity_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(space_identity)")
                }
                users_table_exists_for_recovery = (
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'space_users'"
                    ).fetchone()
                    is not None
                )
                existing_identity = (
                    connection.execute(
                        "SELECT space_kind, space_name FROM space_identity WHERE singleton = 1"
                    ).fetchone()
                    if {"space_kind", "space_name"}.issubset(identity_columns)
                    else None
                )
                existing_user_count = (
                    connection.execute("SELECT COUNT(*) FROM space_users").fetchone()[0]
                    if users_table_exists_for_recovery
                    else -1
                )
                recovering_team_initialization = bool(
                    issue_bootstrap
                    and initial_space_name is not None
                    and existing_identity is not None
                    and existing_identity["space_kind"] == "team"
                    and existing_identity["space_name"] == initial_space_name
                    and existing_user_count == 0
                )
                if not recovering_team_initialization:
                    raise ValueError("This RCP data directory already contains a space.")
            if not identity_table_exists:
                legacy_database = (
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                    ).fetchone()
                    is not None
                )
                if require_new and legacy_database:
                    raise ValueError("This RCP data directory already contains RCP data.")
                stored_space_kind = (
                    "personal" if legacy_database else requested_space_kind or "personal"
                )
                if requested_space_kind is not None and requested_space_kind != stored_space_kind:
                    raise ValueError(
                        "An existing RCP database migrates to personal; it cannot be opened "
                        f"as {requested_space_kind}."
                    )
                connection.execute(
                    """
                    CREATE TABLE space_identity (
                        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                        space_id TEXT NOT NULL UNIQUE,
                        space_kind TEXT NOT NULL CHECK(space_kind IN ('personal', 'team')),
                        space_name TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO space_identity(singleton, space_id, space_kind, space_name)
                    VALUES (1, ?, ?, ?)
                    """,
                    (initial_space_id or str(uuid.uuid4()), stored_space_kind, initial_space_name),
                )
            else:
                identity_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(space_identity)")
                }
                if "space_id" not in identity_columns:
                    raise RuntimeError("RCP space identity schema is invalid.")
                identity = connection.execute(
                    "SELECT space_id FROM space_identity WHERE singleton = 1"
                ).fetchone()
                if identity is None:
                    raise RuntimeError("RCP space identity is unavailable.")
                _canonical_space_id(identity["space_id"])
                if "space_kind" not in identity_columns:
                    connection.execute(
                        """
                        ALTER TABLE space_identity
                        ADD COLUMN space_kind TEXT CHECK(space_kind IN ('personal', 'team'))
                        """
                    )
                    connection.execute(
                        "UPDATE space_identity SET space_kind = 'personal' WHERE singleton = 1"
                    )
                    stored_space_kind = "personal"
                else:
                    identity = connection.execute(
                        "SELECT space_kind FROM space_identity WHERE singleton = 1"
                    ).fetchone()
                    assert identity is not None
                    stored_space_kind = _stored_space_kind(identity["space_kind"])

                if requested_space_kind is not None and requested_space_kind != stored_space_kind:
                    raise ValueError(
                        f"RCP space is {stored_space_kind}; it cannot be opened as "
                        f"{requested_space_kind}."
                    )

            identity = connection.execute(
                "SELECT space_id, space_kind FROM space_identity WHERE singleton = 1"
            ).fetchone()
            if identity is None:
                raise RuntimeError("RCP space identity is unavailable.")
            _canonical_space_id(identity["space_id"])
            stored_space_kind = _stored_space_kind(identity["space_kind"])

            users_table_exists = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'space_users'"
                ).fetchone()
                is not None
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS space_users (
                    user_id TEXT PRIMARY KEY,
                    identity_kind TEXT NOT NULL
                        CHECK(identity_kind IN ('local_owner', 'team_member')),
                    display_name TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS team_bootstrap_codes (
                    code_id TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    consumed_at TEXT,
                    consumed_by TEXT,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS team_invitations (
                    invitation_id TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    consumed_by TEXT,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS team_member_tokens (
                    token_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS team_sessions (
                    session_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            if not users_table_exists and stored_space_kind == "personal":
                now = self.now()
                owner = SpaceUserRecord(
                    user_id=str(uuid.uuid4()),
                    identity_kind="local_owner",
                    created_at=now,
                    updated_at=now,
                )
                connection.execute(
                    """
                    INSERT INTO space_users (
                        user_id, identity_kind, display_name, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        owner.user_id,
                        owner.identity_kind,
                        owner.display_name,
                        owner.created_at,
                        owner.updated_at,
                    ),
                )
            users = self._space_users_from_connection(connection)
            if stored_space_kind == "personal":
                if len(users) != 1 or users[0].identity_kind != "local_owner":
                    raise RuntimeError("A personal RCP space must contain exactly one local owner.")
            elif any(user.identity_kind == "local_owner" for user in users):
                raise RuntimeError("A team RCP space cannot contain a local owner.")

            # S111 stores may already have the earlier trigger that protected
            # only ``space_id``. Replace it atomically so the additive kind is
            # covered as soon as the migration commits.
            connection.execute("DROP TRIGGER IF EXISTS space_identity_immutable")
            connection.execute(
                """
                CREATE TRIGGER space_identity_immutable
                BEFORE UPDATE OF singleton, space_id, space_kind ON space_identity
                BEGIN
                    SELECT RAISE(ABORT, 'space identity is immutable');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS space_user_identity_immutable
                BEFORE UPDATE OF user_id, identity_kind ON space_users
                BEGIN
                    SELECT RAISE(ABORT, 'space user identity is immutable');
                END
                """
            )
            connection.commit()
            connection.executescript(
                """
                BEGIN IMMEDIATE;
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
                CREATE TABLE IF NOT EXISTS result_views (
                    view_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    experiment_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    origin_operation_id TEXT NOT NULL,
                    latest_operation_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    reasoning TEXT NOT NULL,
                    run_on TEXT NOT NULL,
                    native_session_id TEXT NOT NULL,
                    stage_host TEXT NOT NULL,
                    stage_root TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
                    html TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    kept_filename TEXT,
                    kept_at TEXT,
                    CHECK((kept_filename IS NULL) = (kept_at IS NULL))
                );
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    home_space_id TEXT,
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
                CREATE TABLE IF NOT EXISTS project_aliases (
                    alias_id TEXT PRIMARY KEY,
                    canonical_project_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS project_aliases_canonical
                    ON project_aliases(canonical_project_id, alias_id);
                CREATE TABLE IF NOT EXISTS provider_skill_inventories (
                    provider TEXT NOT NULL,
                    host TEXT NOT NULL,
                    configured_binary TEXT NOT NULL,
                    resolved_binary TEXT,
                    provider_version TEXT,
                    command_json TEXT NOT NULL DEFAULT '[]',
                    protocol TEXT,
                    skills_json TEXT NOT NULL DEFAULT '[]',
                    inventory_hash TEXT,
                    status TEXT NOT NULL,
                    diagnostic TEXT,
                    refreshed_at TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(provider, host, configured_binary)
                );
                CREATE TABLE IF NOT EXISTS graph_runs (
                    operation_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    campaign_id TEXT,
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
                    last_activity_at TEXT,
                    campaign_worker_handoffs_cleared_at TEXT,
                    dispatch_authority_json TEXT,
                    authorized_space_id TEXT,
                    authorized_user_id TEXT,
                    authorized_display_name TEXT
                );
                CREATE INDEX IF NOT EXISTS graph_runs_project
                    ON graph_runs(project_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS campaigns (
                    campaign_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    root_operation_id TEXT,
                    status TEXT NOT NULL,
                    starting_instruction TEXT,
                    invocation_ceiling INTEGER NOT NULL CHECK(invocation_ceiling >= 1),
                    invocations_used INTEGER NOT NULL DEFAULT 0
                        CHECK(invocations_used >= 0 AND invocations_used <= invocation_ceiling),
                    authorized_space_id TEXT NOT NULL,
                    authorized_user_id TEXT NOT NULL,
                    authorized_display_name TEXT NOT NULL,
                    stop_requested_at TEXT,
                    ending TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    ended_at TEXT
                );
                CREATE INDEX IF NOT EXISTS campaigns_project
                    ON campaigns(project_id, created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS campaigns_one_live_project
                    ON campaigns(project_id)
                    WHERE status IN (
                        'queued', 'running', 'stopping', 'wrapping_up', 'needs_action'
                    );
                CREATE TABLE IF NOT EXISTS campaign_invocations (
                    campaign_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(campaign_id, operation_id),
                    FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id),
                    FOREIGN KEY(operation_id) REFERENCES graph_runs(operation_id)
                );
                CREATE INDEX IF NOT EXISTS campaign_invocations_campaign
                    ON campaign_invocations(campaign_id, created_at, operation_id);
                CREATE TABLE IF NOT EXISTS campaign_reports (
                    report_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL UNIQUE,
                    ending TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    html TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id),
                    FOREIGN KEY(operation_id) REFERENCES graph_runs(operation_id)
                );
                CREATE INDEX IF NOT EXISTS campaign_reports_campaign
                    ON campaign_reports(campaign_id, created_at, report_id);
                CREATE TABLE IF NOT EXISTS campaign_messages (
                    message_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    sender_role TEXT NOT NULL,
                    sender_task_id TEXT,
                    authorized_space_id TEXT,
                    authorized_user_id TEXT,
                    authorized_display_name TEXT,
                    recipient_task_id TEXT NOT NULL,
                    control_node_id TEXT,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    delivery_operation_id TEXT,
                    FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id)
                );
                CREATE INDEX IF NOT EXISTS campaign_messages_campaign
                    ON campaign_messages(campaign_id, created_at, message_id);
                CREATE TABLE IF NOT EXISTS campaign_recoveries (
                    recovery_id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    operation_id TEXT,
                    purpose TEXT NOT NULL,
                    failure_kind TEXT NOT NULL,
                    retry_mode TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                    max_attempts INTEGER NOT NULL CHECK(max_attempts >= 1),
                    status TEXT NOT NULL,
                    next_attempt_at TEXT,
                    diagnostic TEXT NOT NULL,
                    admitted_operation_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(campaign_id) REFERENCES campaigns(campaign_id),
                    FOREIGN KEY(operation_id) REFERENCES graph_runs(operation_id),
                    FOREIGN KEY(admitted_operation_id) REFERENCES graph_runs(operation_id)
                );
                CREATE INDEX IF NOT EXISTS campaign_recoveries_due
                    ON campaign_recoveries(status, next_attempt_at, created_at);
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
                    event_kind TEXT NOT NULL DEFAULT 'message',
                    command_id TEXT,
                    campaign_id TEXT,
                    command_verb TEXT,
                    command_phase TEXT,
                    idempotency_key TEXT,
                    payload_json TEXT,
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
                    experiment_episode_id TEXT,
                    execution_host TEXT NOT NULL,
                    check_command TEXT NOT NULL,
                    log_path TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    graph_condition_json TEXT,
                    armed_revision INTEGER,
                    continuation_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_checked_at TEXT,
                    last_exit_code INTEGER,
                    last_error TEXT,
                    completed_at TEXT,
                    next_check_at TEXT,
                    consecutive_error_count INTEGER NOT NULL DEFAULT 0,
                    group_id TEXT,
                    group_label TEXT,
                    notified INTEGER NOT NULL DEFAULT 0,
                    notification_operation_id TEXT,
                    stopped_by TEXT,
                    stop_reason TEXT,
                    stopped_at TEXT,
                    stop_operation_id TEXT
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
            self._ensure_column(connection, "projects", "home_space_id", "TEXT")
            self._ensure_column(connection, "space_identity", "space_name", "TEXT")
            self._ensure_column(connection, "paper_drafts", "ancestor_content", "TEXT")
            self._ensure_column(
                connection,
                "result_views",
                "html",
                "TEXT NOT NULL DEFAULT ''",
            )
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
            self._ensure_column(
                connection,
                "graph_runs",
                "campaign_worker_handoffs_cleared_at",
                "TEXT",
            )
            self._ensure_column(connection, "graph_runs", "result_json", "TEXT")
            self._ensure_column(connection, "graph_runs", "dispatch_authority_json", "TEXT")
            self._ensure_column(connection, "graph_runs", "authorized_space_id", "TEXT")
            self._ensure_column(connection, "graph_runs", "authorized_user_id", "TEXT")
            self._ensure_column(connection, "graph_runs", "authorized_display_name", "TEXT")
            self._ensure_column(connection, "graph_runs", "campaign_id", "TEXT")
            self._ensure_column(connection, "campaign_messages", "authorized_space_id", "TEXT")
            self._ensure_column(connection, "campaign_messages", "authorized_user_id", "TEXT")
            self._ensure_column(
                connection,
                "campaign_messages",
                "authorized_display_name",
                "TEXT",
            )
            self._ensure_column(
                connection,
                "graph_run_events",
                "event_kind",
                "TEXT NOT NULL DEFAULT 'message'",
            )
            self._ensure_column(connection, "graph_run_events", "command_id", "TEXT")
            self._ensure_column(connection, "graph_run_events", "campaign_id", "TEXT")
            self._ensure_column(connection, "graph_run_events", "command_verb", "TEXT")
            self._ensure_column(connection, "graph_run_events", "command_phase", "TEXT")
            self._ensure_column(connection, "graph_run_events", "idempotency_key", "TEXT")
            self._ensure_column(connection, "graph_run_events", "payload_json", "TEXT")
            self._ensure_column(connection, "watchers", "next_check_at", "TEXT")
            self._ensure_column(
                connection,
                "watchers",
                "consecutive_error_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(connection, "watchers", "group_id", "TEXT")
            self._ensure_column(connection, "watchers", "group_label", "TEXT")
            self._ensure_column(connection, "watchers", "experiment_episode_id", "TEXT")
            self._ensure_column(connection, "watchers", "stopped_by", "TEXT")
            self._ensure_column(connection, "watchers", "stop_reason", "TEXT")
            self._ensure_column(connection, "watchers", "stopped_at", "TEXT")
            self._ensure_column(connection, "watchers", "stop_operation_id", "TEXT")
            self._ensure_column(connection, "watchers", "graph_condition_json", "TEXT")
            self._ensure_column(connection, "watchers", "armed_revision", "INTEGER")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS result_views_project_experiment "
                "ON result_views(project_id, experiment_id, updated_at DESC, view_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS result_views_project_chat "
                "ON result_views(project_id, chat_id, updated_at DESC, view_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS result_views_expiry "
                "ON result_views(expires_at, kept_filename)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS team_member_tokens_hash "
                "ON team_member_tokens(token_hash)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS team_member_tokens_active_user "
                "ON team_member_tokens(user_id) WHERE revoked_at IS NULL"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS team_invitations_creator "
                "ON team_invitations(created_by, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS team_sessions_user_expiry "
                "ON team_sessions(user_id, expires_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS graph_runs_campaign "
                "ON graph_runs(campaign_id, created_at, operation_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS graph_run_events_command "
                "ON graph_run_events(command_id, command_phase, event_id)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS graph_run_events_command_start_id "
                "ON graph_run_events(command_id) "
                "WHERE event_kind = 'command' AND command_phase = 'start'"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS graph_run_events_command_exit_id "
                "ON graph_run_events(command_id) "
                "WHERE event_kind = 'command' AND command_phase = 'exit'"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS graph_run_events_campaign_key_start "
                "ON graph_run_events(campaign_id, idempotency_key) "
                "WHERE event_kind = 'command' AND command_phase = 'start' "
                "AND campaign_id IS NOT NULL AND idempotency_key IS NOT NULL"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS watchers_due "
                "ON watchers(status, next_check_at, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS watchers_due_unclaimed "
                "ON watchers(status, notified, next_check_at, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS watchers_group_members "
                "ON watchers(group_id, created_at, watcher_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS watchers_group_delivery_candidates "
                "ON watchers(notified, status, group_id, consecutive_error_count)"
            )
            connection.execute(
                "UPDATE watchers SET experiment_episode_id = "
                "json_extract(continuation_json, '$.control_episode_id') "
                "WHERE experiment_episode_id IS NULL "
                "AND json_extract(continuation_json, '$.patch_kind') = 'experiment_loop'"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS watchers_experiment_episode "
                "ON watchers(project_id, node_id, experiment_episode_id, status)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS watchers_graph_conditions "
                "ON watchers(project_id, status, notified, graph_condition_json)"
            )
            connection.execute("DROP INDEX IF EXISTS graph_runs_active_project")
            connection.execute("DROP INDEX IF EXISTS agent_tasks_active_project")
            if issue_bootstrap:
                if stored_space_kind != "team" or initial_space_name is None:
                    raise ValueError("A bootstrap code requires a named team space.")
                if recovering_team_initialization:
                    if connection.execute("SELECT 1 FROM space_users LIMIT 1").fetchone():
                        raise ValueError("This RCP data directory already contains a space.")
                    connection.execute("DELETE FROM team_bootstrap_codes")
                bootstrap_code, code_id, code_hash = _new_enrollment_code("bootstrap")
                connection.execute(
                    """
                    INSERT INTO team_bootstrap_codes (code_id, code_hash, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (code_id, code_hash, self.now()),
                )
        return bootstrap_code

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        name: str,
        definition: str,
    ) -> None:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            try:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            except sqlite3.OperationalError:
                columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                if name not in columns:
                    raise

    @staticmethod
    def now() -> str:
        return datetime.now(UTC).isoformat()
