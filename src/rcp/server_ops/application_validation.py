"""Application-owned copied-state migration, path fencing, and graph/read verification."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sqlite3
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, cast, get_args

import httpx
import tomlkit
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator, model_validator

from rcp.api import create_app
from rcp.api.resource_watermarks import ResourceName
from rcp.background import AdmittedLaunch, BackgroundAgentTasks, StartupEffectFence
from rcp.config import AGENT_EXECUTION_PROFILES, Manifest, load_manifest
from rcp.consolidation import ConsolidationPoller
from rcp.core.authority import WriteAuthority
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
from rcp.server_ops.application_snapshot import copy_live_research_tree
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

if TYPE_CHECKING:
    from fastapi import FastAPI

    from rcp.storage import AgentTaskRecord, AppStore

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
        "workspace_dir",
        "output_path",
        "log_path",
        "cwd",
        "job_root",
        "exit_path",
        # A machine card's jobs directory lives on that machine, like its writable
        # paths; a rehearsal copies it unchanged, as it did from the manifest.
        "jobs_root",
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
    excluded_project_ids: tuple[str, ...] = ()
    # Whether the live display caches were carried; a restored archive has none.
    display_caches: bool = False

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
    display_cache_root: Path | None = None,
    excluded_project_ids: tuple[str, ...] = (),
) -> RehearsalOverlay:
    root = operation_root.resolve() / "overlay"
    data_dir = root / "data"
    projects_root = root / "projects"
    absent_root = root / "known-absent"
    for path in (root, data_dir, projects_root, absent_root):
        path.mkdir(mode=_DIRECTORY_MODE)
    if display_cache_root is not None:
        # Cached display bytes an older release wrote, read as the live server reads them.
        copy_live_research_tree(display_cache_root, data_dir / "project-snapshots")
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

        AppStore(database_path).close()
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
            if project_id in excluded_project_ids:
                # Keep operational rows for the startup proof, but make every
                # omitted project's file access resolve to an absent private path.
                absent = absent_root / hashlib.sha256(project_id.encode()).hexdigest()
                connection.execute(
                    "UPDATE projects SET locator=?, state_location=? WHERE project_id=?",
                    (str(absent / "manifest.toml"), str(absent), project_id),
                )
                continue
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
        _validate_rebound_paths(
            connection, root=root, projects=projects, excluded_project_ids=excluded_project_ids
        )
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
        excluded_project_ids=excluded_project_ids,
        display_caches=display_cache_root is not None,
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
    for repository in configuration.repositories:
        # An alias names a folder inside the disposable overlay.
        if (
            repository.alias in {"", ".", ".."}
            or "/" in repository.alias
            or "\x00" in repository.alias
        ):
            raise CandidateRehearsalRefused("A repository alias is not a plain folder name.")
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
        history = HistoryManager(
            load_manifest(manifest_path),
            expected_space_id=str(row["home_space_id"]),
            write_authority=WriteAuthority(),
        )
        expected_graph = history.state()
        if expected_graph.revision != expected_revision:
            raise CandidateRehearsalRefused(
                "The current release could not replay the captured canonical head."
            )
        # Branch history is canonical too; protected restore proves it the same way.
        for head in capture.branch_heads:
            branch = history.branch(
                str(head.target.branch_id), expected_project_id=history.project_id
            )
            if branch.head_ref(branch.initialize()) != head:
                raise CandidateRehearsalRefused(
                    "The current release could not replay a captured branch head."
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
        try:
            tasks.shutdown()
        finally:
            tasks.store.close()


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
    if configuration.skill_defaults_configuration:
        defaults = tomlkit.table()
        for name, value in configuration.skill_defaults_configuration.items():
            defaults.add(name, value)
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
        host_column = "execution_host" if table == "browser_owners" else "stage_host"
        if host_column not in columns:
            raise CandidateRehearsalRefused(
                f"Path-bearing table {table!r} has no stage-host boundary."
            )
        rows = connection.execute(
            f'SELECT rowid, {host_column} AS stage_host, stage_root FROM "{table}" '
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
            if table == "browser_owners":
                connection.execute(
                    "UPDATE browser_owners SET workspace_dir = ? WHERE rowid = ?",
                    (str(rebound / "workspace"), row["rowid"]),
                )
            if "output_path" in columns:
                connection.execute(
                    f'UPDATE "{table}" SET output_path = ? '
                    "WHERE rowid = ? AND output_path IS NOT NULL",
                    (str(rebound / "output"), row["rowid"]),
                )
    if "watchers" in tables:
        rows = connection.execute("SELECT rowid, execution_host FROM watchers").fetchall()
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
            if column == "jobs_root" and table != "space_machines":
                raise CandidateRehearsalRefused(
                    f"Copied table {table!r} unexpectedly owns {column!r}."
                )
            if column == "workspace_dir" and table != "browser_owners":
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
    excluded_project_ids: tuple[str, ...] = (),
) -> None:
    expected = {project.project_id: project for project in projects}
    for row in connection.execute(
        "SELECT project_id, locator, state_location, state_remote FROM projects"
    ).fetchall():
        if str(row["project_id"]) in excluded_project_ids:
            if any(
                not Path(row[key]).is_relative_to(root / "known-absent") or Path(row[key]).exists()
                for key in ("locator", "state_location")
            ):
                raise CandidateRehearsalRefused("An excluded project escaped its absent boundary.")
            continue
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
            host_column = "execution_host" if table == "browser_owners" else "stage_host"
            for row in connection.execute(
                f'SELECT {host_column} AS stage_host, stage_root FROM "{table}" '
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
        if table == "browser_owners":
            for row in connection.execute(
                "SELECT execution_host, workspace_dir FROM browser_owners"
            ).fetchall():
                if not row["execution_host"] and (
                    not Path(row["workspace_dir"]).is_relative_to(root)
                    or Path(row["workspace_dir"]).exists()
                    or Path(row["workspace_dir"]).is_symlink()
                ):
                    raise CandidateRehearsalRefused(
                        "A copied local browser workspace escaped its known-absent boundary."
                    )
        if table == "watchers":
            for row in connection.execute(
                "SELECT execution_host, log_path, cwd FROM watchers"
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
    return asyncio.run(_run_candidate_child(overlay_path, result_path))


async def _run_candidate_child(overlay_path: Path, result_path: Path) -> int:
    overlay: RehearsalOverlay | None = None
    fence = StartupEffectFence("candidate update rehearsal")
    lock_context = None
    lock_acquired = False
    opened = None
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
        result = await _verify_candidate(overlay, opened, fence)
        _write_private_json(result_path, result)
        return 0
    except BaseException as exc:
        failed = _failed_candidate_result(exc, fence)
        with suppress(OSError, ValueError):
            _write_private_json(result_path, failed)
        return 1
    finally:
        if opened is not None:
            opened.close()
        if lock_context is not None and lock_acquired:
            lock_context.__exit__(None, None, None)


def _failed_candidate_result(
    exc: BaseException, fence: StartupEffectFence
) -> CandidateRehearsalResult:
    diagnostic = redact_server_text(str(exc)).strip()
    if not diagnostic or len(diagnostic) > BACKUP_DIAGNOSTIC_MAX_CHARS:
        diagnostic = "Candidate copied-state verification failed."
    return CandidateRehearsalResult(
        status="failed",
        attempted_effects=fence.attempted_effects,
        diagnostic=diagnostic,
    )


async def _verify_candidate(
    overlay: RehearsalOverlay, opened: AppStore, fence: StartupEffectFence
) -> CandidateRehearsalResult:
    users = opened.space_users()
    if opened.space_kind != "team":
        raise CandidateRehearsalRefused("The copied database is not one team space.")
    users_by_id = {user.user_id: user for user in users if user.identity_kind == "team_member"}
    project_principals = _project_principals(overlay, opened, users_by_id)
    default_user_id = next(iter(users_by_id), None)

    def rehearsal_principal(request, store):
        requested = request.headers.get("x-rcp-rehearsal-user", default_user_id)
        if requested is None or requested not in users_by_id:
            return None
        return store.space_user(requested)

    with _phase_log() as log:
        app = create_app(
            data_dir=Path(overlay.data_dir),
            trusted_principal_resolver=rehearsal_principal,
            startup_effect_fence=fence,
        )
        # Own the in-process transport rather than importing a test-only client:
        # dependency warnings must not leak into the operator's CLI status line.
        # ASGITransport does not run lifespan; enter it explicitly for the proof.
        reads: list[str] = []
        results: list[CandidateProjectVerification] = []
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                follow_redirects=True,
            ) as client,
        ):
            log.phase = "health and inventory reads"
            await _verify_candidate_health(client, opened)
            reads.append("/api/health")
            cards = await _candidate_project_cards(client, sorted(users_by_id))
            if set(cards) != (
                {project.project_id for project in overlay.projects}
                | set(overlay.excluded_project_ids)
            ):
                raise CandidateRehearsalRefused(
                    "Candidate project inventory omitted or substituted a project."
                )
            reads.append("/api/projects")
            # Space-wide reads first serve copied caches and rows that no project open
            # has rebuilt, then the state that opening every project leaves behind.
            if overlay.display_caches:
                log.phase = "space reads before projects open"
                await _verify_aggregate_reads(client, overlay, opened, sorted(users_by_id))
                # A copied cache can lag its history after a failed refresh, and the
                # project route serves it as it is; the replay proof reads a fresh one.
                for project in overlay.projects:
                    app.state.catalog._cached_snapshot_path(project.project_id).unlink(
                        missing_ok=True
                    )
            log.phase = "project reads"
            for project in overlay.projects:
                headers = {"x-rcp-rehearsal-user": project_principals[project.project_id]}
                if project.capture_status == "captured":
                    results.append(await _verify_captured_project(client, project, headers, reads))
                else:
                    results.append(_verify_unavailable_project(cards[project.project_id], project))
            log.phase = "space reads after projects open"
            await _verify_aggregate_reads(client, overlay, opened, sorted(users_by_id))
            log.phase = "due consolidation"
            await asyncio.to_thread(_verify_due_consolidation, app, _captured_project_ids(overlay))
            log.phase = "shutdown"
        refusal = log.refusal(_captured_project_ids(overlay))
        if refusal is not None:
            raise CandidateRehearsalRefused(refusal)
        if fence.attempted_effects:
            raise CandidateRehearsalRefused(
                "Candidate startup attempted an effect while its fence was closed."
            )
    return CandidateRehearsalResult(
        status="verified",
        space_id=opened.space_id,
        space_kind="team",
        startup_recovery=StartupRecoveryReadModel.model_validate(app.state.startup_recovery_plan),
        projects=tuple(sorted(results, key=lambda item: item.project_id)),
        reads=tuple(reads),
        attempted_effects=fence.attempted_effects,
    )


# Warnings a candidate check may log on copied data, as (logger, message
# template). Each passes only while it names no copied project; the same
# warning about copied state fails the check. Keep this list short.
_EXPECTED_WARNINGS = frozenset(
    {
        # An uncopied project's Experiment target cannot be read on the copy.
        ("rcp.api.index", "Unavailable %s Experiment target %s in project %s: %s"),
    }
)


@contextmanager
def _phase_log() -> Iterator[_PhaseLog]:
    """Capture the candidate check's application records, at WARNING and worse."""
    log = _PhaseLog()
    root = logging.getLogger()
    previous = root.level
    root.addHandler(log)
    if previous > logging.WARNING:
        root.setLevel(logging.WARNING)
    try:
        yield log
    finally:
        root.removeHandler(log)
        root.setLevel(previous)


