from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from pydantic import TypeAdapter

from rcp.comments.models import COMMENT_MEDIA_TYPES, COMMENT_TARGET_ANCHOR_KINDS, CommentAnchor
from rcp.comments.render import render_thread, thread_budget_chars
from rcp.limits import COMMENT_POST_MAX_CHARS, COMMENT_THREAD_MAX_CHARS
from rcp.storage.agent_threads import private_artifact_owner
from rcp.storage.artifacts import readable_artifact_in
from rcp.storage.bulletin import parse_member_mentions
from rcp.storage.membership import live_project_member
from rcp.storage.mixin_base import StoreMixinBase
from rcp.storage.models import AgentTaskAdmissionConflict, AgentTaskRecord
from rcp.storage.resource_versions import install_comment_resource_versions

if TYPE_CHECKING:
    from rcp.storage import AppStore


class CommentError(ValueError):
    def __init__(self, code: str, status_code: int = 422):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True)
class CommentTargetRoute:
    project_id: str
    owner: str | None
    chat_id: str | None
    chat_owner: str | None
    graph_target: str
    media_type: str | None = None

    def readable_by(self, connection: sqlite3.Connection, user_id: str) -> bool:
        return live_project_member(connection, self.project_id, user_id) and self.owner in {
            None,
            user_id,
        }


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


def _chat_owner(connection: sqlite3.Connection, chat_id: str | None) -> str | None:
    row = connection.execute(
        "SELECT user_id FROM agent_threads WHERE thread_id=?", (chat_id,)
    ).fetchone()
    return str(row[0]) if row is not None else None


def _graph_target(task: AgentTaskRecord | None) -> str:
    return task.graph_target.key if task is not None else "main"


def _artifact_route(
    store: AppStore, connection: sqlite3.Connection, project_id: str, target_id: str
) -> CommentTargetRoute | None:
    from rcp.runs.artifact_edit_admission import _artifact_chat_id, artifact_reply_origin

    artifact = readable_artifact_in(connection, project_id, target_id, now=store.now())
    if artifact is None:
        return None
    try:
        chat_id = _artifact_chat_id(*artifact_reply_origin(store, artifact))
    except AgentTaskAdmissionConflict:
        chat_id = None
    chat_id = chat_id if isinstance(chat_id, str) and chat_id else None
    return CommentTargetRoute(
        project_id=project_id,
        owner=private_artifact_owner(connection, artifact),
        chat_id=chat_id,
        chat_owner=_chat_owner(connection, chat_id),
        graph_target=_graph_target(store.agent_task(artifact.origin_operation_id or "")),
        media_type=artifact.media_type,
    )


def _reply_route(
    store: AppStore, connection: sqlite3.Connection, project_id: str, target_id: str
) -> CommentTargetRoute | None:
    task = store.agent_task(target_id)
    if task is None or task.project_id != project_id:
        return None
    chat_id = task.request.get("chat_id")
    chat_id = chat_id if isinstance(chat_id, str) and chat_id else None
    owner = _chat_owner(connection, chat_id)
    return CommentTargetRoute(
        project_id=project_id,
        owner=owner,
        chat_id=chat_id,
        chat_owner=owner,
        graph_target=_graph_target(task),
    )


def _paper_route(
    store: AppStore, connection: sqlite3.Connection, project_id: str, target_id: str
) -> CommentTargetRoute | None:
    row = connection.execute(
        "SELECT 1 FROM paper_files WHERE project_id=? AND file_id=? AND deleted_at IS NULL",
        (project_id, target_id),
    ).fetchone()
    if row is None:
        return None
    return CommentTargetRoute(
        project_id=project_id, owner=None, chat_id=None, chat_owner=None, graph_target="main"
    )


_TARGET_ROUTES = {"artifact": _artifact_route, "reply": _reply_route, "paper": _paper_route}


def _target_route(
    store: AppStore, connection: sqlite3.Connection, project_id: str, kind: str, target_id: str
) -> CommentTargetRoute | None:
    resolve = _TARGET_ROUTES.get(kind)
    return resolve(store, connection, project_id, target_id) if resolve else None


def readable_comment_target(
    store: AppStore,
    connection: sqlite3.Connection,
    user_id: str,
    project_id: str,
    kind: str,
    target_id: str,
) -> CommentTargetRoute | None:
    route = _target_route(store, connection, project_id, kind, target_id)
    return route if route is not None and route.readable_by(connection, user_id) else None


