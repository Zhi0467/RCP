"""Shared local primitives for server maintenance code.

This module is intentionally standard-library-only.  Remote helpers remain
self-contained because their source is shipped to another interpreter.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path
from typing import Literal

PrivateFileReadFailure = Literal[
    "unavailable",
    "unsafe",
    "incomplete",
    "changed",
    "cannot_read",
]


class PrivateFileReadError(ValueError):
    """Classified failure from the shared stable private-file reader."""

    def __init__(self, failure: PrivateFileReadFailure) -> None:
        super().__init__(failure)
        self.failure = failure


def canonical_json_bytes(value: object) -> bytes:
    """Encode one value using RCP's stable JSON representation."""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_line(value: object) -> bytes:
    """Encode one stable JSON value followed by its record delimiter."""

    return canonical_json_bytes(value) + b"\n"


def canonical_json_text(value: object) -> str:
    """Encode one value using RCP's stable JSON text representation."""

    return canonical_json_bytes(value).decode("utf-8")


def canonical_uuid4(value: str, *, label: str) -> str:
    """Require the lowercase, hyphenated canonical spelling of a UUID4."""

    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{label} must be a canonical UUID4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError(f"{label} must be a lowercase, hyphenated canonical UUID4")
    return value


def canonical_operation_uuid(value: str, *, label: str) -> str:
    """Require the canonical spelling of a task operation identity.

    RCP mints ordinary task ids with UUID4 and deterministic Experiment-loop and
    Auto-research child ids with UUID5, so a durable capture must accept both.
    """

    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{label} must be a canonical UUID") from exc
    if parsed.version not in (4, 5) or str(parsed) != value:
        raise ValueError(f"{label} must be a lowercase, hyphenated canonical UUID4 or UUID5")
    return value


def is_canonical_uuid4(value: object) -> bool:
    """Return whether a value is the canonical spelling of a UUID4."""

    if not isinstance(value, str):
        return False
    try:
        canonical_uuid4(value, label="value")
    except ValueError:
        return False
    return True


def normalized_absolute_path(value: str, *, label: str) -> str:
    """Require one bounded, normalized absolute path, allowing the root."""

    _bounded_path_text(value, label=label)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError(f"{label} must be absolute and normalized")
    return value


def normalized_absolute_non_root_path(value: str, *, label: str) -> str:
    """Require one bounded, normalized absolute path other than the root."""

    normalized_absolute_path(value, label=label)
    if Path(value) == Path("/"):
        raise ValueError(f"{label} must be an absolute normalized non-root path")
    return value


def _bounded_path_text(value: str, *, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 4096
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} must be one bounded absolute path")


def write_all(descriptor: int, payload: bytes) -> None:
    """Write all bytes to an already-open descriptor or fail."""

    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write")
        remaining = remaining[written:]


def read_stable_private_file(
    path: Path,
    *,
    expected_uid: int,
    expected_mode: int,
    maximum: int,
    chunk_size: int,
) -> bytes:
    """Read one bounded private file while proving stable path identity."""

    if maximum <= 0 or chunk_size <= 0:
        raise ValueError("private file read bounds must be positive")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise PrivateFileReadError("unavailable") from exc
    try:
        try:
            initial = os.fstat(descriptor)
            if (
                not stat.S_ISREG(initial.st_mode)
                or initial.st_uid != expected_uid
                or stat.S_IMODE(initial.st_mode) != expected_mode
                or initial.st_size > maximum
            ):
                raise PrivateFileReadError("unsafe")
            chunks: list[bytes] = []
            remaining = initial.st_size
            while remaining:
                chunk = os.read(descriptor, min(chunk_size, remaining))
                if not chunk:
                    raise PrivateFileReadError("incomplete")
                chunks.append(chunk)
                remaining -= len(chunk)
            final = os.fstat(descriptor)
            path_final = path.lstat()
        except PrivateFileReadError:
            raise
        except OSError as exc:
            raise PrivateFileReadError("cannot_read") from exc
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(initial, name) != getattr(final, name) for name in stable) or any(
            getattr(final, name) != getattr(path_final, name) for name in stable
        ):
            raise PrivateFileReadError("changed")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def fsync_file(path: Path) -> None:
    """Synchronize one regular file without following a final symlink."""

    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("fsync target is not a regular file")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_directory(path: Path) -> None:
    """Synchronize one directory without following a final symlink."""

    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("fsync target is not a directory")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_directory_tree(root: Path) -> None:
    """Synchronize only the directories in a local tree, children first."""

    directories = _tree_directories(root)
    for directory in reversed(directories):
        fsync_directory(directory)


def fsync_file_tree(root: Path) -> None:
    """Synchronize every regular file and directory in a local tree."""

    directories = _tree_directories(root)
    for directory in directories:
        for entry in directory.iterdir():
            metadata = entry.lstat()
            if stat.S_ISREG(metadata.st_mode):
                fsync_file(entry)
            elif not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("fsync tree contains a non-regular entry")
    for directory in reversed(directories):
        fsync_directory(directory)


def _tree_directories(root: Path) -> list[Path]:
    pending = [root]
    directories: list[Path] = []
    while pending:
        directory = pending.pop()
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("fsync tree contains a non-directory path")
        directories.append(directory)
        children: list[Path] = []
        for entry in directory.iterdir():
            metadata = entry.lstat()
            if stat.S_ISDIR(metadata.st_mode):
                children.append(entry)
            elif not stat.S_ISREG(metadata.st_mode):
                raise ValueError("fsync tree contains a non-regular entry")
        pending.extend(sorted(children, reverse=True))
    return directories


__all__ = [
    "canonical_json_bytes",
    "canonical_json_line",
    "canonical_json_text",
    "canonical_uuid4",
    "fsync_directory",
    "fsync_directory_tree",
    "fsync_file",
    "fsync_file_tree",
    "is_canonical_uuid4",
    "normalized_absolute_non_root_path",
    "normalized_absolute_path",
    "PrivateFileReadError",
    "read_stable_private_file",
    "write_all",
]
