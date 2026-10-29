"""Bounded regular-file tree reader, also shipped intact to SSH hosts."""

from __future__ import annotations

import base64
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass
class RegularTree:
    files: dict[str, bytes]
    directories: list[str]


def read_regular_tree(
    root: Path,
    directory: str = "",
    /,
    *,
    max_entries: int,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> RegularTree:
    """Walk relative to open descriptors, refusing links and changed entries."""
    parts = PurePosixPath(directory)
    if parts.is_absolute() or ".." in parts.parts:
        raise ValueError("workspace tree directory must be relative")
    if min(max_entries, max_files, max_file_bytes, max_total_bytes) < 0:
        raise ValueError("workspace tree limits must not be negative")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    result: dict[str, bytes] = {}
    directories: list[str] = []
    total = 0
    count = 0

    def visit(fd: int, prefix: str) -> None:
        nonlocal total, count
        names: list[str] = []
        with os.scandir(fd) as entries:
            for entry in entries:
                count += 1
                if count > max_entries:
                    raise ValueError("workspace tree exceeds entry limit")
                names.append(entry.name)
        for name in sorted(names):
            relative = f"{prefix}/{name}" if prefix else name
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError("workspace tree contains a nonregular entry or symlink")
            child = os.open(name, flags if stat.S_ISDIR(info.st_mode) else file_flags, dir_fd=fd)
            try:
                opened = os.fstat(child)
                if (opened.st_dev, opened.st_ino, opened.st_mode) != (
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                ):
                    raise ValueError("workspace tree changed during collection")
                if stat.S_ISDIR(opened.st_mode):
                    directories.append(relative)
                    visit(child, relative)
                    continue
                if opened.st_nlink != 1:
                    raise ValueError("workspace tree contains a hard-linked file")
                if len(result) >= max_files:
                    raise ValueError("workspace tree exceeds file count limit")
                limit = min(max_file_bytes, max_total_bytes - total)
                if opened.st_size > limit:
                    raise ValueError("workspace tree exceeds byte limit")
                with os.fdopen(child, "rb", closefd=False) as stream:
                    data = stream.read(limit + 1)
                if len(data) > limit:
                    raise ValueError("workspace tree exceeds byte limit")
                total += len(data)
                result[relative] = data
            finally:
                os.close(child)

    descriptors: list[int] = []
    try:
        fd = os.open(root, flags)
        descriptors.append(fd)
        for part in parts.parts:
            fd = os.open(part, flags, dir_fd=fd)
            descriptors.append(fd)
        visit(fd, "")
        return RegularTree(result, directories)
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


if __name__ == "__main__":
    try:
        tree = read_regular_tree(
            Path(sys.argv[1]),
            sys.argv[2],
            max_entries=int(sys.argv[3]),
            max_files=int(sys.argv[4]),
            max_file_bytes=int(sys.argv[5]),
            max_total_bytes=int(sys.argv[6]),
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(45) from exc
    print(
        json.dumps(
            {
                "files": [
                    (name, base64.b64encode(data).decode("ascii"))
                    for name, data in tree.files.items()
                ],
                "directories": tree.directories,
            }
        )
    )
