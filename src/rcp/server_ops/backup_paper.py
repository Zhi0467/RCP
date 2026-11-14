"""Versioned Paper recovery section carried by transfer.

Files are the Paper folder's bytes, read by the caller. Tables are Paper
identity and History from the supplied SQLite transaction, plus every body
they or the files name. A section from before the folder may also hold the
retired tables; restoring one leaves the startup migration to settle them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import uuid
import zlib
from collections.abc import Mapping
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

PAPER_PROJECT_TABLES = (
    "paper_files",
    "paper_updates",
    "paper_doc_states",
    "paper_revisions",
    "paper_publication",
    "paper_versions",
    "paper_saved_snapshots",
    "paper_change_groups",
    "paper_changes",
    "paper_change_receipts",
)
PAPER_SHARED_TABLES = ("paper_blobs", "paper_version_texts")
PAPER_SECTION_TABLES = ("paper_files", "paper_versions")


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
        publication = self.tables.get("paper_publication")
        if publication is not None and (
            len(publication) != 1 or publication[0].get("saved_revision") != self.revision
        ):
            raise ValueError("paper snapshot differs from saved publication revision")
        if len({file.file_id for file in self.files}) != len(self.files) or len(
            {file.path for file in self.files}
        ) != len(self.files):
            raise ValueError("paper snapshot repeats a file identity or path")
        blobs: dict[str, bytes] = {}
        for row in self.tables.get("paper_blobs", ()):
            body = decode_cell(row.get("bytes"))
            if not isinstance(body, bytes) or hashlib.sha256(body).hexdigest() != row.get("sha256"):
                raise ValueError("paper binary does not match its hash")
            if row.get("size") != len(body):
                raise ValueError("paper binary does not match its size")
            digest = row.get("sha256")
            blobs[cast(str, digest)] = body
        texts = set()
        for row in self.tables.get("paper_version_texts", ()):
            body = decode_cell(row.get("zbody"))
            if not isinstance(body, bytes) or hashlib.sha256(
                zlib.decompress(body)
            ).hexdigest() != row.get("sha256"):
                raise ValueError("paper history text does not match its hash")
            digest = row.get("sha256")
            texts.add(digest)
        blob_refs, text_refs = _references(self.files, self.tables)
        if blob_refs != set(blobs) or text_refs != texts:
            raise ValueError("paper section content inventory differs from its references")
        return self


def safe_paper_path(value: str) -> str:
    from rcp.paper.models import paper_path

    return paper_path(value)


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
    """Body references, read like ``collect_bodies``: only the JSON columns are decoded,
    so document text that happens to look like JSON is never taken for metadata."""
    blobs = {file.blob_sha256 for file in files if file.blob_sha256 is not None}
    texts: set[str] = set()

    def receipt_blobs(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "blob_sha256" and isinstance(item, str):
                    blobs.add(item)
                else:
                    receipt_blobs(item)
        elif isinstance(value, list):
            for item in value:
                receipt_blobs(item)

    # History may retain deleted/replaced content.
    for table in ("paper_versions", "paper_saved_snapshots"):
        for row in tables.get(table, ()):
            for entry in json.loads(row["manifest"]):
                if entry.get("text_sha256"):
                    texts.add(entry["text_sha256"])
                if entry.get("blob_sha256"):
                    blobs.add(entry["blob_sha256"])
    for row in tables.get("paper_change_receipts", ()):
        receipt_blobs(json.loads(row["payload"]))
    # Only undecided cards hold their bodies; a decided card's surviving bodies are
    # held by its receipts.
    undecided = {
        row["group_id"]
        for row in tables.get("paper_change_groups", ())
        if row["state"] in {"pending", "outdated"}
    }
    for row in tables.get("paper_changes", ()):
        if row["group_id"] in undecided:
            change = json.loads(row["payload"])
            blobs.update(change[key] for key in ("before_blob", "after_blob") if change.get(key))
    return blobs, texts


def capture_paper_section(
    connection: sqlite3.Connection, project_id: str, files: Mapping[str, bytes]
) -> PaperSection | None:
    """Pair the folder's bytes with identity and History from one caller-owned snapshot."""
    present = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
    }
    tables = {
        table: _rows(connection, table, "WHERE project_id = ?", (project_id,))
        for table in PAPER_SECTION_TABLES
        if table in present
    }
    if not files and not any(tables.values()):
        return None
    from rcp.paper.models import paper_file_kind

    revision = (
        connection.execute(
            "SELECT revision FROM paper_state WHERE project_id = ?", (project_id,)
        ).fetchone()
        if "paper_state" in present
        else None
    )
    identities = {
        str(row["path"]): str(row["file_id"])
        for row in tables.get("paper_files", ())
        if row.get("deleted_at") is None
    }
    saved = []
    folder_blobs: dict[str, bytes] = {}
    for path, data in sorted(files.items()):
        file_id = identities.get(path) or str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{project_id}/{path}")
        )
        if paper_file_kind(path, data) == "text":
            saved.append(
                PaperSavedFile(file_id=file_id, path=path, kind="text", text=data.decode())
            )
            continue
        digest = hashlib.sha256(data).hexdigest()
        folder_blobs[digest] = data
        saved.append(PaperSavedFile(file_id=file_id, path=path, kind="binary", blob_sha256=digest))
    blob_refs, text_refs = _references(saved, tables)
    for table, hashes in (("paper_blobs", blob_refs), ("paper_version_texts", text_refs)):
        rows = []
        for digest in sorted(hashes):
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("invalid paper content hash")
            if table == "paper_blobs" and digest in folder_blobs:
                body = folder_blobs[digest]
                rows.append({"sha256": digest, "bytes": encode_cell(body), "size": len(body)})
                continue
            found = _rows(connection, table, "WHERE sha256 = ?", (digest,))
            if len(found) != 1:
                raise ValueError("missing paper content")
            row = found[0]
            if table == "paper_blobs" and "bytes" not in row:
                raise ValueError("paper recovery requires SQLite-backed binary content")
            rows.append(row)
        tables[table] = tuple(rows)
    return PaperSection(
        project_id=project_id,
        revision=revision[0] if revision else 0,
        files=tuple(saved),
        tables=tables,
    )


def section_files(section: PaperSection) -> dict[str, bytes]:
    """The folder bytes a section carries, to write after its rows commit."""
    blobs = {
        str(row["sha256"]): cast(bytes, decode_cell(row["bytes"]))
        for row in section.tables.get("paper_blobs", ())
    }
    return {
        file.path: (file.text or "").encode()
        if file.kind == "text"
        else blobs[file.blob_sha256 or ""]
        for file in section.files
    }


def restore_paper_section(
    connection: sqlite3.Connection, section: PaperSection, project_id: str | None = None
) -> None:
    """Insert exact durable rows into the caller's transaction; the caller writes the files."""
    project_id = project_id or section.project_id
    git_groups = {
        str(row["group_id"])
        for row in section.tables.get("paper_change_groups", ())
        if row.get("task_id") == "paper-git"
    }
    for table in (*PAPER_SHARED_TABLES, *PAPER_PROJECT_TABLES):
        for archived in section.tables.get(table, ()):
            row = dict(archived)
            if (
                table in {"paper_change_groups", "paper_changes", "paper_change_receipts"}
                and row.get("group_id") in git_groups
            ):
                continue
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
    if "paper_publication" not in section.tables:
        connection.execute(
            "INSERT OR IGNORE INTO paper_state(project_id, revision) VALUES (?, ?)",
            (project_id, section.revision),
        )
