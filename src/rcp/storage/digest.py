"""Commit-ordered digest events and per-member acknowledgement cursors."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from urllib.parse import quote

from rcp.limits import DIGEST_LANDING_EVENT_LIMIT


def migrate_digest(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS digest_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
        kind TEXT NOT NULL, item_id TEXT NOT NULL, target TEXT NOT NULL,
        source_key TEXT NOT NULL, source_label TEXT NOT NULL, actor_user_id TEXT,
        node_ids_json TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
    )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS digest_events_project ON digest_events(project_id,seq)"
    )
    connection.execute("""CREATE TABLE IF NOT EXISTS digest_marks (
        project_id TEXT NOT NULL, user_id TEXT NOT NULL, seq INTEGER NOT NULL,
        marked_at TEXT NOT NULL, PRIMARY KEY(project_id,user_id)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS digest_heads (
        project_id TEXT NOT NULL, target TEXT NOT NULL, revision INTEGER NOT NULL,
        patch_id TEXT NOT NULL, PRIMARY KEY(project_id,target)
    )""")


def append_digest_event(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    kind: str,
    item_id: str,
    created_at: str,
    target: str = "main",
    source_key: str = "",
    source_label: str = "",
    actor_user_id: str | None = None,
    node_ids: Iterable[str] = (),
    payload: dict | None = None,
) -> int:
    """Append inside the source writer's transaction; never commit independently."""
    if not connection.in_transaction:
        raise ValueError("digest event requires its source transaction")
    result = connection.execute(
        """INSERT INTO digest_events(project_id,kind,item_id,target,source_key,source_label,
        actor_user_id,node_ids_json,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            project_id,
            kind,
            item_id,
            target,
            source_key,
            source_label,
            actor_user_id,
            json.dumps(list(node_ids)),
            json.dumps(payload or {}),
            created_at,
        ),
    )
    return result.lastrowid


def digest_event(row: sqlite3.Row) -> dict:
    result = dict(row)
    result["node_ids"] = json.loads(result.pop("node_ids_json"))
    result["payload"] = json.loads(result.pop("payload_json"))
    return result


def _require_member(connection, project_id, user_id):
    if (
        connection.execute(
            "SELECT 1 FROM project_members m JOIN space_users u ON u.user_id=m.user_id "
            "WHERE m.project_id=? AND m.user_id=? AND u.removed_at IS NULL "
            "AND u.removal_started_at IS NULL",
            (project_id, user_id),
        ).fetchone()
        is None
    ):
        raise KeyError(project_id)


def digest_cursor(connection: sqlite3.Connection) -> int:
    # Project deletion removes its rows but cannot rewind a displayed global cursor.
    row = connection.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='digest_events'"
    ).fetchone()
    return row[0] if row else 0


def _has_head(connection, project_id):
    return (
        connection.execute(
            "SELECT 1 FROM digest_heads WHERE project_id=? AND target='main'", (project_id,)
        ).fetchone()
        is not None
    )


class DigestStoreMixin:
    def digest_snapshot(self, project_id: str, user_id: str) -> tuple[dict | None, int, list[dict]]:
        with self.connection() as conn:
            conn.execute("BEGIN")
            _require_member(conn, project_id, user_id)
            if not _has_head(conn, project_id):
                return None, 0, []
            existing = conn.execute(
                "SELECT 1 FROM digest_marks WHERE project_id=? AND user_id=?", (project_id, user_id)
            ).fetchone()
            if existing is None:
                conn.rollback()
                conn.execute("BEGIN IMMEDIATE")
                _require_member(conn, project_id, user_id)
                if not _has_head(conn, project_id):
                    return None, 0, []
                conn.execute(
                    "INSERT OR IGNORE INTO digest_marks(project_id,user_id,seq,marked_at) VALUES(?,?,?,?)",
                    (project_id, user_id, digest_cursor(conn), self.now()),
                )
            cursor = digest_cursor(conn)
            mark = dict(
                conn.execute(
                    "SELECT seq,marked_at FROM digest_marks WHERE project_id=? AND user_id=?",
                    (project_id, user_id),
                ).fetchone()
            )
            events = [
                digest_event(row)
                for row in conn.execute(
                    "SELECT * FROM digest_events WHERE project_id=? AND seq>? AND seq<=? ORDER BY seq",
                    (project_id, mark["seq"], cursor),
                )
            ]
            return mark, cursor, events

    def catch_up_digest(self, project_id: str, user_id: str, seq: int) -> dict:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _require_member(conn, project_id, user_id)
            if not _has_head(conn, project_id):
                raise ValueError("The project digest baseline is not available yet.")
            maximum = digest_cursor(conn)
            if seq < 0 or seq > maximum:
                raise ValueError("digest cursor is ahead of the event log")
            conn.execute(
                """INSERT INTO digest_marks(project_id,user_id,seq,marked_at) VALUES(?,?,?,?)
                ON CONFLICT(project_id,user_id) DO UPDATE SET seq=excluded.seq,
                marked_at=excluded.marked_at WHERE excluded.seq>digest_marks.seq""",
                (project_id, user_id, seq, self.now()),
            )
            return dict(
                conn.execute(
                    "SELECT seq,marked_at FROM digest_marks WHERE project_id=? AND user_id=?",
                    (project_id, user_id),
                ).fetchone()
            )

    def digest_event_batches(self, project_ids: list[str], user_id: str) -> dict:
        if not project_ids:
            return {}
        with self.connection() as conn:
            conn.execute("BEGIN")
            marks = conn.execute(
                "SELECT project_id,seq,marked_at FROM digest_marks WHERE user_id=? AND project_id IN ("
                + ",".join("?" for _ in project_ids)
                + ") AND EXISTS (SELECT 1 FROM digest_heads h "
                "WHERE h.project_id=digest_marks.project_id AND h.target='main')",
                (user_id, *project_ids),
            ).fetchall()
            result = {}
            for mark in marks:
                events = [
                    digest_event(row)
                    for row in conn.execute(
                        "SELECT * FROM digest_events WHERE project_id=? AND seq>? ORDER BY seq DESC LIMIT ?",
                        (mark["project_id"], mark["seq"], DIGEST_LANDING_EVENT_LIMIT),
                    )
                ]
                events.reverse()
                result[mark["project_id"]] = (
                    {"seq": mark["seq"], "marked_at": mark["marked_at"]},
                    events[-1]["seq"] if events else mark["seq"],
                    events,
                )
        return result


def append_episode_ended(connection, episode_id, created_at):
    row = connection.execute("SELECT * FROM episodes WHERE episode_id=?", (episode_id,)).fetchone()
    if (
        row is None
        or connection.execute(
            "SELECT 1 FROM digest_events WHERE project_id=? AND kind='episode_ended' AND item_id=?",
            (row["project_id"], episode_id),
        ).fetchone()
    ):
        return
    append_digest_event(
        connection,
        project_id=row["project_id"],
        kind="episode_ended",
        target=(
            "main"
            if json.loads(row["graph_target_json"])["kind"] == "main"
            else "branch:" + json.loads(row["graph_target_json"])["branch_id"]
        ),
        item_id=episode_id,
        created_at=created_at,
        payload={
            "title": row["mode"].replace("_", " "),
            "status": row["status"],
            "deep_link": digest_link(
                row["project_id"], json.loads(row["graph_target_json"]), "episode", episode_id
            ),
        },
    )


def append_question_attention(connection, row, created_at):
    origin = json.loads(row["origin_json"])
    target_ref = origin["graph_target"]
    target = "main" if target_ref["kind"] == "main" else "branch:" + target_ref["branch_id"]
    active = row["state"] == "pending" and not row["withdrawn_readonly"]
    previous = connection.execute(
        "SELECT payload_json FROM digest_events WHERE project_id=? AND kind='question_attention' AND item_id=? ORDER BY seq DESC LIMIT 1",
        (row["project_id"], row["question_id"]),
    ).fetchone()
    if previous and json.loads(previous[0])["active"] == active:
        return
    project_link = f"#/projects/{quote(row['project_id'], safe='')}"
    link = (
        f"{project_link}?view=chats&chat={quote(row['owner_id'], safe='')}"
        if row["owner_kind"] == "chat"
        else digest_link(row["project_id"], target_ref, "episode", row["owner_id"])
    )
    append_digest_event(
        connection,
        project_id=row["project_id"],
        kind="question_attention",
        item_id=row["question_id"],
        target=target,
        created_at=created_at,
        payload={"active": active, "title": row["question"], "deep_link": link},
    )


def digest_link(project_id: str, target: dict, route: str, item_id: str) -> str:
    key = "main" if target["kind"] == "main" else "branch:" + target["branch_id"]
    return f"#/projects/{quote(project_id, safe='')}/targets/{quote(key, safe='')}/{route}/{quote(item_id, safe='')}"


def rewrite_digest_project_links(connection, old_project_id, project_id):
    old = f"#/projects/{quote(old_project_id, safe='')}"
    new = f"#/projects/{quote(project_id, safe='')}"
    for row in connection.execute(
        "SELECT seq,payload_json FROM digest_events WHERE project_id=?", (project_id,)
    ):
        payload = json.loads(row["payload_json"])
        link = payload.get("deep_link")
        if isinstance(link, str) and (
            link == old or link.startswith(old + "/") or link.startswith(old + "?")
        ):
            payload["deep_link"] = new + link[len(old) :]
            connection.execute(
                "UPDATE digest_events SET payload_json=? WHERE seq=?",
                (json.dumps(payload), row["seq"]),
            )


def append_task_failed(connection, operation_id, created_at, *, previous_status):
    if previous_status == "failed":
        return
    row = connection.execute(
        "SELECT * FROM graph_runs WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if row is None or row["status"] != "failed" or row["kind"] in {"node_chat", "project_chat"}:
        return
    append_digest_event(
        connection,
        project_id=row["project_id"],
        kind="task_failed",
        item_id=operation_id,
        created_at=created_at,
        payload={"title": row["kind"], "status": "failed", "deep_link": None},
    )