class _PhaseLog(logging.Handler):
    """Every WARNING or worse record the candidate check logs, tagged with its phase."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.phase = "startup"
        self.records: list[tuple[str, logging.LogRecord]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((self.phase, record))

    def refusal(self, captured: set[str]) -> str | None:
        """Name the first record that is not an expected warning about uncopied state."""
        for phase, record in self.records:
            message = record.getMessage()
            if (
                record.levelno == logging.WARNING
                and record.exc_info is None
                and (record.name, record.msg) in _EXPECTED_WARNINGS
                and not any(project_id in message for project_id in captured)
            ):
                continue
            error = record.exc_info[0] if record.exc_info else None
            return (
                f"Candidate logged {record.levelname} from {record.name} during {phase}: "
                f"{message[:300]}" + (f" ({error.__name__})" if error is not None else "")
            )
        return None


def _captured_project_ids(overlay: RehearsalOverlay) -> set[str]:
    return {
        project.project_id for project in overlay.projects if project.capture_status == "captured"
    }


def _project_principals(
    overlay: RehearsalOverlay, opened: AppStore, users_by_id: Mapping[str, object]
) -> dict[str, str]:
    """The enrolled member each copied project is read as."""

    principals: dict[str, str] = {}
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
        principals[project.project_id] = principal
    return principals


async def _verify_candidate_health(client: httpx.AsyncClient, opened: AppStore) -> None:
    health = await client.get("/api/health")
    if health.status_code != 200:
        raise CandidateRehearsalRefused("Candidate health read failed.")
    payload = health.json()
    if payload.get("space_id") != opened.space_id or payload.get("space_kind") != "team":
        raise CandidateRehearsalRefused("Candidate health changed copied space identity.")


async def _candidate_project_cards(
    client: httpx.AsyncClient, principals: list[str]
) -> dict[str, dict[str, object]]:
    """Every member's project inventory, which must agree; protected before enrollment."""

    if not principals:
        listing = await client.get("/api/projects")
        if listing.status_code != 403:
            raise CandidateRehearsalRefused(
                "Candidate pre-enrollment project inventory did not stay protected."
            )
        return {}
    cards: dict[str, dict[str, object]] = {}
    for principal in principals:
        listing = await client.get("/api/projects", headers={"x-rcp-rehearsal-user": principal})
        if listing.status_code != 200:
            raise CandidateRehearsalRefused("Candidate project inventory read failed.")
        for raw_card in listing.json():
            card = dict(raw_card)
            # The unread digest count is per member; the catalog card is not.
            card.pop("digest_count", None)
            project_id = str(card["id"])
            existing = cards.get(project_id)
            if existing is not None and existing != card:
                raise CandidateRehearsalRefused(
                    "Candidate project inventory changed between member reads."
                )
            cards[project_id] = card
    return cards


