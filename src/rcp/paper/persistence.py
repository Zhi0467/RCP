"""SQLite snapshot bodies and retention, always called inside the owner's transaction."""

from __future__ import annotations

import json
import sqlite3
import uuid
import zlib
from pathlib import Path

from rcp import limits
from rcp.paper.models import (
    PaperFile,
    PaperLimit,
    PaperSnapshot,
    digest,
    paper_file_kind,
    paper_path,
)


class PaperSnapshotReader:
    """Read saved trees from exactly the caller's SQLite snapshot, without writes."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def manifest(self, project_id: str, revision: int | None = None) -> tuple[int, list[dict]]:
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
        return revision, json.loads(row[0])

    def snapshot(self, project_id: str, revision: int | None = None) -> PaperSnapshot:
        revision, entries = self.manifest(project_id, revision)
        return read_snapshot(self.connection, revision, json.dumps(entries))

    def raw_file(self, project_id: str, file_id: str, revision: int | None = None):
        revision, entries = self.manifest(project_id, revision)
        entry = next((entry for entry in entries if entry["file_id"] == file_id), None)
        if entry is None:
            raise KeyError(file_id)
        snapshot = read_snapshot(self.connection, revision, json.dumps([entry]))
        file = snapshot.files[0]
        data = (
            (file.text or "").encode()
            if file.kind == "text"
            else snapshot._blobs[file.blob_sha256 or ""]
        )
        return file, data

    def metadata(self, project_id: str):
        revision, entries = self.manifest(project_id)
        for entry in entries:
            if entry["kind"] == "binary":
                entry["size"] = self.connection.execute(
                    "SELECT size FROM paper_blobs WHERE sha256=?", (entry["blob_sha256"],)
                ).fetchone()[0]
        return revision, entries

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


def read_published_tree(root: Path) -> tuple[list[PaperFile], dict[str, bytes], int]:
    """Read a state root's published tree for a database that holds no Paper yet.

    Returns the files, their binary bodies, and the last published revision, so a
    fresh database adopts the tree and continues after it instead of replacing it.
    """
    from rcp.server_ops.backup_paper import paper_projection_sources

    files: list[PaperFile] = []
    blobs: dict[str, bytes] = {}
    for source in paper_projection_sources(root):
        if source.stat().st_size > limits.PAPER_MAX_TREE_BYTES:
            raise PaperLimit("Paper tree is too large")
        path = source.relative_to(root / "paper").as_posix()
        data = source.read_bytes()
        file = PaperFile(file_id=str(uuid.uuid4()), path=path, kind=paper_file_kind(path, data))
        if file.kind == "text":
            file.text = data.decode()
        else:
            file.blob_sha256 = digest(data)
            blobs[file.blob_sha256] = data
        files.append(file)
    marker = root / ".publish" / "paper-current.json"
    revision = json.loads(marker.read_text(encoding="utf-8"))["revision"] if marker.is_file() else 0
    return files, blobs, revision


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


def prune(connection: sqlite3.Connection, project: str, *, refuse: bool = False) -> None:
    """Thin history to its budgets, keeping what cannot be thinned.

    Only a named-version request passes ``refuse=True``: edits never fail on a
    history quota that named versions and protected bases fill.
    """
    connection.execute(
        "DELETE FROM paper_versions WHERE project_id=? AND kind='auto' AND version_id NOT IN "
        "(SELECT version_id FROM paper_versions WHERE project_id=? AND kind='auto' "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?)",
        (project, project, limits.PAPER_HISTORY_AUTO_LIMIT),
    )
    protected = {
        row[0]
        for row in connection.execute(
            "SELECT base_revision FROM paper_change_groups WHERE project_id=? "
            "AND state IN ('pending','outdated') UNION "
            "SELECT json_extract(r.payload_json, '$.revision') FROM graph_run_receipts r "
            "JOIN graph_runs g ON g.operation_id=r.operation_id "
            "WHERE g.project_id=? AND g.status IN ('queued','running','pausing') "
            "AND r.category='paper_turn_base'",
            (project, project),
        )
        if row[0] is not None
    }
    protected_sql = ",".join("?" for _ in protected) or "SELECT NULL WHERE 0"
    connection.execute(
        "DELETE FROM paper_saved_snapshots WHERE project_id=? AND revision NOT IN "
        "(SELECT revision FROM paper_saved_snapshots WHERE project_id=? ORDER BY revision DESC "
        f"LIMIT ?) AND revision NOT IN ({protected_sql})",
        (project, project, limits.PAPER_SAVED_REVISION_LIMIT, *protected),
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
            "SELECT revision FROM paper_saved_snapshots WHERE project_id=? "
            f"AND revision NOT IN ({protected_sql}) "
            "AND revision < (SELECT MAX(revision) FROM paper_saved_snapshots WHERE project_id=?) "
            "ORDER BY revision LIMIT 1",
            (project, *protected, project),
        ).fetchall()
        if rows:
            connection.execute(
                "DELETE FROM paper_saved_snapshots WHERE project_id=? AND revision=?",
                (project, rows[0][0]),
            )
            continue
        if not refuse:
            # Timed cleanup: named versions and protected bases fill the quota,
            # and the next history request is the one refused.
            break
        raise PaperLimit("Paper history byte quota reached")
    connection.execute(
        "DELETE FROM paper_revisions WHERE project_id=? AND revision <= MIN("
        "(SELECT published_revision FROM paper_publication WHERE project_id=?), "
        "(SELECT MAX(revision) FROM paper_versions WHERE project_id=?))",
        (project, project, project),
    )
    for table in ("paper_updates", "paper_doc_states"):
        connection.execute(
            f"DELETE FROM {table} WHERE project_id=? AND file_id IN "
            "(SELECT file_id FROM paper_files WHERE project_id=? AND deleted_at IS NOT NULL) "
            "AND file_id NOT IN (SELECT j.value FROM paper_change_receipts r, json_tree(r.payload) j "
            "WHERE r.project_id=? AND j.key='file_id' AND j.value IS NOT NULL)",
            (project, project, project),
        )


def collect_bodies(connection: sqlite3.Connection) -> None:
    """Rare global maintenance, separate from each project's history pruning."""
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
                " AND sha256 NOT IN (SELECT j.value FROM paper_change_receipts r, "
                "json_tree(r.payload) j WHERE j.key='blob_sha256' AND j.value IS NOT NULL)"
                " AND sha256 NOT IN (SELECT j.value FROM paper_changes c "
                "JOIN paper_change_groups g ON g.project_id=c.project_id AND g.group_id=c.group_id, "
                "json_each(c.payload) j WHERE g.state IN ('pending','outdated') "
                "AND j.key IN ('before_blob','after_blob') AND j.value IS NOT NULL)"
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
