"""Single-use human approvals; questions own resolution and delivery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any

from rcp.core.models import AuthorizedHuman
from rcp.limits import APPROVAL_CARD_TTL_HOURS
from rcp.storage.digest import append_question_attention
from rcp.storage.mixin_base import StoreMixinBase


def migrate_approval_cards(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE approval_cards (
        question_id TEXT PRIMARY KEY,
        action_json TEXT NOT NULL, pinned_json TEXT NOT NULL, pinned_sha256 TEXT NOT NULL,
        expires_at TEXT NOT NULL, request_id TEXT NOT NULL,
        running_since TEXT, result_json TEXT
    )""")


def card_sha256(action: dict[str, Any], pinned: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            {"action": action, "pinned": pinned},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def insert_approval_card(
    connection: sqlite3.Connection, question_id: str, card: dict[str, Any], now: str
) -> None:
    action, pinned = card["action"], card["pinned"]
    expires = (datetime.fromisoformat(now) + timedelta(hours=APPROVAL_CARD_TTL_HOURS)).isoformat()
    connection.execute(
        "INSERT INTO approval_cards (question_id,action_json,pinned_json,pinned_sha256,expires_at,request_id) VALUES (?,?,?,?,?,?)",
        (
            question_id,
            json.dumps(action),
            json.dumps(pinned),
            card_sha256(action, pinned),
            expires,
            str(uuid.uuid4()),
        ),
    )


def _settle(
    connection: sqlite3.Connection,
    question_id: str,
    result: dict[str, Any],
    now: str,
    human: AuthorizedHuman | None = None,
) -> None:
    row = connection.execute(
        "SELECT request_id FROM approval_cards WHERE question_id=?", (question_id,)
    ).fetchone()
    result = {**result, "request_id": row["request_id"]}
    outcome = result["outcome"]
    answer = f"failed: {result.get('reason', 'unknown')}" if outcome == "failed" else outcome
    connection.execute(
        "UPDATE approval_cards SET result_json=? WHERE question_id=? AND result_json IS NULL",
        (json.dumps(result), question_id),
    )
    connection.execute(
        """UPDATE questions SET state=?,answer=?,resolved_at=?,resolved_by_json=?,
        answer_revision=answer_revision+1 WHERE question_id=? AND state='pending'""",
        (
            "answered",
            answer,
            now,
            human.model_dump_json() if human else None,
            question_id,
        ),
    )
    row = connection.execute(
        "SELECT * FROM questions WHERE question_id=?", (question_id,)
    ).fetchone()
    if row is not None:
        append_question_attention(connection, row, now)


class ApprovalCardStoreMixin(StoreMixinBase):
    def private_question_owner(self, chat_id: str) -> str | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT user_id FROM agent_threads WHERE thread_id=?", (chat_id,)
            ).fetchone()
        return row["user_id"] if row else None

    def approval_card(self, question_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM approval_cards WHERE question_id=?", (question_id,)
            ).fetchone()
            if row is None:
                return None
            result = json.loads(row["result_json"]) if row["result_json"] else None
            closed = connection.execute(
                """SELECT 1 FROM questions q JOIN agent_threads t
                ON t.thread_id=q.owner_id WHERE q.question_id=? AND t.closed_at IS NOT NULL""",
                (question_id,),
            ).fetchone()
            if (
                result is None
                and row["running_since"] is None
                and (closed or row["expires_at"] <= self.now())
            ):
                result = {"outcome": "expired"}
                _settle(connection, question_id, result, self.now())
            action = json.loads(row["action_json"])
            return {
                "request_id": row["request_id"],
                "kind": action["kind"],
                "action": action,
                "pinned": json.loads(row["pinned_json"]),
                "pinned_sha256": row["pinned_sha256"],
                "expires_at": row["expires_at"],
                "state": "expired"
                if result and result["outcome"] == "expired"
                else "running"
                if row["running_since"]
                else "pending",
                "result": result,
            }

    def take_expired_approval_card(self, question_id: str) -> bool:
        with self.connection() as connection:
            return bool(
                connection.execute(
                    """UPDATE approval_cards SET running_since=?
                WHERE question_id=? AND running_since IS NULL
                AND json_extract(result_json, '$.outcome')='expired'""",
                    (self.now(), question_id),
                ).rowcount
            )

    def claim_approval_card(self, question_id: str) -> bool:
        with self.connection() as connection:
            return bool(
                connection.execute(
                    """UPDATE approval_cards SET running_since=?
                WHERE question_id=? AND running_since IS NULL AND result_json IS NULL
                AND expires_at>? AND EXISTS (SELECT 1 FROM questions q
                WHERE q.question_id=approval_cards.question_id AND q.state='pending'
                AND q.withdrawn_readonly=0)""",
                    (self.now(), question_id, self.now()),
                ).rowcount
            )

    def settle_approval_card(
        self,
        question_id: str,
        result: dict[str, Any],
        *,
        resolved_by: AuthorizedHuman | None = None,
    ) -> None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _settle(connection, question_id, result, self.now(), resolved_by)

    def fail_interrupted_approval_cards(self) -> None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for row in connection.execute(
                "SELECT question_id FROM approval_cards WHERE running_since IS NOT NULL AND result_json IS NULL"
            ).fetchall():
                _settle(
                    connection,
                    row["question_id"],
                    {"outcome": "failed", "reason": "interrupted"},
                    self.now(),
                )
