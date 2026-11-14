"""Move each project's SQLite-held Paper into its folder, once.

The steps run in order and each one is idempotent. The `paper_migrations` row
is written last, so a crash resumes at the first unfinished step:

1. Install the last saved tree into the empty folder through a sibling
   staging directory. A folder that already holds files was installed.
2. In one transaction: fold the editor update tail into the buffer cache with
   the hash of its text, keeping every `file_id`; clear legacy draft holds;
   settle unfinished Coach turns as interrupted; reject undecided change cards;
   render each old Coach turn into static transcript text and make it history.
3. Remove `.research/paper`, then record the marker.

Old Coach rows are read as raw JSON: the Coach's request model is retired.
"""

from __future__ import annotations

import functools
import json
import logging
import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING

from pycrdt import Doc, Text, get_update, merge_updates

from rcp.keyed_locks import KeyedLocks
from rcp.paper.models import digest
from rcp.paper.persistence import PaperSnapshotReader

if TYPE_CHECKING:
    from rcp.paper.folder import PaperFolder
    from rcp.storage import AppStore

INTERRUPTED_COACH_NOTICE = "The Paper Coach is now a Paper chat, so this turn was stopped."
REJECTED_CARDS_NOTICE = "Paper change cards are retired, so these changes were not applied."
_TERMINAL = ("succeeded", "failed", "interrupted")
_LOCKS = KeyedLocks()
_LOG = logging.getLogger(__name__)


def needs_paper_migration(store: AppStore, project_id: str) -> bool:
    with store.connection() as connection:
        row = connection.execute(
            "SELECT NOT EXISTS (SELECT 1 FROM paper_migrations WHERE project_id = :p) AND ("
            "EXISTS (SELECT 1 FROM paper_publication WHERE project_id = :p) OR "
            "EXISTS (SELECT 1 FROM paper_drafts WHERE project_id = :p) OR "
            "EXISTS (SELECT 1 FROM graph_runs WHERE project_id = :p AND kind = 'paper_coach'))",
            {"p": project_id},
        ).fetchone()
    return bool(row[0])


def migrate_paper(
    store: AppStore,
    project_id: str,
    folder: PaperFolder,
    remove_projection: Callable[[], None],
) -> bool:
    """Run every unfinished step for one project; False when nothing was owed."""
    with _LOCKS(f"{store.path.resolve()}:{project_id}"):
        if not needs_paper_migration(store, project_id):
            return False
        files = _saved_files(store, project_id)
        if files:
            folder.install(files)
        _settle_database(store, project_id)
        remove_projection()
        _mark(store, project_id)
        return True


def _mark(store: AppStore, project_id: str) -> None:
    with store.connection() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO paper_migrations(project_id, migrated_at) VALUES (?, ?)",
            (project_id, store.now()),
        )


def migrate_papers(
    store: AppStore,
    folder_for: Callable[[str], PaperFolder],
    remove_projection: Callable[[str], None],
) -> list[str]:
    """Migrate every project at startup; a failed project is logged and retried later."""
    failed = []
    for project in store.projects():
        project_id = project.project_id
        try:
            migrate_paper(
                store,
                project_id,
                folder_for(project_id),
                functools.partial(remove_projection, project_id),
            )
        except Exception:
            _LOG.exception("Paper of project %s could not move into its folder yet", project_id)
            failed.append(project_id)
    return failed


def _saved_files(store: AppStore, project_id: str) -> dict[str, bytes]:
    """The last saved tree, or a pre-tree draft as `introduction.md`."""
    with store.connection() as connection:
        if connection.execute(
            "SELECT 1 FROM paper_publication WHERE project_id = ?", (project_id,)
        ).fetchone():
            snapshot = PaperSnapshotReader(connection).legacy_saved_snapshot(project_id)
            return {file.path: snapshot.file_bytes(file) for file in snapshot.files}
        draft = connection.execute(
            "SELECT content FROM paper_drafts WHERE project_id = ?", (project_id,)
        ).fetchone()
    return {"introduction.md": draft[0].encode()} if draft else {}


