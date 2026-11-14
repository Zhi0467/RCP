"""SQLite snapshot bodies and retention, always called inside the owner's transaction."""

from __future__ import annotations

import json
import sqlite3
import uuid
import zlib
from pathlib import Path

from rcp import limits
from rcp.paper.models import (
    PaperEntry,
    PaperFile,
    PaperLimit,
    PaperSnapshot,
    digest,
    paper_path,
)


class PaperSnapshotReader:
    """Read history versions from exactly the caller's SQLite snapshot, without writes."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def version(self, project_id: str, version_id: str) -> tuple[int, list[dict]]:
        row = self.connection.execute(
            "SELECT revision, manifest FROM paper_versions WHERE project_id=? AND version_id=?",
            (project_id, version_id),
        ).fetchone()
        if row is None:
            raise KeyError(version_id)
        return int(row[0]), json.loads(row[1])

    def snapshot(self, project_id: str, version_id: str) -> PaperSnapshot:
        revision, entries = self.version(project_id, version_id)
        return read_snapshot(self.connection, revision, json.dumps(entries))

    def legacy_saved_snapshot(self, project_id: str) -> PaperSnapshot:
        """The last tree SQLite saved before the Paper became a folder."""
        row = self.connection.execute(
            "SELECT s.revision, s.manifest FROM paper_publication p JOIN paper_saved_snapshots s "
            "ON s.project_id=p.project_id AND s.revision=p.saved_revision WHERE p.project_id=?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise KeyError("Paper has no saved tree")
        return read_snapshot(self.connection, int(row[0]), row[1])

    def raw_file(self, project_id: str, file_id: str, version_id: str) -> tuple[PaperFile, bytes]:
        revision, entries = self.version(project_id, version_id)
        entry = next((entry for entry in entries if entry["file_id"] == file_id), None)
        if entry is None:
            raise KeyError(file_id)
        snapshot = read_snapshot(self.connection, revision, json.dumps([entry]))
        file = snapshot.files[0]
        return file, snapshot.file_bytes(file)

    def version_file(self, project_id: str, version_id: str, path: str) -> bytes | None:
        revision, entries = self.version(project_id, version_id)
        entry = next((entry for entry in entries if entry["path"] == path), None)
        if entry is None:
            return None
        snapshot = read_snapshot(self.connection, revision, json.dumps([entry]))
        return snapshot.file_bytes(snapshot.files[0])

    def entries(self, project_id: str, version_id: str) -> tuple[int, list[PaperEntry]]:
        revision, manifest = self.version(project_id, version_id)
        entries = []
        for entry in manifest:
            sha = entry.get("text_sha256") or entry["blob_sha256"]
            size = entry.get("size")
            if size is None:
                row = self.connection.execute(
                    "SELECT size FROM paper_blobs WHERE sha256=?", (sha,)
                ).fetchone()
                size = row[0] if row else 0
            entries.append(PaperEntry(entry["file_id"], entry["path"], entry["kind"], sha, size))
        return revision, entries

    @staticmethod
    def materialize(snapshot: PaperSnapshot, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink() or any(dest.iterdir()):
            raise ValueError("Paper destination must be an empty directory")
        for file in snapshot.files:
            path = dest / paper_path(file.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as output:
                output.write(snapshot.file_bytes(file))


def manifest_for(connection: sqlite3.Connection, files: list[PaperFile]) -> str:
    manifest = []
    for file in sorted(files, key=lambda item: item.path):
        entry: dict[str, str | int] = {
            "file_id": file.file_id,
            "path": file.path,
            "kind": file.kind,
        }
        if file.kind == "text":
            body = (file.text or "").encode()
            sha = digest(body)
            connection.execute(
                "INSERT OR IGNORE INTO paper_version_texts VALUES (?, ?)",
                (sha, zlib.compress(body)),
            )
            entry["text_sha256"] = sha
            entry["size"] = len(body)
        else:
            entry["blob_sha256"] = file.blob_sha256 or ""
        manifest.append(entry)
    return json.dumps(manifest, sort_keys=True)


def read_snapshot(connection: sqlite3.Connection, revision: int, manifest: str) -> PaperSnapshot:
    files = []
    blobs = {}
    for entry in json.loads(manifest):
        entry = dict(entry)
        entry.pop("size", None)
        sha = entry.pop("text_sha256", None)
        if sha is not None:
            row = connection.execute(
                "SELECT zbody FROM paper_version_texts WHERE sha256 = ?", (sha,)
            ).fetchone()
            if row is None:
                raise ValueError("Paper history body is missing")
            entry["text"] = zlib.decompress(row[0]).decode()
        else:
            sha = entry["blob_sha256"]
            row = connection.execute(
                "SELECT bytes FROM paper_blobs WHERE sha256=?", (sha,)
            ).fetchone()
            if row is None:
                raise ValueError("Paper history blob is missing")
            blobs[sha] = bytes(row[0])
        files.append(PaperFile(**entry))
    snapshot = PaperSnapshot(revision=revision, files=files)
    snapshot._blobs = blobs
    return snapshot


def save_version(
    connection: sqlite3.Connection,
    project: str,
    snapshot: PaperSnapshot,
    actor: str,
    now: str,
    name: str | None = None,
) -> str:
    kind = "named" if name is not None else "auto"
    if name is not None:
        if not name.strip():
            raise ValueError("Version name must contain 1 to 200 characters")
        count = connection.execute(
            "SELECT COUNT(*) FROM paper_versions WHERE project_id=? AND kind='named'", (project,)
        ).fetchone()[0]
        if count >= limits.PAPER_HISTORY_NAMED_LIMIT:
            raise PaperLimit("Named Paper version quota reached")
    version_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO paper_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            project,
            version_id,
            snapshot.revision,
            kind,
            name,
            actor,
            now,
            manifest_for(connection, snapshot.files),
        ),
    )
    # Only a named request is refused at the quota; automatic history stays best effort.
    prune(connection, project, refuse=kind == "named")
    return version_id


def _protected_versions(connection: sqlite3.Connection, project: str) -> set[str]:
    rows = connection.execute(
        "SELECT turn_version_id FROM paper_state WHERE project_id=? UNION "
        "SELECT json_extract(r.payload_json, '$.before_version_id') FROM graph_run_receipts r "
        "JOIN graph_runs g ON g.operation_id=r.operation_id "
        "WHERE g.project_id=? AND r.category='paper_turn' UNION "
        "SELECT json_extract(r.payload_json, '$.after_version_id') FROM graph_run_receipts r "
        "JOIN graph_runs g ON g.operation_id=r.operation_id "
        "WHERE g.project_id=? AND r.category='paper_turn'",
        (project, project, project),
    )
    return {row[0] for row in rows if row[0] is not None}


def prune(connection: sqlite3.Connection, project: str, *, refuse: bool = False) -> None:
    """Thin automatic history to its budgets, keeping versions a turn list shows.

    Only a named-version request passes ``refuse=True``: edits never fail on a
    history quota that named and protected versions fill.
    """
    protected = sorted(_protected_versions(connection, project))
    protected_sql = ",".join("?" for _ in protected) or "SELECT NULL WHERE 0"
    connection.execute(
        "DELETE FROM paper_versions WHERE project_id=? AND kind='auto' "
        f"AND version_id NOT IN ({protected_sql}) AND version_id NOT IN "
        "(SELECT version_id FROM paper_versions WHERE project_id=? AND kind='auto' "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?)",
        (project, *protected, project, limits.PAPER_HISTORY_AUTO_LIMIT),
    )
    while _history_bytes(connection, project) > limits.PAPER_HISTORY_MAX_BYTES:
        row = connection.execute(
            "SELECT version_id FROM paper_versions WHERE project_id=? AND kind='auto' "
            f"AND version_id NOT IN ({protected_sql}) ORDER BY created_at, rowid LIMIT 1",
            (project, *protected),
        ).fetchone()
        if row:
            connection.execute(
                "DELETE FROM paper_versions WHERE project_id=? AND version_id=?", (project, row[0])
            )
            continue
        if not refuse:
            break
        raise PaperLimit("Paper history byte quota reached")
    connection.execute(
        "DELETE FROM paper_doc_states WHERE project_id=? AND file_id IN "
        "(SELECT file_id FROM paper_files WHERE project_id=? AND deleted_at IS NOT NULL)",
        (project, project),
    )


def collect_bodies(connection: sqlite3.Connection) -> None:
    """Rare global maintenance, separate from each project's history pruning."""
    for table, column in (("paper_version_texts", "text_sha256"), ("paper_blobs", "blob_sha256")):
        connection.execute(
            f"DELETE FROM {table} WHERE sha256 NOT IN ("
            f"SELECT json_extract(value, '$.{column}') FROM paper_versions, json_each(manifest) "
            f"WHERE json_extract(value, '$.{column}') IS NOT NULL)"
            + (
                " AND sha256 NOT IN (SELECT base_sha FROM paper_turn_files "
                "WHERE base_sha IS NOT NULL UNION SELECT coach_sha FROM paper_turn_files "
                "WHERE coach_sha IS NOT NULL)"
                if table == "paper_blobs"
                else ""
            )
        )


def _history_bytes(connection: sqlite3.Connection, project: str) -> int:
    texts, blobs = set(), set()
    for row in connection.execute(
        "SELECT manifest FROM paper_versions WHERE project_id=?", (project,)
    ):
        for entry in json.loads(row[0]):
            if "text_sha256" in entry:
                texts.add(entry["text_sha256"])
            else:
                blobs.add(entry["blob_sha256"])
    total = 0
    for table, column, hashes in (
        ("paper_version_texts", "length(zbody)", texts),
        ("paper_blobs", "size", blobs),
    ):
        for sha in hashes:
            row = connection.execute(
                f"SELECT {column} FROM {table} WHERE sha256=?", (sha,)
            ).fetchone()
            if row:
                total += row[0]
    return total
