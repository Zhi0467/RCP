"""Read a bounded workspace tree; shipped verbatim to the execution host."""

from __future__ import annotations

import base64
import json
import os
import stat
import sys


def read_tree(
    root: str, name: str, max_files: int, max_file_bytes: int, max_total_bytes: int
) -> list[tuple[str, str]]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    result: list[tuple[str, str]] = []
    total = 0
    directories = 0

    def visit(fd: int, prefix: str) -> None:
        nonlocal total, directories
        # scandir is incremental: even a directory of empty files is bounded.
        with os.scandir(fd) as entries:
            for entry in entries:
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    directories += 1
                    if directories > max_files * 4:
                        raise ValueError("workspace tree exceeds directory limit")
                    child = os.open(entry.name, flags, dir_fd=fd)
                    try:
                        visit(child, relative)
                    finally:
                        os.close(child)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("workspace tree contains a nonregular entry or symlink")
                if len(result) >= max_files:
                    raise ValueError("workspace tree exceeds file count limit")
                child = os.open(entry.name, file_flags, dir_fd=fd)
                try:
                    info = os.fstat(child)
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError("workspace tree entry is not a regular file")
                    if info.st_nlink > 1:
                        raise ValueError("workspace tree contains a hard-linked file")
                    limit = min(max_file_bytes, max_total_bytes - total)
                    if info.st_size > limit:
                        raise ValueError("workspace tree exceeds byte limit")
                    chunks: list[bytes] = []
                    size = 0
                    while True:
                        chunk = os.read(child, min(1024 * 1024, limit - size + 1))
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > limit:
                            raise ValueError("workspace tree exceeds byte limit")
                        chunks.append(chunk)
                    total += size
                    result.append((relative, base64.b64encode(b"".join(chunks)).decode("ascii")))
                finally:
                    os.close(child)

    descriptors: list[int] = []
    try:
        fd = os.open(root, flags)
        descriptors.append(fd)
        for part in ("workspace", *name.split("/")):
            fd = os.open(part, flags, dir_fd=fd)
            descriptors.append(fd)
        visit(fd, "")
        return result
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def main() -> None:
    try:
        result = read_tree(sys.argv[1], sys.argv[2], *(int(x) for x in sys.argv[3:6]))
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(45) from exc
    print(json.dumps(result))


if __name__ == "__main__":
    main()