def _settle_database(store: AppStore, project_id: str) -> None:
    now = store.now()
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _fold_updates(connection, project_id)
        connection.execute(
            "UPDATE paper_publication SET hold = NULL WHERE project_id = ?", (project_id,)
        )
        coach = connection.execute(
            "SELECT operation_id, status FROM graph_runs "
            "WHERE project_id = ? AND kind = 'paper_coach' ORDER BY created_at, operation_id",
            (project_id,),
        ).fetchall()
        store.interrupt_agent_tasks(
            connection,
            [row[0] for row in coach if row[1] not in _TERMINAL],
            detail=INTERRUPTED_COACH_NOTICE,
            reason="paper_migration",
            now=now,
        )
        _reject_cards(store, connection, project_id, now)
        for row in coach:
            _render_transcript(connection, project_id, row[0])
        store.detach_agent_tasks_for_history(connection, [row[0] for row in coach])


def _fold_updates(connection: sqlite3.Connection, project_id: str) -> None:
    file_ids = [
        row[0]
        for row in connection.execute(
            "SELECT file_id FROM paper_files WHERE project_id = ? AND kind = 'text' "
            "AND deleted_at IS NULL ORDER BY file_id",
            (project_id,),
        )
    ]
    for file_id in file_ids:
        cached = connection.execute(
            "SELECT state, through_seq FROM paper_doc_states WHERE project_id = ? AND file_id = ?",
            (project_id, file_id),
        ).fetchone()
        through = cached[1] if cached else 0
        tail = connection.execute(
            'SELECT seq, "update" FROM paper_updates WHERE project_id = ? AND file_id = ? '
            "AND seq > ? ORDER BY seq",
            (project_id, file_id, through),
        ).fetchall()
        if cached is None and not tail:
            continue
        encoded = merge_updates(cached[0] if cached else b"\x00\x00", *(row[1] for row in tail))
        doc = Doc({"text": Text()}, skip_gc=False)
        doc.apply_update(encoded)
        state = merge_updates(doc.get_update(), get_update(encoded, doc.get_state()))
        through = tail[-1][0] if tail else through
        connection.execute(
            "INSERT INTO paper_doc_states(project_id, file_id, state, through_seq, text_sha256) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(project_id, file_id) DO UPDATE SET "
            "state = excluded.state, through_seq = excluded.through_seq, "
            "text_sha256 = excluded.text_sha256",
            (project_id, file_id, state, through, digest(str(doc["text"]).encode())),
        )
        connection.execute(
            "DELETE FROM paper_updates WHERE project_id = ? AND file_id = ? AND seq <= ?",
            (project_id, file_id, through),
        )


def _reject_cards(
    store: AppStore, connection: sqlite3.Connection, project_id: str, now: str
) -> None:
    undecided = "project_id = ? AND state IN ('pending', 'outdated')"
    tasks = sorted(
        {
            row[0]
            for row in connection.execute(
                f"SELECT task_id FROM paper_change_groups WHERE {undecided}", (project_id,)
            )
        }
    )
    connection.execute(
        "UPDATE paper_change_groups SET state = 'rejected', decided_by = 'migration', "
        f"decided_at = ? WHERE {undecided}",
        (now, project_id),
    )
    store.warn_agent_tasks(connection, tasks, detail=REJECTED_CARDS_NOTICE, now=now)


def _render_transcript(connection: sqlite3.Connection, project_id: str, operation_id: str) -> None:
    """Store the turn's message, answer, follow-ups, and card outcomes as one text."""
    request_json, result_json = connection.execute(
        "SELECT request_json, result_json FROM graph_runs WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    request = json.loads(request_json)
    result = json.loads(result_json) if result_json else {}
    parts = [str(request.get("message") or "")]
    if result.get("answer"):
        parts.append(str(result["answer"]))
    parts.extend(
        f"Follow-up: {row[0]}"
        for row in connection.execute(
            "SELECT text FROM paper_coach_steers WHERE operation_id = ? ORDER BY created_at",
            (operation_id,),
        )
    )
    for state, payloads in connection.execute(
        "SELECT g.state, json_group_array(c.payload) FROM paper_change_groups g "
        "JOIN paper_changes c ON c.project_id = g.project_id AND c.group_id = g.group_id "
        "WHERE g.project_id = ? AND g.task_id = ? GROUP BY g.group_id ORDER BY g.created_at",
        (project_id, operation_id),
    ):
        paths = sorted({json.loads(payload).get("path", "") for payload in json.loads(payloads)})
        parts.append(f"Changes {state}: {', '.join(paths)}")
    result["transcript"] = "\n\n".join(part for part in parts if part)
    connection.execute(
        "UPDATE graph_runs SET result_json = ? WHERE operation_id = ?",
        (json.dumps(result), operation_id),
    )
