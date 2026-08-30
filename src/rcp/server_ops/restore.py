"""Crash-safe construction of one offline team-server restore candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rcp.limits import (
    BACKUP_COPY_BUFFER_BYTES,
    BACKUP_RECEIPT_MAX_BYTES,
    SERVER_RESTORE_DECRYPT_TIMEOUT_SECONDS,
)
from rcp.server_ops.backup import BACKUP_ARCHIVE_FORMAT, require_age_1x
from rcp.server_ops.backup_models import BackupArchiveManifest
from rcp.server_ops.cli import CallerIdentity, PreparedServerCommand, ServerEventEmitter
from rcp.server_ops.config import (
    InstalledServerConfig,
    load_installed_server_config,
    validate_age_recipient,
)
from rcp.server_ops.git_credentials import (
    DeployKeyMaterial,
    GitCredentialManager,
    GitCredentialRefused,
    GitWriteProbe,
    restore_deploy_key_operator_step,
)
from rcp.server_ops.install import (
    InstalledServiceControlRefused,
    InstalledSystemServiceController,
)
from rcp.server_ops.layout import DEFAULT_SERVER_LAYOUT, ServerLayout
from rcp.server_ops.models import (
    CommandAction,
    ExternalAction,
    ExternalServiceTarget,
    MachineTarget,
    NonsecretField,
    ServerCommandRequest,
    ServerPlanEvent,
    ServerStep,
)
from rcp.server_ops.project_checkout import (
    ProjectCheckoutManager,
    ProjectCheckoutRefused,
)
from rcp.storage import AppStore, ProjectProvisioningMachineIntent

RESTORE_JOURNAL_SCHEMA_VERSION = 1
RESTORE_JOURNAL_NAME = "restore.json"
RESTORE_JOURNAL_MODE = 0o600
RESTORE_DIRECTORY_MODE = 0o700
RESTORE_ARCHIVE_FORMAT = BACKUP_ARCHIVE_FORMAT

# Every schema era accepted by the immutable server-upgrade harness, plus the
# current schema. A later schema change must deliberately extend this set before
# that release can restore its own archives.
SUPPORTED_RESTORE_DATABASE_SCHEMAS = frozenset(
    {
        "91b15bf1f86acba1a9e29d3ad2d222a568fc3ea1781bb09ade823f2d990ca9f0",
        "fc86f6048a4696bdab5b06771271a3651e7972432ad6f5b5ceefc3be40794426",
        "fb854eddda342b3d83507c3c8dd344cc635306b6a82b022382ed5c0dbdbf6f52",
        "bd813aa0f7161aa398433459f622c4d4d156e4a14c201d1b1cb3d1088a57b70f",
        "67ec363b621d9d8e3d0855c719d511e32b6d82733735cbb996c13b284d57105c",
        "742cd73108cb6cc8e23122ad8a1c18444c28ce572b3c2e16d4673365cec2f8a4",
        "ad6edcdd78df9b32ad11f6ff14e2f51ee68034f27f2a46dfe3840fb0a64926c1",
        "5b06cd734c8fd7cbd016e425955751407e61fa21b864a0a81d6585ca219e314e",
    }
)

_SHA256 = frozenset("0123456789abcdef")
_FULL_COMMIT = frozenset("0123456789abcdef")
_RESTORE_ALIAS = re.compile(r"[a-z][a-z0-9-]{0,47}")
_OPENSSH_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")


class RestoreRefused(RuntimeError):
    """A restore stopped without making ambiguous target state serveable."""


class _ReportedRestoreFailure(RuntimeError):
    pass


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _digest(value: str, *, label: str) -> str:
    if len(value) != 64 or any(character not in _SHA256 for character in value):
        raise ValueError(f"{label} must be lowercase SHA-256")
    return value


def _absolute(value: str, *, label: str) -> str:
    path = Path(value)
    if not path.is_absolute() or path == Path("/") or ".." in path.parts or str(path) != value:
        raise ValueError(f"{label} must be an absolute normalized non-root path")
    return value


class RestoreConfirmation(_StrictModel):
    confirmed_data_dir: str
    confirmed_by: str = Field(min_length=1, max_length=400)
    confirmed_at: datetime

    @field_validator("confirmed_data_dir")
    @classmethod
    def validate_data_dir(cls, value: str) -> str:
        return _absolute(value, label="confirmed restore data directory")

    @field_validator("confirmed_by")
    @classmethod
    def validate_actor(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized or any(ord(character) < 32 for character in normalized):
            raise ValueError("restore confirmer must be one safe line")
        return normalized

    @field_validator("confirmed_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("restore confirmation time requires a UTC offset")
        return value


class RestoreRepositoryRecovery(_StrictModel):
    project_id: str
    repository_alias: str
    machine_alias: str
    state: Literal["key_started", "key_ready", "checkout_ready"]
    deploy_key_label: str | None = None
    deploy_public_key: str | None = None
    public_key_fingerprint: str | None = None
    probed_commit: str | None = None
    central_root: str | None = None
    repository_path: str | None = None
    checkout_disposition: Literal["request_created", "reused_existing"] | None = None
    checkout_commit: str | None = None

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except (AttributeError, ValueError) as exc:
            raise ValueError("restore repository project id must be a canonical UUID4") from exc
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError("restore repository project id must be a canonical UUID4")
        return value

    @field_validator("repository_alias", "machine_alias")
    @classmethod
    def validate_alias(cls, value: str) -> str:
        if _RESTORE_ALIAS.fullmatch(value) is None:
            raise ValueError("restore repository alias is invalid")
        return value

    @field_validator("deploy_key_label", "deploy_public_key")
    @classmethod
    def validate_key_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        if (
            not value
            or len(value) > 16 * 1024
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError(f"restore {info.field_name.replace('_', ' ')} is invalid")
        return value

    @field_validator("public_key_fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str | None) -> str | None:
        if value is not None and _OPENSSH_FINGERPRINT.fullmatch(value) is None:
            raise ValueError("restore public-key fingerprint is invalid")
        return value

    @field_validator("probed_commit", "checkout_commit")
    @classmethod
    def validate_commit(cls, value: str | None, info) -> str | None:
        if value is not None and (
            len(value) != 40 or any(character not in _FULL_COMMIT for character in value)
        ):
            raise ValueError(f"restore {info.field_name.replace('_', ' ')} is invalid")
        return value

    @field_validator("central_root", "repository_path")
    @classmethod
    def validate_optional_path(cls, value: str | None, info) -> str | None:
        return (
            None
            if value is None
            else _absolute(value, label=f"restore {info.field_name.replace('_', ' ')}")
        )

    @model_validator(mode="after")
    def validate_progress(self) -> RestoreRepositoryRecovery:
        key_fields = (
            self.deploy_key_label,
            self.deploy_public_key,
            self.public_key_fingerprint,
        )
        checkout_fields = (
            self.central_root,
            self.repository_path,
            self.checkout_disposition,
            self.checkout_commit,
        )
        if self.state == "key_started" and any((*key_fields, self.probed_commit, *checkout_fields)):
            raise ValueError("a restore key-start receipt cannot claim later effects")
        if self.state in {"key_ready", "checkout_ready"} and not all(key_fields):
            raise ValueError("a restore key-ready receipt requires its complete public proof")
        if self.state == "key_ready" and any(checkout_fields):
            raise ValueError("a restore key-ready receipt cannot claim a checkout")
        if self.state == "checkout_ready" and (
            self.probed_commit is None or not all(checkout_fields)
        ):
            raise ValueError("a restore checkout receipt requires every checkout proof")
        if self.checkout_commit is not None and self.checkout_commit != self.probed_commit:
            raise ValueError("restore checkout and write-probe commits differ")
        return self


class RestoreProjectRebind(_StrictModel):
    project_id: str
    locator: str
    state_location: str
    state_remote: bool

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except (AttributeError, ValueError) as exc:
            raise ValueError("restored project id must be a canonical UUID4") from exc
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError("restored project id must be a canonical UUID4")
        return value

    @field_validator("locator")
    @classmethod
    def validate_locator(cls, value: str) -> str:
        return _absolute(value, label="restored project locator")

    @field_validator("state_location")
    @classmethod
    def validate_state_location(cls, value: str) -> str:
        if (
            not value
            or len(value) > 8192
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("restored project state location is invalid")
        return value


class RestoreProjectPublication(_StrictModel):
    project_id: str
    capture_sha256: str
    published_files: int = Field(ge=1)
    published_bytes: int = Field(ge=0)
    main_revision: int = Field(ge=0)
    branch_count: int = Field(ge=0)

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except (AttributeError, ValueError) as exc:
            raise ValueError("published project id must be a canonical UUID4") from exc
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError("published project id must be a canonical UUID4")
        return value

    @field_validator("capture_sha256")
    @classmethod
    def validate_capture_sha256(cls, value: str) -> str:
        return _digest(value, label="published project capture digest")


class RestoreOperationJournal(_StrictModel):
    schema_version: Literal[RESTORE_JOURNAL_SCHEMA_VERSION] = RESTORE_JOURNAL_SCHEMA_VERSION
    operation_id: str
    archive_format: Literal[RESTORE_ARCHIVE_FORMAT] = RESTORE_ARCHIVE_FORMAT
    archive_path: str
    archive_sha256: str
    archive_size_bytes: int = Field(gt=0)
    manifest_sha256: str
    configured_data_dir: str
    candidate_root: str
    candidate_sqlite_path: str
    candidate_sqlite_sha256: str
    manifest: BackupArchiveManifest
    confirmation: RestoreConfirmation
    replacement_restore: Literal[True] = True
    machine_local_operations: Literal["not_restored"] = "not_restored"
    phase: Literal[
        "archive_verified",
        "sqlite_restored",
        "checkouts_recovering",
        "checkouts_ready",
        "projects_rebinding",
        "checkouts_reconstructed",
        "projects_publishing",
        "projects_published",
    ]
    detached_at: datetime
    restored_sqlite_sha256: str | None = None
    repository_recoveries: tuple[RestoreRepositoryRecovery, ...] = ()
    project_rebinds: tuple[RestoreProjectRebind, ...] = ()
    project_publications: tuple[RestoreProjectPublication, ...] = ()
    updated_at: datetime

    @field_validator("operation_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except (AttributeError, ValueError) as exc:
            raise ValueError("restore operation id must be a canonical UUID4") from exc
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError("restore operation id must be a canonical UUID4")
        return value

    @field_validator(
        "archive_path",
        "configured_data_dir",
        "candidate_root",
        "candidate_sqlite_path",
    )
    @classmethod
    def validate_path(cls, value: str, info) -> str:
        return _absolute(value, label=info.field_name.replace("_", " "))

    @field_validator(
        "archive_sha256",
        "manifest_sha256",
        "candidate_sqlite_sha256",
        "restored_sqlite_sha256",
    )
    @classmethod
    def validate_digest(cls, value: str | None, info) -> str | None:
        return None if value is None else _digest(value, label=info.field_name.replace("_", " "))

    @field_validator("detached_at", "updated_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("restore journal times require a UTC offset")
        return value

    @model_validator(mode="after")
    def validate_boundary(self) -> RestoreOperationJournal:
        root = Path(self.candidate_root)
        candidate = Path(self.candidate_sqlite_path)
        if root.parent.name != "restore-operations" or root not in candidate.parents:
            raise ValueError("restore candidate paths are outside the operation root")
        expected_root = f"candidate-{self.archive_sha256}"
        if root.name != expected_root:
            raise ValueError("restore candidate root is not bound to the archive")
        if self.configured_data_dir != self.confirmation.confirmed_data_dir:
            raise ValueError("restore destination differs from its human confirmation")
        if self.manifest.database_schema_sha256 not in SUPPORTED_RESTORE_DATABASE_SCHEMAS:
            raise ValueError("restore journal names an unsupported persistence boundary")
        installed = self.phase != "archive_verified"
        if not installed and (
            self.restored_sqlite_sha256 is not None
            or self.repository_recoveries
            or self.project_rebinds
            or self.project_publications
        ):
            raise ValueError("an uninstalled restore journal cannot name later effects")
        if installed and (
            self.restored_sqlite_sha256 is None
            or self.restored_sqlite_sha256 != self.candidate_sqlite_sha256
        ):
            raise ValueError("a restored journal must bind the installed candidate bytes")
        repository_keys = [
            (item.project_id, item.repository_alias) for item in self.repository_recoveries
        ]
        project_ids = [item.project_id for item in self.project_rebinds]
        publication_ids = [item.project_id for item in self.project_publications]
        if repository_keys != sorted(set(repository_keys)):
            raise ValueError("restore repository recovery receipts must be sorted and unique")
        if project_ids != sorted(set(project_ids)):
            raise ValueError("restore project rebind receipts must be sorted and unique")
        if publication_ids != sorted(set(publication_ids)):
            raise ValueError("restore project publication receipts must be sorted and unique")
        captured = {
            project.project_id: project
            for project in self.manifest.projects
            if project.status == "captured"
        }
        expected_repositories: set[tuple[str, str]] = set()
        for project in captured.values():
            if project.recovery is None:
                raise ValueError("captured restore project is missing its recovery descriptor")
            expected_repositories.update(
                (project.project_id, repository.alias)
                for repository in project.recovery.repositories
            )
        if not set(repository_keys).issubset(expected_repositories):
            raise ValueError("restore repository receipt is outside captured recovery inventory")
        if not set(project_ids).issubset(captured):
            raise ValueError("restore project rebind is outside captured project inventory")
        if self.phase in {
            "checkouts_ready",
            "projects_rebinding",
            "checkouts_reconstructed",
            "projects_publishing",
            "projects_published",
        } and (
            set(repository_keys) != expected_repositories
            or any(item.state != "checkout_ready" for item in self.repository_recoveries)
        ):
            raise ValueError("checkout-ready restore phase requires every repository receipt")
        if self.phase in {
            "checkouts_reconstructed",
            "projects_publishing",
            "projects_published",
        } and set(project_ids) != set(captured):
            raise ValueError("reconstructed restore phase requires every captured project rebind")
        if self.project_rebinds and self.phase not in {
            "projects_rebinding",
            "checkouts_reconstructed",
            "projects_publishing",
            "projects_published",
        }:
            raise ValueError("project rebind receipts require the catalog-rebinding phase")
        if not set(publication_ids).issubset(captured):
            raise ValueError("restore project publication is outside captured project inventory")
        if self.project_publications and self.phase not in {
            "projects_publishing",
            "projects_published",
        }:
            raise ValueError("project publication receipts require the publication phase")
        if self.phase == "projects_published" and set(publication_ids) != set(captured):
            raise ValueError("published restore phase requires every captured project receipt")
        return self


@dataclass(frozen=True)
class RestoreCandidate:
    archive_path: Path
    archive_sha256: str
    archive_size_bytes: int
    manifest_sha256: str
    manifest: BackupArchiveManifest
    root: Path
    sqlite_path: Path
    sqlite_sha256: str
    detached_at: datetime


@dataclass(frozen=True)
class RestoreCheckoutRecoveryOutcome:
    journal: RestoreOperationJournal
    operator_action: ServerStep | None = None


class RestoreMachine(Protocol):
    def admission(self) -> AbstractContextManager[None]: ...

    def configured_data_dir(self) -> Path: ...

    def stage_candidate(
        self,
        archive_path: Path,
        identity_file: Path,
        *,
        confirmed_by: str,
    ) -> RestoreCandidate: ...

    def journal_candidate(
        self,
        candidate: RestoreCandidate,
        confirmation: RestoreConfirmation,
    ) -> RestoreOperationJournal: ...

    def install_sqlite_candidate(
        self,
        journal: RestoreOperationJournal,
    ) -> RestoreOperationJournal: ...

    def verify_offline_candidate(
        self,
        journal: RestoreOperationJournal,
    ) -> RestoreOperationJournal: ...

    def recover_checkouts(
        self,
        journal: RestoreOperationJournal,
        *,
        resume_argv: tuple[str, ...],
        step_number: int,
    ) -> RestoreCheckoutRecoveryOutcome: ...

    def rebind_checkouts(
        self,
        journal: RestoreOperationJournal,
    ) -> RestoreOperationJournal: ...

    def publish_projects(
        self,
        journal: RestoreOperationJournal,
    ) -> RestoreOperationJournal: ...


def prepare_restore_command(
    request: ServerCommandRequest,
    identity: CallerIdentity,
    *,
    machine: RestoreMachine | None = None,
    resume_executable: Path = DEFAULT_SERVER_LAYOUT.cli_wrapper,
) -> PreparedServerCommand:
    if (
        request.command != "server restore"
        or request.archive_path is None
        or request.recovery_identity_file is None
    ):
        raise ValueError("prepare_restore_command requires one complete server restore request")
    resolved_machine = machine or LinuxRestoreMachine()
    data_dir = resolved_machine.configured_data_dir()
    plan = ServerPlanEvent(
        command=request.command,
        timestamp=datetime.now(UTC),
        steps=_restore_plan(identity, data_dir),
    )

    def execute(emitter: ServerEventEmitter, _input_stream: BinaryIO) -> None:
        if request.restore_confirmed_data_dir != str(data_dir):
            _execute_restore(
                request,
                identity,
                emitter,
                resolved_machine,
                data_dir=data_dir,
                resume_executable=resume_executable,
            )
            return
        try:
            with resolved_machine.admission():
                _execute_restore(
                    request,
                    identity,
                    emitter,
                    resolved_machine,
                    data_dir=data_dir,
                    resume_executable=resume_executable,
                )
        except RestoreRefused as exc:
            planned = emitter.events[0]
            assert isinstance(planned, ServerPlanEvent)
            emitter.emit_step(
                planned.steps[2].model_copy(update={"state": "failed", "message": str(exc)})
            )

    return PreparedServerCommand(plan=plan, execute=execute)


def _restore_plan(identity: CallerIdentity, data_dir: Path) -> tuple[ServerStep, ...]:
    root = MachineTarget(host=identity.host, os_account="root")
    return (
        ServerStep(
            number=1,
            title="Confirm the replacement destination",
            purpose=(
                "Bind this replacement restore to the installed server's fixed, fresh data "
                "directory before any target bytes change."
            ),
            performed_by="human",
            target=root,
            phase="restore_destination_confirm",
            state="pending",
            expected_success=f"The operator confirms exact destination {data_dir}.",
            message="RCP will display the configured destination for explicit confirmation.",
        ),
        ServerStep(
            number=2,
            title="Decrypt and verify the archive",
            purpose=(
                "Authenticate age ciphertext integrity and verify the strict manifest, every "
                "file hash, database schema, and compatible source boundary in protected space."
            ),
            performed_by="system",
            target=root,
            phase="restore_archive_verify",
            state="pending",
            expected_success="Every declared archive byte and persistence boundary is supported.",
            message="RCP will validate the complete archive before changing the destination.",
        ),
        ServerStep(
            number=3,
            title="Fence service and journal the restore",
            purpose=(
                "Prove systemd is stopped, the configured data directory is empty, and fsync the "
                "exact resumable operation outside every restore target."
            ),
            performed_by="system",
            target=root,
            phase="restore_journal",
            state="pending",
            expected_success="One durable archive-bound restore journal owns the empty target.",
            message="RCP will stop the service and publish the pre-mutation journal.",
        ),
        ServerStep(
            number=4,
            title="Install detached SQLite candidate",
            purpose=(
                "Atomically install the migrated database after every captured continuation and "
                "unfinished machine preparation has been fenced as restored history."
            ),
            performed_by="system",
            target=root,
            phase="restore_sqlite",
            state="pending",
            expected_success="The target contains only the exact detached SQLite candidate.",
            message="RCP will install the validated database without starting the service.",
        ),
        ServerStep(
            number=5,
            title="Verify the stopped restored-state candidate",
            purpose=(
                "Read back target integrity and journal identity while leaving checkout recovery, "
                "publication, authority review, and activation for the remaining restore steps."
            ),
            performed_by="system",
            target=root,
            phase="restore_offline_readback",
            state="pending",
            expected_success="The exact replacement database is durable and systemd remains stopped.",
            message="RCP will verify the offline candidate before checkout recovery begins.",
        ),
        ServerStep(
            number=6,
            title="Reconstruct central checkouts with fresh keys",
            purpose=(
                "Generate replacement repository identities, prove GitHub write access, clone "
                "the exact local or SSH checkouts, and reject conflicting retained research."
            ),
            performed_by="system",
            target=root,
            phase="restore_checkouts",
            state="pending",
            expected_success="Every captured repository has one verified replacement checkout.",
            message="RCP will recover each checkout without publishing archived project state.",
        ),
        ServerStep(
            number=7,
            title="Rebind the stopped project catalog",
            purpose=(
                "Regenerate local locator state and point restored rows only at the verified "
                "replacement checkouts."
            ),
            performed_by="system",
            target=root,
            phase="restore_catalog_rebind",
            state="pending",
            expected_success="Every captured project names only its replacement checkout set.",
            message="RCP will keep every rebound project unavailable until publication passes.",
        ),
        ServerStep(
            number=8,
            title="Publish and replay-verify restored projects",
            purpose=(
                "Publish only captured canonical histories, chats, Paper, facts, and referenced "
                "kept files through their owners, then prove exact replay and byte readback."
            ),
            performed_by="system",
            target=root,
            phase="restore_project_publication",
            state="pending",
            expected_success=(
                "Every captured project is replay-proven and readable; explicitly uncaptured "
                "projects remain visible but unavailable."
            ),
            message="RCP will not start the service or restore excluded machine state.",
        ),
    )


def _execute_restore(
    request: ServerCommandRequest,
    identity: CallerIdentity,
    emitter: ServerEventEmitter,
    machine: RestoreMachine,
    *,
    data_dir: Path,
    resume_executable: Path,
) -> None:
    planned = emitter.events[0]
    assert isinstance(planned, ServerPlanEvent)
    steps = planned.steps
    confirmed = request.restore_confirmed_data_dir
    resume = _restore_resume_argv(
        request,
        resume_executable=resume_executable,
        data_dir=data_dir,
    )
    if confirmed is None:
        emitter.emit_step(
            steps[0].model_copy(
                update={
                    "state": "operator_action_needed",
                    "message": (
                        "Confirm that this installed destination is the intended fresh replacement "
                        "target; RCP will not redirect systemd to another path."
                    ),
                    "actions": (CommandAction(argv=resume),),
                    "fields": (NonsecretField(name="configured_data_dir", value=str(data_dir)),),
                    "resume_argv": resume,
                }
            )
        )
        return
    if confirmed != str(data_dir):
        emitter.emit_step(
            steps[0].model_copy(
                update={
                    "state": "failed",
                    "message": (
                        "The confirmed restore destination differs from the installed RCP data "
                        "directory. Rerun using the exact displayed path."
                    ),
                }
            )
        )
        return
    now = datetime.now(UTC)
    confirmer = f"{identity.username}@{identity.host} uid={identity.uid}"
    confirmation = RestoreConfirmation(
        confirmed_data_dir=confirmed,
        confirmed_by=confirmer,
        confirmed_at=now,
    )
    emitter.emit_step(
        steps[0].model_copy(
            update={
                "state": "succeeded",
                "message": "The replacement destination is explicitly confirmed.",
                "fields": (NonsecretField(name="configured_data_dir", value=confirmed),),
            }
        )
    )
    try:
        candidate = _run_restore_step(
            emitter,
            steps[1],
            running="Decrypting in protected space and checking every declared archive byte.",
            operation=lambda: machine.stage_candidate(
                Path(request.archive_path),
                Path(request.recovery_identity_file),
                confirmed_by=confirmer,
            ),
            succeeded="The archive, persistence boundary, and detached database candidate pass.",
            fields=lambda value: (
                NonsecretField(name="archive_sha256", value=value.archive_sha256),
                NonsecretField(name="space_id", value=value.manifest.space_id),
                NonsecretField(name="source_commit", value=value.manifest.rcp_source_commit),
                NonsecretField(name="project_count", value=len(value.manifest.projects)),
            ),
        )
        journal = _run_restore_step(
            emitter,
            steps[2],
            running="Stopping systemd, proving the target empty, and fsyncing the restore journal.",
            operation=lambda: machine.journal_candidate(candidate, confirmation),
            succeeded="The stopped replacement target is owned by one durable restore journal.",
            fields=lambda value: (
                NonsecretField(name="operation_id", value=value.operation_id),
                NonsecretField(name="restore_phase", value=value.phase),
            ),
        )
        journal = _run_restore_step(
            emitter,
            steps[3],
            running="Installing the exact detached SQLite candidate atomically.",
            operation=lambda: machine.install_sqlite_candidate(journal),
            succeeded="The detached SQLite candidate is durable in the configured data directory.",
            fields=lambda value: (
                NonsecretField(name="restore_phase", value=value.phase),
                NonsecretField(name="sqlite_sha256", value=value.candidate_sqlite_sha256),
            ),
        )
        journal = _run_restore_step(
            emitter,
            steps[4],
            running="Reading back the journal, SQLite integrity, target inventory, and stopped service.",
            operation=lambda: machine.verify_offline_candidate(journal),
            succeeded="The offline restored-state candidate is valid and remains unserved.",
            fields=lambda value: (
                NonsecretField(name="restore_phase", value=value.phase),
                NonsecretField(name="service_state", value="stopped_disabled"),
                NonsecretField(name="replacement_restore", value=True),
            ),
        )
        emitter.emit_step(
            steps[5].model_copy(
                update={
                    "state": "running",
                    "message": (
                        "Creating fresh deploy keys and reconstructing exact replacement checkouts."
                    ),
                }
            )
        )
        try:
            outcome = machine.recover_checkouts(
                journal,
                resume_argv=resume,
                step_number=steps[5].number,
            )
        except RestoreRefused as exc:
            emitter.emit_step(steps[5].model_copy(update={"state": "failed", "message": str(exc)}))
            return
        if outcome.operator_action is not None:
            emitter.emit_step(outcome.operator_action)
            return
        journal = outcome.journal
        emitter.emit_step(
            steps[5].model_copy(
                update={
                    "state": "succeeded",
                    "message": (
                        "Every captured repository has a fresh key and one verified replacement "
                        "checkout. Archived project state is still unpublished."
                    ),
                    "fields": (
                        NonsecretField(
                            name="recovered_repositories",
                            value=len(journal.repository_recoveries),
                        ),
                    ),
                }
            )
        )
        journal = _run_restore_step(
            emitter,
            steps[6],
            running="Regenerating locators and rebinding stopped catalog rows.",
            operation=lambda: machine.rebind_checkouts(journal),
            succeeded=(
                "Every captured project is rebound but remains unavailable until publication, "
                "replay, authority review, and activation pass."
            ),
            fields=lambda value: (
                NonsecretField(name="restore_phase", value=value.phase),
                NonsecretField(name="rebound_projects", value=len(value.project_rebinds)),
            ),
        )
        journal = _run_restore_step(
            emitter,
            steps[7],
            running=(
                "Publishing captured project bytes through their canonical owners and replaying "
                "every retained graph head."
            ),
            operation=lambda: machine.publish_projects(journal),
            succeeded=(
                "Every captured project passed canonical replay and exact byte readback. The "
                "replacement service remains stopped pending authority review."
            ),
            fields=lambda value: (
                NonsecretField(name="restore_phase", value=value.phase),
                NonsecretField(name="published_projects", value=len(value.project_publications)),
            ),
        )
    except _ReportedRestoreFailure:
        return


def _restore_resume_argv(
    request: ServerCommandRequest,
    *,
    resume_executable: Path,
    data_dir: Path,
) -> tuple[str, ...]:
    assert request.archive_path is not None
    assert request.recovery_identity_file is not None
    return (
        "sudo",
        str(resume_executable),
        "server",
        "restore",
        request.archive_path,
        "--identity-file",
        request.recovery_identity_file,
        "--confirm-data-dir",
        str(data_dir),
    )


def _restore_probe_cleanup_step(
    material: DeployKeyMaterial,
    probe: GitWriteProbe,
    *,
    number: int,
    resume_argv: tuple[str, ...],
) -> ServerStep:
    temporary_ref = probe.temporary_ref or "the request-scoped restore probe ref"
    return ServerStep(
        number=number,
        title="Inspect the restore write-probe ref",
        purpose=(
            "Resolve one ambiguous request-owned GitHub ref without deleting unrelated history."
        ),
        performed_by="human",
        target=ExternalServiceTarget(
            service="github.com",
            resource=material.repository.identity,
            destination_url=material.repository.settings_url,
            required_authority_role="repository administrator",
        ),
        phase="restore_github_probe_cleanup",
        state="operator_action_needed",
        expected_success=(
            "The exact request-scoped ref is absent and the same restore can repeat its proof."
        ),
        message=probe.diagnostic,
        actions=(
            ExternalAction(
                instruction=(
                    f"Inspect {temporary_ref!r} in {material.repository.identity}. Remove it only "
                    "if it is the restore-owned probe ref described above; otherwise preserve it "
                    "and investigate the ownership conflict."
                )
            ),
        ),
        fields=(
            NonsecretField(name="repository", value=material.repository.identity),
            NonsecretField(name="temporary_ref", value=temporary_ref),
        ),
        resume_argv=resume_argv,
    )


def _run_restore_step(
    emitter: ServerEventEmitter,
    planned: ServerStep,
    *,
    running: str,
    operation,
    succeeded: str,
    fields,
):
    emitter.emit_step(planned.model_copy(update={"state": "running", "message": running}))
    try:
        value = operation()
    except RestoreRefused as exc:
        emitter.emit_step(planned.model_copy(update={"state": "failed", "message": str(exc)}))
        raise _ReportedRestoreFailure from exc
    emitter.emit_step(
        planned.model_copy(
            update={"state": "succeeded", "message": succeeded, "fields": tuple(fields(value))}
        )
    )
    return value


class LinuxRestoreMachine:
    """Root coordinator for protected validation and one stopped target database."""

    def __init__(
        self,
        layout: ServerLayout = DEFAULT_SERVER_LAYOUT,
        *,
        config_loader: Callable[[Path], InstalledServerConfig] = load_installed_server_config,
        service_control: InstalledSystemServiceController | None = None,
        age_executable: str = "age",
        clock: Callable[[], datetime] | None = None,
        service_identity: tuple[int, int] | None = None,
        root_identity: tuple[int, int] = (0, 0),
        decryptor: Callable[[Path, Path, Path], str | None] | None = None,
        commit_compatible: Callable[[str, str, Path], bool] | None = None,
        detach_worker: Callable[[Path, str, datetime], None] | None = None,
        credential_manager: GitCredentialManager | None = None,
        checkout_manager: ProjectCheckoutManager | None = None,
    ) -> None:
        self.layout = layout
        self.config_loader = config_loader
        self.age_executable = age_executable
        self.clock = clock or (lambda: datetime.now(UTC))
        if service_identity is None:
            account = pwd.getpwnam(layout.service_account)
            service_identity = (account.pw_uid, account.pw_gid)
        self.service_uid, self.service_gid = service_identity
        self.root_uid, self.root_gid = root_identity
        self.service_control = service_control or InstalledSystemServiceController(
            layout,
            root_identity=root_identity,
        )
        self.decryptor = decryptor or self._decrypt_archive
        self.commit_compatible = commit_compatible or _git_commit_is_supported
        self.detach_worker = detach_worker or self._detach_candidate
        self.credential_manager = credential_manager or GitCredentialManager(layout)
        self.checkout_manager = checkout_manager or ProjectCheckoutManager(
            layout,
            credential_manager=self.credential_manager,
        )
        self._admission_active = False

    @contextmanager
    def admission(self) -> Iterator[None]:
        """Serialize the complete machine-changing restore command."""

        if self._admission_active:
            yield
            return
        from rcp.server_ops.backup import BackupRunRefused, backup_run_coordination_lock
        from rcp.server_ops.update import UpdateRefused, server_update_operation_lock

        try:
            with (
                server_update_operation_lock(
                    self.layout,
                    root_uid=self.root_uid,
                    root_gid=self.root_gid,
                    service_gid=self.service_gid,
                ),
                backup_run_coordination_lock(
                    self.layout,
                    expected_uid=self.service_uid,
                    expected_gid=self.service_gid,
                ),
            ):
                self._admission_active = True
                try:
                    yield
                finally:
                    self._admission_active = False
        except (BackupRunRefused, UpdateRefused) as exc:
            raise RestoreRefused(
                "Another source update or protected backup owns the server machine boundary. "
                "Wait for it to finish, then rerun restore."
            ) from exc

    def configured_data_dir(self) -> Path:
        try:
            config = self.config_loader(self.layout.config_path)
        except (OSError, ValueError) as exc:
            raise RestoreRefused(
                "The installed server configuration is missing or invalid. Complete server "
                "install before restore."
            ) from exc
        if config.paths.model_dump() != self.layout.recorded_paths():
            raise RestoreRefused("The installed configuration names another server layout.")
        return self.layout.data_dir

    def stage_candidate(
        self,
        archive_path: Path,
        identity_file: Path,
        *,
        confirmed_by: str,
    ) -> RestoreCandidate:
        self.configured_data_dir()
        archive_sha256, archive_size = _hash_regular_file(archive_path)
        current_release = self._current_release()
        current_commit = current_release.name
        existing = read_restore_journal_if_present(
            self.layout,
            expected_uid=self.service_uid,
        )
        if existing is not None and (
            existing.archive_sha256 != archive_sha256
            or existing.archive_size_bytes != archive_size
            or existing.archive_path != str(archive_path)
        ):
            raise RestoreRefused(
                "Another unfinished restore owns this server. Re-enter restore with its exact "
                "archive before attempting a replacement operation."
            )
        candidate_root = self.layout.restore_operations_root / f"candidate-{archive_sha256}"
        if existing is not None and _candidate_matches_journal(
            existing, expected_uid=self.service_uid
        ):
            return RestoreCandidate(
                archive_path=archive_path,
                archive_sha256=archive_sha256,
                archive_size_bytes=archive_size,
                manifest_sha256=existing.manifest_sha256,
                manifest=existing.manifest,
                root=candidate_root,
                sqlite_path=Path(existing.candidate_sqlite_path),
                sqlite_sha256=existing.candidate_sqlite_sha256,
                detached_at=existing.detached_at,
            )
        self._prepare_candidate_root(candidate_root)
        plaintext = candidate_root / "archive.tar"
        try:
            recovery_recipient_fingerprint = self.decryptor(
                archive_path,
                identity_file,
                plaintext,
            )
            manifest, manifest_sha256 = _extract_verified_archive(
                plaintext,
                candidate_root / "payload",
                expected_uid=self.service_uid,
                expected_gid=self.service_gid,
            )
            if manifest.database_schema_sha256 not in SUPPORTED_RESTORE_DATABASE_SCHEMAS:
                raise RestoreRefused(
                    "This archive uses an unsupported newer database boundary. Update RCP to "
                    f"commit {manifest.rcp_source_commit} or a compatible later main commit, then "
                    "rerun restore before changing the target."
                )
            if (
                recovery_recipient_fingerprint is not None
                and recovery_recipient_fingerprint != manifest.encryption_recipient_fingerprint
            ):
                raise RestoreRefused(
                    "The supplied recovery identity does not match the recipient fingerprint "
                    "recorded by this archive."
                )
            if not self.commit_compatible(
                manifest.rcp_source_commit,
                current_commit,
                current_release,
            ):
                raise RestoreRefused(
                    "This archive was created by code the installed restore release does not "
                    f"support. Update RCP to commit {manifest.rcp_source_commit} or a compatible "
                    "later main commit, then rerun restore before changing the target."
                )
            raw_sqlite = candidate_root.joinpath(
                "payload", *PurePosixPath(manifest.sqlite_snapshot.archive_path).parts
            )
            raw_store = AppStore.open_read_only_snapshot(raw_sqlite)
            if (
                raw_store.space_id != manifest.space_id
                or raw_store.space_kind != "team"
                or raw_store.space_name != manifest.space_name
                or _database_schema_sha256(raw_sqlite) != manifest.database_schema_sha256
            ):
                raise RestoreRefused(
                    "The archived database identity or schema differs from its manifest."
                )
            restored = candidate_root / "restored" / "rcp.sqlite3"
            restored.parent.mkdir(mode=RESTORE_DIRECTORY_MODE)
            os.chown(restored.parent, self.service_uid, self.service_gid)
            shutil.copyfile(raw_sqlite, restored)
            os.chown(restored, self.service_uid, self.service_gid)
            os.chmod(restored, RESTORE_JOURNAL_MODE)
            detached_at = existing.detached_at if existing is not None else self.clock()
            detachment_actor = (
                existing.confirmation.confirmed_by if existing is not None else confirmed_by
            )
            self.detach_worker(restored, detachment_actor, detached_at)
            sqlite_sha256, _ = _hash_regular_file(restored, expected_uid=self.service_uid)
            _fsync_file(restored)
            _fsync_directory(restored.parent)
            return RestoreCandidate(
                archive_path=archive_path,
                archive_sha256=archive_sha256,
                archive_size_bytes=archive_size,
                manifest_sha256=manifest_sha256,
                manifest=manifest,
                root=candidate_root,
                sqlite_path=restored,
                sqlite_sha256=sqlite_sha256,
                detached_at=detached_at,
            )
        except RestoreRefused:
            raise
        except Exception as exc:
            raise RestoreRefused(
                "The protected archive could not produce one verified restore candidate. The "
                "configured data directory remains unchanged."
            ) from exc
        finally:
            plaintext.unlink(missing_ok=True)

    def journal_candidate(
        self,
        candidate: RestoreCandidate,
        confirmation: RestoreConfirmation,
    ) -> RestoreOperationJournal:
        with self.admission():
            return self._journal_candidate_locked(candidate, confirmation)

    def _journal_candidate_locked(
        self,
        candidate: RestoreCandidate,
        confirmation: RestoreConfirmation,
    ) -> RestoreOperationJournal:
        if confirmation.confirmed_data_dir != str(self.configured_data_dir()):
            raise RestoreRefused("The restore confirmation no longer matches installed config.")
        existing = read_restore_journal_if_present(
            self.layout,
            expected_uid=self.service_uid,
        )
        if existing is None and any(self.layout.data_dir.iterdir()):
            raise RestoreRefused(
                "The configured RCP data directory is not fresh and empty. Restore will not "
                "stop or replace an initialized space."
            )
        try:
            self.service_control.fence_stopped_disabled()
        except InstalledServiceControlRefused as exc:
            raise RestoreRefused(
                "RCP could not prove systemd stopped with no main process. Repair the service "
                "fence before retrying restore."
            ) from exc
        if existing is not None:
            _require_same_candidate(existing, candidate, confirmation)
            return existing
        _require_private_empty_directory(
            self.layout.data_dir,
            uid=self.service_uid,
            gid=self.service_gid,
            label="configured restore data directory",
        )
        now = self.clock()
        journal = RestoreOperationJournal(
            operation_id=str(uuid.uuid4()),
            archive_path=str(candidate.archive_path),
            archive_sha256=candidate.archive_sha256,
            archive_size_bytes=candidate.archive_size_bytes,
            manifest_sha256=candidate.manifest_sha256,
            configured_data_dir=str(self.layout.data_dir),
            candidate_root=str(candidate.root),
            candidate_sqlite_path=str(candidate.sqlite_path),
            candidate_sqlite_sha256=candidate.sqlite_sha256,
            manifest=candidate.manifest,
            confirmation=confirmation,
            phase="archive_verified",
            detached_at=candidate.detached_at,
            updated_at=now,
        )
        write_restore_journal(journal, self.layout, uid=self.service_uid, gid=self.service_gid)
        return read_restore_journal(self.layout, expected_uid=self.service_uid)

    def install_sqlite_candidate(
        self,
        journal: RestoreOperationJournal,
    ) -> RestoreOperationJournal:
        current = read_restore_journal(self.layout, expected_uid=self.service_uid)
        if current.operation_id != journal.operation_id:
            raise RestoreRefused("The durable restore operation changed before database install.")
        target = self.layout.data_dir / "rcp.sqlite3"
        if current.phase != "archive_verified":
            _require_restored_target(current, target, uid=self.service_uid, gid=self.service_gid)
            return current
        candidate = Path(current.candidate_sqlite_path)
        digest, _ = _hash_regular_file(candidate, expected_uid=self.service_uid)
        if digest != current.candidate_sqlite_sha256:
            raise RestoreRefused("The detached SQLite candidate changed after journal publication.")
        entries = tuple(self.layout.data_dir.iterdir())
        if not entries:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".rcp.sqlite3.restore-",
                dir=self.layout.data_dir,
            )
            temporary = Path(temporary_name)
            try:
                os.fchown(descriptor, self.service_uid, self.service_gid)
                os.fchmod(descriptor, RESTORE_JOURNAL_MODE)
                with candidate.open("rb") as source, os.fdopen(descriptor, "wb") as destination:
                    descriptor = -1
                    shutil.copyfileobj(source, destination, BACKUP_COPY_BUFFER_BYTES)
                    destination.flush()
                    os.fsync(destination.fileno())
                with suppress(FileExistsError):
                    os.link(temporary, target, follow_symlinks=False)
                _fsync_directory(self.layout.data_dir)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                temporary.unlink(missing_ok=True)
        _require_restored_target(current, target, uid=self.service_uid, gid=self.service_gid)
        updated = current.model_copy(
            update={
                "phase": "sqlite_restored",
                "restored_sqlite_sha256": current.candidate_sqlite_sha256,
                "updated_at": self.clock(),
            }
        )
        write_restore_journal(updated, self.layout, uid=self.service_uid, gid=self.service_gid)
        return read_restore_journal(self.layout, expected_uid=self.service_uid)

    def verify_offline_candidate(
        self,
        journal: RestoreOperationJournal,
    ) -> RestoreOperationJournal:
        current = read_restore_journal(self.layout, expected_uid=self.service_uid)
        if current.operation_id != journal.operation_id or current.phase == "archive_verified":
            raise RestoreRefused("The restore journal has not reached the offline SQLite boundary.")
        target = self.layout.data_dir / "rcp.sqlite3"
        _require_restored_target(current, target, uid=self.service_uid, gid=self.service_gid)
        _verify_sqlite_integrity(target)
        try:
            self.service_control.fence_stopped_disabled()
        except InstalledServiceControlRefused as exc:
            raise RestoreRefused(
                "The candidate is restored, but RCP could not prove systemd remains stopped."
            ) from exc
        return current

    def recover_checkouts(
        self,
        journal: RestoreOperationJournal,
        *,
        resume_argv: tuple[str, ...],
        step_number: int,
    ) -> RestoreCheckoutRecoveryOutcome:
        """Reconstruct every captured checkout, pausing only for explicit GitHub work."""

        with self.admission():
            current = self._current_restore(journal)
            if current.phase in {
                "checkouts_reconstructed",
                "projects_publishing",
                "projects_published",
            }:
                return RestoreCheckoutRecoveryOutcome(journal=current)
            if current.phase == "sqlite_restored":
                current = self._write_restore_phase(current, "checkouts_recovering")
            if current.phase not in {
                "checkouts_recovering",
                "checkouts_ready",
                "projects_rebinding",
            }:
                raise RestoreRefused("The restore journal is outside checkout recovery.")

            for capture in self._captured_projects(current):
                assert capture.recovery is not None
                machines = {item.alias: item for item in capture.recovery.machines}
                for repository in capture.recovery.repositories:
                    machine = self._recovery_machine(machines[repository.machine_alias])
                    progress = self._repository_progress(
                        current,
                        project_id=capture.project_id,
                        repository_alias=repository.alias,
                    )
                    if progress is None:
                        try:
                            self.credential_manager.preflight_recovery_key(
                                machine,
                                space_id=current.manifest.space_id,
                                project_id=capture.project_id,
                                repository_alias=repository.alias,
                            )
                        except GitCredentialRefused as exc:
                            raise RestoreRefused(str(exc)) from exc
                        progress = RestoreRepositoryRecovery(
                            project_id=capture.project_id,
                            repository_alias=repository.alias,
                            machine_alias=repository.machine_alias,
                            state="key_started",
                        )
                        current = self._write_repository_progress(current, progress)
                    try:
                        material = self.credential_manager.prepare_recovery_key(
                            machine,
                            repository.repository,
                            space_id=current.manifest.space_id,
                            project_id=capture.project_id,
                            repository_alias=repository.alias,
                        )
                    except GitCredentialRefused as exc:
                        raise RestoreRefused(
                            "The fresh restore deploy key could not be created or read back."
                        ) from exc
                    if material.public_key_fingerprint == repository.public_key_fingerprint:
                        raise RestoreRefused(
                            "The replacement checkout reused the archived deploy-key identity. "
                            "Preserve that key and repair the deterministic credential path."
                        )
                    if progress.state == "key_started":
                        progress = progress.model_copy(
                            update={
                                "state": "key_ready",
                                "deploy_key_label": material.label,
                                "deploy_public_key": material.public_key,
                                "public_key_fingerprint": material.public_key_fingerprint,
                            }
                        )
                        current = self._write_repository_progress(current, progress)
                    else:
                        self._require_same_key(progress, material)
                    if progress.state == "checkout_ready":
                        try:
                            result = self.checkout_manager.prepare_recovery(
                                machine,
                                material,
                                project_id=capture.project_id,
                                repository_alias=repository.alias,
                                state_repository=(
                                    repository.alias
                                    == capture.recovery.configuration.state_repository
                                ),
                                expected_head=progress.probed_commit,
                                retained_provisioning_commit=repository.git_commit,
                                archived_research=self._archived_research(
                                    capture,
                                    repository_alias=repository.alias,
                                ),
                            )
                        except ProjectCheckoutRefused as exc:
                            raise RestoreRefused(str(exc)) from exc
                        self._require_same_checkout(
                            progress,
                            result,
                            expected_root=machines[repository.machine_alias].resolved_central_root,
                        )
                        continue

                    try:
                        probe = self.credential_manager.probe_write(
                            machine,
                            material,
                            request_id=current.operation_id,
                        )
                    except GitCredentialRefused as exc:
                        raise RestoreRefused("The fresh restore Git write proof failed.") from exc
                    if not probe.ready:
                        if probe.status in {
                            "github_host_trust_needed",
                            "github_grant_needed",
                        }:
                            return RestoreCheckoutRecoveryOutcome(
                                journal=current,
                                operator_action=restore_deploy_key_operator_step(
                                    self.credential_manager,
                                    machine,
                                    material,
                                    number=step_number,
                                    resume_argv=resume_argv,
                                ),
                            )
                        if probe.status in {"cleanup_failed", "temporary_ref_conflict"}:
                            return RestoreCheckoutRecoveryOutcome(
                                journal=current,
                                operator_action=_restore_probe_cleanup_step(
                                    material,
                                    probe,
                                    number=step_number,
                                    resume_argv=resume_argv,
                                ),
                            )
                        if probe.status == "empty_repository":
                            raise RestoreRefused(
                                "The captured GitHub repository no longer has any commit. "
                                "Restore cannot reconstruct its archived checkout anchor."
                            )
                        raise RestoreRefused(probe.diagnostic)
                    assert probe.commit is not None
                    progress = progress.model_copy(update={"probed_commit": probe.commit})
                    current = self._write_repository_progress(current, progress)
                    try:
                        result = self.checkout_manager.prepare_recovery(
                            machine,
                            material,
                            project_id=capture.project_id,
                            repository_alias=repository.alias,
                            state_repository=(
                                repository.alias == capture.recovery.configuration.state_repository
                            ),
                            expected_head=probe.commit,
                            retained_provisioning_commit=repository.git_commit,
                            archived_research=self._archived_research(
                                capture,
                                repository_alias=repository.alias,
                            ),
                        )
                    except ProjectCheckoutRefused as exc:
                        raise RestoreRefused(str(exc)) from exc
                    if (
                        result.machine_alias != repository.machine_alias
                        or result.repository_path != repository.resolved_path
                        or result.central_root
                        != machines[repository.machine_alias].resolved_central_root
                        or result.commit != probe.commit
                    ):
                        raise RestoreRefused(
                            "The reconstructed checkout receipt differs from the archived target."
                        )
                    progress = progress.model_copy(
                        update={
                            "state": "checkout_ready",
                            "central_root": result.central_root,
                            "repository_path": result.repository_path,
                            "checkout_disposition": result.checkout_disposition,
                            "checkout_commit": result.commit,
                        }
                    )
                    current = self._write_repository_progress(current, progress)
            if current.phase == "checkouts_recovering":
                current = self._write_restore_phase(current, "checkouts_ready")
            return RestoreCheckoutRecoveryOutcome(journal=current)

    def rebind_checkouts(
        self,
        journal: RestoreOperationJournal,
    ) -> RestoreOperationJournal:
        """Regenerate locators and atomically rebind stopped restored catalog rows."""

        with self.admission():
            current = self._current_restore(journal)
            if current.phase in {
                "checkouts_reconstructed",
                "projects_publishing",
                "projects_published",
            }:
                return current
            if current.phase == "checkouts_ready":
                current = self._write_restore_phase(current, "projects_rebinding")
            if current.phase != "projects_rebinding":
                raise RestoreRefused("The restore journal has not completed checkout recovery.")
            from rcp.projects import (
                RestoredProjectRebindRefused,
                rebind_restored_project_registration,
            )

            store = AppStore(self.layout.data_dir / "rcp.sqlite3")
            rebound = {item.project_id for item in current.project_rebinds}
            for capture in self._captured_projects(current):
                paths = {
                    item.repository_alias: item.repository_path
                    for item in current.repository_recoveries
                    if item.project_id == capture.project_id
                    and item.state == "checkout_ready"
                    and item.repository_path is not None
                }
                try:
                    record = rebind_restored_project_registration(
                        store,
                        capture,
                        repository_paths=paths,
                        data_dir=self.layout.data_dir,
                        uid=self.service_uid,
                        gid=self.service_gid,
                    )
                except RestoredProjectRebindRefused as exc:
                    raise RestoreRefused(str(exc)) from exc
                receipt = RestoreProjectRebind(
                    project_id=capture.project_id,
                    locator=record.locator,
                    state_location=record.state_location,
                    state_remote=record.state_remote,
                )
                if capture.project_id not in rebound:
                    current = self._write_project_rebind(current, receipt)
                    rebound.add(capture.project_id)
                elif (
                    next(
                        item
                        for item in current.project_rebinds
                        if item.project_id == capture.project_id
                    )
                    != receipt
                ):
                    raise RestoreRefused(
                        "The restored project binding changed after its durable receipt."
                    )
            return self._write_restore_phase(current, "checkouts_reconstructed")

    def publish_projects(
        self,
        journal: RestoreOperationJournal,
    ) -> RestoreOperationJournal:
        """Publish only archived project-owned bytes, then replay and expose each project."""

        with self.admission():
            current = self._current_restore(journal)
            if current.phase == "projects_published":
                return current
            if current.phase == "checkouts_reconstructed":
                current = self._write_restore_phase(current, "projects_publishing")
            if current.phase != "projects_publishing":
                raise RestoreRefused(
                    "The restore journal has not completed checkout reconstruction."
                )
            from rcp.projects import (
                RestoredProjectPublicationRefused,
                complete_restored_project_publication,
                restored_project_owners,
            )

            store = AppStore(self.layout.data_dir / "rcp.sqlite3")
            for capture in current.manifest.projects:
                if capture.status != "uncaptured":
                    continue
                try:
                    store.mark_uncaptured_project_unavailable_for_restore(
                        capture.project_id,
                        diagnostic=capture.unavailable_reason or "project capture failed",
                    )
                except (KeyError, RuntimeError, ValueError, sqlite3.Error) as exc:
                    raise RestoreRefused(
                        "An explicitly uncaptured project could not remain visible as unavailable."
                    ) from exc

            publications = {item.project_id: item for item in current.project_publications}
            operation_projects = {
                task.operation_id: task.project_id
                for capture in self._captured_projects(current)
                for task in store.all_project_agent_tasks(capture.project_id)
            }
            for capture in self._captured_projects(current):
                entries = tuple(sorted(capture.files, key=lambda item: item.archive_path))
                manifests = [
                    item
                    for item in entries
                    if item.group == "canonical"
                    and item.source_relative_path == ".research/manifest.toml"
                ]
                if len(manifests) != 1:
                    raise RestoreRefused(
                        "A captured project does not contain exactly one canonical manifest."
                    )
                try:
                    owners = restored_project_owners(
                        store,
                        capture,
                        archived_manifest=self._project_archive_source(current, manifests[0]),
                        data_dir=self.layout.data_dir,
                    )
                    canonical = {
                        item.source_relative_path.removeprefix(".research/"): (
                            self._project_archive_source(current, item),
                            item.sha256,
                            item.size_bytes,
                        )
                        for item in entries
                        if item.group == "canonical"
                    }
                    assert capture.main_head is not None
                    materialization = owners.history.restore_canonical_history(
                        canonical,
                        expected_main_head=capture.main_head,
                        expected_branch_heads=capture.branch_heads,
                    )
                    for entry in entries:
                        source = self._project_archive_source(current, entry)
                        if entry.group == "chat":
                            owners.service.restore_canonical_chat(
                                source,
                                expected_sha256=entry.sha256,
                                expected_size=entry.size_bytes,
                                operation_projects=operation_projects,
                            )
                        elif entry.group == "paper_introduction":
                            owners.paper.restore_canonical(
                                source,
                                expected_sha256=entry.sha256,
                                expected_size=entry.size_bytes,
                            )
                        elif entry.group == "fact":
                            owners.workspace.restore_exact_file(
                                entry.source_relative_path.removeprefix(".research/"),
                                source,
                                expected_sha256=entry.sha256,
                                expected_size=entry.size_bytes,
                            )
                        elif entry.group == "kept_artifact":
                            owners.workspace.restore_kept_artifact(
                                PurePosixPath(entry.source_relative_path).name,
                                source,
                                expected_sha256=entry.sha256,
                                expected_size=entry.size_bytes,
                            )
                        elif entry.group == "legacy_kept_result_view":
                            owners.workspace.restore_kept_result_view(
                                PurePosixPath(entry.source_relative_path).name,
                                source,
                                expected_sha256=entry.sha256,
                                expected_size=entry.size_bytes,
                            )
                    if owners.workspace.remote:
                        owners.workspace.refresh()
                    self._verify_project_publication_bytes(owners.workspace, entries)
                    complete_restored_project_publication(
                        store,
                        capture,
                        owners,
                        materialization,
                    )
                except (
                    OSError,
                    RestoredProjectPublicationRefused,
                    RuntimeError,
                    ValueError,
                ) as exc:
                    raise RestoreRefused(
                        f"Restored project {capture.project_id} failed canonical publication "
                        "or replay verification."
                    ) from exc
                receipt = self._project_publication_receipt(capture)
                existing = publications.get(capture.project_id)
                if existing is not None and existing != receipt:
                    raise RestoreRefused(
                        "A restored project publication changed after its durable receipt."
                    )
                if existing is None:
                    current = self._write_project_publication(current, receipt)
                    publications[capture.project_id] = receipt
            return self._write_restore_phase(current, "projects_published")

    def _current_restore(
        self,
        journal: RestoreOperationJournal,
    ) -> RestoreOperationJournal:
        current = read_restore_journal(self.layout, expected_uid=self.service_uid)
        if current.operation_id != journal.operation_id:
            raise RestoreRefused("The durable restore operation changed during recovery.")
        return current

    def _write_restore_phase(
        self,
        journal: RestoreOperationJournal,
        phase: Literal[
            "checkouts_recovering",
            "checkouts_ready",
            "projects_rebinding",
            "checkouts_reconstructed",
            "projects_publishing",
            "projects_published",
        ],
    ) -> RestoreOperationJournal:
        updated = journal.model_copy(update={"phase": phase, "updated_at": self.clock()})
        write_restore_journal(
            updated,
            self.layout,
            uid=self.service_uid,
            gid=self.service_gid,
        )
        return read_restore_journal(self.layout, expected_uid=self.service_uid)

    def _write_project_publication(
        self,
        journal: RestoreOperationJournal,
        receipt: RestoreProjectPublication,
    ) -> RestoreOperationJournal:
        items = {item.project_id: item for item in journal.project_publications}
        items[receipt.project_id] = receipt
        updated = journal.model_copy(
            update={
                "project_publications": tuple(items[key] for key in sorted(items)),
                "updated_at": self.clock(),
            }
        )
        write_restore_journal(
            updated,
            self.layout,
            uid=self.service_uid,
            gid=self.service_gid,
        )
        return read_restore_journal(self.layout, expected_uid=self.service_uid)

    def _write_repository_progress(
        self,
        journal: RestoreOperationJournal,
        progress: RestoreRepositoryRecovery,
    ) -> RestoreOperationJournal:
        items = {
            (item.project_id, item.repository_alias): item for item in journal.repository_recoveries
        }
        items[(progress.project_id, progress.repository_alias)] = progress
        updated = journal.model_copy(
            update={
                "repository_recoveries": tuple(items[key] for key in sorted(items)),
                "updated_at": self.clock(),
            }
        )
        write_restore_journal(
            updated,
            self.layout,
            uid=self.service_uid,
            gid=self.service_gid,
        )
        return read_restore_journal(self.layout, expected_uid=self.service_uid)

    def _write_project_rebind(
        self,
        journal: RestoreOperationJournal,
        receipt: RestoreProjectRebind,
    ) -> RestoreOperationJournal:
        items = {item.project_id: item for item in journal.project_rebinds}
        items[receipt.project_id] = receipt
        updated = journal.model_copy(
            update={
                "project_rebinds": tuple(items[key] for key in sorted(items)),
                "updated_at": self.clock(),
            }
        )
        write_restore_journal(
            updated,
            self.layout,
            uid=self.service_uid,
            gid=self.service_gid,
        )
        return read_restore_journal(self.layout, expected_uid=self.service_uid)

    @staticmethod
    def _captured_projects(
        journal: RestoreOperationJournal,
    ) -> tuple:
        return tuple(
            sorted(
                (item for item in journal.manifest.projects if item.status == "captured"),
                key=lambda item: item.project_id,
            )
        )

    @staticmethod
    def _repository_progress(
        journal: RestoreOperationJournal,
        *,
        project_id: str,
        repository_alias: str,
    ) -> RestoreRepositoryRecovery | None:
        return next(
            (
                item
                for item in journal.repository_recoveries
                if item.project_id == project_id and item.repository_alias == repository_alias
            ),
            None,
        )

    def _recovery_machine(self, archived) -> ProjectProvisioningMachineIntent:
        if archived.location == "local":
            if (
                archived.os_account != self.layout.service_account
                or archived.resolved_central_root != str(self.layout.projects_root)
            ):
                raise RestoreRefused(
                    "The archived local checkout target differs from this installation."
                )
            # The production layout is fixed; model_construct keeps injected test
            # layouts usable after the exact archived/install comparison above.
            return ProjectProvisioningMachineIntent.model_construct(
                alias=archived.alias,
                location="local",
                host="",
                os_account=archived.os_account,
                central_root=archived.resolved_central_root,
            )
        return ProjectProvisioningMachineIntent(
            alias=archived.alias,
            location="ssh",
            host=archived.host,
            os_account=archived.os_account,
            central_root=archived.resolved_central_root,
        )

    @staticmethod
    def _require_same_key(
        progress: RestoreRepositoryRecovery,
        material: DeployKeyMaterial,
    ) -> None:
        if (
            progress.deploy_key_label != material.label
            or progress.deploy_public_key != material.public_key
            or progress.public_key_fingerprint != material.public_key_fingerprint
        ):
            raise RestoreRefused("The fresh restore deploy key changed after its durable receipt.")

    @staticmethod
    def _archived_research(capture, *, repository_alias: str) -> dict[str, tuple[str, int]]:
        assert capture.recovery is not None
        if repository_alias != capture.recovery.configuration.state_repository:
            return {}
        return {
            item.source_relative_path: (item.sha256, item.size_bytes)
            for item in capture.files
            if item.source_relative_path.startswith(".research/")
        }

    @staticmethod
    def _project_archive_source(journal: RestoreOperationJournal, entry) -> Path:
        root = Path(journal.candidate_root) / "payload"
        source = root.joinpath(*PurePosixPath(entry.archive_path).parts)
        if not source.is_relative_to(root):
            raise RestoreRefused("A restored project archive path escaped its candidate root.")
        try:
            metadata = source.lstat()
        except OSError as exc:
            raise RestoreRefused("A restored project archive file is unavailable.") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise RestoreRefused("A restored project archive entry is not a regular file.")
        return source

    @staticmethod
    def _project_publication_receipt(capture) -> RestoreProjectPublication:
        payload = json.dumps(
            capture.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        assert capture.main_head is not None
        return RestoreProjectPublication(
            project_id=capture.project_id,
            capture_sha256=hashlib.sha256(payload).hexdigest(),
            published_files=len(capture.files),
            published_bytes=capture.total_bytes,
            main_revision=capture.main_head.revision,
            branch_count=len(capture.branch_heads),
        )

    @staticmethod
    def _verify_project_publication_bytes(workspace, entries) -> None:
        for entry in entries:
            relative = PurePosixPath(entry.source_relative_path)
            if entry.group in {"kept_artifact", "legacy_kept_result_view"}:
                if entry.group == "kept_artifact":
                    data = workspace.read_kept_artifact(
                        relative.name,
                        max_bytes=max(1, entry.size_bytes),
                    )
                else:
                    data = workspace.read_kept_result_view(
                        relative.name,
                        max_bytes=max(1, entry.size_bytes),
                    )
                observed = (hashlib.sha256(data).hexdigest(), len(data))
            else:
                if relative.parts[0] != ".research":
                    raise RestoreRefused("A canonical project byte has an invalid restore path.")
                destination = workspace.root.joinpath(*relative.parts[1:])
                observed = _hash_regular_file(destination)
            if observed != (entry.sha256, entry.size_bytes):
                raise RestoreRefused(
                    "A restored project file changed before final publication readback."
                )

    @staticmethod
    def _require_same_checkout(progress, result, *, expected_root: str) -> None:
        if (
            result.machine_alias != progress.machine_alias
            or result.repository_alias != progress.repository_alias
            or result.repository_path != progress.repository_path
            or result.central_root != expected_root
            or result.central_root != progress.central_root
            or result.commit != progress.checkout_commit
            or result.checkout_disposition != progress.checkout_disposition
        ):
            raise RestoreRefused(
                "The reconstructed checkout changed after its durable recovery receipt."
            )

    def _current_release(self) -> Path:
        try:
            return self.service_control.current_release()
        except InstalledServiceControlRefused as exc:
            raise RestoreRefused(
                "The installed current release pointer is unsafe or unavailable."
            ) from exc

    def _prepare_candidate_root(self, root: Path) -> None:
        _require_private_directory(
            self.layout.restore_operations_root,
            uid=self.service_uid,
            gid=self.service_gid,
            label="restore operations root",
        )
        if root.exists() or root.is_symlink():
            _require_private_directory(
                root,
                uid=self.service_uid,
                gid=self.service_gid,
                label="restore candidate root",
            )
            shutil.rmtree(root)
            _fsync_directory(root.parent)
        root.mkdir(mode=RESTORE_DIRECTORY_MODE)
        os.chown(root, self.service_uid, self.service_gid)
        _fsync_directory(root.parent)

    def _decrypt_archive(self, archive: Path, identity: Path, plaintext: Path) -> str:
        _require_protected_identity(identity, root_uid=self.root_uid)
        try:
            require_age_1x(self.age_executable)
        except Exception as exc:
            raise RestoreRefused(
                "The installed upstream age CLI is not a supported 1.x release."
            ) from exc
        environment = {
            "HOME": str(self.layout.service_home),
            "USER": self.layout.service_account,
            "LOGNAME": self.layout.service_account,
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
        }
        age_path = Path(self.age_executable)
        age_keygen = (
            str(age_path.with_name("age-keygen")) if age_path.parent != Path(".") else "age-keygen"
        )
        try:
            recipient = subprocess.run(
                (age_keygen, "-y", str(identity)),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=environment,
                timeout=30.0,
                check=False,
            )
            completed = subprocess.run(
                (
                    self.age_executable,
                    "--decrypt",
                    "--identity",
                    str(identity),
                    "--output",
                    str(plaintext),
                    str(archive),
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                timeout=SERVER_RESTORE_DECRYPT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RestoreRefused("age could not decrypt the supplied archive safely.") from exc
        try:
            public_recipient = validate_age_recipient(recipient.stdout.strip())
        except ValueError as exc:
            raise RestoreRefused(
                "The supplied recovery identity is not one supported native X25519 age key."
            ) from exc
        if recipient.returncode != 0:
            raise RestoreRefused(
                "The supplied recovery identity is not one supported native X25519 age key."
            )
        if completed.returncode != 0:
            raise RestoreRefused(
                "age could not decrypt the supplied archive with this protected identity."
            )
        info = plaintext.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise RestoreRefused("age did not produce one regular plaintext archive.")
        os.chown(plaintext, self.service_uid, self.service_gid)
        os.chmod(plaintext, RESTORE_JOURNAL_MODE)
        return hashlib.sha256(public_recipient.encode("ascii")).hexdigest()

    def _detach_candidate(self, database: Path, confirmed_by: str, detached_at: datetime) -> None:
        if os.geteuid() == self.service_uid:
            detach_restore_database(database, confirmed_by=confirmed_by, detached_at=detached_at)
            return
        account = pwd.getpwuid(self.service_uid)
        python = self._current_release() / ".venv" / "bin" / "python"
        environment = (
            f"HOME={account.pw_dir}",
            f"USER={account.pw_name}",
            f"LOGNAME={account.pw_name}",
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG=C.UTF-8",
        )
        completed = subprocess.run(
            (
                "runuser",
                "--user",
                account.pw_name,
                "--",
                "env",
                "-i",
                *environment,
                str(python),
                "-m",
                "rcp.server_ops.restore",
                "--detach-sqlite",
                str(database),
                "--confirmed-by",
                confirmed_by,
                "--detached-at",
                detached_at.isoformat(),
            ),
            cwd=self._current_release(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=SERVER_RESTORE_DECRYPT_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode != 0:
            raise RestoreRefused(
                "The service-account restore worker could not migrate and detach the candidate."
            )


def detach_restore_database(
    database: Path,
    *,
    confirmed_by: str,
    detached_at: datetime,
) -> None:
    store = AppStore(database)
    store.detach_restored_lifecycle(
        diagnostic=(
            "This operation was captured by a replacement-server archive and cannot resume on "
            "the restored machine."
        ),
        confirmed_by=confirmed_by,
        detached_at=detached_at.isoformat(),
    )
    _verify_sqlite_integrity(database)


def restore_journal_path(layout: ServerLayout = DEFAULT_SERVER_LAYOUT) -> Path:
    return layout.restore_operations_root / RESTORE_JOURNAL_NAME


def read_restore_journal_if_present(
    layout: ServerLayout = DEFAULT_SERVER_LAYOUT,
    *,
    expected_uid: int,
) -> RestoreOperationJournal | None:
    path = restore_journal_path(layout)
    if not path.exists() and not path.is_symlink():
        return None
    return read_restore_journal(layout, expected_uid=expected_uid)


def read_restore_journal(
    layout: ServerLayout = DEFAULT_SERVER_LAYOUT,
    *,
    expected_uid: int,
) -> RestoreOperationJournal:
    path = restore_journal_path(layout)
    payload = _read_private_file(path, expected_uid=expected_uid, maximum=BACKUP_RECEIPT_MAX_BYTES)
    try:
        journal = RestoreOperationJournal.model_validate_json(payload)
    except ValueError as exc:
        raise RestoreRefused("The unfinished restore journal is invalid or unsupported.") from exc
    if (
        journal.configured_data_dir != str(layout.data_dir)
        or Path(journal.candidate_root).parent != layout.restore_operations_root
    ):
        raise RestoreRefused("The unfinished restore journal names another server layout.")
    return journal


def write_restore_journal(
    journal: RestoreOperationJournal,
    layout: ServerLayout = DEFAULT_SERVER_LAYOUT,
    *,
    uid: int,
    gid: int,
) -> None:
    path = restore_journal_path(layout)
    payload = (
        json.dumps(
            journal.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) > BACKUP_RECEIPT_MAX_BYTES:
        raise RestoreRefused("The restore journal exceeds its fixed size bound.")
    if path.exists() or path.is_symlink():
        _read_private_file(path, expected_uid=uid, maximum=BACKUP_RECEIPT_MAX_BYTES)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, RESTORE_JOURNAL_MODE)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def unfinished_restore_operation(
    layout: ServerLayout = DEFAULT_SERVER_LAYOUT,
    *,
    expected_uid: int,
) -> RestoreOperationJournal | None:
    root = layout.restore_operations_root
    try:
        root_gid = root.lstat().st_gid
    except OSError as exc:
        raise RestoreRefused("The restore operations root is unavailable.") from exc
    _require_private_directory(
        root,
        uid=expected_uid,
        gid=root_gid,
        label="restore operations root",
    )
    entries = tuple(root.iterdir())
    if not entries:
        return None
    journal = read_restore_journal_if_present(layout, expected_uid=expected_uid)
    if journal is None:
        raise RestoreRefused(
            "Restore machine state exists without its journal; preserve and inspect it before "
            "running another server operation."
        )
    allowed = {restore_journal_path(layout), Path(journal.candidate_root)}
    if set(entries) != allowed:
        raise RestoreRefused("The restore operations root contains unknown machine state.")
    _require_private_directory(
        Path(journal.candidate_root),
        uid=expected_uid,
        gid=Path(journal.candidate_root).stat().st_gid,
        label="restore candidate root",
    )
    return journal


def _extract_verified_archive(
    plaintext: Path,
    payload_root: Path,
    *,
    expected_uid: int,
    expected_gid: int,
) -> tuple[BackupArchiveManifest, str]:
    payload_root.mkdir(mode=RESTORE_DIRECTORY_MODE)
    os.chown(payload_root, expected_uid, expected_gid)
    try:
        with tarfile.open(plaintext, mode="r:") as archive:
            members = archive.getmembers()
            if not members or members[0].name != "manifest.json":
                raise RestoreRefused("The restore archive does not begin with its manifest.")
            manifest_member = members[0]
            _require_regular_archive_member(manifest_member)
            if manifest_member.size > BACKUP_RECEIPT_MAX_BYTES:
                raise RestoreRefused("The restore archive manifest exceeds its size bound.")
            manifest_stream = archive.extractfile(manifest_member)
            if manifest_stream is None:
                raise RestoreRefused("The restore archive manifest cannot be read.")
            manifest_bytes = manifest_stream.read(BACKUP_RECEIPT_MAX_BYTES + 1)
            try:
                manifest = BackupArchiveManifest.model_validate_json(manifest_bytes)
            except ValueError as exc:
                raise RestoreRefused(
                    "The restore archive manifest is invalid or unsupported."
                ) from exc
            canonical_manifest = _manifest_bytes(manifest)
            if manifest_bytes != canonical_manifest:
                raise RestoreRefused("The restore archive manifest is not canonical.")
            manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
            entries = {
                entry.archive_path: entry
                for entry in (
                    manifest.sqlite_snapshot,
                    *(item for project in manifest.projects for item in project.files),
                )
            }
            if len(entries) + 1 != len(members):
                raise RestoreRefused(
                    "The restore archive member inventory differs from its manifest."
                )
            seen: set[str] = set()
            for member in members[1:]:
                _require_regular_archive_member(member)
                entry = entries.get(member.name)
                if entry is None or member.name in seen or member.size != entry.size_bytes:
                    raise RestoreRefused(
                        "The restore archive contains an undeclared, repeated, or resized member."
                    )
                destination = payload_root.joinpath(*PurePosixPath(member.name).parts)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=RESTORE_DIRECTORY_MODE)
                _chown_tree_parents(destination.parent, payload_root, expected_uid, expected_gid)
                descriptor = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o400,
                )
                digest = hashlib.sha256()
                size = 0
                source = archive.extractfile(member)
                if source is None:
                    os.close(descriptor)
                    raise RestoreRefused("A declared restore archive member cannot be read.")
                try:
                    while True:
                        chunk = source.read(BACKUP_COPY_BUFFER_BYTES)
                        if not chunk:
                            break
                        digest.update(chunk)
                        size += len(chunk)
                        view = memoryview(chunk)
                        while view:
                            written = os.write(descriptor, view)
                            if written <= 0:
                                raise OSError("short restore member write")
                            view = view[written:]
                    os.fchown(descriptor, expected_uid, expected_gid)
                    os.fchmod(descriptor, 0o400)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                if size != entry.size_bytes or digest.hexdigest() != entry.sha256:
                    raise RestoreRefused(
                        "A restore archive member does not match its declared hash."
                    )
                seen.add(member.name)
            if seen != set(entries):
                raise RestoreRefused("The restore archive omits declared files.")
    except RestoreRefused:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise RestoreRefused("The decrypted restore archive is unsafe or unreadable.") from exc
    _fsync_tree(payload_root)
    return manifest, manifest_sha256


def _require_regular_archive_member(member: tarfile.TarInfo) -> None:
    path = PurePosixPath(member.name)
    if (
        not member.isreg()
        or path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or member.mode != 0o400
        or member.uid != 0
        or member.gid != 0
        or member.mtime != 0
    ):
        raise RestoreRefused("The restore archive contains an unsafe or unsupported member.")


def _manifest_bytes(manifest: BackupArchiveManifest) -> bytes:
    return (
        json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _database_schema_sha256(path: Path) -> str:
    uri = f"{path.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_schema
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name, tbl_name
            """
        ).fetchall()
    payload = [dict(row) for row in rows]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _verify_sqlite_integrity(path: Path) -> None:
    uri = f"{path.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RestoreRefused("The restored SQLite candidate failed integrity_check.")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RestoreRefused("The restored SQLite candidate failed foreign_key_check.")


