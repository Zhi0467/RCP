"""Canonical backup-format bytes shared by capture and restore."""

from __future__ import annotations

import hashlib
import sqlite3

from rcp.server_ops._local_primitives import canonical_json_bytes, canonical_json_line
from rcp.server_ops.backup_models import BackupArchiveManifest


def canonical_backup_manifest_bytes(manifest: BackupArchiveManifest) -> bytes:
    """Return the one byte representation stored in protected archives."""

    return canonical_json_line(manifest.model_dump(mode="json"))


def database_schema_sha256(connection: sqlite3.Connection) -> str:
    """Hash the stable non-SQLite-owned schema projection."""

    rows = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_schema
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name, tbl_name
        """
    ).fetchall()
    payload = [
        {
            "type": row[0],
            "name": row[1],
            "tbl_name": row[2],
            "sql": row[3],
        }
        for row in rows
    ]
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


__all__ = ["canonical_backup_manifest_bytes", "database_schema_sha256"]
