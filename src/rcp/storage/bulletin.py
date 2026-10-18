"""Target-local operational hearsay with atomic posting receipts."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from typing import Any

from rcp.core.graph_targets import graph_target_json
from rcp.core.transition_models import GraphTargetRef
from rcp.limits import (
    BULLETIN_KEY_MAX_CHARS,
    BULLETIN_PAGE_SIZE,
    BULLETIN_POSTS_PER_TARGET_MAX,
    BULLETIN_REFS_MAX_COUNT,
    BULLETIN_REPLY_MAX_CHARS,
    BULLETIN_TEXT_MAX_CHARS,
    BULLETIN_UNREAD_MAX_COUNT,
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
        "text TEXT NOT NULL, refs_json TEXT NOT NULL, reply_to TEXT, created_at TEXT NOT NULL, "
        "author_actor_id TEXT, mentions_json TEXT NOT NULL DEFAULT '[]', edited_at TEXT, deleted_at TEXT)"
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

    connection.execute(
        "CREATE TABLE IF NOT EXISTS bulletin_reads (project_id TEXT NOT NULL, target_key TEXT NOT NULL, user_id TEXT NOT NULL, last_sequence INTEGER NOT NULL, PRIMARY KEY(project_id,target_key,user_id))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS bulletin_mention_observations (project_id TEXT NOT NULL, target_key TEXT NOT NULL, post_id TEXT NOT NULL, user_id TEXT NOT NULL, created_at TEXT NOT NULL, observed_at TEXT, PRIMARY KEY(post_id,user_id))"
    )
    install_bulletin_resource_versions(connection)


def _record(row: sqlite3.Row) -> dict[str, Any]:
    author = {"kind": row["author_kind"]}
    for key in ("operation_id", "actor_label", "user_id", "display_name"):
        if row[key] is not None:
            author[key] = row[key]
    if row["author_actor_id"] is not None:
        author["actor_id"] = row["author_actor_id"]
    return {
        "post_id": row["post_id"],
        "author": author,
        "text": row["text"],
        "refs": json.loads(row["refs_json"]),
        "reply_to": row["reply_to"],
        "created_at": row["created_at"],
        "mentions": json.loads(row["mentions_json"]),
        "edited_at": row["edited_at"],
        "deleted_at": row["deleted_at"],
        "reply_count": row["reply_count"] if "reply_count" in row.keys() else 0,  # noqa: SIM118 - sqlite rows iterate values
    }


_REPLY_COUNT = "(SELECT count(*) FROM bulletin_posts r WHERE r.project_id=p.project_id AND r.target_key=p.target_key AND r.reply_to=p.post_id AND r.deleted_at IS NULL)"
_VISIBLE = f"(p.deleted_at IS NULL OR (p.reply_to IS NULL AND {_REPLY_COUNT}>0))"


def _members(connection: sqlite3.Connection, project_id: str) -> list[dict[str, str]]:
    return [
        dict(row)
        for row in connection.execute(
            "SELECT u.user_id, COALESCE(u.display_name,u.user_id) AS display_name FROM project_members m "
            "JOIN space_users u ON u.user_id=m.user_id JOIN projects p ON p.project_id=m.project_id "
            "WHERE m.project_id=? AND u.removal_started_at IS NULL AND p.retired_at IS NULL ORDER BY display_name,u.user_id",
            (project_id,),
        )
    ]


def _parse(
    connection: sqlite3.Connection, project_id: str, target_key: str, text: str
) -> tuple[list[str], list[dict[str, str]]]:
    from rcp.bulletin import bulletin_actor_id

    refs = list(
        dict.fromkeys(
            token.rstrip(".,;:)") for token in re.findall(r"#([A-Za-z0-9][A-Za-z0-9_./-]*)", text)
        )
    )[:BULLETIN_REFS_MAX_COUNT]
    members = {member["user_id"] for member in _members(connection, project_id)}
    mentions = [
        {"kind": kind, "id": identity}
        for kind, identity in dict.fromkeys(re.findall(r"<@(member|agent):([^<>\s]+)>", text))
        if kind == "agent" or identity in members
    ]
    if "<@agent:" not in text:
        return refs, mentions
    target = (
        GraphTargetRef(kind="main")
        if target_key == "main"
        else GraphTargetRef(kind="branch", branch_id=target_key.removeprefix("branch:"))
    )
    agents = {
        bulletin_actor_id(connection, row)
        for row in connection.execute(
            "SELECT * FROM graph_runs WHERE project_id=? AND graph_target_json=? AND visible=1",
            (project_id, graph_target_json(target)),
        )
    }
    return refs, [m for m in mentions if m["kind"] == "member" or m["id"] in agents]


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
        f"SELECT p.*, {_REPLY_COUNT} AS reply_count FROM bulletin_posts p WHERE project_id=? AND target_key=? AND sequence<? AND {_VISIBLE} ORDER BY sequence DESC LIMIT ?",
        (project_id, target_key, before, BULLETIN_PAGE_SIZE + 1),
    ).fetchall()
    total = connection.execute(
        f"SELECT count(*) FROM bulletin_posts p WHERE project_id=? AND target_key=? AND {_VISIBLE}",
        (project_id, target_key),
    ).fetchone()[0]
    posts = [_record(row) for row in rows[:BULLETIN_PAGE_SIZE]]
    reply_ids = {post["reply_to"] for post in posts if post["reply_to"]}
    parents = {}
    if reply_ids:
        placeholders = ",".join("?" for _ in reply_ids)
        parents = {
            row["post_id"]: _record(row)
            for row in connection.execute(
                "SELECT * FROM bulletin_posts WHERE project_id=? AND target_key=? "
                f"AND post_id IN ({placeholders})",
                (project_id, target_key, *reply_ids),
            )
        }
    for post in posts:
        if post["reply_to"]:
            parent = parents.get(post["reply_to"])
            post["reply_parent"] = (
                {key: parent[key] for key in ("post_id", "author", "text")} if parent else None
            )
    return {
        "posts": posts,
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
    reply_to: str | None,
    action: str = "post",
    post_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(key, str) or not key.strip() or len(key) > BULLETIN_KEY_MAX_CHARS:
        raise BulletinError("bulletin_key_invalid")
    if reply_to is not None and (
        not isinstance(reply_to, str)
        or not reply_to.strip()
        or len(reply_to) > BULLETIN_REPLY_MAX_CHARS
    ):
        raise BulletinError("bulletin_reply_invalid")
    digest = hashlib.sha256(
        json.dumps(
            {"action": action, "post_id": post_id, "text": text, "reply_to": reply_to},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    author_key = author["kind"] + ":" + (author.get("operation_id") or author.get("user_id", ""))
    receipt = connection.execute(
        "SELECT args_digest,result_json FROM bulletin_command_receipts WHERE project_id=? AND target_key=? AND author_key=? AND key=?",
        (project_id, target_key, author_key, key),
    ).fetchone()
    if receipt:
        if receipt["args_digest"] != digest:
            raise BulletinError("bulletin_key_conflict")
        return json.loads(receipt["result_json"])
    # Replay survives Stop; fresh posts share the same transaction as its fence.
    if author["kind"] == "agent":
        blocked = connection.execute(
            """
            SELECT 1 FROM graph_runs AS task
            LEFT JOIN auto_research_child_work_attempts AS attempt
                ON attempt.operation_id = task.operation_id
            LEFT JOIN auto_research_child_work AS work ON work.worker_id = attempt.worker_id
            LEFT JOIN auto_research_child_experiments AS child
                ON child.child_episode_id = task.episode_id
            LEFT JOIN episodes AS episode ON episode.episode_id IN (
                task.episode_id, work.episode_id, child.auto_research_episode_id
            )
            WHERE task.operation_id = ? AND (
                episode.stop_requested_at IS NOT NULL OR episode.ending IS NOT NULL
                OR (episode.mode = 'auto_research' AND episode.status != 'running')
                OR work.stop_requested_at IS NOT NULL
                OR (work.worker_id IS NOT NULL AND work.current_operation_id != task.operation_id)
                OR (child.child_episode_id IS NOT NULL AND child.state != 'running')
            ) LIMIT 1
            """,
            (author["operation_id"],),
        ).fetchone()
        if blocked:
            raise BulletinError("bulletin_post_forbidden")
    error = None
    if action != "delete" and (
        not isinstance(text, str) or not text.strip() or len(text) > BULLETIN_TEXT_MAX_CHARS
    ):
        error = "bulletin_text_invalid"
    elif reply_to is not None:
        parent = connection.execute(
            "SELECT reply_to,deleted_at FROM bulletin_posts WHERE project_id=? AND target_key=? AND post_id=?",
            (project_id, target_key, reply_to),
        ).fetchone()
        if parent is None or parent["reply_to"] is not None or parent["deleted_at"] is not None:
            error = "bulletin_reply_invalid"
    if action != "post":
        existing = connection.execute(
            "SELECT * FROM bulletin_posts WHERE project_id=? AND target_key=? AND post_id=?",
            (project_id, target_key, post_id),
        ).fetchone()
        if existing is None:
            error = "bulletin_post_not_found"
        elif existing["author_kind"] != author["kind"] or (
            existing["user_id"] != author["user_id"]
            if author["kind"] == "member"
            else not author.get("actor_id") or existing["author_actor_id"] != author["actor_id"]
        ):
            error = "bulletin_not_author"
        elif existing["deleted_at"] is not None:
            error = "bulletin_post_deleted"
    if error:
        outcome = {"error": error}
    else:
        refs, mentions = (
            ([], [])
            if action == "delete"
            else _parse(connection, project_id, target_key, text or "")
        )
        if action == "edit":
            connection.execute(
                "UPDATE bulletin_posts SET text=?,refs_json=?,mentions_json=?,edited_at=? WHERE post_id=?",
                (text, json.dumps(refs), json.dumps(mentions), now, post_id),
            )
        elif action == "delete":
            connection.execute(
                "UPDATE bulletin_posts SET text='',refs_json='[]',mentions_json='[]',deleted_at=? WHERE post_id=?",
                (now, post_id),
            )
        else:
            post_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO bulletin_posts(post_id,project_id,target_key,author_kind,operation_id,actor_label,user_id,display_name,text,refs_json,reply_to,created_at,author_actor_id,mentions_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    author.get("actor_id"),
                    json.dumps(mentions),
                ),
            )
        for mention in mentions:
            if mention["kind"] == "member" and mention["id"] != author.get("user_id"):
                connection.execute(
                    "INSERT OR IGNORE INTO bulletin_mention_observations(project_id,target_key,post_id,user_id,created_at) VALUES(?,?,?,?,?)",
                    (project_id, target_key, post_id, mention["id"], now),
                )
        outcome = {
            "post": _record(
                connection.execute(
                    f"SELECT p.*, {_REPLY_COUNT} AS reply_count FROM bulletin_posts p WHERE post_id=?",
                    (post_id,),
                ).fetchone()
            )
        }
        connection.execute(
            "DELETE FROM bulletin_posts WHERE project_id=? AND target_key=? AND sequence NOT IN (SELECT sequence FROM bulletin_posts WHERE project_id=? AND target_key=? ORDER BY sequence DESC LIMIT ?)",
            (project_id, target_key, project_id, target_key, BULLETIN_POSTS_PER_TARGET_MAX),
        )
        connection.execute(
            "DELETE FROM bulletin_mention_observations WHERE project_id=? AND target_key=? "
            "AND observed_at IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM bulletin_posts p WHERE p.post_id=bulletin_mention_observations.post_id)",
            (project_id, target_key),
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
    def bulletin_members(self, project_id: str) -> list[dict[str, str]]:
        with self.connection() as connection:
            return _members(connection, project_id)

    def list_bulletin_mentions(
        self, project_id: str, target_key: str, actor_id: str
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return [
                _record(row)
                for row in connection.execute(
                    f"SELECT p.*, {_REPLY_COUNT} AS reply_count FROM bulletin_posts p WHERE project_id=? AND target_key=? AND deleted_at IS NULL AND EXISTS(SELECT 1 FROM json_each(p.mentions_json) m WHERE json_extract(m.value,'$.kind')='agent' AND json_extract(m.value,'$.id')=?) ORDER BY sequence DESC",
                    (project_id, target_key, actor_id),
                )
            ]

    def mutate_bulletin_post(
        self,
        project_id: str,
        target_key: str,
        post_id: str,
        *,
        user_id: str,
        key: str,
        text: str | None = None,
        action: str = "edit",
    ) -> dict[str, Any]:
        if action not in {"edit", "delete"}:
            raise BulletinError("bulletin_command_invalid")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not live_project_member(connection, project_id, user_id):
                raise BulletinError("bulletin_forbidden")
            ProjectProvisioningStoreMixin._require_project_accepts_new_work(connection, project_id)
            outcome = _post(
                connection,
                project_id,
                target_key,
                now=self.now(),
                author={"kind": "member", "user_id": user_id},
                key=key,
                text=text,
                reply_to=None,
                action=action,
                post_id=post_id,
            )
        return _result(outcome)["post"]

    def mark_bulletin_read(
        self, project_id: str, target_key: str, user_id: str, sequence_post_id: str
    ) -> None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not live_project_member(connection, project_id, user_id):
                raise BulletinError("bulletin_forbidden")
            row = connection.execute(
                "SELECT sequence FROM bulletin_posts WHERE project_id=? AND target_key=? AND post_id=?",
                (project_id, target_key, sequence_post_id),
            ).fetchone()
            if row is None:
                raise BulletinError("bulletin_post_not_found")
            connection.execute(
                "INSERT INTO bulletin_reads VALUES(?,?,?,?) ON CONFLICT(project_id,target_key,user_id) DO UPDATE SET last_sequence=MAX(last_sequence,excluded.last_sequence)",
                (project_id, target_key, user_id, row["sequence"]),
            )

    def bulletin_channels(self, project_id: str, user_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            branches = connection.execute(
                "SELECT branch_id,name FROM graph_branches WHERE project_id=? AND archived=0",
                (project_id,),
            ).fetchall()
            channels = [{"target_key": "main", "branch_id": None, "name": "main"}] + [
                {
                    "target_key": "branch:" + row["branch_id"],
                    "branch_id": row["branch_id"],
                    "name": row["name"] or row["branch_id"][:8],
                }
                for row in branches
            ]
            for channel in channels:
                row = connection.execute(
                    "SELECT MAX(created_at) AS last_post_at, MIN(?,SUM(CASE WHEN sequence>COALESCE((SELECT last_sequence FROM bulletin_reads WHERE project_id=? AND target_key=? AND user_id=?),0) AND NOT(author_kind='member' AND user_id=?) THEN 1 ELSE 0 END)) AS unread FROM bulletin_posts WHERE project_id=? AND target_key=? AND deleted_at IS NULL",
                    (
                        BULLETIN_UNREAD_MAX_COUNT,
                        project_id,
                        channel["target_key"],
                        user_id,
                        user_id,
                        project_id,
                        channel["target_key"],
                    ),
                ).fetchone()
                channel.update(last_post_at=row["last_post_at"], unread=row["unread"] or 0)
            branches = sorted(channels[1:], key=lambda channel: channel["name"])
            branches.sort(key=lambda channel: channel["last_post_at"] or "", reverse=True)
            return channels[:1] + branches

    def bulletin_waiting_chat_ids(self, project_id: str, target: GraphTargetRef) -> set[str]:
        """Human Work chats remain alive while their target has an undelivered observer."""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT chat_id FROM watchers "
                "WHERE project_id=? AND graph_target_json=? AND episode_id IS NULL "
                "AND worker_id IS NULL AND status IN ('active','degraded','completed') "
                "AND notified=0 AND notification_operation_id IS NULL",
                (project_id, graph_target_json(target)),
            ).fetchall()
        return {str(row["chat_id"]) for row in rows}

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
        reply_to: str | None = None,
        cursor: str | None = None,
        post_id: str | None = None,
    ) -> dict[str, Any]:
        if subcommand not in {"read", "post", "edit", "delete"}:
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
            from rcp.bulletin import bulletin_actor_id

            actor_id = bulletin_actor_id(connection, task)
            if actor_id is None:
                raise BulletinError("bulletin_forbidden")
            outcome = _post(
                connection,
                project_id,
                target_key,
                now=self.now(),
                author={
                    "actor_id": actor_id,
                    "kind": "agent",
                    "operation_id": operation_id,
                    "actor_label": _agent_label(task),
                },
                key=key,
                text=text,
                reply_to=reply_to,
                action=subcommand,
                post_id=post_id,
            )
        return _result(outcome)


def _agent_label(task: sqlite3.Row) -> str:
    """The author name a member reads: the agent's part, then its seat node."""

    request = json.loads(task["request_json"] or "{}")
    part = (
        "Worker"
        if request.get("role") == "worker"
        else "Orchestrator"
        if task["kind"] == "auto_research"
        else "Experiment"
        if request.get("patch_kind") == "experiment_loop"
        else "Agent"
    )
    seat = request.get("node_id") or request.get("control_node_id")
    return f"{part} · {seat}" if seat else part
