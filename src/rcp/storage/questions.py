"""Questions are operational records; each resolution preserves its origin binding."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid

from rcp.core.models import AuthorizedHuman
from rcp.limits import ASK_ANSWER_MAX_LENGTH
from rcp.storage.question_models import (
    QuestionArgumentConflict,
    QuestionOrigin,
    QuestionRecord,
    QuestionStateConflict,
)

_EPISODE_ANCESTORS = """
    WITH RECURSIVE ancestors(episode_id, project_id, continues_episode_id) AS (
        SELECT episode_id, project_id, continues_episode_id FROM episodes WHERE episode_id=?
        UNION
        SELECT episode.episode_id, episode.project_id, episode.continues_episode_id
        FROM episodes AS episode JOIN ancestors
          ON episode.episode_id=ancestors.continues_episode_id
         AND episode.project_id=ancestors.project_id
    )
"""


def migrate_questions(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE questions (
            question_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            owner_kind TEXT NOT NULL CHECK(owner_kind IN ('chat','episode')),
            owner_id TEXT NOT NULL,
            key TEXT NOT NULL,
            origin_json TEXT NOT NULL,
            argument_hash TEXT NOT NULL,
            question TEXT NOT NULL,
            choices_json TEXT NOT NULL,
            multiple INTEGER NOT NULL CHECK(multiple IN (0,1)),
            state TEXT NOT NULL CHECK(state IN ('pending','answered','dismissed')),
            answer TEXT,
            chosen_choices_json TEXT NOT NULL DEFAULT '[]',
            resolved_by_json TEXT,
            resolved_at TEXT,
            answer_revision INTEGER NOT NULL DEFAULT 0,
            client_receipt_revision INTEGER,
            client_receipt_operation_id TEXT,
            client_receipt_request_id TEXT,
            client_receipt_at TEXT,
            followup_operation_id TEXT,
            followup_claimed_at TEXT,
            dismissal_delivered_at TEXT,
            withdrawn_readonly INTEGER NOT NULL DEFAULT 0 CHECK(withdrawn_readonly IN (0,1)),
            created_at TEXT NOT NULL,
            UNIQUE(project_id, owner_kind, owner_id, key)
        )
        """
    )
    connection.execute(
        "CREATE INDEX questions_open ON questions(project_id,state,withdrawn_readonly)"
    )


def _question_record(row: sqlite3.Row) -> QuestionRecord:
    data = dict(row)
    for name in ("project_id", "owner_kind", "owner_id"):
        data.pop(name)
    for name in ("origin", "choices", "chosen_choices", "resolved_by"):
        value = data.pop(f"{name}_json")
        data[name] = json.loads(value) if value is not None else None
    return QuestionRecord.model_validate(data)


