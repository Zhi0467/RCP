"""Versioned saved-paper recovery section, shared by backup and transfer.

S1 seam: bind PAPER_SNAPSHOT_FACTORY to a read-only PaperLive over the supplied
SQLite transaction. It must not migrate, publish, or read the live database.
This slice uses the contract's SQLite BLOB variant. External blob storage needs
an S1-owned retention/export/import adapter before it can replace that variant.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import stat
import tempfile
import zlib
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from rcp.limits import BACKUP_INVENTORY_MAX_ENTRIES

PAPER_PROJECT_TABLES = (
    "paper_files",
    "paper_updates",
    "paper_doc_states",
    "paper_revisions",
    "paper_publication",
    "paper_versions",
    "paper_change_groups",
    "paper_changes",
    "paper_change_receipts",
)
PAPER_SHARED_TABLES = ("paper_blobs", "paper_version_texts")


class PaperSavedFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    file_id: str
    path: str
    kind: Literal["text", "binary"]
    text: str | None = None
    blob_sha256: str | None = None

    @model_validator(mode="after")
    def validate_file(self) -> PaperSavedFile:
        safe_paper_path(self.path)
        if self.kind == "text":
            if self.text is None or self.blob_sha256 is not None:
                raise ValueError("paper text requires exactly one text body")
        elif self.text is not None or not re.fullmatch(r"[0-9a-f]{64}", self.blob_sha256 or ""):
            raise ValueError("paper binary requires its content hash")
        return self


class SavedPaperSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")
    revision: int = Field(ge=0)
    files: tuple[PaperSavedFile, ...]


class PaperSnapshotReader(Protocol):
    def snapshot(self, project_id: str, revision: int | None = None) -> object: ...
    def materialize(self, snapshot: object, dest: Path) -> None: ...


# Explicit integration seams; no guessed PaperLive constructor or CRDT decoder.
PAPER_SNAPSHOT_FACTORY: Callable[[sqlite3.Connection], PaperSnapshotReader] | None = None


class PaperSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    project_id: str
    revision: int = Field(ge=0)
    files: tuple[PaperSavedFile, ...]
    tables: dict[str, tuple[dict[str, JsonValue], ...]]

    @model_validator(mode="after")
    def validate_section(self) -> PaperSection:
        if set(self.tables) - set((*PAPER_PROJECT_TABLES, *PAPER_SHARED_TABLES)):
            raise ValueError("unclassified paper table")
        for table, rows in self.tables.items():
            for row in rows:
                if any(not re.fullmatch(r"[a-z_][a-z0-9_]*", key) for key in row):
                    raise ValueError("invalid paper column")
                if table in PAPER_PROJECT_TABLES and row.get("project_id") != self.project_id:
                    raise ValueError("paper records belong to another project")
        publication = self.tables.get("paper_publication", ())
        if len(publication) != 1 or publication[0].get("saved_revision") != self.revision:
            raise ValueError("paper snapshot differs from saved publication revision")
        if len({file.file_id for file in self.files}) != len(self.files) or len(
            {file.path for file in self.files}
        ) != len(self.files):
            raise ValueError("paper snapshot repeats a file identity or path")
        live_files = {
            (row.get("file_id"), row.get("path"), row.get("kind"))
            for row in self.tables.get("paper_files", ())
            if row.get("deleted_at") is None
        }
        if live_files != {(file.file_id, file.path, file.kind) for file in self.files}:
            raise ValueError("paper snapshot differs from saved file identities")
        blobs: dict[str, bytes] = {}
        for row in self.tables.get("paper_blobs", ()):
            body = decode_cell(row.get("bytes"))
            if not isinstance(body, bytes) or hashlib.sha256(body).hexdigest() != row.get("sha256"):
                raise ValueError("paper binary does not match its hash")
            if row.get("size") != len(body):
                raise ValueError("paper binary does not match its size")
            digest = row.get("sha256")
            if not isinstance(digest, str):
                raise ValueError("invalid paper content hash")
            blobs[digest] = body
        texts = set()
        for row in self.tables.get("paper_version_texts", ()):
            body = decode_cell(row.get("zbody"))
            if not isinstance(body, bytes) or hashlib.sha256(
                zlib.decompress(body)
            ).hexdigest() != row.get("sha256"):
                raise ValueError("paper history text does not match its hash")
            digest = row.get("sha256")
            if not isinstance(digest, str):
                raise ValueError("invalid paper history hash")
            texts.add(digest)
        blob_refs, text_refs = _references(self.files, self.tables)
        if blob_refs != set(blobs) or text_refs != texts:
            raise ValueError("paper section content inventory differs from its references")
        return self


def safe_paper_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or not path.parts
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {".", "..", ".git", ".publish"} for part in path.parts)
        or "\\" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("unsafe paper path")
    return value


def paper_projection_sources(root: Path) -> tuple[Path, ...]:
    """Inventory regular projection files; never follow directory or file links."""
    paper = root / "paper"
    if not paper.exists() and not paper.is_symlink():
        return ()
    pending = [paper]
    files: list[Path] = []
    count = 0
    while pending:
        directory = pending.pop()
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise ValueError("unsafe paper projection directory")
        for path in sorted(directory.iterdir()):
            count += 1
            if count > BACKUP_INVENTORY_MAX_ENTRIES:
                raise ValueError("paper projection exceeds inventory bound")
            safe_paper_path(path.relative_to(paper).as_posix())
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                pending.append(path)
            elif stat.S_ISREG(mode):
                files.append(path)
            else:
                raise ValueError("unsafe paper projection entry")
    return tuple(sorted(files))


def encode_cell(value: object) -> JsonValue:
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if value is None or isinstance(value, (str, int, float)):
        return value
    raise ValueError("unsupported SQLite paper value")


def decode_cell(value: JsonValue) -> str | int | float | bytes | None:
    if isinstance(value, dict) and set(value) == {"$bytes"} and isinstance(value["$bytes"], str):
        return base64.b64decode(value["$bytes"], validate=True)
    if value is None or isinstance(value, (str, int, float)):
        return value
    raise ValueError("invalid encoded SQLite paper value")


def _rows(connection: sqlite3.Connection, table: str, where: str = "", args: tuple = ()):
    cursor = connection.execute(f'SELECT * FROM "{table}" {where}', args)
    columns = [item[0] for item in cursor.description]
    rows = [
        dict(zip(columns, (encode_cell(value) for value in row), strict=True)) for row in cursor
    ]
    return tuple(sorted(rows, key=lambda row: json.dumps(row, sort_keys=True)))


def _references(files, tables) -> tuple[set[str], set[str]]:
    blobs = {file.blob_sha256 for file in files if file.blob_sha256 is not None}
    texts: set[str] = set()

    # History and coach receipts may retain deleted/replaced content.
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"blob_sha256", "text_sha256"} and isinstance(item, str):
                    (blobs if key == "blob_sha256" else texts).add(item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str) and value[:1] in {"[", "{"}:
            with suppress(ValueError):
                visit(json.loads(value))

    for table, rows in tables.items():
        if table in PAPER_PROJECT_TABLES:
            for row in rows:
                visit(row)
    return blobs, texts


def capture_paper_section(connection: sqlite3.Connection, project_id: str) -> PaperSection | None:
    """Read one caller-owned snapshot transaction, including its CRDT and history."""
    present = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
    }
    tables = {
        table: _rows(connection, table, "WHERE project_id = ?", (project_id,))
        for table in PAPER_PROJECT_TABLES
        if table in present
    }
    if not any(tables.values()):
        return None
    if PAPER_SNAPSHOT_FACTORY is None:
        raise ValueError("S1 PaperLive snapshot factory is not bound")
    owner = PAPER_SNAPSHOT_FACTORY(connection)
    raw = owner.snapshot(project_id)
    snapshot = SavedPaperSnapshot.model_validate(
        raw.model_dump() if isinstance(raw, BaseModel) else raw
    )
    with tempfile.TemporaryDirectory(prefix="rcp-paper-capture-") as temporary:
        destination = Path(temporary) / "paper"
        owner.materialize(raw, destination)
        actual = {
            path.relative_to(destination).as_posix(): path
            for path in paper_projection_sources(Path(temporary))
        }
        if set(actual) != {file.path for file in snapshot.files}:
            raise ValueError("materialized paper inventory differs from saved snapshot")
        for file in snapshot.files:
            body = actual[file.path].read_bytes()
            if (file.kind == "text" and body != (file.text or "").encode()) or (
                file.kind == "binary" and hashlib.sha256(body).hexdigest() != file.blob_sha256
            ):
                raise ValueError("materialized paper differs from saved snapshot")
    blob_refs, text_refs = _references(snapshot.files, tables)
    for table, hashes in (("paper_blobs", blob_refs), ("paper_version_texts", text_refs)):
        rows = []
        for digest in sorted(hashes):
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("invalid paper content hash")
            found = _rows(connection, table, "WHERE sha256 = ?", (digest,))
            if len(found) != 1:
                raise ValueError("missing paper content")
            row = found[0]
            if table == "paper_blobs" and "bytes" not in row:
                raise ValueError("paper recovery requires SQLite-backed binary content")
            rows.append(row)
        tables[table] = tuple(rows)
    return PaperSection(
        project_id=project_id, revision=snapshot.revision, files=snapshot.files, tables=tables
    )


def restore_paper_section(
    connection: sqlite3.Connection, section: PaperSection, project_id: str | None = None
) -> None:
    """Insert exact durable state into the caller's transaction; never publish it."""
    section = PaperSection.model_validate(section.model_dump())
    project_id = project_id or section.project_id
    for table in (*PAPER_SHARED_TABLES, *PAPER_PROJECT_TABLES):
        for archived in section.tables.get(table, ()):
            row = dict(archived)
            if table in PAPER_PROJECT_TABLES:
                row["project_id"] = project_id
            columns = tuple(row)
            if table in PAPER_SHARED_TABLES:
                existing = _rows(connection, table, "WHERE sha256 = ?", (row["sha256"],))
                if existing:
                    if existing != (row,):
                        raise ValueError("paper shared content collision")
                    continue
            names = ", ".join(f'"{column}"' for column in columns)
            placeholders = ", ".join("?" for _ in columns)
            connection.execute(
                f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})',
                tuple(decode_cell(row[column]) for column in columns),
            )


