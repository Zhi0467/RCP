"""Application-owned copied-state migration, path fencing, and graph/read verification."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Protocol

import tomlkit
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator, model_validator

from rcp.api import create_app
from rcp.background import BackgroundAgentTasks, StartupEffectFence
from rcp.config import AGENT_EXECUTION_PROFILES, Manifest, load_manifest
from rcp.history import HistoryManager
from rcp.limits import (
    BACKUP_COPY_BUFFER_BYTES,
    BACKUP_DIAGNOSTIC_MAX_CHARS,
)
from rcp.projects import (
    TEAM_PROJECT_DELETE_CONFIRMATION,
    TEAM_PROJECT_DELETE_UNAVAILABLE_REASON,
)
from rcp.server_ops._local_primitives import (
    canonical_json_bytes,
    canonical_json_line,
)
from rcp.server_ops._local_primitives import (
    canonical_uuid4 as _canonical_uuid4,
)
from rcp.server_ops._local_primitives import (
    fsync_directory as _fsync_directory,
)
from rcp.server_ops._local_primitives import (
    fsync_file as _fsync_file,
)
from rcp.server_ops._local_primitives import (
    normalized_absolute_path as _absolute_path,
)
from rcp.server_ops.backup_capture import (
    BackupSQLiteCaptureReceipt,
)
from rcp.server_ops.backup_models import BackupManifestConfiguration, BackupProjectCapture
from rcp.server_ops.backup_project_files import (
    BackupProjectFileCaptureReceipt,
)
from rcp.server_ops.models import redact_server_text
from rcp.sources.imported import (
    ImportedProviderSourceInventory,
    ImportedProviderSourceStore,
)

REHEARSAL_OVERLAY_SCHEMA_VERSION = 1
CANDIDATE_REHEARSAL_RESULT_SCHEMA_VERSION = 1

_SHA256 = re.compile(r"[0-9a-f]{64}")
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_MAX_RECEIPT_BYTES = 4 * 1024 * 1024
_PATH_COLUMN_NAMES = frozenset(
    {
        "locator",
        "state_location",
        "stage_root",
        "output_path",
        "log_path",
        "cwd",
        "job_root",
        "exit_path",
    }
)


class CandidateRehearsalRefused(RuntimeError):
    """The candidate or copied-state boundary failed safely before cutover."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class StartupRecoveryReadModel(_StrictModel):
    active_operation_ids: tuple[str, ...]
    stopping_experiment_operation_ids: tuple[str, ...]
    report_episode_ids: tuple[str, ...]
    auto_research_recovery_operation_ids: tuple[str, ...]
    active_watcher_ids: tuple[str, ...]


