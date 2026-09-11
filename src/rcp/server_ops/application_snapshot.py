"""Typed application payload selection; release and recovery transitions belong elsewhere."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rcp.artifacts import AgentArtifactDescriptor, recover_local_regular_file_replacement
from rcp.config import load_manifest
from rcp.limits import (
    BACKUP_COPY_BUFFER_BYTES,
    BACKUP_INVENTORY_MAX_ENTRIES,
)
from rcp.runs.chat import _local_chat_artifact_directory, _logical_chat_turn_operation_id
from rcp.server_ops._local_primitives import (
    fsync_directory as _fsync_directory,
)
from rcp.server_ops._local_primitives import (
    write_all as _write_all,
)
from rcp.server_ops.backup_models import (
    BackupProjectCapture,
)
from rcp.server_ops.backup_project_files import (
    BackupProjectFileCaptureReceipt,
)
from rcp.sources.imported import (
    ImportedProviderSourceInventory,
    ImportedProviderSourceStore,
)
from rcp.storage import AppStore
from rcp.storage.models import ProjectTransferUploadRecord
from rcp.transfer.target import target_transfer_archive_path
from rcp.transport.state import LocalStateWorkspace, state_workspace_for_probe

_SHA256 = re.compile(r"[0-9a-f]{64}")
_DIRECTORY_MODE = 0o700
_PAYLOAD_FILE_MODE = 0o400


class ApplicationSnapshotRefused(RuntimeError):
    """The final local rollback boundary was incomplete or unsafe."""


def _settle_accepting_artifact_replacements(
    store: AppStore,
    data_dir: Path,
    projects: BackupProjectFileCaptureReceipt,
) -> None:
    """Resolve local acceptance journals before paths are copied into a checkpoint."""

    captured_projects = {project.project_id: project for project in projects.projects}
    try:
        for candidate in store.accepting_artifact_revision_candidates():
            source = store.agent_task(candidate.source_operation_id)
            if source is None or not source.result:
                raise ValueError("an accepting artifact revision lost its source task")
            raw_artifacts = source.result.get("artifacts")
            if not isinstance(raw_artifacts, list):
                raise ValueError("an accepting artifact revision lost its source artifact")
            descriptor = next(
                (
                    item
                    for raw in raw_artifacts
                    if (item := AgentArtifactDescriptor.model_validate(raw)).artifact_id
                    == candidate.source_artifact_id
                ),
                None,
            )
            if descriptor is None:
                raise ValueError("an accepting artifact revision lost its source artifact")
            if descriptor.kept_filename is not None:
                captured = captured_projects.get(candidate.project_id)
                if captured is None or captured.status != "captured" or captured.locator is None:
                    raise ValueError("an accepting kept artifact lacks a local checkpoint route")
                workspace = state_workspace_for_probe(load_manifest(captured.locator), data_dir)
                if isinstance(workspace, LocalStateWorkspace):
                    workspace.recover_kept_artifact_replacement(descriptor.kept_filename)
                continue
            if candidate.stage_host:
                continue
            if not source.stage_root:
                raise ValueError("an accepting artifact revision lost its local stage")
            source_scope_id = _logical_chat_turn_operation_id(store, source.operation_id)
            recovery_key = hashlib.sha256(
                f"{source.stage_root}\0{source_scope_id}".encode()
            ).hexdigest()[:32]
            recover_local_regular_file_replacement(
                _local_chat_artifact_directory(store, source, source_scope_id),
                descriptor.name,
                recovery_directory=(
                    Path(source.stage_root) / "inputs" / ".artifact-replacements" / recovery_key
                ),
            )
    except (OSError, ValueError) as exc:
        raise ApplicationSnapshotRefused(
            "An accepting artifact replacement could not be settled before checkpointing."
        ) from exc


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
        return _relative_path(value, label="checkpoint file path")

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("checkpoint files require lowercase SHA-256")
        return value


class ApplicationSnapshotRoot(_StrictModel):
    project_id: str
    live_path: str
    archive_path: str
    files: tuple[ApplicationSnapshotFile, ...]


def _project_restore_location(
    project: BackupProjectCapture,
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
        raise ValueError("project research rollback file escaped .research")
    return PurePosixPath(*path.parts[1:]).as_posix()


def _parent_directories(files: list[ApplicationSnapshotFile]) -> tuple[str, ...]:
    directories: set[str] = set()
    for item in files:
        parent = PurePosixPath(item.relative_path).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return tuple(sorted(directories))


def _set_private_directory_modes(root: Path) -> None:
    for current, directory_names, _file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        metadata = current_path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or current_path.is_symlink():
            raise ApplicationSnapshotRefused("A rebuilt checkpoint directory is unsafe.")
        current_path.chmod(_DIRECTORY_MODE)
        for name in directory_names:
            path = current_path / name
            if path.is_symlink():
                # A preserved stage link to a directory; nothing is rebuilt through it.
                continue
            metadata = path.lstat()
            if not stat.S_ISDIR(metadata.st_mode):
                raise ApplicationSnapshotRefused("A rebuilt checkpoint directory is unsafe.")


def _snapshot_tree(
    source: Path,
    destination: Path,
    *,
    relative_prefix: PurePosixPath,
    keep_links: bool = False,
) -> tuple[tuple[str, ...], list[ApplicationSnapshotFile]]:
    initial = _tree_inventory(source, keep_links=keep_links)
    destination.mkdir(mode=_DIRECTORY_MODE, parents=True)
    for relative in initial[0]:
        destination.joinpath(*PurePosixPath(relative).parts).mkdir(
            mode=_DIRECTORY_MODE,
            parents=True,
            exist_ok=True,
        )
    files: list[ApplicationSnapshotFile] = []
    for relative, signature in initial[1]:
        copied = _copy_stable_file(
            source.joinpath(*PurePosixPath(relative).parts),
            destination.joinpath(*PurePosixPath(relative).parts),
            relative_path=(relative_prefix / relative).as_posix(),
            restore_mode=signature[-1],
        )
        files.append(copied)
    for relative, target in initial[2]:
        # Recreated by its text alone: the checkpoint never reads through a link.
        os.symlink(target, destination.joinpath(*PurePosixPath(relative).parts))
    if _tree_inventory(source, keep_links=keep_links) != initial:
        raise ApplicationSnapshotRefused("A recovery-critical tree changed during checkpointing.")
    directories: set[str] = set()
    for path in (relative_prefix, *(relative_prefix / path for path in initial[0])):
        current = path
        while current != PurePosixPath("."):
            directories.add(current.as_posix())
            current = current.parent
    return tuple(sorted(directories)), files


def _tree_inventory(
    root: Path,
    *,
    keep_links: bool = False,
) -> tuple[
    tuple[str, ...],
    tuple[tuple[str, tuple[int, ...]], ...],
    tuple[tuple[str, str], ...],
]:
    """Inventory one tree's directories, regular files, and (optionally) links.

    Trees RCP writes itself never contain links, so a link refuses them. A
    retained run stage is written by an agent, whose scratch legitimately holds
    links (pytest ``*current``, a virtual environment's ``bin/python``); with
    ``keep_links`` each is recorded by its text and recreated verbatim, never
    followed, so a rollback restores the stage exactly as the run left it.
    """

    if not root.is_dir() or root.is_symlink():
        raise ApplicationSnapshotRefused("A recovery-critical root is not an ordinary directory.")
    directories: list[str] = []
    files: list[tuple[str, tuple[int, ...]]] = []
    links: list[tuple[str, str]] = []
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        directory_names.sort()
        file_names.sort()
        for name in directory_names:
            path = current_path / name
            relative = _stage_relative_path(path, root)
            if path.is_symlink() and keep_links:
                links.append((relative, _link_text(path, root)))
                continue
            metadata = path.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
                raise ApplicationSnapshotRefused("A recovery-critical tree contains a link.")
            directories.append(relative)
        for name in file_names:
            path = current_path / name
            relative = _stage_relative_path(path, root)
            if path.is_symlink() and keep_links:
                links.append((relative, _link_text(path, root)))
                continue
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
                raise ApplicationSnapshotRefused(
                    "A recovery-critical tree contains a special file."
                )
            files.append(
                (
                    relative,
                    (
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_size,
                        metadata.st_mtime_ns,
                        metadata.st_ctime_ns,
                        stat.S_IMODE(metadata.st_mode),
                    ),
                )
            )
        if len(directories) + len(files) + len(links) > BACKUP_INVENTORY_MAX_ENTRIES:
            raise ApplicationSnapshotRefused(
                "A recovery-critical tree exceeds its inventory bound."
            )
    return tuple(directories), tuple(files), tuple(links)


_LINK_TEXT_MAX_BYTES = 4096


def _stage_relative_path(path: Path, root: Path) -> str:
    """Name one stage entry the way the supervisor's checkpoint manifest requires.

    The supervisor rejects backslashes and control characters in entry paths, so
    a stage entry with such a name refuses here, before the payload is prepared,
    instead of failing checkpoint creation afterwards.
    """
    relative = path.relative_to(root).as_posix()
    try:
        return _relative_path(relative, label="recovery stage entry")
    except ValueError as error:
        raise ApplicationSnapshotRefused(
            f"A recovery stage entry has an unusable name: {relative!r}"
        ) from error


def _link_text(path: Path, root: Path) -> str:
    target = os.readlink(path)
    if (
        not target
        or len(target.encode()) > _LINK_TEXT_MAX_BYTES
        or any(ord(c) < 32 for c in target)
    ):
        raise ApplicationSnapshotRefused(
            f"A recovery stage link has an unusable target: {path.relative_to(root).as_posix()}"
        )
    return target


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
        raise ApplicationSnapshotRefused("A checkpoint source file is unavailable.") from exc
    if not stat.S_ISREG(initial.st_mode) or source.is_symlink():
        raise ApplicationSnapshotRefused("A checkpoint source is not an ordinary file.")
    destination.parent.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
    source_descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    destination_descriptor = -1
    digest = hashlib.sha256()
    size = 0
    try:
        opened = os.fstat(source_descriptor)
        if (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino):
            raise ApplicationSnapshotRefused("A checkpoint source changed while it was opened.")
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
            raise ApplicationSnapshotRefused("A checkpoint source changed during copying.")
        if size != final.st_size:
            raise ApplicationSnapshotRefused("A checkpoint file copy is incomplete.")
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


def _require_directory(path: Path, *, expected_uid: int | None, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ApplicationSnapshotRefused(f"The {label} is unavailable.") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or (expected_uid is not None and metadata.st_uid != expected_uid)
    ):
        raise ApplicationSnapshotRefused(f"The {label} is not one safe owned directory.")


class ApplicationSnapshotPolicy:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.expected_uid = os.geteuid()

    def _copy_imported_sources(
        self,
        app_root_path: Path,
        receipt: BackupProjectFileCaptureReceipt,
        *,
        captured_root: bool,
    ) -> tuple[set[str], list[ApplicationSnapshotFile]]:
        directories: set[str] = set()
        files: list[ApplicationSnapshotFile] = []
        collection = app_root_path / "project-sources"
        if captured_root:
            collection.mkdir(mode=_DIRECTORY_MODE)
            directories.add("project-sources")
        expected_present = tuple(
            sorted(capture.project_id for capture in receipt.imported_sources if capture.present)
        )
        try:
            if expected_present and not captured_root:
                raise ApplicationSnapshotRefused(
                    "The final capture omitted the imported provider-source app-data root."
                )
            if ImportedProviderSourceStore.project_ids(self.data_dir) != expected_present:
                raise ApplicationSnapshotRefused(
                    "The imported provider-source owners changed after final capture."
                )
            for capture in receipt.imported_sources:
                expected = ImportedProviderSourceInventory.model_validate(
                    capture.inventory.model_dump()
                )
                owner = ImportedProviderSourceStore(self.data_dir, capture.project_id)
                if not capture.present:
                    if os.path.lexists(owner.root):
                        raise ApplicationSnapshotRefused(
                            "An imported provider-source root appeared after final capture."
                        )
                    continue
                project_root = collection / capture.project_id
                project_root.mkdir(mode=_DIRECTORY_MODE)
                snapshot = owner.capture_snapshot(
                    project_root / "provider-history",
                    expected_inventory=expected,
                )
                expected_files = {
                    entry.source_relative_path.removeprefix("provider-history/"): (
                        entry.sha256,
                        entry.size_bytes,
                    )
                    for entry in capture.files
                }
                if (
                    not snapshot.present
                    or {
                        item.relative_path: (item.sha256, item.size_bytes)
                        for item in snapshot.files
                    }
                    != expected_files
                ):
                    raise ApplicationSnapshotRefused(
                        "The imported provider-source checkpoint differs from final capture."
                    )
                prefix = PurePosixPath("project-sources") / capture.project_id
                directories.update(
                    {
                        prefix.as_posix(),
                        (prefix / "provider-history").as_posix(),
                        *(
                            (prefix / "provider-history" / item.provider).as_posix()
                            for item in expected.files
                        ),
                    }
                )
                files.extend(
                    ApplicationSnapshotFile(
                        relative_path=(prefix / "provider-history" / item.relative_path).as_posix(),
                        sha256=item.sha256,
                        size_bytes=item.size_bytes,
                        mode=0o400,
                    )
                    for item in snapshot.files
                )
        except ApplicationSnapshotRefused:
            raise
        except (OSError, ValueError) as exc:
            raise ApplicationSnapshotRefused(
                "The imported provider-source checkpoint could not be captured safely."
            ) from exc
        return directories, files

    def _copy_transfer_inbox(
        self,
        snapshot_store: AppStore,
        app_root_path: Path,
    ) -> tuple[set[str], list[ApplicationSnapshotFile]]:
        """Capture only receipt-backed complete target upload archives.

        The live inbox is deliberately excluded from ordinary backup and
        rehearsal.  An update checkpoint is the one local boundary that may
        retain a complete upload, but only when the immutable SQLite snapshot
        contains a typed completion row that binds the request, archive, and
        exact bytes.  Filesystem names are derived through the target owner;
        no path supplied by a database row is trusted.
        """

        uploads = self._read_complete_transfer_uploads(snapshot_store)
        live_root = self.data_dir / "transfer-inbox"
        if not uploads:
            if os.path.lexists(live_root):
                self._require_private_transfer_directory(live_root)
            self._require_empty_transfer_root(
                "transfer-inbox",
                "A transfer inbox entry has no typed completed-upload proof yet. Finish or "
                "remove the transfer before this source update.",
            )
            return set(), []

        self._require_private_transfer_directory(live_root)
        try:
            entries = tuple(live_root.iterdir())
        except OSError as exc:
            raise ApplicationSnapshotRefused(
                "The transfer inbox cannot be inventoried safely."
            ) from exc
        expected_names = {f"{upload.request_id}.rcp-transfer" for upload in uploads}
        actual_names = {entry.name for entry in entries}
        if expected_names - actual_names:
            raise ApplicationSnapshotRefused(
                "A complete transfer upload is missing its archive file."
            )
        if actual_names - expected_names:
            raise ApplicationSnapshotRefused(
                "The transfer inbox contains an unknown, partial, or untyped entry."
            )

        destination_root = app_root_path / "transfer-inbox"
        destination_root.mkdir(mode=_DIRECTORY_MODE)
        copied: list[ApplicationSnapshotFile] = []
        for upload in uploads:
            source = target_transfer_archive_path(self.data_dir, upload.request_id)
            self._require_transfer_archive_file(source)
            copied_file = _copy_declared_file(
                source,
                destination_root / source.name,
                relative_path=f"transfer-inbox/{source.name}",
                expected_sha256=upload.archive_sha256,
                expected_size=upload.archive_size_bytes,
                restore_mode=0o600,
            )
            # The source mode/owner are part of this checkpoint boundary, not
            # merely properties of the copied payload.
            self._require_transfer_archive_file(source)
            copied.append(copied_file)
        return {"transfer-inbox"}, copied

    def _read_complete_transfer_uploads(
        self,
        snapshot_store: AppStore,
    ) -> tuple[ProjectTransferUploadRecord, ...]:
        try:
            stored = snapshot_store.target_project_transfer_uploads()
        except (KeyError, RuntimeError, ValueError, sqlite3.Error) as exc:
            raise ApplicationSnapshotRefused(
                "The SQLite snapshot has no readable typed transfer-upload table."
            ) from exc
        if len(stored) > BACKUP_INVENTORY_MAX_ENTRIES:
            raise ApplicationSnapshotRefused("The transfer-upload inventory exceeds its bound.")

        uploads: list[ProjectTransferUploadRecord] = []
        for upload in stored:
            if upload.status == "consumed":
                continue
            if upload.status != "complete" or upload.receipt is None:
                raise ApplicationSnapshotRefused(
                    "A transfer inbox upload is not at its durable complete boundary."
                )
            try:
                request = snapshot_store.project_transfer_request(upload.request_id)
            except (KeyError, RuntimeError, ValueError) as exc:
                raise ApplicationSnapshotRefused(
                    "A complete transfer upload is not bound to a valid target request."
                ) from exc
            if (
                request is None
                or request.side != "target"
                or request.project_id != upload.project_id
                or request.archive_sha256 != upload.archive_sha256
                or request.archive_size_bytes != upload.archive_size_bytes
            ):
                raise ApplicationSnapshotRefused(
                    "A complete transfer upload is not bound to its target archive request."
                )
            uploads.append(upload)
        return tuple(uploads)

    def _require_private_transfer_directory(self, path: Path) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ApplicationSnapshotRefused("The transfer inbox is unavailable.") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != self.expected_uid
            or stat.S_IMODE(metadata.st_mode) != _DIRECTORY_MODE
        ):
            raise ApplicationSnapshotRefused(
                "The transfer inbox has unsafe ownership, mode, or type."
            )

    def _require_transfer_archive_file(self, path: Path) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ApplicationSnapshotRefused(
                "A complete transfer upload is missing its archive file."
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != self.expected_uid
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ApplicationSnapshotRefused(
                "A complete transfer upload has unsafe ownership, mode, or type."
            )

    def _require_empty_transfer_exports(self) -> None:
        self._require_empty_transfer_root(
            "transfer-exports",
            "A sealed transfer export has no update-checkpoint integration yet. Finish the "
            "transfer before this source update.",
        )

    def _require_empty_transfer_root(self, name: str, diagnostic: str) -> None:
        root = self.data_dir / name
        if not os.path.lexists(root):
            return
        _require_directory(root, expected_uid=self.expected_uid, label=name.replace("-", " "))
        try:
            entries = tuple(root.iterdir())
        except OSError as exc:
            raise ApplicationSnapshotRefused(f"The {name} root cannot be inventoried.") from exc
        if entries:
            raise ApplicationSnapshotRefused(diagnostic)

    def copy_project_roots(
        self,
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
                item.model_copy(
                    update={"relative_path": _strip_research_prefix(item.relative_path)}
                )
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