def verify_paper_section_records(connection: sqlite3.Connection, section: PaperSection) -> None:
    """Bind the portable section to the exact archived SQLite revision."""
    for table in PAPER_PROJECT_TABLES:
        present = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)
        ).fetchone()
        rows = (
            _rows(connection, table, "WHERE project_id = ?", (section.project_id,))
            if present
            else ()
        )
        if rows != section.tables.get(table, ()):
            raise ValueError("paper section differs from archived SQLite records")
    for table in PAPER_SHARED_TABLES:
        for row in section.tables.get(table, ()):
            if _rows(connection, table, "WHERE sha256 = ?", (row["sha256"],)) != (row,):
                raise ValueError("paper section differs from archived SQLite content")


# S1 migration-registry seam: exact sqlite_schema rows for each fresh/upgraded
# Paper shape, including indexes/triggers. No permissive schema-name exception.
PAPER_RESTORE_SCHEMA_VARIANTS: tuple[tuple[tuple[str, str, str, str | None], ...], ...] = ()


def paper_restore_base_schema(connection: sqlite3.Connection) -> str | None:
    """Recognize only registered paper DDL over an independently supported era."""
    from rcp.server_ops._local_primitives import canonical_json_bytes

    rows = [
        tuple(row)
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name, tbl_name"
        )
    ]
    tables = set((*PAPER_PROJECT_TABLES, *PAPER_SHARED_TABLES))
    paper = tuple(row for row in rows if row[2] in tables)
    if not paper or paper not in PAPER_RESTORE_SCHEMA_VARIANTS:
        return None
    payload = [
        dict(zip(("type", "name", "tbl_name", "sql"), row, strict=True))
        for row in rows
        if row[2] not in tables
    ]
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
