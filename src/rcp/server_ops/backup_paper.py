"""Versioned saved-paper recovery section, shared by backup and transfer.

Saved trees are decoded by the live owner's read-only persistence reader over
the supplied SQLite transaction. Binary bodies remain in that same snapshot.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import stat
import zlib
from contextlib import suppress
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from rcp.limits import BACKUP_INVENTORY_MAX_ENTRIES

PAPER_PROJECT_TABLES = (
    "paper_files",
    "paper_updates",
    "paper_doc_states",
    "paper_revisions",
    "paper_publication",
    "paper_versions",
    "paper_apply_receipts",
    "paper_saved_snapshots",
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
    from rcp.paper.models import paper_path

    return paper_path(value)


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
                # Deleted identities survive after their last retained body expires.
                # A retained version/snapshot supplies any remaining body reference.
                if table == "paper_files" and row.get("deleted_at") is not None:
                    continue
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
    from rcp.paper.persistence import PaperSnapshotReader

    raw = PaperSnapshotReader(connection).snapshot(project_id)
    snapshot = SavedPaperSnapshot.model_validate(raw.model_dump())
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
    tables = dict(section.tables)
    legacy_receipts = tables.pop("paper_apply_receipts", ())
    if legacy_receipts:
        receipts = {
            str(row["receipt_key"]): dict(row) for row in tables.get("paper_change_receipts", ())
        }
        for legacy in legacy_receipts:
            key = str(legacy["receipt_key"])
            row = receipts.setdefault(
                key,
                {
                    "project_id": project_id,
                    "receipt_key": key,
                    "group_id": None,
                    "action": "apply",
                    "payload": "{}",
                    "created_at": "",
                },
            )
            payload = json.loads(str(row["payload"]))
            payload.update(
                fingerprint=legacy["fingerprint"], receipt=json.loads(str(legacy["receipt"]))
            )
            row["payload"] = json.dumps(payload)
        tables["paper_change_receipts"] = tuple(receipts.values())
    for table in (*PAPER_SHARED_TABLES, *PAPER_PROJECT_TABLES):
        for archived in tables.get(table, ()):
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


# Paper migrations use identical CREATE statements for fresh and upgraded databases.
# This exact allowlist is independent of the schema being validated.
PAPER_RESTORE_SCHEMA_VARIANTS = (
    (
        (
            "index",
            "paper_files_live_path",
            "paper_files",
            "CREATE UNIQUE INDEX paper_files_live_path\n"
            "                ON paper_files(project_id, path) WHERE deleted_at IS NULL",
        ),
        (
            "index",
            "paper_versions_revision",
            "paper_versions",
            "CREATE INDEX paper_versions_revision\n"
            "                ON paper_versions(project_id, revision)",
        ),
        (
            "table",
            "paper_apply_receipts",
            "paper_apply_receipts",
            "CREATE TABLE paper_apply_receipts (\n"
            "                project_id TEXT NOT NULL,\n"
            "                receipt_key TEXT NOT NULL,\n"
            "                fingerprint TEXT NOT NULL,\n"
            "                receipt TEXT NOT NULL,\n"
            "                PRIMARY KEY(project_id, receipt_key)\n"
            "            )",
        ),
        (
            "table",
            "paper_blobs",
            "paper_blobs",
            "CREATE TABLE paper_blobs (\n"
            "                sha256 TEXT PRIMARY KEY,\n"
            "                bytes BLOB NOT NULL,\n"
            "                size INTEGER NOT NULL CHECK(size >= 0)\n"
            "            )",
        ),
        (
            "table",
            "paper_doc_states",
            "paper_doc_states",
            "CREATE TABLE paper_doc_states (\n"
            "                project_id TEXT NOT NULL,\n"
            "                file_id TEXT NOT NULL,\n"
            "                state BLOB NOT NULL,\n"
            "                through_seq INTEGER NOT NULL,\n"
            "                through_revision INTEGER NOT NULL DEFAULT 0,\n"
            "                PRIMARY KEY(project_id, file_id)\n"
            "            )",
        ),
        (
            "table",
            "paper_files",
            "paper_files",
            "CREATE TABLE paper_files (\n"
            "                project_id TEXT NOT NULL,\n"
            "                file_id TEXT NOT NULL,\n"
            "                path TEXT NOT NULL,\n"
            "                kind TEXT NOT NULL CHECK(kind IN ('text', 'binary')),\n"
            "                created_at TEXT NOT NULL,\n"
            "                deleted_at TEXT,\n"
            "                blob_sha256 TEXT,\n"
            "                PRIMARY KEY(project_id, file_id)\n"
            "            )",
        ),
        (
            "table",
            "paper_publication",
            "paper_publication",
            "CREATE TABLE paper_publication (\n"
            "                project_id TEXT PRIMARY KEY,\n"
            "                saved_revision INTEGER NOT NULL DEFAULT 0,\n"
            "                published_revision INTEGER NOT NULL DEFAULT 0,\n"
            "                pending_since TEXT,\n"
            "                last_error TEXT,\n"
            "                hold TEXT,\n"
            "                last_edit_at TEXT\n"
            "            )",
        ),
        (
            "table",
            "paper_revisions",
            "paper_revisions",
            "CREATE TABLE paper_revisions (\n"
            "                project_id TEXT NOT NULL,\n"
            "                revision INTEGER NOT NULL,\n"
            "                created_at TEXT NOT NULL,\n"
            "                actor TEXT NOT NULL,\n"
            "                kind TEXT NOT NULL,\n"
            "                PRIMARY KEY(project_id, revision)\n"
            "            )",
        ),
        (
            "table",
            "paper_saved_snapshots",
            "paper_saved_snapshots",
            "CREATE TABLE paper_saved_snapshots (\n"
            "                project_id TEXT NOT NULL,\n"
            "                revision INTEGER NOT NULL,\n"
            "                manifest TEXT NOT NULL,\n"
            "                PRIMARY KEY(project_id, revision)\n"
            "            )",
        ),
        (
            "table",
            "paper_updates",
            "paper_updates",
            "CREATE TABLE paper_updates (\n"
            "                project_id TEXT NOT NULL,\n"
            "                file_id TEXT NOT NULL,\n"
            "                seq INTEGER NOT NULL,\n"
            '                "update" BLOB NOT NULL,\n'
            "                revision INTEGER NOT NULL,\n"
            "                actor TEXT NOT NULL,\n"
            "                created_at TEXT NOT NULL,\n"
            "                PRIMARY KEY(project_id, file_id, seq)\n"
            "            )",
        ),
        (
            "table",
            "paper_version_texts",
            "paper_version_texts",
            "CREATE TABLE paper_version_texts (\n"
            "                sha256 TEXT PRIMARY KEY,\n"
            "                zbody BLOB NOT NULL\n"
            "            )",
        ),
        (
            "table",
            "paper_versions",
            "paper_versions",
            "CREATE TABLE paper_versions (\n"
            "                project_id TEXT NOT NULL,\n"
            "                version_id TEXT NOT NULL,\n"
            "                revision INTEGER NOT NULL,\n"
            "                kind TEXT NOT NULL CHECK(kind IN ('auto', 'named')),\n"
            "                name TEXT,\n"
            "                actor TEXT NOT NULL,\n"
            "                created_at TEXT NOT NULL,\n"
            "                manifest TEXT NOT NULL,\n"
            "                PRIMARY KEY(project_id, version_id)\n"
            "            )",
        ),
        # Saves and publication invalidate the Paper badge (resource_versions).
        (
            "trigger",
            "resource_paper_publication_delete",
            "paper_publication",
            "CREATE TRIGGER resource_paper_publication_delete BEFORE DELETE ON \"paper_publication\"  BEGIN INSERT INTO resource_versions(scope,resource,entity,generation) SELECT OLD.project_id,'paper',COALESCE('',''),1 WHERE (OLD.project_id) IS NOT NULL AND (1) ON CONFLICT(scope,resource,entity) DO UPDATE SET generation=generation+1; END",
        ),
        (
            "trigger",
            "resource_paper_publication_insert",
            "paper_publication",
            "CREATE TRIGGER resource_paper_publication_insert AFTER INSERT ON \"paper_publication\"  BEGIN INSERT INTO resource_versions(scope,resource,entity,generation) SELECT NEW.project_id,'paper',COALESCE('',''),1 WHERE (NEW.project_id) IS NOT NULL AND (1) ON CONFLICT(scope,resource,entity) DO UPDATE SET generation=generation+1; END",
        ),
        (
            "trigger",
            "resource_paper_publication_update",
            "paper_publication",
            'CREATE TRIGGER resource_paper_publication_update AFTER UPDATE ON "paper_publication" WHEN OLD."saved_revision" IS NOT NEW."saved_revision" OR OLD."published_revision" IS NOT NEW."published_revision" OR OLD."pending_since" IS NOT NEW."pending_since" OR OLD."last_error" IS NOT NEW."last_error" OR OLD."hold" IS NOT NEW."hold" BEGIN INSERT INTO resource_versions(scope,resource,entity,generation) SELECT NEW.project_id,\'paper\',COALESCE(\'\',\'\'),1 WHERE (NEW.project_id) IS NOT NULL AND (1) ON CONFLICT(scope,resource,entity) DO UPDATE SET generation=generation+1; INSERT INTO resource_versions(scope,resource,entity,generation) SELECT OLD.project_id,\'paper\',COALESCE(\'\',\'\'),1 WHERE (OLD.project_id) IS NOT NULL AND (1) ON CONFLICT(scope,resource,entity) DO UPDATE SET generation=generation+1; END',
        ),
    ),
)


# Migration 64 adds durable coach cards and prepared mutation receipts.
_PAPER_CHANGES_SCHEMA = (
    (
        "index",
        "paper_change_groups_task",
        "paper_change_groups",
        "CREATE INDEX paper_change_groups_task\n"
        "            ON paper_change_groups(project_id, task_id, created_at)",
    ),
    (
        "table",
        "paper_change_groups",
        "paper_change_groups",
        "CREATE TABLE paper_change_groups (\n"
        "            project_id TEXT NOT NULL, group_id TEXT NOT NULL, task_id TEXT NOT NULL,\n"
        "            base_revision INTEGER NOT NULL, mode TEXT NOT NULL, state TEXT NOT NULL,\n"
        "            decided_by TEXT, decided_at TEXT, created_at TEXT NOT NULL,\n"
        "            PRIMARY KEY (project_id, group_id))",
    ),
    (
        "table",
        "paper_change_receipts",
        "paper_change_receipts",
        "CREATE TABLE paper_change_receipts (\n"
        "            project_id TEXT NOT NULL, receipt_key TEXT NOT NULL, group_id TEXT NOT NULL,\n"
        "            action TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,\n"
        "            PRIMARY KEY (project_id, receipt_key),\n"
        "            FOREIGN KEY (project_id, group_id)\n"
        "                REFERENCES paper_change_groups(project_id, group_id) ON DELETE CASCADE)",
    ),
    (
        "table",
        "paper_changes",
        "paper_changes",
        "CREATE TABLE paper_changes (\n"
        "            project_id TEXT NOT NULL, change_id TEXT NOT NULL, group_id TEXT NOT NULL,\n"
        "            payload TEXT NOT NULL, PRIMARY KEY (project_id, change_id),\n"
        "            FOREIGN KEY (project_id, group_id)\n"
        "                REFERENCES paper_change_groups(project_id, group_id) ON DELETE CASCADE)",
    ),
)
PAPER_RESTORE_SCHEMA_VARIANTS += (
    tuple(sorted((*PAPER_RESTORE_SCHEMA_VARIANTS[0], *_PAPER_CHANGES_SCHEMA))),
)
# Migration 65 unifies live results and prepared coach receipts in one table.
PAPER_RESTORE_SCHEMA_VARIANTS += (
    tuple(
        sorted(
            (
                *(
                    row
                    for row in PAPER_RESTORE_SCHEMA_VARIANTS[1]
                    if row[2] not in {"paper_apply_receipts", "paper_change_receipts"}
                ),
                (
                    "table",
                    "paper_change_receipts",
                    "paper_change_receipts",
                    "CREATE TABLE paper_change_receipts (\n"
                    "        project_id TEXT NOT NULL, receipt_key TEXT NOT NULL, group_id TEXT,\n"
                    "        action TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,\n"
                    "        PRIMARY KEY (project_id, receipt_key),\n"
                    "        FOREIGN KEY (project_id, group_id)\n"
                    "            REFERENCES paper_change_groups(project_id, group_id) ON DELETE CASCADE)",
                ),
            )
        )
    ),
)


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