# Project-level resources a member page subscribes to, plus main's graph.
_WATERMARK_RESOURCES = (
    *(
        {"name": name}
        for name in get_args(ResourceName)
        if name not in {"graph", "chat", "task", "episode"}
    ),
    {"name": "graph", "ref": "main"},
)


async def _verify_aggregate_reads(
    client: httpx.AsyncClient,
    overlay: RehearsalOverlay,
    opened: AppStore,
    members: list[str],
) -> None:
    """Every member's space-wide reads: Runs, Experiment indexes, watermarks, Inbox.

    Kept out of `reads`, like Paper: the previous release's baseline names only
    the reads it served.
    """

    captured = _captured_project_ids(overlay)
    for member in members:
        headers = {"x-rcp-rehearsal-user": member}
        visible = sorted(opened.member_project_ids(member))
        experiment_index = {"mode": "experiment_loop"}
        responses = [
            await client.get("/api/space/runs", headers=headers),
            await client.get("/api/episodes", params=experiment_index, headers=headers),
            await client.post(
                "/api/resource-watermarks",
                headers=headers,
                json={
                    "client_id": "update-rehearsal",
                    "open": [
                        {"project_id": project_id, "resources": list(_WATERMARK_RESOURCES)}
                        for project_id in visible
                        if project_id in captured
                    ],
                    "due": visible,
                },
            ),
        ]
        for project_id in visible:
            base = f"/api/projects/{project_id}"
            responses.append(
                await client.get(
                    f"{base}/experiment-episodes", params=experiment_index, headers=headers
                )
            )
            responses.append(await client.get(f"{base}/consolidation", headers=headers))
        for response in responses:
            path = response.request.url.path
            if response.status_code != 200:
                raise CandidateRehearsalRefused(
                    f"Candidate read {response.request.method} {path} "
                    f"returned {response.status_code}."
                )
            # The indexes degrade an unreadable live loop to one row rather than
            # fail; on copied local state that row is the candidate's failure.
            if path.endswith(("/api/episodes", "/experiment-episodes")) and any(
                row["project_id"] in captured for row in response.json()["unavailable"]
            ):
                raise CandidateRehearsalRefused(
                    f"Candidate read GET {path} could not read a copied Experiment."
                )


