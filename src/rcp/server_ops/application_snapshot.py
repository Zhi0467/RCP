"""Typed application payload selection; release and recovery transitions belong elsewhere."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rcp.limits import (
    BACKUP_COPY_BUFFER_BYTES,
)
from rcp.server_ops._local_primitives import (
    fsync_directory as _fsync_directory,
)
from rcp.server_ops._local_primitives import (
    write_all as _write_all,
)
from rcp.server_ops.backup_capture import BackupSnapshotProjectInventory
from rcp.server_ops.backup_models import (
    BackupProjectCapture,
)
from rcp.server_ops.backup_project_files import (
    BackupProjectFileCaptureReceipt,
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_DIRECTORY_MODE = 0o700
_PAYLOAD_FILE_MODE = 0o400


class ApplicationSnapshotRefused(RuntimeError):
    """Application proof material was incomplete or changed."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
    )


def _relative_path(value: str, *, label: str) -> str:
    if (
        not value
        or value != value.strip()
        or "\\" in value
        or len(value.encode("utf-8")) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} must be one bounded relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError(f"{label} must be normalized and relative")
    return value


class ApplicationSnapshotFile(_StrictModel):
    relative_path: str
    sha256: str
    size_bytes: int = Field(ge=0)
    mode: int = Field(ge=0, le=0o777)

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        return _relative_path(value, label="proof file path")

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("proof files require lowercase SHA-256")
        return value


class ApplicationSnapshotRoot(_StrictModel):
    project_id: str
    live_path: str
    archive_path: str
    files: tuple[ApplicationSnapshotFile, ...]


def _project_restore_location(
    project: BackupProjectCapture | BackupSnapshotProjectInventory,
) -> tuple[Literal["local_research", "remote_excluded"], Path | None]:
    assert project.recovery is not None
    configuration = project.recovery.configuration
    repositories = {item.alias: item for item in project.recovery.repositories}
    machines = {item.alias: item for item in project.recovery.machines}
    state_repository = repositories[configuration.state_repository]
    machine = machines[state_repository.machine_alias]
    if machine.location == "ssh":
        return "remote_excluded", None
    repository_root = Path(state_repository.resolved_path)
    research_root = repository_root / ".research"
    if project.locator != str(research_root / "manifest.toml"):
        raise ApplicationSnapshotRefused(
            "A local project's catalog locator differs from its reviewed central checkout."
        )
    return "local_research", research_root


def _strip_research_prefix(value: str) -> str:
    path = PurePosixPath(value)
    if len(path.parts) < 2 or path.parts[0] != ".research":
        raise ValueError("project research proof file escaped .research")
    return PurePosixPath(*path.parts[1:]).as_posix()


def _set_private_directory_modes(root: Path) -> None:
    """Keep the application's generated proof directories private."""
    for current, _directories, _files in os.walk(root, followlinks=False):
        Path(current).chmod(_DIRECTORY_MODE)


def copy_proof_tree(source: Path, destination: Path) -> None:
    """Copy generated application proof material; the supervisor owns live snapshots."""
    shutil.copytree(source, destination, symlinks=True)


def _copy_declared_file(
    source: Path,
    destination: Path,
    *,
    relative_path: str,
    expected_sha256: str,
    expected_size: int | None,
    restore_mode: int,
) -> ApplicationSnapshotFile:
    copied = _copy_stable_file(
        source,
        destination,
        relative_path=relative_path,
        restore_mode=restore_mode,
    )
    if copied.sha256 != expected_sha256 or (
        expected_size is not None and copied.size_bytes != expected_size
    ):
        raise ApplicationSnapshotRefused("A declared capture file differs from its receipt.")
    return copied


def _copy_stable_file(
    source: Path,
    destination: Path,
    *,
    relative_path: str,
    restore_mode: int,
) -> ApplicationSnapshotFile:
    try:
        initial = source.lstat()
    except OSError as exc:
        raise ApplicationSnapshotRefused(
            "An application proof source file is unavailable."
        ) from exc
    if not stat.S_ISREG(initial.st_mode) or source.is_symlink():
        raise ApplicationSnapshotRefused("An application proof source is not an ordinary file.")
    destination.parent.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
    source_descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    destination_descriptor = -1
    digest = hashlib.sha256()
    size = 0
    try:
        opened = os.fstat(source_descriptor)
        if (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino):
            raise ApplicationSnapshotRefused(
                "An application proof source changed while it was opened."
            )
        destination_descriptor = os.open(
            destination,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            _PAYLOAD_FILE_MODE,
        )
        while True:
            chunk = os.read(source_descriptor, BACKUP_COPY_BUFFER_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            _write_all(destination_descriptor, chunk)
        os.fchmod(destination_descriptor, _PAYLOAD_FILE_MODE)
        os.fsync(destination_descriptor)
        final = os.fstat(source_descriptor)
        path_final = source.lstat()
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(initial, name) != getattr(final, name) for name in stable) or any(
            getattr(final, name) != getattr(path_final, name) for name in stable
        ):
            raise ApplicationSnapshotRefused("An application proof source changed during copying.")
        if size != final.st_size:
            raise ApplicationSnapshotRefused("An application proof file copy is incomplete.")
    finally:
        os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
    _fsync_directory(destination.parent)
    return ApplicationSnapshotFile(
        relative_path=relative_path,
        sha256=digest.hexdigest(),
        size_bytes=size,
        mode=restore_mode,
    )


def copy_project_roots(
    operation_root: Path,
    receipt: BackupProjectFileCaptureReceipt,
    *,
    capture_root: Path,
) -> tuple[ApplicationSnapshotRoot, ...]:
    roots: list[ApplicationSnapshotRoot] = []
    for project in receipt.projects:
        if project.status == "uncaptured":
            continue
        archive_root = PurePosixPath("payload/project-files") / project.project_id
        destination_root = operation_root.joinpath(*archive_root.parts)
        destination_root.mkdir(mode=_DIRECTORY_MODE, parents=True)
        copied_files: list[ApplicationSnapshotFile] = []
        for entry in project.files:
            source = capture_root.joinpath(*PurePosixPath(entry.archive_path).parts)
            relative = PurePosixPath(entry.source_relative_path)
            copied_files.append(
                _copy_declared_file(
                    source,
                    destination_root.joinpath(*relative.parts),
                    relative_path=relative.as_posix(),
                    expected_sha256=entry.sha256,
                    expected_size=entry.size_bytes,
                    restore_mode=0o600,
                )
            )
        _kind, research_root = _project_restore_location(project)
        if research_root is None:
            continue
        research_files = tuple(
            item.model_copy(update={"relative_path": _strip_research_prefix(item.relative_path)})
            for item in copied_files
            if PurePosixPath(item.relative_path).parts[0] == ".research"
        )
        roots.append(
            ApplicationSnapshotRoot(
                project_id=project.project_id,
                live_path=str(research_root),
                archive_path=(archive_root / ".research").as_posix(),
                files=research_files,
            )
        )
    return tuple(sorted(roots, key=lambda item: item.project_id))
