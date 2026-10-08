"""Voice request identity, recorded by the admitted task's own transaction."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from rcp.keyed_locks import KeyedLocks
from rcp.limits import VOICE_HARD_CAP_SECONDS, VOICE_TRANSCRIPT_RETENTION_SECONDS
from rcp.storage.mixin_base import StoreMixinBase
from rcp.storage.models import AgentTaskRecord


class ClientRequestConflict(ValueError):
    """A request key already belongs to another member or admission route."""


class ClientRequestRecord(BaseModel):
    project_id: str
    user_id: str
    key: str
    route: str
    operation_id: str | None = None
    episode_id: str | None = None


class _Admission(BaseModel):
    project_id: str
    user_id: str
    key: str
    route: str
    result_kind: Literal["operation", "episode"]


# Synchronous route scope only: never attach request identity to provider requests
# or their subsequent continuations. The process owns its database (invariant 8).
_ADMISSION: ContextVar[tuple[object, _Admission] | None] = ContextVar(
    "client_request_admission", default=None
)
_LOCKS = KeyedLocks()


def _expiry_cutoff(now: str) -> str:
    # A transcript expires 30 days after its last save, which can trail the
    # admission by up to one session's hard cap.
    retention = VOICE_TRANSCRIPT_RETENTION_SECONDS + VOICE_HARD_CAP_SECONDS
    return (datetime.fromisoformat(now) - timedelta(seconds=retention)).isoformat()


class ClientRequestStoreMixin(StoreMixinBase):
    def client_request(self, project_id: str, key: str) -> ClientRequestRecord | None:
        with self.connection() as connection:
            # An expired key is gone even before a later admission prunes it.
            connection.execute(
                "DELETE FROM client_requests WHERE project_id = ? AND key = ? AND created_at < ?",
                (project_id, key, _expiry_cutoff(self.now())),
            )
            row = connection.execute(
                "SELECT * FROM client_requests WHERE project_id = ? AND key = ?",
                (project_id, key),
            ).fetchone()
        return ClientRequestRecord.model_validate(dict(row)) if row is not None else None

    @contextmanager
    def client_request_admission(
        self,
        project_id: str,
        user_id: str,
        key: str | None,
        route: str,
        *,
        result_kind: Literal["operation", "episode"] = "operation",
    ) -> Iterator[ClientRequestRecord | None]:
        if key is None:
            yield None
            return
        with _LOCKS(f"{self.path.resolve()}:{project_id}:{key}"):
            existing = self.client_request(project_id, key)
            if existing is not None:
                if existing.user_id != user_id or existing.route != route:
                    raise ClientRequestConflict("Request key belongs to another member or route.")
                yield existing
                return
            admission = _Admission(
                project_id=project_id,
                user_id=user_id,
                key=key,
                route=route,
                result_kind=result_kind,
            )
            token = _ADMISSION.set((self, admission))
            try:
                yield None
            finally:
                _ADMISSION.reset(token)


def record_client_request(
    store: object,
    connection: sqlite3.Connection,
    task: AgentTaskRecord,
) -> None:
    """Called inside task insertion; any later admission failure rolls this back too."""
    scope = _ADMISSION.get()
    if scope is None or scope[0] is not store:
        return
    admission = scope[1]
    if (
        task.project_id != admission.project_id
        or task.authorized_by is None
        or task.authorized_by.user_id != admission.user_id
    ):
        raise ClientRequestConflict("Request admission identity changed.")
    if not connection.in_transaction:
        raise RuntimeError("Client request recording requires an admission transaction.")
    episode_id = task.episode_id if admission.result_kind == "episode" else None
    if admission.result_kind == "episode" and episode_id is None:
        raise ValueError("Client request admission requires an episode.")
    connection.execute(
        "DELETE FROM client_requests WHERE created_at < ?", (_expiry_cutoff(task.created_at),)
    )
    try:
        connection.execute(
            """INSERT INTO client_requests
            (project_id, user_id, key, route, operation_id, episode_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                admission.project_id,
                admission.user_id,
                admission.key,
                admission.route,
                task.operation_id if episode_id is None else None,
                episode_id,
                task.created_at,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ClientRequestConflict("Request key has already been admitted.") from exc


def migrate_client_requests(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE client_requests (
            project_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            key TEXT NOT NULL,
            route TEXT NOT NULL,
            operation_id TEXT REFERENCES graph_runs(operation_id) ON DELETE CASCADE,
            episode_id TEXT REFERENCES episodes(episode_id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (project_id, user_id, key),
            UNIQUE (project_id, key),
            CHECK ((operation_id IS NOT NULL) != (episode_id IS NOT NULL))
        )"""
    )
    connection.execute("CREATE INDEX client_requests_created ON client_requests(created_at)")
