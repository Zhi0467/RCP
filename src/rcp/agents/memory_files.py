"""Project memory lives on its base, outside graph history and task stages."""

from __future__ import annotations

from pathlib import Path, PurePath
from typing import TYPE_CHECKING

from rcp.agents.memory_file_io import (
    change_project_files,
    memory_root,
    prepare_files,
    safe_component,
)
from rcp.keyed_locks import KeyedLocks
from rcp.limits import MEMORY_MIGRATED_TITLE_MAX_CHARS
from rcp.transport.run_stage import RemoteRunStage

if TYPE_CHECKING:
    from rcp.storage import AppStore
    from rcp.storage.models import AgentTaskRecord, ProjectRecord


_MEMORY_LOCKS = KeyedLocks()


def project_memory_root(
    data_dir: Path, project_id: str, remote_stage: RemoteRunStage | None = None
) -> Path:
    """Create the project root on the base account, using the existing transport."""
    safe_component(project_id)
    if remote_stage is not None:
        return Path(remote_stage.project_memory({"project_id": project_id})["root"])
    return memory_root(data_dir.expanduser().resolve(), project_id)


def task_has_project_memory(task: AgentTaskRecord | None) -> bool:
    """One admission rule for memory prompts and writable roots."""
    return task is not None and task.kind in {"node_chat", "project_chat", "auto_research"}


def change_project_memory(data_dir: Path, project: ProjectRecord, destination: str | None) -> None:
    if project.state_remote:
        host, separator, _path = project.state_location.partition(":/")
        if not separator:
            raise ValueError("Remote project state location has no base host")
        RemoteRunStage(host).project_memory(
            {"project_id": project.project_id, "destination": destination}
        )
    else:
        change_project_files(data_dir, project.project_id, destination)


def finish_project_memory_rename(store: AppStore, project: ProjectRecord) -> None:
    """Retry alias moves after the identity commit, before creating the new root."""
    with store.connection() as connection:
        if connection.execute(
            "SELECT 1 FROM memory_migrations WHERE project_id = ?", (project.project_id,)
        ).fetchone():
            return
        aliases = connection.execute(
            "SELECT alias_id FROM project_aliases WHERE canonical_project_id = ?",
            (project.project_id,),
        ).fetchall()
    for row in aliases:
        old_id = row[0]
        change_project_memory(
            store.path.parent, project.model_copy(update={"project_id": old_id}), project.project_id
        )
        with store.connection() as connection:
            connection.execute(
                "UPDATE memory_migrations SET project_id = ? WHERE project_id = ?",
                (project.project_id, old_id),
            )


def member_memory_root(root: PurePath, user_id: str) -> Path:
    """Return a member's private folder; only the thread owner creates it."""
    return Path(root) / "members" / safe_component(user_id)


def prepare_project_memory(
    store: AppStore, project_id: str, remote_stage: RemoteRunStage | None = None
) -> tuple[str, int | None]:
    """Migrate once, committing the marker only after the base confirms all writes.

    The process owns its data directory; a project lock serializes first launches
    without holding a SQLite writer lock across remote I/O. A failed base call
    leaves no marker, so a retry finishes create-if-absent note publication.
    """
    safe_component(project_id)
    with _MEMORY_LOCKS(f"{store.path.resolve()}:{project_id}"):
        with store.connection() as connection:
            pending = connection.execute(
                "SELECT 1 FROM memory_deletions WHERE project_id = ? OR project_id IN "
                "(SELECT alias_id FROM project_aliases WHERE canonical_project_id = ?)",
                (project_id, project_id),
            ).fetchone()
        if pending:
            raise ValueError("Project memory deletion remains pending; retry cleanup before launch")
        project = store.project(project_id)
        if project is not None:
            finish_project_memory_rename(store, project)
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
            result = remote_stage.project_memory(
                {
                    "project_id": project_id,
                    "lessons": lessons,
                    "title_max_chars": MEMORY_MIGRATED_TITLE_MAX_CHARS,
                }
            )
            root, size = Path(result["root"]), result["index_bytes"]
        else:
            root = project_memory_root(store.path.parent, project_id)
            size = prepare_files(root, lessons, MEMORY_MIGRATED_TITLE_MAX_CHARS)
        if not migrated:
            with store.connection() as connection:
                connection.execute(
                    "INSERT INTO memory_migrations VALUES (?,?,?)",
                    (project_id, store.now(), len(lessons or [])),
                )
    return str(root / "shared"), size


def read_project_memory_indexes(
    store: AppStore,
    project_id: str,
    remote_stage: RemoteRunStage | None = None,
) -> dict:
    """Voice reads the shared index on the project's actual base."""
    from rcp import limits
    from rcp.agents.memory_file_io import read_indexes

    safe_component(project_id)
    if remote_stage is not None:
        return remote_stage.project_memory(
            {
                "project_id": project_id,
                "read_indexes": True,
                "max_bytes": limits.MEMORY_INDEX_MAX_BYTES,
            },
        )
    return read_indexes(store.path.parent / "memory" / project_id, limits.MEMORY_INDEX_MAX_BYTES)
