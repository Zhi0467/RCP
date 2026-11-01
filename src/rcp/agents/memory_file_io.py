"""Memory filesystem operations, shipped unchanged to the execution host."""

from __future__ import annotations

import json
import os
import re
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


def prepare_files(root: Path, lessons: list[dict] | None) -> int:
    shared = root / "shared"
    notes = shared / "notes"
    ensure_directory(shared)
    ensure_directory(notes)
    index = shared / "MEMORY.md"
    if lessons is not None:
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
            _write(
                notes / name, f"# {title}\norigin: lesson {lesson_id}\n\n{text}\n", replace=False
            )
            lines.append(f"- [{title}](notes/{name}) — Migrated operational note.\n")
        # A fresh project may already have notes supplied by its member.
        _write(index, "".join(lines), replace=bool(ordered))
    elif not index.exists():
        _write(index, "", replace=False)
    if index.is_symlink() or not index.is_file():
        raise ValueError("memory index must be a regular file")
    return index.stat().st_size


def main() -> None:
    request = json.load(sys.stdin)
    root = memory_root(Path.home() / ".rcp", request["project_id"])
    size = prepare_files(root, request["lessons"]) if "lessons" in request else 0
    print(json.dumps({"root": str(root), "index_bytes": size}))


if __name__ == "__main__":
    main()
