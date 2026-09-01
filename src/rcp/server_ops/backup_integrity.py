"""Canonical backup-format bytes shared by capture and restore."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from rcp.server_ops.backup_models import BackupArchiveManifest


def canonical_backup_manifest_bytes(manifest: BackupArchiveManifest) -> bytes:
    """Return the one byte representation stored in protected archives."""

    return (
        json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


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
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["canonical_backup_manifest_bytes", "database_schema_sha256"]
