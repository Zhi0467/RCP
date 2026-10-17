"""Target-local operational hearsay with atomic posting receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any

from rcp.core.transition_models import GraphTargetRef
from rcp.limits import (
    BULLETIN_PAGE_SIZE,
    BULLETIN_POSTS_PER_TARGET_MAX,
    BULLETIN_REFS_MAX_COUNT,
    BULLETIN_TEXT_MAX_CHARS,
)
from rcp.storage.membership import live_project_member
from rcp.storage.mixin_base import StoreMixinBase
from rcp.storage.provisioning import ProjectProvisioningStoreMixin
from rcp.storage.resource_versions import install_bulletin_resource_versions


class BulletinError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def migrate_graph_bulletin(connection: sqlite3.Connection) -> None:
    if "bulletin_enabled" not in {
        row[1] for row in connection.execute("PRAGMA table_info(episodes)")
    }:
        connection.execute(
            "ALTER TABLE episodes ADD COLUMN bulletin_enabled INTEGER NOT NULL DEFAULT 1"
        )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS bulletin_posts (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
        "post_id TEXT NOT NULL UNIQUE, project_id TEXT NOT NULL, target_key TEXT NOT NULL, "
        "author_kind TEXT NOT NULL CHECK(author_kind IN ('agent','member')), "
        "operation_id TEXT, actor_label TEXT, user_id TEXT, display_name TEXT, "
        "text TEXT NOT NULL, refs_json TEXT NOT NULL, reply_to TEXT, created_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS bulletin_target ON bulletin_posts(project_id,target_key,sequence DESC)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS bulletin_command_receipts (project_id TEXT NOT NULL, "
        "target_key TEXT NOT NULL, author_key TEXT NOT NULL, key TEXT NOT NULL, "
        "args_digest TEXT NOT NULL, result_json TEXT NOT NULL, "
        "PRIMARY KEY(project_id,target_key,author_key,key))"
    )

    install_bulletin_resource_versions(connection)


def _record(row: sqlite3.Row) -> dict[str, Any]:
    author = {"kind": row["author_kind"]}
    for key in ("operation_id", "actor_label", "user_id", "display_name"):
        if row[key] is not None:
            author[key] = row[key]
    return {
        "post_id": row["post_id"],
        "author": author,
        "text": row["text"],
        "refs": json.loads(row["refs_json"]),
        "reply_to": row["reply_to"],
        "created_at": row["created_at"],
    }


def _list(
    connection: sqlite3.Connection, project_id: str, target_key: str, cursor: str | None
) -> dict[str, Any]:
    try:
        before = int(cursor) if cursor is not None else 9223372036854775807
    except (TypeError, ValueError) as exc:
        raise BulletinError("bulletin_cursor_invalid") from exc
    if before < 0 or before > 9223372036854775807:
        raise BulletinError("bulletin_cursor_invalid")
    rows = connection.execute(
        "SELECT * FROM bulletin_posts WHERE project_id=? AND target_key=? AND sequence<? ORDER BY sequence DESC LIMIT ?",
        (project_id, target_key, before, BULLETIN_PAGE_SIZE + 1),
    ).fetchall()
    total = connection.execute(
        "SELECT count(*) FROM bulletin_posts WHERE project_id=? AND target_key=?",
        (project_id, target_key),
    ).fetchone()[0]
    return {
        "posts": [_record(row) for row in rows[:BULLETIN_PAGE_SIZE]],
        "total": total,
        "next_cursor": str(rows[BULLETIN_PAGE_SIZE - 1]["sequence"])
        if len(rows) > BULLETIN_PAGE_SIZE
        else None,
    }


def _post(
    connection: sqlite3.Connection,
    project_id: str,
    target_key: str,
    *,
    now: str,
    author: dict[str, str],
    key: str | None,
    text: str | None,
    refs: list[str] | None,
    reply_to: str | None,
) -> dict[str, Any]:
    if not isinstance(key, str) or not key.strip():
        raise BulletinError("bulletin_key_invalid")
    refs = refs or []
    digest = hashlib.sha256(
        json.dumps({"text": text, "refs": refs, "reply_to": reply_to}, sort_keys=True).encode()
    ).hexdigest()
    author_key = author["kind"] + ":" + author.get("operation_id", author.get("user_id", ""))
    receipt = connection.execute(
        "SELECT args_digest,result_json FROM bulletin_command_receipts WHERE project_id=? AND target_key=? AND author_key=? AND key=?",
        (project_id, target_key, author_key, key),
    ).fetchone()
    if receipt:
        if receipt["args_digest"] != digest:
            raise BulletinError("bulletin_key_conflict")
        return json.loads(receipt["result_json"])
    error = None
    if not isinstance(text, str) or not text.strip() or len(text) > BULLETIN_TEXT_MAX_CHARS:
        error = "bulletin_text_invalid"
    elif len(refs) > BULLETIN_REFS_MAX_COUNT or any(
        not isinstance(ref, str) or not ref.strip() or len(ref) > BULLETIN_TEXT_MAX_CHARS
        for ref in refs
    ):
        error = "bulletin_refs_invalid"
    elif reply_to is not None:
        parent = connection.execute(
            "SELECT reply_to FROM bulletin_posts WHERE project_id=? AND target_key=? AND post_id=?",
            (project_id, target_key, reply_to),
        ).fetchone()
        if parent is None or parent["reply_to"] is not None:
            error = "bulletin_reply_invalid"
    if error:
        outcome = {"error": error}
    else:
        post_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO bulletin_posts(post_id,project_id,target_key,author_kind,operation_id,actor_label,user_id,display_name,text,refs_json,reply_to,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                post_id,
                project_id,
                target_key,
                author["kind"],
                author.get("operation_id"),
                author.get("actor_label"),
                author.get("user_id"),
                author.get("display_name"),
                text,
                json.dumps(refs),
                reply_to,
                now,
            ),
        )
        outcome = {
            "post": _record(
                connection.execute(
                    "SELECT * FROM bulletin_posts WHERE post_id=?", (post_id,)
                ).fetchone()
            )
        }
        connection.execute(
            "DELETE FROM bulletin_posts WHERE project_id=? AND target_key=? AND sequence NOT IN (SELECT sequence FROM bulletin_posts WHERE project_id=? AND target_key=? ORDER BY sequence DESC LIMIT ?)",
            (project_id, target_key, project_id, target_key, BULLETIN_POSTS_PER_TARGET_MAX),
        )
    connection.execute(
        "INSERT INTO bulletin_command_receipts VALUES (?,?,?,?,?,?)",
        (project_id, target_key, author_key, key, digest, json.dumps(outcome)),
    )
    return outcome


def _result(outcome: dict[str, Any]) -> dict[str, Any]:
    if "error" in outcome:
        raise BulletinError(outcome["error"])
    return outcome


def bulletin_enabled_for_task(connection: sqlite3.Connection, operation_id: str) -> bool:
    row = connection.execute(
        "SELECT owner.bulletin_enabled FROM graph_runs AS task "
        "JOIN episodes AS episode ON episode.episode_id=task.episode_id "
        "LEFT JOIN auto_research_child_experiments AS child ON child.child_episode_id=episode.episode_id "
        "JOIN episodes AS owner ON owner.episode_id=COALESCE(child.auto_research_episode_id, episode.isolation_owner_episode_id, episode.episode_id) "
        "WHERE task.operation_id=?",
        (operation_id,),
    ).fetchone()
    return row is None or bool(row["bulletin_enabled"])


class BulletinStoreMixin(StoreMixinBase):
    def bulletin_enabled_for_task(self, operation_id: str) -> bool:
        with self.connection() as connection:
            return bulletin_enabled_for_task(connection, operation_id)

    def bulletin_last_post_times(self, project_id: str, target_key: str) -> dict[str, str]:
        with self.connection() as connection:
            return {
                row["operation_id"]: row["created_at"]
                for row in connection.execute(
                    "SELECT operation_id,MAX(created_at) AS created_at FROM bulletin_posts WHERE project_id=? AND target_key=? AND author_kind='agent' GROUP BY operation_id",
                    (project_id, target_key),
                )
            }

    def list_bulletin(
        self, project_id: str, target_key: str, *, cursor: str | None = None
    ) -> dict[str, Any]:
        with self.connection() as connection:
            return _list(connection, project_id, target_key, cursor)

    def add_bulletin_post(
        self,
        project_id: str,
        target_key: str,
        text: str,
        *,
        user_id: str,
        key: str,
        refs: list[str] | None = None,
        reply_to: str | None = None,
    ) -> dict[str, Any]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not live_project_member(connection, project_id, user_id):
                raise BulletinError("bulletin_forbidden")
            ProjectProvisioningStoreMixin._require_project_accepts_new_work(connection, project_id)
            user = connection.execute(
                "SELECT display_name FROM space_users WHERE user_id=?", (user_id,)
            ).fetchone()
            outcome = _post(
                connection,
                project_id,
                target_key,
                now=self.now(),
                author={
                    "kind": "member",
                    "user_id": user_id,
                    "display_name": user["display_name"] or user_id,
                },
                key=key,
                text=text,
                refs=refs,
                reply_to=reply_to,
            )
        return _result(outcome)["post"]

    def execute_bulletin_command(
        self,
        project_id: str,
        operation_id: str,
        subcommand: str,
        *,
        key: str | None = None,
        text: str | None = None,
        refs: list[str] | None = None,
        reply_to: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if subcommand not in {"read", "post"}:
            raise BulletinError("bulletin_command_invalid")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT * FROM graph_runs WHERE project_id=? AND operation_id=?",
                (project_id, operation_id),
            ).fetchone()
            if task is None:
                raise BulletinError("bulletin_forbidden")
            if not bulletin_enabled_for_task(connection, operation_id):
                raise BulletinError("bulletin_disabled")
            target_key = GraphTargetRef.model_validate_json(task["graph_target_json"]).key
            if subcommand == "read":
                return _list(connection, project_id, target_key, cursor)
            authority = json.loads(task["dispatch_authority_json"] or "{}")
            if authority.get("task_contract") not in {
                "work_auto",
                "orchestrate",
            } or not live_project_member(connection, project_id, task["authorized_user_id"]):
                raise BulletinError("bulletin_forbidden")
            ProjectProvisioningStoreMixin._require_project_accepts_new_work(connection, project_id)
            outcome = _post(
                connection,
                project_id,
                target_key,
                now=self.now(),
                author={"kind": "agent", "operation_id": operation_id, "actor_label": task["kind"]},
                key=key,
                text=text,
                refs=refs,
                reply_to=reply_to,
            )
        return _result(outcome)