class QuestionStoreMixin:
    def create_or_get_question(
        self,
        *,
        origin: QuestionOrigin,
        key: str,
        question: str,
        choices: list[str] | None = None,
        multiple: bool = False,
    ) -> QuestionRecord:
        """Reuse owner+key across turns; never replace the first origin binding."""
        arguments = {"question": question, "choices": choices or [], "multiple": multiple}
        argument_hash = hashlib.sha256(
            json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        record = QuestionRecord(
            question_id=uuid.uuid4().hex,
            origin=origin,
            key=key,
            argument_hash=argument_hash,
            created_at=self.now(),
            **arguments,
        )
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM questions WHERE project_id=? AND owner_kind=? AND owner_id=? AND key=?",
                (origin.project_id, origin.owner_kind, origin.owner_id, key),
            ).fetchone()
            if row is not None:
                if row["argument_hash"] != argument_hash:
                    raise QuestionArgumentConflict("question key already names different arguments")
                return _question_record(row)
            connection.execute(
                """INSERT INTO questions
                (question_id,project_id,owner_kind,owner_id,key,origin_json,argument_hash,
                 question,choices_json,multiple,state,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record.question_id,
                    origin.project_id,
                    origin.owner_kind,
                    origin.owner_id,
                    key,
                    origin.model_dump_json(),
                    argument_hash,
                    question,
                    json.dumps(record.choices),
                    multiple,
                    record.state,
                    record.created_at,
                ),
            )
        return record

    def get_question(self, question_id: str) -> QuestionRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM questions WHERE question_id=?", (question_id,)
            ).fetchone()
        return _question_record(row) if row is not None else None

    def list_questions(
        self,
        *,
        project_id: str | None = None,
        owner_kind: str | None = None,
        owner_id: str | None = None,
        open_only: bool = False,
    ) -> list[QuestionRecord]:
        """Open means pending and answerable; withdrawn episode cards remain in history."""
        clauses, parameters = [], []
        for column, value in (
            ("project_id", project_id),
            ("owner_kind", owner_kind),
            ("owner_id", owner_id),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                parameters.append(value)
        if open_only:
            clauses.append("state='pending' AND withdrawn_readonly=0")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM questions" + where + " ORDER BY created_at,question_id", parameters
            ).fetchall()
        return [_question_record(row) for row in rows]

    def answer_question(
        self,
        question_id: str,
        *,
        answer: str,
        choices: list[str] | None = None,
        resolved_by: AuthorizedHuman,
    ) -> QuestionRecord:
        """Resolve once; an exact duplicate resolution is safe to retry."""
        if not isinstance(answer, str) or len(answer) > ASK_ANSWER_MAX_LENGTH:
            raise ValueError("invalid question answer length")
        return self._resolve_question(question_id, "answered", resolved_by, answer, choices or [])

    def dismiss_question(self, question_id: str, *, resolved_by: AuthorizedHuman) -> QuestionRecord:
        return self._resolve_question(question_id, "dismissed", resolved_by, None, [])

    def _resolve_question(
        self,
        question_id: str,
        state: str,
        resolved_by: AuthorizedHuman,
        answer: str | None,
        choices: list[str],
    ) -> QuestionRecord:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM questions WHERE question_id=?", (question_id,)
            ).fetchone()
            if row is None:
                raise KeyError(question_id)
            record = _question_record(row)
            if record.withdrawn_readonly:
                raise QuestionStateConflict("question is withdrawn and read-only")
            if len(set(choices)) != len(choices) or set(choices) - set(record.choices):
                raise ValueError("answer choices must be distinct offered choices")
            if not record.multiple and len(choices) > 1:
                raise ValueError("question accepts at most one choice")
            if state == "answered" and not (answer and answer.strip()) and not choices:
                raise ValueError("answer requires text or a selected choice")
            if record.state != "pending":
                if (
                    record.state == state
                    and record.answer == answer
                    and record.chosen_choices == choices
                    and record.resolved_by == resolved_by
                ):
                    return record
                raise QuestionStateConflict("question is already resolved")
            connection.execute(
                """UPDATE questions SET state=?,answer=?,chosen_choices_json=?,resolved_by_json=?,
                resolved_at=?,answer_revision=answer_revision+1 WHERE question_id=?""",
                (
                    state,
                    answer,
                    json.dumps(choices),
                    resolved_by.model_dump_json(),
                    self.now(),
                    question_id,
                ),
            )
            return _question_record(
                connection.execute(
                    "SELECT * FROM questions WHERE question_id=?", (question_id,)
                ).fetchone()
            )

    def record_question_receipt(
        self,
        question_id: str,
        *,
        answer_revision: int,
        operation_id: str,
        request_id: str,
    ) -> bool:
        """Record explicit receipt by the original turn, never merely a sent response.

        Missing or ambiguous receipt deliberately leaves follow-up delivery eligible.
        """
        if not request_id:
            raise ValueError("client receipt requires a transport request id")
        with self.connection() as connection:
            return (
                connection.execute(
                    """UPDATE questions SET client_receipt_revision=?,client_receipt_operation_id=?,
                client_receipt_request_id=?,client_receipt_at=?
                WHERE question_id=? AND state='answered' AND answer_revision=?
                AND json_extract(origin_json,'$.operation_id')=?
                AND client_receipt_revision IS NULL AND followup_operation_id IS NULL""",
                    (
                        answer_revision,
                        operation_id,
                        request_id,
                        self.now(),
                        question_id,
                        answer_revision,
                        operation_id,
                    ),
                ).rowcount
                == 1
            )

    def claim_question_followup(
        self,
        connection: sqlite3.Connection,
        question_id: str,
        *,
        answer_revision: int,
        operation_id: str,
    ) -> bool:
        """Claim inside the caller's task-insert transaction; never commit here.

        The caller owns settlement, liveness, scope and budget admission. A failed
        task insert must roll back this claim in that same transaction.
        """
        if not connection.in_transaction:
            raise ValueError("question follow-up requires an active task-insert transaction")
        if not operation_id:
            raise ValueError("question follow-up requires an operation id")
        return (
            connection.execute(
                """UPDATE questions SET followup_operation_id=?,followup_claimed_at=?
            WHERE question_id=? AND state='answered' AND answer_revision=?
            AND client_receipt_revision IS NULL AND followup_operation_id IS NULL
            AND withdrawn_readonly=0""",
                (operation_id, self.now(), question_id, answer_revision),
            ).rowcount
            == 1
        )

    def mark_question_dismissal_delivered(self, question_id: str) -> bool:
        with self.connection() as connection:
            return (
                connection.execute(
                    """UPDATE questions SET dismissal_delivered_at=?
                WHERE question_id=? AND state='dismissed' AND dismissal_delivered_at IS NULL""",
                    (self.now(), question_id),
                ).rowcount
                == 1
            )

    def set_episode_questions_withdrawn(
        self, project_id: str, episode_id: str, *, withdrawn: bool
    ) -> int:
        """Withdraw ended-episode cards, or reopen its cards on continuation.

        A continuation queries its predecessor's owner id; the origin binding
        never moves to a new episode and resolved cards never become pending.
        """
        with self.connection() as connection:
            return connection.execute(
                """UPDATE questions SET withdrawn_readonly=?
                WHERE project_id=? AND owner_kind='episode' AND owner_id=?""",
                (withdrawn, project_id, episode_id),
            ).rowcount

    def episode_questions(self, project_id: str, episode_id: str) -> list[QuestionRecord]:
        """Read this continuation's questions without moving their original bindings."""
        with self.connection() as connection:
            rows = connection.execute(
                _EPISODE_ANCESTORS
                + """SELECT questions.* FROM questions JOIN ancestors
                     ON questions.owner_id=ancestors.episode_id
                    AND questions.project_id=ancestors.project_id
                    WHERE questions.project_id=? AND questions.owner_kind='episode'
                    ORDER BY questions.created_at, questions.question_id""",
                (episode_id, project_id),
            ).fetchall()
        return [_question_record(row) for row in rows]

    def set_episode_chain_questions_withdrawn_in_connection(
        self, connection: sqlite3.Connection, episode_id: str, *, withdrawn: bool
    ) -> None:
        """Change answerability atomically with an ending or continuation creation."""
        if not connection.in_transaction:
            raise ValueError("episode question lifecycle requires an active transaction")
        connection.execute(
            _EPISODE_ANCESTORS
            + """UPDATE questions SET withdrawn_readonly=?
                WHERE owner_kind='episode' AND EXISTS (
                    SELECT 1 FROM ancestors WHERE ancestors.episode_id=questions.owner_id
                    AND ancestors.project_id=questions.project_id
                )""",
            (episode_id, withdrawn),
        )

    def withdraw_episode_question_if_ended(self, question_id: str) -> bool:
        """Close a question whose creation raced its episode's ending fence.

        Serialized with endings: either this check sees the fence, or the later
        ending sees the newly inserted question. Only this card is affected.
        """
        with self.connection() as connection:
            return bool(
                connection.execute(
                    """UPDATE questions SET withdrawn_readonly=1
                WHERE question_id=? AND owner_kind='episode' AND EXISTS (
                    SELECT 1 FROM episodes WHERE episodes.episode_id=questions.owner_id
                    AND episodes.project_id=questions.project_id
                    AND (status!='running' OR ending IS NOT NULL OR stop_requested_at IS NOT NULL)
                )""",
                    (question_id,),
                ).rowcount
            )
