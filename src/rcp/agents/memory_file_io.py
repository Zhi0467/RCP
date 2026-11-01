"""Memory filesystem operations, shipped unchanged to the execution host."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
import tempfile
from contextlib import suppress
from pathlib import Path


def safe_component(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", value):
        raise ValueError("invalid memory path component")
    return value


def ensure_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("memory directory cannot be a symlink")
    if not path.exists():
        ensure_directory(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise ValueError("memory path is not a directory")


def memory_root(data_dir: Path, project_id: str) -> Path:
    root = data_dir / "memory" / safe_component(project_id)
    ensure_directory(root.parent)
    ensure_directory(root)
    return root


def _write(path: Path, content: str, *, replace: bool) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("memory note must be a regular file")
    descriptor, temporary = tempfile.mkstemp(prefix=".memory-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            with suppress(FileExistsError):
                os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def prepare_files(root: Path, lessons: list[dict] | None) -> int | None:
    shared = root / "shared"
    notes = shared / "notes"
    index = shared / "MEMORY.md"
    if lessons is None:
        try:
            # Never follow a redirected directory or index on ordinary launches.
            if not all(stat.S_ISDIR(path.lstat().st_mode) for path in (shared, notes)):
                return None
            metadata = index.lstat()
            return metadata.st_size if stat.S_ISREG(metadata.st_mode) else None
        except OSError:
            return None
    ensure_directory(shared)
    ensure_directory(notes)
    ordered = sorted(
        lessons,
        key=lambda row: (
            bool(row["human_owned"]),
            str(row["updated_at"]),
            str(row["lesson_id"]),
        ),
        reverse=True,
    )
    lines = []
    for row in ordered:
        lesson_id = safe_component(str(row["lesson_id"]))
        text = str(row["text"])
        title = next(
            (line.strip().lstrip("#").strip() for line in text.splitlines() if line.strip()),
            f"Lesson {lesson_id}",
        )
        title = title.replace("[", "").replace("]", "")
        name = f"lesson-{lesson_id}.md"
        _write(notes / name, f"# {title}\norigin: lesson {lesson_id}\n\n{text}\n", replace=False)
        lines.append(f"- [{title}](notes/{name}) — Migrated operational note.\n")
    # A fresh project may already have notes supplied by its member.
    _write(index, "".join(lines), replace=bool(ordered))
    if index.is_symlink() or not index.is_file():
        raise ValueError("memory index must be a regular file")
    return index.lstat().st_size


def change_project_files(data_dir: Path, project_id: str, destination: str | None) -> None:
    """Move or remove the whole project tree without following a project symlink."""
    parent = data_dir / "memory"
    if not parent.exists():
        return
    ensure_directory(parent)
    root = parent / safe_component(project_id)
    if destination is not None:
        if not root.exists() and not root.is_symlink():
            return
        target = parent / safe_component(destination)
        if target.exists() or target.is_symlink():
            raise ValueError("memory destination already exists")
        if root.exists() or root.is_symlink():
            root.rename(target)
    elif root.is_symlink() or root.is_file():
        root.unlink()
    elif root.exists():
        shutil.rmtree(root)


def read_indexes(root: Path, user_id: str, max_bytes: int) -> dict:
    """Read just the two authorized indexes, without following directory or file links."""
    import stat

    paths = {
        "shared_index": root / "shared" / "MEMORY.md",
        "private_index": root / "members" / safe_component(user_id) / "MEMORY.md",
    }
    result: dict = {"truncated": False}
    for key, path in paths.items():
        try:
            for directory in path.parents:
                if directory.is_symlink():
                    raise ValueError("memory directory cannot be a symlink")
                if directory == root.parent:
                    break
            # Non-blocking, so a FIFO is refused below instead of waited on.
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            continue
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("memory index must be a regular file")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                data = stream.read(max_bytes + 1)
            result[key] = data[:max_bytes].decode("utf-8", errors="ignore")
            result["truncated"] = result["truncated"] or len(data) > max_bytes
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    return result


def main() -> None:
    request = json.load(sys.stdin)
    data_dir = Path.home() / ".rcp"
    if "destination" in request:
        change_project_files(data_dir, request["project_id"], request["destination"])
        print(json.dumps({}))
        return
    if "read_indexes" in request:
        root = Path.home() / ".rcp" / "memory" / safe_component(request["project_id"])
        print(json.dumps(read_indexes(root, request["user_id"], request["max_bytes"])))
        return
    root = memory_root(data_dir, request["project_id"])
    size = prepare_files(root, request["lessons"]) if "lessons" in request else 0
    print(json.dumps({"root": str(root), "index_bytes": size}))


if __name__ == "__main__":
    main()
