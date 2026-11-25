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
from rcp.storage.models import ACTIVE_AGENT_TASK_STATUSES
from rcp.storage.resource_versions import (
    install_agent_thread_version,
    install_project_question_version,
)


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


def migrate_shared_chat_card_versions(connection: sqlite3.Connection) -> None:
    """A card in a chat that is not an agent thread moves its project's question list."""

    for event in ("insert", "update", "delete"):
        connection.execute(f"DROP TRIGGER IF EXISTS resource_approval_cards_questions_{event}")
    install_project_question_version(
        connection,
        "approval_cards",
        "(SELECT q.project_id FROM questions q WHERE q.question_id=ROW.question_id "
        "AND q.owner_kind='chat' AND NOT EXISTS "
        "(SELECT 1 FROM agent_threads t WHERE t.thread_id=q.owner_id))",
    )


def migrate_plan_card_revisions(connection: sqlite3.Connection) -> None:
    """A null `shelved_at` reads as an open card, and null revision columns as never revised."""
    for column in ("shelved_at", "revised_operation_id", "revised_at", "revised_by_json"):
        connection.execute(f"ALTER TABLE approval_cards ADD COLUMN {column} TEXT")


PLAN_CARD_RAISED_RECEIPT = "work_experiment_card_raised"


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
    connection.execute(
        "INSERT INTO approval_cards (question_id,action_json,pinned_json,pinned_sha256,expires_at,request_id) VALUES (?,?,?,?,?,?)",
        (
            question_id,
            json.dumps(action),
            json.dumps(pinned),
            card_sha256(action, pinned),
            _expiry(now),
            str(uuid.uuid4()),
        ),
    )


def _expiry(now: str) -> str:
    return (datetime.fromisoformat(now) + timedelta(hours=APPROVAL_CARD_TTL_HOURS)).isoformat()


