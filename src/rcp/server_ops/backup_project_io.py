"""Bounded optimistic byte capture for one backup project."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Literal

from rcp.history.manager import (
    CanonicalFactSource,
    canonical_fact_sources,
    iter_canonical_fact_bytes,
)
from rcp.limits import (
    BACKUP_COPY_BUFFER_BYTES,
    BACKUP_INVENTORY_MAX_ENTRIES,
    BACKUP_STABLE_READ_ATTEMPTS,
    CHAT_ARTIFACT_MAX_FILE_BYTES,
)
from rcp.regular_file_reader import _open_local_directory
from rcp.server_ops._local_primitives import fsync_directory
from rcp.server_ops._local_primitives import write_all as _write_all
from rcp.server_ops.backup_models import BackupFileEntry
from rcp.service import (
    CanonicalChatBackupSource,
    canonical_chat_backup_sources,
    iter_canonical_chat_backup_prefix,
)
from rcp.transport.state import StateUnavailable

ProjectFileGroup = Literal[
    "canonical",
    "chat",
    "paper_introduction",
    "paper_projection",
    "fact",
    "kept_artifact",
    "legacy_kept_result_view",
    "space_thread",
    "local_memory",
]


class BackupProjectFileUnavailable(RuntimeError):
    """One project could not support an honest optimistic file capture."""


def capture_app_data_root(
    data_dir: Path,
    capture_root: Path,
    root_name: Literal["agent-threads", "memory"],
) -> tuple[BackupFileEntry, ...]:
    """Inventory an owned root, then copy its regular files through stable reads."""
    root = data_dir / root_name
    try:
        root_metadata = root.lstat()
    except FileNotFoundError:
        return ()
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("The app-data root is not a regular directory.")
    pending = [root]
    sources: list[tuple[Path, os.stat_result]] = []
    observed_entries = 0
    while pending:
        directory = pending.pop()
        directory_descriptor = _open_local_directory(directory)
        try:
            with os.scandir(directory_descriptor) as children:
                for child in children:
                    observed_entries += 1
                    if observed_entries > BACKUP_INVENTORY_MAX_ENTRIES:
                        raise ValueError("The app-data inventory exceeds its entry bound.")
                    metadata = child.stat(follow_symlinks=False)
                    path = directory / child.name
                    relative = path.relative_to(root)
                    if stat.S_ISDIR(metadata.st_mode):
                        if root_name == "agent-threads" and len(relative.parts) > 1:
                            if len(relative.parts) == 2 and child.name == "stages":
                                continue
                            raise ValueError(
                                "The transcript root contains an unexpected directory."
                            )
                        pending.append(path)
                    elif stat.S_ISREG(metadata.st_mode):
                        if root_name == "agent-threads" and (
                            len(relative.parts) != 2 or path.suffix != ".jsonl"
                        ):
                            raise ValueError("The transcript root contains an unexpected file.")
                        if root_name == "memory" and len(relative.parts) < 2:
                            raise ValueError("Memory files must belong to a project directory.")
                        sources.append((path, metadata))
                    else:
                        raise ValueError("The app-data root contains a non-regular entry.")
        finally:
            os.close(directory_descriptor)
    entries = tuple(
        stable_copy_entry(
            source,
            capture_root,
            None,
            PurePosixPath(source.relative_to(data_dir).as_posix()),
            group="space_thread" if root_name == "agent-threads" else "local_memory",
            expected_size=metadata.st_size if root_name == "memory" else None,
            observed=metadata,
            complete_lines=root_name == "agent-threads",
        )
        for source, metadata in sorted(sources, key=lambda item: item[0])
    )
    return entries


def capture_chat_files(
    source_root: Path,
    project_root: Path,
    project_id: str,
    *,
    operation_projects: Mapping[str, str],
) -> list[BackupFileEntry]:
    entries: list[BackupFileEntry] = []
    for source in canonical_chat_backup_sources(source_root):
        entry = _copy_chat_prefix(
            source,
            project_root,
            project_id,
            operation_projects=operation_projects,
        )
        if entry is not None:
            entries.append(entry)
    return entries


def _copy_chat_prefix(
    source: CanonicalChatBackupSource,
    project_root: Path,
    project_id: str,
    *,
    operation_projects: Mapping[str, str],
) -> BackupFileEntry | None:
    relative = PurePosixPath(".research/chat") / source.path.name
    destination = _destination_path(project_root, relative)
    _prepare_destination_parent(project_root, destination.parent)
    temporary = destination.with_name(f".{destination.name}.backup-{uuid.uuid4().hex}")
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        0o400,
    )
    digest = hashlib.sha256()
    size = 0
    try:
        for line in iter_canonical_chat_backup_prefix(
            source,
            project_id=project_id,
            operation_projects=operation_projects,
        ):
            _write_all(descriptor, line)
            digest.update(line)
            size += len(line)
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        descriptor = -1
        _unlink_if_present(temporary)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not size:
        _unlink_if_present(temporary)
        return None
    os.replace(temporary, destination)
    fsync_directory(destination.parent)
    return _file_entry(project_id, relative, "chat", digest.hexdigest(), size)


def fact_backup_sources(source_root: Path) -> tuple[CanonicalFactSource, ...]:
    try:
        return canonical_fact_sources(source_root)
    except ValueError as exc:
        raise BackupProjectFileUnavailable(str(exc)) from exc


def stable_copy_fact_entry(
    source_root: Path,
    source: CanonicalFactSource,
    project_root: Path,
    project_id: str,
    relative: PurePosixPath,
) -> BackupFileEntry:
    """Copy one fact through its owner while allowing bounded stable replacement."""

    destination = _destination_path(project_root, relative)
    _prepare_destination_parent(project_root, destination.parent)
    last_error: BaseException | None = None
    for _ in range(BACKUP_STABLE_READ_ATTEMPTS):
        temporary = destination.with_name(f".{destination.name}.backup-{uuid.uuid4().hex}")
        try:
            current = next(
                (
                    candidate
                    for candidate in canonical_fact_sources(source_root)
                    if candidate.relative_path == source.relative_path
                ),
                None,
            )
            if current is None:
                raise ValueError("The fact disappeared before capture.")
            digest, size = _copy_fact_source(source_root, current, temporary)
            os.replace(temporary, destination)
            fsync_directory(destination.parent)
            return _file_entry(project_id, relative, "fact", digest, size)
        except (OSError, ValueError) as exc:
            last_error = exc
            _unlink_if_present(temporary)
    raise BackupProjectFileUnavailable(
        "A project fact did not stabilize for capture."
    ) from last_error


def _copy_fact_source(
    source_root: Path,
    source: CanonicalFactSource,
    destination: Path,
) -> tuple[str, int]:
    descriptor = os.open(
        destination,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        0o400,
    )
    digest = hashlib.sha256()
    size = 0
    try:
        for chunk in iter_canonical_fact_bytes(
            source_root,
            source,
            chunk_size=BACKUP_COPY_BUFFER_BYTES,
        ):
            _write_all(descriptor, chunk)
            digest.update(chunk)
            size += len(chunk)
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
        return digest.hexdigest(), size
    finally:
        os.close(descriptor)


def stable_copy_entry(
    source: Path,
    project_root: Path,
    project_id: str | None,
    relative: PurePosixPath,
    *,
    group: Literal[
        "canonical",
        "paper_introduction",
        "paper_projection",
        "fact",
        "space_thread",
        "local_memory",
    ],
    expected_size: int | None = None,
    observed: os.stat_result | None = None,
    complete_lines: bool = False,
) -> BackupFileEntry:
    destination = _destination_path(project_root, relative)
    _prepare_destination_parent(project_root, destination.parent)
    last_error: BaseException | None = None
    for _ in range(BACKUP_STABLE_READ_ATTEMPTS):
        temporary = destination.with_name(f".{destination.name}.backup-{uuid.uuid4().hex}")
        try:
            digest, size = _copy_one_stable_file(
                source,
                temporary,
                expected_size=expected_size,
                observed=observed,
                complete_lines=complete_lines,
            )
            os.replace(temporary, destination)
            fsync_directory(destination.parent)
            return _file_entry(project_id, relative, group, digest, size)
        except (OSError, ValueError) as exc:
            last_error = exc
            _unlink_if_present(temporary)
    raise BackupProjectFileUnavailable(
        "A project file did not stabilize for capture."
    ) from last_error


def _copy_one_stable_file(
    source: Path,
    destination: Path,
    *,
    expected_size: int | None,
    observed: os.stat_result | None = None,
    complete_lines: bool = False,
) -> tuple[str, int]:
    directory_descriptor = _open_local_directory(source.parent)
    try:
        source_descriptor = os.open(
            source.name,
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
    finally:
        os.close(directory_descriptor)
    destination_descriptor = -1
    try:
        initial = os.fstat(source_descriptor)
        current = source.lstat()
        if (
            not stat.S_ISREG(initial.st_mode)
            or (initial.st_dev, initial.st_ino) != (current.st_dev, current.st_ino)
            or (expected_size is not None and initial.st_size != expected_size)
            or (
                observed is not None
                and (
                    (initial.st_dev, initial.st_ino) != (observed.st_dev, observed.st_ino)
                    or initial.st_size < observed.st_size
                )
            )
        ):
            raise ValueError("The project file changed before capture.")
        destination_descriptor = os.open(
            destination,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o400,
        )
        digest = hashlib.sha256()
        complete_digest = digest.copy()
        complete_size = 0
        size = 0
        boundary = observed.st_size if complete_lines and observed is not None else initial.st_size
        while size < boundary:
            chunk = os.read(source_descriptor, min(BACKUP_COPY_BUFFER_BYTES, boundary - size))
            if not chunk:
                break
            _write_all(destination_descriptor, chunk)
            newline = chunk.rfind(b"\n") + 1 if complete_lines else 0
            if newline:
                digest.update(chunk[:newline])
                complete_digest = digest.copy()
                complete_size = size + newline
            digest.update(chunk[newline:])
            size += len(chunk)
        final = os.fstat(source_descriptor)
        path_final = source.lstat()
        stable_fields = (
            ("st_dev", "st_ino")
            if complete_lines
            else ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        )
        if any(getattr(initial, field) != getattr(final, field) for field in stable_fields) or any(
            getattr(final, field) != getattr(path_final, field) for field in stable_fields
        ):
            raise ValueError("The project file changed during capture.")
        if size != boundary or final.st_size < boundary or path_final.st_size < boundary:
            raise ValueError("The project file copy is incomplete.")
        if complete_lines:
            os.ftruncate(destination_descriptor, complete_size)
            digest, size = complete_digest, complete_size
        os.fchmod(destination_descriptor, 0o400)
        os.fsync(destination_descriptor)
        return digest.hexdigest(), size
    finally:
        os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)


def stable_workspace_bytes(reader: Callable[[], bytes]) -> bytes:
    previous: tuple[str, int, bytes] | None = None
    last_error: BaseException | None = None
    for _ in range(BACKUP_STABLE_READ_ATTEMPTS):
        try:
            data = reader()
            if not isinstance(data, bytes) or len(data) > CHAT_ARTIFACT_MAX_FILE_BYTES:
                raise ValueError("kept-file bytes exceed their existing product limit")
        except (OSError, StateUnavailable, ValueError) as exc:
            last_error = exc
            previous = None
            continue
        observed = (hashlib.sha256(data).hexdigest(), len(data), data)
        if previous is not None and observed == previous:
            return data
        previous = observed
    raise BackupProjectFileUnavailable("A kept project file did not stabilize.") from last_error


def write_bytes_entry(
    data: bytes,
    project_root: Path,
    project_id: str,
    relative: PurePosixPath,
    *,
    group: Literal["kept_artifact", "legacy_kept_result_view"],
) -> BackupFileEntry:
    destination = _destination_path(project_root, relative)
    _prepare_destination_parent(project_root, destination.parent)
    descriptor = os.open(
        destination,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        0o400,
    )
    try:
        _write_all(descriptor, data)
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(destination.parent)
    return _file_entry(
        project_id,
        relative,
        group,
        hashlib.sha256(data).hexdigest(),
        len(data),
    )


def _file_entry(
    project_id: str | None,
    relative: PurePosixPath,
    group: ProjectFileGroup,
    sha256: str,
    size: int,
) -> BackupFileEntry:
    return BackupFileEntry(
        archive_path=(
            (PurePosixPath("projects") / project_id / relative).as_posix()
            if project_id is not None
            else relative.as_posix()
        ),
        source_relative_path=relative.as_posix(),
        group=group,
        sha256=sha256,
        size_bytes=size,
    )


def _destination_path(project_root: Path, relative: PurePosixPath) -> Path:
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("project backup destination must be one safe relative path")
    destination = project_root.joinpath(*relative.parts)
    if not destination.is_relative_to(project_root):
        raise ValueError("project backup destination escaped its capture root")
    return destination


def _prepare_destination_parent(project_root: Path, parent: Path) -> None:
    relative = parent.relative_to(project_root)
    current = project_root
    for part in relative.parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            metadata = current.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise ValueError("project backup destination ancestry is unsafe") from None


def discard_failed_project_capture(capture_root: Path, project_root: Path) -> None:
    expected_parent = capture_root / "projects"
    if project_root.parent != expected_parent or not project_root.name:
        raise RuntimeError("refusing to discard an unbound project capture")
    try:
        metadata = project_root.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("refusing to discard an unsafe project capture")
    shutil.rmtree(project_root)


def _unlink_if_present(path: Path) -> None:
    with suppress(FileNotFoundError):
        path.unlink()


def fsync_directory_tree(root: Path) -> None:
    pending = [root]
    directories: list[Path] = []
    observed_entries = 0
    while pending:
        directory = pending.pop()
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("project backup output contains an unsafe directory")
        directories.append(directory)
        entries = list(directory.iterdir())
        observed_entries += len(entries)
        if observed_entries > BACKUP_INVENTORY_MAX_ENTRIES:
            raise ValueError("project backup output exceeds its entry bound")
        for entry in entries:
            item = entry.lstat()
            if stat.S_ISDIR(item.st_mode):
                pending.append(entry)
            elif not stat.S_ISREG(item.st_mode):
                raise ValueError("project backup output contains an unsafe entry")
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        fsync_directory(directory)


__all__ = [
    "BackupProjectFileUnavailable",
    "capture_app_data_root",
    "capture_chat_files",
    "discard_failed_project_capture",
    "fact_backup_sources",
    "fsync_directory",
    "fsync_directory_tree",
    "stable_copy_entry",
    "stable_copy_fact_entry",
    "stable_workspace_bytes",
    "write_bytes_entry",
]
