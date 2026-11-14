from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from pydantic import TypeAdapter

from rcp.comments.models import CommentAnchor
from rcp.comments.render import render_thread
from rcp.limits import COMMENT_POST_MAX_CHARS, COMMENT_THREAD_MAX_CHARS
from rcp.storage.agent_threads import private_artifact_owner
from rcp.storage.artifacts import ArtifactStoreMixin
from rcp.storage.bulletin import parse_member_mentions
from rcp.storage.membership import live_project_member
from rcp.storage.mixin_base import StoreMixinBase
from rcp.storage.resource_versions import install_comment_resource_versions


class CommentError(ValueError):
    def __init__(self, code: str, status_code: int = 422):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def migrate_comments(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE comment_threads (thread_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
        "target_kind TEXT NOT NULL CHECK(target_kind IN ('artifact','paper','reply')), "
        "target_id TEXT NOT NULL, graph_target TEXT NOT NULL, drawn_version TEXT, anchor_json TEXT NOT NULL, "
        "state TEXT NOT NULL CHECK(state IN ('open','resolved')), created_by TEXT NOT NULL, "
        "created_at TEXT NOT NULL, closed_by TEXT, closed_at TEXT, sent_operation_id TEXT, send_key TEXT)"
    )
    connection.execute(
        "CREATE INDEX comment_thread_target ON comment_threads(project_id,target_kind,target_id,state)"
    )
    connection.execute(
        "CREATE TABLE comment_posts (post_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
        "thread_id TEXT NOT NULL REFERENCES comment_threads(thread_id) ON DELETE CASCADE, "
        "author_id TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL, edited_at TEXT)"
    )
    connection.execute("CREATE INDEX comment_post_thread ON comment_posts(thread_id,created_at)")
    connection.execute(
        "CREATE TABLE comment_mentions (project_id TEXT NOT NULL, "
        "post_id TEXT NOT NULL REFERENCES comment_posts(post_id) ON DELETE CASCADE, "
        "thread_id TEXT NOT NULL REFERENCES comment_threads(thread_id) ON DELETE CASCADE, "
        "user_id TEXT NOT NULL, created_at TEXT NOT NULL, observed_at TEXT, PRIMARY KEY(post_id,user_id))"
    )
    connection.execute(
        "CREATE INDEX comment_mention_member ON comment_mentions(project_id,user_id)"
    )
    install_comment_resource_versions(connection)


def delete_comments_by_target(
    connection: sqlite3.Connection, project_id: str, kind: str, target_id: str
) -> None:
    connection.execute(
        "DELETE FROM comment_threads WHERE project_id=? AND target_kind=? AND target_id=?",
        (project_id, kind, target_id),
    )


def _target_readable(
    connection: sqlite3.Connection,
    user_id: str,
    kind: str,
    target_id: str,
    now: str,
    project_id: str | None = None,
) -> bool:
    if kind == "artifact":
        row = connection.execute(
            "SELECT project_id FROM artifacts WHERE artifact_id=?", (target_id,)
        ).fetchone()
    elif kind == "reply":
        row = connection.execute(
            "SELECT project_id FROM graph_runs WHERE operation_id=?", (target_id,)
        ).fetchone()
    elif kind == "paper":
        row = connection.execute(
            "SELECT project_id FROM paper_files WHERE file_id=? AND deleted_at IS NULL AND (? IS NULL OR project_id=?)",
            (target_id, project_id, project_id),
        ).fetchone()
    else:
        return False
    if row is None or (project_id is not None and row["project_id"] != project_id):
        return False
    project_id = str(row["project_id"])
    if not live_project_member(connection, project_id, user_id):
        return False
    if kind == "artifact":
        artifact = ArtifactStoreMixin._readable_artifact(connection, project_id, target_id, now=now)
        if artifact is None:
            return False
        owner = private_artifact_owner(connection, artifact)
        return owner is None or owner == user_id
    if kind == "reply":
        owner = connection.execute(
            "SELECT user_id FROM agent_threads WHERE thread_id=(SELECT json_extract(request_json,'$.chat_id') FROM graph_runs WHERE operation_id=?)",
            (target_id,),
        ).fetchone()
        return owner is None or owner["user_id"] == user_id
    return True


def _thread(connection: sqlite3.Connection, project_id: str, thread_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM comment_threads WHERE project_id=? AND thread_id=?", (project_id, thread_id)
    ).fetchone()
    if row is None:
        raise CommentError("comment_thread_not_found", 404)
    item = dict(row)
    item["anchor"] = json.loads(item.pop("anchor_json"))
    posts = []
    for row in connection.execute(
        "SELECT p.*, COALESCE(u.display_name,p.author_id) AS display_name FROM comment_posts p LEFT JOIN space_users u ON u.user_id=p.author_id WHERE thread_id=? ORDER BY p.created_at,p.rowid",
        (thread_id,),
    ):
        post = dict(row)
        post["author"] = {"user_id": post["author_id"], "display_name": post.pop("display_name")}
        post["mentions"] = [
            dict(m)
            for m in connection.execute(
                "SELECT m.user_id AS id, COALESCE(u.display_name,m.user_id) AS display_name FROM comment_mentions m LEFT JOIN space_users u ON u.user_id=m.user_id WHERE post_id=? ORDER BY m.user_id",
                (post["post_id"],),
            )
        ]
        posts.append(post)
    item["posts"] = posts
    return item


def _save_post(
    connection: sqlite3.Connection,
    thread: dict[str, Any],
    post_id: str,
    user_id: str,
    text: str,
    now: str,
    *,
    edit: bool = False,
) -> None:
    if not text.strip() or len(text) > COMMENT_POST_MAX_CHARS:
        raise CommentError("comment_post_invalid")
    try:
        mentions = parse_member_mentions(connection, thread["project_id"], text)
    except ValueError as exc:
        raise CommentError("comment_agent_mention_invalid") from exc
    members = {
        m["id"]
        for m in mentions
        if _target_readable(
            connection,
            m["id"],
            thread["target_kind"],
            thread["target_id"],
            now,
            thread["project_id"],
        )
    }
    if edit:
        connection.execute(
            "UPDATE comment_posts SET text=?,edited_at=? WHERE post_id=?", (text, now, post_id)
        )
    else:
        connection.execute(
            "INSERT INTO comment_posts VALUES (?,?,?,?,?,?,NULL)",
            (post_id, thread["project_id"], thread["thread_id"], user_id, text, now),
        )
    existing = {
        row[0]
        for row in connection.execute(
            "SELECT user_id FROM comment_mentions WHERE post_id=?", (post_id,)
        )
    }
    for member in existing - members:
        connection.execute(
            "DELETE FROM comment_mentions WHERE post_id=? AND user_id=?", (post_id, member)
        )
    for member in members - existing:
        connection.execute(
            "INSERT INTO comment_mentions VALUES (?,?,?,?,?,NULL)",
            (thread["project_id"], post_id, thread["thread_id"], member, now),
        )
    if (
        len(render_thread(_thread(connection, thread["project_id"], thread["thread_id"])))
        > COMMENT_THREAD_MAX_CHARS
    ):
        raise CommentError("comment_thread_too_long")


def _post_thread(
    connection: sqlite3.Connection, project_id: str, post_id: str, user_id: str
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT thread_id,author_id FROM comment_posts WHERE project_id=? AND post_id=?",
        (project_id, post_id),
    ).fetchone()
    if row is None:
        raise CommentError("comment_post_not_found", 404)
    if row["author_id"] != user_id:
        raise CommentError("comment_not_author", 403)
    return _thread(connection, project_id, row["thread_id"])


def _reopen(connection: sqlite3.Connection, thread_id: str) -> None:
    connection.execute(
        "UPDATE comment_threads SET state='open',closed_by=NULL,closed_at=NULL,sent_operation_id=NULL,send_key=NULL WHERE thread_id=?",
        (thread_id,),
    )


class CommentStoreMixin(StoreMixinBase):
    def comment_target_readable(
        self, user_id: str, kind: str, target_id: str, *, project_id: str | None = None
    ) -> bool:
        with self.connection() as connection:
            return _target_readable(connection, user_id, kind, target_id, self.now(), project_id)

    def create_comment_thread(
        self,
        project_id: str,
        *,
        target_kind: str,
        target_id: str,
        anchor: dict[str, Any],
        text: str,
        user_id: str,
        graph_target: str = "main",
        drawn_version: str | None = None,
    ) -> dict[str, Any]:
        anchor = TypeAdapter(CommentAnchor).validate_python(anchor).model_dump(mode="json")
        anchor["drawn_version"] = None
        thread_id = uuid.uuid4().hex
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not _target_readable(
                connection, user_id, target_kind, target_id, self.now(), project_id
            ):
                raise CommentError("comment_target_not_found", 404)
            now = self.now()
            connection.execute(
                "INSERT INTO comment_threads VALUES (?,?,?,?,?,?,?,'open',?,?,NULL,NULL,NULL,NULL)",
                (
                    thread_id,
                    project_id,
                    target_kind,
                    target_id,
                    graph_target,
                    drawn_version,
                    json.dumps({**anchor, "comment": ""}),
                    user_id,
                    now,
                ),
            )
            item = _thread(connection, project_id, thread_id)
            _save_post(connection, item, uuid.uuid4().hex, user_id, text, now)
            return _thread(connection, project_id, thread_id)

    def get_comment_thread(self, project_id: str, thread_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            item = _thread(connection, project_id, thread_id)
            if (
                item["target_kind"] == "paper"
                and connection.execute(
                    "SELECT 1 FROM paper_files WHERE project_id=? AND file_id=? AND deleted_at IS NULL",
                    (project_id, item["target_id"]),
                ).fetchone()
                is None
            ):
                raise CommentError("comment_target_not_found", 404)
            return item

    def list_comment_threads(
        self, project_id: str, target_kind: str, target_id: str
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            if (
                target_kind == "paper"
                and connection.execute(
                    "SELECT 1 FROM paper_files WHERE project_id=? AND file_id=? AND deleted_at IS NULL",
                    (project_id, target_id),
                ).fetchone()
                is None
            ):
                return []
            return [
                _thread(connection, project_id, row[0])
                for row in connection.execute(
                    "SELECT thread_id FROM comment_threads WHERE project_id=? AND target_kind=? AND target_id=? ORDER BY created_at,rowid",
                    (project_id, target_kind, target_id),
                )
            ]

    def add_comment_post(
        self, project_id: str, thread_id: str, *, user_id: str, text: str
    ) -> dict[str, Any]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = _thread(connection, project_id, thread_id)
            _save_post(connection, item, uuid.uuid4().hex, user_id, text, self.now())
            _reopen(connection, thread_id)
            return _thread(connection, project_id, thread_id)

    def edit_comment_post(
        self, project_id: str, post_id: str, *, user_id: str, text: str
    ) -> dict[str, Any]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = _post_thread(connection, project_id, post_id, user_id)
            _save_post(connection, item, post_id, user_id, text, self.now(), edit=True)
            return _thread(connection, project_id, item["thread_id"])

    def delete_comment_post(self, project_id: str, post_id: str, *, user_id: str) -> None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = _post_thread(connection, project_id, post_id, user_id)
            if item["posts"][0]["post_id"] == post_id:
                connection.execute(
                    "DELETE FROM comment_threads WHERE thread_id=?", (item["thread_id"],)
                )
            else:
                connection.execute("DELETE FROM comment_posts WHERE post_id=?", (post_id,))

    def resolve_comment_thread(
        self, project_id: str, thread_id: str, *, user_id: str
    ) -> dict[str, Any]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _thread(connection, project_id, thread_id)
            connection.execute(
                "UPDATE comment_threads SET state='resolved',closed_by=?,closed_at=? WHERE thread_id=? AND state='open'",
                (user_id, self.now(), thread_id),
            )
            return _thread(connection, project_id, thread_id)

    def reopen_comment_thread(
        self, project_id: str, thread_id: str, *, user_id: str
    ) -> dict[str, Any]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _thread(connection, project_id, thread_id)
            _reopen(connection, thread_id)
            return _thread(connection, project_id, thread_id)

    def mark_threads_sent(self, ids: list[str], operation_id: str, key: str, actor: str) -> None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for thread_id in ids:
                connection.execute(
                    "UPDATE comment_threads SET state='resolved',sent_operation_id=?,send_key=?,closed_by=?,closed_at=? WHERE thread_id=? AND state='open'",
                    (operation_id, key, actor, self.now(), thread_id),
                )

    def sendable_threads(self, ids: list[str], key: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            items = []
            for thread_id in dict.fromkeys(ids):
                row = connection.execute(
                    "SELECT project_id FROM comment_threads WHERE thread_id=?", (thread_id,)
                ).fetchone()
                if row is None:
                    raise CommentError("comment_thread_not_found", 404)
                item = _thread(connection, row["project_id"], thread_id)
                if item["state"] != "open" and item["send_key"] != key:
                    raise CommentError("comment_thread_not_sendable", 409)
                items.append(item)
            return items

    def open_comment_mentions(self, project_id: str, user_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT m.*, t.target_kind,t.target_id,t.graph_target FROM comment_mentions m JOIN comment_threads t ON t.thread_id=m.thread_id WHERE m.project_id=? AND m.user_id=? AND t.state='open' ORDER BY m.created_at,m.rowid",
                (project_id, user_id),
            ).fetchall()
            return [
                {**dict(row), "thread": _thread(connection, project_id, row["thread_id"])}
                for row in rows
                if _target_readable(
                    connection,
                    user_id,
                    row["target_kind"],
                    row["target_id"],
                    self.now(),
                    project_id,
                )
            ]
