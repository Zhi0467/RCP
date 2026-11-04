"""Steered messages of Paper coach turns, which have no chat transcript to hold them."""

from __future__ import annotations

import sqlite3
from typing import Literal

from pydantic import BaseModel

from rcp.storage.mixin_base import StoreMixinBase


class PaperCoachSteerRecord(BaseModel):
    message_id: str
    operation_id: str
    text: str
    attempt: int
    turn_id: str
    status: Literal["delivered", "refused", "unknown"]
    label: str
    reason: str | None
    created_at: str


def migrate_paper_coach_steers(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE paper_coach_steers (
        message_id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL REFERENCES graph_runs(operation_id),
        text TEXT NOT NULL, attempt INTEGER NOT NULL, turn_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('delivered', 'refused', 'unknown')),
        label TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL)""")
    connection.execute(
        "CREATE INDEX paper_coach_steers_operation ON paper_coach_steers(operation_id, created_at)"
    )


class PaperCoachSteerStoreMixin(StoreMixinBase):
    def paper_coach_steer(self, message_id: str) -> PaperCoachSteerRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM paper_coach_steers WHERE message_id=?", (message_id,)
            ).fetchone()
        return PaperCoachSteerRecord.model_validate(dict(row)) if row is not None else None

    def paper_coach_steers(self, operation_id: str) -> list[PaperCoachSteerRecord]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM paper_coach_steers WHERE operation_id=? "
                "ORDER BY created_at, message_id",
                (operation_id,),
            ).fetchall()
        return [PaperCoachSteerRecord.model_validate(dict(row)) for row in rows]

    def add_paper_coach_steer(self, record: PaperCoachSteerRecord) -> bool:
        """Insert once; False when the message id is already recorded."""
        row = record.model_dump()
        with self.connection() as connection:
            return (
                connection.execute(
                    f"INSERT INTO paper_coach_steers ({','.join(row)}) "
                    f"VALUES ({','.join('?' for _ in row)}) ON CONFLICT(message_id) DO NOTHING",
                    tuple(row.values()),
                ).rowcount
                == 1
            )

    def settle_paper_coach_steer(
        self, message_id: str, *, status: str, label: str, reason: str | None
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE paper_coach_steers SET status=?, label=?, reason=? WHERE message_id=?",
                (status, label, reason, message_id),
            )
