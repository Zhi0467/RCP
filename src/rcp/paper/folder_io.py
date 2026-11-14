"""Paper folder filesystem operations, shipped unchanged to the base machine.

Stdlib only: the same source runs in-process locally and as `python3 -c` over SSH.
Every path walk holds directory descriptors opened with O_NOFOLLOW, so a symlinked
directory, a symlink, a hard link, or a special file is refused or reported, never
followed. A write that replaces an existing file returns the bytes it displaced.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Literal

TEMP_PREFIX = ".rcp-paper-"
IGNORED_NAMES = (
    f"{TEMP_PREFIX}*",
    ".DS_Store",
    "__pycache__",
    "*.swp",
    "*.swo",
    "*.swx",
    "*~",
    ".#*",
    "4913",
    "*.aux",
    "*.log",
    "*.out",
    "*.toc",
    "*.lof",
    "*.lot",
    "*.fls",
    "*.fdb_latexmk",
    "*.synctex.gz",
    "*.synctex(busy)",
    "*.blg",
    "*.bcf",
    "*.run.xml",
    "*.nav",
    "*.snm",
    "*.xdv",
    "*.dvi",
)
_RESERVED_PARTS = {"", ".", "..", ".git", ".publish"}
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_SWAP_FLAG = {"darwin": 2, "linux": 2}
_NO_REPLACE_FLAG = {"darwin": 4, "linux": 1}


class UnsupportedPath(ValueError):
    """The path crosses or names a symlink, hard link, or special file."""


@dataclass(frozen=True)
class Entry:
    path: str
    size: int
    mtime_ns: int
    inode: int
    kind: Literal["file", "unsupported"]


@dataclass(frozen=True)
class WriteResult:
    sha: str | None
    displaced: bytes | None
    displaced_sha: str | None
    conflict: bool


def safe_component(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", value):
        raise ValueError("invalid Paper folder component")
    return value


def paper_path(value: str) -> str:
    """The Paper path rules of `rcp.paper.models.paper_path`, without Pydantic."""
    parts = value.split("/")
    if (
        not value
        or len(value.encode()) > 1024
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in _RESERVED_PARTS or len(part.encode()) > 255 for part in parts)
        or PurePosixPath(value).is_absolute()
    ):
        raise ValueError("Invalid Paper path")
    return value


def paper_root(data_dir: Path, project_id: str) -> Path:
    """`<data_dir>/paper/<project_id>`, created as real directories."""
    root = data_dir / "paper" / safe_component(project_id)
    for directory in (data_dir, root.parent, root):
        directory.mkdir(mode=0o700, exist_ok=True)
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise ValueError("Paper folder cannot be a symlink")
    return root


def ignored(name: str) -> bool:
    return any(fnmatchcase(name, pattern) for pattern in IGNORED_NAMES)


def scan(root: Path) -> list[Entry]:
    entries: list[Entry] = []
    with _directory(root, ()) as descriptor:
        _scan_into(descriptor, "", entries)
    return sorted(entries, key=lambda entry: entry.path)


def read(root: Path, paths: list[str]) -> dict[str, bytes | None]:
    return {path: _read_regular(root, path) for path in paths}


def hash(root: Path, paths: list[str]) -> dict[str, str | None]:
    return {path: None if data is None else _sha(data) for path, data in read(root, paths).items()}


def write(
    root: Path,
    path: str,
    data: bytes,
    expect_sha: str | None,
    *,
    settle_seconds: float = 0.0,
    settle_max_seconds: float = 0.0,
) -> WriteResult:
    """Replace or create one file and return the bytes it displaced.

    A different current file still loses its place; its bytes come back for the
    caller to merge. A swapped-out inode is re-read until its mtime is quiet for
    `settle_seconds` (at most `settle_max_seconds`), so a writer still holding it
    is not lost.
    """
    *parents, name = paper_path(path).split("/")
    with _directory(root, parents, create=True) as directory:
        current = _regular_status(directory, name)
        temporary = _temporary_file(directory, data, current)
        try:
            if current is None:
                try:
                    os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                    os.fsync(directory)
                    return _result(_sha(data), None, expect_sha)
                except FileExistsError:
                    _regular_status(directory, name)
            _rename(directory, temporary, directory, name, _SWAP_FLAG)
            os.fsync(directory)
            displaced = _settled_read(directory, temporary, settle_seconds, settle_max_seconds)
            return _result(_sha(data), displaced, expect_sha)
        finally:
            _unlink_quietly(directory, temporary)


def delete(
    root: Path,
    path: str,
    expect_sha: str | None,
    *,
    settle_seconds: float = 0.0,
    settle_max_seconds: float = 0.0,
) -> WriteResult:
    """Remove one file and return its last bytes; a missing file is already deleted."""
    *parents, name = paper_path(path).split("/")
    with _directory(root, parents) as directory:
        if directory is None or _regular_status(directory, name) is None:
            return _result(None, None, expect_sha)
        temporary = _temporary_name()
        try:
            os.rename(name, temporary, src_dir_fd=directory, dst_dir_fd=directory)
        except FileNotFoundError:
            return _result(None, None, expect_sha)
        try:
            os.fsync(directory)
            displaced = _settled_read(directory, temporary, settle_seconds, settle_max_seconds)
            return _result(None, displaced, expect_sha)
        finally:
            _unlink_quietly(directory, temporary)


def rename(root: Path, source: str, destination: str) -> None:
    """Move one file without ever replacing an existing destination."""
    *source_parents, source_name = paper_path(source).split("/")
    *target_parents, target_name = paper_path(destination).split("/")
    with _directory(root, source_parents) as source_directory:
        if source_directory is None or _regular_status(source_directory, source_name) is None:
            raise FileNotFoundError(source)
        with _directory(root, target_parents, create=True) as target_directory:
            _rename(source_directory, source_name, target_directory, target_name, _NO_REPLACE_FLAG)
            os.fsync(target_directory)
        os.fsync(source_directory)


def _result(sha: str | None, displaced: bytes | None, expect_sha: str | None) -> WriteResult:
    displaced_sha = None if displaced is None else _sha(displaced)
    return WriteResult(sha, displaced, displaced_sha, displaced_sha != expect_sha)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@contextmanager
def _directory(root: Path, parts: Sequence[str], *, create: bool = False) -> Iterator:
    """Yield a descriptor for `root/parts`, or None when absent and not created."""
    try:
        descriptor = os.open(root, _DIRECTORY_FLAGS)
    except OSError as error:
        raise ValueError("Paper folder must be a real directory") from error
    try:
        for part in parts:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=descriptor)
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if create:
                    raise
                os.close(descriptor)
                descriptor = -1
                yield None
                return
            except OSError as error:
                raise UnsupportedPath("Paper directory is a link or not a directory") from error
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _regular_status(directory: int, name: str) -> os.stat_result | None:
    """None when absent; refuses anything but a singly linked regular file."""
    try:
        status = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(status.st_mode) or status.st_nlink > 1:
        raise UnsupportedPath("Paper file is a link or special file")
    return status


def _temporary_name() -> str:
    return f"{TEMP_PREFIX}{secrets.token_hex(8)}"


def _temporary_file(directory: int, data: bytes, current: os.stat_result | None) -> str:
    name = _temporary_name()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(name, flags, 0o666, dir_fd=directory)
    try:
        if current is not None:
            os.fchmod(descriptor, stat.S_IMODE(current.st_mode))
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        _unlink_quietly(directory, name)
        raise
    os.close(descriptor)
    return name


def _rename(source_dir: int, source: str, target_dir: int, target: str, flags: dict) -> None:
    if sys.platform not in flags:
        raise OSError("Paper folder writes require Linux or macOS")
    libc = ctypes.CDLL(None, use_errno=True)
    call = libc.renameatx_np if sys.platform == "darwin" else libc.renameat2
    result = call(
        source_dir,
        os.fsencode(source),
        target_dir,
        os.fsencode(target),
        flags[sys.platform],
    )
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target)


def _unlink_quietly(directory: int, name: str) -> None:
    with suppress(FileNotFoundError):
        os.unlink(name, dir_fd=directory)


def _read_all(descriptor: int) -> bytes:
    chunks = []
    offset = 0
    while chunk := os.pread(descriptor, 1 << 20, offset):
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _signature(status: os.stat_result) -> tuple[int, int, int]:
    return status.st_size, status.st_mtime_ns, status.st_ctime_ns


def _settled_read(directory: int, name: str, settle: float, settle_max: float) -> bytes | None:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        deadline = time.monotonic() + settle_max
        while True:
            before = os.fstat(descriptor)
            data = _read_all(descriptor)
            quiet = time.time() - before.st_mtime_ns / 1e9
            stable = _signature(os.fstat(descriptor)) == _signature(before)
            remaining = deadline - time.monotonic()
            if (stable and quiet >= settle) or remaining <= 0:
                return data
            if stable:
                time.sleep(min(settle - quiet, remaining))
    finally:
        os.close(descriptor)


def _read_regular(root: Path, path: str) -> bytes | None:
    *parents, name = paper_path(path).split("/")
    try:
        with _directory(root, parents) as directory:
            if directory is None:
                return None
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
    except (OSError, UnsupportedPath):
        return None
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink > 1:
            return None
        return _read_all(descriptor)
    finally:
        os.close(descriptor)


def _scan_into(directory: int, prefix: str, entries: list[Entry]) -> None:
    for name in sorted(os.listdir(directory)):
        if ignored(name):
            continue
        path = f"{prefix}{name}"
        try:
            status = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            continue
        try:
            paper_path(path)
            valid = True
        except ValueError:
            valid = False
        if valid and stat.S_ISDIR(status.st_mode):
            try:
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory)
            except OSError:
                continue
            try:
                _scan_into(child, f"{path}/", entries)
            finally:
                os.close(child)
            continue
        supported = valid and stat.S_ISREG(status.st_mode) and status.st_nlink == 1
        entries.append(
            Entry(
                path=path,
                size=status.st_size,
                mtime_ns=status.st_mtime_ns,
                inode=status.st_ino,
                kind="file" if supported else "unsupported",
            )
        )


def _encode(data: bytes | None) -> str | None:
    return None if data is None else base64.b64encode(data).decode("ascii")


def _encoded_result(result: WriteResult) -> dict:
    return {**asdict(result), "displaced": _encode(result.displaced)}


def handle(request: dict, data_dir: Path) -> dict:
    """One JSON request against `<data_dir>/paper/<project_id>`."""
    root = paper_root(data_dir, request["project_id"])
    operation = request["op"]
    settle = {
        "settle_seconds": request.get("settle_seconds", 0.0),
        "settle_max_seconds": request.get("settle_max_seconds", 0.0),
    }
    if operation == "root":
        return {"root": str(root)}
    if operation == "scan":
        return {"entries": [asdict(entry) for entry in scan(root)]}
    if operation == "read":
        return {
            "files": {path: _encode(data) for path, data in read(root, request["paths"]).items()}
        }
    if operation == "hash":
        return {"hashes": hash(root, request["paths"])}
    if operation == "write":
        data = base64.b64decode(request["data"])
        return _encoded_result(write(root, request["path"], data, request["expect_sha"], **settle))
    if operation == "delete":
        return _encoded_result(delete(root, request["path"], request["expect_sha"], **settle))
    if operation == "rename":
        rename(root, request["source"], request["destination"])
        return {}
    raise ValueError(f"unknown Paper folder operation: {operation}")


def main() -> None:
    print(json.dumps(handle(json.load(sys.stdin), Path.home() / ".rcp")))


if __name__ == "__main__":
    main()
