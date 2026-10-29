"""Operational lessons and atomic, operation-owned command receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any

from rcp.limits import LESSON_TEXT_MAX_CHARS, LESSONS_LIST_PAGE_SIZE, LESSONS_PER_PROJECT_MAX
from rcp.storage.membership import live_project_member
from rcp.storage.mixin_base import StoreMixinBase


class LessonError(ValueError):
    def __init__(self, code: str):
        self.code = code
        messages = {
            "lesson_edit_not_authorized": "Only a member's ordinary chat or consolidation may list, update, or delete lessons.",
            "lesson_forbidden": "This turn or member is not authorized to manage these lessons.",
            "lesson_text_invalid": f"Enter a nonblank lesson of at most {LESSON_TEXT_MAX_CHARS} characters.",
            "lessons_limit": f"This project has reached its limit of {LESSONS_PER_PROJECT_MAX} lessons.",
            "lesson_not_found": "This lesson no longer exists. Refresh the lesson list.",
            "lesson_command_invalid": "The requested lesson command is not supported.",
            "lesson_key_invalid": "Provide a valid idempotency key for this lesson command.",
            "lesson_key_conflict": "This key was already used with different lesson arguments.",
        }
        super().__init__(messages[code])


def migrate_operational_lessons(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS operational_lessons (lesson_id TEXT PRIMARY KEY, "
        "project_id TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, author_kind TEXT NOT NULL CHECK(author_kind IN ('agent','human')), "
        "operation_id TEXT, user_id TEXT, display_name TEXT, human_owned INTEGER NOT NULL "
        "CHECK(human_owned IN (0,1)), CHECK((author_kind='agent' AND operation_id IS NOT NULL "
        "AND user_id IS NULL AND display_name IS NULL) OR (author_kind='human' AND operation_id "
        "IS NULL AND user_id IS NOT NULL AND display_name IS NOT NULL AND human_owned=1)))"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS operational_lessons_project ON "
        "operational_lessons(project_id,human_owned DESC,updated_at DESC,lesson_id)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS lesson_command_receipts (project_id TEXT NOT NULL, "
        "operation_id TEXT NOT NULL, subcommand TEXT NOT NULL, key TEXT NOT NULL, "
        "args_digest TEXT NOT NULL, result_json TEXT NOT NULL, "
        "PRIMARY KEY(operation_id,subcommand,key))"
    )


def _lesson_permissions(
    connection: sqlite3.Connection, task: sqlite3.Row
) -> tuple[tuple[str, ...], bool]:
    """Resolve the launching owner's actions and whether it acts as its human.

    Question answers and watcher wakes keep the same chat authority. Episode
    and child Work turns never inherit it from their authorizing human.
    """
    if task["kind"] not in {"node_chat", "project_chat", "auto_research"}:
        return (), False
    authority = json.loads(task["dispatch_authority_json"] or "{}")
    contract = authority.get("task_contract")
    if contract not in {"work_auto", "orchestrate", "discuss"}:
        return (), False
    member = task["authorized_user_id"] and live_project_member(
        connection, task["project_id"], task["authorized_user_id"]
    )
    if not member:
        return (), False
    consolidation = connection.execute(
        "SELECT 1 FROM consolidation_runs WHERE operation_id=?",
        (task["operation_id"],),
    ).fetchone()
    if consolidation is not None:
        return ("add", "list", "update", "delete"), False
    scope = authority.get("scope", {})
    human_chat = (
        task["kind"] in {"node_chat", "project_chat"}
        and task["episode_id"] is None
        and contract in {"work_auto", "discuss"}
        and authority.get("profile") == "ordinary"
        and bool(scope.get("chat_id"))
        and scope.get("patch_kind") == (None if contract == "discuss" else "work")
        and connection.execute(
            "SELECT 1 FROM auto_research_child_work_attempts WHERE operation_id=?",
            (task["operation_id"],),
        ).fetchone()
        is None
    )
    if human_chat:
        return ("add", "list", "update", "delete"), True
    return (("add",) if contract in {"work_auto", "orchestrate"} else ()), False


def lesson_command_actions(store: Any, operation_id: str) -> tuple[str, ...]:
    """The handler and its prompt share this launch-time action set."""
    with store.connection() as connection:
        task = connection.execute(
            "SELECT * FROM graph_runs WHERE operation_id=?", (operation_id,)
        ).fetchone()
        return _lesson_permissions(connection, task)[0] if task is not None else ()


def _record(row: sqlite3.Row) -> dict[str, Any]:
    author = {"kind": row["author_kind"]}
    if row["author_kind"] == "agent":
        author["operation_id"] = row["operation_id"]
    else:
        author.update(user_id=row["user_id"], display_name=row["display_name"])
    return {
        **{name: row[name] for name in ("lesson_id", "text", "created_at", "updated_at")},
        "author": author,
        "human_owned": bool(row["human_owned"]),
    }


def _require_member(connection: sqlite3.Connection, project_id: str, user_id: str) -> None:
    if not live_project_member(connection, project_id, user_id):
        raise LessonError("lesson_forbidden")


def _mutate(
    connection: sqlite3.Connection,
    project_id: str,
    subcommand: str,
    *,
    now: str,
    text: str | None,
    lesson_id: str | None,
    operation_id: str | None = None,
    user_id: str | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    human = operation_id is None
    if subcommand != "delete" and (
        not isinstance(text, str) or not text.strip() or len(text) > LESSON_TEXT_MAX_CHARS
    ):
        raise LessonError("lesson_text_invalid")
    if subcommand == "add":
        count = connection.execute(
            "SELECT count(*) FROM operational_lessons WHERE project_id=?", (project_id,)
        ).fetchone()[0]
        if count >= LESSONS_PER_PROJECT_MAX:
            raise LessonError("lessons_limit")
        lesson_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO operational_lessons VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                lesson_id,
                project_id,
                text,
                now,
                now,
                "human" if human else "agent",
                operation_id,
                user_id,
                display_name,
                int(human),
            ),
        )
    else:
        row = connection.execute(
            "SELECT 1 FROM operational_lessons WHERE project_id=? AND lesson_id=?",
            (project_id, lesson_id),
        ).fetchone()
        if row is None:
            raise LessonError("lesson_not_found")
        if subcommand == "delete":
            connection.execute(
                "DELETE FROM operational_lessons WHERE project_id=? AND lesson_id=?",
                (project_id, lesson_id),
            )
            return {}
        connection.execute(
            "UPDATE operational_lessons SET text=?,updated_at=?,author_kind=?,operation_id=?,"
            "user_id=?,display_name=?,human_owned=? WHERE project_id=? AND lesson_id=?",
            (
                text,
                now,
                "human" if human else "agent",
                operation_id,
                user_id,
                display_name,
                int(human),
                project_id,
                lesson_id,
            ),
        )
    return {
        "lesson": _record(
            connection.execute(
                "SELECT * FROM operational_lessons WHERE lesson_id=?", (lesson_id,)
            ).fetchone()
        )
    }


class LessonStoreMixin(StoreMixinBase):
    def list_lessons(self, project_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return [
                _record(row)
                for row in connection.execute(
                    "SELECT * FROM operational_lessons WHERE project_id=? "
                    "ORDER BY human_owned DESC,updated_at DESC,lesson_id",
                    (project_id,),
                )
            ]

    def _human_lesson_mutation(
        self,
        project_id: str,
        subcommand: str,
        *,
        user_id: str,
        display_name: str | None = None,
        text: str | None = None,
        lesson_id: str | None = None,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _require_member(connection, project_id, user_id)
            self._require_project_accepts_new_work(connection, project_id)
            return _mutate(
                connection,
                project_id,
                subcommand,
                now=self.now(),
                text=text,
                lesson_id=lesson_id,
                user_id=user_id,
                display_name=display_name,
            )

    def add_lesson(
        self, project_id: str, text: str, *, user_id: str, display_name: str
    ) -> dict[str, Any]:
        return self._human_lesson_mutation(
            project_id, "add", text=text, user_id=user_id, display_name=display_name
        )["lesson"]

    def update_lesson(
        self, project_id: str, lesson_id: str, text: str, *, user_id: str, display_name: str
    ) -> dict[str, Any]:
        return self._human_lesson_mutation(
            project_id,
            "update",
            lesson_id=lesson_id,
            text=text,
            user_id=user_id,
            display_name=display_name,
        )["lesson"]

    def delete_lesson(self, project_id: str, lesson_id: str, *, user_id: str) -> None:
        self._human_lesson_mutation(project_id, "delete", lesson_id=lesson_id, user_id=user_id)

    def execute_lesson_command(
        self,
        project_id: str,
        operation_id: str,
        subcommand: str,
        *,
        key: str | None = None,
        lesson_id: str | None = None,
        text: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if subcommand not in {"add", "update", "delete", "list"}:
            raise LessonError("lesson_command_invalid")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT * FROM graph_runs WHERE operation_id=? AND project_id=?",
                (operation_id, project_id),
            ).fetchone()
            if task is None or not task["authorized_user_id"]:
                raise LessonError("lesson_forbidden")
            _require_member(connection, project_id, task["authorized_user_id"])
            self._require_project_accepts_new_work(connection, project_id)
            actions, human_chat = _lesson_permissions(connection, task)
            if not actions:
                raise LessonError("lesson_forbidden")
            if subcommand not in actions:
                raise LessonError("lesson_edit_not_authorized")
            if subcommand == "list":
                rows = connection.execute(
                    "SELECT * FROM operational_lessons WHERE project_id=? AND lesson_id>? "
                    "ORDER BY lesson_id LIMIT ?",
                    (project_id, cursor or "", LESSONS_LIST_PAGE_SIZE + 1),
                ).fetchall()
                return {
                    "lessons": [_record(row) for row in rows[:LESSONS_LIST_PAGE_SIZE]],
                    "next_cursor": rows[LESSONS_LIST_PAGE_SIZE - 1]["lesson_id"]
                    if len(rows) > LESSONS_LIST_PAGE_SIZE
                    else None,
                }
            if not isinstance(key, str) or not key.strip():
                raise LessonError("lesson_key_invalid")
            digest = hashlib.sha256(
                json.dumps(
                    {"lesson_id": lesson_id, "text": text},
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            receipt = connection.execute(
                "SELECT args_digest,result_json FROM lesson_command_receipts "
                "WHERE operation_id=? AND subcommand=? AND key=?",
                (operation_id, subcommand, key),
            ).fetchone()
            if receipt is not None:
                if receipt["args_digest"] != digest:
                    raise LessonError("lesson_key_conflict")
                outcome = json.loads(receipt["result_json"])
            else:
                try:
                    outcome = {
                        "result": _mutate(
                            connection,
                            project_id,
                            subcommand,
                            now=self.now(),
                            text=text,
                            lesson_id=lesson_id,
                            operation_id=None if human_chat else operation_id,
                            user_id=task["authorized_user_id"] if human_chat else None,
                            display_name=task["authorized_display_name"] if human_chat else None,
                        )
                    }
                except LessonError as exc:
                    outcome = {"error": exc.code}
                connection.execute(
                    "INSERT INTO lesson_command_receipts VALUES (?,?,?,?,?,?)",
                    (project_id, operation_id, subcommand, key, digest, json.dumps(outcome)),
                )
        if "error" in outcome:
            raise LessonError(outcome["error"])
        return outcome["result"]