def _require_readable(
    store: AppStore, connection: sqlite3.Connection, user_id: str, thread: dict[str, Any]
) -> CommentTargetRoute:
    route = readable_comment_target(
        store,
        connection,
        user_id,
        thread["project_id"],
        thread["target_kind"],
        thread["target_id"],
    )
    if route is None:
        raise CommentError("comment_target_not_found", 404)
    return route


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
        "SELECT p.*, COALESCE(u.display_name,p.author_id) AS display_name "
        "FROM comment_posts p LEFT JOIN space_users u ON u.user_id=p.author_id "
        "WHERE thread_id=? ORDER BY p.created_at,p.rowid",
        (thread_id,),
    ):
        post = dict(row)
        post["author"] = {"user_id": post["author_id"], "display_name": post.pop("display_name")}
        post["mentions"] = [
            dict(m)
            for m in connection.execute(
                "SELECT m.user_id AS id, COALESCE(u.display_name,m.user_id) AS display_name "
                "FROM comment_mentions m LEFT JOIN space_users u ON u.user_id=m.user_id "
                "WHERE post_id=? ORDER BY m.user_id",
                (post["post_id"],),
            )
        ]
        posts.append(post)
    item["posts"] = posts
    return item


def _save_post(
    connection: sqlite3.Connection,
    route: CommentTargetRoute,
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
    members = {m["id"] for m in mentions if route.readable_by(connection, m["id"])}
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
        thread_budget_chars(_thread(connection, thread["project_id"], thread["thread_id"]))
        > COMMENT_THREAD_MAX_CHARS
    ):
        raise CommentError("comment_thread_too_long")


def _post_thread_id(connection: sqlite3.Connection, project_id: str, post_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT thread_id,author_id FROM comment_posts WHERE project_id=? AND post_id=?",
        (project_id, post_id),
    ).fetchone()
    if row is None:
        raise CommentError("comment_post_not_found", 404)
    return row


def _reopen(connection: sqlite3.Connection, thread_id: str) -> None:
    connection.execute(
        "UPDATE comment_threads SET state='open',closed_by=NULL,closed_at=NULL,"
        "sent_operation_id=NULL,send_key=NULL WHERE thread_id=?",
        (thread_id,),
    )


