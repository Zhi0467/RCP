"""Read one bounded window of a repository file without following symlinks.

This module is the single implementation of that read. RCP calls `read_window`
in process for a local repository, and ships this module's own source to a
repository host to run under `python3 -c` for a remote one, so the two paths
cannot drift apart. It therefore imports only the standard library and never
`rcp`, and its command form speaks the exit codes and framing that
`rcp.repository_preview` expects.
"""

from __future__ import annotations

import json
import os
import stat
import sys

CHUNK_BYTES = 1024 * 1024
MISSING_EXIT = 44
UNREADABLE_EXIT = 45


class WindowUnreadable(Exception):
    """The path is not a file this reader will open."""


def window_bounds(line: int | None, window: int) -> tuple[int, int]:
    if line is None:
        return 1, 2 * window
    return max(1, line - window), line + window


def read_window(
    root: str,
    parts: tuple[str, ...],
    *,
    line: int | None,
    max_bytes: int,
    window: int,
) -> tuple[bytes, int, bool, int]:
    """Return (data, start_line, complete, total_bytes) for one bounded window."""

    if not root.startswith("/"):
        raise WindowUnreadable("repository root must be absolute")
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise WindowUnreadable("this platform cannot read without following links")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptors: list[int] = []
    try:
        directory_fd = os.open(root, directory_flags)
        descriptors.append(directory_fd)
        for part in parts[:-1]:
            directory_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            descriptors.append(directory_fd)
        file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
        descriptors.append(file_fd)
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise WindowUnreadable("path is not a regular file")
        if metadata.st_size <= max_bytes:
            return _whole_file(file_fd, max_bytes=max_bytes, size=metadata.st_size)
        return _line_window(
            file_fd,
            line=line,
            max_bytes=max_bytes,
            window=window,
            size=metadata.st_size,
        )
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _whole_file(file_fd: int, *, max_bytes: int, size: int) -> tuple[bytes, int, bool, int]:
    # A file still being appended can outgrow the size just measured, so the read
    # stays bounded and the extra byte reports that growth honestly.
    chunks: list[bytes] = []
    remaining = max_bytes + 1
    while remaining:
        chunk = os.read(file_fd, min(CHUNK_BYTES, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    return data[:max_bytes], 1, len(data) <= max_bytes, max(size, len(data))


def _line_window(
    file_fd: int,
    *,
    line: int | None,
    max_bytes: int,
    window: int,
    size: int,
) -> tuple[bytes, int, bool, int]:
    first, last = window_bounds(line, window)
    anchor = line or first
    collected: list[bytes] = []
    start = first
    total = 0
    current = 1
    buffer = b""
    value = b""
    while current <= last:
        index = buffer.find(b"\n")
        # One line is held at most to the byte bound, so a file with no delimiter
        # cannot grow this reader's memory with the file.
        if index < 0:
            if len(value) < max_bytes:
                value = (value + buffer)[:max_bytes]
            # Once the cited line alone fills the budget, every later byte of it is
            # discarded anyway, so stop rather than scan to its delimiter.
            exhausted = current >= anchor and len(value) >= max_bytes
            buffer = b"" if exhausted else os.read(file_fd, CHUNK_BYTES)
            if buffer:
                continue
            if not value:
                break
        else:
            if len(value) < max_bytes:
                value = (value + buffer[:index])[:max_bytes]
            buffer = buffer[index + 1 :]
        if current >= first:
            collected.append(value)
            total += len(value) + 1
            # The cited line is the evidence, so leading context is what gives way
            # to the byte bound; the header states the range that survived.
            while total > max_bytes and len(collected) > 1 and start < anchor:
                total -= len(collected.pop(0)) + 1
                start += 1
            if total > max_bytes and start >= anchor:
                break
        value = b""
        current += 1
    return b"\n".join(collected)[:max_bytes], start, False, size


def main(argv: list[str]) -> int:
    """Write one JSON header line and the window bytes, for the shipped form."""

    root, relative, limit, line, window = argv
    parts = tuple(relative.split("/"))
    if not relative or relative.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        return UNREADABLE_EXIT
    try:
        data, start_line, complete, total_bytes = read_window(
            root,
            parts,
            line=int(line) or None,
            max_bytes=int(limit),
            window=int(window),
        )
    except FileNotFoundError:
        return MISSING_EXIT
    except (WindowUnreadable, NotADirectoryError, OSError):
        return UNREADABLE_EXIT
    header = {"start_line": start_line, "complete": complete, "total_bytes": total_bytes}
    sys.stdout.write(json.dumps(header) + "\n")
    sys.stdout.flush()
    sys.stdout.buffer.write(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
