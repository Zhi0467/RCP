"""Machine-local Paper Git transport records."""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from rcp.storage.mixin_base import StoreMixinBase


class HeldFile(TypedDict):
    file_id: str
    path: str
    base_commit: str
    kind: Literal["text", "file"]


class SkippedEntry(TypedDict):
    path: str
    reason: str


class PaperGitLink(BaseModel):
    project_id: str
    state: Literal["pending", "linked"] = "pending"
    repository: str
    branch: str = "main"
    key_fingerprint: str = ""
    linked_by: str
    linked_at: str = ""
    synced_commit: str = ""
    synced_revision: int = 0
    held: list[HeldFile] = Field(default_factory=list)
    skipped: list[SkippedEntry] = Field(default_factory=list)
    last_error: str = ""
    checked_at: str = ""


def migrate_paper_git_links(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS paper_git_links (
        project_id TEXT PRIMARY KEY,
        state TEXT NOT NULL CHECK(state IN ('pending','linked')),
        repository TEXT NOT NULL, branch TEXT NOT NULL, key_fingerprint TEXT NOT NULL,
        linked_by TEXT NOT NULL, linked_at TEXT NOT NULL, synced_commit TEXT NOT NULL,
        synced_revision INTEGER NOT NULL, held TEXT NOT NULL, skipped TEXT NOT NULL,
        last_error TEXT NOT NULL, checked_at TEXT NOT NULL)""")


def _link(row: sqlite3.Row) -> PaperGitLink:
    values = dict(row)
    for key in ("held", "skipped"):
        values[key] = json.loads(values[key])
    return PaperGitLink.model_validate(values)


class PaperGitLinkStoreMixin(StoreMixinBase):
    def paper_git_links(self) -> list[PaperGitLink]:
        with self.connection() as connection:
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_git_links'"
            ).fetchone():
                return []
            return [_link(row) for row in connection.execute("SELECT * FROM paper_git_links")]

    def paper_git_link(self, project_id: str) -> PaperGitLink | None:
        return next(
            (link for link in self.paper_git_links() if link.project_id == project_id), None
        )

    def save_paper_git_link(self, link: PaperGitLink) -> None:
        row = link.model_dump()
        for key in ("held", "skipped"):
            row[key] = json.dumps(row[key])
        with self.connection() as connection:
            connection.execute(
                f"INSERT INTO paper_git_links ({','.join(row)}) VALUES ({','.join('?' for _ in row)}) "
                "ON CONFLICT(project_id) DO UPDATE SET "
                + ",".join(f"{key}=excluded.{key}" for key in row if key != "project_id"),
                tuple(row.values()),
            )

    def delete_paper_git_link(self, project_id: str) -> None:
        with self.connection() as connection:
            connection.execute("DELETE FROM paper_git_links WHERE project_id=?", (project_id,))
