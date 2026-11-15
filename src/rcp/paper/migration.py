"""Move each project's SQLite-held or published Paper into its folder, once.

The steps run in order and each one is idempotent. The `paper_migrations` row
is written last, so a crash resumes at the first unfinished step:

1. Install the last saved tree, else the `.research/paper` publication with any
   legacy draft, into the empty folder through a sibling staging directory. A
   folder that already holds files was installed.
2. In one transaction: keep the publication and any draft the folder did not
   take as named History versions; give a published-only tree its `file_id`s;
   fold the editor update tail into the buffer cache with the hash of its text,
   keeping every `file_id`; clear legacy draft holds and drafts; settle
   unfinished Coach turns as interrupted; reject undecided change cards; render
   each old Coach turn into static transcript text and make it history.
3. Remove `.research/paper`, then record the marker.

A project that owes nothing is marked at once, so its publication is read once.
Old Coach rows are read as raw JSON: the Coach's request model is retired.
"""

from __future__ import annotations

import functools
import json
import logging
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from pycrdt import Doc, Text, get_update, merge_updates

from rcp.keyed_locks import KeyedLocks
from rcp.paper import folder_io
from rcp.paper.models import PaperFile, PaperSnapshot, digest, paper_file_kind
from rcp.paper.persistence import PaperSnapshotReader, save_version

if TYPE_CHECKING:
    from rcp.paper.folder import PaperFolder
    from rcp.storage import AppStore
    from rcp.transport.state import StateWorkspace

INTERRUPTED_COACH_NOTICE = "The Paper Coach is now a Paper chat, so this turn was stopped."
REJECTED_CARDS_NOTICE = "Paper change cards are retired, so these changes were not applied."
PUBLISHED_VERSION = "Published Paper"
DRAFT_VERSION = "Unpublished draft"
_INTRODUCTION = "introduction.md"
_TERMINAL = ("succeeded", "failed", "interrupted")
_LOCKS = KeyedLocks()
_LOG = logging.getLogger(__name__)


@dataclass
class _Plan:
    """What the folder receives and what History keeps beside it."""

    files: dict[str, bytes]
    versions: dict[str, dict[str, bytes]] = field(default_factory=dict)
    identities: bool = False


def needs_paper_migration(store: AppStore, project_id: str) -> bool:
    """Whether SQLite still holds Paper rows that have not moved into the folder."""
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
    workspace: Callable[[], StateWorkspace],
) -> bool:
    """Run every unfinished step for one project; False when nothing was owed."""
    with _LOCKS(f"{store.path.resolve()}:{project_id}"):
        if _marked(store, project_id):
            return False
        projection = _read_projection(workspace())
        if not projection and not needs_paper_migration(store, project_id):
            _mark(store, project_id)
            return False
        plan = _plan(store, project_id, projection)
        if plan.files:
            folder.install(plan.files)
        _settle_database(store, project_id, plan)
        workspace().remove_paper_tree()
        _mark(store, project_id)
        return True


def _marked(store: AppStore, project_id: str) -> bool:
    with store.connection() as connection:
        return (
            connection.execute(
                "SELECT 1 FROM paper_migrations WHERE project_id = ?", (project_id,)
            ).fetchone()
            is not None
        )


def _mark(store: AppStore, project_id: str) -> None:
    with store.connection() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO paper_migrations(project_id, migrated_at) VALUES (?, ?)",
            (project_id, store.now()),
        )


def migrate_papers(
    store: AppStore,
    folder_for: Callable[[str], PaperFolder],
    workspace_for: Callable[[str], StateWorkspace],
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
                functools.partial(workspace_for, project_id),
            )
        except Exception:
            _LOG.exception("Paper of project %s could not move into its folder yet", project_id)
            failed.append(project_id)
    return failed


def _read_projection(workspace: StateWorkspace) -> dict[str, bytes]:
    """The `.research/paper` publication, read without following links."""
    with workspace.read_snapshot() as root:
        paper = root / "paper"
        if not paper.is_dir() or paper.is_symlink():
            return {}
        paths = [entry.path for entry in folder_io.scan(paper) if entry.kind == "file"]
        return {
            path: data for path, data in folder_io.read(paper, paths).items() if data is not None
        }


