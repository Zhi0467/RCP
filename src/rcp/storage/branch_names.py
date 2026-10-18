"""Project-wide branch labels and permanent name reservations."""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import UTC, datetime

from rcp.core.graph_targets import graph_target_json
from rcp.core.transition_models import GraphHeadRef, GraphTargetRef
from rcp.storage.mixin_base import StoreMixinBase
from rcp.storage.models import EpisodeRecord, GraphBranchRecord
from rcp.storage.provisioning import ProjectProvisioningStoreMixin


class BranchNameInvalid(ValueError):
    """The requested branch label violates the public naming contract."""


class BranchNameTaken(ValueError):
    """Another branch owns or previously used this name."""


def validate_branch_name(name: str) -> str:
    if name == "main" or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}[a-z0-9]", name) is None:
        raise BranchNameInvalid("Use 2–32 lowercase letters, digits, or hyphens; main is reserved.")
    return name


def normalize_branch_name(name: object) -> str | None:
    if name is None:
        return None
    if not isinstance(name, str):
        raise BranchNameInvalid("A branch name must be text.")
    return None if name == "" else validate_branch_name(name)


def migrate_branch_names(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS branch_names (
        project_id TEXT NOT NULL, branch_id TEXT NOT NULL, name TEXT NOT NULL,
        PRIMARY KEY(project_id, branch_id), UNIQUE(project_id, name)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS branch_name_reservations (
        project_id TEXT NOT NULL, name TEXT NOT NULL, branch_id TEXT NOT NULL,
        PRIMARY KEY(project_id, name)
    )""")


def _fallback(mode: str, created_at: str, occupied: set[str]) -> str:
    created = datetime.fromisoformat(created_at).astimezone(UTC)
    month = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
    # Named for the launch mode, not a node type: experiment_loop -> experiment.
    prefix = "research" if mode == "auto_research" else mode.removesuffix("_loop")
    base = f"{prefix}-{month[created.month - 1]}-{created.day:02d}"
    name, suffix = base, 2
    while name in occupied:
        name = f"{base}-{suffix}"
        suffix += 1
    return name


def _legacy_branch_names(connection: sqlite3.Connection, project_id: str) -> dict[str, str]:
    """Resolve every root, including archived roots, in deterministic creation order."""
    rows = connection.execute(
        "SELECT episode_id, mode, created_at FROM episodes WHERE project_id = ? "
        "AND json_extract(graph_target_json, '$.kind') = 'branch' "
        "AND json_extract(graph_target_json, '$.branch_id') = episode_id "
        "AND NOT EXISTS (SELECT 1 FROM graph_branches g WHERE g.project_id=episodes.project_id "
        "AND g.branch_id=episodes.episode_id)",
        (project_id,),
    ).fetchall()
    stored = dict(
        connection.execute(
            "SELECT branch_id, name FROM branch_names WHERE project_id = ?", (project_id,)
        ).fetchall()
    )
    occupied = (
        {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM branch_name_reservations WHERE project_id = ?", (project_id,)
            )
        }
        | set(stored.values())
        | set(resolve_branch_names(connection, project_id).values())
    )
    names: dict[str, str] = {}
    for row in sorted(
        rows,
        key=lambda row: (
            datetime.fromisoformat(row["created_at"]).astimezone(UTC),
            row["episode_id"],
        ),
    ):
        branch_id = str(row["episode_id"])
        name = stored.get(branch_id)
        if name is None:
            name = _fallback(row["mode"], row["created_at"], occupied)
        names[branch_id] = name
        occupied.add(name)
    return names


def migrate_graph_branches(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE graph_branches (
        project_id TEXT NOT NULL, branch_id TEXT NOT NULL, owner_episode_id TEXT,
        parent_target_json TEXT NOT NULL, parent_revision INTEGER NOT NULL CHECK(parent_revision >= 0),
        parent_transition_id TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
        name TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0, 1)),
        auto_archive INTEGER NOT NULL DEFAULT 0 CHECK(auto_archive IN (0, 1)),
        PRIMARY KEY(project_id, branch_id), UNIQUE(project_id, name)
    )""")
    connection.execute(
        "CREATE INDEX graph_branches_owner ON graph_branches(project_id, owner_episode_id)"
    )
    backfill_graph_branches(connection)
    from rcp.storage.resource_versions import migrate_graph_branch_versions

    migrate_graph_branch_versions(connection)


def backfill_graph_branches(connection: sqlite3.Connection) -> None:
    """Retain old-writer branch identity and computed names without rewriting episodes."""
    projects = connection.execute("SELECT DISTINCT project_id FROM episodes").fetchall()
    for project in projects:
        project_id = str(project[0])
        names = _legacy_branch_names(connection, project_id)
        for branch_id, name in names.items():
            row = connection.execute(
                "SELECT e.*, json_extract(s.state_json, '$.graph_archived') AS archived "
                "FROM episodes e LEFT JOIN episode_isolation_states s "
                "ON s.project_id=e.project_id AND s.owner_episode_id=COALESCE(e.isolation_owner_episode_id,e.episode_id) "
                "WHERE e.project_id=? AND e.episode_id=?",
                (project_id, branch_id),
            ).fetchone()
            head = GraphHeadRef.model_validate_json(row["graph_base_head_json"])
            connection.execute(
                "INSERT OR IGNORE INTO graph_branches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    project_id,
                    branch_id,
                    branch_id,
                    graph_target_json(head.target),
                    head.revision,
                    head.transition_id,
                    row["root_operation_id"] or f"episode:{branch_id}",
                    row["created_at"],
                    name,
                    int(bool(row["archived"])),
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO branch_names VALUES (?, ?, ?)", (project_id, branch_id, name)
            )
            connection.execute(
                "INSERT OR IGNORE INTO branch_name_reservations VALUES (?, ?, ?)",
                (project_id, name, branch_id),
            )


def resolve_branch_names(connection: sqlite3.Connection, project_id: str) -> dict[str, str]:
    return dict(
        connection.execute(
            "SELECT branch_id, name FROM graph_branches WHERE project_id=? ORDER BY created_at, branch_id",
            (project_id,),
        ).fetchall()
    )


def _chain_root(connection: sqlite3.Connection, project_id: str, episode_id: str) -> str:
    seen: set[str] = set()
    while episode_id not in seen:
        seen.add(episode_id)
        row = connection.execute(
            "SELECT continues_episode_id FROM episodes WHERE project_id=? AND episode_id=?",
            (project_id, episode_id),
        ).fetchone()
        if row is None:
            raise KeyError(episode_id)
        if row[0] is None:
            return episode_id
        episode_id = str(row[0])
    raise ValueError("episode continuation cycle")


def _branch_record(row: sqlite3.Row) -> GraphBranchRecord:
    data = dict(row)
    data["parent_head"] = GraphHeadRef(
        target=GraphTargetRef.model_validate_json(data.pop("parent_target_json")),
        revision=data.pop("parent_revision"),
        transition_id=data.pop("parent_transition_id"),
    )
    return GraphBranchRecord.model_validate(data)


def _insert_graph_branch(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    branch_id: str,
    owner_episode_id: str | None,
    parent_head: GraphHeadRef,
    created_by: str,
    created_at: str,
    name: object = None,
    auto_archive: bool = False,
    mode: str = "fork",
) -> GraphBranchRecord:
    name = normalize_branch_name(name)
    row = connection.execute(
        "SELECT * FROM graph_branches WHERE project_id=? AND branch_id=?", (project_id, branch_id)
    ).fetchone()
    if row is not None:
        stored = _branch_record(row)
        if (
            stored.owner_episode_id != owner_episode_id
            or stored.parent_head != parent_head
            or stored.created_by != created_by
            or (
                name is not None
                and connection.execute(
                    "SELECT 1 FROM branch_name_reservations "
                    "WHERE project_id=? AND branch_id=? AND name=?",
                    (project_id, branch_id, name),
                ).fetchone()
                is None
            )
        ):
            raise ValueError("graph branch identity cannot be replaced")
        return stored
    occupied = set(resolve_branch_names(connection, project_id).values()) | {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM branch_name_reservations WHERE project_id=?", (project_id,)
        )
    }
    if name is None:
        name = _fallback(mode, created_at, occupied)
    elif name in occupied:
        raise BranchNameTaken("That branch name is already taken.")
    record = GraphBranchRecord(
        project_id=project_id,
        branch_id=branch_id,
        owner_episode_id=owner_episode_id,
        parent_head=parent_head,
        created_by=created_by,
        created_at=created_at,
        name=name,
        auto_archive=auto_archive,
    )
    connection.execute(
        "INSERT INTO graph_branches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (
            project_id,
            branch_id,
            owner_episode_id,
            graph_target_json(parent_head.target),
            parent_head.revision,
            parent_head.transition_id,
            created_by,
            created_at,
            name,
            int(auto_archive),
        ),
    )
    connection.execute(
        "INSERT INTO branch_name_reservations VALUES (?, ?, ?)", (project_id, name, branch_id)
    )
    return record


