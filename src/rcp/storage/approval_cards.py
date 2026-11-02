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
from rcp.storage.resource_versions import install_agent_thread_version


def migrate_approval_cards(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE approval_cards (
        question_id TEXT PRIMARY KEY,
        action_json TEXT NOT NULL, pinned_json TEXT NOT NULL, pinned_sha256 TEXT NOT NULL,
        expires_at TEXT NOT NULL, request_id TEXT NOT NULL,
        running_since TEXT, result_json TEXT
    )""")
    # The 5 s expiry sweep reads only open cards.
    connection.execute(
        "CREATE INDEX approval_cards_open ON approval_cards(expires_at) "
        "WHERE result_json IS NULL AND running_since IS NULL"
    )

    install_agent_thread_version(
        connection,
        "approval_cards",
        "SELECT t.user_id FROM questions q JOIN agent_threads t ON t.thread_id=q.owner_id WHERE q.question_id=ROW.question_id AND q.owner_kind='chat' AND q.project_id=coalesce(t.project_id, (SELECT space_id FROM space_identity WHERE singleton=1))",
    )


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


_DUE_CARDS = """SELECT c.question_id, q.project_id FROM approval_cards c
    JOIN questions q ON q.question_id=c.question_id
    LEFT JOIN agent_threads t ON t.thread_id=q.owner_id
    WHERE c.result_json IS NULL AND c.running_since IS NULL
    AND (c.expires_at<=? OR t.closed_at IS NOT NULL) AND {where}"""


def _expire(
    connection: sqlite3.Connection, where: str, parameters: tuple[str, ...], now: str
) -> set[str]:
    """Settle open cards past expiry or in a closed thread; lock only when one is due."""
    query = _DUE_CARDS.format(where=where)
    if connection.execute(query, (now, *parameters)).fetchone() is None:
        return set()
    connection.execute("BEGIN IMMEDIATE")
    rows = connection.execute(query, (now, *parameters)).fetchall()
    for row in rows:
        _settle(connection, row["question_id"], {"outcome": "expired"}, now)
    return {row["project_id"] for row in rows}


def _card_view(row: sqlite3.Row) -> dict[str, Any]:
    result = json.loads(row["result_json"]) if row["result_json"] else None
    action = json.loads(row["action_json"])
    if result and result["outcome"] == "expired":
        state = "expired"
    else:
        state = "running" if row["running_since"] else "pending"
    return {
        "request_id": row["request_id"],
        "kind": action["kind"],
        "action": action,
        "pinned": json.loads(row["pinned_json"]),
        "pinned_sha256": row["pinned_sha256"],
        "expires_at": row["expires_at"],
        "state": state,
        "result": result,
    }


class ApprovalCardStoreMixin(StoreMixinBase):
    def approval_card(self, question_id: str) -> dict[str, Any] | None:
        return self.approval_cards([question_id]).get(question_id)

    def approval_cards(self, question_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Cards by question in one deferred read; only a due card takes the write lock."""
        if not question_ids:
            return {}
        marks = ",".join("?" * len(question_ids))
        with self.connection() as connection:
            _expire(connection, f"c.question_id IN ({marks})", tuple(question_ids), self.now())
            rows = connection.execute(
                f"SELECT * FROM approval_cards WHERE question_id IN ({marks})", question_ids
            ).fetchall()
        return {row["question_id"]: _card_view(row) for row in rows}

    def expire_approval_cards(self) -> set[str]:
        """Settle every open card past its expiry or in a closed thread; return projects."""
        with self.connection() as connection:
            return _expire(connection, "1", (), self.now())

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

    def release_approval_card(self, question_id: str) -> None:
        """Undo a claim whose action could not start; the member may tap again."""
        with self.connection() as connection:
            connection.execute(
                "UPDATE approval_cards SET running_since=NULL "
                "WHERE question_id=? AND result_json IS NULL",
                (question_id,),
            )

    def interrupted_approval_cards(self, claimed_before: str) -> list[dict[str, Any]]:
        """Cards claimed before `claimed_before` without a result: their process stopped."""
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT c.question_id, c.action_json, c.request_id, t.user_id
                FROM approval_cards c JOIN questions q ON q.question_id=c.question_id
                LEFT JOIN agent_threads t ON t.thread_id=q.owner_id
                WHERE c.running_since < ? AND c.result_json IS NULL""",
                (claimed_before,),
            ).fetchall()
        return [
            {
                "question_id": row["question_id"],
                "action": json.loads(row["action_json"]),
                "request_id": row["request_id"],
                "user_id": row["user_id"],
            }
            for row in rows
        ]
