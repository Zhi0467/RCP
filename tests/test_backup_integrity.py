from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from rcp.server_ops.backup_integrity import (
    canonical_backup_manifest_bytes,
    database_schema_sha256,
)
from rcp.server_ops.backup_models import BackupArchiveManifest
from rcp.storage import AppStore

_HISTORICAL_MANIFEST = (
    b'{"captured_app_data_entries":[],"captured_at":"2026-08-29T12:00:00Z",'
    b'"database_schema_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
    b'"encryption_recipient_fingerprint":"f6e86359f7f66188375fd4c01222fc6774f5f82de588564169e8c178da5533d4",'
    b'"excluded_app_data_entries":["rcp.lock"],"imported_sources":[],'
    b'"installation_id":"69726714-fee6-427f-8e1b-337350518beb","projects":[],'
    b'"rcp_source_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","schema_version":1,'
    b'"source_deploy_key_label":null,"source_public_key_fingerprint":null,'
    b'"space_id":"70994440-4c57-41b0-a2f6-8878856db969","space_name":"Backup Lab",'
    b'"sqlite_snapshot":{"archive_path":"database/rcp.sqlite3","group":"sqlite_snapshot",'
    b'"sha256":"fcc6e6dba857aeb8abfbbf8536c7022e2b69198d79a4be2eaefa2f35820a3209",'
    b'"size_bytes":24,"source_relative_path":"rcp.sqlite3"},"status":"complete",'
    b'"total_bytes":24,"uncaptured_app_data_entries":[]}\n'
)


def test_historical_backup_manifest_keeps_its_canonical_bytes_and_digest() -> None:
    manifest = BackupArchiveManifest.model_validate_json(_HISTORICAL_MANIFEST)

    assert canonical_backup_manifest_bytes(manifest) == _HISTORICAL_MANIFEST
    assert hashlib.sha256(_HISTORICAL_MANIFEST).hexdigest() == (
        "49e6d3e263ba877cedbf7775ee44b42a2f2eff7ec6b510e2f3c10abe397ca2f1"
    )


def test_database_schema_digest_is_shared_by_live_and_immutable_readers(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rcp.sqlite3"
    store, _ = AppStore.initialize_team_space(database, "Backup integrity")

    with store.connection() as live:
        live_digest = database_schema_sha256(live)
    uri = f"{database.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as immutable:
        immutable_digest = database_schema_sha256(immutable)

    assert live_digest == immutable_digest
    assert live_digest == "192554f3171d2758042cafe337c0c76ca67496f72a6107b5fbd5aa6bebf54e63"