class CommentStoreMixin(StoreMixinBase):
    def comment_target_route(
        self, user_id: str, kind: str, target_id: str, *, project_id: str
    ) -> CommentTargetRoute | None:
        with self.connection() as connection:
            return readable_comment_target(
                cast("AppStore", self), connection, user_id, project_id, kind, target_id
            )

    def _writable_thread(
        self, connection: sqlite3.Connection, project_id: str, thread_id: str, user_id: str
    ) -> tuple[dict[str, Any], CommentTargetRoute]:
        connection.execute("BEGIN IMMEDIATE")
        item = _thread(connection, project_id, thread_id)
        return item, _require_readable(cast("AppStore", self), connection, user_id, item)

    def _authored_thread(
        self, connection: sqlite3.Connection, project_id: str, post_id: str, user_id: str
    ) -> tuple[dict[str, Any], CommentTargetRoute]:
        connection.execute("BEGIN IMMEDIATE")
        row = _post_thread_id(connection, project_id, post_id)
        item = _thread(connection, project_id, row["thread_id"])
        route = _require_readable(cast("AppStore", self), connection, user_id, item)
        if row["author_id"] != user_id:
            raise CommentError("comment_not_author", 403)
        return item, route

    def create_comment_thread(
        self,
        project_id: str,
        *,
        target_kind: str,
        target_id: str,
        anchor: dict[str, Any],
        text: str,
        user_id: str,
        drawn_version: str | None = None,
    ) -> dict[str, Any]:
        anchor = TypeAdapter(CommentAnchor).validate_python(anchor).model_dump(mode="json")
        anchor["drawn_version"] = None
        thread_id = uuid.uuid4().hex
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            route = _require_readable(
                cast("AppStore", self),
                connection,
                user_id,
                {"project_id": project_id, "target_kind": target_kind, "target_id": target_id},
            )
            if anchor["kind"] not in COMMENT_TARGET_ANCHOR_KINDS[target_kind] or (
                route.media_type is not None
                and route.media_type not in COMMENT_MEDIA_TYPES[anchor["kind"]]
            ):
                raise CommentError("comment_anchor_unsupported")
            now = self.now()
            connection.execute(
                "INSERT INTO comment_threads VALUES (?,?,?,?,?,?,?,'open',?,?,NULL,NULL,NULL,NULL)",
                (
                    thread_id,
                    project_id,
                    target_kind,
                    target_id,
                    route.graph_target,
                    drawn_version,
                    json.dumps({**anchor, "comment": ""}),
                    user_id,
                    now,
                ),
            )
            item = _thread(connection, project_id, thread_id)
            _save_post(connection, route, item, uuid.uuid4().hex, user_id, text, now)
            return _thread(connection, project_id, thread_id)

    def get_comment_thread(self, project_id: str, thread_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            return _thread(connection, project_id, thread_id)

    def list_comment_threads(
        self, project_id: str, target_kind: str, target_id: str
    ) -> list[dict[str, Any]]:
        with self.connection() as connection:
            return [
                _thread(connection, project_id, row[0])
                for row in connection.execute(
                    "SELECT thread_id FROM comment_threads "
                    "WHERE project_id=? AND target_kind=? AND target_id=? "
                    "ORDER BY created_at,rowid",
                    (project_id, target_kind, target_id),
                )
            ]

    def list_chat_reply_threads(
        self, project_id: str, chat_id: str, user_id: str
    ) -> list[tuple[dict[str, Any], CommentTargetRoute]] | None:
        """Every thread on a reply in `chat_id`, with its route; None when the
        user cannot read the chat."""
        store = cast("AppStore", self)
        with self.connection() as connection:
            if not live_project_member(connection, project_id, user_id) or _chat_owner(
                connection, chat_id
            ) not in {None, user_id}:
                return None
            listed: list[tuple[dict[str, Any], CommentTargetRoute]] = []
            for row in connection.execute(
                "SELECT thread.thread_id, thread.target_id FROM comment_threads AS thread "
                "JOIN graph_runs AS run ON run.operation_id=thread.target_id "
                "WHERE thread.project_id=? AND thread.target_kind='reply' AND run.project_id=? "
                "AND json_extract(run.request_json, '$.chat_id')=? "
                "ORDER BY thread.created_at,thread.rowid",
                (project_id, project_id, chat_id),
            ).fetchall():
                route = readable_comment_target(
                    store, connection, user_id, project_id, "reply", row[1]
                )
                if route is not None:
                    listed.append((_thread(connection, project_id, row[0]), route))
            return listed

    def add_comment_post(
        self, project_id: str, thread_id: str, *, user_id: str, text: str
    ) -> dict[str, Any]:
        with self.connection() as connection:
            item, route = self._writable_thread(connection, project_id, thread_id, user_id)
            _save_post(connection, route, item, uuid.uuid4().hex, user_id, text, self.now())
            _reopen(connection, thread_id)
            return _thread(connection, project_id, thread_id)

    def edit_comment_post(
        self, project_id: str, post_id: str, *, user_id: str, text: str
    ) -> dict[str, Any]:
        with self.connection() as connection:
            item, route = self._authored_thread(connection, project_id, post_id, user_id)
            _save_post(connection, route, item, post_id, user_id, text, self.now(), edit=True)
            return _thread(connection, project_id, item["thread_id"])

    def delete_comment_post(self, project_id: str, post_id: str, *, user_id: str) -> None:
        with self.connection() as connection:
            item, _ = self._authored_thread(connection, project_id, post_id, user_id)
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
            self._writable_thread(connection, project_id, thread_id, user_id)
            connection.execute(
                "UPDATE comment_threads SET state='resolved',closed_by=?,closed_at=? "
                "WHERE thread_id=? AND state='open'",
                (user_id, self.now(), thread_id),
            )
            return _thread(connection, project_id, thread_id)

    def reopen_comment_thread(
        self, project_id: str, thread_id: str, *, user_id: str
    ) -> dict[str, Any]:
        with self.connection() as connection:
            self._writable_thread(connection, project_id, thread_id, user_id)
            _reopen(connection, thread_id)
            return _thread(connection, project_id, thread_id)

    def mark_threads_sent(self, ids: list[str], operation_id: str, key: str, actor: str) -> None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for thread_id in ids:
                connection.execute(
                    "UPDATE comment_threads SET state='resolved',sent_operation_id=?,send_key=?,"
                    "closed_by=?,closed_at=? WHERE thread_id=? AND state='open'",
                    (operation_id, key, actor, self.now(), thread_id),
                )

    def sendable_threads(
        self, project_id: str, user_id: str, ids: list[str], key: str
    ) -> list[dict[str, Any]]:
        store = cast("AppStore", self)
        with self.connection() as connection:
            items = []
            for thread_id in dict.fromkeys(ids):
                item = _thread(connection, project_id, thread_id)
                _require_readable(store, connection, user_id, item)
                if item["state"] != "open" and item["send_key"] != key:
                    raise CommentError("comment_thread_not_sendable", 409)
                until = item["closed_at"] if item["send_key"] == key else None
                items.append({**item, "rendered": render_thread(item, until=until)})
            return items

    def open_comment_mentions(self, project_id: str, user_id: str) -> list[dict[str, Any]]:
        store = cast("AppStore", self)
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT m.*, t.target_kind,t.target_id,t.graph_target FROM comment_mentions m "
                "JOIN comment_threads t ON t.thread_id=m.thread_id "
                "WHERE m.project_id=? AND m.user_id=? AND t.state='open' "
                "ORDER BY m.created_at,m.rowid",
                (project_id, user_id),
            ).fetchall()
            routes: dict[tuple[str, str], CommentTargetRoute | None] = {}
            mentions = []
            for row in rows:
                target = (row["target_kind"], row["target_id"])
                if target not in routes:
                    routes[target] = readable_comment_target(
                        store, connection, user_id, project_id, *target
                    )
                route = routes[target]
                if route is None:
                    continue
                thread = _thread(connection, project_id, row["thread_id"])
                mentions.append({**dict(row), "thread": {**thread, "chat_id": route.chat_id}})
            return mentions