def _git_commit_is_supported(archive_commit: str, current_commit: str, release: Path) -> bool:
    if (
        len(archive_commit) != 40
        or len(current_commit) != 40
        or any(character not in _FULL_COMMIT for character in archive_commit + current_commit)
    ):
        return False
    completed = subprocess.run(
        ("git", "-C", str(release), "merge-base", "--is-ancestor", archive_commit, current_commit),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30.0,
        check=False,
    )
    return completed.returncode == 0


def _require_same_candidate(
    journal: RestoreOperationJournal,
    candidate: RestoreCandidate,
    confirmation: RestoreConfirmation,
) -> None:
    if (
        journal.archive_path != str(candidate.archive_path)
        or journal.archive_sha256 != candidate.archive_sha256
        or journal.archive_size_bytes != candidate.archive_size_bytes
        or journal.manifest_sha256 != candidate.manifest_sha256
        or journal.candidate_sqlite_sha256 != candidate.sqlite_sha256
        or journal.confirmation.confirmed_data_dir != confirmation.confirmed_data_dir
    ):
        raise RestoreRefused("The re-entered restore differs from its durable journal.")


def _candidate_matches_journal(journal: RestoreOperationJournal, *, expected_uid: int) -> bool:
    try:
        digest, _ = _hash_regular_file(
            Path(journal.candidate_sqlite_path), expected_uid=expected_uid
        )
    except (OSError, RestoreRefused):
        return False
    return digest == journal.candidate_sqlite_sha256