def _settle(
    connection: sqlite3.Connection,
    question_id: str,
    result: dict[str, Any],
    now: str,
    human: AuthorizedHuman | None = None,
    received_by_operation_id: str | None = None,
) -> None:
    """Record the result and answer the question.

    `received_by_operation_id` names the member's own turn that already tells the agent
    this outcome, so the answer is recorded as received and no follow-up turn repeats it.
    """
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
        """UPDATE questions SET state='answered',answer=:answer,resolved_at=:now,
        resolved_by_json=:human,answer_revision=answer_revision+1,
        client_receipt_revision=CASE WHEN :receiver IS NULL THEN client_receipt_revision
            ELSE answer_revision+1 END,
        client_receipt_operation_id=coalesce(:receiver,client_receipt_operation_id),
        client_receipt_request_id=CASE WHEN :receiver IS NULL THEN client_receipt_request_id
            ELSE :request END,
        client_receipt_at=CASE WHEN :receiver IS NULL THEN client_receipt_at ELSE :now END
        WHERE question_id=:question AND state='pending'""",
        {
            "answer": answer,
            "now": now,
            "human": human.model_dump_json() if human else None,
            "receiver": received_by_operation_id,
            "request": row["request_id"],
            "question": question_id,
        },
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
    AND ((c.expires_at<=? AND c.shelved_at IS NULL) OR t.closed_at IS NOT NULL) AND {where}"""


def _expire(
    connection: sqlite3.Connection, where: str, parameters: tuple[str, ...], now: str
) -> set[str]:
    """Settle open cards past expiry or in a closed thread; lock only when one is due.

    A parked card never expires; only its thread closing settles it.
    """
    query = _DUE_CARDS.format(where=where)
    if connection.execute(query, (now, *parameters)).fetchone() is None:
        return set()
    connection.execute("BEGIN IMMEDIATE")
    rows = connection.execute(query, (now, *parameters)).fetchall()
    for row in rows:
        _settle(connection, row["question_id"], {"outcome": "expired"}, now)
    return {row["project_id"] for row in rows}


_RUN_HERE_ACTIVE = f"""(SELECT CASE WHEN r.operation_id IS NOT NULL
    THEN r.status IN ({",".join(repr(status) for status in sorted(ACTIVE_AGENT_TASK_STATUSES))})
    ELSE EXISTS (SELECT 1 FROM agent_thread_queue u
        WHERE u.queue_id=json_extract(c.result_json,'$.result.queue_id')
        AND u.operation_id IS NULL AND u.removed_at IS NULL AND u.refusal_reason IS NULL) END
    FROM (SELECT 1) LEFT JOIN graph_runs r
    ON r.operation_id=json_extract(c.result_json,'$.result.operation_id'))"""


def _card_state(row: sqlite3.Row, outcome: str | None) -> str:
    """A Run in this chat card reads `running_here` while the turn it sent is queued or running."""
    if outcome == "expired":
        return "expired"
    if outcome == "run_here":
        return "running_here" if row["run_here_active"] else "ran_here"
    if row["running_since"]:
        return "running"
    return "parked" if row["shelved_at"] else "pending"


def _card_view(row: sqlite3.Row) -> dict[str, Any]:
    result = json.loads(row["result_json"]) if row["result_json"] else None
    action = json.loads(row["action_json"])
    outcome = result["outcome"] if result else None
    state = _card_state(row, outcome)
    return {
        "request_id": row["request_id"],
        "kind": action["kind"],
        "action": action,
        "pinned": json.loads(row["pinned_json"]),
        "pinned_sha256": row["pinned_sha256"],
        "expires_at": row["expires_at"],
        "state": state,
        "result": result,
        "shelved_at": row["shelved_at"],
        "revised_at": row["revised_at"],
        "revised_by": json.loads(row["revised_by_json"]) if row["revised_by_json"] else None,
        "revised_operation_id": row["revised_operation_id"],
        "run_here_operation_id": (
            result["result"]["operation_id"] if result and outcome == "run_here" else None
        ),
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
                f"SELECT c.*, {_RUN_HERE_ACTIVE} AS run_here_active FROM approval_cards c "
                f"WHERE c.question_id IN ({marks})",
                question_ids,
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

    def claim_approval_card(self, question_id: str, pinned_sha256: str | None = None) -> bool:
        """Take the single-use claim, only while the card still carries `pinned_sha256`.

        A member's tap names the hash it saw, so it cannot claim a card revised since.
        A parked card has no expiry to pass.
        """
        with self.connection() as connection:
            return bool(
                connection.execute(
                    """UPDATE approval_cards SET running_since=?
                WHERE question_id=? AND running_since IS NULL AND result_json IS NULL
                AND (expires_at>? OR shelved_at IS NOT NULL)
                AND (? IS NULL OR pinned_sha256=?) AND EXISTS (SELECT 1 FROM questions q
                WHERE q.question_id=approval_cards.question_id AND q.state='pending'
                AND q.withdrawn_readonly=0)""",
                    (self.now(), question_id, self.now(), pinned_sha256, pinned_sha256),
                ).rowcount
            )

    def revise_approval_card(
        self,
        question_id: str,
        *,
        revised_by: dict[str, Any],
        action: dict[str, Any] | None = None,
        pinned: dict[str, Any] | None = None,
        question: str | None = None,
        operation_id: str | None = None,
        raised_kickoff_id: str | None = None,
        expected_sha256: str | None = None,
        shelve: bool = False,
    ) -> str | None:
        """Rewrite one open card in place and return its hash, or None when the condition
        fails: the card is claimed or settled, or no longer carries `expected_sha256`.

        An agent revision names its turn (`operation_id`), replaces the action, pins and
        question text, renews a pending card's expiry, and records that turn's raised
        receipt. A member revision names the hash its draft started from, keeps the stored
        pins and the revising turn, and may park the card in the same transaction. A
        changed card gets a new request id, so a Start never returns an episode started
        for the plan before the revision. It never takes the Start claim.
        """
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM approval_cards WHERE question_id=? "
                "AND result_json IS NULL AND running_since IS NULL",
                (question_id,),
            ).fetchone()
            if row is None or expected_sha256 not in (None, row["pinned_sha256"]):
                return None
            now = self.now()
            digest = row["pinned_sha256"]
            if action is not None and (
                operation_id is not None or action != json.loads(row["action_json"])
            ):
                pins = pinned if pinned is not None else json.loads(row["pinned_json"])
                digest = card_sha256(action, pins)
                renew = operation_id is not None and row["shelved_at"] is None
                connection.execute(
                    """UPDATE approval_cards SET action_json=?,pinned_json=?,pinned_sha256=?,
                    request_id=?,revised_at=?,revised_by_json=?,
                    revised_operation_id=coalesce(?,revised_operation_id),expires_at=?
                    WHERE question_id=?""",
                    (
                        json.dumps(action),
                        json.dumps(pins),
                        digest,
                        str(uuid.uuid4()),
                        now,
                        json.dumps(revised_by),
                        operation_id,
                        _expiry(now) if renew else row["expires_at"],
                        question_id,
                    ),
                )
            if question is not None:
                connection.execute(
                    "UPDATE questions SET question=? WHERE question_id=?", (question, question_id)
                )
            if operation_id is not None and raised_kickoff_id is not None:
                from rcp.storage.agent_tasks import AgentTaskStoreMixin

                AgentTaskStoreMixin._insert_agent_task_receipt(
                    connection,
                    operation_id,
                    PLAN_CARD_RAISED_RECEIPT,
                    json.dumps({"kickoff_id": raised_kickoff_id}),
                    tier="diagnostic",
                    created_at=now,
                )
            if shelve and row["shelved_at"] is None:
                connection.execute(
                    "UPDATE approval_cards SET shelved_at=? WHERE question_id=?",
                    (now, question_id),
                )
                asked = connection.execute(
                    "SELECT * FROM questions WHERE question_id=?", (question_id,)
                ).fetchone()
                append_question_attention(connection, asked, now)
            return digest

    def settle_approval_card(
        self,
        question_id: str,
        result: dict[str, Any],
        *,
        resolved_by: AuthorizedHuman | None = None,
        received_by_operation_id: str | None = None,
    ) -> None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _settle(
                connection,
                question_id,
                result,
                self.now(),
                resolved_by,
                received_by_operation_id,
            )

    def record_declined_rejection(self, question_id: str, rejection: dict[str, Any]) -> None:
        """Add the rejection a Decline then attempted to that declined card's result."""
        with self.connection() as connection:
            connection.execute(
                "UPDATE approval_cards SET result_json=json_patch(result_json, ?) "
                "WHERE question_id=? AND json_extract(result_json, '$.outcome')='declined'",
                (json.dumps(rejection), question_id),
            )

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
                """SELECT c.question_id, c.action_json, c.request_id, t.user_id,
                q.owner_id FROM approval_cards c JOIN questions q ON q.question_id=c.question_id
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
                "chat_id": row["owner_id"],
            }
            for row in rows
        ]
