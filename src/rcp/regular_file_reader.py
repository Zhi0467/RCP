"""Symlink-refusing bounded reader, also shipped intact to SSH hosts."""

from __future__ import annotations

import base64
import errno
import fnmatch
import json
import os
import stat
import sys
from pathlib import Path


def _open_local_directory(directory: Path) -> int:
    if not directory.is_absolute():
        raise ValueError("artifact directory must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    current = os.open("/", flags)
    try:
        for part in directory.parts[1:]:
            following = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = following
        return current
    except FileNotFoundError:
        os.close(current)
        raise
    except OSError as exc:
        os.close(current)
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError("artifact directory is not a regular directory") from exc
        raise


def read_local_regular_file(
    directory: Path, name: str, *, max_bytes: int, tail: bool = False
) -> bytes:
    """Read one direct regular child without following a symlink."""
    if Path(name).name != name or name in {"", ".", ".."}:
        raise ValueError("artifact name must be a plain base name")
    # O_NONBLOCK keeps a FIFO swapped in after listing from blocking the open;
    # the regular-file check below then refuses it, and regular reads ignore it.
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = _open_local_directory(directory)
    try:
        try:
            file_fd = os.open(name, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            raise
        except OSError as exc:
            try:
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                raise
            except OSError:
                raise exc from None
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("artifact is not a readable regular file") from exc
            raise
        try:
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("artifact is not a regular file")
            if metadata.st_size > max_bytes and not tail:
                raise ValueError("artifact exceeds the per-file limit")
            if tail and metadata.st_size > max_bytes:
                os.lseek(file_fd, -max_bytes, os.SEEK_END)
            chunks: list[bytes] = []
            remaining = max_bytes if tail else max_bytes + 1
            while remaining > 0:
                chunk = os.read(file_fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > max_bytes:
                raise ValueError("artifact exceeds the per-file limit")
            return data
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


def _matching_regular_files(
    directory_fd: int, parts: list[str], scan: dict[str, int], prefix: str = ""
):
    """Yield matches while keeping their parent directory descriptor open.

    `scan["left"]` bounds the directory entries listed across the whole walk, so a
    broad pattern over a large tree stops early instead of listing everything.
    """
    names: list[str] = []
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            if scan["left"] <= 0:
                scan["exhausted"] = 1
                break
            scan["left"] -= 1
            names.append(entry.name)
    for name in sorted(names):
        if not fnmatch.fnmatchcase(name, parts[0]):
            continue
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        relative = f"{prefix}{name}"
        if len(parts) == 1:
            if stat.S_ISREG(metadata.st_mode):
                yield directory_fd, name, relative
        elif stat.S_ISDIR(metadata.st_mode):
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            try:
                child_fd = os.open(name, flags, dir_fd=directory_fd)
            except OSError as exc:
                if exc.errno in {errno.ENOENT, errno.ELOOP, errno.ENOTDIR}:
                    continue
                raise
            try:
                yield from _matching_regular_files(child_fd, parts[1:], scan, relative + "/")
            finally:
                os.close(child_fd)


def read_local_regular_files(
    directory: Path,
    pattern: str,
    *,
    max_files: int,
    max_total_bytes: int,
    max_bytes: int,
    max_entries: int,
    tail: bool = False,
) -> dict:
    """Read bounded folder matches through directory fds, never symlinks."""
    parts = pattern.split("/")
    if ".." in directory.parts or any(part in {"", ".", ".."} or "**" in part for part in parts):
        raise ValueError("invalid live folder path or pattern")
    directory_fd = _open_local_directory(directory)
    files: list[dict] = []
    truncated = False
    remaining = max_total_bytes
    scan = {"left": max_entries, "exhausted": 0}
    matches = _matching_regular_files(directory_fd, parts, scan)
    try:
        for parent_fd, name, relative in matches:
            if len(files) >= max_files or remaining <= 0:
                truncated = True
                break
            item = {"path": relative, "data": "", "truncated": False}
            try:
                flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
                try:
                    file_fd = os.open(name, flags, dir_fd=parent_fd)
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        continue
                    raise
                try:
                    metadata = os.fstat(file_fd)
                    if not stat.S_ISREG(metadata.st_mode):
                        continue
                    limit = min(max_bytes, remaining)
                    clipped_tail = tail and metadata.st_size > limit
                    if clipped_tail:
                        os.lseek(file_fd, -(limit + 1), os.SEEK_END)
                    chunks: list[bytes] = []
                    unread = limit + 1
                    while unread:
                        chunk = os.read(file_fd, min(1024 * 1024, unread))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        unread -= len(chunk)
                    data = b"".join(chunks)
                    item["truncated"] = metadata.st_size > limit or len(data) > limit
                    remaining -= min(len(data), limit)
                    if clipped_tail:
                        boundary = data[:1] == b"\n"
                        data = data[1:]
                        if not boundary:
                            data = data.partition(b"\n")[2]
                    else:
                        data = data[:limit]
                        if item["truncated"] and not data.endswith(b"\n"):
                            data = data.rpartition(b"\n")[0]
                    item["data"] = base64.b64encode(data).decode("ascii")
                    truncated = truncated or item["truncated"]
                finally:
                    os.close(file_fd)
            except OSError as exc:
                item["error"] = str(exc)
            files.append(item)
    finally:
        matches.close()
        os.close(directory_fd)
    return {
        "files": sorted(files, key=lambda item: item["path"]),
        "truncated": truncated or bool(scan["exhausted"]),
    }


if __name__ == "__main__" and sys.argv[1] == "files":
    print(
        json.dumps(
            read_local_regular_files(
                Path(sys.argv[2]),
                sys.argv[3],
                max_files=int(sys.argv[4]),
                max_total_bytes=int(sys.argv[5]),
                max_bytes=int(sys.argv[6]),
                max_entries=int(sys.argv[7]),
                tail=sys.argv[8] == "tail",
            )
        )
    )
elif __name__ == "__main__":
    path = Path(sys.argv[1])
    sys.stdout.buffer.write(
        read_local_regular_file(
            path.parent, path.name, max_bytes=int(sys.argv[2]), tail=sys.argv[3] == "tail"
        )
    )
