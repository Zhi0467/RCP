"""Project memory lives on its base; member memory on the backend. Both stay outside history."""

from __future__ import annotations

import shutil
import stat
from contextlib import suppress
from pathlib import Path, PurePath
from typing import TYPE_CHECKING

from rcp.agents.memory_file_io import (
    change_project_files,
    ensure_directory,
    memory_root,
    prepare_files,
    safe_component,
    shared_folder_usable,
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


def task_member_memory_owner(store: AppStore, task: AgentTaskRecord | None) -> str | None:
    """The member whose private notes this turn uses: only that member's own thread turns."""
    if (
        task is None
        or task.kind not in {"space_chat", "project_chat"}
        or task.episode_id is not None
    ):
        return None
    chat_id = task.request.get("chat_id")
    thread = store.agent_thread(chat_id) if isinstance(chat_id, str) else None
    if thread is None or (thread.kind == "space") != (task.kind == "space_chat"):
        return None
    if thread.kind == "project" and thread.project_id != task.project_id:
        return None
    return thread.user_id


def prepare_member_memory(store: AppStore, user_id: str) -> tuple[str | None, int | None]:
    """Create the member's backend folder; a redirected folder is never named.

    Returns the folder when it and its parents are real directories, and the
    index size when the index is a regular file.
    """
    memory = store.path.parent.expanduser().resolve() / "memory"
    root = member_memory_root(memory, user_id)
    index = root / "MEMORY.md"
    try:
        ensure_directory(root / "notes")
        if not all(
            stat.S_ISDIR(path.lstat().st_mode)
            for path in (memory, root.parent, root, root / "notes")
        ):
            return None, None
        # O_EXCL never follows an existing link, so this only ever creates the index.
        with suppress(FileExistsError):
            index.touch(mode=0o600, exist_ok=False)
        metadata = index.lstat()
    except (OSError, ValueError):
        return None, None
    return str(root), metadata.st_size if stat.S_ISREG(metadata.st_mode) else None


def remove_member_memory(data_dir: Path, user_id: str) -> None:
    """Delete a removed member's backend folder without following a link."""
    root = member_memory_root(data_dir / "memory", user_id)
    if any(path.is_symlink() for path in (root.parent.parent, root.parent)):
        raise ValueError("Member memory must not be reached through a symlink")
    if root.is_symlink() or root.is_file():
        root.unlink()
    elif root.exists():
        shutil.rmtree(root)


def prepare_project_memory(
    store: AppStore, project_id: str, remote_stage: RemoteRunStage | None = None
) -> tuple[str | None, int | None]:
    """Migrate once, committing the marker only after the base confirms all writes.

    The process owns its data directory; a project lock serializes first launches
    without holding a SQLite writer lock across remote I/O. A failed base call
    leaves no marker, so a retry finishes create-if-absent note publication.
    """
    safe_component(project_id)
    with _MEMORY_LOCKS(f"{store.path.resolve()}:{project_id}"):
        project = store.project(project_id)
        location = project.state_location if project else None
        # Only a deletion still owed on this same base could remove the new notes;
        # one left on an old or unreachable base never blocks a launch.
        with store.connection() as connection:
            pending = connection.execute(
                "SELECT 1 FROM memory_deletions WHERE (? IS NULL OR state_location = ?) "
                "AND (project_id = ? OR project_id IN (SELECT alias_id FROM project_aliases "
                "WHERE canonical_project_id = ?))",
                (location, location, project_id, project_id),
            ).fetchone()
        if pending:
            raise ValueError("Project memory deletion remains pending; retry cleanup before launch")
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
            usable = result["shared_usable"]
        else:
            root = project_memory_root(store.path.parent, project_id)
            size = prepare_files(root, lessons, MEMORY_MIGRATED_TITLE_MAX_CHARS)
            usable = shared_folder_usable(root)
        if not migrated:
            with store.connection() as connection:
                connection.execute(
                    "INSERT INTO memory_migrations VALUES (?,?,?)",
                    (project_id, store.now(), len(lessons or [])),
                )
    # A redirected `shared` is never named, so no launch can grant it as a write root.
    return (str(root / "shared") if usable else None), size


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
