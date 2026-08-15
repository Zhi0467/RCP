"""Advisory-lock holder for the canonical state repository.

RCP ships this module's *own source* to the execution machine and runs it with
``python -c``; nothing in RCP imports it. Keeping it a real module instead of a
string literal is what lets ruff, the formatter, and ``tests/test_remote_scripts.py``
see it — a hand-transcribed copy is the copy that rots.

Protocol. ``argv[1]`` is the lock path. The holder prints one status word on
stdout — ``legacy-directory``, ``unsafe-entry``, or ``error`` and exits, or
``contended`` followed by ``acquired`` once the wait finishes. It then reads one
JSON command per line from stdin and prints one JSON response per line, holding
the lock for as long as stdin stays open.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path


def relative_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe relative path: {value}")
    return path


def apply_staged(command: dict, lock_path: str) -> dict:
    root = Path(command["root"])
    stage = Path(command["stage"])
    if (
        Path(lock_path).name != ".refresh.lock"
        or root != Path(lock_path).parent
        or not root.is_absolute()
        or stage.parent != root / ".publish"
    ):
        raise ValueError("invalid canonical root or staging directory")
    paths = [relative_path(value) for value in command["paths"]]
    commit_value = command.get("commit")
    commit = relative_path(commit_value) if commit_value is not None else None
    commit_is_directory = bool(command.get("commit_is_directory"))
    ordinary = [
        path
        for path in paths
        if commit is None
        or (path != commit and not (commit_is_directory and commit in path.parents))
    ]
    commit_target = root / commit if commit is not None else None
    try:
        if commit is not None:
            commit_source = stage / commit
            commit_target.parent.mkdir(parents=True, exist_ok=True)
            if commit_target.exists():
                if commit_source.exists():
                    if commit_is_directory:
                        shutil.rmtree(commit_source)
                    else:
                        commit_source.unlink()
            elif commit_source.exists():
                os.replace(commit_source, commit_target)
            else:
                raise FileNotFoundError(f"missing staged history commit: {commit_source}")
        for path in ordinary:
            source = stage / path
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_file():
                os.replace(source, target)
        shutil.rmtree(stage, ignore_errors=True)
        return {"ok": True, "commit_status": "present" if commit is not None else None}
    except Exception as exc:
        if commit_target is None:
            commit_status = None
        elif commit_is_directory:
            commit_status = "present" if commit_target.is_dir() else "absent"
        else:
            commit_status = "present" if commit_target.is_file() else "absent"
        return {"ok": False, "commit_status": commit_status, "error": str(exc)[:1000]}


def kept_view_candidate(base_name: str, index: int) -> str:
    if index == 1:
        return base_name
    return f"{base_name[:-5]}-{index}.html"


def keep_staged_view(command: dict, lock_path: str) -> dict:
    root = Path(command["root"])
    stage = Path(command["stage"])
    base_name = command["base_name"]
    if (
        Path(lock_path).name != ".refresh.lock"
        or root != Path(lock_path).parent
        or not root.is_absolute()
        or root.name != ".research"
        or stage.parent != root / ".publish"
        or not re.fullmatch(r"view-[0-9]+-[0-9]+", stage.name)
        or not isinstance(base_name, str)
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,238})[.]html", base_name)
    ):
        raise ValueError("invalid result-view root, stage, or base name")
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("safe result-view file operations are unavailable")
    if not stat.S_ISDIR(os.lstat(root).st_mode):
        raise ValueError("canonical root is not a regular directory")
    if not stat.S_ISDIR(os.lstat(stage.parent).st_mode):
        raise ValueError("result-view staging parent is not a regular directory")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    stage_fd = os.open(stage, directory_flags)
    try:
        if os.listdir(stage_fd) != ["content.html"]:
            raise ValueError("result-view stage does not contain exactly content.html")
        source_fd = os.open("content.html", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=stage_fd)
        try:
            source_info = os.fstat(source_fd)
            if not stat.S_ISREG(source_info.st_mode):
                raise ValueError("staged result view is not a regular file")
            if source_info.st_size > 16 * 1024 * 1024:
                raise ValueError("staged result view exceeds the per-file limit")

            repository_fd = os.open(root.parent, directory_flags)
            try:
                with contextlib.suppress(FileExistsError):
                    os.mkdir("views", 0o755, dir_fd=repository_fd)
                try:
                    views_fd = os.open("views", directory_flags, dir_fd=repository_fd)
                except OSError as exc:
                    raise ValueError("repository views path is not a regular directory") from exc
                try:
                    for index in range(1, 10000):
                        candidate = kept_view_candidate(base_name, index)
                        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                        try:
                            target_fd = os.open(candidate, flags, 0o644, dir_fd=views_fd)
                        except FileExistsError:
                            continue
                        try:
                            bytes_left = 16 * 1024 * 1024
                            while True:
                                chunk = os.read(source_fd, min(1024 * 1024, bytes_left + 1))
                                if not chunk:
                                    break
                                if len(chunk) > bytes_left:
                                    raise ValueError(
                                        "staged result view exceeds the per-file limit"
                                    )
                                bytes_left -= len(chunk)
                                remaining = memoryview(chunk)
                                while remaining:
                                    written = os.write(target_fd, remaining)
                                    if written <= 0:
                                        raise OSError("short result-view write")
                                    remaining = remaining[written:]
                            os.fsync(target_fd)
                        except BaseException:
                            os.close(target_fd)
                            target_fd = -1
                            try:
                                os.unlink(candidate, dir_fd=views_fd)
                                os.fsync(views_fd)
                            except OSError:
                                pass
                            raise
                        finally:
                            if target_fd >= 0:
                                os.close(target_fd)
                        os.fsync(views_fd)
                        os.fsync(repository_fd)
                        return {"ok": True, "name": candidate}
                    raise FileExistsError("too many repository result-view name collisions")
                finally:
                    os.close(views_fd)
            finally:
                os.close(repository_fd)
        finally:
            os.close(source_fd)
    finally:
        with contextlib.suppress(OSError):
            os.unlink("content.html", dir_fd=stage_fd)
        os.close(stage_fd)
        with contextlib.suppress(OSError):
            os.rmdir(stage)


def main() -> None:
    lock_path = sys.argv[1]
    try:
        mode = os.lstat(lock_path).st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None:
        if stat.S_ISDIR(mode):
            # A crashed mkdir-era run leaves its lock directory behind empty, and
            # that artifact is RCP's to clear. rmdir reclaims exactly that case:
            # anything with contents is somebody's state and still refuses.
            try:
                os.rmdir(lock_path)
            except OSError:
                print("legacy-directory", flush=True)
                raise SystemExit(0) from None
        elif not stat.S_ISREG(mode):
            print("unsafe-entry", flush=True)
            raise SystemExit(0)
    try:
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
    except IsADirectoryError:
        print("legacy-directory", flush=True)
        raise SystemExit(0) from None
    except OSError as exc:
        if os.path.lexists(lock_path) and os.path.islink(lock_path):
            print("unsafe-entry", flush=True)
            raise SystemExit(0) from None
        print("error", flush=True)
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        print("unsafe-entry", flush=True)
        raise SystemExit(0)
    with os.fdopen(descriptor, "a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("contended", flush=True)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        print("acquired", flush=True)
        for line in sys.stdin:
            try:
                command = json.loads(line)
                if command.get("op") == "apply":
                    response = apply_staged(command, lock_path)
                elif command.get("op") == "keep-view":
                    response = keep_staged_view(command, lock_path)
                else:
                    raise ValueError("unsupported lock-holder command")
            except Exception as exc:
                response = {"ok": False, "commit_status": None, "error": str(exc)[:1000]}
            print(json.dumps(response, separators=(",", ":")), flush=True)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
