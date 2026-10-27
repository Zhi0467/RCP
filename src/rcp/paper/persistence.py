"""SQLite snapshot bodies and retention, always called inside the owner's transaction."""

from __future__ import annotations

import json
import sqlite3
import uuid
import zlib
from pathlib import Path

from rcp import limits
from rcp.paper.models import PaperFile, PaperLimit, PaperSnapshot, digest, paper_path


class PaperSnapshotReader:
    """Read saved trees from exactly the caller's SQLite snapshot, without writes."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def snapshot(self, project_id: str, revision: int | None = None) -> PaperSnapshot:
        if revision is None:
            row = self.connection.execute(
                "SELECT saved_revision FROM paper_publication WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if row is None:
                raise KeyError("Paper has no saved tree")
            revision = int(row[0])
        row = self.connection.execute(
            "SELECT manifest FROM paper_saved_snapshots WHERE project_id=? AND revision=?",
            (project_id, revision),
        ).fetchone()
        if row is None:
            row = self.connection.execute(
                "SELECT manifest FROM paper_versions WHERE project_id=? AND revision=? "
                "ORDER BY created_at DESC LIMIT 1",
                (project_id, revision),
            ).fetchone()
        if row is None:
            raise KeyError("Paper revision is no longer retained")
        return read_snapshot(self.connection, revision, row[0])

    @staticmethod
    def materialize(snapshot: PaperSnapshot, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink() or any(dest.iterdir()):
            raise ValueError("Paper destination must be an empty directory")
        for file in snapshot.files:
            path = dest / paper_path(file.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = (
                (file.text or "").encode()
                if file.kind == "text"
                else snapshot._blobs[file.blob_sha256 or ""]
            )
            with path.open("xb") as output:
                output.write(data)


def manifest_for(connection: sqlite3.Connection, files: list[PaperFile]) -> str:
    manifest = []
    for file in sorted(files, key=lambda item: item.path):
        entry = {"file_id": file.file_id, "path": file.path, "kind": file.kind}
        if file.kind == "text":
            body = (file.text or "").encode()
            sha = digest(body)
            connection.execute(
                "INSERT OR IGNORE INTO paper_version_texts VALUES (?, ?)",
                (sha, zlib.compress(body)),
            )
            entry["text_sha256"] = sha
        else:
            entry["blob_sha256"] = file.blob_sha256 or ""
        manifest.append(entry)
    return json.dumps(manifest, sort_keys=True)


def read_snapshot(connection: sqlite3.Connection, revision: int, manifest: str) -> PaperSnapshot:
    files = []
    blobs = {}
    for entry in json.loads(manifest):
        entry = dict(entry)
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
        if not name.strip() or len(name) > 200:
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
    prune(connection, project)
    return version_id


def prune(connection: sqlite3.Connection, project: str) -> None:
    connection.execute(
        "DELETE FROM paper_versions WHERE project_id=? AND kind='auto' AND version_id NOT IN "
        "(SELECT version_id FROM paper_versions WHERE project_id=? AND kind='auto' "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?)",
        (project, project, limits.PAPER_HISTORY_AUTO_LIMIT),
    )
    connection.execute(
        "DELETE FROM paper_saved_snapshots WHERE project_id=? AND revision NOT IN "
        "(SELECT revision FROM paper_saved_snapshots WHERE project_id=? ORDER BY revision DESC "
        "LIMIT ?)",
        (project, project, limits.PAPER_SAVED_REVISION_LIMIT),
    )
    while _history_bytes(connection, project) > limits.PAPER_HISTORY_MAX_BYTES:
        row = connection.execute(
            "SELECT version_id FROM paper_versions WHERE project_id=? AND kind='auto' "
            "ORDER BY created_at, rowid LIMIT 1",
            (project,),
        ).fetchone()
        if row:
            connection.execute(
                "DELETE FROM paper_versions WHERE project_id=? AND version_id=?", (project, row[0])
            )
            continue
        rows = connection.execute(
            "SELECT revision FROM paper_saved_snapshots WHERE project_id=? ORDER BY revision LIMIT 2",
            (project,),
        ).fetchall()
        if len(rows) > 1:
            connection.execute(
                "DELETE FROM paper_saved_snapshots WHERE project_id=? AND revision=?",
                (project, rows[0][0]),
            )
            continue
        raise PaperLimit("Paper history byte quota reached")
    for table in ("paper_updates", "paper_doc_states"):
        connection.execute(
            f"DELETE FROM {table} WHERE project_id=? AND file_id IN "
            "(SELECT file_id FROM paper_files WHERE project_id=? AND deleted_at IS NOT NULL)",
            (project, project),
        )
    # Bodies are shared across projects and both history collections.
    for table, column in (("paper_version_texts", "text_sha256"), ("paper_blobs", "blob_sha256")):
        connection.execute(
            f"DELETE FROM {table} WHERE sha256 NOT IN ("
            f"SELECT json_extract(value, '$.{column}') FROM paper_saved_snapshots, "
            "json_each(manifest) WHERE json_extract(value, '$." + column + "') IS NOT NULL UNION "
            f"SELECT json_extract(value, '$.{column}') FROM paper_versions, json_each(manifest) "
            "WHERE json_extract(value, '$."
            + column
            + "') IS NOT NULL)"
            + (
                " AND sha256 NOT IN (SELECT blob_sha256 FROM paper_files "
                "WHERE blob_sha256 IS NOT NULL AND deleted_at IS NULL)"
                if table == "paper_blobs"
                else ""
            )
        )


def _history_bytes(connection: sqlite3.Connection, project: str) -> int:
    texts, blobs = set(), set()
    for row in connection.execute(
        "SELECT manifest FROM paper_saved_snapshots WHERE project_id=? UNION ALL "
        "SELECT manifest FROM paper_versions WHERE project_id=?",
        (project, project),
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
