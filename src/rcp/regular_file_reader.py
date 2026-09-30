"""Symlink-refusing bounded reader, also shipped intact to SSH hosts."""

from __future__ import annotations

import errno
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


if __name__ == "__main__":
    path = Path(sys.argv[1])
    sys.stdout.buffer.write(
        read_local_regular_file(
            path.parent, path.name, max_bytes=int(sys.argv[2]), tail=sys.argv[3] == "tail"
        )
    )