class _RehearsalLaunches:
    """The task engine as the consolidation poller sees it, minus the provider.

    Each launch runs the engine's own pre-spawn validation on the copied row
    and stops where a live launch would start the provider.
    """

    def __init__(self, tasks: BackgroundAgentTasks, captured: set[str]) -> None:
        self._tasks = tasks
        self._captured = captured
        self.dispatch_authority_resolver = tasks.dispatch_authority_resolver
        self.validated: set[str] = set()
        self.refusals: dict[str, str] = {}

    def launch_admitted(self, operation_id: str) -> AgentTaskRecord:
        from rcp.runs.provider_login import AdmissionRefused

        record = self._tasks.store.agent_task(operation_id)
        if record is None:
            raise KeyError(operation_id)
        if record.project_id not in self._captured:
            # An omitted project is warned about, not proved; a raise here would
            # log an ERROR that refuses the whole update.
            return record
        try:
            launch = self._tasks.validate_admitted_launch(operation_id)
        except AdmissionRefused:
            # Provider sign-in and machine programs stay outside the copy; a live
            # launch defers on them and retries.
            launch = record
        except Exception as exc:
            self.refusals[operation_id] = str(exc)
            raise
        self.validated.add(operation_id)
        return launch.record if isinstance(launch, AdmittedLaunch) else launch


