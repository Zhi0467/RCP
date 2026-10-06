"""Questions are operational records; each resolution preserves its origin binding."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid

from rcp.core.models import AuthorizedHuman
from rcp.limits import ASK_ANSWER_MAX_LENGTH
from rcp.storage.digest import append_question_attention
from rcp.storage.mixin_base import StoreMixinBase
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
        CREATE TABLE IF NOT EXISTS questions (
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
        "CREATE INDEX IF NOT EXISTS questions_open ON questions(project_id,state,withdrawn_readonly)"
    )


def _question_record(row: sqlite3.Row) -> QuestionRecord:
    data = dict(row)
    for name in ("project_id", "owner_kind", "owner_id"):
        data.pop(name)
    for name in ("origin", "choices", "chosen_choices", "resolved_by"):
        value = data.pop(f"{name}_json")
        data[name] = json.loads(value) if value is not None else None
    return QuestionRecord.model_validate(data)


class QuestionStoreMixin(StoreMixinBase):
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
            append_question_attention(
                connection,
                connection.execute(
                    "SELECT * FROM questions WHERE question_id=?", (record.question_id,)
                ).fetchone(),
                self.now(),
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

    def questions_needing_chat_reconciliation(
        self, *, project_id: str | None = None
    ) -> list[QuestionRecord]:
        """Pending projections plus unreceived chat answers needing admission/dispatch."""
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM questions WHERE state='answered'
                AND (? IS NULL OR project_id=?)
                AND (answer_projected_revision < answer_revision OR (
                    owner_kind='chat' AND client_receipt_revision IS NULL
                    AND (followup_operation_id IS NULL OR EXISTS (
                        SELECT 1 FROM graph_runs WHERE operation_id=followup_operation_id
                        AND status='queued'
                    ))
                )) ORDER BY created_at,question_id""",
                (project_id, project_id),
            ).fetchall()
        return [_question_record(row) for row in rows]

    def questions_needing_experiment_reconciliation(
        self, *, project_id: str | None = None
    ) -> list[QuestionRecord]:
        """Unreceived episode answers that may still need receipt, admission, or dispatch."""
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM questions WHERE owner_kind='episode' AND state='answered'
                AND (? IS NULL OR project_id=?) AND client_receipt_revision IS NULL
                AND (followup_operation_id IS NULL OR EXISTS (
                    SELECT 1 FROM graph_runs WHERE operation_id=followup_operation_id
                    AND status IN ('queued','succeeded')
                )) ORDER BY created_at,question_id""",
                (project_id, project_id),
            ).fetchall()
        # A succeeded follow-up stays until its receipt is replayed, in case the process
        # stopped between its settlement and the receipt.
        return [_question_record(row) for row in rows]

    def mark_question_answer_projected(self, question_id: str, answer_revision: int) -> None:
        """Advance after canonical publication or discovery of its stable message id."""
        with self.connection() as connection:
            connection.execute(
                """UPDATE questions SET answer_projected_revision=?
                WHERE question_id=? AND answer_revision=? AND answer_projected_revision<?""",
                (answer_revision, question_id, answer_revision, answer_revision),
            )

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
            updated = connection.execute(
                "SELECT * FROM questions WHERE question_id=?", (question_id,)
            ).fetchone()
            append_question_attention(connection, updated, self.now())
            return _question_record(updated)

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

    def question_receipt_candidate_operations(self, question_id: str) -> list[str]:
        """Find successful receiver evidence after a crash before settlement callbacks."""
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT DISTINCT receipt.operation_id FROM graph_run_receipts AS receipt
                JOIN graph_runs AS task ON task.operation_id=receipt.operation_id
                WHERE receipt.category='question_answer_acknowledged' AND task.status='succeeded'
                AND json_extract(receipt.payload_json,'$.question_id')=?""",
                (question_id,),
            ).fetchall()
        return [row["operation_id"] for row in rows]

    def record_question_continuation_receipt(
        self,
        question_id: str,
        *,
        answer_revision: int,
        operation_id: str,
        request_id: str,
    ) -> bool:
        """Receipt an inherited answer only with a successful, identically bound receiver.

        The client's acknowledgement proves which response this settled turn
        consumed. Question provenance stays immutable; delivery names the actual receiver.
        """
        if not request_id:
            raise ValueError("client receipt requires a transport request id")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM questions WHERE question_id=?", (question_id,)
            ).fetchone()
            task_row = connection.execute(
                "SELECT * FROM graph_runs WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None or task_row is None:
                return False
            question = _question_record(row)
            task = self._agent_task_record(task_row)
            origin = question.origin
            if (
                question.state != "answered"
                or question.answer_revision != answer_revision
                or question.client_receipt_revision is not None
                or question.followup_operation_id is not None
                or task.status != "succeeded"
                or task.project_id != origin.project_id
                or task.request.get("provider") != origin.provider
                or task.request.get("mode")
                != ("discuss" if origin.capability == "discuss" else "work")
                or task.request.get("artifact_edit") is not None
                or task.native_session_id != origin.native_session_id
                or task.stage_root != origin.stage_root
                or (task.stage_host or "") != (origin.stage_host or "")
                or task.write_scope_fingerprint != origin.write_scope_fingerprint
                or task.graph_target != origin.graph_target
                or task.dispatch_authority is None
                or task.dispatch_authority.profile != "ordinary"
                or task.dispatch_authority.task_contract != origin.capability
            ):
                return False
            if origin.owner_kind == "chat":
                if (
                    task.kind not in {"node_chat", "project_chat"}
                    or task.episode_id is not None
                    or task.request.get("chat_id") != origin.owner_id
                ):
                    return False
            else:
                episode_id = task.episode_id
                seen: set[str] = set()
                while episode_id and episode_id not in seen:
                    seen.add(episode_id)
                    episode = connection.execute(
                        "SELECT mode,project_id,continues_episode_id FROM episodes WHERE episode_id=?",
                        (episode_id,),
                    ).fetchone()
                    if (
                        episode is None
                        or episode["mode"] != "experiment_loop"
                        or episode["project_id"] != origin.project_id
                    ):
                        return False
                    if episode_id == origin.owner_id:
                        break
                    episode_id = episode["continues_episode_id"]
                if episode_id != origin.owner_id:
                    return False
            acknowledged = connection.execute(
                """SELECT 1 FROM graph_run_receipts
                WHERE operation_id=? AND category='question_answer_acknowledged'
                AND json_extract(payload_json,'$.question_id')=?
                AND json_extract(payload_json,'$.answer_revision')=?
                AND json_extract(payload_json,'$.request_id')=? LIMIT 1""",
                (operation_id, question_id, answer_revision, request_id),
            ).fetchone()
            if acknowledged is None:
                return False
            return (
                connection.execute(
                    """UPDATE questions SET client_receipt_revision=?,client_receipt_operation_id=?,
                client_receipt_request_id=?,client_receipt_at=? WHERE question_id=?
                AND client_receipt_revision IS NULL AND followup_operation_id IS NULL""",
                    (answer_revision, operation_id, request_id, self.now(), question_id),
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
            rows = connection.execute(
                """UPDATE questions SET withdrawn_readonly=?
                WHERE project_id=? AND owner_kind='episode' AND owner_id=? RETURNING *""",
                (withdrawn, project_id, episode_id),
            ).fetchall()
            for row in rows:
                append_question_attention(connection, row, self.now())
            return len(rows)

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
        rows = connection.execute(
            _EPISODE_ANCESTORS
            + """UPDATE questions SET withdrawn_readonly=?
                WHERE owner_kind='episode' AND EXISTS (
                    SELECT 1 FROM ancestors WHERE ancestors.episode_id=questions.owner_id
                    AND ancestors.project_id=questions.project_id
                ) RETURNING *""",
            (episode_id, withdrawn),
        ).fetchall()
        for row in rows:
            append_question_attention(connection, row, self.now())

    def withdraw_episode_question_if_ended(self, question_id: str) -> bool:
        """Close a question whose creation raced its episode's ending fence.

        Serialized with endings: either this check sees the fence, or the later
        ending sees the newly inserted question. Only this card is affected.
        """
        with self.connection() as connection:
            rows = connection.execute(
                """UPDATE questions SET withdrawn_readonly=1
                WHERE question_id=? AND owner_kind='episode' AND EXISTS (
                    SELECT 1 FROM episodes WHERE episodes.episode_id=questions.owner_id
                    AND episodes.project_id=questions.project_id
                    AND (status!='running' OR ending IS NOT NULL OR stop_requested_at IS NOT NULL)
                ) RETURNING *""",
                (question_id,),
            ).fetchall()
            for row in rows:
                append_question_attention(connection, row, self.now())
            return bool(rows)
