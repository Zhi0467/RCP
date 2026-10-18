"""Batched display invalidation: indexed SQLite reads, never a project open."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal, Self, get_args

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, model_validator

from rcp.api.dependencies import (
    SyncCoordinatorDependency,
    _api_services,
    get_catalog,
    get_identity_access,
    get_store,
)
from rcp.api.identity import IdentityAccess
from rcp.limits import TASK_PROGRESS_REFRESH_SECONDS
from rcp.projects import ProjectCatalog
from rcp.storage import AppStore

ResourceName = Literal[
    "tasks.status",
    "tasks.progress",
    "usage",
    "watchers",
    "chats",
    "questions",
    "consolidation",
    "digest",
    "lessons",
    "bulletin",
    "episodes",
    "refs",
    "artifacts",
    "settings",
    "terminals",
    "members",
    "paper",
    "graph",
    "chat",
    "task",
    "episode",
]
RESOURCE_NAMES = frozenset(get_args(ResourceName))
_PROCESS_EPOCH = uuid.uuid4().hex
# Change when the projection/token contract changes, independently of DB migrations.
_PROJECTION_VERSION = 1
router = APIRouter()
CatalogDependency = Annotated[ProjectCatalog, Depends(get_catalog)]
IdentityDependency = Annotated[IdentityAccess, Depends(get_identity_access)]
_TIMED = frozenset({"tasks.progress", "task"})
StoreDependency = Annotated[AppStore, Depends(get_store)]


def watermark_epoch(database_epoch: str) -> str:
    """Shared restart, restore, and projection epoch for display caches."""
    return f"{_PROCESS_EPOCH}:{_PROJECTION_VERSION}:{database_epoch}"


def graph_head_token(epoch: str, project_id: str, ref: str, generation: str) -> str:
    """Identity of a published graph target, shared by watermarks and live pages."""
    return hashlib.sha256(json.dumps([epoch, project_id, ref, generation]).encode()).hexdigest()


def live_graph_head(
    store: AppStore, catalog: ProjectCatalog, project_id: str, branch_id: str | None
) -> str:
    with store.connection() as connection:
        epoch = connection.execute("SELECT epoch FROM resource_watermark_epoch").fetchone()[0]
    ref = f"branch:{branch_id}" if branch_id else "main"
    generation = catalog.content_generations(project_id, {f"graph:{ref}"})[f"graph:{ref}"]
    return graph_head_token(watermark_epoch(epoch), project_id, ref, generation)


class ResourceKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ResourceName
    ref: str | None = None
    entity: str | None = None

    @model_validator(mode="after")
    def scoped_key(self) -> Self:
        if self.name == "graph":
            if (
                self.entity is not None
                or not self.ref
                or not (self.ref == "main" or (self.ref.startswith("branch:") and self.ref[7:]))
            ):
                raise ValueError("graph requires main or branch:<id> ref and no entity")
        elif self.name in {"chat", "task", "episode"}:
            if not self.entity or self.ref is not None:
                raise ValueError("entity resource requires an entity and no ref")
        elif self.ref is not None or self.entity is not None:
            raise ValueError("project resource has no ref or entity")
        return self

    def database_entity(self) -> str:
        return self.entity or self.ref or ""

    def content_family(self) -> str | None:
        if self.name == "graph":
            return f"graph:{self.ref}"
        if self.name == "chat":
            return f"chat:{self.entity}"
        if self.name == "terminals":
            return "settings"
        # Episode views summarize their branch head and merge eligibility.
        if self.name in {"episodes", "episode"}:
            return "refs"
        if self.name in {"chats", "paper", "settings", "refs"}:
            return self.name
        return None


class OpenProject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    current_ref: str = "main"
    resources: list[ResourceKey]


class WatermarkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str
    open: list[OpenProject]
    due: list[str]
    activated: str | None = None

    @model_validator(mode="after")
    def unique_projects(self) -> Self:
        if len({project.project_id for project in self.open}) != len(self.open):
            raise ValueError("open projects must be unique")
        return self


class ResourceVersion(ResourceKey):
    version: str
    valid_until: str | None


class SyncHealth(BaseModel):
    last_completed_at: str | None
    status: Literal["ok", "syncing", "error", "local"]
    error: str | None


class ProjectWatermarks(BaseModel):
    requested_id: str
    project_id: str
    resources: list[ResourceVersion]
    sync: SyncHealth


class WatermarkResponse(BaseModel):
    epoch: str
    as_of: str
    projects: list[ProjectWatermarks]
    unavailable: list[str]


@router.post("/api/resource-watermarks")
def resource_watermarks(
    body: WatermarkRequest,
    request: Request,
    *,
    catalog: CatalogDependency,
    identity: IdentityDependency,
    store: StoreDependency,
    coordinator: SyncCoordinatorDependency,
) -> WatermarkResponse:
    member = identity.acting_user(request)
    as_of = datetime.fromisoformat(store.now())
    requested = list(dict.fromkeys([*(item.project_id for item in body.open), *body.due]))
    subscriptions = {item.project_id: item.resources for item in body.open}
    with store.connection() as connection:
        # A single read transaction fences membership, counters, and deadlines.
        connection.execute("BEGIN")
        authorized = connection.execute(
            """
            SELECT requested.value AS requested_id,
                   CASE WHEN member.user_id IS NOT NULL AND user.user_id IS NOT NULL
                        THEN project.project_id END AS project_id,
                   epoch.epoch
            FROM resource_watermark_epoch AS epoch
            LEFT JOIN json_each(?) AS requested ON 1
            LEFT JOIN project_aliases AS alias ON alias.alias_id = requested.value
            LEFT JOIN projects AS project
              ON project.project_id = COALESCE(alias.canonical_project_id, requested.value)
             AND project.retired_at IS NULL
            LEFT JOIN project_members AS member
              ON member.project_id = project.project_id AND member.user_id = ?
            LEFT JOIN space_users AS user ON user.user_id = member.user_id
              AND user.removal_started_at IS NULL AND user.removed_at IS NULL
            """,
            (json.dumps(requested), member.user_id),
        ).fetchall()
        aliases = {
            row["requested_id"]: row["project_id"] for row in authorized if row["project_id"]
        }
        epoch = watermark_epoch(authorized[0]["epoch"])
        due = list(dict.fromkeys(body.due))
        keys = [
            (requested_id, aliases[requested_id], key)
            for requested_id in due
            if requested_id in aliases
            for key in subscriptions.get(requested_id, [])
        ]
        encoded = json.dumps(
            [
                [index, project_id, key.name, key.database_entity()]
                for index, (_, project_id, key) in enumerate(keys)
            ]
        )
        counters = connection.execute(
            """
            WITH requested AS (
              SELECT value ->> '$[0]' AS id, value ->> '$[1]' AS scope,
                     value ->> '$[2]' AS resource, value ->> '$[3]' AS entity
              FROM json_each(?)
            ), scopes AS (
              SELECT id, scope, resource, entity FROM requested
              UNION SELECT id, '', resource, entity FROM requested
              UNION SELECT id, scope, resource, '' FROM requested
              UNION SELECT id, '', resource, '' FROM requested
              UNION SELECT id, scope, resource, ? FROM requested WHERE resource='chats'
            )
            SELECT scopes.id, COALESCE(SUM(version.generation), 0) AS generation
            FROM scopes LEFT JOIN resource_versions AS version
              ON version.scope = scopes.scope AND version.resource = scopes.resource
             AND version.entity = scopes.entity
            GROUP BY scopes.id
            """,
            (encoded, f"member:{member.user_id}"),
        ).fetchall()
        deadlines = connection.execute(
            """
            WITH requested AS (
              SELECT value ->> '$[0]' AS id, value ->> '$[1]' AS scope,
                     value ->> '$[2]' AS resource, value ->> '$[3]' AS entity
              FROM json_each(?)
            ), scopes AS (
              SELECT id, scope, resource, entity FROM requested
              UNION SELECT id, '', resource, entity FROM requested
              UNION SELECT id, scope, resource, '' FROM requested
              UNION SELECT id, '', resource, '' FROM requested
            ), boundaries AS (
              SELECT id,
                (SELECT MAX(deadline) FROM resource_deadlines AS d
                 WHERE d.scope = scopes.scope AND d.resource = scopes.resource
                   AND d.entity = scopes.entity AND d.deadline <= ?) AS elapsed,
                (SELECT MIN(deadline) FROM resource_deadlines AS d
                 WHERE d.scope = scopes.scope AND d.resource = scopes.resource
                   AND d.entity = scopes.entity AND d.deadline > ?) AS upcoming
              FROM scopes
            )
            SELECT id, MAX(elapsed) AS elapsed, MIN(upcoming) AS upcoming
            FROM boundaries GROUP BY id
            """,
            (encoded, as_of.timestamp(), as_of.timestamp()),
        ).fetchall()
        # Elapsed time and heuristic progress come from the clock while a task runs.
        timed = sorted({project_id for _, project_id, key in keys if key.name in _TIMED})
        running = (
            {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT run.project_id FROM json_each(?) AS project "
                    "JOIN graph_runs AS run ON run.project_id = project.value "
                    "AND run.status IN ('queued', 'running', 'pausing')",
                    (json.dumps(timed),),
                )
            }
            if timed
            else set()
        )
    generations = {row["id"]: row["generation"] for row in counters}
    boundaries = {row["id"]: (row["elapsed"], row["upcoming"]) for row in deadlines}
    open_ids = {aliases[item.project_id] for item in body.open if item.project_id in aliases}
    activated = aliases.get(body.activated) if body.activated is not None else None
    coordinator.note_demand(body.client_id, open_ids, activated if activated in open_ids else None)
    families: dict[str, set[str]] = {}
    for _, project_id, key in keys:
        if (family := key.content_family()) is not None:
            families.setdefault(project_id, set()).add(family)
    content = {
        project_id: catalog.content_generations(project_id, names)
        for project_id, names in families.items()
    }
    progress_tick = int(as_of.timestamp() // TASK_PROGRESS_REFRESH_SECONDS)
    health = {
        project_id: SyncHealth.model_validate(coordinator.sync_health(project_id))
        for project_id in {aliases[requested_id] for requested_id in due if requested_id in aliases}
    }
    resources: dict[str, list[ResourceVersion]] = {}
    for index, (requested_id, project_id, key) in enumerate(keys):
        elapsed, upcoming = boundaries[index]
        family = key.content_family()
        file_version = content[project_id][family] if family is not None else ""
        if key.name == "graph":
            assert key.ref is not None
            file_version = graph_head_token(epoch, project_id, key.ref, file_version)
        if key.name == "terminals":
            runtime_version = _api_services(request).terminals.resource_generation(project_id)
        elif key.name in _TIMED and project_id in running:
            runtime_version = progress_tick
        else:
            runtime_version = ""
        # The graph snapshot embeds canonical reachability, which moves with
        # sync errors while the published content generation stays put.
        reachable = health[project_id].error is None if key.name == "graph" else None
        token = hashlib.sha256(
            json.dumps(
                [
                    epoch,
                    member.user_id,
                    project_id,
                    key.name,
                    key.ref,
                    key.entity,
                    generations[index],
                    elapsed,
                    file_version,
                    runtime_version,
                    reachable,
                ]
            ).encode()
        ).hexdigest()
        resources.setdefault(requested_id, []).append(
            ResourceVersion(
                **key.model_dump(),
                version=token,
                valid_until=datetime.fromtimestamp(upcoming, UTC).isoformat()
                if upcoming is not None
                else None,
            )
        )
    return WatermarkResponse(
        epoch=epoch,
        as_of=as_of.isoformat(),
        projects=[
            ProjectWatermarks(
                requested_id=project_id,
                project_id=aliases[project_id],
                resources=resources.get(project_id, []),
                sync=health[aliases[project_id]],
            )
            for project_id in due
            if project_id in aliases
        ],
        unavailable=[project_id for project_id in due if project_id not in aliases],
    )