def _verify_due_consolidation(app: FastAPI, captured: set[str]) -> None:
    """Launch queued consolidations of `captured` projects, then admit each schedule when due.

    Real admission writes the rows; the engine's own launch validation reads them
    back. Only the provider process is left out. Installed-upgrade CI also runs
    this on the upgraded live data.
    """

    tasks: BackgroundAgentTasks = app.state.background_tasks
    store = tasks.store

    def service_for(project_id: str):
        if project_id not in captured:
            raise KeyError(project_id)  # Not copied; the proof never opens it.
        return app.state.catalog.open(project_id)

    launches = _RehearsalLaunches(tasks, captured)
    poller = ConsolidationPoller(
        store, cast(BackgroundAgentTasks, launches), service_for=service_for
    )
    before = {run.run_id for run in store.consolidation_runs()}
    now = datetime.fromisoformat(store.now())
    for instant in sorted(
        {
            now,
            *(
                max(now, datetime.fromisoformat(s.next_due_at))
                for s in store.consolidation_schedules()
            ),
        }
    ):
        poller.dispatch_due(instant)
    if launches.refusals:
        operation_id, reason = next(iter(launches.refusals.items()))
        raise CandidateRehearsalRefused(
            f"Candidate refused consolidation {operation_id} before launch: {reason}"
        )
    for run in store.consolidation_runs():
        if run.project_id not in captured:
            continue
        task = store.agent_task(run.operation_id) if run.operation_id else None
        due = run.run_id not in before or (task is not None and task.status == "queued")
        if due and run.operation_id is not None and run.operation_id not in launches.validated:
            raise CandidateRehearsalRefused(
                f"Candidate consolidation {run.operation_id} never reached launch validation."
            )
        if run.run_id not in before and run.error_code is not None:
            raise CandidateRehearsalRefused(
                f"Candidate consolidation admission failed with {run.error_code}."
            )


