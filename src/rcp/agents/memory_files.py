"""Project memory lives on its base, outside graph history and task stages."""

from __future__ import annotations

import importlib.resources
import json
from pathlib import Path, PurePath
from typing import TYPE_CHECKING

from rcp.agents.memory_file_io import memory_root, prepare_files, safe_component
from rcp.keyed_locks import KeyedLocks
from rcp.transport.run_stage import RemoteRunStage, _ssh_failure

if TYPE_CHECKING:
    from rcp.storage import AppStore


_MEMORY_LOCKS = KeyedLocks()


def _remote_memory(stage: RemoteRunStage, request: dict) -> dict:
    source = (
        importlib.resources.files("rcp.agents")
        .joinpath("memory_file_io.py")
        .read_text(encoding="utf-8")
    )
    result = stage._ssh_bytes(["python3", "-c", source], input_data=json.dumps(request).encode())
    if result.returncode:
        raise _ssh_failure(result, "could not prepare project memory")
    return json.loads(result.stdout)


def project_memory_root(
    data_dir: Path, project_id: str, remote_stage: RemoteRunStage | None = None
) -> Path:
    """Create the project root on the base account, using the existing transport."""
    safe_component(project_id)
    if remote_stage is not None:
        return Path(_remote_memory(remote_stage, {"project_id": project_id})["root"])
    return memory_root(data_dir.expanduser().resolve(), project_id)


def member_memory_root(root: PurePath, user_id: str) -> Path:
    """Return a member's private folder; only the thread owner creates it."""
    return Path(root) / "members" / safe_component(user_id)


def prepare_project_memory(
    store: AppStore, project_id: str, remote_stage: RemoteRunStage | None = None
) -> tuple[str, int]:
    """Migrate once, committing the marker only after the base confirms all writes.

    The process owns its data directory; a project lock serializes first launches
    without holding a SQLite writer lock across remote I/O. A failed base call
    leaves no marker, so a retry finishes create-if-absent note publication.
    """
    safe_component(project_id)
    with _MEMORY_LOCKS(f"{store.path.resolve()}:{project_id}"):
        with store.connection() as connection:
            migrated = connection.execute(
                "SELECT 1 FROM memory_migrations WHERE project_id=?", (project_id,)
            ).fetchone()
            lessons = (
                None
                if migrated
                else [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM operational_lessons WHERE project_id=?", (project_id,)
                    )
                ]
            )
        if remote_stage is not None:
            result = _remote_memory(remote_stage, {"project_id": project_id, "lessons": lessons})
            root, size = Path(result["root"]), int(result["index_bytes"])
        else:
            root = project_memory_root(store.path.parent, project_id)
            size = prepare_files(root, lessons)
        if not migrated:
            with store.connection() as connection:
                connection.execute(
                    "INSERT INTO memory_migrations VALUES (?,?,?)",
                    (project_id, store.now(), len(lessons or [])),
                )
    return str(root / "shared"), size