class RehearsalProjectOverlay(_StrictModel):
    project_id: str
    name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    capture_status: Literal["captured", "remote_unreachable", "archive_uncaptured"]
    overlay_locator: str
    original_locator: str
    original_state_location: str
    original_remote: bool
    original_reachable: bool | None
    original_error_sha256: str | None
    expected_card_sha256: str
    expected_graph_sha256: str | None
    expected_revision: int | None = None

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        return _canonical_uuid4(value, label="project identity")

    @field_validator("overlay_locator", "original_locator")
    @classmethod
    def validate_path(cls, value: str, info) -> str:
        return _absolute_path(value, label=info.field_name.replace("_", " "))

    @field_validator(
        "original_error_sha256",
        "expected_card_sha256",
        "expected_graph_sha256",
    )
    @classmethod
    def validate_projection_digest(cls, value: str | None, info) -> str | None:
        if value is None:
            if info.field_name in {"original_error_sha256", "expected_graph_sha256"}:
                return None
            raise ValueError("project projection digest is required")
        if _SHA256.fullmatch(value) is None:
            raise ValueError("project projection digest must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def validate_capture(self) -> RehearsalProjectOverlay:
        if self.capture_status == "captured" and self.expected_revision is None:
            raise ValueError("captured rehearsal projects require an expected revision")
        if (self.capture_status == "captured") != (self.expected_graph_sha256 is not None):
            raise ValueError("only captured rehearsal projects require an expected graph digest")
        if self.capture_status == "remote_unreachable" and (
            not self.original_remote or self.original_reachable is not False
        ):
            raise ValueError(
                "an unavailable rehearsal project must already be a failed remote projection"
            )
        return self


class RehearsalOverlay(_StrictModel):
    schema_version: Literal[1] = REHEARSAL_OVERLAY_SCHEMA_VERSION
    root: str
    data_dir: str
    database_path: str
    capture_id: str
    sqlite_receipt_sha256: str
    sqlite_snapshot_sha256: str
    project_receipt_sha256: str
    space_id: str
    expected_startup_recovery: StartupRecoveryReadModel
    projects: tuple[RehearsalProjectOverlay, ...]
    transfer_inbox_entries: tuple[str, ...]

    @field_validator("root", "data_dir", "database_path")
    @classmethod
    def validate_path(cls, value: str, info) -> str:
        return _absolute_path(value, label=info.field_name.replace("_", " "))

    @field_validator("capture_id", "space_id")
    @classmethod
    def validate_capture_id(cls, value: str) -> str:
        return _canonical_uuid4(value, label="capture identity")

    @field_validator(
        "sqlite_receipt_sha256",
        "sqlite_snapshot_sha256",
        "project_receipt_sha256",
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("rehearsal capture digests must be lowercase SHA-256")
        return value

    @field_validator("transfer_inbox_entries")
    @classmethod
    def validate_transfer_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for path in value:
            _absolute_path(path, label="transfer inbox overlay path")
        if tuple(sorted(set(value))) != value:
            raise ValueError("transfer inbox overlay paths must be sorted and unique")
        return value

    @model_validator(mode="after")
    def validate_boundary(self) -> RehearsalOverlay:
        root = Path(self.root)
        if (
            Path(self.data_dir).parent != root
            or Path(self.database_path) != Path(self.data_dir) / "rcp.sqlite3"
            or any(
                not Path(project.overlay_locator).is_relative_to(root) for project in self.projects
            )
            or any(not Path(path).is_relative_to(root) for path in self.transfer_inbox_entries)
        ):
            raise ValueError("rehearsal overlay paths escaped their request root")
        project_ids = [project.project_id for project in self.projects]
        if tuple(sorted(project_ids)) != tuple(project_ids) or len(project_ids) != len(
            set(project_ids)
        ):
            raise ValueError("rehearsal projects must be sorted and unique")
        return self


class CandidateProjectVerification(_StrictModel):
    project_id: str
    status: Literal["verified", "not_replay_verified"]
    revision: int | None
    projection_sha256: str

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        return _canonical_uuid4(value, label="project identity")

    @field_validator("projection_sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("project projection digest must be lowercase SHA-256")
        return value


class CandidateRehearsalResult(_StrictModel):
    schema_version: Literal[1] = CANDIDATE_REHEARSAL_RESULT_SCHEMA_VERSION
    status: Literal["verified", "failed"]
    space_id: str | None = None
    space_kind: Literal["team"] | None = None
    startup_recovery: StartupRecoveryReadModel | None = None
    projects: tuple[CandidateProjectVerification, ...] = ()
    reads: tuple[str, ...] = ()
    attempted_effects: tuple[str, ...] = ()
    diagnostic: Annotated[str, StringConstraints(max_length=BACKUP_DIAGNOSTIC_MAX_CHARS)] | None = (
        None
    )

    @model_validator(mode="after")
    def validate_result(self) -> CandidateRehearsalResult:
        if self.status == "verified":
            if (
                self.space_id is None
                or self.space_kind != "team"
                or self.startup_recovery is None
                or self.diagnostic is not None
                or self.attempted_effects
            ):
                raise ValueError("verified rehearsal result is incomplete or crossed its fence")
        elif self.diagnostic is None:
            raise ValueError("failed rehearsal result requires a diagnostic")
        project_ids = [project.project_id for project in self.projects]
        if tuple(sorted(project_ids)) != tuple(project_ids) or len(project_ids) != len(
            set(project_ids)
        ):
            raise ValueError("candidate project results must be sorted and unique")
        return self


class CandidateDatabaseMigrator(Protocol):
    def __call__(self, database_path: Path) -> None: ...


def build_rehearsal_overlay(
    operation_root: Path,
    *,
    sqlite_receipt: BackupSQLiteCaptureReceipt,
    sqlite_receipt_sha256: str,
    project_receipt: BackupProjectFileCaptureReceipt,
    project_receipt_sha256: str,
    capture_root: Path,
    candidate_migrator: CandidateDatabaseMigrator | None = None,
) -> RehearsalOverlay:
    root = operation_root.resolve() / "overlay"
    data_dir = root / "data"
    projects_root = root / "projects"
    absent_root = root / "known-absent"
    for path in (root, data_dir, projects_root, absent_root):
        path.mkdir(mode=_DIRECTORY_MODE)
    database_path = data_dir / "rcp.sqlite3"
    _copy_verified_file(
        Path(sqlite_receipt.snapshot_path),
        database_path,
        expected_sha256=sqlite_receipt.sqlite_snapshot.sha256,
        expected_size=sqlite_receipt.sqlite_snapshot.size_bytes,
    )
    # The running release records its startup-recovery expectation on the
    # unmigrated copy. Its store refuses a ledger longer than its own, so it
    # cannot read the copy once the candidate has migrated it; the plan reads
    # only task, watcher, and episode rows, which the rebinding below and the
    # candidate migration must both leave unchanged.
    expected_startup_recovery = _expected_startup_recovery(database_path)
    if (
        project_receipt.capture_id != sqlite_receipt.capture_id
        or project_receipt.sqlite_receipt_sha256 != sqlite_receipt_sha256
        or project_receipt.sqlite_snapshot_sha256 != sqlite_receipt.sqlite_snapshot.sha256
    ):
        raise CandidateRehearsalRefused(
            "The project files and SQLite snapshot do not share one capture boundary."
        )

    for capture in project_receipt.imported_sources:
        if not capture.present:
            continue
        source_root = capture_root / "project-sources" / capture.project_id / "provider-history"
        expected = ImportedProviderSourceInventory.model_validate(capture.inventory.model_dump())
        try:
            published = ImportedProviderSourceStore(
                data_dir,
                capture.project_id,
            ).publish_snapshot(source_root, expected)
        except (OSError, ValueError) as exc:
            raise CandidateRehearsalRefused(
                "The imported provider-source snapshot failed rehearsal publication."
            ) from exc
        if published != expected:
            raise CandidateRehearsalRefused(
                "The imported provider-source rehearsal readback differs."
            )

    # The running release owns the coordinator, while the candidate gets only
    # this disposable migration phase. Run it before any schema/path inventory
    # so candidate-added path owners cannot appear after containment validation.
    if candidate_migrator is None:
        from rcp.storage import AppStore

        AppStore(database_path)
    else:
        candidate_migrator(database_path)

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT project_id, home_space_id, locator, name, state_location, "
            "state_remote, last_opened_at, revision, primary_question, attention_count, "
            "last_refresh_at, reachable, error FROM projects ORDER BY project_id"
        ).fetchall()
        captures = {project.project_id: project for project in project_receipt.projects}
        if set(captures) != {str(row["project_id"]) for row in rows}:
            raise CandidateRehearsalRefused(
                "The project capture does not inventory every copied database project."
            )
        projects: list[RehearsalProjectOverlay] = []
        connection.execute("BEGIN IMMEDIATE")
        for row in rows:
            project_id = str(row["project_id"])
            capture = captures[project_id]
            project_root = projects_root / project_id
            project_root.mkdir(mode=_DIRECTORY_MODE)
            project = _prepare_overlay_project(
                row,
                capture,
                project_root=project_root,
                capture_root=capture_root,
            )
            projects.append(project)
            state_location = (
                row["state_location"]
                if bool(row["state_remote"])
                else str(Path(project.overlay_locator).parent)
            )
            connection.execute(
                "UPDATE projects SET locator = ?, state_location = ? WHERE project_id = ?",
                (project.overlay_locator, state_location, project_id),
            )
        _rebind_local_stage_paths(connection, absent_root)
        transfer_paths = _transfer_inbox_overlay_paths(connection, data_dir)
        _validate_path_column_inventory(connection)
        connection.commit()
        _validate_rebound_paths(connection, root=root, projects=projects)
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    _fsync_file(database_path)
    _fsync_directory(data_dir)
    overlay = RehearsalOverlay(
        root=str(root),
        data_dir=str(data_dir),
        database_path=str(database_path),
        capture_id=sqlite_receipt.capture_id,
        sqlite_receipt_sha256=sqlite_receipt_sha256,
        sqlite_snapshot_sha256=sqlite_receipt.sqlite_snapshot.sha256,
        project_receipt_sha256=project_receipt_sha256,
        space_id=sqlite_receipt.space_id,
        expected_startup_recovery=expected_startup_recovery,
        projects=tuple(projects),
        transfer_inbox_entries=transfer_paths,
    )
    for path in overlay.transfer_inbox_entries:
        if Path(path).exists() or Path(path).is_symlink():
            raise CandidateRehearsalRefused(
                "A copied incoming transfer resolved to an existing rehearsal path."
            )
    return overlay


def _prepare_overlay_project(
    row: sqlite3.Row,
    capture: BackupProjectCapture,
    *,
    project_root: Path,
    capture_root: Path,
) -> RehearsalProjectOverlay:
    if (
        capture.status == "uncaptured"
        and row["reachable"] == 0
        and str(row["error"]).startswith("Not captured by the replacement archive: ")
    ):
        return _prepare_uncaptured_archive_overlay(row, project_root)
    if capture.recovery is None:
        raise CandidateRehearsalRefused(
            "A copied project has no typed configuration for path-safe rehearsal."
        )
    if capture.status == "uncaptured":
        if not (
            capture.unavailable_kind == "remote_unreachable"
            and bool(row["state_remote"])
            and row["reachable"] == 0
            and row["error"]
            and _configuration_state_is_remote(capture.recovery.configuration)
        ):
            raise CandidateRehearsalRefused(
                "A project capture failed without an already-unreachable SSH proof."
            )
        capture_status: Literal["captured", "remote_unreachable", "archive_uncaptured"] = (
            "remote_unreachable"
        )
        expected_revision = None
        expected_graph_sha256 = None
    else:
        capture_status = "captured"
        if capture.main_head is None:
            raise CandidateRehearsalRefused("A captured project lost its canonical head.")
        expected_revision = capture.main_head.revision

    configuration = capture.recovery.configuration
    repository_roots = {
        repository.alias: project_root / "repositories" / repository.alias
        for repository in configuration.repositories
    }
    for repository_root in repository_roots.values():
        _mkdir_private_parents(repository_root)
    state_root = repository_roots[configuration.state_repository]
    if capture.status == "captured":
        for entry in capture.files:
            source = capture_root.joinpath(*PurePosixPath(entry.archive_path).parts)
            destination = state_root.joinpath(*PurePosixPath(entry.source_relative_path).parts)
            if entry.source_relative_path == ".research/manifest.toml":
                _read_verified_bytes(
                    source,
                    expected_sha256=entry.sha256,
                    expected_size=entry.size_bytes,
                )
            else:
                _copy_verified_file(
                    source,
                    destination,
                    expected_sha256=entry.sha256,
                    expected_size=entry.size_bytes,
                )
    manifest_path = state_root / ".research" / "manifest.toml"
    _mkdir_private_parents(manifest_path.parent)
    _write_private_bytes(
        manifest_path,
        _render_overlay_manifest(configuration, repository_roots, project_root).encode("utf-8"),
    )
    if capture.status == "captured":
        expected_graph = HistoryManager(
            load_manifest(manifest_path),
            expected_space_id=str(row["home_space_id"]),
        ).state()
        if expected_graph.revision != expected_revision:
            raise CandidateRehearsalRefused(
                "The current release could not replay the captured canonical head."
            )
        expected_graph_sha256 = _canonical_sha256(expected_graph.model_dump(mode="json"))
    expected_card = {
        "id": str(row["project_id"]),
        "home_space_id": row["home_space_id"],
        "name": str(row["name"]),
        "locator": str(row["locator"]),
        "state_location": str(row["state_location"]),
        "remote": bool(row["state_remote"]),
        "last_opened_at": row["last_opened_at"],
        "revision": row["revision"],
        "primary_question": row["primary_question"],
        "attention_count": row["attention_count"],
        "last_refresh_at": row["last_refresh_at"],
        "reachable": None if row["reachable"] is None else bool(row["reachable"]),
        "error_sha256": _optional_text_sha256(row["error"]),
        "can_delete": True,
        "delete_unavailable_reason": None,
        "delete_confirmation": TEAM_PROJECT_DELETE_CONFIRMATION,
    }
    return RehearsalProjectOverlay(
        project_id=str(row["project_id"]),
        name=str(row["name"]),
        capture_status=capture_status,
        overlay_locator=str(manifest_path),
        original_locator=str(row["locator"]),
        original_state_location=str(row["state_location"]),
        original_remote=bool(row["state_remote"]),
        original_reachable=(None if row["reachable"] is None else bool(row["reachable"])),
        original_error_sha256=_optional_text_sha256(row["error"]),
        expected_card_sha256=_canonical_sha256(expected_card),
        expected_graph_sha256=expected_graph_sha256,
        expected_revision=expected_revision,
    )


def _prepare_uncaptured_archive_overlay(row: sqlite3.Row, root: Path) -> RehearsalProjectOverlay:
    """Preserve the explicit protected-archive omission without opening any locator."""
    locator = root / "known-absent" / "manifest.toml"
    card = {
        "id": str(row["project_id"]),
        "home_space_id": row["home_space_id"],
        "name": str(row["name"]),
        "locator": str(row["locator"]),
        "state_location": str(row["state_location"]),
        "remote": bool(row["state_remote"]),
        "last_opened_at": row["last_opened_at"],
        "revision": row["revision"],
        "primary_question": row["primary_question"],
        "attention_count": row["attention_count"],
        "last_refresh_at": row["last_refresh_at"],
        "reachable": False,
        "error_sha256": _optional_text_sha256(row["error"]),
        "can_delete": True,
        "delete_unavailable_reason": None,
        "delete_confirmation": TEAM_PROJECT_DELETE_CONFIRMATION,
    }
    return RehearsalProjectOverlay(
        project_id=str(row["project_id"]),
        name=str(row["name"]),
        capture_status="archive_uncaptured",
        overlay_locator=str(locator),
        original_locator=str(row["locator"]),
        original_state_location=str(row["state_location"]),
        original_remote=bool(row["state_remote"]),
        original_reachable=False,
        original_error_sha256=_optional_text_sha256(row["error"]),
        expected_card_sha256=_canonical_sha256(card),
        expected_graph_sha256=None,
    )


def _expected_startup_recovery(database_path: Path) -> StartupRecoveryReadModel:
    from rcp.storage import AppStore

    tasks = BackgroundAgentTasks(
        AppStore(database_path),
        _unused_rehearsal_stream,
        startup_effect_fence=StartupEffectFence("current-release rehearsal expectation"),
    )
    try:
        return StartupRecoveryReadModel.model_validate(tasks.plan_startup_recovery().as_dict())
    finally:
        tasks.shutdown()


def _configuration_state_is_remote(configuration: BackupManifestConfiguration) -> bool:
    repositories = {repository.alias: repository for repository in configuration.repositories}
    machines = {machine.alias: machine for machine in configuration.machines}
    repository = repositories.get(configuration.state_repository)
    if repository is None:
        return False
    machine = machines.get(repository.machine)
    return bool(machine is not None and machine.host)


def _render_overlay_manifest(
    configuration: BackupManifestConfiguration,
    repository_roots: Mapping[str, Path],
    project_root: Path,
) -> str:
    document = tomlkit.document()
    document.add("name", configuration.name)
    machines = tomlkit.aot()
    for item in configuration.machines:
        machine = tomlkit.table()
        machine.add("alias", item.alias)
        machine.add("host", "")
        machine.add("os_account", "")
        machines.append(machine)
    document.add("machines", machines)
    if configuration.compute_connections:
        connections = tomlkit.aot()
        for item in configuration.compute_connections:
            connection = tomlkit.table()
            connection.add("id", item.id)
            connection.add("name", item.name)
            connection.add("kind", item.kind)
            if item.ssh_target:
                connection.add("ssh_target", item.ssh_target)
            if item.access_hint:
                connection.add("access_hint", item.access_hint)
            connections.append(connection)
        document.add("compute_connections", connections)
    repositories = tomlkit.aot()
    for item in configuration.repositories:
        repository = tomlkit.table()
        repository.add("alias", item.alias)
        repository.add("machine", item.machine)
        repository.add("path", str(repository_roots[item.alias]))
        repositories.append(repository)
    document.add("repositories", repositories)
    project = tomlkit.table()
    project.add("truth_scope", list(configuration.project_truth_scope))
    document.add("project", project)
    state = tomlkit.table()
    state.add("repository", configuration.state_repository)
    document.add("state", state)
    agent = tomlkit.table()
    agent.add("default_run_truth_scope", list(configuration.default_run_truth_scope))
    agent.add(
        "default_auto_research_invocation_ceiling",
        configuration.default_auto_research_invocation_ceiling,
    )
    defaults = tomlkit.table()
    defaults.add("workflow_ids", list(configuration.skill_defaults.workflow_ids))
    defaults.add("skill_ids", list(configuration.skill_defaults.skill_ids))
    agent.add("skill_defaults", defaults)
    profiles = {profile.profile: profile for profile in configuration.agent_profiles}
    for surface in AGENT_EXECUTION_PROFILES:
        item = profiles[surface]
        profile = tomlkit.table()
        profile.add("provider", item.provider)
        profile.add("runtime", item.runtime)
        profile.add("model", item.model)
        profile.add("reasoning", item.reasoning)
        profile.add("run_on", item.run_on)
        permissions = tomlkit.table()
        for key, value in item.permissions.model_dump(mode="json").items():
            permissions.add(key, value)
        profile.add("permissions", permissions)
        agent.add(surface, profile)
    document.add("agent", agent)
    absent = project_root / "known-absent-provider-history"
    sources = tomlkit.table()
    for name in (
        "claude_roots",
        "codex_roots",
        "remote_claude_roots",
        "remote_codex_roots",
    ):
        sources.add(name, [str(absent / name)])
    document.add("sources", sources)
    content = tomlkit.dumps(document)
    Manifest.model_validate(tomlkit.parse(content).unwrap())
    return content


def _rebind_local_stage_paths(connection: sqlite3.Connection, absent_root: Path) -> None:
    tables = _schema_columns(connection)
    for table, columns in tables.items():
        if "stage_root" not in columns:
            continue
        if "stage_host" not in columns:
            raise CandidateRehearsalRefused(
                f"Path-bearing table {table!r} has no stage-host boundary."
            )
        rows = connection.execute(
            f'SELECT rowid, stage_host, stage_root FROM "{table}" '
            "WHERE stage_root IS NOT NULL AND stage_root != ''"
        ).fetchall()
        for row in rows:
            if row["stage_host"]:
                continue
            rebound = absent_root / "stages" / table / str(row["rowid"])
            connection.execute(
                f'UPDATE "{table}" SET stage_root = ? WHERE rowid = ?',
                (str(rebound), row["rowid"]),
            )
            if "output_path" in columns:
                connection.execute(
                    f'UPDATE "{table}" SET output_path = ? '
                    "WHERE rowid = ? AND output_path IS NOT NULL",
                    (str(rebound / "output"), row["rowid"]),
                )
    if "watchers" in tables:
        rows = connection.execute(
            "SELECT rowid, execution_host FROM watchers WHERE job_id IS NULL"
        ).fetchall()
        for row in rows:
            if row["execution_host"]:
                continue
            rebound = absent_root / "watchers" / str(row["rowid"])
            connection.execute(
                "UPDATE watchers SET log_path = ?, cwd = ? WHERE rowid = ?",
                (str(rebound / "log"), str(rebound / "cwd"), row["rowid"]),
            )
    if "compute_jobs" in tables:
        rows = connection.execute("SELECT rowid, execution_host FROM compute_jobs").fetchall()
        for row in rows:
            if row["execution_host"]:
                continue
            rebound = absent_root / "compute_jobs" / str(row["rowid"])
            connection.execute(
                "UPDATE compute_jobs SET job_root = ?, cwd = ?, log_path = ?, exit_path = ? "
                "WHERE rowid = ?",
                (
                    str(rebound),
                    str(rebound / "cwd"),
                    str(rebound / "log"),
                    str(rebound / "exit"),
                    row["rowid"],
                ),
            )


def _transfer_inbox_overlay_paths(
    connection: sqlite3.Connection,
    data_dir: Path,
) -> tuple[str, ...]:
    if "project_provisioning_requests" not in _schema_columns(connection):
        return ()
    request_ids = [
        str(row[0])
        for row in connection.execute(
            "SELECT request_id FROM project_provisioning_requests "
            "WHERE kind = 'incoming_transfer' ORDER BY request_id"
        ).fetchall()
    ]
    return tuple(str(data_dir / "transfer-inbox" / request_id) for request_id in request_ids)


def _validate_path_column_inventory(connection: sqlite3.Connection) -> None:
    for table, columns in _schema_columns(connection).items():
        unexpected = {
            column
            for column in columns
            if (
                column in _PATH_COLUMN_NAMES or column.endswith("_path") or column.endswith("_root")
            )
            and column not in _PATH_COLUMN_NAMES
        }
        if unexpected:
            raise CandidateRehearsalRefused(
                f"Copied table {table!r} has unclassified path columns: {sorted(unexpected)}."
            )
        for column in columns.intersection(_PATH_COLUMN_NAMES):
            if column in {"locator", "state_location"} and table != "projects":
                raise CandidateRehearsalRefused(
                    f"Copied table {table!r} unexpectedly owns {column!r}."
                )
            if column in {"log_path", "cwd"} and table not in {"watchers", "compute_jobs"}:
                raise CandidateRehearsalRefused(
                    f"Copied table {table!r} unexpectedly owns {column!r}."
                )
            if column in {"job_root", "exit_path"} and table != "compute_jobs":
                raise CandidateRehearsalRefused(
                    f"Copied table {table!r} unexpectedly owns {column!r}."
                )
            if column == "output_path" and "stage_root" not in columns:
                raise CandidateRehearsalRefused(
                    f"Copied table {table!r} has output paths without a stage boundary."
                )


def _validate_rebound_paths(
    connection: sqlite3.Connection,
    *,
    root: Path,
    projects: list[RehearsalProjectOverlay],
) -> None:
    expected = {project.project_id: project for project in projects}
    for row in connection.execute(
        "SELECT project_id, locator, state_location, state_remote FROM projects"
    ).fetchall():
        project = expected[str(row["project_id"])]
        if row["locator"] != project.overlay_locator or not Path(row["locator"]).is_relative_to(
            root
        ):
            raise CandidateRehearsalRefused("A copied project locator escaped its overlay.")
        if not bool(row["state_remote"]):
            state = Path(str(row["state_location"]))
            if not state.is_relative_to(root):
                raise CandidateRehearsalRefused(
                    "A copied local canonical-state pointer escaped its overlay."
                )
    for table, columns in _schema_columns(connection).items():
        if "stage_root" in columns:
            for row in connection.execute(
                f'SELECT stage_host, stage_root FROM "{table}" '
                "WHERE stage_root IS NOT NULL AND stage_root != ''"
            ).fetchall():
                if not row["stage_host"] and not Path(str(row["stage_root"])).is_relative_to(root):
                    raise CandidateRehearsalRefused(
                        f"A copied local stage in {table!r} escaped its overlay."
                    )
                if not row["stage_host"] and (
                    Path(str(row["stage_root"])).exists()
                    or Path(str(row["stage_root"])).is_symlink()
                ):
                    raise CandidateRehearsalRefused(
                        f"A copied local stage in {table!r} is not known-absent."
                    )
        if table == "watchers":
            for row in connection.execute(
                "SELECT execution_host, log_path, cwd FROM watchers WHERE job_id IS NULL"
            ).fetchall():
                if not row["execution_host"] and any(
                    not Path(str(row[name])).is_relative_to(root) for name in ("log_path", "cwd")
                ):
                    raise CandidateRehearsalRefused(
                        "A copied local watcher path escaped its overlay."
                    )
        if table == "compute_jobs":
            for row in connection.execute(
                "SELECT execution_host, job_root, cwd, log_path, exit_path FROM compute_jobs"
            ).fetchall():
                if not row["execution_host"] and any(
                    not Path(str(row[name])).is_relative_to(root)
                    for name in ("job_root", "cwd", "log_path", "exit_path")
                ):
                    raise CandidateRehearsalRefused(
                        "A copied local compute job path escaped its overlay."
                    )


def _schema_columns(connection: sqlite3.Connection) -> dict[str, set[str]]:
    tables = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    if any('"' in table or "\x00" in table for table in tables):
        raise CandidateRehearsalRefused("The copied database has an unsafe table name.")
    return {
        table: {
            str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        for table in tables
    }


def run_candidate_child(overlay_path: Path, result_path: Path) -> int:
    overlay: RehearsalOverlay | None = None
    fence = StartupEffectFence("candidate update rehearsal")
    lock_context = None
    lock_acquired = False
    try:
        overlay = RehearsalOverlay.model_validate_json(_read_bounded_file(overlay_path))
        if overlay_path.parent != Path(overlay.root).parent:
            raise CandidateRehearsalRefused(
                "The candidate overlay manifest is outside its rehearsal operation."
            )
        from rcp.__main__ import instance_lock
        from rcp.storage import AppStore

        lock_context = instance_lock(Path(overlay.data_dir), timeout=0.0)
        lock_context.__enter__()
        lock_acquired = True
        opened = AppStore(Path(overlay.database_path))
        users = opened.space_users()
        if opened.space_kind != "team":
            raise CandidateRehearsalRefused("The copied database is not one team space.")
        users_by_id = {user.user_id: user for user in users if user.identity_kind == "team_member"}
        project_principals: dict[str, str] = {}
        for project in overlay.projects:
            principal = next(
                (
                    membership.user_id
                    for membership in opened.project_members(project.project_id)
                    if membership.user_id in users_by_id
                ),
                None,
            )
            if principal is None:
                raise CandidateRehearsalRefused(
                    f"Copied project {project.project_id} has no enrolled member."
                )
            project_principals[project.project_id] = principal
        default_user_id = next(iter(users_by_id), None)

        def rehearsal_principal(request, store):
            requested = request.headers.get("x-rcp-rehearsal-user", default_user_id)
            if requested is None or requested not in users_by_id:
                return None
            return store.space_user(requested)

        app = create_app(
            data_dir=Path(overlay.data_dir),
            trusted_principal_resolver=rehearsal_principal,
            startup_effect_fence=fence,
        )
        # Keep the test-client compatibility layer inside the captured candidate
        # child. Importing this module is part of the operator-facing update
        # coordinator, where dependency warnings would corrupt the rotating CLI
        # status line.
        from fastapi.testclient import TestClient

        reads: list[str] = []
        results: list[CandidateProjectVerification] = []
        with TestClient(app) as client:
            health = client.get("/api/health")
            if health.status_code != 200:
                raise CandidateRehearsalRefused("Candidate health read failed.")
            health_payload = health.json()
            if (
                health_payload.get("space_id") != opened.space_id
                or health_payload.get("space_kind") != "team"
            ):
                raise CandidateRehearsalRefused("Candidate health changed copied space identity.")
            reads.append("/api/health")
            cards: dict[str, dict[str, object]] = {}
            if users_by_id:
                for principal in sorted(users_by_id):
                    listing = client.get(
                        "/api/projects",
                        headers={"x-rcp-rehearsal-user": principal},
                    )
                    if listing.status_code != 200:
                        raise CandidateRehearsalRefused("Candidate project inventory read failed.")
                    for raw_card in listing.json():
                        card = dict(raw_card)
                        project_id = str(card["id"])
                        existing = cards.get(project_id)
                        if existing is not None and existing != card:
                            raise CandidateRehearsalRefused(
                                "Candidate project inventory changed between member reads."
                            )
                        cards[project_id] = card
            else:
                listing = client.get("/api/projects")
                if listing.status_code != 403:
                    raise CandidateRehearsalRefused(
                        "Candidate pre-enrollment project inventory did not stay protected."
                    )
            if set(cards) != {project.project_id for project in overlay.projects}:
                raise CandidateRehearsalRefused(
                    "Candidate project inventory omitted or substituted a project."
                )
            reads.append("/api/projects")
            for project in overlay.projects:
                headers = {"x-rcp-rehearsal-user": project_principals[project.project_id]}
                if project.capture_status == "captured":
                    response = client.get(
                        f"/api/projects/{project.project_id}",
                        headers=headers,
                    )
                    if response.status_code != 200:
                        raise CandidateRehearsalRefused(
                            f"Candidate replay failed for project {project.project_id}."
                        )
                    payload = response.json()
                    graph = payload.get("graph")
                    revision = graph.get("revision") if isinstance(graph, dict) else None
                    if (
                        payload.get("id") != project.project_id
                        or revision != project.expected_revision
                    ):
                        raise CandidateRehearsalRefused(
                            f"Candidate replay changed project {project.project_id}."
                        )
                    tasks = client.get(
                        f"/api/projects/{project.project_id}/tasks",
                        headers=headers,
                    )
                    watchers = client.get(
                        f"/api/projects/{project.project_id}/watchers",
                        headers=headers,
                    )
                    if tasks.status_code != 200 or watchers.status_code != 200:
                        raise CandidateRehearsalRefused(
                            f"Candidate operational reads failed for project {project.project_id}."
                        )
                    reads.extend(
                        (
                            f"/api/projects/{project.project_id}",
                            f"/api/projects/{project.project_id}/tasks",
                            f"/api/projects/{project.project_id}/watchers",
                        )
                    )
                    results.append(
                        CandidateProjectVerification(
                            project_id=project.project_id,
                            status="verified",
                            revision=revision,
                            projection_sha256=_canonical_sha256(graph),
                        )
                    )
                else:
                    # Compare durable identities, not this rehearsal's copied paths.
                    card = dict(cards[project.project_id])
                    card.update(
                        locator=project.original_locator,
                        state_location=project.original_state_location,
                    )
                    projection_sha256 = unavailable_card_projection_sha256(
                        card,
                        project.expected_card_sha256,
                        team_space=opened.space_kind == "team",
                    )
                    if projection_sha256 != project.expected_card_sha256:
                        raise CandidateRehearsalRefused(
                            f"Candidate changed unavailable projection {project.project_id}."
                        )
                    results.append(
                        CandidateProjectVerification(
                            project_id=project.project_id,
                            status="not_replay_verified",
                            revision=None,
                            projection_sha256=projection_sha256,
                        )
                    )
        if fence.attempted_effects:
            raise CandidateRehearsalRefused(
                "Candidate startup attempted an effect while its fence was closed."
            )
        startup = app.state.startup_recovery_plan
        result = CandidateRehearsalResult(
            status="verified",
            space_id=opened.space_id,
            space_kind="team",
            startup_recovery=StartupRecoveryReadModel.model_validate(startup),
            projects=tuple(sorted(results, key=lambda item: item.project_id)),
            reads=tuple(reads),
            attempted_effects=fence.attempted_effects,
        )
        _write_private_json(result_path, result)
        return 0
    except BaseException as exc:
        diagnostic = redact_server_text(str(exc)).strip()
        if not diagnostic or len(diagnostic) > BACKUP_DIAGNOSTIC_MAX_CHARS:
            diagnostic = "Candidate copied-state verification failed."
        failed = CandidateRehearsalResult(
            status="failed",
            attempted_effects=fence.attempted_effects,
            diagnostic=diagnostic,
        )
        with suppress(OSError, ValueError):
            _write_private_json(result_path, failed)
        return 1
    finally:
        if lock_context is not None and lock_acquired:
            lock_context.__exit__(None, None, None)


def _copy_verified_file(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> None:
    _mkdir_private_parents(destination.parent)
    source_descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    destination_descriptor = -1
    try:
        initial = os.fstat(source_descriptor)
        path_initial = source.lstat()
        if (
            not stat.S_ISREG(initial.st_mode)
            or (initial.st_dev, initial.st_ino) != (path_initial.st_dev, path_initial.st_ino)
            or initial.st_size != expected_size
        ):
            raise CandidateRehearsalRefused(
                "A captured rehearsal file has unsafe type, identity, or size."
            )
        destination_descriptor = os.open(
            destination,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            _FILE_MODE,
        )
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(source_descriptor, BACKUP_COPY_BUFFER_BYTES)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:
                    raise OSError("short rehearsal file write")
                view = view[written:]
            digest.update(chunk)
            size += len(chunk)
        final = os.fstat(source_descriptor)
        path_final = source.lstat()
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (
            any(getattr(initial, field) != getattr(final, field) for field in stable_fields)
            or any(getattr(final, field) != getattr(path_final, field) for field in stable_fields)
            or size != expected_size
            or digest.hexdigest() != expected_sha256
        ):
            raise CandidateRehearsalRefused(
                "A captured rehearsal file changed or was copied incompletely."
            )
        os.fchmod(destination_descriptor, _FILE_MODE)
        os.fsync(destination_descriptor)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
    _fsync_directory(destination.parent)


def _read_verified_bytes(
    source: Path,
    *,
    expected_sha256: str,
    expected_size: int,
) -> bytes:
    try:
        info = source.lstat()
    except OSError as exc:
        raise CandidateRehearsalRefused("A captured rehearsal file is unavailable.") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
        raise CandidateRehearsalRefused("A captured rehearsal file has unsafe type or size.")
    data = source.read_bytes()
    if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_sha256:
        raise CandidateRehearsalRefused("A captured rehearsal file changed after capture.")
    return data


def _mkdir_private_parents(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=_DIRECTORY_MODE)
    for directory in (path, *path.parents):
        if directory == current.parent:
            break
        if directory.exists() and directory.is_relative_to(current):
            os.chmod(directory, _DIRECTORY_MODE)


def _write_private_json(path: Path, model: BaseModel) -> None:
    _write_private_bytes(path, _model_bytes(model))


def _write_private_bytes(path: Path, content: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        _FILE_MODE,
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short rehearsal write")
            view = view[written:]
        os.fchmod(descriptor, _FILE_MODE)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _model_bytes(model: BaseModel) -> bytes:
    content = canonical_json_line(model.model_dump(mode="json"))
    if len(content) > _MAX_RECEIPT_BYTES:
        raise CandidateRehearsalRefused("A rehearsal receipt exceeds its fixed size bound.")
    return content


def _read_bounded_file(path: Path) -> bytes:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_RECEIPT_BYTES:
            raise ValueError("unsafe file")
        content = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise CandidateRehearsalRefused("A rehearsal handoff file is unavailable.") from exc
    if len(content) > _MAX_RECEIPT_BYTES:
        raise CandidateRehearsalRefused("A rehearsal handoff file is oversized.")
    return content


def _optional_text_sha256(value: object) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def unavailable_card_projection_sha256(
    card: Mapping[str, object],
    expected_sha256: str,
    *,
    team_space: bool,
) -> str:
    """Hash an uncaptured project's card, accepting a predecessor's retired team shape.

    A release before team deletion hashed team cards as non-deletable and without
    `delete_confirmation`. When the current shape does not match the expectation
    and the space is a team, the retired shape is tried; if it matches, its digest
    is returned so both the candidate rehearsal and the live cutover readback agree
    with the predecessor. Any other mismatch returns the current digest, which the
    caller refuses. Retire this adapter once no server runs a pre-team-deletion
    release.
    """

    comparison = _project_card_comparison(card)
    digest = _canonical_sha256(comparison)
    if digest == expected_sha256 or not team_space:
        return digest
    legacy_comparison = dict(comparison)
    legacy_comparison.pop("delete_confirmation", None)
    legacy_comparison.update(
        can_delete=False,
        delete_unavailable_reason=TEAM_PROJECT_DELETE_UNAVAILABLE_REASON,
    )
    legacy_digest = _canonical_sha256(legacy_comparison)
    return legacy_digest if legacy_digest == expected_sha256 else digest


def _project_card_comparison(card: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": card.get("id"),
        "home_space_id": card.get("home_space_id"),
        "name": card.get("name"),
        "locator": card.get("locator"),
        "state_location": card.get("state_location"),
        "remote": card.get("remote"),
        "last_opened_at": card.get("last_opened_at"),
        "revision": card.get("revision"),
        "primary_question": card.get("primary_question"),
        "attention_count": card.get("attention_count"),
        "last_refresh_at": card.get("last_refresh_at"),
        "reachable": card.get("reachable"),
        "error_sha256": _optional_text_sha256(card.get("error")),
        "can_delete": card.get("can_delete"),
        "delete_unavailable_reason": card.get("delete_unavailable_reason"),
        "delete_confirmation": card.get("delete_confirmation"),
    }


def _expected_candidate_reads(
    projects: tuple[RehearsalProjectOverlay, ...],
) -> tuple[str, ...]:
    reads = ["/api/health", "/api/projects"]
    for project in projects:
        if project.capture_status == "captured":
            reads.extend(
                (
                    f"/api/projects/{project.project_id}",
                    f"/api/projects/{project.project_id}/tasks",
                    f"/api/projects/{project.project_id}/watchers",
                )
            )
    return tuple(reads)


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


async def _unused_rehearsal_stream(*_args, **_kwargs):
    if False:  # pragma: no cover - type-correct effect tripwire
        yield object()
