"""Personal HTML views bound to the graph of their retained origin task."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from pydantic import BaseModel

from rcp.core.models import AuthorizedHuman
from rcp.core.transition_models import GraphTargetRef
from rcp.limits import RESEARCH_VIEW_PIN_LIMIT
from rcp.storage.artifact_models import Artifact
from rcp.storage.artifacts import ArtifactStoreMixin
from rcp.storage.resource_versions import _bump, _install


class ResearchViewConflict(ValueError):
    def __init__(self, code: str, status: int = 409) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class ResearchViewPin(BaseModel):
    artifact_id: str
    title: str
    position: int


class ResearchViewOffers(BaseModel):
    pin_branch_id: str | None
    can_pin: bool
    pinned: bool


def migrate_research_views(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE research_view_pins (project_id TEXT NOT NULL, user_id TEXT NOT NULL, "
        "branch_id TEXT NOT NULL, artifact_id TEXT NOT NULL, position INTEGER NOT NULL, "
        "created_at TEXT NOT NULL, PRIMARY KEY(project_id,user_id,branch_id,artifact_id))"
    )
    _install(connection, "research_view_pins", [_bump("ROW.project_id", "artifacts")])


def _require_member(connection: sqlite3.Connection, project_id: str, user_id: str) -> None:
    member = connection.execute(
        "SELECT 1 FROM project_members m JOIN projects p ON p.project_id=m.project_id "
        "JOIN space_users u ON u.user_id=m.user_id WHERE m.project_id=? AND m.user_id=? "
        "AND p.retired_at IS NULL AND u.removal_started_at IS NULL AND u.removed_at IS NULL",
        (project_id, user_id),
    ).fetchone()
    if member is None:
        raise ResearchViewConflict("project_not_found", 404)


def _bound_target(connection: sqlite3.Connection, artifact: Artifact) -> str:
    row = connection.execute(
        "SELECT graph_target_json FROM graph_runs WHERE operation_id=? AND project_id=?",
        (artifact.origin_operation_id, artifact.project_id),
    ).fetchone()
    if row is None:
        raise ResearchViewConflict("origin_unavailable")
    return GraphTargetRef.model_validate_json(row[0]).branch_id or ""


def _pins(
    connection: sqlite3.Connection, project_id: str, user_id: str, branch_id: str
) -> list[ResearchViewPin]:
    rows = connection.execute(
        "SELECT p.artifact_id,p.position,a.metadata FROM research_view_pins p "
        "JOIN artifacts a ON a.artifact_id=p.artifact_id AND a.project_id=p.project_id "
        "WHERE p.project_id=? AND p.user_id=? AND p.branch_id=? ORDER BY p.position,p.artifact_id",
        (project_id, user_id, branch_id),
    ).fetchall()
    return [
        ResearchViewPin(
            artifact_id=row[0],
            position=row[1],
            title=(artifact.display_title or artifact.source_name),
        )
        for row in rows
        for artifact in [Artifact.model_validate_json(row[2])]
    ]


class ResearchViewStoreMixin(ArtifactStoreMixin):
    def research_view_pins(
        self, project_id: str, user_id: str, branch_id: str | None = None
    ) -> list[ResearchViewPin]:
        with self.connection() as connection:
            _require_member(connection, project_id, user_id)
            return _pins(connection, project_id, user_id, branch_id or "")

    def _pin_expired(self, connection: sqlite3.Connection, artifact: Artifact) -> bool:
        """Expired and unkept, unless an active edit still protects it (as Keep allows)."""
        return bool(
            artifact.expires_at
            and not artifact.kept_at
            and datetime.fromisoformat(artifact.expires_at) <= datetime.fromisoformat(self.now())
            and artifact.artifact_id not in self._protected_edit_artifact_ids(connection)
        )

    def research_view_offers(self, artifact: Artifact, user_id: str) -> ResearchViewOffers:
        with self.connection() as connection:
            _require_member(connection, artifact.project_id, user_id)
            try:
                target = _bound_target(connection, artifact)
            except ResearchViewConflict:
                return ResearchViewOffers(pin_branch_id=None, can_pin=False, pinned=False)
            pins = _pins(connection, artifact.project_id, user_id, target)
            pinned = any(pin.artifact_id == artifact.artifact_id for pin in pins)
            return ResearchViewOffers(
                pin_branch_id=target or None,
                can_pin=artifact.media_type == "text/html"
                and not self._pin_expired(connection, artifact)
                and (pinned or len(pins) < RESEARCH_VIEW_PIN_LIMIT),
                pinned=pinned,
            )

    def pin_research_view(
        self,
        project_id: str,
        user_id: str,
        artifact_id: str,
        branch_id: str | None,
        *,
        resolved_by: AuthorizedHuman | None = None,
    ) -> list[ResearchViewPin]:
        with self.artifact_lock(artifact_id), self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _require_member(connection, project_id, user_id)
            row = connection.execute(
                "SELECT metadata FROM artifacts WHERE artifact_id=? AND project_id=?",
                (artifact_id, project_id),
            ).fetchone()
            if row is None:
                raise ResearchViewConflict("artifact_not_found", 404)
            artifact = Artifact.model_validate_json(row[0])
            if self._pin_expired(connection, artifact):
                raise ResearchViewConflict("artifact_expired", 410)
            if artifact.media_type != "text/html":
                raise ResearchViewConflict("not_html")
            target = branch_id or ""
            if _bound_target(connection, artifact) != target:
                raise ResearchViewConflict("target_mismatch")
            pins = _pins(connection, project_id, user_id, target)
            if any(pin.artifact_id == artifact_id for pin in pins):
                return pins
            if len(pins) >= RESEARCH_VIEW_PIN_LIMIT:
                raise ResearchViewConflict("pin_limit_reached")
            self._keep_artifact(connection, artifact, resolved_by=resolved_by)
            connection.execute(
                "INSERT INTO research_view_pins VALUES (?,?,?,?,?,?)",
                (
                    project_id,
                    user_id,
                    target,
                    artifact_id,
                    max((pin.position for pin in pins), default=-1) + 1,
                    self.now(),
                ),
            )
            return _pins(connection, project_id, user_id, target)

    def unpin_research_view(
        self, project_id: str, user_id: str, artifact_id: str, branch_id: str | None
    ) -> list[ResearchViewPin]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _require_member(connection, project_id, user_id)
            connection.execute(
                "DELETE FROM research_view_pins "
                "WHERE project_id=? AND user_id=? AND branch_id=? AND artifact_id=?",
                (project_id, user_id, branch_id or "", artifact_id),
            )
            return _pins(connection, project_id, user_id, branch_id or "")
