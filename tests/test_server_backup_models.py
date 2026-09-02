from __future__ import annotations

from datetime import UTC, datetime

from rcp.server_ops.backup_models import BackupArchiveManifest, BackupFileEntry
from rcp.server_ops.config import ServerSourceConfig, create_installed_server_config

INSTALLATION_ID = "69726714-fee6-427f-8e1b-337350518beb"
FINGERPRINT = "SHA256:" + ("A" * 43)


def test_deploy_key_archive_validates_after_source_transitions_to_public() -> None:
    deploy_key_config = create_installed_server_config(
        source=ServerSourceConfig(
            origin="git@github.com:openai/rcp.git",
            authentication="deploy_key",
            public_key_fingerprint=FINGERPRINT,
        ),
        installation_id=INSTALLATION_ID,
    )
    archived = BackupArchiveManifest(
        space_id="70994440-4c57-41b0-a2f6-8878856db969",
        space_name="Research lab",
        rcp_source_commit="a" * 40,
        database_schema_sha256="b" * 64,
        captured_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        sqlite_snapshot=BackupFileEntry(
            archive_path="database/rcp.sqlite3",
            source_relative_path="rcp.sqlite3",
            group="sqlite_snapshot",
            sha256="c" * 64,
            size_bytes=1,
        ),
        encryption_recipient_fingerprint="d" * 64,
        installation_id=deploy_key_config.installation_id,
        source_deploy_key_label=f"rcp-source:{deploy_key_config.installation_id}",
        source_public_key_fingerprint=deploy_key_config.source.public_key_fingerprint,
        excluded_app_data_entries=("rcp.lock",),
        uncaptured_app_data_entries=(),
        projects=(),
        status="complete",
        total_bytes=1,
    )
    public_config = deploy_key_config.model_copy(
        update={
            "source": ServerSourceConfig(
                origin="https://github.com/openai/rcp.git",
                authentication="public",
            )
        }
    )

    validated = BackupArchiveManifest.model_validate_json(archived.model_dump_json())

    assert validated.installation_id == public_config.installation_id
    assert validated.source_deploy_key_label == f"rcp-source:{public_config.installation_id}"
    assert public_config.source.authentication == "public"