def _require_restored_target(
    journal: RestoreOperationJournal,
    target: Path,
    *,
    uid: int,
    gid: int,
) -> None:
    entries = tuple(target.parent.iterdir())
    if entries != (target,):
        raise RestoreRefused("The configured data directory contains unexpected restore state.")
    info = target.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or (info.st_uid, info.st_gid) != (uid, gid)
        or stat.S_IMODE(info.st_mode) != RESTORE_JOURNAL_MODE
    ):
        raise RestoreRefused("The restored database has unsafe ownership or mode.")
    digest, _ = _hash_regular_file(target, expected_uid=uid)
    if digest != journal.candidate_sqlite_sha256:
        raise RestoreRefused("The restored database differs from the journaled candidate.")


def _require_protected_identity(path: Path, *, root_uid: int) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise RestoreRefused("The supplied recovery identity file is unavailable.") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in {root_uid, os.geteuid()}
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise RestoreRefused(
            "The recovery identity must be a private regular file owned by the invoking root "
            "operator."
        )


def _hash_regular_file(path: Path, *, expected_uid: int | None = None) -> tuple[str, int]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    size = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or (
            expected_uid is not None and before.st_uid != expected_uid
        ):
            raise RestoreRefused("A restore input is not one owned regular file.")
        while True:
            chunk = os.read(descriptor, BACKUP_COPY_BUFFER_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RestoreRefused("A restore input changed while it was being verified.")
    finally:
        os.close(descriptor)
    return digest.hexdigest(), size


def _read_private_file(path: Path, *, expected_uid: int, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != expected_uid
            or stat.S_IMODE(info.st_mode) != RESTORE_JOURNAL_MODE
            or info.st_size > maximum
        ):
            raise RestoreRefused("A restore machine record has unsafe ownership, mode, or size.")
        chunks: list[bytes] = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(descriptor, min(BACKUP_COPY_BUFFER_BYTES, remaining))
            if not chunk:
                raise RestoreRefused("A restore machine record is incomplete.")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _require_private_directory(path: Path, *, uid: int, gid: int, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise RestoreRefused(f"The {label} is unavailable.") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or (info.st_uid, info.st_gid) != (uid, gid)
        or stat.S_IMODE(info.st_mode) != RESTORE_DIRECTORY_MODE
    ):
        raise RestoreRefused(f"The {label} has unsafe ownership or mode.")


def _require_private_empty_directory(path: Path, *, uid: int, gid: int, label: str) -> None:
    _require_private_directory(path, uid=uid, gid=gid, label=label)
    if any(path.iterdir()):
        raise RestoreRefused(
            "The configured RCP data directory is not fresh and empty. Restore will not replace "
            "or adopt an initialized space."
        )


def _chown_tree_parents(path: Path, root: Path, uid: int, gid: int) -> None:
    current = path
    while current != root:
        os.chown(current, uid, gid)
        os.chmod(current, RESTORE_DIRECTORY_MODE)
        current = current.parent


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_tree(root: Path) -> None:
    directories = [root]
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            directories.append(path)
        elif path.is_file():
            _fsync_file(path)
    for directory in reversed(directories):
        _fsync_directory(directory)


def _worker_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--detach-sqlite", type=Path)
    parser.add_argument("--confirmed-by")
    parser.add_argument("--detached-at")
    return parser


def _main(argv: list[str] | None = None) -> int:
    arguments = _worker_parser().parse_args(argv)
    if arguments.detach_sqlite is None or not arguments.confirmed_by or not arguments.detached_at:
        return 2
    try:
        detached_at = datetime.fromisoformat(arguments.detached_at)
        detach_restore_database(
            arguments.detach_sqlite,
            confirmed_by=arguments.confirmed_by,
            detached_at=detached_at,
        )
    except Exception:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the service-account worker
    raise SystemExit(_main())


__all__ = [
    "LinuxRestoreMachine",
    "RESTORE_ARCHIVE_FORMAT",
    "RESTORE_JOURNAL_NAME",
    "RestoreCandidate",
    "RestoreConfirmation",
    "RestoreOperationJournal",
    "RestoreRefused",
    "SUPPORTED_RESTORE_DATABASE_SCHEMAS",
    "detach_restore_database",
    "prepare_restore_command",
    "read_restore_journal",
    "read_restore_journal_if_present",
    "restore_journal_path",
    "unfinished_restore_operation",
    "write_restore_journal",
]