def _plan(store: AppStore, project_id: str, projection: dict[str, bytes]) -> _Plan:
    """The saved tree wins; else the publication, whose text a stale draft never replaces."""
    with store.connection() as connection:
        saved = None
        if connection.execute(
            "SELECT 1 FROM paper_publication WHERE project_id = ?", (project_id,)
        ).fetchone():
            snapshot = PaperSnapshotReader(connection).legacy_saved_snapshot(project_id)
            saved = {file.path: snapshot.file_bytes(file) for file in snapshot.files}
        draft = connection.execute(
            "SELECT content, base_hash FROM paper_drafts WHERE project_id = ?", (project_id,)
        ).fetchone()
    if saved is not None:
        plan = _Plan(saved)
    else:
        plan = _Plan(dict(projection), identities=True)
        if draft is not None:
            text = draft[0].encode()
            published = projection.get(_INTRODUCTION)
            if published is None or published == text or draft[1] == digest(published):
                plan.files[_INTRODUCTION] = text
            else:
                plan.versions[DRAFT_VERSION] = {_INTRODUCTION: text}
    if projection and projection != plan.files:
        plan.versions[PUBLISHED_VERSION] = projection
    plan.files = _distinct_paths(plan.files)
    hidden = sorted(
        path for path in plan.files if any(folder_io.ignored(part) for part in path.split("/"))
    )
    if hidden:
        _LOG.warning("Paper of project %s keeps files its tree hides: %s", project_id, hidden)
    return plan


def _distinct_paths(files: dict[str, bytes]) -> dict[str, bytes]:
    """Rename paths equal but for case, which a case-insensitive folder would merge."""
    result: dict[str, bytes] = {}
    taken: set[str] = set()
    for path, data in sorted(files.items()):
        target = path
        suffix = PurePosixPath(path).suffix
        stem = path[: len(path) - len(suffix)] if suffix else path
        number = 2
        while target.casefold() in taken:
            target = f"{stem} ({number}){suffix}"
            number += 1
        if target != path:
            _LOG.warning("Paper path %s clashes by case; it moves to %s", path, target)
        taken.add(target.casefold())
        result[target] = data
    return result


def _settle_database(store: AppStore, project_id: str, plan: _Plan) -> None:
    now = store.now()
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        ids = _identities(connection, project_id, plan, now)
        for name, files in plan.versions.items():
            _keep_version(connection, project_id, name, files, ids, now)
        _fold_updates(connection, project_id)
        connection.execute(
            "UPDATE paper_publication SET hold = NULL WHERE project_id = ?", (project_id,)
        )
        connection.execute("DELETE FROM paper_drafts WHERE project_id = ?", (project_id,))
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


def _identities(
    connection: sqlite3.Connection, project_id: str, plan: _Plan, now: str
) -> dict[str, str]:
    """Each live path's `file_id`; a published-only tree gets its ids here."""
    ids = {
        path: file_id
        for path, file_id in connection.execute(
            "SELECT path, file_id FROM paper_files WHERE project_id = ? AND deleted_at IS NULL",
            (project_id,),
        )
    }
    if plan.identities:
        for path, data in sorted(plan.files.items()):
            if path not in ids:
                ids[path] = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO paper_files(project_id, file_id, path, kind, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (project_id, ids[path], path, paper_file_kind(path, data), now),
                )
    return ids


def _keep_version(
    connection: sqlite3.Connection,
    project_id: str,
    name: str,
    files: dict[str, bytes],
    ids: dict[str, str],
    now: str,
) -> None:
    """A named version the folder did not take; a resumed migration keeps one."""
    if connection.execute(
        "SELECT 1 FROM paper_versions WHERE project_id = ? AND kind = 'named' AND name = ?",
        (project_id, name),
    ).fetchone():
        return
    entries = []
    blobs = {}
    for path, data in sorted(files.items()):
        file_id = ids.get(path) or str(uuid.uuid4())
        if paper_file_kind(path, data) == "text":
            entries.append(PaperFile(file_id=file_id, path=path, kind="text", text=data.decode()))
        else:
            blobs[digest(data)] = data
            entries.append(
                PaperFile(file_id=file_id, path=path, kind="binary", blob_sha256=digest(data))
            )
    for sha, data in blobs.items():
        connection.execute(
            "INSERT OR IGNORE INTO paper_blobs VALUES (?, ?, ?)", (sha, data, len(data))
        )
    save_version(
        connection, project_id, PaperSnapshot(revision=0, files=entries), "migration", now, name
    )


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