async def _verify_captured_project(
    client: httpx.AsyncClient,
    project: RehearsalProjectOverlay,
    headers: dict[str, str],
    reads: list[str],
) -> CandidateProjectVerification:
    base = f"/api/projects/{project.project_id}"
    response = await client.get(base, headers=headers)
    if response.status_code != 200:
        raise CandidateRehearsalRefused(
            f"Candidate replay failed for project {project.project_id}."
        )
    payload = response.json()
    graph = payload.get("graph")
    revision = graph.get("revision") if isinstance(graph, dict) else None
    if payload.get("id") != project.project_id or revision != project.expected_revision:
        raise CandidateRehearsalRefused(f"Candidate replay changed project {project.project_id}.")
    tasks = await client.get(f"{base}/tasks", headers=headers)
    watchers = await client.get(f"{base}/watchers", headers=headers)
    if tasks.status_code != 200 or watchers.status_code != 200:
        raise CandidateRehearsalRefused(
            f"Candidate operational reads failed for project {project.project_id}."
        )
    reads.extend((base, f"{base}/tasks", f"{base}/watchers"))
    # Paper reads are proven stable here but kept out of `reads`: the
    # previous release's baseline names only the reads it served.
    paper_proof = await _paper_read_proof(client, project.project_id, headers=headers)
    if await _paper_read_proof(client, project.project_id, headers=headers) != paper_proof:
        raise CandidateRehearsalRefused(
            f"Candidate Paper reads changed project {project.project_id}."
        )
    return CandidateProjectVerification(
        project_id=project.project_id,
        status="verified",
        revision=revision,
        projection_sha256=_canonical_sha256(graph),
    )


def _verify_unavailable_project(
    listed: dict[str, object], project: RehearsalProjectOverlay
) -> CandidateProjectVerification:
    # Compare durable identities, not this rehearsal's copied paths.
    card = dict(listed)
    card.update(locator=project.original_locator, state_location=project.original_state_location)
    projection_sha256 = unavailable_card_projection_sha256(
        card, project.expected_card_sha256, team_space=True
    )
    if projection_sha256 != project.expected_card_sha256:
        raise CandidateRehearsalRefused(
            f"Candidate changed unavailable projection {project.project_id}."
        )
    return CandidateProjectVerification(
        project_id=project.project_id,
        status="not_replay_verified",
        revision=None,
        projection_sha256=projection_sha256,
    )


async def _paper_read_proof(
    client: httpx.AsyncClient, project_id: str, *, headers: dict[str, str]
) -> str:
    """Read durable live Paper through its served API, including every saved file."""
    base = f"/api/projects/{project_id}/paper"
    proof: list[object] = []

    async def read(path: str):
        response = await client.get(path, headers=headers)
        if response.status_code != 200:
            raise CandidateRehearsalRefused(f"Candidate Paper read failed for {project_id}.")
        return response

    tree = (await read(f"{base}/tree")).json()
    proof.append(tree)
    proof.append((await read(f"{base}/history")).json())
    for file in tree["files"]:
        file_id = file["file_id"]
        raw = await read(f"{base}/files/{file_id}/raw?revision={tree['saved_revision']}")
        digest = hashlib.sha256(raw.content).hexdigest()
        if digest != file["sha256"]:
            raise CandidateRehearsalRefused("Candidate Paper file digest differs from its tree.")
        proof.append((file_id, digest))
    return _canonical_sha256(proof)


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
