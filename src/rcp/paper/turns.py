"""A finished Paper turn for review: each file the agent wrote, diffed against the turn's start.

The `paper_turn` receipt names the before and after versions; `paper_turn_files`
holds each file's base and the agent's bytes. A row diffs the base, else the
before version's file, against the agent's last bytes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from rcp.paper.changes import line_changes
from rcp.paper.models import paper_file_kind
from rcp.paper.persistence import PaperSnapshotReader
from rcp.storage import AppStore


class PaperTurnRow(BaseModel):
    path: str
    kind: str
    clash: bool
    added: int | None
    removed: int | None


class PaperTurn(BaseModel):
    before_version_id: str
    after_version_id: str
    files: list[PaperTurnRow]


class PaperTurnHunk(BaseModel):
    old_start: int
    new_start: int
    old: list[str]
    new: list[str]


class PaperTurnFile(BaseModel):
    path: str
    kind: str
    clash: bool
    before: str | None
    coach: str | None
    current: str | None = None
    hunks: list[PaperTurnHunk]


def turn_versions(store: AppStore, operation_id: str) -> tuple[str, str]:
    receipts = store.agent_task_receipts_by_category(operation_id, "paper_turn")
    if not receipts:
        raise KeyError(operation_id)
    payload = receipts[-1].payload
    return str(payload["before_version_id"]), str(payload["after_version_id"])


def _text(path: str, data: bytes | None) -> str | None:
    if data is None:
        return ""
    return data.decode() if paper_file_kind(path, data) == "text" else None


def _hunks(before: str, after: str) -> list[PaperTurnHunk]:
    hunks, shift = [], 0
    for change in line_changes(before, after):
        old, new = change.before.splitlines(), change.after.splitlines()
        hunks.append(
            PaperTurnHunk(
                old_start=change.first_line, new_start=change.first_line + shift, old=old, new=new
            )
        )
        shift += len(new) - len(old)
    return hunks


def turn_file(
    store: AppStore, project_id: str, before_version_id: str, row: dict[str, Any]
) -> PaperTurnFile:
    """One listed file: its turn-start text, the agent's text, and their line hunks."""
    path = row["path"]
    with store.connection() as conn:

        def blob(sha: str | None) -> bytes | None:
            found = conn.execute("SELECT bytes FROM paper_blobs WHERE sha256=?", (sha,)).fetchone()
            return bytes(found[0]) if found is not None else None

        base = (
            blob(row["base_sha"])
            if row["base_sha"]
            else PaperSnapshotReader(conn).version_file(project_id, before_version_id, path)
        )
        coach = blob(row["coach_sha"]) if row["coach_sha"] else None
    before, after = _text(path, base), _text(path, coach)
    textual = row["kind"] != "unsupported" and None not in (before, after)
    return PaperTurnFile(
        path=path,
        kind=row["kind"],
        clash=row["clash"],
        before=before,
        coach=after,
        hunks=_hunks(before or "", after or "") if textual else [],
    )


def turn_rows(
    store: AppStore, project_id: str, before_version_id: str, rows: list[dict[str, Any]]
) -> list[PaperTurnRow]:
    """The turn's files, clashes first, each with its added and removed line counts."""
    listed = []
    for row in rows:
        file = turn_file(store, project_id, before_version_id, row)
        diffable = file.kind != "unsupported" and None not in (file.before, file.coach)
        listed.append(
            PaperTurnRow(
                path=file.path,
                kind=file.kind,
                clash=file.clash,
                added=sum(len(hunk.new) for hunk in file.hunks) if diffable else None,
                removed=sum(len(hunk.old) for hunk in file.hunks) if diffable else None,
            )
        )
    return sorted(listed, key=lambda row: not row.clash)
