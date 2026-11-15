"""Opaque share-link tokens for team-space items, outside graph data.

A token names one item on one graph target. It grants nothing: every open
rechecks membership and readability, and the row dies with its project.
"""

from __future__ import annotations

import secrets
import sqlite3
from typing import Literal

from pydantic import BaseModel

from rcp.storage.mixin_base import StoreMixinBase

ShareLinkKind = Literal["artifact", "node", "paper", "comment", "chat", "reply"]


class ShareLinkRecord(BaseModel):
    token: str
    project_id: str
    kind: ShareLinkKind
    item_id: str
    graph_target: str
    created_by: str
    created_at: str


def migrate_share_links(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS share_links (token TEXT PRIMARY KEY, "
        "project_id TEXT NOT NULL, "
        "kind TEXT NOT NULL CHECK(kind IN ('artifact','node','paper','comment','chat','reply')), "
        "item_id TEXT NOT NULL, graph_target TEXT NOT NULL, created_by TEXT NOT NULL, "
        "created_at TEXT NOT NULL, UNIQUE(project_id, kind, item_id, graph_target))"
    )


class ShareLinkStoreMixin(StoreMixinBase):
    def share_link_token(
        self,
        project_id: str,
        kind: ShareLinkKind,
        item_id: str,
        graph_target: str,
        *,
        created_by: str,
    ) -> str:
        key = (project_id, kind, item_id, graph_target)
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO share_links VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(project_id, kind, item_id, graph_target) DO NOTHING",
                (secrets.token_urlsafe(16), *key, created_by, self.now()),
            )
            row = connection.execute(
                "SELECT token FROM share_links "
                "WHERE project_id=? AND kind=? AND item_id=? AND graph_target=?",
                key,
            ).fetchone()
        return str(row[0])

    def share_link(self, token: str) -> ShareLinkRecord | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM share_links WHERE token=?", (token,)).fetchone()
        return ShareLinkRecord.model_validate(dict(row)) if row is not None else None
