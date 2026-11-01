from __future__ import annotations

import hashlib
import json
import stat
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rcp.server_ops.backup import _write_deterministic_archive, build_archive_manifest
from rcp.server_ops.backup_capture import BackupCaptureCoordinator
from rcp.server_ops.backup_integrity import canonical_backup_manifest_bytes
from rcp.server_ops.backup_models import BackupArchiveManifest, BackupFileEntry
from rcp.server_ops.backup_project_files import BackupProjectFileCaptureCoordinator
from rcp.server_ops.restore import RestorePrepareRequest, prepare_restore
from rcp.storage import AppStore
from tests.test_backup_encryption import _installed
from tests.test_backup_sqlite_capture import _metadata

THREAD = "agent-threads/member/thread.jsonl"
NOTE = "memory/project/shared/notes/note.md"


def _capture(data: Path, destination: Path, *, stale_transcript: bool = False):
    store, bootstrap = AppStore.initialize_team_space(data / "rcp.sqlite3", "Backup lab")
    store.enroll_team_member(bootstrap, "Member")
    if stale_transcript:
        stale = store.preprovision_team_member("Departed member")
        path = data / "agent-threads" / stale.user_id / "thread.jsonl"
        path.parent.mkdir(parents=True)
        path.write_bytes(b'{"text":"private"}\n')
    with store.connection() as connection:
        connection.execute(
            "INSERT INTO memory_migrations VALUES (?,?,?)", ("project", store.now(), 1)
        )
        connection.execute(
            "INSERT INTO memory_deletions VALUES (?,?,?,?)", ("pending", "/old", "{}", None)
        )
    snapshot = BackupCaptureCoordinator(store, data, _metadata(data)).capture_sqlite()
    store.close()
    files = BackupProjectFileCaptureCoordinator(data).capture(
        snapshot.receipt_path, expected_sha256=snapshot.receipt_sha256
    )
    manifest = build_archive_manifest(
        installed=_installed(destination),
        sqlite_receipt=snapshot.receipt,
        project_publication=files,
    )
    return manifest, snapshot.receipt_path.parent


def _restore(
    tmp_path: Path,
    manifest: BackupArchiveManifest,
    capture_root: Path,
    *,
    remove_member: str | None = None,
):
    archive = tmp_path / "backup.tar"
    with archive.open("wb") as stream:
        _write_deterministic_archive(stream, manifest, capture_root)
    data = tmp_path / "replacement"
    data.mkdir(mode=0o700)
    output = tmp_path / "restore"
    request = RestorePrepareRequest(
        version=1,
        data_dir=str(data),
        output_dir=str(output),
        plaintext_path=str(archive),
        plaintext_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        recipient_fingerprint=manifest.encryption_recipient_fingerprint,
        source_commit="a" * 40,
        preparation_id=str(uuid.uuid4()),
        detached_at=datetime.now(UTC),
        confirmed_data_dir=str(data),
        confirmed_by="test operator",
        previous_roots=(),
        remove_stale_member=remove_member,
    )
    reply = prepare_restore(request)
    assert reply["status"] == "operator_action_needed"
    review_fields = reply["fields"]
    assert isinstance(review_fields, list)
    fields = {item["name"]: item["value"] for item in review_fields}
    output = tmp_path / "prepared"
    reply = prepare_restore(
        request.model_copy(
            update={
                "output_dir": str(output),
                "old_authority_disposition": "old-machine-fenced-and-credentials-revoked",
                "confirm_old_authority": fields["old_authority_boundary"],
                "confirm_member_roster": fields["member_roster_boundary"],
            }
        )
    )
    assert reply["status"] == "prepared"
    for entry in manifest.app_data_files:
        if entry.group == "space_thread" and Path(entry.archive_path).parts[1] == remove_member:
            assert not (output / "baseline/overlay/data" / entry.archive_path).exists()
            continue
        assert (output / "baseline/overlay/data" / entry.archive_path).read_bytes() == (
            output / "app-data" / entry.archive_path
        ).read_bytes()
    return output / "app-data"


def test_space_transcripts_and_memory_round_trip(tmp_path: Path):
    data = tmp_path / "data"
    for name, content in (
        (THREAD, b'{"text":"saved"}\n{"text":"torn'),
        (NOTE, b"# Remember\n"),
        ("agent-threads/member/stages/live/patch.json", b"{}"),
    ):
        path = data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    manifest, root = _capture(data, tmp_path / "backups")
    restored = _restore(tmp_path, manifest, root)
    assert manifest.status == "complete"
    assert (restored / THREAD).read_bytes() == b'{"text":"saved"}\n'
    assert (restored / NOTE).read_bytes() == b"# Remember\n"
    assert not (restored / "agent-threads/member/stages").exists()
    for name in (THREAD, NOTE):
        assert stat.S_IMODE((restored / name).stat().st_mode) == 0o600
    with (
        closing(AppStore.open_read_only_snapshot(restored / "rcp.sqlite3")) as store,
        store.connection() as connection,
    ):
        assert connection.execute("SELECT lesson_count FROM memory_migrations").fetchone()[0] == 1
        assert not connection.execute("SELECT 1 FROM memory_deletions").fetchall()


@pytest.mark.parametrize("name", [THREAD, NOTE, "memory/project", "agent-threads/member"])
def test_unsafe_app_data_is_visibly_uncaptured(tmp_path: Path, name: str):
    data = tmp_path / "data"
    link = data / name
    link.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"private\n")
    link.symlink_to(outside)
    manifest, root = _capture(data, tmp_path / "backups")
    assert manifest.status == "partial"
    assert name.split("/")[0] in manifest.uncaptured_app_data_entries
    assert not (root / name).exists()
    assert outside.read_bytes() == b"private\n"


def test_archive_without_app_data_groups_still_restores(tmp_path: Path):
    manifest, root = _capture(tmp_path / "data", tmp_path / "backups")
    historical = json.loads(canonical_backup_manifest_bytes(manifest))
    historical.pop("app_data_files", None)
    manifest = BackupArchiveManifest.model_validate_json(json.dumps(historical))
    assert "app_data_files" not in json.loads(canonical_backup_manifest_bytes(manifest))
    restored = _restore(tmp_path, manifest, root)
    assert not (restored / "agent-threads").exists()
    assert not (restored / "memory").exists()


@pytest.mark.parametrize(
    ("group", "path"),
    [
        ("space_thread", "agent-threads/member/stages/file.jsonl"),
        ("space_thread", "other/member/thread.jsonl"),
        ("local_memory", "memory/project/../../outside"),
        ("local_memory", "outside/project/note.md"),
    ],
)
def test_app_data_entries_cannot_escape_owned_roots(group: str, path: str):
    with pytest.raises(ValueError):
        BackupFileEntry.model_validate(
            dict(
                archive_path=path,
                source_relative_path=path,
                group=group,
                sha256="a" * 64,
                size_bytes=1,
            )
        )


def test_restore_removing_member_omits_their_space_transcripts(tmp_path: Path):
    manifest, root = _capture(tmp_path / "data", tmp_path / "backups", stale_transcript=True)
    entry = next(item for item in manifest.app_data_files if item.group == "space_thread")
    member = Path(entry.archive_path).parts[1]
    restored = _restore(tmp_path, manifest, root, remove_member=member)
    assert not (restored / "agent-threads" / member).exists()