def reserve_branch_name(
    connection: sqlite3.Connection, record: EpisodeRecord, requested: object = None
) -> None:
    """Create the root's branch record within its episode admission transaction."""
    branch_id = record.graph_target.branch_id
    if branch_id is None:
        return
    if (
        connection.execute(
            "SELECT 1 FROM graph_branches WHERE project_id=? AND branch_id=?",
            (record.project_id, branch_id),
        ).fetchone()
        is not None
    ):
        return
    if record.continues_episode_id is not None or (
        record.isolation_owner_episode_id is not None
        and record.isolation_owner_episode_id != record.episode_id
    ):
        raise ValueError("episode branch is unavailable")
    if record.graph_base_head is None:
        raise ValueError("a branch requires its immutable parent head")
    _insert_graph_branch(
        connection,
        project_id=record.project_id,
        branch_id=branch_id,
        owner_episode_id=record.episode_id,
        parent_head=record.graph_base_head,
        created_by=f"orchestrator:{record.root_operation_id or record.episode_id}",
        created_at=record.created_at,
        name=requested,
        mode=record.mode,
    )


class BranchNameStoreMixin(StoreMixinBase):
    def create_graph_branch(
        self,
        *,
        project_id: str,
        branch_id: str,
        owner_episode_id: str | None,
        parent_head: GraphHeadRef,
        created_by: str,
        created_at: str | None = None,
        name: str | None = None,
        auto_archive: bool = False,
    ) -> GraphBranchRecord:
        identity = uuid.UUID(branch_id)
        if identity.version != 4 or str(identity) != branch_id:
            raise ValueError("a graph branch requires a canonical UUID4")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            ProjectProvisioningStoreMixin._require_project_accepts_new_work(connection, project_id)
            if owner_episode_id is not None:
                owner_episode_id = _chain_root(connection, project_id, owner_episode_id)
            return _insert_graph_branch(
                connection,
                project_id=project_id,
                branch_id=branch_id,
                owner_episode_id=owner_episode_id,
                parent_head=parent_head,
                created_by=created_by,
                created_at=created_at or self.now(),
                name=name,
                auto_archive=auto_archive,
            )

    def graph_branch(self, project_id: str, branch_id: str) -> GraphBranchRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM graph_branches WHERE project_id=? AND branch_id=?",
                (project_id, branch_id),
            ).fetchone()
        return _branch_record(row) if row is not None else None

    def graph_branches(self, project_id: str) -> list[GraphBranchRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM graph_branches WHERE project_id=? ORDER BY created_at, branch_id",
                (project_id,),
            ).fetchall()
        return [_branch_record(row) for row in rows]

    def branches_owned_by(self, project_id: str, episode_id: str) -> list[GraphBranchRecord]:
        with self.connection() as connection:
            owner = _chain_root(connection, project_id, episode_id)
            rows = connection.execute(
                "SELECT * FROM graph_branches WHERE project_id=? AND owner_episode_id=? ORDER BY created_at, branch_id",
                (project_id, owner),
            ).fetchall()
        return [_branch_record(row) for row in rows]

    def branch_owner(self, project_id: str, branch_id: str) -> str | None:
        record = self.graph_branch(project_id, branch_id)
        if record is None:
            raise KeyError(branch_id)
        return record.owner_episode_id

    def branch_names(self, project_id: str) -> dict[str, str]:
        with self.connection() as connection:
            connection.execute("BEGIN")
            return resolve_branch_names(connection, project_id)

    def rename_branch(self, project_id: str, branch_id: str, name: str) -> str:
        name = validate_branch_name(name)
        if branch_id == "main":
            raise BranchNameInvalid("main cannot be renamed.")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            ProjectProvisioningStoreMixin._require_project_accepts_new_work(connection, project_id)
            names = resolve_branch_names(connection, project_id)
            if branch_id not in names:
                raise KeyError(branch_id)
            if names[branch_id] == name:
                return name
            if (
                name in names.values()
                or connection.execute(
                    "SELECT 1 FROM branch_name_reservations"
                    " WHERE project_id = ? AND name = ? AND branch_id != ?",
                    (project_id, name, branch_id),
                ).fetchone()
                is not None
            ):
                raise BranchNameTaken("That branch name is already taken.")
            connection.execute(
                "UPDATE graph_branches SET name=? WHERE project_id=? AND branch_id=?",
                (name, project_id, branch_id),
            )
            # Retain the old computed name too: legacy neighbors must not shift on rename.
            connection.execute(
                "INSERT OR IGNORE INTO branch_name_reservations VALUES (?, ?, ?)",
                (project_id, names[branch_id], branch_id),
            )
            connection.execute(
                "INSERT OR IGNORE INTO branch_name_reservations VALUES (?, ?, ?)",
                (project_id, name, branch_id),
            )
        return name
